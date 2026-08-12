"""Tests for ergo.contradiction.ingest — chunking + the bulk-ingest fast path.

These are the regression guards the module's own docstring asks for: the v1 ingest
sliced mid-token and produced garbage chunk starts like "onsumer of this token".
`chunk_text` promises that NO output chunk ever starts or ends mid-word, packs
paragraphs up to `size`, carries a word-aligned overlap tail, and hard-splits an
oversized single paragraph on WORD boundaries. All pure functions, no mocking.
"""
from __future__ import annotations

import pytest

from ergo.contradiction.ingest import chunk_text, ingest_text


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _para(tag: str, n: int) -> str:
    """A single-line paragraph of `n` short, in-vocab words tagged uniquely."""
    return " ".join(f"{tag}{j}" for j in range(n))


def _vocab(text: str) -> set[str]:
    return set(text.split())


def _assert_word_safe(chunks: list[str], source: str) -> None:
    """The core invariant: every token of every chunk is a whole word from the
    source — i.e. nothing was sliced mid-word, no fragment leaked in."""
    vocab = _vocab(source)
    for c in chunks:
        assert c, "empty chunk emitted"
        assert c == c.strip(), f"chunk has surrounding whitespace: {c!r}"
        assert set(c.split()) <= vocab, (
            f"chunk introduced a non-vocab (mid-word) token: {c!r}")


# --------------------------------------------------------------------------
# Degenerate / boundary inputs
# --------------------------------------------------------------------------
def test_empty_text_returns_no_chunks():
    assert chunk_text("") == []


def test_whitespace_only_text_returns_no_chunks():
    assert chunk_text("   \n\n  \t\n\n ") == []


def test_short_text_is_one_stripped_chunk():
    assert chunk_text("  hello world  ") == ["hello world"]


def test_size_is_clamped_to_at_least_one():
    # size=0 would be a div/zero-ish trap; the function clamps to >=1 and must
    # still emit only whole words.
    src = "alpha beta gamma"
    chunks = chunk_text(src, size=0)
    _assert_word_safe(chunks, src)
    assert "".join(chunks).replace(" ", "") == src.replace(" ", "")


def test_overlap_larger_than_size_does_not_crash():
    src = _para("w", 60)
    chunks = chunk_text(src, size=50, overlap=10_000)  # clamped to size-1
    assert len(chunks) > 1
    _assert_word_safe(chunks, src)


# --------------------------------------------------------------------------
# Paragraph packing
# --------------------------------------------------------------------------
def test_small_paragraphs_pack_into_one_chunk():
    src = "first short para.\n\nsecond short para.\n\nthird short para."
    chunks = chunk_text(src, size=1200)
    assert len(chunks) == 1
    assert chunks[0] == src  # paragraphs rejoined verbatim with blank lines


def test_paragraph_boundary_starts_new_chunk_when_over_size():
    p0, p1, p2 = _para("a", 12), _para("b", 12), _para("c", 12)
    src = f"{p0}\n\n{p1}\n\n{p2}"
    chunks = chunk_text(src, size=40, overlap=0)
    # 3 paragraphs each near `size` cannot share a chunk -> a boundary splits them.
    assert len(chunks) >= 2
    _assert_word_safe(chunks, src)


# --------------------------------------------------------------------------
# The headline guarantee: never mid-word (the documented v1 regression)
# --------------------------------------------------------------------------
def test_no_chunk_starts_or_ends_midword_across_paragraphs():
    paras = [_para(chr(ord("a") + i), 10) for i in range(6)]  # each > overlap
    src = "\n\n".join(paras)
    chunks = chunk_text(src, size=80, overlap=15)
    assert len(chunks) > 1
    _assert_word_safe(chunks, src)
    # soft size bound holds (kept-as-is <= size*2; hard-split pieces smaller)
    assert all(len(c) <= 80 * 2 for c in chunks)


def test_oversized_single_paragraph_is_hard_split_on_words():
    src = _para("w", 400)  # one paragraph, no blank lines, well over size*2
    chunks = chunk_text(src, size=100, overlap=10)
    assert len(chunks) > 1
    _assert_word_safe(chunks, src)
    assert all(len(c) <= 100 * 2 for c in chunks)


# --------------------------------------------------------------------------
# Overlap tail behaviour (continuity knob)
# --------------------------------------------------------------------------
def test_positive_overlap_shares_words_between_consecutive_chunks():
    paras = [_para(chr(ord("a") + i), 9) for i in range(5)]
    src = "\n\n".join(paras)
    chunks = chunk_text(src, size=60, overlap=20)
    assert len(chunks) > 1
    for a, b in zip(chunks, chunks[1:]):
        assert set(a.split()) & set(b.split()), (
            "overlap>0 should carry a word-aligned tail into the next chunk")


def test_zero_overlap_shares_nothing_between_distinct_chunks():
    paras = [_para(chr(ord("a") + i), 8) for i in range(4)]  # disjoint vocabs
    src = "\n\n".join(paras)
    chunks = chunk_text(src, size=50, overlap=0)
    assert len(chunks) > 1
    for a, b in zip(chunks, chunks[1:]):
        assert not (set(a.split()) & set(b.split()))


# --------------------------------------------------------------------------
# ingest_text: one remember_fact per chunk, optional source footer
# --------------------------------------------------------------------------
class _FakeStore:
    def __init__(self):
        self.calls: list[tuple] = []
        self._statements: set[str] = set()

    def has_active_statement(self, org_id, project, statement):
        return statement in self._statements

    def remember_fact(self, org_id, project, who, statement):
        self.calls.append((org_id, project, who, statement))
        self._statements.add(statement)

    def remember_facts_batch(self, org_id, project, who, statements,
                             max_active_claims=None):
        # Minimal fake standing in for ContradictionStore.remember_facts_batch:
        # loops the same dedup + remember_fact logic ingest_text
        # used to do itself, one call recorded per NEW statement -- these tests
        # only assert on `calls`/counts, never on transaction atomicity (that's
        # covered against a REAL store below).
        stored, skipped, claim_ids = 0, 0, []
        for stmt in statements:
            if self.has_active_statement(org_id, project, stmt):
                skipped += 1
                continue
            self.remember_fact(org_id, project, who, stmt)
            stored += 1
            claim_ids.append(f"fake-{stored}")
        return {"stored": stored, "skipped": skipped, "claim_ids": claim_ids}


def test_ingest_text_stores_one_fact_per_chunk():
    store = _FakeStore()
    text = "para one here is fine.\n\npara two over there is also fine."
    out = ingest_text(store, text, who="w", org_id="o", project="p")
    assert out == {"chunks": len(store.calls), "stored": len(store.calls),
                   "skipped": 0, "source": None}
    assert len(store.calls) == 1  # short text -> single chunk
    assert all(c[:3] == ("o", "p", "w") for c in store.calls)


def test_ingest_text_appends_source_footer_when_given():
    store = _FakeStore()
    ingest_text(store, "a fact worth keeping around.", who="w",
                org_id="o", project="p", source="runbooks/deploy.md")
    stored_stmt = store.calls[0][3]
    assert stored_stmt.endswith("[source: runbooks/deploy.md]")


def test_ingest_text_without_source_stores_chunk_verbatim():
    store = _FakeStore()
    ingest_text(store, "a fact worth keeping around.", who="w",
                org_id="o", project="p")
    assert "[source:" not in store.calls[0][3]


# --------------------------------------------------------------------------
# finding #2: cap the resulting chunk count, checked BEFORE any store
# write (a huge document must not partially land).
# --------------------------------------------------------------------------
def test_ingest_text_raises_chunk_limit_exceeded_before_storing_when_over_cap(
        monkeypatch):
    from ergo.contradiction import ingest as ingest_mod
    from ergo.contradiction.ingest import ChunkLimitExceeded

    monkeypatch.setattr(ingest_mod, "chunk_text",
                        lambda text, *a, **kw: ["c1", "c2", "c3", "c4"])
    store = _FakeStore()
    with pytest.raises(ChunkLimitExceeded):
        ingest_mod.ingest_text(store, "irrelevant (chunk_text is mocked)",
                               who="w", org_id="o", project="p", max_chunks=2)
    assert store.calls == []  # nothing written once the cap trips


def test_ingest_text_max_chunks_none_is_unlimited_default():
    store = _FakeStore()
    out = ingest_text(store, "a fact worth keeping around.", who="w",
                      org_id="o", project="p")  # no max_chunks -> unaffected
    assert out["chunks"] == 1


def test_ingest_text_negative_max_chunks_is_a_value_error_not_a_chunk_limit():
    """A negative max_chunks is invalid configuration, not an input-too-large
    signal: len(chunks) > max_chunks would be true for ANY non-empty
    document against a negative cap, silently rejecting every legitimate
    ingest as ChunkLimitExceeded. Reject it explicitly instead."""
    store = _FakeStore()
    with pytest.raises(ValueError):
        ingest_text(store, "a fact worth keeping around.", who="w",
                    org_id="o", project="p", max_chunks=-1)
    assert store.calls == []


def test_ingest_text_max_chunks_zero_is_a_valid_degenerate_limit():
    from ergo.contradiction.ingest import ChunkLimitExceeded

    store = _FakeStore()
    with pytest.raises(ChunkLimitExceeded):
        ingest_text(store, "a fact worth keeping around.", who="w",
                    org_id="o", project="p", max_chunks=0)
    assert store.calls == []


# --------------------------------------------------------------------------
# Finding #10 (superseded by the atomic batch below): retry
# after a mid-document failure must not duplicate chunks.
#
# Review finding: ingest_text() now stores the whole
# batch via ONE store.remember_facts_batch() call -- a SINGLE `BEGIN
# IMMEDIATE` transaction for every chunk, not one transaction per chunk like
# the old per-chunk remember_fact() loop. This is what makes the active-claim
# cap check atomic (see store.py's QuotaExceeded docstring): a batch that
# would cross the cap must be refused before ANY chunk lands, which requires
# the cap check and every insert to share one transaction. The direct
# consequence is that a mid-batch failure now rolls back EVERYTHING (no
# partial commits) rather than leaving the chunks-before-the-failure
# committed -- still no duplicates on retry, just via all-or-nothing instead
# of partial-then-top-up.
# --------------------------------------------------------------------------
def test_ingest_text_batch_failure_rolls_back_atomically_then_retry_stores_all(
        monkeypatch):
    from ergo.contradiction import ingest as ingest_mod
    from ergo.contradiction.store import ContradictionStore

    # Isolate the dedup/batch behavior from chunk_text's own splitting rules:
    # fix the chunk set so "a multi-chunk document" doesn't depend on packing size.
    monkeypatch.setattr(ingest_mod, "chunk_text",
                        lambda text, *a, **kw: ["chunk one", "chunk two",
                                                 "chunk three", "chunk four"])

    real_store = ContradictionStore(":memory:")
    original_insert = real_store._insert
    calls = {"n": 0}

    def _flaky_insert(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated failure mid-batch")
        return original_insert(*a, **kw)

    monkeypatch.setattr(real_store, "_insert", _flaky_insert)

    with pytest.raises(RuntimeError):
        ingest_mod.ingest_text(real_store, "irrelevant text (chunk_text is mocked)",
                               who="w", org_id="o", project="p", source="doc.md")

    # ONE atomic transaction for the whole batch: the failure on chunk 3 rolls
    # back chunks 1 and 2 too -- nothing partially committed.
    active = real_store.active("o", "p")
    assert active.state == "project_missing"

    # Natural retry of the same document, now that the transient failure is gone.
    calls["n"] = 0
    monkeypatch.setattr(real_store, "_insert", original_insert)
    out = ingest_mod.ingest_text(real_store, "irrelevant text (chunk_text is mocked)",
                                 who="w", org_id="o", project="p", source="doc.md")

    active = real_store.active("o", "p")
    statements = [c["statement"] for c in active.claims]
    assert len(statements) == len(set(statements)) == 4  # no duplicates
    assert out["stored"] == 4
