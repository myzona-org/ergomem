"""Claim extractor — `prose -> candidate atomic modal statements`.

The ingestion front-end for real documents (normalize comes after we
have something to normalize). This is intentionally simple and lossy: we strip
markdown structure, split into sentences, keep only the ones containing a modal
verb, and lightly clean them. The downstream normalizer (`RuleNormalizer` or
`LLMNormalizer`) does the structured field extraction.

Design notes:
- Strips fenced code blocks (multi-line, lots of false 'should/may' inside) but
  PRESERVES inline-code *content* (the backticks are removed, the text kept) —
  inline code is almost always an identifier/flag/field name that IS the subject
  of the claim ("`--no-cache` cannot be combined with ..."). Deleting it produced
  garbage claims like "Cannot be combined with ." (the 2026-06-26 ingest FP).
- Strips MD headings/list bullets/links so the statement text is readable.
- Conservative sentence splitter on `.?!\\n` with abbreviation guards.
- Statements shorter than 25 chars or longer than 350 chars are dropped (noise).
- Returns provenance: (text, source_path, line_no) so the ingest report can
  point you at the file.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MODAL_PATTERNS = [
    r"\bmust not\b", r"\bmust\b", r"\bshall not\b", r"\bshall\b",
    r"\bshould not\b", r"\bshould\b", r"\bmay not\b", r"\bmay\b",
    r"\bcannot\b", r"\bcan't\b", r"\bdo not\b", r"\bdon't\b",
    r"\bnever\b", r"\balways\b",
    r"\brequired\b", r"\bforbidden\b", r"\bprohibited\b",
    r"\bnot allowed\b", r"\bnot recommended\b",
    r"\bis required to\b", r"\bare required to\b",
]
MODAL_RE = re.compile("|".join(MODAL_PATTERNS), re.IGNORECASE)

# Trailing period after these tokens isn't a sentence boundary.
_ABBREV = {"e.g", "i.e", "etc", "vs", "cf", "mr", "mrs", "ms", "dr", "fig",
           "no", "v", "approx", "incl"}


@dataclass
class Candidate:
    text: str
    source: str
    line: int


# --- markdown cleanup ----------------------------------------------------
_FENCE_RE = re.compile(r"```.*?```", re.S)
# Capture the inner text so we can KEEP it (strip only the backticks).
_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_MD_HEADING = re.compile(r"^\s*#{1,6}\s+", re.M)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)
_MD_NUMBERED = re.compile(r"^\s*\d+\.\s+", re.M)
_MD_BOLD_ITALIC = re.compile(r"[*_]{1,3}")
_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def clean_markdown(raw: str) -> str:
    s = _FRONTMATTER.sub("", raw)
    s = _FENCE_RE.sub(" ", s)
    s = _MD_IMG.sub(" ", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _INLINE_CODE.sub(r"\1", s)   # keep inline-code content (drop only backticks)
    s = _MD_HEADING.sub("", s)
    s = _MD_BULLET.sub("", s)
    s = _MD_NUMBERED.sub("", s)
    s = _MD_BOLD_ITALIC.sub("", s)
    s = _HTML_TAG.sub(" ", s)
    return s


# --- inline-code masking for the structured normalizer -------------------
# clean_markdown() (above) keeps inline-code *content* because the identifier is
# usually the subject of a claim — that decision is load-bearing (the 2026-06-26
# "Cannot be combined with ." FP). But the RuleNormalizer scans the RAW remember()
# text, which never passes through clean_markdown, AND even content-preserving
# stripping leaves a trap: a backtick identifier that contains a modal- or
# object-looking sub-token joined by hyphens/underscores will hijack the modality
# boundary / object scan. Real example: the HTTP Cache-Control directive
# `must-revalidate` — the `\bmust\b` inside the identifier (hyphen is a word
# boundary) gets picked as the claim's modal verb, shredding subject + modality.
#
# mask_inline_code() replaces each span with a digit-only sentinel that matches no
# modal/object keyword, so scanning sees only the real prose. unmask_inline_code()
# restores the literal identifier text (backticks dropped) for the subject/object.
_CODE_SENTINEL = re.compile(r"\x00(\d+)\x00")


def mask_inline_code(text: str) -> tuple[str, list[str]]:
    """Replace each `inline code` span with a \\x00<idx>\\x00 sentinel.

    Returns (masked_text, spans) where spans[idx] is the literal code content
    (backticks already stripped). The sentinel is digits + NUL only, so no
    modal/object/scope regex can match inside it.
    """
    spans: list[str] = []

    def repl(m: re.Match) -> str:
        spans.append(m.group(1).strip())
        return f" \x00{len(spans) - 1}\x00 "

    return _INLINE_CODE.sub(repl, text), spans


def unmask_inline_code(text: str, spans: list[str]) -> str:
    """Inverse of mask_inline_code(): restore code content for the given spans."""
    def repl(m: re.Match) -> str:
        i = int(m.group(1))
        return spans[i] if 0 <= i < len(spans) else m.group(0)

    return _CODE_SENTINEL.sub(repl, text)


# --- sentence splitter ---------------------------------------------------
_SENT_BOUNDARY = re.compile(r"([.!?])\s+(?=[A-Z(\"'`])|\n{2,}")

def split_sentences(text: str) -> list[str]:
    # protect abbreviations by lowercasing the period
    def protect(m):
        token = m.group(1).lower().rstrip(".")
        if token in _ABBREV:
            return m.group(1) + "_DOT_"
        return m.group(0)
    text = re.sub(r"\b(\w+)\.", protect, text)
    parts = _SENT_BOUNDARY.split(text)
    # recombine sentence + delimiter (capture-group can yield None)
    sents = []
    buf = ""
    for p in parts:
        if p is None:
            continue
        if p in (".", "!", "?"):
            sents.append((buf + p).strip())
            buf = ""
        else:
            buf += p
    if buf.strip():
        sents.append(buf.strip())
    # restore protected periods
    return [s.replace("_DOT_", ".") for s in sents]


# --- main extractor ------------------------------------------------------
def extract_from_text(text: str, source: str = "<inline>") -> list[Candidate]:
    cleaned = clean_markdown(text)
    out: list[Candidate] = []
    # rough line numbers via prefix-length matching on the original
    lines = text.splitlines()
    line_idx_by_prefix = {}
    cum = 0
    for i, line in enumerate(lines, 1):
        line_idx_by_prefix[cum] = i
        cum += len(line) + 1

    def best_line(probe: str) -> int:
        # cheap: search the original for the first 30 chars
        key = probe[:30].strip().lower()
        if not key:
            return 0
        low = text.lower()
        idx = low.find(key)
        if idx < 0:
            return 0
        # walk down keys
        ln = 1
        for k in sorted(line_idx_by_prefix):
            if k > idx:
                break
            ln = line_idx_by_prefix[k]
        return ln

    for sent in split_sentences(cleaned):
        s = _WHITESPACE.sub(" ", sent).strip(" >|")
        if len(s) < 25 or len(s) > 350:
            continue
        if not MODAL_RE.search(s):
            continue
        # crude noise filters: section anchors, table separators, code-looking
        if s.startswith("|") or s.startswith("```") or s.startswith("@"):
            continue
        out.append(Candidate(text=s, source=source, line=best_line(s)))
    return out


def extract_from_files(paths: Iterable[Path]) -> list[Candidate]:
    out: list[Candidate] = []
    for p in paths:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        out.extend(extract_from_text(raw, source=str(p)))
    return out


def discover_md(roots: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        root = Path(root).expanduser()
        if root.is_file() and root.suffix.lower() == ".md":
            paths.append(root)
        elif root.is_dir():
            paths.extend(sorted(root.rglob("*.md")))
    return paths


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--max", type=int, default=20)
    args = ap.parse_args()
    cands = extract_from_files(discover_md([Path(p) for p in args.paths]))
    print(f"# candidates: {len(cands)}")
    for c in cands[: args.max]:
        rel = c.source.split("/")[-1]
        print(f"  [{rel}:{c.line}] {c.text}")
