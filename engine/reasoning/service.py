"""The AI Reasoning Service.

Answers one question: what should we do? It proposes; it never decides.

Three properties worth stating plainly, because they are what make the model safe
to put in front of a commerce system.

It cannot assert risk. ProposedAction has no field for it, and the tool schema has
no field for it. A model arguing that a refund is harmless produces a rationale
string that a human reads and the Risk Gate ignores.

It cannot execute. This module imports no adapter, holds no client to a merchant
platform, and returns data. Execution happens after Decision and Risk have had
their say.

It fails soft. If the provider is down, rate limited, or the model has been
retired, the turn falls back to rule-based proposals rather than failing. A shopper
waiting on a declined card should not also see an error because our model vendor
was busy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from engine.decision import operation_for
from shared.models import (
    ActionType,
    Cart,
    Diagnosis,
    FrictionType,
    Order,
    Product,
    ProposedAction,
)

from .context import build_context
from .llm import LLMClient, LLMConfig, LLMUnavailable
from .prompts import PROPOSABLE, SYSTEM_PROMPT, propose_tool

logger = logging.getLogger(__name__)

#: Rule-based proposals, used when the model is unavailable. Deliberately the same
#: shape the model produces, so the fallback is invisible downstream.
_FALLBACK: dict[FrictionType, tuple[ActionType, ...]] = {
    FrictionType.DEAD_SEARCH: (
        ActionType.SUGGEST_ALTERNATIVE,
        ActionType.RECOMMEND_PRODUCTS,
    ),
    FrictionType.PRODUCT_UNAVAILABLE: (
        ActionType.SUGGEST_ALTERNATIVE,
        ActionType.NOTIFY_BACK_IN_STOCK,
    ),
    FrictionType.VARIANT_UNAVAILABLE: (
        ActionType.CHECK_AVAILABILITY,
        ActionType.SUGGEST_ALTERNATIVE,
    ),
    FrictionType.PROMOTION_FAILED: (
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.APPLY_PROMOTION,
    ),
    FrictionType.PAYMENT_DECLINED: (
        ActionType.OFFER_ALTERNATE_PAYMENT,
        ActionType.SPLIT_PAYMENT,
        ActionType.RETRY_PAYMENT,
    ),
    FrictionType.CART_ABANDONED: (
        ActionType.RECOMMEND_PRODUCTS,
        ActionType.APPLY_PROMOTION,
    ),
    FrictionType.CHECKOUT_ERROR: (ActionType.ANSWER_PRODUCT_QUESTION,),
    FrictionType.REPEATED_FAILURE: (ActionType.ANSWER_PRODUCT_QUESTION,),
    FrictionType.OTHER: (ActionType.ANSWER_PRODUCT_QUESTION,),
}

_FALLBACK_ASSISTANCE: tuple[ActionType, ...] = (
    ActionType.ANSWER_PRODUCT_QUESTION,
    ActionType.RECOMMEND_PRODUCTS,
)


@dataclass
class Reasoning:
    """One turn's output.

    used_model is recorded and surfaced in the console, so nobody has to guess
    whether a given case was reasoned about or fell back to rules.
    """

    actions: list[ProposedAction]
    diagnosis: Diagnosis | None
    reply: str | None
    used_model: bool
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    fallback_reason: str | None = None


class ReasoningService:
    """Proposes actions. Never decides, never executes."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client
        self._enabled = client is not None

    @classmethod
    def from_env(cls) -> ReasoningService:
        """Build from environment, or build disabled if no key is configured.

        A missing key is not an error. The engine runs on rule-based proposals
        without one, which is how it worked before the model existed and how it
        keeps working if a key is revoked.
        """
        if not LLMConfig.available():
            logger.info("no model key configured; reasoning will use rules")
            return cls(None)
        try:
            return cls(LLMClient())
        except LLMUnavailable as exc:
            logger.warning("model client unavailable: %s", exc)
            return cls(None)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def reason(
        self,
        *,
        friction: FrictionType | None,
        message: str | None = None,
        query: str | None = None,
        products: list[Product] | None = None,
        cart: Cart | None = None,
        order: Order | None = None,
        catalog_sample: list[Product] | None = None,
        history: list[str] | None = None,
        recorded_friction: list[str] | None = None,
    ) -> Reasoning:
        """Produce proposals for one situation, or one shopper message."""
        if self._client is None:
            return self._fallback(friction, "no model configured")

        context = build_context(
            friction=friction,
            message=message,
            query=query,
            products=products,
            cart=cart,
            order=order,
            catalog_sample=catalog_sample,
            history=history,
            recorded_friction=recorded_friction,
        )

        try:
            result = await self._client.complete(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": context}],
                tools=[propose_tool()],
                force_tool=True,
            )
        except LLMUnavailable as exc:
            logger.warning("reasoning fell back: %s", exc)
            return self._fallback(friction, str(exc))

        call = next(
            (c for c in result.tool_calls if c["name"] == "propose_actions"), None
        )
        if call is None:
            # The model replied in prose despite being told to call the tool. Open
            # models do this occasionally. Falling back is more honest than trying to
            # parse intent out of free text.
            return self._fallback(friction, "model did not call the tool")

        actions = self._parse(call["arguments"])
        if not actions:
            return self._fallback(friction, "model proposed nothing usable")

        args = call["arguments"]
        return Reasoning(
            actions=actions,
            diagnosis=Diagnosis(
                friction_type=friction or FrictionType.OTHER,
                cause=str(args.get("diagnosis", "")).strip() or "not stated",
                evidence=[str(e) for e in (args.get("evidence") or [])][:6],
                diagnosed_at=datetime.now(UTC),
            ),
            reply=str(args.get("reply", "")).strip() or None,
            used_model=True,
            model_name=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )

    # -- parsing -----------------------------------------------------------

    def _parse(self, args: dict) -> list[ProposedAction]:
        """Turn the tool arguments into proposals.

        Unknown action types are dropped rather than guessed at. If a model invents
        an action name, the honest response is to discard it - mapping it onto
        something plausible would be inventing intent on the model's behalf.
        """
        out: list[ProposedAction] = []

        for raw in args.get("actions") or []:
            name = str(raw.get("action_type", "")).strip()
            try:
                action_type = ActionType(name)
            except ValueError:
                logger.warning("model proposed unknown action %r", name)
                continue

            # The schema's enum should already prevent this, but enforce it here too.
            # Schema and parser disagreeing about what is proposable is a bug
            # whichever way it is resolved, and this is the side that runs even if a
            # provider ignores the enum.
            if action_type not in PROPOSABLE:
                logger.warning(
                    "model proposed a non-proposable action %r; discarded", name
                )
                continue

            # The model occasionally proposes the same action twice with different
            # rationales. Keep the first - a duplicate is not a second option, and
            # showing it twice makes the engine look like it cannot count.
            if any(a.action_type is action_type for a in out):
                continue

            parameters: dict[str, object] = {}
            if q := raw.get("search_query"):
                parameters["query"] = str(q)
            if pid := raw.get("product_id"):
                parameters["product_id"] = str(pid)
            if vid := raw.get("variant_id"):
                parameters["variant_id"] = str(vid)
            if code := raw.get("code"):
                parameters["code"] = str(code).upper()
            if (qty := raw.get("quantity")) and isinstance(qty, int) and qty > 0:
                parameters["quantity"] = qty
            confidence = raw.get("confidence")
            out.append(
                ProposedAction(
                    action_type=action_type,
                    # The engine decides which operation an action targets, not the
                    # model. One less thing that can be got wrong.
                    operation=operation_for(action_type),
                    parameters=parameters,
                    rationale=str(raw.get("rationale", "")).strip() or None,
                    confidence=(
                        float(confidence)
                        if isinstance(confidence, (int, float))
                        and 0.0 <= float(confidence) <= 1.0
                        else None
                    ),
                )
            )
        return out

    def _fallback(self, friction: FrictionType | None, reason: str) -> Reasoning:
        """Rule-based proposals. Identical shape to the model's output."""
        types = (
            _FALLBACK.get(friction, _FALLBACK_ASSISTANCE)
            if friction
            else _FALLBACK_ASSISTANCE
        )
        return Reasoning(
            actions=[
                ProposedAction(
                    action_type=t,
                    operation=operation_for(t),
                    rationale="proposed by rules; the model was not used",
                    confidence=round(0.7 - 0.1 * i, 2),
                )
                for i, t in enumerate(types)
            ],
            diagnosis=None,
            reply=None,
            used_model=False,
            fallback_reason=reason,
        )