#!/usr/bin/env python3
"""Measure /why's precision against a labeled query -> expected-claim set (private-repo issue #209).

Rebuilds an in-memory ContradictionStore from the checked-in synthetic
corpus (corpus_synthetic.jsonl) using the same fastembed embedder the
engine uses, then runs the actual ContradictionStore.why() against it --
not a live server, so this is fully offline and reproducible from the two
files checked into this eval.

Matching is by exact claim STATEMENT text, not id: remember_fact() assigns a
fresh id to every row on each rebuild, so statement text is the only stable
join key between the dataset's `expected_statement` and a fresh why() result.

Usage:
    python evals/why_precision/evaluate.py [--errors] [--data PATH] [--corpus PATH]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from ergo.contradiction.store import ContradictionStore  # noqa: E402
from ergo.embedders import get_embedder  # noqa: E402

ORG_ID = "claude-code"


def build_store(corpus_path: Path) -> ContradictionStore:
    rows = [json.loads(l) for l in corpus_path.read_text().splitlines() if l.strip()]
    store = ContradictionStore(":memory:", embedder=get_embedder())
    for r in rows:
        store.remember_fact(ORG_ID, r["project"], "claude-code", r["statement"],
                             reason=r.get("reason"))
    return store


def classify(row: dict, result: dict | None) -> str:
    """suppressed | match | wrong -- see README for the taxonomy.

    store.why() returns None (no reasoned claim at all), {"suppressed": {...}}
    (a top hit exists but scores below WHY_RELEVANCE_FLOOR), or a flat
    {"claim", "reason", "score", "history"} dict -- see store.py:why().
    """
    if result is None or "suppressed" in result:
        return "suppressed"
    if row["expected_type"] == "true_negative":
        return "wrong"
    if result["claim"] == row["expected_statement"]:
        return "match"
    return "wrong"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "dataset_synthetic.jsonl"))
    ap.add_argument("--corpus", default=str(HERE / "corpus_synthetic.jsonl"))
    ap.add_argument("--errors", action="store_true", help="print every wrong/suppressed case")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.data).read_text().splitlines() if l.strip()]
    store = build_store(Path(args.corpus))

    results = []
    for row in rows:
        out = store.why(ORG_ID, row["project"], row["query"])
        verdict = classify(row, out)
        score = None
        if out is not None:
            score = out["suppressed"]["score"] if "suppressed" in out else out["score"]
        results.append((row, verdict, score, out))

    total = len(results)
    by_verdict = {"match": [], "suppressed": [], "wrong": []}
    for row, verdict, score, out in results:
        by_verdict[verdict].append((row, score, out))

    wrong_rate = len(by_verdict["wrong"]) / total if total else 0.0

    print(f"rows: {total}  (excluded as uncertain: 0 -- every row was defensibly "
          f"labeled 'claim' or 'true_negative', see rationale field)\n")
    print(f"  MATCH      : {len(by_verdict['match'])}")
    print(f"  SUPPRESSED : {len(by_verdict['suppressed'])}  (correct behavior for "
          f"true_negative rows; a missed answer for 'claim' rows)")
    print(f"  WRONG      : {len(by_verdict['wrong'])}  <- confident wrong answer, "
          f"the dangerous case private-repo issue #209 is about")
    print(f"\n  WRONG-ANSWER RATE: {wrong_rate:.3f} ({len(by_verdict['wrong'])}/{total})\n")

    print("  by phrasing category:")
    cats = sorted({r["phrasing"] for r, _, _, _ in results})
    for cat in cats:
        cat_rows = [(r, v) for r, v, _, _ in results if r["phrasing"] == cat]
        cat_wrong = sum(1 for r, v in cat_rows if v == "wrong")
        print(f"    {cat:15} n={len(cat_rows):2}  wrong={cat_wrong:2}  "
              f"rate={cat_wrong/len(cat_rows):.3f}")

    def fmt_scores(bucket):
        scores = sorted(s for _, s, _ in by_verdict[bucket] if s is not None)
        if not scores:
            return "n/a"
        return f"min={scores[0]:.4f} median={scores[len(scores)//2]:.4f} max={scores[-1]:.4f}"

    print("\n  score distribution:")
    print(f"    match      : {fmt_scores('match')}")
    print(f"    suppressed : {fmt_scores('suppressed')}")
    print(f"    wrong      : {fmt_scores('wrong')}")

    if args.errors:
        print("\n===== WRONG / SUPPRESSED DETAIL =====")
        for row, verdict, score, out in results:
            if verdict == "match":
                continue
            print(f"\n[{verdict}] id={row['id']} phrasing={row['phrasing']} "
                  f"project={row['project']} score={score}")
            print(f"   query   : {row['query']}")
            print(f"   expected: {row['expected_statement']}")
            if out is not None and "suppressed" not in out:
                print(f"   got     : {out['claim'][:200]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
