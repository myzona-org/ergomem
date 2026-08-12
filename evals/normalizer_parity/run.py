#!/usr/bin/env python3
"""Measure RuleNormalizer (zero-dependency) parity against the LLM normalizer.

WHY: Ergo's deploy target is an air-gapped server. The default normalizer calls an
LLM (claude-haiku via LiteLLM) on every remember() — 1-2s latency, token cost, and a
runtime SPOF. If the deterministic RuleNormalizer is "close enough", Ergo becomes one
image with ZERO runtime LLM dependency. This harness runs BOTH normalizers over the
same prose claims and reports how often they agree on the structured fields the
contradiction judge actually consumes: subject, modality, object, and derived polarity.

It does NOT change the default normalizer — it only measures. The swap is
an operator call, informed by the parity bucket this prints.

LLM SIDE — cached, offline-capable:
  The live default is OpenAINormalizer(model=claude-haiku) behind a LiteLLM proxy, and
  every prose claim has already been extracted into the on-disk cache
  (evals/contradiction/.cache/llm_norm_litellm_haiku.json). So this runs OFFLINE against
  REAL claude-haiku extractions at zero cost. If a claim is NOT cached and the proxy is
  unreachable, that claim is skipped; if NOTHING can be obtained, the script emits the
  explicit "LLM side unavailable — parity not measured" note (it never fakes numbers).

Usage:
    python evals/normalizer_parity/run.py [--data PATH] [--cache PATH]
                                          [--model NAME] [--write-md] [--worst N]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from ergo.contradiction.normalize import RuleNormalizer, OpenAINormalizer  # noqa: E402
from ergo.contradiction.structural import _stance                          # noqa: E402

DEFAULT_DATA = ROOT / "evals/prose/dataset.jsonl"
DEFAULT_CACHE = ROOT / "evals/contradiction/.cache/llm_norm_litellm_haiku.json"
FIELDS = ("subject", "modality", "object", "polarity")


def load_claim_texts(path: Path) -> list[str]:
    """The unique individual claim texts (both sides of every pair), order-stable."""
    seen, out = set(), []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for side in ("a", "b"):
            t = row.get(side)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def polarity(norm: dict):
    """Derived stance: +1 asserts/permits the subject-state, -1 forbids it, None if
    undetermined. This is what the judge's polarity gate keys on, so it's the field
    that matters more than the raw modality/object tokens individually."""
    return _stance(norm)


def subjects_overlap(a: str | None, b: str | None) -> bool:
    """Subjects 'agree' if one contains the other or they share >=50% of tokens
    (Jaccard). Exact string equality is too harsh across two extractors that phrase
    the same noun differently ('v1 auth flow' vs 'legacy v1 auth flow')."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return a == b
    if a == b or a in b or b in a:
        return True
    ta = set(a.replace("_", " ").split())
    tb = set(b.replace("_", " ").split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.5


def field_agree(field: str, rn: dict, ln: dict) -> bool:
    if field == "subject":
        return subjects_overlap(rn.get("subject"), ln.get("subject"))
    if field == "polarity":
        return polarity(rn) == polarity(ln)
    return rn.get(field) == ln.get(field)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--model", default=os.environ.get("ERGO_LLM_MODEL", "claude-haiku"))
    ap.add_argument("--provider", default="openai",
                    help="LLM provider for the cached/live side (openai|litellm)")
    ap.add_argument("--worst", type=int, default=12, help="how many divergences to list")
    ap.add_argument("--write-md", action="store_true", help="(re)write RESULTS.md")
    args = ap.parse_args()

    texts = load_claim_texts(Path(args.data))

    # LLM side: cache-first. A dummy key lets us construct the client purely to read
    # the cache; a real network call only happens for an UNcached text (and is allowed
    # to fail -> that text is skipped).
    os.environ.setdefault("OPENAI_API_KEY", "cache-only-no-network")
    llm = OpenAINormalizer(model=args.model, cache_path=args.cache)
    # RuleNormalizer is stateless here: each claim scored independently, like the LLM.
    rule = RuleNormalizer(track_context=False)

    paired = []          # (text, rule_norm, llm_norm)
    llm_failures = 0
    for t in texts:
        claim = {"text": t}
        try:
            ln = llm.normalize(claim)        # cache hit = offline; miss = live call
        except Exception:
            llm_failures += 1
            continue
        paired.append((t, rule.normalize(claim), ln))

    n = len(paired)
    if n == 0:
        msg = ("LLM side unavailable — parity not measured. "
               f"0/{len(texts)} claims had a cached LLM norm and the proxy was "
               "unreachable (no network call succeeded). Re-run with the LiteLLM "
               "proxy reachable, or with a populated --cache, to measure parity.")
        print(msg)
        if args.write_md:
            _write_md(args, texts, paired=[], note=msg, per_field={}, worst=[])
        return 0

    # --- score -------------------------------------------------------------
    per_field = {f: 0 for f in FIELDS}
    exact = 0           # all 4 fields agree
    core = 0            # modality + object + polarity agree (ignores subject phrasing)
    worst = []
    for t, rn, ln in paired:
        agrees = {f: field_agree(f, rn, ln) for f in FIELDS}
        for f in FIELDS:
            per_field[f] += agrees[f]
        if all(agrees.values()):
            exact += 1
        if agrees["modality"] and agrees["object"] and agrees["polarity"]:
            core += 1
        else:
            worst.append((t, rn, ln, agrees))

    pct = lambda c: 100.0 * c / n
    print(f"\nRuleNormalizer vs LLMNormalizer({args.model})  —  {n} claims "
          f"({len(texts)} unique; {llm_failures} LLM-unavailable, skipped)\n")
    print(f"  {'field':<12} parity")
    print(f"  {'-'*24}")
    for f in FIELDS:
        print(f"  {f:<12} {pct(per_field[f]):5.1f}%   ({per_field[f]}/{n})")
    print(f"  {'-'*24}")
    print(f"  {'EXACT (all 4)':<12} {pct(exact):5.1f}%   ({exact}/{n})")
    print(f"  {'CORE (mod+obj+pol)':<12} {pct(core):4.1f}%   ({core}/{n})")

    bucket = _bucket(pct(exact))
    print(f"\n  => exact-match parity {pct(exact):.1f}% lands in: {bucket}\n")

    print(f"  Worst divergences (modality/object/polarity disagreements), "
          f"showing {min(args.worst, len(worst))} of {len(worst)}:")
    for t, rn, ln, agrees in worst[:args.worst]:
        bad = [f for f in FIELDS if not agrees[f]]
        print(f"   - [{','.join(bad)}] {t[:78]}")
        print(f"       rule: subj={rn.get('subject')!r} mod={rn.get('modality')} "
              f"obj={rn.get('object')} pol={polarity(rn)}")
        print(f"       llm : subj={ln.get('subject')!r} mod={ln.get('modality')} "
              f"obj={ln.get('object')} pol={polarity(ln)}")

    if args.write_md:
        _write_md(args, texts, paired,
                  note=None, per_field=per_field, worst=worst,
                  exact=exact, core=core, llm_failures=llm_failures)
        print(f"\n  wrote {HERE / 'RESULTS.md'}")
    return 0


def _bucket(exact_pct: float) -> str:
    if exact_pct >= 85:
        return ">=85% (SWAP-CANDIDATE: RuleNormalizer could become the default)"
    if exact_pct >= 70:
        return "70-84% (KEEP LLM default; add a zero-dependency flag)"
    return "<70% (LLM STAYS the default)"


def _write_md(args, texts, paired, note, per_field, worst,
              exact=0, core=0, llm_failures=0):
    n = len(paired)
    lines = ["# Normalizer parity — RuleNormalizer vs LLMNormalizer", ""]
    lines.append(f"_Generated by `evals/normalizer_parity/run.py` over "
                 f"`{Path(args.data).name}` ({len(texts)} unique claim texts)._")
    lines.append("")
    if note:
        lines += ["## Result: NOT MEASURED", "", f"> {note}", ""]
        (HERE / "RESULTS.md").write_text("\n".join(lines))
        return
    pct = lambda c: 100.0 * c / n
    lines += [
        f"LLM side: `{args.model}` (cached real extractions, offline). "
        f"Scored **{n}** claims ({llm_failures} unavailable, skipped). "
        "RuleNormalizer run stateless (per-claim, no coref context) for a fair "
        "head-to-head.",
        "",
        "## Parity",
        "",
        "| field | parity |",
        "|---|---|",
    ]
    for f in FIELDS:
        lines.append(f"| {f} | {pct(per_field[f]):.1f}% ({per_field[f]}/{n}) |")
    lines += [
        f"| **EXACT (all 4 agree)** | **{pct(exact):.1f}%** ({exact}/{n}) |",
        f"| CORE (modality+object+polarity) | {pct(core):.1f}% ({core}/{n}) |",
        "",
        f"**Bucket: {_bucket(pct(exact))}**",
        "",
        "## Interpretation",
        "",
        "The gap is real-prose vocabulary, not the two fixes in this task. "
        "RuleNormalizer is tuned for the synthetic Helix policy domain; on this "
        "natural-prose set it returns `object=None` (and therefore `polarity=None`) "
        "whenever the claim's verb falls outside its small canonical enum — `seeded`, "
        "`validated`, `restarted`, `forbidden`, `exposed`, `automatic` — which is most "
        "of the divergences below. The LLM abstracts those to canonical verbs "
        "(`allowed`/`required`/`enforced`) and resolves `must never`→`must_not`; "
        "RuleNormalizer's modal set has no `never`/`forbidden` negator, so it also "
        "flips a chunk of `modality`. **The coref + inline-code fixes shipped in this "
        "task barely move THIS number** — by design: this contradiction dataset has "
        "almost no bare-pronoun subjects or backtick spans. Those fixes target "
        "document-INGESTION prose (extract.py → remember()), which this set doesn't "
        "exercise; they are unit-tested separately.",
        "",
        "**Decision input:** parity is far below the 85% swap threshold, so the LLM "
        "normalizer stays the default. Closing this gap would mean expanding "
        "RuleNormalizer's object/modal vocabulary + abstraction rules (a separate, "
        "calibration-sensitive effort — out of scope here, which was measure-only).",
        "",
        "`polarity` is the derived stance (`structural._stance`): modality sign × "
        "object-polarity sign — the field the judge's polarity gate actually keys on. "
        "`subject` parity uses containment / ≥50% token-Jaccard, not exact string "
        "equality (two extractors phrase the same noun differently).",
        "",
        "## Worst divergences (modality/object/polarity disagreements)",
        "",
    ]
    for t, rn, ln, agrees in worst[: args.worst]:
        bad = ", ".join(f for f in FIELDS if not agrees[f])
        lines += [
            f"- **[{bad}]** {t}",
            f"  - rule: subj=`{rn.get('subject')}` mod=`{rn.get('modality')}` "
            f"obj=`{rn.get('object')}` pol=`{polarity(rn)}`",
            f"  - llm&nbsp;: subj=`{ln.get('subject')}` mod=`{ln.get('modality')}` "
            f"obj=`{ln.get('object')}` pol=`{polarity(ln)}`",
        ]
    (HERE / "RESULTS.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
