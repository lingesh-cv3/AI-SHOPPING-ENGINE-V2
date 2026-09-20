"""Shopper V2's own, additive database table.

Phase 1 needs one durable fact V1's `shopping_states` table has no column
for: the last query that actually produced a real result, so a later
constraint-only turn ("under 2000") can fall back to it instead of
searching the literal budget sentence and finding nothing (Stage 2A
Finding 4 - see PROGRESS.md's Shopper V2 section, part E).

`engine/db/session.py::create_schema` uses `Base.metadata.create_all`,
which creates MISSING TABLES ONLY and cannot alter an existing one (see
that module's own comment). Adding a column to the shared `shopping_states`
table would need a real migration this project has no tooling for and no
stated plan to add casually (CLAUDE.md's database-safety rules). A new,
V2-owned table sidesteps that entirely: `create_all` creates it
automatically on next startup, no ALTER TABLE, no risk to `shopping_states`
or any other existing table or row.

Imports `Base` from `engine.db.models` (the shared declarative base, so
`create_all` picks this table up) but does not add anything to that V1-
adjacent file itself - the table's definition lives here, under V2's own
package, the same "V2 owns its own additions" pattern `state.py` already
uses for `ShoppingStateV2` (a typed view, not a new table) extended one
step further for the one fact that genuinely needs a new column.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.models import Base, _now


class SearchContextV2(Base):
    """The last shopper-turn query that actually returned a verified
    relevant product, per (session_id, connection_id) - not every query,
    only ones that worked. A budget-only follow-up turn with no product
    noun of its own falls back to this rather than searching its own
    literal text and finding nothing. See `search_context.py` for the
    read/write functions and `pipeline.py` for where this is used."""

    __tablename__ = "shopper_v2_search_context"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: The query string, never a product id or anything execution-capable -
    #: this table cannot become a way to smuggle an action through, only a
    #: search string a shopper's own earlier turn already produced.
    last_effective_query: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
