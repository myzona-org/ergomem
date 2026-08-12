# HTTP API Reference

The engine's only public surface. Bounded threaded stdlib server (`http.server`'s `ThreadingHTTPServer`, capped by `ERGO_HTTP_MAX_WORKERS`, default 8) wrapping per-tenant `ContradictionStore` instances.

- **Default bind:** `0.0.0.0:8000` (override with `ERGO_HTTP_HOST` / `ERGO_HTTP_PORT`)
- **Inside the image:** port 8000. Typical host publish, loopback-only: `-p 127.0.0.1:8788:8000`.
- **Source:** [`ergo/contradiction/server.py`](../ergo/contradiction/server.py)

## Auth

Every route except `GET /health` and `GET /ready` resolves the bearer token server-side to a **principal**
(`org_id`, `who`, `role`) before doing anything else:

```
Authorization: Bearer <token>
```

There is exactly one kind of bearer: a token equal to `ERGO_API_TOKEN`, or (if
`ERGO_API_TOKEN` is unset entirely) **any** bearer at all when bound to a loopback
address (`127.0.0.1`, `::1`, or `localhost`) — the server logs a `WARNING` at boot and
runs **open** (dev/test only). A non-loopback bind with no token **refuses to start at
all** unless `ERGO_INSECURE_NO_AUTH=1` is also set. `org_id`/`project`/`who` are honored
from the request body, routed to the single store at `ERGO_DB_PATH`. There is no tenant-token
lookup, no superadmin token, and no `/admin/*` — this build has no control plane.

`GET /health` and `GET /ready` always work without auth (private-repo issue #187). `GET /metrics`
(ergo's private-repo issue #121) has its own operator-only gate — see [its own
section](#get-metrics--prometheus-text-exposition-ergo-private-repo-issue-121).

There are no tenant quotas and no plan-based metering in this build — every route runs
unmetered, and `POST /import` has no plan gate.


## Checking for an error: REST vs MCP/JSON-RPC (private-repo issue #186)

Everything documented on this page is the **plain REST transport** — for these routes the
HTTP status code **is authoritative**. `401` means unauthorized, `409` means conflict, `200`
means success; a caller can and should branch on the status code alone (`curl -w
'%{http_code}'`, `response.status_code`, etc.).

**The MCP/JSON-RPC transport (`ergo/mcp/server.py`, see [MCP.md](MCP.md)) is different, and
is NOT changing.** MCP tools call this same REST API internally, but every non-2xx REST
response is caught and re-surfaced as a **successful** JSON-RPC tool result whose *payload*
carries the failure — this is conventional JSON-RPC-over-HTTP (transport succeeded; the
error lives in the envelope), not a bug. Concretely, a revoked/expired token calling an
`ergo_*` MCP tool gets:

```
HTTP/2 200
{"result": "error", "status_code": 401, "error": {"error": "unauthorized"}}
```

`status_code` mirrors the REST status the underlying HTTP call actually got (`401` here);
`error` is that REST response's own parsed JSON body verbatim (whatever shape that route's
error uses elsewhere on this page — e.g. `{"error": "unauthorized"}` for a 401, `{"error":
"tenant '...' is over its monthly write cap"}` for a 403, etc.), not a fixed schema of its
own. **There is no `state` key** — a client that assumes one (a reasonable guess, but wrong)
will crash parsing this payload instead of handling it.

**A caller over MCP/JSON-RPC must check `result` (and, on `"error"`, `status_code` inside
the payload) — never the HTTP status code**, which will read `200` for both a genuine
success and an authenticated-but-failed call alike. A wrapper written the REST way (`curl -w
'%{http_code}'`, checking only the transport status) will misreport a fully revoked key as
healthy. See [MCP.md](MCP.md#error-envelope-private-repo-issue-186) for the client-author-facing version
of this same warning plus the concrete payload shape.

409 (conflict) from the three guarded-write tools (`ergo_remember`/`ergo_learn`/
`ergo_supersede`) is the one exception worth calling out explicitly: it is a **documented,
non-error outcome** of the contradiction gate, so those tools return it as a normal
`result:"conflict"` payload (not the `result:"error"` shape above) — see their own sections
below.

## Project scope resolution (DEFAULT-PROJECT, private-repo issue #179)

`project` is **optional** on every tenant-facing route that takes one (`/remember`,
`/learn`, `/supersede`, `/retract`, `/ingest`, `/active`, `/history`, `/audit`,
`/recall`, `/why`) — matching every MCP tool's own signature. When a request omits
it, the engine resolves it in order, and an explicit `project` argument **always
wins**:

1. The request's own `project` field/query-param, if given (any non-blank string).

3. The literal fallback `"default"` — but this branch appends a warning to the
   response's `warnings` field (the same channel `/remember`/`/learn`/`/supersede`
   already carry for MED-tier judge hits; the routes without one — `/ingest`,
   `/retract`, and the GET reads — gain a `warnings` key **only** when there's
   something to report).

This is deliberately **never** a `400` — tightening the no-project case to a hard
error is an explicit possible later phase, not this one. A legacy/superadmin
principal (no `tokens` row, so no `default_project`) that omits `project` falls
straight through to step 3.

### Scope visibility on `/recall` and `/why` (private-repo issue #184)

An empty `/recall`/`/why` result is otherwise indistinguishable from "this project
exists and just has nothing relevant" — dangerous, since the whole point of a
recall-before-you-reverse-a-decision workflow is telling those two cases apart.
Both routes echo the resolved scope as an **additive** `scope` object alongside
their existing body:

```json
{ "results": [],
  "scope": { "project": "default", "source": "fallback", "state": "project_missing" },
  "warnings": ["..."] }
```

- `project` — the resolved project string that was actually queried (step 1/2/3 above).
- `source` — which resolution step won: `"explicit"` | `"token_default"` | `"fallback"`.
- `state` — the SAME three-state existence signal `/active` already returns
  (`"project_missing"` | `"empty"` | `"has_active_claims"`, see `GET /active` below),
  reflecting the project's actual **claims**, never just a `projects`-table row —
  a project can hold thousands of active claims while a specific `/why` query (its
  own `reasoned_only` filter finds nothing) or `/recall` query (the global vector
  index's nearest-k all belong to another project) legitimately returns nothing, and
  `state` must still say `"has_active_claims"` in that case, matching what `/active`
  on the same scope would say. Only computed when the result set is empty — a
  non-empty result already proves `"has_active_claims"`, so the common case pays no
  extra cost.

`scope.state` describes the scope **as of this query**; it is read via a separate,
un-locked call made after `/recall`'s or `/why`'s own store access completes, so it
is not transactionally atomic with `results`/`why` — a concurrent write landing in
that gap can make it stale by the time the response is read. Acceptable for a
diagnostic field; not worth adding locking for.

`/active`, `/history`, and `/audit` are out of scope for this field — `/active`
already has its own three-state signal, and `/history`/`/audit` are a possible
follow-up.

## Endpoints

### `GET /health` — liveness

No auth required to REACH the route — it never 401s, even with an invalid or missing
bearer, because monitors (Docker `HEALTHCHECK`, uptime checkers) must be able to hit it
with zero credentials. The **body**, however, is split by whether the bearer resolves to a
principal (private-repo issue #187): ownership/ops metadata (`owner_name`, `owner_contact`,
`backup_path`, `kill_review_date`, `last_write_at`) was never here — that's the
bearer-authed `GET /diagnose` — and the model-stack/on-disk-path detail below now joins
it behind the same gate.

**No bearer, or an invalid/revoked one** (still `200`, never `401` — that would break every
monitor that never carries a bearer):

```json
{
  "status": "ok",
  "retrieval": true,
  "probe_state": "project_missing"
}
```

`retrieval` stays public deliberately: `status: "ok"` + `retrieval: true` is the documented
session-start pre-flight every client relies on ("the whole API is available") — moving it
behind auth would break every agent's startup check.

**A valid bearer** (the legacy `ERGO_API_TOKEN` — same gate `/diagnose` uses) gets the full payload, unchanged from before:

```json
{
  "status": "ok",
  "db_path": "/data/ergo.db",
  "normalizer": "OpenAINormalizer(claude-haiku)",
  "nli": "Xenova/nli-deberta-v3-base:quantized",
  "embedder": "FastEmbed(dim=384)",
  "retrieval": true,
  "booted_at": "2026-06-27T14:23:33+0000",
  "probe_state": "project_missing"
}
```

If `ERGO_API_TOKEN` isn't configured at all (the documented dev/test open-API mode — see
[Auth](#auth) above), every caller resolves to the legacy principal regardless of bearer, so
`/health` returns the full payload unconditionally in that mode too — the same open posture
every other route already has when no credential is presented. A real deployment always sets
`ERGO_API_TOKEN`, so this can't become a leak in production.

`probe_state` is the cheap readiness probe — runs `active("_probe", "_probe")` to confirm the store responds. Three possible values: `project_missing` (healthy, empty), `empty`, `has_active_claims`. Anything else → 503. On a probe failure the body is `{"status": "unhealthy"}` (unauthenticated) or `{"status": "unhealthy", "error": "<str(e)>"}` (authed) — the status code (`503`) and the `status` value never change with auth state, only how much detail comes with it.

### `GET /ready` (also `HEAD /ready`) — write-path readiness

Same reachability contract as `/health`: no bearer (or an invalid one) never 401s. `/health`
only proves the DB is reachable — it never calls the normalizer, so a normalizer-provider
outage leaves `/health` green while every guarded write (`/remember`/`/learn`/`/supersede`)
is 503ing with `NormalizeUnavailable`. `/ready` does the SAME DB probe `/health` does,
**plus** an actual normalizer liveness call (`normalize()` with a fixed probe statement), so
it reflects write-path readiness rather than just liveness.

The body is split the same way `/health`'s is (private-repo issue #187): the normalizer-probe detail
below can carry a raw provider error string on the `degraded` path, so it moves behind the
same bearer gate — `status` + `probe_state` are the only fields an unauthenticated (or
invalidly-authenticated) caller gets. The status code and `status`/`probe_state` values
never change with auth state.

**No bearer, or an invalid one:**

```json
{
  "status": "ready",
  "probe_state": "has_active_claims"
}
```

**A valid bearer** (unchanged from before this split):

```json
{
  "status": "ready",
  "normalizer": {"ok": true},
  "probe_state": "has_active_claims",
  "checked_at": "2026-07-19T00:00:00Z",
  "cached": false
}
```

Status/body mapping (authed columns shown; an unauthenticated/invalid-bearer caller gets
only `status`+`probe_state` — or just `status` on a DB-probe failure — at the SAME status
code in every row):

| condition | status | authed body |
|---|---|---|
| DB probe + normalizer probe both OK | `200` | `{"status": "ready", "normalizer": {"ok": true}, "probe_state": ..., "checked_at": ..., "cached": ...}` |
| DB probe raises | `503` | `{"status": "unhealthy", "error": "<str(e)>"}` — matches `/health`'s failure shape exactly |
| normalizer raises `NormalizeUnavailable` | `503` | `{"status": "degraded", "normalizer": {"ok": false, "error": "<str(e)>", "retryable": <bool>}, "probe_state": ..., "checked_at": ..., "cached": ...}` |
| first-ever probe still in flight, wait budget exceeded | `503` | `{"status": "degraded", "normalizer": {"ok": false, "error": "first readiness probe still in progress...", "retryable": true}, "probe_state": ..., "checked_at": null, "cached": false}` |

As with `/health`, no `ERGO_API_TOKEN` configured at all means every caller gets the full
(authed) body — the documented open dev/test posture.

The normalizer probe is TTL-cached (`ERGO_READY_PROBE_TTL_SECONDS`, default 15s, lock-guarded
— see [CONFIGURATION.md](CONFIGURATION.md#auth--networking)): at most one real `normalize()`
call per TTL window regardless of hit rate, so this unauthenticated-reachable, LLM-calling
route can't become a cost/DoS vector for a monitor polling every few seconds or a flood of
anonymous requests. `0` disables the cache and probes every hit. `cached` in the response
tells you whether that hit paid for a fresh probe.

On the very first probe since boot (no cached result yet), a concurrent request waits behind
whichever request is already calling `normalize()`, but only up to
`ERGO_READY_FIRST_PROBE_WAIT_SECONDS` (default 3s — well under the normalizer's own 60-120s
request timeout). If that budget is exceeded, the request reports `checked_at: null` and gives
up rather than pinning a worker thread from the bounded pool (`ERGO_HTTP_MAX_WORKERS`) for
however long a hung provider takes. This only ever happens during that one narrow startup
window — once any probe has ever landed, later requests read the cache instead.

`HEAD /ready` mirrors `HEAD /health`: same status code, an accurate auto-computed
`Content-Length` for whichever body (slim or full) the auth state would have sent, and an
empty body.

### `POST /remember` — guarded write

The contradiction-checked path. Use for **decisions / constraints / rejections / conventions** — anything you'd want the engine to refuse if it contradicts existing belief.

Pipeline: `normalize → structural compare → NLI residual (gated) → judge tier → write-guard insert`.

**Request body:**

```json
{
  "org_id": "acme",
  "project": "platform",
  "who": "alice",
  "statement": "Deploy on Tuesdays",
  "reason": "team is on-call Mon/Wed",
  "force_exception": null
}
```

| field | required | description |
|---|---|---|
| `org_id` | yes | tenant id |
| `project` | no | project namespace within the tenant — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `who` | yes | actor (audit row) |
| `statement` | yes | the claim text |
| `reason` | no | why (free text; surfaced via `/why`) |
| `force_exception` | no | string. If set, stores even on conflict, with this string as the exception reason |

**Responses:**

| code | body | meaning |
|---|---|---|
| `200` | `{"result":"stored", "claim_id":"...", "warnings":[...]}` | written; any MED-tier warnings included |
| `409` | `{"result":"conflict", "conflicts":[...], "warnings":[...]}` | blocked; conflict set returned for client to resolve |
| `400` | `{"error":"missing field: ..."}` | missing required field |
| `503` | `{"result":"degraded", "error":"normalizer unavailable: ..."}` | the LLM normalizer's provider is infrastructurally unavailable (connection error, timeout, 429, 5xx, auth 401/403) — the write is refused so the contradiction check is never silently skipped. Retryable. Pass `force_exception` to store anyway (contradiction check skipped, audited) |
| `502` | `{"result":"degraded", "error":"normalizer unavailable: ..."}` | the provider answered with a PERMANENT client-error status (400/404/422/...) — a bad model name or malformed request, not a transient blip. Non-retryable. Pass `force_exception` to store anyway (contradiction check skipped, audited) |
| `413` | e.g. `{"error":"field 'statement' exceeds max length of 32768 chars"}` | `statement` or `reason` exceeds `ERGO_MAX_FIELD_CHARS` (default 32768); the offending field name is whichever one tripped it |

| `500` | `{"error":"..."}` | normalizer/judge/store failure (any other exception; malformed model OUTPUT still degrades to an all-unknown norm and stores normally, it does not 503/502) |

**Conflict body shape** (each item in `conflicts`):

```json
{
  "claim": { "id": "...", "who": "alice", "statement": "...", "reason": "...", "status": "active", ... },
  "tier": "block",
  "score": 0.97,
  "source": "structural",
  "reason": "same subject + opposing modality"
}
```

### `POST /learn` — guarded write with source provenance

The bridge between `/ingest` (raw reference chunks, **no** gate) and `/remember` (decisions, full gate). Use it when an agent **read something and formed a belief** — *"from reading `<source>`, I believe `<statement>`"*. Runs the **same contradiction pipeline as `/remember`**, and folds `source` into the stored reason so the provenance is retrievable via `/why` and `/recall` (which echo `reason`, not a separate source field).

The contradiction check runs on the `statement` only (the source never pollutes the judge). Because a learned claim always carries a reason, it can win a `/why` query.

**Request body:**

```json
{
  "org_id": "acme",
  "project": "platform",
  "who": "claude-code",
  "statement": "Sessions are never reused across gateway restarts",
  "source": "sessions.py:847",
  "reason": "observed in the restart path",
  "force_exception": null
}
```

| field | required | description |
|---|---|---|
| `org_id`, `who` | yes | as `/remember` |
| `project` | no | as `/remember` — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `statement` | yes | the belief to contradiction-check + store |
| `source` | **yes** | provenance (file:line, URL, doc id). This is what makes it a *learned* claim; a blank source is a `400` |
| `reason` | no | extra rationale. Stored as `"<reason>\n[learned from: <source>]"`; with no reason the stored reason is `"Learned from reading <source>."` |
| `force_exception` | no | string. If set, stores even on conflict (same as `/remember`) |

**Responses:** mirror `/remember`, with the source echoed back.

| code | body | meaning |
|---|---|---|
| `200` | `{"result":"learned", "claim_id":"...", "source":"...", "warnings":[...]}` | written |
| `409` | `{"result":"conflict", "source":"...", "conflicts":[...], "warnings":[...]}` | the belief blocks on an existing active claim |
| `400` | `{"error":"missing field: source"}` (or `"... non-empty source"`) | missing/blank required field |
| `503` | `{"result":"degraded", "error":"normalizer unavailable: ..."}` | same degraded-provider refusal as `/remember` (retryable); pass `force_exception` to override |
| `502` | `{"result":"degraded", "error":"normalizer unavailable: ..."}` | same permanent-provider-error refusal as `/remember` (non-retryable); pass `force_exception` to override |
| `413` | e.g. `{"error":"field 'source' exceeds max length of 32768 chars"}` | `statement`, `reason`, or `source` exceeds `ERGO_MAX_FIELD_CHARS`; the offending field name is whichever one tripped it |

| `500` | `{"error":"..."}` | normalizer/judge/store failure |

### `POST /ingest` — fast document path

Bulk reference content. **Skips** the LLM normalizer + contradiction judge (per the README rule: "fact = upsert freely"). Embeds each chunk and stores via `remember_fact()`. Use this for reference docs / corpora; use `/remember` for decisions you want contradiction-checked.

Requires `retrieval:true` (the embedder must be enabled). Returns 503 otherwise.

**Request body:**

```json
{
  "org_id": "acme",
  "project": "kb",
  "who": "alice",
  "text": "...markdown or plain text body...",
  "source": "TOOLS.md"
}
```

| field | required | description |
|---|---|---|
| `org_id`, `who` | yes | as `/remember` |
| `project` | no | as `/remember` — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `text` | yes | the document body. `ERGO_MAX_INGEST_BYTES` (default 8 MiB, larger than the default `ERGO_MAX_BODY_BYTES` used by every other route) caps the ENTIRE JSON request body, not `text` alone — leave headroom for `org_id`/`project`/`who`/`source` and JSON quoting/escaping overhead |
| `source` | no | provenance string; gets appended to each chunk as `[source: ...]` for retrieval citations. Capped at `ERGO_MAX_FIELD_CHARS` |

**Response 200:**

```json
{ "result": "ingested", "chunks": 49, "stored": 49, "source": "TOOLS.md" }
```

| code | body | meaning |
|---|---|---|
| `200` | `{"result":"ingested", "chunks":N, "stored":N, "source":"..."}` | written; gains a `"warnings":[...]` key only when `project` was omitted and defaulted (see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179)) |
| `400` | `{"error":"missing field: ..."}` | missing required field |
| `413` | `{"error":"field 'source' exceeds max length of 32768 chars"}` (or a request/body-too-large error) | `source` exceeds `ERGO_MAX_FIELD_CHARS`, the body exceeds `ERGO_MAX_INGEST_BYTES`, or the resulting chunk count exceeds `ERGO_MAX_INGEST_CHUNKS` (checked before any chunk is stored) |

| `503` | `{"error":"retrieval not enabled on this server"}` | `ERGO_RETRIEVAL` is off |
| `500` | `{"error":"..."}` | store failure |

Chunking is paragraph-aware and **word-safe** (never splits mid-word). Default chunk size ≈ 1200 chars with ≈ 150-char overlap. See [`ergo/contradiction/ingest.py`](../ergo/contradiction/ingest.py).

### `POST /supersede` — change of mind

Guarded re-write: replaces an existing active claim with a new one, **and runs the contradiction guard against all OTHER active claims first**. If the new claim would itself contradict another active claim, the supersede is refused (returns the conflict set).

There is no project-count check here: `existing_id` must already be an active claim in `(org_id, project)`, so its project always already exists.


`project` is optional, same as `/remember` — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted. Note that since `existing_id` must already be an active claim in the resolved `(org_id, project)`, an omitted `project` that falls back to a key default or `"default"` most likely just 404s if that's not where the claim actually lives — pass `project` explicitly when superseding a specific claim.

**Request body:**

```json
{
  "org_id": "acme",
  "project": "platform",
  "who": "alice",
  "existing_id": "c7ebd06676e34d90a42341067e023e37",
  "statement": "Deploy on Wednesdays",
  "reason": "Tuesdays now collide with the standup window"
}
```

**Response 200:**

```json
{
  "result": "superseded",
  "claim_id": "<new id>",
  "warnings": [...]
}
```

Side effects: old row → `status='superseded'`, new row inserted with `supersedes` pointing at the old id (this is what `/why` walks).

| code | body | meaning |
|---|---|---|
| `200` | `{"result":"superseded", "claim_id":"...", "warnings":[...]}` | written |
| `409` | `{"result":"conflict", "conflicts":[...], "warnings":[...]}` | the new statement still conflicts with another active claim; nothing was written |
| `404` | `{"error":"no active claim <existing_id> in (org=..., project=...)"}` | `existing_id` is not an active claim in scope |
| `400` | `{"error":"missing field: ..."}` | missing/blank required field |
| `413` | e.g. `{"error":"field 'statement' exceeds max length of 32768 chars"}` | `statement` or `reason` exceeds `ERGO_MAX_FIELD_CHARS`; the offending field name is whichever one tripped it |

| `503` | `{"result":"degraded", "error":"normalizer unavailable: ..."}` | the LLM normalizer's provider is infrastructurally unavailable (retryable) — unlike `/remember`/`/learn`, `/supersede` has **no `force_exception` override**, so this refusal is unconditional |
| `502` | `{"result":"degraded", "error":"normalizer unavailable: ..."}` | the provider answered with a permanent client-error status (non-retryable) — same no-override rule as the `503` row |
| `500` | `{"error":"..."}` | normalizer/judge/store failure |

### `POST /retract` — wrong-at-birth removal

Explicit retraction: flips an **active** claim to `status='retracted'`, removing it from the active belief set.

**Retract ≠ supersede.** Retract means *"this claim was wrong at birth — a typo, test junk, a mis-scoped write; it should never have been believed."* Supersede means *"this decision changed — replace it and keep the why-chain."* If a decision genuinely evolved, use [`POST /supersede`](#post-supersede--change-of-mind) so the history explains the change.

No contradiction gate runs (removal cannot create a contradiction), so no LLM-normalizer or embedder call either. The row is **not deleted**: it stays in `/history` with `status='retracted'` plus `retract_reason` and `retracted_at`, while `/active`, `/recall`, `/why`, and the contradiction judge all exclude it structurally (they filter on `status='active'`). Every retraction writes a `writes` audit row (`action='retract'`); no new claim row is inserted and no `claim_seq` is consumed.

**Request body:**

```json
{
  "org_id": "acme",
  "project": "platform",
  "who": "alice",
  "claim_id": "c7ebd06676e34d90a42341067e023e37",
  "reason": "test junk written against the prod scope"
}
```

| field | required | description |
|---|---|---|
| `org_id`, `who` | yes | as `/remember` (`who` lands in the audit row) |
| `project` | no | as `/remember` — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `claim_id` | yes | id of the **active** claim to retract |
| `reason` | yes | why it was wrong at birth — removal is audit-loud; a blank reason is a `400` |

**Responses:**

| code | body | meaning |
|---|---|---|
| `200` | `{"result":"retracted", "claim_id":"...", "retracted":true}` | removed from the active set; gains a `"warnings":[...]` key only when `project` was omitted and defaulted |
| `404` | `{"error":"no active claim <id> in (org=..., project=...)"}` | no **active** claim with that id in scope (missing, already retracted, or superseded) |
| `400` | `{"error":"missing field: ..."}` or `{"error":"retract requires a non-empty reason"}` | missing field / blank reason |
| `413` | e.g. `{"error":"field 'reason' exceeds max length of 32768 chars"}` | `reason` exceeds `ERGO_MAX_FIELD_CHARS` |

| `500` | `{"error":"..."}` | store failure |


### `GET /export` — export active claims

**Data portability is ungated, full stop** (private-repo issue #109). Reads active claims **straight off the store's DB** — deliberately not `/recall` (a semantic-search read over an embedded subset that would drop out-of-topic claims and never surfaces `source`).

**Query params:**

| name | required | description |
|---|---|---|
| `org_id` | yes (tenant token: implied by the token) | tenant |
| `project` | no | scope to a single project; omitted → every project in `org_id` |
| `limit` | no | opt into paging (private-repo issue #203); **omitted → UNBOUNDED, the full scope in one response** (see Pagination below) |
| `offset` | no | row-count offset into the head list, same semantics as [`GET /active`](#get-active--list-active-claims)'s `offset`; ignored when `limit` is omitted |

**Response 200:**

```json
{
  "ergo_export_version": 2,
  "org_id": "acme",
  "exported_at": "2026-07-17T22:10:03Z",
  "embedder": { "provider": "FastEmbedEmbedder", "dim": 384 },
  "claim_count": 2,
  "returned": 2,
  "truncated": false,
  "next_offset": null,
  "as_of_seq": { "platform": 41 },
  "claims": [
    {
      "id": "c7ebd06676e34d90a42341067e023e37",
      "project": "platform",
      "statement": "Deploy on Tuesdays",
      "reason": "team is on-call Mon/Wed",
      "source": null,
      "status": "active",
      "who": "alice",
      "tier": null,
      "created_at": "2026-07-01T12:00:00Z",
      "revisions": [
        {
          "statement": "Deploy on Wednesdays",
          "reason": "original on-call rotation",
          "who": "bob",
          "status": "superseded",
          "created_at": "2026-05-01T09:00:00Z"
        }
      ]
    }
  ]
}
```

Top-level `claims[]` is still exactly one entry per **active** claim ("head") — but private-repo issue #189 attaches that head's supersede history as a nested `revisions[]` list instead of dropping it. `revisions` is **omitted entirely** (never a null/empty placeholder) for a head that was never revised — most claims have no chain, and the export is already large. When present, `revisions` is **oldest-first**: `revisions[0]` is the very first claim ever made in that lineage, `revisions[-1]` is the one immediately before the current head — read top-to-bottom as the decision's narrative. Each entry is the plain `{statement, reason, who, status, created_at}` fields off the superseded row itself (its OWN reason at the time it was superseded, not the reason of whatever replaced it); a `revisions` entry never carries `id`/`project`/`source`/`tier` — those aren't re-stored as their own rows, they're folded into the imported claim's `reason` on the way back in (see `POST /import` below).

**Retracted claims are excluded entirely** — not as a top-level entry, and never inside any head's `revisions`. `retract` means wrong-at-birth (typo, test junk, mis-scoped write); re-exporting one would resurrect noise the user deliberately removed — that's a documented decision, not a gap. Retraction history remains available via [`GET /history`](#get-history--full-history-active--superseded--retracted). This falls out structurally: `retract()` only ever flips a claim that is CURRENTLY `active`, so a superseded row (the only kind that can appear inside `revisions`) can never have been retracted, and a retracted claim (having left `active`) is never itself a head.

**NO vectors are ever included** — `embedder` is metadata describing what *would* re-embed the claims on import, not the embeddings themselves; vectors are embedder/dimension-specific and are always recomputed on `POST /import`. `source` and `tier` are reserved fields, always `null` in Phase 1 (on heads AND on `revisions` entries) — neither is a persisted per-claim column today (a `/learn` claim's provenance lives folded into `reason`; `tier` only ever exists transiently on a conflict).

**Pagination (private-repo issue #203):** an unpaginated export blows an MCP client's tool-output ceiling on a large corpus (504 KB measured on a 176-claim tenant) — the same problem private-repo issue #185 fixed for `/active`, one route over. `limit`/`offset` reuse `/active`'s exact vocabulary and clamp-and-degrade contract (`limit` clamps to `[1, 200]`, degrading a non-numeric value to `25` — deliberately smaller than `/active`'s `50`/`500` pair, since an export head can carry a nested `revisions[]` chain and so runs heavier per-entry: the reported corpus averaged ~2,864 bytes/head against `/active`'s ~1,944 B/claim, and MCP double-encodes either way). **Unlike `/active`, omitting `limit` stays fully UNBOUNDED** rather than defaulting to a capped page — export is the backup/migration source of truth `POST /import` round-trips against, so a caller passing no `limit` must never be silently handed a truncated file it believes is complete. `returned`/`truncated`/`next_offset`/`claim_count` are always present regardless of whether `limit` was supplied, with `claim_count` being the scope's **total**, never just the current page — keep calling with `offset=next_offset` until `truncated` is `false`. Pagination is by HEAD, not by raw row: a head's full `revisions[]` chain always rides along inside its own page, atomically, regardless of where its superseded predecessors sit in insertion order — it can never be torn across a page boundary, which is what would otherwise corrupt a multi-page round trip through `POST /import`.

**Snapshot consistency (private-repo issue #203):** an export is meant to be a faithful snapshot, not a casual browse — `/active`'s OFFSET-based paging is fine to tear (merely confusing) but a torn export composed across concurrent writes could produce a file that never corresponded to any single instant of the store, and that file feeds directly into `POST /import`. `as_of_seq` (`{project: max_claim_seq}`, computed fresh on every call from the same rows the response is built from) is a **detectability marker**, not a lock: a multi-page caller should compare `as_of_seq` across the pages it fetches, since `/remember`/`/learn`/`/supersede` all consume a fresh `claim_seq` on write — if it's identical on every page, nothing in scope was created or revised mid-walk; if it changed, re-walk from `offset=0` before trusting the composed file as a clean snapshot. A single scoped `project` always gets a key here — `0` for an empty/never-used project, never a missing key, since the caller named exactly one project to report a baseline for; `project` omitted (whole-org) has no such single key to default, so an org with zero claims anywhere legitimately gets `{}`. This was chosen over cursor-based (`claim_seq`) paging specifically so `limit`/`offset` stay identical to `/active`'s row-count vocabulary rather than inventing a second one. Known, accepted gap: `retract()` flips a row's status in place **without** consuming a new `claim_seq` (by design), so a bare retract racing a walk — with no accompanying insert/supersede — won't move this marker; retraction is a rare, deliberate, administrative action, not the everyday write this catches, and this is the same blind spot the engine's own internal `preflight_seq` write-race guard already accepts.

**`ergo_export_version` stays `2`** — the pagination fields above are purely additive (`claims[]` itself is unchanged), so a v1/v2 importer that only ever reads known fields is unaffected; a version bump was judged unnecessary (contrast with private-repo issue #189's bump to `2`, which changed how a claim's own `revisions[]` needed to be interpreted). A v1 document (no `revisions` key anywhere) still imports unchanged via `POST /import` — the field is simply absent, not a breaking change.

| code | body | meaning |
|---|---|---|
| `200` | (above) | exported |
| `400` | `{"error":"org_id required"}` | legacy token with no `org_id` resolvable |

| `500` | `{"error":"..."}` | store failure |

### `POST /import` — contradiction-checked merge of exported claims (private-repo issue #122)

Runs every incoming claim **through the existing contradiction gate** — an "import
audit" — never a reimplemented or bypassed one: each claim is dispatched to the SAME
internal path `/remember` uses (or `/learn`, when the claim carries a non-empty
`source`, so provenance is folded into the reason exactly like a live `/learn` call).
Always synchronous — the merge runs inline in this request and the response is the
full result; there is no background job queue, no plan gate, and no per-plan size cap
in this build.


**Request body** (a full `GET /export` document — including a single page of a
paginated one, private-repo issue #203 — is accepted as-is; the extra
`ergo_export_version`/`exported_at`/`embedder`/`claim_count`/`returned`/
`truncated`/`next_offset`/`as_of_seq` fields are simply ignored):

```json
{
  "org_id": "acme",
  "target_project": null,
  "claims": [
    { "project": "platform", "statement": "Deploy on Tuesdays", "reason": "team is on-call Mon/Wed", "who": "alice" }
  ]
}
```

| field | required | description |
|---|---|---|
| `org_id` | yes (tenant token: implied by the token) | tenant |
| `claims` | yes | list of claim objects; each needs at least `statement` and a `project` (its own, or via `target_project` below) |
| `target_project` | no | remap **every** claim into this one project, overriding each claim's own `project` |

Per claim, in order (this is `_import_merge`, reused verbatim on every call): (1) if the claim carries a non-empty `revisions[]` (GET /export's nested why-chain, private-repo issue #189), it is **folded into `reason`** before anything else runs — see below; (2) an exact-text active-statement duplicate in scope is **skipped** (`skipped_duplicate`, same rule as `/ingest`'s idempotent re-ingest — never re-stored, never re-checked); (3) otherwise the claim runs the guarded write (`/remember`, or `/learn` if `source` is non-empty) — a blocking conflict is reported in `conflicts` and **never stored or forced**; a clean/warn result is stored.

**Folding `revisions[]` into `reason` (private-repo issue #189):** `_import_merge` only ever reads specific per-claim keys, so an unrecognized `revisions` key would otherwise be silently ignored — an export → import round trip would lose the whole why-chain narrative while still reporting a clean import. Instead, each entry (oldest-first, matching `GET /export`'s order) is rendered as `"- <statement> (<status> by <who> at <created_at>): <reason>"` and appended under a `\n[prior revisions:\n...\n]` block on the claim's own `reason`, mirroring the established `/learn` pattern of folding provenance into `reason` rather than a new column (`"...\n[learned from: <source>]"`). This is **bounded** to `ERGO_MAX_FIELD_CHARS` (`_max_field_chars()`, default 32 KiB) — a chain long enough to not fit is truncated to as many whole entries as fit, and the folded text **says so** (`[revisions truncated: chain exceeded the field-size cap]`) rather than trimming silently. A claim with no `revisions` key (or an empty one) is completely unaffected — `reason` passes through unchanged, exactly as before this issue.


**Response `200`**:

```json
{
  "imported": 5,
  "skipped_duplicate": 1,
  "conflicts": [
    { "statement": "Never deploy on Tuesdays", "against": "<existing claim id>", "reason": "same subject + opposing modality", "tier": "block" }
  ],
  "conflict_count": 1,
  "cap_reached": false
}
```

| code | body | meaning |
|---|---|---|
| `200` | (shape above) | merge ran, always synchronously |
| `400` | `{"error":"missing field: org_id"}` / `{"error":"missing field: claims (must be a list)"}` | missing required top-level field |
| `413` | ordinary request-body-too-large error for a raw body over `ERGO_MAX_INGEST_BYTES` | oversized request body |
| `500` | `{"error":"..."}` | store error |


### `GET /recall` — hybrid semantic search

Read-only. Requires `retrieval:true`.

**Query params:**

| name | required | default | description |
|---|---|---|---|
| `org_id` | yes | — | tenant |
| `project` | no | — | project — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `q` (alias `query`) | yes | — | the search text |
| `limit` | no | 10 | clamped to `[1, 50]` |

**Response 200:**

```json
{
  "results": [
    {
      "id": "...",
      "who": "alice",
      "statement": "...",
      "reason": "...",
      "project": "kb",
      "status": "active",
      "created_at": "2026-06-27T15:00:11Z",
      "score": 0.7613,
      "vec_sim": 0.7661,
      "kw": 0.75
    },
    ...
  ],
  "scope": { "project": "kb", "source": "explicit", "state": "has_active_claims" }
}
```

Score is `0.7 × vec_sim + 0.3 × kw` (cosine + word-overlap). Superseded / retracted claims are excluded structurally by the join on `status='active'`. See [docs/RETRIEVAL.md](RETRIEVAL.md). The response always gains a top-level `scope` object (see [Scope visibility](#scope-visibility-on-recall-and-why-private-repo-issue-184)), and gains a top-level `"warnings":[...]` key alongside `results` only when `project` was omitted and defaulted (see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179)).

Errors: `400` if `org_id` or `q` is missing, `413` if `q` exceeds `ERGO_MAX_FIELD_CHARS`, `503` if retrieval is off.

### `GET /why` — reasoned belief + history

Read-only. Requires `retrieval:true`. Like `/recall` but `reasoned_only=true` (a reason-less fact can never win a "why" query), returns only the top hit, and walks the supersede chain. Same error codes as `/recall` (400/413/503); the response always gains a top-level `scope` object (see [Scope visibility](#scope-visibility-on-recall-and-why-private-repo-issue-184)), and gains a top-level `"warnings":[...]` key alongside `why` in the same defaulted-project case.

**Query params:** `org_id`, `q` (no `limit`). `project` is optional — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted.

**Response 200 (match found):**

```json
{
  "why": {
    "claim": "Never deploy on Fridays.",
    "reason": "incident risk outweighs low traffic",
    "score": 0.6348,
    "history": [
      { "id": "<newest>",     "claim": "Never deploy on Fridays.", "reason": "incident risk..." },
      { "id": "<superseded>", "claim": "Deploy on Fridays.",       "reason": "low traffic" }
    ]
  },
  "scope": { "project": "kb", "source": "explicit", "state": "has_active_claims" }
}
```

**Response 200 (no reasoned claim in scope at all):**

```json
{ "why": null,
  "scope": { "project": "kb", "source": "explicit", "state": "empty" } }
```

**Response 200 (suppressed — below the relevance floor, private-repo issue #202):**

```json
{ "why": null,
  "suppressed": { "reason": "below_relevance_floor", "score": 0.4511, "floor": 0.5 },
  "scope": { "project": "kb", "source": "explicit", "state": "has_active_claims" } }
```

#### Relevance floor (private-repo issue #202)

`recall()` always has a nearest neighbour, so without a floor a nonsense query against a populated project got a confident, completely unrelated claim back — `/why`'s own contract ("returns null when nothing matches") was never actually true on a project with any reasoned claims in it. `WHY_RELEVANCE_FLOOR` (`ergo/contradiction/store.py`, currently `0.5`) is the cutoff: when the best reasoned hit scores below it, `why` is `null` **and** the response carries the additive `suppressed` object above instead of a low-confidence answer, so a suppressed result is never silently identical to "this project holds no reasoned claims" — only one of those two `why: null` shapes carries `suppressed`.

`score` is always present on a real match (same `0.7 × vec_sim + 0.3 × kw` blend as `/recall`), whether or not the floor applies, so a caller who wants a stricter or looser cutoff than the built-in floor can apply their own on top. There is no env var or config knob for the floor by design — see the calibration note at `WHY_RELEVANCE_FLOOR`'s definition for the measured score distribution it's based on.

**A suppressed answer means "ask a more specific question," not "there is no decision on file."** The underlying claim still exists (`scope.state` says `"has_active_claims"`, same as a real match) — the query just didn't land close enough to it. Call `/recall` directly (it returns `score`/`vec_sim`/`kw` per hit with no floor applied) if you want to see what was actually nearby and judge it yourself.

### `GET /active` — list active claims

Bounded page by default (private-repo issue #185): a populated project returning **every**
active claim in full (statement, reason, the persisted norm JSON, all
timestamp/status columns) was measured at ~300 KB for ~170 claims on a real
tenant — enough to blow a caller's tool-output ceiling. Truncation is loud,
never silent: the response always carries the scope's true total alongside
whatever page was returned.

**Query params:**

| name | required | default | description |
|---|---|---|---|
| `org_id` | yes | — | tenant |
| `project` | no | — | project — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `limit` | no | 50 | page size; clamped to `[1, 500]` server-side regardless of what's requested; a non-numeric/missing value degrades to the default rather than erroring |
| `offset` | no | 0 | rows to skip, oldest-`claim_seq`-first; page with `offset=next_offset` (below) to walk the whole set with no gaps or duplicates |
| `projection` | no | `full` | `full` (every column, unchanged since private-repo issue #185) or `summary` (private-repo issue #206 — each claim reduced to `{id, statement}`, `reason` omitted entirely); an unrecognized value degrades to `full` rather than erroring |

**Response 200:**

```json
{ "state": "project_missing" | "empty" | "has_active_claims",
  "count": 170,          // TOTAL active claims in scope, regardless of limit
  "returned": 50,        // claims in THIS page
  "truncated": true,
  "truncated_reason": "row_limit",  // or "byte_budget"; null when not truncated
  "next_offset": 50,     // null when not truncated
  "claims": [ ...50 full claims... ] }
```

`state` describes the **scope**, not the page: a project with 170 claims read
at `offset=500` still reports `has_active_claims` with an empty `claims`
list, never `empty` — the three-state contract (`project_missing` | `empty` |
`has_active_claims`) is otherwise unchanged from before pagination existed.

Gains a `"warnings":[...]` key alongside the fields above only when `project`
was omitted and defaulted.

Out of scope for this change: `GET /history` (same unbounded shape, tracked
separately) and `GET /export` (at the time, intentionally a bulk
full-fidelity path — private-repo issue #203 later gave it its own opt-in pagination,
unbounded by default; see [`GET /export`](#get-export--export-active-claims)).

#### Summary mode + byte budget (private-repo issue #206)

Private-repo issue #185's bounded page still landed at 201.6 KB on a real verbose corpus (avg
1,944 B/claim; MCP JSON-stringifies the tool result inside its own envelope,
roughly doubling every escaped quote in every `reason` on the wire) — a fixed
row count can't be right for both a terse corpus and a verbose one. Two
independent knobs, both opt-in so an existing caller sees **no change**:

- **`projection=summary`** — the reporter's top-ranked fix. `"what does this
  project believe?"` is a statement question; `reason` is the bulk of a
  claim's bytes and, per the reporter, the field a browse rarely needs — call
  `GET /recall` or `GET /why` for the one or two claims you actually want the
  reason for, by id. Deliberately a plain 2-value enum (`full`/`summary`),
  not an arbitrary field mask: **`projection` was chosen over `fields`**
  (which conventionally means an arbitrary comma-separated column mask, e.g.
  Google's `fields=` field-mask convention — the wrong precedent for a fixed
  named shape), **`view`** (too generic/overloaded a term to read
  unambiguously on `/active`, `/recall`, and `/export` alike), and **`mode`**
  (already vague enough to collide with an unrelated future request-behavior
  toggle). `projection=full|basic`-style enums for exactly this "named subset
  of columns" shape are an established REST precedent (e.g. Google's own
  Directory/Admin APIs), which is the model this follows. Not implemented on
  `/recall`/`/export` in this PR, but the name is chosen to generalize to
  both.
- **`ACTIVE_BYTE_BUDGET` (~50 KB of served JSON, not exposed as a param)** —
  the reporter's second-ranked fix, and the only one of the two that
  self-tunes per corpus rather than assuming an average bytes/claim. Applies
  ONLY inside the paginated (`limit` given) branch a page already fetched:
  it trims that page front-to-back once its served JSON would exceed the
  budget, always keeping at least the first claim so a page can never
  collapse to nothing. Composes with `projection=summary` — a summary row is
  a fraction of a full row's size, so the budget rarely binds once a caller
  opts into it. Never applies to the internal unbounded opt-out (`limit`
  omitted entirely, used by the contradiction judge and the `/health`/
  `/ready` probes) — that path never paged in the first place. No new query
  param or config knob: the budget is a hard-coded engine constant, same
  spirit as `MAX_ACTIVE_LIMIT`.
- **`truncated_reason`** makes the two causes distinguishable: `"row_limit"`
  means the page filled to `limit` and more rows remain (raising `limit`
  helps, up to `MAX_ACTIVE_LIMIT`); `"byte_budget"` means the byte budget cut
  the page short of `limit` (raising `limit` will **not** help — the wire
  size, not the row count, is the constraint). `null` when `truncated` is
  `false`.

**Default vs. opt-in:** unlike private-repo issue #185's own breaking change to `/active`'s
default (a naive caller needed protecting from an unbounded response by
default, even without changing anything on their end), summary mode does
**not** change the default. Private-repo issue #185 only shrank the row *count* per page — every
row it did return kept every column. Summary mode removes a column
(`reason`) from every row it returns; for a corpus where `reason` **is** the
point of the store (the reporter's own words), defaulting to a mode that
silently drops it would be a real regression for the majority of callers who
never asked for it, not the input-shape-agnostic size cap private-repo issue #185 was. This
follows the same precedent [`GET /export`](#get-export--export-active-claims)
(private-repo issue #208) deliberately chose over `/active`'s own — export is a backup/
migration source of truth an existing caller must never get a silently
different shape from — while `/active` here is a browse endpoint where
"silently different shape" is exactly the wrong failure mode, just for a
narrower reason (content loss, not truncation risk) than export's. The byte
budget, by contrast, is **not** opt-in — an existing caller already accepted
"my page may be truncated" the moment private-repo issue #185 shipped `limit`/`offset`;
`ACTIVE_BYTE_BUDGET` only ever narrows an already-bounded page and reports
why through the same loud fields, so it extends private-repo issue #185's existing contract
rather than changing it.

### `GET /history` — full history (active + superseded + retracted)

Query params: `org_id` (required), `project` (optional, same as `/active`).

```json
{ "history": [ { ...claim... }, ... ] }
```

Ordered by `claim_seq` ascending. Gains a top-level `"warnings":[...]` key alongside
`history` only when `project` was omitted and defaulted.

### `GET /audit` — tenant-facing audit trail (private-repo issue #124)

Read-only. Self-serve read of **this caller's own org's** `writes` + `conflict_events` audit rows.


**Tenant isolation:** exactly the `/history`/`/active` pattern — the token's own org (`forced_org`) always wins over any client-supplied `org_id`. A tenant can never read another org's audit rows, even by passing a different `?org_id=`.

**Query params:**

| name | required | default | description |
|---|---|---|---|
| `org_id` | yes (tenant token: implied by the token) | — | tenant |
| `project` | no | — | project — see [Project scope resolution](#project-scope-resolution-default-project-private-repo-issue-179) if omitted |
| `limit` | no | 200 | caps EACH stream independently (not their sum); clamped to `[1, 1000]` server-side regardless of what's requested |

Each stream returns its most recent `limit` rows, ordered oldest → newest by `id` (monotonic even when two rows share a `ts`) — so a busy tenant never gets an unbounded payload.

**Response 200:**

```json
{
  "writes": [
    { "id": 1, "org_id": "acme", "project": "platform", "who": "alice",
      "action": "remember", "claim_id": "c7eb...", "preflight_seq": 1,
      "commit_seq": 1, "ts": "2026-07-20T12:00:00Z" }
  ],
  "conflict_events": [
    { "id": 1, "org_id": "acme", "project": "platform", "who": "bob",
      "incoming": "Deploy on Fridays.", "against": "c7eb...",
      "stored_claim_id": null, "tier": "block", "score": 0.94,
      "source": "structural", "reason": "same subject, opposing modality",
      "resolution": "flagged", "exception_reason": null,
      "preflight_seq": 2, "commit_seq": null, "judge_version": "...",
      "normalizer_version": "...", "ts": "2026-07-20T12:05:00Z" }
  ]
}
```

See [docs/STORAGE.md](STORAGE.md) for the full column reference on both tables. Gains a top-level `"warnings":[...]` key alongside `writes`/`conflict_events` only when `project` was omitted and defaulted.

| code | body | meaning |
|---|---|---|
| `200` | (above) | audit rows for the caller's own org+project |
| `400` | `{"error":"org_id required"}` | missing `org_id` |
| `401` | `{"error":"unauthorized"}` | missing/invalid bearer |

| `500` | `{"error":"..."}` | store failure |

### `GET /diagnose` — ops view (anti-zombie)

The running binary answers for itself — never point at a README.

```json
{
  "service": "ergo contradiction engine",
  "db_path": "/data/ergo.db",
  "normalizer": "OpenAINormalizer(claude-haiku)",
  "nli": "Xenova/nli-deberta-v3-base:quantized",
  "auth": "token",
  "booted_at": "2026-06-27T14:23:33+0000",
  "uptime_seconds": 320.4,
  "owner_name": "Alex",
  "owner_contact": "you@example.com",
  "backup_path": null,
  "last_backup_at": null,
  "last_write_at": "2026-06-27T16:01:12+0000",
  "kill_review_date": null,
  "kill_review_status": "unset",
  "warnings": [
    "no ERGO_BACKUP_PATH configured — first `docker rm` is unsurvivable",
    "no ERGO_KILL_REVIEW_DATE — keep/kill review not scheduled"
  ],
  "counts": { "claims_total": 114, "claims_active": 114, "projects": 1, ... },
  "conflicts": { "events_total": 0, "flagged_blocks": 0, "warned": 0, ... },
  "versions": {
    "engine_normalizer": "OpenAINormalizer:1",
    "engine_judge": "structural-1+nli-deberta-v3",
    "engine_norm_schema": "1",
    "stored_normalizers": [...],
    "stored_judges": [...],
    "drift": false
  }
}
```

`warnings[]` is the anti-zombie field: things the operator must act on (missing owner, backup not configured, version drift between stored claims and the running engine, etc.).

### `GET /metrics` — Prometheus text exposition (ergo private-repo issue #121)

**Auth: operator-only, unlike every other unauthenticated monitor route.** `/health`
and `/ready` are deliberately always-open so any monitor can hit them with zero
config; `/metrics` is the opposite choice, because it exposes per-route latency and
guarded-write outcome counts an operator may not want visible to an arbitrary tenant
caller. The gate (`Handler._metrics_authed`):

1. If `ERGO_SUPERADMIN_TOKEN` is configured, only that exact bearer passes — the legacy `ERGO_API_TOKEN` gets `401`).
2. Else, if `ERGO_API_TOKEN` is configured, that gate applies instead (`_authed()`) —
   a single-tenant deployment that never provisioned a superadmin token still gets
   *some* operator gate rather than none.


No bearer / wrong bearer → `401 {"error":"unauthorized"}`.

Returns `200` with `Content-Type: text/plain; version=0.0.4; charset=utf-8` — the
Prometheus text exposition format, hand-rendered (no `prometheus_client` dependency;
see `ergo/contradiction/metrics.py` — stdlib-only, matching house style). Four metric
families, **all in-process counters that reset on restart** (single-process by design,
same reasoning as the bounded thread pool; there is no
cross-process aggregation to do):

- `ergo_http_requests_total{method,route,status}` — a Counter, one increment per
  completed request.
- `ergo_http_request_duration_seconds{method,route}` — a Histogram (`_bucket`, `_sum`,
  `_count`) of wall-clock request latency.
- `ergo_guarded_write_outcomes_total{route,outcome}` — a Counter over exactly the three
  guarded-write routes (`/remember`, `/learn`, `/supersede` — **not** `/ingest`, which
  skips the contradiction gate entirely). `outcome` is one of `stored` (200) /
  `conflict` (409) / `quota` (403) / `5xx` (>=500, including the degraded-provider 502/503
  mapping).
- `ergo_pipeline_stage_duration_seconds{stage}` — a Histogram of `normalize` / `judge` /
  `nli` stage latency (the LLM normalizer round-trip, the structural+NLI judge's total
  time per write, and the raw ONNX NLI inference call specifically — the middle one
  typically dominated by the last when NLI actually runs).
- `ergo_http_workers{name}` — two Gauges, `name="active"` (currently live
  connection-handling threads) and `name="max"` (`ERGO_HTTP_MAX_WORKERS`), for
  thread-pool saturation.

```
# HELP ergo_http_requests_total Total HTTP requests, by method/route/status.
# TYPE ergo_http_requests_total counter
ergo_http_requests_total{method="GET",route="/health",status="200"} 42
...
# HELP ergo_http_workers Thread-pool worker gauges (name="active"|"max").
# TYPE ergo_http_workers gauge
ergo_http_workers{name="active"} 1
ergo_http_workers{name="max"} 8
```


## Examples

End-to-end (assuming `ergo:merge` running on `:8788`, token in `$TOK`):

```bash
TOK="Authorization: Bearer $ERGO_API_TOKEN"

# 1. Ingest a doc (no contradiction gate)
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/ingest -d @TOOLS.json
#  → {"result":"ingested","chunks":49,"stored":49,"source":"TOOLS.md"}

# 2. Record a decision (gated)
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/remember -d '{
  "org_id":"acme","project":"platform","who":"alice",
  "statement":"Deploy on Tuesdays","reason":"team is on-call Mon/Wed"
}'
#  → {"result":"stored","claim_id":"..."}

# 3. Try a contradicting one
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/remember -d '{
  "org_id":"acme","project":"platform","who":"bob",
  "statement":"Never deploy on Tuesdays","reason":"incident risk"
}'
#  → 409 {"result":"conflict","conflicts":[{...alice... "tier":"block"}]}

# 3b. Learn a belief FROM a source (gated, like /remember, with provenance)
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/learn -d '{
  "org_id":"acme","project":"platform","who":"claude-code",
  "statement":"Sessions are never reused across gateway restarts",
  "source":"sessions.py:847","reason":"observed in the restart path"
}'
#  → {"result":"learned","claim_id":"...","source":"sessions.py:847"}

# 4. Change your mind via supersede
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/supersede -d '{
  "org_id":"acme","project":"platform","who":"alice","existing_id":"<id>",
  "statement":"Never deploy on Tuesdays","reason":"incident risk outweighs on-call"
}'
#  → {"result":"superseded","claim_id":"<new id>"}

# 4b. Remove a wrong-at-birth claim (typo / test junk) — audit-loud, no gate
curl -s -H "$TOK" -X POST http://127.0.0.1:8788/retract -d '{
  "org_id":"acme","project":"platform","who":"alice","claim_id":"<id>",
  "reason":"test junk written against the prod scope"
}'
#  → {"result":"retracted","claim_id":"<id>","retracted":true}

# 5. Read it back
curl -s -H "$TOK" "http://127.0.0.1:8788/why?org_id=acme&project=platform&q=when%20to%20deploy"
# → {"why": {"claim":"Never deploy on Tuesdays","reason":"...","history":[...2 entries...]}}
```

## MCP endpoint

`api.ergomem.com/mcp` serves the SAME 14 tools as this API (one tool per route
above) over the MCP streamable-HTTP transport — a second transport on the
same engine, not a separate service. Source:
[`ergo/mcp/server.py`](../ergo/mcp/server.py); the same module also runs
stdio (`python -m ergo.mcp.server`, the default) for local launchers like
Claude Code. Hosted run-line, containerized behind the reverse proxy /
Cloudflare tunnel that terminates `api.ergomem.com`
(see the `Dockerfile`'s pip-install comment for how this ships in-image):

```bash
python -m ergo.mcp.server --transport streamable-http --host 0.0.0.0 --port 8790 \
  --allowed-host api.ergomem.com --allowed-host mcp.example.com
```


- **Transport:** MCP streamable-HTTP, stateless (no session affinity — every
  request is independent, so there's nothing to pin to a particular backend
  instance).
- **`--host 0.0.0.0`:** the CLI's own default is `127.0.0.1` (secure-by-default
  for a bare-metal/local run — see below). Inside a container that default
  only binds loopback WITHIN that container, which a published Docker port or
  another container's reverse proxy can never reach; a containerized,
  proxy-fronted deployment needs `--host 0.0.0.0` to actually accept those
  connections. A bare-metal run with a client on the same box should keep the
  `127.0.0.1` default instead.
- **`--allowed-host`:** a SEPARATE concern from `--host` above — the SDK
  auto-enables DNS-rebinding protection that restricts the incoming `Host`
  header to loopback whenever this process binds `127.0.0.1` (the default) —
  correct for a direct local client, but a reverse proxy/tunnel forwards the
  ORIGINAL public `Host` (e.g. `api.ergomem.com`) unchanged, which that
  loopback-only allowlist then rejects with `421 Misdirected Request`. Pass
  every public hostname this process is reachable at as a repeatable
  `--allowed-host`; loopback stays admitted too either way. Omit the flag
  entirely for a purely local run — the SDK's own loopback-only default is
  untouched.
- **Auth:** the same bearer as the rest of this API — each tool call uses
  YOUR OWN `Authorization: Bearer <token>` header, forwarded straight through
  to the underlying route. There is no separate MCP-specific token, and a
  missing/malformed header surfaces this API's own `401` back through the
  tool call rather than silently falling back to any server-side default.
- **Scope defaults:** same as the routes above, except `project` — with no
  single local checkout to take a git-repo basename from on a hosted
  multi-tenant server, an omitted `project` is left for the ENGINE to resolve
  (DEFAULT-PROJECT, private-repo issue #179: the engine's own
  `"default"` fallback with a warning — see [Project scope
  resolution](#project-scope-resolution-default-project-private-repo-issue-179)) rather
  than this adapter substituting anything itself. Still overridable per call,
  same as every other scope field.

Client examples (drop into the relevant MCP config, using your own `ERGO_API_TOKEN`):

```jsonc
// Claude Code / Codex — mcpServers entry
{
  "mcpServers": {
    "ergo": {
      "type": "http",
      "url": "https://api.ergomem.com/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

```jsonc
// Cursor — mcp.json entry
{
  "mcpServers": {
    "ergo": {
      "url": "https://api.ergomem.com/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

## What's NOT here (yet)

- `POST /backup` — programmatic backup trigger. Use the SQLite online-backup API or `VACUUM INTO` against the live container's `/data/ergo.db` instead.
- Streaming / SSE for long ingests — `/ingest` is synchronous; large bodies should be chunked client-side.
- Repo-onboarding adapter (a dedicated ingest path shaped for a git checkout) and a panel UI for export/import — MCP already surfaces `ergo_export`/`ergo_import` ([MCP.md](MCP.md)).

## Related

- Architecture & data flow → [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Env vars / boot config → [`CONFIGURATION.md`](CONFIGURATION.md)
- DB schema & write-guard → [`STORAGE.md`](STORAGE.md)
- Contradiction pipeline internals → [`CONTRADICTION-PIPELINE.md`](CONTRADICTION-PIPELINE.md)
- Embeddings + recall scoring → [`RETRIEVAL.md`](RETRIEVAL.md)
