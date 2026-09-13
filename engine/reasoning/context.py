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
    shopping_state: dict | None = None,
    memory_facts: list[dict] | None = None,
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

    # Their current mission, if one has been captured - the structured
    # "current shopping state" layer. Placed early, right after what they
    # just said, because it is high-value grounding for everything that
    # follows: a budget or exclusion stated three turns ago still applies to
    # this one, and it should not have to survive by being re-read out of
    # raw history prose.
    if shopping_state:
        bits = []
        if shopping_state.get("goal"):
            bits.append(f"goal: {shopping_state['goal']}")
        if shopping_state.get("budget_amount"):
            currency = shopping_state.get("budget_currency") or ""
            bits.append(f"budget: {shopping_state['budget_amount']} {currency}".strip())
        for field, label in (
            ("must_have", "must have"),
            ("exclusions", "must not have"),
            ("nice_to_have", "would like"),
            ("priorities", "trade-off priorities"),
        ):
            values = shopping_state.get(field)
            if values:
                bits.append(f"{label}: {', '.join(values)}")
        if shopping_state.get("mission_status"):
            bits.append(f"status: {shopping_state['mission_status']}")
        if bits:
            parts.append(
                "THEIR CURRENT SHOPPING MISSION (established earlier this "
                "visit - treat a newer, contradicting statement in THE "
                "SHOPPER SAYS above as replacing this, not the other way "
                "round):\n  " + "; ".join(bits)
            )

    # Durable facts about this shopper, from a previous visit - never from
    # this conversation's own history, which is already covered below.
    # Authenticated shoppers only; see engine/db/memory_facts.py.
    #
    # A documented boundary, not an oversight (see PROGRESS.md's hardening-
    # pass entry for the full audit finding): this is the ONLY place
    # durable memory reaches the model. `engine/execution/mission_ranking.py`
    # never receives `memory_facts` at all, by design, so the deterministic
    # shortlist/ranking can never be driven by a stored preference. What
    # the audit found is that the model can still fold a fact handed to it
    # here into its own `shopping_state` structured output on a later,
    # fully generic message - and *that* structured output IS read by
    # ranking, so a durable fact can end up influencing the shortlist
    # indirectly, through the model's own extraction habit rather than
    # through any code path that treats it as an input to ranking. This is
    # real, observed behaviour - and explicitly NOT an engineered
    # dependency: nothing here is written to rely on it happening, nothing
    # asserts it happens, and it could stop with a prompt or model change
    # and nothing would catch the regression. Do not build on this
    # assuming it is guaranteed; if durable-memory-driven ranking is wanted
    # as a real feature, it needs an explicit, tested code path, not this
    # side effect.
    if memory_facts:
        parts.append(
            "WHAT WE ALREADY KNOW ABOUT THIS SHOPPER FROM BEFORE (stated by "
            "them previously, not necessarily relevant to this exact "
            "message):\n"
            + "\n".join(f"  - {f['key']}: {f['value']}" for f in memory_facts)
        )

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
            "EARLIER IN THIS VISIT, AND NOT NECESSARILY WHAT THEY ARE ASKING ABOUT NOW (they have not necessarily "
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
            # Labelled with their ids, because the model is asked to name a variant
            # and cannot name one it has never seen. Showing "8" alone led it to
            # invent an id or omit the field, and the turn was wasted asking a
            # question the shopper had already answered.
            #
            # Only the buyable ones. An id it cannot use is an id it might try.
            bits.append(
                "options "
                + ", ".join(
                    f"{v.title or v.variant_id}={v.variant_id}" for v in buyable
                )
            )
        else:
            bits.append("every option sold out")
        bits.append(str(p.availability).replace("_", " ").lower())

    if p.categories:
        bits.append(p.categories[0])
    return ", ".join(bits)
