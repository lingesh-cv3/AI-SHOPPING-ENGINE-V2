"""Semantic (vector-space) retrieval - a real, measured gap-closer, not a
default replacement for keyword search.

The adapter's own `search_products` is deliberately narrow (title-only, or
close to it - see `sample_merchant/store.py`'s own docstring): a shopper
asking about an attribute that only appears in a product's DESCRIPTION,
never its title, gets zero real candidates back from the adapter, and
nothing downstream ever sees that product at all. Live, catalog-grounded
example: Northfield's "Winter Road Shoe" is water-resistant per its own
description ("Water-resistant knit with a reflective heel"), but its title
says nothing about water - a shopper asking for "a water resistant running
shoe" gets no adapter hits for it whatsoever, though it is a real match.

This module never invents a product or a fact about one. It re-scores an
already-fetched, already-real candidate set (never a second, hidden
catalog) using TF-IDF cosine similarity over each product's own real
title+description+categories text - a classical, deterministic vector-
space technique, not a neural embedding (no model weights, no external
API call, no network dependency - this environment has neither a
configured embeddings endpoint nor headroom for a multi-gigabyte model
download, and CLAUDE.md's own standing rule is to justify a technology
choice with a measured gap, not adopt one by default). It is explicitly
NOT a synonym solver: two texts sharing zero vocabulary at all (the
catalog literally never uses the word "trainers" anywhere - see
`sample_merchant/seed/catalog.py`'s own docstring naming this exact,
still-open gap) cannot be related by ANY lexical method, this one
included - that gap is named honestly in PROGRESS.md, not claimed solved
here.

Structural position in the pipeline: this is an ADDITIVE candidate-set
widener only, triggered only when the adapter's own keyword search
already came back weak or empty for a query with real signal. Every
candidate this module surfaces still goes through the EXACT SAME
downstream hard-filter (`mission_ranking.rank_and_filter`) and evidence
verification (`evidence.build_evidence`) as any keyword-found candidate -
this module has no way to bypass a budget/exclusion/stock check or to
mark itself "verified". Authoritative catalog data is never replaced:
every product this module scores is a real `Product` already fetched from
the adapter this same turn (via the same bounded `_full_catalog_sample`
this codebase already uses for its wide-browse fallback, capped the same
way) - this module only re-orders/re-selects among products that already
exist, real availability and price intact.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

#: Below this cosine similarity, a candidate is not a real semantic match -
#: chosen empirically against this module's own benchmark (see
#: engine/shopper_v2/eval/semantic_benchmark.py): the description-only
#: "water resistant" -> "Winter Road Shoe" match scores ~0.35-0.5 in
#: practice; unrelated products score under 0.1. Not tuned per-example -
#: a single global floor, the same discipline `_MIN_RATING_COUNT_FOR_SIGNAL`
#: already uses for a different signal.
_SIMILARITY_FLOOR = 0.15

#: How many additional candidates this module will ever contribute in one
#: turn - a widener, not a second full catalog scan result set.
_MAX_SEMANTIC_CANDIDATES = 8

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _product_text(product: dict) -> str:
    title = product.get("title") or ""
    description = product.get("description") or ""
    categories = " ".join(product.get("categories") or [])
    return f"{title} {description} {categories}"


@dataclass(frozen=True)
class SemanticMatch:
    product_id: str
    score: float


def _tf(tokens: list[str]) -> Counter:
    return Counter(tokens)


def _idf(corpus_tokens: list[list[str]]) -> dict[str, float]:
    n_docs = len(corpus_tokens)
    doc_freq: Counter = Counter()
    for tokens in corpus_tokens:
        for term in set(tokens):
            doc_freq[term] += 1
    # Standard smoothed idf - a term in every document scores idf=1 (still
    # positive, never zero/negative), a rare term scores much higher.
    return {term: math.log((1 + n_docs) / (1 + df)) + 1 for term, df in doc_freq.items()}


def _tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf = _tf(tokens)
    vec = {term: count * idf.get(term, 0.0) for term, count in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0:
        return vec
    return {term: v / norm for term, v in vec.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate the smaller vector - both are already unit-normalized above,
    # so this is a plain dot product.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return sum(v * longer.get(term, 0.0) for term, v in shorter.items())


def semantic_matches(
    query: str, catalog_sample: list[dict], *, exclude_ids: set[str] | None = None
) -> list[SemanticMatch]:
    """Real TF-IDF cosine similarity between `query` and every product's own
    real text in `catalog_sample`. Returns only products scoring at or
    above `_SIMILARITY_FLOOR`, sorted highest first, capped at
    `_MAX_SEMANTIC_CANDIDATES` - a widener, never a replacement result set.

    `catalog_sample` is the caller's job to fetch (real, adapter-sourced
    dicts, the same shape `relevance.py`/`mission_ranking.py` already
    consume) - this function does no I/O of its own, matching the
    "pure function over already-fetched data" discipline every other
    deterministic module in this codebase already follows.
    """
    query_tokens = _tokenize(query)
    if not query_tokens or not catalog_sample:
        return []

    exclude_ids = exclude_ids or set()
    candidates = [p for p in catalog_sample if p.get("product_id") not in exclude_ids]
    if not candidates:
        return []

    doc_tokens = [_tokenize(_product_text(p)) for p in candidates]
    idf = _idf(doc_tokens + [query_tokens])
    query_vec = _tfidf_vector(query_tokens, idf)
    if not query_vec:
        return []

    scored = []
    for product, tokens in zip(candidates, doc_tokens):
        doc_vec = _tfidf_vector(tokens, idf)
        score = _cosine(query_vec, doc_vec)
        if score >= _SIMILARITY_FLOOR:
            scored.append(SemanticMatch(product_id=product["product_id"], score=score))

    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:_MAX_SEMANTIC_CANDIDATES]
