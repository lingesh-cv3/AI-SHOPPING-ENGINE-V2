"""Zero data, a platform that cannot, and a platform that is down (AC-11, AC-18).

No cart and no declarations say nothing confident; a platform with no `compatible_with` data (the generic Magento/Shopware case) gets one
honest sentence and never "incompatible"; an unreadable cart gives no anchors and the turn still replies; a failing product read skips
that product with no raw error; a failing adapter at the summary still renders the summary.

    python tests/shopper_v2/test_complete_set_zero_data_platform.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_zero_platform.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_zero_platform.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import standard_catalogue, start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids, says_forbidden  # noqa: E402

failures = 0
NO_DATA_SENTENCE = "I found no merchant-confirmed pairings for what's in your cart, so I'm not suggesting extras."


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. Zero data: no cart yet, nothing declared")
    shop = FakeShop(standard_catalogue())
    sid, _s, turn = await start_mission(shop, MSG, cart_id=None)
    tr = g.COMPLETE_TRACE.get() or {}
    check("a fresh mission with no cart id and no declarations says nothing about pairs or extras", "The merchant lists" not in turn.reply and "Optional extra" not in turn.reply
          and "also listed by the merchant" not in turn.reply, turn.reply[-300:])
    check("the trace records WHY it is silent", tr.get("reason") == "no_anchors", tr)
    check("no '0' or invented figures about add-ons", "0 extras" not in turn.reply and "0 pairs" not in turn.reply)

    print("\n2. A platform that cannot: anchors exist but NOTHING is declared (the generic client)")
    plain = FakeShop(standard_catalogue())
    psid, _p, pturn = await start_mission(plain, MSG)
    mid = await advance(plain, psid, "S-A")
    check("stage prompts stay silent about extras and pairs", "Optional extra" not in mid[0] and "The merchant lists" not in mid[0], mid[0][-300:])
    fin = await advance(plain, psid, "K-1", "E-1")
    check("the closing summary says plainly that no merchant-confirmed pairings were found, so no extras", NO_DATA_SENTENCE in fin[0], fin[0][-320:])
    check("it never says incompatible / won't work / doesn't fit", not says_forbidden(fin[0]) and not says_forbidden(mid[0]), (says_forbidden(fin[0]), says_forbidden(mid[0])))
    check("and offers no cards", fin[1] == [], ids(fin[1]))

    print("\n3. A platform that is down")
    down = FakeShop(cs_catalogue())
    dsid, _d, _dt = await start_mission(down, MSG)
    down.put_in_cart("S-A")
    down.cart_fails = True
    t = await g.handle_command(adapter=down, command="next", message="next", state=_dt.state, cart_id="C1", memory_facts=None)
    tr = g.COMPLETE_TRACE.get() or {}
    check("an unreadable cart gives no anchors and no extras, and the turn still replies", t is not None and "Optional extra" not in t.reply and tr.get("anchors") == []
          and tr.get("reason") == "cart_unreadable", (tr, t and t.reply[-200:]))
    check("nothing is invented from stored state (no label naming the shoe)", "going with your Alpha Running Shoes" not in t.reply, t.reply[-250:])
    check("no raw error text", "cart unreadable" not in t.reply and "RuntimeError" not in t.reply, t.reply[-200:])
    down.cart_fails = False

    shop2 = FakeShop(cs_catalogue())
    sid2, _s2, _t2 = await start_mission(shop2, MSG)
    shop2.put_in_cart("S-A")
    shop2.product_fails = {"K-1", "X-SLIDE"}  # the declared sock option and the extra both fail to load
    a = await g.advance_from_cart(adapter=shop2, session_id=sid2, connection_id="conn_demo", cart_id="C1")
    check("a product that fails to load is skipped; the reply lists only what rendered, with no raw error", a is not None and "X-SLIDE" not in ids(a[1]) and "cannot fetch" not in a[0],
          a and a[0][-300:])
    shop2.product_fails = set()

    shop3 = FakeShop(cs_catalogue())
    sid3, _s3, _t3 = await start_mission(shop3, MSG)
    await advance(shop3, sid3, "S-A", "K-1")
    shop3.product_fails = set(shop3.products)  # every product read fails at the end
    shop3.put_in_cart("E-1")
    last = await g.advance_from_cart(adapter=shop3, session_id=sid3, connection_id="conn_demo", cart_id="C1")
    check("with every product read failing at the summary, the summary still renders (no complement paragraph, no exception)",
          last is not None and "That completes your list" in last[0] and "Optional extra" not in last[0], last and last[0][-250:])


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
