"""Store-level concurrency tests (bounded threaded HTTP server, per-store locks).

These drive REAL threads against REAL sqlite files -- no mocks -- to prove the
bug the change fixes and pin the fix: `sqlite3.Connection` objects are NOT
safe for concurrent use by multiple threads even with `check_same_thread=
False`, so `ContradictionStore` needs its own locking on top of that flag.
Before this change, ContradictionStore didn't accept concurrent callers at
all (single-threaded server); these tests exercise the per-instance `RLock`
+ the existing `BEGIN IMMEDIATE` / `SQLITE_BUSY` retry serializing that SAME
connection safely.

The cross-store test below constructs two `ContradictionStore` instances
directly (one per sqlite file) rather than leasing them through
`TenantRegistry` -- the property under test (two separate store instances/
files never cross-contaminate under concurrent writes) is store-level, not
anything about how the two instances got constructed, so it needs no
tenant-leasing machinery to prove. `TenantRegistry`'s OWN race-safety (its
dict-mutation lock, preventing two threads from ever opening two separate
connections to the same tenant's file) has no
single-tenant equivalent -- a single-tenant deployment has exactly one
`_STORE`, built once at boot, never leased concurrently -- and lives in
`tests/contradiction/test_store_concurrency_control.py` instead.
"""
from __future__ import annotations

import threading
import time

from ergo.contradiction.store import ContradictionStore, Stored


class _SlowNormalizer:
    """Models a real LLM-normalizer provider round-trip: `normalize()` signals
    `started` the instant it's entered, then blocks for `delay` seconds before
    returning a fixed norm. Used to prove `self._lock` is no longer held
    across the provider call (review finding, 2026-07-13). A test waits on
    `started` (not a fixed sleep-and-hope) so it deterministically probes
    lock behavior while the provider call is actually in progress."""
    VERSION = "slow-stub"

    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.started = threading.Event()

    def normalize(self, claim):
        self.started.set()
        time.sleep(self.delay)
        return {"subject": "x", "modality": "must", "object": "enabled", "value": None,
                "scope": {"env": None, "service_class": None, "region": None, "version": None},
                "valid_from": None, "valid_until": None, "exception": None}


def test_same_tenant_concurrent_writes_yield_unique_contiguous_claim_seq(tmp_path):
    """N concurrent guarded writes to the SAME store (one tenant) must all
    land with unique, contiguous claim_seq values -- no lost writes, no
    duplicated seq, no corruption of the underlying sqlite connection."""
    store = ContradictionStore(str(tmp_path / "tenant.db"))
    n = 20
    results: list = [None] * n
    errors: list[Exception] = []

    def _write(i):
        try:
            results[i] = store.remember(
                "acme", "p", f"user{i}", f"Claim number {i} must hold.")
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent remember() raised: {errors}"
    assert all(isinstance(r, Stored) for r in results), results

    seqs = sorted(c["claim_seq"] for c in store.history("acme", "p"))
    assert seqs == list(range(1, n + 1)), f"claim_seq not contiguous/unique: {seqs}"
    assert len({r.claim_id for r in results}) == n  # every claim_id distinct


def test_cross_tenant_concurrent_writes_land_only_in_their_own_file(tmp_path):
    """Concurrent writes to DIFFERENT stores (different ContradictionStore
    instances/files) must never cross-contaminate: each store's on-disk file
    holds only its own rows. Two stores = two locks, so neither writer's
    lock blocks the other."""
    store_a = ContradictionStore(str(tmp_path / "org-a.db"))
    store_b = ContradictionStore(str(tmp_path / "org-b.db"))

    errors: list[Exception] = []

    def _write_many(store, org, n):
        try:
            for i in range(n):
                r = store.remember(org, "p", "who", f"{org} claim {i} must hold.")
                assert isinstance(r, Stored), r
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    ta = threading.Thread(target=_write_many, args=(store_a, "org-a", 15))
    tb = threading.Thread(target=_write_many, args=(store_b, "org-b", 15))
    ta.start()
    tb.start()
    ta.join(timeout=20)
    tb.join(timeout=20)

    assert not errors, f"concurrent cross-tenant remember() raised: {errors}"

    hist_a = store_a.history("org-a", "p")
    hist_b = store_b.history("org-b", "p")
    assert len(hist_a) == 15 and all(c["org_id"] == "org-a" for c in hist_a)
    assert len(hist_b) == 15 and all(c["org_id"] == "org-b" for c in hist_b)
    # org-a's data must be completely invisible in org-b's file and vice versa
    assert store_a.active("org-b", "p").state == "project_missing"
    assert store_b.active("org-a", "p").state == "project_missing"


def test_remember_does_not_hold_lock_across_slow_normalize(tmp_path):
    """Review finding (2026-07-13): remember() must normalize (the slow provider
    round-trip) OUTSIDE `self._lock`, holding the lock only for the DB
    read/write phase. Pre-fix, `self._lock` wrapped the WHOLE method, so a
    slow write for one org on the shared LEGACY singleton store blocked every
    OTHER caller of that same store instance -- including one supplying a
    completely unrelated org_id -- for the full provider round-trip: a
    de-facto cross-tenant lock (path instructions: never a global
    cross-tenant lock). A plain DB read (`active()`) for an unrelated org on
    the SAME store instance must return almost immediately, not block for
    ~`delay` seconds behind the slow write's normalize() call."""
    normalizer = _SlowNormalizer(delay=0.3)
    store = ContradictionStore(str(tmp_path / "shared.db"), normalizer=normalizer)

    def slow_write():
        store.remember("orgA", "infra", "alice", "slow claim needs a lock check.")

    t = threading.Thread(target=slow_write)
    t.start()
    # Wait for the provider call to actually be IN PROGRESS (not a fixed
    # sleep-and-hope): normalize() sets this the instant it's entered, before
    # its own internal delay.
    assert normalizer.started.wait(timeout=1), "writer thread never entered normalize()"

    start = time.monotonic()
    store.active("orgB", "infra")  # unrelated org, pure DB read
    elapsed = time.monotonic() - start

    t.join(timeout=2)
    assert not t.is_alive(), "writer thread never finished"
    assert elapsed < 0.2, (
        f"active() for an unrelated org blocked for {elapsed:.2f}s -- "
        "self._lock is still held across the slow normalize() call")
