"""Ergo MCP server — a thin adapter over the Ergo HTTP API.

ARCHITECTURE INVARIANT: this module is a pure HTTP client. It talks to the
running engine at ``ERGO_API_URL`` with ``Authorization: Bearer
$ERGO_API_TOKEN``. It MUST NOT import ``ContradictionStore``, open ``ergo.db``,
or touch the DB directly. The HTTP API is the single source of truth; MCP is
just another transport on top of it.

One tool per HTTP endpoint, schemas mirroring ``docs/HTTP-API.md``:

    ergo_remember   POST /remember    (guarded write — surfaces 409 conflicts)
    ergo_learn      POST /learn       (guarded write + source provenance — can 409)
    ergo_ingest     POST /ingest      (fast doc path, skips the gate)
    ergo_supersede  POST /supersede   (change of mind — also gated, can 409)
    ergo_retract    POST /retract     (wrong-at-birth removal — reason required,
                                       no gate; 404 -> structured error)
    ergo_recall     GET  /recall      (hybrid semantic search)
    ergo_why        GET  /why         (reasoned belief + supersede history)
    ergo_active     GET  /active      (enumerate active claims)
    ergo_history    GET  /history     (full history incl. superseded + retracted)
    ergo_audit      GET  /audit         (private-repo issue #124: tenant's own writes + conflict_events)
    ergo_diagnose   GET  /diagnose      (ops view: counts, warnings)
    ergo_health     GET  /health        (liveness; no auth)
    ergo_export        GET  /export         (portable scope export, no vectors)
    ergo_import        POST /import         (contradiction-checked merge, always synchronous)

Two transports over the SAME 14 tools:

    python -m ergo.mcp.server                             # stdio (default, unchanged)
    python -m ergo.mcp.server --transport streamable-http  # hosted, /mcp

The streamable-HTTP app also serves an unauthenticated liveness route,
``GET /mcp-health`` (see ``_mcp_health`` / ``build_app()`` below) — ``/mcp``
itself correctly 406s a plain GET/HEAD, which is unmonitorable by a plain
HTTP-status monitor.

Scope defaults match the retired curl skill: org_id="claude-code",
who="claude-code". In stdio mode ``project`` defaults to the basename of the
REPOSITORY directory — resolved via the git COMMON dir, so a ``git worktree``
checkout maps to its parent repo's name rather than the worktree's own
directory name (see ``default_project()`` below) — falling back to
basename(cwd) if that's not a git repo at all. That cwd/git-basename
detection is meaningless on a hosted server, where a single process serves
every caller — so in streamable-HTTP mode, an omitted ``project`` is no
longer defaulted here at all (DEFAULT-PROJECT, private-repo issue #179): the adapter
just omits it from the outgoing request and lets the ENGINE resolve it
(the caller's own token
``default_project``, always `None` in this build,else the engine's own ``"default"`` fallback with a warning) — the engine
owns token metadata and this also fixes raw HTTP API callers, not just MCP.
Every default is overridable per call either way.

In streamable-HTTP mode each tool call authenticates against the Ergo API
using the INCOMING request's own ``Authorization: Bearer <key>`` header —
never the process-wide ``ERGO_API_TOKEN`` (a hosted process serves many
tenants, each with their own key). ``ERGO_API_TOKEN`` is only ever used as a
fallback in stdio mode, where there is no incoming HTTP request to read a
header from.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Config (env passthrough — never hardcode the token)
# --------------------------------------------------------------------------- #

DEFAULT_API_URL = "http://127.0.0.1:8788"
DEFAULT_ORG_ID = "claude-code"
DEFAULT_WHO = "claude-code"
# Short connect timeout so a black-holed host (firewall drop, not RST) fails
# fast; read stays generous for slow guarded writes (normalize + NLI + embed).
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)

# streamable-HTTP transport defaults. Loopback-only default host: edge
# routing/deploy (the reverse proxy that terminates api.ergomem.com/mcp) is
# out of scope here — this process just needs to be reachable BY that proxy.
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8790

# /mcp-health (this sidecar's own liveness route, streamable-HTTP mode only —
# see build_app()) probes the upstream engine's /health with a short,
# INDEPENDENT timeout, distinct from DEFAULT_TIMEOUT above (60s read, sized
# for slow guarded writes): a liveness probe must fail fast so a monitor sees
# red promptly instead of hanging on a black-holed engine.
MCP_HEALTH_PROBE_TIMEOUT = httpx.Timeout(3.0, connect=3.0)

# Loopback wildcards FastMCP itself auto-enables when host is 127.0.0.1 /
# localhost / ::1 (see mcp/server/fastmcp/server.py __init__ — the SAME
# condition build_app() below preserves for the no-flag default). Always
# kept alongside any --allowed-host so binding loopback-only stays usable
# even once a public hostname is added.
_LOOPBACK_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def api_url() -> str:
    """Base URL of the Ergo HTTP API."""
    return os.environ.get("ERGO_API_URL", DEFAULT_API_URL).rstrip("/")


def api_token() -> Optional[str]:
    """Bearer token. Read fresh from env each time so tests/rotation work."""
    return os.environ.get("ERGO_API_TOKEN") or None


def default_project(cwd: Optional[str] = None) -> str:
    """Project scope = basename of the REPOSITORY directory, resolved via the
    git COMMON dir (shared by every ``git worktree`` checkout of a repo) so a
    worktree maps to its parent repo's name, not its own directory name.
    Inside a worktree, ``--show-toplevel`` returns the WORKTREE's own path —
    every claim written from there used to land in a project named after the
    worktree, silently fragmenting memory that belongs together.

    Falls back to the old toplevel-basename behavior (correct for a plain
    checkout, wrong inside a worktree) if the git-common-dir probe fails for
    any reason — not a repo, or an installed git older than 2.31 that
    doesn't recognize ``--path-format`` (that flag is REQUIRED: without it,
    ``--git-common-dir`` may return a path relative to cwd, e.g. ``.git``,
    whose parent is ``.`` — a nonsense basename) — and finally to
    ``basename(cwd)`` if git is unavailable entirely. Never raises: this
    runs on every stdio tool call.
    """
    base = Path(cwd) if cwd else Path.cwd()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=5,
        )
        candidate = out.stdout.strip()
        if out.returncode == 0 and candidate and Path(candidate).is_absolute():
            return Path(candidate).parent.name
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return base.name


# --------------------------------------------------------------------------- #
# HTTP client (the only thing that touches the engine)
# --------------------------------------------------------------------------- #


# Sentinel distinguishing "caller didn't pass a token" (fall back to the
# pinned instance token, then env) from "caller resolved a definite token,
# which may legitimately be None" (send no Authorization header, full stop —
# see ErgoClient._headers). A bare `token: Optional[str] = None` default
# can't tell these apart; the tool layer's _resolve_token() (server.py) needs
# the second case for HTTP mode's missing-header path to NOT silently fall
# back to ERGO_API_TOKEN.
_UNSET: Any = object()


class ErgoError(RuntimeError):
    """A non-success, non-conflict response from the engine.

    Carries the status code and parsed/raw body so the tool layer can surface a
    structured error to the model instead of swallowing it.
    """

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"ergo HTTP {status_code}: {body}")


class ErgoClient:
    """Thin httpx wrapper. One method per endpoint. Returns parsed JSON.

    409 (conflict) is NOT an error here — it is a documented outcome of the
    guarded write paths, returned to the caller as a normal body so the tool
    layer can present the conflict set. Every other non-2xx raises ErgoError.
    """

    # Statuses each endpoint is allowed to return as a normal (non-error) body.
    _GUARDED_OK = (200, 409)
    _OK = (200,)
    # This build's /import always returns 200 -- synchronous, no plan
    # gate, no background job queue.
    _IMPORT_OK = (200, 202)

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
    ):
        self._base_url = (base_url or api_url()).rstrip("/")
        # token resolved lazily per-request via _headers() unless pinned here.
        self._token = token
        self._client = httpx.Client(
            base_url=self._base_url, transport=transport, timeout=timeout
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    # -- internals -------------------------------------------------------- #

    def _headers(self, auth: bool = True, token: Any = _UNSET) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if auth:
            if token is _UNSET:
                # No per-call token given: pinned instance token, else env —
                # the pre-HTTP-mode behavior, unchanged.
                resolved = self._token if self._token is not None else api_token()
            else:
                # Caller (the tool layer's _resolve_token) already made the
                # call — including "None", which means send NO Authorization
                # header rather than silently falling back to env/pinned.
                resolved = token
            if resolved:
                headers["Authorization"] = f"Bearer {resolved}"
        return headers

    def _parse(self, resp: httpx.Response, ok: tuple[int, ...]) -> dict[str, Any]:
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code in ok:
            return body if isinstance(body, dict) else {"data": body}
        raise ErgoError(resp.status_code, body)

    # -- endpoints -------------------------------------------------------- #

    def health(self, *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/health", headers=self._headers(auth=False))
        return self._parse(resp, self._OK)

    def remember(self, payload: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.post("/remember", headers=self._headers(token=token), json=payload)
        return self._parse(resp, self._GUARDED_OK)

    def learn(self, payload: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.post("/learn", headers=self._headers(token=token), json=payload)
        return self._parse(resp, self._GUARDED_OK)

    def ingest(self, payload: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.post("/ingest", headers=self._headers(token=token), json=payload)
        return self._parse(resp, self._OK)

    def supersede(self, payload: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.post("/supersede", headers=self._headers(token=token), json=payload)
        return self._parse(resp, self._GUARDED_OK)

    def retract(self, payload: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.post("/retract", headers=self._headers(token=token), json=payload)
        return self._parse(resp, self._OK)

    def recall(self, params: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/recall", headers=self._headers(token=token), params=params)
        return self._parse(resp, self._OK)

    def why(self, params: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/why", headers=self._headers(token=token), params=params)
        return self._parse(resp, self._OK)

    def active(self, params: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/active", headers=self._headers(token=token), params=params)
        return self._parse(resp, self._OK)

    def history(self, params: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/history", headers=self._headers(token=token), params=params)
        return self._parse(resp, self._OK)

    def audit(self, params: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/audit", headers=self._headers(token=token), params=params)
        return self._parse(resp, self._OK)

    def diagnose(self, *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/diagnose", headers=self._headers(token=token))
        return self._parse(resp, self._OK)

    def export(self, params: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        resp = self._client.get("/export", headers=self._headers(token=token), params=params)
        return self._parse(resp, self._OK)

    def import_claims(self, payload: dict[str, Any], *, token: Any = _UNSET) -> dict[str, Any]:
        # Named `import_claims`, not `import` -- `import` is a Python keyword.
        # `_call("import_claims", payload)` resolves it by attribute name.
        # This build's /import always returns 200 -- synchronous, no plan
        # gate, no background job queue.
        resp = self._client.post("/import", headers=self._headers(token=token), json=payload)
        return self._parse(resp, self._IMPORT_OK)


# Module-level client, lazily built from env. Tests swap it via set_client().
_CLIENT: Optional[ErgoClient] = None


def get_client() -> ErgoClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ErgoClient()
    return _CLIENT


def set_client(client: Optional[ErgoClient]) -> None:
    global _CLIENT
    _CLIENT = client


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #

_INSTRUCTIONS = (
    "Ergo decision-memory. A claim is scoped to (org_id, project). Defaults: "
    "org_id='claude-code', project=git-repo basename (worktree-aware) of cwd, who='claude-code'. "
    "Use ergo_remember for decisions with a reason (contradiction-guarded; can "
    "return a structured conflict you must resolve — cancel, ergo_supersede, or "
    "retry with force_exception). Use ergo_learn for a belief formed FROM a "
    "source (same gate + provenance). Use ergo_ingest for bulk reference docs. "
    "Use ergo_recall/ergo_why to look things up before changing settled choices."
)

# HOSTED-INSTRUCTIONS (private-repo issue #184): `_INSTRUCTIONS` above's cwd-basename claim
# is true for stdio only -- one process, one cwd. Over streamable-HTTP a
# single process serves every caller and there is no cwd to inspect; an
# omitted `project` is deliberately left unset here and resolved at the
# ENGINE instead (see the module docstring). Shipping the stdio wording to a
# hosted caller told every agent "the safe default is cwd-based" when the
# real, dangerous-if-unnoticed default is the token's own `default_project`
# or the literal project "default" -- used ONLY by build_app() below; stdio's
# `mcp` singleton keeps `_INSTRUCTIONS` unchanged.
_HOSTED_INSTRUCTIONS = (
    "Ergo decision-memory (hosted). A claim is scoped to (org_id, project). "
    "org_id and who come from your bearer token. `project` is optional and "
    "resolved by the engine in this order: your explicit `project` argument, "
    "else this token's own default_project, else the literal project "
    "'default' (with a warning) -- this server handles every caller, so "
    "always pass `project` explicitly whenever you mean a specific scope. "
    "Check `/recall` and `/why` responses' `scope` field to see which "
    "project was actually searched. Use ergo_remember for decisions with a "
    "reason (contradiction-guarded; can return a structured conflict you "
    "must resolve — cancel, ergo_supersede, or retry with force_exception). "
    "Use ergo_learn for a belief formed FROM a source (same gate + "
    "provenance). Use ergo_ingest for bulk reference docs. Use "
    "ergo_recall/ergo_why to look things up before changing settled choices."
)

mcp = FastMCP("ergo", instructions=_INSTRUCTIONS)


# --------------------------------------------------------------------------- #
# HTTP-mode request context: per-request auth passthrough + scope defaults.
#
# Both hinge on the SAME signal — is there an incoming Starlette request
# behind the tool call the SDK is currently dispatching? Verified against the
# installed 1.28.1 source: mcp/server/lowlevel/server.py sets a request-scoped
# contextvar for EVERY transport (including stdio, where the request is
# None), populated from mcp/server/streamable_http.py's
# ``ServerMessageMetadata(request_context=<starlette Request>)`` only for the
# streamable-HTTP transport. ``FastMCP.get_context()`` is what exposes it.
# --------------------------------------------------------------------------- #


def _incoming_request() -> Any:
    """The Starlette Request behind the current tool call, or None.

    None in stdio mode (no HTTP transport exists at all) and, defensively, if
    a tool is somehow invoked outside of any active MCP request (e.g. called
    directly, as the unit tests in test_server.py do).
    """
    ctx = mcp.get_context()
    try:
        request_context = ctx.request_context
    except ValueError:
        return None
    return getattr(request_context, "request", None)


def _in_http_mode() -> bool:
    """True when the current tool call arrived over streamable-HTTP."""
    return _incoming_request() is not None


def _bearer_from_request(request: Any) -> Optional[str]:
    """Parse ``Authorization: Bearer <token>`` off an incoming HTTP request."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _resolve_token() -> Any:
    """Token to pass through to the ErgoClient method for the current call.

    HTTP mode: ONLY the caller's own incoming Authorization header — never
    ERGO_API_TOKEN. One hosted process serves many tenants, each with their
    own key; falling back to the process env token would silently let every
    caller borrow whichever token happens to be baked into that container. A
    missing/malformed header resolves to a definite ``None``, meaning no
    Authorization header goes out on the downstream call — the engine's own
    401 is what surfaces to the model (via _call/_err below), never a crash
    and never a silent env fallback.

    Stdio mode (no incoming request): the ``_UNSET`` sentinel, NOT
    ``api_token()`` directly. This is deliberate and was a real regression
    (review round 2, private-repo PR #81) — ``_call()`` always passes ``token=...``
    explicitly, and ``ErgoClient._headers`` treats an explicit ``None`` as
    "definitely no auth" rather than "unspecified" (see the ``_UNSET``
    sentinel's own docstring above). Returning ``api_token()`` here directly
    would short-circuit past a pinned ``ErgoClient(token=...)`` instance and
    always use the env var instead in stdio mode. ``_UNSET`` tells
    ``_headers`` to apply its OWN precedence (pinned instance token, then
    env) exactly as it did before streamable-HTTP mode existed.
    """
    request = _incoming_request()
    if request is not None:
        return _bearer_from_request(request)
    return _UNSET


def _scope(org_id: Optional[str], project: Optional[str]) -> tuple[str, Optional[str]]:
    """Resolve (org_id, project) for a tool call.

    DEFAULT-PROJECT (private-repo issue #179): in HTTP (hosted) mode, an omitted
    `project` is no longer substituted with the literal string "default"
    here -- that hard-coded substitution ran BEFORE the engine ever saw the
    request, so a per-key `default_project`
(always `None` in this build)    could never
    take effect, and every hosted caller who forgot `project` silently burned
    a project slot with no warning (a Free-plan tenant's ENTIRE allowance is
    1 project). The engine now owns this resolution (server.py's
    `_resolve_project`: explicit `project` -> the caller's own token
    `default_project` -> the "default" fallback, WITH a warning) since it's
    the only place that knows the token's metadata AND covers raw HTTP API
    callers too, not just MCP. So HTTP mode just omits `project` from the
    outgoing payload/params when it wasn't given (`None` here signals that
    to every call site below) and lets the engine decide.

    Stdio mode is UNCHANGED: `default_project()`'s git-common-dir derivation
    is load-bearing (out of scope for private-repo issue #179) and stays the default.
    """
    if project is not None:
        proj: Optional[str] = project
    elif _in_http_mode():
        proj = None
    else:
        proj = default_project()
    return (org_id or DEFAULT_ORG_ID, proj)


def _put_project(payload: dict[str, Any], proj: Optional[str]) -> dict[str, Any]:
    """Include `project` in an outgoing payload/params dict only when
    `_scope` resolved a concrete value -- omitted entirely when `None`
    (hosted mode, caller didn't pass one) so the ENGINE decides (DEFAULT-
    PROJECT, private-repo issue #179) instead of this adapter substituting anything.
    Mutates and returns `payload` for a convenient call-site one-liner."""
    if proj is not None:
        payload["project"] = proj
    return payload


def _err(exc: ErgoError) -> dict[str, Any]:
    """Render an ErgoError as structured tool output (don't swallow it)."""
    return {"result": "error", "status_code": exc.status_code, "error": exc.body}


def _call(method: str, *args: Any) -> dict[str, Any]:
    """Invoke a client method, rendering non-2xx responses AND transport
    failures (connect/timeout/DNS/protocol errors) as the same structured
    ``{"result": "error", ...}`` shape every tool docstring promises — instead
    of letting httpx exceptions escape as raw strings through FastMCP.
    """
    client = get_client()
    token = _resolve_token()
    try:
        return getattr(client, method)(*args, token=token)
    except ErgoError as exc:
        return _err(exc)
    except httpx.HTTPError as exc:
        return {"result": "error", "error": str(exc), "url": client.base_url}


@mcp.tool()
def ergo_remember(
    statement: str,
    reason: Optional[str] = None,
    force_exception: Optional[str] = None,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    who: Optional[str] = None,
) -> dict[str, Any]:
    """Record a contradiction-guarded decision/constraint/convention.

    Always include a `reason` — reason-less claims can never win an ergo_why
    query. Be specific, no pronouns, one claim per call. If the engine detects a
    contradiction it returns a structured conflict (result="conflict", with the
    conflicting claim, tier, and reason) instead of storing — resolve it
    (cancel, ergo_supersede the old id, or retry with `force_exception`). Set
    `force_exception` to a justification string to store anyway despite a
    conflict (audit-loud).
    """
    org, proj = _scope(org_id, project)
    payload: dict[str, Any] = _put_project({
        "org_id": org,
        "who": who or DEFAULT_WHO,
        "statement": statement,
    }, proj)
    if reason is not None:
        payload["reason"] = reason
    if force_exception is not None:
        payload["force_exception"] = force_exception
    return _call("remember", payload)


@mcp.tool()
def ergo_learn(
    statement: str,
    source: str,
    reason: Optional[str] = None,
    force_exception: Optional[str] = None,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    who: Optional[str] = None,
) -> dict[str, Any]:
    """Record a belief learned FROM a source, contradiction-guarded with provenance.

    The bridge between ergo_ingest (raw reference chunks, no gate) and
    ergo_remember (decisions, full gate): use this when you read something and
    formed a belief — e.g. `statement="sessions are never reused across gateway
    restarts", source="sessions.py:847"`. Runs the SAME contradiction pipeline as
    ergo_remember, and folds `source` into the stored reason so the provenance is
    retrievable via ergo_why / ergo_recall. `source` is required (that is what
    makes this a learned claim). A contradiction comes back as structured
    `result="conflict"` (not an error) — resolve it by cancel, ergo_supersede, or
    retry with `force_exception`, exactly like ergo_remember.
    """
    org, proj = _scope(org_id, project)
    payload: dict[str, Any] = _put_project({
        "org_id": org,
        "who": who or DEFAULT_WHO,
        "statement": statement,
        "source": source,
    }, proj)
    if reason is not None:
        payload["reason"] = reason
    if force_exception is not None:
        payload["force_exception"] = force_exception
    return _call("learn", payload)


@mcp.tool()
def ergo_ingest(
    text: str,
    source: Optional[str] = None,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    who: Optional[str] = None,
) -> dict[str, Any]:
    """Bulk-ingest reference content (runbook/README/design doc).

    Skips the LLM normalizer + contradiction gate ("facts upsert freely"),
    chunks word-safely, embeds, and stores for later recall. Use this for
    documents you want recall-able — NOT for decisions you want
    contradiction-checked (use ergo_remember for those). `source` is a
    provenance string appended to each chunk as `[source: ...]`.
    """
    org, proj = _scope(org_id, project)
    payload: dict[str, Any] = _put_project({
        "org_id": org,
        "who": who or DEFAULT_WHO,
        "text": text,
    }, proj)
    if source is not None:
        payload["source"] = source
    return _call("ingest", payload)


@mcp.tool()
def ergo_supersede(
    existing_id: str,
    statement: str,
    reason: Optional[str] = None,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    who: Optional[str] = None,
) -> dict[str, Any]:
    """Replace an existing active claim with a revised one ("I changed my mind").

    Get `existing_id` from ergo_recall / ergo_why / ergo_active. The new claim
    is contradiction-checked against all OTHER active claims first; if it would
    itself conflict, the supersede is refused with a structured conflict set.
    The old row becomes status='superseded' and the new one links back to it —
    this is what ergo_why walks for history.
    """
    org, proj = _scope(org_id, project)
    payload: dict[str, Any] = _put_project({
        "org_id": org,
        "who": who or DEFAULT_WHO,
        "existing_id": existing_id,
        "statement": statement,
    }, proj)
    if reason is not None:
        payload["reason"] = reason
    return _call("supersede", payload)


@mcp.tool()
def ergo_retract(
    claim_id: str,
    reason: str,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    who: Optional[str] = None,
) -> dict[str, Any]:
    """Retract a wrong-at-birth claim — remove it from the active belief set.

    Retract vs supersede: use ergo_retract when the claim should NEVER have
    been stored (typo, test junk, mis-scoped write); use ergo_supersede when a
    decision genuinely CHANGED (that keeps the why-chain). `reason` is
    required — removal is audit-loud. Get `claim_id` from ergo_recall /
    ergo_active / ergo_history. The claim vanishes from ergo_active /
    ergo_recall / ergo_why and is no longer contradiction-checked against, but
    stays in ergo_history with status='retracted'. A missing or
    already-retracted claim_id comes back as structured output
    (result="error", status_code=404), not an exception.
    """
    org, proj = _scope(org_id, project)
    payload: dict[str, Any] = _put_project({
        "org_id": org,
        "who": who or DEFAULT_WHO,
        "claim_id": claim_id,
        "reason": reason,
    }, proj)
    return _call("retract", payload)


@mcp.tool()
def ergo_recall(
    query: str,
    limit: int = 10,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Hybrid semantic search over active claims in a scope.

    Returns results scored `0.7*vec_sim + 0.3*keyword_overlap`. Read `score`,
    `vec_sim`, and `kw` independently — a high `vec_sim` with `kw=0` is a
    semantic-only match; verify before treating it as authoritative. `limit` is
    clamped to [1, 50] by the engine.
    """
    org, proj = _scope(org_id, project)
    params = _put_project({"org_id": org, "q": query, "limit": limit}, proj)
    return _call("recall", params)


@mcp.tool()
def ergo_why(
    query: str,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Best REASONED belief for a query, plus its supersede history.

    Like ergo_recall but reasoned-only (a reason-less fact can never win),
    returns the single top hit and walks the supersede chain. Always includes
    `score` when a claim is returned, so you can judge confidence yourself.

    Returns `{"why": null}` in TWO distinct cases — check for `suppressed`
    to tell them apart:
    - No reasoned claim exists in scope at all (no `suppressed` key).
    - A claim exists but scored below the relevance floor: `{"why": null,
      "suppressed": {"reason": "below_relevance_floor", "score": ..., "floor": ...}}`.
      This means the query didn't land on anything the store actually knows —
      it is NOT a licence to fabricate a rationale, and it is NOT the same as
      "no decision is on file". Ask a more specific question or check
      ergo_recall yourself instead of guessing.
    """
    org, proj = _scope(org_id, project)
    params = _put_project({"org_id": org, "q": query}, proj)
    return _call("why", params)


@mcp.tool()
def ergo_active(
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    projection: Optional[str] = None,
) -> dict[str, Any]:
    """Enumerate active claims in a scope, capped by default (private-repo issue #185).

    Returns `{"state": "project_missing"|"empty"|"has_active_claims", "count":
    <total in scope>, "returned": <this page>, "truncated": bool,
    "truncated_reason": "row_limit"|"byte_budget"|null, "next_offset": <int or
    null>, "claims": [...]}`. `state` describes the SCOPE, not the page --
    claims beyond your `offset` still read `has_active_claims`, never `empty`.
    If `truncated` is true, call again with `offset=next_offset` to page
    through the rest; `truncated_reason` tells you whether raising `limit`
    would help ("row_limit") or won't ("byte_budget" -- the page was already
    cut short of `limit` by a ~50 KB served-JSON cap, private-repo issue #206). `limit` is
    omitted by default so the engine's own default/max apply (never
    hardcoded here, same as ergo_audit). A typo in the scope reads as
    `project_missing` — looks empty but isn't; double-check org_id/project
    spelling.

    `projection="summary"` (private-repo issue #206, opt-in — default is `"full"`, every
    column unchanged since #185) drops each claim to just `{"id",
    "statement"}`, omitting `reason` entirely. Prefer this when YOU are the
    caller with the context ceiling: browsing "what does this project
    believe?" over a verbose corpus at the default full page can cost
    ~200 KB (MCP JSON-stringifies the tool result inside its own envelope,
    roughly doubling every escaped quote in every `reason` on the wire) —
    summary mode cuts that to a fraction, and you can `ergo_recall`/`ergo_why`
    the one or two claims you actually need the reason for by id.
    """
    org, proj = _scope(org_id, project)
    params: dict[str, Any] = _put_project({"org_id": org}, proj)
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    if projection is not None:
        params["projection"] = projection
    return _call("active", params)


@mcp.tool()
def ergo_history(
    org_id: Optional[str] = None,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Full claim history for a scope — active + superseded + retracted.

    Ordered by claim_seq ascending. Use ergo_active for just the live set.
    """
    org, proj = _scope(org_id, project)
    params = _put_project({"org_id": org}, proj)
    return _call("history", params)


@mcp.tool()
def ergo_audit(
    limit: Optional[int] = None,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Tenant-facing audit trail: this scope's own `writes` + `conflict_events`
    rows (private-repo issue #124).

    Self-serve read for a compliance/security need — the caller only ever sees
    its OWN org's rows (the engine's `forced_org` always wins over any
    org_id passed here, same isolation guarantee as ergo_active/ergo_history).
    Returns `{"writes": [...], "conflict_events": [...]}`, each ordered oldest
    -> newest. `limit` caps EACH stream independently (not their sum);
    omitted, the engine applies its own default (200); passed, the engine
    still clamps it to `[1, 1000]` -- this adapter deliberately does NOT
    hardcode either number so it can never drift out of sync with the
    engine's own DEFAULT_AUDIT_LIMIT/MAX_AUDIT_LIMIT.
    """
    org, proj = _scope(org_id, project)
    params: dict[str, Any] = _put_project({"org_id": org}, proj)
    if limit is not None:
        params["limit"] = limit
    return _call("audit", params)


@mcp.tool()
def ergo_diagnose() -> dict[str, Any]:
    """Ops view of the running engine: counts, conflict telemetry, warnings.

    Report-only — it never mutates beliefs. `warnings[]` is the anti-zombie
    field (missing owner/backup, version drift). Any fix still requires an
    explicit ergo_supersede.
    """
    return _call("diagnose")


@mcp.tool()
def ergo_health() -> dict[str, Any]:
    """Liveness probe (no auth). Call once before relying on Ergo.

    `status: ok` with `retrieval: true` means the whole API (incl. recall/why/
    ingest) is available.
    """
    return _call("health")


@mcp.tool()
def ergo_export(
    project: Optional[str] = None,
    org_id: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> dict[str, Any]:
    """Export the scope's active claims as a portable document (no vectors).

    Open to every deployment — data portability is never gated, and
    reads never consume any write allowance.
    Returns `{ergo_export_version, org_id, exported_at, embedder:{provider,
    dim}, claim_count, returned, truncated, next_offset, as_of_seq,
    claims:[...]}` (ergo_export_version stays 2 — the new pagination fields
    are purely additive, they don't change how `claims[]` itself is parsed
    or imported). Each `claims[]` entry is one ACTIVE claim ("head"), never
    the embedding vectors themselves (embedder/dimension-specific, always
    recomputed on import), with its supersede history riding along as a
    nested, oldest-first `revisions[]` (omitted entirely for a head that
    was never revised) — retracted claims are excluded entirely, never
    even inside another head's `revisions`. Scoped to `project` like
    ergo_recall/ergo_active; pass no `project` override to leave the
    default scope narrowing in place.

    PAGINATION (private-repo issue #203): `limit`/`offset` are omitted by default, which
    keeps the pre-existing UNBOUNDED behavior — the full scope in one
    call, same as before this issue, so an existing caller never silently
    gets a truncated file it believes is complete. This is the OPPOSITE
    default from ergo_active (which always caps): export is a backup/
    migration source of truth, not a casual browse. For a large corpus
    over MCP, opt into paging by passing `limit` — e.g. `limit=25` is a
    reasonable starting point (an export head can carry a nested
    `revisions[]` chain, so it's heavier than an ergo_active row, and MCP
    double-encodes the tool result on the wire — every quote in every
    `reason` gets escaped — so a page costs roughly TWICE its raw JSON
    size). `truncated`/`next_offset` describe the SAME loud-truncation
    contract as ergo_active: keep calling with `offset=next_offset` until
    `truncated` is false, and `claim_count` is always the scope TOTAL
    regardless of which page you're holding.

    SNAPSHOT CONSISTENCY: `as_of_seq` (`{project: max_claim_seq}`) is a
    marker for multi-page walks — an export is meant to be a faithful
    snapshot, and pages fetched across concurrent writes could otherwise
    compose a file that never corresponded to any single instant of the
    store. Compare `as_of_seq` across the pages you fetch: if it's
    identical on every page, no claim was created or superseded in this
    scope mid-walk. If it changed, the store was written to while you were
    paging — re-walk from `offset=0` before treating the composed file as
    a clean snapshot for ergo_import. (Narrow known gap: a bare
    ergo_retract mid-walk, with no accompanying insert/supersede, doesn't
    move this marker — retraction is rare and administrative, not the
    everyday write this catches.)

    Pairs with ergo_import — feed the returned `claims` list straight into
    it (whether from one unbounded call or glued together across pages).
    """
    org, proj = _scope(org_id, project)
    params: dict[str, Any] = _put_project({"org_id": org}, proj)
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return _call("export", params)


@mcp.tool()
def ergo_import(
    claims: list,
    target_project: Optional[str] = None,
    org_id: Optional[str] = None,
    project: Optional[str] = None,
    who: Optional[str] = None,
) -> dict[str, Any]:
    """Contradiction-checked MERGE of claims (e.g. from ergo_export) into a scope.

    `claims` is the `claims` array from an ergo_export result (or any list of
    claim-shaped dicts). Each incoming claim runs through the SAME guarded
    write path ergo_remember/ergo_learn use — never force-written: an
    exact-text duplicate is skipped (`skipped_duplicate`), a blocking
    contradiction is reported in `conflicts[]` (the "import audit") and left
    unstored, everything else is stored. A claim's `revisions[]` (its nested
    why-chain from ergo_export, private-repo issue #189), if present, is FOLDED into its
    stored `reason` before that write — bounded to the field-size cap, with
    truncation announced inline if the chain doesn't fit — rather than
    silently dropped; a claim with no `revisions` key is unaffected.

    No plan gate and no active-claim cap in this build -- every claim in
    the list is always attempted, and the merge always completes
    synchronously.
    `target_project`, if given, remaps every claim into
    that one project, overriding each claim's own `project`.
    """
    # /import has no top-level `project` field of its own (only the optional
    # `target_project` remap below) -- `project` only feeds org resolution
    # here, mirroring the other scoped tools' `_scope()` call for consistency.
    org, _proj = _scope(org_id, project)
    payload: dict[str, Any] = {"org_id": org, "claims": claims}
    if target_project is not None:
        payload["target_project"] = target_project
    if who is not None:
        payload["who"] = who
    return _call("import_claims", payload)


# All 14 tool functions, in the order documented in the module docstring.
# `mcp.tool()` returns its argument unchanged (verified against the installed
# 1.28.1 source), so these are still the plain functions the direct-call unit
# tests in test_server.py exercise — this tuple just lets build_app() below
# register the SAME implementations on a second, independent FastMCP app.
_TOOL_FUNCS: tuple[Any, ...] = (
    ergo_remember,
    ergo_learn,
    ergo_ingest,
    ergo_supersede,
    ergo_retract,
    ergo_recall,
    ergo_why,
    ergo_active,
    ergo_history,
    ergo_audit,
    ergo_diagnose,
    ergo_health,
    ergo_export,
    ergo_import,
)


async def _mcp_health(request: Request) -> JSONResponse:
    """Liveness route for THIS sidecar process: ``GET /mcp-health``.

    Registered only on the streamable-HTTP app (see ``build_app()`` below) —
    the sidecar's other route, ``/mcp``, correctly 406s a plain GET/HEAD (it
    expects the MCP protocol), which makes it unmonitorable by a plain
    HTTP-status monitor (e.g. UptimeRobot). This route is a Starlette
    ``Route``, which auto-serves HEAD for a GET-only route — exactly what a
    HEAD-only monitor needs, with no separate HEAD branch to write.

    Unauthenticated, same as the upstream engine's own ``/health`` — it's a
    liveness probe, not a data route. Probes ``GET {ERGO_API_URL}/health``
    with a short, independent timeout (``MCP_HEALTH_PROBE_TIMEOUT``) so a
    black-holed engine fails the check fast instead of hanging the monitor's
    request. An upstream 200 is reported as 200 ``{"status": "ok"}``;
    anything else (non-200, unreachable, timeout) is reported as 503
    ``{"status": "degraded", "upstream": "<brief reason, no secrets>"}`` —
    never a raw 500 traceback.
    """
    try:
        async with httpx.AsyncClient(timeout=MCP_HEALTH_PROBE_TIMEOUT) as client:
            resp = await client.get(f"{api_url()}/health")
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"status": "degraded", "upstream": f"{type(exc).__name__}"},
            status_code=503,
        )
    except Exception as exc:  # defensive: never let this route 500 a monitor
        return JSONResponse(
            {"status": "degraded", "upstream": f"unexpected {type(exc).__name__}"},
            status_code=503,
        )
    if resp.status_code == 200:
        return JSONResponse({"status": "ok"})
    return JSONResponse(
        {"status": "degraded", "upstream": f"HTTP {resp.status_code}"},
        status_code=503,
    )


def build_app(
    *,
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    stateless_http: bool = True,
    allowed_hosts: tuple[str, ...] = (),
) -> FastMCP:
    """Build a FRESH streamable-HTTP FastMCP app with the same 14 tools.

    A new instance per call — rather than mutating the stdio ``mcp`` singleton
    above — because the SDK's StreamableHTTPSessionManager (created lazily the
    first time ``streamable_http_app()`` runs) may only be ``.run()`` once per
    instance; a shared singleton couldn't be re-exercised across test
    functions, and mutating the process-wide stdio server for a one-off HTTP
    run isn't desirable either. ``stateless_http=True`` by default — no
    session affinity, per the hosted-endpoint decision.

    ``allowed_hosts``: FastMCP auto-enables DNS-rebinding protection whenever
    ``host`` is a loopback address (127.0.0.1 / localhost / ::1 — see the
    installed SDK's ``FastMCP.__init__``), restricting the incoming ``Host``
    header to that SAME loopback value. That is exactly the bind address this
    process uses behind a reverse proxy / tunnel (e.g. Cloudflare) — but the
    proxy forwards the ORIGINAL public Host header (e.g. ``api.ergomem.com``)
    unchanged, which the loopback-only allowlist then rejects with a 421. Pass
    the public hostname(s) this process is reachable at through
    ``allowed_hosts`` to admit them too; empty (the default) leaves the SDK's
    own loopback-only behavior completely unchanged. Hosts are used verbatim
    (no wildcard applied) — include a ``:*`` suffix yourself if the proxy
    forwards a port in the Host header.
    """
    transport_security = None
    if allowed_hosts:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*allowed_hosts, *_LOOPBACK_ALLOWED_HOSTS],
        )
    app = FastMCP(
        "ergo",
        instructions=_HOSTED_INSTRUCTIONS,
        host=host,
        port=port,
        stateless_http=stateless_http,
        transport_security=transport_security,
    )
    app.custom_route("/mcp-health", methods=["GET"])(_mcp_health)
    for fn in _TOOL_FUNCS:
        app.add_tool(fn)
    return app


def main() -> None:
    """Entry point. Default transport is stdio (existing launchers, e.g. the
    Claude Code / Codex / opencode MCP configs, keep working unchanged); pass
    ``--transport streamable-http`` to serve the same 14 tools over HTTP at
    ``http://<host>:<port>/mcp`` instead.
    """
    parser = argparse.ArgumentParser(prog="python -m ergo.mcp.server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
        help="stdio (default) or streamable-http",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HTTP_HOST, help="streamable-http bind host"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_HTTP_PORT, help="streamable-http bind port"
    )
    parser.add_argument(
        "--allowed-host",
        dest="allowed_hosts",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "public Host header this process is reachable at behind a "
            "reverse proxy/tunnel (repeatable). Without this, FastMCP's "
            "loopback-only DNS-rebinding protection rejects any Host other "
            "than the --host value with a 421 -- see build_app()."
        ),
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
        return

    build_app(
        host=args.host, port=args.port, allowed_hosts=tuple(args.allowed_hosts)
    ).run(transport="streamable-http")


if __name__ == "__main__":
    main()
