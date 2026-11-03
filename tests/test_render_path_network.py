"""A network check on the render path must never hold the bar hostage.

Claude Code cancels an in-flight status line script whenever the next update
fires, and with ``refreshInterval`` set that is every N seconds. A repaint that
blocks on the network for longer than that is killed before it prints — and
because it is also killed before it can record that a check happened, the very
next repaint walks into the same stall. The bar never renders again.

Seen in the wild on a network that advertises IPv6 but black-holes it:
registry.npmjs.org publishes 12 AAAA + 12 A records, urllib's ``timeout=3``
applies *per address*, so the hourly Claude Code update check stalled for
12 x 3s = 36s before reaching the first IPv4 address — against a 2s refresh.
Every repaint was killed at ~2s, none ever wrote the cache, and the bar stayed
blank until something ran the script outside Claude Code.

Three properties keep a repaint safe, each pinned below:

1. An attempt is recorded BEFORE the network is touched, so a repaint that is
   killed (or fails) mid-check is not retried by the next one.
2. One fetch has a hard wall-clock ceiling that covers DNS and every address,
   not a timeout that restarts for each address.
3. Address families are interleaved, so a dead family costs one short attempt
   instead of one full timeout per address — the check still *succeeds*.

Run: ``python tests/test_render_path_network.py``
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time as real_time
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

LOCAL_SHA = "a" * 40
GBP = "£"


class _Killed(BaseException):
    """Stands in for Claude Code cancelling the script mid-check.

    A BaseException so that, like a real kill, no ``except Exception`` handler
    gets to run any cleanup or bookkeeping after the network call.
    """


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._body


class _Network:
    """A scriptable network that records every time the code reaches for it.

    Patches both the transport the render path used before the fix
    (``urllib.request.urlopen``) and the bounded one it must use after
    (``_fetch_bounded``), so each test observes real behaviour either way.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)  # per call: bytes | "fail" | "kill"
        self.calls = []

    def _next(self, url):
        self.calls.append(url)
        return self.outcomes.pop(0) if self.outcomes else "fail"

    def urlopen(self, req, timeout=None):
        outcome = self._next(req.full_url)
        if outcome == "kill":
            raise _Killed()
        if outcome == "fail":
            raise OSError("network unreachable")
        return _FakeResponse(outcome)

    def fetch_bounded(self, req, max_bytes, budget=None):
        outcome = self._next(req.full_url)
        if outcome == "kill":
            raise _Killed()
        if outcome == "fail":
            return None
        return outcome[:max_bytes]


def _confine_writes(td):
    """Patches that fail the test if the code under test writes outside *td*.

    main() syncs ``statusLine.refreshInterval`` and prunes legacy hooks in
    Claude Code's settings.json. Pointed at the developer's real config dir,
    a test run rewrites their live settings — so the config dir is moved into
    the sandbox as well, and any write that still escapes is an error.
    """
    root = Path(td).resolve()

    def guarded(real):
        def wrapper(filepath, *args, **kwargs):
            target = Path(filepath).resolve()
            if root != target and root not in target.parents:
                raise AssertionError(f"test wrote outside its sandbox: {target}")
            return real(filepath, *args, **kwargs)
        return wrapper

    return [
        mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(root)}),
        mock.patch.object(cs, "_atomic_json_write", guarded(cs._atomic_json_write)),
        mock.patch.object(cs, "_secure_open_write", guarded(cs._secure_open_write)),
    ]


@contextmanager
def _sandbox(network, clock=None, git_branch="main"):
    """Isolated state dir + scripted network (+ optional fake clock and git)."""

    def fake_run(cmd, **kwargs):
        # subprocess.run's real surface: any keyword argument, with the output
        # delivered wherever stdout points (a pipe, or the temp file network
        # git writes to). Network git never starts a process: it is one more
        # call on the scripted network, so a test's count covers it as well.
        if any(sub in cmd for sub in ("ls-remote", "fetch", "pull", "clone", "push")):
            outcome = network._next(" ".join(str(part) for part in cmd))
            if outcome == "kill":
                raise _Killed()
            if outcome == "fail":
                returncode, out = 128, ""
            else:
                sha = outcome.decode("utf-8", "replace").strip()
                returncode, out = 0, f"{sha}\trefs/heads/main\n"
        elif "symbolic-ref" in cmd:
            returncode, out = 0, git_branch
        elif "rev-parse" in cmd:
            returncode, out = 0, LOCAL_SHA
        elif "get-url" in cmd:
            returncode, out = 0, f"https://github.com/{cs.GITHUB_REPO}"
        else:
            returncode, out = 0, ""
        text = any(kwargs.get(k) for k in ("text", "universal_newlines", "encoding", "errors"))
        captured = kwargs.get("capture_output") or kwargs.get("stdout") is cs.subprocess.PIPE
        dest = kwargs.get("stdout")
        if hasattr(dest, "write"):
            dest.write(out.encode("utf-8"))
            out = None
        elif captured:
            out = out if text else out.encode("utf-8")
        else:
            out = None  # not captured: it would have gone to our own stdout
        err = ("" if text else b"") if kwargs.get("capture_output") else None
        return SimpleNamespace(returncode=returncode, stdout=out, stderr=err)

    with tempfile.TemporaryDirectory() as td:
        patches = [
            mock.patch.object(cs, "get_state_dir", lambda: Path(td)),
            mock.patch.object(cs.urllib.request, "urlopen", network.urlopen),
            mock.patch.object(cs, "_fetch_bounded", network.fetch_bounded,
                              create=True),
            mock.patch.object(cs.subprocess, "run", fake_run),
            mock.patch.dict(cs._exchange_rate_mem, {}, clear=True),
            mock.patch.dict(getattr(cs, "_own_stamps", {}), {}, clear=True),
        ] + _confine_writes(td)
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
    cs._exchange_rate_mem.clear()
    getattr(cs, "_own_stamps", {}).clear()


NPM_LATEST = json.dumps({"version": "9.9.9"}).encode()
FX_LATEST = json.dumps({"rates": {"GBP": 0.5}}).encode()


class ClaudeUpdateCheckTrapTest(unittest.TestCase):
    def test_killed_check_is_not_retried_by_the_next_repaint(self):
        net = _Network(["kill"])
        with _sandbox(net):
            with self.assertRaises(_Killed):
                cs.check_claude_code_update("2.1.278")
            _new_process()
            cs.check_claude_code_update("2.1.278")
        self.assertEqual(
            len(net.calls), 1,
            "a repaint killed mid-check left no trace, so the next repaint "
            "walked straight back into the same stall — the bar can never "
            "render while the network is slow",
        )

    def test_failed_check_is_not_retried_by_the_next_repaint(self):
        net = _Network(["fail"])
        with _sandbox(net):
            self.assertIsNone(cs.check_claude_code_update("2.1.278"))
            _new_process()
            self.assertIsNone(cs.check_claude_code_update("2.1.278"))
        self.assertEqual(len(net.calls), 1,
                         "a failed check must back off, not retry per repaint")

    def test_check_is_retried_once_the_backoff_has_passed(self):
        clock = _FakeClock()
        net = _Network(["fail", NPM_LATEST])
        with _sandbox(net, clock=clock):
            self.assertIsNone(cs.check_claude_code_update("2.1.278"))
            clock.now += cs._NET_RETRY_BACKOFF + 1
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.278"), True)
        self.assertEqual(len(net.calls), 2)

    def test_successful_check_still_caches_its_verdict(self):
        net = _Network([NPM_LATEST])
        with _sandbox(net):
            self.assertIs(cs.check_claude_code_update("2.1.278"), True)
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.278"), True)
        self.assertEqual(len(net.calls), 1)


class ExchangeRateTrapTest(unittest.TestCase):
    def test_killed_fetch_is_not_retried_by_the_next_repaint(self):
        net = _Network(["kill"])
        with _sandbox(net):
            with self.assertRaises(_Killed):
                cs._get_exchange_rate(GBP)
            _new_process()
            cs._get_exchange_rate(GBP)
        self.assertEqual(len(net.calls), 1)

    def test_failed_fetch_is_not_retried_by_the_next_repaint(self):
        net = _Network(["fail"])
        with _sandbox(net):
            rate, code = cs._get_exchange_rate(GBP)
            self.assertEqual((rate, code), (cs._FALLBACK_RATES["GBP"], "GBP"))
            _new_process()
            rate, code = cs._get_exchange_rate(GBP)
            self.assertEqual((rate, code), (cs._FALLBACK_RATES["GBP"], "GBP"))
        self.assertEqual(len(net.calls), 1)

    def test_failed_refresh_keeps_serving_the_last_real_rate(self):
        clock = _FakeClock()
        net = _Network([FX_LATEST, "fail"])
        with _sandbox(net, clock=clock):
            self.assertEqual(cs._get_exchange_rate(GBP), (0.5, "GBP"))
            clock.now += cs._EXCHANGE_RATE_TTL + 1
            _new_process()
            self.assertEqual(
                cs._get_exchange_rate(GBP), (0.5, "GBP"),
                "a rate that was real yesterday beats a constant baked into "
                "the script",
            )

    def test_fetch_is_retried_once_the_backoff_has_passed(self):
        clock = _FakeClock()
        net = _Network(["fail", FX_LATEST])
        with _sandbox(net, clock=clock):
            cs._get_exchange_rate(GBP)
            clock.now += cs._NET_RETRY_BACKOFF + 1
            _new_process()
            self.assertEqual(cs._get_exchange_rate(GBP), (0.5, "GBP"))
        self.assertEqual(len(net.calls), 2)


class PulseUpdateCheckTrapTest(unittest.TestCase):
    def test_killed_check_is_not_retried_by_the_next_repaint(self):
        net = _Network(["kill"])
        with _sandbox(net):
            with self.assertRaises(_Killed):
                cs.check_for_update()
            _new_process()
            cs.check_for_update()
        self.assertEqual(len(net.calls), 1)

    def test_failed_check_is_not_retried_by_the_next_repaint(self):
        net = _Network(["fail"])
        with _sandbox(net):
            self.assertIsNone(cs.check_for_update())
            _new_process()
            self.assertIsNone(cs.check_for_update())
        self.assertEqual(len(net.calls), 1)

    def test_check_is_retried_once_the_backoff_has_passed(self):
        clock = _FakeClock()
        net = _Network(["fail", LOCAL_SHA.encode()])
        with _sandbox(net, clock=clock):
            self.assertIsNone(cs.check_for_update())
            clock.now += cs._NET_RETRY_BACKOFF + 1
            _new_process()
            self.assertIs(cs.check_for_update(), False)
        self.assertEqual(len(net.calls), 2)

    def test_git_leg_runs_alone_on_the_next_free_repaint_and_its_sha_decides(self):
        clock = _FakeClock()
        net = _Network(["fail", LOCAL_SHA.encode()])  # the API fails; git answers
        with _sandbox(net, clock=clock):
            self.assertIsNone(cs.check_for_update())
            self.assertEqual(len(net.calls), 1, "git ran in the API leg's repaint")
            clock.now += cs._NET_MIN_SPACING + 0.1
            _new_process()
            self.assertIs(cs.check_for_update(), False)
        self.assertEqual(len(net.calls), 2)
        self.assertIn("ls-remote", net.calls[1])

    def test_killed_git_leg_is_not_retried_by_the_next_repaint(self):
        clock = _FakeClock()
        net = _Network(["fail", "kill"])
        with _sandbox(net, clock=clock):
            self.assertIsNone(cs.check_for_update())
            clock.now += cs._NET_MIN_SPACING + 0.1
            _new_process()
            with self.assertRaises(_Killed):
                cs.check_for_update()
            clock.now += cs._NET_MIN_SPACING + 0.1
            _new_process()
            self.assertIsNone(cs.check_for_update())
        self.assertEqual(len(net.calls), 2)


class OneNetworkOperationPerRepaintTest(unittest.TestCase):
    """Each check fits inside a refresh interval; two in a row do not.

    The update checks share a one-hour TTL, so once they lapse together they
    lapse together forever — and a repaint that runs both back to back (~3s
    measured) is cancelled at the 2s refresh, every hour.
    """

    def test_second_check_in_the_same_repaint_waits_for_a_later_one(self):
        clock = _FakeClock()
        net = _Network([LOCAL_SHA.encode(), NPM_LATEST])
        with _sandbox(net, clock=clock):
            self.assertIs(cs.check_for_update(), False)
            clock.now += 1.0  # same repaint, a second later
            self.assertIsNone(cs.check_claude_code_update("2.1.278"))
            self.assertEqual(len(net.calls), 1,
                             "one repaint must not stack network checks")
            clock.now += cs._NET_MIN_SPACING  # a later repaint
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.278"), True)
        self.assertEqual(len(net.calls), 2)

    def test_the_held_back_check_is_not_put_into_backoff(self):
        clock = _FakeClock()
        net = _Network([LOCAL_SHA.encode(), NPM_LATEST])
        with _sandbox(net, clock=clock):
            cs.check_for_update()
            cs.check_claude_code_update("2.1.278")  # held back, not failed
            clock.now += cs._NET_MIN_SPACING
            self.assertLess(cs._NET_MIN_SPACING, cs._NET_RETRY_BACKOFF)
            self.assertIs(cs.check_claude_code_update("2.1.278"), True)

    def test_usage_fetch_is_never_held_back(self):
        """Usage is the bar itself; only the optional extras take turns."""
        net = _Network([])
        with _sandbox(net):
            self.assertTrue(cs._begin_net_attempt("claude_code_update"))
            self.assertTrue(cs._begin_net_attempt("usage", 60, primary=True))


class _ReachedLiveAddress(Exception):
    """Raised by a fake live connect: proof the connect phase got that far,
    without having to fake TLS on top of it."""


class _FakeSocket:
    """IPv6 is black-holed (eats its whole timeout); IPv4 answers in 30ms."""

    def __init__(self, world, family):
        self.world = world
        self.family = family
        self.timeout = None
        self.closed = False
        world.sockets.append(self)

    def settimeout(self, value):
        self.timeout = value

    def bind(self, address):
        pass

    def connect(self, sockaddr):
        self.world.attempts.append(self.family)
        latency = self.world.latency.get(self.family, 0.03)
        if self.family in self.world.dead_families or (
                self.timeout is not None and self.timeout < latency):
            self.world.clock.now += self.timeout
            raise socket.timeout("timed out")
        self.world.clock.now += latency
        if self.world.stop_at_live_connect:
            raise _ReachedLiveAddress()

    def close(self):
        self.closed = True


class _FakeWorld:
    def __init__(self, dead_families, stop_at_live_connect=False, latency=None):
        self.clock = _FakeClock()
        self.dead_families = dead_families
        self.stop_at_live_connect = stop_at_live_connect
        self.latency = latency or {}
        self.attempts = []
        self.sockets = []

    def getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        v6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "",
               (f"2606:4700::6810:{i:x}22", port, 0, 0)) for i in range(12)]
        v4 = [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
               (f"104.16.{i}.34", port)) for i in range(12)]
        return v6 + v4  # the order Windows hands back: every AAAA first

    def socket(self, family, type_=None, proto=None):
        return _FakeSocket(self, family)

    @contextmanager
    def installed(self):
        with mock.patch.object(cs, "time", self.clock), \
                mock.patch.object(cs.socket, "getaddrinfo", self.getaddrinfo), \
                mock.patch.object(cs.socket, "socket", self.socket):
            yield


class BoundedConnectorTest(unittest.TestCase):
    def test_dead_ipv6_costs_one_short_attempt_not_one_timeout_per_address(self):
        world = _FakeWorld(dead_families={socket.AF_INET6})
        with world.installed():
            start = world.clock.time()
            connect = cs._bounded_connector(start + cs._NET_FETCH_BUDGET)
            sock = connect(("registry.npmjs.org", 443), 3)
            elapsed = world.clock.time() - start
        self.assertEqual(sock.family, socket.AF_INET)
        self.assertLessEqual(
            world.attempts.index(socket.AF_INET), 1,
            "IPv4 must be reached by the second attempt — trying all 12 dead "
            "IPv6 addresses first is the 36-second stall",
        )
        self.assertLess(elapsed, cs._NET_FETCH_BUDGET)
        self.assertTrue(all(s.closed for s in world.sockets if s is not sock),
                        "sockets from failed attempts must be closed")

    def test_unreachable_host_gives_up_within_the_budget(self):
        world = _FakeWorld(dead_families={socket.AF_INET6, socket.AF_INET})
        with world.installed():
            start = world.clock.time()
            connect = cs._bounded_connector(start + cs._NET_FETCH_BUDGET)
            with self.assertRaises(OSError):
                connect(("registry.npmjs.org", 443), 3)
            elapsed = world.clock.time() - start
        self.assertLessEqual(
            elapsed, cs._NET_FETCH_BUDGET + 0.01,
            f"24 dead addresses consumed {elapsed:.1f}s — the ceiling must "
            "cover the whole connect, not restart for each address",
        )


class SlowButWorkingConnectionTest(unittest.TestCase):
    def test_slow_address_connects_when_the_caller_allows_the_time(self):
        """The short attempt cap exists to step past dead addresses quickly,
        not to outlaw slow ones: a link that needs 600ms to connect must still
        work for a caller (the usage API) that allows 10s."""
        world = _FakeWorld(dead_families={socket.AF_INET6},
                           latency={socket.AF_INET: 0.6})
        with world.installed():
            start = world.clock.time()
            connect = cs._bounded_connector(start + 10)
            sock = connect(("api.anthropic.com", 443), 10)
            elapsed = world.clock.time() - start
        self.assertEqual(sock.family, socket.AF_INET)
        self.assertLess(elapsed, 3.0,
                        "the dead family must not eat the patient pass either")


class RenderPathWiringTest(unittest.TestCase):
    """No transport mocks here: each call site runs for real over fake
    sockets, so a site left on plain ``urlopen`` (one full timeout per dead
    address) shows up as IPv4 being reached late."""

    def _assert_reaches_ipv4_promptly(self, call):
        world = _FakeWorld(dead_families={socket.AF_INET6},
                           stop_at_live_connect=True)
        with tempfile.TemporaryDirectory() as td, world.installed(), \
                mock.patch.object(cs, "get_state_dir", lambda: Path(td)), \
                mock.patch.dict(cs._exchange_rate_mem, {}, clear=True), \
                mock.patch.dict(getattr(cs, "_own_stamps", {}), {}, clear=True):
            start = world.clock.time()
            call(start)
            elapsed = world.clock.time() - start
        self.assertIn(socket.AF_INET, world.attempts,
                      "the call never reached the network at all")
        self.assertLessEqual(world.attempts.index(socket.AF_INET), 1)
        self.assertLess(elapsed, 1.0)

    def test_pulse_update_check(self):
        """The status line's own call, check_for_update(), down to the sockets.

        Local git is answered by a fake and network git fails closed: no real
        git can start, and any attempt at one is recorded (the raise alone
        would be swallowed by _git). The repaint's one network leg is the
        GitHub API over the bounded connect; git ls-remote has no place behind
        it, and from a checkout it would reach the real origin."""
        spawned = []

        def local_git_only(cmd, **kwargs):
            args = list(cmd[1:])
            spawned.append(args)
            sub = args[0] if args else ""
            if sub in ("ls-remote", "fetch", "pull", "clone", "push"):
                raise AssertionError(f"network git from a wiring test: {args}")
            out = {"rev-parse": LOCAL_SHA, "symbolic-ref": "main",
                   "remote": f"https://github.com/{cs.GITHUB_REPO}"}.get(sub, "")
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        verdicts = []
        with mock.patch.object(cs.subprocess, "run", local_git_only):
            self._assert_reaches_ipv4_promptly(
                lambda start: verdicts.append(cs.check_for_update()))
        network_git = [a for a in spawned
                       if a and a[0] in ("ls-remote", "fetch", "pull", "clone", "push")]
        self.assertEqual(network_git, [],
                         "the repaint ran network git behind its API leg")
        self.assertIn(["rev-parse", "HEAD"], spawned,
                      "the check never got as far as asking local git")
        self.assertEqual(verdicts, [None],
                         "the fake network never answers, so there is no verdict")

    def test_claude_code_update_check(self):
        self._assert_reaches_ipv4_promptly(
            lambda start: cs.check_claude_code_update("2.1.278"))

    def test_exchange_rate(self):
        self._assert_reaches_ipv4_promptly(
            lambda start: cs._get_exchange_rate(GBP))


class ConcurrentSessionsTest(unittest.TestCase):
    """Two Claude Code sessions share one state dir."""

    def test_a_stamp_written_by_another_session_mid_attempt_survives(self):
        fired = []

        def other_session_gets_in_first(real):
            def wrapper(*args, **kwargs):
                if not fired:
                    fired.append(1)
                    # Session B arms the usage kill guard in the gap between
                    # session A reading the stamps and writing its own.
                    assert cs._begin_net_attempt("usage", 60, primary=True)
                return real(*args, **kwargs)
            return wrapper

        with _sandbox(_Network([])):
            seams = [mock.patch.object(
                cs, "_atomic_json_write",
                other_session_gets_in_first(cs._atomic_json_write))]
            if hasattr(cs, "_write_stamp"):
                seams.append(mock.patch.object(
                    cs, "_write_stamp",
                    other_session_gets_in_first(cs._write_stamp)))
            for seam in seams:
                seam.start()
            try:
                self.assertTrue(cs._begin_net_attempt("claude_code_update"))
            finally:
                for seam in reversed(seams):
                    seam.stop()
            self.assertTrue(fired)
            self.assertFalse(
                cs._begin_net_attempt("usage", 60, primary=True),
                "session A's write erased session B's live kill guard",
            )

    def test_finishing_one_fetch_does_not_disarm_another_sessions_guard(self):
        clock = _FakeClock()
        with _sandbox(_Network([]), clock=clock):
            own = getattr(cs, "_own_stamps", {})
            self.assertTrue(cs._begin_net_attempt("usage", 60, primary=True))
            session_a = dict(own)
            clock.now += 61  # A is still waiting on its request...
            own.clear()      # ...when session B, a separate process,
            self.assertTrue(cs._begin_net_attempt("usage", 60, primary=True))
            own.clear()
            own.update(session_a)  # A's request finally returns
            cs._end_net_attempt("usage")
            own.clear()
            self.assertFalse(
                cs._begin_net_attempt("usage", 60, primary=True),
                "session A lifted a guard that belonged to session B",
            )


class AuthorizedRequestConnectTest(unittest.TestCase):
    def test_dead_ipv6_does_not_eat_the_whole_request_timeout(self):
        """The usage API call allows itself 10s. With one black-holed AAAA
        record listed first that was a 10s stall before IPv4 was even tried —
        on the path that draws the bar when stdin carries no rate limits."""
        world = _FakeWorld(dead_families={socket.AF_INET6},
                           stop_at_live_connect=True)
        with world.installed():
            start = world.clock.time()
            with self.assertRaises(_ReachedLiveAddress):
                cs._authorized_request(
                    "https://api.anthropic.com/api/oauth/usage", "tok",
                    timeout=10)
            elapsed = world.clock.time() - start
        self.assertLess(
            elapsed, 1.0,
            f"reaching a live address took {elapsed:.0f}s of dead-family "
            "timeouts — authorised requests must use the bounded connect too",
        )


USAGE = {"five_hour": {"utilization": 42.0, "resets_at": "2026-08-01T00:00:00Z"}}


class UsageFetchTrapTest(unittest.TestCase):
    """Drive main() down the API fallback (no rate limits on stdin)."""

    def _run_main(self, td, fetch, real_render=False):
        """One repaint. *real_render* keeps the real build_status_line, so a
        value it cannot format surfaces as the blank bar it would be; the
        network is then trapped, git is absent and the line is never clipped."""
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        fake_stdout = mock.Mock()
        fake_stdout.buffer = io.BytesIO()
        rendered = {}
        reached = []

        def fake_build(usage, plan, config=None, stdin_ctx=None, cache_age=None):
            if not isinstance(usage, dict):
                raise TypeError("usage must be a dict")  # as the real one does
            rendered["usage"] = usage
            rendered["cache_age"] = cache_age
            return "bar"

        def no_network(*args, **kwargs):
            reached.append(args[:1])
            raise OSError("network disabled in this test")

        def no_git(*args, **kwargs):
            raise FileNotFoundError("git is not installed in this test")

        with contextlib.ExitStack() as confined, \
                mock.patch.object(sys, "argv", ["claude_status.py"]), \
                mock.patch.object(sys, "stdin", fake_stdin), \
                mock.patch.object(sys, "stdout", fake_stdout), \
                mock.patch.object(cs, "get_state_dir", lambda: Path(td)), \
                mock.patch.object(cs, "get_cache_path",
                                  lambda: Path(td) / "cache.json"), \
                mock.patch.object(cs, "load_config", lambda: {}), \
                mock.patch.object(cs, "_cleanup_hooks", lambda: None), \
                mock.patch.object(cs, "get_credentials", lambda: ("tok", "max")), \
                mock.patch.object(cs, "fetch_usage", fetch), \
                mock.patch.dict(getattr(cs, "_own_stamps", {}), {}, clear=True), \
                mock.patch.object(cs, "check_for_update", lambda: False), \
                mock.patch.object(cs, "check_claude_code_update",
                                  lambda v=None: False), \
                mock.patch.object(cs, "build_status_line",
                                  cs.build_status_line if real_render else fake_build):
            for patch in _confine_writes(td):
                confined.enter_context(patch)
            if real_render:
                for patch in (
                        mock.patch.object(cs.socket, "getaddrinfo", no_network),
                        mock.patch.object(cs.socket, "create_connection", no_network),
                        mock.patch.object(cs.urllib.request, "urlopen", no_network),
                        mock.patch.object(cs.subprocess, "run", no_git),
                        mock.patch.object(cs, "_detect_terminal_width", lambda: None)):
                    confined.enter_context(patch)
            cs.main()
        self.assertEqual(reached, [], "the repaint reached for the real network")
        rendered["printed"] = fake_stdout.buffer.getvalue()
        return rendered

    @staticmethod
    def _plant(td, entry):
        """Write *entry* as cache.json. json.dumps writes NaN and Infinity as
        bare literals and a huge int in full, exactly as a corrupt file would."""
        path = Path(td) / "cache.json"
        path.write_text(json.dumps(entry), encoding="utf-8")
        return path

    @staticmethod
    def _rate_limited(calls):
        """A fetch the server answers with 429, counting every call."""
        def fetch(_token):
            calls.append(1)
            raise urllib.error.HTTPError(
                "https://api.anthropic.com/api/oauth/usage", 429,
                "Too Many Requests", {}, io.BytesIO(b""))
        return fetch

    def _assert_backoff_recorded(self, cache, before):
        """cache.json holds a finite rate_limited_until inside the backoff cap."""
        written = json.loads(cache.read_text(encoding="utf-8"))
        until = written.get("rate_limited_until")
        self.assertIsInstance(until, float, "no 429 backoff was recorded")
        self.assertGreater(until, before)
        self.assertLessEqual(
            until,
            real_time.time() + cs._RATE_LIMIT_BACKOFF_MAX + cs._RATE_LIMIT_JITTER)
        return written

    def _stale_cache(self, td, age):
        (Path(td) / "cache.json").write_text(
            json.dumps({"timestamp": real_time.time() - age, "line": "old",
                        "usage": USAGE, "plan": "max"}),
            encoding="utf-8",
        )

    def test_killed_fetch_is_not_retried_and_the_bar_still_renders(self):
        calls = []

        def fetch(_token):
            calls.append(1)
            if len(calls) == 1:
                raise _Killed()
            return USAGE

        with tempfile.TemporaryDirectory() as td:
            self._stale_cache(td, age=600)
            with self.assertRaises(_Killed):
                self._run_main(td, fetch)
            rendered = self._run_main(td, fetch)
        self.assertEqual(len(calls), 1,
                         "the repaint after a killed usage fetch must not "
                         "walk into the same request again")
        self.assertIn(b"bar", rendered["printed"])
        self.assertEqual(rendered["usage"], USAGE)
        self.assertAlmostEqual(rendered["cache_age"], 600, delta=30)

    def test_killed_fetch_with_nothing_cached_still_prints_a_line(self):
        calls = []

        def fetch(_token):
            calls.append(1)
            raise _Killed()

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(_Killed):
                self._run_main(td, fetch)
            rendered = self._run_main(td, fetch)
        self.assertEqual(len(calls), 1)
        self.assertTrue(rendered["printed"].strip(),
                        "an empty stdout is a blank status bar")

    def test_unusable_stale_cache_falls_back_to_a_placeholder(self):
        """_read_stale_cache hands back whatever JSON object is on disk; the
        guard branch runs outside every fetch error handler, so a raise there
        is an empty stdout — a blank bar for the whole guard window."""
        for bad in ({"timestamp": None, "usage": USAGE, "line": "old"},
                    {"timestamp": "yesterday", "usage": USAGE},
                    {"timestamp": 1.0, "usage": "nonsense"},
                    {"timestamp": 1.0, "line": None}):
            calls = []

            def fetch(_token):
                calls.append(1)
                raise _Killed()

            with self.subTest(cache=bad), tempfile.TemporaryDirectory() as td:
                (Path(td) / "cache.json").write_text(json.dumps(bad),
                                                     encoding="utf-8")
                with self.assertRaises(_Killed):
                    self._run_main(td, fetch)
                rendered = self._run_main(td, fetch)
                self.assertEqual(len(calls), 1)
                self.assertTrue(rendered["printed"].strip())

    def test_stale_cache_with_an_out_of_range_timestamp_still_draws_the_bar(self):
        """A timestamp too big for a float made float() raise OverflowError,
        which no handler caught: main() died inside read_cache, before the
        guard branch was reached, so nothing was drawn. Fixing read_cache
        alone is not enough: _cache_age raised the same error in the guard
        branch, which swallowed it and showed the placeholder instead of the
        cached usage it had."""
        calls = []

        def fetch(_token):
            calls.append(1)
            raise _Killed()

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "cache.json").write_text(
                json.dumps({"timestamp": 10 ** 400, "usage": USAGE,
                            "plan": "max"}),
                encoding="utf-8")
            with self.assertRaises(_Killed):
                self._run_main(td, fetch)
            rendered = self._run_main(td, fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rendered.get("usage"), USAGE,
                         "the cached usage was dropped for the placeholder")
        self.assertEqual(rendered["cache_age"], float("inf"))

    def test_successful_fetch_does_not_delay_the_next_refresh(self):
        """Without rate limits on stdin (older Claude Code) the API is the
        only source, refreshed every cache TTL. The kill guard must not turn
        that into one refresh per backoff window."""
        calls = []

        def fetch(_token):
            calls.append(1)
            return USAGE

        with tempfile.TemporaryDirectory() as td:
            self._run_main(td, fetch)
            self._stale_cache(td, age=600)  # the cache TTL has lapsed
            self._run_main(td, fetch)
        self.assertEqual(len(calls), 2)

    def test_failed_fetch_does_not_leave_the_guard_armed(self):
        calls = []

        def fetch(_token):
            calls.append(1)
            raise urllib.error.URLError("unreachable")

        with tempfile.TemporaryDirectory() as td:
            self._run_main(td, fetch)
            (Path(td) / "cache.json").unlink()  # the cached error line lapsed
            self._run_main(td, fetch)
        self.assertEqual(len(calls), 2,
                         "an ordinary failure is already remembered by the "
                         "cached error line; the guard is only for kills")

    def test_backoff_with_an_unusable_timestamp_still_draws_the_cached_bar(self):
        """While a 429 backoff is active, read_cache hands the cache back
        without checking its timestamp, and main() subtracted it raw. Text,
        null, a list, a dict or an int too big for a float raised: a blank
        bar on every repaint until the backoff ran out. NaN and Infinity gave
        an age the staleness label cannot use. An unusable timestamp must read
        as stale (the red label), and the backoff must still hold."""
        for bad in (None, "yesterday", [1], {"t": 1}, 10 ** 400, -10 ** 400,
                    float("nan"), float("inf"), "1e999"):
            calls = []

            def fetch(_token):
                calls.append(1)
                return USAGE

            with self.subTest(timestamp=repr(bad)[:24]), \
                    tempfile.TemporaryDirectory() as td:
                self._plant(td, {"timestamp": bad, "usage": USAGE, "plan": "max",
                                 "rate_limited_until": real_time.time() + 300,
                                 "rate_limit_fails": 1})
                rendered = self._run_main(td, fetch)
                self.assertEqual(calls, [], "the API was called inside the backoff")
                self.assertIn(b"bar", rendered["printed"])
                self.assertEqual(rendered.get("usage"), USAGE)
                self.assertGreaterEqual(rendered["cache_age"], cs.STALENESS_RED,
                                        "an unusable timestamp must read as stale")

    def test_backoff_with_an_unusable_timestamp_draws_through_the_real_renderer(self):
        """The same through the real renderer, where NaN reached
        _format_staleness and int(nan // 60) raised ValueError."""
        for bad in (float("nan"), 10 ** 400, "yesterday"):
            calls = []

            def fetch(_token):
                calls.append(1)
                return USAGE

            with self.subTest(timestamp=repr(bad)[:24]), \
                    tempfile.TemporaryDirectory() as td:
                self._plant(td, {"timestamp": bad, "usage": USAGE, "plan": "max",
                                 "rate_limited_until": real_time.time() + 300})
                rendered = self._run_main(td, fetch, real_render=True)
                printed = rendered["printed"].decode("utf-8")
                self.assertEqual(calls, [])
                self.assertIn("42%", printed, "the cached usage was not drawn")
                self.assertIn("10m+ ago", printed, "the stale label is missing")

    def test_backoff_with_a_valid_timestamp_still_shows_its_true_age(self):
        """Behaviour kept: inside a backoff a well-formed cache is drawn with
        its real age, so the staleness label fires at the usual thresholds."""
        calls = []

        def fetch(_token):
            calls.append(1)
            return USAGE

        with tempfile.TemporaryDirectory() as td:
            self._plant(td, {"timestamp": real_time.time() - 200, "usage": USAGE,
                             "plan": "max",
                             "rate_limited_until": real_time.time() + 300})
            rendered = self._run_main(td, fetch)
        self.assertEqual(calls, [])
        self.assertAlmostEqual(rendered["cache_age"], 200, delta=20)

    def test_numeric_text_timestamp_is_read_as_its_number(self):
        """float() accepts text, so read_cache called a fresh timestamp
        written as "1792674000.5" fresh, and main() then raised subtracting
        a string from a float. Inside a backoff the same text arrived too."""
        for backoff in (False, True):
            calls = []

            def fetch(_token):
                calls.append(1)
                return USAGE

            with self.subTest(backoff=backoff), tempfile.TemporaryDirectory() as td:
                entry = {"timestamp": repr(real_time.time() - 20), "usage": USAGE,
                         "plan": "max"}
                if backoff:
                    entry["rate_limited_until"] = real_time.time() + 300
                self._plant(td, entry)
                rendered = self._run_main(td, fetch)
                self.assertEqual(calls, [], "a fresh cache was fetched again")
                self.assertIn(b"bar", rendered["printed"])
                self.assertAlmostEqual(rendered["cache_age"], 20, delta=15)

    def test_429_with_an_unusable_stale_timestamp_still_draws_and_backs_off(self):
        """The 429 handler subtracted the stale timestamp raw, before it wrote
        the backoff. An int too big for a float, text, null or a list raised
        there: a blank bar, and with no backoff on disk every later repaint
        called the API again (about once a minute while rate limited, against
        a 60-900 s backoff). NaN and Infinity did not raise, but reached the
        renderer as an unusable age and were written back as the data time."""
        for bad in (10 ** 400, "bad", None, [1], float("nan"), float("inf")):
            calls = []
            fetch = self._rate_limited(calls)
            with self.subTest(timestamp=repr(bad)[:24]), \
                    tempfile.TemporaryDirectory() as td:
                cache = self._plant(td, {"timestamp": bad, "usage": USAGE,
                                         "plan": "max"})
                before = real_time.time()
                rendered = self._run_main(td, fetch)
                self.assertEqual(len(calls), 1)
                self.assertIn(b"bar", rendered["printed"])
                self.assertEqual(rendered.get("usage"), USAGE)
                self.assertGreaterEqual(rendered["cache_age"], cs.STALENESS_RED,
                                        "an unusable timestamp must read as stale")
                written = self._assert_backoff_recorded(cache, before)
                self.assertEqual(written.get("rate_limit_fails"), 1)
                self.assertIsNotNone(cs._cache_seconds(written.get("timestamp")),
                                     "an unusable timestamp was written back")
                # The next repaint falls inside the backoff: no API call.
                again = self._run_main(td, fetch)
                self.assertEqual(len(calls), 1, "the 429 backoff was not honoured")
                self.assertIn(b"bar", again["printed"])

    def test_429_with_an_unusable_stale_timestamp_draws_through_the_real_renderer(self):
        """The same 429 through the real renderer, where a NaN stale age
        raised ValueError in the staleness label."""
        for bad in (float("nan"), 10 ** 400):
            calls = []
            fetch = self._rate_limited(calls)
            with self.subTest(timestamp=repr(bad)[:24]), \
                    tempfile.TemporaryDirectory() as td:
                cache = self._plant(td, {"timestamp": bad, "usage": USAGE,
                                         "plan": "max"})
                before = real_time.time()
                rendered = self._run_main(td, fetch, real_render=True)
                printed = rendered["printed"].decode("utf-8")
                self.assertIn("42%", printed, "the stale usage was not drawn")
                self.assertIn("10m+ ago", printed, "the stale label is missing")
                self._assert_backoff_recorded(cache, before)
                again = self._run_main(td, fetch, real_render=True)
                self.assertEqual(len(calls), 1, "the 429 backoff was not honoured")
                self.assertIn("42%", again["printed"].decode("utf-8"))

    def test_429_with_an_unusable_fail_count_still_draws_and_backs_off(self):
        """int() of an infinite rate_limit_fails raised OverflowError inside
        the 429 handler, before it wrote the backoff: a blank bar, and every
        repaint after the kill guard lapsed called the API again."""
        for bad in (float("inf"), float("-inf")):
            for with_usage in (True, False):
                calls = []
                fetch = self._rate_limited(calls)
                entry = {"timestamp": real_time.time() - 600, "line": "old",
                         "rate_limit_fails": bad}
                if with_usage:
                    entry.update(usage=USAGE, plan="max")
                with self.subTest(fail_count=repr(bad), stale_usage=with_usage), \
                        tempfile.TemporaryDirectory() as td:
                    cache = self._plant(td, entry)
                    before = real_time.time()
                    rendered = self._run_main(td, fetch)
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(rendered["printed"].strip(),
                                    "an empty stdout is a blank status bar")
                    written = self._assert_backoff_recorded(cache, before)
                    self.assertEqual(written.get("rate_limit_fails"), 1)
                    again = self._run_main(td, fetch)
                    self.assertEqual(len(calls), 1, "the 429 backoff was not honoured")
                    self.assertTrue(again["printed"].strip())

    def test_429_with_undrawable_stale_usage_still_backs_off(self):
        """The 429 handler drew the stale usage before it wrote the backoff,
        and checked only that it was truthy. Text, a list or a number there,
        or a usage dict holding a value the renderer cannot format (a
        hand-edited or corrupt file), raised: a blank bar and no backoff, so
        the API was called again once the kill guard lapsed. The repaint now
        says it is rate limited, and the rewrite drops what it could not draw."""
        cases = [("usage=%r" % (bad,), bad, False) for bad in ("text", [1], 5, True)]
        cases += [
            ("five_hour utilization NaN",
             {"five_hour": {"utilization": float("nan"),
                            "resets_at": "2026-08-01T00:00:00Z"}}, True),
            ("seven_day as text", {"seven_day": "text"}, True),
        ]
        for label, bad, real_render in cases:
            calls = []
            fetch = self._rate_limited(calls)
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                cache = self._plant(td, {"timestamp": real_time.time() - 600,
                                         "line": "old", "usage": bad, "plan": "max"})
                before = real_time.time()
                rendered = self._run_main(td, fetch, real_render=real_render)
                self.assertEqual(len(calls), 1)
                self.assertIn(b"Rate limited", rendered["printed"])
                written = self._assert_backoff_recorded(cache, before)
                self.assertNotIn("usage", written,
                                 "the usage the bar could not draw was written back")
                again = self._run_main(td, fetch, real_render=real_render)
                self.assertEqual(len(calls), 1, "the 429 backoff was not honoured")
                self.assertIn(b"Rate limited", again["printed"])

    def test_429_keeps_data_of_unknown_age_marked_stale(self):
        """When the stale data's time was unusable, missing or zero, the 429
        rewrite let write_cache stamp the copy with the current time, so the
        next repaint drew data of unknown age as just fetched. The rewrite now
        records the first second of the epoch, which every reader takes as
        long stale: the red label stays until a fetch succeeds."""
        missing = object()
        for bad in (10 ** 400, "bad", None, float("nan"), float("-inf"), 0, missing):
            calls = []
            fetch = self._rate_limited(calls)
            entry = {"usage": USAGE, "plan": "max"}
            if bad is not missing:
                entry["timestamp"] = bad
            label = "missing" if bad is missing else repr(bad)[:24]
            with self.subTest(timestamp=label), tempfile.TemporaryDirectory() as td:
                cache = self._plant(td, entry)
                before = real_time.time()
                first = self._run_main(td, fetch)
                self.assertGreaterEqual(first["cache_age"], cs.STALENESS_RED)
                written = self._assert_backoff_recorded(cache, before)
                written_ts = cs._cache_seconds(written.get("timestamp"))
                self.assertIsNotNone(written_ts)
                self.assertGreaterEqual(real_time.time() - written_ts, cs.STALENESS_RED,
                                        "the rewrite stamped data of unknown age as fresh")
                again = self._run_main(td, fetch)
                self.assertEqual(len(calls), 1, "the 429 backoff was not honoured")
                self.assertGreaterEqual(again["cache_age"], cs.STALENESS_RED,
                                        "the next repaint drew data of unknown age as fresh")

    def test_429_keeps_a_valid_data_time_exactly(self):
        """Preservation pin: a usable data time is carried as written."""
        for good in (real_time.time() - 600, int(real_time.time()) - 600):
            calls = []
            fetch = self._rate_limited(calls)
            with self.subTest(timestamp=type(good).__name__), \
                    tempfile.TemporaryDirectory() as td:
                cache = self._plant(td, {"timestamp": good, "usage": USAGE,
                                         "plan": "max"})
                self._run_main(td, fetch)
                self.assertEqual(len(calls), 1)
                written = json.loads(cache.read_text(encoding="utf-8"))
                self.assertEqual(written["timestamp"], good)
                self.assertIs(type(written["timestamp"]), type(good))

    def test_unparseable_cache_file_still_draws_and_backs_off(self):
        """read_cache and _read_stale_cache caught JSON syntax errors and
        OSError only. A file that is not UTF-8 (UnicodeDecodeError), nested
        past the parser's depth (RecursionError) or holding an int past the
        int/str digit limit (a plain ValueError on Python 3.11+ and the
        3.8.14 / 3.9.14 / 3.10.7 security releases) raised out of main() on
        every repaint until someone replaced the file."""
        usage = json.dumps(USAGE).encode("utf-8")
        for label, raw in (
                ("not utf-8", b'{"timestamp": "\xff\xfe", "usage": ' + usage + b"}"),
                ("nested too deep", b"[" * 100000 + b"]" * 100000),
                ("int past the digit limit",
                 b'{"timestamp": 1' + b"0" * 5000 + b', "usage": ' + usage + b"}")):
            calls = []
            fetch = self._rate_limited(calls)
            with self.subTest(cache=label), tempfile.TemporaryDirectory() as td:
                cache = Path(td) / "cache.json"
                cache.write_bytes(raw)
                before = real_time.time()
                rendered = self._run_main(td, fetch)
                self.assertEqual(len(calls), 1)
                self.assertTrue(rendered["printed"].strip(),
                                "an empty stdout is a blank status bar")
                self._assert_backoff_recorded(cache, before)
                again = self._run_main(td, fetch)
                self.assertEqual(len(calls), 1, "the 429 backoff was not honoured")
                self.assertTrue(again["printed"].strip())


class FetchCeilingTest(unittest.TestCase):
    def test_hung_transport_is_abandoned_at_the_budget(self):
        """getaddrinfo() has no timeout at all; a VPN with unreachable DNS
        servers can hang it for many seconds. Only a wall-clock ceiling around
        the whole fetch bounds that."""
        release = threading.Event()
        self.addCleanup(release.set)

        def hung(req, max_bytes, deadline):
            release.wait(10)
            return b"too late"

        req = urllib.request.Request("https://registry.npmjs.org/x")
        with mock.patch.object(cs, "_open_and_read", hung, create=True):
            started = real_time.monotonic()
            result = cs._fetch_bounded(req, 1000, budget=0.2)
            elapsed = real_time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 1.0)

    def test_budget_fits_inside_the_shortest_refresh_interval_users_set(self):
        # --install writes refreshInterval: 2 while animation is on; a fetch
        # that can outlive it gets the repaint cancelled.
        self.assertLess(cs._NET_FETCH_BUDGET, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
