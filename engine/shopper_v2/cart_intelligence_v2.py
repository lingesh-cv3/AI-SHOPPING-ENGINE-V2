"""Cart intelligence - via the EXISTING `CART_INTELLIGENCE` dispatch
(`engine/execution/service.py`, unmodified) - the same decision -> risk ->
record_case -> execute_case shape `cart_status.py`/`order_status.py`/
`checkout.py` already use for a read-only, non-financial, auto-approved
single-turn answer.

Closes a real, live-verified V1 -> V2 gap (Batch 4 completion audit):
`engine/execution/cart_intelligence.py::analyze_cart` (real findings -
duplicate lines, an over-budget total, a missing declared complement, an
unavailable line, a stated exclusion sitting in the cart) already has a
complete, real dispatch in `engine/execution/service.py` and is already
reachable through V1 (`engine/api/chat.py`). Until this module,
`capability.py` collapsed the model's own `CART_INTELLIGENCE` hint into
the SAME `Capability.CART_STATUS` bucket as a bare "what's in my cart"
question, so the analysis itself was never reached through V2 - an
unreachable claim (CLAUDE.md's own anti-pattern), not a safety issue,
since the underlying dispatch was always the authoritative,
already-hardened one.

Purely a read plus, when the dispatch's own logic finds a real missing
complement, ONE real cart-proposal row it creates the same way V1's
identical dispatch already does (`db.cart_proposals.create` inside
`execute_case`, unmodified here) - never an autonomous mutation. The
proposal is PENDING until the shopper's own explicit accept, exactly like
every other proposal in this codebase; this module cannot make that
proposal, only report the same fields `execute_case` already returned.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from engine.db import record_case
from engine.decision import DecisionEngine, operation_for
from engine.risk import RiskGate
from engine.execution import ExecutionService
from shared.models import ActionType, ProposedAction

logger = logging.getLogger(__name__)

_NO_CART_REPLY = (
    "I don't have a cart to check for this conversation yet - start "
    "shopping and I'll be able to tell you if anything's worth flagging."
)

#: Closed-class intent vocabulary, one small fixed set per finding `kind` -
#: same discipline `capability.py`'s own comparison/temporal-reference
#: word lists use. Live-reproduced gap this closes: "my cart is over
#: 10000, what can I change to bring it under budget" used to get every
#: finding `analyze_cart` had (missing-complement chatter mixed in with
#: the real budget problem) instead of an answer actually about budget -
#: CLAUDE.md's own "answer the message in front of you" principle applied
#: to a multi-finding capability, not a single-answer one.
_INTENT_WORDS: dict[str, tuple[str, ...]] = {
    "over_budget": (
        "budget", "over 10000", "under budget", "bring it under", "too expensive",
        "cheaper", "reduce", "lower the total", "spend less", "spending too much",
    ),
    "duplicate_line": ("duplicate", "twice", "two of the same", "same item"),
    "missing_complement": ("missing", "should i add", "goes with", "complement"),
    "unavailable": ("out of stock", "unavailable", "sold out"),
    "unconfirmed_requirement": ("does it actually", "confirm", "sure it's"),
}

_BUDGET_NUMBER = re.compile(r"(?:under|below|less than|bring it under)\s*\$?\s*([\d,]+)", re.IGNORECASE)


def _requested_kinds(message: str) -> set[str] | None:
    """Which finding kind(s) the shopper's own message actually asks
    about, or `None` when nothing in the closed-class lists matches (the
    caller then shows every finding, exactly as before - never narrower
    than the shopper's real question, only ever narrower than "everything
    regardless of what was asked")."""
    lowered = message.lower()
    matched = {
        kind for kind, words in _INTENT_WORDS.items() if any(w in lowered for w in words)
    }
    return matched or None


def _budget_reduction_suggestion(message: str, payload: dict) -> str | None:
    """A CONCRETE change - which real line(s) to drop - that brings the
    real cart total under the shopper's stated (or, failing that,
    message-stated) budget. Grounded entirely in `payload["lines"]`'s own
    real prices (see service.py's CART_INTELLIGENCE dispatch, which
    already fetched them for `analyze_cart`) - never a guess at what
    "cheaper" might mean."""
    lines = payload.get("lines") or []
    grand_total = payload.get("grand_total")
    if not lines or grand_total is None:
        return None
    match = _BUDGET_NUMBER.search(message)
    if not match:
        return None
    try:
        target = Decimal(match.group(1).replace(",", ""))
        total = Decimal(grand_total)
    except InvalidOperation:
        return None
    if total <= target:
        return None

    # Greedy: drop the single most expensive real line first; if that
    # alone is enough, that is the concrete suggestion. Never proposes
    # removing more than actually needed.
    priced = [
        (l, Decimal(l["line_total"])) for l in lines if l.get("line_total") is not None
    ]
    if not priced:
        return None
    priced.sort(key=lambda pair: pair[1], reverse=True)
    costliest_line, costliest_amount = priced[0]
    remaining = total - costliest_amount
    if remaining <= target:
        return (
            f"Removing {costliest_line['title']} ({costliest_line['line_total']}) "
            f"would bring your total to {remaining}, under your {target} budget."
        )
    # One line isn't enough - name the top 2 real candidates rather than
    # guessing a specific combination the shopper didn't ask to see
    # computed exhaustively.
    if len(priced) >= 2:
        second_line, second_amount = priced[1]
        combined_remaining = remaining - second_amount
        if combined_remaining <= target:
            return (
                f"Removing {costliest_line['title']} ({costliest_line['line_total']}) "
                f"and {second_line['title']} ({second_line['line_total']}) would bring "
                f"your total to {combined_remaining}, under your {target} budget."
            )
    return (
        f"Your cart is {total}, {total - target} over your {target} budget - "
        f"even removing your most expensive item, {costliest_line['title']} "
        f"({costliest_line['line_total']}), won't be enough on its own."
    )


async def run_cart_intelligence_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
) -> tuple[str, dict | None]:
    """Returns (reply, payload_or_None). Every finding named in the reply
    comes straight from `executed.payload["findings"]` - the real,
    unmodified `CART_INTELLIGENCE` dispatch's own output - never
    synthesized or embellished here."""
    if not cart_id:
        return (_NO_CART_REPLY, None)

    proposed = ProposedAction(
        action_type=ActionType.CART_INTELLIGENCE,
        operation=operation_for(ActionType.CART_INTELLIGENCE),
        parameters={"said": message[:80]},
        rationale="shopper asked about their cart's health/issues",
        confidence=0.8,
    )

    trace = await decision.decide([proposed], connection_id=connection_id)
    risk_decision = gate.classify(trace.selected)

    case_id = None
    try:
        case_id = await record_case(
            connection_id=connection_id,
            friction=None,
            query=message[:300],
            cart_id=cart_id,
            order_id=None,
            session_id=session_id,
            reasoning={
                "used_model": False, "model_name": None, "diagnosis": None,
                "evidence": [], "fallback_reason": None, "model_reply": None,
                "shopper_reply": None, "prompt_tokens": None, "completion_tokens": None,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(proposed.action_type),
                        "parameters": proposed.parameters,
                        "rationale": proposed.rationale,
                        "confidence": proposed.confidence,
                    }
                ],
                "rejected": [
                    {"action_type": str(r.action_type), "reason": r.reason, "detail": r.detail}
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(risk_decision.outcome),
                "rule": risk_decision.policy_rule,
                "reason": risk_decision.reason,
                "financial": risk_decision.properties.financial,
            },
        )
    except Exception:  # noqa: BLE001 - never let case-recording failure crash the turn
        logger.exception("shopper_v2 cart_intelligence: could not record case")
        return ("I couldn't check your cart right now - please try again.", None)

    if str(risk_decision.outcome) != "AUTO":
        return ("I can't check your cart right now - someone needs to approve it first.", None)
    if case_id is None:
        return ("I couldn't check your cart right now - please try again.", None)

    executed = await execution.execute_case(connection_id, case_id)
    if not executed.succeeded:
        return (executed.summary, None)

    payload = executed.payload or {}
    findings = payload.get("findings") or []

    # A concrete, computed budget-reduction answer wins outright when the
    # shopper's own message names a real target number - this is a more
    # specific, more useful answer than `analyze_cart`'s own generic
    # `over_budget` finding message, and per the shopper's actual question
    # (CLAUDE.md's "answer the message in front of you"), nothing else
    # this capability could also say is relevant to it.
    budget_reply = _budget_reduction_suggestion(message, payload)
    if budget_reply:
        return (budget_reply, payload)

    requested_kinds = _requested_kinds(message)
    shown_findings = (
        [f for f in findings if f["kind"] in requested_kinds] if requested_kinds else findings
    )
    # A genuine question about one kind of issue, on a cart that has ONLY
    # a different kind, is still owed a real, honest answer about what
    # was actually asked - never silently substituted with the unrelated
    # finding just because it exists.
    if requested_kinds and not shown_findings and findings:
        return (
            "I didn't find anything matching that in your cart right now - "
            "here's what I did notice: " + " ".join(f["message"] for f in findings),
            payload,
        )

    if shown_findings:
        reply = " ".join(f["message"] for f in shown_findings)
        created_proposal = payload.get("created_proposal")
        if created_proposal and any(f["kind"] == "missing_complement" for f in shown_findings):
            reply += " I've proposed adding the missing complement - confirm from your pending suggestions if you'd like it."
    else:
        reply = "Nothing to flag - your cart looks fine."

    return (reply, payload)
