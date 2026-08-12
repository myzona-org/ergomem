"""Pluggable embedding providers for Ergo.

Pick a provider with ERGO_EMBED_PROVIDER (default: fastembed, fully local/offline).
Each provider declares its vector dimension so `ergo init` can build the right column.

    ERGO_EMBED_PROVIDER = fastembed | ollama | openai | cohere | voyage
    ERGO_EMBED_MODEL     = provider-specific model name (sensible default per provider)
    ERGO_EMBED_DIM       = override dimension (only needed for custom models)

Add a provider by registering a class in PROVIDERS — no other file changes needed.
"""
from __future__ import annotations

import os
import threading
from typing import Protocol


class Embedder(Protocol):
    dim: int
    def embed(self, text: str) -> list[float]: ...


def _raise_for_status_with_body(r) -> None:
    """Like ``r.raise_for_status()`` but folds the response body into the
    error, so a provider's JSON error detail (bad model, quota, bad input)
    isn't discarded. The body is safe to include — credentials only ever
    travel in the request header, never the response.
    """
    import httpx
    try:
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise httpx.HTTPStatusError(
            f"{exc}: {r.text}", request=exc.request, response=exc.response
        ) from exc


# ---- providers -------------------------------------------------------------

class FastEmbed:
    """Local, offline ONNX. Zero credentials. Default.

    Uses a local model cache (ERGO_FASTEMBED_CACHE / FASTEMBED_CACHE, default
    ~/.cache/fastembed_cache). If the cache dir already holds a model, `embed()`
    requests `local_files_only=True` from fastembed so it never phones home --
    a plain, call-local constructor kwarg (no process-global env mutation, so
    nothing to race across threads or leak into unrelated code sharing the
    process).
    """
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or "BAAI/bge-small-en-v1.5"
        self.dim = dim or 384
        self.cache_dir = (os.environ.get("ERGO_FASTEMBED_CACHE")
                          or os.environ.get("FASTEMBED_CACHE")
                          or os.path.expanduser("~/.cache/fastembed_cache"))
        self._m = None
        self._init_lock = threading.Lock()
        # Decide ONCE, here at construction, whether the cache dir already
        # holds *something* -- never re-checked, never touched again. This
        # replaces the old implementation, which set-then-restored the
        # PROCESS-GLOBAL `os.environ["HF_HUB_OFFLINE"]` on EVERY embed() call:
        # that raced under concurrent embed() calls from multiple
        # bounded-threaded-server worker threads (one thread's restore could
        # stomp another thread's still-in-flight "1"), AND -- discovered via
        # the prove-the-bug-first CI run for this change -- leaking a
        # process-global env var also broke completely unrelated code later in
        # the SAME process (e.g. a real NLIJudge() download in another test)
        # once set, since it was never restored. Fixed by not touching
        # os.environ AT ALL: the offline decision is passed straight to
        # `TextEmbedding(..., local_files_only=True)` below, a plain
        # constructor kwarg fastembed already supports (see
        # fastembed.common.model_management.ModelManagement.download_model),
        # which is local to this one call -- no shared state, so nothing to
        # race and nothing to leak. Only force it on when the cache dir
        # already has something in it -- on a fresh machine that skipped the
        # pre-cache step, forcing offline unconditionally would turn a normal
        # first-time download into a hard failure.
        self._local_files_only = bool(
            os.path.isdir(self.cache_dir) and os.listdir(self.cache_dir))

    def embed(self, text: str) -> list[float]:
        if self._m is None:
            # Double-checked locking: guards the lazy TextEmbedding load itself
            # (a separate concern from the offline-mode fix above) so two
            # threads racing on the first-ever embed() call can't both
            # construct the model and have one silently overwrite self._m.
            with self._init_lock:
                if self._m is None:
                    from fastembed import TextEmbedding
                    kwargs = {"local_files_only": True} if self._local_files_only else {}
                    self._m = TextEmbedding(model_name=self.model,
                                            cache_dir=self.cache_dir, **kwargs)
        return [float(x) for x in next(self._m.embed([text]))]


class Ollama:
    """Local server (http://localhost:11434). Good for self-hosters who don't want fastembed."""
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or "nomic-embed-text"
        self.dim = dim or 768
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    def embed(self, text: str) -> list[float]:
        import httpx
        r = httpx.post(f"{self.host}/api/embeddings",
                       json={"model": self.model, "prompt": text}, timeout=60)
        _raise_for_status_with_body(r)
        return r.json()["embedding"]


class OpenAI:
    """Hosted. Needs OPENAI_API_KEY. The 'I just have a key' path."""
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or "text-embedding-3-small"
        self.dim = dim or 1536
        self.key = os.environ["OPENAI_API_KEY"]

    def embed(self, text: str) -> list[float]:
        import httpx
        r = httpx.post("https://api.openai.com/v1/embeddings",
                       headers={"Authorization": f"Bearer {self.key}"},
                       json={"model": self.model, "input": text}, timeout=60)
        _raise_for_status_with_body(r)
        return r.json()["data"][0]["embedding"]


class Cohere:
    """Hosted. Needs COHERE_API_KEY."""
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or "embed-english-v3.0"
        self.dim = dim or 1024
        self.key = os.environ["COHERE_API_KEY"]

    def embed(self, text: str) -> list[float]:
        import httpx
        r = httpx.post("https://api.cohere.com/v2/embed",
                       headers={"Authorization": f"Bearer {self.key}"},
                       json={"model": self.model, "texts": [text],
                             "input_type": "search_document", "embedding_types": ["float"]},
                       timeout=60)
        _raise_for_status_with_body(r)
        return r.json()["embeddings"]["float"][0]


class Voyage:
    """Hosted. Needs VOYAGE_API_KEY."""
    def __init__(self, model: str | None = None, dim: int | None = None):
        self.model = model or "voyage-3-lite"
        self.dim = dim or 512
        self.key = os.environ["VOYAGE_API_KEY"]

    def embed(self, text: str) -> list[float]:
        import httpx
        r = httpx.post("https://api.voyageai.com/v1/embeddings",
                       headers={"Authorization": f"Bearer {self.key}"},
                       json={"model": self.model, "input": [text]}, timeout=60)
        _raise_for_status_with_body(r)
        return r.json()["data"][0]["embedding"]


PROVIDERS: dict[str, type] = {
    "fastembed": FastEmbed,
    "ollama": Ollama,
    "openai": OpenAI,
    "cohere": Cohere,
    "voyage": Voyage,
}

_active: Embedder | None = None


def build(name: str, model: str | None = None, dim: int | None = None) -> Embedder:
    """Construct a specific provider instance (not the env singleton).

    Used by migrations that need a target embedder distinct from the active one.
    """
    name = name.lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider '{name}'. options: {', '.join(PROVIDERS)}")
    return PROVIDERS[name](model=model, dim=dim)


def get_embedder() -> Embedder:
    """Singleton embedder chosen from env. Dimension is locked once chosen."""
    global _active
    if _active is None:
        name = os.environ.get("ERGO_EMBED_PROVIDER", "fastembed").lower()
        if name not in PROVIDERS:
            raise ValueError(f"unknown ERGO_EMBED_PROVIDER='{name}'. options: {', '.join(PROVIDERS)}")
        model = os.environ.get("ERGO_EMBED_MODEL")
        dim_env = os.environ.get("ERGO_EMBED_DIM")
        dim = int(dim_env) if dim_env else None
        _active = PROVIDERS[name](model=model, dim=dim)
    return _active


def active_dim() -> int:
    return get_embedder().dim


def embed(text: str) -> list[float]:
    return get_embedder().embed(text)
