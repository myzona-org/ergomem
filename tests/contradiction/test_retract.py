"""Retraction tests — the store half of POST /retract (wrong-at-birth removal).

Semantics under test: retract = "this claim was WRONG AT BIRTH, remove it from
the active belief set"; supersede = "this decision CHANGED, replace it and keep
the why-chain". Retract runs no contradiction gate, inserts no new row, consumes
no claim_seq — it flips status and persists retract_reason/retracted_at, plus a
`writes` audit row. Also covers the idempotent column migration at store init.
"""
import hashlib
import sqlite3

import pytest

from ergo.contradiction.store import (ContradictionStore, Stored, Conflict,
                                      Retracted)


def fresh():
    return ContradictionStore(":memory:")


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


# --- happy path -----------------------------------------------------------
def test_retract_flips_status_and_persists_reason_and_timestamp():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.retract("us", "infra", "alice", a.claim_id, "typo — meant TLS 1.3")
    assert isinstance(r, Retracted)
    assert r.claim_id == a.claim_id
    row = s.db.execute("SELECT * FROM claims WHERE id=?", (a.claim_id,)).fetchone()
    assert row["status"] == "retracted"
    assert row["retract_reason"] == "typo — meant TLS 1.3"
    assert row["retracted_at"] is not None


def test_retract_writes_audit_row_and_no_conflict_event():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.retract("us", "infra", "bob", a.claim_id, "test junk")
    w = s.db.execute("SELECT * FROM writes WHERE action='retract'").fetchone()
    assert w is not None
    assert w["claim_id"] == a.claim_id
    assert w["who"] == "bob"
    assert w["preflight_seq"] == 1
    # nothing was judged -> no conflict_events row
    n = s.db.execute("SELECT count(*) c FROM conflict_events").fetchone()["c"]
    assert n == 0


def test_retract_inserts_no_row_and_consumes_no_claim_seq():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.remember("us", "infra", "alice", "MFA on login must be required.")
    s.retract("us", "infra", "alice", a.claim_id, "wrong at birth")
    # still exactly two rows; the next write takes seq 3, not 4
    s.remember("us", "infra", "alice", "Backups must run nightly.")
    seqs = [r["claim_seq"] for r in s.history("us", "infra")]
    assert seqs == [1, 2, 3]


# --- validation + scoping -------------------------------------------------
def test_retract_requires_non_empty_reason():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    for bad in (None, "", "   "):
        with pytest.raises(ValueError):
            s.retract("us", "infra", "alice", a.claim_id, bad)
    # claim untouched by the failed attempts
    assert len(s.active("us", "infra").claims) == 1


def test_retract_missing_claim_raises_keyerror():
    s = fresh()
    with pytest.raises(KeyError, match="no active claim"):
        s.retract("us", "infra", "alice", "no-such-id", "reason")


def test_retract_already_retracted_claim_raises_keyerror():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.retract("us", "infra", "alice", a.claim_id, "wrong at birth")
    with pytest.raises(KeyError, match="no active claim"):
        s.retract("us", "infra", "alice", a.claim_id, "again")


def test_retract_is_project_scoped():
    s = fresh()
    a = s.remember("us", "projectA", "alice", "TLS 1.2 must be enabled in prod.")
    with pytest.raises(KeyError):
        s.retract("us", "projectB", "bob", a.claim_id, "mis-scoped attack")
    assert len(s.active("us", "projectA").claims) == 1


# --- exclusion from all active read paths ----------------------------------
def test_retracted_claim_vanishes_from_active_and_recall_but_not_history():
    s = ContradictionStore(":memory:", embedder=FakeEmbedder())
    a = s.remember("o", "p", "ed", "Backups are stored on the Synology NAS.",
                   reason="that's where the disk space is")
    assert s.recall("o", "p", "where are backups stored")  # visible before
    s.retract("o", "p", "ed", a.claim_id, "wrong box — they're on the R730")
    aset = s.active("o", "p")
    assert aset.state == "empty" and aset.claims == []
    assert s.recall("o", "p", "where are backups stored") == []
    hist = s.history("o", "p")
    assert len(hist) == 1 and hist[0]["status"] == "retracted"


def test_retracted_claim_is_no_longer_judged_against():
    """A formerly-blocking claim stops blocking once retracted: the same
    opposing statement that 409'd before now stores clean."""
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    blocked = s.remember("us", "infra", "bob", "TLS 1.2 must not be enabled in prod.")
    assert isinstance(blocked, Conflict)
    s.retract("us", "infra", "alice", a.claim_id, "test junk from a demo")
    r = s.remember("us", "infra", "bob", "TLS 1.2 must not be enabled in prod.")
    assert isinstance(r, Stored)


def test_diagnostics_counts_retracted():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.retract("us", "infra", "alice", a.claim_id, "wrong at birth")
    diag = s.diagnostics()
    assert diag["counts"]["claims_retracted"] == 1
    assert diag["counts"]["claims_active"] == 0


# --- migration idempotency --------------------------------------------------
def _claim_columns(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(claims)").fetchall()}
    finally:
        con.close()


def test_migration_is_idempotent_across_reopens(tmp_path):
    db = str(tmp_path / "ergo.db")
    s1 = ContradictionStore(db)
    a = s1.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s1.db.close()
    # second open of the same file must not fail on the already-added columns
    s2 = ContradictionStore(db)
    r = s2.retract("us", "infra", "alice", a.claim_id, "wrong at birth")
    assert isinstance(r, Retracted)
    assert {"retract_reason", "retracted_at"} <= _claim_columns(db)


# claims DDL as it existed before the retract columns were added — a frozen
# snapshot for the upgrade test, NOT kept in sync with store.SCHEMA on purpose.
PRE_RETRACT_CLAIMS_DDL = """
CREATE TABLE claims (
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
  PRIMARY KEY (id),
  UNIQUE (org_id, project, id),
  UNIQUE (org_id, project, claim_seq),
  FOREIGN KEY (org_id, project, supersedes)
    REFERENCES claims(org_id, project, id),
  CHECK (length(trim(statement)) > 0),
  CHECK (status IN ('active', 'superseded', 'retracted')),
  CHECK (claim_seq > 0)
);
"""


def test_migration_upgrades_pre_existing_db_without_the_columns(tmp_path):
    db = str(tmp_path / "legacy.db")
    con = sqlite3.connect(db)
    con.executescript(PRE_RETRACT_CLAIMS_DDL)
    con.close()
    assert "retract_reason" not in _claim_columns(db)
    # opening the store migrates the legacy table in place...
    s = ContradictionStore(db)
    assert {"retract_reason", "retracted_at"} <= _claim_columns(db)
    # ...and the full remember -> retract cycle works on it
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.retract("us", "infra", "alice", a.claim_id, "wrong at birth")
    assert isinstance(r, Retracted)
