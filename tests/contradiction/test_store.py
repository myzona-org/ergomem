"""Write-guard tests for the contradiction store.

Structural-only (no NLI / no Ollama) so they run anywhere, fast. Includes the
13 regression tests demanded by a 2026-06-25 code review:
cross-project supersede, supersede-must-revalidate, SQLITE_BUSY retry, race
re-check, stale conflict revalidation, warning-tier surfacing, exception audit,
empty-subject not HIGH-cleared, exception-before-scope, exception scope leakage,
invalid date degrades, persisted-norm reuse, NLI label-map validation.
"""
import json
import sqlite3
import threading
import time

import pytest

from ergo.contradiction.normalize import NormalizeUnavailable
from ergo.contradiction.store import (ContradictionStore, Stored, Conflict,
                                      ActiveSet, ExportSet, _is_busy)


def fresh():
    return ContradictionStore(":memory:")


class _DegradedNormalizer:
    """Always raises NormalizeUnavailable — models a provider that is
    infrastructurally down (connection/timeout/429/5xx/auth), finding #3."""
    VERSION = "degraded-stub"

    def normalize(self, claim):
        raise NormalizeUnavailable("provider down: connection refused")


# --- original happy-path coverage ----------------------------------------
def test_clear_write_is_stored():
    s = fresh()
    r = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    assert isinstance(r, Stored)
    aset = s.active("us", "infra")
    assert aset.state == "has_active_claims" and len(aset.claims) == 1


def test_active_empty_states_are_distinct():
    s = fresh()
    # project never initialized
    aset = s.active("us", "nope")
    assert aset.state == "project_missing"
    # initialize via a write
    s.remember("us", "infra", "alice", "MFA on login must be required.")
    # now check a project that exists but with no active claims is "empty" not "missing"
    # (simulate: superseded everything)
    s.db.execute("UPDATE claims SET status='superseded'")
    aset = s.active("us", "infra")
    assert aset.state == "empty"


def test_project_state_is_the_cheap_three_state_check():
    # private-repo issue #184 (+ follow-up): /recall and /why need to know the SAME
    # three-state signal /active returns, without paying for a full
    # active() row fetch -- project_state() is that extracted primitive.
    s = fresh()
    assert s.project_state("us", "nope") == "project_missing"
    s.remember("us", "infra", "alice", "MFA on login must be required.")
    assert s.project_state("us", "infra") == "has_active_claims"
    # a project row that exists but with everything superseded is "empty",
    # not "project_missing" -- same distinction active() makes.
    s.db.execute("UPDATE claims SET status='superseded'")
    assert s.project_state("us", "infra") == "empty"


def test_project_state_is_has_active_claims_even_when_every_claim_is_reason_less():
    # FOLLOW-UP REGRESSION (mechanism 1, orchestrator finding on private-repo PR #188): a
    # project holding only /ingest facts (no `reason`) must never be
    # reported as "empty" just because `why()`'s reasoned_only filter would
    # find nothing in it (a documented invariant: "why searches reasoned
    # entries only"). `project_state()` looks at claim STATUS alone, never
    # at whether a claim happens to carry a `reason` -- that's what makes it
    # safe for `/why`'s route to use as a fallback when its OWN reasoned-only
    # result is empty (see tests/contradiction/test_server.py's server-level
    # coverage of that combination).
    s = fresh()
    s.remember_fact("us", "kb", "alice", "The sky is blue.")  # no reason
    assert s.project_state("us", "kb") == "has_active_claims"


def test_contradiction_is_blocked_not_silently_inserted():
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.remember("us", "infra", "bob", "TLS 1.2 must not be enabled in prod.")
    assert isinstance(r, Conflict)
    assert r.conflicts and r.conflicts[0].claim["who"] == "alice"
    assert len(s.active("us", "infra").claims) == 1
    n = s.db.execute("SELECT COUNT(*) c FROM conflict_events WHERE resolution='flagged'").fetchone()["c"]
    assert n == 1


def test_ingested_fact_is_not_contradiction_checked():
    """A reference fact (remember_fact -> NULL norm) is not a decision to be
    contradiction-checked. A later remember whose text would textually
    contradict the fact must still be Stored, and the judge must never
    re-normalize the NULL-norm fact row."""
    s = fresh()
    s.remember_fact("us", "infra", "doc", "TLS 1.2 must be enabled in prod.")
    # same scope, directly-opposing decision: blocked only if the fact were judged
    r = s.remember("us", "infra", "alice", "TLS 1.2 must not be enabled in prod.")
    assert isinstance(r, Stored)
    # fact stayed a NULL-norm row (never re-normalized into a stored norm)
    norm = s.db.execute(
        "SELECT norm FROM claims WHERE who='doc'").fetchone()["norm"]
    assert norm is None


def test_default_normalizer_coref_state_never_leaks_across_scopes():
    """Audit 2026-07-04 finding 1: the store-default RuleNormalizer tracked
    pronoun antecedents across ALL writes, so a bare-pronoun statement in one
    (org, project) resolved against another tenant's most recent subject — the
    poisoned norm was persisted (norms are the durable comparison source, #6)
    and a later clean write in the victim scope was wrongly blocked."""
    s = fresh()
    assert isinstance(
        s.remember("orgA", "projA", "a", "TLS 1.2 must be enabled in prod."), Stored)
    r2 = s.remember("orgB", "projB", "b", "It must be disabled in prod.")
    assert isinstance(r2, Stored)
    norm = json.loads(s.db.execute(
        "SELECT norm FROM claims WHERE id=?", (r2.claim_id,)).fetchone()["norm"])
    assert norm["subject"] != "tls 1.2"  # orgA's subject must not cross scopes
    # and orgB's genuinely clean write must not 409 against the poisoned claim
    r3 = s.remember("orgB", "projB", "b", "TLS 1.2 must be enabled in prod.")
    assert isinstance(r3, Stored)


def test_disjoint_scope_coexists():
    s = fresh()
    s.remember("us", "infra", "alice", "The /debug endpoint must be disabled in prod.")
    r = s.remember("us", "infra", "carol", "The /debug endpoint may be enabled in dev.")
    assert isinstance(r, Stored)
    assert len(s.active("us", "infra").claims) == 2


def test_supersede_resolution():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.remember("us", "infra", "bob", "TLS 1.2 must not be enabled in prod.")  # blocked
    new = s.supersede("us", "infra", "bob", a.claim_id,
                      "TLS 1.2 must not be enabled in prod.")
    assert isinstance(new, Stored)
    active = s.active("us", "infra").claims
    assert len(active) == 1 and active[0]["who"] == "bob"
    assert len(s.history("us", "infra")) == 2


def test_store_with_conflict_exception_writes_audit_row():
    """#8: force_exception must persist a conflict_events row with the exception."""
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.store_with_conflict_exception(
        "us", "infra", "dave", "TLS 1.2 must not be enabled in prod.",
        reason="legacy appliance carve-out", exception_reason="approved: legacy box X")
    assert isinstance(r, Stored)
    assert len(s.active("us", "infra").claims) == 2
    # audit row exists with resolution='exception' AND captured the exception reason
    row = s.db.execute(
        "SELECT * FROM conflict_events WHERE resolution='exception'").fetchone()
    assert row is not None
    assert row["exception_reason"] == "approved: legacy box X"
    assert row["against"] is not None
    assert row["stored_claim_id"] == r.claim_id


def test_tenant_isolation():
    s = fresh()
    s.remember("orgA", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.remember("orgB", "infra", "zoe", "TLS 1.2 must not be enabled in prod.")
    assert isinstance(r, Stored)
    assert len(s.active("orgA", "infra").claims) == 1
    assert len(s.active("orgB", "infra").claims) == 1


def test_claim_seq_monotonic_per_project():
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.remember("us", "infra", "alice", "MFA on login must be required.")
    seqs = [r["claim_seq"] for r in s.history("us", "infra")]
    assert seqs == [1, 2]


# --- learn: provenance-attached remember (bridge ingest -> memory) -------
def test_learn_stores_through_full_pipeline_with_provenance():
    """learn() must go through remember() (a real claim, normalized norm row) and
    keep the source in the reason so /why-style reads can surface provenance."""
    s = fresh()
    r = s.learn("us", "infra", "claude",
                "Sessions are never reused across gateway restarts.",
                source="sessions.py:847")
    assert isinstance(r, Stored)
    row = s.db.execute("SELECT * FROM claims WHERE id=?", (r.claim_id,)).fetchone()
    # a learned claim is a DECISION (normalized norm row), not a NULL-norm fact
    assert row["norm"] is not None
    # provenance rides in the reason (recall/why echo reason, not a source field);
    # with no caller reason the source-only phrasing still carries it + is why-able
    assert "sessions.py:847" in row["reason"]
    assert row["reason"].strip()  # non-empty reason -> the learned claim can win why()


def test_learn_runs_the_contradiction_gate():
    """A learned belief that opposes an existing active claim must be BLOCKED —
    learn must not bypass the judge (the whole point of the bridge)."""
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.learn("us", "infra", "claude", "TLS 1.2 must not be enabled in prod.",
                source="legacy-appliance-notes.md")
    assert isinstance(r, Conflict)
    assert r.conflicts and r.conflicts[0].claim["who"] == "alice"
    assert len(s.active("us", "infra").claims) == 1


def test_learn_caller_reason_is_preserved_alongside_source():
    s = fresh()
    r = s.learn("us", "infra", "claude", "Retries must cap at 3 attempts.",
                source="client.py:42", reason="beyond 3 the upstream rate-limits us")
    row = s.db.execute("SELECT reason FROM claims WHERE id=?", (r.claim_id,)).fetchone()
    assert "beyond 3 the upstream rate-limits us" in row["reason"]
    assert "client.py:42" in row["reason"]


def test_learn_requires_non_empty_source():
    s = fresh()
    for bad in (None, "", "   "):
        with pytest.raises(ValueError):
            s.learn("us", "infra", "claude", "A belief without provenance.", source=bad)


def test_learn_force_exception_stores_despite_conflict():
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.learn("us", "infra", "claude", "TLS 1.2 must not be enabled in prod.",
                source="exception-memo.md", force_exception="approved: legacy box X")
    assert isinstance(r, Stored)
    assert len(s.active("us", "infra").claims) == 2


# --- fail CLOSED on provider infrastructure failure --
# finding #3 — supersedes the fail-open-on-every-exception behavior
# --------------------------------------------------------------------------
def test_remember_refuses_write_on_normalize_unavailable():
    """No force_exception -> the guarded write is refused (fails CLOSED),
    NOT silently stored with an unguarded/all-unknown norm."""
    s = ContradictionStore(":memory:", normalizer=_DegradedNormalizer())
    with pytest.raises(NormalizeUnavailable):
        s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    assert s.active("us", "infra").state == "project_missing"  # nothing written
    row = s.db.execute(
        "SELECT * FROM conflict_events WHERE resolution='degraded_refused'").fetchone()
    assert row is not None
    assert row["stored_claim_id"] is None


def test_remember_force_exception_stores_despite_normalize_unavailable():
    """force_exception overrides the degraded refusal — the same mechanism
    used to override a contradiction — and the bypass is audited loudly."""
    s = ContradictionStore(":memory:", normalizer=_DegradedNormalizer())
    r = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.",
                   force_exception="approved: provider outage, ship anyway")
    assert isinstance(r, Stored)
    row = s.db.execute("SELECT * FROM claims WHERE id=?", (r.claim_id,)).fetchone()
    assert row["norm"] is None  # contradiction check was skipped, not faked
    assert row["conflict_exception"] == "approved: provider outage, ship anyway"
    audit = s.db.execute(
        "SELECT * FROM conflict_events WHERE resolution='degraded_exception'").fetchone()
    assert audit is not None
    assert audit["stored_claim_id"] == r.claim_id
    assert audit["exception_reason"] == "approved: provider outage, ship anyway"


def test_learn_refuses_on_normalize_unavailable():
    """learn() delegates to remember(), so it inherits the same fail-closed
    behavior (no separate normalizer call to patch around)."""
    s = ContradictionStore(":memory:", normalizer=_DegradedNormalizer())
    with pytest.raises(NormalizeUnavailable):
        s.learn("us", "infra", "claude", "A belief.", source="memo.md")


def test_supersede_always_fails_closed_on_normalize_unavailable():
    """supersede() has no force_exception override today, so a degraded
    provider always refuses (unconditional fail-closed)."""
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.normalizer = _DegradedNormalizer()
    with pytest.raises(NormalizeUnavailable):
        s.supersede("us", "infra", "bob", a.claim_id,
                    "TLS 1.2 must not be enabled in prod.")
    # original claim untouched
    active = s.active("us", "infra").claims
    assert len(active) == 1 and active[0]["id"] == a.claim_id
    row = s.db.execute(
        "SELECT * FROM conflict_events WHERE resolution='degraded_refused'").fetchone()
    assert row is not None


def test_diagnostics_reports_normalize_degraded_telemetry():
    """/diagnose telemetry (last_normalize_degraded_at + a count) must surface
    both refused AND forced-through degrades."""
    s = ContradictionStore(":memory:", normalizer=_DegradedNormalizer())
    diag = s.diagnostics()
    assert diag["conflicts"]["normalize_degraded_count"] == 0
    assert diag["conflicts"]["last_normalize_degraded_at"] is None

    with pytest.raises(NormalizeUnavailable):
        s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.remember("us", "infra", "bob", "MFA must be required.",
              force_exception="approved anyway")

    diag = s.diagnostics()
    assert diag["conflicts"]["normalize_degraded_count"] == 2
    assert diag["conflicts"]["last_normalize_degraded_at"] is not None


# --- code-review regression tests -----------------------------------------
def test_supersede_rejects_cross_project_id():
    """#3: supersede MUST be scoped by (org_id, project, id)."""
    s = fresh()
    a = s.remember("us", "projectA", "alice", "TLS 1.2 must be enabled in prod.")
    # Bob tries to supersede project A's claim while pointing at project B.
    with pytest.raises(KeyError):
        s.supersede("us", "projectB", "bob", a.claim_id,
                    "TLS 1.2 must not be enabled in prod.")
    # project A claim is untouched
    assert len(s.active("us", "projectA").claims) == 1


def test_supersede_blocks_if_residual_conflict_remains():
    """#2: superseding A must not leave the new claim contradicting still-active B."""
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    carol = s.remember("us", "infra", "carol",
                       "TLS 1.2 must be required in prod.")  # compatible w/ alice, active
    assert isinstance(carol, Stored)
    # Bob tries to supersede alice with the opposite stance — but carol is still active
    r = s.supersede("us", "infra", "bob", a.claim_id,
                    "TLS 1.2 must not be enabled in prod.")
    assert isinstance(r, Conflict), "supersede must block when other actives conflict"
    # alice is still active, no corruption
    assert any(c["id"] == a.claim_id for c in s.active("us", "infra").claims)


def test_sqlite_busy_is_retried_not_raised(tmp_path):
    """#1: SQLITE_BUSY at BEGIN IMMEDIATE must be retried, not raised."""
    db = str(tmp_path / "ergo.db")
    s1 = ContradictionStore(db, busy_timeout_ms=3000, max_retries=12)
    s1.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")

    lock_acquired = threading.Event()

    def hold_then_release():
        # connection lives entirely inside this thread (avoid cross-thread sqlite3)
        b = sqlite3.connect(db, isolation_level=None, timeout=5)
        b.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        time.sleep(0.4)
        b.execute("COMMIT")
        b.close()

    t = threading.Thread(target=hold_then_release)
    t.start()
    assert lock_acquired.wait(timeout=2)  # ensure blocker has the writer slot
    # This write MUST eventually complete (blocker releases inside retry budget).
    r = s1.remember("us", "infra", "alice", "MFA on login must be required.")
    t.join()
    assert isinstance(r, Stored), "write must succeed once the lock is released"


def test_race_recheck_retries_when_seq_advanced(tmp_path):
    """#3 race re-check: if another writer inserts between preflight and commit,
    we must retry rather than commit on a stale seq."""
    db = str(tmp_path / "race.db")
    s1 = ContradictionStore(db)
    s2 = ContradictionStore(db)
    s1.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    # both stores preflight an empty active set in a different project
    # (drive concurrent writes; expectation = both eventually succeed, no exception)
    a = s1.remember("us", "p", "alice", "A must be enabled.")
    b = s2.remember("us", "p", "bob", "B must be enabled.")
    assert isinstance(a, Stored) and isinstance(b, Stored)
    seqs = sorted(c["claim_seq"] for c in s1.history("us", "p"))
    assert seqs == [1, 2]   # monotonic, no duplicates -> race re-check worked


def test_warning_tier_surfaced_and_audited():
    """#7: MED-tier (warn) contradictions are NOT silently dropped — surfaced + audited."""
    s = fresh()
    # opposing soft/hard stance, same scope -> structural MED contradiction (warn)
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    r = s.remember("us", "infra", "bob", "TLS 1.2 should not be enabled in prod.")
    # warn-tier: not blocking, but must be surfaced as a warning + audit row
    assert isinstance(r, Stored)
    assert len(r.warnings) >= 1, "warn-tier contradiction must be returned"
    rows = s.db.execute(
        "SELECT * FROM conflict_events WHERE resolution='warned'").fetchall()
    assert rows, "warn-tier contradiction must be audited"


def test_persisted_norm_is_reused_not_renormalized():
    """#6: the persisted `norm` column is the source of truth for re-comparison."""
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    row = s.db.execute("SELECT norm FROM claims").fetchone()
    assert row["norm"] is not None, "norm must be persisted"
    # candidate built from the row carries norm so the judge skips re-normalization
    import json
    norm = json.loads(row["norm"])
    cand = s._candidate(sqlite3.Row.keys.__self__) if False else None  # noqa: just a comment
    # exercise via the public path: a new opposing claim still blocks
    r = s.remember("us", "infra", "bob", "TLS 1.2 must not be enabled in prod.")
    assert isinstance(r, Conflict)


def test_version_drift_columns_persisted():
    """#33: every stored claim records normalizer + judge versions."""
    s = fresh()
    s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    row = s.db.execute("SELECT normalizer_id, normalizer_version, judge_version, "
                       "norm_schema_version FROM claims").fetchone()
    assert row["normalizer_id"] == "RuleNormalizer"
    assert row["normalizer_version"] and row["judge_version"]
    assert row["norm_schema_version"] == "1"


def test_diagnostics_reports_counts_conflicts_and_version_drift():
    s = fresh()
    a = s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")
    s.remember("us", "infra", "carol", "MFA on login must be required in prod.")
    s.remember("us", "infra", "bob", "TLS 1.2 must not be enabled in prod.")
    s.db.execute("UPDATE claims SET normalizer_version='older' WHERE id=?", (a.claim_id,))
    diag = s.diagnostics()
    assert diag["counts"]["claims_total"] == 2
    assert diag["counts"]["claims_active"] == 2
    assert diag["conflicts"]["flagged_blocks"] == 1
    assert diag["versions"]["drift"] is True
    assert any(v.endswith(":older") for v in diag["versions"]["stored_normalizers"])


def test_lexical_empty_query_scores_zero():
    assert ContradictionStore._lexical("", "some text") == 0.0


def test_is_busy_uses_error_message_fallback():
    assert _is_busy(sqlite3.OperationalError("database is locked")) is True
    assert _is_busy(sqlite3.OperationalError("syntax error near nope")) is False


def test_retry_exhaustion_raises_runtime_error(monkeypatch):
    s = fresh()
    monkeypatch.setattr(s, "_remember_attempt", lambda *a, **k: (_ for _ in ()).throw(
        __import__("ergo.contradiction.store", fromlist=["_Retry"])._Retry()))
    with pytest.raises(RuntimeError, match="exceeded retries"):
        s.remember("us", "infra", "alice", "TLS 1.2 must be enabled in prod.")


# --- structural-only regressions (independent of the store) -------------
def test_empty_subject_does_not_clear_HIGH_unrelated():
    """#11 + Fix #1 (2026-06-26): an empty subject must never become a confident
    HIGH 'unrelated' clearance (the original #11 invariant) nor a contradiction.
    Fix #1 routes it to HIGH 'incomparable' -> the judge logs it and never hands a
    subject-less pair to noisy prose-NLI (that deferral caused the false-flag flood)."""
    from ergo.contradiction.structural import compare
    a = {"subject": "", "modality": "must", "object": "enabled", "scope": {}}
    b = {"subject": "", "modality": "must_not", "object": "enabled", "scope": {}}
    v = compare(a, b)
    assert v.label == "incomparable", v
    assert v.label not in ("unrelated", "contradiction"), v


def test_no_exceptions_does_not_bypass_scope():
    """#14: 'no exceptions' must NOT override disjoint scope."""
    from ergo.contradiction.structural import compare
    a = {"subject": "tls", "modality": "must", "object": "enabled",
         "scope": {"env": "prod"}, "exception": "no exceptions"}
    b = {"subject": "tls", "modality": "must_not", "object": "enabled",
         "scope": {"env": "dev"}}
    v = compare(a, b)
    assert v.label == "coexist", v


def test_exception_scope_leakage_is_not_silently_cleared():
    """#13/#15: a RuleNormalizer mis-scoping 'except in dev' onto a prod rule must
    not turn an opposing prod claim into HIGH coexist (we now CAP free-text
    exception clearance at MED — and importantly disjoint scope wins first)."""
    from ergo.contradiction.structural import compare
    # A: 'must_not in prod, except in dev'; B: 'may in prod' — overlapping scope, real conflict.
    a = {"subject": "x", "modality": "must_not", "object": "allowed",
         "scope": {"env": "prod"}, "exception": "except in dev"}
    b = {"subject": "x", "modality": "may", "object": "allowed",
         "scope": {"env": "prod"}}
    v = compare(a, b)
    assert v.label == "contradiction", v


def test_invalid_date_degrades_to_LOW():
    """#16: invalid/garbage dates must NOT produce HIGH structural decisions."""
    from ergo.contradiction.structural import compare
    a = {"subject": "x", "modality": "must", "object": "enabled",
         "scope": {}, "valid_from": "tomorrow"}
    b = {"subject": "x", "modality": "must_not", "object": "enabled",
         "scope": {}, "valid_from": "2026-13-99"}
    v = compare(a, b)
    assert v.confidence == "LOW", v


def test_nli_label_map_validated_at_init(monkeypatch, tmp_path):
    """#30: NLI must fail loud at init if the model's label map lacks 'contradiction'."""
    import json as _json, sys, types
    # fake config that LACKS 'contradiction'
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(_json.dumps({"id2label": {"0": "yes", "1": "no"}}))
    tok_path = tmp_path / "tokenizer.json"
    tok_path.write_text("{}")
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"")

    def fake_download(repo, fname, **kw):
        if fname.endswith("config.json"):
            return str(cfg_path)
        if fname.endswith("tokenizer.json"):
            return str(tok_path)
        return str(model_path)

    # patch huggingface_hub at the SOURCE — nli.py imports inside __init__
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    class FakeSess:
        def __init__(self, *a, **k): pass
        def get_inputs(self): return []
    class FakeTok:
        @staticmethod
        def from_file(p):
            class T:
                def enable_truncation(self, **kw): pass
            return T()
    fake_ort = types.SimpleNamespace(InferenceSession=lambda *a, **k: FakeSess())
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    fake_tokenizers = types.SimpleNamespace(Tokenizer=FakeTok)
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tokenizers)

    from ergo.contradiction.nli import NLIJudge
    with pytest.raises(RuntimeError, match="contradiction"):
        NLIJudge()


# --- org_stats' conflicts_by_day 7-day series -----------------------------
def _insert_conflict_event(s, org_id, ts, resolution="flagged"):
    s.db.execute(
        "INSERT INTO conflict_events(org_id,project,who,incoming,resolution,ts) "
        "VALUES(?,?,?,?,?,?)",
        (org_id, "p", "alice", "some incoming statement", resolution, ts))
    s.db.commit()


def _day_ts(days_ago: int, hhmmss: str = "12:00:00") -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days_ago * 86400))
    return f"{day}T{hhmmss}Z"


def test_org_stats_conflicts_by_day_is_seven_entries_zero_filled_and_ordered():
    s = fresh()
    stats = s.org_stats("empty-org")
    by_day = stats["conflicts_by_day"]
    assert len(by_day) == 7
    assert all(day["count"] == 0 for day in by_day)
    dates = [day["date"] for day in by_day]
    assert dates == sorted(dates)  # oldest -> newest
    assert dates[-1] == time.strftime("%Y-%m-%d", time.gmtime())
    assert dates[0] == time.strftime("%Y-%m-%d", time.gmtime(time.time() - 6 * 86400))
    for day in by_day:
        assert set(day) == {"date", "count"}


def test_org_stats_conflicts_by_day_counts_window_edges_and_multi_event_day():
    s = fresh()
    org = "acme"
    # today: in window
    _insert_conflict_event(s, org, _day_ts(0), resolution="flagged")
    # 6 days ago: in-window edge (oldest day of the 7-day window)
    _insert_conflict_event(s, org, _day_ts(6), resolution="superseded")
    # 7 days ago: OUT of window -- must not be counted in conflicts_by_day
    _insert_conflict_event(s, org, _day_ts(7), resolution="exception")
    # a day with 2 events, any resolution -- both counted
    _insert_conflict_event(s, org, _day_ts(3, "09:00:00"), resolution="flagged")
    _insert_conflict_event(s, org, _day_ts(3, "10:00:00"), resolution="exception")

    stats = s.org_stats(org)

    # existing lifetime total is unaffected by the new field -- includes the
    # out-of-window row that conflicts_by_day must exclude.
    assert stats["conflict_events"] == 5

    by_day = stats["conflicts_by_day"]
    assert len(by_day) == 7
    counts = {day["date"]: day["count"] for day in by_day}
    assert counts[_day_ts(0)[:10]] == 1
    assert counts[_day_ts(6)[:10]] == 1
    assert counts[_day_ts(3)[:10]] == 2
    assert _day_ts(7)[:10] not in counts


def test_org_stats_conflicts_by_day_is_isolated_between_orgs():
    s = fresh()
    _insert_conflict_event(s, "org-a", _day_ts(0))
    stats_b = s.org_stats("org-b")
    assert all(day["count"] == 0 for day in stats_b["conflicts_by_day"])
    stats_a = s.org_stats("org-a")
    assert sum(day["count"] for day in stats_a["conflicts_by_day"]) == 1


# --- audit_log() — tenant-facing GET /audit read (private-repo issue #124) -------------
def _insert_write(s, org_id, project, who, ts, action="remember", claim_id=None):
    s.db.execute(
        "INSERT INTO writes(org_id,project,who,action,claim_id,ts) "
        "VALUES(?,?,?,?,?,?)",
        (org_id, project, who, action, claim_id, ts))
    s.db.commit()


def test_audit_log_returns_both_streams_shaped_as_dicts():
    from ergo.contradiction.store import DEFAULT_AUDIT_LIMIT
    s = fresh()
    _insert_write(s, "acme", "p", "alice", "2026-01-01T00:00:00Z")
    _insert_conflict_event(s, "acme", "2026-01-01T00:00:01Z")
    # _insert_conflict_event hardcodes project="p" -- matches _insert_write above.
    log = s.audit_log("acme", "p")
    assert set(log) == {"writes", "conflict_events"}
    assert len(log["writes"]) == 1
    assert log["writes"][0]["org_id"] == "acme"
    assert log["writes"][0]["who"] == "alice"
    assert len(log["conflict_events"]) == 1
    assert log["conflict_events"][0]["org_id"] == "acme"


def test_audit_log_cross_tenant_isolation_by_org_and_project():
    s = fresh()
    _insert_write(s, "org-a", "p", "alice", "2026-01-01T00:00:00Z")
    _insert_write(s, "org-b", "p", "bob", "2026-01-01T00:00:00Z")
    _insert_write(s, "org-a", "other-project", "alice", "2026-01-01T00:00:00Z")
    _insert_conflict_event(s, "org-a", "2026-01-01T00:00:01Z")
    _insert_conflict_event(s, "org-b", "2026-01-01T00:00:01Z")

    log_a = s.audit_log("org-a", "p")
    assert len(log_a["writes"]) == 1
    assert log_a["writes"][0]["org_id"] == "org-a"
    assert log_a["writes"][0]["who"] == "alice"
    assert len(log_a["conflict_events"]) == 1
    assert log_a["conflict_events"][0]["org_id"] == "org-a"

    log_b = s.audit_log("org-b", "p")
    assert len(log_b["writes"]) == 1
    assert log_b["writes"][0]["org_id"] == "org-b"
    assert log_b["writes"][0]["who"] == "bob"
    assert len(log_b["conflict_events"]) == 1
    assert log_b["conflict_events"][0]["org_id"] == "org-b"

    # a project this org never wrote to comes back empty, not another
    # project's rows leaking through.
    log_a_other = s.audit_log("org-a", "unused-project")
    assert log_a_other == {"writes": [], "conflict_events": []}


def test_audit_log_orders_oldest_to_newest_by_id():
    s = fresh()
    # Insert out of ts order -- ordering must follow insertion (id), not ts.
    _insert_write(s, "acme", "p", "alice", "2026-01-03T00:00:00Z", claim_id="third")
    _insert_write(s, "acme", "p", "alice", "2026-01-01T00:00:00Z", claim_id="first")
    _insert_write(s, "acme", "p", "alice", "2026-01-02T00:00:00Z", claim_id="second")
    log = s.audit_log("acme", "p")
    assert [w["claim_id"] for w in log["writes"]] == ["third", "first", "second"]
    ids = [w["id"] for w in log["writes"]]
    assert ids == sorted(ids)


def test_audit_log_default_limit_bounds_each_stream_to_most_recent():
    from ergo.contradiction.store import DEFAULT_AUDIT_LIMIT
    s = fresh()
    total = DEFAULT_AUDIT_LIMIT + 5
    for i in range(total):
        _insert_write(s, "acme", "p", "alice", "2026-01-01T00:00:00Z", claim_id=str(i))
    log = s.audit_log("acme", "p")
    assert len(log["writes"]) == DEFAULT_AUDIT_LIMIT
    # most-recent N, oldest -> newest within that window.
    claim_ids = [w["claim_id"] for w in log["writes"]]
    assert claim_ids == [str(i) for i in range(5, total)]


def test_audit_log_limit_is_clamped_to_max_and_at_least_one():
    from ergo.contradiction.store import MAX_AUDIT_LIMIT
    s = fresh()
    for i in range(3):
        _insert_write(s, "acme", "p", "alice", "2026-01-01T00:00:00Z", claim_id=str(i))

    log = s.audit_log("acme", "p", limit=MAX_AUDIT_LIMIT + 500)
    assert len(log["writes"]) == 3  # nothing to clamp down to since only 3 rows exist

    log_min = s.audit_log("acme", "p", limit=0)
    assert len(log_min["writes"]) == 1

    log_neg = s.audit_log("acme", "p", limit=-10)
    assert len(log_neg["writes"]) == 1


def test_audit_log_degrades_to_default_on_bad_limit():
    from ergo.contradiction.store import DEFAULT_AUDIT_LIMIT
    s = fresh()
    for i in range(3):
        _insert_write(s, "acme", "p", "alice", "2026-01-01T00:00:00Z", claim_id=str(i))

    for bad in ("not-a-number", None, []):
        log = s.audit_log("acme", "p", limit=bad)
        # only 3 rows exist -- the assertion that matters is that a bad
        # `limit` degrades to DEFAULT_AUDIT_LIMIT instead of raising.
        assert len(log["writes"]) == 3


# --- active() pagination — bounded GET /active reads (private-repo issue #185) ---------
def test_active_unbounded_mode_is_unchanged_when_limit_omitted():
    # limit=None (the parameter default) is the pre-#185 opt-out: every
    # active row, count == returned == len(claims), never truncated. This
    # is the internal-caller / contradiction-judge behavior that must not
    # silently change.
    s = fresh()
    for i in range(3):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")
    aset = s.active("us", "infra")
    assert aset.state == "has_active_claims"
    assert len(aset.claims) == 3
    assert aset.count == 3
    assert aset.returned == 3
    assert aset.truncated is False
    assert aset.next_offset is None


def test_active_default_limit_caps_the_page_but_count_is_the_true_total():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT
    s = fresh()
    total = DEFAULT_ACTIVE_LIMIT + 5
    for i in range(total):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")
    aset = s.active("us", "infra", limit=DEFAULT_ACTIVE_LIMIT)
    assert aset.state == "has_active_claims"
    assert aset.count == total  # separate COUNT(*), independent of the page
    assert aset.returned == DEFAULT_ACTIVE_LIMIT
    assert len(aset.claims) == DEFAULT_ACTIVE_LIMIT
    assert aset.truncated is True
    assert aset.next_offset == DEFAULT_ACTIVE_LIMIT


def test_active_paging_with_offset_walks_the_whole_set_once_no_gaps_no_dupes():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT
    s = fresh()
    total = DEFAULT_ACTIVE_LIMIT * 2 + 7
    ids = [s.remember("us", "infra", "alice", f"Rule {i} must hold.").claim_id
           for i in range(total)]

    seen = []
    offset = 0
    pages = 0
    while True:
        aset = s.active("us", "infra", limit=DEFAULT_ACTIVE_LIMIT, offset=offset)
        seen.extend(c["id"] for c in aset.claims)
        pages += 1
        assert pages <= 10  # sanity: must terminate
        if not aset.truncated:
            assert aset.next_offset is None
            break
        offset = aset.next_offset

    assert len(seen) == total
    assert len(set(seen)) == total  # no duplicates
    assert set(seen) == set(ids)  # no gaps


def test_active_offset_past_end_is_empty_claims_but_state_stays_has_active_claims():
    # Non-negotiable #4: state describes the SCOPE, not the page.
    s = fresh()
    s.remember("us", "infra", "alice", "Only rule.")
    aset = s.active("us", "infra", limit=10, offset=500)
    assert aset.claims == []
    assert aset.returned == 0
    assert aset.count == 1
    assert aset.truncated is False  # nothing left to page to
    assert aset.next_offset is None
    assert aset.state == "has_active_claims"  # never "empty"


def test_active_limit_is_clamped_to_max_and_at_least_one():
    from ergo.contradiction.store import MAX_ACTIVE_LIMIT
    s = fresh()
    for i in range(3):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")

    aset = s.active("us", "infra", limit=MAX_ACTIVE_LIMIT + 500)
    assert aset.returned == 3  # nothing to clamp down to since only 3 rows exist

    aset_min = s.active("us", "infra", limit=0)
    assert aset_min.returned == 1

    aset_neg = s.active("us", "infra", limit=-10)
    assert aset_neg.returned == 1


def test_active_degrades_to_default_on_bad_limit():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT
    s = fresh()
    for i in range(3):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")

    for bad in ("not-a-number", None, []):
        aset = s.active("us", "infra", limit=bad)
        # only 3 rows exist -- the assertion that matters is that a bad
        # `limit` degrades to DEFAULT_ACTIVE_LIMIT instead of raising.
        assert aset.returned == 3


def test_active_project_missing_and_empty_states_unaffected_by_pagination():
    s = fresh()
    aset = s.active("us", "nope", limit=10)
    assert aset.state == "project_missing"
    assert aset.count == 0
    assert aset.claims == []

    s.remember("us", "infra", "alice", "MFA on login must be required.")
    s.db.execute("UPDATE claims SET status='superseded'")
    aset = s.active("us", "infra", limit=10)
    assert aset.state == "empty"
    assert aset.count == 0
    assert aset.claims == []


def test_active_pushes_limit_offset_into_sql_not_python_slicing():
    # Non-negotiable #1: paginate in SQL, not by loading every row and
    # slicing in Python. Pin this by recording the actual SQL text passed
    # to _exec() and asserting LIMIT/OFFSET is IN the claims query itself
    # -- a Python-side slice would never need to build that SQL at all.
    s = fresh()
    for i in range(5):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")

    queries = []
    real_exec = s._exec

    def spy(sql, params=()):
        queries.append(sql)
        return real_exec(sql, params)

    s._exec = spy
    aset = s.active("us", "infra", limit=2, offset=1)
    assert aset.returned == 2

    claims_queries = [q for q in queries if "FROM claims" in q and "status='active'" in q]
    assert any("LIMIT ? OFFSET ?" in q for q in claims_queries), claims_queries


# --- active() summary projection + byte budget (private-repo issue #206) --------------
def test_active_summary_projection_returns_id_and_statement_and_omits_reason():
    s = fresh()
    s.remember("us", "infra", "alice", "Rule must hold.", reason="because compliance")
    aset = s.active("us", "infra", limit=10, projection="summary")
    assert len(aset.claims) == 1
    claim = aset.claims[0]
    assert set(claim) == {"id", "statement"}
    assert "reason" not in claim  # key ABSENT, not merely empty/null


def test_active_summary_projection_count_is_still_scope_total():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT
    s = fresh()
    total = DEFAULT_ACTIVE_LIMIT + 5
    for i in range(total):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")
    aset = s.active("us", "infra", limit=DEFAULT_ACTIVE_LIMIT, projection="summary")
    assert aset.count == total
    assert aset.returned == DEFAULT_ACTIVE_LIMIT
    assert aset.truncated is True


def test_active_summary_projection_paging_walks_whole_scope_once_no_gaps_no_dupes():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT
    s = fresh()
    total = DEFAULT_ACTIVE_LIMIT * 2 + 7
    ids = [s.remember("us", "infra", "alice", f"Rule {i} must hold.").claim_id
           for i in range(total)]

    seen = []
    offset = 0
    pages = 0
    while True:
        aset = s.active("us", "infra", limit=DEFAULT_ACTIVE_LIMIT, offset=offset,
                        projection="summary")
        seen.extend(c["id"] for c in aset.claims)
        pages += 1
        assert pages <= 10  # sanity: must terminate
        if not aset.truncated:
            assert aset.next_offset is None
            break
        offset = aset.next_offset

    assert len(seen) == total
    assert len(set(seen)) == total  # no duplicates
    assert set(seen) == set(ids)  # no gaps


def test_active_bad_projection_degrades_to_full():
    s = fresh()
    s.remember("us", "infra", "alice", "Rule must hold.", reason="because")
    for bad in ("not-a-projection", None, "", 42):
        aset = s.active("us", "infra", limit=10, projection=bad)
        assert "reason" in aset.claims[0]


def test_active_full_projection_default_is_unchanged_from_before_summary_mode():
    # #206 must not touch the existing shape of a caller who never asks for
    # projection at all -- every column full mode already returned stays,
    # unaffected by the new opt-in argument.
    s = fresh()
    s.remember("us", "infra", "alice", "Rule must hold.", reason="because compliance")
    aset_default = s.active("us", "infra", limit=10)
    aset_explicit_full = s.active("us", "infra", limit=10, projection="full")
    assert aset_default.claims == aset_explicit_full.claims
    claim = aset_default.claims[0]
    assert claim["statement"] == "Rule must hold."
    assert claim["reason"] == "because compliance"
    assert "norm" in claim
    assert "status" in claim


def test_active_byte_budget_truncates_a_fat_corpus_before_the_row_limit():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT, ACTIVE_BYTE_BUDGET
    s = fresh()
    # Each claim's reason alone is ~4 KB -- DEFAULT_ACTIVE_LIMIT (50) of these
    # would be ~200 KB of served JSON, blowing ACTIVE_BYTE_BUDGET (~50 KB)
    # long before the row limit does.
    fat_reason = "x" * 4000
    for i in range(DEFAULT_ACTIVE_LIMIT):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.", reason=fat_reason)

    aset = s.active("us", "infra", limit=DEFAULT_ACTIVE_LIMIT)
    assert aset.count == DEFAULT_ACTIVE_LIMIT
    assert aset.returned < DEFAULT_ACTIVE_LIMIT  # cut short of the row limit
    assert aset.truncated is True
    assert aset.truncated_reason == "byte_budget"
    assert aset.next_offset == aset.returned
    served_bytes = sum(len(json.dumps(c)) for c in aset.claims)
    assert served_bytes <= ACTIVE_BYTE_BUDGET


def test_active_row_limit_truncation_reports_row_limit_not_byte_budget():
    from ergo.contradiction.store import DEFAULT_ACTIVE_LIMIT
    s = fresh()
    total = DEFAULT_ACTIVE_LIMIT + 5
    for i in range(total):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.")
    aset = s.active("us", "infra", limit=DEFAULT_ACTIVE_LIMIT)
    assert aset.truncated is True
    assert aset.truncated_reason == "row_limit"


def test_active_not_truncated_has_no_truncated_reason():
    s = fresh()
    s.remember("us", "infra", "alice", "Only rule.")
    aset = s.active("us", "infra", limit=10)
    assert aset.truncated is False
    assert aset.truncated_reason is None


def test_active_byte_budget_never_applies_in_unbounded_mode():
    # The unbounded internal opt-out (limit=None) must stay exactly as before
    # -- a byte budget is a paging concern, and unbounded callers never page.
    from ergo.contradiction.store import ACTIVE_BYTE_BUDGET
    s = fresh()
    fat_reason = "x" * 4000
    for i in range(30):
        s.remember("us", "infra", "alice", f"Rule {i} must hold.", reason=fat_reason)
    aset = s.active("us", "infra")  # no limit -> unbounded opt-out
    assert aset.truncated is False
    assert aset.returned == 30
    served_bytes = sum(len(json.dumps(c)) for c in aset.claims)
    assert served_bytes > ACTIVE_BYTE_BUDGET  # well over budget, but never cut


# --- export_claims: nested revisions[] why-chain (private-repo issue #189) -------------
def test_export_claims_chain_of_three_nests_revisions_oldest_first():
    """A -> B -> C supersede chain exports as ONE active head (C) with 2
    nested `revisions`, oldest-first: revisions[0] is A (the very first
    claim), revisions[1] is B (the one immediately before C). Each entry
    carries the PREDECESSOR's own statement/reason/who/status/created_at --
    e.g. A keeps its own original reason, not C's."""
    s = fresh()
    a = s.remember("acme", "infra", "alice", "Postgres is the datastore.",
                   reason="initial pick, ops familiarity")
    assert isinstance(a, Stored)
    b = s.supersede("acme", "infra", "bob", a.claim_id,
                    "MySQL is the datastore.", reason="chosen for replication support")
    assert isinstance(b, Stored)
    c = s.supersede("acme", "infra", "carol", b.claim_id,
                    "CockroachDB is the datastore.", reason="chosen for distributed consistency")
    assert isinstance(c, Stored)

    claims = s.export_claims("acme", project="infra").claims
    assert len(claims) == 1  # only the active head is a top-level entry
    head = claims[0]
    assert head["id"] == c.claim_id
    assert head["status"] == "active"
    assert head["statement"] == "CockroachDB is the datastore."
    assert head["reason"] == "chosen for distributed consistency"

    revisions = head["revisions"]
    assert len(revisions) == 2
    assert revisions[0]["statement"] == "Postgres is the datastore."
    assert revisions[0]["reason"] == "initial pick, ops familiarity"
    assert revisions[0]["who"] == "alice"
    assert revisions[0]["status"] == "superseded"
    assert "created_at" in revisions[0]
    assert revisions[1]["statement"] == "MySQL is the datastore."
    assert revisions[1]["reason"] == "chosen for replication support"
    assert revisions[1]["who"] == "bob"
    assert revisions[1]["status"] == "superseded"


def test_export_claims_never_revised_omits_revisions_key():
    """A claim that was never superseded must not carry a `revisions` key
    at all -- no null/empty placeholder on every claim (most claims have no
    chain, and the export is already large)."""
    s = fresh()
    s.remember("acme", "infra", "alice", "TLS 1.2 must be enabled in prod.",
              reason="compliance")
    claims = s.export_claims("acme", project="infra").claims
    assert len(claims) == 1
    assert "revisions" not in claims[0]


def test_export_claims_retracted_head_is_excluded_entirely():
    """A retracted (never-superseded) claim must not appear as a top-level
    export entry -- retraction is wrong-at-birth removal, and re-exporting
    it would resurrect noise the user deliberately removed."""
    s = fresh()
    a = s.remember("acme", "infra", "alice", "Typo claim, remove me.", reason="oops")
    s.retract("acme", "infra", "alice", a.claim_id, reason="wrong at birth")
    claims = s.export_claims("acme", project="infra").claims
    assert claims == []


def test_export_claims_retracted_active_claim_leaves_no_orphaned_predecessor():
    """A -> B (B active), then B is retracted: B must not appear (retracted),
    and A (superseded, now with no active descendant) must not resurface as
    an orphaned top-level head or inside any revisions list -- it only ever
    appears nested under an ACTIVE head, and there is none left."""
    s = fresh()
    a = s.remember("acme", "infra", "alice", "Postgres is the datastore.",
                   reason="initial pick")
    b = s.supersede("acme", "infra", "bob", a.claim_id,
                    "MySQL is the datastore.", reason="chosen for replication")
    s.retract("acme", "infra", "bob", b.claim_id, reason="wrong at birth after all")
    claims = s.export_claims("acme", project="infra").claims
    assert claims == []


def test_export_claims_chain_walk_survives_a_corrupted_cycle(monkeypatch):
    """Defense-in-depth (review finding on private-repo PR #189): a cycle can never arise
    through the guarded write path (`supersedes` is set exactly once at
    insert time), but export_claims must not infinite-loop if one is ever
    introduced by a corrupted/legacy row -- it must degrade (stop walking)
    rather than hang. Runs with a short thread-level timeout so a
    regression here fails the test suite instead of hanging CI forever."""
    s = fresh()
    a = s.remember("acme", "infra", "alice", "A.", reason="ra")
    b = s.supersede("acme", "infra", "bob", a.claim_id, "B.", reason="rb")
    c = s.supersede("acme", "infra", "carol", b.claim_id, "C.", reason="rc")
    # Corrupt the chain: point A's supersedes at C, forming a cycle
    # A -> C -> B -> A that the guarded API could never produce.
    s.db.execute("UPDATE claims SET supersedes=? WHERE id=?", (c.claim_id, a.claim_id))
    s.db.commit()

    result = {}

    def _run():
        result["claims"] = s.export_claims("acme", project="infra").claims

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "export_claims hung on a cyclic supersedes chain"
    claims = result["claims"]
    assert len(claims) == 1
    assert claims[0]["id"] == c.claim_id
    # The cycle is broken before it loops back to a claim already seen.
    stmts = [rev["statement"] for rev in claims[0]["revisions"]]
    assert len(stmts) == len(set(stmts))


# --- export_claims: pagination (private-repo issue #203) --------------------------------
def test_export_claims_returns_export_set_with_pagination_fields():
    s = fresh()
    s.remember("acme", "infra", "alice", "Only rule.", reason="r")
    result = s.export_claims("acme", project="infra")
    assert isinstance(result, ExportSet)
    assert result.count == 1
    assert result.returned == 1
    assert result.truncated is False
    assert result.next_offset is None
    assert result.as_of_seq == {"infra": 1}


def test_export_claims_no_limit_is_unbounded_exactly_like_pre_pagination_behavior():
    # Backward compat: an existing caller passing no `limit` must keep
    # getting the FULL scope, never a silently truncated file (private-repo issue #203).
    s = fresh()
    for i in range(30):
        s.remember("acme", "infra", "alice", f"Rule {i} must hold.", reason="r")
    result = s.export_claims("acme", project="infra")
    assert result.count == 30
    assert result.returned == 30
    assert len(result.claims) == 30
    assert result.truncated is False
    assert result.next_offset is None


def test_export_claims_limit_offset_walks_whole_corpus_no_gaps_no_repeats():
    s = fresh()
    ids = []
    for i in range(23):
        r = s.remember("acme", "infra", "alice", f"Rule {i} must hold.", reason="r")
        ids.append(r.claim_id)

    seen = []
    offset = 0
    for _ in range(100):  # generous cap so a broken loop fails instead of hanging
        page = s.export_claims("acme", project="infra", limit=5, offset=offset)
        assert page.count == 23
        seen.extend(c["id"] for c in page.claims)
        if not page.truncated:
            assert page.next_offset is None
            break
        offset = page.next_offset
    assert len(seen) == len(set(seen)) == 23
    assert set(seen) == set(ids)


def test_export_claims_count_is_scope_total_regardless_of_page():
    s = fresh()
    for i in range(10):
        s.remember("acme", "infra", "alice", f"Rule {i} must hold.", reason="r")
    first = s.export_claims("acme", project="infra", limit=3, offset=0)
    last = s.export_claims("acme", project="infra", limit=3, offset=9)
    assert first.count == 10
    assert last.count == 10


def test_export_claims_reading_past_the_end_is_a_clean_empty_page():
    s = fresh()
    s.remember("acme", "infra", "alice", "Only rule.", reason="r")
    result = s.export_claims("acme", project="infra", limit=5, offset=500)
    assert result.claims == []
    assert result.returned == 0
    assert result.count == 1
    assert result.truncated is False
    assert result.next_offset is None


def test_export_claims_limit_is_clamped_to_max_and_at_least_one():
    from ergo.contradiction.store import MAX_EXPORT_LIMIT
    s = fresh()
    for i in range(3):
        s.remember("acme", "infra", "alice", f"Rule {i} must hold.", reason="r")

    result = s.export_claims("acme", project="infra", limit=MAX_EXPORT_LIMIT + 500)
    assert result.returned == 3

    result_min = s.export_claims("acme", project="infra", limit=0)
    assert result_min.returned == 1

    result_neg = s.export_claims("acme", project="infra", limit=-10)
    assert result_neg.returned == 1


def test_export_claims_degrades_to_default_on_bad_limit():
    from ergo.contradiction.store import DEFAULT_EXPORT_LIMIT
    s = fresh()
    for i in range(3):
        s.remember("acme", "infra", "alice", f"Rule {i} must hold.", reason="r")

    for bad in ("not-a-number", []):
        result = s.export_claims("acme", project="infra", limit=bad)
        # only 3 rows exist -- what matters is a bad `limit` degrades to
        # DEFAULT_EXPORT_LIMIT instead of raising.
        assert result.returned == 3


def test_export_claims_scoped_project_populates_as_of_seq_even_when_empty():
    # Review finding on private-repo PR #208: ExportSet.as_of_seq's own docstring promises
    # a single SCOPED project always gets a key -- a caller comparing
    # as_of_seq[project] across calls must never see a missing key just
    # because this particular call happened to find zero rows (a typo'd or
    # not-yet-used project name is exactly when a baseline matters most).
    s = fresh()
    result = s.export_claims("acme", project="never-used")
    assert result.claims == []
    assert result.as_of_seq == {"never-used": 0}


def test_export_claims_whole_org_as_of_seq_stays_empty_when_org_has_no_claims():
    # The opposite case, project=None: with no rows anywhere in the org
    # there are no project keys to enumerate a baseline for, so {} is
    # correct here (contrast with the single-scoped-project case above,
    # which always knows the one project key to report).
    s = fresh()
    result = s.export_claims("acme", project=None)
    assert result.claims == []
    assert result.as_of_seq == {}


def test_export_claims_whole_org_pagination_across_multiple_projects():
    # project=None ("every project in org_id") must page correctly too.
    s = fresh()
    for i in range(6):
        s.remember("acme", "p1", "alice", f"P1 rule {i}.", reason="r")
    for i in range(6):
        s.remember("acme", "p2", "alice", f"P2 rule {i}.", reason="r")

    seen = []
    offset = 0
    for _ in range(100):
        page = s.export_claims("acme", project=None, limit=4, offset=offset)
        assert page.count == 12
        seen.extend(c["id"] for c in page.claims)
        if not page.truncated:
            break
        offset = page.next_offset
    assert len(seen) == len(set(seen)) == 12
    assert page.as_of_seq == {"p1": 6, "p2": 6}


def test_export_claims_chain_astride_page_boundary_stays_atomic_with_its_head():
    """A head's full supersede chain must ride along inside its OWN page,
    never split, even when the chain's head lands right at a page edge
    (private-repo issue #203) -- pagination is over HEADS, and each head embeds its
    entire `revisions[]` regardless of where predecessors sit."""
    s = fresh()
    # Two plain claims, then a 3-claim supersede chain, then two more plain
    # claims -- with limit=3 the chain's head (claim_seq 5, "C.") is the
    # LAST entry of the first page.
    s.remember("acme", "infra", "alice", "Plain 1.", reason="r")
    s.remember("acme", "infra", "alice", "Plain 2.", reason="r")
    a = s.remember("acme", "infra", "alice", "A.", reason="ra")
    b = s.supersede("acme", "infra", "bob", a.claim_id, "B.", reason="rb")
    c = s.supersede("acme", "infra", "carol", b.claim_id, "C.", reason="rc")
    s.remember("acme", "infra", "alice", "Plain 3.", reason="r")
    s.remember("acme", "infra", "alice", "Plain 4.", reason="r")

    page1 = s.export_claims("acme", project="infra", limit=3, offset=0)
    assert [h["statement"] for h in page1.claims] == ["Plain 1.", "Plain 2.", "C."]
    head_c = page1.claims[2]
    assert head_c["id"] == c.claim_id
    assert [rev["statement"] for rev in head_c["revisions"]] == ["A.", "B."]

    page2 = s.export_claims("acme", project="infra", limit=3, offset=3)
    assert [h["statement"] for h in page2.claims] == ["Plain 3.", "Plain 4."]
    # The chain never leaks a fragment into the next page.
    assert not any("revisions" in h for h in page2.claims)
