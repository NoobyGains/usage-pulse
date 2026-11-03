"""The 'usage' kill guard, on both paths that fetch usage, across accounts.

The render-path fix arms a write-ahead stamp ("usage") before every usage
fetch, so a repaint that Claude Code cancels mid-request leaves a trace and
the next repaint draws from cache instead of walking into the same stall.
Upstream 3.4.0 made the usage cache per account (``get_account_state_dir``).
These tests pin how the guard has to sit alongside that:

* the "usage" stamp belongs to one account. Another account's cut-short fetch
  must not hold this account's Fable/Opus/Sonnet caps refresh off;
* every other stamp ("_last", the update checks, exchange rates) stays
  machine-wide: network turn-taking is per machine on purpose;
* the default account (``~/.claude``) keeps the stamp where it always was;
* a guard that says "not now" sends no request, so it must not use up
  upstream's per-account ``model_caps_attempted_at`` retry window;
* a killed caps fetch is not retried inside the guard window, even when
  another session's cache write has wiped ``model_caps_attempted_at``, and the
  bar still renders with the cached caps;
* the guard's fallback render on the API path keeps the provider rows, like
  every other output site.

Nothing here touches the network (socket and urlopen are blocked), reads real
credentials (``get_credentials`` returns a fake token), or writes outside a
temp dir (the file writers are wrapped to fail the test if they try).

Run: ``python -m unittest tests.test_port_usage_guard``
"""
import io
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, cs  # noqa: E402
from terminal_fixtures import host_terminal_width  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# What the OAuth usage endpoint returns for a Fable weekly cap.
FABLE_PAYLOAD = {
    "limits": [{
        "kind": "weekly_scoped",
        "percent": 81,
        "resets_at": "2099-09-14T18:00:00+00:00",
        "scope": {"model": {"display_name": "Fable"}},
    }],
}
STALE_FABLE = {"utilization": 37.0, "resets_at": "2099-09-14T18:00:00+00:00"}
STALE_CORE = {"five_hour": {"utilization": 42.0, "resets_at": "2099-09-08T18:00:00+00:00"},
              "seven_day": {"utilization": 23.0, "resets_at": "2099-09-14T18:00:00+00:00"}}
PROVIDER_ROW = "PROVIDER-ROW-SENTINEL"


class _Killed(BaseException):
    """Claude Code cancelling the script mid-request.

    A BaseException, so no ``except Exception`` handler gets to run cleanup
    after the network call, just as with a real kill.
    """


class _TtyStdin:
    """An interactive stdin: main() reads nothing, so it takes the API path."""

    def isatty(self):
        return True

    def read(self, *_):
        raise AssertionError("main() read an interactive stdin")


class _GuardHarness(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.default_dir = self.sb.claude_dir(".claude", creds=False)
        self.account_a = self.sb.claude_dir(".claude-a", creds=False)
        self.account_b = self.sb.claude_dir(".claude-b", creds=False)
        self.outcomes = []   # per fetch: "ok" | "kill"
        self.fetches = []    # the account dir each fetch ran under
        self.on_disk_at_fetch = []  # (model_caps_attempted_at, usage stamp)
        self.network = []    # any attempt to reach the real network

        base = dict(cs.load_config())  # sandboxed: the built-in defaults
        show = dict(base.get("show", {}))
        show.update({"opus": False, "sonnet": False, "fable": True, "extra": False})
        base["show"] = show
        base["providers"] = []
        self.config = base

        root = self.sb.root.resolve()

        def confined(real):
            def wrapper(filepath, *args, **kwargs):
                target = Path(filepath).resolve()
                if target != root and root not in target.parents:
                    raise AssertionError("test wrote outside its sandbox: %s" % target)
                return real(filepath, *args, **kwargs)
            return wrapper

        def no_network(*args, **kwargs):
            self.network.append(args[:1])
            raise OSError("network disabled in tests")

        patches = [
            mock.patch.dict(cs._own_stamps, {}, clear=True),
            # The line is printed through a fake stdout with no fileno, so width
            # detection would fall through to the contributor's own terminal and
            # fit (cut) the line to their window. Pin it to "no terminal".
            mock.patch.object(cs, "_detect_terminal_width", lambda: None),
            mock.patch.object(cs, "get_credentials", lambda: ("fake-token", "Max 20x")),
            mock.patch.object(cs, "fetch_usage", self._fetch),
            mock.patch.object(cs, "load_config",
                              lambda: dict(self.config, show=dict(self.config["show"]))),
            mock.patch.object(cs, "check_for_update", lambda *a, **k: False),
            mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: False),
            mock.patch.object(cs, "sync_status_line_refresh", lambda *a, **k: None),
            mock.patch.object(cs, "_cleanup_hooks", lambda: None),
            mock.patch.object(cs.socket, "getaddrinfo", no_network),
            mock.patch.object(cs.socket, "create_connection", no_network),
            mock.patch.object(cs.urllib.request, "urlopen", no_network),
            mock.patch.object(cs, "_atomic_json_write", confined(cs._atomic_json_write)),
            mock.patch.object(cs, "_secure_open_write", confined(cs._secure_open_write)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.assertEqual(self.network, [], "a test reached for the real network")

    # --- helpers -----------------------------------------------------------
    def _fetch(self, token, timeout=10):
        self.assertEqual(token, "fake-token")
        self.fetches.append(cs.get_account_state_dir())
        # What a kill at this instant would leave behind on disk.
        try:
            attempted = self._cache().get("model_caps_attempted_at")
        except (OSError, ValueError):
            attempted = None
        self.on_disk_at_fetch.append((attempted, cs._read_stamp("usage")))
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "kill":
            raise _Killed()
        usage = cs._normalize_usage(json.loads(json.dumps(FABLE_PAYLOAD)))
        usage.update(json.loads(json.dumps(STALE_CORE)))
        return usage

    def _use(self, account_dir):
        if account_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            self.sb.set_active(account_dir)

    @staticmethod
    def _new_process():
        """Each repaint is its own process: nothing held in memory survives."""
        cs._own_stamps.clear()

    def _stdin_payload(self):
        now = time.time()
        return json.dumps({"session_id": "s1", "rate_limits": {
            "five_hour": {"used_percentage": 15, "resets_at": now + 10800},
            "seven_day": {"used_percentage": 66, "resets_at": now + 604800}}})

    def _repaint(self, stdin_payload=None):
        """Run one status-line repaint; return the printed text, ANSI stripped."""
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        stdin = _TtyStdin() if stdin_payload is None else io.StringIO(stdin_payload)
        self._new_process()
        with mock.patch.object(cs.sys, "argv", ["claude_status.py"]), \
                mock.patch.object(cs.sys, "stdin", stdin), \
                mock.patch.object(cs.sys, "stdout", _Out()):
            cs.main()
        return ANSI.sub("", buf.getvalue().decode("utf-8", "replace")).strip()

    def _plant_stale_caps(self, with_fable=True):
        """A cache whose caps are past the TTL and whose last attempt is too."""
        old = time.time() - cs._MODEL_CAPS_TTL - 30
        usage = json.loads(json.dumps(STALE_CORE))
        if with_fable:
            usage["seven_day_fable"] = dict(STALE_FABLE)
        data = {"timestamp": old, "line": "", "usage": usage, "plan": "Max 20x",
                "model_caps_fetched_at": old, "model_caps_attempted_at": old}
        path = Path(cs.get_cache_path())
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _cache(self):
        return json.loads(Path(cs.get_cache_path()).read_text(encoding="utf-8"))

    def _arm_and_die(self):
        """A repaint of the active account armed 'usage' and was then killed."""
        self.assertTrue(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))
        self._new_process()

    def _assert_caps_requests_were_write_ahead(self):
        """Every caps request had both stamps on disk before it went out, so a
        kill at any point inside it still holds the next repaint off."""
        self.assertTrue(self.on_disk_at_fetch)
        for attempted, stamp in self.on_disk_at_fetch:
            self.assertIsNotNone(attempted, "model_caps_attempted_at not on disk")
            self.assertGreater(attempted, time.time() - 30)
            self.assertGreater(stamp, time.time() - 30, "usage stamp not on disk")


class StampScopeTest(_GuardHarness):
    """Where each stamp lives (test e, plus the per-key split)."""

    def test_default_account_usage_stamp_has_not_moved(self):
        """Upgrading single-account users keep the guard they already have."""
        expected = self.sb.state / "net_attempt_usage.stamp"
        self._use(None)
        self.assertEqual(cs._stamp_path("usage"), expected)
        # An explicit CLAUDE_CONFIG_DIR naming ~/.claude is the same account.
        self._use(str(self.default_dir) + os.sep)
        self.assertEqual(cs._stamp_path("usage"), expected)

    def test_second_account_usage_stamp_sits_beside_its_cache(self):
        self._use(self.account_b)
        path = cs._stamp_path("usage")
        self.assertEqual(path.parent, Path(cs.get_cache_path()).parent)
        self.assertEqual(path.parent, cs.get_account_state_dir())
        self.assertNotEqual(path.parent, self.sb.state)
        self.assertEqual(path.name, "net_attempt_usage.stamp")

    def test_every_other_stamp_stays_machine_wide(self):
        for account in (None, self.account_a, self.account_b):
            self._use(account)
            for key, name in (("_last", "net_attempt__last.stamp"),
                              ("update_check", "net_attempt_update_check.stamp"),
                              ("claude_code_update", "net_attempt_claude_code_update.stamp"),
                              ("exchange_rate:GBP", "net_attempt_exchange_rate_GBP.stamp")):
                with self.subTest(account=account, key=key):
                    self.assertEqual(cs._stamp_path(key), self.sb.state / name)

    def test_one_accounts_usage_guard_does_not_refuse_another(self):
        self._use(self.account_a)
        self._arm_and_die()
        self._use(self.account_b)
        self.assertTrue(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True),
                        "account A's cut-short fetch held account B's off")
        cs._end_net_attempt("usage")
        self._use(self.account_a)
        self.assertFalse(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True),
                         "account B lifted or overwrote account A's guard")

    def test_turn_taking_stays_per_machine(self):
        """A usage fetch in one account still makes the optional checks of
        another account's window wait their turn: one network check per
        repaint on this machine, not per account."""
        self._use(self.account_a)
        self._arm_and_die()
        self._use(self.account_b)
        self.assertFalse(cs._begin_net_attempt("claude_code_update"),
                         "the '_last' turn-taking stamp became per account")
        self._use(self.account_a)
        self._new_process()
        self.assertFalse(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))


class CrossAccountCapsTest(_GuardHarness):
    """Test a: account A's killed fetch must not starve account B's caps."""

    def test_other_accounts_killed_fetch_does_not_block_this_accounts_caps(self):
        self._use(self.account_a)
        self._arm_and_die()

        self._use(self.account_b)
        b_cache = Path(cs.get_cache_path())
        self.assertNotEqual(b_cache.parent, self.sb.state, "B must have its own dir")
        self._plant_stale_caps()

        line = self._repaint(self._stdin_payload())

        self.assertEqual(len(self.fetches), 1,
                         "account B's caps refresh was held off by account A's stamp")
        self.assertEqual(self.fetches[0], b_cache.parent)
        cached = json.loads(b_cache.read_text(encoding="utf-8"))
        self.assertEqual(cached["usage"]["seven_day_fable"]["utilization"], 81)
        self.assertTrue(cached.get("model_caps_fetched_at"))
        self.assertIn("Fable", line)
        self.assertIn("81%", line)
        self._assert_caps_requests_were_write_ahead()
        # A's guard is still armed: B neither saw it nor lifted it.
        self._use(self.account_a)
        self.assertFalse(cs._begin_net_attempt("usage", cs._USAGE_KILL_GUARD, primary=True))


class HostTerminalWidthTest(_GuardHarness):
    """The line these tests read is not fitted to the contributor's own window
    (the harness's fake stdout has no fileno, so width detection falls through
    to stderr and the Fable bar is cut in an ordinary terminal)."""

    def test_the_cap_is_drawn_whatever_width_the_hosts_terminal_has(self):
        self._use(None)
        for columns in (80, 160):
            with self.subTest(columns=columns):
                self._plant_stale_caps()
                with host_terminal_width(columns):
                    line = self._repaint(self._stdin_payload())
                self.assertIn("81%", line,
                              "the line was fitted to a %d-column host terminal" % columns)


class RefusedGuardDoesNotBurnRetryTest(_GuardHarness):
    """Test b: 'not now' from the guard is not an attempt."""

    def test_refused_guard_leaves_the_retry_window_alone(self):
        self._use(None)
        planted = self._plant_stale_caps()
        self._arm_and_die()  # another window of this account is mid-fetch

        line = self._repaint(self._stdin_payload())

        self.assertEqual(self.fetches, [])
        self.assertIn("Fable", line, "the bar must still draw the cached caps")
        self.assertEqual(self._cache().get("model_caps_attempted_at"),
                         planted["model_caps_attempted_at"],
                         "a refused guard recorded an attempt it never made")

        cs._stamp_path("usage").unlink()  # that window's fetch came back
        self._repaint(self._stdin_payload())

        self.assertEqual(len(self.fetches), 1,
                         "the refused guard pushed the next try a whole "
                         "_MODEL_CAPS_RETRY away")
        self.assertEqual(self._cache()["usage"]["seven_day_fable"]["utilization"], 81)
        self._assert_caps_requests_were_write_ahead()

    def test_missing_token_still_waits_the_retry_window(self):
        """Upstream: no token is an attempt too, retried at most every
        _MODEL_CAPS_RETRY, so a Keychain read is not made on every repaint."""
        self._use(None)
        self._plant_stale_caps()
        creds = []

        def no_token():
            creds.append(1)
            return None, None

        with mock.patch.object(cs, "get_credentials", no_token):
            self._repaint(self._stdin_payload())
            self.assertEqual(len(creds), 1)
            attempted = self._cache().get("model_caps_attempted_at")
            self.assertIsNotNone(attempted, "a no-token attempt was not recorded")
            self.assertGreater(attempted, time.time() - 30)
            self._repaint(self._stdin_payload())
            self._repaint(self._stdin_payload())
        # The plan comes from the cache, so only the caps branch reads
        # credentials, and it stays shut inside the retry window.
        self.assertEqual(len(creds), 1, "credentials were read on every repaint")
        self.assertEqual(self.fetches, [])
        self.assertFalse(cs._stamp_path("usage").exists(),
                         "the guard was left armed with no request in flight")


class CapsPathKillGuardTest(_GuardHarness):
    """Test c: a killed caps fetch on the stdin path (C10/C11 coverage)."""

    def test_killed_caps_fetch_is_not_retried_inside_the_guard_window(self):
        self._use(None)
        self._plant_stale_caps()
        self.outcomes = ["kill"]
        with self.assertRaises(_Killed):
            self._repaint(self._stdin_payload())
        self.assertEqual(len(self.fetches), 1)
        # Both write-ahead stamps were on disk before the request.
        self.assertGreater(cs._read_stamp("usage"), time.time() - 30)
        attempted = self._cache().get("model_caps_attempted_at")
        self.assertIsNotNone(attempted, "the attempt was not recorded before the request")
        self.assertGreater(attempted, time.time() - 30)

        line = self._repaint(self._stdin_payload())

        self.assertEqual(len(self.fetches), 1,
                         "the repaint after a killed caps fetch walked straight "
                         "back into the same request")
        self.assertIn("Fable", line)
        self.assertIn("37%", line, "the cached caps must still render")

        # Once both windows have passed, it tries again, once.
        cs._write_stamp("usage", time.time() - cs._USAGE_KILL_GUARD - 1)
        cached = self._cache()
        cached["model_caps_attempted_at"] = time.time() - cs._MODEL_CAPS_RETRY - 1
        Path(cs.get_cache_path()).write_text(json.dumps(cached), encoding="utf-8")
        line = self._repaint(self._stdin_payload())
        self.assertEqual(len(self.fetches), 2)
        self.assertIn("81%", line)
        self._assert_caps_requests_were_write_ahead()

    def test_guard_holds_even_after_another_session_wipes_the_attempt(self):
        """Another window's end-of-branch write can drop model_caps_attempted_at
        (it wrote what it read before this window's stamp). The per-key stamp
        is a file of its own, so it still holds the killed request off."""
        self._use(None)
        self._plant_stale_caps()
        self.outcomes = ["kill"]
        with self.assertRaises(_Killed):
            self._repaint(self._stdin_payload())

        cached = self._cache()
        cached.pop("model_caps_attempted_at", None)
        Path(cs.get_cache_path()).write_text(json.dumps(cached), encoding="utf-8")

        line = self._repaint(self._stdin_payload())

        self.assertEqual(len(self.fetches), 1,
                         "with model_caps_attempted_at wiped, nothing held the "
                         "killed caps request off")
        self.assertIn("37%", line)

    def test_killed_caps_fetch_with_nothing_cached_still_prints_a_line(self):
        self._use(self.account_b)
        self.outcomes = ["kill"]
        with self.assertRaises(_Killed):
            self._repaint(self._stdin_payload())
        line = self._repaint(self._stdin_payload())
        self.assertEqual(len(self.fetches), 1)
        self.assertIn("Session", line)


class KillGuardProviderRowsTest(_GuardHarness):
    """Test d: the API-path fallback render keeps the provider rows."""

    def _plant_stale_api_cache(self):
        data = {"timestamp": time.time() - 600, "line": "old",
                "usage": json.loads(json.dumps(STALE_CORE)), "plan": "Max 20x"}
        Path(cs.get_cache_path()).write_text(json.dumps(data), encoding="utf-8")

    def _with_sentinel_rows(self):
        seen = []

        def append(line, config):
            seen.append(line)
            return line + "\n" + PROVIDER_ROW

        patch = mock.patch.object(cs, "_append_provider_rows", append)
        patch.start()
        self.addCleanup(patch.stop)
        return seen

    def test_guard_fallback_keeps_provider_rows(self):
        self._use(None)
        self._plant_stale_api_cache()
        seen = self._with_sentinel_rows()
        self.outcomes = ["kill"]
        with self.assertRaises(_Killed):
            self._repaint()  # API path: the usage fetch is killed

        printed = self._repaint()

        self.assertEqual(len(self.fetches), 1, "the guard must still hold")
        self.assertIn(PROVIDER_ROW, printed,
                      "the kill-guard render dropped the provider rows")
        lines = printed.splitlines()
        self.assertEqual(lines[-1].strip(), PROVIDER_ROW)
        self.assertIn("42%", lines[0], "the stale cache must still render")
        self.assertTrue(seen)

    def test_guard_placeholder_keeps_provider_rows(self):
        self._use(self.account_b)
        self._with_sentinel_rows()
        self._arm_and_die()  # nothing cached and the guard is armed

        printed = self._repaint()

        self.assertEqual(self.fetches, [])
        self.assertIn("retrying shortly", printed)
        self.assertIn(PROVIDER_ROW, printed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
