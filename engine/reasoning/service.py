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
import math
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

#: Words that make "add/remove ... cart" a mutation, not a status question.
#: Checked first, and narrowly, because "cart" alone is not enough - "add
#: this to my cart" must never be answered as if it were a read.
_CART_MUTATION_WORDS = ("add", "remove", "delete", "clear", "empty", "change", "update")


def _looks_like_cart_question(message: str) -> bool:
    """A plain-text heuristic for the rules fallback only - the model gets a
    real prompt instruction instead. Deliberately narrow: it only has to
    catch the direct, common phrasings ("show cart", "what's in my cart",
    "what have I got") without misfiring on a mutation that happens to
    mention the cart too."""
    said = message.lower()
    if "cart" not in said and "basket" not in said and "bag" not in said:
        return False
    if any(w in said for w in _CART_MUTATION_WORDS):
        return False
    return True


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
    #: The provider's own retry hint when a rate limit forced the rule fallback,
    #: so the shopper can be told a real number rather than "a few seconds". Only
    #: set on the rate-limited fallback path; None everywhere else.
    retry_after_seconds: float | None = None


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
        skip_model: bool = False,
    ) -> Reasoning:
        """Produce proposals for one situation, or one shopper message."""
        # Asked not to wait, so do not.
        #
        # Set by a caller who cannot leave somebody hanging - a declined payment,
        # where the recovery options come from the capability table rather than
        # from judgement. The model would only have phrased them, and a fixed
        # sentence now beats a nicer one in thirty seconds.
        if skip_model:
            return self._fallback(friction, "caller asked not to wait", message=message)

        if self._client is None:
            return self._fallback(friction, "no model configured", message=message)

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
            return self._fallback(
                friction,
                str(exc),
                message=message,
                retry_after_seconds=exc.retry_after_seconds,
            )

        call = next(
            (c for c in result.tool_calls if c["name"] == "propose_actions"), None
        )
        if call is None:
            # The model replied in prose despite being told to call the tool. Open
            # models do this occasionally. Falling back is more honest than trying to
            # parse intent out of free text.
            return self._fallback(friction, "model did not call the tool", message=message)

        actions = self._parse(call["arguments"])
        if not actions:
            return self._fallback(
                friction, "model proposed nothing usable", message=message
            )

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
            if cwid := raw.get("compare_with_id"):
                parameters["compare_with_id"] = str(cwid)
            if oid := raw.get("order_id"):
                parameters["order_id"] = str(oid).strip()
            if code := raw.get("code"):
                parameters["code"] = str(code).upper()
            if (qty := raw.get("quantity")) and isinstance(qty, int) and qty > 0:
                parameters["quantity"] = qty
            # math.isfinite rejects NaN and +/-inf - a model is free to emit either
            # in a "number" field, and Decimal(str(nan)) raises InvalidOperation on
            # the first comparison downstream rather than filtering anything, which
            # would break the turn with a raw exception instead of a safe reply.
            if (
                (mx := raw.get("max_price")) is not None
                and isinstance(mx, (int, float))
                and math.isfinite(mx)
            ):
                parameters["max_price"] = mx
            if (
                (mn := raw.get("min_price")) is not None
                and isinstance(mn, (int, float))
                and math.isfinite(mn)
            ):
                parameters["min_price"] = mn
            if raw.get("top_rated") is True:
                parameters["top_rated"] = True
            if cat := raw.get("category"):
                parameters["category"] = str(cat)
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

    def _fallback(
        self,
        friction: FrictionType | None,
        reason: str,
        *,
        message: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> Reasoning:
        """Rule-based proposals. Identical shape to the model's output."""
        types = (
            _FALLBACK.get(friction, _FALLBACK_ASSISTANCE)
            if friction
            else _FALLBACK_ASSISTANCE
        )
        # A cart-status question ("show cart", "what's in my cart") has a
        # deterministic, correct answer that needs no judgement at all - it
        # is exactly the kind of thing rules should handle without the
        # model, not something to leave to whichever of the generic
        # assistance actions the fallback tuple happens to carry. Checked
        # here rather than added to _FALLBACK_ASSISTANCE outright, because
        # that tuple is used whenever friction is None regardless of what
        # was actually said - this only changes behaviour for a message
        # that is actually asking about the cart.
        if friction is None and message and _looks_like_cart_question(message):
            types = (ActionType.CHECK_CART_STATUS, *types)
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
            retry_after_seconds=retry_after_seconds,
        )