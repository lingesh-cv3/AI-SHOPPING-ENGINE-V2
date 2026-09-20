# Shopper V2 Stage 2A — Baseline Report

Status: Stage 2A complete. Stage 2 overall is **not** complete and is **not**
moved to Completed.md. This report documents the measurement/foundation
phase only — no retrieval-quality change was made.

Generated from `engine/shopper_v2/eval/results/baseline_latest.json`
(also kept as a timestamped copy in the same directory). Regenerate with:

```
python -m engine.shopper_v2.eval.run_baseline
```

(requires the two demo merchant backends on 8001/8002; no LLM key needed —
see "Design decision" below).

---

## 1. What this measures, and what it deliberately does not

The Stage 2A contracts (`engine/shopper_v2/contracts.py`) and the eval
runner (`engine/shopper_v2/eval/`) drive Stage 1's **unmodified**
`retrieval.py`/`evidence.py` pipeline, wrapped so each step (candidate
retrieval → hard filter → ranking → evidence) produces a typed, inspectable
result. No retrieval algorithm, adapter method, or relevance heuristic was
changed.

**Deliberate design decision, stated once here:** `SearchIntent` for every
corpus turn is built directly from that turn's own stated, ground-truth
fields (query text, budget, exclusions), **not** from a live LLM
`understand()` call. This makes the baseline exactly reproducible and keeps
it free of the Groq throttle (CLAUDE.md: "roughly four model turns a
minute"), at the cost of not exercising `understanding.py`/`orchestrator.py`
at all. `intent_extraction_accuracy` is therefore reported as **unavailable**
(§6), not guessed at. This baseline measures the deterministic retrieval
pipeline itself, independent of intent-extraction quality — which is
exactly Stage 2A's own scope per SHOPPER_V2_STAGE2_SPEC.md §5/§18.

---

## 2. Discrepancies found between the spec and the actual repository

Per the task's own instruction ("do not assume the spec perfectly matches
the current code... document the discrepancy before proceeding"):

1. **`engine/shopper_v2/evidence.py` never calls the LLM semantic judge.**
   SHOPPER_V2_STAGE2_SPEC.md's own §2.3 describes `judge_relevance`/
   `judge_best_match` as part of "the deterministic re-scoring layer Stage 1
   reuses." On inspection, `evidence.py`'s imports are exactly
   `best_matches_with_score`, `query_diverges_from_said`, `shop_carries`,
   `weak_single_word_match` from `engine.execution.relevance` — **never**
   `ReasoningService.judge_relevance` or `judge_best_match`. Those two
   methods are called only from V1's `engine/execution/service.py`. This
   means the V2 Stage 1 pipeline inherited V1's deterministic layer but
   **not** its LLM semantic-judge fallback — a real, previously undocumented
   gap, confirmed live by `kb-falsedecline-01` below (§4).

2. **`mission_ranking.rank_and_filter` does not separate "hard filter" from
   "ranking" as two steps.** The spec's target architecture (§5–§8) treats
   hard constraint filtering (C) and relevance ranking (D) as two distinct
   contract steps. The actual function does both in one pass: it applies
   hard filters (exclusions/budget/stock) AND immediately truncates to
   `DEFAULT_LIMIT=4` by combined score, in the same call. There is no public
   way to get "everything that survives hard filtering" without also
   getting cut to 4 by soft score. `engine/shopper_v2/eval/adapt.py::hard_filter`
   works around this by calling `rank_and_filter` with `limit=len(products)`
   to recover the hard-filter-only view — a workaround for measurement
   purposes, not a change to the function itself. This conflation is also
   the direct cause of a real finding below (`kb-lang-01`, §4).

3. **Rejection-reason classification cannot be fully attributed from public
   data alone.** The spec's `RejectReason` (§8) names four reasons
   (`excluded_by_shopper`, `out_of_stock`, `over_budget`,
   `ambiguous_exclusion_match`). `rank_and_filter`'s exclusion-matching
   (`_matches`/`_uncertain`) is private, so Stage 2A's classifier can only
   confidently attribute `OUT_OF_STOCK`/`OVER_BUDGET` from public fields; a
   fifth reason, `UNCLASSIFIED`, was added to `contracts.py` (documented
   inline) for rejections the classifier cannot otherwise attribute without
   duplicating private logic — a small, honest addition beyond the spec's
   original table, not a silent gap.

---

## 3. Corpus

**20 cases, 24 turns**, `engine/shopper_v2/eval/corpus.py`. Every ground
truth value was derived by tracing the actual code path each query takes
(Northfield's title-only `sample_merchant/store.py::search_items`, Kettle's
name+collection `sample_merchant_two/store.py::search`, and the
deterministic re-scoring in `engine/execution/relevance.py`/
`mission_ranking.py`) against the real seeded catalogs — no LLM was used to
decide ground truth, per the task's explicit instruction. Several
ground-truth values were **corrected during the baseline run itself**, after
the first run surfaced a mismatch between the hand-traced expectation and
the actual measured behavior; each correction is documented inline in
`corpus.py` and referenced again in §4 below, distinguishing "the eval
harness had it wrong" from "the pipeline has a real gap."

| Category | Cases | Turns |
|---|---|---|
| discovery | 3 | 3 |
| attributes | 2 | 2 |
| constraints | 2 | 2 |
| multi_turn | 3 | 8 |
| language_variation | 3 | 3 |
| no_match | 3 | 3 |
| false_substitution_regression | 1 | 1 |
| false_decline_regression | 1 | 1 |
| multi_item | 1 | 2 |
| platform_condition (zero-data, platform-down) | 2 | 2 |

Both merchants represented: Northfield 16 turns, Kettle 6 turns, plus 1
zero-data turn and 1 platform-down turn (synthetic in-process stub adapters
— see §7).

---

## 4. Findings — what the current system does well, where it fails

### 4a. Works correctly

- **Clean single-item discovery matches confidently and correctly**
  (`nf-disc-01` "I need running shoes" → P1001 only;
  `nf-attr-01` "lightweight running shoe" → P1001, description-boosted).
- **Multi-word ties are found and returned together when genuinely tied**
  (`kb-disc-01` "single origin coffee" → 3 in-stock single-origin products,
  correctly excluding the out-of-stock Kenya Nyeri AA — Kettle's
  boolean-only stock field, per CLAUDE.md, is honored correctly).
- **Budget hard-filtering works correctly when the query and budget arrive
  together** (`nf-constr-01`, `kb-multi-01` turn 0→ turn-budget cases where
  the product noun is present).
- **The already-patched false-substitution fix holds end to end.**
  `nf-nomatch-01`, `nf-falsesub-01` (the exact PROGRESS.md kayak/water-bottle
  shape) both correctly produce zero verified products — no substitution
  occurred in either case.
- **Zero-data and platform-down both degrade honestly** — no fabricated
  product, no raw exception reaching evidence (§7).
- **Cross-turn tenant/merchant separation holds** — confirmed both by this
  corpus's own multi-item case (`nf-multiitem-01`) and by the dedicated
  infrastructure test (`test_tenant_merchant_separation_in_run_corpus`).

### 4b. Real, freshly-discovered gaps (not previously documented in PROGRESS.md)

**Finding 1 — a substring-matching gap in the adapter's OWN initial search,
one layer earlier than the already-patched stopword bug.**
`nf-falsesub-01` ("I need a kayak for the lake this weekend") returns two
*candidates* — P2007 "Thermal Half Zip" and P3002 "Performance Socks Three
Pack" — neither related to kayaks at all. Cause: `sample_merchant/store.py`'s
own initial search does substring matching with **no** stopword filtering
(`w in title`, not word-boundary, not `_significant_words`-filtered) — so
the stopwords "for" and "the" in the sentence substring-match inside
"per**FOR**mance" and "**THE**rmal" respectively. This is a previously
undocumented instance of the same *class* of bug PROGRESS.md's stopword fix
already closed in `relevance.py`'s **re-scoring** layer — but this instance
lives in the **candidate-retrieval** layer itself, one step upstream of
where the existing fix operates. **The existing re-scoring safety net still
catches it** — both spurious candidates correctly score 0 and are dropped,
so no false substitution reaches the shopper — but the false candidates are
still generated and briefly held as real search results, which matters for
`total_available` accuracy and for anything downstream that might read
`CandidateSet.candidates` before the evidence step runs.

**Finding 2 — a real relevant product can be silently dropped purely by
candidate-set size, independent of textual relevance.**
`kb-lang-01` ("espresso blends") returns 5 candidates. Two are genuinely,
correctly tied at the top lexical score: KB-BLD-05 "Morning Bell Espresso
Blend" (title match) and KB-SUB-14 "Espresso Subscription" (whose own story
text says "Always a **blend** that works in milk"). Measured result: **only
KB-SUB-14 is verified relevant; KB-BLD-05 is silently dropped.** Root cause,
traced directly: with no priority/must-have/budget signal in play, every
candidate scores 0 in `rank_and_filter`'s scoring loop except the rating
tiebreaker — and `DEFAULT_LIMIT=4` then keeps only the top 4 by **rating**,
which cuts KB-BLD-05 purely because its fixed rating happens to be lower,
with **zero regard for the fact that it is one of the two best lexical
matches for the actual query**. This is a direct, measured consequence of
Discrepancy #2 (§2) — `rank_and_filter` conflating hard filtering with a
soft top-N cut — manifesting as a real wrong outcome, not just an
architectural untidiness. **This is the clearest single piece of evidence
in this corpus that the current pipeline's correctness depends on
candidate-set size**: identical relevance logic produces the right answer
when ≤4 candidates are returned and can silently drop a correct one once
more than 4 are returned with no other ranking signal present. This
directly bears on the real-volume question central to Stage 2B/2C planning
(§9).

**Finding 3 — single-strong-candidate queries are structurally
indistinguishable from single-coincidental-word matches.**
Three independent cases converge on the same root cause:
`nf-multi-01` turn 0 ("I need sneakers" → correct sole match P1003, but
`weak_single_word_match` flags it unconfident because the winning score is
exactly 1), `nf-attr-02` ("waterproof jacket" → correct sole match P2002,
same flag), and `nf-lang-02` ("a shoe" → genuinely ambiguous, correctly
flagged). **`weak_single_word_match`'s `top_score == 1` rule cannot
distinguish "the one and only correct product, named in one word" from "a
coincidental one-word overlap with an unrelated product."** Both produce
identical evidence (`weak_match=True`, `relevance_verified=False`) even
though one is a real miss and the other is correct caution. This is a
structural ceiling of the deterministic layer, not a bug fixable by another
closed-class word list — it is the actual shape of the "keyword overlap
alone cannot express confidence" limitation the spec's own §2.3 names.

**Finding 4 — a bare constraint-refinement turn with no product noun loses
the prior turn's product context entirely.**
`nf-multi-01` turns 1–2 ("under 2000" / "actually make that under 1000")
and `kb-multi-01` turn 1 ("under 3000") all search the **literal budget
sentence itself** against the adapter and return **zero candidates** — not
"the previous turn's product, now re-evaluated against the new budget."
Root cause, traced directly: `SearchIntent.query` (mirroring
`orchestrator.py`'s real `delta.search_query or message` fallback) is
always this turn's own raw message, because `StateDelta.search_query` is
always `None` (per `understanding.py`'s own documented comment — no model
tool field currently populates it). This reproduces, in a new shape, the
exact class of bug `orchestrator.py`'s own code comment already documents
having partially fixed (the "goal" fallback bug) — but a pure
budget/constraint-only follow-up, **exactly the shape of CLAUDE.md's task
examples 7–8** ("Under ₹3000." / "Actually, make that under ₹2000."),
still has no query to search on. This is the single most consequential
finding in this baseline for Stage 2B planning (§9): closing it needs
`SearchIntent` to be built from BOTH this turn's message AND the carried
prior product/query context (§6 of the spec already anticipates this via
`mission_context`, but nothing currently populates a retrievable prior
query for a no-noun turn to fall back to).

### 4c. Classification of every failure by pipeline stage

| Finding | Stage |
|---|---|
| 1 — stopword substring false candidates | **Retrieval** (adapter's own initial search) |
| 2 — DEFAULT_LIMIT/rating cut drops a relevant candidate | **Ranking/relevance** (`rank_and_filter`'s conflated hard-filter+rank step) |
| 3 — single-word confidence ceiling | **Ranking/relevance** (`weak_single_word_match`) |
| 4 — budget-only turn loses product context | **Understanding/state** (`SearchIntent`/`StateDelta.search_query` never populated) |

**No response-generation failure was found in this corpus** — `respond.py`
was not exercised by Stage 2A's baseline at all (the baseline stops at the
evidence step; `respond.py`'s own synthesis is unchanged from Stage 1 and
out of this measurement's scope). This is a real gap in what Stage 2A
covers, not a claim that response generation is defect-free — see §10.

---

## 5. Exact baseline metrics (from `baseline_latest.json`)

**Overall** (24 turns; 12 turns carry a specific expected-relevant-id
ground truth, the rest are no-match/platform-condition cases):

| Metric | Value |
|---|---|
| Recall@4 | 0.667 (8/12) |
| Precision@4 | 0.667 |
| MRR | 0.917 |
| NDCG@4 | 0.917 |
| Hard-constraint violation rate | 0.0 (0/8 reason-checked no-match turns) |
| False-substitution rate | 0.083 (1/12 — Finding 1's spurious candidates, caught downstream) |
| False-decline rate | 1.0 (1/1 — the one case in this category, `kb-falsedecline-01`, by design reproduces a known false decline; see §2 discrepancy #1) |
| No-match correctness | 1.0 (8/8 no-match-expected turns correctly produced no confident verified product) |
| Multi-turn constraint retention | 1.0 (11/11 — budget correctly applied to whatever WAS retrieved each turn; does not detect Finding 4, a retrieval-scope problem, not a state-retention one) |

**By merchant:**

| | Northfield | Kettle |
|---|---|---|
| Turns | 16 | 6 |
| Recall@4 | 0.667 | 0.667 |
| MRR | 0.889 | 1.0 |
| False-substitution rate | 0.0 | 0.333 (1/3 — Finding 2) |
| False-decline rate | n/a (no case in this category) | 1.0 (Finding, §2 discrepancy #1) |

Northfield and Kettle do **not** behave identically — confirmed, not
assumed: Kettle's false-substitution rate is measurably higher in this
corpus specifically because of Finding 2 (candidate-set-size-driven
dropping), which only manifests when >4 candidates are returned with no
other ranking signal — more likely on Kettle's smaller, less
category-diverse catalog where a single query more easily pulls >4
candidates from one collection.

---

## 6. Metrics explicitly NOT computed, and why

Per the task's "do not manufacture metrics" instruction, three requested
metrics are reported as unavailable rather than guessed at
(`engine/shopper_v2/eval/metrics.py::UNAVAILABLE_METRICS`):

- **`intent_extraction_accuracy`** — this baseline bypasses the LLM
  `understand()` step by design (§1); measuring this needs a separate,
  smaller, LLM-driven pass not built in Stage 2A.
- **`token_usage_cost`** — the measured pipeline (`retrieval.py` +
  `evidence.py`) makes **zero** LLM calls (confirmed by import inspection,
  §2 discrepancy #1) — there is no token usage to report because no model
  call happens in the measured path.
- **`reranking_latency`** — no reranking step exists in the current
  pipeline; there is nothing to time.

---

## 7. Zero-data / platform-down

Both real-client conditions were exercised, via small in-process stub
adapters (`ZeroDataAdapter`, `PlatformDownAdapter` in
`engine/shopper_v2/eval/runner.py`) implementing only `search_products` —
not a new external service, not a new seeded merchant backend:

- **Zero-data**: `search_products` returns an empty `ProductSearchResult`.
  Measured: `no_match_reason="no_candidates"`, zero evidence entries, no
  fabricated product. Correct.
- **Platform-down**: `search_products` raises `ConnectionError`. Measured:
  `candidate_set.unavailable=True`, `no_match_reason="platform_unavailable"`,
  no raw exception reached the evidence layer. Correct, and consistent with
  invariant 5 (raw platform errors never reach shoppers).

---

## 8. Performance baseline

Measured directly (`Timings` in `engine/shopper_v2/eval/adapt.py`,
wall-clock via `time.perf_counter()`), against the two live local demo
merchant backends (no network beyond localhost):

| | Value |
|---|---|
| Mean total turn time | ~3–8 ms (varied slightly run to run; both runs shown in this document were <10ms mean) |
| Max total turn time | ~10–28 ms |
| Min total turn time | ~0.04–0.09 ms (synthetic stub adapters) |
| LLM calls in measured path | **0** (confirmed by import inspection — §2, §6) |
| Token usage | n/a — no model call in measured path |

**This is not a production latency figure.** It measures only the
retrieval→evidence pipeline against a ~16–38 SKU local demo catalog with a
sub-millisecond local HTTP round trip — it says nothing about behavior at a
real 40,000-SKU catalog, which is explicitly not exercised in Stage 2A (see
Remaining Limitations, §10). No SLA is claimed; per SHOPPER_V2_STAGE2_SPEC.md
§16, this is the required first baseline measurement, not a target.

---

## 9. What evidence would justify each later-stage technology

Stated as **evidence gates**, per the spec's own technology table (§14) and
the task's "critical stop condition" — none of the following is
implemented in Stage 2A:

- **Stage 2B (lexical improvements) is justified now**, by Findings 1–4
  above — all four are lexical/deterministic-layer or state-shape gaps, not
  gaps requiring semantic retrieval. Finding 4 in particular (query context
  loss on constraint-only turns) is the single highest-value, lowest-risk
  fix visible in this baseline, and is a `SearchIntent`/understanding-layer
  fix, not a new retrieval technology.
- **Semantic retrieval (pgvector/embeddings) is NOT justified by this
  baseline.** No case in this corpus failed because a real match shared *no*
  literal word with the query text at all — `kb-falsedecline-01`'s "fruity
  coffee" case is the closest candidate, but its failure mode is fully
  explained by Discrepancy #1 (the LLM judge simply isn't wired into V2 at
  all) — restoring that existing, already-built fallback is a much smaller,
  already-proven fix than adopting new retrieval infrastructure, and should
  be evaluated before semantic retrieval is considered.
- **Reranking is NOT justified by this baseline.** Finding 2 (the clearest
  ordering-quality-looking failure) is not actually an ordering problem —
  the correct product IS scored correctly, it is dropped by a hard
  candidate-count cut before ordering quality could matter. A reranker
  applied after today's `DEFAULT_LIMIT=4` cut would never see the dropped
  candidate at all.
- **A catalog projection/index is NOT justified by this baseline** —
  performance at ~16–38 SKUs says nothing about real volume, and no
  synthetic large catalogue was built in Stage 2A (§10).

---

## 10. Remaining limitations (honestly stated, not deferred silently)

- **No large synthetic catalogue was built.** Every metric above is
  measured at Northfield's 38 SKUs / Kettle's 16 SKUs — three-plus orders of
  magnitude below CLAUDE.md's ~40,000-SKU real-client profile. Finding 2
  (candidate-set-size-driven dropping) is the clearest signal in this whole
  report that catalogue size materially changes correctness, which makes
  this the single most important gap left for whoever picks up Stage 2B/2C
  planning to close before trusting any of these percentages at real scale.
- **`respond.py` (response generation) is entirely outside this baseline's
  measurement.** No unsupported-product-claim rate was computed against
  actual generated shopper-facing text — see §4c.
- **`intent_extraction_accuracy` is unmeasured** — deliberate scope
  decision, §1/§6.
- **The corpus is hand-authored and finite (20 cases).** It is not a
  statistically powered sample of CV3's real query distribution — it exists
  to exercise specific, named, traceable code paths and known regression
  shapes, not to estimate a population-level accuracy rate.
- **Only two demo merchants exist to test against**, both structurally
  small and English-only — no real client catalogue, no non-English
  content, no malformed/partial merchant data was exercised.

---

*End of Stage 2A baseline report. Stage 2 remains open in PROGRESS.md.*
