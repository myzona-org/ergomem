"""Tests for ergo.embedders — provider factory + per-provider request/parse logic.

No network: provider construction is lazy (FastEmbed doesn't load the ONNX model
until embed()), and the hosted providers' embed() paths are tested with a stubbed
httpx.post so we verify URL/headers/payload and the provider-specific response
shape (each vendor nests the vector differently — that's where bugs hide).
"""
from __future__ import annotations

import os

import httpx
import pytest

from ergo import embedders
from ergo.embedders import (
    build, get_embedder, active_dim, embed,
    FastEmbed, Ollama, OpenAI, Cohere, Voyage, PROVIDERS,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the env singleton + clear every provider-selecting / credential var."""
    monkeypatch.setattr(embedders, "_active", None)
    for k in ("ERGO_EMBED_PROVIDER", "ERGO_EMBED_MODEL", "ERGO_EMBED_DIM",
              "ERGO_FASTEMBED_CACHE", "FASTEMBED_CACHE", "OLLAMA_HOST",
              "OPENAI_API_KEY", "COHERE_API_KEY", "VOYAGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------
# Fake httpx response/post
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._payload


def _fake_post(captured: dict, payload, raise_exc=None):
    def _post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Resp(payload, raise_exc)
    return _post


# --------------------------------------------------------------------------
# build()  — explicit provider construction
# --------------------------------------------------------------------------
def test_build_returns_requested_provider():
    assert isinstance(build("fastembed"), FastEmbed)
    assert isinstance(build("ollama"), Ollama)


def test_build_is_case_insensitive():
    assert isinstance(build("OLLAMA"), Ollama)


def test_build_unknown_provider_raises_with_options():
    with pytest.raises(ValueError) as ei:
        build("bogus")
    msg = str(ei.value)
    assert "bogus" in msg
    assert "fastembed" in msg  # the options list is surfaced


def test_build_passes_model_and_dim_through():
    e = build("ollama", model="custom-embed", dim=99)
    assert e.model == "custom-embed"
    assert e.dim == 99


def test_build_openai_without_key_raises_keyerror():
    # The hosted constructors read the key eagerly from the env.
    with pytest.raises(KeyError):
        build("openai")


def test_every_registered_provider_is_constructible_offline(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("COHERE_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "k")
    for name in PROVIDERS:
        e = build(name)
        assert isinstance(e.dim, int) and e.dim > 0


# --------------------------------------------------------------------------
# get_embedder() — env-driven singleton, dim locked once chosen
# --------------------------------------------------------------------------
def test_get_embedder_defaults_to_fastembed():
    e = get_embedder()
    assert isinstance(e, FastEmbed)
    assert e.dim == 384


def test_get_embedder_honours_env_provider(monkeypatch):
    monkeypatch.setenv("ERGO_EMBED_PROVIDER", "ollama")
    assert isinstance(get_embedder(), Ollama)


def test_get_embedder_honours_model_and_dim_overrides(monkeypatch):
    monkeypatch.setenv("ERGO_EMBED_PROVIDER", "ollama")
    monkeypatch.setenv("ERGO_EMBED_MODEL", "my-model")
    monkeypatch.setenv("ERGO_EMBED_DIM", "256")
    e = get_embedder()
    assert e.model == "my-model"
    assert e.dim == 256


def test_get_embedder_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("ERGO_EMBED_PROVIDER", "nope")
    with pytest.raises(ValueError) as ei:
        get_embedder()
    assert "nope" in str(ei.value)


def test_get_embedder_is_a_locked_singleton(monkeypatch):
    first = get_embedder()  # fastembed
    # changing the env afterwards must NOT swap the active embedder (dim is locked)
    monkeypatch.setenv("ERGO_EMBED_PROVIDER", "ollama")
    second = get_embedder()
    assert second is first
    assert second.dim == 384


def test_active_dim_reflects_active_embedder(monkeypatch):
    monkeypatch.setenv("ERGO_EMBED_PROVIDER", "ollama")
    assert active_dim() == 768


def test_module_embed_delegates_to_active(monkeypatch):
    class _Stub:
        dim = 3
        def __init__(self):
            self.seen = None
        def embed(self, text):
            self.seen = text
            return [1.0, 2.0, 3.0]

    stub = _Stub()
    monkeypatch.setattr(embedders, "_active", stub)
    assert embed("hello") == [1.0, 2.0, 3.0]
    assert stub.seen == "hello"


# --------------------------------------------------------------------------
# Provider defaults (constructed lazily, no model load / no network)
# --------------------------------------------------------------------------
def test_fastembed_defaults():
    e = FastEmbed()
    assert e.model == "BAAI/bge-small-en-v1.5"
    assert e.dim == 384
    assert e._m is None  # model not loaded at construction


def test_fastembed_cache_dir_env_precedence(monkeypatch):
    monkeypatch.setenv("FASTEMBED_CACHE", "/tmp/fallback")
    monkeypatch.setenv("ERGO_FASTEMBED_CACHE", "/tmp/preferred")
    assert FastEmbed().cache_dir == "/tmp/preferred"
    monkeypatch.delenv("ERGO_FASTEMBED_CACHE")
    assert FastEmbed().cache_dir == "/tmp/fallback"


def test_ollama_host_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://box:1234")
    assert Ollama().host == "http://box:1234"


# --------------------------------------------------------------------------
# FastEmbed offline-mode contract (finding #3, revised for concurrency):
# the old implementation set-then-restored the PROCESS-GLOBAL
# `os.environ["HF_HUB_OFFLINE"]` on EVERY embed() call, which (a) raced under
# concurrent embed() calls from multiple bounded-threaded-server worker
# threads (one thread's restore could stomp another's still in-flight "1"),
# and (b) even set-once-at-init still leaked into completely unrelated code
# later in the SAME process once left set (caught by this change's own CI
# run: a real NLIJudge() download in another test file failed offline because
# an earlier FastEmbed() construction had left HF_HUB_OFFLINE=1 process-wide).
# Fixed by not touching os.environ AT ALL: the offline decision (made once,
# at __init__, from whether the cache dir already has something in it) is
# passed straight to `TextEmbedding(..., local_files_only=True)` — a plain
# constructor kwarg fastembed already supports — which is local to that one
# call, so there is no shared state to race OR to leak.
# --------------------------------------------------------------------------
class _FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding — records the `local_files_only`
    kwarg it was constructed with, without touching the network or ONNX."""

    seen_local_files_only: dict = {}

    def __init__(self, model_name=None, cache_dir=None, **kwargs):
        _FakeTextEmbedding.seen_local_files_only["value"] = kwargs.get("local_files_only")

    def embed(self, texts):
        yield [0.1, 0.2]


def test_fastembed_does_not_request_local_files_only_when_cache_dir_is_empty(
        tmp_path, monkeypatch):
    monkeypatch.setenv("ERGO_FASTEMBED_CACHE", str(tmp_path))  # exists but empty -> nothing cached yet
    e = FastEmbed(dim=2)
    assert e._local_files_only is False
    env_before = dict(os.environ)
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeTextEmbedding)
    e.embed("hi")
    assert _FakeTextEmbedding.seen_local_files_only["value"] is None
    assert dict(os.environ) == env_before  # no os.environ mutation whatsoever


def test_fastembed_requests_local_files_only_when_cache_dir_is_populated(
        tmp_path, monkeypatch):
    (tmp_path / "models--BAAI--bge-small-en-v1.5").mkdir()
    monkeypatch.setenv("ERGO_FASTEMBED_CACHE", str(tmp_path))  # cache already populated
    e = FastEmbed(dim=2)
    assert e._local_files_only is True  # decided once, at construction
    env_before = dict(os.environ)
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeTextEmbedding)
    e.embed("hi")
    assert _FakeTextEmbedding.seen_local_files_only["value"] is True
    # No env var was ever touched -- nothing for another test's unrelated
    # huggingface_hub call (e.g. a real NLIJudge()) to trip over later in the
    # same process.
    assert dict(os.environ) == env_before


def test_fastembed_lazy_model_load_is_not_duplicated_under_concurrent_embed(tmp_path, monkeypatch):
    """Two threads racing on the FIRST-EVER embed() call (self._m is None on
    both) must not each construct their own TextEmbedding -- the
    double-checked lock inside embed() must serialize the lazy load so
    self._m is built exactly once."""
    import threading as _threading
    import time as _time

    build_count = {"n": 0}

    class _SlowFakeTextEmbedding:
        def __init__(self, model_name=None, cache_dir=None, **kwargs):
            # widen the race window so two threads are very likely to both
            # observe self._m is None before either finishes constructing
            _time.sleep(0.05)
            build_count["n"] += 1

        def embed(self, texts):
            yield [0.1, 0.2]

    monkeypatch.setenv("ERGO_FASTEMBED_CACHE", str(tmp_path))
    monkeypatch.setattr("fastembed.TextEmbedding", _SlowFakeTextEmbedding)
    e = FastEmbed(dim=2)

    results = []
    errors = []

    def _worker():
        try:
            results.append(e.embed("hi"))
        except Exception as exc:  # pragma: no cover - surfaced via errors below
            errors.append(exc)

    threads = [_threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == 8
    assert all(r == [0.1, 0.2] for r in results)
    assert build_count["n"] == 1  # exactly one TextEmbedding ever constructed


def test_fastembed_concurrent_embed_calls_do_not_corrupt_os_environ(tmp_path, monkeypatch):
    """Many concurrent embed() calls (model already loaded) must never touch
    os.environ at all -- there is no per-call env mutation of any kind -- and
    every call must still return the correct, deterministic vector."""
    import threading as _threading

    class _EchoTextEmbedding:
        def __init__(self, model_name=None, cache_dir=None, **kwargs):
            pass

        def embed(self, texts):
            # deterministic vector derived from the input text's length, so a
            # cross-thread mix-up would show up as a wrong-length result
            t = texts[0]
            yield [float(len(t)), 1.0]

    (tmp_path / "models--BAAI--bge-small-en-v1.5").mkdir()
    monkeypatch.setenv("ERGO_FASTEMBED_CACHE", str(tmp_path))
    monkeypatch.setattr("fastembed.TextEmbedding", _EchoTextEmbedding)
    e = FastEmbed(dim=2)
    e.embed("warmup")  # force the lazy model load before the concurrent burst
    env_before = dict(os.environ)

    results: list[list[float]] = [None] * 40  # type: ignore[list-item]
    errors = []

    def _worker(i, text):
        try:
            results[i] = e.embed(text)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    texts = [f"claim number {i}" for i in range(40)]
    threads = [_threading.Thread(target=_worker, args=(i, t))
               for i, t in enumerate(texts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert all(r == [float(len(t)), 1.0] for r, t in zip(results, texts))
    assert dict(os.environ) == env_before  # no os.environ mutation whatsoever


# --------------------------------------------------------------------------
# Hosted embed() — request shaping + provider-specific response parsing
# --------------------------------------------------------------------------
def test_openai_embed_request_and_parse(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cap: dict = {}
    monkeypatch.setattr(httpx, "post",
                        _fake_post(cap, {"data": [{"embedding": [0.1, 0.2]}]}))
    vec = OpenAI().embed("hi")
    assert vec == [0.1, 0.2]
    assert cap["url"] == "https://api.openai.com/v1/embeddings"
    assert cap["headers"]["Authorization"] == "Bearer sk-test"
    assert cap["json"]["input"] == "hi"
    assert cap["json"]["model"] == "text-embedding-3-small"


def test_ollama_embed_request_and_parse(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(httpx, "post",
                        _fake_post(cap, {"embedding": [1.0, 2.0, 3.0]}))
    vec = Ollama().embed("hi")
    assert vec == [1.0, 2.0, 3.0]
    assert cap["url"].endswith("/api/embeddings")
    assert cap["json"] == {"model": "nomic-embed-text", "prompt": "hi"}


def test_cohere_embed_parses_nested_float_shape(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "co-test")
    cap: dict = {}
    monkeypatch.setattr(httpx, "post",
                        _fake_post(cap, {"embeddings": {"float": [[0.5, 0.6]]}}))
    vec = Cohere().embed("hi")
    assert vec == [0.5, 0.6]
    assert cap["json"]["input_type"] == "search_document"


def test_voyage_embed_parses_data_shape(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vo-test")
    cap: dict = {}
    monkeypatch.setattr(httpx, "post",
                        _fake_post(cap, {"data": [{"embedding": [0.7, 0.8]}]}))
    vec = Voyage().embed("hi")
    assert vec == [0.7, 0.8]
    assert cap["json"]["input"] == ["hi"]


def test_hosted_embed_propagates_http_errors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    boom = RuntimeError("502 bad gateway")
    monkeypatch.setattr(httpx, "post",
                        _fake_post({}, {}, raise_exc=boom))
    with pytest.raises(RuntimeError, match="502"):
        OpenAI().embed("hi")


# --------------------------------------------------------------------------
# hosted providers must surface the response body on non-2xx, without
# leaking the API key (finding #4)
# --------------------------------------------------------------------------
def test_openai_400_error_body_is_surfaced_without_leaking_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(
        400,
        json={"error": {"message": "invalid model 'text-embedding-3-smal'"}},
        request=request,
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        OpenAI().embed("hi")
    msg = str(ei.value)
    assert "invalid model" in msg
    assert "sk-super-secret" not in msg


def test_ollama_400_error_body_is_surfaced(monkeypatch):
    request = httpx.Request("POST", "http://localhost:11434/api/embeddings")
    response = httpx.Response(
        400, json={"error": "model 'bogus' not found"}, request=request
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **k: response)
    with pytest.raises(httpx.HTTPStatusError, match="not found"):
        Ollama().embed("hi")
