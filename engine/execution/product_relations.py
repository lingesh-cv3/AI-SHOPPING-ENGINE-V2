"""Compatibility and complementary-product reasoning.

Both questions this module answers - "does A work with B?" and "what else
goes with A?" - are answered from exactly one source: a merchant's own
declared `compatible_with` list on `Product` (see
`shared.models.commerce.Product.compatible_with`). Nothing here infers a
relationship from category, title or description text.

That restraint is the whole point. A category match ("both are running
shoes") or a text match ("both mention 'trail'") is not evidence of fit -
gravel-race shoes and a road marathon plate are the same category and
actively wrong together. Guessing compatibility from text is exactly the
"nonexistent product substitution" / "false compatibility" failure mode the
Shopper spec calls out by name. So the only three answers this module can
give are:

- ``"compatible"``  - the merchant's own data says so, in either direction
- ``"not_compatible"`` - never actually asserted here (see below)
- ``"unknown"`` - no declared relationship either way

There is deliberately no path from "unknown" to "not_compatible" either.
Absence of a declared relationship is not evidence of active incompatibility
- it just means nobody told us. A merchant who never bothered to link two
products that do, in fact, work together should get "unknown", not a false
negative that talks a shopper out of a fine purchase.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.models import Product


@dataclass(frozen=True)
class CompatibilityResult:
    verdict: str  # "compatible" | "unknown"
    reason: str


def check_compatibility(first: Product, second: Product) -> CompatibilityResult:
    """Whether `first` and `second` are declared compatible.

    Checked both directions - a merchant may have only recorded the
    relationship on one of the two products - because a shopper asking
    "does the grinder work with the filter blend" does not know or care
    which product's record the merchant happened to store it on.
    """
    if second.product_id in first.compatible_with or first.product_id in second.compatible_with:
        return CompatibilityResult(
            verdict="compatible",
            reason=f"{first.title} and {second.title} are declared compatible by the merchant.",
        )
    return CompatibilityResult(
        verdict="unknown",
        reason=(
            f"No declared compatibility relationship between {first.title} "
            f"and {second.title}. That means it hasn't been confirmed either "
            f"way - not that they don't work together."
        ),
    )


def complementary_product_ids(anchor: Product) -> list[str]:
    """The product ids the merchant has declared as complementary to `anchor`.

    Returns `anchor.compatible_with` itself - a plain accessor, not a
    computation - kept as its own function so the execution and reply-
    building layers share one name for "where this data comes from" rather
    than reading `.compatible_with` directly in three places.
    """
    return list(anchor.compatible_with)
