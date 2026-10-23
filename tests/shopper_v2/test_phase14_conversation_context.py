"""Phase 1 of the Rev. 2 architecture migration: `ConversationContext` /
`LastRendered` are additive - built every turn, consulted by nobody yet.
This test proves `build_context()` assembles correctly from real
`ShoppingStateV2` shapes, independent of the graph."""

import asyncio
import sys

sys.path.insert(0, ".")

from engine.shopper_v2.conversation_context import build_context  # noqa: E402
from engine.shopper_v2.state import (  # noqa: E402
    FulfillmentItem,
    LastRendered,
    ShoppingStateV2,
    Topic,
)


class _Line:
    def __init__(self, product_id, title, quantity):
        self.product_id = product_id
        self.title = title
        self.quantity = quantity


class _Cart:
    def __init__(self, lines):
        self.lines = lines


class _Adapter:
    def __init__(self, cart=None, raise_on_cart=False):
        self._cart = cart
        self._raise = raise_on_cart

    async def get_cart(self, cart_id):
        if self._raise:
            raise RuntimeError("adapter down")
        return self._cart


def _state_with_focus_stack() -> ShoppingStateV2:
    active = Topic(topic_id="t2", kind="discovery", label="socks", product_ids=["P3002"], state="active")
    paused = Topic(
        topic_id="t1", kind="discovery", label="running shoes",
        product_ids=["P1001", "P1009"], state="paused",
    )
    mission = Topic(topic_id="t3", kind="mission", label="race list", product_ids=[], state="paused")
    return ShoppingStateV2(
        session_id="sess1", connection_id="conn_demo",
        must_have=["waterproof"], priorities=["cushioning"],
        focus_stack=[active, paused, mission],
        last_rendered=LastRendered(product_ids=["P3002"], source_capability="discovery", source_topic_id="t2"),
        fulfillment_queue=[FulfillmentItem(product_id="P1005", title="Fell Runner Trail Shoe", status="needs_variant")],
    )


def main() -> bool:
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  {'ok' if cond else 'FAILED'}\t{label}")
        if not cond:
            ok = False

    async def scenario_basic():
        state = _state_with_focus_stack()
        adapter = _Adapter(cart=_Cart([_Line("P1001", "Trailblazer Running Shoe", 1)]))
        ctx = await build_context(
            state=state, adapter=adapter, cart_id="BSK001",
            history=["Shopper: show me shoes", "You: here are some shoes"],
            memory_facts=[{"key": "prefers_lightweight", "value": "true"}],
        )
        check("active_topic is the one Topic marked active", ctx.active_topic is not None and ctx.active_topic.topic_id == "t2")
        check("paused_topics excludes the active one", all(t.topic_id != "t2" for t in ctx.paused_topics))
        check("paused_topics includes the paused discovery topic", any(t.topic_id == "t1" for t in ctx.paused_topics))
        check("current_mission is the kind=mission topic", ctx.current_mission is not None and ctx.current_mission.topic_id == "t3")
        check("last_rendered is read straight from state, unmodified", ctx.last_rendered is not None and ctx.last_rendered.product_ids == ["P3002"])
        check("constraints_summary carries must_have", ctx.constraints_summary["must_have"] == ["waterproof"])
        check("constraints_summary carries priorities", ctx.constraints_summary["priorities"] == ["cushioning"])
        check("history is passed through", ctx.history == ["Shopper: show me shoes", "You: here are some shoes"])
        check("durable_facts parsed from memory_facts", len(ctx.durable_facts) == 1 and ctx.durable_facts[0].key == "prefers_lightweight")
        check("cart_snapshot is a real, fresh read", ctx.cart_snapshot is not None and ctx.cart_snapshot.cart_id == "BSK001")
        check("cart_snapshot lines match the real cart", ctx.cart_snapshot.lines[0].product_id == "P1001")
        check("fulfillment_queue passed through", len(ctx.fulfillment_queue) == 1 and ctx.fulfillment_queue[0].product_id == "P1005")

    async def scenario_no_cart():
        state = _state_with_focus_stack()
        adapter = _Adapter()
        ctx = await build_context(state=state, adapter=adapter, cart_id=None)
        check("no cart_id -> cart_snapshot is None, never guessed", ctx.cart_snapshot is None)

    async def scenario_cart_fetch_fails():
        state = _state_with_focus_stack()
        adapter = _Adapter(raise_on_cart=True)
        ctx = await build_context(state=state, adapter=adapter, cart_id="BSK999")
        check("adapter failure degrades honestly (None), never raises", ctx.cart_snapshot is None)

    async def scenario_empty_state():
        state = ShoppingStateV2(session_id="sess2", connection_id="conn_demo")
        adapter = _Adapter()
        ctx = await build_context(state=state, adapter=adapter, cart_id=None)
        check("zero-data session: active_topic is None, not fabricated", ctx.active_topic is None)
        check("zero-data session: paused_topics is empty", ctx.paused_topics == [])
        check("zero-data session: last_rendered is None", ctx.last_rendered is None)
        check("zero-data session: durable_facts empty when none passed", ctx.durable_facts == [])

    asyncio.run(scenario_basic())
    asyncio.run(scenario_no_cart())
    asyncio.run(scenario_cart_fetch_fails())
    asyncio.run(scenario_empty_state())

    if ok:
        print("\n  all conversation-context checks passed")
    else:
        print("\n  FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if main() else 1)
