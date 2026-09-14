"""Cart intelligence: understanding the actual cart, not repeating it back.

A pure function over data already fetched fresh this turn - the real cart
(`Cart`), the real, freshly-fetched `Product` for every line in it, and the
current `ShoppingState` (budget/exclusions/priorities). Every finding here is
traceable to one of those three real sources; nothing is inferred from
category or description text, and nothing here ever fabricates a product
fact. Structurally the same discipline `mission_ranking.py` and
`product_relations.py` already follow, applied to the cart instead of the
catalog.

Findings are deliberately conservative: this reports what the data actually
shows (a duplicate line, an over-budget total, a missing declared
complement, an unavailable line, a shopper-stated exclusion sitting in the
cart) and nothing it would have to guess at (there is no "you probably
don't need this" heuristic here - that would be exactly the unsupported
claim CLAUDE.md's honesty requirements rule out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from shared.models import Cart, Product


@dataclass
class CartFinding:
    kind: str
    message: str
    product_ids: list[str] = field(default_factory=list)


def analyze_cart(
    cart: Cart,
    products_by_id: dict[str, Product],
    shopping_state: dict | None,
) -> list[CartFinding]:
    """`products_by_id` must carry a freshly-fetched `Product` for every
    distinct product_id on the cart's lines that the caller could resolve -
    a line whose product could not be fetched (deleted, adapter error) is
    simply skipped for product-dependent findings rather than guessed at,
    though it still counts toward duplicate/quantity checks that only need
    the cart line itself.
    """
    findings: list[CartFinding] = []
    shopping_state = shopping_state or {}

    findings.extend(_duplicate_lines(cart))
    findings.extend(_unavailable_lines(cart, products_by_id))
    findings.extend(_budget_check(cart, shopping_state))
    findings.extend(_exclusion_conflicts(cart, products_by_id, shopping_state))
    findings.extend(_missing_complements(cart, products_by_id))
    findings.extend(_return_risk(cart, products_by_id))

    return findings


def _duplicate_lines(cart: Cart) -> list[CartFinding]:
    """Two or more lines for the same product (different variants counted
    separately is fine and normal - two DIFFERENT sizes of the same shoe is
    not a mistake). Flags only a genuine same-product, same-variant repeat,
    which the platform itself should usually merge but not every adapter
    does (Kettle merges same-variant lines, Northfield does not - see
    CLAUDE.md)."""
    seen: dict[tuple[str, str | None], list[str]] = {}
    for line in cart.lines:
        key = (line.product_id, line.variant_id)
        seen.setdefault(key, []).append(line.title)

    out = []
    for (product_id, _variant_id), titles in seen.items():
        if len(titles) > 1:
            out.append(
                CartFinding(
                    kind="duplicate_line",
                    message=(
                        f"{titles[0]} appears as {len(titles)} separate lines "
                        f"in your cart - worth combining into one."
                    ),
                    product_ids=[product_id],
                )
            )
    return out


def _unavailable_lines(
    cart: Cart, products_by_id: dict[str, Product]
) -> list[CartFinding]:
    """A line whose product, checked fresh just now, is no longer buyable -
    grounded in the live availability just fetched, never the availability
    that happened to be true when it was added."""
    out = []
    for line in cart.lines:
        product = products_by_id.get(line.product_id)
        if product is None:
            continue
        if str(product.availability) == "OUT_OF_STOCK":
            out.append(
                CartFinding(
                    kind="unavailable",
                    message=f"{line.title} is now out of stock.",
                    product_ids=[line.product_id],
                )
            )
    return out


def _budget_check(cart: Cart, shopping_state: dict) -> list[CartFinding]:
    """The cart's real total against a budget the shopper actually stated
    this session - never a budget assumed or invented."""
    amount = shopping_state.get("budget_amount")
    if not amount or cart.grand_total is None:
        return []
    try:
        budget = Decimal(str(amount))
    except InvalidOperation:
        return []
    total = cart.grand_total.amount
    if total > budget:
        over_by = total - budget
        return [
            CartFinding(
                kind="over_budget",
                message=(
                    f"Your cart total ({total} {cart.grand_total.currency}) is "
                    f"{over_by} {cart.grand_total.currency} over the "
                    f"{budget} {cart.grand_total.currency} budget you mentioned."
                ),
            )
        ]
    return []


def _exclusion_conflicts(
    cart: Cart, products_by_id: dict[str, Product], shopping_state: dict
) -> list[CartFinding]:
    """A product sitting in the cart that matches something the shopper
    explicitly said to exclude this session - checked against the real
    title/category text of the real fetched product, the same coarse
    keyword match `mission_ranking` already uses, not a guess."""
    exclusions = [str(e).strip().lower() for e in (shopping_state.get("exclusions") or []) if e]
    if not exclusions:
        return []
    out = []
    for line in cart.lines:
        product = products_by_id.get(line.product_id)
        haystack = " ".join(
            filter(None, [line.title.lower(), (product.description or "").lower() if product else ""])
        )
        for excl in exclusions:
            if excl and excl in haystack:
                out.append(
                    CartFinding(
                        kind="exclusion_conflict",
                        message=(
                            f"{line.title} is in your cart, but you said to "
                            f"avoid \"{excl}\" this session."
                        ),
                        product_ids=[line.product_id],
                    )
                )
                break
    return out


#: Same threshold/reasoning as engine/execution/return_risk.py - kept as an
#: independent constant rather than importing a private module constant,
#: since this operates on real Product objects, not the dict shape
#: return_risk.assess_product expects (the recommendation/comparison
#: payloads this list came from are plain dicts; a cart's fetched products
#: are the real pydantic Product model).
_MIN_RATING_COUNT_FOR_SIGNAL = 15
_LOW_RATING_THRESHOLD = 3.7


def _return_risk(cart: Cart, products_by_id: dict[str, Product]) -> list[CartFinding]:
    """A real, evidence-grounded pre-purchase friction signal for something
    already in the cart - a numerically low rating, from the platform's own
    declared rating/rating_count, never invented. Never a claim about this
    product's actual return history (no such data exists in this system -
    see return_risk.py's own module docstring for why)."""
    out = []
    for line in cart.lines:
        product = products_by_id.get(line.product_id)
        if product is None or product.rating is None:
            continue
        count = product.rating_count or 0
        if count >= _MIN_RATING_COUNT_FOR_SIGNAL and product.rating < _LOW_RATING_THRESHOLD:
            out.append(
                CartFinding(
                    kind="return_risk",
                    message=(
                        f"{line.title} has a lower customer rating "
                        f"({product.rating}/5 from {count} reviews) - worth a "
                        f"second look before you check out."
                    ),
                    product_ids=[line.product_id],
                )
            )
    return out


def _missing_complements(
    cart: Cart, products_by_id: dict[str, Product]
) -> list[CartFinding]:
    """What the merchant has declared as complementary to something already
    in the cart, that is not itself already in the cart - reuses the exact
    same compatible_with data CHECK_COMPATIBILITY/SUGGEST_COMPLEMENTARY
    already answer from, applied cart-wide instead of to one named
    product."""
    in_cart = {line.product_id for line in cart.lines}
    out = []
    for line in cart.lines:
        product = products_by_id.get(line.product_id)
        if product is None:
            continue
        missing = [pid for pid in product.compatible_with if pid not in in_cart]
        if missing:
            out.append(
                CartFinding(
                    kind="missing_complement",
                    message=(
                        f"{line.title} usually goes with "
                        f"{', '.join(missing)} - not in your cart yet."
                    ),
                    product_ids=missing,
                )
            )
    return out
