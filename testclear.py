import asyncio


async def main():
    from engine.api.deps import engine
    from engine.db import create_schema, record_case, dispose

    await create_schema()
    a = engine.registry.adapter_for("conn_demo")

    cart = await a.create_cart()
    await a.add_to_cart(cart.cart_id, "P1001", variant_id="P1001-8")
    await a.add_to_cart(cart.cart_id, "P1005", variant_id="P1005-8")

    before = await a.get_cart(cart.cart_id)
    print(f"\nbefore: {before.item_count} item(s)")

    cid = await record_case(
        connection_id="conn_demo",
        friction=None,
        query="remove all",
        cart_id=cart.cart_id,
        order_id=None,
        session_id="t",
        reasoning={"used_model": True},
        decision={
            "proposed": [{"action_type": "CLEAR_CART", "parameters": {}}],
            "selected_action": "CLEAR_CART",
            "selection_reason": "t",
        },
        risk={
            "outcome": "AUTO",
            "rule": "AUTO_CLEARED",
            "reason": "safe",
            "financial": False,
        },
    )

    r = await engine.execution.execute_case("conn_demo", cid)
    print(f"\nsucceeded : {r.succeeded}")
    print(f"summary   : {r.summary}")
    print(f"shopper   : {r.shopper_summary}")
    print(f"error     : {r.error_code}")

    after = await a.get_cart(cart.cart_id)
    print(f"\nafter: {after.item_count} item(s)")

    await dispose()


asyncio.run(main())