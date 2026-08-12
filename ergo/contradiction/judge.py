"""Combined contradiction judge: normalize -> structural -> NLI fallback -> tiers.

Returns (label, score) where label is "contradiction" or "neutral" (the harness only
needs the binary split), plus a confidence tier accessible via `.verdict()`.

Confidence tiers: structural HIGH decisions block/clear directly; only
the ambiguous residual pays for an NLI call. Tiers map to product behavior:
  block (>= block_thr), warn (>= warn_thr), else silent-log.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

from .structural import compare, HIGH, OBJECT_POLARITY
from .normalize import GoldNormalizer, RuleNormalizer  # noqa: F401 (re-export convenience)

# --- text-level precision gates (added 2026-06-26 after a large real-world
# document corpus ingest) ----
# The structural comparator works on normalized fields, which can be too coarse on
# real prose: distinct predicates collapse to the same canonical object, and two
# different subjects (e.g. "Synology Chat" / "Microsoft Teams") collapse to one
# ("bundled plugin"). These two gates re-check the RAW text before trusting an
# 'opposing-stance' verdict. See the prose eval's calibration history in
# evals/prose/RESULTS.md for detail.
_WORD = re.compile(r"[a-z0-9']+")
_NEG = {"not", "no", "never", "cannot", "cant", "without", "neither", "nor", "none"}
# Omission verbs: "Skip X" / "Avoid X" / "Omit X" assert the action is NOT done, so
# vs a plain "Do X" they are a polarity flip — but they carry no _NEG token and the
# bare verb forms aren't object-antonyms, so the pre-2026-06-27 gate missed them and
# silently dropped real contradictions even when NLI scored them 1.0 (the
# smoke-test "Skip smoke-testing X" vs "Smoke-test X" false-negative). Counted with
# _NEG in the same odd/even parity bucket; the NLI_OVERLAP_FLOOR still gates them, so
# an omission verb on unrelated text can't manufacture a flag.
_OMISSION = {
    "skip", "skips", "skipped", "skipping",
    "omit", "omits", "omitted", "omitting",
    "avoid", "avoids", "avoided", "avoiding",
    "forgo", "forgoes", "forgoing", "forwent",
    "bypass", "bypasses", "bypassed", "bypassing",
}
# Fix #2 (2026-06-26): on the subject-unknown NLI path, a bare negation-parity
# flip (a stray "not"/"never" in one sentence) is too weak to trust — it produced
# the bulk of the false warns. We require EITHER real object-antonym opposition
# (_strong_polarity_flip) OR a negation flip backed by shared content above this
# floor. Calibrated on evals/prose/dataset.jsonl (real FPs sit at overlap <=0.18;
# genuine NLI-dependent contradictions sit at >=~0.25).
NLI_OVERLAP_FLOOR = 0.25
_STOP = set(
    "a an the is are was were be been being to of in for on and or with by as at "
    "from into out only also each any all this that these those it its their them "
    "they we you i he she so but if then when where which who than".split()
)
ASPECT_JACCARD_FLOOR = 0.35   # below this, same-subject opposing claims are 'different aspects'


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("n't", " not"))


def _content(words) -> set:
    return {w for w in words if w not in _STOP and len(w) > 1}


def _jaccard(a_words, b_words) -> float:
    A, B = _content(a_words), _content(b_words)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _strong_polarity_flip(a_words, b_words) -> bool:
    """Real semantic opposition: an enabling term on one side and a disabling term
    on the other (object-polarity antonyms: enabled/disabled, approved/rejected,
    retained/removed, …). Strong enough to trust even when subjects only fuzzily
    match, because it names the opposition explicitly — unlike a bare stray-'not'."""
    sym = set(a_words) ^ set(b_words)
    signs = {OBJECT_POLARITY[w] for w in sym if w in OBJECT_POLARITY}
    return (1 in signs and -1 in signs)


def _polarity_flip(a_words, b_words) -> bool:
    """True iff the two texts express OPPOSING polarity — the thing that actually
    makes a contradiction. Detected via (1) net-negation parity (odd vs even count
    of not/no/never/…) or (2) object-polarity antonyms (enabled vs disabled, …) in
    the symmetric token difference. If there's NO flip, an 'opposing-stance'
    structural verdict is a normalization artifact (coarse object/subject), not a
    real contradiction.

    NOTE: the negation-parity half is WEAK on the subject-unknown NLI path (a stray
    'not' in an unrelated sentence trips it). Callers on that path pair it with an
    overlap floor; the strong (antonym) half is trustworthy on its own."""
    na = sum(1 for w in a_words if w in _NEG or w in _OMISSION)
    nb = sum(1 for w in b_words if w in _NEG or w in _OMISSION)
    if (na % 2) != (nb % 2):
        return True
    return _strong_polarity_flip(a_words, b_words)


def _value_grounded(va: str, vb: str, a_words, b_words) -> bool:
    """Confirm the two differing object-values are real: distinct, and each side's
    distinguishing token(s) actually appear in its own raw text. This is the
    precision guard for the value-conflict path — it stops a hallucinated/ungrounded
    normalizer value from manufacturing a 'blue vs red'-style contradiction."""
    av, bv = set(_tokens(va)), set(_tokens(vb))
    if not av or not bv:
        return False
    diff_a, diff_b = av - bv, bv - av
    if not diff_a or not diff_b:
        return False
    a_set, b_set = set(a_words), set(b_words)
    return bool(diff_a & a_set) and bool(diff_b & b_set)


@dataclass
class JudgeResult:
    label: str            # "contradiction" | "neutral"
    score: float
    tier: str             # "block" | "warn" | "log"
    source: str           # "structural" | "nli"
    reason: str


class ContradictionJudge:
    def __init__(self, normalizer, nli=None, nli_threshold: float = 0.5,
                 warn_threshold: float = 0.35):
        self.normalizer = normalizer
        self.nli = nli
        self.nli_threshold = nli_threshold
        self.warn_threshold = warn_threshold

    def verdict(self, claim_a: dict, claim_b: dict) -> JudgeResult:
        # Trust a persisted/extractor-provided `norm` if present (#6) — store passes
        # the stored norm so old claims aren't re-normalized on every comparison.
        na = claim_a.get("norm") if claim_a.get("norm") is not None else self.normalizer.normalize(claim_a)
        nb = claim_b.get("norm") if claim_b.get("norm") is not None else self.normalizer.normalize(claim_b)
        v = compare(na, nb)

        # --- text-level precision gates (only for contradiction verdicts) -------
        text_a = claim_a.get("text") or ""
        text_b = claim_b.get("text") or ""
        if v.label == "contradiction" and text_a and text_b:
            aw, bw = _tokens(text_a), _tokens(text_b)
            # Gate 0 — value conflict: a "blue canary" vs "red canary" / "port 8443"
            # vs "port 9090" disagreement has NO polarity flip (both prescribe), so
            # the polarity gate below would wrongly veto it. Instead confirm the two
            # differing object-values are actually grounded in their texts (guards
            # against a hallucinated normalizer value), then honour the structural
            # tier: HIGH -> block, softer modality -> warn.
            if getattr(v, "value_conflict", False):
                va = (na.get("value") or "")
                vb = (nb.get("value") or "")
                if _value_grounded(va, vb, aw, bw):
                    if v.confidence == HIGH:
                        return JudgeResult("contradiction", 0.95, "block",
                                           "structural-value", v.reason)
                    return JudgeResult("contradiction", 0.6, "warn",
                                       "structural-value", v.reason)
                return JudgeResult("neutral", 0.1, "log", "value-gate",
                                   f"value mismatch not grounded in text ({v.reason})")
            # Gate 1 — polarity: no opposing polarity in the actual text means the
            # 'opposing stance' was a normalization artifact (coarse object verbs /
            # collapsed subjects). Catches templated boilerplate + soft collisions.
            if not _polarity_flip(aw, bw):
                return JudgeResult("neutral", 0.05, "log", "polarity-gate",
                                   f"no polarity flip in text — parallel/restatement ({v.reason})")
            # Gate 2 — aspect: genuine flip but near-zero predicate overlap means the
            # two claims share only a coarse subject token, not a topic (paths: null
            # bytes vs resolution; secretrefs: structured-objects vs file-safety).
            jac = _jaccard(aw, bw)
            if jac < ASPECT_JACCARD_FLOOR:
                if self.nli is not None:
                    s = self.nli.contradiction_score(text_a, text_b)
                    if s < self.nli_threshold:
                        return JudgeResult("neutral", s, "log", "aspect-gate",
                                           f"low predicate overlap ({jac:.2f}) + nli {s:.2f} — different aspects")
                    return JudgeResult("contradiction", s, "warn", "aspect-gate",
                                       f"low overlap ({jac:.2f}) but nli {s:.2f} — surfaced as warn")
                return JudgeResult("contradiction", 0.5, "warn", "aspect-gate",
                                   f"low predicate overlap ({jac:.2f}) — warn not block")

        if v.confidence == HIGH:
            if v.label == "contradiction":
                return JudgeResult("contradiction", 0.95, "block", "structural", v.reason)
            return JudgeResult("neutral", 0.05, "log", "structural", v.reason)

        # ambiguous (MED/LOW) -> NLI if available
        if self.nli is not None:
            ta = claim_a.get("text") or ""
            tb = claim_b.get("text") or ""
            s = self.nli.contradiction_score(ta, tb)
            if s >= self.nli_threshold:
                # NLI wants to flag. But raw-prose NLI is noisy (P~0.58 — it
                # conflates "related / different aspect" with "contradiction").
                # Gate it (Fix #2, 2026-06-26) on TWO text signals together:
                #   1. a polarity flip must exist (negation parity OR object
                #      antonym) — no opposition in the text => not a contradiction.
                #   2. that opposition must be on SHARED content (overlap). An
                #      object-antonym across two *unrelated* sentences (one says
                #      "enabled", the other "disabled" about different things) is a
                #      false flip; real contradictions restate the same claim and so
                #      carry high overlap. Overlap is the arbiter, not flip-strength.
                aw, bw = _tokens(ta), _tokens(tb)
                if not _polarity_flip(aw, bw):
                    return JudgeResult("neutral", s, "log", "nli+polarity-gate",
                                       f"nli {s:.2f} but no polarity flip in text ({v.reason})")
                overlap = _jaccard(aw, bw)
                if overlap >= ASPECT_JACCARD_FLOOR:      # shared predicate -> confident
                    return JudgeResult("contradiction", s, "block", "nli",
                                       f"nli {s:.2f} + polarity flip, overlap {overlap:.2f} ({v.reason})")
                if overlap >= NLI_OVERLAP_FLOOR:         # moderate -> surface as warn
                    return JudgeResult("contradiction", s, "warn", "nli+aspect-gate",
                                       f"nli {s:.2f} + polarity flip, overlap {overlap:.2f} -> warn ({v.reason})")
                # opposition but on unrelated content -> drop (the FP killer).
                return JudgeResult("neutral", s, "log", "nli+aspect-gate",
                                   f"nli {s:.2f}, polarity flip but overlap {overlap:.2f} < {NLI_OVERLAP_FLOOR} ({v.reason})")
            if s >= self.warn_threshold:
                # mid-band NLI: only surface if there's a polarity flip on shared
                # content; otherwise it's prose noise.
                aw, bw = _tokens(ta), _tokens(tb)
                if _polarity_flip(aw, bw) and _jaccard(aw, bw) >= NLI_OVERLAP_FLOOR:
                    return JudgeResult("contradiction", s, "warn", "nli",
                                       f"nli residual warn ({v.reason})")
                return JudgeResult("neutral", s, "log", "nli", f"nli mid-band, no opposition ({v.reason})")
            return JudgeResult("neutral", s, "log", "nli", f"nli residual ({v.reason})")

        # no NLI: fall back to the structural guess
        label = "contradiction" if v.label == "contradiction" else "neutral"
        return JudgeResult(label, 0.5, "warn", "structural", f"low-conf structural ({v.reason})")

    def __call__(self, claim_a: dict, claim_b: dict):
        r = self.verdict(claim_a, claim_b)
        return r.label, r.score
