"""HTTP-layer concurrency tests for the bounded ThreadingHTTPServer.

Prove-the-bug-first: `ergo/contradiction/server.py` used to run a plain
`HTTPServer`, which processes exactly ONE request at a time across ALL
tenants -- a slow guarded write (the LLM normalizer call inside
/remember//learn//supersede, ~1-3s) head-of-line-blocked every other request,
including another tenant's /health. On that old server this file's core test
(`test_head_of_line_non_blocking_...`) would take ~1s for tenant B's calls too
(fully serialized behind tenant A's sleep) instead of the <300ms this test
asserts -- so it is a real regression proof, not a no-op.

Threading note (same as test_multitenancy.py): boot() runs on the server's
own background thread, before the socket starts accepting -- only the actual
HTTP requests made by tests run concurrently against the now-
ThreadingHTTPServer-backed handler. The two cross-tenant tests below drive
two distinct `org_id` scopes through the SAME legacy token rather than
provisioning two real tenants (machinery this build doesn't have) -- their subject is the HTTP
layer's concurrency behavior (no head-of-line blocking, no cross-scope data
leakage), which is identical either way since both scopes land in the same
pooled `_STORE` regardless of how the caller is authenticated; provisioning
was only ever a vehicle for "two independent scopes", never the point of the
test.
"""
from __future__ import annotations

import socket
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
import pytest

from ergo.contradiction import server
from ergo.contradiction.normalize import RuleNormalizer
from tests.contradiction.conftest import start_server_thread

LEGACY_TOKEN = "legacy-secret"
LEGACY_HDR = {"Authorization": f"Bearer {LEGACY_TOKEN}"}


@pytest.fixture
def start_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ERGO_DB_PATH", str(tmp_path / "ergo.db"))
    monkeypatch.setenv("ERGO_API_TOKEN", LEGACY_TOKEN)
    monkeypatch.delenv("ERGO_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ERGO_NLI", "off")
    monkeypatch.setenv("ERGO_RETRIEVAL", "off")

    started: list[ThreadingHTTPServer] = []

    def _start():
        def _boot_and_bind():
            server.boot()
            # Use the production server class (finding #1) so these
            # tests exercise the real admission bound, not a plain/naive
            # ThreadingHTTPServer.
            httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            httpd.daemon_threads = True
            return httpd

        httpd = start_server_thread(_boot_and_bind)
        started.append(httpd)
        _start.httpd = httpd  # exposed for tests that need direct access
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def test_start_server_fixture_surfaces_the_real_boot_exception_not_a_timeout(
        start_server, monkeypatch):
    """If server.boot() raises inside the server thread, start_server() must
    re-raise that exact exception (chained via `from`) instead of reporting
    the generic 'server thread failed to start' timeout message -- otherwise
    a genuine boot error and a slow-CI timeout are indistinguishable from
    the failure text alone."""
    boom = RuntimeError("synthetic boot failure for this test")
    monkeypatch.setattr(server, "boot", lambda: (_ for _ in ()).throw(boom))

    with pytest.raises(RuntimeError, match="synthetic boot failure for this test") as exc_info:
        start_server()
    assert exc_info.value.__cause__ is boom


def test_start_server_fixture_surfaces_system_exit_instead_of_keyerror(
        start_server, monkeypatch):
    """A fail-closed boot SystemExit must be reported as the startup cause,
    not masked by the helper's missing result entry."""
    monkeypatch.setattr(server, "boot", lambda: (_ for _ in ()).throw(SystemExit(1)))

    with pytest.raises(RuntimeError, match=r"SystemExit\(1\)") as exc_info:
        start_server()
    assert isinstance(exc_info.value.__cause__, SystemExit)


# --------------------------------------------------------------------------
# 1. Head-of-line non-blocking (the core proof).
# --------------------------------------------------------------------------
def test_head_of_line_non_blocking_slow_write_does_not_block_other_tenant(
        start_server, monkeypatch):
    """While tenant A is parked mid-write, tenant B's /health + reads must
    still be fully serviced concurrently.

    Deterministic rendezvous rather than a wall-clock threshold: tenant A's
    guarded write parks inside the (monkeypatched) normalizer on an Event and
    only returns once tenant B has proven it can be served. On the bounded
    ThreadingHTTPServer each request owns its own thread, so B completes
    immediately and releases A. On the OLD single-threaded HTTPServer A's
    parked handler owns the only thread, so B can never be serviced -- its
    requests time out and this test goes red. Being pure happens-while
    ordering (no latency budget), it is immune to CI-runner CPU starvation
    that previously flaked an absolute/relative timing ceiling."""
    orig_normalize = RuleNormalizer.normalize
    a_parked = threading.Event()   # set once A is stuck inside its guarded write
    b_served = threading.Event()   # set once B has been fully serviced

    def _slow_normalize(self, claim, context=None):
        a_parked.set()
        # Park until B proves concurrent service; bounded so a regressed
        # (serial) server can't hang the suite -- it unblocks and B has by
        # then already timed out, so the assertions below still fail red.
        b_served.wait(timeout=10)
        return orig_normalize(self, claim, context=context)

    monkeypatch.setattr(RuleNormalizer, "normalize", _slow_normalize)

    url = start_server()

    a_result: dict = {}

    def _fire_a():
        r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                       json={"org_id": "org-a", "project": "p", "who": "alice",
                             "statement": "Org A must hold.",
                             "reason": "r"}, timeout=15)
        a_result["status"] = r.status_code

    ta = threading.Thread(target=_fire_a)
    ta.start()
    assert a_parked.wait(timeout=10), "tenant A never entered its guarded write"

    # A is now parked inside its write. On a serial server these would block
    # until A returns (which waits on b_served -> deadlock -> httpx timeout).
    b_status: dict = {}
    try:
        b_status["health"] = httpx.get(f"{url}/health", timeout=5).status_code
        b_status["active"] = httpx.get(
            f"{url}/active", headers=LEGACY_HDR,
            params={"org_id": "org-b", "project": "p"}, timeout=5).status_code
    except httpx.TimeoutException as exc:  # serial-server regression
        b_served.set()
        ta.join(timeout=15)
        pytest.fail(f"tenant B was head-of-line blocked behind tenant A's "
                    f"parked guarded write: {exc!r}")
    b_served.set()
    ta.join(timeout=15)

    assert a_result["status"] == 200, a_result
    assert b_status["health"] == 200
    assert b_status["active"] == 200


# --------------------------------------------------------------------------
# 2. Cross-tenant parallel writes over HTTP land only in their own org.
# --------------------------------------------------------------------------
def test_cross_tenant_parallel_http_writes_land_only_in_their_own_org(start_server):
    url = start_server()

    errors: list[Exception] = []

    def _write_many(org_id, n):
        try:
            for i in range(n):
                r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                               json={"org_id": org_id, "project": "p", "who": "w",
                                     "statement": f"claim {i} must hold.",
                                     "reason": "r"}, timeout=10)
                assert r.status_code == 200, r.text
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    ta = threading.Thread(target=_write_many, args=("org-a", 10))
    tb = threading.Thread(target=_write_many, args=("org-b", 10))
    ta.start()
    tb.start()
    ta.join(timeout=20)
    tb.join(timeout=20)

    assert not errors, errors

    hist_a = httpx.get(f"{url}/history", headers=LEGACY_HDR,
                       params={"org_id": "org-a", "project": "p"}, timeout=10).json()["history"]
    hist_b = httpx.get(f"{url}/history", headers=LEGACY_HDR,
                       params={"org_id": "org-b", "project": "p"}, timeout=10).json()["history"]
    assert len(hist_a) == 10 and all(c["org_id"] == "org-a" for c in hist_a)
    assert len(hist_b) == 10 and all(c["org_id"] == "org-b" for c in hist_b)


# --------------------------------------------------------------------------
# 3. Bounded pool: concurrent in-flight requests never exceed
#    ERGO_HTTP_MAX_WORKERS, regardless of how many connections pile up.
# --------------------------------------------------------------------------
def test_bounded_pool_never_exceeds_max_workers(start_server, monkeypatch):
    monkeypatch.setenv("ERGO_HTTP_MAX_WORKERS", "3")
    url = start_server()

    concurrency = {"current": 0, "max": 0}
    lock = threading.Lock()
    real_route_get = server.Handler._route_get

    def _counting_route_get(self):
        with lock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
        try:
            time.sleep(0.15)  # widen the window so bursts actually overlap
            return real_route_get(self)
        finally:
            with lock:
                concurrency["current"] -= 1

    # Patched AFTER the server is already serving: do_GET resolves
    # self._route_get() per-request at call time, so every request from here
    # on picks up the instrumented version regardless of when boot() ran.
    monkeypatch.setattr(server.Handler, "_route_get", _counting_route_get)

    errors: list[Exception] = []

    def _fire():
        try:
            r = httpx.get(f"{url}/health", timeout=10)
            assert r.status_code == 200
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    n = 12
    threads = [threading.Thread(target=_fire) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert concurrency["max"] <= 3, (
        f"observed {concurrency['max']} concurrent in-flight requests with "
        "ERGO_HTTP_MAX_WORKERS=3 -- the bounding semaphore did not cap concurrency")
    assert concurrency["max"] >= 2, (
        "sanity: no overlap at all observed -- this test would pass vacuously "
        "on a fully serial (unbounded-but-never-parallel) implementation too")


# --------------------------------------------------------------------------
# 4. finding #1: live handler THREADS (not just in-handler work) must
#    never exceed ERGO_HTTP_MAX_WORKERS, even under a burst of many more
#    connections than the bound that never complete a request.
# --------------------------------------------------------------------------
def test_connection_burst_never_spawns_more_threads_than_the_worker_bound(
        start_server, monkeypatch):
    """ThreadingHTTPServer spawns one OS thread per ACCEPTED connection
    BEFORE do_GET/do_POST ever run, so the semaphore acquired at the top of
    each handler body bounds concurrent *work* but not thread/connection
    count -- a burst of connections that never even finish sending a request
    still each get a live thread. Instrument Handler.setup()/finish() (which
    every accepted connection's thread runs, before a single byte of the
    request is read) to prove concurrent connection-handling threads never
    exceed the bound, even when far more connections are opened than the
    bound and none of them ever completes a request."""
    monkeypatch.setenv("ERGO_HTTP_MAX_WORKERS", "3")
    monkeypatch.setenv("ERGO_HTTP_TIMEOUT", "0.3")
    url = start_server()
    u = urlparse(url)

    concurrency = {"current": 0, "max": 0}
    lock = threading.Lock()
    real_setup = server.Handler.setup
    real_finish = server.Handler.finish

    def _counting_setup(self):
        with lock:
            concurrency["current"] += 1
            concurrency["max"] = max(concurrency["max"], concurrency["current"])
        real_setup(self)

    def _counting_finish(self):
        try:
            real_finish(self)
        finally:
            with lock:
                concurrency["current"] -= 1

    monkeypatch.setattr(server.Handler, "setup", _counting_setup)
    monkeypatch.setattr(server.Handler, "finish", _counting_finish)

    socks = []
    try:
        for _ in range(15):
            socks.append(socket.create_connection((u.hostname, u.port), timeout=5))
        # Each admitted connection's thread blocks reading a request line
        # until the (short) socket timeout, then exits -- give every one of
        # the 15 connections time to cycle through in batches of the bound.
        time.sleep(2.5)
        assert concurrency["max"] <= 3, (
            f"observed {concurrency['max']} concurrent connection-handling "
            f"threads for a burst of 15 connections with "
            "ERGO_HTTP_MAX_WORKERS=3 -- connection admission is not bounded "
            "before thread creation")
    finally:
        for s in socks:
            s.close()


# --------------------------------------------------------------------------
# 5. Review follow-up: a saturated admission pool must not stop
#    shutdown() from making progress, and must reject (not hang) an extra
#    connection instead of blocking the accept loop forever.
# --------------------------------------------------------------------------
def test_shutdown_makes_progress_while_admission_pool_is_saturated(
        start_server, monkeypatch):
    """If every admission permit is held by a long-lived/stalled connection,
    process_request()'s acquire must not block the accept loop forever --
    otherwise shutdown() can never make progress (the loop never gets back
    to check the shutdown flag). admission_timeout bounds that wait so a
    saturated pool rejects new connections instead of hanging, and shutdown
    completes promptly even while a permit is still held."""
    monkeypatch.setattr(server.BoundedThreadingHTTPServer, "admission_timeout", 0.3)
    monkeypatch.setenv("ERGO_HTTP_MAX_WORKERS", "1")
    monkeypatch.setenv("ERGO_HTTP_TIMEOUT", "30")  # the hog outlives this test
    url = start_server()
    httpd = start_server.httpd
    u = urlparse(url)

    # Saturate the sole permit: connect but never send a request line, so
    # the handler thread parks inside rfile.readline() holding the permit.
    hog = socket.create_connection((u.hostname, u.port), timeout=5)
    time.sleep(0.5)  # let the hog's thread actually start and hold the permit

    try:
        # A second connection must be rejected (closed, no HTTP response)
        # once admission_timeout elapses, instead of hanging behind the
        # saturated pool.
        extra = socket.create_connection((u.hostname, u.port), timeout=5)
        start = time.monotonic()
        resp = extra.recv(4096)
        elapsed = time.monotonic() - start
        extra.close()
        assert resp == b"", (
            f"extra connection got a response instead of being closed "
            f"outright: {resp[:60]!r}")
        assert elapsed < 2.0, (
            f"extra connection was not rejected promptly ({elapsed:.2f}s) "
            "-- admission wait is not bounded")

        # shutdown() must complete promptly too, even though the hog still
        # holds the permit and hasn't been closed yet.
        shutdown_start = time.monotonic()
        httpd.shutdown()
        shutdown_elapsed = time.monotonic() - shutdown_start
        assert shutdown_elapsed < 2.0, (
            f"shutdown() blocked for {shutdown_elapsed:.2f}s behind a "
            "saturated admission pool")
    finally:
        hog.close()
