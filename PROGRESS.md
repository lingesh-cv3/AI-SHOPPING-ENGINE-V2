# Current Progress

## Overall Status

Overall project: **~70%** implementation complete against the currently
defined scope in CLAUDE.md's roadmap. Shopper V2: **~75%** implementation
complete — it is now the default shopper experience (V1 remains reachable
as a rollback), but several roadmap-level shopper outcomes and real-volume
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

### Shopper V2 as the default assistant — ~90% complete
- `/api/v2/chat` (`engine/api/chat_v2.py`) is now the default path the real
  storefront opens (`storefront/src/App.tsx`, `assistantMode` defaults to
  `"v2"`); V1 (`engine/api/chat.py`) remains fully intact and reachable via
  a build-time flag (`VITE_SHOPPER_DEFAULT=v1`) or a per-session "switch to
  classic assistant" control.
- V2 owns discovery, comparison, compatibility, cart proposals, checkout
  continuity, payment recovery, missions, basket proposals, trade-off
  re-ranking, cart status and clear-cart — all routed through the
  unmodified Decision Engine → Risk Gate → execution → adapter pipeline.
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

---

## Not Yet Built

### Advanced Product Discovery — ~30% complete
A deterministic mission-aware shortlist/ranking building block
(`engine/execution/mission_ranking.py`) exists and is wired into V1's
`RECOMMEND_PRODUCTS`, but no general free-text discovery turn delivering the
roadmap outcome ("a product they would not have found by keyword, and why")
has been built on either pipeline.

### Personalized Recommendations — ~15% complete
Write-only interaction instrumentation (`ShopperEvent`,
`engine/execution/events.py`) exists, logging impressions/cart-adds/
purchases, but nothing reads it back into ranking, synthesis, or durable
memory. A returning shopper is not yet shown anything that visibly reflects
past purchases/keeps/returns.

### Mission-Based Shopping — ~30% complete
`ShoppingState` re-ranks an already-fetched product list against a stated
mission (live-verified flipping the top-ranked product as priorities
change), but multi-item, cross-category basket building in one turn (e.g.
shoes + socks + hydration for a marathon) is unbuilt on V1; V2's basket
proposal work covers part of this and needs reconciling against the
roadmap outcome specifically.

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
