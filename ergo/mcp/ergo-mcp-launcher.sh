#!/bin/bash
# Ergo MCP launcher — runs the stdio MCP adapter using the repo's venv Python so
# its pinned deps (mcp==1.29.0, httpx==0.28.1) don't depend on whatever Python the
# harness happens to use.
#
# Deliberately does NOT `cd` into the repo: the server resolves its default
# `project` scope from the *current* working directory (git-repo basename,
# worktree-aware — see default_project() in server.py), so it must inherit
# the cwd of whatever project Claude Code launched it in.
# `ergo` is installed editable in the venv, but we also export PYTHONPATH as a
# belt-and-suspenders so `-m ergo.mcp.server` resolves regardless.
#
# Secrets (ERGO_API_TOKEN) come from the harness's mcpServers.<name>.env or the
# surrounding shell env — never from this script.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../ergo/ergo/mcp
REPO="$(cd "$DIR/../.." && pwd)"                        # repo root

export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
exec "$REPO/.venv/bin/python" -m ergo.mcp.server
