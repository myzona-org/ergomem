# Ergo /why Precision Eval Set (private-repo issue #209)

**Purpose:** private-repo issue #209 is the harder residual left after private-repo issue #202's relevance floor
(`WHY_RELEVANCE_FLOOR = 0.5`, which fixed *"nothing matched, so don't answer"`).
This set measures the sharper failure: `/why` returning the **wrong** claim
**confidently**, at a score no floor can filter, because the nearest neighbour
in vector space is not the right answer. Do not touch `WHY_RELEVANCE_FLOOR` —
raising it to catch these cases would suppress the majority of
genuinely correct answers (see private-repo issue #209's own analysis).

## What this tests

A `query -> expected_statement` pair. `expected_statement` is either the exact
text of the claim `/why` *should* surface, or `null` for a genuine true
negative (no claim in scope answers the query, so `/why` should suppress).

Every row is drawn from a synthetic corpus purpose-built for this eval —
see [Data handling](#data-handling) below for how it's generated and what
it does (and doesn't) measure.


## Data handling

This harness ships as a synthetic-only eval: `corpus_synthetic.jsonl` and
`dataset_synthetic.jsonl`, both produced by `generate_synthetic_corpus.py
--emit corpus|dataset`. They invent a fictional decision-memory product
and deliberately rebuild the *structure* that makes this eval meaningful
— clusters whose rows differ only in scope or in which verbs they
enumerate, so the nearest neighbour in embedding space is not
automatically the right answer. Expected statements are looked up from
the corpus by index rather than retyped, since `classify()` compares with
`==`.


## Offline harness (no live server, no prod dependency at run time)

`corpus_synthetic.jsonl` holds 68 synthetic claims, fields
`{statement, reason, project}`. `evaluate.py` rebuilds an **in-memory**
`ContradictionStore` from this corpus using `ergo.embedders.get_embedder()`
(the same fastembed bge-small/384 model the engine uses, offline/local),
then calls the real `ContradictionStore.why()` — the actual production
code, not a reimplementation — against it.


Every score cited in this README and in `dataset_synthetic.jsonl`'s
`rationale` fields comes directly from running `evaluate.py` against the
two synthetic files checked into this eval — fully reproducible offline,
with no external dependency.


Because the offline harness's fresh IDs are not stable across rebuilds
(`remember_fact` assigns a new ULID every run), rows are matched by **exact
claim statement text**, not by claim id.

## Label taxonomy

| `expected_type` | meaning | `/why` should |
|---|---|---|
| `claim` | a specific claim in scope genuinely answers the query | return exactly that claim |
| `true_negative` | no claim in scope answers the query | return null / suppressed |

Verdicts the scorer assigns per row:

| verdict | condition | interpretation |
|---|---|---|
| `match` | returned claim's statement == `expected_statement` | correct |
| `suppressed` | `/why` returned null or a `suppressed` marker | correct for `true_negative` rows; a missed answer (not dangerous) for `claim` rows |
| `wrong` | `/why` returned a non-null claim that isn't the expected one | **the dangerous case** — confident, wrong, uncited-as-wrong |

**Wrong-answer rate** = `count(wrong) / total`. This is the headline number,
reported overall and broken down by `phrasing` category. A `match` or a
`suppressed` are both acceptable outcomes; only `wrong` is the private-repo issue #209
failure mode (a confidently wrong citation that terminates an agent's search).

## Labelling honesty

Every row's `rationale` names the specific reason its `expected_statement` (or
`true_negative` label) was chosen, and cross-checks whether a candidate claim
merely *mentions* the query's topic in passing (not a defensible answer) vs.
is *actually about* it. **0 rows were excluded as uncertain** — every
ambiguous case (e.g. the real answer existing in the *other* project, out of
the query's scope) was resolved to an honest `true_negative` label rather than
guessed at, with the ambiguity spelled out in the rationale.


## Composition (25 rows)

- **18 "claim" rows** grouped into near-neighbour clusters — rows in the
  same cluster differ only in scope, in which verbs they enumerate, or in
  a single changed numeric threshold, engineered so the nearest neighbour
  in embedding space is not automatically the right answer. See
  `generate_synthetic_corpus.py` for how clusters are built.
- **7 true-negative rows**, sanity-checking that the relevance floor
  still suppresses queries with no answering claim in scope.
- 22 rows use the corpus's primary project scope, 3 use a second project
  scope, checking the harness respects project scoping.
- 2 rows currently reproduce as `wrong` at baseline (see below); every
  other row currently matches or is correctly suppressed.
- Phrasing spread: 11 well-formed questions, 10 terse keyword queries, 4
  badly-phrased-but-genuine queries.


## Baseline (synthetic harness)

```
rows: 25  (excluded as uncertain: 0 -- every row was defensibly labeled
'claim' or 'true_negative', see rationale field)

  MATCH      : 16
  SUPPRESSED : 7  (correct behavior for true_negative rows; a missed
answer for 'claim' rows)
  WRONG      : 2  <- confident wrong answer, the dangerous case this eval
is about

  WRONG-ANSWER RATE: 0.080 (2/25)

  by phrasing category:
    badly_phrased   n= 4  wrong= 0  rate=0.000
    terse_keyword   n=10  wrong= 1  rate=0.100
    well_formed     n=11  wrong= 1  rate=0.091

  score distribution:
    match      : min=0.6808 median=0.7842 max=0.8500
    suppressed : min=0.4017 median=0.4607 max=0.4987
    wrong      : min=0.5021 median=0.5082 max=0.5082
```

Both wrong cases land just above `WHY_RELEVANCE_FLOOR` (0.502–0.508) —
this is exactly the failure mode this eval targets: no scalar threshold
separates `wrong` from `match`, so a fix has to look at *what* is
matching, not just *how well*. Use this harness to confirm retrieval
runs correctly and that a change hasn't broken it outright; a small,
engineered-cluster corpus like this one is a sensitivity check, not a
statistically representative precision benchmark.


## Usage

```bash
python evals/why_precision/evaluate.py              # summary only
python evals/why_precision/evaluate.py --errors      # print every wrong/suppressed row + what /why returned
python evals/why_precision/evaluate.py --data other_dataset.jsonl --corpus other_corpus.jsonl
```

This script is an **offline measurement tool** (like `evals/prose/evaluate.py`)
— it does not run in CI or the release gate.
