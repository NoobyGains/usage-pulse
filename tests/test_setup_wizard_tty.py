"""The setup wizard on a real terminal.

``tests/test_setup_wizard.py`` drives every screen through a patched
``_read_key``: that proves the wizard's logic, not the terminal. Whether the
console hands ``msvcrt.getwch`` the arrow-key prefix bytes, whether Esc leaves
a half-drawn screen cleanly, whether the apply step writes the right files
when nothing is mocked: only a real console can show that. This module runs
``claude_status.py --setup`` as a child process on a pseudo terminal, presses
the keys a person would, waits for each screen to appear, and checks the
files the wizard leaves behind in a sandbox.

Windows: ConPTY through pywinpty, the console behind Windows Terminal and
PowerShell. ``pip install pywinpty``; skipped without it.
POSIX: the stdlib ``pty``. That transport has not been run by the maintainer
yet, so it is opt-in: ``USAGE_PULSE_TTY_TESTS=1``.

Run: ``python tests/test_setup_wizard_tty.py``
"""
import codecs
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "claude_status.py"
PULSE_SRC = REPO_ROOT / "commands" / "pulse.md"
WIN = sys.platform == "win32"

if WIN:
    try:
        import winpty  # noqa: F401
        HAVE_PTY, SKIP_REASON = True, ""
    except ImportError:
        HAVE_PTY, SKIP_REASON = False, "needs pywinpty on Windows: pip install pywinpty"
else:
    HAVE_PTY = os.environ.get("USAGE_PULSE_TTY_TESTS", "").strip() not in ("", "0")
    SKIP_REASON = "the POSIX pty transport is opt-in: USAGE_PULSE_TTY_TESTS=1"

# What a terminal sends for each key.
ENTER, ESC, BACKSPACE = "\r", "\x1b", "\x7f"
UP, DOWN, LEFT = "\x1b[A", "\x1b[B", "\x1b[D"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI (colours, cursor, clear)
                   r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (window title)
                   r"|\x1b[()][A-Za-z0-9]|\x1b[=>]")   # charset / keypad modes


def _plain(raw):
    """Terminal output as the words a person reads: no escapes, single spaces."""
    text = _ANSI.sub("", raw).replace("\r", "")
    return re.sub(r"[ \t]+", " ", text)


class _ConPty:
    """A child on a Windows ConPTY (pywinpty)."""

    def __init__(self, argv, cwd, env, rows=40, cols=100):
        import winpty
        self.proc = winpty.PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))

    def read(self):
        return self.proc.read(4096)       # str; raises EOFError when the child is gone

    def write(self, keys):
        self.proc.write(keys)

    def alive(self):
        return self.proc.isalive()

    def exit_code(self):
        return self.proc.exitstatus

    def close(self):
        try:
            # pywinpty 3.1 marks an exited child "closed" in isalive() without
            # closing its bridge sockets; clearing the flag makes close() do it.
            self.proc.closed = False
            self.proc.close(force=True)
        except Exception:
            pass


class _PosixPty:
    """A child on a POSIX pseudo terminal (stdlib pty)."""

    def __init__(self, argv, cwd, env, rows=40, cols=100):
        import fcntl
        import pty
        import struct
        import termios
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                     stderr=slave, start_new_session=True, close_fds=True)
        os.close(slave)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def read(self):
        try:
            data = os.read(self.master, 4096)
        except OSError:            # EIO once the child has closed its side
            raise EOFError
        if not data:
            raise EOFError
        return self._decoder.decode(data)

    def write(self, keys):
        os.write(self.master, keys.encode("utf-8"))

    def alive(self):
        return self.proc.poll() is None

    def exit_code(self):
        return self.proc.poll()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)
        try:
            os.close(self.master)
        except OSError:
            pass


class _Wizard:
    """``--setup`` running on a pseudo terminal inside a sandboxed home."""

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.cfg = self.home / ".claude"
        self.cfg.mkdir(parents=True)
        env = dict(os.environ)
        # Every path the script may touch lives under the sandbox: home,
        # config dir, state dir (LOCALAPPDATA / XDG), and the other tools it
        # looks for. The developer's real accounts must never be read or written.
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "LOCALAPPDATA": str(self.root / "local"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "CLAUDE_CONFIG_DIR": str(self.cfg),
            "CODEX_HOME": str(self.root / "codex"),
            "COLUMNS": "100", "LINES": "40",
            "PYTHONIOENCODING": "utf-8",
        })
        for key in ("USAGE_PULSE_NONINTERACTIVE", "CLAUDE_PULSE_NONINTERACTIVE",
                    "MSYSTEM", "TERM_PROGRAM", "HOMEDRIVE", "HOMEPATH"):
            env.pop(key, None)
        argv = [sys.executable, str(SCRIPT), "--setup"]
        self.pty = (_ConPty if WIN else _PosixPty)(argv, str(self.root), env)
        self._chunks = []
        self._lock = threading.Lock()
        self._pump = threading.Thread(target=self._read_forever, daemon=True)
        self._pump.start()

    def _read_forever(self):
        while True:
            try:
                chunk = self.pty.read()
            except (EOFError, OSError):
                return
            except Exception:
                return
            with self._lock:
                self._chunks.append(chunk)

    def raw(self):
        with self._lock:
            return "".join(self._chunks)

    def text(self):
        return _plain(self.raw())

    def expect(self, needle, count=1, timeout=20):
        """Wait until *needle* has appeared *count* times; return the text so far."""
        end = time.time() + timeout
        while True:
            text = self.text()
            if text.count(needle) >= count:
                return text
            if not self.pty.alive():
                self._pump.join(1.0)
                text = self.text()
                if text.count(needle) >= count:
                    return text
                raise AssertionError(f"the wizard exited before showing {needle!r} "
                                     f"(x{count}); output ends:\n{text[-1500:]}")
            if time.time() > end:
                raise AssertionError(f"timed out waiting for {needle!r} (x{count}); "
                                     f"output ends:\n{text[-1500:]}")
            time.sleep(0.05)

    def send(self, keys):
        self.pty.write(keys)

    def wait(self, timeout=30):
        end = time.time() + timeout
        while self.pty.alive() and time.time() < end:
            time.sleep(0.05)
        if self.pty.alive():
            raise AssertionError("the wizard did not exit; output ends:\n" + self.text()[-1500:])
        self._pump.join(2.0)
        return self.pty.exit_code()

    def close(self):
        self.pty.close()

    # -- what the wizard may have written ------------------------------------
    @property
    def settings(self):
        return self.cfg / "settings.json"

    @property
    def pulse_command(self):
        return self.cfg / "commands" / "pulse.md"

    def config_file(self):
        hits = [p for p in self.root.rglob("config.json") if p.parent.name == "claude-status"]
        return hits[0] if hits else None


class _RecordingPty:
    """Stands in for the pseudo terminal: remembers the environment it was
    given and ends immediately, so no child process is started."""
    started_with = []

    def __init__(self, argv, cwd, env):
        _RecordingPty.started_with.append(env)

    def read(self):
        raise EOFError

    def close(self):
        pass


class WizardChildEnvironmentTest(unittest.TestCase):
    """Runs without a pseudo terminal: only the environment handed to the child."""

    def test_the_child_never_inherits_a_name_that_forces_unattended_setup(self):
        """cmd_setup treats USAGE_PULSE_NONINTERACTIVE (the README name) and the
        older CLAUDE_PULSE_NONINTERACTIVE alike. A child that inherits either
        runs unattended, and every screen the test waits for never appears."""
        names = ("USAGE_PULSE_NONINTERACTIVE", "CLAUDE_PULSE_NONINTERACTIVE")
        _RecordingPty.started_with = []
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, {name: "1" for name in names}), \
                mock.patch.object(sys.modules[__name__], "_ConPty" if WIN else "_PosixPty",
                                  _RecordingPty):
            _Wizard(root)
        self.assertEqual(len(_RecordingPty.started_with), 1)
        for name in names:
            self.assertNotIn(name, _RecordingPty.started_with[0])


@unittest.skipUnless(HAVE_PTY, SKIP_REASON)
class SetupWizardOnATerminalTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="pulse-tty-")
        self.addCleanup(self._td.cleanup)
        self.w = _Wizard(self._td.name)
        self.addCleanup(self.w.close)

    def test_enter_through_the_defaults_installs_everything(self):
        w = self.w
        text = w.expect("Step 0/13 · Welcome")
        self.assertIn("settings.json not there yet", text, "the environment report")
        w.send(ENTER)
        w.expect("Step 1/13 · Status line")
        w.send(ENTER)
        text = w.expect("Step 2/13 · Hooks")
        self.assertGreaterEqual(text.count("[x]"), 2, "agents and team are ticked by default")
        w.send(ENTER)
        text = w.expect("Step 4/13 · Model caps")
        self.assertNotIn("Step 3/13", text, "no providers in the sandbox: that screen is skipped")
        for step in ("Step 5/13 · Theme", "Step 6/13 · Bar size", "Step 7/13 · Bar style",
                     "Step 8/13 · Layout", "Step 9/13 · Currency", "Step 10/13 · Clock",
                     "Step 11/13 · Effort", "Step 12/13 · Widgets", "Step 13/13 · Summary"):
            w.send(ENTER)
            w.expect(step)
        w.send(ENTER)                       # Apply
        w.expect("Done.")
        self.assertEqual(w.wait(), 0)

        data = json.loads(w.settings.read_text(encoding="utf-8"))
        self.assertEqual(data["statusLine"]["type"], "command")
        self.assertIn("claude_status.py", data["statusLine"]["command"])
        self.assertIn("--subagent-status-line", data["subagentStatusLine"]["command"])
        for event in ("SubagentStart", "SubagentStop", "TeammateIdle", "TaskCreated", "TaskCompleted"):
            self.assertIn(event, data.get("hooks", {}), f"default hook groups install {event}")
        self.assertEqual(w.pulse_command.read_text(encoding="utf-8"),
                         PULSE_SRC.read_text(encoding="utf-8"))
        self.assertIsNotNone(w.config_file(), "config.json was saved")

    def test_arrow_keys_back_and_escape_change_nothing(self):
        w = self.w
        w.expect("Step 0/13 · Welcome")
        w.send(ENTER)
        w.expect("Step 1/13 · Status line")
        w.send(DOWN)
        w.expect("▸ No")                    # the console turned the arrow sequence into a key
        w.send(LEFT)
        w.expect("Step 0/13 · Welcome", count=2)
        w.send(ENTER)
        w.expect("Step 1/13 · Status line", count=2)
        w.send(ESC)
        w.expect("Nothing changed.")
        self.assertEqual(w.wait(), 0)
        self.assertFalse(w.settings.exists(), "Esc must not write settings.json")
        self.assertFalse(w.pulse_command.exists(), "Esc must not install /pulse")

    def test_picked_options_and_typed_text_are_saved(self):
        w = self.w
        w.expect("Step 0/13 · Welcome")
        w.send(ENTER)
        w.expect("Step 1/13 · Status line")
        w.send(ENTER)
        w.expect("Step 2/13 · Hooks")
        w.send("n")                         # untick every hook group
        w.send(ENTER)
        w.expect("Step 4/13 · Model caps")
        w.send(ENTER)
        w.expect("Step 5/13 · Theme")
        w.send(DOWN + ENTER)                # default → ocean
        w.expect("Step 6/13 · Bar size")
        w.send(UP + ENTER)                  # large → medium-large (the list wraps)
        w.expect("Step 7/13 · Bar style")
        w.send(DOWN + ENTER)                # classic → block
        w.expect("Step 8/13 · Layout")
        w.send(ENTER)
        w.expect("Step 9/13 · Currency")
        w.send(UP + ENTER)                  # the last entry is "Other…"
        w.expect("Currency symbol")
        w.send("R")
        w.send("x")
        w.send(BACKSPACE)
        w.send("p")
        w.expect("Rp")                      # typed, corrected, echoed
        w.send(ENTER)
        w.expect("Step 10/13 · Clock")
        w.send(DOWN + ENTER)                # 12h → 24h
        w.expect("Step 11/13 · Effort")
        w.send(ENTER)
        w.expect("Step 12/13 · Widgets")
        w.send(ENTER)
        w.expect("Step 13/13 · Summary")
        w.send(ENTER)
        w.expect("Done.")
        self.assertEqual(w.wait(), 0)

        cfg = json.loads(w.config_file().read_text(encoding="utf-8"))
        self.assertEqual(cfg["theme"], "ocean")
        self.assertEqual(cfg["bar_size"], "medium-large")
        self.assertEqual(cfg["bar_style"], "block")
        self.assertEqual(cfg["currency"], "Rp")
        self.assertEqual(cfg["clock_format"], "24h")
        data = json.loads(w.settings.read_text(encoding="utf-8"))
        self.assertIn("claude_status.py", data["statusLine"]["command"])
        for event in ("SubagentStart", "TeammateIdle", "TaskCreated"):
            self.assertNotIn(event, data.get("hooks", {}), "no hook group was ticked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
