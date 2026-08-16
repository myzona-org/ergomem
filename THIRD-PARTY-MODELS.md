# Third-Party Models

This project's Docker image bakes two model checkpoints at build time (see `Dockerfile`
and `docker/boot-test.py`). Neither is training data or code this project wrote — both
are pulled from the Hugging Face Hub and shipped as binary weights inside the built
image. This file records what's actually baked, under what license, and — for the one
case where that isn't settled — says so plainly instead of asserting an answer.

**Scope note:** the built Docker image is published publicly
(`ghcr.io/myzona-org/ergomem`), with the NLI judge's license question below still
open — a deliberate call, not an oversight. This file doesn't gate that decision;
it exists so anyone deciding whether to redistribute this image themselves (fork
it, mirror it, rebuild it) has the honest answer to check first, including the
exact place where the honest answer is "unresolved, here's what's known." It also
doesn't gate publishing this source repository itself — those are two separate
steps.

## 1. Embedder — `BAAI/bge-small-en-v1.5`

- **License: MIT.** Verified on the model card (`license:mit` tag on the Hub) —
  commercial use, modification, and redistribution are all permitted.
- **What's actually baked is not that repo directly.** This project's embedder
  (`ergo/embedders.py`) calls FastEmbed with model name `BAAI/bge-small-en-v1.5`;
  FastEmbed's own model registry resolves that name to a converted ONNX mirror,
  `Qdrant/bge-small-en-v1.5-onnx-Q`, and downloads `model_optimized.onnx` from it.
  FastEmbed's registry records that mirror's license as MIT (inherited from the
  original weights); the mirror repo's own Hub tag currently says Apache-2.0. Both
  permit commercial redistribution, so the discrepancy doesn't change the answer here,
  but it's recorded rather than silently picking one.
- **As observed 2026-08-12** (Hugging Face Hub API, `?blobs=true`):
  - Upstream `BAAI/bge-small-en-v1.5`, revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`.
  - Mirror `Qdrant/bge-small-en-v1.5-onnx-Q`, revision
    `52398278842ec682c6f32300af41344b1c0b0bb2`; `model_optimized.onnx` sha256
    `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431`.
- **Not currently pinned in the build.** The Dockerfile passes the model *name*, not a
  revision, so a rebuild pulls whatever FastEmbed's registry resolves to at that moment.
  The values above are the actual artifact as of this writing, not a promise that a
  future rebuild reproduces them exactly.

## 2. NLI judge — `Xenova/nli-deberta-v3-base`

- **License: UNRESOLVED.** Not MIT, not Apache-2.0, not "presumed open" — genuinely
  unsettled, and this file does not resolve it, only records it:
  - The Hugging Face model card for `Xenova/nli-deberta-v3-base` — the exact repo this
    project's Dockerfile pulls from (see `ergo/contradiction/nli.py` and `Dockerfile`) —
    **declares no license at all** (empty license tag on the Hub as of this writing).
  - The model it's an ONNX export of, `cross-encoder/nli-deberta-v3-base`, is
    **Apache-2.0**. That covers the upstream cross-encoder checkpoint; it does not by
    itself establish the terms the ONNX mirror is redistributed under, since that
    repo's own listing asserts none.
  - The base architecture, `microsoft/deberta-v3-base`, is **MIT**.
  - Training data is **SNLI (CC BY-SA 4.0)** and MultiNLI. A share-alike data license
    doesn't automatically extend to weights trained on that data, but it's part of the
    honest provenance chain and belongs here rather than being left out.
  - **As observed 2026-08-12:** `Xenova/nli-deberta-v3-base` revision
    `80a99030ce45a69a39ea2a6f50756d03859ff521`; the file this project's default
    `ERGO_NLI_VARIANT=quantized` actually downloads, `onnx/model_quantized.onnx`, sha256
    `9d83d9d0ba179cab8b78b942dcc42756da7712b2bbe101a2a4c25fdfa7968e2f`. Upstream
    `cross-encoder/nli-deberta-v3-base` revision `6c749ce3425cd33b46d187e45b92bbf96ee12ec7`.
  - **Practical consequence:** this project publishes the built Docker image
    containing this exact checkpoint anyway, with this license question
    **disclosed rather than resolved** — a deliberate call, not a claim that the
    question is settled. Until this is either clarified upstream (an explicit
    license from the mirror's maintainer, or a re-conversion done by this project
    directly from the Apache-2.0 upstream) or otherwise confirmed, anyone
    redistributing this image further, or relying on it commercially, should treat
    the NLI judge's license as unresolved and make their own call using the chain
    above. Running the engine from source, or building your own image after
    re-converting the model yourself, carries the exact same open question — it
    is not somehow safer, just less visible.
  - ⚠️ If this project ever re-converts the model from the Apache-2.0 upstream to settle
    this, that changes the model's bytes. The pinned NLI runtime's numerics are known to
    be sensitive to exactly that kind of change, so any re-conversion needs the judge's
    evaluation harness re-run against the new checkpoint before it ships — not a
    drop-in swap.

## Reproducing these values

```
curl -s "https://huggingface.co/api/models/<repo>?blobs=true"
```

`sha` in the response is the revision; each file's `lfs.sha256` field (when present) is
that file's content hash. `<repo>` above is any of the four repo ids named in this file.
