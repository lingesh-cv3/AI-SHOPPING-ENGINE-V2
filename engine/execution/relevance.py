"""Deterministic product-relevance validation for RECOMMEND_PRODUCTS.

An adapter's own `search_products(query)` already does its own keyword
matching (per-platform, sometimes on titles only, sometimes on more) and
that matching is deliberately loose - a single shared word is enough for a
product to come back at all, because a shop titled by varietal ("Ethiopia
Guji Natural") or by generic name ("Sneaker" vs a shopper's "trainers")
needs a wide net to find anything real at all. See
engine/execution/service.py's own long comment on the Kettle "fruity
coffee" regression a stricter net once caused.

What that net does not do is validate that what came back is *actually
what was asked for* rather than merely sharing one word with it. "Running
Cap" and "Running Shoe" both contain "running"; only one is a shoe. This
module re-scores whatever the adapter already returned, using the same
kind of literal word-overlap the adapter used to find it in the first
place - no external knowledge, nothing platform-specific, nothing
hardcoded to any product or category - and keeps only the *best-matching
tier*: products tied for the highest number of shared significant words
with the shopper's query. A product sharing one word out of three no
longer rides along just because it cleared the price filter; it has to be
at least as good a match as the best thing the search actually found.

This is deliberately not semantic understanding. It cannot tell that
"trainers" means "shoes" if no product's text ever uses the word "shoes"
at all - that gap is inherent to keyword search and is named honestly
elsewhere (see PROGRESS.md). What it fixes is the narrower, always-fixable
problem: among products a keyword search already found, do not let a
weaker match stand in as if it were as good as a stronger one.
"""

from __future__ import annotations

from shared.models import Product

#: Same floor engine/execution/service.py and sample_merchant/store.py both
#: already use for "a real word, not filler" - kept consistent so a word
#: this module ignores is a word the search itself would have ignored too.
_MIN_WORD_LEN = 2

#: EIGHTH-gap fix: common English function words that pass the length
#: floor above (long enough to look "significant") but carry no product
#: intent - found live when the RECOMMEND_PRODUCTS "rescue search" branch
#: (engine/execution/service.py, fires when the model leaves `query`
#: blank and searches the shopper's raw sentence instead) scored "do you
#: sell kayaks?" against Northfield's catalog: "you" cleared the length
#: floor and `_overlap`'s substring check (`w in text`) matched it inside
#: ordinary marketing copy containing "your" ("the perfect fit for your
#: daily runs") on several unrelated products, producing a real-looking
#: overlap score of 1 across four different, unrelated items - not a
#: query-specific coincidence but a structural one, since a raw shopper
#: sentence (unlike a deliberate `query` parameter) is full of pronouns
#: and auxiliary verbs like this. `weak_single_word_match`'s own
#: `len(words) <= 1` check assumes a 3-word query already had real
#: multi-word discrimination applied - not true when two of those three
#: words are stopwords doing no actual discriminating. This list is
#: deliberately short and closed-class (pronouns, articles, common
#: auxiliaries) - words that are structurally never a product name or
#: attribute in English, not a per-example patch for one failing query.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "you", "your", "yours", "with", "have",
        "has", "had", "does", "did", "not", "any", "some", "this", "that",
        "these", "those", "can", "could", "would", "will", "sell", "sells",
        "selling", "offer", "offers", "carry", "carries", "got", "get",
        "there", "here", "what", "which", "who", "whom", "about", "into",
        # Same class as the "you"/"do you sell" gap above, found live
        # against a broad category browse: "show me shoes" scored every
        # genuine shoe in the catalog as a weak, top_score==1 match,
        # because "show" and (once past the length floor) request-framing
        # verbs like "need"/"want"/"looking"/"find" counted as
        # "significant" alongside the one real product word "shoes" -
        # inflating the query's apparent word count with words that
        # structurally never discriminate one product from another,
        # exactly the failure mode this list already exists to close.
        # Never a product name or attribute in English, not a per-example
        # patch for one failing sentence.
        "show", "need", "needs", "needed", "want", "wants", "wanted",
        "looking", "look", "find", "finding", "please", "help",
    }
)

#: NINETEENTH-gap fix: generic call-to-action/consumption verbs that appear
#: in almost any product's marketing copy regardless of category ("we pick,
#: you drink", "use daily", "try it today") - too weak, on their own, to
#: establish `shop_carries`'s "does this shop carry anything like this"
#: floor. Deliberately narrower than `_STOPWORDS` (these are real, useful
#: search words in other contexts - a shopper asking "what can I drink
#: here" should still work via the category/title checks that run before
#: this exclusion applies) - excluded only from being SUFFICIENT by
#: themselves in `shop_carries`'s broad full-product-text fallback, the one
#: check loose enough that a single coincidental verb match can make an
#: unrelated shop appear to "carry" nearly anything asked about.
_GENERIC_ACTION_VERBS = frozenset(
    {
        "drink", "drinks", "eat", "eats", "use", "uses", "wear", "wears",
        "try", "tries", "enjoy", "enjoys", "buy", "buys", "want", "wants",
        "need", "needs",
    }
)


def _significant_words(text: str) -> list[str]:
    # Punctuation is stripped here, not just for the stopword check below -
    # found live in the same investigation: a raw sentence like "do you
    # sell kayaks?" kept "kayaks?" (with the trailing "?") as its one
    # actually-meaningful word, and `_overlap`'s substring check can never
    # match "kayaks?" against a product's real, punctuation-free "kayak"/
    # "kayaks" text - silently weakening the one word in the query that
    # should have mattered most, while stopwords with no punctuation
    # attached ("you", "sell") matched cleanly. Stripping first fixes both
    # sides of the same investigation, not just the stopword half.
    words = [w.strip(".,!?'\"") for w in text.lower().split()]
    return [w for w in words if len(w) > _MIN_WORD_LEN and w not in _STOPWORDS]


def _product_text(p: Product | dict) -> str:
    if isinstance(p, dict):
        title = p.get("title") or ""
        description = p.get("description") or ""
        categories = p.get("categories") or []
    else:
        title = p.title or ""
        description = p.description or ""
        categories = p.categories or []
    return " ".join([title, description, " ".join(categories)]).lower()


#: Consonant/digraph endings after which English adds "-es" rather than a
#: bare "-s" (box/boxes, glass/glasses, dress/dresses, brush/brushes,
#: watch/watches). Deliberately narrow and closed-class, the same
#: philosophy as `_STOPWORDS` above - not a general stemmer.
_SIBILANT_ENDINGS = ("s", "x", "z", "ch", "sh")


def _stem(word: str) -> str:
    """Canonical singular-ish form of `word`, covering ordinary English
    "-s" and "-es" plurals only.

    TWELFTH-gap fix: the plural handling this replaces (a bare
    `word.rstrip("s")`, in both `_overlap` and `_is_real_title_citation`)
    only strips a single trailing "s" - correct for "bottles" -> "bottle"
    but wrong for a genuine "-es" plural, where the "e" is also part of
    the suffix, not the stem: "boxes".rstrip("s") -> "boxe", which never
    equals "box". Reproduced live and deterministically against a real
    Kettle catalog title: `_is_real_title_citation({"taster","gift",
    "boxes"}, [Product(title="Taster Gift Box")])` returned False (should
    be True) for exactly this reason - a shopper citing "Taster Gift Box"
    as "gift boxes" was spuriously flagged as a coincidental guess.

    Fixed by first checking for the "-es" pattern specifically (strip
    "es" rather than "s" when the word minus "es" itself ends in a
    sibilant sound - `box`, `glass`, `dress`, `brush`), falling back to a
    single-character "-s" strip otherwise. Deliberately does NOT use
    `str.rstrip("s")` for the fallback either: that method strips every
    trailing "s" it finds, not just one, which would mangle an
    already-singular word ending in a doubled "s" ("glass" ->
    `rstrip("s")` -> "gla") - guarded against explicitly (a word ending
    in "ss" is left alone by the plain "-s" branch; the "-es" branch
    above already gives it the correct stem when a genuine plural form of
    it is seen).

    FIFTEENTH-gap fix: also handles the "-y" -> "-ies" plural
    (category/categories, accessory/accessories, battery/batteries) -
    found live and deterministically against a real Northfield
    department name: "Accessory" (a shopper's ordinary singular of the
    real "Accessories" department) did not stem-match "Accessories",
    since neither the "-es"-after-sibilant rule nor the plain "-s" strip
    fires on a "-y"/"-ies" pair. A word ending "ies" is stemmed to that
    word with "ies" replaced by "y" ("accessories" -> "accessory"); a
    word already ending in a bare "y" is left alone, so the two now
    collapse to the same stem. Deliberately narrow (a fixed length guard
    to avoid mangling a short word that only coincidentally ends "ies")
    rather than a general y/i vowel-shift rule - same closed-class
    philosophy as the rest of this function.
    """
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if (
        word.endswith("es")
        and len(word) > 3
        and word[:-2].endswith(_SIBILANT_ENDINGS)
    ):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 1:
        return word[:-1]
    return word


def _overlap(words: list[str], text: str) -> int:
    """How many of `words` appear in `text`, singular or plural.

    The same "count both forms, do not punish a plural" rule
    sample_merchant/store.py's own search already applies - kept identical
    here so this re-score never disagrees with the search it is checking.
    """
    return sum(1 for w in words if w in text or _stem(w) in text)


def best_matches_with_score(products: list, query: str) -> tuple[int, list]:
    """Same as `best_matches`, but also returns the winning overlap score.

    A score of 0 with a non-empty `products` return never happens (see
    `best_matches`); a score of 0 with an empty return means the query had
    significant words but nothing shared even one. The score itself lets a
    caller distinguish a single-word coincidental hit ("water" matching an
    "Insulated Water Bottle" when the shopper actually asked about kayaks)
    from a genuinely strong match, without hardcoding any word list.
    """
    words = _significant_words(query)
    if not words:
        return len(words), products
    scored = [(_overlap(words, _product_text(p)), p) for p in products]
    scored = [(s, p) for s, p in scored if s > 0]
    if not scored:
        return 0, []
    top = max(s for s, _ in scored)
    return top, [p for s, p in scored if s == top]


def best_matches(products: list, query: str) -> list:
    """Keep only the products that best match `query`'s significant words.

    Returns `products` unchanged if the query has no significant words (an
    empty or filler-only query is not something this can validate - there
    is nothing to check relevance against). Otherwise scores every product
    by word overlap and keeps only those tied for the top score, dropping
    anything scoring zero even if the adapter returned it. An empty
    result means the adapter's own candidates are not a good match for
    what was actually asked - the caller's job, not this function's, to
    decide what to say about that.
    """
    _, matches = best_matches_with_score(products, query)
    return matches


def weak_single_word_match(query: str, top_score: int) -> bool:
    """True when a match's entire justification is one shared word.

    Originally gated on the QUERY having at most one significant word
    ("a query with more than one significant word that only achieves a
    top score of 1 already went through the ordinary multi-word
    discrimination `best_matches` provides"). Found live to be wrong: a
    model that paraphrases a made-up need into a multi-word proxy term
    ("kayak" -> "water sports gear", three significant words) can still
    land on a top score of exactly 1 - only "water" happened to overlap
    "Insulated Water Bottle" - while the other two words matched nothing
    on any product. A top score of 1 is a single coincidental word
    overlap and nothing more, REGARDLESS of how many words the query had;
    "more words were tried and none of them helped" is not
    discrimination, it is the same weak evidence as a one-word query,
    just with padding. Checked directly: a genuine multi-word match - an
    exact product-title citation ("Ethiopia Guji Natural" for "a fruity
    coffee") or an ordinary two-noun phrase ("running shoes") - scores 2
    or higher in practice, because a real match shares more than one
    real word; this only fires when it does not. Used to decide when a
    narrow-search "hit" is weak enough to need the wider `shop_carries`/
    semantic-judge check applied to it too, not just to the zero-match
    browse fallback.
    """
    return top_score == 1


def _is_real_title_citation(query_words: set[str], candidates: list[Product]) -> bool:
    """True when `query_words` is (near-)exactly some single real candidate's
    own title, word for word - the actual signature of a model naming a
    specific product by name, independent of how many words it took.

    TENTH-gap fix: the word-count exemption this replaces ("a query of 3+
    significant words diverging from what the shopper said is a real
    citation") is a proxy, not the real signal - it is defeated by a
    vague, GENERIC multi-word guess ("water sports accessories") that is
    exactly as ungrounded as a one-word guess ("water"), just longer.
    Reproduced live: "I need a kayak" -> query="water sports accessories"
    -> a real but unrelated "Insulated Water Bottle" (its title sharing
    only "water"; "accessories" matched via category, not the title)
    slipped through with zero disclosure because 3 words was enough to
    skip the divergence check entirely, regardless of whether those words
    actually named anything real.

    A first version of this fix checked `query_words <= title_words` (query
    is a SUBSET of the title's words) and immediately regressed a second
    case, found live in the same investigation: "water bottle" (2 words) is
    also a subset of "Insulated Water Bottle 750ml"'s own words, so a
    fragment of the WRONG product's title read as a "citation" of it just
    as easily as the full, correct title of the RIGHT product would have.
    A citation names the product, not a piece of it - so this requires
    NEAR-EQUALITY (symmetric difference of at most one word, to tolerate a
    size/unit suffix like "750ml" the shopper's phrasing would never
    repeat), not mere one-directional containment.

    ELEVENTH-gap follow-up: words are compared after the same plural
    normalization `_overlap` already applies ("count both forms, do not
    punish a plural") - found live that an unnormalized comparison here
    disagreed with `_overlap` in the same file and spuriously hedged a
    genuinely correct citation ("ethiopia guji naturals" for the real
    title "Ethiopia Guji Natural" counted as a 2-word difference - remove
    "natural", add "naturals" - instead of zero).

    TWELFTH-gap follow-up: normalization now goes through the shared
    `_stem` helper (not a bare `.rstrip("s")`) so an ordinary "-es" plural
    ("gift boxes" for the real title "Taster Gift Box") is recognized the
    same way `_overlap` recognizes it - see `_stem`'s own docstring for
    why a plain single-"s" strip cannot do this correctly.

    Deliberately NOT handling ordinary abbreviation ("marathon shoe" for
    "Marathon Pro Racing Shoe") in this same pass: a subset-plus-majority
    rule was tried on paper and immediately reopens the exact "water
    bottle" regression above - {"water", "bottle"} is 2 of that title's 4
    real words, a clear majority, and no check against *other* candidates
    catches it, because it is not ambiguous with a different product, it
    is a generic phrase that happens to fully overlap the wrong one.
    Nothing short of real semantic judgment can tell that apart from a
    deliberate abbreviation of a specific title, so this stays open rather
    than risk reintroducing an already-documented bug for a narrower one.
    """
    if not query_words:
        return False
    normalized_query = {_stem(w) for w in query_words}
    for p in candidates:
        title_words = set(_significant_words(p.title))
        normalized_title = {_stem(w) for w in title_words}
        if len(normalized_query ^ normalized_title) <= 1:
            return True
    return False


#: Deliberately narrow, closed-class phrases (the same philosophy as
#: `_STOPWORDS`/`_GENERIC_ACTION_VERBS` above) that explicitly rule OUT a
#: product a shopper's message otherwise appears to name - not a general
#: negation parser, just the handful of ways a shopper actually says "not
#: this one" in an active shopping conversation.
_EXCLUSION_PHRASES = ("different from", "not the", "other than", "instead of")


def mentions_explicit_exclusion_of(said: str, title: str) -> bool:
    """True when `said` explicitly rules out the product named by `title` -
    e.g. "something different from the Trailblazer" - rather than asking
    for more of it.

    Found live, adversarially: a re-add/repeat-signal match ("another") can
    coincidentally fire on a message that is actually asking for a
    DIFFERENT product than the one already in the cart ("can you also add
    another running shoe, something different from the trailblazer, maybe
    a trail shoe") - the shared word "another" plus the cart line's own
    title words being present is not enough to override an explicit
    "different from X" naming that exact line. Checked by requiring one of
    a short, closed set of exclusion phrases to appear immediately before a
    near-full mention of the title's own significant words - not a general
    negation parser, since that would be its own large, error-prone
    undertaking; this only needs to catch the shopper explicitly ruling out
    the one product a repeat-signal match already found.
    """
    lowered = said.lower()
    title_words = _significant_words(title)
    if len(title_words) < 2:
        return False
    for phrase in _EXCLUSION_PHRASES:
        idx = lowered.find(phrase)
        if idx == -1:
            continue
        # The exclusion only counts if it is followed, within a short
        # window, by the actual title it is excluding - "different from
        # the trailblazer" - not an unrelated "different from usual".
        window = lowered[idx : idx + len(phrase) + 60]
        if sum(1 for w in title_words if w in window) >= 2:
            return True
    return False


def query_diverges_from_said(
    query: str, raw_said: str, candidates: list[Product] | None = None
) -> bool:
    """True when the model's search `query` shares NOT ONE significant word
    with what the shopper actually typed (`raw_said`) - a structural signal
    that the model invented a proxy term rather than searching on the
    shopper's own words, independent of how many words that proxy has.

    EIGHTH-gap fix, second half: `weak_single_word_match` only catches a
    single-word proxy ("kayak" -> "water") scoring exactly 1. Found live
    that a multi-word proxy the model guesses just as wrongly ("kayak" ->
    "water bottle", two significant words) slips past that check entirely,
    since it is not "weak" by word-count even though it is exactly as
    ungrounded in what the shopper actually said. This does not replace
    `weak_single_word_match` - both are checked, combined with `or` - it
    catches the case that check's word-count framing cannot see.

    Only fires when there is at least one significant word on each side to
    compare (an empty `query` or a `raw_said` with nothing significant in
    it says nothing about divergence either way). A genuine, specific
    product-title citation ("Ethiopia Guji Natural" for "a fruity coffee")
    must never be hedged as if it were a coincidental guess - originally
    approximated by a query-length cap (skip anything 3+ words), replaced
    by `_is_real_title_citation` (TENTH gap) after the length cap itself
    was found to exempt an equally-wrong, merely longer, generic guess.
    When `candidates` is not supplied (existing callers unaffected), falls
    back to the original length-cap approximation for backward
    compatibility.

    SIXTEENTH-gap fix: "shares at least one word" (the original
    `isdisjoint` check) is not the same claim as "every word in the
    query is grounded in what the shopper actually said" - and the gap
    between those two claims is exactly how a model-invented word slips
    through. Reproduced live: raw_said "water sports accessories",
    model's own `query` "water bottle" - shares the word "water" with
    what the shopper typed, so the old disjoint check called this
    "not diverging" and never flagged it, while the second word,
    "bottle", is a pure model invention appearing nowhere in what the
    shopper said and happens to be the exact word that made an unrelated
    "Insulated Water Bottle" score high enough (2) to also clear
    `weak_single_word_match`'s threshold. One shared generic word bought
    a free pass for one entirely invented specific word riding beside
    it. Reproduced at 5/5 on this exact phrasing, not a rare coincidence
    - a batch composed mostly of other kayak phrasings had been masking
    the true failure rate of this specific one for several rounds (see
    PROGRESS.md's own after-the-fact correction).

    Fixed by checking SUBSET rather than intersection: divergence now
    fires whenever any of the query's (stemmed) significant words is
    absent from what the shopper (stemmed) actually said - not only when
    none of them are present. A query entirely built from the shopper's
    own words (in any order, any subset) is never flagged, whatever its
    length; a query introducing even one word the shopper never said and
    that isn't part of a verified real title citation is. This does not
    reject the match outright - the caller still runs it through the
    wider sample and the semantic judge before deciding what to say -
    it only stops treating "shares a word" as proof the whole query is
    grounded when only part of it demonstrably is.
    """
    query_words = set(_significant_words(query))
    said_words = set(_significant_words(raw_said))
    if not query_words or not said_words:
        return False
    if candidates is not None:
        if _is_real_title_citation(query_words, candidates):
            return False
    elif len(query_words) > 2:
        return False
    normalized_query = {_stem(w) for w in query_words}
    normalized_said = {_stem(w) for w in said_words}
    return not normalized_query.issubset(normalized_said)


def shop_carries(query: str, catalog_sample: list) -> bool:
    """Does this shop plausibly sell anything like `query` at all?

    Used only to gate the whole-catalog browse fallback in
    engine/execution/service.py, which otherwise substitutes an unrelated
    real product when a keyword search finds nothing. That fallback exists
    because keyword search is too literal on its own (see this module's own
    docstring on the "fruity coffee" / varietal-naming case), so refusing it
    outright whenever the raw query has no title hit would reopen that
    exact regression.

    This checks a wider, already-real signal instead: every product's own
    declared `categories` field, aggregated across `catalog_sample`, plus
    description text (broader than titles alone) for the same reason
    category alone might still miss a genuine but undercategorized product.
    "A fruity coffee" and "coffee" both overlap Kettle's real "Coffee"
    category even though neither is a literal title match; "kayak" overlaps
    nothing in a running-shoe or coffee shop's real category set.

    `catalog_sample` is the caller's job to make as complete as the catalog
    size honestly allows - see `engine/execution/service.py::_full_catalog_sample`,
    which pages through the adapter's own real `total_available` count up to
    an explicit, stated bound rather than a single fixed-size page. This
    function itself has no opinion on catalog size; it only ever sees
    whatever sample it is given, and a query genuinely absent from that
    sample - however large - still reads as "not carried."

    Returns True (never blocks) when the query has no significant words, or
    when `catalog_sample` carries no category data at all - a platform that
    declares no categories has given this function nothing to judge
    relevance against, and refusing on missing data would be worse than not
    checking at all.
    """
    words = _significant_words(query)
    if not words:
        return True
    categories = {c.lower() for p in catalog_sample for c in _categories_of(p)}
    if not categories:
        return True
    category_text = " ".join(categories)
    if _overlap(words, category_text) > 0:
        return True
    # Also check full product text (title+description+categories) across the
    # sample, not just the categories - a real match one word off from the
    # narrow keyword search's own match (already handled above by title) can
    # still show up in a product's description even when no category names it.
    #
    # NINETEENTH-gap fix: found live, reported directly - "do you sell
    # energy drinks?" on Kettle (a coffee roaster) returned True here,
    # because "you drink" appears verbatim in one product's marketing copy
    # ("We pick, you drink. Cancel whenever.") and `_overlap`'s substring
    # check counted that as a real match for the query word "drinks". This
    # is not evidence the shop carries anything remotely like "energy
    # drinks" - it is a generic call-to-action verb ("drink"/"eat"/"use"/
    # "try"/"enjoy"/"get"/"buy"/"have"/"want"/"need") that appears in
    # almost any product's marketing copy regardless of what the product
    # actually is, and on its own in a single-word fallback this weak, it
    # can make an unrelated shop "carry" nearly anything a shopper asks
    # for as long as the word also happens to double as an ordinary verb.
    # Excluded here, not from the general stopword list, since these are
    # real, legitimate SEARCH words elsewhere (a shopper asking "what can
    # I drink here" should still work via the category/title checks above)
    # - only excluded from being sufficient, by themselves, to establish
    # this specific "does the shop carry this at all" floor.
    generic_words = [w for w in words if w not in _GENERIC_ACTION_VERBS]
    if not generic_words:
        return False
    return any(_overlap(generic_words, _product_text(p)) > 0 for p in catalog_sample)


def _categories_of(p: Product | dict) -> list[str]:
    if isinstance(p, dict):
        return list(p.get("categories") or [])
    return list(p.categories or [])
