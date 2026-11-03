"""Tests for the optional MiniMax provider extension (PR #43).

When Claude Code is pointed at MiniMax's Anthropic-compatible endpoint via
``mxclaude``, four stdin fields don't describe the session correctly. The
provider block in ``_parse_stdin_context`` corrects them, gated on the
``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_MODEL`` env vars.

These tests pin three things:

* the env gate (the block must be inert for normal Anthropic sessions);
* the corrections (context %, cost, token counts, rate-limit reshape);
* robustness — malformed stdin or quota JSON must never raise, because a
  status line that throws crashes Claude Code's status bar.

Run: ``python tests/test_minimax_provider.py``
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import claude_status as cs  # noqa: E402
from provider_fixtures import FakeMmx, ProviderSandbox, mmx_quota  # noqa: E402


MINIMAX_ENV = {
    "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    "ANTHROPIC_MODEL": "MiniMax-M3",
}


def _stdin(input_tokens, output_tokens, used_percentage=12.0, cost=9.99):
    """Build a Claude Code stdin JSON blob with a context_window + cost."""
    return json.dumps({
        "context_window": {
            "used_percentage": used_percentage,
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "context_window_size": 200_000,  # the shim's wrong value
        },
        "cost": {"total_cost_usd": cost},
    })


class QuotaParserTest(unittest.TestCase):
    def test_happy_path_maps_both_windows(self):
        out = cs._parse_minimax_quota({
            "model_remains": [{
                "current_interval_status": 1,
                "current_interval_remaining_percent": 19.0,   # → 81% used
                "current_weekly_remaining_percent": 87.0,     # → 13% used
                "end_time": 1_900_000_000_000,                # ms epoch
                "weekly_end_time": 1_900_500_000_000,
            }],
        })
        self.assertAlmostEqual(out["five_hour"]["utilization"], 81.0)
        self.assertAlmostEqual(out["seven_day"]["utilization"], 13.0)
        self.assertTrue(out["five_hour"]["resets_at"].startswith("20"))

    def test_skips_unused_bucket_status_3(self):
        out = cs._parse_minimax_quota({
            "model_remains": [{
                "current_interval_status": 3,  # un-used this period → skip
                "current_interval_remaining_percent": 50.0,
                "end_time": 1_900_000_000_000,
            }],
        })
        self.assertEqual(out, {})

    def test_malformed_values_do_not_raise(self):
        # Non-numeric percent / bad timestamp: skip the bucket, never throw.
        out = cs._parse_minimax_quota({
            "model_remains": [{
                "current_interval_status": 1,
                "current_interval_remaining_percent": "not-a-number",
                "end_time": "garbage",
                "current_weekly_remaining_percent": 90.0,
                "weekly_end_time": 1_900_000_000_000,
            }],
        })
        # five_hour dropped (bad data), seven_day still parsed.
        self.assertNotIn("five_hour", out)
        self.assertAlmostEqual(out["seven_day"]["utilization"], 10.0)

    def test_non_dict_input_returns_empty(self):
        self.assertEqual(cs._parse_minimax_quota([]), {})
        self.assertEqual(cs._parse_minimax_quota("nope"), {})


class StdinGateTest(unittest.TestCase):
    def setUp(self):
        # The quota now goes through the shared 120 s provider cache, which
        # lives in the state dir: keep it (and HOME) in a throwaway sandbox.
        self.sb = ProviderSandbox(self)
        # And never start a real mmx on a machine that has one installed.
        p = mock.patch.object(cs.shutil, "which", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_inactive_without_minimax_env(self):
        # No MiniMax env → block is inert, Anthropic values pass through.
        with mock.patch.dict("os.environ", {}, clear=True):
            res = cs._parse_stdin_context(_stdin(100_000, 50_000, used_percentage=12.0))
        self.assertAlmostEqual(res["context_pct"], 12.0)   # unchanged
        self.assertEqual(res["context_limit"], 200_000)     # unchanged

    def test_recomputes_context_cost_and_limit(self):
        with mock.patch.dict("os.environ", MINIMAX_ENV, clear=True):
            res = cs._parse_stdin_context(_stdin(100_000, 50_000, used_percentage=12.0, cost=9.99))
        # 150k / 1M = 15%, not the shim's 12% and not against 200k.
        self.assertAlmostEqual(res["context_pct"], 15.0)
        self.assertEqual(res["context_used"], 150_000)
        self.assertEqual(res["context_limit"], 1_000_000)   # real M3 window
        # tier1 (≤512k): 100k*0.60/1M + 50k*2.40/1M = 0.06 + 0.12 = 0.18
        self.assertAlmostEqual(res["cost_usd"], 0.18)
        self.assertNotAlmostEqual(res["cost_usd"], 9.99)    # not Anthropic price

    def test_tier2_pricing_above_512k(self):
        with mock.patch.dict("os.environ", MINIMAX_ENV, clear=True):
            res = cs._parse_stdin_context(_stdin(600_000, 100_000))
        # total 700k > 512k → tier2: 600k*1.20/1M + 100k*4.80/1M = 0.72 + 0.48
        self.assertAlmostEqual(res["cost_usd"], 1.20)

    def test_malformed_tokens_do_not_raise(self):
        bad = json.dumps({"context_window": {"total_input_tokens": "oops"},
                          "cost": {"total_cost_usd": 1.0}})
        with mock.patch.dict("os.environ", MINIMAX_ENV, clear=True):
            res = cs._parse_stdin_context(bad)  # must not raise
        self.assertIsInstance(res, dict)

    def test_quota_subprocess_populates_rate_limits(self):
        # mmx_quota(): 20 % of the 5-hour and 80 % of the weekly window left.
        fake = FakeMmx(mmx_quota(left5=20.0, leftw=80.0))
        with mock.patch.dict("os.environ", MINIMAX_ENV), \
                mock.patch.object(cs.shutil, "which", return_value="/bin/mmx"), \
                mock.patch.object(cs.subprocess, "run", fake):
            res = cs._parse_stdin_context(_stdin(10, 10))
        self.assertAlmostEqual(res["_rate_limits"]["five_hour"]["utilization"], 80.0)
        self.assertAlmostEqual(res["_rate_limits"]["seven_day"]["utilization"], 20.0)

    def test_missing_mmx_binary_is_silent(self):
        with mock.patch.dict("os.environ", MINIMAX_ENV), \
                mock.patch.object(cs.shutil, "which", return_value="/bin/mmx"), \
                mock.patch.object(cs.subprocess, "run", side_effect=FileNotFoundError):
            res = cs._parse_stdin_context(_stdin(10, 10))  # must not raise
        self.assertNotIn("_rate_limits", res)

    def test_mmx_binary_resolved_via_path_lookup(self):
        """PR #50: npm installs mmx as a .cmd shim on Windows, which
        subprocess.run will not auto-resolve (it only finds .exe). The call
        must go through shutil.which like _GIT_PATH / _CLAUDE_PATH."""
        fake = FakeMmx(rc=1)
        with mock.patch.dict("os.environ", MINIMAX_ENV), \
                mock.patch.object(cs.shutil, "which",
                                  return_value=r"C:\fake\mmx.CMD") as which, \
                mock.patch.object(cs.subprocess, "run", fake):
            cs._parse_stdin_context(_stdin(10, 10))
        which.assert_any_call("mmx")
        self.assertEqual(fake.calls[0][0][0], r"C:\fake\mmx.CMD")

    def test_no_mmx_on_path_starts_no_process(self):
        """No mmx on PATH: nothing is started at all (it used to spawn the bare
        name on every repaint just to catch the FileNotFoundError)."""
        fake = FakeMmx(rc=1)
        with mock.patch.dict("os.environ", MINIMAX_ENV), \
                mock.patch.object(cs.shutil, "which", return_value=None), \
                mock.patch.object(cs.subprocess, "run", fake):
            res = cs._parse_stdin_context(_stdin(10, 10))
        self.assertEqual(fake.calls, [])
        self.assertNotIn("_rate_limits", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
