"""End-to-end tests for `POST /import` (export-import phase-1 brief): the
contradiction-checked merge that dispatches every incoming
claim through the SAME guarded write path `/remember`/`/learn` use (never a
reimplemented gate), dedup against active statements, the active-claim/
project caps stopping gracefully with `cap_reached: true` (never a 500), and
the export -> import round trip.

Driven entirely through the LEGACY_TOKEN (single-tenant/back-compat) path,
which is always synchronous -- `_import_merge`'s real per-claim behavior
(conflict/dedup/revisions-folding/round-trip) is exercised identically
whether the request arrives via this path or the (not-in-this-build)
ASYNC path (private-repo issue #122), so testing it once here is not lost coverage.
This build has no plan gate and no async import path at all, so there
is nothing further to test beyond what's exercised above.
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


def _import(url, org_id, claims, target_project=None, who=None):
    """POST /import on the legacy (always-synchronous) path -- asserts 200
    and returns the body directly (`{imported, skipped_duplicate, conflicts,
    conflict_count, cap_reached}`)."""
    payload = {"org_id": org_id, "claims": claims}
    if target_project is not None:
        payload["target_project"] = target_project
    if who is not None:
        payload["who"] = who
    r = httpx.post(f"{url}/import", headers=LEGACY_HDR, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# 1. Clean import stores + is retrievable via /recall (proves re-embed).
# --------------------------------------------------------------------------
def test_clean_import_stores_and_is_retrievable_via_recall(start_server, monkeypatch):
    monkeypatch.setenv("ERGO_RETRIEVAL", "on")
    url = start_server()

    body = _import(url, "acme", [
        {"project": "p", "statement": "The vault code is 1234.", "who": "alice"},
    ])
    assert body["imported"] == 1
    assert body["skipped_duplicate"] == 0
    assert body["conflicts"] == []
    assert body["conflict_count"] == 0
    assert body["cap_reached"] is False

    recall = httpx.get(f"{url}/recall", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p", "q": "vault code"}).json()
    assert len(recall["results"]) == 1
    assert recall["results"][0]["statement"] == "The vault code is 1234."


# --------------------------------------------------------------------------
# 2. A claim contradicting an existing active claim -> conflicts, not stored.
# --------------------------------------------------------------------------
def test_import_conflict_is_reported_and_not_stored(start_server):
    url = start_server()

    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "bob",
                    "statement": "TLS 1.2 must be enabled in prod.",
                    "reason": "security baseline"})

    body = _import(url, "acme", [
        {"project": "p", "statement": "TLS 1.2 must not be enabled in prod.",
         "who": "bob", "reason": "conflicting import"},
    ])
    assert body["imported"] == 0
    assert body["conflict_count"] == 1
    conflict = body["conflicts"][0]
    assert conflict["statement"] == "TLS 1.2 must not be enabled in prod."
    assert conflict["tier"] == "block"
    assert conflict["against"]
    assert conflict["reason"]

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    assert len(active["claims"]) == 1
    assert active["claims"][0]["statement"] == "TLS 1.2 must be enabled in prod."


# --------------------------------------------------------------------------
# 3. Duplicate active statement -> skipped_duplicate.
# --------------------------------------------------------------------------
def test_import_duplicate_active_statement_is_skipped(start_server):
    url = start_server()

    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "acme", "project": "p", "who": "alice",
                    "statement": "Deploy on Tuesdays.", "reason": "r"})

    body = _import(url, "acme", [
        {"project": "p", "statement": "Deploy on Tuesdays.", "who": "alice"},
    ])
    assert body["imported"] == 0
    assert body["skipped_duplicate"] == 1
    assert body["conflicts"] == []
    assert body["conflict_count"] == 0
    assert body["cap_reached"] is False


# --------------------------------------------------------------------------
# 4. Round-trip: export org A -> import into empty org B -> same active
#    statements present and recallable.
# --------------------------------------------------------------------------
def test_round_trip_export_then_import_into_empty_org(start_server, monkeypatch):
    monkeypatch.setenv("ERGO_RETRIEVAL", "on")
    url = start_server()

    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "platform", "who": "alice",
                    "statement": "Deploy on Tuesdays.", "reason": "on-call Mon/Wed"})
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "kb", "who": "alice",
                    "statement": "The vault code is 1234.", "reason": "r"})

    export = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                       params={"org_id": "org-a"}).json()
    assert export["claim_count"] == 2

    body = _import(url, "org-b", export["claims"])
    assert body["imported"] == 2

    active_platform = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                                params={"org_id": "org-b", "project": "platform"}).json()
    assert active_platform["claims"][0]["statement"] == "Deploy on Tuesdays."

    recall = httpx.get(f"{url}/recall", headers=LEGACY_HDR,
                       params={"org_id": "org-b", "project": "kb", "q": "vault code"}).json()
    assert len(recall["results"]) == 1
    assert recall["results"][0]["statement"] == "The vault code is 1234."

    # org A's own claims are untouched (still 2, still active).
    export_a_again = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                               params={"org_id": "org-a"}).json()
    assert export_a_again["claim_count"] == 2


# --------------------------------------------------------------------------
# 4b. Multi-page export round trip (private-repo issue #203): paging must not tear a
#     supersede chain across pages, and gluing every page's `claims` back
#     together must round-trip through /import exactly like a single
#     unbounded export -- nothing lost, nothing duplicated.
# --------------------------------------------------------------------------
def test_multi_page_export_with_chain_astride_boundary_round_trips_cleanly(
        start_server, monkeypatch):
    monkeypatch.setenv("ERGO_RETRIEVAL", "off")
    url = start_server()

    # Two plain claims, then a 3-link supersede chain, then two more plain
    # claims -- with a page size of 3 the chain's head ("Datastore: C.")
    # lands as the LAST entry of page 1, deliberately astride the boundary.
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "p", "who": "alice",
                    "statement": "Plain 1.", "reason": "r"})
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "p", "who": "alice",
                    "statement": "Plain 2.", "reason": "r"})
    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "org-a", "project": "p", "who": "alice",
                         "statement": "Datastore: A.", "reason": "ra"})
    a_id = r.json()["claim_id"]
    r = httpx.post(f"{url}/supersede", headers=LEGACY_HDR,
                   json={"org_id": "org-a", "project": "p", "who": "alice",
                         "existing_id": a_id, "statement": "Datastore: B.", "reason": "rb"})
    b_id = r.json()["claim_id"]
    httpx.post(f"{url}/supersede", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "p", "who": "alice",
                    "existing_id": b_id, "statement": "Datastore: C.", "reason": "rc"})
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "p", "who": "alice",
                    "statement": "Plain 3.", "reason": "r"})
    httpx.post(f"{url}/remember", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "p", "who": "alice",
                    "statement": "Plain 4.", "reason": "r"})

    # Walk every page with limit=3 and glue the claims back together.
    all_claims = []
    offset = 0
    pages = 0
    for _ in range(20):
        pages += 1
        page = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                         params={"org_id": "org-a", "limit": 3, "offset": offset}).json()
        assert page["ergo_export_version"] == 2
        all_claims.extend(page["claims"])
        if not page["truncated"]:
            break
        offset = page["next_offset"]
    assert pages > 1, "test is only meaningful if the corpus actually paged"
    assert len(all_claims) == 5  # 4 plain heads + 1 chain head (A->B->C collapses to C)

    # The chain's head still carries its FULL revisions -- never split
    # across the page boundary it straddled.
    head = next(c for c in all_claims if c["statement"] == "Datastore: C.")
    assert [rev["statement"] for rev in head["revisions"]] == [
        "Datastore: A.", "Datastore: B."]

    # Round-trip the glued-together claims into a fresh org.
    result = _import(url, "org-b", all_claims)
    assert result["imported"] == 5
    assert result["skipped_duplicate"] == 0
    assert result["conflicts"] == []

    active_b = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                         params={"org_id": "org-b", "project": "p"}).json()
    statements = {c["statement"] for c in active_b["claims"]}
    assert statements == {"Plain 1.", "Plain 2.", "Plain 3.", "Plain 4.",
                          "Datastore: C."}
    assert active_b["count"] == 5  # nothing lost, nothing duplicated

    stored_head = next(c for c in active_b["claims"]
                       if c["statement"] == "Datastore: C.")
    assert "prior revisions" in stored_head["reason"]
    assert "Datastore: A." in stored_head["reason"]
    assert "Datastore: B." in stored_head["reason"]


# --------------------------------------------------------------------------
# 5. Nested `revisions[]` (private-repo issue #189): folded into the stored `reason`,
#    bounded with an announced truncation, a v1 document (no `revisions`
#    key) importing unchanged, and a full round trip preserving the
#    why-chain narrative through the SAME guarded write path (never a
#    reimplemented gate -- `has_active_statement` dedup still runs on the
#    HEAD's own statement, unaffected by anything folded into `reason`).
# --------------------------------------------------------------------------
def test_import_folds_revisions_into_stored_reason(start_server):
    url = start_server()

    body = _import(url, "acme", [
        {"project": "p", "statement": "CockroachDB is the datastore.",
         "reason": "chosen for distributed consistency", "who": "carol",
         "revisions": [
             {"statement": "Postgres is the datastore.", "reason": "initial pick",
              "who": "alice", "status": "superseded",
              "created_at": "2026-05-01T00:00:00Z"},
             {"statement": "MySQL is the datastore.", "reason": "chosen for replication",
              "who": "bob", "status": "superseded",
              "created_at": "2026-05-02T00:00:00Z"},
         ]},
    ])
    assert body["imported"] == 1

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    stored = active["claims"][0]
    assert stored["statement"] == "CockroachDB is the datastore."
    reason = stored["reason"]
    assert reason.startswith("chosen for distributed consistency")
    assert "prior revisions" in reason
    assert "Postgres is the datastore." in reason
    assert "initial pick" in reason
    assert "alice" in reason
    assert "MySQL is the datastore." in reason
    assert "chosen for replication" in reason
    assert "bob" in reason
    # oldest-first: Postgres (the earliest) appears before MySQL in the text.
    assert reason.index("Postgres is the datastore.") < reason.index("MySQL is the datastore.")


def test_import_claim_with_no_revisions_key_is_unaffected(start_server):
    url = start_server()

    body = _import(url, "acme", [
        {"project": "p", "statement": "Deploy on Tuesdays.", "reason": "on-call Mon/Wed",
         "who": "alice"},
    ])
    assert body["imported"] == 1

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    assert active["claims"][0]["reason"] == "on-call Mon/Wed"


def test_import_v1_document_without_revisions_key_still_imports_fine(start_server):
    """A hand-crafted v1 export (no `revisions` field anywhere, the shape
    GET /export produced before private-repo issue #189) must still import unchanged."""
    url = start_server()

    v1_doc = {
        "ergo_export_version": 1,
        "org_id": "somewhere-else",
        "exported_at": "2026-05-01T00:00:00Z",
        "embedder": {"provider": "FastEmbedEmbedder", "dim": 384},
        "claim_count": 1,
        "claims": [
            {"id": "old-id-123", "project": "p", "statement": "Deploy on Tuesdays.",
             "reason": "on-call Mon/Wed", "source": None, "status": "active",
             "who": "alice", "tier": None, "created_at": "2026-04-01T00:00:00Z"},
        ],
    }
    body = _import(url, "acme", v1_doc["claims"])
    assert body["imported"] == 1
    assert body["conflicts"] == []

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    assert active["claims"][0]["statement"] == "Deploy on Tuesdays."
    assert active["claims"][0]["reason"] == "on-call Mon/Wed"


def test_import_long_revisions_chain_truncates_and_announces_it(start_server, monkeypatch):
    """A chain long enough to exceed `ERGO_MAX_FIELD_CHARS` must truncate --
    never silently -- and the stored reason must say so."""
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", "400")
    url = start_server()

    revisions = [
        {"statement": f"Revision number {i} of the datastore choice, with some padding text.",
         "reason": f"reason number {i} for why this was picked at the time",
         "who": f"engineer-{i}", "status": "superseded",
         "created_at": f"2026-05-{i:02d}T00:00:00Z"}
        for i in range(1, 30)
    ]
    body = _import(url, "acme", [
        {"project": "p", "statement": "Final datastore choice.", "reason": "final call",
         "who": "carol", "revisions": revisions},
    ])
    assert body["imported"] == 1

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    reason = active["claims"][0]["reason"]
    assert len(reason) <= 400
    assert "truncated" in reason
    # not every revision made it in -- proves it actually truncated, not
    # just happened to fit.
    assert f"engineer-{len(revisions) - 1}" not in reason


def test_import_revisions_fold_never_clips_the_claims_own_reason(start_server, monkeypatch):
    """Review finding on private-repo issue #189: if the claim's own `reason`
    alone already leaves no room for even one whole revision entry (plus
    the truncation note), the fold must NEVER slice into `reason` to make
    room for secondary history -- the reason is the PRIMARY content. The
    earlier implementation hard-clipped `base` itself here while still
    announcing "revisions truncated", silently losing the primary content
    and mislabeling what was actually lost."""
    long_reason = ("This is the claim's own load-bearing rationale. " * 4).strip()
    # Cap leaves only 10 chars of headroom past the reason itself -- nowhere
    # near enough for the header/footer/note/an entry, but also not enough
    # for the note alone (57 chars), so the fold must drop the revisions
    # ENTIRELY and return `reason` byte-for-byte unchanged.
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", str(len(long_reason) + 10))
    url = start_server()

    body = _import(url, "acme", [
        {"project": "p", "statement": "Final choice.", "reason": long_reason, "who": "carol",
         "revisions": [
             {"statement": "Earlier choice.", "reason": "earlier reasoning", "who": "alice",
              "status": "superseded", "created_at": "2026-01-01T00:00:00Z"},
         ]},
    ])
    assert body["imported"] == 1

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    reason = active["claims"][0]["reason"]
    # The claim's own reason must survive FULLY intact, byte for byte --
    # never clipped to make room for the (secondary) revision history.
    assert reason == long_reason


def test_import_revisions_fold_keeps_base_intact_when_exactly_one_entry_fits(start_server, monkeypatch):
    """When the cap leaves room for `base` + the note + exactly ONE whole
    revision entry (but not a second), that one entry is kept, the note is
    present, and -- the point of this test -- `base` is still fully intact,
    never partially consumed to squeeze the entry in."""
    base = "final call"
    entry1_stmt = "Earlier choice."
    entry2_stmt = "Middle choice, quite a bit longer than the first one here."
    # cap sized (see the module's _fold_revisions_into_reason for the exact
    # overhead accounting) so entry 1 fits alongside base+note but entry 2
    # does not.
    monkeypatch.setenv("ERGO_MAX_FIELD_CHARS", "172")
    url = start_server()

    body = _import(url, "acme", [
        {"project": "p", "statement": "Final statement.", "reason": base, "who": "carol",
         "revisions": [
             {"statement": entry1_stmt, "reason": "earlier reasoning", "who": "alice",
              "status": "superseded", "created_at": "2026-01-01T00:00:00Z"},
             {"statement": entry2_stmt, "reason": "middle reasoning also fairly long here for padding purposes",
              "who": "bob", "status": "superseded", "created_at": "2026-01-02T00:00:00Z"},
         ]},
    ])
    assert body["imported"] == 1

    active = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                       params={"org_id": "acme", "project": "p"}).json()
    reason = active["claims"][0]["reason"]
    assert len(reason) <= 172
    assert reason.startswith(base)  # base is intact, not partially consumed
    assert entry1_stmt in reason
    assert entry2_stmt not in reason
    assert "truncated" in reason


def test_round_trip_export_then_import_preserves_why_chain_narrative(start_server, monkeypatch):
    """Full export -> import round trip on a store WITH a supersede chain:
    the revised claim's history is not silently dropped -- it survives
    folded into the imported claim's `reason`."""
    monkeypatch.setenv("ERGO_RETRIEVAL", "on")
    url = start_server()

    r = httpx.post(f"{url}/remember", headers=LEGACY_HDR,
                   json={"org_id": "org-a", "project": "platform", "who": "alice",
                         "statement": "Postgres is the datastore.",
                         "reason": "initial pick"})
    claim_id = r.json()["claim_id"]
    httpx.post(f"{url}/supersede", headers=LEGACY_HDR,
              json={"org_id": "org-a", "project": "platform", "who": "bob",
                    "existing_id": claim_id,
                    "statement": "CockroachDB is the datastore.",
                    "reason": "chosen for distributed consistency"})

    export = httpx.get(f"{url}/export", headers=LEGACY_HDR,
                       params={"org_id": "org-a"}).json()
    assert export["ergo_export_version"] == 2
    assert export["claim_count"] == 1
    assert len(export["claims"][0]["revisions"]) == 1

    body = _import(url, "org-b", export["claims"])
    assert body["imported"] == 1

    active_b = httpx.get(f"{url}/active", headers=LEGACY_HDR,
                         params={"org_id": "org-b", "project": "platform"}).json()
    stored = active_b["claims"][0]
    assert stored["statement"] == "CockroachDB is the datastore."
    assert "Postgres is the datastore." in stored["reason"]
    assert "initial pick" in stored["reason"]
