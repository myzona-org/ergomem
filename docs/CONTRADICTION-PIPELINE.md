# Contradiction Pipeline

The four-stage write-time check. Source: [`ergo/contradiction/normalize.py`](../ergo/contradiction/normalize.py), [`structural.py`](../ergo/contradiction/structural.py), [`nli.py`](../ergo/contradiction/nli.py), [`judge.py`](../ergo/contradiction/judge.py).

```
incoming statement  ─►  1. NORMALIZE  ─►  2. STRUCTURAL  ─►  3. NLI (gated)  ─►  4. JUDGE  ─►  tier
```

It runs **only** on `POST /remember`, `POST /learn`, and `POST /supersede` (`/learn` is `remember` with source provenance folded into the reason — same gate). `POST /ingest` skips the whole pipeline (the `remember_fact` fast-path stores reference content without the gate, per the README "fact = upsert freely" rule). Ingested facts are also skipped on the **candidate side** (2026-06-29): rows with a NULL `norm` are never contradiction-checked against an incoming decision, so a bulk-ingested chunk can't 409 a `/remember` — only prior decisions can.

## The bet

NLI alone on real prose ≈ lexical baseline (P≈0.58). Normalizing first (extracting modality + subject + scope), then running structural compare on the normalized form, then using NLI only on the *fuzzy residual* **with an overlap gate**, beats both pieces alone. Labeled prose eval after the 2026-06-27 fixes:

| config | precision | recall | F1 | false hard-blocks |
|---|---:|---:|---:|---:|
| structural-only | 0.933 | 0.467 | 0.622 | 0 |
| NLI ungated (broken) | 0.420 | 0.700 | 0.525 | 0 |
| **NLI + gates (current)** | **0.957** | **0.733** | **0.830** | **0** |

See [EVALS.md](EVALS.md) for the full numbers + how to reproduce.

## Stage 1 — Normalize

Turns prose into a structured claim.

```python
# example output of normalize.py for "Deploy on Tuesdays."
{
  "modality":   "should",          # must | should | may | must_not | should_not | may_not
  "subject":    "deploy",          # canonical entity / action
  "object":     None,              # canonical state verb (enabled/used/removed/…), enum-constrained
  "value":      None,              # value-bearing object selector (colour/port/version/…) — see below
  "scope":      {"env": None, "team": None},
  "valid_from": None,
  "valid_until": None,
  "exceptions": [],
  "subject_kind": "PRESENT"        # PRESENT | FUZZY | MISSING (added in 2026-06-27 fixes)
}
```

### The `value` field (2026-06-27 — the blue/red canary fix)

`object` is **enum-constrained** to a fixed list of canonical state verbs
(`enabled`, `used`, `removed`, …). That's deliberate — it's what `_stance()` reasons
over — but it has no room for the *value* that distinguishes two otherwise-identical
prescriptions. So `"must use a blue canary"` and `"must use a red canary"` both
normalized to `object: "used"` and collapsed to an identical norm (or the colour
leaked into `subject`, making the two look *unrelated*). Either way the contradiction
gate was **starved** — it compared norms that no longer carried the distinguisher.

`value` is the fix: a single **atomic** value-bearing selector — a colour, a port
number, a version, a hostname, a numeric count — kept out of `subject` (so the
subject stays shared) and out of `object` (so the polarity enum stays clean). It is
**not** enum-constrained; it's free-text, lowercased, or `None`. The prompt is told
to leave it `None` for bare states (`"must be enabled"`) and for documentation that
merely lists option names/modes (`open`, `off`, `always`, `allowlist`). Old persisted
norms without the key degrade gracefully — every reader uses `norm.get("value")`.

### Available normalizers

| class | for | needs network | speed |
|---|---|---|---|
| `RuleNormalizer` | regex / deterministic. Default when `ERGO_LLM_PROVIDER` is unset. | no | < 1 ms |
| `GoldNormalizer` | eval fixtures only (looks up pre-computed norms in cache). | no | < 1 ms |
| `AnthropicNormalizer` | Claude Haiku via `api.anthropic.com`. | yes | ~150 ms |
| `OpenAINormalizer` | OpenAI-shape `/v1/chat/completions`. Use this for **LiteLLM** too (`ERGO_LLM_PROVIDER=litellm` is an alias). | yes | ~150 ms (~80 ms via LiteLLM proxy on LAN) |
| `OllamaNormalizer` | local Ollama server. | LAN only | ~500 ms – 2 s depending on model |

Factory: `LLMNormalizer(*args, **kwargs)` picks the backend from `ERGO_LLM_PROVIDER` (or `ANTHROPIC_API_KEY` presence as the fallback default).

**Subject extraction is the critical piece.** If `subject` is `MISSING` (extractor returned nothing), the engine treats the claim as **incomparable** to anything that doesn't share the same subject (see structural). If `subject` is `FUZZY` (low confidence), comparison defers to NLI.

### LLM cache

The LLM normalizers cache results in `ERGO_LLM_CACHE` (default `<repo>/evals/contradiction/.cache/llm_norm.json`) keyed by statement hash + provider + model id. Writes are atomic (`os.replace` via a temp file). Re-runs against the same statement are free.

### Fail-open on refusal (2026-06-29)

If the LLM returns something that isn't JSON (a refusal, chatter, a safety
deflection), `_parse_json` returns `None` and `extract()` degrades to an
**unknown norm** (`subject_kind: MISSING` → structural says `incomparable`)
instead of raising. A normalizer refusal can therefore never 500 a write — the
claim is stored un-gated rather than rejected for the wrong reason.

## Stage 2 — Structural compare

Pure Python, deterministic. Takes two normalized claims (incoming + candidate from the active set) and returns a `Verdict(label, tier, score, source, reason)`.

```python
# structural.py decision tree (simplified)
if incoming.subject_kind == "MISSING":
    return Verdict("incomparable", HIGH)        # added 2026-06-27 fix 1 — judge logs it, NLI never runs

if same_subject and overlapping_scope and overlapping_time:
    if opposing_modality:                       # must vs must_not, etc.
        return Verdict("contradiction", HIGH, source="structural")
    # value conflict (2026-06-27): same subject, SAME-direction stance, but the
    # atomic `value` selectors disagree (blue vs red, port 8443 vs 9090) → you
    # can't be required to use BOTH. No polarity flip exists, so the verdict
    # carries value_conflict=True and the judge SKIPS the polarity gate for it.
    if same_direction_stance and atomic(value_a) and atomic(value_b) and value_a != value_b:
        return Verdict("contradiction", HIGH, source="structural", value_conflict=True)
    if same_modality:
        return Verdict("consistent", HIGH)
    return Verdict("uncertain", MED)            # different modality, not opposing → defer

if subject_kind == "FUZZY":
    return Verdict("unknown", LOW, "defer to NLI on fuzzy subject")

if disjoint_scope or disjoint_time:
    return Verdict("coexist", HIGH)             # principled "they don't fight" — not an escape hatch

return Verdict("unknown", LOW)                  # nothing structural to say → defer to NLI
```

Confidence tiers: `HIGH` / `MED` / `LOW`. The judge uses these to decide whether to call NLI.

**Scope = environment + team + tenant.** A `prod` claim and a `dev` claim aren't a contradiction even if their statements look opposing.

**Time = `valid_from` / `valid_until`.** Non-overlapping date windows → coexist.

**Modality opposition lexicon** (from `judge.py`): `must`↔`must_not`, `should`↔`should_not`, `may`↔`may_not`, plus a polarity lexicon for verbs (`approved`↔`rejected`, `retained`↔`removed`, `grant`↔`deny`, etc.). Expanded in the 2026-06-27 fix 2.

**Value conflict** (`value_conflict=True`, source `structural-value`): same subject + same-direction stance + overlapping scope/time, but the **atomic** `value` selectors disagree. Tier follows modality strength (`must`/`must` → HIGH block, softer → warn). Guard rails so config-enumeration docs don't false-flag: both stances must be *determined and equal*, and both values must be *atomic* (≤ 2 tokens, ≤ 24 chars — multi-word extractor noise like `"stable zalo user ids"` is refused). The judge additionally confirms the differing value tokens are **grounded in the raw text** (`_value_grounded`) before blocking, so a hallucinated normalizer value can't manufacture a contradiction.

## Stage 3 — NLI (gated)

Only runs when structural returns `LOW` confidence (couldn't decide). Even then, **the result is gated** before it's allowed to block a write.

### Model

- `Xenova/nli-deberta-v3-base` (HF), quantized ONNX export, ~190 MB.
- Loaded via `onnxruntime` + `tokenizers` + `numpy`. **No PyTorch.**
- CPU-only by design (small enough that GPU adds no value vs single-thread serialization).
- Baked into the image; the Dockerfile's no-network boot test confirms it loads offline.

### Output

`{"contradiction": 0.94, "entailment": 0.03, "neutral": 0.03}` — three softmax probabilities.

### The gates (2026-06-27 fix 2)

A raw NLI `contradiction > τ` is **not** enough to block, because NLI on prose has a ~58 % precision floor (stray "not" → spurious flips). The gates:

```python
# pseudocode of judge.py NLI-residual branch
if nli.contradiction < threshold:
    return "no flag"

# require a polarity flip on SHARED content
overlap = jaccard(incoming.tokens, candidate.tokens)
if overlap < NLI_OVERLAP_FLOOR (= 0.25):
    return "drop"                  # bare stray-`not` on unrelated text → silent log

if has_polarity_flip(incoming, candidate):
    if overlap >= 0.5:    return "block"
    elif overlap >= 0.35: return "warn"
    else:                 return "drop"

return "no flag"
```

**`has_polarity_flip` checks** modality opposition (`must` vs `must_not`), the polarity lexicon (`approve` vs `reject`), and negation parity. **Object antonyms across unrelated sentences** are deliberately *not* treated as flips — that was the noise generator the gates kill.

### Why "gated" matters

Pre-fix (2026-06-25): NLI hard-blocked **270 false positives** on a 3299-claim corpus ingest because the gates only ran on the structural path; the subject-unknown defer-to-NLI path called raw NLI unguarded. Post-fix: same ingest, **0 false blocks, 13 warns** — below structural-only's 14.

## Stage 4 — Judge → tier

Combines the two verdicts into one of three outcomes:

| outcome | tier | what happens |
|---|---|---|
| **BLOCK** | `block` | `/remember` returns **409 Conflict** with the conflict set; nothing written; row added to `conflict_events` (`resolution='flagged'`). Caller must resolve (force_exception, supersede, or drop). |
| **WARN** | `warn` | claim is **stored**, but the warn is returned in the response *and* recorded to `conflict_events` (`resolution='warned'`). |
| **clean** | — | claim stored without flagging. |

```python
# judge.py decision (simplified)
structural_verdict, nli_residual = ...

if structural_verdict.label == "contradiction" and structural_verdict.value_conflict:
    # 2026-06-27: blue/red canary, port 8443/9090 — no polarity flip exists, so the
    # polarity gate must NOT veto. Confirm the values are grounded in text, then
    # honour the structural tier (HIGH → block, softer → warn). source="structural-value".
    if _value_grounded(value_a, value_b, text_a, text_b):
        return BLOCK if structural_verdict.tier == HIGH else WARN
    return clean                                  # value not grounded → log, don't flag

if structural_verdict.label == "contradiction" and structural_verdict.tier == HIGH:
    # gates apply HERE too (since 2026-06-26 wave A)
    if polarity_gate.passes and aspect_gate.passes:
        return BLOCK
    return WARN

if structural_verdict.label == "incomparable":   # subject MISSING (2026-06-27 fix 1)
    return clean                                  # logged, NLI never runs

if structural_verdict.tier == LOW:
    return nli_residual.gated_outcome             # block | warn | drop (Stage 3 above)

return clean
```

## Cost / latency budget per `/remember`

On a single-claim write against a project with ~100 active claims:

| stage | typical | notes |
|---|---|---|
| normalize (LLM, cache miss) | ~80–200 ms | LiteLLM/Anthropic. Cache hit: < 1 ms. |
| structural compare (× 100 actives) | ~5 ms total | pure Python, no I/O. |
| NLI residual (~10 % of comparisons) | ~10–30 ms each | onnxruntime CPU, single thread. |
| judge + write-guard + SQLite insert | ~5 ms | BEGIN IMMEDIATE + 2 INSERTs. |
| **end-to-end (cache miss)** | **~100–250 ms** | dominated by the LLM normalize. |
| **end-to-end (cache hit)** | **~10–40 ms** | judge + NLI + write. |

`/ingest` is much cheaper per chunk (~30–50 ms) because it skips stages 1–4 entirely — only embed + write-guard insert.

## Two real-corpus runs that locked in the current config

### 2026-06-26 — the 270-false-block disaster

NLI on, no gates on the subject-unknown defer path. 3299-claim corpus ingest produced **270 BLOCKED + 17 WARN**. Root-caused to the gates only running on the structural `contradiction` branch — the `unknown → defer to NLI` path went straight to raw NLI. Fix: apply the same polarity + aspect gates before any NLI hard-block. → 1 block + 832 warns.

### 2026-06-27 — the labeled-eval calibration

832 warns was still noise. Two more fixes:
1. **Subject-MISSING → `incomparable`** (NLI never runs at all when the extractor gave us nothing).
2. **Overlap-gated polarity** (`NLI_OVERLAP_FLOOR=0.25`; a stray "not" on unrelated text drops, overlap arbitrates block / warn / drop).

Calibrated against `evals/prose/dataset.jsonl` (105 pairs; 60 negatives **harvested from the engine's own false positives** → non-circular). Numbers: precision 0.42 → **0.957**, recall 0.70 → **0.733**, F1 0.53 → **0.830**. Real ingest: 832 warns → **13** (below structural-only's 14).

### 2026-06-27 (evening) — the blue/red canary value-loss fix

Live MCP verification caught the normalizer collapsing `"must use a blue canary"`
and `"must use a red canary"` to non-contradictory norms (the colour was dropped or
absorbed into `subject`). Fix: the `value` field + the structural value-conflict gate
above. Deployed to the live `ergo-merged` container the same day (image `ergo:merge`
rebuilt from `main` @ `d4a40a1`); a live HTTP self-test confirmed the second canary
now returns **409** (`source=structural-value`).

**Calibration shift (honest).** A/B on the same pinned NLI runtime (fresh caches, so
the new prompt is fully exercised) moved the prose eval **recall 0.68 → ~0.81 (+0.13)**
and **F1 ~+0.04**, at the cost of **precision ~0.95 → ~0.80–0.86 (−0.1)** — but the
hard invariant **false hard-blocks stayed 0**, and the value gate *itself* contributed
**0 false positives**. The residual FPs are all advisory **aspect-gate warns** (NLI=1.00
on documentation-enumeration prose, surfaced more often because better subject-matching
reaches that path), never blocks. A prompt-softening retune was tried and reverted — it
cut recall for no precision gain, confirming the precision band is NLI/claude-haiku
nondeterminism-driven (~±0.05), not prompt-wording-driven. Net: a deliberate
precision↔recall trade that revises the recorded P≈0.957 calibration; the canary/port
class is a real correctness gap that's now closed.

## Open follow-ups

- **Recall ceiling is structural, not NLI.** Recall@1 currently caps around 0.73 because the structural subject-matcher can't reach across paraphrase / coreference. Real-corpus impact is small (the 13 warns are mostly noise we want to suppress, not contradictions we're missing).
- **No clear-statement / mismatch surface in the response.** If structural says "incomparable", the client doesn't know whether that's "subject was missing" vs "scope disjoint" vs "modality different". `conflict_events` rows have the detail; consider exposing it on the API.
- **NLI variant is `quantized` (~190 MB).** `int8` (~80 MB) might be good enough — needs an eval pass to confirm precision doesn't drop. Rebuild with `--build-arg ERGO_NLI_VARIANT=int8`.

## Related

- The HTTP surface that drives this → [HTTP-API.md](HTTP-API.md)
- Eval suites and numbers → [EVALS.md](EVALS.md)
- Retrieval (the *other* half of the engine, doesn't run this pipeline) → [RETRIEVAL.md](RETRIEVAL.md)
