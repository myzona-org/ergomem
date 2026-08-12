"""Contradiction-aware claim store (SQLite) — write-guard at the store layer.

Hardened after a 2026-06-25 code review:

- SQLITE_BUSY / SQLITE_LOCKED are first-class retryable outcomes via explicit
  `BEGIN IMMEDIATE` + bounded backoff (#1).
- `supersede()` is a guarded write: normalize OUTSIDE the writer slot, judge
  against the rest of the active set excluding the to-be-superseded id, project-
  scoped lookup + composite FK so cross-project supersession is structurally
  impossible (#2, #3, #4).
- The persisted `norm` JSON is the source of truth for re-comparison — candidates
  carry their stored norm, the judge skips re-normalization (#6).
- Warning-tier contradictions are surfaced and audited (#7).
- `store_with_conflict_exception` writes a proper `conflict_events` row per
  flagged claim with the score/source/reason (#8).
- Composite `UNIQUE (org_id, project, claim_seq)`, `UNIQUE (org_id, project, id)`,
  and CHECK constraints harden the schema (#9, #10).
- Version-drift columns persisted on every row so a normalizer/judge swap is a
  detectable data-compat event (#33).
- `active()` / `history()` return typed three-state results so empty-store
  ambiguity is structural, not interpretive (#34).
- Audit trail (`writes`, `conflict_events`) records enough to reconstruct a
  decision: preflight_seq, commit_seq, judge_version, normalizer_version (#35).

Hardened against a 2026-07-12 review (finding #3;
re-review 2026-07-13): `remember()`/`learn()`/`supersede()` now FAIL CLOSED when the
normalizer raises `NormalizeUnavailable` — provider infrastructure failure
(connection/timeout/429/5xx/auth, `e.retryable=True`) OR a PERMANENT provider
4xx (400/404/422/..., `e.retryable=False`) — instead of the old
fail-open-on-every-exception behavior (superseded) that
originally left the permanent-4xx class falling through to fail-open too: the
write is refused (re-raised for server.py to map to HTTP 503 or 502 per
`e.retryable`) and audited as `conflict_events.resolution='degraded_refused'`,
UNLESS `force_exception` is supplied (remember/learn only — supersede has no
override), in which case the claim is stored with the contradiction check
skipped (`norm=None`) and audited as `resolution='degraded_exception'`.
`diagnostics()` surfaces both as `normalize_degraded_count` /
`last_normalize_degraded_at` for /diagnose telemetry.

Concurrency (bounded threaded HTTP server): the
sqlite connection is opened with `check_same_thread=False` so any worker
thread may use it, and every public method that touches `self.db` holds a
per-INSTANCE `threading.RLock` (`self._lock`) for the duration of that DB
work — RLock because these DB-touching methods can call each other on the
same thread (`why`->`recall`). There is NO lock shared across store
instances: two tenants are two ContradictionStore objects with two
independent locks. The `BEGIN IMMEDIATE` + `SQLITE_BUSY` retry above remains
the per-database serialization of record (defense in depth alongside the
per-instance lock).

`remember()`/`learn()`/`supersede()` deliberately do NOT hold `self._lock`
across the LLM-normalizer round-trip (~1-3s) or the embedder call: those
touch no `self.db` state, so `self._lock` is scoped ONLY to
the DB read/write phase (`_remember_attempt`/`_supersede_attempt`/
`_degraded_remember_attempt`). Holding it across the provider call would
serialize every caller of THIS store instance behind a slow round-trip — on
the legacy singleton route (server.py's `_STORE`, shared by every
caller-supplied org_id), that would make the per-instance lock a de-facto
CROSS-TENANT lock, which the house rule forbids ("use per-instance locks,
never a global cross-tenant lock").

Still deferred (explicit IOUs):
- AuthContext at the library boundary (#32) — added when the HTTP API slice lands.
- Full atomic file-locking on multi-process LLM cache (#27) — handled in
  LLMNormalizer (wave C).
"""
from __future__ import annotations
import json
import random
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .normalize import RuleNormalizer, NormalizeUnavailable
from .judge import ContradictionJudge
from .structural import HIGH
from . import metrics

NORM_SCHEMA_VERSION = "1"
JUDGE_VERSION = "structural-1+nli-deberta-v3"

# Tenant-facing GET /audit (private-repo issue #124): unbounded audit payloads are a real
# risk on a busy tenant, so each of the two streams is capped defensively.
# DEFAULT_AUDIT_LIMIT is what the route applies when the caller omits
# `limit`; MAX_AUDIT_LIMIT is the hard ceiling `audit_log()` clamps to
# regardless of what the caller asks for (mirrors /recall's clamp pattern).
DEFAULT_AUDIT_LIMIT = 200
MAX_AUDIT_LIMIT = 1000

# GET /active (private-repo issue #185): a full active set is EVERY column of every active
# claim (statement, reason, the persisted norm JSON blob, all timestamp/status
# columns) -- a real ~170-claim project measured at ~300 KB unbounded, enough
# to blow a caller's tool-output ceiling. DEFAULT_ACTIVE_LIMIT is the page
# size applied when the caller omits `limit`; MAX_ACTIVE_LIMIT is the hard
# ceiling `active()` clamps to regardless of what the caller asks for (same
# clamp shape as DEFAULT_AUDIT_LIMIT/MAX_AUDIT_LIMIT above). A claim row is
# far heavier than an audit row, so both numbers sit well below the audit
# pair: 50 claims lands well under most tool-output ceilings even at the
# reported ~1.7 KB/claim, and 500 keeps even an explicit large `limit` from
# reproducing the unbounded-response bug this issue exists to fix.
DEFAULT_ACTIVE_LIMIT = 50
MAX_ACTIVE_LIMIT = 500

# GET /active byte budget (private-repo issue #206): the row-count page above still lands at
# 201.6 KB on a real verbose corpus (avg 1,944 B/claim, MCP double-encoding
# roughly doubles that again on the wire) -- a fixed row count can't be right
# for both a terse and a verbose corpus. ACTIVE_BYTE_BUDGET caps the SERVED
# JSON of a page (post-projection, pre-MCP-envelope) at ~50 KB, self-tuning
# across corpora where DEFAULT_ACTIVE_LIMIT's fixed row count can't -- it only
# ever narrows a page that `limit` already bounded, one row at a time, from
# the front, and always keeps at least the first row so a page can never
# collapse to nothing (same "at least 1" shape as the limit clamp above).
# Applies to the paginated (`limit` given) branch of `active()` ONLY, never
# the internal unbounded opt-out (the contradiction judge, health probes) --
# those callers never pay for or silently inherit either cap. Composes with
# `projection="summary"` below: a summary row is a fraction of a full row's
# size, so the byte budget rarely binds once a caller opts into it.
ACTIVE_BYTE_BUDGET = 50_000

# GET /active projection (private-repo issue #206): `"full"` (default) is every column,
# unchanged since #185. `"summary"` drops everything but `id`/`statement` --
# `reason` is the bulk of a claim's bytes and, per the reporter, the field a
# "what does this project believe?" browse rarely needs; a caller who wants
# one claim's reason back can `ergo_recall`/`ergo_why` it by id. Kept as a
# plain 2-value enum (not an arbitrary field mask) on purpose -- see the PR
# body for why `projection` was chosen over `fields`/`view`/`mode`.
ACTIVE_PROJECTIONS = ("full", "summary")

# GET /export (private-repo issue #203): unlike /active, export defaults to UNBOUNDED --
# an existing backup/migration caller passing no `limit` must keep getting
# the FULL scope, never a silently truncated file it believes is complete
# (the same silent-incompleteness failure mode #184/#185 exist to prevent,
# just one route over -- export is the "faithful snapshot" source of truth
# ergo_import round-trips against, so a changed default here is a much
# worse backward-incompatibility than /active's browse-only case). Paging
# is opt-in: passing `limit` gets the SAME loud-truncation shape /active
# uses (`count`/`returned`/`truncated`/`next_offset`), same field names,
# same clamp-and-degrade contract. DEFAULT_EXPORT_LIMIT only fires once a
# caller HAS opted in but supplies a non-numeric/garbage value, or as the
# page size an MCP caller is told to start with. Both numbers sit BELOW
# the /active pair even though export stays unbounded by default: an
# export head is heavier than an /active row (it can carry a nested
# `revisions[]` why-chain) -- the reported corpus averaged ~2,864
# bytes/head (504.2 KB / 176 heads, private-repo issue #203) versus /active's ~1,944
# B/claim, and MCP double-encodes on the wire either way.
DEFAULT_EXPORT_LIMIT = 25
MAX_EXPORT_LIMIT = 200

# GET /why relevance floor (private-repo issue #202): recall() always has a nearest
# neighbour, so without a floor a nonsense query against a populated project
# gets a confident, completely unrelated reasoned claim back instead of null.
# Calibrated against real query/claim pairs via the live
# ergo_why/ergo_recall MCP tools: nonsense queries (gibberish, unrelated
# domains) scored well below the floor; genuine queries -- including
# deliberately vague/short/wrong-vocabulary ones, the case a floor most
# risks damaging -- scored above it, with only a tight margin between the
# two, so the floor sits low in that gap to bias against suppressing a
# real answer (the issue's "opposite failure" is symmetric with the one
# #202 reports). See evals/why_precision/ for the eval harness that
# measures the sharper residual failure this floor alone cannot catch:
# why() can still surface a confidently WRONG claim above the floor.
WHY_RELEVANCE_FLOOR = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  id            TEXT NOT NULL,
  org_id        TEXT NOT NULL,
  project       TEXT NOT NULL,
  who           TEXT NOT NULL,
  statement     TEXT NOT NULL,
  reason        TEXT,
  norm          TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  supersedes    TEXT,
  claim_seq     INTEGER NOT NULL,
  conflict_exception TEXT,
  normalizer_id      TEXT NOT NULL,
  normalizer_version TEXT NOT NULL,
  norm_schema_version TEXT NOT NULL,
  judge_version      TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  retract_reason TEXT,
  retracted_at   TEXT,
  evicted_reason TEXT,
  evicted_at     TEXT,
  PRIMARY KEY (id),
  UNIQUE (org_id, project, id),
  UNIQUE (org_id, project, claim_seq),
  FOREIGN KEY (org_id, project, supersedes)
    REFERENCES claims(org_id, project, id),
  CHECK (length(trim(statement)) > 0),
  CHECK (status IN ('active', 'superseded', 'retracted', 'evicted')),
  CHECK (claim_seq > 0)
);
CREATE INDEX IF NOT EXISTS idx_claims_active
  ON claims(org_id, project, status);
CREATE INDEX IF NOT EXISTS idx_claims_history
  ON claims(org_id, project, claim_seq);

CREATE TABLE IF NOT EXISTS projects (
  org_id TEXT NOT NULL,
  project TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (org_id, project)
);

CREATE TABLE IF NOT EXISTS conflict_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  project TEXT NOT NULL,
  who TEXT NOT NULL,
  incoming TEXT NOT NULL,
  against TEXT,                                      -- id of conflicting claim
  stored_claim_id TEXT,                              -- id of the claim ultimately stored, if any
  tier TEXT,
  score REAL,
  source TEXT,
  reason TEXT,
  resolution TEXT,                                   -- flagged | warned | exception | superseded
  exception_reason TEXT,
  preflight_seq INTEGER,
  commit_seq INTEGER,
  judge_version TEXT,
  normalizer_version TEXT,
  ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS writes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_id TEXT NOT NULL,
  project TEXT NOT NULL,
  who TEXT NOT NULL,
  action TEXT NOT NULL,
  claim_id TEXT,
  preflight_seq INTEGER,
  commit_seq INTEGER,
  ts TEXT NOT NULL
);
"""


# --- result types ---------------------------------------------------------
@dataclass
class ConflictHit:
    claim: dict
    tier: str
    score: float
    source: str
    reason: str


@dataclass
class Conflict:
    incoming_text: str
    incoming_norm: dict
    conflicts: list                # list[ConflictHit] — blocking
    warnings: list = field(default_factory=list)  # MED-tier (warn) hits
    stored: bool = False
    claim_id: str | None = None


@dataclass
class Stored:
    claim_id: str
    stored: bool = True
    conflicts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class ActiveSet:
    state: Literal["project_missing", "empty", "has_active_claims"]
    claims: list
    # Pagination (private-repo issue #185). `count` is the TOTAL active claims in the
    # scope regardless of `limit`/`offset` -- a separate COUNT(*), so it
    # stays correct independently of which page `claims` holds. `returned`
    # is len(claims); `truncated`/`next_offset` make truncation loud and
    # self-describing instead of a caller silently assuming a short list
    # means "that's everything". Defaulted (rather than required) so a
    # 2-positional-arg construction -- e.g. test fakes built before this
    # issue -- still works; `active()` itself always fills these in
    # explicitly.
    count: int = 0
    returned: int = 0
    truncated: bool = False
    next_offset: int | None = None
    # `truncated_reason` (private-repo issue #206): `None` when `truncated` is False;
    # otherwise "row_limit" (the page filled to `limit`, more rows remain) or
    # "byte_budget" (ACTIVE_BYTE_BUDGET cut the page short of `limit`) -- a
    # caller who only sees `truncated: true` can't tell whether raising
    # `limit` would even help; this makes that honest instead of ambiguous.
    truncated_reason: str | None = None


@dataclass
class ExportSet:
    """Return shape of `export_claims()` (private-repo issue #203) -- SAME pagination
    vocabulary as `ActiveSet` above (`count`/`returned`/`truncated`/
    `next_offset`), plus one export-specific field:

    `as_of_seq` is a `{project: max_claim_seq}` snapshot marker, computed
    from every row this call read. A single SCOPED `project` always gets a
    key -- `0` for an empty/never-used project, never a missing key --
    since the caller named exactly one project to report a baseline for.
    `project=None` (whole-org) has no such single key to default: an org
    with zero claims anywhere legitimately has no projects to enumerate,
    so `{}` is correct there (contrast the scoped case, which always knows
    which one key to report even at zero). It exists because an
    export is meant to be a FAITHFUL SNAPSHOT, not just a browse: pages
    fetched at different times, across concurrent writes, could compose a
    file that never corresponded to any single instant of the store --
    torn in a way `/active`'s pagination doesn't need to worry about,
    since a browsed page is merely confusing, never round-tripped into
    `/import`. `remember()`/`learn()`/`supersede()` all consume a fresh
    `claim_seq` on insert (mirrors the store's own internal
    `preflight_seq` race-guard idiom), so `as_of_seq` changing between two
    page fetches for the SAME multi-page walk means the scope was written
    to mid-walk -- the composed file is not a guaranteed single-instant
    snapshot, and a careful caller should re-walk from `offset=0`. This
    makes an inconsistent set DETECTABLE rather than silently accepted --
    chosen over cursor-based paging (walking by `claim_seq` instead of
    `offset`) because the brief for this issue requires reusing /active's
    exact `limit`/`offset` row-count vocabulary, not inventing a second
    one. Known gap, honestly documented rather than hidden: `retract()`
    flips a row's status in place WITHOUT consuming a new `claim_seq` (by
    design -- see its own docstring), so a bare retract racing a
    multi-page walk (no insert/supersede alongside it) will not move
    `as_of_seq`. This is the same blind spot the in-house `preflight_seq`
    race-guard already accepts; retraction is a rare, deliberate,
    administrative action, not the everyday write this marker exists to
    catch.
    """
    claims: list
    count: int = 0
    returned: int = 0
    truncated: bool = False
    next_offset: int | None = None
    as_of_seq: dict = field(default_factory=dict)


@dataclass
class Retracted:
    claim_id: str
    retracted: bool = True


# --- store ----------------------------------------------------------------
class _Retry(Exception):
    pass


class _Aborted(Exception):
    pass


class QuotaExceeded(Exception):
    """Raised by the write-guard's COMMIT path when inserting would push a
    tenant's active-claim count at/over its cap. Raised from
    `_enforce_claim_cap`, which MUST be called while
    `self._lock` is held AND inside an open `BEGIN IMMEDIATE` transaction,
    immediately before the insert(s) it guards -- the same placement as the
    `preflight_seq` race-recheck a few lines above it in every `_*_attempt`
    method. This closes the TOCTOU race the original design had: server.py
    used to read the active-claim count with the store's lock released
    BEFORE `remember()`/`learn()`/`ingest()` ever acquired it for their own
    transaction, so two concurrent requests for the same tenant could both
    observe room under the cap, both pass, and both commit -- overshooting
    it. Checking here instead means the count-read and every insert it gates
    happen while `self._lock` (a per-INSTANCE RLock all writes to this store
    already serialize behind) is held continuously, so no other write on
    THIS store instance can interleave between the check and the commit.
    """
    def __init__(self, cap: int, current: int, additions: int = 1):
        self.cap = cap
        self.current = current
        self.additions = additions
        super().__init__(f"active-claim cap of {cap} reached (current: {current})")


class ProjectLimitExceeded(Exception):
    """Raised by the write-guard's COMMIT path (PLAN-METERING) when a
    /remember or /learn write would create a NEW project beyond the
    tenant's plan project-count limit. Same atomicity contract as
    `QuotaExceeded`/`_enforce_claim_cap`: raised from
    `_enforce_project_limit`, which MUST be called while `self._lock` is
    held AND inside an open `BEGIN IMMEDIATE` transaction, immediately
    before `_ensure_project` -- so the project-existence check and the
    project-row insert it gates are indivisible from any other writer's
    perspective on this store instance. Existing projects (already present
    in the `projects` table) are ALWAYS allowed regardless of the limit --
    only creating a project NOT already in scope can trip this."""
    def __init__(self, cap: int, current: int):
        self.cap = cap
        self.current = current
        super().__init__(f"project cap of {cap} reached (current: {current})")


_BUSY_CODES = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} if hasattr(sqlite3, "SQLITE_BUSY") else set()


def _is_busy(err: sqlite3.OperationalError) -> bool:
    code = getattr(err, "sqlite_errorcode", None)
    if code is not None and code in _BUSY_CODES:
        return True
    msg = str(err).lower()
    return "locked" in msg or "busy" in msg


def _project_claim(claim: dict, projection: str) -> dict:
    """Apply GET /active's projection (private-repo issue #206). `"full"` is a no-op
    (identity); `"summary"` drops every column but `id`/`statement` -- built
    as a fresh dict comprehension so `reason` is genuinely ABSENT from the
    key set, not merely null/empty."""
    if projection == "summary":
        return {"id": claim["id"], "statement": claim["statement"]}
    return claim


def _apply_byte_budget(claims: list, budget: int) -> tuple[list, bool]:
    """Trim a fetched page down to ACTIVE_BYTE_BUDGET bytes of served JSON
    (private-repo issue #206), front-to-back. Always keeps at least the first claim even
    if it alone exceeds `budget` -- a byte budget must narrow a page, never
    collapse it to nothing (same "at least 1" guarantee `limit`'s own clamp
    makes). Returns `(kept_claims, hit)`; `hit` is True only when the budget
    actually cut something off the page `limit`/`offset` already fetched."""
    kept: list = []
    total = 0
    for claim in claims:
        size = len(json.dumps(claim))
        if kept and total + size > budget:
            return kept, True
        kept.append(claim)
        total += size
    return kept, False


class ContradictionStore:
    def __init__(self, path: str = ":memory:", normalizer=None, nli=None,
                 embedder=None, block_tier=("block",), busy_timeout_ms: int = 5000,
                 max_retries: int = 8):
        # check_same_thread=False: the bounded threaded HTTP server (server.py)
        # may service this store's tenant from any worker thread. A single
        # sqlite3.Connection object is still NOT safe
        # for concurrent use by multiple threads even with this flag, so
        # `self._lock` (a per-INSTANCE RLock, below) guards every public
        # method that touches self.db for its full duration -- RLock because
        # methods call each other on the same thread (learn->remember,
        # why->recall). There is deliberately NO lock shared across store
        # instances: two tenants are two ContradictionStore objects with two
        # independent locks, so a slow guarded write on tenant A's store only
        # ever holds tenant A's lock, never blocking tenant B's store.
        # isolation_level=None: we own every transaction boundary (no implicit Py txns).
        self.db = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0,
                                  isolation_level=None, check_same_thread=False)
        self._lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self.db.executescript(SCHEMA)
        self._migrate()
        # --- optional retrieval layer (sqlite-vec) ---
        # When an embedder is supplied, the SAME store gains semantic recall/why
        # over everything it has remembered. Absent an embedder the store behaves
        # exactly as before (the contradiction-firewall path is unchanged), so the
        # existing test suite and any embedder-less caller keep working untouched.
        self.embedder = embedder
        self.embed_dim = getattr(embedder, "dim", None) if embedder is not None else None
        self.retrieval = False
        self._serialize = None
        if self.embedder is not None:
            self._init_vec()
        # Stateless default: one store instance serves every (org_id, project),
        # so cross-claim pronoun-coref tracking would leak subjects across scopes.
        self.normalizer = normalizer or RuleNormalizer(track_context=False)
        self.normalizer_id = type(self.normalizer).__name__
        self.normalizer_version = getattr(self.normalizer, "VERSION", "1")
        self.judge = ContradictionJudge(self.normalizer, nli=nli)
        self.block_tier = set(block_tier)
        self.max_retries = max_retries

    def _migrate(self):
        """Idempotent additive column upgrades for pre-existing DBs.

        SCHEMA is CREATE TABLE IF NOT EXISTS, so a live DB created before a
        column existed never picks it up from the schema script alone. Additive
        + nullable columns land here via ALTER TABLE — no norm_schema_version
        bump (precedent: the norm `value` field was additive too).
        """
        cols = {r["name"] for r in
                self.db.execute("PRAGMA table_info(claims)").fetchall()}
        for col in ("retract_reason", "retracted_at"):
            if col not in cols:
                self.db.execute(f"ALTER TABLE claims ADD COLUMN {col} TEXT")
        # 'evicted' widens the status CHECK constraint (private-repo issue #123) -- run
        # AFTER the additive ALTER TABLE above so a truly ancient DB (predating
        # BOTH retract_reason/retracted_at AND 'evicted') already has those two
        # columns in place before the rebuild copies them across.
        self._migrate_evicted_status()

    def _migrate_evicted_status(self):
        """Widen `claims.status`'s CHECK constraint to include the 'evicted'
        terminal status (private-repo issue #123, per-tenant retention/TTL eviction).

        SQLite has no `ALTER TABLE ... ADD CONSTRAINT` / `DROP CONSTRAINT`, so
        a pre-existing DB whose `claims` table was created before 'evicted'
        existed is stuck with the OLD CHECK (`'active','superseded',
        'retracted'`) forever unless the table itself is rebuilt -- SCHEMA's
        `CREATE TABLE IF NOT EXISTS` never touches an already-existing table's
        constraints. This runs the standard SQLite 12-step table-rebuild
        (https://sqlite.org/lang_altertable.html#otheralter): disable FK
        enforcement, build a `claims_new` with the widened CHECK (and this
        same PR's `evicted_reason`/`evicted_at` columns) inside ONE
        transaction, copy every row across, drop the old table, rename the
        new one into place, recreate the two indexes, verify with
        `PRAGMA foreign_key_check` BEFORE committing (never after -- a broken
        FK must abort the transaction, not get persisted), then re-enable FK
        enforcement.

        Idempotent + safe to call on every open: detected via
        `sqlite_master.sql` containing the literal `'evicted'` substring, so a
        DB already on the new schema (every fresh DB created by SCHEMA above,
        or one already migrated by a prior boot) is a single cheap SELECT and
        nothing else. NEVER drops or renames data -- every column value
        (including `retract_reason`/`retracted_at` on a DB migrated by the
        ALTER TABLE loop above, or NULL on one predating even that) round-trips
        into `claims_new` before the old table is dropped.
        """
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
        if row is None or "evicted" in row["sql"]:
            return
        # retract_reason/retracted_at are GUARANTEED present here (never
        # conditional): _migrate() runs its own additive ALTER TABLE loop for
        # those two columns immediately before calling this method, so by
        # this point they already exist on every DB, however old -- the
        # INSERT below can select them directly, no per-DB branching needed.
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("""
                    CREATE TABLE claims_new (
                      id            TEXT NOT NULL,
                      org_id        TEXT NOT NULL,
                      project       TEXT NOT NULL,
                      who           TEXT NOT NULL,
                      statement     TEXT NOT NULL,
                      reason        TEXT,
                      norm          TEXT,
                      status        TEXT NOT NULL DEFAULT 'active',
                      supersedes    TEXT,
                      claim_seq     INTEGER NOT NULL,
                      conflict_exception TEXT,
                      normalizer_id      TEXT NOT NULL,
                      normalizer_version TEXT NOT NULL,
                      norm_schema_version TEXT NOT NULL,
                      judge_version      TEXT NOT NULL,
                      created_at    TEXT NOT NULL,
                      retract_reason TEXT,
                      retracted_at   TEXT,
                      evicted_reason TEXT,
                      evicted_at     TEXT,
                      PRIMARY KEY (id),
                      UNIQUE (org_id, project, id),
                      UNIQUE (org_id, project, claim_seq),
                      FOREIGN KEY (org_id, project, supersedes)
                        REFERENCES claims_new(org_id, project, id),
                      CHECK (length(trim(statement)) > 0),
                      CHECK (status IN ('active', 'superseded', 'retracted', 'evicted')),
                      CHECK (claim_seq > 0)
                    )
                """)
                self.db.execute("""
                    INSERT INTO claims_new (id, org_id, project, who, statement, reason,
                      norm, status, supersedes, claim_seq, conflict_exception, normalizer_id,
                      normalizer_version, norm_schema_version, judge_version, created_at,
                      retract_reason, retracted_at, evicted_reason, evicted_at)
                    SELECT id, org_id, project, who, statement, reason,
                      norm, status, supersedes, claim_seq, conflict_exception, normalizer_id,
                      normalizer_version, norm_schema_version, judge_version, created_at,
                      retract_reason, retracted_at, NULL, NULL
                    FROM claims
                """)
                self.db.execute("DROP TABLE claims")
                self.db.execute("ALTER TABLE claims_new RENAME TO claims")
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_claims_active "
                    "ON claims(org_id, project, status)")
                self.db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_claims_history "
                    "ON claims(org_id, project, claim_seq)")
                bad = self.db.execute("PRAGMA foreign_key_check(claims)").fetchall()
                if bad:
                    raise RuntimeError(
                        f"claims table rebuild left dangling foreign keys: {bad}")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    # --- retrieval layer (sqlite-vec) ------------------------------------
    def _init_vec(self):
        """Load sqlite-vec and create the cosine KNN table. Enables recall()/why()."""
        import sqlite_vec
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self._serialize = sqlite_vec.serialize_float32
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_claims USING vec0("
            f"claim_id TEXT PRIMARY KEY, embedding float[{self.embed_dim}] distance_metric=cosine)")
        self.retrieval = True

    def _embed_claim(self, statement, reason=None):
        """Embed claim+reason OUTSIDE the writer slot (None when retrieval is off)."""
        if self.embedder is None:
            return None
        text = statement if not reason else f"{statement}\n{reason}"
        return self.embedder.embed(text)

    @staticmethod
    def _lexical(query, text):
        """Fraction of query word-tokens present in text — the keyword half of the
        hybrid score (SQLite has no pg_trgm; this mirrors word_similarity intent)."""
        q = set(re.findall(r"\w+", query.lower()))
        if not q:
            return 0.0
        t = set(re.findall(r"\w+", text.lower()))
        return len(q & t) / len(q)

    def recall(self, org_id, project, query, limit=10, reasoned_only=False):
        """Hybrid semantic + keyword search over ACTIVE claims (vec 0.7 + kw 0.3).

        Superseded/retracted claims are excluded structurally (join filters on
        status='active'), so the vector index never needs deleting on supersede.
        """
        with self._lock:
            if not self.retrieval:
                raise RuntimeError(
                    "retrieval not enabled: construct ContradictionStore with an embedder")
            qvec = self.embedder.embed(query)
            serialized = self._serialize(qvec)
            # The vec index is global (all orgs/projects) and vectors are never
            # deleted on supersede/retract, so a fixed KNN budget can be consumed
            # entirely by rows the scope/status filter below discards, silently
            # dropping an on-topic active in-scope claim (audit #8). Grow k
            # geometrically until we have `limit` qualifying results OR we've
            # searched every vector — so recall can never miss an in-scope active
            # claim that a full scan would have surfaced, while the common case
            # (results mostly in-scope) still pays only the small initial budget.
            total = self._exec("SELECT COUNT(*) AS n FROM vec_claims").fetchone()["n"]
            if not total:
                return []
            k = max(limit * 8, 40)
            while True:
                capped = min(k, total)
                rows = self._exec(
                    "SELECT claim_id, distance FROM vec_claims "
                    "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (serialized, capped)).fetchall()
                scored = []
                for r in rows:
                    c = self._exec(
                        "SELECT * FROM claims WHERE id=? AND org_id=? AND project=? AND status='active'",
                        (r["claim_id"], org_id, project)).fetchone()
                    if not c:
                        continue
                    if reasoned_only and not (c["reason"] and c["reason"].strip()):
                        continue
                    vec_sim = max(0.0, 1.0 - r["distance"])
                    kw = self._lexical(query, c["statement"])
                    score = 0.7 * vec_sim + 0.3 * kw
                    scored.append((score, vec_sim, kw, c))
                if len(scored) >= limit or capped >= total:
                    break
                k *= 2
            scored.sort(key=lambda t: t[0], reverse=True)
            return [{
                "id": c["id"], "who": c["who"], "statement": c["statement"],
                "reason": c["reason"], "project": c["project"], "status": c["status"],
                "created_at": c["created_at"], "score": round(s, 4),
                "vec_sim": round(v, 4), "kw": round(kw, 4),
            } for s, v, kw, c in scored[:limit]]

    def why(self, org_id, project, query):
        """Best-matching REASONED belief + its reason + full supersede history.

        reasoned_only=True so a reason-less fact can't win a rationale query.
        Returns None when there is no reasoned claim in scope at all. When the
        best match scores below WHY_RELEVANCE_FLOOR, returns a `suppressed`
        marker instead of a low-confidence answer -- nearest-neighbour always
        has a nearest, so without this a nonsense query gets a confident,
        unrelated claim back (private-repo issue #202).
        """
        with self._lock:
            hits = self.recall(org_id, project, query, limit=1, reasoned_only=True)
            if not hits:
                return None
            top = hits[0]
            if top["score"] < WHY_RELEVANCE_FLOOR:
                return {"suppressed": {"reason": "below_relevance_floor",
                                        "score": top["score"],
                                        "floor": WHY_RELEVANCE_FLOOR}}
            return {"claim": top["statement"], "reason": top["reason"],
                    "score": top["score"],
                    "history": self._chain(org_id, project, top["id"])}

    def _chain(self, org_id, project, claim_id):
        """Walk the supersede chain backwards from claim_id (newest -> oldest)."""
        chain, cur = [], claim_id
        while cur:
            r = self._exec(
                "SELECT id, statement, reason, supersedes FROM claims "
                "WHERE org_id=? AND project=? AND id=?",
                (org_id, project, cur)).fetchone()
            if not r:
                break
            chain.append({"id": r["id"], "claim": r["statement"], "reason": r["reason"]})
            cur = r["supersedes"]
        return chain

    # --- transaction primitives ------------------------------------------
    def _begin_immediate(self):
        for attempt in range(self.max_retries):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as e:
                if not _is_busy(e):
                    raise
                # bounded backoff with jitter
                time.sleep(min(0.05 * (2 ** attempt) + random.random() * 0.02, 1.0))
        raise sqlite3.OperationalError(
            f"BEGIN IMMEDIATE: db remained BUSY after {self.max_retries} retries")

    def _exec(self, sql, params=()):
        for attempt in range(self.max_retries):
            try:
                return self.db.execute(sql, params)
            except sqlite3.OperationalError as e:
                if not _is_busy(e):
                    raise
                time.sleep(min(0.02 * (2 ** attempt), 0.5))
        raise sqlite3.OperationalError(f"persistent BUSY on: {sql.split()[0]}")

    def _commit(self):
        self._exec("COMMIT")

    def _rollback(self):
        try:
            self.db.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    # --- helpers ----------------------------------------------------------
    def _ensure_project(self, org_id: str, project: str):
        self._exec(
            "INSERT OR IGNORE INTO projects(org_id, project, created_at) VALUES(?,?,?)",
            (org_id, project, _now()))

    def _max_seq(self, org_id: str, project: str) -> int:
        row = self._exec(
            "SELECT COALESCE(MAX(claim_seq),0) s FROM claims WHERE org_id=? AND project=?",
            (org_id, project)).fetchone()
        return row["s"]

    def _active_rows(self, org_id, project, exclude_id=None, limit=None, offset=0):
        """Fetch active claim rows for a scope.

        `limit=None` (the default) is the pre-#185 behavior every internal
        caller (the contradiction judge, `active()`'s own unbounded mode)
        relies on: every active row, unordered, one query, no COUNT(*).
        Passing an int `limit` pushes `LIMIT`/`OFFSET` into SQL (private-repo issue #185
        non-negotiable #1 -- never load every row and slice in Python) and
        orders by `claim_seq` so repeated calls with a growing `offset` walk
        the set exactly once, with no gaps or duplicates, across writes that
        don't touch this scope.
        """
        query = "SELECT * FROM claims WHERE org_id=? AND project=? AND status='active'"
        params = [org_id, project]
        if exclude_id is not None:
            query += " AND id!=?"
            params.append(exclude_id)
        if limit is not None:
            query += " ORDER BY claim_seq LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        cur = self._exec(query, tuple(params))
        return cur.fetchall()

    @staticmethod
    def _candidate(row) -> dict:
        """Build a normalized claim dict for the judge from a stored row.
        Carries the persisted `norm` so we don't re-normalize on every read (#6)."""
        norm = json.loads(row["norm"]) if row["norm"] else None
        return {"text": row["statement"], "norm": norm}

    def _judge_against(self, incoming: dict, active_rows) -> tuple[list, list]:
        """Returns (blocking, warnings). Uses the judge with persisted norms.

        Rows with a NULL `norm` are reference facts ingested via remember_fact —
        "a fact, not a decision to be contradiction-checked" (per that method's
        contract). They are skipped here: contradiction-checking against them is
        not the design intent, and live-re-normalizing them would re-incur the
        per-claim LLM cost (and refusal-crash risk) that ingest exists to avoid.
        """
        blocking, warnings = [], []
        for c in active_rows:
            if not c["norm"]:
                continue
            cand = self._candidate(c)
            res = self.judge.verdict(incoming, cand)
            if res.label != "contradiction":
                continue
            hit = ConflictHit(claim=dict(c), tier=res.tier, score=res.score,
                              source=res.source, reason=res.reason)
            if res.tier in self.block_tier:
                blocking.append(hit)
            else:
                warnings.append(hit)
        return blocking, warnings

    def _enforce_claim_cap(self, org_id, max_active_claims, additions=1):
        """Raise QuotaExceeded if inserting `additions` more active claims
        for org_id would push the tenant at/over max_active_claims. See
        QuotaExceeded's docstring for the atomicity contract this depends on
        (MUST be called while self._lock is held, inside an open BEGIN
        IMMEDIATE, right before the insert(s)). None or <=0 means unlimited:
        no query at all, matching the ERGO_TENANT_MAX_ACTIVE_CLAIMS
        "0 = unlimited" semantics -- this is the SAME predicate as
        active_claim_count()/org_stats()'s active_claims, inlined here
        rather than calling those (both would re-acquire self._lock, which
        is safe -- it's an RLock -- but pointless indirection on this
        already-locked hot path).

        `additions <= 0` also returns immediately, BEFORE the count query
        (finding): a fully-deduplicated remember_facts_batch
        re-ingest (every chunk already an active statement in scope) calls
        this with additions=0, and inserting nothing can never push a
        tenant over its cap -- that must stay true even if the tenant is
        already AT or OVER the cap (e.g. an admin lowered the override
        after claims were already stored), which would otherwise make a
        pure no-op re-ingest fail with QuotaExceeded for adding zero claims."""
        if not max_active_claims or max_active_claims <= 0:
            return
        if additions <= 0:
            return
        current = self._exec(
            "SELECT count(*) FROM claims WHERE org_id=? AND status='active'",
            (org_id,)).fetchone()[0]
        if current + additions > max_active_claims:
            raise QuotaExceeded(cap=max_active_claims, current=current,
                                additions=additions)

    def _enforce_project_limit(self, org_id, project, project_limit):
        """Raise ProjectLimitExceeded if `project` is NOT already an
        existing project for org_id AND creating it would push the
        tenant's distinct-project count at/over `project_limit`
        (PLAN-METERING). See ProjectLimitExceeded's docstring for the
        atomicity contract (MUST be called while self._lock is held, inside
        an open BEGIN IMMEDIATE, right before `_ensure_project`).

        None means unlimited: no query at all (mirrors
        `_enforce_claim_cap`'s "no query at all" shortcut). A project
        already present in the `projects` table is ALWAYS allowed --
        "existing projects keep working" even if the tenant is already at
        or over the limit (e.g. an admin downgraded the plan after
        multiple projects were already created)."""
        if project_limit is None:
            return
        exists = self._exec(
            "SELECT 1 FROM projects WHERE org_id=? AND project=?",
            (org_id, project)).fetchone()
        if exists is not None:
            return
        current = self._exec(
            "SELECT count(*) FROM projects WHERE org_id=?", (org_id,)).fetchone()[0]
        if current >= project_limit:
            raise ProjectLimitExceeded(cap=project_limit, current=current)

    def _audit_write(self, org_id, project, who, action, claim_id,
                     preflight_seq=None, commit_seq=None):
        self._exec(
            "INSERT INTO writes(org_id,project,who,action,claim_id,preflight_seq,"
            "commit_seq,ts) VALUES(?,?,?,?,?,?,?,?)",
            (org_id, project, who, action, claim_id, preflight_seq, commit_seq, _now()))

    def _audit_conflict(self, *, org_id, project, who, incoming, hit, resolution,
                        stored_claim_id=None, exception_reason=None,
                        preflight_seq=None, commit_seq=None):
        self._exec(
            "INSERT INTO conflict_events(org_id,project,who,incoming,against,"
            "stored_claim_id,tier,score,source,reason,resolution,exception_reason,"
            "preflight_seq,commit_seq,judge_version,normalizer_version,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, project, who, incoming, hit.claim["id"], stored_claim_id,
             hit.tier, hit.score, hit.source, hit.reason, resolution,
             exception_reason, preflight_seq, commit_seq,
             JUDGE_VERSION, self.normalizer_version, _now()))

    # --- write-guard primary entry point ---------------------------------
    def remember(self, org_id, project, who, statement, reason=None,
                 force_exception=None, max_active_claims=None, project_limit=None):
        """Run the contradiction check, then insert iff clear (or forced).

        Returns Stored on success, Conflict if blocking conflicts found (and
        force_exception is None). Both result types carry `warnings` for MED-tier
        flags (#7).

        Normalize + embed happen OUTSIDE `self._lock` (a 2026-07-13 review):
        those are calls to the external provider/embedder and never touch
        `self.db`, so holding the lock across them would serialize every
        caller of THIS store instance behind a slow LLM round-trip —
        including, on the legacy singleton route, callers supplying UNRELATED
        org_ids, which makes the per-instance lock a de-facto cross-tenant
        lock (path instructions: "use per-instance locks, never a global
        cross-tenant lock"). `self._lock` is scoped ONLY to the DB
        read/write phases (`_remember_attempt` / `_degraded_remember_attempt`),
        which is where the shared sqlite3 Connection actually needs
        single-Python-thread-at-a-time access; correctness of the guarded
        write itself still comes from the `BEGIN IMMEDIATE` + race-recheck
        inside those methods, not from holding the lock across normalize.

        If the normalizer's provider returns an error (`NormalizeUnavailable`
        — connection error/timeout/429/5xx/auth, OR a PERMANENT 4xx like
        400/404/422), the write FAILS CLOSED either way: this method
        re-raises so the caller (server.py) can refuse with HTTP 503
        (retryable) or 502 (permanent), UNLESS `force_exception` is set, in
        which case the statement is stored anyway with the contradiction
        check skipped (norm=None, same as a fact) and the bypass is recorded
        loudly in `conflict_events` (finding #3).

        `max_active_claims`: if set (and > 0), atomically
        enforced against THIS insert -- see QuotaExceeded's docstring for
        why the check has to live inside the write transaction, not in a
        preflight call before this method is even entered. force_exception
        does NOT bypass it: the claim cap is a resource limit, not a
        contradiction override, so the two are orthogonal. None or <=0
        means unlimited (no check at all).

        `project_limit` (PLAN-METERING): if set, atomically enforced (same
        placement/rationale as `max_active_claims` above, via
        `_enforce_project_limit`) against creating project `project` for
        org_id if it is not already an existing project in scope. None
        means unlimited (no check at all).
        """
        try:
            with metrics.time_stage("normalize"):
                incoming_norm = self.normalizer.normalize({"text": statement})
        except NormalizeUnavailable as e:
            return self._degraded_remember(org_id, project, who, statement,
                                           reason, force_exception, e,
                                           max_active_claims, project_limit)
        incoming = {"text": statement, "norm": incoming_norm,
                    "embedding": self._embed_claim(statement, reason)}

        last_err = None
        for _ in range(self.max_retries):
            try:
                with self._lock:
                    return self._remember_attempt(org_id, project, who, statement,
                                                  reason, incoming, force_exception,
                                                  max_active_claims, project_limit)
            except _Retry:
                last_err = "retry"
                continue
        raise RuntimeError(f"remember: exceeded retries ({last_err})")

    def _degraded_remember(self, org_id, project, who, statement, reason,
                           force_exception, err, max_active_claims=None,
                           project_limit=None):
        """Handle a NormalizeUnavailable raised while normalizing `statement`.

        No force_exception -> audit the refusal and re-raise (fail CLOSED,
        server.py maps this to HTTP 503 or 502 per `err.retryable`). With
        force_exception -> store the claim with the contradiction check
        skipped entirely (norm=None) and
        an audited conflict_events row, mirroring how a forced contradiction
        override is recorded (#8). Embedding (like normalize) happens OUTSIDE
        `self._lock` — see remember()'s docstring."""
        if not force_exception:
            with self._lock:
                self._audit_degraded(org_id, project, who, statement, err,
                                     resolution="degraded_refused")
            raise err
        embedding = self._embed_claim(statement, reason)
        last_err = None
        for _ in range(self.max_retries):
            try:
                with self._lock:
                    return self._degraded_remember_attempt(
                        org_id, project, who, statement, reason, force_exception,
                        err, embedding, max_active_claims, project_limit)
            except _Retry:
                last_err = "retry"
                continue
        raise RuntimeError(f"remember (degraded override): exceeded retries ({last_err})")

    def _degraded_remember_attempt(self, org_id, project, who, statement, reason,
                                   force_exception, err, embedding,
                                   max_active_claims=None, project_limit=None):
        preflight_seq = self._max_seq(org_id, project)
        self._begin_immediate()
        try:
            if self._max_seq(org_id, project) != preflight_seq:
                self._rollback()
                raise _Retry
            self._enforce_claim_cap(org_id, max_active_claims)
            self._enforce_project_limit(org_id, project, project_limit)
            commit_seq = preflight_seq + 1
            cid = self._insert(org_id, project, who, statement, reason, None, None,
                               commit_seq, force_exception, embedding=embedding)
            self._ensure_project(org_id, project)
            self._audit_write(org_id, project, who, "store_with_exception_degraded",
                              cid, preflight_seq=preflight_seq, commit_seq=commit_seq)
            self._audit_degraded(org_id, project, who, statement, err,
                                 resolution="degraded_exception", stored_claim_id=cid,
                                 exception_reason=force_exception)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Stored(claim_id=cid)

    def _audit_degraded(self, org_id, project, who, statement, err, *, resolution,
                        stored_claim_id=None, exception_reason=None):
        """Record a provider-infrastructure degrade (refused OR forced-through)
        so /diagnose telemetry can surface `last_normalize_degraded_at` + a
        count (finding #3)."""
        self._exec(
            "INSERT INTO conflict_events(org_id,project,who,incoming,against,"
            "stored_claim_id,tier,score,source,reason,resolution,exception_reason,"
            "preflight_seq,commit_seq,judge_version,normalizer_version,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, project, who, statement, None, stored_claim_id, None, None,
             "normalizer_infra", str(err), resolution, exception_reason, None, None,
             JUDGE_VERSION, self.normalizer_version, _now()))

    def _remember_attempt(self, org_id, project, who, statement, reason,
                          incoming, force_exception, max_active_claims=None,
                          project_limit=None):
        # PREFLIGHT (no write lock)
        preflight_seq = self._max_seq(org_id, project)
        active = self._active_rows(org_id, project)
        with metrics.time_stage("judge"):
            blocking, warnings = self._judge_against(incoming, active)

        # if blocking + not forced -> log + return Conflict; revalidate first (#5)
        if blocking and not force_exception:
            self._begin_immediate()
            try:
                if self._max_seq(org_id, project) != preflight_seq:
                    self._rollback()
                    raise _Retry
                # re-check that each blocking conflict claim is still active
                still_active_ids = {r["id"] for r in self._active_rows(org_id, project)}
                blocking = [h for h in blocking if h.claim["id"] in still_active_ids]
                if not blocking:
                    self._rollback()
                    # nothing to block on anymore — re-run from scratch
                    raise _Retry
                for h in blocking:
                    self._audit_conflict(org_id=org_id, project=project, who=who,
                                         incoming=statement, hit=h,
                                         resolution="flagged",
                                         preflight_seq=preflight_seq)
                for w in warnings:
                    self._audit_conflict(org_id=org_id, project=project, who=who,
                                         incoming=statement, hit=w,
                                         resolution="warned",
                                         preflight_seq=preflight_seq)
                self._commit()
            except Exception:
                self._rollback()
                raise
            return Conflict(incoming_text=statement, incoming_norm=incoming["norm"],
                            conflicts=blocking, warnings=warnings)

        # COMMIT path (BEGIN IMMEDIATE + race re-check + insert)
        self._begin_immediate()
        try:
            if self._max_seq(org_id, project) != preflight_seq:
                self._rollback()
                raise _Retry
            self._enforce_claim_cap(org_id, max_active_claims)
            self._enforce_project_limit(org_id, project, project_limit)
            commit_seq = preflight_seq + 1
            cid = self._insert(org_id, project, who, statement, reason,
                               incoming["norm"], None, commit_seq, force_exception,
                               embedding=incoming.get("embedding"))
            self._ensure_project(org_id, project)
            action = "store_with_exception" if force_exception else "remember"
            self._audit_write(org_id, project, who, action, cid,
                              preflight_seq=preflight_seq, commit_seq=commit_seq)
            # audit any flagged conflicts that were force-excepted (#8)
            if force_exception:
                for h in blocking:
                    self._audit_conflict(org_id=org_id, project=project, who=who,
                                         incoming=statement, hit=h,
                                         resolution="exception",
                                         exception_reason=force_exception,
                                         stored_claim_id=cid,
                                         preflight_seq=preflight_seq,
                                         commit_seq=commit_seq)
            for w in warnings:
                self._audit_conflict(org_id=org_id, project=project, who=who,
                                     incoming=statement, hit=w,
                                     resolution="warned",
                                     stored_claim_id=cid,
                                     preflight_seq=preflight_seq,
                                     commit_seq=commit_seq)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Stored(claim_id=cid, conflicts=blocking, warnings=warnings)

    def _insert(self, org_id, project, who, statement, reason, norm, supersedes,
                seq, exception=None, embedding=None) -> str:
        cid = _ulid()
        self._exec(
            "INSERT INTO claims(id,org_id,project,who,statement,reason,norm,status,"
            "supersedes,claim_seq,conflict_exception,normalizer_id,normalizer_version,"
            "norm_schema_version,judge_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, org_id, project, who, statement, reason,
             json.dumps(norm) if norm is not None else None, "active",
             supersedes, seq, exception, self.normalizer_id, self.normalizer_version,
             NORM_SCHEMA_VERSION, JUDGE_VERSION, _now()))
        if self.retrieval and embedding is not None:
            self._exec("INSERT INTO vec_claims(claim_id, embedding) VALUES(?,?)",
                       (cid, self._serialize(embedding)))
        return cid

    def has_active_statement(self, org_id, project, statement) -> bool:
        """Is `statement` already an active claim in this scope?

        Used by bulk ingest to make re-ingesting a document idempotent: a
        chunk's exact text (plus its source footer) is deterministic given the
        same input, so this is a dedup key with no schema change needed.
        """
        with self._lock:
            row = self._exec(
                "SELECT 1 FROM claims WHERE org_id=? AND project=? AND status='active' "
                "AND statement=? LIMIT 1",
                (org_id, project, statement)).fetchone()
            return row is not None

    def has_active_claim(self, org_id, project, claim_id) -> bool:
        """Is `claim_id` an active claim in this scope?

        Used by the /supersede handler to 404 a request whose target doesn't
        exist BEFORE metering it — a supersede that can't run the gate must
        not consume a monthly write unit. This mirrors `_supersede_attempt`'s
        own preflight query; that in-lock preflight stays the authoritative
        check (this one only avoids charging the common typo'd-id case, and a
        claim retracted in the tiny window between the two still 404s).
        """
        with self._lock:
            row = self._exec(
                "SELECT 1 FROM claims WHERE org_id=? AND project=? AND id=? "
                "AND status='active' LIMIT 1",
                (org_id, project, claim_id)).fetchone()
            return row is not None

    # --- fact fast-path (README: 'fact = upsert freely, no gate') -----------
    def remember_fact(self, org_id, project, who, statement, reason=None,
                      max_active_claims=None):
        """Store reference content WITHOUT the LLM normalizer or contradiction judge.

        Still embedded for recall. This is the bulk-ingest path: a document chunk
        is a fact, not a decision to be contradiction-checked, so it skips the
        per-claim LLM normalize that makes gated ingest O(LLM calls). Decisions /
        constraints that SHOULD be contradiction-checked still go through
        remember().

        `max_active_claims`: same atomic single-insert
        enforcement as remember() -- see QuotaExceeded's docstring. For a
        MULTI-statement batch (bulk /ingest), use remember_facts_batch()
        instead: checking the cap once per call here would let concurrent
        single-chunk inserts each individually pass while the batch as a
        whole crosses the cap, and would also check against the RAW chunk
        count rather than the dedup-aware new-chunk count.
        """
        # Embed OUTSIDE self._lock (finding, same rationale as
        # remember()/supersede()'s docstrings: the embedder call touches no
        # self.db state, so holding the per-instance lock across it would
        # serialize every other write to THIS tenant's store behind it --
        # cheap for the baked-in fastembed default, but a real cost for a
        # network-based embedder provider). The embedding is computed ONCE
        # and reused across retries (a statement's embedding doesn't depend
        # on anything a _Retry invalidates).
        embedding = self._embed_claim(statement, reason)
        with self._lock:
            for _ in range(self.max_retries):
                try:
                    return self._remember_fact_attempt(org_id, project, who, statement,
                                                       reason, embedding, max_active_claims)
                except _Retry:
                    continue
            raise RuntimeError("remember_fact: exceeded retries")

    def _remember_fact_attempt(self, org_id, project, who, statement, reason,
                               embedding, max_active_claims=None):
        preflight_seq = self._max_seq(org_id, project)
        self._begin_immediate()
        try:
            if self._max_seq(org_id, project) != preflight_seq:
                self._rollback()
                raise _Retry
            self._enforce_claim_cap(org_id, max_active_claims)
            commit_seq = preflight_seq + 1
            cid = self._insert(org_id, project, who, statement, reason, None, None,
                               commit_seq, embedding=embedding)
            self._ensure_project(org_id, project)
            self._audit_write(org_id, project, who, "remember_fact", cid,
                              preflight_seq=preflight_seq, commit_seq=commit_seq)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Stored(claim_id=cid)

    def remember_facts_batch(self, org_id, project, who, statements: list,
                             max_active_claims=None) -> dict:
        """Atomically store a BATCH of facts (bulk-/ingest path)
        in ONE `BEGIN IMMEDIATE` transaction: dedup against already-active
        statements in scope (skip, same rule as remember_fact's implicit
        contract via has_active_statement) AND against repeats within the
        batch itself, then enforce the tenant's active-claim cap ONCE
        against the actual count of NEW (non-duplicate) statements this
        batch would insert -- refusing the WHOLE batch (raises
        QuotaExceeded, nothing stored) if that would cross the cap. This is
        what "per-chunk aware... a batch that would cross the cap is refused
        up front, before any chunk is stored" means atomically: the dedup
        scan, the cap check, and every insert happen while self._lock is
        held continuously for the WHOLE batch, so no concurrent write on
        this store instance can interleave between the check and the last
        insert -- see QuotaExceeded's docstring for the race this closes vs.
        a preflight count-then-insert design.

        Every candidate statement is embedded OUTSIDE self._lock, BEFORE the
        dedup scan (finding): we don't know ahead of time which
        statements will survive dedup, and holding the lock across N
        embedder calls would serialize every other write to this tenant's
        store behind the whole batch for the duration -- cheap for the
        baked-in fastembed default, a real cost for a network-based
        embedder provider. A statement repeated within the batch is only
        ever embedded once (deduped via a dict keyed by statement text).

        Returns {"stored": int, "skipped": int, "claim_ids": [...]}."""
        embeddings = {s: self._embed_claim(s) for s in dict.fromkeys(statements)}
        with self._lock:
            for _ in range(self.max_retries):
                try:
                    return self._remember_facts_batch_attempt(
                        org_id, project, who, statements, embeddings, max_active_claims)
                except _Retry:
                    continue
            raise RuntimeError("remember_facts_batch: exceeded retries")

    def _remember_facts_batch_attempt(self, org_id, project, who, statements,
                                      embeddings, max_active_claims):
        preflight_seq = self._max_seq(org_id, project)
        self._begin_immediate()
        try:
            if self._max_seq(org_id, project) != preflight_seq:
                self._rollback()
                raise _Retry
            # Dedup against BOTH already-active statements in scope AND
            # repeats WITHIN this same batch (finding:
            # has_active_statement() only sees committed DB state, so
            # without a within-batch seen-set two identical statements in
            # one `statements` list -- e.g. a document with a repeated
            # boilerplate chunk -- would both land as separate active
            # claims, double-counting against the cap for no additional
            # value). Runs INSIDE the transaction, after the race-recheck
            # above, so the scan is guaranteed consistent with what we're
            # about to commit -- no separate preflight-then-recheck dance
            # needed for the dedup decision itself.
            to_store = []
            seen_in_batch = set()
            skipped = 0
            for s in statements:
                if s in seen_in_batch or self.has_active_statement(org_id, project, s):
                    skipped += 1
                    continue
                seen_in_batch.add(s)
                to_store.append(s)
            self._enforce_claim_cap(org_id, max_active_claims, additions=len(to_store))
            claim_ids = []
            seq = preflight_seq
            for stmt in to_store:
                seq += 1
                cid = self._insert(org_id, project, who, stmt, None, None, None,
                                   seq, embedding=embeddings[stmt])
                self._audit_write(org_id, project, who, "remember_fact", cid,
                                  preflight_seq=preflight_seq, commit_seq=seq)
                claim_ids.append(cid)
            if to_store:
                self._ensure_project(org_id, project)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {"stored": len(to_store), "skipped": skipped, "claim_ids": claim_ids}

    # --- learn: provenance-attached remember (bridge ingest -> memory) -------
    def learn(self, org_id, project, who, statement, source, reason=None,
              force_exception=None, max_active_claims=None, project_limit=None):
        """Provenance-attached remember: "from reading <source>, I believe <statement>".

        Bridges the fast-path /ingest world (reference chunks, no gate) to the
        decision world: an agent that read something and formed a BELIEF runs
        that belief through the FULL contradiction pipeline — identical to
        remember() (normalize -> structural -> NLI -> tiered judge -> guarded
        write) — with the `source` folded into the stored reason so provenance
        rides with the claim and surfaces in recall()/why() (which echo `reason`,
        not a separate source field).

        `source` is required: it is what distinguishes a learned claim from a bare
        remembered one. The contradiction check runs on the STATEMENT only
        (remember normalizes statement, not reason), so provenance never pollutes
        the judge. A learned claim always carries a reason, so it can win why().

        Returns Stored on success, Conflict if the belief blocks on an existing
        active claim (and force_exception is None) — same contract as remember().

        No lock here: this method only validates `source` and composes the
        reason string (pure Python, no `self.db` touch) before delegating to
        remember(), which owns its own fine-grained lock scope. Wrapping this
        delegation in `self._lock` would hold the lock across remember()'s
        slow normalize+embed calls too — exactly the cross-tenant-lock problem
        remember()'s own scoping fix exists to avoid (a 2026-07-13 review).

        `max_active_claims`/`project_limit` (PLAN-METERING):
        passed straight through to remember() -- see its docstring.
        """
        if not source or not str(source).strip():
            raise ValueError("learn requires a non-empty source")
        src = str(source).strip()
        if reason and reason.strip():
            composed = f"{reason.strip()}\n[learned from: {src}]"
        else:
            composed = f"Learned from reading {src}."
        return self.remember(org_id, project, who, statement, reason=composed,
                             force_exception=force_exception,
                             max_active_claims=max_active_claims,
                             project_limit=project_limit)

    # --- supersede (guarded write) ---------------------------------------
    def supersede(self, org_id, project, who, existing_id, statement, reason=None):
        """Replace an active claim with a new one — RUNS THE CONTRADICTION GUARD
        against all OTHER active claims first (#2/#3/#4).

        Unlike remember()/learn(), supersede() has no force_exception override
        today, so a NormalizeUnavailable (provider error — infra failure OR a
        permanent 4xx) always fails CLOSED here: audited, then re-raised for
        server.py to map to HTTP 503 (retryable) or 502 (permanent — finding
        #3).

        Normalize + embed happen OUTSIDE `self._lock` (#4, hardened further by
        a 2026-07-13 review — see remember()'s docstring for the full
        rationale): `self._lock` is scoped ONLY to `_supersede_attempt`, the DB
        read/write phase."""
        # Normalize AND embed OUTSIDE the writer slot (#4).
        try:
            with metrics.time_stage("normalize"):
                incoming_norm = self.normalizer.normalize({"text": statement})
        except NormalizeUnavailable as e:
            with self._lock:
                self._audit_degraded(org_id, project, who, statement, e,
                                     resolution="degraded_refused")
            raise
        incoming = {"text": statement, "norm": incoming_norm,
                    "embedding": self._embed_claim(statement, reason)}

        last_err = None
        for _ in range(self.max_retries):
            try:
                with self._lock:
                    return self._supersede_attempt(org_id, project, who, existing_id,
                                                   statement, reason, incoming)
            except _Retry:
                last_err = "retry"
                continue
        raise RuntimeError(f"supersede: exceeded retries ({last_err})")

    def _supersede_attempt(self, org_id, project, who, existing_id, statement,
                           reason, incoming):
        # 1. preflight: confirm existing claim, project-scoped (#3)
        row = self._exec(
            "SELECT * FROM claims WHERE org_id=? AND project=? AND id=? AND status='active'",
            (org_id, project, existing_id)).fetchone()
        if not row:
            raise KeyError(
                f"no active claim {existing_id} in (org={org_id}, project={project})")
        preflight_seq = self._max_seq(org_id, project)
        others = self._active_rows(org_id, project, exclude_id=existing_id)
        with metrics.time_stage("judge"):
            blocking, warnings = self._judge_against(incoming, others)
        if blocking:
            # supersede would leave the new claim conflicting with another active
            # claim — refuse and surface (#2).
            return Conflict(incoming_text=statement, incoming_norm=incoming["norm"],
                            conflicts=blocking, warnings=warnings)

        # 2. commit phase under BEGIN IMMEDIATE + race re-check
        self._begin_immediate()
        try:
            if self._max_seq(org_id, project) != preflight_seq:
                self._rollback()
                raise _Retry
            # re-confirm existing is still active under the lock
            still = self._exec(
                "SELECT id FROM claims WHERE org_id=? AND project=? AND id=? AND status='active'",
                (org_id, project, existing_id)).fetchone()
            if not still:
                self._rollback()
                raise _Retry
            self._exec(
                "UPDATE claims SET status='superseded' WHERE org_id=? AND project=? AND id=?",
                (org_id, project, existing_id))
            commit_seq = preflight_seq + 1
            cid = self._insert(org_id, project, who, statement, reason,
                               incoming["norm"], existing_id, commit_seq,
                               embedding=incoming.get("embedding"))
            self._audit_write(org_id, project, who, "supersede", cid,
                              preflight_seq=preflight_seq, commit_seq=commit_seq)
            self._exec(
                "INSERT INTO conflict_events(org_id,project,who,incoming,against,"
                "stored_claim_id,resolution,preflight_seq,commit_seq,judge_version,"
                "normalizer_version,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (org_id, project, who, statement, existing_id, cid, "superseded",
                 preflight_seq, commit_seq, JUDGE_VERSION, self.normalizer_version, _now()))
            for w in warnings:
                self._audit_conflict(org_id=org_id, project=project, who=who,
                                     incoming=statement, hit=w, resolution="warned",
                                     stored_claim_id=cid,
                                     preflight_seq=preflight_seq, commit_seq=commit_seq)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Stored(claim_id=cid, warnings=warnings)

    def store_with_conflict_exception(self, org_id, project, who, statement,
                                      reason, exception_reason):
        return self.remember(org_id, project, who, statement, reason,
                             force_exception=exception_reason)

    # --- retract (wrong-at-birth removal) ---------------------------------
    def retract(self, org_id, project, who, claim_id, reason):
        """Remove a wrong-at-birth claim from the active belief set.

        Retract vs supersede: retract = "this claim was WRONG AT BIRTH (typo,
        test junk, mis-scoped write) — remove it from the active belief set";
        supersede = "this decision CHANGED — replace it and keep the why-chain".
        A genuinely revised decision must go through supersede() so the history
        explains the change.

        NO contradiction gate runs (removal cannot create a contradiction), so
        no normalizer or embedder call either. No new claim row is inserted and
        no claim_seq is consumed: the existing row flips to status='retracted'
        with `retract_reason` + `retracted_at` persisted, staying visible in
        history() while every active-set read path (active/recall/why/judge)
        excludes it structurally via its status='active' filter.

        `reason` is REQUIRED — removal is audit-loud, like every other write.
        Raises ValueError on a blank reason, KeyError if there is no ACTIVE
        claim with that id in (org_id, project).
        """
        with self._lock:
            if not reason or not str(reason).strip():
                raise ValueError("retract requires a non-empty reason")
            reason = str(reason).strip()
            last_err = None
            for _ in range(self.max_retries):
                try:
                    return self._retract_attempt(org_id, project, who, claim_id, reason)
                except _Retry:
                    last_err = "retry"
                    continue
            raise RuntimeError(f"retract: exceeded retries ({last_err})")

    def _retract_attempt(self, org_id, project, who, claim_id, reason):
        # 1. preflight: confirm the claim is active, project-scoped
        row = self._exec(
            "SELECT * FROM claims WHERE org_id=? AND project=? AND id=? AND status='active'",
            (org_id, project, claim_id)).fetchone()
        if not row:
            raise KeyError(
                f"no active claim {claim_id} in (org={org_id}, project={project})")
        preflight_seq = self._max_seq(org_id, project)

        # 2. commit phase under BEGIN IMMEDIATE + race re-check
        self._begin_immediate()
        try:
            if self._max_seq(org_id, project) != preflight_seq:
                self._rollback()
                raise _Retry
            # re-confirm the claim is still active under the lock
            still = self._exec(
                "SELECT id FROM claims WHERE org_id=? AND project=? AND id=? AND status='active'",
                (org_id, project, claim_id)).fetchone()
            if not still:
                self._rollback()
                raise _Retry
            self._exec(
                "UPDATE claims SET status='retracted', retract_reason=?, retracted_at=? "
                "WHERE org_id=? AND project=? AND id=?",
                (reason, _now(), org_id, project, claim_id))
            # no new claim row (no claim_seq consumed) and no conflict_events
            # row (nothing was judged) — just the write audit.
            self._audit_write(org_id, project, who, "retract", claim_id,
                              preflight_seq=preflight_seq)
            self._commit()
        except Exception:
            self._rollback()
            raise
        return Retracted(claim_id=claim_id)

    # --- retention/eviction (TTL + max-count, private-repo issue #123) -----------------
    def _eviction_candidates(self, org_id, project, ttl_days, max_active):
        """Select the active claims a retention sweep WOULD evict, oldest
        first, WITHOUT mutating anything -- shared by `evict()`'s dry_run and
        real path so the two can never disagree on selection logic.

        `project=None` scopes the whole tenant (every project under org_id);
        a specific `project` scopes to just that one -- same convention as
        `export_claims`. Off-by-default (the CRITICAL load-bearing
        invariant): if BOTH `ttl_days` and `max_active` are falsy (None or
        0), this returns an empty list unconditionally, no query at all --
        a tenant that never configures retention can never lose a claim to
        a sweep, regardless of what caller/route drives it.

        TTL selects every active claim with `created_at` older than
        `ttl_days` days ago. `max_active` then trims the claims that would
        SURVIVE the TTL pass down to the ceiling, oldest-by-`created_at`
        first (tie-broken by `claim_seq`, then `id`, for determinism) -- so
        the two knobs compose: TTL always removes what's stale, and
        max_active enforces the ceiling on whatever remains after that.
        `claim_seq` is a per-(org_id, project) counter, NOT a tenant-wide
        one, so a `project=None` tenant-wide sweep must never order by
        `claim_seq` alone -- two projects' seq 1,2,3.. interleave, and a
        brand-new claim in a young project would be evicted ahead of a
        genuinely older claim in another project. Within a single project
        (`project=<name>`) `created_at` order and `claim_seq` order agree,
        so this is a no-op change for that case. Returns a list of
        `sqlite3.Row` (id, project, claim_seq, created_at), deduplicated, in
        no particular final order (callers needing an order re-sort)."""
        if not ttl_days and not max_active:
            return []
        if project is not None:
            scope_sql = "org_id=? AND project=?"
            scope_params = (org_id, project)
        else:
            scope_sql = "org_id=?"
            scope_params = (org_id,)
        selected: dict[str, sqlite3.Row] = {}
        ttl_ids: set[str] = set()
        if ttl_days:
            cutoff = _iso_days_ago(ttl_days)
            rows = self._exec(
                f"SELECT id, project, claim_seq, created_at FROM claims "
                f"WHERE {scope_sql} AND status='active' AND created_at < ? "
                f"ORDER BY created_at ASC, claim_seq ASC, id ASC",
                scope_params + (cutoff,)).fetchall()
            for r in rows:
                ttl_ids.add(r["id"])
                selected[r["id"]] = r
        if max_active:
            active_rows = self._exec(
                f"SELECT id, project, claim_seq, created_at FROM claims "
                f"WHERE {scope_sql} AND status='active' "
                f"ORDER BY created_at ASC, claim_seq ASC, id ASC",
                scope_params).fetchall()
            non_ttl = [r for r in active_rows if r["id"] not in ttl_ids]
            excess = len(non_ttl) - max_active
            if excess > 0:
                for r in non_ttl[:excess]:
                    selected[r["id"]] = r
        return list(selected.values())

    def evict(self, org_id: str, project: str | None, who: str, *,
             ttl_days: int | None = None, max_active: int | None = None,
             dry_run: bool = False) -> dict:
        """Audit-loud retention sweep: flip the selected active claims to
        `status='evicted'`, NEVER a hard delete (the load-bearing "Ergo
        remembers forever" promise -- eviction just removes a claim from the
        ACTIVE belief set, exactly like `retract()`, and stays in `history()`
        forever with `status='evicted'`).

        Off by default: a caller that passes neither
        `ttl_days` nor `max_active` gets `evicted_count: 0` unconditionally --
        see `_eviction_candidates`'s docstring for why that check happens
        BEFORE any query runs, not just before any mutation.

        `dry_run=True` (count-first preview) returns the candidate list
        WITHOUT mutating anything -- a plain read under `self._lock`, no
        `BEGIN IMMEDIATE`, mirrors `purge_project`'s dry_run contract.

        The real path flips every candidate under ONE `BEGIN IMMEDIATE`
        transaction: `UPDATE ... SET status='evicted', evicted_reason=?,
        evicted_at=? WHERE id=? AND status='active'` (the `status='active'`
        re-check closes the same race `retract()`'s preflight/re-check
        pattern closes -- a claim retracted/superseded/evicted by a
        concurrent write between selection and this UPDATE simply updates
        zero rows and is silently skipped, never double-evicted or
        clobbered), one `_audit_write(..., "evict", claim_id, ...)` row per
        successful eviction (mirrors `retract()` -- no new claim row, no
        `claim_seq` consumed, no `conflict_events` row -- nothing was
        judged), and drops the claim's `vec_claims` row (if retrieval is
        enabled) so it leaves recall/why immediately, exactly like a
        retracted claim already does structurally via the `status='active'`
        filter every active-set read path applies.

        Returns {"org_id", "project", "dry_run", "evicted_count",
        "evicted_ids": [...]}. `evicted_ids` on the real path is only the
        claims that ACTUALLY flipped (excludes any lost race)."""
        with self._lock:
            candidates = self._eviction_candidates(org_id, project, ttl_days, max_active)
            if dry_run or not candidates:
                return {
                    "org_id": org_id, "project": project, "dry_run": dry_run,
                    "evicted_count": len(candidates),
                    "evicted_ids": [r["id"] for r in candidates],
                }
            reason = _eviction_reason(ttl_days, max_active)
            now = _now()
            self._begin_immediate()
            try:
                evicted_ids = []
                for r in candidates:
                    cur = self._exec(
                        "UPDATE claims SET status='evicted', evicted_reason=?, "
                        "evicted_at=? WHERE id=? AND status='active'",
                        (reason, now, r["id"]))
                    if cur.rowcount == 0:
                        # lost a race to a concurrent retract/supersede/evict
                        # on this exact claim since selection -- skip, never
                        # double-evict or clobber whatever it became.
                        continue
                    evicted_ids.append(r["id"])
                    if self.retrieval:
                        self._exec(
                            "DELETE FROM vec_claims WHERE claim_id=?", (r["id"],))
                    self._audit_write(org_id, r["project"], who, "evict", r["id"])
                self._commit()
            except Exception:
                self._rollback()
                raise
            return {
                "org_id": org_id, "project": project, "dry_run": False,
                "evicted_count": len(evicted_ids), "evicted_ids": evicted_ids,
            }

    # --- admin: hard-delete a project (row-level, within a SHARED store) --
    def purge_project(self, org_id: str, project: str, dry_run: bool = False) -> dict:
        """Hard-delete every row for one `(org_id, project)` out of a shared
        (pooled) store — the admin-side counterpart to test-pollution cleanup
        (e.g. thousands of `ingest-selftest*` claims), distinct from
        `tenant_ops.delete_tenant`'s whole-silo-file purge: this is row-level
        within ONE database that many (org_id, project) pairs already share.

        Deletes, in ONE `BEGIN IMMEDIATE` transaction (single-writer, same
        pattern as `_remember_attempt`/`_retract_attempt`):
          - `claims` — every status (active/superseded/retracted), not just
            active, so a purge is a REAL purge, not a soft one.
          - `vec_claims` — the matching sqlite-vec `vec0` rows, targeted by
            `claim_id IN (SELECT id FROM claims WHERE ...)` so ONLY the purged
            project's own vectors are removed; a foreign project's rows in the
            same shared KNN index are untouched (`retrieval` off — no embedder
            configured — means the table was never created, so this is
            skipped entirely; see `_init_vec`). Deleted BEFORE `claims` itself
            so the subquery still has the claim ids to join against.
          - `writes` + `conflict_events` — full audit-trail purge for the
            scope (there is no "why" left to explain once the project itself
            is gone, unlike `retract()` which keeps the row for history).
          - the `projects` registry row, if present.

        Idempotent (KEY property): a non-existent/empty project deletes
        nothing and returns all-zero counts — never an error, never a
        KeyError, mirroring `remember_facts_batch`'s "nothing to do" no-op
        shape rather than `retract()`'s "must already exist" contract.

        `dry_run=True` returns the counts that WOULD be deleted (a plain read
        under `self._lock`, no `BEGIN IMMEDIATE`, no mutation at all) so a
        caller can preview before committing to an irreversible hard delete.

        Bulk single-statement deletes (not a Python-side loop) are safe against
        the `claims.supersedes` self-referencing FK here specifically BECAUSE
        every row for the scope is removed together: SQLite's foreign-key
        check for `PRAGMA foreign_keys=ON` runs at statement completion, and by
        the time this DELETE finishes, no row in (org_id, project) still
        references another — the whole chain left the table in the same
        statement.
        """
        with self._lock:
            claims_count = self._exec(
                "SELECT count(*) FROM claims WHERE org_id=? AND project=?",
                (org_id, project)).fetchone()[0]
            if self.retrieval:
                vectors_count = self._exec(
                    "SELECT count(*) FROM vec_claims WHERE claim_id IN "
                    "(SELECT id FROM claims WHERE org_id=? AND project=?)",
                    (org_id, project)).fetchone()[0]
            else:
                vectors_count = 0
            writes_count = self._exec(
                "SELECT count(*) FROM writes WHERE org_id=? AND project=?",
                (org_id, project)).fetchone()[0]
            conflict_events_count = self._exec(
                "SELECT count(*) FROM conflict_events WHERE org_id=? AND project=?",
                (org_id, project)).fetchone()[0]

            if dry_run:
                return {
                    "org_id": org_id, "project": project, "dry_run": True,
                    "claims_deleted": claims_count,
                    "vectors_deleted": vectors_count,
                    "writes_deleted": writes_count,
                    "conflict_events_deleted": conflict_events_count,
                }

            self._begin_immediate()
            try:
                # vec_claims FIRST: targets claim ids via a subquery against
                # `claims`, which must still be intact when this runs.
                if self.retrieval:
                    self._exec(
                        "DELETE FROM vec_claims WHERE claim_id IN "
                        "(SELECT id FROM claims WHERE org_id=? AND project=?)",
                        (org_id, project))
                self._exec(
                    "DELETE FROM writes WHERE org_id=? AND project=?",
                    (org_id, project))
                self._exec(
                    "DELETE FROM conflict_events WHERE org_id=? AND project=?",
                    (org_id, project))
                self._exec(
                    "DELETE FROM claims WHERE org_id=? AND project=?",
                    (org_id, project))
                self._exec(
                    "DELETE FROM projects WHERE org_id=? AND project=?",
                    (org_id, project))
                self._commit()
            except Exception:
                self._rollback()
                raise
            return {
                "org_id": org_id, "project": project, "dry_run": False,
                "claims_deleted": claims_count,
                "vectors_deleted": vectors_count,
                "writes_deleted": writes_count,
                "conflict_events_deleted": conflict_events_count,
            }

    # --- read API (typed empty-store states #34) -------------------------
    def active(self, org_id, project, limit=None, offset=0,
               projection="full") -> ActiveSet:
        """Enumerate active claims in a scope, paginated (private-repo issue #185) and
        optionally projected down to a summary shape (private-repo issue #206).

        `limit=None` (the default parameter value, NOT `DEFAULT_ACTIVE_LIMIT`)
        is the pre-#185 unbounded mode: every active row, one query, no
        separate `COUNT(*)` (count is just `len(rows)`, free). This is the
        explicit opt-out non-negotiable #5 requires -- callers that need the
        full set (the contradiction judge via `_active_rows` directly, and
        the `/health`/`/ready` probes at server.py which only ever read
        `.state`) pass no `limit` and so never pay for or silently inherit a
        cap. `ACTIVE_BYTE_BUDGET` never applies in this mode either, for the
        same reason: it's a paging concern, and unbounded callers never paged.

        Passing a `limit` opts into the capped, paginated mode: it is
        clamped to `[1, MAX_ACTIVE_LIMIT]` here regardless of what the caller
        passes, and a non-numeric/`None` `limit` degrades to
        `DEFAULT_ACTIVE_LIMIT` rather than raising -- same clamp shape as
        `audit_log()`/`DEFAULT_AUDIT_LIMIT` (private-repo issue #124). The `/active` HTTP
        route and the `ergo_active` MCP tool are the callers that opt in,
        applying `DEFAULT_ACTIVE_LIMIT` themselves when the caller's own
        request omits `limit`. Once a page of up to `limit` rows is fetched,
        `ACTIVE_BYTE_BUDGET` (private-repo issue #206) may narrow it further -- a verbose
        corpus can still blow the wire even at a capped row count, and a
        byte budget self-tunes across corpora where a fixed `limit` can't.
        `truncated_reason` says which bound actually fired ("row_limit" or
        "byte_budget") so a caller knows whether raising `limit` would help.

        `projection` (private-repo issue #206, default `"full"`) is a no-op unless it's
        `"summary"`, which drops every column but `id`/`statement` -- `reason`
        is the bulk of a claim's bytes and, per the reporter, the field a
        "what does this project believe?" browse rarely needs. A bad value
        degrades to `"full"` rather than raising, same fail-open shape as a
        bad `limit`/`offset`. Orthogonal to pagination: applies in both the
        unbounded and paginated branches, and to the byte-budget calculation
        below (a summary row is a fraction of a full row's size, so the
        budget rarely binds once a caller opts into summary mode).

        `state` always describes the SCOPE, not the page (non-negotiable #4):
        it is computed from `count` (the true total), never from how many
        rows this page returned, so `offset` past the end of a non-empty
        scope still reports `has_active_claims` with an empty `claims` list,
        never `empty`.
        """
        if projection not in ACTIVE_PROJECTIONS:
            projection = "full"
        with self._lock:
            proj = self._exec(
                "SELECT 1 FROM projects WHERE org_id=? AND project=?",
                (org_id, project)).fetchone()
            if limit is None:
                rows = self._active_rows(org_id, project)
                claims = [_project_claim(dict(r), projection) for r in rows]
                count = len(claims)
                returned = count
                truncated = False
                next_offset = None
                truncated_reason = None
            else:
                try:
                    limit = max(1, min(int(limit), MAX_ACTIVE_LIMIT))
                except (TypeError, ValueError):
                    limit = DEFAULT_ACTIVE_LIMIT
                try:
                    offset = max(0, int(offset))
                except (TypeError, ValueError):
                    offset = 0
                count = self._exec(
                    "SELECT COUNT(*) c FROM claims WHERE org_id=? AND project=? "
                    "AND status='active'", (org_id, project)).fetchone()["c"]
                rows = self._active_rows(org_id, project, limit=limit, offset=offset)
                claims = [_project_claim(dict(r), projection) for r in rows]
                claims, budget_hit = _apply_byte_budget(claims, ACTIVE_BYTE_BUDGET)
                returned = len(claims)
                truncated = (offset + returned) < count
                next_offset = (offset + returned) if truncated else None
                truncated_reason = ("byte_budget" if budget_hit else
                                     "row_limit" if truncated else None)
            if count > 0:
                state = "has_active_claims"
            elif proj is None:
                state = "project_missing"
            else:
                state = "empty"
            return ActiveSet(state, claims, count=count, returned=returned,
                             truncated=truncated, next_offset=next_offset,
                             truncated_reason=truncated_reason)

    def project_state(self, org_id, project) -> str:
        """Cheap three-state existence check -- SAME vocabulary as
        `ActiveSet.state` (`"project_missing"` | `"empty"` |
        `"has_active_claims"`), but without `active()`'s full row fetch
        (private-repo issue #184 follow-up: /recall and /why's `scope.state` needs this
        when their OWN result set is empty).

        Deliberately does NOT reuse "no row in `projects`" as a proxy for
        "has no active claims" -- an earlier version of this check did
        exactly that (`project_exists()`), which is wrong: a project can
        hold thousands of active claims while `/why`'s reasoned-only filter
        or `/recall`'s global-KNN-then-post-filter both legitimately come
        back empty for THIS query, and the row in `projects` still exists.
        Reporting `"empty"` in that case is a false claim in the exact field
        this feature exists to make trustworthy -- it also directly
        contradicts what `/active` on the same scope would say. So this
        checks claims FIRST (the thing that actually matters) and only asks
        "does the project exist at all" to disambiguate the zero-claims
        case, exactly like `active()` does -- just without loading the rows.

        `LIMIT 1` on both queries: this only ever needs an existence bit,
        never a count or the rows themselves.

        Not called under this store's own external lock chain from the
        caller side -- reads its own consistent snapshot but is NOT
        transactionally atomic with whatever `recall()`/`why()` call
        preceded it (their locks are already released by the time this
        runs), so a concurrent write in between can make this stale. That's
        acceptable for a diagnostic field; see docs/HTTP-API.md."""
        with self._lock:
            has_claim = self._exec(
                "SELECT 1 FROM claims WHERE org_id=? AND project=? "
                "AND status='active' LIMIT 1",
                (org_id, project)).fetchone()
            if has_claim is not None:
                return "has_active_claims"
            proj = self._exec(
                "SELECT 1 FROM projects WHERE org_id=? AND project=? LIMIT 1",
                (org_id, project)).fetchone()
            return "empty" if proj is not None else "project_missing"

    def history(self, org_id, project) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._exec(
                "SELECT * FROM claims WHERE org_id=? AND project=? ORDER BY claim_seq",
                (org_id, project)).fetchall()]

    def audit_log(self, org_id, project, limit: int = DEFAULT_AUDIT_LIMIT) -> dict:
        """Read-only tenant-scoped audit trail for GET /audit (private-repo issue #124):
        this org+project's own `writes` and `conflict_events` rows -- the
        same two tables `org_stats()`/`org_graph()` read for superadmin, but
        scoped to the CALLER's own tenant (the route's `forced_org`) rather
        than `_REGISTRY.lease`, so a tenant can self-serve its own write
        history via its own read-only route.

        `limit` bounds EACH stream independently (not their sum) and is
        clamped to `[1, MAX_AUDIT_LIMIT]` here regardless of what the caller
        passes, so a huge tenant can never force an unbounded payload. Within
        that bound, each stream returns its most recent `limit` rows, then
        orders them oldest -> newest by `id` (the autoincrement PK -- always
        monotonic, even when two rows share the same `ts`).

        A non-numeric/`None` `limit` degrades to `DEFAULT_AUDIT_LIMIT` rather
        than raising -- the route already guards this on its own query-param
        parse, but this is a public store method any other caller can reach
        directly, and a read path should fail open the same way `history()`/
        `active()` never raise on a caller's malformed scope.
        """
        with self._lock:
            try:
                limit = max(1, min(int(limit), MAX_AUDIT_LIMIT))
            except (TypeError, ValueError):
                limit = DEFAULT_AUDIT_LIMIT
            writes = [dict(r) for r in self._exec(
                "SELECT * FROM (SELECT * FROM writes WHERE org_id=? AND project=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (org_id, project, limit)).fetchall()]
            conflict_events = [dict(r) for r in self._exec(
                "SELECT * FROM (SELECT * FROM conflict_events WHERE org_id=? AND project=? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id",
                (org_id, project, limit)).fetchall()]
            return {"writes": writes, "conflict_events": conflict_events}

    def export_claims(self, org_id: str, project: str | None = None,
                      limit: int | None = None, offset: int = 0) -> ExportSet:
        """Read ACTIVE claims directly off the `claims` table for GET /export
        (export-import phase-1 brief; nested why-chain
        added private-repo issue #189; pagination added private-repo issue #203) -- deliberately NOT
        via recall()/why() (a semantic-search read over an embedded subset),
        so every claim in scope round-trips regardless of whether retrieval
        is even enabled on this server.

        PAGINATION (private-repo issue #203): `limit=None` (the default) is the
        pre-#203 unbounded mode -- every head, one page, no truncation.
        This is the deliberate backward-compat choice for export
        specifically (see DEFAULT_EXPORT_LIMIT's comment above `active()`'s
        equivalent /active chose the opposite default): an existing
        backup/migration caller passing no `limit` must keep getting the
        FULL scope. Passing a `limit` opts into the SAME loud-truncation
        shape `active()` uses (`count`/`returned`/`truncated`/
        `next_offset`, returned via `ExportSet` instead of `ActiveSet`),
        clamped to `[1, MAX_EXPORT_LIMIT]` and degrading a non-numeric/
        `None` limit to `DEFAULT_EXPORT_LIMIT` -- same clamp-and-degrade
        contract as `active()`/`audit_log()`.

        Pagination happens over HEADS, in Python, AFTER the full
        chain-resolution scan below -- not pushed into SQL LIMIT/OFFSET
        like `_active_rows()`. This is deliberate, not a shortcut: a
        head's supersede chain can straddle arbitrary claim_seq positions
        (a predecessor can sit anywhere earlier in the scope), so
        correctly assembling ANY head's `revisions[]` already requires the
        WHOLE scope's active+superseded rows in memory -- true even before
        this issue, since a single SELECT was always the design (see
        QUERY COUNT below). Slicing the OUTPUT list of heads is the only
        place pagination can apply without either splitting one head's
        chain across two pages (a torn `revisions[]` that would corrupt a
        round trip through /import) or re-querying per head (N+1). The
        trade-off: each page call redoes the full-scope scan, so cost is
        O(scope size) per page rather than O(page size) -- accepted
        because export's correctness bar (never split a chain, never lose
        or duplicate a claim on round trip) matters more here than raw
        query cost, and a single full-scope SELECT already existed
        pre-pagination.

        SNAPSHOT CONSISTENCY (private-repo issue #203): see `ExportSet.as_of_seq`'s own
        docstring for the full reasoning -- in short, `as_of_seq` is a
        `{project: max_claim_seq}` marker computed from this call's own
        row scan, letting a multi-page caller DETECT (not prevent) a scope
        that was written to mid-walk, rather than silently composing a
        file that never corresponded to any single instant of the store.

        NO vectors are ever included: embeddings are embedder/dimension-
        specific (embed_dim is fixed at vec-table creation -- see
        `_init_vec`) and are always RE-COMPUTED on import via `remember()`/
        `learn()`'s own `_embed_claim` call, so shipping them would tie an
        export to one embedder/dimension forever (same rationale as
        migrate_silo.py's dimension-safety guards for org-to-org copies).

        `project=None` exports every project in `org_id`; a specific
        project scopes to just that one. The top-level `claims` list is
        still one entry per ACTIVE claim ("head") -- but private-repo issue #189 attaches
        each head's supersede history as a nested `revisions` list (OMITTED
        entirely, not an empty/null placeholder, for a head that was never
        revised) rather than dropping it:

            {..., "status": "active", "revisions": [
                {"statement": ..., "reason": ..., "who": ..., "status":
                 "superseded", "created_at": ...},   # oldest predecessor first
                ...,
            ]}

        Chain order is OLDEST-FIRST (revisions[0] is the very first claim
        ever made in that lineage, revisions[-1] is the one immediately
        before the current active head) -- a client reads it top-to-bottom
        as the decision's narrative. `revisions` entries are the plain
        `{statement, reason, who, status, created_at}` fields off the
        superseded row itself -- e.g. a claim's OWN `reason` at the time it
        was superseded, not the reason of whatever replaced it.

        RETRACTED claims are excluded entirely -- not as a head, and never
        inside any head's `revisions`. `retract` means wrong-at-birth
        (typo, test junk, mis-scoped write); re-exporting one would resurrect
        noise the user deliberately removed. That history stays available via
        `/history`. This exclusion falls out structurally: `retract()` only
        ever flips a claim that is CURRENTLY `status='active'` (see its own
        docstring), so a superseded row -- the only kind that can appear
        inside a `revisions` chain -- can never have been retracted; and a
        retracted claim, having left `status='active'`, is never itself a
        head. The query below simply never selects `status='retracted'`
        rows, so they can't reach either position.

        QUERY COUNT: still exactly ONE SELECT (two for a whole-org export:
        the loop below is pure Python over rows already in memory), same as
        before this issue -- widened from `status='active'` to
        `status IN ('active','superseded')` so every chain link for every
        head is available up front, then each head's chain is walked
        backwards in-memory against that one result set. No per-claim or
        per-chain-link query, so this does not degenerate into N+1 on a
        tenant with thousands of claims or long chains.

        `source` and `tier` are always `None` in Phase 1 (heads AND
        revisions): neither is a persisted per-claim column today -- a
        `/learn` claim's provenance is folded straight into `reason` (e.g.
        "...\\n[learned from: <source>]"), and `tier` only ever exists
        transiently on a judge verdict / `conflict_events` row, never on the
        claim itself. They are kept in the head's export shape (rather than
        omitted) so a client parses one stable JSON contract from day one;
        POST /import still honors an explicit `source` on a hand-crafted
        import payload (routes it through the `/learn` semantics), even
        though a round-tripped export never populates one. `revisions`
        entries never carry `source`/`tier`/`id`/`project` at all -- they are
        folded into the imported claim's `reason` on the way back in (see
        `_import_merge` in server.py), not re-stored as their own rows.
        """
        with self._lock:
            if project is not None:
                rows = self._exec(
                    "SELECT * FROM claims WHERE org_id=? AND project=? "
                    "AND status IN ('active','superseded') ORDER BY claim_seq",
                    (org_id, project)).fetchall()
            else:
                rows = self._exec(
                    "SELECT * FROM claims WHERE org_id=? "
                    "AND status IN ('active','superseded') ORDER BY project, claim_seq",
                    (org_id,)).fetchall()
            by_id = {r["id"]: r for r in rows}
            # A single scoped `project` always gets a key here, even 0 for
            # an empty/never-used project (finding on private-repo PR #208) -- the
            # caller passed an explicit project, so there's exactly one key
            # to report a baseline for, and a missing key would read as
            # "no marker available" rather than "nothing written yet".
            # `project=None` (whole-org) has no such single key to default:
            # an empty org legitimately has no projects to enumerate, so {}
            # stands.
            as_of_seq: dict[str, int] = {project: 0} if project is not None else {}
            heads = []
            for r in rows:
                as_of_seq[r["project"]] = max(as_of_seq.get(r["project"], 0),
                                              r["claim_seq"])
                if r["status"] == "active":
                    heads.append(r)
            count = len(heads)
            if limit is None:
                page = heads
                returned = count
                truncated = False
                next_offset = None
            else:
                try:
                    limit = max(1, min(int(limit), MAX_EXPORT_LIMIT))
                except (TypeError, ValueError):
                    limit = DEFAULT_EXPORT_LIMIT
                try:
                    offset = max(0, int(offset))
                except (TypeError, ValueError):
                    offset = 0
                page = heads[offset:offset + limit]
                returned = len(page)
                truncated = (offset + returned) < count
                next_offset = (offset + returned) if truncated else None
            out = []
            for r in page:
                revisions = []
                seen = {r["id"]}
                cur = r["supersedes"]
                while cur:
                    if cur in seen:
                        # Defensive: a cycle can never arise through the
                        # guarded write path (supersedes is set exactly
                        # once at insert time, to a claim_seq that already
                        # existed), but a read endpoint must degrade rather
                        # than infinite-loop on a corrupted/legacy row.
                        break
                    pred = by_id.get(cur)
                    if pred is None:
                        break
                    seen.add(cur)
                    revisions.append({
                        "statement": pred["statement"], "reason": pred["reason"],
                        "who": pred["who"], "status": pred["status"],
                        "created_at": pred["created_at"],
                    })
                    cur = pred["supersedes"]
                revisions.reverse()  # oldest-first
                entry = {
                    "id": r["id"], "project": r["project"], "statement": r["statement"],
                    "reason": r["reason"], "source": None, "status": r["status"],
                    "who": r["who"], "tier": None, "created_at": r["created_at"],
                }
                if revisions:
                    entry["revisions"] = revisions
                out.append(entry)
            return ExportSet(claims=out, count=count, returned=returned,
                             truncated=truncated, next_offset=next_offset,
                             as_of_seq=as_of_seq)

    def active_claim_count(self, org_id: str) -> int:
        """Cheap scalar for a tenant's current active-claim count -- the
        same predicate as org_stats()'s active_claims, split out as a
        dedicated one-liner so a caller that only wants the count doesn't
        pay for org_stats' extra projects/conflict-events/last-write
        aggregates.

        NOT used on the /remember, /learn, /ingest hot path:
        that cap check now runs INSIDE the write-guard transaction via
        `_enforce_claim_cap` (atomic with the insert -- see QuotaExceeded's
        docstring for why a preflight count-then-insert here was a TOCTOU
        race). This method remains a plain read-only stat query, e.g. for
        admin/diagnostic use outside the write path."""
        with self._lock:
            return self._exec(
                "SELECT count(*) FROM claims WHERE org_id=? AND status='active'",
                (org_id,)).fetchone()[0]

    def org_stats(self, org_id: str) -> dict:
        """Read aggregate claim/write/conflict stats for one tenant org."""
        with self._lock:
            active_claims = self._exec(
                "SELECT count(*) FROM claims WHERE org_id=? AND status='active'",
                (org_id,)).fetchone()[0]
            projects = [
                {"project": row["project"], "active_claims": row["active_claims"]}
                for row in self._exec(
                    "SELECT project, count(*) AS active_claims FROM claims "
                    "WHERE org_id=? AND status='active' GROUP BY project ORDER BY project",
                    (org_id,)).fetchall()
            ]
            conflict_events = self._exec(
                "SELECT count(*) FROM conflict_events WHERE org_id=?", (org_id,)
            ).fetchone()[0]
            conflicts_by_day = self._conflicts_by_day(org_id)
            last_write_at = self._exec(
                "SELECT max(ts) FROM writes WHERE org_id=?", (org_id,)
            ).fetchone()[0]
            return {
                "active_claims": active_claims,
                "projects": projects,
                "conflict_events": conflict_events,
                "conflicts_by_day": conflicts_by_day,
                "last_write_at": last_write_at,
            }

    def _conflicts_by_day(self, org_id: str) -> list[dict]:
        """Zero-filled 7-day (UTC, INCLUDING today) daily count of
        conflict_events rows for one org, oldest -> newest, counting ALL
        rows regardless of `resolution` -- the same population org_stats'
        lifetime `conflict_events` total counts, just bucketed by day.

        Must be called with self._lock already held (org_stats' caller).
        One GROUP BY aggregate for the count; the per-day list comprehension
        below is pure zero-fill over 7 fixed dates, not a loop over rows.
        """
        now = time.time()
        days = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
                for i in range(6, -1, -1)]
        rows = self._exec(
            "SELECT substr(ts, 1, 10) AS day, count(*) AS c FROM conflict_events "
            "WHERE org_id=? AND substr(ts, 1, 10) >= ? GROUP BY day",
            (org_id, days[0])).fetchall()
        counts = {row["day"]: row["c"] for row in rows}
        return [{"date": d, "count": counts.get(d, 0)} for d in days]

    def org_graph(self, org_id: str, project: str | None = None) -> dict:
        """Read one tenant org's claims and relationships as a memory graph.

        The response is bounded by the org's claim and conflict-event counts;
        the largest current tenant is small enough to return without pagination.
        """
        with self._lock:
            claim_sql = (
                "SELECT id, project, status, statement, who, created_at, supersedes "
                "FROM claims WHERE org_id=?"
            )
            params: tuple = (org_id,)
            if project is not None:
                claim_sql += " AND project=?"
                params += (project,)
            claim_sql += " ORDER BY project, claim_seq"
            claims = self._exec(claim_sql, params).fetchall()
            claim_ids = {row["id"] for row in claims}

            nodes = [
                {
                    "id": row["id"],
                    "project": row["project"],
                    "status": row["status"],
                    "statement": row["statement"][:200],
                    "who": row["who"],
                    "created_at": row["created_at"],
                }
                for row in claims
            ]
            edges = [
                {"type": "supersedes", "from": row["id"], "to": row["supersedes"]}
                for row in claims
                if row["supersedes"] and row["supersedes"] in claim_ids
            ]

            conflict_sql = (
                "SELECT stored_claim_id, against, tier, resolution, ts "
                "FROM conflict_events WHERE org_id=? "
                "AND stored_claim_id IS NOT NULL AND stored_claim_id != '' "
                "AND against IS NOT NULL AND against != ''"
            )
            conflict_params: tuple = (org_id,)
            if project is not None:
                conflict_sql += " AND project=?"
                conflict_params += (project,)
            conflict_sql += " ORDER BY id"
            for row in self._exec(conflict_sql, conflict_params).fetchall():
                if row["stored_claim_id"] not in claim_ids or row["against"] not in claim_ids:
                    continue
                edges.append({
                    "type": "conflict",
                    "from": row["stored_claim_id"],
                    "to": row["against"],
                    "tier": row["tier"],
                    "resolution": row["resolution"],
                    "ts": row["ts"],
                })
            return {"nodes": nodes, "edges": edges}

    # --- ops diagnostics (make the binary answer for itself) ----------------
    def diagnostics(self) -> dict:
        """Counts + conflict telemetry + version/drift meta read live from the DB.

        Anti-zombie: a running store must be able to report whether it actually
        holds data, when it was last written, and whether a normalizer/judge swap
        has left mixed versions in the table (a silent data-compat hazard).
        """
        with self._lock:
            def scalar(sql, params=()):
                row = self._exec(sql, params).fetchone()
                return row[0] if row else None

            counts = {
                "claims_total": scalar("SELECT count(*) FROM claims"),
                "claims_active": scalar("SELECT count(*) FROM claims WHERE status='active'"),
                "claims_superseded": scalar("SELECT count(*) FROM claims WHERE status='superseded'"),
                "claims_retracted": scalar("SELECT count(*) FROM claims WHERE status='retracted'"),
                "claims_evicted": scalar("SELECT count(*) FROM claims WHERE status='evicted'"),
                "projects": scalar("SELECT count(*) FROM projects"),
            }
            conflicts = {
                "events_total": scalar("SELECT count(*) FROM conflict_events"),
                "flagged_blocks": scalar("SELECT count(*) FROM conflict_events WHERE resolution='flagged'"),
                "warned": scalar("SELECT count(*) FROM conflict_events WHERE resolution='warned'"),
                "force_exceptions": scalar("SELECT count(*) FROM conflict_events WHERE resolution='exception'"),
                "superseded": scalar("SELECT count(*) FROM conflict_events WHERE resolution='superseded'"),
                # Provider-infrastructure degrades (finding #3): refused
                # (503, nothing stored) + forced-through
                # (audited override) both count, so an operator can see the
                # firewall has been degraded at all, not just how it resolved.
                "normalize_degraded_count": scalar(
                    "SELECT count(*) FROM conflict_events "
                    "WHERE resolution IN ('degraded_refused','degraded_exception')"),
                "last_normalize_degraded_at": scalar(
                    "SELECT max(ts) FROM conflict_events "
                    "WHERE resolution IN ('degraded_refused','degraded_exception')"),
            }
            # version-drift detection: more than one distinct stored version is a
            # data-compat event the operator must know about.
            norm_versions = [r[0] for r in self._exec(
                "SELECT DISTINCT normalizer_id || ':' || normalizer_version FROM claims").fetchall()]
            judge_versions = [r[0] for r in self._exec(
                "SELECT DISTINCT judge_version FROM claims").fetchall()]
            schema_versions = [r[0] for r in self._exec(
                "SELECT DISTINCT norm_schema_version FROM claims").fetchall()]
            return {
                "counts": counts,
                "conflicts": conflicts,
                "last_write_at": scalar("SELECT max(created_at) FROM claims"),
                "versions": {
                    "engine_normalizer": f"{self.normalizer_id}:{self.normalizer_version}",
                    "engine_judge": JUDGE_VERSION,
                    "engine_norm_schema": NORM_SCHEMA_VERSION,
                    "stored_normalizers": norm_versions,
                    "stored_judges": judge_versions,
                    "stored_norm_schemas": schema_versions,
                    "drift": (len(norm_versions) > 1 or len(judge_versions) > 1
                              or len(schema_versions) > 1),
                },
            }


# --- utilities -----------------------------------------------------------
def _ulid() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_days_ago(days: int, now: float | None = None) -> str:
    """ISO instant `days` before `now` (epoch seconds, defaults to the real
    current time) -- the TTL cutoff for `_eviction_candidates`. Lexical
    string comparison against `created_at` (also `%Y-%m-%dT%H:%M:%SZ`) is
    valid because both are zero-padded, fixed-width, UTC ISO instants --
    same pattern as `control.py`'s `_autoload_pending_stale_cutoff`."""
    base = now if now is not None else time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(base - days * 86400))


def _eviction_reason(ttl_days: int | None, max_active: int | None) -> str:
    """Human-readable `evicted_reason` persisted on every evicted claim --
    audit-loud, mirrors `retract_reason` always being present."""
    parts = []
    if ttl_days:
        parts.append(f"ttl_days={ttl_days}")
    if max_active:
        parts.append(f"max_active={max_active}")
    return "retention sweep (" + ", ".join(parts) + ")"


if __name__ == "__main__":
    s = ContradictionStore(":memory:")
    org, proj = "acme", "platform"

    def show(r):
        if isinstance(r, Stored):
            print(f"  -> STORED {r.claim_id[:8]}  warnings={len(r.warnings)}")
        else:
            c = r.conflicts[0]
            print(f"  -> BLOCKED: conflicts with {c.claim['who']}'s claim "
                  f"({c.tier}, {c.reason})")

    print('alice: "TLS 1.2 must be enabled in prod"')
    a = s.remember(org, proj, "alice", "TLS 1.2 must be enabled in prod."); show(a)
    print('carol: "/debug may be enabled in dev"  (different env)')
    show(s.remember(org, proj, "carol", "The /debug endpoint may be enabled in dev."))
    print('bob:   "TLS 1.2 must NOT be enabled in prod"  (real conflict)')
    show(s.remember(org, proj, "bob", "TLS 1.2 must not be enabled in prod."))
    print('bob supersedes alice:')
    show(s.supersede(org, proj, "bob", a.claim_id, "TLS 1.2 must not be enabled in prod."))

    print("\nactive set:", s.active(org, proj).state)
    for c in s.active(org, proj).claims:
        print(f"   [{c['who']}] {c['statement']}")
    print("history length:", len(s.history(org, proj)))
