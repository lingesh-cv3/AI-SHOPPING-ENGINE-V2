"""Deterministic resolution of "add <the product's name>" to ONE real product.

The model-driven path (`verification.py`) resolves what a shopper means when a
model is available; this is the model-free floor under it, and also the
guarantee that naming a product exactly is enough. A shopper who types the
name of a product the shop sells ("add Trailblazer Running Shoe") must get
THAT product, whether or not it happens to be among the last things shown and
whether or not they searched for it first.

Discipline (same as everywhere else in this package):

* Candidates only ever come from the adapter - products in view, then the
  shop's own search results for the words typed. Nothing is invented.
* A match must be unambiguous. Two products that both fit equally well, or a
  fuzzy score without a clear margin over the runner-up, resolve to nothing
  and the caller keeps its existing "which one did you mean?" behaviour.
* Exact mention of a full product title is the strongest signal; a word that
  belongs to exactly one title among the products in view is next; a
  spelling-tolerant comparison is the last resort and needs both a high score
  and a clear margin.
* This only decides WHICH product. Adding it still goes through
  `fulfillment.start_fulfillment` (variant question, Decision Engine, Risk Gate,
  execution) exactly like any other add.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

from . import vocabulary
from .reference_resolution import _distinctive_title_words
from .retrieval import retrieve

logger = logging.getLogger(__name__)

_COMMAND_WORDS = frozenset({
    "add", "buy", "purchase", "get", "put", "take", "order", "i", "ill", "i'll", "want", "would",
    "like", "to", "the", "a", "an", "my", "cart", "basket", "bag", "please", "in", "into", "on",
    "me", "it", "now", "one", "of", "and", "for",
})

_FUZZY_MIN_SCORE = 0.70
_FUZZY_MIN_MARGIN = 0.15
_FUZZY_MIN_LETTERS = 8


@dataclass
class NamedAdd:
    product_id: str
    title: str
    #: "exact" (full title typed), "distinctive" (a word unique to it among
    #: the products in view) or "fuzzy" (spelling-tolerant, with a margin).
    how: str


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _remainder(message: str) -> str:
    words = re.findall(r"[a-z0-9']+", message.lower())
    return " ".join(w for w in words if w not in _COMMAND_WORDS)


def _exact(products: list, lowered: str) -> list:
    hits = [p for p in products if p.title and p.title.strip().lower() in lowered]
    if len(hits) > 1:
        longest = max(len(p.title) for p in hits)
        hits = [p for p in hits if len(p.title) == longest]
    return hits


def _distinctive(products: list, lowered: str) -> list:
    per_title = {p.product_id: _distinctive_title_words(p.title) for p in products if p.title}
    owners: dict[str, set[str]] = {}
    for pid, words in per_title.items():
        for w in words:
            owners.setdefault(w, set()).add(pid)
    hit_ids = {
        pid for pid, words in per_title.items()
        if any(len(owners[w]) == 1 and w in lowered for w in words)
    }
    return [p for p in products if p.product_id in hit_ids]


def _fuzzy(products: list, remainder: str) -> list:
    spoken = _letters(remainder)
    if len(spoken) < _FUZZY_MIN_LETTERS:
        return []
    scored = sorted(
        ((difflib.SequenceMatcher(None, spoken, _letters(p.title)).ratio(), p) for p in products if p.title),
        key=lambda pair: -pair[0],
    )
    if not scored or scored[0][0] < _FUZZY_MIN_SCORE:
        return []
    if len(scored) > 1 and scored[0][0] - scored[1][0] < _FUZZY_MIN_MARGIN:
        return []
    return [scored[0][1]]


async def resolve_named_add(
    *, adapter, message: str, view_products: list, allow_fuzzy: bool = True,
) -> NamedAdd | None:
    """The one real product `message` names, or `None` (nothing named, or not
    unambiguous). `view_products` are the real products currently in view."""
    lowered = message.lower()
    remainder = _remainder(message)
    if not remainder:
        return None

    hits = _exact(view_products, lowered)
    if len(hits) == 1:
        return NamedAdd(hits[0].product_id, hits[0].title, "exact")
    if len(hits) > 1:
        return None
    hits = _distinctive(view_products, lowered)
    if len(hits) == 1:
        return NamedAdd(hits[0].product_id, hits[0].title, "distinctive")
    if len(hits) > 1:
        return None

    # Not among what is in view: the shop's own catalogue, for the words typed
    # (the whole phrase first, then each longer word on its own so a misspelled
    # word does not hide the rest of the name).
    found: dict[str, object] = {}
    queries = [remainder] + [w for w in remainder.split() if len(w) >= 4 and w != remainder]
    for query in queries:
        try:
            result = await retrieve(adapter, query, limit=20)
        except Exception:  # noqa: BLE001 - never raise into the turn
            logger.warning("named_product: catalogue lookup failed", exc_info=True)
            return None
        if result.unavailable:
            return None
        for p in result.products:
            found.setdefault(p.product_id, p)
        hits = _exact(list(found.values()), lowered)
        if len(hits) == 1:
            return NamedAdd(hits[0].product_id, hits[0].title, "exact")
        if len(hits) > 1:
            return None
    if allow_fuzzy:
        hits = _fuzzy(list(found.values()), remainder)
        if len(hits) == 1:
            return NamedAdd(hits[0].product_id, hits[0].title, "fuzzy")
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Several products named in one request: "Show me the Trailblazer and the Starter Road Shoe".
#
# The ordinary search ranks the whole phrase against every title and keeps only the top-scoring match, so the second
# product was silently dropped. Here each named part is resolved on its own, against the shop's own products, by
# COVERAGE: a part names a product when every distinctive word of the part is a word of exactly one product's title.
# Nothing is invented, no relevance threshold is lowered, and a part that is ambiguous ("running shoes") or has no
# catalogue evidence at all ("unicorn") is never forced into a product - it is reported.
# ---------------------------------------------------------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"\s*(?:,|&|;|\band\b|\bplus\b|\bas well as\b|\balong with\b)\s*", re.IGNORECASE)
_SEPARATOR_HINT = re.compile(r"[,&;]|\b(?:and|plus|as well as|along with)\b", re.IGNORECASE)
_FRAME_WORDS = _COMMAND_WORDS | frozenset({
    "show", "me", "find", "see", "look", "looking", "have", "do", "you", "can", "could", "please", "want", "need",
    "like", "tell", "about", "give", "some", "display", "pull", "up", "also", "both", "compare", "check", "im", "we",
    "is", "are", "there", "any", "got", "sell", "carry", "stock", "what", "whats", "which", "where", "your", "yours",
    "hi", "hello", "hey", "ok", "okay", "just", "only", "then", "so",
})


@dataclass
class NamedProducts:
    #: real products, in the order the shopper named them, each resolved uniquely
    products: list
    #: parts with no catalogue evidence at all (no word of them is a word the shop uses)
    missing: list[str]
    #: parts whose words the shop does use but that name no single product ("running shoes")
    ambiguous: list[str]


def _segment_words(segment: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", segment.lower()) if w not in _FRAME_WORDS and len(w) >= 2]


def _stems(words) -> set[str]:
    return {vocabulary._stem(w) for w in words}


def _has_unknown_proper_name(part: str, words: list[str], corrections: dict, message: str, vocab, pool=None) -> bool:
    """Does this part contain a capitalised word the shop has never used, even after spelling correction?

    "Zephyr Cloud Boot": "Cloud" and "Boot" are words the shop uses, so the part is not wholly unknown, but "Zephyr" is a
    capitalised name no product, category or variant carries - the shopper named something that is not here. Only a
    capitalised word counts (a lowercase unknown word is usually an ordinary modifier: "cheap socks", "warm hat"), and
    never the very first word of the message (sentence-initial capitals prove nothing). A spelling correction to a real
    catalogue word ("Trailblazr") makes the word known."""
    first = (message or "").strip().split(" ")[0] if (message or "").strip() else ""
    content = set(words)
    for tok in re.findall(r"[A-Za-z0-9']+", part):
        if not tok[0].isupper() or tok == first or tok in corrections:
            continue
        low = tok.lower()
        if low in content and vocabulary._stem(low) not in vocab.all_stems:
            # A brand held only in a product's attributes or description is not missing: look at everything the
            # searched products carry, not just their titles.
            if any(low in str(getattr(p, "model_dump", lambda: p)()).lower() for p in (pool or {}).values()):
                continue
            return True
    return False


async def resolve_named_products(*, adapter, message: str, vocab) -> NamedProducts | None:
    """The real products a multi-part request names, or `None` when it is not that shape (one part, or nothing named
    precisely enough). Triggers only when it is safe: at least two parts resolve to a single product each, or one
    does and another is plainly absent from the catalogue with no ambiguous part in between - so "trail shoes and
    socks" stays a category request and never becomes "just the socks"."""
    if not _SEPARATOR_HINT.search(message or ""):
        return None
    parts = [p for p in _SEGMENT_SPLIT.split(message) if p and _segment_words(p)]
    if len(parts) < 2:
        return None
    resolved: list = []
    seen: set[str] = set()
    missing: list[str] = []
    ambiguous: list[str] = []
    for part in parts:
        text = part
        corrections: dict = {}
        try:
            text, corrections = vocabulary.spell_only(part, vocab)
        except Exception:  # noqa: BLE001 - spelling help is optional
            text = part
        words = _segment_words(text)
        if not words:
            continue
        known_to_shop = [w for w in words if vocabulary._stem(w) in vocab.all_stems]
        # The shop vocabulary is built from a bounded sample of the catalogue, so a word missing from it proves nothing
        # at real volume (a 40k-SKU shop): a part is only reported missing after the catalogue itself, searched with a
        # bounded query, has no product that covers it.
        all_unknown = not known_to_shop
        pool: dict[str, object] = {}
        queries = [" ".join(words)] if all_unknown else [" ".join(words)] + [w for w in words if len(w) >= 4 and len(words) > 1]
        for query in queries:
            try:
                result = await retrieve(adapter, query, limit=20)
            except Exception:  # noqa: BLE001 - never raise into the turn
                logger.warning("named_product: catalogue lookup failed", exc_info=True)
                return None
            if result.unavailable:
                return None
            for p in result.products:
                pool.setdefault(p.product_id, p)
        want = _stems(words)
        covering = [p for p in pool.values() if p.title and want <= _stems(re.findall(r"[a-z0-9']+", p.title.lower()))]
        if len(covering) > 1:
            exact = [p for p in covering if _stems(re.findall(r"[a-z0-9']+", p.title.lower())) == want]
            covering = exact if len(exact) == 1 else covering
        if len(covering) == 1:
            if covering[0].product_id not in seen:
                seen.add(covering[0].product_id)
                resolved.append(covering[0])
        elif not covering and (all_unknown or _has_unknown_proper_name(part, words, corrections, message, vocab, pool)):
            missing.append(" ".join(_segment_words(part)))
        else:
            ambiguous.append(" ".join(words))
    if len(resolved) >= 2 or (len(resolved) == 1 and missing and not ambiguous):
        return NamedProducts(products=resolved, missing=missing, ambiguous=ambiguous)
    return None
