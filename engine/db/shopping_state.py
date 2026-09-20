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

import asyncio
from collections import defaultdict
from typing import Any

from .models import ShoppingState
from .session import session_scope

#: Scalar fields a caller may explicitly null via `clear_fields` - see
#: `merge`'s own docstring for why this exists (hardening-pass fix: a
#: retracted budget/goal used to have no way to actually clear, ever, once
#: set - "budget doesn't matter now" left the old amount silently active
#: for the rest of the session).
_CLEARABLE_SCALARS = {"goal", "budget_amount", "budget_currency", "mission_status"}

#: One asyncio.Lock per (session_id, connection_id), created lazily -
#: hardening-pass fix for a real, reproduced data-integrity bug: concurrent
#: `merge()` calls to the same session raced (read-modify-write with no
#: concurrency control), and 10 concurrent additions to one session lost 8
#: of them. This project runs a single engine process against one SQLite
#: file (see CLAUDE.md's database-safety section), so a plain in-process
#: lock is a correct, sufficient fix for this architecture - not a stand-in
#: for a distributed lock a multi-process deployment would need instead.
#: The registry grows one entry per distinct session ever merged into and
#: is never evicted; acceptable at this project's scale (the same tradeoff
#: this codebase already accepts elsewhere, e.g. session_holdouts), and
#: cheap to add an eviction sweep later if it ever becomes real memory
#: pressure - not attempted here to avoid solving a problem that does not
#: exist yet.
_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


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
            "mission_items": list(row.mission_items or []),
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
    mission_items: list[dict] | None = None,
    clear_fields: list[str] | None = None,
) -> None:
    """Fold one turn's structured output into the stored mission.

    A scalar argument left as None means "this turn said nothing new about
    that field" and leaves the stored value untouched - new explicit
    information supersedes old, but silence is not a statement. The list
    arguments are additive/subtractive against what is already stored,
    never a wholesale replacement (`active_products` is the one deliberate
    exception - see the model's own docstring for why).

    `clear_fields` is the explicit-retraction mechanism a scalar field
    otherwise has no way to reach - hardening-pass fix. "Budget doesn't
    matter anymore" must actually null `budget_amount`, not leave the old
    figure silently active; there is deliberately no magic sentinel (a 0
    budget is a real, different statement) - the field genuinely becomes
    `None` again, the same as before it was ever set. Applied *before* this
    same call's own explicit new values, so a turn that both retracts and
    immediately restates a field in one message (unusual, but possible) ends
    with the newly-stated value, not cleared - the newest explicit
    information always wins, whichever argument carries it.

    A no-op call (every argument left at its default) still creates the row
    on first use, so later reads always find *something* to build on rather
    than the caller having to branch on "does a mission exist yet".

    Concurrency: the whole read-modify-write below is serialized per
    (session_id, connection_id) by an in-process lock - see the module-level
    `_locks` registry's own comment for why this is the right fix for this
    project's single-process architecture. Different sessions never
    contend with each other; the same session's own concurrent turns
    (two tabs, a fast retry) now queue rather than silently clobbering one
    another's additions.
    """
    async with _locks[(session_id, connection_id)]:
        async with session_scope() as db:
            row = await db.get(ShoppingState, (session_id, connection_id))
            if row is None:
                row = ShoppingState(session_id=session_id, connection_id=connection_id)
                db.add(row)

            cleared_now = {f for f in (clear_fields or []) if f in _CLEARABLE_SCALARS}
            # A field the model happened to re-supply this same turn with
            # the value ALREADY stored is far more likely to be the model
            # echoing stale context back (this codebase's own Context
            # Builder feeds the current mission back to the model every
            # turn) than a genuine new statement - a real hardening-pass
            # finding, live-reproduced: "budget doesn't matter anymore"
            # correctly triggered a clear, and the model's own extraction
            # simultaneously re-emitted the *same* old budget_amount in its
            # structured output for the same turn, which silently undid the
            # clear before this fix. Only a value that actually DIFFERS
            # from what was stored is trusted as a genuine restatement
            # strong enough to override a same-turn clear.
            before = {
                "goal": row.goal,
                "budget_amount": row.budget_amount,
                "budget_currency": row.budget_currency,
                "mission_status": row.mission_status,
            }
            for field in cleared_now:
                setattr(row, field, None)

            if goal is not None and (goal != before["goal"] or "goal" not in cleared_now):
                row.goal = goal
            if budget_amount is not None and (
                str(budget_amount) != before["budget_amount"]
                or "budget_amount" not in cleared_now
            ):
                row.budget_amount = str(budget_amount)
            if budget_currency is not None and (
                budget_currency != before["budget_currency"]
                or "budget_currency" not in cleared_now
            ):
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
            if mission_items is not None:
                row.mission_items = list(mission_items)
            if mission_status is not None and (
                mission_status != before["mission_status"]
                or "mission_status" not in cleared_now
            ):
                row.mission_status = mission_status
