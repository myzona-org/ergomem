# Ergo memory plugin for OpenClaw

[![OpenClaw](https://img.shields.io/badge/OpenClaw-%3E%3D2026.3.24--beta.2-6366f1)](https://docs.openclaw.ai/)
[![License](https://img.shields.io/badge/License-Non--Commercial-blue)](../../../LICENSE)

An [OpenClaw](https://docs.openclaw.ai/) memory-slot plugin backed by
[Ergo](../../../README.md) — decision memory that remembers *why*, and
refuses to silently contradict itself. See the sibling
[Hermes Agent plugin](../../memory/ergo/README.md) (`plugins/memory/ergo/`)
for the shared design; this is the OpenClaw (TypeScript) port of the same
scoping and never-auto-capture rules.

## What this plugin does (and deliberately does not do)

- **Recall is automatic.** Before every prompt build, this plugin hooks
  `before_prompt_build` and runs a read-only `GET /recall` against your Ergo
  engine, injecting any relevant decisions as `prependContext`. This is
  always on by default (`autoRecall: true`) and safe — reads never touch the
  contradiction gate, and a slow/unreachable engine degrades to "no results"
  instead of blocking or failing the turn.
- **Capture is NOT automatic**, and this is a deliberate difference from
  every other OpenClaw memory plugin in the ecosystem. Ergo is a *decision*
  store with mandatory reasons — `/why` deliberately can't surface a
  reason-less claim — and feeding every completed turn through the
  contradiction-guarded write path would produce spurious conflicts against
  real decisions, degrading the one feature Ergo exists for. `autoCapture`
  defaults to **`false`**. Writes only happen when the agent deliberately
  calls `memory_store` or one of the `ergo_*` tools below.
- An **opt-in** turn-capture path exists for operators who want it anyway:
  set `autoCapture: true` and each completed turn (`agent_end`) is posted to
  `POST /ingest` — the gate-free fact path, **never** `/remember` — with a
  bounded number of in-flight requests (dropped-and-logged past the cap,
  never queued unboundedly) and a best-effort flush on plugin teardown. Turn
  this on only if you understand that ingested chunks are never
  contradiction-checked.

### Hook choice: `before_prompt_build`, not `before_agent_start`

Both hooks exist in the published `openclaw` SDK and carry a system-prompt
injection payload, which is why the two reference memory plugins disagree:
the first-party `@openclaw/memory-lancedb` uses `before_prompt_build`, while
the third-party `openclaw-memory-mem0` uses `before_agent_start`. Checked
against `openclaw`'s own shipped type declarations (`dist/hook-types-*.d.ts`,
verified against `openclaw@2026.7.1-2`): `before_agent_start` is explicitly
marked `@deprecated` — *"Legacy compatibility hook that combines both
phases... Use before_model_resolve and before_prompt_build."* This plugin
uses the current, non-deprecated hook.

## Install

This plugin lives inside the `ergo` repository at `plugins/openclaw/ergo/`,
not as a separate published package (npm/ClawHub publishing has not been
authorized). Of OpenClaw's four documented install sources, only one was
actually exercised against a real `openclaw` install for this README —
the other three are documented for completeness but **unverified** here:

| Source | Command | Verified? |
|---|---|---|
| Local path (`--link`) | `openclaw plugins install --link ./plugins/openclaw/ergo` | **Yes** — see below |
| git | `openclaw plugins install git:<owner>/<repo>@<ref>` | No — this repo is currently private; also unclear whether OpenClaw's git installer supports installing a subdirectory of a larger repo the way Hermes's `hermes plugins install <owner>/<repo>/<subdir>` does. Try it once the repo is public, and if it fails, `git clone` + `--link` is the fallback. |
| npm | `openclaw plugins install npm:openclaw-plugin-ergo` | No — not published to npm |
| ClawHub | `openclaw plugins install clawhub:memory-ergo` | No — not published to ClawHub |

**Verified local-path install**, run against a real `openclaw@2026.7.1`
CLI in an isolated `--dev` profile:

```bash
git clone https://github.com/myzona-org/ergomem.git
cd ergomem/plugins/openclaw/ergo
npm install   # only real runtime dependency is @sinclair/typebox
openclaw plugins install --link "$(pwd)"
```

Or run [`./install.sh`](./install.sh) from this directory: it probes your
Ergo engine's `/health` and prints the exact config block above with real
values substituted, ready to paste. Requires `ERGO_PROJECT=<your-project>`
(no guessed default — this plugin's own directory is also named `ergo`, so
guessing from the current directory would silently mis-scope your memory).

Observed output:

```
Plugin manifest id "memory-ergo" differs from npm package name "openclaw-plugin-ergo"; using manifest id as the config key.
Exclusive slot "memory" switched from "memory-core" to "memory-ergo".
Linked plugin path: .../plugins/openclaw/ergo
Restart the gateway to load plugins.
```

`openclaw plugins inspect memory-ergo` then reported `Status: loaded` and
`Legacy before_agent_start: no` (confirming the hook-choice decision above
against the real loader), and `openclaw plugins list` showed it `enabled`
with OpenClaw's own `memory-core` correctly deactivated by the exclusive
slot switch.

Selecting it (done automatically by `--link` above, since `kind: "memory"`
triggers exclusive slot activation) is otherwise a config edit:

```json
{
  "plugins": {
    "slots": { "memory": "memory-ergo" },
    "entries": {
      "memory-ergo": {
        "config": {
          "baseUrl": "http://127.0.0.1:8788",
          "project": "my-project"
        }
      }
    }
  }
}
```

## Config

| key | type | default | notes |
|---|---|---|---|
| `baseUrl` | string | `http://127.0.0.1:8788` | Ergo API base URL. An **explicit empty string disables the plugin entirely** (no tools, no hooks registered) — the documented kill switch. |
| `apiToken` | string | unset | Ergo API bearer token. Marked `sensitive` in `uiHints`. Leave unset for an open/dev instance. |
| `project` | string | unset | See scoping below. |
| `autoRecall` | boolean | `true` | Inject relevant Ergo memory before every prompt. |
| `autoCapture` | boolean | `false` | Opt-in turn capture via `POST /ingest`. See above. |
| `recallLimit` | integer | `5` | Max memories injected per auto-recall / `memory_recall` call (clamped to `[1, 50]`). |

`org_id` and `who` are **not** configurable — they default to `"default"`
and `"openclaw-agent"` respectively, deliberately left out of the config
surface (same call as the Hermes sibling plugin), since they're rarely
meaningful to tune per OpenClaw install.

### Project scoping

Every claim is scoped to `(org_id, project)`. Two different code paths
resolve `project`, because the OpenClaw plugin SDK's tool-call surface and
its hook surface carry different context:

- **Tool calls** (`memory_recall`, `memory_store`, `ergo_*`, ...) always use
  the plugin's *statically* configured `project` (or the literal default
  `"openclaw"` if unset). Verified against the published `AgentTool` type:
  `execute(toolCallId, params, signal?, onUpdate?)` carries no per-call
  agent/session context, so there is nothing dynamic to fall back to here.
- **The auto-recall hook** (`before_prompt_build`) *does* receive a
  `PluginHookAgentContext` with an `agentId`, so when `project` was never
  explicitly configured, it falls back to the harness's `agentId` instead of
  the literal default — mirroring the Hermes plugin's
  config > harness-identity > literal-default precedence. An explicitly
  configured `project` always wins over `agentId`, even if it happens to
  equal the literal default string.

Every write/read call forces `org_id`/`project`/`who` onto the outgoing
request **after** merging in the tool's own arguments, never as a default a
caller could beat — and every tool's parameter schema sets
`additionalProperties: false`, so a model can't even attempt to smuggle a
different scope through tool arguments. Both defenses are independently
unit-tested (`tests/config.test.ts`, including a `forceScopeWrite`/
`forceScopeRead` mutation-proof: reverting the override-not-default merge
order fails the test suite).

## Tools

The memory slot's conventional three tools, so this is a drop-in backend for
any OpenClaw agent already using `memory_recall`/`memory_store`/
`memory_forget`:

| tool | endpoint | notes |
|---|---|---|
| `memory_recall` | `GET /recall` | Degrades to "no results" instead of failing when the engine is unreachable. |
| `memory_store` | `POST /remember` | A store is a deliberate act, so the contradiction gate applies. A `409` conflict comes back as structured tool output (`details.result === "conflict"`, with the conflicting claim + reason), never a thrown error — the agent decides whether to call `ergo_supersede`, store different content, or drop it. |
| `memory_forget` | `POST /retract` | **Not** supersede — wrong-at-birth removal only. Unlike the conventional `{id}` shape, this tool's schema also requires a `reason` (Ergo's `/retract` mandates one; a reason-less delete would violate the audit-loud design). `id` maps to Ergo's `claim_id` field. |

Plus the decision-ledger verbs no other memory plugin has, namespaced
`ergo_*` so they can't collide with the slot's conventional names. These
mirror [`ergo/mcp/server.py`](../../../ergo/mcp/server.py)'s naming,
arguments, and scoping — the same vocabulary, not a second one:

| tool | endpoint | notes |
|---|---|---|
| `ergo_why` | `GET /why` | Best REASONED belief + supersede history. A reason-less fact can never win this query. |
| `ergo_learn` | `POST /learn` | Guarded write with source provenance; same 409-as-data handling as `memory_store`. |
| `ergo_supersede` | `POST /supersede` | Replace an existing claim ("I changed my mind"); can also 409 if the revision itself conflicts. |
| `ergo_retract` | `POST /retract` | Same as `memory_forget`, native field names (`claim_id`, `reason`). |
| `ergo_active` | `GET /active` | Enumerate active claims (`limit`/`offset`). |
| `ergo_history` | `GET /history` | Full history incl. superseded/retracted. |
| `ergo_diagnose` | `GET /diagnose` | Ops view of the engine: counts, conflict stats, version-drift warnings. |
| `ergo_health` | `GET /health` | Liveness probe. |

`ergo_remember`, `ergo_recall`, and `ergo_ingest` are **deliberately not**
duplicated: `memory_store` and `memory_recall` already cover `/remember` and
`/recall`, and `/ingest` is reachable only through the opt-in `autoCapture`
path above, never as a manually-invoked tool.

## Development

No build step — OpenClaw loads `index.ts` directly (`package.json`'s
`"openclaw": { "extensions": ["./index.ts"] }`, same pattern as the
third-party `openclaw-memory-mem0` reference plugin), and this package's own
test suite runs the same raw TypeScript via Node's native type-stripping,
with no bundler and no `openclaw` installation required:

```bash
npm install     # installs the one real runtime dependency, @sinclair/typebox
npm test        # node --experimental-strip-types --test tests/*.test.ts
```

Requires Node 22.22.3+ (the same floor `openclaw` itself declares). Tests
stub the plugin-facing `api` object and the global `fetch` — they never
import or require an OpenClaw installation, so this package cannot leak a
Node toolchain requirement into this repo's own private dev-loop CI, which
stays Python-only and self-hosted. The public export's generated CI
workflow does run this suite, as a separate `openclaw-plugin-tests` job on a
plain GitHub-hosted runner with Node set up fresh each run — a public
contributor changing this plugin gets real CI feedback on it.

`index.ts` intentionally does **not** `import { definePluginEntry} from
"openclaw/plugin-sdk/plugin-entry"` at the top level — that's a real (VALUE,
not type-only) import from the `openclaw` package, and importing it would
break the "no OpenClaw installation required for tests" property the moment
any test imports `index.ts`. Instead it exports a plain object matching the
`DefinedPluginEntry` shape directly — verified against the published
`openclaw` type declarations to be exactly the normalized shape
`definePluginEntry` itself produces. `openclaw` stays a `peerDependency` for
its type declarations only.

No lockfile is committed. The only real dependency is `@sinclair/typebox`
(pinned to a `^0.34` range in `package.json`), and a local `npm install`
here will also auto-install `openclaw` as a peer (npm's default
auto-install-peers behavior) — locking *that* entire transitive tree in a
committed `package-lock.json` would be both enormous and misleading, since
`openclaw` itself is provided by whatever OpenClaw installation this plugin
is dropped into, not bundled by this package.
