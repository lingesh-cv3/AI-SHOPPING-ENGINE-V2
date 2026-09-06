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

`healthcheck.py` - 72 checks, one path end to end. Reports SKIP rather than FAIL
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
row. Storefront typecheck is unchanged at 10 pre-existing errors throughout (see
Known Issues) - none of this session's fixes touched those lines.

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

---

## Known Issues / Pending

Reported after a walkthrough. **Not a complete list** - walk the product before
trusting anything.

**Guest checkout is a requirement disagreement, not a bug.** It was built so a
shopper can browse and buy without an account, on the reasoning that forcing signup
loses sales. The product owner wants sign-in required. Decide it explicitly rather
than "fixing" a deliberate decision.

**`/merchant` and `/operations` do not carry the merchant in their address.** They
read it from `sessionStorage`, so which client's console you get depends on which
shop you last visited. `/northfield/merchant` is the fix, and `/signin` has the same
problem.

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
thirty; the merchant report's "shoppers helped" figure counts cases, not distinct
shoppers; a case can get stuck in `DIAGNOSED` state with no path to resolution;
closing a handover with a blank note tells the shopper nothing by design, which is
worth revisiting; the sign-in screen shows a connection id or platform name rather
than the merchant's actual name; `npm run build` fails on 10 pre-existing
TypeScript errors (`tsc -b` only - `vite dev` is unaffected); several code-quality
items (dead code in `switchMerchant`, a rejection branch pasted three times in
`routes.py`, duplicate field declarations in `ChatReply`, an unreachable `Landing.tsx`).

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
2. Route the merchant/operator consoles by address (`/northfield/merchant`) rather
   than `sessionStorage`, and fix `/signin` the same way.
3. Consider a browser-level test layer before trusting any future frontend fix
   without one - this session's own backend fixes could all be verified
   automatically; the remaining known issues mostly can't be, for the same
   underlying reason.
4. Work through the smaller findings listed under Known Issues in whatever order
   next picks up this file - none of them are architecturally risky, they just
   didn't get to this session.
