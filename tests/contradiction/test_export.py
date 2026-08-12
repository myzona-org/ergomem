"""End-to-end tests for `GET /export` (export-import phase-1 brief):
active-claims-only export read straight off the store DB (never
`/recall`, which is a semantic-search read over an embedded subset and drops
`source`), and the `?project=` scope filter.

Export has been open to every plan since private-repo issue #109 -- only Import stays
Pro/Team-gated -- so the plan-GATE tests (free/
pro/team/grandfathered TARGET tenants, all -> 200) that used to live here
only ever exercised machinery this build doesn't have, to prove a real tenant with
various plans isn't blocked; a legacy caller is always allowed regardless of
plan by construction (`_export_import_gate_for_plan` returns None
unconditionally for `op == "export"`). Those moved to
`tests/contradiction/test_export_control.py`, dropped from the public
export; everything below is unaffected by that split.
"""
from __future__ import annotations

import httpx
import pytest

from ergo.contradiction import server
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

    started: list = []

    def _start():
        def _boot_and_bind():
            server.boot()
            httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            httpd.daemon_threads = True
            return httpd

        httpd = start_server_thread(_boot_and_bind)
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# 1. Fields + no-vector-field, using the legacy token (grandfathered, always
#    allowed).
# --------------------------------------------------------------------------
def test_export_returns_active_claims_with_correct_fields_and_no_vector_field(start_server):
    url = start_server()
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "acme", "project": "platform", "who": "alice",
                         "statement": "Deploy on Tuesdays.", "reason": "on-call Mon/Wed"})
    assert r.status_code == 200, r.text
    claim_id = r.json()["claim_id"]

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR, params={"org_id": "acme"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["ergo_export_version"] == 2
    assert body["org_id"] == "acme"
    assert isinstance(body["exported_at"], str) and body["exported_at"].endswith("Z")
    assert "provider" in body["embedder"] and "dim" in body["embedder"]
    assert body["claim_count"] == 1
    assert len(body["claims"]) == 1

    claim = body["claims"][0]
    assert claim["id"] == claim_id
    assert claim["project"] == "platform"
    assert claim["statement"] == "Deploy on Tuesdays."
    assert claim["reason"] == "on-call Mon/Wed"
    assert claim["status"] == "active"
    assert claim["who"] == "alice"
    assert "created_at" in claim
    assert set(claim) == {"id", "project", "statement", "reason", "source",
                          "status", "who", "tier", "created_at"}
    # NO vector field anywhere in the export -- vectors are embedder/dim-
    # specific and always recomputed on import.
    assert "embedding" not in claim
    assert "vector" not in claim
    assert "norm" not in claim


def test_export_excludes_retracted_and_superseded_claims_as_top_level_entries(start_server):
    """A superseded predecessor is no longer entirely dropped (private-repo issue #189) --
    it moves INTO the active head's nested `revisions` -- but it still never
    appears as its own top-level `claims[]` entry."""
    url = start_server()
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "acme", "project": "p", "who": "alice",
                         "statement": "Original.", "reason": "r"})
    claim_id = r.json()["claim_id"]
    httpx.post(f"{url}/supersede", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "alice",
                    "existing_id": claim_id, "statement": "Revised.", "reason": "r2"})

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR, params={"org_id": "acme"})
    body = r.json()
    assert body["claim_count"] == 1
    assert body["claims"][0]["statement"] == "Revised."
    assert {c["statement"] for c in body["claims"]} == {"Revised."}
    assert body["claims"][0]["revisions"] == [
        {"statement": "Original.", "reason": "r", "who": "alice",
         "status": "superseded", "created_at": body["claims"][0]["revisions"][0]["created_at"]},
    ]


def test_export_retracted_claim_does_not_appear_at_all(start_server):
    url = start_server()
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "acme", "project": "p", "who": "alice",
                         "statement": "Typo, remove me.", "reason": "oops"})
    claim_id = r.json()["claim_id"]
    httpx.post(f"{url}/retract", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "alice",
                    "claim_id": claim_id, "reason": "wrong at birth"})

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR, params={"org_id": "acme"})
    body = r.json()
    assert body["claim_count"] == 0
    assert body["claims"] == []


def test_export_supersede_chain_of_three_nests_revisions_oldest_first(start_server):
    url = start_server()
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "acme", "project": "p", "who": "alice",
                         "statement": "Postgres is the datastore.",
                         "reason": "initial pick"})
    a_id = r.json()["claim_id"]
    r = httpx.post(f"{url}/supersede", headers=LEGACY_HDR,
                   json={"org_id": "acme", "project": "p", "who": "bob",
                         "existing_id": a_id, "statement": "MySQL is the datastore.",
                         "reason": "chosen for replication"})
    b_id = r.json()["claim_id"]
    httpx.post(f"{url}/supersede", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "carol",
                    "existing_id": b_id, "statement": "CockroachDB is the datastore.",
                    "reason": "chosen for distributed consistency"})

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR, params={"org_id": "acme"})
    body = r.json()
    assert body["claim_count"] == 1
    head = body["claims"][0]
    assert head["statement"] == "CockroachDB is the datastore."
    revisions = head["revisions"]
    assert [rev["statement"] for rev in revisions] == [
        "Postgres is the datastore.", "MySQL is the datastore.",
    ]
    assert [rev["who"] for rev in revisions] == ["alice", "bob"]
    assert all(rev["status"] == "superseded" for rev in revisions)


# --------------------------------------------------------------------------
# 2. ?project= scope filter.
# --------------------------------------------------------------------------
def test_export_project_filter_scopes_correctly(start_server):
    url = start_server()
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p1", "who": "alice",
                    "statement": "P1 statement.", "reason": "r"})
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p2", "who": "alice",
                    "statement": "P2 statement.", "reason": "r"})

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                  params={"org_id": "acme", "project": "p1"})
    body = r.json()
    assert body["claim_count"] == 1
    assert body["claims"][0]["project"] == "p1"
    assert body["claims"][0]["statement"] == "P1 statement."

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR, params={"org_id": "acme"})
    body = r.json()
    assert body["claim_count"] == 2
    assert {c["project"] for c in body["claims"]} == {"p1", "p2"}


# --------------------------------------------------------------------------
# 3. Pagination (private-repo issue #203): `limit`/`offset` opt-in, loud-truncation shape
#    matching /active's field names/semantics, omitted `limit` stays
#    unbounded (backward compat), and the `as_of_seq` snapshot marker.
# --------------------------------------------------------------------------
def test_export_no_limit_param_stays_unbounded_backward_compat(start_server):
    url = start_server()
    for i in range(10):
        httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                  json={"org_id": "acme", "project": "p", "who": "alice",
                        "statement": f"Rule {i}.", "reason": "r"})
    r = httpx.get(f"{url}/export", headers=LEGACY_HDR, params={"org_id": "acme"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claim_count"] == 10
    assert len(body["claims"]) == 10
    assert body["returned"] == 10
    assert body["truncated"] is False
    assert body["next_offset"] is None


def test_export_limit_offset_walks_whole_corpus_no_gaps_no_repeats(start_server):
    url = start_server()
    for i in range(13):
        httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                  json={"org_id": "acme", "project": "p", "who": "alice",
                        "statement": f"Rule {i}.", "reason": "r"})

    seen_ids = []
    offset = 0
    for _ in range(50):
        r = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                      params={"org_id": "acme", "limit": 4, "offset": offset})
        body = r.json()
        assert body["claim_count"] == 13
        seen_ids.extend(c["id"] for c in body["claims"])
        if not body["truncated"]:
            assert body["next_offset"] is None
            break
        offset = body["next_offset"]
    assert len(seen_ids) == len(set(seen_ids)) == 13


def test_export_reading_past_the_end_is_a_clean_empty_page(start_server):
    url = start_server()
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "alice",
                    "statement": "Only rule.", "reason": "r"})
    r = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                  params={"org_id": "acme", "limit": 5, "offset": 500})
    body = r.json()
    assert body["claims"] == []
    assert body["returned"] == 0
    assert body["claim_count"] == 1
    assert body["truncated"] is False
    assert body["next_offset"] is None


def test_export_project_scoped_pagination(start_server):
    url = start_server()
    for i in range(6):
        httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                  json={"org_id": "acme", "project": "p1", "who": "alice",
                        "statement": f"P1 rule {i}.", "reason": "r"})
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p2", "who": "alice",
                    "statement": "P2 rule.", "reason": "r"})

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                  params={"org_id": "acme", "project": "p1", "limit": 2, "offset": 2})
    body = r.json()
    assert body["claim_count"] == 6
    assert body["returned"] == 2
    assert all(c["project"] == "p1" for c in body["claims"])


def test_export_as_of_seq_present_and_stable_across_unchanged_pages(start_server):
    url = start_server()
    for i in range(5):
        httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                  json={"org_id": "acme", "project": "p", "who": "alice",
                        "statement": f"Rule {i}.", "reason": "r"})

    r1 = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                   params={"org_id": "acme", "limit": 2, "offset": 0})
    r2 = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                   params={"org_id": "acme", "limit": 2, "offset": 2})
    body1, body2 = r1.json(), r2.json()
    assert body1["as_of_seq"] == {"p": 5}
    assert body1["as_of_seq"] == body2["as_of_seq"]


def test_export_scoped_project_as_of_seq_populated_even_when_empty(start_server):
    url = start_server()
    r = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                  params={"org_id": "acme", "project": "never-used"})
    body = r.json()
    assert body["claims"] == []
    assert body["as_of_seq"] == {"never-used": 0}


def test_export_malformed_limit_query_param_degrades_instead_of_500(start_server, monkeypatch):
    # Review finding on private-repo PR #208: the route's OWN pre-parse of `limit`/`offset`
    # only caught ValueError -- q.get("limit") returning a non-string
    # element (e.g. a caller/proxy that hands the handler an already-parsed,
    # malformed query dict) raises TypeError from int(), which propagated
    # uncaught instead of degrading like the store's own clamp-and-degrade
    # contract everywhere else in this codebase.
    import ergo.contradiction.server as server_mod

    url = start_server()
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "alice",
                    "statement": "Only rule.", "reason": "r"})

    real_parse_qs = server_mod.parse_qs

    def _malformed_parse_qs(qs, *args, **kwargs):
        result = real_parse_qs(qs, *args, **kwargs)
        if "limit" in result:
            result["limit"] = [None]  # not a string -- int(None) is TypeError
        return result

    monkeypatch.setattr(server_mod, "parse_qs", _malformed_parse_qs)

    r = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                  params={"org_id": "acme", "limit": "5"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["returned"] == 1  # degraded to DEFAULT_EXPORT_LIMIT, not a 500
