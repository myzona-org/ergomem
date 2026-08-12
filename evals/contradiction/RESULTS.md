# Contradiction Engine — Eval Results

*Date: 2026-06-25. Dataset: 270 synthetic "Helix" pairs (108 contradiction / 92
hard-coexist / 40 entailment / 30 unrelated). Model: Xenova/nli-deberta-v3-base
(ONNX, quantized) via onnxruntime — no torch. Run: `python evals/contradiction/evaluate.py`.*

## Headline

| Judge | Precision | Recall | F1 | FP | FN |
|---|---|---|---|---|---|
| lexical baseline (scope-blind) | 0.596 | 0.981 | 0.741 | 72 | 2 |
| NLI only (deberta-v3) | 0.581 | 1.000 | 0.735 | 78 | 0 |
| structural (rule norm) | 0.990 | 0.963 | 0.977 | 1 | 4 |
| structural (gold norm) | 1.000 | 0.981 | 0.991 | 0 | 2 |
| hybrid (rule norm + NLI) | 0.991 | 0.981 | 0.986 | 1 | 2 |
| **hybrid (gold norm + NLI)** | **1.000** | **1.000** | **1.000** | 0 | 0 |

## What it proves (the architecture bet)

1. **NLI alone is no better than the dumb baseline (P=0.581).** A general cross-encoder
   NLI flags 78 false positives because it cannot reason about scope / version / time —
   it sees "must X" vs "must not X" and calls contradiction even when one is prod and
   the other dev, or one is v1 and the other v2. This is the empirical refutation of
   "bake an instruct/NLI model as the primary judge" — and confirmation of the
   "normalize first" call over an "NLI-as-judge" alternative.
2. **The structural (normalize-first) layer is the precision win:** 0.596 → 0.99+. It
   deterministically resolves the false-positive families (disjoint env/service/version
   scope, non-overlapping time windows, exception carve-outs that actually agree).
3. **Hybrid = best of both.** Structural decides the clear cases; NLI judges only the
   ambiguous residual *after* the scope traps are already filtered out — so it lifts
   recall (0.963 → 0.981) without re-introducing FPs. Gold-norm hybrid is perfect.

## Remaining errors (honest)

- **Rule-norm hybrid, 1 FP:** `"...must be used until 2026-09-30"` vs `"...must not be
  used after 2026-09-30"`. The rule normalizer parsed "after DATE" as `valid_from =
  DATE` instead of `DATE + 1 day`, so the windows touch and it scores contradiction.
  Gold-norm gets it right -> this is an **extraction-quality** bug, exactly the gap a
  real LLM extractor (or a date-boundary fix) closes. Not a logic flaw.
- **2 FN (gold) / 2 FN (hybrid-rule):** exception-clause + one required_vs_forbidden
  case where stance was undetermined and NLI scored just under threshold. Threshold is
  tunable against this set.

## Reading for calibration

The set is now the fixed yardstick. Any change to the normalizer, structural rules, NLI
model, or thresholds re-runs in ~11s and shows precisely which category moved. Targets:
keep precision >= 0.97 (don't get muted) while pushing recall toward gold's ceiling.

## Update 2026-06-25 (b): date-boundary fix + LLM normalizer

- **Fix 1 — rule-normalizer date fix:** `"after DATE"` now -> `DATE + 1 day` (and `"before DATE"`
  -> `DATE - 1 day`). The lone structural(rule) false positive is gone:
  **structural (rule norm) now P=1.000 R=0.963 F1=0.981** (was P=0.990, FP 1 -> 0).
- **Fix 2 — LLMNormalizer (Ollama instruct extractor):** built + integration-validated.
  gemma4 produced a perfect extraction on a cold call
  (`"TLS 1.2 must be enabled for all public-facing services in prod."` ->
  subject `tls 1.2`, modality `must`, object `enabled`, scope `{prod, public-facing}`).
  **Full benchmark deferred:** the shared LAN GPU box (Ollama) was saturated at the time
  (even a warm 1-token generate exceeded 75s) — not stressing production further. The
  extractor caches by (model, text), so `run_llm_norm.py` resumes; run it off-peak:
  `python evals/contradiction/run_llm_norm.py --n 40 --with-nli`
  Expectation to verify: LLM norm should approach the gold-norm ceiling on prose the
  RuleNormalizer mangles (the seed's irregular phrasings).

## Update 2026-06-25 (c): post-code-review hardening

The contradiction engine was adversarially code-reviewed. Waves A+B+C landed
structural fixes, the SQLite write-guard hardening, and LLM/NLI
guard rails. Re-run on the same 270-pair set:

| Judge | Pre-review | Post-review |
|---|---|---|
| lexical baseline | P=0.596 R=0.981 F1=0.741 | (unchanged) |
| NLI only | P=0.581 R=1.000 F1=0.735 | (unchanged) |
| structural (rule norm) | P=0.990 R=0.963 F1=0.977 | **P=1.000 R=0.991 F1=0.995** |
| structural (gold norm) | P=1.000 R=0.981 F1=0.991 | **P=1.000 R=0.991 F1=0.995** |
| hybrid (rule + NLI) | P=0.991 R=0.981 F1=0.986 | **P=0.991 R=1.000 F1=0.995** |
| hybrid (gold + NLI) | P=1.000 R=1.000 F1=1.000 | P=0.991 R=1.000 F1=0.995 |

- Rule norm now matches gold norm on structural-only.
- Hybrid (rule+NLI) hit perfect recall without losing precision.
- 20/20 store tests passing (including 12 review-demanded regression tests).

## Repro

```bash
python evals/contradiction/generate.py     # rebuild dataset.jsonl (deterministic)
python evals/contradiction/evaluate.py     # full comparison table (downloads NLI once)
```
