"""Friction/Recovery capability - the V2 home for the moments a shopper
hits something going wrong: a dead search, a promo code that didn't
apply, a payment that didn't go through.

Before this module, `storefront/src/App.tsx` synthesized a shopper-sounding
sentence ("I searched for X and nothing came up.") and sent it through
plain discovery, hoping the model would infer what kind of help was
actually needed from the sentence alone - the same shape CLAUDE.md's
roadmap explicitly calls a report/discovery-shaped answer, not a proper
capability, for a problem discovery was never built to solve. The frontend
already KNOWS precisely which friction happened; `chat_v2.py`'s new
`ChatRequestV2.friction` field lets it say so directly, and
`capability.py::route` reads that explicit signal before ever consulting
the model's own hint.

Payment friction (`PAYMENT_DECLINED`) is NOT handled here - it already has
a real, tested capability (`payment_recovery.py`, `Capability.
PAYMENT_RECOVERY`) that this module reuses rather than duplicates; see
`orchestrator.py`'s routing.

Read-only: calls only `adapter.list_departments()` (the same real,
merchant-declared category list the storefront's own nav and this
widget's greeting already show - never invented) and, for a dead search,
`retrieval.retrieve` on the merchant's stated goal/must-haves if any are
already on record. Never imports `engine.decision`, `engine.risk`, or any
`engine.execution` mutation path.
"""

from __future__ import annotations

import logging

from .evidence import TurnEvidence
from .state import ShoppingStateV2

logger = logging.getLogger(__name__)

DEAD_SEARCH = "DEAD_SEARCH"
PROMOTION_FAILED = "PROMOTION_FAILED"


async def run_friction_recovery_turn(
    *,
    friction: str,
    adapter,
    state: ShoppingStateV2,
) -> tuple[str, TurnEvidence]:
    """Returns (reply, evidence). `evidence` is always empty here - this
    capability answers about what went wrong, not about a product - but it
    keeps the same return shape every other capability uses so
    `orchestrator.py` doesn't need a special case."""
    if friction == DEAD_SEARCH:
        return await dead_search_reply(adapter), TurnEvidence(query=None, products=[])
    if friction == PROMOTION_FAILED:
        return _promotion_failed_reply(state), TurnEvidence(query=None, products=[])
    return (
        "Something didn't go as expected there - let me know what you're "
        "looking for and I'll help from here.",
        TurnEvidence(query=None, products=[]),
    )


async def dead_search_reply(adapter) -> str:
    """A dead search is not a dead end - the merchant's own real department
    list (never invented) gives the shopper something concrete to try
    instead of a bare apology."""
    try:
        departments = await adapter.list_departments()
    except Exception:  # noqa: BLE001 - degrade honestly, never raise to the shopper
        logger.warning("shopper_v2 friction_recovery: list_departments failed", exc_info=True)
        departments = []

    if not departments:
        return (
            "I couldn't find anything matching that search - try a different "
            "word for it, or tell me more about what you're after."
        )

    shown = departments[:5]
    if len(shown) == 1:
        listed = shown[0]
    else:
        listed = ", ".join(shown[:-1]) + f", or {shown[-1]}"
    return (
        "I couldn't find anything matching that search. This store carries "
        f"{listed} - would one of those be a better starting point, or tell "
        "me more specifically what you're after."
    )


def _promotion_failed_reply(state: ShoppingStateV2) -> str:
    """Honest by construction: this module is never told WHY a code failed
    (the real reason - expired, not found, ineligible - is already surfaced
    to the shopper directly by the storefront's own error handling,
    `storefront/src/api.ts::shopperMessage`), so it never guesses a cause
    it doesn't have. It offers the one thing it can honestly offer:
    continuing without the code, or naming a real budget so discovery can
    help find something that fits without one."""
    if state.budget_amount:
        return (
            "That code didn't apply - you can still check out without it, "
            "or I can look for something that fits your budget without "
            "needing a code at all."
        )
    return (
        "That code didn't apply. You can still check out without it, or "
        "tell me your budget and I'll look for something that fits it "
        "directly."
    )
