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
  creation, guarantees a paid cart cannot be charged twice on a different
  card, a reissued cart id cannot inherit a stale paid status, and a paid
  cart cannot be mutated afterward — enforced on both the REST cart routes
  and the chat/tap execution path.
- **Cross-tenant and cross-shopper isolation**, enforced by an httpOnly
  visitor cookie and an ownership table (`db.owners`), checked on every
  cart/session/order route on both V1 and V2.

## Shopper AI

- **Shopper V2 is the only shopper routing the real storefront calls.**
  `/api/v2/chat` (`engine/shopper_v2/`) is the sole path
  `storefront/src/ChatWidget.tsx` invokes (`api.chatV2`, unconditional) -
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
  candidates — a cart only changes after a shopper's own explicit call to
  `POST /api/chat/proposals/{id}/accept`.
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
- **`tests/shopper_v2/`**: 27 files covering typed contracts, state
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
