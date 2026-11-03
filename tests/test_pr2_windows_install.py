"""Windows install fixes: the commands written to settings.json must run.

Claude Code runs a status line or hook command through Git Bash when Git for
Windows is installed and through PowerShell when it is not, and the
``statusLine`` setting has no field to say which. So the text the installer
writes has to parse in whichever shell ends up running it. These tests run the
real installed text through every shell this machine has, against a stand-in
script that only records the arguments it was given (nothing here touches the
network or the real home folder).

Findings covered:
  F04  the status line / hook command was a PowerShell parse error
  F09  the plugin's SessionStart hook was Bash-only
  F20  both installers picked the Microsoft Store python stub
  F22  ``$HOME`` is Git Bash's home, not the profile folder Python installed to
  F36  the command pinned one exact Python (versioned folder, venv)

Run: ``python tests/test_pr2_windows_install.py``
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

WIN = sys.platform == "win32"

# Stand-in for claude_status.py: records its arguments, does nothing else.
STAND_IN = (
    "import json, os, sys\n"
    "with open(os.environ['PULSE_TEST_MARKER'], 'w', encoding='utf-8') as f:\n"
    "    json.dump(sys.argv[1:], f)\n"
)


def _git_bash():
    base = os.environ.get("ProgramFiles", r"C:\Program Files")
    bash = Path(base) / "Git" / "bin" / "bash.exe"
    return str(bash) if bash.exists() else None


def hook_shells(*names):
    """Shells Claude Code may run a command in here, as argv prefixes.

    sh elsewhere; on Windows Git Bash, or PowerShell (pwsh 7, then Windows
    PowerShell 5.1) when Git Bash is not installed. ``names`` narrows the set.
    """
    shells = {}
    if not WIN:
        sh = shutil.which("sh")
        if sh:
            shells["sh"] = [sh, "-c"]
    else:
        bash = _git_bash()
        if bash:
            shells["git-bash"] = [bash, "-c"]
        for name, exe in (("pwsh7", "pwsh"), ("powershell5.1", "powershell")):
            found = shutil.which(exe)
            if found:
                shells[name] = [found, "-NoProfile", "-NonInteractive", "-Command"]
    if names:
        shells = {k: v for k, v in shells.items() if k in names or (k == "sh" and "git-bash" in names)}
    return shells


def run_in(argv, command, env, stdin=b"{}"):
    return subprocess.run(argv + [command], input=stdin, capture_output=True, env=env, timeout=180)


_VENV_ROOT = None
_VENV_PYTHON = None


def _make_venv_with_space(_unused=None):
    """A real interpreter at a path with a space in it, like C:\Program Files\PythonXY (built once)."""
    global _VENV_ROOT, _VENV_PYTHON
    if _VENV_PYTHON is None:
        _VENV_ROOT = tempfile.TemporaryDirectory()
        venv = Path(_VENV_ROOT.name).resolve() / "with space" / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                       check=True, capture_output=True, timeout=180)
        _VENV_PYTHON = venv / ("Scripts/python.exe" if WIN else "bin/python3")
    return _VENV_PYTHON


def tearDownModule():
    if _VENV_ROOT is not None:
        _VENV_ROOT.cleanup()


class _CommandSandbox(unittest.TestCase):
    """install_status_line / install_hooks writing into a temp settings.json."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        self.home = self.root / "home"
        self.script = self.home / ".usage-pulse" / "claude_status.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text(STAND_IN, encoding="utf-8")
        self.cfg = self.root / "claude"
        self.cfg.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.marker = self.root / "marker.json"
        self.settings = self.cfg / "settings.json"
        home = self.home
        for target, value in (("_claude_config_dir", lambda: self.cfg),
                              ("get_state_dir", lambda: self.state),
                              ("get_config_path", lambda: self.root / "config.json"),
                              ("__file__", str(self.script)),
                              ("utf8_print", lambda *a, **k: None)):
            p = mock.patch.object(cs, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(Path, "home", lambda: home)
        p.start()
        self.addCleanup(p.stop)
        cs._settings_backed_up.clear()
        self.addCleanup(cs._settings_backed_up.clear)

    def require_plain_interpreter(self):
        """The 'plain interpreter' tests need this machine's own python path to be one plain word."""
        if not cs._SHELL_NEUTRAL_WORD.fullmatch(Path(sys.executable).as_posix()):
            self.skipTest(f"interpreter path needs quoting: {sys.executable}")

    def pin(self, launcher=None, short=None, git_bash=True, executable=None):
        """Fix the environment-dependent choices (create=True: the helpers may not exist yet)."""
        for name, value in (("_windows_py_launcher", lambda: launcher),
                            ("_windows_short_path", lambda path: short),
                            ("_git_bash_available", lambda settings=None: git_bash)):
            p = mock.patch.object(cs, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        if executable is not None:
            p = mock.patch.object(sys, "executable", str(executable))
            p.start()
            self.addCleanup(p.stop)

    def installed(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def hook_command(self, event, flag):
        for entry in self.installed()["hooks"][event]:
            for hook in entry["hooks"]:
                if flag in hook["command"]:
                    return hook["command"]
        self.fail(f"no {flag} command in {event}")

    def run_everywhere(self, command, shells, expected_args, env_extra=None):
        self.assertTrue(shells, "no shell available to test with")
        for name, argv in shells.items():
            with self.subTest(shell=name, command=command):
                if self.marker.exists():
                    self.marker.unlink()
                env = dict(os.environ, PULSE_TEST_MARKER=str(self.marker), **(env_extra or {}))
                r = run_in(argv, command, env)
                detail = (name, r.returncode, r.stdout[-300:], r.stderr[-400:])
                self.assertEqual(r.returncode, 0, detail)
                self.assertTrue(self.marker.exists(), detail)
                self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8")), expected_args, detail)


class StatusLineRunsInEveryShellTest(_CommandSandbox):
    """F04: the text written for the status line has to parse in Git Bash and PowerShell alike."""

    def test_plain_interpreter_status_line(self):
        self.require_plain_interpreter()
        self.pin(launcher=None, git_bash=True)
        cs.install_status_line()
        command = self.installed()["statusLine"]["command"]
        self.run_everywhere(command, hook_shells(), [])

    def test_plain_interpreter_agent_panel_rows(self):
        self.require_plain_interpreter()
        self.pin(launcher=None, git_bash=True)
        cs.install_status_line()
        command = self.installed()["subagentStatusLine"]["command"]
        self.run_everywhere(command, hook_shells(), ["--subagent-status-line"])

    def test_plain_interpreter_non_blocking_hooks(self):
        self.require_plain_interpreter()
        self.pin(launcher=None, git_bash=True)
        self.assertTrue(cs.install_hooks({"heartbeat", "agents"}))
        for event, flag in (("PostToolUse", "--hook-refresh"), ("SubagentStart", "--hook-subagent-start")):
            with self.subTest(event=event):
                self.run_everywhere(self.hook_command(event, flag), hook_shells(), [flag])

    def test_interpreter_path_with_a_space_when_git_bash_runs_it(self):
        exe = _make_venv_with_space(self.root)
        self.pin(launcher=None, short=None, git_bash=True, executable=exe)
        cs.install_status_line()
        command = self.installed()["statusLine"]["command"]
        self.assertIn("with space", command)
        self.run_everywhere(command, hook_shells("git-bash"), [])
        cs.install_hooks({"heartbeat", "agents"})
        for event, flag in (("PostToolUse", "--hook-refresh"), ("SubagentStop", "--hook-subagent-stop")):
            self.run_everywhere(self.hook_command(event, flag), hook_shells("git-bash"), [flag])

    @unittest.skipUnless(WIN, "PowerShell is the fallback shell on Windows only")
    def test_interpreter_path_with_a_space_when_only_powershell_runs_it(self):
        exe = _make_venv_with_space(self.root)
        self.pin(launcher=None, short=None, git_bash=False, executable=exe)
        cs.install_status_line()
        command = self.installed()["statusLine"]["command"]
        self.assertIn("with space", command)
        shells = hook_shells("pwsh7", "powershell5.1")
        self.run_everywhere(command, shells, [])
        self.run_everywhere(self.installed()["subagentStatusLine"]["command"], shells, ["--subagent-status-line"])
        cs.install_hooks({"heartbeat", "agents"})
        for event, flag in (("PostToolUse", "--hook-refresh"), ("SubagentStop", "--hook-subagent-stop")):
            self.run_everywhere(self.hook_command(event, flag), shells, [flag])

    @unittest.skipUnless(WIN, "Windows-only wording")
    def test_setup_says_so_when_it_wrote_the_powershell_form(self):
        printed = []
        self.pin(launcher=None, short=None, git_bash=False, executable=r"C:\Program Files\Py\python.exe")
        with mock.patch.object(cs, "utf8_print", lambda text="": printed.append(text)):
            cs.install_status_line()
        self.assertTrue(any("PowerShell" in line for line in printed), printed)


class PulseCommandTextTest(unittest.TestCase):
    """F04/F22: the exact text, per platform and interpreter shape (pure function)."""

    SCRIPT = "C:/Users/me/.usage-pulse/claude_status.py"

    def build(self, python_cmd, script=SCRIPT, flag=None, git_bash=True, platform="win32"):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(cs, "_git_bash_available", lambda settings=None: git_bash, create=True):
            return cs._pulse_command(python_cmd, script, flag)

    def test_bare_interpreter_gets_a_single_quoted_script(self):
        self.assertEqual(self.build("C:/Python313/python.exe"),
                         "C:/Python313/python.exe 'C:/Users/me/.usage-pulse/claude_status.py'")

    def test_flag_is_appended(self):
        self.assertEqual(self.build("C:/Python313/python.exe", flag="--subagent-status-line"),
                         "C:/Python313/python.exe 'C:/Users/me/.usage-pulse/claude_status.py' --subagent-status-line")

    def test_launcher_form(self):
        self.assertEqual(self.build("py -3"), "py -3 'C:/Users/me/.usage-pulse/claude_status.py'")

    def test_quoted_interpreter_with_git_bash_is_the_bash_form(self):
        self.assertEqual(self.build('"C:/Program Files/Python313/python.exe"'),
                         "\"C:/Program Files/Python313/python.exe\" 'C:/Users/me/.usage-pulse/claude_status.py'")

    def test_quoted_interpreter_without_git_bash_is_the_powershell_form(self):
        self.assertEqual(self.build('"C:/Program Files/Python313/python.exe"', git_bash=False),
                         "& \"C:/Program Files/Python313/python.exe\" 'C:/Users/me/.usage-pulse/claude_status.py'")

    def test_script_path_with_an_apostrophe_uses_double_quotes(self):
        self.assertEqual(self.build("C:/Python313/python.exe", script="C:/Users/O'Brien/.usage-pulse/claude_status.py"),
                         "C:/Python313/python.exe \"C:/Users/O'Brien/.usage-pulse/claude_status.py\"")

    def test_posix_keeps_the_established_form(self):
        self.assertEqual(self.build("python3", script="/home/u/.usage-pulse/claude_status.py", platform="linux"),
                         'python3 "/home/u/.usage-pulse/claude_status.py"')
        self.assertEqual(self.build('"/opt/my py/bin/python3"', script="/home/u/x/claude_status.py",
                                    flag="--hook-refresh", platform="darwin"),
                         '"/opt/my py/bin/python3" "/home/u/x/claude_status.py" --hook-refresh')


class SyncCanReadTheNewFormsTest(unittest.TestCase):
    """--sync-install-path finds the script in whatever the installer now writes."""

    def test_script_path_is_found_in_every_windows_form(self):
        script = "C:/Users/First Last/.claude/plugins/cache/mk/usage-pulse/3.6.2/claude_status.py"
        for python_cmd, git_bash in (("py -3", True), ("C:/Python313/python.exe", True),
                                     ('"C:/Program Files/Python313/python.exe"', True),
                                     ('"C:/Program Files/Python313/python.exe"', False)):
            with self.subTest(python_cmd=python_cmd, git_bash=git_bash),                     mock.patch.object(sys, "platform", "win32"),                     mock.patch.object(cs, "_git_bash_available", lambda settings=None, _g=git_bash: _g):
                command = cs._pulse_command(python_cmd, script, "--subagent-status-line")
                self.assertEqual(cs._pulse_script_in_command(command), script, command)

    def test_apostrophe_path_is_found_too(self):
        script = "C:/Users/O'Brien/.usage-pulse/claude_status.py"
        with mock.patch.object(sys, "platform", "win32"):
            command = cs._pulse_command("C:/Python313/python.exe", script)
        self.assertEqual(cs._pulse_script_in_command(command), script, command)


class HomeVariableTest(_CommandSandbox):
    """F22: ``$HOME`` is whatever the running shell says, not Python's profile folder."""

    def test_win_portable_path_is_a_plain_forward_slash_path(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(Path, "home", lambda: Path(r"C:\Users\me")):
            self.assertEqual(cs._win_portable_path(r"C:\Users\me\.usage-pulse\claude_status.py"),
                             "C:/Users/me/.usage-pulse/claude_status.py")
            self.assertEqual(cs._win_portable_path(r"D:\tools\Py\python.exe"), "D:/tools/Py/python.exe")
            self.assertEqual(cs._win_portable_path(r"\\?\C:\Users\me\x\claude_status.py"),
                             "C:/Users/me/x/claude_status.py")
            self.assertEqual(cs._win_portable_path(r"\\?\UNC\srv\share\x\claude_status.py"),
                             "//srv/share/x/claude_status.py")

    def test_posix_paths_are_left_alone(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(cs._win_portable_path("/home/u/a b/claude_status.py"), "/home/u/a b/claude_status.py")

    def test_nothing_the_installer_writes_uses_the_home_variable(self):
        self.pin(launcher=None, git_bash=True)
        cs.install_status_line()
        cs.install_hooks()
        self.assertNotIn("$HOME", json.dumps(self.installed()))

    @unittest.skipUnless(WIN and _git_bash(), "needs Git Bash on Windows")
    def test_status_line_runs_when_home_points_somewhere_else(self):
        """HOME set to a corporate home drive / Emacs dir while Python's profile is elsewhere."""
        self.require_plain_interpreter()
        self.pin(launcher=None, git_bash=True)
        cs.install_status_line()
        other_home = self.root / "somewhere else"
        other_home.mkdir()
        self.run_everywhere(self.installed()["statusLine"]["command"], hook_shells("git-bash"), [],
                            env_extra={"HOME": str(other_home)})


class PythonCommandTest(unittest.TestCase):
    """F22/F36: which interpreter text goes into the command."""

    def cmd(self, executable, home=r"C:\Users\Plain", launcher=None, short=None, platform="win32",
            prefix=None, base_prefix=None, base_executable=None):
        patches = [mock.patch.object(sys, "platform", platform),
                   mock.patch.object(Path, "home", lambda: Path(home)),
                   mock.patch.object(sys, "executable", executable),
                   mock.patch.object(cs, "_windows_py_launcher", lambda: launcher, create=True),
                   mock.patch.object(cs, "_windows_short_path", lambda path: short, create=True)]
        if prefix is not None:
            patches.append(mock.patch.object(sys, "prefix", prefix))
            patches.append(mock.patch.object(sys, "base_prefix", base_prefix))
        if base_executable is not None:
            patches.append(mock.patch.object(sys, "_base_executable", base_executable, create=True))
        for p in patches:
            p.start()
        try:
            return cs._get_python_cmd()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_interpreter_under_the_profile_is_the_literal_path(self):
        self.assertEqual(self.cmd(r"C:\Users\Plain\Py\python.exe"), "C:/Users/Plain/Py/python.exe")

    def test_profile_with_a_space_is_quoted_not_rewritten(self):
        self.assertEqual(self.cmd(r"C:\Users\First Last\Py\python.exe", home=r"C:\Users\First Last"),
                         '"C:/Users/First Last/Py/python.exe"')

    def test_path_outside_the_profile_stays_bare(self):
        self.assertEqual(self.cmd(r"C:\Python313\python.exe"), "C:/Python313/python.exe")

    def test_a_space_in_the_path_uses_the_short_path_when_there_is_one(self):
        self.assertEqual(self.cmd(r"C:\Program Files\Python313\python.exe", short="C:/PROGRA~1/Python313/python.exe"),
                         "C:/PROGRA~1/Python313/python.exe")

    def test_a_space_with_no_short_path_is_quoted(self):
        self.assertEqual(self.cmd(r"C:\Program Files\Python313\python.exe"),
                         '"C:/Program Files/Python313/python.exe"')

    def test_the_py_launcher_wins_when_it_works(self):
        """F36: py survives a Python upgrade; a versioned python.exe path does not."""
        self.assertEqual(self.cmd(r"C:\Users\Plain\AppData\Local\Programs\Python\Python313\python.exe",
                                  launcher="py -3"), "py -3")

    def test_a_venv_is_replaced_by_the_interpreter_underneath_it(self):
        """F36: a deleted venv or conda-less env must not blank the bar."""
        self.assertEqual(self.cmd(r"C:\proj\.venv\Scripts\python.exe", prefix=r"C:\proj\.venv",
                                  base_prefix=r"C:\Python313", base_executable=r"C:\Python313\python.exe"),
                         "C:/Python313/python.exe")

    def test_posix_is_unchanged(self):
        self.assertEqual(self.cmd("/usr/bin/python3", platform="linux"), "/usr/bin/python3")
        self.assertEqual(self.cmd("/opt/my py/bin/python3", platform="darwin"), '"/opt/my py/bin/python3"')


class PyLauncherProbeTest(unittest.TestCase):
    """F36: only a py launcher that really runs Python 3.8+ is used (the Store alias exists but may not work)."""

    def probe(self, which="C:/Windows/py.exe", result=None, error=None, platform="win32"):
        run = mock.Mock(side_effect=error) if error else mock.Mock(return_value=result)
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(cs.shutil, "which", lambda name: which), \
                mock.patch.object(cs.subprocess, "run", run):
            return cs._windows_py_launcher(), run

    def test_working_launcher(self):
        got, run = self.probe(result=subprocess.CompletedProcess([], 0, b"", b""))
        self.assertEqual(got, "py -3")
        self.assertIn("-3", run.call_args[0][0])

    def test_launcher_with_no_python_behind_it(self):
        got, _ = self.probe(result=subprocess.CompletedProcess([], 103, b"", b"No suitable Python runtime found"))
        self.assertIsNone(got)

    def test_launcher_missing(self):
        got, run = self.probe(which=None)
        self.assertIsNone(got)
        run.assert_not_called()

    def test_launcher_that_cannot_be_started_or_hangs(self):
        self.assertIsNone(self.probe(error=OSError("nope"))[0])
        self.assertIsNone(self.probe(error=subprocess.TimeoutExpired("py", 10))[0])

    def test_other_platforms_never_use_it(self):
        got, run = self.probe(result=subprocess.CompletedProcess([], 0, b"", b""), platform="linux")
        self.assertIsNone(got)
        run.assert_not_called()


class GitBashDetectionTest(unittest.TestCase):
    """The shell choice depends on whether Claude Code can find Git Bash."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def detect(self, settings=None, env=None, which=None):
        nowhere = str(self.root / "none")
        patched = {k: nowhere for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")}
        patched.update(env or {})
        with mock.patch.dict(os.environ, patched),                 mock.patch.object(cs.shutil, "which", lambda name: which):
            if "CLAUDE_CODE_GIT_BASH_PATH" not in patched:
                os.environ.pop("CLAUDE_CODE_GIT_BASH_PATH", None)
            return cs._git_bash_available(settings)

    def make_bash(self, *parts):
        bash = self.root.joinpath(*parts)
        bash.parent.mkdir(parents=True, exist_ok=True)
        bash.write_text("", encoding="utf-8")
        return bash

    def test_nothing_installed(self):
        self.assertFalse(self.detect())

    def test_program_files_install(self):
        self.make_bash("pf", "Git", "bin", "bash.exe")
        self.assertTrue(self.detect(env={"ProgramFiles": str(self.root / "pf")}))

    def test_path_set_by_environment_variable(self):
        bash = self.make_bash("custom", "bash.exe")
        self.assertTrue(self.detect(env={"CLAUDE_CODE_GIT_BASH_PATH": str(bash)}))

    def test_path_set_in_settings_json(self):
        bash = self.make_bash("custom2", "bash.exe")
        self.assertTrue(self.detect(settings={"env": {"CLAUDE_CODE_GIT_BASH_PATH": str(bash)}}))
        self.assertFalse(self.detect(settings={"env": {"CLAUDE_CODE_GIT_BASH_PATH": str(self.root / "gone.exe")}}))

    def test_found_next_to_git_on_path(self):
        self.make_bash("portable", "Git", "bin", "bash.exe")
        git = self.root / "portable" / "Git" / "cmd" / "git.exe"
        git.parent.mkdir(parents=True)
        git.write_text("", encoding="utf-8")
        self.assertTrue(self.detect(which=str(git)))

    def test_wsl_bash_does_not_count(self):
        """System32\\bash.exe is WSL, not Git Bash."""
        system = self.make_bash("Windows", "System32", "bash.exe")
        self.assertFalse(self.detect(which=str(system)))


class SessionStartHookTest(unittest.TestCase):
    """F09: the plugin's SessionStart hook must parse in sh, Git Bash, pwsh 7 and PowerShell 5.1."""

    HOOKS = REPO_ROOT / "hooks" / "hooks.json"

    def command(self):
        data = json.loads(self.HOOKS.read_text(encoding="utf-8"))
        entries = data["hooks"]["SessionStart"]
        self.assertEqual(len(entries), 1)
        hooks = entries[0]["hooks"]
        self.assertEqual(len(hooks), 1)
        return entries[0], hooks[0]

    def test_no_bash_only_syntax(self):
        entry, hook = self.command()
        text = json.dumps(hook)
        for bad in ("/dev/null", "||", "&&", "2>", "| "):
            self.assertNotIn(bad, hook.get("command", ""), bad)
        self.assertIn("--sync-install-path", text)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", text)
        self.assertEqual(entry["matcher"], "startup|resume")
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["timeout"], 15)

    def test_hook_always_ends_with_exit_zero(self):
        _, hook = self.command()
        self.assertTrue(hook["command"].rstrip().endswith("exit 0"), hook["command"])

    def test_hook_repoints_an_old_plugin_folder_in_every_shell(self):
        shells = hook_shells()
        if not shells:
            self.skipTest("no hook shell available")
        _, hook = self.command()
        for name, argv in shells.items():
            with self.subTest(shell=name), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp).resolve()
                cfg, home = tmp / "claude", tmp / "home"
                plugin_root = cfg / "plugins" / "cache" / "mk" / "usage-pulse" / "9.9.9"
                plugin_root.mkdir(parents=True)
                (plugin_root / "commands").mkdir()
                shutil.copyfile(REPO_ROOT / "claude_status.py", plugin_root / "claude_status.py")
                shutil.copyfile(REPO_ROOT / "commands" / "pulse.md", plugin_root / "commands" / "pulse.md")
                old = cfg / "plugins" / "cache" / "mk" / "usage-pulse" / "9.9.0" / "claude_status.py"
                cfg.mkdir(exist_ok=True)
                home.mkdir()
                settings = cfg / "settings.json"
                settings.write_text(json.dumps({"statusLine": {"type": "command",
                                                               "command": f"python '{old.as_posix()}'"}}),
                                    encoding="utf-8")
                py_dir = str(Path(sys.executable).parent)
                path = os.pathsep.join([py_dir, os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]
                                       if WIN else [py_dir, "/usr/bin", "/bin"])
                env = dict(os.environ, PATH=path, CLAUDE_CONFIG_DIR=str(cfg), HOME=str(home), USERPROFILE=str(home),
                           LOCALAPPDATA=str(tmp / "la"), APPDATA=str(tmp / "ra"), XDG_CACHE_HOME=str(tmp / "cache"),
                           XDG_CONFIG_HOME=str(tmp / "xdg"), PYTHONIOENCODING="utf-8")
                # Claude Code substitutes the plugin folder in its native spelling.
                command = hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root))
                r = run_in(argv, command, env)
                detail = (name, r.returncode, r.stdout[-300:], r.stderr[-500:])
                self.assertEqual(r.returncode, 0, detail)
                data = json.loads(settings.read_text(encoding="utf-8"))
                self.assertIn("9.9.9", data["statusLine"]["command"], detail)
                self.assertNotIn("9.9.0", data["statusLine"]["command"], detail)

    def test_hook_is_silent_and_exits_zero_when_python_is_missing(self):
        shells = hook_shells()
        if not shells:
            self.skipTest("no hook shell available")
        _, hook = self.command()
        for name, argv in shells.items():
            with self.subTest(shell=name), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32") if WIN else "/nonexistent"
                env = dict(os.environ, PATH=path)
                command = hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(Path(tmp) / "gone"))
                r = run_in(argv, command, env)
                self.assertEqual(r.returncode, 0, (name, r.stdout[-300:], r.stderr[-300:]))


# ---------------------------------------------------------------------------
# F20: the installers must skip a python that is only the Microsoft Store stub
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True, timeout=120)


class _InstallerSandbox(unittest.TestCase):
    """Run the real installer end to end against a local git repo and shim interpreters."""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is needed to build the local origin")
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name).resolve()
        src = self.root / "origin"
        src.mkdir()
        (src / "claude_status.py").write_text(STAND_IN, encoding="utf-8")
        _git("init", "-q", cwd=src)
        _git("add", "-A", cwd=src)
        _git("commit", "-q", "-m", "stand-in", cwd=src)
        self.origin_url = src.as_uri()
        self.shims = self.root / "shims"
        self.shims.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.install_dir = self.root / "install"
        self.cfg = self.root / "cfg"
        self.marker = self.root / "marker.json"

    def env(self):
        env = dict(os.environ)
        env.update({
            "PATH": str(self.shims) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "USAGE_PULSE_DIR": self.install_dir.as_posix(), "CLAUDE_CONFIG_DIR": self.cfg.as_posix(),
            "USAGE_PULSE_REPO_URL": self.origin_url, "USAGE_PULSE_NONINTERACTIVE": "1",
            "PULSE_TEST_MARKER": str(self.marker),
        })
        for key in ("CLAUDE_PULSE_DIR", "CLAUDE_PULSE_REPO_URL", "CLAUDE_PULSE_NONINTERACTIVE"):
            env.pop(key, None)
        return env

    def assert_setup_ran(self, result):
        detail = (result.returncode, result.stdout[-600:], result.stderr[-600:])
        self.assertEqual(result.returncode, 0, detail)
        self.assertTrue(self.marker.exists(), detail)
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8")), ["--setup", "--non-interactive"], detail)


class ShInstallerTest(_InstallerSandbox):
    """install.sh (curl | sh, and Git Bash on Windows)."""

    def setUp(self):
        super().setUp()
        self.sh = (str(Path(_git_bash()).with_name("sh.exe")) if WIN and _git_bash() else shutil.which("sh"))
        if not self.sh or not Path(self.sh).exists():
            self.skipTest("no POSIX sh available")

    def shim(self, name, body):
        path = self.shims / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def stub(self, name):
        # What the Microsoft Store leaves on PATH: present, but it only complains.
        self.shim(name, "echo 'Python was not found; run without arguments to install from the Microsoft Store' >&2\nexit 9\n")

    def works(self, name, drop_first=None):
        guard = f'[ "$1" = "{drop_first}" ] && shift\n' if drop_first else ""
        self.shim(name, f'{guard}exec "{Path(sys.executable).as_posix()}" "$@"\n')

    def install(self):
        return subprocess.run([self.sh, (REPO_ROOT / "install.sh").as_posix()], env=self.env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=240)

    def test_python3_stub_is_skipped_for_a_working_python(self):
        self.stub("python3")
        self.works("python")
        self.assert_setup_ran(self.install())

    def test_stubs_are_skipped_for_the_py_launcher(self):
        self.stub("python3")
        self.stub("python")
        self.works("py", drop_first="-3")
        self.assert_setup_ran(self.install())

    def test_a_working_python3_is_still_preferred(self):
        self.works("python3")
        self.stub("python")
        self.assert_setup_ran(self.install())


@unittest.skipUnless(WIN, "install.ps1 is the Windows installer")
class PowerShellInstallerTest(_InstallerSandbox):
    """install.ps1 (`irm | iex`) in pwsh 7 and Windows PowerShell 5.1."""

    def setUp(self):
        super().setUp()
        self.engines = {n: shutil.which(e) for n, e in (("pwsh7", "pwsh"), ("powershell5.1", "powershell"))
                        if shutil.which(e)}
        if not self.engines:
            self.skipTest("no PowerShell available")

    def stub(self, name):
        (self.shims / f"{name}.cmd").write_text(
            "@echo Python was not found; run without arguments to install from the Microsoft Store\r\n"
            "@exit /b 9009\r\n", encoding="ascii")

    def works(self, name, py_launcher=False):
        shift = "@shift\r\n" if py_launcher else ""
        (self.shims / f"{name}.cmd").write_text(
            f'@echo off\r\n{shift}"{sys.executable}" %1 %2 %3 %4 %5 %6\r\n', encoding="ascii")

    def install_in_every_engine(self):
        script = (REPO_ROOT / "install.ps1").as_posix()
        for name, exe in self.engines.items():
            # A fresh install folder and marker per engine (git's read-only files make a clean-up unreliable).
            self.install_dir = self.root / f"install-{name}"
            self.marker = self.root / f"marker-{name}.json"
            # Like `irm | iex`: the script text is evaluated, not run as a file.
            command = f"Invoke-Expression ([System.IO.File]::ReadAllText('{script}'))"
            result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", command],
                                    env=self.env(), capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", stdin=subprocess.DEVNULL, timeout=240)
            with self.subTest(engine=name):
                self.assert_setup_ran(result)

    def test_python_stub_is_skipped_for_the_py_launcher(self):
        self.stub("python")
        self.stub("python3")
        self.works("py", py_launcher=True)
        self.install_in_every_engine()

    def test_python_stub_is_skipped_for_python3(self):
        self.stub("python")
        self.stub("py")
        self.works("python3")
        self.install_in_every_engine()

    def test_a_working_python_is_used_when_nothing_else_is(self):
        self.stub("py")
        self.stub("python3")
        self.works("python")
        self.install_in_every_engine()


if __name__ == "__main__":
    unittest.main(verbosity=2)
