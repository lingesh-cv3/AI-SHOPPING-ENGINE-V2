"""Rev. 2 architecture (2026-09): general question-answering capability.

Closes the gap named in this session's own root-cause report: every
capability before this one always produces "show/rank products" replies,
never a reply whose SHAPE matches what the shopper actually asked
(a relationship, an explanation, a single recommendation, more
alternatives). This module handles every `question_type` other than
"retrieve"/"state_change" (see `InterpretationResult.question_type`'s own
docstring) with ONE general mechanism, not a handler per example phrase:

    resolve the real entities the question needs (one or two, depending on
    shape - already done by `verification.py::verify()`, including its
    generalized, catalog-agnostic attribute lookup, `attributes.py`)
        -> build a GroundedFacts bag of ONLY the real values/reasons that
           exist for them
        -> compose a reply whose text is built DIRECTLY from those real
           values (deterministic templates, not a second, independent
           model guess at the numbers) - this is what guarantees the
           reply actually states the relationship/reason/recommendation
           rather than merely describing that one could be computed.

Never imports engine.decision/risk/execution mutation paths - read-only,
matching compare.py/compatibility.py/tradeoff.py's own discipline. Never
mutates the cart, never proposes an action - a question answered here is
exactly as inert as a discovery listing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine.execution import mission_ranking
from shared.models import Product

from .attributes import ATTRIBUTE_LABELS, format_attribute_value, get_attribute_value
from .evidence import ProductEvidence, TurnEvidence, product_as_dict
from .state import ShoppingStateV2
from .verification import ResolvedReference, VerifiedInterpretation

logger = logging.getLogger(__name__)


@dataclass
class GroundedEntity:
    """One real, resolved side of a question - a label for what it was
    called (never invented; either the topic's own real label, from the
    per-turn message, or the product's own title), its real product(s),
    and whatever real attribute value/reasoning grounds why it was
    picked. Every field here traces back to a real `Product` or a real
    ranking result - nothing is synthesized."""

    label: str
    products: list[Product]
    attribute: str | None = None
    attribute_value: float | None = None
    fit_reasons: list[str] = field(default_factory=list)

    @property
    def top(self) -> Product | None:
        return self.products[0] if self.products else None


def _entity_from_resolved(resolved: ResolvedReference | None) -> GroundedEntity | None:
    """Builds a `GroundedEntity` entirely from what `verify()` already
    resolved - `candidates_considered` already holds the real `Product`
    objects for the scope that was checked, so this never needs a
    separately-fetched products map."""
    if resolved is None:
        return None
    products_by_id = {p.product_id: p for p in resolved.candidates_considered}
    ids = resolved.product_ids or [p.product_id for p in resolved.candidates_considered[:1]]
    products = [products_by_id[pid] for pid in ids if pid in products_by_id]
    if not products:
        return None
    attribute, value = resolved.resolved_attribute or (None, None)
    label = products[0].title if len(products) == 1 else f"{len(products)} options"
    return GroundedEntity(label=label, products=products, attribute=attribute, attribute_value=value)


def _evidence_for(products: list[Product], tag: str) -> TurnEvidence:
    return TurnEvidence(
        query=None,
        products=[
            ProductEvidence(
                product_id=p.product_id, exists=True, hard_constraints_passed=True,
                relevance_verified=True, relevance_score=0, weak_match=False,
                evidence_fields=[tag],
            )
            for p in products
        ],
    )


def _currency_of(products: list[Product]) -> str | None:
    for p in products:
        if p.price is not None:
            return p.price.currency
    return None


#: "Lower is better" for price - a real shopper never means "the most
#: expensive one" by "cheaper." "Higher is better" for rating. Purely a
#: fact about what each real, existing attribute MEANS, never a per-word
#: keyword rule about shopper phrasing - extending to a third real
#: attribute later is a one-line addition here, same as `attributes.py`.
_ATTRIBUTE_BETTER_DIRECTION: dict[str, str] = {"price": "min", "rating": "max"}
_ATTRIBUTE_WIN_WORD: dict[str, str] = {"price": "cheaper", "rating": "better rated"}


def _relationship_reply(a: GroundedEntity, b: GroundedEntity, currency: str | None) -> str:
    """Deterministic, grounded-by-construction: states which of the two
    real entities wins on the real attribute value each resolved to, and
    both real values - never a generic "here's what I found" that leaves
    the actual relationship for the shopper to work out themselves."""
    field_name = a.attribute or b.attribute
    direction = _ATTRIBUTE_BETTER_DIRECTION.get(field_name, "min")
    win_word = _ATTRIBUTE_WIN_WORD.get(field_name, "the better match")
    a_val, b_val = a.attribute_value, b.attribute_value
    winner, loser = (a, b) if (a_val <= b_val if direction == "min" else a_val >= b_val) else (b, a)
    winner_val = winner.attribute_value
    loser_val = loser.attribute_value
    label = ATTRIBUTE_LABELS.get(field_name, field_name)
    winner_str = format_attribute_value(field_name, winner_val, currency if field_name == "price" else None)
    loser_str = format_attribute_value(field_name, loser_val, currency if field_name == "price" else None)
    return (
        f"The {winner.label} is {win_word} than the {loser.label} - "
        f"{label} {winner_str} compared with {loser_str}."
    )


async def run_relationship_or_compound(
    *, verified: VerifiedInterpretation, message: str = "",
    reasoning=None, state: ShoppingStateV2 | None = None,
) -> tuple[str | None, TurnEvidence, list[Product], dict | None]:
    """`question_type in ("relationship", "compound_comparison")`. Both
    sides must have resolved to a real attribute value on exactly one real
    product each - anything short of that is named honestly, with a
    `pending_question` payload describing exactly what's still missing so
    a correction on a later turn can be replayed against it (see
    `graph.py`'s STATE_CHANGE handling).

    Production architecture (2026-09), Phase 5: an attribute-unavailable
    side is no longer an immediate decline. `attributes.py` only resolves
    a LITERAL field (price/rating/whatever this catalog has a real column
    for) - a USE-CASE question ("which is better for long runs?") has no
    literal field to look up, but is not therefore unanswerable: the real
    candidate products' own title/description text may genuinely support
    a grounded answer. One bounded, grounded synthesis attempt is made
    (`reasoning.synthesize`, the SAME call `compare.py` already uses,
    validated by the SAME shared `grounded_reply_or_fallback` gate - never
    a second, differently-hardened mechanism) before falling back to the
    honest "I don't have that attribute data" decline. `reasoning=None`
    (a caller with no reasoning service, or a genuinely non-shopping
    context) skips straight to the existing honest decline - this is
    additive, never a regression for a caller that doesn't pass it."""
    primary = verified.resolved_reference
    secondary = verified.resolved_secondary
    if primary is not None and primary.attribute_unavailable:
        grounded = await _try_grounded_use_case_reply(
            candidates=primary.candidates_considered, message=message,
            reasoning=reasoning, state=state,
        )
        if grounded is not None:
            text, products_shown, used_model = grounded
            return (text, _evidence_for(products_shown, "use_case_grounded"), products_shown, None)
        return (
            f"I don't have {primary.candidates_considered[0].title if primary.candidates_considered else 'that'} "
            "attribute data for these products - I can compare by price or rating instead, if that helps.",
            TurnEvidence(query=None, products=[]), [], None,
        )
    if secondary is not None and secondary.attribute_unavailable:
        return (
            "I can find one side of that, but I don't have that attribute's real data to resolve the other - "
            "want me to compare by price or rating instead?",
            TurnEvidence(query=None, products=[]), [], None,
        )
    entity_a = _entity_from_resolved(primary)
    entity_b = _entity_from_resolved(secondary)
    if entity_a is None or entity_b is None or entity_a.attribute is None or entity_b.attribute is None:
        # Stabilization fix (2026-09): a correction on the NEXT turn used
        # to re-run the ENTIRE comparison from a combined message,
        # requiring the model to resolve BOTH sides correctly again in one
        # go - measured unreliable (the same side that already resolved
        # correctly could get lost the second time too). Now keeps
        # whichever side DID resolve, as real, already-verified data (real
        # product ids + the real attribute/value already computed), so a
        # correction only ever needs to resolve the ONE missing side -
        # never re-asks the model to redo work it already did correctly.
        resolved_side = None
        if entity_a is not None and entity_a.attribute is not None:
            resolved_side = {
                "product_ids": [p.product_id for p in entity_a.products],
                "label": entity_a.label, "attribute": entity_a.attribute,
                "attribute_value": entity_a.attribute_value,
            }
        elif entity_b is not None and entity_b.attribute is not None:
            resolved_side = {
                "product_ids": [p.product_id for p in entity_b.products],
                "label": entity_b.label, "attribute": entity_b.attribute,
                "attribute_value": entity_b.attribute_value,
            }
        missing = "the second thing to compare" if entity_a is not None else "what to compare"
        pending = {"missing": missing, "resolved_side": resolved_side}
        return (
            f"I can see one side of that, but I need {missing} to answer - which other product or group did you mean?",
            TurnEvidence(query=None, products=[]), [], pending,
        )
    currency = _currency_of(entity_a.products + entity_b.products)
    reply = _relationship_reply(entity_a, entity_b, currency)
    products_shown = entity_a.products + entity_b.products
    return reply, _evidence_for(products_shown, "relationship_resolved"), products_shown, None


async def _try_grounded_use_case_reply(
    *, candidates: list[Product], message: str, reasoning, state: ShoppingStateV2 | None,
) -> tuple[str, list[Product], bool] | None:
    """Production architecture (2026-09), Phase 5 - one bounded, grounded
    attempt to answer a USE-CASE question ("better for long runs", "which
    is more durable") over real candidates, when no literal attribute
    field exists for it. Returns `None` (never a guess) when reasoning is
    unavailable, when there are not at least two real candidates to
    reason over, or when the synthesis call itself produces nothing
    grounded - the caller's own honest decline applies in every one of
    those cases, unchanged."""
    if reasoning is None or not getattr(reasoning, "enabled", False):
        return None
    if len(candidates) < 2:
        return None
    from .respond import _claims_a_cart_action, grounded_reply_or_fallback

    items = [
        {
            "product_id": p.product_id, "title": p.title,
            "price": str(p.price) if p.price is not None else None,
            "availability": str(p.availability), "description": p.description,
        }
        for p in candidates
    ]
    synthesis = await reasoning.synthesize(
        kind="comparison", message=message, items=items,
        shopping_state=(state.as_prompt_dict() if state is not None else None),
        stream_policy="cited_reason_check",
    )
    if not synthesis.text or _claims_a_cart_action(synthesis.text):
        return None
    text, used_model = grounded_reply_or_fallback(
        cited_reason=synthesis.cited_reason, text=synthesis.text, products=candidates,
        fallback="", used_model=synthesis.used_model,
    )
    if not text:
        return None
    return (text, candidates, used_model)


def entity_from_stored_side(stored: dict, products_by_id: dict[str, Product]) -> GroundedEntity | None:
    """Rebuilds a `GroundedEntity` from a `pending_question`'s stored
    `resolved_side` (real ids/label/attribute/value only - see
    `run_relationship_or_compound`'s own docstring) plus a fresh product
    fetch. `None` when the stored products are no longer fetchable (a rare
    catalog-consistency edge case, degrades honestly rather than raising)."""
    products = [products_by_id[pid] for pid in stored.get("product_ids", []) if pid in products_by_id]
    if not products:
        return None
    return GroundedEntity(
        label=stored.get("label") or products[0].title, products=products,
        attribute=stored.get("attribute"), attribute_value=stored.get("attribute_value"),
    )


def _topic_label(topic, products_by_id: dict[str, Product]) -> str:
    if topic.label:
        return topic.label
    titles = [products_by_id[pid].title for pid in topic.product_ids if pid in products_by_id]
    return ", ".join(titles[:2]) if titles else "the other one"


def deterministic_relationship_fallback(
    context, products_by_id: dict[str, Product],
) -> tuple[GroundedEntity, GroundedEntity] | None:
    """Last-resort, no-model-call deterministic backstop for "which is
    cheaper?"-shaped questions - measured live: the model's own claim can
    fail to resolve EITHER side at all for a message with no explicit
    product noun ("which is cheaper?" alone, relying entirely on
    conversation memory of two prior topics). Uses the real topic
    structure directly, defaulting to price/min - the same default
    `verification.py::resolve_reference`'s own superlative branch already
    uses for a bare comparative with no named attribute. Handles BOTH
    real shapes measured live: an active topic vs. the most recently
    paused one, AND the legacy-bridge self-heal case
    (`conversation_context.py::build_context`'s own docstring - when
    Focus Stack never tracked a push, every real topic surfaces as
    PAUSED, none marked active) by falling back to the two most recent
    paused topics instead. A synthetic topic's own blank `.label` is
    replaced with a real, product-title-derived one (`_topic_label`,
    mirroring `verification.py::_effective_label`'s identical fallback) -
    never the empty string. Never invents a topic or a value; `None` when
    there genuinely aren't two real topics with real priced products to
    compare."""
    if context is None:
        return None
    if context.active_topic is not None and context.paused_topics:
        topic_a, topic_b = context.active_topic, context.paused_topics[0]
    elif context.active_topic is None and len(context.paused_topics) >= 2:
        topic_a, topic_b = context.paused_topics[0], context.paused_topics[1]
    else:
        return None
    products_a = [products_by_id[pid] for pid in topic_a.product_ids if pid in products_by_id]
    products_b = [products_by_id[pid] for pid in topic_b.product_ids if pid in products_by_id]
    a = resolve_missing_side(products_a, "price")
    b = resolve_missing_side(products_b, "price")
    if a is None or b is None:
        return None
    a.label = _topic_label(topic_a, products_by_id)
    b.label = _topic_label(topic_b, products_by_id)
    return a, b


def resolve_missing_side(
    candidates: list[Product], attribute: str, direction: str = "min",
) -> GroundedEntity | None:
    """Stabilization fix (2026-09): resolves the SECOND side of a
    relationship after a correction, deterministically - never asks the
    model to restate the attribute a second time (a correction like
    "Actually I meant the shoes" names the TOPIC, not the attribute; the
    attribute is already known from the side that resolved cleanly on the
    FIRST attempt, stored in `pending_question`). Picks the real,
    already-fetched candidate with the best real value for the SAME
    attribute the first side was compared on - the same deterministic
    picking logic `verification.py::resolve_reference`'s own superlative
    branch already uses, applied here without a second model call."""
    valued = [p for p in candidates if get_attribute_value(p, attribute) is not None]
    if not valued:
        return None
    pick = (max if direction == "max" else min)(valued, key=lambda p: get_attribute_value(p, attribute))
    return GroundedEntity(
        label=pick.title, products=[pick], attribute=attribute,
        attribute_value=get_attribute_value(pick, attribute),
    )


def run_explanation(
    *, message: str, products: list[Product], state: ShoppingStateV2, scope_label: str,
) -> tuple[str, TurnEvidence, list[Product]]:
    """`question_type == "explanation"` - "why this/why did you pick
    that." Re-derives a REAL grounded reason NOW, from the same
    deterministic `mission_ranking.rank_and_filter` every discovery/trade-
    off reply already trusts, over the real products currently in view
    and the shopper's own current, real stated priorities/constraints -
    never a persisted, possibly-stale copy of an earlier turn's own reply
    text. When nothing in the current state actually distinguishes one
    product from another (no priorities/constraints stated), says so
    honestly rather than inventing a reason."""
    if not products:
        return (
            "I don't have anything specific in view to explain right now - "
            "ask me to show something first.",
            TurnEvidence(query=None, products=[]), [],
        )
    product_dicts = [product_as_dict(p) for p in products]
    shortlist, fit_reasons = mission_ranking.rank_and_filter(
        product_dicts, state.as_prompt_dict(), limit=len(product_dicts),
    )
    if not shortlist:
        return (
            f"{scope_label} matched what you asked for, but nothing about your current priorities "
            "ranked one above another - they're shown in the order found.",
            _evidence_for(products, "explanation_no_ranking"), products,
        )
    top_id = shortlist[0]["product_id"]
    top_product = next((p for p in products if p.product_id == top_id), products[0])
    reasons = fit_reasons.get(top_id) or []
    if reasons:
        reason_text = "; ".join(reasons[:3])
        reply = f"{top_product.title} is shown first because {reason_text}."
    else:
        reply = f"{top_product.title} matched {scope_label} - nothing you've stated so far ranks it above the others."
    return reply, _evidence_for([top_product], "explanation_grounded"), [top_product]


#: Deliberately structural, not phrase-specific: ANY digit in the message
#: is treated as "a hard number was stated," regardless of what word
#: surrounds it ("under 5000", "budget of 3k", "5000 rupees or less") -
#: this never tries to parse the number itself (that stays
#: `delta.budget_amount`'s own, separately-verified job), it only detects
#: the STRUCTURAL fact that a number was said at all, so a recommendation
#: can never silently proceed as if nothing numeric had been mentioned.
_DIGIT_PATTERN = __import__("re").compile(r"\d")


def _budget_disclaimer_if_needed(message: str, budget_amount: str | None) -> str:
    if budget_amount or not _DIGIT_PATTERN.search(message):
        return ""
    return " (I couldn't confirm a specific budget from what you said, so this isn't filtered by price.)"


def run_recommendation(
    *, products: list[Product], state: ShoppingStateV2, budget_amount: str | None,
    scope_label: str, message: str = "",
) -> tuple[str, TurnEvidence, list[Product]]:
    """`question_type == "recommendation"` - "what's the best one under X."
    Filters to the real budget (when one was actually stated - never
    invented) and picks the top real, ranked candidate, stating why -
    same grounded-reasoning source as `run_explanation`. A budget that
    excludes everything is reported honestly, naming the real cheapest
    alternative instead of silently ignoring the constraint.

    Structural fix (2026-09): a stated hard constraint must never
    silently disappear. When the message plainly contains a number but
    `budget_amount` never made it into state (the same already-documented
    per-turn budget-capture reliability gap this project has elsewhere),
    the reply says so explicitly rather than recommending an item that
    may be well outside what the shopper actually asked for with no hint
    anything was missed."""
    disclaimer = _budget_disclaimer_if_needed(message, budget_amount)
    if not products:
        return (
            "I don't have anything in view to recommend from yet - ask me to show something first.",
            TurnEvidence(query=None, products=[]), [],
        )
    in_budget = products
    if budget_amount:
        try:
            limit = float(budget_amount)
            in_budget = [p for p in products if p.price is not None and float(p.price.amount) <= limit]
        except (TypeError, ValueError):
            in_budget = products
    if budget_amount and not in_budget:
        cheapest = min((p for p in products if p.price is not None), key=lambda p: p.price.amount, default=None)
        if cheapest is None:
            return (
                f"Nothing in {scope_label} is under {budget_amount} - I don't have prices for these to check.",
                TurnEvidence(query=None, products=[]), [],
            )
        return (
            f"Nothing in {scope_label} is under {budget_amount} - the closest is {cheapest.title} "
            f"at {cheapest.price.amount} {cheapest.price.currency}.",
            _evidence_for([cheapest], "recommendation_over_budget"), [cheapest],
        )
    product_dicts = [product_as_dict(p) for p in in_budget]
    shortlist, fit_reasons = mission_ranking.rank_and_filter(
        product_dicts, state.as_prompt_dict(), limit=len(product_dicts),
    )
    if not shortlist:
        return (
            f"I don't have a confident best pick in {scope_label} right now.",
            TurnEvidence(query=None, products=[]), [],
        )
    top_id = shortlist[0]["product_id"]
    top_product = next((p for p in in_budget if p.product_id == top_id), in_budget[0])
    reasons = fit_reasons.get(top_id) or []
    price_bit = (
        f" at {top_product.price.amount} {top_product.price.currency}" if top_product.price else ""
    )
    if reasons:
        reply = f"{top_product.title}{price_bit} is the best option - {'; '.join(reasons[:2])}."
    else:
        reply = f"{top_product.title}{price_bit} is the best real match I have{' under ' + budget_amount if budget_amount else ''}."
    return reply + disclaimer, _evidence_for([top_product], "recommendation_grounded"), [top_product]


def run_alternatives(
    *, all_topic_products: list[Product], already_shown_ids: set[str], scope_label: str,
) -> tuple[str, TurnEvidence, list[Product]]:
    """`question_type == "alternatives"` - "what else do you have." Real,
    different products from the SAME already-established topic, excluding
    what's already been shown - never a re-search for the literal words
    "what else," and never a re-display of the same set."""
    remaining = [p for p in all_topic_products if p.product_id not in already_shown_ids]
    if not remaining:
        return (
            f"That's everything I have for {scope_label} right now.",
            TurnEvidence(query=None, products=[]), [],
        )
    shown = remaining[:9]
    names = ", ".join(p.title for p in shown)
    return (
        f"A few more from {scope_label}: {names}.",
        _evidence_for(shown, "alternatives"), shown,
    )
