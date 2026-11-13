"""Tests for usage normalisation, config-dir resolution, and pricing lookup.

Three separate concerns, all of which decide what numbers the user sees:

* ``_normalize_usage`` folds the OAuth payload's model-scoped ``limits[]``
  entries (how Fable's weekly cap is reported) into flat ``seven_day_<model>``
  keys the renderer already understands;
* ``_claude_config_dir`` honours ``CLAUDE_CONFIG_DIR`` so a multi-profile setup
  doesn't get its status line registered in one directory and its ``/pulse``
  command in another;
* pricing lookup resolves the *longest* matching model prefix — a first-match
  scan lets the bare ``claude-opus-4`` key ($15/$75) swallow ``claude-opus-4-8``
  ($5/$25) and treble every reported cost.

Run: ``python tests/test_usage_and_config.py``
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402


class NormalizeUsageTest(unittest.TestCase):
    def test_weekly_scoped_becomes_seven_day_key(self):
        usage = cs._normalize_usage({"limits": [{
            "kind": "weekly_scoped",
            "percent": 82,
            "resets_at": "2026-08-01T00:00:00Z",
            "scope": {"model": {"display_name": "Fable"}},
        }]})
        self.assertEqual(usage["seven_day_fable"]["utilization"], 82.0)
        self.assertEqual(usage["seven_day_fable"]["resets_at"], "2026-08-01T00:00:00Z")

    def test_display_name_is_slugified(self):
        usage = cs._normalize_usage({"limits": [{
            "kind": "weekly_scoped", "percent": 5,
            "scope": {"model": {"display_name": "Sonnet 4.6"}},
        }]})
        self.assertIn("seven_day_sonnet_4_6", usage)

    def test_falls_back_to_model_id(self):
        usage = cs._normalize_usage({"limits": [{
            "kind": "weekly_scoped", "percent": 5,
            "scope": {"model": {"id": "claude-fable-5"}},
        }]})
        self.assertIn("seven_day_claude_fable_5", usage)

    def test_existing_flat_field_wins(self):
        """A top-level field that already has data must not be overwritten."""
        usage = cs._normalize_usage({
            "seven_day_fable": {"utilization": 10.0},
            "limits": [{
                "kind": "weekly_scoped", "percent": 99,
                "scope": {"model": {"display_name": "Fable"}},
            }],
        })
        self.assertEqual(usage["seven_day_fable"]["utilization"], 10.0)

    def test_other_kinds_ignored(self):
        usage = cs._normalize_usage({"limits": [
            {"kind": "five_hour", "percent": 50,
             "scope": {"model": {"display_name": "Opus"}}},
        ]})
        self.assertNotIn("seven_day_opus", usage)

    def test_missing_percent_skipped(self):
        usage = cs._normalize_usage({"limits": [{
            "kind": "weekly_scoped",
            "scope": {"model": {"display_name": "Fable"}},
        }]})
        self.assertNotIn("seven_day_fable", usage)

    def test_malformed_entries_never_raise(self):
        payloads = [
            {"limits": "not-a-list"},
            {"limits": ["string", 7, None, {}]},
            {"limits": [{"kind": "weekly_scoped", "percent": "abc",
                         "scope": {"model": {"display_name": "X"}}}]},
            {"limits": [{"kind": "weekly_scoped", "percent": 1, "scope": None}]},
            {"limits": [{"kind": "weekly_scoped", "percent": 1,
                         "scope": {"model": "not-a-dict"}}]},
            {"limits": [{"kind": "weekly_scoped", "percent": 1,
                         "scope": {"model": {"display_name": "!!!"}}}]},
            {},
            "not-a-dict",
            None,
        ]
        for payload in payloads:
            cs._normalize_usage(payload)  # must not raise

    def test_no_limits_returns_input_unchanged(self):
        self.assertEqual(cs._normalize_usage({"a": 1}), {"a": 1})


class ClaudeConfigDirTest(unittest.TestCase):
    def test_defaults_to_home_dot_claude(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(cs._claude_config_dir(), Path.home() / ".claude")

    def test_honours_env_var(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path("/tmp/profile-a"))}):
            self.assertEqual(cs._claude_config_dir(), Path("/tmp/profile-a"))

    def test_blank_value_treated_as_unset(self):
        """Path("") resolves to the process cwd — never what the user meant."""
        for blank in ("", "   ", "\t"):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": blank}):
                self.assertEqual(cs._claude_config_dir(), Path.home() / ".claude")


class PricingLookupTest(unittest.TestCase):
    def _resolve(self, model_id):
        """Mirror the longest-prefix resolution used by the cost scanner."""
        pricing = cs.API_PRICING.get(model_id)
        if pricing is None:
            best = None
            for key in cs.API_PRICING:
                if model_id.startswith(key) and (best is None or len(key) > len(best)):
                    best = key
            pricing = cs.API_PRICING[best] if best else None
        return pricing

    def test_opus_4_8_is_not_shadowed_by_opus_4(self):
        self.assertEqual(self._resolve("claude-opus-4-8")["input"], 5.0)
        self.assertEqual(self._resolve("claude-opus-4-8-20260101")["input"], 5.0)

    def test_legacy_opus_4_keeps_old_pricing(self):
        self.assertEqual(self._resolve("claude-opus-4")["input"], 15.0)

    def test_current_models_present(self):
        for model_id in ("claude-fable-5", "claude-opus-5", "claude-sonnet-5"):
            self.assertIn(model_id, cs.API_PRICING, model_id)
            self.assertIn(model_id, cs.API_PRICING_DISPLAY, model_id)

    def test_fable_is_priced_above_opus(self):
        self.assertGreater(
            cs.API_PRICING["claude-fable-5"]["input"],
            cs.API_PRICING["claude-opus-5"]["input"],
        )

    def test_context_windows_reflect_1m_models(self):
        self.assertEqual(cs.MODEL_CONTEXT_WINDOWS["Opus 5"], 1_000_000)
        self.assertEqual(cs.MODEL_CONTEXT_WINDOWS["Sonnet 5"], 1_000_000)
        self.assertEqual(cs.MODEL_CONTEXT_WINDOWS["Haiku 4.5"], 200_000)


class Claude55PricingTest(unittest.TestCase):
    """Fable 5.1 / Opus 5.5 / Sonnet 5.5 / Haiku 5.5 rates, as listed on
    https://platform.claude.com/docs/en/about-claude/pricing (per MTok)."""

    EXPECTED = {
        "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
        "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
        "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
        "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    }

    def test_rates_match_the_pricing_page(self):
        for model_id, (inp, out, cr, cw) in self.EXPECTED.items():
            p = cs._pricing_for(model_id)
            self.assertIsNotNone(p, model_id)
            self.assertEqual(
                (p["input"], p["output"], p["cache_read"], p["cache_write"]),
                (inp, out, cr, cw), model_id)

    def test_dated_ids_resolve_to_the_55_entry_not_the_older_prefix(self):
        """Longest prefix: claude-opus-5-5 must not fall back to claude-opus-5."""
        def resolve(model_id):
            best = None
            for key in cs.API_PRICING:
                if model_id.startswith(key) and (best is None or len(key) > len(best)):
                    best = key
            return best
        self.assertEqual(resolve("claude-opus-5-5"), "claude-opus-5-5")
        self.assertEqual(resolve("claude-opus-5-5-20261001"), "claude-opus-5-5")
        self.assertEqual(resolve("claude-opus-5-20260601"), "claude-opus-5")
        self.assertEqual(resolve("claude-sonnet-5-5"), "claude-sonnet-5-5")
        self.assertEqual(resolve("claude-fable-5-1"), "claude-fable-5-1")
        self.assertEqual(resolve("claude-haiku-5-5"), "claude-haiku-5-5")
        self.assertEqual(cs._pricing_for("claude-opus-5")["input"], 5.0)

    def test_short_names_and_display_names(self):
        for model_id, short in (("claude-fable-5-1", "Fable"), ("claude-opus-5-5", "Opus"),
                                ("claude-sonnet-5-5", "Sonnet"), ("claude-haiku-5-5", "Haiku"),
                                ("claude-opus-5-5-20261001", "Opus")):
            self.assertEqual(cs._model_short_name(model_id), short, model_id)
        self.assertEqual(cs.API_PRICING_DISPLAY["claude-opus-5-5"], "Opus 5.5")
        self.assertEqual(cs.API_PRICING_DISPLAY["claude-fable-5-1"], "Fable 5.1")

    def test_context_windows(self):
        for name in ("Fable 5.1", "Opus 5.5", "Sonnet 5.5", "Haiku 5.5"):
            self.assertEqual(cs.MODEL_CONTEXT_WINDOWS[name], 1_000_000, name)

    def test_haiku_long_context_rates_above_100k_prompt_tokens(self):
        base = cs._pricing_for("claude-haiku-5-5", prompt_tokens=100_000)
        long_ = cs._pricing_for("claude-haiku-5-5", prompt_tokens=100_001)
        self.assertEqual(base["input"], 0.10)
        self.assertEqual((long_["input"], long_["output"], long_["cache_read"],
                          long_["cache_write"]), (0.50, 2.50, 0.05, 0.625))
        # No prompt size given: standard rates.
        self.assertEqual(cs._pricing_for("claude-haiku-5-5")["input"], 0.10)

    def test_other_models_have_no_long_context_tier(self):
        for model_id in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"):
            self.assertEqual(cs._pricing_for(model_id, prompt_tokens=900_000),
                             cs._pricing_for(model_id), model_id)

    def test_junk_prompt_tokens_never_raise(self):
        for junk in (None, "x", [], {}, float("nan")):
            self.assertEqual(cs._pricing_for("claude-haiku-5-5", prompt_tokens=junk)["input"], 0.10)


class Claude52PricingTest(unittest.TestCase):
    """Fable 5.2, Mythos 5.2 and Mythos 5.1 (issue #61). Without rows of their
    own, the longest-prefix fallback priced them as Fable 5 / Mythos 5: $10 /
    $50 with $1 cache hits. Rates per MTok from
    https://platform.claude.com/docs/en/about-claude/pricing (read 2026-11-13).
    """

    # (input, output, cache hit, 5-minute cache write)
    EXPECTED = {
        "claude-fable-5-2": (8.0, 40.0, 0.20, 10.0),
        "claude-mythos-5-2": (8.0, 40.0, 0.20, 10.0),
        "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.5),
    }
    DISPLAY = {
        "claude-fable-5-2": "Fable 5.2",
        "claude-mythos-5-2": "Mythos 5.2",
        "claude-mythos-5-1": "Mythos 5.1",
    }

    def test_rates_match_the_pricing_page(self):
        for model_id, (inp, out, cr, cw) in self.EXPECTED.items():
            p = cs._pricing_for(model_id)
            self.assertIsNotNone(p, model_id)
            self.assertEqual(
                (p["input"], p["output"], p["cache_read"], p["cache_write"]),
                (inp, out, cr, cw), model_id)

    def test_older_fable_and_mythos_rows_are_unchanged(self):
        self.assertEqual(cs._pricing_for("claude-fable-5")["cache_read"], 1.0)
        self.assertEqual(cs._pricing_for("claude-mythos-5")["cache_read"], 1.0)
        self.assertEqual(cs._pricing_for("claude-fable-5-1")["input"], 10.0)

    def test_names_and_context_windows(self):
        for model_id, name in self.DISPLAY.items():
            self.assertEqual(cs.API_PRICING_DISPLAY.get(model_id), name, model_id)
            self.assertEqual(cs.MODEL_CONTEXT_WINDOWS.get(name), 1_000_000, name)
        self.assertEqual(cs._model_short_name("claude-fable-5-2"), "Fable")
        self.assertEqual(cs._model_short_name("claude-mythos-5-2"), "Mythos")
        self.assertEqual(cs._model_short_name("claude-mythos-5-1"), "Mythos")

    def _scan(self, entries):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sess = Path(td) / "projects" / "proj" / "session.jsonl"
            sess.parent.mkdir(parents=True)
            sess.write_text(
                "\n".join(_json.dumps(e) for e in entries), encoding="utf-8"
            )
            with mock.patch.object(cs, "_claude_config_dir", lambda: Path(td)):
                return cs._scan_session_costs()

    @staticmethod
    def _entry(model, n):
        """One response with a million tokens of every kind, so each rate
        shows up in the cost at face value."""
        return {
            "type": "assistant",
            "timestamp": "2026-11-12T12:00:00Z",
            "message": {"id": f"msg_{n}", "model": model, "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
                "cache_read_input_tokens": 1_000_000,
                "cache_creation_input_tokens": 2_000_000,
                "cache_creation": {"ephemeral_5m_input_tokens": 1_000_000,
                                   "ephemeral_1h_input_tokens": 1_000_000},
            }},
        }

    def test_transcript_lines_are_priced_at_their_own_rates(self):
        """The issue's acceptance test: a claude-fable-5-2 line is priced at
        $8 / $40 / $0.20 / $10, and the 1-hour write at 2x input ($16)."""
        result = self._scan([self._entry(m, i) for i, m in enumerate(self.EXPECTED)])
        for model_id, (inp, out, cr, cw) in self.EXPECTED.items():
            self.assertIn(model_id, result["models"], model_id)
            self.assertAlmostEqual(result["models"][model_id]["cost_usd"],
                                   inp + out + cr + cw + 2 * inp, msg=model_id)
        # Nothing fell through to the older family rows.
        self.assertNotIn("claude-fable-5", result["models"])
        self.assertNotIn("claude-mythos-5", result["models"])

    def test_suffixed_id_resolves_to_the_52_row(self):
        result = self._scan([self._entry("claude-fable-5-2-20261109", 0)])
        self.assertEqual(list(result["models"]), ["claude-fable-5-2"])
        self.assertAlmostEqual(result["models"]["claude-fable-5-2"]["cost_usd"],
                               8 + 40 + 0.20 + 10 + 16)


class TranscriptTimestampTest(unittest.TestCase):
    def test_z_suffix_parsed(self):
        """Claude Code writes Z-suffixed stamps; fromisoformat only accepted
        them from 3.11, and this project supports 3.8."""
        self.assertIsNotNone(cs._parse_transcript_ts("2026-07-18T15:01:07.532Z"))

    def test_explicit_offset_parsed(self):
        self.assertIsNotNone(cs._parse_transcript_ts("2026-07-18T15:01:07+00:00"))

    def test_naive_treated_as_utc(self):
        self.assertIsNotNone(cs._parse_transcript_ts("2026-07-18T15:01:07"))

    def test_garbage_returns_none(self):
        for bad in ("nope", "", None, 7, [], "2026-13-45T99:99:99Z"):
            self.assertIsNone(cs._parse_transcript_ts(bad), repr(bad))


# The real promo table is empty since Sonnet 5's launch rate became the
# standard price, so promo-mechanism tests inject a synthetic entry. The
# numbers deliberately match no real model's rates.
_SYNTHETIC_PROMO = {
    "claude-sonnet-5": {
        "until": "2026-08-31",
        "pricing": {"input": 1.5, "output": 7.5,
                    "cache_read": 0.15, "cache_write": 1.875},
    },
}


class PromoPricingTest(unittest.TestCase):
    """Promotional rates expire on a date, so they live outside API_PRICING."""

    def test_promo_rate_applies_through_the_cutoff(self):
        from datetime import date
        with mock.patch.dict(cs.API_PRICING_PROMOS, _SYNTHETIC_PROMO, clear=True):
            self.assertEqual(cs._pricing_for("claude-sonnet-5", date(2026, 7, 1))["input"], 1.5)
            self.assertEqual(cs._pricing_for("claude-sonnet-5", date(2026, 8, 31))["input"], 1.5)

    def test_reverts_to_list_price_after_the_cutoff(self):
        from datetime import date
        with mock.patch.dict(cs.API_PRICING_PROMOS, _SYNTHETIC_PROMO, clear=True):
            self.assertEqual(cs._pricing_for("claude-sonnet-5", date(2026, 9, 1))["input"], 2.0)

    def test_models_without_a_promo_use_the_table(self):
        from datetime import date
        with mock.patch.dict(cs.API_PRICING_PROMOS, _SYNTHETIC_PROMO, clear=True):
            self.assertEqual(cs._pricing_for("claude-opus-5", date(2026, 7, 1))["input"], 5.0)

    def test_sonnet_5_launch_rate_is_the_standard_price(self):
        """Anthropic cancelled the scheduled 2026-09-01 increase to $3/$15;
        $2/$10 is the list price on any date, with no promo entry involved."""
        from datetime import date
        self.assertEqual(cs.API_PRICING_PROMOS, {})
        self.assertEqual(cs._pricing_for("claude-sonnet-5", date(2026, 8, 15))["input"], 2.0)
        self.assertEqual(cs._pricing_for("claude-sonnet-5", date(2026, 9, 15))["input"], 2.0)
        self.assertEqual(cs._pricing_for("claude-sonnet-5", date(2026, 9, 15))["output"], 10.0)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(cs._pricing_for("not-a-model"))

    def test_mythos_preview_is_priced_and_sized(self):
        # $25/$125 was the real Glasswing-preview rate; transcripts naming
        # this id date from that window, so the historical price must stay.
        self.assertEqual(cs._pricing_for("claude-mythos-preview")["input"], 25.0)
        self.assertEqual(cs.MODEL_CONTEXT_WINDOWS["Mythos Preview"], 1_000_000)


class ScanPricesEntriesByTheirOwnDateTest(unittest.TestCase):
    """The cost scanner must price each transcript entry at the rates that
    were in force when the entry was made, not on the day the scan runs.

    Otherwise, once a promo period lapses, every promo-era turn gets
    retroactively repriced at the list rate — the cumulative widget's
    history would silently inflate overnight. Exercised with the synthetic
    promo above, since the real promo table is currently empty.
    """

    class _FrozenDatetime(cs.datetime):
        """datetime whose now() sits after the synthetic promo cutoff."""
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 1, 12, 0, 0, tzinfo=tz)

    def _scan(self, entries, since_ts=None):
        import json as _json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sess = Path(td) / "projects" / "proj" / "session.jsonl"
            sess.parent.mkdir(parents=True)
            sess.write_text(
                "\n".join(_json.dumps(e) for e in entries), encoding="utf-8"
            )
            with mock.patch.object(cs, "datetime", self._FrozenDatetime), \
                    mock.patch.object(cs, "_claude_config_dir", lambda: Path(td)), \
                    mock.patch.dict(cs.API_PRICING_PROMOS, _SYNTHETIC_PROMO,
                                    clear=True):
                return cs._scan_session_costs(since_ts=since_ts)

    @staticmethod
    def _entry(model, ts, input_tokens=1_000_000):
        e = {
            "type": "assistant",
            "message": {"model": model, "usage": {"input_tokens": input_tokens,
                                                  "output_tokens": 0}},
        }
        if ts is not None:
            e["timestamp"] = ts
        return e

    def test_promo_era_entries_keep_promo_pricing_after_the_cutoff(self):
        result = self._scan([
            # In the (synthetic) promo window: $1.50/MTok even though "now"
            # is October.
            self._entry("claude-sonnet-5", "2026-08-15T12:00:00Z"),
            # After the cutoff: list price $2/MTok.
            self._entry("claude-sonnet-5", "2026-09-15T12:00:00Z"),
            # Promo must also survive the longest-prefix fallback resolution.
            self._entry("claude-sonnet-5-20260601", "2026-08-20T12:00:00Z"),
        ])
        self.assertAlmostEqual(
            result["models"]["claude-sonnet-5"]["cost_usd"], 1.5 + 2.0 + 1.5
        )

    def test_haiku_55_is_priced_per_request_by_prompt_size(self):
        """Prompt = input + cache read + cache write; over 100K the whole
        request (cache hits and output included) takes the higher rates."""
        def entry(**usage):
            return {"type": "assistant", "timestamp": "2026-10-01T12:00:00Z",
                    "message": {"model": "claude-haiku-5-5", "usage": usage}}
        result = self._scan([
            # 50K prompt: standard. 50K*0.10 + 10K*0.50 = 0.005 + 0.005
            entry(input_tokens=50_000, output_tokens=10_000),
            # 20K input + 90K cache read = 110K > 100K: surcharged.
            # 20K*0.50 + 90K*0.05 + 10K*2.50 = 0.01 + 0.0045 + 0.025
            entry(input_tokens=20_000, cache_read_input_tokens=90_000,
                  output_tokens=10_000),
        ])
        self.assertAlmostEqual(
            result["models"]["claude-haiku-5-5"]["cost_usd"], 0.010 + 0.0395)

    def test_undated_entries_still_price_at_the_scan_day(self):
        """No timestamp means no better information — the scan-day default
        stands, which after the cutoff is the list price."""
        result = self._scan([self._entry("claude-sonnet-5", None)])
        self.assertAlmostEqual(result["models"]["claude-sonnet-5"]["cost_usd"], 2.0)


class NullSessionWindowTest(unittest.TestCase):
    """An account without a 5-hour window gets ``five_hour: null`` from the
    usage API. The state writers that run after the render must not raise on
    it (they did, and the bar went blank), and the bar must not draw a 0%
    session limit the account does not have."""

    WEEKLY_ONLY = {"five_hour": None, "seven_day": {"utilization": 10, "resets_at": None}}

    def test_state_writers_accept_a_null_window(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td,                 mock.patch.dict(os.environ, {"LOCALAPPDATA": td, "XDG_CACHE_HOME": td,
                                             "CLAUDE_CONFIG_DIR": td}):
            cs._append_history(dict(self.WEEKLY_ONLY))
            cs._update_heatmap(dict(self.WEEKLY_ONLY))
            cs._update_session_state(dict(self.WEEKLY_ONLY), {"model_name": "Opus"})
            samples = cs._read_history()
            sessions = cs._get_active_sessions()
        self.assertEqual((samples[-1]["s"], samples[-1]["w"]), (0, 10))
        self.assertTrue(sessions is not None)

    def test_only_a_weekly_window_draws_no_session_placeholder(self):
        cfg = {"show": {"session": True, "weekly": True}}
        line = cs.build_status_line(dict(self.WEEKLY_ONLY), "Max 20x", cfg)
        self.assertNotIn("Session", line)
        self.assertIn("Weekly", line)
        self.assertIn("10%", line)
        # No data at all: the empty placeholder stays, as before.
        self.assertIn("Session", cs.build_status_line({}, "", cfg))
        # Both windows null: still the placeholder, never an exception.
        self.assertIn("Session", cs.build_status_line({"five_hour": None, "seven_day": None}, "", cfg))


class StaleCacheShapeTest(unittest.TestCase):
    """_read_stale_cache feeds the 429 fallback; raising there prints nothing."""

    def test_wrong_shaped_cache_returns_none(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            for junk in ("42", '"s"', "[1,2]", "null", "true"):
                p.write_text(junk, encoding="utf-8")
                self.assertIsNone(cs._read_stale_cache(p), junk)

    def test_valid_cache_still_returned(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.json"
            p.write_text(json.dumps({"line": "x", "timestamp": 1}), encoding="utf-8")
            self.assertIsNotNone(cs._read_stale_cache(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
