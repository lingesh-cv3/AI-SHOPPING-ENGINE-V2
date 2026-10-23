"""The complete-the-set code is READ-ONLY and changed none of the authoritative layers (AC-13, AC-14).

(a) Static: `mission_complements.py` imports only the standard library and `engine.execution.product_relations`; it names no ActionType,
    no payment/ledger/cart-write function, no reasoning service and no events module; its only adapter call is `get_product`.
(b) Dynamic: a whole guided run that surfaces labels, forward pairs, stage-fills, extras and the closing paragraph, on a platform that
    RECORDS every attempted write, makes zero attempts.
(c) git: nothing changed under engine/db, risk, decision, execution (except the additive events.py), adapters, shared, or the add path
    (`fulfillment.py`, `cart_proposal.py`), nor `compatibility.py`; events.py only gained lines. No migration: an old mission item loads.

    python tests/shopper_v2/test_complete_set_no_direct_mutation.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_no_mutation.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_no_mutation.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue  # noqa: E402

failures = 0
ALLOWED_IMPORTS = {"__future__", "logging", "dataclasses", "decimal", "engine.execution.product_relations"}
FORBIDDEN_NAMES = ("ActionType", "add_to_cart", "create_cart", "checkout", "release_payment", "begin_payment", "payment_settled",
                   "execute_case", "claim_pending", "accept_cart_proposal_core", "start_fulfillment", "ReasoningService", "synthesize")


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. Static scan of mission_complements.py")
    src_path = os.path.join(ROOT, "engine", "shopper_v2", "mission_complements.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    check("it imports only the standard library and engine.execution.product_relations", imported <= ALLOWED_IMPORTS, sorted(imported - ALLOWED_IMPORTS))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("it names no ActionType, cart/payment write, fulfillment, reasoning or proposal function", not [n for n in FORBIDDEN_NAMES if n in names],
          [n for n in FORBIDDEN_NAMES if n in names])
    calls = sorted({n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "adapter"})
    check("the only adapter method it calls is get_product", calls == ["get_product"], calls)
    text = open(src_path, encoding="utf-8").read().lower()
    check("it never reads categories or descriptions off a product to relate two products", ".categories" not in text and ".description" not in text
          and "categories=" not in text)
    check("no platform-specific branch (the same code serves every platform)", not any(w in text for w in ("kettle", "northfield", "conn_demo", "conn_kettle")))

    print("\n2. Dynamic: a whole run that surfaces everything makes zero attempted writes")
    shop = FakeShop(cs_catalogue())
    sid, _s, turn = await start_mission(shop, MSG)
    out = await advance(shop, sid, "S-A", "K-1", "E-1")
    await g.handle_command(adapter=shop, command="next", message="next", state=turn.state, cart_id="C1", memory_facts=None)
    check("labels, forward pairs, extras and the closing paragraph were all produced", out is not None and "Optional extra" in out[0] and "X-SLIDE" in [p.product_id for p in out[1]],
          out and out[0][-200:])
    check("the platform recorded ZERO attempted writes (an exception would have been swallowed, so the attempts are what is asserted)", shop.write_attempts == [], shop.write_attempts)

    print("\n3. git: nothing authoritative changed; the only change under engine/execution is additive")
    try:
        d = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "engine/db", "engine/risk", "engine/decision", "engine/execution",
                            ":(exclude)engine/execution/events.py", "adapters", "shared", "engine/shopper_v2/fulfillment.py",
                            "engine/shopper_v2/cart_proposal.py", "engine/shopper_v2/compatibility.py"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if d.returncode != 0:
            print("  skip    git unavailable here (not counted as a pass or a failure)")
        else:
            check("git diff vs HEAD is empty for db, risk, decision, execution (except events.py), adapters, shared, fulfillment, cart_proposal, compatibility",
                  not d.stdout.strip(), d.stdout.split())
            n = subprocess.run(["git", "diff", "--numstat", "HEAD", "--", "engine/execution/events.py"], cwd=ROOT, capture_output=True, text=True, timeout=60)
            parts = (n.stdout.split() + ["0", "0"])[:2]
            check("events.py only GAINED lines (new event constants and a read-only aggregate)", parts[1] == "0", n.stdout.strip())
    except Exception as exc:  # noqa: BLE001
        print(f"  skip    git unavailable here ({type(exc).__name__}); not counted as a pass or a failure")
    from engine.shopper_v2.state import MissionItem

    old = MissionItem.model_validate({"item_id": "a", "label": "shoes", "guided": True, "candidates": ["P1"], "total_budget": "9000"})
    check("a mission item stored before this change still loads (no migration)", old.offered_complement_ids == [])


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
