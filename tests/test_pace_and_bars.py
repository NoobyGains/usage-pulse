"""Pace arrows (``--pace-style``), the per-section bar toggles (``session_bar``,
``weekly_bar``, ``context_bar``) and fixed bar sizes with ``--bar-size auto``
(3.6.0).

The first two started life in community forks; the third is the fix for bars
that shrank to two characters in a narrow Claude Code window.

Run: ``python tests/test_pace_and_bars.py``
"""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LAYOUTS = ("standard", "compact", "minimal", "percent-first")
FILL, EMPTY = cs.BAR_STYLES["classic"]
# Every widget off (the bars themselves stay on); each test switches on what it needs.
QUIET = {k: k.endswith("_bar") for k in cs.DEFAULT_SHOW}


def _iso(seconds_ahead):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_ahead)).isoformat()


def usage(session=27, weekly=73, session_left=3 * 3600, weekly_left=3 * 86400, **extra):
    """3 h of the 5 h window left (40 % elapsed), 3 of 7 days left (57 % elapsed)."""
    u = {"five_hour": {"utilization": session, "resets_at": _iso(session_left)},
         "seven_day": {"utilization": weekly, "resets_at": _iso(weekly_left)}}
    u.update(extra)
    return u


@contextmanager
def sandbox():
    """State and config under a temp dir; the width cache reset."""
    with tempfile.TemporaryDirectory() as td:
        env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td,
               "XDG_CONFIG_HOME": td, "HOME": td}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cs, "get_config_path", return_value=Path(td) / "config.json"), \
                mock.patch.object(cs, "_cached_terminal_width", None):
            yield Path(td)


def render(show, layout="standard", stdin_ctx=None, usage_=None, raw=False, **cfg_extra):
    cfg = {"show": dict(QUIET, **show), "layout": layout}
    cfg.update(cfg_extra)
    with sandbox():
        line = cs.build_status_line(usage_ if usage_ is not None else usage(), "", cfg,
                                    stdin_ctx if stdin_ctx is not None else {"context_pct": 35.0})
    return line if raw else ANSI.sub("", line)


def bars_in(text):
    return text.count(FILL) + text.count(EMPTY)


def run_main(*argv):
    out = []
    with mock.patch.object(cs, "utf8_print", out.append), \
            mock.patch.object(sys, "argv", ["claude_status.py", *argv]):
        cs.main()
    return "\n".join(out)


class FormatPaceTest(unittest.TestCase):
    def test_no_pace_no_text(self):
        self.assertEqual(cs._format_pace(50, None), "")
        self.assertEqual(cs._format_pace(50, None, "percent"), "")

    def test_percent_style(self):
        self.assertEqual(cs._format_pace(50, 43.4, "percent"), " (43%)")

    def test_arrow_bands(self):
        cases = [(80, 40, "⇈", cs.BRIGHT_RED), (52, 40, "↑", cs.YELLOW), (43, 40, "→", cs.DIM),
                 (30, 40, "↓", cs.GREEN), (10, 40, "⇊", cs.BRIGHT_GREEN)]
        for pct, pace, glyph, colour in cases:
            with self.subTest(pct=pct):
                self.assertEqual(cs._format_pace(pct, pace), f" {colour}{glyph}{cs.RESET}")

    def test_band_edges(self):
        # Five points either way is still on track; twenty is the double arrow.
        self.assertIn("→", cs._format_pace(45, 40))
        self.assertIn("→", cs._format_pace(35, 40))
        self.assertIn("↑", cs._format_pace(45.1, 40))
        self.assertIn("↓", cs._format_pace(34.9, 40))
        self.assertIn("↑", cs._format_pace(60, 40))
        self.assertIn("⇈", cs._format_pace(60.1, 40))
        self.assertIn("↓", cs._format_pace(20, 40))
        self.assertIn("⇊", cs._format_pace(19.9, 40))

    def test_unknown_style_falls_back_to_arrows(self):
        self.assertIn("↑", cs._format_pace(60, 40, "sparkles"))
        self.assertEqual(cs.DEFAULT_PACE_STYLE, "arrows")
        self.assertEqual(cs.PACE_STYLES, ("arrows", "percent"))


class PaceOnTheBarTest(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(cs.DEFAULT_SHOW["pace"])
        line = render({"session": True, "weekly": True})
        for glyph in "↑↓⇈⇊→(":
            self.assertNotIn(glyph, line)

    def test_arrows_in_every_layout(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                line = render({"session": True, "weekly": True, "pace": True}, layout,
                              usage_=usage(session=73, weekly=10))
                self.assertIn("73% ⇈", line)   # 73 % used, 40 % of the window gone
                self.assertIn("10% ⇊", line)   # 10 % used, 57 % of the week gone

    def test_arrow_is_coloured(self):
        raw = render({"session": True, "pace": True}, raw=True, usage_=usage(session=73))
        self.assertIn(f"{cs.BRIGHT_RED}⇈{cs.RESET}", raw)

    def test_percent_style_shows_the_elapsed_share(self):
        line = render({"session": True, "weekly": True, "pace": True}, pace_style="percent")
        self.assertIn("27% (40%)", line)
        self.assertIn("73% (57%)", line)

    def test_per_model_caps_get_arrows_too(self):
        line = render({"opus": True, "pace": True},
                      usage_=usage(seven_day_opus={"utilization": 80, "resets_at": _iso(3 * 86400)}))
        self.assertIn("Opus", line)
        self.assertIn("80% ⇈", line)

    def test_config_default_and_flag(self):
        with sandbox():
            self.assertEqual(cs.load_config().get("pace_style"), "arrows")
            out = run_main("--pace-style", "percent")
            self.assertIn("Pace style:", out)
            self.assertIn("--show pace", out, "reminds you the widget is off")
            self.assertEqual(cs.load_config()["pace_style"], "percent")
            out = run_main("--pace-style", "sparkles")
            self.assertIn("Usage: --pace-style <arrows|percent>", out)
            self.assertEqual(cs.load_config()["pace_style"], "percent", "a bad value changes nothing")
            self.assertIn("percent", run_main("--pace-style"))


class BarToggleTest(unittest.TestCase):
    def test_defaults_on(self):
        for key in ("session_bar", "weekly_bar", "context_bar"):
            self.assertTrue(cs.DEFAULT_SHOW[key], key)

    def test_session_without_its_bar(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                on = render({"session": True, "timer": True}, layout)
                off = render({"session": True, "timer": True, "session_bar": False}, layout)
                self.assertEqual(bars_in(on), 12)
                self.assertEqual(bars_in(off), 0, off)
                self.assertIn("27%", off)
                self.assertRegex(off, r"\d+h \d+m", "the timer stays")
                self.assertNotIn("  ", off, "no double space where the bar was")

    def test_weekly_without_its_bar(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                off = render({"weekly": True, "weekly_bar": False}, layout)
                self.assertEqual(bars_in(off), 0, off)
                self.assertIn("73%", off)
                self.assertNotIn("  ", off)

    def test_context_without_its_bar(self):
        for layout in LAYOUTS:
            with self.subTest(layout=layout):
                off = render({"context": True, "context_bar": False}, layout)
                self.assertEqual(bars_in(off), 0, off)
                self.assertIn("35%", off)
                self.assertNotIn("  ", off)
        warning = render({"context": True, "context_warning": True, "context_bar": False},
                         stdin_ctx={"context_pct": 85.0})
        self.assertEqual(bars_in(warning), 0, warning)
        self.assertIn("85%", warning)

    def test_empty_session_without_its_bar(self):
        line = render({"session": True, "session_bar": False}, usage_={})
        self.assertIn("Session 0%", line)
        self.assertEqual(bars_in(line), 0)

    def test_only_the_named_bar_goes(self):
        line = render({"session": True, "weekly": True, "context": True, "session_bar": False})
        self.assertEqual(bars_in(line), 24, line)   # weekly and context keep theirs
        self.assertIn("Session 27%", line)

    def test_show_and_hide_accept_them(self):
        with sandbox():
            out = []
            with mock.patch.object(cs, "utf8_print", out.append):
                cs.cmd_hide("weekly_bar,context_bar")
                cs.cmd_show("weekly_bar")
            cfg = cs.load_config()
            self.assertTrue(cfg["show"]["weekly_bar"])
            self.assertFalse(cfg["show"]["context_bar"])
            self.assertEqual(out[0], "Disabled: weekly_bar, context_bar")


class BarSizeTest(unittest.TestCase):
    SHOW = {"session": True, "weekly": True, "context": True}

    def _render(self, bar_size, columns, show=None):
        with mock.patch.object(cs, "_detect_terminal_width", return_value=columns):
            return render(show or self.SHOW, bar_size=bar_size, max_width=80)

    def test_setting(self):
        self.assertEqual(cs._bar_width_setting({"bar_size": "auto"}), (12, True))
        self.assertEqual(cs._bar_width_setting({"bar_size": "small"}), (4, False))
        self.assertEqual(cs._bar_width_setting({"bar_size": "bogus"}), (12, False))
        self.assertEqual(cs._bar_width_setting({}), (12, False))
        self.assertEqual(cs.BAR_SIZE_CHOICES,
                         ("small", "small-medium", "medium", "medium-large", "large", "auto"))
        self.assertEqual(cs.DEFAULT_BAR_SIZE, "large", "the default is a fixed size")

    def test_named_size_is_never_shrunk(self):
        for columns in (60, 100, 300):
            with self.subTest(columns=columns):
                self.assertEqual(bars_in(self._render("large", columns)), 36)
        self.assertEqual(bars_in(self._render("small", 60)), 12)

    def test_auto_shrinks_to_fit(self):
        # Only the three bar sections are on, so the labels and numbers around
        # the bars are 41 columns. The line is measured, not estimated (the old
        # flat guess of 18 columns a bar plus 15 made this line shrink to the
        # floor at 100 columns although it fits at full size).
        self.assertEqual(bars_in(self._render("auto", 60)), 6)     # budget 48: (48 - 41) // 3 = 2, the floor
        self.assertEqual(bars_in(self._render("auto", 90)), 30)    # budget 72: (72 - 41) // 3 = 10 each
        self.assertEqual(bars_in(self._render("auto", 100)), 36)   # budget 80: room for large already
        self.assertEqual(bars_in(self._render("auto", 300)), 36)
        for columns in (60, 70, 90, 100):
            with self.subTest(columns=columns):
                self.assertLessEqual(len(self._render("auto", columns)), columns * 80 // 100)

    def test_hidden_bars_free_the_budget_in_auto(self):
        line = self._render("auto", 100, dict(self.SHOW, weekly_bar=False, context_bar=False))
        self.assertEqual(bars_in(line), 12)

    def test_provider_rows_follow_the_setting(self):
        # No main line has been measured in this run (an earlier test in the
        # same process may have left one): this pins the standalone estimate.
        with mock.patch.object(cs, "_detect_terminal_width", return_value=100), \
                mock.patch.object(cs, "_SHRINK_MEMO", None):
            self.assertEqual(cs._provider_bar_width({"bar_size": "large", "max_width": 80}, 15), 12)
            self.assertEqual(cs._provider_bar_width({"bar_size": "small", "max_width": 80}, 15), 4)
            self.assertLess(cs._provider_bar_width({"bar_size": "auto", "max_width": 80}, 15), 12)

    def test_cli_accepts_auto_and_rejects_nonsense(self):
        with sandbox():
            out = run_main("--bar-size", "auto")
            self.assertIn("shrinking to fit", out)
            self.assertEqual(cs.load_config()["bar_size"], "auto")
            out = run_main("--bar-size", "huge")
            self.assertIn("Unknown size: huge", out)
            self.assertIn("auto", out)
            self.assertEqual(cs.load_config()["bar_size"], "auto")
            self.assertIn("auto", run_main("--bar-size"))

    def test_config_summary_names_the_mode(self):
        with sandbox():
            run_main("--bar-size", "auto")
            with mock.patch.object(cs, "get_local_commit", return_value=None), \
                    mock.patch.object(cs, "_CLAUDE_PATH", None):
                self.assertIn("shrinks to fit", run_main("--config"))


class WidthDetectionTest(unittest.TestCase):
    def test_columns_from_claude_code_wins_when_piped(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "104"}), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(cs._detect_terminal_width_uncached(), 104)

    def test_no_columns_does_not_raise(self):
        env = {k: v for k, v in os.environ.items() if k != "COLUMNS"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            result = cs._detect_terminal_width_uncached()
        self.assertTrue(result is None or isinstance(result, int), result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
