"""Tests for the setup wizard (``--setup``), its screen toolkit and hook groups.

The wizard needs a real terminal, so these tests drive it the way
``tests/test_pick_theme.py`` drives the picker: a scripted key sequence
replaces the module-global ``_read_key`` and a fake stdout reports itself as a
TTY. Every file the wizard can touch — settings.json, the user config, the
state dir — lives in a temporary directory.

Run: ``python tests/test_setup_wizard.py``
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402


class _FakeTTY:
    """A stdout/stdin stand-in that says it is a terminal and records output."""

    def __init__(self, tty=True):
        self.buffer = io.BytesIO()
        self._tty = tty

    def isatty(self):
        return self._tty

    def write(self, text):
        self.buffer.write(text.encode("utf-8"))

    def flush(self):
        pass

    def text(self):
        return self.buffer.getvalue().decode("utf-8")


def _keys(seq):
    it = iter(seq)

    def read():
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("the wizard asked for more keys than the script has")
    return read


class _Sandbox(unittest.TestCase):
    """Point every path the wizard uses at a temp dir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.cfg_dir = root / "claude"
        self.cfg_dir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.user_config = root / "config.json"
        self.settings = self.cfg_dir / "settings.json"
        self.out = _FakeTTY()
        self.inp = _FakeTTY()
        for target, value in (("_claude_config_dir", lambda: self.cfg_dir),
                              ("get_state_dir", lambda: self.state),
                              ("get_config_path", lambda: self.user_config),
                              ("_enable_windows_ansi", lambda: None),
                              ("check_for_update", lambda: False),
                              ("check_claude_code_update", lambda *a, **k: False)):
            p = mock.patch.object(cs, target, value)
            p.start()
            self.addCleanup(p.stop)
        for name, obj in (("stdout", self.out), ("stdin", self.inp)):
            p = mock.patch.object(sys, name, obj)
            p.start()
            self.addCleanup(p.stop)
        # cmd_setup goes unattended on either name. Clear both for this test and
        # hand the contributor's own values back afterwards.
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("USAGE_PULSE_NONINTERACTIVE", "CLAUDE_PULSE_NONINTERACTIVE"):
            os.environ.pop(name, None)
        cs._settings_backed_up.clear()
        self.addCleanup(cs._settings_backed_up.clear)

    def write_config(self, data):
        self.user_config.write_text(json.dumps(data), encoding="utf-8")

    def saved_config(self):
        return json.loads(self.user_config.read_text(encoding="utf-8"))

    def settings_data(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def run_wizard(self, keys, args=()):
        with mock.patch.object(cs, "_read_key", _keys(keys)):
            return cs.cmd_setup(list(args))


# ---------------------------------------------------------------------------
# Toolkit
# ---------------------------------------------------------------------------

ITEMS = [("a", "Alpha", "first"), ("b", "Beta", "second"), ("c", "Gamma", "third")]


class ToolkitTest(_Sandbox):
    def _run(self, fn, keys, *a, **k):
        with mock.patch.object(cs, "_read_key", _keys(keys)):
            return fn(*a, **k)

    def test_select_moves_wraps_and_returns_index(self):
        self.assertEqual(self._run(cs._screen_select, ["down", "enter"], "S", "T", ITEMS), 1)
        self.assertEqual(self._run(cs._screen_select, ["up", "enter"], "S", "T", ITEMS), 2)
        self.assertEqual(self._run(cs._screen_select, ["j", "j", "j", "enter"], "S", "T", ITEMS), 0)
        self.assertEqual(self._run(cs._screen_select, ["3", "enter"], "S", "T", ITEMS), 2)
        self.assertEqual(self._run(cs._screen_select, ["enter"], "S", "T", ITEMS, 2), 2)

    def test_select_back_and_cancel(self):
        self.assertIs(self._run(cs._screen_select, ["left"], "S", "T", ITEMS), cs._BACK)
        self.assertIs(self._run(cs._screen_select, ["b"], "S", "T", ITEMS), cs._BACK)
        for key in ("esc", "q", "ctrl-c"):
            self.assertIsNone(self._run(cs._screen_select, [key], "S", "T", ITEMS))

    def test_select_preview_follows_cursor(self):
        seen = []
        self._run(cs._screen_select, ["down", "enter"], "S", "T", ITEMS,
                  preview=lambda key: seen.append(key) or f"PREVIEW-{key}")
        self.assertEqual(seen, ["a", "b"])
        self.assertIn("PREVIEW-b", self.out.text())

    def test_every_screen_clears_and_redraws(self):
        self._run(cs._screen_select, ["down", "down", "enter"], "S", "T", ITEMS)
        self.assertEqual(self.out.text().count("\x1b[H\x1b[J"), 3)

    def test_multiselect_toggle_all_none(self):
        self.assertEqual(self._run(cs._screen_multiselect, [" ", "down", " ", "enter"],
                                   "S", "T", ITEMS), {"a", "b"})
        self.assertEqual(self._run(cs._screen_multiselect, [" ", "enter"], "S", "T", ITEMS, {"a"}), set())
        self.assertEqual(self._run(cs._screen_multiselect, ["a", "enter"], "S", "T", ITEMS),
                         {"a", "b", "c"})
        self.assertEqual(self._run(cs._screen_multiselect, ["n", "enter"], "S", "T", ITEMS,
                                   {"a", "b"}), set())
        self.assertIs(self._run(cs._screen_multiselect, ["left"], "S", "T", ITEMS), cs._BACK)
        self.assertIsNone(self._run(cs._screen_multiselect, ["esc"], "S", "T", ITEMS))

    def test_multiselect_action_can_edit_items(self):
        items = list(ITEMS)

        def rename(i):
            items[i] = (items[i][0], "Renamed", items[i][2])
        self._run(cs._screen_multiselect, ["down", "r", "enter"], "S", "T", items,
                  actions={"r": rename})
        self.assertEqual(items[1][1], "Renamed")
        self.assertIn("Renamed", self.out.text())

    def test_text_typing_backspace_and_validation(self):
        keys = ["x", "y", "backspace", "z", "enter"]
        self.assertEqual(self._run(cs._screen_text, keys, "S", "T", "ab"), "abxz")
        # q and b are text here, not cancel/back.
        self.assertEqual(self._run(cs._screen_text, ["q", "b", "enter"], "S", "T"), "qb")
        bad_then_ok = ["enter", "7", "enter"]
        self.assertEqual(self._run(cs._screen_text, bad_then_ok, "S", "T",
                                   validate=lambda t: None if t else "empty"), "7")
        self.assertIn("empty", self.out.text())
        self.assertIs(self._run(cs._screen_text, ["left"], "S", "T"), cs._BACK)
        self.assertIsNone(self._run(cs._screen_text, ["esc"], "S", "T"))

    def test_text_respects_max_len(self):
        self.assertEqual(self._run(cs._screen_text, list("abcdefg") + ["enter"], "S", "T",
                                   max_len=5), "abcde")

    def test_confirm(self):
        self.assertTrue(self._run(cs._screen_confirm, ["enter"], "S", "T", True))
        self.assertFalse(self._run(cs._screen_confirm, ["enter"], "S", "T", False))
        self.assertFalse(self._run(cs._screen_confirm, ["down", "enter"], "S", "T", True))
        self.assertTrue(self._run(cs._screen_confirm, ["y"], "S", "T", False))
        self.assertFalse(self._run(cs._screen_confirm, ["n"], "S", "T", True))
        self.assertIs(self._run(cs._screen_confirm, ["left"], "S", "T"), cs._BACK)
        self.assertIsNone(self._run(cs._screen_confirm, ["esc"], "S", "T"))


@unittest.skipUnless(sys.platform == "win32", "Windows-specific key decoding")
class WindowsBackspaceTest(unittest.TestCase):
    def test_backspace_and_del_decode(self):
        import msvcrt
        for ch in ("\x08", "\x7f"):
            with mock.patch.object(msvcrt, "getwch", lambda ch=ch: ch):
                self.assertEqual(cs._read_key(), "backspace")
        with mock.patch.object(msvcrt, "getwch", lambda: " "):
            self.assertEqual(cs._read_key(), " ")


# ---------------------------------------------------------------------------
# Hook groups
# ---------------------------------------------------------------------------

class HookGroupsTest(_Sandbox):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(cs, "utf8_print", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _events(self):
        return set(self.settings_data().get("hooks", {}))

    def test_parse_groups(self):
        self.assertEqual(cs._parse_hook_groups("agents, team"), {"agents", "team"})
        self.assertEqual(cs._parse_hook_groups("all"), set(cs.HOOK_GROUP_NAMES))
        with self.assertRaises(ValueError):
            cs._parse_hook_groups("agents,bogus")
        with self.assertRaises(ValueError):
            cs._parse_hook_groups("")

    def test_only_selected_groups_are_written(self):
        self.assertTrue(cs.install_hooks({"agents"}))
        self.assertEqual(self._events(), {"SubagentStart", "SubagentStop"})
        cs.install_hooks({"team"})
        self.assertEqual(self._events(), {"SubagentStart", "SubagentStop",
                                          "TeammateIdle", "TaskCreated", "TaskCompleted"})
        self.assertEqual(cs._installed_pulse_hooks(self.settings_data()), {"agents", "team"})

    def test_default_is_all_three_groups(self):
        cs.install_hooks()
        self.assertEqual(cs._installed_pulse_hooks(self.settings_data()), set(cs.HOOK_GROUP_NAMES))
        post = self.settings_data()["hooks"]["PostToolUse"]
        self.assertEqual(post[0]["matcher"], "")
        # Pinned to exit 0 like every other hook command (F25).
        self.assertTrue(post[0]["hooks"][0]["command"].endswith("--hook-refresh; exit 0"))

    def test_remove_keeps_other_groups_and_foreign_hooks(self):
        foreign = {"matcher": "", "hooks": [{"type": "command", "command": "lint.sh"}]}
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [foreign]}, "model": "x"}),
                                 encoding="utf-8")
        cs.install_hooks()
        self.assertTrue(cs.remove_hooks({"heartbeat", "team"}))
        data = self.settings_data()
        self.assertEqual(data["hooks"]["PostToolUse"], [foreign])
        self.assertEqual(data["model"], "x")
        self.assertEqual(cs._installed_pulse_hooks(data), {"agents"})
        self.assertNotIn("TaskCreated", data["hooks"], "an emptied event list is dropped")

    def test_invalid_settings_are_never_overwritten(self):
        self.settings.write_text("{not json", encoding="utf-8")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(cs.install_hooks())
            self.assertFalse(cs.remove_hooks({"agents"}))
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_foreign_command_sharing_our_entry_survives(self):
        cs.install_hooks({"agents"})
        data = self.settings_data()
        data["hooks"]["SubagentStop"][0]["hooks"].append({"type": "command", "command": "echo foreign"})
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(cs.remove_hooks({"agents"}))
        left = self.settings_data()["hooks"]["SubagentStop"]
        self.assertEqual([h["command"] for e in left for h in e["hooks"]], ["echo foreign"])
        self.assertTrue(cs.install_hooks({"agents"}))
        cmds = [h["command"] for e in self.settings_data()["hooks"]["SubagentStop"] for h in e["hooks"]]
        self.assertIn("echo foreign", cmds)
        self.assertEqual(sum("--hook-subagent-stop" in c for c in cmds), 1)

    def test_unknown_group_raises(self):
        with self.assertRaises(ValueError):
            cs.install_hooks({"nope"})

    def test_cli_only_flag(self):
        with mock.patch.object(sys, "argv", ["x", "--install-hooks", "--only", "team"]):
            cs.main()
        self.assertEqual(cs._installed_pulse_hooks(self.settings_data()), {"team"})
        with mock.patch.object(sys, "argv", ["x", "--install-hooks", "--only", "teams"]):
            with self.assertRaises(SystemExit) as ctx:
                cs.main()
        self.assertEqual(ctx.exception.code, 2)


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

PROVIDER_ROWS = [
    {"id": "claude:default", "type": "claude", "label": "Claude", "path": "~/.claude",
     "active": True, "enabled": False, "tier": "Max", "age_seconds": 5, "note": ""},
    # For an unconfigured row, "enabled" is detection's recommendation.
    {"id": "claude:b", "type": "claude", "label": "Claude (b)", "path": "~/.claude-b",
     "active": False, "enabled": True, "tier": "Max", "age_seconds": 50, "note": ""},
    {"id": "codex", "type": "codex", "label": "Codex", "path": "~/.codex",
     "active": False, "enabled": True, "tier": "", "age_seconds": 60, "note": ""},
    {"id": "minimax", "type": "minimax", "label": "MiniMax", "path": "mmx",
     "active": False, "enabled": False, "tier": "", "age_seconds": None, "note": "runs mmx"},
]

# Screens: welcome, status line, hooks, providers, caps, theme, bar size,
# bar style, layout, currency, clock, effort, widgets, summary.
ENTER_THROUGH = ["enter"] * 14
ENTER_THROUGH_NO_PROVIDERS = ["enter"] * 13


class _WithProviders(_Sandbox):
    def setUp(self):
        super().setUp()
        self.set_calls = []
        self.rows = [dict(r) for r in PROVIDER_ROWS]

        def fake_set(config, enable_ids=(), disable_ids=(), labels=None):
            self.set_calls.append({"enable": set(enable_ids), "disable": set(disable_ids),
                                   "labels": dict(labels or {})})
            config = dict(config)
            types = {r["id"]: r["type"] for r in PROVIDER_ROWS}
            config["providers"] = [{"id": i, "type": types.get(i, "codex"), "enabled": True}
                                   for i in sorted(enable_ids)]
            return config
        def fake_detect(config=None):
            # Like the real one: a configured id reports its configured state,
            # an unconfigured one its recommendation.
            cfg = config if isinstance(config, dict) else cs.load_config()
            conf = {e["id"]: e for e in cfg.get("providers", [])
                    if isinstance(e, dict) and e.get("id")}
            out = []
            for r in self.rows:
                r = dict(r)
                if r["id"] in conf:
                    r["enabled"] = bool(conf[r["id"]].get("enabled"))
                out.append(r)
            return out
        for name, fn in (("detect_providers", fake_detect),
                         ("set_providers", fake_set)):
            p = mock.patch.object(cs, name, fn, create=True)
            p.start()
            self.addCleanup(p.stop)


class WizardDefaultsTest(_WithProviders):
    def test_enter_through_applies_the_default_plan(self):
        self.write_config({"theme": "ocean", "bar_size": "small"})
        self.assertEqual(self.run_wizard(ENTER_THROUGH), 0)
        data = self.settings_data()
        self.assertIn("claude_status.py", data["statusLine"]["command"])
        self.assertIn("--subagent-status-line", data["subagentStatusLine"]["command"])
        self.assertTrue((self.cfg_dir / "commands" / "pulse.md").exists())
        # Interactive defaults: live agents + team hooks, heartbeat off.
        self.assertEqual(cs._installed_pulse_hooks(data), {"agents", "team"})
        # Providers: everything detected except the active account and MiniMax.
        self.assertEqual(self.set_calls, [{"enable": {"claude:b", "codex"}, "disable": set(),
                                           "labels": {}}])
        saved = self.saved_config()
        self.assertEqual(saved["theme"], "ocean")
        self.assertEqual(saved["bar_size"], "small")
        self.assertFalse(saved["show"]["opus"] or saved["show"]["sonnet"] or saved["show"]["fable"])
        self.assertFalse(saved["show"]["heartbeat"])
        self.assertIn("Restart Claude Code", self.out.text())

    def test_step_header_and_summary_shown(self):
        self.run_wizard(ENTER_THROUGH)
        text = self.out.text()
        self.assertIn("Step 3/13 · Providers", text)
        self.assertIn("Apply these changes?", text)
        self.assertIn("install agents, team", text)

    def test_choosing_default_theme_does_not_factory_reset(self):
        self.write_config({"theme": "ocean", "bar_size": "small", "currency": "£"})
        keys = ["enter"] * 5 + ["up", "enter"] + ["enter"] * 8   # up from ocean → default
        self.assertEqual(self.run_wizard(keys), 0)
        saved = self.saved_config()
        self.assertEqual(saved["theme"], "default")
        self.assertEqual(saved["bar_size"], "small")
        self.assertEqual(saved["currency"], "£")

    def test_settings_preserved_and_other_status_line_replaced(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "ccline"},
                                             "model": "opus"}), encoding="utf-8")
        self.assertEqual(self.run_wizard(ENTER_THROUGH), 0)
        text = self.out.text()
        self.assertIn("Replace your current status line?", text)
        self.assertIn("ccline", text)
        data = self.settings_data()
        self.assertIn("claude_status.py", data["statusLine"]["command"])
        self.assertEqual(data["model"], "opus")
        self.assertIn("Replaced your previous status line command: ccline", text)

    def test_declining_status_line_keeps_the_other_tool(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "ccline"}}),
                                 encoding="utf-8")
        keys = ["enter", "n"] + ["enter"] * 12
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(self.settings_data()["statusLine"]["command"], "ccline")


class WizardCancelTest(_WithProviders):
    def _assert_untouched(self):
        self.assertFalse(self.settings.exists(), "settings.json must not be written")
        self.assertFalse(self.user_config.exists(), "config must not be written")
        self.assertEqual(self.set_calls, [])
        self.assertIn("Nothing changed.", self.out.text())

    def test_esc_on_first_screen(self):
        self.assertEqual(self.run_wizard(["esc"]), 0)
        self._assert_untouched()

    def test_esc_mid_way(self):
        self.assertEqual(self.run_wizard(["enter"] * 7 + ["esc"]), 0)
        self._assert_untouched()

    def test_cancel_on_summary(self):
        self.assertEqual(self.run_wizard(["enter"] * 13 + ["n"]), 0)
        self._assert_untouched()

    def test_ctrl_c_keypress(self):
        self.assertEqual(self.run_wizard(["enter", "ctrl-c"]), 0)
        self._assert_untouched()

    def test_keyboard_interrupt(self):
        def boom():
            raise KeyboardInterrupt
        with mock.patch.object(cs, "_read_key", boom):
            self.assertEqual(cs.cmd_setup([]), 0)
        self._assert_untouched()

    def test_alternate_screen_is_always_left(self):
        self.run_wizard(["enter", "esc"])
        text = self.out.text()
        self.assertEqual(text.count("\x1b[?1049h"), 1)
        self.assertEqual(text.count("\x1b[?1049l"), 1)
        self.assertTrue(text.index("\x1b[?1049l") < text.index("Nothing changed."))

    def test_invalid_settings_stop_with_error(self):
        self.settings.write_text("{oops", encoding="utf-8")
        self.assertEqual(self.run_wizard(["enter"]), 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{oops")
        self.assertFalse(self.user_config.exists())


class WizardChoicesTest(_WithProviders):
    def test_back_returns_to_previous_screen(self):
        # welcome, status line, hooks → back → status line again, then on.
        keys = ["enter", "enter", "left", "enter"] + ["enter"] * 12
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(self.out.text().count("Install the usage-pulse status line?"), 2)

    def test_caps_opt_in(self):
        keys = ["enter"] * 4 + [" ", "down", "down", " ", "enter"] + ["enter"] * 9
        self.assertEqual(self.run_wizard(keys), 0)
        show = self.saved_config()["show"]
        self.assertTrue(show["opus"])
        self.assertFalse(show["sonnet"])
        self.assertTrue(show["fable"])
        self.assertIn("usage-API call every", self.out.text())

    def test_heartbeat_tick_turns_on_its_widget(self):
        keys = ["enter", "enter", "down", "down", " ", "enter"] + ["enter"] * 11
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(cs._installed_pulse_hooks(self.settings_data()), set(cs.HOOK_GROUP_NAMES))
        self.assertTrue(self.saved_config()["show"]["heartbeat"])

    def test_unticking_an_installed_group_removes_it(self):
        with mock.patch.object(cs, "utf8_print", lambda *a, **k: None):
            cs.install_hooks()
        # Pre-ticked: all three. Untick heartbeat (third row).
        keys = ["enter", "enter", "down", "down", " ", "enter"] + ["enter"] * 11
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(cs._installed_pulse_hooks(self.settings_data()), {"agents", "team"})
        self.assertIn("remove heartbeat", self.out.text())

    def test_provider_untick_and_rename(self):
        # Rows: claude:default, claude:b, codex, minimax. Untick codex, rename claude:b.
        rename = ["r"] + ["backspace"] * 30 + list("Work") + ["enter"]
        keys = (["enter"] * 3 + ["down"] + rename + ["down", " ", "enter"] + ["enter"] * 10)
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(self.set_calls, [{"enable": {"claude:b"}, "disable": set(),
                                           "labels": {"claude:b": "Work"}}])

    def test_already_configured_provider_is_not_re_enabled(self):
        self.write_config({"providers": [{"id": "codex", "type": "codex", "enabled": False}]})
        self.assertEqual(self.run_wizard(ENTER_THROUGH), 0)
        self.assertEqual(self.set_calls[0]["enable"], {"claude:b"})

    def test_enabled_provider_untick_disables_it(self):
        self.rows[2]["enabled"] = True   # codex already on
        self.write_config({"providers": [{"id": "codex", "type": "codex", "enabled": True}]})
        keys = ["enter"] * 3 + ["down", "down", " ", "enter"] + ["enter"] * 10
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(self.set_calls[0]["disable"], {"codex"})

    def test_currency_other_and_clock_and_effort(self):
        before_currency = ["enter"] * 9
        # currency: jump to the last option ("Other…") with up, type a symbol.
        currency = ["up", "enter", "k", "r", "enter"]
        keys = before_currency + currency + ["down", "enter", "down", "down", "enter"] + ["enter"] * 2
        self.assertEqual(self.run_wizard(keys), 0)
        saved = self.saved_config()
        self.assertEqual(saved["currency"], "kr")
        self.assertEqual(saved["clock_format"], "24h")
        self.assertEqual(saved["effort_format"], "short")

    def test_first_run_defaults_to_the_detected_currency(self):
        with mock.patch.object(cs, "_detect_default_currency", lambda: "€"):
            self.assertEqual(self.run_wizard(ENTER_THROUGH), 0)
        self.assertEqual(self.saved_config()["currency"], "€")

    def test_saved_currency_is_kept_on_rerun(self):
        self.write_config({"currency": "$"})
        with mock.patch.object(cs, "_detect_default_currency", lambda: "€"):
            self.assertEqual(self.run_wizard(ENTER_THROUGH), 0)
        self.assertEqual(self.saved_config()["currency"], "$")

    def test_live_preview_is_drawn_on_look_screens(self):
        self.run_wizard(ENTER_THROUGH)
        self.assertIn("Opus 5.5", self.out.text())

    def test_budget_widget_asks_for_amount(self):
        items = [k for k, _l, _d in cs._SETUP_WIDGETS if k in cs.DEFAULT_SHOW]
        pos = items.index("budget")
        keys = (["enter"] * 12 + ["down"] * pos + [" ", "enter"]
                + list("25") + ["enter"] + ["enter"])
        self.assertEqual(self.run_wizard(keys), 0)
        saved = self.saved_config()
        self.assertEqual(saved["budget_usd"], 25.0)
        self.assertTrue(saved["show"]["budget"])

    def test_widget_toggle_only_writes_changes(self):
        items = [k for k, _l, _d in cs._SETUP_WIDGETS if k in cs.DEFAULT_SHOW]
        pos = items.index("cache")
        keys = ["enter"] * 12 + ["down"] * pos + [" ", "enter", "enter"]
        self.assertEqual(self.run_wizard(keys), 0)
        show = self.saved_config()["show"]
        self.assertTrue(show["cache"])
        self.assertTrue(show["lines"])
        self.assertIn("Show: cache", self.out.text())


NONINTERACTIVE_NAMES = ("USAGE_PULSE_NONINTERACTIVE", "CLAUDE_PULSE_NONINTERACTIVE")


class AmbientNoninteractiveEnvTest(_WithProviders):
    """A contributor (or a CI job) may export the documented
    USAGE_PULSE_NONINTERACTIVE=1; cmd_setup treats either name as "unattended",
    so every interactive-wizard test would silently take the unattended path
    unless the sandbox clears both names, and hands them back afterwards."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {name: "1" for name in NONINTERACTIVE_NAMES})
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_the_sandbox_clears_every_name_that_forces_unattended_setup(self):
        for name in NONINTERACTIVE_NAMES:
            self.assertNotIn(name, os.environ)

    def test_the_wizard_still_runs_interactively(self):
        self.assertEqual(self.run_wizard(["esc"]), 0)
        self.assertIn("Nothing changed.", self.out.text())
        self.assertFalse(self.settings.exists(), "the unattended path ran and wrote settings.json")

    def test_the_ambient_values_come_back_after_a_test(self):
        class _Probe(_Sandbox):
            def runTest(self):
                pass

        for name in NONINTERACTIVE_NAMES:
            os.environ[name] = "1"
        result = unittest.TestResult()
        _Probe().run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        for name in NONINTERACTIVE_NAMES:
            self.assertEqual(os.environ.get(name), "1", "%s was dropped for later tests" % name)


class WizardWithoutProvidersTest(_Sandbox):
    """A build without the providers functions skips that screen."""

    def setUp(self):
        super().setUp()
        for name in ("detect_providers", "set_providers"):
            p = mock.patch.object(cs, name, None, create=True)
            p.start()
            self.addCleanup(p.stop)

    def test_providers_screen_skipped_with_note(self):
        self.assertIsNone(cs._providers_api()[0])
        self.assertEqual(self.run_wizard(ENTER_THROUGH_NO_PROVIDERS), 0)
        text = self.out.text()
        self.assertNotIn("Step 3/13 · Providers", text)
        self.assertIn("Providers: not available in this build", text)

    def test_back_skips_over_the_missing_providers_screen(self):
        # welcome, status, hooks, caps → back lands on hooks, not providers.
        keys = ["enter"] * 3 + ["left", "enter"] + ["enter"] * 10
        self.assertEqual(self.run_wizard(keys), 0)
        self.assertEqual(self.out.text().count("Which hooks should usage-pulse install?"), 2)


class NonInteractiveTest(_WithProviders):
    def _no_keys(self):
        return mock.patch.object(cs, "_read_key", _keys([]))

    def test_flag_installs_status_line_and_providers_but_no_hooks(self):
        with self._no_keys():
            self.assertEqual(cs.cmd_setup(["--setup", "--non-interactive"]), 0)
        data = self.settings_data()
        self.assertIn("claude_status.py", data["statusLine"]["command"])
        self.assertIn("subagentStatusLine", data)
        self.assertEqual(cs._installed_pulse_hooks(data), set())
        self.assertTrue((self.cfg_dir / "commands" / "pulse.md").exists())
        self.assertEqual(self.set_calls[0]["enable"], {"claude:b", "codex"})

    def test_no_tty_falls_back_without_reading_keys(self):
        self.out._tty = False
        self.inp._tty = False
        with self._no_keys():
            self.assertEqual(cs.cmd_setup(["--setup"]), 0)
        self.assertIn("No interactive terminal", self.out.text())
        self.assertTrue(self.settings.exists())

    def test_env_var_forces_defaults(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PULSE_NONINTERACTIVE": "1"}), self._no_keys():
            self.assertEqual(cs.cmd_setup(["--setup"]), 0)

    def test_documented_env_var_forces_defaults(self):
        """USAGE_PULSE_NONINTERACTIVE=1 is the name the README gives."""
        with mock.patch.dict(os.environ, {"USAGE_PULSE_NONINTERACTIVE": "1"}), self._no_keys():
            self.assertEqual(cs.cmd_setup(["--setup"]), 0)
        self.assertTrue(self.settings.exists(), "the unattended defaults were not applied")
        self.assertNotIn("Nothing changed.", self.out.text())

    def test_rerun_does_not_re_enable(self):
        with self._no_keys():
            cs.cmd_setup(["--non-interactive"])
            cs.cmd_setup(["--non-interactive"])
        self.assertEqual(len(self.set_calls), 1, "the second run found nothing new")

    def test_invalid_settings_exit_nonzero(self):
        self.settings.write_text("[1,", encoding="utf-8")
        with self._no_keys(), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(cs.cmd_setup(["--non-interactive"]), 1)
        self.assertIn("invalid JSON", err.getvalue())
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "[1,")

    def test_main_exits_with_the_setup_code(self):
        self.settings.write_text("[1,", encoding="utf-8")
        with mock.patch.object(sys, "argv", ["x", "--setup", "--non-interactive"]), \
                mock.patch("sys.stderr", io.StringIO()), self._no_keys():
            with self.assertRaises(SystemExit) as ctx:
                cs.main()
        self.assertEqual(ctx.exception.code, 1)


class SettingsBackupTest(_Sandbox):
    """Every write of settings.json keeps a copy first, so a bad install is undoable."""

    def setUp(self):
        super().setUp()
        self.printed = []
        p = mock.patch.object(cs, "utf8_print", lambda *a, **k: self.printed.append(" ".join(str(x) for x in a)))
        p.start()
        self.addCleanup(p.stop)
        self.original = json.dumps({"theme": "dark", "statusLine": {"type": "command", "command": "other-tool"}})
        self.settings.write_text(self.original, encoding="utf-8")

    def backups(self):
        d = self.state / "settings-backups"
        return sorted(d.glob("*")) if d.exists() else []

    def test_install_status_line_keeps_the_previous_file(self):
        cs.install_status_line()
        files = self.backups()
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("claude-settings.json."), files[0].name)
        self.assertEqual(files[0].read_text(encoding="utf-8"), self.original)
        self.assertIn("other-tool", files[0].read_text(encoding="utf-8"))
        self.assertIn("claude_status.py", self.settings_data()["statusLine"]["command"])
        self.assertTrue(any("Previous settings.json kept at" in line for line in self.printed))

    def test_hooks_install_and_remove_back_up_too(self):
        cs._settings_backed_up.clear()
        self.assertTrue(cs.install_hooks({"agents"}))
        self.assertEqual(len(self.backups()), 1)
        cs._settings_backed_up.clear()
        self.assertTrue(cs.remove_hooks({"agents"}))
        self.assertEqual(len(self.backups()), 2)

    def test_one_backup_per_file_per_process(self):
        cs.install_status_line()
        cs.install_hooks({"team"})
        cs.sync_status_line_refresh({"refresh_interval": 7})
        self.assertEqual(len(self.backups()), 1, "later writes in the same run reuse the first copy")

    def test_missing_settings_means_no_backup(self):
        self.settings.unlink()
        cs.install_status_line()
        self.assertEqual(self.backups(), [])
        self.assertTrue(self.settings.exists())

    def test_only_the_newest_five_are_kept(self):
        d = self.state / "settings-backups"
        d.mkdir()
        for i in range(7):
            f = d / f"claude-settings.json.2026010{i}-000000"
            f.write_text(f"old {i}", encoding="utf-8")
            os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
        (d / "other-settings.json.20260101-000000").write_text("another account", encoding="utf-8")
        cs.install_status_line()
        ours = sorted(d.glob("claude-settings.json.*"))
        self.assertEqual(len(ours), cs._SETTINGS_BACKUP_KEEP)
        self.assertFalse((d / "claude-settings.json.20260100-000000").exists())
        self.assertFalse((d / "claude-settings.json.20260101-000000").exists())
        self.assertTrue(any(f.read_text(encoding="utf-8") == self.original for f in ours))
        self.assertTrue((d / "other-settings.json.20260101-000000").exists(), "other accounts' copies are untouched")

    def test_a_failed_copy_never_blocks_the_install(self):
        with mock.patch.object(cs.shutil, "copy2", side_effect=OSError("disk full")):
            self.assertIsNone(cs._backup_settings(self.settings))
            cs.install_status_line()
        self.assertIn("claude_status.py", self.settings_data()["statusLine"]["command"])
        self.assertEqual(self.backups(), [])

    def test_wizard_defaults_keep_a_backup(self):
        with mock.patch.object(cs, "_read_key", _keys([])):
            self.assertEqual(cs.cmd_setup(["--setup", "--non-interactive"]), 0)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(encoding="utf-8"), self.original)


class NonInteractiveSubprocessTest(unittest.TestCase):
    """End to end in a sandboxed HOME, the way the installers call it."""

    def test_setup_non_interactive_in_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = dict(os.environ)
            for key in ("HOME", "USERPROFILE"):
                env[key] = str(home)
            env["LOCALAPPDATA"] = str(home / "la")
            env["XDG_CACHE_HOME"] = str(home / "cache")
            env["XDG_CONFIG_HOME"] = str(home / "config")
            for key in ("HOMEDRIVE", "HOMEPATH", "CLAUDE_CONFIG_DIR", "CLAUDE_PULSE_NONINTERACTIVE"):
                env.pop(key, None)
            result = subprocess.run([sys.executable, str(REPO_ROOT / "claude_status.py"),
                                     "--setup", "--non-interactive"],
                                    env=env, capture_output=True, text=True, encoding="utf-8",
                                    stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
            self.assertIn("claude_status.py", data["statusLine"]["command"])
            self.assertNotIn("hooks", data)
            self.assertTrue((home / ".claude" / "commands" / "pulse.md").exists())



class AutoProviderIdsTest(unittest.TestCase):
    """Unattended setup switches on rows that are new AND recommended."""

    ROWS = [
        {"id": "claude:default", "type": "claude", "active": True, "enabled": True},
        {"id": "claude:b", "type": "claude", "active": False, "enabled": True},
        {"id": "codex", "type": "codex", "active": False, "enabled": True},
        {"id": "gemini", "type": "gemini", "active": False, "enabled": False},   # stale file
        {"id": "minimax", "type": "minimax", "active": False, "enabled": True},
        {"id": "file:kimi", "type": "file", "active": False, "enabled": True},
    ]

    def test_fresh_machine(self):
        self.assertEqual(cs._auto_provider_ids(self.ROWS, {}), {"claude:b", "codex"})

    def test_configured_rows_are_left_alone(self):
        cfg = {"providers": [{"id": "codex", "type": "codex", "enabled": False}]}
        self.assertEqual(cs._auto_provider_ids(self.ROWS, cfg), {"claude:b"})


class PythonCommandQuotingTest(unittest.TestCase):
    """The interpreter path is the literal forward-slash path, quoted only when it has a space.

    It used to be rewritten to ``$HOME/...``: that is Git Bash's home folder,
    not the one Python installed to, and a ParserError in PowerShell. The
    ``py`` launcher and 8.3 short paths are pinned off so the interpreter's own
    path is what is under test (see tests/test_pr2_windows_install.py for those).
    """

    def _cmd(self, home, exe):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(Path, "home", lambda: Path(home)), \
                mock.patch.object(sys, "executable", exe), \
                mock.patch.object(cs, "_windows_py_launcher", lambda: None), \
                mock.patch.object(cs, "_windows_short_path", lambda path: None):
            return cs._get_python_cmd()

    def test_home_with_a_space(self):
        self.assertEqual(self._cmd(r"C:\Users\First Last", r"C:\Users\First Last\Py\python.exe"),
                         '"C:/Users/First Last/Py/python.exe"')

    def test_home_without_a_space_is_a_plain_literal_path(self):
        self.assertEqual(self._cmd(r"C:\Users\Plain", r"C:\Users\Plain\Py\python.exe"),
                         "C:/Users/Plain/Py/python.exe")

    def test_path_outside_home_stays_bare(self):
        self.assertEqual(self._cmd(r"C:\Users\Plain", r"C:\Python313\python.exe"),
                         "C:/Python313/python.exe")

if __name__ == "__main__":
    unittest.main(verbosity=2)
