"""The chat endpoint.

Free-form shopper messages, through the same pipeline everything else uses:
reasoning proposes, the Decision Engine selects, the Risk Gate classifies, and
execution runs whatever cleared.

Two things distinguish chat from the friction path.

It reads shared memory. Before reasoning, it loads the conversation so far and any
friction the storefront already recorded against this session. A shopper whose card
was declined can open the chat and say "what now?" without explaining anything,
because the engine already knows.

Safe actions run immediately. A conversation that says "let me check" and then goes
silent for fifteen minutes waiting on an approval is not a conversation. When the
gate clears an action automatically, it executes within the turn and the result goes
into the reply. When it does not clear, the shopper is told honestly that a person is
looking - and the case is already in the queue when they are.
"""

from __future__ import annotations
from engine.decision import operation_for
from shared.models import (
    ActionType,
    CommerceError,
    DeclineReason,
    ErrorCode,
    FrictionType,
    ProposedAction,
)
import logging

from fastapi import Depends, APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from engine import db
from engine import session as session_store
from engine.execution import events as shopper_events
from engine.execution import mission_ranking
from engine.execution import return_risk
from engine.reasoning import cart_intent, explicit_extraction
from engine.notify import confirm_order
from shared.models import CommerceError

from .auth import (
    Visitor,
    any_key,
    belongs_to,
    require_checkout_identity,
    require_owner,
    visitor,
)
from .why import explain
from .deps import MERCHANT_NAMES, engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

#: What to actually tell a shopper about *why* a card failed, keyed by the
#: normalized reason every adapter already maps a platform's own refusal code
#: to (`shared.models.commerce.DeclineReason`). A shopper who hears "your card
#: was declined" and nothing else has no way to tell "try a different card"
#: apart from "wait and try the same one again" - the two situations need
#: different advice, and the adapter already knows which one this is.
_DECLINE_SENTENCE: dict[DeclineReason, str] = {
    DeclineReason.INSUFFICIENT_FUNDS: "That card has insufficient funds",
    DeclineReason.CARD_EXPIRED: "That card has expired",
    DeclineReason.INCORRECT_DETAILS: "Those card details don't look right",
    DeclineReason.LIMIT_EXCEEDED: "That card is over its limit",
    DeclineReason.ISSUER_DECLINED: "Your bank declined that card",
    DeclineReason.SUSPECTED_FRAUD: "That card was flagged and declined",
}


def _decline_fact(reason: DeclineReason | None) -> str:
    """The one honest sentence a shopper needs immediately: what happened, and
    what to do about it. Never a guess - `UNKNOWN`/`None` (a gateway that
    genuinely does not say why) gets the same generic sentence this always
    used to say, rather than inventing a specific reason with nothing behind
    it."""
    if reason and reason in _DECLINE_SENTENCE:
        return f"{_DECLINE_SENTENCE[reason]}, so nothing has been charged. Try a different card."
    return "That card was declined, so nothing has been charged."


def _find_second_item(
    message: str,
    catalog_sample: list | None,
    handled_product_id: str | None,
):
    """Find a second real product the shopper named this turn that this
    turn's single action did not handle - see `_second_item_note`, which
    turns this into the queued-suggestion reply. Returns the matched
    product (from `catalog_sample`) or None.

    The engine only ever executes one action per turn - the Decision Engine
    picks exactly one candidate, by design (see `engine/decision/engine.py`'s
    own docstring: "answers exactly one question"). A shopper who names two
    products to add in one message ("the Trailblazer size 8 and the Marathon
    Pro size 8") gets only the first one handled.

    Genuinely adding both in the SAME turn is not attempted - that would mean
    either loosening variant matching (the double-add bug a looser rule
    already caused once, Completed.md #15) or guessing which variant the
    second item wants without a tap, which the tap-trust invariant forbids.
    Instead the second, real, named-but-unhandled product is queued as a
    `CartProposal` (the same tap-accept/revalidate-before-execute mechanism
    already used for cart_intelligence's and PROPOSE_CART_CHANGE's
    suggestions) so the shopper can pick it up with one tap in the
    Suggestions panel - including its own size prompt if it needs one -
    rather than being told to type the request again from scratch.

    Detecting the second product is done here, directly against the
    shopper's own words and the real catalog - NOT by checking whether the
    model happened to propose a second ADD_TO_CART. Measured directly
    against this codebase's actual model (gpt-4o-mini): asked to propose one
    ADD_TO_CART per named product, in 0 of 8 identical trials did it
    propose more than one, even with an explicit instruction to. Relying on
    that to detect a dropped second item would mean this note almost never
    fires - silently, the same failure this function exists to stop. Instead
    every catalog product's title is checked against the shopper's raw
    message directly: if a product they clearly named is not the one that
    just ran, they asked for something this turn did not handle, regardless
    of what the model's proposal happened to contain.
    """
    if not catalog_sample or not message:
        return None
    said = message.lower()
    for product in catalog_sample:
        if product.product_id == handled_product_id:
            continue
        title_words = [w for w in product.title.lower().split() if len(w) > 3]
        if not title_words:
            continue
        # A shopper names a product colloquially - "the Marathon Pro", not
        # "the Marathon Pro Racing Shoe" - so the bar is the title's own
        # first distinctive word (almost always the actual product name on
        # this catalog: Trailblazer, Marathon, Everyday), not most of the
        # full title. A short, generic first word ("GPS Watch Series 4")
        # falls back to needing two words in common instead, so "shoe" or
        # "watch" alone still cannot false-positive on half the catalog.
        first = title_words[0]
        # >5, not >4 - "Watch" (5) is a generic-enough word to show up in an
        # unrelated sentence ("what a nice watch"); "Trailblazer"/"Marathon"/
        # "Everyday" (8-11) are not.
        matched = first in said if len(first) > 5 else (
            sum(1 for w in title_words if w in said) >= 2
        )
        if matched:
            return product
    return None


def _honest_synthesis(text: str | None, shopping_state: dict | None, fallback: str) -> str:
    """The central hardening-pass fix, in one place - and, after a
    `value-auditor` pass caught a real gap in this function's first version,
    now a structural gate rather than a phrase blocklist.

    The first version of this function only discarded a synthesized reply
    when it matched an enumerated regex of "priority claim" phrasings
    ("since you prioritized X", "your priority", ...). `value-auditor`
    directly falsified that as a general fix: ordinary rephrasings the
    regex never anticipated ("you mentioned lightness matters a lot, so the
    Trailblazer fits perfectly", "because comfort is what you need most,
    I picked this for you") passed through untouched, still making an
    ungrounded personalization claim. A blocklist of an LLM's own free-text
    phrasing cannot be complete - the space of ways to say "I chose this
    for your stated reason" is not enumerable.

    So this function no longer tries to detect the claim in the text at
    all. It asks a simpler, structurally answerable question instead: did
    `shopping_state` actually capture anything (`priorities`/`must_have`/
    `constraints`) that could make ANY personalization claim true this
    turn? If not, the model's synthesized text is not trusted AT ALL,
    whatever it says - not because every ungrounded synthesis is
    necessarily false, but because there is no way to verify from the text
    alone which parts are grounded and which are the model narrating from
    the raw message it was never supposed to reason from directly. The
    `fallback` (a plain, engine-written, fact-only sentence) is used
    instead. This trades away some legitimate neutral narrative quality on
    ungrounded turns for a guarantee that actually holds: no synthesized
    reply reaches a shopper as a personalization claim unless
    `shopping_state` genuinely has something to justify it.

    A SECOND gap, found by further adversarial testing of this same
    function (not by `value-auditor`, but by the same standard it set):
    "some grounded state exists" is not the same as "the claim is about
    what's actually stored." With `shopping_state.priorities=["lightweight"]`,
    the text "Since you prioritized durability above all else, the Marathon
    Pro is the clear winner" passed the first check untouched - a stated
    priority existed, so the whole reply was trusted, even though the
    specific thing it claimed was prioritized was never said by the
    shopper at all. Closed the same way as the first gap: not by trying to
    parse what the claim specifically asserts (as fragile as the regex
    blocklist this function already replaced once), but by requiring the
    reply to actually MENTION at least one of the real stored terms
    (`mission_ranking.stated_terms`/`any_term_mentioned`) - a floor, not a
    semantic proof, but one that closes this specific, now-demonstrated
    bypass shape without reintroducing a phrase blocklist.
    """
    if not text:
        return fallback
    if not mission_ranking.has_stated_priority(shopping_state):
        logger.info(
            "no captured mission state this turn; using the plain "
            "deterministic reply rather than trusting synthesized text "
            "that cannot be verified as grounded"
        )
        return fallback
    terms = mission_ranking.stated_terms(shopping_state)
    if not mission_ranking.any_term_mentioned(text, terms):
        logger.warning(
            "synthesis referenced no actual stored mission term (%r); "
            "falling back rather than trusting an unrelated claim",
            terms,
        )
        return fallback
    return text


def _unconfirmed_compare_items(message: str | None, comparison: list[dict]) -> list[str]:
    """Which of the (always exactly two) compared products the shopper's own
    message never actually named - a deterministic, grounded check, not a
    model judgement. Hardening-pass fix: asking to compare a real product
    against a nonexistent one used to succeed silently, substituting a
    different real product with no indication anything was swapped - the
    shopper never learns their named item wasn't found. A product whose
    title shares no distinctive word with the raw message is reported here
    so the reply can disclose the substitution honestly instead of
    proceeding as if the shopper had actually named it."""
    if not message:
        return []
    said = message.lower()
    unconfirmed = []
    for item in comparison:
        # The first distinctive word of a title is almost always the actual
        # product/model name on this catalog ("Trailblazer", "Marathon",
        # "Ethiopia") - a trailing generic category word ("Shoe", "Coffee")
        # is shared across many products and checking it alone would miss
        # a real substitution whenever both compared items happen to share
        # that generic word, which is common (two shoes, two coffees).
        title_words = [w for w in str(item.get("title", "")).lower().split() if len(w) > 3]
        if not title_words:
            continue
        identifying = title_words[0]
        if identifying not in said:
            unconfirmed.append(item.get("title", "that item"))
    return unconfirmed


def _diversify_by_category(products: list, limit: int) -> list:
    """Round-robin one product per category, in the order categories first
    appear, rather than truncating a raw catalog-order list.

    A product with no category at all is treated as its own single-item
    "category" - it still gets a fair turn in the rotation rather than being
    dropped, since dropping it would be a different, unrelated bias.
    """
    buckets: dict[str, list] = {}
    for p in products:
        key = p.categories[0] if p.categories else f"__none__{p.product_id}"
        buckets.setdefault(key, []).append(p)

    out: list = []
    while len(out) < limit and any(buckets.values()):
        for key in list(buckets.keys()):
            if not buckets[key]:
                continue
            out.append(buckets[key].pop(0))
            if len(out) >= limit:
                break
    return out


class ChatRequest(BaseModel):
    connection_id: str
    session_id: str
    message: str = Field(min_length=1, max_length=800)
    cart_id: str | None = None
    order_id: str | None = None

    #: Set when the storefront already knows what went wrong - a dead search, a
    #: declined payment. The shopper still gets a conversation; the engine just
    #: starts from a diagnosis rather than inferring one from their words.
    friction: str | None = None
    #: The search term, when the friction was a dead search. Without it the model is
    #: guessing at what they wanted.
    query: str | None = None
    #: Facts the storefront already knows, merged into whatever the model proposes.
    #:
    #: This exists because asking a model to re-derive something we already hold is
    #: adding a failure mode for nothing. When a shopper types a coupon code into a
    #: box, we have the code; the model naming it in prose and omitting it from the
    #: structured field - which is what open models reliably do - then leaves an
    #: approval nobody can action. Supplying it directly cannot fail.
    known: dict[str, str] = Field(default_factory=dict)

    #: Handle this without calling the model.
    #:
    #: Set when the answer does not need judgement and the shopper cannot be
    #: kept waiting. A declined payment is the case: the recovery options come
    #: from the capability table, so the model would only have phrased them -
    #: and a fixed sentence now beats a nicer one in thirty seconds, or never,
    #: while the provider is busy.
    skip_model: bool = False

    #: This message was invented by the engine, not typed by the shopper - the
    #: /pay endpoint recursing into chat() after a decline is the one case.
    #: Recording it as a shopper turn would put words in their mouth that the
    #: transcript and merchant console then show as their own; the friction
    #: itself is already recorded as a Case and read back as fact, not
    #: conversation (see session/store.py), so nothing here needs it repeated.
    synthetic: bool = False
class ChatReply(BaseModel):
    """One assistant turn.

    action_taken is populated only when something actually ran. awaiting_person is
    the honest alternative - the shopper is told a person is looking rather than
    being given a vague acknowledgement.
    """

    reply: str
    session_id: str
    case_id: str | None = None

    used_model: bool = False
    diagnosis: str | None = None

    action_taken: str | None = None
    action_summary: str | None = None
    products: list[dict] = Field(default_factory=list)

    #: Two products, side by side, when the shopper asked to compare them.
    #: Separate from `products` (a list to browse) because the storefront
    #: renders them differently - a comparison is always exactly two things
    #: read against each other, not a set to pick from.
    comparison: list[dict] = Field(default_factory=list)

    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    #: A total and the ways to pay it, when the shopper asked to check out.
    #: Rendered as buttons. Tapping one is what charges - nothing here does.
    payment: dict = Field(default_factory=dict)

    #: The shop's real departments/collections, offered when a shopper asked
    #: what's available with nothing to narrow it - "here's everything" used
    #: to mean six products picked by catalog order, which read as "we mostly
    #: sell shoes" on a shop that sells much more. Rendered as tappable
    #: category buttons, the same reasoning as `choices` above.
    category_choices: list[str] = Field(default_factory=list)

    #: A shopper-safe account of how this was decided, including what was
    #: ruled out. Shown behind a toggle rather than in the reply, because a
    #: shopper who is happy with the answer should not have to read the
    #: reasoning to get to it.
    why: dict | None = None

    #: The model was unreachable because of rate limiting, so this reply is
    #: a fallback rather than an answer. The storefront keeps the shopper's
    #: message when this is set, so pressing send again is one key rather
    #: than retyping a sentence.
    rate_limited: bool = False

    #: How many seconds the provider asked us to wait.
    retry_after_seconds: int | None = None

    awaiting_person: bool = False
    risk_rule: str | None = None
    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    awaiting_person: bool = False
    risk_rule: str | None = None

    #: True when the engine actually changed the cart. The storefront refetches on
    #: this rather than guessing from the reply text - a cart that says one thing in
    #: chat and another in the sidebar is worse than no chat at all.
    cart_changed: bool = False

    #: Surfaced so the storefront can show that memory is being used rather than
    #: leaving the shopper to wonder whether it remembered.
    remembered_turns: int = 0
    remembered_friction: int = 0
    # --- pipeline trace ---
    # Included so the demo panel can render the engine's working from the same
    # response the shopper's reply came from. Two endpoints for one turn would mean
    # two model calls, which on a per-minute token budget is a real cost for a view
    # only we look at.
    proposed: list[str] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)
    selected_action: str | None = None
    selection_reason: str | None = None
    escalated_because_empty: bool = False
    risk_outcome: str | None = None
    risk_rule: str | None = None
    risk_reason: str | None = None
    financial: bool = False
    model_reply: str | None = None
    evidence: list[str] = Field(default_factory=list)
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    #:
    #: Set when the answer does not depend on judgement and the shopper
    #: cannot be kept waiting - a declined payment, where the recovery
    #: options come from the capability table and the model would only
    #: have phrased them.


async def _mine(
    who: Visitor,
    connection_id: str,
    *,
    session_id: str,
    cart_id: str | None = None,
    read_only: bool = False,
) -> None:
    """Refuse unless this conversation and cart belong to the caller.

    Both, because a message carries both and either one being somebody else's is a
    problem. A stranger reading a transcript learns what somebody bought; a
    stranger naming another shopper's cart_id in a pay request spends against a
    basket they did not fill.

    Session ids come from the storefront and look like sess_<uuid4 hex>, generated
    with crypto.randomUUID() - not countable the way cart ids are, and no longer
    guessable the way a Math.random() id was, but "hard to guess" was never the
    same as "checked", and this is the check.

    `read_only` is forwarded to the session ownership check only. A cart_id is
    never present on a route that could pass read_only=True (the transcript poll
    takes no cart_id), so there is nothing to narrow there.
    """
    await require_owner(
        who,
        connection_id,
        db.owners.SESSION,
        session_id,
        code="SESSION_NOT_FOUND",
        message="no such conversation",
        read_only=read_only,
    )

    if cart_id:
        await require_owner(
            who,
            connection_id,
            db.owners.CART,
            cart_id,
            code="CART_NOT_FOUND",
            message="no such cart",
        )


#: What a holdout shopper is told: nothing tailored, nothing that promises help,
#: because none was attempted. One sentence rather than one per friction type -
#: the point is that no reasoning ran, and a reply that read as though it had
#: (even a generic-sounding one written per situation) would misrepresent that.
_HOLDOUT_REPLY = "Sorry, that didn't work. Please try again in a moment."


async def _holdout_reply(req: ChatRequest, friction_type: FrictionType) -> ChatReply:
    """Record the friction and answer as though there were no assistant.

    Still writes a Case - the whole point of a holdout is measuring what
    happens without help, and that requires a row to measure. is_holdout=True
    is what lets the merchant report tell this apart from a genuine AUTO
    clearance: nothing was proposed, nothing ran, and the outcome is recorded
    unresolved unless the shopper's own next action resolves it some other way.
    """
    case_id = await db.record_case(
        connection_id=req.connection_id,
        friction=req.friction,
        query=(req.query or req.message)[:300],
        cart_id=req.cart_id,
        order_id=req.order_id,
        session_id=req.session_id,
        reasoning={},
        decision={},
        risk={
            "outcome": "AUTO",
            "rule": "HOLDOUT_NO_ASSISTANCE",
            "reason": "this session was assigned to the merchant's holdout slice",
            "financial": False,
        },
        is_holdout=True,
    )
    await db.record_outcome(
        connection_id=req.connection_id,
        case_id=case_id,
        resolved=False,
        final_state="OUTCOME",
        friction_type=req.friction,
        required_human=False,
    )
    # The shopper's own turn is already recorded by the caller before this
    # runs (chat() writes it as soon as the message arrives, before friction
    # is even parsed) - only the reply is this function's to write.
    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=_HOLDOUT_REPLY,
        case_id=case_id,
    )
    return ChatReply(
        reply=_HOLDOUT_REPLY,
        session_id=req.session_id,
        case_id=case_id,
        used_model=False,
        risk_rule="HOLDOUT_NO_ASSISTANCE",
    )


@router.post("", response_model=ChatReply)
async def chat(
    req: ChatRequest,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> ChatReply:
    """Handle one shopper message.

    Auth and ownership only. The pipeline itself lives in `_process_turn`,
    shared with the webhook route (`engine/api/webhooks.py`) - a merchant's
    own platform reporting a decline is the same friction the storefront
    reports, run through the identical reasoning/decision/risk/execution
    chain, and duplicating that chain anywhere risk-gate-adjacent is exactly
    the kind of copy this project's invariants exist to prevent. What differs
    for a webhook is not the pipeline, only how the caller is authenticated:
    a shopper's key and cookie here, a platform's signature there.
    """
    belongs_to(key, req.connection_id)
    await _mine(who, req.connection_id, session_id=req.session_id, cart_id=req.cart_id)
    return await _process_turn(
        req,
        owner_keys=await who.keys_for(req.connection_id),
        shopper_id=await who.shopper_id_for(req.connection_id),
    )


async def _process_turn(
    req: ChatRequest,
    *,
    owner_keys: list[str] | None = None,
    shopper_id: str | None = None,
) -> ChatReply:
    """The pipeline: reasoning, decision, risk, execution, reply.

    No auth or ownership check here - callers are responsible for that
    before this runs. `chat()` checks a shopper's key and cookie; the
    webhook route checks the merchant platform's signature instead. Neither
    belongs in the pipeline itself.

    `owner_keys` is the one narrow exception: not an auth check (the caller
    has already run one), but the identity CHECK_ORDER_STATUS needs to decide
    whether *this* order belongs to whoever is asking, so it can safely name
    what they bought rather than staying deliberately thin. `chat()` passes
    the visitor's own keys; the webhook route leaves it `None` - a merchant's
    own platform reporting an event has no shopper identity to check against,
    and defaulting to "not verified" is the safe direction.

    `shopper_id` is the same idea, for durable shopper memory: only a
    signed-in shopper has a stable identity to attach a durable fact to
    (see engine/db/memory_facts.py). `chat()` passes it when one is signed
    in; the webhook route leaves it `None`, same reasoning as `owner_keys`.
    """
    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    # Shared memory, both halves. Read before the new turn is recorded, so the
    # history is what was said *before* this message.
    history, friction = await session_store.context_for(
        req.session_id, req.connection_id
    )

    # The current shopping mission (goal/budget/constraints/...) and, for a
    # signed-in shopper only, durable facts from a previous visit. Both read
    # before reasoning so this turn's proposal can see them; both updated
    # after reasoning returns, from that same call's structured output - see
    # below. Neither is conversation history: they are the structured-memory
    # layers the raw 14-turn window above is deliberately not asked to hold.
    #
    # Best-effort, like the commerce lookups below: a shopping turn should
    # not fail because a memory read hiccuped. Reasoning with less context is
    # better than a turn that breaks outright over it.
    shopping_state = None
    memory_facts: list[dict] = []
    try:
        shopping_state = await db.shopping_state.get(req.session_id, req.connection_id)
        if shopper_id:
            memory_facts = await db.memory_facts.for_shopper(
                req.connection_id, shopper_id
            )
    except Exception:  # noqa: BLE001
        logger.exception("could not read shopping state/memory; continuing without it")

    if not req.synthetic:
        await session_store.add_turn(
            session_id=req.session_id,
            connection_id=req.connection_id,
            speaker="shopper",
            text=req.message,
        )

    # Commerce context. Best-effort: reasoning with less is better than a turn that
    # fails because a lookup did.
    catalog_sample = None
    cart = None
    order = None
    try:
        # Fetched wider than what the model actually sees, then diversified
        # across categories before truncating - a plain first-20-in-catalog-
        # order browse handed the model a sample almost entirely from
        # whichever category happens to be seeded first (Footwear, on both
        # demo catalogs), which biased "what's available" toward that one
        # category even with nothing in the message naming it. Round-robining
        # one product per category before capping means a bare "what do you
        # have" is shown breadth, not just whichever department was seeded
        # first.
        browse = await adapter.search_products("", limit=60)
        catalog_sample = _diversify_by_category(browse.products, limit=20)
        if req.cart_id:
            cart = await adapter.get_cart(req.cart_id)
        if req.order_id:
            order = await adapter.get_order(req.order_id)
    except CommerceError:
        pass
    friction_type = None
    if req.friction:
        try:
            friction_type = FrictionType(req.friction)
        except ValueError:
            friction_type = None

    # The holdout. Only friction turns are eligible - a shopper's own freeform
    # question (friction_type is None) is general assistance, not the recovery
    # mechanism the holdout exists to prove caused a sale, so it is answered
    # normally either way. Checked here, before reasoning ever runs, so a
    # holdout session costs no tokens and gets no proposal to withhold - the
    # point is that nothing was attempted, not that something was attempted
    # and then hidden.
    if friction_type is not None:
        policy = engine.policies.get(req.connection_id)
        if policy.holdout_percent > 0:
            in_holdout = await db.holdout_status(
                req.connection_id,
                req.session_id,
                holdout_percent=policy.holdout_percent,
            )
            if in_holdout:
                return await _holdout_reply(req, friction_type)

    reasoning = await engine.reasoning.reason(
        skip_model=req.skip_model,
        friction=friction_type,
        message=req.message,
        query=req.query,
        cart=cart,
        order=order,
        catalog_sample=catalog_sample,
        history=history,
        recorded_friction=friction,
        shopping_state=shopping_state,
        memory_facts=memory_facts,
    )

    # Fold this turn's structured output into stored memory - never a second
    # model call, just parsing what the same call already returned.
    #
    # Hardening-pass fix, folded in here rather than as a separate pass:
    # the model's own extraction misses an obvious, explicit priority/
    # must-have statement on a real fraction of turns - independently
    # reproduced repeatedly, including on phrasing as unambiguous as "I
    # only care about being as lightweight as physically possible, nothing
    # else matters." `explicit_extraction` is a narrow, deterministic
    # supplement (not a second general-purpose NLP system - see its own
    # module docstring) that recognises a small set of unambiguous trigger
    # phrases and UNIONS its findings into the model's own output, never
    # replacing it. A message that matches nothing here contributes
    # nothing - the model's extraction remains the primary source.
    explicit_found = explicit_extraction.extract(req.message)
    explicit_clears = explicit_extraction.extract_clears(req.message)
    combined_state: dict = dict(reasoning.shopping_state or {})
    for field, values in explicit_found.items():
        combined_state[field] = list(
            dict.fromkeys((combined_state.get(field) or []) + values)
        )
    if explicit_clears:
        combined_state["clear_fields"] = list(
            dict.fromkeys((combined_state.get("clear_fields") or []) + explicit_clears)
        )

    # Best-effort: losing a memory update is a worse outcome to fail the
    # whole turn over than to log and move on, the same standard as every
    # other non-critical write in this function (record_case, add_turn).
    try:
        if combined_state:
            await db.shopping_state.merge(
                req.session_id, req.connection_id, **combined_state
            )
        if shopper_id and reasoning.durable_facts:
            for fact in reasoning.durable_facts:
                await db.memory_facts.upsert(
                    req.connection_id,
                    shopper_id,
                    key=fact["key"],
                    value=fact["value"],
                    source_session_id=req.session_id,
                )
    except Exception:  # noqa: BLE001
        logger.exception("could not persist shopping state/memory; continuing")
    else:
        # Re-read rather than patch the in-memory dict by hand: `shopping_state`
        # was fetched at the *start* of this turn, before reasoning ran, so it
        # is last turn's mission - a shopper who states their goal/budget/
        # exclusion for the first time in THIS message would otherwise have it
        # persisted correctly but not actually used by this same turn's
        # mission-aware ranking/synthesis below, which read the stale variable.
        # Only worth the extra read when something was actually written -
        # including by the deterministic supplement above, not just the model.
        if combined_state:
            try:
                shopping_state = await db.shopping_state.get(
                    req.session_id, req.connection_id
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not re-read shopping state; continuing")
    # Merge what we already know into the proposals. Never overwrite: if the model
    # did supply a value, it saw context we did not pass here and should be trusted
    # over our guess.
    # An order question with no number means this visit's most recent order.
    #
    # "What was my last order" answered "I can't find that order" for somebody who
    # had just bought something, because the model correctly left the field blank -
    # they had not given a number - and nothing filled it in. There is no shopper
    # identity to search a history against, but the session knows what it created.
    for candidate in reasoning.actions:
        if str(candidate.action_type) == "CHECK_ORDER_STATUS" and not candidate.parameters.get(
            "order_id"
        ):
            recent = await session_store.latest_order(
                req.session_id, req.connection_id
            )
            if recent:
                candidate.parameters["order_id"] = recent

    if req.known:
        for candidate in reasoning.actions:
            for field, value in req.known.items():
                candidate.parameters.setdefault(field, value)

    # The shopper's own words, carried alongside every proposal.
    #
    # Asked which size, a shopper types "7". The model then writes "I'll add it in
    # size 7" in its prose and leaves the structured field empty, so execution has
    # nothing to act on and asks again - and again, because the same thing happens
    # every turn. Watching someone answer the same question four times is what
    # prompted this.
    #
    # Passing the raw message costs nothing and cannot fail. Execution matches it
    # against the actual variant labels and ignores it when nothing matches, so a
    # shopper saying "yes please" does not accidentally choose a size.
    for candidate in reasoning.actions:
        candidate.parameters.setdefault("said", req.message[:80])

    # Deterministic bridge from plain cart-question phrasing to
    # CART_INTELLIGENCE, closing the exact gap live testing found: the
    # model does not reliably propose this action type for phrasing that
    # obviously calls for it. Added as an extra candidate, unioned
    # alongside whatever the model itself proposed - never a replacement,
    # never forced to win. The Decision Engine's own ranking
    # (engine/decision/ranking.py) still decides what actually runs; this
    # only guarantees the option exists to be ranked when a shopper's own
    # words plainly ask about their actual cart and a cart exists to look
    # at. See engine/reasoning/cart_intent.py for the exact phrase set.
    if cart is not None and cart_intent.wants_cart_intelligence(req.message):
        already_proposed = any(
            str(c.action_type) == "CART_INTELLIGENCE" for c in reasoning.actions
        )
        if not already_proposed:
            reasoning.actions.append(
                ProposedAction(
                    action_type=ActionType.CART_INTELLIGENCE,
                    operation=operation_for(ActionType.CART_INTELLIGENCE),
                    parameters={"said": req.message[:80]},
                    rationale="the shopper's own words plainly asked about their cart",
                    confidence=1.0,
                )
            )

    trace = await engine.decision.decide(
        reasoning.actions, connection_id=req.connection_id, friction=friction_type
    )
    decision = engine.gate.classify(trace.selected)
    # A rules fallback has no reply of its own. Saying "let me look into that" and
    # then showing unrelated products reads as the assistant misunderstanding, when
    # what actually happened is our model provider was busy. Better to say so.
    awaiting = str(decision.outcome) != "AUTO"
    rate_limited = bool(
        reasoning.fallback_reason and "rate limit" in reasoning.fallback_reason
    )

    # One chain, in priority order. Two separate blocks meant the second silently
    # overwrote the first, so a rate-limited turn reported an approval problem
    # instead of the real cause.
    if rate_limited:
        # Say what actually happened. A held action is not the story when we could
        # not reach our model provider at all.
        # Specific where we can be. A shopper told a number will wait; one told
        # "a few seconds" tries immediately, fails again, and concludes the thing
        # is broken.
        wait = getattr(reasoning, "retry_after_seconds", None)
        reply = (
            (
                f"I'm handling a lot of questions right now - give me about "
                f"{wait} seconds and send that again."
                if wait
                else "I'm handling a lot of questions right now - give me a few "
                "seconds and send that again."
            )
            + " You can still tap anything on screen in the meantime, that part "
            "does not wait for me."
        )
    elif trace.escalated_because_empty:
        # Everything proposed was unavailable, so the model's reply promises things
        # this platform cannot do. Replace it rather than caveat it.
        reply = (
            "I'm sorry that didn't work. I can't sort this one out myself, so "
            "I've passed it to someone at the shop who can."
        )
    elif awaiting:
        # The model wrote as if its action had run. It has not.
        reply = (
            "I can help with that, but someone at the shop needs to approve it "
            "first. I've passed it on and they'll pick it up shortly."
        )
    else:
        reply = reasoning.reply or "Let me look into that."
    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=req.connection_id,
            friction=req.friction,
            query=(req.query or req.message)[:300],
            cart_id=req.cart_id,
            order_id=req.order_id,
            session_id=req.session_id,
            reasoning={
                "used_model": reasoning.used_model,
                "model_name": reasoning.model_name,
                "diagnosis": reasoning.diagnosis.cause if reasoning.diagnosis else None,
                "evidence": reasoning.diagnosis.evidence if reasoning.diagnosis else [],
                "fallback_reason": reasoning.fallback_reason,
                "model_reply": reasoning.reply,
                "shopper_reply": reply,
                "prompt_tokens": reasoning.prompt_tokens,
                "completion_tokens": reasoning.completion_tokens,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(c.action_type),
                        "parameters": c.parameters,
                        "rationale": c.rationale,
                        "confidence": c.confidence,
                    }
                    for c in reasoning.actions
                ],
                "rejected": [
                    {
                        "action_type": str(r.action_type),
                        "reason": r.reason,
                        "detail": r.detail,
                    }
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(decision.outcome),
                "rule": decision.policy_rule,
                "reason": decision.reason,
                "financial": decision.properties.financial,
            },
            approval_timeout_minutes=engine.policies.get(
                req.connection_id
            ).approval_timeout_minutes,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record chat case; continuing")

     # Safe actions run inside the turn. A conversation that promises to check and
    # then falls silent for fifteen minutes is not a conversation.
    #
    # The outcome sentence is written here, from the execution result - never by the
    # model. The model writes its reply before anything has run, so any claim it
    # makes about what happened is a guess. Owning this sentence is what stops
    # "I've added that to your cart" appearing when nothing was added.
    action_taken = None
    action_summary = None
    products: list[dict] = []
    comparison: list[dict] = []
    choices: list[dict] = []
    category_choices: list[str] = []
    payment: dict = {}
    choices: list[dict] = []
    cart_changed = False
    # ESCALATE_TO_HUMAN is not financial, not irreversible and touches no
    # customer data, so the gate clears it to run automatically - it needs no
    # approval to hand a shopper to a person. `awaiting` alone therefore missed
    # the one action whose entire meaning is "a person is now involved": a
    # shopper was told one would help and the reply's own awaiting_person flag
    # said otherwise.
    escalated_to_human = False

    # The parameters of whatever was selected, so a failed lookup can
    # name the number the shopper actually typed.
    params_for_reply = dict(trace.selected.action.parameters or {})

    if not awaiting and case_id:
        executed = await engine.execution.execute_case(req.connection_id, case_id)
        action_taken = executed.action_type
        action_summary = executed.summary
        escalated_to_human = executed.action_type == str(ActionType.ESCALATE_TO_HUMAN)
        found = executed.payload.get("products")
        mission_exhausted = False
        # True only when RECOMMEND_PRODUCTS's keyword+price search came back
        # empty and the engine fell back to an unfiltered browse instead of
        # telling the shopper nothing matched - see engine/execution/service.py.
        # The fallback exists so a real cheaper item is not hidden just
        # because its title lacks the shopper's exact word (see that file's
        # own long comment on the Kettle regression this already caused
        # once), but a shopper asking for "waterproof running shoes" must
        # not be told "here's what's available" about a running cap or a
        # canvas sneaker as if it matched - that is the same generic,
        # pre-fetch reply CLAUDE.md's reply-before-data section names as the
        # anti-pattern, just reached through a different action.
        relevance_relaxed = bool(executed.payload.get("relevance_relaxed"))
        relaxed_query = str(executed.payload.get("query") or "")
        # True when real, relevant matches exist for the stated product
        # intent but none clear the shopper's own stated price limit - the
        # product/category intent must never be discarded just because a
        # hard constraint on top of it produces zero results. Distinct from
        # relevance_relaxed: these products DO match what was asked, they
        # are simply over budget, and are shown as that, cheapest first.
        budget_relaxed = bool(executed.payload.get("budget_relaxed"))
        budget_relaxed_max = executed.payload.get("max_price")
        if isinstance(found, list):
            products = found
            # Mission-aware shortlist - deterministic, never a model call.
            # Only for RECOMMEND_PRODUCTS: SUGGEST_ALTERNATIVE is already the
            # "search failed, showing something else" fallback, where a
            # scarce result set narrowing further is the wrong default.
            # Category-choice offers (handled separately below) never reach
            # here since `found` would be None for that branch.
            if executed.action_type == "RECOMMEND_PRODUCTS" and products:
                # budget_relaxed means service.py already made a deliberate,
                # honest choice: these are the real relevant matches for
                # the stated product intent, shown specifically BECAUSE none
                # fit the price limit - re-applying the same budget as a
                # hard filter here would just discard them again and lose
                # the one thing this response exists to say. must_have/
                # exclusions still apply; only the redundant budget re-check
                # is skipped, and only for this one response.
                ranking_state = (
                    {**shopping_state, "budget_amount": None}
                    if budget_relaxed and shopping_state
                    else shopping_state
                )
                shortlisted, fit_reasons = mission_ranking.rank_and_filter(
                    products, ranking_state, memory_facts=memory_facts
                )
                for p in shortlisted:
                    reasons = fit_reasons.get(p.get("product_id"))
                    if reasons:
                        p["fit"] = reasons
                    # Real, grounded pre-purchase friction signals - never a
                    # claim about this product's actual return history (no
                    # such data exists anywhere in this system). Advisory
                    # only: never removes a product from the shortlist,
                    # never blocks anything.
                    risk_findings = return_risk.assess_product(
                        p, shopping_state, catalog_sample=products
                    )
                    if risk_findings:
                        p["return_risk"] = [
                            {
                                "kind": f.kind,
                                "evidence_type": f.evidence_type,
                                "message": f.message,
                                "safer_alternative_id": f.safer_alternative_id,
                            }
                            for f in risk_findings
                        ]
                        try:
                            await shopper_events.record(
                                connection_id=req.connection_id,
                                session_id=req.session_id,
                                event_type=shopper_events.EventType.RETURN_RISK_FLAGGED,
                                shopper_id=shopper_id,
                                product_id=p.get("product_id"),
                                detail={"kinds": [f.kind for f in risk_findings]},
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception("could not record return_risk_flagged; continuing")
                # The catalog had real matches, but every one of them was
                # ruled out by a hard constraint (an exclusion, or over
                # budget) - a different, honest situation from "the catalog
                # had nothing at all," and one that needs its own reply
                # rather than falling through to the pre-fetch model guess.
                mission_exhausted = bool(products) and not shortlisted
                products = shortlisted
                if products:
                    try:
                        await shopper_events.record_many(
                            connection_id=req.connection_id,
                            session_id=req.session_id,
                            event_type=shopper_events.EventType.PRODUCT_IMPRESSION,
                            shopper_id=shopper_id,
                            product_ids=[
                                p["product_id"] for p in products if p.get("product_id")
                            ],
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("could not record product_impression; continuing")
        found_comparison = executed.payload.get("comparison")
        if isinstance(found_comparison, list):
            comparison = found_comparison
            for c in comparison:
                findings = return_risk.assess_product(c, shopping_state, catalog_sample=comparison)
                if findings:
                    c["return_risk"] = [
                        {
                            "kind": f.kind,
                            "evidence_type": f.evidence_type,
                            "message": f.message,
                            "safer_alternative_id": f.safer_alternative_id,
                        }
                        for f in findings
                    ]
            if comparison:
                try:
                    await shopper_events.record(
                        connection_id=req.connection_id,
                        session_id=req.session_id,
                        event_type=shopper_events.EventType.COMPARISON_STARTED,
                        shopper_id=shopper_id,
                        detail={
                            "product_ids": [
                                c["product_id"] for c in comparison if c.get("product_id")
                            ]
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("could not record comparison_started; continuing")
        found_categories = executed.payload.get("category_choices")
        if isinstance(found_categories, list):
            category_choices = found_categories

        if category_choices:
            # Same reasoning as needs_choice below: the model's reply was
            # written before this ran, so it cannot know whether the answer
            # was six products or a category list - replacing it rather than
            # appending keeps the shopper from reading a sentence about
            # products that are not there.
            reply = (
                "We carry a few different things - "
                + ", ".join(category_choices[:-1])
                + (" and " if len(category_choices) > 1 else "")
                + category_choices[-1]
                + ". Which would you like to see?"
            )
        elif executed.needs_choice:
            # Not a failure. The shopper has not said enough yet, and asking is the
            # right answer - a guessed size is a return waiting to happen.
            #
            # Replaces the model's sentence rather than appending to it - the same
            # shape as CLEAR_CART and CHECK_ORDER_STATUS below. The model is told to
            # fill variant_id when a shopper names a size in the same message
            # ("add the size 9 to my cart"), but execution only ever trusts a tap
            # or an exact match to a bare follow-up answer, so that guess is
            # discarded. Appending a question to a reply that already claimed the
            # size was understood read as the system contradicting itself in one
            # message - acknowledging a size and re-asking for it in the same
            # breath. Replacing it means the shopper only ever sees the question.
            choices = executed.choices
            first = executed.choices[0] if executed.choices else {}
            reply = (
                "There's more than one option for "
                + str(first.get("product_title", "that"))
                + ". Which would you like?"
            )
            needs_choice_second_item = _find_second_item(
                req.message, catalog_sample, first.get("product_id")
            )
            if needs_choice_second_item is not None and req.cart_id:
                try:
                    created = await db.cart_proposals.create(
                        connection_id=req.connection_id,
                        session_id=req.session_id,
                        shopper_id=shopper_id,
                        cart_id=req.cart_id,
                        change_type="ADD",
                        product_id=needs_choice_second_item.product_id,
                        reason="you also asked for this in the same message",
                        evidence=[req.message] if req.message else [],
                        snapshot_price=(
                            str(needs_choice_second_item.price.amount)
                            if needs_choice_second_item.price
                            else None
                        ),
                    )
                    await shopper_events.record(
                        connection_id=req.connection_id,
                        session_id=req.session_id,
                        event_type=shopper_events.EventType.PROPOSAL_CREATED,
                        shopper_id=shopper_id,
                        product_id=needs_choice_second_item.product_id,
                        detail={
                            "proposal_id": created.get("proposal_id"),
                            "change_type": "ADD",
                            "source": "multi_item_add",
                        },
                    )
                    reply += (
                        f"\n\nOnce that's picked, I've also queued "
                        f"{needs_choice_second_item.title} as a suggestion - "
                        f"see Suggestions to add it with one tap."
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("could not queue second-item proposal; continuing")
                    reply += (
                        "\n\nI can only take one item at a time right now - ask me "
                        "for the other one next and I'll add that too."
                    )
        elif executed.action_type == "CLEAR_CART":
            # Written by the engine, not the model.
            #
            # A shopper said "remove all", the model said "your cart is now empty",
            # and three items remained - because it described what it intended
            # rather than what happened. The reply is composed from the result here
            # so it can only say what actually occurred.
            if executed.succeeded:
                cart_changed = True
                reply = executed.shopper_summary or "Your cart is empty now."
            else:
                reply = (
                    "I could not change your cart just then. Have another go, or "
                    "use the cart on the right."
                )
        elif executed.action_type == "CHECK_ORDER_STATUS":
            # Replaces the model's sentence rather than appending to it.
            #
            # The model guesses at an answer before the lookup runs, so on a
            # success the shopper got the same fact twice in different words, and
            # on a failure they got the generic search message - "I couldn't turn
            # anything up" - when an order number is not a search term and finding
            # nothing means something specific.
            if executed.succeeded:
                reply = executed.shopper_summary or executed.summary
                payload = executed.payload or {}
                order_id = payload.get("order_id")
                lines = payload.get("lines") or []
                if owner_keys and order_id and lines:
                    owner = await db.owners.owner_of(
                        req.connection_id, db.owners.ORDER, order_id
                    )
                    if owner in owner_keys:
                        items = ", ".join(
                            f"{l['quantity']} x {l['title']}"
                            if l.get("quantity", 1) != 1
                            else l["title"]
                            for l in lines
                        )
                        reply = f"{reply} It had {items}."
            else:
                wanted = str(
                    (executed.payload or {}).get("order_id")
                    or params_for_reply.get("order_id")
                    or ""
                ).strip()
                reply = (
                    (
                        f"I can't find an order numbered {wanted}. "
                        if wanted
                        else "I can't find that order. "
                    )
                    + "Check the number on your confirmation, or someone at the "
                    "shop can look it up for you."
                )
        elif executed.action_type == "CHECK_CART_STATUS":
            # Replaces the model's sentence rather than appending to it, the
            # same shape as CHECK_ORDER_STATUS just above. Live conversation
            # exposed why this needed its own branch rather than trusting
            # whatever the model wrote: "show cart" had no dedicated action
            # at all before this, so it fell through to a browse/recommend
            # proposal and the shopper got an unrelated category list
            # instead of an answer to the question they actually asked.
            reply = (
                executed.shopper_summary or executed.summary
                if executed.succeeded
                else "I couldn't read your cart just then. The cart panel on "
                "the right shows what's in it."
            )
        elif executed.action_type == "COMPARE_PRODUCTS" and executed.succeeded:
            # The reply-before-data fix, applied here: `reasoning.reply` was
            # written before this comparison ever ran, so before this branch
            # existed the shopper simply got that pre-fetch guess verbatim,
            # sitting next to a real, correct comparison table it never
            # referenced (live-reproduced during the Shopper audit this
            # session implements: a stated preference one turn earlier was
            # completely ignored by a generic "I can help you compare"
            # reply). A second, small model call - made only now, with the
            # real fetched prices/stock/descriptions and the shopper's
            # current mission already in hand - writes the sentence instead.
            #
            # Bounded, not a blanket "call the model twice every turn": only
            # for this one action type, and only when the first call actually
            # reached the model - if it fell back to rules, a second call
            # would fail the same way, so there is no point trying and no
            # extra latency spent on an already-throttled turn.
            if reasoning.used_model:
                synth = await engine.reasoning.synthesize(
                    kind="comparison",
                    message=req.message,
                    items=comparison,
                    shopping_state=shopping_state,
                )
                reply = _honest_synthesis(
                    synth.text, shopping_state, "Here's how they compare."
                )
            else:
                reply = "Here's how they compare."
            # Honest-substitution check, deterministic and grounded - not a
            # model judgement. See `_unconfirmed_compare_items`'s own
            # docstring for the bug this closes.
            unconfirmed = _unconfirmed_compare_items(req.message, comparison)
            if unconfirmed:
                names = " and ".join(unconfirmed)
                reply = (
                    f"I couldn't find an exact match for everything you "
                    f"mentioned, so this compares the closest real products "
                    f"I could find, including {names}. "
                ) + reply
            try:
                await db.shopping_state.merge(
                    req.session_id,
                    req.connection_id,
                    active_products=[c["product_id"] for c in comparison],
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record active_products; continuing")
        elif executed.action_type == "CHECK_COMPATIBILITY" and executed.succeeded:
            # Deterministic, not a second model call - unlike COMPARE_PRODUCTS's
            # synthesis above, there is no meaningful sentence-writing task
            # here: the verdict is one of exactly two words
            # (compatible/unknown) and the reason is already a complete,
            # grounded sentence built in product_relations.check_compatibility.
            # Spending a throttled model turn to rephrase "compatible" would
            # buy nothing and risks the same false-certainty failure this
            # whole feature exists to avoid.
            reply = executed.summary or "I couldn't determine compatibility just now."
        elif executed.action_type == "SUGGEST_COMPLEMENTARY" and executed.succeeded:
            # Same reasoning as CHECK_COMPATIBILITY just above: deterministic,
            # grounded only in the merchant's own declared relationships, no
            # model call needed to list them.
            payload = executed.payload or {}
            complements = payload.get("complements") or []
            anchor_title = (payload.get("anchor") or {}).get("title", "that product")
            if complements:
                names = ", ".join(c["title"] for c in complements)
                reply = f"Goes well with {anchor_title}: {names}."
            else:
                reply = (
                    f"The merchant hasn't declared anything as a specific "
                    f"complement for {anchor_title} yet."
                )
        elif executed.action_type == "CART_INTELLIGENCE" and executed.succeeded:
            # Deterministic, same reasoning as CHECK_COMPATIBILITY above -
            # cart_intelligence.analyze_cart already produced complete,
            # grounded sentences; no model call needed to restate them.
            payload = executed.payload or {}
            findings = payload.get("findings") or []
            created_proposal = payload.get("created_proposal")
            if findings:
                reply = " ".join(f["message"] for f in findings)
            else:
                reply = "Nothing to flag - your cart looks fine."
            # A diagnosis alone is a report, not a feature - see CLAUDE.md's
            # "read-only twin" anti-pattern. When the cart has a real,
            # actionable gap (a merchant-declared complement missing), the
            # execution layer already turned it into a real, stored
            # CartProposal - so the reply offers it as something to accept,
            # the same one-more-step-away action the Suggestions panel
            # already surfaces for a model-proposed change.
            if created_proposal:
                try:
                    await shopper_events.record(
                        connection_id=req.connection_id,
                        session_id=req.session_id,
                        event_type=shopper_events.EventType.PROPOSAL_CREATED,
                        shopper_id=shopper_id,
                        product_id=created_proposal.get("product_id"),
                        detail={
                            "proposal_id": created_proposal.get("proposal_id"),
                            "change_type": created_proposal.get("change_type"),
                            "source": "cart_intelligence",
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("could not record proposal_created; continuing")
                reply = (
                    f"{reply} I've put together a suggestion to add it - see "
                    f"Suggestions to accept or skip it."
                )
        elif executed.action_type == "PROPOSE_CART_CHANGE" and executed.succeeded:
            payload = executed.payload or {}
            proposal = payload.get("proposal") or {}
            product_title = payload.get("product_title", "that item")
            change_type = proposal.get("change_type")
            try:
                await shopper_events.record(
                    connection_id=req.connection_id,
                    session_id=req.session_id,
                    event_type=shopper_events.EventType.PROPOSAL_CREATED,
                    shopper_id=shopper_id,
                    product_id=proposal.get("product_id"),
                    detail={"proposal_id": proposal.get("proposal_id"), "change_type": change_type},
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record proposal_created; continuing")
            verb = {
                "ADD": f"add {product_title}",
                "REMOVE": f"remove {product_title}",
                "REPLACE": f"replace it with {product_title}",
                "CHANGE_QUANTITY": f"change the quantity of {product_title}",
            }.get(change_type, f"change {product_title}")
            reply = (
                f"Want me to {verb}? Nothing's changed yet - "
                f"say the word and I'll do it, or let me know if you'd rather not."
            )
        elif executed.action_type == "PREPARE_CHECKOUT" and executed.succeeded:
            payment = executed.payload
            try:
                await shopper_events.record(
                    connection_id=req.connection_id,
                    session_id=req.session_id,
                    event_type=shopper_events.EventType.CHECKOUT_STARTED,
                    shopper_id=shopper_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record checkout_started; continuing")
            # Replaces the model's sentence rather than appending to it.
            #
            # It writes before the action runs, so it cannot know the cart was
            # readable or what the total came to - and it hedges accordingly:
            # "I'll get your checkout ready, you'll see the total shortly." Then
            # the total appears in the same message.
            #
            # Where the engine has a fact and the model has a guess, the fact wins.
            # The block below shows the total and every line, so this is one short
            # sentence and a question.
            reply = "Here's your total. How would you like to pay?"
        elif executed.succeeded:
            if executed.action_type in {
                "ADD_TO_CART",
                "REMOVE_CART_LINE",
                "UPDATE_CART_QUANTITY",
            }:
                cart_changed = True
                count = executed.payload.get("item_count")
                total = executed.payload.get("grand_total")
                removed = executed.payload.get("removed")
                try:
                    if executed.action_type == "ADD_TO_CART":
                        await shopper_events.record(
                            connection_id=req.connection_id,
                            session_id=req.session_id,
                            event_type=shopper_events.EventType.ADD_TO_CART,
                            shopper_id=shopper_id,
                            product_id=trace.selected.action.parameters.get(
                                "chosen_product"
                            )
                            or trace.selected.action.parameters.get("product_id"),
                        )
                    elif executed.action_type == "REMOVE_CART_LINE":
                        await shopper_events.record(
                            connection_id=req.connection_id,
                            session_id=req.session_id,
                            event_type=shopper_events.EventType.REMOVE_FROM_CART,
                            shopper_id=shopper_id,
                            product_id=trace.selected.action.parameters.get(
                                "product_id"
                            ),
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("could not record cart event; continuing")
                done = f"Done - {removed} is out of your cart" if removed else "Done"
                done_sentence = done + (
                    f". Your cart is now {count} item(s), {total}." if count is not None else "."
                )
                second_item = (
                    _find_second_item(
                        req.message,
                        catalog_sample,
                        trace.selected.action.parameters.get("product_id"),
                    )
                    if executed.action_type == "ADD_TO_CART" and req.cart_id
                    else None
                )
                second_note = ""
                if second_item is not None:
                    # A genuine second product was named this turn and not
                    # handled by this turn's single action. Queue it as a
                    # real CartProposal - the same tap-accept, revalidate-
                    # before-execute mechanism cart_intelligence and
                    # PROPOSE_CART_CHANGE already use - so it shows up in the
                    # Suggestions panel for one tap, including its own size
                    # prompt via the ordinary needs_choice path if it needs
                    # one, rather than requiring the shopper to ask again
                    # from scratch. Never auto-added outright: a tap through
                    # accept_proposal is what stays trusted, not a guess made
                    # here about which variant they want.
                    try:
                        created = await db.cart_proposals.create(
                            connection_id=req.connection_id,
                            session_id=req.session_id,
                            shopper_id=shopper_id,
                            cart_id=req.cart_id,
                            change_type="ADD",
                            product_id=second_item.product_id,
                            reason="you also asked for this in the same message",
                            evidence=[req.message] if req.message else [],
                            snapshot_price=(
                                str(second_item.price.amount) if second_item.price else None
                            ),
                        )
                        await shopper_events.record(
                            connection_id=req.connection_id,
                            session_id=req.session_id,
                            event_type=shopper_events.EventType.PROPOSAL_CREATED,
                            shopper_id=shopper_id,
                            product_id=second_item.product_id,
                            detail={
                                "proposal_id": created.get("proposal_id"),
                                "change_type": "ADD",
                                "source": "multi_item_add",
                            },
                        )
                        second_note = (
                            f"\n\nI can only add one item at a time right now, so "
                            f"I've queued {second_item.title} as a suggestion - "
                            f"see Suggestions to add it with one tap."
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("could not queue second-item proposal; continuing")
                        second_note = (
                            "\n\nI can only take one item at a time right now - ask me "
                            "for the other one next and I'll add that too."
                        )
                if second_note:
                    # Drop the model's own pre-written sentence rather than
                    # prefixing it, when a second item was genuinely named.
                    # Live conversation exposed why: asked to add two
                    # products in one message, the model sometimes claims
                    # the second one "isn't in our inventory" - a real
                    # catalog product it never actually searched for,
                    # because its reply is written before this action ran
                    # and it only ever proposes one ADD_TO_CART. Prefixing
                    # a false "couldn't find X" onto the honest queued-
                    # suggestion note left the shopper reading a
                    # contradiction - told a real product does not exist,
                    # then immediately told it was queued. The engine has
                    # already confirmed (by scanning the actual catalog)
                    # that the item is real, so that fact wins outright.
                    reply = done_sentence + second_note
                else:
                    reply = f"{reply}\n\n{done_sentence}"
            elif mission_exhausted:
                # The catalog genuinely had matches; the mission's own hard
                # constraints (an exclusion, a budget) ruled out every one of
                # them. Deterministic and specific, from the real
                # shopping_state fields - not the generic "I couldn't turn
                # anything up" the final catch-all below would otherwise
                # produce, which would read as a broken search rather than
                # an honestly-applied constraint.
                bits = []
                if shopping_state and shopping_state.get("exclusions"):
                    bits.append(
                        "without " + ", ".join(shopping_state["exclusions"])
                    )
                if shopping_state and shopping_state.get("budget_amount"):
                    currency = shopping_state.get("budget_currency") or ""
                    bits.append(
                        f"under {shopping_state['budget_amount']} {currency}".strip()
                    )
                constraint = " and ".join(bits) if bits else "with what you've asked for"
                reply = (
                    f"I found options, but nothing fits {constraint}. "
                    "Want me to loosen one of those and look again?"
                )
            elif budget_relaxed and products:
                # Real matches for what was actually asked exist - they are
                # simply over the price limit stated this turn. Named
                # explicitly rather than silently substituting a cheaper,
                # unrelated product, or silently showing the over-budget
                # ones as if they fit. Deterministic, not model-written -
                # same reasoning as relevance_relaxed below.
                price_note = (
                    f" under {budget_relaxed_max:g}" if budget_relaxed_max else ""
                )
                reply = (
                    f'No exact matches for "{relaxed_query}"{price_note}. '
                    "Here are the closest real matches, cheapest first - "
                    "all currently over that budget:"
                )
            elif relevance_relaxed and products:
                # Deterministic, not model-written - the model's reply is
                # written before this search runs (see above) and has no way
                # to know the keyword match came back empty, so it cannot be
                # trusted to say so on its own. No synthesis pass over these
                # products either: they were not chosen for relevance to
                # what was asked, so there is nothing true a synthesis could
                # say about why they fit.
                reply = (
                    f'Nothing matched "{relaxed_query}" exactly, so here\'s '
                    "what's available instead:"
                )
            elif products:
                # Was "pass" - trusting the model's pre-written reply as-is,
                # on the theory that a label above the cards is just noise.
                # Live conversation exposed the real failure this missed:
                # the model's reply is written before this search/browse/
                # recommend action actually runs, so it can ask a question
                # the result already contradicts - "would you prefer the
                # Trailblazer or the Marathon Pro?" while the cards below
                # show four different shoes, none of them a two-way choice;
                # "would you prefer sneakers or running shoes?" while the
                # cards mix in shorts and a singlet that are neither. A
                # shopper reading that has no idea which of the two named
                # things to answer, because neither is the actual choice in
                # front of them. Replacing it with a plain count is the same
                # "the engine has a fact, the model has a guess" rule this
                # file already applies to CLEAR_CART, CHECK_ORDER_STATUS and
                # PREPARE_CHECKOUT - here the fact is simply how many things
                # are actually shown, and the cards themselves are the
                # choice, not a question needing a typed answer.
                count = len(products)
                reply = f"Here's what's available - {count} option{'s' if count != 1 else ''}:"
                # Same reply-before-data fix as COMPARE_PRODUCTS above,
                # scoped to a genuinely curated discovery result rather than
                # every browse: only RECOMMEND_PRODUCTS (never
                # SUGGEST_ALTERNATIVE's "search failed, showing something
                # else" case), only when the model was actually reachable,
                # and only when the shopper's mission/message gives the
                # synthesis something real to reason over rather than
                # narrating an arbitrary six-item slice of the catalog.
                if (
                    executed.action_type == "RECOMMEND_PRODUCTS"
                    and reasoning.used_model
                    and (shopping_state or req.message)
                ):
                    synth = await engine.reasoning.synthesize(
                        kind="discovery",
                        message=req.message,
                        items=products,
                        shopping_state=shopping_state,
                    )
                    reply = _honest_synthesis(synth.text, shopping_state, reply)
                try:
                    await db.shopping_state.merge(
                        req.session_id,
                        req.connection_id,
                        active_products=[p["product_id"] for p in products],
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("could not record active_products; continuing")
            elif executed.action_type == "CHECK_AVAILABILITY":
                reply = f"{reply}\n\n{executed.summary}"
        else:
            # Say what failed rather than leaving the model's optimistic reply
            # standing alone. A shopper told "here are some options" who then sees no
            # options assumes the interface is broken.
            #
            # Replaces the model's sentence rather than appending to it - the same
            # shape as the needs_choice, CLEAR_CART and CHECK_ORDER_STATUS branches
            # above. The model writes its reply before execution runs, so on a
            # failure it can hold a claim that execution just refuted ("Sure, I can
            # add that...") and appending the failure sentence left the shopper
            # both halves in one message - the same contradiction the needs_choice
            # branch used to produce. The failure is the fact; the model's guess is
            # dropped outright.
            reply = (
                "I couldn't turn anything up for that. Someone at the shop can "
                "help if you'd like."
            )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=case_id,
        choices=choices,
        products=products,
        comparison=comparison,
        payment=payment,
        category_choices=category_choices,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        case_id=case_id,
        used_model=reasoning.used_model,
        diagnosis=reasoning.diagnosis.cause if reasoning.diagnosis else None,
        action_taken=action_taken,
        action_summary=action_summary,
        products=products,
        comparison=comparison,
        choices=choices,
        payment=payment,
        category_choices=category_choices,
        rate_limited=rate_limited,
        retry_after_seconds=getattr(reasoning, "retry_after_seconds", None),
        why=explain(
            diagnosis=(
                reasoning.diagnosis.cause
                if reasoning and reasoning.diagnosis
                else None
            ),
            evidence=(
                reasoning.diagnosis.evidence
                if reasoning and reasoning.diagnosis
                else None
            ),
            # trace.rejected is only ever a platform's incapacity - the decision
            # engine never records a candidate it simply ranked below the winner,
            # because that list is also what an operator reads as "the engine
            # could not do this itself" in the ops and merchant consoles, and a
            # ranking artifact does not belong there.
            #
            # A shopper asking "why this?" wants both halves, though: on a
            # platform that can do several things - Kettle can retry a card,
            # split the payment or offer another method - the honest answer to
            # a successful recovery is not "nothing was ruled out", it is "these
            # were viable and this one was picked". Recomputed here, once, from
            # what was actually proposed, so it never touches the audit trail.
            rejected=[
                {"action_type": str(r.action_type), "reason": r.reason}
                for r in trace.rejected
            ]
            + [
                {"action_type": str(a.action_type), "reason": "RANKED_LOWER"}
                for a in reasoning.actions
                if str(a.action_type) != str(trace.selected.action.action_type)
                and not any(
                    str(r.action_type) == str(a.action_type) for r in trace.rejected
                )
            ],
            selected_action=str(trace.selected.action.action_type),
            risk_rule=decision.policy_rule,
        ),
        awaiting_person=awaiting or escalated_to_human,
        risk_rule=decision.policy_rule,
        cart_changed=cart_changed,
        remembered_turns=len(history),
        remembered_friction=len(friction),
        proposed=[str(c.action_type) for c in reasoning.actions],
        rejected=[
            {"action_type": str(r.action_type), "reason": r.reason, "detail": r.detail}
            for r in trace.rejected
        ],
        selected_action=str(trace.selected.action.action_type),
        selection_reason=trace.selected.selection_reason,
        escalated_because_empty=trace.escalated_because_empty,
        risk_outcome=str(decision.outcome),
        risk_reason=decision.reason,
        financial=decision.properties.financial,
        model_reply=reasoning.reply,
        evidence=reasoning.diagnosis.evidence if reasoning.diagnosis else [],
        model_name=reasoning.model_name,
        prompt_tokens=reasoning.prompt_tokens,
        completion_tokens=reasoning.completion_tokens,
    )


@router.get("/{connection_id}/{session_id}")
async def transcript(
    connection_id: str,
    session_id: str,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> dict:
    """The conversation so far.

    Returns structured turns rather than formatted strings, because the widget polls
    this to pick up messages written by something other than the shopper - an
    operator approving a payment recovery, for instance. Those arrive here rather
    than as a reply to anything the shopper said.

    Theirs only. This route answered for any session id anybody named, which made
    a shopper's whole conversation - what they asked, what they were told, what
    they bought - readable by anyone holding the key that ships in the page.
    """
    belongs_to(key, connection_id)
    if engine.registry.adapter_for(connection_id) is None:
        raise HTTPException(404, f"unknown connection '{connection_id}'")
    # read_only=True: a transcript poll must never be the thing that claims an
    # unstarted conversation. A session id that nobody has posted a first
    # message against yet is refused here (SESSION_NOT_FOUND) exactly as if it
    # did not exist - because, ownership-wise, it does not yet. Only the chat/
    # act POST routes (which write the first turn) may originate a session.
    await _mine(who, connection_id, session_id=session_id, read_only=True)
    return {
        "session_id": session_id,
        "turns": await session_store.turns(session_id, connection_id, limit=60),
        "friction": await session_store.recent_friction(
            session_id, connection_id, limit=5
        ),
    }


class ActRequest(BaseModel):
    """A shopper tapped something.

    Distinct from a message because there is nothing to interpret. They named a
    product and possibly an option, and both arrived as ids.
    """

    connection_id: str
    session_id: str
    product_id: str
    variant_id: str | None = None
    cart_id: str | None = None
    #: What to write in the transcript. The shopper's gesture, in words, so the
    #: conversation still reads like one.
    said: str = "Add that to my cart"


@router.post("/act", response_model=ChatReply)
async def act(
    req: ActRequest,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> ChatReply:
    """Carry out a tap, without asking a model anything.

    The safety path is unchanged. The action still goes through the Decision Engine
    for capability and policy, and through the Risk Gate. Skipping reasoning skips
    only the part that had nothing to contribute.
    """
    belongs_to(key, req.connection_id)
    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")
    await _mine(who, req.connection_id, session_id=req.session_id, cart_id=req.cart_id)

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="shopper",
        text=req.said,
    )

    parameters: dict[str, object] = {"chosen_product": req.product_id}
    if req.variant_id:
        parameters["chosen_variant"] = req.variant_id

    candidate = ProposedAction(
        action_type=ActionType.ADD_TO_CART,
        operation=operation_for(ActionType.ADD_TO_CART),
        parameters=parameters,
        rationale="the shopper tapped this",
        confidence=1.0,
    )

    trace = await engine.decision.decide(
        [candidate], connection_id=req.connection_id, friction=None
    )
    decision = engine.gate.classify(trace.selected)
    awaiting = str(decision.outcome) != "AUTO"

    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=req.connection_id,
            friction=None,
            query=req.said[:300],
            cart_id=req.cart_id,
            order_id=None,
            session_id=req.session_id,
            reasoning={
                # No model was involved, and the console should say so rather than
                # implying a diagnosis nobody made.
                "used_model": False,
                "fallback_reason": "the shopper chose this directly",
                "shopper_reply": None,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(candidate.action_type),
                        "parameters": candidate.parameters,
                        "rationale": candidate.rationale,
                        "confidence": candidate.confidence,
                    }
                ],
                "rejected": [
                    {
                        "action_type": str(r.action_type),
                        "reason": r.reason,
                        "detail": r.detail,
                    }
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(decision.outcome),
                "rule": decision.policy_rule,
                "reason": decision.reason,
                "financial": decision.properties.financial,
            },
            approval_timeout_minutes=engine.policies.get(
                req.connection_id
            ).approval_timeout_minutes,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record a tap; continuing")

    # The product itself, for the wording below. Best-effort: a reply without the
    # product's name is worse than one with it, and not worth failing the turn over.
    product = None
    try:
        product = await adapter.get_product(req.product_id)
    except CommerceError:
        pass

    def describe() -> str:
        """The product, in a sentence a person would say.

        The merchant's description is a fragment rather than a sentence - "Built for
        milk. Chocolate, toffee, a clean finish." Joining it with a dash produced
        "Good choice - the X - built for milk": three clauses and two dashes, which
        reads like a machine. A full stop between the name and the note reads like
        someone talking.
        """
        if product is None:
            return ""
        if not product.description:
            return f"The {product.title}, good choice."
        return f"The {product.title}. {product.description.rstrip('.')}."

    reply = "Someone at the shop needs to approve that first."
    choices: list[dict] = []
    cart_changed = False
    action_summary = None
    # Same reasoning as the chat handler above: ESCALATE_TO_HUMAN clears the
    # gate automatically, so `awaiting` alone cannot tell a shopper a person is
    # now involved.
    escalated_to_human = False

    if not awaiting and case_id:
        executed = await engine.execution.execute_case(req.connection_id, case_id)
        action_summary = executed.summary
        escalated_to_human = executed.action_type == str(ActionType.ESCALATE_TO_HUMAN)

        if executed.needs_choice:
            choices = executed.choices
            what = describe()
            reply = (f"{what} " if what else "") + "Which would you like?"
        elif executed.succeeded:
            cart_changed = True
            try:
                await shopper_events.record(
                    connection_id=req.connection_id,
                    session_id=req.session_id,
                    event_type=shopper_events.EventType.ADD_TO_CART,
                    shopper_id=await who.shopper_id_for(req.connection_id),
                    product_id=req.product_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not record cart event; continuing")
            count = executed.payload.get("item_count")
            total = executed.payload.get("grand_total")
            chosen = next(
                (
                    v.title
                    for v in (product.variants if product else [])
                    if v.variant_id == req.variant_id
                ),
                None,
            )
            title = product.title if product else "That"
            reply = (
                f"{title}"
                + (f" ({chosen})" if chosen else "")
                + " is in your cart"
                + (
                    f" - {count} item{'' if count == 1 else 's'}, {total}."
                    if count is not None
                    else "."
                )
            )
        else:
            # executed.summary is written for an operator reading a queue. It
            # says things like "A cart and a product are both needed to add a
            # line" and, when a platform misbehaves, "no endpoint at /graphql".
            # Neither belongs in front of a shopper, and the second is close to a
            # security problem: it describes our infrastructure to a stranger.
            #
            # So the shopper gets a short, true sentence per case, and the real
            # summary goes to the queue where somebody can act on it.
            code = executed.error_code or ""
            name = product.title if product else "that"
            if code == "INVENTORY_INSUFFICIENT" or "sold out" in executed.summary:
                reply = (
                    f"{name} is sold out at the moment. Would you like me to "
                    "suggest something similar?"
                )
            elif code in {"PRODUCT_UNAVAILABLE", "VARIANT_UNAVAILABLE"}:
                reply = (
                    "I couldn't find that one - it may have just been taken "
                    "down. Have a look at what else is in the shop and I'll help "
                    "from there."
                )
            elif code == "CART_NOT_FOUND" or "cart" in executed.summary.lower():
                reply = (
                    "Something went wrong with your basket. Try refreshing the "
                    "page and I'll pick up where we left off."
                )
            elif code == "CART_ALREADY_PAID":
                reply = (
                    "That order's already placed, so I can't change what's in "
                    "it. Start a new cart if you'd like to buy something else."
                )
            else:
                reply = (
                    "I couldn't add that, sorry. Someone at the shop can help if "
                    "you'd like."
                )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=case_id,
        choices=choices,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        case_id=case_id,
        used_model=False,
        action_taken=str(ActionType.ADD_TO_CART),
        action_summary=action_summary,
        choices=choices,
        awaiting_person=awaiting or escalated_to_human,
        risk_rule=decision.policy_rule,
        cart_changed=cart_changed,
        selected_action=str(trace.selected.action.action_type),
        risk_outcome=str(decision.outcome),
    )


async def _run_trusted_cart_action(
    *,
    connection_id: str,
    session_id: str,
    cart_id: str,
    action_type: ActionType,
    parameters: dict[str, object],
    rationale: str,
):
    """Push one already-approved cart change through the ordinary Decision
    Engine and Risk Gate - the exact same pipeline `act()` above uses for a
    tapped button, and for the same reason: a shopper accepting a specific
    stored proposal is a trusted decision, not a model guess, so it earns
    the same treatment a tap does. Never called with anything the shopper
    has not explicitly approved (see `accept_proposal` below).
    """
    candidate = ProposedAction(
        action_type=action_type,
        operation=operation_for(action_type),
        parameters=parameters,
        rationale=rationale,
        confidence=1.0,
    )
    trace = await engine.decision.decide(
        [candidate], connection_id=connection_id, friction=None
    )
    decision = engine.gate.classify(trace.selected)
    awaiting = str(decision.outcome) != "AUTO"

    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=connection_id,
            friction=None,
            query=rationale[:300],
            cart_id=cart_id,
            order_id=None,
            session_id=session_id,
            reasoning={
                "used_model": False,
                "fallback_reason": "the shopper accepted a stored proposal",
                "shopper_reply": None,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(candidate.action_type),
                        "parameters": candidate.parameters,
                        "rationale": candidate.rationale,
                        "confidence": candidate.confidence,
                    }
                ],
                "rejected": [
                    {"action_type": str(r.action_type), "reason": r.reason, "detail": r.detail}
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(decision.outcome),
                "rule": decision.policy_rule,
                "reason": decision.reason,
                "financial": decision.properties.financial,
            },
            approval_timeout_minutes=engine.policies.get(connection_id).approval_timeout_minutes,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record a proposal-accept case; continuing")

    if awaiting or not case_id:
        return None, case_id, awaiting
    executed = await engine.execution.execute_case(connection_id, case_id)
    return executed, case_id, awaiting


class ProposalCartRequest(BaseModel):
    connection_id: str
    session_id: str
    cart_id: str


@router.get("/proposals")
async def list_proposals(
    connection_id: str,
    session_id: str,
    cart_id: str,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> dict:
    """The proposals still waiting on this shopper's decision for this cart -
    nothing here has touched the cart yet."""
    belongs_to(key, connection_id)
    await _mine(who, connection_id, session_id=session_id, cart_id=cart_id)
    pending = await db.cart_proposals.pending_for_cart(connection_id, cart_id)
    return {"proposals": pending}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    req: ProposalCartRequest,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> dict:
    """The shopper said no. No mutation happens - rejecting is itself the
    entire effect, same as accepting a proposal is not itself the mutation
    (that only happens once re-validated - see accept_proposal)."""
    belongs_to(key, req.connection_id)
    await _mine(who, req.connection_id, session_id=req.session_id, cart_id=req.cart_id)

    proposal = await db.cart_proposals.get(req.connection_id, proposal_id)
    if proposal is None or proposal["cart_id"] != req.cart_id:
        raise HTTPException(404, "no such proposal")
    if proposal["state"] != "PENDING":
        raise HTTPException(409, f"that proposal is already {proposal['state'].lower()}")

    await db.cart_proposals.set_state(req.connection_id, proposal_id, state="REJECTED")
    try:
        await shopper_events.record(
            connection_id=req.connection_id,
            session_id=req.session_id,
            event_type=shopper_events.EventType.PROPOSAL_REJECTED,
            shopper_id=await who.shopper_id_for(req.connection_id),
            product_id=proposal.get("product_id"),
            detail={"proposal_id": proposal_id},
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record proposal_rejected; continuing")
    return {"proposal_id": proposal_id, "state": "REJECTED"}


@router.post("/proposals/{proposal_id}/accept", response_model=ChatReply)
async def accept_proposal(
    proposal_id: str,
    req: ProposalCartRequest,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> ChatReply:
    """The shopper said yes. Only now does anything actually change - and
    only after re-checking the proposal against live data, because a
    suggestion made a few messages ago may no longer be true: the product
    could have sold out, its price could have moved, or the proposal could
    simply be old enough that trusting it blindly would be trusting a
    stale guess rather than a current fact.
    """
    belongs_to(key, req.connection_id)
    await _mine(who, req.connection_id, session_id=req.session_id, cart_id=req.cart_id)

    proposal = await db.cart_proposals.get(req.connection_id, proposal_id)
    if proposal is None or proposal["cart_id"] != req.cart_id:
        raise HTTPException(404, "no such proposal")
    if proposal["state"] != "PENDING":
        raise HTTPException(409, f"that proposal is already {proposal['state'].lower()}")

    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    stale_reason = None
    if db.cart_proposals.is_stale(proposal):
        stale_reason = "it's been a while since I suggested that"
    else:
        try:
            fresh = await adapter.get_product(proposal["product_id"])
        except CommerceError:
            fresh = None
        if fresh is None:
            stale_reason = "I can no longer find that product"
        elif str(fresh.availability) == "OUT_OF_STOCK" and proposal["change_type"] in {"ADD", "REPLACE"}:
            stale_reason = "that item just went out of stock"
        elif (
            proposal.get("snapshot_price")
            and fresh.price
            and str(fresh.price.amount) != proposal["snapshot_price"]
        ):
            stale_reason = "the price has changed since I suggested that"

    if stale_reason:
        await db.cart_proposals.set_state(req.connection_id, proposal_id, state="EXPIRED")
        return ChatReply(
            reply=f"I can't do that one anymore - {stale_reason}. Want me to look again?",
            session_id=req.session_id,
            case_id=None,
            used_model=False,
            action_taken="PROPOSE_CART_CHANGE",
            action_summary="proposal expired at accept time",
            awaiting_person=False,
            cart_changed=False,
            selected_action=None,
            risk_outcome=None,
        )

    change_type = proposal["change_type"]
    parameters_by_step: list[tuple[ActionType, dict]] = []
    if change_type == "ADD":
        params = {"chosen_product": proposal["product_id"]}
        if proposal.get("variant_id"):
            params["chosen_variant"] = proposal["variant_id"]
        parameters_by_step.append((ActionType.ADD_TO_CART, params))
    elif change_type == "REMOVE":
        parameters_by_step.append(
            (ActionType.REMOVE_CART_LINE, {"product_id": proposal["product_id"]})
        )
    elif change_type == "CHANGE_QUANTITY":
        parameters_by_step.append(
            (
                ActionType.UPDATE_CART_QUANTITY,
                {"product_id": proposal["product_id"], "quantity": proposal.get("quantity") or 1},
            )
        )
    elif change_type == "REPLACE":
        parameters_by_step.append(
            (ActionType.REMOVE_CART_LINE, {"product_id": proposal["replaces_product_id"]})
        )
        add_params = {"chosen_product": proposal["product_id"]}
        if proposal.get("variant_id"):
            add_params["chosen_variant"] = proposal["variant_id"]
        parameters_by_step.append((ActionType.ADD_TO_CART, add_params))

    await db.cart_proposals.set_state(req.connection_id, proposal_id, state="ACCEPTED")
    try:
        await shopper_events.record(
            connection_id=req.connection_id,
            session_id=req.session_id,
            event_type=shopper_events.EventType.PROPOSAL_ACCEPTED,
            shopper_id=await who.shopper_id_for(req.connection_id),
            product_id=proposal.get("product_id"),
            detail={"proposal_id": proposal_id},
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record proposal_accepted; continuing")

    last_executed = None
    last_case_id = None
    awaiting_any = False
    step_summaries: list[str] = []
    needs_choice_options: list[dict] = []
    for step_type, step_params in parameters_by_step:
        executed, case_id, awaiting = await _run_trusted_cart_action(
            connection_id=req.connection_id,
            session_id=req.session_id,
            cart_id=req.cart_id,
            action_type=step_type,
            parameters=step_params,
            rationale=f"shopper accepted proposal {proposal_id}",
        )
        last_case_id = case_id or last_case_id
        if awaiting:
            awaiting_any = True
            step_summaries.append("a person needs to approve part of this first")
            break
        last_executed = executed
        if executed is not None and executed.needs_choice:
            # Honest, distinct case from a plain failure: the product has
            # options (size/grind/etc) the proposal never captured, so
            # accepting cannot finish it in this one tap. Rather than
            # dead-ending the shopper ("add it directly" with no way to do
            # that from here), surface the real size/option buttons - the
            # same `choices` mechanism the ordinary needs_choice path
            # already renders as taps - so one more tap finishes it. The
            # proposal itself is retired below (never left PENDING) so it
            # cannot be accepted a second time once the shopper resolves it
            # through the normal tap-to-add path, which would risk a
            # duplicate add.
            needs_choice_options = executed.choices or []
            step_summaries.append(
                "that item needs a size/option choice - pick one below"
            )
            break
        if executed is None or not executed.succeeded:
            step_summaries.append(executed.summary if executed else "that step could not run")
            break
        step_summaries.append(executed.summary)

    all_succeeded = bool(parameters_by_step) and last_executed is not None and last_executed.succeeded and not awaiting_any

    execution_result = {
        "steps": step_summaries,
        "succeeded": all_succeeded,
    }
    await db.cart_proposals.set_state(
        req.connection_id,
        proposal_id,
        # NEEDS_CHOICE is its own terminal state, distinct from FAILED: the
        # product is real and available, it simply has options the proposal
        # never captured. Terminal (not PENDING) so this exact proposal
        # cannot be accepted a second time once the shopper resolves it via
        # the ordinary tap-to-add-a-size path below - re-accepting it after
        # that would risk adding the item twice.
        state=(
            "EXECUTED" if all_succeeded
            else "NEEDS_CHOICE" if needs_choice_options
            else "FAILED"
        ),
        execution_result=execution_result,
    )
    if all_succeeded:
        try:
            await shopper_events.record(
                connection_id=req.connection_id,
                session_id=req.session_id,
                event_type=shopper_events.EventType.PROPOSAL_EXECUTED,
                shopper_id=await who.shopper_id_for(req.connection_id),
                product_id=proposal.get("product_id"),
                detail={"proposal_id": proposal_id},
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record proposal_executed; continuing")

    if awaiting_any:
        reply = "That change needs someone at the shop to approve first - I've passed it on."
    elif all_succeeded:
        reply = last_executed.shopper_summary or last_executed.summary or "Done."
    elif needs_choice_options:
        first = needs_choice_options[0] if needs_choice_options else {}
        reply = (
            "There's more than one option for "
            + str(first.get("product_title", "that"))
            + ". Which would you like?"
        )
    else:
        reply = (
            "I couldn't finish that change. "
            + (step_summaries[-1] if step_summaries else "")
        ).strip()
        if change_type == "REPLACE" and len(step_summaries) == 2:
            # The remove half already ran before the add half failed - the
            # honest, partial-failure case this whole endpoint has to be
            # able to say out loud rather than pretend didn't happen.
            reply = (
                f"I removed the old item, but couldn't add the replacement: "
                f"{step_summaries[-1]}"
            )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=last_case_id,
        choices=needs_choice_options,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        case_id=last_case_id,
        used_model=False,
        action_taken="PROPOSE_CART_CHANGE",
        action_summary=execution_result["steps"][-1] if execution_result["steps"] else None,
        choices=needs_choice_options,
        awaiting_person=awaiting_any,
        cart_changed=all_succeeded,
        selected_action=change_type,
        risk_outcome=None,
    )


class PayRequest(BaseModel):
    """A shopper chose a card and confirmed.

    No action type corresponds to this, deliberately. The AI cannot propose it, the
    risk gate never sees it, and nothing but a shopper's own request reaches here.
    """

    connection_id: str
    session_id: str
    cart_id: str
    card_last4: str


@router.post("/pay", response_model=ChatReply)
async def pay(
    req: PayRequest,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> ChatReply:
    """Take payment, in the conversation the shopper is already in.

    The one place in the system that charges without an approval, and the reason is
    that the person approving is the shopper - who is here, and who just tapped a
    card. Requiring anybody else's approval for somebody to buy something would be
    an odd reading of a guarantee about not spending other people's money.

    A decline is recorded as friction rather than returned as an error, so the
    existing recovery path fires: the shopper is offered help in the same
    conversation, and on a platform that supports it the money can still be
    recovered.
    """
    belongs_to(key, req.connection_id)

    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    # Checkout requires an account with an email - the confirmation has to reach
    # somebody. A guest is sent to sign in (their basket carries over), and an
    # account without an email is prompted for one.
    _shopper_id, shopper_email = await require_checkout_identity(who, req.connection_id)

    # Before anything is charged. This route takes a cart_id from the caller and
    # spent against it without ever asking whose it was.
    await _mine(who, req.connection_id, session_id=req.session_id, cart_id=req.cart_id)

    # And before anything is charged twice.
    #
    # The platform key is derived from the cart and the card, which recognises the
    # same card tapped twice and nothing else. A different card was a different key
    # and bought the same basket again at the full amount. The card cannot come out
    # of that key - a decline is stored under it too, so a cart-only key would
    # replay the refusal forever and there would be no recovery to offer. So the
    # engine keeps its own answer to "has this basket been bought".
    settled = await db.idempotency.begin_payment(req.connection_id, req.cart_id)

    if settled is not None:
        if settled.get("in_flight"):
            reply = (
                "That is going through now - give it a moment rather than "
                "paying again."
            )
        else:
            reply = (
                f"That basket is already paid for - it is order "
                f"{settled.get('order_id')}. Nothing has been charged again."
            )

        await session_store.add_turn(
            session_id=req.session_id,
            connection_id=req.connection_id,
            speaker="assistant",
            text=reply,
        )
        return ChatReply(
            reply=reply,
            session_id=req.session_id,
            used_model=False,
            cart_changed=False,
            payment={
                "paid": True,
                "order_id": settled.get("order_id"),
                # The storefront reads this to stop offering the cards again,
                # rather than inferring it from the wording of a sentence.
                "already_paid": True,
                "cart_retired": True,
            },
        )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="shopper",
        text=f"Pay with the card ending {req.card_last4}",
    )

    try:
        # Derived from the cart and the card, never generated fresh.
        #
        # A shopper on a slow connection taps pay twice. Both requests arrive. With
        # the same key the platform recognises the second as a retry of the first;
        # with a random key per attempt it sees two purchases, which is exactly the
        # failure the interface makes this parameter mandatory to prevent.
        # Named idem_key, not key. `key` is already the auth record from
        # Depends(any_key), and shadowing it here meant chat() was later handed a
        # string where it expected a key - two different things sharing one name,
        # twelve lines apart.
        idem_key = f"pay-{req.cart_id}-{req.card_last4}"

        result = await adapter.checkout_with_card(
            req.cart_id, card_last4=req.card_last4, idempotency_key=idem_key
        )
    except CommerceError as exc:
        # A platform refusal is not the shopper's fault and not their business.
        logger.exception("checkout failed on %s", req.connection_id)

        # Nothing was attempted, so the basket goes back. Holding the claim would
        # lock somebody out of their own cart over an error that was not theirs.
        await db.idempotency.release_payment(req.connection_id, req.cart_id)

        if exc.code == ErrorCode.CART_INVALID:
            # An empty basket, on both platforms. Not a platform failure and
            # nothing to do with the card - the previous message reassured a
            # shopper their card was fine and told them to try again, which
            # changes nothing when there is nothing to buy.
            reply = "There's nothing in this basket to pay for yet."
        else:
            reply = (
                "Something went wrong taking the payment, and it was not your "
                "card. Nothing has been charged - try again in a moment."
            )
        await session_store.add_turn(
            session_id=req.session_id,
            connection_id=req.connection_id,
            speaker="assistant",
            text=reply,
        )
        return ChatReply(
            reply=reply,
            session_id=req.session_id,
            used_model=False,
            cart_changed=False,
        )

    order = result.order
    order_id = order.order_id if order else None

    # A success locks the basket for good; a decline leaves it buyable, because the
    # shopper is about to be offered another way to pay and refusing them then
    # would turn a recoverable sale into a lost one.
    await db.idempotency.payment_settled(
        req.connection_id,
        req.cart_id,
        succeeded=result.succeeded,
        order_id=order_id,
        summary=(
            f"paid {order_id}" if result.succeeded else f"declined on {order_id}"
        ),
        amount=(
            str(order.amount_paid.amount)
            if result.succeeded and order and order.amount_paid
            else None
        ),
        currency=(
            order.amount_paid.currency
            if result.succeeded and order and order.amount_paid
            else None
        ),
        lines=(
            [
                {
                    "product_id": line.product_id,
                    "product_name": line.title,
                    "quantity": line.quantity,
                    "unit_price": (
                        str(line.unit_price.amount) if line.unit_price else None
                    ),
                    "line_total": (
                        str(line.line_total.amount) if line.line_total else None
                    ),
                    "currency": (
                        line.unit_price.currency if line.unit_price else None
                    ),
                }
                for line in order.lines
            ]
            if result.succeeded and order
            else None
        ),
    )

    # Theirs, paid or declined. The declined one matters more: it is the order a
    # shopper comes back to look at, and the one an operator writes to them about.
    if order_id:
        keys = await who.keys_for(req.connection_id)
        await db.owners.take(
            req.connection_id, db.owners.ORDER, order_id, keys[0]
        )

    if result.succeeded:
        reply = (
            f"Paid. Your order is {order_id} and you will get a confirmation "
            "shortly. Thank you."
        )
        try:
            await shopper_events.record(
                connection_id=req.connection_id,
                session_id=req.session_id,
                event_type=shopper_events.EventType.PURCHASE_COMPLETED,
                shopper_id=_shopper_id,
                detail={"order_id": order_id} if order_id else {},
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record purchase_completed; continuing")
        if order_id:
            await confirm_order(
                engine.mailer,
                connection_id=req.connection_id,
                merchant_name=MERCHANT_NAMES.get(req.connection_id, req.connection_id),
                order_id=order_id,
                to_email=shopper_email,
            )
        # See shop.py's checkout route for why: a shopper whose card was
        # declined here and who then retried this same cart - however they
        # got there, whether nobody helped, an operator said no, or nobody
        # got to it in time - just resolved their own problem. The paid
        # amount is passed through so the merchant report counts this as
        # recovered revenue, the same as an operator-approved RETRY_PAYMENT
        # already does - without this, the case is marked "resolved" (moving
        # resolution_rate) but the sale itself never shows up in
        # revenue_recovered, which is exactly the gap that hid Kettle's
        # decline-then-recovered sales from the merchant view.
        paid_money = order.amount_paid if order else None
        await db.resolve_unresolved_payment_cases_for_cart(
            req.connection_id,
            req.cart_id,
            amount=str(paid_money.amount) if paid_money else None,
            currency=paid_money.currency if paid_money else None,
        )
    else:
        # Runs the decline through the same pipeline a decline anywhere else takes,
        # rather than writing a reply here and stopping. The earlier version told
        # the shopper we could help and created no case, so nobody could - and a
        # promise with no mechanism behind it is worse than saying nothing.
        #
        # This also means Kettle offers a recovery and Northfield escalates, from
        # the platform capabilities rather than from a branch in this handler.
        # skip_model, because a shopper whose card was just refused is the last
        # person who should be left waiting.
        #
        # The recovery still runs: the engine works out that Kettle can retry a
        # payment and Northfield cannot from the capability table, not from the
        # model. The model would only have phrased it, and a fixed sentence now
        # beats a nicely worded one in thirty seconds - or never, while throttled.
        recovered = await chat(
            ChatRequest(
                connection_id=req.connection_id,
                session_id=req.session_id,
                message="My card was declined.",
                friction=FrictionType.PAYMENT_DECLINED,
                cart_id=req.cart_id,
                order_id=order_id,
                skip_model=True,
                synthetic=True,
            ),
            key=key,
            who=who,
        )

        # Prefixed rather than replaced. The first sentence is the fact the shopper
        # needs immediately - nothing was taken - and the rest is whatever the
        # engine decided to do about it.
        # The pipeline already wrote its own turn, so this is what the shopper
        # sees on screen now and what the poll will find in the session a moment
        # later. Prefixing here produced two messages for one decline: this one,
        # and the pipeline's, arriving seconds apart in slightly different words.
        #
        # So the fact is prepended for the immediate reply only, and the stored
        # turn is left as the pipeline wrote it.
        # The escalation-because-nothing-was-available reply opens with its own
        # apology ("I'm sorry that didn't work") - fine on its own, but redundant
        # once the sentence above it has already named the specific reason. Strip
        # it here rather than in that reply's own source, since that text is
        # shared with every other friction type this same branch handles, not
        # just a payment decline.
        continuation = recovered.reply or ""
        continuation = continuation.removeprefix("I'm sorry that didn't work. ")
        reply = f"{_decline_fact(result.decline_reason)} {continuation}"

        return ChatReply(
            reply=reply,
            session_id=req.session_id,
            case_id=recovered.case_id,
            used_model=recovered.used_model,
            action_taken=recovered.action_taken,
            action_summary=recovered.action_summary,
            awaiting_person=recovered.awaiting_person,
            risk_rule=recovered.risk_rule,
            selected_action=recovered.selected_action,
            risk_outcome=recovered.risk_outcome,
            # Carried through rather than rebuilt. The pipeline already worked out
            # what it ruled out and why, and this is the turn where a shopper is
            # most entitled to see it: their payment was refused and they are being
            # told a person will handle it.
            why=recovered.why,
            cart_changed=True,
            payment={
                "paid": False,
                "order_id": order_id,
                "cart_retired": False,
            },
        )

    # Only the success path reaches here now. The decline path returned above,
    # after the pipeline had written its own turn - writing a second one would
    # show the shopper the same event twice.
    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=None,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        used_model=False,
        action_taken="CHECKOUT",
        action_summary=(
            f"Shopper paid {order_id}" if result.succeeded else f"Card declined on {order_id}"
        ),
        # True either way. A declined checkout still empties nothing but the
        # storefront needs to re-read, because the order now exists.
        cart_changed=True,
        payment={
            "paid": result.succeeded,
            "order_id": order_id,
            # Tells the storefront to start a fresh cart. A paid cart is finished,
            # and leaving it on screen invites a shopper to pay it twice.
            "cart_retired": result.succeeded,
            "grand_total": str(order.grand_total) if order and order.grand_total else None,
        },
    )
