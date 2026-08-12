# Storage — SQLite schema, write-guard, backups

One SQLite file at `ERGO_DB_PATH` (default `/data/ergo.db`). All five tables below live in this one file. WAL mode is enabled. Source: [`ergo/contradiction/store.py`](../ergo/contradiction/store.py).


## Tables

### `claims` — the truth

The append-only history of every statement ever written, plus current `status`. This is the source of truth; everything else either indexes it (`vec_claims`) or audits it (`writes`, `conflict_events`).

```sql
CREATE TABLE claims (
  id                  TEXT NOT NULL,            -- ULID (hex)
  org_id              TEXT NOT NULL,
  project             TEXT NOT NULL,
  who                 TEXT NOT NULL,
  statement           TEXT NOT NULL,
  reason              TEXT,                     -- nullable for facts
  norm                TEXT,                     -- JSON: modality, subject, scope, valid_from/until, ...
  status              TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'superseded' | 'retracted' | 'evicted'
  supersedes          TEXT,                     -- previous claim id in the chain
  claim_seq           INTEGER NOT NULL,         -- monotonic per (org_id, project)
  conflict_exception  TEXT,                     -- reason string if stored via force_exception
  normalizer_id       TEXT NOT NULL,            -- e.g. "RuleNormalizer", "OpenAINormalizer"
  normalizer_version  TEXT NOT NULL,
  norm_schema_version TEXT NOT NULL,
  judge_version       TEXT NOT NULL,            -- e.g. "structural-1+nli-deberta-v3"
  created_at          TEXT NOT NULL,            -- ISO UTC
  retract_reason      TEXT,                     -- why the claim was wrong at birth (set on retract)
  retracted_at        TEXT,                     -- ISO UTC (set on retract)
  evicted_reason       TEXT,                     -- which retention knob(s) fired (set on evict)
  evicted_at           TEXT,                     -- ISO UTC (set on evict)

  PRIMARY KEY (id),
  UNIQUE (org_id, project, id),
  UNIQUE (org_id, project, claim_seq),
  FOREIGN KEY (org_id, project, supersedes)
    REFERENCES claims(org_id, project, id),
  CHECK (length(trim(statement)) > 0),
  CHECK (status IN ('active', 'superseded', 'retracted', 'evicted')),
  CHECK (claim_seq > 0)
);

CREATE INDEX idx_claims_active  ON claims(org_id, project, status);
CREATE INDEX idx_claims_history ON claims(org_id, project, claim_seq);
```

**Field notes:**

- `norm` (JSON) is the **persisted normalized form** — modality, subject, object, `value` (the value-bearing object selector: colour/port/version/…, added 2026-06-27), scope (env/region/team), `valid_from` / `valid_until`. Stored once at write time so the judge never re-normalizes on read (cost discipline + reproducibility on re-judge). The `value` field is additive and nullable, so it did **not** bump `norm_schema_version` (= `1`) — old norms without the key read back as `value: None` and the structural comparator uses `norm.get("value")`, so `/diagnose` reports no drift.
- `supersedes` is a **composite-FK** `(org_id, project, supersedes) → claims(org_id, project, id)`. Cross-tenant supersession is structurally impossible.
- `claim_seq` is monotonic *per (org_id, project)*. The write-guard uses it as the optimistic-concurrency token (see below).
- Version columns (`normalizer_id`, `normalizer_version`, `judge_version`, `norm_schema_version`) are recorded on every row so `/diagnose` can detect **drift** between the running engine and stored history (a data-compat hazard).
- `retract_reason` / `retracted_at` (added 2026-07-02 with `POST /retract`) are **additive + nullable**, so no `norm_schema_version` bump (same precedent as the norm `value` field). Because the schema script is `CREATE TABLE IF NOT EXISTS`, pre-existing DBs get them via an **idempotent migration at store init** (`PRAGMA table_info` check + `ALTER TABLE ADD COLUMN`).
- `evicted_reason` / `evicted_at` (added with private-repo issue #123's retention sweep) are **additive + nullable**, same precedent as `retract_reason`/`retracted_at`. Unlike those two, adding the `'evicted'` status ALSO widens the `status` CHECK constraint, which SQLite cannot do via `ALTER TABLE` — a pre-existing DB gets the standard [12-step SQLite table rebuild](https://sqlite.org/lang_altertable.html#otheralter) at store init instead: build a `claims_new` with the widened CHECK inside one transaction (`PRAGMA foreign_keys=OFF`), copy every row across, drop the old table, rename the new one into place, recreate the two indexes, verify with `PRAGMA foreign_key_check` **before** committing, then re-enable FK enforcement. Detected via `sqlite_master.sql` containing the literal `'evicted'` substring, so a DB already on the new schema is a single cheap `SELECT` and nothing else — safe to run on every open.

**Status lifecycle** — every transition is terminal; every active-set read path (`/active`, `/recall`, `/why`, the contradiction judge) sees only `status='active'`:

```
active ──supersede──▶ superseded   decision CHANGED: new linked row carries the why-chain
active ──retract────▶ retracted    claim was WRONG AT BIRTH: no new row; reason + timestamp
                                   land on the row itself (retract_reason, retracted_at)
active ──evict───────▶ evicted     retention TTL/max-count fired: no new row, no claim_seq
                                   consumed, no conflict_events row; reason + timestamp land
                                   on the row itself (evicted_reason, evicted_at)
```

### `vec_claims` — sqlite-vec virtual table (KNN index)

```sql
CREATE VIRTUAL TABLE vec_claims USING vec0(
  claim_id  TEXT PRIMARY KEY,
  embedding float[384] distance_metric=cosine
);
```

- 1:1 with `claims.id` — populated on insert by `/remember`, `/learn`, `/ingest`, `/supersede` (the new row only).
- `float[384]` matches the default fastembed `bge-small-en-v1.5`. If you switch embedder to a different dimension, **rebuild the DB** — the engine refuses writes on dim mismatch (`ERGO_EMBED_DIM` enforced at first write).
- **Superseded / retracted rows are NOT deleted from `vec_claims`.** The `/recall` query JOINs back to `claims` on `status='active'`, so their vectors are structurally excluded from results without paying for expensive index deletes.
- **Evicted rows ARE the one exception: their `vec_claims` row IS deleted** as part of the same `BEGIN IMMEDIATE` eviction transaction. Retention exists to shrink a tenant's footprint (data lifecycle, not just belief-set membership), so leaving the vector behind forever would defeat that purpose; the `status='active'` JOIN filter would already exclude it from `/recall` results either way, so this is a cleanup optimization, not a correctness requirement.

### `projects` — registry

```sql
CREATE TABLE projects (
  org_id     TEXT NOT NULL,
  project    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, project)
);
```

Used by `/active`'s three-state read (`project_missing` vs `empty` vs `has_active_claims`) so the engine never returns an ambiguous "no claims".

### `writes` — audit (every write action)

```sql
CREATE TABLE writes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id        TEXT NOT NULL,
  project       TEXT NOT NULL,
  who           TEXT NOT NULL,
  action        TEXT NOT NULL,  -- 'remember' | 'remember_fact' | 'supersede' | 'retract' | 'store_with_exception'
  claim_id      TEXT,
  preflight_seq INTEGER,
  commit_seq    INTEGER,
  ts            TEXT NOT NULL
);
```

Every successful write produces a `writes` row. `preflight_seq` and `commit_seq` are the snapshot points (see write-guard below) — useful for forensics on a race.

A caller can read its own `writes` + `conflict_events` rows via [`GET /audit`](HTTP-API.md#get-audit--tenant-facing-audit-trail-private-repo-issue-124) (private-repo issue #124).


### `conflict_events` — audit (every flagged claim)

```sql
CREATE TABLE conflict_events (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id              TEXT NOT NULL,
  project             TEXT NOT NULL,
  who                 TEXT NOT NULL,
  incoming            TEXT NOT NULL,            -- the statement that was being written
  against             TEXT,                     -- id of conflicting claim
  stored_claim_id     TEXT,                     -- id ultimately stored (null if blocked)
  tier                TEXT,                     -- 'block' | 'warn'
  score               REAL,
  source              TEXT,                     -- e.g. 'structural', 'nli'
  reason              TEXT,
  resolution          TEXT,                     -- 'flagged' | 'warned' | 'exception' | 'superseded'
  exception_reason    TEXT,
  preflight_seq       INTEGER,
  commit_seq          INTEGER,
  judge_version       TEXT,
  normalizer_version  TEXT,
  ts                  TEXT NOT NULL
);
```

`resolution` is what to filter on: `flagged` = blocked (write refused), `warned` = MED-tier surfaced but stored, `exception` = stored anyway via `force_exception`, `superseded` = the conflict was resolved by a supersede.

## The write-guard (race-safe commit)

`/remember` and `/supersede` go through this same primitive. Source: `_remember_attempt` / `_supersede_attempt` in `store.py`.

```
1. PREFLIGHT (no SQLite write transaction yet, but held under the store's
   per-instance `threading.RLock` for the whole call — see below)
   - normalize statement              (slow, may call LLM)
   - embed                            (slow, model inference)
   - load active claim set
   - run judge → blocking + warnings
   - record preflight_seq = MAX(claim_seq) for (org_id, project)

2. If blocking & not force_exception:
   - BEGIN IMMEDIATE
   - re-check MAX(claim_seq) == preflight_seq  → if not, _Retry
   - re-check each blocking claim is STILL active  → if not, _Retry
   - audit each blocking row to conflict_events (resolution='flagged')
   - audit warnings (resolution='warned')
   - COMMIT
   - return Conflict(conflicts=[...], warnings=[...])

3. Else (commit path):
   - BEGIN IMMEDIATE
   - re-check MAX(claim_seq) == preflight_seq  → if not, _Retry
   - commit_seq = preflight_seq + 1
   - INSERT claim (with persisted norm)
   - INSERT vec_claims (if retrieval enabled)
   - ensure_project()
   - audit writes (action='remember' | 'remember_fact' | 'supersede' | 'store_with_exception')
   - audit force-excepted blocks and warnings to conflict_events
   - COMMIT
   - return Stored(claim_id, conflicts, warnings)
```

**Key properties:**

- **Normalize + embed run INSIDE the per-instance store lock** (`threading.RLock`, held for the full duration of the public method call — see below). This is a deliberate change from the pre-multi-tenancy single-threaded model, where the whole server was single-threaded and there was no separate lock to hold: a slow 200-ms-plus LLM normalizer call for one caller only ever blocks other callers of the SAME store.

- **SQLITE_BUSY / SQLITE_LOCKED are first-class retryable outcomes**, not 500s. Bounded exponential backoff with jitter, capped at `max_retries=8`.
- **The retry path re-judges from a fresh snapshot** — never silent inserts on stale data.
- **`supersede()` is project-scoped** by composite FK — a request targeting an existing id in a different `(org_id, project)` cannot succeed.
- **`retract()` reuses the same concurrency discipline** (preflight active-claim check + `preflight_seq`, `BEGIN IMMEDIATE`, still-active re-check under the lock, `_Retry` loop) but runs **no judge and no insert**: it flips `status`, sets `retract_reason`/`retracted_at`, and audits `action='retract'`. No `claim_seq` is consumed and no `conflict_events` row is written (nothing was judged).
- **`evict()` (private-repo issue #123, retention sweep) is simpler still** — no preflight/`_Retry` loop at all: candidate selection and the batch of status flips happen inside ONE `BEGIN IMMEDIATE`, each `UPDATE ... WHERE id=? AND status='active'` re-checking that exact claim is still active (so a claim retracted/superseded/evicted by a concurrent write between selection and commit is silently skipped, never double-evicted or clobbered), one `_audit_write(..., "evict", claim_id, ...)` row per successful eviction, and the matching `vec_claims` row deleted. Off by default at the query level: if neither `ttl_days` nor `max_active` is passed, candidate selection short-circuits to an empty list before any SQL runs at all. This is a store-level capability only in this build — there is no HTTP route that drives it.

## Empty-store states

`/active` returns a three-state typed result so "no claims" is never ambiguous:

| state | meaning |
|---|---|
| `project_missing` | no row in `projects` for `(org_id, project)` — typo, wrong tenant, or never used |
| `empty` | project exists, zero active claims |
| `has_active_claims` | scope has one or more active claims |

This avoids the "I asked for X, got nothing back, did the store break?" failure mode.

**`/active` is paginated (private-repo issue #185), and `state` describes the SCOPE, not
the page returned.** A capped `limit`/`offset` read at an `offset` past the
end of a populated scope comes back with an empty `claims` list but is still
`has_active_claims` — `state` is computed from a separate `COUNT(*)` of the
scope, never from how many rows the current page happened to return, so it
can never be mistaken for `empty`. See [HTTP-API.md](HTTP-API.md#get-active--list-active-claims) for the response shape (`count`/`returned`/`truncated`/`next_offset`).

## Backup

**Never `cp ergo.db` while the engine is running.** WAL mode keeps the active write-ahead log in `ergo.db-wal` and the shared-memory index in `ergo.db-shm` — a raw file copy of just `ergo.db` while writes are flowing can corrupt the destination.

Three safe options:

### 1. Stop the container, then copy

```bash
docker stop ergo
docker cp ergo:/data/ergo.db ./ergo-$(date +%F).db    # safe: nothing writing
docker start ergo
```

Acceptable downtime is usually a few seconds.

### 2. SQLite online-backup API (online, while engine is running)

What a nightly host-side backup job should use. Works concurrently with writes (the
source is briefly locked per page). From inside the container:

```python
import sqlite3
src = sqlite3.connect("/data/ergo.db")
dst = sqlite3.connect("/data/ergo-backup.db")
with dst:
    src.backup(dst)
```

### 3. `VACUUM INTO` (online, compacted snapshot)

Produces a clean, fully-vacuumed snapshot without blocking writes. **The slim
image has no `sqlite3` CLI**, so run it through python:

```bash
docker exec ergo python -c "import sqlite3; sqlite3.connect('/data/ergo.db').execute(\"VACUUM INTO '/data/ergo-$(date +%F).db'\")"
docker cp ergo:/data/ergo-$(date +%F).db ./
```

### Restore

Stop the container, replace `/data/ergo.db` (and remove any stale `-wal` / `-shm` siblings), restart:

```bash
docker stop ergo
docker run --rm -v ergo-data:/data alpine sh -c \
  'rm -f /data/ergo.db /data/ergo.db-wal /data/ergo.db-shm'
docker cp ./ergo-snapshot.db ergo:/data/ergo.db
docker run --rm -v ergo-data:/data alpine chown -R 1000:1000 /data
docker start ergo
curl -s http://127.0.0.1:8788/health    # confirm probe_state reasonable
```

(Container is `ergo`; the live volume is `ergo-data`. The old
`ergo-merged-data` volume is retained only as the pre-2026-07-13 archive.)

## Hardware / FS rules

- **Local disk only.** WAL doesn't behave reliably on network filesystems (NFS, SMB, etc.). Don't put `/data` on a remote mount.
- **One writer at a time, per database.** The HTTP server runs a **bounded** `ThreadingHTTPServer` (`ERGO_HTTP_MAX_WORKERS`, default 8 — see [DEVELOPMENT.md](DEVELOPMENT.md#style--conventions)), not a single thread, so requests for *different* tenants (or a legacy store vs. a tenant store) genuinely run in parallel. Within any ONE database file, writes still serialize: each store holds its own `threading.RLock` around every public method plus the `BEGIN IMMEDIATE` + `SQLITE_BUSY` retry above — that per-database serialization is unchanged, it just no longer means the whole *server* is single-threaded.

## Related

- HTTP API surface → [HTTP-API.md](HTTP-API.md)
- Config (env vars, providers) → [CONFIGURATION.md](CONFIGURATION.md)
- Embedding + recall scoring → [RETRIEVAL.md](RETRIEVAL.md)
