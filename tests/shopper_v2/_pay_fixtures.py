"""Fixtures for the payment-safety tests (not a test itself: the runner only collects test_*.py).

`FakePlatform` behaves like the platforms the engine talks to, for payments: it really records a purchase when it "charges",
replays the original purchase for a repeated idempotency key, and can be scripted to fail in the ways that matter -
a timeout or 5xx AFTER it charged (the ambiguous case), a clean refusal, a decline, an unexpected exception, a hang.
Every call is recorded so a test can prove exactly how many times the platform was asked and how many purchases exist
for one basket. It imports nothing from `engine` (the importing test sets DATABASE_URL first).
"""
from __future__ import annotations

import asyncio
import itertools
from decimal import Decimal

from shared.models import (
    CheckoutLookup,
    CheckoutResult,
    CommerceError,
    ErrorCode,
    Money,
    Order,
    OrderStatus,
    PaymentRecoveryMethod,
    PaymentRecoveryResult,
)
from shared.models.commerce import PaymentStatus
from shared.models.errors import mark_write_ambiguous

CART = "BSK-T1"


def money(amount=500, currency="INR"):
    return Money(amount=Decimal(str(amount)), currency=currency)


def make_order(ref, *, paid=True, amount=500):
    return Order(
        order_id=ref,
        status=OrderStatus.PAID if paid else OrderStatus.AWAITING_PAYMENT,
        payment_status=PaymentStatus.CAPTURED if paid else PaymentStatus.DECLINED,
        lines=[], grand_total=money(amount), amount_paid=money(amount if paid else 0), currency="INR",
    )


class FakePlatform:
    """A platform adapter for payments. `script` is consumed one entry per checkout call (default: charge and succeed).

    Entries: "ok", "decline", "charge_then_timeout", "charge_then_5xx", ("error", ErrorCode), ("ambiguous_then_error", ErrorCode),
    "unexpected", "hang", "slow_ok".
    """

    honours_idempotency_keys = True

    def __init__(self, script=None, *, honours=True):
        self.script = list(script or [])
        self.honours_idempotency_keys = honours
        self.purchases: list[dict] = []
        self.checkout_calls: list[str] = []
        self.recover_calls: list[str] = []
        self._seq = itertools.count(1)
        self.recover_script: list = []

    # ---- helpers the tests read
    def purchases_for(self, cart_id=CART, *, paid_only=True):
        return [p for p in self.purchases if p["cart"] == cart_id and (p["paid"] or not paid_only)]

    def _charge(self, cart_id, key, *, paid):
        if self.honours_idempotency_keys:
            for p in self.purchases:
                if p["key"] == key:
                    return p
        p = {"ref": f"ORD{next(self._seq):04d}", "cart": cart_id, "key": key, "paid": paid}
        self.purchases.append(p)
        return p

    def _result(self, p):
        order = make_order(p["ref"], paid=p["paid"])
        return CheckoutResult(succeeded=p["paid"], order=order, payment_status=order.payment_status)

    async def checkout_with_card(self, cart_id, *, card_last4, idempotency_key):
        self.checkout_calls.append(idempotency_key)
        step = self.script.pop(0) if self.script else "ok"
        kind, arg = (step if isinstance(step, tuple) else (step, None))
        if kind == "ok":
            return self._result(self._charge(cart_id, idempotency_key, paid=True))
        if kind == "slow_ok":
            await asyncio.sleep(0.4)
            return self._result(self._charge(cart_id, idempotency_key, paid=True))
        if kind == "decline":
            return self._result(self._charge(cart_id, idempotency_key, paid=False))
        if kind == "charge_then_timeout":
            self._charge(cart_id, idempotency_key, paid=True)
            mark_write_ambiguous()
            raise CommerceError(ErrorCode.TIMEOUT, "timeout calling /purchase", platform_detail="RAW-PLATFORM-SECRET-TEXT")
        if kind == "charge_then_5xx":
            self._charge(cart_id, idempotency_key, paid=True)
            mark_write_ambiguous()
            raise CommerceError(ErrorCode.UPSTREAM_ERROR, "platform returned 503", platform_detail="RAW-PLATFORM-SECRET-TEXT")
        if kind == "error":
            raise CommerceError(arg, "platform said no", platform_detail="RAW-PLATFORM-SECRET-TEXT")
        if kind == "ambiguous_then_error":
            mark_write_ambiguous()  # an earlier attempt of this same call timed out; the final error looks conclusive
            raise CommerceError(arg, "basket closed", platform_detail="RAW-PLATFORM-SECRET-TEXT")
        if kind == "unexpected":
            raise RuntimeError("boom: not a CommerceError")
        if kind == "hang":
            await asyncio.sleep(3600)
        raise AssertionError(f"unknown script step {step!r}")

    async def recover_payment(self, order_id, *, method: PaymentRecoveryMethod, idempotency_key, amount=None):
        self.recover_calls.append(idempotency_key)
        step = self.recover_script.pop(0) if self.recover_script else "ok"
        if step == "timeout":
            mark_write_ambiguous()
            raise CommerceError(ErrorCode.TIMEOUT, "timeout", platform_detail="RAW-PLATFORM-SECRET-TEXT")
        if step == "decline":
            return PaymentRecoveryResult(succeeded=False, method_used=method, reason="declined again")
        order = make_order(order_id)
        return PaymentRecoveryResult(succeeded=True, method_used=method, order=order, amount_recovered=money(), reason="ok")


class FakePlatformWithLookup(FakePlatform):
    """Adds `lookup_checkout`. `lookup_mode`: "truth" (answers from its own purchases), "partial", "fails"."""

    def __init__(self, script=None, *, honours=True, lookup_mode="truth"):
        super().__init__(script, honours=honours)
        self.lookup_mode = lookup_mode
        self.lookup_calls = 0

    async def lookup_checkout(self, cart_id, idempotency_keys):
        self.lookup_calls += 1
        if self.lookup_mode == "fails":
            raise CommerceError(ErrorCode.TIMEOUT, "lookup timeout")
        if self.lookup_mode == "partial":
            return CheckoutLookup(status="UNKNOWN")
        rows = [p for p in self.purchases if p["cart"] == cart_id]
        orders = [make_order(p["ref"], paid=p["paid"]) for p in rows]
        return CheckoutLookup(status="FOUND" if orders else "ABSENT", orders=orders)


def says_unknown(text):
    low = (text or "").lower()
    return "couldn't confirm" in low and "don't pay again" in low


def falsely_reassures(text):
    return "nothing has been charged" in (text or "").lower()
