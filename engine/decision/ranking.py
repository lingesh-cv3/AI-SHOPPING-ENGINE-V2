"""Action ranking.

Given several candidates that are all available and all permitted, which one
should the engine choose? This module answers that with a static table rather
than a scoring function.

A table was chosen deliberately over cleverness:

- It is explainable. A merchant asking "why did it offer a substitute instead of
  a discount?" gets a real answer, not a number.
- It is auditable. The preference order is visible in one place and changing it
  is a reviewed code change.
- It is cheap for the merchant. The ordering encodes a bias: solve the problem
  without spending the merchant's money if there is any way to do so. A free
  alternative beats a discount, every time.

Learning may adjust this ordering later, once outcome data shows a preference is
wrong. Learning must never touch risk classification - that stays deterministic
in the gate. Ranking is opinion; risk is a rule.
"""

from __future__ import annotations

from shared.models import ActionType, FrictionType

#: Per-friction preference order, most preferred first.
#:
#: Read each list as a fallback chain: try the first thing that is available and
#: permitted, then the next. Money-touching options appear late in every chain,
#: not because they are forbidden here - the Risk Gate handles that - but because
#: a solution that costs the merchant nothing is a better solution.
PREFERENCE: dict[FrictionType, tuple[ActionType, ...]] = {
    # The shopper searched and found nothing. Almost always a search-quality
    # problem rather than an empty catalog, so suggesting what they meant is both
    # the cheapest and the most likely fix.
    FrictionType.DEAD_SEARCH: (
        ActionType.SUGGEST_ALTERNATIVE,
        ActionType.RECOMMEND_PRODUCTS,
        ActionType.ANSWER_PRODUCT_QUESTION,
    ),
    # They wanted something specific that is gone. Offer a substitute first; only
    # offer to tell them when it returns if there is no substitute, since that
    # defers the sale rather than making it.
    FrictionType.PRODUCT_UNAVAILABLE: (
        ActionType.SUGGEST_ALTERNATIVE,
        ActionType.RECOMMEND_PRODUCTS,
        ActionType.NOTIFY_BACK_IN_STOCK,
    ),
    # A specific size or colour is out. Another variant of the same product is a
    # much better answer than a different product.
    FrictionType.VARIANT_UNAVAILABLE: (
        ActionType.CHECK_AVAILABILITY,
        ActionType.SUGGEST_ALTERNATIVE,
        ActionType.NOTIFY_BACK_IN_STOCK,
    ),
    # Their coupon failed. Explaining why is free and often enough - the code may
    # simply need a higher basket total. Offering a different discount costs the
    # merchant real margin, so it comes last.
    FrictionType.PROMOTION_FAILED: (
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.APPLY_PROMOTION,
    ),
    # Something broke at checkout and we do not know what. Escalating is honest;
    # guessing at a checkout fault risks making it worse.
    FrictionType.CHECKOUT_ERROR: (
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.ESCALATE_TO_HUMAN,
    ),
    # The card was refused. Every option here is financial, so every option is
    # human-gated regardless of order. Ordered by how reversible each is and how
    # little friction it adds for the shopper.
    # The card was refused. Every option here is financial, so every option is
    # human-gated regardless of order.
    #
    # Alternate method leads, not retry. A bank that just said no to a card says no
    # again when asked identically - Kettle's own capability declaration states this
    # (same_method_retry_effective: false). Retrying first wastes the shopper's
    # patience on the option least likely to work.
    # Alternate method leads, not retry. A bank that just said no to a card says no
    # again when asked identically - Kettle's own capability declaration states this
    # (same_method_retry_effective: false). Retrying first spends the shopper's
    # patience on the option least likely to work.
    FrictionType.PAYMENT_DECLINED: (
        ActionType.OFFER_ALTERNATE_PAYMENT,
        ActionType.SPLIT_PAYMENT,
        ActionType.RETRY_PAYMENT,
        ActionType.ESCALATE_TO_HUMAN,
    ),
    # They left with items in the cart. Reminding them what is there costs
    # nothing; discounting to win them back costs margin on a sale that might
    # have completed anyway.
    FrictionType.CART_ABANDONED: (
        ActionType.RECOMMEND_PRODUCTS,
        ActionType.NOTIFY_BACK_IN_STOCK,
        ActionType.APPLY_PROMOTION,
    ),
    # They have already hit the same wall more than once. Automation has had its
    # chance; hand it to a person with the full history.
    FrictionType.REPEATED_FAILURE: (ActionType.ESCALATE_TO_HUMAN,),
    FrictionType.OTHER: (
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.ESCALATE_TO_HUMAN,
    ),
}

#: Used when no friction type is known - a plain shopping-assistance turn rather
#: than a recovery case.
#:
#: Note the ordering against the friction chains above: doing beats describing. A
#: shopper who asks "do you have running shoes" is better served by being shown
#: five shoes than by being told that yes, we do. ANSWER_PRODUCT_QUESTION sits last
#: because it executes nothing - it is the right answer only when the model
#: proposed nothing else, which is exactly what happens for a genuine question like
#: "what is your returns policy".
ASSISTANCE_PREFERENCE: tuple[ActionType, ...] = (
    ActionType.ADD_TO_CART,
    ActionType.RECOMMEND_PRODUCTS,
    ActionType.SUGGEST_ALTERNATIVE,
    ActionType.CHECK_AVAILABILITY,
    ActionType.ANSWER_PRODUCT_QUESTION,
)


def rank_for(friction: FrictionType | None) -> tuple[ActionType, ...]:
    """The preference chain for a friction type, or the assistance chain."""
    if friction is None:
        return ASSISTANCE_PREFERENCE
    return PREFERENCE.get(friction, PREFERENCE[FrictionType.OTHER])


def rank_of(action_type: ActionType, friction: FrictionType | None) -> int:
    """Position in the preference chain. Lower is better.

    Anything absent from the chain sorts last rather than being rejected. An
    unranked action is still a valid action - the AI may have proposed something
    sensible that this table does not anticipate, and dropping it entirely would
    be worse than trying it after the known-good options.
    """
    chain = rank_for(friction)
    try:
        return chain.index(action_type)
    except ValueError:
        return len(chain)


def explain_preference(friction: FrictionType | None) -> str:
    """Human-readable chain, for the consoles and for merchant conversations."""
    chain = rank_for(friction)
    label = friction or "shopping assistance"
    return f"{label}: " + " -> ".join(str(a) for a in chain)