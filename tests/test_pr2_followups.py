"""Follow-ups to the 3.7.0 sweep fixes: the pieces the fix slices left to the coordinator.

F25  every installed hook command is pinned to exit 0, the non-blocking ones too
F40  the heartbeat in the bar is this window's, not whichever window's hook fired last
F15  the last state-file readers return their empty default on a damaged file
F03  git's output is decoded as UTF-8; a stray surrogate cannot blank the bar
F37  a reinstall keeps the other keys (padding) of our own statusLine
F23  a CLI command that fails says so and exits 1; the status line still never crashes
U3   ``--config`` says "restart" when the newer Claude Code is already installed

Nothing here touches the real home, settings or network: every case runs against
a throwaway state dir, and the one end-to-end case runs the script in a
subprocess whose environment points at a temp dir.

Run: ``python -m unittest tests.test_pr2_followups``
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(REPO_ROOT))

from provider_fixtures import ProviderSandbox, cs  # noqa: E402
from test_pr2_stdin_data import MainHarness  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
USAGE = {"five_hour": {"utilization": 12}, "seven_day": {"utilization": 34}}
WIN = sys.platform == "win32"


# ---------------------------------------------------------------------------
# F25 - every hook command exits 0
# ---------------------------------------------------------------------------

def _hook_shells():
    """Every shell Claude Code may run a hook command in, as argv prefixes."""
    if not WIN:
        sh = shutil.which("sh")
        return {"sh": [sh, "-c"]} if sh else {}
    shells = {}
    bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    if bash.exists():
        shells["git-bash"] = [str(bash), "-c"]
    for name, exe in (("pwsh7", "pwsh"), ("powershell5.1", "powershell")):
        found = shutil.which(exe)
        if found:
            shells[name] = [found, "-NoProfile", "-NonInteractive", "-Command"]
    return shells


class InstalledHooksAreAllPinnedToExitZeroTest(unittest.TestCase):
    """A moved or deleted install must never make Claude Code report a hook error."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.settings = root / "settings.json"
        state = root / "state"
        for patch in (mock.patch.object(cs, "_claude_config_dir", lambda: root),
                      mock.patch.object(cs, "get_state_dir", lambda: state),
                      mock.patch.object(cs, "utf8_print", lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)
        cs._settings_backed_up.clear()
        self.addCleanup(cs._settings_backed_up.clear)

    def _hooks(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]

    def _pulse_commands(self):
        found = {}
        for event, entries in self._hooks().items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    if "claude_status.py" in hook.get("command", ""):
                        found.setdefault(event, []).append(hook["command"])
        return found

    def test_every_installed_command_ends_with_exit_0(self):
        cs.install_hooks()
        found = self._pulse_commands()
        self.assertEqual(set(found), {"PostToolUse", "SubagentStart", "SubagentStop",
                                      "TeammateIdle", "TaskCreated", "TaskCompleted"})
        for event, commands in found.items():
            for command in commands:
                with self.subTest(event=event):
                    self.assertTrue(command.rstrip().endswith("; exit 0"), command)

    def test_the_non_blocking_events_keep_their_matcher(self):
        cs.install_hooks()
        hooks = self._hooks()
        for event in ("PostToolUse", "SubagentStart"):
            with self.subTest(event=event):
                self.assertEqual(hooks[event][-1]["matcher"], "")

    def test_a_reinstall_replaces_an_old_unpinned_command_instead_of_adding_one(self):
        old = {"matcher": "", "hooks": [{"type": "command",
               "command": "python3 \"/gone/claude_status.py\" --hook-refresh"}]}
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [old]}}), encoding="utf-8")
        cs.install_hooks({"heartbeat"})
        cs.install_hooks({"heartbeat"})
        commands = self._pulse_commands()["PostToolUse"]
        self.assertEqual(len(commands), 1, commands)
        self.assertTrue(commands[0].endswith("--hook-refresh; exit 0"), commands)

    def test_the_heartbeat_and_agent_start_commands_exit_zero_when_the_script_is_gone(self):
        shells = _hook_shells()
        if not shells:
            self.skipTest("no hook shell available")
        cs.install_hooks({"heartbeat", "agents"})
        found = self._pulse_commands()
        for event in ("PostToolUse", "SubagentStart"):
            command = found[event][0]
            self.assertEqual(command.count("claude_status.py"), 1, command)
            missing = command.replace("claude_status.py", "pulse-gone-dir/claude_status.py")
            for name, argv in shells.items():
                with self.subTest(event=event, shell=name):
                    r = subprocess.run(argv + [missing], input=b"{}", capture_output=True,
                                       timeout=120)
                    self.assertEqual((r.returncode, r.stdout), (0, b""), (name, r.stderr[-300:]))

    def test_the_pinned_heartbeat_command_still_runs_the_script(self):
        shells = _hook_shells()
        if not shells:
            self.skipTest("no hook shell available")
        cs.install_hooks({"heartbeat"})
        command = self._pulse_commands()["PostToolUse"][0]
        for name, argv in shells.items():
            with self.subTest(shell=name), tempfile.TemporaryDirectory() as state:
                env = dict(os.environ, LOCALAPPDATA=state, XDG_CACHE_HOME=state,
                           HOME=state, USERPROFILE=state, CLAUDE_CONFIG_DIR=state)
                payload = json.dumps({"tool_name": "Read", "session_id": "pin-sess"})
                r = subprocess.run(argv + [command], input=payload.encode("utf-8"),
                                   capture_output=True, env=env, timeout=120)
                self.assertEqual((r.returncode, r.stdout), (0, b""), (name, r.stderr[-300:]))
                beat = Path(state) / "claude-status" / "hook_sessions" / "pin-sess.json"
                self.assertTrue(beat.exists(), (name, r.stderr[-300:]))


# ---------------------------------------------------------------------------
# F40 - the heartbeat is this window's
# ---------------------------------------------------------------------------

class HeartbeatBelongsToThisWindowTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        p = mock.patch.object(cs, "get_config_path", return_value=self.sb.root / "config.json")
        p.start()
        self.addCleanup(p.stop)
        self.now = time.time()

    def _beat(self, sid, tools, started_ago, last_tool="Read"):
        folder = cs._hook_sessions_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sid}.json").write_text(json.dumps({
            "tool_count": tools, "session_start": self.now - started_ago,
            "session_id": sid, "last_tool": last_tool, "last_refresh": self.now}),
            encoding="utf-8")

    def _shared(self, sid, tools, started_ago, last_tool="Bash"):
        state = {"tool_count": tools, "session_start": self.now - started_ago,
                 "last_tool": last_tool, "last_refresh": self.now}
        if sid:
            state["session_id"] = sid
        cs._get_hook_state_path().write_text(json.dumps(state), encoding="utf-8")

    def _line(self, stdin_ctx, **show_on):
        show = {k: False for k in cs.DEFAULT_SHOW}
        show.update(heartbeat=True, **show_on)
        return ANSI.sub("", cs.build_status_line(USAGE, "", {"show": show}, stdin_ctx))

    def test_each_window_shows_its_own_tool_count_and_time(self):
        self._beat("win-a", 7, 3700)
        self._beat("win-b", 50, 72 * 3600)
        self._shared("win-b", 50, 72 * 3600)
        line = self._line({"session_id": "win-a"})
        self.assertIn("7 tools", line)
        self.assertIn("1h 1m", line)
        self.assertNotIn("50 tools", line)
        self.assertNotIn("72h", line)

    def test_the_window_that_fired_last_still_sees_its_own_numbers(self):
        self._beat("win-a", 7, 100)
        self._beat("win-b", 50, 200)
        self._shared("win-b", 50, 200)
        self.assertIn("50 tools", self._line({"session_id": "win-b"}))

    def test_a_new_window_shows_no_heartbeat_until_its_first_tool_call(self):
        self._beat("win-b", 50, 72 * 3600)
        self._shared("win-b", 50, 72 * 3600)
        line = self._line({"session_id": "win-new"})
        self.assertNotIn("tools", line)
        self.assertNotIn("50", line)

    def test_a_shared_state_from_before_session_ids_is_not_shown_to_a_named_window(self):
        self._shared(None, 4001, 72 * 3600)
        self.assertNotIn("tools", self._line({"session_id": "win-new"}))

    def test_a_window_whose_own_file_is_missing_still_gets_its_shared_snapshot(self):
        self._shared("win-a", 3, 60)
        self.assertIn("3 tools", self._line({"session_id": "win-a"}))

    def test_without_a_session_id_on_stdin_the_shared_state_is_used(self):
        self._shared("win-b", 9, 60)
        for ctx in ({}, None):
            with self.subTest(ctx=ctx):
                self.assertIn("9 tools", self._line(ctx))

    def test_the_last_tool_widget_is_this_windows_too(self):
        self._beat("win-a", 7, 100, last_tool="Edit")
        self._beat("win-b", 50, 100, last_tool="Bash")
        self._shared("win-b", 50, 100, last_tool="Bash")
        line = self._line({"session_id": "win-a"}, last_tool=True)
        self.assertIn("Last: Edit", line)
        self.assertNotIn("Bash", line)

    def test_last_tool_of_another_window_is_not_shown_to_a_new_one(self):
        self._beat("win-b", 50, 100, last_tool="Bash")
        self._shared("win-b", 50, 100, last_tool="Bash")
        self.assertNotIn("Last:", self._line({"session_id": "win-new"}, last_tool=True))


class SessionStateIsGivenTheLoadedConfigTest(MainHarness):
    """main() already holds the config; the session marker must not read the file again."""

    def setUp(self):
        super().setUp()
        self.configs = []
        real = cs._update_session_state

        def spy(usage, stdin_ctx, config=None):
            self.configs.append(config)
            return real(usage, stdin_ctx, config)

        p = mock.patch.object(cs, "_update_session_state", spy)
        p.start()
        self.addCleanup(p.stop)

    def _assert_given_the_config(self, how):
        self.assertTrue(self.configs, f"{how}: _update_session_state was never called")
        for config in self.configs:
            self.assertIsInstance(config, dict, f"{how}: config was not passed on")
            self.assertIn("show", config)

    def test_stdin_path(self):
        self.repaint(self.payload(five=(10, 3600), seven=(20, 86400)))
        self._assert_given_the_config("stdin path")

    def test_cached_path(self):
        self.write_cache({"timestamp": time.time(), "line": "cached", "plan": "Max 20x",
                          "usage": {"five_hour": {"utilization": 10, "resets_at": None},
                                    "seven_day": {"utilization": 20, "resets_at": None}}})
        self.repaint(self.payload(rate_limits=False))
        self._assert_given_the_config("cached path")

    def test_api_fallback_path(self):
        self.repaint(self.payload(rate_limits=False))
        self._assert_given_the_config("API fallback path")

    def test_gateway_path(self):
        spend = {"spend_limit": {"used_percentage": 62.8, "resets_at": time.time() + 86400 * 20,
                                 "used_usd": 314.12, "limit_usd": 500, "period": "monthly"}}
        self.token = (None, None)
        self.repaint(self.payload(extra_rl=spend))
        self._assert_given_the_config("gateway path")


# ---------------------------------------------------------------------------
# F15 - the remaining readers
# ---------------------------------------------------------------------------

NOT_UTF8 = b"\xff\xfe\x80 garbage \x9d"


class DamagedStateReadersTest(unittest.TestCase):
    """A damaged state file reads as "nothing there", never as an exception."""

    DICT_DAMAGE = {"bytes that are not UTF-8": NOT_UTF8, "a truncated write": b'{"tool_count": 4',
                   "an empty file": b"", "a list": b"[1, 2]", "a number": b"42",
                   "a string": b'"text"', "null": b"null"}
    LIST_DAMAGE = {"bytes that are not UTF-8": NOT_UTF8, "a truncated write": b'[{"t": 4',
                   "an empty file": b"", "an object": b'{"a": 1}', "a number": b"42",
                   "a string": b'"text"', "null": b"null"}

    def setUp(self):
        self.sb = ProviderSandbox(self)

    def _damaged(self, path, table, reader, expect):
        for case, data in table.items():
            with self.subTest(file=Path(path).name, damage=case):
                Path(path).write_bytes(data)
                try:
                    result = reader()
                except Exception as exc:  # noqa: BLE001 - the defect itself
                    self.fail(f"raised {type(exc).__name__}: {exc}")
                self.assertEqual(result, expect() if callable(expect) else expect)

    def test_hook_state(self):
        self._damaged(cs._get_hook_state_path(), self.DICT_DAMAGE, cs._read_hook_state, None)

    def test_usage_history(self):
        self._damaged(cs._get_history_path(), self.LIST_DAMAGE, cs._read_history, [])

    def test_context_history(self):
        self._damaged(cs._get_context_history_path(), self.LIST_DAMAGE,
                      cs._read_context_history, [])

    def test_animation_state(self):
        self._damaged(cs._get_anim_state_path(), self.DICT_DAMAGE, cs._load_anim_state, {})

    def test_stats(self):
        def read():
            stats = cs._load_stats()
            return (stats["total_sessions"], stats["daily_dates"], stats["current_streak"],
                    stats["longest_streak"], stats["last_date"])

        self._damaged(cs._get_stats_path(), self.DICT_DAMAGE, read, (0, [], 0, 0, ""))

    def test_a_damaged_stats_file_still_counts_the_day(self):
        cs._get_stats_path().write_bytes(NOT_UTF8)
        stats, _ = cs._update_stats()
        self.assertEqual(stats["total_sessions"], 1)
        self.assertEqual(json.loads(cs._get_stats_path().read_text(encoding="utf-8"))["total_sessions"], 1)

    def test_heatmap_renders_over_a_damaged_file(self):
        for case, data in {**self.DICT_DAMAGE, "hours as a list": b'{"hours": ["x"]}',
                           "hours as text": b'{"hours": "x"}'}.items():
            with self.subTest(damage=case):
                cs._get_heatmap_path().write_bytes(data)
                try:
                    out = cs._render_heatmap({})
                except Exception as exc:  # noqa: BLE001 - the defect itself
                    self.fail(f"raised {type(exc).__name__}: {exc}")
                self.assertEqual(len(out.split("\n")), 8, out)

    def test_heatmap_renders_over_damaged_hours(self):
        key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        for case, hours in {"a number for the hour": 5, "a list for the hour": [1],
                            "text for the percentage": {"session_pct": "lots"},
                            "null percentage": {"session_pct": None},
                            "infinite percentage": {"session_pct": float("inf")}}.items():
            with self.subTest(damage=case):
                cs._get_heatmap_path().write_text(
                    json.dumps({"hours": {key: hours}}), encoding="utf-8")
                try:
                    out = cs._render_heatmap({})
                except Exception as exc:  # noqa: BLE001 - the defect itself
                    self.fail(f"raised {type(exc).__name__}: {exc}")
                self.assertEqual(len(out.split("\n")), 8, out)

    def test_good_files_still_read_as_they_were(self):
        now = time.time()
        cs._get_hook_state_path().write_text(json.dumps({"tool_count": 3}), encoding="utf-8")
        self.assertEqual(cs._read_hook_state(), {"tool_count": 3})
        samples = [{"t": now, "s": 5, "w": 6}]
        cs._get_history_path().write_text(json.dumps(samples), encoding="utf-8")
        self.assertEqual(cs._read_history(), samples)
        ctx = [{"t": now, "c": 40, "s": "sess"}]
        cs._get_context_history_path().write_text(json.dumps(ctx), encoding="utf-8")
        self.assertEqual(cs._read_context_history(), ctx)
        cs._get_anim_state_path().write_text(json.dumps({"prev_pct_session": 50}), encoding="utf-8")
        self.assertEqual(cs._load_anim_state(), {"prev_pct_session": 50})
        stats = {"first_seen": "2026-01-01", "total_sessions": 4, "daily_dates": ["2026-01-01"],
                 "current_streak": 1, "longest_streak": 2, "last_date": "2026-01-01"}
        cs._get_stats_path().write_text(json.dumps(stats), encoding="utf-8")
        self.assertEqual(cs._load_stats(), stats)
        key = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        cs._get_heatmap_path().write_text(
            json.dumps({"hours": {key: {"session_pct": 90}}}), encoding="utf-8")
        self.assertIn("\u2588", cs._render_heatmap({}))

    def test_history_keeps_only_samples_the_renderer_can_draw(self):
        now = time.time()
        cs._get_history_path().write_text(json.dumps(
            [1, "x", {"t": now, "s": "high"}, {"t": now, "s": 3, "w": 4}]), encoding="utf-8")
        self.assertEqual(cs._read_history(), [{"t": now, "s": 3, "w": 4}])


# ---------------------------------------------------------------------------
# F03 - encodings
# ---------------------------------------------------------------------------

class GitBranchIsReadAsUtf8Test(unittest.TestCase):
    """git prints UTF-8; the machine's code page must not decide how it is read."""

    def setUp(self):
        self.sb = ProviderSandbox(self)

    def test_a_non_ascii_branch_name_survives_the_locale(self):
        seen = {}

        def fake_git(cmd, **kwargs):
            seen.update(kwargs)
            raw = "feature/caf\u00e9-\u2713".encode("utf-8")
            # What subprocess does: decode with the encoding it was given, else
            # with the machine's code page (cp1252 here).
            text = raw.decode(kwargs.get("encoding") or "cp1252", kwargs.get("errors") or "strict")
            return SimpleNamespace(returncode=0, stdout=text + "\n", stderr="")

        with mock.patch.object(cs.subprocess, "run", fake_git):
            branch = cs._get_git_branch()
        self.assertEqual(branch, "feature/caf\u00e9-\u2713")
        self.assertEqual(seen.get("errors"), "replace")

    def test_bytes_that_are_not_utf8_cannot_make_git_branch_raise(self):
        def fake_git(cmd, **kwargs):
            text = b"main\xff".decode(kwargs.get("encoding") or "cp1252", kwargs.get("errors") or "strict")
            return SimpleNamespace(returncode=0, stdout=text, stderr="")

        with mock.patch.object(cs.subprocess, "run", fake_git):
            self.assertTrue(cs._get_git_branch().startswith("main"))


class AStraySurrogateCannotBlankTheBarTest(MainHarness):
    """The last write to stdout replaces what UTF-8 cannot encode instead of raising."""

    BAD = "Session 12% \ud800 tail"

    def test_the_stdin_path_still_prints_the_line(self):
        with mock.patch.object(cs, "_guarded_status_line", lambda *a, **k: self.BAD):
            out = self.repaint(self.payload(five=(12, 3600), seven=(20, 86400)))
        self.assertIn("Session 12%", out)
        self.assertIn("tail", out)

    def test_the_cached_path_still_prints_the_line(self):
        self.write_cache({"timestamp": time.time(), "line": "cached", "plan": "Max 20x",
                          "usage": {"five_hour": {"utilization": 12, "resets_at": None},
                                    "seven_day": {"utilization": 20, "resets_at": None}}})
        with mock.patch.object(cs, "_guarded_status_line", lambda *a, **k: self.BAD):
            out = self.repaint(self.payload(rate_limits=False))
        self.assertIn("Session 12%", out)

    def test_the_api_path_still_prints_the_line(self):
        with mock.patch.object(cs, "build_status_line", lambda *a, **k: self.BAD):
            out = self.repaint(self.payload(rate_limits=False))
        self.assertIn("Session 12%", out)

    def test_the_fatal_error_handler_writes_replace_too(self):
        out = io.BytesIO()
        with mock.patch.object(cs, "RESET", "\ud800"), \
                mock.patch.object(sys, "argv", ["claude_status.py"]), \
                mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                mock.patch.object(sys, "stdout", SimpleNamespace(buffer=out, flush=lambda: None)):
            cs._handle_fatal_error(RuntimeError("boom"))
        self.assertEqual(out.getvalue(), b"?\n")


# ---------------------------------------------------------------------------
# F37 - the rest of our own statusLine survives a reinstall
# ---------------------------------------------------------------------------

class ReinstallKeepsOtherStatusLineKeysTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.settings = root / "settings.json"
        self.config = {}
        for patch in (mock.patch.object(cs, "_claude_config_dir", lambda: root),
                      mock.patch.object(cs, "get_state_dir", lambda: root / "state"),
                      mock.patch.object(cs, "get_config_path", lambda: root / "config.json"),
                      mock.patch.object(cs, "load_config", lambda: dict(self.config)),
                      mock.patch.object(cs, "_windows_py_launcher", lambda: None),
                      mock.patch.object(cs, "utf8_print", lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)
        cs._settings_backed_up.clear()
        self.addCleanup(cs._settings_backed_up.clear)

    def _seed(self, status_line):
        self.settings.write_text(json.dumps({"model": "keep-me", "statusLine": status_line}),
                                 encoding="utf-8")

    def _installed(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_padding_and_unknown_keys_survive(self):
        self._seed({"type": "command", "command": "python /old/claude_status.py",
                    "padding": 2, "somethingNew": {"a": 1}})
        cs.install_status_line()
        data = self._installed()
        line = data["statusLine"]
        self.assertEqual(line["padding"], 2)
        self.assertEqual(line["somethingNew"], {"a": 1})
        self.assertEqual(line["type"], "command")
        self.assertIn("claude_status.py", line["command"])
        self.assertNotIn("/old/", line["command"])
        self.assertEqual(data["model"], "keep-me")

    def test_a_stale_refresh_interval_goes_when_none_is_needed(self):
        self._seed({"type": "command", "command": "python /old/claude_status.py",
                    "refreshInterval": 15, "refresh": 150, "padding": 1})
        cs.install_status_line()
        line = self._installed()["statusLine"]
        self.assertNotIn("refreshInterval", line)
        self.assertNotIn("refresh", line, "the key Claude Code never had must still go")
        self.assertEqual(line["padding"], 1)

    def test_the_refresh_interval_is_updated_in_place(self):
        self.config = {"animate": "rainbow"}
        self._seed({"type": "command", "command": "python /old/claude_status.py",
                    "refreshInterval": 15, "padding": 1})
        cs.install_status_line()
        line = self._installed()["statusLine"]
        self.assertEqual(line["refreshInterval"], cs.REFRESH_ANIMATED)
        self.assertEqual(line["padding"], 1)

    def test_someone_elses_status_line_is_still_replaced_whole(self):
        self._seed({"type": "command", "command": "my-own-bar --fancy", "padding": 3,
                    "refreshInterval": 9})
        cs.install_status_line()
        line = self._installed()["statusLine"]
        self.assertEqual(set(line), {"type", "command"})
        self.assertIn("claude_status.py", line["command"])

    def test_a_status_line_that_is_not_an_object_is_replaced(self):
        self._seed("python /old/claude_status.py")
        cs.install_status_line()
        self.assertEqual(self._installed()["statusLine"]["type"], "command")

    def test_the_agent_panel_command_is_rewritten_too(self):
        self._seed({"type": "command", "command": "python /old/claude_status.py", "padding": 2})
        cs.install_status_line()
        self.assertIn("--subagent-status-line", self._installed()["subagentStatusLine"]["command"])


# ---------------------------------------------------------------------------
# F23 - a failed CLI command is not reported as success
# ---------------------------------------------------------------------------

class _Streams:
    """stdout (with .buffer) and stderr stand-ins, and the argv/stdin to run under."""

    def __init__(self, argv, tty):
        self.out = io.BytesIO()
        self.err = io.StringIO()
        self.argv = ["claude_status.py", *argv]
        self.tty = tty

    def __enter__(self):
        self._patches = [
            mock.patch.object(sys, "argv", self.argv),
            mock.patch.object(sys, "stdin", SimpleNamespace(isatty=lambda: self.tty)),
            mock.patch.object(sys, "stdout", SimpleNamespace(buffer=self.out, flush=lambda: None,
                                                             write=lambda s: None)),
            mock.patch.object(sys, "stderr", self.err),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self._patches):
            patch.stop()


class FatalErrorHandlerTest(unittest.TestCase):
    def test_status_line_mode_still_swallows_the_error(self):
        with _Streams([], tty=False) as s:
            cs._handle_fatal_error(RuntimeError("boom"))
        self.assertEqual(s.out.getvalue(), (cs.RESET + "\n").encode("utf-8"))
        self.assertEqual(s.err.getvalue(), "")

    def test_a_command_flag_reports_the_error_and_exits_1(self):
        for argv in (["--theme", "ocean"], ["--install"], ["--stats"]):
            with self.subTest(argv=argv), _Streams(argv, tty=False) as s:
                with self.assertRaises(SystemExit) as caught:
                    cs._handle_fatal_error(PermissionError("settings.json is locked"))
                self.assertEqual(caught.exception.code, 1)
                self.assertIn("settings.json is locked", s.err.getvalue())
                self.assertIn("PermissionError", s.err.getvalue())
                self.assertEqual(s.out.getvalue(), b"", "a failure must not print a blank bar")

    def test_a_person_at_a_terminal_gets_the_error_too(self):
        for argv in ([], ["--config"]):
            with self.subTest(argv=argv), _Streams(argv, tty=True) as s:
                with self.assertRaises(SystemExit) as caught:
                    cs._handle_fatal_error(OSError("disk full"))
                self.assertEqual(caught.exception.code, 1)
                self.assertIn("disk full", s.err.getvalue())

    def test_the_flags_claude_code_runs_stay_quiet_and_exit_0(self):
        internal = [row[0] for row in cs.CLI_FLAGS if row[2] == "Internal"]
        self.assertIn("--hook-refresh", internal)
        for flag in internal:
            with self.subTest(flag=flag), _Streams([flag], tty=False) as s:
                cs._handle_fatal_error(RuntimeError("boom"))
                self.assertEqual(s.out.getvalue(), b"",
                                 "a hook's stdout is read by Claude Code as context")
                self.assertEqual(s.err.getvalue(), "")

    def test_an_error_message_that_cannot_be_encoded_still_exits_1(self):
        class Narrow(io.StringIO):
            def write(self, text):
                text.encode("ascii")
                return super().write(text)

        with _Streams(["--stats"], tty=False) as s:
            s.err = Narrow()
            sys.stderr = s.err
            with self.assertRaises(SystemExit) as caught:
                cs._handle_fatal_error(RuntimeError("caf\u00e9 \u2713"))
        self.assertEqual(caught.exception.code, 1)


class FailedCommandExitsNonZeroEndToEndTest(unittest.TestCase):
    """The real script, in a subprocess whose home is a temp dir."""

    def _run(self, config_is_blocked):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("home", "local", "xdg-cache", "xdg-config", "appdata", "claude"):
                (root / name).mkdir()
            env = dict(os.environ, HOME=str(root / "home"), USERPROFILE=str(root / "home"),
                       LOCALAPPDATA=str(root / "local"), APPDATA=str(root / "appdata"),
                       XDG_CACHE_HOME=str(root / "xdg-cache"),
                       XDG_CONFIG_HOME=str(root / "xdg-config"),
                       CLAUDE_CONFIG_DIR=str(root / "claude"), PYTHONIOENCODING="utf-8")
            env.pop("USAGE_PULSE_NONINTERACTIVE", None)
            if config_is_blocked:
                # Where the config is saved is a folder, so the save cannot succeed.
                base = root / ("local" if WIN else "xdg-config") / "claude-status"
                (base / "config.json").mkdir(parents=True)
            theme = next(t for t in cs.THEMES if t != "default")
            return subprocess.run(
                [sys.executable, "-I", str(REPO_ROOT / "claude_status.py"), "--theme", theme],
                input=b"", capture_output=True, env=env, timeout=120)

    def test_a_save_that_fails_exits_1_and_says_why(self):
        r = self._run(config_is_blocked=True)
        self.assertEqual(r.returncode, 1, (r.stdout[-300:], r.stderr[-300:]))
        self.assertTrue(r.stderr.strip(), "nothing said why")

    def test_the_same_command_succeeds_when_the_save_can(self):
        r = self._run(config_is_blocked=False)
        self.assertEqual(r.returncode, 0, (r.stdout[-300:], r.stderr[-300:]))


# ---------------------------------------------------------------------------
# F07 - the cost-scan claim must hold when two windows start together
# ---------------------------------------------------------------------------

class CostScanClaimSurvivesAClockWobbleTest(unittest.TestCase):
    """Found when the full suite flaked: test_pr2_costs.test_claim_is_exclusive.

    A window takes ``now`` and then tries for the claim; a window that took the
    claim a moment earlier stamped the file a moment AFTER that ``now``. The age
    came out slightly negative, the fresh claim was judged "stale", deleted and
    taken over, and two scans ran at once: the exact thing the claim is for.
    """

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.lock = cs.get_account_state_dir() / "cost_scan_weekly.lock"

    def test_a_claim_stamped_just_after_now_is_still_held(self):
        now = time.time()
        self.assertTrue(cs._claim_cost_scan("weekly", now))
        for ahead in (0.001, 0.5, 5):
            with self.subTest(stamped_ahead_by=ahead):
                os.utime(self.lock, (now + ahead, now + ahead))
                self.assertFalse(cs._claim_cost_scan("weekly", now))
                self.assertTrue(self.lock.exists(), "the live claim was deleted")

    def test_a_claim_from_far_in_the_future_still_lapses(self):
        """A clock set back must not wedge the claim for good."""
        now = time.time()
        self.assertTrue(cs._claim_cost_scan("weekly", now))
        os.utime(self.lock, (now + 3600, now + 3600))
        self.assertTrue(cs._claim_cost_scan("weekly", now))

    def test_an_old_claim_still_lapses_and_a_young_one_still_holds(self):
        now = time.time()
        self.assertTrue(cs._claim_cost_scan("weekly", now))
        os.utime(self.lock, (now - 10, now - 10))
        self.assertFalse(cs._claim_cost_scan("weekly", now))
        old = now - 2 * cs._COST_SCAN_LOCK_TTL
        os.utime(self.lock, (old, old))
        self.assertTrue(cs._claim_cost_scan("weekly", now))


# ---------------------------------------------------------------------------
# U3 - --config says restart when the newer Claude Code is already installed
# ---------------------------------------------------------------------------

class ConfigUpdateLineSaysRestartTest(unittest.TestCase):
    RUNNING, CHANNEL = "2.1.100", "2.1.199"

    def setUp(self):
        self.sb = ProviderSandbox(self)
        (self.sb.state / "claude_code_update.json").write_text(json.dumps({
            "timestamp": time.time(), "update_available": True, "local": self.RUNNING,
            "remote": self.CHANNEL, "channel": "latest",
            "tags": {"latest": self.CHANNEL}}), encoding="utf-8")

    def _claude_line(self, installed):
        lines = []
        fake_version = SimpleNamespace(returncode=0, stdout=f"{self.RUNNING} (Claude Code)\n", stderr="")
        with mock.patch.object(cs, "utf8_print", lambda s="": lines.append(str(s))), \
                mock.patch.object(cs, "load_config", lambda: {}), \
                mock.patch.object(cs, "get_local_commit", lambda: None), \
                mock.patch.object(cs, "get_credentials", lambda *a, **k: (None, None)), \
                mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: True), \
                mock.patch.object(cs, "_installed_claude_version", lambda: installed), \
                mock.patch.object(cs, "_CLAUDE_PATH", "claude"), \
                mock.patch.object(cs.subprocess, "run", lambda *a, **k: fake_version):
            cs.cmd_print_config()
        found = [ANSI.sub("", ln) for ln in lines if "Claude Code:" in ln]
        self.assertEqual(len(found), 1, lines)
        return found[0]

    def test_already_installed_means_restart(self):
        for installed in (self.CHANNEL, "2.1.250"):
            with self.subTest(installed=installed):
                line = self._claude_line(installed)
                self.assertIn("restart", line.lower())
                self.assertNotIn("claude update", line)
                self.assertIn(self.CHANNEL, line)

    def test_not_yet_installed_means_update(self):
        for installed in ("2.1.150", self.RUNNING):
            with self.subTest(installed=installed):
                line = self._claude_line(installed)
                self.assertIn("claude update", line)
                self.assertNotIn("restart", line.lower())

    def test_an_unknown_install_keeps_the_update_wording(self):
        line = self._claude_line(None)
        self.assertIn("claude update", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
