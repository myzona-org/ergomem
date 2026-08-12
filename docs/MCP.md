# MCP Adapter

A native [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes the Ergo HTTP API as typed tools, so MCP hosts (Claude Code, etc.) reach
Ergo through native tool-calls instead of shelling out to `curl`.

- **Source:** [`ergo/mcp/server.py`](../ergo/mcp/server.py)
- **Launcher:** [`ergo/mcp/ergo-mcp-launcher.sh`](../ergo/mcp/ergo-mcp-launcher.sh)
- **Transport:** stdio · **SDK:** official `mcp` Python SDK · **HTTP client:** `httpx`

## Architecture invariant

The MCP server is a **thin client over the HTTP API** ([HTTP-API.md](HTTP-API.md)).
It calls `ERGO_API_URL` with `Authorization: Bearer $ERGO_API_TOKEN`. It does **not**
import `ContradictionStore`, open `ergo.db`, or touch the DB directly — the HTTP API
stays the single source of truth; MCP is just another transport. This preserves the
MVP invariant: *keep CLI/MCP working through the API.*

## Tools (one per endpoint)

| Tool | Endpoint | Notes |
|---|---|---|
| `ergo_remember` | `POST /remember` | Guarded write. A contradiction comes back as structured `result:"conflict"` (not an error) — resolve by cancel / `ergo_supersede` / retry with `force_exception`. |
| `ergo_learn` | `POST /learn` | Belief formed FROM a source: same contradiction gate as `ergo_remember`, with `source` (required) folded into the reason so provenance is retrievable via `ergo_why`/`ergo_recall`. Bridges `ergo_ingest` → `ergo_remember`. |
| `ergo_ingest` | `POST /ingest` | Bulk doc fast-path; skips the contradiction gate. |
| `ergo_supersede` | `POST /supersede` | Change of mind; also gated, can return a conflict. |
| `ergo_retract` | `POST /retract` | Wrong-at-birth removal (typo / test junk / mis-scoped write): flips the claim to `status='retracted'`. `reason` required, no contradiction gate; a missing/already-retracted id surfaces as structured `result:"error"` with `status_code:404`. Use `ergo_supersede` when the decision *changed*. |
| `ergo_recall` | `GET /recall` | Hybrid semantic search. Response carries an additive `scope:{project,source,state}` (private-repo issue #184) naming the project actually searched and whether it exists — an empty `results` alongside `state:"project_missing"` is not the same as `state:"empty"`. |
| `ergo_why` | `GET /why` | Best reasoned belief + supersede history. Same additive `scope` object as `ergo_recall`. Always carries `score` on a real match; below `WHY_RELEVANCE_FLOOR` returns `why:null` plus an additive `suppressed:{reason,score,floor}` object instead of a low-confidence guess (private-repo issue #202) — see [HTTP-API.md](HTTP-API.md#relevance-floor-private-repo-issue-202). A suppressed answer means "ask a more specific question," not "nothing is decided here"; don't fabricate a rationale when you see it. |
| `ergo_active` | `GET /active` | Enumerate active claims, capped by default (private-repo issue #185): a bounded page, `limit`/`offset` to page through the rest. Response always carries `count` (the scope's true total), `returned`, `truncated`, `truncated_reason`, `next_offset` so truncation is loud, never silent — `state` describes the scope, not the page. `limit` clamped server-side to `[1, 500]`, default 50. `projection="summary"` (private-repo issue #206, opt-in — default `"full"`) reduces each claim to `{id, statement}`, omitting `reason`; prefer it when YOU are the context-ceilinged caller, since MCP's own double-encoding roughly doubles a verbose corpus's page on the wire. A ~50 KB served-JSON byte budget (not a param — see [HTTP-API.md](HTTP-API.md#summary-mode--byte-budget-private-repo-issue-206)) can also narrow a page short of `limit`; `truncated_reason` says which bound fired (`"row_limit"` vs `"byte_budget"`) so you know whether raising `limit` would even help. |
| `ergo_history` | `GET /history` | Full history incl. superseded + retracted. |
| `ergo_audit` | `GET /audit` | Tenant-facing audit trail (private-repo issue #124): the caller's own `writes` + `conflict_events` rows only — same tenant-isolation guarantee as `ergo_active`/`ergo_history`. `limit` caps each stream, clamped to `[1, 1000]`. |
| `ergo_diagnose` | `GET /diagnose` | Ops view; report-only. |
| `ergo_health` | `GET /health` | Liveness; reachable with no bearer, but the model-stack/`db_path` detail is behind auth (private-repo issue #187) — an unauthenticated or invalid bearer gets `status`/`retrieval`/`probe_state` only. |
| `ergo_export` | `GET /export` | Never gated in this build. Exports the scope's active claims (no embedding vectors — those are recomputed on import), each with its supersede history nested as an oldest-first `revisions[]` (private-repo issue #189; omitted for a head never revised). Retracted claims are excluded entirely, never even inside `revisions`. `limit`/`offset` (private-repo issue #203) are opt-in and, unlike `ergo_active`, **omitted means UNBOUNDED** — the full scope in one call, so an existing caller never silently gets a truncated file. Opting in gets the same `claim_count`/`returned`/`truncated`/`next_offset` loud-truncation shape as `ergo_active` (clamped to `[1, 200]`, default `25` — an export head is heavier than an `ergo_active` row), plus an `as_of_seq` snapshot marker for detecting a scope written to mid-walk across pages. Pairs with `ergo_import`. |

| `ergo_import` | `POST /import` | Always synchronous in this build — no plan gate, no background job queue. Each claim runs through the same guarded path as `ergo_remember`/`ergo_learn` (exact duplicates skipped, blocking contradictions reported in `conflicts[]` and left unstored); a claim's `revisions[]`, if present, is folded into its stored `reason` (bounded, truncation announced inline) rather than dropped (private-repo issue #189). `target_project` remaps every claim into one project. |


14 tools total, one per tenant-facing HTTP route. There is no `/admin/*` in this build.


### Scope defaults

Mirrors the retired curl skill: `org_id="claude-code"`, `project` = the git
**repository**'s basename, resolved from the **launch cwd** via the git common
dir (`git rev-parse --path-format=absolute --git-common-dir`) so a `git worktree`
checkout maps to its parent repo's name rather than the worktree's own directory
name — falling back to the toplevel basename (older git without `--path-format`,
or not a repo at all → `basename(cwd)`). `who="claude-code"`. Every default is
overridable per call. The launcher deliberately does not `cd`, so `project` tracks
whichever repo the host launched the server from.

**Hosted (streamable-HTTP) mode is different** (DEFAULT-PROJECT, private-repo issue #179): the
cwd/git-basename derivation above is meaningless when a single process serves
every tenant, so an omitted `project` is no longer defaulted by this adapter at
all — it's simply left out of the outgoing request, and the **engine** resolves
it instead: the engine's own
`"default"` fallback with a warning appended to the response. An explicit
`project` argument always wins either way. See [HTTP-API.md's Project scope
resolution](HTTP-API.md#project-scope-resolution-default-project-private-repo-issue-179) for
the full resolution order — fixing this at the engine (not here) also covers
raw HTTP API callers, not just MCP.

Because that resolution difference is real, the two transports also ship
**different `initialize` instructions** (private-repo issue #184): stdio's `_INSTRUCTIONS`
tells the model `project` defaults to the cwd-derived basename above (true for
stdio); `build_app()` (streamable-HTTP only) instead uses a separate
`_HOSTED_INSTRUCTIONS` string that states the real hosted resolution order
(explicit `project` → the token's own `default_project` → the literal
`"default"` fallback) and says nothing about a cwd, since there isn't one to
inspect. Before this fix the hosted transport shipped the stdio wording
verbatim — authoritative-looking, model-facing text asserting a default that
doesn't exist over HTTP. Check `ergo_recall`/`ergo_why`'s `scope` field (above)
to see which project a given call actually searched, in either mode.

## Error envelope (private-repo issue #186)

Every tool call is a JSON-RPC request over MCP, and JSON-RPC-over-HTTP has a different
error contract than the plain REST API this adapter wraps ([HTTP-API.md](HTTP-API.md)):
**the transport call itself succeeds (`HTTP 200`, JSON-RPC `result` present) even when the
underlying REST call to the engine failed** — the failure lives *inside* the tool's result
payload, not the HTTP status. This is correct, conventional JSON-RPC-over-HTTP and is not
changing; it just needs to be documented where a client author will actually hit it.

Every tool wraps a non-2xx REST response (`_err()` in `ergo/mcp/server.py`) as:

```json
{"result": "error", "status_code": 401, "error": {"error": "unauthorized"}}
```

- `result` is always `"error"` for this shape (contrast with a successful call, whose
  result is the tool's normal return value, and with the three guarded-write tools'
  documented `result:"conflict"` — a 409 is a normal outcome, not this error shape).
- `status_code` is the REST status the underlying HTTP call actually got.
- `error` is that REST response's own parsed JSON body, whatever shape that endpoint uses
  elsewhere (see [HTTP-API.md](HTTP-API.md) for each route's error bodies) — **there is no
  fixed `state` key**, so don't guess at the shape; branch on `status_code` if you need to.

**Worked example — a revoked/expired key**, the case most likely to trip up a naive
wrapper: calling any `ergo_*` tool with a dead token returns `HTTP/2 200` with the body
above (`status_code: 401`). A wrapper that only checks the transport status code (e.g.
`curl -w '%{http_code}'`, or any HTTP client's `.status_code`/`.ok`) reads this as success —
the key looks alive when it's fully revoked. **Check `result` and `status_code` in the
payload; never the HTTP status** for anything going over this MCP/JSON-RPC transport. This
is the opposite of the plain REST routes in [HTTP-API.md](HTTP-API.md#auth), where the HTTP
status IS authoritative — don't apply REST-style status-code checking to MCP calls, and
don't apply this payload-checking style to plain `curl`/REST calls either.

## Configuration (env passthrough)

| Var | Default | Purpose |
|---|---|---|
| `ERGO_API_URL` | `http://127.0.0.1:8788` | Engine base URL |
| `ERGO_API_TOKEN` | *(unset)* | Bearer token. **Never commit it.** Injected via the host's `mcpServers.<name>.env`. |

## Install + register with Claude Code

```bash
pip install -e ".[mcp]"        # mcp==1.29.0, httpx==0.28.1 (pinned)

# User scope (available in every project), token injected via env — not committed:
claude mcp add ergo --scope user \
  --env ERGO_API_TOKEN="$ERGO_API_TOKEN" \
  --env ERGO_API_URL="http://127.0.0.1:8788" \
  -- /abs/path/to/ergo/ergo/mcp/ergo-mcp-launcher.sh

claude mcp get ergo            # expect: Status: ✔ Connected
```

Or copy [`.mcp.json.example`](../.mcp.json.example) to `.mcp.json` (project scope) —
it uses `${ERGO_API_TOKEN}` expansion so no secret lands in git.

## Tests

- **Unit (HTTP mocked):** `tests/mcp_adapter/test_server.py` — schema/defaults/auth +
  409-conflict surfacing, via `httpx.MockTransport`. No network, no DB.
- **Live:** drive the registered launcher with any MCP client (`initialize` →
  `tools/list` → `tools/call`) against the running container.

```bash
python -m pytest tests/mcp_adapter/ -q
```

## History

The native MCP adapter **superseded the `ergo-memory` curl skill on 2026-06-27**.
A snapshot of the retired skill is kept in the private repo for rollback reference.
