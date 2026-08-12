#!/usr/bin/env python3
"""Measure the contradiction judge against the labeled PROSE eval set.

Normalizes each pair with the configured LLM normalizer (cached), runs the full
judge (structural -> gates -> NLI), and scores predictions vs gold labels.

A prediction is POSITIVE if the judge returns label=="contradiction" (block OR
warn — both "surface" a flag to the user). We also report block-only precision,
since a false *block* (stops a write) is the most damaging failure.

Usage (reuse the litellm norm cache so harvested texts are free):
    ERGO_LLM_PROVIDER=litellm ERGO_LLM_MODEL=claude-haiku \
    ERGO_LLM_CACHE=evals/contradiction/.cache/llm_norm_litellm_haiku.json \
    OPENAI_BASE_URL=http://litellm.internal:4000/v1 OPENAI_API_KEY=$KEY \
    python evals/prose/evaluate.py [--no-nli] [--errors]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from ergo.contradiction.normalize import LLMNormalizer  # noqa: E402
from ergo.contradiction.judge import ContradictionJudge  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-nli", action="store_true", help="structural only (no NLI)")
    ap.add_argument("--errors", action="store_true", help="print every misclassified pair")
    ap.add_argument("--data", default=str(HERE / "dataset.jsonl"))
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.data).read_text().splitlines() if l.strip()]
    norm = LLMNormalizer()
    nli = None
    if not args.no_nli:
        from ergo.contradiction.nli import NLIJudge
        nli = NLIJudge()
    judge = ContradictionJudge(norm, nli=nli)

    def claim(text):
        return {"text": text, "norm": norm.normalize({"text": text})}

    tp = fp = fn = tn = 0
    block_fp = 0          # not_contradiction predicted as a hard BLOCK (worst)
    errors, by_tag = [], {}
    for r in rows:
        v = judge.verdict(claim(r["a"]), claim(r["b"]))
        pred_pos = (v.label == "contradiction")           # block or warn
        gold_pos = (r["label"] == "contradiction")
        fam = r["tag"].split("/")[0]
        by_tag.setdefault(fam, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
        if gold_pos and pred_pos:
            tp += 1; by_tag[fam]["tp"] += 1
        elif (not gold_pos) and pred_pos:
            fp += 1; by_tag[fam]["fp"] += 1
            if v.tier == "block":
                block_fp += 1
            errors.append(("FP", r, v))
        elif gold_pos and (not pred_pos):
            fn += 1; by_tag[fam]["fn"] += 1
            errors.append(("FN", r, v))
        else:
            tn += 1; by_tag[fam]["tn"] += 1

    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    print(f"config: {'structural-only' if args.no_nli else 'structural + NLI'}  "
          f"normalizer={type(norm).__name__}({getattr(norm,'model','?')})")
    print(f"pairs: {len(rows)}  (pos={tp+fn}  neg={fp+tn})\n")
    print(f"  TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  precision (flag) : {prec:.3f}")
    print(f"  recall    (flag) : {rec:.3f}")
    print(f"  F1               : {f1:.3f}")
    print(f"  false HARD blocks: {block_fp}   (the write-stoppers — want 0)")
    print("\n  per-family (fp/fn are the errors):")
    for fam, d in sorted(by_tag.items()):
        print(f"    {fam:12} tp={d['tp']:2} fp={d['fp']:2} fn={d['fn']:2} tn={d['tn']:2}")

    if args.errors:
        print("\n===== MISCLASSIFIED =====")
        for kind, r, v in errors:
            print(f"\n[{kind}] gold={r['label']}  tag={r['tag']}  tier={v.tier} src={v.source}")
            print(f"   reason: {v.reason}")
            print(f"   A: {r['a'][:110]}")
            print(f"   B: {r['b'][:110]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
