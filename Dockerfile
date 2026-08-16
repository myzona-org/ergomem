# Ergo \u2014 contradiction-on-write engine.
#
# Image policy (all-in-one offline image, no torch; updated 2026-06-26):
#   BAKED at build time, runs offline:
#     - app + sqlite-vec store + ONNX Runtime
#     - embedder:  BAAI/bge-small-en-v1.5 (~130 MB, fastembed)
#     - NLI judge: Xenova/nli-deberta-v3-base (~80\u2013190 MB quantized, ONNX)
#   REMOTE at runtime (the ONLY network dependency):
#     - instruct LLM for prose normalization \u2192 Anthropic Messages API
#       (claude-haiku-4-5 by default; older claude-3-5-haiku-* ids 404 on
#        accounts provisioned with the 4.x lineup).
#     - air-gapped fallback: set ERGO_LLM_PROVIDER=ollama and point
#       ERGO_LLM_BASE at a reachable Ollama; image stays the same.
#
# Target size: ~1 GB total. The instruct LLM is NOT baked \u2014 that's the
# >85 % weight saving over the original \"bake everything\" plan.
#
# Why no Postgres anymore: SQLite + sqlite-vec is the current storage layer,
# chosen so the image is a single offline binary with no separate DB service
# to run. Reversible if that stops being true.
#
# Layer order (fixed 2026-07-25, was a known cost trap): `ergo/` is installed
# via a throwaway STUB package (empty ergo/__init__.py) before the dependency
# install and the two model bakes below, then swapped for the real source
# tree and reinstalled (--no-deps --force-reinstall) AFTER the bakes. Editing
# ergo/ therefore only invalidates that one cheap reinstall layer + the
# no-network boot test that follows it -- never the ~320 MB embedder/NLI
# bakes, which previously re-ran (re-pulling from HuggingFace) on every PR
# that touched ergo/. Do not move `COPY ergo` back above the bakes.

FROM python:3.12-slim AS base

# OCI annotations so the published GHCR package self-links to its repo,
# README, and licence -- github.com reads org.opencontainers.image.source
# to make that association automatically, no manual "linked repository" step.
LABEL org.opencontainers.image.title="ergo" \
      org.opencontainers.image.description="Memory that remembers WHY -- and catches itself contradicting." \
      org.opencontainers.image.licenses="LicenseRef-Ergo-NonCommercial"
LABEL org.opencontainers.image.source="https://github.com/myzona-org/ergomem"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models/hf \
    ERGO_EMBED_PROVIDER=fastembed \
    ERGO_FASTEMBED_CACHE=/models/fastembed \
    ERGO_NLI_CACHE=/models/nli \
    ERGO_LLM_PROVIDER=anthropic \
    ERGO_LLM_MODEL=claude-haiku-4-5 \
    ERGO_DB_PATH=/data/ergo.db \
    ERGO_LLM_CACHE=/data/llm_norm_cache.json
    # ^ code default is <repo>/evals/contradiction/.cache/llm_norm.json, which
    # doesn't exist in this image and sits under /app (root-owned, read-only
    # to the non-root runtime user below) -- redirect it into the already
    # writable /data volume so the LLM normalizer's cache actually persists
    # instead of silently mkdir-failing and falling back to RuleNormalizer
    # on every boot.

# OS deps. sqlite-vec ships its own .so so libsqlite3-dev isn't needed.
# ca-certificates is required for the runtime HTTPS call to api.anthropic.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./

# --- Stub `ergo` package (layer-order fix, see below) -----------------------
# hatchling's [tool.hatch.build.targets.wheel] packages = ["ergo"] only needs
# the directory to exist to satisfy `pip install .` -- pyproject.toml pins a
# STATIC version = "0.1.0" (not setuptools_scm/hatch-vcs), so there's no
# version string that has to live in real code either. An empty stub lets the
# dependency install below run and be cached BEFORE the real `COPY ergo`, so
# editing ergo/ no longer invalidates it or the model bakes that follow.
RUN mkdir -p ergo && touch ergo/__init__.py

# Install ergo's deps: the 'engine' extra (fastembed + onnxruntime + tokenizers
# + huggingface_hub + sqlite-vec) plus 'mcp' (the adapter's streamable-HTTP
# transport, mcp==1.29.0 + its bundled starlette/uvicorn) so this image can
# also run `python -m ergo.mcp.server --transport streamable-http` as a
# second process/container in front of api.ergomem.com/mcp — same image,
# different command; the HTTP API's own ENTRYPOINT below is unchanged. This
# only installs against the STUB package above, so it depends on
# pyproject.toml's dependency list, never on ergo/'s actual source -- editing
# ergo/ does not invalidate this layer or the bakes below it.
#
# Container startup for that second process (NOT run by this image's own
# ENTRYPOINT/CMD -- started separately, e.g. by the orchestrator/compose file
# fronting the reverse proxy):
#   python -m ergo.mcp.server --transport streamable-http --host 0.0.0.0 \
#     --port 8790 --allowed-host api.ergomem.com --allowed-host mcp.example.com
# --host 0.0.0.0 is required here: the CLI's own default (127.0.0.1) only
# binds loopback INSIDE the container, which a published port / another
# container's reverse proxy can never reach. --allowed-host is the SEPARATE
# fix for the Host header the proxy forwards -- see docs/HTTP-API.md's "MCP
# endpoint" section and ergo/mcp/server.py's build_app() docstring. Bare-metal
# / local runs keep the secure --host 127.0.0.1 default; only a containerized,
# proxy-fronted deployment needs --host 0.0.0.0.
RUN pip install ".[engine,mcp]"

# --- Bake the EMBEDDER (~130 MB) -------------------------------------------
ARG ERGO_EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from fastembed import TextEmbedding; \
TextEmbedding(model_name='${ERGO_EMBED_MODEL}', cache_dir='/models/fastembed')" \
 && find /models/fastembed -type d -name '.locks' -prune -exec rm -rf {} + 2>/dev/null || true

# --- Bake the NLI JUDGE (~80\u2013190 MB) ----------------------------------------
# Pull just the files NLIJudge.__init__ actually loads, into the same cache_dir
# that ERGO_NLI_CACHE points at, so the runtime loader finds them under
# local_files_only=True.
ARG ERGO_NLI_MODEL=Xenova/nli-deberta-v3-base
ARG ERGO_NLI_VARIANT=quantized
RUN python -c "\
from huggingface_hub import hf_hub_download; \
repo='${ERGO_NLI_MODEL}'; cache='/models/nli'; \
hf_hub_download(repo, 'config.json', cache_dir=cache); \
hf_hub_download(repo, 'tokenizer.json', cache_dir=cache); \
hf_hub_download(repo, 'onnx/model_${ERGO_NLI_VARIANT}.onnx', cache_dir=cache); \
print('NLI model files cached at', cache)"

# --- Install the REAL `ergo` package ----------------------------------------
# Swap the stub for the actual source tree and reinstall ONLY the local
# project: --no-deps because the dependency install above already resolved
# and installed everything ergo needs (no re-fetch of onnxruntime etc.);
# --force-reinstall because pip would otherwise no-op (the stub already
# satisfies the requirement it installed earlier). This is the ONLY layer
# below the bakes that editing ergo/ invalidates -- by design, and cheap
# (a local wheel build + install, no dependency download).
COPY ergo ./ergo
RUN pip install --no-deps --force-reinstall .

# --- No-network boot test (all baked models must load with no outbound net) -
# Prove the baked models load with NO outbound DNS / HTTP. Build fails if
# anything tries to fetch — no silent-download regressions allowed.
# Logic lives in docker/boot-test.py so it can stay readable (def + multi-line).
# MUST run after the real `COPY ergo` above (not the stub): it imports
# ergo.embedders and ergo.contradiction.nli, which don't exist in the stub.
COPY docker/boot-test.py /tmp/boot-test.py
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 ERGO_NLI_LOCAL_ONLY=1 \
    ERGO_NLI_MODEL="${ERGO_NLI_MODEL}" ERGO_NLI_VARIANT="${ERGO_NLI_VARIANT}" \
    python /tmp/boot-test.py \
 && rm /tmp/boot-test.py

# --- Client tools ----------------------------------------------------------
# Ship the standalone repo ingestor (tools/ergo-ingest-repo). Already below
# the model bakes (and now below the real ergo install + boot test too) so
# editing it never invalidates those expensive layers. It only needs
# `requests`, already present transitively via huggingface_hub. Run inside
# the container with: python tools/ergo-ingest-repo/ergo_ingest_repo.py \
#   <repo> --project P --ergo-url http://127.0.0.1:8000
COPY tools ./tools

# --- Runtime ---------------------------------------------------------------
# Keep HF_HUB_OFFLINE=1 at runtime so any forgotten code path that tries to
# pull a new model crashes instead of silently fetching.
#
# Persist the baked-model selector ARGs into the runtime ENV so the boot-time
# loader uses exactly the model/variant that was baked. Without this, a build
# with e.g. --build-arg ERGO_NLI_VARIANT=int8 bakes only model_int8.onnx while
# the server defaults to 'quantized' at boot, looks for model_quantized.onnx
# under ERGO_NLI_LOCAL_ONLY=1, fails, and the container never goes healthy.
# (These ARGs default to the standard model/variant, so the default build is
# unchanged.)
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    ERGO_NLI_LOCAL_ONLY=1 \
    ERGO_NLI_MODEL="${ERGO_NLI_MODEL}" \
    ERGO_NLI_VARIANT="${ERGO_NLI_VARIANT}" \
    ERGO_EMBED_MODEL="${ERGO_EMBED_MODEL}"

VOLUME ["/data"]
EXPOSE 8000

# --- Non-root runtime (finding #7) --------------------------------------
# Everything above (apt-get, pip install, model bakes, tools copy) still runs
# as root, which the bakes need (writing under /models, installing packages).
# Only the actual server process drops privilege: create an unprivileged
# user/group here, AFTER the bakes, so the bake layers stay untouched, and
# chown just the paths written at runtime (/data volume + fastembed's HF
# cache dir under /models/fastembed, which fastembed re-touches on load even
# though the weights themselves were baked read-only). The baked model dirs
# (HF_HOME, ERGO_NLI_CACHE) only need to be readable, which they already are
# for any non-root user under the default umask.
RUN groupadd --gid 1000 ergo \
 && useradd --uid 1000 --gid ergo --no-create-home --shell /usr/sbin/nologin ergo \
 && mkdir -p /data \
 && chown -R ergo:ergo /data /models/fastembed

# HTTP API for the contradiction engine.
# Single-threaded stdlib server wrapping the SQLite ContradictionStore.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)" || exit 1
USER ergo
ENTRYPOINT ["python", "-m", "ergo.contradiction.server"]

# --- Honest size note ------------------------------------------------------
# Expected layers, approximate:
#   python:3.12-slim base       ~150 MB
#   pip deps (onnxruntime etc.) ~350 MB
#   fastembed model             ~130 MB
#   NLI model (quantized)       ~190 MB
#   app code                    ~  5 MB
#   ----------------------------------
#   TOTAL                       ~825 MB  (compressed: ~400 MB)
#
# --build-arg ERGO_NLI_VARIANT=int8 (~80 MB) shaves another ~110 MB.
# Switching ERGO_LLM_PROVIDER=ollama at runtime requires a separate Ollama
# server reachable from this container; the image itself does not include one.
