# Progress

## Maintenance note

> This file holds what is **not** done: features not yet built, bugs not yet
> fixed, and open questions - the opposite list from `Completed.md`.
>
> **The rule that keeps the two files honest:** when an item here is fixed or
> built and fully verified - the relevant test suites pass (backend suites run
> against the live services; a browser check by hand or via Playwright where
> an HTTP test cannot see the bug), and any doc claim about it has been
> checked against the real, running product rather than assumed - move its
> write-up out of this file and into `Completed.md`, appended at the end of
> "Completed Work" with the next number. Do not leave a finished item
> described in both places, and do not describe an unfinished one only in
> `Completed.md`. A partially-done fix (code written, suite not yet run; one
> surface verified, another still assumed) stays here until every piece of it
> is actually verified.
>
> **And the value bar.** Passing tests is necessary and not sufficient. Before
> an item moves to `Completed.md` it must also carry written answers to
> CLAUDE.md's six value-bar questions, and a statement of which of the four
> real-client conditions (zero data, real volume, a platform that cannot, a
> platform that is down) were actually exercised and how. A feature that is
> correct, tested, verified in a browser, and changes nothing for anybody
> stays here - correctness is not the same as value, and this file has
> already let two features through on correctness alone. See CLAUDE.md's
> Feature Implementation Rules for the full bar, and use the `value-auditor`
> agent before moving anything out of this section.
>
> Reviewed and updated at the end of every session, same as before - what
> changed is which file a finished item goes in, not whether it gets written
> down.

---

## Features Not Yet Built

**The assistant is not installable.** A React component in our storefront, not a
script tag a merchant adds to their site.

**No gate runs the browser tests.** The Playwright layer exists
(`storefront/tests/checkout.spec.ts`, run with `npm test`) but nothing wires it
into a daily or pre-commit gate — it still runs when somebody remembers to run it.
The class of bug the finger-pointing in `Completed.md` kept hitting is now
catchable, just not yet caught automatically.

**A published reliability number.** `eval.py` exists and is unfinished. Nobody in
this market publishes one.

**No Postgres, no hosting, no repo split.** SQLite by connection string, local only.
The operations console shares a bundle with the shopper storefront, which is why the
operator key must be typed rather than built in.

---

## Known Issues / Pending

Reported after a walkthrough. **Not a complete list** - walk the product before
trusting anything.

**Occasional near-duplicate assistant messages** from the poll's deduplication -
the last real behaviour bug, browser-only. One reproduction attempt so far
(see `Completed.md`'s standing-lesson entries for the general practice this
follows): delayed the transcript poll's GET by 4s with Playwright network
interception, then fired several rapid model-free chat taps while that poll
was still in flight - the exact shape the `useEffect`'s stale-closure
structure (it depends on `turns`, which changes on every tap, tearing down
and rebuilding the interval and its `seen` snapshot each time) suggested
could cause it. It did not reproduce: three taps, three correct distinct
replies, no duplicates or drops. The one shape already known to cause a
near-duplicate - the pay endpoint prepending a sentence, making the stored
turn a suffix of what's shown - is already handled by the existing suffix
check, so whatever is still causing the reported duplicates is a narrower or
different timing window than the one tried. Worth retrying with a different
trigger (two consecutive poll cycles overlapping, or a race specifically with
an operator's approval writing into the session mid-poll) rather than more of
the same race shape. Reproducing this costs real model calls under the Groq
throttle, which slows down each attempt but is not why it remains unfixed -
the bug itself is a frontend timing issue independent of which model, or
whether one, is behind the chat.

A case can get stuck in `DIAGNOSED` state with no path to resolution - needs a
deliberate lifecycle-semantics decision before touching it. The code does
close the case out via `record_outcome`, so it is not literally orphaned, but
using `DIAGNOSED` as a terminal state for "needs a choice" is semantically odd.

Closing a handover with a blank note tells the shopper nothing by design,
which is worth revisiting rather than an obvious bug.

**One real user-reported chat bug, still open** (the original list had
seven; seven were addressed this session - see `Completed.md` #34, both its
original four and its later continuation covering the button-recovery
re-test finding, the comparison UI, and category-first browsing, all now
fixed and verified. This one item is a deliberate partial mitigation, not a
full fix):

- **Multi-item add in one message.** "Add the X size 8 and the Y size 8"
  still only ever adds the first item in one turn - `Completed.md` #34's
  continuation added an honest note telling the shopper the second item
  wasn't dropped silently ("ask me for the other one next and I'll add that
  too"), but both items are still not added simultaneously. A real
  architecture gap, not a quick fix: `engine/decision/engine.py`'s Decision
  Engine selects exactly one action per turn by design (its own docstring
  says as much), and the existing anti-double-add rule only trusts an exact
  tap or an exact whole-message match, never a parsed multi-item free-text
  guess - loosening that matching is the same fuzzy-matching trap
  `Completed.md` #15 already documents as tried and removed for causing
  double-adds. A real fix needs a pending-item queue that survives the tap
  round-trip across turns (propose item one, wait for the shopper's size tap,
  then automatically re-offer item two rather than requiring the shopper to
  ask again) - deliberately not attempted this session to avoid risking a
  half-built state machine that could reopen the double-add bug class.
  Whoever picks this up next should design that queue explicitly, not patch
  around the single-action-per-turn constraint.

### Why the tests did not used to catch these

**None of them could see the browser.** Every UI-state bug found by hand lived
in React state against `sessionStorage` - state held in two places, effects
firing in the wrong order, stale closures - and the HTTP suites never drove a
real browser. `storefront/tests/checkout.spec.ts` (see `Completed.md`, #21) is
the first thing that watches the actual browser. The remaining Known Issues
above are still disproportionately the kind no HTTP test can see - a next pass
should extend the Playwright suite to them rather than reach for
`healthcheck.py` again.

---

## Next Steps

### Existing Engineering Work

0. **The reply-before-data constraint.** `engine.reasoning.reason()` writes
   the shopper-facing reply before `execute_case()` runs (`chat.py:374` vs
   `:552`), so any reply describing data fetched during the turn is generic by
   construction — it is written before that data exists. Previously noted only
   inside the Smart Product Comparison checkbox below; promoted here because
   it blocks four Phase 1 shopper roadmap items, not one — see CLAUDE.md's
   Shopper Roadmap, items 1 through 4. Needs a second reasoning pass over the
   executed result, or a different pipeline shape. **Highest-priority
   architecture work.** Building any of the four blocked shopper items before
   this is fixed will produce exactly the generic output the value bar exists
   to prevent — do not start them first.

1. **Guest handover / session behaviour:** still open. Guests no longer pay,
   but can still chat and get escalated, and the shop now prompts for an email
   where it used to finish a sale. Whether this nudges merchant conversion is
   worth watching.

2. **Browser-level test coverage:** the Playwright layer now exists
   (`storefront/tests/checkout.spec.ts`, `npm test` from `storefront/`).
   Extend this existing suite rather than starting a second one. Best next
   addition is the near-duplicate-message poll bug: reproduce it in the
   browser with a different trigger before attempting a fix.

3. **Remaining Known Issues:** the `DIAGNOSED`-stuck item needs a deliberate
   lifecycle-semantics decision. The blank-note handover is also a deliberate
   behaviour worth revisiting rather than an obvious bug.

4. **Installable storefront assistant:** the next major onboarding gap.
   The current assistant is a React component in the CV3 storefront rather
   than a merchant-installable widget/script.

5. **Automated browser-test gate:** Playwright exists but is not yet wired
   into a daily, pre-commit, or CI gate.

6. **Reliability evaluation:** `eval.py` exists but the evaluation system and
   published reliability number remain unfinished.

7. **Production infrastructure:** Postgres, hosting, and separation of the
   shopper storefront and CV3 operations deployment remain unbuilt.

8. **Webhooks:** Kettle webhook support is completed (`Completed.md`, #31).
   Broader webhook support across real merchant/platform adapters remains
   future work where required by a platform.

---

## Research-Backed Product Work

The research roadmap is maintained in `CLAUDE.md`.

These features are planned product directions, not implemented functionality.
Before starting any item below, write its six value-bar answers and real-client
plan with the `feature-spec` agent — see CLAUDE.md's Feature Implementation
Rules. Do not build from the checkbox label alone.

### Shopper — Initial Priorities

**Blocked on item 0 above (the reply-before-data constraint) — do not start
these first:**

- [ ] Advanced Product Discovery
- [ ] Smart Product Comparison - **partially covered by `Completed.md` #27.**
      Checked directly against the code, not assumed: the comparison action
      (`ActionType.COMPARE_PRODUCTS`) fetches both products fresh via
      `adapter.get_product()` on every call - never from anything cached
      earlier in the conversation - and returns structured fields (title,
      price, description, availability), which satisfies "compare products
      using structured, live product information" in full.

      Not covered: "explain meaningful differences rather than simply
      listing specifications." `execution/service.py`'s `COMPARE_PRODUCTS`
      dispatch (lines ~466-495) returns exactly that - a plain listing of
      the same fields for each product, with no synthesis of which
      differences actually matter for the shopper's situation. It cannot be
      otherwise as built: the model's reply is written by
      `engine.reasoning.reason()` *before* `execute_case()` ever runs (the
      reasoning step precedes execution in `_process_turn`, confirmed at
      `chat.py:374` vs `:552`), so the reply is generic ("Let's see how they
      differ in price, stock and features") rather than a claim grounded in
      the actual fetched values - it is written before those values exist.
      Closing this gap needs a second reasoning pass *over* the fetched
      comparison (or a different pipeline shape entirely), not a UI or
      prompt change - it is a real, unimplemented capability, kept here
      rather than marked done. **See item 0 above - this is the same
      constraint, and fixing it here fixes it for all four blocked shopper
      items at once, not just this one.**
- [ ] Personalized Recommendations
- [ ] Mission-Based Shopping

**Not blocked:**

- [ ] Smart Cart & Checkout Recovery

### Merchant — Initial Priorities

- [x] **CV3 Merchant Copilot** — moved to `Completed.md` (#32, #35, #39, #42).
      Both slices of the "read-only twin" gap are now closed: payments/
      recovery (#35/#39, an existing platform action) and catalogue/
      inventory/unmet-demand (#42, a new `MerchantTask` record, since no
      platform action exists to attach - see Completed.md #42 for the full
      feature-spec-first build and verification). The Copilot now shows
      live banners for both pending recovery cases and open merchant tasks,
      each deep-linking to a real place a merchant can act. Re-run
      `value-auditor` before trusting this stays closed if the Copilot's
      scope grows further.

      **`_catalog_alerts`'s real-volume bug was fixed in an earlier
      session** (independently re-verified by reading `_scan_catalog`
      directly this session, not re-taken on trust):
      it no longer reads a fixed `search_products("", limit=100)` page and
      presents it as the whole catalog. `engine/api/routes.py::_scan_catalog`
      pages the adapter with real `offset`/`limit` pagination (added as an
      adapter extension on both `adapters/sample/adapter.py` and
      `adapters/kettle/adapter.py`, and on the two demo backends'
      `/api/v1/items` and GraphQL `products` operations) until a short page
      is seen or a 250-page/50,000-product safety cap is hit, and the result
      always carries `reachable`/`complete`/`truncated_at` so a partial scan
      is never presented as the whole catalog. Verified against a 150- and
      120-product synthetic fixture (`sample_merchant.store.seed_bulk`/
      `clear_bulk`, and the Kettle GraphQL `seedBulk`/`clearBulk` operations -
      test-only, `TESTBULK-`-prefixed, torn down after use) - exact counts
      confirmed (188 = 38 + 150 scanned on Northfield, 78 out-of-stock = 3
      real + 75 synthetic, products past index 100 reachable), tenant
      isolation held both directions, and the merchant's backend being killed
      mid-request degrades to `reachable: false` rather than a broken turn or
      stale data. `low_stock_available` also now distinguishes "checked,
      nothing low" from "this platform has no low-stock concept at all"
      (Kettle: boolean stock only), read from the adapter's own declared
      `CHECK_INVENTORY` capability constraint rather than guessed from an
      empty list. A merchant-console panel (`storefront/src/InventoryPanel.tsx`)
      now shows this too, reading the identical `/api/catalog/{connection_id}`
      function the copilot's "what's out of stock" answers use, so the two
      surfaces can never disagree.

      **Not moved to Completed.md** - this fixes the real-volume honesty bug
      CLAUDE.md names as the worked example, but the result is still
      read-only: a merchant sees the (now honest, now complete) list and has
      to act on it elsewhere. No existing report/ops figure moves from this
      fix either - it removes a silent-wrong-data risk rather than adding a
      new measured outcome. Value-bar items 4 ("where is the action") and 5
      ("what number moves") are not cleared, so this stays a fix to an
      existing anti-pattern rather than a newly completed feature, per the
      same reasoning the Merchant Copilot item above stays reopened.

      Response-quality fix applied this session: "which products are selling
      best" was returning both a quantity list and a revenue list of the same
      products back to back - pure repetition. `SYSTEM_PROMPT` in
      `engine/copilot/service.py` now routes a generic "selling best"
      question to `top_by_quantity` alone (revenue shown inline per
      product), a revenue-framed question ("generated the most revenue",
      "making the most money") to `top_by_revenue` alone, and "by revenue
      instead" always to `top_by_revenue` regardless of what came before
      (no conversation memory exists, so the phrase itself is the whole
      signal). Also fixed: "which products are underperforming" no longer
      relabels the lowest-quantity list as "underperforming" - there is no
      trend or benchmark in the data to support that judgment, so the
      copilot now says so plainly, while "which products sold the least" (a
      neutral ranking question, not a judgment) still answers directly from
      `lowest_performers`, labeled "lowest-selling BY QUANTITY". Verified
      live against both Northfield and Kettle for all seven phrasings; no
      duplication, correct ranking per phrasing, no false underperformance
      claim, tenant isolation held. This does not change the "not done"
      status above - it is a quality fix to the existing read-only
      answering half, not the still-missing act-from-the-answer capability.

      **The Merchant console was rebuilt as one coherent product** in a
      separate session (see Completed.md #36): left-nav across six groups,
      Overview, Sales & Revenue with a real trend, Product Performance
      fully exposed, Orders & Conversion (checkout-attempt success rate,
      genuinely instrumented), Customer & Shopping Insights, an honestly-
      unsupported Returns section, AI Commerce, Recovery, Holdout with
      sample-size caveats, and a deterministic Business Insights synthesis.
      This is a real, tested, browser-verified restructuring - but it does
      NOT close this item's own remaining gap (catalogue/inventory/unmet-
      demand Copilot answers still have no attached action) and introduces
      four of its own genuine, explicitly-documented gaps rather than
      faking them:

      **Items 1 and 4 below were closed in a later session** (see
      Completed.md #37) - cart-creation funnel instrumentation and the
      approval-timeout mutability/bug fix are both done and verified.
      Items 2 and 3 remain open, unchanged:

      1. ~~No full session-to-cart-to-checkout funnel~~ **Partially
         closed** (#37): cart creation is now genuinely instrumented
         (`FunnelEvent`, logged at `shop.py::create_cart` for every
         visitor, guest or signed-in), giving a real cart → checkout
         attempt → completed order funnel with bounded, correct rates.
         Still open: a true sessions/visits stage. This engine has no
         page-view event for a guest before they create a cart, so "how
         many people looked at the shop" remains unanswerable - that would
         need a materially different kind of instrumentation (a page-view
         or visit-start event), not an extension of the cart-creation log.
      2. **No real Returns capability.** Neither adapter declares a
         return/refund method, and `ISSUE_REFUND` is structurally
         unreachable (declared as a type, never in the model's proposable-
         action list, no adapter implements it). The Returns section says
         this plainly; building real return handling is its own feature,
         not attempted here.
      3. **No deep customer analytics.** No repeat-purchase index, CLV,
         segments, or cohorts exist in the schema - Customer & Shopping
         Insights is intentionally narrow (unmet demand + friction only)
         rather than fabricating any of those.
      4. ~~`approval_timeout_minutes` is still not merchant-configurable~~
         **Closed** (#37): the route had a real bug (any unrelated policy
         save silently reset the timeout to a hardcoded default) as well
         as being unexposed - both fixed, and Settings now has a genuine
         Save control, verified in a real browser to survive an unrelated
         settings change.

      **Overview rebuilt again (#40)** against a real Merchant SaaS
      dashboard reference - embedded Copilot, a real revenue trend chart
      (new `daily_revenue_series`/`/api/sales-series`, no charting library
      existed before this), a functional date-range picker, product-level
      attention (out-of-stock/low-stock only - no per-product conversion
      or returns figure exists to show), and opportunities trimmed to ones
      with a real destination (no fabricated "create promotion"/"compare
      products" actions). Verified live on both merchants with zero JS
      errors and real screenshots. Not yet done as part of this pass:
      - A live zero-data walkthrough (a freshly seeded empty merchant) -
        the empty-state code paths exist and follow the same pattern as
        every other panel, but were not exercised against a real empty
        connection this session.
      - A content-completeness catalogue scan ("poor product content" as
        an attention/opportunity source) - a real, buildable signal
        (`Product.description`/`image_url` can be null) but new backend
        work outside this slice's scope.
      - A merchant-account header badge (avatar/name/dropdown) - no
        backing session/profile-switching capability exists yet to make
        one real rather than decorative chrome, so it was deliberately not
        added.

      **Layout corrected in #41** after this first pass was rejected as
      looking like "many bordered boxes stacked vertically" rather than
      one coherent dashboard - the KPI+Copilot area was squeezed into one
      row with the KPI grid forced to 2x2; the reference actually shows a
      two-column hero (KPI row stacked above Trend+Health on the left,
      Copilot as one tall panel on the right). Restructured to match, KPI
      row forced to a true 4-across grid, opportunities rebuilt from
      stacked text rows into a real card grid with per-card action
      buttons, product-intelligence row proportions changed to give Top
      Products its due width, Commerce Health trimmed to the reference's
      five tiles, and the header block (wordmark, real merchant name,
      subtitle, account chip) added since #40 omitted it. Two real
      overflow/wrap bugs found and fixed during this session's own live
      screenshot verification (KPI values bleeding past their card border;
      a currency unit breaking mid-word) - same lesson as #40's off-by-one:
      verify against the rendered page, not the code.

      **Sales & Revenue brought to the same visual tier in #43** - its
      own analytics-oriented layout (KPI row, trend chart, top-by-revenue/
      top-by-quantity tables, a real evidence-based Attention panel,
      embedded Copilot), not a copy of Overview. Also fixed two real bugs
      found while building it: `average_order_value` was silently computed
      over a smaller population than `completed_order_count` implied (no
      way for a merchant to know why the numbers didn't reconcile - now
      exposed as `priced_order_count` with an honest caveat on both this
      page and Overview), and Overview's own KPI row never actually
      respected its date-range picker for Revenue/Orders/AOV (the `days`
      param was never passed to `/api/report`).

      **Orders & Conversion brought to the same tier in #44**, plus a
      genuinely new capability: `checkout_conversion` gained an optional
      `compare` parameter (one query shape run twice - current window and
      the equal-length prior window - not two independently-written
      queries), so the page can now answer "has conversion changed"
      honestly, with a real point-delta or an honest "no prior data yet"
      rather than an invented percentage. A new deterministic "biggest
      drop-off" panel names whichever real stage (cart-abandonment vs.
      checkout-failure) lost more this window. Visitor/session
      instrumentation remains explicitly out of scope - re-confirmed, not
      re-decided; still a separate, larger project.

      **Product Performance brought to the same tier in #45**, plus
      per-product period comparison (`revenue_change_pct`/
      `quantity_change_pct`, `None` rather than a fabricated 0% when a
      product is new this period) and a real "top-5 revenue share"
      figure (`total_revenue` - every distinct product summed, not just
      the shown top-N). **Found and fixed a real, pre-existing product-
      data-integrity bug while verifying this slice**:
      `db.idempotency.forget_payment` cleared the payment-ledger row for
      a recycled cart id (reissued after a merchant-backend restart) but
      never touched `OrderLine`, whose `row_id` derives from the same
      payment key - so a recycled cart id's new, genuine purchase
      silently overwrote whatever `OrderLine` rows its previous life had
      written instead of getting its own. Fixed to also purge those
      stale rows; verified against the literal failure mode (forced a
      real cart-id recycle, confirmed the stale row was purged at cart
      creation and the aggregate correctly dropped by exactly the
      earlier-corrupted amount).

      **Still fully open:** full page-by-page visual polish beyond
      Overview, Sales & Revenue, Orders & Conversion, and now Product
      Performance. Customer Insights, Inventory & Catalog, Payments &
      Checkout, AI Commerce, Recovery, Holdout, Business Insights,
      Platform and Settings still use the plainer card/list treatment
      from #36 rather than the KPI-card/status-badge/trend-chart system
      built for Overview in #37/#40/#41 and extended to Sales & Revenue
      (#43), Orders & Conversion (#44), and Product Performance (#45) -
      a real UI-consistency gap, not a functional one.

- [ ] AI Store Diagnosis
- [ ] Recovery Opportunity Radar
- [ ] Catalog Intelligence
- [ ] Inventory Intelligence

### CV3 Operations — Initial Priorities

- [~] **CV3 Operations Copilot** — answering half built and verified
      (`Completed.md` #33). **Reopened against the value bar**, same shape as
      the Merchant Copilot above: it reads the same four repository functions
      the console already renders (`ops_stats`, `pending_across`,
      `handovers_across`, `decided_across`) and cannot act on any of it. An
      operator who asks "what's waiting on me" still has to go to the queue
      and do it by hand.

      Remaining work: approve, close and escalate reachable directly from the
      answer itself, through the existing routes and risk gate. Do not move
      this back to `Completed.md` until that is built and `value-auditor`
      returns VALUABLE, not THIN.

- [ ] Cross-Merchant Command Center
- [ ] Integration Health Monitoring
- [ ] Automatic Incident Detection

These should not be treated as completed until each feature is implemented
end-to-end, all relevant tests pass, the real user-facing behaviour has been
verified, **and it clears the value bar in CLAUDE.md** - correctness alone is
not sufficient, per the maintenance note above.

### Implementation Priority

Existing correctness, security, testing, and merchant-onboarding work takes
priority over new product features. Within that, item 0 above (the
reply-before-data constraint) takes priority over any of the four shopper
items it blocks.

When a research-backed feature is selected for active implementation:

1. Write its spec first (`feature-spec` agent) - six value-bar answers, which
   real-client conditions apply and how they'll be exercised, which figure
   moves.
2. Keep it in this file while implementation is in progress.
3. Run `value-auditor` before considering it finished.
4. Only then remove it from `PROGRESS.md` and add the completed, value-cleared
   work to `Completed.md`.

Do not add the entire future roadmap to `PROGRESS.md`; the complete roadmap
belongs in `CLAUDE.md`. This file tracks only the features currently queued
for implementation and existing unfinished work.
