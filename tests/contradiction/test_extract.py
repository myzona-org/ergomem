"""Tests for ergo.contradiction.extract — prose -> candidate modal statements.

Guards the regressions the module docstring records:
  - inline-code *content* is preserved (only the backticks are stripped). Deleting
    it produced garbage claims like "Cannot be combined with ." (the 2026-06-26 FP),
    because the inline code IS usually the subject ("`--no-cache` cannot be ...").
  - fenced code blocks are removed wholesale (they are full of false should/may/must).
  - only sentences with a modal verb, between 25 and 350 chars, survive.
All pure functions — no network, no store.
"""
from __future__ import annotations

from pathlib import Path

from ergo.contradiction.extract import (
    clean_markdown, split_sentences, extract_from_text,
    extract_from_files, discover_md, Candidate,
    mask_inline_code, unmask_inline_code,
)


# --------------------------------------------------------------------------
# clean_markdown
# --------------------------------------------------------------------------
def test_inline_code_content_is_preserved_backticks_dropped():
    # THE 2026-06-26 regression: keep the identifier, drop only the backticks.
    out = clean_markdown("The `--no-cache` flag must not be combined with `--pull`.")
    assert "`" not in out
    assert "--no-cache" in out
    assert "--pull" in out


def test_fenced_code_block_is_stripped_wholesale():
    out = clean_markdown("before text here\n```\nyou should never do this\n```\nafter text here")
    assert "should never" not in out
    assert "before text here" in out
    assert "after text here" in out


def test_markdown_link_keeps_text_drops_url():
    out = clean_markdown("See [the docs](http://example.com/x) for more.")
    assert "the docs" in out
    assert "http" not in out and "example.com" not in out


def test_image_is_removed_including_alt_and_path():
    out = clean_markdown("![alt text](pic.png) stays")
    assert "pic.png" not in out
    assert "alt text" not in out
    assert "stays" in out


def test_headings_and_bullets_markers_stripped_text_kept():
    out = clean_markdown("## Title\n\n- a bullet item")
    assert "#" not in out
    assert "Title" in out
    assert "a bullet item" in out
    # the bullet marker itself is gone
    assert "- a bullet" not in out


def test_yaml_frontmatter_is_removed():
    out = clean_markdown("---\ntitle: x\ntags: y\n---\nBody must stay here.")
    assert "title:" not in out
    assert "Body must stay here." in out


# --------------------------------------------------------------------------
# mask_inline_code / unmask_inline_code (normalizer-facing code masking)
# --------------------------------------------------------------------------
def test_mask_replaces_code_spans_with_digit_only_sentinels():
    masked, spans = mask_inline_code("The `must-revalidate` directive must be set.")
    # the modal-looking identifier is gone from the scannable text...
    assert "must-revalidate" not in masked
    # ...but the REAL modal verb survives for the scanner.
    assert "must be set" in masked
    assert spans == ["must-revalidate"]


def test_unmask_restores_original_code_content():
    masked, spans = mask_inline_code("Bind to `port 8443` always.")
    assert unmask_inline_code(masked, spans).strip() == "Bind to  port 8443  always."


def test_mask_roundtrip_preserves_multiple_spans():
    text = "Use `--no-cache` and `--pull` together."
    masked, spans = mask_inline_code(text)
    assert spans == ["--no-cache", "--pull"]
    # neither identifier is visible to a keyword scan while masked
    assert "no-cache" not in masked and "pull" not in masked
    assert unmask_inline_code(masked, spans).replace("  ", " ").strip() == \
        "Use --no-cache and --pull together."


# --------------------------------------------------------------------------
# split_sentences
# --------------------------------------------------------------------------
def test_splits_on_period_before_capital():
    sents = split_sentences("Alpha must run here. Beta should stop there.")
    assert sents == ["Alpha must run here.", "Beta should stop there."]


def test_abbreviation_period_does_not_split_dr():
    sents = split_sentences("Use Dr. Smith now. It must pass review.")
    assert sents == ["Use Dr. Smith now.", "It must pass review."]


def test_abbreviation_period_does_not_split_etc():
    # "etc." is protected, so the only boundary is the trailing one -> 1 sentence.
    sents = split_sentences("Foo bar etc. baz must go now today.")
    assert sents == ["Foo bar etc. baz must go now today."]


def test_exclaim_and_question_are_boundaries():
    sents = split_sentences("Stop now! You must wait. Why though?")
    assert sents == ["Stop now!", "You must wait.", "Why though?"]


# --------------------------------------------------------------------------
# extract_from_text
# --------------------------------------------------------------------------
def test_only_modal_sentences_are_kept():
    text = ("The deploy must always use a blue canary here.\n\n"
            "This is plain prose with no modal verb at all.")
    cands = extract_from_text(text)
    assert len(cands) == 1
    assert "must always use a blue canary" in cands[0].text


def test_inline_code_subject_survives_end_to_end():
    text = "The `--no-cache` flag must not be combined with the pull flag here."
    cands = extract_from_text(text)
    assert len(cands) == 1
    assert "`" not in cands[0].text
    assert "--no-cache" in cands[0].text
    assert "must not" in cands[0].text


def test_fenced_code_yields_no_candidates():
    text = "```\nyou should never ever do this thing right here friend\n```"
    assert extract_from_text(text) == []


def test_sentence_shorter_than_25_chars_is_dropped():
    assert extract_from_text("Must go.") == []


def test_sentence_longer_than_350_chars_is_dropped():
    long_obj = "y" * 400
    assert extract_from_text(f"The setting must be {long_obj}") == []


def test_candidate_carries_source_and_line_number():
    text = ("Intro line that is just prose here.\n\n"
            "The deploy must always use a blue canary here.\n\n"
            "More prose without any modal words.")
    cands = extract_from_text(text, source="doc.md")
    assert len(cands) == 1
    c = cands[0]
    assert isinstance(c, Candidate)
    assert c.source == "doc.md"
    assert c.line == 3  # the modal sentence sits on the third source line


def test_default_source_is_inline_marker():
    cands = extract_from_text("The job must retry on a transient failure here.")
    assert cands and cands[0].source == "<inline>"


# --------------------------------------------------------------------------
# discover_md + extract_from_files (filesystem front-end)
# --------------------------------------------------------------------------
def test_discover_md_finds_markdown_under_a_directory(tmp_path):
    (tmp_path / "a.md").write_text("The service must never log secrets to disk.")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.md").write_text("Backups must run daily without fail.")
    (tmp_path / "ignore.txt").write_text("not markdown")
    found = {p.name for p in discover_md([tmp_path])}
    assert found == {"a.md", "b.md"}


def test_extract_from_files_tags_candidates_with_file_path(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("The service must never log secrets to disk anywhere here.")
    cands = extract_from_files([p])
    assert len(cands) == 1
    assert cands[0].source == str(p)
    assert "must never log secrets" in cands[0].text


def test_extract_from_files_skips_unreadable_paths(tmp_path):
    missing = tmp_path / "does_not_exist.md"
    # read failure is swallowed (the loop continues) -> no crash, no candidates.
    assert extract_from_files([missing]) == []
