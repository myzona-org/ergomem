#!/usr/bin/env python3
"""Benchmark the LLMNormalizer (Ollama instruct extractor) against gold/rule norms.

The LLM extractor is the real 'normalize' role — it should match
the gold-norm ceiling on prose the RuleNormalizer mangles. Extractions are cached by
(model, text) in evals/contradiction/.cache/, so this is RESUMABLE: run it against a
small sample now, or the full set off-peak when the shared GPU box is free.

Usage:
    python evals/contradiction/run_llm_norm.py [--n N] [--model NAME] [--with-nli]

Env: ERGO_LLM_MODEL, ERGO_LLM_BASE (default a local LAN Ollama host), ERGO_LLM_CACHE.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from ergo.contradiction import (GoldNormalizer, RuleNormalizer, LLMNormalizer,  # noqa: E402
                                ContradictionJudge)
from evals.contradiction.evaluate import evaluate, _summary_row, load  # noqa: E402


def stratified_sample(data, n):
    by_label = {}
    for r in data:
        by_label.setdefault(r["label"], []).append(r)
    out, i = [], 0
    # round-robin across labels for balance
    while len(out) < n and any(by_label.values()):
        for lbl in list(by_label):
            if by_label[lbl]:
                out.append(by_label[lbl].pop())
                if len(out) >= n:
                    break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40, help="sample size (0 = full set)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--with-nli", action="store_true")
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args()

    data = load()
    sample = data if args.n == 0 else stratified_sample(data, args.n)
    print(f"sample={len(sample)} pairs  model={args.model or 'env-default'}")

    llm = LLMNormalizer(model=args.model) if args.model else LLMNormalizer()

    # warm the cache, with retries + coverage accounting (don't let one timeout kill it)
    ok = fail = 0
    t0 = time.time()
    claims = {}
    for r in sample:
        for side in ("claim_a", "claim_b"):
            claims[r[side]["text"]] = r[side]
    for i, (text, claim) in enumerate(claims.items(), 1):
        for attempt in range(args.retries + 1):
            try:
                llm.normalize(claim)
                ok += 1
                break
            except Exception as e:
                if attempt == args.retries:
                    fail += 1
                    print(f"  [{i}/{len(claims)}] FAIL {type(e).__name__}: {str(e)[:60]}")
        if i % 10 == 0:
            print(f"  extracted {i}/{len(claims)}  ok={ok} fail={fail}  "
                  f"({(time.time()-t0)/i:.1f}s/claim)", flush=True)
    print(f"extraction coverage: ok={ok} fail={fail} of {len(claims)} unique claims "
          f"in {time.time()-t0:.0f}s")
    if fail:
        print("  (failed claims fall back to error -> excluded; re-run to resume via cache)")

    nli = None
    if args.with_nli:
        from ergo.contradiction.nli import NLIJudge
        nli = NLIJudge()

    rows = [
        ("gold norm", evaluate(ContradictionJudge(GoldNormalizer(), nli=nli), sample)),
        ("rule norm", evaluate(ContradictionJudge(RuleNormalizer(), nli=nli), sample)),
        ("LLM norm", evaluate(ContradictionJudge(llm, nli=nli), sample)),
    ]
    tag = " + NLI" if nli else " (structural only)"
    print(f"\n===== LLM-NORMALIZER BENCHMARK{tag}  (n={len(sample)}) =====")
    for name, res in rows:
        print(_summary_row(name, res))


if __name__ == "__main__":
    main()
