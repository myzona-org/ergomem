"""No-network boot test: all baked models must load with no outbound network.

Run inside the Docker build with HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1,
ERGO_NLI_LOCAL_ONLY=1. We monkey-patch socket.getaddrinfo so ANY network
attempt raises loud — that proves the baked models load offline and that the
runtime never falls back to a silent fetch.

Build fails (and so does the image) if this script exits non-zero.
"""
from __future__ import annotations
import os
import socket
import sys


def _block(*args, **kwargs):
    raise RuntimeError(f"NO_NETWORK_AT_BOOT attempted: getaddrinfo{args!r}")


def main() -> int:
    nli_repo = os.environ.get("ERGO_NLI_MODEL", "Xenova/nli-deberta-v3-base")
    nli_variant = os.environ.get("ERGO_NLI_VARIANT", "quantized")
    nli_cache = os.environ.get("ERGO_NLI_CACHE", "/models/nli")

    print("--- network blocked, loading baked models ---", flush=True)
    socket.getaddrinfo = _block  # hard-disable name resolution

    # 1. Embedder (fastembed).
    from ergo.embedders import FastEmbed

    e = FastEmbed()
    v = e.embed("hello")
    assert len(v) == e.dim, f"embedder dim mismatch: {len(v)} vs {e.dim}"
    print(f"embedder OK    dim={e.dim}", flush=True)

    # 2. NLI judge (ONNX, via huggingface_hub local-only).
    from ergo.contradiction.nli import NLIJudge

    NLIJudge(
        repo=nli_repo,
        variant=nli_variant,
        cache_dir=nli_cache,
        local_files_only=True,
    )
    print(f"NLI judge OK   repo={nli_repo} variant={nli_variant}", flush=True)

    # 3. sqlite-vec extension load.
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    print("sqlite-vec OK", flush=True)

    print("=== no-network boot test passed ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
