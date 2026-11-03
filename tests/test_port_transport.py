"""The transport that carries the Claude OAuth token, exercised over loopback.

``_safe_opener`` is the only opener that ever sends the user's OAuth bearer
token. The render-path fix swapped its HTTPS handler for one whose connect is
bounded (see "Render-path networking" in claude_status.py), so this module
pins, end to end, what that handler must still do exactly as the standard
library does, and the few things it must do better:

* TLS: certificates are verified, hostnames are checked, and the connection
  gets the TLS settings the stock handler would give it on this Python, so a
  later in-process change to ``ssl._create_default_https_context`` reaches the
  token request no more than it reaches a stock handler.
* Redirects: followed only to https on an allowlisted host, so the token is
  never re-sent to a third party, nor over plain http. The starting URL must
  be https too: a plain-http one is refused before anything is built or sent.
* Proxies: a CONNECT tunnel still goes through the bounded connector, and TLS
  is still verified through it.
* The connector is a real drop-in for ``socket.create_connection``: the
  default-timeout sentinel and None mean what they mean there, and the DNS
  lookup obeys the caller's deadline instead of hanging past it.
* A Python built without ssl still loads the script and draws the bar.
* An error answer (a 403, say) that ``_fetch_bounded`` turns into "no answer"
  is closed there, not left holding its socket until garbage collection.
* The script no longer imports ``http.client``, which nothing in it used.

Isolation: every server listens on 127.0.0.1, DNS is faked for the test host
names, ``no_proxy=*`` keeps any proxy the process picked up at import out of
the way, and a guard refuses every connect that is not loopback, so nothing
here can reach the real network. The token is a fake marker string. The
certificates come from throwaway CAs made at test time with the ``openssl``
CLI (the stdlib cannot make certificates) in a temp dir that is deleted
afterwards: no private key is committed, and no system or user trust store is
touched; only the opener under test is told to trust the test CA. Without an
openssl CLI the TLS tests skip, saying so.

Run: ``python tests/test_port_transport.py``
"""
import ast
import contextlib
import gc
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.response
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import ssl
except ImportError:  # a Python built without OpenSSL; WithoutSslTest still runs
    ssl = None

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import claude_status as cs  # noqa: E402

FAKE_TOKEN = "fake-oauth-token-for-loopback-tests-0123456789"
API_HOST = "api.anthropic.com"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
LOOPBACK = ("127.0.0.1", "::1")
BLOCKED = "Redirect to non-allowed domain blocked"
HAVE_SSL = ssl is not None and hasattr(urllib.request, "HTTPSHandler")


# ---------------------------------------------------------------------------
# Throwaway certificates (openssl CLI, generated per test run)
# ---------------------------------------------------------------------------

def _find_openssl():
    """The openssl CLI, or None. Git for Windows ships one that is off PATH."""
    found = shutil.which("openssl")
    if found:
        return found
    if os.name != "nt":
        return None
    candidates = []
    git = shutil.which("git")
    if git:
        here = Path(git).resolve().parent
        # <Git>\cmd\git.exe, <Git>\bin\git.exe or <Git>\mingw64\bin\git.exe
        for root in (here.parent, here.parent.parent):
            candidates.append(root / "usr" / "bin" / "openssl.exe")
            candidates.append(root / "mingw64" / "bin" / "openssl.exe")
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "openssl.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


# Subjects live in config files rather than "-subj /CN=...": the MSYS openssl
# in Git for Windows rewrites arguments that look like POSIX paths. The
# extensions are what Python 3.13+'s strict X.509 verification insists on.
_CA_CNF = """\
[req]
prompt = no
distinguished_name = dn
[dn]
CN = {cn}
[ext]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""

_LEAF_CNF = """\
[req]
prompt = no
distinguished_name = dn
[dn]
CN = {cn}
[ext]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:{cn}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
"""


class _PKI:
    """Two throwaway CAs and three server certificates.

    ``ca`` is the one a test may choose to trust; ``rogue`` is never trusted.
    ``api`` is api.anthropic.com from ``ca``, ``evil`` is evil.example from
    ``ca`` (a valid chain for the wrong name), and ``rogue_api`` is
    api.anthropic.com from ``rogue``. All three leaves share one key.
    """

    def __init__(self, openssl, directory):
        self._openssl = openssl
        self.dir = Path(directory)
        for ca in ("ca", "rogue"):
            self._write(ca + ".cnf", _CA_CNF.format(cn="usage-pulse test %s (throwaway)" % ca))
            self._run("req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
                      "-days", "2", "-config", ca + ".cnf", "-extensions", "ext",
                      "-keyout", ca + ".key", "-out", ca + ".crt")
        self._run("genrsa", "-out", "leaf.key", "2048")
        for name, host, ca in (("api", API_HOST, "ca"), ("evil", "evil.example", "ca"),
                               ("rogue_api", API_HOST, "rogue")):
            self._write(name + ".cnf", _LEAF_CNF.format(cn=host))
            self._run("req", "-new", "-key", "leaf.key", "-config", name + ".cnf",
                      "-out", name + ".csr")
            self._run("x509", "-req", "-in", name + ".csr", "-CA", ca + ".crt",
                      "-CAkey", ca + ".key", "-set_serial",
                      "0x%x" % int.from_bytes(os.urandom(8), "big"),
                      "-days", "2", "-sha256", "-extfile", name + ".cnf",
                      "-extensions", "ext", "-out", name + ".crt")
        self.ca = str(self.dir / "ca.crt")
        self.key = str(self.dir / "leaf.key")

    def cert(self, name):
        return str(self.dir / (name + ".crt"))

    def _write(self, name, text):
        (self.dir / name).write_text(text, encoding="ascii")

    def _run(self, *args):
        result = subprocess.run([self._openssl] + list(args), cwd=str(self.dir),
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError("openssl %s failed: %s" % (args[0], result.stderr.strip()[-300:]))


_pki_state = []  # [("ok", _PKI) | ("skip", reason)], built once per test run


def _require_pki(test):
    if not HAVE_SSL:
        test.skipTest("this Python has no ssl module")
    if not _pki_state:
        openssl = _find_openssl()
        if openssl is None:
            _pki_state.append(("skip", "no openssl CLI found; it is needed to make "
                                       "throwaway test certificates"))
        else:
            directory = tempfile.mkdtemp(prefix="usage-pulse-test-pki-")
            unittest.addModuleCleanup(shutil.rmtree, directory, True)
            try:
                _pki_state.append(("ok", _PKI(openssl, directory)))
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                _pki_state.append(("skip", "openssl (%s) could not make test "
                                           "certificates: %s" % (openssl, exc)))
    kind, value = _pki_state[0]
    if kind == "skip":
        test.skipTest(value)
    return value


# ---------------------------------------------------------------------------
# Loopback servers
# ---------------------------------------------------------------------------

OK_RESPONSE = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
               b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")


def _redirect(location):
    return ("HTTP/1.1 302 Found\r\nLocation: %s\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n" % location).encode("latin-1")


def _read_head(conn):
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _parse_head(data):
    lines = data.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            name, value = line.split(b":", 1)
            headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return lines[0].decode("latin-1"), headers


class _Server:
    """A loopback listener; each connection is handled in turn on a daemon thread."""

    def __init__(self, handle):
        self._handle = handle
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.05)  # polled, so close() works on every OS
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="test-server-%d" % self.port)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            conn.settimeout(5)
            try:
                self._handle(conn)
            except Exception:
                pass  # the client side of each test asserts what mattered
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self._thread.join(5)
        self._sock.close()


class _TLSOrigin:
    """api.anthropic.com over TLS: records each request and answers from a script."""

    def __init__(self, certfile, keyfile):
        self._cert = (certfile, keyfile)
        self.responses = []  # raw responses, one per request; then 200 {}
        self.requests = []
        self.handshake_failures = []
        self.sni = []
        self._server = _Server(self._handle)
        self.port = self._server.port

    def _handle(self, raw):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*self._cert)
        ctx.sni_callback = self._record_sni
        try:
            conn = ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            self.handshake_failures.append(type(exc).__name__)
            return
        try:
            head = _read_head(conn)
            if not head:
                return
            line, headers = _parse_head(head)
            self.requests.append({"line": line, "host": headers.get("host"),
                                  "authorization": headers.get("authorization")})
            conn.sendall(self.responses.pop(0) if self.responses else OK_RESPONSE)
        except (ssl.SSLError, OSError):
            pass
        finally:
            conn.close()

    def _record_sni(self, _sslobj, server_name, _ctx):
        self.sni.append(server_name)
        return None

    def close(self):
        self._server.close()


class _PlainOrigin:
    """api.anthropic.com:80 in plain text: any connection here is a leak."""

    def __init__(self):
        self.requests = []
        self._server = _Server(self._handle)
        self.port = self._server.port

    @property
    def accepted(self):
        return self._server.accepted

    def _handle(self, conn):
        line, headers = _parse_head(_read_head(conn))
        self.requests.append({"line": line, "authorization": headers.get("authorization")})
        conn.sendall(OK_RESPONSE)

    def close(self):
        self._server.close()


class _ConnectProxy:
    """A CONNECT proxy that tunnels to *upstream_port* and keeps what the client sent."""

    def __init__(self, upstream_port):
        self._upstream_port = upstream_port
        self.connect_lines = []
        self.client_bytes = b""
        self._server = _Server(self._handle)
        self.port = self._server.port

    def _handle(self, conn):
        data = _read_head(conn)
        head, _, rest = data.partition(b"\r\n\r\n")
        self.connect_lines.append(_parse_head(head)[0])
        self.client_bytes += data
        upstream = socket.create_connection(("127.0.0.1", self._upstream_port), timeout=5)
        try:
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            back = threading.Thread(target=self._pump, args=(upstream, conn, False), daemon=True)
            back.start()
            self._pump(conn, upstream, True)
            back.join(5)
        finally:
            upstream.close()

    def _pump(self, src, dst, record):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if record:
                    self.client_bytes += data
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def close(self):
        self._server.close()


# ---------------------------------------------------------------------------
# Isolation and seams
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _offline(routes=None, lookups=None, no_proxy="*"):
    """Fake DNS for the test hosts, and refuse every connect that isn't loopback.

    *routes* maps (host, port) to a loopback port. Numeric loopback addresses
    resolve normally; every other name fails as unknown, so a test can never
    reach a real host even by mistake.
    """
    routes = dict(routes or {})
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if lookups is not None:
            lookups.append((host, port))
        try:
            key = (str(host).lower(), int(port))
        except (TypeError, ValueError):
            key = None
        if key in routes:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", routes[key]))]
        if host in LOOPBACK:
            return real_getaddrinfo(host, port, family, type, proto, flags)
        raise socket.gaierror(socket.EAI_NONAME, "test DNS: %r is not a loopback test host" % (host,))

    def guarded_connect(sock, address):
        if not (isinstance(address, tuple) and address and address[0] in LOOPBACK):
            raise OSError("test guard refused a non-loopback connect to %r" % (address,))
        return real_connect(sock, address)

    # urllib's ftp and file handlers look names up with gethostbyname, which
    # the fake getaddrinfo above does not cover: refuse those lookups too.
    real_gethostbyname = socket.gethostbyname

    def fake_gethostbyname(host):
        if lookups is not None:
            lookups.append((host, None))
        if host in LOOPBACK:
            return real_gethostbyname(host)
        raise socket.gaierror(socket.EAI_NONAME, "test DNS: %r is not a loopback test host" % (host,))

    def fake_gethostbyname_ex(host):
        return host, [], [fake_gethostbyname(host)]

    with mock.patch.object(socket, "getaddrinfo", fake_getaddrinfo), \
            mock.patch.object(socket, "gethostbyname", fake_gethostbyname), \
            mock.patch.object(socket, "gethostbyname_ex", fake_gethostbyname_ex), \
            mock.patch.object(socket.socket, "connect", guarded_connect), \
            mock.patch.dict(os.environ, {"no_proxy": no_proxy}):
        yield


@contextlib.contextmanager
def _token_opener(*extra_handlers, trust=None):
    """Lend the module an opener built exactly like ``_safe_opener``.

    With *trust*, the opener trusts that CA file too: the stdlib's default
    context factory adds it while the opener is built (and, on Pythons whose
    stock handler builds its context per connection, while it is used). Only
    contexts made inside this block see the CA.
    """
    with contextlib.ExitStack() as stack:
        if trust is not None:
            real_factory = ssl._create_default_https_context

            def factory(*args, **kwargs):
                ctx = real_factory(*args, **kwargs)
                ctx.load_verify_locations(cafile=trust)
                return ctx

            stack.enter_context(mock.patch.object(ssl, "_create_default_https_context", factory))
        opener = urllib.request.build_opener(*(extra_handlers + (cs._NoRedirectHandler,
                                                                 cs._BoundedHTTPSHandler())))
        stack.enter_context(mock.patch.object(cs, "_safe_opener", opener))
        yield opener


@contextlib.contextmanager
def _spy_connector():
    """Record every address the bounded connector is asked to connect to."""
    calls = []
    real = cs._bounded_connector

    def spy(deadline):
        connect = real(deadline)

        def recording(address, *args, **kwargs):
            calls.append(tuple(address))
            return connect(address, *args, **kwargs)
        return recording

    with mock.patch.object(cs, "_bounded_connector", spy):
        yield calls


def _usage_request(timeout=5):
    """The usage call as fetch_usage makes it; returns (status, body)."""
    with cs._authorized_request(USAGE_URL, FAKE_TOKEN, headers={"Accept": "application/json"},
                                timeout=timeout) as resp:
        return resp.status, resp.read()


def _close_quietly(exc):
    try:
        exc.close()
    except Exception:
        pass


def _release_http_error(exc):
    """Close the file under an HTTPError a test made, if there is one.

    It goes past the error's own close(), which a fake may make raise. On 3.8
    and 3.9 an HTTPError made without a body has no file under it (its
    addinfourl init is skipped), so there is nothing to close, and
    ``HTTPError.close`` there would raise KeyError('file'). From 3.10 one is
    always there, an empty BytesIO at least, and 3.14 warns if it is left
    open.
    """
    if "file" in vars(exc):
        urllib.error.HTTPError.close(exc)


# Far past what a loopback failure takes (milliseconds), far short of the
# sandboxed runner's 300 s kill.
_BLOCKING_CALL_WATCHDOG = 20  # seconds


def _finish_or_fail(test, call, seconds=_BLOCKING_CALL_WATCHDOG, on_hang=None):
    """Run *call* on a daemon thread and hand back its result or its exception.

    For a call that deliberately has no timeout of its own: if it is still
    running after *seconds*, *on_hang* gets one chance to unblock it, and the
    test fails then and there instead of hanging the whole run.
    """
    outcome = []

    def run():
        try:
            outcome.append((True, call()))
        except BaseException as exc:  # re-raised on the test's own thread below
            outcome.append((False, exc))

    worker = threading.Thread(target=run, daemon=True, name="test-watchdog-call")
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        if on_hang is not None:
            on_hang()
        test.fail("still blocked after %ss: a call with no timeout of its own is waiting "
                  "on a peer that never answered" % seconds)
    ok, value = outcome[0]
    if ok:
        return value
    raise value


class _LoopbackTest(unittest.TestCase):
    def loopback(self, cert="api", no_proxy="*"):
        pki = _require_pki(self)
        origin = _TLSOrigin(pki.cert(cert), pki.key)
        self.addCleanup(origin.close)
        plain = _PlainOrigin()
        self.addCleanup(plain.close)
        lookups = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_offline({(API_HOST, 443): origin.port, (API_HOST, 80): plain.port},
                                     lookups, no_proxy))
        return SimpleNamespace(pki=pki, origin=origin, plain=plain, lookups=lookups)


# ---------------------------------------------------------------------------
# TLS on the token request
# ---------------------------------------------------------------------------

class _StopAtConnection(Exception):
    """Raised by a stand-in connection class once it has seen its arguments."""


@unittest.skipUnless(HAVE_SSL, "this Python has no ssl module")
class TLSSettingsParityTest(unittest.TestCase):
    """The connection gets exactly the TLS settings a stock handler would give it.

    On 3.12+ the stock handler builds its context once, when it is built, and
    hands that to every connection; on 3.8-3.11 it hands over context and
    check_hostname as given (None by default). Anything less leaves the
    connection to build its own context from a process-wide global, every
    time it connects.
    """

    def _connection_kwargs(self, handler):
        seen = {}

        class Recorder:
            def __init__(self, host, **kwargs):
                seen.update(kwargs)
                raise _StopAtConnection()

        req = urllib.request.Request(USAGE_URL)
        req.timeout = 5
        with mock.patch.object(http.client, "HTTPSConnection", Recorder):
            with self.assertRaises(_StopAtConnection):
                handler.https_open(req)
        return seen

    def test_token_handler_passes_the_same_tls_settings_as_the_stock_handler(self):
        stock = self._connection_kwargs(urllib.request.HTTPSHandler())
        handler = cs._BoundedHTTPSHandler()
        ours = self._connection_kwargs(handler)
        self.assertEqual(sorted(ours), sorted(stock))
        self.assertIs(ours.get("context"), handler._context,
                      "the handler's own context must reach the connection")
        if "check_hostname" in stock:
            self.assertEqual(ours["check_hostname"], handler._check_hostname)
        self.assertEqual(ours["timeout"], 5)


class TokenRequestTLSTest(_LoopbackTest):
    def test_untrusted_certificate_is_refused_before_the_token_is_sent(self):
        world = self.loopback(cert="rogue_api")
        with self.assertRaises(urllib.error.URLError) as caught:
            _usage_request()
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(world.origin.requests, [], "the server must never see the request")

    def test_a_later_unverified_context_hack_cannot_weaken_the_token_request(self):
        """The copy-pasted "fix" for certificate errors, applied after import.

        A stock handler built at import (3.6.0's token opener) keeps verifying;
        the swapped-in handler must too, or the token goes to whoever answers.
        """
        if urllib.request.HTTPSHandler()._context is None:
            self.skipTest("on Python %d.%d the stdlib's own HTTPSHandler builds a context "
                          "per connection from the global, so the token opener matches it; "
                          "the parity test covers this version" % sys.version_info[:2])
        world = self.loopback(cert="rogue_api")
        real = ssl._create_default_https_context
        ssl._create_default_https_context = ssl._create_unverified_context
        try:
            with self.assertRaises(urllib.error.URLError) as caught:
                _usage_request()
        finally:
            ssl._create_default_https_context = real
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(world.origin.requests, [], "the token reached an untrusted server")

    def test_trusted_certificate_gets_the_token_through_the_bounded_connect(self):
        world = self.loopback(cert="api")
        with _token_opener(trust=world.pki.ca), _spy_connector() as connects:
            self.assertEqual(_usage_request(), (200, b"{}"))
        self.assertEqual(connects, [(API_HOST, 443)])
        self.assertEqual(world.origin.sni, [API_HOST])
        self.assertEqual(len(world.origin.requests), 1)
        self.assertEqual(world.origin.requests[0]["host"], API_HOST)
        self.assertEqual(world.origin.requests[0]["authorization"], "Bearer " + FAKE_TOKEN)

    def test_valid_certificate_for_another_host_is_refused(self):
        world = self.loopback(cert="evil")
        with _token_opener(trust=world.pki.ca):
            with self.assertRaises(urllib.error.URLError) as caught:
                _usage_request()
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(world.origin.sni, [API_HOST])
        self.assertEqual(world.origin.requests, [])


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------

class TokenRedirectTest(_LoopbackTest):
    def test_redirect_to_another_domain_is_blocked_before_it_is_looked_up(self):
        world = self.loopback()
        world.origin.responses = [_redirect("https://evil.example/steal")]
        with _token_opener(trust=world.pki.ca):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _usage_request()
        _close_quietly(caught.exception)
        self.assertEqual((caught.exception.code, caught.exception.reason), (302, BLOCKED))
        self.assertNotIn("evil.example", [host for host, _port in world.lookups])

    def test_redirect_to_plain_http_on_an_allowed_host_is_blocked(self):
        """The stdlib copies Authorization onto the redirected request, so
        following an https -> http hop would send the token in plain text."""
        for location in ("http://api.anthropic.com/api/oauth/usage",
                         "HTTP://API.ANTHROPIC.COM/api/oauth/usage"):
            with self.subTest(location=location):
                world = self.loopback()
                world.origin.responses = [_redirect(location)]
                with _token_opener(trust=world.pki.ca):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        _usage_request()
                _close_quietly(caught.exception)
                self.assertEqual((caught.exception.code, caught.exception.reason), (302, BLOCKED))
                self.assertEqual(world.plain.accepted, 0, "the plaintext listener was reached")
                self.assertEqual(world.plain.requests, [])
                self.assertNotIn((API_HOST, 80), world.lookups)

    def test_redirect_to_https_on_an_allowed_host_is_still_followed(self):
        for location in ("https://api.anthropic.com/api/oauth/usage?again=1",
                         "/api/oauth/usage?again=1"):
            with self.subTest(location=location):
                world = self.loopback()
                world.origin.responses = [_redirect(location)]
                with _token_opener(trust=world.pki.ca):
                    self.assertEqual(_usage_request(), (200, b"{}"))
                self.assertEqual(len(world.origin.requests), 2)
                self.assertIn("again=1", world.origin.requests[1]["line"])
                self.assertEqual(world.origin.requests[1]["authorization"], "Bearer " + FAKE_TOKEN)


# ---------------------------------------------------------------------------
# The starting URL: https only
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _requests_built():
    """Record the URL of every urllib ``Request`` made while this is active."""
    built = []
    real = urllib.request.Request

    class Recording(real):
        def __init__(self, url, *args, **kwargs):
            built.append(url)
            super().__init__(url, *args, **kwargs)

    with mock.patch.object(urllib.request, "Request", Recording):
        yield built


class TokenSchemeTest(unittest.TestCase):
    """The token (and the refresh token in a refresh's body) travel only over https.

    The domain allowlist alone let ``http://api.anthropic.com/...`` through,
    and the stock HTTPHandler then sends the bearer token in plain text to
    whatever answers on port 80, or to an http_proxy. Redirects were already
    https-only; the starting URL must be too, and it must be refused before a
    request carrying the token exists, before any lookup, before any socket,
    as the ValueError every caller already handles.
    """

    def setUp(self):
        self.plain = _PlainOrigin()
        self.addCleanup(self.plain.close)
        self.lookups = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        routes = {(host, 80): self.plain.port for host in cs._TOKEN_ALLOWED_DOMAINS}
        stack.enter_context(_offline(routes, self.lookups))
        self.built = stack.enter_context(_requests_built())

    def _attempt(self, url, token=FAKE_TOKEN, **kwargs):
        """Call ``_authorized_request``; return what it raised (or None), and
        what the plaintext listener accepted and received meanwhile."""
        del self.lookups[:], self.built[:]
        accepted, received = self.plain.accepted, len(self.plain.requests)
        try:
            resp = cs._authorized_request(url, token, timeout=2, **kwargs)
        except Exception as exc:
            outcome = exc
        else:
            with resp:
                resp.read()
            outcome = None
        return outcome, (self.plain.accepted - accepted, self.plain.requests[received:])

    def assertRefusedUpFront(self, url, outcome, plaintext):
        accepted, received = plaintext
        self.assertEqual(accepted, 0, "%s reached the plaintext listener" % url)
        self.assertEqual(received, [], "the token went out in plain text")
        self.assertEqual(self.lookups, [], "%s got as far as a DNS lookup" % url)
        self.assertEqual(self.built, [], "a request carrying the token was built for %s" % url)
        self.assertIsInstance(outcome, ValueError,
                              "callers handle ValueError; got %r" % (outcome,))
        self.assertNotIn(FAKE_TOKEN, str(outcome), "the error text carried the token")

    def test_plain_http_to_an_allowed_host_is_refused_before_anything_is_sent(self):
        for url in ("http://api.anthropic.com/api/oauth/usage",
                    "HTTP://API.ANTHROPIC.COM/api/oauth/usage",
                    "http://api.anthropic.com:443/api/oauth/usage"):
            with self.subTest(url=url):
                outcome, plaintext = self._attempt(url)
                self.assertRefusedUpFront(url, outcome, plaintext)
                self.assertIn("https", str(outcome))

    def test_the_refresh_token_is_never_posted_over_plain_http(self):
        body = json.dumps({"grant_type": "refresh_token",
                           "refresh_token": "fake-refresh-token-0123"}).encode("utf-8")
        url = "http://platform.claude.com/v1/oauth/token"
        outcome, plaintext = self._attempt(url, token=None, data=body, method="POST",
                                           headers={"Content-Type": "application/json"})
        self.assertRefusedUpFront(url, outcome, plaintext)

    def test_other_schemes_are_refused_the_same_way(self):
        for url in ("ftp://api.anthropic.com/api/oauth/usage",
                    "//api.anthropic.com/api/oauth/usage"):
            with self.subTest(url=url):
                outcome, plaintext = self._attempt(url)
                self.assertRefusedUpFront(url, outcome, plaintext)

    def test_https_on_an_allowed_host_still_goes_out_with_the_token(self):
        seen = []

        class Opener:
            def open(self, req, timeout=None):
                seen.append((req.type, req.get_header("Authorization"), timeout))
                return io.BytesIO(b"{}")

        with mock.patch.object(cs, "_safe_opener", Opener()):
            for url in ("https://api.anthropic.com/api/oauth/usage",
                        "HTTPS://API.ANTHROPIC.COM/api/oauth/usage"):
                cs._authorized_request(url, FAKE_TOKEN, timeout=3)
        self.assertEqual(seen, [("https", "Bearer " + FAKE_TOKEN, 3)] * 2)

    def test_https_to_another_domain_is_still_refused(self):
        outcome, plaintext = self._attempt("https://evil.example/api/oauth/usage")
        self.assertRefusedUpFront("https://evil.example/", outcome, plaintext)
        self.assertIn("evil.example is not an allowed domain", str(outcome))


# ---------------------------------------------------------------------------
# Proxy tunnelling
# ---------------------------------------------------------------------------

class ProxyTunnelTest(_LoopbackTest):
    def _proxied(self, world):
        proxy = _ConnectProxy(world.origin.port)
        self.addCleanup(proxy.close)
        handler = urllib.request.ProxyHandler({"https": "http://127.0.0.1:%d" % proxy.port})
        return proxy, handler

    def test_tunnelled_request_uses_the_bounded_connect_and_verifies_tls(self):
        world = self.loopback(cert="api", no_proxy="no-such-host.invalid")
        proxy, handler = self._proxied(world)
        with _token_opener(handler, trust=world.pki.ca), _spy_connector() as connects:
            self.assertEqual(_usage_request(), (200, b"{}"))
        self.assertEqual(connects, [("127.0.0.1", proxy.port)],
                         "the connect to the proxy must go through the bounded connector")
        self.assertEqual(len(proxy.connect_lines), 1)
        self.assertTrue(proxy.connect_lines[0].startswith("CONNECT api.anthropic.com:443 "),
                        proxy.connect_lines)
        self.assertEqual(world.origin.sni, [API_HOST])
        self.assertEqual(world.origin.requests[0]["authorization"], "Bearer " + FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN.encode(), proxy.client_bytes,
                         "the proxy saw the token in clear text")

    def test_untrusted_certificate_through_the_tunnel_is_refused(self):
        world = self.loopback(cert="rogue_api", no_proxy="no-such-host.invalid")
        proxy, handler = self._proxied(world)
        with _token_opener(handler), _spy_connector() as connects:
            with self.assertRaises(urllib.error.URLError) as caught:
                _usage_request()
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(connects, [("127.0.0.1", proxy.port)])
        self.assertEqual(len(proxy.connect_lines), 1)
        self.assertEqual(world.origin.requests, [])
        self.assertNotIn(FAKE_TOKEN.encode(), proxy.client_bytes)


# ---------------------------------------------------------------------------
# The connector's timeout argument, as socket.create_connection reads it
# ---------------------------------------------------------------------------

class ConnectorTimeoutTest(unittest.TestCase):
    """http.client hands the connector whatever timeout its caller gave:
    a number, None, or (when the caller gave none) the default sentinel."""

    def setUp(self):
        self.listener = _Server(lambda conn: None)
        self.addCleanup(self.listener.close)
        self.address = ("127.0.0.1", self.listener.port)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_offline())

    def _connect(self, deadline, timeout):
        sock = cs._bounded_connector(deadline)(self.address, timeout)
        self.addCleanup(sock.close)
        self.assertEqual(sock.getpeername()[:2], self.address)
        return sock

    def _stock(self, timeout):
        sock = socket.create_connection(self.address, timeout)
        self.addCleanup(sock.close)
        return sock.gettimeout()

    def test_default_sentinel_leaves_the_process_default_like_create_connection(self):
        previous = socket.getdefaulttimeout()
        for default in (None, 7.0):
            with self.subTest(default=default):
                socket.setdefaulttimeout(default)
                try:
                    sock = self._connect(None, socket._GLOBAL_DEFAULT_TIMEOUT)
                    expected = self._stock(socket._GLOBAL_DEFAULT_TIMEOUT)
                finally:
                    socket.setdefaulttimeout(previous)
                self.assertEqual(expected, default)
                self.assertEqual(sock.gettimeout(), expected)

    def test_none_blocks_like_create_connection(self):
        sock = self._connect(None, None)
        self.assertIsNone(self._stock(None))
        self.assertIsNone(sock.gettimeout())

    def test_under_a_deadline_no_timeout_still_cannot_outlive_it(self):
        for timeout in (None, socket._GLOBAL_DEFAULT_TIMEOUT):
            with self.subTest(timeout=timeout):
                sock = self._connect(time.time() + 5, timeout)
                self.assertIsNotNone(sock.gettimeout())
                self.assertGreater(sock.gettimeout(), 4.0, "the deadline, not a fixed fallback")
                self.assertLessEqual(sock.gettimeout(), 5.0)

    def test_a_number_is_still_capped_by_the_deadline(self):
        self.assertEqual(self._connect(time.time() + 5, 2).gettimeout(), 2)
        self.assertLessEqual(self._connect(time.time() + 1, 3).gettimeout(), 1.0)
        self.assertEqual(self._connect(None, 3).gettimeout(), 3)

    def test_the_deadline_caps_each_operation_not_the_whole_exchange(self):
        """What the docstring promises, no more: each read waits at most the
        time that was left at connect, but a peer that answers a little at a
        time, each piece inside that cap, carries the exchange past the
        deadline. Only ``_fetch_bounded`` holds a fetch to a wall clock."""
        pieces = (b"one ", b"two ", b"three ", b"four")
        trickle = _Server(_answer_in_pieces(pieces, 0.3))  # 0.9 s in all
        self.addCleanup(trickle.close)
        began = time.monotonic()
        sock = cs._bounded_connector(time.time() + 0.6)(("127.0.0.1", trickle.port), None)
        self.addCleanup(sock.close)
        sock.sendall(b"GET / HTTP/1.1\r\n\r\n")
        self.assertLessEqual(sock.gettimeout(), 0.6, "no timeout must still be capped")
        received = b""
        while len(received) < len(b"".join(pieces)):
            chunk = sock.recv(64)
            if not chunk:
                break
            received += chunk
        self.assertEqual(received, b"".join(pieces))
        self.assertGreater(time.monotonic() - began, 0.6,
                           "the exchange was meant to outlast the deadline")

    @unittest.skipUnless(HAVE_SSL, "this Python has no ssl module")
    def test_token_opener_without_a_timeout_fails_as_a_url_error(self):
        """OpenerDirector.open() defaults timeout to the sentinel; a TypeError
        there would escape urllib's error wrapping and show as "Data error"."""
        req = urllib.request.Request("https://127.0.0.1:%d/" % self.listener.port)
        with self.assertRaises(urllib.error.URLError):
            # The handshake blocks for as long as the peer stays silent, so a
            # listener that failed to close would hang here: fail fast instead.
            _finish_or_fail(self, lambda: cs._safe_opener.open(req),
                            on_hang=self.listener.close)


class WatchdogHelperTest(unittest.TestCase):
    """``_finish_or_fail`` turns a hang into a quick, named failure."""

    def test_a_call_that_never_returns_fails_the_test_quickly(self):
        release = threading.Event()
        self.addCleanup(release.set)
        unblocked = []
        started = time.monotonic()
        with self.assertRaises(self.failureException) as caught:
            _finish_or_fail(self, lambda: release.wait(60), seconds=0.2,
                            on_hang=lambda: unblocked.append(True))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(unblocked, [True], "on_hang must get its chance to unblock the call")
        self.assertIn("still blocked after 0.2s", str(caught.exception))

    def test_the_result_and_the_exception_come_back_unchanged(self):
        self.assertEqual(_finish_or_fail(self, lambda: 42, seconds=5), 42)
        error = urllib.error.URLError("refused")

        def failing():
            raise error

        with self.assertRaises(urllib.error.URLError) as caught:
            _finish_or_fail(self, failing, seconds=5)
        self.assertIs(caught.exception, error)


# ---------------------------------------------------------------------------
# DNS obeys the deadline
# ---------------------------------------------------------------------------

class _HungResolver:
    """getaddrinfo on a VPN whose DNS servers have gone away."""

    def __init__(self, seconds=20):
        self.release = threading.Event()
        self.seconds = seconds
        self.calls = []

    def __call__(self, host, port, *args, **kwargs):
        self.calls.append((host, port, threading.current_thread().name))
        self.release.wait(self.seconds)
        raise socket.gaierror(socket.EAI_AGAIN, "test resolver gave up")


def _child_env(home):
    """The environment for a child interpreter: a throwaway home, no credentials."""
    env = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.startswith(("PYTHON", "ANTHROPIC_", "CLAUDE_", "USAGE_PULSE_", "CODEX_")):
            continue
        if any(hint in upper for hint in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")):
            continue
        env[name] = value
    env.update({
        "HOME": home,
        "USERPROFILE": home,
        "LOCALAPPDATA": os.path.join(home, "AppData", "Local"),
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"),
        "CODEX_HOME": os.path.join(home, ".codex"),
        "CLAUDE_PULSE_NONINTERACTIVE": "1",
    })
    for var in ("LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        os.makedirs(env[var], exist_ok=True)
    return env


class ResolverDeadlineTest(unittest.TestCase):
    """getaddrinfo has no timeout of its own; the connector must supply one."""

    def _hung(self):
        resolver = _HungResolver()
        self.addCleanup(resolver.release.set)  # let the abandoned lookup finish
        return resolver

    def test_hung_dns_cannot_outlast_the_token_request_timeout(self):
        resolver = self._hung()
        with _offline(), mock.patch.object(socket, "getaddrinfo", resolver):
            started = time.monotonic()
            with self.assertRaises(urllib.error.URLError) as caught:
                cs._authorized_request(USAGE_URL, FAKE_TOKEN, timeout=1)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.9, "a hung lookup held the token request for %.1fs" % elapsed)
        self.assertIsInstance(caught.exception.reason, socket.timeout)

    def test_connector_gives_up_on_dns_at_the_deadline_without_opening_a_socket(self):
        resolver = self._hung()
        opened = []

        def no_socket(*args, **kwargs):
            opened.append(args)
            raise AssertionError("a socket was opened before DNS answered")

        with _offline(), mock.patch.object(socket, "getaddrinfo", resolver), \
                mock.patch.object(socket, "socket", no_socket):
            started = time.monotonic()
            with self.assertRaises(socket.timeout):
                cs._bounded_connector(time.time() + 0.5)((API_HOST, 443), 3)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.2)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(opened, [])

    def test_spent_budget_does_not_start_a_lookup(self):
        resolver = self._hung()
        with _offline(), mock.patch.object(socket, "getaddrinfo", resolver):
            started = time.monotonic()
            with self.assertRaises(OSError):
                cs._bounded_connector(time.time() - 1)((API_HOST, 443), 3)
            elapsed = time.monotonic() - started
        self.assertEqual(resolver.calls, [])
        self.assertLess(elapsed, 0.5)

    def test_dns_failure_comes_back_unchanged(self):
        error = socket.gaierror(socket.EAI_NONAME, "no such host")

        def failing(*args, **kwargs):
            raise error

        with _offline(), mock.patch.object(socket, "getaddrinfo", failing):
            with self.assertRaises(socket.gaierror) as caught:
                cs._bounded_connector(time.time() + 2)((API_HOST, 443), 3)
        self.assertIs(caught.exception, error)

    def test_dns_answer_within_the_deadline_still_connects(self):
        listener = _Server(lambda conn: None)
        self.addCleanup(listener.close)
        with _offline({(API_HOST, 443): listener.port}):
            sock = cs._bounded_connector(time.time() + 2)((API_HOST, 443), 3)
        self.addCleanup(sock.close)
        self.assertEqual(sock.getpeername()[:2], ("127.0.0.1", listener.port))

    def test_without_a_deadline_dns_runs_on_the_calling_thread(self):
        listener = _Server(lambda conn: None)
        self.addCleanup(listener.close)
        threads = []
        with _offline({(API_HOST, 443): listener.port}):
            real = socket.getaddrinfo

            def recording(*args, **kwargs):
                threads.append(threading.current_thread())
                return real(*args, **kwargs)

            with mock.patch.object(socket, "getaddrinfo", recording):
                sock = cs._bounded_connector(None)((API_HOST, 443), 3)
        self.addCleanup(sock.close)
        self.assertEqual(threads, [threading.current_thread()])

    def test_an_abandoned_lookup_does_not_keep_the_process_alive(self):
        code = textwrap.dedent("""
            import socket, sys, time
            sys.path.insert(0, sys.argv[1])
            import claude_status as cs

            def hang(*args, **kwargs):
                time.sleep(30)
                raise socket.gaierror("too late")

            socket.getaddrinfo = hang
            started = time.monotonic()
            try:
                cs._bounded_connector(time.time() + 0.3)(("api.anthropic.com", 443), 3)
            except OSError as exc:
                print("gave up: %s after %.2fs" % (type(exc).__name__, time.monotonic() - started))
        """)
        with tempfile.TemporaryDirectory() as home:
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-I", "-c", code, str(REPO_ROOT)],
                                    env=_child_env(home), cwd=home, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=20)
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gave up: ", result.stdout)
        self.assertLess(elapsed, 10, "the blocked lookup thread kept the process alive")


# ---------------------------------------------------------------------------
# Error answers that _fetch_bounded swallows are closed, not left to GC
# ---------------------------------------------------------------------------

FORBIDDEN_BODY = b'{"error": {"type": "forbidden"}}'
FORBIDDEN_PIECES = (
    b"HTTP/1.1 403 Forbidden\r\n",
    b"Content-Type: application/json\r\n",
    b"Content-Length: %d\r\n" % len(FORBIDDEN_BODY),
    b"Connection: close\r\n\r\n" + FORBIDDEN_BODY,
)


def _answer_in_pieces(pieces, gap):
    """A handler that reads the request, then sends *pieces* *gap* seconds apart."""
    def handle(conn):
        _read_head(conn)
        for index, piece in enumerate(pieces):
            if index:
                time.sleep(gap)
            conn.sendall(piece)
    return handle


@contextlib.contextmanager
def _threads_started():
    """Record every thread started while this is active."""
    started = []
    real = threading.Thread

    class Recording(real):
        def start(self):
            started.append(self)
            return super().start()

    with mock.patch.object(threading, "Thread", Recording):
        yield started


@contextlib.contextmanager
def _error_responses_seen():
    """Keep each HTTPError that ``_open_and_read`` raises, and when it was raised.

    It is re-raised unchanged, so ``_fetch_bounded``'s worker still gets it.
    """
    seen = []
    real = cs._open_and_read

    def spy(req, max_bytes, deadline):
        try:
            return real(req, max_bytes, deadline)
        except urllib.error.HTTPError as exc:
            seen.append((exc, time.monotonic()))
            raise

    with mock.patch.object(cs, "_open_and_read", spy):
        yield seen


def _py38_http_error_init(self, url, code, msg, hdrs, fp):
    """``HTTPError.__init__`` as Python 3.8 and 3.9 have it (Lib/urllib/error.py).

    With fp=None it skips the addinfourl (file wrapper) init, so the error has
    no file under it and its own close() raises KeyError('file'). 3.10 and
    later wrap an empty BytesIO instead. Patched in, it lets any Python build
    an error the way 3.8 and 3.9 do.
    """
    self.code = code
    self.msg = msg
    self.hdrs = hdrs
    self.fp = fp
    self.filename = url
    if fp is not None:
        urllib.response.addinfourl.__init__(self, fp, hdrs, url, code)


class _UnclosableHTTPError(urllib.error.HTTPError):
    """An error whose own close() raises, as a 3.8/3.9 one made without a body does."""

    def close(self):
        raise KeyError("file")


class FetchBoundedErrorResponseTest(unittest.TestCase):
    """An HTTP error answer is still a live response, and the worker owns it.

    urllib raises an error status as HTTPError, which is also the open
    response: its socket and its unread body. ``_fetch_bounded`` turns every
    failure into None, so its worker is the last code that can close it; left
    to garbage collection the socket stays open until then, and Python 3.14
    says so: "ResourceWarning: Implicitly cleaning up <HTTPError 403 ...>".
    The update checks hit exactly this whenever GitHub rate-limits (403).
    """

    def serve(self, gap=0.0):
        server = _Server(_answer_in_pieces(FORBIDDEN_PIECES, gap))
        self.addCleanup(server.close)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_offline())
        return urllib.request.Request("http://127.0.0.1:%d/check" % server.port,
                                      headers={"User-Agent": "usage-pulse-test"})

    def fetch(self, req, budget=None):
        """``_fetch_bounded``, then a bounded wait for its worker to finish."""
        with _threads_started() as started:
            began = time.monotonic()
            body = cs._fetch_bounded(req, 1000, budget=budget)
            returned = time.monotonic()
        for thread in started:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "the fetch worker never finished")
        return body, began, returned

    def seen(self, errors):
        for exc, _when in errors:
            self.addCleanup(_close_quietly, exc)
        return [exc.code for exc, _when in errors]

    def test_a_403_leaves_no_resource_warning(self):
        """On 3.14+ the unclosed answer warns; earlier Pythons stay silent
        about it, which the next test covers."""
        req = self.serve()
        gc.collect()  # garbage from earlier tests must not be blamed on this one
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            body, _began, _returned = self.fetch(req)
            gc.collect()
        self.assertIsNone(body)
        leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [], "an error answer was left for garbage collection")

    def test_a_403_is_closed_by_the_worker_that_swallows_it(self):
        req = self.serve()
        with _error_responses_seen() as errors:
            body, _began, _returned = self.fetch(req)
        self.assertIsNone(body)
        self.assertEqual(self.seen(errors), [403])
        self.assertTrue(errors[0][0].fp.isclosed(), "the 403 answer (its socket) was left open")

    def test_a_403_that_arrives_after_the_ceiling_is_closed_too(self):
        """Each piece lands inside the socket timeout, so the answer does
        come, just after ``_fetch_bounded`` has already given up on it."""
        req = self.serve(gap=0.3)
        with _error_responses_seen() as errors:
            body, began, returned = self.fetch(req, budget=0.6)
        self.assertIsNone(body)
        self.assertLess(returned - began, 1.2, "the wall-clock ceiling no longer holds")
        self.assertEqual(self.seen(errors), [403], "the late 403 never reached the worker")
        self.assertGreater(errors[0][1], returned, "the 403 came before the ceiling")
        self.assertTrue(errors[0][0].fp.isclosed(), "the late 403 answer was left open")

    def test_open_and_read_still_hands_the_http_error_to_its_caller(self):
        """Closing is the swallower's job: the transport itself still raises
        the error, readable, for any caller that wants its code or body."""
        req = self.serve()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            cs._open_and_read(req, 1000, time.time() + 5)
        self.addCleanup(_close_quietly, caught.exception)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(caught.exception.read(), FORBIDDEN_BODY)

    def test_an_error_that_cannot_be_closed_is_still_just_no_answer(self):
        """On 3.8 and 3.9 an HTTPError made without a body has no file under
        it, and its close() raises; that must not escape the worker thread."""
        made = []

        def failing(req, max_bytes, deadline):
            made.append(_UnclosableHTTPError(req.full_url, 403, "Forbidden", {}, None))
            raise made[-1]

        escaped = []
        self.addCleanup(lambda: [_release_http_error(exc) for exc in made])
        with mock.patch.object(cs, "_open_and_read", failing), \
                mock.patch.object(threading, "excepthook",
                                  lambda args: escaped.append(args.exc_type)):
            body, _began, _returned = self.fetch(urllib.request.Request("http://127.0.0.1:9/"))
        self.assertIsNone(body)
        self.assertEqual(len(made), 1)
        self.assertEqual(escaped, [], "an exception escaped the fetch worker")


class HttpErrorCleanupTest(unittest.TestCase):
    """A test's clean-up of an HTTPError it made works on every supported Python.

    The test owns whatever file is under the error. Closing that file must not
    go through the error's own close(), which a fake may make raise, nor
    assume a file is there: on 3.8 and 3.9 one made without a body has none.
    """

    def test_the_guard_test_passes_with_the_python_38_and_39_constructor(self):
        inner = FetchBoundedErrorResponseTest(
            "test_an_error_that_cannot_be_closed_is_still_just_no_answer")
        result = unittest.TestResult()
        with mock.patch.object(urllib.error.HTTPError, "__init__", _py38_http_error_init):
            inner.run(result)
        self.assertEqual((result.testsRun, result.skipped), (1, []))
        problems = [report for _test, report in result.errors + result.failures]
        self.assertEqual(problems, [], "the guard test fails or errors on 3.8 and 3.9")

    def test_the_body_is_closed_even_when_the_errors_own_close_raises(self):
        body = io.BytesIO(FORBIDDEN_BODY)
        # Held until the assert: freeing the error would let the stdlib's own
        # finalizer close the body and hide a helper that left it open.
        exc = _UnclosableHTTPError("http://127.0.0.1:9/", 403, "Forbidden", {}, body)
        _release_http_error(exc)
        self.assertTrue(body.closed, "the body under the error was left open")


# ---------------------------------------------------------------------------
# A Python built without ssl
# ---------------------------------------------------------------------------

_NOSSL_CHILD = textwrap.dedent(r"""
    import importlib.abc, importlib.util, socket, sys, time

    class _NoSSL(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name in ("ssl", "_ssl"):
                raise ImportError("simulated: this Python was built without OpenSSL")
            return None

    sys.meta_path.insert(0, _NoSSL())
    sys.dont_write_bytecode = True
    for name in ("ssl", "_ssl"):
        sys.modules.pop(name, None)

    def note(text):
        sys.stderr.write("PROBE " + text + "\n")
        sys.stderr.flush()

    def refuse(*args, **kwargs):
        note("network-attempt")  # urllib would wrap this OSError as a URLError too
        raise OSError("test guard: no network in this test")

    socket.getaddrinfo = refuse
    socket.create_connection = refuse
    socket.socket.connect = refuse

    import urllib.error, urllib.request
    note("https-handler-present=%s" % hasattr(urllib.request, "HTTPSHandler"))

    spec = importlib.util.spec_from_file_location("claude_status_nossl", sys.argv[1])
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    note("loaded")

    # A sandboxed HOME doesn't hide the macOS Keychain, which the stdin path
    # reads for the plan name: never let a test reach real credentials.
    cs.get_credentials = lambda *args, **kwargs: (None, None)
    cs.check_for_update = lambda *args, **kwargs: False
    cs.check_claude_code_update = lambda *args, **kwargs: False
    cs.main()
    sys.stdout.flush()

    try:
        cs._authorized_request("https://api.anthropic.com/api/oauth/usage", "fake-token", timeout=1)
        note("authorized=no-error")
    except urllib.error.URLError as exc:
        note("authorized=URLError: %s" % (exc.reason,))
    try:
        cs._open_and_read(urllib.request.Request("https://registry.npmjs.org/x"), 10, time.time() + 1)
        note("open_and_read=no-error")
    except urllib.error.URLError as exc:
        note("open_and_read=URLError: %s" % (exc.reason,))
    note("fetch_bounded=%r" % (cs._fetch_bounded(urllib.request.Request("https://registry.npmjs.org/x"), 10),))
""")


class WithoutSslTest(unittest.TestCase):
    """Since Claude Code 2.1.80 the bar draws from stdin with no network at all,
    so a Python without ssl must still load the script and draw it; only the
    HTTPS calls fail, at run time, as the URLError every caller handles."""

    @classmethod
    def setUpClass(cls):
        now = int(time.time())
        payload = json.dumps({"model": {"display_name": "Fable 5.1"}, "rate_limits": {
            "five_hour": {"used_percentage": 17, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 2, "resets_at": now + 86400}}})
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [sys.executable, "-I", "-c", _NOSSL_CHILD, str(REPO_ROOT / "claude_status.py")],
                input=payload.encode("utf-8"), env=_child_env(home), cwd=home,
                capture_output=True, timeout=60)
        cls.returncode = result.returncode
        cls.stdout = result.stdout.decode("utf-8", "replace")
        cls.stderr = result.stderr.decode("utf-8", "replace")
        cls.notes = [line[len("PROBE "):].strip() for line in cls.stderr.splitlines()
                     if line.startswith("PROBE ")]

    def test_the_simulation_really_removes_https(self):
        self.assertIn("https-handler-present=False", self.notes, self.stderr)

    def test_the_script_loads_and_draws_the_bar_from_stdin(self):
        self.assertEqual(self.returncode, 0, self.stderr[-2000:])
        self.assertIn("loaded", self.notes)
        self.assertIn("Session", self.stdout)

    def test_https_calls_fail_at_run_time_as_a_url_error(self):
        """Refused before any socket: urllib has no handler for https at all."""
        self.assertEqual(self.returncode, 0, self.stderr[-2000:])
        self.assertIn("authorized=URLError: unknown url type: https", self.notes)
        self.assertIn("open_and_read=URLError: unknown url type: https", self.notes)
        self.assertIn("fetch_bounded=None", self.notes)

    def test_nothing_reaches_for_the_network(self):
        self.assertEqual(self.returncode, 0, self.stderr[-2000:])
        self.assertNotIn("network-attempt", self.notes)


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------

class ImportHygieneTest(unittest.TestCase):
    def test_http_client_is_neither_imported_nor_used(self):
        """urllib does all the HTTP; the bounded handler reaches http.client
        only through the connection class urllib hands it."""
        tree = ast.parse((REPO_ROOT / "claude_status.py").read_text(encoding="utf-8"))
        imported, used = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [(node.lineno, alias.name) for alias in node.names
                             if alias.name.split(".")[0] == "http"]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and (node.module or "").split(".")[0] == "http":
                    imported.append((node.lineno, node.module))
            elif isinstance(node, ast.Name) and node.id == "http":
                used.append(node.lineno)
        self.assertEqual(used, [], "these lines use the name http, which nothing binds")
        self.assertEqual(imported, [], "an unused import of http")
        self.assertFalse(hasattr(cs, "http"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
