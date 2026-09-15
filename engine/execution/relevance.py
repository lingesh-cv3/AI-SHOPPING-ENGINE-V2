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
    declared `categories` field, aggregated across `catalog_sample` (the
    same wide, unfiltered browse the caller already fetches for this
    fallback - no new data model, no new fetch). "A fruity coffee" and
    "coffee" both overlap Kettle's real "Coffee" category even though
    neither is a literal title match; "kayak" overlaps nothing in a running-
    shoe or coffee shop's real category set. Also checks description text
    (broader than titles alone) for the same reason category alone might
    still miss a genuine but undercategorized product.

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
