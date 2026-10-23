"""Phase 1/2 production discovery pipeline for Shopper V2.

Wires the Stage 2A typed contracts (`contracts.py`) into the LIVE
`/api/v2/chat` path (previously they existed only inside the offline
`engine/shopper_v2/eval/` package - PROGRESS.md's Immediate Next Work item
5), and closes Stage 2A Findings 4 and 5 (Findings 2 and 3 were closed
directly in `evidence.py`, since they are fixes to how V2 calls the
existing deterministic layer, not new pipeline behavior - see that
module's own comments).

This module calls only:
  - the adapter's own `search_products` (read-only, same as `retrieval.py`)
  - `evidence.build_evidence` (unmodified call signature; the function
    itself is now Finding-2/3-fixed)
  - `ReasoningService.judge_relevance` / `judge_best_match` (existing
    PUBLIC methods V1's `engine/execution/service.py` already uses -
    reused, not forked, and called only as a bounded fallback, never on
    every turn)
  - `search_context.py` (V2's own new table, see `db_models.py`)

Never imports `engine.decision`, `engine.risk`, or any `engine.execution`
mutation path. Never modifies `engine/execution/relevance.py` or
`engine/execution/mission_ranking.py`.
"""

from __future__ import annotations

import asyncio
import logging
import re

from engine.execution import mission_ranking, semantic_retrieval
from engine.reasoning.service import ReasoningService
from shared.models import Operation, Product

from . import named_product, search_context, vocabulary
from .contracts import Candidate, CandidateSet, RetrievalMethod, SearchIntent
from .evidence import TurnEvidence, build_evidence, product_as_dict
from .reference_resolution import _distinctive_title_words
from .retrieval import retrieve
from .tracing import TurnTrace

logger = logging.getLogger(__name__)

_JUDGE_TIMEOUT_S = 12.0
_CATALOG_SAMPLE_LIMIT = 40
#: Batch 4: semantic retrieval's own wider sample - deliberately larger
#: than `_CATALOG_SAMPLE_LIMIT` (the LLM judge's sample), since TF-IDF
#: scoring is a cheap, deterministic, no-model-call operation and can
#: afford to look at more of the catalog per turn than a paid model call
#: would. Still an explicit, disclosed bound (the same honesty
#: `engine/execution/service.py::_MAX_CATALOG_SCAN` already applies), not
#: the whole catalog - a real 40k-SKU client is not fully covered by this
#: either, and this module makes no claim that it is.
_SEMANTIC_SAMPLE_LIMIT = 500
#: Cost-bounded retry cap for `_try_semantic_judge`'s pick-then-verify loop
#: - each attempt is one more `judge_best_match` model call, so this bounds
#: the worst case to 3 extra calls on top of the 1 `judge_relevance` call,
#: never unbounded.
_MAX_JUDGE_PICK_ATTEMPTS = 3

#: Closed-class, non-phrase-specific: words a bare conversational follow-up
#: is built from ("Why this?", "tell me more", "what about it") that name
#: NOTHING of their own - `_distinctive_title_words` alone doesn't filter
#: these (its own contract is product TITLES, a different vocabulary), so
#: without this a bare follow-up's own pronouns/deictics ("this", "why")
#: would wrongly look like a distinctive noun that disagrees with the
#: prior query, exactly the same false-positive shape as the topic-switch
#: check this filters for below - never used to guess MEANING, only to
#: recognize that a message carries no product-specific content to check
#: at all.
#: An explicit request for a product ("I want X", "do you have X", "show me X").
#: The "this store does not carry it" verdict (see `run_discovery_turn`) is only
#: ever drawn for a message of this shape - never for "recommend one", "which
#: would you choose?" or any other phrase whose unfamiliar words are not a
#: product the shopper asked for.
_REQUEST_FRAME = re.compile(
    r"\bi(?:'d| would)? (?:want|need|like)\b|\blooking for\b|\bdo you (?:have|sell|carry|stock)\b|"
    r"\bhave you got\b|\bgot any\b|\bshow me\b|\bfind me\b|\bget me\b",
    re.IGNORECASE,
)

_CONTINUATION_FILLER_WORDS = frozenset({
    "this", "that", "these", "those", "it", "them", "one", "ones", "why",
    "how", "what", "else", "more", "again", "then", "please", "yes",
    "okay", "ok", "sure", "wait", "hold", "actually", "never", "mind",
    "show", "tell", "give", "find", "any", "some", "want", "need",
})


_RETRIEVAL_LIMIT = 20


async def _do_retrieve(adapter, query: str, *, limit: int = _RETRIEVAL_LIMIT) -> CandidateSet:
    """One read-only `adapter.search_products` call, reshaped into a
    `CandidateSet`. Same call `retrieval.py::retrieve` makes, made directly
    here (not by calling that function) only so `total_available` can be
    kept to compute `CandidateSet.truncated` - `retrieval.py`'s own
    docstring deliberately discards it ("Stage 1 makes no 'this is the
    whole catalog' claim"), which is correct for Stage 1's un-typed
    `RetrievalResult` but is exactly the field `CandidateSet` exists to
    carry (SHOPPER_V2_STAGE2_SPEC.md §7). Same pattern Stage 2A's own eval
    harness already uses (`eval/adapt.py::retrieve_candidates`) - this is
    that same, already-reviewed pattern now applied to the live pipeline."""
    try:
        result = await adapter.search_products(query, limit=limit)
    except Exception:  # noqa: BLE001 - degrade the same way retrieval.py does
        logger.warning("shopper_v2 pipeline: adapter search failed", exc_info=True)
        return CandidateSet(
            intent=SearchIntent(raw_message=query, query=query),
            candidates=[],
            unavailable=True,
        )
    products = list(getattr(result, "products", result) or [])
    total_available = getattr(result, "total_available", None)
    truncated = (total_available is not None and total_available > len(products)) or (
        total_available is None and len(products) >= limit
    )
    candidates = [
        Candidate(product=p, retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
        for p in products
    ]
    return CandidateSet(
        intent=SearchIntent(raw_message=query, query=query),
        candidates=candidates,
        total_available=total_available,
        unavailable=False,
        truncated=truncated,
    )


async def _run_discovery_turn_once(
    *,
    reasoning: ReasoningService,
    adapter,
    session_id: str,
    connection_id: str,
    message: str,
    delta_query: str | None,
    constraint_only_turn: bool = False,
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    trace: TurnTrace,
    browse_all: bool = False,
    raw_said_extra: str = "",
    preset_products: list[Product] | None = None,
) -> tuple[TurnEvidence, list[Product], SearchIntent]:
    """The full retrieval -> evidence step for one V2 discovery turn.
    Returns (evidence, all fetched products, the SearchIntent actually
    used) - `respond.py` is unchanged and still takes exactly this shape.

    `constraint_only_turn` (from `orchestrator.py`'s own real `StateDelta` -
    never a guess at word content) makes `SearchIntent` genuinely drive
    WHICH query is searched first, not just feed the semantic-judge
    rescue: a turn that stated a new budget with no new goal of its own
    ("under 2000" alone) searches the carried-forward topic directly,
    skipping the guaranteed-empty search Finding 4's reactive fallback
    below would otherwise have to wait for.
    """
    primary_query = (delta_query or message or "").strip() or message
    if browse_all:
        # The shopper asked for the store's whole domain ("show me coffee"
        # at a coffee merchant) - a browse, not a search for a product word.
        primary_query = ""
    proactive_carry_forward_used = False
    if constraint_only_turn:
        last_query = await search_context.get_last_effective_query(
            session_id, connection_id
        )
        if last_query:
            primary_query = last_query
            proactive_carry_forward_used = True

    intent = SearchIntent.from_shopping_state(
        raw_message=message, query=primary_query, shopping_state=shopping_state
    )
    trace.search_intent_query = intent.query
    trace.search_intent_fallback_used = proactive_carry_forward_used

    if preset_products is not None:
        # Category-grounded browse: the candidates are real products whose
        # own `categories` matched a category the interpreter named AND
        # the merchant's taxonomy confirmed (see `run_discovery_turn`).
        candidate_set = CandidateSet(
            intent=intent,
            candidates=[
                Candidate(product=p, retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
                for p in preset_products
            ],
            total_available=len(preset_products),
            unavailable=False,
            truncated=False,
        )
    else:
        candidate_set = await _do_retrieve(adapter, intent.query)
    # A carried-forward query is, by construction, words the shopper said
    # on an EARLIER turn, not this one - `query_diverges_from_said` (called
    # inside `build_evidence` below) checks the query's words against what
    # the shopper said THIS turn, so without this the proactive path would
    # wrongly look like a diverging, ungrounded query on every turn it
    # fires for. Same reasoning Finding 4's reactive fallback already
    # applies for the identical shape - see its own comment below.
    raw_said_for_evidence = (
        f"{primary_query} {message}" if proactive_carry_forward_used else message
    )
    if raw_said_extra:
        # Spelling-normalized words are still the shopper's own words (a
        # corrected token IS what they typed, fixed) - without this the
        # divergence check would read a correction as an invented query.
        raw_said_for_evidence = f"{raw_said_for_evidence} {raw_said_extra}"

    if candidate_set.unavailable:
        trace.candidate_count = 0
        evidence = build_evidence(
            query=intent.query,
            raw_said=raw_said_for_evidence,
            products=[],
            shopping_state=shopping_state,
            memory_facts=memory_facts,
            catalog_sample=None,
            platform_unavailable=True,
        )
        trace.no_match_reason = evidence.no_match_reason
        return evidence, [], intent

    products = [c.product for c in candidate_set.candidates]

    # --- Stage 2A Finding 4: constraint-only-turn context fallback -------
    #
    # Purely results-driven: only fires when THIS turn's own query found
    # literally nothing, and only when a prior turn's query is actually on
    # record. A shopper switching topics ("actually, do you have kayaks")
    # still searches their own new words first and correctly finds
    # nothing on its own merits - this never overrides a real result.
    #
    # Rev. 2 migration (2026-09): live-reproduced, this used to fire for
    # ANY zero-result query regardless of whether it plausibly continued
    # the prior topic - a genuinely NEW topic's own query returning zero
    # results (e.g. a merchant search bug, or simply nothing in stock)
    # would silently substitute the PREVIOUS topic's results instead of
    # saying so honestly ("Show me socks." finding 0 candidates for a
    # tokenizer reason silently re-showed the previous running-shoes
    # search). The missing check: nothing here confirmed THIS message
    # actually relates to the prior query at all before reusing it. Now
    # requires a real, distinctive word shared between this turn's own
    # message and the prior query - the same word-overlap discipline
    # `verification.py`'s cross-check and `compare.py`'s historical-group
    # matching already use for this exact class of decision - before ever
    # trusting the substitution; a message that shares nothing with the
    # prior query is exactly what "switching topics" means, and must
    # degrade honestly (see the caller: an honest "nothing found" reply,
    # never a masked substitution).
    if not products:
        last_query = await search_context.get_last_effective_query(
            session_id, connection_id
        )
        # A bare deictic follow-up ("Why this?", "tell me more") carries no
        # distinctive noun of its OWN to check at all - that is evidence of
        # pure continuation, never of a topic switch, so it must not be
        # penalized the same way a message that DOES name something else
        # entirely ("Show me socks.") correctly is. Only a message that
        # names something specific AND shares nothing with the prior query
        # is the real switch-topic signal this fix targets.
        message_content_words = _distinctive_title_words(message) - _CONTINUATION_FILLER_WORDS
        last_query_words = _distinctive_title_words(last_query or "")
        plausibly_continues = not message_content_words or bool(message_content_words & last_query_words)
        if last_query and last_query != intent.query and plausibly_continues:
            fallback_set = await _do_retrieve(adapter, last_query)
            if not fallback_set.unavailable and fallback_set.candidates:
                candidate_set = fallback_set
                products = [c.product for c in candidate_set.candidates]
                intent = SearchIntent(
                    raw_message=message,
                    query=last_query,
                    categories=intent.categories,
                    positive_terms=intent.positive_terms,
                    negative_terms=intent.negative_terms,
                    budget_amount=intent.budget_amount,
                    budget_currency=intent.budget_currency,
                )
                trace.search_intent_fallback_used = True
                trace.search_intent_query = intent.query
                # `query_diverges_from_said` checks the QUERY's words
                # against what the shopper said THIS turn - correctly, for
                # a model-invented proxy term. But a carried-forward query
                # is, by construction, words the shopper said on an
                # EARLIER turn, not this one - without this, every
                # fallback-using turn would wrongly look like a diverging,
                # ungrounded query. Including the earlier turn's own real
                # words (not inventing any) keeps that check honest: the
                # query remains fully grounded in real shopper words,
                # just not only this turn's.
                raw_said_for_evidence = f"{last_query} {message}"

    trace.candidate_count = len(products)
    trace.candidate_set_truncated = candidate_set.truncated

    catalog_sample: list[Product] | None = None
    evidence = build_evidence(
        query=intent.query,
        raw_said=raw_said_for_evidence,
        products=products,
        shopping_state=shopping_state,
        memory_facts=memory_facts,
        catalog_sample=catalog_sample,
        platform_unavailable=False,
    )
    trace.eligible_count = sum(1 for e in evidence.products if e.hard_constraints_passed)
    trace.no_match_reason = evidence.no_match_reason
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)

    # --- Batch 4: TF-IDF semantic retrieval widener ----------------------
    #
    # Tried BEFORE the LLM judge below, deliberately: this is a free,
    # deterministic, no-model-call re-score - cheaper and faster than a
    # model call, so it is worth trying first whenever the adapter's own
    # narrow (title-only, in this codebase's demo adapters) keyword search
    # already came back weak/empty. Never replaces authoritative catalog
    # data: every candidate it can possibly surface is a real `Product`
    # already fetched this same turn from the adapter's own wide sample,
    # and every one still goes through the EXACT SAME `build_evidence`
    # hard-filter/verification pass below as any keyword-found candidate -
    # this step only WIDENS the candidate set `build_evidence` sees, never
    # marks anything verified itself.
    if evidence.no_match_reason in ("no_candidates", "no_relevant_match"):
        try:
            wide_result = await retrieve(adapter, "", limit=_SEMANTIC_SAMPLE_LIMIT)
        except Exception:  # noqa: BLE001 - degrade to the existing verdict, never raise further
            wide_result = None
        if wide_result is not None and not wide_result.unavailable and wide_result.products:
            already_ids = {p.product_id for p in products}
            wide_by_id = {p.product_id: p for p in wide_result.products}
            wide_dicts = [product_as_dict(p) for p in wide_result.products]
            matches = semantic_retrieval.semantic_matches(
                intent.query or message, wide_dicts, exclude_ids=already_ids
            )
            trace.semantic_retrieval_tried = True
            trace.semantic_retrieval_candidates = len(matches)
            if matches:
                added = [
                    wide_by_id[m.product_id] for m in matches if m.product_id in wide_by_id
                ]
                products = [*products, *added]
                evidence = build_evidence(
                    query=intent.query,
                    raw_said=raw_said_for_evidence,
                    products=products,
                    shopping_state=shopping_state,
                    memory_facts=memory_facts,
                    catalog_sample=None,
                    platform_unavailable=False,
                )
                trace.no_match_reason = evidence.no_match_reason
                trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
                trace.semantic_retrieval_verified = sum(
                    1
                    for e in evidence.products
                    if e.relevance_verified and e.product_id in {m.product_id for m in matches}
                )

    # --- Stage 2A Finding 5: restore the LLM semantic-judge fallback -----
    #
    # `evidence.py` never calls `judge_relevance`/`judge_best_match` -
    # confirmed by reading its imports (PROGRESS.md, Shopper V2 §E,
    # finding 5). V1's `engine/execution/service.py` already has and uses
    # both; this restores that SAME existing capability to the V2 path,
    # rather than building a new one. Bounded: only runs when the
    # deterministic layer found no confident match at all, only one model
    # call (judge_relevance) plus, conditionally, a second
    # (judge_best_match) - never on a turn that already has a confident
    # answer, and never mutating anything.
    if (
        evidence.no_match_reason in ("no_candidates", "no_relevant_match")
        and reasoning is not None
        and getattr(reasoning, "enabled", False)
    ):
        evidence, judged_product = await _try_semantic_judge(
            reasoning=reasoning,
            adapter=adapter,
            message=message,
            evidence=evidence,
            already_fetched=products,
            shopping_state=shopping_state,
            trace=trace,
        )
        if judged_product is not None and judged_product.product_id not in {
            p.product_id for p in products
        }:
            products = [*products, judged_product]
        trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)

    # Persist the effective query for a LATER constraint-only turn to fall
    # back to - only when this turn actually verified something, so a
    # dead-end query never becomes tomorrow's fallback.
    if any(e.relevance_verified for e in evidence.products):
        await search_context.set_last_effective_query(
            session_id, connection_id, intent.query
        )

    return evidence, products, intent


async def _products_in_categories(adapter, categories: list[str], vocab) -> list[Product]:
    """Real products whose own `categories` include one of the (already
    taxonomy-validated) category names. Tries the adapter's bounded
    department browse first (both demo platforms support `dept=` +
    paging); when that yields nothing, filters one bounded browse sample
    client-side - a disclosed sample, not a full-catalog scan."""
    wanted = {c.lower() for c in categories}
    found: dict[str, Product] = {}
    for cat in categories:
        try:
            result = await adapter.search_products("", limit=_RETRIEVAL_LIMIT, dept=cat)
        except TypeError:
            break  # adapter has no department browse - use the sample below
        except Exception:  # noqa: BLE001
            logger.warning("shopper_v2 pipeline: department browse failed for %r", cat, exc_info=True)
            continue
        for p in list(getattr(result, "products", result) or []):
            if wanted & {c.lower() for c in (getattr(p, "categories", None) or [])}:
                found.setdefault(p.product_id, p)
    if found:
        return list(found.values())
    try:
        sample = await adapter.search_products("", limit=_CATALOG_SAMPLE_LIMIT * 5)
    except Exception:  # noqa: BLE001
        return []
    return [
        p for p in list(getattr(sample, "products", sample) or [])
        if wanted & {c.lower() for c in (getattr(p, "categories", None) or [])}
    ]


async def _products_with_variant_terms(adapter, words: list[str]) -> list[Product]:
    """Real products one of whose variants carries every word (see `vocabulary.product_has_variant_terms`),
    from one bounded browse sample - a disclosed sample, not a full-catalogue scan."""
    try:
        sample = await adapter.search_products("", limit=_CATALOG_SAMPLE_LIMIT * 5)
    except Exception:  # noqa: BLE001
        logger.warning("shopper_v2 pipeline: variant-term browse failed", exc_info=True)
        return []
    return [
        p for p in list(getattr(sample, "products", sample) or [])
        if vocabulary.product_has_variant_terms(p, words)
    ]


def _buyable(product: Product) -> bool:
    variants = list(getattr(product, "variants", None) or [])
    if variants:
        return any(str(v.availability) != "OUT_OF_STOCK" for v in variants)
    return str(getattr(product, "availability", "UNKNOWN")) != "OUT_OF_STOCK"


def _product_stems(product: Product) -> set[str]:
    """Every word this product's own catalogue data carries (title, description,
    departments, variant titles and option values), stemmed - the only evidence an
    attribute word may be matched against."""
    parts = [product.title or "", product.description or "", *(product.categories or [])]
    for v in getattr(product, "variants", None) or []:
        parts.append(v.title or "")
        parts.extend(str(x) for x in (v.attributes or {}).values())
    return {vocabulary._stem(w) for w in vocabulary._tokens(" ".join(parts))}


async def _declared_domain_categories(adapter, vocab) -> list[str]:
    """The merchant-declared departments a shop-domain word ("coffee" at a coffee
    shop) means - from the connection's own capability declaration, validated
    against the departments that really exist in its catalogue. Empty when the
    merchant declares none, which keeps whole-catalogue behaviour."""
    try:
        caps = await adapter.get_capabilities()
        raw = caps.constraint(Operation.SEARCH_PRODUCTS, "domain_categories")
    except Exception:  # noqa: BLE001 - no declaration is a degrade, never a failure
        return []
    if not isinstance(raw, str) or not raw.strip():
        return []
    return vocabulary.validate_categories([c.strip() for c in raw.split("|") if c.strip()], vocab)


def _list_titles(products: list[Product], limit: int = 6) -> str:
    return ", ".join(p.title for p in products[:limit])


async def _domain_grounded_turn(
    *, adapter, vocab, norm, domain_word: str, common: dict, trace: TurnTrace,
):
    """A request that names the shop's whole domain plus descriptors ("a fruity
    coffee", "chocolatey coffee", "decaf coffee") answered from the merchant's
    OWN declared range, with the descriptors treated as preferences to be matched
    against what each product's data actually says - never as proof that such a
    product exists or is in stock:

    * descriptors every shown product's own text carries -> exactly those products;
    * descriptors nothing in the range carries -> the in-stock range, with an honest
      line saying none is described that way (no semantic guess such as
      "blueberry means fruity");
    * the only descriptor matches are sold out -> said so, with what IS in stock;
    * the whole range sold out -> said so.
    Returns `(evidence, products, intent)` like any discovery turn, or `None` when
    this merchant declares no domain range (the caller then behaves as before)."""
    core = await _declared_domain_categories(adapter, vocab)
    if not core:
        return None
    range_products = await _products_in_categories(adapter, core, vocab)
    if not range_products:
        return None
    descriptors = [
        w for w in (
            vocabulary.content_words(norm.query, vocab)[0] + vocabulary.content_words(norm.query, vocab)[1]
        )
    ]
    stems = [vocabulary._stem(w) for w in descriptors]
    in_stock = [p for p in range_products if _buyable(p)]
    matched = [p for p in range_products if stems and all(st in _product_stems(p) for st in stems)] if stems else []
    matched_in_stock = [p for p in matched if _buyable(p)]

    reply: str | None = None
    if not stems:
        shown = in_stock
    elif matched_in_stock:
        shown = matched_in_stock
    else:
        shown = in_stock
        said = " ".join(descriptors)
        if matched:
            reply = (
                f"{_list_titles(matched)} fits \"{said}\" but "
                f"{'is' if len(matched) == 1 else 'are'} sold out right now."
            )
            if in_stock:
                reply += f" In stock instead: {_list_titles(in_stock)}."
        elif in_stock:
            reply = (
                f"Nothing in our {domain_word} range is described as \"{said}\" - I can only go by "
                f"what each product's description says. These {domain_word} options are in stock: "
                f"{_list_titles(in_stock)}."
            )
    if not shown:
        trace.query_normalization = {
            **(trace.query_normalization or {}), "outcome": "domain_range_sold_out",
            "domain_reply": f"Everything in our {domain_word} range is sold out right now.",
        }
        evidence = build_evidence(
            query=norm.query, raw_said=common["message"], products=[], shopping_state=common["shopping_state"],
            memory_facts=common["memory_facts"], catalog_sample=None, platform_unavailable=False,
        )
        return evidence, [], SearchIntent.from_shopping_state(
            raw_message=common["message"], query=norm.query, shopping_state=common["shopping_state"])
    result = await _run_discovery_turn_once(**common, delta_query=None, browse_all=True, preset_products=shown)
    if not any(e.relevance_verified for e in result[0].products):
        return None
    trace.query_normalization = {
        **(trace.query_normalization or {}),
        "corrections": norm.corrections, "dropped_domain_terms": norm.dropped_domain_terms,
        "outcome": "domain_grounded", "domain_categories": core, "descriptors": descriptors,
        **({"domain_reply": reply} if reply else {}),
    }
    return result


async def run_discovery_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    session_id: str,
    connection_id: str,
    message: str,
    delta_query: str | None,
    constraint_only_turn: bool = False,
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    trace: TurnTrace,
    category_hints: list[str] | None = None,
) -> tuple[TurnEvidence, list[Product], SearchIntent]:
    """The public discovery entry: normalize the shopper's words against the
    merchant's REAL catalog vocabulary (spelling + domain words - see
    `vocabulary.py`), run the pipeline on the normalized query, and VERIFY
    the result. A normalized search that verifies nothing never replaces a
    literal search that did: the shopper's own words are re-run and win
    whenever the correction found nothing real. Nothing is ever corrected
    against a general dictionary, and a vocabulary that cannot be built
    (platform down) degrades to exactly the pre-normalization behavior."""
    common = dict(
        reasoning=reasoning, adapter=adapter, session_id=session_id,
        connection_id=connection_id, message=message,
        constraint_only_turn=constraint_only_turn, shopping_state=shopping_state,
        memory_facts=memory_facts, trace=trace,
    )
    base_query = (delta_query or message or "").strip() or message
    norm = None
    vocab = None
    valid_categories: list[str] = []
    if not constraint_only_turn:
        try:
            vocab = await vocabulary.get_vocabulary(adapter, connection_id)
            if vocab is not None:
                norm = vocabulary.normalize_query(base_query, vocab)
                valid_categories = vocabulary.validate_categories(category_hints or [], vocab)
        except Exception:  # noqa: BLE001 - normalization must never break a turn
            logger.warning("shopper_v2 pipeline: query normalization failed", exc_info=True)
            norm = None

    # Every distinctive word the shopper used is unknown to this store's own
    # catalogue vocabulary ("jockey underwear", "coffee" at a running shop): the
    # store does not sell it. A whole-category fallback would answer a specific
    # request with an unrelated department (running tights for underwear,
    # electrolyte tablets for coffee), so it is withheld and the turn ends in an
    # honest "this store doesn't carry that" instead. Words the store DOES know
    # (including its own domain) never trigger this.
    not_carried = False
    if (
        vocab is not None and norm is not None and not constraint_only_turn
        and not norm.browse_only and _REQUEST_FRAME.search(message or "")
    ):
        # The store's own domain words ("coffee" at a coffee shop) are removed
        # from `norm.query` by normalization, but they are still words this
        # store knows: "a fruity coffee" is a request for coffee with an
        # unfamiliar adjective, never for something the store does not sell.
        known_words, unknown_words = vocabulary.content_words(
            " ".join([norm.query, *norm.dropped_domain_terms]), vocab,
        )
        not_carried = bool(unknown_words) and not known_words

    def _mark_not_carried(result):
        evidence, products, intent = result
        if not_carried and not any(e.relevance_verified for e in evidence.products):
            trace.query_normalization = {
                **(trace.query_normalization or {}), "outcome": "not_carried",
            }
            evidence = evidence.model_copy(update={"shop_carries_category": False})
        return evidence, products, intent

    async def _category_browse(outcome: str):
        preset = await _products_in_categories(adapter, valid_categories, vocab)
        if not preset:
            return None
        result = await _run_discovery_turn_once(
            **common, delta_query=None, browse_all=True, preset_products=preset,
        )
        if not any(e.relevance_verified for e in result[0].products):
            return None
        trace.query_normalization = {
            **(
                {"corrections": norm.corrections, "dropped_domain_terms": norm.dropped_domain_terms}
                if norm is not None else {}
            ),
            "grounded_categories": valid_categories,
            "outcome": outcome,
        }
        return result

    async def _variant_browse():
        """Last grounded resort before an honest no-match: a word that only product VARIANTS can ground ("beans"
        -> the "250g whole bean" option on every coffee). Real catalogue evidence, no model guess; the products
        come from a bounded browse sample (disclosed, like the category fallbacks) and then go through exactly the
        same hard filters (stock, budget, exclusions) as any other candidate."""
        if vocab is None or constraint_only_turn:
            return None
        query_text = norm.query if norm is not None else base_query
        words = vocabulary.variant_only_words(query_text, vocab)
        if not words:
            return None
        # Every distinctive word must be accounted for by variant evidence (the store's own domain word, already
        # neutralized by normalization, counts as accounted for). Any other word - unknown ("kayak") or a title
        # word the ordinary search already handled - means this is not a pure variant request, and grounding on
        # the variant word alone would answer a different question than the one asked.
        # (`query_text` is already normalized: the store's domain word is not in it, so it needs no check.)
        known, unknown = vocabulary.content_words(query_text, vocab)
        if unknown or any(vocabulary._stem(w) not in vocab.variant_stems for w in known):
            return None
        # EVERY known variant word must be carried by one variant, not only the variant-only ones: "ground beans"
        # is not "beans" just because "ground" also happens to appear in some description.
        terms = [w for w in known if vocabulary._stem(w) in vocab.variant_stems]
        preset = await _products_with_variant_terms(adapter, terms)
        if not preset:
            return None
        result = await _run_discovery_turn_once(
            **common, delta_query=None, browse_all=True, preset_products=preset,
        )
        if not any(e.relevance_verified for e in result[0].products):
            return None
        trace.query_normalization = {
            **(
                {"corrections": norm.corrections, "dropped_domain_terms": norm.dropped_domain_terms}
                if norm is not None else {}
            ),
            "grounded_variant_terms": words,
            "outcome": "variant_grounded",
        }
        return result

    # The request names one of the merchant's declared DEPARTMENTS exactly ("Show me Footwear" - the tap of a
    # category choice, or the same words typed): browse that department. The platform's own keyword search does
    # not match department names (only 1 of Northfield's 6 departments returned anything), and a title match on the
    # name ("Recovery Slide" for "Recovery") would show products from OTHER departments.
    if vocab is not None and not constraint_only_turn:
        named_departments = vocabulary.departments_named(base_query, vocab)
        if named_departments:
            saved_categories = valid_categories
            valid_categories = named_departments
            try:
                grounded = await _category_browse("department_named_browse")
            finally:
                valid_categories = saved_categories
            if grounded is not None:
                return grounded

    # Several products named in one request ("the Trailblazer and the Starter Road Shoe"): each named part is resolved
    # on its own and every one found is shown, in the order named; a part that cannot be found is said so. The
    # ordinary search would keep only the top-scoring match and silently drop the rest.
    if vocab is not None and not constraint_only_turn:
        try:
            multi = await named_product.resolve_named_products(adapter=adapter, message=message, vocab=vocab)
        except Exception:  # noqa: BLE001 - never break a turn on this
            logger.warning("shopper_v2 pipeline: multi-product resolution failed", exc_info=True)
            multi = None
        if multi is not None:
            result = await _run_discovery_turn_once(
                **common, delta_query=None, browse_all=True, preset_products=list(multi.products),
            )
            evidence, shown_products, intent = result
            verified = {e.product_id for e in evidence.products if e.relevance_verified}
            order = {p.product_id: i for i, p in enumerate(multi.products)}
            shown_products = sorted(
                [p for p in shown_products if p.product_id in verified], key=lambda p: order.get(p.product_id, 99),
            )
            evidence = evidence.model_copy(update={"products": sorted(
                evidence.products, key=lambda e: order.get(e.product_id, 99),
            )})
            unavailable = [p.title for p in multi.products if p.product_id not in verified]
            parts = []
            if shown_products:
                parts.append("Here " + ("they are" if len(shown_products) > 1 else "it is") + ": "
                             + ", ".join(p.title for p in shown_products) + ".")
            if unavailable:
                parts.append(f"{', '.join(unavailable)} {'is' if len(unavailable) == 1 else 'are'} not available right now "
                             "(out of stock or outside what you asked for).")
            if multi.missing:
                parts.append(f"I couldn't find: {', '.join(multi.missing)}.")
            if multi.ambiguous:
                parts.append(f"{', '.join(multi.ambiguous)} could mean several products - tell me which one.")
            trace.query_normalization = {
                "outcome": "named_products", "named": [p.product_id for p in multi.products],
                "missing": multi.missing, "ambiguous": multi.ambiguous, "unavailable": unavailable,
                "domain_reply": " ".join(parts),
            }
            return evidence, shown_products, intent

    # A shop-domain word with or without descriptors ("coffee", "a fruity coffee")
    # at a merchant that DECLARES what its domain range is: answered from that
    # range deterministically, no interpreter needed (see `_domain_grounded_turn`).
    if norm is not None and vocab is not None and norm.dropped_domain_terms and not constraint_only_turn:
        try:
            domain_result = await _domain_grounded_turn(
                adapter=adapter, vocab=vocab, norm=norm, domain_word=norm.dropped_domain_terms[0],
                common=common, trace=trace,
            )
        except Exception:  # noqa: BLE001 - grounding must never break a turn
            logger.warning("shopper_v2 pipeline: domain grounding failed", exc_info=True)
            domain_result = None
        if domain_result is not None:
            return domain_result

    # The shopper asked for the store's whole domain ("show me coffee") and
    # the interpreter grounded it onto real categories: browse THOSE, not
    # the entire store (equipment/gifts are not what "coffee" means).
    if valid_categories and norm is not None and norm.browse_only:
        grounded = await _category_browse("category_grounded_browse")
        if grounded is not None:
            return grounded

    if norm is not None and norm.changed:
        evidence, products, intent = await _run_discovery_turn_once(
            **common, delta_query=norm.query, browse_all=norm.browse_only,
            raw_said_extra=" ".join(norm.corrections.values()),
        )
        if any(e.relevance_verified for e in evidence.products):
            trace.query_normalization = {
                "corrections": norm.corrections,
                "dropped_domain_terms": norm.dropped_domain_terms,
                "browse_only": norm.browse_only,
                "outcome": "normalized_verified",
            }
            return evidence, products, intent
        literal = await _run_discovery_turn_once(**common, delta_query=delta_query)
        literal_verified = any(e.relevance_verified for e in literal[0].products)
        trace.query_normalization = {
            "corrections": norm.corrections,
            "dropped_domain_terms": norm.dropped_domain_terms,
            "browse_only": norm.browse_only,
            "outcome": "literal_kept" if literal_verified else "neither_verified",
        }
        # Prefer whichever attempt found something real; when neither did,
        # a category the interpreter grounded (and the taxonomy confirmed) is
        # the last real option before an honest no-match.
        if not literal_verified and valid_categories and not not_carried:
            grounded = await _category_browse("category_grounded_after_no_match")
            if grounded is not None:
                return grounded
        if not literal_verified:
            grounded = await _variant_browse()
            if grounded is not None:
                return grounded
        return _mark_not_carried(literal)

    literal_result = await _run_discovery_turn_once(**common, delta_query=delta_query)
    if (
        valid_categories and not not_carried
        and not any(e.relevance_verified for e in literal_result[0].products)
    ):
        grounded = await _category_browse("category_grounded_after_no_match")
        if grounded is not None:
            return grounded
    if not any(e.relevance_verified for e in literal_result[0].products):
        grounded = await _variant_browse()
        if grounded is not None:
            return grounded
    return _mark_not_carried(literal_result)


def _judge_pick_passes_hard_filter(product: Product, shopping_state: dict | None) -> bool:
    """The semantic judge only ever proposes a product it found plausible
    by category/description - it has no concept of the shopper's stated
    budget or exclusions, because it is never given `shopping_state` to
    reason over. Structural fix for a live-reproduced bug: a query like
    "lightweight running shoes under 2000" found no literal keyword match,
    the judge picked a real running shoe on category grounds alone (the
    Trailblazer, INR 4299), and the caller marked it `relevance_verified`
    with no budget check at all - a genuine hard-constraint violation
    reaching the shopper. Reuses the SAME deterministic hard-filter
    (`mission_ranking.rank_and_filter`) every other candidate in this turn
    was already checked against, on a singleton list - not a new
    heuristic, not a second budget-parsing implementation."""
    from .evidence import product_as_dict

    shortlist, _ = mission_ranking.rank_and_filter(
        [product_as_dict(product)], shopping_state, limit=1
    )
    return bool(shortlist)


async def _try_semantic_judge(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    evidence: TurnEvidence,
    already_fetched: list[Product],
    shopping_state: dict | None,
    trace: TurnTrace,
) -> tuple[TurnEvidence, Product | None]:
    """Fetches a bounded catalog sample (read-only browse, same shape as
    V1's `_full_catalog_sample` pattern) only when actually needed, judges
    plausibility, and - only if plausible - asks which single candidate
    best fits. Never invents a product: `judge_best_match` only ever
    returns an id present in the sample it was given, verified against
    that same set before being trusted (see `ReasoningService.judge_best_match`'s
    own docstring - this module adds no additional trust here).

    Returns (possibly-updated evidence, the real `Product` object the
    judge picked - or `None` if nothing changed). The caller merges the
    returned `Product` into its own fetched-products list, since a product
    the judge picks from the wider sample may not have been part of the
    turn's original narrow-search candidates.

    The judge's pick is advisory only, exactly like every other candidate
    this turn: it is never marked `relevance_verified` unless it ALSO
    passes the same deterministic hard filter (budget/exclusions/stock)
    every other product was checked against - see
    `_judge_pick_passes_hard_filter`. A pick that fails is treated as no
    match, not silently downgraded to unverified-but-shown."""
    # Step 22 live finding (gpt-4o-mini, 2 of 2): "beans" at a coffee shop
    # matched no product text, and this judge used to BROWSE a generic
    # catalogue sample and pick one product as "the best fit" - a manufactured
    # single answer to a request that names a whole class of products, which
    # also pre-empted the grounded category path (a verified pick meant
    # "something was found", so the interpreter's taxonomy-validated
    # categories were never browsed). The judge now only ever chooses among
    # this turn's REAL retrieved candidates (`already_fetched`); with none
    # there is nothing to judge, and the turn ends in the honest no-match
    # reply (or the grounded category browse, when the interpreter supplied
    # categories the store's own taxonomy confirms).
    if not already_fetched:
        return evidence, None
    sample = list(already_fetched)

    categories = sorted({c for p in sample for c in (p.categories or [])})
    sample_titles = [
        f"{p.title} - {p.description}"[:160] if p.description else p.title
        for p in sample[:_CATALOG_SAMPLE_LIMIT]
    ]

    trace.llm_judge_used = True
    trace.model_calls += 1
    try:
        plausible = await asyncio.wait_for(
            reasoning.judge_relevance(
                need=message, categories=categories, sample_titles=sample_titles
            ),
            timeout=_JUDGE_TIMEOUT_S,
        )
    except TimeoutError:
        logger.info("shopper_v2 pipeline: semantic relevance judgment timed out")
        plausible = None
    except Exception:  # noqa: BLE001 - same degrade-honestly rule as V1's own call site
        logger.exception("shopper_v2 pipeline: semantic relevance judgment failed")
        plausible = None
    trace.llm_judge_result = plausible

    if not plausible:
        return evidence, None

    candidates_by_id = {p.product_id: p for p in sample}

    # Bounded retry: if the judge's pick fails the shopper's own hard
    # constraints (budget/exclusions/stock), it does not mean nothing in
    # the sample qualifies - only that this particular pick doesn't. Retry
    # with that pick excluded, so a genuinely in-budget candidate sitting
    # in the SAME sample (e.g. a 699 INR product the judge overlooked in
    # favour of a 4299 INR one) gets a real second look, rather than the
    # whole rescue giving up after one wrong guess. Capped at
    # `_MAX_JUDGE_PICK_ATTEMPTS` - this is a cost-bounded retry, not a loop
    # that could run away on a stubborn model (see the Groq-throttle
    # constraint this package's own docs call "the single most concrete
    # constraint Stage 2 must design against").
    excluded_ids: set[str] = set()
    picked_id: str | None = None
    picked_product: Product | None = None
    for _attempt in range(_MAX_JUDGE_PICK_ATTEMPTS):
        remaining = [p for p in sample if p.product_id not in excluded_ids][:12]
        if not remaining:
            break
        payload = [
            {"product_id": p.product_id, "title": p.title, "description": p.description}
            for p in remaining
        ]
        trace.model_calls += 1
        try:
            candidate_id = await asyncio.wait_for(
                reasoning.judge_best_match(need=message, candidates=payload),
                timeout=_JUDGE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.info("shopper_v2 pipeline: best-match judgment timed out")
            candidate_id = None
        except Exception:  # noqa: BLE001
            logger.exception("shopper_v2 pipeline: best-match judgment failed")
            candidate_id = None

        if candidate_id is None or candidate_id not in candidates_by_id:
            break

        candidate_product = candidates_by_id[candidate_id]
        if _judge_pick_passes_hard_filter(candidate_product, shopping_state):
            picked_id, picked_product = candidate_id, candidate_product
            trace.llm_judge_pick_passed_hard_filter = True
            break

        logger.info(
            "shopper_v2 pipeline: semantic judge picked %s but it fails the "
            "shopper's own hard constraints (budget/exclusion/stock) - "
            "excluding it and giving the judge another look",
            candidate_id,
        )
        trace.llm_judge_pick_passed_hard_filter = False
        excluded_ids.add(candidate_id)

    if picked_id is None or picked_product is None:
        return evidence, None

    existing_ids = {e.product_id for e in evidence.products}
    if picked_id not in existing_ids:
        from .evidence import ProductEvidence

        new_entry = ProductEvidence(
            product_id=picked_id,
            exists=True,
            category_match=None,
            hard_constraints_passed=True,
            relevance_verified=True,
            relevance_score=0,
            weak_match=False,
            evidence_fields=["semantic_judge_match"],
        )
        new_evidence = TurnEvidence(
            query=evidence.query,
            shop_carries_category=True,
            products=[*evidence.products, new_entry],
            no_match_reason=None,
        )
        return new_evidence, picked_product

    # Already among this turn's fetched candidates but the deterministic
    # layer withheld confidence - upgrade that one entry rather than
    # duplicate it. `hard_constraints_passed` is already re-verified above
    # via `_judge_pick_passes_hard_filter` (the same deterministic filter,
    # not this entry's possibly-stale flag), so it is safe to also set it
    # True here rather than leave a verified-but-marked-failed contradiction.
    updated = [
        e.model_copy(
            update={
                "hard_constraints_passed": True,
                "failed_constraints": [],
                "relevance_verified": True,
                "evidence_fields": [*e.evidence_fields, "semantic_judge_match"],
            }
        )
        if e.product_id == picked_id
        else e
        for e in evidence.products
    ]
    new_evidence = TurnEvidence(
        query=evidence.query,
        shop_carries_category=True,
        products=updated,
        no_match_reason=(
            None if any(e.relevance_verified for e in updated) else evidence.no_match_reason
        ),
    )
    return new_evidence, picked_product
