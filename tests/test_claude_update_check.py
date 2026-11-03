"""The Claude Code update indicator, and the Pulse update check's quiet paths.

U1  The indicator compared ``running != npm latest``, so anyone on a NEWER
    build than the registry's ``latest`` (an rc, a ``next`` build, a
    ``minimumVersion`` pin) was told to update. It now compares as versions:
    only a strictly newer channel version shows, and anything unparseable on
    either side shows nothing.
U2  It always compared against the ``latest`` dist-tag, but Claude Code's
    ``autoUpdatesChannel`` setting can be ``"stable"`` (about a week behind).
    One request to the registry's dist-tags endpoint now serves both channels;
    the tags are cached, and the channel is read from the user's Claude
    ``settings.json`` (honouring ``CLAUDE_CONFIG_DIR``) each time.
U3  Claude Code updates itself in the background and the new version applies
    only after a restart. When the newer version is already installed on disk
    the bar says ``↻ Restart Claude`` instead of ``↑ Claude Update``. The
    installed version is read from files only, never by running ``claude``.
F33 A plugin install, or one made without git, paid one ``git`` process per
    repaint because "not a git install" (and "a fork") were never cached.

Nothing here touches the real network, git, home or settings: the registry
transport, ``subprocess.run`` and every directory are faked or temporary.

Run: ``python tests/test_claude_update_check.py``
"""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

DIST_TAGS_URL = "https://registry.npmjs.org/-/package/@anthropic-ai/claude-code/dist-tags"
OLD_LATEST_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
TAGS = {"stable": "2.1.311", "next": "2.1.317", "latest": "2.1.317"}
UPDATE = "↑ Claude Update"
RESTART = "↻ Restart Claude"
LOCAL_SHA = "a" * 40
NETWORK_GIT = ("ls-remote", "fetch", "pull", "clone", "push")


class _Registry:
    """Stands in for ``_open_and_read``, the transport under ``_fetch_bounded``.

    Serves the dist-tags document at its URL and, so that a regression to the
    old endpoint is visible as a wrong verdict rather than a crash, the old
    per-tag document at the old one.
    """

    def __init__(self, tags=None, raw=None):
        self.tags = dict(TAGS if tags is None else tags)
        self.raw = raw
        self.urls = []

    def __call__(self, req, max_bytes, deadline):
        url = req.full_url
        self.urls.append(url)
        if url == DIST_TAGS_URL:
            body = self.raw if self.raw is not None else json.dumps(self.tags).encode()
        elif url == OLD_LATEST_URL:
            body = json.dumps({"name": "@anthropic-ai/claude-code",
                               "version": self.tags.get("latest")}).encode()
        else:
            raise AssertionError("unexpected URL in a test: %s" % url)
        return body[:max_bytes]


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


def _no_urlopen(*args, **kwargs):
    raise AssertionError("plain urlopen reached from the update check")


def _no_subprocess(*args, **kwargs):
    raise AssertionError("a subprocess was started: %r" % (args[:1],))


def _new_process():
    """Each repaint is a fresh process: nothing held in memory survives."""
    cs._own_stamps.clear()


def _next_repaint(clock):
    clock.now += cs._NET_MIN_SPACING + 0.1
    _new_process()


class _Ctx(SimpleNamespace):
    """What a sandbox hands the test: its directories."""


@contextlib.contextmanager
def _sandbox(registry=None, settings=None, subprocess_run=_no_subprocess, clock=None):
    """Temp home, Claude config dir and state dir; faked registry; no processes.

    *settings* is a dict or list (written as JSON), a str or bytes (written
    as is) for the Claude config dir's settings.json; None writes no file.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        home, cfg, state = root / "home", root / "cfg", root / "state"
        for d in (home, cfg, state):
            d.mkdir()
        if settings is not None:
            if isinstance(settings, (str, bytes)):
                raw = settings if isinstance(settings, bytes) else settings.encode("utf-8")
            else:
                raw = json.dumps(settings).encode("utf-8")
            (cfg / "settings.json").write_bytes(raw)
        env = {k: v for k, v in os.environ.items()
               if k not in ("XDG_DATA_HOME", "CLAUDE_CONFIG_DIR")}
        env.update({"HOME": str(home), "USERPROFILE": str(home),
                    "CLAUDE_CONFIG_DIR": str(cfg)})
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(cs, "get_state_dir", lambda: state),
            mock.patch.object(cs, "_open_and_read",
                              registry if registry is not None else _Registry()),
            mock.patch.object(cs.urllib.request, "urlopen", _no_urlopen),
            mock.patch.object(cs.subprocess, "run", subprocess_run),
            mock.patch.object(cs, "_CLAUDE_PATH", None),
            mock.patch.dict(cs._own_stamps, {}, clear=True),
        ]
        if clock is not None:
            patches.append(mock.patch.object(cs, "time", clock))
        for p in patches:
            p.start()
        try:
            yield _Ctx(root=root, home=home, cfg=cfg, state=state)
        finally:
            for p in reversed(patches):
                p.stop()


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


@contextlib.contextmanager
def _link(link, target):
    """Make *link* point at *target*: a real symlink, or the same through realpath.

    Creating a symlink on Windows needs a privilege most accounts lack. Then
    ``os.path.realpath`` is made to resolve *link* as a symlink would, so the
    code under test follows the same path either way.
    """
    link, target = Path(link), Path(target)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError):
        real = os.path.realpath
        want = os.path.normcase(str(link))

        def fake(path, *a, **k):
            if os.path.normcase(str(path)) == want:
                return str(target)
            return real(path, *a, **k)

        with mock.patch.object(os.path, "realpath", fake):
            yield
    else:
        yield


# ---------------------------------------------------------------------------
# U1: compare as versions
# ---------------------------------------------------------------------------

class VersionHelpersTest(unittest.TestCase):
    def test_numeric_prefix_becomes_a_tuple_of_ints(self):
        for text, want in (("2.1.317", (2, 1, 317)), ("v2.1.9", (2, 1, 9)),
                           ("2.1.317-rc.1", (2, 1, 317)), (" 2.1.10 ", (2, 1, 10)),
                           ("2.1.37 (Claude Code)", (2, 1, 37))):
            with self.subTest(text=text):
                self.assertEqual(cs._version_tuple(text), want)

    def test_unparseable_is_none(self):
        for text in ("", None, "nightly", "latest", "x.1.2", "-1.2", "\x1b[31m", "   "):
            with self.subTest(text=text):
                self.assertIsNone(cs._version_tuple(text))

    def test_comparison_is_numeric_and_pads_short_versions(self):
        self.assertEqual(cs._version_cmp("2.1.10", "2.1.9"), 1)
        self.assertEqual(cs._version_cmp("2.1.9", "2.1.10"), -1)
        self.assertEqual(cs._version_cmp("2.1", "2.1.0"), 0)
        self.assertEqual(cs._version_cmp("2.1.317-rc.1", "2.1.317"), 0)
        self.assertEqual(cs._version_cmp("3.0.0", "2.9.99"), 1)
        self.assertIsNone(cs._version_cmp("nightly", "2.1.317"))
        self.assertIsNone(cs._version_cmp("2.1.317", ""))

    def test_a_huge_digit_run_cannot_raise(self):
        self.assertIsNotNone(cs._version_tuple("1" * 6000 + ".2.3"))
        self.assertIsNotNone(cs._version_cmp("1" * 6000, "2"))


class RunningNewerThanRegistryTest(unittest.TestCase):
    """U1: a build newer than npm's tag is not "out of date"."""

    def _verdict(self, running, latest="2.1.317"):
        reg = _Registry(tags={"latest": latest, "stable": latest, "next": latest})
        with _sandbox(reg):
            return cs.check_claude_code_update(running)

    def test_running_newer_than_latest_is_not_an_update(self):
        self.assertIs(self._verdict("2.1.320"), False,
                      "a newer-than-latest build was told to update")

    def test_running_a_prerelease_of_the_latest_is_not_an_update(self):
        self.assertIs(self._verdict("2.1.317-rc.1"), False)

    def test_running_older_is_an_update(self):
        self.assertIs(self._verdict("2.1.308"), True)

    def test_running_the_latest_is_not_an_update(self):
        self.assertIs(self._verdict("2.1.317"), False)

    def test_comparison_is_numeric_not_textual(self):
        # "2.1.9" > "2.1.10" as text; as versions 2.1.10 is the newer one.
        self.assertIs(self._verdict("2.1.9", latest="2.1.10"), True)
        self.assertIs(self._verdict("2.1.10", latest="2.1.9"), False)

    def test_unparseable_running_version_shows_nothing(self):
        self.assertIsNot(self._verdict("nightly"), True)

    def test_unparseable_registry_version_shows_nothing(self):
        self.assertIsNot(self._verdict("2.1.308", latest="garbage"), True)

    def test_indicator_stays_off_for_a_newer_build(self):
        reg = _Registry()
        with _sandbox(reg):
            line = cs.append_claude_update_indicator("LINE", None, {"cc_version": "2.1.320"})
        self.assertEqual(line, "LINE")

    def test_a_newer_build_is_cached_as_no_update(self):
        reg = _Registry()
        with _sandbox(reg):
            cs.check_claude_code_update("2.1.320")
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.320"), False)
        self.assertEqual(len(reg.urls), 1)


# ---------------------------------------------------------------------------
# U2: the user's channel
# ---------------------------------------------------------------------------

class ChannelTest(unittest.TestCase):
    def test_one_bounded_request_goes_to_the_dist_tags_endpoint(self):
        reg = _Registry()
        with _sandbox(reg):
            cs.check_claude_code_update("2.1.300")
        self.assertEqual(reg.urls, [DIST_TAGS_URL])

    def test_stable_channel_compares_against_the_stable_tag(self):
        with _sandbox(_Registry(), settings={"autoUpdatesChannel": "stable"}):
            self.assertIs(cs.check_claude_code_update("2.1.311"), False,
                          "a stable-channel user on stable was told to update to latest")
        with _sandbox(_Registry(), settings={"autoUpdatesChannel": "stable"}) as ctx:
            self.assertIs(cs.check_claude_code_update("2.1.308"), True)
            cached = json.loads((ctx.state / "claude_code_update.json").read_text("utf-8"))
        self.assertEqual(cached["remote"], "2.1.311")

    def test_latest_is_the_default_for_every_other_settings_state(self):
        cases = {
            "explicit latest": {"autoUpdatesChannel": "latest"},
            "key missing": {"model": "x"},
            "unknown value": {"autoUpdatesChannel": "beta"},
            "wrong case": {"autoUpdatesChannel": "Stable"},
            "not a string": {"autoUpdatesChannel": 5},
            "null": {"autoUpdatesChannel": None},
            "a list": {"autoUpdatesChannel": ["stable"]},
            "settings not an object": [1, 2],
            "settings not JSON": "{nope",
            "settings not UTF-8": b"\xff\xfe{",
            "nesting too deep to parse": b"[" * 100000 + b"]" * 100000,
            "no settings file": None,
        }
        for case, settings in cases.items():
            with self.subTest(case=case):
                with _sandbox(_Registry(), settings=settings):
                    self.assertIs(cs.check_claude_code_update("2.1.311"), True,
                                  "%s did not fall back to the latest tag" % case)

    def test_claude_config_dir_is_honoured(self):
        reg = _Registry()
        with _sandbox(reg, settings={"autoUpdatesChannel": "stable"}) as ctx:
            # A decoy under ~/.claude that says the opposite must be ignored.
            _write(ctx.home / ".claude" / "settings.json",
                   json.dumps({"autoUpdatesChannel": "latest"}))
            self.assertIs(cs.check_claude_code_update("2.1.311"), False)

    def test_default_config_dir_is_read_when_the_variable_is_unset(self):
        reg = _Registry()
        with _sandbox(reg) as ctx:
            del os.environ["CLAUDE_CONFIG_DIR"]
            _write(ctx.home / ".claude" / "settings.json",
                   json.dumps({"autoUpdatesChannel": "stable"}))
            self.assertIs(cs.check_claude_code_update("2.1.311"), False)

    def test_the_stable_tag_missing_shows_nothing_rather_than_nagging_to_latest(self):
        reg = _Registry(tags={"latest": "2.1.317", "next": "2.1.317"})
        with _sandbox(reg, settings={"autoUpdatesChannel": "stable"}):
            self.assertIs(cs.check_claude_code_update("2.1.311"), False)

    def test_a_payload_without_a_usable_latest_tag_is_a_failed_check(self):
        for raw in (b"not json", b"[1, 2]", b"{}", b'{"latest": 5}', b'{"stable": "2.1.311"}'):
            with self.subTest(raw=raw):
                reg = _Registry(raw=raw)
                with _sandbox(reg) as ctx:
                    self.assertIsNone(cs.check_claude_code_update("2.1.300"))
                    self.assertFalse((ctx.state / "claude_code_update.json").exists())


class TagCachingTest(unittest.TestCase):
    def test_the_tags_are_cached_not_just_a_verdict(self):
        reg = _Registry()
        with _sandbox(reg) as ctx:
            cs.check_claude_code_update("2.1.311")
            cached = json.loads((ctx.state / "claude_code_update.json").read_text("utf-8"))
        self.assertEqual(cached.get("tags"), TAGS)
        self.assertIs(cached["update_available"], True)
        self.assertEqual(cached["local"], "2.1.311")
        self.assertEqual(cached["remote"], "2.1.317")

    def test_switching_channel_is_answered_from_the_cached_tags(self):
        reg = _Registry()
        with _sandbox(reg) as ctx:
            self.assertIs(cs.check_claude_code_update("2.1.311"), True)  # latest
            (ctx.cfg / "settings.json").write_text(
                json.dumps({"autoUpdatesChannel": "stable"}), encoding="utf-8")
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.311"), False)
            cached = json.loads((ctx.state / "claude_code_update.json").read_text("utf-8"))
        self.assertEqual(len(reg.urls), 1, "a channel switch cost a new request")
        self.assertEqual(cached["remote"], "2.1.311",
                         "--config reads 'remote' and would show the old channel's version")
        self.assertIs(cached["update_available"], False)

    def test_a_channel_switch_does_not_extend_the_cache_lifetime(self):
        reg = _Registry()
        with _sandbox(reg) as ctx:
            cs.check_claude_code_update("2.1.311")
            path = ctx.state / "claude_code_update.json"
            stamp = json.loads(path.read_text("utf-8"))["timestamp"]
            (ctx.cfg / "settings.json").write_text(
                json.dumps({"autoUpdatesChannel": "stable"}), encoding="utf-8")
            _new_process()
            cs.check_claude_code_update("2.1.311")
            self.assertEqual(json.loads(path.read_text("utf-8"))["timestamp"], stamp)

    def test_a_new_running_version_is_a_miss(self):
        reg, clock = _Registry(), _FakeClock()
        with _sandbox(reg, clock=clock):
            self.assertIs(cs.check_claude_code_update("2.1.311"), True)
            clock.now += cs._NET_RETRY_BACKOFF + 1  # past the attempt stamp's backoff
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.317"), False)
        self.assertEqual(len(reg.urls), 2)

    def test_a_cache_without_tags_is_a_miss(self):
        # The 3.6.2 format: a verdict and the latest version, but no tags.
        reg = _Registry()
        with _sandbox(reg) as ctx:
            _write(ctx.state / "claude_code_update.json", json.dumps({
                "timestamp": cs.time.time(), "update_available": False,
                "local": "2.1.311", "remote": "2.1.311"}))
            self.assertIs(cs.check_claude_code_update("2.1.311"), True)
        self.assertEqual(len(reg.urls), 1)

    def test_malformed_tags_in_the_cache_are_a_miss(self):
        for tags in ("x", [1], None, {"latest": 5}, {"stable": "2.1.311"}):
            with self.subTest(tags=tags):
                reg = _Registry()
                with _sandbox(reg) as ctx:
                    _write(ctx.state / "claude_code_update.json", json.dumps({
                        "timestamp": cs.time.time(), "update_available": False,
                        "local": "2.1.311", "remote": "2.1.311", "tags": tags}))
                    self.assertIs(cs.check_claude_code_update("2.1.311"), True)
                self.assertEqual(len(reg.urls), 1)

    def test_failed_fetch_backs_off_like_before(self):
        reg = _Registry(raw=b"not json")
        with _sandbox(reg):
            self.assertIsNone(cs.check_claude_code_update("2.1.300"))
            _new_process()
            self.assertIsNone(cs.check_claude_code_update("2.1.300"))
        self.assertEqual(len(reg.urls), 1, "a failed check must back off, not retry per repaint")

    def test_interactive_check_is_not_gated_and_fills_the_shared_cache(self):
        reg = _Registry(raw=b"not json")
        with _sandbox(reg):
            self.assertIsNone(cs.check_claude_code_update("2.1.300"))  # bar: fails, backs off
            reg.raw = None
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.300", interactive=True), True)
            _new_process()
            self.assertIs(cs.check_claude_code_update("2.1.300"), True)
        self.assertEqual(len(reg.urls), 2)


# ---------------------------------------------------------------------------
# U3: restart instead of update
# ---------------------------------------------------------------------------

def _windows_native(ctx, versions=("2.1.308", "2.1.311", "2.1.317")):
    """A Windows native install: a real claude.exe and version-named files."""
    launcher = _write(ctx.home / ".local" / "bin" / "claude.exe", b"MZ")
    for name in versions:
        _write(ctx.home / ".local" / "share" / "claude" / "versions" / name)
    return launcher


class InstalledVersionTest(unittest.TestCase):
    """_installed_claude_version: files only, never a subprocess."""

    def _installed(self, launcher, platform):
        with mock.patch.object(cs, "_CLAUDE_PATH", str(launcher) if launcher else None), \
                mock.patch.object(cs.sys, "platform", platform):
            return cs._installed_claude_version()

    def test_windows_native_uses_the_highest_version_named_entry(self):
        with _sandbox() as ctx:
            launcher = _windows_native(ctx)
            _write(ctx.home / ".local" / "share" / "claude" / "versions" / "2.1.318.tmp")
            _write(ctx.home / ".local" / "share" / "claude" / "versions" / "latest")
            self.assertEqual(self._installed(launcher, "win32"), "2.1.317")

    def test_windows_native_orders_versions_numerically(self):
        with _sandbox() as ctx:
            launcher = _windows_native(ctx, versions=("2.1.9", "2.1.10", "2.0.99"))
            self.assertEqual(self._installed(launcher, "win32"), "2.1.10")

    def test_windows_native_accepts_exe_suffixed_entries(self):
        with _sandbox() as ctx:
            launcher = _windows_native(ctx, versions=("2.1.311.exe", "2.1.317.exe"))
            self.assertEqual(self._installed(launcher, "win32"), "2.1.317")

    def test_windows_native_is_found_without_claude_on_the_path(self):
        with _sandbox() as ctx:
            _windows_native(ctx)
            self.assertEqual(self._installed(None, "win32"), "2.1.317")

    def test_xdg_data_home_replaces_the_default_data_dir(self):
        with _sandbox() as ctx:
            launcher = _write(ctx.home / ".local" / "bin" / "claude.exe", b"MZ")
            _write(ctx.home / ".local" / "share" / "claude" / "versions" / "2.1.200")
            xdg = ctx.root / "xdg"
            _write(xdg / "claude" / "versions" / "2.1.317")
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(xdg)}):
                self.assertEqual(self._installed(launcher, "win32"), "2.1.317")

    def test_posix_native_reads_the_version_from_the_symlink_target(self):
        with _sandbox() as ctx:
            data = ctx.home / ".local" / "share" / "claude" / "versions"
            _write(data / "2.1.311")
            target = _write(data / "2.1.317")
            launcher = ctx.home / ".local" / "bin" / "claude"
            with _link(launcher, target):
                self.assertEqual(self._installed(launcher, "linux"), "2.1.317",
                                 "the launcher's target says 2.1.317; 2.1.311 sits beside it")

    def test_posix_native_follows_the_launcher_not_the_newest_file(self):
        with _sandbox() as ctx:
            data = ctx.home / ".local" / "share" / "claude" / "versions"
            target = _write(data / "2.1.311")
            _write(data / "2.1.317")  # downloaded, launcher not switched yet
            launcher = ctx.home / ".local" / "bin" / "claude"
            with _link(launcher, target):
                self.assertEqual(self._installed(launcher, "linux"), "2.1.311")

    def test_posix_plain_file_launcher_is_not_guessed_from_a_versions_dir(self):
        with _sandbox() as ctx:
            launcher = _windows_native(ctx)
            self.assertIsNone(self._installed(launcher, "linux"))

    def test_symlink_to_something_that_is_not_a_versions_entry_is_unknown(self):
        with _sandbox() as ctx:
            target = _write(ctx.root / "opt" / "claude-wrapper")
            launcher = ctx.home / ".local" / "bin" / "claude"
            with _link(launcher, target):
                self.assertIsNone(self._installed(launcher, "linux"))

    def test_windows_npm_shim_reads_the_package_next_to_it(self):
        with _sandbox() as ctx:
            npm = ctx.root / "AppData" / "npm"
            launcher = _write(npm / "claude.cmd", "@echo off")
            _write(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
                   json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.1.317"}))
            _windows_native(ctx, versions=("2.1.100",))  # a stale native leftover
            self.assertEqual(self._installed(launcher, "win32"), "2.1.317")

    def test_posix_npm_symlink_resolves_into_the_package(self):
        with _sandbox() as ctx:
            prefix = ctx.root / "usr"
            pkg = prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
            _write(pkg / "package.json",
                   json.dumps({"name": "@anthropic-ai/claude-code", "version": "2.1.317"}))
            target = _write(pkg / "bin" / "claude.exe")
            launcher = prefix / "bin" / "claude"
            with _link(launcher, target):
                self.assertEqual(self._installed(launcher, "linux"), "2.1.317")

    def test_a_package_json_for_another_package_is_ignored(self):
        with _sandbox() as ctx:
            npm = ctx.root / "npm"
            launcher = _write(npm / "claude.cmd", "@echo off")
            _write(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json",
                   json.dumps({"name": "something-else", "version": "9.9.9"}))
            self.assertIsNone(self._installed(launcher, "win32"))

    def test_unreadable_package_json_is_unknown(self):
        for data in (b"{nope", b"\xff\xfe", b"[]", b'{"name": "@anthropic-ai/claude-code"}', b""):
            with self.subTest(data=data), _sandbox() as ctx:
                npm = ctx.root / "npm"
                launcher = _write(npm / "claude.cmd", "@echo off")
                _write(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "package.json", data)
                self.assertIsNone(self._installed(launcher, "win32"))

    def test_nothing_found_is_none(self):
        with _sandbox() as ctx:
            self.assertIsNone(self._installed(None, "win32"))
            self.assertIsNone(self._installed(ctx.root / "missing" / "claude.exe", "win32"))
            self.assertIsNone(self._installed(None, "linux"))

    def test_an_empty_versions_dir_is_none(self):
        with _sandbox() as ctx:
            launcher = _write(ctx.home / ".local" / "bin" / "claude.exe", b"MZ")
            (ctx.home / ".local" / "share" / "claude" / "versions").mkdir(parents=True)
            self.assertIsNone(self._installed(launcher, "win32"))

    def test_never_starts_a_process(self):
        # _sandbox makes subprocess.run raise; reaching it would surface here.
        with _sandbox() as ctx:
            launcher = _windows_native(ctx)
            self.assertEqual(self._installed(launcher, "win32"), "2.1.317")


class RestartIndicatorTest(unittest.TestCase):
    """U3: the newer version is already on disk, so say restart, not update."""

    def _line(self, running, installed, tags=None, settings=None, config=None):
        with _sandbox(_Registry(tags), settings=settings), \
                mock.patch.object(cs, "_installed_claude_version",
                                  return_value=installed, create=True):
            return cs.append_claude_update_indicator("LINE", config, {"cc_version": running})

    def test_installed_channel_version_says_restart(self):
        line = self._line("2.1.308", "2.1.317")
        self.assertEqual(line, "LINE %s%s%s" % (cs.BRIGHT_YELLOW, RESTART, cs.RESET))

    def test_installed_newer_than_the_channel_still_says_restart(self):
        self.assertIn(RESTART, self._line("2.1.308", "2.1.320"))

    def test_installed_older_than_the_channel_says_update(self):
        line = self._line("2.1.308", "2.1.311")
        self.assertEqual(line, "LINE %s%s%s" % (cs.BRIGHT_YELLOW, UPDATE, cs.RESET))

    def test_unknown_installed_version_says_update(self):
        self.assertIn(UPDATE, self._line("2.1.308", None))

    def test_nothing_shows_when_the_running_version_is_current(self):
        self.assertEqual(self._line("2.1.317", "2.1.320"), "LINE")

    def test_restart_is_judged_against_the_users_channel(self):
        stable = {"autoUpdatesChannel": "stable"}
        # Stable channel: 2.1.311 is the target and it is installed.
        self.assertIn(RESTART, self._line("2.1.308", "2.1.311", settings=stable))
        # Latest channel: 2.1.317 is the target and only 2.1.311 is installed.
        self.assertIn(UPDATE, self._line("2.1.308", "2.1.311"))

    def test_the_show_flag_controls_both_texts(self):
        off = {"show": {"claude_update": False}}
        self.assertEqual(self._line("2.1.308", "2.1.317", config=off), "LINE")
        self.assertEqual(self._line("2.1.308", None, config=off), "LINE")

    def test_installed_version_is_not_read_unless_an_update_is_pending(self):
        probe = mock.Mock(return_value="2.1.317")
        with _sandbox(), mock.patch.object(cs, "_installed_claude_version", probe, create=True):
            cs.append_claude_update_indicator("LINE", None, {"cc_version": "2.1.317"})
        probe.assert_not_called()

    def test_without_a_cache_the_text_falls_back_to_update(self):
        # check_claude_code_update is stubbed, so no cache holds the versions.
        with _sandbox(), \
                mock.patch.object(cs, "check_claude_code_update", return_value=True), \
                mock.patch.object(cs, "_installed_claude_version",
                                  return_value="2.1.317", create=True):
            line = cs.append_claude_update_indicator("LINE", None, {"cc_version": "2.1.308"})
        self.assertIn(UPDATE, line)

    def test_end_to_end_from_files_on_disk(self):
        with _sandbox(_Registry()) as ctx:
            launcher = _windows_native(ctx)  # 2.1.317 is installed
            with mock.patch.object(cs, "_CLAUDE_PATH", str(launcher)), \
                    mock.patch.object(cs.sys, "platform", "win32"):
                restart = cs.append_claude_update_indicator("LINE", None, {"cc_version": "2.1.308"})
                _new_process()
                current = cs.append_claude_update_indicator("LINE", None, {"cc_version": "2.1.317"})
        self.assertIn(RESTART, restart)
        self.assertNotIn(UPDATE, restart)
        self.assertEqual(current, "LINE")

    def test_end_to_end_installed_older_than_channel_says_update(self):
        with _sandbox(_Registry()) as ctx:
            launcher = _windows_native(ctx, versions=("2.1.308", "2.1.311"))
            with mock.patch.object(cs, "_CLAUDE_PATH", str(launcher)), \
                    mock.patch.object(cs.sys, "platform", "win32"):
                line = cs.append_claude_update_indicator("LINE", None, {"cc_version": "2.1.308"})
        self.assertIn(UPDATE, line)


# ---------------------------------------------------------------------------
# F33: negative verdicts are cached, plugin installs skip the check
# ---------------------------------------------------------------------------

class _Git:
    """``subprocess.run`` stand-in for the local git calls of the Pulse check."""

    def __init__(self, local=LOCAL_SHA, origin="https://github.com/%s.git" % cs.GITHUB_REPO,
                 branch="main"):
        self.local, self.origin, self.branch = local, origin, branch
        self.calls = []

    def __call__(self, cmd, **kwargs):
        args = list(cmd[1:])
        self.calls.append(args)
        sub = args[0] if args else ""
        rc, out = 0, ""
        if sub == "rev-parse":
            rc, out = (0, self.local + "\n") if self.local else (128, "")
        elif sub == "symbolic-ref":
            out = self.branch + "\n"
        elif sub == "remote":
            out = self.origin + "\n"
        elif sub in NETWORK_GIT:
            rc = 128
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    @property
    def network_calls(self):
        return [a for a in self.calls if a and a[0] in NETWORK_GIT]


def _read_cache(ctx):
    try:
        return json.loads((ctx.state / "update_check.json").read_text("utf-8"))
    except OSError:
        return None


class PulseUpdateQuietPathsTest(unittest.TestCase):
    def test_a_non_git_install_spawns_git_once_not_on_every_repaint(self):
        clock, git, reg = _FakeClock(), _Git(local=None), _Registry()
        with _sandbox(reg, subprocess_run=git, clock=clock) as ctx:
            first = cs.check_for_update()
            self.assertEqual(git.calls, [["rev-parse", "HEAD"]])
            for _ in range(4):
                _next_repaint(clock)
                self.assertIs(cs.check_for_update(), False)
            cached = _read_cache(ctx)
        self.assertIn(first, (None, False))
        self.assertEqual(
            len(git.calls), 1,
            "a non-git install spawned git on a later repaint: %r" % (git.calls,))
        self.assertIsNotNone(cached, "no verdict was cached for a non-git install")
        self.assertIs(cached["update_available"], False)
        self.assertEqual(cached["reason"], "no_git")
        self.assertEqual(reg.urls, [])

    def test_a_fork_spawns_git_once_not_on_every_repaint(self):
        clock, reg = _FakeClock(), _Registry()
        git = _Git(origin="https://github.com/someone/else.git")
        with _sandbox(reg, subprocess_run=git, clock=clock) as ctx:
            first = cs.check_for_update()
            spawned = len(git.calls)
            for _ in range(4):
                _next_repaint(clock)
                self.assertIs(cs.check_for_update(), False)
            cached = _read_cache(ctx)
        self.assertIn(first, (None, False))
        self.assertEqual(len(git.calls), spawned,
                         "a fork spawned git again: %r" % (git.calls[spawned:],))
        self.assertIsNotNone(cached, "no verdict was cached for a fork")
        self.assertIs(cached["update_available"], False)
        self.assertEqual(cached["reason"], "fork")
        self.assertEqual(cached["local"], LOCAL_SHA[:8])
        self.assertEqual(git.network_calls, [])
        self.assertEqual(reg.urls, [])

    def test_the_negative_verdict_lapses_with_the_cache_ttl(self):
        clock, git = _FakeClock(), _Git(local=None)
        with _sandbox(subprocess_run=git, clock=clock):
            cs.check_for_update()
            clock.now += cs.UPDATE_CHECK_TTL + 1
            _new_process()
            cs.check_for_update()
        self.assertEqual(len(git.calls), 2, "the verdict never expired")

    def test_a_rewritten_script_invalidates_a_cached_negative(self):
        clock, git = _FakeClock(), _Git(local=None)
        with _sandbox(subprocess_run=git, clock=clock) as ctx:
            cs.check_for_update()
            path = ctx.state / "update_check.json"
            cached = json.loads(path.read_text("utf-8"))
            cached["script_mtime"] = (cached["script_mtime"] or 0) - 5  # an older file
            path.write_text(json.dumps(cached), encoding="utf-8")
            _next_repaint(clock)
            cs.check_for_update()
        self.assertEqual(len(git.calls), 2)

    def test_a_real_tracking_install_is_still_checked(self):
        # Not a fork, on main: the verdict needs the network, so no negative is cached.
        clock, git = _FakeClock(), _Git()
        reg = _Registry()
        with _sandbox(reg, subprocess_run=git, clock=clock) as ctx:
            cs.check_for_update()
            cached = _read_cache(ctx)
        self.assertTrue(cached is None or "reason" not in cached)


class PluginInstallTest(unittest.TestCase):
    def _plugin_file(self, ctx):
        return str(ctx.root / ".claude" / "plugins" / "cache" / "mkt" / "usage-pulse"
                   / "3.6.2" / "claude_status.py")

    def test_a_plugin_install_spawns_no_git_at_all(self):
        for interactive in (False, True):
            with self.subTest(interactive=interactive):
                git, reg = _Git(), _Registry()
                with _sandbox(reg, subprocess_run=git) as ctx, \
                        mock.patch.object(cs, "__file__", self._plugin_file(ctx)):
                    verdict = cs.check_for_update(interactive=interactive)
                    wrote = (ctx.state / "update_check.json").exists()
                self.assertEqual(git.calls, [], "a plugin install spawned git")
                self.assertEqual(reg.urls, [])
                self.assertIsNone(verdict)
                self.assertFalse(wrote)

    def test_the_pulse_indicator_stays_off_for_a_plugin_install(self):
        git = _Git(local="b" * 40)
        with _sandbox(subprocess_run=git) as ctx, \
                mock.patch.object(cs, "__file__", self._plugin_file(ctx)):
            self.assertEqual(cs.append_update_indicator("LINE"), "LINE")
        self.assertEqual(git.calls, [])

    def test_a_git_checkout_in_a_plugin_looking_folder_is_still_checked(self):
        # <anything>/usage-pulse/<worktree>/claude_status.py with a .git beside it
        # is a developer's checkout, not a marketplace cache folder.
        git = _Git()
        with _sandbox(subprocess_run=git) as ctx:
            folder = ctx.root / "code" / "usage-pulse" / "main"
            (folder / ".git").mkdir(parents=True)
            with mock.patch.object(cs, "__file__", str(folder / "claude_status.py")):
                cs.check_for_update()
        self.assertTrue(git.calls, "a real checkout was skipped as if it were a plugin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
