# Retrieval — embeddings, sqlite-vec, hybrid scoring

How `GET /recall`, `GET /why`, and `POST /ingest` actually work. Source: `recall()` / `why()` / `_chain()` in [`ergo/contradiction/store.py`](../ergo/contradiction/store.py), [`ergo/embedders.py`](../ergo/embedders.py), [`ergo/contradiction/ingest.py`](../ergo/contradiction/ingest.py).

## The two halves of the engine

Ergo has two kinds of write path:

| path | endpoint | contradiction-checked? | embeds? |
|---|---|---|---|
| Decisions / constraints | `POST /remember`, `POST /learn` (source-attached), `POST /supersede` | yes (full pipeline) | yes |
| Reference content | `POST /ingest` (calls `remember_fact`) | **no** (skips normalize + judge) | yes |

Both populate the **same** SQLite + sqlite-vec store, so retrieval finds everything regardless of which path wrote it. The split is purely a performance optimization — skipping the LLM normalize per chunk is what makes bulk-document ingest orders of magnitude faster than routing the same content through the guarded write path.

## Embedder

[`ergo/embedders.py`](../ergo/embedders.py) — pluggable, dim-locked-on-first-write.

| provider | model | dim | needs | notes |
|---|---|---|---|---|
| `fastembed` *(default, baked)* | `BAAI/bge-small-en-v1.5` | 384 | nothing | ONNX, offline, ~130 MB in the image |
| `ollama` | `nomic-embed-text` | 768 | reachable Ollama | works against the host's local Ollama |
| `openai` | `text-embedding-3-small` | 1536 | `OPENAI_API_KEY` | hosted |
| `cohere` | `embed-english-v3.0` | 1024 | `COHERE_API_KEY` | hosted |
| `voyage` | `voyage-3-lite` | 512 | `VOYAGE_API_KEY` | hosted |

Pick with `ERGO_EMBED_PROVIDER`. Override the model with `ERGO_EMBED_MODEL` (and `ERGO_EMBED_DIM` if it differs from the provider default). See [CONFIGURATION.md](CONFIGURATION.md#embedder-the-retrieval-layer).

**Dim is locked at first write.** The `vec_claims` table declares `float[<dim>]` from the embedder's `dim` attribute. Switching to a different-dimension model later requires a full re-embed: [`ergo/contradiction/reembed.py`](../ergo/contradiction/reembed.py) rebuilds `vec_claims` at a new target dim into a brand-new file (never in-place). The engine itself still refuses writes on mismatch instead of silently mixing spaces.

**The model is baked into the image.** The Dockerfile does:

```dockerfile
RUN python -c "from fastembed import TextEmbedding; \
TextEmbedding(model_name='${ERGO_EMBED_MODEL}', cache_dir='/models/fastembed')"
```

…and the no-network boot test reloads it offline (`HF_HUB_OFFLINE=1 socket.getaddrinfo blocked`) to confirm there's no silent runtime fetch.

## The vec_claims table

Declared in `_init_vec()` of `ContradictionStore`:

```sql
CREATE VIRTUAL TABLE vec_claims USING vec0(
  claim_id  TEXT PRIMARY KEY,
  embedding float[384] distance_metric=cosine
);
```

- 1:1 with `claims.id`. Inserted right next to the claim insert, **inside the same write-guard transaction** (so vec rows can't get out of sync with claim rows).
- Embedding computed **outside the writer lock** (the embedder forward pass is slow; we don't hold the global SQLite write lock during it).
- `distance_metric=cosine` — sqlite-vec returns `distance` (lower = more similar); we convert to `vec_sim = 1 - distance` for the score.

## Why we don't delete superseded vectors

When `/supersede` runs:

1. **New** claim is inserted with its own vec row.
2. **Old** claim has `status` flipped to `superseded`. **Its vec row stays.**
3. `recall()` JOINs `vec_claims` → `claims` and filters on `status='active'`, so superseded vectors are excluded from results.

This avoids two failure modes:

- Expensive vec-index deletes on every supersede (sqlite-vec rebuilds aren't free).
- Loss of the old vector — if someone later wants to query history (`/history` or future "what did we believe at time T?" features), the embedding is still there to use.

Tradeoff: the index grows monotonically. With realistic write volume (a few hundred decisions / a few thousand ingest chunks per project) this is fine; sqlite-vec scales to ~10M rows without trouble.

## `recall()` — hybrid scoring

```python
def recall(self, org_id, project, query, limit=10, reasoned_only=False):
    qvec = self.embedder.embed(query)
    k = max(limit * 8, 40)                       # over-fetch to survive the JOIN filter
    rows = vec_claims MATCH ? AND k = ? ORDER BY distance
    for r in rows:
        c = JOIN claims WHERE status='active'    # superseded rows fall out here
        if reasoned_only and not c["reason"]:    # /why uses this
            continue
        vec_sim = 1.0 - r["distance"]            # cosine: 0 → identical
        kw      = lexical_overlap(query, statement)
        score   = 0.7 * vec_sim + 0.3 * kw
    return top N by score
```

### The 0.7 / 0.3 split

- **Vector (`vec_sim`)** captures semantic similarity — "github auth" matches a chunk about `.secrets/github.env` even if the chunk doesn't contain the word "auth".
- **Keyword (`kw`)** rescues exact-token cases — searching for `203.0.113.7` should *hit* a chunk containing that exact string, even if the surrounding semantic context drifts.

`kw` is **token-overlap fraction** (the fraction of query words present in the statement). Cheap, no extra index. SQLite doesn't ship `pg_trgm`; this approximates the same intent without the cost of a separate index.

The 0.7 / 0.3 weight isn't tuned beyond "vector should dominate; keyword is the tiebreaker". Easy follow-up: calibrate against a labeled retrieval set.

### Over-fetch (`k = limit × 8, min 40`)

The vec KNN returns `k` candidates by distance. Some get filtered out by the `status='active'` join (superseded), some by `reasoned_only` (when called from `/why`). Without over-fetching, the post-filter result set could be shorter than `limit`. 8× is empirical headroom.

### Output shape

```json
{
  "id": "...",
  "who": "alice",
  "statement": "...",
  "reason": "...",
  "project": "kb",
  "status": "active",
  "created_at": "...",
  "score":  0.7613,
  "vec_sim": 0.7661,
  "kw":     0.75
}
```

The three score fields are surfaced so a caller can see *why* a result ranked where it did (purely semantic vs lexical-dominated). Useful for debugging weird hits.

## `why()` — reasoned belief + history

```python
def why(self, org_id, project, query):
    hits = self.recall(org_id, project, query, limit=1, reasoned_only=True)
    if not hits:
        return None
    top = hits[0]
    return {
        "claim":   top["statement"],
        "reason":  top["reason"],
        "score":   top["score"],
        "history": self._chain(org_id, project, top["id"])
    }
```

### Why `reasoned_only=True`

A fact (kind = upsert, reason optional) can't win a "why" query — there's nothing to explain. Filtering at the recall level ensures the top hit is something that *carries* a reason. If no reasoned claim matches, return `null` (don't fake an explanation from a reason-less fact).

### `_chain()` — the supersede walk

```python
def _chain(self, org_id, project, claim_id):
    chain, cur = [], claim_id
    while cur:
        r = SELECT id, statement, reason, supersedes
            FROM claims WHERE org_id=? AND project=? AND id=?
        chain.append({"id": ..., "claim": ..., "reason": ...})
        cur = r["supersedes"]
    return chain
```

Newest → oldest. Each entry has its own claim text + reason, so the history reads as a sequence of decisions:

```json
[
  { "claim": "Never deploy on Tuesdays.", "reason": "incident risk outweighs on-call" },
  { "claim": "Deploy on Tuesdays.",        "reason": "team is on-call Mon/Wed" }
]
```

Project-scoped lookups + composite FK on `supersedes` mean the chain can't ever cross tenants.

## `/ingest` — chunking + fact fast-path

[`ergo/contradiction/ingest.py`](../ergo/contradiction/ingest.py).

```python
def ingest_text(store, text, who, org_id, project, source=None):
    chunks = chunk_text(text)                 # word-safe paragraph chunker
    for ch in chunks:
        stmt = f"{ch}\n\n[source: {source}]"
        store.remember_fact(org_id, project, who, stmt)
    return {"chunks": len(chunks), "stored": ...}
```

### Chunking (word-safe)

- Paragraph-aware: splits on `\n\s*\n` first.
- **Never splits mid-word.** If overlap would land in the middle of a token, it skips forward to the next space.
- Default size ≈ 1200 chars with ≈ 150-char overlap (tunable in `chunk_text`).
- Oversized single paragraphs are hard-split **on word boundaries**.

The v1 chunker sliced on character boundaries and produced chunks starting `"onsumer of this token..."`. The current chunker fixes that.

**Known wart:** the chunker can split an *attribute* from its *keyword* (e.g. a table row where the cell holding a hostname lands in chunk N but its IP address lands in chunk N+1) — a query for the label alone can miss the value it belongs to. Table-aware chunking is the obvious fix; not done yet.

### `remember_fact()` vs `remember()`

| | `remember_fact` | `remember` |
|---|---|---|
| LLM normalize | **no** | yes |
| Structural compare | **no** | yes |
| NLI | **no** | yes |
| Judge | **no** | yes |
| Embed | yes | yes |
| Insert claim | yes | yes |
| Insert vec_claims | yes | yes |
| Write-guard | yes | yes |
| Audit row | `action='remember_fact'` | `action='remember'` |

The fast-path is **why** the engine can bulk-ingest a batch of reference docs in a fraction of the time routing each one through the guarded write path would take. Use `remember_fact` for reference content; use `remember` for things you'd want refused if they contradict existing belief.

## Latency budget

On the merged image, against a project with ~100 active claims:

| op | typical | dominant cost |
|---|---|---|
| `/ingest` per chunk | ~30–50 ms | embed (~10–20 ms) + write-guard insert |
| `/recall` per query | ~20–50 ms | embed query + vec KNN + JOIN + score |
| `/why` per query | ~25–60 ms | recall + chain walk (usually 1–2 hops) |
| `/remember` per write | ~100–250 ms (cache miss) / ~10–40 ms (hit) | dominated by LLM normalize |

## Open follow-ups

1. **Table-aware chunking.** Detect markdown tables and keep rows intact across chunk boundaries.
2. **Calibrate the 0.7 / 0.3 hybrid weight.** Build a labeled retrieval set (similar to `evals/prose/dataset.jsonl` for the firewall) and pick the F1-maximizing weight.

*(Re-embed migration shipped 2026-07-22, private-repo issue #126: swapping embedder provider/dimension no longer means "rebuild the DB by hand" — [`ergo/contradiction/reembed.py`](../ergo/contradiction/reembed.py) rebuilds the SQLite file offline into a brand-new file (backup-first, dry-run, active-claim count verify, never in-place). It's an operator-run offline tool, not an HTTP route — no `POST /reembed`.)*

*(Retraction support shipped 2026-07-02: [`POST /retract`](HTTP-API.md#post-retract--wrong-at-birth-removal) flips a claim to `status='retracted'`, and retracted rows are filtered out of recall/why the same structural way superseded ones are.)*

## Related

- HTTP surface for these endpoints → [HTTP-API.md](HTTP-API.md)
- The schema for `claims` + `vec_claims` → [STORAGE.md](STORAGE.md)
- The *other* engine half (contradiction pipeline) → [CONTRADICTION-PIPELINE.md](CONTRADICTION-PIPELINE.md)
