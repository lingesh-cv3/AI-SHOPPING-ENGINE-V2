"""The Evidence object: wraps the EXISTING deterministic relevance/ranking
logic (`engine.execution.relevance`, `engine.execution.mission_ranking`)
behind named fields, per the Stage 1 spec. No new scoring heuristic is
introduced here - every verdict below is produced by calling an existing,
already-hardened function, not by re-deriving its logic.

This module is the direct, structural answer to "product exists != product
is relevant": a `ProductEvidence.exists=True` entry is never enough on its
own for `respond.py` to describe a product as a match - `relevance_verified`
must also be True.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from engine.execution import mission_ranking
from engine.execution.relevance import (
    any_overlap_ids,
    best_matches_with_score,
    query_diverges_from_said,
    shop_carries,
    weak_single_word_match,
)
from shared.models import Product


class ProductEvidence(BaseModel):
    product_id: str
    exists: bool
    category_match: bool | None = None
    hard_constraints_passed: bool
    failed_constraints: list[str] = Field(default_factory=list)
    relevance_verified: bool
    relevance_score: int
    weak_match: bool
    #: True when `weak_match` was the ONLY reason relevance was withheld,
    #: and this product was the single hard-filter survivor with no other
    #: candidate to be ambiguous against (Stage 2A Finding 3's fix) - see
    #: `build_evidence`'s own comment on `_single_survivor_override`.
    weak_match_overridden: bool = False
    evidence_fields: list[str] = Field(default_factory=list)


class TurnEvidence(BaseModel):
    query: str | None
    shop_carries_category: bool | None = None
    products: list[ProductEvidence] = Field(default_factory=list)
    no_match_reason: str | None = None


def build_evidence(
    *,
    query: str | None,
    raw_said: str,
    products: list[Product],
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    catalog_sample: list[Product] | None,
    platform_unavailable: bool,
) -> TurnEvidence:
    """Verify, never invent. Every product in the returned `products` list
    came from `products` (i.e. `exists=True` for all of them, by
    construction - this function never adds an id that wasn't already
    fetched); a product failing verification is dropped from the response
    entirely rather than shown with a caveat, matching the "No-Match
    Behavior" contract (never silently substitute, never present a
    non-match as a match)."""
    if platform_unavailable:
        return TurnEvidence(
            query=query, products=[], no_match_reason="platform_unavailable"
        )

    if not products:
        shop_carries_category = (
            shop_carries(query, catalog_sample or []) if query else None
        )
        return TurnEvidence(
            query=query,
            shop_carries_category=shop_carries_category,
            products=[],
            no_match_reason="no_candidates",
        )

    # Hard-filter + soft-rank via the EXISTING deterministic ranking layer -
    # same function, same semantics V1 already relies on for
    # RECOMMEND_PRODUCTS. `rank_and_filter` takes plain dicts; Product ->
    # dict via each product's own known fields, matching what that module
    # already expects from adapter results elsewhere in this codebase.
    #
    # Stage 2A Finding 2 fix: `rank_and_filter`'s own default `limit`
    # (mission_ranking.DEFAULT_LIMIT = 4) conflates its hard-filter pass
    # (exclusions/budget/stock - a real "continue, drop it" decision) with
    # its own separate soft top-N display cut. Measured live: a candidate
    # set of >4 products with no priority/must-have/budget signal in play
    # scores every survivor at 0 except a rating tiebreaker, and the
    # default limit then silently drops a genuinely relevant product for
    # no reason but candidate-set size (see SHOPPER_V2_BASELINE_REPORT.md,
    # `kb-lang-01`). Passing `limit=len(product_dicts)` here recovers the
    # hard-filter-ONLY view - every product that actually survives
    # exclusions/budget/stock, uncapped - so `hard_passed` below reflects
    # real hard-filter survival rather than an arbitrary display-size cut.
    # `mission_ranking.py` itself is untouched: this is the same call
    # `engine/shopper_v2/eval/adapt.py::hard_filter` already makes for
    # measurement; this is that same fix now applied to the live pipeline,
    # not just the offline evaluation harness.
    product_dicts = [_as_dict(p) for p in products]
    shortlist, fit_reasons = mission_ranking.rank_and_filter(
        product_dicts,
        shopping_state,
        memory_facts=memory_facts,
        limit=max(len(product_dicts), 1),
    )
    shortlisted_ids = {p["product_id"] for p in shortlist}

    top_score, best = (0, [])
    if query:
        top_score, best = best_matches_with_score(products, query)
    best_ids = {p.product_id for p in best} if query else {p.product_id for p in products}

    diverges = False
    if query:
        diverges = query_diverges_from_said(query, raw_said, candidates=products)
    weak = weak_single_word_match(query, top_score) if query else False

    # Stage 2A Finding 3 fix: `weak_single_word_match`'s `top_score == 1`
    # rule cannot, by itself, distinguish "the one and only correct
    # product, named in a single word" (nf-multi-01's "sneakers" ->
    # Everyday Canvas Sneaker; nf-attr-02's "waterproof jacket" -> Packable
    # Rain Jacket) from "a coincidental one-word overlap with an unrelated
    # product" - both produce identical evidence. `mission_ranking.py` and
    # `relevance.py` are untouched (this stays a V2-only interpretation
    # layered on top of their existing, unmodified signal): when exactly
    # ONE candidate survives hard filtering at all, there is no second
    # candidate for a one-word match to be ambiguous AGAINST - ambiguity
    # requires a rival. A divergent query (the model naming something the
    # shopper never said) is a different, unrelated risk and is NOT
    # overridden by this rule.
    #
    # Generalization ("show me shoes" showing only 1 of 4 real shoes):
    # `top_score == 1` is also the ONLY possible value whenever a query's
    # sole significant word is a plain category name shared by several
    # real products (P1001/P1002/P1005/P1008 all score 1 on "shoe") - the
    # single-survivor rule above never fires because there are several
    # survivors, not because the match is actually ambiguous. The genuine
    # ambiguity case Finding 3 is guarding against is a survivor that
    # DIDN'T make the top keyword score riding along with ones that did
    # (e.g. a "Running Cap" hard-filtered alongside real running shoes
    # would score lower on "shoes" and be excluded from best_ids). So the
    # safe generalization is: every hard-filter survivor is ALSO a
    # top-scoring match - no lower-scoring outlier is hiding among them.
    # That is a strict superset of the single-survivor case (trivially
    # true when there is only one) and never fires on a mixed bag.
    # Generalized fix: a hard-filter survivor with ZERO real word overlap
    # with the query (it merely didn't violate budget/stock/exclusions -
    # nothing about it says "this might be what the query means") is not a
    # rival interpretation and must never block the single-strong-match
    # override below. Live-reproduced, platform-agnostic gap: a mission
    # item labeled "something for energy" retrieved a real energy product
    # (one genuine word overlap) ALONGSIDE an unrelated sock that shares no
    # word with the query at all - the sock still counted as a "survivor"
    # under the old, unrestricted check, made `all_survivors_are_top_matches`
    # false, and starved the override that exists exactly for this shape of
    # match. Restricting to survivors with `any_overlap_ids` (relevance.py)
    # keeps the override's own safety property (a genuine rival with real,
    # if lower, overlap still blocks it) while no longer letting completely
    # irrelevant candidates - on ANY merchant, ANY category, any vague-but-
    # genuine item label - do the same. See that function's own docstring.
    relevant_survivor_ids = (
        shortlisted_ids & any_overlap_ids(products, query) if query else shortlisted_ids
    )
    all_survivors_are_top_matches = bool(shortlisted_ids) and relevant_survivor_ids.issubset(
        best_ids
    )

    # Batch 4 fix (V2 completion audit): `rank_and_filter`'s own score/order
    # used to be read ONLY for shortlist membership above - `evidence_products`
    # was then built by iterating `products` in the adapter's raw search-
    # result order, so ranking (budget/must-have/priority scoring, and this
    # batch's own durable-memory personalization) could change WHICH
    # products were verified but never the ORDER they were shown in. A
    # shopper's stated priority or a signed-in durable preference had no
    # visible effect on the shown sequence at all - live-reproduced:
    # `prefers_lightweight` never moved a genuinely lightweight-described
    # product up, because nothing downstream of `rank_and_filter` ever
    # consulted its own ordering. Reordered here to `shortlist`'s real
    # order for every hard-filter survivor, followed by every
    # non-survivor in its original order (so a filtered-out product is
    # still reported on, never silently dropped from evidence - only
    # WHERE it appears among survivors changes).
    id_to_product = {p.product_id: p for p in products}
    ranked_survivors = [
        id_to_product[pid] for pid in (p["product_id"] for p in shortlist)
        if pid in id_to_product
    ]
    non_survivors = [p for p in products if p.product_id not in shortlisted_ids]
    products = ranked_survivors + non_survivors

    evidence_products: list[ProductEvidence] = []
    for p in products:
        hard_passed = p.product_id in shortlisted_ids
        failed: list[str] = []
        if not hard_passed:
            failed.append("hard_constraint_or_out_of_stock")

        # Mirrors the actual gate `engine/execution/service.py` applies
        # (search for `weak_single_word_match(...) or
        # query_diverges_from_said(...)` there): EITHER signal alone is
        # enough to withhold confident relevance, not only both together -
        # a product sharing exactly one word (weak) or reached via a
        # query the shopper never actually said (diverges) is not a
        # confirmed match, whichever single reason applies.
        overridden = (
            hard_passed
            and all_survivors_are_top_matches
            and p.product_id in best_ids
            and weak
            and not diverges
        )
        relevant = p.product_id in best_ids and not diverges and (not weak or overridden)
        fields: list[str] = []
        if p.product_id in fit_reasons:
            fields.extend(fit_reasons[p.product_id])
        if p.product_id in best_ids:
            fields.append("title_or_description_overlap")
        if overridden:
            fields.append("sole_hard_filter_survivor_weak_match_overridden")

        evidence_products.append(
            ProductEvidence(
                product_id=p.product_id,
                exists=True,
                category_match=hard_passed if shopping_state else None,
                hard_constraints_passed=hard_passed,
                failed_constraints=failed,
                relevance_verified=hard_passed and relevant,
                relevance_score=top_score,
                weak_match=weak,
                weak_match_overridden=overridden,
                evidence_fields=fields,
            )
        )

    shop_carries_category = None
    no_match_reason = None
    if not any(e.relevance_verified for e in evidence_products):
        shop_carries_category = shop_carries(query, catalog_sample or []) if query else None
        no_match_reason = "no_relevant_match"

    return TurnEvidence(
        query=query,
        shop_carries_category=shop_carries_category,
        products=evidence_products,
        no_match_reason=no_match_reason,
    )


def already_verified_evidence(
    products: list[Product], evidence_fields_by_id: dict[str, list[str]] | None = None
) -> TurnEvidence:
    """Shared-contract helper for a capability whose products are already
    trusted by construction - the shopper explicitly named or is directly
    re-examining them (comparison's `active_products` targets,
    trade-off's re-ranked set), not a fresh query result `build_evidence`'s
    hard-filter/relevance-scoring pass would need to verify.

    Deliberately NOT built by calling `build_evidence` itself: that
    function's hard filter (`mission_ranking.rank_and_filter`) would
    re-apply the shopper's CURRENT budget/exclusions to a product they
    explicitly named a turn ago - silently dropping a comparison target
    the shopper is looking straight at because a later, unrelated
    statement changed their budget would be a real regression, not a
    correctness improvement. `compare.py` and `tradeoff.py` both used to
    hand-roll this identical shape (one `ProductEvidence` per product,
    `hard_constraints_passed=True`, `relevance_verified=True`,
    `relevance_score=0`, `weak_match=False`) with only `evidence_fields`
    differing - consolidated here so a third capability with the same
    "already-in-view, already-trusted" shape (a future post-purchase or
    return-risk module, say) has one real place to reuse rather than a
    third hand-rolled copy."""
    fields_by_id = evidence_fields_by_id or {}
    return TurnEvidence(
        query=None,
        products=[
            ProductEvidence(
                product_id=p.product_id,
                exists=True,
                hard_constraints_passed=True,
                relevance_verified=True,
                relevance_score=0,
                weak_match=False,
                evidence_fields=fields_by_id.get(p.product_id, []),
            )
            for p in products
        ],
    )


def product_as_dict(p: Product) -> dict:
    """The plain-dict shape `mission_ranking.rank_and_filter` already
    expects (see `engine/api/chat.py`'s own RECOMMEND_PRODUCTS branch,
    which builds an equivalent dict before calling it) - field names match
    that existing contract exactly, not reinvented here.

    Public (promoted from the former `_as_dict`) so every V2 module that
    needs to hand a `Product` to `mission_ranking` uses this ONE
    conversion - `pipeline.py`'s semantic-judge hard-constraint check
    (`_judge_pick_passes_hard_filter`) and `tradeoff.py`/`compare.py`'s
    shared-evidence adoption both need it; a second private copy is
    exactly the duplication this package's shared-contract work exists to
    close."""
    return {
        "product_id": p.product_id,
        "title": p.title,
        "description": p.description,
        "categories": list(p.categories or []),
        "price": str(p.price) if p.price is not None else None,
        "availability": str(p.availability),
        "rating": p.rating,
        "rating_count": p.rating_count,
    }


#: Backward-compatible alias - existing in-module call sites keep working
#: unchanged; new external callers should use `product_as_dict`.
_as_dict = product_as_dict


#: Internal-only evidence tags (real, but written for THIS module's own
#: `all_survivors_are_top_matches`/weak-match bookkeeping, not shopper
#: prose) - never shown verbatim in a "Why this?" panel.
_INTERNAL_EVIDENCE_TAGS = {
    "title_or_description_overlap",
    "sole_hard_filter_survivor_weak_match_overridden",
}


def build_why_v2(
    evidence: TurnEvidence,
    shopping_state: dict | None,
    products: list,
) -> dict | None:
    """Grounded "Why this?" panel for a V2 turn - real fields only, the
    same discipline `engine/api/why.py::explain` already applies for V1,
    just built from THIS package's own real per-turn signals
    (`ProductEvidence.evidence_fields` - itself only ever real
    `mission_ranking.rank_and_filter` `fit_reasons` phrases or a real
    stock fact - never a model guess) instead of V1's decision/risk
    rejection list, since a V2 discovery/compare/mission turn has no
    rejected ACTIONS to report on at all.

    Conversational-flow review fix (Issue #7): V1's own `why` field was
    never wired into `ChatReplyV2` at all - the frontend's existing
    "Why this?" button silently never rendered for a single V2 turn,
    since `turn.why` was always `None`. Returns `None` (never renders)
    when there is genuinely nothing grounded to show - never a placeholder
    or invented justification."""
    verified = [p for p in evidence.products if p.relevance_verified]
    if not verified:
        return None
    top = verified[0]

    evidence_lines = [f for f in top.evidence_fields if f not in _INTERNAL_EVIDENCE_TAGS]

    id_to_product = {
        (p.product_id if hasattr(p, "product_id") else p.get("product_id")): p
        for p in products
    }
    top_product = id_to_product.get(top.product_id)
    if top_product is not None:
        availability = getattr(top_product, "availability", None)
        if availability is None and isinstance(top_product, dict):
            availability = top_product.get("availability")
        if availability:
            evidence_lines.append(f"in stock: {availability}")
        return_risk = getattr(top_product, "return_risk", None)
        if return_risk is None and isinstance(top_product, dict):
            return_risk = top_product.get("return_risk")
        for finding in return_risk or []:
            note = finding.get("note") if isinstance(finding, dict) else None
            if note:
                evidence_lines.append(note)

    # What else was real and in view, but didn't make the cut - phrased
    # generically (never attributing a specific reason to a specific
    # un-named product this module cannot honestly single out), and only
    # ever built from the shopper's OWN real stated constraints, exactly
    # as `mission_ranking.explain_no_match` already does for the no-match
    # case.
    declined: list[str] = []
    non_survivors = [p for p in evidence.products if not p.relevance_verified and p.exists]
    if non_survivors:
        reasons: list[str] = []
        state = shopping_state or {}
        must_haves = [m for m in (state.get("must_have") or []) if m]
        budget = state.get("budget_amount")
        if must_haves:
            reasons.append(f"didn't confirm {must_haves[0]}" if len(must_haves) == 1 else "didn't confirm every requirement")
        if budget:
            reasons.append(f"were over your {budget} budget")
        if reasons:
            count = len(non_survivors)
            declined.append(
                f"{count} other option{'s' if count != 1 else ''} that {' or '.join(reasons)}"
            )

    if not evidence_lines and not declined:
        return None

    found: list[str] = []
    if evidence.query:
        found.append(f'Matched against "{evidence.query}".')

    return {
        "found": found,
        "declined": declined[:3],
        "evidence": evidence_lines[:4],
    }
