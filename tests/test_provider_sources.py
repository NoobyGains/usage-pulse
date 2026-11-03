"""File-based provider sources: Gemini (agy), Codex rollouts, custom JSON, MiniMax.

Run: ``python -m unittest tests.test_provider_sources``
"""
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import (  # noqa: E402
    FakeMmx, ProviderSandbox, codex_line, cs, iso_z, mmx_quota)


class TimeHelpersTest(unittest.TestCase):
    def test_iso_z_variants(self):
        for raw in ("2026-10-21T06:13:21Z", "2026-10-21T06:13:21.123456789Z",
                    "2026-10-21T06:13:21.5+00:00", "2026-10-21T06:13:21z"):
            dt = cs._parse_reset(raw)
            self.assertIsNotNone(dt, raw)
            self.assertIsNotNone(dt.tzinfo)

    def test_parse_reset_epoch_and_garbage(self):
        self.assertEqual(int(cs._parse_reset(1792386351).timestamp()), 1792386351)
        self.assertEqual(int(cs._parse_reset(1792386351000).timestamp()), 1792386351)
        self.assertEqual(int(cs._parse_reset("1792386351").timestamp()), 1792386351)
        for bad in (None, True, "", "soon", float("nan"), float("inf"), -5, {}, [], 10 ** 30):
            self.assertIsNone(cs._parse_reset(bad), bad)

    def test_window_rollover_and_clamp(self):
        now = time.time()
        self.assertEqual(cs._provider_window(55, now - 10, now),
                         {"utilization": 0.0, "resets_at": None})
        self.assertEqual(cs._provider_window(150, None, now)["utilization"], 100.0)
        self.assertEqual(cs._provider_window(-3, None, now)["utilization"], 0.0)
        self.assertIsNone(cs._provider_window(None, None, now))
        self.assertIsNone(cs._provider_window("x", None, now))
        self.assertIsNone(cs._provider_window(float("nan"), None, now))
        w = cs._provider_window(40, now + 3600, now)
        # format_reset_time must understand what we store (Python 3.8 safe ISO).
        self.assertTrue(cs.format_reset_time(w["resets_at"]))


class GeminiTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)

    def test_happy_path_with_z_timestamps(self):
        d = self.sb.gemini(quota={
            "gemini-5h": {"remaining_fraction": 0.95487475, "reset_time": iso_z(5754)},
            "gemini-weekly": {"remaining_fraction": 0.3221116, "reset_time": iso_z(248101)},
            "3p-5h": {"remaining_fraction": 1, "reset_time": iso_z(17746)},
            "gemini-argon-wtu-weekly": {"remaining_fraction": 0, "reset_time": iso_z(9999)},
        })
        r = cs._read_gemini_quota(d, "gemini")
        self.assertAlmostEqual(r["usage"]["five_hour"]["utilization"], 4.512525, places=4)
        self.assertAlmostEqual(r["usage"]["seven_day"]["utilization"], 67.78884, places=4)
        self.assertTrue(cs.format_reset_time(r["usage"]["five_hour"]["resets_at"]))
        self.assertTrue(cs.format_weekly_reset(r["usage"]["seven_day"]["resets_at"]))
        self.assertEqual(r["tier"], "Google AI Ultra")
        # the file itself also works as the path
        self.assertEqual(cs._read_gemini_quota(d / "statusline_last_live.json")["usage"],
                         r["usage"])

    def test_expired_reset_reads_zero_and_3p_bucket(self):
        d = self.sb.gemini(quota={
            "gemini-5h": {"remaining_fraction": 0.2, "reset_time": iso_z(-60)},
            "3p-weekly": {"remaining_fraction": 0.25, "reset_time": iso_z(3600)},
        }, plan_tier=None)
        r = cs._read_gemini_quota(d, "gemini")
        self.assertEqual(r["usage"]["five_hour"], {"utilization": 0.0, "resets_at": None})
        self.assertNotIn("seven_day", r["usage"])
        self.assertIsNone(r["tier"])
        r3 = cs._read_gemini_quota(d, "3p")
        self.assertAlmostEqual(r3["usage"]["seven_day"]["utilization"], 75.0)
        self.assertNotIn("five_hour", r3["usage"])

    def test_bad_files_read_as_none(self):
        d = self.sb.home / ".gemini" / "antigravity-cli"
        d.mkdir(parents=True)
        f = d / "statusline_last_live.json"
        self.assertIsNone(cs._read_gemini_quota(d))            # missing
        f.write_text("{not json", encoding="utf-8")
        self.assertIsNone(cs._read_gemini_quota(d))            # invalid
        f.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(cs._read_gemini_quota(d))            # wrong shape
        f.write_text(json.dumps({"quota": {"gemini-5h": {"remaining_fraction": "x"}},
                                 "pad": "y" * 2_000_000}), encoding="utf-8")
        self.assertIsNone(cs._read_gemini_quota(d))            # over 1 MB
        f.write_text(json.dumps({"quota": {"gemini-5h": {"remaining_fraction": "x"}}}),
                     encoding="utf-8")
        self.assertEqual(cs._read_gemini_quota(d)["usage"], {})


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.codex = self.sb.home / ".codex"

    def test_envelope_and_tier(self):
        self.sb.codex_rollout(["{}", codex_line(29.0, 5.0)])
        r = cs._read_codex_quota(self.codex)
        self.assertEqual(r["usage"]["five_hour"]["utilization"], 29.0)
        self.assertEqual(r["usage"]["seven_day"]["utilization"], 5.0)
        self.assertEqual(r["tier"], "Plus")
        self.assertTrue(r["changed"])

    def test_premium_skipped_and_truncated_last_line(self):
        self.sb.codex_rollout([codex_line(10.0, 2.0),
                               codex_line(99.0, 99.0, limit_id="premium"),
                               codex_line(77.0, 77.0)[:-40]])  # half-written line
        r = cs._read_codex_quota(self.codex)
        self.assertEqual(r["usage"]["five_hour"]["utilization"], 10.0)

    def test_newest_without_rate_limits_falls_back(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(42.0, 7.0)], name="rollout-old.jsonl", mtime=now - 100)
        self.sb.codex_rollout(['{"type":"event_msg","payload":{"type":"user_message"}}'],
                              name="rollout-new.jsonl", mtime=now)
        r = cs._read_codex_quota(self.codex)
        self.assertEqual(r["usage"]["five_hour"]["utilization"], 42.0)
        self.assertTrue(r["src"][0]["path"].endswith("rollout-new.jsonl"))
        self.assertEqual(len(r["src"]), 2, "every candidate is part of the cache key")

    def test_empty_or_missing_sessions(self):
        self.assertIsNone(cs._read_codex_quota(self.codex))
        (self.codex / "sessions").mkdir(parents=True)
        self.assertIsNone(cs._read_codex_quota(self.codex))
        (self.codex / "sessions" / "2026" / "10" / "19").mkdir(parents=True)
        self.assertIsNone(cs._read_codex_quota(self.codex))

    def test_only_two_newest_day_dirs_are_scanned(self):
        now = time.time()
        # an older day dir holding a newer file must not be looked at
        self.sb.codex_rollout([codex_line(11.0, 1.0)], day="2026/10/17", name="rollout-x.jsonl",
                              mtime=now)
        self.sb.codex_rollout([codex_line(22.0, 2.0)], day="2026/10/18", name="rollout-y.jsonl",
                              mtime=now - 50)
        self.sb.codex_rollout([codex_line(33.0, 3.0)], day="2026/10/19", name="rollout-z.jsonl",
                              mtime=now - 100)
        files = cs._find_codex_rollouts(self.codex)
        self.assertEqual([f.name for f in files], ["rollout-y.jsonl", "rollout-z.jsonl"])
        # month boundaries sort chronologically too: 11/01 and 10/19 are now
        # the two newest days, so 10/18 drops out; newest mtime first.
        self.sb.codex_rollout([codex_line(44.0, 4.0)], day="2026/11/01", name="rollout-n.jsonl",
                              mtime=now - 500)
        files = cs._find_codex_rollouts(self.codex)
        self.assertEqual([f.name for f in files], ["rollout-z.jsonl", "rollout-n.jsonl"])
        self.assertEqual(cs._find_codex_rollout(self.codex).name, "rollout-z.jsonl")

    def test_large_rollout_reads_only_the_tail(self):
        f = self.sb.codex_rollout([], name="rollout-big.jsonl")
        with open(f, "wb") as fh:
            fh.write(b'{"type":"x","payload":{}}\n')
            fh.seek(20_000_000)
            fh.write(b"\n" + codex_line(64.0, 8.0).encode("utf-8") + b"\n")
        reads = []
        real_open = open

        def spy_open(path, mode="r", *a, **kw):
            fh = real_open(path, mode, *a, **kw)
            if "b" in mode:
                real_read = fh.read

                def read(n=-1):
                    data = real_read(n)
                    reads.append(len(data))
                    return data
                fh.read = read
            return fh

        with mock.patch.object(cs, "open", spy_open, create=True):
            r = cs._read_codex_quota(self.sb.home / ".codex")
        self.assertEqual(r["usage"]["five_hour"]["utilization"], 64.0)
        self.assertLessEqual(sum(reads), cs._CODEX_TAIL_BYTES)

    def test_change_in_the_rollout_actually_used_invalidates(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(20.0, 2.0)], name="rollout-old.jsonl", mtime=now - 100)
        self.sb.codex_rollout(['{"type":"event_msg","payload":{"type":"user_message"}}'],
                              name="rollout-new.jsonl", mtime=now)
        first = cs._read_codex_quota(self.codex)
        self.assertEqual(first["usage"]["five_hour"]["utilization"], 20.0)
        cached = json.loads(json.dumps({"timestamp": first["data_ts"], "usage": first["usage"],
                                        "src": first["src"], "plan": first["tier"]}))
        # The older, still-running session reports new numbers; the newest file is untouched.
        self.sb.codex_rollout([codex_line(80.0, 8.0)], name="rollout-old.jsonl", mtime=now - 50)
        again = cs._read_codex_quota(self.codex, cached)
        self.assertTrue(again["changed"])
        self.assertEqual(again["usage"]["five_hour"]["utilization"], 80.0)

    def test_cache_hit_skips_file_read(self):
        self.sb.codex_rollout([codex_line(29.0, 5.0)])
        first = cs._read_codex_quota(self.codex)
        cached = {"timestamp": first["data_ts"], "usage": first["usage"],
                  "src": first["src"], "plan": first["tier"]}
        cached = json.loads(json.dumps(cached))  # survives the JSON round trip
        with mock.patch.object(cs, "_codex_rate_limits_from_tail",
                               side_effect=AssertionError("file was re-read")):
            again = cs._read_codex_quota(self.codex, cached)
        self.assertFalse(again["changed"])
        self.assertEqual(again["usage"], first["usage"])

    def test_codex_home_env_honoured(self):
        other = self.sb.root / "elsewhere"
        self.sb.codex_rollout([codex_line(12.0, 3.0)], home=other)
        os.environ["CODEX_HOME"] = str(other)
        self.assertEqual(cs._codex_home(), other)
        self.assertEqual(cs._read_codex_quota(cs._codex_home())["usage"]["five_hour"]["utilization"],
                         12.0)


class FileProviderTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.path = self.sb.root / "kimi.json"

    def _write(self, data, raw=None):
        self.path.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")

    def test_top_level_and_label(self):
        now = time.time()
        self._write({"label": "Kimi", "five_hour": {"used_percentage": 42, "resets_at": int(now + 600)},
                     "seven_day": {"used_percentage": 7, "resets_at": iso_z(86400)}})
        r = cs._read_file_quota(self.path)
        self.assertEqual(r["label"], "Kimi")
        self.assertEqual(r["usage"]["five_hour"]["utilization"], 42.0)
        self.assertEqual(r["usage"]["seven_day"]["utilization"], 7.0)
        self.assertTrue(r["usage"]["seven_day"]["resets_at"])

    def test_under_rate_limits_with_utilization_and_ms(self):
        now = time.time()
        self._write({"rate_limits": {"five_hour": {"utilization": 12.5,
                                                   "resets_at": int((now + 600) * 1000)}}})
        r = cs._read_file_quota(self.path)
        self.assertEqual(r["usage"]["five_hour"]["utilization"], 12.5)
        self.assertNotIn("seven_day", r["usage"])
        self.assertIsNone(r["label"])

    def test_malformed_files(self):
        self.assertIsNone(cs._read_file_quota(self.path))      # missing
        self._write(None, raw="nope")
        self.assertIsNone(cs._read_file_quota(self.path))
        self._write([1, 2])
        self.assertIsNone(cs._read_file_quota(self.path))
        self._write({"five_hour": "x", "seven_day": {"used_percentage": "lots"},
                     "label": "\x1b[2Jevil"})
        r = cs._read_file_quota(self.path)
        self.assertEqual(r["usage"], {})
        self.assertNotIn("\x1b", r["label"])

    def test_bom_is_fine(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"five_hour": {"used_percentage": 3}}).encode("utf-8"))
        self.assertEqual(cs._read_file_quota(self.path)["usage"]["five_hour"]["utilization"], 3.0)


class MiniMaxProviderTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)

    def _run(self):
        # mmx runs with no pipes now: its output lands in the stdout file it is
        # handed, as the real subprocess.run would deliver it.
        return FakeMmx(mmx_quota(left5=80, leftw=40))

    def test_reuses_parser_and_respects_ttl(self):
        run = self._run()
        with mock.patch.object(cs.shutil, "which", return_value="/bin/mmx"), \
                mock.patch.object(cs.subprocess, "run", run):
            c = cs._read_minimax_quota("minimax")
            self.assertAlmostEqual(c["usage"]["five_hour"]["utilization"], 20.0)
            self.assertAlmostEqual(c["usage"]["seven_day"]["utilization"], 60.0)
            cs._read_minimax_quota("minimax")
            self.assertEqual(len(run.calls), 1)  # cached for 120 s

    def test_no_mmx_no_process(self):
        with mock.patch.object(cs.shutil, "which", return_value=None), \
                mock.patch.object(cs.subprocess, "run") as run:
            self.assertEqual(cs._read_minimax_quota("minimax"), {})
            run.assert_not_called()

    def test_row_skipped_when_window_runs_on_minimax(self):
        config = {"providers": [{"type": "minimax", "id": "minimax"}]}
        self.assertEqual(len(cs._provider_row_entries(config)), 1)
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
                                          "ANTHROPIC_MODEL": "MiniMax-M3"}):
            self.assertEqual(cs._provider_row_entries(config), [])


if __name__ == "__main__":
    unittest.main()
