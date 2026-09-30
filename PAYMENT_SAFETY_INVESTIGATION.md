# Payment-safety investigation: ambiguous outcomes recorded as definite

Status: **OPEN. Code-read only - not reproduced.** Opened 2026-09-30 while auditing
Operations. Nothing in the ledger, execution, pay routes or adapters has been
changed. This is deliberately separate from the Operations work and from Shopper
V2; it needs its own spec, a `bug-reproducer` run, and `invariant-guard` review
(invariants 2, 3 and 7) before any change is made.

## The question

When a money-touching call ends in a timeout or transport/5xx failure, the
platform may or may not have acted. Does the engine ever record that as a
definite "nothing happened", and can that let the same basket be charged twice?

## What the code does today (read, not run)

**1. Transport layer.** `adapters/framework/http.py` `PlatformClient.request`
retries on timeout / transport error / 429 / 5xx, sending the same
`Idempotency-Key` header, and after the last attempt raises the final
`CommerceError` with code `TIMEOUT`, `UPSTREAM_ERROR` or `RATE_LIMITED`. A `TIMEOUT`
or 5xx does not tell the caller whether the platform processed the request.

**2. Both pay routes treat every `CommerceError` as "nothing was attempted".**
`engine/api/chat.py:3160-3166` and `engine/api/shop.py:514-517` catch
`CommerceError` from `adapter.checkout_with_card(...)` and call
`db.idempotency.release_payment(...)`, whose docstring says "Give the basket back
after a charge that never happened". `release_payment` deletes the `paid_*` ledger
row if it is `IN_FLIGHT`. The chat route then tells the shopper "Nothing has been
charged - try again in a moment." That is true for `CART_INVALID`; it is not
established for `TIMEOUT` / `UPSTREAM_ERROR`.

**3. The platform key is per card, the ledger key is per cart.** The platform key
is `pay-{cart_id}-{card_last4}`. After `release_payment` deletes the cart-level row:
same card again is protected only if the platform honours the key (both demo
platforms do: `sample_merchant/store.py:337`, `sample_merchant_two/store.py:259-262`);
a *different* card is a different platform key, and the cart-level ledger row that
would have refused it is gone. That is the double-charge shape `db/idempotency.py`
was written to prevent (see its "A basket is paid for once" section).

**4. Money actions routed through `execute_case`.** `_run` catches `CommerceError`
and returns `Executed(succeeded=False, error_code=str(exc.code), final_state=FAILED)`
(`engine/execution/service.py:462-470`). `execute_case` then calls
`db.idempotency.complete(key, succeeded=False, ...)` (`:328-334`), so a timed-out
`RETRY_PAYMENT` / `OFFER_ALTERNATE_PAYMENT` / `SPLIT_PAYMENT` ends `DONE`,
`succeeded=False`. A rerun returns "Already done: ..." and never retries, which is
conservative - but the ledger now says *failed*, not *unknown*. `complete()` does
not store the error code, so the attempt row cannot distinguish a decline from a
timeout; only free text in `summary` does.

**5. Process death.** Between `begin_payment` and `payment_settled` /
`release_payment` there is no `finally`. A non-`CommerceError` exception, a client
disconnect that cancels the handler, or a crash leaves the `paid_*` row
`IN_FLIGHT`. `begin_payment` then answers "going through now" for that basket
indefinitely (no stale takeover). `purge_old()` would delete it after 7 days but is
never scheduled.

**6. Related, not payment-ledger:** `execute_case` has no case/approval state
guard, and `ISSUE_REFUND` (approvable) has no `_dispatch` case, so an approved
refund ends `UNSUPPORTED` ("no execution path yet").

## What is NOT known

- That a timeout on either demo platform actually produces a charge-then-timeout.
  Not reproduced. The merchant simulators may never do it.
- What real client platforms (Magento, Shopware, custom) do on a timed-out
  checkout, or whether they honour the idempotency header.
- Whether any production path reaches (2) with `TIMEOUT` / `UPSTREAM_ERROR` often
  enough to matter.

## Ledger facts that constrain any fix

There is no cart -> order or idempotency-key -> order lookup in
`StandardCommerceInterface` (only `get_order(order_id)` and `get_cart`), so an
`IN_FLIGHT` or ambiguous row cannot be reconciled from the platform side today.
No existing ledger function is a safe transition out of an unknown-outcome row:
`release_payment` assumes nothing was attempted, `payment_settled` needs
platform-confirmed order data, `complete` records a definite result.

## Next steps (none taken)

1. `bug-reproducer`: fault-inject the Kettle/Northfield simulator (charge, then
   delay past the client timeout / return 5xx) and record the exact sequence for
   (a) same card retry and (b) different card.
2. Decide whether the outcome set needs an explicit UNKNOWN state, and what may
   move a row out of it (evidence from `get_order`, never a guess).
3. Only then a spec + `invariant-guard`; any fix touches `db/idempotency.py`,
   `engine/api/chat.py` and `engine/api/shop.py` - `chat.py` currently carries
   uncommitted Shopper V2 changes, so coordinate that first.

Operations relevance: stuck `IN_FLIGHT` attempts are detected and surfaced by the
Operations slice as evidence-only incidents that cannot be resolved by Operations.
This investigation owns the ledger side.
