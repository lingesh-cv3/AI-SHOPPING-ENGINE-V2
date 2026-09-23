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
    UniqueConstraint,
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

    #: True when this friction was left to resolve itself - the shopper's
    #: session was in the merchant's holdout slice, so no reasoning ran, no
    #: action was proposed, and no recovery was attempted. Exists so the
    #: merchant report can compare the holdout's own resolution rate against
    #: the assisted group's - the only way to show a merchant that a sale
    #: was actually caused by the engine rather than one that would have
    #: happened anyway.
    is_holdout: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

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


    #: When somebody at the shop dealt with a handover, and who.
    #:
    #: Null means still waiting. Escalations used to have nowhere to record this,
    #: which is why twelve of them sat unseen: there was no difference between a
    #: handover nobody had touched and one that was finished.
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    handled_by: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: What the operator did about it, in their words.
    #:
    #: Sent to the shopper and kept in the record. The engine cannot know whether
    #: somebody phoned them or sent a payment link, so it asks rather than
    #: inventing a message - and a handover closed with no explanation leaves the
    #: shopper exactly where they were when we promised them help.
    handled_note: Mapped[str | None] = mapped_column(String(400), nullable=True)


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

    #: Options offered with this turn, serialized as JSON - size or variant choices
    #: a shopper must tap before an action can run. Without this a reload restored
    #: turns as text only, so a question offered before the refresh had nothing left
    #: to tap after it: the buttons were never wrong, they were never saved.
    choices_json: Mapped[str | None] = mapped_column(Text)

    #: The same gap `choices_json` closed, for the other three things a turn can
    #: carry that a shopper taps: recommended/searched products, a two-item
    #: comparison, and the card picker offered at checkout. Without these a
    #: reload mid-browse, mid-compare or mid-pay restored the sentence but not
    #: what it was talking about - the offer was still live, only the way to
    #: act on it was gone, the exact shape `choices_json` exists to prevent.
    products_json: Mapped[str | None] = mapped_column(Text)
    comparison_json: Mapped[str | None] = mapped_column(Text)
    payment_json: Mapped[str | None] = mapped_column(Text)
    category_choices_json: Mapped[str | None] = mapped_column(Text)

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

    #: What fraction of new sessions get no assistance at all, so their outcome
    #: can be compared against the assisted group's. Zero unless a merchant
    #: deliberately turns it on - a merchant who never asked for an experiment
    #: should never have shoppers silently left unhelped by one.
    holdout_percent: Mapped[int] = mapped_column(Integer, default=0)

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


class Shopper(Base):
    """Somebody who signs in, so their conversation survives closing the tab.

    Scoped to one merchant. An account at Kettle is not an account at Northfield,
    because these are separate clients who happen to share an engine - and letting
    one username span both would mean Northfield learning that its customer also
    buys coffee. That is not ours to tell them.

    So the constraint is on (connection_id, username) rather than username alone,
    and the same person signing up at both shops is two rows that never meet.
    """

    __tablename__ = "shoppers"
    __table_args__ = (
        UniqueConstraint("connection_id", "username", name="uq_shopper_per_merchant"),
    )

    shopper_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(80), index=True)

    #: A bcrypt hash, which carries its own salt and cost factor.
    #:
    #: Not SHA-256, unlike the API keys. Those are 32 random bytes with no
    #: dictionary to attack, so a fast hash is correct there. A password is chosen
    #: by a person, so it needs a slow one - the whole point is making guessing
    #: expensive for somebody holding the database.
    password_hash: Mapped[str] = mapped_column(String(200))

    #: What to call them. Optional, because insisting on a real name in order to
    #: remember a conversation is a strange thing to require.
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)

    #: Where the order confirmation goes. Nullable because accounts created before
    #: this field existed have none, and the storefront prompts for one at the
    #: checkout gate rather than forcing a migration everywhere. Checkout is refused
    #: until it is present - the confirmation has to reach somebody.
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )







class ShopperSession(Base):
    """A signed-in shopper's session, held server-side.

    Server-side rather than a JWT, and that is the decision worth defending. A JWT
    cannot be revoked before it expires, so "sign me out" would be a lie until the
    token aged out - and on a shared computer that is the one promise that has to be
    true. A row can be deleted. There is already a database, and one lookup per
    request costs nothing measurable.

    The token is stored as a SHA-256 hash, not bcrypt. It is 32 bytes of urandom
    with no dictionary to attack, so the reasoning is the same as for the API keys
    and the opposite of the one for passwords.
    """

    __tablename__ = "shopper_sessions"

    session_token_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: The first twelve characters, indexed, so a token is found without hashing
    #: every row. The prefix alone is not usable.
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))

    shopper_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Revoked rather than deleted, so "when did this session end, and was it
    #: signed out or did it expire" stays answerable.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SignInAttempt(Base):
    """A failed sign-in, so guessing can be slowed down.

    In the database rather than in memory, because in-memory counters reset on
    every restart and do not exist for a second worker - which makes them
    theatre. A row per failure is a write nobody will notice at this scale.

    Only failures are recorded. A successful sign-in is not evidence of anything
    worth rate limiting, and keeping them would be building a login log nobody
    asked for.
    """

    __tablename__ = "sign_in_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    #: Indexed with the time, because the only question asked of this table is
    #: "how many times has this name failed recently".
    username: Mapped[str] = mapped_column(String(80), index=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )




class ShopperCart(Base):
    """Which cart belongs to which shopper, per merchant.

    The conversation already follows a signed-in shopper and the basket did not,
    which is the kind of inconsistency somebody notices at once: their chat is where
    they left it and their cart is empty.

    One cart per shopper per merchant. A shopper with an account at both shops has
    two baskets, because they are two shops - and merging them would mean Northfield
    learning what somebody bought from Kettle.
    """

    __tablename__ = "shopper_carts"
    __table_args__ = (
        UniqueConstraint("shopper_id", "connection_id", name="uq_cart_per_shop"),
    )

    row_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    shopper_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    #: The platform's own cart id. Ours only in the sense that we remember it.
    cart_id: Mapped[str] = mapped_column(String(120))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


class FunnelEvent(Base):
    """One real funnel-stage event, logged as it happens - not reconstructed
    after the fact from a table that was never meant to answer this.

    Exists to answer "how many carts were started this window" honestly,
    which nothing in this schema could answer before: `ShopperCart` is
    upserted once per shopper per merchant and reused forever, and a
    guest's cart is not tracked there at all (guests use the cookie-keyed
    `ResourceOwner` table instead). This logs the moment `create_cart`
    actually mints a new cart, for every visitor - guest or signed-in -
    so the count reflects real funnel entry regardless of account status.

    Written once per real cart-creation call, from `shop.py::create_cart`
    only. The storefront caches a cart id client-side and only calls that
    route when it does not already have one (the same mechanism that makes
    "cart persists across reload" already work), so a refresh does not
    produce a second event for the same visit - this table inherits that
    guarantee rather than re-implementing it.

    Only one event type exists today: `CART_CREATED`. A "checkout started"
    stage, distinct from an actual payment attempt, was deliberately not
    added - this engine has no separate "viewed checkout" step before a
    card is submitted (the `POST .../checkout` call *is* the payment
    attempt, already recorded as an `ExecutionAttempt` row), so a distinct
    "checkout started" event would be measuring something that does not
    happen here.

    Historical boundary: only carts created after this table shipped are
    counted. An older cart has no row here and cannot be reconstructed -
    stated in the funnel figures themselves, not hidden.
    """

    __tablename__ = "funnel_events"

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(24), index=True)
    cart_id: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class MerchantTask(Base):
    """A merchant work item, deterministically flagged from real catalogue/
    unmet-demand data - not a commerce `ActionType`, and never touches the
    risk gate, an adapter, or merchant inventory. It is a CV3-owned record
    a merchant can resolve or dismiss, closing the Merchant Copilot's
    "read-only twin" gap for problem types that have no real platform
    action to attach (unlike payments/recovery, see #39): there is no
    adapter operation this engine may call to fix "out of stock", so the
    action here is tracking the problem as a real, persisted decision
    rather than inventing a fake inventory-write capability.

    `kind` + `subject_key` name the underlying fact (e.g. `OUT_OF_STOCK` +
    a product_id, or `UNMET_DEMAND` + a normalized search query) - stable
    identity for the same real-world problem across repeated syncs. The
    unique constraint below is the idempotency mechanism: asking the
    Copilot the same question twice, or opening the Tasks panel twice,
    upserts one row rather than creating a new task each time.

    A `RESOLVED`/`DISMISSED` task is never silently reopened by a later
    sync, even if the same fact is still true (matching `decide_approval`'s
    own "decided once, never revisited unasked" behaviour) - an accepted,
    named limitation rather than change-detection machinery this session
    did not build.
    """

    __tablename__ = "merchant_tasks"

    task_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(160))
    label: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(12), default="OPEN", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (
        UniqueConstraint(
            "connection_id", "kind", "subject_key", name="uq_merchant_task_subject"
        ),
        Index("ix_merchant_tasks_conn_state", "connection_id", "state"),
    )


class ResourceOwner(Base):
    """Who a cart, a conversation or an order belongs to.

    The gap this closes: every route was locked by merchant and none by shopper. A
    publishable key is bound to one shop and ships in the browser, so every shopper
    at that shop holds the same one - which made "is this caller entitled to this
    merchant" the only question anybody asked. Cart ids run BSK00001, BSK00002, so
    reading somebody else's basket was counting, not guessing.

    The owner is not a shopper account. Most shoppers never make one, and a guest's
    basket needs protecting just as much as a member's - more, really, since the
    guest is the common case. So an owner is whichever browser made the thing,
    identified by a cookie it cannot read and did not choose, and a signed-in
    shopper additionally owns everything filed under their account.

    One row per resource, and the first claim wins. A resource nobody has claimed is
    claimed by whoever touches it first, which is what lets the carts already in
    this database keep working. That is trust-on-first-use and it is honestly
    weaker than claiming at creation: somebody counting through cart ids before
    their owners come back would claim them. It closes the hole for everything made
    from here on, and the durable answer is ids that cannot be counted - which is
    the platform's decision, not ours.
    """

    __tablename__ = "resource_owners"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "kind", "resource_id", name="uq_one_owner_per_thing"
        ),
    )

    row_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    connection_id: Mapped[str] = mapped_column(String(64), index=True)

    #: "cart", "session" or "order". A string rather than an enum because the set
    #: is small, the values are written once, and a migration to add one should not
    #: be a schema change.
    kind: Mapped[str] = mapped_column(String(16), index=True)

    #: The platform's id, or the storefront's session id. Not ours either way - we
    #: only record who reached it first.
    resource_id: Mapped[str] = mapped_column(String(200), index=True)

    #: A visitor cookie's fingerprint, or a shopper_id. Both are opaque and neither
    #: is guessable, which is what makes them usable as an answer to "are you the
    #: same person as last time".
    owner_key: Mapped[str] = mapped_column(String(80), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


class SentMail(Base):
    """A record of every order confirmation the engine tried to send.

    Checkout requires an email precisely so a confirmation can reach somebody -
    collecting the address and never using it would make that gate empty
    ceremony. `delivered` is honest about whether a real send happened: no SMTP
    credentials means every row here is `delivered=False`, the same
    recorded-rather-than-pretended shape `NOTIFY_BACK_IN_STOCK` already uses when
    a capability isn't built. The row still exists either way, because "did we
    even try" has to be answerable without reading the log.
    """

    __tablename__ = "sent_mail"

    row_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    to_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)

    #: True only for a real SMTP send that succeeded. False either because no
    #: SMTP is configured, or because a configured send failed.
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )


class OrderLine(Base):
    """One product line of one completed (paid) order.

    Written once, at the same call sites that already write the payment
    ledger (`db.idempotency.payment_settled`) for a *succeeded* checkout -
    never for a decline or an abandoned cart, and never twice for a retried
    already-paid cart, because `payment_settled` itself is only ever called
    once per cart (guarded by the `begin_payment`/ledger claim). The source
    is the `Order.lines` the adapter's own checkout/recovery response already
    returns - never invented, never backfilled for orders that predate this
    table.

    `row_id` is derived from the settling idempotency key plus the cart line
    id, so even a best-effort double-call (see the recovery call site, which
    is intentionally best-effort/non-fatal) cannot double-insert the same
    line - a second write with the same primary key is a no-op merge, not a
    duplicate row.
    """

    __tablename__ = "order_lines"

    row_id: Mapped[str] = mapped_column(String(160), primary_key=True)

    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    cart_id: Mapped[str] = mapped_column(String(64), index=True)

    product_id: Mapped[str] = mapped_column(String(120), index=True)
    product_name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[str | None] = mapped_column(String(32))
    line_total: Mapped[str | None] = mapped_column(String(32))
    currency: Mapped[str | None] = mapped_column(String(8))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    __table_args__ = (
        Index("ix_order_lines_conn_product", "connection_id", "product_id"),
    )


class ShoppingState(Base):
    """The active shopping mission for one conversation - the "current shopping
    state" layer of the Context Builder (see engine/reasoning/context.py).

    One row per (session_id, connection_id), the same scope a conversation
    already has - a guest's mission lives as long as their session, a
    signed-in shopper's for as long as that conversation exists. Not scoped
    to the shopper account: a mission is "what this visit is trying to
    buy", not a durable fact about the person, which is what
    ShopperMemoryFact is for instead.

    Every scalar field (goal/budget/currency/mission_status) is overwritten
    only when the model's structured output supplies a new value that turn;
    an omitted field means "no new information this turn", not "clear it" -
    new explicit information supersedes old, stale information is left
    alone rather than erased by silence. The list fields
    (constraints/must_have/nice_to_have/exclusions) are maintained the same
    way at the call site (engine/db/shopping_state.py's `merge`): additions
    are appended and de-duplicated, and an explicit removal (the shopper
    correcting themselves) takes an item back out - the row is never
    replaced wholesale from one turn's partial view of the mission.
    """

    __tablename__ = "shopping_states"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget_amount: Mapped[str | None] = mapped_column(String(24), nullable=True)
    budget_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    constraints: Mapped[list] = mapped_column(JSON, default=list)
    must_have: Mapped[list] = mapped_column(JSON, default=list)
    nice_to_have: Mapped[list] = mapped_column(JSON, default=list)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)

    #: Free-text trade-off notes ("lighter over cushioning"), not numeric
    #: weights - capturing what the shopper said takes priority over a
    #: quantitative re-ranking model this pass does not build. Kept as an
    #: ordered list, most recent last, so re-stated priorities read in the
    #: order the shopper actually gave them.
    priorities: Mapped[list] = mapped_column(JSON, default=list)

    #: Product ids currently under consideration - what a comparison or a
    #: shortlist was actually built from, so a later turn ("compare those
    #: two again but cheaper first") has something concrete to refer back
    #: to. Replaced wholesale each time it changes, unlike the list fields
    #: above - "what am I looking at right now" has no meaningful partial
    #: update.
    active_products: Mapped[list] = mapped_column(JSON, default=list)

    #: One of "exploring", "comparing", "deciding", "done" - never invented
    #: by anything other than the model's own structured output, and left
    #: null until the model states one.
    mission_status: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: Shopper V2 only (see engine/shopper_v2/missions.py) - a typed list of
    #: independent per-item missions for a multi-item shopping trip ("I need
    #: running shoes, socks and a hydration bottle"), each with its own
    #: label/constraints/must_have/nice_to_have/exclusions/priorities/
    #: candidates/selected_product_id/status. Replaced wholesale each time
    #: it changes, same reasoning as `active_products` above - the whole
    #: list is the current state of the mission, not something meaningful
    #: to patch field-by-field at the row level (the per-item patching
    #: happens one level up, in missions.py, before this column is
    #: written). V1 never reads or writes this column.
    mission_items: Mapped[list] = mapped_column(JSON, default=list)

    #: Shopper V2 only - a bounded history of recent turns' verified
    #: product groups (most recent first), e.g. [["P1005"], ["P1001",
    #: "P1002", ...]] after "show me slides" then "show me shoes". Additive
    #: to `active_products` above, not a replacement for it:
    #: `active_products` keeps its existing "what am I looking at right
    #: now" single-focus meaning for compare-style latching; this field
    #: exists so a later plural reference ("add both") spanning a topic
    #: switch can still be answered honestly - by naming what was actually
    #: shown across recent turns - even though `active_products` itself
    #: was already overwritten by the newer turn. Never a new source of
    #: truth: every id here was already a real, verified `active_products`
    #: entry at the time its turn ran. V1 never reads or writes this
    #: column.
    recent_product_groups: Mapped[list] = mapped_column(JSON, default=list)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ShopperMemoryFact(Base):
    """One durable, structured fact about a signed-in shopper - the "important
    shopper memory" layer, deliberately separate from ShoppingState (the
    current mission) and from raw conversation history.

    Authenticated shoppers only. A guest has no durable identity to attach a
    fact to, and inventing one to remember them across visits is exactly
    the tracking-without-consent this project's own guest model already
    refuses elsewhere (see ShopperCart's docstring on the same point).

    `key` plus (connection_id, shopper_id) is the supersession identity: a
    later fact with the same key replaces the value, provenance, confidence
    and timestamp of the earlier one rather than accumulating a duplicate
    row - "new explicit information supersedes conflicting old information"
    applies to durable memory the same way it applies to the current
    shopping state, just at a longer time horizon. `key` is a short, stable
    label the model or a deterministic rule chose (e.g. "prefers_low_acidity",
    "owns:prod_123"), not free text - two different phrasings of the same
    preference must collide into one row, not two.

    A chat claim is never itself an authoritative purchase record - see
    `provenance`. Only a value derived from real commerce data (an actual
    order) may ever carry a commerce-grounded provenance; anything else is
    "the shopper said so", which is a real, useful, but explicitly
    lower-confidence kind of fact than an authoritative order line.
    """

    __tablename__ = "shopper_memory_facts"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "shopper_id", "key", name="uq_memory_fact_key"
        ),
        Index("ix_memory_facts_shopper", "connection_id", "shopper_id"),
    )

    fact_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    shopper_id: Mapped[str] = mapped_column(String(64), index=True)

    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)

    #: "shopper_stated" (the common case - something they said in chat) or
    #: "commerce_data" (derived from an actual order/adapter fact, never
    #: from conversation). Nothing here writes "commerce_data" today - that
    #: is deliberately reserved for a future pass that would derive a fact
    #: from OrderLine rows rather than chat - but the column exists so a
    #: shopper-stated preference is never silently indistinguishable from a
    #: verified purchase.
    provenance: Mapped[str] = mapped_column(String(24), default="shopper_stated")
    confidence: Mapped[float] = mapped_column(default=1.0)
    source_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Null means "no expiry known" - most preferences do not go stale on a
    #: schedule. Set only where a fact is inherently time-bound (never
    #: populated by this pass; the column exists so a future write can
    #: express it without a migration).
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ShopperEvent(Base):
    """One raw shopper-journey interaction, logged as it happens - the
    minimal instrumentation foundation Phase 2 asks for, deliberately
    nothing more.

    Kept strictly separate from semantic shopper memory
    (`ShopperMemoryFact`) and from the current mission (`ShoppingState`):
    an event is a fact about what happened, not a durable preference, and
    nothing in this codebase promotes an event into either of those tables
    automatically. A future personalized-ranking pass can read this table;
    this pass only writes it.

    `event_type` is one of a small fixed set (see
    `engine/execution/events.py`'s `EventType` for the authoritative list)
    - a string column rather than an enum for the same reason `SessionTurn`
    already gives (`speaker`): the set is small, append-only, and a new
    value should not need a migration.

    `shopper_id` is null for a guest - an event is still worth recording
    (aggregate, cross-session product-interest signal a merchant could one
    day read), it just cannot yet be tied to a durable identity. Written
    best-effort from `engine/api/chat.py`, never allowed to fail a turn.
    """

    __tablename__ = "shopper_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    shopper_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)

    event_type: Mapped[str] = mapped_column(String(32), index=True)
    product_id: Mapped[str | None] = mapped_column(String(120), index=True, nullable=True)

    #: Small, event-specific extra detail (e.g. a comparison's second
    #: product id, an add-to-cart's quantity) - never a place semantic
    #: memory or commerce state is derived from.
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    __table_args__ = (
        Index("ix_shopper_events_conn_type", "connection_id", "event_type"),
    )


class CartProposal(Base):
    """A structured, stored cart change the AI has suggested but not yet
    carried out - the "model suggestion -> structured proposal -> shopper
    approval -> risk check -> execution -> actual cart state" chain made
    concrete as a row, rather than as a sentence in a chat reply that
    happens to also mutate the cart.

    Created only by `PROPOSE_CART_CHANGE` execution (see
    `engine/execution/service.py`), which never itself touches the cart -
    creating this row IS the entire effect of that action type. A cart is
    only ever actually changed when a shopper explicitly accepts a specific
    proposal by id (`POST /api/chat/proposals/{id}/accept`), which converts
    it into the same trusted, Decision-Engine-and-Risk-Gate-checked
    `ProposedAction` shape a tapped button already uses
    (`engine/api/chat.py::act`) - a proposal's existence is never itself
    authority to mutate anything.

    `state` moves forward only: PENDING -> ACCEPTED|REJECTED|EXPIRED, and
    ACCEPTED -> EXECUTED|FAILED|NEEDS_CHOICE. Never back. NEEDS_CHOICE is its
    own terminal outcome, not a failure: the product is real and available,
    it simply has an option (size/grind/etc) this proposal never captured -
    `engine/api/chat.py::accept_proposal` returns the same `choices` shape a
    tapped size question already uses so the shopper finishes it with one
    more ordinary trusted tap, and retires the proposal itself so it cannot
    be accepted a second time once that tap lands. A proposal is revalidated
    against the live cart/catalog at accept time
    (`engine/api/chat.py::accept_proposal`) rather than trusted from
    creation time - the
    cart or the product's price/availability may have changed since, and a
    stale proposal executed blindly is exactly the "trusted input beats
    model guess, but only if the model's guess is still true" gap this
    table exists to close.
    """

    __tablename__ = "cart_proposals"

    proposal_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    shopper_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    cart_id: Mapped[str] = mapped_column(String(64), index=True)

    #: "ADD" | "REMOVE" | "REPLACE" | "CHANGE_QUANTITY". A plain string for
    #: the same append-only reasoning every other small-fixed-set column in
    #: this file already gives.
    change_type: Mapped[str] = mapped_column(String(24))

    product_id: Mapped[str] = mapped_column(String(120))
    variant_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: REPLACE only - the product this would remove in the same action.
    replaces_product_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    #: Why this was proposed, in the shopper's terms ("completes your
    #: espresso setup"). Advisory text, same as ProposedAction.rationale -
    #: never itself a fact the execution layer trusts.
    reason: Mapped[str] = mapped_column(Text)
    #: Concrete, grounded facts backing the proposal - e.g. "declared
    #: compatible with KB-EQP-10", "within your stated budget" - never a
    #: free-form model claim with nothing under it.
    evidence: Mapped[list] = mapped_column(JSON, default=list)

    #: A snapshot of the product's price at proposal time, so accept-time
    #: revalidation can detect a price change rather than silently acting on
    #: a now-stale figure.
    snapshot_price: Mapped[str | None] = mapped_column(String(24), nullable=True)

    state: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    #: Set only once execution (accept) actually ran - what really happened,
    #: never what the model claimed would happen.
    execution_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    #: Shopper V2 only (see engine/shopper_v2/basket_proposal.py) - the
    #: `MissionItem.item_id` this proposal fulfills, when it was created as
    #: part of a multi-item basket proposal. Opaque metadata: nothing in
    #: `engine/api/chat.py::accept_proposal`'s revalidate-then-execute path
    #: reads or depends on this, so an accept/reject/expiry behaves
    #: identically whether or not it is set. Null for every proposal V1
    #: creates, and for any V2 proposal not created as part of a basket.
    mission_item_id: Mapped[str | None] = mapped_column(String(24), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_cart_proposals_cart", "connection_id", "cart_id", "state"),
    )


class SessionHoldout(Base):
    """Whether one session was assigned to the no-assistance holdout group.

    Assigned once, at the first friction event of a session, and never
    reassigned - a shopper compared against the "no assistant" outcome for one
    problem and the "assisted" outcome for the next would not be a controlled
    comparison of anything. The row is the memory that makes the assignment
    sticky for the rest of that visit.
    """

    __tablename__ = "session_holdouts"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    is_holdout: Mapped[bool] = mapped_column(Boolean)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
