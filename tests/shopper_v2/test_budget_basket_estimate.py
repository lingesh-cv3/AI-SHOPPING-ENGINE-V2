"""Basket estimate: an honest verdict (AC-4, AC-5), including zero data and a platform that cannot price.

"Fits" is claimed only when it is KNOWN; "over" only from a certain lower bound; everything else is "unknown", said plainly. Each
check below would fail an implementation that always answers "fits" or treats an unreadable cart as an empty one.

    python tests/shopper_v2/test_budget_basket_estimate.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_estimate.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_estimate.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

from _budget_fixtures import FakeShop, product, standard_catalogue  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def item(label, prices, currency="INR", stage="pending"):
    from engine.shopper_v2.state import MissionItem

    return MissionItem(item_id=uuid.uuid4().hex[:8], label=label, guided=True, stage=stage,
                       candidates=[f"{label}-{i}" for i in range(len(prices))], candidate_prices=[None if p is None else str(p) for p in prices],
                       price_currency=currency if any(p is not None for p in prices) else None)


async def checks():
    from engine.shopper_v2 import basket_budget as bb
    from engine.shopper_v2 import guided_mission as g
    from engine.shopper_v2.state import ShoppingStateV2

    print("1. Pure estimate: verdict from known prices only")
    fits = bb.estimate(budget=Decimal(8000), budget_currency=None, cart=None, cart_state="none",
                       items=[item("shoes", [1999, 4999, 8999]), item("socks", [499, 799]), item("energy", [899, 1799])])
    check("lows 3397 and typicals 6997 within 8000 -> fits", fits.verdict == "fits" and fits.sum_lows == Decimal(3397), (fits.verdict, fits.sum_lows))
    tight = bb.estimate(budget=Decimal(5000), budget_currency=None, cart=None, cart_state="none",
                        items=[item("shoes", [1999, 4999, 8999]), item("socks", [499, 799]), item("energy", [899, 1799])])
    check("lows fit (3397) but typicals (6997) do not -> tight", tight.verdict == "tight", tight.verdict)
    over = bb.estimate(budget=Decimal(3000), budget_currency=None, cart=None, cart_state="none",
                       items=[item("shoes", [1999, 4999]), item("socks", [499, 799]), item("energy", [899])])
    check("lows alone (3397) exceed 3000 -> over", over.verdict == "over", over.verdict)

    print("\n2. AC-4: an unpriced stage")
    over_unpriced = bb.estimate(budget=Decimal(2000), budget_currency=None, cart=None, cart_state="none",
                                items=[item("shoes", [1999, 4999]), item("socks", [499]), item("mystery", [None, None])])
    check("'over' is still decided from the known lower bound when one stage is unpriced (2498 > 2000)", over_unpriced.verdict == "over",
          over_unpriced.verdict)
    fitting_unpriced = bb.estimate(budget=Decimal(9000), budget_currency=None, cart=None, cart_state="none",
                                   items=[item("shoes", [1999, 4999]), item("mystery", [None])])
    check("an unpriced stage with an otherwise fitting total is UNKNOWN, never 'fits'", fitting_unpriced.verdict == "unknown",
          fitting_unpriced.verdict)
    check("...and the unpriced stage is named", fitting_unpriced.unknown_stages == ["mystery"], fitting_unpriced.unknown_stages)
    text = bb.verdict_text(fitting_unpriced)
    check("...and the wording says it cannot tell (no confident claim)", "can't tell" in text and "should fit" not in text and "Good so far" not in text, text)

    print("\n3. AC-5: currencies")
    mixed_budget = bb.estimate(budget=Decimal(8000), budget_currency="USD", cart=None, cart_state="none",
                               items=[item("shoes", [1999], currency="INR")])
    check("a budget currency that differs from the candidates' -> unknown", mixed_budget.verdict == "unknown", mixed_budget.verdict)
    check("...and the reply says currency is the reason", "different currencies" in bb.verdict_text(mixed_budget), bb.verdict_text(mixed_budget))
    # a cart whose lines disagree on currency cannot be totalled
    from shared.models import Cart, CartLine, Money

    mixed_cart = Cart(cart_id="C1", currency="INR", subtotal=Money(amount=Decimal(0), currency="INR"), lines=[
        CartLine(line_id="1", product_id="S-A", title="a", quantity=1, unit_price=Money(amount=Decimal(10), currency="INR"),
                 line_total=Money(amount=Decimal(10), currency="INR")),
        CartLine(line_id="2", product_id="U-1", title="u", quantity=1, unit_price=Money(amount=Decimal(10), currency="USD"),
                 line_total=Money(amount=Decimal(10), currency="USD"))])
    mc = bb.estimate(budget=Decimal(8000), budget_currency=None, cart=mixed_cart, cart_state="ok", items=[item("socks", [499])])
    check("a cart with mixed currencies cannot be totalled -> unknown, committed unknown (not 0)", mc.verdict == "unknown" and mc.committed is None,
          (mc.verdict, mc.committed))

    print("\n3b. A candidate already in the real cart is not a price the shopper can still choose")
    in_cart = Cart(cart_id="C1", currency="INR", subtotal=Money(amount=Decimal(1999), currency="INR"), lines=[
        CartLine(line_id="1", product_id="shoes2-0", title="cheapest shoe", quantity=1, unit_price=Money(amount=Decimal(1999), currency="INR"),
                 line_total=Money(amount=Decimal(1999), currency="INR"))])
    stage = item("shoes2", [1999, 4999])
    ex = bb.estimate(budget=Decimal(9000), budget_currency=None, cart=in_cart, cart_state="ok", items=[stage])
    check("the cheapest candidate is already in the cart, so the stage's low is the next one (4999), not 1999", ex.lows.get(stage.item_id) == Decimal(4999), ex.lows)

    print("\n4. Zero data and an unreadable cart")
    nocart = bb.estimate(budget=Decimal(8000), budget_currency=None, cart=None, cart_state="none", items=[item("shoes", [1999])])
    check("no cart yet: committed is a KNOWN 0 and the verdict is computed from the stage prices", nocart.committed == 0 and nocart.verdict == "fits",
          (nocart.committed, nocart.verdict))
    unreadable = bb.estimate(budget=Decimal(8000), budget_currency=None, cart=None, cart_state="unreadable", items=[item("shoes", [1999])])
    check("an unreadable cart is NOT an empty cart: committed unknown, verdict unknown", unreadable.committed is None and unreadable.verdict == "unknown",
          (unreadable.committed, unreadable.verdict))
    check("...and the wording names the cause", "couldn't read your cart" in bb.verdict_text(unreadable), bb.verdict_text(unreadable))
    nobudget = bb.estimate(budget=None, budget_currency=None, cart=None, cart_state="none", items=[item("shoes", [1999])])
    check("no budget stated: no verdict and no budget talk at all", nobudget.verdict == "none" and bb.verdict_text(nobudget) == "", nobudget.verdict)

    print("\n5. Guided mission end to end: zero data, no budget")
    shop = FakeShop(standard_catalogue())
    state = ShoppingStateV2(session_id=f"est_{uuid.uuid4().hex[:8]}", connection_id="conn_demo")
    spec = g.parse_mission("I want shoes, socks and something for energy")
    turn = await g.start(adapter=shop, spec=spec, message="I want shoes, socks and something for energy", state=state, cart_id=None, memory_facts=None)
    check("a mission without a budget says nothing about a budget", turn is not None and "budget" not in turn.reply.lower() and turn.budget_trace is None,
          turn and turn.reply[-200:])
    check("the stage items now carry real prices for the estimate",
          all(len(i.candidate_prices) == len(i.candidates) and i.price_currency == "INR" for i in turn.state.mission_items if i.guided),
          [(i.label, i.candidate_prices) for i in turn.state.mission_items])
    old_row = {"item_id": "x", "label": "shoes", "guided": True, "stage": "pending", "candidates": ["P1"]}
    from engine.shopper_v2.state import MissionItem

    mi = MissionItem.model_validate(old_row)
    check("an item stored before this change still loads (no migration)", mi.candidate_prices == [] and mi.price_currency is None)


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
