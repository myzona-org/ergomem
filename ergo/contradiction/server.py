"""Minimal HTTP server for the Ergo contradiction engine.

Zero extra deps — stdlib http.server only. Wraps ContradictionStore behind a tiny
JSON API so the self-contained Docker image is a real deployable service.

Routes:
  GET  /health            -> liveness + config (normalizer, nli, db path, store state)
  GET  /ready              -> write-path readiness: the SAME cheap DB probe /health
                              does, PLUS an actual normalizer liveness probe (calls
                              normalize() with a fixed probe statement) -- /health
                              alone can stay green during a normalizer-provider
                              outage even though every guarded write is 503ing.
                              200 ready / 503 degraded (normalizer
                              down) / 503 unhealthy (DB probe failed). Unauthenticated
                              like /health. The normalizer probe is TTL-cached
                              (ERGO_READY_PROBE_TTL_SECONDS, default 15s; 0 disables
                              the cache) so this unauth, LLM-calling route can't
                              become a cost/DoS vector.
  GET  /diagnose          -> ops view: counts, version meta, owner/backup hints
  GET  /metrics           -> Prometheus text exposition (ergo's private-repo issue #121):
                             per-route request counts/latency, guarded-write
                             outcomes (stored/409 conflict/403 quota/5xx),
                             normalize/NLI/judge stage timings, and thread-pool
                             saturation vs ERGO_HTTP_MAX_WORKERS. Operator-only
                             (see _metrics_authed's docstring for the exact
                             gate) -- unlike /health/ready this is never
                             unauthenticated by design. In-process counters,
                             reset on restart (single-process by design).
  POST /remember          -> {org_id, project?, who, statement, reason} -> Stored|Conflict.
                             Fails CLOSED (never falls through to fail-open) on any
                             provider error UNLESS force_exception is set, in which
                             case the write proceeds with the contradiction check
                             skipped and the bypass audited: 503 (degraded,
                             RETRYABLE — connection/timeout/429/5xx/auth,
                             finding #3) or 502 (bad gateway,
                             NON-retryable — a PERMANENT provider 4xx like 400/404/
                             422, re-review #3). A metered
                             tenant (PLAN-METERING below) over its monthly
                             guarded-write cap, or writing a NEW project beyond its
                             plan's project-count limit, gets 403 before any of the
                             above ever runs.
  POST /learn             -> {org_id, project?, who, statement, source, reason?}  guarded
                             write with source provenance folded into the reason;
                             same 503/502/force_exception degraded-provider behavior,
                             and the same PLAN-METERING 403s, as /remember above.
  POST /supersede         -> {org_id, project?, who, existing_id, statement, reason}.
                             Same 503/502 provider-error mapping as /remember, but
                             with NO force_exception override (unconditional
                             fail-closed). Counts against the PLAN-METERING monthly
                             write cap (below) like /remember/learn, but is NEVER
                             subject to the project-count limit (a supersede's
                             project always already has an active claim) nor the
                             active-claims cap (net-zero: a new claim replaces an
                             old one).
  POST /retract           -> {org_id, project?, who, claim_id, reason}  remove a
                             wrong-at-birth claim from the active set (404 if no
                             active claim in scope)
  GET  /active            -> ?org_id&project?&limit?&offset?  active claim set (3
                             empty-store states). Bounded page by default (private-repo issue #185):
                             `limit` defaults to DEFAULT_ACTIVE_LIMIT, clamped to
                             MAX_ACTIVE_LIMIT; response always carries `count` (a
                             separate COUNT(*), the SCOPE total regardless of the
                             page), `returned`, `truncated`, `next_offset` so
                             truncation is loud, never silent -- `state` still
                             describes the scope, not the page.
  GET  /history           -> ?org_id&project?  full claim history
  GET  /audit             -> ?org_id&project?&limit?  tenant-facing audit trail (issue
                             #124): this caller's OWN `writes` + `conflict_events` rows
                             only -- forced_org always wins, same as every other tenant
                             read; `limit` defaults to DEFAULT_AUDIT_LIMIT, clamped to
                             MAX_AUDIT_LIMIT
  GET  /recall            -> ?org_id&project?&q&limit  hybrid semantic+keyword search
  GET  /why               -> ?org_id&project?&q  best reasoned belief + reason + history
  POST /ingest            -> {org_id, project?, who, text, source?}  chunk + store a doc

  DEFAULT-PROJECT (private-repo issue #179): `project` is optional on every route above
  (matches every MCP tool signature -- tightening this to a required field
  is explicitly out of scope for this feature). When omitted, the engine
  resolves it in order: an explicit `project` argument always wins; failing
  that, the authenticated token's own `default_project`
(always `None` in this build -- there is no admin surface to set it)  ; failing that, the
  pre-existing literal fallback `"default"` -- but with a warning appended
  to the response's `warnings` field (the existing channel Stored/Conflict
  already carry; the write/read routes without one gain a `warnings` key
  only when there's something to report) so the silent single-project-slot
  burn this issue exists to fix (a Free-plan token's ENTIRE project
  allowance is 1) is visible instead of silent. This is deliberately NEVER
  a 400 -- see the acceptance criteria in private-repo issue #179.
  GET  /export            -> ?org_id&project?  open to every plan (private-repo issue #109 --
                             data portability is never gated): active claims
                             in scope, read straight off the DB (never /recall, which
                             drops fields), NO vectors (export-import phase-1 brief).
  POST /import            -> {claims:[...], target_project?}  merges each claim
                             through the SAME guarded write path /remember or
                             /learn uses (never reimplemented) -- dup active
                             statement -> skipped_duplicate; a blocking
                             conflict -> reported in `conflicts`, never
                             stored/forced. Always synchronous in this build --
                             no plan gate, no per-plan size cap, no background
                             job queue.

Auth: if ERGO_API_TOKEN is set, every route except /health and /ready requires
`Authorization: Bearer <token>`. If unset, a loopback-only bind (127.0.0.1, ::1, or
localhost) still runs open (dev/test only) and logs a warning at boot; a
non-loopback bind refuses to start at all unless ERGO_INSECURE_NO_AUTH=1 is also
set, in which case it logs a louder warning and runs open to the network (see
_validate_auth_bind). A bearer equal to ERGO_API_TOKEN always resolves to the
legacy admin principal (today's behavior: org_id/project/who honored from the
request body, routed to the default/legacy store below). Any OTHER bearer is
rejected (401) -- this build has no control plane to resolve it against.

  ERGO_SUPERADMIN_TOKEN, if set, is also the preferred operator gate for
  GET /metrics (falls back to ERGO_API_TOKEN if unset) — a bearer separate
  from ERGO_API_TOKEN, checked independently. See _metrics_authed's
  docstring for the exact gate.

Config via env:
  ERGO_DB_PATH         sqlite file (default /data/ergo.db) — the legacy/default store
  ERGO_SUPERADMIN_TOKEN preferred operator gate for GET /metrics (see above)
  ERGO_LLM_PROVIDER    anthropic|openai|litellm|ollama (default: none -> RuleNormalizer)
  ERGO_NLI             on|off (default on; uses the baked NLI judge)
  ERGO_HTTP_HOST       bind host (default 0.0.0.0)
  ERGO_HTTP_PORT       bind port (default 8000)
  ERGO_HTTP_MAX_WORKERS  max concurrent live connection-handling threads
                       (default 8) — see "Concurrency model" below.
  ERGO_LOG_FORMAT      "plain" (default) or "json" -- the access log written
                       to stderr (ergo's private-repo issue #121). "plain" keeps today's
                       `[ergo] <client> - <message>` line exactly as-is;
                       "json" emits one parseable JSON object per line
                       instead ({"ts","client","message"}). Opt-in only --
                       nothing changes unless this is set.
  ERGO_MAX_BODY_BYTES  max request body size for every POST route except
                       /ingest (default 1 MiB); over cap -> 413, checked
                       BEFORE the body is read off the socket.
  ERGO_MAX_INGEST_BYTES  max request body size for POST /ingest specifically
                       (default 8 MiB, larger than ERGO_MAX_BODY_BYTES since
                       legitimate reference docs run bigger); over cap -> 413.
  ERGO_MAX_FIELD_CHARS max length of any single guarded string field
                       (statement/reason/source/q) (default 32768); over
                       cap -> 413.
  ERGO_MAX_INGEST_CHUNKS  max chunk count POST /ingest may produce (default
                       5000), checked before any chunk is stored; over
                       cap -> 413.

Concurrency model (thread-bound-before-spawn per finding #1): the server
runs on `BoundedThreadingHTTPServer` (below), a
`ThreadingHTTPServer` subclass with `daemon_threads = True` so shutdown never
hangs on a stuck request. Each connection still gets its own thread once
admitted, so a slow guarded write for one tenant (the LLM normalizer call
inside /remember/learn/supersede, ~1-3s) cannot head-of-line-block another
tenant's /health or reads. Concurrency AND connection admission are BOTH
bounded by the same per-instance `threading.BoundedSemaphore(
ERGO_HTTP_MAX_WORKERS)` (built once in `__init__`, never rebuilt mid-flight):
`process_request()` (which runs synchronously on the single accept-loop
thread, BEFORE a worker thread is spawned) acquires a permit first, so a
connection burst queues at the OS accept() backlog instead of spawning an
unbounded thread per connection; the permit is released once the whole
request has finished (`process_request_thread`'s `finally`) — a naive
*unbounded* ThreadingHTTPServer, or one that only bounds handler-body work
after the thread already exists, is deliberately not used. Thread-safety
below the HTTP layer is per-tenant: each tenant's ContradictionStore (and the
single ControlStore) opens its sqlite connection with `check_same_thread=
False` and guards every public method with its OWN per-instance lock
(store.py, control.py) — there is no lock shared across tenants, so two
different tenants' stores still run fully in parallel. The write-guard's
`BEGIN IMMEDIATE` + `SQLITE_BUSY` retry (store.py) is unchanged and remains
the per-database serialization of record.

Ops / anti-zombie surface — surfaced by /diagnose so the running binary answers for
itself instead of a README:
  ERGO_OWNER             named owner of this deployment
  ERGO_OWNER_CONTACT     how to reach the owner (email/handle)
  ERGO_BACKUP_PATH       where backups land (online-backup API / VACUUM INTO target)
  ERGO_KILL_REVIEW_DATE  ISO date the owner must review keep/kill
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import urlparse, parse_qs

from .store import (ContradictionStore, Stored, Conflict, QuotaExceeded,
                    ProjectLimitExceeded, DEFAULT_AUDIT_LIMIT, MAX_AUDIT_LIMIT,
                    DEFAULT_ACTIVE_LIMIT, MAX_ACTIVE_LIMIT, ACTIVE_PROJECTIONS,
                    DEFAULT_EXPORT_LIMIT, MAX_EXPORT_LIMIT)
from .normalize import RuleNormalizer, NormalizeUnavailable
from .ingest import ingest_text, ChunkLimitExceeded
from . import metrics
from .singletenant import (
    ControlStore, Principal, LEGACY_PRINCIPAL,
    plan_limits, current_month_key, month_reset_at,
    TenantRegistry,
)


# --- engine singletons (built once at boot) ------------------------------------
_STORE: ContradictionStore | None = None
_CFG: dict = {}
# Multi-tenancy control plane: resolves bearer tokens minted for real
# tenants. None until boot() runs (tests that inject a
# FakeStore directly never call boot(), so _resolve_principal degrades to the
# legacy-only path when _CONTROL is None).
_CONTROL: ControlStore | None = None
_REGISTRY: TenantRegistry | None = None

# --- per-tenant rate limiting (never engaged in a single-tenant
# deployment: `_enforce_rate_limit` below exempts every principal here,
# since `_CONTROL` is always `None` and the legacy principal's
# `tenant_id` is always `None`; this machinery mirrors the private
# multi-tenant deployment's code shape) ---------------------------


# --- /ready normalizer-probe cache (READY-ROUTE) --------------------------
class _ReadyProbeCache:
    """Owns the TTL-cached normalizer-probe result for `/ready` (module
    state, bound to a FRESH instance at `boot()` -- same "own it per server
    instance, never a bare leftover global" pattern `_STORE`/`_CONTROL`/
    `_REGISTRY` already follow -- so `boot()`/tests never leak a stale
    cached result across server instances or test runs).

    `/ready` is deliberately unauthenticated (a monitor/liveness route,
    same as `/health`) yet it actively calls the normalizer -- for an
    LLM-backed provider that's a real cost, and an unauthenticated route
    that triggers a paid call on every hit is a DoS/cost vector. This cache
    dedupes that: at most one real `normalize()` call per TTL window,
    regardless of how many requests land inside it. `BoundedThreadingHTTPServer`
    serves requests on concurrent threads, so the (timestamp, result) pair
    is shared mutable state -- `self._lock` guards it, BUT (finding
    on #115, the same anti-pattern the old in-memory rate-limiter's own
    docstring used to warn about, citing store.py's `remember()`)
    `self._lock` is NEVER held
    across the actual `normalize()` call -- an LLM round trip can take
    seconds, and a provider outage (exactly what `/ready` exists to detect)
    can make it far slower than that. Holding the lock for that whole span
    would queue every concurrent `/ready` request behind it, each pinning a
    worker thread from the bounded pool (`ERGO_HTTP_MAX_WORKERS`) for the
    duration -- turning the readiness probe into the very worker-pool
    exhaustion it's meant to surface. Instead this is single-flight: ONE
    thread ("the leader") does the real call outside any lock; any other
    thread that lands here while that call is in flight either (a) rides
    the leader's result once it lands (`threading.Event`, only when there
    is no prior result to fall back on at all -- i.e. the very first probe
    since boot, a narrow one-time startup window) or (b) far more commonly,
    once at least one probe has ever completed, immediately returns the
    last known result (marked `cached=True`) rather than waiting on
    anything -- a follower's request latency is never coupled to how long
    the in-flight external call takes.

    Case (a)'s wait is itself BOUNDED (finding on private-repo PR #115:
    "Unbounded wait on first probe", `ERGO_READY_FIRST_PROBE_WAIT_SECONDS`,
    default 3s -- well under the normalizer's own 60-120s request timeout):
    if the leader hasn't landed within that short budget, the follower
    gives up and returns a controlled "not ready yet" result rather than
    pinning its worker thread for however long a hung provider takes.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._checked_at: float | None = None  # time.monotonic() of last real probe
        self._checked_at_iso: str | None = None  # wall-clock ISO of that same probe
        self._result: dict | None = None
        self._inflight: threading.Event | None = None  # set when the in-flight probe lands
        # A GENUINE bug in the normalizer (anything _probe_normalizer's own
        # `except NormalizeUnavailable` doesn't catch) that killed the
        # leader before it ever produced a result -- only ever consulted by
        # a follower on the narrow first-probe-since-boot path below.
        self._pending_error: Exception | None = None

    def probe(self, normalizer, ttl_seconds: int) -> tuple[dict, bool, str]:
        """Return (normalizer_status_dict, cached, checked_at). `checked_at`
        is always the wall-clock time of the last REAL `normalize()` call --
        on a cache hit this deliberately does NOT advance to "now": reporting
        "now" on a cached hit would tell a
        monitor the write path was just verified when the underlying probe
        may be up to `ttl_seconds` stale; `cached` alone doesn't convey
        *how* stale).

        `ttl_seconds <= 0` disables the cache entirely -- per a SEPARATE
        finding on private-repo PR #115, this must mean every call does its OWN
        independent probe with NO single-flight coordination at all: a
        caller who explicitly sets ERGO_READY_PROBE_TTL_SECONDS=0 is opting
        OUT of the DoS guard (the docstring/brief promise is "probe every
        hit"), so it would be wrong to have a concurrent call quietly ride
        another call's in-flight (possibly stale-by-the-time-it-lands)
        result the way a TTL>0 follower does below -- that's a form of
        caching this mode is meant to disable.
        """
        if ttl_seconds <= 0:
            result = _probe_normalizer(normalizer)  # NEVER under self._lock
            return result, False, _iso_now()
        now = time.monotonic()
        with self._lock:
            if (self._checked_at is not None
                    and (now - self._checked_at) < ttl_seconds):
                return self._result, True, self._checked_at_iso
            if self._inflight is not None:
                if self._result is not None:
                    # a probe is already running elsewhere; ride the last
                    # known result instead of piling this worker thread
                    # onto a possibly slow/hanging call it didn't start.
                    return self._result, True, self._checked_at_iso
                event = self._inflight  # first-ever probe, already claimed
                is_leader = False
            else:
                event = self._inflight = threading.Event()
                is_leader = True
        if not is_leader:
            # narrow startup-only path: no probe has EVER completed, so
            # there is truly nothing to fall back on -- wait for the
            # leader, but only up to a SHORT, readiness-specific budget
            # (finding on private-repo PR #115: "Unbounded wait on first probe").
            # The leader's normalize() call is itself bounded only by the
            # normalizer's own request timeout (60-120s); an unbounded
            # wait here would let a hung provider during this window pin
            # every follower's worker thread for that whole span,
            # exhausting the bounded pool (ERGO_HTTP_MAX_WORKERS) right
            # when a monitor needs a fast red /ready.
            wait_seconds = _ready_first_probe_wait_seconds()
            if not event.wait(timeout=wait_seconds):
                # This thread never touched the normalizer itself -- there
                # is nothing to re-raise, and the leader (still running)
                # may yet populate the cache for the NEXT request. Report
                # a controlled "not ready yet" result instead of blocking
                # any further.
                return {
                    "ok": False,
                    "error": "first readiness probe still in progress "
                             f"(no result within {wait_seconds}s)",
                    "retryable": True,
                }, False, None
            with self._lock:
                if self._result is None:
                    # finding on private-repo PR #115: `_probe_normalizer` only
                    # catches `NormalizeUnavailable` -- a genuine bug killed
                    # the leader before it ever set `self._result`. Re-raise
                    # the SAME exception here instead of returning `None`
                    # for `normalizer_status_dict`, which would otherwise
                    # crash `_ready_status`'s `norm_result["ok"]` with a
                    # confusing bare TypeError for a follower request that
                    # never even touched the normalizer itself.
                    raise self._pending_error or RuntimeError(
                        "ready probe: leader finished with neither a result "
                        "nor a recorded error")
                return self._result, True, self._checked_at_iso
        try:
            result = _probe_normalizer(normalizer)  # NEVER under self._lock
        except Exception as e:
            with self._lock:
                self._pending_error = e
                self._inflight = None
            event.set()
            raise
        checked_at_iso = _iso_now()
        with self._lock:
            self._result = result
            self._checked_at = now
            self._checked_at_iso = checked_at_iso
            self._pending_error = None
            self._inflight = None
        event.set()
        return result, False, checked_at_iso

    def reset(self) -> None:
        self._checked_at = None
        self._checked_at_iso = None
        self._result = None
        self._inflight = None
        self._pending_error = None


_READY_PROBE_CACHE = _ReadyProbeCache()


def _max_http_workers() -> int:
    try:
        n = int(os.environ.get("ERGO_HTTP_MAX_WORKERS", "8"))
    except ValueError:
        return 8
    return n if n > 0 else 8


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that bounds live handler THREADS (not just
    in-handler work) to ERGO_HTTP_MAX_WORKERS (finding #1).

    A plain ThreadingHTTPServer spawns one OS thread per ACCEPTED connection
    before do_GET/do_POST ever run, so a semaphore acquired at the top of the
    handler body caps concurrent *work* but not thread/connection count -- a
    burst of thousands of (even incomplete) connections still spawns
    thousands of threads. `process_request()` runs synchronously on the
    single accept-loop thread, BEFORE `ThreadingMixIn.process_request` spawns
    the worker thread, so acquiring the bound here blocks admission of the
    NEXT connection instead of letting it spawn an unbounded thread; the
    excess simply queues at the OS accept() level (backlog bumped below)
    instead of becoming live Python threads. The permit is released once the
    whole request has been handled (`process_request_thread`'s `finally`),
    not merely once do_GET/do_POST's route body returns -- this also
    subsumes the do_GET/do_POST semaphore guard that used to bound handler
    work only (consolidated here per the finding #1 fix).

    The admission semaphore is a PER-INSTANCE attribute (built once in
    __init__ from ERGO_HTTP_MAX_WORKERS), not a lazily-rebuildable
    module-level global: process_request()'s acquire and
    process_request_thread()'s release must always be the exact same
    semaphore object for this server's whole lifetime -- a module-level
    "rebuild if the env var changed" helper could hand back a *different*
    object between the two calls and release() a semaphore that was never
    acquired (raises, or masks a real leak).

    admission_timeout bounds how long a single accept-loop iteration can
    block waiting for a permit: without it, once the pool is saturated (long
    -lived or stalled requests holding every permit), server_close()/
    shutdown() can never make progress because the accept loop is parked
    inside process_request()'s acquire and never gets back to check the
    shutdown flag. On timeout the connection is rejected (socket closed, no
    HTTP response -- admission was never granted) instead of blocking
    forever.
    """

    # Generous accept-queue backlog: admission is deliberately gated here, so
    # a burst piles up in the cheap OS queue instead of spawning threads.
    request_queue_size = 128
    admission_timeout = 10.0

    def __init__(self, *args, **kwargs):
        self._admission_sem = threading.BoundedSemaphore(_max_http_workers())
        # Explicit counter for the /metrics thread-pool saturation gauge
        # (ergo's private-repo issue #121) -- deliberately NOT read off
        # `_admission_sem`'s private `_value` attribute (a CPython
        # implementation detail, not a documented API): this is a plain
        # int incremented/decremented under its own lock in lockstep with
        # every acquire/release below, so it survives a Python version
        # change unaffected.
        self._active_count = 0
        self._active_count_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._admission_sem.acquire(timeout=self.admission_timeout):
            try:
                request.close()
            except OSError:
                pass
            return
        with self._active_count_lock:
            self._active_count += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_count_lock:
                self._active_count -= 1
            self._admission_sem.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_count_lock:
                self._active_count -= 1
            self._admission_sem.release()


def _max_body_bytes() -> int:
    try:
        n = int(os.environ.get("ERGO_MAX_BODY_BYTES", str(1024 * 1024)))
    except ValueError:
        return 1024 * 1024
    return n if n > 0 else 1024 * 1024


def _max_ingest_bytes() -> int:
    try:
        n = int(os.environ.get("ERGO_MAX_INGEST_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        return 8 * 1024 * 1024
    return n if n > 0 else 8 * 1024 * 1024


def _max_field_chars() -> int:
    try:
        n = int(os.environ.get("ERGO_MAX_FIELD_CHARS", str(32 * 1024)))
    except ValueError:
        return 32 * 1024
    return n if n > 0 else 32 * 1024


def _max_ingest_chunks() -> int:
    try:
        n = int(os.environ.get("ERGO_MAX_INGEST_CHUNKS", "5000"))
    except ValueError:
        return 5000
    return n if n > 0 else 5000


def _log_format() -> str:
    """`ERGO_LOG_FORMAT` (ergo's private-repo issue #121): "plain" (default) keeps today's
    `[ergo] <client> - <message>` stderr access log exactly as-is; "json"
    emits one parseable JSON object per line instead. Any other/unset value
    falls back to plain -- opting in requires the exact literal "json"."""
    return os.environ.get("ERGO_LOG_FORMAT", "plain").strip().lower()


# Every literal route this server actually handles (ergo's private-repo issue #121
# /metrics, finding: "unbounded cardinality"). `_record_request_
# metrics` normalizes an incoming request's path against this set BEFORE
# using it as a `route` label -- without that normalization, a client
# hitting many distinct nonexistent paths (no auth required to reach the
# 404 catch-all) would grow the in-process metrics dicts without bound for
# the life of the process, an unauthenticated memory-exhaustion vector.
# Anything not in this set (including every 404) is labeled "other".
_KNOWN_ROUTES = frozenset({
    "/health", "/ready", "/metrics", "/diagnose",
    "/remember", "/learn", "/supersede", "/retract", "/ingest",
    "/active", "/history", "/audit", "/recall", "/why", "/export", "/import",
})


def _metrics_route_label(path: str) -> str:
    """Normalize a request path to a bounded-cardinality `/metrics` route
    label -- see `_KNOWN_ROUTES`'s docstring above."""
    return path if path in _KNOWN_ROUTES else "other"


# The three guarded-write routes (ergo's private-repo issue #121 /metrics): /ingest is
# deliberately excluded -- it skips the contradiction gate entirely (README:
# "fact = upsert freely, no gate"), so it has no stored/conflict/quota/5xx
# outcome to report in the SAME sense /remember, /learn, and /supersede do.
_GUARDED_WRITE_ROUTES = frozenset({"/remember", "/learn", "/supersede"})


def _guarded_write_outcome(route: str, code: int) -> str | None:
    """Map a guarded-write route's HTTP status to the outcome bucket ergo
    private-repo issue #121 asks `/metrics` to report: stored (200) / conflict (409) /
    quota (403) / 5xx (>=500). "quota" deliberately lumps together every 403
    cause on these routes (per-tenant active-claim cap, PLAN-METERING
    monthly-write-cap, and project-count-limit) -- all three are the same
    shape of signal to an operator ("this tenant hit a ceiling")
.    Any
    route NOT in `_GUARDED_WRITE_ROUTES` (every read, /ingest, /retract,
/import)    returns None unconditionally -- not counted here at
    all, the request-level counters above already cover it. A 502/503
    (degraded-provider fail-closed) is itself >=500 so it lands in "5xx";
    every other status (400 bad request, 404 no such claim, 413 oversized
    field) on a guarded route also returns None -- the write-guard never ran
    in those cases, so there is no guarded-write outcome to report."""
    if route not in _GUARDED_WRITE_ROUTES:
        return None
    if code >= 500:
        return "5xx"
    if code == 200:
        return "stored"
    if code == 409:
        return "conflict"
    if code == 403:
        return "quota"
    return None


def _tenant_rate_limit_per_min_default() -> int:
    """0 = unlimited -- deliberately DIFFERENT from
    the _max_* helpers above, which floor any non-positive value back to
    their default. Only a negative or unparsable value falls back here; 0
    passes through unchanged."""
    try:
        n = int(os.environ.get("ERGO_TENANT_RATE_LIMIT_PER_MIN", "120"))
    except ValueError:
        return 120
    return n if n >= 0 else 120


def _ready_probe_ttl_seconds() -> int:
    """TTL (seconds) for `/ready`'s cached normalizer probe. `0` is
    meaningful (disables the cache -- probe every hit), so this floors like
    `_tenant_rate_limit_per_min_default` above, NOT like the `_max_*`
    helpers: only a negative or unparsable value falls back to the default
    of 15."""
    try:
        n = int(os.environ.get("ERGO_READY_PROBE_TTL_SECONDS", "15"))
    except ValueError:
        return 15
    return n if n >= 0 else 15


def _ready_first_probe_wait_seconds() -> float:
    """Budget (seconds) a follower will wait for the very FIRST `/ready`
    probe since boot to land before giving up (finding on private-repo PR #115:
    "Unbounded wait on first probe"). That narrow startup-only path has no
    prior cached result to fall back on, so without a bound a follower's
    `event.wait()` is only limited by the normalizer's own request timeout
    (60-120s) -- and `BoundedThreadingHTTPServer` caps TOTAL worker threads
    at `ERGO_HTTP_MAX_WORKERS`, so a hung provider during that window could
    pin the whole pool for that whole span, exactly when a monitor needs a
    fast red `/ready`. Deliberately floors like the `_max_*` helpers (any
    non-positive or unparsable value falls back to the default) -- unlike
    `ERGO_READY_PROBE_TTL_SECONDS`, `0` has no useful meaning here."""
    try:
        n = float(os.environ.get("ERGO_READY_FIRST_PROBE_WAIT_SECONDS", "3"))
    except ValueError:
        return 3.0
    return n if n > 0 else 3.0


def _tenant_max_active_claims_default() -> int:
    """0 = unlimited -- see
    _tenant_rate_limit_per_min_default's docstring for why this floors
    differently from the _max_* helpers above."""
    try:
        n = int(os.environ.get("ERGO_TENANT_MAX_ACTIVE_CLAIMS", "50000"))
    except ValueError:
        return 50000
    return n if n >= 0 else 50000


def _tenant_rate_limit_burst_per_min_default() -> int:
    """RATE-LIMIT-PERSISTENCE: the default EXTRA burst allowance added on top
    of `_tenant_rate_limit_per_min_default()`'s steady rate, in the same
    "requests" units, for the token bucket's capacity
    (`capacity = rate_limit_per_min + burst`). Unlike
    `_tenant_rate_limit_per_min_default`, `0` here is the DEFAULT (no extra
    burst beyond the steady rate -- this is what keeps a tenant with no
    burst config at today's effective per-class ceiling, the backward-
    compat requirement), not a special "unlimited" sentinel; a negative or
    unparsable value falls back to 0 like the other ERGO_TENANT_* knobs
    fall back to THEIR defaults."""
    try:
        n = int(os.environ.get("ERGO_TENANT_RATE_LIMIT_BURST_PER_MIN", "0"))
    except ValueError:
        return 0
    return n if n >= 0 else 0


def _effective_rate_limit(principal: Principal) -> int:
    """Per-tenant override (tenant_limits, carried on Principal since
    resolve() already joined it) wins over the env default; None means no
    override. A negative override is treated as "no override" defensively
    (set_limits()/the admin route already reject negatives at write time)."""
    override = principal.rate_limit_per_min_override
    if override is not None and override >= 0:
        return override
    return _tenant_rate_limit_per_min_default()


def _effective_rate_limit_burst(principal: Principal) -> int:
    """RATE-LIMIT-PERSISTENCE: see `_effective_rate_limit`'s docstring -- same
    override-wins-over-env-default resolution, for the rate-limit burst
    allowance. Applies uniformly to every route class (there is no
    per-class override, only a per-tenant one -- RATE-LIMIT-PERSISTENCE keeps
    the override surface small/simple per the issue brief)."""
    override = principal.rate_limit_burst_per_min_override
    if override is not None and override >= 0:
        return override
    return _tenant_rate_limit_burst_per_min_default()


def _effective_max_active_claims(principal: Principal) -> int:
    """See _effective_rate_limit's docstring -- same override-wins-over-
    env-default resolution for the active-claim cap."""
    override = principal.max_active_claims_override
    if override is not None and override >= 0:
        return override
    return _tenant_max_active_claims_default()


def _check_rate_limit(principal: Principal, route_class: str) -> tuple[bool, int]:
    """Never reached in a single-tenant deployment: `_enforce_rate_limit`
    below exempts every principal here, since `principal.tenant_id` is
    always `None` and `_CONTROL` is always `None`. Delegates to a
    per-tenant `ControlStore.check_rate_limit` this build never
    constructs. `route_class` is an opaque string key ("read"/"write"
    today, matching the do_GET/do_POST split). Returns (allowed,
    retry_after_seconds); retry_after is only meaningful when allowed is
    False."""
    limit = _effective_rate_limit(principal)
    burst = _effective_rate_limit_burst(principal)
    return _CONTROL.check_rate_limit(principal.tenant_id, route_class, limit, burst)


# NOTE: the preflight active-claim-cap check that used to live here
# (`_check_claim_cap`, calling `store.active_claim_count()` with the store's
# lock released BEFORE remember()/learn()/ingest() ever took it) was a TOCTOU
# race -- two concurrent requests could both observe room under the cap, both
# pass this check, and both commit, overshooting it (finding). The
# cap is now enforced ATOMICALLY inside the store's own write-guard
# transaction (`ContradictionStore._enforce_claim_cap`, called from
# `_remember_attempt`/`_remember_fact_attempt`/`_remember_facts_batch_attempt`
# right before each insert, while `self._lock` is held for the whole
# transaction) -- see store.py's `QuotaExceeded` docstring. server.py now
# just computes the effective cap (`_effective_max_active_claims`) and passes
# it into `store.remember()`/`store.learn()`/`ingest_text()`, catching
# `QuotaExceeded` and mapping it to 403.


# --- plan-based monthly write / project metering (PLAN-METERING) ----------
def _plan_limits_for(principal: Principal) -> dict | None:
    """None for a legacy/superadmin principal (tenant_id is None, same
    exemption as `_effective_max_active_claims`'s `principal.tenant_id is
    not None` gate) OR for a real tenant with plan=NULL (grandfathered) --
    both mean "unlimited, no metering at all". Otherwise
    {"monthly_write_limit": int, "project_limit": int|None} from
    control.py's PLAN_DEFAULTS."""
    if principal.tenant_id is None:
        return None
    return plan_limits(principal.plan)


def _enforce_monthly_write_cap(principal: Principal, plan_lims: dict) -> tuple[bool, dict]:
    """Atomically check-and-increment the tenant's current-month
    guarded-write usage against `plan_lims["monthly_write_limit"]` (see
    `ControlStore.check_and_increment_monthly_write` for the TOCTOU-safe
    atomicity contract, including its CREDIT-BALANCE spend-after-allowance
    extension). Returns (allowed, body) -- `body` is only meaningful when
    allowed is False: the exact 403 structured body from the brief, ready
    to `_send(403, body)`. `credit_balance` rides along on the SAME
    check-and-increment call (no second query): a denial only ever happens
    when the balance was 0/absent (any positive balance gets spent instead
    and the request is `allowed`), so this always reports 0 here -- it's
    what lets a client confirm the refusal is "genuinely stopped" (out of
    allowance AND out of credits) rather than wondering if a stale
    balance was silently ignored."""
    limit = plan_lims["monthly_write_limit"]
    allowed, used, credit_balance = _CONTROL.check_and_increment_monthly_write(
        principal.tenant_id, limit)
    if allowed:
        return True, {}
    return False, {
        "error": "monthly_write_limit_reached",
        "plan": principal.plan,
        "limit": limit,
        "used": used,
        "credit_balance": credit_balance,
        "resets_at": month_reset_at(current_month_key()),
        "upgrade": "https://ergomem.com/pricing",
    }


def _export_import_gate_for_plan(plan_lims: dict | None, plan: str | None,
                                  op: str) -> dict | None:
    """Core exemption logic behind the Import gate (`op == "import"`); Export
    (`op == "export"`) is never gated -- reads never consume any write
    allowance and data portability is never restricted. This build has no
    plan gate at all (`plan_lims` is always None), shared by the
    tenant-facing `_export_import_gate` (below, keyed off the CALLER's own
    Principal)
.
    `plan_lims` is the resolved `plan_limits()` dict for the scope being
    checked -- None means exempt (legacy/pooled, superadmin, or a
    grandfathered NULL-plan real tenant); only a real `plan_lims` paired
    with `plan == 'free'` and `op == 'import'` is blocked. Returns None if
    allowed, a ready-to-send 403 body otherwise."""
    if op == "export":
        return None
    if plan_lims is None:
        return None
    if plan != "free":
        return None
    return {
        "error": "import_requires_pro_or_team",
        "plan": plan,
        "upgrade": "https://ergomem.com/pricing",
    }


def _export_import_gate(principal: Principal, op: str) -> dict | None:
    """None if `op` ("export" or "import") is allowed for this principal; a
    ready-to-send 403 body otherwise. `_plan_limits_for(principal)` already
    resolves the same legacy/superadmin/grandfathered exemption set this
    used to check directly -- see `_export_import_gate_for_plan` for the
    shared gate logic itself."""
    return _export_import_gate_for_plan(_plan_limits_for(principal), principal.plan, op)


def _import_size_cap_error(plan_lims: dict | None, claims_len: int) -> dict | None:
    """None if `claims_len` is within `plan_lims["import_max_claims"]` (or
    `plan_lims` is None -- exempt); a ready-to-send 413 body otherwise.
    Checked by `POST /import`'s plan-size gate.
    """
    if plan_lims is None:
        return None
    import_cap = plan_lims["import_max_claims"]
    if import_cap is not None and claims_len > import_cap:
        return {
            "error": f"import of {claims_len} claims exceeds the "
                    f"plan's import_max_claims of {import_cap}",
            "import_max_claims": import_cap,
        }
    return None


_REVISIONS_TRUNCATED_NOTE = "\n[revisions truncated: chain exceeded the field-size cap]"


def _fold_revisions_into_reason(reason, revisions, cap: int):
    """Fold an imported claim's `revisions[]` (private-repo issue #189's nested
    export shape -- GET /export's oldest-first supersede-chain history) into
    the imported claim's `reason`, mirroring the established `/learn`
    pattern of folding provenance into `reason` rather than a new column
    (`"...\\n[learned from: <source>]"`).

    Unknown keys on an incoming claim are otherwise silently ignored by
    `_import_merge` (it only reads specific per-claim keys) -- exactly the
    silent-incompleteness failure mode #184/#185 fixed elsewhere, and not
    acceptable here: an export -> import round trip would lose 100% of the
    why-chain narrative while reporting a clean import. Folding into
    `reason` is the only guarded write path the gate understands (there is
    no `revisions` column), so this is a text composition, not a schema
    change.

    Returns `reason` UNCHANGED if `revisions` is falsy (a claim with no
    `revisions` key must behave exactly as it does today). Otherwise
    returns `reason` (or a synthesized stand-in if `reason` was empty) with
    a `\\n[prior revisions: ...]` block appended, BOUNDED to `cap` chars
    total so a long chain can never blow `_max_field_chars()`. If the chain
    doesn't fit, it is truncated to as many whole entries as fit and the
    truncation is announced INSIDE the folded text -- never silent.

    NEVER clips `base` (the claim's own `reason`) to make room for the
    folded history: the claim's own rationale is the PRIMARY content, the
    folded predecessor chain is secondary. If not even one whole entry fits
    alongside `base` and the truncation note, `base` is returned fully
    intact (plus the note, only if the note itself fits without touching
    `base`) -- the revisions are what gets dropped, and the note never
    claims "revisions truncated" while it was actually `base` that got cut."""
    if not revisions:
        return reason
    lines = []
    for rev in revisions:
        if not isinstance(rev, dict):
            continue
        stmt = str(rev.get("statement") or "").strip()
        if not stmt:
            continue
        rsn = str(rev.get("reason") or "").strip()
        who = str(rev.get("who") or "").strip() or "unknown"
        status = str(rev.get("status") or "").strip() or "superseded"
        created = str(rev.get("created_at") or "").strip()
        meta = f"{status} by {who}" + (f" at {created}" if created else "")
        entry = f"- {stmt} ({meta})" + (f": {rsn}" if rsn else "")
        lines.append(entry)
    if not lines:
        return reason
    base = str(reason).strip() if reason and str(reason).strip() else \
        "Imported with prior revisions."
    header = "\n[prior revisions:\n"
    footer = "\n]"
    full = base + header + "\n".join(lines) + footer
    if len(full) <= cap:
        return full
    budget = cap - len(base) - len(header) - len(footer) - len(_REVISIONS_TRUNCATED_NOTE)
    kept = []
    used = 0
    for entry in lines:
        cost = len(entry) + (1 if kept else 0)
        if used + cost > max(budget, 0):
            break
        kept.append(entry)
        used += cost
    if not kept:
        # `base` alone (plus the header/footer/note overhead) already
        # leaves no room for even a single whole entry -- keep `base`
        # fully intact rather than slicing into it, and only append the
        # note if it fits WITHOUT touching `base`.
        room = cap - len(base)
        return base + _REVISIONS_TRUNCATED_NOTE if room >= len(_REVISIONS_TRUNCATED_NOTE) else base
    # `used` (by construction, via `budget`) guarantees this fits within
    # `cap` without ever needing to clip `base` or an already-kept entry.
    return base + header + "\n".join(kept) + _REVISIONS_TRUNCATED_NOTE + footer


def _import_merge(store, org_id: str, claims_in: list, target_project: str | None,
                   default_who: str, max_active_claims: int | None,
                   project_limit: int | None) -> dict:
    """Contradiction-checked merge loop for `POST /import` -- the SAME
    guarded write path (`store.remember`, or `store.learn` when a claim
    carries a non-empty `source`) this route must run every incoming
    claim through,
    never a reimplemented gate. Dedup (`has_active_statement`), conflict
    reporting, and the graceful `cap_reached` stop are all here exactly
    once.

    Returns the route's response body: {"imported", "skipped_duplicate",
    "conflicts", "conflict_count", "cap_reached"}. `QuotaExceeded` /
    `ProjectLimitExceeded` stop the loop gracefully (folded into
    `cap_reached`, never propagated) -- the brief's explicit "never a 500"
    requirement. `NormalizeUnavailable` and any other `Exception` propagate
    for the caller to map to the same HTTP status the inline loop used to
    (503/502 degraded, or 500): that mapping is route-response plumbing,
    not merge logic, so it stays at the call site instead of being
    duplicated here.

    A claim carrying a `revisions` list (GET /export's nested why-chain,
    private-repo issue #189) has it FOLDED into `reason` via `_fold_revisions_into_reason`
    before the guarded write runs -- there is no `revisions` column, so this
    is the only way the chain survives a round trip instead of being
    silently dropped like any other unknown key. A claim with no
    `revisions` key is unaffected -- `reason` passes through unchanged,
    exactly as before this issue."""
    imported = 0
    skipped_duplicate = 0
    conflicts: list = []
    cap_reached = False
    for claim in claims_in:
        if not isinstance(claim, dict):
            continue
        statement = claim.get("statement")
        if not statement or not str(statement).strip():
            continue
        project = target_project or claim.get("project")
        if not project or not str(project).strip():
            continue
        who = claim.get("who") or default_who
        reason = _fold_revisions_into_reason(
            claim.get("reason"), claim.get("revisions"), _max_field_chars())
        source = claim.get("source")
        # Same dedup rule as /ingest's idempotent re-ingest: an exact-text
        # active statement already in scope is skipped, never re-stored or
        # re-checked.
        if store.has_active_statement(org_id, project, statement):
            skipped_duplicate += 1
            continue
        try:
            if source and str(source).strip():
                r = store.learn(org_id, project, who, statement, source,
                                reason=reason, max_active_claims=max_active_claims,
                                project_limit=project_limit)
            else:
                r = store.remember(org_id, project, who, statement, reason=reason,
                                   max_active_claims=max_active_claims,
                                   project_limit=project_limit)
        except (QuotaExceeded, ProjectLimitExceeded):
            # PLAN-METERING: stop gracefully -- return what
            # was imported so far with cap_reached:true, NEVER a 500 (the
            # brief's explicit requirement).
            cap_reached = True
            break
        if isinstance(r, Stored):
            imported += 1
        else:
            # Blocking conflict: NOT stored, NOT forced -- reported for the
            # caller to review (the "import audit").
            hit = r.conflicts[0] if r.conflicts else None
            conflicts.append({
                "statement": r.incoming_text,
                "against": hit.claim["id"] if hit else None,
                "reason": hit.reason if hit else None,
                "tier": hit.tier if hit else None,
            })
    return {
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "cap_reached": cap_reached,
    }


def _iso_now() -> str:
    """UTC ISO-8601 instant with a literal 'Z' suffix (GET /export's
    `exported_at` -- matches control.py's `_now()` format)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RequestTooLarge(Exception):
    """Raised by `Handler._body()` when Content-Length exceeds the
    configured cap (finding #2) -- mapped to HTTP 413 at every call
    site, before the oversized body is ever read off the socket."""


def _oversized_field(body: dict, fields: tuple) -> str | None:
    """Return the name of the first field in `body` whose string value
    exceeds ERGO_MAX_FIELD_CHARS, or None if all are within bounds."""
    cap = _max_field_chars()
    for f in fields:
        v = body.get(f)
        if isinstance(v, str) and len(v) > cap:
            return f
    return None


_DEFAULT_PROJECT_FALLBACK = "default"


class ResolvedProject(NamedTuple):
    """Return shape of `_resolve_project` (private-repo issue #184): `project` is the
    string every existing caller wants; `source` names which resolution
    branch won -- `"explicit"` | `"token_default"` | `"fallback"` -- so
    `/recall` and `/why` can echo it in their `scope` object instead of
    re-deriving it (duplicating the same if/elif) at the call site."""
    project: str
    source: str


def _resolve_project(explicit_project, principal: Principal, warnings: list) -> ResolvedProject:
    """DEFAULT-PROJECT (private-repo issue #179): resolve the project scope for a
    tenant-facing request that may omit `project` -- `project` is optional
    on every route (matches every MCP tool signature).

    Resolution order, an explicit argument ALWAYS wins:
      1. `explicit_project` (any non-blank string) -- the caller said so.
         Reported as source "explicit".
      2. `principal.default_project` -- the authenticated token's OWN
         per-key default, carried on `principal`; always `None` here,
         since this build has no control plane to look one up from
         ). None for a legacy/superadmin principal (no `tokens` row)
         and for any real token that never set one. Reported as source
         "token_default".
      3. The pre-existing literal fallback `"default"` -- but this branch
         APPENDS a warning to `warnings` (the caller's own response-
         `warnings` list) so the silent single-project-slot burn this issue
         exists to fix (a Free-plan token's ENTIRE project allowance is 1)
         is visible instead of silent. Reported as source "fallback".

    Never raises and never 400s -- tightening the no-project case to an
    error is an explicit later phase (private-repo issue #179), out of scope here."""
    if explicit_project:
        return ResolvedProject(explicit_project, "explicit")
    if principal.default_project:
        return ResolvedProject(principal.default_project, "token_default")
    warnings.append(
        "no project specified and this token has no default_project set -- "
        f"defaulting to project {_DEFAULT_PROJECT_FALLBACK!r}. Set "
        "default_project, or pass project "
        "explicitly, to avoid burning a plan's project slot on this name."
    )
    return ResolvedProject(_DEFAULT_PROJECT_FALLBACK, "fallback")


def _scope_info(store, org_id: str, project: str, source: str, *, non_empty: bool) -> dict:
    """Additive `scope` echo for `/recall` and `/why` (private-repo issue #184): tells
    the caller which project string was actually queried (`project`), which
    `_resolve_project` branch produced it (`source`), and whether that
    project actually has active claims (`state`) -- so an empty RESULT SET
    is never silently confused with an empty PROJECT. `state` reuses the
    SAME three-state vocabulary `/active` already returns via
    `store.active()` (`ActiveSet.state` -- see store.py) rather than
    inventing a second notion of project existence.

    `non_empty=True` means the caller already has a hit (a recall result, or
    a non-null `why`), which alone proves `"has_active_claims"` -- skip the
    state query entirely so the hot path (the common case) stays cheap. Only
    an EMPTY result pays for `store.project_state()`.

    A follow-up on the very first version of this function found it computed
    `state` from `projects`-table existence ALONE, which is wrong: a project
    can hold thousands of active claims while /why's reasoned-only filter or
    /recall's global-KNN-then-post-filter both legitimately return nothing
    for THIS query -- that old code reported `"empty"` about a project full
    of claims, contradicting what `/active` on the same scope would say.
    `store.project_state()` checks actual claims first, so this can no
    longer happen. `scope.state` describes the scope AS OF this query, not
    atomically with `results`/`why` (their locks are already released by the
    time this runs) -- acceptable for a diagnostic field; see
    docs/HTTP-API.md."""
    state = "has_active_claims" if non_empty else store.project_state(org_id, project)
    return {"project": project, "source": source, "state": state}


def _degraded_status(e: NormalizeUnavailable) -> tuple[int, dict]:
    """Map a NormalizeUnavailable to its HTTP status + body. Shared by
    /remember, /learn, /supersede.

    e.retryable=True  -> 503 (degraded, RETRYABLE: connection error/timeout/
                         429/5xx/auth — worth a retry).
    e.retryable=False -> 502 (bad gateway, NON-retryable: a PERMANENT
                         provider client-error status — 400/404/422/... —
                         re-review #3. This class used to fall through to
                         fail-open and store the write with NO contradiction
                         check; it must fail CLOSED too, just marked
                         non-retryable so callers don't blindly retry a bad
                         model name.)
    """
    status = 503 if e.retryable else 502
    return status, {"result": "degraded", "error": f"normalizer unavailable: {e}"}


# Fixed probe text for /ready's normalizer liveness check (READY-ROUTE) --
# never persisted, never contradiction-checked, just a cheap call shape that
# exercises the SAME normalize() path a real /remember would take so a
# provider outage that 503s guarded writes also flips /ready red.
_READY_PROBE_STATEMENT = "Ergo readiness probe: this statement is never stored."


def _probe_normalizer(normalizer) -> dict:
    """Call the normalizer once with the fixed probe statement and translate
    the outcome into `/ready`'s `normalizer` sub-object. Only
    `NormalizeUnavailable` is caught (the one exception `/remember`'s own
    fail-closed path treats as "provider down") -- any other exception is a
    real bug and should propagate/500, not be swallowed as a false
    "unavailable"."""
    try:
        normalizer.normalize({"text": _READY_PROBE_STATEMENT})
        return {"ok": True}
    except NormalizeUnavailable as e:
        return {"ok": False, "error": str(e), "retryable": e.retryable}


def _build_normalizer():
    # track_context=False: this singleton normalizes every request from every
    # (org_id, project) for the process lifetime, so cross-request pronoun-coref
    # state would resolve one tenant's bare pronoun against another tenant's
    # subject and persist the poisoned norm. Coref context is a single-document
    # feature (explicit `context` arg), never cross-request server state.
    provider = os.environ.get("ERGO_LLM_PROVIDER")
    if not provider:
        return RuleNormalizer(track_context=False), "RuleNormalizer"
    # lazy import so the offline default never imports httpx-using paths
    from .normalize import LLMNormalizer
    try:
        n = LLMNormalizer()
        return n, f"{type(n).__name__}({getattr(n, 'model', '?')})"
    except Exception as e:  # missing key etc. -> fall back, don't crash the server
        sys.stderr.write(f"[ergo] LLM normalizer unavailable ({e}); using RuleNormalizer\n")
        return RuleNormalizer(track_context=False), "RuleNormalizer (LLM fallback)"


def _build_nli():
    if os.environ.get("ERGO_NLI", "on").lower() in ("0", "off", "false", "no"):
        return None, "off"
    try:
        from .nli import NLIJudge
        repo = os.environ.get("ERGO_NLI_MODEL", "Xenova/nli-deberta-v3-base")
        variant = os.environ.get("ERGO_NLI_VARIANT", "quantized")
        j = NLIJudge(repo=repo, variant=variant,
                     cache_dir=os.environ.get("ERGO_NLI_CACHE"),
                     local_files_only=os.environ.get("ERGO_NLI_LOCAL_ONLY", "").lower()
                     in ("1", "true", "yes"))
        return j, f"{repo}:{variant}"
    except Exception as e:
        sys.stderr.write(f"[ergo] NLI judge unavailable ({e}); structural only\n")
        return None, "unavailable"


def _build_embedder():
    """Build the retrieval embedder (default fastembed, baked into the image).

    Warms the model so /health reports retrieval honestly. Never crashes boot:
    any failure (missing model, bad provider) disables retrieval, the
    contradiction firewall keeps running.
    """
    if os.environ.get("ERGO_RETRIEVAL", "on").lower() in ("0", "off", "false", "no"):
        return None, "off (ERGO_RETRIEVAL)"
    try:
        from ergo.embedders import get_embedder
        e = get_embedder()
        vec = e.embed("warmup")  # validate the baked model actually loads (offline)
    except Exception as ex:
        sys.stderr.write(f"[ergo] embedder unavailable ({ex}); retrieval disabled\n")
        return None, f"unavailable: {ex}"
    # Fail LOUD at boot on a declared-vs-actual dim mismatch: the vec table is
    # built as float[e.dim], so a model whose real output length differs (e.g.
    # ERGO_EMBED_MODEL=BAAI/bge-base-en-v1.5 (768) with dim left at the 384
    # default) would boot green — /health says retrieval:true — then 500 EVERY
    # guarded write and ingest at the sqlite-vec insert. Crash here instead so
    # the misconfig is obvious at startup, not at first write. (Validation only:
    # dim is still fixed at vec-table creation; a provider switch is a re-embed
    # migration, never an in-place change.)
    if len(vec) != e.dim:
        raise RuntimeError(
            f"embedder dim mismatch: {type(e).__name__} model "
            f"'{getattr(e, 'model', '?')}' returned {len(vec)} floats but dim is "
            f"configured as {e.dim} — set ERGO_EMBED_DIM to {len(vec)} (or unset "
            f"it to use the model default) and re-create the vec table")
    return e, f"{type(e).__name__}(dim={e.dim})"


def _validate_auth_bind(host: str) -> None:
    """Refuse an unauthenticated listener on anything reachable remotely."""
    if os.environ.get("ERGO_API_TOKEN"):
        return

    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"

    if loopback:
        return
    if os.environ.get("ERGO_INSECURE_NO_AUTH") == "1":
        sys.stderr.write(
            "[ergo] WARNING: ERGO_INSECURE_NO_AUTH=1 — API is OPEN TO THE "
            "NETWORK without ERGO_API_TOKEN\n")
        return
    sys.stderr.write(
        "[ergo] REFUSING TO START: ERGO_API_TOKEN is unset and "
        f"ERGO_HTTP_HOST={host!r} is not a loopback bind; set ERGO_API_TOKEN "
        "or bind ERGO_HTTP_HOST=127.0.0.1 (or another loopback address)\n")
    raise SystemExit(1)


def boot() -> str:
    global _STORE, _CFG, _CONTROL, _REGISTRY, _READY_PROBE_CACHE
    host = os.environ.get("ERGO_HTTP_HOST", "0.0.0.0")
    _validate_auth_bind(host)
    #
    # Same story for the /ready normalizer-probe cache (READY-ROUTE): a
    # fresh instance per boot() so a stale cached result from a previous
    # server lifetime (or a previous test's normalizer) can never leak into
    # this one.
    _READY_PROBE_CACHE = _ReadyProbeCache()
    db_path = os.environ.get("ERGO_DB_PATH", "/data/ergo.db")
    db_dir = os.path.dirname(db_path) or "."
    os.makedirs(db_dir, exist_ok=True)
    normalizer, nname = _build_normalizer()
    nli, nliname = _build_nli()
    embedder, ename = _build_embedder()
    try:
        _STORE = ContradictionStore(db_path, normalizer=normalizer, nli=nli,
                                    embedder=embedder)
    except Exception as e:  # sqlite-vec load / vec-table failure -> firewall-only
        sys.stderr.write(f"[ergo] store+retrieval init failed ({e}); firewall-only\n")
        ename = f"disabled (store init failed: {e})"
        _STORE = ContradictionStore(db_path, normalizer=normalizer, nli=nli)

    # Single-tenant deployment: no control plane, no per-tenant store
    # registry. Every non-admin route already branches on
    # `principal.tenant_id is None` before touching either name, so
    # both stay None.
    _CONTROL = None
    _REGISTRY = None

    _CFG = {
        "db_path": db_path,
        "normalizer": nname,
        "nli": nliname,
        "embedder": ename,
        "retrieval": bool(getattr(_STORE, "retrieval", False)),
        "owner": os.environ.get("ERGO_OWNER", "(unset)"),
        "owner_contact": os.environ.get("ERGO_OWNER_CONTACT", "(unset)"),
        "backup_path": os.environ.get("ERGO_BACKUP_PATH"),
        "kill_review_date": os.environ.get("ERGO_KILL_REVIEW_DATE"),
        "auth": "token" if os.environ.get("ERGO_API_TOKEN") else "OPEN (no token)",
        "http_max_workers": _max_http_workers(),
        "booted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "booted_monotonic": time.monotonic(),
        "last_write_at": None,
    }
    if not os.environ.get("ERGO_API_TOKEN"):
        sys.stderr.write("[ergo] WARNING: no ERGO_API_TOKEN set — API is OPEN (dev/test only)\n")
    sys.stderr.write(f"[ergo] booted: {json.dumps(_CFG)}\n")
    return host


# --- (de)serialization ---------------------------------------------------------
def _ser(obj):
    if is_dataclass(obj):
        return {k: _ser(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_ser(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    return obj


def _diagnose(store) -> dict:
    """Assemble the anti-zombie ops view: live counts + version-drift + ownership / backup /
    kill-review, plus computed anti-zombie warnings the operator can act on. The
    running binary answers for itself instead of pointing at a README.

    `store` is the caller's own store (the legacy singleton, or the tenant store
    resolved from the principal) — /diagnose is scoped to the
    caller's tenant, not a global view."""
    today = time.strftime("%Y-%m-%d")
    try:
        diag = store.diagnostics()
    except Exception as e:
        diag = {"error": f"diagnostics query failed: {e}"}

    last_write_at = diag.get("last_write_at") or _CFG.get("last_write_at")

    # backup freshness: surface the configured target + its mtime if it exists.
    backup_path = _CFG.get("backup_path")
    last_backup_at = None
    if backup_path and os.path.exists(backup_path):
        last_backup_at = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(os.path.getmtime(backup_path)))

    # anti-zombie warnings: things an owner must actually fix.
    warnings = []
    if not backup_path:
        warnings.append("no ERGO_BACKUP_PATH configured — first `docker rm` is unsurvivable")
    elif last_backup_at is None:
        warnings.append(f"backup target '{backup_path}' does not exist yet — no backup taken")
    if _CFG.get("owner") in (None, "(unset)"):
        warnings.append("no named owner (ERGO_OWNER) — anti-zombie ownership unset")

    krd = _CFG.get("kill_review_date")
    if krd:
        kill_review_status = "OVERDUE" if krd < today else "scheduled"
        if kill_review_status == "OVERDUE":
            warnings.append(f"kill-review date {krd} is PAST — owner must review keep/kill")
    else:
        kill_review_status = "unset"
        warnings.append("no ERGO_KILL_REVIEW_DATE — keep/kill review not scheduled")

    versions = diag.get("versions") if isinstance(diag.get("versions"), dict) else {}
    if versions.get("drift"):
        warnings.append("VERSION DRIFT: stored claims span multiple normalizer/judge/schema "
                        "versions — a data-compat hazard")
    counts = diag.get("counts") if isinstance(diag.get("counts"), dict) else {}
    if counts.get("claims_total") == 0:
        warnings.append("store is EMPTY (0 claims) — healthy but holding no knowledge yet")

    uptime_s = None
    if _CFG.get("booted_monotonic") is not None:
        uptime_s = round(time.monotonic() - _CFG["booted_monotonic"], 1)

    out = {
        "service": "ergo contradiction engine",
        "db_path": _CFG.get("db_path"),
        "normalizer": _CFG.get("normalizer"),
        "nli": _CFG.get("nli"),
        "auth": _CFG.get("auth"),
        "booted_at": _CFG.get("booted_at"),
        "uptime_seconds": uptime_s,
        # ownership / operational provenance
        "owner_name": _CFG.get("owner"),
        "owner_contact": _CFG.get("owner_contact"),
        "backup_path": backup_path,
        "last_backup_at": last_backup_at,
        "last_write_at": last_write_at,
        "last_successful_import_at": None,  # packs not wired into this store yet
        "kill_review_date": krd,
        "kill_review_status": kill_review_status,
        "warnings": warnings,
    }
    # fold in the live DB-derived blocks (counts / conflicts / versions)
    for k in ("counts", "conflicts", "versions"):
        if k in diag:
            out[k] = diag[k]
    if "error" in diag:
        out["diagnostics_error"] = diag["error"]
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ergo/0.1"

    def setup(self):
        # Socket timeout so a stalled client (Content-Length promising more
        # bytes than ever arrive, or a half-open peer) is dropped instead of
        # blocking the single-threaded server forever: handle_one_request turns
        # the TimeoutError into a closed connection and the next queued request
        # proceeds. Read per connection (like the auth token) so it's tunable.
        try:
            self.timeout = float(os.environ.get("ERGO_HTTP_TIMEOUT", "") or 30.0)
        except ValueError:
            self.timeout = 30.0
        super().setup()

    def log_message(self, fmt, *args):  # access log -> stderr
        # Structured JSON logging (ergo's private-repo issue #121): plaintext stays the
        # DEFAULT (nothing changes unless an operator opts in) -- only when
        # ERGO_LOG_FORMAT=json is set does this emit one parseable JSON
        # object per line instead. Applies to every call BaseHTTPRequestHandler
        # routes through log_message, not just the access log (its own
        # log_error() -- e.g. a malformed request line -- funnels here too).
        if _log_format() == "json":
            sys.stderr.write(json.dumps({
                "ts": _iso_now(),
                "client": self.address_string(),
                "message": fmt % args,
            }) + "\n")
        else:
            sys.stderr.write("[ergo] %s - %s\n" % (self.address_string(), fmt % args))

    def _record_request_metrics(self, code: int) -> None:
        """Record this request's route/method/status/latency + (for the
        three guarded-write routes) its stored/conflict/quota/5xx outcome
        (ergo's private-repo issue #121). Called from `_send`/`_send_head`, the single
        terminal call every route handler makes exactly once per request --
        no other instrumentation point is needed.

        The route label is normalized against `_KNOWN_ROUTES` (unbounded
        cardinality otherwise) BEFORE it ever reaches the metrics
        registry -- the raw request path is attacker-controlled and every
        unmatched path already falls through to a 404, so using it verbatim
        as a label would let a client grow the in-process counters/
        histograms without bound just by hitting distinct nonexistent
        paths, no auth required."""
        route = _metrics_route_label(urlparse(self.path).path)
        method = self.command or "?"
        t0 = getattr(self, "_req_t0", None)
        elapsed = (time.monotonic() - t0) if t0 is not None else 0.0
        metrics.REQUEST_COUNT.inc((method, route, str(code)))
        metrics.REQUEST_DURATION.observe((method, route), elapsed)
        outcome = _guarded_write_outcome(route, code)
        if outcome is not None:
            metrics.WRITE_OUTCOME.inc((route, outcome))

    # --- helpers ---
    def _send(self, code, payload, headers: dict | None = None):
        # `headers` is currently only used for 429's Retry-After (TENANT-
        # QUOTAS) -- kept optional/last so every existing call site is
        # untouched.
        body = json.dumps(_ser(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self._record_request_metrics(code)

    def _send_head(self, code, payload, headers: dict | None = None):
        """HEAD counterpart of `_send`: same status + headers a GET request
        for this same `payload` would receive (including an accurate
        Content-Length), but the body itself is never written -- a HEAD
        response MUST NOT have a body (RFC 9110 §9.3.2)."""
        body = json.dumps(_ser(payload)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self._record_request_metrics(code)

    def _send_text(self, code, text: str, content_type: str = "text/plain; charset=utf-8"):
        """Like `_send`, but for a plain-text body (only `/metrics` today --
        Prometheus text exposition format is not JSON)."""
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._record_request_metrics(code)

    def _authed(self) -> bool:
        token = os.environ.get("ERGO_API_TOKEN")
        if not token:
            return True
        hdr = self.headers.get("Authorization", "")
        return hmac.compare_digest(hdr, f"Bearer {token}")

    def _resolve_principal(self) -> Principal | None:
        """Resolve the bearer token to a Principal, or None if unauthorized.

        Back-compat: a bearer equal to ERGO_API_TOKEN (or
        no ERGO_API_TOKEN configured at all — today's open dev/test mode) always
        resolves to LEGACY_PRINCIPAL, so nothing about today's behavior changes
        for existing callers.
        Any other bearer is rejected -- this build has no control plane
        to resolve it against.
        """
        if self._authed():
            return LEGACY_PRINCIPAL
        hdr = self.headers.get("Authorization", "")
        if _CONTROL is not None and hdr.startswith("Bearer "):
            principal = _CONTROL.resolve(hdr[len("Bearer "):])
            if principal is not None:
                return principal
        return None

    def _is_superadmin(self) -> bool:
        """A bearer equal to ERGO_SUPERADMIN_TOKEN, and ONLY that bearer, is the
        superadmin principal
(this build's only other gate, GET /metrics)        . Deliberately separate from
        _authed()/_resolve_principal(): a valid tenant token or the legacy
        ERGO_API_TOKEN must NOT pass this check."""
        token = os.environ.get("ERGO_SUPERADMIN_TOKEN")
        if not token:
            return False
        hdr = self.headers.get("Authorization", "")
        return hmac.compare_digest(hdr, f"Bearer {token}")

    def _metrics_authed(self) -> bool:
        """Operator-only gate for `GET /metrics` (ergo's private-repo issue #121).

        Unlike `/health`/`/ready` (deliberately always-open monitor routes --
        see their own docstrings), `/metrics` exposes per-route latency and
        guarded-write outcome counts AGGREGATED ACROSS EVERY TENANT, so it
        never gets that same always-open treatment, and -- unlike an
        ordinary tenant route -- a valid PER-TENANT token must never be
        enough to read it: that would hand one tenant another tenant's
        operational data on a surface that was never meant to be
        tenant-scoped in the first place.

        Prefers the superadmin bearer when one is configured. Falls back to the exact-match legacy gate
        (`ERGO_API_TOKEN`, via `_authed()` -- safe here specifically BECAUSE
        a configured legacy token makes `_authed()` an exact
        `hmac.compare_digest` against it, which a per-tenant token can never
        satisfy) when no superadmin token is configured, so a single-tenant
        deployment that never provisioned one still gets SOME operator gate.

        When NEITHER env var is configured, `/metrics` falls back to today's
        fully-open dev/test default (the same posture every other route
        already has when no credential is presented at all). This build has
        no control plane and no per-tenant bearer to reject, so the
        `_CONTROL is not None` guard below is always false here.

        """
        if os.environ.get("ERGO_SUPERADMIN_TOKEN"):
            return self._is_superadmin()
        if os.environ.get("ERGO_API_TOKEN"):
            return self._authed()
        hdr = self.headers.get("Authorization", "")
        if _CONTROL is not None and hdr.startswith("Bearer "):
            if _CONTROL.resolve(hdr[len("Bearer "):]) is not None:
                return False
        return True


    def _store_and_org(self, principal: Principal):
        """Legacy principal (tenant_id is None) -> the existing singleton store,
        org_id left for the caller to supply (today's behavior, driven by body/
        query). Real principal -> the tenant's own store from the registry, with
        org_id pinned to the principal — NEVER trusted from client input.

        For a real principal this hands out an active LEASE on the tenant's
        store (tenant_registry.py, finding #4): the caller MUST release it
        via `_release_store(principal)` in a `finally` once done, so idle
        eviction or a concurrent tenant delete can never close the connection
        out from under this in-flight request."""
        if principal.tenant_id is None:
            return _STORE, None
        return _REGISTRY.get(principal.tenant_id), principal.org_id

    def _release_store(self, principal: Principal) -> None:
        """Release the lease acquired by `_store_and_org` for a real (non-legacy)
        principal. No-op for the legacy singleton store, which is never leased."""
        if principal.tenant_id is not None and _REGISTRY is not None:
            _REGISTRY.release(principal.tenant_id)

    def _enforce_rate_limit(self, principal: Principal, route_class: str) -> bool:
        """Called right after
        principal resolution in both _route_get ("read") and _route_post
        ("write"), BEFORE any store lease is taken. Legacy (pooled,
        tenant_id is None) and superadmin principals are exempt --
        superadmin never reaches here at all (dispatched to _admin() first).

        Returns True if the request was REJECTED (the 429 response, with
        Retry-After, has already been sent) -- the caller MUST `return`
        immediately without doing anything else. Returns False if the
        request may proceed normally.

        CRITICAL (finding): this used to `return self._send(...)`
        directly, and BOTH call sites checked `if rate_limited is not None:
        return rate_limited`. But `Handler._send()` has no return statement
        -- it always evaluates to None -- so that check was ALWAYS false,
        rejected or not. A rate-limited request's handler kept running and
        called `self._send()` a SECOND time for the real route underneath,
        writing TWO complete HTTP responses back-to-back onto the same
        socket. httpx-based tests still saw the correct 429 because the
        client stops reading once it has consumed the first response's
        Content-Length and closes the connection, silently discarding the
        second write -- but the underlying route body still executed
        despite being "rate limited" (a real POST /remember would still
        have written to the database), and on a real HTTP/1.1 keep-alive
        connection the leftover bytes would corrupt the NEXT request parsed
        off that connection. Reproduced with a raw socket read (not httpx)
        showing two concatenated `HTTP/1.0 ...` status lines in one
        response. Returning an explicit bool instead of `_send`'s return
        value closes this."""
        if principal.tenant_id is None:
            return False
        allowed, retry_after = _check_rate_limit(principal, route_class)
        if allowed:
            return False
        self._send(
            429,
            {"error": "tenant rate limit exceeded",
             "route_class": route_class,
             "limit_per_min": _effective_rate_limit(principal),
             "retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)})
        return True

    def _body(self, max_bytes: int | None = None) -> dict:
        """Parse the JSON request body, bounded (finding #2).

        `max_bytes` overrides the default ERGO_MAX_BODY_BYTES cap (used by
        POST /ingest, which allows a larger ceiling). Content-Length is
        validated and capped BEFORE any bytes are read off the socket: a
        missing/empty header means no body (unchanged); a non-integer value
        raises ValueError (-> 400); a negative value (notably -1, which
        would otherwise make rfile.read(-1) read-until-EOF and hang the
        handler thread on a client that never closes the connection) raises
        ValueError (-> 400) instead of ever touching rfile.read(); a value
        over the cap raises RequestTooLarge (-> 413).
        """
        cap = max_bytes if max_bytes is not None else _max_body_bytes()
        raw = self.headers.get("Content-Length")
        if raw is None or raw == "":
            return {}
        try:
            n = int(raw)
        except ValueError:
            raise ValueError(f"invalid Content-Length: {raw!r}")
        if n < 0:
            raise ValueError(f"invalid Content-Length: {raw!r}")
        if n == 0:
            return {}
        if n > cap:
            raise RequestTooLarge(
                f"request body of {n} bytes exceeds the {cap}-byte limit")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            # valid JSON but not an object ([1,2,3], null, "x", ...) — every
            # POST route reads fields off it, so reject here once as a 400
            # instead of letting body.get() blow up the handler mid-response.
            raise ValueError(
                f"expected a JSON object, got {type(body).__name__}")
        return body

    # --- routing ---
    def do_GET(self):
        # Bounded concurrency (finding #1): admission is gated earlier now,
        # at connection/thread-creation time
        # by BoundedThreadingHTTPServer.process_request -- the permit is
        # already held for the lifetime of this thread, so do_GET itself no
        # longer needs its own acquire/release (double-acquiring the same
        # semaphore here would halve real concurrency and could deadlock at
        # ERGO_HTTP_MAX_WORKERS=1).
        self._req_t0 = time.monotonic()  # ergo's private-repo issue #121: /metrics latency
        self._route_get()

    def do_HEAD(self):
        # Same admission story as do_GET above: BoundedThreadingHTTPServer
        # already gates concurrency at thread-creation time, so HEAD needs no
        # additional acquire/release either -- it just needs to land on the
        # SAME path (a monitor sends HEAD /health, never GET) rather than a
        # separate, unguarded code path.
        self._req_t0 = time.monotonic()  # ergo's private-repo issue #121: /metrics latency
        u = urlparse(self.path)
        if u.path == "/health":
            code, payload = self._health_status(self._health_authed())
            return self._send_head(code, payload)
        if u.path == "/ready":
            code, payload = self._ready_status(self._health_authed())
            return self._send_head(code, payload)
        # Every other route requires auth/body handling that only matters for
        # a response body a HEAD request will never receive -- 405 rather
        # than reimplementing each route's admission just to throw the body
        # away.
        return self._send_head(
            405, {"error": "method not allowed", "path": u.path},
            headers={"Allow": "GET, POST"})

    def _health_authed(self) -> bool:
        """Auth gate for the FULL `/health`/`/ready` payload (private-repo issue #187).

        `/health` and `/ready` themselves stay reachable by anyone -- no
        bearer at all still gets a 200/503 with the public allowlist below
        -- but the ops/model detail (`db_path`, `normalizer`, `nli`,
        `embedder`, `booted_at`; `/ready`'s normalizer-probe detail) is the
        same class of ops metadata `/diagnose` already gates on a resolved
        `Principal`, so this reuses that exact gate rather than inventing a
        new one:
        any bearer that resolves (only the legacy `ERGO_API_TOKEN` in this
        build -- there is no control plane to resolve a per-tenant token
        against) unlocks the full
        payload. An invalid/revoked bearer resolves to `None` and falls
        back to the public allowlist rather than a 401 -- these routes must
        stay reachable by monitors, which may not carry a bearer at all.

        When no `ERGO_API_TOKEN` is configured at all (the documented
        dev/test open-API mode), `_resolve_principal()` already resolves
        ANY caller to `LEGACY_PRINCIPAL` -- the same open posture every
        other route already has in that mode -- so an open dev/test
        deployment keeps returning the full payload regardless of bearer.
        A real deployment always sets `ERGO_API_TOKEN`, so this can't
        become a leak in production.

        This build never constructs a control store at all (`_CONTROL`
        is always `None`), so `_resolve_principal()` never reaches into
        it here -- the guard exists for the (private, multi-tenant)
        deployment shape this code was written for, where liveness must
        never depend on a control store's health: a fault there must
        degrade to the public/unauthenticated payload, never crash the
        route the release gate and the anti-zombie check depend on. The
        engine's own health is what `/health`/`/ready` exist to report.
        """
        try:
            return self._resolve_principal() is not None
        except Exception:
            return False

    def _health_status(self, authed: bool) -> tuple[int, dict]:
        """Build the `/health` response (status code + JSON body).

        Shared by `_route_get` (GET, writes the body) and `do_HEAD` (HEAD,
        needs the same payload only to compute an accurate Content-Length).
        /health is unauthenticated (reachable with no bearer at all), but
        the body itself is now split by `authed` (private-repo issue #187): `status`,
        `retrieval`, and `probe_state` are the public allowlist -- `retrieval`
        stays public deliberately, since "`status: ok` + `retrieval: true`"
        is the documented session-start pre-flight every client relies on.
        Ownership/ops metadata (owner, contact, backup_path,
        kill_review_date, ...) was never here -- that's `/diagnose` -- and
        the model-stack/on-disk-path detail (`db_path`, `normalizer`,
        `nli`, `embedder`, `booted_at`) now joins it behind the same
        bearer-auth gate as `/diagnose` (see `_health_authed`).
        """
        state = "unknown"
        try:
            # No `limit` -> Store.active()'s unbounded opt-out mode (issue
            # #185 non-negotiable #5): this probe only ever reads `.state`,
            # never `.claims`, so it must not silently inherit the /active
            # route's default cap or pay for the paginated mode's extra
            # COUNT(*).
            state = _STORE.active(
                "_probe", "_probe").state  # cheap readiness probe
        except Exception as e:
            if not authed:
                return 503, {"status": "unhealthy"}
            return 503, {"status": "unhealthy", "error": str(e)}
        if not authed:
            return 200, {
                "status": "ok",
                "retrieval": _CFG.get("retrieval"),
                "probe_state": state,
            }
        return 200, {
            "status": "ok",
            "db_path": _CFG.get("db_path"),
            "normalizer": _CFG.get("normalizer"),
            "nli": _CFG.get("nli"),
            "embedder": _CFG.get("embedder"),
            "retrieval": _CFG.get("retrieval"),
            "booted_at": _CFG.get("booted_at"),
            "probe_state": state,
        }

    def _ready_status(self, authed: bool) -> tuple[int, dict]:
        """Build the `/ready` response (status code + JSON body).

        Sibling to `_health_status` above, but closes the gap that method
        leaves open: `/health` only proves the DB is
        reachable, so a normalizer-provider outage leaves `/health` green
        while every guarded write (`/remember`/`/learn`/`/supersede`) 503s
        with `NormalizeUnavailable` -- a monitor watching `/health` alone
        can't see a write-path outage. `/ready` does BOTH the same cheap DB
        probe `/health` does AND a normalizer liveness probe, so it
        actually reflects write-path readiness.

        Also unauthenticated (reachable with no bearer), same as `/health`
        -- but split by `authed` the same way (private-repo issue #187): `status` and
        `probe_state` are the public allowlist; the normalizer-probe detail
        (`normalizer`, `checked_at`, `cached`) -- which can include raw
        provider error strings on the `degraded` path -- moves behind the
        same bearer-auth gate `/health`'s model-stack detail uses (see
        `_health_authed`). The 503/`degraded` status itself never changes
        for an unauthenticated caller -- only how much detail comes with it.

        The normalizer probe is TTL-cached (module-scope
        `_READY_PROBE_CACHE`, lock-guarded -- see its docstring) so this
        unauthenticated, LLM-calling route can't be turned into a cost/DoS
        vector: at most one real `normalize()` call per
        ERGO_READY_PROBE_TTL_SECONDS window (default 15s; 0 disables the
        cache and probes every hit). Still an ON-DEMAND probe (it actively
        calls the normalizer every TTL window) rather than a passive
        last-known-state read of real write traffic.
        """
        state = "unknown"
        try:
            # No `limit` -> unbounded opt-out mode, same reasoning as
            # _health_status above (private-repo issue #185 non-negotiable #5).
            state = _STORE.active(
                "_probe", "_probe").state  # same cheap DB probe /health uses
        except Exception as e:
            if not authed:
                return 503, {"status": "unhealthy"}
            return 503, {"status": "unhealthy", "error": str(e)}
        norm_result, cached, checked_at = _READY_PROBE_CACHE.probe(
            _STORE.normalizer, _ready_probe_ttl_seconds())
        if not norm_result["ok"]:
            if not authed:
                return 503, {"status": "degraded", "probe_state": state}
            return 503, {
                "status": "degraded",
                "normalizer": norm_result,
                "probe_state": state,
                "checked_at": checked_at,
                "cached": cached,
            }
        if not authed:
            return 200, {"status": "ready", "probe_state": state}
        return 200, {
            "status": "ready",
            "normalizer": norm_result,
            "probe_state": state,
            "checked_at": checked_at,
            "cached": cached,
        }

    def _route_get(self):
        u = urlparse(self.path)
        if u.path == "/health":
            code, payload = self._health_status(self._health_authed())
            return self._send(code, payload)
        if u.path == "/ready":
            code, payload = self._ready_status(self._health_authed())
            return self._send(code, payload)
        if u.path == "/metrics":
            # Operator-only (ergo's private-repo issue #121) -- see _metrics_authed's
            # docstring for the exact gate. 401, not 404: /metrics is not a
            # feature that's ever "off", so it never hides its own existence.
            if not self._metrics_authed():
                return self._send(401, {"error": "unauthorized"})
            gauges = {
                "active": getattr(self.server, "_active_count", 0),
                "max": _max_http_workers(),
            }
            return self._send_text(200, metrics.render(gauges=gauges),
                                   content_type="text/plain; version=0.0.4; charset=utf-8")
        principal = self._resolve_principal()
        if principal is None:
            return self._send(401, {"error": "unauthorized"})
        if self._enforce_rate_limit(principal, "read"):
            return
        try:
            store, forced_org = self._store_and_org(principal)
        except LookupError as e:
            # registry.get() refuses a lease while this tenant is mid-delete
            # (finding #4) -- temporary, not a broken store: 503, not 500.
            return self._send(503, {"error": str(e)})
        except Exception as e:  # tenant store open failure -> clean 500, not a dropped connection
            return self._send(500, {"error": f"tenant store unavailable: {e}"})
        try:
            q = parse_qs(u.query)
            # forced_org (a real, non-legacy principal) always wins over a
            # client-supplied org_id — org_id comes from the principal, never
            # from client input.
            org = forced_org if forced_org is not None else (q.get("org_id") or [""])[0]
            explicit_proj = (q.get("project") or [""])[0]
            if u.path == "/active":
                if not org:
                    return self._send(400, {"error": "org_id required"})
                warnings: list = []
                proj = _resolve_project(explicit_proj, principal, warnings).project
                # Bounded page by default (private-repo issue #185) -- mirrors /audit's own
                # query-param clamp immediately below; Store.active() clamps
                # again internally so a malformed value here still can't slip
                # through as unbounded.
                try:
                    limit = max(1, min(int((q.get("limit") or [str(DEFAULT_ACTIVE_LIMIT)])[0]),
                                       MAX_ACTIVE_LIMIT))
                except ValueError:
                    limit = DEFAULT_ACTIVE_LIMIT
                try:
                    offset = max(0, int((q.get("offset") or ["0"])[0]))
                except ValueError:
                    offset = 0
                # `projection` (private-repo issue #206): unrecognized values degrade to
                # "full" here too, even though Store.active() re-validates --
                # same belt-and-suspenders shape as the limit/offset clamps
                # above.
                projection = (q.get("projection") or ["full"])[0]
                if projection not in ACTIVE_PROJECTIONS:
                    projection = "full"
                result = _ser(store.active(org, proj, limit=limit, offset=offset,
                                            projection=projection))
                if warnings:
                    result["warnings"] = warnings
                return self._send(200, result)
            if u.path == "/history":
                if not org:
                    return self._send(400, {"error": "org_id required"})
                warnings = []
                proj = _resolve_project(explicit_proj, principal, warnings).project
                resp = {"history": store.history(org, proj)}
                if warnings:
                    resp["warnings"] = warnings
                return self._send(200, resp)
            if u.path == "/audit":
                # Tenant-facing audit read (private-repo issue #124): the per-org `writes` +
                # `conflict_events` rows a customer needs for a compliance/
                # security self-serve request. `org` above already resolved
                # forced_org (the token's own tenant) over any client-supplied
                # org_id -- a tenant can NEVER read another org's audit rows
                # by passing a different `?org_id=`.
                if not org:
                    return self._send(400, {"error": "org_id required"})
                warnings = []
                proj = _resolve_project(explicit_proj, principal, warnings).project
                try:
                    limit = max(1, min(int((q.get("limit") or [str(DEFAULT_AUDIT_LIMIT)])[0]),
                                       MAX_AUDIT_LIMIT))
                except ValueError:
                    limit = DEFAULT_AUDIT_LIMIT
                resp = store.audit_log(org, proj, limit=limit)
                if warnings:
                    resp["warnings"] = warnings
                return self._send(200, resp)
            if u.path in ("/recall", "/why"):
                if not org:
                    return self._send(400, {"error": "org_id required"})
                warnings = []
                resolved = _resolve_project(explicit_proj, principal, warnings)
                proj = resolved.project
                qy = (q.get("q") or q.get("query") or [""])[0]
                if not qy:
                    return self._send(400, {"error": "q (query) required"})
                if len(qy) > _max_field_chars():
                    return self._send(413, {"error": f"field 'q' exceeds max "
                                            f"length of {_max_field_chars()} chars"})
                if not getattr(store, "retrieval", False):
                    return self._send(503, {"error": "retrieval not enabled on this server"})
                try:
                    if u.path == "/why":
                        result = store.why(org, proj, qy)
                        suppressed = None
                        if isinstance(result, dict) and "suppressed" in result:
                            suppressed, result = result["suppressed"], None
                        resp = {"why": result}
                        if suppressed is not None:
                            resp["suppressed"] = suppressed
                        if warnings:
                            resp["warnings"] = warnings
                        resp["scope"] = _scope_info(
                            store, org, proj, resolved.source,
                            non_empty=result is not None or suppressed is not None)
                        return self._send(200, resp)
                    try:
                        limit = max(1, min(int((q.get("limit") or ["10"])[0]), 50))
                    except ValueError:
                        limit = 10
                    results = store.recall(org, proj, qy, limit=limit)
                    resp = {"results": results}
                    if warnings:
                        resp["warnings"] = warnings
                    resp["scope"] = _scope_info(store, org, proj, resolved.source,
                                                non_empty=bool(results))
                    return self._send(200, resp)
                except Exception as e:
                    return self._send(500, {"error": str(e)})
            if u.path == "/diagnose":
                return self._send(200, _diagnose(store))
            if u.path == "/export":
                # Export is never gated in this build (private-repo issue #109).
                if not org:
                    return self._send(400, {"error": "org_id required"})
                gate = _export_import_gate(principal, "export")
                if gate is not None:
                    return self._send(403, gate)
                project_filter = (q.get("project") or [None])[0]
                if project_filter is not None and not project_filter.strip():
                    project_filter = None
                # Pagination (private-repo issue #203) is opt-in: omitting `limit`
                # entirely stays UNBOUNDED -- backward compat, unlike
                # /active's always-capped default (an existing backup/
                # migration caller must keep getting the full scope, never
                # a silently truncated file). A PRESENT `limit` is parsed/
                # clamped here (same clamp-and-degrade contract as
                # /active) before Store.export_claims clamps again
                # internally.
                limit_values = q.get("limit") or []
                if limit_values:
                    try:
                        limit = max(1, min(int(limit_values[0]), MAX_EXPORT_LIMIT))
                    except (TypeError, ValueError):
                        limit = DEFAULT_EXPORT_LIMIT
                else:
                    limit = None
                try:
                    offset = max(0, int((q.get("offset") or ["0"])[0]))
                except (TypeError, ValueError):
                    offset = 0
                result = store.export_claims(org, project=project_filter,
                                             limit=limit, offset=offset)
                provider = (type(store.embedder).__name__
                           if getattr(store, "embedder", None) is not None else None)
                return self._send(200, {
                    "ergo_export_version": 2,
                    "org_id": org,
                    "exported_at": _iso_now(),
                    "embedder": {"provider": provider, "dim": getattr(store, "embed_dim", None)},
                    "claim_count": result.count,
                    "returned": result.returned,
                    "truncated": result.truncated,
                    "next_offset": result.next_offset,
                    "as_of_seq": result.as_of_seq,
                    "claims": result.claims,
                })
            return self._send(404, {"error": "not found", "path": u.path})
        finally:
            self._release_store(principal)

    def do_POST(self):
        # See do_GET's comment: admission is already bounded at thread
        # creation, so no per-route semaphore guard is needed here either.
        self._req_t0 = time.monotonic()  # ergo's private-repo issue #121: /metrics latency
        self._route_post()

    def _route_post(self):
        u = urlparse(self.path)
        principal = self._resolve_principal()
        if principal is None:
            return self._send(401, {"error": "unauthorized"})
        if self._enforce_rate_limit(principal, "write"):
            return
        # /ingest gets a larger body cap than every other guarded write --
        # legitimate reference docs run bigger than a statement/reason/source
        # triple (finding #2). /import shares that larger cap: a bulk claims
        # payload legitimately runs bigger than any other route's body too.
        # This build has no plan gate and no per-plan claim-count cap, so this
        # raw-bytes floor is the only ceiling on an import's body size.
        cap = _max_ingest_bytes() if u.path in ("/ingest", "/import") else None
        try:
            body = self._body(max_bytes=cap)
        except RequestTooLarge as e:
            return self._send(413, {"error": str(e)})
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        try:
            store, forced_org = self._store_and_org(principal)
        except LookupError as e:
            # registry.get() refuses a lease while this tenant is mid-delete
            # (finding #4) -- temporary, not a broken store: 503, not 500.
            return self._send(503, {"error": str(e)})
        except Exception as e:  # tenant store open failure -> clean 500, not a dropped connection
            return self._send(500, {"error": f"tenant store unavailable: {e}"})
        try:
            return self._dispatch_post(u, body, store, forced_org, principal)
        finally:
            self._release_store(principal)

    def _dispatch_post(self, u, body, store, forced_org, principal):
        if forced_org is not None:
            # Real (non-legacy) principal: org_id and who are authoritative from
            # the token, never the body. `project` stays a body-supplied
            # namespace, not an auth boundary.
            body["org_id"] = forced_org
            body["who"] = principal.who

        if u.path == "/remember":
            for f in ("org_id", "who", "statement"):
                if not body.get(f):
                    return self._send(400, {"error": f"missing field: {f}"})
            # DEFAULT-PROJECT (private-repo issue #179): `project` is optional -- resolved
            # in order (explicit -> token's default_project -> literal
            # "default", with a warning) rather than 400ing, see
            # _resolve_project's docstring.
            project_warnings: list = []
            body["project"] = _resolve_project(body.get("project"), principal,
                                               project_warnings).project
            if not body["statement"].strip():
                return self._send(400, {"error": "statement must not be blank"})
            oversized = _oversized_field(body, ("statement", "reason"))
            if oversized:
                return self._send(413, {"error": f"field '{oversized}' exceeds "
                                        f"max length of {_max_field_chars()} chars"})
            # PLAN-METERING: monthly write cap, checked-and-incremented
            # atomically BEFORE the gate runs -- see _enforce_monthly_write_cap.
            # None means unlimited (legacy/superadmin exempt, or a real
            # tenant with plan=NULL / grandfathered).
            plan_lims = _plan_limits_for(principal)
            project_limit = None
            if plan_lims is not None:
                allowed, err_body = _enforce_monthly_write_cap(principal, plan_lims)
                if not allowed:
                    return self._send(403, err_body)
                project_limit = plan_lims["project_limit"]
            elif principal.tenant_id is not None:
                # UNMETERED-COUNT (private-repo issue #215): a real tenant with plan=NULL
                # (grandfathered) is exempt from enforcement but NOT from
                # counting -- record the write so the admin stats surface
                # stops reporting a permanent, false 0. Advisory only: never
                # a limit check, never a 403. Legacy/superadmin principals
                # (tenant_id is None) stay skipped -- no tenant to key on.
                _CONTROL.record_monthly_write(principal.tenant_id)
            # The cap is enforced ATOMICALLY inside
            # store.remember()'s own write-guard transaction (see
            # QuotaExceeded's docstring in store.py) -- None for a legacy/
            # superadmin principal means "no check at all" (exempt).
            max_active_claims = (_effective_max_active_claims(principal)
                                 if principal.tenant_id is not None else None)
            try:
                r = store.remember(body["org_id"], body["project"], body["who"],
                                   body["statement"], reason=body.get("reason"),
                                   force_exception=body.get("force_exception"),
                                   max_active_claims=max_active_claims,
                                   project_limit=project_limit)
            except QuotaExceeded as e:
                return self._send(403, {
                    "error": f"tenant active-claim cap of {e.cap} reached "
                            f"(current: {e.current})",
                    "max_active_claims": e.cap, "active_claims": e.current})
            except ProjectLimitExceeded as e:
                return self._send(403, {
                    "error": "project_limit_reached", "plan": principal.plan,
                    "limit": e.cap, "used": e.current, "resets_at": None,
                    "upgrade": "https://ergomem.com/pricing"})
            except NormalizeUnavailable as e:
                # Provider error (connection/timeout/429/5xx/auth, OR a
                # permanent 400/404/422) with no force_exception override:
                # fail CLOSED, not a 500 (see _degraded_status — 503 retryable
                # / 502 permanent, finding #3).
                status, resp = _degraded_status(e)
                return self._send(status, resp)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            _CFG["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            kind = "stored" if isinstance(r, Stored) else "conflict"
            r.warnings = project_warnings + r.warnings
            return self._send(200 if isinstance(r, Stored) else 409,
                              {"result": kind, **_ser(r)})

        if u.path == "/learn":
            # "from reading <source>, I believe <statement>": the full
            # contradiction pipeline (same as /remember) with source provenance
            # folded into the stored reason so it surfaces via /why and /recall.
            for f in ("org_id", "who", "statement", "source"):
                if not body.get(f):
                    return self._send(400, {"error": f"missing field: {f}"})
            # DEFAULT-PROJECT (private-repo issue #179): see /remember above.
            project_warnings: list = []
            body["project"] = _resolve_project(body.get("project"), principal,
                                               project_warnings).project
            if not body["statement"].strip():
                return self._send(400, {"error": "statement must not be blank"})
            oversized = _oversized_field(body, ("statement", "reason", "source"))
            if oversized:
                return self._send(413, {"error": f"field '{oversized}' exceeds "
                                        f"max length of {_max_field_chars()} chars"})
            # PLAN-METERING: same atomic monthly-cap enforcement as /remember above.
            plan_lims = _plan_limits_for(principal)
            project_limit = None
            if plan_lims is not None:
                allowed, err_body = _enforce_monthly_write_cap(principal, plan_lims)
                if not allowed:
                    return self._send(403, err_body)
                project_limit = plan_lims["project_limit"]
            elif principal.tenant_id is not None:
                # UNMETERED-COUNT (private-repo issue #215): see /remember above.
                _CONTROL.record_monthly_write(principal.tenant_id)
            # Same atomic enforcement as /remember above.
            max_active_claims = (_effective_max_active_claims(principal)
                                 if principal.tenant_id is not None else None)
            try:
                r = store.learn(body["org_id"], body["project"], body["who"],
                                body["statement"], body["source"],
                                reason=body.get("reason"),
                                force_exception=body.get("force_exception"),
                                max_active_claims=max_active_claims,
                                project_limit=project_limit)
            except ValueError as e:  # empty/blank source -> client error, not 500
                return self._send(400, {"error": str(e)})
            except QuotaExceeded as e:  # see /remember's identical mapping
                return self._send(403, {
                    "error": f"tenant active-claim cap of {e.cap} reached "
                            f"(current: {e.current})",
                    "max_active_claims": e.cap, "active_claims": e.current})
            except ProjectLimitExceeded as e:  # see /remember's identical mapping
                return self._send(403, {
                    "error": "project_limit_reached", "plan": principal.plan,
                    "limit": e.cap, "used": e.current, "resets_at": None,
                    "upgrade": "https://ergomem.com/pricing"})
            except NormalizeUnavailable as e:  # see /remember's identical mapping
                status, resp = _degraded_status(e)
                return self._send(status, resp)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            _CFG["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            # Mirror /remember's 200/409 mapping: a learned belief that blocks on
            # an existing active claim is a Conflict (nothing written) -> 409.
            kind = "learned" if isinstance(r, Stored) else "conflict"
            r.warnings = project_warnings + r.warnings
            return self._send(200 if isinstance(r, Stored) else 409,
                              {"result": kind, "source": body["source"], **_ser(r)})

        if u.path == "/ingest":
            for f in ("org_id", "who", "text"):
                if not body.get(f):
                    return self._send(400, {"error": f"missing field: {f}"})
            # DEFAULT-PROJECT (private-repo issue #179): see /remember above.
            project_warnings: list = []
            body["project"] = _resolve_project(body.get("project"), principal,
                                               project_warnings).project
            oversized = _oversized_field(body, ("source",))
            if oversized:
                return self._send(413, {"error": f"field '{oversized}' exceeds "
                                        f"max length of {_max_field_chars()} chars"})
            if not getattr(store, "retrieval", False):
                return self._send(503, {"error": "retrieval not enabled on this server"})
            # Per-chunk aware AND atomic -- ingest_text() now
            # inserts the whole batch in ONE store transaction
            # (remember_facts_batch) that checks the cap once, against the
            # dedup-aware count of NEW (non-duplicate) chunks this call would
            # actually add, and refuses the WHOLE batch up front (nothing
            # stored) if that would cross it. See QuotaExceeded's docstring
            # in store.py for why this has to happen inside the store's own
            # locked transaction rather than a preflight count here.
            max_active_claims = (_effective_max_active_claims(principal)
                                 if principal.tenant_id is not None else None)
            try:
                r = ingest_text(store, body["text"], who=body["who"],
                                org_id=body["org_id"], project=body["project"],
                                source=body.get("source"),
                                max_chunks=_max_ingest_chunks(),
                                max_active_claims=max_active_claims)
            except ChunkLimitExceeded as e:
                return self._send(413, {"error": str(e)})
            except QuotaExceeded as e:
                return self._send(403, {
                    "error": f"ingest of {e.additions} new chunks would exceed "
                            f"the tenant active-claim cap of {e.cap} "
                            f"(current: {e.current})",
                    "max_active_claims": e.cap, "active_claims": e.current,
                    "chunks": e.additions})
            except Exception as e:
                return self._send(500, {"error": str(e)})
            _CFG["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if project_warnings:
                r["warnings"] = project_warnings
            return self._send(200, {"result": "ingested", **r})

        if u.path == "/supersede":
            for f in ("org_id", "who", "existing_id", "statement"):
                if not body.get(f):
                    return self._send(400, {"error": f"missing field: {f}"})
            # DEFAULT-PROJECT (private-repo issue #179): see /remember above.
            project_warnings: list = []
            body["project"] = _resolve_project(body.get("project"), principal,
                                               project_warnings).project
            if not body["statement"].strip():
                return self._send(400, {"error": "statement must not be blank"})
            oversized = _oversized_field(body, ("statement", "reason"))
            if oversized:
                return self._send(413, {"error": f"field '{oversized}' exceeds "
                                        f"max length of {_max_field_chars()} chars"})
            # PLAN-METERING: /supersede IS a metered guarded write for the
            # monthly cap (unlike the ACTIVE-CLAIMS cap below, which never
            # applies to supersede -- a supersede is net-zero, replacing one
            # active claim with another, so it can't grow the active set;
            # the monthly write cap is a completely different axis -- it
            # counts requests, not active-claim growth, so a supersede still
            # consumes one). No project_limit check here: supersede's
            # `existing_id` must already be an active claim in (org_id,
            # project), so its project already exists -- there is no NEW
            # project for supersede to create.
            # Meter ONLY once the request is known to run the gate: a
            # supersede whose `existing_id` resolves to no active claim 404s
            # before the guard ever runs, so charging it would double-bill a
            # client typo (or an already-retracted id) -- the same "gate never
            # ran" exemption the 400/401 paths get. Pre-check existence BEFORE
            # the atomic cap increment; store.supersede()'s own in-lock
            # preflight stays the authoritative TOCTOU re-check (a claim
            # retracted in the tiny window between the two still 404s, now
            # without having consumed a unit in the common case).
            if not store.has_active_claim(body["org_id"], body["project"],
                                          body["existing_id"]):
                return self._send(404, {"error":
                    f"no active claim {body['existing_id']} in "
                    f"(org={body['org_id']}, project={body['project']})"})
            plan_lims = _plan_limits_for(principal)
            if plan_lims is not None:
                allowed, err_body = _enforce_monthly_write_cap(principal, plan_lims)
                if not allowed:
                    return self._send(403, err_body)
            elif principal.tenant_id is not None:
                # UNMETERED-COUNT (private-repo issue #215): see /remember above.
                _CONTROL.record_monthly_write(principal.tenant_id)
            try:
                r = store.supersede(body["org_id"], body["project"], body["who"],
                                    body["existing_id"], body["statement"],
                                    reason=body.get("reason"))
            except KeyError as e:  # no active claim with that existing_id in scope
                return self._send(404, {"error": e.args[0] if e.args else str(e)})
            except NormalizeUnavailable as e:  # see /remember's identical mapping;
                # supersede has no force_exception override, so this is unconditional.
                status, resp = _degraded_status(e)
                return self._send(status, resp)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            _CFG["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            # supersede runs the contradiction guard against all OTHER active
            # claims; if the revised claim would still conflict it returns a
            # Conflict (store.py) and nothing was written. Mirror /remember's
            # 200/409 mapping so a still-conflicting supersede surfaces as 409
            # (the MCP adapter already treats /supersede as _GUARDED_OK={200,409}).
            kind = "superseded" if isinstance(r, Stored) else "conflict"
            r.warnings = project_warnings + r.warnings
            return self._send(200 if isinstance(r, Stored) else 409,
                              {"result": kind, **_ser(r)})

        if u.path == "/retract":
            # Wrong-at-birth removal. Retract ≠ supersede: retract removes a
            # claim that should never have been believed (typo, test junk,
            # mis-scoped write); supersede replaces a decision that CHANGED and
            # keeps the why-chain. No contradiction gate — removal cannot
            # create a contradiction.
            for f in ("org_id", "who", "claim_id", "reason"):
                if not body.get(f):
                    return self._send(400, {"error": f"missing field: {f}"})
            # DEFAULT-PROJECT (private-repo issue #179): see /remember above.
            project_warnings: list = []
            body["project"] = _resolve_project(body.get("project"), principal,
                                               project_warnings).project
            oversized = _oversized_field(body, ("reason",))
            if oversized:
                return self._send(413, {"error": f"field '{oversized}' exceeds "
                                        f"max length of {_max_field_chars()} chars"})
            try:
                r = store.retract(body["org_id"], body["project"], body["who"],
                                  body["claim_id"], body["reason"])
            except KeyError as e:  # no active claim with that id in scope
                return self._send(404, {"error": e.args[0] if e.args else str(e)})
            except ValueError as e:  # blank reason -> client error, not 500
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": str(e)})
            _CFG["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            resp = {"result": "retracted", **_ser(r)}
            if project_warnings:
                resp["warnings"] = project_warnings
            return self._send(200, resp)

        if u.path == "/import":
            # This build has no per-tenant background-import queue: the legacy
            # principal (`tenant_id is None`, the only kind this build ever
            # produces) is always plan-gate-exempt and has no `tenants` row to
            # file a job against, so every import always runs synchronously.
            if not body.get("org_id"):
                return self._send(400, {"error": "missing field: org_id"})
            claims_in = body.get("claims")
            if not isinstance(claims_in, list):
                return self._send(400, {"error": "missing field: claims (must be a list)"})
            gate = _export_import_gate(principal, "import")
            if gate is not None:
                return self._send(403, gate)
            plan_lims = _plan_limits_for(principal)
            project_limit = None
            if plan_lims is not None:
                cap_err = _import_size_cap_error(plan_lims, len(claims_in))
                if cap_err is not None:
                    return self._send(413, cap_err)
                project_limit = plan_lims["project_limit"]
            org_id = body["org_id"]
            target_project = body.get("target_project")
            default_who = principal.who or body.get("who") or "import"


            # Legacy/superadmin/pooled -- unchanged synchronous behavior.
            max_active_claims = None
            try:
                result = _import_merge(store, org_id, claims_in, target_project,
                                       default_who, max_active_claims, project_limit)
            except NormalizeUnavailable as e:  # see /remember's identical mapping
                status, resp = _degraded_status(e)
                return self._send(status, resp)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            if result["imported"]:
                _CFG["last_write_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            return self._send(200, result)

        return self._send(404, {"error": "not found", "path": u.path})


def main():
    host = boot()
    port = int(os.environ.get("ERGO_HTTP_PORT", "8000"))
    # Bounded threaded server (finding #1): each admitted connection gets
    # its own thread so a slow guarded write
    # for one tenant cannot head-of-line-block another tenant's /health or
    # reads, but BoundedThreadingHTTPServer bounds admission itself (its own
    # per-instance semaphore, sized from ERGO_HTTP_MAX_WORKERS) BEFORE a
    # thread is even spawned, so a connection burst can't stampede the
    # LiteLLM proxy or spawn unbounded threads. daemon_threads=True
    # (ThreadingHTTPServer's default, set explicitly here for clarity) so
    # shutdown never hangs on an in-flight request.
    srv = BoundedThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"[ergo] listening on http://{host}:{port} "
                     f"(max_workers={_max_http_workers()})\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
