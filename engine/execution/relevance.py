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


def _overlap(words: list[str], text: str) -> int:
    """How many of `words` appear in `text`, singular or plural.

    The same "count both forms, do not punish a plural" rule
    sample_merchant/store.py's own search already applies - kept identical
    here so this re-score never disagrees with the search it is checking.
    """
    return sum(1 for w in words if w in text or w.rstrip("s") in text)


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


def query_diverges_from_said(query: str, raw_said: str) -> bool:
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

    Deliberately conservative in two ways, the second found live after the
    first version of this function shipped and immediately regressed a
    working case: (1) only fires when there is at least one significant
    word on each side to compare (an empty `query` or a `raw_said` with
    nothing significant in it says nothing about divergence either way);
    (2) only fires when `query` is SHORT (at most two significant words).
    A short, generic proxy term ("water", "water bottle") diverging from
    what the shopper said is the actual signature of a wrong guess. A
    longer query - three or more significant words - diverging from the
    raw sentence is exactly what a model naming a SPECIFIC real product by
    its own title looks like ("Ethiopia Guji Natural" for "a fruity
    coffee") - a genuinely correct, specific citation that will never
    share words with the shopper's generic phrasing, and must not be
    hedged as if it were a coincidental guess. Reproduced live: without
    this length cap, the exact-title-match case above was flagged and
    given a "couldn't confirm an exact match" disclosure on a fully
    correct answer, 5/5 trials - a real, caught regression, not a
    hypothetical one.
    """
    query_words = set(_significant_words(query))
    said_words = set(_significant_words(raw_said))
    if not query_words or not said_words or len(query_words) > 2:
        return False
    return query_words.isdisjoint(said_words)


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
    return any(_overlap(words, _product_text(p)) > 0 for p in catalog_sample)


def _categories_of(p: Product | dict) -> list[str]:
    if isinstance(p, dict):
        return list(p.get("categories") or [])
    return list(p.categories or [])
