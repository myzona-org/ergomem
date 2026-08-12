"""Sanity checks on plugins/memory/ergo/plugin.yaml against the documented
Hermes manifest format (see the developer-guide/memory-provider-plugin doc
and hermes_cli/plugins_cmd.py's `_read_manifest`)."""

from __future__ import annotations

from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "ergo"


def test_plugin_yaml_has_required_fields():
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "ergo"
    assert "version" in manifest
    assert "description" in manifest and manifest["description"]


def test_plugin_yaml_hooks_list_is_empty_by_design():
    # Design decision: no automatic-capture hooks
    # (on_session_end / on_pre_compress / on_memory_write / ...) are wired.
    # Writes only ever happen through a deliberate tool call.
    manifest = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest.get("hooks", []) == []


def test_readme_documents_recall_automatic_capture_not():
    readme = (PLUGIN_DIR / "README.md").read_text()
    assert "automatic" in readme.lower()
    assert "ERGO_HERMES_CAPTURE_TURNS" in readme
