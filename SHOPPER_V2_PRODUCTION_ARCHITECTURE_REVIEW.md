# Shopper V2 Production Architecture Review (2026-09-28)

Planning document. No code changed. Does not modify CLAUDE.md, PROGRESS.md,
or Completed.md. This is a stress-test of `SHOPPER_V2_FINAL_ARCHITECTURE_
DECISION.md` against arbitrary long natural conversations and compound
turns, not the historical example list — several findings below **revise**
that document's design rather than merely confirming it. Where a revision is
load-bearing, it is stated as a decision, not an open question, per this
review's mandate to design the production end state now.

---

## 0. Method

Each of the fourteen stress-test questions is answered by (1) constructing an
adversarial scenario the historical example list does not cover, (2) tracing
it against the verified code facts already established in
`SHOPPER_V2_END_STATE_GAP_ANALYSIS.md` (Gaps A-S, all code-cited) and the
architecture decision document, (3) stating whether the proposed design
survives, and if not, deciding the fix. No new code was read this pass —
every citation below is inherited from the two prior documents' own verified
citations; no new claim is asserted without one.

---

## 1. Compound-turn expressiveness

**Adversarial case:** *"Actually, I don't want lightweight anymore — show me
waterproof trail shoes under $150, and how does that compare to the
Trailblazer?"* One turn containing: a correction (retract a stated
preference), a preference change (waterproof), a hard constraint (under
$150), a search (trail shoes), a reference (Trailblazer, named), and a
comparison (new results vs. the reference).

**Verdict: the architecture decision document's schema does NOT survive
this unchanged.** `VerifiedInterpretation` as widened there still carries a
single `capability: Capability` field — a compound turn is not one
capability, and forcing a single dispatch target either drops the
correction/preference/constraint or requires a second, uncoordinated
extraction pass (recreating Gap B).

**Decision (revises the interpretation contract, §3 below):** the single
interpretation call's output is restructured into two independent parts:

- `state_deltas: list[StateDelta]` — every preference/constraint/correction/
  cancellation implied by the turn, order-independent, always applied in
  full before any response action runs. A correction ("I don't want
  lightweight anymore") is itself one `StateDelta` (a removal), not a
  separate mechanism.
- `operations: list[Operation]` — an ORDERED list of response actions, each
  carrying its own capability + resolved entities + question_type. The
  common case has exactly one operation. A compound turn produces an
  ordered chain: `[SEARCH(query="trail shoes", constraints=[waterproof,
  <$150]), COMPARE(entities=[<top of previous op's results>, named:
  "Trailblazer"])]`. Each operation may reference the OUTPUT of a prior
  operation in the same turn (an intra-turn handle, not a persisted id) —
  see §9 for why this must not go through the persisted `ConversationContext`
  mid-turn.

This is still **one model call** (the "single authoritative interpretation"
principle is not violated — compound-ness is expressed in the output shape,
not by adding a second call).

---

## 2. State ownership/mutation matrix

| State object | Single writer (function) | Reader contract | Persistence | Lifecycle |
|---|---|---|---|---|
| `ConversationContext` | `build_context()`, invoked exactly once, as the **terminal** step of every turn (after all operations in the chain complete, before the reply is returned) — never mid-turn, never conditionally | Every node that needs "what's relevant now" for a NEW turn's own decision reads only the version built at that turn's own `load_context` step | Not persisted itself (rebuilt each turn from persisted state) | Created fresh every turn; never partially rebuilt |
| Focus Stack (`Topic`, `focus_stack`) | `topic_transition.py::decide_transition` + `apply_transition`, consuming the single interpretation's `context_action` when available (§3); the generalized `finalize` (below) backfills `Topic.product_ids`/label once real evidence exists | Every capability node, via `ConversationContext.active_topic`/`paused_topics` | Persisted every turn via `shopping_state_store.merge()` | Push/resume/continue per turn; a topic is retired only by explicit resume-elsewhere or session end, never deleted mid-conversation |
| Current product set (`active_products`, `Topic.product_ids`, `LastRendered`) | **One function, universally**: the generalized `finalize` node — called exactly once per turn, after every operation in the chain completes, for every capability that resolved a concrete product set (search, compare, recommend, explain, alternatives, compatibility, complementary, mission item resolution, tradeoff) | Reference resolution (any operation), `ConversationContext` assembly | Persisted every turn this triad changes | Overwritten only when the SAME turn's own verified retrieval/resolution produced real ids; never overwritten with an empty result from a failed turn (existing `node_finalize` self-heal principle, generalized) |
| Preferences (session-scoped) | `apply_delta`, consuming `state_deltas` (§1) exclusively | `mission_ranking.rank_and_filter`, `_preference_status_reply` | Persisted every turn a delta exists | See §5 for full lifecycle (temporary/durable/cancelled) |
| Durable memory facts | `db/memory_facts.py` (unchanged), written only from a `state_delta` tagged `scope="durable"` | `rank_and_filter`'s durable-fact fallback | Cross-session, per shopper account | See §5 |
| Mission (`MissionItem`, `mission_status`) | `missions.py`'s own resolution logic (unchanged) — but its product-set OUTPUT is handed to the SAME generalized `finalize`, not written directly (see §14 for why this changes existing code) | Recommendation-context assembly, mission's own reply | Persisted every turn mission state changes | Item added/tracked/fulfilled/abandoned per existing mission lifecycle, unchanged |
| `PendingAction` | Whichever operation creates a pending confirmation/clarification (§8) | The single pending-action handler | Persisted until resolved or superseded | Cleared on resolution (confirm/cancel/correct) or superseded by a new pending action of a different kind (never two pending actions coexist — a new one replaces, with the old one's own "no longer relevant" disclosed if it was never resolved) |
| Real cart | The adapter, fetched fresh every time it is checked | `fulfillment.py`, `intent_resolution.py`'s remove/set_quantity path, `cart_status.py` | Not cached across turns — always a live read | Authoritative always; nothing in this architecture ever reads a cached cart value for a decision |
| Order state | The adapter's real order records | `order_status.py` | Not cached | Authoritative always |
| Evidence (`TurnEvidence`) | `evidence.build_evidence`/`already_verified_evidence` (unchanged, confirmed already-universal) | Every capability's reply-construction path | Turn-scoped only, never persisted across turns | Rebuilt fresh every turn from that turn's own retrieval |
| `TurnTrace` | `graph.py`'s own per-node writes (unchanged) | `degraded_disclaimer` (§4), test/audit tooling | Turn-scoped only | Rebuilt fresh every turn |

**Invariant this matrix enforces:** every row has exactly one writer
function and one persistence rule. Two rows in the prior architecture
document's own draft (`active_products` written by both `node_finalize` and
`node_mission`/`node_tradeoff` directly) are corrected here to a single
writer — see §14.

---

## 3. Interpretation contract

**Authoritative, single call, per turn:** the merged function (name:
`interpret_turn`, superseding both the old `understand()` and the old
`interpret_turn()`/`verify()` — see decision below) produces:

```
InterpretationResult:
    state_deltas: list[StateDelta]          # §1 — always applied in full
    operations: list[Operation]              # §1 — ordered, ≥1
    conversational_repair: "confirmation" | "cancellation" | "correction" | None
    durable_fact_candidates: list[DurableFactCandidate]

StateDelta:
    kind: "preference_add" | "preference_remove" | "constraint_add" |
          "constraint_remove" | "exclusion_add" | "exclusion_remove"
    key: str
    value: str | None
    domain: str                              # §5
    scope: "session" | "durable"

Operation:
    capability: Capability
    resolved_entities: list[ResolvedEntity]  # N-ary, §7
    reference_query: ReferenceQuery | None    # §7's constraint-primitive shape
    question_type: QuestionType
    cart_operation: CartOperation | None
    depends_on_prior_operation: bool          # true = resolve against THIS
                                               # turn's own preceding operation's
                                               # output, not persisted state
```

**Decision — `understand()` and `interpret_turn()` are merged into one
call, not kept as two.** The architecture decision document retained them
as separate calls (Stage 1 delta/durable-fact extraction, Stage 2
interpretation). Stress-testing this against compound turns and against
this project's OWN prior empirical finding (PROGRESS.md Phase 13/14: the
two-signal OR-combination of `interpret().durable_fact_candidates` and
`understand().priorities_add` was explicitly retired because "both are the
model re-judging the identical semantic question in a different field of
the SAME kind of call... their failures are correlated on hard turns, not
independent") shows keeping them separate reintroduces exactly that
already-diagnosed failure mode at the ARCHITECTURE level, not just for
preference extraction. **One call, one schema, one failure mode.**
`apply_delta` runs immediately after this single call and only after it —
never before, never from a second call's output.

**Fallback-only, never co-equal (unchanged from the architecture
document):** `capability.py::route()`/`SOFT_CAPABILITIES` (9 closed-class
heuristics, Gap P), `topic_transition.py::decide_transition`'s blind guess,
and `intent_resolution.py::resolve_cart_target`'s own separate LLM call.

**Decision — no cascading independent LLM fallback.** The architecture
document left `resolve_cart_target`'s own `interpret_cart_reference` call
"conditional" (only called when the merged interpretation didn't already
resolve a cart operation). Stress-testing §11 (provider failure) shows this
is insufficient: if the merged call fails outright (not merely "didn't
address cart"), `resolve_cart_target` must NOT attempt its own separate
LLM call — a second independent model call after the first has already
failed can succeed or fail independently, producing an answer basis the
rest of the turn never agreed to. **Rule: once the single interpretation
call has been attempted and failed for this turn, every remaining decision
for that turn uses ONLY deterministic fallback logic — no further LLM call
of any kind is attempted.** This retires `resolve_cart_target`'s own LLM
call as an independent authority entirely (its deterministic remove/
set_quantity-against-real-cart logic is retained; only its own separate
`interpret_cart_reference` model call is removed, folded into the single
interpretation's `cart_operation` field).

---

## 4. Fallback/degradation contract

**Safe deterministic fallback (permitted to run when interpretation is
unavailable):** any fallback that either (a) takes no new semantic action
and produces an honest clarifying/degraded reply, or (b) commits only a
STATE-PRESERVING, self-correcting placeholder — never a new, uncorrectable
semantic commitment.

**Semantic fallback (must never independently commit conversational
state):** a fallback that guesses a NEW topic push with real content, a
cart operation, or a capability classification that leads to an
irreversible action, when the primary interpreter is unavailable.

**Adversarial finding:** `topic_transition.py::decide_transition`'s blind
guess, under total interpretation failure, can commit a "push" (a brand-new
topic) that nothing later corrects — the existing category-mismatch
self-heal in `node_finalize` only corrects a wrongly-kept "continue"
against real retrieved evidence; it does not verify a wrongly-guessed
"push". **Decision:** generalize the self-heal to cover BOTH directions —
every blind topic-shape guess (push OR continue) is treated as
**provisional** until the turn's own real, verified product evidence either
confirms it (label/title word-overlap matches) or contradicts it (mismatch
→ retroactive correction), exactly as already proven for "continue"; this
closes the asymmetry without inventing a new mechanism. If the turn's own
retrieval ALSO fails (total degradation), the provisional commitment stays
inert (an empty-labeled or unconfirmed topic) and is corrected by whichever
LATER turn first produces real evidence — never presented as confirmed
before that.

**Every terminal reply built from a fallback-shaped classification appends
`degraded_disclaimer(trace)`** (Gap I) — unchanged from the architecture
document, but now stated as a **co-requirement of the interpretation
reorder, not a later phase**: shipping the reorder (§3) without this
disclosure would make wrong fallback substitution silent rather than
merely less frequent. See §10 phase ordering.

---

## 5. Preference/memory semantics

Full taxonomy, decided:

| Type | Enforcement | Storage | Cancellation |
|---|---|---|---|
| Hard constraint / must-have | Deterministic filter — a product failing it is never shown, regardless of ranking | Session `StateDelta` (`constraint_add`) or per-turn mission-item field | Explicit `constraint_remove` only |
| Exclusion | Deterministic filter (inverse of must-have) | Same as above | Explicit `exclusion_remove` only |
| Preference / priority | Soft ranking signal, weighted, never filters | `StateDelta(kind="preference_add")`, carries a `domain` | See below |
| Temporary preference | `scope="session"` — cleared at session end (inactivity boundary), never promoted to durable unless the shopper's own language generalizes it ("I always...", "for future reference...") | Session state row | Explicit cancellation, or session end |
| Durable preference | `scope="durable"` — written to `db/memory_facts.py`, read every future session | Cross-session fact store | Explicit cancellation (tombstone, below) |
| Cancelled preference | **Decision:** cancellation writes a tombstone (`status="cancelled"`), never a bare deletion — a bare delete cannot prevent the same fact being re-derived from replayed conversation history on a later turn; a tombstone is checked before any re-extraction is trusted | Same row, `status` field | N/A (terminal state until explicitly re-stated) |
| Domain-scoped preference | `domain: str` is a real extracted value (e.g. "running_shoes"), not a fixed enum. Ranking applies it only when the CURRENT retrieval's own category/query shares real word-overlap with the domain (the same `_distinctive_title_words`-style mechanism already proven elsewhere in this codebase — no new matching technology) | Same row | Scoped removal: "forget the lightweight preference" removes the entry whose domain/value matches the NAMED preference, not merely the most-recently-stated one (an additive precision over the existing "remove most recent" mechanism) |
| Cross-domain preference | `domain="*"` (an explicit sentinel the interpreter sets when the shopper's own language generalizes — "I only buy sustainable brands", "regardless of category") — applied to every retrieval regardless of domain match | Same row | Same tombstone rule |

**Decision — durable-vs-session precedence rule (a real ambiguity the prior
documents left unresolved).** When a durable fact and a session-stated
preference address the same domain and disagree (durable: "prefers decaf";
this session: "I want regular coffee this time"), the SESSION-scoped,
MORE RECENT, EXPLICITLY-STATED preference wins for ranking this session;
the durable fact is left untouched in storage (it still applies in a future
session) unless the shopper's own language targets the durable fact
specifically ("forget that I ever said I prefer decaf" / "stop remembering
that"). A bare "forget that" cancels whichever is currently ACTIVE and
influencing this session's ranking (the session override if one exists,
else the durable fact) — never both by default.

---

## 6. Recommendation/comparison reasoning contract

**Deterministic/LLM responsibility boundary for recommendation (decided,
generalizing the architecture document's `RecommendationContext`):**
candidate retrieval, hard filtering, ranking/scoring, and `fit_reasons`
computation are 100% deterministic (`mission_ranking.rank_and_filter`,
unchanged). The LLM's role is exactly two things: (a) mapping the turn's
natural language into `RecommendationContext`'s inputs (already the single
interpretation's job, §3), and (b) phrasing the "why" explanation text,
which must be grounded — every claim traceable to a real `fit_reasons`/
`stated_terms` entry, never free invention. The LLM never re-orders or
overrides the deterministic ranking's own top pick.

**Decision — one shared grounded-reasoning contract for comparison,
explanation, alternatives, AND recommendation**, not four separate
mechanisms (the architecture document only ported the grounding pattern
into `compare.py` specifically; stress-testing shows this reintroduces
duplicated logic across the other three). Single contract:

```
reason_over_evidence(question_type, resolved_entities, evidence: TurnEvidence,
                      context: RecommendationContext) -> GroundedReply

GroundedReply:
    claims: list[GroundedClaim]     # each claim cites the real evidence/
                                     # attribute/stated_term it derives from
    text: str                        # assembled only from claims above
```

This one function replaces `compare.py`'s own synthesis call, `answer.py`'s
`_relationship_reply`/`run_explanation`/`run_alternatives`/
`run_recommendation`'s separate reply-construction logic, reusing
`respond.py`'s already-proven, already-adversarially-hardened
`_honest_discovery_reply`/`_deterministic_personalized_reply` pattern as
its implementation, not a new mechanism. N-ary capable (resolves the
architecture document's `resolved_entities` widening, §1/§7) — "which of
these is the best value" over 4 shown items reasons over all 4.

---

## 7. Reference-resolution contract

**Adversarial finding: the architecture document's fix (widen the closed
`reference_kind` enum with two more values — "constraint", "disjunctive")
does not survive stress-testing.** Every additional novel phrasing
("the one on the left" — spatial; "the one two down from the cheapest" —
composed ordinal+sort; "either of the two under $100" — filter+set-op
combined) would require yet another enum value and yet another branch,
recreating the exact accretion pattern Gap C diagnosed. An enum, however
wide, is not the fix.

**Decision — reference resolution is redesigned as an orthogonal
constraint-satisfaction query over the current candidate set, not a
closed taxonomy of "kinds":**

```
ReferenceQuery:
    sort_key: str | None            # an attribute name (via attributes.py's
                                      # already-open, honest-fallback resolver)
    sort_direction: "min" | "max" | None
    position: int | None             # supports negative ("last"); composes
                                      # with sort (e.g. sort=price,min,
                                      # position=1 = "the cheapest")
    filter_predicate: (attribute, operator, value) | None   # "under $50"
    named_terms: list[str] | None    # word-overlap against candidate titles
    set_op: "all" | "any" | None     # "all of these" vs. "either of these"
    scope: "active_topic" | "paused_topic" | "all_topics" | "named_source"
    source_capability: Capability | None   # "the one you recommended"
```

Every representative example in the target spec (ordinal, superlative,
comparative_pair, named, all, "the one you recommended", historical) is a
COMPOSITION of these orthogonal fields, not a new enum value. A genuinely
novel phrasing either decomposes into this primitive set (resolved
correctly, generalizing for free) or does not (honest clarifying question,
same bar as before — never a silent guess). This directly answers the
stress-test question: YES, this is now general enough without a growing
enum, because new PHRASINGS map onto existing PRIMITIVES; only a genuinely
new primitive (not yet seen in any conversation class) would require a
schema change, and none is identified by this review.

**`escalate_ambiguous_reference`'s bounded retry** is invoked from this one
shared resolution entry point for every capability (Gap D closed
architecturally, not per-capability).

---

## 8. Pending-action contract

**Adversarial finding: the architecture document's three fixed kinds
(`fulfillment_variant`, `cart_proposal`, `comparison_question`) are not
exhaustive.** Counter-examples: a pending MISSION-item disambiguation
("which of these did you mean for the energy gel?"), a pending ORDER
disambiguation (Gap M's own fix), a pending PREFERENCE-conflict
clarification ("you said both budget-conscious and premium-only — which
matters more?"). Each of these would otherwise need its own new
`PendingAction.kind` value — the same accretion risk as §7.

**Decision — reduce to exactly two general kinds, not enumerate every
case:**

```
PendingAction:
    kind: "clarification" | "commerce_action"
    candidates: list[Entity] | None       # real, resolvable options — used
                                            # by "clarification" (mission item,
                                            # order, comparison question,
                                            # preference conflict — all are
                                            # "which of these did you mean")
    payload: dict                          # kind-specific mechanics (e.g. a
                                            # commerce_action carries the
                                            # concrete cart mutation to confirm,
                                            # covering BOTH the old
                                            # fulfillment_variant and
                                            # cart_proposal cases — both are
                                            # "here is a concrete mutation,
                                            # confirm or reject", not two
                                            # different concepts)
    created_turn_id: str
```

One handler, keyed on `conversational_repair` (§3) and the pending kind:
`confirmation` resolves against the pending action's own candidates/
payload; `cancellation` withdraws it regardless of kind (the existing
`fulfillment.py` cancellation logic is the proven template); `correction`
replays against it; nothing pending → honest "nothing to confirm" degrade.
This answers the stress-test question affirmatively: yes, two general
kinds cover every confirmation/cancellation/correction case identified,
including cases not in the original three, without opening a third parallel
mechanism.

---

## 9. Can current-product-set, `LastRendered`, Focus Stack, and
`ConversationContext` ever disagree after a transition?

**No, provided three invariants hold, all now decided:**

1. **Exactly one writer** for the current-product-set triad — the
   generalized `finalize` (§2), with mission's and tradeoff's own direct
   writes absorbed into it, not left as parallel writers (§14).
2. **`ConversationContext` is rebuilt exactly once, unconditionally, as the
   terminal step of every turn** (§2) — not conditionally (as today, gated
   on `_reconcile_authoritative_resume` firing), and not until every
   operation in a compound turn's chain (§1) has completed.
3. **Intra-turn chained operations (§1) resolve against an intra-turn
   scratch handle, never against the persisted `ConversationContext`
   mid-turn.** This is a genuinely new requirement surfaced only by the
   compound-turn stress test (§1): if operation 2 ("compare these") needs
   operation 1's ("search trail shoes") just-produced results, it must read
   them from the turn's own in-memory chain state, not from
   `ConversationContext` (which is correctly stale until the terminal
   rebuild in invariant 2). Conflating these two would either force a
   premature mid-turn rebuild (reintroducing partial-state risk) or cause
   operation 2 to see stale data. Keeping them separate resolves it
   cleanly.

Given these three, the four objects are updated atomically at one point per
turn and read consistently by every node before that point — they cannot
disagree at any turn boundary a later turn could observe.

---

## 10. Does the LangGraph ordering guarantee no semantic decision or
irreversible mutation happens before authoritative interpretation, when
available?

**Yes, given the following, all decided in this review:**

- The single interpretation call (§3) runs immediately after
  `load_context`, before `topic_transition`, before any capability dispatch,
  before any cart-operation check.
- `topic_transition` and `resolve_cart_target`'s remaining deterministic
  logic (§3's "no cascading LLM fallback" decision) **consume** the single
  interpretation's `context_action`/`cart_operation` when present — they
  never independently re-derive meaning when real interpretation succeeded.
- When interpretation is genuinely unavailable, every commitment made by
  the deterministic fallback is **provisional** (§4's generalized self-heal)
  — never presented as final until corroborated by the turn's own real
  evidence or a later turn's real signal.
- No cart mutation (`ActionType`-bearing) is ever constructed from a
  fallback guess (§3, §11) — only from a resolved `cart_operation`,
  itself only ever produced by the single interpretation call succeeding.

The one caveat, stated honestly: `apply_delta` (writing `state_deltas`) runs
immediately after the single interpretation call, before topic_transition —
this IS a real mutation before topic-shape is decided, but it is a
**data-only** mutation (preference/constraint values), never a
topic/capability/cart decision, and it is exactly what the merged call
(§3) authorized — not an independent guess. This is consistent with the
invariant, not an exception to it.

---

## 11. Can provider failure cause a wrong capability substitution, false
preference state, false cart state, false topic state, or false success?

Traced against the decisions above, each individually:

- **Wrong capability substitution:** can still occur (the deterministic
  fallback can still classify "add X" as DISCOVER, per the historically
  observed bug) — but it is now always disclosed (`degraded_disclaimer`,
  §4, shipped as a co-requirement of the reorder). Item 13's actual bar
  ("must not falsely imply... uncertainty must result in appropriate
  clarification") is about honesty, not omniscience — disclosed
  substitution meets the bar; silent substitution does not. **Closed.**
- **False preference state:** the merged call either succeeds (real
  `state_deltas`) or fails entirely (empty `state_deltas`, per §3's
  no-cascading rule) — there is no code path where a fallback INVENTS a
  preference delta. **Closed.**
- **False cart state:** a cart mutation requires a resolved
  `cart_operation` from the single interpretation call; under total
  failure, no fallback constructs one (§3's decision to retire
  `resolve_cart_target`'s own independent LLM call removes the one
  remaining path that could have produced a second, divergent guess).
  **Closed.**
- **False topic state:** the generalized self-heal (§4) treats every blind
  guess as provisional against real evidence; a totally-degraded turn (no
  interpretation AND no real retrieval) leaves the commitment inert and
  correctable by a later turn, never presented as confirmed. **Closed.**
- **False success:** unchanged and already correct — a "success" claim is
  only ever written after real `run_trusted_action` (Decision→Risk→
  Execution→Adapter) succeeds; nothing in this review touches that path.
  **Confirmed, not at risk.**

**Ordering dependency this finding imposes on §10's phase plan:** the
reorder (interpretation-first) and the disclosure (Gap I) must ship
together, not as separate phases — the reorder alone reduces HOW OFTEN
wrong substitution happens (fewer redundant guesses) but does not make it
honest; only the disclosure does that. Shipping the reorder without the
disclosure would be a real, if temporary, regression against item 13.

---

## 12. Platform independence — Northfield REST vs. Kettle GraphQL

**Confirmed: nothing in this architecture is merchant-specific.**
Retrieval, cart, order, and payment operations reach the adapter interface
exclusively — no code path above the adapter branches on merchant identity
(confirmed in the gap analysis's "retain as-is" list: compatibility/
complementary determination reads platform-declared `product_relations`,
never a code branch; payment-recovery availability is a declared
capability, filtered at the Decision Engine). The one real cross-merchant
divergence found empirically (PROGRESS.md: Kettle's "beans" zero-candidate
bug, a policy-Q&A routing miss) is a RETRIEVAL-QUALITY gap, not
conversational-logic branching — its fix (Gap O, §13) extends the SAME
rescue mechanism discovery already has to missions, uniformly, with no
merchant-specific code. This architecture introduces no new
merchant-specific logic anywhere in §1-9 above.

---

## 13. Closing the 40k-SKU retrieval question at the architectural level

**What must be empirically proven vs. what requires an architectural
decision, separated:**

The confirmed structural risk (Gap L): `rank_and_filter`'s hard-constraint
filter runs only over whatever page the adapter's own keyword-relevance
search returned (bounded at 20, or up to 500 via the semantic widener) —
a hard constraint (budget/exclusion) is never proven correct against the
TRUE full catalogue, because the underlying adapter search ranks by
keyword relevance, not by constraint satisfaction, and there is no
guarantee every constraint-satisfying product surfaces within any fixed
page size at real catalogue scale. **This cannot be closed by testing
alone — no volume of empirical testing on a 40k-SKU fixture proves a
bounded-page approach is correct in general, since correctness depends on
the adapter's own ranking behavior for arbitrary future queries, not on
any specific test's queries.** This is the architectural decision this
review is required to make, not defer:

**Decision:** for any constraint that maps to a real, structured product
field (budget/price range, category exclusion, a declared attribute),
enforcement uses **exhaustive pagination until the true candidate set is
satisfied or a real, disclosed cap is hit** — never a single bounded fetch
treated as complete. Concretely: the retrieval loop pages through the
adapter's `search_products` (or an equivalent broader query) until either
(a) enough constraint-satisfying candidates are found to rank confidently,
(b) the adapter signals the true end of its result set (a short/empty
page), or (c) a real, generous safety cap is hit — and in case (c), the
reply **must** disclose the truncation exactly as `_scan_catalog` (the
already-correct, already-verified merchant-console pattern) already does
for catalogue scanning, reusing that proven mechanism rather than
inventing a new one. Where an adapter declares (capability-gated, never a
code branch) native structured filtering (e.g., a price-range or category
parameter on `search_products`), that is used as a performance
optimization layered on top — never a correctness requirement, since not
every adapter will declare it.

**What remains for empirical validation (not an architectural gap):**
whether the pagination-until-satisfied approach performs acceptably
(latency, adapter load) at real 40k-SKU/six-figure-case volume, and
whether the disclosure wording reads honestly at that scale. This is a
production acceptance criterion (§12 below... **renumbered §14 in the
required section list — production acceptance criteria**), not an
open architecture question — the correctness MECHANISM is decided here.

---

## 14. Other competing sources of truth, duplicate mechanisms, or hidden
mutation paths beyond Gaps A-S

Found by this review, not present in the prior catalogue:

- **`node_mission`'s and `node_tradeoff`'s own direct writes to
  `active_products`/`recent_product_groups`** (confirmed in the graph.py
  trace) would become a SECOND and THIRD writer alongside the generalized
  `finalize` once Gap S's fix lands — recreating the exact "two writers,
  no rebuild" class of bug being fixed, unless explicitly absorbed.
  **Decision:** both are refactored to hand their resolved product set to
  the SAME generalized `finalize` path (§2's single-writer invariant),
  removing their own direct writes rather than leaving them as a parallel
  mechanism. This is a required addition to Phase 0's scope beyond what
  the architecture document specified (it described tradeoff's existing
  direct write as already-correct "retain as-is"; this review revises that
  — it was correct ONLY while it was the sole non-discovery writer, and
  stops being safe once `finalize` becomes universal).
- **`mission_ranking.rank_and_filter`'s callers pass ad hoc context
  shapes.** Discovery, recommendation, tradeoff, and mission each
  currently assemble their own input to this function somewhat
  independently. **Decision:** every caller must pass through the same
  assembled `RecommendationContext`-shaped object (§6), not four
  independently-built dicts — otherwise the identical stated constraint
  could be enforced inconsistently across capabilities, a subtler version
  of the "competing source of truth" pattern this review was asked to hunt
  for.
- **Durable-vs-session preference precedence** (§5) was a genuine
  unresolved ambiguity in every prior document — not a code defect, but an
  unspecified semantic rule that any implementation would otherwise have
  to invent ad hoc, risking a fifth undocumented "authority." Resolved in
  §5.
- **Intra-turn chained-operation resolution** (§1, §9) is a hidden
  mutation/staleness risk that only appears once compound turns are
  considered — not present in the single-operation-per-turn design the
  prior documents implicitly assumed. Resolved by the intra-turn scratch
  handle in §9.

No further competing-truth pattern was found beyond these four plus the
eighteen already catalogued in Gaps A-S.

---

## 15. Security invariants

Unchanged, restated for completeness (out of scope for revision by this
review; confirmed not touched by anything in §1-14):

- The AI cannot assert risk; risk properties come from a static table.
- Money never moves unattended; `FINANCIAL_ALWAYS_HUMAN` is unconditional.
- Payment has no action type; only a shopper's own tap reaches `/pay`.
- A tap beats the model's guess (`chosen_variant` overrides `variant_id`).
- Raw platform errors never reach shoppers.
- Cross-tenant/cross-shopper isolation via `db.owners` and the httpOnly
  visitor cookie.
- Idempotency: a cart is claimed once at creation, keyed on cart id alone.

Every decision in §1-14 operates strictly above the Decision→Risk→
Execution→Adapter boundary; none constructs a mutation, asserts a risk
property, or reads/writes payment state.

---

## 16. Production acceptance criteria

Before this architecture is considered PROVEN in production (not merely
DESIGNED — the distinction this review's mandate draws):

1. The full long, multi-topic, compound-turn conversation class (§1's
   adversarial example plus the product-behavior spec's own long
   conversation) runs clean on both merchants, including at least one
   genuinely compound turn per run.
2. A scripted, test-double-LLM regression matrix (not live-model-dependent)
   proves: no silent capability substitution, no false preference/cart/
   topic state, honest disclosure on every forced-failure turn (§11),
   exercised across all twelve capability classes.
3. The pagination-until-satisfied retrieval mechanism (§13) is measured
   against a real 40k-SKU/six-figure-case synthetic fixture for latency and
   disclosure correctness — the mechanism is already decided; this
   measures its production performance, not its correctness.
4. `ConversationContext`/current-product-set/Focus Stack/`LastRendered`
   agreement (§9) is asserted programmatically at the end of every turn in
   the regression suite, not just reasoned about.
5. Cross-merchant parity (§12) reconfirmed on both Northfield and Kettle
   for every capability class, including the Kettle-specific
   retrieval-rescue extension (Gap O).
6. Existing invariant suites (`fuzz.py`, `auditroutes.py`, `invariant-
   guard`) remain clean throughout — no regression against the six
   security invariants at any point in implementation.

---

## 17. Remaining architectural blockers

None. Every gap identified by this review (§1, §4, §6, §7, §8, §11, §13,
§14) was resolved with a concrete decision within this same document,
per this review's own mandate to design the production end state now
rather than defer. Nothing in §1-16 is left as an open question requiring
further design discussion before implementation can begin.

---

## 18. Already solved — must not be redesigned

Confirmed correct by direct code inspection in the prior two documents;
nothing in this review revises any of the following:

- `fulfillment.py`'s variant detection (generic across attribute types)
  and its direct-add execution path (real `run_trusted_action`, no
  shortcut).
- `compatibility.py`'s compatibility/complementary determination (grounded
  in real platform-declared data, never an LLM guess).
- `attributes.py`'s open-vocabulary attribute-name resolution with honest
  fallback for any unrecognized attribute.
- `intent_resolution.py`'s "add" logic removal (Phase 10) — clean, no
  dead-code residue.
- `checkout.py`/`cart_status.py`/`payment_recovery.py` — genuinely
  low-risk, no reference-resolution decision to make.
- `evidence.build_evidence`/`already_verified_evidence` — the two shared,
  already-universal evidence constructors.
- `respond.py`'s honest-synthesis discipline (`_honest_discovery_reply`/
  `_deterministic_personalized_reply`) — reused, not redesigned, as the
  implementation of §6's shared grounded-reasoning contract.
- `_scan_catalog`'s honest-truncation-disclosure pattern — reused, not
  redesigned, as the implementation of §13's retrieval decision.
- The entire Decision→Risk→Execution→Adapter pipeline and all six security
  invariants (§15).
- Cross-tenant/cross-shopper isolation, idempotent payment, tap-beats-
  model precedence.

---

## 19. Final verdict

**A. PRODUCTION ARCHITECTURE READY FOR IMPLEMENTATION.**

Every stress-test question in this review's mandate was answered with
either a confirmation (§2's ownership matrix holds; §9's four state objects
provably cannot disagree; §11's five failure modes are each closed; §12's
platform independence holds; §15's security invariants are untouched) or a
concrete, decided revision to the prior architecture (§1's compound-
operation schema; §3's single merged interpretation call and no-cascading-
fallback rule; §4's generalized bidirectional self-heal; §6's one shared
grounded-reasoning contract; §7's constraint-primitive reference schema;
§8's two-kind pending-action model; §13's pagination-until-satisfied
retrieval mechanism; §14's single-writer corrections to mission/tradeoff).
No question was left open, hedged, or deferred as "discover during
implementation." Implementation may begin against §1-15 of this document
as the authoritative design, validated against §16's production acceptance
criteria.
