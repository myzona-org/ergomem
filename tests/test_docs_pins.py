"""Guards against a recurring drift class: a `==`-pinned dependency version
gets hand-copied into a comment/doc/script, and only ONE of the copies gets
updated on the next bump. Round-4 pre-flip fixed the copy in docs/MCP.md but
missed two others (a Dockerfile comment and the MCP launcher script's
comment) because the old version of this test only read one hand-named path
(`docs/MCP.md`) -- it was structurally unable to catch the other two.

This derives every `==`-pinned distribution straight from pyproject.toml (the
single source of truth) and then scans every git-tracked file for
restatements of `<dist>==<version>`, asserting the version always matches the
pin. A stale copy anywhere in the tree fails the suite now, not just in
whichever one file a human happened to check.

Deliberately does not restate any pinned version number here -- including in
this docstring -- because this file is itself tracked and gets scanned along
with everything else, so a hardcoded example would eventually go stale and
fail the very test it sits in. Everything is derived from pyproject.toml at
test time.

Static parse only -- no network, no install.
"""
import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# A real pin in pyproject.toml is a quoted TOML string literal, e.g.
# `"<dist>==<version>"`. Comments and prose never use this exact quoted
# syntax, so this can't pick up an unrelated `==` in a docstring or comment.
_PYPROJECT_PIN_RE = re.compile(r'"([A-Za-z0-9_.\-]+)==(\d+(?:\.\d+){1,2})"')

# Restatements in the wild aren't quoted, so this is looser -- but it still
# requires a dotted version number (at least X.Y), which is what keeps it
# from misfiring on unrelated things like an HTTP status code (`/mcp==406`)
# or a health-check name (`/mcp-health==200`) that happen to contain "mcp".
# The trailing negative lookahead stops a pre-release/build suffix (e.g.
# `1.29.0rc1`) from silently truncating to a matching prefix and reading as
# a correct restatement when the actual string on disk differs from the pin.
_RESTATEMENT_VERSION = r"\d+(?:\.\d+){1,2}(?![A-Za-z0-9.\-])"

_SCAN_SUFFIXES = {".md", ".py", ".sh", ".yml", ".yaml", ".toml", ".example", ".json"}


def _pinned_versions() -> dict[str, str]:
    text = _PYPROJECT.read_text()
    pins: dict[str, str] = {}
    for dist, version in _PYPROJECT_PIN_RE.findall(text):
        if dist in pins and pins[dist] != version:
            raise AssertionError(
                f"pyproject.toml pins {dist!r} to two different versions: "
                f"{pins[dist]!r} and {version!r} -- fix pyproject.toml itself first")
        pins[dist] = version
    assert pins, "found no `\"<dist>==<version>\"` pins in pyproject.toml"
    return pins


_UNTRACKED_DIR_NAMES = {".git", ".venv", "__pycache__", "node_modules"}


def _tracked_files() -> list[Path]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        proc = None
    if proc is not None and proc.returncode == 0:
        return [_REPO_ROOT / line for line in proc.stdout.splitlines() if line]
    # Not a git checkout (or no `git` binary at all) -- e.g. the generated
    # OSS-export tree, which is a plain `git archive` extraction with no
    # `.git` directory. Walk the tree directly instead; it only ever
    # contains what generate.py exported.
    return [
        p for p in _REPO_ROOT.rglob("*")
        if p.is_file()
        and not _UNTRACKED_DIR_NAMES.intersection(p.relative_to(_REPO_ROOT).parts)
    ]


def _is_scannable(path: Path) -> bool:
    return path.name == "Dockerfile" or path.suffix in _SCAN_SUFFIXES


def test_pinned_dependency_versions_are_consistent_everywhere():
    pins = _pinned_versions()
    pin_re = re.compile(
        r"\b(" + "|".join(re.escape(d) for d in pins) + r")=="
        r"(" + _RESTATEMENT_VERSION + r")"
    )

    mismatches = []
    for path in _tracked_files():
        if not _is_scannable(path):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(_REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for dist, version in pin_re.findall(line):
                expected = pins[dist]
                if version != expected:
                    mismatches.append(
                        f"{rel}:{lineno}: found {dist}=={version}, "
                        f"pyproject.toml pins {dist}=={expected}")

    assert not mismatches, (
        "stale dependency-pin restatement(s) found -- pyproject.toml is the "
        "single source of truth, update each site below to match it:\n  "
        + "\n  ".join(mismatches)
    )


# The same drift class, one level up: the PROJECT's own version is restated
# in ergo/__init__.py (`__version__`) and in the README's `docker pull
# ghcr.io/<org>/<repo>:<version>` quickstart. Nothing tied those to
# pyproject.toml, so cutting a release could ship a README telling readers
# to pull the tag that release just superseded.
_PROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"(\d+(?:\.\d+){1,2})"')
_DUNDER_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')
_IMAGE_TAG_RE = re.compile(r"ghcr\.io/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+:(\d+(?:\.\d+){1,2})")


def _project_version() -> str:
    m = _PROJECT_VERSION_RE.search(_PYPROJECT.read_text())
    assert m, "pyproject.toml has no top-level `version = \"X.Y.Z\"`"
    return m.group(1)


def test_dunder_version_matches_pyproject():
    text = (_REPO_ROOT / "ergo" / "__init__.py").read_text()
    m = _DUNDER_VERSION_RE.search(text)
    assert m, "ergo/__init__.py declares no __version__"
    assert m.group(1) == _project_version(), (
        f"ergo/__init__.py says __version__ = {m.group(1)!r} but pyproject.toml "
        f"says {_project_version()!r} -- pyproject.toml is the source of truth")


def test_published_image_tag_restatements_match_the_project_version():
    expected = _project_version()
    mismatches = []
    for path in _tracked_files():
        if not _is_scannable(path):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(_REPO_ROOT)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for version in _IMAGE_TAG_RE.findall(line):
                if version != expected:
                    mismatches.append(
                        f"{rel}:{lineno}: pulls image tag {version}, "
                        f"pyproject.toml declares {expected}")

    assert not mismatches, (
        "stale published-image tag(s) -- bump them with the release, or the "
        "quickstart points at a superseded image:\n  " + "\n  ".join(mismatches)
    )
