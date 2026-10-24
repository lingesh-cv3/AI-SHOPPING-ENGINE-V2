"""Cross-stage compatibility and complete-the-set for guided missions.

"I want shoes, socks and something for energy, total budget 9000": once a product is really in the cart, the shopper is told which of
the options on screen the MERCHANT lists as going with it, which option in a LATER stage it is paired with, and - clearly labelled as
optional and not on their list - which merchant-declared add-ons complete what they chose, with what each adds to the basket and what
it leaves of the budget.

The only evidence is the merchant's own declared `Product.compatible_with` list (either direction), through
`engine.execution.product_relations`. Category, title words, price band and search similarity are NOT evidence: nothing in this module
reads them. There are exactly two user-visible states, "the merchant lists X as going with Y" and silence/"not confirmed"; there is no
"incompatible" (no data source supplies it, and a state that can only fire from absence would be a false claim).

READ-ONLY. This module never adds to a cart, builds a proposal, or touches Decision, Risk, Execution or payment, and imports nothing
from them (only `product_relations`). It reads `adapter.get_product` and takes the cart the caller already read. A product is only
offered if it was fetched live THIS turn, is buyable and, when a budget exists, priced in the budget's currency; the shopper's own tap
or typed add then goes through `fulfillment.start_fulfillment` -> `accept_cart_proposal_core` (live price/stock re-check, atomic claim,
Decision -> Risk Gate -> Execution -> Adapter) exactly as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from engine.execution.product_relations import check_compatibility, complementary_product_ids

logger = logging.getLogger(__name__)

# Bounds, so the reads stay constant whatever the catalogue size.
_MAX_ANCHORS = 6
_MAX_COMPLEMENT_FETCHES = 12
_MAX_PARTNER_FETCHES = 12
_MAX_OTHER_STAGES = 3
_MAX_EXTRAS = 2
_MAX_NAMED = 4


def pair_status(a, b) -> str:
    """"confirmed" only when the merchant declared the pair (either direction); otherwise "unknown". Never anything worse."""
    return "confirmed" if check_compatibility(a, b).verdict == "compatible" else "unknown"


def _buyable(product) -> bool:
    variants = list(getattr(product, "variants", None) or [])
    if variants:
        return any(str(v.availability) != "OUT_OF_STOCK" for v in variants)
    return str(getattr(product, "availability", "UNKNOWN")) != "OUT_OF_STOCK"


def _price(product) -> tuple[Decimal | None, str | None]:
    price = getattr(product, "price", None)
    if price is None or getattr(price, "amount", None) is None:
        return None, None
    return Decimal(str(price.amount)), str(price.currency)


def _offer_price(product) -> tuple[Decimal | None, str | None, bool]:
    """The price an add-on can honestly be quoted at: (amount, currency, varies).

    The shopper picks the option AFTER the add-on is announced, so with variants the quote comes from the buyable options, never from the
    product-level price: uniform -> that price; differing -> the lowest ("from", varies=True); a buyable option with no usable price (its own or the product's) or a
    mixed currency -> unknown (None). No variants -> the product's own price.
    """
    buyable = [v for v in (getattr(product, "variants", None) or []) if str(v.availability) != "OUT_OF_STOCK"]
    if not buyable:
        price, cur = _price(product)
        return price, cur, False
    quotes = []
    for v in buyable:
        vp = getattr(v, "price", None)
        if vp is None or getattr(vp, "amount", None) is None:
            vp = getattr(product, "price", None)  # a variant with no price of its own inherits the product's (the adapter contract)
        if vp is None or getattr(vp, "amount", None) is None:
            return None, None, False
        quotes.append((Decimal(str(vp.amount)), str(vp.currency)))
    if len({c for _a, c in quotes}) != 1:
        return None, None, False
    low = min(a for a, _c in quotes)
    return low, quotes[0][1], any(a != low for a, _c in quotes)


def _money(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "no listed price"
    shown = f"{amount:.0f}" if amount == amount.to_integral() else f"{amount:.2f}"
    return f"{shown} {currency}".strip() if currency else shown


@dataclass
class ComplementView:
    #: sentences for the stage prompt / closing paragraph (empty string = silence)
    label_text: str = ""
    forward_text: str = ""
    stage_fill_text: str = ""
    alt_text: str = ""
    extras_text: str = ""
    closing_text: str = ""
    #: products to show as cards (the extras), already live-checked; the shopper adds one the usual way
    extras: list = field(default_factory=list)
    #: how many named "the merchant lists" pairs between the cart and the shown options (equals compat_counts()["compatible"])
    named_pairs: int = 0
    confirmed_cart_pairs: int = 0
    trace: dict = field(default_factory=dict)

    def prompt_text(self) -> str:
        return " ".join(x for x in (self.label_text, self.forward_text, self.stage_fill_text, self.alt_text, self.extras_text) if x)


async def count_confirmed_pairs(adapter, cart) -> int:
    """Pairs of products really in the cart that the merchant lists as going together (bounded to the first 6 lines)."""
    if cart is None or not cart.lines:
        return 0
    products = []
    for pid in _unique([ln.product_id for ln in cart.lines])[:_MAX_ANCHORS]:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - unreadable product data is "unknown", never a failure
            logger.warning("mission_complements: get_product failed for %r", pid, exc_info=True)
    return sum(1 for i, a in enumerate(products) for b in products[i + 1:] if pair_status(a, b) == "confirmed")


def _unique(ids):
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


async def build_complement_view(
    adapter, *, items: list, cart, cart_state: str, est, exclude_ids: set, offered_ids: set,
    current=None, shown: list | None = None, alternatives: list | None = None, priority_ids: list | None = None,
    include_extras: bool = False, closing: bool = False,
) -> ComplementView:
    """Everything the guided replies say about what goes with what, from the merchant's declared data and this turn's live reads.
    Never raises; every failure degrades to silence with a reason in the trace."""
    view = ComplementView()
    trace: dict = {"anchors": [], "labels": [], "forward_pairs": [], "stage_fill": [], "extras_offered_ids": [],
                   "cheaper_alternatives_noted": [], "reason": None}
    view.trace = trace
    cache: dict = {}
    fetched = {"n": 0}

    async def get(pid):
        if pid in cache:
            return cache[pid]
        fetched["n"] += 1
        try:
            product = await adapter.get_product(pid)
        except Exception:  # noqa: BLE001 - skipped, never invented
            logger.warning("mission_complements: get_product failed for %r", pid, exc_info=True)
            product = None
        cache[pid] = product
        return product

    try:
        # Anchors are products REALLY in the cart this turn. An unreadable or empty cart gives none (never state-only anchors, never a
        # guess). Forward pairing between open stages needs no anchor, so it still runs.
        anchors: list = []
        if cart_state != "ok" or cart is None:
            trace["reason"] = "cart_unreadable" if cart_state == "unreadable" else "no_anchors"
        else:
            line_ids = _unique([ln.product_id for ln in cart.lines])
            if not line_ids:
                trace["reason"] = "no_anchors"
            else:
                ordered = _unique([*(priority_ids or []), *line_ids])
                ordered = [i for i in ordered if i in set(line_ids)][:_MAX_ANCHORS]
                anchors = [p for p in [await get(pid) for pid in ordered] if p is not None]
                if not anchors:
                    trace["reason"] = "anchor_fetch_failed"
        trace["anchors"] = [a.product_id for a in anchors]
        anchor_ids = {a.product_id for a in anchors}
        shown = [p for p in (shown or []) if p.product_id not in anchor_ids]

        # ---- 1. which shown options the merchant lists as going with something already in the cart
        pairs = [(s, a) for s in shown for a in anchors if pair_status(a, s) == "confirmed"]
        view.named_pairs = len(pairs)
        trace["labels"] = [{"option": s.product_id, "anchor": a.product_id} for s, a in pairs]
        if pairs:
            named = "; ".join(f"{s.title} as going with your {a.title}" for s, a in pairs[:_MAX_NAMED])
            more = f" (+{len(pairs) - _MAX_NAMED} more)" if len(pairs) > _MAX_NAMED else ""
            view.label_text = f"The merchant lists {named}{more}."

        # ---- 2. forward pairing and stage-fill against OTHER open stages' options
        if current is not None:
            others = [i for i in items if getattr(i, "guided", False) and i.stage == "pending" and i.item_id != current.item_id][:_MAX_OTHER_STAGES]
            partners: list[tuple[object, list]] = []
            budget_left = _MAX_PARTNER_FETCHES
            for stage in others:
                ids = [c for c in stage.candidates if c not in exclude_ids][:4][:budget_left]
                budget_left -= len(ids)
                prods = [p for p in [await get(pid) for pid in ids] if p is not None and _buyable(p)]
                partners.append((stage, prods))
            fwd, fill = [], []
            for stage, prods in partners:
                for y in prods:
                    for x in shown:
                        if pair_status(x, y) == "confirmed":
                            fwd.append((x, y, stage))
                    for a in anchors:
                        if pair_status(a, y) == "confirmed":
                            fill.append((y, a, stage))
            trace["forward_pairs"] = [{"option": x.product_id, "later": y.product_id, "stage": s.label} for x, y, s in fwd]
            trace["stage_fill"] = [{"option": y.product_id, "anchor": a.product_id, "stage": s.label} for y, a, s in fill]
            if fwd:
                view.forward_text = " ".join(
                    f"{x.title} is also listed by the merchant as going with {y.title}, an option for your {s.label} stage."
                    for x, y, s in fwd[:2])
            if fill:
                y, a, s = fill[0]
                view.stage_fill_text = f"For your {s.label} stage, the merchant lists {y.title} as going with your {a.title}."

        # ---- 3. cheaper alternatives: a saving only for a live-checked alternative, and honest about compatibility
        if anchors and alternatives and shown and est is not None and est.budget is not None and est.currency and current is not None:
            left = None
            if est.committed is not None:
                reserve = sum((low for k, low in est.lows.items() if k != current.item_id), Decimal(0))
                left = est.budget - est.committed - reserve
            ref = next((s for s in shown if _price(s)[0] is not None and left is not None and _price(s)[0] > left
                        and _price(s)[1] == est.currency), None)
            notes = []
            if ref is not None:
                for alt in alternatives[:2]:
                    ap, ac = _price(alt)
                    rp = _price(ref)[0]
                    if ap is None or ac != est.currency or rp is None or rp <= ap:
                        continue
                    anchor = anchors[0]
                    stance = (f"the merchant lists it as going with your {anchor.title}" if pair_status(anchor, alt) == "confirmed"
                              else f"the merchant hasn't confirmed it goes with your {anchor.title}")
                    notes.append(f"{alt.title} ({_money(ap, ac)}) is {_money(rp - ap, ac)} less than {ref.title}; {stance}.")
                    trace["cheaper_alternatives_noted"].append(alt.product_id)
            view.alt_text = " ".join(notes)

        # ---- 4. optional extras: merchant-declared complements of the anchors, not in the cart, not on any open stage
        if include_extras:
            # Never an option of ANY stage on the shopper's list, open or closed: an option of a stage they already satisfied with another
            # product (or skipped) is a substitute for what they chose, not something that completes it.
            stage_candidates = {c for i in items if getattr(i, "guided", False) for c in i.candidates}
            wanted: dict[str, list[str]] = {}
            for a in anchors:
                for pid in complementary_product_ids(a):
                    if pid in anchor_ids or pid in exclude_ids or pid in stage_candidates or pid in offered_ids:
                        continue
                    wanted.setdefault(pid, [])
                    if a.title not in wanted[pid]:
                        wanted[pid].append(a.title)
            extras, texts = [], []
            extra_fetches = 0
            for pid, cites in wanted.items():
                if len(extras) >= _MAX_EXTRAS or extra_fetches >= _MAX_COMPLEMENT_FETCHES:
                    break
                if pid not in cache:
                    extra_fetches += 1
                product = await get(pid)
                if product is None or not _buyable(product):
                    continue
                price, cur, varies = _offer_price(product)
                if est is not None and est.budget is not None and (price is None or cur != est.currency):
                    continue  # with a budget in play, an extra we cannot price in that currency is not offered
                cite = " and ".join(f"your {t}" for t in cites[:2])
                shown = f"from {_money(price, cur)}, price depends on the option" if varies else _money(price, cur)
                text = f"Optional extra, not on your list - the merchant lists {product.title} ({shown}) as going with {cite}."
                if est is not None and est.budget is not None and est.committed is not None and price is not None and est.remaining is not None:
                    after = est.remaining - price
                    if varies:
                        text += f" It adds at least {_money(price, cur)}; your remaining budget would go from {_money(est.remaining, est.currency)} to at most {_money(after, est.currency)}."
                    else:
                        text += f" It adds {_money(price, cur)}; your remaining budget would go from {_money(est.remaining, est.currency)} to {_money(after, est.currency)}."
                    sum_lows = est.sum_lows if est.sum_lows is not None else Decimal(0)
                    if after - sum_lows < 0:
                        text += (f" That would leave too little for the rest of your list (the cheapest options I found still need "
                                 f"about {_money(sum_lows, est.currency)}).")
                extras.append(product)
                texts.append(text)
            view.extras = extras
            view.extras_text = " ".join(texts)
            trace["extras_offered_ids"] = [p.product_id for p in extras]

        # ---- 5. closing paragraph: confirmed pairs actually in the cart, or one honest sentence when there are none
        if closing:
            cart_pairs = [(a, b) for i, a in enumerate(anchors) for b in anchors[i + 1:] if pair_status(a, b) == "confirmed"]
            view.confirmed_cart_pairs = len(cart_pairs)
            bits = []
            if cart_pairs:
                named = "; ".join(f"{b.title} as going with your {a.title}" for a, b in cart_pairs[:3])
                bits.append(f"The merchant lists {named}.")
            declared_anywhere = any(complementary_product_ids(a) for a in anchors)
            if anchors and not cart_pairs and not declared_anywhere:
                trace["reason"] = "no_declared_relationships"
                bits.append("I found no merchant-confirmed pairings for what's in your cart, so I'm not suggesting extras.")
            view.closing_text = " ".join(bits)
        if not (pairs or view.forward_text or view.stage_fill_text or view.extras or view.closing_text):
            trace["reason"] = trace["reason"] or "no_declared_relationships"
    except Exception:  # noqa: BLE001 - a suggestion view never breaks a mission turn
        logger.warning("mission_complements: view failed", exc_info=True)
        trace["reason"] = "view_failed"
        return ComplementView(trace=trace)
    return view
