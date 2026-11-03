"""A new session must not inherit the previous session's context % and cost.

Issue #55: the stdin context is persisted to one file per account and every
payload is merged on top of it, so that a repaint arriving without a field
keeps the last known value. Claude Code sends ``context_window.used_percentage:
null`` at the start of a session, so the merge handed the *previous* session's
context %, cost and the rest to the new one until its first reply arrived.
With two sessions open at once, each bar could show the other's numbers.

Per-session fields are now dropped when the session id changes; only the
account-wide ones (rate limits, Claude Code version, spend limit) carry over.

Run: ``python tests/test_stdin_session_scope.py``
"""
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal_fixtures import host_terminal_width  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class SessionScopeTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name) / "state"
        self.state.mkdir()
        cfg_dir = Path(self._td.name) / "claude"
        cfg_dir.mkdir()
        self.fetches = []

        def fake_fetch(token, timeout=10):
            self.fetches.append(1)
            return {"five_hour": {"utilization": 1, "resets_at": None},
                    "seven_day": {"utilization": 2, "resets_at": None}}

        for target, value in (("get_state_dir", lambda: self.state),
                              ("_claude_config_dir", lambda: cfg_dir),
                              ("get_config_path", lambda: Path(self._td.name) / "config.json"),
                              # main() fits the line to the terminal it finds; pin
                              # "no terminal" so the contributor's window cannot cut it.
                              ("_detect_terminal_width", lambda: None),
                              ("check_for_update", lambda: False),
                              ("check_claude_code_update", lambda: False),
                              ("get_credentials", lambda: ("token", "Max 20x")),
                              ("fetch_usage", fake_fetch)):
            patcher = mock.patch.object(cs, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        show = dict(cs.DEFAULT_SHOW, branch=False, model=False, providers=False)
        cfg = {"show": show, "layout": "standard", "currency": "$"}
        patcher = mock.patch.object(cs, "load_config", lambda: dict(cfg, show=dict(show)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _repaint(self, payload):
        stdout, stdin = sys.stdout, sys.stdin
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        sys.stdin = io.StringIO(json.dumps(payload))
        sys.stdout = _Out()
        try:
            cs.main()
        except SystemExit:
            pass
        finally:
            sys.stdout, sys.stdin = stdout, stdin
        return ANSI.sub("", buf.getvalue().decode())

    @staticmethod
    def _payload(sid, pct=None, cost=None, **extra):
        p = {"session_id": sid, "model": {"id": "claude-opus-5-5"},
             "workspace": {"current_dir": "."},
             "context_window": {"used_percentage": pct},
             "rate_limits": {"five_hour": {"used_percentage": 23, "resets_at": None},
                             "seven_day": {"used_percentage": 67, "resets_at": None}}}
        if cost is not None:
            p["cost"] = {"total_cost_usd": cost}
        p.update(extra)
        return p

    def _persisted(self):
        return json.loads((cs.get_account_state_dir() / "stdin_ctx.json").read_text(encoding="utf-8"))

    def test_context_and_cost_draw_whatever_width_the_hosts_terminal_has(self):
        """main() fits the line to the terminal it finds; the fake stdout has
        no fileno, so an ordinary 80-column window used to cut the context % and
        cost, and every assertNotIn below would then pass for the wrong reason."""
        for columns in (80, 160):
            with self.subTest(columns=columns):
                with host_terminal_width(columns):
                    line = self._repaint(self._payload("s1", pct=45, cost=0.43))
                self.assertIn("45%", line, "fitted to a %d-column host terminal" % columns)
                self.assertIn("$0.43", line, "fitted to a %d-column host terminal" % columns)

    def test_new_session_starts_without_the_old_context_and_cost(self):
        line = self._repaint(self._payload("s1", pct=45, cost=0.43))
        self.assertIn("45%", line)
        self.assertIn("$0.43", line)

        # The reporter's repro: a brand-new session, no API call made yet.
        line = self._repaint(self._payload("s2", pct=None))
        self.assertNotIn("45%", line, "the old session's context % leaked into the new one")
        self.assertNotIn("$0.43", line, "the old session's cost leaked into the new one")
        persisted = self._persisted()
        self.assertEqual(persisted["session_id"], "s2")
        self.assertNotIn("context_pct", persisted)
        self.assertNotIn("cost_usd", persisted)

    def test_same_session_keeps_its_context_through_a_null_repaint(self):
        """The merge exists for this case: a repaint without the field must
        not blank what the same session already showed."""
        self._repaint(self._payload("s1", pct=45, cost=0.43))
        line = self._repaint(self._payload("s1", pct=None))
        self.assertIn("45%", line)
        self.assertIn("$0.43", line)

    def test_account_wide_fields_survive_a_session_change(self):
        self._repaint(self._payload("s1", pct=45))
        payload = self._payload("s2")
        del payload["rate_limits"]
        line = self._repaint(payload)
        self.assertIn("23%", line, "the account's rate limits should carry over")
        self.assertEqual(self.fetches, [], "carried-over limits mean no usage-API call")

    def test_two_sessions_interleaved_keep_their_own_numbers(self):
        self._repaint(self._payload("s1", pct=45, cost=0.43))
        line = self._repaint(self._payload("s2", pct=10, cost=0.10))
        self.assertIn("10%", line)
        self.assertNotIn("45%", line)
        line = self._repaint(self._payload("s1", pct=None))
        self.assertNotIn("10%", line, "session 1 showed session 2's context")
        self.assertNotIn("$0.10", line, "session 1 showed session 2's cost")

    def test_fragment_repaint_keeps_the_owning_session(self):
        self._repaint(self._payload("s1", pct=45, cost=0.43))
        line = self._repaint({"cost": {"total_cost_usd": 0.5}})   # no session identity
        self.assertIn("45%", line)
        self.assertEqual(self._persisted()["session_id"], "s1")
        line = self._repaint(self._payload("s2", pct=None))
        self.assertNotIn("45%", line)

    def test_a_file_from_an_older_version_has_no_session_id(self):
        """Without an owner recorded there is nothing to compare: behave as before."""
        self._repaint(self._payload("s1", pct=45))
        path = cs.get_account_state_dir() / "stdin_ctx.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["session_id"]
        path.write_text(json.dumps(data), encoding="utf-8")
        line = self._repaint(self._payload("s2", pct=None))
        self.assertIn("45%", line)
        self.assertEqual(self._persisted()["session_id"], "s2")

    def test_corrupt_persisted_file_is_ignored(self):
        path = cs.get_account_state_dir() / "stdin_ctx.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        line = self._repaint(self._payload("s1", pct=45))
        self.assertIn("45%", line)
        self.assertEqual(self._persisted()["session_id"], "s1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
