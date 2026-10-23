"""Cross-stage compatibility is THREE-valued and never invents an incompatibility (AC-9).

`compatible` only for a pair the merchant declared compatible (either direction); `incompatible` ONLY for an explicitly declared
incompatible pair (today no data source supplies one, so it is unreachable in production - the guided replies never say it);
everything else is `unknown`, worded as missing data and not as a problem.

    python tests/shopper_v2/test_budget_cross_stage_compat.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_compat.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_compat.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import FakeShop, product, standard_catalogue, start_mission  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import basket_budget as bb
    from engine.shopper_v2 import guided_mission as g

    shoe = product("S-A", "Alpha Running Shoes", 1999, compatible_with=["K-1"])
    sock_declared = product("K-1", "Crew Socks", 499)
    sock_other = product("K-2", "Trail Socks", 799)
    plain_a, plain_b = product("X-1", "Thing One", 100), product("X-2", "Thing Two", 100)

    print("1. compat_status, pure")
    check("declared on ONE side counts as compatible (either direction)", bb.compat_status(shoe, sock_declared) == "compatible"
          and bb.compat_status(sock_declared, shoe) == "compatible")
    check("an undeclared pair is UNKNOWN, never incompatible", bb.compat_status(plain_a, plain_b) == "unknown", bb.compat_status(plain_a, plain_b))
    check("incompatible ONLY when explicitly declared incompatible", bb.compat_status(plain_a, plain_b, frozenset({"X-2"})) == "incompatible")
    check("a declared-compatible pair is not downgraded by an empty incompatible set", bb.compat_status(shoe, sock_declared, frozenset()) == "compatible")
    counts = bb.compat_counts([shoe], [sock_declared, sock_other])
    check("counts split the pairs", counts == {"compatible": 1, "unknown": 1, "incompatible": 0}, counts)

    print("\n2. Guided reply: a confirmed pair is reported, an unconfirmed one is worded as missing data")
    cat = standard_catalogue()
    for p in cat:
        if p.product_id == "S-A":
            p.compatible_with.append("K-1")
    shop = FakeShop(cat)
    sid, spec, turn = await start_mission(shop, "I want shoes, socks and something for energy, total budget 8000")
    shop.put_in_cart("S-A")
    adv = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id="conn_demo", cart_id="C1")
    text = adv[0] if adv else ""
    check("after the shoe is in the cart, the socks stage reports the merchant-confirmed pair", "merchant confirms 1 of these options" in text, text[:420])
    check("...and calls the other one 'not confirmed either way (missing data, not a problem)'", "aren't confirmed either way" in text and "not a problem" in text, text[:420])

    shop2 = FakeShop(standard_catalogue())
    sid2, _s, turn2 = await start_mission(shop2, "I want shoes, socks and something for energy, total budget 8000")
    shop2.put_in_cart("S-A")
    adv2 = await g.advance_from_cart(adapter=shop2, session_id=sid2, connection_id="conn_demo", cart_id="C1")
    text2 = adv2[0] if adv2 else ""
    check("with NO declared relationships the reply says 'isn't confirmed either way'", "isn't confirmed either way" in text2, text2[:420])
    check("NEGATIVE CONTROL: it never says incompatible / won't work for an undeclared pair",
          "incompatible" not in text2.lower() and "won't work" not in text2.lower() and "not compatible" not in text2.lower(), text2[:420])
    check("the trace records the pair counts", adv2 is not None and (g.BUDGET_TRACE.get() or {}).get("compat_pairs", {}).get("incompatible") == 0,
          g.BUDGET_TRACE.get())


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
