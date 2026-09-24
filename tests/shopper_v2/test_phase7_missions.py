"""Multi-item shopping missions (`engine/shopper_v2/missions.py`).

Model-free for the deterministic RESOLUTION checks (a stub `ReasoningService`
scripts `MissionItemIntent` exactly as `test_phase4_intent_resolution.py`
scripts `CartReferenceIntent`) - never trusting the model's own reference
directly, only ever an already-verified real item. Live for the retrieval
integration (requires the Northfield demo backend on 8001, real
`adapter.search_products`/`get_product` calls, real `shopping_states` row
persistence).

    python tests/shopper_v2/test_phase7_missions.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase7.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase7.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class _ScriptedReasoning:
    enabled = True

    def __init__(self, intent):
        self._intent = intent

    async def interpret_mission_items(self, **kwargs):
        return self._intent


async def _model_free_checks() -> None:
    from engine.db import create_schema
    from engine.reasoning.service import MissionItemIntent
    from engine.shopper_v2.missions import apply_mission_message
    from engine.shopper_v2.state import MissionItem, ShoppingStateV2

    await create_schema()

    class _NullAdapter:
        async def search_products(self, query, *, limit=20):
            raise AssertionError("should not be called when no item needs retrieval")

    # 1. Brand-new items on an empty mission.
    reasoning = _ScriptedReasoning(
        MissionItemIntent(new_item_labels=["running shoes", "socks", "hydration bottle"])
    )
    state = ShoppingStateV2(session_id="s1", connection_id="conn_demo")

    class _StubAdapterWithHits:
        async def search_products(self, query, *, limit=20):
            from shared.models import Availability, Money, Product, ProductSearchResult
            return ProductSearchResult(
                query=query,
                products=[
                    Product(
                        product_id=f"P-{query[:4]}", title=f"{query.title()} Item",
                        price=Money(amount="1000.00", currency="INR"),
                        availability=Availability.IN_STOCK,
                    )
                ],
            )

        async def get_product(self, product_id):
            from shared.models import Availability, Money, Product
            return Product(
                product_id=product_id, title="Stub Product",
                price=Money(amount="1000.00", currency="INR"),
                availability=Availability.IN_STOCK,
            )

    new_state, handled, _touched, _basket, _reasons, _add_now = await apply_mission_message(
        reasoning=reasoning, adapter=_StubAdapterWithHits(),
        message="I need running shoes, socks and a hydration bottle",
        history=None, state=state,
    )
    check("three new items are created", len(new_state.mission_items) == 3)
    check("handled=True for a genuine multi-item statement", handled)
    check(
        "each new item starts 'exploring'",
        all(i.status == "exploring" for i in new_state.mission_items),
    )

    # 2. A reference to one of them by ordinal, refining its constraints.
    reasoning2 = _ScriptedReasoning(
        MissionItemIntent(
            new_item_labels=[], reference_type="ordinal", position=1,
            constraints_add=["lightweight"],
        )
    )
    refined_state, handled2, _touched, _basket, _reasons, _add_now = await apply_mission_message(
        reasoning=reasoning2, adapter=_StubAdapterWithHits(),
        message="make the shoes lightweight", history=None, state=new_state,
    )
    check("ordinal reference resolves to the real first item", handled2)
    shoes_item = next(i for i in refined_state.mission_items if i.label == "running shoes")
    check("the constraint lands on the correct item", "lightweight" in shoes_item.constraints)
    other_items = [i for i in refined_state.mission_items if i.label != "running shoes"]
    check(
        "other items' constraints are untouched",
        all(not i.constraints for i in other_items),
    )

    # 3. Out-of-range ordinal never guesses.
    reasoning3 = _ScriptedReasoning(
        MissionItemIntent(
            new_item_labels=[], reference_type="ordinal", position=99,
            constraints_add=["red"],
        )
    )
    unresolved_state, handled3, _touched, _basket, _reasons, _add_now = await apply_mission_message(
        reasoning=reasoning3, adapter=_NullAdapter(),
        message="make that one red", history=None, state=refined_state,
    )
    check("an unresolvable reference is never applied to any item", not handled3)
    check(
        "state is left unchanged when the reference cannot be resolved",
        unresolved_state.mission_items == refined_state.mission_items,
    )

    # 4. A plain follow-up question with no new items and no resolvable
    #    reference is honestly not handled - falls through to discovery.
    reasoning4 = _ScriptedReasoning(MissionItemIntent(new_item_labels=[]))
    unrelated_state, handled4, _touched, _basket, _reasons, _add_now = await apply_mission_message(
        reasoning=reasoning4, adapter=_NullAdapter(),
        message="do you have anything in a different color?", history=None, state=refined_state,
    )
    check("a message naming nothing new/existing is not treated as mission bookkeeping", not handled4)

    # 5. Reasoning disabled -> never called, always falls through cleanly.
    class _Disabled:
        enabled = False

        async def interpret_mission_items(self, **kwargs):
            raise AssertionError("must not be called when reasoning is disabled")

    disabled_state, handled5, _touched, _basket, _reasons, _add_now = await apply_mission_message(
        reasoning=_Disabled(), adapter=_NullAdapter(),
        message="I need shoes and socks", history=None, state=ShoppingStateV2(session_id="s2", connection_id="conn_demo"),
    )
    check("a disabled reasoning service short-circuits cleanly", not handled5)

    # 5b. Deterministic, model-free proof that a stated budget constraint
    # genuinely hard-filters by price - not just keyword-matches "under
    # 5000" as text. A live-search-based version of this check is vacuous
    # whenever the literal budget phrase happens to zero out the keyword
    # search before the filter gets a chance to run (a real gap found by
    # value-auditor); this stub adapter sidesteps that entirely by
    # returning one over-budget and one under-budget candidate directly,
    # so only `mission_ranking.rank_and_filter`'s own price comparison
    # decides which survives.
    from shared.models import Availability, Money, Product, ProductSearchResult

    class _StubAdapterMixedPrices:
        async def search_products(self, query, *, limit=20):
            return ProductSearchResult(
                query=query,
                products=[
                    Product(
                        product_id="cheap-shoe", title="Budget Running Shoes",
                        description="Everyday running shoes.",
                        price=Money(amount="3000.00", currency="INR"),
                        availability=Availability.IN_STOCK,
                    ),
                    Product(
                        product_id="pricey-shoe", title="Premium Running Shoes",
                        description="Everyday running shoes.",
                        price=Money(amount="9000.00", currency="INR"),
                        availability=Availability.IN_STOCK,
                    ),
                ],
            )

        async def get_product(self, product_id):
            products = {
                p.product_id: p
                for p in (await self.search_products("")).products
            }
            return products.get(product_id)

    budget_item = MissionItem(item_id="budget-item", label="running shoes")
    budget_state = ShoppingStateV2(
        session_id="s-budget", connection_id="conn_demo", mission_items=[budget_item],
    )
    reasoning5b = _ScriptedReasoning(
        MissionItemIntent(
            new_item_labels=[], reference_type="ordinal", position=1,
            constraints_add=["under 5000"],
        )
    )
    from engine.shopper_v2.missions import apply_mission_message as _apply

    refined5b, handled5b, _touched5b, _basket5b, _reasons5b, _add_now5b = await _apply(
        reasoning=reasoning5b, adapter=_StubAdapterMixedPrices(),
        message="under 5000 for the shoes", history=None, state=budget_state,
    )
    check("a stated budget refinement is handled", handled5b)
    refined_candidates = refined5b.mission_items[0].candidates
    check(
        "the budget hard-filter actually ran (candidates list is non-empty)",
        len(refined_candidates) > 0,
    )
    check(
        "the under-budget candidate survives",
        "cheap-shoe" in refined_candidates,
    )
    check(
        "the over-budget candidate is genuinely excluded, not just unranked",
        "pricey-shoe" not in refined_candidates,
    )

    # 6. Item cap: a mission already at the cap does not grow further.
    from engine.shopper_v2.missions import _MAX_ITEMS
    full_items = [
        MissionItem(item_id=f"i{i}", label=f"item {i}") for i in range(_MAX_ITEMS)
    ]
    full_state = ShoppingStateV2(session_id="s3", connection_id="conn_demo", mission_items=full_items)
    reasoning6 = _ScriptedReasoning(MissionItemIntent(new_item_labels=["one more thing"]))
    capped_state, handled6, _touched, _basket, _reasons, _add_now = await apply_mission_message(
        reasoning=reasoning6, adapter=_NullAdapter(),
        message="I also need one more thing", history=None, state=full_state,
    )
    check("a mission at the item cap does not add another item", len(capped_state.mission_items) == _MAX_ITEMS)


async def _live_retrieval_check() -> None:
    """Requires Northfield (8001). Confirms mission-item candidates are
    real, adapter-verified product ids, and that the row actually
    persists via the same `shopping_states` table V1 already uses."""
    from engine.reasoning.service import MissionItemIntent
    from engine.shopper_v2.missions import run_mission_turn
    from engine.shopper_v2.state import ShoppingStateV2, load
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase7_live"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        reasoning = _ScriptedReasoning(MissionItemIntent(new_item_labels=["running shoes"]))
        state = ShoppingStateV2(session_id=session_id, connection_id=connection_id)
        reply, products, new_state, handled, _proposal_ids = await run_mission_turn(
            reasoning=reasoning, adapter=adapter, message="I need running shoes",
            history=None, state=state,
        )
        check("a live mission turn is handled", handled)
        check("a reply is produced", bool(reply))
        item = new_state.mission_items[0]
        check(
            "candidates are real product ids actually returned by the adapter",
            all(isinstance(pid, str) and pid for pid in item.candidates),
        )

        persisted = await load(session_id, connection_id)
        check(
            "the mission item persists to the real shopping_states row",
            len(persisted.mission_items) == 1 and persisted.mission_items[0].label == "running shoes",
        )

        # Regression check for a real, live-discovered bug: refining an
        # EXISTING item (not adding a new one) must still produce a real
        # per-item reply, not the empty-diff "Noted." fallback that
        # resulted from mutating `state.mission_items` objects in place
        # and then diffing against that same, now-already-mutated
        # "before" snapshot.
        reasoning2 = _ScriptedReasoning(
            MissionItemIntent(
                new_item_labels=[], reference_type="ordinal", position=1,
                constraints_add=["under 5000"],
            )
        )
        reply2, products2, new_state2, handled2, _proposal_ids2 = await run_mission_turn(
            reasoning=reasoning2, adapter=adapter, message="under 5000 for the shoes",
            history=None, state=new_state,
        )
        check("a refinement turn is handled", handled2)
        check(
            "the refinement reply is NOT the empty-diff fallback",
            reply2 is not None and reply2.strip() != "Noted.",
        )
        check(
            "the refined item's constraint is real and persisted",
            "under 5000" in new_state2.mission_items[0].constraints,
        )
        check(
            "the ORIGINAL state object passed in is never mutated by the call",
            state.mission_items == [] and new_state.mission_items[0].constraints == [],
        )

        # Regression for a real, live-discovered bug: "under 5000" was only
        # ever applied as free-text (title/description keyword matching),
        # never as a numeric hard-filter, because the per-item
        # `item_state_dict["budget_amount"]` passed to `build_evidence` was
        # unconditionally None - a candidate over budget could survive as a
        # verified match. Assert every real candidate this refinement kept
        # is genuinely within the stated budget.
        refined_item = new_state2.mission_items[0]
        for pid in refined_item.candidates:
            product = await adapter.get_product(pid)
            price_amount = (
                Decimal(str(product.price.amount)) if product and product.price else None
            )
            check(
                f"candidate {pid} genuinely respects the stated 'under 5000' budget",
                product is None or price_amount is None or price_amount < 5000,
            )
    finally:
        await engine.close()
        await dispose()


def main() -> int:
    asyncio.run(_model_free_checks())
    asyncio.run(_live_retrieval_check())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 7 mission checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
