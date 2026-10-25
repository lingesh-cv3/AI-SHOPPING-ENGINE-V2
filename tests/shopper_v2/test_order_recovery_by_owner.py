"""Order recovery: "did my order go through?" with NO order number resolves to the newest order the CALLER holds - not only one this conversation recorded.

Before: `latest_order` read only conversation Case rows, so an order paid from the cart panel (which claims the order in `db.owners` but records no turn),
or asked about from a new conversation / another device, got "I don't have an order number". Now ownership answers, and it is the same ownership check
every other order read uses: a different shopper, a different merchant and a caller with no keys never resolve someone else's order.

Needs the Northfield demo merchant on 8001 (a real checkout), like test_phase6_order_status.py.

    python tests/shopper_v2/test_order_recovery_by_owner.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_order_recovery.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    if os.path.exists(_DB_FILE + _suffix):
        os.remove(_DB_FILE + _suffix)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_order_recovery.db"
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail=None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + ("" if ok or detail is None else f"   {detail!r}"))
    if not ok:
        failures += 1


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose, owners as db_owners
    from engine.shopper_v2.order_status import run_order_status_turn

    await create_schema()
    engine = build_engine()
    conn = "conn_demo"
    adapter = engine.registry.adapter_for(conn)

    async def buy(owner_key: str) -> str:
        cart = await adapter.create_cart()
        await adapter.add_to_cart(cart.cart_id, "P3001", quantity=1)
        res = await adapter.checkout(cart.cart_id, idempotency_key=str(uuid.uuid4()))
        assert res.succeeded
        # exactly what the cart-panel checkout does: claim the order, record NO conversation turn
        await db_owners.claim(conn, db_owners.ORDER, res.order.order_id, owner_key)
        return res.order.order_id

    async def ask(session: str, keys, connection: str = conn, message: str = "did my order go through?"):
        return await run_order_status_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution, connection_id=connection,
            session_id=session, message=message, hinted_order_id=None, owner_keys=keys,
        )

    try:
        print("1. An order paid from the cart panel is found from a brand-new conversation")
        older = await buy("acct_ann")
        await asyncio.sleep(0.05)
        newer = await buy("acct_ann")
        reply, payload = await ask("sess_never_seen_before", ["acct_ann", "browser_ann"])
        check("the newest order the caller holds is the one looked up", payload is not None and payload.get("order_id") == newer, payload)
        check("not the older one", payload is not None and payload.get("order_id") != older)

        print("2. Another device / a guest browser that holds the same order key sees it; a different shopper does not")
        _, p_other = await ask("sess_phone", ["browser_bob"])
        check("a different shopper has no order to look up (honest ask, no fabrication)", p_other is None)
        _, p_none = await ask("sess_x", [])
        check("a caller with no keys owns nothing", p_none is None)
        _, p_empty = await ask("sess_y", None)
        check("owner_keys=None behaves as before (no lookup by ownership)", p_empty is None)

        print("3. Tenant isolation: the same keys at another merchant resolve nothing")
        _, p_kettle = await ask("sess_k", ["acct_ann", "browser_ann"], connection="conn_kettle")
        check("no Northfield order is reachable through Kettle", p_kettle is None)
        check("latest_owned is scoped to the connection", await db_owners.latest_owned("conn_kettle", db_owners.ORDER, ["acct_ann"]) is None)

        print("4. A named order id still wins over 'the latest'")
        reply2, p_named = await run_order_status_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution, connection_id=conn,
            session_id="sess_named", message=f"what about {older}?", hinted_order_id=older, owner_keys=["acct_ann"],
        )
        check("the named (older) order is the one reported", p_named is not None and p_named.get("order_id") == older)
    finally:
        await engine.close()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
