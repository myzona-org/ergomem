"""Ergo contradiction engine.

Pipeline: normalize -> structural compare -> NLI fallback -> confidence tiers.

  normalize.py  -- text -> structured claim (modality, subject, scope, time, exception)
  structural.py -- deterministic compare over normalized fields (the precision win)
  nli.py        -- local cross-encoder NLI via ONNX Runtime (no torch); fuzzy residual
  judge.py      -- combines the above into one (label, score, tier) verdict
"""
from .structural import compare, Verdict, HIGH, LOW
from .normalize import (
    GoldNormalizer, RuleNormalizer, LLMNormalizer,
    OllamaNormalizer, AnthropicNormalizer, OpenAINormalizer,
)
from .judge import ContradictionJudge

__all__ = ["compare", "Verdict", "HIGH", "LOW",
           "GoldNormalizer", "RuleNormalizer", "LLMNormalizer",
           "OllamaNormalizer", "AnthropicNormalizer", "OpenAINormalizer",
           "ContradictionJudge"]
