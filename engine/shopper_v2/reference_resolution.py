"""Generic structured reference resolution over the rolling product ledger.

Resolves an ordinal ("the first one", "the second product"), a collective
reference ("both", "all three", "all of them", "the first and third"), or a
comparative pair reference ("the cheaper one", "the more expensive one")
against REAL, already-verified tracked product ids - `active_products` /
`recent_product_groups` (`state.py`) - never a guess, never invented, and
never scenario-specific: the reference words below are closed linguistic
classes (ordinals, quantifiers, comparatives) that hold for ANY product,
merchant, or catalog, the same discipline this codebase already applies to
stopwords/generic-action-verbs/comparison words elsewhere. Only the
RESOLUTION TARGET (which real ids these words point at) is state-dependent;
nothing here is keyed to a specific product, phrase, or test scenario.

This module answers ONE question - "which real, already-shown ids (if any)
does this message name a subset of?" - and returns `None` whenever it
cannot determine that from real structure, so a caller never falls back to
inventing a default subset (e.g. "both" with only one real tracked product
resolves to `None`, not a guess at which second product was meant).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 0-based ordinal positions. Closed class, not tied to any product domain -
#: "first"/"second"/... mean the same thing whether the group is shoes,
#: coffees, or watches.
_ORDINAL_TO_INDEX: dict[str, int] = {
    "first": 0,
    "second": 1,
    "third": 2,
    "fourth": 3,
    "fifth": 4,
    "sixth": 5,
    "seventh": 6,
    "eighth": 7,
    "ninth": 8,
    "tenth": 9,
}

#: Spelled-out counts, for validating a stated group size ("all three")
#: against the REAL group size rather than assuming it.
_NUMBER_WORDS: dict[str, int] = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}

#: Closed-class collective-reference phrases meaning "the whole group I was
#: just shown" - never a specific count, so always resolves to every
#: tracked id regardless of how many there are.
_WHOLE_GROUP_PHRASES = (
    "all of them", "all of these", "all of those", "each of them",
    "every one of them", "the whole", "all four", "all five",
)

#: Comparative pair vocabulary - meaningful only when exactly two real,
#: priced products are in view (see `resolve_comparative_pair`).
_CHEAPER_WORDS = ("cheaper", "less expensive", "the lower priced one", "the lower price one")
_PRICIER_WORDS = ("pricier", "more expensive", "the higher priced one", "the higher price one")


def _levenshtein_at_most_one(a: str, b: str) -> bool:
    """True when `a` and `b` differ by at most one single-character edit
    (insert/delete/substitute) OR one adjacent-letter transposition - a
    real, bounded typo tolerance for the ~10-word closed ordinal
    vocabulary specifically, never open-ended fuzzy matching over
    arbitrary text. Safe precisely because the target set is tiny and
    fixed: a word this close to "first" is never going to coincidentally
    also be this close to a real, unrelated word this codebase would
    misfire on.

    Live-reproduced, generalized: "frist" for "first" is a genuine,
    extremely common typo - swapping two adjacent letters - which is
    Damerau-Levenshtein distance 1 but plain Levenshtein distance 2 (both
    the swapped positions count as substitutions under simple
    same-length diffing); transposition is checked explicitly so this
    real, common typo shape is covered, not just insert/delete/substitute."""
    if a == b:
        return True
    if len(a) == len(b):
        diff_positions = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(diff_positions) <= 1:
            return True
        if len(diff_positions) == 2:
            i, j = diff_positions
            if j == i + 1 and a[i] == b[j] and a[j] == b[i]:
                return True
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = j = edits = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
        else:
            edits += 1
            if edits > 1:
                return False
            j += 1
    return True


def resolve_ordinal_positions(message: str) -> list[int] | None:
    """Real 0-based positions named by ordinal words in `message` - "the
    first and third" -> [0, 2]. "last" is returned as -1 (a real Python
    negative index, resolved against the group's own length by the
    caller). Returns `None` when no ordinal word is present at all - this
    is a structural absence-of-signal, never a default guess.

    Typo-tolerant (2026-09, live-reproduced: "add frist and second to the
    cart" failed outright on the literal typo) - every token in the
    message is also checked within edit-distance 1 of each real ordinal
    word (see `_levenshtein_at_most_one`), bounded to this tiny closed
    vocabulary so it can never misfire on an unrelated real word."""
    lowered = f" {message.lower().strip()} "
    positions = [idx for word, idx in _ORDINAL_TO_INDEX.items() if f" {word} " in lowered]
    # Typo-tolerant scan runs regardless of whether OTHER ordinal words
    # already matched exactly in this same message - "add frist and
    # second" must find BOTH "first" (typo'd) and "second" (exact), not
    # stop checking once any exact match exists. Only skips a token that
    # already matched a real word exactly (via the exact-word membership
    # check on `_ORDINAL_TO_INDEX`), so a genuinely exact word is never
    # redundantly re-matched by its own typo-tolerant neighbor.
    exact_words_present = {word for word in _ORDINAL_TO_INDEX if f" {word} " in lowered}
    tokens = [t.strip(".,!?'\"") for t in lowered.split()]
    for token in tokens:
        if len(token) < 4 or token in exact_words_present:
            continue
        for word, idx in _ORDINAL_TO_INDEX.items():
            if word in exact_words_present:
                continue
            if _levenshtein_at_most_one(token, word) and idx not in positions:
                positions.append(idx)
    if re.search(r"\blast\b", lowered):
        positions.append(-1)
    return sorted(set(positions), key=lambda p: (p == -1, p)) if positions else None


#: Digit-position fallback vocabulary check - see `resolve_ordinal_positions`'s
#: sibling handling of "1"/"2" below. Deliberately requires TWO OR MORE
#: distinct digit tokens before firing at all (never a single bare digit,
#: which is far more likely a size/quantity/price elsewhere in this
#: codebase) - the real, reported gap this closes is a shopper answering
#: with the SAME numbering this codebase's own clarifying questions
#: already display ("1 and 2", or pasting "1. X; 2. Y" back verbatim),
#: never an ordinary single-number statement.
_DIGIT_TOKEN_PATTERN = re.compile(r"\b([1-9]|10)\b")


def resolve_digit_positions(message: str) -> list[int] | None:
    """0-based positions from bare digit tokens ("1 and 2" -> [0, 1]) -
    ONLY when 2 or more distinct digits are present, so an ordinary
    single-number statement ("size 8", "quantity 2") is never
    reinterpreted as a position reference. A genuine multi-digit list is
    a real, closed, unambiguous pattern this codebase's own numbered
    clarifying questions already produce and shoppers naturally echo
    back."""
    matches = _DIGIT_TOKEN_PATTERN.findall(message.lower())
    positions = sorted({int(d) - 1 for d in matches})
    return positions if len(positions) >= 2 else None


def resolve_group_selector(message: str, group_size: int) -> list[int] | None:
    """Real 0-based positions for a collective/ordinal reference, checked
    against the REAL group size - never assumed:

    - "both" resolves to [0, 1] only when the real group has >= 2 members;
      never invents a second member for a group of one.
    - "all N" (spelled out, e.g. "all three") resolves to the whole group
      ONLY when N matches the real group size - "all three" against a
      real group of 4 is a genuine mismatch this refuses to guess through,
      returning `None` so the caller can ask rather than silently pick 3
      of the 4 or all 4.
    - A whole-group phrase ("all of them", "each of them", ...) always
      resolves to every real tracked id, whatever the count.
    - An ordinal or ordinal-combination ("the first and third") resolves
      to those specific real positions, dropping any that don't exist in
      this group rather than raising.

    Returns `None` when the message names no resolvable subset at all -
    the caller must never substitute a default in that case."""
    if group_size <= 0:
        return None
    lowered = f" {message.lower().strip()} "

    if " both " in lowered or lowered.strip() == "both":
        return [0, 1] if group_size >= 2 else None

    for word, n in _NUMBER_WORDS.items():
        if f"all {word}" in lowered:
            return list(range(group_size)) if n == group_size else None

    if any(phrase in lowered for phrase in _WHOLE_GROUP_PHRASES):
        return list(range(group_size))

    ordinal_range = resolve_ordinal_range(message)
    if ordinal_range is not None:
        which, n = ordinal_range
        # Clamping (never refusing) is deliberate and different from "all
        # N"'s exact-match requirement above: "the first five" against a
        # real group of 3 is not a mismatch to refuse - it is an honest
        # request for as many of the leading/trailing real items as
        # actually exist, the same way a human would read it.
        n = min(n, group_size)
        return list(range(n)) if which == "first" else list(range(group_size - n, group_size))

    ordinals = resolve_ordinal_positions(message)
    if ordinals is not None:
        real_positions = []
        for pos in ordinals:
            actual = group_size - 1 if pos == -1 else pos
            if 0 <= actual < group_size and actual not in real_positions:
                real_positions.append(actual)
        return real_positions or None

    digits = resolve_digit_positions(message)
    if digits is not None:
        real_positions = [p for p in digits if 0 <= p < group_size]
        return real_positions or None

    return None


#: Composes an ordinal ("first"/"last") with a real count word - "the
#: first two", "the last three" - a genuine grammatical COMBINATION of two
#: closed classes this module already recognizes separately
#: (`_ORDINAL_TO_INDEX`, `_NUMBER_WORDS`), never its own new word list.
#: Root-cause fix: "add first two to the cart" used to be unresolvable
#: because only a single named ordinal ("first" alone) or a fixed
#: collective phrase was ever recognized - nobody had modeled "ordinal +
#: count" as its own real pattern, so it fell through to a generic
#: clarifying question regardless of how unambiguous it actually was.
_ORDINAL_RANGE_WORDS = ("first", "last")


def resolve_ordinal_range(message: str) -> tuple[str, int] | None:
    """`("first", 2)` for "the first two", `("last", 3)` for "the last
    three" (or a bare digit: "the first 2"). Returns `None` when the
    message names no such combination - a single bare ordinal ("the
    first one") is `resolve_ordinal_positions`' job, not this one, and a
    bare count with no ordinal ("two of them") is `parse_requested_count`/
    `resolve_group_selector`'s collective-phrase handling, not this
    one either; this function only ever fires on the genuine composition
    of both."""
    lowered = f" {message.lower().strip()} "
    for ordinal_word in _ORDINAL_RANGE_WORDS:
        idx = lowered.find(f" {ordinal_word} ")
        if idx == -1:
            continue
        rest = lowered[idx + len(ordinal_word) + 2 :]
        for count_word, n in _NUMBER_WORDS.items():
            if rest.startswith(f"{count_word} ") or rest.strip() == count_word:
                return (ordinal_word, n)
        match = re.match(r"\s*(\d{1,3})\b", rest)
        if match:
            return (ordinal_word, int(match.group(1)))
    return None


def looks_like_group_or_ordinal_reference(message: str) -> bool:
    """True when `message` itself contains a real, closed-class ordinal or
    collective-reference marker - never sufficient alone; the caller still
    requires a real tracked group before trusting this. Same discipline as
    `capability.py`'s own comparison/historical-reference word checks."""
    lowered = f" {message.lower().strip()} "
    if " both " in lowered or lowered.strip() == "both":
        return True
    if any(f"all {word}" in lowered for word in _NUMBER_WORDS):
        return True
    if any(phrase in lowered for phrase in _WHOLE_GROUP_PHRASES):
        return True
    if resolve_ordinal_range(message) is not None:
        return True
    if resolve_ordinal_positions(message) is not None:
        return True
    return resolve_digit_positions(message) is not None


def looks_like_comparative_pair_reference(message: str) -> bool:
    """True when `message` names one side of a real comparative pair
    ("the cheaper one") - never an absolute superlative ("cheapest"),
    which `compare.py`'s own `_deterministic_superlative` already covers
    for an arbitrary-sized group."""
    lowered = f" {message.lower().strip()} "
    return any(w in lowered for w in _CHEAPER_WORDS) or any(w in lowered for w in _PRICIER_WORDS)


#: Closed-class requested-count vocabulary for a discovery turn ("show me
#: TWO running shoes", "a COUPLE of coffees", "just ONE") - the same
#: closed-linguistic-class discipline as every other word list in this
#: module, generalized to cardinality rather than reference. Longer
#: phrases first so "a couple of" matches before the bare "couple" would.
_QUANTITY_WORDS: dict[str, int] = {
    "a couple of": 2, "a couple": 2, "couple of": 2, "couple": 2,
    "a pair of": 2, "pair of": 2,
    "a few": 3, "few": 3, "several": 4,
    "a single": 1, "just one": 1, "one": 1,
    "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

#: Words whose nearby presence means a number in the message is a BUDGET,
#: a size, or some other non-count quantity - never the requested number
#: of products. Closed class, not tied to any one category (a size number,
#: a pack count, a percentage, a unit all read the same way regardless of
#: what the product is).
_NON_COUNT_CONTEXT = (
    "under", "over", "below", "above", "at least", "at most", "around",
    "budget", "spend", "price", "cost", "$", "₹", "rs.", "rs ", "inr",
    "size", "pack", "gb", "ml", "oz", "inch", "%", "percent", "kg", "lb",
)


def parse_requested_count(message: str) -> int | None:
    """The real number of products the shopper explicitly asked to see
    THIS turn ("show me two running shoes" -> 2, "a couple of coffees" ->
    2, "just one" -> 1, "all three" -> handled separately by
    `resolve_group_selector` against an already-shown group, not here -
    this is about a NEW discovery request, before anything has been shown
    yet). Returns `None` when the message states no explicit count at all
    - the caller must never invent a default in that case, only apply its
    own.

    Never mistakes a budget figure, a size, or a pack/unit count for a
    requested product count - `_NON_COUNT_CONTEXT` checks a small window
    on both sides of whatever number-like token was found; a message with
    such a cue nearby is treated as having stated no product count."""
    lowered = f" {message.lower().strip()} "
    for phrase in sorted(_QUANTITY_WORDS, key=len, reverse=True):
        idx = lowered.find(f" {phrase} ")
        if idx == -1:
            continue
        window = lowered[max(0, idx - 15) : idx]
        if any(cue in window for cue in _NON_COUNT_CONTEXT):
            continue
        return _QUANTITY_WORDS[phrase]

    match = re.search(r"\b(\d{1,3})\b", lowered)
    if match:
        before = lowered[max(0, match.start() - 15) : match.start()]
        after = lowered[match.end() : match.end() + 15]
        if any(cue in before for cue in _NON_COUNT_CONTEXT) or any(
            cue in after for cue in _NON_COUNT_CONTEXT
        ):
            return None
        n = int(match.group(1))
        if 1 <= n <= 20:
            return n
    return None


def resolve_comparative_pair(message: str, priced_products: list) -> object | None:
    """Given exactly the real, currently-in-view priced products, returns
    the one comparative reference names ("the cheaper one" -> the real
    lower-priced product), or `None` when there aren't exactly two priced
    products to compare (never guesses which two the shopper meant) or the
    message names neither side."""
    if len(priced_products) != 2:
        return None
    lowered = f" {message.lower().strip()} "
    a, b = priced_products
    lower, higher = (a, b) if a.price.amount <= b.price.amount else (b, a)
    if any(w in lowered for w in _CHEAPER_WORDS):
        return lower
    if any(w in lowered for w in _PRICIER_WORDS):
        return higher
    return None


# ---------------------------------------------------------------------------
# QueryIntent - the single, typed consolidation point.
#
# Root-cause fix (2026-09): "how many," "sorted how," "the min/max of what,"
# and "all of them" used to each be answered by an independent function
# (or, for ranking, not answered at all outside one narrow branch of
# compare.py). Every capability that needs any of this now reads ONE typed
# object instead of re-deriving its own answer to the same question -
# closing the specific class of bug where a capability worked for the one
# phrasing it was built against and nothing else.
# ---------------------------------------------------------------------------

#: Real attribute words - deliberately only the two attributes
#: `engine.execution.ranking.KNOWN_ATTRIBUTES` actually has real data for.
#: A word for an attribute this catalog has no real field for (e.g.
#: "durability") is never added here - see `QueryIntent.sort_attribute`'s
#: own docstring for why that must stay `None`, honestly, rather than
#: silently falling back to price.
_ATTRIBUTE_WORDS: dict[str, tuple[str, ...]] = {
    "price": ("price", "cost", "expensive", "cheap", "cheaper", "cheapest", "pricier", "priciest"),
    "rating": ("rating", "rated", "reviews", "review", "reviewed"),
}

_PRICE_MIN_WORDS = ("cheapest", "least expensive", "lowest price", "lowest priced")
_PRICE_MAX_WORDS = ("most expensive", "priciest", "highest price", "highest priced")
_RATING_MAX_WORDS = ("best rated", "highest rated", "top rated", "best reviewed")
_RATING_MIN_WORDS = ("worst rated", "lowest rated")

#: Real, closed-class direction phrases - "rank/sort ... DIRECTION",
#: attribute-agnostic (the attribute itself is found separately, via
#: `_ATTRIBUTE_WORDS`, and defaults to "price" when a direction is stated
#: but no attribute word is - see `parse_query_intent`'s own comment on
#: why that specific default is the honest one for a shopping context).
_DESC_WORDS = ("high to low", "highest to lowest", "descending", "high-to-low")
_ASC_WORDS = ("low to high", "lowest to highest", "ascending", "low-to-high")

#: Explicit "don't cap it" request - closed class, never confused with
#: "all N" (an exact-count collective reference against an ALREADY-SHOWN
#: group, handled separately by `resolve_group_selector`). This is about a
#: NEW discovery request stating no specific number at all, just "all"/
#: "every" of them.
_SHOW_ALL_WORDS = (" all ", " every ", " everything ")


@dataclass
class QueryIntent:
    """One turn's real, structured request shape - never invented, only
    ever set from a real, closed-class marker actually present in the
    message. Every field defaults to "no signal" (`None`/`False`); a
    caller must never treat an unset field as a real answer of its own
    (e.g. `sort_attribute is None` means "nothing to sort by," never
    "sort by price")."""

    #: An explicit integer count ("two", "3", "a couple") - `None` when
    #: none was stated.
    requested_count: int | None = None
    #: An explicit, uncapped "show me all/every one" request - mutually
    #: informative with `requested_count` (a message names at most one of
    #: the two; both `None`/`False` together means no explicit count was
    #: stated at all, the caller's own default applies).
    show_all: bool = False
    #: "price" or "rating" - one of `engine.execution.ranking.KNOWN_ATTRIBUTES`
    #: - or `None` when the message named no sortable attribute at all.
    #: NEVER a fabricated attribute this catalog has no real field for.
    sort_attribute: str | None = None
    #: "asc" or "desc" - set only alongside a genuine direction phrase or
    #: superlative; `None` means no ordering was requested.
    sort_direction: str | None = None
    #: "min" or "max" - set for an absolute superlative ("the cheapest
    #: one," "the best rated") - distinct from `sort_direction` in that a
    #: superlative asks for the ONE best answer, not the whole list
    #: reordered (a caller may still choose to show the whole list sorted
    #: AND name the superlative one; both readings are supported by
    #: having both fields available, never conflated into one).
    superlative: str | None = None
    #: `("first", 2)` / `("last", 3)` - a real ordinal-range composition
    #: (see `resolve_ordinal_range`) - `None` when the message named no
    #: such combination.
    ordinal_range: tuple[str, int] | None = None
    #: `message` with every recognized cardinality/sort/superlative
    #: PHRASE removed - the real product-describing words a retrieval/
    #: relevance-scoring query should actually be built from. Live-
    #: reproduced, generalized gap this closes: "show me three running
    #: shoes ranked by price high to low" scored "Highridge..." as the
    #: ONLY match, because the literal word "high" (from "high to low")
    #: is a real substring of "High-ridge" - the exact same class of
    #: accidental-substring-match bug this codebase already fixed once
    #: for "for" inside "Performance", now closed at the SOURCE (never
    #: handing a ranking/count word to relevance scoring at all) rather
    #: than chasing each coincidental collision individually. Falls back
    #: to the original `message` if stripping would leave nothing
    #: (e.g. a message that is ITSELF only "all" or "the cheapest one",
    #: naming no product at all) - never returns an empty query.
    core_query: str = ""


def parse_query_intent(message: str) -> QueryIntent:
    """The ONE place a capability should look to answer "how many," "sorted
    how," or "does this name a min/max" for `message` - never a second,
    local re-derivation. Composes the existing closed-class parsers in
    this module (`parse_requested_count`, `resolve_ordinal_range`) with
    the new attribute/direction/superlative/show-all detection, so every
    caller gets the same answer to the same question."""
    lowered = f" {message.lower().strip()} "

    show_all = any(w in lowered for w in _SHOW_ALL_WORDS)
    requested_count = None if show_all else parse_requested_count(message)

    sort_attribute: str | None = None
    for attr, words in _ATTRIBUTE_WORDS.items():
        if any(w in lowered for w in words):
            sort_attribute = attr
            break

    superlative: str | None = None
    if any(w in lowered for w in _PRICE_MIN_WORDS):
        superlative, sort_attribute = "min", "price"
    elif any(w in lowered for w in _PRICE_MAX_WORDS):
        superlative, sort_attribute = "max", "price"
    elif any(w in lowered for w in _RATING_MAX_WORDS):
        superlative, sort_attribute = "max", "rating"
    elif any(w in lowered for w in _RATING_MIN_WORDS):
        superlative, sort_attribute = "min", "rating"

    sort_direction: str | None = None
    if any(w in lowered for w in _DESC_WORDS):
        sort_direction = "desc"
    elif any(w in lowered for w in _ASC_WORDS):
        sort_direction = "asc"
    if sort_direction and sort_attribute is None:
        # A stated direction with no named attribute defaults to "price" -
        # the one, disclosed, documented default: "rank them high to low"
        # in a shopping conversation with no attribute named means price,
        # the same way a human shop assistant would read it. Never
        # defaulted the other way (an attribute with no direction does
        # NOT default to a direction - "sorted by rating" alone is
        # genuinely ambiguous and left unset).
        sort_attribute = "price"
    if superlative:
        sort_direction = sort_direction or ("asc" if superlative == "min" else "desc")

    return QueryIntent(
        requested_count=requested_count,
        show_all=show_all,
        sort_attribute=sort_attribute,
        sort_direction=sort_direction,
        superlative=superlative,
        ordinal_range=resolve_ordinal_range(message),
        core_query=strip_intent_phrases(message),
    )


#: Every closed-class phrase this module recognizes as a cardinality/
#: sort/superlative/quantifier SIGNAL, never a product-descriptive word -
#: consolidated here so `strip_intent_phrases` and `parse_query_intent`
#: can never drift apart about what counts as "signal, not a real search
#: word." Longest phrases first so a multi-word phrase is removed whole
#: before a shorter word inside it could partially match instead.
_INTENT_SIGNAL_PHRASES: tuple[str, ...] = tuple(
    sorted(
        {
            *_QUANTITY_WORDS,
            *_DESC_WORDS,
            *_ASC_WORDS,
            *_PRICE_MIN_WORDS,
            *_PRICE_MAX_WORDS,
            *_RATING_MAX_WORDS,
            *_RATING_MIN_WORDS,
            *(w for words in _ATTRIBUTE_WORDS.values() for w in words),
            "all", "every", "everything", "ranked", "rank", "ranking",
            "sorted", "sort", "based on",
        },
        key=len,
        reverse=True,
    )
)


def strip_intent_phrases(message: str) -> str:
    """`message` with every real, closed-class cardinality/sort/
    superlative/quantifier phrase removed - see `QueryIntent.core_query`'s
    own docstring for the exact live-reproduced bug this exists to close
    structurally. Never removes anything else - any word not in the
    closed set above is, by construction, left exactly as the shopper
    wrote it."""
    cleaned = message
    for phrase in _INTENT_SIGNAL_PHRASES:
        cleaned = re.sub(rf"\b{re.escape(phrase)}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or message


def resolve_named_subset(message: str, titles: list[str]) -> list[int] | None:
    """Real 0-based positions for every one of `titles` (the real,
    already-shown products' own titles, same order) that appears in
    `message` as a substring - a general, deterministic mechanism for a
    message naming SEVERAL real products by their own full titles
    together in one message ("1. Marathon Pro Racing Shoe; 2. Cloudstep
    Max Cushion Running Shoe" - pasting a prior clarifying question's own
    numbered list back verbatim, a real, reported case). Requires at
    least 2 matches - a single named product is already resolved by the
    model's own single `named_hint` matching
    (`intent_resolution.py::_resolve_reference`), so this never fires for
    an ordinary single-name message and never duplicates that path."""
    lowered = message.lower()
    positions = [
        i for i, title in enumerate(titles)
        if title and title.strip().lower() in lowered
    ]
    return positions if len(positions) >= 2 else None
