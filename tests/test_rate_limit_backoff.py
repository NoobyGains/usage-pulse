"""A 429 must extend the backoff without making stale usage look fresh.

The 429 fallback re-writes the usage it just read from the stale cache so the
backoff state persists. ``write_cache`` used to stamp that re-write with the
current time, so every failed retry reset the data's apparent age — the
staleness warning never fired and ten-minute-old quota bars rendered as live.
The re-write must carry the *original* data timestamp forward and only refresh
the backoff deadline and failure count.

Run: ``python tests/test_rate_limit_backoff.py``
"""
import io
import json
import os
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

USAGE = {"five_hour": {"utilization": 42.0, "resets_at": "2026-08-01T00:00:00Z"}}


class WriteCachePreservesDataTimestampTest(unittest.TestCase):
    def test_explicit_data_timestamp_is_kept(self):
        old = time.time() - 600
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            cs.write_cache(path, "line", USAGE, "max",
                           rate_limited_until=time.time() + 60,
                           rate_limit_fails=2, data_timestamp=old)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["timestamp"], old)
            self.assertEqual(data["rate_limit_fails"], 2)

    def test_default_is_still_the_current_time(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            before = time.time()
            cs.write_cache(path, "line", USAGE, "max")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(data["timestamp"], before)


class RateLimitedRefreshKeepsUsageAgeTest(unittest.TestCase):
    """Drive main() into the 429 fallback and check what it writes back."""

    def _run_main_into_429(self, cache_path):
        def raise_429(_token):
            raise urllib.error.HTTPError(
                "https://api.anthropic.com", 429, "Too Many Requests", {},
                io.BytesIO(b""),
            )

        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        rendered = {}

        def fake_build(usage, plan, config=None, stdin_ctx=None, cache_age=None):
            rendered["cache_age"] = cache_age
            return "line"

        # CLAUDE_CONFIG_DIR: main() syncs statusLine.refreshInterval in Claude
        # Code's settings.json — with load_config mocked empty, a run against
        # the developer's real config dir strips it from their live settings.
        with tempfile.TemporaryDirectory() as state_td, \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": state_td}), \
                mock.patch.object(sys, "argv", ["claude_status.py"]), \
                mock.patch.object(sys, "stdin", fake_stdin), \
                mock.patch.object(cs, "get_state_dir", lambda: Path(state_td)), \
                mock.patch.object(cs, "get_cache_path", lambda: cache_path), \
                mock.patch.object(cs, "load_config", lambda: {}), \
                mock.patch.object(cs, "_cleanup_hooks", lambda: None), \
                mock.patch.object(cs, "get_credentials", lambda: ("tok", "max")), \
                mock.patch.object(cs, "fetch_usage", raise_429), \
                mock.patch.object(cs, "build_status_line", fake_build):
            cs.main()
        return rendered

    def test_429_rewrite_preserves_the_original_data_timestamp(self):
        old = time.time() - 600
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            cache_path.write_text(
                json.dumps({"timestamp": old, "line": "old", "usage": USAGE,
                            "plan": "max"}),
                encoding="utf-8",
            )

            rendered = self._run_main_into_429(cache_path)

            data = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(
                data["timestamp"], old,
                "the 429 re-write must not re-stamp ten-minute-old usage as fresh",
            )
            self.assertGreater(data["rate_limited_until"], time.time() + 30)
            self.assertEqual(data["rate_limit_fails"], 1)
            # The render during the 429 itself must also see the true age.
            self.assertAlmostEqual(rendered["cache_age"], 600, delta=30)


class BackoffScheduleTest(unittest.TestCase):
    def test_unusable_fail_count_restarts_the_schedule(self):
        """cache.json is untrusted input, and json reads Infinity as a float.
        int() of an infinite count raises OverflowError, which the except did
        not name, so the 429 handlers crashed before they wrote the backoff:
        a blank bar, and the next repaint called the API again."""
        base = cs._RATE_LIMIT_BACKOFF_BASE
        for bad in (float("inf"), float("-inf"), float("nan"), "soon", [1], None):
            with self.subTest(fail_count=repr(bad)):
                delay, fails = cs._rate_limit_backoff(bad)
                self.assertEqual(fails, 1)
                self.assertTrue(base <= delay <= base + cs._RATE_LIMIT_JITTER, delay)

    def test_huge_finite_fail_count_waits_the_longest(self):
        delay, fails = cs._rate_limit_backoff(10 ** 400)
        self.assertEqual(fails, 10 ** 400 + 1)
        self.assertTrue(cs._RATE_LIMIT_BACKOFF_MAX <= delay
                        <= cs._RATE_LIMIT_BACKOFF_MAX + cs._RATE_LIMIT_JITTER, delay)


if __name__ == "__main__":
    unittest.main(verbosity=2)
