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

**An intermittent read-after-write lag on aggregate reads (`total_sales()`'s
`completed_order_count`, `checkout_conversion()`'s `carts_created`) against
the SQLite dev database, precisely isolated to real adapter I/O - real
issue, root cause narrowed but not fixed, independently re-investigated a
second time with a full binary-search rather than trusted from the first
pass's conclusion.**

First pass (see the session that found this) reproduced it with direct
instrumentation and tried two speculative fixes (SQLite WAL mode,
`NullPool`), neither of which changed the behaviour; both were reverted.
That pass guessed at pooling/aiosqlite-queue mechanics without confirming
which part of the real request actually mattered.

This second investigation instead built a binary search: a debug route was
temporarily added that isolates the DB layer entirely from HTTP
(`asyncio.run` calling `total_sales`/`payment_settled` directly, no
FastAPI/uvicorn in the path at all) - **zero staleness in 30/30 rounds**.
Then, progressively, real HTTP requests were added back one piece at a
time, each tested 20-30 rounds:

- single HTTP request doing write-then-read in one task - 0/30 stale
- two separate HTTP requests, minimal write (`begin_payment`+
  `payment_settled`, no `OrderLine` lines) - 0/30
- the same, with real `OrderLine` lines written - 0/30
- the same, with a 2-second `asyncio.sleep` between claim and settle
  (mimicking real adapter latency) - 0/30
- the same, with a *real* `httpx` GET to Kettle's own `/health` in that
  gap (real network I/O, not a timer) - 0/30
- the same, preceded by three separate real HTTP requests to Kettle's
  `/health` (mimicking the request-count shape of create_cart/add_to_cart/
  checkout, still with no DB-write complexity) - 0/30
- a full synthetic replica of the checkout route's entire post-payment
  sequence (`owners.take` for the order, `confirm_order`/
  `record_sent_mail`, `resolve_unresolved_payment_cases_for_cart`) added
  after a minimal write - 0/30

None of these reproduced it. Only two things did, both confirmed on
**two separate merchants and via two independent code paths**:

1. The real `signup` → `create_cart` → `add_to_cart` → `checkout` sequence
   through the actual `shop.py` routes - 4-6 stale rounds per 15 on Kettle
   across repeated runs, and separately reproduced on Northfield (3/15) -
   ruling out anything GraphQL-specific to Kettle's adapter.
2. A debug route that called the **real adapter's own** `create_cart`/
   `add_to_cart`/`checkout_with_card` methods directly - bypassing every
   one of `shop.py`'s own dependencies (`Depends(visitor)`,
   `Depends(shopper_scoped())`, cookies, `require_checkout_identity`)
   entirely - reproduced it at **10/30**, if anything more reliably than
   through the real routes. This conclusively rules out the auth/
   dependency/cookie layer as a cause: the real adapter I/O is sufficient
   and necessary by itself.

Signing up once and reusing the session across many rounds (removing
`signup` specifically from the loop) still reproduced it (5/15) - so it is
not about account creation, specifically the repeated real adapter round
trips (`PlatformClient`, `adapters/framework/http.py` - a **persistent**
`httpx.AsyncClient` held per adapter instance, not a fresh one per call,
unlike every synthetic reproduction attempt above) that are the necessary
and sufficient ingredient. **A real, independent instance of the identical
bug shape was found for free while regression-testing this second
investigation**: `healthcheck.py`'s own "a real cart creation is counted in
`carts_created`" assertion - unrelated repository function
(`checkout_conversion`, not `total_sales`), unrelated test, unrelated code
path - failed on one clean, isolated `healthcheck.py` run with the exact
same shape (real cart+checkout, immediate read, transient miss). This is
strong independent confirmation the phenomenon is general to "real adapter
round trip immediately followed by an aggregate read," not particular to
one function, one test script, or one merchant.

**What this rules out, with direct evidence, not assumption:** a test-
harness/script artifact (reproduces through a single, fully isolated
script driving the real routes with no other concurrent activity, and
separately inside the project's own `healthcheck.py`); the auth/dependency
layer; the specific DB-write shape (`OrderLine` writes, multi-session_scope
patterns, the post-payment notify/resolve-cases writes) in isolation;
generic network I/O or timing delay in isolation (a plain sleep or a plain
HTTP GET, even several in a row, never reproduced it); SQLite journal mode
and connection pooling as configured (both changed, neither affected it).

**What is now narrowed, not fixed:** the real adapter's own HTTP round
trip - through its persistent, per-adapter-instance `httpx.AsyncClient`
(`adapters/framework/http.py::PlatformClient`) - interacting with
aiosqlite's own async execution model under uvicorn's event loop is the
confirmed necessary ingredient. The exact mechanism inside that
interaction (most plausibly something in how `httpx`'s and `aiosqlite`'s
own async scheduling share one event loop, briefly reordering when a
commit becomes visible to a query issued from a different task shortly
after) is below what can be confirmed without instrumenting those two
third-party libraries' own internals, which this investigation did not do
- the user's own instruction for this investigation was explicitly not to
make speculative architectural changes or add sleeps/retries to hide it,
so none were attempted beyond the two (already reverted) tried in the
first pass.

**Verdict: C - a real issue remains**, precisely characterized rather than
vaguely gestured at, with the exact conditions that reproduce and do not
reproduce it now known. It has never been observed to lose, corrupt, or
duplicate data in any reproduction across either investigation - every
stale read is followed by a later read that is correct, and the underlying
`ExecutionAttempt`/`OrderLine` rows are always written correctly and
immediately regardless of when a report happens to observe them. `fuzz.py`
(1200+ assertions across many runs, both investigations) has never caught
a real invariant violation from it. Specific to the SQLite dev path - the
connection string is the only Postgres-vs-SQLite difference in this
codebase (`engine/db/session.py`'s own docstring) - not the Postgres
production target. The likeliest paths to an actual fix are either deep
`aiosqlite`-internals expertise (tracing exactly how its background-thread
queue interacts with `httpx`'s own async I/O under one shared event loop)
or moving local dev onto a real Postgres instance, where this class of
async-driver interaction does not apply the same way; both are bigger than
a targeted patch and were correctly not attempted here.

**Follow-up, same investigation, a third time - which specific figure this
actually affects, checked directly rather than assumed from the report
route's shape.** Prompted by a live report of Sales & Revenue's headline
Revenue appearing stuck across several real purchases: 36 real sequential
purchases were driven across both merchants (25 straight through on
Kettle, 3 on Northfield, plus 8 from the second investigation), each one
checking `total_sales_amount`, `priced_order_count`, and per-product
revenue (`/api/products`) against `completed_order_count` and
`checkout_conversion`'s `completed_orders` in the same before/after pair.
Result: **`total_sales_amount`, `priced_order_count`, and per-product
revenue moved correctly on every single one of the 36 purchases - zero
staleness.** `completed_order_count`/`checkout_conversion`'s
`completed_orders` (a structurally different query - see
`total_sales()`/`checkout_conversion()` in `engine/db/repository.py`) hit
the already-documented transient lag on several of the 36, always self-
correcting on a later read, exactly matching the shape already described
above. A live-browser check (real navigation away and back, the same
fresh-mount-refetch behaviour every page in this console already uses,
since this console has no page that polls) confirmed the same thing:
Sales & Revenue's Revenue figure updated correctly after two consecutive
real purchases, each followed by navigating away and back.

This does not contradict the verdict above - `total_sales()` computes
`order_count = len(attempts)` and the revenue figures from the identical
already-fetched `attempts` list in one function call, so nothing here
implies two different code paths for the same number. It narrows *which*
displayed figure a merchant is likely to actually notice lagging
(`completed_order_count`, i.e. the "Orders" KPI, not "Revenue") if they
happen to reload at the unlucky moment - useful for anyone chasing a
future live report of this, so it is not re-investigated as a mystery
each time. Revenue itself has not been observed to lag in 36 attempts
across two investigations; a report of Revenue specifically appearing
stuck warrants checking first whether the page was simply left open
without navigating away and back (this console fetches once per mount,
by design, on every page - not a bug, and not fixed by adding a poll,
which the console deliberately does not do anywhere).

A case can get stuck in `DIAGNOSED` state with no path to resolution - needs a
deliberate lifecycle-semantics decision before touching it. The code does
close the case out via `record_outcome`, so it is not literally orphaned, but
using `DIAGNOSED` as a terminal state for "needs a choice" is semantically odd.

Closing a handover with a blank note tells the shopper nothing by design,
which is worth revisiting rather than an obvious bug.

**A harmless console-noise regression from this session's session-id
hardening.** `db.owners.may_touch()` now takes `read_only`, wired into the
transcript GET route only (`_mine(..., read_only=True)` in `chat.py`'s
`transcript()`), so a GET against a session id nobody has posted a first
message to is correctly refused (404 `SESSION_NOT_FOUND`) rather than
silently claiming it. Side effect, found live: on a fresh page load, before
any message is sent, the transcript poll/mount-restore call now logs a 404
network error in the browser DevTools console, because it hits the
not-yet-claimed session. Both call sites already have pre-existing
`.catch()` handlers (unchanged by this session), so it is silently absorbed
- no user-visible behaviour changes and no functional regression - but it is
console noise that was not there before. Worth a follow-up to skip the
transcript call until after the first message is sent, not urgent.

**Shopping-state field-extraction reliability, from this session's Phase 1
foundation work (see Next Steps item 0).** The new `shopping_state` fields
on the `propose_actions` tool call are not reliably filled by the model on
every turn that has the information to fill them - one live trial captured
`goal`/`budget_amount` correctly but left `must_have`/`exclusions` empty
despite the shopper stating "lightweight" and "not waterproof" in the same
sentence. The storage/merge/retrieval mechanism itself is sound and tested
in isolation; whether the model can be prompted into consistently
populating every field across arbitrary phrasing has not been exhaustively
tested and should not be assumed.

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

**Weak relevance judgment in `SUGGEST_ALTERNATIVE`'s whole-catalog browse
fallback, pre-existing and unresolved** (found again, not newly introduced,
while a fix was tried and reverted this Phase 2 session — see item 0 above).
A search with no literal keyword hits (e.g. "kayak" on a shop that doesn't
carry one) can fall through to an unrelated item (a water bottle) with no
fabrication — the item is real, correctly priced — but no real relevance
judgment behind offering it. A keyword-overlap guard was tried and reverted
because it broke a legitimate case on Kettle ("a fruity coffee" fails a
literal "coffee" keyword match against varietal-named products like
"Ethiopia Guji Natural"), and could not distinguish that from a genuine
"shop doesn't carry it" case. Needs a better relevance signal than literal
keyword overlap, not attempted here to avoid re-introducing the same
regression.

**A pre-existing, previously-undiscovered logging bug in
`engine/reasoning/llm.py`, found (not introduced) during Phase 2 smoke
testing.** The "discarded unparseable tool call" warning path does
`logger.warning(..., extra={"name": call.function.name})`, colliding with
Python logging's own reserved `name` LogRecord attribute and raising
`KeyError: "Attempt to overwrite 'name' in LogRecord"` — turning a soft
warning into an unhandled 500 to the caller. Reproduced once, immediately
after a clean engine restart, on a bare "hello"; not reproducible on
immediate retry (three retries of the identical request all succeeded), so
it fires only when the model provider returns a malformed/unparseable tool
call — rare, not deterministic. `git diff engine/reasoning/llm.py` is empty,
confirming this is not something this session's own diff caused. A one-line
fix (rename the `extra` key, e.g. to `tool_name`) would close it; not
attempted, out of scope for this phase.

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

0. **The reply-before-data constraint — a scoped fix now built and live-
   verified, not yet value-audited.** A session on top of `d2db753` added
   `ReasoningService.synthesize()` (`engine/reasoning/service.py`) — a
   second, small model call made only after `execute_case()` has produced
   the real authoritative result, and only for two action types
   (`COMPARE_PRODUCTS` and `RECOMMEND_PRODUCTS`; every other action already
   has a deterministic engine-written reply and needs nothing). It carries
   no tool schema — it cannot propose an action, assert risk, or reach
   execution, confirmed by `invariant-guard`. Skipped when the primary
   reasoning call already fell back to rules, to avoid a second call that
   would fail the same way and to avoid extra latency on an
   already-throttled turn. Wired into `engine/api/chat.py`'s
   `COMPARE_PRODUCTS` branch (there was previously no dedicated branch at
   all — a successful comparison's reply was 100% the pre-fetch model
   guess) and the `RECOMMEND_PRODUCTS` branch (previously always the flat
   "Here's what's available — N options:" sentence).

   Live-verified in a real Playwright-driven browser, not assumed from a
   unit test: on Northfield, a shopper who stated "I need running shoes for
   a marathon, lightweight, under 15000, not waterproof" and then compared
   two named products got "Given your focus on lightweight shoes for a
   marathon, the Marathon Pro would be the better fit for you" next to a
   correct, live-fetched comparison table (screenshot at
   `storefront/scratch/comparison_verify.png`, gitignored scratch, not
   committed). On Kettle, a shopper who stated "a coffee that's fruity and
   not too acidic" and then compared two named coffees got "...it depends
   on whether you prefer the unique flavors of the Guji or the balanced
   profile of the Huila" — directly engaging the stated preference, where
   the pre-existing code produced generic boilerplate (this exact gap was
   independently reproduced live earlier in the same session, on this same
   Kettle comparison). Same code path on both connections, confirming
   platform independence.

   Alongside this, structured "current shopping state" (goal/budget/
   constraints/must-have/nice-to-have/exclusions/priorities/active_products/
   mission_status) is now captured from the *existing* `propose_actions`
   tool call — a new `ShoppingState` table and `engine/db/shopping_state.py`
   (get/merge) — no extra model call for this part. Scalars only overwrite
   when the model supplies a new value that turn (verified in isolation: a
   turn that only restates budget leaves goal/must_have/exclusions from an
   earlier turn untouched); list fields are additive/subtractive with
   case-insensitive dedup and an explicit `_remove` list (verified in
   isolation); `active_products` is set deterministically from the real
   executed comparison/recommendation product ids, never the model's guess.
   Honest gap found live: model reliability at filling every new field in
   one call is not perfect — one live trial correctly captured `goal` and
   `budget_amount` but left `must_have`/`exclusions` empty even though the
   shopper stated "lightweight" and "not waterproof" in the same sentence.
   The mechanism (storage, retrieval, context injection, and the synthesis
   pass reading it) works end-to-end; per-field extraction completeness
   across arbitrary phrasing does not, and should not be claimed reliable.

   Durable shopper memory (signed-in shoppers only) was also added — a new
   `ShopperMemoryFact` table and `engine/db/memory_facts.py` (upsert with
   supersession keyed on connection/shopper/key, expiry-aware read),
   populated from a new optional `durable_facts` field on the same tool
   call. Guests never get a row (no stable identity). Verified in
   isolation: a second upsert of the same key replaces rather than
   duplicates; different shoppers/merchants stay isolated. **Not**
   live-verified end-to-end — that needs a real model call actually
   flagging a durable fact and a real second visit, later, as the same
   signed-in shopper, which this session did not drive.

   Verification run against the live services this session:
   `invariant-guard` SAFE on the full diff (one follow-up fix applied: the
   new shopping-state/memory-facts reads and writes in `chat.py` were
   wrapped in try/except to match the existing best-effort pattern used
   nearby for `session_store.add_turn`/`record_case`, after invariant-guard
   flagged them as unwrapped). `healthcheck.py`: 115 passed, 1 FAILED (the
   same pre-existing, already-documented model-variance flake in a Kettle
   recovery `why()` check that appears elsewhere in `Completed.md` under
   this exact "115/1" signature — unrelated code path). `fuzz.py`: 20/20
   sequences, 1200 assertions, every invariant held. `auditroutes.py`:
   every probe held. `npm run build` clean, `npm run lint` unchanged
   baseline.

   **`value-auditor` run against this specific capability, same session —
   verdict THIN, not VALUABLE.** Confirmed the generic-reply defect is
   genuinely fixed rather than relocated (the raw API response for a live
   comparison turn still carries the old-shape pre-fetch `model_reply`
   field alongside the new, actually-grounded `reply` field the shopper
   sees — direct evidence the fix is real, not a renamed no-op), confirmed
   the "Buy now" buttons are driven by the unchanged `comparison` array and
   cannot be broken or hidden by the synthesis text, and confirmed a
   zero-shopping-state (Kettle, no stated preference) turn degrades
   honestly rather than forcing a winner. Two concrete, named gaps kept it
   from clearing the bar: **(1) no figure moves** — this is a real
   capability with no way to ever tell whether it helped anyone, the exact
   "unmeasured feature" anti-pattern; cheapest fix is logging whether a
   synthesized comparison reply preceded an add-to-cart of the "named
   better" product in the same session, even as a rough directional
   signal, before the next pass. **(2) the platform-down path for this
   exact call was never forced live** — `synthesize()` catching
   `LLMUnavailable` and falling back to the safe "Here's how they compare."
   sentence is sound by inspection and gated correctly (only ever called
   after `executed.succeeded`), but nobody has actually throttled/killed
   the model provider mid-comparison to watch it degrade for real. Do not
   move this, or the Smart Product Comparison entry below, to Completed.md
   until both are addressed and a follow-up `value-auditor` pass returns
   VALUABLE.

   Still blocked on this same constraint, unstarted: Advanced Product
   Discovery, Personalized Recommendations, and Mission-Based Shopping
   (items 1, 3 and 4 of the Shopper Roadmap) — the synthesis mechanism now
   exists and is reusable, but none of the three has been wired to it.

   **A follow-on Phase 2 session, on top of this one (uncommitted on
   `main`), added a deterministic mission-aware shortlist/ranking layer —
   still not moved here, see below.** New `engine/execution/mission_ranking.py`
   (`rank_and_filter`, pure function, no I/O) is now wired into
   `engine/api/chat.py`'s `RECOMMEND_PRODUCTS` branch, narrowing the
   previously-unranked first-N-from-catalog slice into a small (default 4)
   shortlist. Exclusions and budget are hard filters (a matching product is
   dropped, not just deprioritized); must-haves and priorities are soft,
   additive scoring via a deliberately coarse 6-character-stem keyword
   check against title/description text, with priorities recency-weighted
   so the most recently stated one outweighs an earlier one — this is what
   makes "actually prioritize X instead" genuinely re-rank the list rather
   than just reword the reply around a fixed set. Reads only
   `shopping_state` (the current-turn mission), never `ShopperMemoryFact`
   (durable memory) — a structural guarantee, confirmed by `invariant-guard`,
   that current explicit intent beats stored preference, not just a prompt
   request. Each surviving product carries a `fit: list[str]` field with
   real, grounded reasons ("within your budget (4299.00 INR)", "matches
   'lightweight'", "fits your priority: carbon racing plate"), traced by
   `invariant-guard` to only real product fields and `shopping_state`
   values, never invented. A new `mission_exhausted` reply branch in
   `chat.py` handles the case where every candidate was ruled out by a hard
   constraint, with a specific, honest deterministic reply rather than
   falling through to the stale pre-fetch model guess. `engine/reasoning/
   prompts.py`'s synthesis prompt was extended to hand the model this same
   `fit` evidence so its narrative can cite the real reason a product was
   shown. `storefront/src/ChatWidget.tsx`/`api.ts`/`styles.css` render it
   as a small "Why this: ..." line under each shortlisted product card.

   A real staleness bug was found and fixed live during this session's own
   testing: `shopping_state` was read at the *start* of `_process_turn`,
   before reasoning ran, so a mission stated for the first time in the
   current message was persisted by the merge step moments later but the
   in-memory variable used for ranking/synthesis in that same turn was
   still the old (often empty) value. Fixed by re-reading `shopping_state`
   from the DB immediately after the merge, only when the model actually
   supplied new shopping-state fields that turn. Verified live: before the
   fix a first-ever mission statement showed no `fit` reasons on the same
   turn's shortlist; after, it does.

   Live-verified, not narrated: on Northfield, "prioritize lightweight"
   then "actually prioritize carbon racing plate instead, for race day"
   flipped the shortlist's #1 product from Trailblazer Running Shoe to
   Marathon Pro Racing Shoe between turns, with the `fit` reason updating
   to match. A signed-in test shopper with a seeded durable memory fact
   (`prefers_lightweight=true`, simulating an established preference) who
   then said "prioritize cushioning over weight this time, I don't care
   about lightweight today" correctly got a shortlist led by the cushioned
   product and a reply following the current stated intent — structurally
   guaranteed, not just prompt-following, since `rank_and_filter` never
   reads durable memory at all.

   New minimal interaction instrumentation: a `ShopperEvent` table
   (`engine/db/models.py`) and `engine/execution/events.py`
   (`record`/`record_many`) log `product_impression` (per shown shortlist
   item), `comparison_started`, `add_to_cart`, `remove_from_cart`,
   `checkout_started`, `purchase_completed` from ~6 call sites in
   `chat.py`, all wrapped best-effort (confirmed by `invariant-guard` not
   able to break a turn, and the payment-completion event specifically
   confirmed to only ever fire after a real successful charge, never
   before or independent of one). Deliberately write-only this phase — not
   auto-promoted into `ShopperMemoryFact`, not read by ranking or
   synthesis — except for one new read function,
   `events.py::shortlist_engagement(connection_id, days=30)`, which joins
   `product_impression`/`add_to_cart` by `(session_id, product_id)` into a
   real conversion rate for the shortlist mechanism. Confirmed by grep this
   session: `shortlist_engagement` is not called anywhere in
   `engine/db/repository.py` or wired into any Merchant/Operations route —
   it exists and was run directly this session (first read
   `{'impressions': 72, 'converted': 0, 'rate': 0.0}` off real accumulated
   test-session traffic — real and unflattering, most of this session's own
   test scripts only viewed shortlists without tapping through — then, to
   prove the wiring actually works end to end, a real cart was created, a
   real shortlist fetched, and the real top-ranked in-stock item tap-added
   with a real variant; the same query then read
   `{'impressions': 87, 'converted': 1, 'rate': 0.0115}`), but has not been
   surfaced on any screen a merchant or operator can see.

   A relevance guard was attempted in `engine/execution/service.py`'s
   RECOMMEND_PRODUCTS whole-catalog browse fallback (refuse the fallback
   if none of its results share a literal keyword with the failed search
   term) and reverted after live-testing on Kettle caught a real
   regression: "a fruity coffee" is a legitimate in-catalog request, but
   searches for "coffee", which matches nothing because Kettle's products
   are named by varietal ("Ethiopia Guji Natural") — exactly the "search
   matches titles literally" pitfall this fallback already exists to route
   around. The guard could not tell that case apart from a shop genuinely
   not carrying something, so it was reverted; `git diff
   engine/execution/service.py` is comment-only, confirmed clean by
   `invariant-guard`. The underlying "kayak search → unrelated water
   bottle" honesty gap (no fabrication — the water bottle is real and
   correctly priced — but a weak relevance judgment) remains open and
   pre-existing, tracked as a Known Issue below rather than fixed here.

   Verification run this session: `invariant-guard` SAFE on the full
   diff (mission_ranking purity, no durable-memory read, `fit` evidence
   provenance, `ShopperEvent` write scoping/safety, payment path
   untouched, no risk-gate bypass). `healthcheck.py`: 115 passed, 1 FAILED
   (the same pre-existing, already-documented Kettle recovery `why()`
   model-variance flake — re-confirmed as the identical signature, not a
   new failure; two transient failures seen mid-session in an unrelated
   `/api/simulate` path were independently confirmed by a clean third
   rerun to be model non-determinism, not a regression). `fuzz.py`: 20/20
   sequences, 1200 assertions, every invariant held. `auditroutes.py`: all
   probes held. `npm run build`/`npm run lint`: clean, same pre-existing
   baseline.

   Full test matrix run live against both merchants: simple mission,
   exclusion, trade-off re-ranking, memory precedence, and isolation (two
   concurrent shoppers on the same connection with different missions,
   confirmed via `asyncio.gather` to never cross-contaminate) all PASS.
   Comparison synthesis itself is unchanged from the item-0 work above and
   was not re-tested deeply this session. Zero-data/relevance (the
   kayak/coffee case) came back mixed and honest, as described above,
   rather than clean. Real volume (150 synthetic test-only products seeded
   into Northfield via `_seed_bulk`/`_clear_bulk`, non-destructively torn
   down after) held the shortlist small and correct throughout. Capability
   limit (Northfield's `recoverPayment: supported=False`) is unchanged by
   this phase. Platform-down was forced live, not just reasoned about: 
   Northfield's own backend process was actually stopped mid-session, a
   real chat turn against `conn_demo` was driven while it was down, and the
   reply degraded to a normalized safe sentence with the raw transport
   failure confined to the internal `action_summary` field only — Northfield
   was then restarted and confirmed healthy with a normal follow-up call.

   **`value-auditor` verdict on this specific capability: THIN, not
   VALUABLE** — explicitly named as materially stronger evidence than the
   comparison-synthesis THIN verdict above (this phase changes what is
   shown and in what order, driven by verifiable deterministic logic, not
   just narration around a fixed list), but still short at the time of the
   audit on: (a) no merchant/ops-read figure existed yet, and (b) the
   platform-down path had only been reasoned about from code, not forced
   live. Both gaps have since had new evidence gathered *after* that
   verdict — `shortlist_engagement` now exists and was run against real
   data (point above), and the platform-down path was directly forced live
   (also above) — but neither has been re-submitted to `value-auditor`.
   **The THIN verdict stands as the last actual audit result.** A
   follow-up `value-auditor` pass is needed to judge whether this new
   evidence is sufficient before anything here moves to Completed.md — do
   not self-certify it as resolved.

   A pre-existing, previously undiscovered bug was found (not introduced)
   while smoke-testing this session, unrelated to any file this phase
   touched: `engine/reasoning/llm.py`'s `logger.warning(..., extra={"name":
   call.function.name})` on the "discarded unparseable tool call" path
   collides with Python logging's own reserved `name` LogRecord attribute,
   raising `KeyError: "Attempt to overwrite 'name' in LogRecord"` and
   turning what should be a soft-logged warning into an unhandled 500.
   Reproduced exactly once, immediately after a clean engine restart, on a
   bare "hello"; three immediate retries of the identical request all
   succeeded normally, so it's a rare path (fires only when the model
   provider returns a malformed/unparseable tool call), not something
   introduced by this session's diff (`git diff engine/reasoning/llm.py` is
   empty). A one-line fix (rename the `extra` key) would close it but was
   deliberately not attempted here — outside this phase's own scope, and
   fixing an unrelated file under time pressure without a dedicated
   verification pass of its own is exactly the scope-creep this project's
   practices warn against. Tracked as a Known Issue below.

   This Phase 2 work is uncommitted on `main` at the time of writing.

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

- [ ] Advanced Product Discovery - **not built as a feature**, but a
      reusable deterministic building block for it now exists and is
      live-verified: `engine/execution/mission_ranking.py::rank_and_filter`,
      wired into `RECOMMEND_PRODUCTS` (see item 0 above, the Phase 2
      addition) narrows an unranked catalog slice into a small,
      evidence-backed shortlist against the shopper's stated mission,
      with grounded `fit` reasons per product. This is the shortlisting
      half of discovery, not the "a shopper describing what they need in
      their own words reaches a product they would not have found by
      keyword" outcome itself — no general free-text discovery turn has
      been built around it yet, and the mechanism's own `value-auditor`
      pass came back THIN (see item 0). Do not treat this checkbox as
      started in the sense of a shipped roadmap feature.
- [ ] Smart Product Comparison - **the specific gap this item was tracking
      ("explain meaningful differences rather than simply listing
      specifications") now has a real, live-verified implementation** - see
      item 0 above for the full account (`ReasoningService.synthesize()`,
      wired into `COMPARE_PRODUCTS` in `engine/api/chat.py`, both merchants
      verified live in a real browser). The live-fetch half was already
      covered by `Completed.md` #27 (both products always fetched fresh via
      `adapter.get_product()`, never cached). What is genuinely new this
      session is the synthesis half, and it is real and demonstrated - a
      comparison reply now visibly reasons over the shopper's stated
      mission next to the actual fetched values, on both demo merchants.

      **Still not moved to Completed.md.** No `value-auditor` pass has been
      run against this specific capability yet, and this file's own
      maintenance note is explicit that a feature does not move on the
      strength of "the code is written and one path was checked by hand" -
      it needs a dedicated pass answering the six value-bar questions (in
      particular: is the difference it now names one a shopper genuinely
      could not get from the platform's own free comparison table, and what
      does it cost if the synthesis states a comparison wrong). Do not move
      this until that pass returns VALUABLE.
- [ ] Personalized Recommendations - unstarted as a feature. The
      product-interaction instrumentation it needs (impressions/views) no
      longer "does not exist at all" — a Phase 2 session added
      `ShopperEvent` and `engine/execution/events.py` (see item 0 above),
      logging impressions, comparisons, cart adds/removes, checkout starts
      and purchases. This is deliberately write-only this phase: it is
      never read by ranking or synthesis, and durable memory
      (`ShopperMemoryFact`) still has no automatic promotion path from it.
      A returning shopper being shown something that visibly reflects what
      they bought/kept/returned is still unbuilt.
- [ ] Mission-Based Shopping - unstarted as a feature, but its core
      deterministic mechanism moved substantially this Phase 2 session.
      `ShoppingState` (goal/budget/must-have/exclusions/priorities, item 0
      above) now drives real, live-verified re-ranking of shortlisted
      products via `mission_ranking.rank_and_filter` (hard-filters
      exclusions/budget, soft-scores must-haves/priorities, recency-weights
      trade-offs), demonstrated live flipping the top-ranked product between
      turns as stated priorities changed. What is still missing for the
      actual roadmap outcome ("a complete, compatible set of items... rather
      than treating every message as an isolated product query"): multi-item
      basket building in one turn is untouched - the Decision Engine's
      one-action-per-turn constraint (see the "Multi-item add" Known Issue
      below) still applies unchanged, and there is no cross-category bundle
      logic (e.g. shoes + socks + hydration for a marathon goal) - the
      mechanism only ranks/filters within one already-fetched product list.

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

      **All remaining Merchant areas brought to the same visual tier in
      #46** (Customer & Shopping Insights, Inventory & Catalog, Payments
      & Checkout, Returns, AI Commerce, Recovery, Holdout, Business
      Insights, Platform & Capabilities, Settings, Merchant Tasks) - see
      Completed.md #46 for the full verification. The Merchant tab is
      now visually and functionally one coherent product across every
      section in its navigation, with the embedded Copilot present on
      every page.

      **Independently audited for functional completion (not just visual
      consistency) in #47** - traced every area's real chain (UI → API →
      repository → database → adapter → commerce event → Merchant metric
      → Copilot/Task/action → outcome) rather than trusting that a page
      rendering meant the underlying capability was complete. Found and
      fixed one real bug (Settings/Copilot were exposing `ISSUE_REFUND`/
      `CANCEL_ORDER` as if they were real, toggleable actions, when
      neither can ever actually execute) and one real gap (Business
      Insights never surfaced unmet-demand as an insight). Every other
      area's full chain was confirmed genuinely complete with direct
      evidence, not assumed. See Completed.md #47 for the full audit
      record, including a real, reproduced-but-unresolved intermittent
      SQLite-dev-path read-after-write timing issue written up below.

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
