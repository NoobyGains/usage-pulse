"""Shared sandbox for the provider tests (not a test module itself).

Everything a provider touches is redirected into a temp dir: HOME/USERPROFILE
(so ``Path.home()``, ``~/.claude*``, ``~/.codex`` and ``~/.gemini`` are fake),
LOCALAPPDATA / XDG dirs (config.json), and ``get_state_dir`` (caches).
``CLAUDE_CONFIG_DIR`` and ``CODEX_HOME`` are removed so the developer's real
accounts can never leak in.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402


class ProviderSandbox:
    def __init__(self, testcase):
        td = tempfile.TemporaryDirectory()
        testcase.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.local = self.root / "local"
        for d in (self.home, self.state, self.local):
            d.mkdir()
        env = {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "LOCALAPPDATA": str(self.local),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
        }
        p = mock.patch.dict(os.environ, env)
        p.start()
        testcase.addCleanup(p.stop)
        for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                    "COPILOT_HOME", "COPILOT_CACHE_HOME"):
            os.environ.pop(var, None)
        p = mock.patch.object(cs, "get_state_dir", lambda: self.state)
        p.start()
        testcase.addCleanup(p.stop)
        memo = getattr(cs, "_ACCOUNT_LABELS_MEMO", {})
        memo.clear()
        testcase.addCleanup(memo.clear)

    def next_repaint(self):
        """Start a new repaint: the 5 s gap the optional network checks leave
        between turns (``_NET_MIN_SPACING``) has passed."""
        try:
            cs._stamp_path("_last").unlink()
        except OSError:
            pass

    # --- fake sources -----------------------------------------------------
    def set_active(self, path):
        os.environ["CLAUDE_CONFIG_DIR"] = str(path)

    def claude_dir(self, name=".claude", token="tok-secret-123", expires_in=3600,
                   tier="default_claude_max_20x", creds=True):
        d = self.home / name
        d.mkdir(exist_ok=True)
        if creds:
            oauth = {"accessToken": token, "refreshToken": "refresh-secret",
                     "rateLimitTier": tier}
            if expires_in is not None:
                oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
            (d / ".credentials.json").write_text(
                json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
        return d

    def accounts_file(self, accounts):
        (self.home / ".claude-accounts.json").write_text(
            json.dumps({"default": "a", "accounts": accounts}), encoding="utf-8")
        getattr(cs, "_ACCOUNT_LABELS_MEMO", {}).clear()

    def gemini(self, name="antigravity-cli", quota=None, plan_tier="Google AI Ultra",
               email="someone@example.com", mtime=None):
        d = self.home / ".gemini" / name
        d.mkdir(parents=True, exist_ok=True)
        data = {"email": email, "plan_tier": plan_tier, "quota": quota or {}}
        if plan_tier is None:
            data.pop("plan_tier")
        f = d / "statusline_last_live.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return d

    def codex_rollout(self, lines, day="2026/10/19", name="rollout-a.jsonl",
                      home=None, mtime=None):
        home = home or (self.home / ".codex")
        d = home / "sessions" / day
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return f


def copilot_snapshot(entitlement=300, remaining=231, pct_left=77.0, unlimited=False,
                     overage=0, overage_ok=False, tbb=None, **extra):
    """One ``quota_snapshots`` entry as Copilot CLI caches it."""
    snap = {"entitlement": entitlement, "remaining": remaining, "quota_remaining": remaining,
            "percent_remaining": pct_left, "unlimited": unlimited, "overage_count": overage,
            "overage_permitted": overage_ok, "quota_id": "premium_interactions",
            "has_quota": True}
    if tbb is not None:
        snap["token_based_billing"] = tbb
    snap.update(extra)
    return {k: v for k, v in snap.items() if v is not None}


def copilot_record(login, sku="monthly_subscriber", plan="individual", budget=None,
                   tbb=None, retrieved=-60, reset="2999-01-01T00:00:00Z", **extra):
    """One account entry of ``copilot-user-cache.json``."""
    resp = {"login": login, "access_type_sku": sku, "copilot_plan": plan,
            "chat_enabled": sku != "no_access"}
    if budget is not None:
        resp["quota_snapshots"] = {
            "chat": copilot_snapshot(1, 1, 100, unlimited=True),
            "premium_interactions": budget}
        resp["quota_reset_date_utc"] = reset
    if tbb is not None:
        resp["token_based_billing"] = tbb
    resp.update(extra)
    return {"schemaVersion": 1, "retrievedAt": iso_z(retrieved), "response": resp}


# Copilot CLI writes these comment lines at the top of the files it manages.
COPILOT_CACHE_HEADER = ("// Disposable cache for Copilot user responses, safe to delete. "
                        "Managed automatically.\n// User settings belong in settings.json.\n")
COPILOT_CONFIG_HEADER = ("// User settings belong in settings.json.\n"
                         "// This file is managed automatically.\n")


def write_copilot(records, active=None, header=True):
    """Write Copilot CLI's record file and config.json into the sandbox.

    ``records`` is a list of ``copilot_record()``; ``active`` is the login
    config.json names as signed in. Both files get the ``//`` comment lines
    Copilot really writes (``header=False`` for plain JSON), pretty-printed as
    it does. Returns the record file's path.
    """
    path = cs._copilot_cache_candidates()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"copilotUserCache": {
        f"v1:{i:064x}": rec for i, rec in enumerate(records)}}, indent=2)
    path.write_text((COPILOT_CACHE_HEADER if header else "") + body, encoding="utf-8")
    home = cs._copilot_home()
    home.mkdir(parents=True, exist_ok=True)
    if active is not None:
        body = json.dumps({
            "trustedFolders": ["/"],
            "lastLoggedInUser": {"host": "https://github.com", "login": active},
            "loggedInUsers": [{"host": "https://github.com", "login": active}]}, indent=2)
        (home / "config.json").write_text(
            (COPILOT_CONFIG_HEADER if header else "") + body, encoding="utf-8")
    return path


def codex_line(used5=29.0, usedw=5.0, limit_id="codex", reset5=None, resetw=None,
               plan="plus"):
    now = time.time()
    rl = {"limit_id": limit_id, "limit_name": None,
          "primary": {"used_percent": used5, "window_minutes": 300,
                      "resets_at": int(reset5 if reset5 is not None else now + 3600)},
          "secondary": {"used_percent": usedw, "window_minutes": 10080,
                        "resets_at": int(resetw if resetw is not None else now + 86400 * 3)},
          "credits": {"has_credits": False}, "plan_type": plan}
    return json.dumps({"timestamp": "2026-10-19T00:00:00Z", "type": "event_msg",
                       "payload": {"type": "token_count", "info": None, "rate_limits": rl}})


def iso_z(seconds_from_now):
    t = time.gmtime(time.time() + seconds_from_now)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def mmx_quota(left5=20.0, leftw=80.0):
    """``mmx quota show --output json`` as the CLI prints it."""
    end_ms = int((time.time() + 3600) * 1000)
    return {"model_remains": [{
        "current_interval_status": 1,
        "current_interval_remaining_percent": left5,
        "current_weekly_remaining_percent": leftw,
        "end_time": end_ms, "weekly_end_time": end_ms + 86_400_000,
    }]}


class FakeMmx:
    """A stand-in for ``subprocess.run`` that delivers output like the real one.

    The output goes wherever ``stdout`` points: into the file object handed in
    (mmx runs with no pipes), or back on the result when a caller asked for a
    captured pipe. ``exc`` is raised instead, to model a failure or a kill.
    """

    def __init__(self, payload=None, rc=0):
        self.payload = mmx_quota() if payload is None else payload
        self.rc = rc
        self.exc = None
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self.exc is not None:
            raise self.exc
        text = json.dumps(self.payload)
        dest = kwargs.get("stdout")
        out = None
        if hasattr(dest, "write"):
            dest.write(text.encode("utf-8"))
        elif kwargs.get("capture_output") or dest is subprocess.PIPE:
            wants_text = any(kwargs.get(k) for k in ("text", "universal_newlines", "encoding"))
            out = text if wants_text else text.encode("utf-8")
        return subprocess.CompletedProcess(cmd, self.rc, stdout=out, stderr=None)
