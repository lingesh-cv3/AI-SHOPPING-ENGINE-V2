"""Response generation for Stage 1.

THE MOST IMPORTANT FILE IN THIS PACKAGE, per the approved spec: this
function's signature is the structural enforcement of "no shopper-facing
response may be generated from the pre-fetch Understanding model response."

`respond()` takes only:
  - `evidence`  (engine.shopper_v2.evidence.TurnEvidence - built AFTER retrieval)
  - `products`  (the real, adapter-fetched Product objects)
  - `state`     (the current, already-updated ShoppingStateV2)

It has no parameter through which a pre-fetch model reply could arrive, by
construction - `engine/shopper_v2/understanding.py::understand` never
returns one in the first place (see that module's docstring), so there is
nothing to pass here even if a future edit tried.
"""

from __future__ import annotations

from engine.execution import mission_ranking
from engine.reasoning.service import ReasoningService
from shared.models import Product

from .evidence import TurnEvidence
from .state import ShoppingStateV2


async def respond(
    *,
    reasoning: ReasoningService,
    message: str,
    evidence: TurnEvidence,
    products: list[Product],
    state: ShoppingStateV2,
    memory_facts: list[dict] | None = None,
) -> tuple[str, bool]:
    """Return (reply_text, used_model). Ordering is enforced by the caller
    (`orchestrator.py`) never invoking this before evidence/products exist -
    this function has no other way to be called."""
    verified = [p for e, p in _pair(evidence, products) if e.relevance_verified]

    if evidence.no_match_reason == "platform_unavailable":
        return (
            "I couldn't reach the store's catalog just now - please try again "
            "in a moment.",
            False,
        )

    if not verified:
        return (_honest_no_match(evidence, state), False)

    # Reuse the EXISTING post-execution synthesis pass - the same one V1
    # already uses for COMPARE_PRODUCTS/RECOMMEND_PRODUCTS
    # (`ReasoningService.synthesize`) - passed only the verified products
    # and current state, never the pre-fetch reply.
    items = [
        {
            "product_id": p.product_id,
            "title": p.title,
            "price": str(p.price) if p.price is not None else None,
            "description": p.description,
        }
        for p in verified
    ]
    shopping_state_dict = state.as_prompt_dict()
    synthesis = await reasoning.synthesize(
        kind="discovery",
        message=message,
        items=items,
        shopping_state=shopping_state_dict,
    )
    fallback = _deterministic_summary(verified, shopping_state_dict)
    if not synthesis.text or _claims_a_cart_action(synthesis.text):
        return (fallback, False)

    return _honest_discovery_reply(
        cited_reason=synthesis.cited_reason,
        shopping_state=shopping_state_dict,
        memory_facts=memory_facts,
        fallback=fallback,
        used_model=synthesis.used_model,
    )


#: Live-reproduced finding, Phase 4: the discovery synthesis pass
#: (`ReasoningService.synthesize(kind="discovery", ...)`) is asked only to
#: describe real, verified products - it is never told the DISCOVER
#: capability cannot mutate a cart, and nothing before this point checked
#: whether it invented such a claim anyway. Reproduced live: "Add that one
#: to my cart" was misrouted to DISCOVER (the model did not propose
#: ADD_TO_CART/PROPOSE_CART_CHANGE that turn) and the synthesized reply
#: said "I've added them to your cart as requested" - a fabricated claim
#: on a turn that never called `cart_proposal.py`, `decision`, `gate`, or
#: `execution` at all. This is exactly the defect this whole package
#: exists to prevent ("never let an LLM-generated claim become
#: authoritative merely because it sounds plausible"), now caught on the
#: DISCOVER path specifically. A deterministic, closed-class check - not a
#: prompt change (this synthesis call is shared with V1's own discovery/
#: recommendation path, and `engine/reasoning/` is left untouched here;
#: only this V2-owned response layer is hardened).
_CART_CLAIM_PHRASES = (
    "added to your cart", "added it to your cart", "added them to your cart",
    "i've added", "i have added", "added to cart",
    "removed from your cart", "removed it from your cart",
    "cleared your cart", "updated your cart",
    "placed your order", "completed your purchase", "checked out",
)


def _claims_a_cart_action(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _CART_CLAIM_PHRASES)


#: Live-reproduced finding (Shopper V2, trade-off reasoning slice,
#: 2026-09): `respond()`'s discovery synthesis has no equivalent guard
#: against a fabricated PERSONALIZATION claim - only the cart-claim guard
#: above. Reproduced live: a turn with `ShoppingStateV2.priorities == []`
#: (the model captured no priority that turn) nonetheless returned "This
#: option perfectly aligns with your priority for lightweight footwear" -
#: an attribution with nothing in state to back it. V1's own
#: `engine/api/chat.py::_honest_synthesis` closed the identical defect
#: after 17 rounds of adversarial hardening (see that function's own
#: docstring for the full history) - the end state there is structural,
#: not a phrase blocklist: the model's free text is never trusted to make
#: a personalization claim at all. Either it names a real stored term
#: (`cited_reason`, validated against `mission_ranking.stated_terms`) and
#: this module writes the attribution clause itself from that real term,
#: or no valid citation exists and the plain, fact-only `fallback` is used
#: - never the model's own uncited prose. Reused here as its own copy
#: rather than imported from `engine.api.chat`, which V2 never imports (see
#: this package's own architecture boundary, `orchestrator.py`'s docstring).
#: `compare.py`/`tradeoff.py`'s OWN near-identical duplication (each
#: hand-rolling the same `ProductEvidence` shape) was a genuine, unintended
#: copy rather than a deliberate cross-module boundary, and has since been
#: consolidated into `evidence.already_verified_evidence`/`product_as_dict` -
#: this one is intentional, that one was not.
_ATTRIBUTION_TEMPLATES = [
    "Since {reason} matters to you, ",
    "Given your focus on {reason}, ",
    "With {reason} being what you care about here, ",
]
#: A durable, cross-visit preference gets its own template set - never
#: silently phrased as if it were said THIS turn (that would itself be a
#: subtler version of the exact fabrication defect this file exists to
#: close: implying evidence for a claim - "you said this just now" - that
#: isn't real). A shopper can tell the difference between "you just told me"
#: and "you've told us before," and only the second is true here.
_MEMORY_ATTRIBUTION_TEMPLATES = [
    "Since you've told us before that {reason} matters to you, ",
    "Going by what you've said you prefer ({reason}), ",
    "Remembering that you like {reason}, ",
]
_MAX_CITED_REASON_WORDS = 8


def _deterministic_personalized_reply(
    cited_reason: str, fallback: str, *, from_memory: bool = False
) -> str:
    """Build the personalized clause ourselves from a real, already-
    validated stored term - never the model's own free text. Identical
    shape to `engine/api/chat.py::_deterministic_personalized_reply`, now
    also able to honestly attribute to a durable memory term rather than
    only a current-session one (see `from_memory`)."""
    if len(cited_reason.split()) > _MAX_CITED_REASON_WORDS:
        return fallback
    templates = _MEMORY_ATTRIBUTION_TEMPLATES if from_memory else _ATTRIBUTION_TEMPLATES
    template = templates[hash(cited_reason) % len(templates)]
    clause = template.format(reason=cited_reason)
    return clause + fallback[0].lower() + fallback[1:] if fallback else clause.rstrip()


def _honest_discovery_reply(
    *,
    cited_reason: str | None,
    shopping_state: dict,
    memory_facts: list[dict] | None,
    fallback: str,
    used_model: bool,
) -> tuple[str, bool]:
    """The structural gate: a personalization claim reaches the shopper
    only when it names a real, currently-stored OR durably-remembered term -
    never the model's own uncited prose, whatever it says. See this
    module's own comment above for the live reproduction this closes, and
    `engine/api/chat.py::_honest_synthesis` for the full adversarial
    history this mirrors.

    Current-session state is checked first and wins on a tie (matches
    `mission_ranking.rank_and_filter`'s own precedence: current explicit
    intent always outranks older stored preference) - a durable memory
    term is only ever the fallback source, never blended with or allowed
    to outrank a real current-turn citation."""
    if cited_reason:
        terms = mission_ranking.stated_terms(shopping_state)
        matched_term = mission_ranking.matching_stated_term(cited_reason, terms)
        if matched_term is not None:
            return (_deterministic_personalized_reply(matched_term, fallback), used_model)

        memory_terms = mission_ranking.durable_preference_terms(memory_facts)
        matched_memory_term = mission_ranking.matching_stated_term(cited_reason, memory_terms)
        if matched_memory_term is not None:
            return (
                _deterministic_personalized_reply(
                    matched_memory_term, fallback, from_memory=True
                ),
                used_model,
            )
    # No valid citation - the model's free text is never trusted to make
    # (or avoid making) a personalization claim, whatever it says; the
    # plain, fact-only fallback is used unconditionally. Matches
    # `_honest_synthesis`'s own final, most-hardened state exactly (see
    # its SEVENTH-gap note): a real trade-off (fewer natural-sounding
    # replies) accepted deliberately in exchange for zero remaining path
    # where an uncited claim reaches a shopper as fact.
    return (fallback, False)


def _pair(evidence: TurnEvidence, products: list[Product]):
    by_id = {p.product_id: p for p in products}
    for pe in evidence.products:
        product = by_id.get(pe.product_id)
        if product is not None:
            yield pe, product


def _honest_no_match(evidence: TurnEvidence, state: ShoppingStateV2) -> str:
    if evidence.no_match_reason == "no_candidates":
        if evidence.shop_carries_category is False:
            return "I couldn't find anything like that here - this store doesn't seem to carry it."
        return "I couldn't find anything matching that search right now."
    if evidence.no_match_reason == "no_relevant_match":
        if state.budget_amount:
            return (
                "I found some results, but none that actually match what you're "
                "looking for within your stated requirements - nothing here is a "
                "confident match, so I'd rather say that than guess."
            )
        return (
            "I found some results, but none of them are a confident match for "
            "what you asked - I don't want to show you something that only "
            "shares a word with your request."
        )
    return "I couldn't confirm a good match for that."


#: Grounded, deterministic phrasing - never model text, so it carries none
#: of the fabrication risk `_honest_discovery_reply` exists to gate. This
#: is the string that actually reaches a shopper on the large majority of
#: turns (the synthesis model's text is discarded whenever it lacks a
#: valid citation - see that function's own SEVENTH-gap note), so its own
#: robotic, one-shape-forever phrasing ("I found N match(es): ...") was
#: itself a real, reported "feels like the old chatbot" complaint - not a
#: model problem, a template problem. Every variant below states only
#: facts already computed by `evidence.py`/`mission_ranking.py` (the
#: count, the titles, whether a stated budget was honored) - nothing new
#: is asserted, only phrased more like a person than a search result line.
def _deterministic_summary(verified: list[Product], shopping_state: dict | None = None) -> str:
    names = [p.title for p in verified[:4]]
    count = len(verified)
    budget_honored = bool((shopping_state or {}).get("budget_amount")) and all(
        p.price is not None for p in verified[:4]
    )
    named = _join_naturally(names)

    if count == 1:
        if budget_honored:
            return f"{named} - and it's within what you said you'd like to spend."
        return f"I found one that fits: {named}."

    variants = [
        f"Here's what I found: {named}.",
        f"A few options that fit: {named}.",
        f"I found {count} that could work: {named}.",
    ]
    if budget_honored:
        variants.append(f"{named} - all within your budget.")
    return variants[hash(named) % len(variants)]


def _join_naturally(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"
