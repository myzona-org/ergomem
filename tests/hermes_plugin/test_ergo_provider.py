"""Unit tests for the Ergo Hermes memory-provider plugin.

HTTP fully mocked via httpx.MockTransport (same harness style as
tests/mcp_adapter/test_server.py). No network, no DB, no Hermes install.

Covers: ABC-contract satisfaction, is_available() making no network call,
prefetch()->/recall with graceful failure, sync_turn() being a no-op by
default (never writing a claim) and non-blocking when the opt-in capture
flag is set, handle_tool_call() routing + 409-conflict surfacing, storage
honouring `hermes_home` (never `~/.hermes`), and the config schema marking
the API token secret.
"""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest


# --------------------------------------------------------------------------- #
# Mock transport harness (mirrors tests/mcp_adapter/test_server.py)
# --------------------------------------------------------------------------- #


class Recorder:
    def __init__(self, routes):
        self.routes = routes  # {(method, path): (status, body)}
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        key = (request.method, request.url.path)
        status, body = self.routes.get(key, (404, {"error": "no route"}))
        return httpx.Response(status, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> dict:
        return json.loads(self.last.content)

    def paths_called(self) -> list[str]:
        with self._lock:
            return [r.url.path for r in self.requests]


def _wire(ergo_plugin_module, provider, routes, *, project="unit-proj", org_id="acme"):
    """Bypass initialize() network-free construction and hand the provider a
    mocked ErgoClient directly -- the same white-box seam
    tests/mcp_adapter/test_server.py uses (`server.set_client`), adapted for
    a per-instance provider instead of a module-level singleton."""
    rec = Recorder(routes)
    transport = httpx.MockTransport(rec.handler)
    client = ergo_plugin_module.ErgoClient(
        base_url="http://ergo.test", token=None, transport=transport
    )
    provider._client = client
    provider._project = project
    provider._org_id = org_id
    provider._who = "hermes-agent"
    return rec


# --------------------------------------------------------------------------- #
# ABC contract
# --------------------------------------------------------------------------- #


def test_provider_satisfies_memory_provider_abc(ergo_plugin_module, provider):
    assert isinstance(provider, ergo_plugin_module.MemoryProvider)
    assert provider.name == "ergo"


def test_register_registers_an_ergo_provider_instance(ergo_plugin_module):
    class FakeCtx:
        def __init__(self):
            self.registered = []

        def register_memory_provider(self, instance):
            self.registered.append(instance)

    ctx = FakeCtx()
    ergo_plugin_module.register(ctx)
    assert len(ctx.registered) == 1
    assert isinstance(ctx.registered[0], ergo_plugin_module.ErgoMemoryProvider)


# --------------------------------------------------------------------------- #
# is_available(): config presence only, NO network call
# --------------------------------------------------------------------------- #


def test_is_available_true_by_default_with_no_config(ergo_plugin_module, provider, monkeypatch):
    monkeypatch.delenv("ERGO_API_URL", raising=False)
    assert provider.is_available() is True


def test_is_available_false_when_api_url_explicitly_blanked(ergo_plugin_module, provider, monkeypatch):
    monkeypatch.setenv("ERGO_API_URL", "")
    assert provider.is_available() is False


def test_initialize_honours_the_same_kill_switch_as_is_available(ergo_plugin_module, provider, tmp_path, monkeypatch):
    """Regression (PR review): is_available() only ever sees the env var (it
    has no hermes_home to read a config file from), but initialize() used to
    resolve the URL with a MORE permissive precedence -- a blanked
    ERGO_API_URL could fall through to a previously-saved config.json value
    and silently activate a client anyway, disagreeing with is_available()."""
    hermes_home = tmp_path / "hermes-home"
    provider.save_config({"api_url": "http://saved-from-config:8788"}, str(hermes_home))
    monkeypatch.setenv("ERGO_API_URL", "")

    assert provider.is_available() is False
    provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
    assert provider._client is None


def test_is_available_makes_no_network_call(ergo_plugin_module, provider, monkeypatch):
    """Regression (PR review, two rounds):

    Round 1: the original version of this test asserted only "is_available()
    must not raise" -- but is_available() wraps its whole body in `except
    Exception: return False`, so a raising httpx.Client construction would
    be silently swallowed into a (still non-raising) `False`. The test
    passed identically whether or not the code under test actually built a
    client.

    Round 2: patching `httpx.Client` construction only catches a FRESH
    client being built. It says nothing about a scenario where
    `provider._client` is already a live client (e.g. left over from a
    prior initialize()) and is_available() sends a request through THAT
    existing instance -- no construction happens, so a construction-only
    patch would report `calls == []` while a real request went out. Patch
    `httpx.Client.send` instead: every outbound request from every client
    instance, freshly built or pre-existing, funnels through it (`.get()`/
    `.request()` both call `.send()` under the hood), so this closes both
    paths with one assertion.
    """
    calls = []

    def _tracking_send(self, request, *args, **kwargs):
        calls.append(request)
        raise AssertionError("is_available() must not send an HTTP request")

    monkeypatch.setattr(ergo_plugin_module._client.httpx.Client, "send", _tracking_send)
    result = provider.is_available()

    assert calls == [], "an HTTP request was sent -- is_available() made a network call"
    assert result is True


# --------------------------------------------------------------------------- #
# prefetch(): maps to GET /recall, fails soft
# --------------------------------------------------------------------------- #


def test_prefetch_calls_recall_with_scope(ergo_plugin_module, provider):
    rec = _wire(ergo_plugin_module, provider, {
        ("GET", "/recall"): (200, {"results": [], "scope": {"project": "unit-proj"}}),
    })
    provider.prefetch("what did we decide about deploys?")
    assert rec.last.method == "GET"
    assert rec.last.url.path == "/recall"
    assert dict(rec.last.url.params)["org_id"] == "acme"
    assert dict(rec.last.url.params)["project"] == "unit-proj"
    assert dict(rec.last.url.params)["q"] == "what did we decide about deploys?"


def test_prefetch_uses_the_short_prefetch_timeout(ergo_plugin_module, provider):
    rec = _wire(ergo_plugin_module, provider, {
        ("GET", "/recall"): (200, {"results": [], "scope": {"project": "unit-proj"}}),
    })
    provider.prefetch("anything")
    timeout = rec.last.extensions["timeout"]
    assert timeout["connect"] == ergo_plugin_module._client.PREFETCH_TIMEOUT.connect
    assert timeout["read"] == ergo_plugin_module._client.PREFETCH_TIMEOUT.read


def test_ergo_recall_tool_call_does_not_use_the_short_prefetch_timeout(ergo_plugin_module, provider):
    """Regression (PR review): ErgoClient.recall() used to hardcode the 4s
    prefetch deadline for every caller, including the deliberate ergo_recall
    tool -- which then inherited a timeout meant only for the automatic
    per-turn path."""
    rec = _wire(ergo_plugin_module, provider, {
        ("GET", "/recall"): (200, {"results": []}),
    })
    provider.handle_tool_call("ergo_recall", {"query": "anything"})
    timeout = rec.last.extensions["timeout"]
    assert timeout["connect"] == ergo_plugin_module._client.DEFAULT_TIMEOUT.connect
    assert timeout["read"] == ergo_plugin_module._client.DEFAULT_TIMEOUT.read


def test_prefetch_formats_results_as_text(ergo_plugin_module, provider):
    _wire(ergo_plugin_module, provider, {
        ("GET", "/recall"): (200, {
            "results": [{"id": "c1", "statement": "Deploy on Tuesdays", "reason": "on-call rotation"}],
            "scope": {"project": "unit-proj"},
        }),
    })
    text = provider.prefetch("deploy schedule")
    assert "Deploy on Tuesdays" in text
    assert "on-call rotation" in text


def test_prefetch_returns_empty_string_when_no_results(ergo_plugin_module, provider):
    _wire(ergo_plugin_module, provider, {
        ("GET", "/recall"): (200, {"results": [], "scope": {"project": "unit-proj"}}),
    })
    assert provider.prefetch("anything") == ""


def test_prefetch_returns_empty_string_on_http_error_status(ergo_plugin_module, provider):
    _wire(ergo_plugin_module, provider, {
        ("GET", "/recall"): (500, {"error": "boom"}),
    })
    assert provider.prefetch("anything") == ""


def test_prefetch_returns_empty_string_on_transport_failure(ergo_plugin_module, provider):
    def _raise_transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(_raise_transport_error)
    provider._client = ergo_plugin_module.ErgoClient(
        base_url="http://ergo.test", token=None, transport=transport
    )
    provider._project = "unit-proj"
    provider._org_id = "acme"
    assert provider.prefetch("anything") == ""


def test_prefetch_returns_empty_string_when_never_initialized(ergo_plugin_module, provider):
    assert provider._client is None
    assert provider.prefetch("anything") == ""


# --------------------------------------------------------------------------- #
# sync_turn(): no-op by default, opt-in capture is non-blocking and never
# hits the guarded /remember path
# --------------------------------------------------------------------------- #


def test_sync_turn_default_never_makes_a_request(ergo_plugin_module, provider):
    rec = _wire(ergo_plugin_module, provider, {
        ("POST", "/ingest"): (200, {"result": "ingested", "chunks": 1, "stored": 1}),
    })
    provider.sync_turn("hello", "hi there")
    assert rec.requests == []


def test_sync_turn_opt_in_capture_is_non_blocking(ergo_plugin_module, provider):
    release = threading.Event()

    def _slow_handler(request: httpx.Request) -> httpx.Response:
        release.wait(timeout=2)
        return httpx.Response(200, json={"result": "ingested", "chunks": 1, "stored": 1})

    transport = httpx.MockTransport(_slow_handler)
    provider._client = ergo_plugin_module.ErgoClient(
        base_url="http://ergo.test", token=None, transport=transport
    )
    provider._project = "unit-proj"
    provider._org_id = "acme"
    provider._who = "hermes-agent"
    provider._capture_turns = True
    provider._agent_context = "primary"
    provider._session_id = "sess-1"

    started = time.monotonic()
    provider.sync_turn("hello", "hi there", session_id="sess-1")
    elapsed = time.monotonic() - started
    release.set()  # let the background thread's request complete
    assert elapsed < 1.0, "sync_turn() must return before the HTTP call completes"


def test_sync_turn_opt_in_capture_posts_to_ingest_never_remember(ergo_plugin_module, provider):
    rec = _wire(ergo_plugin_module, provider, {
        ("POST", "/ingest"): (200, {"result": "ingested", "chunks": 1, "stored": 1}),
        ("POST", "/remember"): (200, {"result": "stored", "claim_id": "should-not-happen"}),
    })
    provider._capture_turns = True
    provider._agent_context = "primary"
    provider._session_id = "sess-1"
    provider.sync_turn("hello", "hi there", session_id="sess-1")

    deadline = time.monotonic() + 2
    while not rec.requests and time.monotonic() < deadline:
        time.sleep(0.02)

    assert rec.paths_called() == ["/ingest"]


def test_sync_turn_opt_in_capture_skips_non_primary_agent_context(ergo_plugin_module, provider):
    rec = _wire(ergo_plugin_module, provider, {
        ("POST", "/ingest"): (200, {"result": "ingested", "chunks": 1, "stored": 1}),
    })
    provider._capture_turns = True
    provider._agent_context = "cron"
    provider._session_id = "sess-1"
    provider.sync_turn("hello", "hi there", session_id="sess-1")
    time.sleep(0.1)
    assert rec.requests == []


def test_sync_turn_opt_in_capture_uses_one_worker_not_one_thread_per_turn(ergo_plugin_module, provider, monkeypatch):
    """Regression (PR review): sync_turn() used to spawn a brand-new daemon
    thread on every call with no cap -- a slow/unresponsive engine plus a
    long session meant unbounded thread growth. A single persistent worker
    must drain every queued turn instead.

    Counts `threading.Thread(...)` CONSTRUCTIONS made by the code under test
    via a monkeypatched wrapper, scoped to this test -- not a live-thread
    scan by name. A process-wide `threading.enumerate()` filtered by name is
    wrong two ways: (1) the pre-fix code named every spawned thread
    "ergo-sync-turn" too, so counting distinct NAMES collapses any number of
    threads into one and can never fail; (2) even counting live objects by
    name is polluted by other tests' same-named worker threads that haven't
    finished exiting yet when this test's snapshot runs.
    """
    release = threading.Event()
    created: list = []
    real_thread = threading.Thread

    def _counting_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        if kwargs.get("name") == "ergo-sync-turn":
            created.append(t)
        return t

    monkeypatch.setattr(ergo_plugin_module.threading, "Thread", _counting_thread)

    def _slow_handler(request: httpx.Request) -> httpx.Response:
        release.wait(timeout=2)
        return httpx.Response(200, json={"result": "ingested", "chunks": 1, "stored": 1})

    transport = httpx.MockTransport(_slow_handler)
    provider._client = ergo_plugin_module.ErgoClient(
        base_url="http://ergo.test", token=None, transport=transport
    )
    provider._project = "unit-proj"
    provider._org_id = "acme"
    provider._who = "hermes-agent"
    provider._capture_turns = True
    provider._agent_context = "primary"
    provider._session_id = "sess-1"

    for i in range(10):
        provider.sync_turn(f"msg {i}", "reply", session_id="sess-1")

    release.set()
    for t in created:
        t.join(timeout=2)

    assert len(created) <= 1


def test_sync_turn_opt_in_capture_drops_rather_than_blocks_when_queue_is_full(ergo_plugin_module, provider):
    release = threading.Event()

    def _blocking_handler(request: httpx.Request) -> httpx.Response:
        release.wait(timeout=5)
        return httpx.Response(200, json={"result": "ingested"})

    transport = httpx.MockTransport(_blocking_handler)
    provider._client = ergo_plugin_module.ErgoClient(
        base_url="http://ergo.test", token=None, transport=transport
    )
    provider._project = "unit-proj"
    provider._org_id = "acme"
    provider._who = "hermes-agent"
    provider._capture_turns = True
    provider._agent_context = "primary"
    provider._session_id = "sess-1"
    provider._capture_queue = ergo_plugin_module.queue.Queue(maxsize=2)

    try:
        started = time.monotonic()
        for i in range(20):
            provider.sync_turn(f"msg {i}", "reply", session_id="sess-1")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, "a full capture queue must drop, never block sync_turn()"
    finally:
        release.set()


# --------------------------------------------------------------------------- #
# Capture worker: respawn-race fix and shutdown flush (deterministic --
# driven directly, never through wall-clock timing/sleep coordination)
# --------------------------------------------------------------------------- #


def test_drain_loop_rechecks_queue_before_exiting_on_a_racing_enqueue(ergo_plugin_module, provider, monkeypatch):
    """Regression (orchestrator, traced independently): on a plain
    queue.Empty the drain loop used to exit unconditionally, even if a job
    was enqueued in the narrow window between the timed-out get() and the
    loop's exit decision -- that job then has no drainer until the next
    sync_turn() call, and is lost entirely if the session ends first.

    Driven synchronously (no thread, no real timeout wait): the queue's own
    get() is wrapped so its FIRST call raises Empty exactly like a real
    timeout would, but as a side effect also enqueues a job -- modelling a
    job landing in that exact race window. The second call retrieves it
    with no wait; the third call finds a genuinely empty queue and the loop
    exits cleanly.
    """
    processed = []

    def _job():
        processed.append("ran")

    real_queue = provider._capture_queue
    real_get = real_queue.get  # captured BEFORE patching -- the actual bound method
    calls = {"n": 0}

    def _racing_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            real_queue.put_nowait(_job)  # simulates a job landing in the race window
            raise ergo_plugin_module.queue.Empty()
        if calls["n"] == 2:
            # NOT real_queue.get_nowait() -- that calls self.get(...) internally,
            # which would recurse into THIS mock (still installed) instead of
            # actually removing the item, looping forever. Call the captured
            # real implementation directly.
            return real_get(block=False)
        raise ergo_plugin_module.queue.Empty()  # genuinely empty now -- exit cleanly

    monkeypatch.setattr(real_queue, "get", _racing_get)

    provider._drain_capture_queue()  # run in THIS thread -- no timing involved

    assert processed == ["ran"]
    assert provider._capture_worker is None


class _FakeThread:
    """Records start() calls and reports a controllable is_alive() -- lets
    _ensure_capture_worker() be tested without any real threading."""

    instances: list = []

    def __init__(self, target=None, daemon=None, name=None):
        self.name = name
        self.started = False
        self._alive = True
        _FakeThread.instances.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self._alive


def test_ensure_capture_worker_spawns_when_none(ergo_plugin_module, provider, monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr(ergo_plugin_module.threading, "Thread", _FakeThread)

    assert provider._capture_worker is None
    provider._ensure_capture_worker()

    assert len(_FakeThread.instances) == 1
    assert _FakeThread.instances[0].started is True
    assert provider._capture_worker is _FakeThread.instances[0]


def test_ensure_capture_worker_does_not_spawn_a_second_time_while_alive(ergo_plugin_module, provider, monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr(ergo_plugin_module.threading, "Thread", _FakeThread)

    provider._ensure_capture_worker()
    provider._ensure_capture_worker()

    assert len(_FakeThread.instances) == 1  # not spawned again while is_alive() is True


def test_ensure_capture_worker_respawns_once_the_old_one_is_gone(ergo_plugin_module, provider, monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr(ergo_plugin_module.threading, "Thread", _FakeThread)

    provider._ensure_capture_worker()
    provider._capture_worker = None  # what the fixed drain loop does on a clean exit
    provider._ensure_capture_worker()

    assert len(_FakeThread.instances) == 2


def test_shutdown_flushes_a_queued_job_before_closing_the_client(ergo_plugin_module, provider, monkeypatch):
    """shutdown() must join the capture worker (letting a queued/in-flight
    job finish) BEFORE closing the client -- not the other way around, which
    used to fail the job's request against an already-closed httpx.Client.
    No sleep: a plain (non-timed-out) join() blocks until the fast mock
    handler's job genuinely completes, so the ordering is deterministic --
    the worker's own idle timeout is shrunk only to keep this test's own
    join() (bounded by the real _SHUTDOWN_DRAIN_TIMEOUT) from waiting on the
    worker's post-job idle wait before returning."""
    monkeypatch.setattr(ergo_plugin_module, "_CAPTURE_WORKER_IDLE_TIMEOUT", 0.05)
    events = []

    def _handler(request: httpx.Request) -> httpx.Response:
        events.append("request")
        return httpx.Response(200, json={"result": "ingested"})

    transport = httpx.MockTransport(_handler)
    client = ergo_plugin_module.ErgoClient(base_url="http://ergo.test", token=None, transport=transport)
    real_close = client.close

    def _tracking_close():
        events.append("close")
        real_close()

    client.close = _tracking_close
    provider._client = client
    provider._project = "unit-proj"
    provider._org_id = "acme"
    provider._who = "hermes-agent"
    provider._capture_turns = True
    provider._agent_context = "primary"
    provider._session_id = "sess-1"

    provider.sync_turn("hello", "hi there", session_id="sess-1")
    provider.shutdown()

    assert events == ["request", "close"]


def test_shutdown_logs_and_does_not_hang_when_drain_times_out(ergo_plugin_module, provider, monkeypatch, caplog):
    """A bounded join() must not let shutdown() hang on an unresponsive
    engine -- it should give up after _SHUTDOWN_DRAIN_TIMEOUT and log what
    was left queued instead of blocking indefinitely."""
    monkeypatch.setattr(ergo_plugin_module, "_SHUTDOWN_DRAIN_TIMEOUT", 0.05)
    release = threading.Event()

    def _blocking_handler(request: httpx.Request) -> httpx.Response:
        release.wait(timeout=5)
        return httpx.Response(200, json={"result": "ingested"})

    transport = httpx.MockTransport(_blocking_handler)
    provider._client = ergo_plugin_module.ErgoClient(
        base_url="http://ergo.test", token=None, transport=transport
    )
    provider._project = "unit-proj"
    provider._org_id = "acme"
    provider._who = "hermes-agent"
    provider._capture_turns = True
    provider._agent_context = "primary"
    provider._session_id = "sess-1"

    provider.sync_turn("hello", "hi there", session_id="sess-1")

    try:
        with caplog.at_level("WARNING", logger="_ergo_hermes_plugin_under_test"):
            started = time.monotonic()
            provider.shutdown()
            elapsed = time.monotonic() - started
        assert elapsed < 1.0, "shutdown() must not hang waiting on an unresponsive engine"
        assert any("timed out" in r.message for r in caplog.records)
    finally:
        release.set()


# --------------------------------------------------------------------------- #
# handle_tool_call(): routes each tool, surfaces 409 as data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool_name,path,method", [
    ("ergo_remember", "/remember", "POST"),
    ("ergo_learn", "/learn", "POST"),
    ("ergo_recall", "/recall", "GET"),
    ("ergo_why", "/why", "GET"),
    ("ergo_supersede", "/supersede", "POST"),
    ("ergo_retract", "/retract", "POST"),
    ("ergo_ingest", "/ingest", "POST"),
    ("ergo_active", "/active", "GET"),
    ("ergo_history", "/history", "GET"),
    ("ergo_health", "/health", "GET"),
])
def test_handle_tool_call_routes_to_expected_endpoint(ergo_plugin_module, provider, tool_name, path, method):
    rec = _wire(ergo_plugin_module, provider, {
        (method, path): (200, {"result": "ok"}),
    })
    args = {
        "ergo_remember": {"statement": "x", "reason": "y"},
        "ergo_learn": {"statement": "x", "source": "y"},
        "ergo_recall": {"query": "x"},
        "ergo_why": {"query": "x"},
        "ergo_supersede": {"existing_id": "id1", "statement": "x"},
        "ergo_retract": {"claim_id": "id1", "reason": "y"},
        "ergo_ingest": {"text": "some doc"},
        "ergo_active": {},
        "ergo_history": {},
        "ergo_health": {},
    }[tool_name]
    result = provider.handle_tool_call(tool_name, args)
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed["result"] == "ok"
    assert rec.last.method == method
    assert rec.last.url.path == path


def test_handle_tool_call_ignores_scope_fields_supplied_in_args(ergo_plugin_module, provider):
    """A model could emit org_id/project/who even though get_tool_schemas()
    never declares them (PR review finding) -- the provider's own configured
    scope must always win, never a value smuggled in through tool args."""
    rec = _wire(ergo_plugin_module, provider, {
        ("POST", "/remember"): (200, {"result": "stored", "claim_id": "c1"}),
    })
    provider.handle_tool_call("ergo_remember", {
        "statement": "x", "reason": "y",
        "org_id": "attacker-org", "project": "attacker-project", "who": "attacker",
    })
    body = rec.last_json()
    assert body["org_id"] == "acme"
    assert body["project"] == "unit-proj"
    assert body["who"] == "hermes-agent"


def test_get_tool_schemas_reject_additional_properties(ergo_plugin_module, provider):
    for schema in provider.get_tool_schemas():
        assert schema["parameters"].get("additionalProperties") is False


def test_handle_tool_call_409_conflict_is_returned_as_usable_data_not_raised(ergo_plugin_module, provider):
    conflict_body = {
        "result": "conflict",
        "conflicts": [{"claim": {"id": "old1", "statement": "Deploy on Fridays"}, "tier": "block", "reason": "opposing modality"}],
    }
    _wire(ergo_plugin_module, provider, {
        ("POST", "/remember"): (409, conflict_body),
    })
    result = provider.handle_tool_call("ergo_remember", {"statement": "Never deploy on Fridays", "reason": "incident risk"})
    parsed = json.loads(result)
    assert parsed["result"] == "conflict"
    assert parsed["conflicts"][0]["claim"]["id"] == "old1"


def test_handle_tool_call_surfaces_genuine_errors_structured_not_raised(ergo_plugin_module, provider):
    _wire(ergo_plugin_module, provider, {
        ("POST", "/remember"): (500, {"error": "normalizer down"}),
    })
    result = provider.handle_tool_call("ergo_remember", {"statement": "x", "reason": "y"})
    parsed = json.loads(result)
    assert parsed["result"] == "error"
    assert parsed["status_code"] == 500


def test_handle_tool_call_unknown_tool_raises_not_implemented(ergo_plugin_module, provider):
    with pytest.raises(NotImplementedError):
        provider.handle_tool_call("ergo_does_not_exist", {})


def test_get_tool_schemas_returns_openai_function_format_for_all_tools(ergo_plugin_module, provider):
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == {
        "ergo_remember", "ergo_learn", "ergo_recall", "ergo_why",
        "ergo_supersede", "ergo_retract", "ergo_ingest", "ergo_active",
        "ergo_history", "ergo_health",
    }
    for schema in schemas:
        assert "description" in schema and schema["description"]
        assert schema["parameters"]["type"] == "object"


# --------------------------------------------------------------------------- #
# Storage honours hermes_home -- never ~/.hermes
# --------------------------------------------------------------------------- #


def test_save_config_writes_under_hermes_home_not_real_home(ergo_plugin_module, provider, tmp_path, monkeypatch):
    tripwire_home = tmp_path / "tripwire-home"
    tripwire_home.mkdir()
    monkeypatch.setattr(ergo_plugin_module.Path, "home", staticmethod(lambda: tripwire_home))

    real_hermes_home = tmp_path / "actual-hermes-home"
    provider.save_config({"project": "acme-proj", "api_url": "http://x:8788"}, str(real_hermes_home))

    config_path = real_hermes_home / "ergo" / "config.json"
    assert config_path.is_file()
    data = json.loads(config_path.read_text())
    assert data["project"] == "acme-proj"

    assert not (tripwire_home / ".hermes").exists()
    assert list(tripwire_home.iterdir()) == []


def test_save_config_never_persists_the_secret_token(ergo_plugin_module, provider, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    provider.save_config({"project": "p", "api_token": "sk-should-not-be-written"}, str(hermes_home))
    data = json.loads((hermes_home / "ergo" / "config.json").read_text())
    assert "api_token" not in data


def test_save_config_with_empty_hermes_home_writes_nothing(ergo_plugin_module, provider, tmp_path, monkeypatch):
    """Regression (PR review): _read_config_file() guards `if not hermes_home:
    return {}`, but save_config() had no matching guard -- an empty string
    resolves Path("") to ".", so save_config would silently write
    ./ergo/config.json relative to the process's CWD (a location
    _read_config_file() can never find again, since it treats a falsy
    hermes_home as "no config" up front)."""
    cwd = tmp_path / "wherever-hermes-happened-to-be-launched-from"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    provider.save_config({"project": "p"}, "")

    assert not (cwd / "ergo").exists()
    assert list(cwd.iterdir()) == []


def test_save_config_with_none_hermes_home_writes_nothing(ergo_plugin_module, provider, tmp_path, monkeypatch):
    cwd = tmp_path / "wherever-hermes-happened-to-be-launched-from"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    provider.save_config({"project": "p"}, None)

    assert not (cwd / "ergo").exists()
    assert list(cwd.iterdir()) == []


def test_initialize_reads_project_from_saved_config(ergo_plugin_module, provider, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    provider.save_config({"project": "from-config-file"}, str(hermes_home))
    provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
    assert provider._project == "from-config-file"


def test_initialize_project_falls_back_to_agent_workspace(ergo_plugin_module, provider, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    provider.initialize(
        "sess-1", hermes_home=str(hermes_home), platform="cli", agent_workspace="team-workspace"
    )
    assert provider._project == "team-workspace"


def test_initialize_project_falls_back_to_default_literal(ergo_plugin_module, provider, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
    assert provider._project == ergo_plugin_module.DEFAULT_PROJECT


def test_initialize_config_file_project_wins_over_agent_workspace(ergo_plugin_module, provider, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    provider.save_config({"project": "explicit-config"}, str(hermes_home))
    provider.initialize(
        "sess-1", hermes_home=str(hermes_home), platform="cli", agent_workspace="team-workspace"
    )
    assert provider._project == "explicit-config"


def test_initialize_warns_if_reinitialized_with_a_different_session_id(ergo_plugin_module, provider, tmp_path, caplog):
    """Defense-in-depth (PR review, ruled a false positive for the current
    one-instance-per-session model, but made loud rather than silent in case
    that model ever changes): a second initialize() call with a DIFFERENT
    session_id on the same instance means instance-level scope state
    (_project/_org_id/_who) is about to reflect the wrong session."""
    hermes_home = tmp_path / "hermes-home"
    with caplog.at_level("WARNING", logger="_ergo_hermes_plugin_under_test"):
        provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
        provider.initialize("sess-2", hermes_home=str(hermes_home), platform="cli")
    assert any("different session_id" in r.message for r in caplog.records)


def test_initialize_does_not_warn_on_first_call_or_same_session_id(ergo_plugin_module, provider, tmp_path, caplog):
    hermes_home = tmp_path / "hermes-home"
    with caplog.at_level("WARNING", logger="_ergo_hermes_plugin_under_test"):
        provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
        provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
    assert not any("different session_id" in r.message for r in caplog.records)


def test_initialize_closes_the_previous_client_before_replacing_it(ergo_plugin_module, provider, tmp_path):
    """Regression (PR review): initialize() unconditionally replaced
    self._client with a new ErgoClient without closing the previous one --
    on a double-init (the same scenario the session-id-change warning above
    already anticipates), the old httpx.Client's connection pool leaked
    silently."""
    hermes_home = tmp_path / "hermes-home"
    provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
    old_client = provider._client
    closed = []
    old_client.close = lambda: closed.append("closed")

    provider.initialize("sess-2", hermes_home=str(hermes_home), platform="cli")

    assert closed == ["closed"]
    assert provider._client is not old_client


def test_initialize_closes_the_previous_client_on_the_kill_switch_path(ergo_plugin_module, provider, tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    provider.initialize("sess-1", hermes_home=str(hermes_home), platform="cli")
    old_client = provider._client
    closed = []
    old_client.close = lambda: closed.append("closed")

    monkeypatch.setenv("ERGO_API_URL", "")
    provider.initialize("sess-2", hermes_home=str(hermes_home), platform="cli")

    assert closed == ["closed"]
    assert provider._client is None


# --------------------------------------------------------------------------- #
# Config schema
# --------------------------------------------------------------------------- #


def test_config_schema_marks_api_token_secret(ergo_plugin_module, provider):
    schema = provider.get_config_schema()
    by_key = {f["key"]: f for f in schema}
    assert by_key["api_token"]["secret"] is True
    assert "api_url" in by_key
    assert "project" in by_key
    assert by_key["api_url"].get("secret", False) is False


def test_shutdown_is_safe_when_never_initialized(provider):
    provider.shutdown()  # must not raise
