"""Offline re-embed migration runner tests (ergo/contradiction/reembed.py,
private-repo issue #126): embedding dim is fixed at vec-table creation, so switching
provider/model/dim needs a real migration path, not an in-place `ALTER`.

Uses deterministic fake embedders (no model download / network needed) at
TWO different dimensions to prove this is a genuine RE-embed (target vectors
computed fresh by the target embedder), not a blob copy across files like
migrate_silo.py's vector-copy path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

import pytest

from ergo.contradiction.reembed import (
    ReembedCountMismatch,
    plan_reembed,
    reembed_silo,
)
from ergo.contradiction.store import ContradictionStore


class HashEmbedder:
    """Hashed bag-of-words -> unit vector. Deterministic, offline. `salt`
    lets the source and target embedders diverge even on the same text and
    dimension, so a passing test can't be accidentally satisfied by a
    byte-for-byte copy."""

    def __init__(self, dim: int, salt: str = ""):
        self.dim = dim
        self.salt = salt

    def embed(self, text):
        v = [0.0] * self.dim
        for w in (self.salt + text).lower().split():
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]


class WrongLenEmbedder(HashEmbedder):
    """Declares one dim but actually returns vectors of another -- proves
    the runner catches a misbehaving embedder instead of silently writing a
    corrupt vec_claims row."""

    def embed(self, text):
        return super().embed(text)[:-1]  # one short of self.dim


def _seed_source(path: str, embedder) -> dict:
    """Seed a source DB with: an active claim WITH a reason, an active claim
    WITHOUT a reason, a 2-row supersede chain, and a retracted claim --
    covering every status the runner must handle."""
    s = ContradictionStore(path, embedder=embedder)
    with_reason = s.remember("o", "p", "alice",
                             "TLS 1.2 must be enabled in prod.",
                             reason="compliance requirement")
    no_reason = s.remember_fact("o", "p", "alice",
                                "Backups run nightly on the NAS.")
    old = s.remember("o", "p", "bob", "Deploy on Fridays.",
                     reason="low weekday traffic")
    superseded = s.supersede("o", "p", "bob", old.claim_id,
                             "Never deploy on Fridays.",
                             reason="incident risk outweighs low traffic")
    dead = s.remember("o", "p", "carol", "Typo claim to retract.")
    s.retract("o", "p", "carol", dead.claim_id, "wrong at birth")
    s.db.close()
    return {
        "with_reason_id": with_reason.claim_id,
        "no_reason_id": no_reason.claim_id,
        "old_id": old.claim_id,
        "superseded_id": superseded.claim_id,
        "dead_id": dead.claim_id,
    }


def _raw_counts(path: str) -> tuple[int, int, int]:
    """(claims_total, claims_active, vec_claims_total) via a plain
    connection -- no store/embedder needed, just row counts. `vec_claims` is
    a sqlite-vec virtual table (vec0 module), so even a read-only COUNT(*)
    against it needs the extension loaded first."""
    import sqlite_vec
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        total = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
        active = conn.execute(
            "SELECT count(*) FROM claims WHERE status='active'").fetchone()[0]
        vecs = conn.execute("SELECT count(*) FROM vec_claims").fetchone()[0]
        return total, active, vecs
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 1. Core: one vector PER ACTIVE claim, full claims history preserved.
# --------------------------------------------------------------------------
def test_reembed_writes_one_vector_per_active_claim_and_preserves_full_history(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    ids = _seed_source(source, HashEmbedder(dim=64, salt="src"))

    report = reembed_silo(source, dest, HashEmbedder(dim=32, salt="tgt"))

    assert report["claims_copied"] == 5   # with_reason, no_reason, old, superseded, dead
    assert report["active_claims"] == 3   # with_reason, no_reason, superseded (the new head)
    assert report["vectors_written"] == 3
    assert report["target_dim"] == 32

    total, active, vecs = _raw_counts(dest)
    assert total == 5     # every status copied — superseded/retracted rows still there
    assert active == 3
    assert vecs == 3      # NOT 5 — active-only re-embed choice (see module docstring)

    import sqlite_vec
    dest_conn = sqlite3.connect(dest)
    dest_conn.enable_load_extension(True)
    sqlite_vec.load(dest_conn)
    dest_conn.enable_load_extension(False)
    try:
        statuses = {r[0] for r in
                    dest_conn.execute("SELECT status FROM claims").fetchall()}
        assert statuses == {"active", "superseded", "retracted"}
        vec_ids = {r[0] for r in
                   dest_conn.execute("SELECT claim_id FROM vec_claims").fetchall()}
        assert vec_ids == {ids["with_reason_id"], ids["no_reason_id"], ids["superseded_id"]}
        assert ids["old_id"] not in vec_ids          # superseded — no vector
        assert ids["dead_id"] not in vec_ids         # retracted — no vector
    finally:
        dest_conn.close()


def test_reembed_copies_writes_and_conflict_events_audit_tables(tmp_path):
    """Review finding (private-repo issue #126): the migration must not silently
    drop the `writes`/`conflict_events` audit trail -- `audit_log()` /
    `GET /audit` (private-repo issue #124) need their full history post-migration, not
    just the claims themselves."""
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    src_conn = sqlite3.connect(source)
    src_writes = src_conn.execute("SELECT count(*) FROM writes").fetchone()[0]
    src_conflicts = src_conn.execute(
        "SELECT count(*) FROM conflict_events").fetchone()[0]
    src_conn.close()
    assert src_writes > 0   # every remember/supersede/retract logs a write

    report = reembed_silo(source, dest, HashEmbedder(dim=32, salt="tgt"))

    assert report["writes_copied"] == src_writes
    assert report["conflict_events_copied"] == src_conflicts

    dest_conn = sqlite3.connect(dest)
    try:
        dest_writes = dest_conn.execute("SELECT count(*) FROM writes").fetchone()[0]
        dest_conflicts = dest_conn.execute(
            "SELECT count(*) FROM conflict_events").fetchone()[0]
    finally:
        dest_conn.close()
    assert dest_writes == src_writes
    assert dest_conflicts == src_conflicts


# --------------------------------------------------------------------------
# 2. Target dim actually baked into the NEW vec_claims CREATE sql.
# --------------------------------------------------------------------------
def test_reembed_bakes_target_dim_into_new_vec_table(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    reembed_silo(source, dest, HashEmbedder(dim=48, salt="tgt"))

    conn = sqlite3.connect(dest)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_claims'"
        ).fetchone()[0]
    finally:
        conn.close()
    m = re.search(r"float\[(\d+)\]", sql)
    assert m and int(m.group(1)) == 48


# --------------------------------------------------------------------------
# 3. Text recomputed EXACTLY like store._embed_claim: statement alone, or
#    statement + "\n" + reason when a reason is present.
# --------------------------------------------------------------------------
def test_reembed_recomputes_text_as_statement_plus_reason(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    target = HashEmbedder(dim=48, salt="tgt")
    ids = _seed_source(source, HashEmbedder(dim=64, salt="src"))

    reembed_silo(source, dest, target)

    dest_store = ContradictionStore(dest, embedder=target)
    row = dest_store.db.execute(
        "SELECT embedding FROM vec_claims WHERE claim_id=?",
        (ids["with_reason_id"],)).fetchone()
    expected = dest_store._serialize(
        target.embed("TLS 1.2 must be enabled in prod.\ncompliance requirement"))
    assert row[0] == expected

    row_no_reason = dest_store.db.execute(
        "SELECT embedding FROM vec_claims WHERE claim_id=?",
        (ids["no_reason_id"],)).fetchone()
    expected_no_reason = dest_store._serialize(
        target.embed("Backups run nightly on the NAS."))
    assert row_no_reason[0] == expected_no_reason
    dest_store.db.close()


# --------------------------------------------------------------------------
# 4. Recall actually works post-migration (proves the vectors are USABLE,
#    not just present) — same end-to-end proof style as test_migrate_silo.py.
# --------------------------------------------------------------------------
def test_reembed_recall_works_on_new_dim_post_migration(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    target = HashEmbedder(dim=48, salt="tgt")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    reembed_silo(source, dest, target)

    dest_store = ContradictionStore(dest, embedder=target)
    hits = dest_store.recall("o", "p", "TLS in production", limit=3)
    assert hits and "tls 1.2" in hits[0]["statement"].lower()

    hits_friday = dest_store.recall("o", "p", "deploy on fridays", limit=3)
    texts_friday = " ".join(h["statement"].lower() for h in hits_friday)
    assert "never deploy" in texts_friday
    dest_store.db.close()


# --------------------------------------------------------------------------
# 5. Count-verify catches a mismatch: a target embedder that misbehaves
#    (declares one dim, returns another) must abort, not write a corrupt row.
# --------------------------------------------------------------------------
def test_reembed_rejects_embedder_returning_wrong_length_vector(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    with pytest.raises(ValueError, match="dim"):
        reembed_silo(source, dest, WrongLenEmbedder(dim=32, salt="tgt"))

    # Rolled back: the destination file/schema may exist (created before the
    # transaction), but must hold ZERO committed claims/vectors — never a
    # half-written migration silently left behind.
    total, active, vecs = _raw_counts(dest)
    assert (total, active, vecs) == (0, 0, 0)


def test_reembed_count_mismatch_type_is_reraised_as_documented_exception(tmp_path):
    """ReembedCountMismatch is exported and importable — the documented
    verify failure mode callers can catch specifically (the wrong-length
    embedder above raises the more specific ValueError first; this just
    locks in that the class exists and is a RuntimeError, per the module's
    contract)."""
    assert issubclass(ReembedCountMismatch, RuntimeError)


# --------------------------------------------------------------------------
# 6. Never in-place: the source file's bytes/rows are untouched.
# --------------------------------------------------------------------------
def test_reembed_source_is_untouched(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    before = _raw_counts(source)
    reembed_silo(source, dest, HashEmbedder(dim=32, salt="tgt"))
    after = _raw_counts(source)

    assert before == after == (5, 3, 5)  # source still embeds every status (pre-migration behavior)


def test_reembed_refuses_to_write_over_an_existing_destination(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))
    with open(dest, "w") as f:
        f.write("not a real db, just proving it's not overwritten")

    with pytest.raises(FileExistsError):
        reembed_silo(source, dest, HashEmbedder(dim=32, salt="tgt"))

    with open(dest) as f:
        assert f.read() == "not a real db, just proving it's not overwritten"


def test_reembed_overwrite_flag_allows_a_deliberate_rerun(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    reembed_silo(source, dest, HashEmbedder(dim=32, salt="tgt"))
    # A second run with a DIFFERENT target dim must cleanly replace the file
    # (fresh vec_claims at the new dim), not collide with the old schema.
    report = reembed_silo(source, dest, HashEmbedder(dim=16, salt="tgt2"),
                          overwrite=True)
    assert report["target_dim"] == 16
    conn = sqlite3.connect(dest)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_claims'"
    ).fetchone()[0]
    conn.close()
    assert "float[16]" in sql


def test_reembed_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        reembed_silo(str(tmp_path / "nope.db"), str(tmp_path / "dest.db"),
                    HashEmbedder(dim=32))


# --------------------------------------------------------------------------
# 7. plan_reembed (--dry-run backing function): read-only, touches nothing.
# --------------------------------------------------------------------------
def test_plan_reembed_reports_counts_and_touches_nothing(tmp_path):
    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    plan = plan_reembed(source, HashEmbedder(dim=32, salt="tgt"))

    assert plan["claims_total"] == 5
    assert plan["claims_active"] == 3
    assert plan["source_dim"] == 64
    assert plan["target_dim"] == 32
    assert not os.path.exists(dest)          # nothing written
    assert _raw_counts(source) == (5, 3, 5)  # source untouched


def test_plan_reembed_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        plan_reembed(str(tmp_path / "nope.db"), HashEmbedder(dim=32))


# --------------------------------------------------------------------------
# 8. CLI entrypoint (`python -m ergo.contradiction.reembed`) end-to-end.
# --------------------------------------------------------------------------
def test_cli_main_dry_run_writes_no_files(tmp_path, capsys):
    from ergo.contradiction import reembed as reembed_mod

    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    # fastembed is the default provider and would try to load a real model;
    # monkeypatch build_embedder is unnecessary here because we exercise the
    # CLI's plumbing, not provider selection — use a throwaway module-level
    # stub instead of hitting the network/model cache.
    orig_build = reembed_mod.build_embedder
    reembed_mod.build_embedder = lambda name, model=None, dim=None: HashEmbedder(
        dim=dim or 32, salt=f"tgt-{name}")
    try:
        rc = reembed_mod.main([
            "--source", source, "--dest", dest,
            "--provider", "fastembed", "--dry-run",
        ])
    finally:
        reembed_mod.build_embedder = orig_build

    assert rc == 0
    assert not os.path.exists(dest)
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["claims_active"] == 3


def test_cli_main_real_run_writes_report_json(tmp_path, capsys):
    from ergo.contradiction import reembed as reembed_mod

    source = str(tmp_path / "source.db")
    dest = str(tmp_path / "dest.db")
    report_path = str(tmp_path / "report.json")
    _seed_source(source, HashEmbedder(dim=64, salt="src"))

    orig_build = reembed_mod.build_embedder
    reembed_mod.build_embedder = lambda name, model=None, dim=None: HashEmbedder(
        dim=dim or 32, salt=f"tgt-{name}")
    try:
        rc = reembed_mod.main([
            "--source", source, "--dest", dest,
            "--provider", "fastembed", "--report-json", report_path,
        ])
    finally:
        reembed_mod.build_embedder = orig_build

    assert rc == 0
    assert os.path.exists(dest)
    with open(report_path) as f:
        report = json.load(f)
    assert report["dry_run"] is False
    assert report["active_claims"] == report["vectors_written"] == 3
    assert report["target_dim"] == 32
