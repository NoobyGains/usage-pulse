"""Another Claude account as a provider row: token safety, throttling, backoff.

The rules pinned here:

* the token is read from the other dir's credentials file and never stored,
  logged, or refreshed (a refresh would log that account's session out);
* at most one request per ``providers_refresh_seconds`` and one *attempt*
  per 60 s whatever the outcome (stamped before the request);
* 429 / 401 / other errors back off and keep the old value;
* at most one network refresh per repaint across all rows;
* each window seeds its own account's cache from stdin.

Run: ``python -m unittest tests.test_provider_claude``
"""
import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, cs  # noqa: E402

API_USAGE = {
    "five_hour": {"utilization": 34.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    "seven_day": {"utilization": 9.0, "resets_at": "2099-01-05T00:00:00+00:00"},
    "seven_day_opus": {"utilization": 1.0, "resets_at": None},
    "extra_usage": {"is_enabled": False},
}


def _http_error(code, retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after else {}

    class _H(dict):
        def get(self, k, d=None):
            return dict.get(self, k.lower(), d)
    return urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", code,
                                  "err", _H(headers), io.BytesIO(b""))


class ClaudeProviderTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.main = self.sb.claude_dir(".claude", token="main-token")
        self.other = self.sb.claude_dir(".claude-b", token="other-secret-token")
        self.entry = {"id": "claude:b", "type": "claude", "dir": "~/.claude-b", "enabled": True}
        self.config = {"providers": [dict(self.entry)], "providers_refresh_seconds": 300}
        # A refresh must never happen, from any path.
        for name in ("_refresh_oauth_token", "refresh_and_retry"):
            p = mock.patch.object(cs, name, side_effect=AssertionError(f"{name} called"))
            p.start()
            self.addCleanup(p.stop)

    def _cache_file(self):
        return cs._provider_cache_path("claude:b")

    def test_token_file_rules(self):
        token, plan = cs._read_other_claude_token(self.other)
        self.assertEqual(token, "other-secret-token")
        self.assertEqual(plan, "Max 20x")
        expired = self.sb.claude_dir(".claude-x", expires_in=10)       # within 30 s margin
        self.assertEqual(cs._read_other_claude_token(expired)[0], None)
        no_exp = self.sb.claude_dir(".claude-y", expires_in=None)
        self.assertEqual(cs._read_other_claude_token(no_exp)[0], None)
        self.assertEqual(cs._read_other_claude_token(self.sb.home / "nowhere"), (None, None))

    def test_fetch_writes_cache_without_token(self):
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            c = cs._refresh_claude_provider(self.entry, self.config)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][0], "other-secret-token")
        self.assertEqual(fetch.call_args[1].get("timeout"), 3)
        self.assertEqual(c["usage"]["five_hour"]["utilization"], 34.0)
        raw = self._cache_file().read_text(encoding="utf-8")
        self.assertNotIn("secret", raw)
        self.assertNotIn("token", raw.lower())
        stored = json.loads(raw)
        self.assertEqual(set(stored["usage"]), {"five_hour", "seven_day"})
        self.assertEqual(stored["plan"], "Max 20x")

    def test_fresh_cache_means_no_fetch(self):
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            cs._refresh_claude_provider(self.entry, self.config)
            cs._refresh_claude_provider(self.entry, self.config, now=time.time() + 120)
        self.assertEqual(fetch.call_count, 1)

    def test_attempt_throttle_survives_cancelled_run(self):
        # A run Claude Code cancelled mid-request left only the attempt stamp.
        now = time.time()
        cs._write_provider_cache("claude:b", {"timestamp": now - 3600, "last_attempt": now - 10})
        with mock.patch.object(cs, "fetch_usage") as fetch:
            cs._refresh_claude_provider(self.entry, self.config, now=now)
        fetch.assert_not_called()
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            cs._refresh_claude_provider(self.entry, self.config, now=now + 61)
        fetch.assert_called_once()

    def test_attempt_is_stamped_before_the_request(self):
        seen = {}

        def fetch(token, timeout=10):
            seen["cache"] = json.loads(self._cache_file().read_text(encoding="utf-8"))
            raise KeyboardInterrupt  # simulate the run being killed mid-flight

        now = time.time()
        with mock.patch.object(cs, "fetch_usage", side_effect=fetch):
            with self.assertRaises(KeyboardInterrupt):
                cs._refresh_claude_provider(self.entry, self.config, now=now)
        self.assertEqual(seen["cache"]["last_attempt"], now)

    def test_expired_token_never_fetches_or_refreshes(self):
        self.sb.claude_dir(".claude-b", expires_in=-60)
        with mock.patch.object(cs, "fetch_usage") as fetch:
            c = cs._refresh_claude_provider(self.entry, self.config)
        fetch.assert_not_called()
        self.assertEqual(c, {})

    def test_429_backoff_persisted_and_respected(self):
        now = time.time()
        with mock.patch.object(cs, "fetch_usage", side_effect=_http_error(429, retry_after=600)):
            c = cs._refresh_claude_provider(self.entry, self.config, now=now)
        self.assertGreaterEqual(c["rate_limited_until"], now + 600)
        self.assertEqual(c["rate_limit_fails"], 1)
        stored = json.loads(self._cache_file().read_text(encoding="utf-8"))
        self.assertEqual(stored["rate_limit_fails"], 1)
        with mock.patch.object(cs, "fetch_usage") as fetch:
            cs._refresh_claude_provider(self.entry, self.config, now=now + 120)
        fetch.assert_not_called()

    def test_429_keeps_old_value(self):
        now = time.time()
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)):
            cs._refresh_claude_provider(self.entry, self.config, now=now - 1000)
        self.sb.next_repaint()   # the refresh took this repaint's turn on the network
        with mock.patch.object(cs, "fetch_usage", side_effect=_http_error(429)):
            c = cs._refresh_claude_provider(self.entry, self.config, now=now)
        self.assertEqual(c["usage"]["five_hour"]["utilization"], 34.0)

    def test_401_sets_auth_backoff(self):
        now = time.time()
        with mock.patch.object(cs, "fetch_usage", side_effect=_http_error(401)):
            c = cs._refresh_claude_provider(self.entry, self.config, now=now)
        self.assertEqual(c["auth_failed_until"], now + 900)
        with mock.patch.object(cs, "fetch_usage") as fetch:
            cs._refresh_claude_provider(self.entry, self.config, now=now + 600)
        fetch.assert_not_called()

    def test_network_error_backs_off(self):
        now = time.time()
        with mock.patch.object(cs, "fetch_usage", side_effect=OSError("down")):
            c = cs._refresh_claude_provider(self.entry, self.config, now=now)
        self.assertEqual(c["error_until"], now + 300)

    def test_one_network_refresh_per_repaint(self):
        self.sb.claude_dir(".claude-c", token="third")
        config = {"providers": [self.entry, {"id": "claude:c", "type": "claude", "enabled": True}]}
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            rows = cs._collect_provider_rows(config)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(rows), 2)
            self.sb.next_repaint()   # the first refresh took the last repaint's turn
            cs._collect_provider_rows(config)
            self.assertEqual(fetch.call_count, 2)  # the other one, next repaint

    def test_active_account_is_skipped(self):
        self.sb.set_active(self.other)
        config = {"providers": [self.entry, {"id": "claude:default", "type": "claude"}]}
        self.assertEqual(cs._active_provider_id(config), "claude:b")
        self.assertEqual([e["id"] for e in cs._provider_row_entries(config)], ["claude:default"])

    def test_stale_row_after_30_minutes(self):
        now = time.time()
        cs._write_provider_cache("claude:b", {
            "timestamp": now - 2000, "last_attempt": now - 30,
            "usage": {"five_hour": {"utilization": 29.0, "resets_at": None}}})
        rows = cs._collect_provider_rows(self.config, now)
        self.assertTrue(rows[0]["stale"])
        head, _tail = cs._render_provider_row(rows[0]["label"], rows[0]["usage"],
                                              {"show": dict(cs.DEFAULT_SHOW)}, 15, 8,
                                              stale=True)
        self.assertIn("~29%", head)


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude")
        self.b = self.sb.claude_dir(".claude-b")
        self.usage = {"five_hour": {"utilization": 12.0, "resets_at": "2099-01-01T00:00:00+00:00"},
                      "seven_day": {"utilization": 3.0, "resets_at": None},
                      "seven_day_fable": {"utilization": 50.0}}

    def test_seeds_active_id_only(self):
        self.sb.set_active(self.b)
        config = {"providers": [{"id": "claude:b", "type": "claude"},
                                {"id": "claude:default", "type": "claude"}]}
        cs._seed_active_provider_cache(self.usage, config)
        stored = cs._read_provider_cache("claude:b")
        self.assertEqual(stored["usage"]["five_hour"]["utilization"], 12.0)
        self.assertNotIn("seven_day_fable", stored["usage"])
        self.assertIsNone(cs._read_provider_cache("claude:default"))

    def test_no_providers_no_write(self):
        cs._seed_active_provider_cache(self.usage, {"providers": []})
        self.assertFalse((self.sb.state / "providers").exists())

    def test_unconfigured_active_uses_derived_id(self):
        config = {"providers": [{"id": "codex", "type": "codex"}]}
        cs._seed_active_provider_cache(self.usage, config)  # active = ~/.claude
        self.assertIsNotNone(cs._read_provider_cache("claude:default"))

    def test_seed_prevents_fetch_in_other_window(self):
        # window B seeds; window A (default account) then needs no request
        self.sb.set_active(self.b)
        config = {"providers": [{"id": "claude:b", "type": "claude"}]}
        cs._seed_active_provider_cache(self.usage, config)
        self.sb.set_active(self.sb.home / ".claude")
        with mock.patch.object(cs, "fetch_usage") as fetch:
            rows = cs._collect_provider_rows(config)
        fetch.assert_not_called()
        self.assertEqual(rows[0]["usage"]["five_hour"]["utilization"], 12.0)

    def test_main_stdin_path_seeds(self):
        self.sb.claude_dir(".claude", expires_in=-60)  # the other row must not fetch
        self.sb.set_active(self.b)
        cfg_path = cs.get_config_path()
        cfg_path.write_text(json.dumps({"providers": [
            {"id": "claude:b", "type": "claude"}, {"id": "claude:default", "type": "claude"}]}),
            encoding="utf-8")
        now = int(time.time())
        payload = json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 41, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 6, "resets_at": now + 86400}}})
        out = io.BytesIO()

        class _Stdout:
            buffer = out

        with mock.patch.object(cs, "check_for_update", lambda: False), \
                mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: False), \
                mock.patch.object(cs, "fetch_usage", side_effect=AssertionError("network")), \
                mock.patch.object(cs.sys, "argv", ["claude_status.py"]), \
                mock.patch.object(cs.sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(cs.sys, "stdout", _Stdout()):
            cs.main()
        self.assertEqual(cs._read_provider_cache("claude:b")["usage"]["five_hour"]["utilization"],
                         41.0)
        text = out.getvalue().decode("utf-8")
        self.assertEqual(len(text.rstrip("\n").split("\n")), 2)  # main line + Claude row



class RefreshLockTest(unittest.TestCase):
    """Two windows must not both fetch one account in the same second."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude", token="main-token")
        self.sb.claude_dir(".claude-b", token="other-secret-token")
        self.entry = {"id": "claude:b", "type": "claude", "dir": "~/.claude-b", "enabled": True}
        self.config = {"providers": [dict(self.entry)], "providers_refresh_seconds": 300}
        self.lock = cs._provider_cache_path("claude:b", create=True).with_suffix(".lock")

    def test_live_lock_from_another_window_skips_the_fetch(self):
        self.lock.write_text("", encoding="utf-8")
        with mock.patch.object(cs, "fetch_usage", side_effect=AssertionError("fetched")):
            cs._refresh_claude_provider(self.entry, self.config)
        self.assertTrue(self.lock.exists(), "another window's lock is left alone")

    def test_stale_lock_is_broken_then_released(self):
        self.lock.write_text("", encoding="utf-8")
        old = time.time() - cs._PROVIDER_LOCK_STALE - 5
        os.utime(self.lock, (old, old))
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fu:
            cs._refresh_claude_provider(self.entry, self.config)
        self.assertEqual(fu.call_count, 1)
        self.assertFalse(self.lock.exists())

    def test_lock_released_after_an_error(self):
        with mock.patch.object(cs, "fetch_usage", side_effect=OSError("down")):
            cs._refresh_claude_provider(self.entry, self.config)
        self.assertFalse(self.lock.exists())

    def test_unsaved_attempt_means_no_fetch(self):
        with mock.patch.object(cs, "_write_provider_cache", return_value=False), \
                mock.patch.object(cs, "fetch_usage", side_effect=AssertionError("fetched")):
            cs._refresh_claude_provider(self.entry, self.config)

    def test_second_window_sees_the_first_windows_stamp(self):
        # Window A stamps and fetches; window B (same second) must not fetch.
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fu:
            cs._refresh_claude_provider(self.entry, self.config, now=1_000_000.0)
            cs._refresh_claude_provider(self.entry, self.config, now=1_000_000.0)
        self.assertEqual(fu.call_count, 1)

if __name__ == "__main__":
    unittest.main()
