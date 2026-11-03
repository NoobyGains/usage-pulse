"""The Pulse update check: one bounded network leg per repaint, and a real --config.

Claude Code cancels a status-line run when the next refresh fires (every 2s
with animation on), so a repaint gets at most one network operation, and that
operation has a hard ceiling (_NET_FETCH_BUDGET).

The update check has two sources for upstream's SHA:
- the GitHub API, which allows 60 unauthenticated requests an hour per address
  (a shared corporate egress uses them up and gets HTTP 403);
- ``git ls-remote``, which GitHub serves without that limit.

Upstream 3.6.0 asks the API and then git in the same call. On the render path
that is two network operations back to back. On Windows the git leg also had
no real ceiling: ``subprocess.run(capture_output=True, timeout=...)`` kills
git.exe, then waits for EOF on pipes that git's remote helper
(git-remote-https.exe) inherited, so the repaint lasted as long as the remote
hung.

Pinned here:
1. A repaint whose API leg fails runs no git. A later repaint with a free turn
   runs git alone, held to the same ceiling. A fresh round starts with the API.
2. While a round has nothing left to try, a repaint spawns no git at all.
3. ``_git`` returns at its timeout even when a helper holds the child's stdio.
4. ``--config`` (interactive) really performs both checks.
5. The API leg is never handed more than _NET_FETCH_BUDGET.

No test here reaches a real network: the API transport (_open_and_read) and
subprocess.run are faked, and the real-process tests use loopback only.

Run: ``python tests/test_port_update_check.py``
"""
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time as real_time
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

LOCAL_SHA = "a" * 40
OTHER_SHA = "b" * 40
UPSTREAM_URL = "https://github.com/%s.git" % cs.GITHUB_REPO
# The registry's dist-tags document (what the Claude Code check now fetches).
NPM_LATEST = json.dumps({"stable": "9.9.0", "next": "9.9.9", "latest": "9.9.9"}).encode()
FAKE_CLAUDE = "claude-fake-for-tests"
NETWORK_GIT = ("ls-remote", "fetch", "pull", "clone", "push")


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "rate limit exceeded", {}, io.BytesIO(b""))


class _Api:
    """Stands in for ``_open_and_read``, the transport under ``_fetch_bounded``.

    The real ``_fetch_bounded`` still runs (thread, ceiling, swallowed
    errors). Outcomes are queued per host: bytes, or an int HTTP status.
    """

    def __init__(self, github=(), npm=()):
        self.queues = {"api.github.com": list(github), "registry.npmjs.org": list(npm)}
        self.urls = []

    def __call__(self, req, max_bytes, deadline):
        url = req.full_url
        self.urls.append(url)
        host = url.split("/")[2]
        queue = self.queues.get(host)
        if queue is None:
            raise AssertionError("unexpected host in a test: %s" % url)
        outcome = queue.pop(0) if queue else 403
        if isinstance(outcome, int):
            error = _http_error(url, outcome)
            error.close()  # nothing reads the body; closed, it leaves no ResourceWarning
            raise error
        return outcome[:max_bytes]

    def calls_to(self, host):
        return [u for u in self.urls if u.split("/")[2] == host]


class _Killed(BaseException):
    """Claude Code cancelling the run mid-call: no ``except Exception`` sees it."""


class _Git:
    """``subprocess.run`` stand-in.

    Local git answers at once: HEAD is *local*, and ``merge-base
    --is-ancestor`` exits *merge_base_rc* (0: upstream's tip is already in our
    history; 1: it is not, so an update is available). Network git is
    scripted: ("ok", sha), ("fail",), ("hang",) or ("kill",); a hang uses up
    its whole timeout, on the fake clock when there is one and in real time
    otherwise, and a kill raises _Killed mid-call. Output goes wherever the
    caller pointed stdout: a pipe (capture_output) or a file object.
    """

    def __init__(self, network=(), clock=None, branch="main", origin=UPSTREAM_URL,
                 local=LOCAL_SHA, merge_base_rc=0):
        self.network = list(network)
        self.clock = clock
        self.branch = branch
        self.origin = origin
        self.local = local
        self.merge_base_rc = merge_base_rc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        args = list(cmd[1:])
        self.calls.append((args, kwargs))
        sub = args[0] if args else ""
        if args == ["--version"]:
            return self._done(kwargs, 0, "2.1.170 (Claude Code)\n")
        if sub in NETWORK_GIT:
            return self._remote(cmd, kwargs)
        if sub == "rev-parse":
            return self._done(kwargs, 0, self.local + "\n")
        if sub == "symbolic-ref":
            return self._done(kwargs, 0, self.branch + "\n")
        if sub == "remote":
            return self._done(kwargs, 0, self.origin + "\n")
        if sub == "merge-base":
            return self._done(kwargs, self.merge_base_rc, "")
        return self._done(kwargs, 0, "")  # cat-file -e: the commit is present

    @staticmethod
    def _done(kwargs, returncode, out):
        dest = kwargs.get("stdout")
        if hasattr(dest, "write"):
            dest.write(out.encode("utf-8"))
            return SimpleNamespace(returncode=returncode, stdout=None, stderr=None)
        return SimpleNamespace(returncode=returncode, stdout=out, stderr="")

    def _remote(self, cmd, kwargs):
        outcome = self.network.pop(0) if self.network else ("fail",)
        if outcome[0] == "hang":
            timeout = kwargs.get("timeout")
            if self.clock is not None:
                self.clock.now += timeout
            else:
                real_time.sleep(timeout)
            raise subprocess.TimeoutExpired(cmd, timeout)
        if outcome[0] == "kill":
            raise _Killed()
        if outcome[0] == "ok":
            return self._done(kwargs, 0, "%s\trefs/heads/main\n" % outcome[1])
        return self._done(kwargs, 128, "")

    @property
    def network_calls(self):
        return [(args, kw) for args, kw in self.calls if args and args[0] in NETWORK_GIT]


def _no_urlopen(*args, **kwargs):
    raise AssertionError("plain urlopen reached from the update check")


@contextmanager
def _sandbox(api, git, clock=None):
    """Isolated state dir, faked API transport and git, optional fake clock."""
    with tempfile.TemporaryDirectory() as td:
        patches = [
            mock.patch.object(cs, "get_state_dir", lambda: Path(td)),
            mock.patch.object(cs, "_open_and_read", api),
            mock.patch.object(cs.subprocess, "run", git),
            mock.patch.object(cs.urllib.request, "urlopen", _no_urlopen),
            mock.patch.object(cs, "_CLAUDE_PATH", FAKE_CLAUDE),
            mock.patch.dict(cs._own_stamps, {}, clear=True),
        ]
        if clock is not None:
            patches.append(mock.patch.object(cs, "time", clock))
        for p in patches:
            p.start()
        try:
            yield Path(td)
        finally:
            for p in reversed(patches):
                p.stop()


def _new_process():
    """Each repaint is a fresh process: nothing held in memory survives."""
    cs._own_stamps.clear()


def _next_repaint(clock):
    """A later repaint, past the turn-taking window, inside the retry backoff."""
    clock.now += cs._NET_MIN_SPACING + 0.1
    _new_process()


class RateLimitedRepaintTest(unittest.TestCase):
    """C2 / R0: the API answers 403 at once, and git would hang."""

    def test_failed_api_leg_runs_no_git_in_the_same_repaint(self):
        api = _Api(github=[403])
        git = _Git(network=[("hang",)])
        with _sandbox(api, git):
            started = real_time.monotonic()
            verdict = cs.check_for_update()
            elapsed = real_time.monotonic() - started
        self.assertEqual(
            git.network_calls, [],
            "a repaint whose API leg failed went on to run git ls-remote: two "
            "network operations in one repaint")
        self.assertIsNone(verdict)
        self.assertLess(
            elapsed, cs._NET_FETCH_BUDGET + 0.5,
            "the repaint took %.2fs; one leg is capped at %.1fs"
            % (elapsed, cs._NET_FETCH_BUDGET))
        self.assertEqual(len(api.calls_to("api.github.com")), 1)


class GitLegTest(unittest.TestCase):
    """C2 / R0: git still answers when the API is rate-limited, one repaint later."""

    def test_next_free_repaint_asks_git_alone_within_the_ceiling(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("ok", LOCAL_SHA)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())  # repaint 1: the API says 403
            self.assertEqual(git.network_calls, [])

            _next_repaint(clock)
            self.assertIs(cs.check_for_update(), False)  # repaint 2: git alone
            self.assertEqual(len(api.calls_to("api.github.com")), 1,
                             "the git repaint asked the API again")
            self.assertEqual(len(git.network_calls), 1)
            args, kwargs = git.network_calls[0]
            self.assertEqual(args[0], "ls-remote")
            self.assertLessEqual(
                kwargs["timeout"], cs._NET_FETCH_BUDGET,
                "the git leg on the render path was allowed %.2fs"
                % kwargs["timeout"])

            _next_repaint(clock)
            spawned = len(git.calls)
            self.assertIs(cs.check_for_update(), False)  # repaint 3: the cached verdict
            self.assertEqual(len(git.calls), spawned)

    def test_git_leg_waits_for_a_free_turn_and_spawns_nothing_meanwhile(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("ok", LOCAL_SHA)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())
            self.assertEqual(git.network_calls, [])
            clock.now += 1.0  # the very next repaint: the turn is still taken
            _new_process()
            spawned = len(git.calls)
            self.assertIsNone(cs.check_for_update())
            self.assertEqual(len(git.calls), spawned,
                             "a repaint that could not run the git leg still "
                             "spawned local git")

    def test_hung_git_leg_is_cut_at_the_fetch_ceiling(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("hang",)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())
            self.assertEqual(git.network_calls, [])
            _next_repaint(clock)
            started = clock.time()
            self.assertIsNone(cs.check_for_update())
            elapsed = clock.time() - started
        self.assertEqual(len(git.network_calls), 1, "the git leg never ran")
        self.assertLessEqual(elapsed, cs._NET_FETCH_BUDGET + 0.01)

    def test_after_the_backoff_a_fresh_round_starts_with_the_api(self):
        clock = _FakeClock()
        api = _Api(github=[403, LOCAL_SHA.encode()])
        git = _Git(network=[("fail",)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())  # API 403
            self.assertEqual(git.network_calls, [])
            _next_repaint(clock)
            self.assertIsNone(cs.check_for_update())  # git fails too
            self.assertEqual(len(git.network_calls), 1, "the git leg never ran")

            clock.now += cs._NET_RETRY_BACKOFF + 1
            _new_process()
            self.assertIs(cs.check_for_update(), False)
        self.assertEqual(len(api.calls_to("api.github.com")), 2)
        self.assertEqual(len(git.network_calls), 1, "the fresh round ran git as well")

    def test_after_a_git_verdict_the_next_round_starts_with_the_api(self):
        clock = _FakeClock()
        api = _Api(github=[403, LOCAL_SHA.encode()])
        git = _Git(network=[("ok", LOCAL_SHA)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())  # API 403, no git yet
            _next_repaint(clock)
            self.assertIs(cs.check_for_update(), False)  # from git

            clock.now += cs.UPDATE_CHECK_TTL + 1  # the hourly cache lapses
            _new_process()
            self.assertIs(cs.check_for_update(), False)
        self.assertEqual(len(api.calls_to("api.github.com")), 2)
        self.assertEqual(len(git.network_calls), 1)


class SpentRoundSpawnsNothingTest(unittest.TestCase):
    """C2 second cost: a round with nothing left to try spawns no git."""

    def test_both_legs_spent_means_zero_subprocesses(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("fail",)], clock=clock)
        with _sandbox(api, git, clock):
            cs.check_for_update()  # API leg fails
            _next_repaint(clock)
            cs.check_for_update()  # git leg fails
            for _ in range(3):
                _next_repaint(clock)
                spawned, asked = len(git.calls), len(api.urls)
                self.assertIsNone(cs.check_for_update())
                self.assertEqual(len(git.calls), spawned,
                                 "a repaint inside the backoff spawned git: %r"
                                 % [args for args, _ in git.calls[spawned:]])
                self.assertEqual(len(api.urls), asked)

    def test_cached_verdict_still_comes_first(self):
        clock = _FakeClock()
        api = _Api(github=[LOCAL_SHA.encode()])
        git = _Git(clock=clock)
        with _sandbox(api, git, clock):
            self.assertIs(cs.check_for_update(), False)
            _next_repaint(clock)
            spawned = len(git.calls)
            self.assertIs(cs.check_for_update(), False,
                          "the stamp the successful check left must not hide "
                          "its own cached verdict")
            self.assertEqual(len(git.calls), spawned)


class RewrittenScriptTest(unittest.TestCase):
    """U1: the script rewritten at the same commit (a local edit, ``git stash
    pop``, ``git checkout -- claude_status.py``) keeps its cached verdict.

    The rewrite changes the file's mtime, so the first fast path misses and the
    check has to ask local git which commit this is. It must not spend the
    round's git leg, or the repaint's network turn, just to find out that the
    cache already answers.
    """

    def setUp(self):
        td = tempfile.mkdtemp(prefix="pulse-script-")
        self.addCleanup(_rmtree_patiently, td)
        self.script = os.path.join(td, "claude_status.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write("# stands in for the installed script\n")
        patch = mock.patch.object(cs, "__file__", self.script)
        patch.start()
        self.addCleanup(patch.stop)

    def _rewrite_script(self):
        st = os.stat(self.script)
        os.utime(self.script, (st.st_atime, st.st_mtime + 10))

    def test_rewrite_at_the_same_commit_keeps_the_cached_verdict(self):
        clock = _FakeClock()
        api = _Api(github=[OTHER_SHA.encode()])
        git = _Git(clock=clock, merge_base_rc=1)  # upstream moved on: update
        with _sandbox(api, git, clock):
            self.assertIs(cs.check_for_update(), True)
            self._rewrite_script()
            verdicts = []
            for _ in range(5):
                _next_repaint(clock)
                verdicts.append(cs.check_for_update())
        self.assertEqual(
            verdicts, [True] * 5,
            "the update arrow vanished after the script was rewritten at the "
            "same commit, although the cached verdict was fresh")
        self.assertEqual(len(api.urls), 1, "a fresh verdict was asked for again")
        self.assertEqual(git.network_calls, [])

    def test_answering_from_the_cache_leaves_the_repaints_turn_free(self):
        clock = _FakeClock()
        api = _Api(github=[OTHER_SHA.encode()], npm=[NPM_LATEST])
        git = _Git(clock=clock, merge_base_rc=1)
        with _sandbox(api, git, clock):
            self.assertIs(cs.check_for_update(), True)
            self._rewrite_script()
            _next_repaint(clock)
            last = cs._read_stamp("_last")
            self.assertIs(cs.check_for_update(), True)
            self.assertEqual(cs._read_stamp("_last"), last,
                             "answering from the cache took the repaint's "
                             "network turn")
            self.assertEqual(cs._read_stamp("update_check_git"), 0.0,
                             "answering from the cache spent the round's git "
                             "leg without running it")
            self.assertIs(cs.check_claude_code_update("2.1.170"), True,
                          "the next check in the same repaint found its turn "
                          "taken by a check that never touched the network")
        self.assertEqual(len(api.calls_to("registry.npmjs.org")), 1)

    def test_rewrite_after_a_git_leg_verdict_keeps_it_for_the_rest_of_the_round(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("ok", OTHER_SHA)], clock=clock, merge_base_rc=1)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())  # API 403
            _next_repaint(clock)
            self.assertIs(cs.check_for_update(), True)  # git: both legs now spent
            self._rewrite_script()
            verdicts = []
            for _ in range(3):
                _next_repaint(clock)
                verdicts.append(cs.check_for_update())
        self.assertEqual(verdicts, [True] * 3,
                         "a spent round hid a fresh verdict for this very commit")
        self.assertEqual(len(git.network_calls), 1)
        self.assertEqual(len(api.urls), 1)

    def test_rewrite_keeps_the_verdict_while_another_check_has_the_turn(self):
        clock = _FakeClock()
        api = _Api(github=[OTHER_SHA.encode()], npm=[NPM_LATEST])
        git = _Git(clock=clock, merge_base_rc=1)
        with _sandbox(api, git, clock):
            self.assertIs(cs.check_for_update(), True)
            self._rewrite_script()
            _next_repaint(clock)
            self.assertIs(cs.check_claude_code_update("2.1.170"), True)  # takes the turn
            self.assertIs(cs.check_for_update(), True,
                          "a taken turn hid a fresh verdict for this very commit")
        self.assertEqual(len(api.calls_to("api.github.com")), 1)

    def test_moved_checkout_in_a_spent_round_spawns_nothing_past_rev_parse(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("fail",)], clock=clock)
        with _sandbox(api, git, clock) as td:
            cs.check_for_update()  # API 403
            _next_repaint(clock)
            cs.check_for_update()  # git fails: this round has nothing left
            # A fresh verdict for the commit the checkout was on before a pull.
            (td / "update_check.json").write_text(json.dumps({
                "timestamp": clock.now, "update_available": True,
                "local": OTHER_SHA[:8], "script_mtime": None}), encoding="utf-8")
            for _ in range(3):
                _next_repaint(clock)
                spawned, asked = len(git.calls), len(api.urls)
                self.assertIsNone(cs.check_for_update())
                subcommands = {args[0] for args, _ in git.calls[spawned:]}
                self.assertLessEqual(
                    subcommands, {"rev-parse"},
                    "with no leg left to run, a repaint went past finding out "
                    "that the cache was for another commit")
                self.assertEqual(len(api.urls), asked)

    def test_moved_checkout_asks_git_on_the_next_free_turn(self):
        clock = _FakeClock()
        api = _Api(github=[LOCAL_SHA.encode()])
        git = _Git(network=[("ok", OTHER_SHA)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIs(cs.check_for_update(), False)  # the API answers
            git.local = OTHER_SHA  # a pull: a new commit, and a new mtime
            self._rewrite_script()
            _next_repaint(clock)
            self.assertIs(cs.check_for_update(), False,
                          "the round's unused git leg should answer for the "
                          "new commit")
        self.assertEqual(len(api.calls_to("api.github.com")), 1)
        self.assertEqual(len(git.network_calls), 1)


class GitLegCadenceTest(unittest.TestCase):
    """U2: a round's git leg does not wait on when the previous round's ran.

    The round stamp already allows one git leg per round, and the git leg's
    write-ahead stamp is what makes a kill mid-leg count as that leg. A second
    backoff on the git stamp itself only pushed each round's git leg as late
    as the previous one had been.
    """

    CADENCE = cs._NET_MIN_SPACING + 1  # seconds between repaints with a free turn

    def test_next_rounds_git_leg_runs_on_its_first_free_repaint(self):
        for late in (10, 300, cs._NET_RETRY_BACKOFF - 2):
            with self.subTest(round_one_git_at=late):
                clock = _FakeClock()
                start = clock.now
                api = _Api(github=[403, 403])
                git = _Git(network=[("fail",), ("ok", LOCAL_SHA)], clock=clock)

                def repaint(at):
                    clock.now = start + at
                    _new_process()
                    return cs.check_for_update()

                with _sandbox(api, git, clock):
                    self.assertIsNone(repaint(0))     # round 1: API 403
                    self.assertIsNone(repaint(late))  # round 1: git fails
                    self.assertEqual(len(git.network_calls), 1)
                    round_two = cs._NET_RETRY_BACKOFF + self.CADENCE
                    self.assertIsNone(repaint(round_two))  # round 2: API 403
                    self.assertEqual(len(api.calls_to("api.github.com")), 2)
                    verdict = repaint(round_two + self.CADENCE)
                self.assertEqual(
                    len(git.network_calls), 2,
                    "round 2's git leg was held off because round 1's ran at "
                    "t+%ds" % late)
                self.assertIs(verdict, False)

    def test_a_git_leg_killed_mid_run_still_counts_as_the_rounds_git_leg(self):
        clock = _FakeClock()
        api = _Api(github=[403, LOCAL_SHA.encode()])
        git = _Git(network=[("kill",)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())  # API 403
            _next_repaint(clock)
            with self.assertRaises(_Killed):
                cs.check_for_update()  # cancelled inside git ls-remote
            for _ in range(3):
                _next_repaint(clock)
                spawned, asked = len(git.calls), len(api.urls)
                self.assertIsNone(cs.check_for_update())
                self.assertEqual(len(git.calls), spawned,
                                 "a repaint after a killed git leg spawned git "
                                 "again in the same round")
                self.assertEqual(len(api.urls), asked)
            clock.now += cs._NET_RETRY_BACKOFF
            _new_process()
            self.assertIs(cs.check_for_update(), False)  # a fresh round: the API
        self.assertEqual(len(git.network_calls), 1)
        self.assertEqual(len(api.calls_to("api.github.com")), 2)

    def test_git_leg_another_session_began_during_slow_local_git_is_not_rerun(self):
        clock = _FakeClock()

        class OtherSessionMeanwhile(_Git):
            """While this repaint waits on a slow local git, another session
            starts the round's git leg (its write-ahead stamps land)."""

            armed = False

            def __call__(self, cmd, **kwargs):
                if self.armed and "symbolic-ref" in cmd:
                    self.armed = False
                    cs._write_stamp("update_check_git", clock.now)
                    cs._write_stamp("_last", clock.now)
                    clock.now += cs._NET_MIN_SPACING + 0.1  # this git is slow
                return super().__call__(cmd, **kwargs)

        api = _Api(github=[403])
        git = OtherSessionMeanwhile(network=[("ok", LOCAL_SHA)], clock=clock)
        with _sandbox(api, git, clock):
            self.assertIsNone(cs.check_for_update())  # API 403
            _next_repaint(clock)
            git.armed = True
            self.assertIsNone(cs.check_for_update())
        self.assertEqual(git.network_calls, [],
                         "two sessions both ran this round's git leg")

    def test_git_stamp_from_before_a_clock_step_back_does_not_spend_the_leg(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("ok", LOCAL_SHA)], clock=clock)
        with _sandbox(api, git, clock):
            # Written a day "later", before the clock was set back.
            cs._write_stamp("update_check_git", clock.now + 86400)
            self.assertIsNone(cs.check_for_update())  # API 403
            _next_repaint(clock)
            self.assertIs(cs.check_for_update(), False,
                          "a stamp from the future held this round's git leg "
                          "off; it would hold every round's off for a day")
        self.assertEqual(len(git.network_calls), 1)

    def test_git_leg_reads_the_sha_from_its_output_file(self):
        clock = _FakeClock()
        api = _Api(github=[403])
        git = _Git(network=[("ok", OTHER_SHA)], clock=clock, merge_base_rc=1)
        with _sandbox(api, git, clock) as td:
            self.assertIsNone(cs.check_for_update())  # API 403
            _next_repaint(clock)
            self.assertIs(cs.check_for_update(), True)
            cached = json.loads((td / "update_check.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["remote"], OTHER_SHA[:8])
        (args, kwargs), = git.network_calls
        self.assertTrue(hasattr(kwargs.get("stdout"), "write"),
                        "network git must write to a file, not a pipe")
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertLessEqual(kwargs["timeout"], cs._NET_FETCH_BUDGET)


class InteractiveConfigTest(unittest.TestCase):
    """K1: --config must really check, not be refused by the render-path turns."""

    def _print_config(self, api, git):
        lines = []
        with _sandbox(api, git), \
                mock.patch.object(cs, "utf8_print", lambda s="": lines.append(str(s))), \
                mock.patch.object(cs, "load_config", lambda: {}), \
                mock.patch.object(cs, "get_credentials", lambda *a, **k: (None, None)):
            cs.cmd_print_config()
        return "\n".join(lines)

    def test_config_command_checks_both_with_cold_caches(self):
        api = _Api(github=[LOCAL_SHA.encode()], npm=[NPM_LATEST])
        git = _Git()
        text = self._print_config(api, git)
        claude_line = [ln for ln in text.splitlines() if "Claude Code:" in ln]
        self.assertEqual(len(claude_line), 1, text)
        self.assertNotIn("check failed", claude_line[0],
                         "--config reported a failure without asking npm")
        self.assertIn("9.9.9 available", claude_line[0])
        self.assertEqual(len(api.calls_to("registry.npmjs.org")), 1)
        self.assertIn("up to date", text)

    def test_config_command_asks_claude_for_its_version_once(self):
        """U4: --config has just read the version; the check must not ask again."""
        api = _Api(github=[LOCAL_SHA.encode()], npm=[NPM_LATEST])
        git = _Git()
        text = self._print_config(api, git)
        asked = [args for args, _ in git.calls if args == ["--version"]]
        self.assertEqual(len(asked), 1,
                         "--config ran 'claude --version' %d times" % len(asked))
        self.assertIn("2.1.170 → 9.9.9 available", text)

    def test_interactive_cannot_be_passed_by_position_to_the_claude_check(self):
        """U3: check_claude_code_update(ver, True) silently skipped the bar's gates."""
        api = _Api(npm=[NPM_LATEST])
        git = _Git()
        with _sandbox(api, git):
            with self.assertRaises(TypeError):
                cs.check_claude_code_update("2.1.170", True)
        self.assertEqual(api.urls, [])

    def test_interactive_cannot_be_passed_by_position_to_the_pulse_check(self):
        api = _Api(github=[LOCAL_SHA.encode()])
        git = _Git()
        with _sandbox(api, git):
            with self.assertRaises(TypeError):
                cs.check_for_update(True)
        self.assertEqual(api.urls, [])
        self.assertEqual(git.calls, [])

    def test_interactive_checks_back_to_back_both_answer(self):
        api = _Api(github=[LOCAL_SHA.encode()], npm=[NPM_LATEST])
        git = _Git()
        with _sandbox(api, git):
            self.assertIs(cs.check_for_update(interactive=True), False)
            self.assertIs(cs.check_claude_code_update("2.1.170", interactive=True), True)
        self.assertEqual(len(api.calls_to("registry.npmjs.org")), 1)

    def test_interactive_check_is_not_refused_by_a_recent_failure(self):
        api = _Api(npm=[503, NPM_LATEST])
        git = _Git()
        with _sandbox(api, git):
            self.assertIsNone(cs.check_claude_code_update("2.1.170"))  # bar: fails
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.170", interactive=True), True)
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.170"), True,
                          "the interactive answer must land in the shared cache")
        self.assertEqual(len(api.calls_to("registry.npmjs.org")), 2)

    def test_interactive_pulse_check_keeps_the_git_fallback(self):
        api = _Api(github=[403])
        git = _Git(network=[("ok", LOCAL_SHA)])
        with _sandbox(api, git):
            self.assertTrue(cs._begin_net_attempt("update_check"))  # the bar just tried
            self.assertIs(cs.check_for_update(interactive=True), False)
            _new_process()
            self.assertIs(cs.check_for_update(), False,
                          "the interactive verdict must land in the shared cache")
        self.assertEqual(len(api.calls_to("api.github.com")), 1)
        self.assertEqual(len(git.network_calls), 1)


class MalformedCacheTest(unittest.TestCase):
    """U6: an unreadable or malformed cache is a cache miss, never an error.

    On the bar a raise is swallowed (no arrow at all), but --config shows
    "check failed" for Claude Code, and it calls the Pulse check outside any
    handler, so a raise there ended the whole command.
    """

    @staticmethod
    def _bad_caches(local, script_mtime=None):
        good = {"timestamp": real_time.time(), "update_available": False,
                "local": local}
        if script_mtime is not None:
            good["script_mtime"] = script_mtime

        def variant(**changes):
            entry = dict(good)
            entry.update(changes)
            return json.dumps(entry).encode()

        return {
            "a JSON list": b"[1, 2]",
            "a JSON number": b"42",
            "a string timestamp": variant(timestamp="yesterday"),
            "a null timestamp": variant(timestamp=None),
            # Valid JSON whole numbers that float() can't hold: it raises
            # OverflowError, which is not a ValueError.
            "a timestamp too big for a float": variant(timestamp=10 ** 400),
            "a negative timestamp too big for a float": variant(timestamp=-10 ** 400),
            "a non-boolean verdict": variant(update_available="no"),
            "bytes that are not UTF-8": b"\xff\xfe{",
            "nesting too deep to parse": b"[" * 100000 + b"]" * 100000,
            "a directory, not a file": None,
        }

    @staticmethod
    def _plant(path, data):
        if data is None:
            path.mkdir()
        else:
            path.write_bytes(data)

    def test_claude_check_treats_a_bad_cache_as_a_miss(self):
        for case, data in self._bad_caches("2.1.170").items():
            for interactive in (False, True):
                with self.subTest(cache=case, interactive=interactive):
                    api = _Api(npm=[NPM_LATEST])
                    with _sandbox(api, _Git()) as td:
                        self._plant(td / "claude_code_update.json", data)
                        try:
                            verdict = cs.check_claude_code_update(
                                "2.1.170", interactive=interactive)
                        except Exception as exc:  # noqa: BLE001 - the defect itself
                            self.fail("a bad cache escaped as %s: %s"
                                      % (type(exc).__name__, exc))
                    self.assertIs(verdict, True)
                    self.assertEqual(len(api.calls_to("registry.npmjs.org")), 1)

    def test_pulse_check_treats_a_bad_cache_as_a_miss(self):
        mtime = Path(cs.__file__).resolve().stat().st_mtime
        for case, data in self._bad_caches(LOCAL_SHA[:8], mtime).items():
            for interactive in (False, True):
                with self.subTest(cache=case, interactive=interactive):
                    api = _Api(github=[OTHER_SHA.encode()])
                    with _sandbox(api, _Git(merge_base_rc=1)) as td:
                        self._plant(td / "update_check.json", data)
                        try:
                            verdict = cs.check_for_update(interactive=interactive)
                        except Exception as exc:  # noqa: BLE001 - the defect itself
                            self.fail("a bad cache escaped as %s: %s"
                                      % (type(exc).__name__, exc))
                    self.assertIs(verdict, True)
                    self.assertEqual(len(api.calls_to("api.github.com")), 1)

    def test_config_command_survives_corrupt_caches(self):
        mtime = Path(cs.__file__).resolve().stat().st_mtime
        # Each verdict is the opposite of what the network will say, and the
        # rest of the entry matches, so a cache trusted instead of skipped
        # fails the assertions as surely as one that raises.
        too_big = {
            "update_check.json": json.dumps({
                "timestamp": 10 ** 400, "update_available": True,
                "local": LOCAL_SHA[:8], "script_mtime": mtime}).encode(),
            "claude_code_update.json": json.dumps({
                "timestamp": 10 ** 400, "update_available": False,
                "local": "2.1.170"}).encode(),
        }
        corruptions = {
            "bytes that are not UTF-8": dict.fromkeys(too_big, b"\xff\xfe{"),
            "a timestamp too big for a float": too_big,
        }
        for case, files in corruptions.items():
            with self.subTest(cache=case):
                api = _Api(github=[LOCAL_SHA.encode()], npm=[NPM_LATEST])
                lines = []
                with _sandbox(api, _Git()) as td, \
                        mock.patch.object(cs, "utf8_print", lambda s="": lines.append(str(s))), \
                        mock.patch.object(cs, "load_config", lambda: {}), \
                        mock.patch.object(cs, "get_credentials", lambda *a, **k: (None, None)):
                    for name, data in files.items():
                        (td / name).write_bytes(data)
                    try:
                        cs.cmd_print_config()
                    except Exception as exc:  # noqa: BLE001 - the defect itself
                        self.fail("--config crashed with %s: %s"
                                  % (type(exc).__name__, exc))
                text = "\n".join(lines)
                self.assertNotIn("check failed", text)
                self.assertIn("up to date (v", text)
                self.assertIn("9.9.9 available", text)


class ApiLegCeilingTest(unittest.TestCase):
    """C12: the GitHub API leg is never handed more than _NET_FETCH_BUDGET."""

    def _budgets(self, call):
        seen = []

        def recorder(req, max_bytes, budget=None):
            seen.append(budget)
            return LOCAL_SHA.encode()

        with mock.patch.object(cs, "_fetch_bounded", recorder), \
                mock.patch.object(cs, "_git", side_effect=AssertionError("git ran")):
            self.assertEqual(call(), LOCAL_SHA)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], "the API leg got no ceiling at all")
        return seen[0]

    def test_api_leg_of_the_chain_is_held_to_the_fetch_ceiling(self):
        for deadline in (real_time.time() + cs._UPDATE_CHECK_BUDGET, None):
            with self.subTest(deadline=deadline):
                budget = self._budgets(lambda: cs.get_remote_commit(deadline=deadline))
                self.assertLessEqual(
                    budget, cs._NET_FETCH_BUDGET,
                    "the GitHub API leg was handed %.1fs" % budget)

    def test_status_line_api_leg_is_held_to_the_fetch_ceiling(self):
        budget = self._budgets(lambda: cs.get_remote_commit(
            deadline=real_time.time() + cs._UPDATE_CHECK_BUDGET, leg="api"))
        self.assertLessEqual(budget, cs._NET_FETCH_BUDGET)

    def test_status_line_check_hands_its_api_leg_the_fetch_ceiling(self):
        seen = []

        def recorder(req, max_bytes, budget=None):
            seen.append(budget)
            return LOCAL_SHA.encode()

        with _sandbox(_Api(), _Git()), mock.patch.object(cs, "_fetch_bounded", recorder):
            self.assertIs(cs.check_for_update(), False)
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], cs._NET_FETCH_BUDGET)


class LsRemoteEnvironmentTest(unittest.TestCase):
    def test_ls_remote_never_prompts_and_reaps_a_stalled_helper(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(cs, "_git", return_value="") as git:
            for key in ("GIT_HTTP_LOW_SPEED_LIMIT", "GIT_HTTP_LOW_SPEED_TIME"):
                os.environ.pop(key, None)
            cs._git_remote_commit(deadline=None)
        env = git.call_args[1]["env"]
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(env.get("GIT_HTTP_LOW_SPEED_LIMIT"), "1")
        self.assertGreaterEqual(int(env.get("GIT_HTTP_LOW_SPEED_TIME", "0")),
                                git.call_args[1]["timeout"],
                                "the low-speed abort must never cut short a call "
                                "its caller is still waiting for")

    def test_users_own_low_speed_setting_is_kept(self):
        with mock.patch.dict(os.environ, {"GIT_HTTP_LOW_SPEED_LIMIT": "500",
                                          "GIT_HTTP_LOW_SPEED_TIME": "30"}), \
                mock.patch.object(cs, "_git", return_value="") as git:
            cs._git_remote_commit(deadline=None)
        env = git.call_args[1]["env"]
        self.assertEqual(env["GIT_HTTP_LOW_SPEED_LIMIT"], "500")
        self.assertEqual(env["GIT_HTTP_LOW_SPEED_TIME"], "30")


# --- Real processes: loopback only, never a real remote ---------------------

_SLACK = 1.0  # seconds past the timeout for spawn + kill + reap on a busy box

_HANGING_GIT = textwrap.dedent("""\
    # A stand-in for git: like git with its remote helper, it hands its own
    # stdout and stderr to a child that outlives it, then hangs.
    import subprocess, sys, tempfile, time
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(%(grandchild)d)"],
        stdout=sys.stdout, stderr=sys.stderr, cwd=tempfile.gettempdir())
    with open(%(pidfile)r, "w") as f:
        f.write(str(child.pid))
    time.sleep(60)
""")

_ANSWERING_GIT = textwrap.dedent("""\
    import sys
    sys.stdout.write("%(sha)s\\trefs/heads/main\\n")
    sys.stderr.write("noise on stderr\\n")
""")


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _rmtree_patiently(path, seconds=5.0):
    """Windows refuses to delete a dir while an exiting process still uses it."""
    end = real_time.monotonic() + seconds
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if real_time.monotonic() > end:
                return  # leave it to the OS temp cleanup rather than fail the test
            real_time.sleep(0.1)


class FakeGitProcessTest(unittest.TestCase):
    """A real child process stands in for git (sys.executable runs a script
    named ``ls-remote`` from the install dir), so this runs on every OS."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="pulse-fakegit-")
        self.addCleanup(_rmtree_patiently, self.td)
        for p in (mock.patch.object(cs, "__file__", os.path.join(self.td, "claude_status.py")),
                  mock.patch.object(cs, "_GIT_PATH", sys.executable)):
            p.start()
            self.addCleanup(p.stop)

    def _install(self, source):
        with open(os.path.join(self.td, "ls-remote"), "w", encoding="utf-8") as f:
            f.write(source)

    def test_helper_holding_the_childs_stdio_cannot_stall_the_caller(self):
        pidfile = os.path.join(self.td, "grandchild.pid")
        self._install(_HANGING_GIT % {"grandchild": 4, "pidfile": pidfile})

        def reap():
            try:
                with open(pidfile, encoding="utf-8") as f:
                    _kill_quietly(int(f.read().strip()))
            except (OSError, ValueError):
                pass
        self.addCleanup(reap)

        timeout = 1.0
        started = real_time.monotonic()
        result = cs._git_remote_commit(deadline=real_time.time() + timeout)
        elapsed = real_time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(
            elapsed, timeout + _SLACK,
            "_git returned after %.2fs against a %.1fs timeout: it waited for "
            "a helper that inherited the child's stdio" % (elapsed, timeout))

    def test_answer_is_read_back_and_stderr_ignored(self):
        self._install(_ANSWERING_GIT % {"sha": OTHER_SHA})
        self.assertEqual(cs._git_remote_commit(deadline=real_time.time() + 10), OTHER_SHA)


@unittest.skipUnless(sys.platform == "win32" and shutil.which("git"),
                     "the inherited-pipe stall is Windows behaviour; needs git")
class RealGitHungRemoteTest(unittest.TestCase):
    """Real git against a loopback server that accepts and never answers."""

    def setUp(self):
        # Cleanups run last-registered-first: the server must close (so git's
        # abandoned helper sees a reset and exits) before the repo is removed.
        self.repo = tempfile.mkdtemp(prefix="pulse-loopback-")
        self.addCleanup(_rmtree_patiently, self.repo)

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.held = []
        self.stop = threading.Event()
        port = self.server.getsockname()[1]
        self.url = "http://127.0.0.1:%d/r.git" % port

        def serve():
            end = real_time.monotonic() + 10  # caps the hang in a RED run
            while not self.stop.is_set() and real_time.monotonic() < end:
                try:
                    conn, _ = self.server.accept()
                    self.held.append(conn)
                except OSError:
                    pass
            for conn in self.held:
                conn.close()
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)

        env = self._git_env()
        git = shutil.which("git")
        subprocess.run([git, "init", "-q", self.repo], env=env, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run([git, "-C", self.repo, "remote", "add", "origin", self.url],
                       env=env, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        for p in (mock.patch.object(cs, "__file__", os.path.join(self.repo, "claude_status.py")),
                  mock.patch.object(cs, "_GIT_PATH", git),
                  mock.patch.dict(os.environ, env, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _git_env():
        """The caller's environment minus anything that reaches outside the test.

        No proxy, no system or global git config (GIT_CONFIG_GLOBAL needs git
        2.32+; older git ignores it), and no inherited GIT_* variable: a git
        hook exports GIT_DIR and friends, which would aim every command here
        at the hook's own repository. git's test suite scrubs GIT_* the same way.
        """
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")
               and not k.upper().startswith("GIT_")}
        env.update(NO_PROXY="127.0.0.1", no_proxy="127.0.0.1",
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0")
        return env

    def _close_server(self):
        self.stop.set()
        self.thread.join(5)
        self.server.close()

    def test_hung_remote_cannot_hold_git_past_its_timeout(self):
        timeout = cs._NET_FETCH_BUDGET
        started = real_time.monotonic()
        result = cs._git_remote_commit(deadline=real_time.time() + timeout)
        elapsed = real_time.monotonic() - started
        self.assertIsNone(result)
        self.assertTrue(self.held, "git never reached the loopback remote")
        self.assertLess(
            elapsed, timeout + _SLACK,
            "git ls-remote held the caller %.2fs against a %.1fs timeout"
            % (elapsed, timeout))


def _git_version():
    git = shutil.which("git")
    if not git:
        return None
    try:
        out = subprocess.run([git, "--version"], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=30).stdout
        return tuple(int(part) for part in out.split()[2].split(".")[:2])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


@unittest.skipUnless((_git_version() or (0, 0)) >= (2, 32),
                     "needs git 2.32+, the first with GIT_CONFIG_GLOBAL")
class RealGitEnvironmentTest(unittest.TestCase):
    """U7: the real-git tests' environment ignores the developer's own git setup.

    Run straight from a shell (as the module docstring suggests) the tests
    would otherwise read the real ~/.gitconfig; run from a git hook they would
    inherit GIT_DIR, which points every git command at the hook's repository.
    """

    def setUp(self):
        self.git = shutil.which("git")
        self.home = tempfile.mkdtemp(prefix="pulse-gitenv-")
        self.addCleanup(_rmtree_patiently, self.home)
        self.xdg = os.path.join(self.home, "xdg")
        os.makedirs(os.path.join(self.xdg, "git"))
        for path in (os.path.join(self.home, ".gitconfig"),
                     os.path.join(self.xdg, "git", "config")):
            with open(path, "w", encoding="utf-8") as f:
                f.write("[pulse]\n\tmarker = leaked\n")

    def _env(self, **extra):
        planted = dict(HOME=self.home, USERPROFILE=self.home, XDG_CONFIG_HOME=self.xdg)
        planted.update(extra)
        with mock.patch.dict(os.environ, planted):
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
            return RealGitHungRemoteTest._git_env()

    def _git(self, env, *args):
        return subprocess.run([self.git, *args], env=env, cwd=self.home,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)

    def test_the_users_global_git_config_is_not_read(self):
        env = self._env()
        leaky = {k: v for k, v in env.items() if k != "GIT_CONFIG_GLOBAL"}
        seen = self._git(leaky, "config", "--get", "pulse.marker")
        self.assertEqual(seen.stdout.strip(), "leaked",
                         "precondition: git reads the planted global config")
        result = self._git(env, "config", "--get", "pulse.marker")
        self.assertEqual((result.returncode, result.stdout.strip()), (1, ""),
                         "the real-git tests read the user's global git config")

    def test_a_git_dir_from_the_caller_does_not_leak_in(self):
        decoy = os.path.join(self.home, "decoy")
        fresh = os.path.join(self.home, "fresh")
        os.makedirs(fresh)
        clean = self._env()
        for repo in (decoy, fresh):
            subprocess.run([self.git, "init", "-q", repo], env=clean, check=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)
        env = self._env(GIT_DIR=os.path.join(decoy, ".git"))
        result = self._git(env, "-C", fresh, "rev-parse", "--absolute-git-dir")
        self.assertEqual(os.path.normcase(os.path.realpath(result.stdout.strip())),
                         os.path.normcase(os.path.realpath(os.path.join(fresh, ".git"))),
                         "a GIT_DIR inherited from the caller (a git hook "
                         "exports one) pointed the test's git at another repo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
