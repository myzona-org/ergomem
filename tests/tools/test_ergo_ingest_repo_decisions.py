"""Tests for the `--mode decisions` curated decision-onboarding path of
tools/ergo-ingest-repo/ergo_ingest_repo.py (repo-decision-onboarding brief).

The default `--mode ingest` crawl (existing behavior) is untouched by this
brief -- these tests exercise ONLY the new curated-file selection, markdown
extraction, and gated `/import` integration.

The tool lives in a hyphenated directory (`tools/ergo-ingest-repo/`) that is
not an importable package name, so it is loaded directly from its file path
via importlib rather than `import ergo_ingest_repo`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_TOOL_DIR = pathlib.Path(__file__).resolve().parents[2] / "tools" / "ergo-ingest-repo"
_MODULE_PATH = _TOOL_DIR / "ergo_ingest_repo.py"


def _load_module():
    mod_name = "ergo_ingest_repo_decisions_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass() introspects sys.modules[cls.__module__] -- register before exec.
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


eir = _load_module()


# ---------------------------------------------------------------------------
# 1. Selection — curated whitelist ONLY.
# ---------------------------------------------------------------------------
def _write(root: pathlib.Path, relpath: str, content: str = "placeholder\n") -> None:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_curated_selection_includes_only_the_whitelist(tmp_path):
    root = tmp_path / "repo"

    curated = [
        "CLAUDE.md",
        "AGENTS.md",
        "sub/agents.md",              # case-insensitive, any location
        "README.md",                  # repo root
        "pkg/README.md",              # top-level dir
        "docs/adr/0001-decision.md",  # docs/adr/**
        "docs/decisions/foo.md",      # docs/decisions/**
        "docs/DECISIONS.md",          # exact file
        "ADR-0002-thing.md",          # ADR-*.md anywhere
        "notes/0003-thing.adr.md",    # **/*.adr.md anywhere
    ]
    excluded = [
        "pkg/deep/README.md",         # not root or top-level dir
        "random.md",                  # not in whitelist
        "docs/other.md",              # random doc, not curated
        "src/main.py",                # code
        "src/main.js",                # code
        ".git/CLAUDE.md",             # inside a skip dir
        "node_modules/AGENTS.md",     # inside a skip dir
    ]
    for rp in curated + excluded:
        _write(root, rp)

    selected = sorted(rp for _ap, rp in eir.walk_curated_files(root))
    assert selected == sorted(curated)


# ---------------------------------------------------------------------------
# 1b. BADGE_OR_LINK_ONLY_RE — ReDoS fix (CodeQL alerts 1+2): the original
# `(!?\[.*?\]\(.*?\)\s*)+` backtracked exponentially on repeated `[]()`
# groups with no terminator. Negated character classes remove the ambiguity.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line,expected", [
    ("[a](b)", True),
    ("![img](u)  [x](y)", True),
    ("  [a](b) .", True),
    ("[a](b) trailing text", False),
    ("not a link", False),
    ("![badge](https://shields.io/x) [ci](https://ci/y)", True),
    ("", False),
    # Regression caught by test_extract_candidates_from_sample_claude_md:
    # the canonical clickable-CI-badge idiom, a link whose text is a single
    # embedded image. A naive `[^\]]*` link-text class (stops at the badge's
    # own `]`) fails to filter this -- it needs the explicit nested-image
    # alternative in _LINK_TEXT.
    ("[![Build Status](https://ci.example.com/badge.svg)](https://ci.example.com)", True),
])
def test_badge_or_link_only_re_equivalence(line, expected):
    assert bool(eir.BADGE_OR_LINK_ONLY_RE.match(line)) == expected


def test_badge_or_link_only_re_is_linear_time_not_exponential():
    """ReDoS regression: `"[]()" * 20 + "@"` (81 bytes, unterminated) took
    ~15s pre-fix on the exponential-backtracking pattern (growth is ~x7 per
    2 extra reps). The fixed pattern is sub-millisecond; a generous 2s
    ceiling keeps this from flaking on a loaded CI box either way."""
    import time

    pathological = "[]()" * 20 + "@"
    start = time.monotonic()
    eir.BADGE_OR_LINK_ONLY_RE.match(pathological)
    assert time.monotonic() - start < 2.0


# ---------------------------------------------------------------------------
# 2. Extraction — markdown -> candidate decision statements.
# ---------------------------------------------------------------------------
SAMPLE_CLAUDE_MD = """\
# Project rules

## Conventions

- Use Postgres for the primary store, never SQLite in prod.
- All API routes must return JSON, including
  errors (no HTML error pages).
- Retry network calls up to 3 times.

Deploys happen only on Tuesdays and Thursdays.

```python
# - this looks like a bullet but is inside a code fence
def foo():
    pass
```

| flag | default |
|------|---------|
| --x  | 1       |

[![Build Status](https://ci.example.com/badge.svg)](https://ci.example.com)

## Out of scope

This paragraph is intentionally way too long to ever be treated as a single
candidate decision statement, because Ergo's `/import` extraction rule skips
any block whose text runs past roughly six hundred characters on the theory
that something that long is prose or narrative documentation rather than a
crisp, atomic, reviewable decision statement that a future reader or a
contradiction-detection pass could usefully reason about on its own -- so
this filler keeps going well beyond that six-hundred-character threshold
purely to exercise the over-long-block skip path in the extractor.
"""


def test_extract_candidates_from_sample_claude_md():
    candidates = eir.extract_candidates(SAMPLE_CLAUDE_MD)
    statements = [c[0] for c in candidates]

    assert "Use Postgres for the primary store, never SQLite in prod." in statements
    assert ("All API routes must return JSON, including errors (no HTML error pages)."
           in statements)
    assert "Retry network calls up to 3 times." in statements
    assert "Deploys happen only on Tuesdays and Thursdays." in statements

    # Skipped: code fence content, table rows, badge/link-only line, and the
    # over-long paragraph.
    joined = "\n".join(statements)
    assert "this looks like a bullet" not in joined
    assert "--x" not in joined
    assert "Build Status" not in joined
    assert not any(len(s) > 600 for s in statements)

    # Heading tracking: bullets under "## Conventions" carry that heading.
    by_statement = dict(candidates)
    assert by_statement["Retry network calls up to 3 times."] == "Conventions"


# ---------------------------------------------------------------------------
# 3. Import integration — POSTs to /import (never /ingest), with
#    {statement, source, project}; a `conflicts` entry is surfaced in the
#    printed report.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._payload


def test_import_claims_posts_to_import_route_with_expected_body(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {
            "imported": 1, "skipped_duplicate": 0,
            "conflicts": [], "conflict_count": 0, "cap_reached": False,
        })

    client = eir.ErgoClient("http://127.0.0.1:8788", "tok")
    monkeypatch.setattr(client.session, "post", fake_post)

    resp = client.import_claims("acme", "myrepo", "ergo-ingest-repo", [
        {"statement": "Deploy on Tuesdays.", "source": "myrepo:CLAUDE.md",
         "project": "myrepo"},
    ])

    assert captured["url"] == "http://127.0.0.1:8788/import"
    assert captured["json"]["org_id"] == "acme"
    assert captured["json"]["target_project"] == "myrepo"
    claim = captured["json"]["claims"][0]
    assert claim["statement"] == "Deploy on Tuesdays."
    assert claim["source"] == "myrepo:CLAUDE.md"
    assert claim["project"] == "myrepo"
    assert resp["imported"] == 1

    # Never posts decisions to /ingest.
    assert "/ingest" not in captured["url"]


def test_run_decisions_mode_surfaces_conflicts_in_report(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    _write(root, "CLAUDE.md", "- TLS 1.2 must not be enabled in prod.\n")

    def fake_post(url, json=None, timeout=None):
        return _FakeResponse(200, {
            "imported": 0, "skipped_duplicate": 0,
            "conflicts": [{
                "statement": "TLS 1.2 must not be enabled in prod.",
                "against": "claim-123",
                "reason": "contradicts: TLS 1.2 must be enabled in prod.",
                "tier": "block",
            }],
            "conflict_count": 1,
            "cap_reached": False,
        })

    args = eir.build_parser().parse_args([
        str(root), "--mode", "decisions", "--project", "myrepo",
    ])
    client = eir.ErgoClient("http://127.0.0.1:8788", "tok")
    monkeypatch.setattr(client.session, "post", fake_post)

    rc = eir.run_decisions_mode(args, root, str(root), client)

    out = capsys.readouterr().out
    assert "CONFLICTS" in out
    assert "TLS 1.2 must not be enabled in prod." in out
    assert "contradicts: TLS 1.2 must be enabled in prod." in out
    assert rc != 0  # conflicts present -> non-zero exit


def test_import_gate_403_reports_clearly_not_a_stack_trace(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    _write(root, "CLAUDE.md", "- Some decision statement here.\n")

    def fake_post(url, json=None, timeout=None):
        return _FakeResponse(403, {"error": "import_requires_pro_or_team",
                                   "plan": "free"})

    args = eir.build_parser().parse_args([
        str(root), "--mode", "decisions", "--project", "myrepo",
    ])
    client = eir.ErgoClient("http://127.0.0.1:8788", "tok")
    monkeypatch.setattr(client.session, "post", fake_post)

    rc = eir.run_decisions_mode(args, root, str(root), client)
    err = capsys.readouterr().err
    assert rc == 2
    assert "Pro/Team" in err
    assert "Traceback" not in err


def test_import_size_cap_413_reports_clearly(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    _write(root, "CLAUDE.md", "- Some decision statement here.\n")

    def fake_post(url, json=None, timeout=None):
        return _FakeResponse(413, {"error": "import of 1 claims exceeds the plan's "
                                            "import_max_claims of 0",
                                   "import_max_claims": 0})

    args = eir.build_parser().parse_args([
        str(root), "--mode", "decisions", "--project", "myrepo",
    ])
    client = eir.ErgoClient("http://127.0.0.1:8788", "tok")
    monkeypatch.setattr(client.session, "post", fake_post)

    rc = eir.run_decisions_mode(args, root, str(root), client)
    err = capsys.readouterr().err
    assert rc == 2
    assert "import_max_claims" in err
    assert "Traceback" not in err


def test_decisions_mode_dry_run_lists_candidates_without_posting(tmp_path, capsys):
    root = tmp_path / "repo"
    _write(root, "CLAUDE.md", "- Use Postgres, never SQLite.\n")

    args = eir.build_parser().parse_args([
        str(root), "--mode", "decisions", "--project", "myrepo", "--dry-run",
    ])
    rc = eir.run_decisions_mode(args, root, str(root), None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "WOULD IMPORT" in out
    assert "Use Postgres, never SQLite." in out


# ---------------------------------------------------------------------------
# Default `/ingest` crawl stays unchanged (regression guard).
# ---------------------------------------------------------------------------
def test_default_mode_is_ingest_and_project_still_required():
    args = eir.build_parser().parse_args(["/tmp/somewhere", "--project", "p"])
    assert args.mode == "ingest"


def test_ingest_mode_without_project_is_still_an_error(capsys):
    rc = eir.main(["/tmp/somewhere"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--project" in err
