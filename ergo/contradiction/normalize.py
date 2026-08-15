"""Normalizers: claim text -> structured `norm` dict.

In production the RuleNormalizer stands in for a local instruct-LLM extractor:
the structured fields are what make contradiction detection robust to
scope/modality/time, instead of guessing from raw prose.

- GoldNormalizer: pass-through (uses the gold `norm` already on a record). Lets us
  measure the structural comparator + NLI in isolation from extraction error.
- RuleNormalizer: regex/keyword extractor. Works well on the regular synthetic
  "Helix" domain; degrades on messier prose (which is exactly the gap a real LLM
  extractor fills — and why GoldNormalizer is the upper bound).
"""
from __future__ import annotations
import json
import os
import re
import tempfile
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol


class NormalizeUnavailable(RuntimeError):
    """The LLM normalizer's provider ANSWERED WITH AN ERROR (or couldn't be
    reached at all) — a connection error/timeout, or ANY non-2xx status,
    raised as an `httpx.HTTPError` from `_chat()`. `retryable` (set by the
    raiser, see `_is_retryable_provider_failure` / `_is_permanent_provider_error`)
    tells the caller which of two fail-CLOSED classes this is:
      - retryable=True: connection error/timeout, or a RETRYABLE non-2xx
        status — 429 rate-limit, 5xx, or auth 401/403. server.py maps this to
        HTTP 503 (degraded, worth a retry).
      - retryable=False: a PERMANENT client-error status (400 bad request,
        404 unknown model, 422, ...) — the provider answered with a
        definitive, non-transient rejection. server.py maps this to HTTP 502
        (bad gateway, NON-retryable — re-review #3: the earlier
        fix only closed the retryable class and left permanent 4xx falling
        through to fail-open, which stored a guarded write with NO
        contradiction check whenever a caller passed a bad model name).
    Distinct from malformed model OUTPUT, which still degrades to an
    all-unknown norm and fails OPEN because
    the provider answered 2xx, just not usefully:
      - a reply WAS received (2xx) but had no parseable JSON / refusal prose
        (caught in `_extract()` via `_parse_json`);
      - a malformed-SHAPE reply (`_chat()` itself raising KeyError/IndexError/
        TypeError/`json.JSONDecodeError` while picking the content out of a
        2xx body, e.g. `body["choices"][0]["message"]["content"]`).

    Raised by `_LLMNormalizerBase._extract()` and left to propagate out of
    `normalize()`. The guarded write path (store.py) catches this and fails
    CLOSED: refuses the write with this exception (server.py maps it to 503
    or 502 per `retryable`) unless the caller supplies an audited
    `force_exception` override. Reversing the old "catch every _chat()
    exception and fail open" behavior per review finding #3: silently skipping the contradiction
    check during a provider error is a silent firewall bypass.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


_RETRYABLE_STATUSES = {401, 403, 429}


def _is_retryable_provider_failure(exc: Exception) -> bool:
    """True for a genuine provider INFRASTRUCTURE failure worth a 503-and-
    retry: a connection error or timeout (any `httpx.HTTPError` that isn't a
    status error), or a RETRYABLE non-2xx status — auth 401/403, rate-limit
    429, or 5xx.

    False for:
      - a malformed/unexpected response BODY on an otherwise-successful reply
        (KeyError/IndexError/TypeError/json.JSONDecodeError while a `_chat()`
        implementation picks the assistant text out of the parsed JSON) —
        that still fails OPEN, it is not this failure class.
      - a PERMANENT client-error status (400 bad request, 404 unknown model,
        422, ...) — see `_is_permanent_provider_error`, the sibling classifier
        for the OTHER fail-closed class (502, non-retryable).

    httpx may be unavailable in a pure-Ollama/offline install with no network
    deps loaded; treat that as "can't be an httpx error" rather than raising.
    """
    try:
        import httpx
    except ImportError:
        return False
    if not isinstance(exc, httpx.HTTPError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRYABLE_STATUSES or status >= 500
    return True


def _is_permanent_provider_error(exc: Exception) -> bool:
    """True for a PERMANENT provider client-error status (400 bad request,
    404 unknown model, 422, and any other non-retryable 4xx) from an
    `httpx.HTTPStatusError` (raised by `_chat()`'s `raise_for_status()`): the
    provider answered with a definitive, non-transient rejection — retrying
    (which a 503 implies) never helps a bad model name or malformed request.

    This is FAIL-CLOSED (NormalizeUnavailable, retryable=False -> HTTP 502),
    not fail-open: re-review #3 reproduced a direct contradiction being
    ACCEPTED (stored with no check) while the provider returned 404 for a bad
    model name, because this class used to fall through to the same
    fail-open path as malformed model output. A definitive provider
    rejection is not the same failure class as "the provider answered
    something we couldn't parse" — it must never silently skip the
    contradiction check.

    Only an `httpx.HTTPStatusError` reaches here; a connection error/timeout
    (no response at all) is never "permanent" in this sense, it is the
    retryable infra class above.
    """
    try:
        import httpx
    except ImportError:
        return False
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status = exc.response.status_code
    return not (status in _RETRYABLE_STATUSES or status >= 500)


def _shift_date(iso: str, days: int) -> str:
    try:
        y, m, d = (int(x) for x in iso.split("-"))
        return (date(y, m, d) + timedelta(days=days)).isoformat()
    except Exception:
        return iso


def _strip_think_blocks(s: str) -> str:
    """Remove every `<think>...</think>` span, linear in len(s).

    Replaces the O(n^2) `re.sub(r"<think>.*?</think>", "", s, flags=re.S)`,
    whose lazy `.*?` rescans from every `<think>` occurrence looking for a
    close tag (CodeQL ReDoS alert). An unterminated `<think>` (no matching
    `</think>`) is left untouched, same as the regex it replaces."""
    out, i = [], 0
    while True:
        a = s.find("<think>", i)
        if a == -1:
            out.append(s[i:])
            return "".join(out)
        b = s.find("</think>", a + 7)
        if b == -1:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:a])
        i = b + 8


def _extract_fenced(s: str) -> str | None:
    """Return the content of the first ```-fenced block, or None if absent.

    Replaces the O(n^2) ``r"```(?:json)?\\s*(.*?)```"`` regex (CodeQL ReDoS
    alert) with an index-based scan."""
    a = s.find("```")
    if a == -1:
        return None
    j = a + 3
    if s[j:j + 4].lower() == "json":
        j += 4
    while j < len(s) and s[j].isspace():
        j += 1
    b = s.find("```", j)
    return None if b == -1 else s[j:b]


class Normalizer(Protocol):
    def normalize(self, claim: dict) -> dict: ...


class GoldNormalizer:
    """Trusts the gold `norm` field on the eval record."""
    VERSION = "1"
    def normalize(self, claim: dict) -> dict:
        return claim["norm"]


_DATE = r"(\d{4}-\d{2}-\d{2})"
_ENVS = ("prod", "staging", "dev")
_SERVICE = ("public-facing", "internal-only", "batch", "analyst",
            "fraud-review", "static-assets")
_OBJECTS = ("enabled", "disabled", "allowed", "disallowed", "used", "removed",
            "blocked", "included", "enforced", "required", "permitted",
            "prohibited", "skipped", "deployed", "approved")
_STOP = {"the", "a", "an", "all", "every", "any", "of", "to", "in", "for",
         "on", "and", "or", "be", "is", "are", "their", "its"}

# Pronoun coreference (rule-based, NO LLM). A claim whose entire subject is a bare
# pronoun ("It must be rotated.") loses its subject under the rule extractor; the
# fix resolves it to the most recent NUMBER-COMPATIBLE antecedent seen in the same
# normalization session. Conservative by design: only a STANDALONE pronoun is a
# coref candidate ("this rule" stays "this rule"), and if no compatible antecedent
# exists the pronoun is left unresolved rather than guessing a wrong subject.
_PRONOUNS_SING = {"it", "this", "that"}
_PRONOUNS_PLUR = {"they", "these", "those"}
_PRONOUNS = _PRONOUNS_SING | _PRONOUNS_PLUR
# Words ending in 's' that are NOT plural (so "tls"/"https" don't read as plural
# antecedents for "they"). The "ss" guard handles class/access/process.
_SINGULAR_S = {"tls", "https", "dns", "cors", "ios", "aws", "jenkins",
               "kubernetes", "redis", "nginx", "kafka", "cors"}

# modality phrase -> canonical, ordered so negatives/multiword match first
_MODAL_PATTERNS = [
    (r"must not|shall not|may not", "must_not"),
    (r"should not|not recommended", "should_not"),
    (r"must|shall|required to|is required|are required", "must"),
    (r"should|recommended", "should"),
    (r"may|can be|is permitted|are permitted|is allowed|are allowed|permitted", "may"),
]


class RuleNormalizer:
    """Deterministic extractor for Helix-style policy claims.

    Stateful only for pronoun coreference: it remembers the recent subjects it has
    emitted (`_recent`, a small rolling window) so a later bare-pronoun subject can
    be resolved against them. Set `track_context=False` for stateless per-claim use
    (the parity eval does this so each claim is scored independently); pass an
    explicit `context` list to `normalize()` to drive resolution deterministically.
    """
    VERSION = "3"   # bumped: pronoun coref + inline-code masking

    def __init__(self, track_context: bool = True, context_window: int = 6):
        self._track = track_context
        self._window = context_window
        self._recent: list[str] = []

    def reset_context(self) -> None:
        """Clear the rolling antecedent memory (e.g. between documents)."""
        self._recent.clear()

    def normalize(self, claim: dict, context: list[str] | None = None) -> dict:
        from .extract import mask_inline_code, unmask_inline_code

        text = claim["text"].strip()
        # Mask `inline code` spans so a modal/object-looking identifier (e.g. the
        # Cache-Control directive `must-revalidate`, or a `--no-cache` flag) can't
        # hijack the modality boundary. We scan the masked text, then restore the
        # real identifier into subject/predicate before field extraction.
        masked, spans = mask_inline_code(text)
        low_spans = [s.lower() for s in spans]
        masked_low = masked.lower()
        low = unmask_inline_code(masked_low, low_spans)   # full text, code restored

        # modality + split point (scan the MASKED text so code can't create a modal)
        modality, mpos = None, None
        for pat, canon in _MODAL_PATTERNS:
            m = re.search(rf"\b({pat})\b", masked_low)
            if m and (mpos is None or m.start() < mpos):
                modality, mpos = canon, m.start()
        # earliest match wins as the subject/predicate boundary
        if mpos is None:
            mpos = len(masked_low)
            modality = "must"

        # split on the masked text, then restore code content in each half
        subject_raw = unmask_inline_code(masked_low[:mpos], low_spans)
        predicate = unmask_inline_code(masked_low[mpos:], low_spans)

        # subject signature
        subject = self._subject_sig(subject_raw)

        # pronoun coreference: resolve a standalone-pronoun subject against the
        # session's recent antecedents (explicit context arg wins over _recent).
        ctx = context if context is not None else self._recent
        subject = self._resolve_coref(subject, ctx)

        # object verb
        obj = None
        for o in _OBJECTS:
            if re.search(rf"\b{o}\b", predicate):
                obj = o
                break
        if obj is None:
            # "turned off" / "default to on|off"
            if "turned off" in predicate or re.search(r"\boff\b", predicate):
                obj = "off"
            elif re.search(r"\bon\b", predicate):
                obj = "on"

        # scope
        env = next((e for e in _ENVS if re.search(rf"\b{e}\b", low)), None)
        svc = next((s for s in _SERVICE if s in low), None)
        region = None
        rm = re.search(r"\b(eu|us)\b(?:\s+region)?", low)
        if rm and "region" in low:
            region = rm.group(1)
        ver = None
        vm = re.search(r"\bv([12])\b", low)
        if vm:
            ver = f"v{vm.group(1)}"

        # time (exclusive boundaries: 'after X' starts X+1; 'before X' ends X-1)
        vf = vu = None
        m = re.search(rf"until\s+{_DATE}", low)
        if m:
            vu = m.group(1)
        m = re.search(rf"before\s+{_DATE}", low)
        if m:
            vu = _shift_date(m.group(1), -1)
        m = re.search(rf"after\s+{_DATE}", low)
        if m:
            vf = _shift_date(m.group(1), 1)
        for kw in ("from", "starting", "as of"):
            m = re.search(rf"{kw}\s+{_DATE}", low)
            if m:
                vf = m.group(1)

        # exception
        exception = None
        em = re.search(r"(no exceptions|except[^.,]*|unless[^.,]*)", low)
        if em:
            exception = em.group(1).strip()

        # value-bearing object selector (colour/port that differentiates otherwise
        # identical prescriptions). The Helix synthetic domain rarely uses these, so
        # the default is None; the LLM normalizer is the primary producer of `value`.
        value = None
        pm = re.search(r"\bport\s+(\d{2,5})\b", low)
        if pm:
            value = pm.group(1)
        else:
            cm = re.search(r"\b(blue|green|red|black|amber)\b", low)
            if cm:
                value = cm.group(1)

        # remember this subject as a future antecedent (only real noun phrases,
        # never an unresolved pronoun, so a wrong subject can't poison later coref).
        if self._track and subject and subject not in _PRONOUNS:
            self._recent.append(subject)
            if len(self._recent) > self._window:
                self._recent.pop(0)

        return {
            "subject": subject, "modality": modality, "object": obj, "value": value,
            "scope": {"env": env, "service_class": svc, "region": region, "version": ver},
            "valid_from": vf, "valid_until": vu, "exception": exception,
        }

    def _resolve_coref(self, subject: str, context: list[str]) -> str:
        """Resolve a bare-pronoun subject to the most recent number-compatible
        antecedent in `context` (most-recent-last). Returns the subject unchanged
        when it isn't a standalone pronoun, or when no compatible antecedent exists
        (conservative: leave unresolved rather than guess)."""
        if subject not in _PRONOUNS:
            return subject
        want_plural = subject in _PRONOUNS_PLUR
        for ante in reversed(context):
            if ante and ante not in _PRONOUNS and self._is_plural(ante) == want_plural:
                return ante
        return subject

    @staticmethod
    def _is_plural(np: str) -> bool:
        """Cheap number heuristic for a noun phrase: an '... and ...' conjunction or
        a trailing token that is a plural 's' (excluding known singular-'s' tech
        terms and '-ss' words)."""
        if not np:
            return False
        if " and " in np:
            return True
        last = np.split()[-1]
        if last in _SINGULAR_S:
            return False
        return last.endswith("s") and not last.endswith("ss")

    @staticmethod
    def _subject_sig(raw: str) -> str:
        # drop scope/leadin words, keep content tokens for matching
        raw = re.sub(r"[^a-z0-9.\s/_-]", " ", raw)
        toks = [t for t in raw.split() if t not in _STOP]
        # strip trailing scope words that sometimes precede the modal
        return " ".join(toks).strip()


_LLM_SYSTEM = """You extract structured fields from a single engineering-policy claim.
Return ONLY a JSON object with EXACTLY these keys:
  subject       short lowercase noun phrase naming WHAT the rule is about (no modality/scope words).
                STRIP any value-bearing qualifier that merely selects WHICH variant — a color,
                a port number, a hostname, a version label, a count/threshold, or a proper-noun
                choice — and put that in `value` instead. Two claims that pick different variants
                of the same thing ("a blue canary" vs "a red canary") MUST share the same subject
                ("canary") and differ only in `value`.
  modality      one of: must | must_not | should | should_not | may
  object        the canonical state verb, one of:
                enabled disabled allowed disallowed used removed blocked included
                enforced required permitted prohibited skipped deployed approved on off
  value         the specific value-bearing detail that distinguishes this claim from an otherwise
                near-identical one: a color, a port number, a hostname, a numeric threshold/count,
                a version string, or a proper-noun choice (lowercase). Use the minimal distinguishing
                token(s) — e.g. "blue", "8443", "us-east-1". Leave it null for a bare state ("must
                be enabled") and for documentation that merely lists option names/modes (e.g.
                "open", "off", "always", "allowlist"). NEVER fold this value into the object verb;
                the object stays one of the canonical verbs above.
  scope         object with keys env, service_class, region, version (each a string or null)
                env in {prod,staging,dev}; region in {eu,us}; version like v1/v2
  valid_from    ISO date the rule starts, or null. "after DATE" => DATE + 1 day. "from/starting/as of DATE" => DATE.
  valid_until   ISO date the rule ends (inclusive), or null. "until DATE" => DATE. "before DATE" => DATE - 1 day.
  exception     short text of any carve-out (e.g. "except in dev", "no exceptions"), or null
Normalize "must not"/"shall not"->must_not, "not recommended"->should_not, "required"->must,
"recommended"->should, "permitted"/"allowed"/"can"->may. Pick the object verb that matches the
claim's polarity (e.g. "must be disabled" => modality=must, object=disabled)."""

_LLM_EXAMPLES = [
    ("TLS 1.2 must be enabled for all public-facing services in prod.",
     {"subject": "tls 1.2", "modality": "must", "object": "enabled", "value": None,
      "scope": {"env": "prod", "service_class": "public-facing", "region": None, "version": None},
      "valid_from": None, "valid_until": None, "exception": None}),
    ("The legacy v1 auth flow must not be used after 2026-09-30.",
     {"subject": "v1 auth flow", "modality": "must_not", "object": "used", "value": None,
      "scope": {"env": None, "service_class": None, "region": None, "version": "v1"},
      "valid_from": "2026-10-01", "valid_until": None, "exception": None}),
    ("Secrets must not be stored in environment variables, except in dev.",
     {"subject": "secrets in env vars", "modality": "must_not", "object": "allowed", "value": None,
      "scope": {"env": None, "service_class": None, "region": None, "version": None},
      "valid_from": None, "valid_until": None, "exception": "except in dev"}),
    # value-bearing selector: subject stays shared ("canary"), the colour goes to `value`
    # so two otherwise-identical prescriptions are distinguishable (the blue/red bug).
    ("Deploys must use a blue canary.",
     {"subject": "canary", "modality": "must", "object": "used", "value": "blue",
      "scope": {"env": None, "service_class": None, "region": None, "version": None},
      "valid_from": None, "valid_until": None, "exception": None}),
    # numeric selector: the port number is the distinguisher, not the subject.
    ("The gateway must bind to port 8443.",
     {"subject": "gateway", "modality": "must", "object": "used", "value": "8443",
      "scope": {"env": None, "service_class": None, "region": None, "version": None},
      "valid_from": None, "valid_until": None, "exception": None}),
]


class _LLMNormalizerBase:
    """Shared scaffolding for instruct-LLM extractors (the 'normalize' role).

    Subclasses implement `_chat(messages) -> str` to call their provider; everything
    else (prompt, few-shot, cache, atomic writes, schema coercion) is shared.

    Hardened after a 2026-06-25 review:
      - `_coerce()` validates every field against the canonical enums; unknown
        values become None so structural._stance() degrades to LOW unknown
        (#18, #26).
      - Cache writes are atomic (os.replace) and use a temp file (#27).

    Hardened against a 2026-07-12 review (finding #5): the cache is shared,
    mutable state on a singleton normalizer serving every concurrent HTTP
    worker thread. `self._cache_lock` guards every read-check-update-save of
    `self._cache`, and `_save()` uses a UNIQUE temp file per write
    (`tempfile.mkstemp`, not a fixed `cache_path + ".tmp"`) so two threads
    racing a cache miss can no longer both target the same tmp path and have
    the loser's `os.replace()` raise `FileNotFoundError` (reproduced with 8
    concurrent threads pre-fix).
    """
    VERSION = "3"   # bumped: provider-agnostic + cache key now includes provider
    PROVIDER = "base"

    def __init__(self, model: str, cache_path: str | None = None, timeout: float = 120.0):
        self.model = model
        self.timeout = timeout
        self.cache_path = Path(cache_path or os.environ.get(
            "ERGO_LLM_CACHE",
            Path(__file__).resolve().parents[2] / "evals/contradiction/.cache/llm_norm.json"))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = {}
        self._cache_lock = threading.Lock()
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except Exception:
                self._cache = {}

    def _key(self, text: str) -> str:
        # provider in the key so an Anthropic-extracted norm doesn't collide with
        # an Ollama-extracted norm of the same text.
        return f"{self.PROVIDER}::{self.model}::{text}"

    def _save(self):
        # Atomic write (#27), hardened for concurrency (#5): a UNIQUE temp file
        # per write (tempfile.mkstemp in the same dir), not a fixed
        # `cache_path + ".tmp"`. With a fixed name, two threads racing a cache
        # miss could both write the SAME tmp path, and whichever thread's
        # os.replace() ran second would find the tmp file already consumed by
        # the first -> FileNotFoundError -> 500 (reproduced with 8 concurrent
        # threads). Caller holds `self._cache_lock` for the full read-save.
        #
        # Best-effort (a 2026-07-13 review): the cache is a pure optimization
        # (a miss just re-extracts), not part of the extraction contract, so a
        # disk-full/permission OSError here must NOT propagate out of
        # normalize() and turn an already-successful extraction into a
        # failure. Swallow OSError (mkstemp/write/os.replace/cleanup); any
        # OTHER exception (e.g. a TypeError from json.dumps on a broken cache
        # entry) still raises — that is a real bug, not a transient I/O fault.
        tmp_name = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=self.cache_path.parent, prefix=self.cache_path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._cache, ensure_ascii=False, indent=0))
            os.replace(tmp_name, self.cache_path)
        except OSError:
            if tmp_name is not None:
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

    def normalize(self, claim: dict) -> dict:
        text = claim["text"].strip()
        k = self._key(text)
        with self._cache_lock:
            if k in self._cache:
                return self._cache[k]
        # _extract() may raise NormalizeUnavailable (provider infrastructure
        # failure) — deliberately NOT caught here; it propagates to the
        # caller (store.py), which fails the guarded write CLOSED (#3).
        norm, ok = self._extract(text)
        # Only cache a norm the LLM actually produced. A degraded all-unknown
        # norm from a TRANSIENT failure (malformed/refusal reply) must never be
        # cached: the cache short-circuits re-extraction, so caching the
        # degrade would poison this statement's norm forever — it would stay
        # subject=None (uncheckable) long after the provider recovered, and
        # every future contradiction check against it would silently degrade
        # to incomparable. Skipping the cache lets a retry re-extract and win
        # once the provider is healthy. (audit #6)
        if ok:
            with self._cache_lock:
                self._cache[k] = norm
                self._save()
        return norm

    def _chat(self, messages: list[dict]) -> str:
        """Provider-specific. Return raw assistant text (caller strips <think> + parses JSON)."""
        raise NotImplementedError

    def _extract(self, text: str) -> tuple[dict, bool]:
        """Return (norm, ok). ok=False means the extraction degraded and the
        result must NOT be cached (see normalize()).

        Raises NormalizeUnavailable when `_chat()` raises a genuine provider
        ERROR — connection refused/timeout, or ANY non-2xx status
        (`httpx.HTTPError`) — split into two fail-CLOSED classes (review
        finding #3):
          - retryable=True (`_is_retryable_provider_failure`): connection
            error/timeout, or 429/5xx/401/403 — server.py maps this to 503.
          - retryable=False (`_is_permanent_provider_error`): a PERMANENT
            client-error status (400/404/422/...) — server.py maps this to
            502. The write-guard (store.py) fails CLOSED on both classes, not
            just the retryable one; it must never silently skip the
            contradiction check because the provider's rejection happened to
            be non-retryable.

        Everything else degrades and fails OPEN to an all-unknown norm,
        unchanged: a reply that WAS received but carries no
        parseable JSON (refusal prose, garbled body — handled below via
        `_parse_json` returning None), AND a malformed/unexpected response
        BODY that makes `_chat()` itself raise while picking the assistant
        text out of an otherwise-successful reply (KeyError/IndexError/
        TypeError/`json.JSONDecodeError` — a 2026-07-13 review: the
        provider answered, just not usefully, that is not an infra failure).
        """
        messages = [{"role": "system", "content": _LLM_SYSTEM}]
        for ex_text, ex_json in _LLM_EXAMPLES:
            messages.append({"role": "user", "content": ex_text})
            messages.append({"role": "assistant", "content": json.dumps(ex_json)})
        messages.append({"role": "user", "content": text})
        try:
            content = self._chat(messages)
        except Exception as e:
            if _is_retryable_provider_failure(e):
                raise NormalizeUnavailable(
                    f"{type(self).__name__}._chat failed: {e}", retryable=True) from e
            if _is_permanent_provider_error(e):
                raise NormalizeUnavailable(
                    f"{type(self).__name__}._chat failed: {e}", retryable=False) from e
            return self._coerce({}), False
        data = self._parse_json(content)
        # An LLM that declines to emit a JSON object (refusal prose on non-policy
        # text, empty body, or an array carrying no object) must NOT 500 the write
        # that triggered this normalize. Degrade to an all-unknown norm so the
        # judge treats the text as non-comparable (LOW/unknown) — the same way
        # _coerce() degrades unknown enum values rather than running on garbage.
        if data is None:
            return self._coerce({}), False
        return self._coerce(data), True

    @staticmethod
    def _parse_json(content: str):
        """Robustly pull a single JSON object out of an LLM response.

        Returns the parsed dict, or None when the model produced no usable JSON
        object — the caller's signal to degrade to an unknown norm instead of
        raising. A normalizer crash here would otherwise propagate up and 500
        any remember/supersede whose contradiction-check re-normalizes the
        offending text.

        Handles the shapes providers actually return:
          - raw JSON object (OpenAI with response_format=json_object)
          - ```json ... ``` fenced (Claude/Anthropic — no json-mode flag)
          - <think>...</think> preamble (reasoning models via Ollama)
          - prose around a single {...} block
          - a top-level [ ... ] array (Claude sometimes returns a list of claims)
          - pure refusal prose with no JSON at all
        """
        # 1. strip reasoning-model think blocks
        content = _strip_think_blocks(content).strip()
        # 2. strip markdown code fences (```json ... ``` or ``` ... ```)
        fenced = _extract_fenced(content)
        if fenced is not None:
            content = fenced.strip()
        # 3. fast path: it's already clean JSON (object or array)
        try:
            return _LLMNormalizerBase._first_object(json.loads(content))
        except json.JSONDecodeError:
            pass
        # 4. last resort: grab the first balanced {...} object in the text
        start = content.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(content)):
                if content[i] == "{":
                    depth += 1
                elif content[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return _LLMNormalizerBase._first_object(
                                json.loads(content[start:i + 1]))
                        except json.JSONDecodeError:
                            break
        # no parseable JSON object — signal "the model gave us nothing usable"
        return None

    @staticmethod
    def _first_object(data):
        """Coerce a parsed-JSON value to a single dict, or None.

        Claude occasionally returns a `[ {...}, {...} ]` array for compound input;
        take the first object. A bare scalar / empty list / null -> None."""
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    return item
        return None

    @staticmethod
    def _coerce(d: dict) -> dict:
        """Strict schema validation (#26). Unknown enum values become None so the
        structural comparator degrades to LOW unknown rather than confidently
        running on garbage."""
        from .structural import MODALITIES, OBJECT_POLARITY
        VALID_ENVS = {None, "prod", "staging", "dev"}
        VALID_REGIONS = {None, "eu", "us"}

        def _enum(value, allowed, normalize_fn=str.lower):
            if value is None:
                return None
            try:
                v = normalize_fn(str(value).strip())
            except Exception:
                return None
            return v if v in allowed else None

        def _date(value):
            if value is None:
                return None
            if not isinstance(value, str):
                return None
            v = value.strip()
            try:
                from datetime import date
                y, m, d_ = (int(p) for p in v.split("-"))
                date(y, m, d_)   # validates
                return v
            except Exception:
                return None

        def _obj(value):
            if value is None:
                return None
            v = str(value).lower().strip().replace(" ", "_")
            return v if v in OBJECT_POLARITY else None

        def _value(value):
            # Value-bearing object selector (colour/port/version/identifier) that
            # differentiates otherwise-identical prescriptions. Unlike `object` this
            # is NOT enum-constrained — dropping it (pre-fix the schema had no such
            # key) is exactly what let "blue canary" and "red canary" collapse to an
            # identical norm. Kept as a short lowercase free-text token, or None.
            if value is None:
                return None
            v = str(value).lower().strip()
            return v or None

        def _version(value):
            if value is None:
                return None
            v = str(value).lower().strip()
            # accept v1, v2, ... and 1, 2 -> v{n}
            if v.isdigit():
                v = f"v{v}"
            return v if v in {"v1", "v2", "v3", "v4"} else None

        scope = d.get("scope") if isinstance(d.get("scope"), dict) else {}
        modality = _enum(d.get("modality"), MODALITIES)
        return {
            "subject": str(d.get("subject") or "").lower().strip() or None,
            "modality": modality,
            "object": _obj(d.get("object")),
            "value": _value(d.get("value")),
            "scope": {
                "env":           _enum(scope.get("env"), VALID_ENVS),
                "service_class": (scope.get("service_class") or None) and str(scope["service_class"]).lower().strip(),
                "region":        _enum(scope.get("region"), VALID_REGIONS),
                "version":       _version(scope.get("version")),
            },
            "valid_from": _date(d.get("valid_from")),
            "valid_until": _date(d.get("valid_until")),
            "exception":   (d.get("exception") or None) and str(d["exception"]).strip(),
        }


class OllamaNormalizer(_LLMNormalizerBase):
    """Local instruct-LLM via Ollama (legacy / air-gapped path).

    Default base_url = http://localhost:11434 (no magic LAN IPs) (#28).
    Kept as a fallback for environments without outbound 443 to api.anthropic.com.
    """
    PROVIDER = "ollama"

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 cache_path: str | None = None, timeout: float = 120.0):
        super().__init__(
            model=model or os.environ.get("ERGO_LLM_MODEL", "qwen3:14b"),
            cache_path=cache_path,
            timeout=timeout,
        )
        self.base = (base_url or os.environ.get("ERGO_LLM_BASE")
                     or "http://localhost:11434").rstrip("/")

    def _chat(self, messages: list[dict]) -> str:
        import httpx
        payload = {"model": self.model, "messages": messages, "stream": False,
                   "format": "json", "think": False,
                   "options": {"temperature": 0, "num_ctx": 2048}}
        r = httpx.post(f"{self.base}/api/chat", json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["message"]["content"]


class AnthropicNormalizer(_LLMNormalizerBase):
    """Hosted instruct-LLM via Anthropic Messages API.

    Why: extraction quality > local 8–14B at a fraction of the image size.
    The same self-contained-image rule still applies for the NLI judge
    (baked, hot path) — but extraction tolerates ~1–2s/claim API latency and is
    much better with Claude than with qwen3:14b on real prose (esp. coreference).

    Requires: ANTHROPIC_API_KEY in env; outbound 443 to api.anthropic.com.

    ⚠️ STATUS 2026-06-26: kept, request shape proven via sibling, but this exact
    endpoint NOT live-validated against a working key.
    HOWEVER: the direct-hosted-API pattern this class uses was validated
    end-to-end the same day via its sibling `OpenAINormalizer` pointed straight at
    `https://api.openai.com/v1` with a real OpenAI key — clean schema-correct
    extraction incl. pronoun coreference. Since both classes extend
    `_LLMNormalizerBase` with identical prompt/_coerce()/cache and differ ONLY in
    `_chat()` (URL + auth header), a working Anthropic key will behave the same.
    The bug was always the dead key, never the code.
    PRODUCTION DEFAULT is `OpenAINormalizer` at the LiteLLM proxy
    (`ERGO_LLM_PROVIDER=litellm`), which speaks OpenAI shape and can route to
    Claude upstream. Do not delete this class — deleting just means re-adding
    identical code when a working direct key exists.
    """
    PROVIDER = "anthropic"
    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 cache_path: str | None = None, timeout: float = 60.0,
                 max_tokens: int = 1024):
        super().__init__(
            # claude-haiku-4-5 is the current cheap/fast tier. NOTE: older
            # "claude-3-5-haiku-*" ids 404 on accounts provisioned with the 4.x
            # lineup (verified 2026-06-26). Override via
            # ERGO_LLM_MODEL if your account exposes different ids.
            model=model or os.environ.get("ERGO_LLM_MODEL", "claude-haiku-4-5"),
            cache_path=cache_path,
            timeout=timeout,
        )
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "AnthropicNormalizer requires ANTHROPIC_API_KEY (env or constructor arg). "
                "For air-gapped deployments use OllamaNormalizer instead."
            )
        self.max_tokens = max_tokens

    def _chat(self, messages: list[dict]) -> str:
        import httpx
        # Anthropic Messages API: system goes in its own field, not in messages[].
        # Few-shots are passed as alternating user/assistant turns (already the
        # shape _LLMNormalizerBase._extract() builds).
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        chat = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_msg,
            "messages": chat,
            "temperature": 0,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }
        r = httpx.post(self.API_URL, headers=headers, json=payload, timeout=self.timeout)
        r.raise_for_status()
        # Messages API returns content as a list of blocks; text lives in type="text" blocks.
        body = r.json()
        parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()


class OpenAINormalizer(_LLMNormalizerBase):
    """OpenAI-shaped chat-completions endpoint (works with LiteLLM, OpenAI,
    OpenRouter, vLLM, llama.cpp server, anything that speaks /v1/chat/completions).

    Why this exists: when Anthropic's direct API isn't usable (key revoked, no
    console access, blocked at the network edge), a LiteLLM proxy in front of
    the *same* Claude family — or any other instruct model — speaks OpenAI shape
    on /v1/chat/completions. Same prompt, same _coerce(), drop-in.

    Auth: Bearer token via OPENAI_API_KEY env (set this to your LiteLLM master
    key when pointing at a LiteLLM proxy).
    Base URL: OPENAI_BASE_URL env (e.g. http://litellm.internal:4000/v1).
    """
    PROVIDER = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, cache_path: str | None = None,
                 timeout: float = 60.0, max_tokens: int = 1024):
        super().__init__(
            model=model or os.environ.get("ERGO_LLM_MODEL", "gpt-4o-mini"),
            cache_path=cache_path,
            timeout=timeout,
        )
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("ERGO_LLM_KEY")
        if not self.api_key:
            raise RuntimeError(
                "OpenAINormalizer requires OPENAI_API_KEY (or ERGO_LLM_KEY) in env. "
                "For a LiteLLM proxy, set this to LITELLM_MASTER_KEY."
            )
        self.base = (base_url or os.environ.get("OPENAI_BASE_URL")
                     or os.environ.get("ERGO_LLM_BASE")
                     or "https://api.openai.com/v1").rstrip("/")
        self.max_tokens = max_tokens

    def _chat(self, messages: list[dict]) -> str:
        import httpx
        # OpenAI /v1/chat/completions takes the system as the first message.
        # Our base._extract() already builds [system, user, asst, user, asst, user].
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        r = httpx.post(f"{self.base}/chat/completions",
                       headers=headers, json=payload, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        return body["choices"][0]["message"]["content"]


def LLMNormalizer(*args, **kwargs):   # noqa: N802  (factory, intentionally callable-like)
    """Backwards-compatible factory.

    Picks the backend via env: `ERGO_LLM_PROVIDER` in
    {anthropic, ollama, openai, litellm}. Default: anthropic if
    ANTHROPIC_API_KEY is set, else ollama. This preserves `LLMNormalizer()`
    as a drop-in for older call sites (tests, eval harness).
    """
    provider = (kwargs.pop("provider", None)
                or os.environ.get("ERGO_LLM_PROVIDER")
                or ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "ollama")).lower()
    if provider == "anthropic":
        return AnthropicNormalizer(*args, **kwargs)
    if provider == "ollama":
        return OllamaNormalizer(*args, **kwargs)
    if provider in ("openai", "litellm"):
        return OpenAINormalizer(*args, **kwargs)
    raise ValueError(
        f"Unknown ERGO_LLM_PROVIDER: {provider!r} "
        "(expected: anthropic | ollama | openai | litellm)"
    )

