#!/usr/bin/env python3
"""Embedding-only contradiction-recall baseline.

THE QUESTION
------------
Ergo's differentiator is the NLI contradiction firewall on the write path. But a
modern embedder might already separate contradictions in vector space, which
would make NLI dead weight. So we measure, with the embedder ALONE (no NLI):

    When a claim is stored and its CONTRADICTORY counterpart is used as the
    query, how often does pure cosine KNN recall surface the stored
    contradiction inside top-K (K = 1, 3, 5, 10)?

Reading of the result (matches the decision gate):
  * If the counterpart is NEVER in top-K, the embedder pushes contradictions far
    apart on its own -> the NLI firewall is redundant.
  * If the counterpart IS in top-K, the contradiction sits among the nearest
    neighbours, where cosine distance alone cannot tell "near & AGREES" from
    "near & CONTRADICTS" -> the NLI firewall is justified (it is the only thing
    that disambiguates near-neighbours).

REALISM (the part reviewers insisted on)
----------------------------------------
Clean SYNTHETIC contradictions (bare polarity flips, disjoint scope) can be
easy for embedders and would bias the answer toward "NLI redundant". Real
contradictions sit CLOSE (same topic, opposite conclusion). So three arms are
reported SEPARATELY:
  A. prose      evals/prose/dataset.jsonl                       (semi-real)
  B. synthetic  evals/contradiction/dataset.jsonl              (templated)
  C. realism    evals/embedding_baseline/realism_pairs.jsonl   (messy, close)

EMBEDDER-ONLY, STORE UNCHANGED
------------------------------
We build a real ContradictionStore WITH the FastEmbed embedder, but the NLI
judge is never invoked: claims are inserted through remember_fact() (the
gate-free fact path -- no normalizer, no judge) and queried by hitting the
sqlite-vec cosine index directly. Nothing in the store/NLI pipeline is modified;
this is measurement only.

WHY A POPULATED CORPUS (not a 1-claim store)
--------------------------------------------
Storing only `a` and querying `b` makes top-K trivially rank-1 (the store holds
one claim) and answers nothing -- it is precisely the artifact the realism
reviewers warned about. Instead, EVERY claim from ALL arms is inserted once into
ONE shared, realistic distractor corpus (~800 notes). Each query then competes
against the same large memory, so cross-arm differences reflect genuine vector
proximity rather than corpus size. For each pair we run BOTH directions
(query b -> find a, query a -> find b); the query's own claim is excluded from
its ranking.

Run: python evals/embedding_baseline/run.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from ergo.contradiction.store import ContradictionStore  # noqa: E402
from ergo.embedders import FastEmbed  # noqa: E402

ORG, PROJ = "eval", "embedding_baseline"
KS = (1, 3, 5, 10)


# --- dataset loaders --------------------------------------------------------
def load_prose(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append({"a": r["a"], "b": r["b"],
                    "contradiction": r["label"] == "contradiction"})
    return out


def load_synth(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append({"a": r["claim_a"]["text"], "b": r["claim_b"]["text"],
                    "contradiction": r["label"] == "contradiction"})
    return out


def load_realism(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append({"a": r["a"], "b": r["b"],
                    "contradiction": r["label"] == "contradiction"})
    return out


# --- vector ranking against the shared corpus -------------------------------
def vec_ranked_ids(store, qvec, k):
    """Full cosine ranking of corpus claim_ids for a query vector (nearest first)."""
    rows = store._exec(
        "SELECT claim_id, distance FROM vec_claims "
        "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (store._serialize(qvec), k)).fetchall()
    return [(r["claim_id"], r["distance"]) for r in rows]


def counterpart_rank(ranked, self_id, target_id):
    """1-based rank of target_id once the query's own claim (self_id) is removed."""
    rank = 0
    for cid, dist in ranked:
        if cid == self_id:
            continue
        rank += 1
        if cid == target_id:
            return rank, dist
    return None, None


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def summarize(trials: list[dict]) -> dict:
    """trials: [{rank:int|None, counterpart_dist:float}]"""
    n = len(trials)
    topk = {k: sum(1 for t in trials if t["rank"] is not None and t["rank"] <= k)
            for k in KS}
    dists = [t["counterpart_dist"] for t in trials if t["counterpart_dist"] is not None]
    dstats = {}
    if dists:
        dists_sorted = sorted(dists)
        dstats = {
            "mean": statistics.fmean(dists),
            "median": statistics.median(dists),
            "p10": dists_sorted[max(0, int(0.10 * (len(dists) - 1)))],
            "p90": dists_sorted[max(0, int(0.90 * (len(dists) - 1)))],
        }
    return {"n": n, "topk": topk, "dist": dstats}


def gate_bucket(pct_value: float) -> str:
    if pct_value <= 0.0:
        return "NEVER (NLI redundant)"
    if pct_value <= 5.0:
        return "1-5% (marginal; consider selective/domain-gated NLI)"
    return ">5% (NLI justified)"


def main():
    arms = {
        "prose":     load_prose(ROOT / "evals" / "prose" / "dataset.jsonl"),
        "synthetic": load_synth(ROOT / "evals" / "contradiction" / "dataset.jsonl"),
        "realism":   load_realism(HERE / "realism_pairs.jsonl"),
    }

    # --- one shared embedder + one shared corpus ---------------------------
    embedder = FastEmbed()
    vec_cache: dict[str, list[float]] = {}

    def embed(text: str):
        v = vec_cache.get(text)
        if v is None:
            v = embedder.embed(text)
            vec_cache[text] = v
        return v

    # ContradictionStore WITH embedder = retrieval enabled. We only ever call
    # remember_fact() (gate-free, no NLI) so the contradiction judge never runs.
    store = ContradictionStore(":memory:", embedder=embedder)

    # Insert every unique claim text exactly once; remember text -> claim_id.
    text_to_id: dict[str, str] = {}
    all_texts = []
    for pairs in arms.values():
        for p in pairs:
            all_texts.append(p["a"])
            all_texts.append(p["b"])
    for text in all_texts:
        if text in text_to_id:
            continue
        # warm the embed cache so query-side reuses the same vector
        embed(text)
        res = store.remember_fact(ORG, PROJ, "eval", text)
        text_to_id[text] = res.claim_id
    corpus_n = len(text_to_id)

    def cosine_dist(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return 1.0 - dot / (na * nb) if na and nb else 1.0

    # --- run trials --------------------------------------------------------
    results = {}
    for arm, pairs in arms.items():
        contra_trials, noncon_trials = [], []
        for p in pairs:
            ia, ib = text_to_id[p["a"]], text_to_id[p["b"]]
            if ia == ib:
                continue  # degenerate identical pair
            va, vb = embed(p["a"]), embed(p["b"])
            direct = cosine_dist(va, vb)  # corpus-independent closeness
            # direction 1: store holds a, query with b -> is a in top-K?
            r1, _ = counterpart_rank(vec_ranked_ids(store, vb, corpus_n), ib, ia)
            # direction 2: store holds b, query with a -> is b in top-K?
            r2, _ = counterpart_rank(vec_ranked_ids(store, va, corpus_n), ia, ib)
            bucket = contra_trials if p["contradiction"] else noncon_trials
            bucket.append({"rank": r1, "counterpart_dist": direct})
            bucket.append({"rank": r2, "counterpart_dist": direct})
        results[arm] = {
            "contradiction": summarize(contra_trials),
            "non_contradiction": summarize(noncon_trials),
        }

    _report(results, corpus_n)
    return 0


def _report(results, corpus_n):
    print("=" * 74)
    print("EMBEDDING-ONLY CONTRADICTION-RECALL BASELINE")
    print(f"shared distractor corpus = {corpus_n} unique claims; "
          f"2 trials per pair (both directions); pure cosine KNN, NLI disabled")
    print("=" * 74)

    def row(label, s):
        if not s["n"]:
            print(f"  {label:18} (no pairs)")
            return
        tk = s["topk"]
        d = s["dist"]
        print(f"  {label:18} n={s['n']:4}  "
              f"top1={pct(tk[1], s['n']):5.1f}%  top3={pct(tk[3], s['n']):5.1f}%  "
              f"top5={pct(tk[5], s['n']):5.1f}%  top10={pct(tk[10], s['n']):5.1f}%  "
              f"| cos-dist med={d.get('median', float('nan')):.3f} "
              f"p10={d.get('p10', float('nan')):.3f} p90={d.get('p90', float('nan')):.3f}")

    print("\nCONTRADICTION pairs (counterpart surfaced in top-K):")
    for arm in ("prose", "synthetic", "realism"):
        row(arm, results[arm]["contradiction"])

    print("\nNON-CONTRADICTION pairs (baseline separation):")
    for arm in ("prose", "synthetic", "realism"):
        row(arm, results[arm]["non_contradiction"])

    print("\nDECISION GATE (REALISM contradiction arm):")
    rc = results["realism"]["contradiction"]
    for k in KS:
        p = pct(rc["topk"][k], rc["n"])
        print(f"  top-{k:<2} surfaced = {p:5.1f}%  -> {gate_bucket(p)}")

    # machine-readable block for faithful transcription into RESULTS.md
    print("\n--- JSON SUMMARY ---")
    out = {"corpus_n": corpus_n, "arms": {}}
    for arm, r in results.items():
        out["arms"][arm] = {
            kind: {
                "n": s["n"],
                "topk_pct": {str(k): round(pct(s["topk"][k], s["n"]), 1) for k in KS},
                "cos_dist": {kk: round(vv, 4) for kk, vv in s["dist"].items()},
            } for kind, s in r.items()
        }
    print(json.dumps(out))


if __name__ == "__main__":
    sys.exit(main())
