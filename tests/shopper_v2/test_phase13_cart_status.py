"""Cart intelligence / "what's in my cart" (`engine/shopper_v2/cart_status.py`).

Closes a real V1 -> V2 parity gap found by the functional-parity audit:
V1's `engine/api/chat.py` has a dedicated `CHECK_CART_STATUS` branch (its
own comment there explains why - "show cart" used to fall through to an
unrelated browse/recommend answer); V2's `capability.py` already routed
this to `Capability.CART_STATUS`, but `orchestrator.py` treated it as
"not yet available" until this slice. This test proves the real,
end-to-end path: a real cart, a real added line, a real
`CHECK_CART_STATUS` dispatch (unmodified, the same one V1 uses) through
the real Decision Engine/Risk Gate/ExecutionService, via
`engine.shopper_v2.orchestrator.run_turn` - not just the lower-level
function in isolation.

Live: requires Northfield (8001). Model-independent for the capability
being exercised (capability routing for a cart-status question does not
depend on model availability the way discovery/mission do), but still
goes through `run_turn`'s full pipeline including the primary
understanding call, so a reachable model is required the same way other
live orchestrator tests in this suite require one - skips honestly if
not configured.

    python tests/shopper_v2/test_phase13_cart_status.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase13.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase13.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


async def _model_free_checks() -> None:
    """The lower-level function, isolated - no cart_id -> honest decline,
    no decision/gate/execution call at all."""
    from engine.shopper_v2.cart_status import run_cart_status_turn

    class _ExplodingDecision:
        async def decide(self, *a, **k):
            raise AssertionError("should not be called with no cart_id")

    reply, payload = await run_cart_status_turn(
        decision=_ExplodingDecision(), gate=None, execution=None,
        connection_id="conn_demo", session_id="s1", cart_id=None,
        message="what's in my cart?",
    )
    check("no cart_id -> honest decline, no decision call", payload is None)
    check("the decline reply is non-empty", bool(reply))


async def _live_checks() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    from engine.shopper_v2.orchestrator import run_turn

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = f"sess_test_phase13_{uuid.uuid4().hex[:8]}"
    adapter = engine.registry.adapter_for(connection_id)

    if engine.reasoning is None or not engine.reasoning.enabled:
        check("model not configured - live cart-status check honestly skipped", True)
        await engine.close()
        await dispose()
        return

    try:
        # 1. An empty real cart - a legitimate question with a legitimate
        #    answer, not a failure (mirrors CHECK_CART_STATUS's own
        #    dispatch, which explicitly treats an empty cart as success).
        empty_cart = await adapter.create_cart()
        empty_result = await run_turn(
            reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
            connection_id=connection_id, message="what's in my cart?",
            cart_id=empty_cart.cart_id,
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
        )
        check(
            "an empty-cart status question routes to cart_status "
            f"(got {empty_result.trace.get('capability')!r})",
            empty_result.trace.get("capability") == "cart_status",
        )
        check(
            "the reply honestly says the cart is empty, not a fabricated count",
            "empty" in empty_result.reply.lower(),
        )

        # 2. A real cart with a real line - the reply must name what's
        #    actually in it, from the real adapter read, never a guess.
        cart = await adapter.create_cart()
        await adapter.add_to_cart(cart.cart_id, "P3001", quantity=1)
        session_id_2 = f"sess_test_phase13_{uuid.uuid4().hex[:8]}"
        result = await run_turn(
            reasoning=engine.reasoning, adapter=adapter, session_id=session_id_2,
            connection_id=connection_id, message="what's in my cart right now?",
            cart_id=cart.cart_id,
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
        )
        check(
            "a real-cart status question routes to cart_status "
            f"(got {result.trace.get('capability')!r})",
            result.trace.get("capability") == "cart_status",
        )
        check(
            "the reply names a real item count, not zero, for a non-empty cart",
            result.trace.get("cart_item_count") == 1,
        )
        check(
            "asking about the cart never mutates it",
            len((await adapter.get_cart(cart.cart_id)).lines) == 1,
        )
    finally:
        await engine.close()
        await dispose()


async def main() -> None:
    await _model_free_checks()
    await _live_checks()


if __name__ == "__main__":
    asyncio.run(main())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all cart-status checks passed")
    print()
    sys.exit(1 if failures else 0)
