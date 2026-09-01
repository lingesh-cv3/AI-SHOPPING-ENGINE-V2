"""The Decision Engine.

Answers exactly one question: which of these candidate actions should we take?

Takes the AI's proposals and returns one SelectedAction. No LLM - this module
imports no model client and reads no free text. The AI's rationale is carried
through untouched for humans to read; it has no effect on selection.

Three stages, in order:

1. Capability filter. Drop candidates whose operation the connection has not
   declared. This is the only place capability_verified is set to True, and the
   Risk Gate blocks anything arriving without it, so this filter is load-bearing
   rather than advisory.

2. Policy filter. Drop what the merchant blocked outright. Selecting something
   only for the gate to BLOCK it one layer later wastes a decision and muddies
   the audit trail.

3. Ranking. Take the most preferred survivor. Confidence breaks ties but never
   overrides preference order.

When nothing survives, the engine returns ESCALATE_TO_HUMAN rather than nothing.
A shopper with a declined card on a platform that cannot retry payments still
needs help; silence is the answer a naive implementation gives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.models import (
    READ_ONLY_OPERATIONS,
    ActionType,
    CapabilitySet,
    FrictionType,
    Operation,
    ProposedAction,
    SelectedAction,
)

from ..risk.policy import PolicyStore
from . import ranking
from .registry import CapabilityRegistry

#: Actions needing no commerce operation. These bypass the capability filter
#: because there is nothing to check - talking to a shopper or handing a case to
#: a colleague does not touch the merchant's backend.
NO_OPERATION_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.ESCALATE_TO_HUMAN,
        ActionType.NO_ACTION,
        ActionType.ANSWER_PRODUCT_QUESTION,
    }
)


@dataclass
class RejectedCandidate:
    """Why one candidate did not make it. Structured, not a log line.

    Operators need to answer "why didn't the engine offer a refund?" from the case
    record months later, without digging through logs.
    """

    action_type: ActionType
    reason: str
    detail: str = ""


@dataclass
class DecisionTrace:
    """The full record of one decision.

    selected is never None. When every candidate is filtered out it holds an
    ESCALATE_TO_HUMAN action - the engine always decides something.
    """

    selected: SelectedAction
    considered: int
    rejected: list[RejectedCandidate] = field(default_factory=list)
    escalated_because_empty: bool = False

    @property
    def survived(self) -> int:
        return self.considered - len(self.rejected)


def operation_for(action_type: ActionType) -> Operation | None:
    """Conventional operation for an action type.

    A convenience for the reasoning layer, which should populate
    ProposedAction.operation rather than leaving the engine to guess. Defined here
    so both sides agree on the mapping instead of duplicating it.
    """
    mapping: dict[ActionType, Operation] = {
        ActionType.RECOMMEND_PRODUCTS: Operation.SEARCH_PRODUCTS,
        ActionType.CHECK_AVAILABILITY: Operation.CHECK_INVENTORY,
        ActionType.SUGGEST_ALTERNATIVE: Operation.SEARCH_PRODUCTS,
        ActionType.ADD_TO_CART: Operation.ADD_TO_CART,
        ActionType.UPDATE_CART_QUANTITY: Operation.UPDATE_CART,
        ActionType.REMOVE_CART_LINE: Operation.UPDATE_CART,
ActionType.PREPARE_CHECKOUT: Operation.GET_CART,
        ActionType.APPLY_PROMOTION: Operation.APPLY_PROMOTION,
        ActionType.RETRY_PAYMENT: Operation.RECOVER_PAYMENT,
        ActionType.OFFER_ALTERNATE_PAYMENT: Operation.RECOVER_PAYMENT,
        ActionType.SPLIT_PAYMENT: Operation.RECOVER_PAYMENT,
        ActionType.ISSUE_REFUND: Operation.RECOVER_PAYMENT,
        ActionType.CANCEL_ORDER: Operation.GET_ORDER,
    }
    return mapping.get(action_type)


def is_read_only(operation: Operation | None) -> bool:
    """Whether an operation is safe for the AI to call directly as a tool."""
    return operation is not None and operation in READ_ONLY_OPERATIONS




class DecisionEngine:
    """Selects one action from many. Deterministic given the same inputs."""

    def __init__(self, registry: CapabilityRegistry, policies: PolicyStore) -> None:
        self._registry = registry
        self._policies = policies

    async def decide(
        self,
        candidates: list[ProposedAction],
        *,
        connection_id: str,
        friction: FrictionType | None = None,
    ) -> DecisionTrace:
        """Filter, rank, and select.

        friction drives the preference order. None means a plain shopping
        assistance turn rather than a recovery case.
        """
        capabilities = await self._registry.get(connection_id)
        policy = self._policies.get(connection_id)

        rejected: list[RejectedCandidate] = []
        survivors: list[ProposedAction] = []

        for candidate in candidates:
            rejection = self._reject_reason(candidate, capabilities, policy.blocked)
            if rejection is None:
                survivors.append(candidate)
            else:
                rejected.append(rejection)

        if not survivors:
            return DecisionTrace(
                selected=self._escalation(
                    connection_id,
                    considered=len(candidates),
                    reason=self._escalation_reason(candidates, capabilities, rejected),
                ),
                considered=len(candidates),
                rejected=rejected,
                escalated_because_empty=True,
            )

        best = min(
            survivors,
            key=lambda c: (
                ranking.rank_of(c.action_type, friction),
                # Confidence is only ever a tie-breaker within one rank. A very
                # confident discount still loses to a free substitute, because
                # cost to the merchant is a policy question and confidence is not.
                -(c.confidence if c.confidence is not None else 0.0),
            ),
        )

        return DecisionTrace(
            selected=SelectedAction(
                action=best,
                connection_id=connection_id,
                selected_from=len(candidates),
                selection_reason=(
                    f"highest-ranked available action for {friction or 'assistance'} "
                    f"(rank {ranking.rank_of(best.action_type, friction)} of "
                    f"{len(ranking.rank_for(friction))})"
                ),
                # Set here and nowhere else.
                capability_verified=True,
            ),
            considered=len(candidates),
            rejected=rejected,
        )

    def _reject_reason(
        self,
        candidate: ProposedAction,
        capabilities: CapabilitySet | None,
        blocked: set[ActionType],
    ) -> RejectedCandidate | None:
        """None means the candidate survives."""

        # Policy first. If forbidden, whether the platform could do it is moot.
        if candidate.action_type in blocked:
            return RejectedCandidate(
                candidate.action_type,
                "BLOCKED_BY_POLICY",
                "the merchant has forbidden this action on this connection",
            )

        if candidate.action_type in NO_OPERATION_ACTIONS:
            return None

        if candidate.operation is None:
            # Malformed proposal. Drop it rather than guess what was meant.
            return RejectedCandidate(
                candidate.action_type,
                "NO_OPERATION_SPECIFIED",
                "action requires a commerce operation but names none",
            )

        if capabilities is None:
            # Unknown connection. Treated as nothing supported - the safe
            # direction when we cannot know.
            return RejectedCandidate(
                candidate.action_type,
                "CAPABILITIES_UNKNOWN",
                "no capability declaration available for this connection",
            )

        if not capabilities.supports(candidate.operation):
            declared = capabilities.operations.get(candidate.operation)
            return RejectedCandidate(
                candidate.action_type,
                "CAPABILITY_UNSUPPORTED",
                (declared.reason if declared and declared.reason else "")
                or f"connection does not support {candidate.operation}",
            )

        return None

    def _escalation(
        self, connection_id: str, *, considered: int, reason: str
    ) -> SelectedAction:
        return SelectedAction(
            action=ProposedAction(
                action_type=ActionType.ESCALATE_TO_HUMAN, rationale=reason
            ),
            connection_id=connection_id,
            selected_from=max(considered, 1),
            selection_reason=reason,
            # Escalation needs no platform capability, so there is nothing to
            # verify and verification is trivially true.
            capability_verified=True,
        )

    def _escalation_reason(
        self,
        candidates: list[ProposedAction],
        capabilities: CapabilitySet | None,
        rejected: list[RejectedCandidate],
    ) -> str:
        """A specific reason, not a generic one.

        The human picking this up needs to know whether the platform cannot do it,
        the merchant forbade it, or the AI proposed nothing. Those lead to
        completely different responses.
        """
        if not candidates:
            return "no action was proposed for this situation"

        if capabilities is None:
            return (
                "the connection has no capability declaration, so no action could "
                "be verified as safe to attempt"
            )

        reasons = {r.reason for r in rejected}
        if reasons == {"CAPABILITY_UNSUPPORTED"}:
            unsupported = ", ".join(sorted({str(r.action_type) for r in rejected}))
            return (
                f"this merchant's platform does not support any applicable action "
                f"({unsupported}); a person needs to handle it"
            )
        if reasons == {"BLOCKED_BY_POLICY"}:
            return "every applicable action is blocked by this merchant's policy"

        return (
            "no proposed action was both supported by the platform and permitted "
            "by policy"
        )