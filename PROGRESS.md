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

### Shopper — Initial Priorities

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
      rather than marked done.
- [ ] Personalized Recommendations
- [ ] Mission-Based Shopping
- [ ] Smart Cart & Checkout Recovery

### Merchant — Initial Priorities

- [x] CV3 Merchant Copilot - built and verified end to end, see `Completed.md` #32
- [ ] AI Store Diagnosis
- [ ] Recovery Opportunity Radar
- [ ] Catalog Intelligence
- [ ] Inventory Intelligence

### CV3 Operations — Initial Priorities

- [x] CV3 Operations Copilot - built and verified end to end, see `Completed.md` #33
- [ ] Cross-Merchant Command Center
- [ ] Integration Health Monitoring
- [ ] Automatic Incident Detection

These should not be treated as completed until each feature is implemented
end-to-end, all relevant tests pass, and the real user-facing behaviour has
been verified.

### Implementation Priority

Existing correctness, security, testing, and merchant-onboarding work takes
priority over new product features.

When a research-backed feature is selected for active implementation, keep it
in this file until it is fully implemented and verified. Then remove it from
`PROGRESS.md` and add the completed work to `Completed.md`.

Do not add the entire future roadmap to `PROGRESS.md`; the complete roadmap
belongs in `CLAUDE.md`. This file tracks only the features currently queued
for implementation and existing unfinished work.