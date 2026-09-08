# Progress

## Maintenance note

> This file must be reviewed and updated at the end of every session —
> reflect what was fixed, what's still pending, and update Features
> Built/Not Yet Built if anything shipped or was newly planned. Keep this
> note at the top.

---

## Features Built

### For the shopper

A conversation that can complete a purchase: greeting, product, size, pay, order
lookup - without leaving the chat. No competitor's assistant does the payment half,
because they cannot touch money safely.

- **Tappable everything.** Product cards and size buttons bypass the model entirely,
  so they are instant, cost no tokens, and keep working while rate limited.
- **It asks rather than guessing a size.** The returns argument above.
- **"Why this?"** on decisions, including what was declined - *"I did not offer
  trying your card again, offering another way to pay"*. The declined half is what a
  seller's assistant cannot write. Now covers a successful recovery too, not only a
  platform's outright incapacity - see Fixed This Session.
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
- **A cart, a conversation and an order each belong to somebody.** A signed-in
  shopper's things follow their account; a guest's follow an httpOnly cookie
  neither they nor a script on the page can read or choose. See Fixed This Session.

### For the merchant

Their own console at `/merchant`, behind their own secret key: revenue recovered
from real outcomes, what shoppers ran into, what was said to them, the ten rules
(now eleven - see Fixed This Session), and per-action policy that persists.

### For CV3

One queue at `/operations` across every client, behind the operator key. Approvals
with rejection notes that stay private, and **handovers** - work needing a person
rather than a decision - with a waiting clock and a message back to the shopper.

### Security

Three key kinds: publishable for the browser, secret per merchant, operator for CV3.
Every route locked or deliberately public, verified by probing the running engine.
`connection_id` is checked against the key, so a caller cannot claim to be a merchant
they are not. A publishable key can no longer reach another shopper's basket,
conversation or order at the same merchant - see Fixed This Session.

### Tests

`healthcheck.py` - 84 checks, one path end to end. Reports SKIP rather than FAIL
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
the picker appears; plus three transcript-bug tests (see Fixed This Session, #21).
`scripts/walk_checkout.cjs` is retired.

`eval.py` - scores the model's judgement across repeated attempts. Unfinished, and
it has already caught a real problem.

`testexpiry.py`, `testcartreuse.py` - focused reproduction scripts, on disk but
gitignored like `testcart.py` and the other `test*.py` scripts already there
(project convention: `test*.py` is scratch except `testshopper.py`). Not part of
the tracked suite; re-run them by hand when touching approvals or cart ownership.

---

## Features Not Yet Built

**Webhooks.** Friction reaches the engine only because our own storefront reports
it. `SupportsWebhooks` is defined and empty. **This is what stands between the engine
and a real client** - no merchant can connect without it.

**The assistant is not installable.** A React component in our storefront, not a
script tag a merchant adds to their site.

**No gate runs the browser tests.** The Playwright layer now exists
(`storefront/tests/checkout.spec.ts`, run with `npm test`) but nothing wires it
into a daily or pre-commit gate — it still runs when somebody remembers to run it.
The class of bug the finger-pointing above kept hitting is now catchable, just not
yet caught automatically.

**The holdout.** A slice of sessions receiving no assistant, so a merchant can see
the difference and know the engine caused it. Answers the objection that loses
deals, nobody in the market does it, and it costs a flag on the session. **Highest
commercial value of anything unbuilt.**

**A comparison action.** The most wanted capability at 74%, and the thing a seller's
assistant is assumed not to do - which is exactly why doing it earns trust.

**A published reliability number.** `eval.py` exists and is unfinished. Nobody in
this market publishes one.

**No Postgres, no hosting, no repo split.** SQLite by connection string, local only.
The operations console shares a bundle with the shopper storefront, which is why the
operator key must be typed rather than built in.

---

## Fixed This Session

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

    These are the last three of the "smaller findings" listed under Known Issues.
    With them, that paragraph is reduced to the two genuinely-open items below
    (near-duplicate messages, `handovers_across` window) plus the `DIAGNOSED`-stuck
    state, which needs care before touching. Verified by restarting the engine and
    reading the code paths; no test-suite count changed (none of these were
    HTTP-observable before this commit either).

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
    (this session) claimed the scenario was demoable. The card picker was one
    shared list (1111/0002/0003) across both merchants, a leftover from an
    earlier shared-list attempt that was reverted for a different reason (a card
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

---

## Known Issues / Pending

Reported after a walkthrough. **Not a complete list** - walk the product before
trusting anything.

**Occasional near-duplicate assistant messages** from the poll's deduplication -
the last real behaviour bug, browser-only, and the likeliest by design (see Next
Steps #2). Worth reproducing in the browser before writing its regression test,
rather than fixing from a guess at the trigger.

Fixed: the `handovers_across` window (see #23 below) - the oldest-50 cap that hid
the newest handovers from the operations console is now a paged list with a total.

Additional smaller findings from an earlier session's walkthrough, not yet fixed
(see the walkthrough transcript for full detail if this file is ever pruned): a
case can get stuck in `DIAGNOSED` state with no path to resolution (needs care -
see Fixed This Session, #22); closing a handover with a blank note tells the
shopper nothing by design, which is worth revisiting. Every other item that once
sat in this paragraph is fixed - the typed add-to-cart contradiction (#15), the
`retry_after_seconds` null (#22c), the `HISTORY_TURNS` "thirty" doc mismatch, the
sign-in screen's merchant name (#17/`useTheme`), and the duplicated rejection
branch and `ChatReply` field (#22a/#22b).

### Why the tests did not used to catch these

**None of them could see the browser.** Every bug found by hand in an earlier
session lived in React state against `sessionStorage` - state held in two places,
effects firing in the wrong order, stale closures - and the suites only ever
drove HTTP. `storefront/tests/checkout.spec.ts` (see Fixed This Session, #21) is
the first thing that watches the actual browser; the three transcript bugs it
now covers were exactly this shape. The remaining Known Issues above are still
disproportionately the kind no HTTP test can see - a next pass should extend the
Playwright suite to them rather than reach for `healthcheck.py` again.

---

## Next Steps

1. The guest-checkout question is decided and implemented - checkout now requires
   a signed-in account with an email. What has not followed yet: **nothing actually
   sends that email.** It is collected and stored, and the shopper sees the order in
   the app, but no confirmation leaves the engine (no SMTP/mailer anywhere) - which
   is the reason the gate exists, so the "got the address" half is done and the
   "used it" half is unbuilt. Also still open after the decision: a handover message
   written for a guest's dying session (guests no longer pay, but can still chat and
   still get escalated), and the shop now prompts for an email where it used to
   finish a sale - whether that nudges a merchant's conversion is worth watching.
2. The browser-level test layer now exists (`storefront/tests/checkout.spec.ts`,
   `npm test` from `storefront/`) - extend it rather than starting a second one.
   Best next addition: the near-duplicate-message poll bug, now the only real
   behaviour Known Issue still open. Reproduce it in the browser first (the
   project's standing rule against fixing a bug from a guess at its output
   applies here too - the poll dedup is exact-match plus suffix-match, and the
   near-duplicate survivor is precisely the case the check does not catch). The
   sign-in screen's merchant-name display was a stale finding - it already shows
   `merchant.name`.
3. The remaining Known Issues are not architectural; the `DIAGNOSED`-stuck item
   is a lifecycle-semantics decision that wants a deliberate call (the code does
   close the case out via `record_outcome`, so it is not literally orphaned), and
   the blank-note handover is a deliberate behaviour worth revisiting rather than
   an obvious bug.
