"""The active shopping mission for one conversation.

Read before reasoning, updated after it - see engine/api/chat.py's
`_process_turn`. This is the "current shopping state" layer of the Context
Builder: goal, budget, constraints, must-haves, nice-to-haves, exclusions,
trade-off priorities, the products currently under consideration, and a
coarse mission status.

Nothing here is invented. Every field this module writes came from the
model's own structured output for the turn that just ran - never inferred
from silence, and never guessed at by this module itself.
"""

from __future__ import annotations

from typing import Any

from .models import ShoppingState
from .session import session_scope


def _dedup_append(existing: list, add: list) -> list:
    """Append new items, case-insensitively de-duplicated against what is
    already there. Order preserved - most recently added last, so a reply
    that lists priorities back to a shopper reads in the order they said
    them."""
    out = list(existing)
    seen = {str(x).strip().lower() for x in out}
    for item in add:
        text = str(item).strip()
        if not text or text.lower() in seen:
            continue
        out.append(text)
        seen.add(text.lower())
    return out


def _remove(existing: list, remove: list) -> list:
    drop = {str(x).strip().lower() for x in remove if str(x).strip()}
    if not drop:
        return existing
    return [x for x in existing if str(x).strip().lower() not in drop]


async def get(session_id: str, connection_id: str) -> dict[str, Any] | None:
    """The mission as it stands, or None if nothing has been captured yet."""
    async with session_scope() as db:
        row = await db.get(ShoppingState, (session_id, connection_id))
        if row is None:
            return None
        return {
            "goal": row.goal,
            "budget_amount": row.budget_amount,
            "budget_currency": row.budget_currency,
            "constraints": list(row.constraints or []),
            "must_have": list(row.must_have or []),
            "nice_to_have": list(row.nice_to_have or []),
            "exclusions": list(row.exclusions or []),
            "priorities": list(row.priorities or []),
            "active_products": list(row.active_products or []),
            "mission_status": row.mission_status,
        }


async def merge(
    session_id: str,
    connection_id: str,
    *,
    goal: str | None = None,
    budget_amount: float | None = None,
    budget_currency: str | None = None,
    constraints_add: list[str] | None = None,
    constraints_remove: list[str] | None = None,
    must_have_add: list[str] | None = None,
    must_have_remove: list[str] | None = None,
    nice_to_have_add: list[str] | None = None,
    nice_to_have_remove: list[str] | None = None,
    exclusions_add: list[str] | None = None,
    exclusions_remove: list[str] | None = None,
    priorities_add: list[str] | None = None,
    active_products: list[str] | None = None,
    mission_status: str | None = None,
) -> None:
    """Fold one turn's structured output into the stored mission.

    A scalar argument left as None means "this turn said nothing new about
    that field" and leaves the stored value untouched - new explicit
    information supersedes old, but silence is not a statement. The list
    arguments are additive/subtractive against what is already stored,
    never a wholesale replacement (`active_products` is the one deliberate
    exception - see the model's own docstring for why).

    A no-op call (every argument left at its default) still creates the row
    on first use, so later reads always find *something* to build on rather
    than the caller having to branch on "does a mission exist yet".
    """
    async with session_scope() as db:
        row = await db.get(ShoppingState, (session_id, connection_id))
        if row is None:
            row = ShoppingState(session_id=session_id, connection_id=connection_id)
            db.add(row)

        if goal is not None:
            row.goal = goal
        if budget_amount is not None:
            row.budget_amount = str(budget_amount)
        if budget_currency is not None:
            row.budget_currency = budget_currency

        row.constraints = _remove(
            _dedup_append(row.constraints or [], constraints_add or []),
            constraints_remove or [],
        )
        row.must_have = _remove(
            _dedup_append(row.must_have or [], must_have_add or []),
            must_have_remove or [],
        )
        row.nice_to_have = _remove(
            _dedup_append(row.nice_to_have or [], nice_to_have_add or []),
            nice_to_have_remove or [],
        )
        row.exclusions = _remove(
            _dedup_append(row.exclusions or [], exclusions_add or []),
            exclusions_remove or [],
        )
        if priorities_add:
            row.priorities = _dedup_append(row.priorities or [], priorities_add)
        if active_products is not None:
            row.active_products = list(active_products)
        if mission_status is not None:
            row.mission_status = mission_status
