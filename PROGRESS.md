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

**Three real user-reported chat bugs, still open** (the original list had
seven; four were fixed this session - see `Completed.md` #34):

- **Multi-item add in one message.** "Add the X size 8 and the Y size 8" only
  ever adds the first item. A real architecture gap, not a quick fix: the
  Decision Engine selects exactly one action per turn by design, and the
  existing anti-double-add rule only trusts an exact tap or an exact
  whole-message match, never a parsed multi-item free-text guess - loosening
  that matching is the same fuzzy-matching trap `Completed.md` #15 already
  documents as tried and removed for causing double-adds.
- **Category-first browsing in the chat UI.** No way to show category
  choices before jumping straight to specific product suggestions.
  Investigation this session found a real head start for whoever picks this
  up: both merchant platforms already have working department/collection
  data and filtering that the engine/model never surfaces - Northfield's
  `dept` param on `/items`, Kettle's `collection` param on `search`. The gap
  is in the reasoning/prompt layer choosing to use it, not in either
  adapter.
- **Product-comparison UI.** Cramped buttons instead of a table, no
  per-item Buy Now action. Frontend work in `storefront/src/ChatWidget.tsx`,
  not yet touched.

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

- [~] **CV3 Merchant Copilot** — answering half built and verified
      (`Completed.md` #32). **Reopened against the value bar**, not complete.
      It answers from report, capabilities, policy, rules, actions, unmet
      demand and catalogue alerts, and is read-only by design: it proposes
      nothing, decides nothing, writes nothing. So a merchant who asks "what's
      out of stock" is told, and then has to go and do something about it
      elsewhere - the value-bar question "what can they do now that they
      could not do before" has no good answer as it stands.

      Remaining work: every answer that identifies a problem offers the
      action that fixes it, executable from that panel through the existing
      risk gate. Also owed: `_catalog_alerts` reads
      `search_products("", limit=100)` on every call, which is correct for
      Northfield and wrong for a real catalogue - the real-volume condition
      was never exercised. Do not move this back to `Completed.md` until both
      are addressed and `value-auditor` returns VALUABLE, not THIN.

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