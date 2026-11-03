"""Width fitting: truncation, wrapping, bar shrinking, cell widths, rainbow.

Pins the layout fixes from the PR 2 sweep (F02, F05, F26, F27, F28, F34, F35):

* a line that is too wide loses whole widgets from the tail (lowest priority
  first), never a slice of a bar or word, and ends in a visible "…";
* the staleness label and the update badges are reserved width that survives;
* ``--wrap auto`` packs as many rows as needed and only marks the cut on the
  last allowed row, with or without a rainbow colour theme;
* ``--bar-size auto`` measures the real line instead of guessing;
* widths count terminal cells (wide CJK and emoji are 2, combining marks 0);
* ``rainbow_colorize`` steps over OSC 8 hyperlinks instead of mangling them.

Run: ``python tests/test_pr2_layout.py``
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

ELLIPSIS = "…"
URL = "https://github.com/owner/repo/pull/59"
FILL, EMPTY = cs.BAR_STYLES["classic"]


def plain(text):
    return cs._strip_ansi(text)


def cells(text):
    """Independent oracle for terminal columns: 0 for marks, 2 for wide, else 1."""
    total = 0
    for ch in plain(text):
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def _iso(seconds_ahead):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_ahead)).isoformat()


def usage_dict():
    return {"five_hour": {"utilization": 37, "resets_at": _iso(2 * 3600 - 90)},
            "seven_day": {"utilization": 64, "resets_at": _iso(3 * 86400 + 7200)}}


STDIN = {"context_pct": 42.0, "cost_usd": 12.34, "lines_added": 1234,
         "lines_removed": 567, "model_name": "Opus 5.5", "effort": "xhigh",
         "session_id": "s"}


@contextlib.contextmanager
def sandbox():
    """State under a temp dir; no git; the width cache reset."""
    with tempfile.TemporaryDirectory() as td:
        env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td,
               "XDG_CONFIG_HOME": td, "HOME": td, "USERPROFILE": td}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cs, "get_state_dir", lambda: Path(td)), \
                mock.patch.object(cs, "_get_git_branch",
                                  return_value="fix/network-trap-3.6.2"), \
                mock.patch.object(cs, "_cached_terminal_width", None):
            yield Path(td)


def stock_line(cache_age=None, columns=None, stdin=STDIN, **cfg):
    """The default widgets, as build_status_line draws them (not yet fitted)."""
    config = dict(cfg)
    with sandbox():
        ctx = mock.patch.object(cs, "_detect_terminal_width", return_value=columns)
        with ctx:
            return cs.build_status_line(usage_dict(), "", config, dict(stdin),
                                        cache_age=cache_age)


def fit(line, columns, suffix="", **cfg):
    with mock.patch.object(cs, "_detect_terminal_width", return_value=columns):
        if suffix:
            return cs._fit_line(line, dict(cfg), suffix=suffix)
        return cs._fit_line(line, dict(cfg))


def budget_for(columns, pct=cs.DEFAULT_MAX_WIDTH_PCT):
    return columns * pct // 100


class TruncateAtWidgetsTest(unittest.TestCase):
    """F02: whole widgets go, never a slice of a bar or word, and a marker shows."""

    def test_stock_line_is_cut_at_a_widget_boundary_with_a_marker(self):
        line = stock_line()
        full = plain(line)
        self.assertGreater(cs._visible_len(line), budget_for(179))
        for columns in (100, 120, 150, 179):
            with self.subTest(columns=columns):
                out = fit(line, columns)
                text = plain(out)
                self.assertLessEqual(cs._visible_len(out), budget_for(columns))
                self.assertTrue(text.endswith(ELLIPSIS), text)
                body = text[:-len(ELLIPSIS)].rstrip()
                self.assertTrue(full.startswith(body), (body, full))
                self.assertTrue(full[len(body):].startswith(" | "),
                                f"cut inside a widget: {text!r}")

    def test_a_line_that_fits_is_untouched(self):
        line = stock_line()
        out = fit(line, 400)
        self.assertEqual(out, line)
        self.assertNotIn(ELLIPSIS, out)

    def test_exact_fit_has_no_marker_and_one_over_has(self):
        text = "aaaa | bbbb | cccc"                    # 18 wide
        exact = fit(text, 18, max_width=100)
        self.assertEqual(plain(exact), text)
        over = fit(text, 17, max_width=100)
        self.assertTrue(plain(over).endswith(ELLIPSIS))
        self.assertLessEqual(cs._visible_len(over), 17)

    def test_one_oversized_widget_is_cut_inside_the_budget_with_a_marker(self):
        with mock.patch.object(cs, "_detect_terminal_width", return_value=25):
            out = cs._truncate_line("x" * 100, {"max_width": 80})
        self.assertEqual(plain(out), "x" * 19 + ELLIPSIS)
        self.assertEqual(cs._visible_len(out), 20)

    def test_a_cut_through_a_hyperlink_still_closes_it(self):
        line = cs._osc8(URL, "X" * 200)
        with mock.patch.object(cs, "_detect_terminal_width", return_value=40):
            out = cs._truncate_line(line, {"max_width": 50})
        self.assertIn(cs.OSC8_END, out)
        self.assertTrue(plain(out).endswith(ELLIPSIS))
        self.assertLessEqual(cs._visible_len(out), 20)

    def test_unknown_width_leaves_the_line_alone(self):
        line = stock_line()
        self.assertEqual(fit(line, None), line)

    def test_colour_state_is_reset_before_the_marker(self):
        line = stock_line()
        out = fit(line, 120)
        head, _, tail = out.rpartition(ELLIPSIS)
        self.assertTrue(head.endswith(cs.DIM) or cs.RESET in head[-20:], repr(head[-20:]))


class StalenessAndBadgesTest(unittest.TestCase):
    """F05: the stale label and the update arrows are cut last, not first."""

    def test_the_stale_label_sits_with_the_first_widget(self):
        for age, label in ((700, "(10m+ ago)"), (400, "(6m ago)"), (150, "(2m ago)")):
            with self.subTest(age=age):
                first = plain(stock_line(cache_age=age)).split(" | ")[0]
                self.assertTrue(first.endswith(label), first)

    def test_a_fresh_line_has_no_stale_label(self):
        self.assertNotIn("ago)", plain(stock_line(cache_age=0)))

    def test_stale_label_survives_the_default_cut(self):
        out = fit(stock_line(cache_age=700), 120)
        self.assertIn("(10m+ ago)", plain(out))

    def test_suffix_is_reserved_width_and_survives(self):
        badges = f" {cs.BRIGHT_YELLOW}↑ Pulse Update{cs.RESET}"
        for columns in (100, 120, 179, 220):
            with self.subTest(columns=columns):
                out = fit(stock_line(cache_age=0), columns, suffix=badges)
                self.assertTrue(plain(out).endswith("↑ Pulse Update"), plain(out))
                self.assertLessEqual(cs._visible_len(out), budget_for(columns))

    def test_suffix_follows_the_marker(self):
        badges = f" {cs.BRIGHT_YELLOW}↑ Pulse Update{cs.RESET}"
        out = plain(fit(stock_line(cache_age=0), 120, suffix=badges))
        self.assertIn(ELLIPSIS + " ↑ Pulse Update", out)

    def test_suffix_alone_wider_than_the_budget_is_still_clipped_in_budget(self):
        out = fit("hello", 12, suffix=" ↑ Pulse Update", max_width=100)
        self.assertLessEqual(cs._visible_len(out), 12)

    def test_suffix_goes_on_the_last_row_only(self):
        out = fit("AAAA | BBBB\nCCCC | DDDD", 60, suffix=" ^^", max_width=100)
        rows = out.split("\n")
        self.assertEqual(len(rows), 2)
        self.assertFalse(plain(rows[0]).endswith("^^"))
        self.assertTrue(plain(rows[1]).endswith("^^"))

    def _run_main(self, columns, config, cache_age):
        """One repaint through main() on the cached-data path, both update
        checks reporting an update, the real renderer, no network or git."""
        usage = usage_dict()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry = {"timestamp": time.time() - cache_age, "usage": usage,
                     "plan": "max", "rate_limited_until": time.time() + 300}
            (root / "cache.json").write_text(json.dumps(entry), encoding="utf-8")
            fake_stdin = mock.Mock()
            fake_stdin.isatty.return_value = True
            fake_stdout = mock.Mock()
            fake_stdout.buffer = io.BytesIO()

            def no_network(*args, **kwargs):
                raise OSError("network disabled in this test")

            def no_git(*args, **kwargs):
                raise FileNotFoundError("git is not installed in this test")

            env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td,
                   "XDG_CONFIG_HOME": td, "HOME": td, "USERPROFILE": td}
            with contextlib.ExitStack() as stack:
                for patch in (
                        mock.patch.dict(os.environ, env),
                        mock.patch.object(sys, "argv", ["claude_status.py"]),
                        mock.patch.object(sys, "stdin", fake_stdin),
                        mock.patch.object(sys, "stdout", fake_stdout),
                        mock.patch.object(cs, "get_state_dir", lambda: root),
                        mock.patch.object(cs, "get_cache_path", lambda: root / "cache.json"),
                        mock.patch.object(cs, "load_config", lambda: dict(config)),
                        mock.patch.object(cs, "_cleanup_hooks", lambda: None),
                        mock.patch.object(cs, "get_credentials", lambda: ("tok", "max")),
                        mock.patch.object(cs, "fetch_usage", no_network),
                        mock.patch.dict(getattr(cs, "_own_stamps", {}), {}, clear=True),
                        mock.patch.object(cs, "check_for_update", lambda: True),
                        mock.patch.object(cs, "check_claude_code_update",
                                          lambda v=None: True),
                        mock.patch.object(cs.socket, "getaddrinfo", no_network),
                        mock.patch.object(cs.socket, "create_connection", no_network),
                        mock.patch.object(cs.urllib.request, "urlopen", no_network),
                        mock.patch.object(cs.subprocess, "run", no_git),
                        mock.patch.object(cs, "_cached_terminal_width", None),
                        mock.patch.object(cs, "_detect_terminal_width",
                                          lambda: columns)):
                    stack.enter_context(patch)
                cs.main()
            return fake_stdout.buffer.getvalue().decode("utf-8")

    def _finish(self, line, providers, columns=100, config=None):
        add = (lambda text, cfg: text + "\n" + providers) if providers else \
            (lambda text, cfg: text)
        with mock.patch.object(cs, "_detect_terminal_width", return_value=columns), \
                mock.patch.object(cs, "check_for_update", lambda: True), \
                mock.patch.object(cs, "check_claude_code_update", lambda v=None: True), \
                mock.patch.object(cs, "_append_provider_rows", add):
            return cs._finish_line(line, config if config is not None else {"max_width": 100})

    def test_finish_line_puts_the_badges_on_the_last_main_row_not_a_provider_row(self):
        out = self._finish("AAAA | BBBB", "PROVIDER | row").split("\n")
        self.assertEqual(len(out), 2)
        self.assertTrue(plain(out[0]).endswith("↑ Claude Update"))
        self.assertEqual(plain(out[1]), "PROVIDER | row")

    def test_finish_line_with_a_two_row_main_line_badges_the_second_row(self):
        out = self._finish("AAAA | BBBB\nCCCC", "PROVIDER | row").split("\n")
        self.assertEqual(len(out), 3)
        self.assertEqual(plain(out[0]), "AAAA | BBBB")
        self.assertTrue(plain(out[1]).startswith("CCCC"))
        self.assertIn("↑ Pulse Update", plain(out[1]))
        self.assertEqual(plain(out[2]), "PROVIDER | row")

    def test_finish_line_fits_provider_rows_on_their_own(self):
        out = self._finish("AAAA", "P" * 80 + " | tail", columns=40).split("\n")
        self.assertLessEqual(cs._visible_len(out[0]), 40)
        self.assertLessEqual(cs._visible_len(out[1]), 40)
        self.assertTrue(plain(out[1]).endswith(ELLIPSIS))

    def test_finish_line_without_any_update_changes_nothing(self):
        with mock.patch.object(cs, "check_for_update", lambda: False), \
                mock.patch.object(cs, "check_claude_code_update", lambda v=None: False), \
                mock.patch.object(cs, "_detect_terminal_width", return_value=100):
            self.assertEqual(cs._finish_line("AAAA | BBBB", {"max_width": 100}),
                             "AAAA | BBBB")

    def test_main_keeps_stale_label_and_both_badges_when_the_line_is_too_wide(self):
        for columns in (100, 110, 120):
            with self.subTest(columns=columns):
                out = self._run_main(columns, {}, cache_age=700)
                text = plain(out)
                self.assertIn("(10m+ ago)", text)
                self.assertIn("↑ Pulse Update", text)
                self.assertIn("↑ Claude Update", text)
                for row in out.split("\n"):
                    self.assertLessEqual(cs._visible_len(row), budget_for(columns))

    def test_main_keeps_the_badges_with_wrap_auto_too(self):
        out = self._run_main(100, {"wrap": "auto"}, cache_age=700)
        text = plain(out)
        self.assertIn("(10m+ ago)", text)
        self.assertIn("↑ Pulse Update", text)
        self.assertIn("↑ Claude Update", text)

    def test_main_without_a_width_prints_everything(self):
        out = self._run_main(None, {}, cache_age=700)
        text = plain(out)
        self.assertIn("(10m+ ago)", text)
        self.assertIn("↑ Pulse Update", text)
        self.assertNotIn(ELLIPSIS, text)


class EveryWidthTest(unittest.TestCase):
    """No terminal width, theme, wrap mode or badge may overflow a row."""

    def test_no_row_ever_exceeds_the_budget(self):
        badges = (f" {cs.BRIGHT_YELLOW}↑ Pulse Update{cs.RESET}"
                  f" {cs.BRIGHT_YELLOW}↑ Claude Update{cs.RESET}")
        cjk = ("⚡fast | コードレビュー | "
               "\U0001f525 12 | é | ❤️ ok | " + "w" * 30)
        lines = {"default": stock_line(cache_age=700),
                 "rainbow": stock_line(theme="rainbow", cache_age=400),
                 "cjk": cjk}
        for name, line in lines.items():
            for wrap in ("off", "auto"):
                for suffix in ("", badges):
                    for columns in range(1, 260, 3):
                        budget = budget_for(columns)
                        with self.subTest(line=name, wrap=wrap, columns=columns,
                                          badges=bool(suffix)):
                            out = fit(line, columns, suffix=suffix, wrap=wrap)
                            rows = out.split("\n")
                            self.assertLessEqual(len(rows), cs.WRAP_MAX_ROWS)
                            for row in rows:
                                self.assertLessEqual(cells(row), budget)
                            # a cut is always marked, a fit never is
                            full = cells(line) + (cells(suffix) if suffix else 0)
                            if full <= budget:
                                self.assertNotIn(ELLIPSIS, plain(out))
                                self.assertEqual(out.count("\n"), 0)
                            elif budget >= 4:
                                self.assertTrue(
                                    ELLIPSIS in plain(out) or wrap == "auto",
                                    plain(out))

    def test_garbage_in_never_raises(self):
        for line in ("", " | ", "|", " | | ", "\033", "\033]8;;", "a\033[", "\n", "\n\n"):
            for columns in (None, 1, 5, 80):
                for wrap in ("off", "auto"):
                    with self.subTest(line=line, columns=columns, wrap=wrap):
                        out = fit(line, columns, suffix=" ^", wrap=wrap)
                        self.assertIsInstance(out, str)


class WrapTest(unittest.TestCase):
    """F26 and F28: --wrap auto packs rows, also for rainbow colouring."""

    def _widgets(self, line):
        return plain(line).split(" | ")

    def test_stock_line_wraps_onto_as_many_rows_as_it_needs(self):
        line = stock_line()
        out = fit(line, 80, wrap="auto")
        rows = out.split("\n")
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertLessEqual(cs._visible_len(row), budget_for(80))
        self.assertNotIn(ELLIPSIS, plain(out))
        rejoined = " | ".join(plain(r) for r in rows)
        self.assertEqual(rejoined, plain(line), "a widget was lost or split")

    def test_a_line_that_fits_is_not_wrapped(self):
        line = stock_line()
        self.assertEqual(fit(line, 400, wrap="auto"), line)

    def test_two_rows_when_two_are_enough(self):
        out = fit(stock_line(), 130, wrap="auto")
        self.assertEqual(len(out.split("\n")), 2)
        self.assertNotIn(ELLIPSIS, plain(out))

    def test_rows_beyond_the_cap_end_in_a_marker_not_a_slice(self):
        line = stock_line()
        out = fit(line, 50, wrap="auto")           # budget 40: needs more than the cap
        rows = out.split("\n")
        self.assertEqual(len(rows), cs.WRAP_MAX_ROWS)
        for row in rows:
            self.assertLessEqual(cs._visible_len(row), budget_for(50))
        self.assertTrue(plain(rows[-1]).endswith(ELLIPSIS), plain(rows[-1]))
        for row in rows[:-1]:
            self.assertNotIn(ELLIPSIS, plain(row))
        full = self._widgets(line)
        for row in rows:
            for widget in plain(row).replace(ELLIPSIS, "").split(" | "):
                widget = widget.strip()
                if widget:
                    self.assertTrue(any(w == widget for w in full), widget)

    def test_badges_stay_on_the_last_row(self):
        badges = f" {cs.BRIGHT_YELLOW}↑ Pulse Update{cs.RESET}"
        out = fit(stock_line(), 80, suffix=badges, wrap="auto")
        rows = out.split("\n")
        self.assertTrue(plain(rows[-1]).endswith("↑ Pulse Update"))
        for row in rows:
            self.assertLessEqual(cs._visible_len(row), budget_for(80))

    def test_a_single_widget_wider_than_a_row_is_cut_with_a_marker(self):
        out = fit("a | " + "w" * 90 + " | b", 40, wrap="auto")
        for row in out.split("\n"):
            self.assertLessEqual(cs._visible_len(row), budget_for(40))
        self.assertIn(ELLIPSIS, plain(out))

    def test_rainbow_theme_wraps(self):
        self._assert_rainbow_wraps(theme="rainbow")

    def test_rainbow_animation_wraps(self):
        self._assert_rainbow_wraps(animate="rainbow")

    def _assert_rainbow_wraps(self, **cfg):
        default = stock_line()
        line = stock_line(**cfg)
        self.assertIn("38;2;", line)
        want = fit(default, 120, wrap="auto").count("\n") + 1
        out = fit(line, 120, wrap="auto")
        rows = out.split("\n")
        self.assertEqual(len(rows), want, "rainbow wrapped differently from the default theme")
        self.assertGreaterEqual(len(rows), 2)
        for row in rows:
            self.assertLessEqual(cs._visible_len(row), budget_for(120))
        self.assertNotIn(ELLIPSIS, plain(out))
        self.assertEqual(" | ".join(plain(r) for r in rows), plain(line))
        self.assertIn("38;2;", rows[-1], "the second row lost its colour")

    def test_rainbow_truncation_cuts_at_a_widget_too(self):
        line = stock_line(theme="rainbow")
        out = fit(line, 120)
        text = plain(out)
        self.assertTrue(text.endswith(ELLIPSIS), text)
        self.assertTrue(plain(line).startswith(text[:-len(ELLIPSIS)].rstrip()))
        self.assertTrue(plain(line)[len(text[:-len(ELLIPSIS)].rstrip()):].startswith(" | "))


class AutoBarSizeTest(unittest.TestCase):
    """F27: --bar-size auto measures the line instead of guessing at it."""

    def _bars(self, line):
        text = plain(line)
        return text.count(FILL) + text.count(EMPTY)

    def _non_bar_width(self):
        line = stock_line(bar_size="small")
        return cs._visible_len(line) - self._bars(line)

    def _auto(self, columns, **cfg):
        config = dict({"bar_size": "auto"}, **cfg)
        return stock_line(columns=columns, **config)

    def test_the_line_fits_when_it_can(self):
        non_bar = self._non_bar_width()
        want_each = 9
        columns = (non_bar + 3 * want_each) * 100 // cs.DEFAULT_MAX_WIDTH_PCT + 1
        budget = budget_for(columns)
        line = self._auto(columns)
        self.assertLessEqual(cs._visible_len(line), budget,
                             "auto left the line too wide, so it gets truncated")
        self.assertGreater(self._bars(line) // 3, 2)

    def test_bars_are_as_wide_as_the_budget_allows(self):
        non_bar = self._non_bar_width()
        for each in (4, 7, 9, 11):
            columns = (non_bar + 3 * each) * 100 // cs.DEFAULT_MAX_WIDTH_PCT + 1
            budget = budget_for(columns)
            with self.subTest(columns=columns):
                line = self._auto(columns)
                bw = self._bars(line) // 3
                self.assertEqual(bw, min(12, (budget - non_bar) // 3))
                self.assertLessEqual(cs._visible_len(line), budget)

    def test_full_size_when_there_is_room(self):
        line = self._auto(400)
        self.assertEqual(self._bars(line), 36)

    def test_floor_is_two_and_the_fit_then_drops_widgets_with_a_marker(self):
        line = self._auto(120)
        self.assertEqual(self._bars(line), 6, "bars are never narrower than two")
        out = fit(line, 120)
        self.assertLessEqual(cs._visible_len(out), budget_for(120))
        self.assertTrue(plain(out).endswith(ELLIPSIS))

    def test_a_named_size_is_still_never_shrunk(self):
        line = stock_line(columns=100, bar_size="large")
        self.assertEqual(self._bars(line), 36)

    def test_a_stale_label_is_part_of_what_is_measured(self):
        non_bar = self._non_bar_width()
        columns = (non_bar + 3 * 9) * 100 // cs.DEFAULT_MAX_WIDTH_PCT + 1
        line = stock_line(columns=columns, cache_age=700, bar_size="auto")
        self.assertLessEqual(cs._visible_len(line), budget_for(columns))

    def test_provider_rows_follow_the_measured_main_line(self):
        non_bar = self._non_bar_width()
        columns = (non_bar + 3 * 9) * 100 // cs.DEFAULT_MAX_WIDTH_PCT + 1
        config = {"bar_size": "auto"}
        with sandbox(), mock.patch.object(cs, "_detect_terminal_width",
                                          return_value=columns):
            line = cs.build_status_line(usage_dict(), "", dict(config), dict(STDIN))
            main_bw = self._bars(line) // 3
            self.assertEqual(cs._provider_bar_width(config, 15), main_bw)

    def test_provider_width_without_a_main_line_still_has_a_sane_fallback(self):
        with mock.patch.object(cs, "_detect_terminal_width", return_value=70), \
                mock.patch.object(cs, "_SHRINK_MEMO", None):
            width = cs._provider_bar_width({"bar_size": "auto", "max_width": 80}, 15)
        self.assertGreaterEqual(width, 2)
        self.assertLess(width, 12)


class CellWidthTest(unittest.TestCase):
    """F34: columns, not code points."""

    def test_wide_characters_count_two(self):
        self.assertEqual(cs._visible_len("コード"), 6)
        self.assertEqual(cs._visible_len("⚡fast"), 6)
        self.assertEqual(cs._visible_len("\U0001f525 3"), 4)

    def test_combining_and_joiners_count_zero(self):
        self.assertEqual(cs._visible_len("é"), 1)
        self.assertEqual(cs._visible_len("❤️ ok"), 4)
        self.assertEqual(cs._visible_len("a‍b"), 2)

    def test_ordinary_text_is_unchanged(self):
        self.assertEqual(cs._visible_len("Session ━━── 37%"), 16)
        self.assertEqual(cs._visible_len(f"{cs.GREEN}abc{cs.RESET}"), 3)

    def test_subagent_row_with_a_wide_name_fits_its_columns(self):
        task = {"id": "t", "name": "コードレビュー担当",
                "status": "running", "model": "claude-opus-5-5"}
        for columns in (40, 60):
            with self.subTest(columns=columns):
                with sandbox():
                    row = cs._render_subagent_row(task, {}, columns=columns)
                self.assertLessEqual(cells(row), columns, plain(row))

    def test_clip_visible_never_splits_a_wide_character(self):
        out = cs._clip_visible("日本語日本語", 5)
        self.assertLessEqual(cells(out), 5)
        self.assertTrue(plain(out).endswith(ELLIPSIS))

    def test_clip_visible_leaves_a_row_that_fits(self):
        self.assertEqual(cs._clip_visible("abc", 3), "abc")

    def test_fit_line_counts_cells_for_the_glyphs_the_script_emits(self):
        line = ("⚡fast | コードレビュー担当者 | "
                "\U0001f525 12 | ☕ Break | " + "w" * 30)
        out = fit(line, 40, max_width=100)
        self.assertLessEqual(cells(out), 40)
        out = fit(line, 40, wrap="auto", max_width=100)
        for row in out.split("\n"):
            self.assertLessEqual(cells(row), 40)

    def test_a_cut_keeps_a_joiner_with_its_base(self):
        text = "ab❤️cd" + "x" * 30
        with mock.patch.object(cs, "_detect_terminal_width", return_value=6):
            out = cs._truncate_line(text, {"max_width": 100})
        self.assertLessEqual(cells(out), 6)
        shown = plain(out)
        self.assertEqual("❤" in shown, "❤️" in shown,
                         "the heart and its emoji selector were separated")


class RainbowEscapeTest(unittest.TestCase):
    """F35: rainbow_colorize treats an OSC 8 hyperlink as one escape sequence."""

    def _colorize(self, text, **kw):
        kw.setdefault("shimmer", False)
        return cs.rainbow_colorize(text, **kw)

    def test_the_link_terminator_survives_when_no_m_follows(self):
        text = cs._osc8("https://x.test/p/59", "#59") + " | 3 agents"
        out = self._colorize(text, color_all=False)
        self.assertEqual(plain(out), "#59 | 3 agents")
        self.assertEqual(cs._visible_len(out), len("#59 | 3 agents"))
        self.assertEqual(out.count(cs.OSC8_START), 2, "the link was never closed")
        self.assertIn(cs.OSC8_END, out)

    def test_a_link_with_an_m_in_the_url_is_not_mistaken_for_a_colour(self):
        text = cs._osc8(URL, "#59") + " | 3 agents"          # "github.com" has an m
        out = self._colorize(text, color_all=False)
        self.assertEqual(plain(out), "#59 | 3 agents")
        self.assertEqual(out.count(cs.OSC8_START), 2)
        after = out.split(cs.OSC8_END)[-1]
        self.assertIn("\033[38;2;", after, "text after the link lost its rainbow")

    def test_color_all_strips_the_link_entirely(self):
        text = cs._osc8(URL, "#59") + " | 3 agents"
        out = self._colorize(text, color_all=True)
        self.assertEqual(plain(out), "#59 | 3 agents")
        self.assertNotIn("]8;;", out)
        self.assertNotIn("github", out)

    def test_existing_colours_are_still_preserved_when_asked(self):
        bar = f"{cs.GREEN}━━{cs.RESET}"
        out = self._colorize("a " + bar + " b", color_all=False)
        self.assertIn(cs.GREEN + "━━", out)
        self.assertEqual(plain(out), "a ━━ b")

    def test_visible_width_is_exact_for_a_rainbow_pr_line(self):
        line = stock_line(theme="rainbow", stdin=dict(
            STDIN, pr_number=59, pr_url=URL, pr_review_state="approved"),
            show=dict(cs.DEFAULT_SHOW, pr=True))
        self.assertEqual(cs._visible_len(line), len(plain(line)))
        self.assertNotIn("]8;;", plain(line))

    def test_lone_escape_and_truncated_sequences_do_not_hang(self):
        for text in ("a\033", "\033]8;;never-ends", "\033[38;2;1;2", "x\033cy"):
            with self.subTest(text=text):
                self.assertIsInstance(self._colorize(text, color_all=False), str)


if __name__ == "__main__":
    unittest.main()
