"""Regression tests for the cost widgets and the money/date formatting (PR 2, slice 4).

F07  The 7-day and all-time cost widgets must never scan transcripts on the
     render path. Claude Code cancels a status line script when the next update
     fires, so a scan that runs before printing (and is only saved when it
     finishes) freezes the bar for as long as the scan outlasts the refresh
     interval. The render path now draws the last saved figure, however old,
     and hands the scan to one detached background process.
F18  The scan counted every transcript line of a response (one per content
     block, all repeating the same message id and usage) and priced every
     cache write at the 5-minute rate, so costs read about twice the truth.
F30  Danish and Norwegian users were converted at the Swedish krona rate,
     DKK/NOK could not be chosen, and the hryvnia sign was never converted.
F39  The weekly reset date dropped the minutes in the 12-hour clock.

Run: ``python tests/test_pr2_costs.py``
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

_CACHE_VERSION = getattr(cs, "_COST_CACHE_VERSION", 2)


# ---------------------------------------------------------------------------
# Shared sandbox
# ---------------------------------------------------------------------------

class _Popen:
    """Records every background process the code under test tries to start."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return mock.Mock(pid=4242)


@contextmanager
def _sandbox():
    """State dir, Claude config dir and in-memory caches all inside a temp dir."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = root / "state"
        state.mkdir()
        cfg = root / "claude"
        (cfg / "projects" / "proj").mkdir(parents=True)
        with mock.patch.object(cs, "get_account_state_dir", lambda: state), \
                mock.patch.object(cs, "get_state_dir", lambda: state), \
                mock.patch.object(cs, "_claude_config_dir", lambda: cfg), \
                mock.patch.dict(cs._cumulative_cost_mem, {"ts": 0, "data": {}}), \
                mock.patch.dict(cs._weekly_cost_mem, {"ts": 0, "data": {}}), \
                mock.patch.dict(cs._exchange_rate_mem, {}, clear=True):
            yield state, cfg


def _write_cache(state, name, total, age, version=None):
    payload = {
        "timestamp": time.time() - age,
        "data": {"models": {}, "total_cost_usd": total, "total_tokens": 1,
                 "session_count": 1, "first_seen": None},
    }
    payload["v"] = _CACHE_VERSION if version is None else version
    if version == "old":
        del payload["v"]
    (state / name).write_text(json.dumps(payload), encoding="utf-8")


def _no_inline_scan(*args, **kwargs):
    raise AssertionError("a transcript scan ran on the render path")


# ---------------------------------------------------------------------------
# F07
# ---------------------------------------------------------------------------

class RenderPathNeverScansTest(unittest.TestCase):

    def _get(self, kind):
        return (cs._get_cached_cumulative_cost if kind == "cumulative"
                else cs._get_cached_weekly_cost)()

    def test_stale_figure_is_drawn_and_the_refresh_is_handed_off(self):
        for kind, name in (("cumulative", "cumulative_cost_cache.json"),
                           ("weekly", "weekly_cost_cache.json")):
            with self.subTest(kind=kind), _sandbox() as (state, _cfg):
                _write_cache(state, name, total=12.34, age=3600)
                popen = _Popen()
                with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                        mock.patch.object(cs.subprocess, "Popen", popen):
                    data = self._get(kind)
                self.assertAlmostEqual(data["total_cost_usd"], 12.34)
                self.assertEqual(len(popen.calls), 1, "one background refresh")

    def test_first_ever_render_does_not_wait_for_a_scan(self):
        for kind in ("cumulative", "weekly"):
            with self.subTest(kind=kind), _sandbox():
                popen = _Popen()
                with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                        mock.patch.object(cs.subprocess, "Popen", popen):
                    data = self._get(kind)
                self.assertEqual(data, {})
                self.assertEqual(len(popen.calls), 1)

    def test_fresh_figure_costs_nothing(self):
        with _sandbox() as (state, _cfg):
            _write_cache(state, "cumulative_cost_cache.json", total=7.5, age=10)
            popen = _Popen()
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", popen):
                data = cs._get_cached_cumulative_cost()
            self.assertAlmostEqual(data["total_cost_usd"], 7.5)
            self.assertEqual(popen.calls, [])

    def test_a_cut_short_refresh_is_not_restarted_by_every_repaint(self):
        """The claim is on disk before the scan starts, so a repaint that is
        killed (or a scan still running) is not followed by another scan on
        the next repaint: that was the loop that froze the bar."""
        with _sandbox() as (state, _cfg):
            popen = _Popen()
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", popen):
                for _ in range(5):
                    cs._get_cached_cumulative_cost()
                    cs._cumulative_cost_mem.update(ts=0, data={})  # a new process each time
                self.assertEqual(len(popen.calls), 1)
                # A claim that was never released (worker killed) lapses on its own.
                claim = state / "cost_scan_cumulative.lock"
                self.assertTrue(claim.exists())
                old = time.time() - 2 * cs._COST_SCAN_LOCK_TTL
                os.utime(claim, (old, old))
                cs._get_cached_cumulative_cost()
                self.assertEqual(len(popen.calls), 2)

    def test_each_kind_has_its_own_claim(self):
        with _sandbox():
            popen = _Popen()
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", popen):
                cs._get_cached_cumulative_cost()
                cs._get_cached_weekly_cost()
            self.assertEqual(len(popen.calls), 2)

    def test_cache_from_the_old_overcounting_scan_is_not_drawn(self):
        """A figure saved before the dedupe fix is about twice the truth:
        better a blank for the few seconds the refresh takes than that."""
        with _sandbox() as (state, _cfg):
            _write_cache(state, "cumulative_cost_cache.json", total=999.0, age=3600,
                         version="old")
            popen = _Popen()
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", popen):
                data = cs._get_cached_cumulative_cost()
            self.assertEqual(data, {})
            self.assertEqual(len(popen.calls), 1)

    def test_damaged_cache_files_do_not_raise(self):
        for junk in ("", "{", "[]", "null", '{"v": %d, "timestamp": "x", "data": 3}' % _CACHE_VERSION,
                     '{"v": %d, "timestamp": 1, "data": []}' % _CACHE_VERSION):
            with self.subTest(junk=junk), _sandbox() as (state, _cfg):
                (state / "weekly_cost_cache.json").write_text(junk, encoding="utf-8")
                with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                        mock.patch.object(cs.subprocess, "Popen", _Popen()):
                    self.assertEqual(cs._get_cached_weekly_cost(), {})

    def test_failure_to_start_the_worker_does_not_break_the_bar(self):
        with _sandbox():
            def boom(*a, **k):
                raise OSError("no process for you")
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", boom):
                self.assertEqual(cs._get_cached_cumulative_cost(), {})

    def test_claim_is_exclusive(self):
        with _sandbox():
            now = time.time()
            self.assertTrue(cs._claim_cost_scan("cumulative", now))
            self.assertFalse(cs._claim_cost_scan("cumulative", now))
            self.assertTrue(cs._claim_cost_scan("weekly", now))


class CostWidgetsRenderTest(unittest.TestCase):
    """Through the real build_status_line: the bar draws the old figure at once."""

    ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")

    def _render(self, currency="$"):
        show = {k: False for k in ("session", "weekly", "context", "branch", "model",
                                   "streak", "update", "claude_update", "effort",
                                   "pomodoro", "cost", "lines", "plan")}
        show.update(cumulative_cost=True, weekly_cost=True)
        cfg = {"show": show, "layout": "standard", "currency": currency}
        return self.ANSI.sub("", cs.build_status_line({}, "", cfg, {}))

    def test_stale_figures_are_on_the_bar_without_a_scan(self):
        with _sandbox() as (state, _cfg):
            _write_cache(state, "cumulative_cost_cache.json", total=12.34, age=7200)
            _write_cache(state, "weekly_cost_cache.json", total=5.0, age=7200)
            popen = _Popen()
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", popen):
                line = self._render()
            self.assertIn("All: $12.34", line)
            self.assertIn("7d: $5.00", line)
            self.assertEqual(len(popen.calls), 2)

    def test_first_ever_render_prints_without_the_widgets(self):
        with _sandbox():
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", _Popen()):
                line = self._render()
            self.assertNotIn("All:", line)
            self.assertNotIn("7d:", line)

    def test_danish_kroner_on_the_bar(self):
        with _sandbox() as (state, _cfg):
            _write_cache(state, "cumulative_cost_cache.json", total=10.0, age=5)
            _write_cache(state, "weekly_cost_cache.json", total=10.0, age=5)
            with mock.patch.object(cs, "_fetch_bounded", return_value=None):
                line = self._render(currency="DKK")
            self.assertIn("All: DKK68.50", line)
            self.assertIn("7d: DKK68.50", line)


class CostWorkerTest(unittest.TestCase):

    @staticmethod
    def _entry(ts, **usage):
        usage.setdefault("input_tokens", 1_000_000)
        usage.setdefault("output_tokens", 0)
        return {"type": "assistant", "timestamp": ts,
                "message": {"model": "claude-sonnet-5-5", "usage": usage}}

    def _transcript(self, cfg, entries, name="s.jsonl"):
        path = cfg / "projects" / "proj" / name
        path.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
        return path

    def test_worker_saves_the_figure_and_drops_its_claim(self):
        with _sandbox() as (state, cfg):
            self._transcript(cfg, [self._entry("2026-01-01T00:00:00Z")])
            self.assertTrue(cs._claim_cost_scan("cumulative", time.time()))
            cs._cost_refresh_worker("cumulative")
            saved = json.loads((state / "cumulative_cost_cache.json").read_text("utf-8"))
            self.assertEqual(saved["v"], _CACHE_VERSION)
            self.assertAlmostEqual(saved["data"]["total_cost_usd"], 2.0)
            self.assertFalse((state / "cost_scan_cumulative.lock").exists())
            # ...and the next repaint, a new process, draws it without any work.
            popen = _Popen()
            with mock.patch.object(cs, "_scan_session_costs", _no_inline_scan), \
                    mock.patch.object(cs.subprocess, "Popen", popen):
                self.assertAlmostEqual(
                    cs._get_cached_cumulative_cost()["total_cost_usd"], 2.0)
            self.assertEqual(popen.calls, [])

    def test_weekly_worker_counts_only_the_last_seven_days(self):
        with _sandbox() as (state, cfg):
            now = datetime.now(timezone.utc)
            fresh = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            old = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._transcript(cfg, [self._entry(fresh), self._entry(old)])
            cs._cost_refresh_worker("weekly")
            saved = json.loads((state / "weekly_cost_cache.json").read_text("utf-8"))
            self.assertAlmostEqual(saved["data"]["total_cost_usd"], 2.0)

    def test_a_failed_scan_keeps_the_claim_so_it_is_not_retried_at_once(self):
        with _sandbox() as (state, _cfg):
            self.assertTrue(cs._claim_cost_scan("cumulative", time.time()))
            with mock.patch.object(cs, "_scan_session_costs",
                                   side_effect=RuntimeError("disk gone")):
                cs._cost_refresh_worker("cumulative")
            self.assertTrue((state / "cost_scan_cumulative.lock").exists())
            self.assertFalse((state / "cumulative_cost_cache.json").exists())

    def test_unknown_kind_is_ignored(self):
        with _sandbox():
            cs._cost_refresh_worker("nonsense")  # must not raise

    def test_spawn_detaches_from_the_status_line_process(self):
        popen = _Popen()
        with mock.patch.object(cs.subprocess, "Popen", popen):
            self.assertTrue(cs._spawn_cost_worker("weekly"))
        args, kwargs = popen.calls[0]
        self.assertEqual(args[0], sys.executable)
        self.assertEqual(args[-1], "weekly")
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertTrue(kwargs["creationflags"] & 0x00000008, "DETACHED_PROCESS")
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_spawn_reports_failure(self):
        def boom(*a, **k):
            raise OSError("nope")
        with mock.patch.object(cs.subprocess, "Popen", boom):
            self.assertFalse(cs._spawn_cost_worker("weekly"))

    def test_a_real_background_process_fills_the_cache(self):
        """End to end: the render path starts a genuine detached interpreter,
        which loads this file, scans a synthetic store and saves the figure."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = root / "claude"
            (cfg / "projects" / "proj").mkdir(parents=True)
            (cfg / "projects" / "proj" / "s.jsonl").write_text(
                json.dumps(self._entry("2026-01-01T00:00:00Z")), encoding="utf-8")
            local = root / "local"
            local.mkdir()
            env = {"CLAUDE_CONFIG_DIR": str(cfg), "LOCALAPPDATA": str(local),
                   "XDG_CACHE_HOME": str(local), "HOME": str(root),
                   "USERPROFILE": str(root)}
            with mock.patch.dict(os.environ, env):
                state = cs.get_account_state_dir()
                self.assertTrue(str(state).startswith(str(root)), state)
                cache = state / "cumulative_cost_cache.json"
                claim = state / "cost_scan_cumulative.lock"
                data = cs._get_cached_cumulative_cost()  # spawns the real worker
                self.assertEqual(data, {})
                deadline = time.time() + 60
                while time.time() < deadline and (not cache.exists() or claim.exists()):
                    time.sleep(0.1)
                self.assertTrue(cache.exists(), "the background process never saved a figure")
                saved = json.loads(cache.read_text("utf-8"))
                self.assertAlmostEqual(saved["data"]["total_cost_usd"], 2.0)
                self.assertFalse(claim.exists(), "claim left behind")


# ---------------------------------------------------------------------------
# F18
# ---------------------------------------------------------------------------

class ScanCountsEachResponseOnceTest(unittest.TestCase):

    def _scan(self, files, since_ts=None):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "projects" / "proj"
            proj.mkdir(parents=True)
            for name, entries in files.items():
                (proj / name).write_text(
                    "\n".join(json.dumps(e) for e in entries), encoding="utf-8")
            with mock.patch.object(cs, "_claude_config_dir", lambda: Path(td)):
                return cs._scan_session_costs(since_ts=since_ts)

    @staticmethod
    def _entry(mid="msg_1", rid="req_1", model="claude-sonnet-5-5",
               ts="2026-10-01T12:00:00Z", **usage):
        usage.setdefault("input_tokens", 1_000_000)
        usage.setdefault("output_tokens", 0)
        message = {"model": model, "usage": usage}
        if mid is not None:
            message["id"] = mid
        entry = {"type": "assistant", "timestamp": ts, "message": message}
        if rid is not None:
            entry["requestId"] = rid
        return entry

    def test_lines_of_one_response_are_counted_once(self):
        """One JSONL line per content block, all repeating the same usage."""
        block = self._entry()
        result = self._scan({"a.jsonl": [block, dict(block), dict(block)]})
        self.assertAlmostEqual(result["total_cost_usd"], 2.0)
        self.assertEqual(result["total_tokens"], 1_000_000)
        self.assertEqual(result["models"]["claude-sonnet-5-5"]["input"], 1_000_000)

    def test_different_responses_are_all_counted(self):
        result = self._scan({"a.jsonl": [
            self._entry("m1", "r1"), self._entry("m2", "r2"), self._entry("m1", "r2")]})
        self.assertAlmostEqual(result["total_cost_usd"], 6.0)

    def test_a_response_copied_into_a_resumed_session_file_is_counted_once(self):
        block = self._entry()
        result = self._scan({"a.jsonl": [block], "b.jsonl": [dict(block)]})
        self.assertAlmostEqual(result["total_cost_usd"], 2.0)

    def test_missing_request_id_falls_back_to_the_message_id(self):
        result = self._scan({"a.jsonl": [self._entry("m1", None), self._entry("m1", None)]})
        self.assertAlmostEqual(result["total_cost_usd"], 2.0)

    def test_entries_without_ids_are_never_merged(self):
        result = self._scan({"a.jsonl": [self._entry(None, None), self._entry(None, None)]})
        self.assertAlmostEqual(result["total_cost_usd"], 4.0)

    def test_the_fullest_report_of_a_response_wins(self):
        """If an early block carried a partial output count, the final figure
        replaces it instead of being dropped as a duplicate."""
        partial = self._entry(output_tokens=1)
        final = self._entry(output_tokens=1_000_000)
        for order in ((partial, final), (final, partial)):
            result = self._scan({"a.jsonl": list(order)})
            # 1M input at $2 + 1M output at $10.
            self.assertAlmostEqual(result["total_cost_usd"], 12.0)
            self.assertEqual(result["models"]["claude-sonnet-5-5"]["output"], 1_000_000)

    def test_dedupe_applies_inside_the_weekly_window_too(self):
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        block = self._entry(ts=ts)
        result = self._scan({"a.jsonl": [block, dict(block)]},
                            since_ts=(now - timedelta(days=7)).timestamp())
        self.assertAlmostEqual(result["total_cost_usd"], 2.0)

    def test_one_hour_cache_writes_cost_twice_the_input_rate(self):
        # Sonnet 5.5: $2 input -> $2.50 for 5 minutes, $4 for one hour.
        one_hour = self._entry(
            cache_creation_input_tokens=1_000_000, input_tokens=0,
            cache_creation={"ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 1_000_000})
        self.assertAlmostEqual(self._scan({"a.jsonl": [one_hour]})["total_cost_usd"], 4.0)

    def test_five_minute_and_unbroken_down_cache_writes_keep_the_5m_rate(self):
        five = self._entry(
            cache_creation_input_tokens=1_000_000, input_tokens=0,
            cache_creation={"ephemeral_5m_input_tokens": 1_000_000,
                            "ephemeral_1h_input_tokens": 0})
        plain = self._entry(mid="m2", rid="r2", cache_creation_input_tokens=1_000_000,
                            input_tokens=0)
        self.assertAlmostEqual(self._scan({"a.jsonl": [five]})["total_cost_usd"], 2.5)
        self.assertAlmostEqual(self._scan({"a.jsonl": [plain]})["total_cost_usd"], 2.5)

    def test_mixed_cache_writes_are_priced_part_by_part(self):
        mixed = self._entry(
            cache_creation_input_tokens=1_000_000, input_tokens=0,
            cache_creation={"ephemeral_5m_input_tokens": 400_000,
                            "ephemeral_1h_input_tokens": 600_000})
        result = self._scan({"a.jsonl": [mixed]})
        self.assertAlmostEqual(result["total_cost_usd"], 0.4 * 2.5 + 0.6 * 4.0)
        self.assertEqual(result["models"]["claude-sonnet-5-5"]["cache_write"], 1_000_000)
        self.assertEqual(result["total_tokens"], 1_000_000)

    def test_breakdown_without_the_total_is_still_priced(self):
        only_breakdown = self._entry(
            input_tokens=0,
            cache_creation={"ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 1_000_000})
        self.assertAlmostEqual(
            self._scan({"a.jsonl": [only_breakdown]})["total_cost_usd"], 4.0)

    def test_malformed_breakdown_falls_back_to_the_5m_rate(self):
        for junk in (None, "x", [], {"ephemeral_1h_input_tokens": "lots"},
                     {"ephemeral_1h_input_tokens": -5},
                     {"ephemeral_1h_input_tokens": 9_999_999_999}):
            with self.subTest(junk=junk):
                entry = self._entry(cache_creation_input_tokens=1_000_000,
                                    input_tokens=0, cache_creation=junk)
                cost = self._scan({"a.jsonl": [entry]})["total_cost_usd"]
                # Never below the 5-minute price, never above the 1-hour price.
                self.assertGreaterEqual(cost, 2.5 - 1e-9)
                self.assertLessEqual(cost, 4.0 + 1e-9)

    def test_one_hour_rate_follows_a_long_context_haiku_request(self):
        # Haiku 5.5 above 100K prompt tokens: $0.50 input -> $1.00 for 1 hour.
        entry = self._entry(
            model="claude-haiku-5-5", input_tokens=0,
            cache_creation_input_tokens=200_000,
            cache_creation={"ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 200_000})
        self.assertAlmostEqual(self._scan({"a.jsonl": [entry]})["total_cost_usd"], 0.2)


# ---------------------------------------------------------------------------
# F30
# ---------------------------------------------------------------------------

@contextmanager
def _offline():
    """No network, no cached rate: the code's own fallback table decides."""
    with _sandbox():
        with mock.patch.object(cs, "_fetch_bounded", return_value=None):
            yield


@contextmanager
def _locale(name):
    import locale
    with mock.patch.object(locale, "getdefaultlocale", lambda *a, **k: (name, "UTF-8")):
        yield


class KroneAndHryvniaTest(unittest.TestCase):

    def test_a_danish_machine_is_priced_in_danish_kroner(self):
        with _offline(), _locale("da_DK"):
            symbol = cs._detect_default_currency()
            rate, code = cs._get_exchange_rate(symbol)
        self.assertEqual(code, "DKK")
        self.assertEqual(rate, cs._FALLBACK_RATES["DKK"])

    def test_a_norwegian_machine_is_priced_in_norwegian_kroner(self):
        with _offline(), _locale("nb_NO"):
            symbol = cs._detect_default_currency()
            rate, code = cs._get_exchange_rate(symbol)
        self.assertEqual((rate, code), (cs._FALLBACK_RATES["NOK"], "NOK"))

    def test_a_swedish_machine_still_gets_kr_at_the_swedish_rate(self):
        with _offline(), _locale("sv_SE"):
            symbol = cs._detect_default_currency()
            rate, code = cs._get_exchange_rate(symbol)
        self.assertEqual(symbol, "kr")
        self.assertEqual((rate, code), (cs._FALLBACK_RATES["SEK"], "SEK"))

    def test_iso_codes_can_be_chosen(self):
        with _offline():
            for given, code in (("DKK", "DKK"), ("NOK", "NOK"), ("SEK", "SEK"),
                                ("nok", "NOK"), (" dkk ", "DKK"), ("GBP", "GBP"),
                                ("USD", "USD")):
                with self.subTest(given=given):
                    rate, got = cs._get_exchange_rate(given)
                    self.assertEqual(got, code)
                    if code != "USD":
                        self.assertEqual(rate, cs._FALLBACK_RATES[code])

    def test_a_saved_bare_kr_follows_the_machines_country(self):
        """Configs saved before the fix hold the shared symbol "kr"."""
        for loc, code in (("da_DK", "DKK"), ("nb_NO", "NOK"), ("sv_SE", "SEK"),
                          ("en_GB", "SEK"), ("en_US", "SEK")):
            with self.subTest(locale=loc), _offline(), _locale(loc):
                self.assertEqual(cs._get_exchange_rate("kr")[1], code)

    def test_a_symbol_the_rate_service_cannot_price_is_not_labelled_as_money(self):
        """The hryvnia has no rate: USD amounts must not wear a hryvnia sign."""
        with _offline():
            self.assertEqual(cs._get_exchange_rate("₴"), (1.0, "USD"))

    def test_a_code_with_no_rate_at_all_is_shown_as_dollars(self):
        """Offline, with no cached or built-in rate, the amount is still USD."""
        with _offline():
            rate, code = cs._get_exchange_rate("₱")  # peso: priced online only
            self.assertEqual((rate, code), (1.0, "USD"))

    def test_existing_symbols_are_unchanged(self):
        with _offline():
            for symbol, code in (("$", "USD"), ("£", "GBP"), ("€", "EUR"),
                                 ("¥", "JPY"), ("C$", "CAD"), ("Fr", "CHF"),
                                 ("₹", "INR"), ("zł", "PLN"), ("R", "ZAR")):
                with self.subTest(symbol=symbol):
                    self.assertEqual(cs._get_exchange_rate(symbol)[1], code)

    def test_junk_input_is_still_usd(self):
        with _offline():
            for junk in ("", "??", "xyz", "kr.", None):
                with self.subTest(junk=junk):
                    self.assertEqual(cs._get_exchange_rate(junk), (1.0, "USD"))

    def test_the_fetched_rate_is_used_for_the_right_code(self):
        with _sandbox():
            body = json.dumps({"rates": {"DKK": 7.1}}).encode()
            with mock.patch.object(cs, "_fetch_bounded", return_value=body) as fetched:
                self.assertEqual(cs._get_exchange_rate("DKK"), (7.1, "DKK"))
            self.assertIn("to=DKK", fetched.call_args[0][0].full_url)


# ---------------------------------------------------------------------------
# F39
# ---------------------------------------------------------------------------

class _LocalTime(datetime):
    """A reset time whose local rendering is fixed, whatever zone the test
    machine is in."""
    local = None

    def astimezone(self, tz=None):
        return type(self).local


def _local(hour, minute, offset=(5, 30)):
    tz = timezone(timedelta(hours=offset[0], minutes=offset[1]))
    _LocalTime.local = datetime(2026, 11, 6, hour, minute, tzinfo=tz)  # a Friday
    return _LocalTime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)


class WeeklyDateMinutesTest(unittest.TestCase):

    def test_twelve_hour_clock_keeps_the_minutes(self):
        day = _local(10, 30).astimezone().strftime("%a")
        for (h, m), want in {
            (10, 30): "10:30am", (17, 45): "5:45pm", (0, 30): "12:30am",
            (12, 30): "12:30pm", (9, 5): "9:05am", (23, 59): "11:59pm",
        }.items():
            with self.subTest(time=f"{h}:{m:02d}"):
                self.assertEqual(cs._weekly_date(_local(h, m), clock="12h"),
                                 f"{day} {want}")

    def test_on_the_hour_stays_compact(self):
        day = _local(10, 0).astimezone().strftime("%a")
        for h, want in {10: "10am", 17: "5pm", 0: "12am", 12: "12pm", 13: "1pm"}.items():
            with self.subTest(hour=h):
                self.assertEqual(cs._weekly_date(_local(h, 0), clock="12h"),
                                 f"{day} {want}")

    def test_default_clock_is_twelve_hour(self):
        day = _local(10, 30).astimezone().strftime("%a")
        self.assertEqual(cs._weekly_date(_local(10, 30)), f"{day} 10:30am")

    def test_twenty_four_hour_clock_is_unchanged(self):
        day = _local(10, 30).astimezone().strftime("%a")
        self.assertEqual(cs._weekly_date(_local(10, 30), clock="24h"), f"{day} 10:30")
        self.assertEqual(cs._weekly_date(_local(17, 0), clock="24h"), f"{day} 17:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
