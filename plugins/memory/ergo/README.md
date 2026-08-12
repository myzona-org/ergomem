# Ergo memory provider for Hermes Agent

[![Hermes Agent](https://img.shields.io/badge/Hermes%20Agent-memory--provider-6366f1)](https://github.com/NousResearch/hermes-agent)
[![License](https://img.shields.io/badge/License-Non--Commercial-blue)](../../../LICENSE)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory-provider
plugin backed by [Ergo](../../../README.md) — decision memory that remembers
*why*, and refuses to silently contradict itself.

## What this plugin does (and deliberately does not do)

- **Recall is automatic.** Before every turn, `prefetch()` runs a read-only
  `GET /recall` against your Ergo engine and injects any relevant decisions
  into context. This is always on and safe — reads never touch the
  contradiction gate.
- **Capture is NOT automatic.** `sync_turn()` (called after every completed
  turn) is a **no-op by default**. Ergo is a *decision* store with mandatory
  reasons — `/why` deliberately can't surface a reason-less claim — and
  feeding every chat turn through the contradiction-guarded write path would
  produce spurious conflicts against real decisions, degrading the one
  feature Ergo exists for. Writes only happen when the agent **deliberately
  calls one of the `ergo_*` tools below**.
- An **opt-in** turn-capture path exists for users who want it anyway: set
  `ERGO_HERMES_CAPTURE_TURNS=1` and each turn is posted to `POST /ingest` —
  the gate-free fact path, never `/remember` — on a background thread, so it
  never blocks the turn. It is skipped entirely outside a `primary` agent
  context (cron/subagent/flush runs never write). This is off by default and
  is not part of the guided setup wizard; turn it on only if you understand
  that ingested chunks are never contradiction-checked.

## Install

This plugin lives inside the `ergo` repository at
`plugins/memory/ergo/`, not as a separate pip package. Two ways to get it
into a Hermes installation:

**1. Directory drop (works today, verified against the loader's discovery
rules in `hermes_cli/plugins_cmd.py`):**

```bash
git clone https://github.com/myzona-org/ergomem.git
cp -r ergomem/plugins/memory/ergo ~/.hermes/plugins/memory/ergo
hermes memory setup ergo   # configure api_url / api_token / project
```

Or run [`./install.sh`](./install.sh) from this directory: it probes your
Ergo engine's `/health` and prints the exact `memory.provider` config above
with real values substituted, ready to paste. Requires `ERGO_PROJECT=<your-project>`
(no guessed default — this plugin's own directory is also named `ergo`, so
guessing from the current directory would silently mis-scope your memory).

Hermes discovers memory providers under `~/.hermes/plugins/memory/<name>/`
(user) the same way it discovers its own bundled `plugins/memory/<name>/`
providers — this is a plain directory with `plugin.yaml` + `__init__.py`,
no build step.

**2. `hermes plugins install <owner>/<repo>/plugins/memory/ergo`** — Hermes'
CLI supports installing a subdirectory of a larger git repo (see
`_resolve_git_url`/`_resolve_subdir_within` in `hermes_cli/plugins_cmd.py`),
which is exactly this layout. This is the intended path once the `ergo` repo
is publicly cloneable; it was verified by reading the installer's source
(subdirectory cloning + native `plugin.yaml` detection), not by running it
end-to-end, since Hermes is not installed in this repo's own environment.

A `pip install`-based route (`[project.entry-points."hermes_agent.plugins"]`)
was **not** set up: that path is for a plugin distributed as its own pip
package, and this plugin intentionally ships inside the `ergo` monorepo
instead of as a separate installable package (see "Why a duplicated HTTP
client" below).

Either way, select it as the active memory provider:

```yaml
# ~/.hermes/config.yaml
memory:
  provider: ergo
```

(Memory providers bypass the general `plugins.enabled` allow-list entirely —
every discovered provider, bundled or user-installed, is loaded, and exactly
one is *active* at a time, chosen by `memory.provider`. There is no separate
enable step. A copy at `~/.hermes/plugins/memory/ergo/` also overrides any
bundled provider of the same name — last-writer-wins in
`register_memory_provider()`.)

## Config

Set via `hermes memory setup ergo`, or directly:

| key | where | required | default |
|---|---|---|---|
| `api_url` | `~/.hermes/ergo/config.json`, or `ERGO_API_URL` | no | `http://127.0.0.1:8788` |
| `api_token` | `.env` (`ERGO_API_TOKEN`) — this is a `secret: true` field, Hermes writes it to `.env` for you | no | none (an open/dev Ergo instance needs none) |
| `project` | `~/.hermes/ergo/config.json` | no | see scoping below |

Everything else (org scoping, opt-in capture) is env-var only, deliberately
left out of the setup wizard:

| env var | default | effect |
|---|---|---|
| `ERGO_ORG_ID` | `default` | overrides the Ergo tenant/org id |
| `ERGO_WHO` | `hermes-agent` | actor label on audit rows |
| `ERGO_HERMES_CAPTURE_TURNS` | off | opt-in turn capture via `/ingest` (see above) |

### Project scoping

Every claim is scoped to `(org_id, project)`. `project` resolves in this
order, so a write never lands somewhere nobody chose:

1. The `project` field saved via `hermes memory setup ergo` (config file).
2. Hermes' own `agent_workspace` kwarg passed to `initialize()`, if set.
3. Hermes' own `agent_identity` kwarg, if set.
4. The literal default `"hermes"`.

Storage (the config file) always lives under the `hermes_home` passed to
`initialize()`/`save_config()` — never a hardcoded `~/.hermes`.

## Tools

Mirrors [`ergo/mcp/server.py`](../../../ergo/mcp/server.py)'s naming and
argument shape (same vocabulary, not a second one), minus the admin/Pro-plan
surface (`audit`, `diagnose`, `export`, `import`) that doesn't belong in a
chat tool list:

| tool | endpoint | notes |
|---|---|---|
| `ergo_remember` | `POST /remember` | guarded write; a `409` conflict comes back as normal tool output (`result: "conflict"`), never an exception — the agent decides whether to supersede, retry with `force_exception`, or drop it |
| `ergo_learn` | `POST /learn` | guarded write with source provenance; same 409 handling |
| `ergo_recall` | `GET /recall` | ad hoc search (prefetch already runs this automatically every turn) |
| `ergo_why` | `GET /why` | reasoned belief + supersede history |
| `ergo_supersede` | `POST /supersede` | replace an existing claim ("I changed my mind") |
| `ergo_retract` | `POST /retract` | remove a wrong-at-birth claim (reason required) |
| `ergo_ingest` | `POST /ingest` | bulk reference content, skips the gate |
| `ergo_active` | `GET /active` | enumerate active claims |
| `ergo_history` | `GET /history` | full history incl. superseded/retracted |
| `ergo_health` | `GET /health` | liveness probe |

## Why a duplicated HTTP client

This plugin does **not** import `ergo.mcp.server.ErgoClient`. It runs inside
a Hermes process with no `ergo-memory` package on its path — depending on it
would drag in the `engine` extra (fastembed, onnxruntime, ...) just to reuse
a small httpx wrapper. `_client.py` is a thin, independently-maintained
duplicate (same 409-as-data contract) rather than a shared import across
that runtime boundary.
