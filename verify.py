from datetime import datetime, UTC
from decimal import Decimal

from shared.models import (
    ActionType, CapabilitySet, Money, Operation, OperationCapability,
    ProductSearchResult, ProposedAction, risk_properties_for,
)
from shared.interfaces import StandardCommerceInterface

# Money is exact, never a float
print("money:", Money(amount=Decimal("4999.50"), currency="inr"))

# Risk properties come from the table, not from the model
print("refund:", risk_properties_for(ActionType.ISSUE_REFUND))
print("add to cart:", risk_properties_for(ActionType.ADD_TO_CART))

# Unknown action types fail closed
print("unknown fails closed:", risk_properties_for("NOT_IN_TABLE"))

# Undeclared capability means unsupported
caps = CapabilitySet(
    connection_id="conn_1", platform="sample",
    operations={
        Operation.SEARCH_PRODUCTS: OperationCapability(
            operation=Operation.SEARCH_PRODUCTS, supported=True
        )
    },
    declared_at=datetime.now(UTC),
)
print("supports search:", caps.supports(Operation.SEARCH_PRODUCTS))
print("supports recoverPayment (undeclared):", caps.supports(Operation.RECOVER_PAYMENT))
print("unsupported count:", len(caps.unsupported()))

# Dead search is built into the contract
print("dead search:", ProductSearchResult(query="nike shoes", products=[]).is_dead_search)

# The AI cannot assert its own risk properties
try:
    ProposedAction(action_type=ActionType.ISSUE_REFUND, financial=False)
    print("LEAK: model asserted risk properties")
except Exception as e:
    print("risk claim rejected:", type(e).__name__)

# An incomplete adapter cannot be instantiated
class HalfBaked(StandardCommerceInterface):
    async def get_capabilities(self): ...
    async def search_products(self, query, *, limit=20): ...

try:
    HalfBaked()
    print("LEAK: incomplete adapter instantiated")
except TypeError:
    print("incomplete adapter rejected")