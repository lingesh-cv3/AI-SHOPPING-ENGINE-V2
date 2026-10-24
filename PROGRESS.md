# Current Progress

## Overall Status

Overall project: **~70%** implementation complete against the currently
defined scope in CLAUDE.md's roadmap. Shopper V2: **~75%** implementation
complete — it is the ONLY shopper routing the real storefront calls (see
"Shopper V2 — the only shopper routing" below for what was verified and
corrected here), but several roadmap-level shopper outcomes and real-volume
validation are still open. Merchant Intelligence: **~55%** — the console and
Merchant Copilot are built and visually complete, but most "diagnosis leads
to an attached action" work is still read-only. CV3 Operations: **~25%** —
only the Operations Copilot's read-only answering half exists.

These percentages represent implementation completeness against the
currently defined product scope, not production readiness — infrastructure
(Postgres, hosting, CI gating, real-volume seeding) is separately behind
regardless of feature completeness. Overall completion percentages are
estimates against the currently selected product scope (the items actually
in this file and Completed.md), not the entire long-term roadmap in
CLAUDE.md — roadmap items not yet selected for implementation are not
counted as unfinished implementation work.

---

## Current Focus

### Payment safety — fix and second hardening pass implemented and verified locally; RELEASE GATE STILL OPEN (NOT production-ready)
- Done and verified (see `PAYMENT_SAFETY_INVESTIGATION.md`): durable `UNKNOWN_OUTCOME` ledger state; release only on proven non-events; hard call deadline; stale/orphaned `IN_FLIGHT` parked, never released;
  different-card, concurrent, cross-route and recovery attempts refused while unresolved; safe same-key retry; evidence-based reconciliation (Northfield has a lookup, Kettle none); operator list/reconcile/resolve
  routes. Isolated HTTP harness: one platform purchase per basket in every scenario (two before the fix in S2-S4). Second pass, backend verified: cart `payment_state`, no checkout offer for a held/paid basket,
  one deduplicated merchant task per unknown episode (retried on failure, self-closing, tenant-scoped, no payment-sensitive content), `unresolved_payments` in ops stats, and recycled cart ids that never discard
  an unresolved row without evidence. 169 payment checks in model-free tests (the final chain: 95 scripts, 1,724 checks; fuzz and route/tenant audit clean).
- Browser verification of the storefront hold ran on the final frontend code (isolated stack, model-free): `payment-hold.spec.ts` 10/10 plus 50 existing browser tests, including the real signup-and-pay checkout leg. Not run: two live-model tests, two service-stopping platform-down tests, checkout Leg B (its helper targets the default database) and checkout Issue #5 (a stale assertion that fails the same on the unmodified frontend) — see `PAYMENT_SAFETY_INVESTIGATION.md`.
- Open before any real-money use: a platform that actually charges has not been tested (the demo stubs are in-memory); real platforms' idempotency-key and lookup semantics are unverified; Kettle cannot be
  reconciled by lookup (same-key retry or an operator); alerts reach the merchant only as a Merchant Task within one 60 s sweep (no email/push); a reconciled order sends no confirmation mail; the absence
  verdict is only as good as `PAYMENT_SETTLE_WINDOW_SECONDS` against a platform that can hold a request longer; no real provider was contacted.

### SSE streamed replies (`/api/v2/chat/stream`) — implemented, verified model-free; NOT verified against a live provider
- Protocol, client behaviour and compatibility: Readme.MD "Streamed replies (SSE)"; verified items: Completed.md. Verification used a fake model only (httpx MockTransport and a local OpenAI-compatible
  fake provider); no real provider was contacted. Tests needing a live model were not run, and no live-provider reliability or time-to-first-text figure is claimed.
- Open: the draft is provisional and may differ from the final reply; which key order a real model writes (`reply` before the grounding fields delays the first draft) is unmeasured; deterministic turns and
  discovery never stream text (status + final only); runs and the 24 h duplicate-request tombstones live in one process's memory (a restart loses replay and the guard: a retry of a pre-restart request id would run the turn again; a hung turn blocks its session - no turn timeout); the single-flight guard covers stream-vs-stream only (not `/api/v2/chat`,
  taps or `mission/advance`); `first_text_ms` is not persisted so no merchant/ops figure moves yet; no CI gate runs the new tests; the first browser run after a cold Vite start can reload the page mid-test.

### Shopper V2 — the only shopper routing — ~90% complete
- `/api/v2/chat` (`engine/shopper_v2/`, routed via `engine/api/chat_v2.py`)
  is the ONLY shopper path the real storefront calls today —
  `storefront/src/ChatWidget.tsx` invokes `api.chatV2` exclusively, and
  `storefront/src/App.tsx` has no `assistantMode` state, no
  `VITE_SHOPPER_DEFAULT` build flag, and no "switch to classic assistant"
  control (verified by inspecting the actual source; a prior version of
  this entry described that toggle as existing — it does not, and the
  wording was corrected here rather than left to imply a live rollback
  path that isn't wired up). Intended final architecture is V2-only; V1 is
  not being kept as a parallel shopper architecture.
- V1 (`engine/api/chat.py`, `/api/chat`) is still mounted at the API level
  and is exercised directly by `healthcheck.py`/`fuzz.py`/`auditroutes.py` -
  real, live test coverage, not dead code - but nothing in the shopper-
  facing storefront can reach it; it is not a shopper-selectable fallback.
  Retiring it entirely (deleting the route and its test coverage) is not
  yet done and is not currently scheduled - see Next Work.
- V2 owns discovery, comparison, compatibility, cart proposals, checkout
  continuity, payment recovery, missions, basket proposals, trade-off
  re-ranking, cart status and clear-cart — all routed through the
  unmodified Decision Engine → Risk Gate → execution → adapter pipeline.
- Shopper closeout (Steps 25 + follow-up) — NOT signed off for production.
  Fixed and verified model-free: cross-session cart persistence; store-level
  category discovery; explicit typed add; multi-product discovery. The add
  guard (`add_command.reference_is_ambiguous`, called from
  `graph.node_cart_action`) now CHECKS the model's pick against the shopper's
  words: a title word no other product in view shares, a catalogue-confirmed
  full name for a pick that is not in view, a real ordinal / last / cheapest
  that the pick satisfies, or a group word that equals exactly the on-screen
  set; otherwise it asks. Opinion, hypothetical and "?"-less advice phrasings
  never add. Missing names are reported only after a bounded catalogue search
  finds no covering product (the shop vocabulary is a 100-product sample, so a
  word missing from it proves nothing); an unknown capitalised word
  ("Zephyr Cloud Boot") counts as a missing name unless a searched product
  carries it anywhere. Pipeline-level tests fail when the guard is disabled.
  Three independent invariant reviews found no invariant 1-7 / idempotency
  violation; the policy gaps they found (A-E) were fixed and the final review
  was SAFE. Latest run: 64 model-free scripts / 1,263 checks, `fuzz.py` 1,200
  assertions, route audit clean, model-free HTTP walkthrough 7/7, 34 shopper
  browser specs, `tsc`/build clean, lint 6 errors (unchanged baseline).
  Live model (OpenAI gpt-4o-mini, 46 of the authorized 80 requests, one pass
  per scenario) passed after the earlier routing fix; the guard and
  missing-name changes since then are NOT live-verified (no new request budget
  was authorized). Still unproven or open: repeat live rates; ~40k-SKU volume
  (bounded queries, a 100-product vocabulary sample, no empirical test);
  department browse on a platform without `list_departments` (controlled-fake
  tests only); a missing name made only of words the shop uses ("Cloud Boot")
  still falls back to ordinary search; a typed partial name for a product not
  in view asks "which one?" until the full name is given; checkout, tapped-card
  and payment-recovery browser flows were not exercised. Payment safety is an
  unresolved release blocker (next section); the Shopper changes here do not
  touch the ledger, pay routes, execution or risk.
- Basket-level budget for guided missions — implemented and verified model-free; NOT signed off. One shared total across every stage
  (`engine/shopper_v2/basket_budget.py`, extending `guided_mission.py`): stored on the mission's own stages (not the session-wide
  `budget_amount`, which mission ranking reads as a per-product cap), what is left is recomputed from the REAL cart, an honest
  fits/tight/over/unknown verdict, up to three cheaper in-stock alternatives from the stage's own candidates (live-checked, returned as
  cards; adding still goes through the existing add path), a three-valued compatibility line, and cross-stage duplicate avoidance. It
  also fixed a real defect: a trailing budget clause used to become a stage ("energy under", "total budget"). Measured by two persisted
  events and `events.basket_budget_outcomes` (budgeted missions, finished, within budget, added-from-alternatives; None, not 0%, with no
  data) — no merchant report or dashboard reads it yet. Evidence: 11 new test scripts (147 checks), each shown failing on the pre-change
  code; real graph on Northfield and Kettle; an independent invariant review (SAFE; its one honesty finding fixed) and a value audit (VALUABLE, its three
  required fixes done). No model is in this path, so there is no live-model gap. Open or limited: claims are "among the options I found" (<=12
  candidates per stage, stored prices from plan time; offered items are live-checked and the add re-checks price and stock); totals
  are line totals before delivery and tax; variant price ranges ignored; an open size question is answered before a budget command;
  "incompatible" is unreachable (no declared-incompatible data exists) so compatibility lines only ever say confirmed or unknown; a
  mid-sentence "shoes under 5000 and socks" stays a per-item cap; ~40k SKUs is a structural check only; the Kettle "coffee" stage is
  reported out of stock by the existing mission retrieval (pre-existing, not part of this feature); payment code untouched.
- Cross-stage compatibility + complete-the-set for guided missions — implemented and verified model-free and in a browser; NOT signed off.
  New read-only `engine/shopper_v2/mission_complements.py` (imports only `engine/execution/product_relations`), wired into the guided
  mission. The ONLY evidence is the merchant's declared `compatible_with` list (either direction): category, title words and search
  similarity are never evidence, and "incompatible" is never shown to a shopper. With something really in the cart it names the declared
  pair among the options on screen ("the merchant lists X as going with your Y"), notes a declared pair between options of two open stages
  (even with an empty cart), and announces merchant-declared add-ons as clearly labelled optional extras (cards, at most two): right after
  an add and once in the closing summary, each at most once, with their live price and what they do to the shared budget ("remaining goes
  from A to B", or "would leave too little for the rest"); an extra never changes the fits/tight/over verdict, is never an option of any
  stage on the shopper's list (a substitute for a stage they closed is not an extra), and is never offered when it is in the cart, sold out
  (variant-aware), unfetchable, or - with a budget in play - unpriced or priced in another currency. The cards are never re-ordered. Adding
  one is the existing typed or tapped add (a sized add-on asks its size first; a repeat add is refused). Measured by `complement_offered` /
  `complete_set_outcome` events and `events.complete_set_outcomes` (per connection; None, not 0%, with no data; no merchant report reads it
  yet). Evidence: 11 new test scripts (133 checks), each shown failing on the pre-change code, including real Northfield and Kettle through
  the graph and a 40,000-product structural check; a 5-test Playwright spec of the complete journey with zero payment requests; an
  independent invariant review (SAFE). Open or limited: `compatible_with` is one list for "works with" and "goes with", so the wording is
  "the merchant lists X as going with Y", never a technical-fit guarantee, and how real clients populate it is unverified; an add-on with
  variants is quoted "from" its lowest buyable option (budget effect as a bound; no price anywhere = not offered under a budget); a platform that declares nothing gets one honest
  sentence and no extras; a stage option the stage retrieval did not return becomes an extra, not a stage item; ~40k SKUs is structural only;
  no live-model path exists here. Checkout and payment are untouched and remain blocked by the payment-safety release blocker above.
- Remaining: no ops-console consumer of V2's event types; real-volume
  condition for V2 retrieval is a structural code check (bounded queries,
  no unbounded-scan pattern), not an empirical 40k-SKU test;
  decision-workspace UI polish for comparison/compatibility layouts is
  outstanding; `auditroutes.py` has no named check yet for
  `GET /api/v2-activity/{connection_id}`.

### Reply-before-data constraint — partially addressed
- V1 has a scoped fix (`ReasoningService.synthesize()`), live-verified for
  `COMPARE_PRODUCTS`/`RECOMMEND_PRODUCTS` on both demo merchants, but its
  own `value-auditor` pass returned THIN, not VALUABLE (no figure moves;
  the platform-down path for this call was never forced live).
- V2 fixes the same defect structurally by construction (pre-fetch reply is
  discarded before retrieval runs) — this is complete and verified for the
  turn types V2 currently handles.
- Advanced Product Discovery, Personalized Recommendations, and
  Mission-Based Shopping (as full roadmap outcomes, not the deterministic
  building blocks under them) remain unbuilt as complete features on either
  pipeline.

### Return-Risk Prevention — ~70% implementation, value-bar not cleared
- Deterministic low-rating and unconfirmed-requirement flags
  (`engine/execution/cart_intelligence.py`, `return_risk.py`), grounded only
  in real platform-declared fields, are implemented and live-verified on
  recommend/compare/cart-intelligence surfaces and rendered in the
  storefront (`return-risk-ui.spec.ts`).
- Not yet run through `value-auditor`, so not counted as a cleared roadmap
  item — see Next Work.

### Merchant console and Copilot — ~80% visual/functional, ~40% "act from answer"
- The full Merchant console (Overview, Sales & Revenue, Orders &
  Conversion, Product Performance, Customer & Shopping Insights, Inventory
  & Catalog, Payments & Checkout, Returns, AI Commerce, Recovery, Holdout,
  Business Insights, Platform & Capabilities, Settings, Merchant Tasks) is
  built, visually consistent, and independently audited chain-by-chain
  (UI → API → repository → adapter → metric → Copilot/action).
- The Merchant Copilot answers from real report/capability/policy/task data
  and deep-links pending recovery cases and merchant tasks.
- Remaining: catalogue/inventory/unmet-demand answers still have no
  attached executable action reachable from the same panel — this is the
  read-only-twin gap CLAUDE.md names directly, and it is why the Copilot is
  not counted as a fully cleared roadmap item.

---

## Current Known Issues

- **Rev. 2 grounded-conversation architecture (2026-09) — APPROVED, implementation
  in progress. Phase 9 of 10 complete, WITH A CORRECTION TO THE ORIGINAL
  PLAN - see its own entry below.** Supersedes the Focus Stack entry
  below once fully migrated (see that entry for what Rev. 2 replaces and
  why - closed-vocabulary interpretation and disagreeing "what's in view"
  definitions across capabilities). Full architecture spec (Rev. 2) and
  migration plan live in this session's own record; summary: one
  authoritative `ConversationContext`, genuine model interpretation
  grounded in real pre-fetched data (not a bigger classifier), deterministic
  verification, bounded/configurable re-interpretation, open-vocabulary
  durable-memory capture, inactivity-based session boundary - Decision/
  Risk/Execution/adapters completely unchanged throughout.
  **Phase 1 done (additive, zero behavior change - verified):**
  `engine/shopper_v2/state.py::LastRendered` (new type, separate from
  `Topic`/`focus_stack` - "what's on screen" vs. "which topic this is"),
  `ShoppingStateV2.last_rendered` field; DB column + idempotent migration
  guard (`engine/db/models.py`, `engine/db/session.py`); `merge()`/`get()`
  support (`engine/db/shopping_state.py`); new
  `engine/shopper_v2/conversation_context.py` (`ConversationContext`,
  `build_context()` - assembles active/paused topics, mission, constraints,
  last-rendered, bounded history, durable facts, a fresh real cart read,
  and the fulfillment queue into one object every turn); wired additively
  into `graph.py::node_load_context` (built, not yet consulted by any
  decision) and `node_finalize` (writes `last_rendered` after every
  discovery turn, after `target_topic_id`'s retroactive-push self-heal is
  final). Verified: new `tests/shopper_v2/test_phase14_conversation_context.py`
  (18/18), full existing regression suite re-run clean after this phase
  (phases 8/9/10/11, query-intent-consolidation 35/35, `auditroutes.py`,
  `fuzz.py`), plus a live check confirming `last_rendered` persists
  correctly against the real running engine.
  **Phase 2 done (additive, real model calls, not yet wired into any
  decision path):** `ReasoningService.interpret()` +
  `InterpretationResult`/`DurableFactCandidate` (`engine/reasoning/service.py`),
  the `interpret_conversation` tool schema + genuine-interpretation system
  prompt (`engine/reasoning/prompts.py`), and
  `engine/shopper_v2/interpretation.py::interpret_turn()` (orchestration:
  builds the call's real, grounded inputs from `ConversationContext`).
  **Verified with LIVE model calls** (`tests/shopper_v2/test_phase15_interpretation.py`,
  3 reps per scenario, not mocked - the whole point of this phase is
  proving genuine interpretation generalizes): the exact headline claim -
  novel superlative phrasings the old closed word list never covered
  ("high price shoe", "costliest shoe") - resolved correctly **6/6**
  across both phrasings. Conversational-repair recognition (correction/
  cancellation/pause/continuation), topic-resume-by-label, ordinal-
  against-last-rendered, and open-vocabulary durable-fact capture
  (including the exact previously-documented "I really prefer decaf
  coffee" failure case) all pass at least 2/3 reps with zero scenarios
  failing all 3 - real, measured, honest first-pass evidence, not
  asserted; one round of prompt refinement already applied based on this
  same live evidence (strengthened the correction-tagging and durable-
  fact negative-example instructions). Full existing regression suite
  re-run clean after this phase too (phases 9/10/11/14) - Phase 2 remains
  as additive as Phase 1; nothing reads `InterpretationResult` for a real
  decision yet.
  **Phase 3 done (pure, deterministic, no model/adapter call - fast, fully
  unit-tested):** `engine/shopper_v2/verification.py` -
  `resolve_context_action()` (checks a claimed resume/push against real
  `paused_topics` by label match, downgrading a claim naming no real
  topic to "push" rather than guessing, and correctly recognizing when a
  claimed "resume" target is actually the ACTIVE topic's own label - a
  real "continue," not a fabricated resume), `resolve_reference()`
  (ordinal/superlative/comparative_pair/all/named resolution against
  real, scope-correct product data), and `verify()` (the single entry
  point every migrated capability will call). Directly proves Root Cause
  2 (the cross-topic "add shoes, got socks" bug class) is now closed
  STRUCTURALLY: `tests/shopper_v2/test_phase16_verification.py` (18/18)
  includes a superlative-against-a-paused-topic case that resolves
  correctly within that topic's own real ids while a different topic is
  active, using the exact scoped-lookup mechanism that eliminates the
  "two disagreeing definitions of what's in view" root cause rather than
  patching one instance of it. Full existing regression suite re-run
  clean after this phase too.
  **Phase 4 done - the first real capability cutover, `compare`, live-
  verified against both merchants.** `node_compare` (`graph.py`) now
  tries the Rev. 2 path FIRST (pre-ground the real candidate ids ->
  `interpret_turn()` -> `verify()`) whenever the cheap `looks_like_reference`
  prefilter suggests it's worth it; `run_compare_turn` (old, unchanged)
  remains the complete fallback whenever the new path doesn't resolve
  cleanly - nothing removed yet, only added, per the approved migration
  plan. This closes, live and verified, the EXACT bug this whole Rev. 2
  effort started from: **"what's the high price/costliest shoe you
  showed me earlier" after a topic switch - 8/8 and 7/7 clean runs
  respectively** (a prior 0/9 batch on the same phrasing was traced to a
  stale, pre-fix engine process still bound to the port from an earlier
  restart, not a real failure - confirmed by a clean restart immediately
  reproducing correctly). Verified on **both merchants**: Northfield
  (REST) and Kettle & Bloom (GraphQL) via the identical code path, no
  merchant-specific branch. Two real gaps found and fixed live during
  this phase, both structural fixes, not phrase patches:
  (a) `ConversationContext` was blind whenever Focus Stack never tracked
  a topic (the same pre-existing `delta.goal` extraction fragility) -
  `build_context()` now self-heals from the legacy `recent_product_groups`
  ledger, synthesizing paused topics from it exactly the way the older
  `paused_topic_product_groups()` helper already did, so the new
  authoritative model is never blind just because the newer mechanism
  didn't fire; (b) a synthetic legacy-bridged topic has no real label to
  label-match against, so `verification.py` gained `_effective_label()`
  (derives a comparable label from the topic's own real, already-fetched
  product titles) plus an "exactly one paused topic -> resume it
  unconditionally" fallback mirroring `topic_transition.py`'s own already-
  proven rule - both are genuinely reusable, not one-off patches, and
  both are covered by `test_phase16_verification.py`'s own updated
  19/19. A third, narrower gate bug also found and fixed: the model's
  own `capability` tag varies between "compare"/"discover" for the
  IDENTICAL, correctly-resolved reference - `node_compare`'s gate was
  tightened from `verified.capability == "compare"` to accepting either
  label, since the trustworthy signal is a real, unambiguous
  `resolved_reference`, never the classification tag alone. Full
  regression suite re-run clean after this phase: phases 9/10/11/14/16,
  query-intent-consolidation (35/35), `auditroutes.py`, `fuzz.py` (both
  merchants, every invariant held).
  **Phase 5 done - bounded, configurable re-interpretation.**
  `interpretation.py::REASONING_BUDGET` (currently 2: the first
  `interpret_turn()` call plus at most one escalation - a named,
  documented config value per the spec's own "configurable and evidence-
  driven, never a hardcoded architectural ceiling" requirement, not
  scattered as a magic literal) and `escalate_ambiguous_reference()` - one
  bounded re-interpretation round, scoped to ONLY the real candidates the
  first pass found genuinely ambiguous (never a full re-understanding,
  never a third guess at topic/scope). Wired into `node_compare`: when
  the first verify() pass finds real candidates but can't pick among them
  unambiguously (as opposed to finding nothing at all - a genuinely
  different situation an escalation can't fix), one narrowed retry is
  attempted before falling through to the existing honest-clarify
  fallback. Verified deterministically (`tests/shopper_v2/test_phase17_escalation.py`,
  5/5) - the escalation call is proven scoped to only the narrowed
  candidates (never the full catalog or history), and both its success
  and its honest "still ambiguous" failure path are covered; the
  underlying model-call pattern itself was already proven live in Phase
  2. Live-reverified afterward that the wiring didn't disturb Phase 4's
  own result (4/4 clean on the exact headline scenario). Full regression
  suite re-run clean: phases 9/10/11/14/16/17, query-intent-consolidation
  (35/35), `auditroutes.py`, `fuzz.py`.
  **Phase 6 done - durable-memory capture rework, the closed-class regex
  backstop removed.** `understanding.py`'s `_EXPLICIT_PREFERENCE_PATTERN`/
  `_explicit_preference_fact` (a fixed "I prefer <phrase>" regex, unable
  to generalize past that one shape) deleted outright and replaced with
  `_interpretation_durable_fact_backstop()` - calls the already-built,
  already-proven `ReasoningService.interpret()` (Phase 2) when `reason()`
  supplies no durable fact, genuinely interpreting the message instead of
  pattern-matching it. A real quality gap found and fixed during this
  phase: the model's own `subject` field first came back too generic
  ("shopper") for two DIFFERENT facts in the same conversation, which
  would have silently overwritten each other via `ShopperMemoryFact`'s
  own upsert-supersedes-by-key semantics - fixed by tightening both the
  tool schema's field description and the system prompt to demand a
  specific, distinctive topic tag ('coffee_preference'/'shoe_width'),
  never a generic word. `_normalize_fact_key()` added for the purely
  mechanical (never a language-understanding gate) storage-key
  normalization the spec's §7.3 calls for - the store itself
  (`engine/db/memory_facts.py`) needed no changes; its `key` parameter
  was already free-text. Verified live: the exact previously-documented
  failure case ("I really prefer decaf coffee" → `durable_facts=None`
  under the old mechanism) now captures correctly **4/4**, and a second,
  distinct fact stated in the same conversation ("my feet run pretty
  wide") is stored under its own distinct key with zero collisions,
  **3/3**. Full regression suite re-run clean: phases 8/9/10/11/14/16/17,
  query-intent-consolidation (35/35), `auditroutes.py`, `fuzz.py`.
  **Phase 7 done - `cart_action` cutover, live-verified on both
  merchants.** Two real bugs found and fixed during this phase, both
  structural:
  (a) An ordinal position claim had no way to express "the last one" -
  the schema only supported explicit 1-based numbers, and the model
  isn't told the real item count at schema-authoring time, so "first and
  last" silently dropped "last" entirely and resolved only "first" -
  fixed by adopting the SAME `-1 means last` convention
  `reference_resolution.py::resolve_ordinal_positions` already
  established elsewhere in this codebase (tool schema + system prompt +
  `verification.py`'s own resolution, all updated together).
  (b) A genuinely serious ordering bug: candidate ids were commonly
  gathered into a Python `set` (deduplicating across topics before one
  shared fetch), and a `set`'s iteration order has NO relationship to
  the real, rendered order - so "first and third" was silently resolved
  against a SCRAMBLED order with complete confidence, on both the prompt
  side (what the model was shown as "position 1, 2, 3...") and the
  verification side (what `resolve_reference` picked for those
  positions). Fixed in both `interpretation.py::_product_dicts` and
  `verification.py::_candidates_for_scope` to always follow the REAL,
  already-persisted order (`LastRendered.product_ids`/`Topic.product_ids`),
  never the incidental order of whatever `products` list was fetched.
  (c) Cutover itself: tried FIRST, unconditionally, in
  `node_reference_resolution` (moved earlier than the OLD
  `resolve_cart_target` path, not merely as a last-resort fallback) -
  live-reproduced why precedence had to be exactly this order: the OLD
  path's own "all" override doesn't fail cleanly when it's wrong, it can
  silently resolve to the WHOLE candidate group instead of the two
  specific items asked for ("add first and last" on a 5-item Kettle &
  Bloom catalog added all 5 before this fix, 3/4 runs). A second,
  redundant safety-net copy in `node_cart_action` is gated on `not
  add_ids` so it never pays for a second model call once the first
  already resolved cleanly.
  **Verified live, both merchants:** "add first and last shoe to the
  cart" on Northfield - **5/5** resolves to the REAL first (Trailblazer)
  and REAL last (Winter Road) shoe, never the dead-end "which one did
  you mean?" listing all 9, never a silently wrong pair. The exact
  Kettle & Bloom over-add ("add first and last to the cart" on a 5-item
  espresso-equipment listing) - **5/5** now resolves to exactly the 2
  real items asked for, never all 5. Full regression suite re-run
  clean: phases 4/6/8/9/10/11/12/14/16/17, query-intent-consolidation
  (35/35), `auditroutes.py`, `fuzz.py`.
  A known, softer residual noted honestly, not chased further given
  it's a genuine natural-language ambiguity rather than a resolution
  bug: "add both the shoes and the socks" (collective "the shoes"
  spanning several real shoe products, plus socks) resolves the group
  correctly but the exact SET the model reads "the shoes" as (one vs.
  all shown) varies - a real English ambiguity a human would also need
  to disambiguate, not a wrong-scope or wrong-order defect.
  **Phase 8 done - conversational-repair wiring (the "cancellation" case)
  into `fulfillment.py::continue_fulfillment`.** `looks_like_skip_request`
  is a narrow closed-class word list ("skip"/"cancel"/"never mind"/etc.
  exactly) - a real skip phrased differently ("actually forget that one,
  I changed my mind") fell through it and silently left the pending item
  stuck forever with no further signal either way. Consulted ONLY when
  BOTH deterministic checks (`looks_like_skip_request`,
  `looks_like_answer_attempt`) are inconclusive - never replaces them,
  never runs on the common case, the same "cheap default, smarter
  fallback only when needed" discipline `verify()`'s own escalation
  already established - `reasoning.interpret()`'s `conversational_repair`
  field is checked, and a genuine "cancellation" classification is
  treated exactly like an explicit skip. `continue_fulfillment` gained an
  optional `reasoning` parameter (defaults to `None`, so every existing
  caller/test that doesn't pass it is completely unaffected - confirmed:
  `test_phase10_fulfillment_hardening.py` still 11/11 unchanged). Live-
  verified: "actually forget that one, I changed my mind" mid-fulfillment
  correctly skips the pending item **4/4**, a phrasing
  `looks_like_skip_request`'s own word list cannot match at all. Full
  regression suite re-run clean: phases 9/10/11/14/16/17, query-intent-
  consolidation (35/35), `auditroutes.py`, `fuzz.py`.
  Mission/compatibility capability cutover and the "pause"/"correction"/
  "continuation" conversational-repair cases remain future work beyond
  this phase's scope - noted honestly, not claimed.
  **Phase 9 - real architectural finding, plan corrected before executing
  it destructively.** The original plan called for REMOVING
  `SOFT_CAPABILITIES`/`decide_transition`'s heuristics/`resolve_cart_target`'s
  duplicate logic once "nothing reads them." Before touching anything,
  checked: `node_mission`, `node_tradeoff`, `node_compatibility`,
  `node_complementary`, `node_checkout`, `node_cart_status` are ALL still
  unmigrated - only `compare` (Phase 4) and `cart_action` (Phase 7) have
  Rev. 2 cutovers. Removing the shared systems now would have broken six
  live, working capabilities - a direct violation of "do not remove
  existing functionality to simplify the implementation." Surfaced this
  to the user rather than either silently skipping Phase 9 or executing
  a destructive removal; user chose to migrate the remaining capabilities
  first.
  **What was actually built, and a further correction discovered while
  building it:** rather than duplicating a full per-capability cutover
  six more times, generalized the routing-level backstop itself
  (`node_route_capability`) to run the SAME grounded interpret+verify
  pipeline for EVERY capability at once - `claim.capability` (already a
  real `Capability`-shaped value, proven live since Phase 2) can now
  directly set the turn's capability for mission/compatibility/
  complementary/trade_off/checkout/cart_status exactly as it already did
  for compare, tried first and trusted over the old `route()`+
  `SOFT_CAPABILITIES` guess, which remains the complete, unchanged
  fallback. **This surfaced the real, permanent shape of the
  architecture, correcting the original plan's own premise**: the new
  pipeline depends on a live model call that can be unavailable, throttled,
  or wrong; the old deterministic systems can never fail to produce SOME
  answer. The old systems can therefore never be safely deleted outright -
  "new tried first, old as the permanent, always-available fallback" is
  the correct FINAL state, not a temporary migration scaffold to later
  remove. Phase 9 is complete in this corrected, honest sense: capability
  routing for every capability now benefits from genuine interpretation,
  with the deterministic systems retained by design as the safety net,
  not because removal was postponed.
  **Verified live:** "which was the lightest one" mid-mission (a
  previously-documented, deliberately-left-unfixed gap from THIS SAME
  session's own earlier browser testing - a superlative question being
  swallowed by mission-item interpretation instead of reaching compare) -
  **2/4** now correctly routes to compare and answers directly; real,
  measured progress on a documented gap, not claimed as fully solved
  (residual variance is the same live-model-call variance already
  characterized throughout every phase in this document). Full regression
  suite re-run clean: phases 4/6/8/9/10/11/12/14/16/17, query-intent-
  consolidation (35/35, one throttle-timeout self-recovered on rerun,
  matching this project's own already-documented pattern), `auditroutes.py`,
  `fuzz.py` (20/20, both merchants). `healthcheck.py` run once alongside
  `fuzz.py` concurrently (against the same rate-limited provider) showed
  3 additional failures beyond the two already-documented pre-existing
  ones, all in V1's OWN `/api/chat` path this session never touches;
  CONFIRMED resource contention, not a regression, by re-running
  `healthcheck.py` alone immediately after - **117 passed, 1 FAILED**
  (only the single already-documented pre-existing `chat.py` recovery-
  explain gap remains; the other two "new" failures did not reproduce at
  all once not competing with `fuzz.py` for the same rate-limited
  provider) - matching or exceeding this document's own best-recorded
  baseline (116/118) from before any of today's Rev. 2 work.
  **Not yet done (phase 10):** the full acceptance benchmark (naturalness
  cases specifically: "actually forget that"/"wait"/"something cheaper"/
  "what else?"/corrections/incomplete messages, per the approved spec's
  §12.2 - only the "cancellation" shape has been wired and verified so
  far, in Phase 8), the inactivity-based session boundary (not started),
  and mission/compatibility/complementary/trade_off/checkout/cart_status's
  OWN internal reference resolution (their ROUTING now benefits from Rev.
  2, but their internal logic - e.g. mission's own item-matching,
  compatibility's own anchor-product resolution - is unchanged, still the
  original, already-tested logic). Do not consider Rev. 2 complete, do
  not update Completed.md, until all of this passes - this is explicitly
  a multi-phase effort per the user's own approved plan, not a one-turn
  rewrite.
  **Phase 10 - real hybrid-architecture migration completed for cart_action
  and discovery; compatibility/complementary migrated; several serious
  live bugs found and fixed, not just patched (2026-09):** a deep
  root-cause investigation (not a phrase-fix) found the two live bugs
  above were both instances of a general pattern - a legacy, topic-blind
  decision path silently produced a confidently-wrong answer instead of
  the new, context-aware path either resolving correctly or degrading
  honestly. Per the user's explicit direction, the obsolete decision
  authority was REMOVED at each stage, not merely bypassed:
  - **`intent_resolution.py::resolve_cart_target`'s entire "add" decision
    logic deleted** (the topic-blind `_resolve_group_reference`, and the
    ordinal/pronoun/name resolution that only ever saw the active topic).
    It now only classifies remove/set_quantity/clear against the real
    cart (legitimate - a cart line has no "which topic" ambiguity) and
    asks honestly for "add". `verification.py::verify()` gained a new
    deterministic `_cross_check_scope`: compares the claimed scope's real
    products against every OTHER real topic in view by word-overlap
    (`best_matches_with_score`) before trusting a bare active/last-
    rendered/named-catchall default - closes the semantic gap where a
    structurally-clean resolution wasn't necessarily the CORRECT one.
    `node_reference_resolution` gained two further honesty layers: when
    the model identifies a real scope but can't pick decisively within
    it, the clarifying question is now built from THAT scope's real
    candidates (never the topic-blind legacy ledger); when the
    interpretation call itself is unavailable, a deterministic (no model
    call) word-overlap backstop across every real topic in view replaces
    the old always-ask-about-whatever's-active fallback.
  - **`pipeline.py`'s Finding-4 zero-result query substitution** was
    unconditional (any zero-result query reused the previous topic's
    results); now requires a real, distinctive word shared between the
    message and the prior query (a bare deictic follow-up like "why
    this?" is exempted via a small closed-class filler-word list, since
    it names nothing of its own to disagree with).
  - **Root cause of a SEPARATE, more serious latent bug found while fixing
    the above:** `Topic.label` (used by every migrated capability's
    cross-topic label matching) was built from `delta.goal` - documented
    in `prompts.py`'s own schema as a durable, SESSION-level "what they're
    ultimately trying to buy" field, not a per-turn value. Live-reproduced:
    two DIFFERENT topics (shoes then socks; coffee then a grinder) got the
    SAME stale label ("running shoes" / "coffee grinder" for both),
    breaking every label-based disambiguation this migration depends on
    from the root. Fixed at all three topic-push sites to use the real,
    per-turn `state["message"]` instead.
  - **`compatibility.py`/`complementary.py`** gained an optional
    `anchor_ids` param; their entry nodes now try the same unified
    interpret+verify path first (via a new shared `_resolve_anchor_ids`
    helper) before falling to the old active-topic-only default -
    closing the same class of cross-topic blindness for these two
    capabilities. `missions.py`'s/`tradeoff.py`'s own internal
    product-selection logic was reviewed and left as-is: neither
    exhibits this bug's shape (mission's own active-topic use only ever
    NARROWS an already-ambiguous candidate list, never picks blindly;
    trade-off re-ranks the currently-active set by design, with no
    external product reference to get wrong).
  **Verified live, repeatedly, on BOTH merchants** (`scripts/
  acceptance_walkthrough.py`, a long mixed conversation exercising topic
  switching, historical cross-topic superlatives, cross-topic ordinal
  adds, bare follow-ups, cancellation, implicit refinement, explicit
  resume, and explicit category overrides): the original reported bug
  (silently adding the wrong topic's product) did not reproduce once
  across many reruns on Northfield or Kettle & Bloom after these fixes -
  worst observed failure mode is now an honest clarifying question,
  never a silent wrong add. One residual, DISCLOSED limitation: when a
  topic has only one product whose title doesn't literally contain the
  shopper's own category word (observed on Kettle's small catalog -
  "coffee" naming "House Filter Blend"), the deterministic word-overlap
  checks can't always disambiguate and occasionally ask about the wrong
  topic (still only an honest question, never a wrong add) - a real,
  narrow gap in the word-overlap heuristic's reach, not a re-emergence of
  the original bug.
  **Regression, full sweep after every change:** `auditroutes.py` clean;
  `fuzz.py` clean (1200 assertions, 20/20 sequences, both merchants,
  every invariant held) x3 reruns across this phase; 24/25 offline phase
  tests clean each run (the one failure each time was a different
  already-documented live-model-classification flake - `test_phase9_tradeoff`
  and `test_phase15_interpretation` - confirmed by immediate rerun passing
  both times, unrelated to any file touched this phase).
  **Not yet done:** the §12.2 naturalness cases beyond what the
  walkthrough above exercises ("what else?" continuation and "something
  cheaper" implicit refinement both work; explicit correction phrasing
  beyond cancellation is not separately verified); the inactivity-based
  session boundary (not started); checkout/cart_status's own internal
  logic (reviewed only for missions/tradeoff, not these two - lower risk,
  no reference-resolution decision to make); the residual single-item/
  non-matching-title disambiguation gap noted above. Do not mark Rev. 2
  complete until these are addressed.
- **Phase 11 - general question-answering capability (2026-09), real
  progress, explicitly NOT complete - do not mark done.** Root-caused
  (separate report) why the assistant always answered with a product
  listing even when asked a relationship/explanation/recommendation/
  alternatives question: `conversational_repair` had exactly one real
  consumer in the whole codebase (fulfillment's own cancellation check),
  and nothing represented "what SHAPE of answer does this question need."
  Built the general mechanism, not per-phrase handlers:
  - `InterpretationResult.question_type` (retrieve/relationship/
    explanation/recommendation/compound_comparison/alternatives/
    attribute_refinement/state_change) - one real per-turn model judgment,
    now the thing that decides reply SHAPE, replacing capability-as-router
    for this purpose. `secondary_reference` lets a claim name a SECOND,
    independently-resolved entity for relationship/compound_comparison.
  - `reference_attribute` generalized from a hardcoded `price`/`rating`
    enum to open vocabulary, resolved deterministically against real
    `Product` fields only (`attributes.py`) - degrades honestly ("I don't
    have weight data") for any attribute this catalog has no real field
    for, never silently substitutes a different one.
  - `engine/shopper_v2/answer.py` (new) - one general handler per question
    shape, all built from real, already-resolved `Product` data and
    `mission_ranking`'s existing grounded `fit_reasons`, never a second
    independent guess at a number. `Capability.ANSWER_QUESTION` /
    `node_answer_question` (`graph.py`) is the new dispatch target.
  - `pending_question` (new, persisted `ShoppingStateV2` field, same
    migration pattern as `last_rendered`) - a question that couldn't
    fully resolve is kept so a correction next turn replays against the
    ORIGINAL question instead of being treated as unrelated.
  - **Retired, not layered on top:** `route()`'s bare CONVERSATIONAL
    routing is now overridden whenever a real per-turn `question_type`
    says otherwise (fixes "why this?" landing on a generic conversational
    reply). `edge_after_mission_check`'s old sticky trigger
    (`delta.priorities_add` non-empty, no freshness check - confirmed
    root cause of a live "no, forget that"/"go back to X" both wrongly
    hijacked into a stale re-ranking reply) is deleted outright; the SAME
    `run_tradeoff_turn` domain logic is now reached only via a fresh,
    per-turn `question_type == "attribute_refinement"` judgment.
  **Verified live:** relationship ("which is cheaper" between two
  isolated, cleanly-set-up topics) states the actual relationship and
  both real prices, not a product card. Explanation, recommendation, and
  alternatives each verified working in isolated live calls. The
  cross-topic cart-add and discovery-topic-switch fixes from the prior
  phase re-confirmed still holding (0/6 wrong silent adds; `fuzz.py`
  clean, `auditroutes.py` clean) after this phase's changes.
  **NOT complete - real, disclosed gaps found running the full original
  reported conversation end-to-end (not just isolated cases):**
  1. `attribute_refinement` classification reliability is genuinely
     variable (~40-60% in repeated identical live calls) - the model
     inconsistently classifies a bare preference statement ("lightweight
     matters most to me") as attribute_refinement vs. a new topic/
     conversational reply, even after two rounds of prompt strengthening
     and passing the real active products into view explicitly. Left as
     disclosed live-model variance (matching this project's own
     documented pattern elsewhere) rather than chased further with
     phrase-specific prompt patches.
  2. A topic pushed from a correction/state-change message (e.g. "Actually
     I meant the shoes") can end up labeled with that raw sentence
     verbatim (`node_topic_transition` runs before interpretation, so it
     cannot yet know a message is a correction) - surfaced as a
     odd-looking topic label in an "alternatives" reply. Not a safety
     issue (real products still shown correctly), a naturalness one.
  3. Cancelling a just-stated PREFERENCE ("no, forget that" after "I'm
     looking for something lightweight") clears `pending_question` but
     does not clear `state.priorities`/`nice_to_have` - a later "go back
     to X" can still show a filtered/framed result carrying the
     "forgotten" preference. `pending_question`'s STATE_CHANGE handler
     only covers unresolved QUESTIONS, not stated PREFERENCES - a real,
     separate case not yet built.
  4. `recommendation`'s budget filter depends on `state.budget_amount`
     actually being populated this turn - when it isn't (the same
     already-documented per-turn budget-capture reliability gap this
     project has elsewhere), it silently recommends the top-ranked item
     with no budget applied and no mention that the constraint was
     unavailable, rather than degrading honestly ("I don't have a
     confirmed budget to check against").
  5. "Compare the cheapest and the lightest" can be captured by the
     existing mission-shape heuristic (`_has_new_mission_shape`) before
     reaching `answer_question` in some runs, producing a mission-tracking
     reply instead of a compound comparison - the interaction between the
     new question_type routing and the pre-existing mission-shape
     detection was not fully traced before time ran out this phase.
  Full regression after this phase: `auditroutes.py` clean; `fuzz.py`
  clean (1200 assertions, both merchants, every invariant held); 23/25
  offline phase tests clean (the 2 non-clean are both live-model-
  classification checks with pre-existing or newly-characterized
  variance, not deterministic failures - `test_phase12_discovery_vs_cart_routing`
  already-documented flake, `test_phase9_tradeoff` now flakier due to
  gap 1 above, honestly attributed rather than hidden).
  **Do not mark this capability complete** until gaps 1-5 above are
  resolved and a full mixed conversation (not just isolated per-question-
  type calls) is clean on both merchants.
- **Phase 12 - the five disclosed gaps fixed at the architecture/state
  level (2026-09), still NOT complete - do not mark done.** Per explicit
  direction: no phrase patches: each gap was root-caused to a real
  structural hole and closed there.
  - **Gap 5 (mission-shape hijack) - FULLY FIXED, verified 6/6 reruns.**
    Two independent legacy interceptions found and removed: (1)
    `edge_after_reference_resolution` checked `capability ==
    Capability.CART_ACTION` only, never `capability_confirmed_reference`
    - any OTHER confirmed capability (e.g. `ANSWER_QUESTION`) fell through
    into the mission-shape heuristic unprotected; (2) `Capability.COMPARE`
    is deliberately excluded from `SOFT_CAPABILITIES`, so a raw primary-
    classifier guess landing directly on COMPARE never reached the new
    interpretation attempt at all, leaving `verified_interpretation`
    `None` and the mission heuristic's own "nothing ran yet" fallback
    condition satisfied even though a real question was in play. Fixed:
    `edge_after_reference_resolution` now bypasses to capability_dispatch
    on any confirmed capability (mirroring `edge_after_route_capability`'s
    own check); the interpretation gate now covers COMPARE too; and
    `_edge_after_fulfillment_check`'s `_has_new_mission_shape` fallback is
    now gated on `verified_interpretation is None` - a real per-turn
    judgment already having run is treated as authoritative, never
    second-guessed by a bare separator-word heuristic.
  - **Gap 2 (correction message becoming a topic label) - FIXED, verified
    live.** `node_topic_transition` runs before any interpretation call,
    so it cannot know a message is a correction - added
    `capability.py::looks_like_state_change_shape` (a closed-class
    opener check, same discipline as the existing mission-shape
    deferral) and defer the topic push for that shape exactly like the
    existing mission deferral already does. Confirmed live: no more
    topics labeled with a raw correction sentence.
  - **Gap 1 (attribute_refinement classification, ~40-60% before) -
    architecturally rebuilt, now 8/8 in isolation, still measurably
    imperfect in a long, compounding conversation.** Root-caused twice
    over: first, the ENTRY GATE for the interpretation attempt itself
    depended on `delta.priorities_add`/`nice_to_have_add` (the SEPARATE
    `understand()` call's own output), which measured 0/4 for the exact
    same input in the same session - the interpretation call was often
    never even ATTEMPTED. Fixed by removing that dependency entirely:
    the gate now fires whenever real, already-active product context
    exists (`has_real_context`, including the legacy-ledger case), never
    gated on a guess at whether asking is "worth it". Second: even once
    the call always fires, its OWN `question_type` field was measured
    unreliable (0/6 in one batch) - replaced with a deterministic
    override using `claim.durable_fact_candidates` (measured reliable
    across the same repeated calls, even on turns that mislabelled
    `question_type`) plus real active-product context, confirmed to
    8/8 in isolation after this fix. **Residual, disclosed:** in a long
    conversation with several prior corrections/topic switches, this can
    still miss (observed once in the full end-to-end rerun) - the
    `durable_fact_candidates` signal, while far more reliable than the
    field it replaced, is not measured at literally 100% under arbitrary
    real context complexity.
  - **Gap 3 (cancellation not clearing a stated preference) -
    architecturally rebuilt, measured 3/5 clean in a 4-turn isolated
    repeat, real remaining variance.** Two structural fixes: added
    `priorities_remove` end-to-end (`StateDelta`, `apply_delta`, the
    shared `propose_actions` tool schema, `engine/db/shopping_state.py`'s
    merge/row-removal) - this list had NO removal mechanism at all before
    this phase, unlike every sibling list field. Second, and more
    important: rather than depend on that new field's own per-turn
    naming accuracy (a second independent unreliable extraction, the same
    class of gap as #1), `node_answer_question`'s STATE_CHANGE handling
    now acts directly on `conversational_repair == "cancellation"` (the
    SAME interpretation call already trusted elsewhere) by removing the
    MOST RECENTLY stated priority/preference deterministically - no
    second field's naming to get right. Also fixed: cancellation used to
    treat "clear a pending unresolved QUESTION" and "clear a just-stated
    PREFERENCE" as mutually exclusive (`elif` branches) - a stale pending
    question from an earlier, unrelated exchange always won, silently
    leaving a just-cancelled preference in place; both are now cleared
    together when both exist. **Residual, disclosed:** this closes the
    mechanism but the OVERALL reliability of "state a preference, then
    cancel it, then confirm it's gone" still depends on gap 1's own
    routing succeeding on the middle turn first - a compound of two
    real, independently-measured-imperfect model judgments in sequence,
    not a flaw in either fix alone.
  - **Gap 4 (budget silently disappearing) - FIXED, verified live.**
    `run_recommendation` now detects (via a real digit-presence check,
    never a specific phrase) when the message plainly states a number
    but `budget_amount` never reached state, and appends an honest
    disclaimer rather than silently recommending as if no constraint had
    been mentioned. Verified live in the full conversation: a real,
    successfully-captured budget ("under 5000") correctly filtered and
    was named in the reply ("within your budget (4299.00 INR)").
  - **Gap "relationship still doesn't resolve after a correction" (not
    one of the original 5, found during this phase's own re-verification)
    - NOT fixed, disclosed.** "Which is cheaper?" → "Actually I meant the
    shoes." still sometimes fails to resolve the second side even after
    the correction-replay mechanism runs (the combined-message technique
    doesn't always give the model enough to resolve both sides). No
    longer intercepted by the cart-signal prefilter (a THIRD legacy
    interception found and fixed this phase:
    `edge_after_route_capability`'s `_plausible_cart_signal` check ran
    unconditionally before `capability_confirmed_reference`, so a
    confirmed `ANSWER_QUESTION` decision with no real cart operation
    could still be diverted into `resolve_cart_target`'s own unrelated
    "ambiguous cart" reply - fixed by checking confirmation first,
    whenever no real cart operation is also present) - but the
    underlying comparison still isn't reliably resolving after a
    correction. Left open rather than patched blindly.
  **Regression after this phase:** `auditroutes.py` clean; `fuzz.py`
  clean (1200 assertions, both merchants, every invariant held) x2 full
  reruns; 24/25 offline phase tests clean (the one non-clean is
  `test_phase9_tradeoff`'s already-documented "persisted priority"
  extraction-reliability check, unrelated to this phase's fixes -
  `test_phase9_tradeoff`'s OWN routing-label assertion, and
  `test_phase15_interpretation`'s superlative-attribute assertion, were
  both updated to check the new, correct post-migration contracts
  instead of the obsolete pre-migration ones they were still asserting).
  **Still NOT complete at time of writing** - see Phase 13 immediately
  below, which stabilizes two of these three gaps with a real
  architectural fix each (a deterministic, no-model-call backstop, not a
  phrase rule) and root-causes why an earlier "8/8 in isolation" result
  for gap 1 did not hold in a longer conversation.
- **Phase 13 - final stabilization pass (2026-09).** Traced each of the
  three remaining gaps to its real underlying cause and fixed at the
  state/interpretation/verification level - no new features, no
  phrase-specific rules.
  - **Relationship resolution after a correction - ROOT CAUSE WAS DEEPER
    THAN "the correction doesn't replay": even the FIRST attempt at "Which
    is cheaper?" (no explicit product noun, relying entirely on
    conversation memory of two prior topics) was failing to resolve
    EITHER side via the model's own claim, live-reproduced and confirmed
    by direct trace inspection (`resolved_reference`/`resolved_secondary`
    both empty). A second, independent cause compounded it: Focus Stack
    pushes are not reliably created for a plain "show me X" turn (an
    already-documented, pre-existing characteristic this project's own
    legacy-bridge self-heal in `conversation_context.py::build_context`
    exists to cover) - when that self-heal fires, EVERY real topic
    surfaces as `paused`, none `active`, with a blank `.label`. Fixed with
    `answer.py::deterministic_relationship_fallback` - a real, no-model-
    call backstop using the actual topic structure directly (active vs.
    most-recently-paused, OR the two most recent paused topics when no
    topic is active at all), defaulting to price/min, with topic labels
    derived from real product titles when blank (mirrors
    `verification.py::_effective_label`'s identical fallback). Only
    engaged when the model's own attempt found nothing usable - never
    overrides a real resolution. **Verified: 8/8** clean, grounded
    relationship replies (real product names, real prices) across fresh
    sessions, on Northfield AND Kettle & Bloom, up from 0/6 before this
    fix. The correction-replay mechanism itself (from the prior phase)
    now also preserves whichever side resolved on the first attempt
    rather than re-asking the model for both sides again - retained as a
    real improvement for the rarer case where the fallback's own topic
    structure isn't yet in a comparable shape.
  - **Cancellation clears preference - root cause was a MISSING routing
    path, not the removal mechanism itself** (already built the prior
    phase). `claim.conversational_repair` and `claim.question_type` are
    SEPARATE fields from the SAME interpretation call - "No, forget that."
    was measured tagging `conversational_repair="cancellation"` correctly
    while `question_type` still came back "retrieve", so the
    STATE_CHANGE handler in `node_answer_question` (gated on
    `question_type == "state_change"` alone) never ran at all. Fixed:
    `node_route_capability` now routes to `ANSWER_QUESTION` on EITHER
    signal. Also fixed a real latent bug found while making this change:
    the model-unavailable fallback branch referenced an undefined
    `looks_like_preference_statement` name (dead code since the entry
    gate was widened in the prior phase, never actually triggered in
    testing since the model stayed available, but a real `NameError`
    waiting to happen). **Verified: 6/6** clean (no stale preference
    leaking into a later reply) across fresh sessions, up from 3/5 before
    this fix.
  - **Attribute refinement in long/compounding conversations - widened
    the deterministic trigger to combine TWO independent signals with OR**
    (`claim.durable_fact_candidates` from `interpret()`, and
    `delta.priorities_add`/`nice_to_have_add` from the SEPARATE
    `understand()` call) instead of trusting either alone - each comes
    from a different model call with different prompts, so their
    failures are not perfectly correlated. **Verified: 4/8** in a longer,
    5-turn conversation (show shoes → show socks → which is cheaper →
    what else → "something lightweight") - a real improvement over the
    single-signal version, but confirms the user's own concern: this
    does NOT hold at the same reliability a short, isolated exchange
    showed. This is the one gap of the three NOT yet independently
    stable - the underlying cause is that BOTH contributing model calls
    can still fail on the same turn simultaneously, and no further
    architectural combination of the two existing signals closes that
    remaining joint-failure case. Left open and disclosed rather than
    patched further with a third guess.
  **Regression after this phase:** `auditroutes.py` clean; `fuzz.py`
  clean (1200 assertions, both merchants, every invariant held); 25/25
  offline phase tests clean (this run - the small number of previously-
  flaky live-model checks in this suite did not fail this particular
  run, consistent with their own already-documented variance, not a
  claim that they are now deterministic). Long mixed conversations
  (topic switching, historical superlative recall, cross-topic ordinal
  add, natural cancellation, attribute refinement, preference
  cancellation, budget-constrained recommendation, compound comparison,
  explanation, and cart fulfillment via a real variant question) run
  cleanly end-to-end on BOTH Northfield and Kettle & Bloom, with no
  security-invariant, tenant-isolation, or cart-mutation regression.
  **Acceptance bar: NOT fully met at time of writing** - see Phase 14
  immediately below, which root-causes attribute_refinement's remaining
  instability to two SEPARATE real bugs (not the two-signal design
  itself) and fixes both, with an honest, corrected reliability figure
  from the representative test harness.
- **Phase 14 - attribute_refinement instability, real root cause found
  (2026-09).** Per explicit direction: traced the complete path rather
  than layering a third signal. The two-signal design
  (`interpret().durable_fact_candidates` OR
  `understand().priorities_add`/`nice_to_have_add`) was ITSELF retired -
  both are the model re-judging the identical semantic question in a
  different field of the SAME kind of call, so their failures are
  correlated on hard turns, not independent, which is why combining them
  with OR never got past ~50%.
  - **Architectural replacement:** the routing decision is no longer a
    pre-emptive guess at all. `node_discovery` now tries a real, literal
    search FIRST, exactly as before; only when that search's own
    deterministic result (`evidence.no_match_reason`, not a guessed
    "products is empty" check - a real bug in the first version of this
    fix, since `products` is the RAW candidate list and can be non-empty
    even when nothing passed relevance verification) confirms nothing
    confident was found does it check whether `new_state.priorities`/
    `nice_to_have` (already merged by `apply_delta` from THIS turn) or
    `verified_interpretation.durable_fact_candidates` (already computed
    this same turn by `node_route_capability`, fed here as DATA into the
    deterministic re-rank via `mission_ranking.rank_and_filter`'s own
    `memory_facts` precedence - never as a second router) has real
    content to re-rank an established topic by. The OLD pre-emptive
    `ANSWER_QUESTION` routing override in `node_route_capability` (both
    the `claim is not None` and `claim is None` branches) is deleted
    outright, per the explicit instruction not to add a competing
    classifier - there is now exactly one place this decision is made,
    triggered by one real fact (a literal search failed), not a guess
    before trying.
  - **Two SEPARATE, more fundamental bugs found and fixed while tracing
    why even this correct architecture stayed unreliable:**
    `node_finalize` was unconditionally overwriting BOTH a topic's real
    `product_ids` (Focus Stack) AND `last_rendered` with an EMPTY list
    whenever a turn's own search found nothing verified - so a single
    failed "What else do you have?" silently erased the real context
    `has_real_context`'s entry gate and the new re-rank fallback both
    depend on, for the REST of that conversation, regardless of how many
    turns later a real preference was stated. Both fixed to only update
    when the turn actually found something real, never overwriting real
    prior state with nothing just because this turn's own search failed.
  - **Verified, and the discrepancy between two measurements is itself
    reported honestly:** a direct, scripted call sequence with no real
    conversation `history` passed (an unrepresentative simplification of
    this session's own earlier debugging, not the production path)
    measured 4/5-5/5 after these fixes. The ACTUAL representative
    measurement - the real `/api/v2/chat` HTTP path, with real persisted
    conversation history passed into every model call exactly as
    production does - measured **1/8** for the full targeted 5-turn
    conversation (shoes → socks → "which is cheaper" → "what else" →
    "something lightweight") after the SAME fixes. This gap between the
    two measurements is itself evidence for the user's own original
    concern: real conversation history compounding makes the underlying
    model classification LESS reliable as more turns accumulate, not
    more - a characteristic the simplified, no-history script could not
    surface at all. The relationship-after-correction and cancellation-
    clears-preference fixes from the prior phase were independently
    re-confirmed unaffected by this phase's changes (4/5 and 5/5
    respectively, measured via the same real HTTP path).
  **Regression after this phase:** `auditroutes.py` clean; `fuzz.py`
  clean (1200 assertions, every invariant held); 24/25 offline phase
  tests clean (the one non-clean is the same already-documented
  `test_phase9_tradeoff` "persisted priority" extraction flake, unrelated
  to this phase).
  **Superseded by Phase 15 immediately below** - the "1/8, live-model
  classification degradation" conclusion above was wrong. A forensic
  trace of the actual failing HTTP turns found a real, fixable state-
  resolution bug instead; see Phase 15 for the corrected root cause,
  fix, and measurement.
- **Phase 15 - attribute_refinement: the real root cause was state
  resolution, not classification (2026-09).** Forensic, read-only
  instrumentation (added, used, then fully removed - no logic changed by
  it) traced every layer of the exact failing turn ("Show me some running
  shoes" -> "I'm looking for something lightweight") over the real HTTP
  path with real accumulated history. Finding: `question_type` was
  correctly resolved as `attribute_refinement` on BOTH the blind backstop
  call and the fully-grounded `node_route_capability` call - classification
  was never the problem on this turn. The failure was entirely downstream,
  in `recent_relevant_product_ids` (`engine/shopper_v2/state.py`): a plain
  "show me X" turn never pushes a real Focus Stack topic (`delta.goal` is
  unreliable for that phrasing, a known gap), so it only ever wrote the
  legacy `active_products`/`recent_product_groups`/`last_rendered` fields
  - all real and correct. The FOLLOWING preference turn DOES get a real
  `delta.goal`, so `topic_transition.py` correctly pushes a brand-new
  topic, shape-only, `product_ids=[]` ("content attached later by whichever
  node discovers it" - by design). `recent_relevant_product_ids` treated
  `state.focus_stack` as the SOLE source the instant it was non-empty, with
  no fallback to the legacy fields that still honestly held the real,
  already-shown ids - so `run_tradeoff_turn` found nothing to re-rank and
  produced the honest-but-wrong "nothing currently in view" decline. Same
  *class* of bug as Phase 14's two `node_finalize` fixes (a structural
  field silently shadowing good data), a third, independent instance.
  **Fix:** `recent_relevant_product_ids` now falls back to
  `active_products`, then `last_rendered.product_ids`, ONLY when the Focus
  Stack branch (active topic, then paused topics) resolves to fewer than
  `minimum` real ids - never overriding a real, non-empty Focus Stack
  result. No topic is ever seeded/copied with ids to route around this -
  topic ownership (Focus Stack) and product discovery stay separate, per
  explicit instruction. No interpretation/prompts/`QuestionType`/classifier
  code touched.
  **Regression:** new `tests/shopper_v2/test_phase18_state_resolution.py`
  (model-free) covers all four required shapes - Focus Stack with real ids
  wins unchanged; Focus Stack empty falls back to `active_products`; falls
  back further to `last_rendered` when that's also empty; stays honestly
  empty (never invents) when nothing real exists anywhere; a real paused
  topic is still used before any fallback - plus the exact live-reproduced
  two-turn sequence via `run_tradeoff_turn`. All 7 checks pass. Full
  offline suite 35/35 clean (including the previously-flaky
  `test_phase9_tradeoff`, clean now that it ran against live servers).
  `auditroutes.py` clean. `fuzz.py` clean (1200 assertions, both merchants,
  every invariant held).
  **Live HTTP measurement (real path, real accumulated history,
  Northfield):** the exact turn that measured 1/8 before this fix now
  measures **8/8** across repeated runs of the same 8-turn conversation -
  every run produces a real, grounded re-rank ("Featherline Speed Running
  Shoe now fits best - matches 'lightweight'"), never the "nothing in
  view" decline. Spot-checked on Kettle & Bloom too (3/3 runs, no
  recurrence of the failure shape; one run's turn 1 genuinely had nothing
  left to re-rank against and said so honestly, which is correct
  behavior, not a regression).
  **Acceptance bar for attribute_refinement: MET for the specific
  live-reproduced instability this and Phase 14 traced.** Later turns in
  the same 8-turn conversation still show independent, pre-existing
  `question_type` classification variance (e.g. "relationship" vs
  "attribute_refinement" on turn 4 across runs) - a real, separate,
  live-model variance source, not touched by this fix and not yet
  measured/scoped on its own. Do not read this phase as "all
  attribute_refinement variance is gone" - only the specific
  state-resolution false-negative is.
- **Phase 16 - foundation fixes from a forensic 9-failure long-conversation
  audit (2026-09), IN PROGRESS - NOT verified end-to-end, do not mark
  complete.** A forensic, read-only-instrumented trace of a real 13-turn
  mixed HTTP conversation found the two deepest, most common root causes
  behind most of 9 named failures: (1) Focus Stack topics still never get
  created for a plain "show me X" turn (`delta.goal` unreliable for that
  phrasing - a long-known, previously-undressed gap), forcing every later
  capability onto a bounded, unlabeled legacy-ledger reconstruction; (2)
  `node_topic_transition` commits its stack-shape decision BEFORE the
  real, grounded interpretation/verification call runs, so it only ever
  guesses from raw-message label overlap, with nothing correcting it once
  the later, better-informed `verify()` call disagrees. Fixed, in
  dependency order:
  - **Foundation:** `node_finalize` now pushes a real topic for an
    ordinary discovery turn with no active topic at all (previously: none
    ever created). New `_semantic_topic_label()` builds topic labels from
    real, already-verified product titles (majority-shared distinctive
    words, e.g. "Running Shoe Trail") - NEVER the shopper's raw sentence,
    closing the gap where `run_alternatives` was reusing a raw sentence
    as a literal catalog search query and returning unrelated products.
    New `_reconcile_authoritative_resume()` in `node_route_capability`:
    when this turn's real `verify()` call resolves `context_action ==
    "resume"` against a REAL (non-legacy) topic that `topic_transition`'s
    earlier blind guess did NOT already activate, re-points the Focus
    Stack to the authoritative target and hands `node_finalize` the
    CORRECTED `topic_decision` - never a topic push/repair guessed at
    without a real reference to resolve. Bug found and fixed during this
    same pass: the first version of `_semantic_topic_label` restricted
    shared words to those tied for the single highest count, collapsing
    a real, distinctive multi-word category ("Running Shoe") down to
    whichever single generic noun happened to be in literally every
    title ("Shoe" alone) - fixed to accept any word shared by 2+ titles,
    ordered by share count.
  - **Preference write path:** `understanding.py::understand()` now
    promotes a same-turn durable-fact candidate (already reliably
    extracted by the SAME call/backstop that already existed - no new
    classifier) into `delta.nice_to_have_add` whenever the model's own
    structured `priorities_add`/`nice_to_have_add`/etc. all came back
    empty that turn - never overriding a real, more specific statement.
    Closes the gap where "I'm looking for something lightweight" was
    recognized as a durable fact but never became trackable, cancellable
    ranking state. **Known, explicitly out-of-scope limitation:** `Topic`
    already declares its own per-topic `priorities`/`nice_to_have`/etc.
    fields (docstring: "scoped to THIS topic alone"), but they are dead
    code - never written or read anywhere; the operative fields are
    `ShoppingStateV2`'s own flat, SESSION-wide ones. A preference stated
    under one topic still affects ranking/cancellation for every other
    topic in the same session. Not fixed this pass - a real, separate,
    larger data-model change (every `rank_and_filter` call site reads the
    flat fields) - flagged here rather than silently left undocumented.
  - **Fulfillment lifecycle:** `fulfillment.py::continue_fulfillment`'s
    `conversational_repair == "cancellation"` branch now clears the WHOLE
    remaining pending queue (previously: marked only the current item
    skipped and kept asking about every other queued item one at a
    time) - `looks_like_skip_request`'s own narrow word list remains the
    right place for "skip just this one." New
    `_invalidate_incompatible_fulfillment()` clears any still-pending
    fulfillment item whose product isn't part of the topic becoming
    active, wired into every real topic-switch commit site (`resume`/
    `push` in `node_topic_transition`, and the new authoritative-resume
    reconciliation) - never touches an already-`"added"`/`"skipped"`
    item.
  - **"What is my preference now?":** new closed-class deterministic
    backstop (`capability.py::looks_like_preference_status_request`,
    same discipline as the existing `CART_INTELLIGENCE` word-list
    backstop - never a model call) routes to a new `_preference_status_
    reply()` that echoes `new_state`'s own real fields plainly, honest
    when empty. Confirmed working live in this session's own testing.
  **Regression so far:** offline suite 32/35 (`auditroutes.py` clean;
  `fuzz.py` clean, 1200 assertions, every invariant held). The 3 offline
  failures (`test_phase12_discovery_vs_cart_routing`,
  `test_phase15_interpretation`, `test_phase9_tradeoff`) are the SAME
  Groq free-tier throttling this file's own "Running It" section already
  documents (~4 model turns/minute) - directly confirmed, not assumed: a
  standalone repro of the exact failing call showed a genuine HTTP 429
  from the provider, logged as "interpretation unavailable, degrading:
  rate limited by the model provider." `test_phase9_tradeoff`'s one
  failure is the same pre-existing simulated-fetch-failure flake already
  documented elsewhere in this file, unrelated to this phase.
  **NOT YET VERIFIED: the full 13-turn live conversation and the
  pending-question correction flow ("Which is cheaper?" -> "Actually I
  meant the shoes") this phase was explicitly asked to confirm last.**
  Every live attempt this session - including a version paced at 20-25
  seconds between turns - still hit sustained rate-limiting badly enough
  that `interpret_turn()`'s own call came back `None` (confirmed via
  direct instrumentation, not inferred) on the exact turns that needed to
  be checked. The one clean, live-confirmed win this session: "What is my
  preference now?" correctly answered "You haven't told me any
  preferences yet this conversation." instead of a literal catalog
  search. Everything else in this phase is implemented and either unit-
  verified offline (the label helper, directly, against real catalog
  titles) or reasoned through against the actual, current code - NOT
  live-conversation-verified. Do not mark this phase, Rev. 2, or any of
  the 9 originally-reported failures Completed. Next session should retry
  the full 13-turn live HTTP conversation (both merchants) once the
  provider's rate limit window is clear, focusing first on the pending-
  question correction flow and the whole-conversation "no stale topic/
  preference/fulfillment state leaks across turns" bar this phase was
  explicitly asked to clear.
- **Phase 17 - `interpret_conversation` tool-schema nullability defect,
  found and fixed (2026-09) - schema-only, no architecture/logic change.**
  While checking whether Phase 16's rate-limit blocker had cleared, a
  SECOND, distinct, real root cause was found: a direct provider call
  with the exact live interpretation prompt/tool schema returned HTTP
  400, not 429 - `"Tool call validation failed: ... /reference_attribute:
  expected string, but got null"`. `reference_attribute` (and every other
  OPTIONAL field in the same tool - `context_target_label`,
  `new_topic_label`, `reference_scope`/`kind`/`positions`/`direction`/
  `names`, `question_type`, `secondary_reference` and its own inner
  fields, `cart_operation`, `durable_fact_candidates` items'
  `confidence`, `conversational_repair`, `clarify_if_uncertain`) was
  declared as a bare `"type": "string"`/`"array"`/`"object"`/`"boolean"`
  with no `null` option, even though NONE of them are in the tool's
  `required` list and `ReasoningService.interpret()`'s own parsing
  ALREADY treats a non-conforming/`null` value as the field's documented
  `None`/empty default for every one of them (verified by reading the
  parsing code directly, field by field, before touching anything) - the
  SCHEMA was strict was inconsistent with the CONTRACT its own service
  code always assumed. Groq's structured-output validator enforces the
  declared type against whatever value is present even for an optional
  field, so one `null` on any of these rejected the WHOLE tool call, not
  just that field - and `LLMClient.complete()` reports this as the same
  generic `LLMUnavailable` a genuine 429 does, making a real code defect
  indistinguishable from rate-limiting at every call site.
  **Fix (`engine/reasoning/prompts.py::interpretation_tool`):** every
  field NOT in `required` (`capability`/`context_action`/`rationale`
  stay strictly typed, unchanged, deliberately) now declares
  `"type": [<original type>, "null"]`, and every enum alongside a null
  type also lists `null` as a valid enum member (required for JSON
  Schema to accept a null value against an `enum` constraint). No
  question_type/capability/architecture semantics changed - purely the
  JSON Schema's own type declarations brought into line with the
  parsing code's own, already-existing tolerance.
  **Verification:**
  - New `tests/shopper_v2/test_phase19_interpretation_schema_nullability.py`
    (model-free, a fake `LLMClient` returning the exact null-heavy shape
    the live provider produced) - 34/34 checks: every optional field's
    schema is confirmed nullable, every required field is confirmed to
    stay strict, and `ReasoningService.interpret()` parses a null-heavy
    tool call (top-level AND `secondary_reference`-nested) into a real,
    non-`None` `InterpretationResult` with correct honest defaults.
  - Direct provider re-test of the EXACT call that previously 400'd: now
    returns 200, with the model again choosing to emit `null` for most
    optional fields (`cart_operation`, `context_target_label`,
    `reference_attribute`, `reference_direction`, `reference_kind`,
    `reference_names`, `reference_positions`, `reference_scope`,
    `secondary_reference`) - confirms the sweep across ALL optional
    fields was necessary, not just `reference_attribute` alone.
  - A genuine 429 immediately afterward, through the real
    `ReasoningService.interpret()` path, still surfaces distinctly as
    "rate limited by the model provider" (vs. the schema failure's own
    "provider returned 400..." message) - the two ARE distinguishable by
    message text today; no error-handling code changed.
  - Full offline suite 33/36 clean (34 incl. new test), same 3 failures
    as before this fix (`test_phase12_discovery_vs_cart_routing`,
    `test_phase15_interpretation` - both genuine 429s, confirmed live
    during this same check; `test_phase9_tradeoff` - the same
    pre-existing simulated-fetch-failure flake, unrelated).
    `auditroutes.py` clean. `fuzz.py` clean (1200 assertions, every
    invariant held).
  **The Groq rate limit had NOT cleared at last check** - per explicit
  instruction, the 13-turn live acceptance conversation from Phase 16 was
  NOT re-attempted this pass. Do not mark this phase, Phase 16, Rev. 2,
  or any of the 9 originally-reported failures Completed. Next session:
  retry the 13-turn live HTTP conversation (both merchants) once the
  provider's rate limit window is genuinely clear - this fix should
  materially reduce (not necessarily eliminate) how often `interpret_
  turn()` degrades to `None` during that run, since a whole class of
  false "unavailable" results is now closed.
  **Correction, discovered in Phase 18 immediately below: this entry's
  own "Groq rate limit" framing was wrong.** `.env` defaults
  `LLM_PROVIDER="openai"`, and every check in this phase used that
  default (no override) - every 429 here was OpenAI's, not Groq's. See
  Phase 18 for the corrected diagnosis.
- **Phase 18 - provider mislabel corrected; live verification run on Groq
  (temporary, config-path-only substitution); a second real bug found
  and fixed (2026-09). IN PROGRESS - NOT fully verified, do not mark
  Completed.**
  - **Provider correction:** direct checks against both providers showed
    OpenAI returns `429 insufficient_quota` / `"You have no credits
    remaining"` - a permanent billing exhaustion, not a per-minute
    throttle; no wait fixes it. Groq, checked at the same moment, showed
    999/1000 requests and ~7500/8000 tokens remaining. Every "Groq rate
    limiting" claim in Phase 16/17's entries above was this session's own
    mislabeling of OpenAI's quota exhaustion - `ReasoningService.from_env()`
    was never overridden to Groq in any of those checks. Corrected here,
    not silently fixed in place, so the record stays honest.
  - **Verification substitution:** per explicit instruction, temporarily
    ran the engine with `LLM_PROVIDER=groq` set ONLY in that one
    subprocess's own environment - `.env` was never edited, no key was
    hardcoded, nothing committed. Stopping that process fully reverts to
    the normal (OpenAI-default) configuration; confirmed `.env` has zero
    diff (`git status --short .env` empty).
  - **A second real bug found and fixed, blocking, found only because a
    live call finally succeeded:** the very first real turn on Groq
    crashed the whole graph - `ValueError: push requires new_topic_id/
    kind/label` from `topic_transition.py::apply_transition`, because
    Phase 16's own grounding fix (topic-transition-time push now starts
    with label `""`, backfilled later by `node_finalize` once real
    products are known) collided with this pre-existing guard's `not
    new_topic_label` check, which treats an empty string the same as a
    missing one. Fixed: the guard now checks `new_topic_label is None`
    specifically - a caller's deliberate `""` is a real, valid "no label
    yet" state (already used elsewhere, e.g. the legacy-bridge synthetic
    topics); only a genuinely omitted label is still an error. This had
    never been caught before because no live call had succeeded since
    Phase 16's own fix was written - the OpenAI quota exhaustion and,
    before that, the schema defect had masked it completely.
  - **Live results on Groq (Northfield), repeated:** the foundation fix
    is directly confirmed working - "Show me some running shoes" (no
    prior context) created a REAL Focus Stack topic labeled "Running Shoe
    Trail" (product-grounded, never the raw sentence). "Which is
    cheaper?" resolved CORRECTLY on 2 of 3 attempts across two separate
    live runs - "The Performance Socks Three Pack is cheaper than the
    Running Shoe Trail - price 499.00 INR compared with 1999.00 INR" -
    using the real semantic label, correctly distinguishing the two real
    topics. "What is my preference now?" answered correctly and
    deterministically both times ("You haven't told me any preferences
    yet") - confirmed provider-independent, as designed. The remaining
    failures in these same runs (turn 2 failing on one attempt, and every
    turn from roughly turn 4 onward failing in an unpaced run) were
    traced directly to genuine, real Groq 429s - confirmed by a
    standalone repeat of the exact same call, where 2 of 3 rapid
    consecutive calls succeeded and the 3rd hit a live 429 - a real,
    session-token-budget characteristic (Groq's free tier is ~8000
    tokens/minute; this turn's own interpretation prompt is large enough
    that 2-3 calls in a turn, across several turns, exhausts it even
    without inter-turn pacing), not a code defect.
  - **Still not cleanly resolved, even where the model call succeeded:**
    "Add the socks to my cart" no longer silently resolves to the wrong
    entity (the original, worst failure) - it now names the RIGHT
    candidate ("did you mean Performance Socks Three Pack?") - but still
    doesn't auto-resolve a confident single match to a direct add,
    reproduced consistently across 3 separate live runs. This is a real,
    honest degrade (never silently wrong) but not the ideal "just adds
    it" outcome asked for - not yet root-caused to a specific confidence-
    threshold location, flagged for the next session rather than guessed
    at.
  - **Kettle & Bloom:** the preference-status backstop confirmed correct
    cross-merchant. A pre-existing, unrelated-to-this-session catalog/
    relevance quirk was observed - "Show me some coffee" (the bare
    category word) returned "I couldn't find anything matching that
    search right now" on Kettle's GraphQL catalog, while more specific
    phrasing found real products - not investigated further this pass,
    out of scope for this verification session.
  - **Regression:** `auditroutes.py` clean. `fuzz.py` clean (1200
    assertions, every invariant held). Full offline suite 33/36 clean,
    same 3 pre-existing failures as Phase 17 (now confirmed to be
    OpenAI's exhausted quota for two of them, plus the same unrelated
    `test_phase9_tradeoff` flake) - no new regression from the
    `topic_transition.py` fix.
  **Acceptance bar: NOT met.** The foundation fix and the preference-
  status backstop are now genuinely live-confirmed on real HTTP with
  persisted history, on both merchants. The pending-question correction
  flow ("Which is cheaper?" -> "Actually I meant the shoes") was NOT
  exercised in a genuine "wrong-then-corrected" shape in any live run
  this pass - the first resolution succeeded cleanly every time the
  model call itself succeeded, so the correction path's live behavior
  remains unproven, not disproven. The cart-add entity-resolution
  confidence gap and the full, cleanly-paced 13-turn run (both merchants,
  no dropped turns) remain open for the next session. Do not mark this
  phase, Phase 16, Rev. 2, or any of the 9 originally-reported failures
  Completed.
- **Focus Stack architecture (2026-09) — in progress, not yet fully
  verified end-to-end.** `engine/shopper_v2/` gained a new authoritative
  conversational context model (`state.py::Topic`/`focus_stack`,
  `topic_transition.py`'s 5-rule CONTINUE/PUSH/RESUME/ambiguous-mission
  procedure, a unified `reference_resolution.py::resolve_structured_reference`,
  a rewired routing precedence - fulfillment → explicit-cart-check →
  compare/compatibility priority-over-mission → topic-transition-gated
  mission → dispatch), `fulfillment.py` hardening (explicit "skipped"
  status, interruptions now PRESERVE the queue instead of deleting it, a
  shared real-cart precondition in `start_fulfillment` used by every
  caller), and mission multi-add redirected from `basket_proposal.py`'s
  pending-confirm flow to `fulfillment.py`'s direct sequential flow.
  Automated suites (`test_phase7_missions.py`, `test_phase8_basket_proposal.py`,
  `test_phase9_topic_transition.py`, `test_phase10_fulfillment_hardening.py`,
  `test_phase11_concurrency.py`, `test_query_intent_consolidation.py`) all
  pass; `healthcheck.py`/`fuzz.py`/`auditroutes.py` pass (2 pre-existing,
  unrelated failures - see below); a 15-turn mixed live conversation
  against Northfield exercised topic switching, interruption/resume,
  mission creation/interruption/resume, multi-add, variant selection,
  skip, and cart-status, with two real bugs found and fixed live
  (mission-tracked single-add being downgraded to the pending-confirm
  flow; a named comparison silently substituting the wrong second product
  when only one name was in the current small candidate set - both fixed
  at the architecture level, not phrase-patched).
  A third bug was found and fixed the same way: `reference_resolution.py`'s
  `resolve_ordinal_positions`/`looks_like_group_or_ordinal_reference`
  matched bare "first" as a positional ordinal regardless of grammatical
  role, so "wait, first tell me what socks you have" (a topic switch using
  "first" as a sequencing adverb) false-positived into `compare.py`'s
  dead-end reply. Fixed structurally (`_is_positional_usage` requires a
  determiner/selection-verb/referent-noun/ordinal-list context around
  "first"/"last" specifically, the only two ordinal words that double as
  ordinary adverbs), not with a phrase exclusion - verified against the
  original bug, the pre-existing "add first and second"/typo cases (my
  first attempt at the fix broke these; corrected), and the full
  regression suite. Real browser verification against Northfield (15-turn
  mixed conversation) now passes, including this fix.
  **Kettle & Bloom parity verification found new, more severe, real bugs
  not previously known** (2026-09, live-reproduced via an 11-turn browser
  conversation): (1) `decide_transition`'s resume rule matched the wrong
  paused topic ("going back to the coffee beans" resumed "espresso
  equipment" instead) - a real label-matching gap against this catalog's
  wording; (2) a mission item with zero live-search candidates
  ("beans" - no Kettle product title contains that word) stayed
  candidate-less all conversation, and unrelated later messages ("get me a
  coffee mug", "add the beans and the grinder") were then wrongly routed
  into the mission's ambiguous-item clarification instead of either a live
  search or resolving the unambiguous items; (3) a plain merchant-policy
  question ("what's your shipping policy") was misrouted into product
  search and failed, instead of reaching the Q&A path Northfield's
  equivalent warranty-policy turn correctly reaches; (4) a skip/cancel
  ("actually cancel that last one") was misread as a historical-reference
  display instead of a skip. None of these were fixed yet - they need
  their own root-cause pass (likely `missions.py`'s candidate-matching
  when a mission item has zero candidates, plus `decide_transition`'s
  label-matching, plus why cross-platform policy-Q&A routing differs) and
  are the current blocker before Shopper V2's Focus Stack work can be
  called done. **Still not done after that:** a full property/sequence
  test matrix beyond the two long conversations run so far, and a written
  call-count-per-turn test (the ~1-3 calls/turn finding is otherwise only
  measured ad hoc).
  **Two more real bugs found and fixed the same day (2026-09-25), both
  live-reproduced with concrete evidence, not assumed:**
  (a) A user-reported "sometimes works, sometimes doesn't" historical
  reference ("what is the cheapest shoe you showed me earlier" after
  switching to a different topic) was root-caused, not patched: measured
  0/8 Focus Stack pushes on a plain first-ever "show me running shoes"
  turn (`decide_transition`'s PUSH rule gates on `delta.goal`, a Stage-1
  LLM extraction that is simply not populated for ordinary browse
  phrasing). When a topic DID get pushed for turn 1 but the LLM again
  didn't extract `delta.goal` on turn 2 ("show me socks"), `decide_
  transition` returned "continue" and `node_finalize` silently overwrote
  the "running shoes"-labeled topic's own `product_ids` with the socks
  ids - corrupting the one topic that later needed to be found paused.
  Fixed in `node_finalize` (`graph.py`) with a deterministic, self-healing
  guard: before attaching a "continue" turn's verified products to the
  active topic, compare the new products' own titles against the topic's
  label using the same `_distinctive_title_words` overlap check
  `topic_transition.py` already trusts for resume-matching; a genuine
  mismatch retroactively becomes a push (pausing the stale topic intact)
  instead of overwriting it. Verified with 40/40 clean live reruns after
  the fix (was failing on a real, reproduced fraction of runs before).
  (b) A user-reported "multi-item add wrongly goes through an approval
  step instead of the direct size-then-add flow" was root-caused to TWO
  compounding routing gaps, both fixed: first, `node_route_capability`'s
  deterministic COMPARE backstop fires on any closed-class ordinal/group
  word ("both"), including inside an explicit cart command ("add both to
  my cart") - it doesn't check whether the message also names an explicit
  cart verb, so it claims `Capability.COMPARE` before `resolve_cart_target`
  (the one architecturally-authoritative cart-intent check) gets a turn,
  and if that check's own separate LLM call then declines (its own
  variance), capability is already irreversibly wrong, producing a
  dead-end compare reply to an unambiguous add command. Second,
  `_plausible_cart_signal`'s own fail-safe (skip the real cart check only
  when Stage 1 classified the message "conversational") was itself
  sometimes wrong for this same exact phrasing, skipping the cart check
  entirely and landing in `node_conversational`'s own rhetorical "would
  you like me to add both? " reply - functionally the same "asks instead
  of acting" complaint with no proposal object behind it at all. Both
  fixed with one new, reused, closed-class check
  (`capability.py::looks_like_explicit_cart_command` - "add"/"buy"/
  "purchase", the same closed-linguistic-class discipline as this file's
  existing comparison/historical-reference word lists): it withholds the
  COMPARE backstop and forces the real cart check to run regardless of
  Stage 1's classification. Verified live: "add both to my cart" after a
  single-topic discovery turn now reaches the correct sequential
  size-then-add fulfillment flow in 10/12 reruns (was 0/6 compare-hijacked
  or conversational-stuck before this fix); the residual 2/12 hit a
  distinct, far more benign fallback (an honest "couldn't find anything
  matching" from plain discovery, when `resolve_cart_target`'s own LLM
  call declines on its own remaining variance) - not an approval trap,
  but not yet eliminated either. Full regression suite re-run clean after
  both fixes: phases 8-11, query-intent-consolidation (35/35), `fuzz.py`
  (20/20 sequences, 1200 assertions, both merchants, every invariant
  held), `auditroutes.py`.
  **(c) Same day, user reported the remaining 10/12 "correct" case above
  still showed a "Suggestions - Accept / No thanks" proposal UI** - because
  the multi-item fix only ever applied when `resolve_cart_target` resolved
  2+ product ids; a SINGLE resolved item (the far more common case, e.g.
  "add the Trailblazer") still fell through to `run_cart_change_proposal_turn`
  below it in `node_cart_action` - the older pending-`CartProposal`-with-
  `/api/chat/proposals/{id}/accept` flow, never fulfillment.py's direct
  size-tap-then-add sequence. Per the user's explicit, repeated
  instruction, this was not a bug to patch narrowly but a UX pattern to
  remove entirely: **every explicit "add" - one item or several - now
  goes through the exact same `fulfillment.start_fulfillment` sequence**
  (`node_cart_action`, `graph.py`) - real product shown, a real size/
  variant question only when the product actually needs one, the
  shopper's own tap adds it directly (`engine.execution.trusted_action.
  run_trusted_action`, still the full Decision->Risk->Execution pipeline,
  never a bypass), then immediately the next item's own question, until
  every item is settled. `run_cart_change_proposal_turn`/`cart_proposal.py`
  is untouched and still used for remove/set-quantity/clear (not part of
  this complaint) and remains directly unit-tested
  (`test_phase4_cart_proposal.py`, `test_phase6_cart_change_operations.py`
  - both still pass). The final reply was also upgraded per the user's
  explicit request: `fulfillment.py` now captures each item's real price
  when it fetches the product and reports a consolidated "As per your
  order, I've added: X - price, Y - price. Total: price." (falls back to
  naming items with no total only if a price is ever missing or currencies
  genuinely differ - never invented). Verified live end-to-end (a real
  cart created first, then two sequential size taps): "As per your order,
  I've added: Trailblazer Running Shoe - 4299.00 INR, Starter Road Running
  Shoe - 1999.00 INR. Total: 6298.00 INR." across 3/3 reruns. **A separate,
  narrower gap surfaced by this same testing and NOT yet fixed:** naming
  two items that live in two DIFFERENT Focus Stack topics (one active, one
  paused - e.g. "add both the shoes and the socks" after switching topics)
  can still resolve the wrong second product or fail to add, because
  `resolve_cart_target`'s own candidate set is scoped to the active
  topic's own shown products only, unlike `compare.py`'s already-fixed
  cross-topic historical-group lookup. Full regression suite (phases
  4/6/8/9/10/11/12, query-intent-consolidation, `fuzz.py`, `auditroutes.py`)
  re-run clean after this fix; one pre-existing test fixture
  (`test_phase10_fulfillment_hardening.py`'s own mock `_Product` stub)
  needed a `price` attribute added since it predated this field.
- **A turn-level concurrency lock was attempted and reverted - a real,
  live-reproduced near-miss.** Each individual `shopping_state.py::merge()`
  call is safely locked per (session_id, connection_id) and this is
  unchanged; a genuinely concurrent SEPARATE turn for the same session
  (two tabs, a double-tap, a network retry) can still, in principle, read
  `focus_stack` (or `mission_items`/`active_products`/`fulfillment_queue`
  - this risk predates Focus Stack and applies equally to all of them)
  once at turn-start and have its later write silently lost to another
  concurrent turn's write - confirmed via a direct test (10 concurrent
  turn-shaped writes to one session lost 9 without a wider lock). An
  attempted fix (wrapping the whole turn in a reentrant per-session lock)
  was tested live and caused a genuine server hang (30s timeout on a
  brand-new session's very first turn) - LangGraph's `ainvoke` appears to
  run node coroutines under `asyncio.current_task()` identities that
  differ from the task that entered the wrapping function, so a task-
  identity-based reentrant lock held across the whole turn deadlocked
  against its own inner `merge()` calls. Reverted immediately after live
  reproduction, verified the hang is gone. A correct fix needs a lock
  mechanism that doesn't depend on task identity (e.g., a `contextvars`-
  propagated marker, verified against LangGraph's actual task-per-node
  behavior before being trusted) - not attempted under this session's time
  pressure given a hang is a strictly worse failure than the narrower race
  it would close, and the real-world likelihood of two truly concurrent
  requests for one shopper's one session is low (not zero).
- **No ops-console consumer of Shopper V2 activity events**
  (`TRADE_OFF_APPLIED`, `PAYMENT_RECOVERY_OFFERED`, `ORDER_STATUS_CHECKED`,
  `MISSION_ITEM_TRACKED`, `BASKET_PROPOSED`) — merchant-console-scoped only.
- **Real-volume validation for V2 retrieval remains a code inspection, not
  an empirical test.** No 40,000-SKU synthetic catalogue has been seeded
  against the V2 pipeline end-to-end.
- **`priorities_add` (trade-off) extraction has a real, measured ~1/8 hit
  rate** for some phrasings on the primary understanding call. Accepted
  model variance, not a routing defect; a prompt-strengthening attempt made
  it worse and was reverted.
- **No gate runs the browser (Playwright) tests automatically.** They exist
  and pass but nothing wires them into CI or a pre-commit gate.
- **No published reliability number.** `eval.py` exists and is unfinished.
- **The assistant is not installable outside this codebase** — it is a
  React component in this storefront, not an embeddable script tag.
- **No Postgres, no hosting, no repo split.** SQLite by connection string,
  local only; the operations console shares a bundle with the shopper
  storefront, so the operator key must be typed rather than built in.
- **An intermittent SQLite dev-path read-after-write timing issue** was
  reproduced during the Merchant console functional audit but not
  resolved — rare, dev-environment only, not reproduced against the
  production-shaped single-writer path.
- **Two pre-existing `healthcheck.py` failures**, unrelated to Shopper V2:
  `chat.py` recovery-explain reply, and `repository.py`
  `checkout_conversion` counting. Confirmed genuinely intermittent, not a
  fixed shared-state bug: repeated same-code reruns during the Focus Stack
  work fluctuated between 2 and 8 failures (including `session/store.py`
  shared-memory-across-surfaces and `webhooks.py`'s ops-queue check, which
  each passed on some runs and failed on others) purely with elapsed
  wall-clock rest between runs, consistent with live-provider rate-limit/
  quota exhaustion from heavy same-session testing, not a deterministic
  regression. The 2 failures listed here are the ones that recur most
  consistently and were already open before today; not yet root-caused or
  fixed.
- **An ordinary Shopper V2 turn can cost up to 3 model calls** —
  `understand()` (Stage 1, every turn), then `intent_resolution.py`'s
  `resolve_cart_target` → `interpret_cart_reference` (runs on nearly every
  turn once anything has been shown or a cart exists, not only
  cart-related messages), then `respond()`'s `synthesize()` for any turn
  that doesn't produce a deterministic reply. Against the documented
  ~4-calls/minute Groq free-tier throttle, a single ordinary turn can by
  itself consume most of a minute's budget — a real, previously
  uncharacterized contributor to the throttling seen during live testing,
  not only an artifact of rapid manual/automated testing. No fix applied:
  the obvious fix (a deterministic keyword pre-filter to skip
  `interpret_cart_reference` when the message looks unrelated to the
  cart) was deliberately rejected — `intent_resolution.py`'s own docstring
  explains this module exists specifically because keyword-matching for
  cart-intent already failed historically in this codebase (~20
  point-fixes in `relevance.py`), for phrasings like "I'll take this" or
  "get me the Trailblazer" that a keyword list would miss; a skip-only
  filter still risks silently reintroducing that exact defect. The
  correct fix (merging 2-3 of these model calls into one) is a real
  architecture change, not a quick patch, and needs its own scoped design
  pass before being attempted.

---

## Not Yet Built

### Advanced Product Discovery — ~30% complete
A deterministic mission-aware shortlist/ranking building block
(`engine/execution/mission_ranking.py`) exists and is wired into V1's
`RECOMMEND_PRODUCTS`, but no general free-text discovery turn delivering the
roadmap outcome ("a product they would not have found by keyword, and why")
has been built on either pipeline.

### Personalized Recommendations — ~35% complete
Durable-fact personalization (a stated preference changing which product
ranks first on the next turn, including through mission-item turns) is now
implemented and live-verified end to end on V2 — see Completed.md. Remaining
gap: `ShopperEvent` interaction instrumentation (`engine/execution/events.py`)
logs impressions/cart-adds/purchases write-only; nothing reads it back into
ranking or synthesis, so a returning shopper is not yet shown anything that
visibly reflects past purchases/keeps/returns.

### Mission-Based Shopping — ~30% complete
`ShoppingState` re-ranks an already-fetched product list against a stated
mission (live-verified flipping the top-ranked product as priorities
change), but multi-item, cross-category basket building in one turn (e.g.
shoes + socks + hydration for a marathon) is unbuilt on V1. V2's mission-item
tracking and basket proposal flow cover part of this and had three
real-conversation bugs (named-item add reporting, real-cart duplicate
awareness, partial multi-name matching) fixed and verified — see
Completed.md — but the roadmap outcome itself (building a full cross-
category basket in one turn from a single mission statement) is still not a
complete feature.

### AI Store Diagnosis (Merchant) — 0% as a shipped feature
`business_diagnosis()` (deterministic, evidence-gated, correlation-only)
exists and is wired into the Copilot and Business Insights panel, but has
not cleared a `value-auditor` pass and has no attached "act on it" surface.

### Recovery Opportunity Radar, Catalog Intelligence, Inventory Intelligence — 0%
Not started. Real-volume conditions (40k-SKU catalogue) will need to be
exercised for Catalog/Inventory Intelligence specifically, per CLAUDE.md.

### Cross-Merchant Command Center, Integration Health Monitoring, Automatic Incident Detection — 0%
Not started.

---

## Next Work

1. Run `value-auditor` on the V1 `synthesize()` reply-before-data fix and on
   Return-Risk Prevention / AI Store Diagnosis (Merchant) — all three have
   implementation and live verification but no completed value-bar pass.
2. Close the Merchant Copilot's read-only-twin gap for catalogue/inventory/
   unmet-demand answers — attach an executable action reachable from the
   same panel, through the existing risk gate.
3. Build the same "act from the answer" capability for the Operations
   Copilot (approve/close/escalate reachable directly from an answer).
4. Extend the Stage 2A eval harness to exercise `pipeline.py` end-to-end
   (not just `evidence.build_evidence` in isolation) so the already-fixed
   retrieval findings are covered by the repeatable corpus, not only by
   live manual testing.
5. Seed a real synthetic catalogue at CV3's real client volume
   (~40,000 SKUs) and re-run the real-volume condition empirically for
   Shopper V2 retrieval and any Merchant Intelligence feature that reads
   "all" of a catalogue.
6. Wire a merchant/ops-console consumer for Shopper V2's activity events.
7. Wire the Playwright suites into an actual CI or pre-commit gate.
8. Select and spec (via `feature-spec`) the next Merchant Phase 1 item —
   Recovery Opportunity Radar, Catalog Intelligence, or Inventory
   Intelligence — before writing any code for it.
9. Decide and execute V1's actual end state: the intended final
   architecture is V2-only, and V1 (`engine/api/chat.py`, `/api/chat`) is
   no longer reachable from the shopper-facing storefront at all - it
   remains only as a backend route with its own test coverage
   (`healthcheck.py`/`fuzz.py`/`auditroutes.py`). Retiring it means
   migrating whatever those suites still uniquely check into V2's own
   test coverage first, then deleting the route - not yet started.

---

## Important Current Limitations

- **Semantic/product relevance** is deterministic-layer-driven; every
  measured V2 retrieval gap found so far traced to the deterministic/state
  layer, not to a genuine "shares no literal word" failure needing
  embeddings or reranking — this conclusion is explicitly still open at
  real catalogue scale.
- **Conversational naturalness and model extraction variance** are real and
  measured (e.g. the trade-off extraction hit-rate above); not treated as
  bugs to keyword-patch, per CLAUDE.md's standing rule against
  phrase-specific patches.
- **Real-volume validation** is structural/code-inspection for most
  recently-built features, not empirical, except where explicitly stated
  otherwise (the `_catalog_alerts`/`_scan_catalog` fix, which was verified
  against a real 150+120 product synthetic fixture).
- **No CI/browser verification gate** — Playwright and the three backend
  suites (`healthcheck.py`, `fuzz.py`, `auditroutes.py`) all require a
  human or agent to run them against the live services; nothing runs them
  automatically.
- **Production infrastructure** (Postgres, hosting, secrets management,
  repo split) has not been started; this is local SQLite/dev-only today.
