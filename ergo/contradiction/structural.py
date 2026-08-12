"""Structural comparator — deterministic contradiction logic over normalized claims.

Operates on `norm` dicts (see normalize.py / the eval-set schema):
    {subject, modality, object, scope:{env,service_class,region,version},
     valid_from, valid_until, exception}

Hardened after a 2026-06-25 code review:
  - Subject result is tri-state (SAME/DIFFERENT/UNKNOWN); fuzzy non-match never
    produces HIGH unrelated; empty subject -> UNKNOWN/LOW (falls to NLI).
  - Modality must validate against the enum; unknown -> stance None -> LOW.
  - Dates parsed strictly; bad/inverted -> intervals not authoritative -> LOW.
  - Exception logic runs AFTER scope/time gates; "no exceptions" only matters
    inside overlapping scope+time; free-text exception caps at LOW unless
    structurally represented.
  - Same stance + different scope is `coexist`, not `entailment`.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date

HIGH = "HIGH"
MED = "MED"
LOW = "LOW"

# Canonical object polarity (single source of truth — also imported by normalizers).
# +1 = the object verb asserts/enables the subject; -1 = it negates/disables it.
OBJECT_POLARITY = {
    "enabled": +1, "allowed": +1, "used": +1, "included": +1, "required": +1,
    "enforced": +1, "on": +1, "permitted": +1, "deployed": +1, "run": +1,
    "run_on_deploy": +1, "approved": +1, "stored": +1, "available": +1,
    # general-purpose antonym terms (so object-antonym opposition is detected on
    # real prose, not just the Helix enum): keep/grant/accept/retain families.
    "retained": +1, "retain": +1, "kept": +1, "keep": +1, "granted": +1,
    "grant": +1, "accepted": +1, "accept": +1, "enable": +1, "allow": +1,
    "disabled": -1, "removed": -1, "blocked": -1, "off": -1, "prohibited": -1,
    "skipped": -1, "disallowed": -1, "not_approved": -1, "turned_off": -1,
    "forbidden": -1, "rejected": -1, "reject": -1, "denied": -1, "deny": -1,
    "remove": -1, "dropped": -1, "disable": -1, "disallow": -1,
}
ENABLING = {k for k, v in OBJECT_POLARITY.items() if v > 0}
DISABLING = {k for k, v in OBJECT_POLARITY.items() if v < 0}

MODALITIES = {"must", "must_not", "should", "should_not", "may"}
STRENGTH = {"must": 2, "must_not": 2, "should": 1, "should_not": 1, "may": 0}
NEG_MODAL = {"must_not", "should_not"}

# Subject-match outcomes
SAME = "same"
DIFFERENT = "different"
UNKNOWN = "unknown"      # both subjects present but only fuzzily overlapping -> defer to NLI
MISSING = "missing"      # one/both subjects absent -> NOT comparable, do NOT defer to noisy NLI

_SCOPE_DIMS = ("env", "service_class", "region", "version")


@dataclass
class Verdict:
    label: str        # contradiction | coexist | entailment | unrelated | unknown
    confidence: str   # HIGH | MED | LOW
    reason: str
    # True only for the "same subject + mutually-exclusive object value" case
    # (blue vs red canary, port 8443 vs 9090). The judge handles these specially:
    # there is no polarity flip in the text, so the polarity gate must NOT veto them.
    value_conflict: bool = False

    @property
    def flags(self) -> bool:
        return self.label == "contradiction"


# --- subject matching (tri-state, no HIGH false-clearances) ----------------
def _subject_kind(a: dict, b: dict) -> tuple[str, str]:
    """Returns (kind, reason). kind in {SAME, DIFFERENT, UNKNOWN}."""
    sa = (a.get("subject") or "").strip()
    sb = (b.get("subject") or "").strip()
    if not sa or not sb:
        # Fix #1 (2026-06-26): a missing subject means we genuinely cannot compare
        # these claims structurally. Handing them to raw NLI (P~0.58 on prose) was
        # the source of the worst false flags. Mark MISSING -> caller drops it.
        return MISSING, "missing subject (extractor failure)"
    if sa == sb:
        return SAME, "exact subject match"
    ta = set(str(sa).replace("_", " ").split())
    tb = set(str(sb).replace("_", " ").split())
    if not ta or not tb:
        return MISSING, "empty token sets"
    jaccard = len(ta & tb) / len(ta | tb)
    if jaccard >= 0.8:
        return SAME, f"high-overlap subject ({jaccard:.2f})"
    if jaccard >= 0.5:
        # could be the same subject phrased differently — not safe to clear or block
        return UNKNOWN, f"fuzzy subject overlap ({jaccard:.2f})"
    return DIFFERENT, f"low subject overlap ({jaccard:.2f})"


# --- scope ----------------------------------------------------------------
def _scope_disjoint(a: dict, b: dict) -> str | None:
    """Disjoint if any dim where BOTH specify a value disagrees. Returns the dim."""
    sa, sb = a.get("scope") or {}, b.get("scope") or {}
    for d in _SCOPE_DIMS:
        va, vb = sa.get(d), sb.get(d)
        if va and vb and va != vb:
            return d
    return None


def _scope_overlaps(a_scope_val, b_scope_val) -> bool:
    """Two values on a single scope dim 'overlap' iff at least one is None (wildcard)
    or they're equal."""
    if a_scope_val is None or b_scope_val is None:
        return True
    return a_scope_val == b_scope_val


# --- time intervals (validated dates only; bad input -> LOW) --------------
def _parse_date(x) -> date | None:
    if x is None:
        return None
    if not isinstance(x, str):
        return None
    try:
        y, m, d = (int(p) for p in x.split("-"))
        return date(y, m, d)
    except Exception:
        return None


def _windows_status(a: dict, b: dict) -> tuple[bool, bool]:
    """(overlap_known, overlaps) — returns (False, _) when any provided date is invalid
    or any interval is inverted; the caller treats unknown intervals as non-authoritative.
    Inclusive day-granularity semantics."""
    af = _parse_date(a.get("valid_from"))
    au = _parse_date(a.get("valid_until"))
    bf = _parse_date(b.get("valid_from"))
    bu = _parse_date(b.get("valid_until"))
    # invalid: provided but unparseable
    for given, parsed in (
        (a.get("valid_from"), af), (a.get("valid_until"), au),
        (b.get("valid_from"), bf), (b.get("valid_until"), bu),
    ):
        if given is not None and parsed is None:
            return False, False
    # inverted intervals
    if af and au and af > au:
        return False, False
    if bf and bu and bf > bu:
        return False, False
    af_eff = af or date.min
    au_eff = au or date.max
    bf_eff = bf or date.min
    bu_eff = bu or date.max
    return True, (af_eff <= bu_eff and bf_eff <= au_eff)


# --- stance (validate modality enum; require known object) ----------------
def _stance(norm: dict):
    """+1 = claim asserts/permits subject-state; -1 = forbids. None if undetermined."""
    modality = norm.get("modality")
    if modality not in MODALITIES:
        return None
    msign = -1 if modality in NEG_MODAL else +1
    obj = (norm.get("object") or "")
    if not isinstance(obj, str):
        return None
    obj = obj.lower().replace(" ", "_")
    osign = OBJECT_POLARITY.get(obj)
    if osign is None:
        return None
    return msign * osign


# --- value selector (the differentiating object argument) -----------------
def _value(norm: dict):
    """Normalized value-bearing object selector (colour/port/version/identifier),
    or None. This is the field whose loss let "blue canary" and "red canary"
    collapse to an identical norm — same subject, same stance, no way to tell them
    apart. A non-null value on BOTH sides that disagrees is a mutually-exclusive
    prescription."""
    v = norm.get("value")
    if v is None:
        return None
    v = str(v).lower().strip()
    return v or None


def _atomic_value(v: str) -> bool:
    """A trustworthy value selector is ATOMIC — a colour, a port, a version, a
    hostname, a short identifier (1-2 tokens). Multi-word values are extractor
    noise on documentation prose (e.g. "stable zalo user ids"), not a real
    mutually-exclusive selector; we refuse to fire the value-conflict gate on them
    so config-enumeration docs don't produce false contradictions."""
    if not v:
        return False
    toks = v.split()
    return 1 <= len(toks) <= 2 and len(v) <= 24


# --- exception parsing (free-text only; structured form would be safer) ---
def _has_no_exception_marker(norm: dict) -> bool:
    ex = (norm.get("exception") or "").lower()
    return "no exception" in ex


def _exception_env_match(norm: dict, other_scope: dict) -> bool:
    """True iff this claim's free-text exception names an env that matches the
    other claim's scope env. Free-text and brittle — caller must NOT use as a
    HIGH clearance; we only use this as one input to a MED/LOW decision."""
    ex = (norm.get("exception") or "").lower()
    if not ex:
        return False
    for env in ("dev", "staging", "prod"):
        if env in ex and (other_scope or {}).get("env") == env:
            return True
    return False


# --- main entry point -----------------------------------------------------
def compare(a: dict, b: dict) -> Verdict:
    # 1. subject
    kind, why = _subject_kind(a, b)
    if kind == DIFFERENT:
        return Verdict("unrelated", HIGH, why)
    if kind == MISSING:
        # Fix #1: terminal, HIGH-confidence "not comparable" -> the judge's HIGH
        # branch logs it and never calls NLI. (Distinct from the fuzzy-subject
        # UNKNOWN case below, which IS worth an NLI look.)
        return Verdict("incomparable", HIGH, f"no subject extracted -> not comparable ({why})")
    if kind == UNKNOWN:
        return Verdict("unknown", LOW, f"subject unknown -> defer to NLI ({why})")

    # 2. scope disjointness (the main FP killer for "X in prod" vs "X in dev")
    dim = _scope_disjoint(a, b)
    if dim is not None:
        sa, sb = _stance(a), _stance(b)
        if sa is not None and sb is not None and sa == sb:
            # same direction in different sub-scopes -> compatible, NOT entailment
            return Verdict("coexist", HIGH,
                           f"same stance, different {dim} scope -> compatible")
        return Verdict("coexist", HIGH, f"disjoint {dim} scope")

    # 3. time intervals (only authoritative if BOTH parse cleanly)
    overlap_known, overlaps = _windows_status(a, b)
    if not overlap_known:
        return Verdict("unknown", LOW, "invalid/inverted date in interval -> defer")
    if not overlaps:
        return Verdict("coexist", HIGH, "non-overlapping time windows (supersession)")

    # 4. exceptions — applied AFTER scope/time overlap is established.
    # "No exceptions" inside the overlapping region: opposing stance => contradiction.
    sa, sb = _stance(a), _stance(b)
    if (_has_no_exception_marker(a) or _has_no_exception_marker(b)) \
            and sa is not None and sb is not None and sa != sb:
        return Verdict("contradiction", HIGH,
                       "blanket rule (no exceptions) vs opposing carve-out in overlapping scope")
    # Free-text exception naming the other claim's env: ambiguous, cap at MED coexist.
    if _exception_env_match(a, b.get("scope") or {}) or \
            _exception_env_match(b, a.get("scope") or {}):
        return Verdict("coexist", MED,
                       "free-text exception appears to match the other claim's scope")

    # 4.5 value conflict — same subject, overlapping scope+time, but the
    # value-bearing object selector differs (blue vs red canary, port 8443 vs 9090).
    # Fires ONLY when BOTH sides are genuine same-direction prescriptions (stance
    # determined and equal) carrying ATOMIC, disagreeing selectors: you cannot be
    # required to use BOTH a blue and a red canary. Opposing stance is handled by
    # the stance logic below; undetermined stance / multi-word noisy values fall
    # through (precision guard against config-enumeration docs). There is no
    # polarity flip here, so the judge must skip the polarity gate — the verdict
    # carries value_conflict=True for exactly that reason.
    va, vb = _value(a), _value(b)
    if (va and vb and va != vb
            and _atomic_value(va) and _atomic_value(vb)
            and sa is not None and sb is not None and sa == sb):
        da = STRENGTH.get(a.get("modality"), 0)
        db = STRENGTH.get(b.get("modality"), 0)
        conf = HIGH if (da == 2 and db == 2) else (MED if max(da, db) >= 1 else LOW)
        return Verdict(
            "contradiction", conf,
            f"value conflict on shared subject: {va!r} vs {vb!r} "
            "(mutually-exclusive object selector, same stance)",
            value_conflict=True)

    # 5. overlapping scope + time: decide by stance
    if sa is None or sb is None:
        return Verdict("unknown", LOW, "stance undetermined (invalid modality/object) -> defer")

    if sa != sb:
        # opposing stance: hardness depends on both modalities being hard
        da = STRENGTH[a["modality"]]
        db = STRENGTH[b["modality"]]
        if da == 2 and db == 2:
            return Verdict("contradiction", HIGH, "hard-vs-hard opposing stance, overlapping scope+time")
        if max(da, db) >= 1:
            return Verdict("contradiction", MED,
                           "soft-vs-hard or soft-vs-soft opposing stance (warn-tier)")
        return Verdict("contradiction", LOW, "permissive opposing stance")

    # same stance -> agreement / strength relation
    da = STRENGTH[a["modality"]]
    db = STRENGTH[b["modality"]]
    if da == db:
        return Verdict("entailment", HIGH, "same stance and strength (restatement/agreement)")
    return Verdict("entailment", HIGH, "same direction, differing strength (must=>should)")
