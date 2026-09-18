"""Typed ShoppingStateV2 and its transition semantics.

This is a typed VIEW over the existing `shopping_states` table
(`engine/db/models.py::ShoppingState`, mutated by `engine/db/shopping_state.py`)
- not a new source of truth, not a new table, not a schema change. See this
package's own spec discussion: Stage 1 deliberately reuses the existing
persistence mechanism and its existing per-(session_id, connection_id)
`asyncio.Lock` (in `engine.db.shopping_state`) rather than inventing a second
concurrency mechanism.

`version` is a domain-only, in-memory counter - incremented once per
successful merge within this process's lifetime for this state object. It is
NOT persisted as a database column and is NOT a durable optimistic-
concurrency token: two separate processes (or a restart) do not share it, and
it does not survive past the object holding it. That is a deliberate,
documented limitation, not an oversight - see the "concurrency" note below.
Real persistent versioning (an actual DB column, checked on write) is
explicitly out of scope for Stage 1 and is deferred to whichever later stage
first needs to run at production concurrency; this field exists now so the
`ShoppingStateV2` schema and its response shape don't have to change again
just to add it later.

Concurrency for Stage 1 is handled the same way V1 already handles it: the
existing `engine.db.shopping_state`-owned lock serializes reads and writes
per (session_id, connection_id), so two concurrent Stage 1 turns for the
same session cannot race each other - the same guarantee V1 has always had,
reused rather than reimplemented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from engine.db import shopping_state as shopping_state_store

if TYPE_CHECKING:
    from .understanding import StateDelta

MissionStatus = Literal["exploring", "comparing", "deciding", "done"]


class ShoppingStateV2(BaseModel):
    """A typed mirror of one `shopping_states` row, plus a domain-only
    `version` counter. Field names and semantics match
    `engine/db/models.py::ShoppingState` exactly - this is a view, not a
    reinterpretation.
    """

    session_id: str
    connection_id: str

    goal: str | None = None
    budget_amount: str | None = None
    budget_currency: str | None = None

    constraints: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    active_products: list[str] = Field(default_factory=list)
    mission_status: MissionStatus | None = None

    #: Domain-only, in-memory. See module docstring - NOT a DB column, NOT a
    #: durable optimistic-concurrency token.
    version: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def as_prompt_dict(self) -> dict:
        """The shape `engine/reasoning/context.py::build_context` and
        `engine/execution/mission_ranking.py::rank_and_filter` already
        expect - both take a plain `dict`, not a Pydantic model, and this
        keeps this module the only place that knows the translation."""
        return {
            "goal": self.goal,
            "budget_amount": self.budget_amount,
            "budget_currency": self.budget_currency,
            "constraints": list(self.constraints),
            "must_have": list(self.must_have),
            "nice_to_have": list(self.nice_to_have),
            "exclusions": list(self.exclusions),
            "priorities": list(self.priorities),
            "active_products": list(self.active_products),
            "mission_status": self.mission_status,
        }


async def load(session_id: str, connection_id: str) -> ShoppingStateV2:
    """Read the current mission, or an empty one at version 0 if nothing has
    been captured yet for this (session_id, connection_id) - mirrors
    `engine.db.shopping_state.get`'s own "None means nothing yet" contract,
    just never returning None itself (an empty typed state is a normal,
    representable value; a missing row is not a special case this layer
    needs its caller to branch on)."""
    raw = await shopping_state_store.get(session_id, connection_id)
    if raw is None:
        return ShoppingStateV2(session_id=session_id, connection_id=connection_id)
    return ShoppingStateV2(session_id=session_id, connection_id=connection_id, **raw)


async def apply_delta(current: ShoppingStateV2, delta: "StateDelta") -> ShoppingStateV2:
    """Fold one turn's `StateDelta` into storage via the EXISTING
    `engine.db.shopping_state.merge` - same lock, same read-modify-write
    discipline, same clear/supersede semantics V1 already relies on - then
    re-read and return the new typed state with `version` incremented by
    one. No new persistence path, no new lock, no DB column added."""
    clear_fields = []
    if delta.clear_goal:
        clear_fields.append("goal")
    if delta.clear_budget:
        clear_fields.append("budget_amount")
        clear_fields.append("budget_currency")

    budget_amount: float | None = None
    if delta.budget_amount is not None:
        try:
            budget_amount = float(delta.budget_amount)
        except (TypeError, ValueError):
            budget_amount = None

    await shopping_state_store.merge(
        current.session_id,
        current.connection_id,
        goal=delta.goal,
        budget_amount=budget_amount,
        budget_currency=delta.budget_currency,
        constraints_add=delta.constraints_add,
        constraints_remove=delta.constraints_remove,
        must_have_add=delta.must_have_add,
        must_have_remove=delta.must_have_remove,
        nice_to_have_add=delta.nice_to_have_add,
        nice_to_have_remove=delta.nice_to_have_remove,
        exclusions_add=delta.exclusions_add,
        exclusions_remove=delta.exclusions_remove,
        priorities_add=delta.priorities_add,
        mission_status=delta.mission_status,
        clear_fields=clear_fields or None,
    )
    new_state = await load(current.session_id, current.connection_id)
    new_state.version = current.version + 1
    return new_state
