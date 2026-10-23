"""Guided multi-product shopping missions: "I'm going for a long run. I want
shoes, socks and something for energy - suggest which are available now".

One request becomes an ordered set of STAGES (one `MissionItem` each, the same
structure `missions.py` already tracks). The shopper is shown what is really in
stock for every stage up front, then guided through them one at a time:
pick a product -> choose an available size/option -> the cart confirms it ->
the next stage starts by itself -> a closing summary. The shopper never has to
repeat the request or ask "what about socks and energy?".

Authority and safety (nothing here is new machinery):

* Stage completion is read from the REAL cart (`adapter.get_cart`), never from
  a model claim or from this module's own earlier output. A stage advances only
  when a product belonging to it is actually a line in the cart, so an add that
  failed, is awaiting the merchant's approval, or was never made can never be
  reported as done - and a retry cannot add anything twice, because adding is
  still `fulfillment.py`'s job (it already refuses a product that is in the
  cart) and the sync below is idempotent.
* This module never adds to a cart, never builds a proposal, never touches the
  Decision Engine, Risk Gate, execution or payment. Selection still goes through
  the existing add paths (`fulfillment.start_fulfillment` for a typed request,
  the product card's own tap for a click).
* Candidates, prices, stock and sizes are read from the adapter and the same
  deterministic ranking/relevance pass (`build_evidence`) discovery and
  missions already use. A stage with nothing in stock is reported as such and
  skipped - nothing is substituted or invented.
* Platform down: retrieval degrades to an honest "can't check the shop" and no
  stage state is created.

State lives in `MissionItem` (additive fields `guided`, `stage`,
`added_product_id`, `sold_out_titles`) persisted through the existing
`shopping_state.mission_items` column, so selections, completed stages,
constraints and the pending stage survive across turns and interruptions.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from engine.db import shopping_state as shopping_state_store
from engine.execution import events as shopper_events

from .evidence import build_evidence
from .retrieval import retrieve
from .state import MissionItem, ShoppingStateV2

logger = logging.getLogger(__name__)

_MAX_STAGES = 6
_SHOWN_PER_STAGE = 4
#: How many in-stock candidates a stage keeps for matching a cart line against
#: (a shopper may add one that was not among the four cards shown).
_KEPT_PER_STAGE = 12


# --------------------------------------------------------------------------
# Parsing: a deterministic reading of "I want A, B and C" - no model.
# --------------------------------------------------------------------------

_WANT = re.compile(
    r"\b(?:i|we)(?:\s+(?:really|just|also))?\s*(?:want|need|require|would like|'d like|"
    r"am looking for|'m looking for|are looking for)\b|\blooking for\b",
    re.IGNORECASE,
)
_TAIL = re.compile(
    r"[.?!;]|\b(?:suggest|recommend|tell me|show me|let me know|which (?:are|is|ones?)|"
    r"that (?:are|is)|what(?:'s| is| are)|please|also)\b",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bplus\b|\bas well as\b)\s*", re.IGNORECASE)
_DET = re.compile(
    r"^(?:(?:a|an|the|some|any|one|two|three|my|new|good|nice|pair of|pairs of|couple of)\s+)+",
    re.IGNORECASE,
)
_SOMETHING = re.compile(
    r"^(?:something|anything|stuff|things?)\s+(?:for|to\s+help\s+with|to\s+give\s+me)\s+(.+)$",
    re.IGNORECASE,
)
_ACTIVITY = re.compile(
    r"\b(?:going|go|heading|headed|training|getting ready|preparing|prepare)\s+(?:out\s+)?for\s+"
    r"(?:an?\s+|my\s+|the\s+)?(?:\w+\s+){0,2}?"
    r"(run|running|jog|jogging|marathon|hike|hiking|trek|trekking|swim|swimming)\b",
    re.IGNORECASE,
)
_ACTIVITY_WORD = {
    "run": "running", "running": "running", "jog": "running", "jogging": "running",
    "marathon": "running", "hike": "hiking", "hiking": "hiking", "trek": "trekking",
    "trekking": "trekking", "swim": "swimming", "swimming": "swimming",
}


@dataclass
class StageSpec:
    label: str


@dataclass
class MissionSpec:
    stages: list[StageSpec]
    activity: str | None = None


def parse_mission(message: str) -> MissionSpec | None:
    """The ordered stages in a "I want A, B and C" request, or `None` when the
    message is not that shape (one product, a question, a cart command...).
    Conservative by design: any part that cannot be reduced to a short product
    phrase makes the whole message "not a mission", so ordinary discovery is
    never hijacked."""
    text = " ".join((message or "").split())
    if not text:
        return None
    m = _WANT.search(text)
    if m is None:
        return None
    activity = None
    am = _ACTIVITY.search(text)
    if am:
        activity = _ACTIVITY_WORD.get(am.group(1).lower())
    rest = text[m.end():].strip()
    tail = _TAIL.search(rest)
    if tail:
        rest = rest[: tail.start()]
    parts = [p.strip(" .") for p in _SPLIT.split(rest) if p.strip(" .")]
    if len(parts) < 2 or len(parts) > _MAX_STAGES:
        return None
    labels: list[str] = []
    for part in parts:
        cleaned = _DET.sub("", part).strip()
        sm = _SOMETHING.match(cleaned)
        if sm:
            cleaned = _DET.sub("", sm.group(1)).strip()
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", cleaned)
        if not words or len(words) > 3 or words[0].lower() in {"to", "compare", "see", "know"}:
            return None
        label = " ".join(w.lower() for w in words)
        if label not in labels:
            labels.append(label)
    if len(labels) < 2:
        return None
    return MissionSpec(stages=[StageSpec(label=l) for l in labels], activity=activity)


# --------------------------------------------------------------------------
# Real stock / candidates for one stage.
# --------------------------------------------------------------------------

def _in_stock(product) -> bool:
    variants = list(getattr(product, "variants", None) or [])
    if variants:
        return any(str(v.availability) != "OUT_OF_STOCK" for v in variants)
    return str(getattr(product, "availability", "UNKNOWN")) != "OUT_OF_STOCK"


def _activity_matches(product, activity: str) -> bool:
    stem = activity[:3]
    blob = f"{product.title} {product.description or ''} {' '.join(product.categories or [])}".lower()
    return stem in blob


def _money(value) -> str:
    return str(value) if value is not None else "price on request"


@dataclass
class StageResult:
    label: str
    candidate_ids: list[str] = field(default_factory=list)
    candidate_titles: list[str] = field(default_factory=list)
    sold_out_titles: list[str] = field(default_factory=list)
    unavailable: bool = False


async def stage_candidates(
    adapter, label: str, *, activity: str | None, budget: Decimal | None,
    message: str, memory_facts: list[dict] | None, avoid: set[str] | None = None,
) -> StageResult:
    """What is really in stock for `label` right now: the same retrieval and
    relevance verification missions/discovery use, then a stock check, then -
    only when real titles/descriptions support it - a preference for products
    that mention the activity ("running"). Never pads with anything that did
    not pass relevance."""
    result = await retrieve(adapter, label, limit=40)
    if result.unavailable:
        return StageResult(label=label, unavailable=True)
    state_dict = {
        "goal": label, "budget_amount": budget, "budget_currency": None, "constraints": [],
        "must_have": [], "nice_to_have": [], "exclusions": [], "priorities": [],
        "active_products": [], "mission_status": "exploring",
    }
    evidence = build_evidence(
        query=label, raw_said=f"{message} {label}", products=result.products,
        shopping_state=state_dict, memory_facts=memory_facts, catalog_sample=None,
        platform_unavailable=False,
    )
    by_id = {p.product_id: p for p in result.products}
    verified = [by_id[e.product_id] for e in evidence.products
                if e.relevance_verified and e.product_id in by_id]
    sold_out = [p.title for p in verified if not _in_stock(p)]
    buyable = [p for p in verified if _in_stock(p)]
    if activity:
        preferred = [p for p in buyable if _activity_matches(p, activity)]
        if preferred:
            buyable = preferred + [p for p in buyable if p not in preferred]
    if avoid:
        buyable = [p for p in buyable if p.product_id not in avoid] + \
                  [p for p in buyable if p.product_id in avoid]
    buyable = buyable[:_KEPT_PER_STAGE]
    return StageResult(
        label=label, candidate_ids=[p.product_id for p in buyable],
        candidate_titles=[p.title for p in buyable], sold_out_titles=sold_out[:3],
    )


# --------------------------------------------------------------------------
# State helpers
# --------------------------------------------------------------------------

def guided_items(state: ShoppingStateV2) -> list[MissionItem]:
    return [i for i in state.mission_items if i.guided]


def pending_stages(items: list[MissionItem]) -> list[MissionItem]:
    return [i for i in items if i.guided and i.stage == "pending"]


def current_stage(items: list[MissionItem]) -> MissionItem | None:
    pend = pending_stages(items)
    return pend[0] if pend else None


def is_active(state: ShoppingStateV2) -> bool:
    """A guided mission with at least one stage still open."""
    return current_stage(state.mission_items) is not None


def view_product_ids(state: ShoppingStateV2) -> list[str]:
    """What the shopper is looking at for references like "the first one" or
    "compare both": the CURRENT stage's shown options (not the whole mission)."""
    stage = current_stage(state.mission_items)
    return list(stage.candidates[:_SHOWN_PER_STAGE]) if stage else []


async def _persist(state: ShoppingStateV2, items: list[MissionItem]) -> ShoppingStateV2:
    await shopping_state_store.merge(
        state.session_id, state.connection_id,
        mission_items=[i.model_dump(mode="json") for i in items],
    )
    return state.model_copy(update={"mission_items": items})


async def _fetch(adapter, ids: list[str]) -> list:
    out = []
    for pid in ids:
        try:
            out.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - skip what cannot be fetched, never invent
            logger.warning("guided_mission: get_product failed for %r", pid, exc_info=True)
    return out


def _options_text(label: str, products: list) -> str:
    lines = [f"{p.title} ({_money(p.price)})" for p in products]
    return f"{label}: " + "; ".join(lines)


# --------------------------------------------------------------------------
# Cart is the authority.
# --------------------------------------------------------------------------

async def _cart_or_none(adapter, cart_id: str | None):
    if not cart_id:
        return None
    try:
        return await adapter.get_cart(cart_id)
    except Exception:  # noqa: BLE001 - platform down: no change, no claim
        logger.warning("guided_mission: get_cart failed for %r", cart_id, exc_info=True)
        return None


def sync_items_with_cart(items: list[MissionItem], cart) -> list[MissionItem]:
    """Marks every open guided stage that has a product in the real cart as
    added. Idempotent. Returns the stages newly completed by THIS call (a stage
    already added is never reported twice). One cart line completes at most one
    stage, current stage first."""
    if cart is None:
        return []
    lines = list(cart.lines)
    used: set[str] = set()
    newly: list[MissionItem] = []
    open_stages = [i for i in items if i.guided and i.stage == "pending"]
    for item in open_stages:
        wanted = set(item.candidates)
        for line in lines:
            if line.line_id in used or line.product_id not in wanted:
                continue
            item.stage = "added"
            item.status = "done"
            item.added_product_id = line.product_id
            item.selected_product_id = line.product_id
            used.add(line.line_id)
            newly.append(item)
            break
    return newly


async def _confirmed_lines(adapter, cart, items: list[MissionItem]) -> list[str]:
    """"Cloudstep Max Cushion Running Shoe (size 9) x1" for each added stage,
    read from the real cart line + the real product's variant."""
    out: list[str] = []
    for item in items:
        if item.stage != "added" or cart is None:
            continue
        line = next((ln for ln in cart.lines if ln.product_id == item.added_product_id), None)
        if line is None:
            continue
        detail = ""
        if line.variant_id:
            try:
                product = await adapter.get_product(line.product_id)
                variant = next((v for v in product.variants if v.variant_id == line.variant_id), None)
                if variant is not None:
                    attrs = ", ".join(f"{k} {v}" for k, v in variant.attributes.items())
                    detail = f" ({attrs or variant.title})" if (attrs or variant.title) else ""
            except Exception:  # noqa: BLE001
                detail = ""
        out.append(f"{line.title}{detail} x{line.quantity}")
    return out


# --------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------

async def _stage_prompt(
    adapter, item: MissionItem, *, lead: str = "Next up:", list_options: bool = True,
) -> tuple[str, list]:
    products = await _fetch(adapter, item.candidates[:_SHOWN_PER_STAGE])
    if not products:
        return f"I couldn't load options for {item.label} just now - try again in a moment.", []
    how = (
        "Tap Add on the one you want, or tell me which and I'll add it (you choose the size "
        "first if it has one). Say skip to leave this one out."
    )
    if not list_options:
        return f"{lead} {item.label}. {how}", products
    names = "; ".join(f"{p.title} ({_money(p.price)})" for p in products)
    extra = f" Sold out right now: {', '.join(item.sold_out_titles)}." if item.sold_out_titles else ""
    return f"{lead} {item.label}. In stock: {names}.{extra} {how}", products


_LEAD_FIRST = "Let's start with"


async def _summary(adapter, cart, items: list[MissionItem]) -> str:
    done = [i for i in items if i.guided]
    lines = await _confirmed_lines(adapter, cart, done)
    skipped = [i.label for i in done if i.stage == "skipped"]
    unavailable = [i.label for i in done if i.stage == "unavailable"]
    parts = ["That completes your list."]
    if lines:
        parts.append("In your cart: " + "; ".join(lines) + ".")
    if skipped:
        parts.append(f"Skipped: {', '.join(skipped)}.")
    if unavailable:
        parts.append(f"Not available here: {', '.join(unavailable)}.")
    if cart is not None and cart.lines:
        total = cart.grand_total or cart.subtotal
        parts.append(f"Cart total: {total} for {cart.item_count} item{'s' if cart.item_count != 1 else ''}.")
    parts.append("Nothing left to choose - say checkout whenever you're ready.")
    return " ".join(parts)


@dataclass
class GuidedTurn:
    reply: str
    products: list = field(default_factory=list)
    state: ShoppingStateV2 | None = None
    #: product ids that are "in view" after this turn (current stage's options).
    view_ids: list[str] = field(default_factory=list)


async def _advance_text(adapter, cart, state: ShoppingStateV2, items: list[MissionItem],
                        newly: list[MissionItem]) -> tuple[str, list]:
    """What follows a confirmed add: the confirmation, then the next stage's
    options - or the closing summary when nothing is left."""
    confirmed = await _confirmed_lines(adapter, cart, newly)
    head = ("Confirmed in your cart: " + "; ".join(confirmed) + ".") if confirmed else ""
    nxt = current_stage(items)
    if nxt is None:
        return (f"{head} {await _summary(adapter, cart, items)}").strip(), []
    prompt, products = await _stage_prompt(adapter, nxt)
    return f"{head} {prompt}".strip(), products


# --------------------------------------------------------------------------
# Start
# --------------------------------------------------------------------------

async def start(
    *, adapter, spec: MissionSpec, message: str, state: ShoppingStateV2,
    cart_id: str | None, memory_facts: list[dict] | None,
) -> GuidedTurn | None:
    """Builds every stage's real, in-stock options, then presents the whole plan
    and the first stage. Returns `None` (caller falls back to ordinary handling)
    when the shop cannot be reached - no state is created in that case."""
    avoid = await shopper_events.rejected_product_ids(state.connection_id, state.session_id)
    results = [
        await stage_candidates(
            adapter, s.label, activity=spec.activity, budget=None, message=message,
            memory_facts=memory_facts, avoid=avoid,
        )
        for s in spec.stages
    ]
    if all(r.unavailable for r in results):
        return None
    items: list[MissionItem] = []
    for r in results:
        label = r.label
        if (
            spec.activity and r.candidate_titles and spec.activity[:3] not in r.label.lower()
            and any(_title_mentions(t, spec.activity) for t in r.candidate_titles[:_SHOWN_PER_STAGE])
        ):
            label = f"{spec.activity} {r.label}"
        items.append(MissionItem(
            item_id=uuid.uuid4().hex[:12], label=label, search_query=r.label,
            candidates=r.candidate_ids, candidate_titles=r.candidate_titles,
            guided=True, stage="pending" if r.candidate_ids else "unavailable",
            status="exploring" if r.candidate_ids else "done",
            sold_out_titles=r.sold_out_titles,
        ))
    return await _present_plan(adapter, items, cart_id, state)


async def adopt(
    *, adapter, state: ShoppingStateV2, items: list[MissionItem], cart_id: str | None,
) -> GuidedTurn:
    """Turns freshly-created mission items (from the model-driven path, which
    found the stages itself) into a guided mission: stock-checks what they
    already carry, marks the rest unavailable, and presents the same plan."""
    items = [i.model_copy(deep=True) for i in items]
    for item in items:
        products = await _fetch(adapter, item.candidates)
        buyable = [p for p in products if _in_stock(p)]
        item.sold_out_titles = [p.title for p in products if not _in_stock(p)][:3]
        item.candidates = [p.product_id for p in buyable][:_KEPT_PER_STAGE]
        item.candidate_titles = [p.title for p in buyable][:_KEPT_PER_STAGE]
        item.guided = True
        item.stage = "pending" if item.candidates else "unavailable"
        item.status = "exploring" if item.candidates else "done"
    return await _present_plan(adapter, items, cart_id, state)


async def _present_plan(
    adapter, items: list[MissionItem], cart_id: str | None, state: ShoppingStateV2,
) -> GuidedTurn:
    """Persists the stages and writes the up-front plan: every stage's real
    in-stock options, stages that cannot be offered (and why), anything already
    in the cart, then the first open stage's call to action."""
    cart = await _cart_or_none(adapter, cart_id)
    sync_items_with_cart(items, cart)
    new_state = await _persist(state, items)

    shown_products: list = []
    plan_lines: list[str] = []
    for item in items:
        if item.stage == "unavailable":
            plan_lines.append(f"{item.label}: nothing in stock here right now, so I'll leave it out.")
            continue
        prods = await _fetch(adapter, item.candidates[:_SHOWN_PER_STAGE])
        if item.stage == "added":
            plan_lines.append(f"{item.label}: you already have it in your cart.")
            continue
        shown_products.extend(prods)
        sold = f" (sold out: {', '.join(item.sold_out_titles)})" if item.sold_out_titles else ""
        plan_lines.append(_options_text(item.label, prods) + sold)
    labels = [i.label for i in items]
    plan = (
        f"Here's what I'd cover: {', '.join(labels[:-1])} and {labels[-1]}. "
        "What's in stock right now - " + " | ".join(plan_lines) + "."
    )
    nxt = current_stage(items)
    if nxt is None:
        reply = f"{plan} {await _summary(adapter, cart, items)}"
        products: list = []
    else:
        prompt, _ = await _stage_prompt(adapter, nxt, lead=_LEAD_FIRST, list_options=False)
        reply = f"{plan} {prompt}"
        products = shown_products
    return GuidedTurn(reply=reply, products=products, state=new_state, view_ids=view_product_ids(new_state))


def _title_mentions(title: str, activity: str) -> bool:
    return activity[:3] in title.lower()


# --------------------------------------------------------------------------
# Follow-up turns while a mission is open
# --------------------------------------------------------------------------

_SKIP = re.compile(
    r"\b(?:skip|pass on|leave (?:it |that |this |the \w+ )?out|don'?t need|do not need|no need|"
    r"not interested|forget (?:about )?(?:it|that|this|the \w+)|i'?ll skip)\b", re.IGNORECASE)
_NEXT = re.compile(
    r"^(?:ok(?:ay)?[ ,.]*)?(?:next|continue|go on|carry on|keep going|what'?s next|what next|"
    r"where were we|resume|what else|and then\??|then\??)\W*$", re.IGNORECASE)
_ADD_ALL = re.compile(
    r"^(?:please )?(?:add|put|get|buy|take)\s+(?:all|these|those|them|everything|all of (?:them|these|those))"
    r"(?:\s+(?:to|into|in)\s+(?:my |the )?(?:cart|basket|bag))?\W*$", re.IGNORECASE)
_WHAT_ABOUT = re.compile(r"^(?:and )?what about (.+?)\W*$", re.IGNORECASE)
_BUDGET = re.compile(
    r"(?:under|below|less than|within|up to|upto|max(?:imum)?|no more than)\s*(?:rs\.?|inr|₹)?\s*"
    r"([\d][\d,]*(?:\.\d+)?)\s*(k)?\b", re.IGNORECASE)


def _stage_named(message: str, items: list[MissionItem]) -> MissionItem | None:
    words = set(re.findall(r"[a-z]+", message.lower()))
    for item in items:
        label_words = set(re.findall(r"[a-z]+", item.label.lower())) - {"running", "hiking"}
        if label_words & words:
            return item
    return None


def _parse_budget(message: str) -> Decimal | None:
    m = _BUDGET.search(message)
    if not m:
        return None
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value * 1000 if m.group(2) else value


def classify_command(message: str, state: ShoppingStateV2) -> str | None:
    """"skip" | "budget" | "next" | None - only while a guided mission is open
    and only for short messages that are unambiguously about the mission."""
    guided = guided_items(state)
    if not guided:
        return None
    active = is_active(state)
    text = " ".join((message or "").split())
    if not text or len(text.split()) > 12:
        return None
    if active and _ADD_ALL.match(text):
        return "add_all"
    if active and _SKIP.search(text):
        return "skip"
    if _NEXT.match(text):
        return "next"
    # "what about socks and energy?" - only when it names the mission's own
    # stages (or "the rest"); "what about waterproof socks?" is a new question.
    wa = _WHAT_ABOUT.match(text)
    if wa:
        rest = set(re.findall(r"[a-z]+", wa.group(1).lower()))
        stage_words = {w for i in guided for w in re.findall(r"[a-z]+", i.label.lower())}
        if rest & {"rest", "others", "other", "remaining"} or (rest & stage_words and rest <= (stage_words | {"and", "the", "then", "also"})):
            return "next"
    if active and _parse_budget(text) is not None and not re.search(r"\b(?:show|find|search|compare|add)\b", text, re.I):
        return "budget"
    return None


async def handle_command(
    *, adapter, command: str, message: str, state: ShoppingStateV2, cart_id: str | None,
    memory_facts: list[dict] | None,
) -> GuidedTurn | None:
    items = [i.model_copy(deep=True) for i in state.mission_items]
    cart = await _cart_or_none(adapter, cart_id)
    newly = sync_items_with_cart(items, cart)
    guided = [i for i in items if i.guided]
    cur = current_stage(items)
    prefix = ""
    if newly:
        confirmed = await _confirmed_lines(adapter, cart, newly)
        prefix = ("Confirmed in your cart: " + "; ".join(confirmed) + ". ") if confirmed else ""

    if command == "add_all":
        # "add these" does not say WHICH product for the open stage, and a
        # mission never adds everything at once: each stage's product, size
        # and approval are the shopper's own. Ask a focused question instead
        # of claiming failure or adding anything.
        if cur is None:
            return None
        new_state = await _persist(state, items)
        prompt, products = await _stage_prompt(
            adapter, cur, lead="I'll take these one stage at a time so each choice is yours. Right now:",
        )
        return GuidedTurn(reply=prefix + prompt, products=products, state=new_state,
                          view_ids=view_product_ids(new_state))

    if command == "skip":
        target = _stage_named(message, guided) or cur
        if target is None:
            return None
        if target.stage == "added":
            reply = f"{target.label} is already in your cart, so there's nothing to skip there."
            new_state = await _persist(state, items)
            return GuidedTurn(reply=prefix + reply, state=new_state, view_ids=view_product_ids(new_state))
        target.stage = "skipped"
        target.status = "done"
        new_state = await _persist(state, items)
        nxt = current_stage(items)
        if nxt is None:
            text = f"Skipped {target.label}. {await _summary(adapter, cart, items)}"
            return GuidedTurn(reply=prefix + text, state=new_state, view_ids=[])
        prompt, products = await _stage_prompt(adapter, nxt)
        return GuidedTurn(
            reply=f"{prefix}Skipped {target.label}. {prompt}", products=products,
            state=new_state, view_ids=view_product_ids(new_state),
        )

    if command == "budget":
        budget = _parse_budget(message)
        target = _stage_named(message, guided)
        if target is None or target.stage != "pending":
            target = cur
        if budget is None or target is None:
            return None
        res = await stage_candidates(
            adapter, target.search_query or target.label, activity=None, budget=budget,
            message=message, memory_facts=memory_facts,
        )
        if res.unavailable:
            return GuidedTurn(
                reply=prefix + "I can't reach the shop to re-check prices right now - your options are unchanged.",
                state=state, view_ids=view_product_ids(state),
            )
        if not res.candidate_ids:
            return GuidedTurn(
                reply=(prefix + f"I don't have any {target.label} under {budget} in stock, so I've kept "
                       "your earlier options. You can raise the limit or skip this one."),
                state=state, view_ids=view_product_ids(state),
            )
        target.candidates = res.candidate_ids
        target.candidate_titles = res.candidate_titles
        target.sold_out_titles = res.sold_out_titles
        target.constraints = [c for c in target.constraints if "under" not in c.lower()] + [f"under {budget}"]
        new_state = await _persist(state, items)
        prompt, products = await _stage_prompt(
            adapter, target, lead=f"Updated for a budget under {budget} -",
        )
        return GuidedTurn(
            reply=prefix + prompt, products=products, state=new_state,
            view_ids=view_product_ids(new_state),
        )

    # "next" / status: re-present the open stage, or the summary when finished.
    new_state = await _persist(state, items)
    if cur is None:
        return GuidedTurn(reply=prefix + await _summary(adapter, cart, items), state=new_state, view_ids=[])
    lead = "Still to do:" if any(i.stage != "pending" for i in guided) else _LEAD_FIRST
    prompt, products = await _stage_prompt(adapter, cur, lead=lead)
    return GuidedTurn(reply=prefix + prompt, products=products, state=new_state,
                      view_ids=view_product_ids(new_state))


# --------------------------------------------------------------------------
# After ANY turn / tap: advance when the cart now holds a stage's product.
# --------------------------------------------------------------------------

async def advance_from_cart(
    *, adapter, session_id: str, connection_id: str, cart_id: str | None,
) -> tuple[str, list] | None:
    """If the real cart now satisfies one or more open stages, persist that and
    return `(text, next stage's products)`; `None` when nothing changed (no
    mission, nothing newly in the cart, or the cart could not be read)."""
    row = await shopping_state_store.get(session_id, connection_id)
    if not row or not row.get("mission_items"):
        return None
    try:
        items = [MissionItem.model_validate(d) for d in row["mission_items"]]
    except Exception:  # noqa: BLE001 - a malformed row never breaks a turn
        logger.warning("guided_mission: could not read mission_items", exc_info=True)
        return None
    if not pending_stages(items):
        return None
    cart = await _cart_or_none(adapter, cart_id)
    newly = sync_items_with_cart(items, cart)
    if not newly:
        return None
    await shopping_state_store.merge(
        session_id, connection_id, mission_items=[i.model_dump(mode="json") for i in items],
    )
    state = ShoppingStateV2(session_id=session_id, connection_id=connection_id, mission_items=items)
    text, products = await _advance_text(adapter, cart, state, items, newly)
    return text, products
