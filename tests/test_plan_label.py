"""The plan's sold name from the OAuth profile (``organization.plan_display_name``).

The credentials file carries only the rate-limit tier, so an Ultra account
read "Max 20x". Rules pinned here:

* only ``organization.plan_display_name`` is read, through the Anthropic
  allowlist, with the OAuth beta header; nothing else in the reply is kept;
* the profile is asked only while the plan is drawn (``show.plan``), at most
  once a day, once an hour after a failure, never during a 429 backoff, with
  the attempt stamped before the request, and it takes its turn with the
  other optional network checks;
* the main line, the cached line and another account's row all show the
  profile's name, and the tier remains the fallback;
* this window seeds its own name into its provider cache for the other window.

Run: ``python -m unittest tests.test_plan_label``
"""
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, cs  # noqa: E402
from test_provider_rendering import plain  # noqa: E402

PROFILE = {"account": {"uuid": "u", "email": "someone@example.com", "full_name": "Some One"},
           "organization": {"uuid": "o", "plan_display_name": "Ultra",
                            "rate_limit_tier": "default_claude_max_20x"}}


def _resp(payload):
    m = mock.MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return m


class FetchPlanLabelTest(unittest.TestCase):
    def _fetch(self, payload):
        with mock.patch.object(cs, "_authorized_request", return_value=_resp(payload)) as req:
            label = cs.fetch_plan_label("tok")
        return label, req

    def test_reads_only_the_plan_name(self):
        label, req = self._fetch(PROFILE)
        self.assertEqual(label, "Ultra")
        args, kwargs = req.call_args
        self.assertEqual(args[0], "https://api.anthropic.com/api/oauth/profile")
        self.assertEqual(args[1], "tok")
        self.assertEqual(kwargs["headers"]["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(kwargs.get("timeout"), cs._PLAN_LABEL_TIMEOUT)

    def test_missing_or_odd_values_give_none(self):
        for payload in ({}, [], {"organization": None}, {"organization": {}},
                        {"organization": {"plan_display_name": 7}},
                        {"organization": {"plan_display_name": "   "}}):
            with self.subTest(payload=payload):
                self.assertIsNone(self._fetch(payload)[0])

    def test_name_is_sanitised_and_short(self):
        label, _ = self._fetch({"organization": {"plan_display_name": "\x1b[31mUltra\x1b[0m " + "x" * 40}})
        self.assertNotIn("\x1b", label)
        self.assertLessEqual(len(label), 24)
        self.assertTrue(label.startswith("Ultra"))

    def test_profile_goes_through_the_allowlist(self):
        with mock.patch.object(cs, "_PROFILE_URL", "https://evil.example/profile"):
            with self.assertRaises(ValueError):
                cs.fetch_plan_label("tok")


class RefreshGateTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude")
        self.calls = []
        for name, value in (("_begin_net_attempt", lambda key, backoff=None, primary=False: True),
                            ("_end_net_attempt", lambda key: None),
                            ("get_credentials", lambda: ("tok", "Max 20x")),
                            ("fetch_plan_label", lambda tok: self.calls.append(tok) or "Ultra")):
            p = mock.patch.object(cs, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.on = {"show": {"plan": True}}
        self.off = {"show": {"plan": False}}

    def test_nothing_happens_while_the_plan_is_hidden(self):
        self.assertIsNone(cs._refresh_plan_label(self.off))
        self.assertEqual(self.calls, [])
        self.assertEqual(cs._display_plan("Max 20x", self.off), "Max 20x")
        self.assertFalse(cs._plan_label_path().exists())

    def test_one_fetch_a_day(self):
        now = time.time()
        self.assertEqual(cs._refresh_plan_label(self.on, now=now), "Ultra")
        self.assertEqual(cs._display_plan("Max 20x", self.on), "Ultra")
        state = json.loads(cs._plan_label_path().read_text(encoding="utf-8"))
        self.assertEqual(state["label"], "Ultra")
        self.assertEqual(state["fetched_at"], now)
        self.assertEqual(state["attempted_at"], now)
        self.assertNotIn("tok", json.dumps(state))
        cs._refresh_plan_label(self.on, now=now + 3600)
        cs._refresh_plan_label(self.on, now=now + cs._PLAN_LABEL_TTL - 5)
        self.assertEqual(len(self.calls), 1)
        cs._refresh_plan_label(self.on, now=now + cs._PLAN_LABEL_TTL + 1)
        self.assertEqual(len(self.calls), 2)

    def test_a_failure_is_stamped_and_waits_an_hour(self):
        now = time.time()
        with mock.patch.object(cs, "fetch_plan_label", side_effect=OSError("down")):
            self.assertIsNone(cs._refresh_plan_label(self.on, now=now))
            state = json.loads(cs._plan_label_path().read_text(encoding="utf-8"))
            self.assertEqual(state["attempted_at"], now)
            self.assertNotIn("label", state)
            self.assertEqual(cs._display_plan("Max 20x", self.on), "Max 20x")
        cs._refresh_plan_label(self.on, now=now + cs._PLAN_LABEL_RETRY - 5)
        self.assertEqual(self.calls, [])
        self.assertEqual(cs._refresh_plan_label(self.on, now=now + cs._PLAN_LABEL_RETRY + 1), "Ultra")

    def test_an_empty_answer_counts_as_an_attempt(self):
        now = time.time()
        with mock.patch.object(cs, "fetch_plan_label", lambda tok: None):
            self.assertIsNone(cs._refresh_plan_label(self.on, now=now))
        cs._refresh_plan_label(self.on, now=now + 60)
        self.assertEqual(self.calls, [])

    def test_backoff_token_and_turn_taking_are_respected(self):
        now = time.time()
        self.assertIsNone(cs._refresh_plan_label(self.on, blocked_until=now + 100, now=now))
        with mock.patch.object(cs, "get_credentials", lambda: (None, None)):
            self.assertIsNone(cs._refresh_plan_label(self.on, now=now))
        with mock.patch.object(cs, "_begin_net_attempt", lambda key, backoff=None, primary=False: False):
            self.assertIsNone(cs._refresh_plan_label(self.on, now=now))
        self.assertEqual(self.calls, [])
        self.assertFalse(cs._plan_label_path().exists())   # waiting leaves no stamp
        # A token in hand skips the credentials read.
        with mock.patch.object(cs, "get_credentials", side_effect=AssertionError("read")):
            self.assertEqual(cs._refresh_plan_label(self.on, token="given", now=now), "Ultra")
        self.assertEqual(self.calls, ["given"])

    def test_a_damaged_state_file_is_ignored(self):
        cs._plan_label_path().parent.mkdir(parents=True, exist_ok=True)
        cs._plan_label_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(cs._display_plan("Max 20x", self.on), "Max 20x")
        self.assertEqual(cs._refresh_plan_label(self.on), "Ultra")


class MainLineTest(unittest.TestCase):
    """The plan widget shows the profile's name, fetched once across repaints."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        cfg_dir = self.sb.home / ".claude"
        cfg_dir.mkdir(exist_ok=True)
        self.calls = []
        show = dict(cs.DEFAULT_SHOW, plan=True, session=True, weekly=True, model=False,
                    branch=False, context=False, update=False, claude_update=False)
        self.cfg = {"show": show, "layout": "standard", "providers": []}
        for target, value in (("_claude_config_dir", lambda: cfg_dir),
                              ("_detect_terminal_width", lambda: None),
                              ("check_for_update", lambda: False),
                              ("check_claude_code_update", lambda v=None: False),
                              ("get_credentials", lambda: ("tok", "Max 20x")),
                              ("fetch_usage", lambda token, timeout=10: {
                                  "five_hour": {"utilization": 1, "resets_at": None},
                                  "seven_day": {"utilization": 2, "resets_at": None}}),
                              ("fetch_plan_label", lambda tok: self.calls.append(tok) or "Ultra"),
                              ("_begin_net_attempt", lambda key, backoff=None, primary=False: True),
                              ("_end_net_attempt", lambda key: None),
                              ("load_config", lambda: dict(self.cfg, show=dict(self.cfg["show"])))):
            p = mock.patch.object(cs, target, value)
            p.start()
            self.addCleanup(p.stop)

    def _repaint(self, payload):
        stdout, stdin, argv = sys.stdout, sys.stdin, sys.argv
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        sys.stdin = io.StringIO(json.dumps(payload))
        sys.stdout = _Out()
        sys.argv = ["claude_status.py"]
        try:
            cs.main()
        except SystemExit:
            pass
        finally:
            sys.stdout, sys.stdin, sys.argv = stdout, stdin, argv
        return plain(buf.getvalue().decode("utf-8", "replace"))

    PAYLOAD = {"session_id": "s", "context_window": {"used_percentage": 1},
               "rate_limits": {"five_hour": {"used_percentage": 10},
                               "seven_day": {"used_percentage": 11}}}

    def test_stdin_repaints_show_the_sold_name_after_one_profile_call(self):
        line = self._repaint(self.PAYLOAD)
        self.assertIn("Ultra", line)
        self.assertNotIn("Max 20x", line)
        self._repaint(self.PAYLOAD)
        self.assertIn("Ultra", self._repaint(self.PAYLOAD))
        self.assertEqual(self.calls, ["tok"])

    def test_hidden_plan_means_no_profile_call(self):
        self.cfg["show"]["plan"] = False
        line = self._repaint(self.PAYLOAD)
        self.assertNotIn("Ultra", line)
        self.assertEqual(self.calls, [])

    def test_api_fallback_shows_the_sold_name(self):
        line = self._repaint({"session_id": "s", "context_window": {"used_percentage": 1}})
        self.assertIn("Ultra", line)
        self.assertEqual(self.calls, ["tok"])


class ProviderRowTest(unittest.TestCase):
    """Another account's row shows that account's sold name, fetched once a day."""

    USAGE = {"five_hour": {"utilization": 34.0, "resets_at": "2099-01-01T00:00:00+00:00"},
             "seven_day": {"utilization": 9.0, "resets_at": "2099-01-05T00:00:00+00:00"}}

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude", token="main-token")
        # The token must outlive the day these tests step through.
        self.sb.claude_dir(".claude-b", token="other-secret-token", expires_in=10 ** 6)
        self.entry = {"id": "claude:b", "type": "claude", "dir": "~/.claude-b", "enabled": True}
        self.calls = []
        for name, value in (("_begin_net_attempt", lambda key, backoff=None, primary=False: True),
                            ("_end_net_attempt", lambda key: None),
                            ("fetch_usage", lambda tok, timeout=10: dict(self.USAGE)),
                            ("fetch_plan_label", lambda tok: self.calls.append(tok) or "Ultra")):
            p = mock.patch.object(cs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _config(self, plan):
        return {"providers": [dict(self.entry)], "providers_refresh_seconds": 300,
                "show": dict(cs.DEFAULT_SHOW, plan=plan)}

    def test_row_tier_is_the_sold_name(self):
        now = time.time()
        config = self._config(True)
        c = cs._refresh_claude_provider(self.entry, config, now=now)
        self.assertEqual((c["plan"], c["plan_label"]), ("Max 20x", "Ultra"))
        self.assertEqual(self.calls, ["other-secret-token"])
        raw = cs._provider_cache_path("claude:b").read_text(encoding="utf-8")
        self.assertNotIn("secret", raw)
        rows = cs._collect_provider_rows(config, now=now)
        self.assertEqual(rows[0]["tier"], "Ultra")
        # The next usage refresh keeps the name without asking again.
        c = cs._refresh_claude_provider(self.entry, config, now=now + 301)
        self.assertEqual(c["plan_label"], "Ultra")
        self.assertEqual(len(self.calls), 1)
        c = cs._refresh_claude_provider(self.entry, config, now=now + cs._PLAN_LABEL_TTL + 301)
        self.assertEqual(len(self.calls), 2)

    def test_hidden_tiers_mean_no_profile_call(self):
        config = self._config(False)
        c = cs._refresh_claude_provider(self.entry, config)
        self.assertEqual(c["plan"], "Max 20x")
        self.assertNotIn("plan_label", c)
        self.assertEqual(self.calls, [])
        self.assertEqual(cs._collect_provider_rows(config)[0]["tier"], "Max 20x")

    def test_a_failed_look_up_keeps_the_tier_and_retries_in_an_hour(self):
        now = time.time()
        config = self._config(True)
        with mock.patch.object(cs, "fetch_plan_label", side_effect=OSError("down")):
            c = cs._refresh_claude_provider(self.entry, config, now=now)
        self.assertNotIn("plan_label", c)
        self.assertEqual(cs._collect_provider_rows(config, now=now)[0]["tier"], "Max 20x")
        cs._refresh_claude_provider(self.entry, config, now=now + 301)
        self.assertEqual(self.calls, [])          # inside the hour
        c = cs._refresh_claude_provider(self.entry, config, now=now + cs._PLAN_LABEL_RETRY + 301)
        self.assertEqual(c["plan_label"], "Ultra")

    def test_this_window_seeds_its_own_name(self):
        config = self._config(True)
        cs._plan_label_path().parent.mkdir(parents=True, exist_ok=True)
        cs._plan_label_path().write_text(json.dumps({"label": "Ultra", "fetched_at": time.time()}))
        cs._seed_active_provider_cache(dict(self.USAGE), config)
        pid = cs._active_provider_id(config) or cs._claude_provider_id_for_dir(cs._claude_config_dir())
        seeded = cs._read_provider_cache(pid)
        self.assertEqual(seeded["plan_label"], "Ultra")


if __name__ == "__main__":
    unittest.main()
