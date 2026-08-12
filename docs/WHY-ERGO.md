# Why Ergo — and why not just ChromaDB + a good embedder?

*Written 2026-07-02, after the dogfood gate produced real incidents. Every example below is quoted from the live store or the repo history — nothing is invented.*

## The problem: agents don't just forget — they remember *wrong*

A vector store gives an agent recall. It does not give it consistency. When a
policy, path, or config changes, the old claim stays in the store, still
embedded, still retrievable, still confident. The next session recalls it and
acts on it. The failure mode isn't amnesia — it's **stale memory poisoning new
decisions**, silently, with no signal that anything is off.

Retrieval alone cannot fix this, and our own baseline eval proves why: on the
realistic contradiction arm (`evals/embedding_baseline/`), the embedder pulls a
stored claim's *contradiction* into the top-10 neighborhood **87% of the time**
(57% at top-1). To a vector store, a contradiction is just a very similar
document. The retriever finds it; nothing judges it.

## What Ergo does differently: verify at write time

Ergo is the judge bolted onto the retriever. Every guarded write
(`/remember`, `/learn`, `/supersede`) runs `normalize → structural compare →
gated NLI → tier decision` against the active claims in scope *before* storing.
A hard conflict comes back **HTTP 409 with the prior claim and its reason** —
the caller must resolve it: cancel, supersede with a new reason, or force an
exception on the record. Softer conflicts store but warn. Every claim carries a
reason; `why()` refuses to answer from reason-less facts; supersede chains
preserve *why the decision changed*.

The write that comes back 409 is the product. It converts "silently overwrite a
settled decision" into "confront the prior decision, on the record."

Measured, not asserted (`docs/EVALS.md`): on 105 labeled real-prose pairs the
judge runs at **P≈0.80–0.86 / R≈0.81 with 0 false hard-blocks** (and 0 false
blocks across every deploy smoke since). The full offline test suite backs this up;
the Docker build fails if the image touches the network at boot.

## The real incident (dogfood)

Ergo's first consumer is its own development — the coding agents building Ergo
use the live instance as their memory. Two incidents from that log:

**The supersede that kept the runbook true.** On 2026-06-27 an agent recorded:

> "Ergo image releases must go through **tools/**ergo-release.sh, not a
> hand-run sequence of docker commands."

Two days later a commit moved the script into the deploy-ops directory. When the agent
recorded the new convention, the write-time gate matched it against the stale
claim — it did not become a second, contradicting "truth" sitting next to the
first. It was resolved as a supersede (2026-06-29),
with the *why it moved* preserved in the new reason
("it's host-side orchestration that can never run in-container"). The avoided
mistake: a future session recalling "how do I release" and getting the dead
`tools/` path — or concluding the convention was contested and hand-running
`docker` commands, exactly what the claim exists to prevent.

**The bug only dogfooding could catch.** Quoted from the session log where it
was diagnosed and fixed (2026-06-29):

> "Dogfood caught a real defect synthetic tests missed. Claude Code recording
> dev decisions into project=\"ergo\" hit a persistent 500 on
> ergo_remember/ergo_supersede. Root cause (now fixed): the LLM
> normalizer crashed on non-JSON refusals … It only manifests on an
> ACCUMULATED real store (a project that already holds ergo_ingest'd docs),
> which is precisely the failure mode synthetic single-claim testing can't
> reach and dogfooding does."

Honesty about the distribution: conflict events have been rare relative to
claim volume in the store's own usage, and overwhelmingly resolved as
supersedes rather than hard blocks — the occasional block/warn events seen so
far were deploy/smoke probes, not organic use. Contradictions are **rare
events with outsized cost**, which is the point: a guard that fires seldom but
truthfully beats a feed of noisy similarity alerts.

## Why not just X?

| Alternative | What it does NOT do (that Ergo does) |
|---|---|
| **ChromaDB** (+ embedder, "3 lines") | Retrieval only. No write-time judgment — a contradiction is stored as one more similar document (and our baseline shows it *will* co-retrieve, unjudged, 87% at top-10). No reasons, no supersede semantics, no "why did this change" history. |
| **Pinecone** | Same judgment gap as Chroma, plus it's a managed cloud service — a non-starter for an air-gapped deployment target. You cannot ship Pinecone into an environment with no outbound network. |
| **pgvector** | Brings a Postgres server dependency for the same unjudged-similarity semantics. Ergo's deploy artifact is one container + one SQLite file (sqlite-vec inside it); restore = copy one file. |
| **Plain SQLite + embedder** | This *is* Ergo's storage layer — minus the entire point: the normalizer, structural comparator, gated NLI judge, 409 contract, reasoned `why()`, supersede chains, `/diagnose`, and the eval suites that calibrate the judge. The store is not the moat. |
| **`because`** (Ergo's own predecessor, retired 2026-06-27) | Had reasons, supersede, and a cosine-threshold write-time *nudge* — but its embeddings/judge called out to a network API (breaks the air-gap boot guarantee), the guard advised rather than enforced a 409 contract, and it had no labeled evals, so its precision was unknown. Ergo bakes the models into the image, enforces the gate at the HTTP layer for every client, and ships the numbers. |

## Where Ergo is overkill — use commodity RAG instead

- **Reference retrieval.** Docs, runbooks, READMEs, meeting notes — anything
  you want *found*, not *defended*. Ergo itself routes this through `/ingest`,
  which deliberately skips the contradiction gate and is ~30× faster. If all
  your content is reference, you don't need Ergo at all — Chroma is fine.
- **High-volume, low-stakes memory.** Chat history, scratch notes, transient
  status. A guarded write costs one LLM normalizer call; paying that tax on
  content nobody will ever contradict is waste.
- **High-throughput multi-writer workloads.** Every Ergo store — the legacy
  pooled DB, or (since multi-tenancy) each tenant's own silo file — is
  single-writer SQLite by design. Multi-tenancy buys N independent
  non-contending writers, not concurrent writers *within* one tenant. If a
  single tenant needs real write concurrency at scale, this is the wrong shape
  for that tenant.
- **Claims that churn constantly.** If "the truth" changes hourly, supersede
  chains become noise. Ergo is for *settled decisions* with a shelf life.
- **Free-form poetry.** The normalizer wants declarative prose
  (subject + stance + object). Writes it can't parse fail open — stored but
  invisible to future contradiction checks — so unparseable content gets
  storage, not protection.

## The honest positioning

Ergo is not a universal memory firewall. It is **selective safety rails for
high-stakes, slow-changing decision domains** — conventions, configs, policies,
architecture choices, ops runbook invariants — where a stale claim silently
reversing a settled decision costs real money or real outages. The moat is not
the vector store (that part genuinely is 3 lines). The moat is the judgment
layer, the labeled evals that keep it honest, the air-gap single-file
deployability, and the discipline around it — including a named owner,
AND-gated continue thresholds, and a documented one-command sunset if the
flags stop earning their keep. A memory product that documents how to kill
itself is one you can trust to tell you the truth about what it caught.
