# Ergo Contradiction Eval Set

**Purpose:** the de-risking gate for this engine. You cannot trust or calibrate
the write-time contradiction detector without a labeled set. This was the
top priority to build *before* tuning thresholds.

## ⚠️ Synthetic only — no proprietary content

Every claim here is **fictional** (a made-up "Helix platform" engineering-policy
domain). It is *domain-shaped* — it exercises the same structural patterns real
policy knowledge has (modality, scope, environment, version, effective time,
exceptions) — but it contains **zero real proprietary third-party content**.
This set is built and run entirely on synthetic data.

## What it tests

The detector's job is to FLAG genuine contradictions while NOT crying wolf on claims
that merely look similar. So the hard part isn't "X required" vs "X forbidden" — it's
the **near-misses that legitimately coexist** (same subject, different scope / env /
version / time). Those are the false-positive traps that get a tool muted in week 2.

## Label taxonomy

| label | meaning | detector should | counts as |
|---|---|---|---|
| `contradiction` | claims genuinely conflict (overlapping scope+time, opposing modality) | **flag** | positive |
| `coexist` | related/semantically close but NOT conflicting (disjoint scope/env/version/time, or compatible exceptions) | **not flag** | negative (FP trap) |
| `entailment` | one claim restates or is implied by the other (same direction; `must` ⇒ `should`) | **not flag** | negative |
| `unrelated` | different subjects; shouldn't even surface as a candidate | **not flag** | negative (sanity) |

Binary mapping for precision/recall: `contradiction` = positive; everything else =
negative. **False positive** = flagging a coexist/entailment. **False negative** =
missing a contradiction.

`difficulty`: `hard` = semantically close but a negative (the traps), or a subtle
partial-overlap contradiction. `easy` = obvious.

## Files

- `seed.jsonl` — hand-authored gold pairs (highest quality, the trickiest cases).
- `generate.py` — merges the seed with a principled combinatorial generator
  (labels are *derivable from structure*: scope-overlap × modality-polarity ×
  time-overlap) and writes `dataset.jsonl`. Deterministic (seeded) so the set is
  stable and reviewable.
- `dataset.jsonl` — the built eval set (committed).
- `evaluate.py` — judge-agnostic harness. Give it any `judge_fn(claim_a, claim_b) ->
  (label, score)` and it reports precision/recall/F1 + a confusion matrix, broken
  down by category and difficulty. Ships a dumb lexical baseline so it runs today and
  gives a floor number (which also demonstrates *why* normalization matters — the
  baseline flags the scope-coexist traps).

## Record schema (one JSON object per line)

```json
{
  "id": "ctr-0001",
  "category": "required_vs_forbidden",
  "difficulty": "easy",
  "label": "contradiction",
  "claim_a": {
    "text": "TLS 1.2 must be enabled for all public-facing services in prod.",
    "who": "alice",
    "norm": {"subject": "tls_1_2", "modality": "must", "object": "enabled",
             "scope": {"env": "prod", "service_class": "public-facing",
                       "region": null, "version": null},
             "valid_from": null, "valid_until": null, "exception": null}
  },
  "claim_b": { "...": "..." },
  "rationale": "same subject+scope, overlapping time, opposing modality (must vs must_not)"
}
```

## Usage

```bash
python evals/contradiction/generate.py        # -> dataset.jsonl + a coverage report
python evals/contradiction/evaluate.py         # runs the baseline judge over dataset.jsonl
```

When the real judge (normalize → structural → NLI) exists, point `evaluate.py` at it
and calibrate the confidence thresholds against this set.
