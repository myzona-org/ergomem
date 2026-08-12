"""Retrieval layer tests for the unified store (recall / why / remember_fact).

Uses a deterministic fake embedder so CI needs no model download. These lock in
the merge: the SAME SQLite store that runs the contradiction firewall also does
semantic recall + reasoned why + supersede-aware history.
"""
import hashlib

import pytest

from ergo.contradiction.store import ContradictionStore, Stored, WHY_RELEVANCE_FLOOR


class FakeEmbedder:
    """Hashed bag-of-words -> unit vector. Deterministic, offline, dim=64."""
    dim = 64

    def embed(self, text):
        v = [0.0] * self.dim
        for w in text.lower().split():
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]


def _store():
    return ContradictionStore(":memory:", embedder=FakeEmbedder())


def test_retrieval_disabled_without_embedder():
    s = ContradictionStore(":memory:")  # no embedder -> firewall-only
    assert s.retrieval is False
    with pytest.raises(RuntimeError):
        s.recall("o", "p", "anything")


def test_remember_fact_is_recallable():
    s = _store()
    r = s.remember_fact("o", "p", "ed", "Backups are stored on the Synology NAS.")
    assert isinstance(r, Stored)
    hits = s.recall("o", "p", "where are backups stored", limit=3)
    assert hits and "synology" in hits[0]["statement"].lower()


def test_recall_ranks_relevant_above_irrelevant():
    s = _store()
    s.remember_fact("o", "p", "ed", "The cat is named Barsik.")
    s.remember_fact("o", "p", "ed", "The CI pipeline runs on GitHub Actions.")
    hits = s.recall("o", "p", "what is the cat called", limit=2)
    assert hits[0]["statement"].lower().startswith("the cat")
    # scores are blended and bounded
    assert 0.0 <= hits[0]["score"] <= 1.0
    assert hits[0]["score"] >= hits[-1]["score"]


def test_why_returns_reason_and_history_across_supersede():
    s = _store()
    a = s.remember("o", "p", "ed", "Deploy on Fridays.", reason="low traffic")
    assert isinstance(a, Stored)
    s.supersede("o", "p", "ed", a.claim_id, "Never deploy on Fridays.",
                reason="incident risk outweighs low traffic")
    # strong word overlap with the FakeEmbedder's hashed bag-of-words scoring
    # (a crude stand-in for a real embedder's semantics) so the match clears
    # WHY_RELEVANCE_FLOOR; "why does the floor matter" is covered separately.
    w = s.why("o", "p", "never deploy on fridays")
    assert w is not None
    assert "never deploy" in w["claim"].lower()
    # full chain: newest first, then the superseded original
    assert len(w["history"]) == 2
    assert "never deploy" in w["history"][0]["claim"].lower()
    assert "deploy on fridays" in w["history"][1]["claim"].lower()


def test_recall_excludes_superseded():
    s = _store()
    a = s.remember("o", "p", "ed", "Sessions expire after 24 hours.", reason="ux")
    s.supersede("o", "p", "ed", a.claim_id, "Sessions expire after 1 hour.",
                reason="security")
    hits = s.recall("o", "p", "session expiry", limit=10)
    texts = " ".join(h["statement"].lower() for h in hits)
    assert "1 hour" in texts
    assert "24 hours" not in texts  # superseded claim is structurally excluded


def test_why_none_when_no_reasoned_match():
    s = _store()
    s.remember_fact("o", "p", "ed", "The sky is blue.")  # fact, no reason
    assert s.why("o", "p", "atmospheric color") is None  # facts can't win a why


# --------------------------------------------------------------------------
# Relevance floor (private-repo issue #202): nearest-neighbour always has a nearest, so
# without a floor a nonsense query against a populated project gets a
# confident, completely unrelated reasoned claim back instead of null.
# --------------------------------------------------------------------------
def test_why_suppressed_below_relevance_floor():
    s = _store()
    s.remember("o", "p", "ed", "Deploy on Fridays.", reason="low traffic")
    # zero word overlap with the stored claim -> vec_sim and kw both 0.0 with
    # the hashed bag-of-words FakeEmbedder, well below any real floor.
    w = s.why("o", "p", "purple elephant tuesday saxophone linoleum")
    assert w is not None
    assert "claim" not in w  # no fabricated answer alongside the suppression marker
    assert w["suppressed"] == {
        "reason": "below_relevance_floor",
        "score": 0.0,
        "floor": WHY_RELEVANCE_FLOOR,
    }


def test_why_above_floor_match_is_unchanged():
    s = _store()
    a = s.remember("o", "p", "ed", "Deploy on Fridays.", reason="low traffic")
    s.supersede("o", "p", "ed", a.claim_id, "Never deploy on Fridays.",
                reason="incident risk outweighs low traffic")
    w = s.why("o", "p", "never deploy on fridays")
    assert w is not None
    assert "suppressed" not in w
    assert w["score"] > WHY_RELEVANCE_FLOOR
    assert "never deploy" in w["claim"].lower()
    assert len(w["history"]) == 2  # supersede history intact


def test_learned_claim_provenance_is_retrievable_via_why():
    """The Task-05 verification gate: a learned belief always carries a reason (so
    it can win why()) and that reason surfaces the source provenance."""
    s = _store()
    r = s.learn("o", "p", "claude",
                "Gateway sessions are never reused across restarts.",
                source="sessions.py:847")
    assert isinstance(r, Stored)
    w = s.why("o", "p", "are gateway sessions reused")
    assert w is not None  # provenance-only learn still produces a why-able reason
    assert "sessions.py:847" in w["reason"]


# --------------------------------------------------------------------------
# Recall KNN budget must not be diluted by out-of-scope / inactive vectors
# (audit finding #8). Vectors are never deleted on supersede/retract and the
# index is global across orgs/projects, so the fixed k = max(limit*8, 40)
# budget could be entirely consumed by rows the scope/status filter discards,
# silently dropping an on-topic active in-scope claim.
# --------------------------------------------------------------------------
def test_recall_not_diluted_by_foreign_scope_vectors():
    s = _store()
    # 45 foreign-project facts (>k=40) that match the query MORE closely than
    # the target (they carry all four query words; the target carries three),
    # so they occupy the entire top-k and the target ranks ~46th globally.
    for i in range(45):
        s.remember_fact("o", "OTHER", "ed", f"alpha bravo charlie delta filler{i}")
    s.remember_fact("o", "p", "ed", "alpha bravo charlie target")
    hits = s.recall("o", "p", "alpha bravo charlie delta", limit=5)
    assert any("target" in h["statement"] for h in hits), (
        "in-scope active claim was dropped because foreign-scope vectors "
        "filled the fixed KNN budget")


def test_recall_not_diluted_by_superseded_vectors():
    s = _store()
    # 45 in-scope facts that closely match the query, then marked superseded
    # (their vectors survive in the index, as they do after a real supersede).
    for i in range(45):
        s.remember_fact("o", "p", "ed", f"alpha bravo charlie delta old{i}")
    s.db.execute("UPDATE claims SET status='superseded'")
    s.db.commit()
    s.remember_fact("o", "p", "ed", "alpha bravo charlie target")
    hits = s.recall("o", "p", "alpha bravo charlie delta", limit=5)
    assert any("target" in h["statement"] for h in hits), (
        "in-scope active claim was dropped because superseded vectors "
        "filled the fixed KNN budget")
