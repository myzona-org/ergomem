"""Direct tests for the NLI judge (ergo/contradiction/nli.py).

Two layers:
  - Offline unit tests: huggingface_hub / onnxruntime / tokenizers are faked
    (same source-level patching as test_store.py), covering init plumbing
    (variant->file map, label map handling, env flags, truncation config),
    feed construction, the softmax, and the both-directions max.
  - Real-model integration tests against the actual ONNX model. They load with
    local_files_only=True and SKIP if the model is not already in the local HF
    cache — they never download.

The fail-loud check for a label map lacking 'contradiction' (#30) is pinned in
test_store.py::test_nli_label_map_validated_at_init.
"""
from __future__ import annotations
import json
import sys
import types

import numpy as np
import pytest

from ergo.contradiction.nli import NLIJudge


# --- fakes ----------------------------------------------------------------
class _FakeEncoding:
    """Deterministic, text-dependent ids so the fake session can key on them."""
    def __init__(self, premise: str, hypothesis: str):
        self.ids = [len(premise) % 97, len(hypothesis) % 97]
        self.attention_mask = [1] * len(self.ids)
        self.type_ids = [0] * len(self.ids)


class _FakeTokenizer:
    def __init__(self, truncation_raises: bool = False):
        self.truncation_raises = truncation_raises
        self.truncation_kwargs = None

    def enable_truncation(self, **kw):
        if self.truncation_raises:
            raise TypeError("no truncation support")
        self.truncation_kwargs = kw

    def encode(self, premise, hypothesis):
        return _FakeEncoding(premise, hypothesis)


class _FakeSession:
    """Returns logits keyed on the input_ids it was fed; records every feed."""
    def __init__(self, logits_by_ids=None, default=(0.0, 0.0, 0.0),
                 input_names=("input_ids", "attention_mask", "token_type_ids")):
        self.logits_by_ids = logits_by_ids or {}
        self.default = default
        self.input_names = input_names
        self.feeds = []

    def get_inputs(self):
        return [types.SimpleNamespace(name=n) for n in self.input_names]

    def run(self, _outputs, feed):
        self.feeds.append(feed)
        key = tuple(feed["input_ids"][0].tolist())
        logits = self.logits_by_ids.get(key, self.default)
        return [np.array([logits], dtype=np.float32)]


@pytest.fixture
def nli_env(monkeypatch, tmp_path):
    """Fake the three heavy deps; tests may swap state.session/.tokenizer/.cfg
    before constructing NLIJudge (imports happen inside __init__)."""
    state = types.SimpleNamespace(
        cfg={"id2label": {"0": "contradiction", "1": "entailment", "2": "neutral"}},
        downloads=[],
        session=_FakeSession(),
        tokenizer=_FakeTokenizer(),
    )

    def fake_download(repo, fname, **kw):
        state.downloads.append((repo, fname, kw))
        p = tmp_path / fname.replace("/", "_")
        p.write_text(json.dumps(state.cfg) if fname == "config.json" else "{}")
        return str(p)

    # patch huggingface_hub at the SOURCE — nli.py imports inside __init__
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    monkeypatch.setitem(
        sys.modules, "onnxruntime",
        types.SimpleNamespace(InferenceSession=lambda *a, **k: state.session))

    class _TokCls:
        @staticmethod
        def from_file(path):
            return state.tokenizer

    monkeypatch.setitem(sys.modules, "tokenizers",
                        types.SimpleNamespace(Tokenizer=_TokCls))
    monkeypatch.delenv("ERGO_NLI_CACHE", raising=False)
    monkeypatch.delenv("ERGO_NLI_LOCAL_ONLY", raising=False)
    return state


# --- init plumbing --------------------------------------------------------
@pytest.mark.parametrize("variant,fname", [
    ("full", "onnx/model.onnx"),
    ("quantized", "onnx/model_quantized.onnx"),
    ("int8", "onnx/model_int8.onnx"),
    ("fp16", "onnx/model_fp16.onnx"),
])
def test_variant_selects_expected_onnx_file(nli_env, variant, fname):
    NLIJudge(variant=variant)
    requested = [f for _, f, _ in nli_env.downloads]
    assert requested == ["config.json", "tokenizer.json", fname]


def test_unknown_variant_raises_before_any_download(nli_env):
    with pytest.raises(KeyError):
        NLIJudge(variant="bf16")
    assert nli_env.downloads == []


def test_missing_id2label_falls_back_to_default_map(nli_env):
    nli_env.cfg = {}
    j = NLIJudge()
    assert j.id2label == {0: "contradiction", 1: "entailment", 2: "neutral"}


def test_label_map_is_int_keyed_and_lowercased(nli_env):
    nli_env.cfg = {"id2label": {"0": "ENTAILMENT", "1": "Neutral", "2": "CONTRADICTION"}}
    j = NLIJudge()
    assert j.id2label == {0: "entailment", 1: "neutral", 2: "contradiction"}


def test_env_cache_dir_and_local_only_are_forwarded(nli_env, monkeypatch):
    monkeypatch.setenv("ERGO_NLI_CACHE", "/tmp/nli-cache")
    monkeypatch.setenv("ERGO_NLI_LOCAL_ONLY", "true")
    NLIJudge()
    for _, _, kw in nli_env.downloads:
        assert kw == {"cache_dir": "/tmp/nli-cache", "local_files_only": True}


def test_local_only_defaults_off_without_env(nli_env):
    NLIJudge()
    for _, _, kw in nli_env.downloads:
        assert kw == {"cache_dir": None}


def test_explicit_local_files_only_beats_env(nli_env, monkeypatch):
    monkeypatch.setenv("ERGO_NLI_LOCAL_ONLY", "1")
    NLIJudge(local_files_only=False)
    for _, _, kw in nli_env.downloads:
        assert "local_files_only" not in kw


def test_truncation_configured_with_max_length(nli_env):
    NLIJudge(max_length=128)
    assert nli_env.tokenizer.truncation_kwargs == {
        "max_length": 128, "strategy": "longest_first"}


def test_tokenizer_without_truncation_support_is_tolerated(nli_env):
    nli_env.tokenizer = _FakeTokenizer(truncation_raises=True)
    j = NLIJudge()  # must not raise (#31 try/except)
    assert j.max_length == 512


# --- inference ------------------------------------------------------------
def test_feed_contains_only_the_session_input_names(nli_env):
    nli_env.session = _FakeSession(input_names=("input_ids", "attention_mask"))
    j = NLIJudge()
    j.scores("premise text", "hypothesis text")
    (feed,) = nli_env.session.feeds
    assert set(feed) == {"input_ids", "attention_mask"}
    assert all(a.dtype == np.int64 for a in feed.values())


def test_scores_is_softmax_of_logits_mapped_to_labels(nli_env):
    nli_env.session = _FakeSession(default=(1.0, 2.0, 3.0))
    j = NLIJudge()
    s = j.scores("a", "b")
    e = np.exp(np.array([1.0, 2.0, 3.0]) - 3.0)
    expected = e / e.sum()
    assert s["contradiction"] == pytest.approx(expected[0])
    assert s["entailment"] == pytest.approx(expected[1])
    assert s["neutral"] == pytest.approx(expected[2])
    assert sum(s.values()) == pytest.approx(1.0)


def test_softmax_is_numerically_stable_for_large_logits(nli_env):
    nli_env.session = _FakeSession(default=(1000.0, 1001.0, 1002.0))
    j = NLIJudge()
    s = j.scores("a", "b")
    assert all(np.isfinite(v) for v in s.values())
    assert sum(s.values()) == pytest.approx(1.0)


def test_scores_respects_the_model_label_order(nli_env):
    nli_env.cfg = {"id2label": {"0": "entailment", "1": "neutral", "2": "contradiction"}}
    nli_env.session = _FakeSession(default=(0.0, 0.0, 5.0))
    j = NLIJudge()
    s = j.scores("a", "b")
    assert s["contradiction"] > s["entailment"] and s["contradiction"] > s["neutral"]


def test_contradiction_score_takes_max_over_both_orderings(nli_env):
    a, b = "aa", "bbb"  # fake tokenizer keys on lengths: (2,3) vs (3,2)
    nli_env.session = _FakeSession(logits_by_ids={
        (2, 3): (0.0, 0.0, 0.0),   # a->b: contradiction 1/3
        (3, 2): (5.0, 0.0, 0.0),   # b->a: contradiction ~0.99
    })
    j = NLIJudge()
    expected = max(j.scores(a, b)["contradiction"], j.scores(b, a)["contradiction"])
    assert j.contradiction_score(a, b) == pytest.approx(expected)
    assert j.contradiction_score(a, b) > 0.9  # the b->a direction must win


# --- real model (skips if not in the local HF cache; never downloads) ------
# Every test below takes this fixture and is marked `real_model` (registered in
# pyproject.toml) so the host-side release gate can deselect them with
# `-m 'not real_model'` — the quantized-NLI numeric
# instability across CPU/runtime is environment-sensitive (see the
# onnxruntime/tokenizers pin comment), and the authoritative real-model
# verification already runs in-container via evals/prose/evaluate.py. The
# default `pytest` invocation (CI's `test` job included) still collects and
# runs them; they just skip gracefully when the model isn't cached.
@pytest.fixture(scope="module")
def real_judge():
    try:
        return NLIJudge(local_files_only=True)
    except Exception as exc:  # noqa: BLE001 — hub raises several error types
        pytest.skip(f"NLI model not in local HF cache: {exc}")


@pytest.mark.real_model
def test_real_model_labels_and_probability_shape(real_judge):
    assert set(real_judge.id2label.values()) == {"contradiction", "entailment", "neutral"}
    s = real_judge.scores("TLS must be enabled in prod.", "TLS is required in prod.")
    assert sum(s.values()) == pytest.approx(1.0, abs=1e-4)
    assert all(0.0 <= v <= 1.0 for v in s.values())


@pytest.mark.real_model
def test_real_model_flags_clear_contradiction(real_judge):
    score = real_judge.contradiction_score(
        "TLS must be enabled in prod.", "TLS must not be enabled in prod.")
    assert score > 0.8


@pytest.mark.real_model
def test_real_model_low_on_same_subject_neutral_pairs(real_judge):
    for a, b in [
        ("TLS must be enabled in prod.", "TLS certificates are renewed yearly."),
        ("The API server uses Postgres.", "The API server logs to stdout."),
    ]:
        assert real_judge.contradiction_score(a, b) < 0.2, (a, b)


@pytest.mark.real_model
def test_real_model_overfires_on_unrelated_pairs_so_judge_gates_are_load_bearing(real_judge):
    """Characterization, not aspiration: the raw cross-encoder scores unrelated
    cross-domain pairs as near-certain contradictions in at least one direction
    (and contradiction_score takes the max). Measured 0.9997 on the docstring's
    own 'neutral' smoke pair. This is the false-positive class the judge's
    subject/polarity/overlap gates exist to suppress — if this test ever starts
    failing, the model got better and the gates may be loosenable."""
    score = real_judge.contradiction_score(
        "Dashboards refresh every 30s.", "Secrets rotate every 90 days.")
    assert score > 0.5


@pytest.mark.real_model
def test_real_model_low_on_entailment_pair(real_judge):
    score = real_judge.contradiction_score(
        "All services must emit logs.", "Services should emit logs.")
    assert score < 0.2
