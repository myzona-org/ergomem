"""Standalone stand-in for hermes-agent's `MemoryProvider` ABC.

`_compat.py` always prefers the real `agent.memory_provider.MemoryProvider`
when this plugin runs inside an actual Hermes process. This class exists
purely so `ErgoMemoryProvider` can be constructed and exercised in THIS
repo's own test suite, which must never depend on Hermes being installed.

It is an independent implementation of the interoperability contract a
Hermes memory-provider plugin must satisfy (method names, arities, and
return-type conventions), written from scratch against the plugin's own
public developer documentation
(https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin)
rather than copied or adapted from upstream's source file -- the wording
below is original, not upstream prose. Only the handful of members
`ErgoMemoryProvider` actually implements or is called with are represented;
this is not a complete or general-purpose reimplementation of the real ABC,
and the real import always wins whenever Hermes is actually present.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class MemoryProvider(ABC):
    """Interoperability stand-in for a Hermes memory-provider base class."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier used for selection/config (e.g. `"ergo"`)."""

    @abstractmethod
    def is_available(self) -> bool:
        """Report readiness from local config alone -- never over the network."""

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """One-time setup for a session; `hermes_home` and `platform` always
        arrive in `kwargs`, everything else is best-effort context."""

    def system_prompt_block(self) -> str:
        """Static text to fold into the system prompt; empty string to skip."""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Best-effort context injected ahead of the next model call."""
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Schedule background prefetch work for a later turn."""
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Post-turn hook for persisting conversation state, if any."""
        return None

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """This provider's tools, each shaped like an OpenAI function spec."""

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch one of `get_tool_schemas()`'s tools; must return JSON text."""
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Release any held resources on process exit."""
        return None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Field descriptors a setup wizard should collect from the user."""
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret setup-wizard values under `hermes_home`."""
        return None

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Fires once a conversation session concludes, not after every turn."""
        return None

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Chance to preserve extracted context before history is summarized."""
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror hook for the harness's own built-in memory writes."""
        return None

    def backup_paths(self) -> List[str]:
        """Extra on-disk locations this provider keeps outside `hermes_home`."""
        return []
