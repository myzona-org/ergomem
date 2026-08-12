"""Deterministic coverage for the single-writer concurrency machinery.

`test_store.py` already exercises the SQLITE_BUSY retry and the preflight_seq
race re-check, but it does so with real threads + timing (blocker holds the
writer slot for a fixed sleep). Those branches therefore fire only when the OS
scheduler interleaves the two writers the right way, so they are covered
flakily — the exact "another writer advanced the seq under our lock" and
"BUSY, back off and retry" arms can go unexecuted on any given run.

These tests drive the SAME branches deterministically by injecting the race
directly (monkeypatch `_max_seq` to report an advanced sequence on the in-lock
re-read, flip a claim's status under the lock, or make `db.execute` raise
SQLITE_BUSY a fixed number of times). No threads, no sleeps-for-timing — every
run walks the load-bearing retry paths that are a deliberate design choice
("SQLITE_BUSY is a first-class retry, not an error to fix with threads").
"""
import sqlite3

import pytest

from ergo.contradiction.store import (ContradictionStore, Stored, Conflict,
                                      Retracted, _Retry)

ENABLE = "TLS 1.2 must be enabled in prod."
DISABLE = "TLS 1.2 must not be enabled in prod."          # HIGH-conflicts with ENABLE
UNRELATED = "MFA on login must be required."               # no conflict with ENABLE


def fresh():
    return ContradictionStore(":memory:")


def _advance_seq_on_second_call(monkeypatch, store):
    """Force ONE preflight_seq mismatch.

    Every guarded-write attempt calls `_max_seq` twice: once at preflight (no
    lock) and once inside BEGIN IMMEDIATE. Making the 2nd call report a higher
    sequence simulates another writer committing in the race window, so the
    first attempt hits its in-lock re-check -> _Retry; the retry (calls 3 & 4)
    reads the real seq and commits cleanly. Returns the call counter so a test
    can assert the retry actually happened (>= 3 calls).
    """
    real = store._max_seq
    calls = {"n": 0}

    def fake(org_id, project):
        calls["n"] += 1
        v = real(org_id, project)
        return v + 100 if calls["n"] == 2 else v

    monkeypatch.setattr(store, "_max_seq", fake)
    return calls


def _deactivate_before_lock(monkeypatch, store, claim_id):
    """Simulate a racing writer that deactivates `claim_id` between our preflight
    and our BEGIN IMMEDIATE. The flip runs as a side effect of the preflight
    `_max_seq` read (which happens after the preflight SELECT, before the lock);
    the store connection is autocommit (isolation_level=None) so it commits
    immediately and survives the attempt's rollback. Retract/supersede consume
    no claim_seq on the racing side, so the seq re-check passes and the *status*
    re-check is what must catch it."""
    real = store._max_seq
    calls = {"n": 0}

    def fake(org_id, project):
        calls["n"] += 1
        if calls["n"] == 1:
            store.db.execute("UPDATE claims SET status='superseded' WHERE id=?",
                             (claim_id,))
        return real(org_id, project)

    monkeypatch.setattr(store, "_max_seq", fake)
    return calls


class _BusyConn:
    """Wraps the store's sqlite connection to raise SQLITE_BUSY the first `fails`
    times it executes a statement starting with `sql_prefix`. Everything else
    delegates to the real connection. (sqlite3.Connection.execute is read-only,
    so we swap the whole `store.db` rather than patch the method.)"""

    def __init__(self, conn, sql_prefix, fails):
        self._conn = conn
        self._prefix = sql_prefix
        self.left = fails

    def execute(self, sql, *args, **kwargs):
        if sql.startswith(self._prefix) and self.left > 0:
            self.left -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _install_busy(monkeypatch, store, sql_prefix, fails):
    proxy = _BusyConn(store.db, sql_prefix, fails)
    monkeypatch.setattr(store, "db", proxy)
    return proxy


# --- preflight_seq race re-check: retry then succeed ----------------------
def test_remember_commit_path_retries_when_seq_advances(monkeypatch):
    s = fresh()
    s.remember("us", "infra", "alice", ENABLE)            # claim_seq 1
    calls = _advance_seq_on_second_call(monkeypatch, s)
    r = s.remember("us", "infra", "bob", UNRELATED)       # non-conflicting -> COMMIT path
    assert isinstance(r, Stored)
    assert calls["n"] >= 3, "in-lock seq mismatch must force a retry"
    seqs = sorted(c["claim_seq"] for c in s.history("us", "infra"))
    assert seqs == [1, 2], "retry must commit on a fresh, monotonic seq"


def test_remember_blocking_path_retries_when_seq_advances(monkeypatch):
    s = fresh()
    s.remember("us", "infra", "alice", ENABLE)
    calls = _advance_seq_on_second_call(monkeypatch, s)
    r = s.remember("us", "infra", "bob", DISABLE)         # conflicts -> blocking branch
    assert isinstance(r, Conflict)
    assert calls["n"] >= 3
    flagged = s.db.execute(
        "SELECT COUNT(*) c FROM conflict_events WHERE resolution='flagged'").fetchone()["c"]
    assert flagged == 1, "conflict audited exactly once (on the successful retry)"


def test_remember_fact_retries_when_seq_advances(monkeypatch):
    s = fresh()
    s.remember_fact("us", "infra", "doc", "Backups run nightly.")
    calls = _advance_seq_on_second_call(monkeypatch, s)
    r = s.remember_fact("us", "infra", "doc", "Backups are copied off-host.")
    assert isinstance(r, Stored)
    assert calls["n"] >= 3


def test_supersede_retries_when_seq_advances(monkeypatch):
    s = fresh()
    a = s.remember("us", "infra", "alice", ENABLE)
    calls = _advance_seq_on_second_call(monkeypatch, s)
    r = s.supersede("us", "infra", "bob", a.claim_id, DISABLE)
    assert isinstance(r, Stored)
    assert calls["n"] >= 3
    active = s.active("us", "infra").claims
    assert len(active) == 1 and active[0]["statement"] == DISABLE


def test_retract_retries_when_seq_advances(monkeypatch):
    s = fresh()
    a = s.remember("us", "infra", "alice", ENABLE)
    calls = _advance_seq_on_second_call(monkeypatch, s)
    r = s.retract("us", "infra", "alice", a.claim_id, "entered by mistake")
    assert isinstance(r, Retracted)
    assert calls["n"] >= 3
    assert s.active("us", "infra").state == "empty"


# --- status race re-check: the claim is deactivated under our lock --------
def test_remember_blocking_conflict_vanishes_under_lock(monkeypatch):
    """The conflicting claim is gone by the time we hold the lock -> re-run from
    scratch and store cleanly (no phantom block)."""
    s = fresh()
    s.remember("us", "infra", "alice", ENABLE)
    real_active = s._active_rows
    calls = {"n": 0}

    def fake_active(org_id, project, exclude_id=None):
        calls["n"] += 1
        # attempt 1 preflight sees alice (blocking); every later read sees her
        # gone, as if a racing writer retracted her in the window.
        if calls["n"] == 1:
            return real_active(org_id, project, exclude_id)
        return []

    monkeypatch.setattr(s, "_active_rows", fake_active)
    r = s.remember("us", "infra", "bob", DISABLE)
    assert isinstance(r, Stored), "vanished conflict must not block the retried write"
    assert calls["n"] >= 3


def test_supersede_retries_when_existing_deactivated_under_lock(monkeypatch):
    s = fresh()
    a = s.remember("us", "infra", "alice", ENABLE)
    calls = _deactivate_before_lock(monkeypatch, s, a.claim_id)
    # attempt 1: seq matches, but the in-lock status re-check finds `a` gone
    # -> _Retry; attempt 2: preflight SELECT finds no active `a` -> KeyError.
    with pytest.raises(KeyError):
        s.supersede("us", "infra", "bob", a.claim_id, DISABLE)
    assert calls["n"] >= 2, "the status re-check must have forced a retry"


def test_retract_retries_when_claim_deactivated_under_lock(monkeypatch):
    s = fresh()
    a = s.remember("us", "infra", "alice", ENABLE)
    calls = _deactivate_before_lock(monkeypatch, s, a.claim_id)
    with pytest.raises(KeyError):
        s.retract("us", "infra", "alice", a.claim_id, "entered by mistake")
    assert calls["n"] >= 2


# --- SQLITE_BUSY backoff: retry within budget, then exhaust ---------------
def test_begin_immediate_retries_busy_then_succeeds(monkeypatch):
    s = fresh()
    proxy = _install_busy(monkeypatch, s, "BEGIN IMMEDIATE", fails=2)
    r = s.remember("us", "infra", "alice", ENABLE)
    assert isinstance(r, Stored)
    assert proxy.left == 0, "both BUSY errors must have been retried past"


def test_begin_immediate_busy_exhaustion_raises(monkeypatch):
    s = ContradictionStore(":memory:", max_retries=3)
    _install_busy(monkeypatch, s, "BEGIN IMMEDIATE", fails=99)
    with pytest.raises(sqlite3.OperationalError, match="remained BUSY"):
        s.remember("us", "infra", "alice", ENABLE)


def test_exec_retries_busy_then_succeeds(monkeypatch):
    s = fresh()
    proxy = _install_busy(monkeypatch, s, "INSERT INTO claims", fails=2)
    r = s.remember("us", "infra", "alice", ENABLE)
    assert isinstance(r, Stored)
    assert proxy.left == 0


def test_exec_busy_exhaustion_raises(monkeypatch):
    s = ContradictionStore(":memory:", max_retries=3)
    _install_busy(monkeypatch, s, "INSERT INTO claims", fails=99)
    # persistent BUSY on the insert -> _exec raises -> commit try/except rolls back
    with pytest.raises(sqlite3.OperationalError, match="persistent BUSY"):
        s.remember("us", "infra", "alice", ENABLE)


# --- _Retry exhaustion for every guarded verb -----------------------------
def _always_retry(*args, **kwargs):
    raise _Retry()


def test_remember_fact_retry_exhaustion(monkeypatch):
    s = fresh()
    monkeypatch.setattr(s, "_remember_fact_attempt", _always_retry)
    with pytest.raises(RuntimeError, match="exceeded retries"):
        s.remember_fact("us", "infra", "doc", "Backups run nightly.")


def test_supersede_retry_exhaustion(monkeypatch):
    s = fresh()
    monkeypatch.setattr(s, "_supersede_attempt", _always_retry)
    with pytest.raises(RuntimeError, match="exceeded retries"):
        s.supersede("us", "infra", "bob", "no-such-id", ENABLE)


def test_retract_retry_exhaustion(monkeypatch):
    s = fresh()
    monkeypatch.setattr(s, "_retract_attempt", _always_retry)
    with pytest.raises(RuntimeError, match="exceeded retries"):
        s.retract("us", "infra", "alice", "no-such-id", "entered by mistake")
