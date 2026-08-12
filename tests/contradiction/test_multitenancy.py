"""Legacy-token back-compat tests: with only `ERGO_API_TOKEN` set (no
provisioned tenant -- this build has no way to provision one at all),
behavior is unchanged from the pre-multi-tenancy engine -- body-driven
org_id/who, open mode when no token is configured, and a wrong bearer is a
clean 401. This is the ONLY guarantee in the original multi-tenancy test
file that is single-tenant/legacy-path behavior rather than real-tenant
provisioning; every other test in that file (isolation between two real
tenants, default-project/scope-visibility checks, tenant store failure
handling) requires minting real tenants, machinery this build doesn't have,
and lives in a sibling test file that's not part of this export -- which is
why that file is dropped from the public export and this one is not.
"""
from __future__ import annotations

from http.server import HTTPServer

import httpx
import pytest

from ergo.contradiction import server
from tests.contradiction.conftest import start_server_thread


@pytest.fixture
def start_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ERGO_DB_PATH", str(tmp_path / "ergo.db"))
    monkeypatch.setenv("ERGO_API_TOKEN", "legacy-secret")
    monkeypatch.setenv("ERGO_HTTP_HOST", "127.0.0.1")
    monkeypatch.delenv("ERGO_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ERGO_NLI", "off")
    monkeypatch.setenv("ERGO_RETRIEVAL", "off")

    started: list[HTTPServer] = []

    def _start():
        def _boot_and_bind():
            server.boot()
            return HTTPServer(("127.0.0.1", 0), server.Handler)

        httpd = start_server_thread(_boot_and_bind)
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


LEGACY_HDR = {"Authorization": "Bearer legacy-secret"}


# --------------------------------------------------------------------------
# Back-compat: legacy ERGO_API_TOKEN behaves exactly as today.
# --------------------------------------------------------------------------
def test_legacy_token_write_then_read_round_trips(start_server):
    url = start_server()
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "claude-code", "project": "ergo", "who": "alice",
                         "statement": "TLS 1.2 must be enabled in prod.",
                         "reason": "compliance"})
    assert r.status_code == 200

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "claude-code", "project": "ergo"}).json()
    assert active["state"] == "has_active_claims"
    assert active["claims"][0]["statement"] == "TLS 1.2 must be enabled in prod."


def test_legacy_token_honors_body_org_id_and_who_unchanged(start_server):
    # The legacy principal must NOT override org_id/who -- that is precisely
    # today's (pre-multi-tenancy) contract the MCP adapter and existing callers
    # depend on.
    url = start_server()
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "whatever-org", "project": "p", "who": "bob",
                         "statement": "Backups run nightly.", "reason": "ops"})
    assert r.status_code == 200
    hist = httpx.get(f"{url}/history", headers=LEGACY_HDR,
                     params={"org_id": "whatever-org", "project": "p"}).json()["history"]
    assert hist[0]["org_id"] == "whatever-org"
    assert hist[0]["who"] == "bob"


def test_wrong_bearer_is_401(start_server):
    url = start_server()
    r = httpx.get(f"{url}/active", headers={"Authorization": "Bearer nope"},
                 params={"org_id": "o", "project": "p"})
    assert r.status_code == 401


def test_no_token_configured_stays_fully_open(start_server, monkeypatch):
    monkeypatch.delenv("ERGO_API_TOKEN", raising=False)
    url = start_server()
    r = httpx.post(f"{url}/remember",
                   json={"org_id": "o", "project": "p", "who": "w",
                         "statement": "Open mode still works with no header."})
    assert r.status_code == 200
