"""Deterministic recognition of "something about my actual cart" requests.

The model does not reliably propose CART_INTELLIGENCE for phrasing that
obviously calls for it - live-reproduced repeatedly: "what should I change
in my cart?", "can you fix my cart?", "do I need anything else?" sometimes
produce SUGGEST_COMPLEMENTARY, ANSWER_PRODUCT_QUESTION, or nothing
cart-specific at all, because deciding "this needs CART_INTELLIGENCE" is a
tool-call choice the model makes freshly every turn, the same unreliability
class as every other model-choice variance already documented in this
project (see CLAUDE.md's "The constraint" and the repeated Groq-throttle/
model-variance entries in Completed.md/PROGRESS.md).

This module is the same shape as `engine.reasoning.explicit_extraction`:
a narrow, deterministic, keyword-based supplement - not a second NLP system,
not a replacement for the model's own judgement, never itself asserting a
risk or a fact about the cart. It only answers one question: does this
message's own words plainly ask about the shopper's actual cart, in a way a
plain keyword match can recognise without guessing? When it says yes, the
caller (engine/api/chat.py) adds CART_INTELLIGENCE as an additional
candidate for the Decision Engine to rank alongside whatever the model
itself proposed - a union, exactly like explicit_extraction, never a
replacement. The Decision Engine's own ranking (engine/decision/ranking.py)
still decides what actually wins; this module only guarantees the option
exists to be ranked, closing the specific failure mode where the model
simply never surfaces it as a candidate at all.
"""

from __future__ import annotations

import re

#: Deliberately narrow and literal, matching only phrasing that is
#: unambiguously about the shopper's own actual cart - never a general
#: "what should I buy" question, which stays with RECOMMEND_PRODUCTS/
#: SUGGEST_COMPLEMENTARY exactly as before. A false negative here just
#: means the model's own choice is unassisted, the same as before this
#: module existed; a false positive would add a candidate the Decision
#: Engine is free to rank below whatever else the model proposed, so the
#: cost of being slightly too eager is low and bounded.
_CART_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(what('?s| is)?|anything|is\s+there\s+anything)\s+wrong\s+with\s+(my|the)\s+cart\b",
        r"\bwhat\s+should\s+i\s+(change|fix|do)\s+(in|with|about)\s+(my|the)\s+cart\b",
        r"\b(can|could)\s+you\s+fix\s+(my|the)\s+cart\b",
        r"\bwhat\s+am\s+i\s+missing\b",
        r"\b(can|could)\s+you\s+optimi[sz]e\s+(my|this|the)\s+cart\b",
        r"\bdo\s+i\s+need\s+anything\s+else\b",
        r"\banything\s+(else\s+)?i\s+(should|need to)\s+(add|get|know)\b",
        r"\bremove\s+the\s+incompatible\s+(item|product)s?\b",
        r"\b(suggest|recommend)\s+a\s+better\s+combination\b",
        r"\bis\s+(my|this|the)\s+cart\s+(ok|okay|fine|good|right)\b",
        r"\bover\s+(my\s+)?budget\b",
        r"\bcheck\s+(my|the)\s+cart\b",
        r"\b(review|look\s+at)\s+(my|the)\s+cart\b",
    )
)


def wants_cart_intelligence(message: str) -> bool:
    """Whether `message`'s own words plainly ask about the actual cart -
    a narrow keyword match, never a semantic judgement, and never itself a
    claim about what the cart contains."""
    text = (message or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _CART_INTENT_PATTERNS)
