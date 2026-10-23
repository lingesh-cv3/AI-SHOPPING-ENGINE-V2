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
#: Advice asked in the middle of a sentence ("do you recommend I buy it?", "you think I should add it?").
_ADVICE_ANYWHERE = re.compile(
    r"\b(?:should i|do you (?:think|recommend|suggest)|would you (?:recommend|suggest)|you think|any reason|is it worth|what happens|what would happen|happens when|happen if|when i add|after i add|before i add|once i add)\b",
    re.IGNORECASE,
)
#: Polite requests TO the assistant ("can you add it to my cart?") stay commands even though they end in "?".
_POLITE_REQUEST = re.compile(
    r"^\s*(?:(?:ok|okay|so|and|please)[,\s]+)*(?:can|could|will)\s+you\b"
    r"(?=.*(?:\bplease\b|\b(?:my|the|our)\s+(?:cart|basket|bag)\b|\bfor me\b))",
    re.IGNORECASE,
)
#: Single-quoted words ("the button says 'add to cart'"); the quote must open after whitespace so apostrophes
#: inside words (I'll, it's) never pair up.
_SINGLE_QUOTED = re.compile(
    r"(?:^|\s)'[^']*\b(?:add|buy|purchase)\b[^']*'(?=\s|[.,!?]|$)", re.IGNORECASE
)
#: Question and explanation words anywhere in the message: asking ABOUT adding is not a command to add, with or without
#: a question mark ("would you buy it", "do you want me to add it", "explain why I should buy it").
_QUESTION_WORDS = re.compile(
    r"\b(?:would you|do you|does it|how to|how do|how can|why|explain|tell me (?:how|why|what)|wonder)\b", re.IGNORECASE
)
_CONDITIONAL = re.compile(r"\b(?:if|unless|in case|supposing|whether|assuming)\b", re.IGNORECASE)


def classify_add_message(message: str) -> str:
    """"command" | "not_a_command" | "none"."""
    text = message or ""
    if not ADD_VERB.search(text) or NOT_AN_ADD.search(text):
        return "none"
    if (_NEGATION.search(text) or _ADVICE_OPENER.search(text) or _CONDITIONAL.search(text)
            or _DEFERRAL.search(text) or _QUOTED.search(text) or _SINGLE_QUOTED.search(text)
            or _ADVICE_ANYWHERE.search(text) or _QUESTION_WORDS.search(text)):
        return "not_a_command"
    if text.rstrip().endswith("?") and not _POLITE_REQUEST.search(text):
        return "not_a_command"
    return "command"


def names_an_add(message: str) -> bool:
    """An add-style command (any add verb) - enough to resolve a product and start the add flow."""
    return classify_add_message(message) == "command"


def is_explicit_add_command(message: str) -> bool:
    """The shopper plainly told us to add it - the only message shape allowed to execute without a proposal."""
    return classify_add_message(message) == "command" and bool(_STRONG_ADD.search(message or ""))


_ORDINALS = {"first": 0, "1st": 0, "former": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3}
_LAST = re.compile(r"\b(?:last|latter)\b", re.IGNORECASE)
_CHEAPEST = re.compile(r"\b(?:cheapest|lowest priced|least expensive)\b", re.IGNORECASE)
_PRICIEST = re.compile(r"\b(?:priciest|most expensive|highest priced)\b", re.IGNORECASE)
#: Positional words we cannot check against catalogue data - they never waive the question.
_UNVERIFIABLE = re.compile(r"\b(?:cheaper|lighter|lightest|heavier|heaviest|bigger|biggest|smaller|smallest|better|best|nicer)\b", re.IGNORECASE)
_GROUP_ALL = re.compile(r"\b(?:all|them|these|those|each|every)\b", re.IGNORECASE)
_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "2": 2, "3": 3, "4": 4}
_FIRST_N = re.compile(r"\bfirst\s+(two|three|four|2|3|4)\b", re.IGNORECASE)
_LAST_N = re.compile(r"\blast\s+(two|three|four|2|3|4)\b", re.IGNORECASE)
_TITLE_NOISE = {"the", "and", "for", "with", "pack", "set", "of"}


def _tokens(text: str) -> set[str]:
    """Words of 4+ letters with a trailing plural 's' removed ("socks" == "sock")."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _TITLE_NOISE:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        if len(w) >= 4:
            out.add(w)
    return out


def _named(message: str, chosen: tuple, candidates: list, verified_ids: frozenset) -> bool:
    """Does the shopper's message identify THIS product?

    A product in view is identified by a word that no OTHER candidate in view shares ("coffee", "running", "shoe" identify
    nothing). A product that is not in view is identified only if the catalogue itself, asked with the shopper's own
    words, names exactly that product (`verified_ids`, computed by the caller); a title word alone never does, because
    the catalogue may hold several products that share it."""
    cand_ids = {v[0] for v in candidates}
    if chosen[0] not in cand_ids:
        return chosen[0] in verified_ids
    others = set()
    for v in candidates:
        if v[0] != chosen[0]:
            others |= _tokens(v[1])
    return bool((_tokens(chosen[1]) - others) & _tokens(message))


def reference_is_ambiguous(message: str, chosen: list, view: list, candidates: list | None = None,
                           verified_ids: frozenset = frozenset()) -> bool:
    """Do the shopper's own words fail to identify the product(s) the model chose? (pure)

    `chosen`, `view` and `candidates` are lists of (product_id, title, price_or_None). `view` is what is on screen in
    display order (positions, "both", "all of them" refer to it); `candidates` is everything "it" could mean (on screen
    plus the active topic), defaulting to `view`. `verified_ids` are chosen products the catalogue itself confirmed the
    message names uniquely. The model's pick is a guess and never authorizes an add on its own: it is accepted only
    when CHECKED against the message - a distinguishing word, a verified catalogue name, a positional/price reference
    the pick actually satisfies, a group word and the pick is exactly that group, or it is the only candidate.
    Anything else - "add it", "add the coffee" with two coffees in view, "add them" with five in view - asks.
    """
    text = message or ""
    if not chosen:
        return True
    view = list(view)
    candidates = list(view if candidates is None else candidates)
    ids = [c[0] for c in chosen]
    view_ids = [v[0] for v in view]
    if len(chosen) == 1:
        c = chosen[0]
        if _named(text, c, candidates, verified_ids):
            return False
        if [v[0] for v in candidates] == [c[0]]:
            return False
        if _UNVERIFIABLE.search(text):
            return True
        for word, idx in _ORDINALS.items():
            if re.search(r"\b" + re.escape(word) + r"\b", text, re.IGNORECASE):
                return not (idx < len(view) and view_ids[idx] == c[0])
        if _LAST.search(text):
            return not (view and view_ids[-1] == c[0])
        for rx, pick in ((_CHEAPEST, min), (_PRICIEST, max)):
            if rx.search(text):
                prices = [v[2] for v in view]
                if not view or any(p is None for p in prices):
                    return True
                target = pick(prices)
                return not (prices.count(target) == 1 and view[prices.index(target)][0] == c[0])
        return True
    # several chosen: each identified, or a group reference the pick matches exactly
    if all(_named(text, c, candidates, verified_ids) for c in chosen):
        return False
    m = _FIRST_N.search(text)
    if m:
        return ids != view_ids[: _NUMBER_WORDS[m.group(1).lower()]]
    m = _LAST_N.search(text)
    if m:
        n = _NUMBER_WORDS[m.group(1).lower()]
        return ids != view_ids[-n:]
    if re.search(r"\bboth\b", text, re.IGNORECASE):
        return not (len(view_ids) == 2 and set(ids) == set(view_ids))
    if _GROUP_ALL.search(text):
        return not (len(view_ids) >= 2 and set(ids) == set(view_ids))
    return True
