"""Tests for status-line rendering: widths, hyperlinks, and row layout.

The status line writes raw ANSI to a terminal, so the width maths has to be
exactly right — a miscount either truncates visible text early or lets the line
spill into Claude Code's notification area.  Three things are pinned here:

* ``_visible_len`` / ``_skip_escape`` count only printable columns, including
  across OSC 8 hyperlinks (whose URLs contain letters that terminate a *CSI*
  sequence, so a colour-only scanner walks straight into the URL);
* ``_osc8`` refuses to linkify anything that could break out of the escape;
* ``_join_parts`` splits widgets across two rows per ``line1_widgets`` /
  ``line2_widgets``, and ``_fit_line`` fits each row against its own budget.

Run: ``python tests/test_rendering.py``
"""
import sys
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

URL = "https://github.com/owner/repo/pull/1234"


class VisibleLenTest(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(cs._visible_len("hello"), 5)

    def test_colour_codes_are_free(self):
        self.assertEqual(cs._visible_len(f"{cs.GREEN}abc{cs.RESET}"), 3)

    def test_hyperlink_url_is_not_counted(self):
        """Regression: the CSI terminator set contains 'h', so a naive scanner
        stopped at the 'h' of 'https' and counted the whole URL as visible."""
        link = cs._osc8(URL, "#1234")
        self.assertEqual(cs._visible_len(link), len("#1234"))

    def test_hyperlink_embedded_in_a_line(self):
        line = f"ab{cs._osc8(URL, '#1')}cd"
        self.assertEqual(cs._visible_len(line), len("ab#1cd"))

    def test_unterminated_escape_does_not_hang(self):
        # Consumes the remainder rather than looping forever.
        self.assertEqual(cs._visible_len("\033]8;;no-terminator"), 0)

    def test_empty(self):
        self.assertEqual(cs._visible_len(""), 0)


class Osc8Test(unittest.TestCase):
    def test_wraps_label(self):
        link = cs._osc8(URL, "#1234")
        self.assertTrue(link.startswith(cs.OSC8_START))
        self.assertTrue(link.endswith(cs.OSC8_END))
        self.assertIn("#1234", link)

    def test_no_url_returns_bare_label(self):
        self.assertEqual(cs._osc8("", "#1"), "#1")
        self.assertEqual(cs._osc8(None, "#1"), "#1")

    def test_control_characters_are_refused(self):
        """A URL containing BEL/ESC/newline/; could terminate the escape early
        and let arbitrary text reach the terminal as a control sequence."""
        for bad in ("http://x\a", "http://x\033[0m", "http://x\ny", "http://x;y"):
            self.assertEqual(cs._osc8(bad, "L"), "L", f"{bad!r} must not be linkified")


class JoinPartsTest(unittest.TestCase):
    def _parts(self):
        return [
            ((10, "session"), "SESSION"),
            ((60, "context"), "CONTEXT"),
            ((110, "model"), "MODEL"),
            ((170, "branch"), "BRANCH"),
        ]

    def test_single_row_by_default(self):
        out = cs._join_parts(self._parts(), {})
        self.assertNotIn("\n", out)
        self.assertEqual(out, "SESSION | CONTEXT | MODEL | BRANCH")

    def test_line2_widgets_demotes_named_ids(self):
        out = cs._join_parts(self._parts(), {"line2_widgets": ["model", "branch"]})
        self.assertEqual(out.split("\n"), ["SESSION | CONTEXT", "MODEL | BRANCH"])

    def test_line1_widgets_is_an_allowlist(self):
        out = cs._join_parts(self._parts(), {"line1_widgets": ["session"]})
        self.assertEqual(out.split("\n"), ["SESSION", "CONTEXT | MODEL | BRANCH"])

    def test_line1_wins_when_both_set(self):
        out = cs._join_parts(
            self._parts(),
            {"line1_widgets": ["session"], "line2_widgets": ["context"]},
        )
        self.assertEqual(out.split("\n")[0], "SESSION")

    def test_empty_row_is_dropped_not_blank(self):
        out = cs._join_parts(self._parts(), {"line2_widgets": ["nothing-matches"]})
        self.assertNotIn("\n", out)

    def test_all_demoted_yields_one_row(self):
        ids = ["session", "context", "model", "branch"]
        out = cs._join_parts(self._parts(), {"line2_widgets": ids})
        self.assertNotIn("\n", out)

    def test_malformed_config_falls_back_to_one_row(self):
        for cfg in ({"line2_widgets": "model"}, {"line1_widgets": 7}, None, "nope"):
            self.assertNotIn("\n", cs._join_parts(self._parts(), cfg))


class FitLineTest(unittest.TestCase):
    def test_each_row_fitted_independently(self):
        cfg = {"max_width": 100}
        two = "AAAA | BBBB\nCCCC | DDDD"
        out = cs._fit_line(two, cfg)
        self.assertEqual(len(out.split("\n")), 2)

    def test_single_row_unchanged_when_it_fits(self):
        self.assertNotIn("\n", cs._fit_line("short", {"max_width": 100}))

    def test_truncation_closes_an_open_hyperlink(self):
        """Cutting inside an OSC 8 link without closing it makes the terminal
        treat everything after as part of the link target."""
        long_label = "X" * 400
        line = cs._osc8(URL, long_label)
        out = cs._truncate_line(line, {"max_width": 20})
        self.assertTrue(out.endswith(cs.OSC8_END + cs.RESET) or cs.OSC8_END in out)


class RefreshIntervalTest(unittest.TestCase):
    def test_animation_gets_the_fast_tick(self):
        self.assertEqual(
            cs._desired_refresh_interval({"animate": "rainbow"}), cs.REFRESH_ANIMATED
        )

    def test_live_heartbeat_gets_the_slow_tick(self):
        """A timer is warranted only once the widget is actually on screen."""
        cfg = {"animate": "off", "show": {"heartbeat": True, "pomodoro": False}}
        with mock.patch.object(cs, "_read_hook_state", return_value={"tool_count": 1}),              mock.patch.object(cs, "_is_hook_state_fresh", return_value=True):
            self.assertEqual(cs._desired_refresh_interval(cfg), cs.REFRESH_TIMED)

    def test_running_focus_timer_gets_the_slow_tick(self):
        cfg = {"animate": "off", "show": {"heartbeat": False, "pomodoro": True}}
        with mock.patch.object(cs, "_read_pomodoro", return_value={"active": True}):
            self.assertEqual(cs._desired_refresh_interval(cfg), cs.REFRESH_TIMED)

    def test_enabled_but_idle_widgets_need_no_timer(self):
        """Regression: heartbeat and pomodoro are on by default but render
        nothing while idle. Asking for a timer on the setting alone relaunched
        Python every 15s — 240 times an hour — to redraw an unchanged bar."""
        cfg = {"animate": "off", "show": {"heartbeat": True, "pomodoro": True}}
        with mock.patch.object(cs, "_is_hook_state_fresh", return_value=False),              mock.patch.object(cs, "_read_pomodoro", return_value=None):
            self.assertIsNone(cs._desired_refresh_interval(cfg))

    def test_static_bar_needs_no_timer(self):
        cfg = {"animate": "off", "show": {"heartbeat": False, "pomodoro": False}}
        self.assertIsNone(cs._desired_refresh_interval(cfg))

    def test_garbage_config_is_safe(self):
        for cfg in (None, "x", 3, []):
            self.assertIsNone(cs._desired_refresh_interval(cfg))


class RateLimitBackoffTest(unittest.TestCase):
    def test_first_failure_waits_about_a_minute(self):
        delay, fails = cs._rate_limit_backoff(0)
        self.assertEqual(fails, 1)
        self.assertGreaterEqual(delay, cs._RATE_LIMIT_BACKOFF_BASE)
        self.assertLessEqual(delay, cs._RATE_LIMIT_BACKOFF_BASE + cs._RATE_LIMIT_JITTER)

    def test_backoff_doubles(self):
        d1, f1 = cs._rate_limit_backoff(0)
        d2, f2 = cs._rate_limit_backoff(f1)
        self.assertEqual(f2, 2)
        self.assertGreaterEqual(d2, 2 * cs._RATE_LIMIT_BACKOFF_BASE)

    def test_capped(self):
        delay, _ = cs._rate_limit_backoff(50)
        self.assertLessEqual(delay, cs._RATE_LIMIT_BACKOFF_MAX + cs._RATE_LIMIT_JITTER)

    def test_absurd_count_does_not_build_a_huge_int(self):
        delay, _ = cs._rate_limit_backoff(10 ** 9)
        self.assertLessEqual(delay, cs._RATE_LIMIT_BACKOFF_MAX + cs._RATE_LIMIT_JITTER)

    def test_garbage_count_treated_as_first_failure(self):
        for bad in (None, "x", -5, [], {}):
            delay, fails = cs._rate_limit_backoff(bad)
            self.assertEqual(fails, 1, f"{bad!r}")


class CorruptStateResilienceTest(unittest.TestCase):
    """Our own state files are still untrusted input.

    A truncated write, a disk error, or a hand-edit can leave *valid JSON of
    the wrong shape*. These paths run on every repaint, so raising blanks the
    user's status bar. All found by adversarial review.
    """

    def test_read_cache_survives_wrong_shaped_json(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            for junk in ("[1, 2]", '"a string"', "42", "null",
                         '{"timestamp": "bad"}', '{"timestamp": null}'):
                p.write_text(junk, encoding="utf-8")
                self.assertIsNone(cs.read_cache(p, 60), junk)

    def test_read_cache_treats_an_out_of_range_timestamp_as_a_miss(self):
        """An int too big for a float made float() raise OverflowError, which
        the ValueError handlers miss, so main() died before drawing anything.
        Infinity read as fresh, or as a backoff, forever."""
        import tempfile
        import time
        huge = "1" + "0" * 400
        hour_ago = repr(time.time() - 3600)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            for junk in ('{"timestamp": %s, "usage": {}}' % huge,
                         '{"timestamp": -%s, "usage": {}}' % huge,
                         '{"timestamp": Infinity, "usage": {}}',
                         '{"timestamp": NaN, "usage": {}}',
                         '{"timestamp": "1e999", "usage": {}}',
                         '{"rate_limited_until": %s, "timestamp": %s, "usage": {}}'
                         % (huge, hour_ago),
                         '{"rate_limited_until": Infinity, "timestamp": %s, "usage": {}}'
                         % hour_ago):
                with self.subTest(cache=junk[:60]):
                    p.write_text(junk, encoding="utf-8")
                    self.assertIsNone(cs.read_cache(p, 60))

    def test_write_cache_survives_non_dict_usage(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            for bad in ("not-a-dict", [1, 2], 7, True):
                cs.write_cache(p, "line", usage=bad)  # must not raise

    def test_sync_refresh_survives_wrong_shaped_settings(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            os.environ["CLAUDE_CONFIG_DIR"] = td
            try:
                for junk in ("[]", '"x"', "5", "null"):
                    (Path(td) / "settings.json").write_text(junk, encoding="utf-8")
                    self.assertIs(cs.sync_status_line_refresh({"animate": "off"}), False, junk)
            finally:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_cache_age_of_corrupt_entry_is_infinite(self):
        self.assertEqual(cs._cache_age({"timestamp": "bad"}), float("inf"))
        self.assertEqual(cs._cache_age([]), float("inf"))
        self.assertLess(cs._cache_age({"timestamp": __import__("time").time()}), 5)

    def test_cache_age_of_out_of_range_timestamp_is_infinite(self):
        """float() raises OverflowError, not ValueError, for an int beyond the
        float range; and an infinite timestamp gives an age of -inf, which
        reads as fresh forever. Both are as unusable as "bad"."""
        for ts in (10 ** 400, -10 ** 400, float("inf"), float("-inf"),
                   float("nan"), "1e999", "inf", "nan"):
            with self.subTest(timestamp=ts):
                self.assertEqual(cs._cache_age({"timestamp": ts}), float("inf"))

    def test_cache_readers_treat_an_unparseable_file_as_a_miss(self):
        """read_cache and _read_stale_cache run on every repaint but caught
        JSON syntax errors and OSError only. Not UTF-8 (UnicodeDecodeError),
        nested past the parser's depth (RecursionError), or an int past the
        int/str digit limit (a plain ValueError on Python 3.11+ and the
        3.8.14 / 3.9.14 / 3.10.7 security releases) raised instead."""
        import tempfile
        usage = b'"usage": {"five_hour": {"utilization": 1.0}}'
        try:
            int("9" * 5000)
            digit_limit = False  # an interpreter from before CVE-2020-10735
        except ValueError:
            digit_limit = True
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            for label, raw in (
                    ("not utf-8", b'{"line": "\xff\xfe", ' + usage + b"}"),
                    ("nested too deep", b"[" * 100000 + b"]" * 100000),
                    ("int past the digit limit",
                     b'{"timestamp": 1' + b"0" * 5000 + b", " + usage + b"}")):
                with self.subTest(cache=label):
                    p.write_bytes(raw)
                    self.assertIsNone(cs.read_cache(p, 60))
                    stale = cs._read_stale_cache(p)
                    if label == "int past the digit limit" and not digit_limit:
                        # It parses there; _cache_age then reads it as stale.
                        self.assertIsInstance(stale, dict)
                    else:
                        self.assertIsNone(stale)


class CsiGrammarTest(unittest.TestCase):
    """_skip_escape follows the ECMA-48 CSI grammar, not a hand-listed set.

    A long true-colour SGR exceeded the old 25-byte scan cap, so the tail was
    counted as visible width and the line truncated far too early.
    """

    def test_long_truecolour_sgr_is_consumed_whole(self):
        seq = "[38;2;255;128;64;48;2;0;0;0;1;4;7m"
        self.assertEqual(cs._visible_len(seq + "AB"), 2)

    def test_ordinary_sgr(self):
        self.assertEqual(cs._visible_len("[0m" + "abc"), 3)
        self.assertEqual(cs._visible_len("[1;31mX"), 1)

    def test_csi_with_intermediate_bytes(self):
        self.assertEqual(cs._visible_len("[?25lZ"), 1)

    def test_bare_escape_and_trailing_escape(self):
        self.assertEqual(cs._visible_len("cQ"), 1)
        self.assertEqual(cs._visible_len("ab"), 2)

    def test_unterminated_csi_does_not_overrun(self):
        self.assertEqual(cs._visible_len("[38;2;255"), 0)


class EffortFormatTest(unittest.TestCase):
    """Effort has three renderings; "med" alone reads as cryptic."""

    def test_short(self):
        self.assertEqual(cs._format_effort("medium", "short"), "med")
        self.assertEqual(cs._format_effort("xhigh", "short"), "xh")

    def test_full(self):
        self.assertEqual(cs._format_effort("medium", "full"), "Medium")
        self.assertEqual(cs._format_effort("xhigh", "full"), "XHigh")

    def test_labeled_is_the_default(self):
        self.assertEqual(cs.DEFAULT_EFFORT_FORMAT, "labeled")
        self.assertEqual(cs._format_effort("medium"), "Effort: Medium")
        self.assertEqual(cs._format_effort("max"), "Effort: Max")

    def test_every_level_renders_in_every_format(self):
        for level in ("low", "medium", "high", "xhigh", "max"):
            for fmt in cs.EFFORT_FORMATS:
                out = cs._format_effort(level, fmt)
                self.assertTrue(out, f"{level}/{fmt} rendered empty")

    def test_unknown_level_passes_through(self):
        """A new effort tier should still appear, just without a nicer name."""
        self.assertEqual(cs._format_effort("ultra", "short"), "ultra")
        self.assertEqual(cs._format_effort("ultra", "full"), "Ultra")

    def test_empty_level_renders_nothing(self):
        for fmt in cs.EFFORT_FORMATS:
            self.assertEqual(cs._format_effort("", fmt), "")
            self.assertEqual(cs._format_effort(None, fmt), "")

    def test_invalid_format_falls_back_to_default(self):
        self.assertEqual(cs._format_effort("medium", "bogus"),
                         cs._format_effort("medium", cs.DEFAULT_EFFORT_FORMAT))


class PrBadgeMarkerTest(unittest.TestCase):
    """GitLab merge requests are written !N; GitHub pull requests #N."""

    def _badge_line(self, stdin_ctx):
        import os
        import tempfile
        cfg = {"show": {"pr": True}}
        with tempfile.TemporaryDirectory() as td:
            # The state dir lives under LOCALAPPDATA / XDG_CACHE_HOME, not
            # CLAUDE_CONFIG_DIR — all three must point at the sandbox or the
            # test reads and writes the developer's real bar state.
            env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td,
                   "XDG_CACHE_HOME": td}
            with mock.patch.dict(os.environ, env):
                return cs.build_status_line({}, "", cfg, stdin_ctx)

    def test_github_pr_renders_hash(self):
        line = self._badge_line({"pr_number": 42})
        self.assertIn("#42", line)

    def test_gitlab_mr_renders_bang(self):
        line = self._badge_line({"pr_number": 42, "pr_kind": "mr"})
        self.assertIn("!42", line)
        self.assertNotIn("#42", line)


class RefreshSyncTransitionsTest(unittest.TestCase):
    """Every transition that changes whether a repaint timer is needed must
    re-evaluate `refreshInterval`; a missed one leaves a stale 15s timer
    armed (or a live countdown frozen) until some unrelated event syncs it."""

    def test_hook_refresh_resyncs_the_timer(self):
        import io
        import tempfile
        stdin = io.StringIO("")  # isatty() is False; empty JSON payload
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(cs, "sync_status_line_refresh") as sync, \
                    mock.patch.object(
                        cs, "_get_hook_state_path",
                        return_value=Path(td) / "hook_state.json"), \
                    mock.patch.object(sys, "stdin", stdin):
                cs.hook_refresh("Bash")
            self.assertTrue(sync.called)

    def test_hook_refresh_syncs_after_persisting_the_state(self):
        """The sync decides by re-reading the persisted hook state, so it
        must run after the write — syncing first judges a just-woken
        heartbeat by its stale on-disk timestamp and leaves the timer
        unarmed until some other repaint happens along."""
        import io
        import tempfile
        order = []
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                        cs, "sync_status_line_refresh",
                        side_effect=lambda *a, **k: order.append("sync")), \
                    mock.patch.object(
                        cs, "_atomic_json_write",
                        side_effect=lambda *a, **k: order.append("write")), \
                    mock.patch.object(
                        cs, "_get_hook_state_path",
                        return_value=Path(td) / "hook_state.json"), \
                    mock.patch.object(sys, "stdin", io.StringIO("")):
                cs.hook_refresh("Bash")
        self.assertEqual(order, ["write", "sync"])

    def test_cmd_pomodoro_resyncs_on_every_path(self):
        with mock.patch.object(cs, "sync_status_line_refresh") as sync, \
                mock.patch.object(cs, "_cmd_pomodoro_inner", return_value=None):
            cs.cmd_pomodoro("start")
        self.assertTrue(sync.called)

        with mock.patch.object(cs, "sync_status_line_refresh") as sync, \
                mock.patch.object(cs, "_cmd_pomodoro_inner",
                                  side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cs.cmd_pomodoro("start")
        self.assertTrue(sync.called)

    def test_pomodoro_expiry_drops_the_timer(self):
        """A focus timer that runs out on its own leaves the screen without
        any command running — the render path itself must drop the timer."""
        expired = {"active": True, "start": 1.0, "duration_minutes": 1}
        with mock.patch.object(cs, "sync_status_line_refresh") as sync, \
                mock.patch.object(cs, "_write_pomodoro") as write:
            out = cs._render_pomodoro(expired, cs.THEMES["default"])
        self.assertEqual(out, "")
        self.assertTrue(write.called)
        self.assertTrue(sync.called)

    def test_status_line_render_self_heals_the_timer(self):
        """The heartbeat ageing past its TTL is observed by nothing but the
        repaints themselves, so the render path re-syncs on every run."""
        import io
        import os
        import tempfile
        fake_stdout = mock.Mock()
        fake_stdout.buffer = io.BytesIO()
        tty_stdin = mock.Mock()
        tty_stdin.isatty.return_value = True
        passthrough = lambda line, *a, **k: line
        with tempfile.TemporaryDirectory() as td:
            # Sandbox the state dir too (LOCALAPPDATA / XDG_CACHE_HOME), so
            # the run cannot touch — or be satisfied by — real bar state
            # such as an expired pomodoro syncing from the render widget.
            env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td,
                   "XDG_CACHE_HOME": td}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(sys, "argv", ["claude_status.py"]), \
                    mock.patch.object(sys, "stdin", tty_stdin), \
                    mock.patch.object(sys, "stdout", fake_stdout), \
                    mock.patch.object(cs, "sync_status_line_refresh",
                                      return_value=False) as sync, \
                    mock.patch.object(cs, "get_credentials",
                                      return_value=(None, "")), \
                    mock.patch.object(cs, "append_update_indicator",
                                      side_effect=passthrough), \
                    mock.patch.object(cs, "append_claude_update_indicator",
                                      side_effect=passthrough):
                cs.main()
        # The self-heal call is the one that passes the loaded config as an
        # argument; widget-level calls take none. Asserting on the args pins
        # this to the top-of-render sync specifically.
        self.assertTrue(any(c.args for c in sync.call_args_list))


if __name__ == "__main__":
    unittest.main(verbosity=2)
