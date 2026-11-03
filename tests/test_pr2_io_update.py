"""Regression tests for the I/O, hook, install and update fixes (F03, F21, F23, F24, F25, F37, F38).

* F03  stdin is read as bytes and decoded as UTF-8 everywhere. A Windows pipe
       decodes with the ANSI code page, which garbled non-ASCII names and left
       lone surrogates that blanked the bar.
* F21  the first bar draw must not delete the heartbeat hook setup installed.
* F23  ``_atomic_json_write`` retries ``os.replace`` on Windows when a scanner
       or another process briefly holds the destination open.
* F24  ``--update`` never discards local work: it refuses a dirty tree or a
       side branch, reports a checkout that is ahead of main, pulls with
       ``--ff-only`` and rolls back with ``reset --keep``, never ``--hard``.
* F25  ``--uninstall`` removes what usage-pulse added to Claude Code's config.
* F37  background settings.json rewrites keep a symlinked file a symlink and
       keep non-ASCII text readable.
* F38  ``--update`` refreshes the installed ``/pulse`` command.

Every test runs in a temp dir; nothing here touches the real home, the real
Claude settings or the network.

Run: ``python -m unittest tests.test_pr2_io_update``
"""
import contextlib
import importlib.util
import io
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The untouched resolver, for the one test that checks CLAUDE_CONFIG_DIR is honoured.
REAL_CONFIG_DIR = cs._claude_config_dir

# Text that is garbled or poisoned by a Windows ANSI-code-page decode of UTF-8:
# "é" turns into two Latin-1 characters; the Cyrillic and CJK samples contain
# bytes (0x9D, 0x8D ...) that cp1252 leaves undefined.
CAFE = "Café résumé"
CYRILLIC = "Новый"
CJK = "修复登录"
EMOJI = "Notes \U0001F4DD cleanup"


def windows_pipe(payload):
    """``sys.stdin`` as Python builds it for a pipe on Windows.

    The ANSI code page (cp1252 here) with ``surrogateescape``: bytes the page
    does not define come through as lone surrogates. ``.buffer`` is the raw
    byte stream a careful reader can use instead.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252", errors="surrogateescape")


class _Out:
    """A stand-in for sys.stdout that records what is written to ``.buffer``."""

    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, _text):
        pass

    def flush(self):
        pass

    def text(self):
        return self.buffer.getvalue().decode("utf-8", "replace")


class SandboxCase(unittest.TestCase):
    """Point every path usage-pulse uses at a temp dir; block the network."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        self.home = self.root / "home"
        self.cfg_dir = self.root / "claude"
        self.state = self.root / "state"
        self.user_config = self.root / "config.json"
        for d in (self.home, self.cfg_dir, self.state):
            d.mkdir()
        self.settings = self.cfg_dir / "settings.json"
        self.network = []

        def no_network(*args, **kwargs):
            self.network.append(args[:1])
            raise OSError("network disabled in tests")

        env = {"HOME": str(self.home), "USERPROFILE": str(self.home),
               "LOCALAPPDATA": str(self.root / "local"),
               "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
               "XDG_CONFIG_HOME": str(self.root / "xdg-config")}
        envp = mock.patch.dict(os.environ, env)
        envp.start()
        self.addCleanup(envp.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        patches = [
            mock.patch.dict(cs._own_stamps, {}, clear=True),
            mock.patch.object(cs, "_claude_config_dir", lambda: self.cfg_dir),
            mock.patch.object(cs, "get_state_dir", lambda: self.state),
            mock.patch.object(cs, "get_config_path", lambda: self.user_config),
            mock.patch.object(cs, "check_for_update", lambda *a, **k: False),
            mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: False),
            mock.patch.object(cs, "get_credentials", lambda: ("fake-token", "Max 20x")),
            mock.patch.object(cs.socket, "getaddrinfo", no_network),
            mock.patch.object(cs.socket, "create_connection", no_network),
            mock.patch.object(cs.urllib.request, "urlopen", no_network),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        cs._settings_backed_up.clear()
        self.addCleanup(cs._settings_backed_up.clear)

    def tearDown(self):
        self.assertEqual(self.network, [], "a test reached for the real network")

    def printing(self):
        """Capture utf8_print; returns the list lines are appended to."""
        lines = []
        p = mock.patch.object(cs, "utf8_print", lambda *a, **k: lines.append(" ".join(str(x) for x in a)))
        p.start()
        self.addCleanup(p.stop)
        return lines

    def settings_data(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    @staticmethod
    def rate_limits():
        now = time.time()
        return {"five_hour": {"used_percentage": 15, "resets_at": now + 10800},
                "seven_day": {"used_percentage": 66, "resets_at": now + 604800}}


# ---------------------------------------------------------------------------
# F03: stdin is UTF-8 bytes, never the Windows ANSI code page
# ---------------------------------------------------------------------------

class StdinDecodingTest(SandboxCase):

    def repaint(self, stdin):
        out = _Out()
        with mock.patch.object(cs.sys, "argv", ["claude_status.py"]), \
                mock.patch.object(cs.sys, "stdin", stdin), \
                mock.patch.object(cs.sys, "stdout", out):
            try:
                cs.main()
            except Exception as exc:  # noqa: BLE001 - this IS the blank bar
                self.fail("main() raised %s: %s -- Claude Code shows a blank bar"
                          % (type(exc).__name__, str(exc)[:90]))
        return ANSI.sub("", out.text()).strip()

    def payload(self, **extra):
        data = {"session_id": "s1", "model": {"display_name": "Opus 5.5"},
                "rate_limits": self.rate_limits()}
        data.update(extra)
        return json.dumps(data, ensure_ascii=False)

    def test_accented_worktree_name_is_not_garbled(self):
        line = self.repaint(windows_pipe(self.payload(worktree={"name": CAFE})))
        self.assertIn(CAFE, line)
        self.assertNotIn("Ã", line, "UTF-8 bytes were decoded with the ANSI code page")

    def test_cyrillic_worktree_name_does_not_blank_the_bar(self):
        line = self.repaint(windows_pipe(self.payload(worktree={"name": CYRILLIC})))
        self.assertIn(CYRILLIC, line)

    def test_cjk_and_emoji_session_names_do_not_blank_the_bar(self):
        show = {"session_name": True}
        for name in (CJK, EMOJI):
            with self.subTest(name=name):
                self.user_config.write_text(json.dumps({"show": show}), encoding="utf-8")
                line = self.repaint(windows_pipe(self.payload(session_name=name)))
                self.assertIn(name, line)

    def test_saved_context_holds_clean_text(self):
        self.repaint(windows_pipe(self.payload(worktree={"name": CYRILLIC})))
        saved = cs.get_account_state_dir() / "stdin_ctx.json"
        text = saved.read_text(encoding="utf-8")
        self.assertEqual(json.loads(text)["worktree_branch"], CYRILLIC)
        self.assertNotRegex(text, r"\\ud[89a-f][0-9a-f]{2}", "a lone surrogate was saved")

    def test_a_surrogate_saved_by_an_older_version_stops_blanking_the_bar(self):
        """Versions up to 3.6.2 persisted lone surrogates in stdin_ctx.json."""
        saved = cs.get_account_state_dir() / "stdin_ctx.json"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text('{"worktree_branch": "bad-\\udc9d-name", "session_id": "s1"}',
                         encoding="utf-8")
        # A fragment repaint (no session identity): the saved worktree is drawn.
        line = self.repaint(io.StringIO(json.dumps({"rate_limits": self.rate_limits()})))
        self.assertIn("bad-", line)

    def test_bytes_that_are_not_utf8_are_replaced_not_fatal(self):
        raw = self.payload(worktree={"name": "x"}).encode("utf-8").replace(b'"x"', b'"a\xffb"')
        line = self.repaint(windows_pipe(raw))
        self.assertIn("a", line)

    def test_a_utf8_byte_order_mark_is_ignored(self):
        raw = b"\xef\xbb\xbf" + self.payload(worktree={"name": CAFE}).encode("utf-8")
        line = self.repaint(windows_pipe(raw))
        self.assertIn(CAFE, line)

    def test_text_only_stdin_still_works(self):
        """A stream without .buffer (or a mock) falls back to the text read."""
        line = self.repaint(io.StringIO(self.payload(worktree={"name": CAFE})))
        self.assertIn(CAFE, line)

    def test_hook_refresh_reads_the_tool_name_as_utf8(self):
        name = "mcp__café__lookup"
        stdin = windows_pipe(json.dumps({"tool_name": name}, ensure_ascii=False))
        with mock.patch.object(cs.sys, "stdin", stdin), \
                mock.patch.object(cs, "sync_status_line_refresh", lambda *a, **k: None):
            cs.hook_refresh("")
        state = json.loads((self.state / "hook_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_tool"], name)

    def test_hook_subagent_reads_the_agent_type_as_utf8(self):
        stdin = windows_pipe(json.dumps({"session_id": "s1", "agent_id": "a1",
                                         "agent_type": "Café-reviewer"}, ensure_ascii=False))
        with mock.patch.object(cs.sys, "stdin", stdin):
            cs.hook_subagent("start")
        marker = next(self.state.rglob("a1.live"))
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["type"], "Café-reviewer")

    def test_subagent_status_line_reads_task_names_as_utf8(self):
        payload = {"columns": 100, "tasks": [{"id": "a", "name": CAFE, "status": "running"}]}
        out = _Out()
        with mock.patch.object(cs.sys, "stdin", windows_pipe(json.dumps(payload, ensure_ascii=False))), \
                mock.patch.object(cs.sys, "stdout", out):
            cs.cmd_subagent_status_line()
        rows = [json.loads(line) for line in out.text().splitlines() if line]
        self.assertEqual(len(rows), 1)
        self.assertIn(CAFE, ANSI.sub("", rows[0]["content"]))

    def test_debug_stdin_saves_and_parses_utf8(self):
        printed = self.printing()
        stdin = windows_pipe(self.payload(worktree={"name": CAFE}))
        with mock.patch.object(cs.sys, "argv", ["claude_status.py", "--debug-stdin"]), \
                mock.patch.object(cs.sys, "stdin", stdin):
            cs.main()
        saved = (self.state / "stdin_debug.json").read_text(encoding="utf-8")
        self.assertIn(CAFE, saved)
        self.assertIn(CAFE, "\n".join(printed).replace("\\u00e9", "é"))


@unittest.skipUnless(sys.platform == "win32", "the ANSI code page decode is a Windows behaviour")
@unittest.skipIf(locale.getpreferredencoding(False).lower().replace("-", "") in ("utf8", "cp65001"),
                 "the ANSI code page is already UTF-8 on this machine")
class WindowsPipeProcessTest(unittest.TestCase):
    """The real script, a real pipe, UTF-8 bytes that contain 0x8D / 0x9D."""

    def test_bytes_with_undefined_cp1252_values_still_draw_the_bar(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home, cfg = root / "home", root / "claude"
            local = root / "local"
            for d in (home, cfg, local / "claude-status"):
                d.mkdir(parents=True)
            (local / "claude-status" / "config.json").write_text(
                json.dumps({"show": {"update": False, "claude_update": False}}), encoding="utf-8")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "CLAUDE_CONFIG_DIR")}
            env.update({"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(local),
                        "APPDATA": str(root / "appdata"), "XDG_CACHE_HOME": str(root / "xc"),
                        "XDG_CONFIG_HOME": str(root / "xf"), "CLAUDE_CONFIG_DIR": str(cfg),
                        "PYTHONUTF8": "0",
                        # belt and braces: nothing in this process may leave the machine
                        "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"})
            now = time.time()
            payload = json.dumps({
                "session_id": "s1", "model": {"display_name": "Opus 5.5"},
                "worktree": {"name": CYRILLIC},
                "rate_limits": {"five_hour": {"used_percentage": 15, "resets_at": now + 10800},
                                "seven_day": {"used_percentage": 66, "resets_at": now + 604800}},
            }, ensure_ascii=False).encode("utf-8")
            self.assertIn(b"\x9d", payload, "the sample must hold a byte cp1252 leaves undefined")
            r = subprocess.run([sys.executable, str(REPO_ROOT / "claude_status.py")],
                               input=payload, capture_output=True, env=env, cwd=str(root), timeout=60)
            out = ANSI.sub("", r.stdout.decode("utf-8", "replace")).strip()
            self.assertIn(CYRILLIC, out, f"bar was blank or garbled: {out!r} stderr={r.stderr[-300:]!r}")


# ---------------------------------------------------------------------------
# F21: the first draw must not delete the heartbeat hook
# ---------------------------------------------------------------------------

class CleanupHooksTest(SandboxCase):

    def hooks(self):
        return self.settings_data().get("hooks", {})

    def test_the_heartbeat_hook_survives_the_first_draw(self):
        self.printing()
        self.assertTrue(cs.install_hooks({"heartbeat", "agents"}))
        cs._cleanup_hooks()
        post = self.hooks().get("PostToolUse", [])
        self.assertTrue(any(cs._is_pulse_hook_entry(e, "--hook-refresh") for e in post),
                        f"the heartbeat hook was deleted: {post}")
        self.assertIn(cs.HOOK_GROUP_NAMES[0], cs._installed_pulse_hooks(self.settings_data()))
        self.assertEqual(cs._installed_pulse_hooks(self.settings_data()), {"heartbeat", "agents"})

    def test_every_current_hook_survives(self):
        self.printing()
        self.assertTrue(cs.install_hooks())
        before = self.settings_data()["hooks"]
        cs._cleanup_hooks()
        self.assertEqual(self.settings_data()["hooks"], before)

    def test_legacy_hooks_are_still_removed_and_foreign_ones_kept(self):
        legacy = {"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command",
                                             "command": 'python "/x/claude_status.py" --animate-tick'}]}],
            "PreToolUse": [{"command": "python /x/claude_status.py --old"}],
            "PostToolUse": [
                {"matcher": "", "hooks": [{"type": "command",
                                           "command": 'python "/x/claude_status.py" --old-hook'}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter --check"}]},
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        }}
        self.settings.write_text(json.dumps(legacy), encoding="utf-8")
        cs._cleanup_hooks()
        hooks = self.hooks()
        self.assertNotIn("UserPromptSubmit", hooks)
        self.assertNotIn("PreToolUse", hooks)
        self.assertEqual([e["hooks"][0]["command"] for e in hooks["PostToolUse"]], ["my-linter --check"])
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["command"], "notify-send done")

    def test_the_marker_is_written_so_it_runs_once(self):
        self.settings.write_text("{}", encoding="utf-8")
        cs._cleanup_hooks()
        self.assertTrue((self.state / "hooks_cleaned").exists())


# ---------------------------------------------------------------------------
# F23 + F37: the atomic writer
# ---------------------------------------------------------------------------

class AtomicWriteRetryTest(SandboxCase):

    def setUp(self):
        super().setUp()
        self.target = self.root / "out" / "config.json"
        self.target.parent.mkdir()
        sleeps = []
        p = mock.patch.object(cs.time, "sleep", lambda s: sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)
        self.sleeps = sleeps

    def flaky_replace(self, failures):
        real = os.replace
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError(13, "Access is denied", dst)
            return real(src, dst)
        return replace, calls

    def test_a_brief_sharing_violation_is_retried(self):
        replace, calls = self.flaky_replace(2)
        with mock.patch.object(cs.sys, "platform", "win32"), \
                mock.patch.object(cs.os, "replace", replace):
            cs._atomic_json_write(self.target, {"a": 1})
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual(calls["n"], 3)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["config.json"])

    def test_it_gives_up_after_a_bounded_wait_and_cleans_up(self):
        replace, calls = self.flaky_replace(10 ** 6)
        with mock.patch.object(cs.sys, "platform", "win32"), \
                mock.patch.object(cs.os, "replace", replace):
            with self.assertRaises(PermissionError):
                cs._atomic_json_write(self.target, {"a": 1})
        self.assertGreater(calls["n"], 3)
        self.assertLess(calls["n"], 30)
        self.assertLess(sum(self.sleeps), 1.5, "a repaint must not hang on a locked file")
        self.assertEqual(list(self.target.parent.iterdir()), [], "the temp file was left behind")

    def test_other_errors_are_not_retried(self):
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            raise OSError("disk full")
        with mock.patch.object(cs.sys, "platform", "win32"), mock.patch.object(cs.os, "replace", replace):
            with self.assertRaises(OSError):
                cs._atomic_json_write(self.target, {"a": 1})
        self.assertEqual(calls["n"], 1)


class AtomicWriteContentTest(SandboxCase):

    def setUp(self):
        super().setUp()
        self.target = self.root / "settings.json"

    def test_non_ascii_text_stays_readable(self):
        data = {"env": {"GREETING": "Grüße – café"}}
        cs._atomic_json_write(self.target, data)
        raw = self.target.read_text(encoding="utf-8")
        self.assertIn("Grüße – café", raw)
        self.assertNotIn("\\u", raw)
        self.assertEqual(json.loads(raw), data)

    def test_a_lone_surrogate_is_kept_as_an_escape_not_a_crash(self):
        cs._atomic_json_write(self.target, {"a": "x\udc9dy", "b": "café"})
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8")),
                         {"a": "x\udc9dy", "b": "café"})
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_compact_output_keeps_its_shape(self):
        cs._atomic_json_write(self.target, {"a": 1, "b": [1, 2]}, indent=None)
        self.assertEqual(self.target.read_text(encoding="utf-8"), '{"a": 1, "b": [1, 2]}')

    def test_a_symlinked_file_stays_a_symlink(self):
        """Stow / home-manager dotfile setups: the link must survive the rewrite.

        Real symlinks need a privilege on Windows, so the link is simulated:
        the writer is told that ``link`` is a symlink to ``real``."""
        real = self.root / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"old": true}', encoding="utf-8")
        link = self.root / "linkdir" / "settings.json"
        link.parent.mkdir()
        link.write_text("placeholder that must be left alone", encoding="utf-8")
        real_islink, real_realpath = os.path.islink, os.path.realpath

        def islink(p):
            return Path(p) == link or real_islink(p)

        def realpath(p, *a, **k):
            return str(real) if Path(p) == link else real_realpath(p, *a, **k)
        with mock.patch.object(cs.os.path, "islink", islink), \
                mock.patch.object(cs.os.path, "realpath", realpath):
            cs._atomic_json_write(link, {"new": True})
        self.assertEqual(json.loads(real.read_text(encoding="utf-8")), {"new": True})
        self.assertEqual(link.read_text(encoding="utf-8"), "placeholder that must be left alone")
        self.assertEqual(sorted(p.name for p in real.parent.iterdir()), ["settings.json"])

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlinks")
    def test_a_real_symlink_survives_when_the_platform_allows_it(self):
        real = self.root / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = self.root / "link.json"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks needs a privilege here")
        cs._atomic_json_write(link, {"n": 1})
        self.assertTrue(os.path.islink(link))
        self.assertEqual(json.loads(real.read_text(encoding="utf-8")), {"n": 1})


class SyncRefreshRewriteTest(SandboxCase):

    def test_a_timer_flip_keeps_non_ascii_settings_readable(self):
        ours = {"type": "command", "command": 'python "/x/claude_status.py"'}
        self.settings.write_text(json.dumps(
            {"env": {"GREETING": "Grüße – café"}, "statusLine": ours},
            ensure_ascii=False), encoding="utf-8")
        self.assertEqual(cs.sync_status_line_refresh({"animate": "rainbow"}), cs.REFRESH_ANIMATED)
        raw = self.settings.read_text(encoding="utf-8")
        self.assertIn("Grüße – café", raw)
        self.assertNotIn("\\u", raw)
        self.assertEqual(json.loads(raw)["statusLine"]["refreshInterval"], cs.REFRESH_ANIMATED)

    def test_a_timer_flip_writes_through_a_symlink(self):
        # Simulated link: reading ``self.settings`` gives the same JSON the real
        # file holds, as a real symlink would; only the real file may change.
        real = self.root / "dotfiles.json"
        before = json.dumps({"statusLine": {"type": "command",
                                            "command": 'python "/x/claude_status.py"'}})
        real.write_text(before, encoding="utf-8")
        self.settings.write_text(before, encoding="utf-8")
        real_islink, real_realpath = os.path.islink, os.path.realpath
        with mock.patch.object(cs.os.path, "islink",
                               lambda p: Path(p) == self.settings or real_islink(p)), \
                mock.patch.object(cs.os.path, "realpath",
                                  lambda p, *a, **k: str(real) if Path(p) == self.settings
                                  else real_realpath(p, *a, **k)):
            cs.sync_status_line_refresh({"animate": "rainbow"})
        self.assertEqual(json.loads(real.read_text(encoding="utf-8"))["statusLine"]["refreshInterval"],
                         cs.REFRESH_ANIMATED)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before,
                         "the link itself was replaced by a rewritten plain file")


# ---------------------------------------------------------------------------
# F24 + F38: --update, against a real (local) git repository
# ---------------------------------------------------------------------------

def _run_git(*args, cwd):
    """Run git in ``cwd`` and return its stripped stdout; a failure fails the test."""
    r = subprocess.run([cs._GIT_PATH, *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stdout}{r.stderr}")
    return r.stdout.strip()


class UpdateRepoCase(SandboxCase):
    """An install clone of a local 'origin'; the GitHub lookups are stubbed.

    The three repositories (bare origin, the maintainer's seed clone, the
    user's install clone, all at commit A) are built once per class and copied
    for each test: git is slow to start on Windows.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(template.cleanup)
        root = Path(template.name).resolve()
        gitconfig = root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n"
                             "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
                             "[core]\n\tautocrlf = false\n", encoding="utf-8")
        envp = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(gitconfig),
                                            "GIT_CONFIG_NOSYSTEM": "1",
                                            "GIT_TERMINAL_PROMPT": "0"})
        envp.start()
        cls.addClassCleanup(envp.stop)
        origin, seed, install = root / "origin", root / "seed", root / "install"
        _run_git("init", "--bare", "-b", "main", str(origin), cwd=root)
        _run_git("init", "-b", "main", str(seed), cwd=root)
        shutil.copyfile(REPO_ROOT / "claude_status.py", seed / "claude_status.py")
        (seed / "config.json").write_text("{}", encoding="utf-8")
        (seed / "commands").mkdir()
        (seed / "commands" / "pulse.md").write_text("pulse command OLD\n", encoding="utf-8")
        _run_git("add", "-A", cwd=seed)
        _run_git("commit", "-m", "A", cwd=seed)
        _run_git("remote", "add", "origin", origin.as_posix(), cwd=seed)
        _run_git("push", "origin", "main", cwd=seed)
        cls.sha_a = _run_git("rev-parse", "HEAD", cwd=seed)
        _run_git("clone", origin.as_posix(), str(install), cwd=root)
        cls.template_root = root

    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin"
        self.seed = self.root / "seed"
        self.install = self.root / "install"
        for repo in (self.origin, self.seed, self.install):
            shutil.copytree(self.template_root / repo.name, repo)
        self.git("remote", "set-url", "origin", self.origin.as_posix(), cwd=self.seed)
        self.git("remote", "set-url", "origin", self.origin.as_posix(), cwd=self.install)

        spec = importlib.util.spec_from_file_location("claude_status_install_copy",
                                                      self.install / "claude_status.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        for name, value in (("_claude_config_dir", lambda: self.cfg_dir),
                            ("get_state_dir", lambda: self.state),
                            ("get_config_path", lambda: self.user_config),
                            ("GITHUB_REPO_ALIASES", ("origin",)),
                            ("_fetch_remote_version", lambda: "9.9.9")):
            p = mock.patch.object(self.mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.remote_sha = None
        p = mock.patch.object(self.mod, "get_remote_commit", lambda *a, **k: self.remote_sha)
        p.start()
        self.addCleanup(p.stop)
        self.out = []
        p = mock.patch.object(self.mod, "utf8_print",
                              lambda *a, **k: self.out.append(" ".join(str(x) for x in a)))
        p.start()
        self.addCleanup(p.stop)

    # -- git helpers ---------------------------------------------------------
    def git(self, *args, cwd):
        return _run_git(*args, cwd=cwd)

    def rev(self, cwd):
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def publish_new_commit(self, pulse_text="pulse command NEW\n"):
        """Upstream main moves on: commit C changes commands/pulse.md."""
        (self.seed / "commands" / "pulse.md").write_text(pulse_text, encoding="utf-8")
        self.git("commit", "-am", "C", cwd=self.seed)
        self.git("push", "origin", "main", cwd=self.seed)
        self.remote_sha = self.rev(self.seed)
        return self.remote_sha

    def local_commit(self, name="local.txt"):
        (self.install / name).write_text("my own work\n", encoding="utf-8")
        self.git("add", name, cwd=self.install)
        self.git("commit", "-m", "L " + name, cwd=self.install)
        return self.rev(self.install)

    def run_update(self, confirm=True):
        argv = ["claude_status.py", "--update"] + (["--confirm"] if confirm else [])
        calls = []
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            calls.append(list(cmd))
            return real_run(cmd, *a, **k)
        with mock.patch.object(sys, "argv", argv), mock.patch.object(self.mod.subprocess, "run", spy):
            self.mod.cmd_update()
        self.git_calls = calls
        return "\n".join(self.out)

    def git_calls_with(self, word):
        return [c for c in self.git_calls if word in c]


class UpdateSafetyTest(UpdateRepoCase):

    def test_a_checkout_that_is_ahead_keeps_its_uncommitted_edit(self):
        """The reported data loss: local commit, uncommitted edit, --update --confirm."""
        c = self.publish_new_commit()
        self.git("pull", "origin", "main", cwd=self.install)     # the install already has C
        mine = self.local_commit()
        (self.install / "config.json").write_text("UNCOMMITTED LOCAL EDIT\n", encoding="utf-8")
        self.remote_sha = c
        text = self.run_update()
        self.assertEqual((self.install / "config.json").read_text(encoding="utf-8"),
                         "UNCOMMITTED LOCAL EDIT\n", "the uncommitted edit was wiped")
        self.assertEqual(self.rev(self.install), mine)
        self.assertEqual(self.git_calls_with("reset"), [])

    def test_a_checkout_that_is_ahead_is_not_reported_as_an_update(self):
        c = self.publish_new_commit()
        self.git("pull", "origin", "main", cwd=self.install)
        mine = self.local_commit()
        self.remote_sha = c
        text = self.run_update()
        self.assertNotIn("Update found", text)
        self.assertIn("ahead", text)
        self.assertEqual(self.rev(self.install), mine)
        self.assertEqual(self.git_calls_with("pull"), [], "nothing should be pulled")

    def test_a_dirty_tree_is_refused_not_updated(self):
        self.publish_new_commit()
        (self.install / "config.json").write_text("UNCOMMITTED LOCAL EDIT\n", encoding="utf-8")
        text = self.run_update()
        self.assertIn("uncommitted", text.lower())
        self.assertEqual(self.rev(self.install), self.sha_a, "the tree must be left as it was")
        self.assertEqual((self.install / "config.json").read_text(encoding="utf-8"),
                         "UNCOMMITTED LOCAL EDIT\n")
        self.assertEqual(self.git_calls_with("pull"), [])

    def test_untracked_files_do_not_block_an_update(self):
        c = self.publish_new_commit()
        (self.install / "notes.tmp").write_text("scratch\n", encoding="utf-8")
        self.run_update()
        self.assertEqual(self.rev(self.install), c)
        self.assertTrue((self.install / "notes.tmp").exists())

    def test_a_side_branch_is_refused(self):
        self.publish_new_commit()
        self.git("checkout", "-b", "topic", cwd=self.install)
        topic_head = self.local_commit()
        text = self.run_update()
        self.assertIn("topic", text)
        self.assertEqual(self.rev(self.install), topic_head)
        self.assertEqual(self.git_calls_with("pull"), [])

    def test_a_detached_head_is_refused(self):
        self.publish_new_commit()
        self.git("checkout", "--detach", cwd=self.install)
        text = self.run_update()
        self.assertIn("detached", text.lower())
        self.assertEqual(self.git_calls_with("pull"), [])

    def test_the_pull_is_fast_forward_only(self):
        c = self.publish_new_commit()
        self.run_update()
        pulls = self.git_calls_with("pull")
        self.assertEqual(len(pulls), 1)
        self.assertIn("--ff-only", pulls[0])
        self.assertEqual(self.rev(self.install), c)

    def test_a_diverged_install_fails_cleanly_and_keeps_its_commit(self):
        self.publish_new_commit()
        mine = self.local_commit()
        text = self.run_update()
        self.assertEqual(self.rev(self.install), mine)
        self.assertNotIn("Integrity check", text)
        self.assertIn("Update failed", text)

    def test_a_mismatch_rolls_back_without_reset_hard(self):
        self.publish_new_commit()
        self.remote_sha = "f" * 40            # GitHub "says" a commit the pull does not land on
        text = self.run_update()
        self.assertIn("Integrity check failed", text)
        self.assertEqual(self.rev(self.install), self.sha_a, "the rollback did not happen")
        resets = self.git_calls_with("reset")
        self.assertTrue(resets, "expected a rollback")
        for call in resets:
            self.assertNotIn("--hard", call)
            self.assertIn("--keep", call)

    def test_a_plain_update_still_works(self):
        c = self.publish_new_commit()
        text = self.run_update()
        self.assertEqual(self.rev(self.install), c)
        self.assertIn("Updated", text)

    def test_up_to_date_is_still_reported(self):
        self.remote_sha = self.sha_a
        text = self.run_update()
        self.assertIn("No update found", text)


class UpdateRefreshesCommandTest(UpdateRepoCase):

    def test_update_copies_the_new_pulse_command_into_the_config_dir(self):
        installed = self.cfg_dir / "commands" / "pulse.md"
        installed.parent.mkdir()
        installed.write_text("pulse command OLD\n", encoding="utf-8")
        self.publish_new_commit("pulse command NEW\n")
        self.run_update()
        self.assertEqual(installed.read_text(encoding="utf-8"), "pulse command NEW\n")

    def test_a_failed_update_leaves_the_installed_command_alone(self):
        installed = self.cfg_dir / "commands" / "pulse.md"
        installed.parent.mkdir()
        installed.write_text("pulse command OLD\n", encoding="utf-8")
        self.publish_new_commit("pulse command NEW\n")
        (self.install / "config.json").write_text("dirty\n", encoding="utf-8")
        self.run_update()
        self.assertEqual(installed.read_text(encoding="utf-8"), "pulse command OLD\n")


# ---------------------------------------------------------------------------
# F25: --uninstall
# ---------------------------------------------------------------------------

class UninstallTest(SandboxCase):

    def setUp(self):
        super().setUp()
        self.printed = self.printing()

    def install_everything(self, extra=None):
        base = {"theme": "dark", "env": {"GREETING": "café"}}
        base.update(extra or {})
        self.settings.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        cs.install_status_line()
        cs.install_hooks()
        cs.install_pulse_command()
        cs._settings_backed_up.clear()
        self.printed.clear()

    def test_it_removes_everything_usage_pulse_added(self):
        self.install_everything()
        self.assertTrue((self.cfg_dir / "commands" / "pulse.md").exists())
        self.assertEqual(cs.cmd_uninstall(), 0)
        data = self.settings_data()
        self.assertNotIn("statusLine", data)
        self.assertNotIn("subagentStatusLine", data)
        self.assertNotIn("hooks", data)
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"], {"GREETING": "café"})
        self.assertFalse((self.cfg_dir / "commands" / "pulse.md").exists())

    def test_it_keeps_other_tools_hooks_and_status_line(self):
        foreign_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}
        self.install_everything()
        data = self.settings_data()
        data["hooks"]["PostToolUse"].insert(0, foreign_hook)
        data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "notify-send done"}]}]
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(cs.cmd_uninstall(), 0)
        hooks = self.settings_data()["hooks"]
        self.assertEqual(hooks["PostToolUse"], [foreign_hook])
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["command"], "notify-send done")
        self.assertEqual(set(hooks), {"PostToolUse", "Stop"})

    def test_another_tools_status_line_is_never_touched(self):
        other = {"type": "command", "command": "ccstatusline", "padding": 2}
        self.settings.write_text(json.dumps({"statusLine": other,
                                             "subagentStatusLine": {"type": "command", "command": "mine"}}),
                                 encoding="utf-8")
        self.assertEqual(cs.cmd_uninstall(), 0)
        data = self.settings_data()
        self.assertEqual(data["statusLine"], other)
        self.assertEqual(data["subagentStatusLine"]["command"], "mine")

    def test_a_pulse_command_that_is_not_ours_is_left_alone(self):
        cmd = self.cfg_dir / "commands" / "pulse.md"
        cmd.parent.mkdir()
        cmd.write_text("my own /pulse command, nothing to do with the script\n", encoding="utf-8")
        self.settings.write_text("{}", encoding="utf-8")
        self.assertEqual(cs.cmd_uninstall(), 0)
        self.assertTrue(cmd.exists())

    def test_a_backup_of_settings_is_kept(self):
        self.install_everything()
        folder = self.state / "settings-backups"
        before = set(folder.glob("*"))
        cs.cmd_uninstall()
        new = sorted(set(folder.glob("*")) - before)
        self.assertEqual(len(new), 1)
        kept = new[0].read_text(encoding="utf-8")
        self.assertIn("statusLine", kept)
        self.assertIn("PostToolUse", kept)

    def test_nothing_installed_is_a_clean_no_op(self):
        self.assertEqual(cs.cmd_uninstall(), 0)
        self.assertFalse(self.settings.exists(), "must not create settings.json")
        self.assertTrue(any("nothing" in line.lower() for line in self.printed), self.printed)

    def test_running_it_twice_is_harmless(self):
        self.install_everything()
        self.assertEqual(cs.cmd_uninstall(), 0)
        after_first = self.settings.read_text(encoding="utf-8")
        self.assertEqual(cs.cmd_uninstall(), 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), after_first)

    def test_an_invalid_settings_file_is_reported_and_untouched(self):
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(cs.cmd_uninstall(), 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_it_honours_claude_config_dir(self):
        other = self.root / "second-profile"
        other.mkdir()
        (other / "settings.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": 'python "/x/claude_status.py"'}}),
            encoding="utf-8")
        self.install_everything()
        with mock.patch.object(cs, "_claude_config_dir", REAL_CONFIG_DIR), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(other)}):
            self.assertEqual(cs.cmd_uninstall(), 0)
        self.assertNotIn("statusLine", json.loads((other / "settings.json").read_text(encoding="utf-8")))
        self.assertIn("statusLine", self.settings_data(), "the default profile must not be touched")

    def test_the_flag_is_wired_into_main_and_help(self):
        with mock.patch.object(cs, "cmd_uninstall", return_value=0) as run, \
                mock.patch.object(cs.sys, "argv", ["claude_status.py", "--uninstall"]):
            cs.main()
        run.assert_called_once_with()
        self.assertIn("--uninstall", {row[0] for row in cs.CLI_FLAGS})

    def test_a_failure_exit_code_reaches_the_shell(self):
        with mock.patch.object(cs, "cmd_uninstall", return_value=1), \
                mock.patch.object(cs.sys, "argv", ["claude_status.py", "--uninstall"]):
            with self.assertRaises(SystemExit) as raised:
                cs.main()
        self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
