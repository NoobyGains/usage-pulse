"""GitHub Copilot CLI: the account record, the row, the host line, the installer.

Run: ``python -m unittest tests.test_provider_copilot``
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import (  # noqa: E402
    ProviderSandbox, copilot_record, copilot_snapshot, cs, iso_z, write_copilot)

ANSI = re.compile(r"\x1b\[[0-9;]*m")
SCRIPT = Path(cs.__file__).resolve()


def plain(text):
    return ANSI.sub("", text)


def business():
    return copilot_record("work", sku="copilot_business_seat", plan="business", tbb=True,
                          budget=copilot_snapshot(8000, 2400, 30.0, tbb=True),
                          reset=iso_z(86400 * 19))


def legacy_pro():
    return copilot_record("pro", budget=copilot_snapshot(300, 231, 77.0), reset=iso_z(86400 * 5))


def free(chat_left=150):
    """Copilot Free as its record really reads: no premium allowance, 200 chat."""
    rec = copilot_record("free", sku="free_limited_copilot", tbb=True, reset=iso_z(86400 * 19),
                         budget=copilot_snapshot(0, 0, 0, tbb=True, has_quota=False,
                                                 quota_reset_at=0))
    rec["response"]["quota_snapshots"]["chat"] = copilot_snapshot(
        200, chat_left, chat_left / 2, tbb=True, quota_id="chat", quota_reset_at=0)
    return rec


def unlimited(login="ent", plan="enterprise", sku="copilot_enterprise_seat"):
    """A seat with no budget of its own (Enterprise without a per-user budget)."""
    return copilot_record(login, sku=sku, plan=plan,
                          budget=copilot_snapshot(-1, 0, 100, unlimited=True))


def no_licence():
    return copilot_record("seatless", sku="no_access")


class RecordTest(unittest.TestCase):
    """_copilot_info: one account record into the figures the bar draws."""

    def info(self, rec):
        return cs._copilot_info(rec["response"], 1.0, time.time())

    def test_business_budget_in_ai_credits(self):
        info = self.info(business())
        self.assertEqual(info["state"], "ok")
        self.assertEqual(info["plan"], "Business")
        self.assertEqual(info["unit"], "AIC")
        self.assertFalse(info["unlimited"])
        self.assertAlmostEqual(info["window"]["utilization"], 70.0)
        self.assertEqual((info["used"], info["limit"]), (5600.0, 8000.0))
        self.assertIsNotNone(info["window"]["resets_at"])

    def test_legacy_billing_counts_premium_requests(self):
        info = self.info(legacy_pro())
        self.assertEqual(info["unit"], "req")
        self.assertAlmostEqual(info["window"]["utilization"], 23.0)
        self.assertEqual((info["used"], info["limit"]), (69.0, 300.0))

    def test_free_plan_draws_its_chat_allowance(self):
        info = self.info(free())
        self.assertEqual((info["plan"], info["unit"], info["unlimited"]), ("Free", "chat", False))
        self.assertAlmostEqual(info["window"]["utilization"], 25.0)
        self.assertEqual((info["used"], info["limit"]), (50.0, 200.0))
        self.assertIsNotNone(info["window"]["resets_at"])     # quota_reset_at is 0: top-level date

    def test_no_allowance_anywhere_draws_no_bar(self):
        rec = free()
        rec["response"]["quota_snapshots"]["chat"] = copilot_snapshot(1, 1, 100, unlimited=True)
        info = self.info(rec)
        self.assertEqual((info["window"], info["unlimited"], info["unit"]), (None, False, "AIC"))

    def test_seat_without_a_budget_of_its_own_has_no_bar(self):
        info = self.info(unlimited())
        self.assertEqual((info["plan"], info["unlimited"], info["window"]),
                         ("Enterprise", True, None))

    def test_signed_in_without_a_seat(self):
        info = self.info(no_licence())
        self.assertEqual(info["state"], "no_licence")
        self.assertIsNone(info["window"])
        seatless = {"login": "x", "chat_enabled": False}    # no sku at all
        self.assertEqual(cs._copilot_info(seatless)["state"], "no_licence")

    def test_overage_and_exhausted_budget(self):
        rec = copilot_record("o", budget=copilot_snapshot(6000, 0, 0.0, overage=250,
                                                           overage_ok=True, tbb=True))
        info = self.info(rec)
        self.assertEqual(info["window"]["utilization"], 100.0)
        self.assertEqual((info["used"], info["overage"], info["overage_ok"]), (6000.0, 250.0, True))

    def test_percentage_worked_out_when_the_record_has_none(self):
        rec = copilot_record("p", budget=copilot_snapshot(200, 50, None))
        self.assertAlmostEqual(self.info(rec)["window"]["utilization"], 75.0)

    def test_budget_that_rolled_over_reads_zero(self):
        rec = copilot_record("r", budget=copilot_snapshot(300, 30, 10.0, overage=4),
                             reset=iso_z(-3600))
        info = self.info(rec)
        self.assertEqual(info["window"], {"utilization": 0.0, "resets_at": None})
        self.assertEqual((info["used"], info["overage"]), (0.0, 0.0))

    def test_reset_falls_back_to_the_snapshot_epoch(self):
        rec = copilot_record("e", budget=copilot_snapshot(300, 150, 50.0,
                                                           quota_reset_at=int(time.time()) + 7200))
        del rec["response"]["quota_reset_date_utc"]
        self.assertIsNotNone(self.info(rec)["window"]["resets_at"])

    def test_damaged_values_never_raise_and_draw_no_bar(self):
        for bad in ("abc", float("nan"), float("inf"), True, None, [], {}, 0, -5):
            rec = copilot_record("d", budget=copilot_snapshot(bad, 5, 50.0))
            rec["response"]["quota_snapshots"]["premium_interactions"]["entitlement"] = bad
            info = self.info(rec)
            self.assertIsNone(info["window"], bad)
        for bad in ("abc", float("nan"), [], {}):
            rec = copilot_record("d", budget=copilot_snapshot(300, bad, bad))
            self.assertIsNone(self.info(rec)["window"], bad)
        for junk in (None, [], "x", 3):
            self.assertIsNone(cs._copilot_info(junk))
        self.assertEqual(cs._copilot_info({"quota_snapshots": "x"})["state"], "ok")

    def test_plan_names(self):
        label = cs._copilot_plan_label
        self.assertEqual(label({"access_type_sku": "free_limited_copilot",
                                "copilot_plan": "individual"}), "Free")
        self.assertEqual(label({"access_type_sku": "plus_monthly_subscriber_quota",
                                "copilot_plan": "individual"}), "Pro+")
        self.assertEqual(label({"copilot_plan": "individual"}), "Individual")
        self.assertIsNone(label({}))


class ReadTest(unittest.TestCase):
    """_read_copilot_quota: which account's record is used."""

    def setUp(self):
        self.sb = ProviderSandbox(self)

    def test_uses_the_signed_in_account(self):
        write_copilot([legacy_pro(), business()], active="Work")
        r = cs._read_copilot_quota()
        self.assertEqual(r["tier"], "Business")
        self.assertAlmostEqual(r["info"]["window"]["utilization"], 70.0)
        self.assertGreater(r["data_ts"], time.time() - 3600)

    def test_explicit_login_wins(self):
        write_copilot([legacy_pro(), business()], active="work")
        self.assertEqual(cs._read_copilot_quota(login="PRO")["info"]["unit"], "req")

    def test_never_shows_another_accounts_numbers(self):
        write_copilot([legacy_pro(), business()], active="someone-else")
        self.assertIsNone(cs._read_copilot_quota())
        self.assertIsNone(cs._read_copilot_quota(login="nobody"))

    def test_newest_record_when_no_account_is_named(self):
        old = legacy_pro()
        old["retrievedAt"] = iso_z(-86400)
        write_copilot([old, business()])
        self.assertEqual(cs._read_copilot_quota()["tier"], "Business")

    def test_newest_of_two_records_for_one_login(self):
        stale = copilot_record("work", plan="business", retrieved=-9000,
                               budget=copilot_snapshot(8000, 8000, 100.0))
        write_copilot([stale, business()], active="work")
        self.assertAlmostEqual(cs._read_copilot_quota()["info"]["window"]["utilization"], 70.0)

    def test_missing_or_damaged_file_is_no_data(self):
        self.assertIsNone(cs._read_copilot_quota())
        path = write_copilot([business()], active="work")
        for text in ("", "{not json", "[1, 2]", '{"copilotUserCache": []}',
                     '{"copilotUserCache": {"v1:x": 5, "v1:y": {"response": 7}}}',
                     "[" * 100000):
            path.write_text(text, encoding="utf-8")
            self.assertIsNone(cs._read_copilot_quota(), text[:20])
        path.write_bytes(b"\xff\xfe\x00bad")
        self.assertIsNone(cs._read_copilot_quota())

    def test_reads_files_with_and_without_copilots_comment_lines(self):
        # Copilot CLI starts the files it manages with // comment lines.
        path = write_copilot([business()], active="work")
        self.assertTrue(path.read_text(encoding="utf-8").startswith("// "))
        self.assertEqual(cs._copilot_active_login(), "work")
        self.assertEqual(cs._read_copilot_quota()["tier"], "Business")
        write_copilot([legacy_pro()], active="pro", header=False)
        self.assertEqual(cs._read_copilot_quota()["info"]["unit"], "req")
        # A value that merely contains // is data, not a comment.
        rec = business()
        rec["response"]["endpoints"] = {"api": "https://api.example.com//x"}
        write_copilot([rec], active="work")
        self.assertEqual(cs._read_copilot_quota()["tier"], "Business")
        with_bom = "﻿" + path.read_text(encoding="utf-8")
        path.write_text(with_bom, encoding="utf-8")
        self.assertEqual(cs._read_copilot_quota()["tier"], "Business")

    def test_record_location_overrides(self):
        other = self.sb.root / "elsewhere"
        with mock.patch.dict(os.environ, {"COPILOT_CACHE_HOME": str(other)}):
            self.assertEqual(cs._copilot_cache_candidates()[0],
                             other / "copilot-user-cache.json")
            write_copilot([business()])
            self.assertEqual(cs._read_copilot_quota()["tier"], "Business")
        with mock.patch.dict(os.environ, {"COPILOT_HOME": str(self.sb.root / "ch")}):
            self.assertEqual(cs._copilot_home(), self.sb.root / "ch")


class RowTest(unittest.TestCase):
    """The Copilot row under Claude Code's main line."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude")
        self.config = {
            "theme": "default", "layout": "standard", "bar_size": "large",
            "show": dict(cs.DEFAULT_SHOW), "providers_label_main": True,
            "providers": [{"id": "claude:default", "type": "claude"},
                          {"type": "copilot"}],
        }
        p = mock.patch.object(cs, "_detect_terminal_width", lambda: 220)
        p.start()
        self.addCleanup(p.stop)

    def rows(self, config=None):
        config = config or self.config
        usage = {"five_hour": {"utilization": 29.0, "resets_at": iso_z(7800)},
                 "seven_day": {"utilization": 5.0, "resets_at": iso_z(400000)}}
        line = cs.build_status_line(usage, "Max 20x", config, {"context_pct": 12})
        return [plain(r) for r in cs._append_provider_rows(line, config).split("\n")]

    def test_default_id_and_label(self):
        entries = cs._normalize_providers(self.config["providers"])
        self.assertEqual(entries[1], {"id": "copilot", "type": "copilot", "enabled": True})
        self.assertEqual(cs._provider_default_label(entries[1]), "Copilot")

    def test_budget_row_lines_up_with_the_main_line(self):
        write_copilot([business()], active="work")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertRegex(rows[1], r"^Copilot\s+Plan \S+ 70% +\| "
                                  r"5,600/8,000 AIC · 2,400 left · R:\d+ \w{3}$")
        self.assertEqual(rows[0].index(" | "), rows[1].index(" | "))

    def test_premium_requests_overage_and_plan_name(self):
        rec = copilot_record("pro", budget=copilot_snapshot(300, 0, 0.0, overage=12,
                                                             overage_ok=True),
                             sku="plus_monthly_subscriber_quota", reset=iso_z(3600 * 5))
        write_copilot([rec], active="pro")
        self.config["show"]["plan"] = True
        row = self.rows()[1]
        self.assertIn("100%", row)
        self.assertIn("300/300 req · 0 left · +12 extra · R:", row)
        self.assertTrue(row.endswith("Pro+"), row)
        self.assertRegex(row, r"R:\d+h \d+m")          # inside the last day: a countdown

    def test_free_plan_row(self):
        write_copilot([free()], active="free")
        self.assertRegex(self.rows()[1], r"^Copilot\s+Plan \S+ 25% +\| "
                                         r"50/200 chat · 150 left · R:\d+ \w{3}$")

    def test_rows_without_a_bar(self):
        write_copilot([unlimited()], active="ent")
        self.assertRegex(self.rows()[1], r"^Copilot\s+Plan no limit +\| Enterprise$")
        write_copilot([no_licence()], active="seatless")
        self.assertRegex(self.rows()[1], r"^Copilot\s+no licence$")
        write_copilot([business()], active="not-cached")
        self.assertRegex(self.rows()[1], r"^Copilot\s+Plan --$")

    def test_old_record_is_drawn_as_a_guess(self):
        old = business()
        old["retrievedAt"] = iso_z(-3 * 86400)
        write_copilot([old], active="work")
        self.assertIn("~70%", self.rows()[1])

    def test_every_layout_draws(self):
        write_copilot([business()], active="work")
        for layout, start in (("compact", "P "), ("minimal", ""), ("percent-first", "70% ")):
            cfg = dict(self.config, layout=layout)
            row = self.rows(cfg)[1]
            self.assertRegex(row, r"^Copilot\s+" + re.escape(start), layout)
            self.assertIn("70%", row)

    def test_rainbow_theme_draws(self):
        write_copilot([business()], active="work")
        self.assertIn("70%", self.rows(dict(self.config, theme="rainbow"))[1])

    def test_detected_and_enabled_without_pinning_the_path(self):
        write_copilot([business()], active="work")
        found = {i["id"]: i for i in cs.detect_providers({"providers": []})}
        item = found["copilot"]
        self.assertEqual((item["type"], item["enabled"], item["tier"], item["note"]),
                         ("copilot", True, "Business", "ok"))
        self.assertNotIn("work", json.dumps(item))            # no account name
        self.assertEqual(cs._provider_entry_from_detected(item),
                         {"id": "copilot", "type": "copilot", "enabled": True,
                          "label": "Copilot"})
        cfg = cs.set_providers({"providers": []}, enable_ids=["copilot"])
        self.assertEqual([p["type"] for p in cfg["providers"]], ["copilot"])

    def test_detection_of_a_seatless_account_is_off_by_default(self):
        write_copilot([no_licence()], active="seatless")
        item = {i["id"]: i for i in cs.detect_providers({"providers": []})}["copilot"]
        self.assertFalse(item["enabled"])
        self.assertIn("licence", item["note"])

    def test_not_detected_without_copilot(self):
        ids = [i["id"] for i in cs.detect_providers({"providers": []})]
        self.assertNotIn("copilot", ids)


# What Copilot CLI 1.0.109 really pipes in (captured from a live session):
# the account name is null, ai_used is an object, and the context figures its
# own badge shows are the current_context_* ones.
PAYLOAD = {
    "cwd": "/work/demo", "session_id": "abc123", "session_name": None,
    "transcript_path": "/tmp/t.jsonl", "version": "1.0.109", "username": None,
    "model": {"id": "gpt-5.4", "display_name": "GPT-5.4 · medium",
              "auto_tier": None, "pending_auto_tier": None},
    "workspace": {"current_dir": "/work/demo"}, "remote": {"connected": False},
    "cost": {"total_api_duration_ms": 0, "total_lines_added": 12, "total_lines_removed": 3,
             "total_duration_ms": 1127, "total_premium_requests": 3},
    "context_window": {
        "total_input_tokens": 0, "total_output_tokens": 0, "total_cache_read_tokens": 0,
        "total_cache_write_tokens": 0, "total_tokens": 0, "context_window_size": None,
        "used_percentage": None, "remaining_percentage": None, "remaining_tokens": None,
        "last_call_input_tokens": 0, "last_call_output_tokens": 0,
        "total_reasoning_tokens": 0, "current_context_tokens": 84000,
        "displayed_context_limit": 200000, "current_context_used_percentage": 42},
    "ai_used": {"total_nano_aiu": 460000000, "formatted": "0.46"},
    "allow_all_enabled": False,
}


class HostLineTest(unittest.TestCase):
    """--host copilot: the line drawn inside Copilot CLI."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.config = {"theme": "default", "layout": "standard", "bar_size": "large",
                       "show": dict(cs.DEFAULT_SHOW),
                       "providers": [{"type": "codex"}]}
        p = mock.patch.object(cs, "_detect_terminal_width", lambda: 220)
        p.start()
        self.addCleanup(p.stop)
        # No request may leave the machine from the Copilot path.
        p = mock.patch.object(cs.urllib.request, "urlopen",
                              side_effect=AssertionError("network call from --host copilot"))
        p.start()
        self.addCleanup(p.stop)

    def line(self, payload=PAYLOAD, config=None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        return plain(cs.copilot_status_line(raw, config or self.config))

    def test_plan_session_credits_and_session_widgets(self):
        write_copilot([legacy_pro(), business()], active="work")
        line = self.line()
        self.assertNotIn("\n", line)                       # no Claude provider rows
        self.assertRegex(line, r"^Plan \S+ 70% 5,600/8,000 AIC · 2,400 left · R:\d+ \w{3} "
                               r"Business \| Session 0\.46 AIC \| ")
        self.assertIn("42%", line)
        self.assertIn("GPT-5.4 · medium", line)
        for claude_only in ("Weekly", "5h", "Session ━", "Max 20x"):
            self.assertNotIn(claude_only, line)

    def test_account_is_the_signed_in_one_unless_the_payload_names_one(self):
        write_copilot([legacy_pro(), business()], active="pro")
        self.assertIn("69/300 req · 231 left", self.line())    # username null: config.json
        self.assertIn("5,600/8,000 AIC", self.line(dict(PAYLOAD, username="work")))
        no_name = {k: v for k, v in PAYLOAD.items() if k != "username"}
        self.assertIn("69/300 req", self.line(no_name))

    def test_session_cost_is_counted_in_the_accounts_unit(self):
        write_copilot([legacy_pro(), business()], active="pro")
        self.assertIn("| Session 3 req |", self.line())         # legacy: premium requests
        self.assertIn("| Session 0.46 AIC |", self.line(dict(PAYLOAD, username="work")))
        no_cost = {k: v for k, v in PAYLOAD.items() if k != "cost"}
        self.assertIn("| Session 0.46 AIC |", self.line(no_cost))

    def test_free_seatless_and_unknown_accounts(self):
        write_copilot([free(), no_licence(), unlimited()], active="free")
        self.assertRegex(self.line(),
                         r"^Plan \S+ 25% 50/200 chat · 150 left · R:\d+ \w{3} Free \| "
                         r"Session 0\.46 AIC \| ")
        self.assertRegex(self.line(dict(PAYLOAD, username="ent")),
                         r"^Plan no limit Enterprise \| Session 3 req \| ")
        seatless = self.line(dict(PAYLOAD, username="seatless"))
        self.assertRegex(seatless, r"^no licence \| ")
        self.assertNotIn("Session", seatless)                   # nothing to count
        self.assertRegex(self.line(dict(PAYLOAD, username="stranger")),
                         r"^Plan -- \| Session 0\.46 AIC \| ")

    def test_session_credit_shapes(self):
        credits = cs._copilot_session_credits
        self.assertEqual(credits(PAYLOAD), 0.46)                # the real shape
        self.assertEqual(credits({"ai_used": 0.46}), 0.46)
        self.assertEqual(credits({"ai_used": "1,234.5 AIC"}), 1234.5)
        self.assertEqual(credits({"ai_used": {"credits": 3}}), 3.0)
        self.assertEqual(credits({"ai_used": {"total_nano_aiu": 0, "formatted": "0"}}), 0.0)
        for absent in ({}, {"ai_used": None}, {"ai_used": True}, {"ai_used": -1},
                       {"ai_used": {"other": 1}}, {"ai_used": "none"}, None, [],
                       {"ai_used": {"total_nano_aiu": float("nan")}}):
            self.assertIsNone(credits(absent), absent)
        requests = cs._copilot_session_requests
        self.assertEqual(requests(PAYLOAD), 3.0)
        for absent in ({}, {"cost": None}, {"cost": {"total_premium_requests": "x"}},
                       {"cost": {"total_premium_requests": -1}}, None):
            self.assertIsNone(requests(absent), absent)
        write_copilot([business()], active="work")
        self.assertNotIn("Session", self.line({k: v for k, v in PAYLOAD.items()
                                               if k != "ai_used"}))

    def test_context_comes_from_copilots_own_badge_fields(self):
        write_copilot([business()], active="work")
        self.assertIn("42%", self.line())                   # used_percentage is null
        fresh = json.loads(json.dumps(PAYLOAD))             # before the first reply
        fresh["context_window"].update(current_context_tokens=0,
                                       current_context_used_percentage=0)
        self.assertIn(" 0%", self.line(fresh))
        tokens = dict(self.config, context_format="tokens")
        self.assertRegex(self.line(config=tokens), r"84(\.0)?k/200k")
        damaged = json.loads(json.dumps(PAYLOAD))
        damaged["context_window"] = {"current_context_used_percentage": "x",
                                     "displayed_context_limit": 0}
        self.assertIn("5,600/8,000 AIC", self.line(damaged))

    def test_garbage_on_stdin_still_draws_the_plan(self):
        write_copilot([business()], active="work")
        for raw in ("", "not json", "[1]", "null", '{"model": 5, "context_window": "x"}',
                    "{" * 50000):
            self.assertIn("5,600/8,000 AIC", self.line(raw), raw[:20])

    def test_claude_only_widgets_stay_off(self):
        write_copilot([business()], active="work")
        show = dict(cs.DEFAULT_SHOW)
        for key in ("heartbeat", "cost", "streak", "sessions", "subagents", "team",
                    "update", "claude_update", "plan", "opus", "fable", "session", "weekly"):
            show[key] = True
        seen = {}
        real = cs.build_status_line

        def spy(usage, plan, config=None, stdin_ctx=None, cache_age=None):
            seen.update(config["show"])
            return real(usage, plan, config, stdin_ctx, cache_age=cache_age)

        with mock.patch.object(cs, "build_status_line", spy):
            self.line(config=dict(self.config, show=show))
        on = {k for k, v in seen.items() if v}
        self.assertLessEqual(on, set(cs._COPILOT_HOST_PARTS) | {"weekly_timer"})
        self.assertIn("model", on)

    def test_hidden_widget_stays_hidden(self):
        write_copilot([business()], active="work")
        show = dict(cs.DEFAULT_SHOW, model=False)
        self.assertNotIn("GPT-5.4", self.line(config=dict(self.config, show=show)))


class HostProcessTest(unittest.TestCase):
    """The real command, as Copilot CLI runs it."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.claude = self.sb.claude_dir(".claude")
        self.settings = self.claude / "settings.json"
        self.settings.write_text(json.dumps({
            "statusLine": {"type": "command", "command": "python claude_status.py",
                           "refreshInterval": 7},
            "hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "x"}]}]}}),
            encoding="utf-8")
        self.sb.set_active(self.claude)

    def run_host(self, *args, stdin=json.dumps(PAYLOAD)):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin.encode(),
                              capture_output=True, env=dict(os.environ), timeout=60)

    def test_prints_one_line_and_leaves_claude_code_alone(self):
        write_copilot([business()], active="work")
        before = self.settings.read_bytes()
        listing = sorted(p.name for p in self.claude.iterdir())
        result = self.run_host("--host", "copilot")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = plain(result.stdout.decode("utf-8"))
        self.assertEqual(out.count("\n"), 1, out)
        self.assertIn("5,600/8,000 AIC", out)
        self.assertIn("GPT-5.4", out)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.claude.iterdir()), listing)

    def test_exit_code_stays_zero_without_any_copilot_data(self):
        result = self.run_host("--host", "copilot", stdin="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Plan --", plain(result.stdout.decode("utf-8")))

    def test_unknown_host_is_refused(self):
        result = self.run_host("--host", "vim")
        self.assertEqual(result.returncode, 2)
        self.assertIn("supported: copilot", result.stdout.decode("utf-8"))


class InstallTest(unittest.TestCase):
    """--install-copilot / --uninstall-copilot write only Copilot's settings."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.home = cs._copilot_home()
        self.home.mkdir(parents=True)
        self.path = self.home / "settings.json"
        p = mock.patch.object(cs, "_windows_py_launcher",
                              lambda: "py -3" if sys.platform == "win32" else None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(cs, "utf8_print", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_install_keeps_every_other_setting(self):
        self.path.write_text(json.dumps({"model": "gpt-5.4", "theme": "auto",
                                         "enabledPlugins": {"a@b": True}}), encoding="utf-8")
        self.assertEqual(cs.cmd_install_copilot(), 0)
        data = self.read()
        self.assertEqual((data["model"], data["theme"], data["enabledPlugins"]),
                         ("gpt-5.4", "auto", {"a@b": True}))
        line = data["statusLine"]
        self.assertEqual(line["type"], "command")
        self.assertTrue(line["command"].endswith(" --host copilot"), line["command"])
        self.assertIn(str(SCRIPT), line["command"])
        backups = list((self.sb.state / "settings-backups").iterdir())
        self.assertEqual(len(backups), 1)

    def test_install_into_a_fresh_folder_and_reinstall(self):
        self.assertEqual(cs.cmd_install_copilot(), 0)
        data = self.read()
        data["statusLine"]["padding"] = 2
        data["footer"] = {"showCustom": False, "showQuota": True}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(cs.cmd_install_copilot(), 0)
        data = self.read()
        self.assertEqual(data["statusLine"]["padding"], 2)         # the user's key survives
        self.assertEqual(data["footer"], {"showCustom": True, "showQuota": True})

    def test_another_tools_status_line_is_replaced_whole(self):
        self.path.write_text(json.dumps({"statusLine": {
            "type": "command", "command": "other-tool", "padding": 9}}), encoding="utf-8")
        self.assertEqual(cs.cmd_install_copilot(), 0)
        self.assertNotIn("padding", self.read()["statusLine"])

    def test_command_forms(self):
        with mock.patch.object(cs, "_windows_py_launcher", lambda: None):
            command = cs._copilot_status_command()
        self.assertTrue(command.endswith(" --host copilot"))
        self.assertIn(str(SCRIPT), command)
        if sys.platform == "win32":
            self.assertTrue(command.startswith('"'), command)
            self.assertEqual(cs._copilot_status_command(),
                             f'py -3 "{SCRIPT}" --host copilot')

    def test_unreadable_settings_are_never_written_over(self):
        for text in ("{ // comment\n}", "[1]", "nope"):
            self.path.write_text(text, encoding="utf-8")
            with mock.patch.object(sys, "stderr"):
                self.assertEqual(cs.cmd_install_copilot(), 1)
                self.assertEqual(cs.cmd_uninstall_copilot(), 1)
            self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_refuses_when_copilot_was_never_run(self):
        with mock.patch.dict(os.environ, {"COPILOT_HOME": str(self.sb.root / "absent")}):
            with mock.patch.object(sys, "stderr"):
                self.assertEqual(cs.cmd_install_copilot(), 1)
            self.assertFalse((self.sb.root / "absent").exists())

    def test_uninstall_removes_only_ours(self):
        self.path.write_text(json.dumps({"model": "m", "statusLine": {
            "type": "command", "command": "other-tool"}}), encoding="utf-8")
        self.assertEqual(cs.cmd_uninstall_copilot(), 0)
        self.assertEqual(self.read()["statusLine"]["command"], "other-tool")
        self.assertEqual(cs.cmd_install_copilot(), 0)
        self.assertEqual(cs.cmd_uninstall_copilot(), 0)
        self.assertEqual(self.read(), {"model": "m"})

    def test_install_never_touches_claude_code(self):
        claude = self.sb.claude_dir(".claude")
        settings = claude / "settings.json"
        settings.write_text('{"statusLine": {"command": "keep"}}', encoding="utf-8")
        self.sb.set_active(claude)
        self.assertEqual(cs.cmd_install_copilot(), 0)
        self.assertEqual(cs.cmd_uninstall_copilot(), 0)
        self.assertEqual(settings.read_text(encoding="utf-8"),
                         '{"statusLine": {"command": "keep"}}')

    def test_flags_are_known_to_the_cli(self):
        self.assertEqual(cs._unknown_cli_flags(
            ["--host", "--install-copilot", "--uninstall-copilot"]), [])


if __name__ == "__main__":
    unittest.main()
