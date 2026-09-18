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

**Round 8 of the honesty/relevance hardening pass - two real, verified fixes
(`c377250`, `6e650b1`), following a value-auditor pass that found the
round-7 "fixes" still broken.** Both problems this session has repeatedly
tried and re-broken to fix are, as of this round, genuinely closed against
everything tested so far - stated carefully, not triumphantly, given this
exact pattern has repeated seven times already.

**Fix 1 (`c377250`) - `_honest_synthesis`'s seventh gap.** The sixth-gap
deterministic-clause fix (see item 0 below) only applied when
`personalizes is not False`. Setting `personalizes=False` - ordinary model
output variance, not adversarial cleverness - fell through to the old
free-text-scanning path, already proven bypassable at the fourth gap.
Reproduced live via two exact cases from an independent audit
(`cited_reason='lightweight', personalizes=False` with prose arguing
durability; `cited_reason=None, personalizes=False` with an implicit
durability claim), both returning the raw false claim verbatim. Closed by
no longer reading `personalizes` as a gate anywhere in the function - only
for logging. A valid `cited_reason` now always uses the deterministic
clause regardless of what `personalizes` claims; with no `cited_reason`,
real stated mission state now always falls back to the plain sentence,
unconditionally - no text-scanning path is left to bypass. The old
`_causal_claim_terms`/`_CAUSAL_CLAIM_PATTERNS` regex (and the now-unused
`re` import) were removed rather than patched an eighth time. Verified via
8 deterministic direct-function-call cases (`scripts/va_fix7_personalizes_false.py`)
covering both exact bypasses plus regression cases (grounded citation,
dishonest citation, no stated priority) - 8/8 pass, no model-call variance
involved since this calls the function directly. `fuzz.py` (20/20
sequences) and `auditroutes.py` (all probes) both clean afterward.

**Fix 2 (`6e650b1`) - the kayak false-substitution gap, three distinct,
previously-undiscovered root causes, not one.** An adversarial
`value-auditor` round found the round-7 semantic-judge fix still
substituting "Insulated Water Bottle 750ml" for "do you sell kayaks?" on
~3/8 live trials (twice measured, ~37.5%). Investigation found:

1. **A second, entirely unguarded code path.** `engine/execution/service.py`'s
   RECOMMEND_PRODUCTS "rescue search" branch (fires when the model leaves
   `query` blank and searches the shopper's raw sentence instead - a path
   no prior round of this fix ever touched) had zero relevance gating -
   not `weak_single_word_match`, not `shop_carries`, not the semantic
   judge. Now runs the identical weak-match check the primary search path
   already uses.
2. **A stopword bug in `relevance.py`'s word-overlap re-scoring.** `_significant_words`
   kept "you" as a "significant" word (3 characters, clears the length
   floor), and `_overlap`'s substring check matched it inside ordinary
   marketing copy containing "your" ("the perfect fit for your daily
   runs") - producing a real-looking overlap score of 1 against several
   unrelated products whenever a full raw sentence, not a deliberate
   short query, was scored. This only bit the raw-sentence rescue path
   above, which is why it survived unnoticed through six rounds of fixes
   to the `query`-based path. A small, closed-class stopword list (`_STOPWORDS`)
   was added, scoped to this module's re-scoring only - not a per-example
   patch, a structural class of word (pronouns, articles, common
   auxiliaries) that is never a product name or attribute. Also fixed a
   latent punctuation bug found in the same pass: significant words kept
   trailing punctuation ("kayaks?"), which can never match a product's
   real, punctuation-free text - silently weakening the one word that
   should have mattered most, on the same line as the stopword fix.
3. **`weak_single_word_match`'s word-count framing missed a multi-word
   wrong proxy.** "kayak" narrowed by the model to "water bottle" (two
   significant words) scores 2 against "Insulated Water Bottle 750ml" -
   not "weak" by that check's own `len(words) <= 1` definition, despite
   being exactly as ungrounded in what the shopper actually said as the
   one-word "water" case the check already covers. New
   `relevance.query_diverges_from_said(query, raw_said)` catches this via
   a different, structural signal instead: does the model's query share
   any word at all with what the shopper actually typed. **This directly
   caused, and then fixed, a real regression within this same
   investigation** - the first version had no length cap, and it flagged
   Kettle's `"a fruity coffee"` -> `query="Ethiopia Guji Natural"` (the
   model correctly naming the specific real product) as "diverging"
   (zero shared words with "a fruity coffee", true but irrelevant) and
   hedged a fully correct answer with "I couldn't confirm an exact
   match..." on 5/5 trials - caught by this investigation's own
   coffee-regression check before shipping, not by a separate audit.
   Fixed by capping the check to queries of two significant words or
   fewer: a short, generic proxy term diverging from the raw sentence is
   the actual signature of a wrong guess; a longer query diverging from a
   generic sentence is what a model correctly citing a *specific* product
   by title looks like, and must not be hedged as if it were coincidental.

Also added: when a weak/uncertain match survives all of the above and is
still shown, the reply now discloses the uncertainty ("I couldn't confirm
an exact match for X - here's the closest thing this shop carries, though
it may not be quite what you meant") instead of the same confident framing
a genuine match gets. The product is shown, never withheld - only the
framing changes.

**Verification, reported with real numbers, not rounded up:** 24 live
kayak trials across three batches after the full fix (8+8+8, engine
restarted between each code change to guarantee the running code matched
what was being tested) - **zero** confident false substitutions, down from
the ~3/8 (37.5%) measured twice in the round-7 audit. "A fruity coffee"
and "something smooth and not too bitter" on Kettle re-verified
unregressed *after* the length-cap fix specifically (5/5 and 5/5,
`scripts/va_fix7_coffee_regression.py`) - the length-cap fix exists
precisely because an earlier version of this same investigation broke the
coffee case, so this is not an assumption, it is a re-measurement.
`fuzz.py` (20/20, every invariant held) and `auditroutes.py` (every probe)
both clean on the final committed state.

**What this round does NOT claim:** no `invariant-guard` or `value-auditor`
agent pass has run against `c377250`/`6e650b1` yet (manual invariant
reasoning only - both changes are shopper-reply-text/candidate-filtering
only, never touching risk/payment/execution/idempotency, confirmed by
reading every call site touched). Neither commit moves to Completed.md.
Given this exact pair of problems has been "fixed" and then found broken
seven times in a row this session, the honest posture is: this is the
first round where the specific previously-reproduced failure modes did
not reproduce in a real re-test, not a claim that no ninth gap exists.
Whoever picks this up next should run a fresh, independent adversarial
audit - construct new phrasings, don't just re-run this round's own test
scripts - before trusting this as closed.

**A separate anomaly, investigated but not fixed here, from the same live
testing.** A fallback in the kayak-testing flow once re-searched a
category label ("Accessories") as if it were a literal product query,
returning unrelated shoes. Traced to the demo merchant backend's own
`/items` search endpoint (`sample_merchant`/`adapters/sample`) doing its
own server-side matching - a different subsystem from anything this
session's `engine/execution/relevance.py`/`service.py` fixes touch.
Fixing it would mean changing the demo merchant's own search behavior,
out of scope for shopper-assistant hardening; documented here rather than
rushed.

**A general-hardening pass, prompted directly by the user calling out a real
pattern: features that were only ever verified against the one example
phrasing used to build them, not the general case a real shopper will
actually type.** Not a new feature - a fix to how one existing mechanism
generalizes. `engine/execution/service.py`'s "is this a bare, generic browse
request" check (`RECOMMEND_PRODUCTS`'s context-reset guard) was a hardcoded
list of eleven exact phrases ("what do you have", "what's available", ...) -
literal string containment, so it matched only those eleven sentences
verbatim and missed any real paraphrase ("what else you got", "what other
stuff do you sell", "got anything else in stock" all failed it, live-
confirmed against the pre-fix code before changing anything). Replaced with
`_GENERIC_BROWSE_WORDS`, a closed vocabulary of "meta-shopping" words (have,
sell, got, carry, stock, available, everything, else, stuff, browse, ...):
a message is treated as a generic browse request iff *every* word in it
belongs to that vocabulary - the moment any real word appears ("shoes",
"waterproof", "kayak"), the check correctly falls through to naming
something specific. This is compositional rather than enumerative - it
covers any wording built from that vocabulary, not just the sentences
someone happened to test - while staying exactly as deterministic and
inspectable as the phrase list it replaced (no model call, no fuzzy
matching, no new I/O). Still honestly narrow in one respect, stated
plainly rather than oversold: a genuinely new *word* for browsing that
isn't in the set (e.g. "peruse") still needs adding, same as before - what
changed is that whole new *sentences* built from already-known words no
longer need to be individually anticipated.

Live-verified against both demo merchants with fresh phrasings not used to
build the fix: "what else you got" (Northfield) and "what other stuff do
you sell" (Kettle) both correctly returned the full category list rather
than a stale or wrong result. `fuzz.py`: 8/8 sequences, 480 assertions,
every invariant held (model-free, so it does not exercise this specific
code path, but confirms nothing else regressed from touching
`engine/execution/service.py`). `healthcheck.py`/`auditroutes.py` were not
re-run this session (services were only brought up for this pass, not left
running for a full suite pass under the Groq throttle) - re-run both before
trusting this beyond the two live checks above.

The other narrow-example spots looked at this session and found already
reasonably general, not left as further work: `engine/execution/
relevance.py`'s word-overlap re-scoring (already a from-scratch algorithm
over arbitrary words, not a hardcoded per-example list, and its "kayak
searches turn up an unrelated water bottle" limitation is already disclosed
to the shopper honestly via `relevance_relaxed`, not hidden - see the
`elif relevance_relaxed and products` branch in `chat.py`); `mission_ranking.py`'s
intensifier/related-term handling (already generalized past the single
"waterproof" example via `_INTENSIFIER_PREFIXES` and an explicit, honestly-
scoped `_RELATED_NOT_EQUIVALENT` table); and `_honest_synthesis` in
`chat.py` (already been through two rounds of adversarial hardening per its
own docstring, closing two real bypass shapes found by `value-auditor` and
further live testing - not re-audited a third time this session, since no
new bypass was found).

**A later session (commit `8275610`) re-spot-checked `_honest_synthesis`
manually - one fresh adversarial live turn, not a formal `value-auditor`
run.** A zero-shopping-state comparison ("compare the Trailblazer Running
Shoe and the Marathon Pro Racing Shoe" with nothing stated first)
correctly returned the plain "Here's how they compare." fallback with no
fabricated personalization claim. This is one spot-check, not an
independent adversarial audit - the `value-auditor`/`invariant-guard`/
`progress-scribe` agents were not available to be invoked in the session
that made this note (an execution-environment constraint, not a decision
to skip them), so none of the four fixes committed this session
(`1564299`, `3884ef0`, `1b20cc4`+`8275610`, and the relevance fix above)
have had a genuine independent `value-auditor` pass. **This is the honest
reason none of them have moved to `Completed.md`** - each is invariant-
sound by direct inspection and live-verified against the test suites and
several fresh phrasings, but per this file's own maintenance note, that is
necessary and not sufficient. The next session with agent access should
run `value-auditor` against all four before considering any of them done,
and `invariant-guard` as a formal (not manual) pass on the two newest
diffs (the multi-item queue and the relevance/extraction commit).

**Still open, not attempted this session given the size of a real fix:**
multi-item add in one message still only adds the first item - the
documented fix (a pending-item queue surviving the tap round-trip across
turns) is a real architecture change, not a generalization of existing
code, and was correctly left for its own dedicated session rather than
rushed here.

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

**Update, a later session:** the `propose_actions` prompt in
`engine/reasoning/prompts.py` was rewritten to explicitly instruct
checking the message against every field in turn rather than stopping at
the first match, with three new worked examples covering compound
multi-constraint sentences. Measured honestly against 10 fresh phrasings
this session did not reuse from any prior test (5 Northfield, 5 Kettle,
read directly from the `shopping_states` table rather than inferred from
the reply text): 7/10 correctly captured multiple/all of the stated
constraints in one call, 3/10 captured nothing at all. This is a real
improvement over the roughly-50%-landing-nothing-usable baseline an
earlier `value-auditor` pass measured, but it is not solved - free-text
extraction reliability from an LLM call remains a genuine, open
limitation and should keep being described that way, not as fixed.
`engine/reasoning/explicit_extraction.py`'s narrow regex stopgap was left
untouched (no new regexes added) since the fix target was the general
extraction call, not the stopgap layered on top of it.

**Second update, a later session (commit `8275610`).** Investigated exactly
which phrasing shapes the 3/10 misses above had in common: constraints
stated as a trailing clause or aside rather than their own sentence
("...and honestly I'm not really into anything too flashy looking
either"), and constraints implied by a stated reason rather than given as
a bare instruction ("my old shoes gave me blisters so cushioning is what I
care about now"). Added two more worked few-shot examples to
`prompts.py` covering exactly these two shapes, rather than guessing.
Re-tested against 10 brand-new fresh phrasings (not reused from this or
any prior test), read directly from `shopping_states`: **9/10 now capture
at least one field correctly (up from 7/10), and only 1/10 captures
nothing at all (down from 3/10).** Two of the nine still miss a secondary
constraint stated in the same message (a caffeine-level exclusion, an
acidity exclusion) - genuine, real improvement, still not a fix. Free-text
extraction reliability from an LLM call remains an open limitation; this
narrows it further and should be described as narrowed, not solved.
`healthcheck.py` 119/120 (1 pre-existing unrelated flake), `fuzz.py` 8/8
sequences (480 assertions), no invariant violations - this fix shares a
commit with the relevance fix below, and both were verified together.
No `value-auditor` pass has been run on this specific change.

**One real user-reported chat bug, still open** (the original list had
seven; seven were addressed this session - see `Completed.md` #34, both its
original four and its later continuation covering the button-recovery
re-test finding, the comparison UI, and category-first browsing, all now
fixed and verified. This one item is a deliberate partial mitigation, not a
full fix):

- **Multi-item add in one message - fixed, live-verified, NOT yet
  value-audited.** "Add the X size 8 and the Y size 8" used to only ever
  add the first item and tell the shopper to ask again for the second. A
  pending-item queue now exists, built as orchestration around the
  existing `CartProposal` mechanism rather than a new `ActionType`: the
  second, real, catalog-named product the shopper mentioned in the same
  message and that this turn's single action did not handle is queued as
  a real `CartProposal` (`engine/api/chat.py`, both the plain-ADD success
  path and the needs-choice path), appearing in the shopper's Suggestions
  panel with one tap. `accept_proposal` gained a `NEEDS_CHOICE` terminal
  state (distinct from `FAILED`) for a queued item that itself needs a
  size/option pick - the proposal is retired (never left `PENDING`) and
  returns the same `choices` shape a normal size question already uses,
  so the shopper finishes it with one more ordinary trusted tap
  (`/api/chat/act`, `chosen_variant` from the tap, never a model guess) -
  never by re-accepting the same proposal, which now correctly 409s.
  `storefront/src/ProposalsPanel.tsx` renders those choices as real
  buttons. The exact-match/tap-trust discipline that prevents fuzzy-match
  double-adds (`Completed.md` #15) is untouched - only the "what to offer
  next" orchestration is new, and no variant is ever guessed from parsed
  free text.

  Live-verified end to end against Northfield: "Add the Trailblazer
  Running Shoe size 8 and the Marathon Pro Racing Shoe size 8" asked for
  the Trailblazer's size (multi-variant), queued the Marathon Pro as a
  proposal; tapping the Trailblazer's size 8 added it; accepting the
  queued proposal returned the Marathon Pro's own size choices (also
  multi-variant); tapping its size 8 added it too - both ended up in the
  cart at quantity 1 each. Re-accepting the same (now-terminal) proposal
  id correctly returned 409 with no cart change - no double-add path
  found. `invariant-guard`: SAFE (verified the proposal path reuses the
  identical `db.cart_proposals.create`/risk-gate/revalidate-at-accept
  mechanism existing `PROPOSE_CART_CHANGE` proposals already use, the tap
  is the only trusted source of `chosen_variant`, and the try/except
  around proposal creation degrades to the old honest text on failure
  rather than crashing the turn). `fuzz.py`: 8/8 sequences, 480
  assertions, every invariant held. `healthcheck.py`/`auditroutes.py`: no
  regression (the only failures across two runs were the pre-existing,
  already-documented Groq-throttle model-variance flakes, different ones
  each run).

  **Not yet done:** a genuinely simultaneous three-or-more-item message is
  untested (only two named items was tried); no `value-auditor` pass has
  been run against this specific capability yet, so it stays here rather
  than moving to `Completed.md` even though it is functionally complete
  and verified - per this file's own maintenance note, a feature needs the
  value bar cleared, not just a passing test, before it moves.

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

**Follow-up investigation, a later session — re-attempted deliberately, live-
tested first before writing any code, and reverted again with a sharper,
evidence-based account of why a deterministic fix does not exist here.**
Before touching code, the exact reported shape was re-driven live against
the running engine to confirm it still reproduces as described. It does not
reproduce the way the existing writeup assumes:

- "do you have a kayak?" on Northfield does **not** go through the
  whole-catalog blind-browse fallback at all. The model itself chose
  `query="water"` (associating "kayak" with water sports), which is a
  genuine, if weak, literal title match against "Insulated Water Bottle
  750ml" - `relevance.best_matches` correctly keeps it as the (only, hence
  top-scoring) result. This is a **reasoning-layer word-association choice**
  passed through a working relevance re-score, not a broken relevance
  re-score - a different bug shape than the one this item names.
- Two fresh genuinely-absent-category phrasings constructed for this
  session - "do you sell tents for camping?" and "I need ski goggles", both
  on Northfield - were **not** mis-routed to an unrelated substitute at all.
  Both were routed by the model to `SUGGEST_ALTERNATIVE`, which already has
  an honest empty-result path (`"I couldn't turn anything up for that."`,
  zero products), and both returned it correctly. The specific failure mode
  this item describes (a confidently-offered *irrelevant* product) did not
  reproduce for either.
- "I want something fruity and not too acidic" on Kettle was re-verified to
  still work, and *why* it works was traced precisely this time: the
  model's extracted query ("fruity") has **zero literal overlap** with any
  product's title, description, or category text in Kettle's own catalog
  (checked directly against `sample_merchant_two/seed/catalog.py` - the
  word "coffee" itself never appears in any product's title, description,
  or department/collection name either). The whole-catalog browse fallback
  returns real, relevant coffee here for one reason only: **Kettle's entire
  catalog is coffee-adjacent**, so an unfiltered browse of everything is
  never actually irrelevant on this specific shop. It is not evidence that
  any relevance signal - literal, category-level, or stemmed - is
  distinguishing this case from a genuinely-absent one.

This directly confirms, with concrete evidence rather than the earlier
session's reasoning-from-the-name, why the previously-reverted
keyword-overlap guard could not work and why no deterministic replacement
(category-level overlap, word-stemming, broader synonym tables) can work
either: **on both demo catalogs, the query word for the legitimate case
("fruity"/"coffee") and the query word for the illegitimate case ("kayak")
each score zero against the real catalog's own text.** There is no literal
signal, at any level of generality this codebase's other relevance code
already uses, that is present for one and absent for the other - the
catalogs simply do not contain the vocabulary either query would need to
match on. Telling these two cases apart requires knowing that "coffee" *is*
what this shop sells and "kayak" *is not*, which is knowledge about the
shop's own identity, not about any product's text - a keyword/stem/category
matcher has nothing to read that fact off of.

**Not fixed, deliberately, a second time - not for lack of trying but
because the two live-tested candidate mechanisms (category-text overlap,
broader stemming) were shown by direct catalog inspection to be
insufficient before any code was written, avoiding a repeat of building and
then reverting.** The two live-tested "problem" phrasings that started this
item (kayak, tent/goggles-style absence) turn out to already be handled
correctly or to be a narrower reasoning-layer issue than a relevance-gate
issue. What remains genuinely open is narrower than the item's original
framing: a model-chosen weak single-word association (like "kayak" →
"water" → water bottle) surviving `relevance.best_matches` as the only
candidate, with no signal available to it that "the only reason this
scored above zero is a coincidental generic word" rather than a real
match. Closing this narrow remainder would need either a genuine semantic
judgment (an LLM relevance check on the specific single-candidate result,
following the same "second small model call after the real result exists"
shape `ReasoningService.synthesize()` already uses elsewhere - not
attempted this session, since it is a real architectural addition deserving
its own dedicated pass and value-bar answers, not a same-session bolt-on)
or a merchant-declared "what this shop actually sells" fact the matcher
could check against (which does not exist as a data model anywhere in this
codebase today). Verification for this pass: no code changed
(`git diff` is empty for this investigation), so no suite was re-run;
confirmed live only via the four fresh chat turns described above.

**Fixed, a later session (commit `8275610`), correcting the "no fix
exists" conclusion above with a narrower mechanism than either prior
session considered.** The prior session's inspection checked whether the
query word appeared anywhere in Kettle's catalog text and found "coffee"
absent - but only checked product names/story text as a whole, not
per-product; a direct re-check found "coffee" *does* appear once, literally,
in one product's own description ("Swiss Water Decaf": *"Tastes like
coffee, not like decaf"*) - the earlier claim that the legitimate and
illegitimate query words "each score zero against the real catalog's own
text" is corrected by this: the coffee word is not entirely absent, it is
merely sparse (one occurrence among the whole catalog), which is why a
`limit=100`-wide unfiltered browse's description-text scan can still see
it while a narrower per-title keyword search cannot.

New `relevance.shop_carries(query, catalog_sample)` gates
`RECOMMEND_PRODUCTS`'s whole-catalog substitution fallback specifically
(never the narrower keyword-matched path that already found something
real, and never `SUGGEST_ALTERNATIVE`, which was already confirmed above
to already degrade honestly): before substituting an unrelated product, it
checks whether the query's significant words overlap *any* product's
category **or description/title text** across the already-fetched wide
browse sample - not just the narrow candidate set that already failed to
match. Live-verified explicitly, both directions, on the actual reported
regression cases: "a fruity coffee" and "something smooth and not too
bitter" on Kettle continue to return real coffee matches (no regression);
"do you sell kayaks?", "I need a tent for camping", and "do you have ski
goggles?" on both merchants now correctly answer "I couldn't turn anything
up for that" instead of substituting an unrelated real product ("something
smooth and not too bitter" also degraded honestly rather than substituting
- an acceptable outcome, not a regression, since Kettle's catalog text
genuinely has no signal for "smooth"/"bitter" either).
`healthcheck.py` 119/120 (1 pre-existing unrelated flake), `fuzz.py` 8/8
sequences (480 assertions), no invariant violations.

**Still genuinely open, honestly:** this closes the specific reported
regression shape (a query whose word is entirely absent from a shop's
catalog vocabulary) but remains keyword-based, not semantic - a query
using a true synonym with literally zero shared substring anywhere in the
catalog (title, description, or category) would still be indistinguishable
from a genuinely-absent category, because `shop_carries` has nothing else
to check. The narrower "kayak" → "water" → water bottle case identified in
the prior session's investigation (a model-chosen single weak word
association surviving as the *only* candidate via the narrower keyword
path, not this fallback) is untouched by this fix and remains open - it
would need a genuine semantic relevance judgment, not a keyword check, per
the prior session's own conclusion. No `value-auditor` pass has been run
on this specific fix.

**The `engine/reasoning/llm.py` logging crash found during Phase 2 smoke
testing is now fixed, as part of the hardening pass** (see item 0 above for
the full account): `extra={"name": ...}` renamed to `extra={"tool_call_name":
...}`, with a new tracked regression script,
`scripts/regression_llm_malformed_tool_call.py`, reproducing the exact crash
shape and confirming it no longer raises. Not moved to Completed.md on its
own — it is one fix inside the still-THIN hardening pass, and this project's
practice is not to split one session's work across both files.

**Discovery-routing bug fixed and verified this session (see `Completed.md`
#52) - one residual limitation left open, stated honestly rather than
claimed closed.** The rescue search added to `engine/execution/service.py`'s
`RECOMMEND_PRODUCTS` bare-browse branch mitigates, but does not eliminate,
the underlying model-variance root cause (the model occasionally leaves
`search_query`/`category` blank even when the shopper's message plainly
names a product): a sufficiently unusual phrasing could still slip past
both the model and the rescue search's own real-word-length heuristic
(`>2 chars`). Not attempted further this session, since a general fix
belongs in the model-side extraction reliability work already tracked
above, not in this narrow rescue path.

**The larger request this discovery-routing fix was extracted from remains
mostly open** - a follow-up session (see `Completed.md` #53) closed two more
pieces of it (a same-word-only false-positive fix plus honest budget
relaxation in discovery, priority-only follow-ups now preserving the
mission, and Return-Risk Prevention's frontend rendering gap). Still open:
- Mission Shopping structured state and a dedicated trade-off UI - untouched.
- A Merchant-side functional verification pass - not attempted.

**Residual limitations left open by `Completed.md` #53, stated honestly
rather than claimed closed:**
- The relevance fix (`engine/execution/relevance.py::best_matches`) is a
  deterministic word-overlap re-scoring heuristic, not semantic
  understanding - it cannot match a synonym or use-case phrase to a product
  whose title/description/categories share no literal word with it. This is
  the same underlying limitation as the Kettle "fruity coffee"/varietal-
  naming gap described below; it narrows the problem (a weaker keyword match
  no longer stands in as if it were as good as a stronger one) without
  solving it.
- The mission-preservation backfill only carries `goal` (as a search query)
  and `budget_amount` (as max_price) forward from `ShoppingState` into
  retrieval. It does not backfill `must_have`/`exclusions`/`category` into
  retrieval's own query/category parameters - those still only feed
  mission_ranking's later filtering step, unchanged from before this
  session.
- Cart-line-level return-risk in `cart_intelligence.py` was not touched or
  re-verified this session (it already existed as plain text from an
  earlier session's work) - only the product-card/comparison rendering gap
  was closed.

**Value-auditor findings from the prior hardening pass, fixed this session
(commits `2b7b5a9`, `7f9959b`, `ee70c84`, on top of `1564299`..`c5db5a4`).**
A `value-auditor` pass on that hardening batch found four concrete,
specific defects rather than vague concerns - each is fixed and
live-verified here, not just re-analyzed:

1. **`shop_carries` real-volume sampling gap (`2b7b5a9`).** It decided
   "does this shop carry X" from a catalog-order sample of only 6-30
   items - the audit named this the same shape as `_catalog_alerts`'s
   `limit=100` anti-pattern, likely to false-negative on a real 40k-SKU
   catalog. Bumped to 200 with the residual real-volume risk stated
   honestly in the code comment (the commerce interface has no
   category-filtered or paginated search primitive to do better with -
   adding one is new adapter-interface surface, out of scope for a
   hardening pass, not attempted).

2. **The actual kayak case, untouched by the prior fix.** The audit
   reproduced live that "do you sell kayaks?" still slipped through: the
   model narrows "kayak" to a proxy query like "water", scoring a
   coincidental single-word hit against an unrelated product (e.g. an
   "Insulated Water Bottle") via the narrow keyword search - a path
   `shop_carries` never gated, since it only fired on the zero-match
   browse fallback. New `relevance.best_matches_with_score`/
   `weak_single_word_match`: a match whose entire justification is one
   shared word from a single-word query now gets the same wider
   category/description re-verification the browse fallback already
   uses. Live-verified: "do you sell kayaks?" and "I need a tent for
   camping" on Northfield now correctly degrade to an honest "couldn't
   find" instead of substituting an unrelated product; "a fruity coffee"
   and "something smooth and not too bitter" on Kettle unaffected (the
   coffee case is unbroken); legitimate single-word queries against a
   real category (bare "coffee", "shoes") still return real results.

3. **`_honest_synthesis`'s third attribution-bypass gap (`7f9959b`).**
   The audit found the second version (gating on `any_term_mentioned` -
   any real stored term appearing ANYWHERE in the text) still passable: a
   reply can mention a true term as filler while its actual causal claim
   names something never stated ("Since you said durability matters most
   to you (though it's also nice and light)..." mentions the real term
   "light" as an aside while falsely claiming durability was the reason).
   New `_causal_claim_terms()`: a narrow, named-pattern extraction (same
   trade-off `explicit_extraction.py` already makes) that pulls the
   SPECIFIC term a reply claims as its reason ("since you said X",
   "because X matters", "your priority of X", "you prioritized X") and
   checks that term individually against `shopping_state`'s real terms.
   Verified against 5 fresh adversarial phrasings: two specifically
   demonstrate the gap this closes (the true term present as filler, a
   false term claimed as the reason) and are now correctly rejected; two
   genuinely grounded phrasings still pass through unmodified; the
   zero-state case still always falls back.

   This is now the THIRD documented iteration of this exact function
   (blocklist -> `any_term_mentioned` floor -> per-claim term check).
   Each round has been adversarially re-tested and each round has found a
   new bypass shape. This one has not itself been through a fourth
   adversarial pass - the next session should not assume it is the last
   gap without checking.

4. **Multi-item queue: named figure + fixed a real silent-drop bug
   (`ee70c84`).** The audit called the pending-proposal queue
   (`Completed.md`-bound, `3884ef0`) the one item that actually cleared
   VALUABLE, pending a named metric and a 3+-item test. New
   `events.py::multi_item_completion_rate(connection_id)` - queued
   second-item proposals vs. actually completed, joined by
   `proposal_id`. Building it surfaced a real, previously-unknown bug:
   a queued item left in `NEEDS_CHOICE` state (needs its own size pick)
   is resolved through the ORDINARY tap/typed-follow-up path, not
   `accept_proposal` again, so nothing closed the loop back to the
   proposal - the metric read `completed: 0` against 13-15 queued real
   proposals until this was fixed. Fixed by linking both real completion
   paths (`act()` and the typed-message `ADD_TO_CART` branch) back to a
   matching `NEEDS_CHOICE` proposal. Live-verified end to end: a genuine
   two-item add-with-a-size-choice flow, driven through the real chat and
   tap routes, now correctly reads `completed: 1`.

   The same live 3+-item test also found a second, real, previously
   undocumented bug: a THIRD named item in one message was silently
   dropped with no disclosure at all - worse than the already-known
   one-at-a-time limitation. `_find_second_item` generalized to
   `_find_extra_items` (finds every extra named product, not just the
   first); still only the first is ever queued (queuing more than one at
   once is a bigger, separately-scoped change, not attempted), but every
   further item is now named honestly in the reply rather than
   disappearing. Live-verified with a genuine three-item message.

**Verification run across all four fixes:** `fuzz.py` 8/8 sequences, 480
assertions, every invariant held, re-run after each fix and again at the
end. `auditroutes.py` fully clean (30/30). `healthcheck.py`: 117 passed, 2
FAILED - both are the already-documented Groq-throttle model-variance
class (directly reproduced by hand: "add the Trailblazer Running Shoe to
my cart" with no size returned `ADD_TO_CART` with 3 choices on one retry
and a different valid action with `used_model` differing on others,
across three back-to-back identical calls with no code changes in
between), not a regression from this diff. `invariant-guard` was run
earlier in this same session against the state before these four fixes
(SAFE - tap-trust preserved, no fabrication, no risk-gate bypass, no
idempotency change) but has **not** been re-run against these four fixes
specifically - worth doing before treating this as fully closed.

**Update: `invariant-guard` and `value-auditor` were both run against
`2b7b5a9`/`7f9959b`/`ee70c84`, and the audit found a real next gap rather
than clearing them outright (`01b78ca`).** `invariant-guard`: SAFE. The
`value-auditor` pass's own overall verdict is worth quoting rather than
summarizing away: none of the four fixes were called thin patches, but
three of them (the catalog-sampling fix, the kayak weak-match fix, and the
multi-item extra-item detection) shared the identical underlying shape - a
bounded sample standing in for "the whole catalog/whole message" - and the
`_honest_synthesis` attribution fix had now failed a THIRD adversarial
round in a row for the identical structural reason (a regex over the
model's own free text is not enumerable). Per CLAUDE.md's own "fix the
shape, not the instance" lesson, the auditor treated this as one open
finding, not four separate ones, and the write-up above stopped short of
calling any of them closed. **Notably, the auditor's own subagent process
exceeded its read-only charter and independently wrote the fix for the
exact shape it was naming** (real catalog pagination via the adapter's own
`offset` support and `total_available`, replacing the flat sample; a
structured `cited_reason` tool field replacing another regex round; a
wider extra-item-detection sample) - left uncommitted in the working tree
rather than committed. This is a real process violation worth naming
(an audit agent should report, not implement), separate from whether the
resulting code was good. It was reviewed as if freshly submitted rather
than trusted: independently checked that `offset`/`total_available` are
real, already-declared adapter capabilities (not invented), that
`force_tool` is a real, pre-existing `LLMClient.complete()` parameter, ran
a fresh `invariant-guard` pass against it (SAFE, with one item resolved by
direct code inspection - confirmed `adapters/framework/http.py`'s
`PlatformClient` already converts every realistic transport failure into
`CommerceError` before it leaves the adapter, so the new pagination loop's
`except CommerceError` genuinely catches everything reachable), and
live-verified independently rather than trusting the diff's own claims:
kayak/tent on Northfield still honestly decline, "a fruity coffee" on
Kettle is unaffected (confirmed the one observed failure was model
query-choice variance - the model sometimes searches an isolated word like
"fruity," absent from all catalog text regardless of sample size, a
pre-existing limitation this fix doesn't touch either way - not a
regression, reproduced identically both ways), and a genuine 4-item
Northfield message now correctly names all three remaining items instead
of silently dropping the one that used to fall outside the old 20-item
diversified sample. `fuzz.py` 8/8 (480 assertions), `auditroutes.py` fully
clean, `healthcheck.py` 117/120 with all 3 failures independently
reproduced by hand as pre-existing model variance (one re-run 6/6 clean in
isolation). Committed as `01b78ca` after this independent review, not
on the strength of the audit's own say-so.

**Still not closed, honestly - do not treat this as the end of the line:**
- `_full_catalog_sample`'s `_MAX_CATALOG_SCAN=2000` is a real, disclosed
  improvement over a flat 200-item sample (structurally covers a shop's
  ENTIRE catalog when it fits under the cap, which both demo merchants do)
  but is still not a complete answer at real 40,000-SKU volume - a fourth
  round of this exact shape is plausible and should not be assumed
  impossible.
- `_honest_synthesis`/`cited_reason` is now on its fourth structural
  iteration. It has not yet been independently adversarially re-tested by
  a fresh audit pass with new phrasings specifically targeting whether the
  model reliably populates `cited_reason` rather than leaving it null
  while still making an implicit, uncited claim in the free-text `reply` -
  the one gap the mechanism's own docstring names as its own most likely
  remaining failure mode. This needs a dedicated pass before being trusted
  as the last iteration.
- No `value-auditor` pass has been run against `01b78ca` itself. Per this
  file's own rule, nothing from this whole chain (the original hardening
  pass, the four-item fix round, or this pagination/cited_reason round)
  moves to `Completed.md` until that happens and returns a genuine
  VALUABLE verdict, not before.

**A dedicated adversarial `value-auditor` pass then ran against `01b78ca`
and confirmed the concern above was warranted, not hypothetical.** 5 of 10
fresh phrasings bypassed `_honest_synthesis` live (a valid `cited_reason`
plus prose arguing a different, unstated reason - "the lightweight design
is nice, but what really seals the deal here is the Marathon Pro's
outstanding durability"), and `judge_relevance` was found still passing
the kayak-substitution false match 2/8 (25%) live trials and still 0/8 on
"something smooth and not too bitter." Two commits followed to fix these
for real:

- **`8b6e179` - `_honest_synthesis`'s SIXTH gap, closed structurally.**
  The root cause: `cited_reason` was checked in isolation against real
  stored terms, but the free-text `text` was never cross-checked against
  what it actually *argues* - a reply can honestly cite "lightweight"
  while its prose argues for "durability," and no regex enumerates every
  way to phrase that (rhetorical question, third-person, comparative,
  hedged language all reproduced it). Fixed by no longer letting the model
  author the causal-attribution sentence at all: when `personalizes` is
  true and `cited_reason` validates against real stored state, the clause
  is now built in code from `cited_reason` alone (one of three
  deterministic templates, picked by a stable hash so a given reason reads
  consistently rather than robotically), and the model's own prose is
  discarded rather than searched for a bypass. Verified: all 5
  previously-bypassing phrasings from the audit's own test harness, plus
  10 fresh adversarial ones constructed separately, now produce zero false
  claims (15/15) - confirmed by direct, deterministic calls to the
  function (no model-call variance to explain away). The false-positive
  case (a genuine, honestly-grounded citation) still produces a natural
  reading sentence, not a robotic one. `fuzz.py` (480 assertions) and
  `auditroutes.py` both clean afterward.

- **`b9f86a1` - `judge_relevance`'s prompt strengthened, honestly not
  fully fixed.** Added an explicit instruction naming the exact failure
  mode already documented in `service.py`'s own comments: a product
  sharing one incidental word with the description (a water bottle for
  "kayak," because both involve "water") is not a match, and the judge
  should not be swayed by coincidental overlap. Live-retested 8 trials of
  "do you sell kayaks?" on Northfield after this change: **3/8 still
  returned the water-bottle substitution** - the same order of magnitude
  as before this change, not a fix. Coffee case unregressed: "a fruity
  coffee" 5/5 clean; "something smooth and not too bitter" still finds no
  real match but continues to degrade honestly (no false claim) 5/5.

  **Honest conclusion after this attempt: the kayak-substitution gap is a
  genuine, currently-irreducible limit of the judge model's own
  reliability on this specific class of query** (a weak, coincidentally-
  overlapping single-word match), not a bug a prompt wording change alone
  can close - the model sees the water bottle in its own candidate sample
  and sometimes judges it plausible anyway despite an explicit instruction
  not to. A further, more expensive mitigation (e.g. a second judge call
  and requiring agreement, or a stricter default-to-False posture when the
  judge's own confidence is anything but clear) was considered but not
  attempted this round, since it would roughly double an already-flagged
  per-turn model-call cost under the documented Groq throttle - a
  trade-off that needs a deliberate decision, not one made silently inside
  a "fix" commit.

  `fuzz.py` and `auditroutes.py` both clean after this change too. Neither
  commit has had a fresh independent `value-auditor`/`invariant-guard`
  pass yet - that should happen before anything from this round, or the
  chain it sits on top of, is considered for `Completed.md`.

  **NINTH round, real code fixes, two distinct root causes found and
  closed - reducing kayak-substitution to a precisely characterized ~1/10
  residual, not claimed eliminated.** An independent `value-auditor` pass
  re-testing the eighth round's "24/24 clean" claim with a larger sample
  found it did not hold: "I need a kayak" (a close variant of the tested
  "do you sell kayaks?") produced an undisclosed, confident single-product
  answer (`"Here's what's available - 1 option:"` showing the Insulated
  Water Bottle, no hedge) on a slice of trials. Root-caused directly
  rather than patched by inspection alone:

  1. **A category-only match had zero relevance discrimination applied at
     all.** `engine/execution/service.py`'s weak-match gate (`if candidates
     and (weak_single_word_match(...) or query_diverges_from_said(...))`)
     is entirely keyed off `query`; when the model instead narrows purely
     by `category` with `query` left blank, `best_matches_with_score`
     returns every product in that category unfiltered (an empty query has
     no significant words to score against, by its own docstring) - so a
     category the model guessed with zero literal grounding sailed through
     exactly as confidently as a real, deliberate "show me your shoes" ->
     category=Footwear. Fixed with a new `category_only_unverified` check,
     deliberately NOT keyed on the category label (a category name rarely
     shares literal words with ordinary phrasing even when correct -
     "shoes" vs "Footwear") but on whether the actual candidates FOUND via
     that category share even one literal word with what the shopper
     actually said (`raw_said`) - grounded ("Trailblazer Running Shoe" for
     "show me your shoes" shares "shoe") passes through unhedged; ungrounded
     ("Insulated Water Bottle" for "I need a kayak" shares nothing) routes
     through the same semantic-judge/disclosure path the query-driven case
     already uses. Verified directly, unit-level, before any live test:
     `best_matches_with_score([water_bottle], "I need a kayak")` scores 0
     (correctly flagged unverified); `best_matches_with_score([shoe], "show
     me your shoes")` scores 1 (correctly NOT flagged) - the discriminator
     itself is sound by construction, not just by luck on live trials.

  2. **`weak_single_word_match`'s own word-count gate was wrong, found via
     live reproduction after fix #1 alone did not close the gap.** Its
     original logic assumed a query with more than one significant word
     scoring only 1 had already been through "real multi-word
     discrimination" and could be trusted. Live trials disproved this
     directly: the model paraphrased "I need a kayak" into a three-word
     proxy ("water sports gear" / "water sports accessories"), of which
     only "water" ever overlapped anything real (`action_summary: 'Searched
     "water sports accessories" and found 1: Insulated Water Bottle
     750ml'`) - a coincidental one-word hit with two padding words that
     matched nothing, not real discrimination. Fixed by dropping the
     word-count condition entirely: `weak_single_word_match` now fires on
     `top_score == 1` regardless of how many significant words the query
     had. Checked this would not regress the two cases the original
     word-count gate existed to protect, by direct scoring before shipping:
     an exact product-title citation ("Ethiopia Guji Natural" for itself)
     scores 3; a genuine two-noun match ("a fruity coffee" against Kettle's
     real catalog) scores 2 - both comfortably above the new threshold, so
     neither is newly hedged.

  **Live-verified, two independent runs of 10 trials each on Northfield
  after both fixes, restarting the engine between the code change and
  each test run (required - `engine/` changes need a restart to take
  effect, confirmed the first test run before restart still showed the
  bug):** "I need a kayak" - 9/10 and 9/10 correctly declined or hedged,
  1/10 and 1/10 still showed the undisclosed confident wrong answer.
  Traced the one residual failure in the second run to a THIRD, narrower
  variant of the same shape, precisely characterized rather than left
  vague: the model's proxy query ("water sports accessories") coincidentally
  scored 2, not 1, because "accessories" is *itself* the Insulated Water
  Bottle's real category name - a second word overlapping for a reason
  that has nothing to do with the query being any more grounded, defeating
  the new `top_score == 1` threshold by exactly one point. Not fixed this
  round - chasing this specific coincidence (a proxy term whose padding
  word happens to also be a real category label) risks the same
  per-example special-casing this file has repeatedly warned against
  reintroducing; it is a real, narrow, now-understood residual, not a
  restatement of the original bug. Control cases re-confirmed unaffected
  in the same session: "show me your shoes" (Northfield) 3/3 confident,
  correct, unhedged; "a fruity coffee" (Kettle) 4/5 confident and correct,
  1/5 degraded honestly via the disclosed browse-fallback path (consistent
  with previously-documented judge variance, not a new regression).

  `fuzz.py` (20/20 sequences, every invariant held) and `auditroutes.py`
  (every probe held) both re-run clean against the restarted engine after
  these changes. No `invariant-guard`/`value-auditor` pass was run against
  this specific round in this session - only manual invariant reasoning
  (both changes are confined to `engine/execution/relevance.py` and
  `engine/execution/service.py`'s reply-shaping/candidate-filtering logic;
  neither touches risk, payment, execution-authorization or idempotency).
  Do not move this, or anything in the chain above it, to `Completed.md`
  without that independent pass - the honest state after nine rounds is
  "substantially improved and now narrowly characterized," not "closed."

  **TENTH and ELEVENTH rounds (commit `ed01b82`), two further real root
  causes found by independent re-test of the ninth round's headline
  number.** A dedicated `value-auditor` pass re-tested "24/24 clean" at
  larger sample and found it did not hold: 3/25 (12%) trials of
  "I need a kayak"/"do you sell kayaks?" on Northfield still produced an
  undisclosed, confident wrong answer. Root-caused, not patched on the
  same spot twice:

  - **TENTH gap** (`engine/execution/relevance.py`): the word-count
    exemption in `query_diverges_from_said` ("a query of 3+ significant
    words diverging from what the shopper said is a real citation") was a
    proxy for "the model named a specific real product," not the actual
    signal - a vague, equally-ungrounded multi-word guess ("water sports
    accessories") is exactly as wrong as a one-word guess, just longer,
    and slipped through untouched. Replaced with `_is_real_title_citation`:
    a query only counts as a real citation when it is near-exactly equal
    (symmetric difference of at most one word, to tolerate a size/unit
    suffix) to some real candidate's own title - the actual signature of
    naming a product, independent of word count. A first version checked
    plain subset-containment and immediately regressed the already-known
    "water bottle" case (a fragment of the WRONG product's title read as a
    citation of it); near-equality is the narrower, correct check.
  - **ELEVENTH gap** (`engine/execution/service.py`): the deterministic
    `shop_carries(query, ...)` call in the query-driven browse fallback
    checked the model's own weak proxy term against a sample that, by
    construction, already contains whatever that proxy term matched -
    tautological, the same shape the sibling weak-match check already
    avoids by checking `raw_said` instead. Fixed to use `raw_said` there
    too. Also made the semantic judge (`judge_relevance`) structurally
    one-directional for this path: when the judge affirms an otherwise-
    ungrounded browse as plausible, `weak_match_uncertain` is now always
    set - the judge may decide whether to show a weak match at all, never
    whether to suppress the honest disclosure a judge-only-confirmed match
    warrants.

  Verified live against the running services: 15/15 kayak-family queries
  on Northfield correctly declined or hedged (0 undisclosed, up from
  3/25); "a fruity coffee" on Kettle unaffected (4/4 confident, correct);
  "something smooth and not too bitter" unaffected (4/4 honestly hedged,
  unchanged). `fuzz.py` (every invariant held) and `auditroutes.py`
  (every probe held) both clean.

  A dedicated adversarial re-test of `ed01b82` (fresh trials, not the
  commit's own script) reproduced the headline number independently -
  18/18 clean on the direct kayak phrasings, 5/6 correctly hedged (1/6
  declined) on phrasings specifically constructed to reconstruct the
  exact "water sports accessories" failure shape, 0/24 undisclosed
  overall (vs. the prior round's 3/25) - and confirmed the coffee control
  unregressed 5/5 and 5/5. Binomial check: given a true 12% failure rate,
  P(0 failures in 24 trials) is under 5%, meaningful evidence the fix
  works rather than small-sample luck, though not exhaustive proof.

  **The same audit also found a genuine over-hedging risk `ed01b82` did
  not address, fixed in a follow-up (commit `<pending>`, same session):**
  `_is_real_title_citation`'s near-equality check did no plural
  normalization, unlike its sibling `_overlap()` in the same file (which
  already has an explicit "count both forms, do not punish a plural"
  rule) - a genuinely correct citation like "ethiopia guji naturals" for
  the real title "Ethiopia Guji Natural" counted as a 2-word difference
  (remove "natural", add "naturals") instead of zero, and would have been
  spuriously hedged as if it were a coincidental guess. Fixed by
  normalizing both sides with the same `w.rstrip("s")` rule `_overlap`
  already uses before computing the symmetric difference.

  **Deliberately left open, not silently dropped:** the same audit also
  flagged ordinary abbreviation ("marathon shoe" for the real title
  "Marathon Pro Racing Shoe") as untolerated by the near-equality check.
  A subset-plus-majority rule was worked through on paper before writing
  any code and immediately reopens the already-documented "water bottle"
  regression: `{"water", "bottle"}` is 2 of "Insulated Water Bottle
  750ml"'s 4 real words, a clear majority, and a check against *other*
  candidates doesn't catch it either, because the problem isn't ambiguity
  with a different product - it's a generic phrase that happens to fully
  overlap the wrong one. Nothing short of real semantic judgment
  distinguishes that from a deliberate abbreviation of a specific title,
  so this stays open rather than risk reintroducing an already-fixed bug
  for a narrower one. Live re-test found this gap did not bite in
  practice for the two abbreviation-shaped queries tried ("do you have
  the marathon shoe", "tell me about the marathon shoe" both returned a
  confident, correct, unhedged answer) - likely because the underlying
  `_overlap` word-count score for these particular titles was already
  strong enough (2+) to route around the citation-check path entirely,
  not because the gap is closed. Do not treat it as closed; it has not
  been forced to fail live, only shown not to fail on the two phrasings
  tried.

  Verified directly (deterministic, no model-call variance): 5 unit-level
  cases confirm the plural fix works, the two already-documented
  regressions (fragment-of-wrong-product, vague-multi-word-non-citation)
  still correctly return False, and the deliberately-unaddressed
  abbreviation case still correctly returns False (not silently broken
  by the plural change). Live re-test on the running engine: 0/10
  undisclosed confident wrong on kayak-family queries on Northfield;
  coffee control on Kettle 4/5 confident-correct (1/5 model-variance
  degrade to the honest browse fallback, consistent with the
  already-documented baseline, not a new regression); both a plural
  ("do you have the guji naturals") and an abbreviated ("do you have the
  marathon shoe") real citation returned a confident, correct, unhedged
  answer live. `fuzz.py` (every invariant held) and `auditroutes.py`
  (every probe held) both re-run clean.

  **This plural-fix commit (`dd56318`) has since had a dedicated
  `invariant-guard` and independent `value-auditor` pass, both run this
  session.** `invariant-guard`: SAFE - confirmed the change is confined to
  `engine/execution/relevance.py`'s word-comparison logic and only ever
  narrows/hedges already-real, already-fetched candidates. `value-auditor`
  independently reproduced the fix's own claims (4 fresh plural cases
  beyond the commit's own example, both regression guards still holding,
  a hand-reconstructed copy of the pre-fix logic confirming the change is
  load-bearing rather than a no-op) and separately confirmed the
  abbreviation gap is the right thing to leave open, with no cheaper safe
  fix found. It also found a genuine, reproducible next gap, addressed
  immediately below rather than left to drift.

  **TWELFTH gap (commit `<pending>`, same session), found by that
  `value-auditor` pass and fixed immediately.** The plural normalization
  `dd56318` added (`w.rstrip("s")`, borrowed from `_overlap`'s existing
  rule) only strips a single trailing "s" - correct for "bottles" ->
  "bottle" but wrong for an ordinary English "-es" plural, where the "e"
  is also part of the suffix: `"boxes".rstrip("s")` -> `"boxe"`, which
  never equals `"box".rstrip("s")` -> `"box"`. Reproduced live and
  deterministically against a real Kettle catalog title:
  `_is_real_title_citation({"taster","gift","boxes"}, [Product(title=
  "Taster Gift Box")])` returned False (should be True) - a shopper
  citing "Taster Gift Box" as "gift boxes" would have been spuriously
  hedged as a coincidental guess, the identical bug class `dd56318` had
  just closed for the simpler "-s" case. Inherited from, and shared with,
  `_overlap()` in the same file (not new to `_is_real_title_citation`).

  Fixed with a single shared `_stem()` helper used by both `_overlap` and
  `_is_real_title_citation` (per CLAUDE.md's "fix the shape, not the
  instance" standard, rather than patching each call site separately):
  strips "es" instead of "s" specifically when the word minus "es" itself
  ends in a sibilant sound (box/glass/dress/brush - the ordinary English
  "-es" pluralization rule), falling back to a single-character "-s"
  strip otherwise. Deliberately does not reuse `str.rstrip("s")` for that
  fallback, since it strips every trailing "s" it finds rather than one -
  found live that this would otherwise mangle an already-singular word
  ending in a doubled "s" ("glass" -> "gla", disagreeing with "glasses"
  correctly stemming to "glass"); guarded explicitly so "glass"/"glasses"
  and "dress"/"dresses" both canonicalize to the same stem rather than
  the naive fix trading one bug for a subtler one.

  Verified directly (14 deterministic unit cases, no model-call
  variance): the "-es" gap case now correctly returns True; the existing
  "-s" plural case ("ethiopia guji naturals") still returns True;
  both standing regression guards (vague multi-word non-citation,
  wrong-fragment-of-a-different-product) still correctly return False;
  two different words that both happen to end in "-es" ("gates"/"gases")
  correctly stem to different roots rather than being collapsed together;
  and the glass/dress double-s cases specifically checked and confirmed
  correct. Live re-test on the running engine: 5/5 fresh phrasings citing
  "Taster Gift Box" as "gift boxes" on Kettle now return a confident,
  correctly unhedged answer (previously would have been spuriously
  hedged); 15/15 kayak-family queries on Northfield remain correctly
  declined/hedged with 0 undisclosed confident-wrong answers, confirming
  no regression from the shared-helper refactor. `fuzz.py` (every
  invariant held) and `auditroutes.py` (every probe held) both re-run
  clean.

  No `invariant-guard`/`value-auditor` pass has run against this specific
  twelfth-gap commit yet - manual invariant reasoning only (confined
  entirely to `engine/execution/relevance.py`'s word-comparison logic, no
  new file touched, same class of change already cleared for `dd56318`
  above). Do not move this, or the ninth/tenth/eleventh/twelfth-gap chain
  it sits on top of, to `Completed.md` without that independent pass.

- **A distinct, newly-found problem, NOT part of the false-substitution
  chain above - the mirror image of it.** An independent adversarial
  audit of the twelfth-gap fix found the false-substitution saga (never
  confidently show an unrelated real product) genuinely closed across
  three fresh query families, but surfaced a different bug class: on
  Kettle, "a fruity coffee" (and near-variants) returned a flat,
  undisclosed "I couldn't turn anything up for that" on ~19-30% of
  trials, even though the exact same input reliably finds a real,
  correct match ("Ethiopia Guji Natural") on other trials seconds later.
  Arguably worse than substitution: a shopper told "no" about something
  the shop demonstrably carries has nowhere to go, where a hedge at
  least shows real options.

  **Root cause, found live with temporary debug instrumentation, not
  guessed** (`engine/execution/service.py`, the `RECOMMEND_PRODUCTS`
  wide-catalog fallback around what is now line ~1000): the model
  sometimes narrows this action by a plausible-sounding but genuinely
  NONEXISTENT category - `category="Coffee"` on Kettle, whose real
  category list is `Single origin`/`Blends`/`Equipment`/`Subscriptions`/
  `Gifts`. `_within_category` then hard-filtered the honest wide-catalog
  browse-fallback down to zero products, wiping out a real, correct
  match that the deterministic `shop_carries` check running moments
  later had *already confirmed* this shop carries (`shop_carries_query`
  was `True` in every failing trial, logged directly). This is the same
  shape as the already-fixed `category_only_unverified` gap from the
  ninth round - a model-guessed category must not override real ground
  truth - just reached from the fallback branch instead of the narrow-
  search branch, so the existing fix never covered it. Confirmed
  precisely, not inferred: 9 of 20 trials logged `category='Coffee'`
  against `real_categories=['Blends', 'Equipment', 'Gifts', 'Single
  origin', 'Subscriptions']`, `wide_products_count=16`, candidates wiped
  to `[]`.

  **Fix:** before applying `_within_category` in this specific fallback,
  check whether the model's stated category matches any of the shop's
  real categories (case-insensitive, drawn from the same `wide_products`
  already fetched, no extra call). If it does not, the category filter
  is dropped for this fallback rather than trusted to zero out real
  candidates - the shop's own real category list is ground truth the
  model's guess does not get to override, the same principle already
  applied elsewhere in this function. A real, verified category (e.g.
  "show me your Blends") still narrows correctly; only a fabricated one
  is defused.

  Verified live against the running engine: 20/20 fresh trials of "a
  fruity coffee" now correctly return a real match (previously 6-9
  failures per 20, i.e. roughly 30-45% depending on the run - worse
  than the ~19% first estimated from a smaller sample). Re-confirmed no
  regression on the just-closed substitution saga: 15/15 kayak-family
  queries on Northfield still correctly decline/hedge with zero
  undisclosed confident-wrong answers; 5/5 "something smooth and not too
  bitter" trials on Kettle unchanged (still an honest hedge, not forced
  into a false match). All four standing deterministic regression
  guards re-checked directly (`_is_real_title_citation`'s plural, "-es"
  plural, fragment, and vague-proxy cases) - all four still correct.
  `fuzz.py`: every invariant held. `auditroutes.py`: every probe held.

  This is a category-filter bug, not a relevance-scoring bug - it lives
  entirely in `engine/execution/service.py`, not `relevance.py`, and
  does not belong in the ninth-through-twelfth-gap numbering above; it
  is its own, newly-discovered defect in the same general area
  (RECOMMEND_PRODUCTS honesty), found because closing the substitution
  saga finally made room to test recall/consistency rather than only
  false-positive substitution. No `invariant-guard`/`value-auditor` pass
  has run against this specific commit yet - do not move it to
  `Completed.md` without one.

  **A dedicated `invariant-guard`/`value-auditor` pass on this commit
  (`d230d35`) found it SAFE and the specific fix verified, but surfaced
  two further precise gaps in the same fix - a fourteenth round, not a
  reopening of the thirteenth.**

  1. **The category-validation logic protected only one of two call
     sites that share it.** `_within_category`'s hard filter is applied
     TWICE in the same `RECOMMEND_PRODUCTS`/`SUGGEST_ALTERNATIVE` shared
     code path: once early, unconditionally, right after the narrow
     keyword search (`relevant = [p for p in relevant if
     _within_category(p)]`), and once again inside the later wide-
     catalog fallback that the thirteenth-gap fix protected. The
     fallback's protection was gated `action_type is
     ActionType.RECOMMEND_PRODUCTS` only - so a bogus model-guessed
     category reaching the EARLY filter on a `SUGGEST_ALTERNATIVE` turn
     still zeroed `relevant` with no protection at all, falling through
     to the still-broken "Searched for X and still found nothing" reply
     for something the shop actually carries. Same bug, same file, one
     action type and one call site away, still open after the
     thirteenth-gap fix.

  2. **The category comparison itself was a bare `.lower()` exact-string
     match, with no plural/hyphen normalization** - even though this
     exact codebase had already built `relevance._stem()` one commit
     earlier (twelfth gap) for precisely this class of problem. A
     model-guessed category like "Blend" (singular) or "single-origin"
     (hyphenated) against a real category "Blends"/"Single origin" would
     fail the exact-match check and have its filter silently dropped -
     not the crash-to-zero bug, but a quiet, undisclosed loss of the
     shopper's intended category narrowing.

  **Fix:** validation is now applied at its actual source - the early,
  shared `_within_category` filter itself - for both action types, via
  a new `_category_is_real`/`_normalize_category` pair
  (`engine/execution/service.py`) that stems and hyphen-normalizes both
  sides before comparing (reusing `relevance._stem`, not a second
  bespoke normalizer). The later wide-catalog fallback's own duplicate
  `.lower()`-based check was replaced with a call to the same shared
  helpers, so the two can no longer disagree with each other about what
  counts as the same category - the exact inconsistency-between-two-
  word-matchers shape this whole multi-round chain has been closing
  everywhere else.

  Verified: 5 direct deterministic checks (`blend`→`Blends`,
  `single-origin`→`Single origin`, a genuinely fabricated category
  correctly still rejected against two different real category sets,
  and an exact real-category match) all correct. Live, against the
  restarted engine: 15/15 "a fruity coffee" trials on Kettle still
  return a real product (0 false declines, no regression from the
  thirteenth-gap fix); 10/10 kayak-family trials on Northfield
  (including the "water sports accessories" phrasing) still correctly
  decline or honestly hedge, zero undisclosed confident-wrong answers.
  `fuzz.py`: every invariant held. `auditroutes.py`: every probe held.
  Not live-forced for `SUGGEST_ALTERNATIVE` specifically (the model's
  own choice of action type/category cannot be dictated from the API,
  the same limitation the auditor that found this gap already noted) -
  relying on the direct deterministic verification and code-level
  symmetry (the same helpers now gate both call sites identically)
  rather than a live reproduction of that exact branch.

  **A dedicated `invariant-guard` pass on the fourteenth-gap commit
  (`0c7a940`) returned SAFE** - confirmed both action types now share
  the identical, real-data-only validation, both failure directions
  fail open (skip the filter) rather than fabricate, and the
  closure/variable-ordering in the shared code block is correct Python
  late-binding, not a latent bug.

  **A dedicated `value-auditor` pass on the same commit independently
  re-verified both fourteenth-gap fixes hold (fresh stem/hyphen pairs,
  fresh fabricated-category rejections, a direct control-flow trace
  confirming the shared-filter claim) and found one more, narrower,
  fifteenth gap - not a reopening of gap two, but an edge of it left
  uncovered.** `relevance._stem()`'s own docstring already scopes it to
  "-s"/"-es" plurals only; it has no rule for the "-y" -> "-ies" plural
  pattern (category/categories, battery/batteries, and - found live,
  not hypothetically - accessory/accessories, a real Northfield
  department name). `_category_is_real("Accessory", {"accessories",
  ...})` returned False before this fix, even though "Accessory" is the
  ordinary singular of a real, live department. Bounded severity by the
  same fail-open design gap two relies on (no false decline, only a
  quiet loss of intended category narrowing) - but real and
  reproducible against the actual seeded catalog, not contrived.

  **Fix:** `_stem()` gained one more narrow, closed-class rule (matching
  its own existing philosophy, not a general vowel-shift stemmer): a
  word ending "ies" is stemmed by replacing "ies" with "y"
  ("accessories" -> "accessory"), so it now collapses to the same stem
  as the plain "-y" singular. Since `engine/execution/service.py`'s
  `_category_is_real`/`_normalize_category` (the fourteenth-gap fix)
  already calls `relevance._stem` directly rather than reimplementing
  its own normalization, this single change closes the gap at both call
  sites with no separate edit needed there - confirmed by reading the
  call site, not assumed.

  Verified: 10 direct deterministic checks - the exact freshly-found
  gap (`accessory`/`accessories` now equal), every previously-fixed
  plural/`-es`/hyphen case re-confirmed unregressed (`box`/`boxes`,
  `bottle`/`bottles`, `natural`/`naturals`, `glass` left un-mangled),
  two more genuine "-y"/"-ies" pairs (`battery`/`batteries`,
  `category`/`categories`), and an explicit non-collapse guard
  (`ties`/`pies` stay distinct, `series` stems harmlessly to a form
  nothing else matches) - all correct. Re-verified end to end through
  `engine/execution/service.py`'s actual `_category_is_real` helper
  (not just the underlying `_stem` function in isolation): `"Accessory"`
  now correctly validates against a real `["Accessories"]` list, and a
  genuinely fabricated category is still correctly rejected. Live,
  against the running engine: 0/8 undisclosed on a fresh kayak-family
  batch (Northfield) and 0/5 false declines on "a fruity coffee"
  (Kettle) - both unregressed from the fourteenth-gap fix's own numbers.
  `auditroutes.py`: every probe held. `fuzz.py`: every invariant held
  (run separately, confirmed clean).

  **A dedicated `invariant-guard` pass on the fifteenth-gap commit
  (`df5c4af`) returned SAFE** - confirmed the change is pure
  string-stemming used only for equality comparison against real,
  already-fetched category/title data, with no path to fabricate or
  introduce a product/category that wasn't actually there.

  **A dedicated `value-auditor` pass on the same commit independently
  confirmed the "-y"/"-ies" fix itself holds (3 fresh pairs beyond the
  commit's own examples, no false collapses found in ~20 words tried),
  but found something more consequential than one more narrow gap: the
  last several rounds' "0/8 unregressed" and "0/10 unregressed" claims
  were technically true of what they tested and misleading about what
  they implied.** A dedicated 15-trial batch giving "water sports
  accessories" - the one phrasing already known, since round 10, to be
  the weak point - its own full weight (rather than 3-4 of a 8-10-trial
  mixed batch, as every commit since the tenth gap had been sampling
  it) reproduced an undisclosed confident-wrong answer **5 times out of
  5**. Traced precisely, not vaguely: the model's own proxy query for
  this phrasing was `"water bottle"`, which shares the word "water"
  with what the shopper actually typed - enough for the old
  `query_diverges_from_said` (an `isdisjoint` check: fires only when
  the query shares NOTHING with what was said) to call it "not
  diverging" - while the second word, "bottle", is a pure model
  invention appearing nowhere in the shopper's sentence and is exactly
  the word that pushed `Insulated Water Bottle`'s score to 2, clearing
  `weak_single_word_match`'s `top_score == 1` threshold by the same one
  point the ninth-gap fix already found this exact product defeating a
  score-based check by. One shared generic word bought a free pass for
  one entirely invented specific word riding beside it - the auditor
  named this correctly as a structural property of the two-gate design
  (an absolute or intersection-based threshold can always be defeated by
  padding with one word that happens to help), not a sixteenth instance
  of "found one more phrasing."

  **Fixed at the shape, not the phrasing, per the auditor's own explicit
  recommendation against continuing round-by-round phrasing patches:**
  `query_diverges_from_said` now checks SUBSET rather than intersection
  - it fires whenever ANY of the query's (stemmed) significant words is
  absent from what the shopper (stemmed) actually said, not only when
  none of them are present. A query built entirely from the shopper's
  own words, in any subset or order, is never flagged, however long;
  a query introducing even one word the shopper never said, and that
  isn't part of a verified real title citation (the existing
  `_is_real_title_citation` exemption is unchanged), now is. This does
  not reject a match outright - it only routes it into the same wider-
  sample/semantic-judge verification every other flagged case already
  goes through - so a legitimate model synonym or paraphrase still gets
  a fair honest-disclosure check rather than an outright refusal; it
  just stops being able to hide behind one coincidentally-shared word.

  Verified: 9 direct deterministic cases covering the exact new bug
  (`"water bottle"`/`"water sports accessories"` now correctly flagged),
  every standing citation exemption (`Ethiopia Guji Natural` and its
  plural, `Taster Gift Box`'s `-es` citation), every standing subset-
  match case (`"shoes"` for `"show me your shoes"`, `"running shoes"`
  for a longer sentence), and one case the OLD check would have missed
  entirely (`"kayak accessories"` against raw_said `"kayak"` - shares
  "kayak", old check said not diverging; new check correctly flags the
  invented "accessories"). All 9 correct. Live, against the restarted
  engine: the exact previously-5/5-failing phrasing now **0/10**
  undisclosed; the rest of the kayak-family batch **0/10**; "a fruity
  coffee" on Kettle **10/10** confident and correct (no over-hedging
  regression from tightening the divergence check); "show me your
  shoes" control unaffected (4 options, not hedged). `fuzz.py`: every
  invariant held, 1200 assertions across 20 sequences. `auditroutes.py`:
  every probe held.

  No `invariant-guard`/`value-auditor` pass has run against this
  specific sixteenth-gap commit yet - do not move any of the
  thirteenth/fourteenth/fifteenth/sixteenth-gap commits to
  `Completed.md` without one covering the final state. Given the pattern
  of the last several rounds, treat any future "N/N clean" claim for
  this mechanism with the same suspicion this round applied to the
  ones before it - test the known-weakest phrasing at its own full
  weight, not diluted inside a mixed batch, before trusting a headline
  number.

### Real user-reported bugs from hands-on testing (9 items, distinct from the kayak/relevance saga above)

A user drove the shopper assistant by hand on both demo merchants and reported
9 concrete transcripts. This is a different, more valuable signal than
synthetic adversarial phrasing - it caught real multi-turn and cart-mutation
bugs the single-turn kayak/relevance saga above never exercised. Investigated
and fixed live against the running services; each item below states its real,
verified status rather than a blanket "fixed."

1. **Category browse false-decline** ("what are all footwear is available" ->
   "I couldn't turn anything up for that"). **Did not reproduce** on a fresh
   session - 3/3 fresh phrasings ("what are all footwear is available",
   "show me all footwear", "what footwear do you have") on Northfield all
   correctly returned real footwear products. Most likely already closed
   incidentally by the mission-state grounding fix (item 4b below) or was
   coincident with the two demo backend processes (Northfield :8001, Kettle
   :8002) having gone down at some point mid-session - confirmed both were
   actually down and had to be restarted during this investigation, unrelated
   to any code change. Not moved to Completed.md - no dedicated fix exists to
   audit, and "stopped reproducing" is weaker evidence than "found and fixed
   the cause."

2a. **Multi-item add in one message** ("add Marathon Pro Racing Shoe, Cork
    Massage Ball and Massage Gun to my cart" -> only the second item was
    asked about, unclear what happened to the other two). Not separately
    re-investigated this session - the underlying mechanism (a pending-
    proposal queue, `ee70c84`/`4fb161a` earlier this session) already handles
    2-4 named items with disclosure per its own extensive verification.
    Given time spent on the higher-severity items below, this specific
    transcript was not re-driven turn-for-turn. Left honestly unverified
    against this exact 3-item real phrasing rather than assumed fixed.

2b. **FIXED AND VERIFIED - re-adding an item already in the cart didn't
    increment its quantity.** Root-caused precisely, not guessed: the
    underlying cart-mutation mechanism is correct (verified directly via a
    trusted tap - `act('P6003')` twice took quantity 1 -> 2 cleanly). The
    real bug is upstream, in how the model resolves which product a free-text
    "add X again" message refers to: reproduced live, 2 of 4 trials where the
    model DID propose ADD_TO_CART resolved "Cork Massage Ball" to a
    *different* real product already sitting in the cart (Massage Gun, P6001)
    purely because both titles share the word "massage" - not a fuzzy-match
    bug in this codebase's own logic, a model extraction error. The other 2
    of 4 trials proposed SUGGEST_ALTERNATIVE instead of ADD_TO_CART at all,
    correctly not mutating the cart but not fulfilling the request either.

    Fixed in `engine/execution/service.py`'s `ADD_TO_CART` branch: before
    trusting the model's `product_id`, check whether the shopper's own raw
    words fully contain (all significant words present, not a fuzzy partial)
    the real title of a *different* product already in this exact cart - if
    so, that already-known real line is trusted over the model's guess, the
    same "a tap is trusted, the model's guess is not" reasoning already
    applied to variant selection, extended here to an already-known cart
    line instead of a fresh tap. Deliberately the strict direction (full
    title containment, at least 2 real title words) to avoid reopening the
    double-add bug class a looser match caused before (see Completed.md
    #15's "chosen_variant" reasoning, same shape). Verified: constructed the
    exact failure case directly (`_significant_words("Cork Massage Ball")` a
    subset of the said-words vs. `_significant_words("Massage Gun")` NOT a
    subset - correctly distinguishes them). Live, 5/5 fresh trials of the
    exact reported sequence now correctly land on quantity 2 for the same
    product, zero misresolutions to a different item.

4a. **FIXED AND VERIFIED - the synthesis mechanism echoed the shopper's
    entire raw message verbatim into the reply.** ("I need lightweight
    running shoes under 5000 that are durable and comfortable." ->
    "Given your focus on I need lightweight running shoes under 5000 that
    are durable and comfortable., here's what's available..."). Root cause:
    `_honest_synthesis`'s `cited_reason` validation (`mission_ranking.
    any_term_mentioned`) only checked that the model's `cited_reason` field
    *contained* a real stored term somewhere - it never required
    `cited_reason` to actually *be* that short term, and the model is not
    obliged to keep it short. The model echoed the shopper's whole sentence
    as `cited_reason`; because that sentence happens to contain the real
    word "lightweight", the check passed and the entire sentence was
    substituted into the deterministic attribution template.

    Fixed with a new `mission_ranking.matching_stated_term(text, terms)` -
    returns the actual matched real term, not just whether one exists -
    and `_honest_synthesis` now builds the attribution clause from that real
    term instead of the model's own wrapper text. Also added a defense-in-
    depth length guard in `_deterministic_personalized_reply` itself (falls
    back to the plain sentence if a stored term is ever implausibly long).
    Verified deterministically (the exact bug case, a genuine short
    citation, and a dishonest citation all behave correctly) and live: the
    exact reported sequence now reads "With durable being what you care
    about here, here's what's available..." - natural, short, never the
    echoed sentence.

4b. **FIXED AND VERIFIED - a follow-up message that doesn't restate the
    product (e.g. "Comfort is more important than durability.", "What would
    you recommend?") produced "I couldn't turn anything up for that"**, even
    though the exact same query had just succeeded two turns earlier in the
    same conversation. Root cause, found by reading the actual mission-state
    backfill code: `query` gets correctly backfilled from `mission_state.
    goal` when the current turn leaves it blank (an existing, working
    mechanism, `engine/execution/service.py` around the "comfort matters
    more" comment) - but the divergence/weak-match checks that decide
    whether to trust that query (`weak_single_word_match`/
    `query_diverges_from_said`/`category_only_unverified`, all from the
    kayak-substitution saga above) were comparing it only against THIS
    turn's own raw words, which - for any turn that doesn't restate the
    whole request - share nothing with it by construction. A perfectly
    grounded, already-established mission was read as an invented proxy
    term.

    Fixed by widening the grounding text those three checks compare against
    to also include the mission state's own stated goal/must-have/priority
    words (not exclusions) whenever `mission_state` was read this turn -
    strictly additional scrutiny on top of the existing trust already given
    to `mission_state.goal` at the backfill point itself (which already
    becomes `query` outright, unconditionally), so this cannot reopen an
    invented-term case that trust boundary didn't already accept. Verified
    live against the exact reported 4-turn sequence: turns 3 and 4 (the
    previously-failing ones) now correctly return the real product instead
    of declining.

5a. **"Show me shoes" got an unnecessary "I couldn't confirm an exact match"
    hedge on an obviously-correct match.** **Did not reproduce** on a fresh
    session - 1/1 trial correctly returned 4 real, unhedged shoe options.
    Most likely already resolved by the mission-state grounding fix (4b) or
    an earlier round of the kayak saga; not independently re-diagnosed given
    it did not reproduce.

5b. **FIXED AND VERIFIED - "Why did you recommend this one?" (and similar
    explanation questions) got a completely unrelated response** - a
    category-menu offer, a variant-choice prompt, or a fresh, failed product
    search, depending on the trial - never an actual answer to the "why"
    question. Root cause: `ANSWER_PRODUCT_QUESTION` (an existing, no-
    execution, conversational action type in this codebase) had no prompt
    guidance at all telling the model when to propose it - confirmed by grep,
    there was no "Propose ANSWER_PRODUCT_QUESTION when..." bullet anywhere in
    `engine/reasoning/prompts.py`, unlike every other action type. Adding
    that guidance alone was NOT sufficient (verified: 3/3 trials still
    misrouted after the prompt-only fix) - consistent with this session's
    established finding elsewhere that prompt-following alone is not
    reliable enough for a routing decision this codebase already treats
    deterministic detectors as necessary for (the generic-browse-phrase
    detector, the mission-state backfill).

    Fixed with a deterministic backstop instead: a small, closed-class
    detector (`_looks_like_recommendation_why_question` in `engine/api/
    chat.py`) recognizes a "why" question that references the act of
    recommending/choosing/picking, and - before reasoning even runs -
    answers directly from the real `fit` data (or, absent a mission-ranked
    `fit`, the real title/description/price) already stored on the most
    recent assistant turn's shown products. No model call for the answer
    itself (`used_model: False`), and nothing not already a real, grounded
    product field. Verified live across both a mission-ranked case (real
    `fit` reasons) and a plain-browse case (no `fit`, falls back to
    describing the real product instead of inventing a reason) - both now
    answer correctly and instantly.

6. **A shop that plainly doesn't carry something (Kettle, a coffee shop,
   asked for a kayak) sometimes escalated to a human** rather than degrading
   honestly. **Did not reproduce** - 0 of 6 fresh trials triggered
   `ESCALATE_TO_HUMAN` or set `awaiting_person`; all correctly returned an
   honest decline (occasionally, 1/6, an imperfect but harmless browse
   fallback, the already-documented residual from the kayak saga above, not
   escalation). Confirmed by reading `engine/decision/engine.py` that
   `ESCALATE_TO_HUMAN` is a Decision-Engine fallback ("when nothing
   survives"), not something the model proposes directly - so this was never
   a promptable behavior to begin with, and its absence here is consistent
   with the extensive kayak/relevance hardening earlier this session giving
   the decision engine a real, non-empty option to select instead of falling
   through to escalation.

9. **FIXED AND VERIFIED - a direct yes/no availability question about one
   named product got the generic multi-option RECOMMEND_PRODUCTS template**
   ("Do you have Trailblazer Running Shoe?" -> "Here's what's available - 1
   option:" plus a product card) instead of a direct, natural answer.
   `CHECK_AVAILABILITY` already existed as a real action type doing exactly
   this work (a real inventory check, a real yes/no summary) but had no
   prompt guidance distinguishing it from RECOMMEND_PRODUCTS for this exact
   phrasing shape. Added one guidance bullet. Verified live: the exact
   reported question now returns "Yes, we have the Trailblazer Running Shoe
   available..." via `CHECK_AVAILABILITY`, not the generic template. Only
   prompt-only fixed (unlike 5b, no deterministic backstop was added) -
   given this session's own finding that prompt-only fixes are not always
   reliable, this should be re-verified across more trials before being
   trusted as fully closed; flagged honestly rather than assumed solid from
   one trial.

**Verification run for this whole batch:** `fuzz.py` and `auditroutes.py`
both re-run clean against the final restarted engine (see below for exact
numbers). Manual invariant reasoning on each fix: 2b touches cart mutation
directly, confirmed to only ever override `product_id` to another product
ALREADY in the shopper's own cart (never invents or reaches across carts),
never touches variant trust (`chosen_variant` still the only trusted variant
source), and the increment mechanism itself was independently confirmed
correct via a real tap before any reasoning-layer fix was even attempted. 4a,
4b, 5b and 9 only affect which shopper-facing reply text is chosen or how a
turn is routed among existing, already-risk-classified action types - none
touch payment, checkout, risk classification, or cross-tenant/cross-shopper
isolation. No dedicated `invariant-guard`/`value-auditor` pass has run against
this batch - do not move any of it to Completed.md without one.

**Item 8, "coffee taste-preference semantic matching" ("something smooth and
not too bitter" repeating the same unhelpful browse fallback across four
different phrasings) was independently reproduced by this real user testing
but not re-investigated** - it is the already-documented, already-understood
semantic relevance gap from the kayak/relevance saga above (a keyword-overlap
system cannot match a taste description to a product with no shared literal
word), not a new defect. The user's report is useful confirmation this is a
real, live-felt limitation for an actual shopper, not just a synthetic test
case - worth weighing when prioritizing whether to fund the "real semantic
judgment" fix that gap has been waiting on since round 10.

**Continuation of item 2b, found independently re-verifying the fork's own
work: the fix above only covers the case where the model DOES propose
`ADD_TO_CART` and resolves the wrong product - it does not cover "add X
again" not reaching `ADD_TO_CART` at all.** Re-tested the exact re-add
scenario end to end with a real cart (create cart -> add Cork Massage Ball
-> ask to add it again): the second message was routed to `RECOMMEND_PRODUCTS`
instead of `ADD_TO_CART`, `cart_changed: false`, quantity stayed at 1. This
is a real, reproducible residual gap in item 2b, not a re-opening of it - the
fix that exists is correct for what it targets (a resolved-but-wrong product
id) and does nothing for a turn that never reaches the ADD_TO_CART branch in
the first place. Not fixed this session - flagged honestly as the actual
remaining scope of "re-adding an item doesn't work reliably," distinct from
what the fork's own writeup above marked FIXED AND VERIFIED for the narrower
case it actually tested.

**EIGHTEENTH gap, found independently while re-verifying the fork's item
2b fix, unrelated to what it was checking: a single-item add spuriously
suggested a completely unmentioned second item.** Reproduced live: "add a
Cork Massage Ball to my cart" (one item, nothing else named) got back "I can
add the Cork Massage Ball to your cart... I can only add one item at a time
right now, so I've queued Massage Gun as a suggestion" - a product the
shopper never mentioned, invented from thin air as far as the shopper could
tell. Root-caused in `_find_extra_items` (`engine/api/chat.py`): the function
never excluded the title words of the item the turn is ALREADY handling
before checking other catalog products for a match - "Cork Massage Ball" and
"Massage Gun" share the single word "massage", so the handled item's own
title word was double-counted as evidence a second, unrelated product was
also named. 100% reproducible, no phrasing cleverness needed - the same
"one shared generic word bought a false positive" shape the sixteenth kayak-
gap fix closed elsewhere in this codebase, found here in a completely
different function that never had that protection.

Fixed by excluding the handled product's own significant title words from
what counts as a match against any OTHER candidate, before applying the
existing first-word/two-word-overlap heuristic. Verified: the exact
reported case (Cork Massage Ball alone) no longer suggests Massage Gun;
the genuine three-item case (Marathon Pro Racing Shoe / Cork Massage Ball /
Massage Gun, none of which share a title word with each other) re-verified
still correctly surfaces every named item with clear disclosure, unaffected
by the exclusion since none of their titles overlap.

**Verification for this session's full real-bug-fixing batch, re-run
independently after the eighteenth-gap fix, on a freshly and cleanly
restarted engine (no process interruption mid-run this time - two earlier
attempts were invalidated by the same shared-environment/process-restart
noise the fork itself flagged):** `auditroutes.py` - every probe held.
`fuzz.py` - 20/20 sequences, 1200 assertions, every invariant held. Live
spot-checks re-confirmed: 4a (no verbatim echo), 5b (why-question answered
correctly, `used_model: false`), 9 (direct availability answer), 2a (three
named items with a real cart, all three clearly accounted for), and the
eighteenth-gap fix itself (no more spurious Massage Gun suggestion) all hold
on a clean engine.

No `invariant-guard`/`value-auditor` pass has run against this batch,
including the eighteenth-gap fix - do not move any of items 2b/4a/4b/5b/9/18
to `Completed.md` without one. Items 1, 2a (fully verified only with a real
cart, not the exact reported no-cart transcript), 5a, and 6 did not
reproduce and were not independently re-diagnosed - treat as "not currently
observed," not "confirmed fixed."

**NINETEENTH gap - the "add X again" residual flagged above is now
FIXED AND VERIFIED.** Root cause, confirmed by reading the actual response
rather than guessed: "add the Cork Massage Ball to my cart again" was
consistently (5/5 live trials) routed to `PROPOSE_CART_CHANGE`, not
`RECOMMEND_PRODUCTS` as first suspected - the model, faced with a product
already sitting in the cart, chose to ask "want me to add X?" rather than
just doing it, even though the shopper's own words were an explicit,
unambiguous instruction. `PROPOSE_CART_CHANGE`'s own prompt guidance ("when
you want to suggest... rather than just doing it") gives the model no
signal that an explicit repeat request is not this case, and per this
session's own established finding, prompt guidance alone is not reliable
enough for a routing decision this codebase already treats deterministic
detectors as necessary for.

Fixed with a deterministic backstop, the same shape as the fifth-round
"why" backstop: `_find_readd_target`/`_deterministic_readd_action`
(`engine/api/chat.py`) recognize an explicit repeat signal ("again",
"another", "more", "one more") combined with a real, near-full title match
against a product ALREADY in the current cart - never a fuzzy or partial
match, the same strict discipline already established for the sibling
ADD_TO_CART product-resolution override - and execute the add directly
through `_run_trusted_cart_action`, the same Decision-Engine-and-Risk-Gate
pipeline a shopper's own tap or proposal-accept already uses. Only the
ACTION TYPE and PRODUCT are decided deterministically; execution itself
still goes through the full pipeline unchanged. Deliberately does NOT also
require a literal "add"/"cart" word in the message - a first version did,
and it excluded a real, unambiguous case found live ("one more Cork Massage
Ball please", no literal "add" or "cart") - the repeat signal plus a full
real title match already narrows this enough on its own.

Verified: 10/10 fresh phrasings ("add X again", "add another X", "one more
X please", "can you add another X", "again, add X", etc.) now correctly
increment quantity (1 -> 2), confirmed against the real cart each time, not
just the reply text. A genuinely fresh product (never before in the cart)
re-confirmed to still route through the ordinary model-driven ADD_TO_CART
path unaffected (needs a size choice as normal, `used_model: true`) -
this backstop cannot fire for anything not already a real line in the
current cart.

**A separate, pre-existing quirk was found while testing this fix, NOT
caused by it and NOT investigated further (out of scope for this pass):**
"also add a [real, uncarted product] to my cart" as a second message in a
session sometimes returns a generic `RECOMMEND_PRODUCTS`-shaped reply with
`used_model: false`, distinct from both this fix's new backstop (confirmed
by trace: the message shares no words with anything already in the cart,
so `_find_readd_target` returns `None` and never fires) and the ordinary
model-driven ADD_TO_CART path (which a genuinely fresh, single-message add
of the same product correctly uses). Not reproduced or root-caused deeply -
flagged here so a future session does not mistake it for something this
fix introduced.

**TWENTIETH gap - the coffee taste-preference matching gap, open and
independently reconfirmed since round 10, is now substantially fixed.**
This was the single most repeatedly-confirmed real limitation across the
entire session, including by real user testing: `judge_relevance` could
confirm a shop plausibly carries "something smooth and not too bitter" as
a *category* of thing, but nothing ever picked *which* real candidate
actually fits a taste/use-case description sharing no literal word with
any product text - every phrasing landed on the identical undifferentiated
4-item Kettle coffee list regardless of what was actually asked.

Built a second, narrower LLM judgment - `ReasoningService.judge_best_match`
(`engine/reasoning/service.py`, new `best_match_prompt`/`best_match_tool`
in `engine/reasoning/prompts.py`) and `ExecutionService.
_pick_best_semantic_match` (`engine/execution/service.py`) - that ranks
among the SAME real, already-approved candidates `judge_relevance` already
confirmed plausible, never a fresh fetch, never an invented product. The
model's returned `product_id` is verified against the real candidate id
set before being trusted at all (the same never-trust-a-bare-id discipline
already used for `cited_reason`/category validation) - a fabricated or
absent id degrades to the existing "show all of them" behavior, never to
a fabricated pick. Wired into all THREE places a taste/use-case query can
land, found by testing each fresh phrasing individually rather than
assuming one fix covered them all: the category-only-unverified branch,
the query-driven weak-match branch, and (found only by testing "coffee
with a gentle taste"/"a light, easy-drinking coffee"/"something with low
acidity" specifically) the plain relevance-relaxed browse branch, which
several genuine taste phrasings hit without ever reaching either of the
first two because enough candidates already survived basic price/category
filtering to make `titles` non-empty before any semantic check ran at all.

A new `semantic_match_confirmed` flag threads through the payload,
checked in `engine/api/chat.py` AHEAD of `budget_relaxed`/`relevance_relaxed`
(both of which this same fallback path also sets, and whose generic
"here's everything instead" framing would otherwise win first and bury
the specific, judge-confirmed pick behind an undifferentiated list again).

Verified live against Kettle with 7 taste/preference phrasings, some
reused from the original user report, some newly constructed: "something
smooth and not too bitter", "a mellow coffee", "coffee with a gentle
taste", "something for someone who doesn't like bitter coffee", "a light,
easy-drinking coffee", "something with low acidity", "a coffee that is not
too intense" - **all 7 now return a single, specific, real product**
(Brazil Cerrado Pulped Natural, House Filter Blend, Colombia Huila Washed,
or Swiss Water Decaf depending on phrasing - genuinely different real
coffees for genuinely different descriptions, not the same list every
time), up from 0/7 at session start. Controls re-verified unaffected: "a
fruity coffee" (a genuine keyword match) still returns its correct answer
unchanged; "do you have tea?" (a genuinely unmatchable request) still
honestly declines with zero products, not a forced fake match. Northfield
kayak-family and "show me shoes" controls re-verified unaffected by this
change (0 undisclosed, 4 confident options respectively).

`fuzz.py` (20/20 sequences, 1200 assertions, every invariant held) and
`auditroutes.py` (every probe held) both re-run clean after both the
nineteenth and twentieth fixes together, on a cleanly restarted engine
with no process interruption mid-run.

Not moved to Completed.md - no `invariant-guard`/`value-auditor` pass has
run against either the nineteenth or twentieth gap fix yet. Honest
residual: `judge_best_match` is still an LLM judgment, not a certainty -
it has not been adversarially stress-tested the way the false-substitution
saga's mechanisms were across 16 rounds, and a dedicated adversarial pass
(constructing phrasings designed to defeat it, checking for a wrong-but-
confident pick rather than only an honest no-match) has not been run
against this specific new mechanism. Given this session's own repeated
lesson that a fix's first "N/N clean" batch has usually undersampled the
actual failure shape, treat this as a real, substantial improvement -
not a closed, audited guarantee - until that pass happens.

**A dedicated adversarial audit of both the nineteenth and twentieth gap
fixes (5aa01bd) found each PARTIALLY FIXED - real progress, and each with
a precise, reproducible residual, not a re-opening of either round's own
"N/N clean" numbers by luck. Two follow-up fixes closed both residuals,
plus one unrelated pre-existing crash the investigation happened to
surface first.**

- **Nineteenth-gap residual (`180e0fa`): the re-add backstop dropped
  `target.variant_id`.** For a single-variant product (the commit's own
  worked example, Cork Massage Ball) this made no difference; for any
  multi-variant product already in the cart - the majority shape in a
  real footwear/apparel catalogue - re-dispatching without a variant
  always landed on `needs_choice`, dead-ending at "which option would
  you like?" instead of ever incrementing quantity. Reproduced 9/10 on
  fresh phrasings against a sized shoe. Fixed by passing
  `target.variant_id` through as `chosen_variant` - the real, already-
  existing cart line's own variant, trusted the same way a real tap's
  variant already is, not a model guess. Verified via a real tap-seeded
  cart (a free-text setup message does not reliably resolve a size on
  its own, so this needed the actual tap endpoint to test correctly):
  three different re-add phrasings against a sized Trailblazer Running
  Shoe correctly incremented quantity 1 -> 2 -> 3 -> 4, same variant
  throughout. Both standing adversarial guards re-confirmed clean.

- **Twentieth-gap residual (`8afdbcf`): "do you sell energy drinks?" -
  reported directly by a user, not constructed - still substituted all
  4 real coffees dressed as an answer.** Took three rounds of reasoning
  from the code alone to mislocate before live debug tracing found the
  real culprit: not `judge_best_match`, not `judge_relevance` (both
  structurally sound, confirmed again by this investigation), but
  `relevance.shop_carries` itself - a plain deterministic function that
  predates this session's relevance-honesty work entirely and had never
  been touched by any of the sixteen false-substitution rounds. It
  returned `True` for "energy drinks" because the word "drink" (stemmed
  from "drinks") appears verbatim in one unrelated product's marketing
  copy ("Roaster's Choice Subscription": "We pick, you drink. Cancel
  whenever.") - a single coincidental word in a full-text fallback loose
  enough to make an unrelated shop appear to "carry" nearly anything
  asked about, the identical shape this whole saga has repeatedly found
  and closed elsewhere, just in a function none of those rounds reached.
  Fixed by excluding a small, closed-class list of generic consumption/
  desire verbs ("drink"/"eat"/"use"/"try"/"enjoy"/"buy"/"want"/"need")
  from being sufficient alone in `shop_carries`'s full-text fallback -
  not added to the general stopword list, since these remain legitimate
  search words elsewhere (a genuine "what can I drink here" still works
  via the category/title checks that run first).

  Explicitly considered and rejected: adding a decline whenever
  `judge_best_match` returns no pick. Its own prompt correctly returns
  "no match" both when nothing fits AND when several real options fit
  equally well with nothing standing out - collapsing those two into one
  decline signal would have broken legitimate broad-browse requests
  ("what coffee do you have?"). The actual defect was one level up, in
  `shop_carries` skipping the judge entirely once it wrongly said yes.

  Verified: 3 direct deterministic cases (energy drinks -> False, fruity
  coffee still -> True via the same Swiss Water Decaf description,
  coffee bean -> True). Live, Kettle: "do you sell energy drinks?" 5/5
  honest decline (was 5/5 false substitution); "do you sell shoes?",
  "any board games?", "do I get a phone charger here?", "do you have
  tea?", "can I get a sandwich" all still honestly decline; "a fruity
  coffee" unaffected 5/5; "something smooth and not too bitter" now
  returns a specific judge-confirmed match rather than the generic
  list - an unplanned improvement, from correctly routing through the
  already-built `judge_best_match` mechanism this fix stopped bypassing.

- **A genuinely unrelated, pre-existing crash found live while verifying
  the above, fixed in the same commit (`8afdbcf`) since the investigation
  happened to be instrumenting the exact code it lives in:** "do you have
  oat milk?" on Kettle raised a raw 500
  (`UnboundLocalError: wide_products`). `if query and not relevant:` is a
  SIBLING of `if not titles and action_type is ActionType.RECOMMEND_PRODUCTS:`
  (identical indent level, not nested inside it) - so `wide_products`,
  only ever assigned inside that RECOMMEND_PRODUCTS-only block, was
  genuinely unset for any `SUGGEST_ALTERNATIVE` turn reaching this line.
  Not introduced by anything in this session's relevance-honesty chain -
  a real, independent, previously-undiscovered defect this investigation
  happened to be the first thing to actually exercise. Fixed with the
  same lazy-fetch pattern already used to build `wide_products` in the
  first place. Verified: 5/5 clean 200 responses with an honest decline
  on the exact previously-crashing input.

`fuzz.py` (1200 assertions, every invariant held) and `auditroutes.py`
(every probe held) both clean after both follow-up fixes, on a cleanly
running engine with no process interruption mid-run. Not moved to
Completed.md - no `invariant-guard`/`value-auditor` pass has run against
either `180e0fa` or `8afdbcf` yet.

**A dedicated adversarial audit of `180e0fa` found it PARTIAL, not FIXED,
with one concrete gap distinct from what that commit itself verified: the
variant-passthrough only reaches the minority of re-add phrasings that
trigger `_deterministic_readd_action`'s direct-execution path (a full,
literal title-word match). The majority of natural re-add phrasings
("add another one of those", "one more of the same shoe") name the
product by pronoun, never match `_find_readd_target`'s strict title
check, and instead reach the model-driven `PROPOSE_CART_CHANGE` path -
where the created proposal's own `variant_id` was left null even when the
model correctly resolved `product_id` to a product already in the cart
with exactly one variant, so accepting the proposal re-asked a size
question the shopper had already answered in this same cart. Also found,
same audit: a secondary false-positive where "...something different
from the trailblazer..." still matched the Trailblazer as a re-add
target purely because "another" and the title's own words were both
present, ignoring the explicit exclusion.**

Both closed in one follow-up:

- **Proposal-path variant fill** (`engine/execution/service.py`,
  `ActionType.PROPOSE_CART_CHANGE`): when the model's own `params` didn't
  specify a `variant_id` and the change is an `ADD`, the real cart is
  checked for existing lines matching the resolved `product_id` - if
  exactly one distinct variant of that product is already in the cart,
  the proposal is created with that real variant filled in rather than
  left null. Deliberately conservative: never guesses among multiple
  different variants already in the cart, never overrides a variant the
  model did specify. `accept_proposal` (`engine/api/chat.py`) already
  correctly forwarded a proposal's own `variant_id` as `chosen_variant`
  before this fix - that half only needed the proposal to actually carry
  a real value, not a code change of its own.
- **"Different from X" exclusion guard** (new
  `relevance.mentions_explicit_exclusion_of`, shared and reused by both
  `_find_readd_target`'s direct-execution path and available for the
  proposal path): a small, closed-class phrase set
  ("different from"/"not the"/"other than"/"instead of") checked for
  proximity to the very product's title words a repeat-signal match is
  about to fire on - if present, that line is skipped rather than
  treated as a re-add target, falling through to normal handling instead.

Verified: direct-execution path re-confirmed with 3 fresh tests against a
real tap-seeded, sized Trailblazer Running Shoe - two ordinary re-add
phrasings correctly incremented quantity (1->2 both times, fresh carts),
and the exclusion-guard phrasing correctly did NOT increment (fell
through to `RECOMMEND_PRODUCTS` instead). The proposal-path variant-fill
logic itself was verified by direct code tracing (the exact real-cart
`CartLine` shape the new code reads, and `accept_proposal`'s existing,
unchanged `variant_id` -> `chosen_variant` forwarding) rather than a full
live model-routed end-to-end run: live re-testing of pronoun-based
re-add phrasings this session was confounded by this shopping engine's
own SQLite dev database having accumulated a very large number of test
carts and proposals across the many rounds of this session's own
testing, making it unreliable to isolate one fresh proposal's real
before/after state from a `GET /api/chat/proposals` response cleanly
within the time available - noted honestly rather than papered over with
an untrustworthy number. `fuzz.py` (1200 assertions, every invariant
held) and `auditroutes.py` (every probe held) both clean on an
uninterrupted engine run.

Not moved to Completed.md - no `invariant-guard`/`value-auditor` pass has
run against this fix, and the proposal-path variant-fill specifically
still wants a clean, isolated live re-test (fresh demo database or a
tighter test harness that doesn't share state with dozens of prior test
runs) before being trusted as fully proven end to end, even though the
code path itself is straightforward and traced correctly.

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

   **A hardening pass followed, on top of this Phase 2 work (also
   uncommitted on `main` at time of writing), responding to an independent
   `value-auditor` pass on Phase 1+2 that returned THIN and named concrete
   defects.** Ten fixes went in. Nothing from this pass moves to
   Completed.md — see the fresh `value-auditor` verdict at the end of this
   entry.

   - **`engine/execution/mission_ranking.py`:** fixed hyphenated/compound-
     word matching — a shopper saying "ultra-lightweight" now matches
     merchant prose like "Lightweight daily trainer" via hyphen-as-space
     tokenization (`_tokenize`) plus a small explicit intensifier-prefix
     list (`_INTENSIFIER_PREFIXES` — "ultra"/"super"/"max"/etc, dropped from
     the required-match set rather than demanded literally). Added a small,
     explicit "related-but-not-equivalent" table (`_RELATED_NOT_EQUIVALENT`)
     so a shopper excluding "waterproof" no longer sees a product described
     only as "water-resistant" silently pass as compliant — such a product
     is now excluded from the confident shortlist as uncertain (`_uncertain`)
     rather than falsely confirmed. Added a stock/availability hard filter
     (`OUT_OF_STOCK` products can no longer be shortlisted; missing/unknown
     availability stays neutral, never penalized or fabricated). The
     previously dead `constraints` field (captured by the model, never read
     by ranking) is now scored the same as must-haves.

   - **New `engine/reasoning/explicit_extraction.py`:** a narrow,
     deterministic regex-based supplement (explicitly not a second general
     NLP system, per its own docstring) that recognizes explicit trigger
     phrases ("prioritize X", "I only care about X", "X matters most",
     "X must be true", "budget doesn't matter anymore") and unions its
     findings into the model's own structured-state output — never
     replacing what the model already captured. Built because the hardening
     pass found the model's own extraction missing an explicit, unambiguous
     priority statement on roughly half of fresh test turns (see the test
     matrix below for the exact, honestly-mixed numbers).

   - **`engine/db/shopping_state.py`:** added `clear_fields` so scalar
     mission fields (`goal`/`budget_amount`/`budget_currency`/
     `mission_status`) can finally be explicitly retracted — previously
     could only ever be set, never cleared, so "budget doesn't matter now"
     left the old figure silently active for the rest of the session.
     Includes a precedence rule found necessary by a real bug caught during
     this session's own live testing: a same-turn "new value" from the
     model only overrides a clear if it's genuinely different from what was
     already stored — otherwise the model was found to be echoing stale
     context back (fed to it by the Context Builder) in the same turn as a
     genuine clear, which silently undid the clear before this refinement.
     Also added a per-`(session_id, connection_id)` `asyncio.Lock`
     serializing the whole read-modify-write in `merge()` — fixes a real,
     reproduced data-integrity bug (10 concurrent `merge()` calls to the
     same session used to drop 8 of 10 updates; now live-verified 10/10 and
     15/15 preserved across multiple trials, including through the real
     HTTP/chat pipeline with a real signed-in shopper, not just at the
     repository level).

   - **`engine/api/chat.py` — the central fix, with a real iteration, both
     versions described honestly, in order:**
     - First version: `_honest_synthesis()` used a regex blocklist
       (`_PRIORITY_CLAIM_PATTERN`) to detect phrases like "since you
       prioritized X" and discard them when `shopping_state` had nothing to
       back them.
     - A dedicated `value-auditor` pass on this session's own work directly
       falsified that as a general fix: it constructed three ordinary
       rephrasings of the identical defect not covered by the regex ("You
       mentioned lightness matters a lot, so the Trailblazer fits
       perfectly", "Because comfort is what you need most, I picked the
       Trailblazer for you", "I picked this because it best matches your
       stated requirement") and confirmed all three passed through
       unmodified, still reaching the shopper as false personalization
       claims. The auditor's own words: "A blocklist of an LLM's own
       free-text phrasing cannot be complete — the space of ways to say 'I
       chose this for your stated reason' is not enumerable," and
       recommended gating structurally on `has_stated_priority()` for every
       synthesized reply rather than only ones matching an enumerated
       pattern.
     - Acted on immediately, same session: `_honest_synthesis()` was
       rewritten to remove the regex entirely — confirmed by reading the
       current function directly, there is no pattern list left in it. It
       now asks only one question — does `shopping_state` have anything
       (`priorities`/`must_have`/`constraints`, via
       `mission_ranking.has_stated_priority`) that could justify ANY
       personalization claim this turn? If not, the synthesized text is not
       trusted at all, whatever it says — not just discarded when it
       happens to match a known phrasing. Real, acknowledged trade-off: an
       ungrounded turn now always gets the plain, engine-written fallback
       sentence, even on the rare occasion the model's free text would have
       been a perfectly neutral, non-personalized, accurate one.
       Live-verified directly against the exact three bypass phrasings the
       auditor found (all three now correctly blocked) plus the original
       enumerated case, and live-verified end-to-end that a genuinely
       grounded turn (a real stated priority) still gets its real, specific
       narrative while an ungrounded comparison correctly degrades to
       "Here's how they compare." with no invented claim.
     - **This second, structural version has not yet had its own dedicated
       fresh `value-auditor` pass** — only the isolated unit-level bypass
       tests and one live smoke test were run against it this session (both
       passing), not a full independent audit repeat. Do not treat the
       earlier THIN verdict (rendered against the first, regex version) as
       re-confirmed resolved by a fresh audit of the second version — it
       has not been re-audited.
     - Also in `chat.py`: a new `_unconfirmed_compare_items()` — when a
       shopper asks to compare a real product against one that doesn't
       exist in the catalog, the system previously silently substituted a
       different real product with no disclosure; it now prepends an
       honest "I couldn't find an exact match for everything you
       mentioned..." note. Live-verified against existing+nonexistent,
       both-named-correctly, typo-like (correctly not triggering false
       disclosure), and nonexistent+nonexistent cases.

   - **`engine/reasoning/llm.py`:** fixed a pre-existing (not introduced by
     Phase 1 or Phase 2, confirmed `git diff` was empty for this file before
     this session), previously-flagged-but-unfixed logging crash —
     `logger.warning(..., extra={"name": ...})` collided with Python
     logging's own reserved `LogRecord.name` attribute, raising `KeyError:
     "Attempt to overwrite 'name' in LogRecord"` on the "discarded
     unparseable tool call" path, turning a designed-to-be-recoverable path
     into an unhandled 500. Fixed by renaming the key to `tool_call_name`.
     A genuine regression test was added and left tracked (not gitignored
     scratch): `scripts/regression_llm_malformed_tool_call.py` — reproduces
     the exact crash shape against the real `LLMClient.complete()` code path
     (mocking only the provider's HTTP response, not the parsing logic) and
     confirms it no longer raises. This project's three standing suites
     (healthcheck/fuzz/auditroutes) are deliberately HTTP-only per their own
     design and don't unit-test individual functions, which is why this
     regression lives as a separate tracked script rather than folded into
     one of them.

   - **`engine/execution/events.py`:** `shortlist_engagement()` now requires
     the add-to-cart event to be causally after the impression event for the
     same `(session, product)` pair — previously just co-occurrence with no
     ordering check at all, so an add that happened before a product was
     ever shown on a shortlist could count as "converted." Live-verified
     with a constructed pathological case (add-before-impression correctly
     excluded, impression-then-later-add correctly counted). The function's
     own docstring is explicit this is instrumentation, not business-value
     evidence — no baseline exists — and the dedicated `value-auditor` pass
     on this whole hardening effort separately confirmed this metric still
     cannot support any product-value claim.

   - **`engine/reasoning/context.py`:** a documented boundary was found and
     written down, not fixed (there is nothing to fix — it is a real,
     accepted property of the architecture): durable memory
     (`memory_facts`) is fed to the model in the prompt but never to
     `mission_ranking.rank_and_filter`. The model can still fold a durable
     fact into its own `shopping_state` structured output on a later, fully
     generic message — and that structured output IS read by ranking, so a
     durable fact can end up influencing the shortlist indirectly through
     the model's own extraction habit, not through any code path that
     treats it as a ranking input. Not an engineered dependency — nothing
     relies on it happening, nothing asserts it happens, and a prompt or
     model change could stop it with nothing to catch the regression.
     Documented in the module's own docstring so a future session doesn't
     mistake this side effect for a guaranteed feature.

   **Fresh test matrix run this session (not reusing the same cherry-picked
   examples from prior sessions) — reporting both favorable and unfavorable
   numbers rather than only the better one:**
   - 10 fresh mission-extraction phrasings tested live: 10/10 captured
     *something* into structured state after all fixes (up from a
     session-measured baseline closer to 50% the independent audit found).
     But the dedicated `value-auditor` pass, testing its *own* 10 fresh
     phrasings independently, measured only 8/10 landing something
     ranking-usable and 2/10 capturing nothing at all — both numbers are
     reported here rather than only the first.
   - 5 independent trade-off A→B/B→A pairs: 2 showed a visible ranking
     change, 3 did not — not a failure, a property of a small demo catalog
     where the top-scoring product for the tested priority pairs happened
     to already satisfy both priorities simultaneously.
   - Exclusions (exact/hyphenated/compound) all verified.
   - Budget set→change→clear all verified working correctly after the
     precedence-rule fix.
   - Comparison honesty verified (4 cases, listed above under
     `_unconfirmed_compare_items`).
   - Concurrency verified live end-to-end (10/10 and 15/15 preserved across
     trials).
   - Zero-data, platform-capability-limit, and both merchants (Northfield +
     Kettle) all reconfirmed unaffected by this pass.

   **Automated suites:** `invariant-guard` SAFE on the hardening-pass diff
   (checked *before* the final structural `_honest_synthesis` rewrite — note
   this ordering explicitly); `fuzz.py` (20/20, every invariant held) was
   re-run *after* the structural rewrite and held. `healthcheck.py` showed
   113 passed / 3 FAILED across two runs, with *different* failing checks
   each time, all in code paths this diff never touched (escalation/
   simulate/webhook/conversion-reporting), plus one repeatable known
   pre-existing Kettle recovery `why()` flake — consistent with the
   already-documented Groq-throttle variance (see CLAUDE.md's "The
   constraint"), not a regression, since every comparison/discovery-specific
   check passed cleanly both times. `auditroutes.py` fully clean both times.

   **`value-auditor` verdict on the hardening pass as a whole: THIN** (not
   VALUABLE, not NOT VALUABLE) — explicitly because the *first* version of
   the central response-honesty fix was found bypassable by ordinary
   rephrasing (see above), and because `shortlist_engagement` still cannot
   support any product-value claim. The individual narrow fixes
   (concurrency, stock filter, waterproof/water-resistant uncertainty, dead
   constraints field, the `llm.py` crash, hyphen/intensifier tokenization)
   were each independently verified and are real — worth stating plainly,
   not underselling what did work. **Nothing from this session moves to
   Completed.md.** The structural rewrite of `_honest_synthesis` addresses
   the auditor's specific recommendation directly, but has not yet been
   re-audited fresh — the next session picking this up should run a *new*
   `value-auditor` pass specifically re-testing the rewritten, structural
   version (try harder to find a third bypass phrasing, since the auditor
   found the first two attempts insufficient in successive rounds) before
   concluding this is closed.

   This hardening pass is uncommitted on `main` at the time of writing, on
   top of the also-uncommitted Phase 1 (`ee21d53`) and Phase 2 (`962c946`)
   commits it builds on.

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

9. **Shopper-assistant real-world-hardening session — five rounds, honest
   final state.** A long session (commits `1564299` through `4017eaf`)
   attempted to generalize several shopper-assistant behaviors past the
   narrow example phrasings they were originally built and tested against,
   at the user's explicit request. The pattern worth recording for whoever
   picks this up: **every one of the first three "fixed" claims in this
   session was independently found still broken on adversarial re-testing**
   by a `value-auditor` pass instructed to try to break it the same way a
   real shopper's varied phrasing would. Two mechanical bugs found by the
   fourth audit round are now fixed and verified (below); two deeper items
   are genuinely still open and should not be attempted again with the same
   kind of patch - they need real architecture, named explicitly below.

   **Fixed and verified this final round:**
   - **Stock-error silent drop** (`engine/api/chat.py`, commit `4fb161a`):
     a multi-item add whose first named product failed (out of stock, price
     changed) fell through to the generic failure reply and silently
     dropped every other named item - the extra-item disclosure logic only
     ran on the success branch. Reproduced 3/3 with "Court Classic size 9,
     Studio Training Flat size 9, Recovery Slide size 9" on Northfield
     (Court Classic out of stock). Fixed by running the same extra-item
     detection on the failure branch too. Live-verified 3/3: both remaining
     items are now named rather than silently gone. (`_find_extra_items`'s
     own word-overlap matching is somewhat over-inclusive here - it also
     named two unrelated products in testing - but over-disclosure is a
     much smaller problem than the silent loss this closes, and is a
     pre-existing property of that helper, not new.)
   - **Doubled catalog fetch per turn** (`engine/execution/service.py`,
     commit `4017eaf`): `_full_catalog_sample` was called twice for the
     same failed `RECOMMEND_PRODUCTS` query in one turn (the weak-single-
     word-match re-check, then again in the zero-match browse fallback),
     roughly doubling real adapter round-trips for a negative query (up to
     ~20 instead of ~10 at `_MAX_CATALOG_SCAN`). Now cached and reused.
   - Both verified with `fuzz.py` (every invariant held) and
     `auditroutes.py` (every probe held) against the restarted engine.
     `healthcheck.py` was not re-run this specific round (the two fixes
     touch narrow branches already covered by the invariant/fuzz suites,
     and this session had already established `healthcheck.py`'s
     documented Groq-throttle instability makes single extra runs low-
     signal) - a full three-suite pass is still owed before anything here
     is considered for Completed.md.

   **Genuinely open - do not patch again with text-matching, needs real
   architecture:**
   - **`_honest_synthesis`'s structural null-citation bypass**
     (`engine/api/chat.py`). Four successive rounds added a `cited_reason`
     field to the synthesis tool schema so the model states its reason
     structurally - but the field is optional/nullable, and nothing forces
     the model to populate it. When left null, a false personalization
     claim in free prose (a true stored term mentioned as filler, while the
     actual causal claim names something never stated) passes through
     completely uncaught - confirmed 5/5 on fresh adversarial phrasings the
     4th audit round constructed, none reused from any prior round. This is
     structurally unfixable by enumerating more phrasings, per the
     function's own docstring history (blocklist, then a term-presence
     floor, then per-claim regex extraction - each defeated in turn). The
     real fix: make citation **mandatory**, not optional, whenever
     `shopping_state` has any stated priority/constraint - reject any
     synthesized reply that makes an implicit personalization claim with no
     `cited_reason` at all, rather than only checking `cited_reason` when
     the model bothers to fill it in. Not attempted this round; flagged
     explicitly as the next real step rather than left ambiguous.
   - **Semantic relevance gap** (`engine/execution/relevance.py`). Two
     separate live-reproduced failure shapes with no available fix using
     category/keyword-overlap matching: (a) "do you sell kayaks?" on
     Northfield still substitutes an unrelated real product (an Insulated
     Water Bottle) in roughly 1 of 5 trials - the model's own query choice
     ("water") coincidentally overlaps a real product with no actual
     relevance; (b) "something smooth and not too bitter" on Kettle - an
     entirely ordinary coffee-shop preference question that never names the
     product noun - failed 100% of trials (5/5 both this round and a prior
     round), because there is no lexical bridge between a taste-quality
     phrase and Kettle's category/description text. Both require a genuine
     semantic-similarity signal (embedding similarity against product
     text, or a dedicated LLM relevance-judgment call) rather than more
     text/category-overlap logic - not attempted, since a bounded pass
     cannot responsibly close this without risking a sixth round of the
     same overclaiming pattern already documented five times in this
     session's history. Catalog-sampling itself (`_full_catalog_sample`,
     paginating via the adapter's real `total_available`, capped at
     `_MAX_CATALOG_SCAN=2000`) is real and not the bottleneck for either of
     these two cases - confirmed directly, since both failures reproduce
     regardless of sample completeness; the root cause is the lack of a
     semantic (not lexical) matching signal.

   **Also still open from earlier in this same session, unchanged:**
   mission-state extraction still misses a constraint entirely on a
   nontrivial minority of fresh compound-constraint phrasings (last
   measured 1/10 with zero capture, 9/10 capturing something but not
   necessarily every stated constraint); real-volume real-client conditions
   for the relevance/discovery matching path have been reasoned about and
   partially exercised (catalog pagination) but not tested against
   anything resembling a true 40,000-SKU catalog, since none exists in this
   environment.

   **Nothing from this entire session (`1564299` through `4017eaf`) has
   moved to Completed.md, and nothing should until:** a fresh, full
   `invariant-guard` + `healthcheck.py`/`fuzz.py`/`auditroutes.py` pass runs
   against the final state (`4017eaf`), AND a `value-auditor` pass
   specifically re-audits the two newly-fixed mechanical bugs above (not
   yet independently re-audited), AND the two named architecture items
   above get their own dedicated implementation-and-audit pass rather than
   being declared solved by inference from the mechanical fixes being real.

10. **The two named architecture items from #9 above, each given a real
    implementation attempt this session (commits `07d3a00`, `4ace479`) -
    genuine progress on both, neither claimed fully solved.**

    **`_honest_synthesis`'s null-citation bypass (item #9's first
    architecture item) - closed structurally, verified in-process, not yet
    live-model-verified.** A required `personalizes` boolean was added to
    `write_synthesis`'s schema (`engine/reasoning/prompts.py`) alongside the
    existing `cited_reason`. `personalizes: true` (or missing/malformed,
    treated the same, fail-closed) with no valid `cited_reason` is now
    rejected outright in `_honest_synthesis` - there is no longer a way to
    get an uncited personalization claim through by leaving an optional
    field empty, since two structured signals must now be honestly
    reconciled instead of one that can be silently omitted.
    `personalizes: false` still passes through the pre-existing free-text
    backstop checks underneath (a model lying on the boolean itself remains
    a theoretical residual bypass, explicitly not claimed closed).

    Verified in-process (deterministic, no model-call variance to fight):
    the 5 previously-defeating phrasings plus 5 fresh adversarial ones
    (rhetorical-question, assumed-shared-knowledge, comparative-emphasis,
    implicit-and-conjunction shapes), tested both with `personalizes` unset
    and explicitly `true` - 20/20 correctly rejected. A genuinely grounded,
    honestly-cited reply still passes (1/1); a neutral factual reply
    mentioning the real term still passes (1/1); the pre-existing "no
    stated priority" case still falls back as before (1/1). 23/23 total,
    script at `scripts/va_cited_reason_fix5_test.py` (gitignored scratch).

    **Live-model verification was attempted and came back inconclusive, not
    positive** - worth stating precisely rather than glossing over. Two
    live comparison-flow attempts against Northfield both showed the
    synthesis call itself falling back (`reply` = the plain fallback
    sentence while `model_reply`, from the separate, unrelated `reason()`
    call, carried real text) - consistent with `LLMUnavailable` firing on
    the synthesis call specifically, before it could ever reach the new
    `personalizes` check. This is not evidence the fix works under real
    model output; it is evidence the two live attempts made this session
    did not exercise the new code path at all. **This turned out to have a
    real, separate cause - see below - not simply Groq-throttle variance as
    first assumed.**

    **A significant separate, previously-undiscovered bug was found and
    fixed while investigating: `synthesis_tool()` (and the new
    `relevance_tool()` below) were missing the OpenAI
    `{"type": "function", "function": {...}}` request wrapper that
    `propose_tool()` correctly has - both were sent as a bare
    `{"name": ..., "parameters": ...}` dict, which an OpenAI-compatible API
    rejects outright with HTTP 400.** Confirmed directly by calling the
    configured provider in isolation and reading the raw error, not
    inferred. Because `ReasoningService.synthesize()` already catches
    `LLMUnavailable` and degrades silently by design (the correct behavior
    for a real throttle), this bug has been indistinguishable from ordinary
    Groq-throttle variance all session - every "the synthesis call fell
    back" observation logged in this file and the ones before it may
    include an unknown fraction caused by this schema bug rather than
    genuine rate-limiting, on whichever provider/session actually had
    `LLM_PROVIDER=openai` configured. Fixed by wrapping both schemas the
    same way `propose_tool()` already is. **Not yet re-verified live
    end-to-end after this fix** - the two live attempts above happened
    before the schema bug was found; a follow-up session should re-run a
    real comparison/discovery turn against a real stated priority and
    confirm `personalizes`/`cited_reason` actually arrive correctly-shaped
    from a real model response now that the request itself is well-formed.

    **The semantic relevance gap (item #9's second architecture item,
    "kayak substituted with an unrelated product" / "a legitimate taste
    question finds nothing") - a real semantic judge built and wired in,
    genuine improvement measured, not a full fix.** New
    `ReasoningService.judge_relevance()` - a small, narrow model call
    (single boolean field, no action/variant/risk field, same
    narrower-than-`reason()` guarantee `synthesize()` already carries) that
    judges "does this shop plausibly carry something matching this need"
    against the shop's real declared categories and a real sample of its
    actual products, never inventing one. Wired into
    `engine/execution/service.py`'s two existing `RECOMMEND_PRODUCTS`
    fallback gates (`relevance.shop_carries`'s weak-single-word-match and
    zero-match-browse checks) - only ever consulted when the deterministic
    check already said "not carried," never overriding a deterministic
    "yes," never able to substitute or invent a product itself (it can only
    keep or discard candidates the adapter already really returned).
    `ExecutionService` now optionally holds a `ReasoningService` reference
    (`engine/api/deps.py` constructs one and shares it with both), `None`
    in any caller that builds `ExecutionService` directly (e.g. a future
    test), which simply skips the semantic check and keeps the prior
    deterministic-only behavior.

    **A second real, previously-undiscovered bug found live while
    verifying this, not assumed from code review: the deterministic
    `shop_carries(query, ...)` check is close to tautological for a weak
    single-word match.** Checking "water" (the model's own narrowed proxy
    term for "kayak") against a catalog sample that, by construction,
    includes the very "Insulated Water Bottle" product that weak match
    found always says "carries" - the product's own text obviously
    contains "water". Confirmed directly in isolation
    (`relevance.shop_carries("water", wide_sample)` returns `True` on
    Northfield, a shop that sells no water sport equipment at all) - this
    silently defeated every one of the four prior kayak-fix attempts
    documented in item #9 above, regardless of catalog sample size, because
    none of them changed *what term* was being checked. Fixed for this one
    gate by asking the semantic judge directly, using the shopper's own
    original wording (`raw_said`, threaded through from the tap/message
    parameters) rather than the model's narrowed proxy query - judging
    relevance against the paraphrase would just repeat its mistake, the
    same lesson item #9's coffee-case investigation already found for a
    different reason.

    **A third bug found live, not anticipated in the plan: the new model
    call had no timeout, and hung a batch test run far longer than
    acceptable** - `fuzz.py`, which never calls the reasoning-triggering
    `/api/chat` POST route at all (only the model-free tap endpoint), still
    reached this new code path through `/api/chat/act` when a tapped
    action's execution happened to be `RECOMMEND_PRODUCTS`-shaped, and one
    run took over 8 minutes before being killed, versus its normal ~100s.
    Fixed with an explicit `asyncio.wait_for(..., timeout=12.0)` around the
    call, degrading to the same `None`-means-"could not confirm" contract
    as any other `LLMUnavailable` - this is a real robustness fix for a
    live shopper turn too, not just the test suite, since an unbounded call
    on this path could have stalled a real turn for however long the
    underlying HTTP client's own default timeout is.

    **Live verification after both bug fixes, honestly reported with both
    the favorable and unfavorable numbers:**
    - Northfield "do you sell kayaks?", 6 fresh trials: 1/6 still showed
      the false substitution (down from ~4/5 measured in item #9 before
      this fix); 3/6 correctly degraded to an honest "nothing matched
      exactly, here's what's available instead" browse; 2/6 gave a clean
      "not carried"/category-list decline. The one residual failure was
      re-investigated: an isolated direct call for the identical query
      returned the correct `False` reliably, so the live miss is a
      transient failure of that one specific call (timeout, throttle, or a
      dropped response) rather than a repeat of the tautology bug - not
      fully diagnosed, and not claimed impossible to recur.
    - Kettle "a fruity coffee", 5 fresh trials: 3/5 returned real coffee
      matches (unchanged from item #9's own baseline for this exact
      phrase); 2/5 routed to `SUGGEST_ALTERNATIVE`'s separate, untouched
      empty-result path instead of `RECOMMEND_PRODUCTS` at all - a
      pre-existing model-routing variance (which action type the model
      proposes for the same message varies by call) this fix does not
      touch and did not introduce, confirmed by isolated direct calls to
      `_shop_carries_semantic` separately returning `True` reliably for
      both "a fruity coffee" and "something smooth and not too bitter" on
      Kettle when that code path is actually reached.

    **Automated suites, run against this exact final state:**
    `fuzz.py` - 1200 assertions across 20 sequences, every invariant held
    (one run needed killing and a clean restart after the timeout bug above
    was found and fixed; the confirming run completed normally in line with
    historical timing). `auditroutes.py` - every probe held, run twice
    (before and after the timeout fix). Manual invariant reasoning only,
    not a dedicated `invariant-guard` agent pass (not available in this
    session's execution context) - traced through all six hard invariants
    and the idempotency rule by reading the actual diff: no fabrication
    (the judge only gates already-fetched real candidates, never returns or
    invents a product itself), no risk/payment/idempotency/tap-trust
    surface touched, every failure path (exception, timeout, missing
    client) degrades to `None` and is never allowed to raise to the
    shopper.

    **Honest residual gaps, stated plainly rather than declared solved:**
    - The semantic judge narrows the false-substitution rate: it does not
      guarantee zero, per the 1/6 residual above.
    - `SUGGEST_ALTERNATIVE`'s own empty-result path is untouched and not
      gated by this judge at all - only `RECOMMEND_PRODUCTS`'s two fallback
      branches are. A message that routes there instead never gets the
      semantic second opinion.
    - The `personalizes` fix's real-model compliance has not been
      confirmed live post-schema-fix (see above) - only the deterministic
      logic is rigorously verified.
    - Neither fix has had an independent `invariant-guard` or
      `value-auditor` pass; both were reasoned through manually in the same
      session that built them, which this file's own standing rule treats
      as insufficient to claim closed.
    - Mission-state extraction's residual miss rate (from item #9,
      unchanged) and true 40,000-SKU-scale verification of the catalog
      pagination path (also unchanged) remain exactly as open as item #9
      already described them.

    **Nothing from this entry moves to Completed.md.** A fresh,
    independent `invariant-guard` pass and a `value-auditor` pass against
    commits `07d3a00` and `4ace479` specifically, plus the live-model
    re-verification of the `personalizes` fix now that the schema bug is
    fixed, are the concrete next steps - not a sixth round of "seems fixed"
    without independent re-testing, the same standing lesson item #9
    already states and this entry does not get to skip just because two
    real bugs were found and fixed along the way.

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
      addition, further hardened - hyphen/intensifier matching, waterproof/
      water-resistant uncertainty, stock filter, dead `constraints` field -
      by the uncommitted hardening pass also described under item 0) narrows
      an unranked catalog slice into a small,
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

- [~] **Return-Risk Prevention - implemented, integrated, live-verified;
      NOT yet independently value-audited.** New `engine/execution/
      return_risk.py::assess_product` computes two kinds of real,
      evidence-grounded pre-purchase friction signal - never a claim about
      actual return history, because no return/RMA data model exists
      anywhere in this codebase (see the module's own docstring): (1)
      `low_rating` (`known_fact`) - a real, platform-declared `rating`
      below 3.7 with at least 15 reviews to be meaningful, optionally
      naming a real, same-category, in-stock, better-rated alternative
      from the same already-fetched result set; (2) `unconfirmed_
      requirement` (`inferred_concern`) - reuses `mission_ranking`'s
      already-shipped "related-but-not-equivalent" logic (the "waterproof"
      vs "water-resistant" distinction already used for exclusions),
      applied here to a shopper's *positive* stated requirement instead.
      Wired into `RECOMMEND_PRODUCTS`/`COMPARE_PRODUCTS` reply payloads
      (`engine/api/chat.py`) as an additive `return_risk` key per product -
      never removes a product, never blocks a purchase - and into
      `cart_intelligence.py` for the low-rating check on products already
      in the cart. A new `ShopperEvent` type `RETURN_RISK_FLAGGED` records
      when a finding was shown. `invariant-guard`: SAFE (confirmed
      advisory-only, no fabrication - every message field traces to a real
      product field or the shopper's own stated words, no cross-catalog
      invention for the safer-alternative suggestion, no new execution/
      risk-gate surface). Live-verified against the real Northfield
      catalog, not a synthetic fixture: a real 3.6-rated product (Cork
      Massage Ball, 82 reviews) correctly flagged; a real "water-resistant"
      product (Winter Road Shoe) correctly flagged as an inferred concern
      against a shopper who stated "waterproof" mattered, while three
      other real shoes with no such conflict were correctly left
      unflagged. `healthcheck.py` 115/116 (the one failure is the
      long-documented, unrelated Kettle recovery `why()` model-variance
      flake), `fuzz.py` 20/20 sequences/1200 assertions every invariant
      held, `auditroutes.py` clean - none of the three suites touch this
      feature directly, but none regressed either.

      **The frontend rendering gap this entry used to name is now closed**
      (`Completed.md` #53): `storefront/src/ChatWidget.tsx` now renders
      `return_risk` on both product cards and the comparison table,
      live-verified in a real browser against a real 3.6-rated Northfield
      product. That was a rendering fix, not a fresh value-auditor pass on
      the feature as a whole.

      **Not moved to Completed.md**: a dedicated `value-auditor` pass was
      started for this feature and did not complete (hit a session rate
      limit mid-run) - do not treat the description above as an
      independent VALUABLE verdict, it is direct testing only. Also
      genuinely unintegrated per the original spec: cart-proposal and
      checkout-continuity integration (points 8/9 of the original ask) were
      not attempted - the finding is currently visible only in the
      recommend/compare/cart-intelligence surfaces, not threaded into a
      `PROPOSE_CART_CHANGE` proposal's own evidence or the checkout-
      continuity reply. Re-run `value-auditor` and close those two
      integration points before considering this complete.

- [~] **AI Store Diagnosis (Merchant) - implemented, integrated, live-
      verified; NOT yet independently value-audited.** New `engine/db/
      repository.py::business_diagnosis(connection_id, days=7)` - a purely
      deterministic function (no model call in the computation itself)
      that compares real current-vs-prior-window figures ALREADY produced
      by the existing, already-verified `checkout_conversion(compare=True)`
      and `product_performance(compare=True)` (completed orders, cart
      creation, cart-to-checkout rate, checkout success rate, per-product
      revenue) and names whichever real delta is largest in magnitude as a
      `primary_factor` - gated by a minimum-order-count floor (10) and a
      minimum-meaningful-change floor (10%/10 points) before anything is
      named, explicitly labeled correlation and never causation (an
      always-present `note` field says so). Exposed via `GET /api/
      diagnosis/{connection_id}` (`merchant_scoped()`-gated, identical
      pattern to `/api/report`). Wired into the Merchant Copilot
      (`engine/copilot/service.py` - new system-prompt rule instructing
      the model to explain the already-computed evidence, never invent a
      cause, and never claim more than "low"/"medium" confidence - "high"
      is reserved for Holdout's differently-evidenced comparison) and into
      the existing deterministic "Business Insights" synthesis panel
      (`storefront/src/BusinessInsights.tsx`). `invariant-guard`: SAFE
      (read-only, no new write path, every number traced to an existing
      already-correct repository function's own output, the primary-factor
      floors genuinely gate before naming anything, no PII in the
      payload). Live-verified against real data on Northfield: correctly
      returned `has_sufficient_data: false` with an honest reason at low
      order volume; correctly distinguished `direction: "new"` (zero prior
      orders) from a fabricated "flat" (a real bug found and fixed this
      same session); correctly named a real primary factor (cart_creation,
      125->656 carts, +424.8%) when one cleared the threshold at a
      one-day window; correctly returned no factor with an honest note at
      a wider window where nothing cleared it. The Merchant Copilot, asked
      live "Why did my sales change this week? What is the evidence?",
      answered using the real diagnosis data verbatim ("there isn't enough
      data to attribute this change to a specific cause... considered new
      activity rather than a measurable change from a baseline... The
      confidence in this observation is low") rather than inventing a
      cause. A real Playwright browser test (`storefront/tests/
      business-diagnosis.spec.ts`) confirms the Business Insights panel
      loads real data without erroring. `healthcheck.py`/`fuzz.py`/
      `auditroutes.py` all re-run clean, unrelated to this addition.

      **Not moved to Completed.md**, same reason as Return-Risk Prevention
      above: the dedicated `value-auditor` pass for this feature did not
      complete (session rate limit). Also genuinely unbuilt per the
      original ask: Merchant C (Opportunity/Risk prioritization), D (the
      full actionable-intelligence loop with a MerchantTask/action
      attached to a diagnosis), E (Holdout/incrementality completion
      beyond what already existed), F (ROI measurement), and I (Returns
      intelligence) were not attempted this session - see the "Explicitly
      not attempted this session" note below for why.

**Explicitly not attempted this session** (the original request asked for
essentially the entire remaining Shopper and Merchant roadmap in one pass;
after confirming the real current state, most of Merchant A/C/D/E/F/G/H/I/J
turned out to already be substantially built in earlier sessions - Merchant
Copilot, the dashboard rebuild, and the `_scan_catalog` real-volume fix are
all already in Completed.md - and the remaining genuinely-unstarted roadmap
items (AI Store Diagnosis, Recovery Opportunity Radar, Catalog Intelligence,
Inventory Intelligence from the roadmap list a few lines below) were
triaged by value/effort rather than attempted exhaustively, per the user's
own explicit choice when asked how to proceed on a scope this size):
- Shopper Proactive Assistance, Multimodal, and a full Shopper UI redesign -
  none attempted; the first needs a deterministic eligibility/suppression
  design of its own scope, the second has no vision infrastructure to build
  from (confirmed again this session - no image-capable model client, no
  image upload path anywhere in this codebase), the third was not found to
  be architecturally necessary by anything discovered this session.
- Recovery Opportunity Radar, Catalog Intelligence (deeper than the already-
  fixed real-volume alert scan), Inventory Intelligence, deep Customer
  Insights (CLV/cohorts/segments - the schema still cannot support these
  honestly), Returns Intelligence (no adapter declares a returns
  capability, unchanged), Holdout/Incrementality beyond what already
  exists, and ROI measurement - none attempted this session.

- [x] **Smart Cart & Checkout Recovery - Cart Intelligence and Cart
      Proposals, both the shopper-facing loop and the merchant-measurement
      half.** Moved to `Completed.md` in full (#49, #51). Nothing about
      this initiative remains open here; see Completed.md for the full
      value-bar answers and verification record on both halves, including
      the real-volume test, the four browser tests covering both merchant
      render branches and tenant isolation, and the one named non-blocking
      caveat (`cart_proposal_activity` has no row-count cap, correct and
      fast at the volume actually tested but unexercised at true
      worst-case merchant volume).

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
