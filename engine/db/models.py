"""Persistence models.

Three tables: a case for every friction event, an approval for every case a human
must decide, and an outcome for every case that finished.

SQLite now, Postgres later. The schema, the types and the queries are all
Postgres-compatible; only the connection string differs. That was chosen over
requiring a database install on every developer machine, and the cost is one
environment variable when we deploy. Nothing here uses a SQLite-only feature.

connection_id on every row is the tenant boundary. Every query filters on it. A
merchant must never be able to see another merchant's cases, and the safest way to
guarantee that is for the column to exist at the lowest level rather than being
enforced by remembering to join correctly.

Money is stored as a string, never a float. The same reason Money uses Decimal: a
recovery system that loses fractions of a rupee is not one to run. The currency is
stored beside it, because an amount without a currency is not money.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    """Always timezone-aware UTC.

    Naive timestamps are how a system ends up reporting that an approval was
    decided before it was requested, once a server moves region.
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Case(Base):
    """One friction event and everything the engine decided about it.

    Written once when the engine runs, then updated as the case progresses. The
    pipeline detail is stored as JSON rather than in separate tables because it is
    an audit record: always read whole, never queried by individual field, and its
    shape should be free to evolve without a migration.
    """

    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)

    friction_type: Mapped[str | None] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(24), index=True)

    # What the shopper was doing. Kept so a human picking the case up can see the
    # situation without reconstructing it.
    query: Mapped[str | None] = mapped_column(String(300))
    cart_id: Mapped[str | None] = mapped_column(String(64))
    order_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Reasoning. used_model matters: a diagnosis from rules deserves less weight
    # than one from the model, and an operator should not have to guess which.
    used_model: Mapped[bool] = mapped_column(Boolean, default=False)
    model_name: Mapped[str | None] = mapped_column(String(80))
    diagnosis: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    fallback_reason: Mapped[str | None] = mapped_column(String(200))

    # Both replies. They differ when the engine overrode its own model, and
    # keeping only the displayed one would hide that the override happened.
    model_reply: Mapped[str | None] = mapped_column(Text)
    shopper_reply: Mapped[str | None] = mapped_column(Text)

    # Decision
    proposed: Mapped[list] = mapped_column(JSON, default=list)
    rejected: Mapped[list] = mapped_column(JSON, default=list)
    selected_action: Mapped[str | None] = mapped_column(String(40))
    selection_reason: Mapped[str | None] = mapped_column(Text)

    # Risk
    risk_outcome: Mapped[str | None] = mapped_column(String(10), index=True)
    risk_rule: Mapped[str | None] = mapped_column(String(48))
    risk_reason: Mapped[str | None] = mapped_column(Text)
    financial: Mapped[bool] = mapped_column(Boolean, default=False)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    approval: Mapped[Approval | None] = relationship(
        back_populates="case", uselist=False, cascade="all, delete-orphan"
    )
    outcome: Mapped[Outcome | None] = relationship(
        back_populates="case", uselist=False, cascade="all, delete-orphan"
    )

    # The queue view: pending work for one merchant, oldest first. Worth an index
    # because it is the query the operations console runs constantly.
    __table_args__ = (
        Index("ix_cases_conn_state", "connection_id", "state"),
        Index("ix_cases_conn_created", "connection_id", "created_at"),
    )


class Approval(Base):
    """A case waiting on a person, and what they decided.

    Separate from the case because its lifecycle is different: a case is written by
    the engine and read by everyone, while an approval is claimed, decided, and
    audited. Keeping them apart also means the queue can be queried without
    dragging the full case payload along.
    """

    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.case_id", ondelete="CASCADE"), unique=True, index=True
    )
    # Duplicated from the case deliberately. The queue filters on it, and joining
    # to cases on every queue read to establish tenancy is both slower and easier
    # to get wrong.
    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    state: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    action_type: Mapped[str] = mapped_column(String(40))
    risk_rule: Mapped[str | None] = mapped_column(String(48))

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    # Set at creation from the merchant's configured timeout. A shopper staring at
    # a declined card will not wait long, so an approval that nobody actions has to
    # expire rather than sit forever pretending to be live work.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)

    case: Mapped[Case] = relationship(back_populates="approval")

    __table_args__ = (Index("ix_approvals_conn_state", "connection_id", "state"),)


class Outcome(Base):
    """How a case ended.

    Feeds merchant reporting and, eventually, the generalized insights CRO and GEO
    would read. required_human is recorded because the difference between an
    auto-resolved case and a human-resolved one is the whole basis of the scaling
    argument.
    """

    __tablename__ = "outcomes"

    outcome_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("cases.case_id", ondelete="CASCADE"), unique=True, index=True
    )
    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    final_state: Mapped[str] = mapped_column(String(24))
    friction_type: Mapped[str | None] = mapped_column(String(40), index=True)

    # Decimal as a string, with its currency beside it. Never a float.
    revenue_recovered_amount: Mapped[str | None] = mapped_column(String(24))
    revenue_recovered_currency: Mapped[str | None] = mapped_column(String(3))

    time_to_resolution_ms: Mapped[int | None] = mapped_column(Integer)
    required_human: Mapped[bool] = mapped_column(Boolean, default=False)

    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    case: Mapped[Case] = relationship(back_populates="outcome")



class SessionTurn(Base):
    """One exchange in a shopper's conversation.

    Stored rather than held in memory so a conversation survives a restart and so
    the operations console can read what was actually said. Redis would be the
    production choice for latency, but the shape is the same and the interface in
    engine/session hides which is in use.

    The important relationship is session_id, which is also on Case. That is what
    makes memory shared: a shopper whose payment declined has a case against their
    session, and when they open the chat, the same session id pulls both the
    conversation and the friction into one context. Two surfaces, one memory.
    """

    __tablename__ = "session_turns"

    turn_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    #: "shopper" or "assistant". Kept as a string rather than a boolean because a
    #: third speaker (a CV3 operator joining the conversation) is a plausible
    #: addition and a boolean would have to be migrated.
    speaker: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)

    #: The case this turn produced, when it produced one. Lets the console jump from
    #: a message to the decision it caused.
    case_id: Mapped[str | None] = mapped_column(String(40), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_turns_session_created", "session_id", "created_at"),)



class ExecutionAttempt(Base):
    """One attempt to execute an action, keyed by its idempotency key.

    Exists so a retry can be recognised as a retry. Written before the platform is
    called, not after: writing after leaves a window where a crash loses the record
    and the retry charges again. Writing first leaves an IN_FLIGHT row instead, which
    is a situation a person can resolve.

    Only money-touching actions get a row. A repeated search costs nothing and
    recording it would be noise.
    """

    __tablename__ = "execution_attempts"

    idempotency_key: Mapped[str] = mapped_column(String(48), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    case_id: Mapped[str] = mapped_column(String(40), index=True)
    action_type: Mapped[str] = mapped_column(String(40))

    #: IN_FLIGHT until the platform answers. A row stuck IN_FLIGHT means the process
    #: died mid-call and nobody knows whether the platform acted.
    state: Mapped[str] = mapped_column(String(12), default="IN_FLIGHT", index=True)
    succeeded: Mapped[bool | None] = mapped_column(Boolean)
    summary: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MerchantPolicy(Base):
    """A connection's risk settings, persisted.

    These lived only in memory until now, which meant every engine restart silently
    reset every merchant to Cautious. Cases and approvals survived; the settings
    governing them did not, so a merchant who had turned automation on found
    everything queuing again after a deploy, with no record of why.

    Stored as JSON rather than as rows per action, because the whole policy is
    always read and written together and its shape should be free to change without
    a migration.
    """

    __tablename__ = "merchant_policies"

    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(12), default="CAUTIOUS")
    auto_allowed: Mapped[list] = mapped_column(JSON, default=list)
    blocked: Mapped[list] = mapped_column(JSON, default=list)
    approval_timeout_minutes: Mapped[int] = mapped_column(Integer, default=15)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

class ApiKey(Base):
    """An API key, stored as a hash.

    Three kinds, because three different callers need three different things:

    **publishable** goes in a browser, so it is not a secret and never was. Anyone
    who views source can read it. Its protection is that it is scoped to one
    merchant and can only do shopper things - search, cart, chat. Treating it as
    confidential would be a lie we tell ourselves.

    **secret** is server to server. It can change policy and decide approvals, so it
    must never reach a browser.

    **operator** is CV3's own, and it spans merchants. Every other key is bound to
    one connection; this is the exception that makes the operations console possible,
    and it is the one worth being careful with.

    The key itself is never stored. On creation it is shown once and then only the
    hash remains, so a database dump does not hand somebody working credentials.
    """

    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: The first twelve characters, indexed, so a key can be found without scanning
    #: every row and hashing each one. The prefix alone is not usable.
    prefix: Mapped[str] = mapped_column(String(16), index=True)

    #: SHA-256 of the whole key.
    #:
    #: Not bcrypt or argon2, and that is deliberate rather than an oversight. Those
    #: exist to make guessing a human-chosen password expensive. A key here is 32
    #: bytes from os.urandom, so there is no dictionary to attack and nothing to
    #: slow down - a plain fast hash is the right tool, and it keeps verification
    #: cheap on a path every request takes.
    key_hash: Mapped[str] = mapped_column(String(64))

    #: publishable, secret, or operator
    kind: Mapped[str] = mapped_column(String(12))

    #: The merchant this key speaks for. Null only for operator keys.
    connection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Domains a publishable key may be used from, as a JSON list. Empty means any,
    #: which is fine in development and should not be in production.
    allowed_origins: Mapped[list] = mapped_column(JSON, default=list)

    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Revoked rather than deleted, so an audit trail keeps pointing at something.
    #: 75% of merchants say being able to revoke access in real time is critical,
    #: and a deleted row cannot explain why a request was refused.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
