"""Tests for `ContradictionStore.purge_project` — hard-deletes one
project's claims + matching vec_claims + audit rows out of a SHARED/pooled
store.

Distinct from `tenant_ops.delete_tenant` (whole-silo-file purge): this is
row-level within ONE database that many (org_id, project) pairs already
share, motivated by test-pollution cleanup (e.g. thousands of
`ingest-selftest*` claims).
"""
import hashlib

import pytest

from ergo.contradiction.store import ContradictionStore


class FakeEmbedder:
    """Hashed bag-of-words -> unit vector (same as test_retrieval.py)."""
    dim = 64

    def embed(self, text):
        v = [0.0] * self.dim
        for w in text.lower().split():
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]


def fresh():
    return ContradictionStore(":memory:")


def fresh_with_vec():
    return ContradictionStore(":memory:", embedder=FakeEmbedder())


# --- happy path: full purge -------------------------------------------------
def test_purge_removes_all_claims_and_vec_claims_for_the_project():
    s = fresh_with_vec()
    a = s.remember("acme", "proj-a", "alice", "Backups run nightly.", "reason a")
    s.supersede("acme", "proj-a", "alice", a.claim_id, "Backups run hourly.", "reason a2")
    assert s.db.execute(
        "SELECT count(*) FROM claims WHERE org_id='acme' AND project='proj-a'"
    ).fetchone()[0] == 2
    assert s.db.execute(
        "SELECT count(*) FROM vec_claims v JOIN claims c ON c.id = v.claim_id "
        "WHERE c.org_id='acme' AND c.project='proj-a'"
    ).fetchone()[0] == 2

    result = s.purge_project("acme", "proj-a")

    # supersede() always audits itself as a conflict_events row
    # (resolution='superseded'), so purging 1 remember + 1 supersede leaves
    # 2 claims, 2 vectors, 2 writes, and 1 conflict_events row to purge too.
    assert result == {
        "org_id": "acme", "project": "proj-a", "dry_run": False,
        "claims_deleted": 2, "vectors_deleted": 2,
        "writes_deleted": 2, "conflict_events_deleted": 1,
    }
    assert s.db.execute(
        "SELECT count(*) FROM claims WHERE org_id='acme' AND project='proj-a'"
    ).fetchone()[0] == 0
    assert s.db.execute("SELECT count(*) FROM vec_claims").fetchone()[0] == 0
    assert s.db.execute(
        "SELECT count(*) FROM writes WHERE org_id='acme' AND project='proj-a'"
    ).fetchone()[0] == 0
    assert s.db.execute(
        "SELECT 1 FROM projects WHERE org_id='acme' AND project='proj-a'"
    ).fetchone() is None


def test_purge_removes_conflict_events_for_the_project():
    s = fresh()
    s.remember("acme", "proj-a", "alice", "TLS 1.2 must be enabled in prod.", "r1")
    conflict = s.remember("acme", "proj-a", "bob", "TLS 1.2 must not be enabled in prod.", "r2")
    from ergo.contradiction.store import Conflict
    assert isinstance(conflict, Conflict)
    assert s.db.execute(
        "SELECT count(*) FROM conflict_events WHERE org_id='acme' AND project='proj-a'"
    ).fetchone()[0] >= 1

    result = s.purge_project("acme", "proj-a")
    assert result["conflict_events_deleted"] >= 1
    assert s.db.execute(
        "SELECT count(*) FROM conflict_events WHERE org_id='acme' AND project='proj-a'"
    ).fetchone()[0] == 0


# --- key technical risk: vec-index consistency for OTHER projects ----------
def test_purge_leaves_a_different_projects_vectors_intact_and_recallable():
    s = fresh_with_vec()
    s.remember("acme", "proj-a", "alice", "Project A uses Postgres.", "reason a")
    b = s.remember("acme", "proj-b", "bob", "Project B uses SQLite.", "reason b")
    assert isinstance(b, object)

    s.purge_project("acme", "proj-a")

    # proj-b's claim must still recall correctly -- no orphaned/corrupted
    # vectors, no cross-scope contamination, KNN index stays consistent.
    hits = s.recall("acme", "proj-b", "Project B uses SQLite.", limit=5)
    assert len(hits) == 1
    assert hits[0]["statement"] == "Project B uses SQLite."

    # proj-a is really gone from both the claim table and the vec index.
    assert s.active("acme", "proj-a").state == "project_missing"
    assert s.db.execute(
        "SELECT count(*) FROM vec_claims v JOIN claims c ON c.id = v.claim_id "
        "WHERE c.org_id='acme' AND c.project='proj-a'"
    ).fetchone()[0] == 0
    # the total vec_claims count matches exactly what's left in claims.
    total_claims = s.db.execute("SELECT count(*) FROM claims").fetchone()[0]
    total_vecs = s.db.execute("SELECT count(*) FROM vec_claims").fetchone()[0]
    assert total_claims == total_vecs == 1


def test_purge_without_retrieval_enabled_reports_zero_vectors():
    s = fresh()  # no embedder -> no vec_claims table at all
    s.remember("acme", "proj-a", "alice", "Some claim.", "reason")
    result = s.purge_project("acme", "proj-a")
    assert result["vectors_deleted"] == 0
    assert result["claims_deleted"] == 1


# --- dry_run: preview only, deletes nothing --------------------------------
def test_dry_run_returns_counts_and_deletes_nothing():
    s = fresh_with_vec()
    s.remember("acme", "proj-a", "alice", "Backups run nightly.", "reason a")

    result = s.purge_project("acme", "proj-a", dry_run=True)

    assert result["dry_run"] is True
    assert result["claims_deleted"] == 1
    assert result["vectors_deleted"] == 1

    # re-query: unchanged
    assert s.db.execute(
        "SELECT count(*) FROM claims WHERE org_id='acme' AND project='proj-a'"
    ).fetchone()[0] == 1
    assert s.db.execute(
        "SELECT count(*) FROM vec_claims"
    ).fetchone()[0] == 1
    assert s.active("acme", "proj-a").state == "has_active_claims"


# --- idempotent: non-existent/empty project ---------------------------------
def test_purge_of_nonexistent_project_returns_zero_counts_not_an_error():
    s = fresh_with_vec()
    result = s.purge_project("acme", "ghost-project")
    assert result == {
        "org_id": "acme", "project": "ghost-project", "dry_run": False,
        "claims_deleted": 0, "vectors_deleted": 0,
        "writes_deleted": 0, "conflict_events_deleted": 0,
    }


def test_purge_is_scoped_to_org_id_and_project_only():
    s = fresh_with_vec()
    s.remember("acme", "proj-a", "alice", "Acme's claim.", "reason")
    s.remember("other-org", "proj-a", "carol", "Other org's claim.", "reason")
    s.remember("acme", "proj-b", "dave", "Acme's other project.", "reason")

    s.purge_project("acme", "proj-a")

    assert s.active("acme", "proj-a").state == "project_missing"
    assert s.active("other-org", "proj-a").state == "has_active_claims"
    assert s.active("acme", "proj-b").state == "has_active_claims"


# --- count consistency: claims and vec_claims drop by exactly N ------------
def test_claims_and_vectors_drop_by_exactly_n():
    s = fresh_with_vec()
    for i in range(5):
        s.remember("acme", "proj-a", "alice", f"Statement number {i}.", f"reason {i}")
    before_claims = s.db.execute("SELECT count(*) FROM claims").fetchone()[0]
    before_vecs = s.db.execute("SELECT count(*) FROM vec_claims").fetchone()[0]
    assert before_claims == before_vecs == 5

    result = s.purge_project("acme", "proj-a")

    assert result["claims_deleted"] == 5
    assert result["vectors_deleted"] == 5
    after_claims = s.db.execute("SELECT count(*) FROM claims").fetchone()[0]
    after_vecs = s.db.execute("SELECT count(*) FROM vec_claims").fetchone()[0]
    assert after_claims == before_claims - 5
    assert after_vecs == before_vecs - 5
