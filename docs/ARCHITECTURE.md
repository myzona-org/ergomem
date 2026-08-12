# Architecture

Markdown twin of [`architecture.html`](architecture.html). One product, one engine, one container. **One SQLite file holding both the claim store and the sqlite-vec KNN index** — same DB, different tables.

## TL;DR

```
                ┌──────────────────────────────────────────┐
   clients ────▶│        HTTP API (stdlib, bearer auth)    │
                │                                          │
                │  POST /remember  /learn  /supersede      │   ┌─ guarded write path
                │     │ normalize → structural → NLI → judge   │  (contradiction firewall)
                │     ▼                                        │
                │  POST /ingest    [chunk → embed → write]  ───┤─ fact fast-path
                │                                              │
                │  GET  /recall    [embed → vec KNN → JOIN]  ──┤─ read path
                │  GET  /why       [recall(reasoned) → chain]  ┘   (retrieval + history)
                │                                          │
                │  POST /retract   [flip status → audit]   │──── wrong-at-birth removal
                │                                          │     (no gate, no new row)
                │  GET  /health  /diagnose /active /history│
                └──────────────────────────────────────────┘
                                │
                                ▼
                ┌──────────────────────────────────────────┐
                │  ONE SQLite file (ergo.db)               │
                │                                          │
                │  claims        vec_claims (sqlite-vec)   │
                │  writes        conflict_events   projects│
                └──────────────────────────────────────────┘


Baked in image:  fastembed (bge-small, 384d) + NLI deberta-v3 (ONNX) + sqlite-vec
Remote at run:   instruct LLM (LiteLLM/Anthropic; Ollama fallback for air-gap)
```

## Components

### HTTP API — [`ergo/contradiction/server.py`](../ergo/contradiction/server.py)
- Python stdlib `http.server` — no framework dep.
- **Bounded threaded server**: `ThreadingHTTPServer` gives each ADMITTED request its own thread (the ~1-3s LLM normalizer call for one guarded write never blocks an already-admitted request), but admission itself is bounded: every accepted connection acquires a permit from ONE per-instance `threading.BoundedSemaphore(ERGO_HTTP_MAX_WORKERS, default 8)` — built once in `BoundedThreadingHTTPServer.__init__`, never a rebuildable module-level global — BEFORE a worker thread is even spawned, so a connection burst queues at the OS accept() level instead of spawning unbounded Python threads. If every permit is held by slow writes, a new connection (even `/health`) waits for one to free up. Below the HTTP layer, the store opens its sqlite connection with `check_same_thread=False` and guards every public method with its OWN `RLock`; the write-guard's `BEGIN IMMEDIATE` + `SQLITE_BUSY` retry remains the per-database serialization of record.

- Bearer resolves server-side to a **principal** (`org_id`/`who`/`role`): the legacy `ERGO_API_TOKEN` (or no token — open dev/test mode) keeps today's body-driven behavior against the single default store; any other bearer is rejected (401) -- this build has no control plane to resolve it against. `/health` always open for liveness probes.
- Reference: [HTTP-API.md](HTTP-API.md).

### Contradiction pipeline — `ergo/contradiction/{normalize,structural,nli,judge}.py`
- Four stages: **normalize** (text → structured claim) → **structural compare** (deterministic, same-subject + opposing modality) → **NLI residual** (gated, only on fuzzy cases) → **judge** (combines into block / warn / clean tier).
- Runs only on `/remember`, `/learn`, and `/supersede`. `/ingest` skips it.
- Reference: [CONTRADICTION-PIPELINE.md](CONTRADICTION-PIPELINE.md).

### Retrieval — `recall()` / `why()` in [`store.py`](../ergo/contradiction/store.py), [`ingest.py`](../ergo/contradiction/ingest.py), [`embedders.py`](../ergo/embedders.py)
- Embedder (default fastembed bge-small, 384d, baked) embeds claims at write time and queries at read time.
- `vec_claims` virtual table (sqlite-vec, cosine KNN) lives **in the same SQLite file** as `claims`.
- `recall()` joins vec hits against `claims` filtered on `status='active'` — superseded / retracted rows are structurally excluded without index deletes.
- `recall()` blends `0.7 × vec_sim + 0.3 × keyword_overlap`.
- `why()` is `recall(reasoned_only=True)` + walk the supersede chain.
- Embedding dim is fixed at `vec_claims` creation time. Switching embedder/provider/dim is a migration, never an in-place `ALTER`: [`ergo/contradiction/reembed.py`](../ergo/contradiction/reembed.py) (private-repo issue #126) rebuilds the SQLite file offline into a brand-new file (backup-first, dry-run, active-claim count verify).
- Reference: [RETRIEVAL.md](RETRIEVAL.md).

### Storage — `ergo/contradiction/store.py`
- Single SQLite file (`ERGO_DB_PATH`, default `/data/ergo.db`), WAL mode.
- 5 tables: `claims` (the truth), `vec_claims` (KNN index), `writes` + `conflict_events` (audit), `projects` (registry).
- Write-guard: race-safe optimistic concurrency via `BEGIN IMMEDIATE` + `preflight_seq` recheck. `SQLITE_BUSY` is a first-class retry, not a 500.
- Composite FK `(org_id, project, supersedes) → claims(org_id, project, id)` — cross-tenant supersession is structurally impossible.
- `status` lifecycle: `active → superseded | retracted | evicted` — all terminal, all excluded from every active-set read path. `evicted` (private-repo issue #123) is the retention sweep's outcome: audit-loud (`evicted_reason`/`evicted_at` on the row, a `writes` audit entry), never a hard delete, off by default per tenant.
- A tenant can self-serve its own `writes` + `conflict_events` rows via `GET /audit` (private-repo issue #124) — same tenant-isolation guarantee as `/active`/`/history`.
- Reference: [STORAGE.md](STORAGE.md).


### Image — [`Dockerfile`](../Dockerfile)
- `python:3.12-slim` base + `pip install ".[engine]"` (fastembed + onnxruntime + tokenizers + numpy + huggingface_hub + sqlite-vec + httpx).
- **Bakes**: fastembed bge-small (~130 MB), NLI deberta-v3 quantized (~190 MB).
- **Remote**: the instruct LLM (Anthropic/LiteLLM/Ollama) — the only network dependency at runtime.
- No-network boot test in build (CI hard gate): blocks `socket.getaddrinfo` and reloads embedder + NLI + sqlite-vec offline. Build fails on any silent fetch.
- Current image size: ~1.06 GB (down from ~1.14 GB after the v1 trim).

## Data flow — five lanes

These match the five colored bands in [`architecture.html`](architecture.html).

### Lane 1: `POST /remember` (amber, **guarded write**)

`POST /learn` rides this same lane: it is `remember` with a required `source` folded into the stored reason (provenance), running the identical pipeline and returning the same `409` on conflict.

```
input            normalize             structural           NLI residual (gated)      judge      write-guard
─────────        ────────────          ────────────         ─────────────────────     ───────    ──────────────
{statement,   →  RuleNorm | LLMNorm  → same subject +    →  fuzzy only;            →  block →   BEGIN IMMEDIATE
 reason,         outside lock          overlapping scope     polarity-flip + overlap   warn       seq recheck
 who, org,                             + opposing modality   gates (NLI_OVERLAP_       clean      INSERT claim
 project}                              → HIGH (block)        FLOOR=0.25)                          INSERT vec
                                       → MED (warn)                                              audit writes
                                       → LOW (defer NLI)                                         audit conflicts
                                                                                                  COMMIT
                                                                                                  → 200 Stored
                                                                                                  → 409 Conflict
```

### Lane 2: `POST /ingest` (green, **fact fast-path**)

```
input               chunker (word-safe)        embed                  remember_fact()       write-guard
─────────           ──────────────────         ───────────            ─────────────────     ──────────────
{text,        →  paragraph-aware,           → fastembed bge-small  → SKIP normalize     → INSERT claim
 source?,        ~1200 chars w/ overlap,      dim=384,                SKIP judge          INSERT vec
 who, org,       never mid-word               outside lock                                audit writes
 project}                                                                                  COMMIT
                                                                                          → 200 {chunks, stored}
                                                                                          ~28 chunks/sec
```

### Lane 3: `POST /supersede` (purple, **change of mind**)

```
lookup existing     normalize + embed         judge vs OTHERS          link chain         write-guard
─────────────       ──────────────────        ──────────────────       ──────────────     ──────────────
WHERE org+proj+    → outside lock          → exclude the           → old.status =       BEGIN IMMEDIATE
 id, status=                                  to-be-superseded         'superseded'        seq recheck
 'active'                                     row from candidates      new.supersedes      UPDATE old
(composite-FK,                                                          = old.id           INSERT new + vec
not just id)                                                                                 audit writes
                                                                                              audit (resolution='superseded')
                                                                                              COMMIT → 200
```

### Lane 4: `GET /recall` (blue, **hybrid read**)

```
embed(query)      sqlite-vec KNN           JOIN claims              hybrid score         sort + limit
──────────        ──────────────────       ──────────────────       ──────────────       ──────────────
bge-small         vec_claims MATCH ?     → status='active'        → 0.7 * vec_sim       → top N
dim=384            AND k = limit×8         (superseded rows          + 0.3 * kw            no writes
                   ORDER BY distance       fall out here)             (token overlap)      → {results: [...]}
```

### Lane 5: `GET /why` (teal, **reasoned + history**)

```
recall(reasoned_only=True)   top reasoned hit         walk supersede chain     return
──────────────────────────   ──────────────────       ──────────────────       ──────────────────
facts can't win;             {claim, reason,          while supersedes:        {claim, reason,
 only entries that            score}                    chain.append(...)        score,
 carry a reason                                         cur = next               history: [newest→oldest]}
                                                                                  null if no match
```

## Why one product, not two

The `claim` is the universal unit — same row whether it came in via:

- a contradiction-checked decision (`POST /remember`),
- a source-attached belief (`POST /learn` — same gate, provenance folded into the reason),
- a bulk doc chunk (`POST /ingest` calling `remember_fact`),
- a supersede (`POST /supersede`).

All four paths feed the same `claims` + `vec_claims` rows, with the same write-guard, the same audit trail, the same retrieval-time JOIN on `status='active'`. The split between "decision write" and "fact ingest" is a performance optimization (skip the LLM normalize for reference content), not a separate product.

This wasn't always true. Pre-2026-06-27 the retrieval verbs lived in a separate Postgres+pgvector tree (`ergo/store.py`, `db.py`, etc.) that wasn't in the image. A later merge folded retrieval into `ergo/contradiction/store.py`, and retired the Postgres tree.

## Where things live (file map)

```
ergo/
├── __init__.py                       # version
├── embedders.py                      # FastEmbed + ollama/openai/cohere/voyage providers
├── mcp/
│   └── server.py                     # stdio MCP adapter (thin client over the HTTP API)
└── contradiction/
    ├── __init__.py
    ├── server.py                     # HTTP API (the only entry point)
    ├── store.py                      # ContradictionStore — claims + write-guard + recall/why
    ├── ingest.py                     # word-safe chunker + bulk fact ingest
    ├── normalize.py                  # RuleNormalizer + LLMNormalizer (anthropic/openai/litellm/ollama)
    ├── structural.py                 # deterministic verdict comparator
    ├── judge.py                      # combine structural+NLI → tier (block/warn/clean)
    ├── nli.py                        # ONNX NLI wrapper (no torch)

    └── extract.py                    # markdown→claim-candidate extraction (eval-side)

tests/                                # SQLite-only, no DB/network needed in CI -- pytest tests -q for the current count
docker/
└── boot-test.py                      # no-network boot gate (the hard CI rule)
docs/                                 # you are here
```

## Operational provenance (anti-zombie)

The running binary answers for itself — never delegate to a README. `/diagnose` surfaces:

- `owner_name` + `owner_contact` (from `ERGO_OWNER` / `ERGO_OWNER_CONTACT`)
- `backup_path` + `last_backup_at` (from `ERGO_BACKUP_PATH` + the file's mtime)
- `last_write_at` (live from `claims` table)
- `kill_review_date` + status (from `ERGO_KILL_REVIEW_DATE`, with `OVERDUE` flag)
- `warnings[]`: missing owner, no backup, version drift between stored and running engine, etc.

A deployment that someone stood up and forgot can't quietly drift. Either `/diagnose` is healthy and the warnings list is clean, or it tells you exactly what's wrong.

## Related

- HTTP surface → [HTTP-API.md](HTTP-API.md)
- Env vars + provider config → [CONFIGURATION.md](CONFIGURATION.md)
- DB schema + write-guard → [STORAGE.md](STORAGE.md)
- Pipeline internals → [CONTRADICTION-PIPELINE.md](CONTRADICTION-PIPELINE.md)
- Embedding + scoring → [RETRIEVAL.md](RETRIEVAL.md)
- Visual diagram (interactive) → [architecture.html](architecture.html)
