"""Decision Engine against a live sample merchant, then through the Risk Gate."""

import asyncio

from adapters.sample import SampleMerchantAdapter
from engine.decision import (
    CapabilityRegistry,
    DecisionEngine,
    explain_preference,
    operation_for,
)
from engine.risk import (
    SUGGESTED_AUTO_ALLOWED,
    AutomationMode,
    PolicyStore,
    RiskGate,
    RiskPolicy,
)
from shared.models import ActionType, FrictionType, ProposedAction


def cand(t, *, confidence=None):
    return ProposedAction(
        action_type=t, operation=operation_for(t), confidence=confidence
    )


async def main():
    adapter = SampleMerchantAdapter(
        connection_id="conn_demo", base_url="http://127.0.0.1:8001"
    )
    registry = CapabilityRegistry()
    registry.register(adapter)
    policies = PolicyStore()
    engine = DecisionEngine(registry, policies)
    gate = RiskGate(policies)

    print("== preference chains ==")
    for f in (
        FrictionType.DEAD_SEARCH,
        FrictionType.PAYMENT_DECLINED,
        FrictionType.PROMOTION_FAILED,
    ):
        print(" ", explain_preference(f))

    print("\n== DEAD SEARCH: AI proposes 3, engine picks 1 ==")
    t = await engine.decide(
        [
            cand(ActionType.RECOMMEND_PRODUCTS, confidence=0.9),
            cand(ActionType.SUGGEST_ALTERNATIVE, confidence=0.4),
            cand(ActionType.APPLY_PROMOTION, confidence=0.95),
        ],
        connection_id="conn_demo",
        friction=FrictionType.DEAD_SEARCH,
    )
    print(f"  considered {t.considered}, survived {t.survived}")
    print(f"  selected: {t.selected.action.action_type}")
    print(f"  why: {t.selected.selection_reason}")
    print("  ^ SUGGEST_ALTERNATIVE won at 0.4 over APPLY_PROMOTION at 0.95")

    print("\n== PAYMENT DECLINED on a platform with no recovery ==")
    t = await engine.decide(
        [
            cand(ActionType.RETRY_PAYMENT, confidence=0.8),
            cand(ActionType.OFFER_ALTERNATE_PAYMENT),
            cand(ActionType.SPLIT_PAYMENT),
        ],
        connection_id="conn_demo",
        friction=FrictionType.PAYMENT_DECLINED,
    )
    print(
        f"  considered {t.considered}, survived {t.survived}, "
        f"empty-escalation: {t.escalated_because_empty}"
    )
    for r in t.rejected:
        print(f"  rejected {r.action_type}: {r.reason} - {r.detail}")
    print(f"  selected: {t.selected.action.action_type}")
    print(f"  reason given to the human: {t.selected.selection_reason}")

    print("\n== merchant blocks a supported action ==")
    policies.set(
        RiskPolicy(
            connection_id="conn_demo",
            mode=AutomationMode.STANDARD,
            auto_allowed=set(SUGGESTED_AUTO_ALLOWED),
            blocked={ActionType.SUGGEST_ALTERNATIVE},
        )
    )
    t = await engine.decide(
        [cand(ActionType.SUGGEST_ALTERNATIVE), cand(ActionType.RECOMMEND_PRODUCTS)],
        connection_id="conn_demo",
        friction=FrictionType.DEAD_SEARCH,
    )
    print(f"  rejected: {[(str(r.action_type), r.reason) for r in t.rejected]}")
    print(f"  selected: {t.selected.action.action_type}")

    print("\n== malformed proposal (no operation named) ==")
    t = await engine.decide(
        [ProposedAction(action_type=ActionType.ADD_TO_CART)],
        connection_id="conn_demo",
        friction=None,
    )
    print(f"  rejected: {[(str(r.action_type), r.reason) for r in t.rejected]}")
    print(f"  selected: {t.selected.action.action_type}")

    print("\n== unknown connection ==")
    t = await engine.decide(
        [cand(ActionType.RECOMMEND_PRODUCTS)],
        connection_id="conn_nonexistent",
        friction=FrictionType.DEAD_SEARCH,
    )
    print(f"  selected: {t.selected.action.action_type}")
    print(f"  reason: {t.selected.selection_reason}")

    print("\n== full pipeline: Decision -> Risk ==")
    policies.set(
        RiskPolicy(
            connection_id="conn_demo",
            mode=AutomationMode.STANDARD,
            auto_allowed=set(SUGGESTED_AUTO_ALLOWED),
        )
    )
    for friction, cands in [
        (FrictionType.DEAD_SEARCH, [cand(ActionType.SUGGEST_ALTERNATIVE)]),
        (FrictionType.PROMOTION_FAILED, [cand(ActionType.APPLY_PROMOTION)]),
        (FrictionType.PAYMENT_DECLINED, [cand(ActionType.RETRY_PAYMENT)]),
    ]:
        t = await engine.decide(cands, connection_id="conn_demo", friction=friction)
        d = gate.classify(t.selected)
        print(
            f"  {str(friction):22} -> {str(t.selected.action.action_type):22} "
            f"-> {d.outcome:6} ({d.policy_rule})"
        )

    print("\n== capability_verified is always set by the engine ==")
    t = await engine.decide([cand(ActionType.ADD_TO_CART)], connection_id="conn_demo")
    print(f"  capability_verified: {t.selected.capability_verified}")

    await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())