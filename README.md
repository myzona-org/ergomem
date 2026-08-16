<div align="center">

<img src="docs/assets/ergo-logo.png" alt="Ergo logo" width="320">

# Ergo

### Memory that remembers _why_ — and catches itself contradicting. 🧠

**Contradiction-guarded decision memory** — reasons, history, and a write-time firewall, in one offline SQLite image.

[**ergomem.com**](https://ergomem.com) &nbsp;·&nbsp; [HTTP API](docs/HTTP-API.md) &nbsp;·&nbsp; [MCP tools](docs/MCP.md)

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-sqlite--vec-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-one_image-2496ED?logo=docker&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-stdio_adapter-6366f1)
![License](https://img.shields.io/badge/License-Non--Commercial-blue)

</div>

---

Most agent-memory tools store notes and do similarity search. Ergo adds the part nobody ships as a first-class type: every belief can carry its **reason**, keep its **history**, supersede with a **why-it-changed**, and get **audited for contradictions**.

## Why it's different

- **Reasons are first-class.** A `reason`-less claim can never win a `why` query — only `recall`; `why` returns the rationale + full supersede history, not just the matching text.
- **Contradictions are caught at write time.** A guarded write runs `normalize → structural → NLI → judge` and returns **409 on a real conflict** instead of silently storing both sides.
- **History you can't lose.** Decisions are append-only — you change them via `supersede` (which keeps the why-it-changed chain), never by overwriting.
- **One file, offline by default.** One SQLite file (+ sqlite-vec), embedder + NLI baked into a single Docker image; the only optional network call is prose-normalization.

## Architecture & data flow

<div align="center">

[**View the interactive architecture diagram →**](docs/architecture.html)


</div>

The guarded **write path** (`remember` / `learn` / `supersede`) runs the contradiction firewall — `normalize → structural → NLI → judge → clean / warn / 409 block` — while `/ingest` takes the **fact fast-path** (chunk → embed → write, no gate). Reads (`recall` / `why`) embed the query, run sqlite-vec KNN, and join against active claims. It's **one SQLite file** holding both the claim store and the KNN index.

Interactive version: [`docs/architecture.html`](docs/architecture.html) &nbsp;·&nbsp; full walkthrough: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## The model — one store, two behaviors

| kind | behavior |
|---|---|
| `fact` | upsert freely, reason optional |
| `decision` / `constraint` / `rejection` / `convention` | append-only, **reason strongly recommended** (a reason-less claim is never rejected but can never win `why`), change only via `supersede` |

One SQLite file. One embedding space. One backup.

## Quickstart — one image, no setup

Everything (app + sqlite-vec + fastembed bge-small + NLI deberta-v3) is baked into one
image. The build runs a **no-network boot test** that hard-fails if any model load tries
to reach the network. The runtime’s only outbound call is the instruct LLM (Anthropic by
default), and only when `/remember` or `/supersede` needs prose-normalization. If no LLM
provider/key is configured, the server falls back to the offline `RuleNormalizer` — so
`/remember` still works with zero LLM setup, just with weaker extraction.

The server listens on **port 8000 inside the image**; the quickstart publishes it on host
port **8788**, loopback-only (`-p 127.0.0.1:8788:8000`), which is also the MCP adapter's
default `ERGO_API_URL`. To reach it from another machine, bind a real interface
(`-p 8788:8000`) **and** set `ERGO_API_TOKEN` — never run a write API reachable from the
network with no token.

```bash
docker pull ghcr.io/myzona-org/ergomem:0.1.2   # multi-arch (amd64 + arm64), models baked in
docker tag ghcr.io/myzona-org/ergomem:0.1.2 ergo

# or build it yourself instead (downloads + bakes the embedder + NLI, needs network once):
# docker build -t ergo .
```

```bash
docker run -d --name ergo -p 127.0.0.1:8788:8000 \
  -e ERGO_API_TOKEN=*** \
  -e ERGO_OWNER="you@example.com" \
  -v ergo-data:/data \
  ergo

TOK="Authorization: Bearer ***"
curl -s http://127.0.0.1:8788/health

# ingest a document (fast, no LLM gate)
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/ingest \
  -d '{"org_id":"acme","project":"kb","who":"alice","text":"...big markdown blob..."}'

# remember a decision (gated, contradiction-checked)
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/remember \
  -d '{"org_id":"acme","project":"kb","who":"alice",
       "statement":"Deploy on Tuesdays","reason":"team is on-call Mon/Wed"}'

# recall + why
curl -s -H "$TOK" "http://127.0.0.1:8788/recall?org_id=acme&project=kb&q=when%20to%20deploy&limit=3"
curl -s -H "$TOK" "http://127.0.0.1:8788/why?org_id=acme&project=kb&q=when%20to%20deploy"
```

The `/data` volume holds the SQLite file (`ergo.db`). Back it up with the
SQLite online-backup API or `VACUUM INTO` — never a raw `cp` while the
engine is running, since WAL mode would corrupt a byte-copy. The published image is a **multi-arch manifest** (`linux/amd64` +
`linux/arm64`), so `docker pull` selects the right one; a local
`docker build` only ever produces the platform you build on.

### Air-gapped (no Anthropic)

Swap the instruct LLM for a local Ollama at boot — same image, different env:

```bash
docker run -d --name ergo -p 127.0.0.1:8788:8000 \
  -e ERGO_API_TOKEN=*** \
  -e ERGO_LLM_PROVIDER=ollama \
  -e ERGO_LLM_BASE=http://host.docker.internal:11434 \
  -e ERGO_LLM_MODEL=qwen2.5:7b \
  -v ergo-data:/data \
  ergo
```

## MCP — stdio adapter

For agent hosts (e.g. Claude Code) there's a **stdio MCP server** that exposes one tool
per endpoint — 14 total: `ergo_remember`, `ergo_learn`, `ergo_ingest`, `ergo_supersede`,
`ergo_retract`, `ergo_recall`, `ergo_why`, `ergo_active`, `ergo_history`, `ergo_audit`,
`ergo_diagnose`, `ergo_health`, `ergo_export`, `ergo_import`. It is a
**thin HTTP client** over the API below (it never opens `ergo.db` directly, so the engine
stays the single source of truth).

```bash
pip install ".[mcp]"                 # mcp SDK + httpx (both pinned)

ERGO_API_URL=http://127.0.0.1:8788 \
ERGO_API_TOKEN=*** \
  python -m ergo.mcp.server          # speaks MCP over stdio
```

Default scope: `org_id="claude-code"`, `project`=git-repo basename of cwd,
`who="claude-code"` (all overridable per call). Register it with a host via
[`.mcp.json.example`](.mcp.json.example); details in [`docs/MCP.md`](docs/MCP.md).

## Reference

<details>
<summary><strong>The HTTP API — 13 tenant-facing endpoints (+ <code>/ingest</code> fast-path)</strong></summary>

<br/>

The engine's only public surface is a small, **bounded-threaded stdlib `http.server`**
(no FastAPI/Flask — zero web-framework deps; a `ThreadingHTTPServer` where each
admitted request gets its own thread, gated by one shared, process-wide admission
bound — `ERGO_HTTP_MAX_WORKERS`, default 8 — so a connection burst can't spawn
unbounded threads) wrapping the SQLite store. **13 + `/ingest` = 14** — one per
[MCP tool](docs/MCP.md) above. Not counted here: `/ready` and `/metrics` (ops-only
liveness/telemetry, no principal resolved). The core
verbs:

| verb | endpoint | does |
|---|---|---|
| `remember` | `POST /remember` | guarded write (decision = append + reason; runs normalize → structural → NLI → judge; **409 on block**) |
| `learn` | `POST /learn` | guarded write **from a source** — same gate as `remember`, with `source` (required) folded into the reason for provenance; bridges `ingest` → `remember` (can 409) |
| `supersede` | `POST /supersede` | "changed my mind because…" — new row, old → superseded, linked; **also contradiction-guarded** (can 409) |
| `retract` | `POST /retract` | "this was wrong at birth" (typo / test junk / mis-scoped write) — flips the claim to `retracted`, **reason required**, audit row, no gate; a *changed* decision still goes through `supersede` |
| `recall` | `GET /recall?org_id&project&q&limit` | hybrid semantic (sqlite-vec cosine) + keyword search over active claims |
| `why` | `GET /why?org_id&project&q` | best **reasoned** belief + reason + full supersede history |
| `active` | `GET /active?org_id&project` | list active claims (`project_missing` / `empty` / `has_active_claims`) |
| `history` | `GET /history?org_id&project` | full history — active + superseded + retracted, by `claim_seq` |
| `audit` | `GET /audit?org_id&project&limit` | tenant self-serve read of its **own** `writes` + `conflict_events` rows (private-repo issue #124) |
| `diagnose` | `GET /diagnose` | live counts, conflict telemetry, version-drift, owner/backup/kill-review |
| `health` | `GET /health` | liveness + allowlisted config (**no auth**; ownership/ops metadata stays on `diagnose`) |
| `export` | `GET /export?org_id&project` | export the scope's active claims (no vectors, no history) — pairs with `import`, never gated in this build |
| `import` | `POST /import` | contradiction-checked merge of exported claims — every claim runs the SAME gate as `remember`/`learn`, so conflicts are reported and skipped, never force-written; always synchronous in this build |

Plus a fast document path:

| extra | endpoint | does |
|---|---|---|
| **ingest** | `POST /ingest` | chunk + embed + `remember_fact` (skips LLM normalize/judge — facts upsert freely) |

Full request/response shapes: [`docs/HTTP-API.md`](docs/HTTP-API.md).

Auth: bearer-token via `Authorization: Bearer $ERGO_API_TOKEN` (set `ERGO_API_TOKEN`
at boot; `/health` stays open). If the var is unset, a loopback-only bind
(`127.0.0.1`, `::1`, or `localhost`) still runs **open** (dev/test only) and warns
at boot; a non-loopback bind **refuses to start at all** unless
`ERGO_INSECURE_NO_AUTH=1` is also set.

</details>

<details>
<summary><strong>Embedding providers — bring your own</strong></summary>

<br/>

You don't need a local GPU lab. The default embedder (fastembed bge-small) is baked
into the image and runs offline; the other providers below are alternates configurable
at boot via one env var.

| `ERGO_EMBED_PROVIDER` | needs | default model | dim |
|---|---|---|---|
| `fastembed` (default, baked) | nothing — local, offline | bge-small-en-v1.5 | 384 |
| `ollama` | reachable Ollama server | nomic-embed-text | 768 |
| `openai` | `OPENAI_API_KEY` | text-embedding-3-small | 1536 |
| `cohere` | `COHERE_API_KEY` | embed-english-v3.0 | 1024 |
| `voyage` | `VOYAGE_API_KEY` | voyage-3-lite | 512 |

The DB locks its dim at first write. Switching providers later requires a re-embed
migration; the engine refuses writes on a mismatch instead of silently mixing
embedding spaces. Add a provider by registering one class in `ergo/embedders.py`.

</details>

<details>
<summary><strong>Note: v1 (Postgres CLI) retirement — 2026-06-27</strong></summary>

<br/>

> The Postgres+pgvector v1 CLI (`ergo init/remember/recall/...`) has been retired.
> The engine is now a self-contained Docker image (SQLite + sqlite-vec, embedder + NLI baked) exposed
> over a small HTTP API (stdlib `http.server`, no web framework). The core verbs are still there —
> they're now HTTP endpoints (see above). The old *in-process* MCP and Matrix-import paths are gone
> with the v1 tree, but a new **stdio MCP adapter** (`python -m ergo.mcp.server`) now ships as a thin
> HTTP client over the same API — see [MCP — stdio adapter](#mcp--stdio-adapter) above.

</details>

## Why "Ergo"

Latin for *therefore*. The point of the product in one word.
