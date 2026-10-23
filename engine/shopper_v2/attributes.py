"""Deterministic, catalog-grounded attribute resolution.

Generalizes `verification.py`'s old hardcoded `reference_attribute in
("price", "rating")` restriction: a shopper can name ANY real product
characteristic in their own words ("price", "cheaper", "weight", "battery
life"). This module never invents a value - it only ever does two things,
both purely mechanical over real, already-fetched `Product` data:

1. Normalize the shopper's own word to a REAL field this codebase's
   `shared.models.Product` actually has (currently `price` and `rating` -
   the only two numeric, comparable fields the schema defines). This is a
   synonym table for a small, FIXED set of real fields, not a per-phrase
   keyword handler for shopping vocabulary - it generalizes automatically
   to every synonym of the same two real attributes, and to nothing else,
   because nothing else is real data yet.
2. Read that field off a real `Product`, honestly reporting `None` when a
   platform hasn't populated it (a product with no `rating` is genuinely
   unrated - never a fabricated 0/5).

When a shopper names an attribute this catalog has no real field for at
all ("weight", "battery life", ...), `resolve_attribute_name` returns
`None` - the caller (`answer.py`) is required to say so honestly, never to
silently substitute a different attribute or invent a number.
"""

from __future__ import annotations

from shared.models import Product

#: Every real, comparable numeric field `Product` declares today, keyed by
#: every normalized synonym a shopper might use for it. Extending this to
#: a THIRD real attribute (e.g. once a platform declares a real `weight`
#: field on `Product`) is a one-line addition here - nothing in
#: `verification.py`/`answer.py` needs to change, since they only ever
#: call `resolve_attribute_name`/`get_attribute_value`, never hardcode a
#: field name themselves.
_ATTRIBUTE_SYNONYMS: dict[str, str] = {
    "price": "price", "cost": "price", "cheap": "price", "cheaper": "price",
    "cheapest": "price", "expensive": "price", "pricier": "price",
    "pricey": "price", "affordable": "price", "budget": "price",
    "rating": "rating", "rated": "rating", "review": "rating",
    "reviews": "rating", "stars": "rating", "score": "rating",
}

#: Human-readable label for each real field, for grounded response text.
ATTRIBUTE_LABELS: dict[str, str] = {
    "price": "price",
    "rating": "rating",
}


def resolve_attribute_name(word: str | None) -> str | None:
    """Maps a shopper's own word to a real `Product` field name, or `None`
    when it names something this catalog has no real, structured data
    for. Never guesses a close match - an unrecognized word is honestly
    unresolved, not silently mapped to the nearest known attribute."""
    if not word:
        return None
    return _ATTRIBUTE_SYNONYMS.get(word.strip().lower())


def get_attribute_value(product: Product, field: str) -> float | None:
    """Reads a real, resolved field off a real product. Returns `None`
    when the platform hasn't populated it for THIS product - never a
    fabricated default (mirrors `Product.rating`'s own documented "no
    rating yet" contract)."""
    if field == "price":
        return float(product.price.amount) if product.price is not None else None
    if field == "rating":
        return product.rating
    return None


def format_attribute_value(field: str, value: float, currency: str | None = None) -> str:
    """Real-data-only formatting for the one thing response generation is
    allowed to state - never a computed/derived phrase, just the actual
    stored value in a readable shape."""
    if field == "price":
        return f"{value:.2f} {currency}".strip() if currency else f"{value:.2f}"
    if field == "rating":
        return f"{value:g}"
    return str(value)
