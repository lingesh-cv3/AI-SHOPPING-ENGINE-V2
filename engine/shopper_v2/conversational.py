"""Conversational capability - greetings, thanks, small talk, "what can
you do?", and every other turn genuinely classified `message_intent ==
"conversational"` (see `understanding.py`'s own docstring for the field
and the live-reproduced bug this closes: every such turn used to reach
`Capability.DISCOVER` and run a real catalog search against a greeting -
"hi" once returned a random product listing from a blank-query scan).

Zero extra model calls: `understanding.py` already captured the model's
own `reply` for THIS turn, conditionally, only for this one classification
- see that module's own reasoning for why the reply-before-data invariant
does not apply here (nothing commerce-shaped is being described). Still
never trusted blindly: the same `_claims_a_cart_action` guard `respond.py`
already uses for the discovery synthesis path is applied here too, so a
model confusing itself into "I've added that to your cart" on a "thanks"
message is caught the same way.

Read-only, no adapter call, no `engine.decision`/`engine.risk`/
`engine.execution` import - a conversational turn changes nothing.
"""

from __future__ import annotations

from .respond import _claims_a_cart_action
from .state import ShoppingStateV2

_GENERIC_GREETING = (
    "Hi! I can help you find products, compare options, or check on an "
    "order - what are you looking for today?"
)
_GENERIC_ACKNOWLEDGEMENT = "Happy to help - let me know if there's anything else."
_WITH_ACTIVE_PRODUCTS = (
    "Happy to help! Let me know if you'd like to see more options, or if "
    "you're ready to move forward with what's already shown."
)


def run_conversational_turn(
    *, conversational_reply: str | None, used_model: bool, state: ShoppingStateV2,
) -> tuple[str, bool]:
    """Returns (reply, used_model). The model's own reply is preferred
    whenever it passed the cart-claim guard; the deterministic fallback
    below is genuinely context-aware too (whether anything is currently
    shown), just less rich than a real model reply - "preserve and use
    relevant context" holds either way, per the batch's own requirement."""
    if conversational_reply and not _claims_a_cart_action(conversational_reply):
        return conversational_reply, used_model

    if state.active_products:
        return _WITH_ACTIVE_PRODUCTS, False
    if state.goal or state.mission_items:
        return _GENERIC_ACKNOWLEDGEMENT, False
    return _GENERIC_GREETING, False
