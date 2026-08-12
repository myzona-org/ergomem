"""Ergo MCP adapter.

A *thin* stdio MCP server that exposes the Ergo HTTP API
(`http://127.0.0.1:8788`) as native MCP tools. It is a pure HTTP client: it
never imports `ContradictionStore`, opens `ergo.db`, or touches the DB. The
HTTP API stays the single source of truth; MCP is just another transport.

Run with: ``python -m ergo.mcp.server`` (stdio transport).
"""

__all__ = ["server"]
