"""What to tell a shopper about how a decision was reached.

Separate from chat.py because the question "what may a shopper be told" deserves one
place somebody can read, rather than being spread across a handler.

The whole file is about what to leave out.
"""

from __future__ import annotations

#: Reasons that are the merchant's business rather than the shopper's.
#:
#: A shopper being told "that is not on this merchant's allowlist" learns something
#: about how the shop is configured and nothing about their own situation. Same for a
#: suspended connection: true, and none of their business.
INTERNAL_REASONS = {
    "NOT_ON_MERCHANT_ALLOWLIST",
    "BLOCKED_BY_MERCHANT_POLICY",
    "CONNECTION_SUSPENDED",
    "CAPABILITY_UNVERIFIED",
    "UNKNOWN_ACTION_TYPE",
    "CAUTIOUS_MODE_GATES_ALL",
}

#: Why an action was ruled out, in words a shopper would use. Only the ones that
#: are about them.
#:
#: Written as fixed text rather than passed through the model, so it cannot be
#: embellished into a claim we did not make.
SHOPPER_REASONS = {
    "FINANCIAL_ALWAYS_HUMAN": "it involves money, so a person has to approve it",
    "IRREVERSIBLE_ALWAYS_HUMAN": "it cannot be undone once done",
    "TOUCHES_CUSTOMER_DATA": "it would mean contacting you, which needs a person",
    "NOT_SUPPORTED_BY_PLATFORM": "this shop's systems cannot do it",
    "PLATFORM_CANNOT": "this shop's systems cannot do it",
}

#: What each action would have been, so "ruled out" names something recognisable.
ACTION_IN_WORDS = {
    "APPLY_PROMOTION": "offering you a discount",
    "ISSUE_REFUND": "refunding you",
    "CANCEL_ORDER": "cancelling the order",
    "RETRY_PAYMENT": "trying your card again",
    "OFFER_ALTERNATE_PAYMENT": "offering another way to pay",
    "SPLIT_PAYMENT": "splitting the payment",
    "NOTIFY_BACK_IN_STOCK": "emailing you when it is back",
    "SUGGEST_ALTERNATIVE": "suggesting something else",
    "RECOMMEND_PRODUCTS": "showing you other products",
    "ADD_TO_CART": "adding it to your cart",
    "ESCALATE_TO_HUMAN": "passing it to someone at the shop",
}


#: Actions a shopper asked for directly, where an explanation is noise.
#:
#: "Why this?" on your own cart total, or on the size you just tapped, answers a
#: question nobody asked. The link belongs on decisions the engine made on their
#: behalf - a recommendation, a recovery, an escalation - where the reasoning is
#: genuinely hidden from them.
NOT_WORTH_EXPLAINING = {
    "PREPARE_CHECKOUT",
    "CHECK_ORDER_STATUS",
    "ADD_TO_CART",
    "REMOVE_CART_LINE",
    "UPDATE_CART_QUANTITY",
}


def explain(
    *,
    diagnosis: str | None,
    evidence: list[str] | None,
    rejected: list[dict] | None,
    selected_action: str | None,
    risk_rule: str | None,
) -> dict | None:
    """A shopper-safe account of how this was decided, or None if there is nothing
    worth saying.

    Returns a dict rather than prose so the interface can lay it out, and so the
    model never gets a chance to rewrite it into something we did not mean.
    """
    if selected_action in NOT_WORTH_EXPLAINING:
        return None

    lines: list[str] = []

    # What we found. The diagnosis is generated, so it is passed through as-is -
    # but it is the model describing the situation rather than making a claim about
    # what we did, which is the safer half of its output.
    if diagnosis:
        lines.append(diagnosis)

    # What we ruled out, and why. This is the part that matters: an assistant that
    # says what it declined to do is making a claim a shopper can check against
    # its own behaviour.
    declined: list[str] = []
    for item in rejected or []:
        reason_code = str(item.get("reason") or "")
        if reason_code in INTERNAL_REASONS:
            continue

        action = ACTION_IN_WORDS.get(str(item.get("action_type") or ""))
        reason = SHOPPER_REASONS.get(reason_code)

        if action and reason:
            declined.append(f"{action}, because {reason}")
        elif action:
            # Named without a reason rather than paired with a code. "Ruled out
            # offering you a discount" is honest; "ruled out offering you a
            # discount (NOT_SUPPORTED)" is us thinking aloud.
            declined.append(action)

    # Nothing ruled out means nothing was chosen between, and a decision nobody
    # made is not one worth explaining. This is what stops "Why this?" appearing
    # on a greeting, where the honest answer is that the shopper said hello.
    #
    # The diagnosis alone is not enough to earn the link. It is the model
    # describing the situation, which a shopper can already see - the value was
    # always in what we declined to do.
    if not declined:
        return None

    if not lines and not declined:
        return None

    return {
        "found": lines,
        "declined": declined[:3],
        # The evidence is facts read from the platform - an order status, a stock
        # count - so it is safe to show and it is what makes the rest credible.
        "evidence": (evidence or [])[:3],
    }
