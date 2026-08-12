# ergo-ingest-repo

A standalone CLI that walks a git repository and feeds it into the
[Ergo](../../README.md) memory engine's HTTP API. It does the things Ergo's
API does not do on its own:

1. **crawls a repo** (clone-from-URL or local path), selecting ingestible files,
2. **chunks code on definition boundaries** (`--mode ingest`, the default) and
   prepends a `# FILE: <path>` header to every blob, so semantic recall can
   surface *where* the code lives, and
3. **onboards a repo's DECISIONS, not its code** (`--mode decisions`): reads
   ONLY curated decision files (`CLAUDE.md`/`AGENTS.md`/`README.md`/ADR
   records), extracts candidate decision statements, and imports them through
   the **gated `/import`** route so Ergo's contradiction audit flags where a
   repo's written rules conflict with the existing store.

It does **not** modify Ergo — it is a pure HTTP client. stdlib + `requests` only,
matching Ergo's zero-heavy-deps philosophy.

## Usage

```bash
export ERGO_API_TOKEN=...          # required (read from env), except for --dry-run

# Ingest a public repo by URL (shallow clone, depth=1):
python3 ergo_ingest_repo.py https://github.com/psf/requests --project myrepo

# Ingest a local checkout:
python3 ergo_ingest_repo.py /path/to/repo --project myrepo

# See exactly what WOULD be ingested, post nothing:
python3 ergo_ingest_repo.py https://github.com/psf/requests --project myrepo --dry-run

# Full per-chunk provenance (see "Known limitation" below):
python3 ergo_ingest_repo.py /path/to/repo --project myrepo --max-blob-chars 1100
```

### Flags

| flag | default | meaning |
|------|---------|---------|
| `repo` (positional) | — | git URL (https/ssh/git) **or** a local directory path |
| `--mode` | `ingest` | `ingest` (default, unchanged) or `decisions` (see below) |
| `--project` | *(required for `ingest`)* | Ergo `project` scope. For `decisions`, defaults to the repo basename. |
| `--org-id` | `claude-code` | Ergo `org_id` scope |
| `--who` | `ergo-ingest-repo` | attribution stored on each claim |
| `--ergo-url` | `http://127.0.0.1:8788` | Ergo base URL |
| `--max-blob-chars` | `5000` | target size of each posted blob (`--mode ingest` only, see limitation) |
| `--max-files` | `0` (no cap) | cap number of files ingested (`--mode ingest` only, testing) |
| `--max-candidates` | `0` (no cap) | cap number of candidate decisions imported (`--mode decisions` only, testing / staying under a plan's `import_max_claims`) |
| `--dry-run` | off | list planned blobs/candidates, post nothing, exit |

The token is read from `ERGO_API_TOKEN`. Only `/health` is unauthenticated; the
tool pre-flights `/health` and aborts if `retrieval` is not `true`.

## Decision-onboarding mode (`--mode decisions`)

"Onboard your repo's DECISIONS, not your code." Instead of crawling every file,
`--mode decisions` reads **only** a curated whitelist of decision-bearing files,
extracts candidate decision statements from their markdown, and POSTs them to
Ergo's **gated `/import`** route (not `/ingest`) — the same contradiction audit
`/remember`/`/learn` run — so it flags where a repo's written rules conflict
with what's already in the store.

```bash
export ERGO_API_TOKEN=...

# Onboard a repo's decisions (project defaults to the repo basename):
python3 ergo_ingest_repo.py /path/to/repo --mode decisions

# See exactly which files/candidates WOULD be imported, post nothing:
python3 ergo_ingest_repo.py /path/to/repo --mode decisions --project myrepo --dry-run
```

Example output (the CONFLICTS section is the headline feature):

```
[decisions] repo=/path/to/repo project=myrepo
  files scanned:        4
  candidates extracted: 12
  imported:             10
  skipped (duplicate):  1
  conflicts:            1

CONFLICTS:
  - INCOMING: 'TLS 1.2 must not be enabled in prod.'
    AGAINST:  'ac8678352be4402d97925766e3f5614c'  (tier=block)
    REASON:   hard-vs-hard opposing stance, overlapping scope+time
{
  "repo": "/path/to/repo",
  "mode": "decisions",
  ...
}
```

### Curated file whitelist (nothing else — no code, no other docs)

- `CLAUDE.md`, `AGENTS.md` — any location, case-insensitive
- `README.md` — repo root + top-level directories only
- ADR / decision records: `docs/adr/**`, `docs/decisions/**`, `**/*.adr.md`,
  `ADR-*.md`, and `docs/DECISIONS.md`

### Extraction rules

- One candidate per **markdown list item** (bullets, including wrapped
  continuation lines) and per **short standalone rule-paragraph**.
- Skipped: code fences, tables, pure-link/badge lines, headings-only, blank
  lines, and over-long blocks (> ~600 chars — treated as prose, not a decision).
- Provenance: each claim's `source` is `<repo_name>:<relpath>` (plus
  `#<nearest heading>` when one is in scope). A non-empty `source` makes
  `/import` use `/learn` semantics, folding the provenance into the claim's
  reason so it's retrievable via `why`/`recall`.

### Handling the plan gate and size cap

`/import` is a Pro/Team-only route: a free-plan token gets a clear
`ERROR: Ergo plan does not allow /import (Pro/Team required): ...` message
(no stack trace), and a batch bigger than the plan's `import_max_claims`
gets a clear `ERROR: batch of N candidates exceeds the plan's
import_max_claims...` message — re-run with `--max-candidates` to shrink it.

## File-type allowlist

**Docs:** `.md .markdown .txt .rst .adoc`
**Code:** `.py .ts .tsx .js .jsx .go .rs .java .rb .c .h .cpp .sh .sql .yaml .yml .toml .json`

**Always skipped:**
- directories: `.git node_modules dist build vendor .venv venv __pycache__
  .mypy_cache .pytest_cache .tox .idea .vscode target`
- lockfiles / generated manifests: `package-lock.json yarn.lock pnpm-lock.yaml
  npm-shrinkwrap.json poetry.lock Pipfile.lock Cargo.lock composer.lock
  Gemfile.lock go.sum flake.lock`
- minified files: any `*.min.*`, or a file whose longest line > 2000 chars or
  whose average line length > 400 chars
- any file > 1 MB
- any file that fails a UTF-8 decode, or is empty/whitespace-only

## Chunking strategy

Every posted blob begins with a `# FILE: <relative/path>` header line and is
tagged with `source=<relative/path>:<startline>-<endline>`.

- **Code files** are split on **top-level definition boundaries** — column-0 lines
  matching `def / class / function / func / fn / const / let / var / type /
  interface / struct / impl / enum / module / package / trait / object / val / sub
  / proc`, plus `export`/`public`/`async` prefixes and `@decorator` lines (a
  decorator stays attached to the def it decorates). Consecutive definitions are
  packed greedily into blobs up to `--max-blob-chars`. A single definition larger
  than that is split into overlapping line windows.
- **Code files with no detectable boundaries** (and any oversized definition) fall
  back to **line-window chunks** (≤150 lines), capped by the char budget, with a
  15-line overlap. Splits are always on line boundaries — never mid-line.
- **Docs / prose** are packed line-by-line into char-budget blobs with a 2-line
  overlap. Ergo re-chunks prose internally anyway.

## Known limitation — neighborhood recall, not exact-line

Two layers of "fuzziness" are worth being honest about:

1. **Semantic recall returns neighborhoods, not exact lines.** `/recall` is hybrid
   semantic+keyword search. A hit landing on the topically-correct file/section is
   a success; do not expect it to point at the exact defining line.

2. **Ergo re-chunks every posted blob to ~1200 chars internally, and only the
   *first* internal sub-chunk keeps the `# FILE:` header.** So with the default
   `--max-blob-chars 5000`, most stored chunks lose their path header (measured:
   ~7/40 recalled chunks retained it on `psf/requests`). The provenance lives in
   the `# FILE:` header embedded in the statement text — Ergo's `/recall` does
   **not** echo back the `source` field at all.

   **Fix / workaround:** run with `--max-blob-chars 1100`. Each posted blob then
   stays under Ergo's re-chunk threshold, so 1 blob = 1 stored chunk and **every**
   recalled chunk keeps its `# FILE:` header (measured: 40/40 on `psf/requests`).
   The cost is roughly 2–3× more POST requests (one normalizer call each).

Provenance is therefore carried by the embedded `# FILE:` header, recoverable with
`re.match(r"# FILE: (\S+)", statement)`, not by Ergo's `source` field.
