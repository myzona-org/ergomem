"""Smoke tests for the LLMNormalizer factory + provider-scoped cache keys.

These tests do NOT hit the network. They check construction logic + cache-key
isolation only. End-to-end extraction lives in the eval harness.
"""
from __future__ import annotations
import os
import pytest

from ergo.contradiction import (
    LLMNormalizer, OllamaNormalizer, AnthropicNormalizer, OpenAINormalizer,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    # Isolate from the real shell env + give each test a private cache file.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ERGO_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ERGO_LLM_MODEL", raising=False)
    monkeypatch.delenv("ERGO_LLM_BASE", raising=False)
    monkeypatch.setenv("ERGO_LLM_CACHE", str(tmp_path / "cache.json"))
    yield


def test_factory_defaults_to_ollama_without_key():
    n = LLMNormalizer()
    assert isinstance(n, OllamaNormalizer)
    assert n.model == "qwen3:14b"
    assert n.base.startswith("http://localhost:11434")


def test_factory_defaults_to_anthropic_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    n = LLMNormalizer()
    assert isinstance(n, AnthropicNormalizer)
    assert n.model == "claude-haiku-4-5"


def test_explicit_provider_overrides_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ERGO_LLM_PROVIDER", "ollama")
    n = LLMNormalizer()
    assert isinstance(n, OllamaNormalizer)


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("ERGO_LLM_PROVIDER", "gemini")  # not implemented
    with pytest.raises(ValueError, match="Unknown ERGO_LLM_PROVIDER"):
        LLMNormalizer()


def test_anthropic_requires_api_key():
    # No key in env, asked explicitly for Anthropic -> hard fail
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicNormalizer()


def test_cache_keys_isolated_per_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    a = AnthropicNormalizer()
    o = OllamaNormalizer()
    # Same text, different providers -> different cache keys (no cross-contamination).
    assert a._key("hello") != o._key("hello")
    assert a._key("hello").startswith("anthropic::")
    assert o._key("hello").startswith("ollama::")


def test_provider_kwarg_takes_precedence(monkeypatch):
    # explicit constructor kwarg beats env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ERGO_LLM_PROVIDER", "anthropic")
    n = LLMNormalizer(provider="ollama")
    assert isinstance(n, OllamaNormalizer)


def test_openai_provider_routes_via_litellm(monkeypatch):
    # ERGO_LLM_PROVIDER=litellm is an alias for OpenAINormalizer (LiteLLM speaks
    # OpenAI shape on /v1/chat/completions).
    monkeypatch.setenv("ERGO_LLM_PROVIDER", "litellm")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-litellm-master")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://litellm.internal:4000/v1")
    n = LLMNormalizer()
    assert isinstance(n, OpenAINormalizer)
    assert n.base == "http://litellm.internal:4000/v1"


def test_openai_requires_api_key():
    # No OPENAI_API_KEY in env, asked for openai -> hard fail with hint about LiteLLM
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAINormalizer()


def test_openai_cache_key_distinct_from_ollama_and_anthropic(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    o = OllamaNormalizer()
    a = AnthropicNormalizer()
    p = OpenAINormalizer()
    keys = {o._key("x"), a._key("x"), p._key("x")}
    # all three providers must produce different cache keys for the same text
    assert len(keys) == 3


def test_parse_json_handles_provider_response_shapes():
    """The base _parse_json must survive the three real-world LLM response shapes."""
    from ergo.contradiction.normalize import _LLMNormalizerBase
    p = _LLMNormalizerBase._parse_json
    # raw JSON (OpenAI json-mode)
    assert p('{"subject": "x", "modality": "must"}')["subject"] == "x"
    # ```json fenced (Anthropic/Claude — no json-mode flag)
    assert p('```json\n{"subject": "y", "modality": "must_not"}\n```')["subject"] == "y"
    # bare ``` fence
    assert p('```\n{"subject": "z"}\n```')["subject"] == "z"
    # <think> preamble (reasoning models)
    assert p('<think>hmm let me think</think>\n{"subject": "w"}')["subject"] == "w"
    # prose around a single {...} block
    assert p('Here is the JSON: {"subject": "v", "modality": "may"} done.')["subject"] == "v"


@pytest.mark.parametrize("content,expected", [
    ('{"a":1}', '{"a":1}'),
    ('```json\n{"a":1}\n```', '{"a":1}'),
    ('```\n{"a":1}\n```', '{"a":1}'),
    ('<think>reasoning</think>{"a":1}', '{"a":1}'),
    ('<think>a</think>x<think>b</think>y', 'xy'),
    ('prose {"a":1} more', 'prose {"a":1} more'),
    ('[{"a":1}]', '[{"a":1}]'),
    ('I refuse.', 'I refuse.'),
    ('', ''),
    ('```   ```', ''),
    ('<think>unclosed', '<think>unclosed'),
    ('``` a ``` b ```', 'a'),
    ('text ```json  {"k":2} ``` tail', '{"k":2}'),
])
def test_think_strip_and_fence_extract_equivalence_table(content, expected):
    """Equivalence table (ReDoS fix, CodeQL alerts 3+4): the index-based
    _strip_think_blocks/_extract_fenced replacement for the old
    `<think>.*?</think>` / ```` ```(?:json)?\\s*(.*?)``` ```` regexes must
    produce byte-identical intermediate content for every shape an LLM
    response can take."""
    from ergo.contradiction.normalize import _strip_think_blocks, _extract_fenced

    stripped = _strip_think_blocks(content).strip()
    fenced = _extract_fenced(stripped)
    result = fenced.strip() if fenced is not None else stripped
    assert result == expected


def test_think_block_stripping_is_linear_time_not_quadratic():
    """ReDoS regression (CodeQL alert 3): `re.sub(r"<think>.*?</think>", ...)`
    rescans from every `<think>` occurrence for a close tag it will never
    find -- O(n^2). 8000 unterminated `<think>` tags took ~1s pre-fix; the
    linear replacement must clear a generous 1s ceiling."""
    import time
    from ergo.contradiction.normalize import _LLMNormalizerBase

    p = _LLMNormalizerBase._parse_json
    pathological = "<think>" * 8000
    start = time.monotonic()
    assert p(pathological) is None
    assert time.monotonic() - start < 1.0


def test_fence_extraction_is_linear_time_not_quadratic():
    """ReDoS regression (CodeQL alert 4): the fenced-block regex's greedy
    `\\s*` immediately followed by lazy `(.*?)` backtracks quadratically when
    no closing ``` ``` ``` ever appears -- ~1s pre-fix at n=20000 padding
    chars. (A purely-whitespace tail is trimmed by the preceding `.strip()`
    before the fence regex ever runs, so the padding needs a trailing
    non-whitespace character to actually reach the fence step pathologically.)
    The linear replacement must clear a generous 1s ceiling."""
    import time
    from ergo.contradiction.normalize import _LLMNormalizerBase

    p = _LLMNormalizerBase._parse_json
    pathological = "```" + " " * 20000 + "x"
    start = time.monotonic()
    assert p(pathological) is None
    assert time.monotonic() - start < 1.0


def test_parse_json_returns_none_on_refusal_instead_of_raising():
    """A model that emits no JSON object must yield None (caller degrades to an
    unknown norm) — NOT a JSONDecodeError that 500s the triggering write."""
    from ergo.contradiction.normalize import _LLMNormalizerBase
    p = _LLMNormalizerBase._parse_json
    # pure refusal prose (the real claude-haiku response on reference-doc text)
    assert p("This is documentation, not a policy rule. No JSON output.") is None
    # empty body
    assert p("") is None
    # bare scalar that happens to be valid JSON
    assert p("42") is None
    # empty array / array of non-objects
    assert p("[]") is None
    assert p('["a", "b"]') is None


def test_parse_json_takes_first_object_from_array():
    """Claude sometimes returns a list of claim objects for compound input;
    take the first object rather than choking on the array."""
    from ergo.contradiction.normalize import _LLMNormalizerBase
    p = _LLMNormalizerBase._parse_json
    assert p('[{"subject": "a"}, {"subject": "b"}]')["subject"] == "a"
    assert p('```json\n[{"subject": "c"}]\n```')["subject"] == "c"


def test_extract_degrades_to_unknown_norm_on_refusal(monkeypatch):
    """End-to-end: when _chat returns refusal prose, _extract must return a
    coerced all-unknown norm (no crash)."""
    from ergo.contradiction.normalize import _LLMNormalizerBase

    class _RefusingNorm(_LLMNormalizerBase):
        PROVIDER = "test"
        def _chat(self, messages):
            return "I cannot extract a policy claim from this. No JSON output."

    n = _RefusingNorm(model="x", cache_path=str(tmp_cache(monkeypatch)))
    norm, ok = n._extract("some reference text")
    assert norm["subject"] is None and norm["modality"] is None
    assert ok is False   # a refusal is a degrade -> normalize() must not cache it


def test_cache_file_corruption_is_ignored(tmp_path):
    from ergo.contradiction.normalize import _LLMNormalizerBase

    cache = tmp_path / "broken.json"
    cache.write_text("{not json")
    n = _LLMNormalizerBase(model="x", cache_path=str(cache))
    assert n._cache == {}


def test_parse_json_balanced_object_ignores_malformed_prefix_object():
    from ergo.contradiction.normalize import _LLMNormalizerBase

    body = 'bad prefix {"subject": "x",} then valid {"subject": "fallback"}'
    assert _LLMNormalizerBase._parse_json(body) is None


def test_ollama_chat_uses_json_mode(monkeypatch):
    requests = []

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"message": {"content": '{"subject":"x"}'}}

    def fake_post(url, json=None, timeout=None, **kwargs):
        requests.append((url, json, timeout, kwargs))
        return Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    n = OllamaNormalizer(model="qwen-test", base_url="http://ollama.test",
                         cache_path=str(tmp_cache(monkeypatch)), timeout=12)
    assert n._chat([{"role": "user", "content": "hi"}]) == '{"subject":"x"}'
    url, payload, timeout, _ = requests[-1]
    assert url == "http://ollama.test/api/chat"
    assert payload["format"] == "json"
    assert payload["think"] is False
    assert timeout == 12


def test_anthropic_chat_separates_system_message(monkeypatch):
    requests = []

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"type": "text", "text": '{"subject":"x"}'},
                                {"type": "tool_use", "text": "ignored"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        requests.append((url, headers, json, timeout))
        return Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    n = AnthropicNormalizer(api_key="sk-test", model="claude-test",
                            cache_path=str(tmp_cache(monkeypatch)), timeout=7,
                            max_tokens=55)
    out = n._chat([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "claim"},
    ])
    assert out == '{"subject":"x"}'
    url, headers, payload, timeout = requests[-1]
    assert url == AnthropicNormalizer.API_URL
    assert headers["x-api-key"] == "sk-test"
    assert payload["system"] == "rules"
    assert payload["messages"] == [{"role": "user", "content": "claim"}]
    assert payload["max_tokens"] == 55
    assert timeout == 7


def test_openai_chat_uses_bearer_and_json_object(monkeypatch):
    requests = []

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": '{"subject":"x"}'}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        requests.append((url, headers, json, timeout))
        return Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    n = OpenAINormalizer(api_key="sk-openai", base_url="http://openai.test/v1",
                         model="gpt-test", cache_path=str(tmp_cache(monkeypatch)),
                         timeout=9, max_tokens=66)
    messages = [{"role": "system", "content": "rules"}]
    assert n._chat(messages) == '{"subject":"x"}'
    url, headers, payload, timeout = requests[-1]
    assert url == "http://openai.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-openai"
    assert payload["messages"] == messages
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_tokens"] == 66
    assert timeout == 9


def tmp_cache(monkeypatch):
    import tempfile, pathlib
    return pathlib.Path(tempfile.mkdtemp()) / "llm_norm.json"
