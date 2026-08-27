"""Merchant risk policy.

Configuration, not logic. A policy says what a merchant has agreed to; the gate
decides what that means for a specific action.

The critical design property: there is nothing a merchant can put in a policy
that makes a money-touching action automatic. The policy can only ever be more
restrictive than the gate's floor, never less.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from shared.models import ActionType

from collections.abc import Callable
from enum import StrEnum


class AutomationMode(StrEnum):
    """How much the merchant currently trusts the engine.

    Every new connection starts CAUTIOUS. Moving to STANDARD is a deliberate
    decision made after the merchant has watched the engine work.
    """

    #: Everything requires human approval, even trivial reversible actions.
    CAUTIOUS = "CAUTIOUS"

    #: Actions the gate proves safe - non-financial, reversible, no customer
    #: contact - run automatically. Everything else still waits for a person.
    STANDARD = "STANDARD"
    #: Nothing runs, nothing queues. Halts a connection during an incident
    #: without disconnecting it.
    SUSPENDED = "SUSPENDED"


class RiskPolicy(BaseModel):
    """One merchant connection's risk configuration."""

    model_config = ConfigDict(extra="forbid")

    connection_id: str
    mode: AutomationMode = AutomationMode.CAUTIOUS

    auto_allowed: set[ActionType] = Field(
        default_factory=set,
        description="Optional restriction. Empty - the normal case - means every "
        "action the gate proves safe runs automatically in STANDARD mode. "
        "Populated, it narrows that to the listed actions only. It can never "
        "widen: the gate's floor is checked first, so a financial action listed "
        "here is still gated.",
    )

    blocked: set[ActionType] = Field(
        default_factory=set,
        description="Actions forbidden outright. Produces BLOCK, not HUMAN - "
        "nothing queues and no one is asked.",
    )

    approval_timeout_minutes: int = Field(
        default=15,
        ge=1,
        description="How long a pending approval waits before expiring. A shopper "
        "staring at a declined card will not wait long.",
    )

    def __hash__(self) -> int:
        return hash(self.connection_id)


def default_policy(connection_id: str) -> RiskPolicy:
    """What a brand-new connection gets before anyone configures it.

    STANDARD, not CAUTIOUS. Cautious meant every harmless thing - answering a
    question, suggesting a product - queued for approval, so a merchant's first
    experience was a hundred notifications about nothing. That trains people to
    approve without reading. Standard is safe because the gate makes it safe:
    rules 5 to 7 run before any policy is read, so nothing touching money, nothing
    irreversible, and nothing that contacts a customer runs unattended whatever
    the mode says.
    """
    return RiskPolicy(
        connection_id=connection_id,
        mode=AutomationMode.STANDARD,
        auto_allowed=set(),
        blocked=set(),
    )


#: A reasonable STANDARD-mode allowlist. Every entry is non-financial and
#: reversible; the gate would refuse anything else regardless of it appearing here.
SUGGESTED_AUTO_ALLOWED: frozenset[ActionType] = frozenset(
    {
        ActionType.RECOMMEND_PRODUCTS,
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.CHECK_AVAILABILITY,
        ActionType.SUGGEST_ALTERNATIVE,
        ActionType.ADD_TO_CART,
        ActionType.UPDATE_CART_QUANTITY,
        ActionType.REMOVE_CART_LINE,
    }
)


class PolicyStore:
    """Policy lookup, keyed by connection.

    Reads are synchronous and served from memory, because the Risk Gate is
    synchronous and called on every decision - making it async would ripple through
    the gate, the decision engine and every caller for no benefit.

    Persistence lives in the API layer, which writes to the database when a setting
    changes and calls hydrate() at startup. Keeping it out of this module matters:
    the risk package should be testable with nothing but Python, and a gate that
    needs a database to answer a question is a gate that can fail for reasons
    unrelated to risk.

    A lookup that misses returns the restrictive default rather than raising, so an
    unconfigured connection degrades to "ask a human" instead of running unattended.
    """

    def __init__(self, on_change: Callable[[RiskPolicy], None] | None = None) -> None:
        self._policies: dict[str, RiskPolicy] = {}
        self._on_change = on_change

    def get(self, connection_id: str) -> RiskPolicy:
        return self._policies.get(connection_id) or default_policy(connection_id)

    def set(self, policy: RiskPolicy) -> None:
        self._policies[policy.connection_id] = policy
        if self._on_change is not None:
            self._on_change(policy)

    def hydrate(self, policies: list[RiskPolicy]) -> None:
        """Load stored policies at startup, without re-triggering persistence."""
        for policy in policies:
            self._policies[policy.connection_id] = policy

    def all(self) -> list[RiskPolicy]:
        return list(self._policies.values())