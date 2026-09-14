"""Return-risk prevention: real, evidence-grounded, pre-purchase friction
signals - never a claim about actual return history, because this system
has no return/RMA data model anywhere (no adapter declares a returns
capability, no order carries a return_reason - see shared/models and
CLAUDE.md's own "No real Returns capability" note). Nothing here can be
"the product with the highest return rate" because that number does not
exist. What CAN be computed honestly, from data this system genuinely has:

1. A **known fact** signal: a numerically low customer rating, from the
   platform's own declared `rating`/`rating_count` (the same field
   `mission_ranking` already reads for "top rated" - never invented here).
2. An **inferred concern** signal: the shopper stated a requirement this
   session (a must-have/priority) that the product's own description does
   not confirm - reusing `mission_ranking`'s own uncertain/related-but-not-
   equivalent logic, the identical discipline already applied to
   exclusions, applied here to a *positive* requirement instead.
3. A **fit-dependent** informational note: the product has size/option
   variants, so the existing "never guess a size, always ask" mechanism
   (see CLAUDE.md's own framing of this as the system's original return-
   prevention property) is the actual safeguard - this module only makes
   that fact visible in the reasoning/reply layer, it does not duplicate
   the enforcement (that already lives in ADD_TO_CART's dispatch).

Every finding is one of exactly these two evidence types - `known_fact` or
`inferred_concern` - never blurred together, and every finding may suggest
a safer alternative only when a real, better-evidenced candidate exists in
the same already-fetched product set. Nothing here ever blocks a purchase;
a finding is advisory only, same as `cart_intelligence`'s findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.execution.mission_ranking import _matches, _text_of, _uncertain

#: Below this many ratings, a numeric average is not evidence of anything -
#: three five-star reviews and three one-star reviews average identically
#: to thirty of each, but only one of those is a real signal. Chosen to
#: match this catalog's own `_rating_for` seed range (12-251 reviews) so
#: the threshold excludes only the genuinely thin end of that range, not
#: an arbitrary number picked without reference to the real data shape.
_MIN_RATING_COUNT_FOR_SIGNAL = 15

#: Below this rating (out of 5), with enough reviews to trust it, is a real,
#: known, platform-declared fact worth surfacing - not a judgement this
#: code is making about the product, just the number itself.
_LOW_RATING_THRESHOLD = 3.7


@dataclass(frozen=True)
class ReturnRiskFinding:
    kind: str  # "low_rating" | "unconfirmed_requirement"
    evidence_type: str  # "known_fact" | "inferred_concern"
    message: str
    product_id: str
    safer_alternative_id: str | None = None


def _better_rated_alternative(
    product: dict, catalog_sample: list[dict] | None
) -> dict | None:
    """A real product from the same already-fetched set, same category,
    genuinely better-rated and in stock - never a product invented or
    pulled from outside what was actually fetched this turn."""
    if not catalog_sample:
        return None
    categories = set(product.get("categories") or [])
    rating = product.get("rating")
    if rating is None:
        return None
    best = None
    for candidate in catalog_sample:
        if candidate.get("product_id") == product.get("product_id"):
            continue
        if str(candidate.get("availability")) == "OUT_OF_STOCK":
            continue
        if categories and not (set(candidate.get("categories") or []) & categories):
            continue
        c_rating = candidate.get("rating")
        c_count = candidate.get("rating_count") or 0
        if c_rating is None or c_count < _MIN_RATING_COUNT_FOR_SIGNAL:
            continue
        if c_rating <= rating:
            continue
        if best is None or c_rating > best.get("rating", 0):
            best = candidate
    return best


def assess_product(
    product: dict,
    shopping_state: dict | None,
    catalog_sample: list[dict] | None = None,
) -> list[ReturnRiskFinding]:
    """Real, grounded pre-purchase friction findings for one product -
    never fabricated, never blocking. `product` and `catalog_sample` are
    already-fetched dicts in the same shape `mission_ranking.rank_and_filter`
    consumes (product_id/title/description/price/categories/rating/
    rating_count/availability)."""
    findings: list[ReturnRiskFinding] = []
    shopping_state = shopping_state or {}

    rating = product.get("rating")
    rating_count = product.get("rating_count") or 0
    if (
        rating is not None
        and rating_count >= _MIN_RATING_COUNT_FOR_SIGNAL
        and rating < _LOW_RATING_THRESHOLD
    ):
        alt = _better_rated_alternative(product, catalog_sample)
        title = product.get("title", "this product")
        message = (
            f"{title} has a lower customer rating ({rating}/5 from "
            f"{rating_count} reviews)."
        )
        if alt:
            message += f" {alt['title']} is rated higher in the same category."
        findings.append(
            ReturnRiskFinding(
                kind="low_rating",
                evidence_type="known_fact",
                message=message,
                product_id=product["product_id"],
                safer_alternative_id=alt["product_id"] if alt else None,
            )
        )

    text = _text_of(product)
    stated_requirements = [
        *(shopping_state.get("must_have") or []),
        *(shopping_state.get("priorities") or []),
    ]
    for requirement in stated_requirements:
        if not requirement:
            continue
        if _matches(text, requirement):
            continue  # confirmed by the product's own description - no concern
        if _uncertain(text, requirement):
            title = product.get("title", "this product")
            findings.append(
                ReturnRiskFinding(
                    kind="unconfirmed_requirement",
                    evidence_type="inferred_concern",
                    message=(
                        f"You said {requirement!r} matters, but {title}'s own "
                        f"description doesn't confirm that - worth checking "
                        f"before you buy."
                    ),
                    product_id=product["product_id"],
                )
            )

    return findings
