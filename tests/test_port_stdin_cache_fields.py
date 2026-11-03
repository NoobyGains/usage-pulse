"""Corrupt time fields in cache.json on the stdin path, and bad Retry-After values.

cache.json is untrusted input. A truncated write, a hand edit, another tool, or
a value this script once wrote itself can leave a time field holding text, an
int too big for a float (10**400), Infinity or NaN. Claude Code 2.1.80+ sends
rate limits on stdin, and on that path main() reads three time fields from the
stale cache after read_cache: model_caps_fetched_at, model_caps_attempted_at
and rate_limited_until.

Pinned here, through main() end to end wherever possible:

* the bar prints a non-empty line on every repaint, whatever those fields
  hold. A raise anywhere in main() is a blank bar, and since nothing rewrote
  the file before the raise, it was the same blank bar on every later repaint;
* an unusable value counts as absent: the caps are stale, no backoff is
  active and no attempt is recent. So the stale caps are fetched once (not
  never: Infinity used to freeze them for good) and not once per repaint (the
  attempt stamp still holds the next repaints off for _MODEL_CAPS_RETRY);
* an unusable value is never written back to cache.json;
* a 429 records a finite backoff. Retry-After is honoured only as a plain
  non-negative number of seconds (RFC 9110 section 10.2.3 delay-seconds),
  capped at six hours; anything else leaves the exponential schedule in
  charge. Same rule for another Claude account's provider row;
* valid values (ints, floats, numeric strings, 0, null, missing, past and
  future) keep their old meaning and are carried forward unchanged.

Nothing here touches the network (socket and urlopen are blocked), reads real
credentials (get_credentials returns a fake token), or writes outside a temp
dir (the file writers are wrapped to fail the test if they try).

Run: ``python -m unittest tests.test_port_stdin_cache_fields``
"""
import http.client
import io
import json
import math
import os
import re
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_fixtures import ProviderSandbox, cs  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# What the OAuth usage endpoint returns for a Fable weekly cap.
FABLE_PAYLOAD = {
    "limits": [{
        "kind": "weekly_scoped",
        "percent": 81,
        "resets_at": "2099-01-05T00:00:00+00:00",
        "scope": {"model": {"display_name": "Fable"}},
    }],
}
STALE_FABLE = {"utilization": 37.0, "resets_at": "2099-01-05T00:00:00+00:00"}
STALE_CORE = {"five_hour": {"utilization": 42.0, "resets_at": "2099-01-01T05:00:00+00:00"},
              "seven_day": {"utilization": 23.0, "resets_at": "2099-01-05T00:00:00+00:00"}}

# The three time fields the stdin branch of main() reads from the stale cache.
FIELDS = ("model_caps_fetched_at", "model_caps_attempted_at", "rate_limited_until")

# Values valid JSON can hold that are not a usable time. json.dumps writes the
# floats as the non-standard literals Infinity/-Infinity/NaN, which json.load
# reads back by default, exactly as a real cache.json would carry them.
BAD_VALUES = (
    ("int too big for a float", 10 ** 400),
    ("negative int too big for a float", -(10 ** 400)),
    ("text", "soon"),
    ("Infinity", float("inf")),
    ("-Infinity", float("-inf")),
    ("NaN", float("nan")),
    ("list", [1]),
    ("object", {"t": 1}),
)
# The four the bug reports name. Each property below is pinned for these in
# each field; the crash and write-back test runs every shape above. (Each
# repaint costs real file I/O, so the full cross product is kept for that one.)
NAMED_BAD_VALUES = tuple(v for v in BAD_VALUES
                         if v[0] in ("int too big for a float", "text", "Infinity", "NaN"))

URL = "https://api.anthropic.com/api/oauth/usage"
SIX_HOURS = 6 * 60 * 60
# An epoch near 1.8e9 has a float step of about 2.4e-7 s, so (now + d) - now
# is d only to within that; bounds below allow a millisecond for it.
EPS = 1e-3

# Retry-After header lines and what a 429 carrying them must record:
# ("honour", n) waits n seconds; ("cap", None) waits the six-hour ceiling;
# ("schedule", None) means the header is not usable and the exponential
# schedule decides alone (60 s plus up to 10 s jitter for a first 429).
RETRY_AFTER_CASES = (
    ("delay-seconds", ["Retry-After: 3113"], ("honour", 3113)),
    ("whitespace around it", ["Retry-After:   3113   "], ("honour", 3113)),
    ("a fraction", ["Retry-After: 3113.5"], ("honour", 3113.5)),
    ("zero", ["Retry-After: 0"], ("schedule", None)),
    ("absent", [], ("schedule", None)),
    ("an HTTP-date", ["Retry-After: Fri, 31 Dec 1999 23:59:59 GMT"], ("schedule", None)),
    ("inf", ["Retry-After: inf"], ("schedule", None)),
    ("Infinity", ["Retry-After: Infinity"], ("schedule", None)),
    ("nan", ["Retry-After: nan"], ("schedule", None)),
    ("negative", ["Retry-After: -5000"], ("schedule", None)),
    ("an exponent", ["Retry-After: 1e9"], ("schedule", None)),
    ("non-ASCII digits", ["Retry-After: ²³¹²"], ("schedule", None)),
    ("eleven digits", ["Retry-After: 99999999999"], ("cap", None)),
    ("400 digits", ["Retry-After: " + "9" * 400], ("cap", None)),
    ("5000 digits", ["Retry-After: " + "9" * 5000], ("cap", None)),
)


def _headers(lines):
    """Response headers exactly as urllib hands them to HTTPError: an
    http.client.HTTPMessage parsed from the raw latin-1 header block."""
    blob = "".join(line + "\r\n" for line in lines) + "\r\n"
    return http.client.parse_headers(io.BytesIO(blob.encode("latin-1")))


def _http_429(lines):
    return urllib.error.HTTPError(URL, 429, "Too Many Requests", _headers(lines),
                                  io.BytesIO(b""))


def _usable_time(value):
    """A time this script can trust: a finite float within a day of now.
    (The script writes time.time() floats; a planted valid value is one too.)"""
    return (isinstance(value, float) and math.isfinite(value)
            and abs(value - time.time()) < 86400)


_MISSING = object()


class _StdinHarness(unittest.TestCase):
    def setUp(self):
        self.sb = ProviderSandbox(self)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)   # the default account
        self.sb.claude_dir(".claude", creds=False)
        self.fetches = []
        self.respond = "ok"    # "ok" | "timeout" | an HTTPError to raise
        self.network = []

        base = dict(cs.load_config())  # sandboxed: the built-in defaults
        show = dict(base.get("show", {}))
        show.update({"opus": False, "sonnet": False, "fable": True, "extra": False})
        base["show"] = show
        base["providers"] = []
        self.config = base

        root = self.sb.root.resolve()

        def confined(real):
            def wrapper(filepath, *args, **kwargs):
                target = Path(filepath).resolve()
                if target != root and root not in target.parents:
                    raise AssertionError("test wrote outside its sandbox: %s" % target)
                return real(filepath, *args, **kwargs)
            return wrapper

        def no_network(*args, **kwargs):
            self.network.append(args[:1])
            raise OSError("network disabled in tests")

        patches = [
            mock.patch.dict(cs._own_stamps, {}, clear=True),
            mock.patch.object(cs, "get_credentials", lambda: ("fake-token", "Max 20x")),
            mock.patch.object(cs, "fetch_usage", self._fetch),
            mock.patch.object(cs, "load_config",
                              lambda: dict(self.config, show=dict(self.config["show"]))),
            mock.patch.object(cs, "check_for_update", lambda *a, **k: False),
            mock.patch.object(cs, "check_claude_code_update", lambda *a, **k: False),
            mock.patch.object(cs, "sync_status_line_refresh", lambda *a, **k: None),
            mock.patch.object(cs, "_cleanup_hooks", lambda: None),
            mock.patch.object(cs.socket, "getaddrinfo", no_network),
            mock.patch.object(cs.socket, "create_connection", no_network),
            mock.patch.object(cs.urllib.request, "urlopen", no_network),
            mock.patch.object(cs, "_atomic_json_write", confined(cs._atomic_json_write)),
            mock.patch.object(cs, "_secure_open_write", confined(cs._secure_open_write)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.assertEqual(self.network, [], "a test reached for the real network")

    # --- helpers -----------------------------------------------------------
    def _fetch(self, token, timeout=10):
        self.assertEqual(token, "fake-token")
        self.fetches.append(time.time())
        if self.respond == "timeout":
            raise OSError("timed out")
        if isinstance(self.respond, BaseException):
            raise self.respond
        usage = cs._normalize_usage(json.loads(json.dumps(FABLE_PAYLOAD)))
        usage.update(json.loads(json.dumps(STALE_CORE)))
        return usage

    @staticmethod
    def _stdin_payload():
        now = time.time()
        return json.dumps({"session_id": "s1", "rate_limits": {
            "five_hour": {"used_percentage": 15, "resets_at": now + 10800},
            "seven_day": {"used_percentage": 66, "resets_at": now + 604800}}})

    def _repaint(self):
        """One status-line repaint as its own process; returns the printed text.

        main() raising is reported as a failure, not an error: the __main__
        handler turns any exception into an empty line, i.e. a blank bar."""
        buf = io.BytesIO()

        class _Out:
            buffer = buf

            def write(self, _):
                pass

            def flush(self):
                pass

        cs._own_stamps.clear()   # nothing held in memory survives a repaint
        with mock.patch.object(cs.sys, "argv", ["claude_status.py"]), \
                mock.patch.object(cs.sys, "stdin", io.StringIO(self._stdin_payload())), \
                mock.patch.object(cs.sys, "stdout", _Out()):
            try:
                cs.main()
            except Exception as exc:  # noqa: BLE001 - this IS the blank bar
                self.fail("main() raised %s: %s -- Claude Code shows a blank bar"
                          % (type(exc).__name__, str(exc)[:80]))
        return ANSI.sub("", buf.getvalue().decode("utf-8", "replace")).strip()

    def _cache_path(self):
        return Path(cs.get_cache_path())

    def _plant(self, **fields):
        """A cache whose caps are past the TTL and whose last attempt is too,
        then *fields* on top. Every scenario starts from a clean slate."""
        self.fetches.clear()
        try:
            cs._stamp_path("usage").unlink()
        except OSError:
            pass
        old = time.time() - cs._MODEL_CAPS_TTL - 30
        usage = json.loads(json.dumps(STALE_CORE))
        usage["seven_day_fable"] = dict(STALE_FABLE)
        data = {"timestamp": old, "line": "", "usage": usage, "plan": "Max 20x",
                "model_caps_fetched_at": old, "model_caps_attempted_at": old}
        for key, value in fields.items():
            if value is _MISSING:
                data.pop(key, None)
            else:
                data[key] = value
        self._cache_path().write_text(json.dumps(data), encoding="utf-8")
        return data

    def _cache(self):
        return json.loads(self._cache_path().read_text(encoding="utf-8"))

    def _assert_no_unusable_time_written(self, cached, context):
        for field in FIELDS:
            if field in cached:
                self.assertTrue(_usable_time(cached[field]),
                                "%s: cache.json now holds %s=%r, which every later "
                                "repaint would read again" % (context, field, cached[field]))

    def _assert_bar(self, line, context):
        self.assertIn("Session", line, "%s: blank or broken bar %r" % (context, line))


class CorruptTimeFieldOnStdinPathTest(_StdinHarness):
    """S1, S2, S4: unusable values in each of the three fields."""

    def test_bar_draws_on_every_repaint_and_the_bad_value_is_dropped(self):
        """The fetch times out, so nothing but the attempt stamp holds the
        next repaint off: the stale caps are tried once per retry window,
        not never (Infinity used to freeze them) and not on every repaint."""
        for field in FIELDS:
            for label, bad in BAD_VALUES:
                with self.subTest(field=field, value=label):
                    self.respond = "timeout"
                    self._plant(**{field: bad})
                    for n in range(2):
                        self._assert_bar(self._repaint(), "repaint %d" % (n + 1))
                        self._assert_no_unusable_time_written(
                            self._cache(), "after repaint %d" % (n + 1))
                    self.assertEqual(len(self.fetches), 1,
                                     "stale caps tried %d times in one retry window "
                                     "(0 means frozen for good)" % len(self.fetches))
                    self.assertGreater(self._cache().get("model_caps_attempted_at", 0),
                                       time.time() - 30, "the attempt was not stamped")

    def test_refresh_after_a_bad_value_draws_the_new_caps(self):
        for field in FIELDS:
            for label, bad in NAMED_BAD_VALUES:
                with self.subTest(field=field, value=label):
                    self.respond = "ok"
                    self._plant(**{field: bad})
                    line = self._repaint()
                    self._assert_bar(line, "refresh")
                    self.assertEqual(len(self.fetches), 1)
                    self.assertIn("81%", line, "the refreshed Fable cap is not drawn")
                    cached = self._cache()
                    self._assert_no_unusable_time_written(cached, "after a refresh")
                    self.assertNotIn("rate_limited_until", cached)
                    for stamp in ("model_caps_fetched_at", "model_caps_attempted_at"):
                        self.assertGreater(cached.get(stamp, 0), time.time() - 30, stamp)

    def test_429_records_a_finite_backoff_and_the_stale_caps_still_draw(self):
        for field in FIELDS:
            for label, bad in NAMED_BAD_VALUES:
                with self.subTest(field=field, value=label):
                    self.respond = _http_429(["Retry-After: 3113"])
                    self._plant(**{field: bad})
                    line = self._repaint()
                    self._assert_bar(line, "429")
                    self.assertIn("37%", line, "the cached Fable cap vanished")
                    self.assertEqual(len(self.fetches), 1)
                    cached = self._cache()
                    self._assert_no_unusable_time_written(cached, "after a 429")
                    remaining = cached["rate_limited_until"] - time.time()
                    self.assertTrue(3000 < remaining <= 3113 + EPS,
                                    "Retry-After 3113 not honoured: %r" % remaining)
                    self.assertEqual(cached.get("rate_limit_fails"), 1)

    def test_bad_value_is_dropped_even_when_nothing_is_fetched(self):
        """With no caps widget on screen nothing is fetched, but the
        end-of-branch write still must not carry the bad value forward."""
        self.config["show"]["fable"] = False
        for field in FIELDS:
            for label, bad in NAMED_BAD_VALUES:
                with self.subTest(field=field, value=label):
                    self._plant(**{field: bad})
                    self._assert_bar(self._repaint(), "no fetch")
                    self.assertEqual(self.fetches, [], "fetched for a hidden widget")
                    self._assert_no_unusable_time_written(self._cache(), "no fetch")


class ValidTimeFieldsKeepTheirMeaningTest(_StdinHarness):
    """Valid values behave exactly as before and are carried forward as they
    were written: the fix must only take away values nothing could use."""

    def _cases(self):
        now = time.time()
        recent, old, future = now - 10, now - cs._MODEL_CAPS_TTL - 30, now + 3600
        return (
            # field, label, value, fetches expected
            ("model_caps_fetched_at", "recent float", recent, 0),
            ("model_caps_fetched_at", "recent int", int(recent), 0),
            ("model_caps_fetched_at", "recent numeric string", repr(recent), 0),
            ("model_caps_fetched_at", "future", future, 0),
            ("model_caps_fetched_at", "old", old, 1),
            ("model_caps_fetched_at", "zero", 0, 1),
            ("model_caps_fetched_at", "string zero", "0", 1),
            ("model_caps_fetched_at", "null", None, 1),
            ("model_caps_fetched_at", "missing", _MISSING, 1),
            ("model_caps_attempted_at", "recent float", recent, 0),
            ("model_caps_attempted_at", "recent int", int(recent), 0),
            ("model_caps_attempted_at", "recent numeric string", repr(recent), 0),
            ("model_caps_attempted_at", "future", future, 0),
            ("model_caps_attempted_at", "old", old, 1),
            ("model_caps_attempted_at", "zero", 0, 1),
            ("model_caps_attempted_at", "null", None, 1),
            ("model_caps_attempted_at", "missing", _MISSING, 1),
            ("rate_limited_until", "future float", now + 600, 0),
            ("rate_limited_until", "future int", int(now + 600), 0),
            ("rate_limited_until", "future numeric string", repr(now + 600), 0),
            ("rate_limited_until", "past", now - 600, 1),
            ("rate_limited_until", "zero", 0, 1),
            ("rate_limited_until", "null", None, 1),
            ("rate_limited_until", "missing", _MISSING, 1),
        )

    def test_valid_values_drive_the_same_decisions(self):
        for field, label, value, expected in self._cases():
            with self.subTest(field=field, value=label):
                self.respond = "ok"
                self._plant(**{field: value})
                line = self._repaint()
                self._assert_bar(line, label)
                self.assertEqual(len(self.fetches), expected)
                cached = self._cache()
                if expected == 0:
                    # Nothing was fetched, so the value is carried forward
                    # exactly as it was planted (type included).
                    self.assertIn(field, cached)
                    self.assertEqual(cached[field], value)
                    self.assertEqual(type(cached[field]), type(value))
                    self.assertIn("37%", line)
                else:
                    self.assertIn("81%", line)


class RetryAfterOnStdinPathTest(_StdinHarness):
    """S3, first site: the stdin caps fetch's 429 handler in main()."""

    def test_retry_after_is_honoured_only_as_plain_seconds_and_capped(self):
        """Both 429 handlers share one parser (pinned case by case below);
        this checks the stdin site really uses it, one case per outcome."""
        for label, lines, (kind, seconds) in RETRY_AFTER_CASES:
            if label not in ("delay-seconds", "absent", "inf", "an exponent", "400 digits"):
                continue
            with self.subTest(retry_after=label):
                self.respond = _http_429(lines)
                self._plant()
                self._assert_bar(self._repaint(), label)
                self.assertEqual(len(self.fetches), 1)
                cached = self._cache()
                until = cached.get("rate_limited_until")
                self.assertTrue(_usable_time(until),
                                "the 429 recorded rate_limited_until=%r" % (until,))
                remaining = until - time.time()
                if kind == "honour":
                    self.assertTrue(seconds - 5 < remaining <= seconds + EPS, remaining)
                elif kind == "cap":
                    self.assertTrue(SIX_HOURS - 5 < remaining <= SIX_HOURS + EPS, remaining)
                else:
                    self.assertTrue(55 < remaining <= 70 + EPS, remaining)
                self._assert_bar(self._repaint(), label + ", next repaint")
                self.assertEqual(len(self.fetches), 1, "the backoff did not hold")

    def test_429_without_a_header_object_still_draws(self):
        """urllib always attaches the response headers, but the handler must
        not depend on it: an AttributeError there escaped main()."""
        self.respond = urllib.error.HTTPError(URL, 429, "Too Many Requests", None,
                                              io.BytesIO(b""))
        self._plant()
        self._assert_bar(self._repaint(), "429 with no headers")
        remaining = self._cache()["rate_limited_until"] - time.time()
        self.assertTrue(55 < remaining <= 70 + EPS, remaining)


class RetryAfterOnProviderRowTest(unittest.TestCase):
    """S3, second site: another Claude account's provider row."""

    def setUp(self):
        self.sb = ProviderSandbox(self)
        self.sb.claude_dir(".claude", token="main-token")
        self.sb.claude_dir(".claude-b", token="other-secret-token")
        self.entry = {"id": "claude:b", "type": "claude", "dir": "~/.claude-b",
                      "enabled": True}
        self.config = {"providers": [dict(self.entry)], "providers_refresh_seconds": 300}
        for name in ("_refresh_oauth_token", "refresh_and_retry"):
            p = mock.patch.object(cs, name, side_effect=AssertionError("%s called" % name))
            p.start()
            self.addCleanup(p.stop)

    def test_retry_after_is_honoured_only_as_plain_seconds_and_capped(self):
        cache_file = cs._provider_cache_path("claude:b")
        real_write = cs._write_provider_cache
        stored_checks = 0
        for label, lines, (kind, seconds) in RETRY_AFTER_CASES:
            with self.subTest(retry_after=label):
                try:
                    cache_file.unlink()
                except OSError:
                    pass
                writes = []

                def spy(pid, data):
                    ok = real_write(pid, data)
                    writes.append(ok)
                    return ok

                now = time.time()
                with mock.patch.object(cs, "fetch_usage",
                                       side_effect=_http_429(lines)) as fetch, \
                        mock.patch.object(cs, "_write_provider_cache", spy):
                    returned = cs._refresh_claude_provider(self.entry, self.config, now=now)
                if not fetch.called:
                    # The attempt stamp could not be saved, so by design no
                    # request was made: nothing here to check.
                    self.assertEqual(writes, [False])
                    continue
                checks = [("returned", returned)]
                # On Windows os.replace now and then fails with WinError 5
                # (on-access scanning), and _write_provider_cache swallows
                # that by design. The file holds the 429 only when the last
                # write landed, so only then is it read back.
                if writes and writes[-1]:
                    stored = json.loads(cache_file.read_text(encoding="utf-8"))
                    checks.append(("stored", stored))
                    stored_checks += 1
                for name, cache in checks:
                    until = cache.get("rate_limited_until")
                    self.assertTrue(_usable_time(until),
                                    "%s rate_limited_until=%r" % (name, until))
                    delay = until - now
                    if kind == "honour":
                        self.assertAlmostEqual(delay, seconds, delta=EPS)
                    elif kind == "cap":
                        self.assertAlmostEqual(delay, SIX_HOURS, delta=EPS)
                    else:
                        self.assertTrue(60 - EPS <= delay <= 70 + EPS, delay)
                    self.assertEqual(cache.get("rate_limit_fails"), 1, name)
        # A rare failed write skips one case; never landing at all is a bug.
        self.assertGreater(stored_checks, len(RETRY_AFTER_CASES) // 2,
                           "the provider cache was almost never written")


class RetryAfterHelperTest(unittest.TestCase):
    """The shared parser both 429 handlers use."""

    def test_header_values(self):
        for label, lines, (kind, seconds) in RETRY_AFTER_CASES:
            with self.subTest(retry_after=label):
                got = cs._retry_after_seconds(_headers(lines))
                self.assertIsInstance(got, float)
                expected = {"honour": seconds, "cap": SIX_HOURS, "schedule": 0.0}[kind]
                self.assertEqual(got, expected)

    def test_headers_that_are_not_a_message(self):
        for label, headers in (("None", None), ("a dict", {"retry-after": "120"}),
                               ("a non-string value", {"retry-after": 120}),
                               ("a list", ["retry-after", "120"])):
            with self.subTest(headers=label):
                got = cs._retry_after_seconds(headers)
                self.assertEqual(got, 120.0 if label == "a dict" else 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
