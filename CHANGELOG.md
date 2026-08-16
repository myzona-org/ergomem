# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates get
added to a version heading once that version is actually tagged and published — this
file is written ahead of that happening, and an invented date would be less honest than
none.

## [Unreleased]

- Lead the quickstart with the published GHCR image; keep the ingest-tool symlink
- Correct the image-publication status in docs; add OCI image labels
- Fix 5 ReDoS findings in the normalizer, ingest tool, and OpenClaw client

## [0.1.0]

The first public release of the Ergo engine: a write-guarded decision-memory HTTP API, a
stdio MCP adapter, and a single offline Docker image. This repository has no earlier
history of its own — this entry is the starting point, not a summary of one.

### Added

- The guarded write path: `POST /remember`, `POST /learn`, `POST /supersede` run
  normalize → structural → NLI → judge and return `409` on a real contradiction instead
  of silently storing both sides.
- The fact fast path: `POST /ingest` — chunk, embed, and write, no contradiction check.
- Reads: `GET /recall` (hybrid vector + keyword search), `GET /why` (reasoned-claim
  retrieval, including full supersede history), `GET /active`, `GET /history`,
  `GET /audit`.
- Wrong-at-birth removal: `POST /retract` — no gate, no new row; the claim leaves the
  active set but stays visible in history as retracted.
- Bulk transfer: `GET /export` and `POST /import` (import re-runs the same guard on
  every incoming claim).
- Operational endpoints: `GET /health`, `GET /ready`, `GET /diagnose`, `GET /metrics`.
- A stdio MCP adapter exposing one `ergo_*` tool per endpoint above (14 tools, matching
  the 14 tenant-facing routes).
- A single all-in-one Docker image: the embedder and NLI judge are baked in at build
  time, so there's no PyTorch at runtime; the only outbound network call at runtime is
  the optional prose-normalization LLM call, and the server falls back to an offline
  rule-based normalizer when none is configured.
- A fail-closed default posture: with no `ERGO_API_TOKEN` set, the server refuses to
  start on a non-loopback bind at all (see `SECURITY.md`).
