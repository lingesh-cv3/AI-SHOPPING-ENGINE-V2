"""Compatibility ACROSS mission stages (AC-3, AC-5, AC-6, AC-8).

Stage-fill (a merchant-declared complement of something in the cart that is also an option of another OPEN stage), optional extras (a
declared complement that belongs to no stage), forward pairing between the options of two open stages, the existing count line staying
consistent with the named labels, and the cards NEVER being re-ordered (label-only).

    python tests/shopper_v2/test_complete_set_cross_stage.py
"""
from __future__ import annotations

import os
import re
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_cross_stage.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_cross_stage.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import standard_catalogue, start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. Stage-fill and an optional extra after the shoe is really in the cart")
    cat = cs_catalogue()
    next(p for p in cat if p.product_id == "S-A").compatible_with.append("E-2")  # the shoe also declares an ENERGY item
    shop = FakeShop(cat)
    sid, spec, turn = await start_mission(shop, MSG)
    adv = await advance(shop, sid, "S-A")
    text, cards = adv
    tr = g.COMPLETE_TRACE.get() or {}
    check("the open socks stage labels its declared option", {"option": "K-1", "anchor": "S-A"} in tr.get("labels", []), tr.get("labels"))
    check("a declared complement that is an option of ANOTHER open stage is a stage-fill for that stage",
          tr.get("stage_fill") == [{"option": "E-2", "anchor": "S-A", "stage": "energy"}]
          and "For your energy stage, the merchant lists Energy Chew Bar as going with your Alpha Running Shoes" in text, (tr.get("stage_fill"), text[-420:]))
    check("a declared complement that is an option of NO open stage is an optional extra, mapped to the mission, citing its anchor",
          "Optional extra, not on your list - the merchant lists Recovery Slide" in text and "as going with your Alpha Running Shoes" in text, text[-520:])
    check("the extra is a CARD", "X-SLIDE" in ids(cards), ids(cards))
    check("the stage-fill item is NOT also offered as an extra (one role per product per reply)", "E-2" not in tr.get("extras_offered_ids", []), tr.get("extras_offered_ids"))

    print("\n2. The existing count line and the named labels cannot disagree (AC-3)")
    m = re.search(r"The merchant confirms (\d+) of these options", text)
    check("the existing count line is still produced, with its exact wording", m is not None, text[-520:])
    check("its number equals the number of named labels in the same reply", m is not None and int(m.group(1)) == len(tr.get("labels", [])) == 1,
          (m and m.group(1), tr.get("labels")))

    print("\n3. Forward pairing between two open stages' options (AC-6)")
    shop2 = FakeShop(cs_catalogue())
    sid2, _s, t2 = await start_mission(shop2, MSG)
    check("with a declared pair between shoes and socks, the FIRST prompt notes it (no cart needed)",
          "Alpha Running Shoes is also listed by the merchant as going with Crew Socks, an option for your socks stage" in t2.reply, t2.reply[-400:])
    plain = FakeShop(standard_catalogue())
    sid3, _s, t3 = await start_mission(plain, MSG)
    check("with no declaration it says nothing about pairs", "also listed by the merchant" not in t3.reply and "The merchant lists" not in t3.reply, t3.reply[-300:])

    print("\n3b. An option of a stage the shopper already satisfied is a substitute, never an 'extra'")
    shop5 = FakeShop(cs_catalogue())
    sid5, _s5, _t5 = await start_mission(shop5, MSG)
    done = await advance(shop5, sid5, "S-B", "K-2", "E-2")  # K-2 declares the Energy Gel (E-1), but the shopper picked the Chew Bar (E-2) for that stage
    check("the closing summary does NOT offer the energy gel the socks declare (it was an option of the energy stage they closed)",
          "E-1" not in ids(done[1]) and "Energy Gel Box" not in done[0].split("Cart total")[-1], (ids(done[1]), done[0][-300:]))

    print("\n4. Label-only: the cards are never re-ordered (AC-8)")
    adv2 = await advance(shop2, sid2, "S-A")
    declared_cards = [c for c in ids(adv2[1]) if c in ("K-1", "K-2")]
    plain_adv = await advance(plain, sid3, "S-A")
    plain_cards = [c for c in ids(plain_adv[1]) if c in ("K-1", "K-2")]
    check("the socks cards keep the SAME order with and without a declaration (the declared option is not moved up)", declared_cards == plain_cards == ["K-1", "K-2"],
          (declared_cards, plain_cards))
    shop4 = FakeShop(cs_catalogue())
    next(p for p in shop4.products.values() if p.product_id == "K-2").compatible_with.append("S-A")  # now the SECOND sock is the declared one
    sid4, _s, _t = await start_mission(shop4, MSG)
    adv4 = await advance(shop4, sid4, "S-A")
    check("even when the declared option is the LAST one, it stays last (not promoted)", [c for c in ids(adv4[1]) if c in ("K-1", "K-2")] == ["K-1", "K-2"],
          ids(adv4[1]))
    check("the extras come after every stage option, never above them", ids(adv4[1]).index("X-SLIDE") > max(ids(adv4[1]).index("K-1"), ids(adv4[1]).index("K-2")), ids(adv4[1]))


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
