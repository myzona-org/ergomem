# Ergo documentation

**Memory that remembers _why_ — and catches itself contradicting.** One engine, one image, one SQLite file (with sqlite-vec for the KNN index inside it).

This `/docs` directory is the canonical reference. The top-level [`README.md`](../README.md) is the quickstart; everything else lives here.

## Start here

- [`WHY-ERGO.md`](WHY-ERGO.md) — the positioning: why write-time verification beats plain RAG, with real dogfood evidence, a why-not-X table, and where Ergo is overkill

## Reference — where stuff lives, how to call it

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — components, file map, data flow lanes (markdown twin of [`architecture.html`](architecture.html))
- [`HTTP-API.md`](HTTP-API.md) — every endpoint, auth, request/response shapes, status codes, examples
- [`MCP.md`](MCP.md) — the stdio MCP adapter: `ergo_*` tools, scope defaults, registration
- [`CONFIGURATION.md`](CONFIGURATION.md) — every `ERGO_*` env var (+ `OPENAI_*`, `OLLAMA_*`, `ANTHROPIC_*`, `HF_*`)
- [`STORAGE.md`](STORAGE.md) — SQLite schema, the 5 tables, write-guard semantics, backup rules

## How it works — the bet, internals

- [`CONTRADICTION-PIPELINE.md`](CONTRADICTION-PIPELINE.md) — `normalize → structural → NLI(gated) → judge` (the four stages, the gates, current numbers)
- [`RETRIEVAL.md`](RETRIEVAL.md) — embedders, sqlite-vec, hybrid `0.7 × vec + 0.3 × keyword` scoring, `recall` / `why` / `ingest` internals

## Ops + dev

- Re-embed migration: [`ergo/contradiction/reembed.py`](../ergo/contradiction/reembed.py) — standalone offline CLI, rebuilds the SQLite file into a new one after an embedder/provider/dim change (backup-first, dry-run, active-claim count verify)
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — local setup, tests, image build, the no-network boot gate
- [`EVALS.md`](EVALS.md) — the eval suites + current numbers + how to reproduce
- [`architecture.html`](architecture.html) — interactive visual diagram and the **source of truth**.


## Quick links by question

| if you're asking… | start at |
|---|---|
| "What endpoint do I POST a decision to? What's the body shape?" | [HTTP-API.md → POST /remember](HTTP-API.md#post-remember--guarded-write) |
| "What does `ERGO_NLI=off` do?" | [CONFIGURATION.md → NLI judge](CONFIGURATION.md#nli-judge) |
| "How do I back up the SQLite file?" | [STORAGE.md → Backup](STORAGE.md#backup) |
| "How does the engine decide BLOCK vs WARN?" | [CONTRADICTION-PIPELINE.md → Stage 4](CONTRADICTION-PIPELINE.md#stage-4--judge--tier) |
| "Why is the wrong chunk coming back from `/recall`?" | [RETRIEVAL.md → recall() — hybrid scoring](RETRIEVAL.md#recall--hybrid-scoring) |
| "How do I run the eval suite?" | [EVALS.md → Reproducing the current canonical numbers](EVALS.md#reproducing-the-current-canonical-numbers) |
| "How do I add a new endpoint?" | [DEVELOPMENT.md → Adding a new endpoint](DEVELOPMENT.md#adding-a-new-endpoint) |
| "What does `/diagnose` actually return?" | [HTTP-API.md → GET /diagnose](HTTP-API.md#get-diagnose--ops-view-anti-zombie) |
| "A claim was wrong from the start (typo / test junk) — how do I remove it?" | [HTTP-API.md → POST /retract](HTTP-API.md#post-retract--wrong-at-birth-removal) |
| "Why did this commit reverse the previous NLI-off recommendation?" | [CONTRADICTION-PIPELINE.md → the two real-corpus runs](CONTRADICTION-PIPELINE.md#two-real-corpus-runs-that-locked-in-the-current-config) |

## Current canonical numbers

- Labeled prose eval (`evals/prose/`, 105 pairs): gated-NLI baseline **P=0.957 / R=0.733 / F1=0.830**; after the 2026-06-27 value-conflict fix, **recall ↑ ~0.81 / precision ~0.80–0.86 (advisory warns)** — **0 false hard-blocks** in both
- **7/7, 0 false blocks, 0 silent misses** on the container smoke (`evals/container/prose_smoke.py`)
- **No-network boot test passes** in `docker build` (the hard CI gate)
- `pytest tests -q` is the authoritative unit-test count — see [EVALS.md](EVALS.md) for the current number and the full suite breakdown
