#!/usr/bin/env python3
"""ergo-ingest-repo — walk a git repo and feed it into Ergo's /ingest API.

A standalone client for the Ergo memory engine. It does the two things Ergo's
HTTP API does NOT do on its own:

  1. crawls a git repository (clone-or-local), selecting ingestible files, and
  2. performs *code-aware* chunking so retrieval can surface WHERE code lives.

Each posted blob is prefixed with a `# FILE: <relpath>` header and tagged with a
`source=<relpath>:<start>-<end>` line range. Ergo re-chunks each blob internally
(~1200 chars), so we post comfortably-sized 3-6 KB blobs split on definition
boundaries when we can detect them, else fixed line windows.

stdlib + requests only — matching Ergo's zero-heavy-deps philosophy.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

DEFAULT_ERGO_URL = "http://127.0.0.1:8788"

# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------

DOC_EXTS = {".md", ".markdown", ".txt", ".rst", ".adoc"}
CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb",
    ".c", ".h", ".cpp", ".sh", ".sql", ".yaml", ".yml", ".toml", ".json",
}
INGEST_EXTS = DOC_EXTS | CODE_EXTS

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "vendor", ".venv", "venv",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", ".idea", ".vscode",
    "target",  # rust/java build output
}

# Exact-name lockfiles / generated manifests we never want as "content".
SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "Pipfile.lock", "Cargo.lock", "composer.lock",
    "Gemfile.lock", "go.sum", "flake.lock",
}

MAX_FILE_BYTES = 1_000_000          # > 1 MB -> skip
MAX_BLOB_CHARS = 5_000              # target posted blob size (3-6 KB band)
WINDOW_LINES = 150                  # fallback line-window size
WINDOW_OVERLAP = 15                 # overlap between consecutive line windows
MINIFIED_MAX_LINE = 2_000           # any single line longer -> treat as minified
MINIFIED_AVG_LINE = 400             # avg line length over this -> minified

# Top-level definition boundary (applied only to lines with no indentation).
BOUNDARY_RE = re.compile(
    r"^(?:"
    r"@"                                            # decorator
    r"|(?:export\s+)?(?:default\s+)?"
    r"(?:public\s+|private\s+|protected\s+|static\s+|abstract\s+|final\s+)*"
    r"(?:async\s+)?"
    r"(?:def|class|function|func|fn|const|let|var|type|interface|struct|"
    r"impl|enum|module|package|trait|object|val|sub|proc)\b"
    r")"
)
DECORATOR_RE = re.compile(r"^@")


# ---------------------------------------------------------------------------
# Decision-onboarding mode (curated whitelist -> gated /import) --
# repo-decision-onboarding brief. Client-only: reads ONLY curated decision
# files (CLAUDE.md/AGENTS.md/README.md/ADR records), extracts markdown
# bullets + short rule-paragraphs as candidate decision statements, and
# imports them through the GATED /import route (contradiction-checked),
# never /ingest. The default `--mode ingest` crawl above is unchanged.
# ---------------------------------------------------------------------------

ADR_NAME_RE = re.compile(r"^adr-.*\.md$")

# Extraction regexes.
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
BULLET_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
# A line that is ONLY a link/image/badge (e.g. shields.io build badges) --
# never a decision statement on its own.
BADGE_OR_LINK_ONLY_RE = re.compile(
    r"^\s*(!?\[.*?\]\(.*?\)\s*)+[.,;:!?]?\s*$"
)

MAX_CANDIDATE_CHARS = 600  # over this -> prose, not a decision.


def repo_basename(repo_label: str) -> str:
    """Derive a short repo name from a URL or local path for `source=` provenance."""
    s = repo_label.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return Path(s).name or s


def is_curated_decision_file(rp: str) -> bool:
    """True if `rp` (posix relpath from repo root) is in the curated decision
    whitelist: CLAUDE.md/AGENTS.md (any location, case-insensitive), README.md
    (repo root + top-level dirs only), and ADR/decision records under
    docs/adr/**, docs/decisions/**, **/*.adr.md, ADR-*.md, docs/DECISIONS.md.
    """
    parts = rp.split("/")
    name = parts[-1]
    lname = name.lower()
    lower_path = rp.lower()

    if lname in ("claude.md", "agents.md"):
        return True
    if lname == "readme.md" and len(parts) <= 2:  # repo root or top-level dir
        return True
    if lower_path.startswith("docs/adr/") or lower_path.startswith("docs/decisions/"):
        return True
    if lname.endswith(".adr.md"):
        return True
    if ADR_NAME_RE.match(lname):
        return True
    if lower_path == "docs/decisions.md":
        return True
    return False


def walk_curated_files(root: Path):
    """Yield (abspath, relpath) for files in the curated decision whitelist.

    NOTHING else -- no code, no other docs. Reuses the same
    SKIP_DIRS as the default crawl so `.git`/`node_modules`/etc. never leak
    a false-positive CLAUDE.md.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.lower().endswith(".md"):
                continue
            ap = Path(dirpath) / fn
            rp = ap.relative_to(root).as_posix()
            if is_curated_decision_file(rp):
                yield ap, rp


def extract_candidates(text: str) -> list[tuple[str, str | None]]:
    """Parse markdown -> candidate decision statements.

    One candidate per markdown list item (bullet, wrapped continuation lines
    included) and per short standalone rule-paragraph. Skips code fences,
    tables, pure-link/badge lines, headings-only, blank lines, and over-long
    blocks (> ~600 chars -- prose, not a decision). Returns
    (statement, nearest_heading_or_None) tuples.
    """
    lines = text.split("\n")
    n = len(lines)
    candidates: list[tuple[str, str | None]] = []
    in_fence = False
    current_heading: str | None = None
    para_buf: list[str] = []

    def flush_para() -> None:
        if not para_buf:
            return
        block = " ".join(x.strip() for x in para_buf).strip()
        para_buf.clear()
        if block and len(block) <= MAX_CANDIDATE_CHARS:
            candidates.append((block, current_heading))

    i = 0
    while i < n:
        line = lines[i].rstrip()

        if FENCE_RE.match(line):
            flush_para()
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        if not line.strip():
            flush_para()
            i += 1
            continue
        if HEADING_RE.match(line):
            flush_para()
            current_heading = HEADING_RE.sub("", line).strip()
            i += 1
            continue
        if (TABLE_ROW_RE.match(line) or TABLE_SEP_RE.match(line)
                or HR_RE.match(line) or BADGE_OR_LINK_ONLY_RE.match(line)):
            flush_para()
            i += 1
            continue

        m = BULLET_RE.match(line)
        if m:
            flush_para()
            indent, _marker, rest = m.groups()
            base_indent = len(indent)
            text_parts = [rest.strip()]
            j = i + 1
            while j < n:
                cont = lines[j]
                if not cont.strip():
                    break
                if (BULLET_RE.match(cont) or HEADING_RE.match(cont)
                        or FENCE_RE.match(cont)):
                    break
                cont_indent = len(cont) - len(cont.lstrip())
                if cont_indent <= base_indent:
                    break
                text_parts.append(cont.strip())
                j += 1
            statement = " ".join(text_parts).strip()
            if statement and len(statement) <= MAX_CANDIDATE_CHARS:
                candidates.append((statement, current_heading))
            i = j
            continue

        para_buf.append(line)
        i += 1

    flush_para()
    return candidates


# ---------------------------------------------------------------------------
# Tally / summary
# ---------------------------------------------------------------------------

@dataclass
class Stats:
    files_scanned: int = 0
    files_ingested: int = 0
    chunks_stored: int = 0
    blobs_posted: int = 0
    skipped: int = 0
    errors: int = 0
    skip_reasons: dict = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


@dataclass
class DecisionStats:
    """Tally for `--mode decisions` (curated-file selection + extraction)."""
    files_scanned: int = 0
    files_skipped: int = 0
    candidates: int = 0
    skip_reasons: dict = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.files_skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def is_probably_minified(lines: list[str]) -> bool:
    if not lines:
        return False
    longest = max(len(ln) for ln in lines)
    if longest > MINIFIED_MAX_LINE:
        return True
    avg = sum(len(ln) for ln in lines) / len(lines)
    return avg > MINIFIED_AVG_LINE


def find_boundaries(lines: list[str]) -> list[int]:
    """Indices (0-based) of lines that start a top-level definition.

    Decorator lines are kept attached to the def/class they precede: a def whose
    immediately-preceding non-blank line is a decorator is NOT treated as a new
    boundary (the decorator above it already opened the segment).
    """
    boundaries: list[int] = []
    for i, ln in enumerate(lines):
        if not ln or ln[0].isspace():
            continue  # only column-0 lines are top-level
        if not BOUNDARY_RE.match(ln):
            continue
        if not DECORATOR_RE.match(ln):
            # Look back: if previous non-blank line was a decorator, skip.
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0 and DECORATOR_RE.match(lines[j].lstrip()) and not lines[j][0].isspace():
                continue
        boundaries.append(i)
    return boundaries


def _emit(blob_lines: list[str], start_idx: int, relpath: str) -> tuple[str, int, int]:
    """Build a posted blob from raw lines plus FILE header. Returns (text, start, end) 1-based."""
    body = "\n".join(blob_lines)
    text = f"# FILE: {relpath}\n{body}"
    start_line = start_idx + 1
    end_line = start_idx + len(blob_lines)
    return text, start_line, end_line


def window_chunks(lines: list[str], relpath: str, base_idx: int = 0,
                  max_chars: int = MAX_BLOB_CHARS) -> list[tuple[str, int, int]]:
    """Line-window chunking with overlap, capped by BOTH line count and char budget.

    A window grows until it hits WINDOW_LINES lines or would exceed max_chars,
    whichever comes first — so a small max_chars (atomic mode) keeps every blob
    under Ergo's internal re-chunk threshold and its `# FILE:` header survives.
    """
    out: list[tuple[str, int, int]] = []
    n = len(lines)
    header_overhead = len(relpath) + 12
    i = 0
    while i < n:
        j = i
        chars = 0
        while j < n and (j - i) < WINDOW_LINES and \
                (j == i or chars + len(lines[j]) + 1 + header_overhead <= max_chars):
            chars += len(lines[j]) + 1
            j += 1
        out.append(_emit(lines[i:j], base_idx + i, relpath))
        if j >= n:
            break
        i = max(j - WINDOW_OVERLAP, i + 1)
    return out


def chunk_code(lines: list[str], relpath: str,
               max_chars: int = MAX_BLOB_CHARS) -> list[tuple[str, int, int]]:
    """Pack top-level definition segments greedily into ~max_chars blobs."""
    boundaries = find_boundaries(lines)
    if not boundaries:
        return window_chunks(lines, relpath, max_chars=max_chars)

    # Build segments: [boundary_i, next_boundary). Any preamble before the first
    # boundary becomes its own leading segment.
    seg_bounds: list[tuple[int, int]] = []
    if boundaries[0] > 0:
        seg_bounds.append((0, boundaries[0]))
    for k, b in enumerate(boundaries):
        end = boundaries[k + 1] if k + 1 < len(boundaries) else len(lines)
        seg_bounds.append((b, end))

    out: list[tuple[str, int, int]] = []
    cur: list[str] = []
    cur_start = seg_bounds[0][0]
    cur_chars = 0
    header_overhead = len(relpath) + 12

    for (s, e) in seg_bounds:
        seg = lines[s:e]
        seg_chars = sum(len(x) + 1 for x in seg)

        # Flush the in-progress blob first if appending this segment overflows.
        if cur and cur_chars + seg_chars + header_overhead > max_chars:
            out.append(_emit(cur, cur_start, relpath))
            cur, cur_start, cur_chars = [], None, 0

        # A single segment too big to fit any blob: window-split it on its own.
        # (cur is guaranteed empty here — if it weren't, the flush above ran.)
        if seg_chars + header_overhead > max_chars:
            out.extend(window_chunks(seg, relpath, base_idx=s, max_chars=max_chars))
            continue

        if not cur:
            cur_start = s
        cur.extend(seg)
        cur_chars += seg_chars

    if cur:
        out.append(_emit(cur, cur_start, relpath))
    return out


def chunk_doc(lines: list[str], relpath: str,
              max_chars: int = MAX_BLOB_CHARS) -> list[tuple[str, int, int]]:
    """Prose: pack lines into ~max_chars blobs at line boundaries (small overlap)."""
    out: list[tuple[str, int, int]] = []
    cur: list[str] = []
    cur_start = 0
    cur_chars = 0
    header_overhead = len(relpath) + 12
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if cur and cur_chars + len(ln) + 1 + header_overhead > max_chars:
            out.append(_emit(cur, cur_start, relpath))
            # 2-line overlap for context continuity
            overlap_start = max(cur_start, i - 2)
            cur = lines[overlap_start:i]
            cur_start = overlap_start
            cur_chars = sum(len(x) + 1 for x in cur)
        if not cur:
            cur_start = i
        cur.append(ln)
        cur_chars += len(ln) + 1
        i += 1
    if cur:
        out.append(_emit(cur, cur_start, relpath))
    return out


def chunk_file(path: Path, relpath: str, stats: Stats,
               max_chars: int = MAX_BLOB_CHARS) -> list[tuple[str, int, int]] | None:
    """Read + chunk one file. Returns blobs or None if the file was skipped."""
    ext = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        stats.note_skip("read-error")
        return None
    if len(raw) > MAX_FILE_BYTES:
        stats.note_skip("too-large")
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        stats.note_skip("non-utf8")
        return None
    if not text.strip():
        stats.note_skip("empty")
        return None

    lines = text.split("\n")
    if is_probably_minified(lines):
        stats.note_skip("minified")
        return None

    if ext in CODE_EXTS:
        return chunk_code(lines, relpath, max_chars=max_chars)
    return chunk_doc(lines, relpath, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Walk
# ---------------------------------------------------------------------------

def should_skip_file(name: str) -> bool:
    if name in SKIP_NAMES:
        return True
    low = name.lower()
    if ".min." in low:  # *.min.js / *.min.css etc.
        return True
    return False


def walk_repo(root: Path):
    """Yield (abspath, relpath) for ingestible files, pruning skip dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            ext = Path(fn).suffix.lower()
            if ext not in INGEST_EXTS:
                continue
            if should_skip_file(fn):
                continue
            ap = Path(dirpath) / fn
            rp = ap.relative_to(root).as_posix()
            yield ap, rp


# ---------------------------------------------------------------------------
# Ergo client
# ---------------------------------------------------------------------------

class ImportGateError(Exception):
    """POST /import returned 403 -- plan does not allow the gated import route."""
    def __init__(self, body: dict):
        self.body = body
        super().__init__(body.get("error", str(body)))


class ImportTooLargeError(Exception):
    """POST /import returned 413 -- batch exceeds the plan's import_max_claims."""
    def __init__(self, body: dict):
        self.body = body
        super().__init__(body.get("error", str(body)))


class ErgoClient:
    def __init__(self, base_url: str, token: str, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def health(self) -> dict:
        r = self.session.get(f"{self.base}/health", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def ingest(self, org_id: str, project: str, who: str, text: str, source: str,
               max_retries: int = 5) -> dict:
        payload = {"org_id": org_id, "project": project, "who": who,
                   "text": text, "source": source}
        delay = 1.0
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = self.session.post(f"{self.base}/ingest", json=payload,
                                      timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = f"network: {exc}"
                if attempt == max_retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if attempt == max_retries:
                    break
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                time.sleep(wait)
                delay = min(delay * 2, 30)
                continue
            # Non-retryable
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError(f"exhausted retries: {last_err}")

    def recall(self, org_id: str, project: str, q: str, limit: int = 5) -> dict:
        r = self.session.get(f"{self.base}/recall",
                             params={"org_id": org_id, "project": project,
                                     "q": q, "limit": limit},
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def import_claims(self, org_id: str, target_project: str, who: str,
                       claims: list[dict], max_retries: int = 5) -> dict:
        """POST candidate decision claims to the GATED `/import` route (NOT
        `/ingest`) -- the contradiction audit (repo-decision-onboarding brief).
        Raises ImportGateError on 403 (free plan) and ImportTooLargeError on
        413 (batch exceeds the plan's import_max_claims), so the caller can
        surface a clear message instead of a stack trace.
        """
        payload = {"org_id": org_id, "target_project": target_project,
                   "who": who, "claims": claims}
        delay = 1.0
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                r = self.session.post(f"{self.base}/import", json=payload,
                                      timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = f"network: {exc}"
                if attempt == max_retries:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                raise ImportGateError(_safe_json(r))
            if r.status_code == 413:
                raise ImportTooLargeError(_safe_json(r))
            if r.status_code == 429 or r.status_code >= 500:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                if attempt == max_retries:
                    break
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
                time.sleep(wait)
                delay = min(delay * 2, 30)
                continue
            # Non-retryable
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError(f"exhausted retries: {last_err}")


def _safe_json(r) -> dict:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {"error": str(body)}
    except ValueError:
        return {"error": r.text[:300]}


# ---------------------------------------------------------------------------
# Repo acquisition
# ---------------------------------------------------------------------------

def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://", "git://"))


def acquire_repo(repo: str):
    """Return (root_path, cleanup_fn, repo_label). Clones URLs shallowly."""
    if is_url(repo):
        tmp = tempfile.mkdtemp(prefix="ergo-ingest-")
        print(f"[clone] shallow-cloning {repo} -> {tmp}", file=sys.stderr)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", repo, tmp],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"git clone failed: {exc.stderr.strip()}") from exc
        return Path(tmp), (lambda: shutil.rmtree(tmp, ignore_errors=True)), repo
    p = Path(repo).expanduser().resolve()
    if not p.is_dir():
        raise RuntimeError(f"local path is not a directory: {p}")
    return p, (lambda: None), str(p)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ergo-ingest-repo",
        description="Walk a git repo (URL or local path) and feed it into Ergo's "
                    "/ingest API with code-aware chunking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Reads the Ergo bearer token from the ERGO_API_TOKEN environment "
               "variable. PUBLIC OSS REPOS ONLY.",
    )
    p.add_argument("repo", help="git repo URL (https/ssh) OR a local directory path")
    p.add_argument("--mode", choices=["ingest", "decisions"], default="ingest",
                   help="'ingest' (default): the existing code-aware /ingest crawl. "
                        "'decisions': curated CLAUDE.md/AGENTS.md/README/ADR-only "
                        "selection, extracted into candidate decision statements and "
                        "imported through the gated /import route (contradiction audit).")
    p.add_argument("--org-id", default="claude-code",
                   help="Ergo org_id scope (default: claude-code)")
    p.add_argument("--project", default=None,
                   help="Ergo project scope. Required for --mode ingest. For "
                        "--mode decisions, defaults to the repo basename.")
    p.add_argument("--who", default="ergo-ingest-repo",
                   help="attribution for stored claims (default: ergo-ingest-repo)")
    p.add_argument("--ergo-url", default=DEFAULT_ERGO_URL,
                   help=f"Ergo base URL (default: {DEFAULT_ERGO_URL})")
    p.add_argument("--dry-run", action="store_true",
                   help="list what WOULD be ingested/imported and exit without "
                        "posting anything")
    p.add_argument("--max-blob-chars", type=int, default=MAX_BLOB_CHARS,
                   help=f"target size of each posted blob (default: {MAX_BLOB_CHARS}). "
                        "Ergo re-chunks blobs to ~1200 chars internally and only the "
                        "FIRST sub-chunk keeps the '# FILE:' header — so set this to "
                        "~1100 for full per-chunk provenance at the cost of more POSTs. "
                        "(--mode ingest only)")
    p.add_argument("--max-files", type=int, default=0,
                   help="cap number of files ingested (0 = no cap; for testing). "
                        "(--mode ingest only)")
    p.add_argument("--max-candidates", type=int, default=0,
                   help="cap number of candidate decisions imported (0 = no cap; "
                        "for testing / staying under a plan's import_max_claims). "
                        "(--mode decisions only)")
    return p


def print_decision_report(repo_label: str, project: str, dstats: "DecisionStats",
                          candidates: list[dict], resp: dict) -> None:
    """Print the decision-onboarding report -- the headline feature:
    files scanned, candidates extracted, imported, skipped-dupes, and a
    CONFLICTS section listing each conflict (incoming ⟂ existing + reason).
    """
    conflicts = resp.get("conflicts") or []
    print(f"[decisions] repo={repo_label} project={project}")
    print(f"  files scanned:        {dstats.files_scanned}")
    print(f"  candidates extracted: {len(candidates)}")
    print(f"  imported:             {resp.get('imported', 0)}")
    print(f"  skipped (duplicate):  {resp.get('skipped_duplicate', 0)}")
    print(f"  conflicts:            {len(conflicts)}")
    if resp.get("cap_reached"):
        print("  NOTE: plan's active-claim cap was reached; import stopped early.")
    if conflicts:
        print()
        print("CONFLICTS:")
        for c in conflicts:
            print(f"  - INCOMING: {c.get('statement')!r}")
            print(f"    AGAINST:  {c.get('against')!r}  (tier={c.get('tier')})")
            print(f"    REASON:   {c.get('reason')}")
    summary = {
        "repo": repo_label,
        "mode": "decisions",
        "project": project,
        "files_scanned": dstats.files_scanned,
        "files_skipped": dstats.files_skipped,
        "skip_reasons": dstats.skip_reasons,
        "candidates": len(candidates),
        "imported": resp.get("imported", 0),
        "skipped_duplicate": resp.get("skipped_duplicate", 0),
        "conflict_count": resp.get("conflict_count", len(conflicts)),
        "cap_reached": resp.get("cap_reached", False),
    }
    print(json.dumps(summary, indent=2))


def run_decisions_mode(args, root: Path, repo_label: str,
                       client: "ErgoClient | None") -> int:
    """Curated whitelist -> extract -> gated /import. Never
    touches /ingest. `client` is None only when args.dry_run is set."""
    repo_name = repo_basename(repo_label)
    project = args.project or repo_name
    dstats = DecisionStats()
    candidates: list[dict] = []

    for ap, rp in walk_curated_files(root):
        dstats.files_scanned += 1
        try:
            raw = ap.read_bytes()
        except OSError:
            dstats.note_skip("read-error")
            continue
        if len(raw) > MAX_FILE_BYTES:
            dstats.note_skip("too-large")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            dstats.note_skip("non-utf8")
            continue

        for statement, heading in extract_candidates(text):
            source = f"{repo_name}:{rp}"
            if heading:
                source += f"#{heading}"
            candidates.append({"statement": statement, "source": source,
                               "project": project})
            dstats.candidates += 1

    if args.max_candidates and len(candidates) > args.max_candidates:
        candidates = candidates[:args.max_candidates]

    if args.dry_run:
        for c in candidates:
            preview = c["statement"][:80].replace("\n", " ")
            print(f"WOULD IMPORT  {c['source']}  {preview!r}")
        summary = {
            "repo": repo_label,
            "mode": "decisions",
            "project": project,
            "files_scanned": dstats.files_scanned,
            "files_skipped": dstats.files_skipped,
            "skip_reasons": dstats.skip_reasons,
            "candidates": len(candidates),
            "dry_run": True,
        }
        print(json.dumps(summary, indent=2))
        return 0

    if not candidates:
        print_decision_report(repo_label, project, dstats, candidates, {
            "imported": 0, "skipped_duplicate": 0, "conflicts": [],
            "conflict_count": 0, "cap_reached": False,
        })
        return 0

    assert client is not None
    try:
        resp = client.import_claims(args.org_id, project, args.who, candidates)
    except ImportGateError as exc:
        print(f"ERROR: Ergo plan does not allow /import (Pro/Team required): "
              f"{exc.body.get('error', exc.body)}", file=sys.stderr)
        return 2
    except ImportTooLargeError as exc:
        cap = exc.body.get("import_max_claims")
        hint = f" (plan cap: {cap})" if cap is not None else ""
        print(f"ERROR: batch of {len(candidates)} candidates exceeds the plan's "
              f"import_max_claims{hint}. Re-run with --max-candidates to shrink "
              f"the batch.", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: import failed: {exc}", file=sys.stderr)
        return 2

    print_decision_report(repo_label, project, dstats, candidates, resp)
    return 1 if resp.get("conflict_count") else 0


def run_ingest_mode(args, root: Path, repo_label: str,
                    client: "ErgoClient | None", t0: float) -> int:
    """The existing code-aware /ingest crawl -- unchanged default behavior."""
    stats = Stats()
    for ap, rp in walk_repo(root):
        stats.files_scanned += 1
        blobs = chunk_file(ap, rp, stats, max_chars=args.max_blob_chars)
        if blobs is None:
            continue
        if not blobs:
            stats.note_skip("no-content")
            continue

        if args.max_files and stats.files_ingested >= args.max_files:
            stats.note_skip("max-files-cap")
            continue

        if args.dry_run:
            for (_text, s, e) in blobs:
                print(f"WOULD INGEST  {rp}:{s}-{e}  (chars={len(_text)})")
            stats.files_ingested += 1
            stats.blobs_posted += len(blobs)
            continue

        file_chunks = 0
        file_failed = False
        for (text, s, e) in blobs:
            source = f"{rp}:{s}-{e}"
            try:
                resp = client.ingest(args.org_id, args.project, args.who,
                                     text, source)
                stored = int(resp.get("stored", resp.get("chunks", 0)))
                file_chunks += stored
                stats.chunks_stored += stored
                stats.blobs_posted += 1
            except Exception as exc:
                stats.errors += 1
                file_failed = True
                print(f"  [err] {source}: {exc}", file=sys.stderr)
        if file_chunks or not file_failed:
            stats.files_ingested += 1
        print(f"[ingest] {rp}  blobs={len(blobs)} stored={file_chunks}  "
              f"(files={stats.files_ingested} chunks={stats.chunks_stored} "
              f"errors={stats.errors})", file=sys.stderr)

    elapsed = round(time.time() - t0, 2)
    summary = {
        "repo": repo_label,
        "files_scanned": stats.files_scanned,
        "files_ingested": stats.files_ingested,
        "chunks_stored": stats.chunks_stored,
        "blobs_posted": stats.blobs_posted,
        "skipped": stats.skipped,
        "skip_reasons": stats.skip_reasons,
        "errors": stats.errors,
        "elapsed_s": elapsed,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, indent=2))
    return 1 if stats.errors else 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    t0 = time.time()

    if args.mode == "ingest" and not args.project:
        print("ERROR: --project is required for --mode ingest.", file=sys.stderr)
        return 2

    token = os.environ.get("ERGO_API_TOKEN", "")
    if not args.dry_run and not token:
        print("ERROR: ERGO_API_TOKEN is not set in the environment.", file=sys.stderr)
        return 2

    try:
        root, cleanup, repo_label = acquire_repo(args.repo)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    client = None
    if not args.dry_run:
        client = ErgoClient(args.ergo_url, token)
        try:
            h = client.health()
        except Exception as exc:
            cleanup()
            print(f"ERROR: cannot reach Ergo at {args.ergo_url}: {exc}", file=sys.stderr)
            return 2
        if not h.get("retrieval"):
            cleanup()
            print(f"ERROR: Ergo retrieval not available: {json.dumps(h)}", file=sys.stderr)
            return 2

    try:
        if args.mode == "decisions":
            return run_decisions_mode(args, root, repo_label, client)
        return run_ingest_mode(args, root, repo_label, client, t0)
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
