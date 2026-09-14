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


def _significant_words(text: str) -> list[str]:
    return [w for w in text.lower().split() if len(w) > _MIN_WORD_LEN]


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
    words = _significant_words(query)
    if not words:
        return products
    scored = [(_overlap(words, _product_text(p)), p) for p in products]
    scored = [(s, p) for s, p in scored if s > 0]
    if not scored:
        return []
    top = max(s for s, _ in scored)
    return [p for s, p in scored if s == top]
