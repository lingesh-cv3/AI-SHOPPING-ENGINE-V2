"""Store-level discovery: "what do you have?" is a question about the STORE, not a search for a product.

Found in the user's 2026-10-21 manual test: "what do you have?" was run as a literal catalogue search for
those words, found nothing, and answered "I couldn't find anything matching that search" with only a footwear
list. A broad question about what the shop offers gets the shop's real categories instead, as tappable
choices (the widget already renders `category_choices` as buttons that send "Show me <category>").

Discipline (same as the rest of this package):

* The categories are the merchant's own DECLARED departments (`adapter.list_departments()`), or, for a platform
  that declares none, the category names its real products carry - never a list written here. Northfield's
  declared departments are Accessories, Apparel, Footwear, Nutrition, Recovery and Tech; Kettle's are Blends,
  Equipment, Gifts, Single origin and Subscriptions. A merchant that supports only some of those shows only
  those.
* A category is offered only when it has at least one product a shopper could actually buy, so a tap never
  leads to an empty page. The check is one bounded department browse per category.
* A platform that cannot list its catalogue (down, or empty) gets an honest sentence and no choices - never
  an invented list, never a raw platform error.
* Nothing here reads or writes the cart, and nothing is added to the shopper's state: it is a read.

Detection is structural, not a phrase list: a message is store discovery when, after removing a small closed
class of question/offering words, NOTHING is left (any remaining word is a product, category or attribute the
shopper actually named, so it is a search) and at least one word actually asks about the shop's offering.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Words that carry no product meaning in a question about the shop: question words, auxiliaries,
#: pronouns, request framing, and the generic nouns of "what do you have / sell / carry".
_STORE_WORDS = frozenset({
    "what", "whats", "which", "who", "how", "do", "does", "did", "you", "your", "yours", "u", "ya", "we", "us", "me",
    "i", "can", "could", "would", "will", "may", "might", "should", "shall", "is", "are", "was", "were", "be", "been",
    "there", "here", "this", "that", "these", "those", "it", "its", "in", "on", "at", "of", "to", "for", "from",
    "with", "about", "around", "a", "an", "the", "all", "any", "some", "every", "and", "or", "so", "ok", "okay",
    "please", "pls", "hi", "hello", "hey", "now", "today", "right", "currently", "just", "only", "also", "then",
    "show", "tell", "see", "look", "let", "lets", "give", "list", "browse", "explore", "check", "know", "want",
    "like", "need", "find", "shop", "shopping", "store", "stores", "shops", "website",
    "site", "online", "here", "have", "has", "got", "had", "sell", "sells", "selling", "sold", "offer", "offers",
    "offering", "offered", "carry", "carries", "carrying", "stock", "stocks", "stocked", "available", "availability",
    "kind", "kinds", "sort", "sorts", "type", "types", "range", "variety", "selection", "product", "products",
    "item", "items", "thing", "things", "stuff", "goods", "everything", "anything", "something", "catalog",
    "catalogue", "categories", "category", "department", "departments", "section", "sections", "menu", "inventory",
    "else", "more", "other", "others", "much", "many", "lot", "lots", "whole", "entire", "full", "complete",
})
#: At least one of these must be present: the message actually asks about the shop's OFFERING (a bare "hi" or
#: "show me something" does not).
_OFFER_ANCHORS = frozenset({
    "have", "has", "got", "sell", "sells", "selling", "offer", "offers", "offering", "carry", "carries", "carrying",
    "stock", "stocks", "available", "availability", "everything", "anything", "products", "product", "items",
    "stuff", "goods", "catalog", "catalogue", "categories", "category", "departments", "department", "sections",
    "menu", "inventory", "range", "variety", "selection", "browse", "kinds", "types", "sorts", "store", "stores",
    "shop", "shops",
})
#: Empty words ONLY inside a wh-question about the shop ("what can I buy here"). In a command they are cart verbs
#: ("buy it", "get one") and must stay content, so the cart flow keeps its first say.
_WH_WORDS = frozenset({"what", "whats", "which"})
_WH_ONLY_WORDS = frozenset({"buy", "purchase", "get"})
#: "what ELSE do you have" is an alternatives question about what is already in view, not a store overview.
_NOT_AN_OVERVIEW = frozenset({"else", "other", "others", "more"})

_WORD = re.compile(r"[a-z']+")

_MAX_CHOICES = 12
_PROBE_LIMIT = 20


def looks_like_store_discovery(message: str) -> bool:
    """True for a broad question about the shop's offering with no product, category or attribute named."""
    words = [w.replace("'", "") for w in _WORD.findall((message or "").lower())]
    if not words:
        return False
    if any(w in _NOT_AN_OVERVIEW for w in words):
        return False
    wh = words[0] in _WH_WORDS
    allowed = _STORE_WORDS | _WH_ONLY_WORDS if wh else _STORE_WORDS
    if any(w not in allowed for w in words):
        return False
    anchors = _OFFER_ANCHORS | _WH_ONLY_WORDS if wh else _OFFER_ANCHORS
    return any(w in anchors for w in words)


@dataclass
class StoreDiscovery:
    reply: str
    categories: list[str] = field(default_factory=list)
    #: "ok" | "empty" | "unavailable" - recorded in the turn trace so a failure is inspectable.
    status: str = "ok"


def _buyable(product) -> bool:
    variants = list(getattr(product, "variants", None) or [])
    if variants:
        return any(str(v.availability) != "OUT_OF_STOCK" for v in variants)
    return str(getattr(product, "availability", "UNKNOWN")) != "OUT_OF_STOCK"


async def _declared_categories(adapter, connection_id: str) -> list[str] | None:
    """The merchant's declared departments, else the category names its real products carry. None = unknown."""
    lister = getattr(adapter, "list_departments", None)
    if lister is not None:
        try:
            declared = [str(d) for d in await lister() if str(d).strip()]
            if declared:
                return declared
        except Exception:  # noqa: BLE001 - fall through to what the products themselves say
            logger.warning("store_discovery: department list unavailable for %s", connection_id, exc_info=True)
    try:
        from . import vocabulary

        vocab = await vocabulary.get_vocabulary(adapter, connection_id)
    except Exception:  # noqa: BLE001
        vocab = None
    if vocab is not None and vocab.categories:
        return list(vocab.categories)
    return None


async def _has_buyable_product(adapter, category: str) -> bool | None:
    """True/False for a real answer, None when the platform could not be asked."""
    try:
        result = await adapter.search_products("", limit=_PROBE_LIMIT, dept=category)
    except TypeError:
        # A platform with no department browse: filter one bounded browse sample by the products' own categories.
        try:
            sample = await adapter.search_products("", limit=_PROBE_LIMIT * 5)
        except Exception:  # noqa: BLE001
            return None
        wanted = category.lower()
        products = [
            p for p in list(getattr(sample, "products", sample) or [])
            if wanted in {c.lower() for c in (getattr(p, "categories", None) or [])}
        ]
        return any(_buyable(p) for p in products)
    except Exception:  # noqa: BLE001
        logger.warning("store_discovery: department browse failed for %r", category, exc_info=True)
        return None
    return any(_buyable(p) for p in list(getattr(result, "products", result) or []))


async def run_store_discovery_turn(adapter, connection_id: str) -> StoreDiscovery:
    declared = await _declared_categories(adapter, connection_id)
    if not declared:
        return StoreDiscovery(
            reply="I can't pull up the catalogue right now - please try again in a moment, or tell me what you're looking for.",
            status="unavailable",
        )
    checks = await asyncio.gather(*[_has_buyable_product(adapter, c) for c in declared[:_MAX_CHOICES * 2]])
    shown = [c for c, ok in zip(declared, checks) if ok][:_MAX_CHOICES]
    if not shown and all(ok is None for ok in checks):
        return StoreDiscovery(
            reply="I can't pull up the catalogue right now - please try again in a moment, or tell me what you're looking for.",
            status="unavailable",
        )
    if not shown:
        return StoreDiscovery(
            reply="Nothing is available to browse right now. Tell me what you're looking for and I'll check.",
            status="empty",
        )
    return StoreDiscovery(
        reply="Here's what we carry - tap a category to browse it, or tell me what you're after: " + ", ".join(shown) + ".",
        categories=shown,
        status="ok",
    )
