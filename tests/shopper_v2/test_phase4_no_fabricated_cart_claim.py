"""Phase 4 live finding: `respond.py`'s discovery synthesis (an LLM call)
fabricated "I've added them to your cart" on a plain discovery turn that
never called `cart_proposal.py`, `decision`, `gate`, or `execution` at
all. Reproduced against the real running engine on 2026-09-18. Fixed by a
deterministic, closed-class check in `respond.py`/`compare.py` - this test
proves that check directly, model-free.

    python tests/shopper_v2/test_phase4_no_fabricated_cart_claim.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.models import Availability, Money, Product  # noqa: E402

from engine.reasoning.service import ReasoningService, Synthesis  # noqa: E402
from engine.shopper_v2.evidence import ProductEvidence, TurnEvidence  # noqa: E402
from engine.shopper_v2.respond import respond, _claims_a_cart_action  # noqa: E402
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class _FabricatingReasoning:
    """A stand-in `ReasoningService` whose `synthesize` always returns the
    exact fabricated claim reproduced live - proves the guard catches it
    without depending on a real, non-deterministic model call."""

    async def synthesize(self, **kwargs):
        return Synthesis(
            text="Here's a great option for you. I've added them to your cart as requested.",
            used_model=True,
        )


def test_claims_detector_catches_the_reproduced_phrase():
    check(
        "the reproduced fabricated phrase is detected",
        _claims_a_cart_action("I've added them to your cart as requested."),
    )
    check(
        "an ordinary discovery sentence is NOT flagged",
        not _claims_a_cart_action(
            "The Trailblazer Running Shoe is a lightweight daily trainer."
        ),
    )


async def test_respond_falls_back_when_synthesis_fabricates_a_claim():
    product = Product(
        product_id="P1001",
        title="Trailblazer Running Shoe",
        description="Lightweight daily trainer.",
        price=Money(amount="4299.00", currency="INR"),
        availability=Availability.IN_STOCK,
    )
    evidence = TurnEvidence(
        query="running shoes",
        products=[
            ProductEvidence(
                product_id="P1001",
                exists=True,
                hard_constraints_passed=True,
                relevance_verified=True,
                relevance_score=2,
                weak_match=False,
            )
        ],
    )
    state = ShoppingStateV2(session_id="s", connection_id="c")

    reply, used_model = await respond(
        reasoning=_FabricatingReasoning(),
        message="I need running shoes",
        evidence=evidence,
        products=[product],
        state=state,
    )
    check(
        "the fabricated cart claim never reaches the shopper",
        "added" not in reply.lower() and "cart" not in reply.lower(),
    )
    check("falls back to the deterministic summary, not the model text", used_model is False)
    check("the deterministic fallback still names the real product", "Trailblazer" in reply)


def main() -> int:
    test_claims_detector_catches_the_reproduced_phrase()
    asyncio.run(test_respond_falls_back_when_synthesis_fabricates_a_claim())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all anti-fabrication checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
