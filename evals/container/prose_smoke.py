#!/usr/bin/env python3
"""Container prose smoke test — exercises the FULL pipeline through the HTTP API.

Unlike the large real-world document corpus ingest script (which talks to
ContradictionStore directly in process), this drives the *running Docker
container* over HTTP /remember, so it
proves the deployed artifact end-to-end: HTTP -> LLM normalize (remote) ->
structural -> NLI -> text gates -> SQLite write.

Each case is an isolated (org, project) pair: claim A is seeded, then claim B is
the probe. Ground truth is on B, expressed as the CONTRACT the engine actually
guarantees today (not the block/warn calibration boundary, which needs a
labeled eval set):
  - "clear"   : a non-contradiction — must be STORED and must NOT hard-block
                (legit scope/time coexist, or one of the three real-prose FP
                failure modes the 2026-06-26 gates kill: boilerplate parallel
                restatement / subject homonym / unrelated).
  - "surface": a genuine contradiction — must be SURFACED (hard block OR warn),
                never silently stored.

The two HARD invariants (the headline):
  * false_blocks  = clear cases that hard-blocked    -> must be 0
  * silent_misses = surface cases stored with no flag -> must be 0
Whether a surface lands as block vs warn is reported but not asserted: on the NLI
residual path a genuine contradiction whose reason-clauses dilute lexical overlap
below the aspect floor (0.35) lands as a warn, not a block. Still surfaced.

Usage:
    ERGO_URL=http://127.0.0.1:8788 ERGO_API_TOKEN=secret-test-token \
        python evals/container/prose_smoke.py
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request

URL = os.environ.get("ERGO_URL", "http://127.0.0.1:8788").rstrip("/")
TOKEN = os.environ.get("ERGO_API_TOKEN", "secret-test-token")
ORG = os.environ.get("ERGO_TEST_ORG", "prose-smoke")

# (project, label, seed_claim, probe_claim, expected)  expected in {"block","stored"}
CASES = [
    ("p1-encrypt", "real contradiction (encryption at rest)",
     "All production database backups must be encrypted at rest, because they contain customer PII.",
     "Production database backups must not be encrypted at rest, since the ops team needs raw-file recovery.",
     "surface"),

    ("p2-env-coexist", "coexist by environment (debug logging)",
     "Verbose debug logging must not be enabled in prod, because it leaks request bodies into the logs.",
     "Verbose debug logging may be enabled in dev so engineers can trace failures locally.",
     "clear"),

    ("p3-time-coexist", "coexist by time window (legacy v1 auth)",
     "The legacy v1 auth flow may be used until 2026-09-30 during the migration window.",
     "The legacy v1 auth flow must not be used after 2026-09-30, once the new flow ships.",
     "clear"),

    ("p4-boilerplate", "parallel restatement / boilerplate (polarity gate)",
     "The Synology Chat plugin ships as a bundled plugin and is enabled by default.",
     "The Microsoft Teams plugin ships as a bundled plugin and is enabled by default.",
     "clear"),

    ("p5-homonym", "subject homonym, different aspect (aspect gate)",
     "File paths must not contain null bytes, or the loader will reject them outright.",
     "File paths must be resolved relative to the workspace root before any read.",
     "clear"),

    ("p6-coref", "coreference + real contradiction (gateway restart)",
     "The gateway must be restarted after any config change, because it only reads settings at startup.",
     "The gateway must not be restarted after a config change; it hot-reloads settings the moment the file changes.",
     "surface"),

    ("p7-unrelated", "unrelated topics (no conflict)",
     "API rate limits must be enforced on every public endpoint to prevent abuse.",
     "Database migrations should be reviewed by a second engineer before they are deployed.",
     "clear"),
]


def _post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _remember(project: str, who: str, statement: str) -> tuple[int, dict]:
    return _post("/remember", {"org_id": ORG, "project": project, "who": who,
                               "statement": statement, "reason": f"prose-smoke/{project}"})


def main() -> int:
    print(f"target: {URL}  org: {ORG}\n")
    results = []
    for project, label, seed, probe, expected in CASES:
        # seed claim A
        s_code, s_body = _remember(project, "seed", seed)
        seed_ok = s_body.get("result") == "stored"
        # probe claim B
        p_code, p_body = _remember(project, "probe", probe)
        got = p_body.get("result")  # "stored" | "conflict"
        warns = p_body.get("warnings") or []
        confs = p_body.get("conflicts") or []

        # actual outcome bucket: "block" (hard) | "warn" (stored+flagged) | "clean"
        if got == "conflict":
            actual = "block"
        elif warns:
            actual = "warn"
        else:
            actual = "clean"

        # contract check
        if expected == "clear":
            ok = seed_ok and actual != "block"      # must not hard-block
        else:  # "surface"
            ok = seed_ok and actual in ("block", "warn")  # must be flagged, not silent

        if actual == "block":
            detail = f"BLOCK: {confs[0]['reason'] if confs else '?'}"
        elif actual == "warn":
            detail = f"warn: {warns[0].get('reason','?')}"
        else:
            detail = "clean (stored, no flag)"
        if not seed_ok:
            detail = f"!! seed not stored (code {s_code}): {s_body}"

        results.append((ok, label, expected, actual, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {label}")
        print(f"        expected={expected:8} actual={actual:6} {detail}")

    passed = sum(1 for r in results if r[0])
    total = len(results)
    clear_cases = [r for r in results if r[2] == "clear"]
    surface_cases = [r for r in results if r[2] == "surface"]
    false_blocks = [r for r in clear_cases if r[3] == "block"]
    silent_misses = [r for r in surface_cases if r[3] == "clean"]
    hard_blocks = sum(1 for r in surface_cases if r[3] == "block")
    warn_surfaced = sum(1 for r in surface_cases if r[3] == "warn")

    print("\n===== SUMMARY =====")
    print(f"  cases passed         : {passed}/{total}")
    print(f"  FALSE BLOCKS         : {len(false_blocks)}  (HARD INVARIANT: must be 0)")
    print(f"  SILENT MISSES        : {len(silent_misses)}  (HARD INVARIANT: must be 0)")
    print(f"  contradictions surfaced: {hard_blocks} as block, {warn_surfaced} as warn")
    if false_blocks:
        print("  !! false-block cases:")
        for r in false_blocks:
            print(f"     - {r[1]}: {r[4]}")
    if silent_misses:
        print("  !! silently-missed contradictions:")
        for r in silent_misses:
            print(f"     - {r[1]}: {r[4]}")
    ok = (passed == total) and not false_blocks and not silent_misses
    print("\nRESULT:", "ALL PASS ✅" if ok else "FAILURES ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
