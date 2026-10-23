# Payment-safety investigation: ambiguous outcomes recorded as definite

Status: **OPEN - RELEASE BLOCKER. REPRODUCED on an isolated local sample merchant (2026-10-23). NOT FIXED.**
Opened 2026-09-30 from a code read; reproduced 2026-10-23. **No payment code has been changed** (ledger, execution, pay routes,
adapters, merchants). This is deliberately separate from the Operations work and from Shopper V2; the fix needs its own spec, tests
and `invariant-guard` review (invariants 2, 3, 5 and 7) before any change is made. **Checkout and payment are not production-ready.**

## The question

When a money-touching call ends in a timeout or transport/5xx failure, the platform may or may not have acted. Does the engine ever
record that as a definite "nothing happened", and can that let the same basket be charged twice?

**Answer (reproduced locally): yes.** After a timeout, a 5xx, or an abandoned request, the pay routes release the cart-level ledger
row and tell the shopper "Nothing has been charged" although the platform had recorded the charge. Paying again with a DIFFERENT card
then produced a second confirmed purchase for the same basket.

## Reproduction (what was run, and what it proves)

Harness and evidence are kept OUTSIDE this repository, on the investigator's machine, under `D:\CV3\payment_repro\`:
`SPEC.md` (the reviewed plan), `FINDINGS.md` (the write-up), `charge_proxy.py`, `engine_wrapper.py`, `payrepro.py`, and the evidence run
`runs/pay-20261023-133641/` (`report.json`, `proxy.jsonl`, engine/merchant logs). They are not committed; whether to bring the harness
into the repo as a regression test is a decision for the owner.

**Isolation (verified before the first purchase request and again after):** a disposable byte copy of `cv3_v2verify.db` under the
harness directory, bound by a fail-closed engine wrapper that asserts the bound database before serving; the local Northfield sample
merchant only, reached through a loopback fault-injecting proxy (it FORWARDS the purchase so the platform really records it, then
withholds the response or returns 503); Kettle unreachable; fake cards only; provider keys, SMTP and proxy variables blank; a continuous
egress monitor found no non-loopback connection; the protected databases (`cv3.db`, `cv3_v2verify.db`, the dated backup) were identical
by hash, size and mtime before and after. No real payment provider, credential or customer data was involved.

**Results (platform truth = the merchant's own purchase records; idempotency keys from the proxy log):**

| Scenario | What the engine told the shopper | Platform purchases for ONE basket | Verdict |
|---|---|---|---|
| S1 timeout, then the SAME card | "Nothing has been charged" (false: it had been charged) | 1 (`ORD00001`; the same key replayed 3x; the second tap settled as `ORD00001`) | no double charge, but a false statement |
| S2 timeout, then a DIFFERENT card (chat `/pay`) | "Nothing has been charged" | **2 confirmed** (`ORD00002` key `pay-BSK00003-1111`, `ORD00003` key `pay-BSK00003-2222`) | **double purchase** |
| S3 5xx, then a different card (REST checkout) | `UPSTREAM_ERROR`, retryable | **2 confirmed** (`ORD00004`, `ORD00005`) | **double purchase** |
| S4 client abandons mid-request, then a different card | (client gone) | **2 confirmed** (`ORD00006`, `ORD00007`) | **double purchase** |

Other observations: in S4 closing the client connection does NOT cancel the handler; the ledger row was `IN_FLIGHT` right after the
abandon and was released about 30 s later when the handler finished (while `IN_FLIGHT`, `begin_payment` would have refused a second tap,
so that window is protected). The injected 503 body text did not reach the response; the REST error shows the engine-translated
`{"code":"UPSTREAM_ERROR","message":"platform returned 503","retryable":true}` (it exposes the status number, not platform detail).

## What is confirmed, and what is NOT

**Confirmed by the reproduction (local, sample merchant):**
- the engine-side behaviour: `release_payment` on a timeout or 5xx, the "Nothing has been charged" reply, and a fresh platform
  key per card;
- that with a platform that does not guard a basket against a second purchase under a different key, this yields two purchases.

**NOT established (do not claim either way):**
- Production behaviour. The sample merchants are in-memory and keyed only by the idempotency key; they have no per-basket guard. Real
  platforms (Magento, Shopware, custom builds) may guard a basket, may hold keys differently, and may or may not honour the header.
  The engine's behaviour is platform-independent; whether it causes a double charge on a given real platform is unverified.
- How often a charge-then-timeout happens on a real client. Not measured.
- A permanently stuck `IN_FLIGHT` row (a crash or a cancelled handler). Not reproduced; the handler continued after a client disconnect.
- The `RETRY_PAYMENT` / `OFFER_ALTERNATE_PAYMENT` path through `execute_case` (Kettle). Not reproduced.

## What the code does today (confirmed by reading; the pay-route behaviour above was also reproduced)

**1. Transport layer.** `adapters/framework/http.py` `PlatformClient.request` retries on timeout / transport error / 429 / 5xx, sending
the same `Idempotency-Key` header, and after the last attempt raises the final `CommerceError` with code `TIMEOUT`, `UPSTREAM_ERROR`
or `RATE_LIMITED`. A `TIMEOUT` or 5xx does not tell the caller whether the platform processed the request.

**2. Both pay routes treat every `CommerceError` as "nothing was attempted".** `engine/api/chat.py` (`/pay`) and `engine/api/shop.py`
(checkout) catch `CommerceError` from `adapter.checkout_with_card(...)` and call `db.idempotency.release_payment(...)`, whose docstring
says "Give the basket back after a charge that never happened". `release_payment` deletes the `paid_*` ledger row if it is `IN_FLIGHT`.
The chat route then tells the shopper "Nothing has been charged - try again in a moment." That is true for `CART_INVALID`; it is not
true for `TIMEOUT` / `UPSTREAM_ERROR` (reproduced).

**3. The platform key is per card, the ledger key is per cart.** The platform key is `pay-{cart_id}-{card_last4}`. After
`release_payment` deletes the cart-level row: the same card again is protected only if the platform honours the key (both demo
platforms do); a *different* card is a different platform key, and the cart-level ledger row that would have refused it is gone. That is
the double-charge shape `db/idempotency.py` was written to prevent (see its "A basket is paid for once" section).

**4. Money actions routed through `execute_case`.** `_run` catches `CommerceError` and returns `Executed(succeeded=False,
error_code=str(exc.code), final_state=FAILED)` (`engine/execution/service.py`). `execute_case` then calls
`db.idempotency.complete(key, succeeded=False, ...)`, so a timed-out `RETRY_PAYMENT` / `OFFER_ALTERNATE_PAYMENT` / `SPLIT_PAYMENT` ends
`DONE`, `succeeded=False`. A rerun returns "Already done: ..." and never retries, which is conservative - but the ledger now says
*failed*, not *unknown*. `complete()` does not store the error code, so the attempt row cannot distinguish a decline from a timeout.
(Not reproduced.)

**5. Process death.** Between `begin_payment` and `payment_settled` / `release_payment` there is no `finally`. A non-`CommerceError`
exception or a crash leaves the `paid_*` row `IN_FLIGHT`. `begin_payment` then answers "going through now" for that basket
indefinitely (no stale takeover). `purge_old()` would delete it after 7 days but is never scheduled. (Not reproduced.)

**6. Related, not payment-ledger:** `execute_case` has no case/approval state guard, and `ISSUE_REFUND` (approvable) has no `_dispatch`
case, so an approved refund ends `UNSUPPORTED` ("no execution path yet").

## Ledger facts that constrain any fix

There is no cart -> order or idempotency-key -> order lookup in `StandardCommerceInterface` (only `get_order(order_id)` and `get_cart`),
so an `IN_FLIGHT` or ambiguous row cannot be reconciled from the platform side today. No existing ledger function is a safe transition
out of an unknown-outcome row: `release_payment` assumes nothing was attempted, `payment_settled` needs platform-confirmed order data,
`complete` records a definite result.

## Proposed fix (NOT implemented; needs its own spec and `invariant-guard` review)

1. Add an explicit `UNKNOWN_OUTCOME` ledger state. Release the cart-level row ONLY on errors that prove nothing happened (`CART_INVALID`,
   a validation 4xx before processing, a clean decline). On `TIMEOUT`, `UPSTREAM_ERROR` (5xx after retries) and unexpected exceptions,
   keep the row as `UNKNOWN_OUTCOME`.
2. `begin_payment` refuses new attempts on an `UNKNOWN_OUTCOME` basket and tells the shopper "we couldn't confirm your payment, please
   don't pay again, we're checking" instead of "Nothing has been charged".
3. Reconcile with evidence, never a guess: add an optional platform lookup (order by idempotency key or by cart); if the platform cannot
   answer, hold the basket and surface an Operations incident for a human to resolve. Only a confirmed order moves the row to
   DONE/succeeded; only a confirmed absence makes the basket buyable again.
4. Wrap the pay routes in `try/finally` so a cancelled or crashing handler never leaves an unexplained `IN_FLIGHT` row; schedule
   `purge_old` / an `IN_FLIGHT` staleness check.
5. `execute_case` money actions: store the error code so a timeout is `UNKNOWN`, not `failed`.

It touches `engine/db/idempotency.py`, `engine/api/chat.py`, `engine/api/shop.py`, `engine/execution/service.py` and the adapter
interface (the lookup). No new action type. `chat.py` is shared with Shopper V2 work, so coordinate before changing it.

## Regression tests required with the fix (they must FAIL on today's code)

Reuse the reproduction harness as an HTTP-level test plus unit tests on the ledger:
- T1 timeout then a different card: the platform must hold exactly ONE purchase; the second tap is refused with the "could not confirm"
  message; the ledger row is `UNKNOWN_OUTCOME`.
- T2 5xx on the REST route: same assertions; the response carries no platform detail.
- T3 same card after a timeout: the second tap settles with the ORIGINAL order id (true today; keep as a guard).
- T4 a definitive failure (`CART_INVALID`, a decline): the row is released and the basket stays buyable (recovery must not regress).
- T5 an abandoned request: no row is left `IN_FLIGHT` after the handler ends.
- T6 reconciliation: a confirmed order moves `UNKNOWN_OUTCOME` to DONE; a confirmed absence makes the basket buyable.

## Release status

Real-money checkout must not ship, and this must not be described as resolved, until the fix is implemented AND T1-T6 pass AND a
platform that actually charges has been checked for the same sequence. Operations relevance: stuck `IN_FLIGHT` attempts are detected
and surfaced by the Operations slice as evidence-only incidents that cannot be resolved by Operations; this investigation owns the
ledger side.
