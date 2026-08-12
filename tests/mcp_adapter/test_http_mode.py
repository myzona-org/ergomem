"""Streamable-HTTP transport — full ASGI protocol round trip, no sockets.

Unlike test_server.py (direct function calls against a mocked ErgoClient —
there is no incoming HTTP request in that path, so it exercises the stdio
fallback behavior), these tests drive the ACTUAL MCP protocol (initialize +
tools/call) against ``server.build_app()``'s streamable-HTTP ASGI app via
``httpx.ASGITransport``. That is what actually populates an incoming
Starlette request behind the tool call, which is the only way to exercise
per-request auth passthrough and the hosted-mode scope default.

Each test builds a FRESH app (``server.build_app()``) and enters its
``session_manager.run()`` itself — the SDK's StreamableHTTPSessionManager may
only be ``.run()`` once per instance, and there's no lifespan handshake to
drive it automatically here since httpx.ASGITransport doesn't send ASGI
lifespan events (verified against the installed httpx 0.28.1 / mcp 1.28.1
sources).

Async test bodies run via ``anyio.run()`` directly from plain ``def`` test
functions — no pytest-asyncio / anyio pytest plugin needed. ``anyio`` is
already a transitive dependency of ``mcp`` and ``httpx`` (see their own
metadata), so this adds nothing beyond the existing ``[mcp]`` extra.
"""

from __future__ import annotations

import socket
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, AsyncIterator, Optional

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ergo.mcp import server

# Loopback + an explicit port so the SDK's DNS-rebinding-protection allowlist
# (built for host="127.0.0.1" — see build_app()'s default) actually matches
# the Host header ASGITransport sends; a bare "127.0.0.1" with no port, or a
# "testserver"-style host, both get rejected 421 by that middleware.
_BASE_URL = "http://127.0.0.1:8790"


class Recorder:
    """Records every request the mocked Ergo API transport receives."""

    def __init__(self, response: httpx.Response | None = None):
        self._response = response or httpx.Response(
            200, json={"result": "stored", "claim_id": "x"}
        )
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


@asynccontextmanager
async def _running_app(app: Any) -> AsyncIterator[Any]:
    """Enter `app`'s streamable-HTTP ASGI app + session manager ONCE, and
    yield the ASGI app ready for one or more (including CONCURRENT) client
    sessions.

    Split out from `_http_session` below so a single running app can back
    multiple simultaneous `_client_session` calls in the same task group —
    `StreamableHTTPSessionManager.run()` may only be entered once per
    instance, so each concurrent session must reuse this same context rather
    than each trying to enter its own.
    """
    asgi_app = app.streamable_http_app()
    async with app.session_manager.run():
        yield asgi_app


@asynccontextmanager
async def _client_session(
    app: Any,
    asgi_app: Any,
    headers: Optional[dict[str, str]] = None,
    base_url: str = _BASE_URL,
) -> AsyncIterator[ClientSession]:
    """One real MCP client session against an ALREADY-RUNNING `asgi_app`
    (see `_running_app`).

    ``base_url``'s host is what ends up in the request's ``Host`` header
    (httpx derives it from the URL, exactly like a real client would) — this
    is how the allowed_hosts tests below simulate requests arriving through
    a reverse proxy/tunnel with a public hostname, entirely in-process (no
    sockets, no real DNS).
    """
    transport = httpx.ASGITransport(app=asgi_app)
    url = f"{base_url}{app.settings.streamable_http_path}"
    async with httpx.AsyncClient(
        transport=transport, base_url=base_url, headers=headers
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (
            read,
            write,
            _get_session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def _http_session(
    app: Any,
    headers: Optional[dict[str, str]] = None,
    base_url: str = _BASE_URL,
) -> AsyncIterator[ClientSession]:
    """Drive one real MCP client session against `app` over ASGI transport.

    Convenience wrapper combining `_running_app` + `_client_session` for the
    common single-session case; every test below except the concurrency ones
    only ever needs one session per app.
    """
    async with _running_app(app) as asgi_app:
        async with _client_session(
            app, asgi_app, headers=headers, base_url=base_url
        ) as session:
            yield session


def run_async(coro_fn, *args, **kwargs):
    """Run an async test body without needing a pytest asyncio plugin."""
    return anyio.run(lambda: coro_fn(*args, **kwargs))


@pytest.fixture
def wire(monkeypatch):
    """Install a mocked ErgoClient shared by the module-level singleton.

    Mirrors test_server.py's `wire` fixture — the tool functions always call
    ``get_client()``, regardless of which FastMCP app instance is actually
    dispatching the request.
    """
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)

    def install(response: httpx.Response | None = None) -> Recorder:
        rec = Recorder(response)
        transport = httpx.MockTransport(rec.handler)
        client = server.ErgoClient(
            base_url="http://ergo.test", token=None, transport=transport
        )
        server.set_client(client)
        return rec

    yield install
    server.set_client(None)


# --------------------------------------------------------------------------- #
# (a) http-mode app boots and lists every registered tool
# --------------------------------------------------------------------------- #


def test_http_app_boots_and_lists_every_tool():
    """The streamable-HTTP transport must list exactly the tools this
    adapter registers -- derived from `_TOOL_FUNCS` itself (this repo's own
    tool count, whatever it is here -- the public export strips
    `ergo_import_status`, so the two trees don't necessarily agree) rather
    than a hardcoded literal set, so this test doesn't need updating every
    time the tool count changes for an unrelated reason."""
    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(app) as session:
            result = await session.list_tools()
            return {t.name for t in result.tools}

    names = run_async(body)
    assert names == {f.__name__ for f in server._TOOL_FUNCS}


def test_build_app_is_stateless_by_default():
    app = server.build_app()
    assert app.settings.stateless_http is True


# --------------------------------------------------------------------------- #
# (a2) hosted `initialize` instructions must not lie about cwd (private-repo issue #184)
# --------------------------------------------------------------------------- #


def test_build_app_instructions_do_not_mention_cwd_and_state_hosted_order():
    # THE BUG (private-repo issue #184): before this fix, build_app() shipped the SAME
    # `_INSTRUCTIONS` stdio uses, which claims `project` defaults to a
    # "git-repo basename (worktree-aware) of cwd" -- false over
    # streamable-HTTP, where one process serves every caller and there is no
    # cwd. This asserts the hosted app's instructions never make that claim,
    # and instead describe the real engine resolution order.
    app = server.build_app()
    assert "cwd" not in app.instructions
    assert "default_project" in app.instructions
    assert "'default'" in app.instructions or '"default"' in app.instructions


def test_stdio_instructions_still_describe_the_cwd_default():
    # The stdio singleton must be UNCHANGED -- stdio's cwd-basename default
    # is real, and callers still need to be told about it.
    assert "cwd" in server.mcp.instructions
    assert "git-repo basename" in server.mcp.instructions


# --------------------------------------------------------------------------- #
# (b) auth passthrough: incoming header wins, env token NOT used in http mode
# --------------------------------------------------------------------------- #


def test_http_tool_call_uses_incoming_bearer_not_env_token(wire, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "env-token-must-not-be-used")
    rec = wire()

    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(
            app, headers={"Authorization": "Bearer caller-token"}
        ) as session:
            return await session.call_tool(
                "ergo_remember", {"statement": "s", "reason": "r"}
            )

    result = run_async(body)
    assert result.structuredContent == {"result": "stored", "claim_id": "x"}
    assert rec.last.headers["authorization"] == "Bearer caller-token"


def test_concurrent_http_tool_calls_do_not_leak_tokens_across_requests(wire):
    """Regression (review finding, round 2, private-repo PR #81): pin per-request token
    isolation under REAL concurrency, not just sequential calls.

    Auth passthrough resolves the token from a contextvar-backed request
    context (``_incoming_request`` / ``_resolve_token`` in server.py) rather
    than an argument threaded explicitly through every call — the sequential
    test above can't tell that apart from a shared/global lookup that would
    leak between overlapping requests in a real multi-tenant deployment. Two
    tool calls are launched SIMULTANEOUSLY (an anyio task group, both against
    the same running app/session-manager — real interleaving, not two
    isolated event loops) with distinct bearer tokens; each downstream
    request recorded by the mocked Ergo API must carry its OWN caller's
    token, correlated by each call's distinct `statement` payload since
    ordering across the two concurrent calls isn't guaranteed.
    """
    rec = wire()

    async def one_call(app: Any, asgi_app: Any, token: str, statement: str) -> None:
        async with _client_session(
            app, asgi_app, headers={"Authorization": f"Bearer {token}"}
        ) as session:
            await session.call_tool(
                "ergo_remember", {"statement": statement, "reason": "r"}
            )

    async def body():
        app = server.build_app(stateless_http=True)
        async with _running_app(app) as asgi_app:
            async with anyio.create_task_group() as tg:
                tg.start_soon(one_call, app, asgi_app, "token-a", "statement-a")
                tg.start_soon(one_call, app, asgi_app, "token-b", "statement-b")

    run_async(body)

    import json

    token_by_statement = {
        json.loads(req.content)["statement"]: req.headers.get("authorization")
        for req in rec.requests
    }
    assert token_by_statement == {
        "statement-a": "Bearer token-a",
        "statement-b": "Bearer token-b",
    }


def test_http_tool_call_scope_omits_project_when_not_given_letting_engine_decide(wire):
    """DEFAULT-PROJECT (private-repo issue #179): hosted-mode scope default no longer
    hard-codes project="default" in the adapter -- an omitted `project` is
    left OUT of the outgoing payload entirely (never the git basename
    either), so the ENGINE resolves it (the caller's own token's
    default_project, else its own "default" fallback with a warning)."""
    rec = wire()

    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(
            app, headers={"Authorization": "Bearer caller-token"}
        ) as session:
            return await session.call_tool(
                "ergo_remember", {"statement": "s", "reason": "r"}
            )

    run_async(body)
    import json

    body_json = json.loads(rec.last.content)
    assert "project" not in body_json
    assert body_json["org_id"] == "claude-code"


def test_http_tool_call_project_override_still_wins(wire):
    rec = wire()

    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(
            app, headers={"Authorization": "Bearer caller-token"}
        ) as session:
            return await session.call_tool(
                "ergo_remember",
                {"statement": "s", "reason": "r", "project": "acme-platform"},
            )

    run_async(body)
    import json

    assert json.loads(rec.last.content)["project"] == "acme-platform"


def test_http_tool_call_get_route_also_omits_project_when_not_given(wire):
    """The same DEFAULT-PROJECT omission applies to GET-style tools
    (ergo_active), not just POST ones -- `_scope` is the single choke point
    all 14 tools route through."""
    rec = wire(httpx.Response(200, json={"state": "empty", "claims": []}))

    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(
            app, headers={"Authorization": "Bearer caller-token"}
        ) as session:
            return await session.call_tool("ergo_active", {})

    run_async(body)
    assert "project" not in rec.last.url.params
    assert rec.last.url.params["org_id"] == "claude-code"


# --------------------------------------------------------------------------- #
# (c) missing header -> structured 401, never a crash, never an env fallback
# --------------------------------------------------------------------------- #


def test_http_tool_call_missing_header_surfaces_401_structured(wire, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "env-token-must-not-be-used")
    rec = wire(httpx.Response(401, json={"error": "unauthorized"}))

    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(app, headers=None) as session:
            return await session.call_tool(
                "ergo_remember", {"statement": "s", "reason": "r"}
            )

    result = run_async(body)
    assert result.structuredContent == {
        "result": "error",
        "status_code": 401,
        "error": {"error": "unauthorized"},
    }
    # No Authorization header sent at all -- env token must NOT have been
    # used as a fallback for a caller who simply forgot theirs.
    assert "authorization" not in rec.last.headers


def test_http_tool_call_malformed_header_treated_as_missing(wire, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "env-token-must-not-be-used")
    rec = wire(httpx.Response(401, json={"error": "unauthorized"}))

    async def body():
        app = server.build_app(stateless_http=True)
        async with _http_session(
            app, headers={"Authorization": "Basic not-a-bearer-token"}
        ) as session:
            return await session.call_tool(
                "ergo_remember", {"statement": "s", "reason": "r"}
            )

    result = run_async(body)
    assert result.structuredContent["result"] == "error"
    assert "authorization" not in rec.last.headers


# --------------------------------------------------------------------------- #
# (d) stdio default behavior unchanged (env token still used)
# --------------------------------------------------------------------------- #


def test_stdio_direct_call_still_uses_env_token(wire, monkeypatch):
    """A direct function call (test_server.py's whole pattern, and how the
    stdio transport ultimately invokes tools too) has no incoming HTTP
    request, so it must keep falling back to ERGO_API_TOKEN exactly as
    before this feature existed.
    """
    monkeypatch.setenv("ERGO_API_TOKEN", "stdio-env-token")
    rec = wire()

    server.ergo_remember(statement="s", reason="r")

    assert rec.last.headers["authorization"] == "Bearer stdio-env-token"


def test_stdio_direct_call_scope_defaults_still_use_git_cwd_basename(
    wire, monkeypatch
):
    monkeypatch.setattr(server, "default_project", lambda cwd=None: "unit-proj")
    rec = wire()

    server.ergo_remember(statement="s", reason="r")

    import json

    assert json.loads(rec.last.content)["project"] == "unit-proj"


# --------------------------------------------------------------------------- #
# --allowed-host / DNS-rebinding protection (production bug fix)
#
# FastMCP auto-enables DNS-rebinding protection restricted to loopback
# whenever host="127.0.0.1" (our own bind default — see the installed SDK's
# FastMCP.__init__), which rejects any OTHER Host header with 421. That's a
# real problem behind a reverse proxy/tunnel (e.g. Cloudflare) that forwards
# the original public Host (api.ergomem.com) unchanged to this loopback
# process. build_app(allowed_hosts=...) is the fix; these tests hit the ASGI
# app with a raw POST (rather than the full ClientSession handshake used
# above) so the 421 is a direct, unambiguous assertion instead of buried in
# a wrapped ExceptionGroup.
# --------------------------------------------------------------------------- #

_INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


async def _raw_initialize(app: Any, base_url: str) -> httpx.Response:
    """POST a bare `initialize` directly at `app`'s ASGI transport.

    `base_url`'s host becomes the request's Host header, exactly as it would
    behind a real reverse proxy — this is what the DNS-rebinding-protection
    middleware validates.
    """
    asgi_app = app.streamable_http_app()
    async with app.session_manager.run():
        transport = httpx.ASGITransport(app=asgi_app)
        url = f"{base_url}{app.settings.streamable_http_path}"
        async with httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            headers={"Accept": "application/json, text/event-stream"},
        ) as http_client:
            return await http_client.post(url, json=_INITIALIZE_BODY)


def test_default_app_rejects_a_public_host_header_with_421():
    """Reproduces the production bug: no --allowed-host means the SDK's own
    loopback-only allowlist applies, so a request whose Host is the public
    tunnel hostname (not 127.0.0.1) is rejected -- even though it's exactly
    what a Cloudflare tunnel forwards for a legitimate request.
    """

    async def body():
        app = server.build_app(stateless_http=True)
        return await _raw_initialize(app, "http://api.ergomem.com")

    response = run_async(body)
    assert response.status_code == 421


def test_allowed_hosts_admits_the_configured_public_host():
    async def body():
        app = server.build_app(
            stateless_http=True, allowed_hosts=("api.ergomem.com",)
        )
        return await _raw_initialize(app, "http://api.ergomem.com")

    response = run_async(body)
    assert response.status_code == 200
    assert 'data: {"jsonrpc":"2.0","id":1,"result"' in response.text


def test_allowed_hosts_keeps_loopback_admitted_too():
    """Loopback wildcards stay allowed alongside any custom host, so binding
    loopback-only (e.g. for a local curl smoke test) keeps working even once
    a public hostname has been added.
    """

    async def body():
        app = server.build_app(
            stateless_http=True, allowed_hosts=("api.ergomem.com",)
        )
        return await _raw_initialize(app, _BASE_URL)

    response = run_async(body)
    assert response.status_code == 200


def test_allowed_hosts_still_rejects_an_unlisted_host():
    async def body():
        app = server.build_app(
            stateless_http=True, allowed_hosts=("api.ergomem.com",)
        )
        return await _raw_initialize(app, "http://evil.example.com")

    response = run_async(body)
    assert response.status_code == 421


def test_build_app_default_allowed_hosts_matches_sdk_auto_detect():
    """No --allowed-host / allowed_hosts=() -> current (pre-fix) behavior is
    completely unchanged: transport_security is left for FastMCP's own
    loopback auto-detect to fill in, not something build_app constructs.
    """
    app = server.build_app()
    assert app.settings.transport_security is not None
    assert app.settings.transport_security.allowed_hosts == list(
        server._LOOPBACK_ALLOWED_HOSTS
    )


def test_cli_allowed_host_flag_is_repeatable(monkeypatch):
    captured = {}

    def fake_build_app(**kwargs):
        captured.update(kwargs)

        class _StubApp:
            def run(self, **_):
                return None

        return _StubApp()

    monkeypatch.setattr(server, "build_app", fake_build_app)
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--transport",
            "streamable-http",
            "--allowed-host",
            "api.ergomem.com",
            "--allowed-host",
            "mcp.example.com",
        ],
    )

    server.main()

    assert captured["allowed_hosts"] == ("api.ergomem.com", "mcp.example.com")


def test_cli_default_has_no_allowed_hosts(monkeypatch):
    captured = {}

    def fake_build_app(**kwargs):
        captured.update(kwargs)

        class _StubApp:
            def run(self, **_):
                return None

        return _StubApp()

    monkeypatch.setattr(server, "build_app", fake_build_app)
    monkeypatch.setattr("sys.argv", ["prog", "--transport", "streamable-http"])

    server.main()

    assert captured["allowed_hosts"] == ()


# --------------------------------------------------------------------------- #
# GET /mcp-health -- sidecar liveness route (unauthenticated, HEAD auto-served)
#
# ``/mcp`` correctly 406s a plain GET/HEAD (it expects the MCP protocol),
# which is unmonitorable by a plain HTTP-status monitor -- /mcp-health is the
# real health route. It calls httpx directly against ERGO_API_URL rather than
# through the ErgoClient/`wire` fixture every other test in this module uses,
# so these tests stand up a REAL stub HTTP server for the upstream engine's
# own /health instead of a mocked transport.
# --------------------------------------------------------------------------- #


class _StubHealthHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the upstream engine's own `/health` route."""

    response_code = 200

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = b'{"status": "ok"}'
        self.send_response(self.response_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # quiet test output
        pass


@pytest.fixture
def stub_upstream():
    """Start a real HTTPServer standing in for the upstream engine's
    /health, so /mcp-health's outbound httpx.get() hits a real socket
    exactly as it would in production."""
    started: list[HTTPServer] = []

    def _start(response_code: int = 200) -> str:
        handler = type(
            "_Handler", (_StubHealthHandler,), {"response_code": response_code}
        )
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        t = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        t.start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def _closed_port_url() -> str:
    """A loopback URL nothing is listening on -- bind then immediately close
    a socket to get a free port guaranteed to refuse the connection, rather
    than guessing a fixed port number that might collide."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


async def _raw_request(app: Any, path: str, method: str = "GET") -> httpx.Response:
    """Plain HTTP request against `app`'s ASGI app -- no MCP protocol
    handshake needed since /mcp-health is a bare Starlette route, entirely
    independent of the streamable-HTTP session manager."""
    asgi_app = app.streamable_http_app()
    transport = httpx.ASGITransport(app=asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
        return await client.request(method, path)


def test_mcp_health_ok_when_upstream_healthy(monkeypatch, stub_upstream):
    monkeypatch.setenv("ERGO_API_URL", stub_upstream(200))

    async def body():
        app = server.build_app(stateless_http=True)
        return await _raw_request(app, "/mcp-health")

    response = run_async(body)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_mcp_health_degraded_when_upstream_unreachable(monkeypatch):
    monkeypatch.setenv("ERGO_API_URL", _closed_port_url())

    async def body():
        app = server.build_app(stateless_http=True)
        return await _raw_request(app, "/mcp-health")

    response = run_async(body)
    assert response.status_code == 503
    body_json = response.json()
    assert body_json["status"] == "degraded"
    assert "upstream" in body_json


def test_mcp_health_degraded_when_upstream_returns_non_200(monkeypatch, stub_upstream):
    monkeypatch.setenv("ERGO_API_URL", stub_upstream(500))

    async def body():
        app = server.build_app(stateless_http=True)
        return await _raw_request(app, "/mcp-health")

    response = run_async(body)
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_mcp_health_is_unauthenticated(monkeypatch, stub_upstream):
    """No Authorization header needed -- same policy as the upstream engine's
    own /health."""
    monkeypatch.setenv("ERGO_API_URL", stub_upstream(200))
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)

    async def body():
        app = server.build_app(stateless_http=True)
        return await _raw_request(app, "/mcp-health")

    response = run_async(body)
    assert response.status_code == 200


def test_mcp_health_serves_head_via_starlette_auto_head(monkeypatch, stub_upstream):
    """UptimeRobot-style monitors probe with HEAD -- Starlette auto-serves
    HEAD for a GET-only Route, so this needs no separate HEAD branch."""
    monkeypatch.setenv("ERGO_API_URL", stub_upstream(200))

    async def body():
        app = server.build_app(stateless_http=True)
        return await _raw_request(app, "/mcp-health", method="HEAD")

    response = run_async(body)
    assert response.status_code == 200
    assert response.content == b""
