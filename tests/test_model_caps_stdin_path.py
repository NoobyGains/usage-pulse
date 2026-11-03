"""Tests for per-model weekly caps on the stdin path.

Claude Code puts only ``five_hour`` and ``seven_day`` in the stdin
``rate_limits`` block (verified against Claude Code 2.1.263 on a Max 20x
account). A per-model weekly cap such as Fable's arrives from the OAuth usage
endpoint only, inside the ``limits[]`` array, as::

    {"kind": "weekly_scoped", "percent": 81,
     "scope": {"model": {"display_name": "Fable"}}}

``_normalize_usage`` folds that into ``seven_day_fable``. Because the stdin
payload always satisfies the "do we have anything to draw" test, the fetch that
collects it has to be triggered on its own schedule rather than only as a
cold-start fallback.

Four properties are pinned here:

* the cap reaches the rendered line at all;
* it is fetched at most once per ``_MODEL_CAPS_TTL``, not once per repaint;
* a 429 records backoff and stops, rather than retrying on the next repaint;
* nothing is fetched when no widget consuming the data is visible.

Run: ``python tests/test_model_caps_stdin_path.py``
"""
import io
import json
import re
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")

FABLE_PAYLOAD = {
    "limits": [{
        "kind": "weekly_scoped",
        "percent": 81,
        "resets_at": "2099-09-14T18:00:00+00:00",
        "scope": {"model": {"display_name": "Fable"}},
    }],
}


class _Headers(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class ModelCapsStdinPath(unittest.TestCase):
    def setUp(self):
        # Every state file — the usage cache above all — lives in a throwaway
        # directory. The first version of this test unlinked the user's real
        # cache.json on every run, and main() would otherwise also read the
        # user's settings.json and run the hourly update checks.
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        state = Path(self._td.name) / "state"
        state.mkdir()
        cfg_dir = Path(self._td.name) / "claude"
        cfg_dir.mkdir()
        # config.json too: without this the test read the developer's real
        # config, and failed on any machine where the cap bars were switched on.
        user_config = Path(self._td.name) / "config.json"
        for target, value in (("get_state_dir", lambda: state),
                              ("_claude_config_dir", lambda: cfg_dir),
                              ("get_config_path", lambda: user_config),
                              ("check_for_update", lambda: False),
                              ("check_claude_code_update", lambda: False)):
            patcher = mock.patch.object(cs, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cache = Path(cs.get_cache_path())

        self._real_fetch = cs.fetch_usage
        self._real_creds = cs.get_credentials
        self._real_config = cs.load_config
        self.addCleanup(setattr, cs, "fetch_usage", self._real_fetch)
        self.addCleanup(setattr, cs, "get_credentials", self._real_creds)
        self.addCleanup(setattr, cs, "load_config", self._real_config)

        cs.get_credentials = lambda: ("token", "Max 20x")
        self.calls = []

        now = time.time()
        self.payload = json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 15, "resets_at": now + 10800},
            "seven_day": {"used_percentage": 66, "resets_at": now + 604800},
        }})

    def _show(self, **overrides):
        """Pin a config whose visibility flags are known, not the user's."""
        base = dict(self._real_config())
        show = dict(base.get("show", {}))
        show.update({"opus": False, "sonnet": False, "fable": True, "extra": False})
        show.update(overrides)
        base["show"] = show
        cs.load_config = lambda: dict(base, show=dict(show))

    def _repaint(self):
        stdout, stdin = sys.stdout, sys.stdin
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        sys.stdin = io.StringIO(self.payload)
        sys.stdout = _Out()
        try:
            cs.main()
        except SystemExit:
            pass
        finally:
            sys.stdout, sys.stdin = stdout, stdin
        return ANSI.sub("", buf.getvalue().decode()).strip()

    def test_cap_renders_and_is_fetched_once_per_ttl(self):
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            return cs._normalize_usage(dict(FABLE_PAYLOAD))

        cs.fetch_usage = fetch
        self._show()

        lines = [self._repaint() for _ in range(3)]

        for line in lines:
            self.assertIn("Fable", line)
            self.assertIn("81%", line)
        self.assertEqual(len(self.calls), 1, "refetched inside the TTL")

        cached = json.loads(self.cache.read_text())
        self.assertIsNotNone(cached["usage"].get("seven_day_fable"))
        self.assertTrue(cached.get("model_caps_fetched_at"))

    def test_expired_ttl_refetches(self):
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            return cs._normalize_usage(dict(FABLE_PAYLOAD))

        cs.fetch_usage = fetch
        self._show()

        self._repaint()
        cached = json.loads(self.cache.read_text())
        # The last attempt happened at the same time as the last fetch.
        cached["model_caps_fetched_at"] = time.time() - cs._MODEL_CAPS_TTL - 1
        cached["model_caps_attempted_at"] = cached["model_caps_fetched_at"]
        self.cache.write_text(json.dumps(cached))
        self._repaint()

        self.assertEqual(len(self.calls), 2, "did not refetch past the TTL")

    def test_rate_limit_records_backoff_and_stops(self):
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            raise urllib.error.HTTPError(
                "usage", 429, "Too Many Requests",
                _Headers({"retry-after": "3113"}), None)

        cs.fetch_usage = fetch
        self._show()

        lines = [self._repaint() for _ in range(3)]

        self.assertEqual(len(self.calls), 1, "kept retrying while rate limited")
        for line in lines:
            self.assertIn("Session", line, "a 429 must not blank the bar")

        cached = json.loads(self.cache.read_text())
        remaining = cached.get("rate_limited_until", 0) - time.time()
        self.assertGreater(remaining, 3000, "Retry-After was not honoured")

    def test_no_fetch_when_no_consuming_widget_is_visible(self):
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            return cs._normalize_usage(dict(FABLE_PAYLOAD))

        cs.fetch_usage = fetch
        self._show(fable=False)

        line = self._repaint()

        self.assertEqual(self.calls, [], "fetched for a hidden widget")
        self.assertIn("Session", line)

    def test_request_timeout_is_bounded(self):
        self.assertLessEqual(cs._MODEL_CAPS_TIMEOUT, 6)
        self.assertGreaterEqual(cs._MODEL_CAPS_TTL, 60)

    def test_caps_are_opt_in_by_default(self):
        """The bars cost a network call, so a stock config must never fetch."""
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            return cs._normalize_usage(dict(FABLE_PAYLOAD))

        cs.fetch_usage = fetch
        base = dict(self._real_config())
        show = dict(base.get("show", {}))
        for key in ("opus", "sonnet", "fable", "extra"):
            show[key] = cs.DEFAULT_SHOW[key]
        base["show"] = show
        cs.load_config = lambda: dict(base, show=dict(show))

        line = self._repaint()

        self.assertFalse(cs.DEFAULT_SHOW["fable"])
        self.assertEqual(self.calls, [], "a default config fetched the caps")
        self.assertIn("Session", line)

    def test_failed_attempt_is_not_retried_on_the_next_repaint(self):
        """A timeout or a run Claude Code cancels leaves no fetched_at stamp.
        The attempt stamp must still hold the next try off, or a busy session
        would start the request on every repaint until one survived."""
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            raise OSError("timed out")

        cs.fetch_usage = fetch
        self._show()

        for _ in range(4):
            self._repaint()

        self.assertEqual(len(self.calls), 1, "retried inside _MODEL_CAPS_RETRY")
        cached = json.loads(self.cache.read_text())
        self.assertTrue(cached.get("model_caps_attempted_at"))
        self.assertFalse(cached.get("model_caps_fetched_at"))

        # Once the retry gap has passed, it tries again.
        cached["model_caps_attempted_at"] = time.time() - cs._MODEL_CAPS_RETRY - 1
        self.cache.write_text(json.dumps(cached))
        self._repaint()
        self.assertEqual(len(self.calls), 2)

    def test_refresh_interval_comes_from_config(self):
        def fetch(token, timeout=10):
            self.calls.append(timeout)
            return cs._normalize_usage(dict(FABLE_PAYLOAD))

        cs.fetch_usage = fetch
        self._show()
        # Widen the config the helper pinned with a 10-minute interval.
        pinned = cs.load_config
        cs.load_config = lambda: dict(pinned(), model_caps_refresh_seconds=600)

        self._repaint()
        cached = json.loads(self.cache.read_text())
        # 7 minutes old: stale at the default 300s, fresh at the configured 600s.
        cached["model_caps_fetched_at"] = time.time() - 420
        cached["model_caps_attempted_at"] = time.time() - 420
        self.cache.write_text(json.dumps(cached))
        self._repaint()
        self.assertEqual(len(self.calls), 1, "config interval was ignored")

        self.assertEqual(cs._model_caps_ttl({"model_caps_refresh_seconds": "5"}),
                         cs._MODEL_CAPS_MIN_TTL, "floor not applied")
        self.assertEqual(cs._model_caps_ttl({"model_caps_refresh_seconds": "x"}),
                         cs._MODEL_CAPS_TTL, "garbage must fall back")


if __name__ == "__main__":
    unittest.main(verbosity=2)
