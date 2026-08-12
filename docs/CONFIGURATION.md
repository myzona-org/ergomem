# Configuration Reference

All runtime config is via environment variables, read by the container at boot — change them by stopping + restarting with new env (the SQLite file is unaffected). All `ERGO_*` vars below are grep-able in [`ergo/contradiction/server.py`](../ergo/contradiction/server.py) (and a few in [`ergo/embedders.py`](../ergo/embedders.py) and [`ergo/contradiction/normalize.py`](../ergo/contradiction/normalize.py)).

## Auth & networking

| var | default | required? | notes |
|---|---|---|---|
| `ERGO_API_TOKEN` | *(unset)* | **required on non-loopback binds** | legacy bearer-token for every route except `/health` and `/ready`. With no token, a non-loopback bind refuses to start unless `ERGO_INSECURE_NO_AUTH=1`; a loopback bind remains open for dev/test and logs a warning.A bearer that is neither the legacy token nor unset is rejected -- this build has no control plane to resolve it against. |
| `ERGO_INSECURE_NO_AUTH` | *(unset)* | no | deliberate escape hatch for tokenless non-loopback binds. Set exactly `1` only when intentionally running an open API; startup emits a louder warning that the API is open to the network. Prefer setting `ERGO_API_TOKEN` or binding `ERGO_HTTP_HOST=127.0.0.1`. |

| `ERGO_HTTP_HOST` | `0.0.0.0` | no | bind address. Without `ERGO_API_TOKEN`, startup refuses non-loopback binds unless `ERGO_INSECURE_NO_AUTH=1`; bind `127.0.0.1`, `::1`, or `localhost` for tokenless dev/test. Inside the image keep `0.0.0.0` with a token set. |
| `ERGO_HTTP_PORT` | `8000` | no | port inside the container. Typical host publish, loopback-only: `-p 127.0.0.1:8788:8000`. |
| `ERGO_HTTP_MAX_WORKERS` | `8` | no | bounded threaded HTTP server: the server runs `ThreadingHTTPServer` (each admitted request its own thread), but every ACCEPTED CONNECTION — from any tenant — must first acquire a permit from ONE shared, per-instance `threading.BoundedSemaphore(ERGO_HTTP_MAX_WORKERS)` before a worker thread is even spawned; a connection burst queues at the OS accept() level instead of spawning unbounded threads (protects the LiteLLM proxy / normalizer backend from a stampede). This bound is process-wide, not per-tenant: it prevents unbounded thread creation, but if every permit is held by slow requests (e.g. several tenants' guarded writes at once), a new connection — including another tenant's `/health` — waits for one to free up. What IS per-tenant is the store lock below the HTTP layer, so a slow write for tenant A never blocks tenant B's already-admitted DB access. A naive *unbounded* threaded server is deliberately not used. |
| `ERGO_HTTP_TIMEOUT` | `30.0` (seconds) | no | per-connection socket timeout — a stalled client (promises more body than it ever sends, or half-open) is dropped instead of blocking its handler thread forever. |
| `ERGO_LOG_FORMAT` | `plain` | no | access-log format on stderr (ergo private-repo issue #121). `plain` (default) keeps today's `[ergo] <client> - <message>` line exactly as-is — nothing changes unless this is set. `json` emits one parseable JSON object per line instead (`{"ts","client","message"}`), for log shippers that want structured input. Any other/unset value falls back to `plain`. |
| `ERGO_MAX_BODY_BYTES` | `1048576` (1 MiB) | no | request-body cap for every POST route except `/ingest`; over cap → `413`. `Content-Length` is validated (rejects negative/non-integer) and enforced before any bytes are read off the socket. |
| `ERGO_MAX_INGEST_BYTES` | `8388608` (8 MiB) | no | request-body cap for `POST /ingest` only — larger than the default cap since legitimate docs run bigger. |
| `ERGO_MAX_FIELD_CHARS` | `32768` | no | per-field cap on any single guarded string field (`statement` / `reason` / `source` / `q`); over cap → `413` naming the offending field. |
| `ERGO_MAX_INGEST_CHUNKS` | `5000` | no | caps the chunk count `POST /ingest` may produce, checked before any chunk is stored; over cap → `413`. |
| `ERGO_READY_PROBE_TTL_SECONDS` | `15` | no | TTL for `GET/HEAD /ready`'s cached normalizer probe. `/ready` is unauthenticated and actively calls the normalizer, so this caps it to at most one real `normalize()` call per window regardless of hit rate — a monitor every 30s or a flood both dedupe to one probe per TTL. `0` disables the cache (probes every hit); a negative/unparsable value falls back to the default. |
| `ERGO_READY_FIRST_PROBE_WAIT_SECONDS` | `3` | no | On the very first `/ready` probe since boot (no cached result yet), a concurrent request waits behind whichever request is already making the real `normalize()` call, up to this budget — well under the normalizer's own 60–120s request timeout. If nothing has landed within it, that request gives up and reports a controlled `"not ready yet"` result (`normalizer.ok: false`, `retryable: true`) instead of pinning a worker thread from the bounded pool (`ERGO_HTTP_MAX_WORKERS`) for however long a hung provider takes. Only ever consulted during that narrow startup window — once any probe has ever completed, later requests read the cache instead. A non-positive or unparsable value falls back to the default. |


## Storage

| var | default | notes |
|---|---|---|
| `ERGO_DB_PATH` | `/data/ergo.db` | SQLite file path — the legacy/default store used by the legacy principal. Mount a Docker volume at `/data`. |


The DB file holds the `claims`, `vec_claims`, `writes`, `conflict_events`, `projects` tables. WAL mode is enabled — that means there will also be a `-wal` and `-shm` file alongside. **Do not back up with `cp`** — see [STORAGE.md → Backup](STORAGE.md#backup).

## Embedder (the retrieval layer)

| var | default | values | notes |
|---|---|---|---|
| `ERGO_RETRIEVAL` | `on` | `on` / `off` / `0` / `false` / `no` | turn the retrieval layer off entirely (firewall-only). `recall` / `why` / `ingest` return 503 when off. |
| `ERGO_EMBED_PROVIDER` | `fastembed` | `fastembed` \| `ollama` \| `openai` \| `cohere` \| `voyage` | which embedder to use. `fastembed` is baked into the image; others need network/keys. |
| `ERGO_EMBED_MODEL` | *(provider default)* | string | override the model id. Defaults: `BAAI/bge-small-en-v1.5` (fastembed), `nomic-embed-text` (ollama), `text-embedding-3-small` (openai), `embed-english-v3.0` (cohere), `voyage-3-lite` (voyage). |
| `ERGO_EMBED_DIM` | *(provider default)* | int | only set if you use a non-default model with a different dimension. The DB **locks the dim at first write** — switching dim later requires a re-embed migration: [`ergo/contradiction/reembed.py`](../ergo/contradiction/reembed.py). |
| `ERGO_FASTEMBED_CACHE` | *(unset)* | path | override the fastembed model cache. In the image this is set to `/models/fastembed` and the model is baked. |

**Per-provider keys (only if you pick that provider):**

| var | for provider | notes |
|---|---|---|
| `OLLAMA_HOST` | `ollama` | default `http://localhost:11434`. Inside Docker on macOS, use `http://host.docker.internal:11434`. |
| `OPENAI_API_KEY` | `openai` | hosted endpoint. |
| `COHERE_API_KEY` | `cohere` | hosted endpoint. |
| `VOYAGE_API_KEY` | `voyage` | hosted endpoint. |

## Instruct LLM (normalizer for `/remember`, `/learn`, and `/supersede`)

The instruct LLM is **only** called by the contradiction-checked paths (`/remember`, `/learn`, `/supersede`) — `/ingest` skips it entirely. If you don't set a provider, the engine falls back to `RuleNormalizer` (deterministic, fast, but blind to pronouns / paraphrasing). Since 2026-06-29 an unparseable LLM reply (refusal / non-JSON) **fails open**: it degrades the claim to an unknown norm instead of 500-ing the write. But a provider-level failure now fails **closed** instead (private-repo PR #58 and private-repo PR #62): a connection error/timeout/429/5xx/auth rejection refuses the write with a retryable `503`, and a permanent provider 4xx (bad model name, malformed request) refuses it with a non-retryable `502`. On `/remember` and `/learn` both are bypassable with an explicit `force_exception`; `/supersede` has no such override, so its `503`/`502` refusal is unconditional. See [HTTP-API.md](HTTP-API.md#post-remember--guarded-write) for the exact status-code mapping.

| var | default | values | notes |
|---|---|---|---|
| `ERGO_LLM_PROVIDER` | *(unset → `RuleNormalizer`)* | `anthropic` \| `openai` \| `litellm` \| `ollama` | which backend. `openai` and `litellm` are equivalent — both use the OpenAI-shape `/v1/chat/completions` API. |
| `ERGO_LLM_MODEL` | provider-specific | string | per provider defaults: `claude-haiku-4-5` (anthropic), `gpt-4o-mini` (openai/litellm), `qwen3:14b` (ollama). |
| `ERGO_LLM_BASE` | *(provider native)* | URL | base URL override. For LiteLLM proxy: e.g. `http://litellm.internal:4000/v1`. |
| `ERGO_LLM_KEY` | *(unset)* | string | API key for openai/litellm (alternative to `OPENAI_API_KEY`). |
| `ERGO_LLM_CACHE` | `<repo>/evals/contradiction/.cache/llm_norm.json` | path | LLM normalize-result cache (single JSON file, atomic writes). |

**Provider-native vars also honored:**

| var | used by | notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `anthropic` | required. Also used as the fallback signal in the factory: if set and `ERGO_LLM_PROVIDER` is unset, default is `anthropic` (else `ollama`). |
| `OPENAI_API_KEY` | `openai` / `litellm` | required. |
| `OPENAI_BASE_URL` | `openai` / `litellm` | same as `ERGO_LLM_BASE` for OpenAI-shape backends. |

### Recipe: production-grade (the live container today)

```bash
ERGO_API_TOKEN=...secret...
ERGO_OWNER=Alex
ERGO_OWNER_CONTACT=you@example.com
ERGO_NLI=on
ERGO_LLM_PROVIDER=litellm
ERGO_LLM_MODEL=claude-haiku
OPENAI_BASE_URL=http://litellm.internal:4000/v1   # LiteLLM proxy
OPENAI_API_KEY=sk-...                              # LiteLLM master key
```

### Recipe: air-gapped (no Anthropic)

```bash
ERGO_API_TOKEN=...secret...
ERGO_LLM_PROVIDER=ollama
ERGO_LLM_BASE=http://host.docker.internal:11434    # reach the host's Ollama
ERGO_LLM_MODEL=qwen2.5:7b
```

### Recipe: retrieval-only (no contradiction LLM)

```bash
ERGO_API_TOKEN=...secret...
# ERGO_LLM_PROVIDER unset → RuleNormalizer is used for /remember and /supersede
# (fast and deterministic, but pronouns / paraphrasing won't be caught)
```

## NLI judge

The NLI cross-encoder runs as the third stage of the contradiction pipeline. The model is baked into the image and loads offline by default.

| var | default | values | notes |
|---|---|---|---|
| `ERGO_NLI` | `on` | `on` / `off` (and synonyms `0` / `false` / `no`) | turn the NLI stage off → structural-only pipeline. |
| `ERGO_NLI_MODEL` | `Xenova/nli-deberta-v3-base` | HF repo id | override. Must be ONNX-exportable. |
| `ERGO_NLI_VARIANT` | `quantized` | `quantized` / `int8` / a specific filename | which `onnx/model_*.onnx` file to load. `quantized` ≈ 190 MB; `int8` ≈ 80 MB (shaves ~110 MB if you rebuild with `--build-arg ERGO_NLI_VARIANT=int8`). |
| `ERGO_NLI_CACHE` | *(unset)* | path | model cache. In the image this is `/models/nli`. |
| `ERGO_NLI_LOCAL_ONLY` | *(unset)* | `1` / `true` / `yes` | force offline load (no HF API calls). Set automatically by the Dockerfile. |

## HuggingFace / Transformers (offline gates)

The Dockerfile sets these at build time and bakes them into the image so the runtime never accidentally phones home:

| var | image value | notes |
|---|---|---|
| `HF_HOME` | `/models/hf` | cache root |
| `HF_HUB_OFFLINE` | `1` | block HF Hub API calls |
| `TRANSFORMERS_OFFLINE` | `1` | block transformers' silent fetches |

Don't change these unless you're rebuilding the image with a different model.

## Owner / anti-zombie

`/diagnose` surfaces these so the running binary answers for itself instead of pointing at a README. The engine **boots successfully** without them but `/diagnose` logs warnings.

| var | default | notes |
|---|---|---|
| `ERGO_OWNER` | `(unset)` | named owner (e.g. `Alex`). `/diagnose` warns if unset. |
| `ERGO_OWNER_CONTACT` | `(unset)` | how to reach them (email/handle). |
| `ERGO_BACKUP_PATH` | *(unset)* | path the backup script writes to. `/diagnose` reports `last_backup_at` from this file's mtime, and warns if unset or if the file doesn't exist. |
| `ERGO_KILL_REVIEW_DATE` | *(unset)* | ISO date when the owner must review whether to keep / kill the deployment. `/diagnose` warns if unset or if the date is past. |

## Full env-file example

```ini
# ~/.config/ergo/ergo.env — a complete env-file for the recipe above
ERGO_API_TOKEN=secret-test-token
ERGO_OWNER=Alex
ERGO_OWNER_CONTACT=you@example.com
ERGO_NLI=on
ERGO_LLM_PROVIDER=litellm
ERGO_LLM_MODEL=claude-haiku
OPENAI_BASE_URL=http://litellm.internal:4000/v1
OPENAI_API_KEY=sk-...
```

Pass with `docker run --env-file ~/.config/ergo/ergo.env ...`.
