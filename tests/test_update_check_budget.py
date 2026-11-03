"""The update check's time budget must bound the blocking calls themselves.

``check_for_update`` advertises a hard ceiling (_UPDATE_CHECK_BUDGET) on one
check, but only inspecting the deadline *between* operations doesn't enforce
it: five sequential calls that each run just under their own fixed timeout
(2s + 3s + 3s + 3s + 2s) can stall a repaint for ~13 seconds. Every blocking
call must receive a timeout capped by the budget that remains.

The fakes here model exactly that adversarial-but-realistic case: each git or
HTTP call succeeds after consuming 90% of whatever timeout it was allowed.

Run: ``python tests/test_update_check_budget.py``
"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

LOCAL_SHA = "a" * 40
REMOTE_SHA = "b" * 40


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now


class UpdateCheckBudgetTest(unittest.TestCase):
    def _run_check(self, op_cost_fraction):
        """Run check_for_update with ops that consume the given fraction of
        whatever timeout they are granted. Returns (verdict, elapsed)."""
        clock = _FakeClock()

        def fake_run(cmd, **kwargs):
            # subprocess.run's real surface: any keyword argument, with the
            # output delivered wherever stdout points (a pipe, or the temp file
            # network git writes to). A call with no timeout at all is charged
            # an hour: nothing stops it blocking that long.
            timeout = kwargs.get("timeout")
            clock.now += (3600 if timeout is None else timeout) * op_cost_fraction
            if "symbolic-ref" in cmd:
                returncode, out = 0, "main"
            elif "rev-parse" in cmd:
                returncode, out = 0, LOCAL_SHA
            elif "get-url" in cmd:
                returncode, out = 0, f"https://github.com/{cs.GITHUB_REPO}"
            elif "ls-remote" in cmd:
                # Never a real remote: upstream's tip, as the API reports it.
                returncode, out = 0, f"{REMOTE_SHA}\trefs/heads/main\n"
            elif any(sub in cmd for sub in ("fetch", "pull", "clone", "push")):
                returncode, out = 128, ""  # no other network git is scripted
            else:  # cat-file / merge-base
                returncode, out = 0, ""
            text = any(kwargs.get(k) for k in ("text", "universal_newlines",
                                               "encoding", "errors"))
            dest = kwargs.get("stdout")
            if hasattr(dest, "write"):
                dest.write(out.encode("utf-8"))
                out = None
            elif kwargs.get("capture_output") or dest is cs.subprocess.PIPE:
                out = out if text else out.encode("utf-8")
            else:
                out = None  # not captured: it would have gone to our own stdout
            err = ("" if text else b"") if kwargs.get("capture_output") else None
            return SimpleNamespace(returncode=returncode, stdout=out, stderr=err)

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return REMOTE_SHA.encode()

        def fake_urlopen(req, timeout=None):
            clock.now += timeout * op_cost_fraction
            return _FakeResponse()

        def fake_fetch(req, max_bytes, budget=None):
            clock.now += budget * op_cost_fraction
            return REMOTE_SHA.encode()

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(cs, "time", clock), \
                mock.patch.object(cs.subprocess, "run", fake_run), \
                mock.patch.object(cs.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(cs, "_fetch_bounded", fake_fetch), \
                mock.patch.object(cs, "get_state_dir", lambda: Path(td)):
            start = clock.time()
            verdict = cs.check_for_update()
            return verdict, clock.time() - start

    def test_slow_operations_cannot_exceed_the_advertised_budget(self):
        verdict, elapsed = self._run_check(op_cost_fraction=0.9)
        self.assertLessEqual(
            elapsed, cs._UPDATE_CHECK_BUDGET + 0.5,
            f"one update check consumed {elapsed:.1f}s of blocking calls — the "
            f"budget of {cs._UPDATE_CHECK_BUDGET}s must cap each call, not just "
            "be glanced at between calls",
        )

    def test_fast_operations_still_produce_a_verdict(self):
        verdict, elapsed = self._run_check(op_cost_fraction=0.01)
        # Remote differs from local but merge-base exits 0 (remote is an
        # ancestor → this checkout is ahead → no update to offer).
        self.assertIs(verdict, False)
        self.assertLess(elapsed, cs._UPDATE_CHECK_BUDGET)


class UpdateBranchGateTest(unittest.TestCase):
    """Only a main/master checkout gets the update nag.

    A checkout parked on a topic or preview branch (v3.2.0-preview, fix/…) is
    a developer or tester and is typically *ahead* of upstream main — where
    "↑ Pulse Update" is not just noise but actively wrong.
    """

    def _run_check(self, branch_stdout, branch_rc=0):
        import tempfile
        network_calls = []

        def fake_run(cmd, **kwargs):
            # subprocess.run's real surface: any keyword argument, with the
            # output delivered wherever stdout points (a pipe, or the temp file
            # network git writes to).
            if any(sub in cmd for sub in ("ls-remote", "fetch", "pull", "clone", "push")):
                network_calls.append(cmd)  # git reaching for the network counts
                returncode, out = 128, ""  # and never gets an answer
            elif "symbolic-ref" in cmd:
                returncode, out = branch_rc, branch_stdout
            elif "rev-parse" in cmd:
                returncode, out = 0, LOCAL_SHA
            elif "get-url" in cmd:
                returncode, out = 0, f"https://github.com/{cs.GITHUB_REPO}"
            else:
                returncode, out = 1, ""
            text = any(kwargs.get(k) for k in ("text", "universal_newlines",
                                               "encoding", "errors"))
            dest = kwargs.get("stdout")
            if hasattr(dest, "write"):
                dest.write(out.encode("utf-8"))
                out = None
            elif kwargs.get("capture_output") or dest is cs.subprocess.PIPE:
                out = out if text else out.encode("utf-8")
            else:
                out = None  # not captured: it would have gone to our own stdout
            err = ("" if text else b"") if kwargs.get("capture_output") else None
            return SimpleNamespace(returncode=returncode, stdout=out, stderr=err)

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return REMOTE_SHA.encode()

        def fake_urlopen(req, timeout=None):
            network_calls.append(req)
            return _FakeResponse()

        def fake_fetch(req, max_bytes, budget=None):
            network_calls.append(req)
            return REMOTE_SHA.encode()

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(cs.subprocess, "run", fake_run), \
                mock.patch.object(cs.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(cs, "_fetch_bounded", fake_fetch), \
                mock.patch.object(cs, "get_state_dir", lambda: Path(td)):
            verdict = cs.check_for_update()
        return verdict, network_calls

    def test_topic_branch_never_nags_and_skips_the_network(self):
        verdict, network = self._run_check("v3.2.0-preview\n")
        self.assertIs(verdict, False)
        self.assertEqual(network, [])

    def test_detached_head_never_nags(self):
        verdict, network = self._run_check("", branch_rc=1)
        self.assertIs(verdict, False)
        self.assertEqual(network, [])

    def test_git_error_is_unknown_not_detached(self):
        """symbolic-ref -q exits 1 for a detached HEAD, but 128 means the
        repo itself is broken — that must stay 'unknown', not be cached as a
        permanent 'never nag' verdict."""
        verdict, network = self._run_check("", branch_rc=128)
        self.assertIsNone(verdict)
        self.assertEqual(network, [])

    def test_main_checkout_still_reaches_the_remote(self):
        verdict, network = self._run_check("main\n")
        self.assertTrue(network, "a main checkout must still check upstream")


if __name__ == "__main__":
    unittest.main(verbosity=2)
