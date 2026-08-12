#!/usr/bin/env python3
"""Build the contradiction eval set.

Principled generator: labels are DERIVED from structure, not guessed.
  contradiction = same subject, overlapping scope, overlapping time, opposing polarity
  coexist       = same subject, opposing polarity, BUT disjoint scope or disjoint time
  entailment    = same subject+scope, same direction (must => should, or paraphrase)
  unrelated     = different subjects

All content is SYNTHETIC ("Helix platform" policy domain). No proprietary third-party content.
Deterministic (seeded) so dataset.jsonl is stable and reviewable.
"""
from __future__ import annotations
import json
import random
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED_PATH = HERE / "seed.jsonl"
OUT_PATH = HERE / "dataset.jsonl"
RNG = random.Random(42)

WHO = ["alice", "bob", "carol", "dave", "erin"]

# subject key -> (noun phrase, positive verb)  [polarity carried by modality]
SUBJECTS = {
    "tls_1_3": ("TLS 1.3", "enabled"),
    "debug_endpoint": ("the /debug endpoint", "enabled"),
    "force_push_main": ("force-pushing to main", "allowed"),
    "pii_in_logs": ("PII in application logs", "allowed"),
    "container_root": ("running containers as root", "allowed"),
    "public_s3": ("public S3 buckets", "allowed"),
    "retry_on_5xx": ("retry-on-5xx", "enabled"),
    "rate_limiting": ("request rate limiting", "enforced"),
    "secrets_in_env": ("storing secrets in env vars", "allowed"),
    "auto_migrations": ("automatic schema migrations on deploy", "enabled"),
    "verbose_logging": ("verbose request logging", "enabled"),
    "request_id_header": ("the request-id header", "included"),
    "feature_flags_default_on": ("feature flags defaulting to on", "enabled"),
    "mfa_login": ("MFA on login", "required"),
    "synthetic_monitoring": ("synthetic monitoring probes", "enabled"),
    "canary_deploys": ("canary deploys", "enabled"),
    "raw_sql_in_handlers": ("raw SQL in request handlers", "allowed"),
    "third_party_cookies": ("third-party cookies", "allowed"),
}

# modality -> (rendered phrase, polarity, strength)
#   polarity: +1 enables/permits, -1 forbids ; strength: 2 hard, 1 soft, 0 permissive
MODAL = {
    "must":       ("must be", +1, 2),
    "must_not":   ("must not be", -1, 2),
    "should":     ("should be", +1, 1),
    "should_not": ("should not be", -1, 1),
    "may":        ("may be", +1, 0),
}

ENVS = ["prod", "staging", "dev"]
SERVICE = ["public-facing", "internal-only", "batch"]
VERSIONS = ["v1", "v2"]
REGIONS = ["eu", "us"]

WINDOWS = {
    "before_cut": (None, "2026-09-30"),
    "after_cut":  ("2026-10-01", None),
    "open":       (None, None),
}


def scope_phrase(scope: dict) -> str:
    parts = []
    if scope.get("env"):
        parts.append(f"in {scope['env']}")
    if scope.get("service_class"):
        parts.append(f"for {scope['service_class']} services")
    if scope.get("region"):
        parts.append(f"in the {scope['region'].upper()} region")
    if scope.get("version"):
        parts.append(f"for {scope['version']} services")
    return (" " + " ".join(parts)) if parts else ""


def time_phrase(vf, vu) -> str:
    if vf and not vu:
        return f" from {vf}"
    if vu and not vf:
        return f" until {vu}"
    return ""


def make_claim(subj_key: str, modality: str, scope: dict, vf=None, vu=None) -> dict:
    noun, verb = SUBJECTS[subj_key]
    modal_phrase, _, _ = MODAL[modality]
    text = f"{noun[0].upper()}{noun[1:]} {modal_phrase} {verb}{scope_phrase(scope)}{time_phrase(vf, vu)}."
    return {
        "text": text,
        "who": RNG.choice(WHO),
        "norm": {
            "subject": subj_key, "modality": modality, "object": verb,
            "scope": {"env": scope.get("env"), "service_class": scope.get("service_class"),
                      "region": scope.get("region"), "version": scope.get("version")},
            "valid_from": vf, "valid_until": vu, "exception": None,
        },
    }


def windows_overlap(a, b) -> bool:
    (af, au), (bf, bu) = a, b
    af = af or "0000-00-00"; au = au or "9999-99-99"
    bf = bf or "0000-00-00"; bu = bu or "9999-99-99"
    return af <= bu and bf <= au


_id = 0
def nid() -> str:
    global _id
    _id += 1
    return f"gen-{_id:04d}"


records: list[dict] = []

def add(category, difficulty, label, a, b, rationale):
    records.append({"id": nid(), "category": category, "difficulty": difficulty,
                    "label": label, "claim_a": a, "claim_b": b, "rationale": rationale})


subj_keys = list(SUBJECTS)

# 1) CONTRADICTION — same subject+scope, overlapping (open) time, opposing polarity
for k in subj_keys:
    add("required_vs_forbidden", "easy", "contradiction",
        make_claim(k, "must", {}), make_claim(k, "must_not", {}),
        "identical subject+scope, open time, opposing polarity")
    add("allowed_vs_disallowed", "easy", "contradiction",
        make_claim(k, "may", {}), make_claim(k, "must_not", {}),
        "permission vs prohibition, same scope")

# 1b) CONTRADICTION — same env scope, opposing polarity (every subject, 2 envs)
for k in subj_keys:
    for env in RNG.sample(ENVS, 2):
        add("required_vs_forbidden", "easy", "contradiction",
            make_claim(k, "must", {"env": env}), make_claim(k, "must_not", {"env": env}),
            f"same env ({env}), opposing polarity")

# 1c) CONTRADICTION — temporal OVERLAP (both open-ended from a start date)
for k in subj_keys:
    add("temporal_overlap", "hard", "contradiction",
        make_claim(k, "must", {}, vf="2026-07-01"),
        make_claim(k, "must_not", {}, vf="2026-08-01"),
        "both open-ended from start dates -> windows overlap after later start -> conflict")

# 1d) CONTRADICTION — exception clause that guts a blanket rule (same scope)
for k in RNG.sample(subj_keys, 10):
    a = make_claim(k, "must_not", {})
    a["norm"]["exception"] = "no exceptions"
    a["text"] = a["text"][:-1] + ", with no exceptions."
    b = make_claim(k, "may", {"service_class": RNG.choice(SERVICE)})
    add("exception_clause_contradiction", "hard", "contradiction", a, b,
        "blanket prohibition (no exceptions) vs a carved-out permission in same scope")

# 2) COEXIST — same subject, opposing polarity, DISJOINT env
for k in subj_keys:
    e1, e2 = RNG.sample(ENVS, 2)
    add("scope_env_coexist", "hard", "coexist",
        make_claim(k, "must", {"env": e1}), make_claim(k, "must_not", {"env": e2}),
        f"opposing polarity but disjoint env ({e1} vs {e2})")

# 2b) COEXIST — DISJOINT service_class
for k in subj_keys:
    s1, s2 = RNG.sample(SERVICE, 2)
    add("scope_service_coexist", "hard", "coexist",
        make_claim(k, "must", {"service_class": s1}), make_claim(k, "may", {"service_class": s2}),
        f"opposing direction but disjoint service_class ({s1} vs {s2})")

# 2c) COEXIST — DISJOINT version
for k in subj_keys:
    add("version_scope_coexist", "hard", "coexist",
        make_claim(k, "must", {"version": "v2"}), make_claim(k, "must_not", {"version": "v1"}),
        "opposing polarity but disjoint version (parallel API versions)")

# 2d) COEXIST — temporal supersession (clean non-overlapping handoff)
for k in subj_keys:
    add("temporal_supersession", "hard", "coexist",
        make_claim(k, "must", {}, vu="2026-09-30"),
        make_claim(k, "must_not", {}, vf="2026-10-01"),
        "opposing polarity, non-overlapping windows -> supersession not live conflict")

# 2e) COEXIST — exception clause that matches the carve-out (they AGREE)
for k in RNG.sample(subj_keys, 10):
    a = make_claim(k, "must_not", {})
    a["norm"]["exception"] = "except in dev"
    a["text"] = a["text"][:-1] + ", except in dev."
    b = make_claim(k, "may", {"env": "dev"})
    add("exception_clause_coexist", "hard", "coexist", a, b,
        "B is exactly A's stated exception -> they agree, not conflict")

# 3) ENTAILMENT — same subject+scope, same direction, must => should
for k in subj_keys:
    add("strength_entailment", "hard", "entailment",
        make_claim(k, "must", {}), make_claim(k, "should", {}),
        "same direction; must entails should")
for k in subj_keys:
    add("not_recommended_vs_prohibited", "hard", "entailment",
        make_claim(k, "must_not", {}), make_claim(k, "should_not", {}),
        "same direction; must_not entails should_not")

# 4) UNRELATED — different subjects (explicit sample of distinct pairs)
all_pairs = list(combinations(subj_keys, 2))
for a, b in RNG.sample(all_pairs, 28):
    add("unrelated", "easy", "unrelated",
        make_claim(a, RNG.choice(["must", "should"]), {}),
        make_claim(b, RNG.choice(["must", "must_not"]), {}),
        "different subjects entirely")

# --- merge seed, dedupe, shuffle, write -------------------------------------
seed = [json.loads(l) for l in SEED_PATH.read_text().splitlines() if l.strip()]
all_recs = seed + records
seen, deduped = set(), []
for r in all_recs:
    key = (r["claim_a"]["text"], r["claim_b"]["text"])
    if key in seen:
        continue
    seen.add(key)
    deduped.append(r)
RNG.shuffle(deduped)

OUT_PATH.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in deduped))

# --- coverage report ---------------------------------------------------------
from collections import Counter
labels = Counter(r["label"] for r in deduped)
cats = Counter(r["category"] for r in deduped)
diff = Counter(r["difficulty"] for r in deduped)
print(f"wrote {len(deduped)} pairs -> {OUT_PATH.relative_to(HERE.parent.parent)}")
print(f"  seed={len(seed)} generated={len(records)} (after dedupe {len(deduped)})")
print("  labels:", dict(labels))
print("  difficulty:", dict(diff))
print("  categories:")
for c, n in sorted(cats.items(), key=lambda x: -x[1]):
    print(f"     {n:>4}  {c}")
neg = labels["coexist"] + labels["entailment"] + labels["unrelated"]
print(f"  positives (contradiction)={labels['contradiction']}  negatives={neg}")
hard_neg = sum(1 for r in deduped if r["label"] in ("coexist",) and r["difficulty"] == "hard")
print(f"  HARD coexist (false-positive traps)={hard_neg}")
for lbl in ("contradiction", "coexist", "entailment", "unrelated"):
    if labels[lbl] < 20:
        print(f"  WARN: thin label '{lbl}' = {labels[lbl]}")
