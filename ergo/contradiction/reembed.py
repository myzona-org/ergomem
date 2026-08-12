"""Offline re-embed migration runner (private-repo issue #126).

Embedding dimension is fixed at `vec_claims` creation time (`float[N]`,
cosine) -- see `store.py`'s `_init_vec()`. Switching embedder/provider/dim
across a tenant silo has never had a supported path (CONFIGURATION.md /
RETRIEVAL.md used to say "not yet implemented"): this module is that path.
It is a load-bearing house convention that a dimension
change is NEVER an in-place `ALTER` -- always a re-embed into a brand-new
file. A safe operator wrapper (backup-first, dry-run, verify, report) drives
`plan_reembed()`/`reembed_silo()` here as a guarded shell sequence: the
heavy/testable logic lives in Python, the wrapper is just the sequencing.

Reads the SOURCE database (a single tenant silo `<tenant_id>.db`, or the
legacy pooled `ergo.db`) strictly READ-ONLY (`PRAGMA query_only = 1`, same
technique as `migrate_silo.py`'s `_open_source`) and writes everything into
a brand-new DESTINATION file built via a target embedder from
`embedders.build(...)` (never the process-global `get_embedder()` singleton
-- that would silently re-embed with whatever the CURRENT process happens to
be configured with instead of the operator-chosen target).

ACTIVE-ONLY RE-EMBED CHOICE (documented per private-repo issue #126's brief -- pick one,
test it): the destination's `claims` table is copied IN FULL, every status
(active/superseded/retracted), preserving `id`/`claim_seq`/`supersedes` so
history and the supersede chain (`why()`'s walk) are unaffected. The
`writes` and `conflict_events` audit tables are copied in full too (every
row, no filtering) -- so `audit_log()` / `GET /audit` (private-repo issue #124) keep
their full trail post-migration, not just the claims themselves. But
`vec_claims` only gets ONE vector per ACTIVE claim. This matches how the
store actually uses the vector index today: `recall()`/`why()` structurally
filter to `status='active'` (see store.py's `recall()` docstring --
"superseded/retracted claims are excluded structurally... so the vector
index never needs deleting on supersede"), so a superseded/retracted row's
vector is never read by anything except a full `purge_project()`. Spending
the (potentially $$-metered, always slow) embedding work on vectors that are
provably dead weight would multiply the migration cost by however long the
supersede/retract history has grown, for zero retrieval benefit -- so this
runner skips them by design.

The atomic swap into production (stop the engine, replace the live file
with the new one, restart) is a deliberate, separately documented OPERATOR
step -- this module and its wrapper only ever produce the new file; neither
ever touches a live/running store.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from argparse import ArgumentParser

from ..embedders import build as build_embedder
from .store import ContradictionStore

__all__ = [
    "ReembedCountMismatch",
    "plan_reembed",
    "reembed_silo",
    "main",
]


class ReembedCountMismatch(RuntimeError):
    """Raised when the active-claim count read from the source doesn't equal
    the number of vectors actually written to the destination -- the
    verify step the brief calls out explicitly. Always raised INSIDE the
    still-open destination transaction, so the caller's `except` rolls it
    back rather than leaving a half-written vec_claims table committed."""


def _open_source_readonly(path: str) -> sqlite3.Connection:
    """Open `path` non-destructively. `PRAGMA query_only = 1` makes any write
    attempt on this connection raise `sqlite3.OperationalError`, mirroring
    `migrate_silo.py`'s `_open_source` -- the same non-destructive-source
    guarantee this house already relies on for a cross-file DB copy."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _source_vec_dim(conn: sqlite3.Connection) -> int | None:
    """Read the embedding dimension baked into the SOURCE `vec_claims`
    table's own `CREATE TABLE` sql (same technique as `migrate_silo.py`'s
    `_vec_table_dim`) -- text-only, so it works even without the sqlite-vec
    extension loaded and even on an empty table. Returns None if the source
    was never built with retrieval enabled (no `vec_claims` table at all)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_claims'"
    ).fetchone()
    sql = row["sql"] if row else None
    if not sql:
        return None
    m = re.search(r"float\[(\d+)\]", sql)
    return int(m.group(1)) if m else None


def _claims_columns(conn: sqlite3.Connection) -> list[str]:
    """Column names straight off the SOURCE `claims` table (via
    `PRAGMA table_info`, not a hardcoded tuple) so a source built by an
    older/newer schema still copies every column it actually has."""
    return [r["name"] for r in conn.execute("PRAGMA table_info(claims)").fetchall()]


def _copy_full_table(src: sqlite3.Connection, dest: sqlite3.Connection,
                      table: str) -> int:
    """Copy EVERY row of `table` from `src` into `dest` verbatim (every
    column, via `PRAGMA table_info` -- not a hardcoded tuple), preserving
    explicit `id` values. Used for `writes`/`conflict_events`: audit tables
    with no FK ordering constraint of their own (unlike `claims`'
    self-referencing `supersedes`), so a plain `ORDER BY id` (their own
    monotonic PK) is enough to keep them in original chronological order --
    no functional requirement, just tidiness. Returns the row count copied.
    """
    if not _table_exists(src, table):
        return 0
    columns = [r["name"] for r in
               src.execute(f"PRAGMA table_info({table})").fetchall()]
    cols_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    rows = src.execute(
        f"SELECT {cols_sql} FROM {table} ORDER BY id", ()).fetchall()
    for row in rows:
        values = tuple(row[c] for c in columns)
        dest.execute(
            f"INSERT INTO {table}({cols_sql}) VALUES({placeholders})",
            values)
    return len(rows)


def _embed_text(statement: str, reason: str | None) -> str:
    """The EXACT concatenation `store.py`'s `_embed_claim` uses: `statement`
    alone, or `f"{statement}\\n{reason}"` when a reason is present.
    Recomputing with anything else would silently desync a migrated claim's
    vector from what a live write would have embedded for it."""
    return statement if not reason else f"{statement}\n{reason}"


def plan_reembed(source_path: str, target_embedder) -> dict:
    """Read-only preview: touches neither `source_path` nor any destination.
    Used by `--dry-run` -- reports what a real run WOULD do (claim counts,
    source/target dims) without writing anything."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"source DB not found: {source_path}")
    conn = _open_source_readonly(source_path)
    try:
        claims_total = claims_active = 0
        if _table_exists(conn, "claims"):
            claims_total = conn.execute(
                "SELECT count(*) AS n FROM claims").fetchone()["n"]
            claims_active = conn.execute(
                "SELECT count(*) AS n FROM claims WHERE status='active'"
            ).fetchone()["n"]
        source_dim = _source_vec_dim(conn)
    finally:
        conn.close()
    return {
        "source_path": source_path,
        "claims_total": claims_total,
        "claims_active": claims_active,
        "source_dim": source_dim,
        "target_dim": target_embedder.dim,
    }


def reembed_silo(source_path: str, dest_path: str, target_embedder,
                  *, overwrite: bool = False) -> dict:
    """The real migration -- see the module docstring for the active-only
    vector choice. Returns a report dict:

        {source_path, dest_path, claims_copied, active_claims,
         vectors_written, writes_copied, conflict_events_copied, target_dim}

    Never mutates `source_path` (opened read-only). Never writes in place:
    `dest_path` must not already exist unless `overwrite=True` is passed
    explicitly (e.g. a deliberate retry) -- a re-embed always targets a NEW
    file. Raises `ReembedCountMismatch` (leaving the destination transaction
    rolled back) if the active claim count read from the source doesn't
    match the number of vectors actually written.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"source DB not found: {source_path}")
    if os.path.exists(dest_path):
        if not overwrite:
            raise FileExistsError(
                f"destination already exists: {dest_path} -- a re-embed "
                "always targets a brand-new file, never in-place (pass "
                "overwrite=True to replace it deliberately)")
        os.remove(dest_path)
        for suffix in ("-wal", "-shm"):
            stale = dest_path + suffix
            if os.path.exists(stale):
                os.remove(stale)

    src = _open_source_readonly(source_path)
    try:
        if not _table_exists(src, "claims"):
            raise ValueError(f"source DB has no claims table: {source_path}")

        columns = _claims_columns(src)
        cols_sql = ", ".join(columns)
        # ORDER BY rowid (the implicit insertion-order column every normal
        # SQLite table has -- `claims`' PK is a TEXT `id`, not an INTEGER
        # alias, so rowid is a distinct, always-monotonic column here) so a
        # `supersedes` row always lands in the destination AFTER the row it
        # references, keeping the self-referencing FK happy across the whole
        # file in one pass -- no per-(org_id, project) partitioning needed,
        # unlike migrate_silo.py's per-org `claim_seq` ordering.
        # Explicit empty params tuple even though this query has no `?`
        # placeholders (column NAMES are code-controlled via
        # `_claims_columns`'s `PRAGMA table_info`, never row data) -- the
        # house semgrep rule (.semgrep.yaml: ergo-sql-string-built-query)
        # flags any bare `$DB.execute(f"...")` with no params argument at
        # all, so this documents the same "fragments are fixed, values go
        # through params" intent migrate_silo.py's WHERE-clause queries
        # already follow, even when there happen to be zero values here.
        claim_rows = src.execute(
            f"SELECT {cols_sql} FROM claims ORDER BY rowid", ()).fetchall()
        project_rows = []
        if _table_exists(src, "projects"):
            project_rows = src.execute(
                "SELECT org_id, project, created_at FROM projects").fetchall()

        dest_store = ContradictionStore(dest_path, embedder=target_embedder)
        try:
            dest = dest_store.db
            dest.execute("BEGIN")
            try:
                placeholders = ", ".join("?" for _ in columns)
                for row in claim_rows:
                    values = tuple(row[c] for c in columns)
                    dest.execute(
                        f"INSERT INTO claims({cols_sql}) VALUES({placeholders})",
                        values)

                for row in project_rows:
                    dest.execute(
                        "INSERT OR IGNORE INTO projects(org_id, project, "
                        "created_at) VALUES(?,?,?)",
                        (row["org_id"], row["project"], row["created_at"]))

                writes_copied = _copy_full_table(src, dest, "writes")
                conflict_events_copied = _copy_full_table(
                    src, dest, "conflict_events")

                active_rows = [r for r in claim_rows if r["status"] == "active"]
                vectors_written = 0
                for row in active_rows:
                    text = _embed_text(row["statement"], row["reason"])
                    vec = target_embedder.embed(text)
                    if len(vec) != target_embedder.dim:
                        raise ValueError(
                            f"target embedder returned a {len(vec)}-dim "
                            f"vector for claim {row['id']} but declares "
                            f"dim={target_embedder.dim} -- refusing to "
                            "write a mismatched vec_claims row")
                    dest.execute(
                        "INSERT INTO vec_claims(claim_id, embedding) "
                        "VALUES(?,?)",
                        (row["id"], dest_store._serialize(vec)))
                    vectors_written += 1

                if vectors_written != len(active_rows):
                    # Defensive -- the loop above can only under-count if it
                    # raised (which already aborts before this line), so this
                    # is the explicit belt-and-suspenders verify the brief
                    # calls out by name, not dead code.
                    raise ReembedCountMismatch(
                        f"active claim count in ({len(active_rows)}) != "
                        f"vectors out ({vectors_written})")

                dest.execute("COMMIT")
            except Exception:
                dest.execute("ROLLBACK")
                raise
        finally:
            dest_store.db.close()
    finally:
        src.close()

    return {
        "source_path": source_path,
        "dest_path": dest_path,
        "claims_copied": len(claim_rows),
        "active_claims": len(active_rows),
        "vectors_written": vectors_written,
        "writes_copied": writes_copied,
        "conflict_events_copied": conflict_events_copied,
        "target_dim": target_embedder.dim,
    }


def main(argv=None) -> int:
    p = ArgumentParser(
        prog="python -m ergo.contradiction.reembed",
        description="Offline re-embed migration: rebuild a tenant silo's "
                     "vec_claims table under a NEW target embedder into a "
                     "brand-new destination file. Never touches --source, "
                     "never writes in-place.")
    p.add_argument("--source", required=True,
                    help="path to the source .db (tenant silo or the "
                         "legacy pooled ergo.db)")
    p.add_argument("--dest", required=True,
                    help="path to the NEW destination .db (must not "
                         "already exist unless --overwrite is given)")
    p.add_argument("--provider", required=True,
                    help="target embedder provider: fastembed | ollama | "
                         "openai | cohere | voyage")
    p.add_argument("--model", default=None,
                    help="override the target provider's default model")
    p.add_argument("--dim", type=int, default=None,
                    help="override the target provider's default dimension")
    p.add_argument("--overwrite", action="store_true",
                    help="replace an existing --dest file (danger -- use "
                         "deliberately, e.g. a retry after a failed run)")
    p.add_argument("--dry-run", action="store_true",
                    help="report the plan (counts, dims) only; write nothing")
    p.add_argument("--report-json", default=None,
                    help="also write the JSON report to this path")
    args = p.parse_args(argv)

    target = build_embedder(args.provider, model=args.model, dim=args.dim)

    if args.dry_run:
        report = plan_reembed(args.source, target)
        report["dry_run"] = True
    else:
        report = reembed_silo(args.source, args.dest, target,
                               overwrite=args.overwrite)
        report["dry_run"] = False

    print(json.dumps(report, indent=2))
    if args.report_json:
        with open(args.report_json, "w") as f:
            json.dump(report, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
