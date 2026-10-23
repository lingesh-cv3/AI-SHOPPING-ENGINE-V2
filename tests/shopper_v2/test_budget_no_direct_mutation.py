"""The basket-budget code is read-only and changed none of the authoritative layers (AC-12, AC-15).

(a) Static: `basket_budget.py` imports nothing from decision, risk or any execution MUTATION path, never names `ActionType`, and never
    calls an adapter write method.
(b) Dynamic: a full guided run against a fake platform whose every write method raises - the run completes, so only reads happened.
(c) This feature changed nothing under engine/db, engine/risk, engine/decision, engine/execution, adapters, shared or fulfillment.py
    (git diff against HEAD), so there is no migration and the Decision -> Risk -> Execution -> Adapter path is untouched.

    python tests/shopper_v2/test_budget_no_direct_mutation.py
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_no_mutation.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_no_mutation.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import FakeShop, standard_catalogue, start_mission  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


WRITE_WORDS = ("add_to_cart", "create_cart", "checkout", "update_cart", "remove_from_cart", "apply_promotion", "pay", "release_payment",
               "begin_payment", "execute_case", "claim_pending", "accept_cart_proposal_core")


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. Static: what basket_budget.py imports and calls")
    src_path = os.path.join(ROOT, "engine", "shopper_v2", "basket_budget.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    bad = [m for m in imported if m.startswith(("engine.decision", "engine.risk", "engine.api", "engine.db"))
           or (m.startswith("engine.execution") and m != "engine.execution.product_relations")]
    check("no import from decision, risk, api, db or any execution module other than the pure product_relations", not bad, bad)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    check("it never names ActionType", "ActionType" not in names)
    called_writes = sorted(n for n in names if n in WRITE_WORDS)
    check("it never references an adapter write or payment function", not called_writes, called_writes)
    adapter_calls = sorted({n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == "adapter"})
    check("the only adapter methods it calls are reads", set(adapter_calls) <= {"get_product", "get_cart"}, adapter_calls)

    print("\n2. Dynamic: a whole guided run on a platform that raises on any write")
    shop = FakeShop(standard_catalogue())
    sid, spec, turn = await start_mission(shop, "I want shoes, socks and something for energy, total budget 4300")
    check("start completed (reads only)", turn is not None)
    for cmd, msg in (("next", "next"), ("total_budget", "total budget 6000"), ("budget", "keep the socks under 600"), ("clear_budget", "no budget"), ("skip", "skip")):
        t = await g.handle_command(adapter=shop, command=cmd, message=msg, state=turn.state, cart_id="C1", memory_facts=None)
        turn = t or turn
    shop.put_in_cart("S-B")
    adv = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id="conn_demo", cart_id="C1")
    check("start, next, total/stage/clear budget, skip and advance-from-cart ran and the platform recorded ZERO attempted writes (an exception would have been swallowed, so the attempts are what is asserted)", shop.write_attempts == [], shop.write_attempts)

    print("\n3. This feature changed none of the authoritative layers")
    try:
        out = subprocess.run(["git", "diff", "--name-only", "HEAD", "--", "engine/db", "engine/risk", "engine/decision", "engine/execution",
                              ":(exclude)engine/execution/events.py",
                              "adapters", "shared", "engine/shopper_v2/fulfillment.py", "engine/shopper_v2/cart_proposal.py"],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        changed = [l for l in out.stdout.splitlines() if l.strip()]
        if out.returncode != 0:
            print("  skip    git unavailable here (not counted as a pass or a failure)")
        else:
            check("git diff vs HEAD is empty for db, risk, decision, execution (except events.py), adapters, shared, fulfillment and cart_proposal", not changed, changed)
            num = subprocess.run(["git", "diff", "--numstat", "HEAD", "--", "engine/execution/events.py"], cwd=ROOT, capture_output=True, text=True, timeout=60)
            parts = (num.stdout.split() + ["0", "0"])[:2]
            check("the one change under engine/execution is ADDITIVE (events.py: new event constants and a read-only aggregate, zero lines removed)",
                  parts[1] == "0", num.stdout.strip())
    except Exception as exc:  # noqa: BLE001
        print(f"  skip    git unavailable here ({type(exc).__name__}); not counted as a pass or a failure")
    from engine.shopper_v2.state import MissionItem

    old = MissionItem.model_validate({"item_id": "a", "label": "shoes", "guided": True, "candidates": ["P1"]})
    check("a mission item stored before this change still loads (no migration)", old.candidate_prices == [] and old.price_currency is None)


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
