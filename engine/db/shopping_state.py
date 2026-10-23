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
_CLEARABLE_SCALARS = {"goal", "budget_amount", "budget_currency", "mission_status", "pending_question"}

class _AsyncRLock:
    """A reentrant `asyncio.Lock` - the SAME task that already holds it may
    re-enter without deadlocking; a DIFFERENT task genuinely blocks until
    the holder's outermost `async with` exits. Plain `asyncio.Lock` is not
    reentrant, and that mattered once callers grew beyond a single
    `merge()` call: Focus Stack review (2026-09) found that a whole V2 turn
    reads `ShoppingStateV2` once at the start, mutates it across several
    nodes, and calls `merge()` several separate times before the turn
    ends - each individual `merge()` call was already safely locked, but
    the WIDER read-compute-write span across the whole turn was not, so
    two genuinely concurrent turns for the same session (a double-tap, a
    network retry, two open tabs) could each read the same starting
    `focus_stack`, each compute their own addition, and the second to
    write would silently discard the first's - live-reproduced directly:
    10 concurrent turn-shaped writes for one session lost 9 of them before
    this fix. Wrapping the WHOLE turn in this same lock (see
    `engine/shopper_v2/graph.py::run_turn_via_graph`) closes that, and
    reentrancy is what lets `merge()` keep locking exactly as before for
    every OTHER caller (V1, standalone scripts) without either dead-
    locking against its own turn-level lock or losing protection when
    called on its own."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self) -> "_AsyncRLock":
        task = asyncio.current_task()
        if self._owner is task and task is not None:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


#: One `_AsyncRLock` per (session_id, connection_id), created lazily -
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
_locks: dict[tuple[str, str], _AsyncRLock] = defaultdict(_AsyncRLock)


def lock_for(session_id: str, connection_id: str) -> _AsyncRLock:
    """The same per-(session_id, connection_id) lock `merge()` already
    uses internally, exposed so a caller that reads state, mutates it
    across several steps, and writes it back several times (a whole V2
    turn - see `engine/shopper_v2/graph.py::run_turn_via_graph`) can hold
    it for that ENTIRE span, not just one individual `merge()` call.
    Reentrant - safe to also call `merge()` normally while already holding
    this from an outer `async with`."""
    return _locks[(session_id, connection_id)]


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
            "recent_product_groups": list(row.recent_product_groups or []),
            "fulfillment_queue": list(row.fulfillment_queue or []),
            "focus_stack": list(row.focus_stack or []),
            "last_rendered": row.last_rendered,
            "pending_question": row.pending_question,
            "preference_domains": dict(row.preference_domains or {}),
            "cancelled_preferences": list(row.cancelled_preferences or []),
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
    priorities_remove: list[str] | None = None,
    active_products: list[str] | None = None,
    mission_status: str | None = None,
    mission_items: list[dict] | None = None,
    recent_product_groups: list[list[str]] | None = None,
    fulfillment_queue: list[dict] | None = None,
    focus_stack: list[dict] | None = None,
    last_rendered: dict | None = None,
    pending_question: dict | None = None,
    preference_domain_updates: dict[str, str] | None = None,
    cancelled_preferences_add: list[str] | None = None,
    cancelled_preferences_remove: list[str] | None = None,
    clear_fields: list[str] | None = None,
) -> None:
    """Fold one turn's structured output into the stored mission.

    A scalar argument left as None means "this turn said nothing new about
    that field" and leaves the stored value untouched - new explicit
    information supersedes old, but silence is not a statement. The list
    arguments are additive/subtractive against what is already stored,
    never a wholesale replacement, except `active_products`, `mission_items`,
    `recent_product_groups`, `fulfillment_queue`, `focus_stack`, and
    `last_rendered` - each represents "the current shape of a whole
    structure" rather than independently mergeable entries, and is always
    written as a complete replacement by its own caller (see each field's
    own docstring in `engine/db/models.py::ShoppingState` for why).

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
                "pending_question": row.pending_question,
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
            if priorities_add or priorities_remove:
                row.priorities = _remove(
                    _dedup_append(row.priorities or [], priorities_add or []),
                    priorities_remove or [],
                )
            if active_products is not None:
                row.active_products = list(active_products)
            if mission_items is not None:
                row.mission_items = list(mission_items)
            if recent_product_groups is not None:
                row.recent_product_groups = list(recent_product_groups)
            if fulfillment_queue is not None:
                row.fulfillment_queue = list(fulfillment_queue)
            if focus_stack is not None:
                row.focus_stack = list(focus_stack)
            if last_rendered is not None:
                row.last_rendered = dict(last_rendered)
            if pending_question is not None and (
                pending_question != before["pending_question"] or "pending_question" not in cleared_now
            ):
                row.pending_question = dict(pending_question)
            if mission_status is not None and (
                mission_status != before["mission_status"]
                or "mission_status" not in cleared_now
            ):
                row.mission_status = mission_status

            # Production architecture (2026-09), Phase 2: domain tags are
            # additive, keyed by the preference's own stated value - a
            # later re-statement of the SAME value under a different
            # domain overwrites its tag (the newest statement's own context
            # is the more relevant one), never accumulates stale entries
            # for values no longer in must_have/nice_to_have/priorities.
            if preference_domain_updates:
                domains = dict(row.preference_domains or {})
                domains.update(preference_domain_updates)
                row.preference_domains = domains
            # Cancellation tombstones: a value moves onto this list when
            # explicitly cancelled (never a bare deletion, which cannot by
            # itself stop the same phrase being re-derived from replayed
            # history - see `ShoppingStateV2.cancelled_preferences`'s own
            # docstring) and off it the moment the shopper explicitly
            # restates it (a genuine re-statement overrides a tombstone by
            # design, it does not permanently ban a phrase).
            if cancelled_preferences_add or cancelled_preferences_remove:
                tombstones = _remove(
                    _dedup_append(row.cancelled_preferences or [], cancelled_preferences_add or []),
                    cancelled_preferences_remove or [],
                )
                row.cancelled_preferences = tombstones
