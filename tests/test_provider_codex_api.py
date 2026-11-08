"""The Codex row from the ChatGPT usage API: token safety, gates, fallback.

The rules pinned here:

* the token is read from ``~/.codex/auth.json``, sent to chatgpt.com only,
  with every redirect refused, and never stored, logged or refreshed;
* an expired JWT, an API-key login or a missing file means no request;
* at most one request per ``providers_refresh_seconds`` and one *attempt*
  per 60 s whatever the outcome (stamped before the request); 429 / 401 /
  other errors back off and keep the old value; one network call per repaint;
* the API's numbers win while fresh, or whenever they are newer than the
  rollout's; otherwise the rollout stays the source; a Codex row older than
  30 minutes is drawn stale;
* a ChatGPT login with no rollout yet is detected.

Run: ``python -m unittest tests.test_provider_codex_api``
"""
import base64
import io
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, codex_line, cs  # noqa: E402


def _jwt(exp, plan="plus"):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    claims = {"exp": exp, "https://api.openai.com/auth": {
        "chatgpt_plan_type": plan, "chatgpt_account_id": "acc"}}
    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.signature-secret"


def _payload(used5=31.0, usedw=7.0, plan="plus", now=None, reset_at=True):
    now = time.time() if now is None else now
    p = {"used_percent": used5, "limit_window_seconds": 18000, "reset_after_seconds": 3600}
    s = {"used_percent": usedw, "limit_window_seconds": 604800, "reset_after_seconds": 86400 * 3}
    if reset_at:
        p["reset_at"] = int(now + 3600)
        s["reset_at"] = int(now + 86400 * 3)
    return {"plan_type": plan, "credits": {"has_credits": False},
            "rate_limit": {"allowed": True, "limit_reached": False,
                           "primary_window": p, "secondary_window": s}}


def _resp(payload):
    m = mock.MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return m


def _http_error(code, retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after else {}

    class _H(dict):
        def get(self, k, d=None):
            return dict.get(self, k.lower(), d)
    return urllib.error.HTTPError(cs._CODEX_USAGE_URL, code, "err", _H(headers), io.BytesIO(b""))


class CodexApiTest(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.codex = self.sb.home / ".codex"
        self.codex.mkdir()
        self.entry = {"id": "codex", "type": "codex", "enabled": True}
        self.config = {"providers": [dict(self.entry)], "providers_refresh_seconds": 300}
        for name in ("_refresh_oauth_token", "refresh_and_retry"):
            p = mock.patch.object(cs, name, side_effect=AssertionError(f"{name} called"))
            p.start()
            self.addCleanup(p.stop)
        # The turn-taking between optional network checks is upstream's and
        # keyed on wall-clock time; these tests step a fake clock, so it is
        # pinned open here and covered on its own below.
        for name, value in (("_begin_net_attempt", lambda key, backoff=None, primary=False: True),
                            ("_end_net_attempt", lambda key: None)):
            p = mock.patch.object(cs, name, value)
            p.start()
            self.addCleanup(p.stop)

    def write_auth(self, token, account_id="acct-1", home=None):
        home = home or self.codex
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"access_token": token, "account_id": account_id,
                       "refresh_token": "refresh-secret", "id_token": "id-secret"},
            "last_refresh": "2026-11-04T03:20:26Z"}), encoding="utf-8")

    def _cache_file(self):
        return cs._provider_cache_path("codex:api")

    # --- token file ---------------------------------------------------------
    def test_token_file_rules(self):
        now = time.time()
        self.write_auth(_jwt(now + 3600))
        self.assertEqual(cs._read_codex_token(self.codex, now),
                         (_jwt(now + 3600), "acct-1", "plus"))
        self.write_auth(_jwt(now + 30))                      # inside the 60 s margin
        self.assertEqual(cs._read_codex_token(self.codex, now), (None, None, None))
        self.write_auth("opaque-token")                     # no JWT: the server judges it
        self.assertEqual(cs._read_codex_token(self.codex, now), ("opaque-token", "acct-1", None))
        self.write_auth(_jwt(now + 3600), account_id=None)
        self.assertEqual(cs._read_codex_token(self.codex, now)[1], None)
        (self.codex / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-x", "tokens": None}))
        self.assertEqual(cs._read_codex_token(self.codex, now), (None, None, None))
        (self.codex / "auth.json").write_text("{not json")
        self.assertEqual(cs._read_codex_token(self.codex, now), (None, None, None))
        self.assertEqual(cs._read_codex_token(self.sb.home / "nowhere", now), (None, None, None))

    # --- the request ----------------------------------------------------------
    def test_fetch_maps_windows_and_sends_the_right_headers(self):
        now = time.time()
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload(now=now))) as op:
            usage, tier = cs._fetch_codex_usage("tok-secret", "acct-1", now)
        self.assertEqual(usage["five_hour"]["utilization"], 31.0)
        self.assertEqual(usage["seven_day"]["utilization"], 7.0)
        self.assertTrue(usage["five_hour"]["resets_at"].startswith("20"))
        self.assertEqual(tier, "Plus")
        req = op.call_args[0][0]
        self.assertIsInstance(req, urllib.request.Request)
        self.assertEqual(req.full_url, "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok-secret")
        self.assertEqual(req.get_header("Chatgpt-account-id"), "acct-1")
        self.assertEqual(op.call_args[1].get("timeout"), cs._PROVIDER_TIMEOUT)

    def test_fetch_falls_back_to_reset_after_seconds(self):
        now = time.time()
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload(now=now, reset_at=False))):
            usage, _ = cs._fetch_codex_usage("tok", None, now)
        reset = cs._parse_reset(usage["five_hour"]["resets_at"]).timestamp()
        self.assertAlmostEqual(reset, now + 3600, delta=2)

    def test_fetch_rejects_other_payloads(self):
        for bad in ({"foo": 1}, [], {"rate_limit": "x"}):
            with mock.patch.object(cs._codex_opener, "open", return_value=_resp(bad)):
                with self.assertRaises(ValueError):
                    cs._fetch_codex_usage("tok", None)

    def test_token_only_goes_to_chatgpt(self):
        with mock.patch.object(cs, "_CODEX_USAGE_URL", "https://evil.example/backend-api/wham/usage"), \
                mock.patch.object(cs._codex_opener, "open") as op:
            with self.assertRaises(ValueError):
                cs._fetch_codex_usage("tok", None)
        op.assert_not_called()
        self.assertEqual(cs._CODEX_TOKEN_DOMAINS, frozenset({"chatgpt.com"}))
        self.assertNotIn("chatgpt.com", cs._TOKEN_ALLOWED_DOMAINS)   # the Anthropic allowlist is separate

    def test_every_redirect_is_refused(self):
        req = urllib.request.Request(cs._CODEX_USAGE_URL, headers={"Authorization": "Bearer x"})
        for target in ("https://chatgpt.com/other", "http://chatgpt.com/other", "https://evil.example/"):
            with self.assertRaises(urllib.error.HTTPError):
                cs._CodexNoRedirectHandler().redirect_request(req, io.BytesIO(b""), 302, "Found", {}, target)

    # --- gates ----------------------------------------------------------------
    def test_refresh_writes_cache_without_the_token(self):
        self.write_auth(_jwt(time.time() + 3600))
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload())) as op:
            c = cs._refresh_codex_api(self.entry, self.config)
        op.assert_called_once()
        self.assertEqual(c["usage"]["five_hour"]["utilization"], 31.0)
        self.assertEqual(c["plan"], "Plus")
        raw = self._cache_file().read_text(encoding="utf-8")
        for secret in ("secret", "token", "acct-1", "signature"):
            self.assertNotIn(secret, raw.lower())
        stored = json.loads(raw)
        self.assertEqual(set(stored["usage"]), {"five_hour", "seven_day"})
        self.assertIn("last_attempt", stored)

    def test_fresh_cache_means_no_fetch(self):
        self.write_auth(_jwt(time.time() + 7200))
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload())) as op:
            cs._refresh_codex_api(self.entry, self.config)
            cs._refresh_codex_api(self.entry, self.config, now=time.time() + 120)
            cs._refresh_codex_api(self.entry, self.config, now=time.time() + 301)
        self.assertEqual(op.call_count, 2)

    def test_no_token_means_no_request(self):
        with mock.patch.object(cs._codex_opener, "open") as op:
            self.assertEqual(cs._refresh_codex_api(self.entry, self.config), {})
            self.write_auth(_jwt(time.time() + 10))         # expired
            self.assertEqual(cs._refresh_codex_api(self.entry, self.config), {})
        op.assert_not_called()

    def test_waits_its_turn_behind_other_network_checks(self):
        self.write_auth(_jwt(time.time() + 3600))
        keys = []

        def begin(key, backoff=None, primary=False):
            keys.append((key, backoff))
            return False
        with mock.patch.object(cs, "_begin_net_attempt", begin), \
                mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload())) as op:
            c = cs._refresh_codex_api(self.entry, self.config)
        op.assert_not_called()
        self.assertEqual(c, {})                       # waiting is not failing: no stamp
        self.assertEqual(keys, [("provider:codex:api", cs._PROVIDER_RETRY)])
        self.assertFalse(self._cache_file().exists())

    def test_budget_is_one_call_per_repaint(self):
        self.write_auth(_jwt(time.time() + 3600))
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload())) as op:
            cs._refresh_codex_api(self.entry, self.config, budget=[0])
            op.assert_not_called()
            budget = [1]
            cs._refresh_codex_api(self.entry, self.config, budget=budget)
            op.assert_called_once()
        self.assertEqual(budget, [0])

    def test_errors_back_off_and_stamp_the_attempt(self):
        now = time.time()
        self.write_auth(_jwt(now + 10 ** 6))   # the cases below step 10 000 s apart
        cases = [(_http_error(401), "auth_failed_until", cs._PROVIDER_AUTH_BACKOFF),
                 (_http_error(429, retry_after=1200), "rate_limited_until", 1200),
                 (_http_error(500), "error_until", cs._PROVIDER_ERROR_BACKOFF),
                 (urllib.error.URLError("down"), "error_until", cs._PROVIDER_ERROR_BACKOFF),
                 (ValueError("junk"), "error_until", cs._PROVIDER_ERROR_BACKOFF)]
        for i, (exc, key, delay) in enumerate(cases):
            with self.subTest(key=key, exc=type(exc).__name__):
                try:
                    self._cache_file().unlink()
                except FileNotFoundError:
                    pass
                t = now + i * 10_000
                with mock.patch.object(cs._codex_opener, "open", side_effect=exc) as op:
                    c = cs._refresh_codex_api(self.entry, self.config, now=t)
                    # A second attempt inside 60 s never happens, whatever failed.
                    cs._refresh_codex_api(self.entry, self.config, now=t + 30)
                self.assertEqual(op.call_count, 1)
                self.assertGreaterEqual(c[key], t + delay - 1)
                self.assertEqual(c["last_attempt"], t)
                self.assertNotIn("usage", c)

    def test_a_payload_without_windows_counts_as_an_error(self):
        self.write_auth(_jwt(time.time() + 3600))
        empty = {"plan_type": "plus", "rate_limit": {"allowed": True}}
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(empty)):
            c = cs._refresh_codex_api(self.entry, self.config)
        self.assertIn("error_until", c)
        self.assertNotIn("usage", c)

    def test_tier_falls_back_to_the_token_plan(self):
        self.write_auth(_jwt(time.time() + 3600, plan="pro"))
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload(plan=None))):
            c = cs._refresh_codex_api(self.entry, self.config)
        self.assertEqual(c["plan"], "Pro")

    # --- the row --------------------------------------------------------------
    def _codex_row(self, now=None):
        rows = cs._collect_provider_rows(self.config, now=now)
        self.assertEqual([r["entry"]["id"] for r in rows], ["codex"])
        return rows[0]

    def test_api_numbers_win_while_fresh(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(29.0, 5.0)], mtime=now - 10)
        self.write_auth(_jwt(now + 3600))
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload(now=now))):
            row = self._codex_row(now)
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 31.0)
        self.assertEqual(row["usage"]["seven_day"]["utilization"], 7.0)
        self.assertEqual(row["tier"], "Plus")
        self.assertFalse(row["stale"])

    def test_rollouts_are_the_fallback_when_the_api_fails(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(29.0, 5.0)], mtime=now - 10)
        self.write_auth(_jwt(now + 3600))
        with mock.patch.object(cs._codex_opener, "open", side_effect=_http_error(503)):
            row = self._codex_row(now)
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 29.0)
        self.assertFalse(row["stale"])
        # ... and when there is no login at all, exactly as before.
        (self.codex / "auth.json").unlink()
        with mock.patch.object(cs._codex_opener, "open") as op:
            row = self._codex_row(now)
        op.assert_not_called()
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 29.0)

    def test_old_api_numbers_lose_to_a_newer_rollout(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(29.0, 5.0)], mtime=now - 100)
        self.write_auth(_jwt(now + 3600))
        stale_api = {"timestamp": now - 3600, "error_until": now + 300, "last_attempt": now - 3600,
                     "usage": {"five_hour": {"utilization": 50.0, "resets_at": None}}}
        cs._write_provider_cache("codex:api", stale_api)
        with mock.patch.object(cs._codex_opener, "open") as op:
            row = self._codex_row(now)
        op.assert_not_called()   # error backoff running
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 29.0)
        self.assertFalse(row["stale"])

    def test_old_api_numbers_beat_an_older_rollout_and_read_stale(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(29.0, 5.0)], mtime=now - 7200)
        self.write_auth(_jwt(now + 3600))
        cs._write_provider_cache("codex:api", {
            "timestamp": now - 3600, "error_until": now + 300, "last_attempt": now - 3600,
            "usage": {"five_hour": {"utilization": 50.0, "resets_at": None}}})
        row = self._codex_row(now)
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 50.0)
        self.assertTrue(row["stale"])

    def test_an_old_rollout_alone_is_stale_too(self):
        now = time.time()
        self.sb.codex_rollout([codex_line(29.0, 5.0)], mtime=now - 7200)
        row = self._codex_row(now)
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 29.0)
        self.assertTrue(row["stale"])
        self.sb.codex_rollout([codex_line(29.0, 5.0)], mtime=now - 60)
        self.assertFalse(self._codex_row(now)["stale"])

    def test_login_without_rollouts_gives_a_row(self):
        now = time.time()
        self.write_auth(_jwt(now + 3600))
        with mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload(now=now))):
            row = self._codex_row(now)
        self.assertEqual(row["usage"]["five_hour"]["utilization"], 31.0)

    def test_the_budget_is_shared_with_claude_rows(self):
        now = time.time()
        self.sb.claude_dir(".claude", token="main-token")
        self.sb.claude_dir(".claude-b", token="other-token")
        self.write_auth(_jwt(now + 3600))
        config = {"providers": [{"id": "claude:b", "type": "claude", "dir": "~/.claude-b", "enabled": True},
                                dict(self.entry)], "providers_refresh_seconds": 300}
        with mock.patch.object(cs, "fetch_usage", return_value={"five_hour": {"utilization": 1.0}}) as claude, \
                mock.patch.object(cs._codex_opener, "open", return_value=_resp(_payload(now=now))) as codex:
            cs._collect_provider_rows(config, now=now)
            self.assertEqual((claude.call_count, codex.call_count), (1, 0))
            cs._collect_provider_rows(config, now=now + 61)
            self.assertEqual((claude.call_count, codex.call_count), (1, 1))

    # --- detection ------------------------------------------------------------
    def test_detect_sees_a_login_without_rollouts(self):
        self.write_auth(_jwt(time.time() + 3600))
        with mock.patch.object(cs.shutil, "which", return_value=None):
            items = {i["id"]: i for i in cs.detect_providers({"providers": []})}
        self.assertIn("codex", items)
        self.assertEqual(items["codex"]["note"], "ok")
        self.assertEqual(items["codex"]["tier"], "Plus")
        self.assertIsNone(items["codex"]["age_seconds"])
        dumped = json.dumps(items)
        self.assertNotIn("secret", dumped)

    def test_detect_prefers_the_rollout_tier(self):
        self.write_auth(_jwt(time.time() + 3600, plan="plus"))
        self.sb.codex_rollout([codex_line(29.0, 5.0, plan="pro")])
        with mock.patch.object(cs.shutil, "which", return_value=None):
            items = {i["id"]: i for i in cs.detect_providers({"providers": []})}
        self.assertEqual(items["codex"]["tier"], "Pro")
        self.assertEqual(items["codex"]["note"], "ok")

    def test_detect_without_login_or_rollouts_lists_nothing(self):
        with mock.patch.object(cs.shutil, "which", return_value=None):
            items = {i["id"]: i for i in cs.detect_providers({"providers": []})}
        self.assertNotIn("codex", items)


if __name__ == "__main__":
    unittest.main()
