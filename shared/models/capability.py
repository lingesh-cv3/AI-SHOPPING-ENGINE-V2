"""Capability declaration.

The hard rule from the architecture: capability authority belongs to the
adapter, never the model. The AI Engine may propose anything; only the
capability set for a specific merchant connection decides what is actually
available. Nothing downstream assumes a capability — it looks it up.

Note the scope carefully. Capabilities belong to a *merchant connection*, not
to a platform. Two merchants on the same platform run identical adapter code
and may still have different capability sets, because one enabled promotions
in their store configuration and the other did not.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .commerce import PaymentRecoveryMethod


class Operation(StrEnum):
    """Every operation in the Standard Commerce Interface.

    This enum is the single source of truth linking three things: the methods
    on the interface, the keys in a capability set, and the operations an
    Action can target. They must not drift apart, so they share one enum.
    """

    GET_CAPABILITIES = "getCapabilities"
    SEARCH_PRODUCTS = "searchProducts"
    GET_PRODUCT = "getProduct"
    CHECK_INVENTORY = "checkInventory"
    GET_CART = "getCart"
    ADD_TO_CART = "addToCart"
    UPDATE_CART = "updateCart"
    GET_ORDER = "getOrder"
    APPLY_PROMOTION = "applyPromotion"
    CHECKOUT = "checkout"
    RECOVER_PAYMENT = "recoverPayment"


#: Operations that only read. These never touch money, never change state, and
#: are the only operations the AI Engine may call directly as tools during
#: reasoning. Everything else must travel the full
#: Decision -> Risk -> Approval -> Adapter path.
READ_ONLY_OPERATIONS: frozenset[Operation] = frozenset(
    {
        Operation.GET_CAPABILITIES,
        Operation.SEARCH_PRODUCTS,
        Operation.GET_PRODUCT,
        Operation.CHECK_INVENTORY,
        Operation.GET_CART,
        Operation.GET_ORDER,
    }
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationCapability(_Base):
    """Whether one operation is available, and under what constraints.

    `supported=False` is a first-class, expected answer. Most connections will
    not support recoverPayment, and the system is designed around that rather
    than treating it as a degraded state.
    """

    operation: Operation
    supported: bool
    reason: str | None = Field(
        default=None,
        description="Why it is unsupported — for operators reading the console, "
        "and for the engine to explain a fallback honestly.",
    )
    constraints: dict[str, str | int | bool] = Field(
        default_factory=dict,
        description="Operation-specific limits, e.g. {'max_cart_lines': 50} or "
        "{'modifiable_after_creation': False}.",
    )


class CapabilitySet(_Base):
    """The full declared capability of one merchant connection.

    Produced by getCapabilities(), refreshed when a connection is established
    and whenever its configuration changes. The Decision Engine checks against
    a cached copy of this; the adapter re-checks at execution time, because a
    capability can be revoked between decision and execution.
    """

    connection_id: str
    platform: str
    operations: dict[Operation, OperationCapability]
    payment_recovery_methods: list[PaymentRecoveryMethod] = Field(
        default_factory=list,
        description="Which recovery mechanisms this connection actually exposes. "
        "Empty is the expected default until a real merchant architecture is "
        "confirmed — an empty list means recovery must escalate to a human "
        "rather than execute.",
    )
    supports_webhooks: bool = False
    webhook_events: list[str] = Field(default_factory=list)
    declared_at: datetime

    def supports(self, operation: Operation) -> bool:
        """The lookup every capability check goes through.

        Unknown operations return False rather than raising. An operation
        missing from the map means the adapter did not declare it, and
        undeclared is treated as unsupported — the safe direction to fail.
        """
        cap = self.operations.get(operation)
        return bool(cap and cap.supported)

    def constraint(self, operation: Operation, key: str) -> str | int | bool | None:
        cap = self.operations.get(operation)
        return cap.constraints.get(key) if cap else None

    def unsupported(self) -> list[Operation]:
        """Everything this connection cannot do — drives the console's capability view."""
        return [op for op in Operation if not self.supports(op)]