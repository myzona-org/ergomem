# Development

Local setup, tests, image build, the no-network boot gate.

## Layout

```
ergo/
├── __init__.py                       # version
├── embedders.py                      # pluggable embedders (fastembed default)
├── mcp/
│   └── server.py                     # stdio MCP adapter over the HTTP API
└── contradiction/
    ├── server.py                     # HTTP API
    ├── store.py                      # ContradictionStore + write-guard + recall/why
    ├── ingest.py                     # word-safe chunker + ingest_text / ingest_file
    ├── normalize.py                  # Rule + LLM normalizers (factory by env)
    ├── structural.py                 # deterministic verdict comparator
    ├── judge.py                      # combine structural + NLI → tier
    ├── nli.py                        # ONNX NLI wrapper (no torch)

    └── extract.py                    # markdown → claim candidates (eval-side)

tests/contradiction/                  # SQLite-only, write-guard/gates/normalizers/NLI/server
tests/mcp_adapter/                    # MCP stdio adapter
tests/test_embedders.py               # embedder providers
evals/                                # eval suites — see EVALS.md
docker/                               # boot-test.py (no-network boot gate)
docs/                                 # you are here
Dockerfile
pyproject.toml
```

## Local setup

Python 3.10+ required. Use a venv.

```bash
git clone https://github.com/myzona-org/ergomem.git
cd ergomem
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The `dev` extra pulls in `engine` (everything the deployable image installs: fastembed, onnxruntime, tokenizers, numpy, huggingface_hub, sqlite-vec) plus `mcp` (the stdio adapter) and pytest — i.e. the full test-suite surface. Installing only `.[engine]` leaves the MCP adapter tests without their SDK; installing neither fails 7 retrieval tests on a missing `sqlite_vec` import.

### Cache the embedder model once

The image bakes `BAAI/bge-small-en-v1.5` (~130 MB) at build time. For local dev you need it in `~/.cache/fastembed_cache` so the engine doesn't try to fetch it at runtime:

```bash
python -c "
from fastembed import TextEmbedding
import os
TextEmbedding(model_name='BAAI/bge-small-en-v1.5',
              cache_dir=os.path.expanduser('~/.cache/fastembed_cache'))
"
```

One-time, ~30 seconds.

### Run the server locally

```bash
export ERGO_DB_PATH=/tmp/ergo-dev.db
export ERGO_HTTP_PORT=8790
export ERGO_API_TOKEN=dev
export ERGO_NLI=off                   # speed up dev loops; turn on for full pipeline
# No ERGO_LLM_PROVIDER → RuleNormalizer (fast, deterministic, no API calls)

python -m ergo.contradiction.server
# [ergo] WARNING: no ERGO_API_TOKEN set... (suppressed because we DID set it)
# [ergo] listening on http://0.0.0.0:8790
```

Smoke test:

```bash
TOK="Authorization: Bearer dev"
curl -s http://127.0.0.1:8790/health | jq .
# → {"status": "ok", "retrieval": true, "probe_state": "..."} -- no bearer, slim
# body only (private-repo issue #187); add -H "$TOK" for the full model-stack/db_path detail
curl -s -H "$TOK" -X POST http://127.0.0.1:8790/remember -d '{
  "org_id":"dev","project":"sandbox","who":"me",
  "statement":"Deploy on Tuesdays","reason":"on-call rotation"
}' | jq .
```

## Tests

```bash
python -m pytest tests -q
```

All tests are in-process SQLite (`":memory:"`) — **no Postgres, no network, no model downloads**. The retrieval tests use a deterministic fake embedder (hashed bag-of-words → unit vector) so CI doesn't need the bge-small model.

### Test groups

| file | locks in |
|---|---|
| `test_store.py` | write-guard, supersede, `learn` provenance, race-safety, audit rows |
| `test_server.py` | HTTP contract: auth, status codes per endpoint, validation, diagnose warnings |
| `test_judge_gates.py` | polarity + aspect gates on the structural and NLI branches |
| `test_value_conflict.py` | value-conflict detection (blue/red canary, ports), groundedness |
| `test_normalize_rules.py` | RuleNormalizer: modality/subject/scope/coref extraction + coerce |
| `test_normalize_factory.py` | LLM factory picks the right provider from env; fail-open on refusal |
| `test_nli.py` | NLIJudge init plumbing, softmax, both-directions max + real-model characterization |
| `test_extract.py` / `test_ingest.py` | markdown→candidates; word-safe chunker + fact fast-path |
| `test_retrieval.py` | recall ranking, why+history, superseded exclusion, retrieval-disabled guard |
| `tests/mcp_adapter/test_server.py` | MCP tool → HTTP wire format, scope defaults, structured 409/500 |
| `tests/test_embedders.py` | embedder factory, provider request/parse shapes, env overrides |

| `test_http_concurrency.py` / `test_store_concurrency.py` | bounded thread admission, per-tenant lock isolation, `claim_seq` race safety under load |

### Adding a test

The contradiction-test pattern (no DB needed):

```python
from ergo.contradiction.store import ContradictionStore, Stored

def test_my_thing():
    s = ContradictionStore(":memory:")
    r = s.remember("acme", "project", "alice", "Deploy on Tuesdays.", reason="on-call")
    assert isinstance(r, Stored)
    assert s.active("acme", "project").state == "has_active_claims"
```

For retrieval-needing tests, use the `FakeEmbedder` pattern from `test_retrieval.py`:

```python
import hashlib
class FakeEmbedder:
    dim = 64
    def embed(self, text):
        v = [0.0] * self.dim
        for w in text.lower().split():
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        n = sum(x*x for x in v) ** 0.5 or 1.0
        return [x/n for x in v]

s = ContradictionStore(":memory:", embedder=FakeEmbedder())
```

Deterministic, offline, fast.

## Build the image

```bash
docker build -t ergo:dev .
```

This:
1. Installs `.[engine]`.
2. Bakes the embedder (~130 MB) into `/models/fastembed`.
3. Bakes the NLI model (~190 MB) into `/models/nli`.
4. **Runs the no-network boot test** — see below.
5. Total image: ~1.06 GB.

To shrink the NLI by ~110 MB at the cost of some precision:

```bash
docker build --build-arg ERGO_NLI_VARIANT=int8 -t ergo:dev .
```

## The no-network boot gate

`docker/boot-test.py` runs *inside the build* and is the **hard CI rule** that prevents silent-fetch regressions. It:

1. Monkey-patches `socket.getaddrinfo` to raise on any DNS lookup.
2. Sets `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` + `ERGO_NLI_LOCAL_ONLY=1`.
3. Imports + warms the embedder (`FastEmbed().embed("hello")`).
4. Imports + warms the NLI judge (`NLIJudge(local_files_only=True)`).
5. Loads sqlite-vec into a fresh SQLite connection.

If *any* of those try to phone home, `getaddrinfo` raises and the build fails. **Build success = the image loads everything offline.**

```dockerfile
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ERGO_NLI_LOCAL_ONLY=1 \
    python /tmp/boot-test.py
```

Expected output during build:

```
--- network blocked, loading baked models ---
embedder OK    dim=384
NLI judge OK   repo=Xenova/nli-deberta-v3-base variant=quantized
sqlite-vec OK
=== no-network boot test passed ===
```

**Do not relax this gate.** If a future code path needs a remote model load, that path must be **explicitly remote** (like the instruct LLM) — not a silent fetch from inside what was supposed to be an offline image.

## Smoke-test the built image

```bash
docker run -d --name ergo-smoke -p 127.0.0.1:8790:8000 -e ERGO_API_TOKEN=dev ergo:dev
for i in $(seq 1 20); do curl -sf http://127.0.0.1:8790/health && break; sleep 1; done
# .normalizer/.embedder/.nli are behind auth (private-repo issue #187) -- a bearer is required
curl -s -H "Authorization: Bearer dev" http://127.0.0.1:8790/health | jq '{normalizer, embedder, nli, retrieval}'
docker rm -f ergo-smoke
```

Expected:

```json
{
  "normalizer": "RuleNormalizer",            // no ERGO_LLM_PROVIDER set
  "embedder":   "FastEmbed(dim=384)",
  "nli":        "Xenova/nli-deberta-v3-base:quantized",
  "retrieval":  true
}
```

For the container-level contradiction smoke (7/7 prose cases) and the labeled prose eval, see [EVALS.md](EVALS.md).

## Style / conventions

- Stdlib first. The whole HTTP server is `http.server`; the store is `sqlite3`; the embedder is `onnxruntime`. The only third-party deps in the engine are model runtimes (fastembed, onnxruntime, tokenizers, numpy) + sqlite-vec + httpx (for remote LLM/embedder calls). Don't add framework dependencies unless there's no stdlib path.
- Bounded threaded HTTP server (hardened by private-repo PR #59 / review finding 1). The server runs `BoundedThreadingHTTPServer` (a `ThreadingHTTPServer` subclass) — each request its own thread — but `process_request()` acquires a per-instance `threading.BoundedSemaphore(ERGO_HTTP_MAX_WORKERS)` (built once in `__init__`, never a rebuildable module-level global) BEFORE a worker thread is even spawned, releasing it only once the whole request is done (`process_request_thread`'s `finally`), so a connection burst queues at the OS accept() level instead of spawning unbounded Python threads. `do_GET`/`do_POST` deliberately do NOT re-acquire anything — double-acquiring the same semaphore would halve concurrency or deadlock at `ERGO_HTTP_MAX_WORKERS=1`. A *naive unbounded* `ThreadingHTTPServer` remains forbidden. Below the HTTP layer, each sqlite connection is opened with `check_same_thread=False` and every public method on `ContradictionStore` holds that instance's own `threading.RLock` for its full duration — there is deliberately NO lock shared across tenant stores (two tenants are two store objects with two independent locks), so a slow guarded write for one tenant never blocks another tenant's requests. The `BEGIN IMMEDIATE` + `SQLITE_BUSY` retry (store.py) is unchanged and remains the per-database serialization of record, now backstopped by the per-instance lock rather than being the sole guarantee.
- Normalize / embed happen **inside** the per-tenant lock (a deliberate change from the old single-threaded model): they're expensive, but since the lock is per-tenant/per-instance, a slow LLM normalizer call for tenant A only ever blocks other callers of tenant A's OWN store — never tenant B's, which has its own independent lock.
- The default embedder (`FastEmbed` in `ergo/embedders.py`) is env-safe under concurrent tenant threads: it decides **once at construction** (by checking whether the fastembed cache dir already holds a model) whether to pass `local_files_only=True` into `TextEmbedding(...)` — a plain, call-local constructor kwarg. It never touches the process-global `os.environ["HF_HUB_OFFLINE"]`, so concurrent `embed()` calls from different tenants' worker threads can't race on it or leak offline-mode into unrelated code sharing the process (an earlier set-then-restore-per-call approach did both, and was replaced before shipping).
- Audit on every write — `writes` + `conflict_events` are non-negotiable. If you add a write path, it gets an audit row (as `/retract` does: `action='retract'`).
- All env vars start with `ERGO_*` (with the historical exceptions of `OPENAI_*`, `OLLAMA_*`, `ANTHROPIC_*`, `HF_*` — those follow upstream conventions).

## Adding a new endpoint

1. Read the body / parse the query in `Handler._route_get` or `_route_post` in `server.py` (the actual routing logic — `do_GET`/`do_POST` themselves just call straight through now; admission is bounded earlier, at connection/thread-creation time, by `BoundedThreadingHTTPServer.process_request`).
2. Call a method on `_STORE` (build a new one on `ContradictionStore` if needed).
3. If retrieval-required, gate on `getattr(_STORE, "retrieval", False)` → 503 when off.
4. Return `_send(200|409|400|500, payload)`.
5. Add a test in `tests/contradiction/`.
6. Document it in [HTTP-API.md](HTTP-API.md).

The existing endpoints (`/remember`, `/learn`, `/ingest`, `/recall`, `/why`, `/supersede`, `/retract`) are all tight examples to copy.

## Adding a new embedder

1. Subclass the `Embedder` protocol in `ergo/embedders.py`:
   ```python
   class MyEmbedder:
       dim = 768
       def __init__(self, model=None, dim=None): ...
       def embed(self, text: str) -> list[float]: ...
   ```
2. Register it in `PROVIDERS = { ..., "myprov": MyEmbedder }`.
3. Set `ERGO_EMBED_PROVIDER=myprov` (+ `ERGO_EMBED_MODEL`, `ERGO_EMBED_DIM` if needed) at boot.
4. Document it in [CONFIGURATION.md](CONFIGURATION.md#embedder-the-retrieval-layer).

The same pattern works for normalizers in `ergo/contradiction/normalize.py` (`AnthropicNormalizer`, `OllamaNormalizer`, `OpenAINormalizer` are existing examples; the factory dispatches on `ERGO_LLM_PROVIDER`).

## Related

- HTTP surface → [HTTP-API.md](HTTP-API.md)
- Eval suites + numbers + how to run → [EVALS.md](EVALS.md)
- SQLite backup/restore → [STORAGE.md → Backup](STORAGE.md#backup)

## CI

Every pull request and every push to `main` runs the full test suite
(GitHub-hosted, no self-hosted runner, no secrets) via the `ci` workflow.
There is no automated merge gate in this repo — a maintainer reviews and
merges PRs by hand. `.semgrep.yaml` ships as a ruleset you can run locally
(`semgrep --config .semgrep.yaml`); no CI job runs it automatically here.

