"""Shared, deterministic ranking/superlative application over real product data.

Consolidates what used to be one-off logic living only inside
`engine/shopper_v2/compare.py`'s own `_deterministic_superlative` (reachable
from exactly one capability, one branch) into ONE real function every
capability - discovery, compare, missions - calls the same way. This is
part of the root-cause consolidation named in the "Shopper item 3 follow-up"
conversation: a general capability, not a fixed branch, is what closes the
whole CLASS of "the deterministic answer only exists in one narrow place"
bug, not just the one reported instance of it.

Only "price" and "rating" are supported, because those are the only two
real, always-comparable numeric fields `shared.models.Product` actually
carries. A request to rank by anything this catalog does not actually
expose as data (e.g. "durability") has no real field behind it - the
caller must refuse that honestly (say so, never invent a value), not
guess. Never a model call, never a fabricated number.
"""

from __future__ import annotations

from shared.models import Availability, Product

#: The ONLY two real, comparable numeric attributes a `Product` carries.
#: Adding a new sortable attribute means adding a real getter here - the
#: one place every caller (discovery/compare/missions) shares - never a
#: second, local copy of "how do I get this product's price."
_ATTRIBUTE_GETTERS = {
    "price": lambda p: p.price.amount if p.price is not None else None,
    "rating": lambda p: p.rating,
}

#: Real, human-readable names for `_ATTRIBUTE_GETTERS`'s keys - used only
#: for an honest "I don't have that to sort by" message, never as data.
KNOWN_ATTRIBUTES = tuple(_ATTRIBUTE_GETTERS)


def mixed_currencies(products: list[Product]) -> bool:
    """True when the priced products here are in more than one currency - their amounts are then different units and must never be ranked against each other."""
    return len({p.price.currency for p in products if p.price is not None}) > 1


def availability_note(product: Product) -> str:
    """" (currently out of stock)" for a product the platform reports as sold out, else "". A superlative pick must never read as buyable when it is not."""
    return " (currently out of stock)" if product.availability == Availability.OUT_OF_STOCK else ""


def sort_products(products: list[Product], attribute: str, direction: str) -> list[Product]:
    """Real, stable sort by `attribute` ("price"/"rating"), `direction`
    ("asc"/"desc"). A product missing a real value for `attribute` (no
    price, no rating) is moved to the end, in its original relative order
    - never assigned a fabricated value that would silently misplace it
    among products that DO have a real one. Returns `products` unchanged,
    in the same order, if `attribute` isn't one of `KNOWN_ATTRIBUTES` -
    the caller must check `KNOWN_ATTRIBUTES` itself before claiming a sort
    happened, this function never fabricates one."""
    getter = _ATTRIBUTE_GETTERS.get(attribute)
    if getter is None:
        return products
    if attribute == "price" and mixed_currencies(products):
        return products  # different currencies are not comparable: leave the order alone rather than rank 900 JPY below 10 USD
    with_value = [(p, getter(p)) for p in products]
    known = [(p, v) for p, v in with_value if v is not None]
    unknown = [p for p, v in with_value if v is None]
    known.sort(key=lambda pair: pair[1], reverse=(direction == "desc"))
    return [p for p, _ in known] + unknown


def pick_superlative(products: list[Product], attribute: str, which: str) -> Product | None:
    """The one real product with the min/max real `attribute` value among
    `products` - `which` is "min" or "max". Returns `None` when
    `attribute` isn't real (see `KNOWN_ATTRIBUTES`) or no product in the
    list has a real value for it - never guesses, never picks an
    arbitrary one to avoid saying "I don't know"."""
    getter = _ATTRIBUTE_GETTERS.get(attribute)
    if getter is None:
        return None
    if attribute == "price" and mixed_currencies(products):
        return None  # no honest cheapest/dearest across currencies (there is no exchange rate to use)
    scored = [(p, getter(p)) for p in products if getter(p) is not None]
    if not scored:
        return None
    fn = max if which == "max" else min
    return fn(scored, key=lambda pair: pair[1])[0]
