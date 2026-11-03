"""Damaged or shared state files must never blank the bar or lose other windows' data.

Slice S3 of the PR-2 sweep (findings F11, F14, F15, F16, F29, F31).

* F11 - cache.json, history, context history, animation state, heatmap and stats
  were rewritten in place, so another window (or Claude Code cancelling a
  repaint) could see an empty file, treat it as "no data" and write that back.
  They now go through ``_atomic_json_write`` and never replace a damaged-looking
  history with a one-sample file while another window may still be writing it.
* F14 - a value the renderer cannot draw inside cache.json (text or NaN where a
  number belongs, a window that is a string, extra_usage numbers as text, a
  reset date past year 3000 on Windows) blanked the bar for good. The cache is
  cleaned where it is read, and the render in main() has a guard that falls back
  to a safe line.
* F15 - a hook_state / history / context_history file of the wrong shape, or not
  UTF-8, raised on every repaint and never healed.
* F16 - ``"rateLimitTier": null`` or ``"claudeAiOauth": null`` in the
  credentials file raised on every repaint.
* F29 - two windows of one account share context_history.json, so a steady 75%
  window showed "Context fast" because the other window's lower samples sat in
  the same series. Samples are now per session.
* F31 - a config.json saved as UTF-16 or ANSI blanked the bar, and one with a
  UTF-8 BOM was silently ignored.

Nothing here touches the network, the real home or the real settings: every
file lives in a temp dir and the writers fail the test if they escape it.

Run: ``python -m unittest tests.test_pr2_damaged_state``
"""
import io
import json
import math
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")

_REAL_GET_CREDENTIALS = cs.get_credentials   # the harness replaces it; one test wants the real one

GOOD_USAGE = {
    "five_hour": {"utilization": 42.0, "resets_at": "2099-01-01T05:00:00+00:00"},
    "seven_day": {"utilization": 23.0, "resets_at": "2099-01-05T00:00:00+00:00"},
}


class _FakeClock:
    def __init__(self, now=1_900_000_000.0):
        self.now = now

    def time(self):
        return self.now


class _Sandboxed(unittest.TestCase):
    """A sandbox home/state dir where any write outside it fails the test."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        for var in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            os.environ.pop(var, None)   # ProviderSandbox restores the environment afterwards
        self.sb.claude_dir(".claude", creds=False)
        root = self.sb.root.resolve()

        def confined(real):
            def wrapper(filepath, *args, **kwargs):
                target = Path(filepath).resolve()
                if target != root and root not in target.parents:
                    raise AssertionError("test wrote outside its sandbox: %s" % target)
                return real(filepath, *args, **kwargs)
            return wrapper

        def retrying(real):
            """Windows refuses os.replace now and then (WinError 5: a scanner or
            indexer holds the file; about one write in a hundred on a dev
            machine). Retrying that is _atomic_json_write's job, not what these
            tests check, so the harness retries it and the tests stay
            deterministic. A replace that never works still raises."""
            def wrapper(filepath, *args, **kwargs):
                for attempt in range(6):
                    try:
                        return real(filepath, *args, **kwargs)
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(0.02)
            return wrapper

        for name, wrap in (("_atomic_json_write", lambda f: confined(retrying(f))),
                           ("_secure_open_write", confined)):
            p = mock.patch.object(cs, name, wrap(getattr(cs, name)))
            p.start()
            self.addCleanup(p.stop)

    # --- helpers -----------------------------------------------------------
    @property
    def state(self):
        return Path(self.sb.state)

    def _write(self, path, data):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            Path(path).write_bytes(data)
        else:
            Path(path).write_text(json.dumps(data), encoding="utf-8")

    def _age(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def _seen_during_write(self, path, write):
        """Contents of *path* at the instant the new JSON is being dumped.

        An in-place writer has already truncated the live file by then; an
        atomic writer is still dumping into a temp sibling.
        """
        seen = []
        real_dump = json.dump

        def spy(obj, fp, *args, **kwargs):
            try:
                seen.append(Path(path).read_bytes())
            except OSError:
                seen.append(None)
            return real_dump(obj, fp, *args, **kwargs)

        with mock.patch.object(cs.json, "dump", spy):
            write()
        self.assertTrue(seen, "nothing was dumped")
        return seen


# ---------------------------------------------------------------------------
# F11 - shared state files are rewritten atomically
# ---------------------------------------------------------------------------

class SharedStateIsWrittenAtomicallyTest(_Sandboxed):
    def _assert_intact_while_writing(self, path, original, write):
        self._write(path, original)
        before = Path(path).read_bytes()
        for seen in self._seen_during_write(path, write):
            self.assertEqual(seen, before,
                             "the live %s was empty or partial while it was being "
                             "rewritten; another window reading now sees no data"
                             % Path(path).name)

    def test_write_cache(self):
        path = self.state / "cache.json"
        self._assert_intact_while_writing(
            path, {"timestamp": time.time(), "line": "old", "usage": GOOD_USAGE,
                   "rate_limited_until": time.time() + 600, "rate_limit_fails": 3},
            lambda: cs.write_cache(path, "new", GOOD_USAGE, "Max 20x"))

    def test_append_history(self):
        path = cs._get_history_path()
        self._assert_intact_while_writing(
            path, [{"t": time.time() - 5, "s": 10, "w": 20}] * 50,
            lambda: cs._append_history(GOOD_USAGE))

    def test_append_context_history(self):
        path = cs._get_context_history_path()
        self._assert_intact_while_writing(
            path, [{"t": time.time() - 5, "c": 10.0, "s": "a"}],
            lambda: cs._append_context_history(12.0, session_id="a"))

    def test_save_anim_state(self):
        path = cs._get_anim_state_path()
        self._assert_intact_while_writing(
            path, {"prev_pct_session": 10}, lambda: cs._save_anim_state({"prev_pct_session": 20}))

    def test_update_heatmap(self):
        path = cs._get_heatmap_path()
        self._assert_intact_while_writing(
            path, {"hours": {"2099-01-01T00": {"session_pct": 1, "weekly_pct": 1, "samples": 1}}},
            lambda: cs._update_heatmap(GOOD_USAGE))

    def test_save_stats(self):
        path = cs._get_stats_path()
        self._assert_intact_while_writing(
            path, {"total_sessions": 4}, lambda: cs._save_stats({"total_sessions": 5}))

    def test_a_failed_replace_skips_the_write_and_keeps_the_old_file(self):
        """Windows refuses os.replace while a reader holds the target open. That
        is 'skip this write', not an error, and not a damaged file."""
        cases = {
            "cache.json": lambda: cs.write_cache(self.state / "cache.json", "n", GOOD_USAGE),
            cs._get_history_path().name: lambda: cs._append_history(GOOD_USAGE),
            cs._get_context_history_path().name: lambda: cs._append_context_history(5.0),
            cs._get_anim_state_path().name: lambda: cs._save_anim_state({"a": 1}),
            cs._get_heatmap_path().name: lambda: cs._update_heatmap(GOOD_USAGE),
            cs._get_stats_path().name: lambda: cs._save_stats({"a": 1}),
        }
        valid = {
            "cache.json": {"timestamp": time.time(), "line": "old"},
            cs._get_history_path().name: [],
            cs._get_context_history_path().name: [],
            cs._get_anim_state_path().name: {"a": 0},
            cs._get_heatmap_path().name: {"hours": {}},
            cs._get_stats_path().name: {"a": 0},
        }
        for name, write in cases.items():
            with self.subTest(file=name):
                path = self.state / name
                self._write(path, valid[name])
                before = path.read_bytes()
                with mock.patch.object(cs.os, "replace", side_effect=PermissionError("in use")):
                    write()   # must not raise
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual([p.name for p in self.state.glob("*.tmp")], [],
                                 "a failed write left its temp file behind")

    def test_stale_temp_files_are_swept_but_fresh_and_foreign_ones_are_not(self):
        old_tmp = self.state / "history.json.4242-0a1b2c3d.tmp"
        fresh_tmp = self.state / "cache.json.4343-0a1b2c3d.tmp"
        foreign = self.state / "notes.tmp"
        for p in (old_tmp, fresh_tmp, foreign):
            p.write_text("x", encoding="utf-8")
        self._age(old_tmp, 3600)
        self._age(foreign, 3600)
        cs._sweep_stale_tmp_files(self.state)
        self.assertFalse(old_tmp.exists(), "an hour-old orphan from a killed write is removed")
        self.assertTrue(fresh_tmp.exists(), "a recent temp file may belong to a live writer")
        self.assertTrue(foreign.exists(), "only this script's own temp names are touched")

    def test_write_cache_occasionally_sweeps_orphans(self):
        orphan = self.state / "stdin_ctx.json.38228-1ead15fc.tmp"
        orphan.write_text("x", encoding="utf-8")
        self._age(orphan, 3600)
        with mock.patch.object(cs.random, "random", return_value=0.0):
            cs.write_cache(self.state / "cache.json", "n", GOOD_USAGE)
        self.assertFalse(orphan.exists())

    def test_history_damaged_by_another_window_is_not_replaced_by_one_sample(self):
        """An empty or half-written history may be an old-version window mid
        write. Starting again from [] erased up to 24 h of samples."""
        path = cs._get_history_path()
        for label, damaged in (("empty", b""), ("truncated", b'[{"t": 1.5, "s": 1'),
                               ("not utf-8", b"\xff\xfe\x00junk")):
            with self.subTest(damage=label):
                self._write(path, damaged)
                cs._append_history(GOOD_USAGE)
                self.assertEqual(path.read_bytes(), damaged, "a fresh damaged file is left alone")

    def test_history_that_stays_damaged_heals(self):
        path = cs._get_history_path()
        self._write(path, b'[{"t": 1.5, "s": 1')
        self._age(path, 3600)
        cs._append_history(GOOD_USAGE)
        samples = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["s"], 42.0)

    def test_context_history_damaged_by_another_window_is_not_replaced(self):
        path = cs._get_context_history_path()
        self._write(path, b"")
        cs._append_context_history(50.0, session_id="a")
        self.assertEqual(path.read_bytes(), b"")
        self._age(path, 3600)
        cs._append_context_history(50.0, session_id="a")
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)


    # --- a reader that arrives while a writer swaps the file in ---------------
    @staticmethod
    def _refuse_open(name, times):
        """open() that raises PermissionError the first *times* a file called
        *name* is opened: what Windows does while os.replace swaps it in."""
        real_open = open
        left = [times]

        def opener(file, *args, **kwargs):
            if Path(str(file)).name == name and left[0] > 0:
                left[0] -= 1
                raise PermissionError(13, "Access is denied")
            return real_open(file, *args, **kwargs)
        return opener

    def test_a_reader_rides_out_a_replace_in_progress(self):
        path = self.state / "cache.json"
        self._write(path, {"timestamp": time.time(), "line": "x", "usage": GOOD_USAGE,
                           "rate_limited_until": time.time() + 600, "rate_limit_fails": 4})
        for name, reader in (("read_cache", lambda: cs.read_cache(path, 60)),
                             ("_read_stale_cache", lambda: cs._read_stale_cache(path))):
            with self.subTest(reader=name):
                with mock.patch("builtins.open", self._refuse_open("cache.json", 2)):
                    cached = reader()
                self.assertIsNotNone(cached, "a refusal that passes was read as 'no data'")
                self.assertEqual(cached["rate_limit_fails"], 4)

    def test_a_refusal_that_never_passes_is_a_miss_not_a_raise(self):
        path = self.state / "cache.json"
        self._write(path, {"timestamp": time.time(), "line": "x"})
        with mock.patch("builtins.open", self._refuse_open("cache.json", 99)):
            self.assertIsNone(cs.read_cache(path, 60))
            self.assertIsNone(cs._read_stale_cache(path))

    def test_history_keeps_its_samples_through_a_transient_refusal(self):
        path = cs._get_history_path()
        now = time.time()
        self._write(path, [{"t": now - 100 + i, "s": 1, "w": 1} for i in range(50)])
        with mock.patch("builtins.open", self._refuse_open("history.json", 2)):
            cs._append_history(GOOD_USAGE)
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 51)


# ---------------------------------------------------------------------------
# F14 - unusable values in cache.json
# ---------------------------------------------------------------------------

BAD_USAGES = (
    ("utilization text", {"five_hour": {"utilization": "abc", "resets_at": None}}),
    ("utilization numeric text", {"five_hour": {"utilization": "37.0"}}),
    ("utilization list", {"five_hour": {"utilization": [1]}}),
    ("utilization NaN", {"five_hour": {"utilization": float("nan")}}),
    ("utilization Infinity", {"five_hour": {"utilization": float("inf")}}),
    ("utilization int too big for a float", {"five_hour": {"utilization": 10 ** 400}}),
    ("utilization -Infinity", {"seven_day": {"utilization": float("-inf")}}),
    ("window is text", {"five_hour": "text"}),
    ("window is a list", {"seven_day": [1, 2]}),
    ("model window text", {"seven_day_fable": {"utilization": "x"}}),
    ("extra_usage is text", {"extra_usage": "on"}),
    ("monthly_limit text", {"extra_usage": {"is_enabled": True, "monthly_limit": "5000",
                                            "used_credits": 120, "utilization": 3}}),
    ("used_credits text", {"extra_usage": {"is_enabled": True, "monthly_limit": 5000,
                                           "used_credits": "120", "utilization": 3}}),
    ("extra utilization NaN", {"extra_usage": {"is_enabled": True, "monthly_limit": 5000,
                                               "used_credits": 120,
                                               "utilization": float("nan")}}),
)


def _theme_config(**show):
    config = dict(cs.load_config())
    config["show"] = dict(config["show"], **show)
    config["providers"] = []
    return config


class DamagedUsageValuesTest(_Sandboxed):
    def _cache_with(self, usage, **extra):
        data = {"timestamp": time.time(), "line": "", "usage": usage, "plan": "Max 20x"}
        data.update(extra)
        path = self.state / "cache.json"
        self._write(path, data)
        return path

    def test_every_unrenderable_value_is_cleaned_where_the_cache_is_read(self):
        """The cleaned usage renders: nothing the renderer cannot draw gets past
        read_cache or _read_stale_cache."""
        for label, usage in BAD_USAGES:
            with self.subTest(value=label):
                path = self._cache_with(usage)
                for cached in (cs.read_cache(path, 60), cs._read_stale_cache(path)):
                    self.assertIsNotNone(cached)
                    cs.build_status_line(cached.get("usage", {}), cached.get("plan", ""),
                                         _theme_config(extra=True, opus=True, sonnet=True,
                                                       fable=True), None, cache_age=0)

    def test_clean_values_pass_through_unchanged(self):
        usage = dict(GOOD_USAGE)
        usage["seven_day_fable"] = {"utilization": 37, "resets_at": None}
        usage["seven_day_opus"] = None   # the API sends null for windows the plan lacks
        usage["extra_usage"] = {"is_enabled": True, "monthly_limit": 5000,
                                "used_credits": 1250.5, "utilization": 25.0}
        path = self._cache_with(usage)
        self.assertEqual(cs.read_cache(path, 60)["usage"], usage)
        self.assertEqual(cs._read_stale_cache(path)["usage"], usage)

    def test_a_bad_window_is_dropped_and_its_neighbours_kept(self):
        usage = {"five_hour": {"utilization": "abc"},
                 "seven_day": {"utilization": 23.0, "resets_at": "2099-01-05T00:00:00+00:00"}}
        cached = cs.read_cache(self._cache_with(usage), 60)
        self.assertNotIn("five_hour", cached["usage"])
        self.assertEqual(cached["usage"]["seven_day"]["utilization"], 23.0)

    def test_usage_that_is_not_a_dict_falls_back_to_the_cached_line(self):
        path = self._cache_with(["not", "a", "dict"], line="Rate limited — retrying in 5 min")
        cached = cs.read_cache(path, 60)
        self.assertNotIn("usage", cached)
        self.assertEqual(cs._read_stale_cache(path)["line"], "Rate limited — retrying in 5 min")

    def test_far_future_reset_dates_do_not_raise(self):
        """On Windows, astimezone() of year 9999 raises OSError, which the
        formatter's except clause did not cover."""
        for stamp in ("9999-12-31T23:59:59+00:00", "3001-01-01T00:00:00+00:00",
                      "0001-01-01T00:00:00+00:00"):
            for fmt in cs.WEEKLY_TIMER_FORMATS:
                with self.subTest(stamp=stamp, fmt=fmt):
                    cs.format_weekly_reset(stamp, fmt=fmt)   # must not raise

    def test_unusable_reset_values_do_not_raise(self):
        for bad in ([1], {"a": 1}, 12345, float("nan"), "\x1b[31mred", "garbage"):
            with self.subTest(value=repr(bad)):
                cs.format_weekly_reset(bad)

    def test_guard_cleans_and_retries_before_giving_up(self):
        bad = {"five_hour": {"utilization": "abc"},
               "seven_day": {"utilization": 23.0, "resets_at": "2099-01-05T00:00:00+00:00"}}
        line = cs._guarded_status_line(bad, "Max 20x", _theme_config(), None, 0)
        self.assertIn("Weekly", ANSI.sub("", line))

    def test_guard_falls_back_to_the_cached_line_then_to_a_message(self):
        with mock.patch.object(cs, "build_status_line", side_effect=TypeError("boom")):
            self.assertEqual(
                cs._guarded_status_line(GOOD_USAGE, "", {}, None, 0, fallback="cached line"),
                "cached line")
            blank = cs._guarded_status_line(GOOD_USAGE, "", {}, None, 0, fallback="")
            self.assertTrue(blank.strip(), "the fallback must never be a blank bar")
            self.assertTrue(cs._guarded_status_line(GOOD_USAGE, "", {}, None, 0,
                                                    fallback=None).strip())


class _MainHarness(_Sandboxed):
    """Drives main() as Claude Code does: one process per repaint."""

    def setUp(self):
        super().setUp()
        self.network = []

        def no_network(*args, **kwargs):
            self.network.append(args[:1])
            raise OSError("network disabled in tests")

        patches = [
            mock.patch.dict(cs._own_stamps, {}, clear=True),
            mock.patch.object(cs, "get_credentials", lambda: ("fake-token", "Max 20x")),
            mock.patch.object(cs, "fetch_usage", lambda *a, **k: dict(GOOD_USAGE)),
            mock.patch.object(cs, "check_for_update", lambda *a, **k: False),
            mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: False),
            mock.patch.object(cs, "sync_status_line_refresh", lambda *a, **k: None),
            mock.patch.object(cs, "_cleanup_hooks", lambda: None),
            mock.patch.object(cs.socket, "getaddrinfo", no_network),
            mock.patch.object(cs.socket, "create_connection", no_network),
            mock.patch.object(cs.urllib.request, "urlopen", no_network),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.assertEqual(self.network, [], "a test reached for the real network")

    @staticmethod
    def _stdin(session_id="s1", rate_limits=True, context_pct=None):
        now = time.time()
        payload = {"session_id": session_id, "model": {"display_name": "Opus 4.8"}}
        if rate_limits:
            payload["rate_limits"] = {
                "five_hour": {"used_percentage": 15, "resets_at": now + 10800},
                "seven_day": {"used_percentage": 66, "resets_at": now + 604800}}
        if context_pct is not None:
            payload["context_window"] = {"used_percentage": context_pct}
        return json.dumps(payload)

    def _repaint(self, stdin_text):
        """One repaint; main() raising is a blank bar, so it fails the test."""
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        cs._own_stamps.clear()
        with mock.patch.object(cs.sys, "argv", ["claude_status.py"]), \
                mock.patch.object(cs.sys, "stdin", io.StringIO(stdin_text)), \
                mock.patch.object(cs.sys, "stdout", _Out()):
            try:
                cs.main()
            except Exception as exc:  # noqa: BLE001 - this IS the blank bar
                self.fail("main() raised %s: %s -- Claude Code shows a blank bar"
                          % (type(exc).__name__, str(exc)[:100]))
        return ANSI.sub("", buf.getvalue().decode("utf-8", "replace")).strip()

    def _plant_cache(self, usage, **extra):
        data = {"timestamp": time.time(), "line": "", "usage": usage, "plan": "Max 20x"}
        data.update(extra)
        self._write(cs.get_cache_path(), data)

    def _forget_stdin(self):
        """Claude Code persists the last stdin; a later repaint without rate
        limits would otherwise still find them."""
        try:
            (cs.get_account_state_dir() / "stdin_ctx.json").unlink()
        except OSError:
            pass


class DamagedCacheOnTheRenderPathTest(_MainHarness):
    def test_stdin_path_draws_on_every_repaint_whatever_the_cache_holds(self):
        for label, usage in BAD_USAGES:
            with self.subTest(value=label):
                self._plant_cache(usage)
                for n in range(2):
                    line = self._repaint(self._stdin())
                    self.assertIn("Session", line, "repaint %d printed %r" % (n + 1, line))

    def test_stdin_path_heals_the_cache(self):
        self._plant_cache({"extra_usage": {"is_enabled": True, "monthly_limit": "5000"}})
        self._repaint(self._stdin())
        usage = json.loads(cs.get_cache_path().read_text(encoding="utf-8")).get("usage", {})
        self.assertNotEqual(usage.get("extra_usage", {}).get("monthly_limit"), "5000")

    def test_cached_branch_draws_through_a_whole_backoff(self):
        """No stdin rate limits and an active 429 backoff: read_cache serves
        the file whatever its age, so a bad value blanked the bar until the
        backoff ended."""
        for label, usage in BAD_USAGES:
            with self.subTest(value=label):
                self._plant_cache(usage, rate_limited_until=time.time() + 3600,
                                  rate_limit_fails=2)
                line = self._repaint(self._stdin(rate_limits=False))
                self.assertTrue(line, "blank bar with %s in the cache" % label)

    def test_a_render_that_raises_still_prints_something(self):
        for stdin_text, plant in (
                (self._stdin(), lambda: None),
                (self._stdin(rate_limits=False),
                 lambda: self._plant_cache(GOOD_USAGE, rate_limited_until=time.time() + 3600))):
            with self.subTest(stdin=bool(json.loads(stdin_text).get("rate_limits"))):
                self._forget_stdin()
                plant()
                with mock.patch.object(cs, "build_status_line", side_effect=ValueError("boom")):
                    line = self._repaint(stdin_text)
                self.assertTrue(line, "blank bar when the render raised")

    def test_cached_branch_never_prints_an_empty_line(self):
        self._forget_stdin()
        self._write(cs.get_cache_path(), {"timestamp": time.time(), "line": 42})
        self.assertTrue(self._repaint(self._stdin(rate_limits=False)))


# ---------------------------------------------------------------------------
# F15 - damaged hook_state / history / context_history
# ---------------------------------------------------------------------------

BAD_HOOK_STATES = (
    ("last_refresh text", {"last_refresh": "1730000000x"}),
    ("last_refresh null", {"last_refresh": None}),
    ("last_refresh list", {"last_refresh": [1]}),
    ("last_refresh NaN", {"last_refresh": float("nan")}),
    ("list", ["x"]),
    ("number", 42),
    ("text", "state"),
)


class DamagedHookStateTest(_Sandboxed):
    def test_freshness_check_never_raises_and_calls_unusable_state_stale(self):
        for label, state in BAD_HOOK_STATES:
            with self.subTest(state=label):
                self.assertFalse(cs._is_hook_state_fresh(state))

    def test_freshness_still_works_for_a_good_state(self):
        self.assertTrue(cs._is_hook_state_fresh({"last_refresh": time.time() - 5}))
        self.assertFalse(cs._is_hook_state_fresh({"last_refresh": time.time() - 3600}))
        self.assertFalse(cs._is_hook_state_fresh({}))
        self.assertFalse(cs._is_hook_state_fresh(None))

    def test_a_far_future_stamp_is_not_fresh_forever(self):
        self.assertFalse(cs._is_hook_state_fresh({"last_refresh": 1e300}))

    def test_heartbeat_and_last_tool_survive_odd_field_values(self):
        config = _theme_config(heartbeat=True, last_tool=True, branch=False)
        for label, extra in (("session_start text", {"session_start": "yesterday"}),
                             ("session_start list", {"session_start": [1]}),
                             ("session_start NaN", {"session_start": float("nan")}),
                             ("tool_count text", {"tool_count": "many"}),
                             ("tool_count list", {"tool_count": [1, 2]}),
                             ("last_tool number", {"last_tool": 7}),
                             ("last_tool list", {"last_tool": ["Bash"]})):
            with self.subTest(field=label):
                state = {"last_refresh": time.time(), "tool_count": 3,
                         "session_start": time.time() - 90, "last_tool": "Bash"}
                state.update(extra)
                with mock.patch.object(cs, "_read_hook_state", return_value=state):
                    line = ANSI.sub("", cs.build_status_line(GOOD_USAGE, "", config, None))
                self.assertIn("tools", line)

    def test_heartbeat_still_reports_a_good_state(self):
        config = _theme_config(heartbeat=True, last_tool=True, branch=False)
        state = {"last_refresh": time.time(), "tool_count": 3,
                 "session_start": time.time() - 90, "last_tool": "Bash"}
        with mock.patch.object(cs, "_read_hook_state", return_value=state):
            line = ANSI.sub("", cs.build_status_line(GOOD_USAGE, "", config, None))
        self.assertIn("3 tools 1m", line)
        self.assertIn("Last: Bash", line)


BAD_SAMPLE_FILES = (
    ("dict", {}),
    ("list of numbers", [1, 2]),
    ("list of lists", [[1, 2]]),
    ("sample time text", [{"t": "x", "s": 1, "w": 1, "c": 1}]),
    ("sample value text", [{"t": time.time(), "s": "high", "w": 1, "c": "high"}]),
    ("sample value null", [{"t": time.time(), "s": None, "w": None, "c": None}]),
    ("text", "history"),
    ("number", 7),
)


class DamagedHistoryFilesTest(_Sandboxed):
    def test_history_of_the_wrong_shape_is_healed_not_fatal(self):
        path = cs._get_history_path()
        for label, damaged in BAD_SAMPLE_FILES:
            with self.subTest(content=label):
                self._write(path, damaged)
                cs._append_history(GOOD_USAGE)    # must not raise
                samples = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(samples, list)
                self.assertEqual(samples[-1]["s"], 42.0)
                self.assertTrue(all(isinstance(s, dict) and isinstance(s["t"], float)
                                    for s in samples))

    def test_good_history_samples_are_kept(self):
        path = cs._get_history_path()
        now = time.time()
        self._write(path, [{"t": now - 60, "s": 10, "w": 20}, {"t": now - 30, "s": 11, "w": 20}])
        cs._append_history(GOOD_USAGE)
        samples = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([s["s"] for s in samples], [10, 11, 42.0])

    def test_history_survives_a_usage_dict_with_odd_windows(self):
        path = cs._get_history_path()
        for label, usage in BAD_USAGES:
            with self.subTest(value=label):
                self._write(path, [])
                cs._append_history(usage)
                self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))), 1)

    def test_context_history_of_the_wrong_shape_is_healed_not_fatal(self):
        path = cs._get_context_history_path()
        for label, damaged in BAD_SAMPLE_FILES:
            with self.subTest(content=label):
                self._write(path, damaged)
                cs._append_context_history(55.0, session_id="a")
                samples = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(samples[-1]["c"], 55.0)
                self.assertEqual(samples[-1]["s"], "a")

    def test_context_velocity_ignores_a_damaged_file(self):
        path = cs._get_context_history_path()
        for label, damaged in BAD_SAMPLE_FILES + (("not utf-8", b"\xff\xfe\x00junk"),
                                                  ("empty", b"")):
            with self.subTest(content=label):
                self._write(path, damaged)
                self.assertIsNone(cs._compute_context_velocity("a"))

    def test_context_history_ignores_a_non_finite_percentage(self):
        path = cs._get_context_history_path()
        self._write(path, [])
        for bad in (float("nan"), float("inf"), "high", None, True):
            cs._append_context_history(bad, session_id="a")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    def test_heatmap_damage_is_healed_not_fatal(self):
        path = cs._get_heatmap_path()
        for label, damaged in (("list", [1, 2]), ("hours text", {"hours": "x"}),
                               ("hours list", {"hours": [1]}),
                               ("entry text", {"hours": {"2099-01-01T00": "busy"}}),
                               ("entry values text",
                                {"hours": {"2099-01-01T00": {"session_pct": "x", "samples": "y"}}})):
            with self.subTest(content=label):
                self._write(path, damaged)
                cs._update_heatmap(GOOD_USAGE)
                heat = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(all(isinstance(v, dict)
                                    and all(isinstance(v[k], (int, float))
                                            for k in ("session_pct", "weekly_pct", "samples"))
                                    for v in heat["hours"].values()))

    def test_heatmap_with_a_fresh_undecodable_file_is_left_alone(self):
        path = cs._get_heatmap_path()
        self._write(path, b"\xff\xfejunk")
        cs._update_heatmap(GOOD_USAGE)
        self.assertEqual(path.read_bytes(), b"\xff\xfejunk")
        self._age(path, 3600)
        cs._update_heatmap(GOOD_USAGE)
        self.assertIn("hours", json.loads(path.read_text(encoding="utf-8")))

    def test_heatmap_keeps_good_entries(self):
        path = cs._get_heatmap_path()
        key = time.strftime("%Y-%m-%dT%H", time.gmtime())
        self._write(path, {"hours": {key: {"session_pct": 90, "weekly_pct": 5, "samples": 3}}})
        cs._update_heatmap(GOOD_USAGE)
        entry = json.loads(path.read_text(encoding="utf-8"))["hours"][key]
        self.assertEqual((entry["session_pct"], entry["samples"]), (90, 4))


class DamagedStateOnTheRenderPathTest(_MainHarness):
    """The render itself may choke on a file this slice does not read (the
    history readers live elsewhere); the bar must still print and the file
    must heal."""

    def test_damaged_history_and_context_history_never_blank_the_bar(self):
        for label, damaged in BAD_SAMPLE_FILES + (("not utf-8", b"\xff\xfe\x00junk"),):
            with self.subTest(content=label):
                self._write(cs._get_history_path(), damaged)
                self._write(cs._get_context_history_path(), damaged)
                line = self._repaint(self._stdin(context_pct=40.0))
                self.assertTrue(line, "blank bar with %s history files" % label)

    def test_damaged_history_heals_within_one_repaint(self):
        """The file is rewritten by the repaint that choked on it, so the bar is
        only degraded once, not until the user finds and deletes the file."""
        for label, damaged, age in (("numbers", [1, 2], 0), ("dict", {}, 0),
                                    ("sample time text", [{"t": "x", "s": 1}], 0),
                                    ("not utf-8, untouched for a while", bytes([0xFF, 0xFE]) + b"junk", 3600)):
            with self.subTest(content=label):
                self._write(cs._get_history_path(), damaged)
                self._write(cs._get_context_history_path(), damaged)
                if age:
                    self._age(cs._get_history_path(), age)
                    self._age(cs._get_context_history_path(), age)
                self.assertTrue(self._repaint(self._stdin(context_pct=40.0)))
                self.assertIn("Session", self._repaint(self._stdin(context_pct=40.0)))
                for path in (cs._get_history_path(), cs._get_context_history_path()):
                    self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), list)

    def test_damaged_hook_state_never_blanks_the_bar(self):
        path = self.state / "hook_state.json"
        for label, damaged in (("null stamp", {"git_branch_ts": None}),
                               ("list", ["x"]),
                               ("text stamp", {"last_refresh": "1730000000"}),
                               ("not utf-8", b"\xe9\xe8bad")):
            with self.subTest(content=label):
                self._write(path, damaged)
                line = self._repaint(self._stdin())
                self.assertTrue(line, "blank bar with a %s hook_state.json" % label)


# ---------------------------------------------------------------------------
# F16 - credentials with explicit nulls
# ---------------------------------------------------------------------------

class NullCredentialFieldsTest(_Sandboxed):
    def _creds(self, content):
        path = Path(os.environ["USERPROFILE"]) / ".claude" / ".credentials.json"
        self._write(path, content)
        return path

    def test_extract_with_a_null_or_odd_tier_gives_no_plan_name(self):
        for tier in (None, "", 5, ["x"], {"a": 1}, True):
            with self.subTest(tier=tier):
                token, plan = cs._extract_credentials(
                    {"claudeAiOauth": {"accessToken": "tok", "rateLimitTier": tier}})
                self.assertEqual((token, plan), ("tok", ""))

    def test_extract_with_a_missing_tier_still_works(self):
        self.assertEqual(cs._extract_credentials({"claudeAiOauth": {"accessToken": "tok"}}),
                         ("tok", ""))

    def test_extract_still_names_real_plans(self):
        self.assertEqual(cs._extract_credentials(
            {"claudeAiOauth": {"accessToken": "t", "rateLimitTier": "default_claude_max_20x"}}),
            ("t", "Max 20x"))
        self.assertEqual(cs._extract_credentials(
            {"claudeAiOauth": {"accessToken": "t", "rateLimitTier": "default_claude_team_5x"}}),
            ("t", "Team 5X"))

    def test_extract_with_an_odd_oauth_section_gives_nothing(self):
        for data in ({"claudeAiOauth": None}, {"claudeAiOauth": "x"}, {"claudeAiOauth": [1]},
                     {"claudeAiOauth": {"accessToken": None}}, {"claudeAiOauth": {"accessToken": 5}},
                     {}, None, [], "x"):
            with self.subTest(data=repr(data)):
                self.assertEqual(cs._extract_credentials(data), (None, None))

    def test_reading_an_unusable_credentials_file_gives_nothing(self):
        for label, content in (("null oauth", {"claudeAiOauth": None}),
                               ("oauth text", {"claudeAiOauth": "x"}),
                               ("top-level list", [1, 2]),
                               ("top-level null", None),
                               ("not utf-8", b"\xff\xfe{\x00junk"),
                               ("empty", b""),
                               ("truncated", b'{"claudeAiOauth": {"accessTok')):
            with self.subTest(content=label):
                self._creds(content)
                self.assertEqual(cs._read_credential_data(), (None, None))
                self.assertEqual(cs.get_credentials()[0], None)

    def test_reading_a_credentials_file_that_cannot_be_opened_gives_nothing(self):
        self._creds({"claudeAiOauth": {"accessToken": "tok"}})
        with mock.patch("builtins.open", side_effect=PermissionError("locked")):
            self.assertEqual(cs._read_credential_data(), (None, None))

    def test_a_good_credentials_file_still_reads(self):
        self._creds({"claudeAiOauth": {"accessToken": "tok", "rateLimitTier": "default_claude_ai"}})
        data, source = cs._read_credential_data()
        self.assertEqual(source, "file")
        self.assertEqual(cs.get_credentials(), ("tok", "Pro"))


class NullCredentialsOnTheRenderPathTest(_MainHarness):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cs, "get_credentials", _REAL_GET_CREDENTIALS)
        patch.start()
        self.addCleanup(patch.stop)

    def _creds(self, content):
        path = Path(os.environ["USERPROFILE"]) / ".claude" / ".credentials.json"
        self._write(path, content)

    def test_stdin_path_draws_with_a_null_tier_or_null_oauth(self):
        for label, content in (
                ("null tier", {"claudeAiOauth": {"accessToken": "tok", "rateLimitTier": None}}),
                ("null oauth", {"claudeAiOauth": None}),
                ("tier number", {"claudeAiOauth": {"accessToken": "tok", "rateLimitTier": 5}})):
            with self.subTest(creds=label):
                self._creds(content)
                for n in range(2):
                    self.assertIn("Session", self._repaint(self._stdin()),
                                  "repaint %d" % (n + 1))

    def test_api_fallback_does_not_blank_the_bar_with_null_oauth(self):
        self._forget_stdin()
        self._creds({"claudeAiOauth": None})
        line = self._repaint(self._stdin(rate_limits=False))
        self.assertTrue(line)


# ---------------------------------------------------------------------------
# F29 - context velocity is per session
# ---------------------------------------------------------------------------

class ContextVelocityIsPerSessionTest(_Sandboxed):
    def setUp(self):
        super().setUp()
        self.clock = _FakeClock()
        p = mock.patch.object(cs, "time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        self.theme = cs.get_theme_colours("default")
        p = mock.patch.object(cs, "_context_session_id", None)
        p.start()
        self.addCleanup(p.stop)

    def _sample(self, pct, session_id, advance=0):
        self.clock.now += advance
        cs._append_context_history(pct, session_id=session_id)

    def test_another_windows_lower_samples_do_not_make_a_steady_window_warn(self):
        """The finding's reproduction: window B at 10%, 11 s later window A at 75%."""
        self._sample(10.0, "B")
        self._sample(75.0, "A", advance=11)
        cs._set_context_session("A")
        label, suffix = cs._format_context_warning(75.0, self.theme)
        self.assertIsNone(label)
        self.assertFalse(suffix)
        self.assertIsNone(cs._compute_context_velocity("A"))

    def test_a_steady_window_does_not_warn_however_often_both_repaint(self):
        for _ in range(6):
            self._sample(10.0, "B", advance=12)
            self._sample(75.0, "A", advance=1)
        for sid in ("A", "B"):
            velocity = cs._compute_context_velocity(sid)
            self.assertTrue(velocity is None or abs(velocity) < 0.5, (sid, velocity))
        cs._set_context_session("A")
        self.assertEqual(cs._format_context_warning(75.0, self.theme), (None, None))
        cs._set_context_session("B")
        self.assertEqual(cs._format_context_warning(10.0, self.theme), (None, None))

    def test_a_window_that_really_is_filling_fast_still_warns(self):
        self._sample(10.0, "B")
        self._sample(50.0, "A", advance=1)
        self._sample(10.0, "B", advance=5)
        self._sample(80.0, "A", advance=20)
        velocity = cs._compute_context_velocity("A")
        self.assertGreater(velocity, cs.CONTEXT_PRESSURE_VELOCITY)
        cs._set_context_session("A")
        label, suffix = cs._format_context_warning(80.0, self.theme)
        self.assertIn("Context", label)
        self.assertIn("fast", suffix)

    def test_the_session_defaults_to_the_one_main_announced(self):
        cs._set_context_session("A")
        self._sample(50.0, None)
        self._sample(80.0, None, advance=30)
        history = json.loads(cs._get_context_history_path().read_text(encoding="utf-8"))
        self.assertEqual({s["s"] for s in history}, {"A"})
        self.assertGreater(cs._compute_context_velocity(), cs.CONTEXT_PRESSURE_VELOCITY)

    def test_samples_without_a_session_do_not_leak_into_a_named_one(self):
        """History written by an older version has no session on its samples."""
        self._write(cs._get_context_history_path(), [
            {"t": self.clock.now - 20, "c": 5.0}, {"t": self.clock.now - 1, "c": 5.0}])
        self._sample(75.0, "A", advance=5)
        self._sample(75.0, "A", advance=20)
        self.assertLess(cs._compute_context_velocity("A"), 0.5)

    def test_an_unnamed_session_keeps_the_old_single_series_behaviour(self):
        cs._set_context_session(None)
        self._sample(10.0, None)
        self._sample(80.0, None, advance=20)
        self.assertGreater(cs._compute_context_velocity(), cs.CONTEXT_PRESSURE_VELOCITY)

    def test_old_samples_are_pruned_and_the_file_stays_bounded(self):
        self._sample(10.0, "gone")
        self._sample(20.0, "A", advance=3700)
        history = json.loads(cs._get_context_history_path().read_text(encoding="utf-8"))
        self.assertEqual([s["s"] for s in history], ["A"])
        self._write(cs._get_context_history_path(),
                    [{"t": self.clock.now - 1000 + i, "c": 20.0, "s": "A"} for i in range(520)])
        self._sample(20.0, "A", advance=1)
        history = json.loads(cs._get_context_history_path().read_text(encoding="utf-8"))
        self.assertLessEqual(len(history), 500)


class ContextHistoryThroughMainTest(_MainHarness):
    def test_main_records_each_windows_session_id(self):
        self._repaint(self._stdin(session_id="win-a", context_pct=70.0))
        self._repaint(self._stdin(session_id="win-b", context_pct=10.0))
        self._repaint(self._stdin(session_id="win-a", context_pct=70.0))
        history = json.loads(cs._get_context_history_path().read_text(encoding="utf-8"))
        self.assertEqual([s["s"] for s in history], ["win-a", "win-b", "win-a"])


# ---------------------------------------------------------------------------
# F31 - config.json encodings
# ---------------------------------------------------------------------------

class ConfigFileEncodingTest(_Sandboxed):
    TEXT = json.dumps({"currency": "£", "line2_widgets": ["cost"], "theme": "ocean"},
                      ensure_ascii=False)

    def _config(self, raw):
        path = cs.get_config_path()
        path.write_bytes(raw)
        return cs.load_config()

    def test_utf8_utf8_with_bom_and_utf16_are_all_read(self):
        for label, raw in (("utf-8", self.TEXT.encode("utf-8")),
                           ("utf-8 with BOM", b"\xef\xbb\xbf" + self.TEXT.encode("utf-8")),
                           ("utf-16 with BOM", self.TEXT.encode("utf-16")),
                           ("utf-16-le no BOM", self.TEXT.encode("utf-16-le")),
                           ("utf-16-be no BOM", self.TEXT.encode("utf-16-be"))):
            with self.subTest(encoding=label):
                config = self._config(raw)
                self.assertEqual(config["currency"], "£")
                self.assertEqual(config["line2_widgets"], ["cost"])
                self.assertEqual(config["theme"], "ocean")

    def test_unreadable_files_give_defaults_not_an_exception(self):
        for label, raw in (("ANSI with a pound sign", self.TEXT.encode("cp1252")),
                           ("random bytes", bytes(range(128, 256))),
                           ("empty", b""),
                           ("truncated", b'{"theme": "oc'),
                           ("a list", b"[1, 2, 3]"),
                           ("null", b"null"),
                           ("a number", b"7")):
            with self.subTest(content=label):
                config = self._config(raw)
                self.assertIsInstance(config, dict)
                self.assertEqual(config["theme"], "default")
                self.assertIn("session", config["show"])

    def test_a_config_that_cannot_be_opened_gives_defaults(self):
        cs.get_config_path().write_text(self.TEXT, encoding="utf-8")
        real_open = open

        def refuse_config(path, *args, **kwargs):
            if Path(path).name == "config.json":
                raise PermissionError("locked")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", refuse_config):
            config = cs.load_config()
        self.assertEqual(config["theme"], "default")

    def test_a_missing_user_config_still_falls_back_to_the_repo_template(self):
        config = cs.load_config()
        self.assertEqual(config["layout"], "standard")
        self.assertIn("session", config["show"])


class UnreadableConfigOnTheRenderPathTest(_MainHarness):
    def test_the_bar_draws_for_every_encoding(self):
        text = json.dumps({"currency": "£", "line2_widgets": ["cost"]}, ensure_ascii=False)
        for label, raw in (("utf-16", text.encode("utf-16")),
                           ("cp1252", text.encode("cp1252")),
                           ("utf-8 BOM", b"\xef\xbb\xbf" + text.encode("utf-8"))):
            with self.subTest(encoding=label):
                cs.get_config_path().write_bytes(raw)
                self.assertIn("Session", self._repaint(self._stdin()))


if __name__ == "__main__":
    unittest.main()
