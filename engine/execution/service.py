"""Execution.

Runs an action that has already been decided, gated, and (where required)
approved. It answers no questions - by the time anything reaches here, what to do
and whether it is allowed have both been settled.

Three things this service is responsible for that nothing else can do:

Re-checking capability at execution time. The Decision Engine filtered against a
cached capability declaration, which may be up to five minutes old. A token can
expire and a scope can be revoked in that window. Checking again here is the
difference between a stale cache causing a wasted decision and a stale cache
causing a failed execution against a live merchant.

Idempotency derived from the case. The key is the case id, not a random value, so a
retried execution of the same case cannot charge a shopper twice. A random key per
attempt would defeat the entire purpose of having one.

Recording what happened. Every execution writes an outcome, including the failures.
A recovery system that only records its successes cannot tell you whether it works.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from engine import db
from engine import session as session_store
from engine.decision import CapabilityRegistry
from engine.execution import cart_intelligence, product_relations, relevance
from shared.models import (
    ActionType,
    CapabilityUnsupported,
    CaseState,
    CommerceError,
    Operation,
)

from shared.models import (
    ActionType,
    CapabilityUnsupported,
    CaseState,
    CommerceError,
    Operation,
    PaymentRecoveryMethod,
    Product,
)

#: Which recovery mechanism each action asks for.
#:
#: RETRY_PAYMENT maps to retrying the same method, which is the one least likely to
#: work - a bank that just declined a card declines it again. It stays available
#: because some declines are transient (an issuer timeout), but the ranking table
#: puts it last for exactly this reason.
_RECOVERY_METHOD = {
    ActionType.RETRY_PAYMENT: PaymentRecoveryMethod.RETRY_SAME_METHOD,
    ActionType.OFFER_ALTERNATE_PAYMENT: PaymentRecoveryMethod.ALTERNATE_METHOD,
    ActionType.SPLIT_PAYMENT: PaymentRecoveryMethod.SPLIT_PAYMENT,
}

logger = logging.getLogger(__name__)

#: A closed vocabulary of "meta-shopping" words - words that talk *about*
#: browsing/buying in general without ever naming a specific product,
#: category, brand or attribute. A message is a generic browse request iff
#: every one of its words is in this set - the moment a real word appears
#: ("shoes", "waterproof", "kayak", "cheap"), the check fails and the
#: message is treated as naming something specific, correctly.
#:
#: This replaced a literal list of eleven exact phrases (see git history),
#: which only matched a shopper saying one of those eleven sentences
#: verbatim - "what else you got", "what other stuff do you sell", "got
#: anything else in stock" all failed it even though they are the same
#: request in different words. A closed word-set is compositional: it
#: covers any ordering/subset of these words, not just the exact phrasings
#: someone happened to test against. Still deliberately not semantic
#: understanding - a genuinely new way of asking ("do you stock things")
#: needs its words added here too - but it no longer requires the whole
#: sentence to be pre-anticipated, only the individual words.
#: Does NOT include "top rated"/"rated"/"best": a bare "what are the top
#: rated products" is its own, already-correct case (rating-sorted, every
#: category) and must keep that signal rather than being flattened to a
#: plain browse.
_GENERIC_BROWSE_WORDS = frozenset(
    {
        "what", "whats", "do", "you", "have", "has", "having", "sell",
        "sells", "selling", "sold", "got", "get", "carry", "carries",
        "offer", "offers", "stock", "stocks", "stocked", "available",
        "availability", "show", "showing", "me", "us", "everything",
        "anything", "else", "other", "others", "product", "products",
        "item", "items", "thing", "things", "stuff", "store", "shop",
        "shops", "is", "in", "at", "the", "a", "an", "your", "can", "i",
        "we", "buy", "buying", "browse", "browsing", "look", "looking",
        "catalog", "catalogue", "range", "selection", "any", "list",
        "listing", "all", "there", "around", "here", "on", "for", "to",
        "of", "and", "or", "please", "just",
    }
)

#: Actions that finish without touching the merchant's backend. Answering a
#: question and handing a case to a colleague are both real outcomes; neither is a
#: commerce operation, and treating them as failures because no API was called
#: would misreport the thing that actually happened.
NO_EXECUTION: frozenset[ActionType] = frozenset(
    {
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.ESCALATE_TO_HUMAN,
        ActionType.NO_ACTION,
        ActionType.NOTIFY_BACK_IN_STOCK,
    }
)

#: Actions that change what is in a cart. Refused once the cart has been paid
#: for - see engine/db/idempotency.py::is_paid.
#:
#: This is the tap/chat path's half of the same guard the REST cart routes carry
#: in engine/api/shop.py. The two share no code: a shopper could not add a line
#: through the sidebar once an order existed, but could still get the assistant
#: to add one to the identical cart, because nothing here had ever asked whether
#: the basket it was about to change had already been bought.
CART_MUTATING: frozenset[ActionType] = frozenset(
    {
        ActionType.ADD_TO_CART,
        ActionType.UPDATE_CART_QUANTITY,
        ActionType.REMOVE_CART_LINE,
        ActionType.CLEAR_CART,
        ActionType.APPLY_PROMOTION,
        # PROPOSE_CART_CHANGE never itself mutates the cart - only a later
        # accept does. It is still refused here, upfront, for a paid cart:
        # dangling a suggestion nobody can ever actually accept ("want me to
        # add X?" against an order that already went through) is a milder
        # version of the same honesty failure a real mutation would be, and
        # accept-time revalidation would refuse it anyway once every
        # underlying step (ADD_TO_CART etc.) hits this same paid-cart check
        # - refusing at proposal time just says so immediately instead of
        # only once the shopper has already tried to accept.
        ActionType.PROPOSE_CART_CHANGE,
    }
)


#: Hard ceiling on how far `_full_catalog_sample` will page, even when a
#: platform reports a much larger real catalog. An explicit, stated bound -
#: not a silent one - per CLAUDE.md's own naming of `_catalog_alerts`'s
#: `limit=100` as the anti-pattern to not repeat: covers a real ~40k-SKU
#: catalog only partially, and that residual gap is real and disclosed
#: (see `_full_catalog_sample`'s own docstring), not claimed solved.
_MAX_CATALOG_SCAN = 2000


async def _full_catalog_sample(adapter, page_size: int = 200) -> list:
    """Page through as much of the real catalog as `_MAX_CATALOG_SCAN` allows,
    using the platform's own reported `total_available` to know how much is
    really out there - rather than a single fixed-size page presented as if
    it were the whole catalog.

    Both demo merchants' backends (`sample_merchant/store.py`,
    `sample_merchant_two/store.py`) report `total_available`/`matchCount` as
    the true match count across their ENTIRE catalog, not the size of the
    page returned - a real, already-available signal this function uses to
    decide how many more pages are actually worth fetching, rather than
    guessing.

    Third round of the same fix: earlier versions used a single
    `limit=100`, then `limit=200`, each presented as "wider" without ever
    checking how much of the real catalog that actually covered - both were
    silently wrong on any catalog bigger than the page itself, which is
    exactly the `_catalog_alerts`-style anti-pattern CLAUDE.md names. This
    version pages until either the whole catalog is covered or
    `_MAX_CATALOG_SCAN` is reached, and is honest that the second case is
    still a real residual gap on a genuinely large catalog (a real
    40,000-SKU client is not fully covered by 2000 items either) - not
    claimed as a complete fix, just a materially larger and now
    self-aware bound instead of an arbitrary small one.
    """
    first = await adapter.search_products("", limit=page_size)
    products = list(first.products)
    total = first.total_available
    if total is None:
        return products
    cap = min(total, _MAX_CATALOG_SCAN)
    offset = len(products)
    while offset < cap:
        try:
            page = await adapter.search_products("", limit=page_size, offset=offset)
        except TypeError:
            # A platform that cannot page (offset is a real extension both
            # demo adapters support, not part of the standard interface -
            # see shared/interfaces/commerce.py) degrades to whatever the
            # first page already returned rather than raising.
            break
        except CommerceError:
            break
        if not page.products:
            break
        products.extend(page.products)
        offset += len(page.products)
    return products


@dataclass
class Executed:
    """What happened when an action ran.

    summary is written for a person reading the queue, not for a log. An operator
    who approved something wants to know what it did, in a sentence.
    """

    succeeded: bool
    action_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    latency_ms: int | None = None
    final_state: str = str(CaseState.OUTCOME)
        #: Set when the action could not proceed because the shopper has not said enough
    #: yet - which size, which grind. Not a failure: asking is the correct outcome,
    #: and guessing a size on their behalf is how a shop ends up with a returns
    #: problem.
    needs_choice: bool = False
    #: What the shopper should be told, if anything. Deliberately separate from
    #: `summary`, which is written for an operator reading a queue. "Recovered
    #: 3422.00 INR on KB-0001" is the right sentence for the person who approved it
    #: and the wrong one for the person whose card failed.
    shopper_summary: str | None = None
    choices: list[dict[str, Any]] = field(default_factory=list)


class ExecutionService:
    """Carries out approved actions. Makes no decisions."""

    def __init__(self, registry: CapabilityRegistry, reasoning=None) -> None:
        self._registry = registry
        #: Optional `ReasoningService` reference, used ONLY for the narrow
        #: semantic relevance judgment (`ReasoningService.judge_relevance`)
        #: that gates the RECOMMEND_PRODUCTS fallback below when the
        #: deterministic keyword/category check already said "no" - never
        #: for proposing or executing anything. `None` (e.g. in tests that
        #: construct `ExecutionService` directly) simply skips the semantic
        #: check and falls back to the deterministic-only behavior that
        #: already existed, so nothing here is a hard dependency.
        self._reasoning = reasoning

    async def execute_case(self, connection_id: str, case_id: str) -> Executed:
        """Run the action a case selected, then record the outcome.

        Loads the case from the database rather than taking the action as an
        argument. That is deliberate: the thing executed must be the thing that was
        approved, and reading it back from storage is the only way to be sure
        nothing was substituted in between.
        """
        case = await db.get_case(connection_id, case_id)
        if case is None:
            return Executed(
                succeeded=False,
                action_type="UNKNOWN",
                summary="That case does not exist on this connection.",
                error_code="CASE_NOT_FOUND",
                final_state=str(CaseState.FAILED),
            )

        try:
            action_type = ActionType(case.selected_action or "")
        except ValueError:
            return Executed(
                succeeded=False,
                action_type=case.selected_action or "UNKNOWN",
                summary="The recorded action is not one the engine recognises.",
                error_code="VALIDATION_ERROR",
                final_state=str(CaseState.FAILED),
            )

        # Idempotency, for actions where a repeat has a cost. The key is derived
        # from the case, so a retry carries the same key as the first attempt and is
        # recognisable as a retry - a fresh random key per attempt would defeat the
        # whole mechanism.
        key = None
        if db.idempotency.guarded(action_type):
            params = self._params_for(case, action_type)
            key = db.idempotency.key_for(case.case_id, str(action_type), params)
            prior = await db.idempotency.claim(
                connection_id=connection_id,
                case_id=case.case_id,
                action_type=str(action_type),
                idempotency_key=key,
            )
            if prior is not None:
                # Already attempted. Return what happened the first time rather than
                # doing it again.
                if prior["state"] == "IN_FLIGHT":
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "An earlier attempt is unresolved - the platform may or "
                            "may not have acted. A person should check before this "
                            "is retried."
                        ),
                        error_code="IDEMPOTENCY_CONFLICT",
                        final_state=str(CaseState.FAILED),
                    )
                return Executed(
                    succeeded=bool(prior.get("succeeded")),
                    action_type=str(action_type),
                    summary=f"Already done: {prior.get('summary')}",
                    payload=prior.get("result") or {},
                    final_state=str(CaseState.OUTCOME),
                )

        result = await self._run(connection_id, case, action_type)

        if key is not None:
            await db.idempotency.complete(
                key,
                succeeded=result.succeeded,
                summary=result.summary,
                result=result.payload,
            )
        # Revenue recovered, where there is any. Only counted when money actually
        # moved - a successful search recovers a sale in spirit and nothing in fact,
        # and reporting it as revenue would inflate every number we quote.
        await db.record_outcome(
            connection_id=connection_id,
            case_id=case_id,
            resolved=result.succeeded,
            final_state=result.final_state,
            friction_type=case.friction_type,
            amount=result.payload.get("recovered_amount"),
            currency=result.payload.get("recovered_currency"),
            required_human=case.risk_outcome != "AUTO",
        )

        # Close the loop. An approval that recovers a payment is useless to the
        # shopper if nobody tells them, and "someone will pick this up" followed by
        # silence is worse than not offering.
        #
        # Only for cases that actually waited on a person. An action that ran
        # automatically already reported itself in the turn that triggered it, and
        # writing it twice would read as the assistant repeating itself.
        # What to tell the shopper, including when it did not work.
        #
        # Only successes had a shopper_summary, so an approved action that then
        # failed left the shopper waiting forever. Every other ending was already
        # covered - expiry tells them, rejection tells them, success tells them -
        # and failure was the one case left silent. That is the worst one to miss,
        # because a person actually looked at it and it still went nowhere.
        #
        # The fallback is deliberately vague about the cause. "The platform reported
        # VOUCHER_DEAD" is for the queue, not for the person who typed the code.
        told = result.shopper_summary
        if not told and not result.succeeded and not result.needs_choice:
            told = (
                "I checked with the shop and that one could not be done, sorry. "
                "If you would still like a hand, ask me and I will pass it on."
            )

        if told and case.session_id and case.risk_outcome != "AUTO":
            try:
                await session_store.add_turn(
                    session_id=case.session_id,
                    connection_id=connection_id,
                    speaker="assistant",
                    text=told,
                    case_id=case_id,
                )
            except Exception:  # noqa: BLE001
                # A failure here loses a message, not the outcome. Worth logging and
                # not worth undoing an executed action over.
                logger.exception("could not deliver the outcome to the shopper")

        return result

    @staticmethod
    def _params_for(case, action_type: ActionType) -> dict:
        """Parameters of the selected proposal, for the idempotency key."""
        for row in case.proposed or []:
            if isinstance(row, dict) and row.get("action_type") == str(action_type):
                return dict(row.get("parameters") or {})
        return {}

    # -- dispatch ----------------------------------------------------------

    async def _run(self, connection_id: str, case, action_type: ActionType) -> Executed:
        if action_type in NO_EXECUTION:
            return self._terminal(action_type, case)

        adapter = self._registry.adapter_for(connection_id)
        if adapter is None:
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary="No adapter is connected for this merchant.",
                error_code="AUTH_FAILED",
                final_state=str(CaseState.FAILED),
            )

        # The capability re-check promised above. The decision was made against a
        # cache; this is the authoritative read.
        operation = self._operation_for(action_type)
        if operation is not None:
            caps = await self._registry.get(connection_id, force=True)
            if caps is None or not caps.supports(operation):
                self._registry.invalidate(connection_id)
                return Executed(
                    succeeded=False,
                    action_type=str(action_type),
                    summary=(
                        "This merchant's platform no longer supports that operation. "
                        "It did when the action was chosen, so the connection may "
                        "have changed."
                    ),
                    error_code="CAPABILITY_UNSUPPORTED",
                    final_state=str(CaseState.UNSUPPORTED),
                )

        # Refused before the platform ever sees it. The order for this cart may
        # already exist, and the platform has no idea we consider the basket
        # closed - only our own ledger knows that.
        if action_type in CART_MUTATING and case.cart_id:
            if await db.idempotency.is_paid(connection_id, case.cart_id):
                return Executed(
                    succeeded=False,
                    action_type=str(action_type),
                    summary="Refused: this cart has already been paid for.",
                    shopper_summary=(
                        "That order's already placed, so I can't change what's "
                        "in it. Start a new cart if you'd like to buy something "
                        "else."
                    ),
                    error_code="CART_ALREADY_PAID",
                    final_state=str(CaseState.FAILED),
                )

        started = time.perf_counter()
        try:
            result = await self._dispatch(adapter, case, action_type)
        except CapabilityUnsupported as exc:
            self._registry.invalidate(connection_id)
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary="The platform refused: it cannot perform that operation.",
                error_code=str(exc.code),
                final_state=str(CaseState.UNSUPPORTED),
            )
        except CommerceError as exc:
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary=f"The platform reported a problem: {exc.message}",
                error_code=str(exc.code),
                latency_ms=int((time.perf_counter() - started) * 1000),
                final_state=str(CaseState.FAILED),
            )

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    async def _shop_carries_semantic(self, need: str, wide_products: list) -> bool | None:
        """Second-opinion semantic check, used ONLY when
        `relevance.shop_carries`'s deterministic keyword/category overlap
        already said "not carried" - never to override a deterministic
        "yes" (that would risk re-opening exactly the false-substitution
        shape this whole mechanism exists to prevent), only to catch the
        two shapes literal overlap structurally cannot: a taste/use-case
        description sharing no literal words with any product ("something
        smooth and not too bitter" at a coffee shop), and a clearly
        unrelated category the deterministic check might still miss on an
        undercategorized platform.

        `need` should be the shopper's own original wording when available
        (not a narrowed search proxy the model may have substituted, e.g.
        "kayak" rewritten to "water") - passed as such by both call sites
        below, since judging relevance against the model's own already-
        weak paraphrase would just repeat the paraphrase's own mistake.

        Returns `None` when no `ReasoningService` was wired in (e.g. a test
        constructing `ExecutionService` directly) or the model call itself
        is unavailable/throttled - callers must treat `None` the same as
        "could not confirm," i.e. keep the deterministic "not carried"
        verdict rather than treat an unconfirmed check as permission.
        """
        if self._reasoning is None or not getattr(self._reasoning, "enabled", False):
            return None
        categories = sorted({c.lower() for p in wide_products for c in relevance._categories_of(p)})
        sample_titles = [
            f"{p.title} - {p.description}"[:160] if p.description else p.title
            for p in wide_products[:40]
        ]
        try:
            # A hard timeout, not just relying on `LLMClient`'s own transport
            # timeout - found live, the hard way: an unbounded call here can
            # stall a turn (and, worse, a batch test run hitting this path
            # repeatedly) far longer than any shopper-facing wait should
            # ever be. 12s matches the rough per-turn latency budget the
            # rest of this codebase already tolerates for a single model
            # call; a slower response degrades exactly like any other
            # `LLMUnavailable` - to the deterministic verdict, never a hang.
            return await asyncio.wait_for(
                self._reasoning.judge_relevance(
                    need=need, categories=categories, sample_titles=sample_titles
                ),
                timeout=12.0,
            )
        except TimeoutError:
            logger.info("semantic relevance judgment timed out; degrading to deterministic verdict")
            return None
        except Exception:
            logger.exception("semantic relevance judgment failed; degrading to deterministic verdict")
            return None

    async def _dispatch(self, adapter, case, action_type: ActionType) -> Executed:
        """Map an action onto a commerce call.

        Note that the search-based actions execute by fetching the alternatives
        rather than by showing them. Execution's job is to produce the result; what
        the shopper sees is the storefront's job.
        """
               # Parameters from the *selected* action, not merged across all proposals.
        # Merging them would let a rejected proposal's search term leak into the
        # chosen one, and two proposals can legitimately carry different terms.
        params: dict[str, Any] = {}
        for row in case.proposed or []:
            if isinstance(row, dict) and row.get("action_type") == str(action_type):
                params = dict(row.get("parameters") or {})
                break

        match action_type:
            case ActionType.SUGGEST_ALTERNATIVE | ActionType.RECOMMEND_PRODUCTS:
                # The model's suggested term, or the shopper's original if it did
                # not supply one. Falling back to the original is not much use, but
                # it is honest - better than inventing a term.
                # The two actions fall back differently, and conflating them was a
                # bug worth spelling out.
                #
                # SUGGEST_ALTERNATIVE answers a search that found nothing, so the
                # original term is a sensible last resort - it at least addresses
                # what they asked for.
                #
                # RECOMMEND_PRODUCTS answers "show me something". Falling back to the
                # shopper's raw message meant a greeting was searched as a keyword:
                # "Hi" matched no product, and the assistant answered a hello with
                # "I couldn't turn anything up for that". An empty query browses the
                # catalog, which is what recommending actually means.
                if action_type is ActionType.RECOMMEND_PRODUCTS:
                    query = str(params.get("query") or "")
                else:
                    query = str(params.get("query") or case.query or "")

                # A price limit is filtered here, not left to the search term -
                # this shop's search matches titles literally and has no notion
                # of price at all, so "under 2000" can only ever be honoured by
                # filtering what comes back, never by searching for it.
                # math.isfinite defends this comparison too, not just the parser
                # upstream (reasoning/service.py) that is meant to keep NaN/inf
                # out - a value reaching this far unfinite must not raise
                # decimal.InvalidOperation and break the turn, so it is treated
                # as "no limit given" rather than trusted.
                raw_max = params.get("max_price")
                raw_min = params.get("min_price")
                max_price = raw_max if isinstance(raw_max, (int, float)) and math.isfinite(raw_max) else None
                min_price = raw_min if isinstance(raw_min, (int, float)) and math.isfinite(raw_min) else None
                price_bounded = max_price is not None or min_price is not None
                top_rated = params.get("top_rated") is True
                category = str(params.get("category") or "").strip()

                # A deterministic backstop, not just a prompt rule. The model
                # is told to leave category/query blank for a bare "what's
                # available", but recent conversation about one category (a
                # "top rated shoes" question, say, still in the history a few
                # lines up) reliably pulled it back in anyway on live testing -
                # the same "answer what's in front of you, not the history"
                # instruction this codebase already relies on for other
                # friction, just not reliable enough on its own here. A small,
                # literal set of genuinely generic browse phrases - checked
                # against this turn's own words, nothing inferred - always
                # resets category/query/top_rated, because a phrase this
                # generic cannot itself be naming a category to keep.
                said = str(params.get("said") or "").strip().lower()
                # The shopper's own original wording, unlowered, for the
                # semantic relevance judge below - deliberately NOT `query`,
                # which may already be the model's own narrowed proxy term
                # ("kayak" rewritten to "water"). Judging relevance against
                # the paraphrase would just repeat the paraphrase's mistake.
                raw_said = str(params.get("said") or "").strip() or query
                _said_words = [w.strip(".,!?'\"") for w in said.split()]
                _said_words = [w for w in _said_words if w]
                is_generic_browse_phrase = (
                    action_type is ActionType.RECOMMEND_PRODUCTS
                    and bool(_said_words)
                    and all(w in _GENERIC_BROWSE_WORDS for w in _said_words)
                )
                if is_generic_browse_phrase:
                    category = ""
                    query = ""
                    top_rated = False

                # A follow-up turn inside an already-active mission ("comfort
                # is more important to me") correctly states no new product,
                # category or price itself - repeating the whole original
                # request every turn would be absurd, and the model is told
                # not to. But "nothing new this turn" is not "no active
                # mission": ShoppingState already holds exactly what was
                # previously stated (goal, budget_amount), and discarding it
                # here is what turned "comfort matters more" into a bare
                # browse offering unrelated categories, as if the shopper
                # had never mentioned running shoes at all. Seeding
                # retrieval from the persisted mission - never from
                # anything invented, and never overriding what this turn
                # DID state - keeps the actual product intent alive across
                # turns; mission_ranking below still re-ranks by whatever
                # priority was just restated. Skipped for an explicit
                # generic-browse phrase above: a shopper asking "what do
                # you have" mid-mission is deliberately asking to browse
                # outside it, not restating the same request.
                # Not gated on category/price/top_rated also being blank
                # this turn: a message can legitimately restate one part of
                # the mission ("under 2000") while leaving the product
                # itself implicit ("lightweight running shoes" already said
                # last turn) - each piece is backfilled independently, and
                # only for what this turn genuinely left blank, so a value
                # this turn DID state is never overridden by an older one.
                # Also gated on the message actually saying something - a
                # bare "hi" partway through a session must still read as a
                # fresh greeting, not as "continue the last mission",
                # however active that mission still is; the same
                # real-word floor the rescue search below already applies.
                said_has_real_word = any(len(w) > 2 for w in said.split())
                if (
                    action_type is ActionType.RECOMMEND_PRODUCTS
                    and not is_generic_browse_phrase
                    and said_has_real_word
                    and (not query or not price_bounded)
                    and case.session_id
                ):
                    mission_state = await db.shopping_state.get(
                        case.session_id, case.connection_id
                    )
                    if mission_state:
                        if not query and mission_state.get("goal"):
                            query = str(mission_state["goal"])
                        if not price_bounded and mission_state.get("budget_amount") is not None:
                            try:
                                max_price = float(mission_state["budget_amount"])
                                price_bounded = True
                            except (TypeError, ValueError):
                                pass

                # A general "what do you have" - no keyword, no category, no
                # price or rating narrowing - used to mean six products picked
                # by catalog order, which read as "we mostly sell shoes" on a
                # shop that also sells apparel, accessories, nutrition and
                # tech. Offering the shop's own real categories instead of a
                # slice of them is the same "let the shopper narrow it,
                # don't guess for them" reasoning behind needs_choice for a
                # variant - so it is returned the same shape, as choices to
                # pick from rather than a paragraph of products.
                if (
                    action_type is ActionType.RECOMMEND_PRODUCTS
                    and not query
                    and not category
                    and not price_bounded
                    and not top_rated
                ):
                    # A deterministic backstop against model intent-extraction
                    # variance, not a second copy of the model's job. `said`
                    # only reaches here still non-empty and not one of the
                    # genuinely generic browse phrases above - so if the
                    # model left search_query blank anyway, that is the
                    # model's parameter-filling failing on a message that
                    # plainly names something ("running shoes for trail
                    # running"), not a shopper who actually wants the
                    # category list. Running the shopper's own words through
                    # the same real, catalog-grounded word-match search
                    # RECOMMEND_PRODUCTS already uses for an explicit query -
                    # never a hardcoded phrase - lets a real product match
                    # win before falling back to categories. Only offers
                    # categories when even this finds nothing, the same
                    # honest last resort as before.
                    # A greeting ("hi") or other short filler also reaches
                    # here with `said` non-empty but carrying nothing a
                    # search could ever match on - the same "at least one
                    # real word" floor the search itself applies, checked
                    # here first so "hi" is not searched as if it were a
                    # product term and does not silently return an
                    # unrelated slice of the catalog.
                    has_real_word = any(len(w) > 2 for w in said.split())
                    if said and has_real_word:
                        rescued = await adapter.search_products(said, limit=30)
                        rescued_relevant = relevance.best_matches(rescued.products, said)
                        if rescued_relevant:
                            titles = [p.title for p in rescued_relevant[:6]]
                            return Executed(
                                succeeded=True,
                                action_type=str(action_type),
                                summary=(
                                    f'Searched "{said}" and found {len(titles)}: '
                                    + ", ".join(titles[:3])
                                    + ("…" if len(titles) > 3 else "")
                                ),
                                payload={
                                    "query": said,
                                    "products": [
                                        {
                                            "product_id": p.product_id,
                                            "title": p.title,
                                            "description": p.description,
                                            "price": str(p.price) if p.price else None,
                                            "availability": str(p.availability),
                                            "rating": p.rating,
                                            "rating_count": p.rating_count,
                                        }
                                        for p in rescued_relevant[:6]
                                    ],
                                },
                            )

                    browse = await adapter.search_products("", limit=100)
                    categories = sorted(
                        {c for p in browse.products for c in p.categories}
                    )
                    if categories:
                        return Executed(
                            succeeded=True,
                            action_type=str(action_type),
                            summary=f"Offered {len(categories)} categories to choose from.",
                            payload={"category_choices": categories},
                        )
                    # No department data at all on this platform - fall through
                    # to the ordinary unfiltered browse below rather than
                    # offering an empty choice list.

                def _within_price(p: Product) -> bool:
                    if not price_bounded:
                        return True
                    if p.price is None:
                        return False
                    if max_price is not None and p.price.amount > Decimal(str(max_price)):
                        return False
                    if min_price is not None and p.price.amount < Decimal(str(min_price)):
                        return False
                    return True

                def _within_category(p: Product) -> bool:
                    if not category:
                        return True
                    return any(c.lower() == category.lower() for c in p.categories)

                # Fetch a wider candidate set when narrowing further - a keyword
                # search's first 6 results, filtered or re-sorted down, can
                # easily land on zero (or the wrong ones) even when a better
                # match exists further in.
                narrowed = price_bounded or top_rated or bool(category)
                found = await adapter.search_products(query, limit=30 if narrowed else 6)
                # Re-score what the adapter's own keyword search already
                # found and keep only the best-matching tier - a product
                # sharing one word with the query (a "Running Cap" for
                # "running shoes") no longer rides along just because it
                # cleared the price filter, when a stronger match (sharing
                # both words) exists in the same result set. See
                # engine/execution/relevance.py for what this does and does
                # not do; it never touches the empty-query browse below.
                match_score, relevant = relevance.best_matches_with_score(
                    found.products, query
                )
                relevant = [p for p in relevant if _within_category(p)]
                in_budget = [p for p in relevant if _within_price(p)]

                # The hard product/category intent must never be discarded
                # just because applying a hard price constraint on top of
                # it produces zero results. Real running shoes existing,
                # all over budget, is a different, honest situation from
                # no running shoes existing at all - and a world apart from
                # substituting an unrelated cheap product as if it matched.
                # `budget_relaxed` lets the reply say which is true.
                budget_relaxed = bool(query) and bool(relevant) and price_bounded and not in_budget
                candidates = relevant if budget_relaxed else in_budget

                # A "match" whose entire justification is one coincidentally
                # shared word ("kayak" narrowed by the model to "water",
                # landing on an "Insulated Water Bottle") has had no real
                # discrimination applied - best_matches only ever compares
                # candidates against each other, and with a single-word
                # query there is nothing to compare against. Re-verify a
                # weak single-word hit against the same wider category/
                # description signal `shop_carries` already uses for the
                # zero-match browse fallback below, so a shop that plainly
                # doesn't sell anything like the query cannot present a
                # coincidental word overlap as if it answered the question.
                # Never applied to a multi-word query or a score>1 match -
                # those already survived real word-overlap discrimination.
                weak_match_rejected = False
                # Cached and reused below (the `if not titles` browse-fallback
                # branch) rather than fetched a second time - both branches
                # page the same real catalog for the same failed query in the
                # same turn, and doing it twice roughly doubled the adapter
                # round-trips a single negative query cost (up to ~20 instead
                # of ~10 at `_MAX_CATALOG_SCAN`), found live this session.
                wide_sample = None
                if candidates and relevance.weak_single_word_match(query, match_score):
                    wide_sample = await _full_catalog_sample(adapter)
                    # NOT gated on `relevance.shop_carries(query, wide_sample)`
                    # here, deliberately - checking the deterministic
                    # category/description overlap of `query` (the model's
                    # own narrowed proxy term, e.g. "water") against a
                    # catalog sample that BY CONSTRUCTION includes the very
                    # candidate that proxy term matched is close to
                    # tautological: "water" trivially "carries" because the
                    # Insulated Water Bottle's own text contains "water".
                    # Found live, confirmed directly: shop_carries("water",
                    # wide_sample) returns True even on a shop that plainly
                    # does not sell kayaks, silently defeating this entire
                    # gate. The semantic judge is asked directly instead,
                    # against the shopper's own original wording (never the
                    # model's substituted term - judging relevance against
                    # the paraphrase would just repeat its mistake). `None`
                    # (unavailable/throttled) keeps the existing, already-
                    # deterministic `shop_carries(query, ...)` verdict as a
                    # fallback rather than treating an unconfirmed check as
                    # permission either way.
                    semantic_carries = await self._shop_carries_semantic(raw_said, wide_sample)
                    if semantic_carries is False or (
                        semantic_carries is None
                        and not relevance.shop_carries(query, wide_sample)
                    ):
                        candidates = []
                        relevant = []
                        weak_match_rejected = True

                if top_rated:
                    # Never invented: a product with no rating on this platform
                    # is excluded from "top rated" rather than treated as a
                    # tied-last 0 - that would be asserting a rating nobody
                    # gave it.
                    candidates = [p for p in candidates if p.rating is not None]
                    candidates.sort(key=lambda p: p.rating, reverse=True)
                elif budget_relaxed:
                    # Cheapest-first, so the closest-to-affordable real
                    # match leads rather than an arbitrary catalog order.
                    candidates.sort(
                        key=lambda p: p.price.amount if p.price else Decimal("inf")
                    )
                titles = [p.title for p in candidates[:6]]
                # Recommending is not searching. If the model's invented term
                # finds nothing, there is still a catalogue to show - and telling
                # somebody who said hello that we could not turn anything up is
                # absurd. A genuinely failed search is SUGGEST_ALTERNATIVE, which
                # keeps its honest empty case below.
                #
                # A price limit does not disable this fallback - it still filters
                # afterward, the same as the keyword search above. "Shoes under
                # 2000" against a shop whose products are titled "Sneaker" and
                # "Shoe" never matches the word "shoes" literally, so refusing to
                # browse the wider catalog here would tell a shopper nothing
                # exists under 2000 when a cheaper product does, just not one
                # whose title happens to contain their exact word.
                # Whether this result set is actually a keyword match, or a
                # relevance-relaxed browse standing in for one - set below,
                # before `query` can be blanked out for the browse retry, so
                # the caller can tell "these match what was asked" from
                # "nothing matched, these are what's available instead"
                # rather than getting a confident-looking list either way.
                relevance_relaxed = False
                shop_carries_query = not weak_match_rejected
                if not titles and action_type is ActionType.RECOMMEND_PRODUCTS:
                    relevance_relaxed = bool(query)
                    # Third round of the same fix. `_full_catalog_sample`
                    # pages through the platform's own real, reported total
                    # match count (up to `_MAX_CATALOG_SCAN`) instead of
                    # trusting a single fixed-size page to stand in for the
                    # whole catalog - see that function's own docstring for
                    # why the two prior versions of this exact comment
                    # (limit=100, then limit=200) were each still silently
                    # wrong on anything bigger than the page itself. A real
                    # 40,000-SKU catalog is still not fully covered by
                    # `_MAX_CATALOG_SCAN` - that residual gap is real,
                    # disclosed, and not claimed solved.
                    wide_products = wide_sample if wide_sample is not None else await _full_catalog_sample(adapter)
                    candidates = [
                        p for p in wide_products if _within_price(p) and _within_category(p)
                    ]
                    if top_rated:
                        candidates = [p for p in candidates if p.rating is not None]
                        candidates.sort(key=lambda p: p.rating, reverse=True)
                    titles = [p.title for p in candidates[:6]]
                    # A literal keyword-overlap relevance guard was tried
                    # here once (refuse this fallback outright when nothing
                    # in the browse shares a literal word with the failed
                    # query) and reverted: "a fruity coffee" searches for
                    # "coffee", which matches nothing on titles because
                    # Kettle's products are named by varietal ("Ethiopia
                    # Guji Natural") - a literal-word guard cannot tell that
                    # case apart from a shop genuinely not carrying
                    # something.
                    #
                    # relevance.shop_carries checks a wider, still-real
                    # signal instead - the shop's own declared product
                    # categories (already fetched in `wide_products`, no
                    # extra call) plus product description text, not just
                    # titles. "Coffee" overlaps Kettle's real "Coffee"
                    # category; "kayak" overlaps nothing in a running-shoe
                    # or coffee shop's real categories. Only gates this
                    # unfiltered-browse substitution path - never the
                    # narrower keyword-matched result above, which already
                    # found something real.
                    if query and not relevant:
                        shop_carries_query = relevance.shop_carries(
                            query, wide_products
                        )
                        if not shop_carries_query:
                            # Deterministic category/description overlap
                            # says no. Second opinion via the semantic
                            # judge, against the shopper's own original
                            # wording - this is what lets "something smooth
                            # and not too bitter" at a coffee shop keep
                            # working: it shares zero literal words with
                            # any product or category, but a real semantic
                            # judgment against the shop's real categories/
                            # sample titles can recognize it as a genuine
                            # coffee-shop taste question. `None`
                            # (unavailable/throttled) keeps the
                            # deterministic "not carried" verdict.
                            semantic_carries = await self._shop_carries_semantic(
                                raw_said, wide_products
                            )
                            if semantic_carries is True:
                                shop_carries_query = True
                            else:
                                titles = []

                if not titles:
                    if query and not shop_carries_query:
                        return Executed(
                            succeeded=False,
                            action_type=str(action_type),
                            summary=f'"{query}" is not something this shop carries.',
                            payload={"query": query, "products": [], "not_carried": True},
                            final_state=str(CaseState.FAILED),
                        )
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "The catalog came back empty."
                            if not query
                            else f'Searched for "{query}" and still found nothing.'
                        ),
                        payload={"query": query, "products": []},
                        final_state=str(CaseState.FAILED),
                    )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        # An empty query is a browse, not a search for nothing.
                        # 'Searched "" and found 6' reads as a bug in the queue.
                        (
                            f"Showed {len(titles)} from the catalog: "
                            if not query
                            else f'Searched "{query}" and found {len(titles)}: '
                        )
                        + ", ".join(titles[:3])
                        + ("…" if len(titles) > 3 else "")
                    ),
                    payload={
                        "query": query,
                        "relevance_relaxed": relevance_relaxed,
                        "budget_relaxed": budget_relaxed,
                        "max_price": max_price,
                        "products": [
                            {
                                "product_id": p.product_id,
                                "title": p.title,
                                # The merchant's own words about the product. A card
                                # with a name and a price gives a shopper nothing to
                                # judge, so they either tap blindly or ignore it.
                                "description": p.description,
                                "price": str(p.price) if p.price else None,
                                "availability": str(p.availability),
                                "rating": p.rating,
                                "rating_count": p.rating_count,
                            }
                            for p in candidates[:6]
                        ],
                    },
                )

            case ActionType.CHECK_AVAILABILITY:
                product_id = params.get("product_id")
                if not product_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="No product was named, so there was nothing to check.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                stock = await adapter.check_inventory(product_id)
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{product_id} is {stock.availability}"
                        + (
                            f", {stock.quantity_available} left"
                            if stock.quantity_available is not None
                            else ""
                        )
                    ),
                    payload={
                        "product_id": product_id,
                        "availability": str(stock.availability),
                        "quantity_available": stock.quantity_available,
                    },
                )
            case ActionType.COMPARE_PRODUCTS:
                first_id = params.get("product_id")
                second_id = params.get("compare_with_id")
                if not (first_id and second_id):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "Two products are needed to compare, and only one "
                            "was named."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                # Fetched fresh rather than read from anything cached earlier in
                # the conversation - a comparison built from a stale price or
                # stock state is worse than not offering one.
                first, second = await asyncio.gather(
                    adapter.get_product(first_id), adapter.get_product(second_id)
                )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=f"Compared {first.title} with {second.title}.",
                    payload={
                        "comparison": [
                            {
                                "product_id": p.product_id,
                                "title": p.title,
                                "description": p.description,
                                "price": str(p.price) if p.price else None,
                                "availability": str(p.availability),
                            }
                            for p in (first, second)
                        ],
                    },
                )
            case ActionType.CHECK_COMPATIBILITY:
                first_id = params.get("product_id")
                second_id = params.get("compare_with_id")
                if not (first_id and second_id):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "Two products are needed to check compatibility, "
                            "and only one was named."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                # Fetched fresh, same reasoning as COMPARE_PRODUCTS: a
                # compatibility verdict built from a stale product record is
                # worse than not answering.
                first, second = await asyncio.gather(
                    adapter.get_product(first_id), adapter.get_product(second_id)
                )
                result = product_relations.check_compatibility(first, second)
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=result.reason,
                    payload={
                        "verdict": result.verdict,
                        "first": {"product_id": first.product_id, "title": first.title},
                        "second": {"product_id": second.product_id, "title": second.title},
                    },
                )
            case ActionType.SUGGEST_COMPLEMENTARY:
                anchor_id = params.get("product_id")
                if not anchor_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="A product is needed to suggest what goes with it.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                anchor = await adapter.get_product(anchor_id)
                related_ids = product_relations.complementary_product_ids(anchor)
                related = (
                    await asyncio.gather(
                        *(adapter.get_product(pid) for pid in related_ids)
                    )
                    if related_ids
                    else []
                )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{len(related)} declared complement(s) for {anchor.title}."
                        if related
                        else f"No declared complementary products for {anchor.title}."
                    ),
                    payload={
                        "anchor": {"product_id": anchor.product_id, "title": anchor.title},
                        "complements": [
                            {
                                "product_id": p.product_id,
                                "title": p.title,
                                "price": str(p.price) if p.price else None,
                                "availability": str(p.availability),
                            }
                            for p in related
                        ],
                    },
                )
            case ActionType.CART_INTELLIGENCE:
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to look at yet.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )
                cart = await adapter.get_cart(case.cart_id)
                distinct_ids = {line.product_id for line in cart.lines}
                fetched = await asyncio.gather(
                    *(adapter.get_product(pid) for pid in distinct_ids),
                    return_exceptions=True,
                )
                products_by_id = {
                    p.product_id: p for p in fetched if isinstance(p, Product)
                }
                shopping_state = None
                if case.session_id:
                    shopping_state = await db.shopping_state.get(
                        case.session_id, case.connection_id
                    )
                findings = cart_intelligence.analyze_cart(
                    cart, products_by_id, shopping_state
                )
                # A diagnosis with nothing to do about it is the "read-only
                # twin" anti-pattern CLAUDE.md names explicitly - so the one
                # finding this can act on (a real, merchant-declared
                # complement missing from the cart) gets turned into an
                # actual, acceptable proposal in the same turn, not just a
                # sentence. Only the first missing-complement finding's
                # first product - one proposal per turn, not a wall of them.
                created_proposal = None
                complement_finding = next(
                    (f for f in findings if f.kind == "missing_complement"), None
                )
                if complement_finding and complement_finding.product_ids:
                    complement_id = complement_finding.product_ids[0]
                    try:
                        complement_product = await adapter.get_product(complement_id)
                        created_proposal = await db.cart_proposals.create(
                            connection_id=case.connection_id,
                            session_id=case.session_id or "",
                            shopper_id=None,
                            cart_id=case.cart_id,
                            change_type="ADD",
                            product_id=complement_id,
                            reason="declared complementary to something already in your cart",
                            evidence=["declared compatible by the merchant"],
                            snapshot_price=(
                                str(complement_product.price.amount)
                                if complement_product.price
                                else None
                            ),
                        )
                    except CommerceError:
                        pass
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{len(findings)} thing(s) worth flagging about this cart."
                        if findings
                        else "Nothing to flag - the cart looks fine."
                    ),
                    payload={
                        "findings": [
                            {
                                "kind": f.kind,
                                "message": f.message,
                                "product_ids": f.product_ids,
                            }
                            for f in findings
                        ],
                        "created_proposal": created_proposal,
                    },
                )
            case ActionType.PROPOSE_CART_CHANGE:
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to propose a change to.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )
                change_type = str(params.get("proposal_change_type") or "").upper()
                product_id = params.get("product_id")
                replaces_id = params.get("proposal_replaces_product_id")
                if not change_type:
                    # The model reliably names the product but not always the
                    # kind of change (live-observed: a plain "propose adding
                    # X" call omitted proposal_change_type entirely). The
                    # product's own presence/absence of a stated replacement
                    # target is enough to infer the two common cases
                    # deterministically rather than failing a proposal the
                    # model clearly did intend to make.
                    change_type = "REPLACE" if replaces_id else "ADD"
                if change_type not in {"ADD", "REMOVE", "REPLACE", "CHANGE_QUANTITY"} or not product_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="Not enough information to propose a cart change.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                if change_type == "REPLACE" and not replaces_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="A replacement needs to name what it replaces.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                try:
                    product = await adapter.get_product(product_id)
                except CommerceError:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="Could not find that product to propose it.",
                        error_code="PRODUCT_UNAVAILABLE",
                        final_state=str(CaseState.FAILED),
                    )
                raw_qty = params.get("quantity")
                quantity = int(raw_qty) if str(raw_qty).isdigit() else None
                evidence = []
                if replaces_id and replaces_id in product.compatible_with:
                    evidence.append(f"declared compatible with {replaces_id}")
                if product.compare_at_price and product.price:
                    evidence.append("currently discounted from the usual price")
                proposal = await db.cart_proposals.create(
                    connection_id=case.connection_id,
                    session_id=case.session_id or "",
                    shopper_id=None,
                    cart_id=case.cart_id,
                    change_type=change_type,
                    product_id=product_id,
                    variant_id=params.get("variant_id"),
                    quantity=quantity,
                    replaces_product_id=replaces_id,
                    reason=str(params.get("proposal_reason") or "suggested based on your cart"),
                    evidence=evidence,
                    snapshot_price=str(product.price.amount) if product.price else None,
                )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=f"Proposed {change_type.lower()} for {product.title}.",
                    payload={"proposal": proposal, "product_title": product.title},
                )
            case ActionType.ADD_TO_CART:
                product_id = params.get("chosen_product") or params.get("product_id")
                if not (product_id and case.cart_id):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="A cart and a product are both needed to add a line.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                product = await adapter.get_product(product_id)
                buyable = [
                    v for v in product.variants if str(v.availability) != "OUT_OF_STOCK"
                ]

                # One rule: a product with options is only ever added by choosing
                # one explicitly.
                #
                # This replaced four interacting mechanisms - the model's suggested
                # variant, matching the shopper's words against labels, matching in
                # reverse for longer labels, and a separate needs-choice check. Each
                # was defensible alone. Together they let a shopper end up with two
                # bags of coffee: one added on a partial match, one added when they
                # picked properly.
                #
                # chosen_variant is set only when a shopper taps an option, so it is
                # the one signal that is unambiguously a choice they made. The
                # model's guess is ignored entirely: it cannot know a size nobody
                # stated, and once the catalog showed it real ids it started
                # supplying them confidently anyway.
                variant_id = params.get("chosen_variant")

                # A typed option, and only when the whole message is the label.
                #
                # Tapping is the intended path, but a shopper who types "8" or
                # "250g ground" in reply to being asked has plainly answered, and
                # asking again would be obtuse. The match is exact and against the
                # entire message - no containment, no partial, nothing fuzzy. That
                # restraint is deliberate: the fuzzy version is what produced two
                # bags of coffee for a shopper who wanted one.
                if not variant_id:
                    said = str(params.get("said") or "").strip().lower()
                    if said:
                        variant_id = next(
                            (
                                v.variant_id
                                for v in buyable
                                if (v.title or "").strip().lower() == said
                            ),
                            None,
                        )

                if product.variants and not variant_id:
                    if not buyable:
                        return Executed(
                            succeeded=False,
                            action_type=str(action_type),
                            summary=f"Every option of {product.title} is sold out.",
                            error_code="INVENTORY_INSUFFICIENT",
                            final_state=str(CaseState.FAILED),
                        )
                    if len(buyable) == 1:
                        # Only one thing they could mean. Asking would be pedantic.
                        variant_id = buyable[0].variant_id
                    else:
                        return Executed(
                            succeeded=False,
                            action_type=str(action_type),
                            summary=(
                                f"{product.title} comes in {len(buyable)} options - "
                                "asked which one."
                            ),
                            needs_choice=True,
                            choices=[
                                {
                                    "variant_id": v.variant_id,
                                    "label": v.title or v.variant_id,
                                    "product_id": product_id,
                                    "product_title": product.title,
                                    "left": v.quantity_available,
                                }
                                for v in buyable
                            ],
                            payload={"product_id": product_id},
                            final_state=str(CaseState.DIAGNOSED),
                        )

                # A chosen option that is not actually on this product is refused
                # rather than dropped. Silently adding the parent product with no
                # option is how an order ends up with a line nobody chose.
                if variant_id and not any(
                    v.variant_id == variant_id for v in product.variants
                ):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=f"{variant_id} is not an option on {product.title}.",
                        error_code="VARIANT_UNAVAILABLE",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.add_to_cart(
                    case.cart_id, product_id, variant_id=variant_id
                )

                chosen = next(
                    (v.title for v in product.variants if v.variant_id == variant_id),
                    None,
                )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Added {product.title}"
                        + (f" ({chosen})" if chosen else "")
                        + f". Cart now holds {cart.item_count} item(s), "
                        f"{cart.grand_total}."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                    },
                )

            case ActionType.CLEAR_CART:
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to clear.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)

                if cart.is_empty:
                    return Executed(
                        succeeded=True,
                        action_type=str(action_type),
                        summary="The cart was already empty.",
                        shopper_summary="Your cart is already empty.",
                        payload={"cart_id": case.cart_id, "item_count": 0},
                    )

                # Every line, one at a time. There is no clear-cart operation in the
                # commerce interface and adding one would mean an adapter change per
                # platform for something every platform can already do with the
                # operation it has.
                removed = [line.title for line in cart.lines]
                for line in cart.lines:
                    cart = await adapter.update_cart(
                        case.cart_id, line.line_id, quantity=0
                    )

                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Cleared {len(removed)} line(s): " + ", ".join(removed[:3])
                    ),
                    shopper_summary=(
                        f"Your cart is empty now - I took out "
                        f"{len(removed)} item{'' if len(removed) == 1 else 's'}."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                        "cleared": removed,
                    },
                )

            case ActionType.REMOVE_CART_LINE | ActionType.UPDATE_CART_QUANTITY:
                # The model names a product; the platform wants a line id. Reading
                # the cart to translate is the adapter boundary working as intended -
                # a shopper says "remove the racing shoe", not "remove LN00042".
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to change.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)
                wanted = str(params.get("product_id") or "").lower()
                title_hint = str(params.get("query") or "").lower()

                line = None
                for candidate in cart.lines:
                    if wanted and candidate.product_id.lower() == wanted:
                        line = candidate
                        break
                    if title_hint and title_hint in candidate.title.lower():
                        line = candidate
                        break
                # One item in the cart and no usable hint - there is only one thing
                # they can mean, and refusing on a technicality would be obtuse.
                if line is None and len(cart.lines) == 1:
                    line = cart.lines[0]

                if line is None:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "Could not tell which item they meant. Asking is better "
                            "than removing the wrong one."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                if action_type is ActionType.REMOVE_CART_LINE:
                    quantity = 0
                else:
                    raw_qty = params.get("quantity")
                    quantity = int(raw_qty) if str(raw_qty).isdigit() else 1

                updated = await adapter.update_cart(
                    case.cart_id, line.line_id, quantity=quantity
                )
                verb = "Removed" if quantity == 0 else f"Set to {quantity}:"
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{verb} {line.title}. Cart now holds "
                        f"{updated.item_count} item(s), {updated.grand_total}."
                    ),
                    payload={
                        "cart_id": updated.cart_id,
                        "item_count": updated.item_count,
                        "grand_total": str(updated.grand_total),
                        "removed": line.title if quantity == 0 else None,
                    },
                )
            case ActionType.CHECK_ORDER_STATUS:
                # The number the shopper gave, or the one this session already
                # knows about. Never a number the model produced on its own - it
                # has no way to know whose order is whose, and a plausible-looking
                # id is the easiest thing in the world for it to invent.
                wanted = str(params.get("order_id") or case.order_id or "").strip()

                if not wanted:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="No order number was given, so nothing was looked up.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                try:
                    order = await adapter.get_order(wanted)
                except CommerceError:
                    # The same answer whether the order does not exist or the
                    # platform refused. Distinguishing them would let somebody
                    # walk the numbering and learn which ids are real.
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=f"No order found for {wanted}.",
                        error_code="ORDER_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                paid = str(order.payment_status) == "CAPTURED"

                # shopper_summary here is deliberately thin - status, payment state,
                # total, no line items - because this layer has no shopper identity
                # to check an order id against, and an order id is guessable. Line
                # items go in payload instead, which is never sent to a browser
                # (ChatReply has no payload field) - only chat.py reads it, and only
                # after it has verified, via db.owners, that the session asking is
                # the one that placed this order. That is what lets a shopper who
                # really does own this order hear what they bought, without handing
                # the same detail to a stranger who guessed or was told a number.
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{order.order_id}: {order.status}, payment "
                        f"{order.payment_status}, {order.grand_total}."
                    ),
                    shopper_summary=(
                        f"Order {order.order_id} is "
                        + (
                            f"paid in full - {order.amount_paid}. It will be on its "
                            "way shortly."
                            if paid
                            else f"still awaiting payment. The total is "
                            f"{order.grand_total} and nothing has been charged yet."
                        )
                    ),
                    payload={
                        "order_id": order.order_id,
                        "status": str(order.status),
                        "payment_status": str(order.payment_status),
                        "grand_total": str(order.grand_total)
                        if order.grand_total
                        else None,
                        "amount_paid": str(order.amount_paid)
                        if order.amount_paid
                        else None,
                        "paid": paid,
                        "lines": [
                            {"title": line.title, "quantity": line.quantity}
                            for line in order.lines
                        ],
                    },
                )

            case ActionType.CHECK_CART_STATUS:
                # Reads and reports. Same shape as PREPARE_CHECKOUT's own cart
                # read just below, minus the "did they ask to pay" gate and
                # the empty-cart refusal - "what's in my cart" on an empty
                # cart is a legitimate question with a legitimate answer,
                # not a failure.
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to check.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)

                if cart.is_empty:
                    return Executed(
                        succeeded=True,
                        action_type=str(action_type),
                        summary="The cart is empty.",
                        shopper_summary="Your cart is empty right now.",
                        payload={"cart_id": cart.cart_id, "item_count": 0, "lines": []},
                    )

                items = ", ".join(
                    f"{line.quantity} x {line.title}"
                    if line.quantity != 1
                    else line.title
                    for line in cart.lines
                )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Cart {cart.cart_id}: {cart.item_count} item(s), "
                        f"{cart.grand_total}."
                    ),
                    shopper_summary=(
                        f"Your cart has {cart.item_count} item(s), "
                        f"{cart.grand_total}: {items}."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                        "lines": [
                            {
                                "title": line.title,
                                "variant": line.variant_id,
                                "quantity": line.quantity,
                                "total": str(line.line_total),
                            }
                            for line in cart.lines
                        ],
                    },
                )

            case ActionType.PREPARE_CHECKOUT:
                # Did the shopper actually ask to pay?
                #
                # Checked here rather than in the prompt, because three prompt rules
                # did not stop the model offering a payment form in reply to "hi".
                # Prompting is advice; this needs to be a fact.
                #
                # The list is short and literal on purpose. It is not trying to
                # understand intent - the model does that, and it is what got this
                # wrong. It asks a narrower question: did this person mention paying
                # at all? Somebody who did will have used one of these words.
                #
                # Erring toward refusing is the right direction. Missing a checkout
                # costs one turn. Offering one unasked is the pushy behaviour that
                # makes shoppers distrust these assistants.
                said = str(params.get("said") or case.query or "").lower()
                asked_to_pay = any(
                    word in said
                    for word in (
                        "pay",
                        "paying",
                        "payment",
                        "checkout",
                        "check out",
                        "buy",
                        "purchase",
                        "order this",
                        "place my order",
                        "card",
                        "total",
                    )
                )

                if not asked_to_pay:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "Did not offer a checkout: nothing in what the shopper "
                            "said was about paying."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                # Reads and reports. Charges nothing, which is why this is
                # non-financial and why the AI is allowed to propose it at all.
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to check out.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)

                if cart.is_empty:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="The cart is empty, so there is nothing to pay for.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                # The cards are the merchant's test set rather than a real payment
                # method list, because these are demo stores. On a real platform
                # this is where the merchant's own methods would be read.
                #
                # Named honestly. A demo that hides which card declines is a demo
                # nobody can drive, and a shopper who taps "no funds" chose to.
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Showed the shopper their total, {cart.grand_total}, "
                        f"and {cart.item_count} item(s)."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                        "subtotal": str(cart.subtotal),
                        "lines": [
                            {
                                "title": line.title,
                                # Two lines of the same product with different
                                # variants are correct and were indistinguishable,
                                # which is what made a right answer look wrong.
                                "variant": line.variant_id,
                                "quantity": line.quantity,
                                "total": str(line.line_total),
                            }
                            for line in cart.lines
                        ],
                        "cards": [
                            {"last4": "1111", "label": "Card ending 1111"},
                            {"last4": "0002", "label": "Card ending 0002 (no funds)"},
                            {"last4": "0003", "label": "Card ending 0003 (expired)"},
                        ],
                    },
                )

            case ActionType.APPLY_PROMOTION:
                code = params.get("code") or params.get("query")
                if not (code and case.cart_id):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "No coupon code was recorded, so nothing could be "
                            "applied. A discount is not something to guess at."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                # Idempotency comes from the case id, so approving twice cannot
                # discount twice.
                await adapter.apply_promotion(case.cart_id, str(code))
                cart = await adapter.get_cart(case.cart_id)
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Applied {code}. Discount {cart.discount_total}, "
                        f"new total {cart.grand_total}."
                    ),
                    shopper_summary=(
                        f"Your {code} discount has been applied. Your total is now "
                        f"{cart.grand_total}."
                    ),
                    payload={
                        "code": str(code),
                        "discount": str(cart.discount_total),
                        "grand_total": str(cart.grand_total),
                    },
                )

            case (
                ActionType.RETRY_PAYMENT
                | ActionType.OFFER_ALTERNATE_PAYMENT
                | ActionType.SPLIT_PAYMENT
            ):
                # Only reachable on a platform that declared recovery supported.
                # Northfield raises CapabilityUnsupported long before this runs.
                if not case.order_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="No order was recorded, so there is nothing to recover.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                method = _RECOVERY_METHOD[action_type]
                key = db.idempotency.key_for(
                    case.case_id, str(action_type), {"order": case.order_id}
                )
                outcome = await adapter.recover_payment(
                    case.order_id, method=method, idempotency_key=key
                )

                if not outcome.succeeded:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            f"Recovery did not work: "
                            f"{outcome.reason or 'the platform declined'}."
                        ),
                        shopper_summary=(
                            f"We tried to put order {case.order_id} through again "
                            "and it did not go through. Someone from the shop will "
                            "be in touch."
                        ),
                        payload={"method": str(method), "order_id": case.order_id},
                        error_code="PAYMENT_RECOVERY_FAILED",
                        final_state=str(CaseState.FAILED),
                    )
                amount = outcome.amount_recovered

                # A successful recovery is a completed purchase, the same as any
                # other, and must count toward total sales the same way - not
                # just toward revenue_recovered. Without this, the payment
                # ledger (which total_sales reads) never learns this basket was
                # ever paid, because recover_payment's own idempotency key is
                # derived from the case and action, not from cart_payment_key -
                # a different lock than the one begin_payment/payment_settled
                # keep. Best-effort: a merchant's total-sales figure degrading
                # to "missing this one row" is a far smaller problem than a
                # shopper's recovered order failing over a report-side write.
                if case.cart_id and amount:
                    try:
                        recovered_order = outcome.order
                        await db.idempotency.payment_settled(
                            case.connection_id,
                            case.cart_id,
                            succeeded=True,
                            order_id=case.order_id,
                            summary=f"recovered {case.order_id}",
                            amount=str(amount.amount),
                            currency=amount.currency,
                            lines=(
                                [
                                    {
                                        "product_id": line.product_id,
                                        "product_name": line.title,
                                        "quantity": line.quantity,
                                        "unit_price": (
                                            str(line.unit_price.amount)
                                            if line.unit_price
                                            else None
                                        ),
                                        "line_total": (
                                            str(line.line_total.amount)
                                            if line.line_total
                                            else None
                                        ),
                                        "currency": (
                                            line.unit_price.currency
                                            if line.unit_price
                                            else None
                                        ),
                                    }
                                    for line in recovered_order.lines
                                ]
                                if recovered_order
                                else None
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "could not record recovered payment in the sales ledger"
                        )

                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Recovered {amount} on {case.order_id}"
                        + (f" - {outcome.reason}." if outcome.reason else ".")
                    ),
                    shopper_summary=(
                        f"Good news - your payment for order {case.order_id} has "
                        f"gone through. Nothing more for you to do."
                    ),
                    payload={
                        "method": str(method),
                        "order_id": case.order_id,
                        # Read by record_outcome. This is the first place in the
                        # system where a real recovered amount exists.
                        "recovered_amount": str(amount.amount) if amount else None,
                        "recovered_currency": amount.currency if amount else None,
                    },
                )

        return Executed(
            succeeded=False,
            action_type=str(action_type),
            summary="The engine has no execution path for that action yet.",
            error_code="CAPABILITY_UNSUPPORTED",
            final_state=str(CaseState.UNSUPPORTED),
        )

    def _terminal(self, action_type: ActionType, case) -> Executed:
        """Actions that complete without a backend call."""
        if action_type is ActionType.ESCALATE_TO_HUMAN:
            return Executed(
                succeeded=True,
                action_type=str(action_type),
                summary=(
                    "Handed to a person. Nothing was attempted automatically, "
                    "which was the correct outcome here."
                ),
                shopper_summary=(
                    "Someone from the shop has picked this up and will be in touch."
                ),
                final_state=str(CaseState.ESCALATED),
            )
        if action_type is ActionType.NOTIFY_BACK_IN_STOCK:
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary=(
                    "Back-in-stock alerts need an email service, which is not "
                    "built. Recorded rather than pretended."
                ),
                error_code="CAPABILITY_UNSUPPORTED",
                final_state=str(CaseState.UNSUPPORTED),
            )
        # The shopper_reply is deliberately not used here. When an action was held
        # for approval, that reply is the holding message - so echoing it back would
        # tell the approver that something needs approving, as the result of them
        # approving it. What the AI actually wanted to say is the useful thing.
        said = case.model_reply or case.shopper_reply
        return Executed(
            succeeded=True,
            action_type=str(action_type),
            summary=(
                f"Answered the shopper: \u201c{said}\u201d"
                if said
                else "Answered the shopper. No commerce operation was needed."
            ),
        )
    @staticmethod
    def _operation_for(action_type: ActionType) -> Operation | None:
        mapping = {
            ActionType.SUGGEST_ALTERNATIVE: Operation.SEARCH_PRODUCTS,
            ActionType.RECOMMEND_PRODUCTS: Operation.SEARCH_PRODUCTS,
            ActionType.CHECK_AVAILABILITY: Operation.CHECK_INVENTORY,
            ActionType.ADD_TO_CART: Operation.ADD_TO_CART,
            ActionType.UPDATE_CART_QUANTITY: Operation.UPDATE_CART,
            ActionType.REMOVE_CART_LINE: Operation.UPDATE_CART,
            ActionType.APPLY_PROMOTION: Operation.APPLY_PROMOTION,
            ActionType.RETRY_PAYMENT: Operation.RECOVER_PAYMENT,
            ActionType.OFFER_ALTERNATE_PAYMENT: Operation.RECOVER_PAYMENT,
            ActionType.SPLIT_PAYMENT: Operation.RECOVER_PAYMENT,
            ActionType.ISSUE_REFUND: Operation.RECOVER_PAYMENT,
        }
        return mapping.get(action_type)
