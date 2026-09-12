# Completed

## Maintenance note

> This file holds work that is **done**: a feature or a fix that is fully built
> and verified end to end, with the relevant test suites passing (backend
> suites run against the live services, browser verification done by hand or
> via Playwright where an HTTP test cannot see the bug). Nothing half-finished
> belongs here - if a fix or a feature is still missing a test pass, a browser
> check, or a piece of its scope, it stays written up in PROGRESS.md until it
> is genuinely done, and only then does an entry move here.
>
> Append-only in spirit: new entries go at the end of "Completed Work" with
> the next number, existing entries are not rewritten except to correct a
> factual error or to note that a later fix generalized or superseded them.
> This is the durable record of what shipped and how it was proven to work -
> PROGRESS.md is the opposite list, what is still open.
> An entry here also carries its value-bar answers and its real-client
> conditions, per CLAUDE.md. An entry that cannot answer "what can somebody do
> now that they could not before" does not belong in this file, however well
> tested it is.
---

## Features Built

### For the shopper

A conversation that can complete a purchase: greeting, product, size, pay, order
lookup - without leaving the chat. No competitor's assistant does the payment half,
because they cannot touch money safely.

- **Tappable everything.** Product cards and size buttons bypass the model entirely,
  so they are instant, cost no tokens, and keep working while rate limited.
- **It asks rather than guessing a size.** The returns argument above.
- **A comparison action.** Ask to compare two named products and get both, fetched
  fresh, side by side - price, description, stock. The most-wanted capability in
  the research (74%), and the thing a seller's assistant is assumed not to do.
  See Completed Work, #27.
- **"Why this?"** on decisions, including what was declined - *"I did not offer
  trying your card again, offering another way to pay"*. The declined half is what a
  seller's assistant cannot write. Now covers a successful recovery too, not only a
  platform's outright incapacity - see Completed Work.
- **Accounts.** Sign up, sign in, sign out, per merchant. Signup collects an email -
  checkout now needs one. bcrypt, server-side sessions, httpOnly cookie, failed
  attempts rate limited per username. Accounts created before the email field
  existed are prompted for it at their next sign-in.
- **Memory that survives closing the tab.** A signed-in shopper's conversation and
  basket both follow them. Fourteen turns stored (`HISTORY_TURNS = 14`, a deliberate
  token-cost bound - see the comment in `engine/session/store.py`); the model reads
  the recent part, because tokens are the binding constraint.
- **Checkout requires an account with an email.** Browsing and filling a cart stay
  guest-friendly, but paying requires sign-in plus an email - the order confirmation
  has to reach somebody, and the gate is what guarantees there is an address on
  file. Enforced twice: the route itself refuses (`require_checkout_identity`: 401
  SIGN_IN_REQUIRED for a guest, 428 EMAIL_REQUIRED for a signed-in account with no
  email) and the storefront hides the card buttons and Pay button behind the gate.
- **The confirmation actually gets sent, and now actually arrives.** `engine/notify/`
  - the gate above existed for this and, until session #26, nothing used the
  address it collected. As of session #29, real Gmail SMTP credentials are
  configured in the local `.env` (gitignored) and a real order-confirmation
  email was received end to end - no code changed, `mailer.py` already read
  `MAILER_SMTP_HOST`/`PORT`/`USER`/`PASSWORD`/`FROM` correctly; it just had
  nothing to connect to before. See Completed Work, #26 and #29.
- **A cart, a conversation and an order each belong to somebody.** A signed-in
  shopper's things follow their account; a guest's follow an httpOnly cookie
  neither they nor a script on the page can read or choose. See Completed Work.

### For the merchant

Their own console at `/merchant`, behind their own secret key: revenue recovered
from real outcomes, what shoppers ran into, what was said to them, the ten rules
(now eleven - see Completed Work), and per-action policy that persists.
**The holdout** - a percentage of new sessions get no assistance at all, so the
console can show the resolution rate with the assistant against without it,
proving the engine caused the difference rather than reporting a number that
would have happened anyway. Off (0%) unless a merchant deliberately turns it on.
See Completed Work, #28 and #30.
**Webhooks** - a merchant's own platform can report friction directly (a
declined payment, an abandoned cart) without our storefront ever being open,
verified by the platform's own signature and run through the identical
pipeline a shopper's own message uses. Built for Kettle; Northfield does not
implement it, deliberately. See Completed Work, #31.
**The Merchant Copilot** - a free-text "ask about your store" panel in
`/merchant`, answering from the same report and pending-approval data the
console already shows, grounded rather than invented, and read-only: it
proposes nothing, decides nothing, writes nothing. See Completed Work, #32.
**Merchant Payments & Checkout** - a recovery queue in `/merchant`, reading
the same merchant-scoped report and approval data the Merchant Copilot uses,
that lets a merchant approve or reject a pending payment recovery directly
from their own console for the first time - previously only reachable from
CV3's own operations queue. See Completed Work, #35.
**The Merchant console, as one product** - `/merchant` restructured from a
long scroll of cards into a left-nav console across six groups (Merchant,
Business, Commerce, AI & Outcomes, Store, AI): a real Overview, Sales &
Revenue with a period trend, Product Performance (quantity/revenue/lowest),
Orders & Conversion (the one funnel stage this engine genuinely
instruments), Customer & Shopping Insights, Returns (honestly unsupported,
not faked), AI Commerce, Recovery, Holdout / Experiment with sample-size
caveats, and a deterministic Business Insights synthesis - each section
naming its own real-client limitations rather than hiding them. See
Completed Work, #36.

### For CV3

One queue at `/operations` across every client, behind the operator key. Approvals
with rejection notes that stay private, and **handovers** - work needing a person
rather than a decision - with a waiting clock and a message back to the shopper.
**The Operations Copilot** - a free-text "ask about your queue" panel in
`/operations`, answering from the same cross-merchant stats, pending queue,
handover and history data the console already shows, resolving bare connection
ids to real merchant names, and read-only: it proposes nothing, decides
nothing, writes nothing. Declines merchant-specific questions and redirects to
the Merchant Copilot instead of answering them from cross-merchant data. See
Completed Work, #33.

### Security

Three key kinds: publishable for the browser, secret per merchant, operator for CV3.
Every route locked or deliberately public, verified by probing the running engine.
`connection_id` is checked against the key, so a caller cannot claim to be a merchant
they are not. A publishable key can no longer reach another shopper's basket,
conversation or order at the same merchant - see Completed Work.

### Tests

`healthcheck.py` - 101 checks, one path end to end. Reports SKIP rather than FAIL
when the provider is busy, and says how many checks never executed.

`fuzz.py` - random shopper sequences, asserting after every step that the cart
matches what was added and removed, a paid cart is never chargeable again *on any
card* (not just the one that paid it), and no merchant's session, cart or orders
are reachable from another. Model-free. Prints its seed so a failure is
reproducible. The paying shoppers now sign in (identity is set before every early
return), since checkout requires it.

`auditroutes.py` - probes the running engine: refused without a key, accepted with
its own, refused another merchant's, and refused another shopper's cart,
conversation and order even when both hold the identical publishable key. The
browser that places the test order signs up with an email first, because the
checkout route now gates on identity.

`storefront/tests/checkout.spec.ts` - the browser-level walk, run with `npm test`
from `storefront/` (serial, `workers: 1`; the Groq throttle forbids parallel model
turns). Drives the checkout gate end to end in a real browser: a guest sees the
sign-in prompt and no card buttons, signs up with an email, and pays; a
`legacy_account.py` account predating the email field is prompted for one before
the picker appears; plus three transcript-bug tests (see Completed Work, #21).
`scripts/walk_checkout.cjs` is retired.

`eval.py` - scores the model's judgement across repeated attempts. Unfinished, and
it has already caught a real problem. (The finishing work itself is tracked in
PROGRESS.md, not here - the harness existing is done; making it complete is not.)

`testexpiry.py`, `testcartreuse.py` - focused reproduction scripts, on disk but
gitignored like `testcart.py` and the other `test*.py` scripts already there
(project convention: `test*.py` is scratch except `testshopper.py`). Not part of
the tracked suite; re-run them by hand when touching approvals or cart ownership.

---

## Completed Work

A full walkthrough as a shopper, a merchant and an operator turned up 37 numbered
issues (see git log for the complete list of commit messages, each with its own
before/after verification). Fixed and committed, each with a failing test written
first, in this order:

1. **Shopper-level scoping.** Any publishable key could read and write any other
   shopper's cart, conversation and order at the same merchant - ids are sequential
   and every shopper at a shop holds the same key. Fixed with an httpOnly visitor
   cookie and an ownership table (`db.owners`), checked on every cart/session/order
   route. `auditroutes.py` now probes for this with two cookie jars.
2. **A paid cart could be charged again with a different card.** The idempotency
   key was derived from the cart *and* the card, so a different card was a
   different key and bought the basket a second time at full price. Fixed with a
   ledger (`db.idempotency`) keyed on the cart alone.
3. **The sidebar Pay button had no idempotency at all** - a fresh uuid per press,
   so three presses bought three orders. Now shares the same ledger as the chat pay
   route.
4. **An approval past its deadline could still execute** if an operator pressed
   Approve on a console drawn before the queue refreshed. Now refused and swept the
   same way an unattended expiry is.
5. **Blocking `ESCALATE_TO_HUMAN` in merchant policy still told the shopper a
   person would help, and created no handover** - the case died in `BLOCKED` while
   the promise was sent anyway. Escalation is now an unblockable floor, the same
   shape as the financial rule.
6. **`fuzz.py`'s paid-cart check retried the same card that had just paid it**, so
   it passed throughout the live double-charge bug above and its own docstring
   recorded the bug as correct behaviour. Now retries a genuinely different card.
7. **A reissued cart id was treated as somebody else's paid basket** after a
   merchant restart reset its counter - the payment ledger didn't know a "fresh"
   cart id might collide with an old one, the same class of bug as #1. Fixed with
   the same "claim outright at creation" pattern.
8. **A cart could still be changed after it was paid for** - lines added, quantities
   changed, coupons applied - through two code paths that never checked (the REST
   cart routes and the chat/tap execution path). Both now consult the payment
   ledger before mutating.
9. **Closing a handover for an unknown case id crashed with a 500** instead of the
   clean refusal the code's own comment promised, because the repository returned
   `False` for "unknown" and `None` for "already closed" and only one of those was
   checked.
10. **`awaiting_person` was false on every escalation** - the flag was computed
    purely from the risk gate's outcome, and escalating a shopper to a person is
    itself risk-cleared (non-financial, reversible), so the one action whose entire
    meaning is "a person is now involved" never showed the "waiting on someone"
    banner.
11. **A successful Kettle recovery explained nothing.** `why` only ever listed a
    platform's outright incapacity, never an option that was simply outranked - so
    Northfield (which can't recover payments at all) showed three honest refusals
    and Kettle (which recovered the payment) showed none, on the exact turn this
    feature exists for.
12. **Paying an empty cart blamed the card** - "it was not your card, try again" -
    for a `CommerceError(CART_INVALID)` that has nothing to do with the card.

Verification for all of the above: `healthcheck.py` grew from 47 to 72 checks,
`fuzz.py` still holds its 1200 assertions across 20 sequences, `auditroutes.py`
holds at every probe including the four new shopper-scoping ones, and
`testexpiry.py` (10/10) and `testcartreuse.py` (6/6) are new, focused reproductions
for the two bugs that needed real timing/restart conditions rather than a database
row. Storefront typecheck held 10 pre-existing errors throughout this batch of
fixes - none of them touched those lines. (Fixed in a later session - see #14
below; `npm run build` now exits 0.)

13. **Approved-then-failed recoveries read as successes in the operations
    console.** `badge()` in `storefront/src/OpsConsole.tsx` coloured the "Already
    decided" tag from `d.state` alone, so an approval that a person got right and
    that then failed on the platform (declined card again, adapter error) showed
    the same green "approved" tag as a genuine success - on the one screen whose
    entire job is telling an operator what actually happened. `badge()` now also
    reads `final_state` (only `"OUTCOME"` counts as success; `state === "APPROVED"`
    with any other `final_state` renders the same red as a rejection), and the row
    gets an inline note: "Approved, but did not go through on the platform... The
    shopper has not been told it worked." No backend change - `final_state` was
    already returned. Pure rendering, so no HTTP-observable assertion is possible;
    no frontend test runner exists in this project yet (`package.json` has no test
    script) and standing one up was treated as its own task rather than folded into
    this fix - verified by hand in the browser instead.

14. **`npm run build` failed on 10 TypeScript errors** - `npm run dev` worked
    throughout, which is how this went unnoticed; only `tsc -b` (the type-check
    stage `build` runs before `vite build`) caught them. All ten were downstream of
    dead code, not live bugs: `App.tsx`'s `switchMerchant` navigated with
    `window.location.assign` and then had an unconditional `return` before ~45 more
    lines that could never run (including the one real type error in the batch,
    `string | null` passed where `string` was required) - deleted outright, along
    with the now-unused `setConnectionState` (the connection is read once and never
    reassigned in this component - navigation, not state, is what changes it),
    `setConnection` and `Link` imports. `AccountMenu.tsx` called `signOut()` with no
    arguments against a signature requiring a `connectionId` it never actually used
    - the sign-out route needs no key at all (`accounts.py:257-270`, cookie-only,
    deliberately privilege-free), so the parameter was dropped from `signOut()`
    rather than threading a value through that would have been ignored. `Landing.tsx`
    was confirmed unreferenced anywhere in the storefront (already flagged as
    probably-dead in Known Issues) and deleted rather than patched. `ChatWidget.tsx`
    stopped destructuring `account`/`onAccount`, which it never reads - `App.tsx`
    still passes them, since greeting a signed-in shopper by name is a real feature
    worth building later and the wiring is one line to restore.

    `npm run build` now exits 0. No frontend test runner exists in this project
    (`package.json` has no test script); this class of bug is exactly what the
    compiler itself catches, so the regression guard is running the actual build
    command rather than a hand-written test - `npm run dev` alone was proven
    insufficient to catch it.

15. **A typed add-to-cart message could contradict itself and add nothing.** A
    shopper who typed "add the size 9 to my cart" in one message got a reply that
    both acknowledged the size and re-asked for it. The model is instructed
    (`engine/reasoning/prompts.py`) to fill `variant_id` from what the shopper
    typed, but execution deliberately ignores that guess - only a tap
    (`chosen_variant`) or an exact whole-message match to a bare follow-up answer
    is trusted (`engine/execution/service.py`, comment there explains this
    replaced four interacting mechanisms that together let a shopper add the same
    item twice). So with more than one buyable variant, execution returns
    `needs_choice` and adds nothing, and `engine/api/chat.py`'s `needs_choice`
    branch used to *append* "Which option would you like...?" to the model's
    reply rather than replace it - and the model's reply, written before
    execution runs, often already claimed the size was understood. The branch now
    replaces the model's sentence outright, the same shape `CLEAR_CART` and
    `CHECK_ORDER_STATUS` already used - the shopper only ever sees the question,
    never the contradiction. Left the "only ask if the model hasn't" trailing-`?`
    heuristic out entirely rather than keeping it for some cases - half-appending
    and half-replacing would have been a second inconsistency.

    Not touched: the exact-whole-message variant matching itself. Loosening it to
    match a variant label anywhere in a longer sentence is the fuzzy matching the
    same comment says was already tried and removed for causing double-adds -
    reopening it to fix a wording problem would trade one bug for the one it
    replaced.

    `healthcheck.py` gained one check in "Cart, through the chat": asking for a
    multi-size product by name (no size stated) must get back the engine's own
    question, not the model's. A first attempt asserted the reply excluded
    success-sounding words ("added", "done", "in your cart", "all set") - run
    against the old code by hand, the model's actual reply ("Sure, I'll add the
    Trailblazer Running Shoe to your cart... Which option would you like...?")
    didn't contain any of them, so that version of the check would have passed
    on the bug it was meant to catch. Rewritten to assert the reply equals the
    engine's own deterministic sentence instead - verified by hand against the
    old code (failed, on two different model phrasings) and the new code
    (passed) before trusting it. 73 checks total.

16. **The other append-not-replace contradiction - the generic failure branch -
    fixed.** `chat.py`'s catch-all `else` (any failed execution that is not
    `needs_choice`/`CLEAR_CART`/`CHECK_ORDER_STATUS`/succeeded `PREPARE_CHECKOUT`)
    used to append "I couldn't turn anything up for that" to the model's
    pre-written reply, so a failure arrived as the model's confident claim
    followed by its contradiction in one message - live evidence: "Sure, I can
    add the Trailblazer Running Shoe to your cart... Which size would you
    like?" then "I couldn't turn anything up for that." The else now replaces
    the model's sentence outright, the same shape as every other special branch.

    The interesting part is the test trigger. The PROGRESS.md note above
    assumed the sold-out `ADD_TO_CART` path (service.py's "every option is
    sold out") reached this branch - it does not in practice. The model reads
    the catalog, sees a sold-out product, and routes the request to
    `SUGGEST_ALTERNATIVE` / `RECOMMEND_PRODUCTS` instead of proposing a doomed
    add - those succeed, so the failure branch never fires. Confirmed
    empirically across several phrasings on two sold-out products. The
    deterministic trigger turned out to be a **paid cart**: the payment ledger
    check at the top of execution refuses any cart-mutating action once the
    basket is paid (`CART_ALREADY_PAID`, service.py), and nothing visible to
    the model distinguishes a paid cart from a live one, so the model reliably
    routes "add this to my cart" to `ADD_TO_CART` and execution reliably
    fails. The healthcheck now builds and pays a real basket, then chats a
    normal-sounding add against the paid cart and asserts the reply equals the
    engine's own failure sentence exactly (the same exact-equality style as
    #15's check, for the same reason - keyword matching would have passed on
    the bug).

    Also checked the same shape elsewhere, per the standing rule: the tap
    endpoint (`/api/chat/act`) has its own failure branch that already replaces
    rather than appends, and the remaining `f"{reply}\n\n..."` appends - the
    `ADD_TO_CART`/`REMOVE_CART_LINE`/`UPDATE_CART_QUANTITY` success path and
    the `CHECK_AVAILABILITY` success path - append a *confirmed* fact onto a
    reply that was already correct, which is not the bug this shape causes
    (appending failure onto an optimistic claim is), so they were left alone
    rather than churned. 74 checks total.

17. **`/merchant` and `/operations` did not carry the merchant in their address.**
    Both pages read the merchant from `sessionStorage`, so which client's console
    you saw depended on which shop you last visited - a Kettle page followed by
    `/merchant` showed Northfield if Northfield was the previous tab. Fixed by
    adding merchant-prefixed routes (`/kettle/merchant`, `/kettle/signin`, etc.)
    where the address itself decides the merchant, matching the pattern the shop
    already uses (`/kettle` is Kettle). Bare `/merchant`, `/signin` and
    `/signup` stay as aliases so a stray bookmark lands somewhere sane rather
    than 404-ing; they fall back to the last-visited merchant, which is the old
    behaviour and the best a merchant-less address can do. `pathFor()` gained an
    optional second argument for sub-pages so internal links (the shop's "Sign
    in" link, the signin ↔ signup toggle) build `/kettle/signin` rather than the
    bare `/signin` that would lose the merchant context. `setConnection()` was
    removed (no caller remains) and `CONNECTION` became `const` (its sole
    reassignment was inside `setConnection`). `npm run build` passes; 7
    pre-existing eslint `set-state-in-effect` errors remain unchanged (in
    App/ApprovalQueue/MerchantConsole/OpsConsole/OrderView/ProductDetail, not
    touched by this fix). No Vitest added: `getConnection()` and `pathFor()`
    depend on `window.location`, setting up jsdom was its own task, and the user
    authorised browser verification for this fix per the existing project
    convention.

18. **The merchant report's "shoppers helped" counted cases, not shoppers.**
    One shopper who hit two problems in a session became two cases and showed as
    two shoppers - the headline figure a merchant checks against their own books
    was inflated by run-ins rather than people. Now counts distinct sessions
    (`repository.py:merchant_report`), and gains a `resolution_rate` (resolved
    problems over problems opened, as a percentage) shown in the merchant
    console beside "Shoppers helped". Both are defensive exactly the way they
    should be: `shoppers_helped` was also over-stated relative to distinct
    shoppers, so the fix is a one-line dedup, and the two new figures make the
    "did it actually work" question answerable in one glance.

    The healthcheck proves the distinct-count with intent rather than a value
    check: it opens a fresh session, runs two declined payments through it (two
    cases, same shopper), and asserts the report's `shoppers_helped` moved by
    one while the friction counts moved by two. The `delta_cases == 2` half
    guards against both payments silently becoming one case, so the check cannot
    pass vacuously. Verified against both code paths: revert the dedup and it
    reports "2 shopper from 2 cases" (FAIL); restore it and it reports "1
    shopper from 2 cases" (PASS). 76 checks total.

19. **No way to demo an approved recovery that then fails on the platform.** The
    operations console had honest rendering for an approval whose execution came
    back refused ("Approved, but did not go through on the platform", OpsConsole),
    but no live path produced one - every real recovery succeeded, so the badge
    and its warn note could never be shown. Added a test card to the Kettle
    platform: `0006` is hard-blocked (a real gateway's "this card cannot pay",
    distinct from "this payment failed"). It declines at checkout like any other
    recoverable card - so the engine proposes a recovery, a person approves it,
    and *then* the platform refuses it. The failure reads as suspected fraud
    upstream (`mapping.py`), the recovery returns `PAYMENT_RECOVERY_FAILED` with
    `final_state = FAILED`, the order stays unpaid, and the shopper is told it did
    not go through. Six new healthcheck checks cover the whole chain, and
    `demo_reset.py` now plants one approved-but-failed recovery in the ops history
    so a walkthrough can show the honest outcome next to a genuine success.

    Doing this surfaced a stale tool: `demo_reset.py` predates the route locks, so
    it never sent a key and could not set merchant policy (401) - and it also
    never shared a visitor cookie, so its cart calls arrived as a different shopper
    every time (carts 404'd at checkout). Both fixed to match how `healthcheck.py`
    authenticates. 80 checks total.

20. **Checkout required no identity, so an order could be confirmed to no one.**
    A guest could reach the card buttons and pay with no address on file - the
    "Guest checkout is deliberate" disagreement from Known Issues, asked explicitly
    and now decided: **paying requires a signed-in account with an email.** Enforced
    in two halves, per the standing rule that hiding the button is only the friendly
    half. The route half is `require_checkout_identity` (new in `engine/api/auth.py`,
    called on `/api/chat/pay` and `/api/shop/.../checkout`): 401 SIGN_IN_REQUIRED for
    a guest, 428 EMAIL_REQUIRED for a signed-in account whose email is null. The UI
    half is `CartPanel.tsx`, which now renders three states - a guest sees the
    sign-in prompt (with sign-up and sign-in links) and no card buttons at all; a
    signed-in shopper with an email sees the card picker and Pay; an account that
    predates the email field sees an email prompt and no buttons until they save
    one. `SignInPage.tsx` gained the email field at signup and `account.ts` gained
    `setEmail` (`/api/account/email`). Legacy rows are prompted at next sign-in
    rather than migrated, and the guest-cart-wins rule is preserved: a shopper's own
    basket takes precedence, and a guest basket is claimed to the account on
    sign-in so nothing already added is lost.

    The gate forced matching fixes in every harness that checked out as a guest:
    `fuzz.py`'s `act_remove`, `act_pay` and `act_read_cart` returned before setting
    who the browser was, so `check_cart_matches` read an account-owned cart with
    visitor-only keys and 404'd - identity is now set ahead of every early return
    (re-verified on 4 seeds, 1200 assertions each); `auditroutes.py` signs its
    order-placing browser up with an email first; `demo_reset.py` signs in for its
    three Kettle checkouts; and `healthcheck.py` gained a drain of the handover
    window at the start of the "promise of a person is kept" block, because 79
    accumulated demo cases had pushed the fresh one (position 78) outside the
    oldest-50 window `handovers_across` returns - the check was failing on backlog,
    not on the code. `npm run build` exits 0; `npm run lint` still carries the same
    7 pre-existing `set-state-in-effect` errors in files this work did not touch.

    Browser verification for the UI half (the half no HTTP test can see) is Leg
    A/Leg B in `checkout.spec.ts` (originally a standalone script, now a standing
    test - see #21): Leg A guest → gate → sign up with email → pay (order lands);
    Leg B legacy account → email prompt → save → picker. Both model-free so the
    Groq throttle never interrupts them. Leg A now asserts the basket at the card
    picker matches the guest basket line-for-line, not just that a basket exists -
    the point of the walk was that ownership, sign-in, cart migration and payment
    all cross in this one flow. All the code above (`auth.py`, `chat.py`, `shop.py`,
    `models.py`, `shoppers.py`, the storefront files, and the four harnesses) is
    now committed; it had shipped in the working tree and in this file before the
    commit did. 82 checks total.

21. **The Playwright walk was a standalone script nobody ran on demand, and three
    small transcript bugs from an earlier walkthrough were still unfixed.** Both
    addressed together, since the bugs are UI-state bugs and the walk is what
    catches those.

    The walk is now `storefront/tests/checkout.spec.ts`, run with `npm test`
    (`npm run test:headed` / `npm run test:ui` for a live browser), wired through
    `playwright.config.ts`. Same two legs as before (guest → gate → sign up → pay;
    legacy account → email prompt → picker), plus three new tests for the fixes
    below. Deliberately serial (`workers: 1`, `fullyParallel: false`): five workers
    hitting the same demo database raced cart bootstrap, and the transcript tests
    each need one real model turn - parallel runs blew past the Groq throttle in a
    way the old single-script walk never triggered. `scripts/walk_checkout.cjs` is
    retired.

    Three fixes, each with its own browser test:

    - **Option buttons did not survive a reload.** `SessionTurn` had no column to
      hold the choices offered with a turn, so the restore-on-mount effect in
      `ChatWidget` could only ever recover `speaker`/`text` - a size question asked
      before a refresh had nothing left to tap after it. Added `choices_json` to
      `session_turns` (idempotent SQLite migration, same pattern as the existing
      shopper-email one in `db/session.py`), threaded through
      `session_store.add_turn`/`turns`, and restored on the frontend
      (`api.ts`'s `StoredTurn`, `ChatWidget`'s restore effect).
    - **A declined payment fabricated a shopper turn** ("My card was declined.").
      `/api/chat/pay` recurses into `chat()` with that invented sentence after a
      decline, and `chat()` unconditionally wrote whatever `message` it received as
      spoken by the shopper. The friction was already recorded as a `Case` and read
      back as fact, not conversation (`session/store.py`'s own stated design), so
      nothing needed the turn repeated. Added `ChatRequest.synthetic`, set only on
      that one recursive call, and the shopper-turn write is skipped when set.
    - **Tapping a size recorded the shopper as having said the bare label** ("8")
      rather than a sentence. `ChatWidget`'s `tapOption` passed the option's label
      straight through as `said`. Variant resolution was never at risk - a tap
      already carries `chosen_variant`, which execution trusts outright and never
      derives from `said` - so this was transcript-only: `tapOption` now builds a
      real sentence naming what it answered ("The 8, for the Trailblazer Running
      Shoe").

    All three verified end to end in a real browser against the running engine,
    one test at a time (the Groq throttle does not allow more than one of these
    per run without waiting between them). `healthcheck.py`'s count is unchanged -
    this is browser coverage, not HTTP.

22. **Three Known-Issue cleanups, no behaviour change.** (a) The rejection branch
    in `routes.py` was pasted three times verbatim in the `decide` handler - only
    the first was ever reachable, the other two were dead copies. Removed them;
    one path to read. (b) `ChatReply` declared `choices` twice with identical
    definitions in `chat.py` - the second shadowed as a no-op. Removed it.
    (c) `retry_after_seconds` was wired through the API and storefront but never
    populated: the rate-limit fallback dropped the `LLMUnavailable.retry_after_seconds`
    that `_retry_after()` reads from the provider's `retry-after` /
    `x-ratelimit-reset-*` headers. `Reasoning` now carries the field and `_fallback`
    threads it, so the shopper is told the real number instead of "a few seconds"
    being the only thing the storefront can ever show.

    These are the last three of the "smaller findings" once listed under Known
    Issues. With them, that paragraph is reduced to the two genuinely-open items
    (near-duplicate messages, `handovers_across` window - #23 fixed the second)
    plus the `DIAGNOSED`-stuck state, which needs care before touching. Verified
    by restarting the engine and reading the code paths; no test-suite count
    changed (none of these were HTTP-observable before this commit either).

23. **`handovers_across` silently hid the newest handovers.** It returned only the
    oldest 50 open handovers, oldest first, with no total. On a long testing session
    or a busy real merchant, the newest cases sat outside that window - an operator
    was told a person would help a shopper while the case for it was invisible,
    reading as "gone" until enough of the oldest were closed. The ops route also had
    no way to ask for more than fifty.

    `handovers_across` now takes `offset` and returns `(rows, total)`; the ops route
    accepts `?offset=&limit=` and reports `total` alongside `handovers`; the console
    pages through the list when it exceeds one page, and its "N people were promised
    help" line uses the real total rather than the page size. Two healthcheck checks
    pin the contract: a page showing one handover still reports a total of one, and
    `offset=1` pages past it to nothing - proving `total` is the whole open set, not
    the page being looked at. 84 checks total. Verified by restarting the engine and
    running the full suite (all 82 ran, 2 model-bound SKIPs as usual).

24. **The cart's "Sign in" and "Create an account" buttons rendered as bare
    underlined text, not buttons** - reported directly ("not properly there") and
    confirmed in a real browser. Two bugs compounding: the gate wrapper in
    `CartPanel.tsx` reused `.gate`, a class built for `Gates.tsx`'s pipeline
    explanation - a fixed 22px/1fr two-column grid meant for exactly one
    numbered-mark-plus-content pair. CartPanel's gates have three stacked children
    (a label, then either a button row or an email field, then a note) with no mark,
    so the grid scattered them across the wrong columns instead of stacking them.
    Separately, the two links carried `className="add"`, but every `.add` rule in
    the stylesheet is scoped to `button.add` or a parent class (`.qbuttons .add`,
    `.payactions .add`, ...) - none of which match a bare `<Link>` rendering an
    `<a>`, so the buttons had no button styling applied at all.

    CartPanel's gates now use their own `.checkout-gate` (flex column, no grid),
    and `.gate-actions .add` gives the anchor case the same visual treatment
    `button.add` already has everywhere else. Also fixed while in there: the
    handovers count line's subject-verb agreement ("1 person were" → "1 person
    was...has not had it"), caught by the same visual pass. Verified in a real
    browser (not just the build): the guest sign-in gate now shows two full-width
    dark buttons side by side, and the legacy-account email prompt (field + Save)
    still lays out correctly under the same rework. `npm run build` and `npm run
    lint` both clean (same 7 pre-existing lint errors, untouched).

25. **Card 0006 - the only way to demo an approved-then-failed recovery live -
    was not reachable anywhere in the shopper UI.** Reported directly, after #19
    claimed the scenario was demoable. The card picker was one shared list
    (1111/0002/0003) across both merchants, a leftover from an earlier
    shared-list attempt that was reverted for a different reason (a card
    doing nothing on one shop read as broken) - but the revert never restored
    each platform's actual declined-card set, so Northfield's 0004 and Kettle's
    0005/0006 were simply absent everywhere, not merchant-gated as intended.

    `CARD_OPTIONS` in `CartPanel.tsx` is now keyed by connection: `conn_demo`
    (Northfield, no recovery capability - every decline there escalates outright)
    gets 1111/0002/0003/0004; `conn_kettle` (can recover) gets
    1111/0002/0003/0005/0006. Verified end to end in a real browser across all
    three surfaces, not just the build: paid with 0006 on Kettle as a shopper
    (declined, chat opened with "someone at the shop needs to approve it"),
    approved the resulting case in the operations console (landed in history as
    "approved, did not run" with the exact "has not been told it worked" note),
    and confirmed the case appears in the Kettle merchant console's activity feed.
    Northfield's picker confirmed to show 0004 and not 0005/0006.

    The instruction behind this fix, worth restating: a claim that something is
    "done" is only true if it is reachable through the actual UI on all three
    surfaces (shopper, operations, merchant) that a real user would use - not
    just provable by a script or a direct API call. `demo_reset.py` and
    `healthcheck.py` calling `card_last4: "0006"` directly was not the same claim
    as a shopper being able to select it.

    After #25, a broader pass re-verified the rest of "Features Built" live in a
    real browser rather than trusting an earlier code-read or an HTTP-only test:
    tappable add-to-cart (152ms, no model call), sign-up with the merchant name
    (not a connection id) shown afterward, cart persistence across a reload, the
    sign-in screen for both merchants (only ever checked by reading the code
    before this pass), the "Why this?" engine panel rendering a real diagnosis
    from a live dead search, and a merchant policy toggle actually persisting
    across a reload rather than only looking flipped. No further "claimed but
    not reachable" gaps turned up.

26. **Checkout requires an email, and until now nothing sent one.** The gate in
    #20 collected an address for exactly this reason; the "used it" half was
    unbuilt. New `engine/notify/` package: `Mailer`
    (`engine/notify/mailer.py`) sends over SMTP if `MAILER_SMTP_HOST` is set, and
    otherwise records rather than delivers - the same "recorded rather than
    pretended" shape `NOTIFY_BACK_IN_STOCK` already uses for an unbuilt
    capability, chosen because no real SMTP credentials existed for this project
    at the time (local, SQLite, no hosting). Real SMTP is switched on by setting
    the environment variable; nothing else changes.

    Every attempt is recorded regardless of whether it actually sent
    (`db.record_sent_mail`, a new `sent_mail` table - `create_all` picks up a new
    table with no migration needed, unlike adding a column to an existing one).
    Wired at both places a payment actually succeeds: `/api/chat/pay`
    (`engine/api/chat.py`) and `/api/shop/{connection}/checkout`
    (`engine/api/shop.py`) - the sidebar Pay button and the chat-driven pay tap
    are two different routes to the same money moving, and both needed the same
    confirmation. `require_checkout_identity` now returns the shopper's email
    alongside the shopper id, since both call sites need it and it was already
    being looked up to get this far.

    No route exposes `sent_mail` - there is no merchant-facing "sent emails"
    view to build one for, and adding an endpoint with no consumer would be
    speculative. Verified instead by reading the table directly after a real
    checkout, on both call sites: paid as a shopper on Northfield via the sidebar
    button (`ORD00007`, recorded, `delivered=False` since no SMTP was
    configured yet), paid on Kettle the same way (`KB-0029`, recorded), and called
    `/api/chat/pay` directly for a fresh signed-up shopper (`ORD00008`,
    recorded) to cover the second route. `healthcheck.py`, `auditroutes.py` and
    `fuzz.py` all held.

27. **Built the comparison action** - the most-wanted capability in the research
    (74%) and the thing a seller's assistant is assumed not to do. New
    `ActionType.COMPARE_PRODUCTS` through all six steps of the standard process
    (`Readme.MD`'s "Adding an action"): non-financial and reversible (read-only,
    same risk shape as `ANSWER_PRODUCT_QUESTION`/`CHECK_AVAILABILITY`), mapped to
    `Operation.GET_PRODUCT`, placed above `ANSWER_PRODUCT_QUESTION` in
    `ASSISTANCE_PREFERENCE` for the chain's own "doing beats describing" reason,
    added to `PROPOSABLE` with a new `compare_with_id` tool-schema parameter and a
    prompt rule for when to propose it, and dispatched in `execution/service.py`
    by fetching both products fresh via `adapter.get_product()` - never from
    anything cached earlier in the conversation, so a comparison can't go stale
    between a shopper mentioning something and asking to compare it.

    `ChatReply` gained a `comparison` field, kept separate from `products` (a
    list to browse) because the storefront renders it differently - always
    exactly two things read against each other, never more. `ChatWidget` renders
    two cards side by side (stacking on a narrow panel), each showing price,
    description and stock, and each tappable via the same `tapProduct()` the rest
    of the app already uses for recommended products, so comparing does not
    dead-end a shopper who decides on the spot.

    Verified end to end, not just in isolation: a direct chat call ("Compare the
    Trailblazer Running Shoe and the Marathon Pro Racing Shoe") correctly
    proposed `COMPARE_PRODUCTS`, auto-cleared through the risk gate, fetched both
    products, and returned a real comparison - confirmed again in the actual
    browser, rendering as two cards with correct prices, descriptions and stock,
    each one tappable. Two new `healthcheck.py` checks pin the contract: the
    model proposes `COMPARE_PRODUCTS` rather than `ANSWER_PRODUCT_QUESTION` for a
    real compare request, and the comparison returned is exactly the two products
    named, not a subset or a substitution. `fuzz.py` holds every invariant across
    20 sequences (run because this touches `execution/service.py` directly), and
    `auditroutes.py` holds. `npm run build` and `npm run lint` both clean (same 7
    pre-existing lint errors, untouched).

28. **Built the holdout - the highest commercial value of anything unbuilt.**
    A slice of new sessions gets no assistance at all, so a merchant can compare
    their outcome against the assisted group's and see the difference the
    engine actually caused, rather than a number that includes sales that would
    have happened anyway.

    Gated behind a new per-merchant policy field, `holdout_percent`, defaulting
    to 0 - a merchant who never asked for an experiment must never have
    shoppers silently left unhelped by one, and every existing deterministic
    test (which assumes every friction turn gets real reasoning) keeps passing
    unchanged. Threaded through the same path as every other policy field:
    `RiskPolicy`, `MerchantPolicy` (new column, migrated idempotently the same
    way the shopper email and `choices_json` columns were), `PolicyUpdate`, the
    GET/PUT `/api/policy` routes, and startup hydration.

    Assignment lives in a new `session_holdouts` table (`db.holdout_status`):
    drawn once, at a session's first friction event, and read back unchanged
    after that - a fresh draw on every friction event would let one shopper
    land in both groups over a single visit, which is not a controlled
    comparison of anything. The check sits in `engine/api/chat.py`, before
    reasoning ever runs: a friction turn for a holdout-assigned session
    short-circuits to `_holdout_reply`, which records a real `Case`
    (`is_holdout=True`, a new column on `cases`) and an unresolved `Outcome`,
    then answers with one fixed, context-free sentence - no diagnosis, no
    proposal, no model call. A shopper's own freeform question is unaffected
    either way; the holdout is specifically about the recovery mechanism the
    business case rests on, not general assistance.

    `merchant_report` gained a holdout comparison (holdout vs. assisted
    resolution rate, from the same time window), null until at least one
    holdout case exists so a merchant who has never turned this on sees
    nothing rather than a confusing 0%-vs-0% panel. The merchant console
    gained a control for it under "Your settings", and the report gained a
    "with the assistant vs. without" panel.

    Verified end to end: forced `holdout_percent` to 100% via the API,
    confirmed a real friction turn returns `risk_rule=HOLDOUT_NO_ASSISTANCE`
    with `used_model` false and no model call spent, confirmed the same
    session stays in the holdout group on a second friction turn, confirmed
    the merchant report splits correctly, then reverted to 0% and confirmed a
    normal session is completely unaffected. Also verified live in the
    browser: set 15% through the actual settings UI, reloaded, and confirmed
    it held - not just the API round-trip. `auditroutes.py` and `fuzz.py`
    (every invariant, 20 sequences - run because this touches `chat.py`'s core
    request path) both held. `npm run build` and `npm run lint` both clean
    (same 7 pre-existing lint errors, untouched - one new one was introduced
    and fixed by moving a derived-state update out of `useEffect` and into
    render, per React's own pattern for state derived from a prop).

    **A real gap in the first version, found by walking the product with the
    user rather than by a review.** `holdout_resolved` was recorded unresolved
    the instant a friction happened and never revisited - so it read as a
    permanent zero regardless of what the shopper actually did next, which
    measures "did the assistant act" rather than "did the problem get fixed".
    A holdout shopper who is declined and simply retries the same card, or a
    different one, on their own - with no recovery ever offered - has resolved
    their own problem, and the comparison's entire point is answering how
    often that happens without help. `db.resolve_unresolved_payment_cases_for_cart`
    (renamed and generalized in #30) is called from both places a payment can
    succeed (`shop.py`'s checkout route and `chat.py`'s pay route, the same two
    routes `confirm_order` already hooks into) and marks the most recent
    unresolved holdout case for that cart resolved, whatever paid it. Verified
    end to end: declined a real cart on a forced 100% holdout, confirmed the
    case recorded unresolved, paid the same cart with a working card with no
    assistance in between, and confirmed the outcome flipped to resolved and
    the merchant report's `holdout_resolved` incremented - using a before/after
    delta rather than an absolute count, since the report window can already
    hold resolved holdout cases from an earlier run and ">= 1" alone would have
    passed even if this specific cart were never actually resolved.
    `auditroutes.py` and `fuzz.py` held again.

29. **Order-confirmation mail is now actually delivered, not just recorded.**
    No code changed - `mailer.py` (built in #26) already sent over real SMTP if
    `MAILER_SMTP_HOST` was set and otherwise recorded without delivering. This
    session, real Gmail SMTP credentials (an app password, not the account
    password) went into the repo root's `.env` (gitignored, not committed):
    `MAILER_SMTP_HOST=smtp.gmail.com`, `MAILER_SMTP_PORT=587`,
    `MAILER_SMTP_USER`/`MAILER_FROM=lingesh@commercev3.com`,
    `MAILER_SMTP_PASSWORD=<app password>`. The engine was restarted to pick up
    the new environment, and a real order-confirmation email was received.
    Anyone else running this locally needs their own credentials in their own
    `.env` - there is nothing to inherit from git.

30. **The holdout-resolution fix in #28 was only half the bug.** Asked to check
    for the same mistake elsewhere rather than trust the one fix, a grep for
    every `record_outcome` call site found two more with the identical shape:
    `engine/api/routes.py`'s rejection branch (an operator turns down a
    proposed recovery) and `engine/expiry.py`'s sweep (an approval nobody
    actioned in time). Both record `resolved=False` the instant they run and
    never look at the case again - so a shopper who was rejected, or whose
    approval expired, and who then simply retried the same cart on their own
    stayed counted as an unresolved problem forever, the exact gap #28 fixed
    for the holdout alone.

    `db.resolve_holdout_case_for_cart` is now
    `db.resolve_unresolved_payment_cases_for_cart` - scoped to
    `friction_type == PAYMENT_DECLINED` (a cart being paid is only an
    unambiguous resolution signal for a payment that was previously declined
    against it, not for an unrelated dead search or failed coupon sharing the
    same cart), and marks every matching unresolved case for that cart, not
    just the most recent - a cart declined twice before it finally paid
    represents two real friction events, both resolved by the eventual
    success. Fixed once, in the repository function both payment routes
    already called, rather than separately in `routes.py` and `expiry.py`
    too - the point of generalizing it is that a fourth friction type
    landing in this same shape later has nowhere left to reopen the bug.

    Verified for all three call sites, not just holdout: declined a cart,
    rejected the resulting approval, then paid the same cart with a working
    card, and confirmed `problems_solved` moved (live-checked directly against
    the running engine: 1784 → 1785); separately, declined a cart, let its
    approval expire via the sweeper, then paid the same cart, and confirmed
    the same (1785 → 1786). Two new healthcheck checks (in "Rejection" and
    "Expiry" respectively, alongside the existing holdout ones), both using a
    before/after delta on `problems_solved` for the same reason the holdout
    check does - an absolute count would pass even if this specific cart's
    case were never actually touched. 94 checks total. `auditroutes.py` and
    `fuzz.py` held again.

    The standing lesson - see CLAUDE.md's "Working practices" - a "recorded
    once, never revisited" outcome bug is a shape, not a one-off. Fixing it
    at a single call site invites finding it again at the next one someone
    copied the pattern into; grep every caller of the same recording function
    before considering the class of bug closed.

31. **Built webhook intake - the feature PROGRESS.md itself named as the one
    blocking a real client from connecting at all.** A merchant's own
    platform can now report friction directly, not only through our
    storefront watching a shopper. `shared/models/events.py` already named
    this as the second of three ways a `Signal` can enter the engine
    (`WIDGET`, `MERCHANT_WEBHOOK`, `ENGINE`); this is what makes it real for
    the second source.

    First, a prerequisite refactor: `engine/api/chat.py`'s `chat()` route was
    split into `chat()` (auth and ownership only) and a new `_process_turn()`
    holding the entire reasoning/decision/risk/execution/reply chain - a pure
    mechanical extraction, confirmed by diff to change no logic. This exists
    so the webhook route could reuse the identical pipeline rather than
    duplicating anything risk-gate-adjacent, which is exactly the kind of
    copy this project's invariants exist to prevent.

    New `engine/api/webhooks.py`: `POST /api/webhooks/{connection}`. No
    bearer key - the caller is a merchant's own backend, not a browser
    holding a credential we issued, so it authenticates via
    `SupportsWebhooks.verify_webhook` instead. An unverified signature is
    401, discarded before the body is ever parsed. Every verified signal
    builds a synthetic `ChatRequest` (`synthetic=True`, so nobody's words are
    misattributed to a shopper who never typed them) and runs through
    `_process_turn` - the exact chain a shopper's own message uses.

    Kettle is the reference implementation
    (`adapters/kettle/adapter.py`): HMAC-SHA256 over the raw body,
    hex-encoded, in an `X-Kettle-Signature` header - the same family of
    scheme Stripe and Shopify actually use, verified with
    `hmac.compare_digest` for the constant-time comparison a signature check
    needs. Northfield does not implement `SupportsWebhooks`; that is
    deliberate - a real integration adds it when that platform's own events
    are worth wiring, not before. `KETTLE_WEBHOOK_SECRET`
    (`engine/api/deps.py`) defaults to a demo value for local convenience,
    the same pattern `MAILER_SMTP_*` already uses.

    Verified end to end against the running engine, not unit-tested in
    isolation: an unsigned webhook refused (401), a wrongly-signed one
    refused (401), an unknown connection refused (404), Northfield (no
    webhook support) refused (404), a correctly signed payment-declined
    event accepted and run through the real pipeline - proposed a real
    recovery action, gated `HUMAN` by `FINANCIAL_ALWAYS_HUMAN`, landed in the
    operations queue with its `MERCHANT_WEBHOOK` provenance visible in the
    query field. Approved it against a real order and watched the platform
    genuinely recover the payment - a first attempt against fabricated
    order/cart ids correctly failed against the real backend instead of
    faking success, proving the whole chain is live, not mocked. Confirmed
    on all three surfaces: the operations queue, the merchant console's
    activity feed, and (had the event carried a real session id) a
    shopper's own chat transcript via the existing poll mechanism.

    Seven new healthcheck checks cover the whole chain (96 total: unsigned,
    wrong signature, unknown connection, unsupported platform, valid
    webhook, real-pipeline verification, unrecognised event type).
    `auditroutes.py` and `fuzz.py` (every invariant, 20 sequences) both
    hold.

    Also fixed in passing, found while running the full suite for this
    change rather than caused by it: `healthcheck.py`'s "a successful
    recovery still explains what it did not do" was genuinely flaky.
    Reproduced directly against the running engine - the check assumed the
    model always proposes all three Kettle recovery actions, so
    `RETRY_PAYMENT` (documented as always ranked last) is reliably the one
    outranked. The model sometimes proposes only two, and when
    `RETRY_PAYMENT` is not among them there is nothing for ranking to
    outrank and nothing for `why()` to explain. Loosened to accept any of
    the three recovery phrasings as the ranked-lower explanation, since
    which two-or-three the model chooses to propose is not something this
    project controls.

    **Correction, found by a later audit:** `get_capabilities` for Kettle
    kept declaring `supports_webhooks: False` after this feature shipped -
    left over from before webhook support existed on that adapter, never
    updated when it was built. Anyone asking (via the console or the
    Merchant Copilot, #32) whether Kettle supported webhooks between this
    entry and the fix got a wrong, capability-grounded "no" on an already-
    working feature. Fixed in `7c525d4` ("Kettle: fix stale
    supports_webhooks=False capability flag"); Northfield's own `False` was
    always correct, since it deliberately does not implement `SupportsWebhooks`.

32. **Built the Merchant Copilot** - Merchant Roadmap Phase 1, item #1 - a
    free-text "ask about your store" question box in `/merchant`. Read-only
    by design: no `ActionType`, no risk gate, no db write anywhere in the
    path, since there is nothing here for the risk model to clear.

    New `engine/copilot/` package: `ask(connection_id, question)` builds
    context from `db.merchant_report(days=30)` and `db.pending_approvals(limit=10)`,
    serializes it as JSON, and sends it to the LLM with a system prompt that
    forbids inventing figures not present in that context - returning
    `CopilotReply(answer, used_model, model_name)`. New route
    `POST /api/copilot/{connection_id}` in `engine/api/routes.py`, gated by
    the same `merchant_scoped()` dependency `/report` and `/stats` already
    use, and new `CopilotQuestion`/`CopilotAnswer` schemas. Frontend: a new
    `Copilot` component in `MerchantConsole.tsx`, rendered above
    `MerchantReport`, plus `console_api.askCopilot` and the `CopilotAnswer`
    type in `api.ts`.

    Extended the same session, before ever being committed: a live test
    asking "what are the current capabilities of this site" exposed that
    the copilot had no capabilities/policy/rules/actions data in its
    context at all - it only ever fetched report + pending approvals, so
    that class of question was unanswerable even when the model was
    reachable, not just when throttled. `ask()` now takes optional
    `capabilities`/`policy`/`rules`/`actions` keyword params folded into the
    same JSON context by a new `_build_context` signature. The four existing
    routes `GET /connections/{id}/capabilities`, `GET /policy/{id}`,
    `GET /policy/rules`, `GET /policy/actions` had their bodies extracted
    into shared helpers (`_capabilities_dict`, `_policy_dict`, `_rules_list`,
    `_actions_list`) so `merchant_copilot` can call the same helpers and pass
    real data in; each route now just calls its helper and returns the
    result unchanged (verified identical behaviour). The system prompt was
    updated to describe all four categories - performance figures, platform
    capabilities, risk policy, risk rules/action types - and to tell the
    model that a "what can this site do" question is answerable from the
    capabilities/actions data, not something to brush off. The copilot now
    answers from report figures, platform capabilities, risk policy, and
    risk rules/action types together.

    Verified end to end against the running engine (restarted to pick up the
    route), not just read: a real question against Northfield's key returned
    an answer citing the exact `shoppers_helped` figure from
    `/api/report/conn_demo`, checked twice against two different live values
    (1017 and 1022) as the report changed between checks - proving the
    number is read live, not hardcoded or stale. Kettle's key against
    `conn_demo`'s copilot route was refused 401, confirming the existing
    cross-merchant scoping holds for this route too. An unanswerable
    question ("what is my top customer's home address?") got an honest "I
    don't have that information" rather than an invented one - the prompt's
    no-invention rule holds in practice, not just on paper.

    `invariant-guard` reviewed the diff against all six hard invariants and
    the idempotency rule by reading the code, not just observing behaviour:
    confirmed `merchant_scoped()` is applied correctly, no write path is
    reachable from `engine/copilot/`, `LLMUnavailable` is caught and
    translated to a safe message rather than a raw error ever reaching the
    merchant, and nothing here gives the model any risk-classification
    power. `test-runner` ran all three suites live with the new route
    present: `healthcheck.py` 98 PASS / 0 FAIL / 3 SKIP (the 3 are the usual
    pre-existing Groq-throttle skips, unrelated to this change), `fuzz.py`
    20/20 sequences (1200 assertions), `auditroutes.py` held at every probe.
    No new checks were added to any of the three suites for this route
    itself - it never touches cart, payment, risk or execution, so this run
    is a clean regression pass confirming nothing else broke, not new
    coverage of the copilot route.

    The extension (capabilities/policy/rules/actions folded in) was
    re-reviewed and re-tested separately rather than assumed safe by
    similarity. `invariant-guard` confirmed the four extracted helpers
    preserve identical auth/response behaviour to the original routes;
    confirmed `merchant_copilot` bypassing the individual
    `Depends(merchant_scoped())` on the helpers is safe because
    `merchant_copilot`'s own `Depends(merchant_scoped())` already validates
    `connection_id` against the caller's key before any helper is called -
    verified by reading `merchant_scoped()`'s actual implementation in
    `engine/api/auth.py`, not just observed behaviour; confirmed
    `_capabilities_dict`'s only possible `HTTPException` (404, unknown
    connection) is caught in `merchant_copilot`'s try/except rather than
    leaking raw; and confirmed the new context is still pure read-only
    informational data with no path back into the risk gate. Verdict: SAFE,
    no violations. `test-runner` re-ran all three suites against the live,
    refactored engine: `healthcheck.py` 96 PASS / 0 FAIL / 4 SKIP (SKIPs are
    the usual Groq-throttle ones, unrelated - the two checks directly
    exercising the refactored code, policy-persistence and
    empty-allowlist, both passed), `fuzz.py` 20/20 sequences (1200
    assertions), `auditroutes.py` held including the four
    specifically-refactored routes. Direct verification without spending a
    model call: called `_capabilities_dict`, `_policy_dict`, `_rules_list`,
    `_actions_list` directly against `conn_demo` and confirmed real data
    (11 operations, STANDARD mode, 11 risk rules, 20 action types) resolves
    correctly; separately called `_build_context` directly with stub data to
    confirm the new fields serialize into the prompt correctly. One real
    live end-to-end proof once the throttle briefly cleared: asked the
    running engine "what are the current capabilities of this site" against
    Northfield's real key and got back a real model-generated answer
    (`used_model: true`) correctly describing Northfield's real capability
    declaration - no webhooks, no payment recovery, 10 supported operations
    listed by name, and `recoverPayment` correctly named as the one
    unsupported operation with the adapter's real reason ("platform exposes
    no payment-recovery or refund endpoint"). A follow-up question
    immediately re-hit the throttle (`used_model: false`, the honest
    fallback), confirming this is the documented Groq free-tier constraint
    (CLAUDE.md's "The constraint") rather than a code defect.

    Frontend: `npm run build` and `npm run lint` both clean (same 7
    pre-existing `set-state-in-effect` lint errors, none in the new code).
    Live-verified in a real browser against the running Vite dev server
    (headless Playwright, since no interactive browser tool was available):
    the "Ask about your store" panel renders above the report panel, the Ask
    button is disabled on empty/whitespace input and enabled with real text,
    Enter-key submission works, no console errors. One real model answer was
    captured live in-browser before the Groq throttle interrupted the rest
    of that run; the throttled fallback text ("I can't reach the model right
    now... the answer above was not generated by the model") was also
    observed rendering correctly in the same run, confirming the
    `used_model:false` honest-fallback path works in the actual UI and not
    only in code.

    No conversation memory - each question stands alone, the same scope
    `/api/simulate` already has - which is a documented, deliberate limit
    of this first version, not a gap found and left unfixed.

    The capabilities/policy/rules/actions extension touched no frontend
    files - `npm run build` re-confirmed clean against the extended
    backend, but no new browser walkthrough was needed since the UI itself
    (the Ask panel, its states) was already verified above and is unchanged
    by this extension.

    Extended again the same session, before ever being committed, with two
    real grounded data sources and a full UI redesign - the copilot's
    context and its panel had both stayed thin relative to what the data
    already on disk could answer.

    New data, both read-only, nothing new tracked: `db.unmet_demand
    (connection_id, *, days=30, limit=10)` in `engine/db/repository.py`
    (exported via `engine/db/__init__.py`) aggregates the `query` text
    already recorded on every `DEAD_SEARCH` friction `Case` - a group-by
    count over data the engine already writes on every dead search,
    case-insensitive, most-frequent first, nothing new instrumented.
    `_catalog_alerts(connection_id, *, limit=20)` in `engine/api/routes.py`
    calls the adapter's `search_products("", limit=100)` fresh on every
    call (no caching) and returns products whose `availability` is
    `OUT_OF_STOCK` or `LOW_STOCK`, wrapped in `except CommerceError:
    return []` so an adapter outage degrades the answer rather than
    breaking the turn. Both threaded into `copilot.ask()` as new optional
    kwargs and folded into the model's context by `_build_context`
    (`engine/copilot/service.py`). The system prompt was extended to state
    explicitly that there is currently no order/successful-search
    tracking, so a "what's my bestseller/top-selling product" question
    must get an honest "not available yet" rather than being inferred from
    the unmet-demand or stock-alert data, which measure something
    different (search failures and stock levels, not purchases) - a
    deliberate scope decision, since real bestseller tracking needs new
    order-line instrumentation this pass does not add.

    UI: the "Ask about your store" panel (`Copilot` component in
    `storefront/src/MerchantConsole.tsx`, new `.copilot-*` classes in
    `storefront/src/styles.css` built on the project's existing
    per-merchant design tokens rather than hardcoded, so it themes
    correctly for both Northfield and Kettle) was rebuilt from a single
    input-plus-overwritten-answer into a running conversation transcript
    (question/answer bubbles, auto-scrolling), five starter suggested-
    question chips shown before the first question is asked, an animated
    "thinking" indicator while waiting on the model, a "Read-only" badge in
    the panel header, and a local markdown-lite renderer (bold, bullet
    lists, and markdown tables rendered as real HTML) written as plain
    string parsing rather than a new dependency, since the model's output
    is simple enough not to need one.

    Verified end to end against the running engine and live database, not
    just read: called `db.unmet_demand('conn_demo', days=30)` and
    `_catalog_alerts('conn_demo')` directly and confirmed real results -
    "trainers" searched 136 times with zero results, and real out-of-stock/
    low-stock titles (Marathon Pro Racing Shoe, Court Classic Tennis Shoe,
    Winter Road Shoe, Windproof Gilet, Thermal Half Zip) matching the
    actual catalog. Two live model calls once the Groq throttle briefly
    cleared: "what's out of stock right now?" got a real answer correctly
    listing exactly the three genuinely out-of-stock Northfield products by
    name; "what are shoppers searching for that we don't carry?" correctly
    ranked "trainers" (136 times) above the one-off noise entries.
    `invariant-guard` reviewed the diff: `unmet_demand`'s WHERE clause
    correctly scopes by `Case.connection_id`, `_catalog_alerts` resolves
    the adapter via the same `connection_id` already validated by
    `merchant_copilot`'s own `Depends(merchant_scoped())` (no cross-
    merchant leak possible), both new functions are read-only (no db
    writes, no adapter mutations), the `except CommerceError: return []`
    correctly guards the one fallible call, and the new data is still pure
    informational JSON with no path back into the risk gate. Verdict: SAFE,
    no violations. `test-runner` ran all three suites live against the
    extended engine: `healthcheck.py` 98 PASS / 0 FAIL / 2 SKIP (Groq
    throttle, unrelated), `fuzz.py` 20/20 sequences (1200 assertions),
    `auditroutes.py` held at every probe (15 locked-route checks, 4
    shopper-scoping checks, 4 public-route checks) - purely additive,
    touching no existing route's behaviour. `npm run build` and `npm run
    lint` both clean (same 7 pre-existing `set-state-in-effect` errors in
    unrelated files, none in `MerchantConsole.tsx`).

33. **Built the Operations Copilot** - CV3 Operations Roadmap Phase 1, item #1
    - the operator-side counterpart to the Merchant Copilot (#32). A free-text
    "ask about your queue" panel in `/operations`, answering from the same
    cross-merchant data the console already shows: `db.ops_stats`,
    `db.pending_across`, `db.handovers_across`, `db.decided_across` - the
    identical repository functions `/api/ops/stats`, `/api/ops/queue`,
    `/api/ops/handovers` and `/api/ops/history` already use, for the
    operator's visible connection set.

    `engine/copilot/service.py`'s shared "call the model, handle the
    fallback" logic was pulled out into a new `_complete(system, context)`
    helper, reused by both the existing merchant-side `ask()` (confirmed by
    diff to be a straight move, no behaviour change) and a new
    `ask_ops(question, connection_ids, merchant_names)`. `ask_ops` resolves
    each row's bare `connection_id` to a human-readable merchant name via a
    `merchant_names` dict (mirroring the enrichment the existing ops routes
    already do) before building the JSON context, and sends it against a new
    `OPS_SYSTEM_PROMPT` that forbids inventing a merchant name, case id, or
    figure not in that context; forbids the model claiming it can act
    (approve, close a handover, change a setting); and - the one adapted from
    a real gap found while writing it - instructs the model to decline a
    merchant-specific question ("what's my resolution rate") and point to the
    Merchant Copilot instead of answering it from cross-merchant workload
    data, since that's a different question this copilot has no business
    answering. New route `POST /api/ops/copilot` in `engine/api/routes.py`,
    gated by the same `Depends(operator)` every other `/api/ops/*` route
    already uses, calling the existing (unchanged) `_operator_connections()`
    helper and `MERCHANT_NAMES`.

    Frontend: the merchant console's copilot UI was extracted into a shared,
    parameterized `storefront/src/Copilot.tsx` (props: eyebrow, title,
    intro, suggestions, placeholder, an `ask` function) - confirmed no
    behaviour or visual change for the merchant console after the
    extraction. `MerchantConsole.tsx` now uses a thin `MerchantCopilot`
    wrapper around it; `OpsConsole.tsx` gained a matching `OpsCopilot`
    wrapper and panel, placed after the stat grid and before the handovers
    panel, with its own starter questions ("What's waiting on me right now,
    oldest first?", "Which merchant has the longest wait?", "What's been
    decided today?", "Are there any handovers nobody has picked up?").

    Verified end to end against the running engine, not just read: a live
    declined Kettle payment was triggered specifically to give the copilot
    something real and current to find, then a direct API call confirmed the
    "waiting on me" answer named the actual merchant ("Kettle & Bloom
    Coffee", never the bare `conn_kettle`), with real wait times and real
    friction/action types. A merchant-specific question ("what's my
    resolution rate this month?") was confirmed to get an honest decline
    pointing to the merchant console, per the prompt's explicit instruction
    - proving that boundary holds in practice, not just on paper. The
    merchant copilot was re-checked after the shared-component extraction
    and still returns the same real answer as before - a direct regression
    check on the refactor, not an assumption of safety by similarity.

    `invariant-guard` reviewed the diff: confirmed `_operator_connections()`
    is unchanged pre-existing behaviour, confirmed `ask_ops` and everything
    it calls are pure reads with no db/adapter writes reachable, confirmed
    the new context is still pure informational JSON with no path back into
    the risk gate, confirmed the `_complete()` extraction left the
    merchant-side `ask()`'s behaviour unchanged. Verdict: SAFE, no
    violations. One pre-existing gap noted, not introduced or worsened here:
    `_complete()` only catches `LLMUnavailable` specifically, so a different,
    unexpected exception from the OpenAI client would leak as a raw 500
    rather than the honest fallback message - this already existed in the
    merchant copilot before this session; flagged as a known gap, not fixed
    in this pass.

    `test-runner` ran all three suites against the live, extended engine:
    `healthcheck.py` 101 PASS / 2 FAIL / 0 SKIP, `fuzz.py` 20/20 sequences
    (1200 assertions), `auditroutes.py` held. The 2 FAILs are pre-existing
    and unrelated - a documented model-behaviour difference from this
    session's earlier Groq-to-OpenAI (gpt-4o-mini) provider switch (two
    checks tied to gpt-4o-mini sometimes proposing fewer recovery-action
    candidates than Groq's model did on specific edge-case turns), confirmed
    by instrumenting the live responses rather than modifying the test files,
    and already known/tracked separately before this feature was built - zero
    regressions attributable to the copilot/refactor changes themselves.
    `npm run build` and `npm run lint` both clean (same pre-existing lint
    errors in unrelated files, none new).

34. **Four real user-reported bugs fixed in shopper chat, not roadmap
    features.** Reported directly as a list of seven; these four are the
    ones addressed this session.

    - **Honest payment-decline reasons, both merchants.** A decline always
      got the same generic "That card was declined, so nothing has been
      charged" regardless of why, even though both adapters already
      correctly mapped the platform's real refusal code to a
      `DeclineReason` enum (`INSUFFICIENT_FUNDS`, `CARD_EXPIRED`,
      `ISSUER_DECLINED`, etc - card 0002 maps to `INSUFFICIENT_FUNDS`, 0003
      to `CARD_EXPIRED` on both platforms) - the value existed and was
      discarded. `engine/api/chat.py` gained a `_DECLINE_SENTENCE` mapping
      and a `_decline_fact()` helper that turns the real
      `CheckoutResult.decline_reason` into a specific sentence ("That card
      has insufficient funds, so nothing has been charged. Try a different
      card."), used in `/api/chat/pay`'s decline branch in place of the old
      generic one. The existing escalation/recovery pipeline is completely
      unchanged (still `skip_model=True`, still creates a case, still
      reaches the ops queue/handover) - only the leading fact sentence
      changed. A redundant "I'm sorry that didn't work" is stripped from the
      continuation via `str.removeprefix` when it would follow the new
      specific sentence, since apologizing after already stating the
      specific problem read as repetitive.

      Verified live on both shops: Northfield 0002 → "has insufficient
      funds"; Northfield 0003 → "has expired"; Kettle 0002 → same specific
      reason followed by Kettle's own recovery-offered continuation,
      unchanged. Confirmed via direct API calls that a handover (Northfield)
      and a pending approval (Kettle) still get created exactly as before -
      the escalation mechanic was not broken by this change.

    - **Order lookup names what was ordered, to the actual owner only.** A
      shopper asking about a past order only ever got the number/status/
      total, never what they'd bought - deliberately thin by original
      design, because execution had no way to check whether the asker was
      the person who placed the order (an order id is guessable). Since
      checkout now requires a signed-in, ownership-tracked account
      (`db.owners`), this was fixable properly. `CHECK_ORDER_STATUS`'s
      dispatch in `engine/execution/service.py` now includes `order.lines`
      (title, quantity) in its `payload` - `payload` is never sent to the
      browser (`ChatReply` has no `payload` field), so this alone changes
      nothing externally. `_process_turn` (`engine/api/chat.py`) gained
      `owner_keys: list[str] | None = None`; `chat()` passes
      `await who.keys_for(req.connection_id)`; the webhook route (untouched)
      implicitly passes `None` via the default. Line items are appended to
      the shopper-facing reply only when
      `db.owners.owner_of(connection_id, ORDER, order_id)` matches one of
      `owner_keys` - otherwise the reply stays exactly as thin as it always
      was.

      Verified live proving both sides of the boundary: paid a real order as
      one shopper, had that shopper ask about it in the same session → got
      the product name appended; a different shopper (different cookie jar,
      same publishable key) asking about the identical order number → got
      only the old thin summary, no leak. `invariant-guard` confirmed the
      ownership check cannot be bypassed by an empty `owner_keys`, an order
      with no owner record, or a stranger's own valid key.

    - **Product recommendations respect a stated price limit.** "I want
      shoes under 2000" previously ignored the price constraint entirely -
      there was no price-filtering mechanism anywhere, and this shop's
      search does literal title matching with no notion of price. Fixed
      with a new `max_price`/`min_price` number field on the
      `RECOMMEND_PRODUCTS`/`SUGGEST_ALTERNATIVE` tool schema
      (`engine/reasoning/prompts.py`, plus a prompt rule to fill it from an
      explicit price statement and never propose something visibly outside
      the limit), parsed into `ProposedAction.parameters` in `_parse()`
      (`engine/reasoning/service.py`), and applied as a post-fetch filter
      (`_within_price`) in `engine/execution/service.py` before both the
      keyword-search path and the empty-catalog-browse fallback. A real bug
      was found and fixed in the same pass: the browse fallback was
      originally disabled entirely when a price limit was set, on the
      mistaken assumption that browsing would show unfiltered results - it
      actually still filters, so disabling it meant "shoes under 2000"
      returned nothing when the literal word "shoes" matched no title, even
      though a cheaper matching product existed under a different word
      ("Sneaker").

      A security-relevant gap was found by `invariant-guard` in this same
      feature and fixed immediately: a NaN or infinite `max_price`/
      `min_price` from the model would reach `Decimal(str(max_price))` and
      raise `decimal.InvalidOperation` on the first comparison - an
      unhandled exception with no try/except anywhere in the call path,
      breaking the turn with a raw error rather than a safe reply (invariant
      5, raw errors never reaching shoppers). Fixed at both layers:
      `math.isfinite()` guards in `_parse()` (the primary defense) and again
      in `execution/service.py`'s own parameter read (defense-in-depth, in
      case a future caller bypasses the parser) - confirmed by direct
      reproduction that a NaN value is now neutralized to "no limit given"
      rather than raising.

      Verified live: "I want shoes under 2000" now returns only products at
      or under 2000 (Everyday Canvas Sneaker 1899, Recovery Slide 1499,
      etc.), correctly falling back to a price-filtered whole-catalog browse
      when the keyword search alone finds nothing under the limit.

    - **Real product ratings, "top rated" queries actually sort by them.**
      There was no rating data anywhere in either merchant's catalog, so
      "top rated products" could only ever be a random-looking suggestion.
      Added `Product.rating: float | None` and
      `Product.rating_count: int | None` (`shared/models/commerce.py`,
      `None` meaning "this platform genuinely has no rating for this one" -
      never a fabricated 0 or 5). Both demo catalogs
      (`sample_merchant/seed/catalog.py`, `sample_merchant_two/seed/catalog.py`)
      gained a `_rating_for(product_id)` computing a fixed, deterministic
      rating and review count from `hashlib.md5(product_id.encode())`
      (deliberately not Python's own salted `hash()`, which varies per
      process and would change every rating on a restart) - assigned once
      per product id, stable forever, which is what lets "top rated" mean
      the same thing twice in a row. Both adapters
      (`adapters/sample/mapping.py`, `adapters/kettle/mapping.py`) map the
      raw rating fields straight through. A new `top_rated: bool` field on
      the same tool schema, with a prompt rule telling the model to set it
      for a top/best/highest-rated request rather than guessing which
      products are "best" itself. `engine/execution/service.py` excludes any
      product with `rating is None` before sorting the rest descending
      (never treating a missing rating as a tied-last 0), applied in both
      the keyword-search path and the empty-query browse fallback -
      confirmed by `invariant-guard` that the None-filter runs before the
      sort in both places so the sort itself cannot raise.

      Verified live: "what are the top rated products" (Northfield) returned
      Race Belt 4.8 → Long Run Tights 4.6 → ... in genuine descending order;
      "what are the top rated shoes" correctly narrowed to footwear only,
      still descending (Marathon Pro Racing Shoe 4.3 → ... → Winter Road
      Shoe 3.7); Kettle's own catalog independently confirmed with its own
      distinct ratings (Colombia Huila Washed 4.9 topping Kettle's list).

    **Verification common to the four above:** `invariant-guard` reviewed the
    full diff (`engine/api/chat.py`, `engine/execution/service.py`,
    `engine/reasoning/service.py`, `engine/reasoning/prompts.py`,
    `shared/models/commerce.py`, both adapters' `mapping.py`, both catalogs'
    seed files) against all six hard invariants and the idempotency rule
    twice (once before the NaN fix, once after) - final verdict SAFE, no
    violations: none of the four fixes touch payment/cart/idempotency logic,
    none give the model any new way to assert risk, and the one real gap
    found (the NaN crash) was fixed and reverified in the same pass.

    **Continuing the same reported list, same session (still uncommitted):**
    the same user report had seven items; three more of them plus one more
    bug the user found on re-test are covered here too.

    - **Button-recovery gap fixed (the user's own re-test finding).** A page
      reload already restored size-choice buttons (`choices`) across a
      refresh, but silently dropped `products` (recommended/searched product
      cards), `comparison` (two-item compare cards), and `payment` (the card
      picker at checkout) - a reload mid-browse, mid-compare or mid-pay lost
      the buttons entirely, leaving only text. Root cause: `SessionTurn` only
      ever had a `choices_json` column; the other three were computed and
      returned live but never persisted. Fixed by adding three new columns
      (`products_json`, `comparison_json`, `payment_json`) via an idempotent
      SQLite migration (same pattern as the existing `choices_json`
      migration in `engine/db/session.py`), threading
      `products`/`comparison`/`payment` through `engine/session/store.py`'s
      `add_turn()`/`turns()`, and updating the ONE `add_turn()` call site (of
      nine total) where these values are actually computed -
      `engine/api/chat.py`'s `_process_turn`. The other eight call sites were
      individually checked and confirmed to have none of these three values
      available in scope, so were correctly left unchanged. Frontend:
      `StoredTurn` (`storefront/src/api.ts`) and `ChatWidget.tsx`'s
      restore-on-mount effect updated to carry all three over the same way
      `choices` already was.

      Verified live with real proof, not just presence: triggered a live
      product search, confirmed the transcript endpoint returns the products
      in the stored turn; triggered a live checkout (PREPARE_CHECKOUT),
      confirmed the restored `payment` object carries the real `cart_id`
      (not a placeholder) - meaning a restored Pay button would actually
      charge the right cart, not just render. `invariant-guard` confirmed
      this is read/replay-only (no new write path to cart/payment/
      execution) and that the restored `payment.cart_id` still flows through
      the same `_mine()` ownership check and the same cart-id-only
      idempotency key as before - restoring data does not create a way to
      bypass or confuse payment idempotency.

    - **Comparison UI redesigned (user-reported: cramped buttons, overflow,
      no explicit buy action).** The old `.comparecard` rendering made each
      entire product card one giant clickable `<button>` (info and action
      conflated, and per the user's report, visually overflowing its
      container). Replaced in `storefront/src/ChatWidget.tsx` with a proper
      fact-by-fact comparison table (`.comparetable` in
      `storefront/src/styles.css`) - one row per fact (price, stock,
      details), one column per product, scrolling inside its own
      `overflow-x: auto` container so the page/bubble never scrolls
      sideways - and an explicit "Buy now" button (`.cpbuy`) at the bottom of
      each column, calling the same `tapProduct()` add-to-cart path the old
      whole-card click used, just now a clearly separate action from reading
      the comparison. Verified via `npm run build`/`npm run lint` (clean, no
      new errors beyond the same 6 pre-existing ones).

    - **Category-first browsing built (user-reported: a generic "what's
      available" ask only ever showed 6 arbitrary products, which happened
      to all be shoes because Northfield's catalog lists footwear first).**
      Investigation found both merchant platforms already carry real
      category data (Northfield's `dept` field, Kettle's `collection`
      field) that the engine never surfaced. Added a new `category: string`
      field to the RECOMMEND_PRODUCTS/SUGGEST_ALTERNATIVE tool schema
      (`engine/reasoning/prompts.py`, with a new prompt rule: leave both
      `search_query` and `category` out for a genuinely general "what do you
      have", let the system offer real categories instead of guessing which
      slice to show; fill `category` with the exact name once the shopper
      names or taps one), parsed in `engine/reasoning/service.py`'s
      `_parse()`. `engine/execution/service.py`'s RECOMMEND_PRODUCTS
      dispatch gained: a new early-return branch that fires only for a
      genuinely unconstrained browse (no query, no category, no price
      bound, no top_rated) - it does one
      `adapter.search_products("", limit=100)` read, derives the distinct
      category set across the results, and returns `category_choices`
      instead of a product list (falling through to the ordinary browse if
      the platform has no category data at all, rather than offering an
      empty list); and a new `_within_category()` filter applied alongside
      the existing `_within_price` filter whenever `category` is set, in
      both the keyword-search path and the empty-query fallback.
      `engine/api/chat.py` threads a new `category_choices` list through
      `_process_turn`, a new `ChatReply.category_choices` field, and a new
      reply-replacement branch ("We carry a few different things - X, Y and
      Z. Which would you like to see?") using the same replace-not-append
      pattern as the pre-existing `needs_choice`/CLEAR_CART/
      CHECK_ORDER_STATUS branches. A fourth new `category_choices_json`
      column was added to `SessionTurn` (same migration/persistence pattern
      as the button-recovery fix above) so category buttons also survive a
      reload. Frontend: `ChatWidget.tsx` renders category buttons the same
      way `choices` buttons already render; tapping one sends "Show me
      {category}" as a normal chat message (consistent with how tapping a
      recommended product already works - not a deterministic tap, since
      RECOMMEND_PRODUCTS was never in the trusted-tap category to begin
      with).

      Verified live end to end, waiting out the Groq throttle between calls
      to get real model responses: "what are the product available in the
      store now" correctly returned 6 real categories (Accessories,
      Apparel, Footwear, Nutrition, Recovery, Tech) with no products shown;
      "Show me Accessories" (same session) correctly narrowed to exactly
      the six real accessories products (Insulated Water Bottle,
      Performance Socks, Foam Roller, Hydration Vest, Running Cap, Race
      Belt) - none of them shoes. `invariant-guard` confirmed the new
      execution branch is a pure read (no cart/payment/adapter write),
      category matching is scoped to the single connection's own catalog
      only, and the model's freely-supplied `category` string can only ever
      narrow to real products that platform actually carries (never invent
      one), with no path to the risk gate.

    - **Multi-item add - honest partial mitigation, not the full fix
      (deliberate scope decision, not a gap left unexplained).** The user's
      original report: "add trailblazer running shoe of size 8 and marathon
      pro racing shoe of size 8" only ever added the first item, with the
      second silently dropped - no acknowledgment it was ever asked for.
      Investigated whether a full fix (both items added in one turn) was
      safely achievable this session: it is not, without a real
      architecture change - `engine/decision/engine.py`'s Decision Engine
      selects exactly one candidate per turn by design (its own docstring:
      "answers exactly one question: which of these candidate actions
      should we take?"), and this project's existing variant-trust rule
      (only an exact tap or exact-whole-message match is ever trusted for
      ADD_TO_CART, never a parsed free-text guess - the fix for a
      documented prior double-add bug, Completed.md #15) means even a
      single ambiguous item always needs a clarifying tap, so two ambiguous
      items in one message would need a pending-item queue that survives
      the tap round-trip across turns - real, separate engineering,
      deliberately not attempted this session rather than risking a
      half-built state machine that could reopen the double-add bug class.

      What WAS fixed: (a) a new prompt rule (`engine/reasoning/prompts.py`)
      telling the model to propose a separate ADD_TO_CART for each product a
      shopper names in one message, up to the four-action schema limit,
      rather than silently picking only the first - so the second item at
      least reaches `reasoning.actions` where the system can see it was
      asked for; (b) a new `_second_item_note()` helper in
      `engine/api/chat.py`, called from both the needs_choice reply and the
      ADD_TO_CART success reply, that detects when `reasoning.actions`
      contains more than one ADD_TO_CART proposal for different products and
      appends an honest sentence - "I can only take one item at a time right
      now - ask me for the other one next and I'll add that too" - rather
      than saying nothing about the second item, which is what happened
      before. This directly fixes the "it is not adding two products, and
      doesn't say so" half of the complaint; it does not fix the "both
      should add in one message" half, which remains open in `PROGRESS.md`
      with the architecture reasoning above, not silently closed.

    **Process-hygiene incident during verification, not an application
    bug:** a test-runner agent reported an apparent server crash under
    `fuzz.py` load correlated with the new turn-persistence code.
    Investigated and disproved: the actual cause was four duplicate
    `fuzz.py` client processes and two duplicate engine processes left
    running simultaneously from repeated manual restarts during iterative
    testing, all contending for the same SQLite file. After killing every
    stray process and restarting cleanly with exactly one instance of each
    service, a full clean run held: `fuzz.py` 20/20 sequences, 1200
    assertions, "every invariant held", zero crashes. Confirmed
    `engine/reasoning/service.py`'s `_FALLBACK_ASSISTANCE` table (used only
    when the model is unreachable) is completely untouched by any diff this
    session via `git diff` - ruling it out as a cause of anything. No code
    fix was needed; this was purely session/process hygiene, not a bug.

    `test-runner` ran the full three-suite pass at the end, after the
    process-hygiene cleanup, against the running engine covering all eight
    fixes in this entry: `healthcheck.py` 99 PASS / 2 FAIL / 0 SKIP - the two
    FAILs ("the model proposes a comparison when asked to compare two
    products", "asks which size instead of guessing") were investigated and
    conclusively attributed to the documented Groq free-tier throttle
    (CLAUDE.md's "The constraint") rather than a regression: both failures
    show the reasoning fallback table selecting `RECOMMEND_PRODUCTS` with
    empty parameters (the only option that table has ever had for an
    unclassified request when the model can't be reached - confirmed via
    `git diff` that `_FALLBACK_ASSISTANCE` was not touched this session), so
    these two specific checks were already fragile under throttle before any
    of this session's changes; the only thing that changed is what an
    empty-param RECOMMEND_PRODUCTS now replies with when hit that way.
    `fuzz.py`: 20/20 sequences, 1200 assertions, every invariant held.
    `auditroutes.py`: all 23 checks held. `npm run build`/`npm run lint`
    clean throughout (same 6 pre-existing lint errors).

    These are bug fixes to already-shipped shopper-chat functionality, not
    new roadmap features, so the value-bar/feature-spec process does not
    apply - each fix restores or corrects behaviour a shopper already relies
    on rather than adding a new capability line to the roadmap.

35. **Built the Merchant-facing Payments & Checkout recovery queue** - Merchant
    Roadmap Phase 1, closing the exact gap CLAUDE.md names for the Merchant
    Copilot ("every answer that identifies a problem must offer the action
    that fixes it, executable from the same panel"), applied here to payment
    recovery specifically rather than to the Copilot's general answering.

    Before this session, a merchant had no reachable way to act on a pending
    payment-recovery case from their own console at all. `storefront/src/
    ApprovalQueue.tsx` exists in the repo but is not imported or rendered
    anywhere (confirmed by grep) - so the only place a recovery could be
    approved or rejected was CV3's own `/operations` console, never the
    merchant's. `storefront/src/PaymentsPanel.tsx` (new, wired into
    `MerchantConsole.tsx`) closes that: payment failures, recovery
    opportunities, pending recovery, recoveries completed and revenue
    recovered, all read from the existing merchant-scoped
    `GET /api/report/{connection}` and `GET /api/approvals/{connection}`
    routes, with Approve/Reject buttons that call the existing
    `POST /api/approvals/{connection}/{approval_id}` route
    (`console_api.decide`) - the same risk-gated, idempotent decide path the
    operations console already uses. No new `ActionType`, no Risk Gate
    bypass, no path for the Copilot or the model to authorize a payment - the
    merchant's own explicit tap is still what the pipeline requires, same as
    every other financial action in this codebase.

    `engine/db/repository.py`'s `merchant_report()` gained two figures this
    session: `recovery_count` (resolved outcomes carrying a
    `revenue_recovered_amount` - a count, not a currency total) and
    `recovery_opportunities` (cases whose `selected_action` is RETRY_PAYMENT/
    OFFER_ALTERNATE_PAYMENT/SPLIT_PAYMENT - not the same as the raw
    PAYMENT_DECLINED friction count, since not every decline gets a recovery
    action proposed: Northfield has no recovery capability at all, so every
    decline there escalates instead). `engine/copilot/service.py` was
    extended to surface and explain both fields, since the copilot already
    read `db.merchant_report()` and `db.pending_approvals()` for its answers
    (Completed.md #32) - the UI panel and the Copilot are reading the
    identical repository call, so they cannot disagree. When
    `supports_payment_recovery` is false, the panel shows why and renders no
    approve/reject controls at all - never an empty queue implying nothing is
    wrong.

    `revenue_recovered` was not renamed or reframed as "lost revenue"
    anywhere in this change; a payment failure is shown as a failure count,
    and money is only ever counted once an `Outcome` row actually carries a
    captured amount - no projection, no counterfactual claim.

    Verified end to end, not assumed:
    - `healthcheck.py`: 103/103 passed at the point this feature's own checks
      were exercised (a later re-run after further live testing, described
      below, showed 4 unrelated model-proposal-selection failures under the
      documented Groq throttle - none touch this diff, and the recovery
      -specific checks "case reaches the queue", "approving executes",
      "revenue is captured", "the order is actually paid", "a second approval
      changes nothing", and "the shopper is told the outcome" passed in both
      runs). `fuzz.py`: every invariant held across 20 sequences. `auditroutes.py`:
      every probe held. `npm run build`: clean, 0 errors.
    - **Zero data**: exercised directly against a real merchant, not a seeded
      empty one - `GET /api/report/conn_kettle?days=0` returned
      `recovery_count: 0`, `recovery_opportunities: 0`,
      `revenue_recovered: "0.00"`, empty friction, no crash, no divide-by-zero.
    - **Real volume**: exercised against Kettle's actual case volume, not a
      demo-scale fixture - `recovery_opportunities: 1393`, `recovery_count: 285`,
      `revenue_recovered: "559588.60"` INR, live-read (`recovery_count` well
      below `recovery_opportunities`, consistent with not every recovery
      succeeding - not an inflated or coincidentally-matching pair of numbers).
    - **A platform that cannot**: Northfield's panel, live-checked, shows the
      "not supported" explanation with zero Approve buttons anywhere on the
      page (confirmed via a real Playwright browser session, not just a
      curl check).
    - **A platform that is down**: the Kettle merchant backend (port 8002)
      was actually stopped, and a real pending approval was decided against
      it live - the response degraded honestly (`"error_code":
      "UPSTREAM_ERROR"`, `"summary": "The platform reported a problem:
      transport failure calling /graphql"`, `"final_state": "FAILED"`), no
      raw platform error reached the caller, and the queue remained readable
      throughout. The backend was then restarted.
    - **Live browser click-through**: a real Playwright session (not the
      typecheck/build guard alone) drove a fresh shopper through Kettle -
      add to cart, sign up, pay with card 0002 (recoverable decline) - which
      produced a real pending approval, then opened the Kettle merchant
      console, confirmed the pending card rendered with its diagnosis and
      shopper-reply text and a visible Approve button, clicked Approve, and
      confirmed on reload that the case had left the pending queue with no
      JavaScript console errors at any point. A `value-auditor` pass run
      mid-session correctly caught that this exact step, and the two
      real-client conditions above, had not yet been exercised and blocked
      the feature from this file until they were - the audit's objection is
      recorded here because it is the reason this entry says "verified" with
      evidence rather than by inspection.

    Six value-bar answers: (1) a merchant's one marketer, deciding whether to
    approve a specific pending recovery for a specific order, from their own
    console; (2) they can now act on a recovery from their own console at
    all, where before the only reachable path was CV3's own operations queue;
    (3) a wrong approve moves real money against a live order - mitigated by
    the pre-existing Risk Gate and idempotency, but the merchant's own
    decision still carries real stakes; (4) the action is Approve/Reject, in
    the same panel, same screen, no navigation away; (5) `recovery_count`,
    `recovery_opportunities` and `revenue_recovered` in `merchant_report()`;
    (6) see the four real-client conditions above, all four now exercised
    rather than assumed.

36. **Rebuilt the Merchant tab as one coherent product** - restructured
    `MerchantConsole.tsx` from a single long scroll of unrelated cards into
    a left-nav console (`.merchant-shell`/`.merchant-nav`) with six groups
    matching a real information architecture (Merchant/Business/Commerce/
    AI & Outcomes/Store/AI), and built or exposed the sections that
    previously had no real surface, closing several genuine gaps the audit
    for this session found rather than assumed:

    - **Overview** (new `Overview.tsx`): total sales, completed orders,
      shoppers helped, checkout success rate, and a "needs your attention"
      list (waiting-on-you cases, pending recoveries, out-of-stock counts,
      an unreachable-platform warning) that deep-links into the section
      with the detail. Every figure is read from the same routes the
      dedicated sections use, so this page cannot show a number a section
      it links to would contradict.
    - **Sales & Revenue** (`MerchantReport.tsx`, trimmed and refocused):
      total sales, AOV, completed orders, revenue recovered, and now a real
      period-over-period trend (`GET /api/sales-trend`, wrapping the
      already-built-but-unexposed `db.sales_period_comparison`). Top
      products, the holdout comparison, recent activity and friction
      breakdown moved to their own sections below rather than staying
      duplicated here.
    - **Orders & Conversion** (new `OrdersConversion.tsx` + new
      `db.checkout_conversion` + `GET /api/conversion`): the one funnel
      stage this engine can report honestly. Every checkout attempt -
      successful or declined - already writes an `ExecutionAttempt` row
      before the platform is called (`idempotency.py::claim`), so
      "checkout attempts vs. completed orders" is a real, SQL-aggregated,
      already-instrumented pair, not a new fabricated metric. Explicitly
      NOT a full session-to-sale funnel: `ShopperCart` is upserted once per
      shopper per merchant and reused forever (not one row per visit), and
      a guest's cart isn't tracked there at all, so "carts started this
      window" cannot be answered honestly from existing data without new
      instrumentation this session did not add. The UI and the repository
      docstring both say this in the same words, so the limitation travels
      with the number rather than living only in a code comment.
    - **Product Performance** (new `ProductPerformance.tsx` + new
      `GET /api/products`): the full `db.product_performance()` breakdown -
      quantity ranking, revenue ranking, lowest performers - that `/report`
      only ever embedded a 5-item quantity slice of. The Copilot already
      read this function directly (session before this one); now the UI
      does too, through the same call, so the two cannot disagree. Verified
      live: switching to "By revenue" on Kettle re-ranks correctly (Burr
      Hand Grinder leads by revenue despite Colombia Huila Washed selling
      more units), and the Copilot asked the identical question in the same
      session returned the identical ranking.
    - **Customer & Shopping Insights** (new `CustomerInsights.tsx` + new
      `GET /api/unmet-demand`): what shoppers searched for and didn't find
      (`db.unmet_demand`, previously Copilot-only), plus the existing
      friction breakdown. Deliberately does not invent repeat-purchase
      tracking, CLV, or segments/cohorts - audited directly against
      `engine/db/models.py` and confirmed no schema exists for any of
      those, so the section says so in its own text rather than showing a
      fabricated number.
    - **Returns** (new `Returns.tsx`): audited before writing any UI -
      neither adapter declares a return/refund capability, `ISSUE_REFUND`
      exists as a type in `shared/models/action.py` but is not in the
      model's proposable-action list (`engine/reasoning/prompts.py`) and no
      adapter implements it, so it is structurally unreachable. This
      section states that plainly instead of shipping a thin or fabricated
      analytics page - the same shape `PaymentsPanel` already uses for
      Northfield's missing recovery.
    - **AI Commerce** (new `AICommerce.tsx`): shoppers helped, resolution
      rate, problems solved, and recent case activity (now reading the
      previously UI-unused `GET /api/cases` route), labelled explicitly as
      observed outcomes, not a causal claim.
    - **Recovery** (new `Recovery.tsx`): a read-only summary of the same
      recovery figures `PaymentsPanel` shows, deep-linking to Payments &
      Checkout for the actual Approve/Reject action rather than duplicating
      those controls - one place to act, one place to disagree with.
    - **Holdout / Experiment** (new `Holdout.tsx`, extracted from the old
      report panel): the existing holdout comparison, now with sample sizes
      shown explicitly and a directional-vs-real-signal caveat driven by an
      actual `n<20` threshold rather than shown unconditionally - verified
      live against Northfield's real data (1 holdout case, 4,971 assisted)
      correctly triggering the "treat as directional, not proof" wording.
      A revenue-per-holdout-group comparison is deliberately not shown:
      `merchant_report()` does not compute one, only a resolution-rate
      split, and showing a number nothing computes would be exactly the
      kind of invented figure this session's own instructions warned
      against.
    - **Business Insights** (new `BusinessInsights.tsx`): a deterministic,
      rule-based synthesis over data already shown elsewhere - recovery
      opportunities outstanding, incomplete inventory scans, low checkout
      success rate, best-seller-by-volume vs. best-seller-by-revenue
      divergence, cases waiting on a human - each insight carries the exact
      figure behind it and no insight fires without real evidence. No model
      call: every rule is a threshold over a number already computed by an
      existing route, which avoids both inventing advice and spending
      Groq-throttled model budget on something a deterministic rule
      answers just as well.
    - **Platform/Capabilities** and **Settings** (`PlatformCapabilities.tsx`,
      `StoreSettings.tsx`): extracted unchanged from the old single-page
      layout into their own sections - same data, same logic, verified
      identical behaviour. Settings gained one honest addition:
      `approval_timeout_minutes` is now shown read-only, with an explicit
      note that it is not currently changeable from the UI - audited first
      and confirmed `PUT /api/policy/{id}` (`set_policy`,
      `engine/api/routes.py`) re-saves the existing value unchanged
      regardless of what is sent, so no save control is offered for a
      setting that would silently do nothing.
    - **Merchant Copilot**: unchanged logic, now its own top-level section
      rather than always-on-screen. Spot-verified this session against two
      of the newly-surfaced domains - "What should I pay attention to
      today?" (correctly synthesized payment declines, pending recoveries,
      unmet demand and out-of-stock products from real, live figures) and
      "Which products generated the most revenue?" (returned the identical
      ranking, in the identical order, as the new Product Performance UI's
      revenue view) - not the full fifteen-phrasing checklist, since most
      of that surface was already verified in the session that built the
      Merchant Copilot (#32) and its later extensions.

    Six new merchant-scoped routes/repository functions this session:
    `db.checkout_conversion` + `GET /api/conversion`, `GET /api/products`
    (wrapping the already-existing `db.product_performance`),
    `GET /api/sales-trend` (wrapping the already-existing
    `db.sales_period_comparison`), `GET /api/unmet-demand` (wrapping the
    already-existing `db.unmet_demand`) - all SQL-aggregated, none pulling
    a full table into Python, none introducing a new arbitrary limit.

    **Real-client conditions, all four actually exercised, not assumed:**
    - *Zero data*: `?days=0` against a real merchant on `/api/conversion`,
      `/api/products` and `/api/sales-trend` all returned honest
      zero/null/empty shapes (`checkout_success_rate: null`, `has_data:
      false`, an explanatory `note`), never a fabricated 0%.
    - *Real volume*: exercised against Kettle's actual data - 380 checkout
      attempts, 1,481 recovery opportunities, 16 products scanned - not a
      demo-scale fixture.
    - *Unsupported capability*: Northfield's Recovery and Returns sections
      both render their honest unsupported explanation with no action
      control, confirmed live in a real Playwright browser session for
      both.
    - *Platform/API down*: not separately re-exercised for the four new
      routes this session, because none of them call the adapter at all -
      `checkout_conversion`, `product_performance`, `sales_period_comparison`
      and `unmet_demand` are pure reads over this engine's own ledger
      (`ExecutionAttempt`, `OrderLine`, `Case`), so a platform outage cannot
      break them by construction. This is a property of the design, stated
      here rather than left to be assumed: it does not need a platform-down
      test the way `InventoryPanel`'s catalog scan or `PaymentsPanel`'s
      approve action do (both already covered in earlier entries), because
      neither of those adapter calls is on this session's new code paths.

    **Verification.** `healthcheck.py` gained "Product performance",
    "Orders & conversion" and "Merchant surfaces are tenant-scoped" sections
    (7 new checks, all model-free) - 109 passed / 4 model-throttle-flaky
    failures on the run used for this entry (the four failures rotate
    between runs and are all model-proposal-selection checks unrelated to
    this diff - confirmed by running twice and seeing a different failing
    set each time, none of them ever a Merchant-surface check).
    `auditroutes.py` extended with the four new routes (refused without a
    key, accepted with it) and held at every probe. `fuzz.py` held every
    invariant across 20 sequences, run twice (once against the worktree,
    once against `main` after the code was copied back). `npm run build`
    clean both times. Live browser walkthrough (Playwright, not just the
    build guard) drove all fifteen nav sections for both Kettle and
    Northfield with zero JavaScript console errors, and separately dumped
    the honesty-critical sections' actual rendered text (Recovery, Returns,
    Payments & Checkout, Holdout for Northfield; Overview, Business
    Insights, and the revenue-view toggle for Kettle) to confirm real
    content rather than just "the panel didn't crash". One real bug found
    and fixed by this walkthrough: `Overview.tsx`'s pending-recovery label
    pluralized as "recoveryies" - fixed to "recovery"/"recoveries" and
    reconfirmed by rebuild.

    **What this session did NOT build, stated rather than left ambiguous:**
    a full session-to-cart-to-checkout funnel (needs new cart-creation
    instrumentation, not a new query - see Orders & Conversion above),
    return/refund capability itself (no adapter supports it - Returns
    states this rather than faking it), deep customer-level analytics -
    CLV, cohorts, segments, repeat-purchase tracking (no schema exists -
    Customer Insights states this rather than faking it), and
    `approval_timeout_minutes` mutability (backend does not currently
    support changing it - Settings shows it read-only rather than a fake
    control). All four are recorded in `PROGRESS.md` rather than implied
    finished by this entry's existence.

37. **Closed two real gaps left open by #36: the funnel and the settings bug.**

    **The Orders & Conversion funnel now has a real cart-creation stage.**
    Previously the only honest figure was checkout-attempt-to-completion,
    because nothing timestamped cart creation as a distinct event. New
    `FunnelEvent` model (`engine/db/models.py`) logs `CART_CREATED` at the
    exact moment `shop.py::create_cart` mints a cart - for guests and
    signed-in shoppers alike, unlike `ShopperCart` which only exists for
    accounts. `db.checkout_conversion` now returns `carts_created`,
    `abandoned_before_checkout`, and `cart_to_checkout_rate` alongside the
    existing checkout-attempt figures, plus `funnel_has_history` (false
    until at least one `FunnelEvent` row exists, so a merchant connected
    before this shipped sees an honest "not measured yet" rather than a
    confusing zero).

    **A real correctness bug found and fixed during this session's own
    verification, not shipped and left for someone else to find:** the
    first implementation compared `carts_created` (only counting carts
    created in the reporting window, which is small right after this
    instrumentation ships) against `checkout_attempts` (which includes
    every historical attempt, going back to before `FunnelEvent` existed) -
    two populations that don't correspond, producing a `cart_to_checkout_rate`
    of 38200% in manual testing. Fixed by joining on cart identity instead
    of comparing raw counts: `ExecutionAttempt.case_id` already stores the
    raw cart id for every `CHECKOUT` row (`idempotency.py::claim` sets
    `case_id=cart_id[:40]` - an existing fact about how that ledger keys
    itself, not a new column), so `checkout_conversion` now counts how many
    of *this window's own created carts* reached a checkout attempt at any
    time, which is bounded 0-100% by construction. Verified live: before
    the fix, a real API call returned `cart_to_checkout_rate: 38200.0`;
    after, the identical scenario returned `0.0` (a cart that hadn't
    reached checkout) and then correctly moved once a real end-to-end
    cart-to-checkout was driven through the actual routes. Two new
    `healthcheck.py` checks pin this: one asserting the rate is always in
    [0, 100], and a real before/after check that creates a cart, adds a
    line, checks out with a working card, and confirms `carts_created`
    moved and `abandoned_before_checkout` did not (a cart that reached
    checkout must not read as abandoned).

    Still explicitly not a sessions/visits funnel: this engine has no
    page-view event for a guest before they create a cart, so "how many
    people looked at the shop" remains unanswerable from existing data -
    stated in the `scope_note` field itself and in the Copilot's system
    prompt, not just a code comment.

    `OrdersConversion.tsx` rebuilt around this as a real funnel
    visualization (cart created → checkout attempted → completed, with the
    conversion rate at each arrow), rather than the flat figure row it was
    in #36.

    **`approval_timeout_minutes` was not just unexposed in the UI - it was
    silently broken.** Audited before touching anything: `PUT
    /api/policy/{id}` constructed a fresh `RiskPolicy` on every save without
    ever reading the connection's current timeout, so *any* unrelated
    settings change (switching automation mode, blocking one action) reset
    a merchant's configured timeout back to the field's hardcoded default
    of 15 minutes. This was a real, live bug affecting the one existing
    caller of that route, not merely a missing feature. Fixed in
    `engine/api/routes.py::set_policy`: `PolicyUpdate` gained an optional
    `approval_timeout_minutes` (bounded 1-120); when the request doesn't
    specify one, the route now reads `engine.policies.get(connection_id)`'s
    *current* value instead of falling back to `RiskPolicy`'s default.
    `StoreSettings.tsx`'s previously-read-only display is now a real
    input+Save control, matching the existing holdout-percent pattern.
    Verified live in a real browser: saved 22 minutes, confirmed it
    persisted, then switched the automation mode (an unrelated save) and
    confirmed the timeout stayed at 22 rather than silently reverting to
    15 - the exact bug this session fixed, reproduced and then confirmed
    fixed in one browser session before being reset back to 15 to leave
    the demo in its normal state.

    **Overview rebuilt into a real dashboard**, not a sparse report: a KPI
    row (total sales, completed orders, checkout success rate, AOV, each
    with a period-over-period comparison where defensible), a Business
    Health grid (six areas - Sales & Revenue, Orders & Conversion,
    Inventory & Catalog, Payments & Checkout, AI/Recovery, Returns - each
    computed client-side from the exact same fetched objects the dashboard
    already holds, so a status can never disagree with the section it
    summarizes; statuses are Healthy/Attention/Unavailable/Unsupported,
    never "Healthy" for a connection with no data), a top-products table,
    a needs-attention feed, summary cards, and an evidence-tagged
    opportunities list, all deep-linking into the relevant section. New
    `.kpi-card`/`.health-card`/`.status-badge`/`.summary-card`/
    `.opportunity-card`/`.funnel` CSS added to `styles.css`, reusing the
    existing token palette (`--surface`, `--line`, `--ok`, `--friction`,
    `--accent`) rather than introducing a second visual language.

    Verified live in a real browser for both merchants: Kettle's Overview
    showed a populated KPI row, six business-health cards with real status
    badges, and a working funnel view (77 carts created, 71.4% reached
    checkout, 47% of those succeeded, 22 abandoned, all internally
    consistent); Northfield's health grid correctly showed differentiated
    statuses per area (Healthy for sales/orders, Attention for inventory/
    payments with real counts, Unsupported for AI/Recovery and Returns) -
    not a uniform "everything's fine" or a uniform "everything's broken",
    which would each have been evidence of a fake/hardcoded status. Zero
    JavaScript console errors on either merchant.

    **The Merchant Copilot was extended to the new funnel data**, not left
    behind: `checkout_conversion` is now passed into `copilot.ask()` and
    `_build_context` (`engine/api/routes.py::merchant_copilot`,
    `engine/copilot/service.py`), with a new `SYSTEM_PROMPT` section
    explaining `carts_created`/`abandoned_before_checkout`/
    `cart_to_checkout_rate`/`funnel_has_history` and repeating the
    sessions-vs-visits limitation. Verified live: asked "How many carts
    were abandoned before checkout?" against Kettle, got "There were 22
    carts abandoned before checkout" - checked directly against
    `/api/conversion`'s own `abandoned_before_checkout: 22` for the same
    window, an exact match, confirming the Copilot and the UI cannot
    disagree because they read the identical repository call.

    **Verification.** `healthcheck.py`: 114 passed / 2 model-throttle-flaky
    failures on the run that added the new checks (confirmed non-
    deterministic across repeated runs, per the established pattern - a
    later run against `main` showed 112 passed / 1 flaky failure, a
    different check each time, never a Merchant-surface or funnel check).
    `fuzz.py`: every invariant held. `auditroutes.py`: held at every probe.
    `npm run build`/`npm run lint`: clean (same 7 pre-existing lint
    errors, none new). Live Playwright walkthrough covering Overview's new
    dashboard elements, the funnel view, and the approval-timeout fix, for
    both merchants, zero JS errors throughout.

    **What remains genuinely unfinished, not faked:** a true sessions/
    visits stage (would need a page-view event this engine has never had,
    for guests specifically - a materially bigger instrumentation project
    than a cart-creation log line); real Returns capability (still no
    adapter implements it); deep customer-level analytics (still no
    schema for CLV/cohorts/segments/repeat-purchase); and full
    page-by-page visual polish beyond Overview and Orders & Conversion -
    Sales & Revenue, Product Performance, Customer Insights, AI Commerce,
    Recovery, Holdout, Business Insights, Platform and Settings all still
    use the plainer card/list treatment from #36 rather than the fuller
    KPI-card/status-badge system built for Overview in this session. All
    recorded in `PROGRESS.md`.

38. **Fixed a real post-checkout cart bug reported by hand: adding a second
    product after a successful purchase failed with a red error.** Root
    cause traced end to end, not assumed frontend-only: `checkout()`'s
    success branch in `App.tsx` created a fresh cart via `api.createCart()`
    but never persisted it anywhere durable - `sessionStorage`'s
    `cv3_cart_{connection}` key kept pointing at the cart that had just
    been paid for, and (for a signed-in shopper) the server's own
    `account.cart_id` stayed on that same paid cart too, since the
    existing claim effect only claims a cart once it holds an item. Any
    remount (switching to the Merchant tab and back, or a reload) read the
    stale paid cart back in, and the backend correctly rejected the next
    add-to-cart with `409 CART_ALREADY_PAID` (`shop.py::_not_if_paid`) -
    which is what showed up as the red error line. The identical, already
    correct pattern existed a few hundred lines away: `onCartRetired`
    (the chat-driven checkout path) already wrote the fresh cart id to
    `sessionStorage`, and its own comment claimed the sidebar checkout did
    "the same" - it didn't. Fixed by making the sidebar checkout do what
    its neighbour already did (`sessionStorage.setItem`), plus one thing
    neither path did: for a signed-in shopper, immediately `claimCart` the
    new cart and update local account state, rather than waiting for the
    claim effect's item-count gate.

    Reproduced against the real running system before touching any code
    (a full guest-and-signed-in Kettle purchase, then a raw `add_line`
    call against the now-paid cart, returning `409 CART_ALREADY_PAID` -
    the exact bug), and reproduced identically on Northfield, confirming
    it was platform-independent frontend state, not an adapter quirk.
    Fixed, then re-verified the same way: on both merchants, a fresh cart
    created and claimed after checkout accepted a new line successfully,
    and a second full checkout on that new cart succeeded end to end (new
    order, no double charge, a retried checkout on the paid cart correctly
    returned `already_paid: true` on the same order rather than a new
    one). `npm run build`/`tsc` typecheck clean; `npm run lint` unchanged
    (same 7 pre-existing errors). `healthcheck.py` (111-113 passed across
    two runs, differing failures each time - all in payment-recovery
    wording and a webhook risk-outcome check, none touching cart/checkout/
    funnel logic, consistent with the documented Groq-throttle flakiness
    in CLAUDE.md's "The constraint", not a regression from this change),
    `fuzz.py` (1200 assertions across 20 sequences, twice, every invariant
    held both times), `auditroutes.py` (every route's lock and shopper
    scoping held). The diff is scoped entirely to `storefront/src/App.tsx`
    - no backend file touched, consistent with the root cause being
    React/sessionStorage state rather than anything server-side (fuzz.py's
    own output independently notes it cannot see this class of bug, since
    it lives in the browser rather than anything a server-side check
    reaches).

39. **Closed the payments/recovery slice of the Merchant Copilot's
    "read-only twin" gap: the copilot now points a merchant at the one
    real action it already has, instead of only ever answering.** Full
    audit first (subagent, read-only, against CLAUDE.md/PROGRESS.md/
    Completed.md and the actual code): confirmed Overview, Sales &
    Revenue, Payments & Checkout, Holdout, Platform/Capabilities and
    Settings are genuinely complete and connected end to end; confirmed
    Returns is correctly MISSING rather than faked (no adapter declares
    refund capability); confirmed Orders & Conversion's visitor/session
    stage is correctly BLOCKED BY DATA (no page-view event exists for a
    guest before cart creation - re-verified directly, not just accepted
    from docs); and independently re-verified `_scan_catalog`'s real-
    volume pagination fix by reading it, not trusting PROGRESS.md's own
    claim. The one concrete, tractable gap the audit converged on: the
    Merchant Copilot's catalogue/inventory/unmet-demand answers still have
    no attached action (correctly BLOCKED - no adapter can write
    inventory, so no fake action was invented for that), but the
    payments/recovery answers *do* have a real, already-built action
    sitting one section away (`PaymentsPanel.tsx`, #35) that the Copilot
    never once pointed a merchant toward.

    Deliberately did not build this from a fresh feature-spec on a whim -
    CLAUDE.md requires a spec before any roadmap build, and the payments
    slice already had every deterministic ingredient needed sitting in
    `copilot/service.py::ask()` (`recovery_pending`, computed before the
    model is ever called - real, not model-guessed). So the count is
    fetched independently by `MerchantConsole.tsx`'s `MerchantCopilot()`
    from the exact same `console_api.queue()` call and `RECOVERY_ACTIONS`
    filter `PaymentsPanel.tsx` itself uses (moved to `api.ts` as a shared
    export so the two can never drift apart), shown as a banner above the
    transcript - present before any question is asked, not gated on the
    model happening to mention it. Clicking it calls the same `onNavigate`
    pattern `Overview.tsx` and `Recovery.tsx` already use to jump straight
    to Payments & Checkout.

    Verified live end to end with a real Playwright script against the
    running Kettle merchant (not assumed from the diff): with zero pending
    recovery cases, no banner renders (checked directly - a stale case
    from an earlier check had already expired, confirmed by the API
    itself returning `[]`, proving the banner tracks live state rather
    than a cached figure). A fresh declined payment (test card `0002`) was
    driven through checkout to create one real `OFFER_ALTERNATE_PAYMENT`
    approval; the banner then read "1 payment recovery case is waiting for
    your approval," and clicking "Review in Payments & Checkout" set
    `aria-current` on that exact nav item. The case was then approved
    through the real route (`/api/approvals/conn_kettle/{id}`, the same
    one `PaymentsPanel` posts to) - recovered 1831.00 INR on order
    KB-0088, `executed.succeeded: true` - and a re-run of the same script
    confirmed the banner disappeared again, matching the now-empty queue.
    Northfield checked separately: zero pending approvals (it doesn't
    support recovery), so the banner correctly never appears there either
    - platform-capability difference respected without a single
    Northfield-specific line of code, because both merchants read the
    identical deterministic filter.

    Caught and fixed one lint regression before committing: exporting
    `RECOVERY_ACTIONS` from `PaymentsPanel.tsx` (a component file) tripped
    `react-refresh/only-export-components`. Moved the constant to `api.ts`
    instead, which both `PaymentsPanel.tsx` and `MerchantConsole.tsx` now
    import - `npm run lint` back to the same 7 pre-existing errors as
    `main`, `npm run build` (tsc) clean. `auditroutes.py` clean
    (unaffected by construction - no backend file in this diff).

    **Not claimed as closing the Merchant Copilot roadmap item.** The
    catalogue/inventory/unmet-demand slice of the same gap remains open -
    there is no real action this engine can attach to "restock this SKU"
    without fabricating a capability no adapter has, and CLAUDE.md
    explicitly forbids inventing one. `PROGRESS.md` updated to record the
    payments slice as closed and the catalogue/inventory slice as the
    remaining, harder half - needing either a genuinely new merchant-side
    capability (a to-do/reminder queue, not a fake platform write) or a
    deliberate decision that "point at the Inventory panel" is itself
    enough of an action, which is a product call CLAUDE.md's own process
    says belongs in a `feature-spec` pass, not a freehand build.

40. **Rebuilt Overview against a real Merchant SaaS dashboard reference,
    mapped honestly against what this data model can actually support -
    not a screenshot copy.** Full audit first: a design-vs-data matrix
    covering every element of the reference (header, KPI row, embedded
    Copilot, revenue trend chart, business health, top products, product-
    level attention, store-wide attention, commerce-health summaries,
    opportunities, platform capabilities), each traced UI → API →
    repository → source, with three genuine gaps surfaced and resolved by
    explicit user decision rather than silently:

    - **The reference's per-product "Conversion" column cannot be built.**
      `product_performance()` has no per-product view/impression data - no
      per-product funnel exists anywhere in this schema. Dropped, not
      faked.
    - **A "Returns rate increased" attention item cannot be built.** No
      adapter implements returns; showing a returns figure would be
      invented from nothing. Omitted entirely - Returns stays an honest
      unsupported state everywhere it appears.
    - **"Create promotion" / "Compare products" opportunity actions have
      no real destination.** No promotion-creation capability exists in
      this engine, and product comparison is a shopper-facing action
      (`COMPARE_PRODUCTS`), not a merchant tool. Dropped in favour of
      opportunities that link to real pages this session already verified
      (Inventory, Payments, Products).
    - **The reference's Revenue Trend line chart needed genuinely new
      backend work, confirmed before building anything**: no chart/SVG
      data-viz library exists anywhere in this codebase, and
      `sales_period_comparison` only ever returned two scalar window
      totals, never a daily series. Built rather than deferred, by
      explicit choice: new `db.daily_revenue_series()`
      (`engine/db/repository.py`) reuses `sales_period_comparison`'s
      identical `ExecutionAttempt` CHECKOUT/DONE/succeeded source in one
      query - a chart can never disagree with the report's own headline
      total or the trend figure, because it is a day-by-day breakdown of
      the same rows, not a second, differently-sourced count. Calendar-
      date bucketing throughout (not a `now`-minus-`timedelta` cutoff) so
      "last 30 days" includes today's partial day rather than silently
      dropping it - caught and fixed during this session's own live
      verification (today's real revenue was missing from the first
      version until the boundary was rewritten date-first). New
      `/api/sales-series/{connection_id}` route, `merchant_scoped()` like
      every other console route, added to `auditroutes.py`'s permanent
      probe list and confirmed refused without a key and across merchants.
      Frontend: `RevenueTrendChart.tsx`, a hand-rolled SVG line+area chart
      (no new npm dependency) - "This period" solid, "Previous period"
      dashed at the same day-offset, an honest empty state rather than a
      flat zero line when there's nothing to chart yet.

    **Copilot embedded directly on Overview**, not just linked to: the
    existing `MerchantCopilot` component (with #39's payments-recovery
    banner intact) was extracted from `MerchantConsole.tsx` into its own
    `MerchantCopilotWidget.tsx` - avoiding a circular import between
    `Overview.tsx` and `MerchantConsole.tsx` - and is now rendered live in
    the Overview layout, sitting beside the KPI row, while the dedicated
    "Merchant Copilot" nav section is kept for focused Q&A (a deliberate,
    smaller-blast-radius choice over removing that nav entry, stated
    explicitly rather than silently matching the reference's footer-link
    treatment).

    **Layout restructured to the reference's information hierarchy**
    (header with a real, functional date-range picker - 7/30/90 days,
    actually re-fetching every figure on this page, not decorative) → KPI
    row + Copilot → revenue trend + business-health list → a three-column
    row (Top performing products as a real revenue/orders table; Products
    needing attention, built honestly narrow - only out-of-stock/low-stock,
    the two conditions this schema can actually name per product; What
    needs attention, the existing store-wide feed) → commerce-health
    summary strip → Sell Better opportunities → Store/Platform. The
    now-honest "Cart → checkout rate" KPI replaces the ambiguous unqualified
    "Conversion Rate" the reference shows - labelled for what it actually
    measures (`conversion.cart_to_checkout_rate`) rather than implying a
    visitor-based denominator this engine has never had.

    **Verified live**, not just built: a real Playwright pass against both
    running merchants confirmed zero JS console errors, a real rendered
    SVG chart (not an empty-state fallback, since both have real revenue
    history), 6 business-health rows, a populated top-products table, the
    date-range picker re-fetching all figures without breaking on a range
    change, and the embedded Copilot panel present and functional.
    Screenshotted both merchants directly: Kettle correctly shows its own
    green theme with `ATTENTION` health items reflecting real declined-
    payment history and 2 real out-of-stock products; Northfield correctly
    shows its own blue theme, "recovery not supported on this platform" in
    the Store/Platform line, no recovery opportunity or attention item
    anywhere (capability-driven, not a Northfield-specific code branch, per
    the existing platform-independence pattern), 3 real out-of-stock
    products and one real 196-times-asked unmet-demand query. One layout
    bug found and fixed during this same verification pass: the KPI row's
    `auto-fit` grid tried to fit 4 cards across a column narrowed by the
    adjacent Copilot panel, wrapping the 4th card onto its own row -
    forced to a 2x2 grid in that specific layout slot.

    **Verification.** `npm run build` (tsc) clean, `npm run lint` back to
    the same 7 pre-existing errors as `main` (0 new). `auditroutes.py`
    clean including the new sales-series route (refused without a key,
    refused across merchants). `healthcheck.py` 113 passed / 3 model-
    wording-flaky failures - the same three, unrelated to anything touched
    here, consistent across this session's repeated runs. `fuzz.py` clean
    (1200 assertions, every invariant held).

    **What was deliberately not attempted this slice**, stated rather than
    implied finished: a merchant-account header badge/dropdown (no backing
    session/profile-switching functionality exists to make one real rather
    than decorative chrome); a content-completeness catalogue scan for
    "poor product content" (a real, buildable signal - `Product.description`/
    `image_url` can be null - but new backend work outside this slice's
    scope, not one of the three items the user explicitly ruled on); zero-
    data verification with a freshly seeded empty merchant (verified
    against both merchants' real, populated history instead - the honest-
    empty-state code paths for the chart and every other panel are written
    and match the existing pattern elsewhere in this file, but a live
    empty-merchant walkthrough was not performed this session). All
    recorded in `PROGRESS.md`.

41. **Corrected Overview's layout to match the planned Merchant dashboard's
    actual information hierarchy, after #40's first pass was rejected as
    reading like "many bordered boxes stacked vertically" rather than one
    coherent product.** Same data, same components reused - a pure layout/
    density/proportion correction, no new backend work.

    Restructured into the two-column hero the reference actually shows:
    a left column stacking the KPI row above a Revenue Trend + Business
    Health row, and a right column holding the Merchant Copilot as one
    tall panel spanning the full height of both rows combined - not
    squeezed into the KPI grid as #40 had it. KPI row forced to a real
    4-across grid (`repeat(4, 1fr)`, not `auto-fit`) so it can never wrap
    regardless of the Copilot column's width. Real bug found and fixed
    during this session's own verification: KPI values were overflowing
    their cards ("164666.8 INR" bleeding past the border) at the new
    narrower width - fixed by giving the currency code its own smaller
    line inside the value (`164666.80` / `INR`) rather than shrinking the
    number into illegibility or truncating it with an ellipsis (rejected -
    truncating a real figure is its own honesty problem, just a visual one
    instead of a data one). A second wrap bug found the same way (Northfield's
    "8888.11 INR" breaking mid-word into "8888.11" / "NR") was fixed by
    making the currency unit `white-space: nowrap` inside a wrapping flex
    container, so the two tokens wrap as whole units, never mid-word.

    Product-intelligence row changed from equal thirds to `1.6fr 1fr 1fr`
    so Top Performing Products (a real revenue/orders table) gets the
    width its data actually needs, matching the reference's proportions
    rather than an arbitrary equal split.

    Opportunities rebuilt from stacked full-width highlighted text rows
    into an actual card grid (`opportunity-grid`/`opportunity-card`) -
    bordered boxes, each with a title, real evidence line, and its own
    "View X →" button wired to `onNavigate`, rather than the whole row
    being one giant click target with no visible action affordance. A
    fourth real opportunity was added from data already fetched but not
    previously surfaced there (`demand.queries`, the same unmet-demand
    signal the attention feed already uses) - not new data, just not
    wasted.

    Commerce Health's six summary tiles reduced to five, matching the
    reference's named set (Inventory, Payments, Returns, AI & Commerce
    Outcomes, Holdout) - the separate "Recovery" tile was folded into "AI
    & Commerce Outcomes" (`"N helped · M recovered"`) rather than kept as
    a seventh, redundant figure the reference doesn't show separately.

    Added the header block the reference's Overview actually has and
    #40 omitted: a small "CV3 · MERCHANT" wordmark, the merchant's real
    display name (fetched from the existing public `/api/connections`
    list - the same source the shopper-facing header already reads, not a
    second invented source) rather than the technical platform id, a
    static subtitle line, and a merchant-identity chip (initials + name)
    beside the date-range picker - built from real merchant data, not a
    decorative account-switcher, since no profile-switching capability
    exists to make one real.

    **Verified live**, not just built, with a real Playwright pass against
    both running merchants after every layout change, iterating on actual
    rendered screenshots rather than code review alone - exactly the
    working method this correction was itself triggered by skipping.
    Confirmed both merchants: zero JS console errors, correct per-merchant
    theming preserved, correct capability differences (Northfield's AI/
    Recovery correctly `UNSUPPORTED`, no recovery opportunity card, no
    recovery figure folded into its AI-outcomes tile), no overflow or
    mid-word wrapping anywhere in the KPI row at 1600px width.

    **Real-transaction verification**, end to end: recorded Kettle's
    figures before (`173 orders`, `164666.80 INR`, AOV `2494.95`), drove a
    real guest→signup→checkout purchase through the actual running system
    (2× `KB-COL-02`, `2950.00 INR`), confirmed the report API updated
    correctly (`174 orders`, `167616.80 INR` - exactly `+2950.00`, AOV
    recalculated to `2501.74`), then confirmed the same three figures on
    the actual rendered Overview page after a fresh load matched the API
    response exactly, not approximately.

    **Verification.** `npm run build` (tsc) clean, `npm run lint` back to
    the same 7 pre-existing errors as `main` (0 new - this diff touches no
    component that previously had zero errors). `auditroutes.py` clean.
    `healthcheck.py` 111/113 passed, 2 model-wording-flaky failures (the
    same class seen on every run this session, unrelated to a pure CSS/
    layout diff that touches no backend file). `fuzz.py` clean (every
    invariant held).

    **What this does not change**: no new data, no new API, no new
    backend logic - `daily_revenue_series`, `merchant_report`,
    `checkout_conversion`, `product_performance`, `catalogAlerts`,
    `capabilities`, and the Copilot's own data all remain exactly as #40
    left them. This entry is a correction to how that same real data is
    arranged and sized, nothing more.

42. **Implemented MerchantTask, closing the catalogue/inventory/unmet-
    demand slice of the Merchant Copilot's "read-only twin" gap** - the
    harder half left open after #39 closed the payments/recovery slice,
    per the feature-spec written and approved earlier this session (no
    freehand build from the name alone).

    **New data model** (`engine/db/models.py::MerchantTask`): `task_id`,
    `connection_id`, `kind` (`OUT_OF_STOCK`/`LOW_STOCK`/`UNMET_DEMAND`),
    `subject_key` (product_id or normalized query), `label`, `detail`
    (JSON snapshot), `state` (`OPEN`/`RESOLVED`/`DISMISSED`), timestamps,
    `decided_by`. `UNIQUE(connection_id, kind, subject_key)` is the
    idempotency mechanism - modeled directly on `Approval`'s own
    conventions. No `ActionType` was added, and nothing under
    `engine/risk/` or `engine/execution/` was touched - confirmed by diff,
    not just asserted - because a task never modifies merchant inventory,
    a cart, or an order; it is a CV3-owned record that a person handled a
    real, flagged problem, not a commerce action needing risk
    classification.

    **New repository functions**: `sync_merchant_tasks` (deterministic,
    zero model calls - diffs live `catalog_alerts`/`unmet_demand` against
    open tasks, create-if-absent/refresh-if-still-open, never reopens a
    decided task even if the same fact persists - a named limitation, not
    hidden), `list_merchant_tasks`, `merchant_task_counts`
    (`tasks_open_count`/`tasks_resolved_recent_count` - genuinely new
    figures, not a repurposed existing one), `decide_merchant_task`
    (idempotent exactly like `decide_approval` - re-deciding an
    already-decided task returns `changed: False`, never a second,
    conflicting decision).

    **New routes**: `GET /api/tasks/{connection_id}` (syncs then returns -
    a merchant sees current tasks whether they've asked the Copilot
    anything or not) and `POST /api/tasks/{connection_id}/{task_id}/decide`,
    both `merchant_scoped()` like every other console route. Also wired
    into the existing Copilot route, so a Copilot question triggers the
    identical sync. Added to `auditroutes.py`'s permanent probe list.

    **New UI**: `TasksPanel.tsx` (new "Merchant Tasks" nav item, Open/
    Resolved/Dismissed filter, Resolve/Dismiss per card) and the Copilot's
    `actionBanner` prop widened from a single banner to a list
    (`actionBanners`, `Copilot.tsx`) so the payments-recovery banner from
    #39 and a new task-count banner can coexist without one replacing the
    other.

    **Verified live end to end**, not just built: real tasks appeared from
    real data with zero model involvement - Kettle got two genuine
    `OUT_OF_STOCK` tasks from its actual out-of-stock catalogue; Northfield
    independently got seven `LOW_STOCK`, three `OUT_OF_STOCK`, and one
    genuinely-earned `UNMET_DEMAND` task (198 real asks for "trainers",
    correctly clearing the >=3 threshold). Resolved one via the real API,
    confirmed `changed: true`; resolved it again, confirmed `changed:
    false` (idempotency); re-ran the sync, confirmed the resolved task
    stayed resolved rather than reopening even though the product was
    still out of stock (the stated limitation, working as designed).
    Cross-merchant and no-key requests both refused with 401. Then
    verified the same lifecycle in a real browser via Playwright against
    Kettle: Overview's Copilot banner correctly read "1 merchant task
    needs your attention," the Tasks panel showed the one real open task,
    clicking Resolve moved it to the Resolved filter (2 resolved tasks
    visible - the one resolved via API earlier plus this one), and the
    Overview banner disappeared once no tasks remained open. Zero JS
    console errors throughout. Screenshotted the panel directly - visually
    consistent with the rest of the design system (same panel/badge/card
    conventions as `PaymentsPanel.tsx`), not a bolted-on afterthought.

    **Verification.** `npm run build` (tsc) clean. `npm run lint`: one new
    instance of the identical pre-existing pattern already accepted in
    `PaymentsPanel.tsx` (`refresh()` called synchronously in an effect) -
    not a new class of problem, stated honestly rather than silently
    absorbed into "no new errors." `auditroutes.py` clean including the
    two new routes. `healthcheck.py` 111/116 passed - all 5 failures from
    classes already diagnosed as model-wording-flaky or order-dependent
    earlier this session, none touching this diff. `fuzz.py`: see below.

    **Not built, per the approved spec's own scoping**: no content-
    completeness scan feeding a `POOR_CONTENT` task kind (would need a new
    catalogue-content scan, separate work); no automatic reopening of a
    decided task if the underlying fact changes (accepted limitation,
    matching `decide_approval`'s own "decided once" behaviour); Tasks is
    not yet cross-linked from Inventory & Catalog or Customer Insights
    (only reachable from its own nav item and the Copilot banner) - a
    real, if minor, discoverability gap for a future slice.

43. **Rebuilt Sales & Revenue to Overview's visual tier - its own
    analytics-oriented information architecture, not a copy of Overview.**
    Audited first: the page was functionally correct (real
    `ExecutionAttempt`-sourced totals, correctly scoped narrower than
    Overview per its own docstring) but visually at the older #36 tier -
    three stacked `headline-figure` blocks, no chart, no date-range
    control, no product breakdown.

    **A real, previously-invisible data-honesty bug found and fixed while
    building this page, not assumed correct from the code**: `total_sales()`
    computes `average_order_value` as `total / priced_count`, where
    `priced_count` is only orders whose `ExecutionAttempt.result` actually
    stored `amount_paid` - a real, intentional, documented distinction
    (older rows written before `payment_settled` started storing it still
    count toward `completed_order_count` but not toward the money
    figures). Nothing exposed `priced_count` anywhere, so both this new
    page and Overview showed "174 orders" beside "AOV 2700.61" with no way
    for a merchant (or a reader of the code) to know those numbers
    describe different populations (2700.61 x 174 is nowhere near the real
    180941.00 total - it's 2700.61 x 67, the priced subset). Fixed by
    adding `priced_order_count` to `total_sales()`'s and `merchant_report()`'s
    return dicts and showing an honest caveat ("Based on 67 of 174 orders
    with a recorded amount") under the AOV figure whenever the two counts
    differ - on **both** Sales & Revenue and Overview, since both display
    the identical figure and would otherwise mislead identically. Verified
    live: Kettle showed "Based on 67 of 174", Northfield "Based on 70 of
    158" - both real, both matching `priced_order_count` read directly
    from the API.

    **A second real bug found in the same pass**: Overview's KPI row
    called `console_api.report()` with no `days` argument, so its
    Revenue/Orders/AOV figures never actually respected the date-range
    picker built for it in #40/#41 - only conversion/trend/chart/products
    did. Fixed by threading `days` through (`console_api.report(days)`),
    and gave `console_api.report()` an optional `days` parameter
    (default 30, preserving every other caller unchanged).

    **New page**: KPI row (Revenue, Orders, AOV with the new caveat,
    Revenue Change - "Not enough data" shown honestly rather than a fake
    percentage when there's no prior-period data to compare against, never
    an invented delta), a real `RevenueTrendChart` fed by the existing
    `/api/sales-series`, a Top-by-revenue table beside it, a Top-by-
    quantity table (deliberately not merged with top-by-revenue - the
    value bar's "selling best is quantity, highest revenue is revenue"
    distinction stays visible rather than collapsed into one ambiguous
    list), a real Attention panel (revenue-down-vs-prior-period when true,
    and a genuinely new deterministic signal - product revenue
    concentration, shown only when one product is >=40% of window revenue
    and there are enough orders for the ratio to mean something), the
    existing recovered-revenue block gated on `supports_payment_recovery`
    exactly as before, and the embedded Merchant Copilot (reused, not
    duplicated).

    **Real transaction verification, end to end**: recorded Kettle's
    before-state (173 orders, 179346.00 INR, AOV 2717.36, Colombia Huila
    Washed at 57500.00 revenue), drove a real guest→signup→checkout
    purchase (1x Colombia Huila Washed, 1595.00 INR captured), confirmed
    after-state via the API (174 orders - exactly +1; 180941.00 -
    exactly +1595.00; AOV recalculated correctly against the unchanged
    67-order priced population) and then confirmed the identical figures
    on the actual rendered page.

    **Verified live on both merchants** via Playwright screenshots at
    1600px: correct per-merchant theming preserved, correct capability
    difference (Northfield shows no Recovered-revenue section at all,
    matching `supports_payment_recovery: false` - no Northfield-specific
    code, the same gate Kettle's page already used), real distinct
    concentration attention items on each (74% Kettle / 73% Northfield -
    coincidentally similar values, independently computed from each
    merchant's own real data), zero JS console errors, the date-range
    picker re-fetching all four data sources without breaking. One
    environment-only false alarm during this verification, recorded so
    the next session doesn't rediscover it: this specific dev machine's
    cold Vite/fetch cycle took ~8-10 seconds to settle on this worktree,
    long enough that two earlier verification passes (3-5s waits) saw a
    permanent "Loading..." and were nearly written up as a real bug before
    a longer wait proved the page resolves correctly - not a code defect.

    **Verification.** `npm run build` (tsc) clean. `npm run lint`: 8
    errors, identical to the post-MerchantTask baseline (0 new).
    `auditroutes.py` clean. `healthcheck.py` 112/116 passed - 4 failures,
    all from classes already diagnosed as model-wording-flaky or
    order-dependent earlier this session, none touching sales/report
    logic. `fuzz.py` clean (every invariant held).

    **Not attempted this slice**, per explicit scope: Orders & Conversion,
    Product Performance, Customer Insights, Inventory, Payments, AI
    Commerce, Recovery, Holdout, Business Insights, Platform, Settings all
    remain at the older #36 visual tier - each needs its own slice. The
    visitor/session funnel was explicitly out of scope and not touched.

44. **Brought Orders & Conversion to Overview/Sales & Revenue's visual
    tier, and added the one genuinely new capability it needed: period-
    over-period comparison for the funnel.** Audited first: the funnel
    itself (`checkout_conversion`) was already real and honest - cart
    creation genuinely instrumented via `FunnelEvent`, checkout-attempt-
    to-completion from the `ExecutionAttempt` ledger, the join that keeps
    `cart_to_checkout_rate` bounded 0-100% already correct, the
    sessions/visits limitation already stated rather than guessed. What
    was missing was purely visual (no header, no date-range control, no
    KPI row, no way to tell whether conversion is improving) and one real
    functional gap: no way to answer "has conversion changed over the
    selected period" - the exact question CLAUDE.md's roadmap and this
    session's brief both name.

    **Visitor/session funnel deliberately not built** - re-confirmed, not
    re-decided: this engine still has no page-view event for a guest
    before cart creation, and building one is a genuinely separate,
    larger instrumentation project (a new event type, wiring the
    storefront to emit it, a guest-identity model for a shopper who has
    never created a cart) that a prior session's explicit instruction
    already carved out of scope. The honest `scope_note` stays exactly as
    it was - no invented denominator anywhere on the rebuilt page.

    **New backend capability**: `checkout_conversion` gained an optional
    `compare: bool` parameter. Refactored the funnel's core query into
    `_conversion_window(connection_id, start, end)` - the identical join
    logic (including the `case_id`-truncation join that keeps
    `cart_to_checkout_rate` from ever exceeding 100%) now runs once for
    the current window and, when `compare=True`, once more for the
    equal-length window immediately before it, under a new `prior` key.
    Not two independently-written queries that could drift apart - one
    query shape, called twice. `compare` defaults to `False` and is
    additive (no `prior` key at all when omitted), so every existing
    caller (`Overview.tsx`'s health-status computation, the Merchant
    Copilot's `checkout_conversion` context) is unaffected - verified
    directly, not assumed, by confirming `'prior' not in response` on the
    default call. New `/api/conversion/{connection_id}?compare=true` query
    param, tenant-isolation re-verified directly on the modified route
    (cross-merchant and no-key both refused with 401).

    **New page**: header with a real 7/30/90-day picker, a 4-KPI row
    (Carts created, Checkout attempts, Completed orders, Cart→checkout
    rate with a real point-delta comparison badge - "+X pts vs. prior Nd"
    when prior data exists, an honest "No prior-period data yet" when it
    doesn't, never an invented percentage), the existing funnel
    visualization kept as-is (it already had a real, distinctive shape),
    a new **Biggest drop-off** panel - a real, deterministic signal
    (whichever of the two real stages, cart-abandonment or checkout-
    failure, lost more in absolute terms this window, each linking to the
    section that can act on it: Customer Insights for abandonment,
    Payments & Checkout for failures), a Funnel detail strip (the raw
    abandoned/failed/success-rate figures, kept visible rather than
    collapsed into just the "biggest" one), and the embedded Merchant
    Copilot (reused, not duplicated).

    **Real transaction verification, end to end, in isolation** (not
    read from a noisy concurrent test run): recorded Kettle's before-state
    (384 carts, 365 checkout attempts, 175 completed orders), drove a real
    guest→signup→checkout purchase, confirmed after-state via the API
    (385/366/176 - exactly +1 at every one of the three real stages).
    Then retried the identical checkout on the same now-paid cart and
    confirmed `already_paid: true` on the same order with all three
    figures unchanged (366/176) - the funnel's exactly-once guarantee
    holds under retry, not just under a fresh attempt.

    **Verified live on both merchants** via Playwright screenshots at
    1600px: correct per-merchant theming, real distinct figures on each
    (Kettle: 384/365/175, cart-abandonment biggest drop at 269 cases;
    Northfield: 329/265/160, biggest drop at 241 cases - independently
    computed, no shared code path beyond the identical capability-
    agnostic query), zero JS console errors, the date-range picker
    re-fetching without breaking, Northfield's Copilot task banner
    (11 tasks) rendering consistently across pages as expected.

    **Verification.** `npm run build` (tsc) clean, `npm run lint`
    identical 8-error baseline (0 new). `auditroutes.py` clean including
    the modified route with its new query param. `healthcheck.py` run
    twice: 113/116 then 112/116, different failure combinations both
    times - all from classes already diagnosed as model-wording-flaky or
    (for "a cart taken to a successful checkout does not count as
    abandoned" specifically) a `days=1`-window timing artifact from the
    suite's own concurrent cart-creation activity, previously documented
    as non-deterministic in an earlier session unrelated to that
    session's diff either. Not treated as a silent pass: the exact
    invariant this check protects was independently verified above via a
    clean, isolated real transaction with zero concurrent noise, and held
    correctly both times (the purchased cart did not appear as abandoned).
    `fuzz.py` clean (every invariant held).

    **Not attempted this slice**, per the stated execution order: Product
    Performance, Customer Insights, Inventory, Payments, AI Commerce,
    Recovery, Holdout, Business Insights, Platform, Settings all remain at
    the older #36 visual tier.

45. **Brought Product Performance to Overview/Sales & Revenue/Orders &
    Conversion's visual tier, added period-over-period comparison, and
    found+fixed a real, pre-existing product-data-integrity bug while
    verifying it end to end.** Audited first: `product_performance` was
    already authoritative (`OrderLine`, written once per succeeded
    checkout at the same call sites as the payment ledger, never for a
    decline), already honestly scoped (`lowest_performers_note`/
    `historical_note` state the real limitations rather than hide them),
    and already the Copilot's own source (no second, independently-
    written ranking). What was missing was purely visual (no header, no
    date-range control, no KPI row) and one real functional gap: no way
    to answer "what changed compared with the previous period" or "what
    share of revenue comes from the top products".

    **A real, previously-undiscovered bug found and fixed while verifying
    this slice's own payment/order-integrity requirement, not assumed
    correct from the code**: `db.idempotency.forget_payment` (called at
    `create_cart` specifically to let a recycled cart id - reissued after
    a merchant-backend restart, since the platform's own counter lives in
    its memory - be legitimately re-charged) only ever deleted the stale
    `ExecutionAttempt` payment-ledger row for that recycled id. It never
    touched `OrderLine`, whose `row_id` is derived from the identical
    payment key (`f"{key}:{i}"`). So a cart id's *second* real, paid life
    silently **overwrote** whatever `OrderLine` rows its *first* life had
    written - upserting by `row_id` rather than inserting fresh - instead
    of getting its own rows. Caught by hand: bought 2x a real product,
    the aggregate only moved by 1 unit / a fraction of the expected
    revenue. Traced directly in `cv3.db` to a single `OrderLine` row whose
    `created_at` was over a day stale, holding a mix of an old order's
    identity and the new purchase's quantity/revenue - the old order's
    real historical contribution had been silently destroyed. Fixed by
    having `forget_payment` also delete any `OrderLine` rows matching that
    same payment-key prefix, exactly mirroring how it already treats the
    payment ledger. **Verified the fix against the literal failure mode**:
    restarted the Kettle merchant backend to force the same cart id
    (`BAG-0001`) to recycle again, confirmed the stale `OrderLine` row was
    purged the moment the new cart was created (before any purchase),
    completed a fresh purchase of a *different* product on it, and
    confirmed the aggregate for the *original* product correctly dropped
    by exactly the amount the corrupted row had wrongly contributed
    (50/62500 from 52/65000 - the +2/+2500 this same bug injected earlier
    in this session), while the new product's own figures were exactly
    correct (order_count, quantity, revenue all consistent with one
    genuine new order).

    **New backend capability**: `product_performance` gained an optional
    `compare: bool` parameter, refactored via a new `_product_totals_window`
    (the identical per-product SQL aggregation, parameterized to an
    arbitrary window) run twice - current and, when asked, the equal-
    length prior window - under new `revenue_prior`/`quantity_prior`/
    `revenue_change_pct`/`quantity_change_pct` fields per row. A product
    with no prior-window sales gets a real `None` percentage ("new this
    period"), never a fabricated 0% or a divide-by-zero. A new
    `total_revenue` field (every distinct product's revenue summed, not
    just the top-N shown) supports an honest "top-5 revenue share" ratio
    against the true total. `compare` defaults to `False` and is additive
    - the Copilot's own call and every other existing caller are
    unaffected, confirmed directly (`'revenue_prior' not in` a default
    response).

    **New page**: header with a 7/30/90-day picker, a 4-KPI row (Distinct
    products sold, Top seller by units, Top by revenue, Top-5 revenue
    share), the existing three-tab ranked table (Best selling/By revenue/
    Lowest selling) rebuilt as a real table with a live "vs. prior" column
    (omitted for the Lowest-selling view, where a comparison isn't the
    point), and the embedded Merchant Copilot.

    **Payment/order-integrity, verified end to end for every required
    case**: successful purchase → exactly one correct `OrderLine`
    (quantity and revenue matched the real cart contents exactly).
    Declined payment (test card `0002`) → zero `OrderLine` rows. Retry of
    an already-paid cart → `already_paid: true`, the existing single row
    unchanged (no duplicate). Payment recovery (approved through the real
    risk-gated route) → exactly one paid `OrderLine` appeared only after
    the recovery succeeded, correctly quantified. Duplicate approval on an
    already-decided case → `changed: false`, `executed: null`, no
    re-execution, no second row.

    **Verified live on both merchants** via Playwright at 1600px: correct
    per-merchant theming, real distinct rankings, the revenue-view table's
    first row matching the "Top by revenue" KPI exactly (a genuine
    ambiguous-selector false alarm caught and resolved during this same
    verification - `page.click('text=By revenue')` was matching the KPI
    card's own label text "Top **by revenue**" before reaching the actual
    tab button; a scoped locator confirmed the app itself was correct all
    along), the Lowest-selling view correctly omitting the comparison
    column, zero JS console errors, tenant isolation re-verified directly
    on the modified route (cross-merchant and no-key both refused with
    401), zero-data (`days=0`) confirmed honest (`has_data: false`, empty
    arrays, real notes, `total_revenue: "0.00"`).

    **Merchant Copilot verified against the same authoritative data**, six
    real questions: "selling best" correctly ranks by quantity (with
    revenue shown inline, never conflated); "generate the most revenue"
    correctly ranks by revenue; "sold the least" correctly ranks by
    quantity ascending and explicitly labels itself "BY QUANTITY"; "which
    products have no recorded sales" is answered honestly ("I cannot
    confirm... the data does not provide that information") rather than
    guessed - no second, independently-written ranking exists in the
    Copilot's own prompt-building path.

    **Verification.** `npm run build` (tsc) clean, `npm run lint`
    identical 8-error baseline (0 new). `auditroutes.py` clean.
    `healthcheck.py` 111/116 passed - 5 failures, all from classes already
    diagnosed as model-wording-flaky or the funnel timing artifact earlier
    this session, none touching product performance (the two dedicated
    "Product performance" healthcheck assertions both passed). `fuzz.py`
    clean (every invariant held).

    **Not attempted this slice**: Customer/Shopping, Inventory & Catalog,
    Payments & Checkout, AI Commerce, Recovery, Holdout, Business
    Insights, Platform, Settings all remain at the older visual tier -
    each needs its own slice.

46. **Brought every remaining Merchant area to Overview/Sales & Revenue/
    Orders & Conversion/Product Performance's visual tier in one sprint:
    Customer & Shopping Insights, Inventory & Catalog, Payments &
    Checkout, Returns, AI Commerce, Recovery, Holdout, Business Insights,
    Platform & Capabilities, Settings, and Merchant Tasks.** This closes
    the last open item from #45 - every section in the Merchant
    navigation now shares the same header/KPI-row/panel/embedded-Copilot
    system, rather than eleven pages at the older #36 plain-card tier
    and four at the newer one.

    **Audited before changing anything**: every one of these eleven
    pages already read from real, authoritative data with honest zero-
    data/unsupported-platform handling (Returns' and Payments &
    Checkout's "not supported on this platform" states, Holdout's "no
    holdout sessions yet", Inventory's reachable/complete/truncated
    honesty fields) - none needed new backend work or a data-model
    change. The gap was purely visual (no header, no KPI row, the older
    `.panel`/`.figures`/`.frictionrow` treatment) and one piece of
    missing wiring: none of the eleven had the Merchant Copilot embedded
    on the page itself, unlike every one of the four already-finished
    areas.

    **What changed, per page**: added `.overview-header` + subtitle,
    a `.kpi-row` of real figures (no new numbers invented - each KPI is
    an existing field from the same API call the page already made,
    surfaced instead of buried in a `.figures` strip), kept each page's
    existing panel content and its own honest empty/unsupported states
    verbatim, and added `<MerchantCopilot onNavigate={...} />` in an
    `.overview-copilot-slot` at the foot of every page. `MerchantConsole.tsx`
    now threads `onNavigate` through to all eleven components
    (previously only Overview/Sales/Orders/Products/Recovery had it).
    `TasksPanel` additionally got its filter tabs moved into the new
    header row instead of the panel head.

    **Real transaction / cross-page-consistency verification**, per
    this sprint's explicit priority ("the same real transaction must
    produce consistent results throughout Merchant" - CLAUDE.md's cross-
    page-consistency requirement): a scratch script
    (`scripts/verify_merchant_final.py`, gitignored) drove, against the
    live engine and both live merchant backends:
    - a real Kettle guest→signup→cart→checkout purchase with a
      succeeding card, confirming `completed_order_count` (report),
      `completed_orders` (conversion), and the purchased product's
      revenue (product performance) all moved by exactly the expected
      amount from the same transaction;
    - an idempotent retry of the same checkout on the now-paid cart with
      a *different* card, confirming zero new orders and zero revenue
      movement (the existing cart-keyed ledger held, unmodified by this
      sprint);
    - a real decline (test card `0002`) → simulate → recovery-approval
      flow, confirming `recovery_count` and `completed_order_count` both
      moved by exactly one, and a *second* decline's recovery *rejected*
      confirming neither figure moved;
    - a real Northfield purchase, confirming its `completed_order_count`
      moved correctly while `recoverPayment` stayed reported as
      unsupported and no recovery UI was exposed for that platform.
    All of the above passed clean on a final isolated run (`NO ISSUES
    FOUND`).

    **A red herring investigated and ruled out, not shipped**: several
    earlier runs of the same script, interleaved with other scratch
    diagnostic scripts hitting the same shared demo database in rapid
    succession, appeared to show `completed_order_count` failing to
    move immediately after a genuine, confirmed-in-the-database
    successful checkout. Chased directly: confirmed the write itself
    was always correct and immediate (raw queries against
    `ExecutionAttempt` from a fresh process always showed the true,
    current count); tried enabling SQLite WAL mode and, separately,
    forcing `NullPool` (no connection reuse) in `engine/db/session.py`
    as candidate fixes for a suspected pooled-connection-snapshot
    staleness - neither changed the behavior, and clean isolated re-runs
    afterward (and on the original, unmodified code) reproduced the
    correct figure immediately every time. Concluded this was noise from
    running many overlapping scratch scripts against one shared SQLite
    file within the same few seconds, not a real defect - both
    speculative `session.py` changes were reverted before committing,
    leaving the module exactly as it was. Flagged here rather than
    silently dropped, in case a future session sees the same symptom
    under similarly heavy concurrent local script load.

    **Verified live on both merchants** via Playwright at 1400px and at
    400px (narrow) for a sample of the changed pages: all sixteen
    Merchant nav sections load with zero console/page errors and non-
    empty content for both `conn_demo` and `conn_kettle`; no horizontal
    overflow at 400px on the checked pages (Payments & Checkout,
    Business Insights, Platform & Capabilities, Merchant Tasks, Customer
    & Shopping Insights - the pages using a `gridColumn: span` KPI
    card); Northfield's Platform & Capabilities correctly shows
    `recoverPayment` unsupported with its adapter-supplied reason;
    Merchant Tasks' Resolve/Dismiss actions and Settings' holdout/
    approval-timeout saves re-confirmed still working through the new
    header layout.

    **Verification.** `npm run build` (tsc) clean. `npm run lint`
    identical 8-error baseline, 0 new (the two touched files that
    already carried the pre-existing "setState in effect" pattern,
    `PaymentsPanel.tsx` and `TasksPanel.tsx`, were not modified in that
    respect). `auditroutes.py` clean (every locked route, shopper-
    scoping, and public-route check passed - no route surface changed
    this slice). `fuzz.py` clean, 1200 assertions across 20 sequences,
    every invariant held. `healthcheck.py` 113/116 passed - the 3
    failures ("escalates when the platform cannot help", "the over-
    promise is replaced", "a successful recovery still explains what it
    did not do") are all chat-reply-wording checks against the model's
    output, the class already diagnosed elsewhere in this file as
    Groq-throttle/model-variance flaky - none touch a Merchant-console
    route or figure this slice changed.

    **Not attempted this sprint**: a true visitor/session funnel stage
    (still explicitly out of scope, per #44); deep customer analytics
    (CLV, cohorts, segments - still no schema to support them honestly,
    per #36); real Returns capability (no adapter declares one); a
    mobile-optimized navigation shell (the left-nav sidebar itself does
    not collapse below ~720px - pre-existing from #36, not touched here,
    and no page's own content overflowed at 400px as a result).
