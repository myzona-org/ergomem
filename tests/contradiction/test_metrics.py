"""Tests for `GET /metrics` (Prometheus text exposition) and structured JSON
access logging (ergo private-repo issue #121).

Prove-the-bug-first: written before server.py grows the `/metrics` route, the
auth gate, or the JSON log-format flag, so the first run of this file MUST be
RED (404 "not found" from the catch-all route, and a plain "[ergo] ..." line
regardless of ERGO_LOG_FORMAT).

Strategy mirrors test_server.py: a FakeStore/FakeNormalizer injected into the
module globals, driven over a real (plain) `http.server.HTTPServer` via httpx.
`ergo.contradiction.metrics` is reset before every test so counters from one
test never leak into another (module-level globals, single process by
design -- see metrics.py's docstring).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import HTTPServer

import httpx
import pytest

from ergo.contradiction import server, metrics
from ergo.contradiction.normalize import NormalizeUnavailable
from ergo.contradiction.store import (
    ActiveSet, Conflict, ConflictHit, ProjectLimitExceeded, QuotaExceeded, Stored,
)


class FakeNormalizer:
    def normalize(self, record):
        return {"subject": "x", "text": record.get("text", "")}


class FakePrincipal:
    def __init__(self, org_id, tenant_id, who):
        self.org_id = org_id
        self.tenant_id = tenant_id
        self.who = who
        self.role = "member"
        self.rate_limit_per_min_override = None
        self.max_active_claims_override = None
        self.plan = None


class FakeControl:
    """Minimal ControlStore stand-in: resolves exactly one known per-tenant
    bearer to a real Principal, everything else to None -- enough to prove
    /metrics never lets a resolved per-tenant token through."""

    def __init__(self, token_plaintext: str, principal: FakePrincipal):
        self.token_plaintext = token_plaintext
        self.principal = principal

    def resolve(self, token_plaintext: str):
        return self.principal if token_plaintext == self.token_plaintext else None


class FakeStore:
    """Implements just the surface /health + the guarded-write routes touch."""

    def __init__(self, retrieval=True):
        self.retrieval = retrieval
        self.normalizer = FakeNormalizer()
        self.remember_result = None
        self.remember_error = None
        self.learn_result = None
        self.learn_error = None
        self.supersede_result = None
        self.supersede_error = None
        self.has_active_claim_result = True

    def active(self, org, proj, limit=None, offset=0, projection="full"):
        return ActiveSet("has_active_claims", [{"id": "c1"}])

    def remember(self, org, proj, who, statement, reason=None, force_exception=None,
                 max_active_claims=None, project_limit=None):
        if self.remember_error is not None:
            raise self.remember_error
        if self.remember_result is not None:
            return self.remember_result
        if "conflict" in statement.lower():
            return Conflict(
                incoming_text=statement, incoming_norm={},
                conflicts=[ConflictHit(claim={"id": "old"}, tier="block",
                                       source="structural", score=0.9, reason="r")])
        return Stored(claim_id="new-id")

    def learn(self, org, proj, who, statement, source, reason=None,
              force_exception=None, max_active_claims=None, project_limit=None):
        if self.learn_error is not None:
            raise self.learn_error
        if self.learn_result is not None:
            return self.learn_result
        return self.remember(org, proj, who, statement, reason=reason,
                             force_exception=force_exception)

    def supersede(self, org, proj, who, existing_id, statement, reason=None):
        if self.supersede_error is not None:
            raise self.supersede_error
        if self.supersede_result is not None:
            return self.supersede_result
        return Stored(claim_id="sup-id")

    def has_active_claim(self, org, proj, claim_id):
        return self.has_active_claim_result


def make_cfg(**over) -> dict:
    base = dict(
        db_path="/tmp/ergo-metrics-test.db", normalizer="RuleNormalizer", nli="off",
        embedder="FakeEmbed(dim=8)", retrieval=True,
        owner="Example Owner", owner_contact="owner@example.com", backup_path=None,
        kill_review_date="2999-01-01", auth="OPEN (no token)",
        booted_at="2026-06-27T00:00:00+0000", booted_monotonic=time.monotonic(),
        last_write_at=None,
    )
    base.update(over)
    return base


@pytest.fixture
def serve(monkeypatch):
    started: list[HTTPServer] = []

    def _start(store: FakeStore, cfg: dict | None = None) -> str:
        monkeypatch.setattr(server, "_STORE", store)
        monkeypatch.setattr(server, "_CFG", cfg if cfg is not None else make_cfg())
        monkeypatch.setattr(server, "_CONTROL", None)
        monkeypatch.setattr(server, "_REGISTRY", None)
        monkeypatch.setattr(server, "_READY_PROBE_CACHE", server._ReadyProbeCache())
        httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        t = threading.Thread(target=httpd.serve_forever,
                             kwargs={"poll_interval": 0.01}, daemon=True)
        t.start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def _clean_env_and_metrics(monkeypatch):
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    monkeypatch.delenv("ERGO_SUPERADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ERGO_LOG_FORMAT", raising=False)
    metrics.reset()
    yield
    metrics.reset()


# --------------------------------------------------------------------------
# GET /metrics -- Prometheus exposition + expected metric names
# --------------------------------------------------------------------------
def test_metrics_returns_200_prometheus_text_with_expected_metric_names(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "ergo_http_requests_total" in body
    assert "ergo_http_request_duration_seconds" in body
    assert "ergo_guarded_write_outcomes_total" in body
    assert "ergo_pipeline_stage_duration_seconds" in body
    assert "ergo_http_workers" in body


def test_metrics_increments_request_counter_for_the_route_that_was_hit(serve):
    url = serve(FakeStore())
    httpx.get(f"{url}/health")
    body = httpx.get(f"{url}/metrics").text
    assert 'route="/health"' in body
    assert 'method="GET"' in body
    # the /health hit itself must be counted with a 200 status label
    assert 'ergo_http_requests_total{method="GET",route="/health",status="200"} 1' in body


def test_unknown_paths_are_bucketed_as_other_not_unbounded_labels(serve):
    # Review finding, round 1, on private-repo PR #132: the raw (attacker-controlled) request
    # path must never be used verbatim as a metrics label -- every unmatched
    # path falls through to a 404, so doing so would let a client grow the
    # in-process counters/histograms without bound just by hitting many
    # distinct nonexistent paths, no auth required.
    url = serve(FakeStore())
    for path in ("/nope", "/totally-bogus", "/x" * 50, "/another-bogus-one"):
        r = httpx.get(f"{url}{path}")
        assert r.status_code == 404
    body = httpx.get(f"{url}/metrics").text
    assert 'route="/nope"' not in body
    assert 'route="/another-bogus-one"' not in body
    assert 'route="other"' in body
    assert f'ergo_http_requests_total{{method="GET",route="other",status="404"}} 4' in body


# --------------------------------------------------------------------------
# GET /metrics -- operator-only auth gate
# --------------------------------------------------------------------------
def test_metrics_open_when_no_token_configured_at_all(serve):
    # Documented dev/test-only tradeoff (see docs/HTTP-API.md): with NEITHER
    # ERGO_SUPERADMIN_TOKEN nor ERGO_API_TOKEN configured, every route in this
    # server runs open -- /metrics is no exception.
    url = serve(FakeStore())
    r = httpx.get(f"{url}/metrics")
    assert r.status_code == 200


def test_metrics_requires_superadmin_bearer_when_configured(serve, monkeypatch):
    monkeypatch.setenv("ERGO_SUPERADMIN_TOKEN", "admin-secret")
    url = serve(FakeStore())

    r = httpx.get(f"{url}/metrics")
    assert r.status_code in (401, 403, 404)

    r = httpx.get(f"{url}/metrics", headers={"Authorization": "Bearer wrong"})
    assert r.status_code in (401, 403, 404)

    r = httpx.get(f"{url}/metrics", headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200
    assert "ergo_http_requests_total" in r.text


def test_metrics_rejects_a_tenant_or_legacy_bearer_when_superadmin_configured(serve, monkeypatch):
    # A valid legacy/tenant token must NOT be enough once an operator has
    # opted into the tighter superadmin gate.
    monkeypatch.setenv("ERGO_SUPERADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("ERGO_API_TOKEN", "legacy-secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/metrics", headers={"Authorization": "Bearer legacy-secret"})
    assert r.status_code in (401, 403, 404)


def test_metrics_falls_back_to_legacy_token_when_no_superadmin_configured(serve, monkeypatch):
    # Single-tenant deployments that never provisioned a superadmin token
    # still get SOME operator gate rather than none.
    monkeypatch.setenv("ERGO_API_TOKEN", "legacy-secret")
    url = serve(FakeStore())

    r = httpx.get(f"{url}/metrics")
    assert r.status_code == 401

    r = httpx.get(f"{url}/metrics", headers={"Authorization": "Bearer legacy-secret"})
    assert r.status_code == 200


def test_metrics_rejects_a_resolved_per_tenant_token_when_neither_token_env_is_set(
        serve, monkeypatch):
    # Review finding, round 5, on private-repo PR #132: /metrics is a GLOBAL, cross-tenant
    # surface (aggregated counters spanning every tenant's traffic). A
    # deployment can have `_CONTROL` fully wired up with real, resolvable
    # per-tenant tokens (simulated below via a monkeypatched `_CONTROL`,
    # since this build never constructs a real one) while ERGO_SUPERADMIN_TOKEN
    # and ERGO_API_TOKEN both happen to be unset (e.g. superadmin
    # provisioning was done once, then the token rotated away, while tenant
    # traffic -- which never depends on either env var -- keeps flowing).
    # In that state /metrics must NOT accept a tenant's own token just
    # because the fallback path is normally "open" -- a valid tenant token
    # must get 403 here, never treated as authorized.
    monkeypatch.delenv("ERGO_SUPERADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    url = serve(FakeStore())  # sets server._CONTROL = None; override AFTER
    principal = FakePrincipal(org_id="acme", tenant_id="tenant-acme", who="alice")
    monkeypatch.setattr(server, "_CONTROL", FakeControl("real-tenant-token", principal))

    r = httpx.get(f"{url}/metrics", headers={"Authorization": "Bearer real-tenant-token"})
    assert r.status_code == 401
    assert "ergo_http_requests_total" not in r.text


def test_metrics_still_open_with_no_bearer_when_neither_token_env_is_set(serve, monkeypatch):
    # Companion to the test above: the documented fully-open dev/test
    # default (no credential presented at all, neither env var configured)
    # must still work even with `_CONTROL` wired up (again simulated via
    # monkeypatch, never real in this build) -- only a bearer that actually
    # RESOLVES to a real tenant is rejected, presenting nothing at all is
    # unaffected.
    monkeypatch.delenv("ERGO_SUPERADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    url = serve(FakeStore())  # sets server._CONTROL = None; override AFTER
    principal = FakePrincipal(org_id="acme", tenant_id="tenant-acme", who="alice")
    monkeypatch.setattr(server, "_CONTROL", FakeControl("real-tenant-token", principal))

    r = httpx.get(f"{url}/metrics")
    assert r.status_code == 200


# --------------------------------------------------------------------------
# Guarded-write outcome counters (stored / 409 conflict / 403 quota / 5xx)
# --------------------------------------------------------------------------
def _remember(url, statement="hello world"):
    return httpx.post(f"{url}/remember", json={
        "org_id": "acme", "project": "p", "who": "ed", "statement": statement,
    })


def test_write_outcome_stored_is_counted(serve):
    url = serve(FakeStore())
    r = _remember(url, "a totally normal statement")
    assert r.status_code == 200
    body = httpx.get(f"{url}/metrics").text
    assert 'ergo_guarded_write_outcomes_total{route="/remember",outcome="stored"} 1' in body


def test_write_outcome_conflict_409_is_counted(serve):
    url = serve(FakeStore())
    r = _remember(url, "this will conflict")
    assert r.status_code == 409
    body = httpx.get(f"{url}/metrics").text
    assert 'ergo_guarded_write_outcomes_total{route="/remember",outcome="conflict"} 1' in body


def test_write_outcome_quota_403_is_counted(serve):
    store = FakeStore()
    store.remember_error = QuotaExceeded(cap=10, current=10)
    url = serve(store)
    r = _remember(url)
    assert r.status_code == 403
    body = httpx.get(f"{url}/metrics").text
    assert 'ergo_guarded_write_outcomes_total{route="/remember",outcome="quota"} 1' in body


def test_write_outcome_5xx_is_counted(serve):
    store = FakeStore()
    store.remember_error = RuntimeError("boom")
    url = serve(store)
    r = _remember(url)
    assert r.status_code == 500
    body = httpx.get(f"{url}/metrics").text
    assert 'ergo_guarded_write_outcomes_total{route="/remember",outcome="5xx"} 1' in body


def test_write_outcome_counted_for_learn_and_supersede_routes_too(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/learn", json={
        "org_id": "acme", "project": "p", "who": "ed",
        "statement": "learned fact", "source": "doc.md:1",
    })
    assert r.status_code == 200
    r = httpx.post(f"{url}/supersede", json={
        "org_id": "acme", "project": "p", "who": "ed",
        "existing_id": "old-id", "statement": "revised",
    })
    assert r.status_code == 200
    body = httpx.get(f"{url}/metrics").text
    assert 'ergo_guarded_write_outcomes_total{route="/learn",outcome="stored"} 1' in body
    assert 'ergo_guarded_write_outcomes_total{route="/supersede",outcome="stored"} 1' in body


def test_reads_and_ingest_are_not_counted_as_guarded_write_outcomes(serve):
    # /active is a read, not a guarded write -- must never show up in
    # ergo_guarded_write_outcomes_total.
    url = serve(FakeStore())
    httpx.get(f"{url}/active", params={"org_id": "acme", "project": "p"})
    body = httpx.get(f"{url}/metrics").text
    assert 'route="/active",outcome=' not in body


# --------------------------------------------------------------------------
# Structured JSON access logging (ERGO_LOG_FORMAT=json)
# --------------------------------------------------------------------------
def test_access_log_is_plaintext_by_default(serve, capsys):
    url = serve(FakeStore())
    httpx.get(f"{url}/health")
    err = capsys.readouterr().err
    assert "[ergo]" in err
    # none of the emitted lines should parse as JSON by default
    for line in err.strip().splitlines():
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


def test_access_log_is_json_when_flag_set(serve, monkeypatch, capsys):
    monkeypatch.setenv("ERGO_LOG_FORMAT", "json")
    url = serve(FakeStore())
    httpx.get(f"{url}/health")
    err = capsys.readouterr().err
    lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    assert lines, "expected at least one access-log line"
    parsed = [json.loads(ln) for ln in lines]
    assert any("message" in rec for rec in parsed)
