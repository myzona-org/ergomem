"""Ergo memory-provider plugin for the Hermes Agent harness.

Design decision: wire automatic RECALL, never wire automatic CAPTURE.
`sync_turn()` (called after every completed turn) is a
no-op by default -- Ergo is a *decision* store with mandatory reasons, `/why`
deliberately can't surface a reason-less claim, and feeding every turn
through the contradiction-guarded write path would produce spurious
conflicts against real decisions. `prefetch()` (called before every turn) is
the safe, cheap, default-on path: a read-only `GET /recall`. Writes only
happen when the agent deliberately calls one of the `ergo_*` tools below.

Scoping is explicit `(org_id, project)`. `project` resolves, in order: the
`project` field saved via `save_config()`/`hermes memory setup`, else
Hermes' own `agent_workspace` kwarg, else `agent_identity`, else the literal
default `DEFAULT_PROJECT` -- documented here and in README.md so a write
never silently lands in a scope nobody chose (the exact failure class the
ergo/mcp adapter was once bitten by with git-worktree basenames).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._client import DEFAULT_API_URL, PREFETCH_TIMEOUT, ErgoClient, ErgoError
from ._compat import MemoryProvider

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "hermes"
DEFAULT_ORG_ID = "default"
DEFAULT_WHO = "hermes-agent"

# Tool names this provider exposes, in the same order as
# ergo/mcp/server.py's `_TOOL_FUNCS` (minus the admin/Pro-plan-only surface:
# audit/diagnose/export/import/import_status -- out of scope for a chat agent).
_TOOL_NAMES = (
    "ergo_remember",
    "ergo_learn",
    "ergo_recall",
    "ergo_why",
    "ergo_supersede",
    "ergo_retract",
    "ergo_ingest",
    "ergo_active",
    "ergo_history",
    "ergo_health",
)

_CONFIG_SUBDIR = "ergo"
_CONFIG_FILENAME = "config.json"

# Bounds the opt-in turn-capture path (see sync_turn()): a single persistent
# daemon worker drains this queue, so a slow/unresponsive engine backs up a
# bounded queue instead of spawning one thread per turn indefinitely.
_CAPTURE_QUEUE_MAXSIZE = 100
_CAPTURE_WORKER_IDLE_TIMEOUT = 5.0

# shutdown() waits at most this long for the capture worker to finish its
# current/queued jobs before closing the HTTP client out from under it.
# Bounded, not indefinite: an unreachable engine must not hang process exit.
_SHUTDOWN_DRAIN_TIMEOUT = 5.0


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ErgoMemoryProvider(MemoryProvider):
    """Read-heavy Ergo integration: automatic recall, deliberate-only writes."""

    def __init__(self) -> None:
        self._client: Optional[ErgoClient] = None
        self._project: str = DEFAULT_PROJECT
        self._org_id: str = DEFAULT_ORG_ID
        self._who: str = DEFAULT_WHO
        self._session_id: str = ""
        self._agent_context: Optional[str] = None
        self._capture_turns: bool = False
        self._capture_queue: "queue.Queue[Any]" = queue.Queue(maxsize=_CAPTURE_QUEUE_MAXSIZE)
        self._capture_worker: Optional[threading.Thread] = None
        self._capture_worker_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "ergo"

    # -- config file (storage lives under hermes_home, never ~/.hermes) ---

    def _config_path(self, hermes_home: str) -> Path:
        return Path(hermes_home) / _CONFIG_SUBDIR / _CONFIG_FILENAME

    def _read_config_file(self, hermes_home: Optional[str]) -> Dict[str, Any]:
        if not hermes_home:
            return {}
        path = self._config_path(hermes_home)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret fields (api_url, project) under `hermes_home`.

        `api_token` is `secret: True` in `get_config_schema()`, so Hermes
        writes it straight to `.env` via its `env_var` and never passes it
        here -- but it's stripped defensively too, in case a caller does.

        A falsy `hermes_home` (empty string, or `None` despite the `str`
        annotation) refuses to write rather than resolving `Path("")`/
        `Path(None)` -- an empty string would otherwise silently create
        `./ergo/config.json` relative to whatever directory the caller
        happened to be running in, a location `_read_config_file()` can
        never find again (it treats a falsy `hermes_home` as "no config"
        up front, the same asymmetry this guard closes). This is treated as
        a caller bug worth logging, not a hard error: `save_config()` is
        invoked by Hermes' own setup flow, and raising here would surface
        as an unhandled exception in that flow rather than the config
        simply, visibly, not being written.
        """
        if not hermes_home:
            logger.warning("ergo memory provider: save_config() called with no hermes_home, not writing anything")
            return
        path = self._config_path(hermes_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, Any] = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, ValueError):
                existing = {}
        existing.update({k: v for k, v in values.items() if k != "api_token"})
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    # -- availability -------------------------------------------------------

    def _resolve_api_url(self, file_cfg: Optional[Dict[str, Any]] = None) -> str:
        """Resolve the effective API URL with ONE precedence, used by both
        `is_available()` and `initialize()` so they can never disagree about
        what "explicitly disabled" means.

        An `ERGO_API_URL` set in the environment -- even to an empty string
        -- always wins, blank included: that is the documented kill switch,
        and it must not be able to fall through to a saved `config.json`
        value (`is_available()` has no `hermes_home` to read that file from,
        so it can only ever see the env var; if `initialize()` used a
        different, more permissive precedence, a blanked env var could look
        "unavailable" while still silently activating a client).
        """
        env = os.environ.get("ERGO_API_URL")
        if env is not None:
            return env.strip()
        if file_cfg and file_cfg.get("api_url"):
            return str(file_cfg["api_url"]).strip()
        return DEFAULT_API_URL

    def is_available(self) -> bool:
        """Config presence only -- no network call.

        Ergo has a legitimate zero-config default (an unauthenticated local
        engine on 127.0.0.1:8788), so this only ever fails closed when a
        user has explicitly set `ERGO_API_URL=` (blank) as a kill switch.
        """
        try:
            return self._resolve_api_url() != ""
        except Exception:
            return False

    # -- lifecycle ------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        # One instance per (agent, session) is the documented model, not
        # assumed: Hermes' loader re-runs register(ctx) -- constructing a
        # fresh provider -- on every load_memory_provider() call (no
        # instance caching), and upstream's own hindsight provider states
        # "the plugin loader creates a new provider per AIAgent, and the
        # gateway creates one AIAgent per concurrent chat session." If that
        # ever stops holding and one instance IS reused across sessions,
        # the instance-level _project/_org_id/_who below would silently
        # start reflecting the wrong session's scope -- this makes that
        # loud instead.
        if self._session_id and session_id != self._session_id:
            logger.warning(
                "ergo memory provider: initialize() called again with a different "
                "session_id (%r -> %r) on the same instance -- instance-level scope "
                "state may now reflect the wrong session",
                self._session_id, session_id,
            )
        hermes_home = kwargs.get("hermes_home")
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context")

        file_cfg = self._read_config_file(hermes_home)

        api_url = self._resolve_api_url(file_cfg)
        if not api_url:
            # Kill switch honoured even if a harness calls initialize()
            # without consulting is_available() first (belt and suspenders --
            # the ABC contract says it should check first).
            logger.warning("ergo memory provider: ERGO_API_URL explicitly blanked, not activating")
            if self._client is not None:
                self._client.close()
            self._client = None
            return

        api_token = os.environ.get("ERGO_API_TOKEN") or None

        self._project = (
            file_cfg.get("project")
            or kwargs.get("agent_workspace")
            or kwargs.get("agent_identity")
            or DEFAULT_PROJECT
        )
        self._org_id = os.environ.get("ERGO_ORG_ID") or file_cfg.get("org_id") or DEFAULT_ORG_ID
        self._who = os.environ.get("ERGO_WHO") or file_cfg.get("who") or DEFAULT_WHO
        self._capture_turns = _env_bool("ERGO_HERMES_CAPTURE_TURNS", default=False)

        if self._client is not None:
            # Resource leak (PR review): a double-init used to replace
            # self._client without closing the old one, leaking its
            # connection pool -- the same double-init the session-id-change
            # warning above already anticipates.
            self._client.close()
        self._client = ErgoClient(base_url=api_url, token=api_token)
        logger.info(
            "ergo memory provider initialized: project=%s org_id=%s capture_turns=%s",
            self._project, self._org_id, self._capture_turns,
        )

    def system_prompt_block(self) -> str:
        return (
            f"Ergo decision memory is active (project={self._project}). Relevant "
            "past decisions are recalled automatically each turn. Use the ergo_* "
            "tools to deliberately record, supersede, or retract a decision -- "
            "nothing is written automatically."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._client is None:
            return ""
        try:
            resp = self._client.recall({
                "org_id": self._org_id,
                "project": self._project,
                "q": query,
                "limit": 5,
            }, timeout=PREFETCH_TIMEOUT)
        except Exception:
            logger.debug("ergo prefetch failed", exc_info=True)
            return ""
        results = resp.get("results") or []
        if not results:
            return ""
        lines = []
        for r in results:
            statement = r.get("statement", "")
            reason = r.get("reason")
            lines.append(f"- {statement} ({reason})" if reason else f"- {statement}")
        return "Relevant Ergo memory:\n" + "\n".join(lines)

    def _drain_capture_queue(self) -> None:
        """Drain loop for the single opt-in-capture worker.

        PR review finding: on a plain `queue.Empty` this used to `return`
        unconditionally -- but a job can be `put_nowait()`'d in the narrow
        window between this thread's `get()` timing out and it actually
        exiting, while `_ensure_capture_worker()` still sees this thread as
        `is_alive()` and declines to spawn a replacement. That job then has
        no drainer until the next `sync_turn()` call, and is lost entirely
        if the session ends first. Fixed by making "decide to exit" and
        "decide to spawn" mutually exclusive: both now hold
        `_capture_worker_lock`, and the exit path re-checks the queue and
        clears `_capture_worker` itself (rather than leaving a stale
        reference for `_ensure_capture_worker()` to find) before returning.
        """
        while True:
            try:
                job = self._capture_queue.get(timeout=_CAPTURE_WORKER_IDLE_TIMEOUT)
            except queue.Empty:
                with self._capture_worker_lock:
                    if not self._capture_queue.empty():
                        continue  # a job raced in between the timeout and this check
                    self._capture_worker = None
                    return
            else:
                try:
                    job()
                except Exception:
                    logger.debug("ergo opt-in turn capture failed", exc_info=True)

    def _ensure_capture_worker(self) -> None:
        with self._capture_worker_lock:
            if self._capture_worker is None or not self._capture_worker.is_alive():
                self._capture_worker = threading.Thread(
                    target=self._drain_capture_queue, daemon=True, name="ergo-sync-turn"
                )
                self._capture_worker.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """No-op by default -- see the module docstring.

        Opt-in via `ERGO_HERMES_CAPTURE_TURNS=1`: posts the turn to
        `POST /ingest` (the gate-free fact path -- never `/remember`) via a
        single persistent daemon worker draining a bounded queue, so this
        never blocks the turn and a slow/unresponsive engine backs up a
        capped queue instead of spawning one thread per turn indefinitely
        (PR review finding). Skipped for non-primary agent contexts
        (cron/subagent/flush), matching the ABC's own guidance that a
        provider should skip writes there.
        """
        if not self._capture_turns or self._client is None:
            return
        if self._agent_context not in (None, "primary"):
            return

        client = self._client
        org_id, project, who = self._org_id, self._project, self._who
        sid = session_id or self._session_id

        def _do_ingest() -> None:
            text = f"User: {user_content}\n\nAssistant: {assistant_content}"
            client.ingest({
                "org_id": org_id,
                "project": project,
                "who": who,
                "text": text,
                "source": f"hermes-turn:{sid}" if sid else "hermes-turn",
            })

        try:
            self._capture_queue.put_nowait(_do_ingest)
        except queue.Full:
            logger.debug("ergo opt-in turn capture dropped -- queue full (engine too slow)")
            return
        self._ensure_capture_worker()

    def shutdown(self) -> None:
        """Flush the capture worker before closing the client.

        PR review finding: `sync_turn()` enqueues and returns before the
        HTTP call runs, so on a normal, prompt session end the LAST turn's
        opt-in capture job is typically still queued or in-flight. Closing
        the client immediately (the old behavior) meant that job's
        `client.ingest(...)` call then ran against an already-closed
        `httpx.Client` -- an exception silently swallowed by
        `_drain_capture_queue`'s own `except Exception: logger.debug(...)`,
        so the final turn was systematically dropped whenever capture was
        enabled. Joining the worker first (bounded, so an unreachable engine
        can't hang process exit) closes that window; a timed-out join logs
        what was left queued rather than silently dropping it.
        """
        worker = self._capture_worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=_SHUTDOWN_DRAIN_TIMEOUT)
            if worker.is_alive():
                logger.warning(
                    "ergo memory provider: shutdown timed out waiting for the capture "
                    "worker to drain -- %d queued capture(s) dropped",
                    self._capture_queue.qsize(),
                )
        if self._client is not None:
            self._client.close()

    # -- tools ------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "ergo_remember",
                "description": (
                    "Record a contradiction-guarded decision/constraint/convention in Ergo. "
                    "Always include a reason -- reason-less claims can never win an ergo_why "
                    "query. If the engine detects a contradiction it returns "
                    "result='conflict' with the conflicting claim instead of storing -- "
                    "resolve it by telling the user, calling ergo_supersede, or retrying with "
                    "force_exception."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string", "description": "The claim text."},
                        "reason": {"type": "string", "description": "Why -- surfaced via ergo_why."},
                        "force_exception": {"type": "string", "description": "Store despite a conflict, with this justification."},
                    },
                    "required": ["statement"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_learn",
                "description": (
                    "Record a belief formed FROM a source (file:line, URL, doc id), "
                    "contradiction-guarded with provenance folded into the stored reason. "
                    "Same conflict handling as ergo_remember."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "statement": {"type": "string"},
                        "source": {"type": "string", "description": "Where this belief came from."},
                        "reason": {"type": "string"},
                        "force_exception": {"type": "string"},
                    },
                    "required": ["statement", "source"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_recall",
                "description": "Hybrid semantic search over active Ergo claims in scope. Prefer this for an ad hoc lookup; automatic prefetch already runs every turn.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "description": "Clamped to [1, 50] by the engine."},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_why",
                "description": "Best REASONED belief for a query plus its supersede history. A reason-less fact can never win this query.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_supersede",
                "description": "Replace an existing active claim with a revised one (\"I changed my mind\"). Get existing_id from ergo_recall/ergo_why/ergo_active.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "existing_id": {"type": "string"},
                        "statement": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["existing_id", "statement"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_retract",
                "description": "Retract a wrong-at-birth claim (typo, test junk, mis-scoped write) -- never for a decision that genuinely changed, use ergo_supersede for that.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "claim_id": {"type": "string"},
                        "reason": {"type": "string", "description": "Required -- removal is audit-loud."},
                    },
                    "required": ["claim_id", "reason"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_ingest",
                "description": "Bulk-ingest reference content (a doc, a README) for later recall. Skips the contradiction gate -- use ergo_remember instead for decisions you want checked.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "source": {"type": "string"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "ergo_active",
                "description": "Enumerate active claims in scope.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "ergo_history",
                "description": "Full claim history for scope -- active + superseded + retracted.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "ergo_health",
                "description": "Liveness probe for the Ergo engine.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]

    def _err(self, exc: ErgoError) -> Dict[str, Any]:
        return {"result": "error", "status_code": exc.status_code, "error": exc.body}

    def _call(self, method: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        assert self._client is not None
        try:
            fn = getattr(self._client, method)
            return fn(payload) if payload is not None else fn()
        except ErgoError as exc:
            return self._err(exc)
        except Exception as exc:  # transport failures (connect/timeout/DNS)
            return {"result": "error", "error": str(exc), "url": self._client.base_url}

    def _write_scoped(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Merge a tool call's own fields with the provider's resolved scope.

        `org_id`/`project`/`who` are FORCED, not defaulted: none of them are
        declared in `get_tool_schemas()`, but nothing stops a model from
        emitting them anyway (the schemas don't set `additionalProperties:
        false`). A `setdefault()` here would let such a value silently win
        over the configured scope -- exactly the "write lands somewhere
        nobody chose" failure this plugin's scoping is meant to prevent.
        """
        payload = dict(args)
        payload["org_id"] = self._org_id
        payload["project"] = self._project
        payload["who"] = self._who
        return payload

    def _read_scoped(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Same forced-scope guarantee as `_write_scoped`, for read calls
        (which have no `who` field)."""
        payload = dict(args)
        payload["org_id"] = self._org_id
        payload["project"] = self._project
        if "query" in payload:
            payload["q"] = payload.pop("query")
        return payload

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        args = args or {}
        if tool_name not in _TOOL_NAMES:
            raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")
        if self._client is None:
            return json.dumps({"result": "error", "error": "ergo provider not initialized"})

        if tool_name in ("ergo_remember", "ergo_learn", "ergo_supersede", "ergo_retract", "ergo_ingest"):
            payload = self._write_scoped(args)
            method = tool_name[len("ergo_"):]
            result = self._call(method, payload)
        elif tool_name in ("ergo_recall", "ergo_why", "ergo_active", "ergo_history"):
            payload = self._read_scoped(args)
            method = tool_name[len("ergo_"):]
            result = self._call(method, payload)
        else:  # ergo_health
            result = self._call("health", None)

        return json.dumps(result)

    # -- setup wizard config --------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_url",
                "description": "Ergo API base URL (self-hosted engine).",
                "default": DEFAULT_API_URL,
                "env_var": "ERGO_API_URL",
            },
            {
                "key": "api_token",
                "description": "Ergo API bearer token. Leave blank for an open/dev instance.",
                "secret": True,
                "required": False,
                "env_var": "ERGO_API_TOKEN",
            },
            {
                "key": "project",
                "description": (
                    "Ergo project scope for this agent. Defaults to Hermes' own "
                    f"agent_workspace/agent_identity if set, else '{DEFAULT_PROJECT}'."
                ),
                "default": DEFAULT_PROJECT,
            },
        ]


def register(ctx) -> None:
    """Called by the Hermes memory plugin discovery system."""
    ctx.register_memory_provider(ErgoMemoryProvider())
