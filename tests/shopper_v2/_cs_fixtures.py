"""Fixtures for the cross-stage compatibility / complete-the-set tests (not a test itself: the runner only collects test_*.py).

`cs_catalogue()` is a small platform with DECLARED pairs and deliberate DECOYS:

    shoes   S-A Alpha 1999 (declares K-1 and X-SLIDE), S-B Bravo 4999, S-C Charlie 8999
    socks   K-1 Crew Socks 499, K-2 Trail Socks 799 (declares E-1)   <- K-2 is a decoy for S-A: same category, shared title word, UNDECLARED
    energy  E-1 Energy Gel Box 1799, E-2 Energy Chew Bar 899
    extras  X-SLIDE Recovery Slide 1499 (declared by S-A; belongs to no stage), X-DECOY Foam Slide 999 (same category and title word as
            the slide, declared by NOBODY)

Built on `_budget_fixtures.FakeShop`, which records every attempted platform write. Imports nothing from `engine`.
"""
from __future__ import annotations

from _budget_fixtures import FakeShop, product  # noqa: F401  (re-exported for the tests)

MSG = "I want shoes, socks and something for energy, total budget 9000"


def cs_catalogue(currency="INR"):
    c = currency
    return [
        product("S-A", "Alpha Running Shoes", 1999, currency=c, compatible_with=["K-1", "X-SLIDE"], categories=["Footwear"]),
        product("S-B", "Bravo Running Shoes", 4999, currency=c, categories=["Footwear"]),
        product("S-C", "Charlie Running Shoes", 8999, currency=c, categories=["Footwear"]),
        product("K-1", "Crew Socks", 499, currency=c, categories=["Socks"]),
        product("K-2", "Trail Socks", 799, currency=c, compatible_with=["E-1"], categories=["Socks"]),
        product("E-1", "Energy Gel Box", 1799, currency=c, categories=["Nutrition"]),
        product("E-2", "Energy Chew Bar", 899, currency=c, categories=["Nutrition"]),
        product("X-SLIDE", "Recovery Slide", 1499, currency=c, categories=["Footwear Recovery"]),
        product("X-DECOY", "Foam Slide", 999, currency=c, categories=["Footwear Recovery"]),
    ]


def ids(cards):
    return [p.product_id for p in (cards or [])]


async def advance(shop, sid, *product_ids, conn="conn_demo"):
    """Put each product in the REAL cart (as the add path would) and run the mission-advance hook; returns the last `(text, cards)`."""
    from engine.shopper_v2 import guided_mission as g

    out = None
    for pid in product_ids:
        shop.put_in_cart(pid)
        out = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id=conn, cart_id="C1")
    return out


FORBIDDEN = ("incompatible", "not compatible", "won't work", "doesn't fit", "does not fit")


def says_forbidden(text):
    low = (text or "").lower()
    return [w for w in FORBIDDEN if w in low]
