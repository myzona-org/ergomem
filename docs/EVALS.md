# Evals — what we measure, how, and what the numbers actually are

Three eval suites, each pointed at a different failure mode. Source: [`evals/`](../evals/).

| suite | tests what | corpus | current verdict |
|---|---|---|---|
| `evals/contradiction/` | the **judge** on a clean synthetic set (Helix) | 270 hand-crafted pairs | F1 = 0.995 (sanity check) |
| `evals/prose/` | the **judge on real prose** (labeled, non-circular) | 105 pairs | gated-NLI baseline **P = 0.957, R = 0.733, F1 = 0.830, 0 false hard-blocks**; after the 2026-06-27 value-conflict change: recall ↑ ~0.81, precision ↓ ~0.80–0.86 (advisory warns), **still 0 false hard-blocks** |
| `evals/container/` | the **deployed image** end-to-end over HTTP | 7 hand-crafted prose pairs | **7/7, 0 false blocks, 0 silent misses** |

Plus the unit suite in `tests/`: write-guard, gates, value-conflict, normalizers, NLI, server, MCP adapter, embedders. `pytest tests -q` is always the authoritative count for whatever's actually in your checkout — run it rather than trusting a number printed here, since this repo's own test count moves with every PR.

## The hard invariants (must never fail)

1. **0 false hard-blocks** on any labeled prose set.
   A blocked write is the engine refusing the user. Any non-zero number here means the engine is unusable in production.
2. **0 silent misses** on the container smoke.
   A "silent miss" means a real contradiction stored without any flag. The engine would be lying about catching contradictions.
3. **All unit tests** pass (`pytest tests -q`).
   Locks in write-guard correctness, gate behavior, value-conflict detection, retrieval ranking, supersede + history.
4. **No-network boot test** passes in `docker build`.
   See [DEVELOPMENT.md → no-network gate](DEVELOPMENT.md#the-no-network-boot-gate).

If any of these regress, **don't ship**.

## Suite 1 — `evals/contradiction/` — the Helix synthetic set

The original 270-pair eval that drove the design. Hand-crafted pairs covering same-subject + opposing-modality, scope-disjoint coexistence, time-windowed coexistence, polarity flips, etc.

- Dataset: [`evals/contradiction/dataset.jsonl`](../evals/contradiction/dataset.jsonl) (270 pairs).
- Harness: judge-agnostic — same set drives RuleNormalizer-only, LLMNormalizer, hybrid, NLI-only, etc.
- Result on the hybrid (rule + structural + NLI): **P = 0.991, R = 1.000, F1 = 0.995**.
- NLI alone on this set: P ≈ 0.58 (≈ lexical baseline) — the original motivation for normalize-first.

Run it:

```bash
source .venv/bin/activate
python evals/contradiction/evaluate.py
```

Results: [`evals/contradiction/RESULTS.md`](../evals/contradiction/RESULTS.md).

**What this proves:** the bet works on a clean labeled set. It doesn't tell you anything about messy prose — that's suite 2.

## Suite 2 — `evals/prose/` — the labeled prose set (the calibration eval)

The eval that actually drives gate tuning. **The negatives are harvested from the engine's own false positives**, so the precision number is non-circular (i.e. measured against real mistakes the engine actually made, not synthetic adversarial examples).

- Dataset: [`evals/prose/dataset.jsonl`](../evals/prose/dataset.jsonl) (105 pairs: 30 positives + 75 negatives).
- 60 of the 75 negatives are real false positives harvested from the engine's own false-positive cache on a large real-world document ingest (the 270-block run + the 832-warn run) — the harvesting tool itself is private (depends on a corpus that doesn't ship), but the harvested rows are baked into `dataset.jsonl`.
- 15 are "near miss" hand-crafted hard negatives.

| config | precision | recall | F1 | false hard-blocks |
|---|---:|---:|---:|---:|
| structural-only | 0.933 | 0.467 | 0.622 | 0 |
| NLI ungated (pre-2026-06-26 fix) | 0.420 | 0.700 | 0.525 | 0 |
| **NLI + gates (current)** | **0.957** | **0.733** | **0.830** | **0** |

Run it:

```bash
source .venv/bin/activate
bash evals/prose/run_eval.sh
```

Results: [`evals/prose/RESULTS.md`](../evals/prose/RESULTS.md). This is the **authoritative current eval** — the EOD 2026-06-26 "keep NLI off" conclusion is overturned here.

**What this proves:** the current gated-NLI config is materially better than structural-only (+27 pts recall, +0.024 precision, 0 false blocks) on real-world prose, not just synthetic data.

> **2026-06-27 recalibration (value-conflict fix).** The table above is the gated-NLI
> baseline. Adding the `value` field + structural value-conflict gate (the blue/red
> canary fix) shifted the operating point: A/B on the same pinned NLI runtime moved
> **recall 0.68 → ~0.81** and **F1 ~+0.04**, at **precision ~0.80–0.86** (down ~0.1,
> all in advisory *warns*). **False hard-blocks stayed 0** and the value gate itself
> added **0 FPs**. Because claude-haiku is non-deterministic at temp 0 (~±0.05 between
> fresh-cache runs), treat these as a band, not a point. Full write-up:
> [`evals/prose/RESULTS.md`](../evals/prose/RESULTS.md) → "2026-06-27 value-conflict".

### Adding cases to the prose set

Two ways:

1. **Manual:** edit `dataset.jsonl`. Each line:
   ```json
   {"a": "...", "b": "...", "label": "contradiction" | "negative", "tag": "..."}
   ```
2. **Harvest from a false-positive run:** if the engine flagged something it shouldn't, a private harvesting tool (not shipped in this tree) can read the false-positive cache from a real-world document ingest and emit harvested-negative entries. Re-run + commit.

After changing the dataset, **re-run the eval and update RESULTS.md** — otherwise the numbers in the docs go stale.

## Suite 3 — `evals/container/` — the deployed image, end-to-end

The thing that proves the **deployable container** does what we think. Not testing the engine in a venv — testing it as it'll run in production, over HTTP, including the LLM normalize call to the LiteLLM proxy.

- Script: [`evals/container/prose_smoke.py`](../evals/container/prose_smoke.py).
- 7 hand-crafted prose pairs covering:
  - 2 real contradictions (one expected to BLOCK, one expected to WARN).
  - 5 negatives: coexist-by-environment, coexist-by-time-window, parallel-restatement boilerplate, subject-homonym/different-aspect, unrelated topics.
- Hard invariants: **0 FALSE BLOCKS** and **0 SILENT MISSES** (both must be 0; the script exits non-zero if either is non-zero).

Current result against `ergo:merge` on :8788:

```
===== SUMMARY =====
  cases passed         : 7/7
  FALSE BLOCKS         : 0  (HARD INVARIANT: must be 0)
  SILENT MISSES        : 0  (HARD INVARIANT: must be 0)
  contradictions surfaced: 1 as block, 1 as warn

RESULT: ALL PASS ✅
```

Run it (needs the container running):

```bash
source .venv/bin/activate
source /path/to/ergo.env                    # for ERGO_API_TOKEN
ERGO_URL=http://127.0.0.1:8788 python evals/container/prose_smoke.py
```

**What this proves:** the deployed binary, with the real LLM normalize, real NLI, real SQLite write-guard, all wired together, surfaces both genuine contradictions correctly and never false-blocks on the curated traps.

## Unit tests — `tests/`

```bash
python -m pytest tests -q
```

| file | locks in |
|---|---|
| `test_store.py` | write-guard, supersede, race-safety, version-drift detection, audit rows, empty-store states |
| `test_judge_gates.py` | polarity + aspect gates on both the structural and NLI-residual branches; the 2026-06-26 fix lives here |
| `test_value_conflict.py` | the `value` field (coerce/prompt), the structural value-conflict gate (atomic + determined-stance guards), `_value_grounded`, and the blue/red canary + port end-to-end block (2026-06-27) |
| `test_normalize_factory.py` | LLM factory picks anthropic / openai / litellm / ollama from env |
| `test_retrieval.py` | recall ranking, why+history across supersede, superseded exclusion, retrieval-disabled error |

All in-process SQLite (`":memory:"`) — **no DB, no network, no model downloads**. Retrieval tests use a deterministic 64-dim fake embedder so they run anywhere.

Add tests next to the closest existing one; see [DEVELOPMENT.md → Adding a test](DEVELOPMENT.md#adding-a-test).

## Reproducing the current canonical numbers

```bash
source .venv/bin/activate

# 1. unit tests (must be all-green: 70 in tests/contradiction/, 85 in tests/)
python -m pytest tests/ -q

# 2. labeled prose eval (must be P>=0.95, R>=0.7, 0 false hard-blocks)
bash evals/prose/run_eval.sh

# 3. container smoke (must be 7/7, 0 false blocks, 0 silent misses)
ERGO_URL=http://127.0.0.1:8788 ERGO_API_TOKEN=$TOK \
  python evals/container/prose_smoke.py

# 4. build's no-network boot test (must pass)
docker build -t ergo:test .
```

Any new commit that changes the engine should be run against all four. Drop them in a Makefile when there's enough volume to justify it.

## Open eval gaps

1. **No labeled retrieval set.** We measure firewall precision/recall against the prose eval, but retrieval ("did `/recall` return the right thing?") is only spot-checked. A labeled retrieval set (~50 queries, expected top-1 chunk) would let us tune the 0.7 / 0.3 hybrid weight and detect chunker regressions.
2. **No latency budget enforcement in CI.** The numbers in [CONTRADICTION-PIPELINE.md → cost / latency budget](CONTRADICTION-PIPELINE.md#cost--latency-budget-per-remember) are anecdotal. A timed smoke that fails the build on > 500 ms p95 `/remember` (cache cold) would catch perf regressions.
3. ~~No multi-writer / concurrency eval.~~ **Closed** (private-repo PR #39): the server now runs a bounded `ThreadingHTTPServer`, and `tests/contradiction/test_http_concurrency.py` (head-of-line non-blocking across tenants, bounded-pool cap) + `test_store_concurrency.py` (same-tenant concurrent writes still yield contiguous/unique `claim_seq`, cross-tenant writes stay siloed) cover exactly this. Still no eval-suite-level (as opposed to unit-test-level) latency-under-load number — that gap remains open.

## Related

- The pipeline being evaluated → [CONTRADICTION-PIPELINE.md](CONTRADICTION-PIPELINE.md)
- How retrieval works → [RETRIEVAL.md](RETRIEVAL.md)
- Dev setup + adding tests → [DEVELOPMENT.md](DEVELOPMENT.md)
