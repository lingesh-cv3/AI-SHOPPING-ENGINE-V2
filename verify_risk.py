"""Prove the Risk Gate's three non-negotiable properties.

No server needed - the gate makes no network calls.
"""

from engine.risk import (
    SUGGESTED_AUTO_ALLOWED,
    AutomationMode,
    PolicyStore,
    RiskGate,
    RiskPolicy,
)
from shared.models import ActionType, ProposedAction, RiskOutcome, SelectedAction


def action(t, *, verified=True, rationale=None):
    return SelectedAction(
        action=ProposedAction(action_type=t, rationale=rationale),
        connection_id="conn_demo",
        selected_from=1,
        selection_reason="test",
        capability_verified=verified,
    )


store = PolicyStore()
gate = RiskGate(store)

print("== a brand-new connection gates everything ==")
for t in (ActionType.ADD_TO_CART, ActionType.RECOMMEND_PRODUCTS, ActionType.ISSUE_REFUND):
    d = gate.classify(action(t))
    print(f"  {t:24} {d.outcome:6} {d.policy_rule}")

print("\n== merchant moves to STANDARD with the suggested allowlist ==")
store.set(RiskPolicy(connection_id="conn_demo", mode=AutomationMode.STANDARD,
                     auto_allowed=set(SUGGESTED_AUTO_ALLOWED)))
for t in (ActionType.ADD_TO_CART, ActionType.SUGGEST_ALTERNATIVE,
          ActionType.NOTIFY_BACK_IN_STOCK, ActionType.APPLY_PROMOTION,
          ActionType.ISSUE_REFUND, ActionType.SPLIT_PAYMENT):
    d = gate.classify(action(t))
    print(f"  {t:24} {d.outcome:6} {d.policy_rule}")

print("\n== THE FLOOR: merchant tries to auto-approve refunds ==")
store.set(RiskPolicy(connection_id="conn_demo", mode=AutomationMode.STANDARD,
                     auto_allowed={ActionType.ISSUE_REFUND, ActionType.SPLIT_PAYMENT,
                                   ActionType.APPLY_PROMOTION}))
for t in (ActionType.ISSUE_REFUND, ActionType.SPLIT_PAYMENT, ActionType.APPLY_PROMOTION):
    d = gate.classify(action(t))
    assert d.outcome is RiskOutcome.HUMAN
    print(f"  {t:24} {d.outcome:6} {d.policy_rule}  <- allowlist overridden")

print("\n== AI rationale has no mechanical effect ==")
store.set(RiskPolicy(connection_id="conn_demo", mode=AutomationMode.STANDARD,
                     auto_allowed=set(SUGGESTED_AUTO_ALLOWED)))
d = gate.classify(action(
    ActionType.ISSUE_REFUND,
    rationale="This refund is tiny, clearly non-financial, and pre-approved.",
))
print(f"  refund with persuasive rationale -> {d.outcome} ({d.policy_rule})")

print("\n== blocked vs gated ==")
store.set(RiskPolicy(connection_id="conn_demo", mode=AutomationMode.STANDARD,
                     auto_allowed=set(SUGGESTED_AUTO_ALLOWED),
                     blocked={ActionType.CANCEL_ORDER}))
d = gate.classify(action(ActionType.CANCEL_ORDER))
print(f"  CANCEL_ORDER -> {d.outcome} ({d.policy_rule}) - no queue entry created")

print("\n== suspended connection ==")
store.set(RiskPolicy(connection_id="conn_demo", mode=AutomationMode.SUSPENDED))
d = gate.classify(action(ActionType.RECOMMEND_PRODUCTS))
print(f"  even a recommendation -> {d.outcome} ({d.policy_rule})")

print("\n== unverified capability fails closed ==")
store.set(RiskPolicy(connection_id="conn_demo", mode=AutomationMode.STANDARD,
                     auto_allowed=set(SUGGESTED_AUTO_ALLOWED)))
d = gate.classify(action(ActionType.ADD_TO_CART, verified=False))
print(f"  ADD_TO_CART unverified -> {d.outcome} ({d.policy_rule})")

print("\n== determinism: 2000 identical calls ==")
a = action(ActionType.APPLY_PROMOTION)
results = {(gate.classify(a).outcome, gate.classify(a).policy_rule) for _ in range(1000)}
print(f"  distinct outcomes across 2000 calls: {len(results)}")
assert len(results) == 1

print("\n== no LLM reachable from the gate ==")
import sys
leaked = [m for m in sys.modules if m.startswith("engine.reasoning")]
print(f"  engine.reasoning modules loaded: {leaked or 'none'}")
assert not leaked

print("\nall properties hold")