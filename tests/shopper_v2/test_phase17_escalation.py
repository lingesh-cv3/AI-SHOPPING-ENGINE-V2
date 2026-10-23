"""Phase 5 of the Rev. 2 architecture migration: bounded re-interpretation
(`escalate_ambiguous_reference`). Deterministic - the underlying model-call
pattern itself was already proven live in Phase 2's own tests
(`test_phase15_interpretation.py`); this tests the ESCALATION WIRING (does
it call with only the narrowed candidates, does re-verification against
that narrowed set work) using a stub reasoning service, not live variance.

Run: python -m tests.shopper_v2.test_phase17_escalation
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from engine.reasoning.service import InterpretationResult  # noqa: E402
from engine.shopper_v2.conversation_context import ConversationContext  # noqa: E402
from engine.shopper_v2.interpretation import REASONING_BUDGET, escalate_ambiguous_reference  # noqa: E402
from engine.shopper_v2.verification import verify  # noqa: E402
from shared.models import Money, Product  # noqa: E402

_checks = []


def check(label, cond):
    _checks.append((label, bool(cond)))


class _StubReasoning:
    """Records exactly what it was called with, returns a fixed claim -
    stands in for a real model call so this test is deterministic."""

    def __init__(self, claim: InterpretationResult):
        self.claim = claim
        self.last_call_kwargs: dict | None = None

    async def interpret(self, **kwargs):
        self.last_call_kwargs = kwargs
        return self.claim


def _product(pid, title, price):
    return Product(product_id=pid, title=title, price=Money(amount=price, currency="INR"))


ambiguous_candidates = [
    _product("P1", "Trailblazer Running Shoe", 4299),
    _product("P2", "Starter Road Running Shoe", 1999),
]
ctx = ConversationContext(session_id="s1", connection_id="conn_demo")


async def scenario_narrows_the_call():
    stub = _StubReasoning(
        InterpretationResult(
            capability="compare", context_action="continue",
            reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[2],
            rationale="picking the second of the two narrowed candidates",
        ),
    )
    claim = await escalate_ambiguous_reference(
        reasoning=stub, message="the second one", context=ctx, candidates=ambiguous_candidates,
    )
    check(
        "escalation call is scoped to ONLY the ambiguous candidates, never the full catalog/history",
        stub.last_call_kwargs is not None
        and len(stub.last_call_kwargs.get("last_rendered_products") or []) == 2,
    )
    check("escalation returns a real InterpretationResult", claim is not None and claim.reference_kind == "ordinal")

    escalation_context = ctx.model_copy(
        update={
            "last_rendered": __import__("engine.shopper_v2.state", fromlist=["LastRendered"]).LastRendered(
                product_ids=[p.product_id for p in ambiguous_candidates], source_capability="escalation",
            ),
        },
    )
    verified = verify(claim, escalation_context, ambiguous_candidates)
    check(
        "re-verification against the narrowed set resolves unambiguously",
        verified.resolved_reference is not None
        and verified.resolved_reference.product_ids == ["P2"]
        and not verified.resolved_reference.ambiguous,
    )


async def scenario_escalation_can_still_fail_honestly():
    stub = _StubReasoning(
        InterpretationResult(
            capability="compare", context_action="continue",
            reference_scope="last_rendered", reference_kind="named", reference_names=["running shoe"],
            rationale="both still match this generic word",
        ),
    )
    claim = await escalate_ambiguous_reference(
        reasoning=stub, message="the running shoe", context=ctx, candidates=ambiguous_candidates,
    )
    verified = verify(claim, ctx, ambiguous_candidates)
    check(
        "escalation that's STILL ambiguous is reported honestly, never guessed",
        verified.resolved_reference is not None and verified.resolved_reference.ambiguous,
    )


def check_budget_is_configurable():
    check("REASONING_BUDGET is a named, documented config value, not a magic literal scattered in code", isinstance(REASONING_BUDGET, int) and REASONING_BUDGET >= 1)


def main() -> bool:
    asyncio.run(scenario_narrows_the_call())
    asyncio.run(scenario_escalation_can_still_fail_honestly())
    check_budget_is_configurable()
    ok = True
    for label, passed in _checks:
        print(f"  {'ok' if passed else 'FAILED'}\t{label}")
        ok = ok and passed
    print("\n  all escalation checks passed" if ok else "\n  FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
