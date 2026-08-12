"""Import shim for hermes-agent's `MemoryProvider` ABC.

Real Hermes runtime always has `agent.memory_provider` importable -- that's
the real base class this plugin subclasses in production. This repo's test
suite runs without Hermes installed, so the fallback is an independently
written stand-in for the same interoperability contract (see
`_standalone_memory_provider.py`), used only so `ErgoMemoryProvider` can be
imported and unit-tested standalone.
"""

from __future__ import annotations

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # pragma: no cover - exercised whenever Hermes isn't installed
    from ._standalone_memory_provider import MemoryProvider

__all__ = ["MemoryProvider"]
