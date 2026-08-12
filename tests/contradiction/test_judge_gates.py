"""Tests for the text-level precision gates (2026-06-26).

These guard against the real-world-document-ingest false-positive classes:
  - Gate 1 (polarity): templated boilerplate / collapsed-subject pairs that have
    no actual opposing polarity in the text.
  - Gate 2 (aspect): same coarse subject, near-zero predicate overlap.
Real contradictions (a genuine polarity flip + shared predicate) must survive.
"""
from __future__ import annotations
import pytest

from ergo.contradiction.judge import (
    ContradictionJudge, _polarity_flip, _tokens, _jaccard,
)
from ergo.contradiction.normalize import RuleNormalizer


# --- unit: polarity flip detector ----------------------------------------
def test_polarity_flip_negation_asymmetry():
    a = _tokens("TLS must be enabled in prod.")
    b = _tokens("TLS must not be enabled in prod.")
    assert _polarity_flip(a, b) is True


def test_polarity_flip_object_antonyms():
    a = _tokens("Telemetry must be enabled for all services.")
    b = _tokens("Telemetry must be disabled for all services.")
    assert _polarity_flip(a, b) is True


def test_no_polarity_flip_on_parallel_boilerplate():
    # both say "no separate install needed" — same polarity, different plugin
    a = _tokens("Synology Chat ships as a bundled plugin, so builds do not need a separate install.")
    b = _tokens("Microsoft Teams ships as a bundled plugin, so no separate install is required.")
    assert _polarity_flip(a, b) is False


def test_no_polarity_flip_on_symmetric_negation():
    # both have exactly one negation -> same net parity, no object antonyms -> no flip
    a = _tokens("The cache must not be cleared on startup.")
    b = _tokens("The buffer must not be flushed on startup.")
    assert _polarity_flip(a, b) is False


def test_polarity_flip_on_odd_vs_even_negation():
    # 1 negation vs 2 negations -> odd/even parity differs -> net opposite polarity
    a = _tokens("Allowlisting does not disable mention gating.")
    b = _tokens("Mention gating cannot be enforced without configured patterns.")
    assert _polarity_flip(a, b) is True


def test_polarity_flip_omission_verb():
    # Regression (smoke-test #7, 2026-06-27): "Skip X" asserts the action is NOT
    # done, so vs "Do X" it is a polarity flip — but "skip" carries no _NEG token and
    # the bare verb isn't an object-antonym, so the pre-fix gate returned False and
    # silently dropped a real contradiction NLI scored 1.0. The _OMISSION lexicon
    # now counts it in the parity bucket.
    a = _tokens("Skip smoke-testing the Ergo wiring before relying on it.")
    b = _tokens("Smoke-test the Ergo wiring before relying on it.")
    assert _polarity_flip(a, b) is True


# --- integration: the judge with gates -----------------------------------
def _claim(text):
    return {"text": text, "norm": RuleNormalizer().normalize({"text": text})}


def _coarse(text, subject, modality, obj):
    # force a coarse norm like the LLM produced (to reproduce the real FPs)
    return {"text": text, "norm": {
        "subject": subject, "modality": modality, "object": obj,
        "scope": {"env": None, "service_class": None, "region": None, "version": None},
        "valid_from": None, "valid_until": None, "exception": None,
    }}


def test_gate1_suppresses_boilerplate():
    j = ContradictionJudge(RuleNormalizer())
    a = _coarse("Synology Chat ships as a bundled plugin, so builds do not need a separate install.",
                "bundled plugin", "may", "skipped")
    b = _coarse("Microsoft Teams ships as a bundled plugin, so no separate install is required.",
                "bundled plugin", "may", "used")
    r = j.verdict(a, b)
    assert r.label == "neutral"
    assert r.tier == "log"
    assert r.source == "polarity-gate"


def test_gate2_downgrades_aspect_collision():
    j = ContradictionJudge(RuleNormalizer())  # no NLI -> low-overlap flip becomes warn
    a = _coarse("Paths must not contain null bytes and must be shorter than 4096 characters.",
                "paths", "must_not", "allowed")
    b = _coarse("Paths are resolved relative to the including file but must stay inside the config directory.",
                "paths", "must", "allowed")
    r = j.verdict(a, b)
    # genuine polarity flip (not vs no-not) but near-zero predicate overlap -> not a hard block
    assert r.tier in ("warn", "log")
    assert r.label != "contradiction" or r.tier == "warn"
    assert r.source == "aspect-gate"


def test_gate2_with_nli_low_score_logs_neutral():
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.2))
    a = _coarse("Paths must not contain null bytes and must be shorter than 4096 characters.",
                "paths", "must_not", "allowed")
    b = _coarse("Paths are resolved relative to the including file but must stay inside the config directory.",
                "paths", "must", "allowed")
    r = j.verdict(a, b)
    assert r.label == "neutral"
    assert r.tier == "log"
    assert r.source == "aspect-gate"


def test_gate2_with_nli_high_score_warns_not_blocks():
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.9))
    a = _coarse("Paths must not contain null bytes and must be shorter than 4096 characters.",
                "paths", "must_not", "allowed")
    b = _coarse("Paths are resolved relative to the including file but must stay inside the config directory.",
                "paths", "must", "allowed")
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "warn"
    assert r.source == "aspect-gate"


def test_real_contradiction_still_blocks():
    j = ContradictionJudge(RuleNormalizer())
    a = _coarse("TLS 1.2 must be enabled for public-facing services in prod.",
                "tls 1.2", "must", "enabled")
    b = _coarse("TLS 1.2 must not be enabled for public-facing services in prod.",
                "tls 1.2", "must_not", "enabled")
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "block"


# --- NLI-residual gating (2026-06-26 fixes) ----------------------------------
# History: the NLI-residual path was UNGATED -> 270 false blocks on a large
# real-world document ingest. First fix added the polarity/aspect gates. The 2026-06-26 evening fixes
# (calibrated on evals/prose/dataset.jsonl) added:
#   Fix #1: a MISSING subject (extractor returned nothing) is dropped, never sent
#           to noisy NLI (distinct from a fuzzy-subject UNKNOWN, which IS).
#   Fix #2: a polarity flip alone is not enough -> the opposition must be on SHARED
#           content (overlap >= NLI_OVERLAP_FLOOR); a bare stray-'not' on unrelated
#           text is dropped. Result on the prose eval: P 0.42 -> 0.96, R 0.70 ->
#           0.73, 0 false hard-blocks.
class _FakeNLI:
    """Deterministic stand-in: always reports a block-worthy contradiction score,
    so the test isolates the GATE behavior, not the NLI model's judgment."""
    def __init__(self, score: float = 0.9):
        self.score = score

    def contradiction_score(self, a: str, b: str) -> float:
        return self.score


def _fuzzy(text, subj):
    # subject PRESENT but only fuzzily matching the partner -> structural UNKNOWN
    # -> the NLI residual path (what these tests exercise). subjects 'alpha beta
    # gamma'/'alpha beta delta' overlap 0.5 -> UNKNOWN (not SAME, not MISSING).
    return {"text": text, "norm": {
        "subject": subj, "modality": "must", "object": "enabled",
        "scope": {"env": None, "service_class": None, "region": None, "version": None},
        "valid_from": None, "valid_until": None, "exception": None,
    }}


SUBJ_A, SUBJ_B = "alpha beta gamma", "alpha beta delta"   # jaccard 0.5 -> UNKNOWN


def _missing(text):
    # subject absent -> structural MISSING -> dropped, NLI never consulted (Fix #1).
    return {"text": text, "norm": {
        "subject": None, "modality": "must", "object": "enabled",
        "scope": {"env": None, "service_class": None, "region": None, "version": None},
        "valid_from": None, "valid_until": None, "exception": None,
    }}


def test_fix1_missing_subject_never_reaches_nli():
    # Even with NLI screaming 0.99, a missing subject must NOT be flagged: it's not
    # comparable, and we refuse to hand it to noisy prose-NLI.
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.99))
    a = _missing("Production backups must be encrypted at rest.")
    b = _missing("Production backups must not be encrypted at rest.")
    r = j.verdict(a, b)
    assert r.label == "neutral"
    assert r.tier == "log"
    assert r.source == "structural"   # decided structurally, NLI never ran


def test_nli_block_requires_polarity_flip():
    # NLI screams contradiction (0.9) but the two templated boilerplate sentences
    # have NO opposing polarity -> must demote to silent log, never hard-block.
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.9))
    a = _fuzzy("Twitch ships as a bundled plugin in current Acme releases.", SUBJ_A)
    b = _fuzzy("Microsoft Teams ships as a bundled plugin in current Acme releases.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.tier == "log"
    assert r.label == "neutral"
    assert "polarity-gate" in r.source


def test_nli_block_survives_with_polarity_flip_and_overlap():
    # Genuine opposing claim the extractor failed to structure: polarity flip +
    # high predicate overlap -> NLI is allowed to block.
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.9))
    a = _fuzzy("Telemetry must be enabled for all services.", SUBJ_A)
    b = _fuzzy("Telemetry must not be enabled for all services.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "block"


def test_omission_verb_survives_gate_and_blocks():
    # Regression (smoke-test #7): fuzzy subject -> structural defers to NLI; NLI
    # screams contradiction; the only signal of opposition is the omission verb
    # "skip" in the text. Pre-fix the polarity gate vetoed it (no _NEG / antonym);
    # post-fix the _OMISSION parity flip + shared predicate overlap lets NLI block.
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.95))
    a = _fuzzy("Skip smoke-testing the deploy pipeline before every release.", SUBJ_A)
    b = _fuzzy("Smoke-test the deploy pipeline before every release.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "block"
    assert r.source == "nli"


def test_omission_verb_on_unrelated_text_still_dropped():
    # The _OMISSION addition must NOT manufacture flags: an omission verb with NO
    # shared predicate stays below NLI_OVERLAP_FLOOR and is dropped (precision guard).
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.95))
    a = _fuzzy("Skip the nightly cache warm-up on staging.", SUBJ_A)
    b = _fuzzy("Hostnames should be lowercased before DNS lookup.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.tier == "log"
    assert r.label == "neutral"


def test_fix2_low_overlap_flip_is_dropped():
    # Polarity flip present (stray 'not') but the two claims share NO content ->
    # dropped to log, not a warn (the Fix #2 FP killer). Pre-fix this was a warn.
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.9))
    a = _fuzzy("Paths must not contain null bytes.", SUBJ_A)
    b = _fuzzy("Hostnames should be lowercased before lookup.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.tier == "log"
    assert r.label == "neutral"


def test_high_nli_moderate_overlap_is_warn_not_block():
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.9))
    a = _fuzzy("Telemetry must not run in prod.", SUBJ_A)
    b = _fuzzy("Telemetry should run in staging.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "warn"
    assert r.source == "nli+aspect-gate"


def test_mid_band_nli_warn_requires_opposition_and_overlap():
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.4))
    a = _fuzzy("Telemetry must not run in prod.", SUBJ_A)
    b = _fuzzy("Telemetry should run in staging.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "warn"
    assert r.source == "nli"


def test_mid_band_nli_without_opposition_is_logged_neutral():
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.4))
    a = _fuzzy("Telemetry should run in prod.", SUBJ_A)
    b = _fuzzy("Telemetry may run in staging.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.label == "neutral"
    assert r.tier == "log"
    assert r.source == "nli"


def test_low_nli_score_is_logged_neutral():
    j = ContradictionJudge(RuleNormalizer(), nli=_FakeNLI(0.1))
    a = _fuzzy("Telemetry must not run in prod.", SUBJ_A)
    b = _fuzzy("Telemetry should run in staging.", SUBJ_B)
    r = j.verdict(a, b)
    assert r.label == "neutral"
    assert r.tier == "log"
    assert r.source == "nli"


def test_call_returns_label_and_score():
    j = ContradictionJudge(RuleNormalizer())
    label, score = j(_claim("TLS must be enabled in prod."),
                     _claim("TLS must be enabled in prod."))
    assert label == "neutral"
    assert isinstance(score, float)
