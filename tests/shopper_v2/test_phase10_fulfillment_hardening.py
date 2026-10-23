"""Tests for engine/shopper_v2/fulfillment.py's Step 6 hardening:
explicit skip semantics, interruption-preserves-the-queue (not deletes
it), and the shared real-cart precondition inside start_fulfillment.
Run directly: python -m tests.shopper_v2.test_phase10_fulfillment_hardening
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from engine.shopper_v2.fulfillment import (  # noqa: E402
    looks_like_skip_request,
    start_fulfillment,
    continue_fulfillment,
)
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402

_checks = []


def check(label: str, ok: bool) -> None:
    _checks.append((label, ok))
    print(("  ok      " if ok else "  FAIL    ") + label)


class _Variant:
    def __init__(self, variant_id, title, attributes, availability="IN_STOCK", quantity_available=5):
        self.variant_id = variant_id
        self.title = title
        self.attributes = attributes
        self.availability = availability
        self.quantity_available = quantity_available


class _Product:
    def __init__(self, product_id, title, variants, price=None):
        self.product_id = product_id
        self.title = title
        self.variants = variants
        self.price = price


class _Line:
    def __init__(self, product_id):
        self.product_id = product_id


class _Cart:
    def __init__(self, lines):
        self.lines = lines


class _StubAdapter:
    def __init__(self, products, cart_lines=None):
        self._products = {p.product_id: p for p in products}
        self._cart_lines = cart_lines or []

    async def get_product(self, product_id):
        return self._products[product_id]

    async def get_cart(self, cart_id):
        return _Cart(self._cart_lines)


class _Case:
    succeeded = True


async def _fake_run_trusted_action(**kwargs):
    return _Case(), "case_1", False


def main() -> bool:
    import engine.shopper_v2.fulfillment as fulfillment_mod
    fulfillment_mod.run_trusted_action = _fake_run_trusted_action

    async def scenario_skip_and_interruption():
        shoe = _Product("P1", "Trailblazer Running Shoe", [
            _Variant("v7", "7", {"size": "7"}), _Variant("v8", "8", {"size": "8"}),
        ])
        socks = _Product("P2", "Performance Socks", [])
        adapter = _StubAdapter([shoe, socks])
        state = ShoppingStateV2(session_id="s1", connection_id="conn_demo")

        reply, added = await start_fulfillment(
            adapter=adapter, decision=None, gate=None, execution=None,
            connection_id="conn_demo", session_id="s1", cart_id=None,
            message="add both", state=state, product_ids=["P1", "P2"], quantity=1,
        )
        check("first item (needs variant) asks a real question", "Trailblazer" in reply and "7" in reply and "8" in reply)
        check("queue has one pending needs_variant item", any(i.status == "needs_variant" for i in state.fulfillment_queue))

        # An unrelated interruption must NOT delete the queue.
        reply2, added2, handled2 = await continue_fulfillment(
            adapter=adapter, decision=None, gate=None, execution=None,
            connection_id="conn_demo", session_id="s1", cart_id=None,
            message="what is your return policy", state=state,
        )
        check("unrelated interruption is not handled by fulfillment", handled2 is False)
        check("queue SURVIVES an unrelated interruption (not deleted)", len(state.fulfillment_queue) > 0)
        check("the shoe item is still needs_variant after the interruption", any(i.status == "needs_variant" for i in state.fulfillment_queue))

        # Explicit skip of the pending item.
        reply3, added3, handled3 = await continue_fulfillment(
            adapter=adapter, decision=None, gate=None, execution=None,
            connection_id="conn_demo", session_id="s1", cart_id=None,
            message="skip this one", state=state,
        )
        check("explicit skip is handled", handled3 is True)
        check("skip reply reports the socks (auto-added, no variant needed) and the skip", "Skipped" in reply3 or "Added" in reply3)
        check("queue is now empty (sequence complete)", state.fulfillment_queue == [])

    async def scenario_already_in_cart():
        gel = _Product("P3", "Energy Gel", [])
        adapter = _StubAdapter([gel], cart_lines=[_Line("P3")])
        state = ShoppingStateV2(session_id="s2", connection_id="conn_demo")
        reply, added = await start_fulfillment(
            adapter=adapter, decision=None, gate=None, execution=None,
            connection_id="conn_demo", session_id="s2", cart_id="cart1",
            message="add the gel", state=state, product_ids=["P3"], quantity=1,
        )
        check("already-in-cart item is never re-queued", added == [])
        check("already-in-cart is reported honestly", "already" in reply.lower())

    asyncio.run(scenario_skip_and_interruption())
    asyncio.run(scenario_already_in_cart())

    check("looks_like_skip_request: real answer never misread as skip", not looks_like_skip_request("size 9", [{"variant_id": "v", "label": "9"}]))

    return all(ok for _, ok in _checks)


if __name__ == "__main__":
    ok = main()
    print()
    print("all fulfillment-hardening checks passed" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)
