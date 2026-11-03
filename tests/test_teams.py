"""Tests for agent-team tracking: TeammateIdle, TaskCreated and TaskCompleted.

Claude Code agent teams fire three hook events the status line consumes to
show the team roster and shared-task progress. Covered here:

* marker-file bookkeeping — the same concurrency-safe design as the subagent
  counter, including out-of-order and repeated events, racing writers,
  session scoping, path traversal, the TTL rules and the pruner's
  one-level-deeper scan;
* the rendered segment in its three layouts;
* the hook handlers' hard contract. Unlike the subagent hooks these events can
  steer the team: exit code 2 keeps a teammate working, deletes a new task or
  refuses a completion, and ``{"continue": false}`` on stdout stops a
  teammate. So the handlers must print nothing and always exit 0;
* ``--install-hooks`` registering the three events idempotently, with a
  command that runs — and exits 0 — in every shell Claude Code uses.

Run: ``python tests/test_teams.py``
"""
import io
import json
import os
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
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402


def _age(path, seconds):
    """Backdate every file and dir under *path* (inclusive) by *seconds*."""
    old = time.time() - seconds
    for p in [path, *Path(path).rglob("*")]:
        os.utime(p, (old, old))


class _StateDirMixin:
    """Redirect the shared state dir into a throwaway temp dir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(
            cs, "get_state_dir", lambda: Path(self._td.name))
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._td.cleanup)

    def _created(self, task_id="1", session="s1", teammate=None, subject="Do a thing"):
        data = {"session_id": session, "task_id": task_id, "task_subject": subject}
        if teammate is not None:
            data["teammate_name"] = teammate
        cs._record_team_event("created", data)

    def _completed(self, task_id="1", session="s1", teammate=None, subject="Do a thing"):
        data = {"session_id": session, "task_id": task_id, "task_subject": subject}
        if teammate is not None:
            data["teammate_name"] = teammate
        cs._record_team_event("completed", data)

    def _idle(self, name="impl", session="s1"):
        cs._record_team_event("idle", {"session_id": session, "teammate_name": name})


class TeamTaskStateTest(_StateDirMixin, unittest.TestCase):
    """Task markers: ``tasks/<id>.open`` renamed to ``.done`` on completion."""

    def test_created_then_completed_counts(self):
        self._created("1")
        self._created("2")
        self.assertEqual(cs._count_team("s1"), (0, 0, 2, 0))
        self._completed("1")
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 1))
        self._completed("2")
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 2))

    def test_completed_before_created_is_a_tombstone(self):
        """A completion that overtakes its creation must not let the late
        creation reopen the task — same rule as a subagent's Stop-before-Start."""
        self._completed("fast")
        self._created("fast")
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 1))
        tasks = cs._team_root() / "s1" / "tasks"
        self.assertFalse((tasks / "fast.open").exists())

    def test_duplicate_completion_is_idempotent(self):
        self._created("1")
        self._completed("1")
        self._completed("1")
        self._completed("1")
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 1))
        # A repeat must not overwrite the first record with a tombstone.
        record = json.loads((cs._team_root() / "s1" / "tasks" / "1.done")
                            .read_text(encoding="utf-8"))
        self.assertIn("created", record)

    def test_duplicate_completion_without_creation_is_idempotent(self):
        self._completed("x")
        self._completed("x")
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 1))

    def test_duplicate_creation_is_idempotent(self):
        self._created("1")
        self._created("1")
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 0))

    def test_reused_task_id_reopens_after_a_full_cycle(self):
        """Task ids are small counters. A creation for an id whose whole
        create→complete cycle was already seen is a new task, not a race."""
        self._created("1")
        self._completed("1")
        self._created("1")
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 0))

    def test_parallel_creates_lose_no_tasks(self):
        """30 simultaneous TaskCreated hooks must all be counted. A shared
        read-modify-write JSON loses most of them to the race."""
        threads = [threading.Thread(target=self._created, args=(f"task{i}",))
                   for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(cs._count_team("s1"), (0, 0, 30, 0))

    def test_parallel_completions_lose_no_tasks(self):
        for i in range(30):
            self._created(f"task{i}")
        threads = [threading.Thread(target=self._completed, args=(f"task{i}",))
                   for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 30))

    def test_task_caught_with_both_markers_counts_as_done(self):
        tasks = cs._team_root() / "s1" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "twin.open").write_text("{}", encoding="utf-8")
        (tasks / "twin.done").write_text("{}", encoding="utf-8")
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 1))

    def test_missing_or_unusable_task_id_is_ignored(self):
        """No fabricated ids: there is no spawn cap to protect, so a made-up
        id could only ever show as a phantom open task."""
        for bad in (None, "", "   ", "..", "///", "-._", [], {"x": 1}, 1.5, True):
            cs._record_team_event("created", {
                "session_id": "s1", "task_id": bad, "teammate_name": "impl"})
            cs._record_team_event("completed", {
                "session_id": "s1", "task_id": bad, "teammate_name": "impl"})
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 0))

    def test_numeric_task_id_is_accepted(self):
        cs._record_team_event("created", {"session_id": "s1", "task_id": 7})
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 0))

    def test_open_tasks_do_not_age_out(self):
        """A pending task has no missed goodbye event. Expiring it by age
        would paint an unfinished list as all done."""
        self._created("slow")
        self._created("quick")
        self._completed("quick")
        marker = cs._team_root() / "s1" / "tasks" / "slow.open"
        old = time.time() - cs.SUBAGENT_STATE_TTL - 60
        os.utime(marker, (old, old))
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 1))

    def test_subject_is_sanitised_and_capped(self):
        self._created("1", subject="\x1b[31mred\x1b[0m line\nbreak " + "y" * 200)
        record = json.loads((cs._team_root() / "s1" / "tasks" / "1.open")
                            .read_text(encoding="utf-8"))
        self.assertLessEqual(len(record["subject"]), 60)
        self.assertTrue(record["subject"].startswith("red line break y"))
        self.assertNotIn("\x1b", record["subject"])
        self.assertNotIn("\n", record["subject"])
        self.assertEqual(record["owner"], "")
        self.assertIsInstance(record["created"], float)

    def test_owner_is_recorded(self):
        self._created("1", teammate="implementer")
        record = json.loads((cs._team_root() / "s1" / "tasks" / "1.open")
                            .read_text(encoding="utf-8"))
        self.assertEqual(record["owner"], "implementer")

    def test_completing_an_old_task_keeps_the_session_dir(self):
        """os.replace keeps the creation mtime: without a touch, completing a
        task older than the TTL leaves the newest marker looking ancient and
        the very next prune erases the session's whole task list."""
        self._created("marathon")
        _age(cs._team_root() / "s1", cs.SUBAGENT_STATE_TTL + 60)
        self._completed("marathon")
        cs._prune_subagent_dirs(cs._team_root())
        self.assertTrue((cs._team_root() / "s1").exists())
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 1))

    def test_writer_survives_a_concurrent_prune_sweep(self):
        """A pruner in another process can rmtree the dir between the
        writer's mkdir and its marker write; one retry restores the event."""
        import shutil as _shutil
        real_mkdir = cs._secure_mkdir
        swept = {"done": False}

        def sweeping_mkdir(path):
            real_mkdir(path)
            if not swept["done"]:
                swept["done"] = True
                _shutil.rmtree(path, ignore_errors=True)

        with mock.patch.object(cs, "_secure_mkdir", sweeping_mkdir):
            self._created("phoenix")
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 0))
        swept["done"] = False
        with mock.patch.object(cs, "_secure_mkdir", sweeping_mkdir):
            self._idle("phoenix-mate")
        self.assertEqual(cs._count_team("s1")[0], 1)


class TeammateStateTest(_StateDirMixin, unittest.TestCase):
    """Teammate markers: ``teammates/<name>.idle`` or ``.working``."""

    def _mates(self, session="s1"):
        return sorted(p.name for p in (cs._team_root() / session / "teammates").iterdir())

    def test_idle_and_working_transitions(self):
        self._created("1", teammate="impl")          # acted → working
        self.assertEqual(cs._count_team("s1"), (1, 1, 1, 0))
        self._idle("impl")
        self.assertEqual(cs._count_team("s1"), (1, 0, 1, 0))
        self._completed("1", teammate="impl")        # acted again → working
        self.assertEqual(cs._count_team("s1"), (1, 1, 0, 1))
        self._idle("impl")
        self.assertEqual(cs._count_team("s1"), (1, 0, 0, 1))

    def test_first_sighting_via_idle(self):
        """No event names a teammate as it starts: an idle is enough to know it."""
        self._idle("researcher")
        self.assertEqual(cs._count_team("s1"), (1, 0, 0, 0))

    def test_transition_leaves_exactly_one_marker(self):
        self._created("1", teammate="impl")
        self.assertEqual(self._mates(), ["impl.working"])
        self._idle("impl")
        self.assertEqual(self._mates(), ["impl.idle"])

    def test_several_teammates_count_separately(self):
        self._created("1", teammate="a")
        self._created("2", teammate="b")
        self._idle("c")
        self._idle("a")
        self.assertEqual(cs._count_team("s1"), (3, 1, 2, 0))

    def test_newer_marker_wins_while_both_exist(self):
        """Racing writers can leave both names in place; the newer mtime is
        the current state until the next transition merges them."""
        mates = cs._team_root() / "s1" / "teammates"
        mates.mkdir(parents=True)
        (mates / "m.idle").write_text("{}", encoding="utf-8")
        (mates / "m.working").write_text("{}", encoding="utf-8")
        now = time.time()
        os.utime(mates / "m.idle", (now - 10, now - 10))
        os.utime(mates / "m.working", (now - 5, now - 5))
        self.assertEqual(cs._count_team("s1"), (1, 1, 0, 0))
        os.utime(mates / "m.idle", (now - 1, now - 1))
        self.assertEqual(cs._count_team("s1"), (1, 0, 0, 0))

    def test_stale_working_teammate_is_not_working(self):
        """A teammate stopped or crashed without an idle event must stop
        counting as working past the TTL (but is still a known teammate)."""
        self._created("1", teammate="ghost")
        marker = cs._team_root() / "s1" / "teammates" / "ghost.working"
        old = time.time() - cs.SUBAGENT_STATE_TTL - 60
        os.utime(marker, (old, old))
        self.assertEqual(cs._count_team("s1"), (1, 0, 1, 0))

    def test_lead_is_not_a_teammate(self):
        """Claude Code's roster names the lead ``team-lead``; its own task
        actions update the list but never the teammate count."""
        self._created("1", teammate="team-lead")
        self._idle("team-lead")
        self.assertEqual(cs._count_team("s1"), (0, 0, 1, 0))

    def test_invalid_names_are_ignored(self):
        for bad in (None, "", "   ", "..", "\x1b[31m", [], {"x": 1}, 3.5, False):
            cs._record_team_event("idle", {"session_id": "s1", "teammate_name": bad})
        self.assertEqual(cs._count_team("s1"), (0, 0, 0, 0))

    def test_unsafe_names_stay_distinct(self):
        """Names _safe_path_id has to alter get a digest suffix, so they
        neither collide with a plain name nor vanish entirely."""
        for name in ("Zo", "Zoë", "研究者", "a b", "ab"):
            self._idle(name)
        self.assertEqual(cs._count_team("s1"), (5, 0, 0, 0))

    def test_windows_device_names_are_not_used_as_file_stems(self):
        for name in ("con", "NUL", "com1"):
            self._idle(name)
        self.assertEqual(cs._count_team("s1"), (3, 0, 0, 0))
        stems = {n.rsplit(".", 1)[0].upper() for n in self._mates()}
        self.assertFalse(stems & {"CON", "NUL", "COM1"}, stems)

    def test_names_differing_only_by_case_stay_distinct(self):
        """NTFS and APFS are case-insensitive: "impl" and "Impl" as bare file
        names are one marker, so each teammate's transitions would clobber
        the other's. Capitals get a digest suffix to keep them apart."""
        self._idle("impl")
        self._created("1", teammate="Impl")
        self._idle("IMPL")
        self.assertEqual(cs._count_team("s1"), (3, 1, 1, 0))
        folded = [n.lower() for n in self._mates()]
        self.assertEqual(len(folded), len(set(folded)), self._mates())


class TeammateRaceTest(_StateDirMixin, unittest.TestCase):
    """Two writers for one teammate must never leave it with no marker.

    The first design wrote the new state, then deleted the other one; two
    concurrent writers could each delete the other's marker and the teammate
    vanished from the roster. Deleting only a strictly older marker still
    fails: between one writer's check and its unlink, the other can rewrite
    that marker and delete this one. Transitions now only rename.
    """

    def _mates_dir(self):
        return cs._team_root() / "s1" / "teammates"

    def _markers(self):
        d = self._mates_dir()
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def _preempted(self, a_state, b_state, preempt_at, initial):
        """Run writer A; at its *preempt_at*-th filesystem mutation, run writer
        B to completion first, then let A carry on. Returns A's mutation count."""
        mates = self._mates_dir()
        if initial:
            cs._set_teammate_state(mates, "m", initial)
        calls = {"n": 0, "in_b": False}
        real_replace, real_unlink, real_open = os.replace, os.unlink, open

        def preempt():
            if calls["in_b"]:
                return
            calls["n"] += 1
            if calls["n"] == preempt_at:
                calls["in_b"] = True
                try:
                    # Real racers are microseconds-to-milliseconds apart, but
                    # NTFS stamps mtimes from a coarse clock: without a gap,
                    # both writers share one tick and an "is it older?" check
                    # never fires, hiding the race this test exists to catch.
                    time.sleep(0.03)
                    cs._set_teammate_state(mates, "m", b_state)
                    time.sleep(0.03)
                finally:
                    calls["in_b"] = False

        def replace(*a, **k):
            preempt()
            return real_replace(*a, **k)

        def unlink(*a, **k):
            preempt()
            return real_unlink(*a, **k)

        def open_(*a, **k):
            preempt()
            return real_open(*a, **k)

        with mock.patch.object(cs.os, "replace", replace), \
             mock.patch.object(cs.os, "unlink", unlink), \
             mock.patch.object(cs, "open", open_, create=True):
            cs._set_teammate_state(mates, "m", a_state)
        return calls["n"]

    def _reset(self):
        import shutil as _shutil
        _shutil.rmtree(cs._team_root(), ignore_errors=True)

    def test_no_single_preemption_point_loses_the_teammate(self):
        """Exhaustive: every point at which writer B can cut into writer A,
        for both transition directions and every starting state."""
        cases = 0
        for a_state, b_state in (("working", "idle"), ("idle", "working")):
            for initial in (None, "idle", "working"):
                self._reset()
                total = self._preempted(a_state, b_state, 10 ** 9, initial)
                for point in range(1, total + 1):
                    self._reset()
                    self._preempted(a_state, b_state, point, initial)
                    ctx = (a_state, b_state, initial, point)
                    self.assertTrue(self._markers(), ctx)
                    self.assertEqual(cs._count_team("s1")[0], 1, ctx)
                    # The next uncontended transition merges any leftovers.
                    cs._set_teammate_state(self._mates_dir(), "m", "idle")
                    self.assertEqual(self._markers(), ["m.idle"], ctx)
                    cases += 1
        self.assertGreater(cases, 6)

    def test_concurrent_writers_never_lose_the_teammate(self):
        """Real threads hammering opposite states for the same teammate."""
        mates = self._mates_dir()
        for _round in range(5):
            self._reset()
            errors = []

            def flip(state):
                try:
                    for _ in range(150):
                        cs._set_teammate_state(mates, "m", state)
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)

            threads = [threading.Thread(target=flip, args=(s,))
                       for s in ("working", "idle", "working", "idle")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertIn(len(self._markers()), (1, 2), self._markers())
            self.assertEqual(cs._count_team("s1")[0], 1)
            cs._set_teammate_state(mates, "m", "working")
            self.assertEqual(self._markers(), ["m.working"])
            self.assertEqual(cs._count_team("s1")[:2], (1, 1))


class TeamScopeAndPruneTest(_StateDirMixin, unittest.TestCase):

    def test_sessions_do_not_mix(self):
        self._created("1", session="sessA", teammate="a")
        self._created("1", session="sessB", teammate="b")
        self._created("2", session="sessB")
        self._completed("1", session="sessB", teammate="b")
        self._idle("b", session="sessB")
        self.assertEqual(cs._count_team("sessA"), (1, 1, 1, 0))
        self.assertEqual(cs._count_team("sessB"), (1, 0, 1, 1))
        self.assertEqual(cs._count_team("sessC"), (0, 0, 0, 0))

    def test_missing_session_id_is_a_noop(self):
        cs._record_team_event("created", {"task_id": "1"})
        cs._record_team_event("idle", {"teammate_name": "a"})
        root = cs._team_root()
        self.assertFalse(root.exists() and any(root.iterdir()))

    def test_unknown_event_is_a_noop(self):
        cs._record_team_event("bogus", {"session_id": "s1", "task_id": "1",
                                        "teammate_name": "a"})
        root = cs._team_root()
        self.assertFalse(root.exists() and any(root.iterdir()))

    def test_malformed_event_data_never_raises(self):
        for bad in ({}, {"session_id": None}, {"session_id": 7, "task_id": []},
                    {"session_id": "s", "task_id": {"x": 1}, "teammate_name": ["a"]},
                    {"session_id": "s", "task_id": "1", "task_subject": {"x": 1}},
                    {"session_id": "s", "task_id": "1", "task_subject": None}):
            for event in ("idle", "created", "completed"):
                cs._record_team_event(event, bad)

    def test_prune_removes_stale_team_dirs(self):
        self._created("1", session="dead", teammate="a")
        self._idle("b", session="dead")
        self._created("1", session="alive", teammate="c")
        _age(cs._team_root() / "dead", cs.SUBAGENT_STATE_TTL + 60)
        cs._prune_subagent_dirs(cs._team_root())
        self.assertFalse((cs._team_root() / "dead").exists())
        self.assertEqual(cs._count_team("alive"), (1, 1, 1, 0))

    def test_prune_judges_by_the_nested_markers_not_the_subdirs(self):
        """Team markers sit one level down. Stale markers inside a freshly
        touched subdir are still stale; one fresh marker keeps the session."""
        self._created("1", session="old")
        _age(cs._team_root() / "old", cs.SUBAGENT_STATE_TTL + 60)
        now = time.time()
        os.utime(cs._team_root() / "old" / "tasks", (now, now))  # subdir fresh
        cs._prune_subagent_dirs(cs._team_root())
        self.assertFalse((cs._team_root() / "old").exists())

        self._created("1", session="busy")
        _age(cs._team_root() / "busy", cs.SUBAGENT_STATE_TTL + 60)
        self._idle("mate", session="busy")                     # one fresh marker
        cs._prune_subagent_dirs(cs._team_root())
        self.assertTrue((cs._team_root() / "busy").exists())
        self.assertEqual(cs._count_team("busy"), (1, 0, 1, 0))

    def test_rewriting_a_marker_keeps_the_session_alive(self):
        """A teammate going idle again rewrites its marker in place, which
        leaves the subdir's own mtime untouched; the nested scan must see it."""
        self._idle("mate")
        _age(cs._team_root() / "s1", cs.SUBAGENT_STATE_TTL + 60)
        self._idle("mate")
        old_dir_mtime = (cs._team_root() / "s1" / "teammates").stat().st_mtime
        self.assertLess(old_dir_mtime, time.time() - cs.SUBAGENT_STATE_TTL)
        cs._prune_subagent_dirs(cs._team_root())
        self.assertTrue((cs._team_root() / "s1").exists())

    def test_empty_fresh_session_dir_is_kept(self):
        """A concurrent hook mid-mkdir must not be swept."""
        (cs._team_root() / "fresh").mkdir(parents=True)
        cs._prune_subagent_dirs(cs._team_root())
        self.assertTrue((cs._team_root() / "fresh").exists())

    def test_subagent_dirs_are_left_to_their_own_root(self):
        """Team events prune the team root only; subagent state is untouched."""
        cs._record_subagent_event("start", {"agent_id": "a1", "session_id": "s1"})
        _age(cs._subagent_root() / "s1", cs.SUBAGENT_STATE_TTL + 60)
        self._created("1")
        self.assertTrue((cs._subagent_root() / "s1").exists())


HOSTILE_PAYLOADS = [
    # (event, payload) — relative traversal, both separators, in every
    # attacker-controlled field. Deep enough to climb out of <state>/teams.
    (event, dict(fields, task_subject="x"))
    for event in ("created", "completed", "idle")
    for fields in (
        {"session_id": "../../../../escape-session", "task_id": "1", "teammate_name": "m"},
        {"session_id": "..\\..\\..\\..\\escape-session", "task_id": "1", "teammate_name": "m"},
        {"session_id": "s", "task_id": "../../../../../escape-task",
         "teammate_name": "../../../../../escape-mate"},
        {"session_id": "s", "task_id": "..\\..\\..\\..\\..\\escape-task",
         "teammate_name": "..\\..\\..\\..\\..\\escape-mate"},
    )
]


def _outside(root, allowed):
    """Every path under *root* that is neither inside *allowed* nor one of
    its ancestors (the dirs the state root legitimately creates)."""
    allowed = allowed.resolve()
    ancestors = {p.resolve() for p in allowed.parents}
    bad = []
    for p in Path(root).rglob("*"):
        rp = p.resolve()
        if rp == allowed or allowed in rp.parents or rp in ancestors:
            continue
        bad.append(str(p))
    return bad


class TeamPathTraversalTest(unittest.TestCase):
    """Hostile ids must never write outside ``<state>/teams``.

    The state dir is nested a few levels inside the temp dir and the whole
    temp dir is listed afterwards, so a write that climbs out of the state
    dir still lands somewhere this test can see it — listing only the state
    dir itself could never fail.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def test_hostile_ids_stay_inside_the_team_root(self):
        state = Path(self._td.name) / "a" / "b" / "state"
        with mock.patch.object(cs, "get_state_dir", lambda: state):
            for event, payload in HOSTILE_PAYLOADS:
                cs._record_team_event(event, payload)
            teams = cs._team_root()
        self.assertTrue(teams.exists())         # the events did write something
        self.assertEqual(_outside(self._td.name, teams), [])

    def test_hostile_ids_through_the_real_cli(self):
        """Same, through the real entry point: real argv, real stdin bytes."""
        base = Path(self._td.name) / "a" / "b"
        env = dict(os.environ, LOCALAPPDATA=str(base), XDG_CACHE_HOME=str(base))
        flags = {"created": "--hook-task-created", "completed": "--hook-task-completed",
                 "idle": "--hook-teammate-idle"}
        for event, payload in HOSTILE_PAYLOADS:
            r = subprocess.run([sys.executable, str(SCRIPT), flags[event]],
                               input=json.dumps(payload).encode("utf-8"),
                               capture_output=True, env=env, timeout=60)
            self.assertEqual((r.returncode, r.stdout), (0, b""), (event, payload, r.stderr))
        teams = base / "claude-status" / "teams"
        self.assertTrue(teams.exists())
        self.assertEqual(_outside(self._td.name, teams), [])

    def test_absolute_ids_do_not_reach_a_drive_root(self):
        """An absolute id would bypass the temp dir entirely, so check the
        roots it would actually land in, under a name unique to this run."""
        import uuid
        tag = f"pulse-escape-{uuid.uuid4().hex[:12]}"
        state = Path(self._td.name) / "a" / "b" / "state"
        spellings = [f"/{tag}", f"\\{tag}"]
        roots = {Path(Path.cwd().anchor), Path(Path(self._td.name).resolve().anchor)}
        if sys.platform == "win32":
            spellings += [f"C:\\{tag}", f"C:/{tag}", f"\\\\?\\C:\\{tag}"]
            roots.add(Path("C:\\"))
        with mock.patch.object(cs, "get_state_dir", lambda: state):
            for event in ("created", "completed", "idle"):
                for bad in spellings:
                    cs._record_team_event(event, {"session_id": bad, "task_id": bad,
                                                  "teammate_name": bad})
                    cs._record_team_event(event, {"session_id": "s", "task_id": bad,
                                                  "teammate_name": bad})
            teams = cs._team_root()
        self.assertEqual(_outside(self._td.name, teams), [])
        for root in roots:
            leaked = [p.name for p in root.iterdir() if tag in p.name]
            self.assertEqual(leaked, [], root)


class TeamRenderTest(_StateDirMixin, unittest.TestCase):
    """Segment strings. ``_sanitize`` strips the ANSI colouring for readable
    assertions, the same way the subagent render tests do."""

    def _render(self, counts, layout="standard"):
        with mock.patch.object(cs, "_count_team", return_value=counts):
            out = cs._render_team({"layout": layout}, "s")
        return out if out is None else cs._sanitize(out)

    def test_nothing_rendered_before_any_team_event(self):
        self.assertIsNone(cs._render_team({}, "s1"))
        self.assertIsNone(self._render((0, 0, 0, 0)))

    def test_no_session_id_shows_nothing(self):
        self._created("1", teammate="a")
        self.assertIsNone(cs._render_team({}, None))
        self.assertIsNone(cs._render_team({}, ""))

    def test_full_layout(self):
        # (teammates, working, tasks_open, tasks_done)
        self.assertEqual(self._render((3, 2, 3, 4)), "team 3 · tasks 4/7")

    def test_compact_layout(self):
        self.assertEqual(self._render((3, 2, 3, 4), "compact"), "⚑3 4/7")

    def test_minimal_layout(self):
        self.assertEqual(self._render((3, 2, 3, 4), "minimal"), "⚑3")

    def test_tasks_only_omits_the_teammate_part(self):
        self.assertEqual(self._render((0, 0, 3, 4)), "tasks 4/7")
        self.assertEqual(self._render((0, 0, 3, 4), "compact"), "⚑4/7")
        self.assertEqual(self._render((0, 0, 3, 4), "minimal"), "⚑4/7")

    def test_teammates_only_omits_the_task_part(self):
        self.assertEqual(self._render((2, 1, 0, 0)), "team 2")
        self.assertEqual(self._render((2, 1, 0, 0), "compact"), "⚑2")
        self.assertEqual(self._render((2, 1, 0, 0), "minimal"), "⚑2")

    def test_working_count_is_not_displayed(self):
        """The hooks cannot observe who is working right now (claiming or
        doing work fires nothing), so a working count would read "0" for a
        busy team. Only the roster size is shown, in every layout."""
        for layout in ("standard", "compact", "minimal"):
            busy = self._render((3, 3, 1, 1), layout)
            quiet = self._render((3, 0, 1, 1), layout)
            self.assertEqual(busy, quiet, layout)
            self.assertNotIn("working", busy)

    def test_finished_task_list_turns_green(self):
        with mock.patch.object(cs, "_count_team", return_value=(1, 0, 0, 5)):
            done = cs._render_team({}, "s")
        with mock.patch.object(cs, "_count_team", return_value=(1, 0, 1, 4)):
            pending = cs._render_team({}, "s")
        self.assertIn(f"{cs.GREEN}5", done)
        self.assertNotIn(cs.GREEN, pending)

    def test_roster_is_never_tinted(self):
        """No cap to escalate against, so no traffic-light colouring."""
        with mock.patch.object(cs, "_count_team", return_value=(20, 20, 0, 0)):
            out = cs._render_team({}, "s")
        for colour in (cs.GREEN, cs.YELLOW, cs.RED):
            self.assertNotIn(colour, out)

    def test_render_is_scoped_to_the_painting_session(self):
        self._created("1", session="mine", teammate="a")
        self._created("1", session="theirs", teammate="b")
        self._created("2", session="theirs", teammate="c")
        out = cs._sanitize(cs._render_team({}, "mine"))
        self.assertEqual(out, "team 1 · tasks 0/1")
        self.assertIsNone(cs._render_team({}, "unknown-session"))

    def test_segment_sits_right_after_the_subagent_counter(self):
        self.assertEqual(cs.WIDGET_PRIORITY["team"], 179)
        self.assertEqual(cs.WIDGET_PRIORITY["subagents"] + 1, cs.WIDGET_PRIORITY["team"])
        self.assertTrue(cs.DEFAULT_SHOW["team"])


class TeamSegmentInStatusLineTest(unittest.TestCase):
    """The ``team`` show-key is wired into the real status-line builder."""

    def _line(self, show):
        cfg = {"show": dict(show, branch=False)}
        with tempfile.TemporaryDirectory() as td:
            # Same sandboxing as the PR badge test: the state dir lives under
            # LOCALAPPDATA / XDG_CACHE_HOME, not CLAUDE_CONFIG_DIR.
            env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td}
            with mock.patch.dict(os.environ, env):
                cs._record_team_event("created", {
                    "session_id": "live-sess", "task_id": "1", "teammate_name": "a"})
                cs._record_subagent_event("start", {
                    "session_id": "live-sess", "agent_id": "ag1"})
                return cs._sanitize(cs.build_status_line(
                    {}, "", cfg, {"session_id": "live-sess"}))

    def test_shown_by_default_after_the_agent_counter(self):
        line = self._line({})
        self.assertIn("team 1 · tasks 0/1", line)
        self.assertLess(line.index("agents"), line.index("team 1 ·"))

    def test_hide_team_removes_only_the_team_segment(self):
        line = self._line({"team": False})
        self.assertNotIn("team 1", line)
        self.assertNotIn("tasks 0/1", line)
        self.assertIn("agents", line)


class TeamHookHandlerTest(_StateDirMixin, unittest.TestCase):
    """Hard contract: whatever arrives on stdin, print nothing, raise nothing."""

    JUNK = ("", "   ", "{not json", "[]", "null", "42", '"str"', "\x00\xff garbage",
            '{"continue": false}', '{"decision": "block"}',
            "[" * 100000 + "]" * 100000,
            json.dumps({"session_id": ["x"], "task_id": {"a": 1}}))

    def _run(self, event, stdin):
        """Run the handler with *stdin* and return every byte it wrote to stdout.

        stdout is a real text wrapper over a byte buffer, so writes through
        either ``sys.stdout`` or ``sys.stdout.buffer`` are both captured.
        """
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch.object(cs.sys, "stdin", stdin), \
             mock.patch.object(cs.sys, "stdout", out):
            cs.hook_team(event)            # must return normally
            out.flush()
        return raw.getvalue()

    def test_garbage_text_stdin_writes_nothing(self):
        for event in ("idle", "created", "completed"):
            for junk in self.JUNK:
                self.assertEqual(self._run(event, io.StringIO(junk)), b"",
                                 (event, junk[:40]))

    def test_garbage_byte_stdin_writes_nothing(self):
        for event in ("idle", "created", "completed"):
            for junk in (b"\xff\xfe\x00junk", b"\xc3\x28", b"{\"a\":", b""):
                stdin = io.TextIOWrapper(io.BytesIO(junk), encoding="utf-8")
                self.assertEqual(self._run(event, stdin), b"", (event, junk))

    def test_valid_payload_is_recorded_silently(self):
        payload = json.dumps({"session_id": "s1", "task_id": "task-001",
                              "task_subject": "Implement auth",
                              "teammate_name": "implementer",
                              "hook_event_name": "TaskCreated"})
        self.assertEqual(self._run("created", io.StringIO(payload)), b"")
        self.assertEqual(cs._count_team("s1"), (1, 1, 1, 0))

    def test_utf8_subject_is_decoded_from_bytes(self):
        """Claude Code sends UTF-8; a Windows text-mode stdin would decode it
        with the ANSI code page and garble it, so the handler reads bytes."""
        payload = json.dumps({"session_id": "s1", "task_id": "1",
                              "task_subject": "Łódź café ✓"}, ensure_ascii=False)
        stdin = io.TextIOWrapper(io.BytesIO(payload.encode("utf-8")), encoding="cp1252")
        self.assertEqual(self._run("created", stdin), b"")
        record = json.loads((cs._team_root() / "s1" / "tasks" / "1.open")
                            .read_text(encoding="utf-8"))
        self.assertEqual(record["subject"], "Łódź café ✓")

    def test_internal_failure_is_swallowed(self):
        with mock.patch.object(cs, "_record_team_event", side_effect=RuntimeError("boom")):
            self.assertEqual(self._run("created", io.StringIO('{"session_id": "s"}')), b"")

    def test_exit_requests_and_interrupts_are_swallowed(self):
        """A future sys.exit(2) on this path must not become a blocking exit;
        nothing that escapes the bookkeeping may reach the process status."""
        for exc in (SystemExit(2), SystemExit(1), KeyboardInterrupt(), GeneratorExit()):
            with mock.patch.object(cs, "_record_team_event", side_effect=exc):
                self.assertEqual(self._run("idle", io.StringIO('{"session_id": "s"}')),
                                 b"", repr(exc))

    def test_missing_stdin_is_swallowed(self):
        self.assertEqual(self._run("idle", None), b"")


class TeamHookProcessTest(unittest.TestCase):
    """The real CLI entry point: exit code 0 and empty stdout, as a process."""

    def _run(self, flag, stdin_bytes, state_dir):
        env = dict(os.environ)
        # Sandbox the state dir (LOCALAPPDATA on Windows, XDG_CACHE_HOME
        # elsewhere) so the test never touches the developer's real state.
        env["LOCALAPPDATA"] = state_dir
        env["XDG_CACHE_HOME"] = state_dir
        return subprocess.run([sys.executable, str(SCRIPT), flag],
                              input=stdin_bytes, capture_output=True, env=env,
                              timeout=60)

    def test_every_flag_exits_zero_and_prints_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            for flag in ("--hook-teammate-idle", "--hook-task-created",
                         "--hook-task-completed"):
                for payload in (b"", b"{not json", b"\xff\xfe junk", b"[]",
                                b'{"continue": false}'):
                    r = self._run(flag, payload, td)
                    self.assertEqual(r.returncode, 0, (flag, payload, r.stderr))
                    self.assertEqual(r.stdout, b"", (flag, payload))

    def test_real_event_sequence_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            base = {"session_id": "proc-sess", "transcript_path": "/x.jsonl",
                    "cwd": td, "team_name": "session-procsess"}
            steps = (
                ("--hook-task-created", dict(base, hook_event_name="TaskCreated",
                                             task_id="task-001",
                                             task_subject="Implement user authentication",
                                             task_description="Add login endpoints",
                                             teammate_name="implementer")),
                ("--hook-teammate-idle", dict(base, hook_event_name="TeammateIdle",
                                              permission_mode="default",
                                              teammate_name="researcher")),
                ("--hook-task-completed", dict(base, hook_event_name="TaskCompleted",
                                               permission_mode="default",
                                               task_id="task-001",
                                               task_subject="Implement user authentication",
                                               teammate_name="implementer")),
            )
            for flag, payload in steps:
                r = self._run(flag, json.dumps(payload).encode("utf-8"), td)
                self.assertEqual((r.returncode, r.stdout), (0, b""), (flag, r.stderr))
            with mock.patch.object(cs, "get_state_dir",
                                   lambda: Path(td) / "claude-status"):
                self.assertEqual(cs._count_team("proc-sess"), (2, 1, 0, 1))


class InstallTeamHooksTest(unittest.TestCase):
    """``--install-hooks`` registers the three team events exactly once."""

    TEAM_EVENTS = {"TeammateIdle": "--hook-teammate-idle",
                   "TaskCreated": "--hook-task-created",
                   "TaskCompleted": "--hook-task-completed"}

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.settings = Path(self._td.name) / "settings.json"
        # install_hooks backs settings.json up into the state dir the second
        # time it runs: that must be this test's own dir, not the contributor's.
        state = Path(self._td.name) / "state"
        for patch in (mock.patch.object(cs, "_claude_config_dir", lambda: Path(self._td.name)),
                      mock.patch.object(cs, "get_state_dir", lambda: state),
                      mock.patch.object(cs, "utf8_print", lambda *a, **k: None)):
            patch.start()
            self.addCleanup(patch.stop)
        cs._settings_backed_up.clear()
        self.addCleanup(cs._settings_backed_up.clear)

    def _hooks(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]

    def _pulse_entries(self, hooks, event, flag):
        return [h for h in hooks.get(event, [])
                if any("claude_status.py" in i.get("command", "") and flag in i.get("command", "")
                       for i in h.get("hooks", []))]

    def test_settings_backups_stay_inside_the_test_sandbox(self):
        """The second install over an existing settings.json takes a backup in
        the state dir. Unpatched, that is the contributor's real state dir, and
        every run left tmpXXXX-settings.json.<stamp> files there (each temp-dir
        prefix is unique, so retention never prunes them)."""
        taken = []
        real = cs._backup_settings

        def spy(settings_path):
            dest = real(settings_path)
            taken.append(dest)
            return dest

        with mock.patch.object(cs, "_backup_settings", spy):
            cs.install_hooks()
            cs.install_hooks()
        written = [d for d in taken if d is not None]
        self.assertTrue(written, "the second install took no backup, so this checks nothing")
        sandbox = Path(self._td.name).resolve()
        for dest in written:
            self.assertIn(sandbox, Path(dest).resolve().parents,
                          "a settings backup was written outside the sandbox: %s" % dest)

    def test_adds_exactly_one_entry_per_team_event(self):
        cs.install_hooks()
        hooks = self._hooks()
        for event, flag in self.TEAM_EVENTS.items():
            entries = self._pulse_entries(hooks, event, flag)
            self.assertEqual(len(entries), 1, event)
            entry = entries[0]
            # These events take no matcher; Claude Code's docs omit the key.
            self.assertNotIn("matcher", entry, event)
            command = entry["hooks"][0]["command"]
            self.assertEqual(entry["hooks"][0]["type"], "command")
            self.assertTrue(command.endswith(f"{flag}; exit 0"), command)

    def test_rerunning_is_idempotent(self):
        cs.install_hooks()
        first = self._hooks()
        cs.install_hooks()
        cs.install_hooks()
        self.assertEqual(self._hooks(), first)
        for event, flag in self.TEAM_EVENTS.items():
            self.assertEqual(len(self._pulse_entries(self._hooks(), event, flag)), 1)

    def test_subagent_entries_keep_their_contract(self):
        """SubagentStart cannot block, so it keeps the plain matcher form.
        SubagentStop *can* (exit 2 keeps the subagent running — hooks
        reference), so it gets the same always-exit-0 command as the team
        events; a moved install must never pin every subagent open."""
        cs.install_hooks()
        hooks = self._hooks()
        start = self._pulse_entries(hooks, "SubagentStart", "--hook-subagent-start")
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]["matcher"], "")
        self.assertTrue(start[0]["hooks"][0]["command"].endswith("--hook-subagent-start"))
        stop = self._pulse_entries(hooks, "SubagentStop", "--hook-subagent-stop")
        self.assertEqual(len(stop), 1)
        self.assertNotIn("matcher", stop[0])
        command = stop[0]["hooks"][0]["command"]
        self.assertIn("--hook-subagent-stop", command)
        self.assertTrue(command.rstrip().endswith("exit 0"), command)

    def test_foreign_hooks_on_the_same_events_survive(self):
        mine = {"hooks": [{"type": "command", "command": "run-quality-gate.sh"}]}
        self.settings.write_text(json.dumps({"hooks": {"TaskCompleted": [mine]}}),
                                 encoding="utf-8")
        cs.install_hooks()
        cs.install_hooks()
        entries = self._hooks()["TaskCompleted"]
        self.assertIn(mine, entries)
        self.assertEqual(len(entries), 2)

    def _hook_shells(self):
        """Every shell Claude Code may run a hook command in, as argv prefixes.

        sh elsewhere; on Windows Git Bash, or PowerShell (pwsh 7, falling
        back to Windows PowerShell 5.1) when Git Bash isn't installed.
        """
        import shutil as _shutil
        if sys.platform != "win32":
            return {"sh": ["/bin/sh", "-c"]}
        shells = {}
        bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if bash.exists():
            shells["git-bash"] = [str(bash), "-c"]
        for name, exe in (("pwsh7", "pwsh"), ("powershell5.1", "powershell")):
            found = _shutil.which(exe)
            if found:
                shells[name] = [found, "-NoProfile", "-NonInteractive", "-Command"]
        return shells

    def _installed_command(self):
        cs.install_hooks()
        return self._pulse_entries(self._hooks(), "TaskCreated",
                                   "--hook-task-created")[0]["hooks"][0]["command"]

    def _run_in(self, argv, command, state):
        env = dict(os.environ, LOCALAPPDATA=str(state), XDG_CACHE_HOME=str(state))
        payload = json.dumps({"session_id": "shell-sess", "task_id": "1",
                              "task_subject": "via shell", "teammate_name": "m"})
        return subprocess.run(argv + [command], input=payload.encode("utf-8"),
                              capture_output=True, env=env, timeout=120)

    def test_installed_command_is_shell_neutral_here(self):
        """With a space-free interpreter path the command carries no $HOME
        (a ParserError in PowerShell) and no quoted interpreter."""
        python = Path(sys.executable).as_posix()
        if not cs._SHELL_NEUTRAL_WORD.fullmatch(python):
            self.skipTest(f"interpreter path needs quoting: {python}")
        command = self._installed_command()
        script = Path(cs.__file__).resolve().as_posix()
        self.assertEqual(command, f"{python} '{script}' --hook-task-created; exit 0")

    def test_interpreter_with_a_space_falls_back_to_the_bash_form(self):
        with mock.patch.object(cs.sys, "executable", r"C:\Program Files\Py\python.exe"):
            command = cs._blocking_hook_command("--hook-task-created", '"PY"', "SCRIPT")
        self.assertEqual(command, '"PY" "SCRIPT" --hook-task-created; exit 0')

    def test_installed_command_records_the_event_in_every_hook_shell(self):
        shells = self._hook_shells()
        if not shells:
            self.skipTest("no hook shell available")
        command = self._installed_command()
        for name, argv in shells.items():
            with self.subTest(shell=name), tempfile.TemporaryDirectory() as state:
                r = self._run_in(argv, command, state)
                self.assertEqual((r.returncode, r.stdout), (0, b""), (name, r.stderr[-300:]))
                marker = Path(state) / "claude-status" / "teams" / "shell-sess" / "tasks" / "1.open"
                self.assertTrue(marker.exists(), (name, r.stderr[-300:]))

    def test_installed_command_exits_zero_when_the_script_is_missing(self):
        """Python exits 2 by itself when the script path is gone — a blocking
        exit for these events. The INSTALLED command must still exit 0."""
        shells = self._hook_shells()
        if not shells:
            self.skipTest("no hook shell available")
        command = self._installed_command()
        self.assertEqual(command.count("claude_status.py"), 1, command)
        missing = command.replace("claude_status.py", "pulse-gone-dir/claude_status.py")
        for name, argv in shells.items():
            with self.subTest(shell=name), tempfile.TemporaryDirectory() as state:
                r = self._run_in(argv, missing, state)
                self.assertEqual((r.returncode, r.stdout), (0, b""), (name, r.stderr[-300:]))
        # The hazard is real: without the guard a POSIX shell passes Python's
        # exit 2 straight through. (PowerShell's -Command reports 1 instead.)
        posix = shells.get("git-bash") or shells.get("sh")
        if posix:
            with tempfile.TemporaryDirectory() as state:
                bare = self._run_in(posix, missing[:-len("; exit 0")], state)
            self.assertEqual(bare.returncode, 2, bare.stderr[-300:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
