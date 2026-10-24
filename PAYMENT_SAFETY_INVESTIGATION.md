# Payment safety: ambiguous payment outcomes

Status: **FIX IMPLEMENTED AND VERIFIED LOCALLY (model-free, isolated sample merchants). NOT production-ready: the release gate below is still open.**
Opened 2026-09-30 from a code read; reproduced 2026-10-23 and again on 2026-10-24 before the fix; fixed and re-run on 2026-10-24.
Spec: `D:\CV3\feature_specs\payment_unknown_outcome_SPEC.md` (v1, v2 and v2.1 after two independent `invariant-guard` reviews).
Evidence and harness (outside the repo): `D:\CV3\payment_repro\` - `FINDINGS.md`, `payrepro.py` (original), `payrepro_fix.py` (post-fix copy with
two added scenarios), `charge_proxy.py`, `engine_wrapper.py`, and the run directories `runs/pay-20261024-104807` (before) and `runs/pay-20261024-114035` (after, final code).

## The question

When a money-touching call ends in a timeout or 5xx, the platform may or may not have acted. Did the engine record that as a definite
"nothing happened", and could the same basket then be charged twice?

**Before the fix: yes (reproduced twice on the local sample merchant).** The pay routes called `release_payment` on EVERY `CommerceError`
and told the shopper "Nothing has been charged", deleting the only per-basket guard; the platform key is per card, so a different card was
a new key and bought the same basket again. Any exception that was not a `CommerceError`, a cancelled handler or a crash left the ledger
row `IN_FLIGHT` forever.

## What was implemented

One shared code path, `engine/execution/payment_guard.py`, used by `/api/chat/pay`, the REST checkout and the payment-recovery money path:

- **`UNKNOWN_OUTCOME` ledger state** (`engine/db/idempotency.py`, same `execution_attempts` row, JSON in `result`, no migration). Every transition
  is a compare-and-set bound to a per-attempt claim token. A timeout, a 5xx, any unexpected exception, a cancelled handler, a failed ledger write
  after the platform answered, a call that outlives the hard deadline (`PAYMENT_CALL_DEADLINE_SECONDS`, default 60) or a stale `IN_FLIGHT` row
  (older than twice the deadline, or found at engine startup) parks the basket as `UNKNOWN_OUTCOME`, shielded from cancellation. It is never released.
- **Release only on proof.** The row is released only for `CART_INVALID`, `CART_NOT_FOUND`, `VALIDATION_ERROR`, `CAPABILITY_UNSUPPORTED`, `AUTH_FAILED`, and only when no
  earlier attempt inside the same call was ambiguous (`shared.models.errors.WriteAmbiguity`, set by `adapters/framework/http.py` on any timeout, transport error,
  retryable status or unreadable body). `RATE_LIMITED`, stock errors and everything else are held, not released.
- **Refusals.** While a basket is `UNKNOWN_OUTCOME` or `IN_FLIGHT`, a different card, a concurrent tap, the other route (chat vs REST) and a payment recovery are
  refused without a platform call, and cart changes are refused (`CART_PAYMENT_PENDING`). The shopper is told, in fixed engine wording, that the outcome could not
  be confirmed and not to pay again; "Nothing has been charged" is said only for a proven non-event. No platform text reaches a shopper or the ledger.
- **Safe same-key retry.** The SAME card is retried (same platform key, so the platform replays the original purchase) only when the platform declares
  `honours_idempotency_keys` AND the settle window (`PAYMENT_SETTLE_WINDOW_SECONDS`, default 120 s, measured from when the row became unknown) has passed.
  A retry that ends in any error returns the basket to `UNKNOWN_OUTCOME`; it is never released.
- **Evidence-based reconciliation** (`engine/execution/payment_reconcile.py`). An optional adapter lookup (`SupportsCheckoutLookup.lookup_checkout`; Northfield implements it,
  Kettle has none) is asked on a blocked tap, by the periodic sweep and by an operator. A PAID order makes the basket paid with the platform's order id (ownership is
  recorded for the cart's owner only; sales lines are written). A COMPLETE answer showing no order, or only unpaid ones, after the settle window makes the basket
  buyable again. A failed, partial, early or unavailable lookup keeps it blocked and flags it for a person.
- **Operator surface** (operator key only): `GET /api/ops/payments/unresolved`, `POST /api/ops/payments/{connection}/{cart}/reconcile`, `POST .../resolve`
  (`paid` or `not_charged`). The platform's evidence outranks the operator: `not_charged` is refused while the platform shows a paid order, `paid` is refused when
  the platform answers completely and shows none; with no lookup the call stands and is recorded `operator-asserted`. Resolution is compare-and-set protected and audited.
- Also: both sample merchant stubs now serialise their scan-then-insert (the claim "same key twice is one purchase" holds under concurrent requests); the sample
  merchant's purchase list can be filtered by basket; `forget_payment` no longer deletes an unresolved row; `purge_old` skips them.

## Reproduction before and after (isolated HTTP harness, same scenarios)

Isolation was verified before the first purchase request and after each run: disposable byte copy of `cv3_v2verify.db` bound by a fail-closed wrapper, local Northfield
only behind a loopback fault-injecting proxy that forwards the purchase and then withholds the response or returns 503, Kettle unreachable, fake cards, provider keys,
SMTP and proxies blank, continuous egress monitor with no non-loopback peer, protected databases identical by hash, size and mtime, all processes stopped, ports free.

| Scenario | Before: platform purchases for ONE basket | After: platform purchases | What the shopper is told after the fix |
|---|---|---|---|
| S1 timeout, then the same card at once | 1 (but "Nothing has been charged") | 1 | first tap: could not confirm / hold; next tap: reconciled, "already paid ... order ORD00001" |
| S2 timeout, then a different card | **2** | **1** | the second card is never sent; reconciled to "already paid" |
| S3 REST 5xx, then a different card | **2** | **1** | first tap `409 PAYMENT_OUTCOME_UNKNOWN`; the second card is never sent |
| S4 client abandons, then a different card | **2** | **1** | the handler parks the basket as unknown; the second card is never sent |
| S5 same card after the settle window (new) | n/a | 1 | settles to the original order |
| S6 chat timeout, then REST with another card (new) | n/a | 1 | refused; reconciled to the original order |

On the sample merchant a reconciliation finds the paid order at once, so these runs end "already paid". The branch where the lookup cannot answer (Kettle, a failing
lookup, a platform that must be waited out) is covered by the model-free tests below, not by this harness.

## Tests (all model-free; fail on the old code, mutation-checked)

`tests/shopper_v2/test_payment_unknown_outcome.py` (42 checks: timeout/different card, 5xx, ambiguity, definitive release, decline, unexpected exception, deadline,
client disconnect, ledger-write failure, concurrency, same-key retry gating, crashed and stale `IN_FLIGHT`, ledger guards, cart locks), `test_payment_reconcile.py` (32:
reconciliation, absence rules, throttle, duplicates, ownership, operator resolution, recovery claim, sweep), `test_payment_tenant_isolation.py` (11),
`test_write_ambiguity_signal.py` (12, real HTTP client over a mock transport, survives `wait_for`/tasks), `test_payment_platform_adapters.py` (8, the two real sample
platforms: Northfield lookup, Kettle none, same key twice at once is one purchase). Deliberately breaking the guard (release on every error; ignore the ambiguity signal;
ignore `UNKNOWN_OUTCOME` in `begin_payment`) makes the suite fail. The existing `test_phase5_checkout.py` and `test_phase5_payment_recovery.py` still pass.

## NOT established - do not claim

- Production behaviour. The sample merchants are in-memory and keyed only by the idempotency key. Whether a real platform honours the key, can answer a lookup, or
  guards a basket is unverified; `honours_idempotency_keys` is a claim about the two demo stubs only. Kettle has no lookup, so its unknown outcomes end with the same-key
  retry or a person.
- That the absence verdict is safe on a platform that can hold a request longer than the settle window; "confirmed not charged" is only as good as that bound.
- How often a charge-then-timeout happens on a real client.
- Anything involving a real payment provider: none was contacted.

## Remaining limitations

- The storefront still shows the card buttons for a basket that is on hold (every tap is refused server-side). No storefront change was made.
- Nothing notifies the merchant when a basket becomes unknown; operators see `GET /api/ops/payments/unresolved`, and the sweep runs every 60 s inside the engine process.
- A reconciled order sends no confirmation mail and records no purchase event (no shopper address or session is in scope).
- A cart id the platform recycles after a restart keeps a stale unresolved row (Northfield clears it by reconciliation; Kettle needs an operator).
- A timed-out recovery is held on the basket, but a later retry of the same recovery case still returns the stored "already done" result (conservative).
- Unresolved payments are not in `ops_stats`; the operator list carries the count.

## Release status

**Real-money checkout must not ship on this evidence alone.** The gate stays open until a platform that actually charges has been checked for these sequences (timeout and
5xx after a charge, abandoned request, a different card, a same-key retry, a lookup), the real platforms' key and lookup semantics are confirmed, the storefront and operations
handling of a held basket is decided, and the owner signs off.
