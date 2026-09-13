"""A narrow, deterministic supplement to the model's own structured-state
extraction - not a second general-purpose NLP system.

Hardening-pass finding (see PROGRESS.md): the model's own `shopping_state`
extraction misses an obvious, explicit priority/requirement statement on a
real fraction of turns - independently reproduced multiple times, including
on phrasing as unambiguous as "I only care about being as lightweight as
physically possible, nothing else matters." When that happens, the
downstream synthesis pass can still write a confident "since you
prioritized X" sentence by re-reading the raw message itself, producing a
claim the deterministic ranking mechanism never actually acted on - the
central defect this hardening pass exists to close.

This module does not replace the model's extraction and does not attempt to
understand shopper intent in general. It recognises a small, explicit set
of unambiguous English trigger phrases ("prioritize", "only care about",
"must be", "needs to be", "X matters most") and extracts the short clause
that follows, stripped of a fixed filler-word list. It folds any match into
the SAME `shopping_state` update shape `engine/db/shopping_state.py::merge`
already accepts (so an existing model-supplied value is never overwritten
by this - `chat.py` merges the two by union, not replacement; see its own
call site). A message that doesn't match any trigger here contributes
nothing - this is a supplement for the specific failure mode found live,
not a substitute for the model on prose it can't parse.
"""

from __future__ import annotations

import re

#: Ends a clause - the capture stops at the first of these, or the end of
#: the message. A bare "for" is included, not just "for now" - "prioritize
#: lightweight for running" should capture "lightweight", not "lightweight
#: for running": in ordinary English the priority target is stated before
#: any trailing "for X" purpose clause.
_CLAUSE_END = re.compile(
    r"[.!?,;]|(?:\s-\s)|(?:\binstead\b)|(?:\bover\b)|(?:\bfor\b)"
    r"|(?:\btoday\b)|(?:\bfrom now on\b)|(?:\bnothing else matters\b)"
)

#: Stripped from the edges of a capture (repeatedly) before it is kept -
#: filler that adds no matchable content and would otherwise poison the
#: keyword-stem match in engine/execution/mission_ranking.py by demanding a
#: word ("physically", "possible") no merchant description will ever use.
_FILLER_WORDS = {
    "being", "being.", "now", "today", "please", "at", "all", "physically",
    "really", "very", "extremely", "totally", "completely", "possible",
    "as", "so", "that", "much", "just", "only", "a", "an", "the",
}

#: Words too generic to ever be worth capturing as a standalone priority/
#: requirement on their own.
_STOP_PHRASES = {"", "this", "that", "it", "one", "something", "anything"}

#: A capture is capped to a short phrase - this is meant to catch "X" in
#: "prioritize X", not swallow an entire trailing sentence. Four content
#: words (after filler-stripping) is generous for an adjective/short noun
#: phrase and rejects a capture that ran past the intended target.
_MAX_CAPTURE_WORDS = 4

#: (trigger substring, capture-is-before-trigger). Deliberately plain
#: substrings, not regexes - simpler to reason about, and every one of
#: these is itself unambiguous enough not to need anchoring.
_PRIORITY_TRIGGERS: list[tuple[str, bool]] = [
    ("prioritize", False),
    ("prioritise", False),
    ("only care about", False),
    ("nothing else matters", False),
]
_PRIORITY_SUFFIX_TRIGGERS: list[str] = [
    "matters most",
    "matters more than anything",
    "is the priority",
    "is my priority",
    "is the only thing that matters",
]

_MUST_HAVE_TRIGGERS: list[tuple[str, bool]] = [
    ("must be", False),
    ("must have", False),
    ("needs to be", False),
    ("need to be", False),
]


def _strip_filler(text: str) -> str:
    words = text.split()
    while words and words[0].lower().strip(".,!?") in _FILLER_WORDS:
        words.pop(0)
    while words and words[-1].lower().strip(".,!?") in _FILLER_WORDS:
        words.pop()
    return " ".join(words)


def _clean(capture: str) -> str | None:
    text = _strip_filler(capture.strip().strip(".,!?").strip())
    if not text or text.lower() in _STOP_PHRASES:
        return None
    if len(text.split()) > _MAX_CAPTURE_WORDS:
        return None
    return text


def _capture_after(message: str, trigger: str) -> str | None:
    lowered = message.lower()
    idx = lowered.find(trigger)
    if idx == -1:
        return None
    rest = message[idx + len(trigger):]
    end = _CLAUSE_END.search(rest)
    clause = rest[: end.start()] if end else rest
    return _clean(clause)


def _capture_before(message: str, trigger: str) -> str | None:
    lowered = message.lower()
    idx = lowered.find(trigger)
    if idx == -1:
        return None
    head = message[:idx]
    # The clause starts after the last clause-ending delimiter before the
    # trigger, or the start of the message.
    last_end = None
    for m in _CLAUSE_END.finditer(head):
        last_end = m
    clause = head[last_end.end():] if last_end else head
    return _clean(clause)


def extract(message: str | None) -> dict[str, list[str]]:
    """Return `{"priorities_add": [...], "must_have_add": [...]}` - either
    or both lists empty when nothing unambiguous was found. Shape matches
    the keyword arguments `engine/db/shopping_state.py::merge` already
    accepts, so a caller can splat this straight in alongside the model's
    own extracted fields.
    """
    if not message:
        return {}

    priorities: list[str] = []
    must_haves: list[str] = []

    for trigger, _ in _PRIORITY_TRIGGERS:
        found = _capture_after(message, trigger)
        if found:
            priorities.append(found)
    for trigger in _PRIORITY_SUFFIX_TRIGGERS:
        found = _capture_before(message, trigger)
        if found:
            priorities.append(found)

    for trigger, _ in _MUST_HAVE_TRIGGERS:
        found = _capture_after(message, trigger)
        if found:
            must_haves.append(found)

    # De-dup case-insensitively within this one message - the same trigger
    # phrasing (e.g. both "prioritize" and "prioritise" appearing, unlikely
    # but possible in a pasted message) should not double-add.
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            key = item.lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    out: dict[str, list[str]] = {}
    if priorities:
        out["priorities_add"] = _dedup(priorities)
    if must_haves:
        out["must_have_add"] = _dedup(must_haves)
    return out


#: Explicit-retraction phrasing for scalar mission fields - the deterministic
#: half of the item-4 scalar-clearing fix. The model can also emit
#: `clear_fields` itself (see prompts.py's schema); this is a backstop for
#: the same reason the priority/must-have patterns above are, and folds into
#: the same `clear_fields` list `shopping_state.py::merge` reads.
_BUDGET_CLEAR_PATTERNS = [
    re.compile(r"\bbudget (?:doesn'?t|does not) matter\b", re.I),
    re.compile(r"\bforget (?:the |about )?(?:the )?budget\b", re.I),
    re.compile(r"\bno budget\b", re.I),
    re.compile(r"\bany budget\b", re.I),
    re.compile(r"\bregardless of (?:the )?price\b", re.I),
    re.compile(r"\bbudget doesn'?t matter\b", re.I),
]


def extract_clears(message: str | None) -> list[str]:
    """Which scalar `ShoppingState` fields this message explicitly retracts,
    deterministically. Currently only budget - see `_BUDGET_CLEAR_PATTERNS`.
    """
    if not message:
        return []
    for pattern in _BUDGET_CLEAR_PATTERNS:
        if pattern.search(message):
            return ["budget_amount", "budget_currency"]
    return []
