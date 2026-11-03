"""Live widgets must describe THIS window, not the machine.

Four findings from the 3.6.2 sweep, one theme: state that belongs to one
Claude Code window was kept in a file every window shares, or keyed by a
number that changes on every repaint.

F06  The branch, changed-file and ahead/behind caches were machine-wide, so a
     window showed another repo's branch (and a non-repo folder hid the branch
     everywhere). They are now kept per repo folder, outside hook_state.json.
F15  A damaged ``git_branch_ts`` in hook_state.json blanked the bar.
F13  On Windows the "session id" was the pid of a throw-away Git Bash, so every
     repaint wrote a new sessions/<pid>.json that nothing removed, and the
     sessions widget counted the window's own repaints as other sessions.
F32  git_drift / files_changed only saved their cache after git returned, so a
     slow git in a big repo was re-run (and cancelled) on every repaint.
F40  The heartbeat showed lifetime totals across all windows ("4001 tools 72h").

Run: ``python -m unittest tests.test_pr2_live_widgets``
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time as real_time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Killed(BaseException):
    """Claude Code cancelling the script mid-git: no handler gets to clean up."""


class _Clock:
    """A settable stand-in for the ``time`` module inside claude_status."""

    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


def _norm(path):
    return os.path.normcase(os.path.realpath(str(path)))


class FakeGit:
    """A scriptable ``git`` that answers according to the folder it runs in."""

    def __init__(self, repos=None):
        # {folder: {"branch": str, "changed": int, "ahead": int, "behind": int}}
        self.repos = {_norm(k): v for k, v in (repos or {}).items()}
        self.calls = []
        self.script = []  # per-call outcomes consumed first: "kill" | "timeout"
        self.on_call = None

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self.on_call:
            self.on_call(cmd)
        if self.script:
            outcome = self.script.pop(0)
            if outcome == "kill":
                raise _Killed()
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        repo = self.repos.get(_norm(kwargs.get("cwd") or os.getcwd()))
        if repo is None:
            return SimpleNamespace(returncode=128, stdout="", stderr="not a git repository")
        args = list(cmd[1:])
        out = ""
        if args[:2] == ["branch", "--show-current"]:
            out = repo.get("branch", "") + "\n"
        elif args[:2] == ["diff", "--name-only"]:
            out = "".join(f"file{i}.py\n" for i in range(repo.get("changed", 0)))
        elif args[0] == "rev-list":
            ahead, behind = repo.get("ahead", 0), repo.get("behind", 0)
            if "--left-right" in args:
                out = f"{ahead}\t{behind}\n"
            elif "HEAD..@{upstream}" in args:
                out = f"{behind}\n"
            elif "@{upstream}..HEAD" in args:
                out = f"{ahead}\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def count(self, word):
        return sum(1 for cmd, _ in self.calls if word in cmd)


@contextmanager
def in_dir(path):
    here = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(here)


class _Base(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.repo_x = self.sb.root / "repo-x"
        self.repo_y = self.sb.root / "repo-y"
        self.plain = self.sb.root / "not-a-repo"
        for d in (self.repo_x, self.repo_y, self.plain):
            d.mkdir()
        self.here = os.getcwd()
        self.addCleanup(os.chdir, self.here)

    def use_git(self, git):
        p = mock.patch.object(cs.subprocess, "run", git)
        p.start()
        self.addCleanup(p.stop)
        return git

    def use_clock(self):
        clock = _Clock()
        p = mock.patch.object(cs, "time", clock)
        p.start()
        self.addCleanup(p.stop)
        return clock


# ---------------------------------------------------------------------------
# F06 - one cache per repo, not one per machine
# ---------------------------------------------------------------------------

class GitCachesArePerRepoTest(_Base):
    def test_branch_follows_the_repo_not_the_machine(self):
        self.use_git(FakeGit({self.repo_x: {"branch": "main"},
                              self.repo_y: {"branch": "feature-login"}}))
        with in_dir(self.repo_x):
            self.assertEqual(cs._get_git_branch(), "main")
        with in_dir(self.repo_y):
            self.assertEqual(cs._get_git_branch(), "feature-login",
                             "window in repo-y showed the branch cached by repo-x")
        with in_dir(self.repo_x):
            self.assertEqual(cs._get_git_branch(), "main")

    def test_a_folder_that_is_not_a_repo_does_not_hide_the_branch_elsewhere(self):
        self.use_git(FakeGit({self.repo_x: {"branch": "main"}}))
        with in_dir(self.plain):
            self.assertIsNone(cs._get_git_branch())
        with in_dir(self.repo_x):
            self.assertEqual(cs._get_git_branch(), "main",
                             "the empty answer cached for a non-repo hid the branch")

    def test_branch_is_cached_per_repo_until_the_ttl(self):
        git = self.use_git(FakeGit({self.repo_x: {"branch": "main"}}))
        clock = self.use_clock()
        with in_dir(self.repo_x):
            cs._get_git_branch()
            clock.now += 5
            cs._get_git_branch()
            self.assertEqual(git.count("--show-current"), 1)
            clock.now += cs.GIT_BRANCH_CACHE_TTL + 1
            cs._get_git_branch()
        self.assertEqual(git.count("--show-current"), 2)

    def test_branch_cache_never_touches_the_heartbeat_file(self):
        """Its read-modify-write of hook_state.json could roll back a newer
        tool_count written by the PostToolUse hook while git was running."""
        hook_file = self.sb.state / "hook_state.json"
        original = {"tool_count": 5, "session_start": 111.0, "last_refresh": 222.0}
        hook_file.write_text(json.dumps(original), encoding="utf-8")
        git = self.use_git(FakeGit({self.repo_x: {"branch": "main"}}))

        def hook_fires_during_git(_cmd):
            hook_file.write_text(json.dumps(dict(original, tool_count=9)), encoding="utf-8")

        git.on_call = hook_fires_during_git
        with in_dir(self.repo_x):
            self.assertEqual(cs._get_git_branch(), "main")
        saved = json.loads(hook_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["tool_count"], 9, "the repaint overwrote the hook's newer count")
        self.assertNotIn("git_branch", saved)

    def test_changed_file_count_follows_the_repo(self):
        self.use_git(FakeGit({self.repo_x: {"changed": 3}, self.repo_y: {"changed": 0}}))
        with in_dir(self.repo_x):
            self.assertEqual(cs._count_changed_files(), 3)
        with in_dir(self.repo_y):
            self.assertEqual(cs._count_changed_files(), 0,
                             "the clean repo showed the other repo's count")

    def test_ahead_behind_follows_the_repo(self):
        self.use_git(FakeGit({self.repo_x: {"ahead": 1, "behind": 2},
                              self.repo_y: {"ahead": 0, "behind": 0}}))
        with in_dir(self.repo_x):
            self.assertEqual(cs._check_git_drift(), (2, 1))
        with in_dir(self.repo_y):
            self.assertEqual(cs._check_git_drift(), (0, 0),
                             "the in-sync repo showed the other repo's drift")

    @unittest.skipUnless(shutil.which("git"), "needs a real git")
    def test_with_real_git_two_repos_keep_their_own_numbers(self):
        def git(folder, *args):
            subprocess.run([shutil.which("git"), "-c", "user.name=t", "-c", "user.email=t@t",
                            *args], cwd=str(folder), check=True, capture_output=True)

        git(self.repo_x, "init", "-q", "-b", "alpha")
        git(self.repo_y, "init", "-q", "-b", "beta")
        for folder in (self.repo_x, self.repo_y):
            (folder / "a.txt").write_text("one\n", encoding="utf-8")
            git(folder, "add", "a.txt")
            git(folder, "commit", "-q", "-m", "first")
        (self.repo_x / "a.txt").write_text("changed\n", encoding="utf-8")
        with in_dir(self.repo_x):
            self.assertEqual(cs._get_git_branch(), "alpha")
            self.assertEqual(cs._count_changed_files(), 1)
        with in_dir(self.repo_y):
            self.assertEqual(cs._get_git_branch(), "beta")
            self.assertEqual(cs._count_changed_files(), 0)


# ---------------------------------------------------------------------------
# F15 (branch part) - a damaged cache must be harmless
# ---------------------------------------------------------------------------

class DamagedBranchCacheTest(_Base):
    def _branch_with(self, hook_state=None, branch_cache=None):
        git = self.use_git(FakeGit({self.repo_x: {"branch": "main"}}))
        if hook_state is not None:
            data = hook_state if isinstance(hook_state, bytes) else hook_state.encode("utf-8")
            (self.sb.state / "hook_state.json").write_bytes(data)
        if branch_cache is not None:
            data = branch_cache if isinstance(branch_cache, bytes) else branch_cache.encode("utf-8")
            (self.sb.state / "git_branch.json").write_bytes(data)
        with in_dir(self.repo_x):
            return cs._get_git_branch(), git

    def test_null_git_branch_ts_in_the_heartbeat_file(self):
        branch, _ = self._branch_with(hook_state='{"git_branch": "stale", "git_branch_ts": null}')
        self.assertEqual(branch, "main")

    def test_text_git_branch_ts_in_the_heartbeat_file(self):
        branch, _ = self._branch_with(hook_state='{"git_branch": "stale", "git_branch_ts": "soon"}')
        self.assertEqual(branch, "main")

    def test_heartbeat_file_of_the_wrong_shape(self):
        branch, _ = self._branch_with(hook_state='["x"]')
        self.assertEqual(branch, "main")

    def test_heartbeat_file_that_is_not_utf8(self):
        branch, _ = self._branch_with(hook_state=b"\xff\xfe{\xe9}")
        self.assertEqual(branch, "main")

    def test_legacy_branch_in_the_heartbeat_file_is_not_trusted(self):
        """A fresh-looking branch left there by 3.6.2 belongs to no repo."""
        branch, _ = self._branch_with(
            hook_state=json.dumps({"git_branch": "stale", "git_branch_ts": real_time.time()}))
        self.assertEqual(branch, "main")

    def test_damaged_branch_cache_shapes(self):
        key = _norm(self.repo_x)
        shapes = [
            "[]",
            "null",
            '{"repos": null}',
            '{"repos": []}',
            json.dumps({"repos": {key: "stale"}}),
            json.dumps({"repos": {key: {"branch": "stale", "timestamp": None}}}),
            json.dumps({"repos": {key: {"branch": "stale", "timestamp": "soon"}}}),
            json.dumps({"repos": {key: {"branch": 7, "timestamp": real_time.time()}}}),
            '{"repos": {%s: {"branch": "stale", "timestamp": Infinity}}}' % json.dumps(key),
            '{"repos": {%s: {"branch": "stale", "timestamp": NaN}}}' % json.dumps(key),
            b"\xff\xfe junk",
            b"",
        ]
        for shape in shapes:
            with self.subTest(shape=shape):
                branch, _ = self._branch_with(branch_cache=shape)
                self.assertEqual(branch, "main")

    def test_a_timestamp_in_the_future_does_not_pin_a_stale_branch(self):
        key = _norm(self.repo_x)
        future = real_time.time() + 10 ** 7
        branch, git = self._branch_with(branch_cache=json.dumps(
            {"repos": {key: {"branch": "stale", "timestamp": future}}}))
        self.assertEqual(branch, "main")
        self.assertEqual(git.count("--show-current"), 1)


# ---------------------------------------------------------------------------
# F32 - record the attempt BEFORE git runs; never wait long on the render path
# ---------------------------------------------------------------------------

class SlowGitCannotFreezeTheBarTest(_Base):
    def test_files_changed_killed_mid_git_is_not_retried_by_the_next_repaint(self):
        git = self.use_git(FakeGit({self.repo_x: {"changed": 4}}))
        clock = self.use_clock()
        with in_dir(self.repo_x):
            self.assertEqual(cs._count_changed_files(), 4)
            clock.now += cs.FILES_CHANGED_CACHE_TTL + 1
            git.script = ["kill"]
            with self.assertRaises(_Killed):
                cs._count_changed_files()
            calls = len(git.calls)
            clock.now += 2  # the next repaint, a fresh process
            self.assertEqual(cs._count_changed_files(), 4,
                             "the last known count is kept while a refresh is pending")
        self.assertEqual(len(git.calls), calls,
                         "a repaint killed mid-git left no trace, so the next one "
                         "ran git again and the bar froze")

    def test_drift_killed_mid_git_is_not_retried_by_the_next_repaint(self):
        git = self.use_git(FakeGit({self.repo_x: {"ahead": 1, "behind": 2}}))
        clock = self.use_clock()
        with in_dir(self.repo_x):
            self.assertEqual(cs._check_git_drift(), (2, 1))
            clock.now += cs.GIT_DRIFT_CACHE_TTL + 1
            git.script = ["kill"]
            with self.assertRaises(_Killed):
                cs._check_git_drift()
            calls = len(git.calls)
            clock.now += 2
            self.assertEqual(cs._check_git_drift(), (2, 1))
        self.assertEqual(len(git.calls), calls)

    def test_files_changed_timeout_keeps_the_last_count(self):
        git = self.use_git(FakeGit({self.repo_x: {"changed": 4}}))
        clock = self.use_clock()
        with in_dir(self.repo_x):
            cs._count_changed_files()
            clock.now += cs.FILES_CHANGED_CACHE_TTL + 1
            git.script = ["timeout"]
            self.assertEqual(cs._count_changed_files(), 4,
                             "a git timeout reset the count to 0")
            calls = len(git.calls)
            clock.now += 2
            cs._count_changed_files()
        self.assertEqual(len(git.calls), calls, "a timed-out check is retried only after the TTL")

    def test_drift_timeout_keeps_the_last_numbers(self):
        git = self.use_git(FakeGit({self.repo_x: {"ahead": 1, "behind": 2}}))
        clock = self.use_clock()
        with in_dir(self.repo_x):
            cs._check_git_drift()
            clock.now += cs.GIT_DRIFT_CACHE_TTL + 1
            git.script = ["timeout"] * 2
            self.assertEqual(cs._check_git_drift(), (2, 1))

    def test_a_check_is_retried_once_its_ttl_has_passed(self):
        git = self.use_git(FakeGit({self.repo_x: {"changed": 4}}))
        clock = self.use_clock()
        with in_dir(self.repo_x):
            cs._count_changed_files()
            clock.now += cs.FILES_CHANGED_CACHE_TTL + 1
            git.script = ["timeout"]
            cs._count_changed_files()
            clock.now += cs.FILES_CHANGED_CACHE_TTL + 1
            git.repos[_norm(self.repo_x)]["changed"] = 6
            self.assertEqual(cs._count_changed_files(), 6)

    def test_git_on_the_render_path_has_a_short_timeout(self):
        git = self.use_git(FakeGit({self.repo_x: {"changed": 1, "ahead": 1, "behind": 1}}))
        with in_dir(self.repo_x):
            cs._count_changed_files()
            cs._check_git_drift()
        timeouts = [kw.get("timeout") for _, kw in git.calls]
        self.assertTrue(timeouts and all(t is not None and t <= 2 for t in timeouts), timeouts)

    def test_drift_asks_git_once_not_twice(self):
        git = self.use_git(FakeGit({self.repo_x: {"ahead": 1, "behind": 1}}))
        with in_dir(self.repo_x):
            cs._check_git_drift()
        self.assertEqual(git.count("rev-list"), 1)

    def test_no_upstream_is_zero_zero_and_cached(self):
        git = self.use_git(FakeGit({}))  # every command fails like "no upstream"
        with in_dir(self.repo_x):
            self.assertEqual(cs._check_git_drift(), (0, 0))
            self.assertEqual(cs._check_git_drift(), (0, 0))
        self.assertEqual(git.count("rev-list"), 1)

    def test_damaged_caches_are_harmless(self):
        git = self.use_git(FakeGit({self.repo_x: {"changed": 2, "ahead": 1, "behind": 1}}))
        key = _norm(self.repo_x)
        for name in (cs.FILES_CHANGED_FILE, cs.GIT_DRIFT_FILE):
            for shape in ("[]", "null", '{"repos": 5}', json.dumps({"repos": {key: []}}),
                          json.dumps({"repos": {key: {"timestamp": None, "attempted": "x",
                                                      "count": "many", "ahead": None}}}),
                          '{"timestamp": null}', b"\xff\xfe"):
                data = shape if isinstance(shape, bytes) else shape.encode("utf-8")
                (self.sb.state / name).write_bytes(data)
                with self.subTest(file=name, shape=shape), in_dir(self.repo_x):
                    self.assertEqual(cs._count_changed_files(), 2)
                    self.assertEqual(cs._check_git_drift(), (1, 1))
                (self.sb.state / name).unlink()


# ---------------------------------------------------------------------------
# F13 - a session is identified by the id Claude Code sends, not a pid
# ---------------------------------------------------------------------------

USAGE = {"five_hour": {"utilization": 12}, "seven_day": {"utilization": 34}}


class SessionMarkersTest(_Base):
    def setUp(self):
        super().setUp()
        self.config = {"show": {"sessions": True}}
        self.sessions = cs._get_sessions_dir()

    def widget(self, on):
        p = mock.patch.object(cs, "load_config", return_value={"show": {"sessions": on}})
        p.start()
        self.addCleanup(p.stop)

    def markers(self):
        return sorted(p.name for p in self.sessions.iterdir() if p.name.endswith(".json"))

    def test_the_stdin_session_id_is_the_identity(self):
        self.assertEqual(cs._get_session_id({"session_id": "abc-123"}), "abc-123")

    def test_the_identity_is_safe_to_use_as_a_file_name(self):
        sid = cs._get_session_id({"session_id": "../../evil name"})
        self.assertNotIn("/", str(sid))
        self.assertNotIn("\\", str(sid))
        self.assertNotIn("..", str(sid))

    def test_a_changing_parent_pid_does_not_change_the_identity(self):
        ctx = {"session_id": "win-1"}
        with mock.patch.object(os, "getppid", side_effect=range(1000, 1010)):
            ids = {cs._get_session_id(ctx) for _ in range(5)}
        self.assertEqual(ids, {"win-1"})

    def test_repaints_of_one_window_leave_one_marker(self):
        self.widget(True)
        with mock.patch.object(os, "getppid", side_effect=range(1000, 1100)):
            for _ in range(30):
                cs._update_session_state(USAGE, {"session_id": "win-1", "model_name": "X"})
        self.assertEqual(len(self.markers()), 1,
                         "one file per repaint piled up in the sessions folder")

    def test_one_window_does_not_count_itself_as_other_sessions(self):
        self.widget(True)
        with mock.patch.object(os, "getppid", side_effect=range(1000, 1100)):
            for _ in range(30):
                cs._update_session_state(USAGE, {"session_id": "win-1"})
        self.assertEqual(cs._get_active_sessions("win-1"), [])

    def test_a_second_window_is_counted_once(self):
        self.widget(True)
        for _ in range(5):
            cs._update_session_state(USAGE, {"session_id": "win-1"})
            cs._update_session_state(USAGE, {"session_id": "win-2"})
        self.assertEqual(len(cs._get_active_sessions("win-1")), 1)
        self.assertEqual(len(cs._get_active_sessions("win-2")), 1)

    def test_the_widget_finds_its_own_window_without_being_told(self):
        """build_status_line asks with no argument: the id main() just stored
        beside the stdin context identifies the window being painted."""
        self.widget(True)
        cs._update_session_state(USAGE, {"session_id": "win-1"})
        cs._update_session_state(USAGE, {"session_id": "win-2"})
        (cs.get_account_state_dir() / "stdin_ctx.json").write_text(
            json.dumps({"session_id": "win-1"}), encoding="utf-8")
        others = cs._get_active_sessions()
        self.assertEqual(len(others), 1)
        self.assertEqual(str(others[0].get("pid")), "win-2")

    def test_no_markers_are_written_while_the_widget_is_off(self):
        self.widget(False)
        for _ in range(5):
            cs._update_session_state(USAGE, {"session_id": "win-1"})
        self.assertEqual(self.markers(), [],
                         "markers were written although nothing reads them")

    def test_a_marker_is_written_when_the_widget_is_on(self):
        self.widget(True)
        cs._update_session_state(USAGE, {"session_id": "win-1", "model_name": "Fable"})
        (name,) = self.markers()
        data = json.loads((self.sessions / name).read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "Fable")
        self.assertEqual(data["session_pct"], 12)
        self.assertEqual(data["weekly_pct"], 34)

    def test_windows_without_a_session_id_write_no_marker(self):
        """The parent pid there is a new shell every repaint: no stable name."""
        self.widget(True)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertIsNone(cs._get_session_id({}))
        with mock.patch.object(cs, "_get_session_id", return_value=None):
            cs._update_session_state(USAGE, {})
        self.assertEqual(self.markers(), [])

    def test_elsewhere_the_parent_pid_is_still_a_stable_fallback(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(os, "getppid", return_value=4242):
            self.assertEqual(cs._get_session_id({}), 4242)

    def _backlog(self, count):
        old = real_time.time() - 10 * cs.SESSION_STALE_SECONDS
        for i in range(count):
            f = self.sessions / f"{10000 + i}.json"
            f.write_text(json.dumps({"pid": 10000 + i, "timestamp": old}), encoding="utf-8")
            os.utime(f, (old, old))

    def test_a_backlog_of_stale_markers_is_cleared_in_bounded_batches(self):
        self._backlog(400)
        self.assertEqual(cs._get_active_sessions("win-1"), [])
        left = len(self.markers())
        self.assertGreater(left, 0, "one repaint deleted thousands of files")
        self.assertLess(left, 400)

    def test_stale_markers_are_not_opened_to_find_out_they_are_stale(self):
        self._backlog(50)
        opened = []
        real_open = open

        def spy(file, *a, **k):
            if str(file).startswith(str(self.sessions)):
                opened.append(file)
            return real_open(file, *a, **k)

        with mock.patch.object(cs, "open", spy, create=True):
            cs._get_active_sessions("win-1")
        self.assertEqual(opened, [], "every stale marker was opened and parsed")

    def test_the_backlog_is_cleared_even_while_the_widget_is_off(self):
        """The 23,000 markers already on disk must go without opting in."""
        self.widget(False)
        self._backlog(150)
        cs._update_session_state(USAGE, {"session_id": "win-1"})
        self.assertLess(len(self.markers()), 150)

    def test_fresh_markers_survive_the_cleanup(self):
        self.widget(True)
        cs._update_session_state(USAGE, {"session_id": "win-2"})
        self._backlog(150)
        cs._update_session_state(USAGE, {"session_id": "win-1"})
        self.assertIn("win-2.json", self.markers())
        self.assertIn("win-1.json", self.markers())

    def test_damaged_markers_do_not_break_the_widget(self):
        shapes = ['{"timestamp": null, "pid": 1}', "[]", "null", '{"pid": 1}',
                  '{"timestamp": "soon"}', '{"timestamp": Infinity}', b"\xff\xfe"]
        for i, shape in enumerate(shapes):
            data = shape if isinstance(shape, bytes) else shape.encode("utf-8")
            (self.sessions / f"bad{i}.json").write_bytes(data)
        self.assertEqual(cs._get_active_sessions("win-1"), [])
        self.assertEqual(self.markers(), [], "damaged markers should be removed")

    def test_the_sessions_folder_is_not_scanned_on_every_repaint_when_off(self):
        self.widget(False)
        cs._update_session_state(USAGE, {"session_id": "win-1"})
        scans = []
        real_scandir, real_listdir = os.scandir, os.listdir

        def spy_scandir(path="."):
            scans.append(str(path))
            return real_scandir(path)

        def spy_listdir(path="."):
            scans.append(str(path))
            return real_listdir(path)

        with mock.patch.object(os, "scandir", spy_scandir),                 mock.patch.object(os, "listdir", spy_listdir):
            for _ in range(5):
                cs._update_session_state(USAGE, {"session_id": "win-1"})
        self.assertEqual([s for s in scans if str(self.sessions) in s], [],
                         "the cleanup stamp should keep repaints from listing the folder")


class SessionsWidgetEndToEndTest(_Base):
    """The whole repaint, as Claude Code drives it: stdin in, one line out."""

    def paint(self, session_id, ppid):
        payload = {
            "session_id": session_id, "model": {"display_name": "Fable 5.1"},
            "workspace": {"current_dir": str(self.plain)},
            "rate_limits": {
                "five_hour": {"used_percentage": 17, "resets_at": int(real_time.time()) + 3600},
                "seven_day": {"used_percentage": 2, "resets_at": int(real_time.time()) + 86400}},
        }
        out = io.BytesIO()

        class _Stdout:
            buffer = out

        with mock.patch.object(cs, "check_for_update", lambda: False),                 mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: False),                 mock.patch.object(cs, "fetch_usage", side_effect=AssertionError("network")),                 mock.patch.object(os, "getppid", return_value=ppid),                 mock.patch.object(cs.sys, "argv", ["claude_status.py"]),                 mock.patch.object(cs.sys, "stdin", io.StringIO(json.dumps(payload))),                 mock.patch.object(cs.sys, "stdout", _Stdout()),                 in_dir(self.plain):
            cs.main()
        return ANSI.sub("", out.getvalue().decode("utf-8"))

    def enable_sessions_widget(self):
        path = cs.get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"show": {"sessions": True}, "max_width": 100}),
                        encoding="utf-8")

    def test_a_lone_window_never_counts_its_own_repaints(self):
        self.enable_sessions_widget()
        lines = [self.paint("window-a", ppid=5000 + i) for i in range(6)]
        self.assertTrue(all("session" not in line for line in lines), lines[-1])
        markers = [p for p in cs._get_sessions_dir().iterdir() if p.name.endswith(".json")]
        self.assertEqual([p.name for p in markers], ["window-a.json"])

    def test_two_windows_see_each_other_once(self):
        self.enable_sessions_widget()
        for i in range(4):
            self.paint("window-a", ppid=5000 + i)
        self.assertIn("+1 session", self.paint("window-b", ppid=6000))
        self.assertIn("+1 session", self.paint("window-a", ppid=5100))
        self.assertNotIn("+2 sessions", self.paint("window-a", ppid=5101))

    def test_with_the_widget_off_no_markers_are_left_behind(self):
        for i in range(6):
            self.paint("window-a", ppid=5000 + i)
        markers = [p for p in cs._get_sessions_dir().iterdir() if p.name.endswith(".json")]
        self.assertEqual(markers, [])


# ---------------------------------------------------------------------------
# F40 - the heartbeat counts this session, not every session ever
# ---------------------------------------------------------------------------

class HeartbeatIsPerSessionTest(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(cs, "sync_status_line_refresh")
        p.start()
        self.addCleanup(p.stop)
        self.hook_file = self.sb.state / "hook_state.json"

    def fire(self, session_id="win-1", tool="Read"):
        payload = {"tool_name": tool}
        if session_id is not None:
            payload["session_id"] = session_id
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            cs.hook_refresh("")

    def state(self):
        return json.loads(self.hook_file.read_text(encoding="utf-8"))

    def test_a_new_session_does_not_inherit_the_lifetime_total(self):
        self.hook_file.write_text(json.dumps({
            "tool_count": 4000, "session_start": real_time.time() - 3 * 86400,
            "last_refresh": real_time.time() - 60, "last_tool": "Bash"}), encoding="utf-8")
        self.fire("brand-new-session")
        state = self.state()
        self.assertEqual(state["tool_count"], 1)
        self.assertLess(real_time.time() - state["session_start"], 30)

    def test_the_heartbeat_widget_shows_the_new_session(self):
        self.hook_file.write_text(json.dumps({
            "tool_count": 4000, "session_start": real_time.time() - 3 * 86400,
            "last_refresh": real_time.time() - 60}), encoding="utf-8")
        self.fire("brand-new-session")
        show = {k: False for k in cs.DEFAULT_SHOW}
        show["heartbeat"] = True
        with mock.patch.object(cs, "get_config_path", return_value=self.sb.root / "config.json"):
            line = ANSI.sub("", cs.build_status_line(USAGE, "", {"show": show}, {}))
        self.assertIn("1 tools", line)
        self.assertNotIn("4001", line)
        self.assertNotIn("72h", line)

    def test_one_session_keeps_counting_from_its_first_call(self):
        self.fire("win-1")
        first = self.state()["session_start"]
        self.fire("win-1")
        self.fire("win-1")
        state = self.state()
        self.assertEqual(state["tool_count"], 3)
        self.assertEqual(state["session_start"], first)

    def test_two_windows_count_separately(self):
        for sid in ("win-1", "win-1", "win-2", "win-1"):
            self.fire(sid)
        self.assertEqual(cs._read_session_heartbeat("win-1")["tool_count"], 3)
        self.assertEqual(cs._read_session_heartbeat("win-2")["tool_count"], 1)
        self.assertIsNone(cs._read_session_heartbeat("win-3"))

    def test_the_shared_file_carries_the_latest_sessions_numbers(self):
        for sid in ("win-1", "win-1", "win-2"):
            self.fire(sid, tool="Edit")
        state = self.state()
        self.assertEqual(state["tool_count"], 1)
        self.assertEqual(state["last_tool"], "Edit")
        self.assertEqual(state["session_id"], "win-2")

    def test_the_freshness_clock_still_moves_on_every_call(self):
        self.fire("win-1")
        self.assertLess(real_time.time() - self.state()["last_refresh"], 30)

    def test_a_hostile_session_id_cannot_escape_the_folder(self):
        self.fire("../../escape")
        for p in self.sb.root.rglob("*escape*"):
            self.assertTrue(self.sb.state in p.parents, p)

    def test_old_session_files_are_pruned_when_a_new_session_starts(self):
        self.fire("win-1")
        folder = cs._get_hook_state_path().parent / "hook_sessions"
        old = real_time.time() - 30 * 86400
        for i in range(5):
            f = folder / f"ancient{i}.json"
            f.write_text(json.dumps({"tool_count": 9}), encoding="utf-8")
            os.utime(f, (old, old))
        self.fire("win-2")
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ["win-1.json", "win-2.json"])

    def test_without_a_session_id_it_still_counts(self):
        self.fire(None)
        self.fire(None)
        self.assertEqual(self.state()["tool_count"], 2)

    def test_without_a_session_id_a_long_idle_gap_starts_over(self):
        self.hook_file.write_text(json.dumps({
            "tool_count": 4000, "session_start": real_time.time() - 3 * 86400,
            "last_refresh": real_time.time() - 3 * 86400}), encoding="utf-8")
        self.fire(None)
        self.assertEqual(self.state()["tool_count"], 1)

    def test_a_damaged_shared_file_is_replaced_not_fatal(self):
        for damaged in ('["x"]', "null", '{"tool_count": "lots", "session_start": "then"}',
                        '{"tool_count": null}', b"\xff\xfe{", '{"tool_count": Infinity}'):
            data = damaged if isinstance(damaged, bytes) else damaged.encode("utf-8")
            for sid in (None, "win-1"):
                with self.subTest(damaged=damaged, session=sid):
                    self.hook_file.write_bytes(data)
                    (cs._get_hook_state_path().parent / "hook_sessions" / "win-1.json").unlink(
                        missing_ok=True)
                    self.fire(sid)
                    self.assertEqual(self.state()["tool_count"], 1)

    def test_a_damaged_session_file_restarts_that_session(self):
        self.fire("win-1")
        (cs._get_hook_state_path().parent / "hook_sessions" / "win-1.json").write_text(
            '{"tool_count": "lots", "session_start": null}', encoding="utf-8")
        self.fire("win-1")
        self.assertEqual(self.state()["tool_count"], 1)

    def test_a_reader_holding_the_file_does_not_drop_the_update(self):
        """Windows refuses os.replace while a repaint has the file open."""
        real_replace = os.replace
        refused = []

        def replace(src, dst):
            if Path(dst).name == "hook_state.json" and not refused:
                refused.append(dst)
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        self.fire("win-1")
        with mock.patch.object(os, "replace", replace):
            self.fire("win-1")
        self.assertEqual(refused and self.state()["tool_count"], 2,
                         "the tool call was lost because a repaint had the file open")


if __name__ == "__main__":
    unittest.main()
