"""Regression tests for the stdin-data fixes F01, F08, F12 and F17.

F01  a window whose reset has passed must not keep its old percentage.
F08  a Claude apps gateway user (spend_limit alone on stdin) and a user with no
     claude.ai login must still get the stdin widgets, not "No credentials".
F12  after /compact the context bar must not keep the pre-compact value, and
     tokens mode counts input tokens only (like the percentage does).
F17  extra usage refreshes while it is on screen, is converted to the local
     currency like the other money widgets, and says so when it is old.

Every case runs the real ``main()`` against a throwaway state dir with the
network blocked; nothing touches the real home.

Run: ``python tests/test_pr2_stdin_data.py``
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _iso(offset_seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class MainHarness(unittest.TestCase):
    """Runs main() against a sandbox; subclasses call ``self.repaint``."""

    CURRENCY = "$"
    SHOW_OVERRIDES = {}

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.state = root / "state"
        self.state.mkdir()
        cfg_dir = root / "claude"
        cfg_dir.mkdir()
        self.fetches = []
        self.cred_calls = []
        self.token = ("token", "Max 20x")
        self.fetch_result = {"five_hour": {"utilization": 1, "resets_at": None},
                             "seven_day": {"utilization": 2, "resets_at": None}}

        def fake_fetch(token, timeout=10):
            self.fetches.append(timeout)
            return json.loads(json.dumps(self.fetch_result))

        def fake_creds():
            self.cred_calls.append(1)
            return self.token

        def blocked(*_a, **_k):
            raise AssertionError("the network must not be touched in these tests")

        for target, value in (("get_state_dir", lambda: self.state),
                              ("_claude_config_dir", lambda: cfg_dir),
                              ("get_config_path", lambda: root / "config.json"),
                              ("check_for_update", lambda: False),
                              ("check_claude_code_update", lambda: False),
                              ("get_credentials", fake_creds),
                              ("fetch_usage", fake_fetch)):
            patcher = mock.patch.object(cs, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(urllib.request, "urlopen", blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

        self.config = {"show": dict(cs.DEFAULT_SHOW, branch=False, providers=False,
                                    **self.SHOW_OVERRIDES),
                       "layout": "standard", "currency": self.CURRENCY}
        patcher = mock.patch.object(cs, "load_config",
                                    lambda: dict(self.config, show=dict(self.config["show"])))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache_path = Path(cs.get_cache_path())

    # -- helpers -----------------------------------------------------------
    def repaint(self, payload=None, raw=None):
        stdout, stdin = sys.stdout, sys.stdin
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        if raw is None:
            raw = "" if payload is None else json.dumps(payload)
        sys.stdin = io.StringIO(raw)
        sys.stdout = _Out()
        try:
            cs.main()
        except SystemExit:
            pass
        finally:
            sys.stdout, sys.stdin = stdout, stdin
        return ANSI.sub("", buf.getvalue().decode("utf-8")).strip()

    @staticmethod
    def payload(sid="s1", five=None, seven=None, extra_rl=None, context=None,
                cost=None, rate_limits=True, **more):
        now = time.time()
        p = {"session_id": sid, "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
             "workspace": {"current_dir": "."}}
        if context is not None:
            p["context_window"] = context
        if cost is not None:
            p["cost"] = {"total_cost_usd": cost}
        if rate_limits:
            rl = {}
            if five is not None:
                rl["five_hour"] = {"used_percentage": five[0], "resets_at": now + five[1]}
            if seven is not None:
                rl["seven_day"] = {"used_percentage": seven[0], "resets_at": now + seven[1]}
            if extra_rl:
                rl.update(extra_rl)
            if rl:
                p["rate_limits"] = rl
        p.update(more)
        return p

    def read_cache(self):
        return json.loads(self.cache_path.read_text(encoding="utf-8"))

    def write_cache(self, data):
        self.cache_path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# F01
# ---------------------------------------------------------------------------
class RolloverHelperTest(unittest.TestCase):
    def test_past_window_reads_zero_without_a_reset(self):
        usage = {"five_hour": {"utilization": 95.0, "resets_at": _iso(-5)},
                 "seven_day": {"utilization": 40.0, "resets_at": _iso(3600)},
                 "seven_day_opus": {"utilization": 70.0, "resets_at": _iso(-1)},
                 "extra_usage": {"is_enabled": True, "used_credits": 5}}
        out = cs._roll_over_expired_windows(usage)
        self.assertEqual(out["five_hour"]["utilization"], 0.0)
        self.assertIsNone(out["five_hour"]["resets_at"])
        self.assertEqual(out["seven_day"]["utilization"], 40.0)
        self.assertEqual(out["seven_day_opus"]["utilization"], 0.0)
        self.assertEqual(out["extra_usage"], usage["extra_usage"])
        self.assertEqual(usage["five_hour"]["utilization"], 95.0, "input was mutated")

    def test_unusable_input_is_left_alone(self):
        for bad in (None, [], "x", 3):
            self.assertEqual(cs._roll_over_expired_windows(bad), bad)
        usage = {"five_hour": {"utilization": 9.0, "resets_at": None},
                 "seven_day": "junk", "seven_day_x": {"utilization": 1, "resets_at": "not a date"}}
        self.assertEqual(cs._roll_over_expired_windows(usage), usage)


class WindowRolloverTest(MainHarness):
    def _age_cache_window(self, key, *, cache_age=0):
        data = self.read_cache()
        data["usage"][key]["resets_at"] = _iso(-30)
        data["timestamp"] = time.time() - cache_age
        self.write_cache(data)

    def _dropped_five_hour(self, cache_age):
        self.repaint(self.payload(five=(95, 3600), seven=(40, 86400)))
        self._age_cache_window("five_hour", cache_age=cache_age)
        # Claude Code drops five_hour once its resets_at has passed.
        line = self.repaint(self.payload(seven=(40, 86400)))
        self.assertNotIn("95%", line)
        self.assertNotIn(" now", line)
        self.assertIn("40%", line)
        usage = self.read_cache()["usage"]
        self.assertEqual((usage.get("five_hour") or {}).get("utilization", 0), 0,
                         "an expired window was written back to cache.json")

    def test_dropped_five_hour_is_not_refilled_from_a_fresh_cache(self):
        self._dropped_five_hour(cache_age=0)

    def test_dropped_five_hour_is_not_refilled_from_an_expired_cache(self):
        self._dropped_five_hour(cache_age=1000)   # past the 60 s TTL

    def test_dropped_seven_day_is_not_refilled_from_cache(self):
        self.repaint(self.payload(five=(30, 3600), seven=(88, 86400)))
        self._age_cache_window("seven_day")
        line = self.repaint(self.payload(five=(30, 3600)))
        self.assertNotIn("88%", line)

    def test_new_session_does_not_inherit_an_expired_window(self):
        self.repaint(self.payload("s1", five=(95, 3600), seven=(40, 86400)))
        ctx_path = cs.get_account_state_dir() / "stdin_ctx.json"
        persisted = json.loads(ctx_path.read_text(encoding="utf-8"))
        persisted["_rate_limits"]["five_hour"]["resets_at"] = _iso(-30)
        ctx_path.write_text(json.dumps(persisted), encoding="utf-8")
        # A new session, before its first reply: no rate_limits on stdin at all.
        line = self.repaint(self.payload("s2", rate_limits=False))
        self.assertNotIn("95%", line)
        self.assertNotIn(" now", line)
        self.assertIn("40%", line, "the still-valid weekly window must survive")

    def test_cached_branch_does_not_draw_an_expired_window(self):
        self.repaint(self.payload(five=(95, 3600), seven=(40, 86400)))
        self._age_cache_window("five_hour")
        # No stdin at all, no persisted rate limits: the fresh cache is drawn.
        (cs.get_account_state_dir() / "stdin_ctx.json").unlink()
        line = self.repaint(raw="")
        self.assertNotIn("95%", line)
        self.assertNotIn(" now", line)

    def test_live_window_is_untouched(self):
        line = self.repaint(self.payload(five=(95, 3600), seven=(40, 86400)))
        self.assertIn("95%", line)
        self.assertIn("40%", line)


# ---------------------------------------------------------------------------
# F08
# ---------------------------------------------------------------------------
class GatewayAndNoCredentialsTest(MainHarness):
    SPEND = {"spend_limit": {"used_percentage": 62.8, "resets_at": time.time() + 86400 * 20,
                             "used_usd": 314.12, "limit_usd": 500, "period": "monthly"}}

    def setUp(self):
        super().setUp()
        self.token = (None, None)   # a gateway sign-in has no claude.ai token

    def _gateway_payload(self, sid="g1"):
        return self.payload(sid, extra_rl=dict(self.SPEND), rate_limits=True,
                            context={"used_percentage": 30, "total_input_tokens": 300000,
                                     "total_output_tokens": 100, "context_window_size": 1000000},
                            cost=1.5)

    def test_spend_limit_alone_draws_the_spend_bar(self):
        for _ in range(2):   # the second repaint used to serve a cached message
            line = self.repaint(self._gateway_payload())
            self.assertNotIn("No credentials", line)
            self.assertIn("spend", line)
            self.assertIn("314", line)
            self.assertIn("30%", line, "the context bar was lost")

    def test_a_repaint_without_stdin_keeps_drawing_the_spend_bar(self):
        self.repaint(self._gateway_payload())
        line = self.repaint(raw="")   # a timed repaint: the persisted context
        self.assertIn("spend", line)
        self.assertNotIn("No credentials", line)
        self.assertEqual(self.cred_calls, [])

    def test_gateway_user_gets_no_session_or_weekly_placeholder(self):
        line = self.repaint(self._gateway_payload())
        self.assertNotIn("Session", line)
        self.assertNotIn("Weekly", line)

    def test_gateway_user_never_touches_credentials_or_usage_api(self):
        self.repaint(self._gateway_payload())
        self.repaint(self._gateway_payload())
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.cred_calls, [])

    def test_spend_limit_next_to_windows_still_skips_credentials_and_api(self):
        payload = self.payload("g3", five=(20, 3600), seven=(30, 86400),
                               extra_rl=dict(self.SPEND))
        line = self.repaint(payload)
        self.assertIn("spend", line)
        self.assertIn("20%", line)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.cred_calls, [])

    def test_session_data_without_credentials_beats_the_login_message(self):
        # Bedrock / Vertex / API-key: model, context and cost, no rate_limits.
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        payload = self.payload(rate_limits=False,
                               context={"used_percentage": 30, "total_input_tokens": 300000,
                                        "total_output_tokens": 100, "context_window_size": 1000000},
                               cost=1.5)
        line = self.repaint(payload)
        self.assertNotIn("No credentials", line)
        self.assertNotIn("API key detected", line)
        self.assertIn("30%", line)
        self.assertEqual(self.fetches, [])

    def test_nothing_on_stdin_keeps_the_login_message(self):
        self.assertIn("No credentials", self.repaint(raw=""))

    def test_nothing_on_stdin_with_api_key_keeps_its_message(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        self.assertIn("API key detected", self.repaint(raw=""))

    def test_subscriber_without_rate_limits_still_uses_the_api(self):
        self.token = ("token", "Max 20x")
        self.fetch_result = {"five_hour": {"utilization": 33, "resets_at": None},
                             "seven_day": {"utilization": 44, "resets_at": None}}
        line = self.repaint(self.payload(rate_limits=False))
        self.assertEqual(len(self.fetches), 1)
        self.assertIn("33%", line)

    def test_an_expired_spend_limit_does_not_count_as_a_gateway(self):
        stale = {"spend_limit": dict(self.SPEND["spend_limit"], resets_at=time.time() - 60)}
        line = self.repaint(self.payload("g2", extra_rl=stale))
        self.assertNotIn("spend", line)


# ---------------------------------------------------------------------------
# F12
# ---------------------------------------------------------------------------
COMPACTED = {"used_percentage": None, "remaining_percentage": None, "current_usage": None,
             "total_input_tokens": 0, "total_output_tokens": 0, "context_window_size": 1000000}


class CompactTest(MainHarness):
    def _full(self, input_tokens=920000, output_tokens=5000, pct=92):
        return {"used_percentage": pct, "current_usage": {"input_tokens": 1},
                "total_input_tokens": input_tokens, "total_output_tokens": output_tokens,
                "context_window_size": 1000000}

    def test_percent_mode_clears_after_compact(self):
        self.assertIn("92%", self.repaint(self.payload("c1", seven=(10, 86400),
                                                       context=self._full())))
        line = self.repaint(self.payload("c1", seven=(10, 86400), context=dict(COMPACTED)))
        self.assertNotIn("92%", line)
        self.assertNotIn("Context", line)

    def test_tokens_mode_never_shows_zero_next_to_the_old_bar(self):
        self.config["context_format"] = "tokens"
        self.repaint(self.payload("c1", seven=(10, 86400), context=self._full()))
        line = self.repaint(self.payload("c1", seven=(10, 86400), context=dict(COMPACTED)))
        self.assertNotIn("0/1M", line)
        self.assertNotIn("Context", line)

    def test_the_bar_comes_back_on_the_next_api_response(self):
        self.repaint(self.payload("c1", seven=(10, 86400), context=self._full()))
        self.repaint(self.payload("c1", seven=(10, 86400), context=dict(COMPACTED)))
        line = self.repaint(self.payload("c1", seven=(10, 86400),
                                         context=self._full(200000, 300, 20)))
        self.assertIn("20%", line)

    def test_compact_is_stored_as_cleared_not_kept(self):
        self.repaint(self.payload("c1", seven=(10, 86400), context=self._full()))
        self.repaint(self.payload("c1", seven=(10, 86400), context=dict(COMPACTED)))
        persisted = json.loads((cs.get_account_state_dir() / "stdin_ctx.json").read_text("utf-8"))
        self.assertIsNone(persisted.get("context_pct"))

    def test_tokens_mode_counts_input_tokens_only(self):
        self.config["context_format"] = "tokens"
        line = self.repaint(self.payload("c2", seven=(10, 86400),
                                         context=self._full(500000, 40000, 50)))
        self.assertIn("500k/1M", line)

    def test_parser_marks_compaction_only_on_a_full_payload_with_no_usage(self):
        full = {"session_id": "x", "model": {"id": "m"}, "context_window": dict(COMPACTED)}
        out = cs._parse_stdin_context(json.dumps(full))
        for key in ("context_pct", "context_used", "context_limit"):
            self.assertIn(key, out)
            self.assertIsNone(out[key], key)
        # A fragment (no session identity) says nothing about the session.
        fragment = {"context_window": dict(COMPACTED)}
        out = cs._parse_stdin_context(json.dumps(fragment))
        self.assertNotIn("context_pct", out)

    def test_a_plain_null_percentage_still_keeps_the_last_value(self):
        # No sign of a compaction (no current_usage key, no zero totals): the
        # merge keeps the session's last known value, as test_stdin_session_scope pins.
        self.repaint(self.payload("c3", seven=(10, 86400), context=self._full(450000, 10, 45)))
        line = self.repaint(self.payload("c3", seven=(10, 86400),
                                         context={"used_percentage": None}))
        self.assertIn("45%", line)


# ---------------------------------------------------------------------------
# F17
# ---------------------------------------------------------------------------
class ExtraUsageTest(MainHarness):
    CURRENCY = "£"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(cs, "_get_exchange_rate", lambda sym: (
            (0.5, "GBP") if sym == "£" else (1.0, "USD")))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_cache(self, extra, *, fetched_ago=None, **more):
        data = {"timestamp": time.time(), "line": "",
                "usage": {"five_hour": {"utilization": 5, "resets_at": _iso(3600)},
                          "seven_day": {"utilization": 6, "resets_at": _iso(86400)},
                          "extra_usage": extra},
                "plan": "Max 20x"}
        if fetched_ago is not None:
            data["model_caps_fetched_at"] = time.time() - fetched_ago
        data.update(more)
        self.write_cache(data)

    EXTRA = {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1000, "utilization": 20.0}

    def _payload(self):
        return self.payload("e1", five=(5, 3600), seven=(6, 86400))

    def test_visible_extra_usage_is_refreshed_when_old(self):
        self._seed_cache(dict(self.EXTRA, used_credits=1234), fetched_ago=40 * 86400)
        self.fetch_result = dict(self.fetch_result,
                                 extra_usage=dict(self.EXTRA, used_credits=2000))
        line = self.repaint(self._payload())
        self.assertEqual(len(self.fetches), 1, "the stale extra figure was never refetched")
        self.assertIn("10.00", line)       # 2000 cents -> $20 -> GBP at 0.5
        self.assertNotIn("6.17", line)     # the 40-day-old 1234 cents

    def test_extra_usage_is_not_refetched_inside_the_ttl(self):
        self._seed_cache(self.EXTRA, fetched_ago=10)
        self.repaint(self._payload())
        self.repaint(self._payload())
        self.assertEqual(self.fetches, [])

    def test_extra_usage_hidden_by_the_user_triggers_no_fetch(self):
        self.config["extra_hidden"] = True
        self._seed_cache(self.EXTRA, fetched_ago=40 * 86400)
        self.repaint(self._payload())
        self.assertEqual(self.fetches, [])

    def test_disabled_extra_usage_triggers_no_fetch(self):
        self._seed_cache({"is_enabled": False}, fetched_ago=40 * 86400)
        self.repaint(self._payload())
        self.assertEqual(self.fetches, [])

    def test_usd_amount_is_converted_like_the_session_cost(self):
        self._seed_cache(self.EXTRA, fetched_ago=10)   # $10.00 used of $50.00
        line = self.repaint(self.payload("e1", five=(5, 3600), seven=(6, 86400), cost=1.5))
        self.assertIn("£5.00/£25.00", line)
        self.assertIn("£0.75", line)   # the session cost, converted the same way

    def test_dollar_users_see_dollars(self):
        self.config["currency"] = "$"
        self._seed_cache(self.EXTRA, fetched_ago=10)
        self.assertIn("$10.00/$50.00", self.repaint(self._payload()))

    def test_amount_only_display_is_converted_too(self):
        self.config["extra_display"] = "amount"
        self._seed_cache(self.EXTRA, fetched_ago=10)
        line = self.repaint(self._payload())
        self.assertIn("£5.00", line)
        self.assertNotIn("£10.00", line)

    def test_other_currency_is_labelled_and_not_converted(self):
        self._seed_cache(dict(self.EXTRA, currency="EUR"), fetched_ago=10)
        line = self.repaint(self._payload())
        self.assertIn("EUR 10.00/EUR 50.00", line)
        self.assertNotIn("£", line)

    def test_same_currency_as_the_users_symbol_is_not_converted(self):
        self._seed_cache(dict(self.EXTRA, currency="GBP"), fetched_ago=10)
        line = self.repaint(self._payload())
        self.assertIn("£10.00/£50.00", line)

    def test_old_figure_carries_a_staleness_marker(self):
        # A rate-limit backoff keeps the refresh away, so the figure stays old.
        self._seed_cache(self.EXTRA, fetched_ago=3 * 86400,
                         rate_limited_until=time.time() + 3600, rate_limited=True)
        line = self.repaint(self._payload())
        self.assertRegex(line, r"Extra .*\(3d ago\)")

    def test_fresh_figure_has_no_marker(self):
        self._seed_cache(self.EXTRA, fetched_ago=30)
        line = self.repaint(self._payload())
        self.assertIn("Extra", line)
        self.assertNotRegex(line, r"Extra[^|]*ago")

    def test_garbage_extra_usage_never_raises(self):
        for bad in ("x", [1], {"is_enabled": True, "monthly_limit": "lots", "used_credits": []},
                    {"is_enabled": True, "monthly_limit": 5000, "used_credits": None,
                     "currency": 7}):
            with self.subTest(bad=bad):
                self._seed_cache(bad, fetched_ago=10)
                self.repaint(self._payload())


if __name__ == "__main__":
    unittest.main()
