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
from .prompts import (
    PROPOSABLE,
    SYSTEM_PROMPT,
    _BEST_MATCH_SYSTEM_PROMPT,
    _RELEVANCE_SYSTEM_PROMPT,
    _SYNTHESIS_SYSTEM_PROMPT,
    _synthesis_prompt,
    best_match_prompt,
    best_match_tool,
    propose_tool,
    relevance_judgment_prompt,
    relevance_tool,
    synthesis_tool,
)

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
    #: Update to the current shopping mission, parsed straight from the same
    #: tool call - never a second model call. None on the rules fallback
    #: (nothing to parse) and whenever the model supplied nothing new this
    #: turn; the caller (engine/db/shopping_state.py::merge) treats an
    #: absent field as "no change", not "clear it".
    shopping_state: dict | None = None
    #: Durable facts the model flagged this turn, almost always empty. Only
    #: ever written to storage for a signed-in shopper - see chat.py.
    durable_facts: list[dict] | None = None


@dataclass
class Synthesis:
    """The result of the post-execution synthesis pass (see `synthesize`
    below) - a second, small model call made only after the authoritative
    commerce result already exists, so its text can actually reference that
    result instead of guessing at it.
    """

    text: str | None
    used_model: bool
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: The shopper's own stated phrase this reply claims to be personalized
    #: to, per the model's own `write_synthesis` tool call - `None` means the
    #: model made no personalization claim (or, on the plain-text fallback
    #: path below, that no structured claim exists to check at all). Checked
    #: by `engine/api/chat.py::_honest_synthesis` against real stored
    #: `shopping_state` terms - a structural fix, see `synthesis_tool()`'s
    #: own docstring for why the prior two rounds of prose-scanning were each
    #: defeated by adversarial rephrasing.
    cited_reason: str | None = None
    #: The model's own declared "does this reply personalize to something the
    #: shopper stated" flag, per `synthesis_tool()`'s now-required
    #: `personalizes` field. `None` means no structured claim was made at all
    #: (the plain-text fallback path, or a tool-call the model skipped) -
    #: `_honest_synthesis` treats that the same as `True`, fail-closed,
    #: precisely because "the model didn't say" must not become a free pass
    #: the way an unset optional field previously was (see `synthesis_tool()`'s
    #: docstring for the bypass this closes).
    personalizes: bool | None = None


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
        shopping_state: dict | None = None,
        memory_facts: list[dict] | None = None,
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
            shopping_state=shopping_state,
            memory_facts=memory_facts,
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
            shopping_state=self._parse_shopping_state(args.get("shopping_state")),
            durable_facts=self._parse_durable_facts(args.get("durable_facts")),
        )

    def _parse_shopping_state(self, raw: object) -> dict | None:
        """Only fields the model actually supplied this turn, validated the
        same defensive way the action parameters are - a NaN/inf budget must
        not be able to break a later Decimal comparison, the same class of
        bug already fixed for max_price/min_price above."""
        if not isinstance(raw, dict) or not raw:
            return None

        out: dict[str, object] = {}
        if goal := raw.get("goal"):
            out["goal"] = str(goal).strip()[:300]
        budget = raw.get("budget_amount")
        if isinstance(budget, (int, float)) and math.isfinite(budget) and budget > 0:
            out["budget_amount"] = budget
        if currency := raw.get("budget_currency"):
            out["budget_currency"] = str(currency).strip()[:8].upper()
        for field in (
            "constraints_add",
            "constraints_remove",
            "must_have_add",
            "must_have_remove",
            "nice_to_have_add",
            "nice_to_have_remove",
            "exclusions_add",
            "exclusions_remove",
            "priorities_add",
        ):
            values = raw.get(field)
            if isinstance(values, list) and values:
                out[field] = [str(v).strip()[:200] for v in values if str(v).strip()]
        status = raw.get("mission_status")
        if status in ("exploring", "comparing", "deciding", "done"):
            out["mission_status"] = status
        clears = raw.get("clear_fields")
        if isinstance(clears, list):
            valid = {"goal", "budget_amount", "budget_currency", "mission_status"}
            cleaned = [c for c in clears if c in valid]
            if cleaned:
                out["clear_fields"] = cleaned
        return out or None

    def _parse_durable_facts(self, raw: object) -> list[dict] | None:
        if not isinstance(raw, list) or not raw:
            return None
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "")).strip()[:120]
            value = str(item.get("value", "")).strip()[:400]
            if key and value:
                out.append({"key": key, "value": value})
        return out or None

    # -- post-execution synthesis -------------------------------------------

    async def synthesize(
        self,
        *,
        kind: str,
        message: str | None,
        items: list[dict],
        shopping_state: dict | None = None,
    ) -> Synthesis:
        """Write the shopper-facing sentence *after* the authoritative result
        already exists, so it can actually reference real fetched values
        instead of the pre-fetch guess `reason()` above produces.

        This is the fix for the reply-before-data constraint - but scoped
        deliberately, not a second call bolted onto every action type.
        `kind` is "comparison" (exactly two products, from COMPARE_PRODUCTS)
        or "discovery" (a curated set, from RECOMMEND_PRODUCTS/
        SUGGEST_ALTERNATIVE). Every other action already has a deterministic,
        engine-written reply (see engine/api/chat.py) that needs no model
        call at all, because it has nothing to synthesize - a cart total or
        an order status is a fact to state, not a judgement to explain.

        No tool schema is passed here on purpose: this call cannot propose
        an action, assert risk, or do anything but return text. It is
        strictly a narrower capability than `reason()`, not an equal one.
        """
        if self._client is None:
            return Synthesis(text=None, used_model=False)

        prompt = _synthesis_prompt(kind, message, items, shopping_state)
        try:
            result = await self._client.complete(
                system=_SYNTHESIS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                tools=[synthesis_tool()],
                force_tool=True,
            )
        except LLMUnavailable as exc:
            logger.info("synthesis pass unavailable, falling back: %s", exc)
            return Synthesis(text=None, used_model=False)

        call = next(
            (c for c in result.tool_calls if c["name"] == "write_synthesis"), None
        )
        if call is None:
            # The model answered in prose instead of calling the tool (open
            # models do this occasionally, same as `reason()` above). There is
            # no `cited_reason` to check in that case - treat it the same as
            # "no personalization claim was structurally made" rather than
            # inventing one, since `_honest_synthesis` already refuses to
            # trust free text it cannot verify.
            text = (result.text or "").strip() or None
            return Synthesis(
                text=text,
                used_model=text is not None,
                model_name=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cited_reason=None,
                personalizes=None,
            )

        args = call["arguments"] if isinstance(call.get("arguments"), dict) else {}
        text = str(args.get("reply", "")).strip() or None
        cited = args.get("cited_reason")
        cited_reason = str(cited).strip()[:300] if isinstance(cited, str) and cited.strip() else None
        personalizes_raw = args.get("personalizes")
        personalizes = personalizes_raw if isinstance(personalizes_raw, bool) else None
        return Synthesis(
            text=text,
            used_model=text is not None,
            model_name=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cited_reason=cited_reason,
            personalizes=personalizes,
        )

    # -- semantic relevance judgment -----------------------------------------

    async def judge_relevance(
        self, *, need: str, categories: list[str], sample_titles: list[str]
    ) -> bool | None:
        """Does this shop plausibly carry something matching `need`, based on
        real semantic/use-case fit rather than literal keyword overlap?

        Used only to gate `engine/execution/relevance.py::shop_carries`'s
        existing fallback decisions when the deterministic keyword/category
        check itself said "no" - never to invent a product, never to bypass
        the real adapter fetch, never to propose an action. This is strictly
        a narrower capability than `reason()`, the same guarantee
        `synthesize()` already carries: no tool schema field here can select
        a variant, assert risk, or reach execution - `relevance_tool()` has
        exactly one boolean field.

        Returns `None` (not `True` or `False`) when the model is unavailable
        or throttled - the caller must treat `None` the same as "could not
        confirm," never as an implicit yes, and fall back to whatever the
        deterministic check already found (the existing, safe, honest-decline
        behavior), the same `LLMUnavailable`-degrades-safely pattern
        `synthesize()` already established.
        """
        if self._client is None:
            return None
        prompt = relevance_judgment_prompt(need, categories, sample_titles)
        try:
            result = await self._client.complete(
                system=_RELEVANCE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                tools=[relevance_tool()],
                force_tool=True,
            )
        except LLMUnavailable as exc:
            logger.info("relevance judgment unavailable, degrading: %s", exc)
            return None
        call = next(
            (c for c in result.tool_calls if c["name"] == "judge_relevance"), None
        )
        if call is None:
            return None
        args = call["arguments"] if isinstance(call.get("arguments"), dict) else {}
        plausible = args.get("plausible")
        return plausible if isinstance(plausible, bool) else None

    async def judge_best_match(
        self, *, need: str, candidates: list[dict]
    ) -> str | None:
        """Which of these real, already-fetched candidates best matches
        `need`, when a literal keyword/stem search cannot rank them (a taste
        or use-case description sharing no literal word with any product
        text). TWENTIETH-gap fix - see `best_match_prompt`'s own docstring.

        Returns a `product_id` ONLY when it is verified to be one of the
        ids actually present in `candidates` - the model's raw output is
        never trusted as-is, the same discipline already used for
        `cited_reason`/category validation elsewhere in this codebase.
        Returns `None` (not a made-up id, not the first candidate as a
        guess) when the model says nothing stands out, when it names an id
        not in the real list, or when it is unavailable/throttled - the
        caller must treat `None` as "could not confirm a single best
        match," never as permission to pick one itself."""
        if self._client is None:
            return None
        valid_ids = {c["product_id"] for c in candidates}
        prompt = best_match_prompt(need, candidates)
        try:
            result = await self._client.complete(
                system=_BEST_MATCH_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                tools=[best_match_tool()],
                force_tool=True,
            )
        except LLMUnavailable as exc:
            logger.info("best-match judgment unavailable, degrading: %s", exc)
            return None
        call = next(
            (c for c in result.tool_calls if c["name"] == "judge_best_match"), None
        )
        if call is None:
            return None
        args = call["arguments"] if isinstance(call.get("arguments"), dict) else {}
        if args.get("matches") is not True:
            return None
        product_id = args.get("product_id")
        if not isinstance(product_id, str) or product_id not in valid_ids:
            logger.info(
                "best-match judge named an id not in the real candidate "
                "list (%r); discarding rather than trusting it",
                product_id,
            )
            return None
        return product_id

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