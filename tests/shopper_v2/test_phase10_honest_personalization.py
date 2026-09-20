"""Fix for the live-reproduced finding (trade-off reasoning slice,
PROGRESS.md's Shopper V2 section): `respond()`'s discovery synthesis could
fabricate a personalization claim ("this aligns with your priority for
lightweight footwear") with nothing in `ShoppingStateV2` to back it -
`respond.py` had an anti-fabrication guard for a fictional CART claim but
none for a fictional PRIORITY-ATTRIBUTION claim. Mirrors
`engine/api/chat.py::_honest_synthesis`'s own, fully-hardened structural
fix: the model's free text is never trusted to make a personalization
claim - only a validated, real stored term (via `cited_reason`) may
produce the engine-written attribution clause; anything else falls back
to the plain, fact-only summary.

    python tests/shopper_v2/test_phase10_honest_personalization.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.models import Availability, Money, Product  # noqa: E402

from engine.reasoning.service import Synthesis  # noqa: E402
from engine.shopper_v2.evidence import ProductEvidence, TurnEvidence  # noqa: E402
from engine.shopper_v2.respond import respond  # noqa: E402
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


_PRODUCT = Product(
    product_id="P1001",
    title="Trailblazer Running Shoe",
    description="Lightweight daily trainer with a cushioned midsole.",
    price=Money(amount="4299.00", currency="INR"),
    availability=Availability.IN_STOCK,
)
_EVIDENCE = TurnEvidence(
    query="running shoes",
    products=[
        ProductEvidence(
            product_id="P1001", exists=True, hard_constraints_passed=True,
            relevance_verified=True, relevance_score=2, weak_match=False,
        )
    ],
)


class _ScriptedReasoning:
    def __init__(self, synthesis: Synthesis):
        self._synthesis = synthesis

    async def synthesize(self, **kwargs):
        return self._synthesis


async def test_uncited_personalization_claim_is_discarded() -> None:
    """The EXACT live reproduction: `priorities == []`, no valid
    `cited_reason`, yet the model's free text makes a priority-attribution
    claim - must never reach the shopper."""
    state = ShoppingStateV2(session_id="s1", connection_id="c")  # priorities=[]
    reasoning = _ScriptedReasoning(
        Synthesis(
            text="This option perfectly aligns with your priority for lightweight footwear.",
            used_model=True,
            personalizes=True,
            cited_reason=None,
        )
    )
    reply, used_model = await respond(
        reasoning=reasoning, message="lightweight is what matters",
        evidence=_EVIDENCE, products=[_PRODUCT], state=state,
    )
    check("the fabricated priority claim never reaches the shopper", "priority" not in reply.lower())
    check("falls back to the deterministic summary, not the model text", used_model is False)
    check("the deterministic fallback still names the real product", "Trailblazer" in reply)


async def test_cited_reason_not_actually_stored_is_discarded() -> None:
    """`cited_reason` naming something that was NEVER actually captured in
    state (a stronger, still-live-reproducible variant of the same bug -
    the model can name a plausible-sounding reason that simply isn't real)
    must also be discarded, not trusted just because a field was filled."""
    state = ShoppingStateV2(session_id="s2", connection_id="c", priorities=["cushioning"])
    reasoning = _ScriptedReasoning(
        Synthesis(
            text="Since durability matters most to you, this is the clear winner.",
            used_model=True,
            personalizes=True,
            cited_reason="durability",  # never actually stored - only "cushioning" was
        )
    )
    reply, used_model = await respond(
        reasoning=reasoning, message="what do you have", evidence=_EVIDENCE,
        products=[_PRODUCT], state=state,
    )
    check("an uncorroborated cited_reason is rejected, not trusted", "durability" not in reply)
    check("falls back to the deterministic summary", used_model is False)


async def test_valid_cited_reason_produces_the_deterministic_clause() -> None:
    """A `cited_reason` that DOES match a real, currently-stored term is
    trusted - but the ENGINE writes the attribution clause from the real
    matched term, never the model's own free text verbatim (the SIXTH/
    SEVENTEENTH-gap discipline `_honest_synthesis` established)."""
    state = ShoppingStateV2(session_id="s3", connection_id="c", priorities=["lightweight"])
    reasoning = _ScriptedReasoning(
        Synthesis(
            text="Given your love of lightweight gear above all else, this wins easily.",
            used_model=True,
            personalizes=True,
            cited_reason="lightweight",
        )
    )
    reply, used_model = await respond(
        reasoning=reasoning, message="what do you have", evidence=_EVIDENCE,
        products=[_PRODUCT], state=state,
    )
    check("a validated citation is honored", "lightweight" in reply.lower())
    check("used_model reflects the model's real contribution", used_model is True)
    check(
        "the model's own free text is never used verbatim - only the real matched term",
        "above all else" not in reply,
    )


async def test_no_personalization_claim_at_all_is_used_as_is() -> None:
    """A plain, non-personalizing, fact-only reply with no citation still
    gets discarded in favor of the plain fallback too, matching
    `_honest_synthesis`'s own final, most-hardened state exactly - not a
    regression, a deliberate, documented trade-off (fewer natural-sounding
    replies for zero remaining ungrounded-claim path)."""
    state = ShoppingStateV2(session_id="s4", connection_id="c")
    reasoning = _ScriptedReasoning(
        Synthesis(
            text="The Trailblazer Running Shoe is a lightweight daily trainer.",
            used_model=True,
            personalizes=False,
            cited_reason=None,
        )
    )
    reply, used_model = await respond(
        reasoning=reasoning, message="what do you have", evidence=_EVIDENCE,
        products=[_PRODUCT], state=state,
    )
    check("an uncited reply falls back to the deterministic summary too", used_model is False)
    check("the fallback still names the real product", "Trailblazer" in reply)


def main() -> int:
    asyncio.run(test_uncited_personalization_claim_is_discarded())
    asyncio.run(test_cited_reason_not_actually_stored_is_discarded())
    asyncio.run(test_valid_cited_reason_produces_the_deterministic_clause())
    asyncio.run(test_no_personalization_claim_at_all_is_used_as_is())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all honest-personalization checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
