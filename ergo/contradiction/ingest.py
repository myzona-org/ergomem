"""Document ingest for the unified Ergo engine.

Chunks text on WORD/paragraph boundaries (never mid-word - the v1 ingest sliced
mid-token, which produced ugly chunk starts like "onsumer of this token") and
stores each chunk via the fact fast-path (remember_fact): embedded and
recall-able, but NOT run through the LLM normalizer or contradiction judge, and
never checked against by later guarded writes. This is the deliberate ungated
path for bulk reference content; POST /learn is the guarded bridge for a belief
formed from a source that you DO want contradiction-checked.
"""
from __future__ import annotations

import re
from pathlib import Path

TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".rst"}


class ChunkLimitExceeded(ValueError):
    """Raised by `ingest_text` when the input produces more chunks than
    `max_chunks` allows (finding #2) -- the HTTP layer maps this to 413.
    Checked BEFORE any chunk is stored, so an oversized document never lands
    partially."""


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Paragraph-aware, word-safe chunking.

    Packs paragraphs up to `size`; carries a word-aligned overlap tail for context
    continuity; hard-splits any oversized single paragraph on WORD boundaries. No
    output chunk ever starts or ends mid-word.
    """
    size = max(1, size)
    overlap = max(0, min(overlap, size - 1))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > size:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            # never start the overlap tail mid-word: drop the leading partial token
            if tail and not buf[-overlap - 1: -overlap].isspace() and " " in tail:
                tail = tail[tail.index(" ") + 1:]
            buf = (tail + "\n\n" + p) if tail else p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)

    # hard-split oversized single paragraphs on word boundaries (never mid-word)
    out: list[str] = []
    for c in chunks:
        if len(c) <= size * 2:
            out.append(c)
            continue
        cur = ""
        for w in c.split():
            if cur and len(cur) + len(w) + 1 > size:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}" if cur else w
        if cur:
            out.append(cur)
    return out


def ingest_text(store, text: str, *, who: str, org_id: str, project: str,
                source: str | None = None, max_chunks: int | None = None,
                max_active_claims: int | None = None) -> dict:
    """Chunk `text` and store every chunk via the fact fast-path.

    Document chunks are facts (reference content), so they use
    remember_facts_batch(): embedded + stored for recall, but NOT run through
    the per-chunk LLM normalizer + contradiction judge (which made bulk
    ingest O(LLM calls)). Decisions you want contradiction-checked go through
    remember()/POST /remember instead.

    A chunk whose exact statement text is already an active claim in scope is
    skipped rather than re-stored, so re-running this on the same document after
    a mid-way failure (retry_exhausted, a dropped connection, ...) does not
    permanently duplicate the chunks that already committed.

    `max_chunks` (finding #2) caps the resulting chunk count -- checked
    BEFORE the store loop, so a document that blows the cap never partially
    lands. None (the default) is unlimited, for callers other than the HTTP
    layer that don't want the cap. 0 is a valid (if degenerate) limit --
    "no chunks allowed"; a negative value is invalid configuration, not an
    input-too-large signal, and raises ValueError rather than silently
    rejecting every non-empty document as ChunkLimitExceeded.

    `max_active_claims`: if set (and > 0), the ENTIRE batch is
    inserted in one atomic transaction (remember_facts_batch) that checks the
    tenant's active-claim cap once, against the dedup-aware count of NEW
    chunks this call would actually add -- a batch that would cross the cap
    raises store.QuotaExceeded and nothing is stored, not even the chunks
    that would have fit. None or <=0 means unlimited (no check).
    """
    if max_chunks is not None and max_chunks < 0:
        raise ValueError(f"max_chunks must be >= 0, got {max_chunks}")
    chunks = chunk_text(text)
    if max_chunks is not None and len(chunks) > max_chunks:
        raise ChunkLimitExceeded(
            f"input produced {len(chunks)} chunks, exceeding the "
            f"{max_chunks}-chunk limit")
    statements = [ch if not source else f"{ch}\n\n[source: {source}]" for ch in chunks]
    result = store.remember_facts_batch(org_id, project, who, statements,
                                        max_active_claims=max_active_claims)
    return {"chunks": len(chunks), "stored": result["stored"],
            "skipped": result["skipped"], "source": source}


def ingest_file(store, path, *, who: str, org_id: str, project: str) -> dict:
    p = Path(path).expanduser()
    if p.suffix.lower() not in TEXT_EXTS:
        raise ValueError(f"unsupported file type {p.suffix!r} (text/markdown only)")
    return ingest_text(store, p.read_text(encoding="utf-8", errors="replace"),
                       who=who, org_id=org_id, project=project, source=str(p))
