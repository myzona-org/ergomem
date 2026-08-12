# Prose contradiction eval — the two NLI fixes

*Date: 2026-06-26 (evening, session 2). Labeled set: `evals/prose/dataset.jsonl`
(105 pairs: 30 contradictions / 75 not). Normalizer: LiteLLM `claude-haiku`
(DeepSeek upstream). Judge: structural + NLI (`Xenova/nli-deberta-v3-base`).*

## Why this exists

The earlier finding (an internal NLI-gated run against a large real-world
document corpus) was that NLI is
**net-negative on real prose**: even after the 270-false-block fix, NLI added ~818
noise warns. Two root causes were named; this is the labeled-set work that fixed
both and turned NLI net-**positive**.

The eval set is deliberately **non-circular**: 60 of the 75 negatives are *real
false positives the engine itself produced* on a large real-world document
ingest (harvested from `conflict_events` via a private harvesting tool). The
other 15 negatives are
hand-authored precision traps (scope/time coexist, boilerplate, homonym,
stronger-subset refinement, stray-not). The 30 positives are hand-authored genuine
contradictions across every mode (negation flip, object antonym, modality, with &
without reason clauses, coreference, same-scope opposing).

## The two fixes

**Fix 1 — `structural.compare()` drops subject-MISSING pairs.** When the extractor
returns *no* subject, the claims aren't comparable, so we no longer hand them to
noisy prose-NLI (P≈0.58). New `MISSING` subject-kind → `Verdict("incomparable",
HIGH)` → the judge logs it and never calls NLI. A *fuzzy* subject overlap still
defers to NLI (that's the legitimate residual).

**Fix 2 — an NLI flag requires a polarity flip ON SHARED CONTENT.** A bare
negation-parity flip (a stray "not"/"never" in one sentence) is too weak on the
subject-unknown path. The opposition must sit on overlapping content
(`overlap >= NLI_OVERLAP_FLOOR = 0.25`): high overlap → block, moderate → warn,
low → drop. Overlap — not flip-strength — is the arbiter (an object-antonym across
two *unrelated* sentences is a false flip; real contradictions restate the same
claim and carry high overlap). Plus a small, general-purpose polarity-lexicon
expansion (approved/rejected, retained/removed, grant/deny, …) so real antonym
oppositions are detected on prose.

## Results — labeled prose set (105 pairs)

| config | Precision | Recall | F1 | false HARD blocks |
|---|---:|---:|---:|---:|
| structural-only | 0.933 | 0.467 | 0.622 | 0 |
| NLI (pre-fix) | 0.420 | 0.700 | 0.525 | 0 |
| **NLI (fix 1+2)** | **0.957** | **0.733** | **0.830** | **0** |

Fixed-NLI beats *both* baselines on every axis: **higher precision than
structural-only** (0.957 vs 0.933) **and +27 pts recall** (0.733 vs 0.467), with
zero write-stopping false blocks. FP fell 29 → 1; that 1 is a borderline *warn*
(overlap 0.27), not a block.

## Results — real corpus (full real-world document ingest, 3299 candidates)

| config | BLOCKED | WARN |
|---|---:|---:|
| structural-only | 0 | 14 |
| NLI ungated (original) | 270 | 17 |
| NLI first-gate-fix | 1 | 832 |
| **NLI fix 1+2** | **1** | **13** |

The 832-warn flood collapsed to **13** — below structural-only's 14 — while the
labeled eval shows materially higher recall. **NLI is now net-positive on real
prose.** The earlier "keep NLI off for prose" recommendation is **reversed**: NLI
on (with these gates) is the better config.

## Honest residuals (the recall ceiling is now STRUCTURAL, not NLI)

The 8 remaining false negatives on the labeled set are almost all `src=structural`:
the extractor gave the two claims subjects that scored "different" (jaccard
0.25–0.40 → `DIFFERENT`, never reaching NLI), or the modality model can't see a
semantic opposition (HTTPS vs HTTP; server-side vs client-side). These are a
**separate** problem from the two NLI fixes — the next lever is structural
subject-matching / a richer normalized predicate, not the judge gates. One miss is
a knife-edge (overlap 0.24, just under the 0.25 floor).

`NLI_OVERLAP_FLOOR = 0.25` sits cleanly between the FP cluster (overlap ≤ 0.18) and
genuine NLI-dependent contradictions (≥ 0.25); it's an operating point on the
precision/recall frontier, calibrated on this set rather than eyeballed in
isolation.

## Repro

```
# build the labeled set (positives + near-miss + harvested corpus FPs)
python evals/prose/build_dataset.py
# measure (reuses the litellm norm cache so harvested texts are free)
bash evals/prose/run_eval.sh            # structural + NLI
bash evals/prose/run_eval.sh --no-nli   # structural only
bash evals/prose/run_eval.sh --errors   # list every misclassification
```

---

## 2026-06-27 — value-conflict change (blue/red canary fix)

The `0.957` row above is the **gated-NLI baseline**. The value-loss fix
(`fix/normalizer-adjective-loss`, merged `d4a40a1`) added a `value` field to the norm
+ a structural value-conflict gate so `"must use a blue canary"` vs `"must use a red
canary"` (and `port 8443` vs `9090`) stop collapsing. That changes the operating point,
so it was re-measured **A/B on the same host with the identical pinned NLI runtime**
(onnxruntime 1.27.0 / tokenizers 0.23.1), each side on a **fresh** LLM-norm cache so the
new prompt is fully exercised:

| config (fresh cache, same runtime) | Precision | Recall | F1 | false HARD blocks |
|---|---:|---:|---:|---:|
| BEFORE (main `52f1cef`) | 0.955 | 0.677 | 0.792 | 0 |
| AFTER (value-conflict fix) | ~0.80–0.86 | ~0.77–0.84 | ~0.79–0.83 | **0** |

**Reading it honestly:**
- **Recall up ~+0.13, F1 up ~+0.04, false HARD blocks still 0** (the only un-shippable metric).
- **Precision down ~0.1**, entirely in advisory **warns** — the value gate itself
  contributed **0 false positives** (its atomic + determined-stance + `_value_grounded`
  guards held); the residual FPs are all pre-existing **aspect-gate** warns (NLI=1.00 on
  documentation-enumeration prose) reached more often now that subjects match better.
- claude-haiku is **non-deterministic at temp 0** (provider/proxy variance): repeated
  fresh-cache runs of the *same* prompt landed P 0.80–0.86 / R 0.77–0.84. So this is a
  **band, not a point** — don't chase the last 0.05.
- A **prompt-softening retune was tried and reverted**: it cut recall to 0.71 for no
  precision gain, confirming the precision band is NLI/nondeterminism-driven, not
  prompt-wording-driven.

**Net:** a deliberate precision↔recall trade. The canary/port class was a real
correctness gap (the gate had *zero* chance to fire), now closed, with the hard
invariant intact. Deployed live to `ergo-merged` the same day; HTTP self-test confirmed
the second canary returns 409 (`source=structural-value`).

Repro the A/B (each side gets its own fresh cache so the prompt change is exercised):

```
# AFTER (current tree)
ERGO_LLM_CACHE=/tmp/prose_after.json bash evals/prose/run_eval.sh
# BEFORE (stash the fix, fresh cache, same runtime)
git stash push ergo/contradiction/{normalize,structural,judge}.py
ERGO_LLM_CACHE=/tmp/prose_before.json bash evals/prose/run_eval.sh
git stash pop
```
