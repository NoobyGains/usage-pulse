"""Provider rows: alignment, layouts, the main-line label, fitting, colour paths.

Run: ``python -m unittest tests.test_provider_rendering``
"""
import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, codex_line, cs, iso_z  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text):
    return ANSI.sub("", text)


class RenderingTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude")
        self.sb.claude_dir(".claude-b", expires_in=-60)  # no network in these tests
        self.sb.accounts_file({"a": {"dir": "~/.claude", "label": "Defend", "email": "a@x.io"},
                               "b": {"dir": "~/.claude-b", "label": "Sunny", "email": "b@x.io"}})
        self.sb.gemini(quota={
            "gemini-5h": {"remaining_fraction": 0.95, "reset_time": iso_z(5000)},
            "gemini-weekly": {"remaining_fraction": 0.32, "reset_time": iso_z(200000)}})
        self.sb.codex_rollout([codex_line(29.0, 5.0)])
        now = time.time()
        cs._write_provider_cache("claude:b", {
            "timestamp": now - 60,
            "usage": {"five_hour": {"utilization": 3.0, "resets_at": iso_z(3000)},
                      "seven_day": {"utilization": 63.0, "resets_at": iso_z(300000)}}})
        self.config = {
            "theme": "default", "layout": "standard", "bar_size": "large",
            "show": dict(cs.DEFAULT_SHOW), "providers_label_main": True,
            "providers": [{"id": "claude:default", "type": "claude"},
                          {"id": "claude:b", "type": "claude"},
                          {"id": "gemini", "type": "gemini"},
                          {"id": "codex", "type": "codex"}],
        }
        p = mock.patch.object(cs, "_detect_terminal_width", lambda: 220)
        p.start()
        self.addCleanup(p.stop)

    def _usage(self):
        return {"five_hour": {"utilization": 29.0, "resets_at": iso_z(7800)},
                "seven_day": {"utilization": 5.0, "resets_at": iso_z(400000)}}

    def _main_line(self, config=None):
        config = config or self.config
        line = cs.build_status_line(self._usage(), "Max 20x", config, {"context_pct": 12})
        return cs._append_provider_rows(line, config)

    def test_rows_share_one_separator_column_with_main_line(self):
        out = self._main_line()
        rows = out.split("\n")
        self.assertEqual(len(rows), 4)  # main + Sunny + Gemini + Codex
        cols = {plain(r).index(" | ") for r in rows}
        self.assertEqual(len(cols), 1, [plain(r) for r in rows])
        self.assertTrue(plain(rows[0]).startswith("Claude (Defend) 5h "))
        self.assertTrue(plain(rows[1]).startswith("Claude (Sunny)  5h "))
        self.assertIn("Weekly", plain(rows[2]))
        self.assertTrue(plain(rows[3]).startswith("Codex"))

    def test_pace_arrows_follow_the_main_line(self):
        # Off by default: no arrow on any row.
        for r in self._main_line().split("\n")[1:]:
            for glyph in "↑↓⇈⇊→":
                self.assertNotIn(glyph, plain(r))
            self.assertNotRegex(plain(r), r"% \(")
        config = dict(self.config, show=dict(self.config["show"], pace=True))
        rows = [plain(r) for r in self._main_line(config).split("\n")]
        # claude:b: 3% used with 50 min of the 5-hour window left (⇊), 63% of
        # the week used with 3.5 days left (↑).
        sunny = rows[1]
        self.assertTrue(sunny.startswith("Claude (Sunny)"), sunny)
        head, tail = sunny.split(" | ")
        self.assertIn("3% ⇊", head)
        self.assertIn("63% ↑", tail)
        # Codex: 29% with an hour of five left, 5% of the week with 3 days left.
        self.assertIn("29% ⇊", rows[3])
        self.assertIn("5% ⇊", rows[3])
        # The rows still share the separator column with the main line.
        self.assertEqual(len({r.index(" | ") for r in rows}), 1, rows)
        # --pace-style percent shows the elapsed share instead.
        rows = [plain(r) for r in self._main_line(dict(config, pace_style="percent")).split("\n")]
        self.assertRegex(rows[1], r"3% \(8[2-4]%\)")

    def test_no_pace_arrow_on_a_stale_row(self):
        now = time.time()
        cs._write_provider_cache("claude:b", {
            "timestamp": now - 3600,
            "usage": {"five_hour": {"utilization": 3.0, "resets_at": iso_z(3000)},
                      "seven_day": {"utilization": 63.0, "resets_at": iso_z(300000)}}})
        config = dict(self.config, show=dict(self.config["show"], pace=True))
        rows = [plain(r) for r in self._main_line(config).split("\n")]
        self.assertIn("~3%", rows[1])
        for glyph in "↑↓⇈⇊→":
            self.assertNotIn(glyph, rows[1], rows[1])
        self.assertIn("29% ⇊", rows[3])   # the live Codex row keeps its arrow

    def test_main_label_needs_rows(self):
        config = dict(self.config, providers=[])
        line = cs.build_status_line(self._usage(), "", config, {})
        self.assertTrue(plain(line).startswith("Session "))
        self.assertEqual(cs._append_provider_rows(line, config), line)

    def test_label_main_off_and_status_message_win(self):
        config = dict(self.config, providers_label_main=False)
        self.assertTrue(plain(cs.build_status_line(self._usage(), "", config, {}))
                        .startswith("Session "))
        show = dict(cs.DEFAULT_SHOW, status_message=True)
        config = dict(self.config, show=show)
        first = plain(self._main_line(config).split("\n")[0])
        self.assertFalse(first.startswith("Claude (Defend)"))

    def test_hidden_providers(self):
        config = dict(self.config, show=dict(cs.DEFAULT_SHOW, providers=False))
        out = self._main_line(config)
        self.assertEqual(len(out.split("\n")), 1)
        self.assertTrue(plain(out).startswith("Session "))

    def test_layouts(self):
        for layout, needle in (("compact", " S "), ("minimal", ""), ("percent-first", "%")):
            config = dict(self.config, layout=layout)
            rows = self._main_line(config).split("\n")
            self.assertEqual(len(rows), 4, layout)
            for r in rows[1:]:
                self.assertIn(needle, plain(r))
                self.assertNotIn(" 5h ", plain(r))
            cols = {plain(r).index(" | ") for r in rows[1:]}
            self.assertEqual(len(cols), 1, layout)

    def test_missing_data_draws_dashes(self):
        config = dict(self.config, providers=[{"id": "file:k", "type": "file",
                                               "path": str(self.sb.root / "absent.json")}])
        rows, _ = cs._render_provider_rows(config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(plain(rows[0]).count("--"), 2)
        self.assertTrue(plain(rows[0]).startswith("k "))

    def test_file_label_used(self):
        f = self.sb.root / "kimi.json"
        f.write_text(json.dumps({"label": "Kimi", "five_hour": {"used_percentage": 50}}),
                     encoding="utf-8")
        config = dict(self.config, providers=[{"id": "file:kimi", "type": "file", "path": str(f)}])
        rows, _ = cs._render_provider_rows(config)
        self.assertTrue(plain(rows[0]).startswith("Kimi"))
        self.assertIn("50%", plain(rows[0]))

    def test_update_indicator_stays_on_main_row(self):
        line = cs.build_status_line(self._usage(), "", self.config, {})
        line = line + " \x1b[93m↑ Pulse Update\x1b[0m"
        out = cs._append_provider_rows(line, self.config).split("\n")
        self.assertIn("Pulse Update", out[0])
        self.assertTrue(all("Pulse Update" not in r for r in out[1:]))

    def test_fit_line_narrow_keeps_rows_in_budget(self):
        with mock.patch.object(cs, "_detect_terminal_width", lambda: 60):
            out = cs._fit_line(self._main_line(), self.config)
        budget = 60 * self.config.get("max_width", cs.DEFAULT_MAX_WIDTH_PCT) // 100
        for row in out.split("\n"):
            self.assertLessEqual(cs._visible_len(row), budget)

    def test_narrow_terminal_shrinks_row_bars_only_in_auto(self):
        wide = cs._provider_bar_width(self.config, 15)
        with mock.patch.object(cs, "_detect_terminal_width", lambda: 70):
            fixed = cs._provider_bar_width(self.config, 15)
            narrow = cs._provider_bar_width(dict(self.config, bar_size="auto"), 15)
        self.assertEqual(wide, 12)
        self.assertEqual(fixed, 12, "a named size is never shrunk (3.6.0)")
        self.assertLess(narrow, wide)
        self.assertGreaterEqual(narrow, 2)

    def test_row_cap(self):
        files = []
        for i in range(8):
            f = self.sb.root / f"p{i}.json"
            f.write_text(json.dumps({"five_hour": {"used_percentage": i}}), encoding="utf-8")
            files.append({"id": f"file:p{i}", "type": "file", "path": str(f)})
        config = dict(self.config, providers=files)
        rows, _ = cs._render_provider_rows(config)
        self.assertEqual(len(rows), 6)

    def test_rainbow_and_text_colour_paths(self):
        config = dict(self.config, theme="rainbow")
        out = self._main_line(config)
        self.assertIn("38;2;", out.split("\n")[1])
        self.assertEqual(len({plain(r).index(" | ") for r in out.split("\n")}), 1)
        config = dict(self.config, text_color="cyan")
        self.assertTrue(self._main_line(config).split("\n")[1].startswith("\x1b[36m"))

    def test_two_row_main_line_keeps_rows_after(self):
        config = dict(self.config, line2_widgets=["context"])
        out = self._main_line(config).split("\n")
        self.assertEqual(len(out), 5)
        self.assertIn("Context", plain(out[1]))

    def test_never_raises(self):
        for bad in (None, "x", {"providers": "junk"}, {"providers": [{"type": "claude", "id": 5}]}):
            self.assertEqual(cs._append_provider_rows("line", bad), "line")
        with mock.patch.object(cs, "_collect_provider_rows", side_effect=RuntimeError("boom")):
            self.assertEqual(cs._append_provider_rows("line", self.config), "line")


if __name__ == "__main__":
    unittest.main()
