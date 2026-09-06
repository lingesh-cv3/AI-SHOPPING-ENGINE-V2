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
- **Accounts.** Sign up, sign in, sign out, per merchant. bcrypt, server-side
  sessions, httpOnly cookie, failed attempts rate limited per username.
- **Memory that survives closing the tab.** A signed-in shopper's conversation and
  basket both follow them. Thirty turns stored; the model reads the recent part,
  because tokens are the binding constraint.
- **Guest checkout.** Deliberate - see the disagreement below.
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

`healthcheck.py` - 74 checks, one path end to end. Reports SKIP rather than FAIL
when the provider is busy, and says how many checks never executed.

`fuzz.py` - random shopper sequences, asserting after every step that the cart
matches what was added and removed, a paid cart is never chargeable again *on any
card* (not just the one that paid it), and no merchant's session, cart or orders
are reachable from another. Model-free. Prints its seed so a failure is
reproducible.

`auditroutes.py` - probes the running engine: refused without a key, accepted with
its own, refused another merchant's, and refused another shopper's cart,
conversation and order even when both hold the identical publishable key.

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

**No browser-level tests.** Still the largest gap - see "Why the tests did not
catch these" under Known Issues.

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

---

## Known Issues / Pending

Reported after a walkthrough. **Not a complete list** - walk the product before
trusting anything.

**Guest checkout is a requirement disagreement, not a bug.** It was built so a
shopper can browse and buy without an account, on the reasoning that forcing signup
loses sales. The product owner wants sign-in required. Decide it explicitly rather
than "fixing" a deliberate decision.

**Guest checkout collects no email.** A guest can buy and the order can reach
nobody. A handover message written by an operator goes into a session that dies with
the tab.

**Option buttons do not survive a reload.** Restored turns are text only, so
refreshing mid-choice leaves a question with nothing to tap.

**Occasional near-duplicate assistant messages** from the poll's deduplication.

**A long testing session shares one demo database with no easy reset for
accumulated backlog.** `handovers_across` returns only the oldest 50 unhandled
cases; after enough manual testing the newest ones fall outside that window and
read as "gone" until the old ones are closed through the ops API. `demo_reset.py`
does not clear this - it only adds curated activity on top. Worth a "close
everything older than N days" operator action, or pagination on the handovers
list, before a real demo.

Additional smaller findings from this session's walkthrough, not yet fixed (see the
walkthrough transcript for full detail if this file is ever pruned): a typed
add-to-cart message can contradict itself and add nothing; the transcript writes a
fabricated shopper turn ("My card was declined.") that was never actually typed;
tapping a size option records the shopper as having said the bare label ("8")
rather than a real sentence; `retry_after_seconds` is wired through the API and the
storefront but the field that should populate it is never actually set, so it is
permanently null; `HISTORY_TURNS` (14) does not match this file's own claim of
thirty; a case can get stuck in `DIAGNOSED` state with no path to resolution;
closing a handover with a blank note tells the shopper nothing by design, which is
worth revisiting; the sign-in screen shows a connection id or platform name rather
than the merchant's actual name; a rejection branch pasted three times in
`routes.py`, duplicate field declarations in `ChatReply`.

### Why the tests did not catch these

**None of them can see the browser.** Every bug found by hand recently lived in
React state against `sessionStorage` - state held in two places, effects firing in
the wrong order, stale closures. A browser-level test would have caught all of them,
and its absence is the largest gap in this project. This session's fixes were all
backend-reachable over HTTP and so could get a real automated test; the ones left in
Known Issues are disproportionately the ones that are not.

---

## Next Steps

1. Decide the guest-checkout requirement question explicitly (sign-in required or
   not) before touching anything downstream of it - email collection, order
   notification, and the handover-message-into-a-dying-session problem all follow
   from that decision.
2. Consider a browser-level test layer before trusting any future frontend fix
   without one - this session's own backend fixes could all be verified
   automatically; the remaining known issues mostly can't be, for the same
   underlying reason.
3. Work through the smaller findings listed under Known Issues in whatever order
   next picks up this file - none of them are architecturally risky, they just
   didn't get to this session.
