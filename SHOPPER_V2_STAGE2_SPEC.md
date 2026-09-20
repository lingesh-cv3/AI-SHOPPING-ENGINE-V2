# Shopper V2, Stage 2 — Specification (research/spec only, no implementation)

Status: DRAFT for review. No code has been written or modified for Stage 2.
Stage 1 (`cfc2b46`) is complete, committed, and untouched by this document.

This is a specification, not an implementation plan with code attached. Every
claim about the repository below was checked against the actual files named;
every claim about external retrieval practice is labelled as general industry
practice, not benchmarked against this codebase.

---

## 1. Executive Summary

Stage 1 proved the architecture: `message -> Understanding -> StateDelta ->
ShoppingStateV2 -> Context -> Retrieval -> Evidence -> Response`, with the
model's pre-fetch reply structurally discarded (`engine/shopper_v2/understanding.py`)
so no shopper-facing text is ever written before the real product data
exists. That closes the reply-before-data defect for the V2 path.

Stage 1's `Retrieval` step (`engine/shopper_v2/retrieval.py`) is a thin,
deliberately unambitious wrapper: it calls the adapter's existing
`search_products(query)` and returns whatever comes back, honestly marked
`unavailable=True` on adapter failure. All actual relevance work is done
downstream in `evidence.py`, which reuses V1's existing, heavily-patched
deterministic re-scoring (`engine/execution/relevance.py`,
`engine/execution/mission_ranking.py`) — a keyword/stem word-overlap scorer
with roughly twenty accumulated point-fixes (stopwords, plurals, hyphenation,
title-citation detection, query-divergence detection), plus an optional LLM
semantic judge (`judge_relevance`, `judge_best_match`) that degrades to `None`
("could not confirm") when the model is unavailable.

This works, and the accumulated patches are not wasted — several encode real,
previously-reproduced failure modes (the "kayak"/"Insulated Water Bottle"
false substitution; the Kettle "fruity coffee" varietal-naming gap) that any
replacement must not regress. But the shape is now brittle by construction:
correctness depends on an ever-growing, hand-maintained set of closed-class
word lists (stopwords, generic action verbs, intensifier prefixes, a
three-entry water-resistant/waterproof table) layered onto a literal
substring/word-overlap search that both demo merchants themselves implement
(`sample_merchant/store.py`, `adapters/kettle/adapter.py`'s underlying store).
Every new failure mode found in production is fixed by adding another
narrow, closed-class rule — which is exactly the shape CLAUDE.md's roadmap
section asks Stage 2 not to repeat.

Stage 2's job is not to add a twenty-first patch. It is to give Shopper V2 a
retrieval layer with an explicit typed contract at each step (intent →
candidates → hard filter → ranked/reranked → verified evidence), built to the
actual scale, latency, and correctness constraints this repository has
(SQLite, a throttled free-tier LLM, ~40-SKU demo catalogues standing in for a
40,000-SKU real one, two structurally different adapters), not to a generic
RAG blueprint.

---

## 2. Current Retrieval Architecture (as it exists today, verified in-repo)

### 2.1 The call chain

```
engine/shopper_v2/orchestrator.py::run_turn
  -> retrieval.retrieve(adapter, query)          # engine/shopper_v2/retrieval.py
       -> adapter.search_products(query, limit=20)
  -> evidence.build_evidence(...)                 # engine/shopper_v2/evidence.py
       -> mission_ranking.rank_and_filter(...)     # engine/execution/mission_ranking.py
       -> relevance.best_matches_with_score(...)   # engine/execution/relevance.py
       -> relevance.query_diverges_from_said(...)
       -> relevance.weak_single_word_match(...)
       -> relevance.shop_carries(...)              # only on zero verified matches
  -> respond.respond(...)                          # engine/shopper_v2/respond.py
       -> reasoning.synthesize(kind="discovery", items=verified_products, ...)
```

`retrieval.py` (48 lines) does nothing beyond calling
`adapter.search_products` and unwrapping `.products` from the returned
`ProductSearchResult`. No caching, no new adapter method, no embeddings, no
new search infrastructure — by design, per its own docstring.

### 2.2 What "search" actually means today, per adapter

- **Northfield** (`sample_merchant/store.py::search_items`): literal
  word-split match against **title only** (`product["item_title"].lower()`,
  `dept` field for department filter). No description or category text is
  searched. Plural handling is a bare `w.rstrip("s")`. Ranked by raw hit
  count, catalog order as tiebreak. `total_available` = `match_count`, but
  `search_products` is always called with a fixed `limit` and Stage 1 never
  reads `total_available`.
- **Kettle** (`sample_merchant_two`, behind GraphQL): same underlying
  substring-style search exposed through a `{ products }` GraphQL query;
  `matchCount` returned separately from the page. Same "search is
  deliberately bad" design intent stated in the adapters' own docstrings.
- Both adapters' `capabilities()` explicitly declare `"search_kind":
  "keyword_substring"` and `"semantic_search": False` — this is a stated,
  declared platform capability, not an assumption Stage 2 has to guess at.

### 2.3 The deterministic re-scoring layer Stage 1 reuses (not owns)

`engine/execution/relevance.py` (429 lines) and
`engine/execution/mission_ranking.py` (435 lines) are V1 modules, already
hardened through roughly 20 documented "gap fixes" (see PROGRESS.md's Known
Issues history — TWELFTH-, FIFTEENTH-, SIXTEENTH-, NINETEENTH-,
TWENTIETH-gap references throughout). Their actual capabilities, verified by
reading the code (not assumed):

- **Word-overlap re-scoring** (`best_matches_with_score`): scores each
  already-fetched candidate by count of shared "significant words" (>2
  chars, not in a 27-word closed stopword list, singular/plural/`-es`/`-ies`
  stemmed via a hand-written `_stem`), keeps only the top-scoring tier.
- **Divergence detection** (`query_diverges_from_said`): flags when the
  model's search query contains a word the shopper never said, unless that
  query is a near-exact title citation (`_is_real_title_citation`, allowing
  at most 1-word symmetric difference from a real candidate's title).
- **Weak-match flag** (`weak_single_word_match`): true whenever the winning
  overlap score is exactly 1, regardless of query length.
- **Category-plausibility fallback** (`shop_carries`): checks whether a
  catalogue sample's categories or full product text overlap the query at
  all, excluding a 15-word closed list of generic call-to-action verbs
  ("drink", "use", "try"...) from being sufficient alone. Used only to decide
  what to *say* on a zero-match turn, not to find products.
- **Hard/soft filtering** (`rank_and_filter`): exclusions are a hard filter
  (with a 1-entry "related but not equivalent" table — `waterproof` vs.
  `water-resistant` — treated as neither confirmed-excluded nor
  confirmed-safe); must-have/constraint/priority terms are additive soft
  score (`_matches`, a 6-character prefix "stem" plus a hand-written
  intensifier-prefix list); budget is a hard filter on parsed `Decimal`
  price; `OUT_OF_STOCK` is a hard filter; rating is a tiebreaker only.
  Returns top `DEFAULT_LIMIT = 4`.
- **Optional LLM semantic judge** (`ReasoningService.judge_relevance`,
  `judge_best_match`, `engine/reasoning/service.py:436-524`): single-boolean
  or single-verified-id tool calls, used only as a fallback when the
  deterministic path is inconclusive, and returns `None` (never a guess) on
  any LLM unavailability — a second, independent model call per gated turn.

None of this is a single "relevance score" function; it's four cooperating
checks (`hard_passed`, `relevant = in best_ids and not (diverges or weak)`,
category plausibility, and an optional LLM call), each individually patched
against a specific reproduced failure. `evidence.py::build_evidence` is where
Stage 1 wires these together — it does not add new relevance logic, only a
typed wrapper (`ProductEvidence`, `TurnEvidence`) around calling them.

### 2.4 Scale, as actually seeded

- Northfield (`sample_merchant/seed/catalog.py`): 38 SKUs, one department
  field, no structured attributes beyond title/description/price/stock/size
  variants.
- Kettle (`sample_merchant_two/seed/catalog.py`): 16 SKUs, similarly
  unstructured (single-origin/blend category, whole-bean/ground/1kg
  variants), stock as boolean only (no quantity).
- Real CV3 clients: ~50 mid-market merchants on Magento/Shopware/custom
  builds, per CLAUDE.md, with catalogues up to ~40,000 SKUs and six figures
  of cases. **Neither demo catalogue is within three orders of magnitude of
  that.** No synthetic large catalogue currently exists in this repository —
  confirmed by `find`/`grep` across `sample_merchant*` and `tests/`; nothing
  named `synthetic`, `large_catalog`, `40000`, or similar was found.

### 2.5 Storage and provider, as actually configured

- Persistence: SQLite via `aiosqlite` + SQLAlchemy 2.0 (confirmed via `pip
  list` against the project's `.venv`). No Postgres, no pgvector, no
  Elasticsearch/OpenSearch, no vector store, no `numpy`/`scikit-learn`/
  `sentence-transformers`/`faiss` installed anywhere in the current
  environment.
- Model access: an OpenAI-compatible client (`openai` package installed),
  pointed at a provider CLAUDE.md names as the free Groq tier, throttled to
  "roughly four model turns a minute" — this is a hard, already-documented
  constraint on how many additional model calls (embeddings, reranking,
  judge calls) any Stage 2 design can add per turn without making the
  existing throttling problem worse.

### 2.6 Existing regression corpus

Six files under `tests/shopper_v2/` (committed in Stage 1): multi-turn
continuity, false-decline regression, false-substitution regression, state
transitions, tenant isolation, and zero-data/platform-down. These are the
Stage 1 corpus and the floor Stage 2 must not regress — they are not yet a
retrieval-quality evaluation set (no Recall@K/precision measurement exists
anywhere in the repo; `eval.py` at the root is explicitly unfinished per
CLAUDE.md and scores "the model's judgement," not retrieval).

---

## 3. Problems Demonstrated by Existing Evidence (not hypothesized)

Drawn directly from PROGRESS.md's own documented history, which is the
authoritative record of what has actually gone wrong, not a hypothetical
list:

1. **False substitution via single shared word** — "do you sell kayaks?"
   repeatedly (measured ~37.5% of live trials in one documented round)
   returned "Insulated Water Bottle 750ml" because "water" overlapped, and
   two independent code paths (primary search vs. the raw-sentence "rescue
   search" fallback) had to each be separately hardened — a structural sign
   that relevance logic living in multiple call sites, gated ad hoc, is
   fragile.
2. **False decline via missed varietal naming** — Kettle's "fruity coffee"
   case, where a real match exists but shares no literal keyword with the
   query, requiring the LLM `judge_relevance` fallback to exist at all.
3. **Stopword/punctuation false positives** — "you"/"your" clearing the
   significant-word floor and matching ordinary marketing copy
   ("...for your daily runs"), each fix scoped by hand to one closed-class
   list at a time.
4. **Morphological gaps fixed one at a time** — `-es`/`-ies` plurals,
   hyphenated intensifiers ("ultra-lightweight"), a 6-character truncation
   "stem" in `mission_ranking.py` that is explicitly documented as "not a
   real stemmer."
5. **No large-catalogue exercise at all.** `_catalog_alerts` (a different
   feature, Merchant Copilot) is already named in CLAUDE.md as the worked
   example of a "correct for Northfield, wrong for a real Magento client"
   bug — the same shape risk applies to retrieval: nothing in this codebase
   has ever run Stage 1's retrieval or evidence path against more than ~38
   products, so nothing about its behavior at 40,000 SKUs is known, only
   assumed.
6. **The re-scoring approach cannot generalize past literal/stemmed overlap
   by construction.** Its own docstring says so directly: "This is
   deliberately not semantic understanding. It cannot tell that 'trainers'
   means 'shoes' if no product's text ever uses the word 'shoes' at all."
   The LLM judge fallback exists precisely because the deterministic layer
   has this hard ceiling — but the judge is single-boolean or
   single-best-id, not a general reranker, and only fires on specific
   trigger conditions already coded into `service.py`, not on every turn.

---

## 4. Research Findings (general industry practice, not repo-specific)

Labelled explicitly as general knowledge about production
conversational/live-commerce retrieval, not validated against this
repository's data:

- **Hybrid lexical + semantic retrieval** (BM25/full-text search combined
  with dense vector similarity, fused e.g. via reciprocal rank fusion) is the
  standard production pattern for catalog/product search once literal
  keyword matching demonstrably misses paraphrase/synonym queries — lexical
  alone (what both demo adapters do today) cannot bridge "trainers" → "shoe";
  dense retrieval alone tends to under-perform on exact SKU/brand/model-number
  lookups, which is why hybrid, not vector-only, is the common recommendation.
- **Structured/hard filtering before ranking** (price, stock, category,
  explicit exclusions) is standard practice and is exactly what
  `mission_ranking.rank_and_filter` already does deterministically — this
  part of the existing design is not something Stage 2 needs to replace,
  only to formalize into an explicit contract step.
- **Cross-encoder reranking** (a small model scoring query+candidate pairs
  jointly, applied only to a shortlist after cheap retrieval) is the standard
  way to get semantic-quality ordering without paying dense-retrieval cost
  over an entire catalogue — but it is an additional model call per
  candidate (or a batched call per shortlist), which is a real latency/cost
  line item against a throttled LLM budget, not a free upgrade.
- **PostgreSQL full-text search (FTS)** is a common, low-operational-cost way
  to get ranked lexical search with stemming/synonym support built into the
  database, without adding a separate search service — relevant here mainly
  as a comparison point, since this repo runs SQLite, not Postgres, today.
- **pgvector / dedicated vector databases** are the standard way to store and
  query dense embeddings at scale — but they require both a Postgres (or
  equivalent) deployment and an embedding pipeline with a defined freshness
  strategy; neither exists in this repo currently.
- **Sentence-transformer embeddings** are the common way to produce the
  dense vectors hybrid/semantic retrieval needs; they require either a local
  model (adds a real, sizeable Python dependency and inference cost) or a
  hosted embedding API call (adds latency and, on a throttled free tier, a
  cost line competing with the existing chat-completion calls).
- **Query rewriting / query understanding** (turning a shopper's free-text
  message into structured intent — categories, attributes, constraints)
  before retrieval is standard in conversational commerce, and is close to
  what Stage 1's `understanding.py`/`StateDelta` already does for mission
  state — the gap is that `StateDelta` does not yet separate "what to search
  for" into its own structured `SearchIntent` distinct from the durable
  mission state (see §6).
- **Catalog projections/read-optimized indexes**, kept in sync with the
  merchant's real catalogue via a defined freshness/rebuild strategy, are
  standard whenever the source-of-truth platform's own search is too weak or
  too slow to query live on every turn — directly relevant here, since both
  demo adapters' "search" is a live, synchronous, in-memory scan with no
  index of its own.
- **Retrieval evaluation metrics** (Recall@K, Precision@K, MRR, NDCG@K) are
  standard for measuring whether a retrieval change actually improved
  results, and none currently exist anywhere in this repository for
  retrieval specifically.
- **Evidence/grounding verification** (never letting a generation step claim
  something the retrieval step didn't actually establish) is exactly the
  discipline Stage 1's `TurnEvidence`/`ProductEvidence` already implements
  structurally — Stage 2 extends this pattern rather than introducing a new
  one.

None of this implies pgvector, embeddings, or a cross-encoder should be
adopted now — see §11 for the actual decision, which is scoped to what this
repository's measured data supports.

---

## 5. Target Retrieval Architecture

Six explicit steps, each with a typed contract (§6–§10), replacing the
current shape where "understanding," "retrieval," and "relevance" are three
loosely-coordinated calls with logic split across two V1 modules and one V2
wrapper:

```
A. Search Intent      — what the shopper means to search for (structured, typed)
B. Candidate Retrieval — how candidates are found (adapter search, today;
                          extensible per §12 without changing this contract)
C. Hard Constraint Filtering — what MUST be true to remain eligible
                          (formalizes what rank_and_filter's hard filters
                          already do, made explicit and testable on its own)
D. Relevance Scoring / Reranking — how eligible candidates are ordered
                          (formalizes best_matches_with_score + the LLM
                          judge fallback into one explicit, swappable step)
E. Evidence            — what facts actually prove eligibility/relevance
                          (Stage 1's TurnEvidence/ProductEvidence, extended)
F. Response Grounding  — unchanged from Stage 1: respond() takes only
                          evidence + verified products + state, never a
                          pre-fetch reply
```

This is a **refactor of an existing pipeline into named, independently
testable steps with typed boundaries**, not a new architecture layered on
top of the old one. Steps C and D already exist today as code
(`mission_ranking.rank_and_filter`'s hard filters; `relevance.py`'s scoring)
— Stage 2's job is to give each step its own contract and test surface so a
later technology change (say, adding semantic retrieval in step B) does not
require touching C/D/E/F at all.

The model (`ReasoningService`) remains, exactly as today, non-authoritative
over price, stock, existence, category membership, catalog contents, cart
state, payment state, and permissions. Every step above operates on data the
adapter actually returned (step B) or facts about that real data (steps C–E)
— the model may only ever (1) help produce a `SearchIntent` in step A, and
(2) optionally contribute a ranking/relevance *signal* in step D, subject to
the same "returns `None`, never a guess, on failure" discipline
`judge_relevance`/`judge_best_match` already establish. It never gets to
assert that a product exists, is in stock, or matches a category — those
remain deterministic checks against real adapter data, exactly as today.

---

## 6. SearchIntent Contract

The gap this closes: Stage 1's `StateDelta.search_query` is currently always
`None` (see `understanding.py`'s own comment — the model's search term, if
any, lives inside `ProposedAction.parameters`, which Stage 1 deliberately
discards along with all other action data). Retrieval today falls back to
`delta.search_query or message` — i.e., in practice, always the raw shopper
message. That is workable for a single query-string adapter call, but it
conflates "the exact string to hand the adapter" with "everything actually
known about what the shopper wants this turn" — budget, exclusions, and
priorities already live separately in `ShoppingStateV2`, but nothing
currently unifies "this turn's retrieval-relevant intent" into one place a
retrieval step can consume without re-deriving it from two different
objects.

```python
class SearchIntent(BaseModel):
    raw_message: str                       # message, unmodified — always available as ground truth
    query: str                              # the string actually used for adapter search_products;
                                             # falls back to raw_message per Stage 1's existing rule
    categories: list[str] = []              # from ShoppingStateV2 / model extraction, never invented
    positive_terms: list[str] = []          # must_have + constraints + priorities, unified —
                                             # mirrors mission_ranking's existing treatment of these
                                             # three fields as one soft-signal set
    negative_terms: list[str] = []          # exclusions — hard filter, unchanged from today
    budget_amount: str | None = None
    budget_currency: str | None = None
    quantity: int | None = None             # for "I need two of these" — not currently modeled anywhere
    mission_context: str | None = None      # ShoppingStateV2.goal, for multi-item missions (§9)
    is_comparison: bool = False             # distinguishes "show me X" from "compare X vs Y" —
                                             # relevant to Roadmap item 2 (Smart Product Comparison),
                                             # not required for Stage 2's own scope, included so the
                                             # contract doesn't need a breaking change to add it later
    ambiguous: bool = False                 # true when understanding could not confidently extract
                                             # intent — drives the no-match/clarify contract in §9
```

This is deliberately **not a new LLM call**. It is a typed reshaping of what
`understanding.py`'s existing `ReasoningService.reason()` call plus the
current `ShoppingStateV2` already produce — the same "no new prompt, no new
tool schema" discipline Stage 1's own `understanding.py` docstring commits
to. `query`, `categories`, `positive_terms`, `negative_terms`, `budget_*`
already exist as data today; `SearchIntent` is the missing typed home for
them, replacing the current "pass `query` as a bare string, and
`shopping_state` as a separate dict, into two different downstream
functions" shape.

---

## 7. Candidate Retrieval Contract

```python
class RetrievalMethod(str, Enum):
    ADAPTER_KEYWORD = "adapter_keyword"     # today's only method — search_products()
    # future, only if §12's staged evolution justifies it:
    # LEXICAL_INDEX = "lexical_index"
    # SEMANTIC_INDEX = "semantic_index"

class Candidate(BaseModel):
    product: Product                        # the real, adapter-fetched object — never synthesized
    retrieval_method: RetrievalMethod
    retrieval_score: float | None            # None when the method provides no meaningful score
                                              # (today's adapters return catalog/hit-count order only)

class CandidateSet(BaseModel):
    intent: SearchIntent
    candidates: list[Candidate]
    total_available: int | None              # from ProductSearchResult.total_available, when the
                                              # adapter provides it — never inferred
    unavailable: bool = False                # platform-down, unchanged from Stage 1's RetrievalResult
    truncated: bool = False                  # NEW: true whenever len(candidates) could plausibly be
                                              # less than what a full catalog scan would find — i.e.
                                              # whenever `limit` was hit and total_available (if known)
                                              # exceeds it, or total_available is unknown. This is the
                                              # field the real-volume condition (§14) exists to force:
                                              # today's retrieval.py never asks this question at all.
```

`truncated` is the field this contract adds that Stage 1's
`RetrievalResult` does not have, and it exists specifically because of
CLAUDE.md's `_catalog_alerts`/`search_products("", limit=100)` worked
example — a caller that never distinguishes "this is everything" from "this
is a page" is the exact bug shape being guarded against here, generalized to
retrieval rather than left as a one-off Merchant Copilot fix.

---

## 8. Hard Constraint Filtering Contract

Formalizes what `mission_ranking.rank_and_filter`'s hard-filter half already
does (exclusions, budget, stock) into its own named, independently testable
step — not new logic, a new boundary around existing logic:

```python
class HardFilterResult(BaseModel):
    eligible: list[Candidate]
    rejected: list[RejectedCandidate]

class RejectedCandidate(BaseModel):
    candidate: Candidate
    reason: Literal[
        "excluded_by_shopper",       # exclusions match — today's `_matches`/`_uncertain` logic
        "out_of_stock",
        "over_budget",
        "ambiguous_exclusion_match", # today's "uncertain" — related-but-not-equivalent term found;
                                     # kept OUT of eligible, same as today, but now named and countable
    ]
```

Splitting this out of `rank_and_filter` as its own contract step matters for
one concrete reason: it makes "how many real candidates did hard filtering
throw away, and why" a directly measurable, loggable fact per turn — today
that information exists only transiently inside `rank_and_filter`'s local
`scored` list and is never surfaced. That number is exactly what the
no-match contract (§9, cases 2–3) and the evaluation framework (§13,
hard-constraint violation rate) both need.

---

## 9. Relevance Scoring / Reranking Contract

```python
class RankedCandidate(BaseModel):
    candidate: Candidate
    relevance_score: float                  # unified score — see below
    scoring_method: Literal["lexical_overlap", "llm_judge", "combined"]
    weak_match: bool                        # today's weak_single_word_match, unchanged semantics
    diverges_from_said: bool                 # today's query_diverges_from_said, unchanged semantics

class RankingResult(BaseModel):
    ranked: list[RankedCandidate]            # ordered, best first
    confident: bool                          # true only when at least one ranked candidate is neither
                                              # weak_match nor diverges_from_said — mirrors today's
                                              # `relevant = in best_ids and not (diverges or weak)` gate
```

This step **keeps today's deterministic lexical scoring
(`best_matches_with_score`) as the default and only currently-justified
scoring method** (see §11 — no evidence yet exists that this repo's demo
catalogues need more). It adds a named seam (`scoring_method`) so an LLM
judge or a future reranker can be plugged in as an alternative or fallback
method without changing the contract shape — exactly the seam
`judge_relevance`/`judge_best_match` already occupy informally today, made
explicit.

---

## 10. Evidence Contract

Extends Stage 1's existing `ProductEvidence`/`TurnEvidence`
(`engine/shopper_v2/evidence.py`) rather than replacing it — these types
already do the right structural thing (a product is never described as a
match unless `relevance_verified` is independently True). Stage 2 adds the
fields the new contract steps above make available:

```python
class VerifiedProductEvidence(BaseModel):        # supersedes ProductEvidence
    product_id: str
    exists: bool                                  # unchanged: true by construction, never invented
    category_match: bool | None
    hard_constraints_passed: bool
    failed_constraints: list[str]
    relevance_verified: bool
    relevance_score: float
    scoring_method: str                           # NEW — which step D method produced the verdict,
                                                    # for evaluation/debugging (§13)
    weak_match: bool
    diverges_from_said: bool                       # NEW — promoted from an internal evidence.py local
                                                    # to a named, inspectable field
    truncated_candidate_set: bool                  # NEW — propagated from CandidateSet.truncated;
                                                    # lets respond.py and evaluation both know when a
                                                    # "no good match" verdict might be a page-size
                                                    # artifact rather than a genuine catalog gap
    evidence_fields: list[str]                     # unchanged
```

`exists`, `category_match`, `hard_constraints_passed`,
`relevance_verified` remain the only fields `respond.py` is permitted to
build a shopper-facing claim from — unchanged from Stage 1's structural
guarantee.

---

## 11. No-Match Contract

The seven cases named in the task, mapped to what Stage 1 already does
(verified against `evidence.py`/`respond.py`) versus what Stage 2 must add:

| # | Case | Stage 1 today | Stage 2 |
|---|------|----------------|---------|
| 1 | Zero candidates retrieved | `no_match_reason="no_candidates"`; `shop_carries` checked | Unchanged; add `truncated` awareness — a zero-candidate result from a `limit`-capped, non-empty-catalog adapter call is not the same claim as "this catalog has nothing," and the reply must not conflate them (see below) |
| 2 | Candidates exist, all fail hard constraints | `no_match_reason` not currently distinguished from case 3 — both fall into `"no_relevant_match"` | **New, distinct reason** `"all_failed_hard_constraints"` — the shopper should be told budget/exclusion/stock was the blocker, not that nothing relevant exists; today's single `"no_relevant_match"` bucket loses this distinction |
| 3 | Candidates exist, semantically irrelevant | `no_match_reason="no_relevant_match"` | Unchanged reason, but now backed by `RankingResult.confident=False` explicitly rather than an inline `not any(e.relevance_verified ...)` check |
| 4 | Merchant has the category, not the product | `shop_carries` returns True but no candidate is relevant → same `"no_relevant_match"` bucket as case 3 today | **New, distinct reason** `"category_present_product_absent"` when `shop_carries`/category evidence is True but zero candidates are relevant — lets `respond.py` say "we carry running shoes, but not that specific model" instead of a generic "nothing matched" |
| 5 | Product name close but not the actual product | Handled today by `_is_real_title_citation`'s near-equality check plus `diverges_from_said` — a close-but-wrong name is NOT treated as a citation and falls through to ordinary relevance scoring | Unchanged; this is one of the most-hardened paths in the existing code and Stage 2 should not touch its logic, only expose `diverges_from_said` as a named evidence field (§10) |
| 6 | Retrieval confidence ambiguous | Today's `weak_single_word_match` (score==1) is the only ambiguity signal; there is no explicit "ask the shopper to clarify" path — a weak match is silently treated as no-match | **New behavior to decide (open decision, §24):** whether an ambiguous case should trigger a clarifying question rather than a flat no-match reply. Not decided here — needs a product decision, not an architecture one, since it changes conversation shape |
| 7 | Platform unavailable | `no_match_reason="platform_unavailable"`, handled first, before any candidate logic runs | Unchanged — already correct, already tested (`tests/shopper_v2/test_zero_data_platform_down.py`) |

The structural rule from the task — "product exists" is never equivalent to
"product is relevant" — is already enforced by Stage 1's own construction
(`ProductEvidence.exists=True` for every fetched product, but
`relevance_verified` independently gated) and remains unchanged in Stage 2;
what Stage 2 adds is finer-grained *reasons* so `respond.py` can be more
honest about *why* there's no confident match, not a change to the
underlying guarantee.

---

## 12. Multi-Turn Behavior

Verified against `state.py`/`orchestrator.py`: `ShoppingStateV2` already
persists `constraints`, `must_have`, `nice_to_have`, `exclusions`,
`priorities`, `budget_amount` across turns via the existing
`shopping_states` table and per-(session, connection) lock, with
add/remove/clear semantics (`StateDelta`). This already correctly handles
the task's cases 7–10 (budget change, "comfort matters more than price")
structurally, **provided** each turn's `SearchIntent` is built by combining
the current turn's new signal with the *persisted* state, not by re-deriving
everything from the raw message alone (retrieval's current fallback,
`delta.search_query or message`, does exactly this correctly for the query
string, but nothing today unifies budget/priorities/exclusions from state
into the same intent object the query string travels in — see §6, which is
the actual gap Stage 2 closes here, not a new persistence mechanism).

One documented, already-fixed regression is directly relevant: the
Stage 1 orchestrator's own comment records that falling back to `goal`
instead of the raw message for retrieval caused every later turn in a
session to silently re-search the *first* turn's goal forever ("under 3000"
searched "running shoes" instead of the actual new query). This is exactly
the class of bug `SearchIntent` (§6) is designed to prevent structurally: by
making "this turn's actual search string" and "the durable mission state" two
explicitly separate, always-both-present fields on one object, rather than
one variable that has to be manually chosen correctly on every call site.

Priority recency-weighting (case 9, "comfort matters more than price") is
already implemented in `mission_ranking.rank_and_filter`'s priorities
scoring (`score += 1.0 + i * 0.5`, later-stated priorities weighted higher)
— confirmed correct by reading the code, not assumed.

**Case 14, multi-item/multi-constraint missions**, is explicitly out of
scope for Stage 2 per CLAUDE.md's own blocking note: Mission-Based Shopping
is one of the four roadmap items blocked on the reply-before-data fix, which
Stage 1 closed only for the single-item discovery path this document covers.
`SearchIntent.mission_context` (§6) is included in the contract now
specifically so a later mission-shopping stage does not require a breaking
schema change to this contract — but building multi-item mission logic
itself is not Stage 2's job.

---

## 13. Evaluation Methodology and Metrics

**Nothing named here currently exists in the repository.** `eval.py`
(unfinished, scores model judgement generally) and the six
`tests/shopper_v2/` regression files (pass/fail on specific reproduced
bugs) are the entire existing evaluation surface for this path — there is
no Recall@K, no latency measurement, no cost tracking for retrieval
specifically anywhere in this codebase today.

**Dataset.** A fixed, versioned set of multi-turn conversations (not
isolated prompts), covering:

- Both demo merchants (Northfield: footwear/running gear; Kettle: coffee),
  using their real seeded catalogues as ground truth.
- The 14 example queries in the task prompt, run against both merchants
  where applicable.
- Every adversarial case already reproduced in PROGRESS.md's history
  (kayak/water-bottle false substitution, Kettle fruity-coffee false
  decline, stopword/punctuation false positives, hyphenated intensifier
  gaps) — reused as **regression cases with known-correct expected
  outcomes**, not invented fresh.
- A zero-data merchant (empty catalogue, seeded specifically for this).
- A larger synthetic catalogue — does not exist yet; must be generated
  (see §17) before real-volume claims can be made at all.
- Platform-unavailable simulation (adapter raising, already exercisable via
  existing test patterns in `healthcheck.py`/`fuzz.py`'s style).
- Misspellings, plural/singular, hyphenation, conflicting/changing
  constraints, multi-turn refinement sequences.

**Metrics** (all currently unmeasured; this is the definition, not a
reported number):

- Recall@K, Precision@K, MRR, NDCG@K — against the fixed dataset's labeled
  correct products per query.
- Hard-constraint violation rate — a candidate in `HardFilterResult.eligible`
  that actually violates a stated exclusion/budget/stock fact (should be
  structurally ~0 given §8's design, but must be measured, not assumed).
- False-substitution rate, false-decline rate — direct counts against the
  known adversarial cases above.
- Unsupported-product-claim rate — any shopper-facing sentence naming a
  product/attribute not present in `VerifiedProductEvidence` for that turn.
- No-match correctness — did the system pick the right one of the 7 cases
  in §11's table.
- Multi-turn constraint retention — does a later turn's `SearchIntent` still
  reflect an earlier turn's un-cleared constraint.
- Query understanding accuracy — does `SearchIntent` extraction match a
  hand-labeled expected intent for the dataset's queries.
- Retrieval latency, reranking latency (if step D gains a second method),
  model-call count per turn, token usage/cost per turn — all measurable via
  simple timing/counting instrumentation around the existing
  `ReasoningService` calls and adapter calls; none of this instrumentation
  exists today.
- Catalog freshness — only relevant once/if a projection/index (§17) exists;
  not applicable to today's live-adapter-call-only design.

This dataset and harness (2A in §18) is Stage 2's actual first deliverable —
everything else in this document is unmeasurable without it existing first.

---

## 14. Technology Decision Table

| Technology | Problem it solves | CV3 needs it now? | Advantages | Disadvantages | Operational cost | Correctness implications | Latency implications | Adopt |
|---|---|---|---|---|---|---|---|---|
| **Existing adapter search** | Baseline candidate retrieval | Yes — already the only source of truth for real products | Zero new infra; platform-declared capability (`search_kind: keyword_substring`) is already honestly modeled | Deliberately weak per-platform (title-only, no synonyms); ceiling is the platform's own search, not something Stage 2 controls | None (already running) | This is the actual data; everything downstream depends on its honesty | Whatever the adapter's own HTTP round-trip costs (already the case today) | **Now** — keep as step B's primary method |
| **PostgreSQL FTS** | Ranked lexical search with built-in stemming/synonym support, without a separate search service | Not established — this repo runs SQLite, not Postgres; no measured evidence the existing per-platform adapter search is the actual bottleneck vs. the re-scoring/evidence layer | Low operational cost relative to a dedicated search service; would give ranked lexical search server-side rather than in Python | Requires migrating off SQLite — a real, non-trivial infrastructure change (CLAUDE.md's own SQLite corruption incident and rules on database changes apply directly) not justified by evidence gathered so far | A full database migration — significant, not incremental | Could reduce reliance on the hand-maintained Python word-overlap/stopword lists if it replaces them, but only if measured to actually fix a real gap those lists don't already cover | Likely lower than current in-Python re-scoring at real catalogue scale, but unmeasured | **Later, only if 2A's baseline measurement shows the SQLite/in-Python lexical layer is the actual bottleneck at real catalog volume** — not now |
| **pgvector** | Store/query dense embeddings for semantic retrieval | Not established — no embedding pipeline exists, no measured recall gap requiring it (see judge_relevance's existing role as a narrower, targeted semantic fallback) | Enables "trainers" → "shoes"-class semantic matches structurally, not via a hand-written synonym list | Requires Postgres (see above) plus an embedding pipeline with a freshness strategy (§17); adds real operational surface for ~40-SKU-to-40,000-SKU catalogues whose actual size may not need dense retrieval at all | High — new datastore dependency, new pipeline, new failure modes to handle per the "platform down"/"zero data" conditions | Directly addresses the documented "cannot tell 'trainers' means 'shoes'" ceiling — but only if the measured gap after 2A/2B shows this ceiling is actually costing real matches at the catalogue sizes CV3 clients actually run | Adds embedding-compute latency (local model) or network latency (hosted API) per query, competing with the existing throttled-LLM latency budget | **Not now.** Revisit only after 2B (lexical improvements) and a measured recall gap at real-volume synthetic data (§17) |
| **Sentence Transformers embeddings** | Produce the dense vectors pgvector-class retrieval needs | Same as pgvector — no current need established | Open-source, no per-call API cost once running locally | A real new Python dependency (not currently installed) with non-trivial model download/load time and memory footprint; local inference on every catalogue update needs a defined pipeline (§17) | Medium-high — model hosting, embedding-refresh job | Same recall benefit as pgvector, same "not yet justified" caveat | Local inference adds CPU/latency cost per embedding computed; batched at index-build time this is fine, but query-time embedding of the shopper's own message adds a small but real per-turn cost | **Not now**, same reasoning as pgvector — paired decision |
| **CrossEncoder reranking** | Higher-quality ordering of an already-small shortlist, without full dense retrieval cost | Not established — the current deterministic scorer already produces a small, correctly-ordered shortlist in the cases actually reproduced in PROGRESS.md; no measured case exists where cross-encoder-quality ordering, as opposed to correct hard filtering, was the actual failure | Better semantic ordering than literal word-overlap on genuinely ambiguous "which of these 4 fits better" cases | Extra model call per turn (batched over the shortlist) against an already-throttled free-tier LLM budget — directly conflicts with CLAUDE.md's own documented "4 model turns/minute" constraint unless carefully budgeted | Medium — one additional model call type, needs its own throttle-aware fallback (`None`-on-failure, same discipline as `judge_relevance`) | Could improve ordering quality once hard filtering + lexical scoring are confirmed correct; does not fix false-substitution/false-decline on its own — those are candidate-set and hard-filter problems, not ordering problems | Adds one more model call to the existing per-turn budget — must be measured against the Groq throttle before adopting | **Later, only if 2A's evaluation shows ordering quality (not candidate coverage) is the residual gap** after 2B/2C |
| **Elasticsearch/OpenSearch** | Dedicated search service: ranked lexical + optional vector, at real catalogue scale, independent of the merchant's own platform search | Not established — no measured latency/scale problem yet exists to justify a new standalone service; each of CV3's ~50 merchants already has its own platform search (Magento/Shopware/custom), which is the actual source of truth Stage 2 must stay compatible with per the adapter-boundary rule | Mature ranked search, good at real catalogue scale, handles both lexical and vector in one service | A significant new operational dependency for an agency running many small merchant integrations — directly conflicts with CLAUDE.md's "does not need to work like a big-vendor stack" client profile unless proven necessary | High — a service to run, monitor, and keep synchronized per-tenant (tenant isolation, §19, becomes a real infra concern, not just an app-layer one) | Would remove the current per-platform adapter search-quality ceiling entirely, replacing it with a projection Stage 2 controls — but see §17's freshness/sync questions before this is safe | Likely improves worst-case latency at real volume, but adds a new failure mode (index staleness, sync lag) that must itself degrade honestly per invariant 5 | **Not now, and only ever as a projection layer behind the adapter boundary, never a replacement for the adapter's own authority over price/stock/existence (per §5's non-authoritative rule)** |
| **LlamaIndex retrieval components** | Off-the-shelf RAG plumbing (chunking, indexing, query pipelines) | No — this is not a document-RAG problem; the "documents" here are structured Product records with a normalized schema (`shared/models/commerce.py::Product`) already, not unstructured text needing chunking | Faster to prototype a generic RAG pipeline | Framework-driven architecture the task explicitly warns against turning this into ("do not turn this into a generic RAG architecture"); adds a dependency whose abstractions don't match a typed-product-catalog problem | Low to adopt, but ongoing framework-version churn cost | Framework retrieval abstractions do not naturally express "never let the model assert price/stock/existence" — that discipline would have to be bolted on anyway, defeating the point of adopting the framework | Framework overhead, unmeasured | **No** — wrong tool for a structured-catalog, not a document-corpus, problem |
| **LangChain retrieval components** | Same category as LlamaIndex | No, same reasoning | Same | Same, plus LangChain's own well-documented tendency toward hidden prompt/behavior coupling that would fight this codebase's existing discipline of hand-written, auditable prompts (`engine/reasoning/prompts.py`) | Low to adopt, ongoing churn | Same framework-vs-typed-boundary mismatch as LlamaIndex | Unmeasured | **No** |
| **LangGraph** | Orchestrating multi-step agent/graph workflows | No — Stage 1's `orchestrator.py::run_turn` is already a small, explicit, linear function whose call order IS the enforced invariant (see its own docstring); a graph framework would obscure that enforced ordering behind framework abstraction rather than keeping it as plain, auditable Python | N/A for this problem | Replaces an already-correct, explicit, single-function control flow with a framework abstraction for no demonstrated benefit | Low to adopt, ongoing dependency | Directly risks weakening the exact "call order is the enforcement" property `orchestrator.py` currently guarantees by construction | Unmeasured, likely net negative (extra indirection) | **No** |
| **Pydantic / PydanticAI typed boundaries** | Typed request/response contracts at each pipeline step | **Yes, already the pattern this repo uses everywhere** (`StateDelta`, `ShoppingStateV2`, `TurnEvidence`, `ProductEvidence` are all Pydantic `BaseModel`s today) | Zero new dependency (Pydantic 2.13 already installed and used throughout); directly matches this document's own §6–§10 contracts | None specific to this use | None — already in use | Strongly positive — this is the mechanism that makes "the model cannot assert X" enforceable as a type-level fact (a field simply doesn't exist for the model to fill in) | Negligible | **Now** — §6–§10's contracts should be implemented as plain Pydantic `BaseModel`s, consistent with the rest of this package; **PydanticAI specifically (the agent framework) is not needed** — Stage 1 already uses the existing `ReasoningService`/tool-call machinery directly, and nothing here demonstrates a need for a new agent framework layered on top of it |

**No overall ranking across these is given, per the task's own instruction** —
each row's "Adopt" column is the actual recommendation, and every "not now"
is conditioned on a specific measurement gate defined in §18, not a permanent
rejection.

---

## 15. Data / Index Strategy

No projection or index currently exists — retrieval is a live, synchronous
call to the adapter's `search_products` on every turn, with no caching layer
(confirmed: `retrieval.py` has none, by its own docstring's explicit "no
caching" statement).

If §18's staged evaluation (2A→2B) demonstrates that a catalog
projection/index is actually justified (e.g., real-volume testing shows the
live adapter call itself, not the relevance layer, is the latency
bottleneck, or that a merchant's platform search is too slow/limited to
query per-turn at real catalogue size):

- **Source of truth**: always the merchant's own platform via its adapter —
  never the projection. This preserves the existing adapter-boundary
  invariant (`Product` objects are only ever real, adapter-fetched data;
  Stage 1's `retrieval.py` already commits to "never fabricating a product
  list").
- **Synchronization strategy**: must be pull-based, on a schedule or
  triggered by the platform's own webhook capability where declared
  (`CapabilitySet.supports_webhooks` — already a real, checked field per
  merchant, e.g. Kettle declares `supports_webhooks=True`, Northfield does
  not) — never a write path the adapter's mutation boundary would need to
  expose, keeping this entirely on the read side per the task's explicit
  "read-only catalog access is allowed" boundary.
- **Freshness guarantee**: must be explicit and stated per merchant (e.g.,
  "index reflects catalog as of last successful sync, timestamped"), never
  silently assumed current — the same "never claim more than is actually
  known" discipline `total_available`/`quantity_available`'s existing
  None-means-unknown convention already follows in `shared/models/commerce.py`.
- **Rebuild behavior**: a full rebuild must be resumable/idempotent and must
  never serve a half-built index as if complete — degrades to "index
  unavailable, fall back to live adapter search" rather than serving stale
  or partial results silently.
- **Failure behavior**: index unavailable → fall back to today's live
  adapter call, exactly the same shape as `retrieval.py`'s existing
  `unavailable=True` degrade path, not a new failure mode.
- **Tenant isolation**: one projection per `connection_id`, enforced the same
  way `db.owners` already enforces cross-merchant isolation elsewhere in
  this codebase — never a shared index queried with a tenant filter bolted
  on after the fact, which is the class of bug the existing ownership-table
  pattern exists specifically to prevent.

This is deliberately conditional — **no index is recommended to be built
without a measured justification from §18**, per §14's technology table.

---

## 16. Performance

No SLA is established anywhere in this repository, and none is invented
here. What is actually known:

- The LLM provider is throttled to "roughly four model turns a minute" per
  CLAUDE.md — a hard, already-documented ceiling on how many *additional*
  model calls (a reranker, an embedding call, a second judge call) any
  Stage 2 design can add per shopper turn without making an already-named
  problem worse. This is the single most concrete, real constraint Stage 2
  must design against.
- No latency baseline for the current retrieval+evidence path has ever been
  measured and recorded (confirmed: no timing instrumentation exists in
  `retrieval.py`, `evidence.py`, or `orchestrator.py`).

**Proposed engineering targets** (explicitly labeled as proposed, not
established SLAs):

- Baseline measurement (2A, §18) must record current p50/p95 latency for
  the full `run_turn` call, broken down by step, against both demo
  catalogues, before any retrieval change is made — this is the actual
  first performance deliverable, not a target number.
- Any new model call added by a Stage 2 change (reranker, judge) should be
  measured against the existing throttle and either (a) not add a call on
  the common path, only on a specific fallback trigger (the same discipline
  `judge_relevance` already uses), or (b) be explicitly justified as
  replacing an existing call rather than adding a new one.
- A proposed (not established) target: Stage 2 changes should not increase
  p95 turn latency by more than the equivalent of one additional throttled
  model call, unless a specific evaluation result (§13) justifies the cost.

---

## 17. Security / Tenant Boundaries and V2 Isolation

Unchanged from Stage 1, and this document proposes no change to any of the
following:

- `engine/shopper_v2/` remains importable only by itself plus
  `engine/api/chat_v2.py`; V1 (`engine/api/chat.py` and everything it calls)
  remains completely unaware Stage 2 exists, per the existing package
  docstring's stated boundary.
- No import of `engine.decision`, `engine.risk`, or `engine.execution`'s
  mutation paths — Stage 2 stays limited to the same read-only relevance/
  ranking helpers (`engine.execution.relevance`,
  `engine.execution.mission_ranking`) Stage 1 already uses, per the task's
  explicit "do not modify Decision Engine, Risk Gate, execution, payment,
  checkout, adapter mutation paths" boundary.
- Read-only catalog access only — no new adapter mutation method, no new
  write path.
- Tenant isolation for any new data (a candidate cache, a future projection)
  must use the existing `db.owners`-style ownership enforcement, never a
  shared structure with an after-the-fact tenant filter — see §15.
- The `tests/shopper_v2/test_tenant_isolation_v2.py` regression must
  continue to pass unmodified in its assertions (its *scope* may grow to
  cover new Stage 2 data structures, but its existing assertions are not to
  be weakened).

---

## 18. Implementation Phases

Determined from the research and repository state, not assumed from the
task's example ordering — the actual justified sequence, given that no
evaluation harness or large-catalogue data exists yet:

**Stage 2A — Contract + evaluation dataset + baseline measurement.**
Implement `SearchIntent`, `CandidateSet`, `HardFilterResult`,
`RankingResult`, `VerifiedProductEvidence` as Pydantic models (§6–§10),
refactoring today's `retrieval.py`/`evidence.py` to use them **without
changing their underlying logic** — i.e., this phase is a typed-boundary
refactor of existing, already-correct behavior, not a new algorithm. Build
the evaluation dataset (§13) from PROGRESS.md's documented adversarial
history plus the task's 14 example queries. Instrument and record baseline
latency/cost/model-call-count (§16). Must not regress any existing
`tests/shopper_v2/` assertion. **This phase produces no new retrieval
capability — it produces the measurement tool everything after it needs.**

**Stage 2B — Structured/lexical retrieval improvements.**
Only once 2A's baseline shows where the deterministic lexical layer
(`relevance.py`/`mission_ranking.py`) actually falls short against the real
dataset — not assumed in advance. Likely candidates, based on the
documented history: extending `_significant_words`/`_stem` coverage,
formalizing the no-match reason taxonomy (§11), and closing the
`quantity`/`is_comparison` gaps `SearchIntent` names but Stage 1 never
populated. This is where most of the task's 14 example queries (misspelling,
plural/singular, budget changes) should already be handled correctly, given
what §2.3/§9/§12 already confirm exists — 2B's job is measuring and closing
whatever the evaluation dataset shows is still wrong, not assuming a rewrite
is needed.

**Stage 2C — Semantic retrieval, only if justified by measured recall gaps.**
Explicitly gated on 2A/2B's measured results against real-volume synthetic
data (§17's large-catalogue requirement must exist before this phase can
even be evaluated meaningfully). Per §14, this is "later," not "next" —
listed here only to preserve the option, not to schedule it.

**Stage 2D — Reranking**, same gating as 2C, and only after 2C if dense
retrieval is itself adopted (a cross-encoder reranks a candidate set; it has
nothing to rerank if candidate retrieval hasn't changed from today's
adapter call).

**Stage 2E — Evidence verification extensions.**
Extending `VerifiedProductEvidence` (§10) usage into `respond.py` for the
finer-grained no-match reasons (§11) — this can actually run in parallel
with 2B, since it depends only on the typed contracts from 2A, not on 2C/2D.

**Stage 2F — Live/shadow evaluation.**
Running the Stage 2 pipeline in shadow (logging results, not serving them)
against real merchant traffic once a real client's data is available,
comparing against V1/Stage-1-current behavior before any cutover. This is
the actual "does it survive a real client" gate (CLAUDE.md's value bar,
condition 6) and cannot honestly be claimed done before it runs.

The task's suggested ordering (2A→2F as contract→lexical→semantic→
rerank→evidence→shadow) is **adopted as the sequence**, with one adjustment:
2E (evidence verification extension) does not need to wait for 2C/2D, since
it depends only on 2A's contracts — it is listed 2E only to match the task's
own lettering, not because it is sequentially blocked on 2C/2D.

---

## 19. Definition of Done

No Stage 2 phase is complete on "the code runs." Per phase:

- Implementation matches its own contract (§6–§11) exactly — no field
  invented beyond what's specified without updating this document first.
- Automated tests pass, including all pre-existing `tests/shopper_v2/`
  regression files unmodified in their assertions.
- Regression coverage for every adversarial case named in §13's dataset,
  each with a known-correct expected outcome, not a guessed one.
- Both Northfield and Kettle exercised — not just one demo merchant.
- Zero-data merchant tested (empty catalogue, honest degrade, not a
  divide-by-zero or confident-empty response).
- Platform-unavailable tested (adapter raises, degrades per invariant 5).
- Tenant isolation verified — `tests/shopper_v2/test_tenant_isolation_v2.py`
  and any new assertions it grows to cover.
- Latency/cost measured and recorded against the §16 baseline, not just
  "seems fast enough."
- Browser/manual verification of the actual shopper-facing reply text for
  every no-match case in §11's table, not just the typed evidence object.
- V1 regression verified — `healthcheck.py`, `fuzz.py`, `auditroutes.py`
  all still pass; V1 is provably untouched (no import of
  `engine.shopper_v2` from anywhere in `engine.api.chat`/V1's call graph).
- No unsupported product claims, no false substitutions — measured against
  §13's dataset, not asserted from a single manual walkthrough.
- Every claim moved from PROGRESS.md to Completed.md for this feature has
  passed `value-auditor` against CLAUDE.md's value bar and real-client
  conditions — a correct, tested, isolated retrieval improvement that
  changes no shopper-visible outcome is not, on its own, done.

---

## 20. Risks

- **Measurement-shaped risk**: Stage 2A's evaluation dataset, if built from
  only the already-known adversarial cases, risks the same trap PROGRESS.md
  already documents happening once before ("a batch composed mostly of
  other kayak phrasings had been masking the true failure rate of this
  exact one for several rounds") — the dataset must include held-out,
  not-yet-seen phrasings, not only replay the exact strings already fixed.
- **Scale risk is currently entirely unmeasured**: every claim in this
  document about "real volume" behavior is a projection from 38/16-SKU
  catalogues, because no larger synthetic catalogue exists yet (§17). Any
  Stage 2C/2D technology decision made before that data exists would be
  exactly the "invent a need" mistake §14 explicitly warns against.
- **LLM throttle risk**: any Stage 2 addition that adds a model call to the
  common path (not just a rare fallback) directly collides with the
  already-documented Groq throttle and could make `healthcheck.py`'s
  existing "cannot complete reliably" problem worse, not better.
- **Contract churn risk**: §6–§10's typed contracts are designed to
  anticipate later stages (mission_context, is_comparison) without a
  breaking change — but if 2C/2D's actual technology choice turns out to
  need a materially different shape than anticipated here, this document's
  contracts will need revision before implementation, not silent
  divergence.
- **Two-adapter generalization risk**: every fix validated only against
  Northfield or only against Kettle risks the same "correct for one demo
  merchant, wrong for the other" shape CLAUDE.md already names as a general
  anti-pattern — §19's "both merchants exercised" requirement exists
  specifically against this risk.

---

## 21. Open Decisions (not resolved by this document)

1. **Ambiguous-match clarifying question (§11, case 6).** Should a weak/
   ambiguous match trigger a clarifying question back to the shopper,
   rather than a flat no-match reply? This changes conversation shape, not
   just retrieval logic, and needs a product decision.
2. **Whether/when a catalog projection (§15/§17) is ever justified**, gated
   entirely on 2A/2B's measured results against real-volume synthetic data
   that does not exist yet — the honest answer today is "unknown, not yet
   measured," not "yes" or "no."
3. **Whether semantic retrieval (§14) is needed at all for CV3's actual
   client catalogue characteristics** (structured, mid-market, largely
   English-language product titles/descriptions) versus whether closing the
   remaining lexical/stemming gaps (2B) is sufficient — this is the single
   largest cost/complexity fork in the whole roadmap and should not be
   decided before 2A's dataset exists.
4. **Who owns generating the large synthetic catalogue** (§17/§20) needed
   before any real-volume claim can be made honestly, and what it should
   actually model (a real Magento export shape? a generated 40,000-row
   table matching CLAUDE.md's stated client profile?).
5. **Whether `SearchIntent.is_comparison`/`mission_context` (§6) should be
   populated by Stage 2 at all**, given that Smart Product Comparison and
   Mission-Based Shopping are both explicitly still blocked/out of scope —
   these fields are included now only to avoid a future breaking contract
   change, not because Stage 2 is expected to populate or use them yet.
6. **Reranking's actual trigger condition (§14, CrossEncoder row)** — if
   ever adopted, should it run on every turn's shortlist, or only on the
   same kind of fallback-trigger condition `judge_relevance` already uses
   (i.e., only when the deterministic layer is inconclusive)? Not decided
   here; depends on 2A/2B's measured results.

---

*End of specification. No Stage 2 code has been written. Awaiting review
before any implementation begins.*
