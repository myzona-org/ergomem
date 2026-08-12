#!/usr/bin/env python3
"""Assemble the labeled PROSE contradiction eval set (a hard release gate).

Three sources, all real-prose-shaped (NOT the clean Helix templates):
  1. POSITIVES   — hand-authored genuine contradictions across every mode
                   (negation flip, object antonym, modality, with/without reason
                   clauses, coreference, same-scope opposing).
  2. NEAR_MISS   — hand-authored NON-contradictions that LOOK contradictory: the
                   precision traps (scope coexist, time supersession, boilerplate
                   parallel, homonym/different-aspect, stronger-subset refinement,
                   same-structure-different-subject, stray-not unrelated).
  3. CORPUS FPs  — real false positives the NLI judge produced on a large
                   real-world document corpus ingest (harvested via
                   harvest_negatives.py). Non-circular:
                   the engine itself made these mistakes on real text.

harvest_negatives.py reads from a real-world document ingest cache that is
private and does not ship in this tree -- rerunning this script from scratch
here will fail at that step. The already-harvested corpus-FP rows are baked
into the committed evals/prose/dataset.jsonl, so the eval itself (evaluate.py)
runs standalone with no dependency on the harvester.

Output: evals/prose/dataset.jsonl  {a, b, label, tag}
label ∈ {contradiction, not_contradiction}
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "dataset.jsonl"
N_CORPUS = 60   # how many harvested corpus FPs to include

# --- 1. POSITIVES: genuine contradictions -------------------------------------
POSITIVES = [
    # negation flip, shared predicate, no reason clause
    ("Production database backups must be encrypted at rest.",
     "Production database backups must not be encrypted at rest.", "neg-flip"),
    ("The /metrics endpoint must be exposed publicly.",
     "The /metrics endpoint must never be exposed publicly.", "neg-flip"),
    ("Webhook payloads should be validated before processing.",
     "Webhook payloads do not need to be validated before processing.", "neg-flip"),
    ("Inbound requests must be authenticated.",
     "Inbound requests must not be authenticated.", "neg-flip"),
    ("The staging database may be seeded with synthetic data.",
     "The staging database must not be seeded with synthetic data.", "modality-flip"),
    # object antonyms
    ("Telemetry collection must be enabled for all production services.",
     "Telemetry collection must be disabled for all production services.", "antonym"),
    ("Guest accounts are allowed to access the staging dashboard.",
     "Guest accounts are forbidden from accessing the staging dashboard.", "antonym"),
    ("The legacy importer should be removed in the next release.",
     "The legacy importer should be retained in the next release.", "antonym"),
    ("Anonymous pairing requests are approved automatically.",
     "Anonymous pairing requests are rejected automatically.", "antonym"),
    ("Outbound email is permitted from the worker nodes.",
     "Outbound email is blocked from the worker nodes.", "antonym"),
    # modality always/never
    ("You must always restart the gateway after editing cron jobs.",
     "You should never restart the gateway after editing cron jobs.", "always-never"),
    ("Schema migrations must run automatically on deploy.",
     "Schema migrations must not run automatically on deploy; an operator triggers them.", "modality"),
    ("Secrets must always be loaded from the vault.",
     "Secrets should never be loaded from the vault.", "always-never"),
    # WITH reason clauses (the lexical-overlap dilution case — must keep recall)
    ("All customer exports must be encrypted, because they contain PII subject to GDPR.",
     "Customer exports must not be encrypted, since the analytics team needs raw file access.", "neg-flip+reason"),
    ("The cache must be cleared on startup to avoid serving stale entries after a deploy.",
     "The cache must not be cleared on startup, because a warm cache is critical for first-request latency.", "neg-flip+reason"),
    ("Feature flags should be evaluated server-side so clients cannot tamper with them.",
     "Feature flags should be evaluated client-side, since the server has no per-user context at render time.", "antonym+reason"),
    ("Background jobs must be idempotent, because the queue delivers at-least-once.",
     "Background jobs do not need to be idempotent, since the queue guarantees exactly-once delivery.", "neg-flip+reason"),
    ("Container images must be rebuilt from source on every release for provenance.",
     "Container images must not be rebuilt on every release; we promote the exact tested artifact.", "neg-flip+reason"),
    # coreference (pronoun subject the extractor must resolve)
    ("The scheduler reads its config only at boot, so it must be restarted after any change.",
     "The scheduler watches its config file, so it must not be restarted when the config changes.", "coref"),
    ("Redis is the source of truth for sessions, so it must never be flushed in production.",
     "Redis only holds a disposable cache, so it may be flushed in production whenever needed.", "coref"),
    ("The token is short-lived, so it should be refreshed on every request.",
     "The token is long-lived, so it should not be refreshed on every request.", "coref"),
    # same explicit scope, opposing (real contradiction within prod)
    ("Debug logging must be disabled in prod.",
     "Debug logging must be enabled in prod.", "same-scope"),
    ("TLS 1.2 must be enabled for public endpoints in prod.",
     "TLS 1.2 must not be enabled for public endpoints in prod.", "same-scope"),
    ("In the EU region, user data must be stored locally.",
     "In the EU region, user data must not be stored locally.", "same-scope"),
    # permission vs prohibition
    ("API keys may be stored in environment variables for local development.",
     "API keys must not be stored in environment variables under any circumstances.", "modality-flip"),
    ("Force-pushing to main is allowed for release managers.",
     "Force-pushing to main is prohibited for everyone, including release managers.", "antonym"),
    # subtler real ones
    ("The bot must reply to every message in the channel.",
     "The bot must stay silent unless it is directly mentioned.", "semantic"),
    ("Sessions expire after 30 minutes of inactivity.",
     "Sessions never expire due to inactivity.", "neg-flip"),
    ("New plugins must ship a config schema.",
     "New plugins are not required to ship a config schema.", "neg-flip"),
    ("The webhook URL must use HTTPS.",
     "The webhook URL may use plain HTTP.", "modality-flip"),
]

# --- 2. NEAR_MISS: look contradictory, are NOT --------------------------------
NEAR_MISS = [
    ("Verbose debug logging must not be enabled in prod, because it leaks request bodies.",
     "Verbose debug logging may be enabled in dev so engineers can trace failures.", "scope-coexist"),
    ("In prod, the admin API must be disabled.",
     "In dev, the admin API may be enabled.", "scope-coexist"),
    ("The legacy v1 auth flow may be used until 2026-09-30 during migration.",
     "The legacy v1 auth flow must not be used after 2026-09-30.", "time-coexist"),
    ("Maintenance mode may be enabled during the deploy window.",
     "Maintenance mode must not be enabled outside the deploy window.", "time-coexist"),
    ("The Slack plugin ships as a bundled plugin and is enabled by default.",
     "The Discord plugin ships as a bundled plugin and is enabled by default.", "boilerplate"),
    ("Twitch ships as a bundled plugin, so no separate install is required.",
     "Microsoft Teams ships as a bundled plugin, so no separate install is required.", "boilerplate"),
    ("File paths must not contain null bytes.",
     "File paths must be resolved relative to the workspace root before use.", "homonym"),
    ("Channel keys must use stable Slack channel IDs.",
     "Room keys should be Matrix room IDs or aliases.", "homonym"),
    ("All public endpoints should be rate-limited.",
     "Public write endpoints must be rate-limited.", "refinement"),
    ("Logs should be retained for at least 30 days.",
     "Audit logs must be retained for at least 90 days.", "refinement"),
    ("The cache must not be cleared on startup.",
     "The buffer must not be flushed on startup.", "same-structure"),
    ("Migrations do not run automatically on deploy.",
     "Backups run nightly at 02:00 UTC.", "stray-not"),
    ("The bot must not post in archived channels.",
     "The bot should greet new members when they join.", "stray-not"),
    ("Secrets must be referenced via SecretRef objects.",
     "Tokens are stored hashed, never in plaintext.", "compatible"),
    ("Pairing in DMs uses the approve flow.",
     "Channel access uses an allowlist of stable IDs.", "compatible"),
]


def main():
    rows = []
    for a, b, tag in POSITIVES:
        rows.append({"a": a, "b": b, "label": "contradiction", "tag": f"pos/{tag}"})
    for a, b, tag in NEAR_MISS:
        rows.append({"a": a, "b": b, "label": "not_contradiction", "tag": f"near-miss/{tag}"})

    # harvested corpus FPs
    harvest = HERE / "harvest_negatives.py"
    res = subprocess.run([sys.executable, str(harvest)], capture_output=True, text=True)
    corpus = [json.loads(l) for l in res.stdout.splitlines() if l.strip()][:N_CORPUS]
    for c in corpus:
        rows.append({"a": c["a"], "b": c["b"], "label": "not_contradiction", "tag": c["tag"]})

    with OUT.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    pos = sum(1 for r in rows if r["label"] == "contradiction")
    neg = len(rows) - pos
    print(f"wrote {OUT}  ({len(rows)} pairs: {pos} contradiction / {neg} not)")
    by_tag = {}
    for r in rows:
        fam = r["tag"].split("/")[0]
        by_tag[fam] = by_tag.get(fam, 0) + 1
    print("families:", by_tag)


if __name__ == "__main__":
    main()
