"""Is this message an explicit command to add something to the cart? (pure, no model, no I/O)

Policy (Step 25, deliberate - restoring the original "an explicit, resolved add is executed" intent that Step 17
had turned into a proposal for every add): the SHOPPER'S OWN WORDS authorize a direct add, never the model's
reading of them. A message is an explicit add command only when all of these hold:

* it contains an add verb ("add", "buy", "purchase", or "put/place ... in the cart") - the strong set;
* it is not a question or advice request ("should I add it?", "how do I add it?", "what happens if I add it?");
* it is not negated, conditional or cancelled ("don't add it", "never add it", "if it's cheap, add it").

Everything else that merely sounds like wanting the product ("I'll take it", "get me the Trailblazer", "sounds
good", "yes", "perfect") is NOT authorization for a direct add: it stays the existing proposal-or-question
behaviour, and a vague agreement never changes a cart.

This module only classifies the sentence. Which product it refers to, whether it needs a size, whether it is
already in the cart, stock, and the Decision -> Risk -> Execution path are all decided elsewhere, unchanged.
"""

from __future__ import annotations

import re

#: Weaker add-shaped verbs: enough to treat a message as a cart-add INTENT (resolve a product, ask, or propose),
#: never enough on their own for a direct add.
ADD_VERB = re.compile(
    r"\b(?:add|buy|purchase|get|order|take|i'?ll take)\b|\b(?:put|place|throw|drop)\b[^.?!]{0,30}\b(?:cart|basket|bag)\b",
    re.IGNORECASE,
)
#: A message that is about something else entirely even though it contains one of those verbs.
NOT_AN_ADD = re.compile(r"\b(?:remove|delete|clear|empty|cancel|undo|compare|show|find|search)\b", re.IGNORECASE)

_STRONG_ADD = re.compile(
    r"\b(?:add|buy|purchase)\b|\b(?:put|place|throw|drop)\b[^.?!]{0,30}\b(?:cart|basket|bag)\b", re.IGNORECASE
)
_NEGATION = re.compile(
    r"\b(?:not|never|without|stop|nothing|none|instead of|rather than|no|cannot|cant|wont)\b|n't\b|\bdont\b", re.IGNORECASE
)
#: Not now: "I might add it later", "maybe add it tomorrow". A deferral is a statement about the future, not a command.
_DEFERRAL = re.compile(
    r"\b(?:later|tomorrow|tonight|someday|eventually|maybe|perhaps|might|at some point|next (?:week|time|month))\b",
    re.IGNORECASE,
)
#: Words quoted from somewhere else ("the sign says "add to cart"") are not the shopper's command.
_QUOTED = re.compile(r'["\u201c\u201d][^"\u201c\u201d]*\b(?:add|buy|purchase)\b[^"\u201c\u201d]*["\u201c\u201d]', re.IGNORECASE)
#: A question or request for advice about adding, not a command to add.
_ADVICE_OPENER = re.compile(
    r"^\s*(?:(?:so|and|but|ok|okay|well|hmm|um)[,\s]+)?"
    r"(?:should|shall|how|why|what|which|when|where|whether|who|is|are|was|were|does|did|do i|do you think|would it|will it"
    r"|can i|could i|may i|am i able|is it possible|is there a way)\b",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(r"\b(?:if|unless|in case|supposing|whether|assuming)\b", re.IGNORECASE)


def classify_add_message(message: str) -> str:
    """"command" | "not_a_command" | "none"."""
    text = message or ""
    if not ADD_VERB.search(text) or NOT_AN_ADD.search(text):
        return "none"
    if (_NEGATION.search(text) or _ADVICE_OPENER.search(text) or _CONDITIONAL.search(text)
            or _DEFERRAL.search(text) or _QUOTED.search(text)):
        return "not_a_command"
    return "command"


def names_an_add(message: str) -> bool:
    """An add-style command (any add verb) - enough to resolve a product and start the add flow."""
    return classify_add_message(message) == "command"


def is_explicit_add_command(message: str) -> bool:
    """The shopper plainly told us to add it - the only message shape allowed to execute without a proposal."""
    return classify_add_message(message) == "command" and bool(_STRONG_ADD.search(message or ""))


_PLURAL = re.compile(r"\b(?:both|all|each|every|and|plus|two|three|four|these|those|them)\b|,", re.IGNORECASE)
_POSITIONAL = re.compile(
    r"\b(?:first|second|third|fourth|last|1st|2nd|3rd|top|cheapest|cheaper|priciest|most expensive|lightest|"
    r"heaviest|biggest|smallest|former|latter)\b",
    re.IGNORECASE,
)
_TITLE_NOISE = {"the", "and", "for", "with", "pack", "set"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in _TITLE_NOISE}


def reference_is_ambiguous(message: str, chosen_titles: list[str], n_in_view: int, chosen_is_sole_view: bool = False) -> bool:
    """Did the shopper's own words leave the product unclear? (pure)

    The model may have picked a product for "add it"; that pick is a guess and must never execute on its own. A
    direct add is allowed only when the message names the chosen product (a distinctive title word), points at it
    positionally ("the second one"), asks for several at once ("both"), or the chosen product is the ONLY one in
    view (so "it" can only mean that one). Otherwise the shopper is asked which one.
    """
    text = message or ""
    if _POSITIONAL.search(text):
        return False
    if len(chosen_titles) > 1:
        return not _PLURAL.search(text)
    if _tokens(text) & _tokens(" ".join(chosen_titles)):
        return False
    return not (chosen_is_sole_view and n_in_view == 1)
