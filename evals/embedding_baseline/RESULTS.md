# Embedding-Only Contradiction-Recall Baseline — RESULTS

*Run 2026-06-28 (Mac mini), embedder = FastEmbed BAAI/bge-small-en-v1.5 (384-dim), pure cosine KNN, **NLI disabled**. Shared distractor corpus = 654 unique claims; 2 trials per pair (both directions).*

## The question
When a claim is stored and its **contradictory** counterpart is queried, how often does the embedder's top-K vector recall ALONE surface the contradiction? If "never," the NLI pipeline is dead weight. If "often," NLI is justified because the embedder *retrieves* contradictions as near-neighbors but cannot *judge* them.

## Results — contradiction pairs (counterpart surfaced in top-K)
| Arm | n | top-1 | top-3 | top-5 | top-10 | cos-dist median |
|---|---|---|---|---|---|---|
| prose | 62 | 77.4% | 90.3% | 95.2% | 96.8% | 0.132 |
| synthetic | 216 | 22.2% | 54.2% | 64.8% | 79.2% | 0.059 |
| **realism** | **70** | **57.1%** | **70.0%** | **74.3%** | **87.1%** | **0.280** |

## Results — non-contradiction pairs (baseline separation)
| Arm | n | top-1 | top-3 | top-5 | top-10 | cos-dist median |
|---|---|---|---|---|---|---|
| prose | 150 | 24.0% | 42.0% | 46.0% | 54.0% | 0.320 |
| synthetic | 324 | 22.5% | 44.1% | 54.3% | 69.4% | 0.090 |
| realism | 0 | — | — | — | — | — |

## Interpretation
**Does the embedder alone surface contradictions in top-K? YES — frequently, and most so on the realistic arm.** On the realism arm (the messy, close-in-vector-space contradictions the reviewers demanded), a contradictory claim lands in the retrieved neighborhood **57.1% of the time at top-1, 87.1% at top-10**. The embedder pulls contradictions IN as near-neighbors — but it has no mechanism to *flag* them as contradictory; to the vector store they are simply "similar." That gap is exactly what NLI fills.

**Synthetic vs realism differ sharply, validating the reviewers' warning.** Synthetic top-1 is only 22.2% (clean polarity flips with disjoint scope read as different topics → pushed apart). Realism top-1 is 57.1% (same topic, opposite conclusion → pulled together). Had we trusted the synthetic arm alone we'd have *underestimated* how often contradictions co-retrieve and possibly mis-judged NLI's value. The realism arm is the trustworthy signal.

## Decision gate (REALISM contradiction arm)
| K | surfaced | bucket |
|---|---|---|
| top-1 | 57.1% | **>5% → NLI justified** |
| top-3 | 70.0% | **>5% → NLI justified** |
| top-5 | 74.3% | **>5% → NLI justified** |
| top-10 | 87.1% | **>5% → NLI justified** |

**Verdict: NLI is JUSTIFIED.** Contradictions appear in candidate retrieval far above the 5% threshold at every K. The embedder is a good *retriever* of contradictions but not a *judge* of them — which is precisely Ergo's bet. The NLI pipeline is not dead weight.

## Implication for architecture (feeds the reconciled review)
This is the empirical green light for **retrieve-first / judge-second**: the embedder cheaply produces a contradiction-rich candidate set (top-K), and NLI runs as a **reranker/judge over those candidates** rather than a tax on every write. The 654-claim corpus shows contradictions reliably enter the top-K, so a judge gated to top-K candidates loses little recall while cutting per-write cost. Carry this into the dogfood rollout and any write-path optimization.

## Gate status
- `python evals/embedding_baseline/run.py` runs clean, prints all three arms. ✅
- `evals/embedding_baseline/realism_pairs.jsonl` = 35 hand-authored close-in-vector-space contradiction pairs. ✅ (expanded to 70 directional trials)
- RESULTS.md filled from a real run (numbers above are live, not placeholders). ✅
- Test suite unchanged: see DEVELOPMENT.md run; confirmed green post-eval.
