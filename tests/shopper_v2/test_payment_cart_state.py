"""The cart a shopper reads carries the basket's payment state; recycled cart ids never inherit or discard an unresolved payment (model-free).

`payment_state` is what the storefront uses to disable payment on a held basket (and to learn a stale page is stale). And when a
platform reissues a cart id whose payment ledger row is still unresolved from the id's earlier life, `create_cart` does not
discard that row on the strength of "the platform says this id is new": it reconciles first, and when it cannot release the row
it does not use that id - another is minted.

    python tests/shopper_v2/test_payment_cart_state.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_cartstate.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_cartstate.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _pay_fixtures import FakePlatform, FakePlatformWithLookup, money  # noqa: E402
from shared.models import Cart  # noqa: E402

failures = 0
CONN = "conn_t"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class Who:
    """The shopper's identity as the route sees it: one browser key."""

    async def keys_for(self, connection_id):
        return ["browser-key"]


def cart(cart_id):
    return Cart(cart_id=cart_id, subtotal=money(0), currency="INR")


class MintMixin:
    def _init_mint(self, ids):
        self.ids = list(ids)
        self.minted = []

    async def create_cart(self):
        cid = self.ids.pop(0)
        self.minted.append(cid)
        return cart(cid)


class MintWithLookup(MintMixin, FakePlatformWithLookup):
    def __init__(self, ids, **kw):
        FakePlatformWithLookup.__init__(self, **kw)
        self._init_mint(ids)


class MintNoLookup(MintMixin, FakePlatform):
    def __init__(self, ids, **kw):
        FakePlatform.__init__(self, **kw)
        self._init_mint(ids)


async def row_of(cart_id):
    from engine.db import idempotency as ledger

    return await ledger.payment_row(CONN, cart_id)


async def pay(platform, cart_id, card="1111"):
    from engine.execution import payment_guard

    return await payment_guard.pay_basket(platform, CONN, cart_id, card)


async def age(cart_id, seconds=7200):
    from datetime import UTC, datetime, timedelta

    from engine.db import idempotency as ledger
    from engine.db.models import ExecutionAttempt
    from engine.db.session import session_scope

    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, ledger.cart_payment_key(CONN, cart_id))
        res = dict(row.result or {})
        res["unknown_at"] = res["first_unknown_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
        res.pop("recon", None)
        row.result = res


async def make_unknown(cart_id):
    p = FakePlatform(["charge_then_timeout"])
    out = await pay(p, cart_id)
    assert out.kind == "unknown", out.kind


async def mint(platform):
    from engine.api import shop
    from fastapi import HTTPException

    shop._adapter = lambda connection_id: platform
    try:
        return await shop.create_cart(CONN, Who(), None)
    except HTTPException as exc:
        return exc


async def checks():
    from engine import db
    from engine.api import shop
    from engine.db import idempotency as ledger

    os.environ["PAYMENT_SETTLE_WINDOW_SECONDS"] = "3600"

    print("1. The cart view carries the basket's payment state")
    states = {}
    for cid in ("V-FREE", "V-FLIGHT", "V-UNKNOWN", "V-PAID", "V-DECLINED"):
        states[cid] = None
    await ledger.begin_payment(CONN, "V-FLIGHT", attempt_key="pay-V-FLIGHT-1111")
    await make_unknown("V-UNKNOWN")
    await pay(FakePlatform(["ok"]), "V-PAID")
    await pay(FakePlatform(["decline"]), "V-DECLINED", "0002")
    got = {cid: (await shop._cart_view(CONN, cart(cid)))["payment_state"] for cid in states}
    check("none -> null, in flight, unknown, paid, and a declined basket is free again (null)",
          got == {"V-FREE": None, "V-FLIGHT": "in_flight", "V-UNKNOWN": "unknown", "V-PAID": "paid", "V-DECLINED": None}, got)
    view = await shop._cart_view(CONN, cart("V-UNKNOWN"))
    check("the normal cart fields are still there", view["cart_id"] == "V-UNKNOWN" and "lines" in view and "grand_total" in view, sorted(view))
    other = await shop._cart_view("conn_other", cart("V-UNKNOWN"))
    check("the same cart id under another merchant is free (the ledger is per merchant)", other["payment_state"] is None, other["payment_state"])

    print("\n2. A recycled id with an unresolved payment is never discarded without evidence")
    await make_unknown("R-1")
    m = MintWithLookup(["R-1", "R-2"])
    r = await mint(m)
    check("inside the settle window the platform's 'fresh id' is not enough: R-1 is skipped and R-2 is used", r["cart_id"] == "R-2" and m.minted == ["R-1", "R-2"], (r, m.minted))
    check("R-1's unresolved row is untouched", (await row_of("R-1"))["state"] == "UNKNOWN_OUTCOME")
    check("only the id the shopper actually got is theirs", await db.owners.owner_of(CONN, db.owners.CART, "R-2") == "browser-key" and await db.owners.owner_of(CONN, db.owners.CART, "R-1") is None)

    await make_unknown("R-B1")
    await age("R-B1")
    m = MintWithLookup(["R-B1"])
    r = await mint(m)
    check("after the settle window the platform shows nothing for it: reconciliation clears the row and the id is used", r["cart_id"] == "R-B1" and m.minted == ["R-B1"], (r, m.minted))
    check("and nothing of the old payment is left to block the new basket", await row_of("R-B1") is None)

    await make_unknown("R-C1")
    m = MintWithLookup(["R-C1", "R-C2"])
    m.purchases.append({"ref": "ORD-REAL", "cart": "R-C1", "key": "pay-R-C1-1111", "paid": True})
    r = await mint(m)
    row = await row_of("R-C1")
    check("an id the platform proves it HAS a paid order for is never 'fresh': settled as paid, skipped", r["cart_id"] == "R-C2" and row["state"] == "DONE" and row["succeeded"] is True and row["order_id"] == "ORD-REAL", (r, row))

    await make_unknown("R-D1")
    m = MintNoLookup(["R-D1", "R-D2"])
    r = await mint(m)
    check("a platform that cannot look orders up: the id is skipped and the row stays unresolved", r["cart_id"] == "R-D2" and (await row_of("R-D1"))["state"] == "UNKNOWN_OUTCOME", (r, await row_of("R-D1")))

    await ledger.begin_payment(CONN, "R-E1", attempt_key="pay-R-E1-1111")
    m = MintWithLookup(["R-E1", "R-E2"])
    r = await mint(m)
    check("an id whose payment is in flight is skipped too", r["cart_id"] == "R-E2" and (await row_of("R-E1"))["state"] == "IN_FLIGHT", (r, await row_of("R-E1")))

    print("\n3. Every id colliding degrades honestly")
    ids = [f"R-F{i}" for i in range(1, 6)]
    for cid in ids:
        await make_unknown(cid)
    m = MintWithLookup(ids)
    r = await mint(m)
    check("five collisions in a row: a retryable 503, no basket handed out", hasattr(r, "status_code") and r.status_code == 503 and r.detail["code"] == "CART_UNAVAILABLE" and r.detail["retryable"] is True, r)
    check("no ownership was taken for any of them and every row is still unresolved",
          all([await db.owners.owner_of(CONN, db.owners.CART, c) is None for c in ids]) and all([(await row_of(c))["state"] == "UNKNOWN_OUTCOME" for c in ids]))
    check("the platform error text is not in the response", "RAW-PLATFORM" not in repr(r.detail))

    print("\n4. Settled rows on a recycled id keep their documented behaviour")
    await pay(FakePlatform(["decline"]), "R-G1", "0002")
    m = MintWithLookup(["R-G1"])
    r = await mint(m)
    check("a declined row on a recycled id is forgotten and the id used", r["cart_id"] == "R-G1" and await row_of("R-G1") is None, (r, await row_of("R-G1")))
    await pay(FakePlatform(["ok"]), "R-H1")
    m = MintWithLookup(["R-H1"])
    r = await mint(m)
    check("a settled paid row with no platform evidence that this id is the same basket is forgotten (a brand new empty basket must not read 'already bought')",
          r["cart_id"] == "R-H1" and await row_of("R-H1") is None, (r, await row_of("R-H1")))

    print("\n5. Zero data")
    m = MintWithLookup(["R-NEW"])
    r = await mint(m)
    check("an id with no history is simply used", r["cart_id"] == "R-NEW" and r["payment_state"] is None, r)


async def main():
    from engine.db import create_schema, dispose

    await create_schema()
    try:
        await checks()
    finally:
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
