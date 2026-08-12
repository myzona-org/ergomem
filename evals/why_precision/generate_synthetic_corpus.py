#!/usr/bin/env python3
"""Regenerate corpus_synthetic.jsonl from synthetic decision-memory statements.

The real snapshot was a dump of the live prod store: real infra runbooks
(container recreate commands with a real env-file path, a real Cloudflare
tunnel id), live Stripe price ids, a real promo code, and customer email
addresses. None of that belongs in a public repo just because it happens to
sit inside a labeled eval set.

This generator invents a fictional decision-memory subject instead: a
feature-flag/decision-audit engine called "Meridian" (project `ergo`) plus
its operator's deployment notes (project `infra`) -- same shape as the real
corpus (claim + optional reason, a mix of engineering/product/infra topics,
supersede chains where a later decision explicitly revises an earlier one),
zero real statements. Every row is entirely made up; any resemblance to a
real product's internal decisions is coincidental structural mimicry, not
disguised real content.

Run:
    python evals/why_precision/generate_synthetic_corpus.py \
        > evals/why_precision/corpus_synthetic.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys

# Each cluster is a topic; each entry is (statement, reason_or_None).
# Clusters intentionally revisit the same mechanism from multiple angles
# (a decision, then a follow-up revision, then a "supersedes" correction) --
# that's what creates the real corpus's near-neighbour / vocabulary-overlap
# structure in embedding space, not literal duplication.

ERGO_CLUSTERS: list[tuple[str, list[tuple[str, str | None]]]] = [
    ("dedup-gate", [
        (
            "Meridian's flag-conflict gate counts negated-verb pairs (skip/omit/avoid/disable "
            "+ inflections) in the same polarity bucket as a bare NOT, so 'skip the rollout' and "
            "'do not roll out' are treated as the same negative stance.",
            "Pre-fix, 'Skip the canary stage' vs 'Run the canary stage' produced no detected "
            "polarity flip -- 'skip' isn't a hand-coded negator and the object isn't an antonym "
            "pair -- so the gate silently let a real conflict through. Reproduced on the flag "
            "'canary-required-for-prod' with two same-day writes from different reviewers.",
        ),
        (
            "The polarity bucket for omission verbs must also cover 'defer' and 'postpone', not "
            "just 'skip'/'omit'/'avoid'/'disable'.",
            "A follow-up audit found 'defer the migration' vs 'run the migration now' scored as "
            "unrelated (LOW) instead of a real stance conflict -- same root cause as the original "
            "skip/omit gap, just a verb the first fix's word list didn't enumerate.",
        ),
        (
            "Meridian's conflict judge must never trust a raw NLI contradiction score above 0.9 "
            "without a subject-overlap and polarity-agreement gate in front of it.",
            "The quantized cross-encoder over-fires on unrelated cross-topic pairs (>0.9) purely "
            "from surface phrasing similarity; a characterization test pins the two known "
            "false-fire pairs so a runtime bump can't silently reintroduce them.",
        ),
        ("Meridian's structural comparator treats same-stance-different-scope as `coexist`, never `entailment`.", None),
        ("A free-text exception clause on a flag rule caps the conflict verdict at LOW unless the exception is structurally represented (its own scope object), never freeform prose.", None),
    ]),
    ("embed-search", [
        (
            "Meridian's `recall()` grows its KNN search budget geometrically (starting at "
            "max(limit*8, 40), doubling on each pass) until it has `limit` in-scope active rows "
            "or has scanned the whole vector index.",
            "Audit finding: the vector index is global across every org/environment and rows are "
            "never deleted on revoke/supersede, so a fixed KNN budget could be entirely consumed "
            "by out-of-scope or inactive rows the post-filter discards, silently starving recall "
            "on an old, heavily-revised project.",
        ),
        (
            "Embedding dimension is fixed at 384 the moment the vector table is created; changing "
            "embedding providers is a re-embed migration, never an in-place dimension change.",
            None,
        ),
        (
            "Meridian's `/why` route must search reasoned entries only -- a reason-less flag "
            "record can never win a rationale query.",
            "A reason-less row can't answer 'why', by construction: there's no text to cite. "
            "Searching it anyway just adds retrieval noise that can outscore the row that "
            "actually explains the decision.",
        ),
        (
            "An empty result from Meridian's `/search` or `/why` never implies the environment "
            "holds no flags, so no route may report an environment as 'empty' on the basis of an "
            "empty result set.",
            "`/why` calling `recall(reasoned_only=True)` and getting zero hits back only tells you "
            "there are no REASONED rows above the relevance floor -- there could be dozens of "
            "reason-less flags still active. Conflating the two produced a false 'no flags "
            "configured' banner in the console for a project that had 40 active flags, none "
            "reasoned.",
        ),
        (
            "Meridian's relevance floor for `/why` is 0.5; raising it to suppress low-confidence "
            "incorrect answers would need to exceed 0.88 and would also suppress a large share "
            "of genuinely correct ones.",
            "An internal calibration pass on labeled samples showed correct and incorrect hits "
            "clustering closely together in score, roughly 0.60-0.75 for both, so no single "
            "global cutoff cleanly separated the two without also losing good answers -- the fix "
            "had to look at what was matching, not just how confidently.",
        ),
        (
            "Meridian's keyword rescue term (`kw`) is token-overlap fraction, weighted 0.3 against "
            "vector similarity's 0.7, specifically to rescue exact-token cases vector search alone "
            "misses.",
            None,
        ),
        (
            "The chunker used for the operator runbook importer can split an attribute from its "
            "keyword across a chunk boundary, which measurably hurts top-1 recall on short "
            "attribute/value rows.",
            "In a targeted replay of short key/value rows, a port number landed in the chunk "
            "AFTER the service name labeling it, so a query for the service name alone missed "
            "the port entirely -- top-1 recall on that replay only recovered once the window "
            "widened to recall@3. Table-aware chunking is the fix; not implemented yet.",
        ),
    ]),
    ("http-api", [
        (
            "Meridian's `POST /flags/:id/resolve` route now maps a residual-conflict result to "
            "HTTP 409, mirroring `/flags` creation, instead of returning 200 with a 'resolved' "
            "body on an unresolved conflict.",
            "The earlier asymmetry meant a client branching on status code alone could silently "
            "ignore a real conflict, since only the body (not the status) signaled it. The CLI "
            "adapter was already coded to expect 409 here, so it degraded quietly for months.",
        ),
        (
            "Every guarded write route accepts an audited `force_exception` override, except "
            "`/flags/:id/resolve`, which has no override at all -- its 409 is unconditional.",
            "Resolve is the terminal step of a review workflow; letting a client force through a "
            "real conflict at that exact step defeats the point of having a review workflow in "
            "the first place.",
        ),
        ("A 400 (bad request) and a 401 (missing/invalid token) are evaluated BEFORE the monthly write-meter increments, so a malformed request never burns a metered write.", None),
        (
            "A guarded write must be metered only once it is known to actually run the conflict "
            "gate: `/flags/:id/resolve` pre-checks that `existing_id` resolves to an active flag "
            "before incrementing the monthly counter.",
            "Before the fix, the meter incremented atomically before the existence preflight, so "
            "a client typo in `existing_id` burned a monthly write unit for a request that never "
            "ran the gate at all -- contradicting the documented model that only gated attempts "
            "count.",
        ),
        (
            "`GET /export` is open to every plan tier, not gated behind Pro/Team.",
            "Export is a data-portability guarantee, not a premium feature; gating it behind a "
            "paid tier would mean a free-tier operator can write flags they can't ever get back "
            "out in bulk.",
        ),
        (
            "`GET /audit` is scoped to the caller's own org's writes and conflict events; there is "
            "no cross-org audit view at any plan tier.",
            None,
        ),
        (
            "Meridian's bearer-token check must use a constant-time comparison, never Python's "
            "plain string `==`.",
            "String equality short-circuits on the first mismatched byte, which is a textbook "
            "timing side-channel for a bearer token; `hmac.compare_digest` closes it for the cost "
            "of one import.",
        ),
        (
            "`DEFAULT_ENVIRONMENT` is optional on every route above the flag-resolution layer, "
            "and its absence must produce a 400 with an explicit acceptance-criteria message, not "
            "a silent fallback.",
            None,
        ),
    ]),
    ("provider-failclosed", [
        (
            "Meridian's LLM-backed flag-description normalizer must FAIL CLOSED when the "
            "provider raises a connection error, timeout, 429, 5xx, or auth rejection -- refuse "
            "the write with a retryable 503, never silently skip the conflict check.",
            "The earlier behavior caught every provider exception and fell back to an "
            "all-unknown normalization, which stored the write with NO conflict check whenever "
            "the provider was merely unreachable -- a silent firewall bypass, not a graceful "
            "degrade.",
        ),
        (
            "A PERMANENT provider client-error status (400 bad request, 404 unknown model, 422) "
            "must fail CLOSED too, mapped to a non-retryable 502 -- distinct from the retryable "
            "503 class.",
            "Reproduced directly: a caller passing a bad model name got a 404 from the provider, "
            "which fell through to the SAME fail-open path as a merely-malformed reply, storing "
            "a write with no conflict check at all. A permanent rejection and 'the provider "
            "answered something we couldn't parse' are not the same failure class.",
        ),
        (
            "Malformed model OUTPUT (2xx reply, no parseable JSON, or a refusal in prose) still "
            "degrades to an all-unknown normalization and fails OPEN -- distinct from the "
            "provider-error classes above, which fail closed.",
            "The provider answered successfully; it just didn't answer usefully. Refusing every "
            "write whenever the LLM declines to structure a description would make the reference "
            "text itself untrustworthy input, which isn't the threat model here.",
        ),
        (
            "The normalizer's cache must use a unique temp file per write (`tempfile.mkstemp`), "
            "never a fixed `cache_path + '.tmp'`.",
            "Two threads racing a cache miss could both target the same tmp path; the loser's "
            "atomic rename then raised `FileNotFoundError` because the winner had already moved "
            "it. Reproduced with 8 concurrent threads pre-fix.",
        ),
        ("Normalize happens OUTSIDE the store's write lock; only the DB read/write phase is scoped to the lock.", "Holding the lock across a 1-3s provider round-trip would head-of-line-block every other concurrent write for the duration of one slow LLM call."),
    ]),
    ("multitenancy", [
        (
            "Meridian's multi-environment mode uses silo isolation: one SQLite file per "
            "environment, keyed by an opaque control-plane id, rather than a single pooled file "
            "scoped by a WHERE clause.",
            "Silo fits the single-writer SQLite engine: each environment becomes its own "
            "independent writer with no cross-environment lock contention, and isolation is by "
            "construction rather than by a WHERE clause a future migration could drop.",
        ),
        (
            "The bounded threaded HTTP server (daemon threads, capped worker count) replaced the "
            "single-threaded server once concurrent multi-environment write load became routine.",
            "The single-threaded server's ~1-3s LLM normalizer call per guarded write stalled "
            "every other environment's request, including `/health` -- worth fixing before "
            "production load, not after an incident.",
        ),
        (
            "A lease on an environment's own store MUST be released via a `finally` block once "
            "the request completes, so idle eviction or a concurrent environment delete can "
            "never close a connection still in use.",
            None,
        ),
        (
            "`registry.evict()` on a store that never drains must raise a `TimeoutError`, which "
            "the route maps to a clean 409 -- never force-close a store still leased by an "
            "in-flight request.",
            None,
        ),
        (
            "A request against an environment mid-delete must see `registry.get()` raise "
            "`LookupError` rather than hand out a lease on an entry that's draining toward close.",
            "That's a temporary condition (the environment is being torn down), not a broken "
            "store -- the route maps it to 503, not 500.",
        ),
        (
            "Meridian's `role` field on an environment token is audit-only metadata at the engine "
            "layer; it grants no authorization on its own.",
            "grep across the route handlers shows zero authorization checks keyed on `role` "
            "anywhere -- every admin-only route is gated by the superadmin token alone. A "
            "self-hosted operator reading only the role table could reasonably assume otherwise, "
            "which is a doc gap, not an engine bug.",
        ),
    ]),
    ("deploy-release", [
        (
            "Meridian's release script takes a backup, runs the full test suite, then builds and "
            "redeploys, gated by a health check with automatic rollback on failure.",
            None,
        ),
        (
            "The release script's health-gate rollback only covers the case where the new "
            "container fails its health check; it does NOT cover `docker run` itself failing "
            "(port already bound, env-file missing, daemon hiccup).",
            "Under `set -e` with no ERR trap, the old container is stopped and removed BEFORE the "
            "new one starts -- if `docker run` itself fails, the script dies with the old "
            "container gone and the new one never started, and the rollback tag sits unused.",
        ),
        (
            "A same-minute retry after a failed release must not collide on the failed "
            "container's rename target.",
            "Timestamp resolution was minutes, not seconds; a same-minute retry's rename target "
            "collided with the previous failed container's name, and the fallback cleanup deleted "
            "the newest failed container -- the exact one an operator would want to debug.",
        ),
        (
            "Claim-count verification during a release samples the count before and after and "
            "demands exact equality against a live, still-writable API.",
            "Any concurrent write during the build window is a false 'DATA RISK' alarm; the check "
            "is deliberately strict because a GENUINE mismatch (wrong volume mounted, count "
            "reset to zero) is exactly the failure mode a release gate exists to catch, even at "
            "the cost of occasional false alarms.",
        ),
        (
            "The required CI test job does not build the Docker image or run the no-network boot "
            "test, so an image-only regression can merge green and first surface mid-release.",
            "Trade-off accepted, not fixed: a release gate that runs the full image build on "
            "every CI run would slow every PR down for a failure mode the release script's own "
            "gate already contains the blast radius of.",
        ),
        ("Rollback image tags are pruned to the newest two, and pruning happens only after a health-gate rollback succeeds, never before.", None),
    ]),
    ("testing", [
        (
            "Meridian's HTTP-concurrency tests must assert on a RELATIVE bound (an environment's "
            "measured non-blocking write time vs another environment's blocking one), never an "
            "absolute wall-clock ceiling.",
            "The absolute ceiling flaked on a starved 2-vCPU CI runner: `time.sleep` releases the "
            "GIL but the environment's post-sleep CPU work does not, so an absolute bound assumed "
            "dedicated cores the CI runner didn't actually have.",
        ),
        (
            "A shared test-server-boot helper now lives in one conftest module; every fixture "
            "that boots a background HTTP server for a test delegates to it instead of "
            "copy-pasting the thread-start-and-wait mechanic.",
            "The same 'boot on a background thread, wait for readiness' mechanic was copy-pasted "
            "across a dozen test files with a too-tight timeout and no way to distinguish a real "
            "boot crash from a momentarily oversubscribed CI runner. One file's fix didn't fix "
            "the other eleven, because it was never one bug -- it was a dozen copies of the same "
            "one.",
        ),
        (
            "A whitespace-only flag description passes truthiness validation, so it must be "
            "caught before it reaches the database CHECK constraint, not after.",
            "Letting the CHECK constraint reject it produced a 500 with raw schema-constraint "
            "text in the response body instead of a clean 400 -- an internal detail leaking into "
            "a client-facing error.",
        ),
        (
            "Any provider-scoped cache key must be verified distinct across all supported "
            "providers, not just pairwise between the two most-used ones.",
            "A pairwise-only check passed while a third provider's key derivation still collided "
            "with one of the first two under a specific input -- only an all-pairs assertion "
            "would have caught it.",
        ),
        ("A degrade-to-unknown-norm path must be exercised end-to-end with a refusing fake provider, not just unit-tested on the parser function in isolation.", None),
    ]),
    ("dependency-pins", [
        (
            "Pin the conflict-judge runtime and its tokenizer library exactly; never unpin "
            "without re-running the labeled precision eval inside the built container first.",
            "The quantized judge model was found to shift its verdict on a fixed pair of near-"
            "duplicate sentences between two runtime/tokenizer combinations, flipping a case from "
            "a clear conflict to a clear non-conflict. Unpinned floors let every image build pull "
            "a different runtime than the eval was calibrated against.",
        ),
        (
            "The MCP-style adapter's client library is pinned with `==`, not a floor, for the "
            "same reproducibility reason the conflict-judge runtime is pinned.",
            "A minor version bump in the adapter SDK can change tool-schema serialization or "
            "transport behavior; pinning means every deployment runs the exact combination the "
            "adapter's own test suite verified.",
        ),
        ("Bump either pin deliberately, in its own change, with the full adapter test suite re-run -- never as a side effect of an unrelated dependency update.", None),
    ]),
    ("positioning", [
        (
            "Meridian must be positioned as guardrails for high-stakes, slow-changing "
            "configuration -- not a universal change-management platform.",
            "An internal realism eval showed genuine conflicts co-occur with unrelated changes "
            "constantly (most flag flips are routine, not policy-violating), so the conflict "
            "gate earns its keep only on the narrow slice of high-stakes flags, and positioning "
            "should say so rather than imply universal coverage.",
        ),
        (
            "The conflict-detection feature is assessed as unproven on organic (non-test) usage, "
            "because no real operator write has yet triggered the gate outside of synthetic "
            "smoke tests.",
            "Lifetime detector record at time of assessment: two firings, both synthetic "
            "smoke-test plants, precision 1.00 but n=2 -- not enough organic signal to claim the "
            "gate demonstrably prevents real incidents yet, however sound the mechanism looks in "
            "isolation.",
        ),
        (
            "Retraction (removing a wrong-at-birth flag record) and superseding (recording that a "
            "decision changed) are different operations and must never share a code path.",
            "Retract means the record should never have existed (typo, test junk, mis-scoped "
            "write) -- no new row, no why-chain link, required audit reason, and it cannot itself "
            "create a conflict. Supersede means the decision genuinely changed and the OLD "
            "decision's reasoning stays visible in the chain. Conflating them would either lose "
            "real history or pollute it with noise.",
        ),
    ]),
]

INFRA_CLUSTERS: list[tuple[str, list[tuple[str, str | None]]]] = [
    ("backups", [
        (
            "Meridian's operator backups run via a daily scheduled job that calls the SQLite "
            "online-backup API inside the running container and copies each backup off-host "
            "afterward.",
            "The deployment previously had no off-host backups configured; the data lives in a "
            "named Docker volume, so an in-volume backup alone does not survive a volume or disk "
            "loss -- hence the separate off-host copy step.",
        ),
        (
            "The container ships with no `sqlite3` CLI, so any backup or maintenance recipe must "
            "use the SQLite online-backup API (`conn.backup(dst)`) or a Python `VACUUM INTO` "
            "one-liner, never a shelled-out `sqlite3` command.",
            "Documentation once claimed a `docker exec ... sqlite3 ... VACUUM INTO` recipe "
            "matched what the nightly job actually ran; probing the live container confirmed "
            "`sqlite3` is absent and the job in fact uses the online-backup API, which needs "
            "neither a CLI nor a bare-connection assumption about virtual-table support.",
        ),
        (
            "Recreating the deployment container must always pass the full canonical env-file, "
            "never a hand-assembled subset of variables.",
            "Container env is fixed at create time, so every upgrade does a stop-and-recreate and "
            "must re-pass every variable; hand-crafting the list risks silently dropping a "
            "model-cache path or a backup-path variable and losing offline model loading or a "
            "previously-cleared health warning.",
        ),
        (
            "A rollback image tag is created before every mutating release step, and pruning of "
            "old rollback tags happens only after a successful health gate.",
            None,
        ),
        ("The pre-deploy off-host backup happens before any container mutation, never after.", "A backup taken after the mutation already happened can't roll back the mutation it was meant to protect against."),
    ]),
    ("host-provisioning", [
        (
            "Code changes to the deployment tooling are made in separate, disposable worktrees, "
            "never directly on the main checkout.",
            "The main checkout stays on a clean default branch as the orchestrator's read-only "
            "vantage point; each fix or feature gets an independent worktree so an in-progress "
            "change never blocks a concurrent one from being reviewed.",
        ),
        (
            "The CI runner used for the release-gate test job is a persistent, self-hosted "
            "machine shared across jobs, not a fresh GitHub-hosted runner per run.",
            "A persistent runner lets the quantized-model cache survive between jobs, which "
            "matters for cold-start time, but it also means a job's state can leak into the next "
            "one if a test doesn't clean up after itself -- worth knowing before debugging a "
            "'only fails in CI' flake.",
        ),
        (
            "A throwaway container used to evaluate a new deploy platform must be destroyed once "
            "the evaluation is reviewed, never left running as a permanent fixture.",
            None,
        ),
        (
            "Every host-side script that mutates the live deployment must support a `--dry-run` "
            "flag that reports what it would do without mutating anything.",
            "A dry-run flag is what let an unattended nightly job safely report deployability "
            "(tests green, backup-baseline present) on every merge to the default branch without "
            "ever taking a live cutover action -- full unattended cutover stays behind an "
            "explicit manual gate.",
        ),
    ]),
    ("networking", [
        (
            "The engine's default bind address is 0.0.0.0 inside the container; the reverse "
            "proxy in front of it is what actually restricts what's publicly reachable.",
            None,
        ),
        (
            "A public bind with no bearer token configured must fail closed at boot, not start "
            "open with a logged warning.",
            "An operator who forgets to set the token on a publicly-reachable deployment "
            "previously got a running-but-wide-open API and a warning easy to miss in boot logs; "
            "failing the boot entirely makes the misconfiguration impossible to accidentally "
            "ship.",
        ),
        (
            "The tunnel fronting the public API terminates at exactly two hostnames, both "
            "pointing at the same backend service.",
            "The product has one canonical domain and one legacy alias that predates the rename; "
            "both need to keep working, so the tunnel config and the server's own allowed-host "
            "check both list both names explicitly rather than assuming a single canonical host.",
        ),
        ("A streamable-HTTP adapter transport must default to loopback-only; a proxy-fronted deployment that needs it to accept external connections must opt in explicitly with a host flag.", None),
    ]),
    ("monitoring", [
        (
            "The `/health` route never probes the LLM normalizer, so a normalizer outage can "
            "leave guarded writes 503-ing while `/health` still reports green.",
            "A regional provider outage took the normalizer down for several hours; `/health` "
            "stayed green throughout because it only checks the store and embedder, not "
            "provider reachability, so the on-call operator had no automated signal and found "
            "out from a user report instead.",
        ),
        (
            "A `/metrics` route exposing request counts and latency histograms is operator-only, "
            "gated the same way admin routes are.",
            None,
        ),
        (
            "The route label recorded in metrics is normalized against a fixed known-routes set "
            "before it ever reaches the metrics registry.",
            "The raw request path is attacker-controlled and every unmatched path already falls "
            "through to a 404, so recording it verbatim as a metrics label would let an attacker "
            "generate unbounded label cardinality just by requesting random paths.",
        ),
        (
            "A cached normalizer-health check reports the wall-clock time of the last REAL probe, "
            "never advancing that timestamp on a cache hit.",
            "Reporting 'now' on a cached hit would tell a monitor the write path was just "
            "verified when the underlying probe may be up to the cache TTL stale -- the `cached` "
            "flag alone doesn't convey that staleness on its own.",
        ),
    ]),
    ("staging-eval", [
        (
            "A throwaway LXC container was used to evaluate an alternative deploy platform "
            "before deciding whether to adopt it for the main deployment.",
            "Testing against a disposable box first, exactly like every other infra evaluation, "
            "avoids risking the live deployment on a platform that might not pan out.",
        ),
        (
            "A locally-hosted LLM was evaluated as a normalizer backend and found unsuitable for "
            "sustained production load, despite working fine on cold/warm single calls.",
            "The shared GPU host saturated under sustained request volume -- warm-up performance "
            "was fine, but the host wedged after a few hundred consecutive calls while keeping "
            "the model loaded and admin endpoints alive, a known queueing pathology under this "
            "workload shape.",
        ),
        (
            "A remote LLM proxy is production-shaped in a way a directly-hosted local model, "
            "sharing a GPU with other workloads, is not.",
            "A same-day head-to-head comparison: the remote proxy sustained full throughput with "
            "zero failures at the same per-call latency, while the local shared-GPU host wedged "
            "partway through an equivalent run.",
        ),
    ]),
]


def _rows(clusters: list[tuple[str, list[tuple[str, str | None]]]], project: str) -> list[dict]:
    out = []
    for _topic, entries in clusters:
        for statement, reason in entries:
            out.append({"statement": statement, "reason": reason, "project": project})
    return out


def build_corpus() -> list[dict]:
    rows = _rows(ERGO_CLUSTERS, "ergo") + _rows(INFRA_CLUSTERS, "infra")
    return rows


# --- paired query set -------------------------------------------------------
#
# (query, phrasing, corpus_index or None, rationale). The expected statement is
# looked up from the corpus BY INDEX rather than retyped, because evaluate.py's
# classify() compares the returned claim to expected_statement with `==` -- a
# retyped copy that drifts by one character would score as a silent WRONG.
#
# Indices are chosen to include the same traps the real set probes: clusters
# where two rows differ only in scope or in which verbs they enumerate (0 vs 1,
# 20 vs 21 vs 22, 31 vs 32 vs 36), so the nearest neighbour in embedding space
# is not automatically the right answer.
DATASET: list[tuple[str, str, int | None, str]] = [
    ("why does the conflict gate treat 'skip' as a negation?", "well_formed", 0,
     "base decision of the dedup-gate cluster; row 1 is a near neighbour that extends it"),
    ("which omission verbs were added to the polarity bucket after the first fix?", "well_formed", 1,
     "the follow-up revision -- must not collapse onto row 0, its own antecedent"),
    ("raw NLI score above 0.9 trust", "terse_keyword", 2,
     "terse keyword phrasing over a distinctive numeric threshold"),
    ("why is the KNN search budget grown geometrically instead of fixed?", "well_formed", 5,
     "retrieval-internals cluster"),
    ("embedding dim change", "terse_keyword", 6,
     "terse phrasing; competes with row 5 which also discusses retrieval internals"),
    ("why can a reason-less record never answer a why query?", "well_formed", 7,
     "the /why-searches-reasoned-only rule"),
    ("relevance floor 0.5 why not raise", "terse_keyword", 9,
     "numeric threshold, adjacent in topic to rows 7 and 8"),
    ("constant time token comparison", "terse_keyword", 18,
     "security decision, lexically distinctive"),
    ("what happens when the description normalizer's provider times out?", "well_formed", 20,
     "head of a three-row cluster (20/21/22) that splits on transient vs permanent vs malformed"),
    ("permanent 400 or 404 from the model provider, fail open or closed?", "well_formed", 21,
     "the sibling that must not collapse onto row 20"),
    ("the model replied 2xx but the body was prose, not JSON -- what then", "badly_phrased", 22,
     "third sibling; badly-phrased on purpose"),
    ("normalizer cache temp file collision", "terse_keyword", 23,
     "distinct mechanism inside the same normalizer cluster"),
    ("why is normalize done outside the store write lock?", "well_formed", 24,
     "lock-ordering decision"),
    ("release script rollback health gate only covers what exactly", "badly_phrased", 32,
     "release cluster (31/32/33/34/36) is the densest near-neighbour group"),
    ("rollback tag pruning newest two", "terse_keyword", 36,
     "must not collapse onto 32 or 34, which share release vocabulary"),
    ("public bind with no token", "terse_keyword", 58,
     "infra project; the fail-closed decision"),
    ("why does /health not probe the normalizer?", "well_formed", 61,
     "infra project; distinctive mechanism"),
    ("backups must not use the sqlite3 CLI -- why not", "badly_phrased", 49,
     "infra project, badly phrased"),
    # --- true negatives: topics deliberately absent from the corpus ---------
    ("what is the retention policy for audit rows older than a year?", "well_formed", None,
     "no retention decision exists in this corpus; /why must suppress rather than reach"),
    ("which cloud region does the primary database run in?", "well_formed", None,
     "no region decision exists; nearest neighbours are infra rows about containers"),
    ("kubernetes autoscaling thresholds", "terse_keyword", None,
     "no orchestration decision exists; terse phrasing makes over-reaching likelier"),
    ("how do we rotate the signing keys for customer webhooks?", "well_formed", None,
     "no key-rotation decision exists; token rows are lexically adjacent but wrong"),
    ("mobile push notification batching", "terse_keyword", None,
     "wholly out-of-domain topic"),
    ("who approves a change to the pricing page copy", "badly_phrased", None,
     "no pricing-approval decision exists in this corpus"),
    ("graphql schema stitching approach", "terse_keyword", None,
     "wholly out-of-domain topic"),
]


def build_dataset(corpus: list[dict]) -> list[dict]:
    rows = []
    for i, (query, phrasing, idx, rationale) in enumerate(DATASET, start=1):
        if idx is None:
            project, statement, kind = "ergo", None, "true_negative"
        else:
            src = corpus[idx]
            project, statement, kind = src["project"], src["statement"], "claim"
        rows.append({
            "id": f"syn-{i:04d}",
            "query": query,
            "project": project,
            "phrasing": phrasing,
            "expected_statement": statement,
            "expected_type": kind,
            "rationale": rationale,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit", choices=("corpus", "dataset"), default="corpus")
    args = ap.parse_args()
    corpus = build_corpus()
    rows = corpus if args.emit == "corpus" else build_dataset(corpus)
    for r in rows:
        sys.stdout.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
