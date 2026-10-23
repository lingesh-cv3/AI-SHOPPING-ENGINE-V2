"""Per-merchant catalog vocabulary: principled query normalization.

Two production search problems share one root: the shopper's words are
compared against a merchant's real catalog text, and the two vocabularies
do not always line up.

1. **Typos.** "wateproof trail shoes" only "worked" because the unknown
   token was silently ignored - the shopper's actual intent word
   (waterproof) never reached matching/ranking, so precision was lost.
   This module corrects a token only against words that REALLY EXIST in the
   merchant's catalog (titles, categories, descriptions) - never a general
   dictionary, never a blind autocorrect - and the caller verifies the
   corrected search against real results, falling back to the shopper's
   literal words when the correction finds nothing.

2. **Domain words.** At "Kettle & Bloom Coffee", "coffee" names the whole
   store, so it appears in no product title or category (the taxonomy is
   Single origin / Blends / Equipment / ...). Treated as a required match
   it either finds nothing ("show me coffee") or, worse, crowns the one
   product whose description happens to contain the word ("show me coffee
   blends" returned 1 of 4 blends). Northfield's category words ARE the
   shopper's words ("Running Shoes"), which is why the same code looked
   fine there. A merchant-identity word with ZERO occurrences in the
   catalog's own titles/categories carries no discriminating information
   and is neutralized; a word that does discriminate within the catalog
   (Northfield's "running") is left alone. Derived from data, never a
   hardcoded word.

Bounded by design: the vocabulary is built from one bounded browse sample
(`_SAMPLE_LIMIT`), cached per connection with a TTL. A 40k-SKU merchant's
vocabulary is therefore a disclosed sample, not the whole catalog - a
correction that the sample cannot confirm simply does not fire.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from dataclasses import dataclass, field

from engine.execution.relevance import _stem

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT = 100
_TTL_SECONDS = 300.0
#: Only tokens long enough for an edit to be unambiguous are ever corrected;
#: a 3-4 letter token is too often a different real word.
_MIN_CORRECTABLE_LEN = 5
_CUTOFF = 0.84
#: Words that carry no product meaning of their own - never spell-corrected
#: and never counted as a "query word" when deciding whether anything
#: distinctive is left after domain words are neutralized.
_FILLER = frozenset({
    "show", "me", "some", "any", "the", "a", "an", "of", "for", "to", "and", "or",
    "with", "in", "on", "please", "want", "need", "looking", "look", "find", "get",
    "give", "have", "do", "you", "your", "what", "which", "do", "i", "im", "my",
    # request-framing verbs, same class as "show"/"find": "search for coffee",
    # "browsing blends" must read as a browse, not as unresolved words.
    "search", "searched", "searching", "browse", "browsed", "browsing",
    "something", "anything", "options", "option", "products", "product", "items",
    "item", "stuff", "things", "thing", "can", "could", "would", "like", "see",
    # budget-phrase words: the number they introduce is a constraint, not a
    # product word - never a reason to treat the rest as "still searching".
    "under", "below", "less", "than", "up", "within", "max", "maximum", "more", "no",
    "budget", "around", "about", "is", "are", "be", "it", "them", "that", "this",
})
_NAME_STOPWORDS = frozenset({"co", "inc", "ltd", "llc", "company", "shop", "store", "the", "and", "of"})

_WORD = re.compile(r"[A-Za-z]+")

_MERCHANT_NAMES: dict[str, str] = {}
_CACHE: dict[str, "CatalogVocabulary"] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def register_merchant_name(connection_id: str, name: str) -> None:
    """Called once at engine assembly (`engine/api/deps.py`) with each
    merchant's trading name. Used ONLY to derive domain words - the name is
    never an identifier (see `MERCHANT_NAMES`' own note)."""
    _MERCHANT_NAMES[connection_id] = name
    _CACHE.pop(connection_id, None)


def cached_vocabulary(connection_id: str) -> "CatalogVocabulary | None":
    """The already-built vocabulary, or None when none exists yet. Never
    triggers a build (no I/O) - for pure/synchronous callers."""
    return _CACHE.get(connection_id)


def can_be_product_name(connection_id: str, words: list[str]) -> bool | None:
    """Whether ANY of `words` occurs in this store's product titles or
    categories - i.e. whether the phrase could be (part of) a product name.
    None when the store's vocabulary is not available, so callers never act
    on a guess."""
    vocab = _CACHE.get(connection_id)
    if vocab is None or not vocab.title_category_stems:
        return None
    return any(_stem(w.lower()) in vocab.title_category_stems for w in words)


def cached_categories(connection_id: str) -> list[str]:
    """The merchant's real category names from the already-built
    vocabulary, or [] when none has been built yet. Never triggers a
    build (no I/O) - callers that need one use `get_vocabulary`."""
    vocab = _CACHE.get(connection_id)
    return list(vocab.categories) if vocab is not None else []


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


@dataclass
class CatalogVocabulary:
    #: every real word in the sampled catalog + the merchant's own name -
    #: the ONLY dictionary a spelling correction may target.
    spell_words: set[str] = field(default_factory=set)
    #: stems of words in product titles + categories (what a shopper's word
    #: can discriminate on).
    title_category_stems: set[str] = field(default_factory=set)
    #: stems of every word anywhere in the sample (titles, categories,
    #: descriptions, merchant name).
    all_stems: set[str] = field(default_factory=set)
    #: merchant-identity stems with zero title/category occurrences.
    domain_stems: set[str] = field(default_factory=set)
    #: stems of the words in product VARIANT titles / option values ("250g whole
    #: bean", "ground", "red"): real catalog evidence that no product title,
    #: category or description carries and that a platform's own keyword search
    #: often does not index. Never a spelling target and never part of
    #: `all_stems` - only used to recognise a shopper's word as one the store
    #: really sells in some form, and to ground it (see `variant_only_words`).
    variant_stems: set[str] = field(default_factory=set)
    categories: list[str] = field(default_factory=list)
    #: the merchant's DECLARED departments (`adapter.list_departments()`), a subset of `categories`: the names a
    #: shopper can tap or type to browse a whole department.
    departments: list[str] = field(default_factory=list)
    built_at: float = 0.0


@dataclass
class Normalization:
    query: str
    corrections: dict[str, str] = field(default_factory=dict)
    dropped_domain_terms: list[str] = field(default_factory=list)
    #: True when nothing distinctive remains after neutralizing domain words
    #: - the shopper asked for the store's whole domain ("show me coffee"),
    #: which is a browse, not a search for a product word.
    browse_only: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.corrections or self.dropped_domain_terms)


def _variant_texts(variant) -> list[str]:
    return [getattr(variant, "title", None) or ""] + [str(x) for x in (getattr(variant, "attributes", None) or {}).values()]


def _variant_words(product) -> list[str]:
    """Alphabetic words (3+ letters) from a product's variant titles and normalized option values. Numbers and
    units ("250g", "7") carry no word and are skipped."""
    words: list[str] = []
    for v in list(getattr(product, "variants", None) or []):
        for text in _variant_texts(v):
            words.extend(w for w in _tokens(text) if len(w) >= 3 and w not in _FILLER)
    return words


#: Generic nouns that may accompany a department name without adding meaning ("all footwear products").
_DEPARTMENT_NOISE = frozenset({
    "all", "every", "everything", "whole", "entire", "category", "categories", "department", "departments",
    "section", "sections", "product", "products", "item", "items", "stuff", "things", "range", "collection",
})


def departments_named(query: str, vocab: CatalogVocabulary) -> list[str]:
    """The declared department(s) the request names EXACTLY: after the framing words ("show me", "all", "products")
    are removed, what remains is precisely that department's own words ("Footwear", "Single origin"). A query with
    any other distinctive word ("running shoes", "red footwear") names no department - it is a search."""
    stems = {
        _stem(w) for w in _tokens(query)
        if len(w) >= 2 and w not in _FILLER and w not in _DEPARTMENT_NOISE
    }
    if not stems:
        return []
    named: list[str] = []
    for d in vocab.departments:
        d_stems = {_stem(w) for w in _tokens(d) if w not in _FILLER and w not in _DEPARTMENT_NOISE}
        if d_stems and d_stems == stems:
            named.append(d)
    return named


def variant_only_words(query: str, vocab: CatalogVocabulary) -> list[str]:
    """Distinctive words of `query` that only product VARIANTS can ground: the store really sells them (a variant
    option says "whole bean") but no title, category or description contains them, so an ordinary keyword search
    finds nothing. Empty when there are none - a word the store has no evidence for is never grounded."""
    out: list[str] = []
    for w in _tokens(query):
        if len(w) < 3 or w in _FILLER:
            continue
        s = _stem(w)
        if s in vocab.variant_stems and s not in vocab.all_stems:
            out.append(w)
    return out


def product_has_variant_terms(product, words: list[str]) -> bool:
    """True when ONE variant of `product` carries every word's stem in its title / option values."""
    stems = {_stem(w) for w in words}
    for v in list(getattr(product, "variants", None) or []):
        if str(getattr(v, "availability", "")) == "OUT_OF_STOCK":
            continue  # a sold-out variant is not evidence the shopper can buy what they asked for
        have = {_stem(w) for text in _variant_texts(v) for w in _tokens(text)}
        if stems <= have:
            return True
    return False


def build_vocabulary(products: list, departments: list[str] | None, merchant_name: str | None) -> CatalogVocabulary:
    vocab = CatalogVocabulary(built_at=time.monotonic())
    title_cat_words: set[str] = set()
    for p in products:
        title = getattr(p, "title", "") or ""
        desc = getattr(p, "description", "") or ""
        cats = list(getattr(p, "categories", None) or [])
        for w in _tokens(title):
            title_cat_words.add(w)
            vocab.spell_words.add(w)
        for c in cats:
            for w in _tokens(c):
                title_cat_words.add(w)
                vocab.spell_words.add(w)
            if c not in vocab.categories:
                vocab.categories.append(c)
        for w in _tokens(desc):
            vocab.spell_words.add(w)
    for p in products:
        for w in _variant_words(p):
            vocab.variant_stems.add(_stem(w))
    for d in departments or []:
        for w in _tokens(str(d)):
            title_cat_words.add(w)
            vocab.spell_words.add(w)
        if str(d) not in vocab.categories:
            vocab.categories.append(str(d))
        if str(d) not in vocab.departments:
            vocab.departments.append(str(d))
    vocab.title_category_stems = {_stem(w) for w in title_cat_words}
    name_words = [w for w in _tokens(merchant_name or "") if w not in _NAME_STOPWORDS]
    for w in name_words:
        vocab.spell_words.add(w)
    vocab.all_stems = {_stem(w) for w in vocab.spell_words}
    vocab.domain_stems = {
        _stem(w) for w in name_words if _stem(w) not in vocab.title_category_stems
    }
    return vocab


async def get_vocabulary(adapter, connection_id: str) -> CatalogVocabulary | None:
    cached = _CACHE.get(connection_id)
    if cached is not None and (time.monotonic() - cached.built_at) < _TTL_SECONDS:
        return cached
    lock = _LOCKS.setdefault(connection_id, asyncio.Lock())
    async with lock:
        cached = _CACHE.get(connection_id)
        if cached is not None and (time.monotonic() - cached.built_at) < _TTL_SECONDS:
            return cached
        try:
            result = await adapter.search_products("", limit=_SAMPLE_LIMIT)
            products = list(getattr(result, "products", result) or [])
        except Exception:  # noqa: BLE001 - no vocabulary is a degrade, never a failure
            logger.warning("vocabulary: catalog sample unavailable for %s", connection_id, exc_info=True)
            return cached
        departments: list[str] | None = None
        lister = getattr(adapter, "list_departments", None)
        if lister is not None:
            try:
                departments = list(await lister())
            except Exception:  # noqa: BLE001
                departments = None
        vocab = build_vocabulary(products, departments, _MERCHANT_NAMES.get(connection_id))
        _CACHE[connection_id] = vocab
        return vocab


def _correct_token(word: str, vocab: CatalogVocabulary) -> str | None:
    lower = word.lower()
    if len(lower) < _MIN_CORRECTABLE_LEN or lower in _FILLER:
        return None
    if lower in vocab.spell_words or _stem(lower) in vocab.all_stems:
        return None
    pool = [w for w in vocab.spell_words if w[0] == lower[0] and abs(len(w) - len(lower)) <= 2 and len(w) >= _MIN_CORRECTABLE_LEN]
    matches = difflib.get_close_matches(lower, pool, n=2, cutoff=_CUTOFF)
    if not matches:
        return None
    if len(matches) == 2:
        r0 = difflib.SequenceMatcher(None, lower, matches[0]).ratio()
        r1 = difflib.SequenceMatcher(None, lower, matches[1]).ratio()
        if r0 - r1 < 0.04:
            return None  # ambiguous - never guess between two real words
    return matches[0]


def content_words(query: str, vocab: CatalogVocabulary) -> tuple[list[str], list[str]]:
    """`(known, unknown)` distinctive words of `query` against THIS store's
    own vocabulary (every word in its sampled titles, categories and
    descriptions, plus its trading name). Framing/filler words, numbers and
    very short words are ignored. A shopper whose distinctive words are ALL
    unknown is asking for something this store does not sell - the one fact a
    category-level fallback must never paper over."""
    known: list[str] = []
    unknown: list[str] = []
    for w in _tokens(query):
        if len(w) < 3 or w in _FILLER:
            continue
        if w in vocab.spell_words or _stem(w) in vocab.all_stems or _stem(w) in vocab.variant_stems:
            known.append(w)
        else:
            unknown.append(w)
    return known, unknown


def spell_only(query: str, vocab: CatalogVocabulary) -> tuple[str, dict[str, str]]:
    """Spelling correction ALONE (no domain-word neutralization), for
    telling the interpreter what the shopper most plausibly typed. Each
    correction is a real catalog word within a small edit distance - the
    interpreter still decides what the message means."""
    corrections: dict[str, str] = {}
    out: list[str] = []
    last = 0
    for m in _WORD.finditer(query):
        out.append(query[last:m.start()])
        last = m.end()
        fixed = _correct_token(m.group(0), vocab)
        if fixed:
            corrections[m.group(0)] = fixed
        out.append(fixed or m.group(0))
    out.append(query[last:])
    return "".join(out), corrections


def validate_categories(claimed: list[str], vocab: CatalogVocabulary) -> list[str]:
    """The interpreter's category claims, kept only where they EXACTLY
    (case-insensitively) name a category in the merchant's real taxonomy.
    A claimed name that does not exist is dropped, never approximated."""
    by_lower = {c.lower(): c for c in vocab.categories}
    out: list[str] = []
    for name in claimed:
        real = by_lower.get(name.strip().lower())
        if real is not None and real not in out:
            out.append(real)
    return out


def normalize_query(query: str, vocab: CatalogVocabulary) -> Normalization:
    """Correct unknown tokens against real catalog words, then neutralize
    merchant-domain words. Non-alphabetic text (numbers, currency) is left
    exactly as written."""
    corrections: dict[str, str] = {}
    dropped: list[str] = []
    out: list[str] = []
    last = 0
    for m in _WORD.finditer(query):
        word = m.group(0)
        out.append(query[last:m.start()])
        last = m.end()
        fixed = _correct_token(word, vocab)
        token = fixed if fixed else word
        if fixed:
            corrections[word] = fixed
        if _stem(token.lower()) in vocab.domain_stems:
            dropped.append(token.lower())
            continue
        out.append(token)
    out.append(query[last:])
    new_query = re.sub(r"\s+", " ", "".join(out)).strip()
    remaining = [w for w in _tokens(new_query) if w not in _FILLER]
    browse_only = bool(dropped) and not remaining
    return Normalization(
        query=new_query, corrections=corrections, dropped_domain_terms=dropped,
        browse_only=browse_only,
    )
