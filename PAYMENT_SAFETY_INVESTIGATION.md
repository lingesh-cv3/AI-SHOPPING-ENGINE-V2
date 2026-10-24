# Payment safety: ambiguous payment outcomes

Status: **FIX IMPLEMENTED AND VERIFIED LOCALLY (model-free backend, isolated sample merchants, and browser tests on an isolated stack); a second hardening pass (storefront hold, merchant/operator alerts, recycled cart ids) is implemented and verified the same way. NOT production-ready: the release gate below is still open.**
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

## Second hardening pass (storefront hold, alerts, recycled cart ids)

Backend verified model-free (64 new checks, 169 payment checks in all); storefront verified in a browser on an isolated stack (see "Browser status").

- **Storefront.** The cart read now carries `payment_state` (`paid` / `in_flight` / `unknown` / null) from the engine's ledger, and the V2 checkout offer refuses a basket that is held, in flight or
  paid. `storefront/src/paymentHold.ts` keeps one shared per-merchant-and-basket hold state read by the cart panel and the assistant's payment picker: payment is disabled with a visible reason
  ("Payment on hold", "Payment in progress", "Checking payment status", "could not check"), a stale click handler cannot pay a held basket, and a "Check again" control re-reads the server (a held basket
  also re-reads every 15 s). It fails CLOSED: a payment request that dies without a clean answer, a 5xx or an unreadable cart disables payment until the server's own state has been read; an unrecognised
  `payment_state` value is held, not free; and a cart object the page was already holding can only RAISE a hold, never lift one - only a fresh read can. A clean decline does not hold the basket.
  The server-side guards stay authoritative whatever the page does.
- **Alerts.** One merchant task (`PAYMENT_UNRESOLVED`, shown in Merchant Tasks) per unknown-outcome EPISODE (a random id fixed when the basket first becomes unknown, kept across same-key retries), with an
  audit reference (a hash of connection and basket), the basket id, the reason code and the start time - no card digits, platform keys, amounts, order or shopper identity, or platform text. Deduplicated
  twice (the ledger records the episode it alerted for; the task's unique key refuses a second row). A failed alert is recorded with a try count and retried by the 60 s sweep (capped), never raised to a
  shopper's tap. It stays open while the basket is unknown or a same-key retry is in flight, closes itself (system) when the ledger resolves, is reopened if closed by the system too early, and a merchant
  cannot "resolve" it (it would only suggest the basket is settled). Operators get `unresolved_payments` and the oldest held minutes in `ops_stats`, shown on the Operations overview.
- **Recycled cart ids.** `create_cart` no longer calls "the platform says this id is new" evidence. If the minted id has an unresolved ledger row it reconciles first; if that cannot release the row (inside
  the settle window, no lookup, in flight) the id is not used and another is minted (up to 5, then a retryable 503); an id the platform proves it has a PAID order for is settled and skipped. The unresolved
  row is never discarded without evidence; a settled declined row on a recycled id is forgotten as before.
- **The Playwright server process** found running during this work (`playwright test-server`, started 09:48 by a VS Code utility process) belongs to the user's VS Code Playwright extension, not to this
  project's test runs (those use `playwright test`); it was left alone.

Defects the new tests caught during development: the held state was being re-derived from a possibly-stale cart object (a hold could be silently lifted), and an unrecognised `payment_state` was treated as free
(fail-open) - both fixed; an episode id derived from a one-second timestamp collapsed two separate episodes into one alert - replaced with a random id; and the independent review found a same-key retry in flight
let the sweep close a still-unresolved basket's alert - fixed and covered.

**Browser status (verified on the final frontend code, isolated stack, model-free).** `storefront/tests/payment-hold.spec.ts` passes 10/10 (held basket, server-only release, 409 unknown, a request that dies, a 5xx with an unreadable cart, an unrecognised state, a clean decline, the assistant picker held and failing closed, the operations figure). The existing browser suites pass alongside it (operations overview, cart restore, tenant isolation, platform-down; the assistant widget, closeout and proposals; guided missions, discovery routing, conversational flow, relevance; and the real signup-and-pay leg of the checkout spec through the new guard: 60 tests in all). Not run, and why: the two tests that need a live model ("a tapped product card...", "a real chat question about the cart...") - this stack has no provider by design; the two platform-down tests that stop services (they skip themselves here); checkout Leg B, whose helper script looks the account up in the default database rather than the isolated one (it found nothing and changed nothing - the protected databases were unchanged); and checkout "Issue #5", which asserts a decline panel the app no longer shows - it fails identically on the unmodified frontend, so it is a stale assertion and not caused by this work. `tsc`, the build and lint (the unchanged 6-error baseline) pass.

## NOT established - do not claim

- Production behaviour. The sample merchants are in-memory and keyed only by the idempotency key. Whether a real platform honours the key, can answer a lookup, or
  guards a basket is unverified; `honours_idempotency_keys` is a claim about the two demo stubs only. Kettle has no lookup, so its unknown outcomes end with the same-key
  retry or a person.
- That the absence verdict is safe on a platform that can hold a request longer than the settle window; "confirmed not charged" is only as good as that bound.
- How often a charge-then-timeout happens on a real client.
- Anything involving a real payment provider: none was contacted.

## Remaining limitations

- A same-key retry in flight is not shown to the shopper as a separate state (they see "payment in progress" or the hold, from the server's state).
- A held basket's alert reaches the merchant within one sweep (60 s); the operator figure and list are immediate. Nothing else notifies anybody (no email, no push).
- The sweep reads unresolved baskets in pages (bounded per sweep); a very large backlog is alerted over several sweeps, and is always counted in `ops_stats`.
- A reconciled order sends no confirmation mail and records no purchase event (no shopper address or session is in scope).
- A timed-out recovery is held on the basket, but a later retry of the same recovery case still returns the stored "already done" result (conservative).
- A custom adapter that does not use the shared HTTP client never sets the ambiguity signal, so its final error code would be trusted as definitive; only the two demo adapters exist today and both use it.
- Operator `resolve` records the operator key's id; there is no per-person operator identity beyond the key.
- Everything in "NOT established" above still stands: no real platform or provider was tested.

## Release status

**Real-money checkout must not ship on this evidence alone.** The gate stays open until a platform that actually charges has been checked for these sequences (timeout and
5xx after a charge, abandoned request, a different card, a same-key retry, a lookup), the real platforms' key and lookup semantics are confirmed, the storefront and operations
handling of a held basket is decided, and the owner signs off.
