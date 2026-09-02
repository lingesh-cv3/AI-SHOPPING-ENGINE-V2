"""API request and response shapes.

Deliberately separate from shared/models. Those are the engine's internal
contracts; these are what the frontend sees. Keeping them apart means an internal
refactor does not silently break the console, and it lets the API present
flattened, display-ready data without polluting the domain models.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from engine.risk import AutomationMode
from shared.models import ActionType, FrictionType


class ConnectionSummary(BaseModel):
    """One merchant connection, as the console lists it."""

    connection_id: str
    merchant_name: str
    platform: str
    mode: AutomationMode
    supported_count: int
    unsupported: list[str]


class PolicyUpdate(BaseModel):
    """A change to a connection's risk policy, from the console."""

    mode: AutomationMode
    auto_allowed: list[ActionType] = Field(default_factory=list)
    blocked: list[ActionType] = Field(default_factory=list)


class RejectionView(BaseModel):
    """Why one candidate was filtered out. Shown in the pipeline view."""

    action_type: str
    reason: str
    detail: str


class SimulateRequest(BaseModel):
    """Run the pipeline for a situation.

    candidates is optional. When omitted, the AI Reasoning Service proposes -
    which is what the storefront does. When supplied, the console can drive the
    pipeline by hand to explore how a policy change would behave, bypassing the
    model entirely.

    query matters more than it looks: without the shopper's actual search term the
    model can only guess at what they wanted, and a guessed alternative is worse
    than none.
    """

    connection_id: str
    friction: FrictionType | None = None
    candidates: list[ActionType] | None = None
    query: str | None = None
    order_id: str | None = None
    session_id: str | None = None
    cart_id: str | None = None

class ApprovalDecision(BaseModel):
    """A person's decision on one pending action."""

    approved: bool
    decided_by: str = "cv3-operator"
    note: str | None = None


class SimulateResponse(BaseModel):
    """The full pipeline trace, for display."""
    #: None when the case could not be recorded. The turn still succeeded; only
    #: the audit row is missing.
    case_id: str | None = None

    friction: str | None
    proposed: list[str]
    rejected: list[RejectionView]
    selected_action: str
    selection_reason: str
    escalated_because_empty: bool

    risk_outcome: str
    risk_rule: str
    risk_reason: str
    financial: bool
    reversible: bool
        # --- reasoning provenance ---
    # Surfaced deliberately. Whether a case was reasoned about or fell back to
    # rules changes how much weight an operator should give the diagnosis, and
    # hiding that would make the two indistinguishable.
    used_model: bool = False
    model_name: str | None = None
    diagnosis: str | None = None
    evidence: list[str] = Field(default_factory=list)
    reply: str | None = None
    shopper_reply: str | None = None
    fallback_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class RuleView(BaseModel):
    """One risk rule, in evaluation order."""

    order: int
    rule: str
    explanation: str


class ActionInfo(BaseModel):
    """An action type and its fixed risk properties.

    Sent to the console so the policy editor can grey out financial actions and
    explain why they can never be automatic, rather than letting a merchant tick
    a box that will be silently overridden.
    """

    action_type: str
    financial: bool
    reversible: bool
    touches_customer_data: bool
    can_ever_be_automatic: bool

class HandoverDone(BaseModel):
    """Who dealt with a handover.

    Required rather than optional. "Somebody closed this" is not much better than
    nobody having closed it, and an audit trail with anonymous entries is one
    nobody trusts.
    """

    handled_by: str

    #: What the operator did, in their words, sent to the shopper verbatim.
    #:
    #: Optional, though the interface asks for it. Making it mandatory would
    #: mean somebody in a hurry typing a full stop, which is worse than an
    #: honest blank - a handover closed without a note still resolves, and the
    #: shopper simply hears nothing.
    note: str | None = None
