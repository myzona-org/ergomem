"""Tests for plugins/memory/ergo/install.sh — the Hermes install helper.

The script must:
- Probe the Ergo engine's /health and exit nonzero with a helpful message
  when it's unreachable.
- Print a ready-to-paste memory.provider YAML block and config.json block,
  with real values substituted, when the engine is reachable.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml

_INSTALL_SH = (
    pathlib.Path(__file__).resolve().parents[2] / "plugins" / "memory" / "ergo" / "install.sh"
)


def _run(overrides: dict) -> subprocess.CompletedProcess:
    """Run install.sh with a real PATH (bash's builtin fallback PATH happens
    to include /usr/bin on both macOS and this repo's CI runner, but that's
    an unstated implementation detail, not a guarantee -- inherit the real
    environment instead of relying on it)."""
    env = {k: v for k, v in os.environ.items() if k not in ("ERGO_PROJECT", "ERGO_API_URL")}
    env.update(overrides)
    return subprocess.run(
        ["bash", str(_INSTALL_SH)], env=env, capture_output=True, text=True, timeout=10
    )


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib method name)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, *args):  # silence test output
        pass


def test_install_script_exists_and_is_executable():
    assert _INSTALL_SH.is_file()
    assert _INSTALL_SH.stat().st_mode & 0o111


def test_install_script_exits_nonzero_with_helpful_message_when_project_unset():
    result = _run({})
    assert result.returncode != 0
    assert "ERGO_PROJECT is not set" in result.stderr


def test_install_script_exits_nonzero_with_helpful_message_when_project_would_break_json():
    result = _run({"ERGO_PROJECT": 'has"quote'})
    assert result.returncode != 0
    assert "quote, backslash, or control character" in result.stderr


def test_install_script_exits_nonzero_with_helpful_message_when_api_url_would_break_json():
    result = _run({"ERGO_PROJECT": "test-project", "ERGO_API_URL": 'http://127.0.0.1:8788"evil'})
    assert result.returncode != 0
    assert "ERGO_API_URL contains a quote, backslash, or control character" in result.stderr


def test_install_script_exits_nonzero_with_helpful_message_when_unreachable():
    result = _run({"ERGO_PROJECT": "test-project", "ERGO_API_URL": "http://127.0.0.1:1"})
    assert result.returncode != 0
    assert "not reachable" in result.stderr.lower()


def test_install_script_prints_parseable_yaml_and_json_when_reachable():
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        result = _run({"ERGO_API_URL": f"http://127.0.0.1:{port}", "ERGO_PROJECT": "test-project"})
        assert result.returncode == 0

        yaml_block = result.stdout.split("memory:", 1)[1].split("\n\n", 1)[0]
        parsed_yaml = yaml.safe_load("memory:" + yaml_block)
        assert parsed_yaml == {"memory": {"provider": "ergo"}}

        json_block = result.stdout[result.stdout.index("{"):]
        parsed_json = json.loads(json_block)
        assert parsed_json == {"api_url": f"http://127.0.0.1:{port}", "project": "test-project"}
    finally:
        server.shutdown()
        thread.join(timeout=5)
