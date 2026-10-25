# Completed

Major capabilities that are implemented, integrated, tested, and — where
CLAUDE.md's value bar applies — cleared it. See PROGRESS.md for everything
partial, unverified, or unbuilt.

## Architecture

- **Platform-independent commerce interface.** Two demo merchants prove it:
  Northfield Running Co. (REST, port 8001, no payment recovery) and Kettle &
  Bloom Coffee (GraphQL, port 8002, supports recovery). The same declined
  card produces a recovery offer on Kettle and an escalation on Northfield,
  decided entirely by declared platform capability, not a code branch.
- **The four-stage pipeline** (reasoning proposes → decision ranks and
  filters by capability → an eleven-rule risk gate classifies
  automatic/needs-approval/blocked → execution carries it out through the
  adapter) is the authoritative path for every mutating action on both
  Shopper V1 and Shopper V2.
- **The payment ledger** (`db.idempotency`), keyed on cart id alone at
  creation, guarantees a SETTLED paid cart cannot be charged twice on a
  different card, a reissued cart id cannot inherit a stale paid status, and
  a paid cart cannot be mutated afterward — enforced on both the REST cart
  routes and the chat/tap execution path. It does NOT protect a charge whose
  result is lost to a timeout or 5xx: the row is released and a different
  card is charged again (reproduced; unresolved release blocker, see
  PROGRESS.md). Checkout and payment are not production-ready.
- **Cross-tenant and cross-shopper isolation**, enforced by an httpOnly
  visitor cookie and an ownership table (`db.owners`), checked on every
  cart/session/order route on both V1 and V2.

## Shopper AI

- **Shopper V2 is the only shopper routing the real storefront calls.**
  `/api/v2/chat` (`engine/shopper_v2/`) is the sole path
  `storefront/src/ChatWidget.tsx` invokes (as `/api/v2/chat/stream`, falling back to the plain route; see the streaming entry below) -
  there is no `assistantMode` state, build flag, or per-session control in
  the storefront that reaches V1 instead. V1's `/api/chat`
  (`engine/api/chat.py`) remains mounted at the API level only, exercised
  directly by `healthcheck.py`/`fuzz.py`/`auditroutes.py`, but is not a
  shopper-selectable fallback - the intended final architecture is
  V2-only, with V1 retained only for its own existing test coverage during
  the transition, not as a permanent parallel shopper architecture (see
  PROGRESS.md's Next Work for retiring it outright). V2 structurally fixes
  the reply-before-data defect: the model's pre-fetch reply is discarded
  before retrieval runs, so shopper-facing text can only be generated from
  real, already-fetched evidence, by construction.
- **V2 discovery, comparison, and compatibility** turns run against live,
  freshly-fetched product data on both demo merchants.
- **V2 cart actions**: cart proposals, shopper accept/reject, checkout
  continuity, payment recovery (capability-aware), cart status, and clear
  cart all route through the unmodified Decision Engine → Risk Gate →
  execution → adapter pipeline, constructing only
  `PROPOSE_CART_CHANGE`/`PREPARE_CHECKOUT`/`CHECK_ORDER_STATUS`/recovery
  candidates. A cart changes only on the shopper's own authorization: a tap
  or typed "yes" on a proposal, or an explicit typed add command
  (`add_command.py`) whose product and variant are unambiguous — both end in
  `accept_cart_proposal_core` (live stock/price re-check, atomic claim). An
  "it", "the coffee" (a word every product in view shares), "all of them" with
  a hidden topic behind it, or a missing size asks first; questions (with or
  without "?"), negations, deferrals, hypotheticals, quoted text and vague
  agreement never add. The model's pick is checked against the shopper's own
  words; pipeline tests fail if the guard is disabled. Verified model-free;
  the final guard was not re-run live (see PROGRESS.md).
  Verified model-free (`test_explicit_add_executes.py`) and live (gpt-4o-mini:
  named add executes, sized product asks, ambiguous/hedged messages add
  nothing).
- **Cross-session cart persistence**: signing out and back in restores the
  shopper's remembered cart instead of letting an empty guest cart replace
  it; ownership-checked, and a transient platform error never discards it
  (`test_cart_persistence_signin.py`, browser `cart-restore`).
- **Store-level discovery**: "What do you have?" answers from the merchant's
  declared departments (tappable choices, buyable-checked) on both demo
  merchants — live-verified; named-department browse and variant-grounded
  terms ("beans") included.
- **Multi-product discovery**: two or three products named in one message are
  each resolved and shown, with a plainly absent one reported rather than
  invented — including when the model routes the message to the compare
  answer (live-verified; `test_step25_named_multi_routed.py`). A name is
  reported missing only after a bounded catalogue search finds nothing, so a
  real product outside the 100-product vocabulary sample is never called
  missing; an unknown capitalised name ("Zephyr Cloud Boot") is reported even
  when it shares words with real products (model-free tests with negative
  controls; these two later changes are not live-verified).
- **Basket-level budget for guided missions**: "I want running shoes, socks and energy gel, total budget 6000" (also "...under 5000",
  "...for 8000 rupees in all") starts a mission with ONE shared total instead of a bogus stage; every stage prompt says what is left
  (recomputed from the real cart), whether the whole list fits (fits / tight / over / unknown - "fits" only when every open stage is
  priced in one currency and the cart was readable), offers up to three cheaper in-stock options from the stage's own candidates as
  cards (live-checked; adding one is the existing typed or tapped add, so a sized shoe still asks its size first), and avoids offering
  anything already in the cart or chosen for another stage. Persisted measure: `basket_budget_set` / `basket_budget_outcome` events and
  `events.basket_budget_outcomes`. Verified model-free (11 scripts, real Northfield and Kettle through the graph, 40,000-product
  structural check, platform-down and tenant cases) with independent invariant and value reviews; limits are listed in PROGRESS.md.
- **Cross-stage compatibility and complete-the-set for guided missions**: once a product is really in the cart, the mission names which
  on-screen options the merchant LISTS as going with it, notes declared pairs between options of two open stages, and announces
  merchant-declared add-ons as optional extras (cards, at most two, once after an add and once in the summary) with their live price and
  their effect on the shared budget. Evidence is only the merchant's declared `compatible_with`; nothing is inferred from category or title,
  "incompatible" is never shown, and a platform with no declarations gets one honest sentence. Read-only: adding an extra is the existing
  typed or tapped add through `fulfillment.start_fulfillment`. Verified model-free (11 scripts incl. real Northfield and Kettle through the
  graph, 40,000-product structural check, platform-down, tenant and zero-data cases), in a browser (5-test spec, no payment requests), with
  an independent invariant review; persisted measure `events.complete_set_outcomes`. Limits and the unverified real-client data question are
  in PROGRESS.md.
- **V2 missions and basket proposals**: a stated goal drives multi-item
  shortlisting with a real reason per item; a discovery-vs-cart-action
  routing bug (a plain restatement misclassified as cart intent) is fixed
  structurally, not by a phrase list.
- **Three mission/cart-sync bugs found in one real conversation, root-caused
  and fixed** (`engine/shopper_v2/missions.py`,
  `intent_resolution.py`/`MissionItemIntent`): (1) naming an already-tracked
  item to add it ("add Energy Gel Box") produced a bare "Noted." with
  nothing added — the named-candidate branch narrowed the item but never
  reported or acted on it; fixed by reporting it and by adding a new
  `add_now: bool` field to `MissionItemIntent` so a named reference can add
  the item's real top candidate through the same risk-gated
  `ActionType.ADD_TO_CART` path (`fulfillment.start_fulfillment`) every
  other add already uses. (2) basket-propose re-proposed a mission item's
  top candidate with no awareness of the shopper's real cart (populated via
  a different path, e.g. direct accept-flow), sometimes proposing a wrong
  or duplicate product for an already-fulfilled need — fixed by fetching
  the real cart once before proposing and skipping any item whose real cart
  lines already share a significant word with the item's own label,
  reported as a new `already_in_cart` outcome. (3) multi-name partial
  references ("include the socks and energy gel box") failed because
  `resolve_named_subset` required each product's full catalog title as an
  exact substring; widened to accept a title's distinctive words, but only
  when a word is exclusive to one shown candidate, so two similar products
  sharing a word can't both resolve from an ambiguous match. All three
  verified live end-to-end together in one real conversation (no DB/state
  seeding): a mission item added directly, two partial names resolved
  together in one basket-propose turn, and the already-fulfilled item
  correctly excluded from later proposals.
  `tests/shopper_v2/test_phase7_missions.py`: 25/25 checks pass.
  `healthcheck.py`: 115 passed on a cooldown rerun (an earlier same-session
  rerun without cooldown showed 8 failures, all in payment-recovery code
  this diff never touched — confirmed as Groq throttle flakiness from heavy
  live testing, not a regression).
- **V1's reply-before-data patch** (`ReasoningService.synthesize()`) is
  implemented and live-verified for `COMPARE_PRODUCTS`/`RECOMMEND_PRODUCTS`
  on both merchants, but is tracked in PROGRESS.md pending a passing
  `value-auditor` run, not listed as a cleared roadmap item here.
- **Tenant isolation on both chat pipelines**, zero-data and platform-down
  honesty (no fabricated product, no raw exception reaching a shopper's
  reply) — verified for V1 and V2 independently.
- **Shopper account state now populates after a real signin/signup**
  (header username, cart's sign-in gate). Two compounding causes fixed:
  `SignInPage` was a sibling route of `App`, not a child, so it had no
  channel to hand the account back; and `App`'s mount-time `whoAmI` check
  had a React 18 StrictMode double-invoke race that silently discarded the
  one fetch whose promise actually resolved. Fixed with a same-tab custom
  event plus a corrected ref-check at resolve time; reproduced live before
  the fix and re-verified live after.

- **Streamed replies over SSE (`POST /api/v2/chat/stream`)** - the same turn as `/api/v2/chat`
  (shared `authorize_turn`/`execute_turn`: same auth, owner check, Decision -> Risk Gate -> Execution -> Adapter, one transcript write),
  reported as ordered, id'd events with heartbeats. Comparison, recommendation and compatibility replies stream REAL provider tokens
  (OpenAI-compatible `stream=True`; only the `reply` field of the forced `write_synthesis` call is forwarded; the engine's own grounding/cart-claim
  check runs before any draft is shown, with a `reset` if it fails); every other turn sends whitelisted status + the final message. A run registry
  makes reconnect (`Last-Event-ID` + `request_id`) replay without re-executing (a late retry is refused, in-process memory only), one turn per session, owner-matched, capped; disconnect/Stop never
  cancels the turn. The widget shows a typing indicator, a provisional draft bubble replaced by the final reply, and Stop; the plain route is
  the fallback. Verified model-free: ~200 backend checks (parser, LLM stream client, sink gate, endpoint over real HTTP incl. replay-once cart change,
  cross-merchant/cross-owner refusal) and 17 Playwright tests, including one against the real engine and a local slow fake provider showing the
  first visible text arrives while the provider is still writing. The live-provider behaviour is not verified (see PROGRESS.md).

- **Order lookup without an order number follows ownership.** `run_order_status_turn` resolves a named id, else the newest order the caller's own keys hold
  (`db.owners.latest_owned`: account first, else browser), else the conversation's own newest. Before, only orders recorded in the conversation were found, so a cart-panel purchase,
  a new device or a new conversation got "I don't have an order number". Verified model-free with a real Northfield checkout (`test_order_recovery_by_owner.py`, shown failing on the old code),
  incl. other-shopper, no-keys and other-merchant refusals. A real platform's order semantics are not verified.
- **Superlatives stay honest.** "Cheapest/most expensive" is never ranked across currencies (no honest answer, no exchange rate), a sold-out pick is labelled out of stock, the model-free
  comparison shows ratings when the platform has them, and the comparison facts the model sees now include rating and categories (`test_superlative_honesty.py`, failing before).

## Product Discovery & Recommendations

- **Live-fetch product comparison**: both compared products are always
  fetched fresh from the adapter, never from anything cached earlier in the
  conversation, on both V1 and V2.
- **Deterministic mission-aware shortlisting** (`mission_ranking.py`): hard
  filters (budget, exclusions) and soft, recency-weighted scoring
  (must-haves, priorities) narrow an unranked catalog slice into a small,
  evidence-backed shortlist with a grounded `fit` reason per product.

## Cart & Checkout

- **Cart intelligence and cart proposals** (shopper-facing loop and
  merchant-side measurement) — Smart Cart & Checkout Recovery's core
  mechanism is complete: a proposal is generated, shown, accepted or
  rejected explicitly, and measured.
- **Multi-item pending-proposal queue** handles 2-4 named items in one
  message with per-item disclosure.
- **Idempotent payment execution** on both the chat pay route and the
  sidebar Pay button, sharing one ledger — no duplicate charge on repeated
  taps or a different card.
- **Ambiguous payment outcomes are held, not released** (local, model-free
  verification only; NOT production-ready — see
  `PAYMENT_SAFETY_INVESTIGATION.md` for the open release gate): a timeout, 5xx,
  crash, disconnect or stale attempt parks the basket as `UNKNOWN_OUTCOME`;
  different-card, concurrent, cross-route and recovery attempts are refused
  until a platform lookup or a named operator resolves it. In the isolated
  harness one basket now yields one platform purchase in every scenario
  (previously two). The cart read carries the basket's `payment_state`, no
  checkout offer is made for a held or paid basket, an unresolved payment
  raises one deduplicated, self-closing merchant task (no payment-sensitive
  content) and an operator figure, and a recycled cart id never discards an
  unresolved ledger row without evidence. Verified by 169 payment checks
  across 9 test files plus the existing recovery/checkout tests, the
  model-free chain, fuzz and the route/tenant audit. The storefront disables
  payment for a held basket, says why, fails closed on an ambiguous payment
  failure and is lifted only by the server's own answer: 10 browser tests plus
  the existing suites passed on the final code (isolated stack, model-free).
- **A tap beats the model's guess**: `chosen_variant` from a shopper's own
  button press always overrides any `variant_id` the model proposed.

## Personalization & Context

- **Structured shopping state** (`ShoppingState`: goal, budget, must-have,
  exclusions, priorities, active products, mission status) is captured from
  the existing tool call, persisted, and read back into ranking and
  synthesis on V1; V2 has its own typed equivalent (`ShoppingStateV2`).
- **Durable shopper memory writes from a real conversation, not just a
  seeded row.** V2's `understand()` was calling the same model function that
  extracts `durable_facts` every turn but discarding the field, so nothing
  was ever persisted to `db.memory_facts` from a live shopper turn even
  though ranking could already read facts back in
  (`mission_ranking.rank_and_filter`). Fixed in
  `engine/shopper_v2/understanding.py`/`graph.py` (`node_understand`) to
  persist `durable_facts`, gated to signed-in shoppers, mirroring V1's
  `chat.py`; a narrow deterministic backstop covers explicit "I prefer X"
  phrasings the model itself sometimes skips. Live-verified end to end (no
  DB seeding) on both Northfield and Kettle & Bloom: a stated preference
  ("I really prefer lightweight gear" / "I really prefer decaf coffee")
  changes which product ranks first on the next turn, including through
  mission-item turns (multi-product tracking) — not just plain discovery.
  `ShopperMemoryFact` upserts with supersession and per-connection/shopper
  isolation.
- **A signed-in shopper can see and forget what is remembered about them** (`GET/DELETE /api/account/memory`, the account menu's "What I remember about you"): own rows only, per merchant,
  guest refused; the model never sees a forgotten fact. Verified by `test_shopper_memory_control.py` (listing, single/all delete, other shopper, other merchant, guest) and
  `shopper-memory.spec.ts` (5 browser tests; the endpoints are mocked there).
- **Write-only interaction instrumentation** (`ShopperEvent`): impressions,
  comparisons, cart adds/removes, checkout starts, purchases — logged for
  every relevant turn, not yet read back into ranking (tracked in
  PROGRESS.md as Personalized Recommendations' remaining gap).

## Merchant / Operations

- **Merchant console rebuilt as one coherent product** across all sections
  (Overview, Sales & Revenue, Orders & Conversion, Product Performance,
  Customer & Shopping Insights, Inventory & Catalog, Payments & Checkout,
  Returns, AI Commerce, Recovery, Holdout, Business Insights, Platform &
  Capabilities, Settings, Merchant Tasks), independently audited end to end
  (UI → API → repository → adapter → metric → Copilot/action), not just
  visually.
- **CV3 Merchant Copilot** answers from report, capabilities, policy,
  rules, actions, unmet demand, and catalogue alerts, and deep-links
  pending recovery cases and merchant tasks it already knows about. Its
  remaining "act on catalogue/inventory answers directly" gap is tracked in
  PROGRESS.md, not claimed complete here.
- **Real-volume catalogue scanning fixed**: `_scan_catalog` pages the
  adapter with real offset/limit pagination until a short page or a safety
  cap is hit, always reports `reachable`/`complete`/`truncated_at`, and was
  verified against real 150+120 product synthetic fixtures with exact
  count reconciliation — the worked CLAUDE.md example
  (`search_products("", limit=100)` presented as the whole catalogue) is
  fixed, not merely described as fixed.
- **CV3 Operations Copilot** answers ops_stats, pending/handover/decided
  queues across merchants, and correctly declines merchant-specific
  questions in favor of the Merchant Copilot. Its "act directly from the
  answer" gap is tracked in PROGRESS.md.
- **checkout_conversion / product_performance / business_diagnosis
  comparisons**: current-vs-prior-window deltas computed from one query
  shape run twice, with honest "no prior data yet" rather than a fabricated
  percentage, and minimum-order/minimum-change floors before naming a
  primary factor.

## Security & Reliability

- **The AI cannot assert risk** — risk properties come from a static table
  keyed by action type; there is no model-writable field for it.
- **Money never moves unattended** — `FINANCIAL_ALWAYS_HUMAN` fires before
  any merchant policy and cannot be disabled by a merchant.
- **Taking payment has no action type** — only a shopper's own tap reaches
  `/api/chat/pay`.
- **Raw platform errors never reach shoppers.**
- **Blocking escalation is an unblockable floor** — `ESCALATE_TO_HUMAN`
  always creates a real handover and never promises help it didn't create.
- **Approval-timeout expiry is swept and enforced**, including against a
  stale console still showing an expired approval as actionable.

## Verification

- **`healthcheck.py`**: grows as bugs are found and fixed; currently
  exercises well past its original scope, model-throttle-aware (SKIPs
  rather than FAILs when the provider is busy).
- **`fuzz.py`**: 20 random shopper sequences, 1200+ assertions, model-free,
  holding cart-integrity and cross-tenant/cross-shopper invariants on every
  run referenced across this project's recent sessions.
- **`auditroutes.py`**: probes every route's key requirement and shopper
  scoping, including two-cookie-jar cross-shopper checks.
- **`tests/shopper_v2/`**: about 100 standalone scripts (see ACCEPTANCE_MATRIX.md for the journey-by-journey run record and its gaps) covering typed contracts, state
  transitions, tenant isolation, zero-data/platform-down honesty, and
  phase-by-phase feature regressions (comparison, compatibility, cart
  proposal, checkout, payment recovery, missions, basket proposal,
  trade-off, cart status, discovery-vs-cart routing).
- **Storefront Playwright suites** (`storefront/tests/`): checkout,
  proposals, discovery routing, relevance/intent, merchant cart
  suggestions, business diagnosis, return-risk UI, and Shopper V2 (V1 and
  V2 parity, Kettle-side GraphQL coverage, payment recovery and order
  status through the browser). `npm run build` is the enforced typecheck
  guard; `npm run dev` does not catch type errors.
- **`invariant-guard`** has been run clean against every recent change
  touching `engine/risk`, `engine/execution`, `engine/api`, `engine/db`, or
  `shared/`, including the full cumulative Shopper V2 diff.
