"""The action pipeline.

One object travels the whole chain, gaining a field at each stage:

    AI Engine       -> ProposedAction    "we could do this"
    Decision Engine -> SelectedAction    "this is the one, and it is supported"
    Risk Gate       -> RiskDecision      "AUTO, HUMAN, or BLOCK"
    Adapter         -> ExecutionResult   "here is what happened"

The single most important thing in this module is that ProposedAction does
*not* carry risk properties. See ACTION_RISK_PROPERTIES below.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .capability import Operation
from .errors import ErrorCode


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActionType(StrEnum):
    """Every action the engine can take on a shopper's behalf.

    Distinct from Operation: an Operation is a technical call on the commerce
    interface, an ActionType is an intent the engine holds. SUGGEST_ALTERNATIVE
    maps to searchProducts; RETRY_PAYMENT maps to recoverPayment. Several
    action types can map to the same operation with different parameters.
    """

    # Assistance
    RECOMMEND_PRODUCTS = "RECOMMEND_PRODUCTS"
    ANSWER_PRODUCT_QUESTION = "ANSWER_PRODUCT_QUESTION"
    CHECK_AVAILABILITY = "CHECK_AVAILABILITY"

    # Recovery — non-financial
    SUGGEST_ALTERNATIVE = "SUGGEST_ALTERNATIVE"
    ADD_TO_CART = "ADD_TO_CART"
    UPDATE_CART_QUANTITY = "UPDATE_CART_QUANTITY"

    #: Show the shopper what they are about to pay, and how they can pay it.
    #:
    #: Charges nothing. The AI may propose this; only a shopper request can
    #: reach the endpoint that takes payment. That split is what lets
    #: checkout happen in the conversation without the AI ever being able to
    #: spend anybody's money.
    #: Empty the cart.
    #:
    #: Added because a shopper said "remove all" and the model, having only
    #: REMOVE_CART_LINE available, said it had cleared the cart and removed
    #: one line. Reversible - anything removed can go back - and charges
    #: nothing.
    CLEAR_CART = "CLEAR_CART"

    PREPARE_CHECKOUT = "PREPARE_CHECKOUT"

    #: Look up an order the shopper names, and report its status.
    #:
    #: Reads and reports. What it may say is deliberately thin - status and
    #: total, nothing else - because an order id is guessable and there is
    #: no shopper identity to check it against yet.
    CHECK_ORDER_STATUS = "CHECK_ORDER_STATUS"
    REMOVE_CART_LINE = "REMOVE_CART_LINE"
    NOTIFY_BACK_IN_STOCK = "NOTIFY_BACK_IN_STOCK"

    # Recovery — money-touching
    APPLY_PROMOTION = "APPLY_PROMOTION"
    RETRY_PAYMENT = "RETRY_PAYMENT"
    OFFER_ALTERNATE_PAYMENT = "OFFER_ALTERNATE_PAYMENT"
    SPLIT_PAYMENT = "SPLIT_PAYMENT"
    ISSUE_REFUND = "ISSUE_REFUND"
    CANCEL_ORDER = "CANCEL_ORDER"

    # Terminal
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    NO_ACTION = "NO_ACTION"


class RiskProperties(_Base):
    """The intrinsic properties of an action type that determine its risk.

    These are facts about the action, not judgements about the situation.
    Issuing a refund is financial and irreversible whatever the circumstances,
    whatever the AI thinks, and whatever the shopper says.
    """

    financial: bool
    reversible: bool
    touches_customer_data: bool = False


#: The static risk table. This is the security boundary of the whole system.
#:
#: Risk properties are looked up from the action type — they are never carried
#: on a ProposedAction, never supplied by the AI Engine, and never inferred at
#: runtime. If the model could assert "this refund is non-financial", the
#: deterministic Risk Gate would be deterministic in name only. It cannot,
#: because there is nowhere on ProposedAction to put such a claim.
#:
#: Changing this table is a code change, reviewed like any other.
ACTION_RISK_PROPERTIES: dict[ActionType, RiskProperties] = {
    ActionType.RECOMMEND_PRODUCTS: RiskProperties(financial=False, reversible=True),
    ActionType.ANSWER_PRODUCT_QUESTION: RiskProperties(financial=False, reversible=True),
    ActionType.CHECK_AVAILABILITY: RiskProperties(financial=False, reversible=True),
    ActionType.SUGGEST_ALTERNATIVE: RiskProperties(financial=False, reversible=True),
    ActionType.ADD_TO_CART: RiskProperties(financial=False, reversible=True),
    ActionType.UPDATE_CART_QUANTITY: RiskProperties(financial=False, reversible=True),
    ActionType.PREPARE_CHECKOUT: RiskProperties(financial=False, reversible=True),
    ActionType.CLEAR_CART: RiskProperties(financial=False, reversible=True),
    ActionType.CHECK_ORDER_STATUS: RiskProperties(financial=False, reversible=True),
    ActionType.REMOVE_CART_LINE: RiskProperties(financial=False, reversible=True),
    ActionType.NOTIFY_BACK_IN_STOCK: RiskProperties(
        financial=False, reversible=False, touches_customer_data=True
    ),
    # Promotions reduce what the merchant collects. Reversible in the cart,
    # but it is still the merchant's money.
    ActionType.APPLY_PROMOTION: RiskProperties(financial=True, reversible=True),
    ActionType.RETRY_PAYMENT: RiskProperties(financial=True, reversible=False),
    ActionType.OFFER_ALTERNATE_PAYMENT: RiskProperties(financial=True, reversible=False),
    ActionType.SPLIT_PAYMENT: RiskProperties(financial=True, reversible=False),
    ActionType.ISSUE_REFUND: RiskProperties(financial=True, reversible=False),
    ActionType.CANCEL_ORDER: RiskProperties(financial=True, reversible=False),
    ActionType.ESCALATE_TO_HUMAN: RiskProperties(financial=False, reversible=True),
    ActionType.NO_ACTION: RiskProperties(financial=False, reversible=True),
}


def risk_properties_for(action_type: ActionType) -> RiskProperties:
    """Look up risk properties. Missing entries fail closed.

    A new ActionType added without a table entry is treated as financial and
    irreversible — the most restrictive classification — so forgetting to
    update the table degrades to "requires human approval" rather than to
    "silently automated".
    """
    return ACTION_RISK_PROPERTIES.get(
        action_type, RiskProperties(financial=True, reversible=False)
    )


class ProposedAction(_Base):
    """A candidate from the AI Engine. A proposal, never a decision.

    Deliberately carries no risk classification, no approval state, and no
    assertion about whether it is safe. The AI may explain its reasoning in
    `rationale`, and that reasoning is shown to humans reviewing the action —
    but it has no mechanical effect on classification.
    """

    action_type: ActionType
    operation: Operation | None = Field(
        default=None,
        description="Which interface operation this would execute, if any. None "
        "for actions like ESCALATE_TO_HUMAN that need no commerce call.",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = Field(
        default=None, description="Why the model proposed this. Advisory only."
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class SelectedAction(_Base):
    """The Decision Engine's choice — one action, confirmed supported."""

    action: ProposedAction
    connection_id: str
    selected_from: int = Field(
        ge=1, description="How many candidates were considered. For audit."
    )
    selection_reason: str
    capability_verified: bool = Field(
        description="Set only after checking the connection's declared capability "
        "set. An action reaching the Risk Gate with this False is a bug."
    )


class RiskOutcome(StrEnum):
    """The three possible verdicts.

    BLOCK is distinct from HUMAN. HUMAN means a person decides. BLOCK means the
    action is not permitted on this connection at all and no approval queue
    entry is created — policy forbids it outright.
    """

    AUTO = "AUTO"
    HUMAN = "HUMAN"
    BLOCK = "BLOCK"


class RiskDecision(_Base):
    """The Risk Gate's verdict. Deterministic: same input, same output, always."""

    outcome: RiskOutcome
    action_type: ActionType
    properties: RiskProperties
    policy_rule: str = Field(
        description="Which configured rule produced this verdict. Every decision "
        "must name its rule — an unexplainable verdict is not auditable."
    )
    reason: str
    decided_at: datetime


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ExecutionResult(_Base):
    """What actually happened at the adapter."""

    succeeded: bool
    action_type: ActionType
    operation: Operation | None = None
    connection_id: str
    idempotency_key: str | None = Field(
        default=None,
        description="Required for every financial action, so a retry after a "
        "timeout cannot execute twice.",
    )
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: ErrorCode | None = None
    error_message: str | None = None
    attempts: int = 1
    latency_ms: int | None = None
    executed_at: datetime