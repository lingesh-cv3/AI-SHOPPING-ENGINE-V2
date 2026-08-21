"""The Risk Gate.

Answers exactly one question: are we allowed to do this automatically?

Three non-negotiable properties:

Deterministic. Same action, same policy, same outcome. No randomness, no
clock-dependent branching, no network calls, no model. decided_at is recorded as
metadata but never read as input.

No LLM, structurally. This module imports shared.models and .policy and nothing
else. It cannot reach engine.reasoning because it does not import it. The AI may
argue in its rationale that an action is safe; that text has no mechanical effect
here and is only shown to the human reviewing it.

Every verdict names its rule. An unexplainable verdict is not auditable, and a
system that touches money must be auditable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.models import (
    ACTION_RISK_PROPERTIES,
    RiskDecision,
    RiskOutcome,
    SelectedAction,
    risk_properties_for,
)

from .policy import AutomationMode, PolicyStore, RiskPolicy

# The order below is load-bearing, not cosmetic. Two aspects matter:
#
# 1. CAPABILITY_UNVERIFIED comes first. An action that never had its capability
#    checked is an upstream bug, and running it would be worse than blocking it.
#
# 2. FINANCIAL_ALWAYS_HUMAN is checked BEFORE any merchant configuration. This is
#    what makes the floor a floor: no allowlist entry, no automation mode, and no
#    AI rationale can move a money-touching action to AUTO. It also means a
#    refund reports FINANCIAL_ALWAYS_HUMAN rather than CAUTIOUS_MODE - the former
#    is a permanent architectural rule, the latter a temporary setting, and an
#    operator reading the audit log needs to know which applied.

RULE_CAPABILITY_UNVERIFIED = "CAPABILITY_UNVERIFIED"
RULE_SUSPENDED = "CONNECTION_SUSPENDED"
RULE_BLOCKED_BY_POLICY = "BLOCKED_BY_MERCHANT_POLICY"
RULE_UNKNOWN_ACTION = "UNKNOWN_ACTION_TYPE"
RULE_FINANCIAL = "FINANCIAL_ALWAYS_HUMAN"
RULE_IRREVERSIBLE = "IRREVERSIBLE_ALWAYS_HUMAN"
RULE_CUSTOMER_DATA = "TOUCHES_CUSTOMER_DATA"
RULE_CAUTIOUS_MODE = "CAUTIOUS_MODE_GATES_ALL"
RULE_NOT_ALLOWLISTED = "NOT_ON_MERCHANT_ALLOWLIST"
RULE_AUTO_CLEARED = "AUTO_CLEARED"

#: The evaluation order in one place, for the console and for anyone auditing the
#: gate. The code below follows this exactly.
RULE_ORDER: tuple[str, ...] = (
    RULE_CAPABILITY_UNVERIFIED,
    RULE_SUSPENDED,
    RULE_BLOCKED_BY_POLICY,
    RULE_UNKNOWN_ACTION,
    RULE_FINANCIAL,
    RULE_IRREVERSIBLE,
    RULE_CUSTOMER_DATA,
    RULE_CAUTIOUS_MODE,
    RULE_NOT_ALLOWLISTED,
    RULE_AUTO_CLEARED,
)


def explain_rules() -> list[tuple[str, str]]:
    """Human-readable rule order, for the Merchant and Operations consoles.

    Exposing this matters: a merchant asking "why did the engine ask me about
    that?" should get the actual rule, not a paraphrase written separately that
    can drift out of step with the code.
    """
    return [
        (RULE_CAPABILITY_UNVERIFIED, "Blocked - never capability-checked"),
        (RULE_SUSPENDED, "Blocked - the connection is suspended"),
        (RULE_BLOCKED_BY_POLICY, "Blocked - you have forbidden this action"),
        (RULE_UNKNOWN_ACTION, "Human - unrecognised action, treated as financial"),
        (RULE_FINANCIAL, "Human - the action touches money. Never automatic."),
        (RULE_IRREVERSIBLE, "Human - the action cannot be undone"),
        (RULE_CUSTOMER_DATA, "Human - the action contacts your customer"),
        (RULE_CAUTIOUS_MODE, "Human - cautious mode gates everything"),
        (RULE_NOT_ALLOWLISTED, "Needs your approval — you've limited automatic actions to a shorter list"),
        (RULE_AUTO_CLEARED, "Automatic - safe, reversible, and allowed by you"),
    ]


class RiskGate:
    """Deterministic classifier. Construct once, call many times."""

    def __init__(self, policies: PolicyStore) -> None:
        self._policies = policies

    def classify(self, selected: SelectedAction) -> RiskDecision:
        """Classify one selected action as AUTO, HUMAN, or BLOCK."""
        return self._evaluate(selected, self._policies.get(selected.connection_id))

    def classify_with_policy(
        self, selected: SelectedAction, policy: RiskPolicy
    ) -> RiskDecision:
        """Classify against an explicit policy.

        For tests, and for dry-run tooling answering "what would happen if we
        changed this setting".
        """
        return self._evaluate(selected, policy)

    def _evaluate(self, selected: SelectedAction, policy: RiskPolicy) -> RiskDecision:
        action_type = selected.action.action_type
        properties = risk_properties_for(action_type)

        def decide(outcome: RiskOutcome, rule: str, reason: str) -> RiskDecision:
            return RiskDecision(
                outcome=outcome,
                action_type=action_type,
                properties=properties,
                policy_rule=rule,
                reason=reason,
                # Metadata only. Never read as input, so determinism holds.
                decided_at=datetime.now(UTC),
            )

        # 1. Capability never verified. Upstream bug; fail closed.
        if not selected.capability_verified:
            return decide(
                RiskOutcome.BLOCK,
                RULE_CAPABILITY_UNVERIFIED,
                "action reached the gate without a capability check",
            )

        # 2. Connection halted. Nothing runs and nothing queues, so an incident
        #    does not fill the queue with work nobody will action.
        if policy.mode is AutomationMode.SUSPENDED:
            return decide(RiskOutcome.BLOCK, RULE_SUSPENDED, "connection is suspended")

        # 3. Merchant forbade it. BLOCK, not HUMAN - asking about something they
        #    already refused wastes their time and implies the answer might be yes.
        if action_type in policy.blocked:
            return decide(
                RiskOutcome.BLOCK,
                RULE_BLOCKED_BY_POLICY,
                f"{action_type} is blocked on this connection",
            )

        # 4. Absent from the risk table. risk_properties_for already failed closed,
        #    but a distinct rule name tells an operator this is a missing table
        #    entry rather than a genuinely financial action.
        if action_type not in ACTION_RISK_PROPERTIES:
            return decide(
                RiskOutcome.HUMAN,
                RULE_UNKNOWN_ACTION,
                f"{action_type} has no risk-table entry; treated as financial",
            )

        # 5. THE FLOOR. Checked before any merchant configuration, so no policy
        #    can override it.
        if properties.financial:
            return decide(
                RiskOutcome.HUMAN,
                RULE_FINANCIAL,
                "action touches money; human approval is mandatory",
            )

        # 6. Irreversible gets a human even with no money involved, because
        #    "undo it" is not available if the judgement was wrong.
        if not properties.reversible:
            return decide(
                RiskOutcome.HUMAN,
                RULE_IRREVERSIBLE,
                "action cannot be undone; human approval required",
            )

        # 7. Anything reaching the customer directly. Reversible in the database,
        #    not in their inbox.
        if properties.touches_customer_data:
            return decide(
                RiskOutcome.HUMAN,
                RULE_CUSTOMER_DATA,
                "action contacts the customer or uses their data",
            )

        # 8. Cautious mode gates everything. The default for a new connection.
        if policy.mode is AutomationMode.CAUTIOUS:
            return decide(
                RiskOutcome.HUMAN,
                RULE_CAUTIOUS_MODE,
                "connection is in cautious mode; all actions are gated",
            )

        # 9. Standard mode, but not approved. Absence of permission is not
        #    permission.
        # 9. Restricted by the merchant, where they have chosen to restrict.
        #
        #    This used to be an allowlist: an action ran only if the merchant had
        #    ticked it. That was a second gate on top of one that already works. By
        #    the time execution reaches here, rules 5 through 7 have proven the
        #    action moves no money, can be undone, and does not contact anyone.
        #    Requiring a tick as well added no safety and produced "needs approval"
        #    for provably harmless things, which trains merchants to approve without
        #    reading - the opposite of what the queue is for.
        #
        #    So the list is now optional and restrictive. Leave it empty and every
        #    safe action runs. Populate it and only those run. It can never widen
        #    permissions, because the floor is checked first.
        if policy.auto_allowed and action_type not in policy.auto_allowed:
            return decide(
                RiskOutcome.HUMAN,
                RULE_NOT_ALLOWLISTED,
                f"{action_type} is not on this merchant's restricted allowlist",
            )

        # 10. Only now does it run unattended.
        return decide(
            RiskOutcome.AUTO,
            RULE_AUTO_CLEARED,
            "non-financial, reversible, and explicitly allowed by the merchant",
        )