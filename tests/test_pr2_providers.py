"""Regression tests for the providers slice of the PR2 sweep (F10, F19).

F10  MiniMax: ``mmx quota show`` ran uncached on every repaint of an mxclaude
     window, and on Windows its ``timeout=3`` did not cap it, because the npm
     ``mmx.cmd`` shim leaves a grandchild process holding the stdout pipe and
     ``subprocess.run`` waits for end-of-file on that pipe after the kill.
F19  A second Claude account's provider row made its own usage-API call in the
     same repaint as the main fetch, outside the one-network-call-per-redraw
     rule, and with no wall-clock ceiling.

Run: ``python -m unittest tests.test_pr2_providers``
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import FakeMmx, ProviderSandbox, cs  # noqa: E402

MINIMAX_ENV = {
    "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    "ANTHROPIC_MODEL": "MiniMax-M3",
}
ANSI = re.compile(r"\x1b\[[0-9;]*m")

API_USAGE = {
    "five_hour": {"utilization": 34.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    "seven_day": {"utilization": 9.0, "resets_at": "2099-01-05T00:00:00+00:00"},
}
FABLE_PAYLOAD = {
    "limits": [{
        "kind": "weekly_scoped", "percent": 81,
        "resets_at": "2099-01-05T00:00:00+00:00",
        "scope": {"model": {"display_name": "Fable"}},
    }],
}


class _Killed(BaseException):
    """Stands in for Claude Code cancelling the run mid-call (no cleanup runs)."""


def _stdin_json():
    return json.dumps({
        "context_window": {"total_input_tokens": 10, "total_output_tokens": 10,
                           "used_percentage": 1.0, "context_window_size": 200_000},
        "cost": {"total_cost_usd": 1.0},
    })


class MiniMaxBase(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        p = mock.patch.dict(os.environ, MINIMAX_ENV)
        p.start()
        self.addCleanup(p.stop)
        self.mmx = FakeMmx()
        for patcher in (mock.patch.object(cs.shutil, "which", return_value="/bin/mmx"),
                        mock.patch.object(cs.subprocess, "run", self.mmx)):
            patcher.start()
            self.addCleanup(patcher.stop)


class MiniMaxStdinPathCacheTest(MiniMaxBase):
    """F10: the mxclaude stdin path must reuse the 120 s provider cache."""

    def test_mmx_runs_once_across_consecutive_repaints(self):
        results = [cs._parse_stdin_context(_stdin_json()) for _ in range(3)]
        self.assertEqual(
            len(self.mmx.calls), 1,
            "every repaint of an mxclaude window started a Node CLI and a network "
            "call to MiniMax; the 120 s cache the provider row keeps was ignored")
        for res in results:
            self.assertAlmostEqual(res["_rate_limits"]["five_hour"]["utilization"], 80.0)
            self.assertAlmostEqual(res["_rate_limits"]["seven_day"]["utilization"], 20.0)

    def test_a_cancelled_mmx_is_not_retried_by_the_next_repaint(self):
        self.mmx.exc = _Killed()
        with self.assertRaises(_Killed):
            cs._parse_stdin_context(_stdin_json())
        self.mmx.exc = None
        cs._parse_stdin_context(_stdin_json())
        self.assertEqual(
            len(self.mmx.calls), 1,
            "a run Claude Code killed mid-mmx left no trace, so the next repaint "
            "walked into the same stall")

    def test_no_run_when_the_attempt_cannot_be_recorded(self):
        with mock.patch.object(cs, "_write_provider_cache", return_value=False):
            res = cs._parse_stdin_context(_stdin_json())
        self.assertEqual(self.mmx.calls, [],
                         "an unrecorded attempt would repeat on every repaint")
        self.assertNotIn("_rate_limits", res)

    def test_quota_older_than_the_stale_limit_is_not_drawn_as_current(self):
        now = time.time()
        cs._write_provider_cache("minimax", {
            "timestamp": now - cs._PROVIDER_STALE_AFTER - 600,
            "last_attempt": now - 10,       # inside the TTL: no new run
            "usage": {"five_hour": {"utilization": 55.0,
                                    "resets_at": cs._iso_from_epoch(now + 3600)}}})
        res = cs._parse_stdin_context(_stdin_json())
        self.assertNotIn("_rate_limits", res)
        self.assertEqual(self.mmx.calls, [])

    def test_fresh_cached_quota_is_drawn_without_a_run(self):
        now = time.time()
        cs._write_provider_cache("minimax", {
            "timestamp": now - 60, "last_attempt": now - 60,
            "usage": {"five_hour": {"utilization": 42.0,
                                    "resets_at": cs._iso_from_epoch(now + 3600)}}})
        res = cs._parse_stdin_context(_stdin_json())
        self.assertEqual(self.mmx.calls, [])
        self.assertAlmostEqual(res["_rate_limits"]["five_hour"]["utilization"], 42.0)

    def test_stdin_rate_limits_still_win_and_skip_mmx(self):
        raw = json.dumps({
            "rate_limits": {"five_hour": {"used_percentage": 7, "resets_at": time.time() + 3600}},
            "context_window": {"total_input_tokens": 1, "total_output_tokens": 1},
        })
        res = cs._parse_stdin_context(raw)
        self.assertEqual(self.mmx.calls, [])
        self.assertAlmostEqual(res["_rate_limits"]["five_hour"]["utilization"], 7.0)

    def test_broken_state_dir_never_crashes_the_line(self):
        with mock.patch.object(cs, "get_state_dir", side_effect=RuntimeError("no home")):
            res = cs._parse_stdin_context(_stdin_json())      # must not raise
        self.assertIsInstance(res, dict)


class MiniMaxNoPipesTest(MiniMaxBase):
    """F10: mmx runs with no pipes, so the timeout is a real ceiling on Windows."""

    def _assert_pipe_free(self):
        self.assertEqual(len(self.mmx.calls), 1)
        _cmd, kw = self.mmx.calls[0]
        self.assertFalse(kw.get("capture_output"), "capture_output creates the pipes")
        self.assertIsNot(kw.get("stdout"), subprocess.PIPE)
        self.assertIsNot(kw.get("stderr"), subprocess.PIPE)
        self.assertTrue(hasattr(kw.get("stdout"), "write"),
                        "stdout must be a temp file, not a pipe")
        self.assertEqual(kw.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kw.get("stderr"), subprocess.DEVNULL)
        self.assertIsNotNone(kw.get("timeout"))

    def test_stdin_path_call_has_no_pipes(self):
        cs._parse_stdin_context(_stdin_json())
        self._assert_pipe_free()

    def test_provider_row_call_has_no_pipes(self):
        c = cs._read_minimax_quota("minimax")
        self._assert_pipe_free()
        self.assertAlmostEqual(c["usage"]["five_hour"]["utilization"], 80.0)

    def test_nonzero_exit_and_garbage_output_draw_nothing(self):
        self.mmx.rc = 1
        self.assertNotIn("usage", cs._read_minimax_quota("minimax"))
        self.sb.next_repaint()
        os.remove(cs._provider_cache_path("minimax"))
        self.mmx.rc = 0
        self.mmx.payload = "not a quota object"
        self.assertNotIn("usage", cs._read_minimax_quota("minimax"))


class RealMmxShimTest(unittest.TestCase):
    """F10: a real child process behind a real shim (an npm-style ``mmx.cmd``
    on Windows, a shell script elsewhere) still delivers its output when mmx
    runs with no pipes."""

    FAKE_MMX = (
        "import json, sys, time\n"
        "if sys.argv[1:] != ['quota', 'show', '--output', 'json', '--quiet']:\n"
        "    sys.exit(3)\n"
        "end = int((time.time() + 3600) * 1000)\n"
        "print(json.dumps({'model_remains': [{\n"
        "    'current_interval_status': 1,\n"
        "    'current_interval_remaining_percent': 20.0,\n"
        "    'current_weekly_remaining_percent': 80.0,\n"
        "    'end_time': end, 'weekly_end_time': end + 86400000}]}))\n"
    )

    def setUp(self):
        self.sb = ProviderSandbox(self)
        shim_dir = Path(tempfile.mkdtemp(prefix="mmx-shim-"))
        self.addCleanup(shutil.rmtree, str(shim_dir), True)
        script = shim_dir / "fake_mmx.py"
        script.write_text(self.FAKE_MMX, encoding="ascii")
        if sys.platform == "win32":
            with open(str(shim_dir / "mmx.cmd"), "w", encoding="ascii", newline="") as f:
                f.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script))
        else:
            shim = shim_dir / "mmx"
            with open(str(shim), "w", encoding="ascii", newline="") as f:
                f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
            shim.chmod(0o755)
        path = str(shim_dir) + os.pathsep + os.environ.get("PATH", "")
        p = mock.patch.dict(os.environ, {"PATH": path})
        p.start()
        self.addCleanup(p.stop)

    def test_output_reaches_the_row_through_the_temp_file(self):
        c = cs._read_minimax_quota("minimax")
        self.assertAlmostEqual(c["usage"]["five_hour"]["utilization"], 80.0)
        self.assertAlmostEqual(c["usage"]["seven_day"]["utilization"], 20.0)

    def test_output_reaches_the_main_bar_through_the_temp_file(self):
        with mock.patch.dict(os.environ, MINIMAX_ENV):
            res = cs._parse_stdin_context(_stdin_json())
        self.assertAlmostEqual(res["_rate_limits"]["five_hour"]["utilization"], 80.0)


@unittest.skipUnless(sys.platform == "win32", "the pipe wait after a kill is a Windows behaviour")
class MiniMaxWindowsHangTest(unittest.TestCase):
    """F10, reproduced for real: an npm-style ``mmx.cmd`` whose grandchild
    outlives the kill. With pipes, ``subprocess.run`` waits for that child."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        shim_dir = Path(tempfile.mkdtemp(prefix="mmx-shim-"))
        self.addCleanup(shutil.rmtree, str(shim_dir), True)
        (shim_dir / "mmx.cmd").write_text(
            '@echo off\r\n"%s" -c "import time; time.sleep(6)"\r\n' % sys.executable,
            encoding="ascii")
        env = {"PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", "")}
        env.update(MINIMAX_ENV)
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        # A short ceiling keeps the test quick; the shim runs for 6 s.
        p = mock.patch.object(cs, "_MINIMAX_TIMEOUT", 1, create=True)
        p.start()
        self.addCleanup(p.stop)

    def test_stdin_path_is_cut_off_at_the_timeout(self):
        began = time.monotonic()
        cs._parse_stdin_context(_stdin_json())
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 3.5, "a hung mmx held the repaint for %.1fs" % elapsed)

    def test_provider_row_is_cut_off_at_the_timeout(self):
        began = time.monotonic()
        cs._read_minimax_quota("minimax")
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 3.5, "a hung mmx held the repaint for %.1fs" % elapsed)


class MiniMaxTurnTakingTest(MiniMaxBase):
    """F19/F10: the MiniMax row takes turns with the other network checks."""

    def test_row_waits_for_a_later_repaint_after_another_check_ran(self):
        # The main usage fetch has just run in this repaint.
        self.assertTrue(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))
        c = cs._read_minimax_quota("minimax")
        self.assertEqual(self.mmx.calls, [], "two network calls in one repaint")
        self.assertNotIn("last_attempt", c, "waiting must not look like a failed attempt")
        self.sb.next_repaint()
        cs._read_minimax_quota("minimax")
        self.assertEqual(len(self.mmx.calls), 1)

    def test_stdin_path_never_waits_on_the_turn_gap(self):
        # On an mxclaude window mmx IS the bar, like the primary usage fetch.
        self.assertTrue(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))
        res = cs._parse_stdin_context(_stdin_json())
        self.assertEqual(len(self.mmx.calls), 1)
        self.assertIn("_rate_limits", res)


class ClaudeProviderBase(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.main = self.sb.claude_dir(".claude", token="main-token")
        self.other = self.sb.claude_dir(".claude-b", token="other-secret-token")
        self.entry = {"id": "claude:b", "type": "claude", "dir": "~/.claude-b", "enabled": True}
        self.config = {"providers": [dict(self.entry)], "providers_refresh_seconds": 300}
        for name in ("_refresh_oauth_token", "refresh_and_retry"):
            p = mock.patch.object(cs, name, side_effect=AssertionError(f"{name} called"))
            p.start()
            self.addCleanup(p.stop)


class ClaudeProviderTurnTakingTest(ClaudeProviderBase):
    """F19: the provider refresh is one more optional check, not a second fetch."""

    def test_refresh_is_skipped_in_a_repaint_that_already_fetched(self):
        # main() has just made its caps fetch (non-cancelled, returned).
        self.assertTrue(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))
        budget = [1]
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            cs._refresh_claude_provider(self.entry, self.config, budget=budget)
        fetch.assert_not_called()
        self.assertEqual(budget, [1], "waiting must not spend the repaint's budget")
        self.assertNotIn("last_attempt", cs._read_provider_cache("claude:b") or {},
                         "waiting is not failing: it must leave no attempt stamp")

    def test_refresh_runs_on_the_next_repaint(self):
        self.assertTrue(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            cs._refresh_claude_provider(self.entry, self.config)
            fetch.assert_not_called()
            self.sb.next_repaint()
            c = cs._refresh_claude_provider(self.entry, self.config)
        fetch.assert_called_once()
        self.assertEqual(c["usage"]["five_hour"]["utilization"], 34.0)

    def test_refresh_claims_the_turn_so_other_checks_wait(self):
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)):
            cs._refresh_claude_provider(self.entry, self.config)
        self.assertFalse(cs._begin_net_attempt("update_check"),
                         "a check right after the provider fetch must wait its turn")

    def test_killed_fetch_keeps_the_next_repaint_away(self):
        with mock.patch.object(cs, "fetch_usage", side_effect=_Killed()):
            with self.assertRaises(_Killed):
                cs._refresh_claude_provider(self.entry, self.config)
        self.sb.next_repaint()
        with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            cs._refresh_claude_provider(self.entry, self.config)
        fetch.assert_not_called()

    def test_unrecorded_attempt_does_not_fetch(self):
        with mock.patch.object(cs, "_write_provider_cache", return_value=False), \
                mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
            cs._refresh_claude_provider(self.entry, self.config)
        fetch.assert_not_called()


class ClaudeProviderCeilingTest(ClaudeProviderBase):
    """F19: the provider fetch has a hard wall-clock ceiling."""

    def test_hung_fetch_is_abandoned_at_the_budget(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hung(token, timeout=10):
            release.wait(8)
            return dict(API_USAGE)

        now = time.time()
        with mock.patch.object(cs, "_PROVIDER_FETCH_BUDGET", 0.3, create=True), \
                mock.patch.object(cs, "fetch_usage", hung):
            began = time.monotonic()
            c = cs._refresh_claude_provider(self.entry, self.config, now=now)
            elapsed = time.monotonic() - began
        self.assertLess(elapsed, 2.5, "the repaint waited %.1fs on a hung fetch" % elapsed)
        self.assertEqual(c.get("error_until"), now + cs._PROVIDER_ERROR_BACKOFF)
        self.assertNotIn("usage", c)

    def test_a_late_answer_after_the_ceiling_is_not_used(self):
        release = threading.Event()

        def slow(token, timeout=10):
            release.wait(8)
            return dict(API_USAGE)

        with mock.patch.object(cs, "_PROVIDER_FETCH_BUDGET", 0.2, create=True), \
                mock.patch.object(cs, "fetch_usage", slow):
            cs._refresh_claude_provider(self.entry, self.config)
            release.set()
            time.sleep(0.2)                  # let the abandoned thread finish
        stored = cs._read_provider_cache("claude:b") or {}
        self.assertNotIn("usage", stored)

    def test_default_budget_fits_a_two_second_refresh_interval(self):
        self.assertLessEqual(getattr(cs, "_PROVIDER_FETCH_BUDGET", 99), 1.5)

    def test_errors_from_the_fetch_still_reach_the_backoff_rules(self):
        class _H(dict):
            def get(self, k, d=None):
                return dict.get(self, k.lower(), d)

        err = urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", 429,
                                     "err", _H({"retry-after": "600"}), io.BytesIO(b""))
        now = time.time()
        with mock.patch.object(cs, "fetch_usage", side_effect=err):
            c = cs._refresh_claude_provider(self.entry, self.config, now=now)
        self.assertGreaterEqual(c["rate_limited_until"], now + 600)


class ClaudeProviderJitterTest(ClaudeProviderBase):
    """F19: the retry window is not the same 60 s for every account."""

    def test_retry_window_differs_between_accounts(self):
        now = time.time()
        allowed_at_60, allowed_at_75 = [], []
        for letter in "abcdefghijkl":
            name = ".claude-%s" % letter
            self.sb.claude_dir(name, token="tok-%s" % letter)
            entry = {"id": "claude:%s" % letter, "type": "claude",
                     "dir": "~/%s" % name, "enabled": True}
            for age, bucket in ((60.5, allowed_at_60), (75.5, allowed_at_75)):
                cs._write_provider_cache(entry["id"], {"timestamp": now - 3600,
                                                       "last_attempt": now - age})
                self.sb.next_repaint()
                with mock.patch.object(cs, "fetch_usage", return_value=dict(API_USAGE)) as fetch:
                    cs._refresh_claude_provider(entry, {"providers": [entry]}, now=now)
                bucket.append(fetch.called)
        self.assertEqual(allowed_at_75, [True] * 12, "the jitter must stay within ~15 s")
        self.assertIn(False, allowed_at_60, "every account retried on exactly the 60 s mark")
        self.assertIn(True, allowed_at_60, "the retry was pushed past 60 s for every account")


class MainRepaintTest(unittest.TestCase):
    """F19, end to end: one repaint makes one usage-API call, not two."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude", token="main-token")
        self.sb.claude_dir(".claude-b", token="other-secret-token")
        cfg_dir = self.sb.home / ".claude"
        user_config = self.sb.root / "config.json"
        base = dict(cs.load_config())
        show = dict(base.get("show", {}))
        show.update({"opus": False, "sonnet": False, "fable": True, "extra": False})
        base["show"] = show
        base["providers"] = [{"id": "claude:b", "type": "claude",
                              "dir": "~/.claude-b", "enabled": True}]
        base["providers_refresh_seconds"] = 300
        self.calls = []

        def fetch(token, timeout=10):
            self.calls.append((token, timeout))
            return cs._normalize_usage(json.loads(json.dumps(FABLE_PAYLOAD)))

        for target, value in (
                ("_claude_config_dir", lambda: cfg_dir),
                ("get_config_path", lambda: user_config),
                ("load_config", lambda: json.loads(json.dumps(base))),
                ("check_for_update", lambda: False),
                ("check_claude_code_update", lambda: False),
                ("get_credentials", lambda: ("main-token", "Max 20x")),
                ("fetch_usage", fetch)):
            patcher = mock.patch.object(cs, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        now = time.time()
        self.payload = json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": 15, "resets_at": now + 10800},
            "seven_day": {"used_percentage": 66, "resets_at": now + 604800},
        }})

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

    def test_second_account_row_does_not_fetch_in_the_same_repaint(self):
        self._repaint()
        self.assertEqual(
            [token for token, _ in self.calls], ["main-token"],
            "the provider row made a second usage-API call in the same repaint")
        self.sb.next_repaint()       # a later repaint: the row's turn
        self._repaint()
        self.assertEqual([token for token, _ in self.calls],
                         ["main-token", "other-secret-token"])

    def test_api_fallback_path_gives_the_row_a_later_repaint_too(self):
        # No rate_limits on stdin: main() fetches the bars itself (fallback path).
        self.payload = "{}"
        self._repaint()
        self.assertEqual(
            [token for token, _ in self.calls], ["main-token"],
            "the provider row made a second usage-API call in the same repaint")
        self.sb.next_repaint()
        self._repaint()
        self.assertIn("other-secret-token", [token for token, _ in self.calls])


if __name__ == "__main__":
    unittest.main(verbosity=2)
