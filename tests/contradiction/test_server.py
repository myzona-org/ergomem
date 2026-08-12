"""Route + auth tests for ergo.contradiction.server (the HTTP API boundary).

The MCP-adapter tests mock httpx and never run these handlers, so this file is the
only thing exercising the real stdlib http.server layer: the Bearer-auth gate,
required-field validation (400s), the 200/409/503 status mapping, query-param /
limit clamping, error->500 mapping, and the /diagnose warnings assembly.

Strategy: inject a FakeStore + a synthetic _CFG into the module globals and run a
real HTTPServer on an ephemeral port, then drive it with httpx. The contradiction
engine itself is faked — we are testing the web layer, not the store.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import HTTPServer
from urllib.parse import urlparse

import httpx
import pytest

from ergo.contradiction import server
from ergo.contradiction.normalize import NormalizeUnavailable
from ergo.contradiction.store import (Stored, Conflict, ConflictHit, ActiveSet,
                                      ExportSet, Retracted)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
DEFAULT_DIAG = {
    "counts": {"claims_total": 5, "projects": 1},
    "conflicts": {"blocked": 0, "warned": 0},
    "versions": {"drift": False, "normalizer": "RuleNormalizer", "schema": 1},
    "last_write_at": "2026-06-27T01:00:00+0000",
}


class FakeNormalizer:
    """Minimal normalizer fake for `/ready`'s probe: records every call
    (so tests can assert how many times the normalizer was actually
    invoked, e.g. the TTL-cache dedupe) and can be flagged to raise
    NormalizeUnavailable on demand. `gate`, if set, makes `normalize()`
    block until released -- used to simulate a slow/hanging provider call
    for the single-flight concurrency test."""

    def __init__(self):
        self.calls: list[dict] = []
        self.unavailable_error: NormalizeUnavailable | None = None
        self.gate: threading.Event | None = None

    def normalize(self, record):
        self.calls.append(record)
        if self.gate is not None:
            self.gate.wait(timeout=5.0)
        if self.unavailable_error is not None:
            raise self.unavailable_error
        return {"subject": "x", "text": record.get("text", "")}


class FakeStore:
    """Implements just the surface the routes touch; records calls for assertions."""

    def __init__(self, retrieval=True):
        self.retrieval = retrieval
        self.calls: list[tuple] = []
        self.recall_limit = None
        self.raise_on: set[str] = set()
        self.active_state = "has_active_claims"
        self.active_claims = [{"id": "c1", "statement": "x"}]
        self.recall_result = [{"id": "c1", "score": 0.5}]
        self.why_result = {"id": "c1", "reason": "because"}
        # project_state (private-repo issue #184 follow-up) fakes: kept as TWO independent
        # sets so a test can put a project in "has a `projects` row but no
        # active claims" (empty) or "has active claims" WITHOUT that being
        # forced to agree with whatever recall_result/why_result says -- the
        # real bug this follow-up fixes is exactly recall/why disagreeing
        # with the store's actual claim state.
        self.projects_with_rows: set[str] = set()
        self.projects_with_active_claims: set[str] = set()
        self.remember_result = None
        self.remember_error = None
        self.learn_result = None
        self.learn_error = None
        self.supersede_result = None
        self.supersede_error = None
        self.has_active_claim_result = True  # supersede pre-check: claim exists
        self.retract_error = None
        self.diag = dict(DEFAULT_DIAG)
        self.normalizer = FakeNormalizer()  # /ready's normalizer liveness probe

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise RuntimeError(f"boom in {name}")

    def active(self, org, proj, limit=None, offset=0, projection="full"):
        self.calls.append(("active", org, proj, limit, offset, projection))
        self._maybe_raise("active")
        return ActiveSet(self.active_state, self.active_claims)

    def history(self, org, proj):
        self.calls.append(("history", org, proj))
        self._maybe_raise("history")
        return [{"id": "c1", "seq": 1}]

    def audit_log(self, org, proj, limit=200):
        self.calls.append(("audit_log", org, proj, limit))
        self._maybe_raise("audit_log")
        return {"writes": [{"id": 1, "org_id": org, "project": proj}],
                "conflict_events": [{"id": 1, "org_id": org, "project": proj}]}

    def recall(self, org, proj, query, limit=10):
        self.calls.append(("recall", org, proj, query, limit))
        self.recall_limit = limit
        self._maybe_raise("recall")
        return self.recall_result

    def why(self, org, proj, query):
        self.calls.append(("why", org, proj, query))
        self._maybe_raise("why")
        return self.why_result

    def project_state(self, org, proj):
        self.calls.append(("project_state", org, proj))
        if proj in self.projects_with_active_claims:
            return "has_active_claims"
        if proj in self.projects_with_rows:
            return "empty"
        return "project_missing"

    def remember(self, org, proj, who, statement, reason=None, force_exception=None,
                 max_active_claims=None, project_limit=None):
        # max_active_claims / project_limit (PLAN-METERING)
        # accepted but not recorded in `calls` -- they're real
        # ContradictionStore's concern now (enforced atomically inside its
        # write-guard transaction), not the HTTP layer's; server.py just
        # passes the effective values through. Existing `calls[-1] == (...)`
        # assertions elsewhere stay unchanged.
        self.calls.append(("remember", org, proj, who, statement, reason, force_exception))
        self._maybe_raise("remember")
        if self.remember_error is not None:
            raise self.remember_error
        if self.remember_result is not None:
            return self.remember_result
        if "conflict" in statement.lower():
            return Conflict(
                incoming_text=statement, incoming_norm={},
                conflicts=[ConflictHit(claim={"id": "old"}, tier="block",
                                       score=0.9, source="structural", reason="r")])
        return Stored(claim_id="new-id")

    def learn(self, org, proj, who, statement, source, reason=None,
              force_exception=None, max_active_claims=None, project_limit=None):
        self.calls.append(("learn", org, proj, who, statement, source, reason,
                           force_exception))
        self._maybe_raise("learn")
        if self.learn_error is not None:
            raise self.learn_error
        if self.learn_result is not None:
            return self.learn_result
        if "conflict" in statement.lower():
            return Conflict(
                incoming_text=statement, incoming_norm={},
                conflicts=[ConflictHit(claim={"id": "old"}, tier="block",
                                       score=0.9, source="structural", reason="r")])
        return Stored(claim_id="learned-id")

    def supersede(self, org, proj, who, existing_id, statement, reason=None):
        self.calls.append(("supersede", org, proj, who, existing_id, statement, reason))
        self._maybe_raise("supersede")
        if self.supersede_error is not None:
            raise self.supersede_error
        if self.supersede_result is not None:
            return self.supersede_result
        return Stored(claim_id="sup-id")

    def retract(self, org, proj, who, claim_id, reason):
        self.calls.append(("retract", org, proj, who, claim_id, reason))
        self._maybe_raise("retract")
        if self.retract_error is not None:
            raise self.retract_error
        return Retracted(claim_id=claim_id)

    def has_active_statement(self, org, proj, statement):
        self.calls.append(("has_active_statement", org, proj, statement))
        return False

    def has_active_claim(self, org, proj, claim_id):
        self.calls.append(("has_active_claim", org, proj, claim_id))
        return self.has_active_claim_result

    def remember_fact(self, org, proj, who, statement):
        self.calls.append(("remember_fact", org, proj, who, statement))
        self._maybe_raise("remember_fact")

    def remember_facts_batch(self, org, proj, who, statements, max_active_claims=None):
        # ingest_text() now calls this instead of looping remember_fact()
        # itself (the real store does the whole batch in one
        # atomic transaction) -- the fake just loops the same dedup +
        # remember_fact calls it always recorded, so existing assertions on
        # `calls` (and `raise_on = {"remember_fact"}`) are unaffected.
        stored, skipped, claim_ids = 0, 0, []
        for stmt in statements:
            if self.has_active_statement(org, proj, stmt):
                skipped += 1
                continue
            self.remember_fact(org, proj, who, stmt)
            stored += 1
            claim_ids.append(f"fake-{stored}")
        return {"stored": stored, "skipped": skipped, "claim_ids": claim_ids}

    def diagnostics(self):
        self.calls.append(("diagnostics",))
        self._maybe_raise("diagnostics")
        return self.diag

    def export_claims(self, org_id, project=None, limit=None, offset=0):
        self.calls.append(("export_claims", org_id, project, limit, offset))
        self._maybe_raise("export_claims")
        claims = [{"id": "c1", "org_id": org_id, "statement": "exported claim"}]
        return ExportSet(claims=claims, count=len(claims), returned=len(claims),
                         as_of_seq={project or "p": 1})


class FakeControl:
    """Minimal ControlStore mock for testing admin tenant routes."""

    def __init__(self):
        self.calls: list[tuple] = []
        # Map of tenant_id -> {"rate_limit_per_min": None, "max_active_claims": None, "plan": ...}
        self.tenant_limits = {}
        # If set, resolve() raises this instead of returning -- simulates a
        # per-tenant control-store fault (locked/corrupt/mid-migration) for
        # the /health + /ready auth-gate exception-safety tests (review
        # finding on private-repo PR #191).
        self.resolve_error: Exception | None = None

    def get_tenant_id(self, org_id: str) -> str | None:
        """Return a tenant_id for the given org_id, or None if not provisioned."""
        self.calls.append(("get_tenant_id", org_id))
        # For testing: return a synthetic tenant_id if org_id starts with "provisioned_",
        # else return None (not provisioned, falls back to pooled store)
        if org_id.startswith("provisioned_"):
            return f"tenant_{org_id}"
        return None

    def get_limits(self, tenant_id: str) -> dict:
        """Return plan limits for a tenant."""
        self.calls.append(("get_limits", tenant_id))
        if tenant_id in self.tenant_limits:
            return self.tenant_limits[tenant_id]
        return {"rate_limit_per_min": None, "max_active_claims": None, "plan": None}

    def resolve(self, token_plaintext: str):
        """Real ControlStore.resolve() reads its backing store with no
        try/except of its own -- this fake mirrors that "can raise" contract
        for tests."""
        self.calls.append(("resolve", token_plaintext))
        if self.resolve_error is not None:
            raise self.resolve_error
        return None  # unknown token -- same as a real miss


def make_cfg(**over) -> dict:
    base = dict(
        db_path="/tmp/ergo-test.db", normalizer="RuleNormalizer", nli="off",
        embedder="FakeEmbed(dim=8)", retrieval=True,
        owner="Example Owner", owner_contact="owner@example.com", backup_path=None,
        kill_review_date="2999-01-01", auth="OPEN (no token)",
        booted_at="2026-06-27T00:00:00+0000", booted_monotonic=time.monotonic(),
        last_write_at=None,
    )
    base.update(over)
    return base


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def serve(monkeypatch):
    """Factory: start a real HTTPServer wired to a given store+cfg; return base URL."""
    started: list[HTTPServer] = []

    def _start(store: FakeStore, cfg: dict | None = None) -> str:
        monkeypatch.setattr(server, "_STORE", store)
        monkeypatch.setattr(server, "_CFG", cfg if cfg is not None else make_cfg())
        # Reset the multi-tenancy globals so a real ControlStore/TenantRegistry
        # left behind by an earlier boot() test (module-global state, sqlite
        # connections are thread-bound) never bleeds into a route test running
        # the handler in its own thread. Tests that need a resolvable
        # per-tenant token opt back in explicitly via monkeypatch.
        monkeypatch.setattr(server, "_CONTROL", None)
        monkeypatch.setattr(server, "_REGISTRY", None)
        # /ready's normalizer-probe cache is module/process state (READY-ROUTE):
        # bind a FRESH instance per `serve()` call so a cached result from one
        # test can never wedge into an unrelated test (monkeypatch auto-reverts
        # this at teardown, same as _STORE/_CFG above).
        monkeypatch.setattr(server, "_READY_PROBE_CACHE", server._ReadyProbeCache())
        httpd = HTTPServer(("127.0.0.1", 0), server.Handler)
        # tight poll_interval so httpd.shutdown() in teardown returns promptly
        # (default 0.5s poll makes every test pay a half-second teardown tax).
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
def _no_token(monkeypatch):
    # Default: server runs open. Auth tests opt back in with setenv.
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)


# --------------------------------------------------------------------------
# /health (always open) + auth gate
# --------------------------------------------------------------------------
def test_health_ok_and_open_without_token(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["probe_state"] == "has_active_claims"  # from the readiness probe


def test_build_normalizer_falls_back_when_llm_constructor_fails(monkeypatch):
    from ergo.contradiction import normalize

    def boom():
        raise RuntimeError("missing key")

    monkeypatch.setenv("ERGO_LLM_PROVIDER", "openai")
    monkeypatch.setattr(normalize, "LLMNormalizer", boom)
    normalizer, name = server._build_normalizer()
    assert type(normalizer).__name__ == "RuleNormalizer"
    assert name == "RuleNormalizer (LLM fallback)"
    # the fallback is the same long-lived singleton: it must be stateless too
    normalizer.normalize({"text": "TLS 1.2 must be enabled in prod."})
    assert normalizer.normalize({"text": "It must be disabled."})["subject"] == "it"


def test_build_normalizer_default_is_stateless_across_requests(monkeypatch):
    """The server singleton normalizes every request from every (org, project),
    so pronoun-coref context tracking would leak one tenant's subject into
    another tenant's persisted norm (audit 2026-07-04 finding 1). A standalone
    pronoun must stay unresolved rather than bind to a prior request's subject."""
    monkeypatch.delenv("ERGO_LLM_PROVIDER", raising=False)
    n, name = server._build_normalizer()
    assert name == "RuleNormalizer"
    n.normalize({"text": "TLS 1.2 must be enabled in prod."})
    assert n.normalize({"text": "It must be disabled in prod."})["subject"] == "it"


def test_build_nli_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ERGO_NLI", "off")
    assert server._build_nli() == (None, "off")


def test_build_nli_falls_back_when_import_or_init_fails(monkeypatch):
    class BoomNLI:
        def __init__(self, **kwargs):
            raise RuntimeError("no model")

    import ergo.contradiction.nli as nli_mod
    monkeypatch.setenv("ERGO_NLI", "on")
    monkeypatch.setattr(nli_mod, "NLIJudge", BoomNLI)
    judge, name = server._build_nli()
    assert judge is None
    assert name == "unavailable"


def test_build_embedder_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ERGO_RETRIEVAL", "false")
    assert server._build_embedder() == (None, "off (ERGO_RETRIEVAL)")


def test_build_embedder_success_warms_model(monkeypatch):
    calls = []

    class E:
        dim = 7
        def embed(self, text):
            calls.append(text)
            return [0.0] * self.dim

    import ergo.embedders as embedders
    monkeypatch.setenv("ERGO_RETRIEVAL", "on")
    monkeypatch.setattr(embedders, "get_embedder", lambda: E())
    embedder, name = server._build_embedder()
    assert calls == ["warmup"]
    assert name == "E(dim=7)"
    assert embedder.dim == 7


def test_build_embedder_failure_disables_retrieval(monkeypatch):
    import ergo.embedders as embedders

    monkeypatch.setenv("ERGO_RETRIEVAL", "on")
    monkeypatch.setattr(embedders, "get_embedder",
                        lambda: (_ for _ in ()).throw(RuntimeError("no embedder")))
    embedder, name = server._build_embedder()
    assert embedder is None
    assert name == "unavailable: no embedder"


def test_build_embedder_wrong_length_fails_loud_at_boot(monkeypatch):
    """A model whose real output length differs from the declared dim must
    crash boot with a clear error (naming model + expected + actual), NOT boot
    green and then 500 every write at the sqlite-vec insert (mcp audit #1)."""
    class WrongLen:
        dim = 384
        model = "BAAI/bge-base-en-v1.5"
        def embed(self, text):
            return [0.0] * 768  # 768-dim model, dim left at the 384 default

    import ergo.embedders as embedders
    monkeypatch.setenv("ERGO_RETRIEVAL", "on")
    monkeypatch.setattr(embedders, "get_embedder", lambda: WrongLen())
    with pytest.raises(RuntimeError) as ei:
        server._build_embedder()
    msg = str(ei.value)
    assert "BAAI/bge-base-en-v1.5" in msg  # names the model
    assert "384" in msg                    # the configured dim
    assert "768" in msg                    # the actual embedding length


def test_boot_refuses_tokenless_non_loopback_bind(monkeypatch, tmp_path, capsys):
    """A tokenless public bind must fail before any server dependencies load."""
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    monkeypatch.setenv("ERGO_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("ERGO_DB_PATH", str(tmp_path / "ergo.db"))
    monkeypatch.delenv("ERGO_INSECURE_NO_AUTH", raising=False)
    monkeypatch.setattr(server, "_build_normalizer",
                        lambda: pytest.fail("boot initialized before auth guard"))

    with pytest.raises(SystemExit) as exc:
        server.boot()

    assert exc.value.code != 0
    message = capsys.readouterr().err
    assert "set ERGO_API_TOKEN" in message
    assert "ERGO_HTTP_HOST=127.0.0.1" in message


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_tokenless_loopback_bind_is_allowed(monkeypatch, host):
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    monkeypatch.delenv("ERGO_INSECURE_NO_AUTH", raising=False)
    server._validate_auth_bind(host)


def test_insecure_no_auth_allows_tokenless_non_loopback(monkeypatch, capsys):
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    monkeypatch.setenv("ERGO_INSECURE_NO_AUTH", "1")

    server._validate_auth_bind("0.0.0.0")

    assert "OPEN TO THE NETWORK" in capsys.readouterr().err


def test_token_keeps_non_loopback_bind_allowed(monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    monkeypatch.delenv("ERGO_INSECURE_NO_AUTH", raising=False)
    server._validate_auth_bind("0.0.0.0")


def test_boot_populates_store_and_cfg(monkeypatch, tmp_path):
    class FakeContradictionStore:
        def __init__(self, path, normalizer=None, nli=None, embedder=None):
            self.path = path
            self.normalizer = normalizer
            self.nli = nli
            self.embedder = embedder
            self.retrieval = embedder is not None

    db = tmp_path / "nested" / "ergo.db"
    monkeypatch.setenv("ERGO_DB_PATH", str(db))
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    monkeypatch.setenv("ERGO_OWNER", "Owner")
    monkeypatch.setattr(server, "_build_normalizer", lambda: ("norm", "Norm"))
    monkeypatch.setattr(server, "_build_nli", lambda: ("nli", "NLI"))
    monkeypatch.setattr(server, "_build_embedder", lambda: ("embed", "Embed"))
    monkeypatch.setattr(server, "ContradictionStore", FakeContradictionStore)
    server.boot()
    assert server._STORE.path == str(db)
    assert server._STORE.retrieval is True
    assert server._CFG["db_path"] == str(db)
    assert server._CFG["auth"] == "token"
    assert server._CFG["owner"] == "Owner"


@pytest.mark.parametrize("configured_host, effective_host", [
    (None, "0.0.0.0"),
    ("127.0.0.1", "127.0.0.1"),
])
def test_boot_returns_effective_http_host(monkeypatch, tmp_path,
                                          configured_host, effective_host):
    class FakeContradictionStore:
        def __init__(self, path, normalizer=None, nli=None, embedder=None):
            self.retrieval = False

    monkeypatch.setenv("ERGO_DB_PATH", str(tmp_path / "ergo.db"))
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    if configured_host is None:
        monkeypatch.delenv("ERGO_HTTP_HOST", raising=False)
    else:
        monkeypatch.setenv("ERGO_HTTP_HOST", configured_host)
    monkeypatch.setattr(server, "_build_normalizer", lambda: ("norm", "Norm"))
    monkeypatch.setattr(server, "_build_nli", lambda: ("nli", "NLI"))
    monkeypatch.setattr(server, "_build_embedder", lambda: (None, "off"))
    monkeypatch.setattr(server, "ContradictionStore", FakeContradictionStore)

    assert server.boot() == effective_host


def test_main_binds_host_returned_by_boot_without_rereading_env(monkeypatch):
    bound: dict[str, object] = {}

    def fake_boot():
        monkeypatch.setenv("ERGO_HTTP_HOST", "env-host-that-must-not-bind")
        return "validated-host"

    class FakeServer:
        daemon_threads = False

        def __init__(self, address, handler):
            bound["address"] = address
            bound["handler"] = handler

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            bound["shutdown"] = True

    monkeypatch.setattr(server, "boot", fake_boot)
    monkeypatch.setattr(server, "BoundedThreadingHTTPServer", FakeServer)
    monkeypatch.setenv("ERGO_HTTP_PORT", "8123")

    server.main()

    assert bound["address"] == ("validated-host", 8123)
    assert bound["shutdown"] is True


def test_boot_retries_store_without_embedder_when_retrieval_init_fails(monkeypatch, tmp_path):
    calls = []

    class FakeContradictionStore:
        def __init__(self, path, normalizer=None, nli=None, embedder=None):
            calls.append(embedder)
            if embedder is not None:
                raise RuntimeError("vec unavailable")
            self.retrieval = False

    monkeypatch.setenv("ERGO_DB_PATH", str(tmp_path / "ergo.db"))
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    monkeypatch.setenv("ERGO_HTTP_HOST", "127.0.0.1")
    monkeypatch.setattr(server, "_build_normalizer", lambda: ("norm", "Norm"))
    monkeypatch.setattr(server, "_build_nli", lambda: ("nli", "NLI"))
    monkeypatch.setattr(server, "_build_embedder", lambda: ("embed", "Embed"))
    monkeypatch.setattr(server, "ContradictionStore", FakeContradictionStore)
    server.boot()
    assert calls == ["embed", None]
    assert server._STORE.retrieval is False
    assert server._CFG["retrieval"] is False
    assert server._CFG["embedder"].startswith("disabled (store init failed")


def test_health_response_is_an_exact_allowlist(serve):
    # No ERGO_API_TOKEN configured at all (the `_no_token` autouse fixture)
    # is the documented dev/test open-API mode: _resolve_principal() always
    # resolves to LEGACY_PRINCIPAL regardless of bearer, so /health gives
    # the FULL payload here -- ownership/ops metadata (owner, contact,
    # backup path, kill-review date, last-write time) still belongs on the
    # bearer-authed /diagnose only. Exact-set equality so a future _CFG
    # addition can't silently leak either. See test_health_full_payload_
    # requires_valid_bearer_when_token_is_configured for the split when a
    # real deployment DOES set a token.
    url = serve(FakeStore())
    body = httpx.get(f"{url}/health").json()
    assert set(body) == {"status", "db_path", "normalizer", "nli", "embedder",
                         "retrieval", "booted_at", "probe_state"}


def test_health_is_exempt_from_auth(serve, monkeypatch):
    # /health stays REACHABLE with no bearer at all (private-repo issue #187 doesn't
    # change that) -- it must not 401 -- but with a real token configured
    # and no bearer presented, the BODY is now the slim public allowlist,
    # not the full payload test_health_response_is_an_exact_allowlist pins
    # for the open-mode case above.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/health")  # no Authorization header
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "retrieval", "probe_state"}


def test_health_unauthenticated_does_not_leak_ops_metadata(serve, monkeypatch):
    # private-repo issue #187: with NO bearer at all and a real token configured, the
    # model stack + on-disk path must be gone entirely, not just omitted
    # from an allowlist check -- pin the negative space explicitly.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    body = httpx.get(f"{url}/health").json()
    assert body["status"] == "ok"
    assert body["retrieval"] is True
    assert body["probe_state"] == "has_active_claims"
    for leaked in ("db_path", "normalizer", "nli", "embedder", "booted_at"):
        assert leaked not in body


def test_health_full_payload_requires_valid_bearer_when_token_is_configured(serve, monkeypatch):
    # private-repo issue #187: a VALID bearer must still get the full payload, byte-for-
    # byte the same fields as the open-mode/unauthenticated-route case --
    # nothing renamed or dropped, just moved behind auth.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/health", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "db_path", "normalizer", "nli", "embedder",
                         "retrieval", "booted_at", "probe_state"}
    assert body["status"] == "ok"
    assert body["probe_state"] == "has_active_claims"


def test_health_invalid_bearer_is_treated_as_unauthenticated_not_401(serve, monkeypatch):
    # private-repo issue #187: an invalid/revoked bearer on /health must NOT 401 -- the
    # route must stay reachable by monitors that never rotate a bearer --
    # it just falls back to the same slim public payload as no bearer at all.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/health", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "retrieval", "probe_state"}


def test_health_bearer_with_control_plane_fault_degrades_to_slim_not_500(serve, monkeypatch):
    # Review finding on private-repo PR #191: a bearer that ISN'T the legacy token makes
    # _resolve_principal() reach into _CONTROL.resolve() -- a real per-tenant
    # control-store read with no try/except of its own (control.py's
    # resolve()). Before this issue, /health touched only the engine store,
    # and THAT probe is already wrapped so a failure degrades to a clean
    # 503. Liveness must never depend on that control store's health: a
    # fault there (locked/corrupt/mid-migration) must degrade /health to the
    # public payload, never crash the route the release gate and the
    # anti-zombie check depend on.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    fake_control = FakeControl()
    fake_control.resolve_error = RuntimeError("control store is locked")
    monkeypatch.setattr(server, "_CONTROL", fake_control)
    r = httpx.get(f"{url}/health", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 200
    assert set(r.json()) == {"status", "retrieval", "probe_state"}


def test_ready_bearer_with_control_plane_fault_degrades_to_slim_not_500(serve, monkeypatch):
    # Same fault-tolerance contract as /health above, asserted separately
    # since /ready has its own 503/degraded paths sharing the same gate.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    fake_control = FakeControl()
    fake_control.resolve_error = RuntimeError("control store is locked")
    monkeypatch.setattr(server, "_CONTROL", fake_control)
    r = httpx.get(f"{url}/ready", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 200
    assert set(r.json()) == {"status", "probe_state"}


def test_health_no_bearer_never_touches_control_plane(serve, monkeypatch):
    # The headerless polling path (Docker HEALTHCHECK, and the private
    # staging smoke test) must stay completely decoupled from the control
    # plane: _authed() (and therefore _resolve_principal()) short-circuits
    # before any bearer parsing reaches _CONTROL, so this is zero added
    # overhead/risk for the traffic pattern that actually polls this route
    # continuously.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    fake_control = FakeControl()
    monkeypatch.setattr(server, "_CONTROL", fake_control)
    r = httpx.get(f"{url}/health")  # no Authorization header
    assert r.status_code == 200
    assert fake_control.calls == []


def test_health_503_when_store_probe_fails(serve):
    store = FakeStore()
    store.raise_on = {"active"}
    url = serve(store)
    r = httpx.get(f"{url}/health")
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"


def test_head_health_ok_empty_body_and_matching_headers(serve):
    # UptimeRobot-style monitors probe with HEAD and require 2xx -- the
    # stdlib http.server default (no do_HEAD) 501s this, which is the bug.
    url = serve(FakeStore())
    r = httpx.head(f"{url}/health")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["Content-Type"] == "application/json"
    # Content-Length must reflect what a GET would have sent (RFC 9110
    # §9.3.2), even though HEAD writes no body bytes.
    get_body = httpx.get(f"{url}/health").content
    assert r.headers["Content-Length"] == str(len(get_body))


def test_head_health_is_exempt_from_auth(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.head(f"{url}/health")  # no Authorization header
    assert r.status_code == 200


def test_head_health_unauthenticated_matches_slim_get_body(serve, monkeypatch):
    # private-repo issue #187: do_HEAD shares _health_status with the GET path -- an
    # unauthenticated HEAD must advertise the Content-Length of the SLIM
    # body it would actually send, not the (now bigger, now smaller
    # depending on field lengths) full authed body.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.head(f"{url}/health")  # no Authorization header
    assert r.status_code == 200
    assert r.content == b""
    unauthed_get_body = httpx.get(f"{url}/health").content
    assert r.headers["Content-Length"] == str(len(unauthed_get_body))
    authed_get_body = httpx.get(f"{url}/health",
                                headers={"Authorization": "Bearer secret"}).content
    assert r.headers["Content-Length"] != str(len(authed_get_body))


def test_head_health_503_when_store_probe_fails(serve):
    store = FakeStore()
    store.raise_on = {"active"}
    url = serve(store)
    r = httpx.head(f"{url}/health")
    assert r.status_code == 503
    assert r.content == b""


def test_head_other_path_is_405_with_allow_header(serve):
    url = serve(FakeStore())
    r = httpx.head(f"{url}/recall")
    assert r.status_code == 405
    assert r.content == b""
    assert r.headers["Allow"] == "GET, POST"


# --------------------------------------------------------------------------
# /ready (also always open) -- write-path readiness (closes the gap where
# /health never probes the normalizer, so a provider outage leaves /health
# green while every guarded write 503s)
# --------------------------------------------------------------------------
def test_ready_ok_when_normalizer_healthy(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["normalizer"] == {"ok": True}
    assert body["probe_state"] == "has_active_claims"


def test_ready_503_when_normalizer_unavailable(serve):
    store = FakeStore()
    store.normalizer.unavailable_error = NormalizeUnavailable("boom", retryable=True)
    url = serve(store)
    r = httpx.get(f"{url}/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["normalizer"]["ok"] is False
    assert body["normalizer"]["retryable"] is True
    assert "boom" in body["normalizer"]["error"]


def test_ready_503_when_store_probe_fails(serve):
    store = FakeStore()
    store.raise_on = {"active"}
    url = serve(store)
    r = httpx.get(f"{url}/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"
    # the DB probe fails before the normalizer is ever consulted
    assert store.normalizer.calls == []


def test_ready_is_unauthenticated(serve, monkeypatch):
    # /ready stays REACHABLE with no bearer -- private-repo issue #187 doesn't 401 it --
    # but with a real token configured, the healthy-path body is now the
    # slim public allowlist, not the full normalizer-probe detail.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/ready")  # no Authorization header
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "probe_state"}
    assert body["status"] == "ready"


def test_ready_full_payload_requires_valid_bearer_when_token_is_configured(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/ready", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "normalizer", "probe_state", "checked_at", "cached"}


def test_ready_degraded_unauthenticated_hides_normalizer_detail(serve, monkeypatch):
    # private-repo issue #187: /ready's degraded payload discloses normalizer/provider
    # detail -- including raw error strings -- so an unauthenticated caller
    # must get status+probe_state only, still a 503, nothing else.
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    store = FakeStore()
    store.normalizer.unavailable_error = NormalizeUnavailable("boom", retryable=True)
    url = serve(store)
    r = httpx.get(f"{url}/ready")  # no Authorization header
    assert r.status_code == 503
    body = r.json()
    assert set(body) == {"status", "probe_state"}
    assert body["status"] == "degraded"
    assert "boom" not in json.dumps(body)


def test_ready_degraded_with_valid_bearer_keeps_full_detail(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    store = FakeStore()
    store.normalizer.unavailable_error = NormalizeUnavailable("boom", retryable=True)
    url = serve(store)
    r = httpx.get(f"{url}/ready", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 503
    body = r.json()
    assert set(body) == {"status", "normalizer", "probe_state", "checked_at", "cached"}
    assert body["normalizer"]["ok"] is False
    assert "boom" in body["normalizer"]["error"]


def test_ready_invalid_bearer_is_treated_as_unauthenticated_not_401(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/ready", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 200
    assert set(r.json()) == {"status", "probe_state"}


def test_head_ready_ok(serve):
    url = serve(FakeStore())
    # Warm the TTL cache first so the HEAD call below and the GET call used
    # to compute the expected Content-Length both land on the SAME cached
    # ("cached": true) payload -- otherwise the flip from cached=false to
    # cached=true between two calls would (correctly) change the body's
    # byte length and make this comparison flaky by construction.
    httpx.get(f"{url}/ready")
    r = httpx.head(f"{url}/ready")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["Content-Type"] == "application/json"
    get_body = httpx.get(f"{url}/ready").content
    assert r.headers["Content-Length"] == str(len(get_body))


def test_ready_caches_probe_within_ttl(serve):
    # default TTL (ERGO_READY_PROBE_TTL_SECONDS unset -> 15s): two GETs in
    # quick succession must trigger at most ONE real normalize() call.
    store = FakeStore()
    url = serve(store)
    first = httpx.get(f"{url}/ready").json()
    second = httpx.get(f"{url}/ready").json()
    assert len(store.normalizer.calls) == 1
    assert first["cached"] is False
    assert second["cached"] is True
    # A cached hit must report the ORIGINAL probe's wall-clock time, not
    # "now" -- otherwise a monitor reading checked_at alone (without also
    # cross-referencing cached) would believe the write path was just
    # verified when the real probe could be up to the TTL stale.
    assert second["checked_at"] == first["checked_at"]


def test_ready_reprobes_when_ttl_zero(serve, monkeypatch):
    monkeypatch.setenv("ERGO_READY_PROBE_TTL_SECONDS", "0")
    store = FakeStore()
    url = serve(store)
    first = httpx.get(f"{url}/ready").json()
    second = httpx.get(f"{url}/ready").json()
    assert len(store.normalizer.calls) == 2
    assert first["cached"] is False
    assert second["cached"] is False
    assert first["checked_at"] is not None
    assert second["checked_at"] is not None


def test_ready_probe_cache_ttl_zero_never_rides_a_concurrent_inflight_probe():
    # Review finding on private-repo PR #115: ERGO_READY_PROBE_TTL_SECONDS=0 documents
    # "probe every hit" -- that promise must hold even when another call is
    # already mid-probe. A caller who disabled the cache is opting OUT of
    # the single-flight dedup too, not just the TTL window; a concurrent
    # call must do its OWN independent probe rather than quietly riding the
    # in-flight call's (soon to be stale-by-definition-here) result.
    cache = server._ReadyProbeCache()
    normalizer = FakeNormalizer()
    gate = threading.Event()
    normalizer.gate = gate

    leader_out: dict = {}

    def run_leader():
        leader_out["result"] = cache.probe(normalizer, 0)

    leader_thread = threading.Thread(target=run_leader)
    leader_thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(normalizer.calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(normalizer.calls) == 1  # the leader is now stuck inside normalize()

        # ttl_seconds<=0 must never block on the leader's in-flight call --
        # it does its own probe, which would also stall on the still-closed
        # gate, so open it first (this second call is what's under test:
        # it must NOT short-circuit to the leader's stale/in-flight state).
        gate.set()
        second_result, second_cached, _ = cache.probe(normalizer, 0)
    finally:
        gate.set()
        leader_thread.join(timeout=5.0)

    assert len(normalizer.calls) == 2  # BOTH calls hit the normalizer independently
    assert second_cached is False
    leader_result, leader_cached, _ = leader_out["result"]
    assert leader_cached is False


def test_ready_probe_cache_follower_does_not_block_on_slow_inflight_probe():
    # Single-flight design (review finding on private-repo PR #115): _ReadyProbeCache must
    # never hold its lock across the real normalize() call, so a follower
    # thread that arrives while another thread is already inside a slow
    # (or hanging) probe rides the LAST KNOWN result instead of queuing
    # behind it -- otherwise a burst of monitor traffic during exactly the
    # outage /ready exists to detect would pin one worker thread per
    # request for the whole stall, defeating the readiness probe.
    #
    # Exercised directly against `_ReadyProbeCache` (not through the HTTP
    # layer): the `serve` fixture's test harness runs a plain, single-
    # threaded `http.server.HTTPServer` (unlike the real
    # `BoundedThreadingHTTPServer`), so it cannot accept a second
    # connection while the first request's handler is still running --
    # this class is where the actual concurrency contract lives.
    cache = server._ReadyProbeCache()
    normalizer = FakeNormalizer()

    # Use a short but strictly positive TTL (single-flight coordination is
    # gated on ttl_seconds > 0 -- see the separate review finding that
    # ttl_seconds<=0 must skip it entirely and always probe independently,
    # so exercising this with ttl=0 would be testing the wrong branch).
    ttl = 1

    # Warm the cache with one fast, successful probe first so there IS a
    # last-known result for a follower to fall back on.
    first_result, first_cached, first_checked_at = cache.probe(normalizer, ttl)
    assert first_cached is False
    assert len(normalizer.calls) == 1

    time.sleep(ttl + 0.1)  # let that result go stale so the next call re-probes

    gate = threading.Event()
    normalizer.gate = gate
    leader_out: dict = {}

    def run_leader():
        leader_out["result"] = cache.probe(normalizer, ttl)

    leader_thread = threading.Thread(target=run_leader)
    leader_thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(normalizer.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(normalizer.calls) == 2  # the leader is now stuck inside normalize()

        start = time.monotonic()
        follower_result, follower_cached, follower_checked_at = cache.probe(normalizer, ttl)
        elapsed = time.monotonic() - start
    finally:
        gate.set()
        leader_thread.join(timeout=5.0)

    assert elapsed < 1.0  # never blocked on the leader's stalled call
    assert follower_cached is True
    assert follower_result == first_result
    assert follower_checked_at == first_checked_at
    # the follower rode the cache -- it never triggered a THIRD normalize() call
    assert len(normalizer.calls) == 2
    leader_result, leader_cached, _ = leader_out["result"]
    assert leader_cached is False  # the leader's own probe did land


def test_ready_probe_cache_follower_reraises_leaders_error_on_first_probe():
    # Review finding on private-repo PR #115: `_probe_normalizer` only catches
    # `NormalizeUnavailable` -- any OTHER exception (a genuine normalizer
    # bug) propagates out of the leader's probe() call. On the very first
    # probe since boot, a follower waiting BEHIND that leader (on the
    # `event.wait()` path -- there is no prior cached result yet to fall
    # back on) must get the SAME exception, not a bare `TypeError` from
    # indexing into a `None` result for a request that never even touched
    # the normalizer.
    cache = server._ReadyProbeCache()
    normalizer = FakeNormalizer()
    gate = threading.Event()

    class _Boom(Exception):
        pass

    def boom_normalize(record):
        normalizer.calls.append(record)
        gate.wait(timeout=5.0)
        raise _Boom("normalizer exploded")

    normalizer.normalize = boom_normalize

    leader_out: dict = {}
    follower_out: dict = {}

    def run_leader():
        try:
            cache.probe(normalizer, 15)
        except Exception as e:  # noqa: BLE001 -- captured for the assertion below
            leader_out["error"] = e

    def run_follower():
        try:
            cache.probe(normalizer, 15)
        except Exception as e:  # noqa: BLE001
            follower_out["error"] = e

    leader_thread = threading.Thread(target=run_leader)
    leader_thread.start()
    deadline = time.monotonic() + 5.0
    while len(normalizer.calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(normalizer.calls) == 1  # the leader is now stuck inside normalize()

    follower_thread = threading.Thread(target=run_follower)
    follower_thread.start()
    time.sleep(0.1)  # give the follower time to land on event.wait() behind the leader

    gate.set()
    leader_thread.join(timeout=5.0)
    follower_thread.join(timeout=5.0)

    assert isinstance(leader_out.get("error"), _Boom)  # the leader's own call raised
    assert isinstance(follower_out.get("error"), _Boom)  # the follower got the SAME error
    assert len(normalizer.calls) == 1  # the follower never triggered a second normalize() call


def test_ready_probe_cache_first_probe_follower_wait_is_bounded(monkeypatch):
    # Review finding on private-repo PR #115 ("Unbounded wait on first probe"): on the
    # very first probe since boot (no cached result yet to fall back on),
    # a follower's event.wait() used to have NO timeout -- bounded only by
    # the normalizer's own 60-120s request timeout. During a startup
    # window with a hanging provider, that pins a worker thread for the
    # whole span, and BoundedThreadingHTTPServer's pool is finite
    # (ERGO_HTTP_MAX_WORKERS) -- exactly when a monitor needs a fast red
    # /ready. The follower must instead give up after a SHORT budget and
    # return a controlled "not ready yet" result.
    monkeypatch.setenv("ERGO_READY_FIRST_PROBE_WAIT_SECONDS", "0.2")
    cache = server._ReadyProbeCache()
    normalizer = FakeNormalizer()
    gate = threading.Event()  # never set during this test -- the leader hangs "forever"
    normalizer.gate = gate

    leader_out: dict = {}

    def run_leader():
        leader_out["result"] = cache.probe(normalizer, 15)

    leader_thread = threading.Thread(target=run_leader)
    leader_thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while len(normalizer.calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(normalizer.calls) == 1  # the leader is now stuck inside normalize()

        start = time.monotonic()
        follower_result, follower_cached, follower_checked_at = cache.probe(normalizer, 15)
        elapsed = time.monotonic() - start
    finally:
        gate.set()  # release the leader so the thread can be joined and cleaned up
        leader_thread.join(timeout=5.0)

    # gave up close to the configured budget, nowhere near a 60-120s hang
    assert elapsed < 2.0
    assert follower_cached is False
    assert follower_checked_at is None
    assert follower_result["ok"] is False
    assert follower_result["retryable"] is True
    assert "progress" in follower_result["error"]
    # the follower never itself called the normalizer
    assert len(normalizer.calls) == 1
    # the leader's own (eventually-released) probe still lands normally
    leader_result, leader_cached, _ = leader_out["result"]
    assert leader_cached is False
    assert leader_result["ok"] is True


def test_ready_response_is_an_exact_allowlist(serve):
    # No ERGO_API_TOKEN configured (the `_no_token` autouse fixture) is the
    # open dev/test mode -- same decision as /health's allowlist test: the
    # full payload is what an open deployment always gets, bearer or not.
    # exact-set equality so a future addition can't silently leak anything.
    url = serve(FakeStore())
    body = httpx.get(f"{url}/ready").json()
    assert set(body) == {"status", "normalizer", "probe_state", "checked_at", "cached"}


def test_health_and_ready_full_payload_in_open_mode_with_no_token_configured(serve):
    # private-repo issue #187's explicit open-mode decision, pinned in one place: with NO
    # ERGO_API_TOKEN configured at all (documented dev/test mode), every
    # OTHER route already runs fully open (_authed() returns True
    # unconditionally), so /health and /ready follow the same posture --
    # the full payload, not the slim one -- rather than inventing a
    # DIFFERENT "authenticated" meaning just for these two routes. A real
    # deployment always sets ERGO_API_TOKEN, so this can't leak in prod.
    url = serve(FakeStore())
    health_body = httpx.get(f"{url}/health").json()
    assert set(health_body) == {"status", "db_path", "normalizer", "nli",
                                "embedder", "retrieval", "booted_at", "probe_state"}
    ready_body = httpx.get(f"{url}/ready").json()
    assert set(ready_body) == {"status", "normalizer", "probe_state", "checked_at", "cached"}


def test_guarded_get_401_without_bearer(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    store = FakeStore()
    url = serve(store)
    r = httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"})
    assert r.status_code == 401
    assert ("active", "o", "p") not in store.calls  # never reached the store


def test_guarded_get_401_with_wrong_bearer(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"},
                  headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_guarded_get_200_with_correct_bearer(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    url = serve(FakeStore())
    r = httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"},
                  headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200


def test_authed_uses_constant_time_compare(monkeypatch):
    # Finding #9: hdr == f"Bearer {token}" short-circuits on the first differing
    # byte, leaking token-prefix timing. Pin that the comparison goes through
    # hmac.compare_digest instead of == (behavior for callers is unchanged).
    import inspect

    from ergo.contradiction import server

    src = inspect.getsource(server.Handler._authed)
    assert "hmac.compare_digest" in src
    assert "hdr ==" not in src


def test_post_401_without_token_does_not_touch_store(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w", "statement": "s"})
    assert r.status_code == 401
    assert not any(c[0] == "remember" for c in store.calls)


def test_open_server_allows_guarded_route_without_header(serve):
    url = serve(FakeStore())  # no token in env
    r = httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# GET routes: validation, serialization, retrieval gating, errors
# --------------------------------------------------------------------------
def test_active_requires_org_id(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/active", params={})  # missing org_id
    assert r.status_code == 400
    assert "org_id required" in r.json()["error"]


def test_active_missing_project_resolves_to_default_with_warning(serve):
    # DEFAULT-PROJECT (private-repo issue #179): `project` is no longer required -- an
    # omitted project resolves via _resolve_project (no key default here,
    # since this open/legacy-mode request has no real token) instead of
    # 400ing, and the fallback carries a warning.
    store = FakeStore()
    url = serve(store)
    r = httpx.get(f"{url}/active", params={"org_id": "o"})  # missing project
    assert r.status_code == 200
    # private-repo issue #185: the route always passes an explicit limit/offset now (the
    # bounded-page default), never the bare 3-tuple call of before.
    assert store.calls[-1] == ("active", "o", "default", server.DEFAULT_ACTIVE_LIMIT, 0, "full")
    assert any("default_project" in w for w in r.json()["warnings"])


def test_active_returns_serialized_set(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "has_active_claims"
    assert body["claims"] == [{"id": "c1", "statement": "x"}]


def test_active_omitted_limit_uses_default_and_zero_offset(serve):
    # private-repo issue #185: no `limit`/`offset` in the request -> the route applies
    # DEFAULT_ACTIVE_LIMIT and offset 0, mirroring /audit's own default-fill.
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"})
    assert store.calls[-1] == ("active", "o", "p", server.DEFAULT_ACTIVE_LIMIT, 0, "full")


def test_active_custom_limit_and_offset_passthrough(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p",
                                       "limit": "5", "offset": "10"})
    assert store.calls[-1] == ("active", "o", "p", 5, 10, "full")


def test_active_limit_above_max_is_clamped(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p",
                                       "limit": str(server.MAX_ACTIVE_LIMIT + 500)})
    assert store.calls[-1] == ("active", "o", "p", server.MAX_ACTIVE_LIMIT, 0, "full")


def test_active_garbage_limit_degrades_to_default(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p",
                                       "limit": "not-a-number"})
    assert store.calls[-1] == ("active", "o", "p", server.DEFAULT_ACTIVE_LIMIT, 0, "full")


def test_active_omitted_projection_defaults_to_full(serve):
    # private-repo issue #206: no `projection` in the request -> the route passes "full"
    # through explicitly, same default-fill shape as limit/offset.
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"})
    assert store.calls[-1] == ("active", "o", "p", server.DEFAULT_ACTIVE_LIMIT, 0, "full")


def test_active_projection_summary_passthrough(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p",
                                       "projection": "summary"})
    assert store.calls[-1] == ("active", "o", "p", server.DEFAULT_ACTIVE_LIMIT, 0, "summary")


def test_active_invalid_projection_degrades_to_full(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/active", params={"org_id": "o", "project": "p",
                                       "projection": "not-a-real-projection"})
    assert store.calls[-1] == ("active", "o", "p", server.DEFAULT_ACTIVE_LIMIT, 0, "full")


def test_active_response_carries_pagination_fields(serve):
    store = FakeStore()
    store.active_claims = [{"id": "c1", "statement": "x"}]
    url = serve(store)
    r = httpx.get(f"{url}/active", params={"org_id": "o", "project": "p"})
    body = r.json()
    # FakeStore.active() returns a real ActiveSet, so the route's _ser() must
    # carry ALL of its fields through, not just state/claims.
    assert set(body) >= {"state", "count", "returned", "truncated", "next_offset", "claims"}


def test_history_wraps_list_under_history_key(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/history", params={"org_id": "o", "project": "p"})
    assert r.status_code == 200
    assert r.json()["history"] == [{"id": "c1", "seq": 1}]


def test_audit_requires_org_id(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/audit", params={})  # missing org_id
    assert r.status_code == 400
    assert "org_id required" in r.json()["error"]


def test_audit_missing_project_resolves_to_default_with_warning(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.get(f"{url}/audit", params={"org_id": "o"})  # missing project
    assert r.status_code == 200
    assert store.calls[-1] == ("audit_log", "o", "default", server.DEFAULT_AUDIT_LIMIT)
    assert any("default_project" in w for w in r.json()["warnings"])


def test_audit_returns_writes_and_conflict_events(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/audit", params={"org_id": "o", "project": "p"})
    assert r.status_code == 200
    body = r.json()
    assert body["writes"] == [{"id": 1, "org_id": "o", "project": "p"}]
    assert body["conflict_events"] == [{"id": 1, "org_id": "o", "project": "p"}]


def test_audit_default_limit_is_200(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/audit", params={"org_id": "o", "project": "p"})
    assert store.calls[-1] == ("audit_log", "o", "p", 200)


def test_audit_limit_clamped_to_max(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/audit", params={"org_id": "o", "project": "p", "limit": "999999"})
    assert store.calls[-1] == ("audit_log", "o", "p", 1000)


def test_audit_limit_clamped_to_1_min(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/audit", params={"org_id": "o", "project": "p", "limit": "0"})
    assert store.calls[-1] == ("audit_log", "o", "p", 1)


def test_audit_limit_defaults_on_non_integer(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/audit", params={"org_id": "o", "project": "p", "limit": "nope"})
    assert store.calls[-1] == ("audit_log", "o", "p", 200)


def test_recall_requires_query(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p"})
    assert r.status_code == 400
    assert "q (query) required" in r.json()["error"]


def test_recall_returns_results(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["results"] == [{"id": "c1", "score": 0.5}]


def test_recall_limit_clamped_to_50_max(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/recall",
              params={"org_id": "o", "project": "p", "q": "hi", "limit": "999"})
    assert store.recall_limit == 50


def test_recall_limit_clamped_to_1_min(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/recall",
              params={"org_id": "o", "project": "p", "q": "hi", "limit": "0"})
    assert store.recall_limit == 1


def test_recall_limit_defaults_on_non_integer(serve):
    store = FakeStore()
    url = serve(store)
    httpx.get(f"{url}/recall",
              params={"org_id": "o", "project": "p", "q": "hi", "limit": "abc"})
    assert store.recall_limit == 10


def test_recall_503_when_retrieval_disabled(serve):
    url = serve(FakeStore(retrieval=False))
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 503
    assert "retrieval not enabled" in r.json()["error"]


def test_recall_500_on_store_error(serve):
    store = FakeStore()
    store.raise_on = {"recall"}
    url = serve(store)
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 500
    assert "boom in recall" in r.json()["error"]


# --------------------------------------------------------------------------
# scope echo on /recall and /why (private-repo issue #184): an empty result is otherwise
# indistinguishable from "this project doesn't exist" -- see SCOPE-VISIBILITY
# HTTP-API-#184.
# --------------------------------------------------------------------------
def test_recall_scope_reports_project_missing_when_empty_and_no_such_project(serve):
    # THE BUG: before this fix, `/recall` returning `[]` here was byte-
    # identical to a project that exists and just has nothing relevant.
    store = FakeStore()
    store.recall_result = []
    url = serve(store)  # "p" was never created -- no row, no claims
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["results"] == []
    assert r.json()["scope"] == {"project": "p", "source": "explicit",
                                 "state": "project_missing"}


def test_recall_scope_reports_empty_when_project_exists_with_no_active_claims(serve):
    store = FakeStore()
    store.recall_result = []
    store.projects_with_rows = {"p"}  # project is real, but genuinely no active claims
    url = serve(store)
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["scope"] == {"project": "p", "source": "explicit",
                                 "state": "empty"}


def test_recall_scope_reports_has_active_claims_on_a_hit_without_extra_query(serve):
    # A non-empty result already proves has_active_claims -- the hot path
    # must not pay for project_state() at all.
    store = FakeStore()  # recall_result defaults to a hit
    url = serve(store)
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["scope"] == {"project": "p", "source": "explicit",
                                 "state": "has_active_claims"}
    assert not any(c[0] == "project_state" for c in store.calls)


def test_recall_scope_reports_has_active_claims_when_empty_result_but_project_is_full(serve):
    # FOLLOW-UP REGRESSION (mechanism 2, orchestrator finding on private-repo PR #188): a
    # project can hold plenty of active claims while THIS query's own
    # `recall()` result comes back empty (real cause: the vec index is
    # global and the nearest-k can all belong to another project). The
    # empty RESULT SET must never be conflated with an empty PROJECT --
    # `scope.state` has to reflect the store's actual claim state, not just
    # `recall_result`.
    store = FakeStore()
    store.recall_result = []  # this query matched nothing
    store.projects_with_active_claims = {"p"}  # but the project is NOT empty
    url = serve(store)
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["results"] == []
    assert r.json()["scope"] == {"project": "p", "source": "explicit",
                                 "state": "has_active_claims"}


def test_recall_scope_source_is_fallback_when_project_omitted(serve):
    store = FakeStore()
    store.recall_result = []
    url = serve(store)
    r = httpx.get(f"{url}/recall", params={"org_id": "o", "q": "hi"})  # no project
    assert r.status_code == 200
    body = r.json()
    assert body["scope"]["source"] == "fallback"
    assert body["scope"]["project"] == "default"
    assert body["scope"]["state"] == "project_missing"
    assert any("default_project" in w for w in body["warnings"])


def test_why_accepts_query_alias_and_returns_why_key(serve):
    url = serve(FakeStore())
    # the route accepts ?query= as an alias for ?q=
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "query": "hi"})
    assert r.status_code == 200
    assert r.json()["why"] == {"id": "c1", "reason": "because"}


def test_why_503_when_retrieval_disabled(serve):
    url = serve(FakeStore(retrieval=False))
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 503


def test_why_scope_reports_project_missing_on_null_why(serve):
    store = FakeStore()
    store.why_result = None
    url = serve(store)  # "typo-project" never created
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "typo-project", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["why"] is None
    assert r.json()["scope"] == {"project": "typo-project", "source": "explicit",
                                 "state": "project_missing"}


def test_why_scope_reports_empty_when_project_exists_with_no_active_claims(serve):
    store = FakeStore()
    store.why_result = None
    store.projects_with_rows = {"p"}
    url = serve(store)
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["scope"]["state"] == "empty"


def test_why_scope_reports_has_active_claims_when_a_reasoned_belief_is_found(serve):
    store = FakeStore()  # why_result defaults to a hit
    url = serve(store)
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["scope"]["state"] == "has_active_claims"
    assert r.json()["scope"]["source"] == "explicit"
    assert not any(c[0] == "project_state" for c in store.calls)


def test_why_scope_reports_has_active_claims_when_null_but_project_holds_only_facts(serve):
    # FOLLOW-UP REGRESSION (mechanism 1, orchestrator finding on private-repo PR #188):
    # `store.why()` is reasoned_only by design ("why searches reasoned
    # entries only" -- a documented invariant, not a bug), so a project
    # holding nothing but reason-less /ingest facts returns `why: null`
    # EVERY time, forever. That must never be reported as `state: "empty"`
    # -- the project is not empty, this query just structurally can't match
    # anything in it.
    store = FakeStore()
    store.why_result = None
    store.projects_with_active_claims = {"p"}  # 170 reason-less facts, say
    url = serve(store)
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    assert r.json()["why"] is None
    assert r.json()["scope"]["state"] == "has_active_claims"


# --------------------------------------------------------------------------
# Relevance floor (private-repo issue #202): a suppressed answer must be visibly
# suppressed, never indistinguishable from a genuine "nothing here".
# --------------------------------------------------------------------------
def test_why_suppressed_below_floor_reports_null_with_suppression_detail(serve):
    store = FakeStore()
    store.why_result = {"suppressed": {"reason": "below_relevance_floor",
                                        "score": 0.4511, "floor": 0.5}}
    store.projects_with_active_claims = {"p"}
    url = serve(store)
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["why"] is None
    assert body["suppressed"] == {"reason": "below_relevance_floor",
                                   "score": 0.4511, "floor": 0.5}
    # a suppressed answer still came from a real claim in scope, not an
    # empty project -- state must say so, same as a genuine match would.
    assert body["scope"] == {"project": "p", "source": "explicit",
                             "state": "has_active_claims"}


def test_why_no_reasoned_claims_at_all_is_distinguishable_from_suppressed(serve):
    store = FakeStore()
    store.why_result = None  # genuinely nothing reasoned in scope
    store.projects_with_rows = {"p"}
    url = serve(store)
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["why"] is None
    assert "suppressed" not in body
    assert body["scope"]["state"] == "empty"


def test_why_above_floor_match_keeps_score_and_omits_suppressed_key(serve):
    store = FakeStore()  # why_result defaults to a real hit, no "suppressed" key
    url = serve(store)
    r = httpx.get(f"{url}/why", params={"org_id": "o", "project": "p", "q": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["why"] == {"id": "c1", "reason": "because"}
    assert "suppressed" not in body
    assert body["scope"] == {"project": "p", "source": "explicit",
                             "state": "has_active_claims"}


def test_unknown_get_path_404(serve):
    url = serve(FakeStore())
    r = httpx.get(f"{url}/nope")
    assert r.status_code == 404
    assert r.json()["path"] == "/nope"


# --------------------------------------------------------------------------
# POST /remember
# --------------------------------------------------------------------------
def test_remember_missing_statement_is_400(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w"})
    assert r.status_code == 400
    assert r.json()["error"] == "missing field: statement"


@pytest.mark.parametrize("path", ["/remember", "/learn", "/supersede"])
def test_whitespace_only_statement_is_400_not_500(serve, path):
    """A whitespace-only statement is truthy, so it passed the missing-field
    check and reached the store, dying on the SQLite
    CHECK(length(trim(statement)) > 0) as a 500 that leaked schema internals.
    Every guarded write that inserts a claim must reject it as a clean 400,
    before touching the store."""
    store = FakeStore()
    url = serve(store)
    body = {"org_id": "o", "project": "p", "who": "w", "statement": "   "}
    if path == "/learn":
        body["source"] = "src.py:1"
    if path == "/supersede":
        body["existing_id"] = "old-1"
    r = httpx.post(f"{url}{path}", json=body)
    assert r.status_code == 400
    assert "blank" in r.json()["error"]
    assert store.calls == []  # rejected before the store is ever called


def test_remember_stored_returns_200_and_sets_last_write(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "deploys must use a canary", "reason": "safety"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "stored"
    assert body["claim_id"] == "new-id"
    # reason forwarded to the store
    assert store.calls[-1] == ("remember", "o", "p", "w",
                               "deploys must use a canary", "safety", None)
    # last_write_at stamped on the live config
    assert server._CFG["last_write_at"] is not None


def test_remember_conflict_returns_409(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "this triggers a conflict"})
    assert r.status_code == 409
    body = r.json()
    assert body["result"] == "conflict"
    assert body["stored"] is False
    assert body["conflicts"][0]["tier"] == "block"


def test_remember_forwards_force_exception(serve):
    store = FakeStore()
    url = serve(store)
    httpx.post(f"{url}/remember",
               json={"org_id": "o", "project": "p", "who": "w",
                     "statement": "ok", "force_exception": "approved by Alex"})
    assert store.calls[-1][-1] == "approved by Alex"


def test_remember_500_on_store_error(serve):
    store = FakeStore()
    store.raise_on = {"remember"}
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w", "statement": "s"})
    assert r.status_code == 500
    assert "boom in remember" in r.json()["error"]


def test_remember_oversized_statement_is_413_not_500(serve, monkeypatch):
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", "10")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "x" * 50, "reason": "r"})
    assert r.status_code == 413
    assert store.calls == []


def test_remember_oversized_reason_is_413(serve, monkeypatch):
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", "10")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "s", "reason": "y" * 50})
    assert r.status_code == 413
    assert store.calls == []


# --------------------------------------------------------------------------
# finding #2: request body / field size caps (413/400, never a hang or
# a 500).
# --------------------------------------------------------------------------
def test_oversized_body_is_413_not_read_into_memory(serve, monkeypatch):
    monkeypatch.setenv("ERGO_MAX_BODY_BYTES", "64")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "x" * 200, "reason": "r"})
    assert r.status_code == 413
    assert store.calls == []


def test_garbage_content_length_is_400_not_500(serve):
    url = serve(FakeStore())
    u = urlparse(url)
    s = socket.create_connection((u.hostname, u.port), timeout=3)
    body = b'{"org_id":"o","project":"p","who":"w","statement":"s","reason":"r"}'
    s.sendall(b"POST /remember HTTP/1.1\r\nHost: t\r\n"
              b"Content-Type: application/json\r\nContent-Length: abc\r\n\r\n" + body)
    resp = s.recv(4096)
    s.close()
    assert b" 400 " in resp.split(b"\r\n", 1)[0], resp[:60]


def test_negative_content_length_is_rejected_before_blocking_on_read(serve, monkeypatch):
    """Content-Length: -1 makes stdlib's rfile.read(-1) read-until-EOF; a
    client that never closes the connection used to tie up the handler
    thread (and its semaphore permit) for the full socket timeout instead of
    a fast 400. Prove the fix rejects a negative Content-Length BEFORE ever
    calling rfile.read()."""
    monkeypatch.setenv("ERGO_HTTP_TIMEOUT", "5")
    url = serve(FakeStore())
    u = urlparse(url)
    s = socket.create_connection((u.hostname, u.port), timeout=5)
    s.sendall(b"POST /remember HTTP/1.1\r\nHost: t\r\n"
              b"Content-Type: application/json\r\nContent-Length: -1\r\n\r\n")
    start = time.monotonic()
    resp = s.recv(4096)
    elapsed = time.monotonic() - start
    s.close()
    assert b" 400 " in resp.split(b"\r\n", 1)[0], resp[:60]
    assert elapsed < 1.0, (
        f"negative Content-Length blocked the handler for {elapsed:.2f}s "
        "instead of being rejected immediately")


def test_recall_oversized_query_is_413(serve, monkeypatch):
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", "10")
    store = FakeStore()
    url = serve(store)
    r = httpx.get(f"{url}/recall",
                  params={"org_id": "o", "project": "p", "q": "x" * 50})
    assert r.status_code == 413
    assert not any(c[0] == "recall" for c in store.calls)


def test_ingest_over_byte_cap_is_413(serve, monkeypatch):
    monkeypatch.setenv("ERGO_MAX_INGEST_BYTES", "64")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "text": "x" * 500})
    assert r.status_code == 413
    assert store.calls == []


def test_ingest_over_chunk_cap_is_413(serve, monkeypatch):
    from ergo.contradiction import ingest as ingest_mod

    monkeypatch.setenv("ERGO_MAX_INGEST_CHUNKS", "2")
    monkeypatch.setattr(ingest_mod, "chunk_text",
                        lambda text, *a, **kw: ["c1", "c2", "c3"])
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "text": "irrelevant, chunk_text is mocked"})
    assert r.status_code == 413
    assert store.calls == []


def test_ingest_oversized_source_is_413(serve, monkeypatch):
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", "10")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "text": "a fact worth keeping around.",
                         "source": "y" * 50})
    assert r.status_code == 413
    assert store.calls == []


def test_remember_normalize_unavailable_returns_503_not_500(serve):
    """Provider infra failure (finding #3) is a
    DISTINCT, deliberate degraded-service refusal — 503, not the generic 500
    that leaks internals."""
    store = FakeStore()
    store.remember_error = NormalizeUnavailable("connection refused")
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w", "statement": "s"})
    assert r.status_code == 503
    body = r.json()
    assert body["result"] == "degraded"
    assert "connection refused" in body["error"]


def test_remember_permanent_normalize_unavailable_returns_502_not_503(serve):
    """re-review #3: a PERMANENT provider 4xx (400/404/422, e.g. a bad
    model name) is a distinct, NON-retryable fail-closed refusal — 502, not
    the retryable 503 (that would suggest a caller should just retry, which
    never helps a permanent rejection) and not the fail-open behavior this
    used to have."""
    store = FakeStore()
    store.remember_error = NormalizeUnavailable("404 model not found", retryable=False)
    url = serve(store)
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w", "statement": "s"})
    assert r.status_code == 502
    body = r.json()
    assert body["result"] == "degraded"
    assert "404 model not found" in body["error"]


def test_post_bad_json_is_400(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/remember", content=b"{not valid json",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "bad json" in r.json()["error"]


@pytest.mark.parametrize("raw", [b"[1,2,3]", b"null", b'"x"', b"42", b"true"])
def test_post_non_object_json_is_400_not_crash(serve, raw):
    # Valid JSON that is not an object used to escape do_POST as an
    # AttributeError (body.get on a list/None/...), closing the socket with
    # no response at all. The contract is a clean 400.
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/remember", content=raw,
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "bad json" in r.json()["error"]
    assert store.calls == []


@pytest.mark.parametrize("path", ["/learn", "/ingest", "/supersede", "/retract"])
def test_post_non_object_json_is_400_on_all_routes(serve, path):
    url = serve(FakeStore())
    r = httpx.post(f"{url}{path}", content=b"[1,2,3]",
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "bad json" in r.json()["error"]


# --------------------------------------------------------------------------
# POST /learn  (guarded write + source provenance — the ingest->memory bridge)
# --------------------------------------------------------------------------
def test_learn_missing_source_is_400(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "a belief"})  # no source
    assert r.status_code == 400
    assert r.json()["error"] == "missing field: source"


def test_learn_stored_returns_200_and_echoes_source(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "sessions are not reused",
                         "source": "sessions.py:847", "reason": "observed"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "learned"
    assert body["claim_id"] == "learned-id"
    assert body["source"] == "sessions.py:847"
    # source + reason forwarded to the store.learn call
    assert store.calls[-1] == ("learn", "o", "p", "w", "sessions are not reused",
                               "sessions.py:847", "observed", None)
    assert server._CFG["last_write_at"] is not None


def test_learn_conflict_returns_409(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "this triggers a conflict",
                         "source": "memo.md"})
    assert r.status_code == 409
    body = r.json()
    assert body["result"] == "conflict"
    assert body["stored"] is False
    assert body["conflicts"][0]["tier"] == "block"


def test_learn_blank_source_is_400_not_500(serve):
    """A whitespace-only source is truthy, so it passes the route's missing-field
    check and reaches store.learn(), which raises ValueError — the route must map
    that to 400 (client error), not a generic 500."""
    store = FakeStore()
    def _raise_value(*a, **k):
        raise ValueError("learn requires a non-empty source")
    store.learn = _raise_value
    url = serve(store)
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "s", "source": "   "})
    assert r.status_code == 400
    assert "non-empty source" in r.json()["error"]


def test_learn_forwards_force_exception(serve):
    store = FakeStore()
    url = serve(store)
    httpx.post(f"{url}/learn",
               json={"org_id": "o", "project": "p", "who": "w", "statement": "ok",
                     "source": "memo.md", "force_exception": "approved by Alex"})
    assert store.calls[-1][-1] == "approved by Alex"


def test_learn_normalize_unavailable_returns_503_not_500(serve):
    store = FakeStore()
    store.learn_error = NormalizeUnavailable("connection refused")
    url = serve(store)
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "s", "source": "memo.md"})
    assert r.status_code == 503
    assert r.json()["result"] == "degraded"


def test_learn_permanent_normalize_unavailable_returns_502_not_503(serve):
    """See /remember's identical 502 mapping for the permanent-4xx class."""
    store = FakeStore()
    store.learn_error = NormalizeUnavailable("400 bad request", retryable=False)
    url = serve(store)
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "s", "source": "memo.md"})
    assert r.status_code == 502
    assert r.json()["result"] == "degraded"


def test_learn_500_on_store_error(serve):
    store = FakeStore()
    store.raise_on = {"learn"}
    url = serve(store)
    r = httpx.post(f"{url}/learn",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "s", "source": "memo.md"})
    assert r.status_code == 500
    assert "boom in learn" in r.json()["error"]


def test_diagnose_surfaces_store_diagnostics_error(serve):
    store = FakeStore()
    store.raise_on = {"diagnostics"}
    url = serve(store)
    body = httpx.get(f"{url}/diagnose").json()
    assert "diagnostics query failed: boom in diagnostics" == body["diagnostics_error"]


# --------------------------------------------------------------------------
# POST /ingest  (exercises the real ingest_text -> store.remember_fact path)
# --------------------------------------------------------------------------
def test_ingest_missing_text_is_400(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w"})
    assert r.status_code == 400
    assert r.json()["error"] == "missing field: text"


def test_ingest_503_when_retrieval_disabled(serve):
    url = serve(FakeStore(retrieval=False))
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w", "text": "hi there"})
    assert r.status_code == 503


def test_ingest_200_stores_chunks(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "text": "A runbook fact worth keeping around here.",
                         "source": "runbook.md"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "ingested"
    assert body["chunks"] >= 1
    assert any(c[0] == "remember_fact" for c in store.calls)


def test_ingest_500_on_store_error(serve):
    store = FakeStore()
    store.raise_on = {"remember_fact"}
    url = serve(store)
    r = httpx.post(f"{url}/ingest",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "text": "A runbook fact worth keeping around here."})
    assert r.status_code == 500
    assert "boom in remember_fact" in r.json()["error"]


# --------------------------------------------------------------------------
# POST /supersede
# --------------------------------------------------------------------------
def test_supersede_missing_existing_id_is_400(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w", "statement": "s"})
    assert r.status_code == 400
    assert r.json()["error"] == "missing field: existing_id"


def test_supersede_stored_returns_200(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "existing_id": "old-1", "statement": "revised", "reason": "changed"})
    assert r.status_code == 200
    assert r.json()["result"] == "superseded"
    assert r.json()["claim_id"] == "sup-id"


def test_supersede_residual_conflict_returns_409(serve):
    """store.supersede can return a Conflict (store.py:586 — the revised claim
    would still conflict with another active claim, so nothing is written). The
    route mirrors /remember: Stored -> 200 'superseded', Conflict -> 409
    'conflict'. The MCP adapter treats /supersede as _GUARDED_OK={200,409}, so
    the 409 body is surfaced as a structured conflict rather than an error."""
    store = FakeStore()
    store.supersede_result = Conflict(
        incoming_text="revised", incoming_norm={},
        conflicts=[ConflictHit(claim={"id": "other"}, tier="block",
                               score=0.9, source="structural", reason="r")])
    url = serve(store)
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "existing_id": "old-1", "statement": "revised"})
    assert r.status_code == 409
    body = r.json()
    assert body["result"] == "conflict"
    assert body["stored"] is False
    assert body["conflicts"][0]["tier"] == "block"


def test_supersede_missing_claim_is_404(serve):
    """A nonexistent/inactive existing_id makes store.supersede raise KeyError.
    /retract already maps that KeyError to 404; /supersede must too — a pure
    client error (unknown id) reported as a generic 500 breaks the response-code
    contract and invites pointless retries."""
    store = FakeStore()
    store.supersede_error = KeyError("no active claim deadbeef in (org=o, project=p)")
    url = serve(store)
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "existing_id": "deadbeef", "statement": "revised"})
    assert r.status_code == 404
    assert "no active claim deadbeef" in r.json()["error"]


def test_supersede_500_on_store_error(serve):
    store = FakeStore()
    store.raise_on = {"supersede"}
    url = serve(store)
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "existing_id": "old-1", "statement": "revised"})
    assert r.status_code == 500
    assert "boom in supersede" in r.json()["error"]


def test_supersede_normalize_unavailable_returns_503_not_500(serve):
    store = FakeStore()
    store.supersede_error = NormalizeUnavailable("connection refused")
    url = serve(store)
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "existing_id": "old-1", "statement": "revised"})
    assert r.status_code == 503
    assert r.json()["result"] == "degraded"


def test_supersede_permanent_normalize_unavailable_returns_502_not_503(serve):
    """See /remember's identical 502 mapping for the permanent-4xx class;
    supersede still has no force_exception override, so this is unconditional."""
    store = FakeStore()
    store.supersede_error = NormalizeUnavailable("422 unprocessable", retryable=False)
    url = serve(store)
    r = httpx.post(f"{url}/supersede",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "existing_id": "old-1", "statement": "revised"})
    assert r.status_code == 502
    assert r.json()["result"] == "degraded"


def test_unknown_post_path_404(serve):
    url = serve(FakeStore())
    r = httpx.post(f"{url}/nope", json={})
    assert r.status_code == 404


# --------------------------------------------------------------------------
# POST /retract  (wrong-at-birth removal — no gate, reason required)
# --------------------------------------------------------------------------
def test_retract_missing_each_field_is_400(serve):
    # DEFAULT-PROJECT (private-repo issue #179): `project` is no longer a required field
    # here -- an omitted project resolves via _resolve_project instead of
    # 400ing (see test_retract_missing_project_resolves_to_default below).
    url = serve(FakeStore())
    full = {"org_id": "o", "project": "p", "who": "w",
            "claim_id": "c1", "reason": "typo"}
    for missing in full:
        if missing == "project":
            continue
        body = {k: v for k, v in full.items() if k != missing}
        r = httpx.post(f"{url}/retract", json=body)
        assert r.status_code == 400
        assert r.json()["error"] == f"missing field: {missing}"


def test_retract_missing_project_resolves_to_default_with_warning(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/retract",
                   json={"org_id": "o", "who": "w", "claim_id": "c1", "reason": "typo"})
    assert r.status_code == 200
    assert store.calls[-1] == ("retract", "o", "default", "w", "c1", "typo")
    assert any("default_project" in w for w in r.json()["warnings"])


def test_retract_200_and_sets_last_write(serve):
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/retract",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "claim_id": "c1", "reason": "test junk"})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "retracted"
    assert body["claim_id"] == "c1"
    assert store.calls[-1] == ("retract", "o", "p", "w", "c1", "test junk")
    assert server._CFG["last_write_at"] is not None


def test_retract_missing_claim_is_404(serve):
    store = FakeStore()
    store.retract_error = KeyError("no active claim c9 in (org=o, project=p)")
    url = serve(store)
    r = httpx.post(f"{url}/retract",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "claim_id": "c9", "reason": "typo"})
    assert r.status_code == 404
    assert "no active claim c9" in r.json()["error"]


def test_retract_blank_reason_is_400_not_500(serve):
    """A whitespace-only reason is truthy, so it passes the route's
    missing-field check and reaches store.retract(), which raises ValueError —
    the route must map that to 400 (client error), not a generic 500."""
    store = FakeStore()
    store.retract_error = ValueError("retract requires a non-empty reason")
    url = serve(store)
    r = httpx.post(f"{url}/retract",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "claim_id": "c1", "reason": "   "})
    assert r.status_code == 400
    assert "non-empty reason" in r.json()["error"]


def test_retract_500_on_store_error(serve):
    store = FakeStore()
    store.raise_on = {"retract"}
    url = serve(store)
    r = httpx.post(f"{url}/retract",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "claim_id": "c1", "reason": "typo"})
    assert r.status_code == 500
    assert "boom in retract" in r.json()["error"]


def test_retract_401_without_token_does_not_touch_store(serve, monkeypatch):
    monkeypatch.setenv("ERGO_API_TOKEN", "secret")
    store = FakeStore()
    url = serve(store)
    r = httpx.post(f"{url}/retract",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "claim_id": "c1", "reason": "typo"})
    assert r.status_code == 401
    assert not any(c[0] == "retract" for c in store.calls)


# --------------------------------------------------------------------------
# Socket timeout (liveness: a stalled client must not freeze the server)
# --------------------------------------------------------------------------
def test_stalled_post_body_is_dropped_and_server_stays_live(serve, monkeypatch):
    """Audit 2026-07-04 finding 3: a client that promises Content-Length: N but
    sends fewer bytes held rfile.read(n) forever, queueing every later request
    behind it (the server is single-threaded by design). With a socket timeout
    the staller is dropped and the next request is answered."""
    monkeypatch.setenv("ERGO_HTTP_TIMEOUT", "0.3")
    url = serve(FakeStore())
    u = urlparse(url)
    stall = socket.create_connection((u.hostname, u.port))
    stall.sendall(b"POST /remember HTTP/1.1\r\nHost: t\r\n"
                  b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
    time.sleep(0.1)  # let the server accept the staller before the real request
    try:
        r = httpx.get(f"{url}/health", timeout=3.0)
    finally:
        stall.close()
    assert r.status_code == 200


# --------------------------------------------------------------------------
# GET /diagnose  (the anti-zombie ops view: warnings assembly is real logic)
# --------------------------------------------------------------------------
def test_diagnose_warns_on_missing_backup_owner_and_kill_review(serve):
    url = serve(FakeStore(), make_cfg(backup_path=None, owner="(unset)",
                                      kill_review_date=None))
    body = httpx.get(f"{url}/diagnose").json()
    warns = " | ".join(body["warnings"])
    assert "ERGO_BACKUP_PATH" in warns
    assert "ERGO_OWNER" in warns
    assert "ERGO_KILL_REVIEW_DATE" in warns
    assert body["kill_review_status"] == "unset"


def test_diagnose_flags_overdue_kill_review(serve):
    url = serve(FakeStore(), make_cfg(kill_review_date="2000-01-01"))
    body = httpx.get(f"{url}/diagnose").json()
    assert body["kill_review_status"] == "OVERDUE"
    assert any("PAST" in w for w in body["warnings"])


def test_diagnose_flags_version_drift(serve):
    store = FakeStore()
    store.diag = {**DEFAULT_DIAG, "versions": {"drift": True}}
    url = serve(store)
    body = httpx.get(f"{url}/diagnose").json()
    assert any("VERSION DRIFT" in w for w in body["warnings"])


def test_diagnose_flags_empty_store(serve):
    store = FakeStore()
    store.diag = {**DEFAULT_DIAG, "counts": {"claims_total": 0}}
    url = serve(store)
    body = httpx.get(f"{url}/diagnose").json()
    assert any("EMPTY" in w for w in body["warnings"])


def test_diagnose_clean_config_has_no_warnings(serve, tmp_path):
    backup = tmp_path / "ergo-latest.db"
    backup.write_bytes(b"x")  # exists -> last_backup_at populated, no warning
    url = serve(FakeStore(), make_cfg(backup_path=str(backup), owner="Alex",
                                      kill_review_date="2999-01-01"))
    body = httpx.get(f"{url}/diagnose").json()
    assert body["warnings"] == []
    assert body["last_backup_at"] is not None
    assert body["kill_review_status"] == "scheduled"

