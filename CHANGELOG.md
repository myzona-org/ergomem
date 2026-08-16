# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates get
added to a version heading once that version is actually tagged and published — this
file is written ahead of that happening, and an invented date would be less honest than
none.

## [Unreleased]

Nothing yet — entries land here as changes ship after the release below.

## [0.1.2] — 2026-08-16

- The published image now carries `org.opencontainers.image.created`, `.revision` and
  `.version`, at both per-architecture and manifest-index level, so a pulled image can be
  traced back to the commit that built it. `0.1.0` and `0.1.1` shipped with all three empty.

## [0.1.1] — 2026-08-16

- Quickstart leads with `docker pull ghcr.io/myzona-org/ergomem:0.1.1` instead of a
  from-source build; the from-source path stays as the alternative.
- The published image is built and pushed by this repository's own Actions workflow
  (multi-arch `linux/amd64` + `linux/arm64`), not by hand from a laptop.
- Docs no longer claim an OCI `image.source` label is what links the package to this
  repo — only an Actions-pushed package auto-links.
- `tools/ergo-ingest-repo/ergo-ingest-repo` ships as the symlink it is upstream, instead
  of a second copy of `ergo_ingest_repo.py`.
- Fixed 5 ReDoS findings in the normalizer, the ingest tool, and the OpenClaw client.

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
