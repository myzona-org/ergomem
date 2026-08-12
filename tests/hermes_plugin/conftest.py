"""Shared fixtures for the Ergo Hermes memory-provider plugin's own suite.

This repo's test suite must never depend on Hermes being installed (CLAUDE.md:
"Hermes must not become a dependency of this repo's suite"). So instead of
`import plugins.memory.ergo` (which would require `plugins/` to be an
importable package on sys.path, and could collide with an unrelated top-level
`plugins` package), every test loads `plugins/memory/ergo/__init__.py`
directly off disk via importlib -- the exact file Hermes' own plugin loader
executes at runtime, with no import-path assumptions either way.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "memory" / "ergo"
_MODULE_NAME = "_ergo_hermes_plugin_under_test"


def _load_plugin_package():
    """Load the plugin's `__init__.py` as a real package (not a bare module).

    `submodule_search_locations` is required so the package's own relative
    imports (`from ._client import ...`) resolve -- a bare
    `spec_from_file_location(name, path)` with no search path produces a
    non-package module where relative imports raise ImportError.
    """
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    init_path = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME, init_path, submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def ergo_plugin_module():
    return _load_plugin_package()


@pytest.fixture()
def provider(ergo_plugin_module):
    return ergo_plugin_module.ErgoMemoryProvider()
