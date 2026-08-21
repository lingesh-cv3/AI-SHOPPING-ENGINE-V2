"""Context assembly.

What the model gets to see, and in what form. This is a smaller question than it
sounds and a more consequential one: everything here is something the model can
reason over, and everything omitted is something it cannot draw a conclusion about -
because the prompt forbids asserting facts not present.

Three rules govern what goes in:

Normalized only. The model never sees price_paise or stock_state "Y". It sees the
same normalized models the rest of the engine sees. A model that learned one
platform's field names would produce proposals that only work on that platform,
which defeats the adapter boundary.

No capability list. The model is not told what the merchant's platform supports. It
proposes freely and the Decision Engine filters. The alternative - feeding
capabilities in so it only proposes supported actions - was considered and
rejected: it moves capability authority into the reasoning layer, and it hides the
capability gap instead of surfacing it.

No raw payloads. A platform's original response is preserved for operators but
never fed to the model, so a change in a source's format cannot quietly change how
the engine reasons.
"""

from __future__ import annotations

from shared.models import Cart, FrictionType, Order, Product


def build_context(
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
) -> str:
    """Assemble the user message for one turn.

    Written as plain labelled text rather than JSON. Open models follow prose
    context more reliably than nested JSON, and it keeps the token count down,
    which matters on a per-minute token budget.
    """
    parts: list[str] = []

    if message:
        # The shopper's own words go first. Everything after is context for
        # answering them, not a competing instruction.
        parts.append(f"THE SHOPPER SAYS: {message}")
    elif friction:
        parts.append(f"SITUATION: {friction}")
    else:
        parts.append("SITUATION: the shopper is browsing normally")

    if friction and message:
        parts.append(f"AND THE STOREFRONT DETECTED: {friction}")

    if query:
        parts.append(f'THE SHOPPER SEARCHED FOR: "{query}"')

    if products is not None:
        if products:
            parts.append(
                "SEARCH RETURNED:\n"
                + "\n".join(f"  - {_product_line(p)}" for p in products[:8])
            )
        else:
            parts.append("SEARCH RETURNED: nothing at all")

    # The catalog sample is what makes a useful alternative possible. Without it the
    # model can only guess at search terms; with it, it can propose a term that will
    # actually match something.
    if catalog_sample:
        parts.append(
            "SOME PRODUCTS THIS SHOP SELLS (for picking a search term that will "
            "actually match):\n"
            + "\n".join(f"  - {_product_line(p)}" for p in catalog_sample[:20])
        )

    if cart and not cart.is_empty:
        lines = "\n".join(
            f"  - {ln.quantity} x {ln.title} at {ln.unit_price}" for ln in cart.lines
        )
        promos = (
            f", coupon {', '.join(cart.applied_promotions)}"
            if cart.applied_promotions
            else ""
        )
        parts.append(f"THEIR CART ({cart.grand_total} total{promos}):\n{lines}")
    elif cart:
        parts.append("THEIR CART: empty")

    if order:
        paid = order.amount_paid or "nothing"
        reason = (
            f", the bank said {order.decline_reason}" if order.decline_reason else ""
        )
        parts.append(
            f"THEIR ORDER {order.order_id}: {order.status}, payment "
            f"{order.payment_status}{reason}. Paid {paid} of {order.grand_total}."
        )

    # Recorded friction is labelled as observed fact rather than as conversation.
    # The assistant should know a payment failed without claiming it was discussed.
    if recorded_friction:
        parts.append(
            "THE STOREFRONT ALREADY RECORDED THIS VISIT (they have not necessarily "
            "mentioned it to you):\n"
            + "\n".join(f"  - {f}" for f in recorded_friction)
        )

    if history:
        parts.append("WHAT YOU HAVE BOTH SAID SO FAR:\n" + "\n".join(history))

    parts.append(
        "Reply to them and propose what would help. Call propose_actions."
        if message
        else "What would help? Call propose_actions."
    )
    return "\n\n".join(parts)


def _product_line(p: Product) -> str:
    """One product, compactly.

    Variant availability is summarised rather than listed in full. A model does not
    need every size to reason about whether something is buyable, and the token
    budget is tight.
    """
    bits = [p.title, f"id {p.product_id}", str(p.price) if p.price else "no price"]

    if p.variants:
        buyable = [v for v in p.variants if v.availability != "OUT_OF_STOCK"]
        if buyable:
            bits.append("sizes " + "/".join(v.title or v.variant_id for v in buyable))
        else:
            bits.append("every size sold out")
    else:
        bits.append(str(p.availability).replace("_", " ").lower())

    if p.categories:
        bits.append(p.categories[0])
    return ", ".join(bits)