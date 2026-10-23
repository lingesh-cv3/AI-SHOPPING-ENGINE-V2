"""Rev. 2 architecture (2026-09), Phase 3: deterministic verification.

Pure, synchronous, no model call, no adapter call - every function here
takes an `InterpretationResult` (a set of CHECKABLE CLAIMS from
`ReasoningService.interpret()`) plus real, already-fetched data, and
returns either a real, resolved answer or an honest "could not resolve"
signal. Nothing here ever invents a product id or a topic id; every
resolution is a lookup against structure the caller has already verified
is real.

This module absorbs the pure resolution primitives `reference_resolution.py`
already has (`resolve_group_selector`, `resolve_comparative_pair`,
`_distinctive_title_words`) as internals - the fix for Root Cause 2 (two
independently-scoped, disagreeing definitions of "what's in view") is
precisely that this is now the ONE place any capability's reference gets
resolved, never a second, capability-private copy of the same logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.execution.relevance import best_matches_with_score
from engine.reasoning.service import InterpretationResult, SecondaryOperation, SecondaryReference
from shared.models import Product

from .attributes import get_attribute_value, resolve_attribute_name
from .conversation_context import ConversationContext
from . import vocabulary
from .reference_resolution import _distinctive_title_words
from .topic_transition import _label_matches


@dataclass
class ResolvedReference:
    """The real, verified answer to a `StructuredReference` claim -
    `product_ids` is always either empty (nothing resolved) or a set of
    real ids that genuinely exist in the scope that was checked. Never a
    guess: `ambiguous=True` means real candidates exist but more than one
    is plausible, and the caller must ask, never pick one."""

    product_ids: list[str] = field(default_factory=list)
    ambiguous: bool = False
    #: The real candidates that made it ambiguous, or that were searched
    #: and found nothing - named so a clarifying question can name them,
    #: never a generic "here's everything" fallback.
    candidates_considered: list[Product] = field(default_factory=list)
    #: True when the shopper named a real attribute concept
    #: (`reference_attribute`) that does not map to any real, populated
    #: field on the candidates in scope - distinct from ordinary
    #: ambiguity, so the caller can say "I don't have <attribute> data
    #: for these" rather than a generic "which one did you mean?".
    attribute_unavailable: bool = False
    #: The real, resolved field name and value actually used to make this
    #: pick (e.g. `("price", 1999.0)`) - the one thing response generation
    #: is allowed to state a number from. `None` when nothing numeric was
    #: resolved (e.g. an ordinal/named pick).
    resolved_attribute: tuple[str, float] | None = None


@dataclass
class VerifiedInterpretation:
    """The checked, trustworthy result of a turn's interpretation -
    downstream capability dispatch reads ONLY this, never the raw
    `InterpretationResult` claim directly."""

    capability: str
    context_action: str  # "continue" | "push" | "resume" - "ambiguous" is always resolved to one of these
    context_target_topic_id: str | None = None
    new_topic_label: str | None = None
    resolved_reference: ResolvedReference | None = None
    cart_operation: str = "none"
    conversational_repair: str = "none"
    needs_clarification: bool = False
    durable_fact_candidates: list = field(default_factory=list)
    #: The semantic operation this turn wants - see
    #: `InterpretationResult.question_type`'s own docstring. Downstream
    #: response generation reads THIS, never the raw claim, to decide the
    #: reply's shape.
    question_type: str = "retrieve"
    #: The second, independently-resolved side of a "relationship"/
    #: "compound_comparison" question - `None` for every other
    #: question_type, or when the claim carried no `secondary_reference`.
    resolved_secondary: ResolvedReference | None = None
    #: Production architecture (2026-09), Phase 5 - compound turns.
    #: Passed through UNRESOLVED (this module has no real results to
    #: resolve `reference_names` against yet - the primary operation
    #: hasn't run) - `graph.py::node_discovery` resolves it inline, once
    #: its own real results exist, using an intra-turn handle. `None` for
    #: the overwhelmingly common single-operation turn.
    secondary_operation: SecondaryOperation | None = None
    #: Category names the interpreter claimed for a non-category search
    #: word. Passed through UNVALIDATED (this module has no taxonomy) -
    #: `pipeline.py` checks every name against the merchant's real
    #: categories before using any of it.
    catalog_categories: list[str] = field(default_factory=list)


def _effective_label(topic, products_by_id: dict[str, Product]) -> str:
    """A topic's own real label, or - for a legacy-bridged synthetic topic
    with no label at all (see `conversation_context.py::build_context`'s
    own docstring) - a deterministic stand-in built from its own real
    products' titles, so label-overlap matching has real words to work
    with instead of comparing against an empty string. Never invented
    text: every word here traces back to a real, already-fetched product
    title."""
    if topic.label:
        return topic.label
    titles = [products_by_id[pid].title for pid in topic.product_ids if pid in products_by_id]
    return ", ".join(titles[:3])


def resolve_context_action(
    claim: InterpretationResult, context: ConversationContext, products: list[Product] | None = None,
) -> tuple[str, str | None]:
    """Checks the claimed `context_action` against real `paused_topics` -
    a claimed "resume" or "ambiguous" must name a real paused topic (by
    label match, the same `_label_matches` word-overlap check
    `topic_transition.py`'s own RESUME rule already trusts) or it is
    downgraded to "push," never silently guessed at. "push"/"continue"
    pass through - `graph.py`'s own apply/mutation logic (unchanged) still
    owns actually creating or activating a topic."""
    products_by_id = {p.product_id: p for p in (products or [])}
    if claim.context_action in ("resume", "ambiguous") and claim.context_target_label:
        match = next(
            (
                t for t in context.paused_topics
                if _label_matches(claim.context_target_label, _effective_label(t, products_by_id))
            ),
            None,
        )
        if match is not None:
            return "resume", match.topic_id
        # Fallback: the model may have echoed the ACTIVE topic's own label
        # (a genuine "continue," not a resume) rather than naming a real
        # paused one - check before giving up and pushing.
        if context.active_topic is not None and _label_matches(
            claim.context_target_label, _effective_label(context.active_topic, products_by_id),
        ):
            return "continue", context.active_topic.topic_id
        # Legacy-bridge fallback (see `conversation_context.py::build_context`'s
        # own docstring): when even a title-derived label matches nothing
        # AND there's only one paused topic, resuming it unconditionally is
        # exactly `topic_transition.py`'s own already-proven "historical
        # phrasing, exactly one paused topic -> resume it" rule, never a
        # new guess this module invents.
        if len(context.paused_topics) == 1:
            return "resume", context.paused_topics[0].topic_id
    if claim.context_action == "push":
        return "push", None
    if claim.context_action == "resume":
        # Claimed a specific resume that didn't check out against anything
        # real - never silently push either; this is exactly the kind of
        # claim that should escalate to a narrower re-interpretation
        # (Phase 5) before falling back. For now (Phase 3, not yet wired),
        # the honest, structural answer is: nothing to resume, treat as new.
        return "push", None
    return "continue", context.active_topic.topic_id if context.active_topic else None


#: Reference kinds whose meaning is relative to what is currently in view
#: (positions, "all of them", comparatives/superlatives over "these") - as
#: opposed to `named`, which is matched by the product's own words against
#: whatever pool the caller fetched.
_RELATIVE_REFERENCE_KINDS = frozenset({"ordinal", "all", "comparative_pair", "superlative"})


def _candidates_for_scope(
    claim: InterpretationResult, context: ConversationContext, products: list[Product],
) -> list[Product]:
    """The real product set the claimed reference scope points to, in the
    scope's own REAL DISPLAY ORDER - `products` is whatever the caller
    already fetched for the relevant scope (last-rendered/active/paused/
    named); this function never fetches anything itself, only selects
    among what's already real.

    Live-reproduced gap this closes (2026-09): the caller typically
    assembles `products` from a `set` of ids (deduplicating across
    several topics before one fetch) - a `set`'s iteration order has no
    relationship to the real, rendered order, so an ordinal reference
    ("first and last") was silently resolving against a SCRAMBLED order
    instead of the real one, picking the wrong items with complete
    confidence. Ordered ids are read from the SAME real, already-
    persisted source (`LastRendered.product_ids`/`Topic.product_ids`) -
    never re-derived - and every id list here is already display-order by
    construction wherever it was written."""
    products_by_id = {p.product_id: p for p in products}
    if claim.reference_scope == "last_rendered" and context.last_rendered:
        ordered_ids = context.last_rendered.product_ids
    elif claim.reference_scope == "active_topic" and context.active_topic:
        ordered_ids = context.active_topic.product_ids
    elif claim.reference_scope == "cart" and context.cart_snapshot:
        # Natural-language cart-operation fix (2026-09): "remove it"/"clear
        # my cart"/"remove the Trailblazer" name something already IN THE
        # CART, not necessarily anything just searched/shown - `cart_
        # snapshot` is a real, freshly-fetched read of the actual cart
        # (`conversation_context.py::CartSnapshot`'s own docstring), never
        # cached, never invented. The caller is responsible for having
        # fetched these same product ids into `products` (mirroring every
        # other scope's own contract here).
        ordered_ids = [line.product_id for line in context.cart_snapshot.lines]
    elif claim.reference_scope == "paused_topic":
        target = next(
            (
                t for t in context.paused_topics
                if claim.context_target_label
                and _label_matches(claim.context_target_label, _effective_label(t, products_by_id))
            ),
            None,
        )
        # Legacy-bridge fallback - see `resolve_context_action`'s own
        # identical reasoning: a synthetic, unlabeled topic has nothing a
        # label match can ever hit; when it's the only paused topic,
        # that's the same real answer `topic_transition.py`'s own
        # "exactly one paused topic" rule already trusts.
        if target is None and len(context.paused_topics) == 1:
            target = context.paused_topics[0]
        ordered_ids = target.product_ids if target else []
    else:
        if claim.reference_kind in _RELATIVE_REFERENCE_KINDS:
            # Root-cause fix (2026-09): a RELATIVE reference ("the first
            # two", "both", "these", "the cheaper one") means what the
            # shopper is looking at right now - never the caller's whole
            # unscoped pool, which is a union of every topic AND (since
            # cart-scope resolution) every cart line. Live-reproduced:
            # "compare the first two" resolved against that union and
            # paired a waterproof trail shoe with Performance Socks. A
            # model that tagged scope 'named'/'none' (or 'last_rendered'/
            # 'active_topic' with nothing there) for an ordinal/"all"/
            # superlative reference still gets bound to the real view.
            if context.last_rendered and context.last_rendered.product_ids:
                ordered_ids = context.last_rendered.product_ids
            elif context.active_topic and context.active_topic.product_ids:
                ordered_ids = context.active_topic.product_ids
            else:
                # Nothing in view at all (legacy-bridged session, no
                # rendered set yet): the pool is all there is - but a cart
                # line the shopper never searched for is never a
                # candidate for a relative reference.
                cart_only = (
                    {l.product_id for l in context.cart_snapshot.lines}
                    if context.cart_snapshot else set()
                )
                topic_ids = {
                    pid for t in [context.active_topic, *context.paused_topics] if t
                    for pid in t.product_ids
                }
                return [p for p in products if p.product_id not in (cart_only - topic_ids)]
        else:
            # "named"/"none": whatever the caller passed IS the candidate
            # set (already scoped by the caller, e.g. a live search
            # result) - a NAMED reference is matched by its own words
            # against the whole pool, wherever the product lives.
            return list(products)
    return [products_by_id[pid] for pid in ordered_ids if pid in products_by_id]


def _cross_check_scope(
    claim: InterpretationResult,
    context: ConversationContext,
    products_by_id: dict[str, Product],
    candidates: list[Product],
    message: str,
) -> list[Product]:
    """Deterministic semantic cross-check, closing the gap live-reproduced
    across two independent bugs this session (a cart-add and a discovery
    turn both silently resolving against the wrong topic): resolving a
    scope structurally (real ids, non-empty) is not the same as resolving
    it CORRECTLY - the model's own `reference_scope` guess for a bare
    ordinal/superlative ("the first shoe") defaults to 'active_topic'/
    'last_rendered' with no check that the shopper's own words actually
    describe what's active - and the 'named'/'none' catch-all is no safer,
    since it returns every fetched product unscoped when the model's own
    scope tag itself misses. Never overrides an EXPLICIT 'paused_topic'
    claim - that one already went through its own real label match in
    `_candidates_for_scope`, an independent confirmation this cross-check
    would only be duplicating.

    Purely a real-word-overlap comparison (`best_matches_with_score`, the
    same deterministic scorer `compare.py`'s historical-group matching and
    `_try_superlative_from_existing_group` already trust for this exact
    class of decision) between the claimed scope's own real products and
    every OTHER real topic genuinely in view this turn - never invents a
    topic, never fires when the message carries no distinctive word to
    score against at all (a bare "the first one" with nothing narrower to
    say correctly stays wherever the model's scope guess pointed - this
    only catches a scope guess that DISAGREES with a specific noun/name
    the shopper actually used)."""
    # Applies whenever the scope wasn't already an explicit, CONFIRMED
    # paused-topic label match (`_candidates_for_scope`'s own "paused_topic"
    # branch already independently verifies that case via `_label_matches`).
    # Deliberately includes the "named"/"none" catch-all too - live-
    # reproduced gap: that branch returns the caller's raw, UNSCOPED,
    # UNORDERED `products` list (a set-union across every topic in view)
    # whenever the model's own `reference_scope` guess itself lands on
    # "named"/"none" for what is actually a plain ordinal/superlative
    # ("the first shoe") - an ordinal position over that arbitrary order
    # can silently pick from the wrong topic entirely, with no label match
    # to have caught it. Cross-checking here rebuilds `candidates` from a
    # real, correctly-ordered topic id list whenever one scores better.
    if claim.reference_scope in ("paused_topic", "cart") or not candidates:
        return candidates
    own_score, _ = best_matches_with_score(candidates, message)
    claimed_ids = {p.product_id for p in candidates}
    real_topics = list(context.paused_topics)
    if context.active_topic is not None:
        real_topics = [context.active_topic, *real_topics]
    best_ids: list[str] = []
    best_score = own_score
    for topic in real_topics:
        topic_products = [products_by_id[pid] for pid in topic.product_ids if pid in products_by_id]
        if not topic_products or set(topic.product_ids) == claimed_ids:
            continue
        score, _ = best_matches_with_score(topic_products, message)
        if score > best_score:
            best_score = score
            best_ids = topic.product_ids
    if best_ids and best_score > own_score:
        return [products_by_id[pid] for pid in best_ids if pid in products_by_id]
    return candidates


def resolve_reference(
    claim: InterpretationResult, context: ConversationContext, products: list[Product], message: str = "",
) -> ResolvedReference:
    """Resolves a `StructuredReference` claim against real product data.
    `products` must include every product the relevant scope could name -
    the caller (a capability node, post-migration) is responsible for
    having fetched them; this function only ever narrows, never invents."""
    products_by_id = {p.product_id: p for p in products}
    candidates = _candidates_for_scope(claim, context, products)
    candidates = _cross_check_scope(claim, context, products_by_id, candidates, message)
    if not candidates or claim.reference_kind == "none":
        return ResolvedReference(candidates_considered=candidates)

    if claim.reference_kind == "ordinal" and claim.reference_positions:
        # -1 means "the last one" - the same convention
        # `reference_resolution.py::resolve_ordinal_positions` already
        # uses, per `interpretation_tool()`'s own schema description. The
        # model is never asked to count the real items and guess the
        # number itself (live-reproduced gap this closes: "first and
        # last" returned positions=[1] alone, silently dropping "last"
        # entirely, because the schema gave it no way to express it
        # without counting - it isn't told how many items exist at
        # schema-authoring time).
        zero_based = sorted(
            {
                (len(candidates) - 1 if p == -1 else p - 1)
                for p in claim.reference_positions
                if p == -1 or p >= 1
            },
        )
        ids = [candidates[i].product_id for i in zero_based if 0 <= i < len(candidates)]
        return ResolvedReference(product_ids=ids, candidates_considered=candidates)

    if claim.reference_kind == "all":
        return ResolvedReference(
            product_ids=[p.product_id for p in candidates], candidates_considered=candidates,
        )

    if claim.reference_kind in ("superlative", "comparative_pair"):
        # Generalized attribute resolution (2026-09): the shopper's own
        # word for the attribute ("weight", "battery life", anything) is
        # checked against what's REALLY on `Product` (`attributes.py`) -
        # defaults to price only when nothing was named at all (the
        # existing, pre-generalization behavior for a bare "the cheaper
        # one"/"the pricier one" with no explicit attribute word).
        field_name = resolve_attribute_name(claim.reference_attribute) or (
            "price" if not claim.reference_attribute else None
        )
        if field_name is None:
            # A real attribute CONCEPT was named, but this catalog has no
            # real, structured field for it - honest, not a silent
            # fallback to price instead of what was actually asked.
            return ResolvedReference(candidates_considered=candidates, attribute_unavailable=True)
        valued = [p for p in candidates if get_attribute_value(p, field_name) is not None]
        if claim.reference_kind == "comparative_pair" and len(valued) != 2:
            return ResolvedReference(candidates_considered=candidates)
        if not valued:
            return ResolvedReference(candidates_considered=candidates, attribute_unavailable=True)
        pick = (max if claim.reference_direction == "max" else min)(
            valued, key=lambda p: get_attribute_value(p, field_name),
        )
        return ResolvedReference(
            product_ids=[pick.product_id], candidates_considered=candidates,
            resolved_attribute=(field_name, get_attribute_value(pick, field_name)),
        )

    if claim.reference_kind == "named" and claim.reference_names:
        # A "name" none of whose words can occur in ANY product title or
        # category of this store is not a product name - it is a deictic word
        # the model mislabelled ("add it" -> names=["it"]; live, ~30% of runs).
        # A deictic singular means the one product in view; with several in
        # view it stays ambiguous and the shopper is asked. Per-merchant and
        # data-grounded (never a pronoun list), and silent when the store's
        # vocabulary is unknown - a real name ("the Marathon") is never rebound.
        _name_words = [w for n in claim.reference_names for w in str(n).replace("-", " ").split()]
        if (
            _name_words
            and vocabulary.can_be_product_name(context.connection_id, _name_words) is False
            and len(candidates) == 1
        ):
            return ResolvedReference(product_ids=[candidates[0].product_id], candidates_considered=candidates)
        matched_ids: list[str] = []
        for name in claim.reference_names:
            name_words = _distinctive_title_words(name)
            if not name_words:
                continue
            matches = [p for p in candidates if name_words & _distinctive_title_words(p.title)]
            if len(matches) == 1:
                matched_ids.append(matches[0].product_id)
            elif len(matches) > 1:
                return ResolvedReference(ambiguous=True, candidates_considered=matches)
        return ResolvedReference(product_ids=matched_ids, candidates_considered=candidates)

    # "group_selector" style fallback for a reference kind this claim's
    # own structured fields don't cleanly cover (e.g. the model tagged
    # 'ordinal' but positions came back empty) - reuses the existing,
    # already-tested closed-class resolver on the raw name text as a last
    # resort before giving up.
    return ResolvedReference(candidates_considered=candidates)


def resolve_secondary_reference(
    secondary: SecondaryReference, context: ConversationContext, products: list[Product], message: str = "",
) -> ResolvedReference:
    """Resolves the SECOND half of a 'relationship'/'compound_comparison'
    question - reuses `resolve_reference`'s exact scope/attribute/cross-
    check logic (never a second, independently-maintained copy of it) by
    adapting `SecondaryReference` into the same `InterpretationResult`
    shape that function already consumes. `reference_kind` defaults to
    'superlative' with direction 'min' when the secondary side names an
    attribute but no explicit kind - the common real shape ("...and the
    lightest") - never invented when the secondary side is genuinely a
    plain named/ordinal reference instead."""
    kind = secondary.reference_kind
    if kind == "none" and secondary.reference_attribute:
        kind = "superlative"
    adapted = InterpretationResult(
        context_action="continue",
        context_target_label=secondary.context_target_label,
        reference_scope=secondary.reference_scope,
        reference_kind=kind,
        reference_positions=secondary.reference_positions,
        reference_attribute=secondary.reference_attribute,
        reference_direction=secondary.reference_direction or "min",
        reference_names=secondary.reference_names,
    )
    return resolve_reference(adapted, context, products, message)


def verify(
    claim: InterpretationResult, context: ConversationContext, products: list[Product], message: str = "",
) -> VerifiedInterpretation:
    """The single entry point every migrated capability calls: takes the
    model's raw claim and returns the checked, trustworthy result -
    downstream code never sees `InterpretationResult` directly again after
    this."""
    context_action, context_target_topic_id = resolve_context_action(claim, context, products)
    resolved_reference = None
    if claim.reference_kind != "none":
        resolved_reference = resolve_reference(claim, context, products, message)
    resolved_secondary = None
    if claim.secondary_reference is not None:
        resolved_secondary = resolve_secondary_reference(claim.secondary_reference, context, products, message)
    needs_clarification = bool(
        claim.clarify_if_uncertain
        or (resolved_reference is not None and resolved_reference.ambiguous)
        or (
            claim.reference_kind != "none"
            and resolved_reference is not None
            and not resolved_reference.product_ids
            and resolved_reference.candidates_considered
        )
    )
    capability = claim.capability
    question_type = claim.question_type
    secondary_operation = claim.secondary_operation
    # A comparison with exactly ONE resolved side whose message ALSO states a
    # new search goal is a compound turn - "show me waterproof trail shoes ...
    # and compare them with the Trailblazer". "Them" is the search result the
    # SAME message asks for and that does not exist yet, so the compare /
    # relationship routes (which need both sides to already exist) can only
    # answer "I can see one side of that". The model reads this sentence both
    # ways run to run; the discover-then-compare reading is the one the engine
    # can carry out (`secondary_operation` runs against THIS turn's own
    # results). Structural, not lexical: it keys on what was resolved and
    # whether a goal was stated, never on the words of the message.
    # The new search is stated as a `goal`, or - as live claims often do - only
    # as new requirements ("waterproof"). A budget alone is not a search.
    _ss = claim.shopping_state or {}
    states_new_goal = bool(
        _ss.get("goal") or _ss.get("constraints_add") or _ss.get("must_have_add") or _ss.get("nice_to_have_add")
    )
    # A secondary side only blocks the rewrite when the shopper NAMED a
    # DIFFERENT product for it. A positional/descriptive "them" resolved
    # against the OLD view is exactly the reference this turn's new search
    # replaces, and a "second side" that resolves to the SAME product as the
    # first (live: the model repeats "the Trailblazer" on both sides) is one
    # side, not two.
    secondary_named_and_resolved = (
        resolved_secondary is not None
        and bool(resolved_secondary.product_ids)
        and claim.secondary_reference is not None
        and claim.secondary_reference.reference_kind == "named"
        and set(resolved_secondary.product_ids) != set(resolved_reference.product_ids if resolved_reference else [])
    )
    one_side_resolved = (
        resolved_reference is not None
        and len(resolved_reference.product_ids) == 1
        and not secondary_named_and_resolved
    )
    compare_intended = (
        capability == "compare"
        or question_type in ("relationship", "compound_comparison")
        or (capability == "mission" and secondary_operation is not None and secondary_operation.capability == "compare")
    )
    if (
        states_new_goal
        and one_side_resolved
        and compare_intended
        and (secondary_operation is None or secondary_operation.capability == "compare")
    ):
        capability = "discover"
        question_type = "retrieve"
        if secondary_operation is None:
            by_id = {p.product_id: p for p in products}
            names = [by_id[i].title for i in resolved_reference.product_ids if i in by_id]
            secondary_operation = SecondaryOperation(capability="compare", reference_names=names)
    return VerifiedInterpretation(
        capability=capability,
        context_action=context_action,
        context_target_topic_id=context_target_topic_id,
        new_topic_label=claim.new_topic_label,
        resolved_reference=resolved_reference,
        cart_operation=claim.cart_operation,
        conversational_repair=claim.conversational_repair,
        needs_clarification=needs_clarification,
        durable_fact_candidates=list(claim.durable_fact_candidates),
        question_type=question_type,
        resolved_secondary=resolved_secondary,
        secondary_operation=secondary_operation,
        catalog_categories=list(claim.catalog_categories),
    )
