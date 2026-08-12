#!/usr/bin/env python3
"""Judge-agnostic evaluation harness for the contradiction eval set.

Give it any judge:  judge_fn(claim_a: dict, claim_b: dict) -> (label: str, score: float)
where label is "contradiction" or anything else (treated as "not contradiction").

Reports precision / recall / F1 against the gold set, a confusion matrix, and
breakdowns by category and difficulty. Ships a deliberately-dumb LEXICAL baseline so
this runs today with zero models — and the baseline's failure on hard scope-coexist
pairs is itself the argument for the normalize-first design.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATASET = HERE / "dataset.jsonl"


def load(path: Path = DATASET) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# --- baseline judge: raw-text antonymy + subject-token overlap, scope-BLIND -----
# Intentionally naive. It flags opposing modality on the same topic regardless of
# scope/time -> it WILL fire on the coexist traps. That is the point.
NEG_MARKERS = ("must not", "should not", "may not", "prohibited", "forbidden",
               "disallowed", "must be turned off", "must be disabled", "removed")

def _polarity(text: str) -> int:
    t = text.lower()
    return -1 if any(m in t for m in NEG_MARKERS) else +1

def _content_tokens(text: str) -> set[str]:
    stop = {"the", "a", "an", "be", "is", "are", "must", "not", "should", "may",
            "in", "for", "of", "to", "on", "and", "or", "services", "all", "from",
            "until", "with", "by", "their", "its", "this", "that"}
    return {w.strip(".,/").lower() for w in text.split()} - stop

def baseline_judge(a: dict, b: dict):
    ta, tb = a["text"], b["text"]
    overlap = _content_tokens(ta) & _content_tokens(tb)
    same_topic = len(overlap) >= 2
    opposing = _polarity(ta) != _polarity(tb)
    if same_topic and opposing:
        return "contradiction", 0.6
    return "neutral", 0.4


def evaluate(judge_fn, data: list[dict] | None = None) -> dict:
    data = data or load()
    tp = fp = fn = tn = 0
    by_cat = defaultdict(lambda: [0, 0, 0, 0])   # tp, fp, fn, tn
    by_diff = defaultdict(lambda: [0, 0, 0, 0])
    fp_examples, fn_examples = [], []

    for r in data:
        gold_pos = (r["label"] == "contradiction")
        pred_label, _ = judge_fn(r["claim_a"], r["claim_b"])
        pred_pos = (pred_label == "contradiction")
        if pred_pos and gold_pos:
            slot = 0; tp += 1
        elif pred_pos and not gold_pos:
            slot = 1; fp += 1
            if len(fp_examples) < 8: fp_examples.append(r)
        elif not pred_pos and gold_pos:
            slot = 2; fn += 1
            if len(fn_examples) < 8: fn_examples.append(r)
        else:
            slot = 3; tn += 1
        by_cat[r["category"]][slot] += 1
        by_diff[r["difficulty"]][slot] += 1

    def prf(tp, fp, fn):
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return p, r, f

    p, r, f = prf(tp, fp, fn)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": p, "recall": r,
            "f1": f, "by_cat": dict(by_cat), "by_diff": dict(by_diff),
            "fp_examples": fp_examples, "fn_examples": fn_examples, "n": len(data)}


def _report(res: dict, name: str):
    print(f"\n=== {name} ===  (n={res['n']})")
    print(f"precision={res['precision']:.3f}  recall={res['recall']:.3f}  f1={res['f1']:.3f}")
    print(f"confusion: TP={res['tp']} FP={res['fp']} FN={res['fn']} TN={res['tn']}")
    print("by difficulty (tp/fp/fn/tn):")
    for d, (tp, fp, fn, tn) in sorted(res["by_diff"].items()):
        print(f"   {d:<5} tp={tp} fp={fp} fn={fn} tn={tn}")
    print("by category (tp/fp/fn/tn):")
    for c, (tp, fp, fn, tn) in sorted(res["by_cat"].items()):
        print(f"   {c:<32} tp={tp} fp={fp} fn={fn} tn={tn}")
    if res["fp_examples"]:
        print("sample FALSE POSITIVES (flagged but should coexist):")
        for r in res["fp_examples"][:4]:
            print(f"   [{r['label']}/{r['category']}] {r['claim_a']['text']}  ||  {r['claim_b']['text']}")


def _summary_row(name, res):
    return (f"{name:<26} P={res['precision']:.3f} R={res['recall']:.3f} "
            f"F1={res['f1']:.3f}  (TP={res['tp']} FP={res['fp']} FN={res['fn']} TN={res['tn']})")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE.parent.parent))  # repo root, to import `ergo`
    from ergo.contradiction import GoldNormalizer, RuleNormalizer, ContradictionJudge

    data = load()
    rows = []

    base = evaluate(baseline_judge, data); rows.append(("lexical baseline", base))
    gold_struct = evaluate(ContradictionJudge(GoldNormalizer()), data)
    rows.append(("structural (gold norm)", gold_struct))
    rule_struct = evaluate(ContradictionJudge(RuleNormalizer()), data)
    rows.append(("structural (rule norm)", rule_struct))

    nli = None
    try:
        from ergo.contradiction.nli import NLIJudge
        nli = NLIJudge()
    except Exception as e:
        print(f"[NLI unavailable: {type(e).__name__}: {e}]")

    if nli is not None:
        nli_only = evaluate(lambda a, b: ("contradiction" if nli.contradiction_score(a["text"], b["text"]) >= 0.5 else "neutral",
                                          nli.contradiction_score(a["text"], b["text"])), data)
        rows.append(("NLI only (deberta-v3)", nli_only))
        hybrid_gold = evaluate(ContradictionJudge(GoldNormalizer(), nli=nli), data)
        rows.append(("hybrid (gold + NLI)", hybrid_gold))
        hybrid_rule = evaluate(ContradictionJudge(RuleNormalizer(), nli=nli), data)
        rows.append(("hybrid (rule + NLI)", hybrid_rule))

    print("\n========== COMPARISON (n={}) ==========".format(len(data)))
    for name, res in rows:
        print(_summary_row(name, res))

    # detailed view on the two most informative judges
    _report(base, "lexical baseline (scope-blind)")
    _report(rule_struct, "structural (rule norm)")
    if nli is not None:
        _report(rows[-1][1], "hybrid (rule + NLI)")
