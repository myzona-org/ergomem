"""Regression tests for the normalizer value-loss bug (2026-06-27).

THE BUG (proven via MCP live verification):
  "must use a blue canary"  -> norm object "used" (lost "blue canary")
  "must use a red canary"   -> norm object "used"  (identical norm)
Both were accepted as non-contradictory because the normalizer collapsed the
colour adjective into the same canonical object verb, so the structural judge
compared two identical norms and the contradiction gate never had a chance to
fire.

THE FIX:
  - `norm` gains a `value` field carrying the value-bearing object selector
    (colour/port/version/identifier) that differentiates otherwise-identical
    prescriptions. The prompt instructs the LLM to keep the subject shared and
    push the distinguisher into `value`; `_coerce` now preserves it (pre-fix the
    schema had no such key, so even an LLM that emitted "blue" had it dropped).
  - The structural comparator flags same-subject / same-stance / overlapping
    scope+time claims whose `value` disagrees as a value_conflict contradiction.
  - The judge honours value_conflict verdicts (no polarity flip exists for blue
    vs red) after confirming the values are grounded in the raw text.
"""
from __future__ import annotations
import json

import pytest

from ergo.contradiction.normalize import (
    _LLMNormalizerBase, RuleNormalizer,
)
from ergo.contradiction.structural import compare
from ergo.contradiction.judge import ContradictionJudge, _value_grounded


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _norm(subject, modality="must", obj="used", value=None,
          env=None, service_class=None, region=None, version=None,
          valid_from=None, valid_until=None, exception=None):
    return {
        "subject": subject, "modality": modality, "object": obj, "value": value,
        "scope": {"env": env, "service_class": service_class,
                  "region": region, "version": version},
        "valid_from": valid_from, "valid_until": valid_until, "exception": exception,
    }


class _StubNormalizer(_LLMNormalizerBase):
    """Simulates a correctly-prompted instruct-LLM WITHOUT touching the network.

    It mirrors what the production prompt now elicits: a shared subject + the
    distinguishing colour/port pushed into `value`. The point of the test is that
    the full _extract -> _coerce path PRESERVES that distinction (pre-fix `_coerce`
    had no `value` key and silently dropped it, re-collapsing the two norms)."""
    PROVIDER = "stub"

    def _chat(self, messages: list[dict]) -> str:
        text = messages[-1]["content"].lower()
        colour = next((c for c in ("blue", "red", "green", "black", "amber")
                       if c in text), None)
        port = None
        for tok in text.replace(".", " ").split():
            if tok.isdigit():
                port = tok
                break
        return json.dumps({
            "subject": "canary",
            "modality": "must",
            "object": "used",
            "value": colour or port,
            "scope": {"env": None, "service_class": None, "region": None, "version": None},
            "valid_from": None, "valid_until": None, "exception": None,
        })


# --------------------------------------------------------------------------
# 1. Normalizer: the headline regression — distinct normalized objects
# --------------------------------------------------------------------------
def test_blue_vs_red_do_not_produce_identical_normalized_objects(tmp_path):
    n = _StubNormalizer(model="stub", cache_path=str(tmp_path / "cache.json"))
    blue = n.normalize({"text": "Deploys must use a blue canary."})
    red = n.normalize({"text": "Deploys must use a red canary."})
    # The whole norm must differ — this is the bug condition stated literally.
    assert blue != red, "blue/red canary collapsed to identical norms (the bug)"
    # And specifically the value-bearing field is what differentiates them.
    assert blue["value"] == "blue"
    assert red["value"] == "red"
    assert blue["value"] != red["value"]
    # Subject stays shared so the structural comparator can find the shared topic.
    assert blue["subject"] == red["subject"] == "canary"


def test_coerce_preserves_value_field():
    # The parser half in isolation: even when the LLM dutifully emits the
    # canonical object verb "used", the distinguishing `value` survives _coerce.
    blue = _LLMNormalizerBase._coerce(
        {"subject": "canary", "modality": "must", "object": "used", "value": "Blue ",
         "scope": {}, "valid_from": None, "valid_until": None, "exception": None})
    red = _LLMNormalizerBase._coerce(
        {"subject": "canary", "modality": "must", "object": "used", "value": "RED",
         "scope": {}, "valid_from": None, "valid_until": None, "exception": None})
    assert blue["value"] == "blue"   # lowercased + stripped
    assert red["value"] == "red"
    assert blue != red


def test_coerce_value_absent_is_none():
    # Backwards-compat: an LLM/payload with no `value` key yields value=None,
    # never a crash — so old persisted norms degrade gracefully.
    n = _LLMNormalizerBase._coerce(
        {"subject": "tls 1.2", "modality": "must", "object": "enabled", "scope": {}})
    assert n["value"] is None


def test_rule_normalizer_emits_value_key():
    n = RuleNormalizer().normalize({"text": "The gateway must bind to port 8443."})
    assert "value" in n
    assert n["value"] == "8443"


# --------------------------------------------------------------------------
# 2. Structural comparator: shared subject + opposing detail -> contradiction
# --------------------------------------------------------------------------
def test_structural_blue_vs_red_is_value_conflict():
    a = _norm("canary", obj="used", value="blue")
    b = _norm("canary", obj="used", value="red")
    v = compare(a, b)
    assert v.label == "contradiction"
    assert v.value_conflict is True
    assert v.confidence == "HIGH"   # must vs must -> hard block tier


def test_structural_port_8443_vs_9090_is_value_conflict():
    a = _norm("gateway", obj="used", value="8443")
    b = _norm("gateway", obj="used", value="9090")
    v = compare(a, b)
    assert v.label == "contradiction"
    assert v.value_conflict is True


def test_structural_same_value_is_not_a_conflict():
    # Identical prescriptions -> agreement, not a value conflict.
    a = _norm("canary", obj="used", value="blue")
    b = _norm("canary", obj="used", value="blue")
    v = compare(a, b)
    assert v.label == "entailment"
    assert v.value_conflict is False


def test_structural_value_on_one_side_only_is_not_a_conflict():
    # "must use a canary" (generic) vs "must use a blue canary" (specific) are
    # compatible — the generic doesn't exclude the specific. Require BOTH non-null.
    a = _norm("canary", obj="used", value=None)
    b = _norm("canary", obj="used", value="blue")
    v = compare(a, b)
    assert v.value_conflict is False
    assert v.label != "contradiction"


def test_structural_value_conflict_respects_disjoint_scope():
    # Different envs -> the two value choices coexist (blue in dev, red in prod).
    a = _norm("canary", obj="used", value="blue", env="dev")
    b = _norm("canary", obj="used", value="red", env="prod")
    v = compare(a, b)
    assert v.label == "coexist"
    assert v.value_conflict is False


def test_structural_multiword_noisy_value_does_not_fire():
    # Precision guard: a multi-word extractor-noise "value" (the zalo dmPolicy doc
    # FP: "stable zalo user ids" vs "open") is NOT an atomic selector, so the gate
    # must NOT fire — config-enumeration docs shouldn't read as contradictions.
    a = _norm("dmpolicy", obj="used", value="stable zalo user ids")
    b = _norm("dmpolicy", obj="used", value="open")
    v = compare(a, b)
    assert v.value_conflict is False
    assert v.label != "contradiction"


def test_structural_value_conflict_requires_determined_stance():
    # If either side's stance is undetermined (object not in the polarity enum),
    # the two claims aren't comparable same-direction prescriptions -> no fire.
    a = _norm("canary", obj="frobnicated", value="blue")   # unknown object -> stance None
    b = _norm("canary", obj="used", value="red")
    v = compare(a, b)
    assert v.value_conflict is False


def test_structural_softer_modality_value_conflict_is_not_high():
    a = _norm("canary", modality="should", obj="used", value="blue")
    b = _norm("canary", modality="should", obj="used", value="red")
    v = compare(a, b)
    assert v.label == "contradiction"
    assert v.value_conflict is True
    assert v.confidence != "HIGH"   # soft-vs-soft -> warn tier, not a hard block


# --------------------------------------------------------------------------
# 3. _value_grounded precision guard
# --------------------------------------------------------------------------
def test_value_grounded_true_for_real_tokens():
    aw = "deploys must use a blue canary".split()
    bw = "deploys must use a red canary".split()
    assert _value_grounded("blue", "red", aw, bw) is True


def test_value_grounded_false_when_hallucinated():
    # normalizer claims a colour neither sentence contains -> not grounded.
    aw = "the service must use the primary cluster".split()
    bw = "the service must use the secondary cluster".split()
    assert _value_grounded("blue", "red", aw, bw) is False


# --------------------------------------------------------------------------
# 4. End-to-end through the judge: the blue/red pair now blocks
# --------------------------------------------------------------------------
def _claim(text, value):
    return {"text": text, "norm": _norm("canary", obj="used", value=value)}


def test_judge_blocks_blue_vs_red_canary():
    j = ContradictionJudge(RuleNormalizer())
    a = _claim("Deploys must use a blue canary.", "blue")
    b = _claim("Deploys must use a red canary.", "red")
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "block"
    assert r.source == "structural-value"


def test_judge_blocks_port_value_conflict():
    j = ContradictionJudge(RuleNormalizer())
    a = {"text": "The gateway must bind to port 8443.",
         "norm": _norm("gateway", obj="used", value="8443")}
    b = {"text": "The gateway must bind to port 9090.",
         "norm": _norm("gateway", obj="used", value="9090")}
    r = j.verdict(a, b)
    assert r.label == "contradiction"
    assert r.tier == "block"


def test_judge_drops_value_conflict_not_grounded_in_text():
    # Structural sees a value conflict from the norms, but the colours are NOT in
    # the raw text (hallucinated extraction) -> the value-gate demotes to log.
    j = ContradictionJudge(RuleNormalizer())
    a = {"text": "The service must use the primary cluster.",
         "norm": _norm("cluster", obj="used", value="blue")}
    b = {"text": "The service must use the secondary cluster.",
         "norm": _norm("cluster", obj="used", value="red")}
    r = j.verdict(a, b)
    assert r.label == "neutral"
    assert r.tier == "log"
    assert r.source == "value-gate"


def test_judge_same_value_does_not_block():
    j = ContradictionJudge(RuleNormalizer())
    a = _claim("Deploys must use a blue canary.", "blue")
    b = _claim("Deploys must use a blue canary.", "blue")
    r = j.verdict(a, b)
    assert r.label != "contradiction"
