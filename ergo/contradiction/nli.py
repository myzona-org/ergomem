"""Local cross-encoder NLI judge via ONNX Runtime — no torch.

Default model: Xenova/nli-deberta-v3-base (ONNX export of
cross-encoder/nli-deberta-v3-base). Runs on CPU
through onnxruntime + tokenizers, so it works on Python 3.14 where torch has no
wheels, and bakes cleanly into an air-gapped image.

Labels (from the model's config.json id2label) are typically
contradiction / entailment / neutral. NLI is directional, so we judge both
orderings and take the max contradiction probability.
"""
from __future__ import annotations
import json
import os

from . import metrics


class NLIJudge:
    """Local cross-encoder NLI judge via ONNX Runtime.

    Hardened after a 2026-06-25 review:
      - Label map validated at init; missing 'contradiction' fails loud (#30).
      - Max-length truncation applied at tokenization (#31).
      - `local_files_only` honored when set in env / passed in for air-gapped
        deployments where runtime HF downloads must fail at startup (#29).
    """
    def __init__(self, repo: str = "Xenova/nli-deberta-v3-base",
                 variant: str = "quantized", cache_dir: str | None = None,
                 local_files_only: bool | None = None, max_length: int = 512):
        from huggingface_hub import hf_hub_download
        import onnxruntime as ort
        from tokenizers import Tokenizer

        cache_dir = cache_dir or os.environ.get("ERGO_NLI_CACHE")
        if local_files_only is None:
            local_files_only = os.environ.get("ERGO_NLI_LOCAL_ONLY", "").lower() in ("1", "true", "yes")
        fname = {
            "full": "onnx/model.onnx",
            "quantized": "onnx/model_quantized.onnx",
            "int8": "onnx/model_int8.onnx",
            "fp16": "onnx/model_fp16.onnx",
        }[variant]

        dl_kwargs = {"cache_dir": cache_dir}
        if local_files_only:
            dl_kwargs["local_files_only"] = True
        cfg_path = hf_hub_download(repo, "config.json", **dl_kwargs)
        tok_path = hf_hub_download(repo, "tokenizer.json", **dl_kwargs)
        model_path = hf_hub_download(repo, fname, **dl_kwargs)

        cfg = json.load(open(cfg_path))
        id2label = cfg.get("id2label") or {"0": "contradiction", "1": "entailment", "2": "neutral"}
        self.id2label = {int(k): v.lower() for k, v in id2label.items()}
        # validate the label map up front (#30)
        labels = set(self.id2label.values())
        if "contradiction" not in labels:
            raise RuntimeError(
                f"NLIJudge: model {repo!r} label map lacks 'contradiction'; got {labels}")

        self.tok = Tokenizer.from_file(tok_path)
        # truncate inputs to a known max length (#31)
        try:
            self.tok.enable_truncation(max_length=max_length, strategy="longest_first")
        except Exception:
            pass
        self.sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.sess.get_inputs()}
        self.repo, self.variant, self.max_length = repo, variant, max_length

    def _logits(self, premise: str, hypothesis: str):
        import numpy as np
        enc = self.tok.encode(premise, hypothesis)
        feed = {}
        if "input_ids" in self.input_names:
            feed["input_ids"] = np.array([enc.ids], dtype=np.int64)
        if "attention_mask" in self.input_names:
            feed["attention_mask"] = np.array([enc.attention_mask], dtype=np.int64)
        if "token_type_ids" in self.input_names:
            feed["token_type_ids"] = np.array([enc.type_ids], dtype=np.int64)
        out = self.sess.run(None, feed)[0][0]
        return out

    def scores(self, premise: str, hypothesis: str) -> dict:
        import numpy as np
        x = self._logits(premise, hypothesis)
        e = np.exp(x - x.max())
        p = e / e.sum()
        return {self.id2label[i]: float(p[i]) for i in range(len(p))}

    def contradiction_score(self, a: str, b: str) -> float:
        with metrics.time_stage("nli"):
            s1 = self.scores(a, b)
            s2 = self.scores(b, a)
        return max(s1.get("contradiction", 0.0), s2.get("contradiction", 0.0))


if __name__ == "__main__":
    # smoke test: a clear contradiction and a clear entailment
    j = NLIJudge()
    print("model:", j.repo, j.variant, "| labels:", j.id2label, "| inputs:", j.input_names)
    pairs = [
        ("TLS must be enabled in prod.", "TLS must not be enabled in prod."),  # contradiction
        ("All services must emit logs.", "Services should emit logs."),         # entail-ish
        ("Dashboards refresh every 30s.", "Secrets rotate every 90 days."),     # unrelated — raw model over-fires (~1.0); see test_nli.py
    ]
    for a, b in pairs:
        print(f"\nA: {a}\nB: {b}")
        print("  a->b:", {k: round(v, 3) for k, v in j.scores(a, b).items()})
        print("  contradiction_score:", round(j.contradiction_score(a, b), 3))
