"""Cross-stage compatibility needs EVIDENCE: only the merchant's declared `compatible_with` counts (AC-1, AC-2, AC-4).

Positive (declared, either direction), negative (same category and shared title words but NOT declared: never labelled, never offered)
and unknown (no data at all, said as missing data). "incompatible" is never shown to a shopper.

    python tests/shopper_v2/test_complete_set_evidence.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_evidence.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_evidence.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import start_mission, standard_catalogue  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids, product, says_forbidden  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import mission_complements as mc

    print("1. pair_status: only a merchant declaration is evidence")
    a, b = product("A", "Alpha Running Shoes", 100, compatible_with=["B"], categories=["Footwear"]), product("B", "Crew Socks", 10)
    decoy = product("D", "Trail Socks", 10, categories=["Socks"])
    check("declared on the FIRST product -> confirmed", mc.pair_status(a, b) == "confirmed")
    check("declared on the SECOND product (reverse direction) -> confirmed", mc.pair_status(b, a) == "confirmed")
    check("same category and a shared title word but NOT declared -> unknown", mc.pair_status(b, decoy) == "unknown")
    check("nothing declared at all -> unknown", mc.pair_status(product("X", "One", 1), product("Y", "Two", 1)) == "unknown")
    check("there is no 'incompatible' value anywhere in the new module", not hasattr(mc, "compat_status")
          and mc.pair_status(a, decoy) in ("confirmed", "unknown"))

    print("\n2. Guided mission: a declared pair is named, an undeclared look-alike is not")
    shop = FakeShop(cs_catalogue())
    sid, spec, turn = await start_mission(shop, MSG)
    check("the mission starts", turn is not None)
    check("at the FIRST prompt (empty cart) a declared pair across stages is named with 'listed by the merchant'",
          "Alpha Running Shoes is also listed by the merchant as going with Crew Socks" in turn.reply, turn.reply[-420:])
    check("the undeclared look-alike (Trail Socks) is NOT paired with the shoe", "Trail Socks, an option" not in turn.reply and
          "going with Trail Socks" not in turn.reply, turn.reply[-420:])
    adv = await advance(shop, sid, "S-A")
    text = adv[0] if adv else ""
    check("with the shoe really in the cart the socks stage names the pair: 'the merchant lists Crew Socks as going with your Alpha Running Shoes'",
          "The merchant lists Crew Socks as going with your Alpha Running Shoes" in text, text[-520:])
    check("the look-alike is not named in any 'the merchant lists' sentence", "lists Trail Socks" not in text, text[-520:])
    from engine.shopper_v2 import guided_mission as g

    labels = (g.COMPLETE_TRACE.get() or {}).get("labels", [])
    check("the trace names exactly the declared pair and nothing else", labels == [{"option": "K-1", "anchor": "S-A"}], labels)

    print("\n3. Adjacency is NOT evidence (AC-4): the decoys are never offered")
    offered = (g.COMPLETE_TRACE.get() or {}).get("extras_offered_ids", [])
    check("the declared add-on is offered", "X-SLIDE" in offered, offered)
    check("the same-category, same-title-word undeclared 'Foam Slide' is NOT offered as an extra", "X-DECOY" not in offered and "X-DECOY" not in ids(adv[1]), offered)
    check("...nor does its name appear in the reply", "Foam Slide" not in text, text[-200:])

    print("\n4. Unknown: a platform with no declarations says nothing confident, and never 'incompatible'")
    plain = FakeShop(standard_catalogue())
    psid, _s, pturn = await start_mission(plain, MSG)
    padv = await advance(plain, psid, "S-A")
    ptext = padv[0] if padv else ""
    check("no label, no extra, no forward pair", "The merchant lists" not in ptext and "Optional extra" not in ptext and "also listed by the merchant" not in pturn.reply,
          ptext[-300:])
    check("the existing line says it is missing data, not a problem", "isn't confirmed either way" in ptext and "missing data" in ptext, ptext[-300:])

    print("\n5. The words 'incompatible / won't work / doesn't fit' never appear, anywhere")
    every = [turn.reply, text, pturn.reply, ptext]
    final = await advance(shop, sid, "K-1", "E-1")
    every.append(final[0] if final else "")
    check("none of the replies in the whole journey uses a forbidden phrase", all(not says_forbidden(r) for r in every), [says_forbidden(r) for r in every])


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
