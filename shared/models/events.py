"""Normalized signals.

Everything that tells the engine something happened arrives here in one shape,
regardless of origin. Three sources exist at launch — the shopper widget,
merchant webhooks, and the engine's own observations. Webeyez, if and when it
is wired in, becomes a fourth ingester producing this same object. Nothing
above the intake knows or cares which source a signal came from.

That is the entire point: the seam is built now so the integration is one
module later, and the engine has no dependency on any external signal
provider in the meantime.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .commerce import DeclineReason


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignalSource(StrEnum):
    """Where a signal came from.

    Recorded for provenance and operator debugging — never for routing. The
    engine must behave identically whichever source reported a dead search.
    """

    WIDGET = "WIDGET"
    MERCHANT_WEBHOOK = "MERCHANT_WEBHOOK"
    ENGINE = "ENGINE"
    WEBEYEZ = "WEBEYEZ"


class FrictionType(StrEnum):
    """The normalized friction vocabulary.

    This is what diagnosis reasons over. Adding a type here means teaching the
    engine a new kind of problem; it does not mean changing any adapter.
    """

    DEAD_SEARCH = "DEAD_SEARCH"
    PRODUCT_UNAVAILABLE = "PRODUCT_UNAVAILABLE"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    PROMOTION_FAILED = "PROMOTION_FAILED"
    CHECKOUT_ERROR = "CHECKOUT_ERROR"
    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    CART_ABANDONED = "CART_ABANDONED"
    REPEATED_FAILURE = "REPEATED_FAILURE"
    OTHER = "OTHER"


class Signal(_Base):
    """One normalized event entering the engine.

    `raw` preserves the source's original payload for operators. Like
    CommerceError.platform_detail, it is the one field permitted to hold
    foreign shape, and it is never fed to the AI Engine — reasoning happens
    over the normalized fields only, so a change in a source's format cannot
    quietly change how the engine thinks.
    """

    signal_id: str
    source: SignalSource
    connection_id: str
    session_id: str | None = None
    friction_type: FrictionType
    occurred_at: datetime
    received_at: datetime

    # Optional context, populated where the source knows it
    product_id: str | None = None
    cart_id: str | None = None
    order_id: str | None = None
    search_query: str | None = None
    decline_reason: DeclineReason | None = None

    pre_diagnosed_cause: str | None = Field(
        default=None,
        description="Some sources supply their own diagnosis rather than a raw "
        "event. Treated as an input to our diagnosis, never as a replacement "
        "for it — the engine reaches its own conclusion and records both when "
        "they differ.",
    )
    raw: dict[str, Any] = Field(default_factory=dict)


class CaseState(StrEnum):
    """The recovery case state machine.

    DETECTED and DIAGNOSED are the AI Engine's work. ACTION_PROPOSED is the
    Decision Engine's. RISK_EVALUATED is the Risk Gate's, and forks into the
    AUTO branch straight to execution, the HUMAN branch held at
    PENDING_APPROVAL, or BLOCKED with no queue entry at all. All live branches
    converge on OUTCOME.
    """

    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    RISK_EVALUATED = "RISK_EVALUATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    EXECUTING = "EXECUTING"
    OUTCOME = "OUTCOME"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    ESCALATED = "ESCALATED"


#: States from which a case can move no further. Anything else is live work,
#: and the operations console surfaces it as such.
TERMINAL_STATES: frozenset[CaseState] = frozenset(
    {
        CaseState.OUTCOME,
        CaseState.REJECTED,
        CaseState.BLOCKED,
        CaseState.FAILED,
        CaseState.TIMEOUT,
        CaseState.UNSUPPORTED,
        CaseState.ESCALATED,
    }
)


class Diagnosis(_Base):
    """The AI Engine's conclusion about why friction occurred."""

    friction_type: FrictionType
    cause: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: list[str] = Field(
        default_factory=list, description="What the model based this on. For humans."
    )
    conflicts_with_source: bool = Field(
        default=False,
        description="True when a source supplied a pre-diagnosed cause and ours "
        "disagrees. Surfaced to operators rather than silently resolved.",
    )
    diagnosed_at: datetime


class Outcome(_Base):
    """The recorded result of a case. Feeds merchant reporting and learning."""

    case_id: str
    connection_id: str
    friction_type: FrictionType
    resolved: bool
    final_state: CaseState
    revenue_recovered_amount: str | None = Field(
        default=None, description="Decimal as string, with currency below."
    )
    revenue_recovered_currency: str | None = None
    time_to_resolution_ms: int | None = None
    required_human: bool = False
    recorded_at: datetime