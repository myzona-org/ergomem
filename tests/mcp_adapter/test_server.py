"""Unit tests for the Ergo MCP adapter — HTTP layer fully mocked.

Covers: scope-default resolution, request shaping for each endpoint, auth-header
behavior, 409-conflict surfacing (must be structured, not swallowed, not raised),
and ErgoError surfacing for genuine failures. No network, no DB, no container.

default_project() itself is exercised against REAL temp git repos/worktrees
(subprocess.run mocking would hide exactly the kind of bug this guards
against — the git-common-dir vs. show-toplevel distinction is subtle enough
that a canned mock response can't tell them apart). Only the graceful-
degradation path (git too old for --path-format) mocks the FIRST subprocess
call and lets the fallback call hit real git.
"""

from __future__ import annotations

import json
import subprocess

import httpx
import pytest

from ergo.mcp import server


# --------------------------------------------------------------------------- #
# Mock transport harness
# --------------------------------------------------------------------------- #


class Recorder:
    """Captures the last request and serves a scripted response per (method, path)."""

    def __init__(self, routes):
        self.routes = routes  # {(method, path): (status, body)}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        status, body = self.routes.get(key, (404, {"error": "no route"}))
        return httpx.Response(status, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> dict:
        return json.loads(self.last.content)


class RawRecorder(Recorder):
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        status, body = self.routes.get(key, (404, "no route"))
        return httpx.Response(status, text=body)


@pytest.fixture
def wire(monkeypatch):
    """Install a mocked ErgoClient and return a helper to script routes."""
    monkeypatch.setenv("ERGO_API_TOKEN", "unit-test-token")
    # Pin a deterministic project so tests don't depend on the checkout name.
    monkeypatch.setattr(server, "default_project", lambda cwd=None: "unit-proj")

    def install(routes):
        rec = Recorder(routes)
        transport = httpx.MockTransport(rec.handler)
        client = server.ErgoClient(
            base_url="http://ergo.test", token=None, transport=transport
        )
        server.set_client(client)
        return rec

    yield install
    server.set_client(None)


# --------------------------------------------------------------------------- #
# Scope defaults
# --------------------------------------------------------------------------- #


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    # A commit is required before `git worktree add -b` can branch off HEAD.
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "seed", cwd=path)
    return path


def test_default_project_normal_repo_uses_repo_name(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    assert server.default_project(str(repo)) == "myrepo"


def test_default_project_nested_subdir_in_repo_uses_repo_name(tmp_path):
    repo = _init_repo(tmp_path / "myrepo")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert server.default_project(str(sub)) == "myrepo"


def test_default_project_worktree_uses_parent_repo_name_not_worktree_name(tmp_path):
    """The regression under test: inside a `git worktree` checkout,
    `--show-toplevel` returns the WORKTREE's own path, so the old
    implementation resolved `project` to the worktree's directory name
    (e.g. "sidebar-fix") instead of the repo it actually belongs to
    ("ergo") — fragmenting memory across every worktree. Must resolve via
    the git COMMON dir instead, which is shared by every worktree."""
    repo = _init_repo(tmp_path / "ergo")
    worktree = tmp_path / "sidebar-fix"
    _git("worktree", "add", "-q", "-b", "sidebar-fix-branch", str(worktree), cwd=repo)
    assert server.default_project(str(worktree)) == "ergo"


def test_default_project_nested_subdir_in_worktree_uses_repo_name(tmp_path):
    repo = _init_repo(tmp_path / "ergo")
    worktree = tmp_path / "teal-restore"
    _git("worktree", "add", "-q", "-b", "teal-restore-branch", str(worktree), cwd=repo)
    sub = worktree / "ergo" / "mcp"
    sub.mkdir(parents=True)
    assert server.default_project(str(sub)) == "ergo"


def test_default_project_non_repo_dir_uses_cwd_basename(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert server.default_project(str(plain)) == "not-a-repo"


def test_default_project_falls_back_when_path_format_unavailable(monkeypatch, tmp_path):
    """Simulate an installed git older than 2.31 (no --path-format support):
    the git-common-dir probe must fail gracefully (nonzero exit, no usable
    output) and default_project() must still fall back to the toplevel-
    basename behavior — real git handles the second (fallback) call."""
    repo = _init_repo(tmp_path / "myrepo")
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if "--path-format=absolute" in cmd:
            return type("R", (), {"returncode": 129, "stdout": ""})()
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server.default_project(str(repo)) == "myrepo"


def test_default_project_falls_back_to_cwd_basename_when_git_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(server.subprocess, "run", boom)
    assert server.default_project("/tmp/some/place") == "place"


def test_scope_defaults_applied(wire):
    rec = wire({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    server.ergo_remember(statement="Use TLS everywhere", reason="security")
    body = rec.last_json()
    assert body["org_id"] == "claude-code"
    assert body["project"] == "unit-proj"
    assert body["who"] == "claude-code"


def test_scope_overrides_applied(wire):
    rec = wire({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    server.ergo_remember(
        statement="s", reason="r", org_id="acme", project="platform", who="alice"
    )
    body = rec.last_json()
    assert (body["org_id"], body["project"], body["who"]) == ("acme", "platform", "alice")


# --------------------------------------------------------------------------- #
# Auth header
# --------------------------------------------------------------------------- #


def test_auth_header_sent_on_guarded_routes(wire):
    rec = wire({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    server.ergo_remember(statement="s", reason="r")
    assert rec.last.headers["Authorization"] == "Bearer unit-test-token"


def test_health_sends_no_auth_header(wire):
    rec = wire({("GET", "/health"): (200, {"status": "ok", "retrieval": True})})
    out = server.ergo_health()
    assert "Authorization" not in rec.last.headers
    assert out["status"] == "ok"


def test_client_close_closes_underlying_httpx_client():
    client = server.ErgoClient(base_url="http://ergo.test",
                               transport=httpx.MockTransport(lambda request:
                                                            httpx.Response(200, json={})))
    assert not client._client.is_closed
    client.close()
    assert client._client.is_closed


def test_get_client_lazily_constructs_singleton(monkeypatch):
    server.set_client(None)
    made = []

    class FakeClient:
        def __init__(self):
            made.append(self)

    monkeypatch.setattr(server, "ErgoClient", FakeClient)
    try:
        assert server.get_client() is made[0]
        assert server.get_client() is made[0]
        assert len(made) == 1
    finally:
        server.set_client(None)


def test_pinned_token_overrides_env(monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "env-token")
    rec = Recorder({("GET", "/diagnose"): (200, {"ok": True})})
    client = server.ErgoClient(base_url="http://ergo.test", token="pinned",
                               transport=httpx.MockTransport(rec.handler))
    assert client.diagnose() == {"ok": True}
    assert rec.last.headers["Authorization"] == "Bearer pinned"


def test_tool_call_respects_pinned_client_token_over_env(monkeypatch):
    """Regression (review finding, round 2, private-repo PR #81): the ABOVE test calls
    ``client.diagnose()`` directly, bypassing ``_call()``/``_resolve_token()``
    entirely, so it couldn't catch a real bug where ``_call()`` always passed
    an explicit ``token=api_token()`` for stdio calls too — silently
    overriding a pinned ``ErgoClient(token=...)`` with ``ERGO_API_TOKEN``
    whenever the env var happened to be set. Goes through the actual tool
    function this time, with ERGO_API_TOKEN set, so a regression here would
    send 'Bearer env-token' instead.
    """
    monkeypatch.setenv("ERGO_API_TOKEN", "env-token")
    monkeypatch.setattr(server, "default_project", lambda cwd=None: "unit-proj")
    rec = Recorder({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    client = server.ErgoClient(base_url="http://ergo.test", token="pinned",
                               transport=httpx.MockTransport(rec.handler))
    server.set_client(client)
    try:
        server.ergo_remember(statement="s", reason="r")
    finally:
        server.set_client(None)
    assert rec.last.headers["Authorization"] == "Bearer pinned"


def test_tool_call_respects_pinned_client_token_without_env(monkeypatch):
    """Same as above with ERGO_API_TOKEN unset, to pin the other half of the
    precedence: no env var to (correctly, in this case) coincide with.
    """
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    monkeypatch.setattr(server, "default_project", lambda cwd=None: "unit-proj")
    rec = Recorder({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    client = server.ErgoClient(base_url="http://ergo.test", token="pinned",
                               transport=httpx.MockTransport(rec.handler))
    server.set_client(client)
    try:
        server.ergo_remember(statement="s", reason="r")
    finally:
        server.set_client(None)
    assert rec.last.headers["Authorization"] == "Bearer pinned"


# --------------------------------------------------------------------------- #
# remember: payload shaping + 409 surfacing
# --------------------------------------------------------------------------- #


def test_remember_omits_optional_fields_when_none(wire):
    rec = wire({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    server.ergo_remember(statement="s")  # no reason, no force_exception
    body = rec.last_json()
    assert "reason" not in body
    assert "force_exception" not in body


def test_remember_includes_force_exception(wire):
    rec = wire({("POST", "/remember"): (200, {"result": "stored", "claim_id": "x"})})
    server.ergo_remember(statement="s", reason="r", force_exception="coexist on purpose")
    assert rec.last_json()["force_exception"] == "coexist on purpose"


def test_remember_409_is_surfaced_structured_not_raised(wire):
    conflict_body = {
        "result": "conflict",
        "conflicts": [
            {
                "claim": {"id": "old123", "statement": "Service X must use TLS"},
                "tier": "block",
                "score": 0.97,
                "source": "structural",
                "reason": "same subject + opposing modality",
            }
        ],
        "warnings": [],
    }
    wire({("POST", "/remember"): (409, conflict_body)})
    out = server.ergo_remember(statement="Service X must not use TLS", reason="x")
    # The conflict must come back as normal structured output, untouched.
    assert out["result"] == "conflict"
    assert out["conflicts"][0]["claim"]["id"] == "old123"
    assert out["conflicts"][0]["tier"] == "block"


def test_remember_500_surfaced_as_structured_error(wire):
    wire({("POST", "/remember"): (500, {"error": "normalizer down"})})
    out = server.ergo_remember(statement="s", reason="r")
    assert out["result"] == "error"
    assert out["status_code"] == 500
    assert out["error"] == {"error": "normalizer down"}


def test_non_json_error_body_is_wrapped_by_tool(monkeypatch):
    rec = RawRecorder({("POST", "/remember"): (500, "plain failure")})
    client = server.ErgoClient(base_url="http://ergo.test", token="x",
                               transport=httpx.MockTransport(rec.handler))
    server.set_client(client)
    try:
        out = server.ergo_remember(statement="s")
    finally:
        server.set_client(None)
    assert out == {"result": "error", "status_code": 500, "error": "plain failure"}


# --------------------------------------------------------------------------- #
# learn: provenance-attached guarded write, can 409
# --------------------------------------------------------------------------- #


def test_learn_payload_shaping(wire):
    rec = wire({("POST", "/learn"): (200, {"result": "learned", "claim_id": "x",
                                           "source": "sessions.py:847"})})
    out = server.ergo_learn(
        statement="sessions are never reused", source="sessions.py:847",
        reason="observed in the restart path")
    body = rec.last_json()
    assert body["statement"] == "sessions are never reused"
    assert body["source"] == "sessions.py:847"
    assert body["reason"] == "observed in the restart path"
    assert (body["org_id"], body["project"], body["who"]) == (
        "claude-code", "unit-proj", "claude-code")
    assert out["result"] == "learned"


def test_learn_omits_optional_fields_when_none(wire):
    rec = wire({("POST", "/learn"): (200, {"result": "learned", "claim_id": "x"})})
    server.ergo_learn(statement="s", source="f.py:1")  # no reason / force_exception
    body = rec.last_json()
    assert body["source"] == "f.py:1"
    assert "reason" not in body
    assert "force_exception" not in body


def test_learn_409_is_surfaced_structured_not_raised(wire):
    conflict_body = {
        "result": "conflict",
        "source": "memo.md",
        "conflicts": [{"claim": {"id": "old1"}, "tier": "block", "score": 0.95,
                       "source": "structural", "reason": "opposing modality"}],
        "warnings": [],
    }
    wire({("POST", "/learn"): (409, conflict_body)})
    out = server.ergo_learn(statement="X must not hold", source="memo.md", reason="x")
    assert out["result"] == "conflict"
    assert out["conflicts"][0]["claim"]["id"] == "old1"


def test_learn_includes_force_exception(wire):
    rec = wire({("POST", "/learn"): (200, {"result": "learned", "claim_id": "x"})})
    server.ergo_learn(statement="s", source="f.py:1", force_exception="approved")
    assert rec.last_json()["force_exception"] == "approved"


# --------------------------------------------------------------------------- #
# supersede: also gated, can 409
# --------------------------------------------------------------------------- #


def test_supersede_payload_and_409(wire):
    rec = wire({("POST", "/supersede"): (409, {"result": "conflict", "conflicts": []})})
    out = server.ergo_supersede(existing_id="abc", statement="new", reason="changed")
    assert rec.last_json()["existing_id"] == "abc"
    assert out["result"] == "conflict"


# --------------------------------------------------------------------------- #
# retract: wrong-at-birth removal — 404/400 must be structured, not raised
# --------------------------------------------------------------------------- #


def test_retract_payload_shaping_and_scope_defaults(wire):
    rec = wire({("POST", "/retract"): (200, {"result": "retracted",
                                             "claim_id": "abc", "retracted": True})})
    out = server.ergo_retract(claim_id="abc", reason="test junk from a demo")
    body = rec.last_json()
    assert body["claim_id"] == "abc"
    assert body["reason"] == "test junk from a demo"
    assert (body["org_id"], body["project"], body["who"]) == (
        "claude-code", "unit-proj", "claude-code")
    assert out["result"] == "retracted"
    assert out["claim_id"] == "abc"


def test_retract_404_is_structured_error_not_exception(wire):
    wire({("POST", "/retract"): (404, {"error": "no active claim abc in (org=o, project=p)"})})
    out = server.ergo_retract(claim_id="abc", reason="typo")
    assert out["result"] == "error"
    assert out["status_code"] == 404
    assert "no active claim" in out["error"]["error"]


def test_retract_400_blank_reason_is_structured_error(wire):
    wire({("POST", "/retract"): (400, {"error": "retract requires a non-empty reason"})})
    out = server.ergo_retract(claim_id="abc", reason="   ")
    assert out["result"] == "error"
    assert out["status_code"] == 400


# --------------------------------------------------------------------------- #
# read paths: param shaping
# --------------------------------------------------------------------------- #


def test_recall_params(wire):
    rec = wire({("GET", "/recall"): (200, {"results": []})})
    server.ergo_recall(query="how is testing set up", limit=5)
    q = dict(rec.last.url.params)
    assert q["q"] == "how is testing set up"
    assert q["limit"] == "5"
    assert q["org_id"] == "claude-code"
    assert q["project"] == "unit-proj"


def test_why_null_passthrough(wire):
    wire({("GET", "/why"): (200, {"why": None})})
    out = server.ergo_why(query="why anything")
    assert out == {"why": None}


def test_recall_scope_field_passes_through_to_the_model(wire):
    # PASSTHROUGH (private-repo issue #184): the `scope` object is worthless if the
    # adapter drops it on the way to the model. This would go red if
    # `_call`/`ErgoClient._parse`/`ergo_recall` filtered the response to a
    # known set of keys instead of passing the engine's body through whole.
    wire({("GET", "/recall"): (200, {
        "results": [],
        "scope": {"project": "default", "source": "fallback",
                  "state": "project_missing"},
    })})
    out = server.ergo_recall(query="anything")
    assert out["scope"] == {"project": "default", "source": "fallback",
                            "state": "project_missing"}


def test_why_scope_field_passes_through_to_the_model(wire):
    wire({("GET", "/why"): (200, {
        "why": None,
        "scope": {"project": "acme", "source": "token_default",
                  "state": "project_missing"},
    })})
    out = server.ergo_why(query="anything")
    assert out["scope"] == {"project": "acme", "source": "token_default",
                            "state": "project_missing"}


def test_why_suppressed_field_passes_through_to_the_model(wire):
    # private-repo issue #202: a suppressed-below-floor answer is worthless if the adapter
    # drops it on the way to the model.
    wire({("GET", "/why"): (200, {
        "why": None,
        "suppressed": {"reason": "below_relevance_floor", "score": 0.4511, "floor": 0.5},
        "scope": {"project": "acme", "source": "explicit", "state": "has_active_claims"},
    })})
    out = server.ergo_why(query="zzz quantum ferret carburetor nonsense")
    assert out["why"] is None
    assert out["suppressed"] == {"reason": "below_relevance_floor",
                                  "score": 0.4511, "floor": 0.5}


def test_ingest_payload(wire):
    rec = wire({("POST", "/ingest"): (200, {"result": "ingested", "chunks": 3})})
    out = server.ergo_ingest(text="body text", source="runbook.md")
    body = rec.last_json()
    assert body["text"] == "body text"
    assert body["source"] == "runbook.md"
    assert out["chunks"] == 3


def test_ingest_omits_source_when_none(wire):
    rec = wire({("POST", "/ingest"): (200, {"result": "ingested", "chunks": 1})})
    server.ergo_ingest(text="body text")
    assert "source" not in rec.last_json()


def test_read_tool_errors_are_structured(wire):
    wire({
        ("GET", "/recall"): (503, {"error": "retrieval disabled"}),
        ("GET", "/why"): (500, {"error": "boom"}),
        ("GET", "/active"): (401, {"error": "unauthorized"}),
        ("GET", "/history"): (500, {"error": "history boom"}),
        ("GET", "/diagnose"): (500, {"error": "diag boom"}),
        ("GET", "/health"): (503, {"error": "unhealthy"}),
    })
    assert server.ergo_recall(query="q")["status_code"] == 503
    assert server.ergo_why(query="q")["status_code"] == 500
    assert server.ergo_active()["status_code"] == 401
    assert server.ergo_history()["status_code"] == 500
    assert server.ergo_diagnose()["status_code"] == 500
    assert server.ergo_health()["status_code"] == 503


# --------------------------------------------------------------------------- #
# transport errors: must not escape as raw exceptions (finding #2)
# --------------------------------------------------------------------------- #


def test_connect_error_is_surfaced_structured_with_url(monkeypatch):
    monkeypatch.setattr(server, "default_project", lambda cwd=None: "unit-proj")

    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = server.ErgoClient(
        base_url="http://ergo.test", token="x", transport=httpx.MockTransport(boom)
    )
    server.set_client(client)
    try:
        out = server.ergo_remember(statement="s", reason="r")
    finally:
        server.set_client(None)
    assert out["result"] == "error"
    assert out["url"] == "http://ergo.test"
    assert "connection refused" in out["error"]


def test_timeout_error_is_surfaced_structured_on_read_tool(monkeypatch):
    monkeypatch.setattr(server, "default_project", lambda cwd=None: "unit-proj")

    def boom(request):
        raise httpx.TimeoutException("timed out", request=request)

    client = server.ErgoClient(
        base_url="http://ergo.test", token="x", transport=httpx.MockTransport(boom)
    )
    server.set_client(client)
    try:
        out = server.ergo_recall(query="q")
    finally:
        server.set_client(None)
    assert out == {"result": "error", "error": "timed out", "url": "http://ergo.test"}


def test_default_client_uses_short_connect_timeout_and_long_read_timeout():
    client = server.ErgoClient(base_url="http://ergo.test")
    timeout = client._client.timeout
    assert timeout.connect < 60.0
    assert timeout.read == 60.0


def test_active_history_diagnose(wire):
    rec = wire(
        {
            ("GET", "/active"): (200, {"state": "empty", "claims": []}),
            ("GET", "/history"): (200, {"history": []}),
            ("GET", "/diagnose"): (200, {"counts": {"claims_total": 0}, "warnings": []}),
        }
    )
    assert server.ergo_active()["state"] == "empty"
    assert server.ergo_history()["history"] == []
    assert server.ergo_diagnose()["counts"]["claims_total"] == 0
    # diagnose takes no scope params
    assert dict(rec.requests[-1].url.params) == {}


# --------------------------------------------------------------------------- #
# ergo_active: GET /active (private-repo issue #185) -- bounded page + pagination fields
# --------------------------------------------------------------------------- #


def test_active_custom_limit_and_offset_passthrough(wire):
    rec = wire({("GET", "/active"): (200, {
        "state": "has_active_claims", "count": 100, "returned": 5,
        "truncated": True, "next_offset": 15, "claims": [],
    })})
    out = server.ergo_active(limit=5, offset=10)
    q = dict(rec.last.url.params)
    assert q["limit"] == "5"
    assert q["offset"] == "10"
    assert out["truncated"] is True
    assert out["next_offset"] == 15


def test_active_projection_passthrough_when_set(wire):
    # private-repo issue #206: summary mode is opt-in -- an explicit projection is sent as
    # a query param, same shape as limit/offset.
    rec = wire({("GET", "/active"): (200, {
        "state": "has_active_claims", "count": 1, "returned": 1,
        "truncated": False, "next_offset": None,
        "claims": [{"id": "c1", "statement": "x"}],
    })})
    out = server.ergo_active(projection="summary")
    q = dict(rec.last.url.params)
    assert q["projection"] == "summary"
    assert out["claims"] == [{"id": "c1", "statement": "x"}]


def test_active_projection_omitted_when_unset(wire):
    # Same "never hardcode the engine's own default" contract as `limit` --
    # an omitted projection sends no query param at all, so the ENGINE's own
    # default ("full") applies.
    rec = wire({("GET", "/active"): (200, {"state": "empty", "claims": []})})
    server.ergo_active()
    q = dict(rec.last.url.params)
    assert "projection" not in q


def test_active_offset_past_end_still_reports_has_active_claims(wire):
    # Non-negotiable #4, exercised through the MCP surface: state describes
    # the scope, not the page.
    wire({("GET", "/active"): (200, {
        "state": "has_active_claims", "count": 3, "returned": 0,
        "truncated": False, "next_offset": None, "claims": [],
    })})
    out = server.ergo_active(offset=500)
    assert out["state"] == "has_active_claims"
    assert out["claims"] == []


# --------------------------------------------------------------------------- #
# ergo_audit: GET /audit (private-repo issue #124) -- tenant-facing audit trail
# --------------------------------------------------------------------------- #


def test_audit_params_omit_limit_when_unset(wire):
    # No `limit` passed -> no `limit` query param sent, so the ENGINE's own
    # DEFAULT_AUDIT_LIMIT applies -- this adapter never hardcodes that number.
    rec = wire({("GET", "/audit"): (200, {"writes": [], "conflict_events": []})})
    out = server.ergo_audit()
    q = dict(rec.last.url.params)
    assert q["org_id"] == "claude-code"
    assert q["project"] == "unit-proj"
    assert "limit" not in q
    assert out == {"writes": [], "conflict_events": []}


def test_audit_custom_limit_passthrough(wire):
    rec = wire({("GET", "/audit"): (200, {"writes": [], "conflict_events": []})})
    server.ergo_audit(limit=5)
    assert dict(rec.last.url.params)["limit"] == "5"


def test_audit_error_is_structured(wire):
    wire({("GET", "/audit"): (401, {"error": "unauthorized"})})
    out = server.ergo_audit()
    assert out["result"] == "error"
    assert out["status_code"] == 401


# --------------------------------------------------------------------------- #
# export/import: client-level GET/POST + tool payload shaping + 403/413
# --------------------------------------------------------------------------- #


def test_client_export_does_get_export():
    rec = Recorder({("GET", "/export"): (200, {"claim_count": 0, "claims": []})})
    client = server.ErgoClient(base_url="http://ergo.test", token="x",
                               transport=httpx.MockTransport(rec.handler))
    out = client.export({"org_id": "acme"})
    assert (rec.last.method, rec.last.url.path) == ("GET", "/export")
    assert out["claims"] == []


def test_client_import_claims_does_post_import():
    rec = Recorder({("POST", "/import"): (200, {"imported": 0, "skipped_duplicate": 0,
                                                 "conflicts": [], "conflict_count": 0,
                                                 "cap_reached": False})})
    client = server.ErgoClient(base_url="http://ergo.test", token="x",
                               transport=httpx.MockTransport(rec.handler))
    out = client.import_claims({"org_id": "acme", "claims": []})
    assert (rec.last.method, rec.last.url.path) == ("POST", "/import")
    assert out["imported"] == 0


def test_ergo_export_uses_scoped_params(wire):
    rec = wire({("GET", "/export"): (200, {"ergo_export_version": 1, "org_id": "claude-code",
                                            "claim_count": 0, "claims": []})})
    out = server.ergo_export()
    q = dict(rec.last.url.params)
    assert q["org_id"] == "claude-code"
    assert q["project"] == "unit-proj"
    assert out["claim_count"] == 0


def test_ergo_export_passes_project_and_org_overrides(wire):
    rec = wire({("GET", "/export"): (200, {"claim_count": 0, "claims": []})})
    server.ergo_export(project="platform", org_id="acme")
    q = dict(rec.last.url.params)
    assert (q["org_id"], q["project"]) == ("acme", "platform")


def test_ergo_export_403_is_structured_error_not_raised(wire):
    wire({("GET", "/export"): (403, {"error": "import_requires_pro_or_team",
                                     "plan": "free", "upgrade": "..."})})
    out = server.ergo_export()
    assert out["result"] == "error"
    assert out["status_code"] == 403
    assert out["error"]["error"] == "import_requires_pro_or_team"


def test_ergo_export_omits_limit_and_offset_when_unset(wire):
    # No `limit`/`offset` passed -> the engine's own unbounded default
    # applies (private-repo issue #203 backward compat) -- this adapter never hardcodes
    # a page size onto an existing caller.
    rec = wire({("GET", "/export"): (200, {"claim_count": 0, "claims": []})})
    server.ergo_export()
    q = dict(rec.last.url.params)
    assert "limit" not in q
    assert "offset" not in q


def test_ergo_export_custom_limit_and_offset_passthrough(wire):
    rec = wire({("GET", "/export"): (200, {
        "claim_count": 100, "returned": 5, "truncated": True,
        "next_offset": 15, "as_of_seq": {"platform": 100}, "claims": [],
    })})
    out = server.ergo_export(limit=5, offset=10)
    q = dict(rec.last.url.params)
    assert q["limit"] == "5"
    assert q["offset"] == "10"
    assert out["truncated"] is True
    assert out["next_offset"] == 15


def test_ergo_import_payload_shaping_minimal(wire):
    rec = wire({("POST", "/import"): (200, {"imported": 1, "skipped_duplicate": 0,
                                             "conflicts": [], "conflict_count": 0,
                                             "cap_reached": False})})
    claims = [{"project": "platform", "statement": "Deploy on Tuesdays", "reason": "on-call"}]
    out = server.ergo_import(claims=claims)
    body = rec.last_json()
    assert body["org_id"] == "claude-code"
    assert body["claims"] == claims
    assert "target_project" not in body
    assert "who" not in body
    assert out["imported"] == 1


def test_ergo_import_includes_target_project_and_who(wire):
    rec = wire({("POST", "/import"): (200, {"imported": 0, "skipped_duplicate": 0,
                                             "conflicts": [], "conflict_count": 0,
                                             "cap_reached": False})})
    server.ergo_import(claims=[], target_project="restored", who="alice", org_id="acme")
    body = rec.last_json()
    assert body["target_project"] == "restored"
    assert body["who"] == "alice"
    assert body["org_id"] == "acme"


def test_ergo_import_reports_conflicts_and_cap_reached_unchanged(wire):
    result_body = {
        "imported": 3,
        "skipped_duplicate": 1,
        "conflicts": [{"statement": "Never deploy on Tuesdays", "against": "old1",
                       "reason": "same subject + opposing modality", "tier": "block"}],
        "conflict_count": 1,
        "cap_reached": True,
    }
    wire({("POST", "/import"): (200, result_body)})
    out = server.ergo_import(claims=[{"statement": "s", "project": "p"}])
    assert out == result_body


def test_ergo_import_413_is_structured_error_not_raised(wire):
    wire({("POST", "/import"): (413, {
        "error": "import of 15000 claims exceeds the plan's import_max_claims of 10000",
        "import_max_claims": 10000,
    })})
    out = server.ergo_import(claims=[{"statement": "s", "project": "p"}])
    assert out["result"] == "error"
    assert out["status_code"] == 413
    assert out["error"]["import_max_claims"] == 10000


# --------------------------------------------------------------------------- #
# ASYNC IMPORT (private-repo issue #122): ergo_import's 202 enqueue shape
# --------------------------------------------------------------------------- #


def test_client_import_claims_accepts_a_202_enqueue_response():
    rec = Recorder({("POST", "/import"): (202, {
        "job_id": "7e0772adcfc04706bdc03d03baf96ebf", "status": "pending",
        "claims_count": 1})})
    client = server.ErgoClient(base_url="http://ergo.test", token="x",
                               transport=httpx.MockTransport(rec.handler))
    out = client.import_claims({"org_id": "acme", "claims": [{"statement": "s"}]})
    assert (rec.last.method, rec.last.url.path) == ("POST", "/import")
    assert out["status"] == "pending"
    assert out["job_id"]


def test_ergo_import_reports_a_202_job_enqueue_unchanged(wire):
    body = {"job_id": "7e0772adcfc04706bdc03d03baf96ebf", "status": "pending",
            "claims_count": 1}
    wire({("POST", "/import"): (202, body)})
    out = server.ergo_import(claims=[{"statement": "s", "project": "p"}])
    assert out == body


