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
    """A CONCRETE change that brings the real cart total under the
    shopper's stated (or, failing that, message-stated) budget - grounded
    entirely in `payload["lines"]`'s own real prices/quantities (see
    service.py's CART_INTELLIGENCE dispatch, which already fetched them
    for `analyze_cart`) - never a guess at what "cheaper" might mean, and
    never bounded to a specific line count (manual-test finding: the old
    top-1/top-2-only logic could genuinely never solve a cart needing 3+
    real removals, not because no solution existed but because this
    function never looked for one).

    Two real strategies, tried in order of how disruptive they are:

    1. Reduce a single line's own QUANTITY (never touching any other
       line) when that alone reaches the target - the least disruptive
       real change, since every distinct product stays in the cart.
    2. Remove whole lines - the MINIMUM number of them, by real price,
       largest-first. This is the standard optimal greedy for "cover at
       least this much excess using the fewest real items" (taking the
       largest available amounts first can never need more items than
       any other combination that also reaches the target), so this
       finds a genuinely minimal-disruption combination for a cart of any
       size, not a first-two-tried guess."""
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
    excess = total - target

    priced = [
        line for line in lines
        if line.get("line_total") is not None and Decimal(line["line_total"]) > 0
    ]
    if not priced:
        return None

    # Strategy 1: a single line's own quantity can absorb the excess
    # without removing any distinct product. Only offered when the line
    # has more than one unit (reducing a quantity-of-one line IS removing
    # it, already covered by strategy 2) and the real per-unit price
    # (line_total / quantity) actually divides the excess without
    # needing the WHOLE line removed.
    for line in sorted(priced, key=lambda l: Decimal(l["line_total"]), reverse=True):
        qty = line.get("quantity") or 1
        if qty <= 1:
            continue
        line_total = Decimal(line["line_total"])
        unit_price = line_total / qty
        if unit_price <= 0:
            continue
        units_to_drop = 0
        for _ in range(qty - 1):  # never drop every unit - that's strategy 2's job
            units_to_drop += 1
            new_total = total - unit_price * units_to_drop
            if new_total <= target:
                new_qty = qty - units_to_drop
                return (
                    f"Reducing {line['title']} from {qty} to {new_qty} would bring "
                    f"your total to {new_total.quantize(Decimal('0.01'))}, under your {target} budget."
                )

    # Strategy 2: minimal-count whole-line removal, largest real price
    # first - optimal for "fewest lines needed to cover this much excess."
    by_price_desc = sorted(priced, key=lambda l: Decimal(l["line_total"]), reverse=True)
    removed: list[dict] = []
    covered = Decimal(0)
    for line in by_price_desc:
        removed.append(line)
        covered += Decimal(line["line_total"])
        if covered >= excess:
            break

    if covered < excess:
        # Removing EVERY real line still isn't enough - genuinely
        # unsolvable by removal alone; say so honestly rather than
        # implying a combination exists.
        return (
            f"Your cart is {total}, {total - target} over your {target} budget - "
            f"removing everything in it would still only save {covered}, so this "
            f"budget isn't reachable by removing items alone."
        )

    new_total = total - covered
    if len(removed) == 1:
        line = removed[0]
        return (
            f"Removing {line['title']} ({line['line_total']}) would bring your "
            f"total to {new_total}, under your {target} budget."
        )
    named = ", ".join(f"{l['title']} ({l['line_total']})" for l in removed[:-1])
    named += f", and {removed[-1]['title']} ({removed[-1]['line_total']})"
    return (
        f"Removing {len(removed)} items - {named} - would bring your total to "
        f"{new_total}, under your {target} budget."
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
