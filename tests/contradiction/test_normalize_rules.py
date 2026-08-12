"""Tests for the deterministic normalizer internals.

Complements test_normalize_factory.py (which covers provider selection + the
_parse_json response-shape handling). Here we pin:
  - RuleNormalizer field extraction (modality/subject/object/scope/time/value)
  - _LLMNormalizerBase._coerce strict schema validation (unknown enum -> None)
  - _shift_date exclusive-boundary date arithmetic
  - _LLMNormalizerBase caching (cache hit skips _chat; persists to disk)
All pure logic — no network, no real LLM.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ergo.contradiction.normalize import (
    RuleNormalizer, NormalizeUnavailable, _LLMNormalizerBase, _shift_date,
)


# track_context=False so module-level _norm() calls stay stateless/per-claim and
# don't leak antecedents between unrelated tests. Coref tests below use their own
# instances / explicit context.
N = RuleNormalizer(track_context=False)


def _norm(text: str) -> dict:
    return N.normalize({"text": text})


# --------------------------------------------------------------------------
# RuleNormalizer — modality
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("X must not be used.", "must_not"),
    ("X shall not be used.", "must_not"),
    ("X may not be used.", "must_not"),
    ("X should not be used.", "should_not"),
    ("Caching is not recommended in prod.", "should_not"),
    ("X must be enabled.", "must"),
    ("TLS is required to be enabled.", "must"),
    ("X should be enabled.", "should"),
    ("X is recommended to be enabled.", "should"),
    ("X may be used.", "may"),
    ("X is permitted to be used.", "may"),
])
def test_modality_extraction(text, expected):
    assert _norm(text)["modality"] == expected


def test_negative_modality_wins_over_bare_must():
    # "must not" must be detected before the bare "must" alternative
    assert _norm("X must not be removed.")["modality"] == "must_not"


def test_no_modal_defaults_to_must():
    assert _norm("Caching enabled in prod.")["modality"] == "must"


# --------------------------------------------------------------------------
# RuleNormalizer — subject signature
# --------------------------------------------------------------------------
def test_subject_drops_stopwords_and_scope_leadin():
    assert _norm("Traffic in the eu region must be blocked.")["subject"] == "traffic eu region"


def test_subject_is_lowercased_text_before_modality():
    assert _norm("TLS 1.2 must be enabled.")["subject"] == "tls 1.2"


# --------------------------------------------------------------------------
# RuleNormalizer — object verb
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,obj", [
    ("X must be enabled.", "enabled"),
    ("X must be disabled.", "disabled"),
    ("X must be blocked.", "blocked"),
    ("X must be removed.", "removed"),
])
def test_object_verb_extraction(text, obj):
    assert _norm(text)["object"] == obj


def test_object_falls_back_to_off_and_on():
    assert _norm("Feature flags must be turned off in dev.")["object"] == "off"
    assert _norm("Feature flags must be on in dev.")["object"] == "on"


# --------------------------------------------------------------------------
# RuleNormalizer — scope (env / service_class / region / version)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("env", ["prod", "staging", "dev"])
def test_scope_env_detected(env):
    assert _norm(f"X must be enabled in {env}.")["scope"]["env"] == env


def test_scope_service_class_detected():
    assert _norm("X must be enabled for public-facing services.")["scope"]["service_class"] == "public-facing"


def test_scope_region_requires_the_word_region():
    assert _norm("Access from the us region must be allowed.")["scope"]["region"] == "us"
    # a bare "eu" without the token "region" must NOT be read as a region
    assert _norm("Traffic in eu must be blocked.")["scope"]["region"] is None


def test_scope_version_detected():
    assert _norm("The v2 flow must be used.")["scope"]["version"] == "v2"


# --------------------------------------------------------------------------
# RuleNormalizer — time boundaries (exclusive: after=+1, before=-1)
# --------------------------------------------------------------------------
def test_until_sets_inclusive_valid_until():
    assert _norm("Caching must be disabled until 2026-12-01.")["valid_until"] == "2026-12-01"


def test_before_shifts_valid_until_back_one_day():
    assert _norm("X may be used before 2026-01-01.")["valid_until"] == "2025-12-31"


def test_after_shifts_valid_from_forward_one_day():
    assert _norm("X must not be used after 2026-09-30.")["valid_from"] == "2026-10-01"


@pytest.mark.parametrize("kw", ["from", "starting", "as of"])
def test_from_style_keywords_set_exact_valid_from(kw):
    assert _norm(f"X must be enforced {kw} 2026-05-01.")["valid_from"] == "2026-05-01"


# --------------------------------------------------------------------------
# RuleNormalizer — exception + value
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,frag", [
    ("X must be blocked, no exceptions.", "no exceptions"),
    ("X must be blocked except in dev.", "except in dev"),
    ("X must be blocked unless approved.", "unless approved"),
])
def test_exception_extracted(text, frag):
    assert _norm(text)["exception"] == frag


def test_value_extracts_port_number():
    assert _norm("The gateway must bind to port 8443.")["value"] == "8443"


def test_value_extracts_colour():
    assert _norm("Deploys must use a blue canary.")["value"] == "blue"


def test_value_port_takes_precedence_over_colour():
    assert _norm("The blue gateway must use port 9090.")["value"] == "9090"


# --------------------------------------------------------------------------
# RuleNormalizer — pronoun coreference (rule-based, no LLM)
# --------------------------------------------------------------------------
def _coref(text: str, context):
    # stateless instance; resolution driven entirely by the explicit context list
    return RuleNormalizer(track_context=False).normalize({"text": text}, context=context)


def test_coref_it_resolves_to_recent_singular():
    out = _coref("It must not be cleared on startup.", context=["cache"])
    assert out["subject"] == "cache"
    assert out["modality"] == "must_not"


def test_coref_they_resolves_to_recent_plural():
    out = _coref("They must be rotated every 90 days.", context=["api tokens"])
    assert out["subject"] == "api tokens"


def test_coref_this_resolves_to_recent_singular():
    out = _coref("This must be disabled.", context=["v1 auth flow"])
    assert out["subject"] == "v1 auth flow"
    assert out["object"] == "disabled"


def test_coref_no_antecedent_leaves_pronoun_unresolved():
    # THE anti-hallucination case: with no antecedent the extractor must NOT
    # invent a subject — it leaves the bare pronoun rather than guessing wrong.
    out = _coref("It must be enabled.", context=[])
    assert out["subject"] == "it"


def test_coref_number_mismatch_leaves_pronoun_unresolved():
    # "they" (plural) has no plural-compatible antecedent ("cache" is singular),
    # so it is conservatively left unresolved rather than mis-bound.
    out = _coref("They must be rotated.", context=["cache"])
    assert out["subject"] == "they"


def test_coref_picks_most_recent_compatible_antecedent():
    # two singular antecedents; "it" binds to the most recent one.
    out = _coref("It must be removed.", context=["the old cache", "the v2 token"])
    assert out["subject"] == "the v2 token"


def test_coref_determiner_pronoun_is_not_treated_as_coref():
    # "this rule" is already a noun phrase, not a standalone pronoun -> untouched.
    out = _coref("This rule must be enforced.", context=["cache"])
    assert out["subject"] == "this rule"


def test_coref_rolling_session_state_across_calls():
    # without explicit context, a shared instance threads recent subjects through.
    n = RuleNormalizer()
    n.normalize({"text": "The cache must be enabled in prod."})
    assert n.normalize({"text": "It must not be cleared on startup."})["subject"] == "cache"


def test_coref_unresolved_pronoun_is_not_remembered_as_antecedent():
    # an unresolved pronoun must never become a future antecedent.
    n = RuleNormalizer()
    n.normalize({"text": "It must be enabled."})           # unresolved, not stored
    assert n.normalize({"text": "They must be rotated."})["subject"] == "they"


# --------------------------------------------------------------------------
# RuleNormalizer — inline-code / backtick masking (extraction not corrupted)
# --------------------------------------------------------------------------
def test_inline_code_modal_substring_does_not_hijack_modality():
    # `must-revalidate` contains "must"; without masking the modal scan binds to
    # that identifier and shreds the subject. Masking makes the REAL "must" win.
    out = _norm("The `must-revalidate` directive must be enabled.")
    assert out["modality"] == "must"
    assert out["object"] == "enabled"
    assert "must-revalidate" in out["subject"]
    assert "directive" in out["subject"]


def test_inline_code_backticks_stripped_from_subject():
    out = _norm("The `--no-cache` flag must not be used.")
    assert "`" not in out["subject"]
    assert "--no-cache" in out["subject"]
    assert out["modality"] == "must_not"
    assert out["object"] == "used"


def test_inline_code_value_inside_span_still_extracted():
    out = _norm("The gateway must bind to `port 8443`.")
    assert out["value"] == "8443"
    assert out["subject"] == "gateway"


def test_inline_code_object_word_in_code_subject_does_not_leak():
    # "disabled" sits inside the code subject; the real object is "removed".
    out = _norm("The `cache.disabled` setting must be removed.")
    assert out["object"] == "removed"
    assert "cache.disabled" in out["subject"]


# --------------------------------------------------------------------------
# _coerce — strict schema validation
# --------------------------------------------------------------------------
_coerce = _LLMNormalizerBase._coerce


def test_coerce_unknown_modality_becomes_none():
    assert _coerce({"modality": "totally-bogus"})["modality"] is None


def test_coerce_known_modality_passes():
    assert _coerce({"modality": "must_not"})["modality"] == "must_not"


def test_coerce_unknown_object_becomes_none():
    assert _coerce({"object": "frobnicate"})["object"] is None


def test_coerce_value_is_lowercased_and_empty_is_none():
    assert _coerce({"value": "BLUE"})["value"] == "blue"
    assert _coerce({"value": "   "})["value"] is None
    assert _coerce({"value": None})["value"] is None


def test_coerce_version_digit_is_promoted_and_out_of_range_dropped():
    assert _coerce({"scope": {"version": "2"}})["scope"]["version"] == "v2"
    assert _coerce({"scope": {"version": "v3"}})["scope"]["version"] == "v3"
    assert _coerce({"scope": {"version": "9"}})["scope"]["version"] is None


def test_coerce_invalid_date_becomes_none_valid_kept():
    out = _coerce({"valid_from": "2026-13-40", "valid_until": "2026-09-30"})
    assert out["valid_from"] is None
    assert out["valid_until"] == "2026-09-30"


def test_coerce_invalid_env_and_region_become_none():
    out = _coerce({"scope": {"env": "mars", "region": "moon"}})
    assert out["scope"]["env"] is None
    assert out["scope"]["region"] is None


def test_coerce_missing_scope_yields_all_none_scope():
    out = _coerce({})
    assert out["scope"] == {"env": None, "service_class": None,
                            "region": None, "version": None}
    assert out["subject"] is None


def test_coerce_trims_and_lowercases_text_fields():
    out = _coerce({"subject": "  TLS  ", "scope": {"service_class": "Public"},
                   "exception": "  except in dev  "})
    assert out["subject"] == "tls"
    assert out["scope"]["service_class"] == "public"
    assert out["exception"] == "except in dev"


# --------------------------------------------------------------------------
# _shift_date
# --------------------------------------------------------------------------
@pytest.mark.parametrize("iso,days,expected", [
    ("2026-09-30", 1, "2026-10-01"),   # month rollover
    ("2026-01-01", -1, "2025-12-31"),  # year rollover backward
    ("2026-12-31", 1, "2027-01-01"),   # year rollover forward
    ("2026-06-15", 0, "2026-06-15"),
])
def test_shift_date_arithmetic(iso, days, expected):
    assert _shift_date(iso, days) == expected


def test_shift_date_invalid_input_passes_through():
    assert _shift_date("not-a-date", 1) == "not-a-date"


# --------------------------------------------------------------------------
# _LLMNormalizerBase caching (via a stub that records _chat calls)
# --------------------------------------------------------------------------
class _StubLLM(_LLMNormalizerBase):
    PROVIDER = "stub"

    def __init__(self, cache_path, reply=None):
        super().__init__(model="m", cache_path=str(cache_path))
        self.calls = 0
        self._reply = reply or '{"subject":"x","modality":"must","object":"enabled"}'

    def _chat(self, messages):
        self.calls += 1
        return self._reply


def test_cache_hit_skips_second_chat(tmp_path):
    n = _StubLLM(tmp_path / "c.json")
    n.normalize({"text": "the same claim"})
    n.normalize({"text": "the same claim"})
    assert n.calls == 1
    n.normalize({"text": "a different claim"})
    assert n.calls == 2


def test_cache_persists_to_disk_across_instances(tmp_path):
    cache = tmp_path / "c.json"
    first = _StubLLM(cache)
    first.normalize({"text": "persist me"})
    assert json.loads(cache.read_text())  # written through

    second = _StubLLM(cache)  # fresh instance reads the persisted cache
    second.normalize({"text": "persist me"})
    assert second.calls == 0  # served from disk cache, no _chat


@pytest.mark.parametrize("break_call", ["mkstemp", "replace"])
def test_cache_write_failure_is_best_effort_not_fatal(tmp_path, monkeypatch, break_call):
    """Review finding (2026-07-13): a disk-full/permission OSError while persisting
    the cache (tempfile.mkstemp or os.replace) must NOT propagate out of
    normalize() and turn a successfully-extracted norm into a failure — the
    cache is an optimization, not part of the extraction contract. normalize()
    must still return the real extracted norm even when the cache write
    fails."""
    import ergo.contradiction.normalize as normalize_mod

    if break_call == "mkstemp":
        monkeypatch.setattr(normalize_mod.tempfile, "mkstemp",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    else:
        monkeypatch.setattr(normalize_mod.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")))

    cache = tmp_path / "c.json"
    n = _StubLLM(cache)
    out = n.normalize({"text": "cache write will fail"})
    assert out["subject"] == "x"       # the real extracted norm, not swallowed
    assert out["modality"] == "must"
    assert out["object"] == "enabled"
    assert not cache.exists()          # the failed write never landed on disk
    # a FRESH instance (no in-memory cache) must re-extract, since nothing
    # was ever persisted -- the failed disk write didn't silently succeed.
    monkeypatch.undo()
    second = _StubLLM(cache)
    second.normalize({"text": "cache write will fail"})
    assert second.calls == 1


def test_extract_runs_coerce_on_chat_output(tmp_path):
    # _chat returns an unknown modality + a fenced JSON body; normalize() must
    # parse it AND coerce the bad enum to None.
    n = _StubLLM(tmp_path / "c.json",
                 reply='```json\n{"subject":"X","modality":"bogus","object":"enabled"}\n```')
    out = n.normalize({"text": "claim"})
    assert out["subject"] == "x"
    assert out["modality"] is None      # coerced from "bogus"
    assert out["object"] == "enabled"


# --------------------------------------------------------------------------
# Fail CLOSED on provider transport errors (finding
# #3 — supersedes the fail-open-on-every-exception behavior / audit
# findings #5+#6). Malformed model OUTPUT still fails open
# (unchanged) — see the dedicated test at the bottom.
# --------------------------------------------------------------------------
class _FlakyLLM(_LLMNormalizerBase):
    """A stub whose _chat fails the first `fail_times` calls, then succeeds.

    Models a provider that restarts / rate-limits (429) / drops the connection
    for a moment. This is exactly the provider INFRASTRUCTURE failure class
    that must now fail CLOSED (raise NormalizeUnavailable), not degrade.
    """
    PROVIDER = "flaky"

    def __init__(self, cache_path, fail_times=1, exc=None):
        super().__init__(model="m", cache_path=str(cache_path))
        self.calls = 0
        self.fail_times = fail_times
        self._exc = exc or httpx.ConnectError("connection refused")

    def _chat(self, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self._exc
        return '{"subject":"x","modality":"must","object":"enabled"}'


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("timeout"),
    httpx.HTTPStatusError("429", request=httpx.Request("POST", "http://x"),
                          response=httpx.Response(429)),
])
def test_transport_error_fails_closed_with_normalize_unavailable(tmp_path, exc):
    # A transport error / non-2xx from the provider must now raise
    # NormalizeUnavailable instead of degrading to an all-unknown norm — the
    # write-guard (store.py) fails the guarded write CLOSED (503), it must
    # never silently skip the contradiction check.
    n = _FlakyLLM(tmp_path / "c.json", fail_times=99, exc=exc)
    with pytest.raises(NormalizeUnavailable):
        n.normalize({"text": "auth tokens must never be logged"})


@pytest.mark.parametrize("status", [429, 500, 503, 401, 403])
def test_retryable_http_status_fails_closed(tmp_path, status):
    # 429 (rate-limit), 5xx, and auth 401/403 ARE the infra-failure class --
    # transient/availability problems worth a 503-and-retry.
    exc = httpx.HTTPStatusError(str(status), request=httpx.Request("POST", "http://x"),
                                response=httpx.Response(status))
    n = _FlakyLLM(tmp_path / "c.json", fail_times=99, exc=exc)
    with pytest.raises(NormalizeUnavailable) as ei:
        n.normalize({"text": "auth tokens must never be logged"})
    assert ei.value.retryable is True


@pytest.mark.parametrize("status", [400, 404, 422])
def test_permanent_http_status_fails_closed_not_open(tmp_path, status):
    """re-review #3: _chat() calls raise_for_status(), which raises
    httpx.HTTPStatusError for EVERY non-2xx, including permanent client errors
    (400 bad request, 404 unknown model, 422) that are the provider answering
    with a definitive, non-transient rejection -- not an availability problem.
    The PRIOR behavior (fail OPEN to an all-unknown norm, same as a malformed
    2xx body) was the exact integrity hole reproduced here: a bad model name
    returning 404 stored a guarded write with NO contradiction check. A
    permanent 4xx must now fail CLOSED too, just marked non-retryable
    (server.py maps this to 502, distinct from the 503 retryable class)."""
    exc = httpx.HTTPStatusError(str(status), request=httpx.Request("POST", "http://x"),
                                response=httpx.Response(status))
    n = _FlakyLLM(tmp_path / "c.json", fail_times=99, exc=exc)
    with pytest.raises(NormalizeUnavailable) as ei:
        n.normalize({"text": "auth tokens must never be logged"})
    assert ei.value.retryable is False


@pytest.mark.parametrize("exc", [
    KeyError("choices"),
    IndexError("list index out of range"),
    TypeError("'NoneType' object is not subscriptable"),
    json.JSONDecodeError("Expecting value", "not json", 0),
])
def test_malformed_response_shape_from_chat_still_fails_open(tmp_path, exc):
    """Review finding (2026-07-13): _chat() raises on a malformed/unexpected
    response BODY too (e.g. OpenAINormalizer's `body["choices"][0]["message"]
    ["content"]`, OllamaNormalizer's `r.json()["message"]["content"]`) — a
    KeyError/IndexError/TypeError/JSONDecodeError from parsing an otherwise-
    successful (2xx) reply. That is NOT a provider infrastructure failure; the
    provider answered, just not usefully, so it must still fail OPEN to an
    all-unknown norm, NOT raise NormalizeUnavailable/503.
    Only httpx.HTTPError (connection/timeout/status/auth) is infra-class."""
    n = _FlakyLLM(tmp_path / "c.json", fail_times=99, exc=exc)
    out = n.normalize({"text": "auth tokens must never be logged"})
    assert out["subject"] is None
    assert out["modality"] is None
    assert out["object"] is None


def test_normalize_unavailable_from_transport_error_is_not_cached(tmp_path):
    # A one-bad-minute failure must not permanently poison the statement's norm:
    # nothing is cached on the failing call, so a retry re-extracts and gets
    # the real norm once the provider recovers.
    cache = tmp_path / "c.json"
    n = _FlakyLLM(cache, fail_times=1)   # first call fails, second succeeds

    with pytest.raises(NormalizeUnavailable):
        n.normalize({"text": "auth tokens must never be logged"})
    assert n.calls == 1
    assert not cache.exists()            # nothing persisted to disk on failure

    second = n.normalize({"text": "auth tokens must never be logged"})
    assert n.calls == 2                  # re-ran _chat, NOT served from cache
    assert second["subject"] == "x"      # got the real norm on retry
    assert json.loads(cache.read_text())["flaky::m::auth tokens must never be logged"]["subject"] == "x"


def test_refusal_norm_is_not_cached_either(tmp_path):
    # Malformed model OUTPUT (a reply WAS received, but _parse_json -> None:
    # refusal prose / garbled body) is UNCHANGED — still fails OPEN to an
    # all-unknown norm and is never cached, so a later good
    # reply wins. This is the class NormalizeUnavailable does NOT cover.
    cache = tmp_path / "c.json"

    class _RefuseThenAnswer(_LLMNormalizerBase):
        PROVIDER = "flaky"

        def __init__(self):
            super().__init__(model="m", cache_path=str(cache))
            self.calls = 0

        def _chat(self, messages):
            self.calls += 1
            if self.calls == 1:
                return "I cannot help with that."   # no JSON -> parse None
            return '{"subject":"x","modality":"must","object":"enabled"}'

    n = _RefuseThenAnswer()
    first = n.normalize({"text": "some claim"})
    assert first["subject"] is None      # fails open, not NormalizeUnavailable
    second = n.normalize({"text": "some claim"})
    assert n.calls == 2
    assert second["subject"] == "x"


# --------------------------------------------------------------------------
# Thread-safe shared cache (finding #5): concurrent cache misses must not
# race the same fixed tmp path.
# --------------------------------------------------------------------------
def test_concurrent_cache_misses_do_not_race_the_tmp_file(tmp_path):
    """Pre-fix, `_save()` wrote to a FIXED `cache_path + '.tmp'`. Two threads
    racing a cache miss could both target that same tmp path; whichever
    thread's `os.replace()` ran second found the file already consumed by the
    first and raised FileNotFoundError -> 500 (reproduced with 8 concurrent
    threads). The fix (a unique tempfile.mkstemp per write + a cache lock)
    must let every thread's write land cleanly."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache = tmp_path / "c.json"

    class _SlowLLM(_LLMNormalizerBase):
        PROVIDER = "slow"

        def _chat(self, messages):
            time.sleep(0.01)  # widen the race window
            return '{"subject":"x","modality":"must","object":"enabled"}'

    n = _SlowLLM(model="m", cache_path=str(cache))
    errors = []

    def work(i):
        try:
            n.normalize({"text": f"claim number {i}"})
        except Exception as e:  # pragma: no cover - only hit pre-fix
            errors.append(e)

    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in as_completed([ex.submit(work, i) for i in range(8)]):
            f.result()

    assert not errors, f"concurrent cache save raised: {errors!r}"
    assert len(json.loads(cache.read_text())) == 8
    # no leftover tmp files from a crashed write
    assert list(tmp_path.glob("*.tmp")) == []
