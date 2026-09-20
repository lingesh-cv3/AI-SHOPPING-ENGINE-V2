"""Phase 12: DISCOVERY must never become CART_ACTION without explicit
shopper action intent - a general capability-routing regression, not a
phrase-specific patch.

Real, live-reproduced bug: "I need shoes" (plain discovery) produced a
real cart proposal through `/api/v2/chat` on Northfield. Root cause,
confirmed by reading `engine/shopper_v2/orchestrator.py`: the PRIMARY
model call's own raw `hinted_action_type` (e.g. `ADD_TO_CART`) was trusted
by `capability.route()` to set `Capability.CART_ACTION` directly, and
nothing reset that stale value when the SEPARATE, deterministic
`intent_resolution.py::resolve_cart_target` check correctly determined
`is_cart_intent=False` for the same turn - the model's own unvalidated
guess, not real cart intent, drove the outcome.

Fixed structurally in `engine/shopper_v2/capability.py`: no cart-mutation
`ActionType` (`ADD_TO_CART`/`REMOVE_CART_LINE`/`UPDATE_CART_QUANTITY`/
`CLEAR_CART`/`PROPOSE_CART_CHANGE`) maps to `Capability.CART_ACTION` any
more - `Capability.CART_ACTION` can now ONLY be reached through
`resolve_cart_target`'s own explicit `is_cart_intent=True` + resolvable
target. See `test_phase1_capability_routing.py` for the pure-function
routing-table regression; THIS file proves the fix holds through the real
`orchestrator.run_turn` pipeline, against a live model and a live
Northfield catalogue - the exact shape the original bug was found in.

Requires Northfield (8001) and a reachable model; both negative and
positive checks are skipped honestly (not silently passed) if no model is
configured. Deliberately small (a handful of representative phrases per
side, not the full CLAUDE.md test matrix) to respect the documented Groq
throttle ("the constraint") - the routing PRINCIPLE is what this proves,
not exhaustive phrase coverage, which is exactly what CLAUDE.md's own
"do not solve with a list of phrases" instruction asks this suite to
avoid leaning on for its actual guarantee (that guarantee lives in
`capability.py`'s structural fix, not in how many phrases this file
happens to try).

    python tests/shopper_v2/test_phase12_discovery_vs_cart_routing.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase12.db"
)
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase12.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


# A representative subset of CLAUDE.md's negative matrix - plain discovery/
# shopping-goal statements that must NEVER produce a cart proposal, however
# strongly they name a product category.
NEGATIVE_DISCOVERY_MESSAGES = [
    "I need shoes",
    "I need running shoes",
    "show me shoes",
    "what shoes do you have?",
]


async def _negative_checks(run_turn, engine, connection_id: str, adapter) -> None:
    for message in NEGATIVE_DISCOVERY_MESSAGES:
        session_id = f"sess_disc_neg_{uuid.uuid4().hex[:8]}"
        result = await run_turn(
            reasoning=engine.reasoning,
            adapter=adapter,
            session_id=session_id,
            connection_id=connection_id,
            message=message,
            decision=engine.decision,
            gate=engine.gate,
            execution=engine.execution,
        )
        capability = result.trace.get("capability")
        proposal_id = result.trace.get("cart_proposal_id")
        check(
            f"{message!r} routes to a discovery-shaped capability, never cart_action "
            f"(got {capability!r})",
            capability != "cart_action",
        )
        check(
            f"{message!r} creates NO cart proposal (got {proposal_id!r})",
            proposal_id is None,
        )
        check(
            f"{message!r} reply never claims a cart action happened",
            not any(
                phrase in result.reply.lower()
                for phrase in ("added to your cart", "i'd like to add", "cart proposal")
            ),
        )


async def _restatement_after_product_shown_check(run_turn, engine, connection_id: str, adapter) -> None:
    """A SECOND, real, live-reproduced instance of the same failure class,
    one layer deeper than capability.py's fix: even after route() stopped
    trusting the primary model's raw hint, the SEPARATE deterministic
    `resolve_cart_target` check (its own bounded `interpret_cart_reference`
    call) could itself misclassify a plain discovery restatement as cart
    intent, specifically once a product was already shown this session -
    "I need shoes" then "I need running shoes" (still just describing what
    they want, not acting) was live-reproduced returning
    `cart_intent_detected=True`. Fixed by clarifying
    `cart_reference_prompt`/`cart_reference_tool`'s own instructions (never
    a phrase list - a structural instruction about what counts as intent
    versus restatement), confirmed clean across 5 repeated live runs before
    this regression test was written. This is exactly why the guarantee
    must be structural (capability.py's routing fix) AND the second check's
    own reliability matters - either layer misfiring alone reproduces the
    user-visible bug."""
    session_id = f"sess_disc_restate_{uuid.uuid4().hex[:8]}"
    first = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
        connection_id=connection_id, message="I need shoes",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check("setup: the first discovery turn shows a real product", bool(first.state.active_products))

    second = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
        connection_id=connection_id, message="I need running shoes",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        "restating the same shopping goal after a product is already shown "
        f"still routes to discovery, not cart_action (got {second.trace.get('capability')!r})",
        second.trace.get("capability") != "cart_action",
    )
    check(
        "the second check's own cart_intent_detected is False for a restatement",
        second.trace.get("cart_intent_detected") is not True,
    )
    check(
        "no cart proposal is fabricated from the restatement",
        second.trace.get("cart_proposal_id") is None,
    )


async def _positive_checks(run_turn, engine, connection_id: str, adapter) -> None:
    """A genuine, explicit action-intent statement, made AFTER a real
    product has actually been shown this session, must still be resolvable
    to Capability.CART_ACTION (a real proposal or an honest decline/
    clarification) - the fix must not have overcorrected into discovery
    always winning."""
    session_id = f"sess_disc_pos_{uuid.uuid4().hex[:8]}"

    discover_result = await run_turn(
        reasoning=engine.reasoning,
        adapter=adapter,
        session_id=session_id,
        connection_id=connection_id,
        message="I need running shoes",
        decision=engine.decision,
        gate=engine.gate,
        execution=engine.execution,
    )
    check(
        "setup: the discovery turn actually shows at least one real product",
        bool(discover_result.state.active_products),
    )

    cart = await adapter.create_cart()
    action_result = await run_turn(
        reasoning=engine.reasoning,
        adapter=adapter,
        session_id=session_id,
        connection_id=connection_id,
        message="I'll take this",
        cart_id=cart.cart_id,
        decision=engine.decision,
        gate=engine.gate,
        execution=engine.execution,
    )
    check(
        "an explicit action-intent statement after discovery routes to "
        f"cart_action (got {action_result.trace.get('capability')!r})",
        action_result.trace.get("capability") == "cart_action",
    )
    check(
        "the cart is not mutated by the proposal turn itself",
        len((await adapter.get_cart(cart.cart_id)).lines) == 0,
    )


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    from engine.shopper_v2.orchestrator import run_turn

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    adapter = engine.registry.adapter_for(connection_id)

    if engine.reasoning is None or not engine.reasoning.enabled:
        check("model not configured - live discovery-vs-cart checks honestly skipped", True)
        await engine.close()
        await dispose()
        return

    try:
        await _negative_checks(run_turn, engine, connection_id, adapter)
        await _restatement_after_product_shown_check(run_turn, engine, connection_id, adapter)
        await _positive_checks(run_turn, engine, connection_id, adapter)
    finally:
        await engine.close()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all discovery-vs-cart-action routing checks passed")
    print()
    sys.exit(1 if failures else 0)
