"""Tests for the 3.4.0 stdin widgets: cache_warm, spend_limit, repo, session_name.

Each widget is rendered through the real ``build_status_line`` in every layout,
and the full path through ``main()`` pins that the new stdin fields survive the
persistence layer (``_STDIN_CTX_KEYS``) between repaints.

Run: ``python tests/test_stdin_widgets.py``
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from terminal_fixtures import host_terminal_width  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LAYOUTS = ("standard", "compact", "minimal", "percent-first")


def render(show, stdin_ctx, layout="standard", **cfg_extra):
    """Render the status line in a sandbox and return it without colour codes."""
    base = {k: False for k in ("session", "weekly", "context", "branch", "model", "streak",
                               "update", "claude_update", "effort", "pomodoro")}
    cfg = {"show": dict(base, **show), "layout": layout}
    cfg.update(cfg_extra)
    with tempfile.TemporaryDirectory() as td:
        # The state dir lives under LOCALAPPDATA / XDG_CACHE_HOME, not
        # CLAUDE_CONFIG_DIR; all three must point at the sandbox.
        env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td}
        with mock.patch.dict(os.environ, env):
            return ANSI.sub("", cs.build_status_line({}, "", cfg, stdin_ctx))


def spend(**over):
    base = {"used_percentage": 25.0,
            "resets_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
            "used_usd": 12.4, "limit_usd": 50.0, "period": "weekly"}
    base.update(over)
    return {"spend_limit": base}


class CacheWarmWidgetTest(unittest.TestCase):
    def _warm(self, **over):
        ctx = {"prompt_cache_warm": True, "prompt_cache_hit_pct": 92.0,
               "prompt_cache_expires_at": time.time() + 180}
        ctx.update(over)
        return ctx

    def test_off_by_default(self):
        self.assertNotIn("♨", render({}, self._warm()))
        self.assertFalse(cs.DEFAULT_SHOW["cache_warm"])

    def test_warm_standard_shows_hit_and_time_left(self):
        line = render({"cache_warm": True}, self._warm())
        self.assertIn("♨ 92% · 3m", line)

    def test_every_layout_renders_it(self):
        for layout in LAYOUTS:
            line = render({"cache_warm": True}, self._warm(), layout)
            self.assertIn("♨", line, layout)
            self.assertIn("92%", line, layout)

    def test_minimal_is_shortened(self):
        line = render({"cache_warm": True}, self._warm(), "minimal")
        self.assertIn("♨92%", line)
        self.assertNotIn("3m", line)

    def test_cold_cache(self):
        for layout in LAYOUTS:
            line = render({"cache_warm": True},
                          {"prompt_cache_warm": False, "prompt_cache_hit_pct": 80.0}, layout)
            self.assertIn("cache cold", line, layout)

    def test_expired_expiry_counts_as_cold_even_if_persisted_as_warm(self):
        line = render({"cache_warm": True}, self._warm(prompt_cache_expires_at=time.time() - 5))
        self.assertIn("cache cold", line)
        self.assertNotIn("♨", line)

    def test_unknown_state_renders_nothing(self):
        for ctx in ({}, {"prompt_cache_warm": None}, {"prompt_cache_hit_pct": 50.0}):
            self.assertEqual(render({"cache_warm": True}, ctx).strip(), "", repr(ctx))

    def test_missing_hit_ratio_still_shows_the_countdown(self):
        line = render({"cache_warm": True}, self._warm(prompt_cache_hit_pct=None))
        self.assertIn("♨", line)
        self.assertIn("3m", line)
        self.assertNotIn("%", line)

    def test_last_minute_counts_in_seconds(self):
        line = render({"cache_warm": True}, self._warm(prompt_cache_expires_at=time.time() + 30))
        self.assertRegex(line, r"· (29|30)s")

    def test_garbage_values_never_raise(self):
        for bad in ("x", [], {}, True, float("nan")):
            render({"cache_warm": True},
                   {"prompt_cache_warm": True, "prompt_cache_hit_pct": bad,
                    "prompt_cache_expires_at": bad})

    def test_ttl_formatter(self):
        self.assertEqual(cs._format_cache_ttl(30), "30s")
        self.assertEqual(cs._format_cache_ttl(179.5), "3m")
        self.assertEqual(cs._format_cache_ttl(3599), "1h")
        self.assertEqual(cs._format_cache_ttl(5400), "1h30m")


class SpendLimitWidgetTest(unittest.TestCase):
    def test_on_by_default(self):
        self.assertTrue(cs.DEFAULT_SHOW["spend_limit"])
        self.assertIn("spend", render({}, spend()))

    def test_standard_money_and_period(self):
        self.assertIn("spend $12.40/$50 wk", render({}, spend()))

    def test_every_layout_renders_it(self):
        expect = {"standard": "spend $12.40/$50 wk", "percent-first": "spend $12.40/$50 wk",
                  "compact": "$12.40/$50 wk", "minimal": "$12.40/$50"}
        for layout in LAYOUTS:
            line = render({}, spend(), layout)
            self.assertIn(expect[layout], line, layout)
        self.assertNotIn("spend", render({}, spend(), "compact"))
        self.assertNotIn("wk", render({}, spend(), "minimal"))

    def test_period_abbreviations(self):
        for period, short in (("daily", "day"), ("weekly", "wk"), ("monthly", "mo")):
            self.assertIn(f"/$50 {short}", render({}, spend(period=period)), period)

    def test_missing_period_is_omitted_and_unknown_period_passes_through(self):
        self.assertTrue(render({}, spend(period=None)).strip().endswith("$12.40/$50"))
        self.assertIn("/$50 yearly", render({}, spend(period="yearly")))

    def test_percentage_fallback_without_usd_fields(self):
        for layout in LAYOUTS:
            line = render({}, spend(used_usd=None, limit_usd=None), layout)
            self.assertIn("25%", line, layout)
            self.assertNotIn("$", line, layout)
        self.assertIn("spend 25% wk", render({}, spend(used_usd=None, limit_usd=None)))

    def test_fractional_limit_keeps_cents(self):
        self.assertIn("$12.40/$49.50", render({}, spend(limit_usd=49.5)))

    def test_colour_follows_the_usage_thresholds(self):
        theme = cs.get_theme_colours("default")
        cfg = {"show": {"branch": False, "model": False}}
        with tempfile.TemporaryDirectory() as td:
            env = {"CLAUDE_CONFIG_DIR": td, "LOCALAPPDATA": td, "XDG_CACHE_HOME": td}
            with mock.patch.dict(os.environ, env):
                for pct, key in ((10, "low"), (60, "mid"), (95, "high")):
                    raw = cs.build_status_line({}, "", cfg, spend(used_percentage=float(pct)))
                    self.assertIn(theme[key] + "$12.40/$50", raw, pct)

    def test_hidden_when_switched_off_or_absent(self):
        self.assertNotIn("spend", render({"spend_limit": False}, spend()))
        self.assertEqual(render({}, {"spend_limit": None}).strip(), "")
        self.assertEqual(render({}, {}).strip(), "")

    def test_hidden_once_the_period_has_reset(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertEqual(render({}, spend(resets_at=past)).strip(), "")

    def test_currency_conversion(self):
        with mock.patch.object(cs, "_get_exchange_rate", return_value=(0.8, "GBP")):
            line = render({}, spend(), currency="£")
        self.assertIn("£9.92/£40 wk", line)

    def test_garbage_never_raises(self):
        for bad in ("x", [], 5, {"used_percentage": "x"}, {"used_percentage": None},
                    spend(used_usd="x")["spend_limit"], spend(resets_at="nope")["spend_limit"]):
            render({}, {"spend_limit": bad})


class RepoAndSessionNameWidgetTest(unittest.TestCase):
    def test_opt_in(self):
        self.assertFalse(cs.DEFAULT_SHOW["repo"])
        self.assertFalse(cs.DEFAULT_SHOW["session_name"])
        ctx = {"repo_slug": "anthropics/claude-code", "session_name": "refactor auth"}
        self.assertEqual(render({}, ctx).strip(), "")

    def test_repo_renders_in_every_layout(self):
        for layout in LAYOUTS:
            line = render({"repo": True}, {"repo_slug": "anthropics/claude-code"}, layout)
            self.assertIn("anthropics/claude-code", line, layout)

    def test_session_name_renders_in_every_layout(self):
        for layout in LAYOUTS:
            line = render({"session_name": True}, {"session_name": "refactor auth"}, layout)
            self.assertIn("refactor auth", line, layout)

    def test_absent_or_wrong_type_renders_nothing(self):
        for bad in (None, "", 5, [], {}):
            self.assertEqual(
                render({"repo": True, "session_name": True},
                       {"repo_slug": bad, "session_name": bad}).strip(), "", repr(bad))

    def test_persisted_text_is_resanitised_at_render(self):
        line = render({"repo": True}, {"repo_slug": "a/b\x1b]0;pwn\x07"})
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)


class PriorityTest(unittest.TestCase):
    def test_ids_and_priorities_registered_at_the_end(self):
        self.assertEqual(
            {k: cs.WIDGET_PRIORITY[k] for k in ("cache_warm", "spend_limit", "repo", "session_name")},
            {"cache_warm": 66, "spend_limit": 52, "repo": 172, "session_name": 105})
        # The providers switch (no priority: it is rows, not a widget) comes last.
        self.assertEqual(list(cs.DEFAULT_SHOW)[-5:],
                         ["cache_warm", "spend_limit", "repo", "session_name", "providers"])
        self.assertEqual(list(cs.WIDGET_PRIORITY)[-4:],
                         ["cache_warm", "spend_limit", "repo", "session_name"])

    def test_order_on_the_line(self):
        ctx = {"prompt_cache_warm": True, "prompt_cache_hit_pct": 90.0,
               "prompt_cache_expires_at": time.time() + 600,
               "repo_slug": "o/r", "session_name": "sess"}
        ctx.update(spend())
        line = render({"cache_warm": True, "repo": True, "session_name": True,
                       "cache": False}, ctx)
        order = [line.index(t) for t in ("spend", "♨", "sess", "o/r")]
        self.assertEqual(order, sorted(order))


class PersistenceThroughMainTest(unittest.TestCase):
    """The new stdin fields must reach the next repaint via stdin_ctx.json."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name) / "state"
        self.state.mkdir()
        cfg_dir = Path(self._td.name) / "claude"
        cfg_dir.mkdir()
        for target, value in (("get_state_dir", lambda: self.state),
                              ("_claude_config_dir", lambda: cfg_dir),
                              # main() fits the line to the terminal it finds; pin
                              # "no terminal" so the contributor's window cannot cut it.
                              ("_detect_terminal_width", lambda: None),
                              ("check_for_update", lambda: False),
                              ("check_claude_code_update", lambda: False),
                              ("get_credentials", lambda: ("token", "Max 20x")),
                              # Fragment repaints carry no rate limits, so the
                              # usage API would be hit; answer from memory.
                              ("fetch_usage", lambda token, timeout=10: {
                                  "five_hour": {"utilization": 1, "resets_at": None},
                                  "seven_day": {"utilization": 2, "resets_at": None}})):
            patcher = mock.patch.object(cs, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        show = dict(cs.DEFAULT_SHOW, cache_warm=True, repo=True, session_name=True,
                    branch=False, model=False)
        cfg = {"show": show, "layout": "standard"}
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

    def _full(self, **extra):
        payload = {"session_id": "s1", "model": {"id": "claude-opus-5-5"},
                   "workspace": {"current_dir": "."}}
        payload.update(extra)
        return payload

    def test_keys_are_in_the_persisted_set_and_survive_a_fragment(self):
        now = time.time()
        full = self._full(
            session_name="refactor auth",
            workspace={"current_dir": ".", "repo": {"host": "github.com", "owner": "o", "name": "r"}},
            prompt_cache={"warm": True, "hit_ratio": 0.92, "expires_at": now + 180},
            rate_limits={"spend_limit": {"used_percentage": 25, "resets_at": now + 86400,
                                         "used_usd": 12.4, "limit_usd": 50, "period": "weekly"}})
        line = self._repaint(full)
        for text in ("refactor auth", "o/r", "♨ 92%", "spend $12.40/$50 wk"):
            self.assertIn(text, line)

        # A config dir other than ~/.claude keeps its state under accounts/<key>.
        persisted = json.loads((cs.get_account_state_dir() / "stdin_ctx.json").read_text(encoding="utf-8"))
        for key in ("prompt_cache_warm", "prompt_cache_hit_pct", "prompt_cache_expires_at",
                    "spend_limit", "repo_slug", "session_name"):
            self.assertIn(key, persisted)

        # A fragment (no session identity) repaint keeps all of it.
        line = self._repaint({"cost": {"total_cost_usd": 0.5}})
        for text in ("refactor auth", "o/r", "♨ 92%", "spend $12.40/$50 wk"):
            self.assertIn(text, line)

    def test_badges_draw_whatever_width_the_hosts_terminal_has(self):
        """main() fits the line to the terminal it finds; the fake stdout has
        no fileno, so a 140-column window used to drop the last badge."""
        now = time.time()
        full = self._full(
            session_name="refactor auth",
            workspace={"current_dir": ".", "repo": {"host": "github.com", "owner": "o", "name": "r"}},
            prompt_cache={"warm": True, "hit_ratio": 0.92, "expires_at": now + 180},
            rate_limits={"spend_limit": {"used_percentage": 25, "resets_at": now + 86400,
                                         "used_usd": 12.4, "limit_usd": 50, "period": "weekly"}})
        for columns in (80, 140):
            with self.subTest(columns=columns):
                with host_terminal_width(columns):
                    line = self._repaint(full)
                for text in ("refactor auth", "o/r", "♨ 92%", "spend $12.40/$50 wk"):
                    self.assertIn(text, line, "fitted to a %d-column host terminal" % columns)

    def test_a_full_payload_without_the_fields_clears_the_badges(self):
        now = time.time()
        self._repaint(self._full(
            session_name="refactor auth",
            prompt_cache={"warm": True, "hit_ratio": 0.92, "expires_at": now + 180}))
        line = self._repaint(self._full())
        self.assertNotIn("refactor auth", line)
        self.assertNotIn("♨", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
