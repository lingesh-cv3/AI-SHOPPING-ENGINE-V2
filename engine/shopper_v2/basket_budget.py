"""Basket-level budget for guided missions: ONE shared total across every stage.

"I want running shoes, socks and energy gel, total budget 6000": the shopper is told, before each pick, what is left, whether
the WHOLE list fits at today's prices, and which in-stock options in the current stage still fit what is left.

Authority and safety (nothing here is new machinery):

* READ-ONLY. This module never adds to a cart, never builds a proposal, and imports nothing from decision, risk, execution or
  `ActionType`. It reads `adapter.get_cart` / `adapter.get_product` and the prices the guided mission already stored. Every add
  still goes through `fulfillment.start_fulfillment` -> `accept_cart_proposal_core` (live price/stock re-check, atomic claim,
  Decision -> Risk Gate -> Execution -> Adapter). A card shown here is just a product card; the shopper's own tap or typed add
  is the only way it reaches a cart.
* What is committed is ALWAYS recomputed from the REAL cart, never a decremented counter, so a failed or approval-pending add
  can never reduce what is left.
* "Fits" is claimed only when it is known: every open stage has a priced candidate, in one currency, and the cart was readable.
  "Over" is decided from a certain lower bound (committed + the cheapest priced option of each stage) even when other stages
  are unpriced. Anything else is "unknown", said plainly - never a guessed price, never a confident 0.
* Compatibility is three-valued. "incompatible" is emitted ONLY for a pair the merchant declared incompatible; today no data
  source supplies that, so it is unreachable in production (said honestly). Absence of a declared relationship is "unknown".
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from engine.execution.product_relations import check_compatibility

logger = logging.getLogger(__name__)

#: How many alternatives a stage offers, and how many stored-cheapest candidates are re-fetched live to find them.
_MAX_ALTERNATIVES = 3
_LIVE_CHECK_LIMIT = 6

# --------------------------------------------------------------------------
# Parsing: ONE reading of a budget clause, shared by the guided mission.
# --------------------------------------------------------------------------

_CUR = r"(?:rs\.?|inr|₹|\$|usd|rupees?|dollars?)?"
_AMT = r"([\d][\d,]*(?:\.\d+)?)\s*(k)?\b"
#: "total budget of 8000", "with a budget of Rs 8000", "budget is 8000"
_BUDGET_OF = re.compile(
    rf"(?:\b(?:with|and|having|within|on)\s+)?(?:\b(?:a|my|our|the)\s+)?\b(?:total\s+|overall\s+)?budget\s*(?:of|is|:|=|at)?\s*{_CUR}\s*{_AMT}",
    re.IGNORECASE,
)
#: "under 5000", "no more than 5000", "up to 5000"
_UNDER = re.compile(
    rf"\b(?:under|below|less than|within|up to|upto|max(?:imum)?|no more than|not more than)\s*{_CUR}\s*{_AMT}",
    re.IGNORECASE,
)
#: "8000 in all", "8000 rupees in total", "for 8000 altogether"
_N_TOTAL = re.compile(
    rf"(?:\b(?:for|with)\s+)?{_CUR}\s*{_AMT}\s*(?:rupees|rs|inr|dollars|usd)?\s*\b(?:in total|in all|altogether|all together|overall|total)\b",
    re.IGNORECASE,
)
_TOTAL_WORDS = re.compile(
    r"\b(?:total|overall|altogether|in all|in total|whole (?:list|thing|basket)|everything|all together|basket)\b|\bbudget (?:of|is)\b",
    re.IGNORECASE,
)
_CLEAR = re.compile(
    r"\b(?:no budget|without (?:a )?budget|(?:forget|ignore|drop|remove|clear|cancel) (?:the |my )?budget|budget (?:doesn'?t|does not|no longer) matter)\b",
    re.IGNORECASE,
)


def _currency_of(text: str) -> str | None:
    low = text.lower()
    if "₹" in text or re.search(r"\b(?:rs\.?|inr|rupees?)\b", low):
        return "INR"
    if "$" in text or re.search(r"\b(?:usd|dollars?)\b", low):
        return "USD"
    return None


def _amount(m: re.Match) -> Decimal | None:
    try:
        value = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    value = value * 1000 if m.group(2) else value
    return value if value > 0 else None


def strip_budget_clause(message: str, trailing_only: bool = False) -> tuple[str, Decimal | None, str | None]:
    """`(message without the budget clause, amount, currency)`; the message is returned unchanged when there is no clause.
    The clause is cut BEFORE a mission's stages are split, so "...gel under 5000" is still three stages and never a stage
    called "gel under" or "total budget". With `trailing_only`, a clause in the MIDDLE of the sentence ("shoes under 5000 and
    socks": a per-item cap) is left alone unless it says it is a total."""
    text = message or ""
    for rx in (_BUDGET_OF, _UNDER, _N_TOTAL):
        m = rx.search(text)
        if m:
            amount = _amount(m)
            if amount is None:
                continue
            if amount < 10 and _currency_of(m.group(0)) is None:
                continue  # "2 in all", "3 total" are quantities, not a budget
            if trailing_only and text[m.end():].strip(" .!?,;") and not _TOTAL_WORDS.search(m.group(0)):
                continue
            cleaned = (text[: m.start()] + " " + text[m.end():]).strip(" ,;")
            cleaned = re.sub(r"\s+", " ", cleaned)
            cleaned = re.sub(r"(?:,|\band|\bwith|\bfor)\s*$", "", cleaned, flags=re.IGNORECASE).strip(" ,;")
            return cleaned, amount, _currency_of(m.group(0))
    return text, None, None


def parse_budget(message: str) -> Decimal | None:
    """The amount in any budget clause ("under 5000", "budget of 8000"), else None."""
    return strip_budget_clause(message)[1]


def is_total_phrase(message: str) -> bool:
    """Does the shopper cap the WHOLE list ("total budget 6000", "6000 in all"), as opposed to a bare "under 500"?"""
    return bool(_TOTAL_WORDS.search(message or "")) and parse_budget(message) is not None


def is_clear_phrase(message: str) -> bool:
    return bool(_CLEAR.search(message or ""))


def money_text(amount: Decimal | None, currency: str | None) -> str:
    if amount is None:
        return "an unknown amount"
    shown = f"{amount:.0f}" if amount == amount.to_integral() else f"{amount:.2f}"
    return f"{shown} {currency}".strip() if currency else shown


# --------------------------------------------------------------------------
# Prices a stage carries (captured where candidates are already fetched).
# --------------------------------------------------------------------------

def price_record(products: list) -> tuple[list[str | None], str | None]:
    """`(prices aligned to `products`, the one currency)` - or all-None and no currency when the stage's prices disagree on
    currency (a total over mixed currencies is meaningless; the estimate then says so)."""
    prices: list[str | None] = []
    currencies: set[str] = set()
    for p in products:
        price = getattr(p, "price", None)
        if price is None or getattr(price, "amount", None) is None:
            prices.append(None)
            continue
        prices.append(str(price.amount))
        currencies.add(str(price.currency))
    if len(currencies) > 1:
        return [None] * len(products), None
    return prices, (next(iter(currencies)) if currencies else None)


def _decimals(strings: list[str | None]) -> list[Decimal]:
    out: list[Decimal] = []
    for s in strings:
        if not s:
            continue
        try:
            out.append(Decimal(str(s)))
        except InvalidOperation:
            continue
    return out


# --------------------------------------------------------------------------
# Estimate
# --------------------------------------------------------------------------

@dataclass
class BasketEstimate:
    budget: Decimal | None = None
    currency: str | None = None
    #: sum of the REAL cart's line totals; None = unknown (cart unreadable / currencies disagree), 0 = known empty
    committed: Decimal | None = None
    remaining: Decimal | None = None
    #: "none" (no budget stated) | "fits" | "tight" | "over" | "unknown"
    verdict: str = "none"
    reason: str | None = None
    unknown_stages: list[str] = field(default_factory=list)
    lows: dict[str, Decimal] = field(default_factory=dict)
    typicals: dict[str, Decimal] = field(default_factory=dict)
    sum_lows: Decimal | None = None


def estimate(*, budget: Decimal | None, budget_currency: str | None, cart, cart_state: str, items: list) -> BasketEstimate:
    """The honest answer to "does the whole list fit?". `cart_state` is "none" (no cart id: committed is a known 0),
    "ok" (`cart` was read) or "unreadable" (the read failed: committed is unknown, never 0)."""
    est = BasketEstimate(budget=budget, currency=budget_currency)
    if budget is None:
        return est
    pending = [i for i in items if getattr(i, "guided", False) and getattr(i, "stage", None) == "pending"]
    observed: set[str] = set()
    taken = committed_ids(cart if cart_state == "ok" else None, items)
    for item in pending:
        prices = _decimals([p for pid, p in zip(item.candidates, item.candidate_prices) if pid not in taken])
        if prices and item.price_currency:
            observed.add(str(item.price_currency))
            est.lows[item.item_id] = min(prices)
            est.typicals[item.item_id] = Decimal(str(statistics.median(prices)))
        else:
            est.unknown_stages.append(item.label)

    committed: Decimal | None
    if cart_state == "none":
        committed = Decimal(0)
    elif cart_state == "ok" and cart is not None:
        lines = list(cart.lines)
        line_currencies = {str(ln.line_total.currency) for ln in lines}
        if len(line_currencies) > 1:
            committed, est.reason = None, "your cart has more than one currency"
        else:
            committed = sum((Decimal(str(ln.line_total.amount)) for ln in lines), Decimal(0))
            observed |= line_currencies
    else:
        committed, est.reason = None, "I couldn't read your cart just now"
    est.committed = committed

    if budget_currency:
        observed.add(budget_currency)
    if len(observed) > 1:
        est.currency, est.committed, est.reason = None, None, "the prices and your budget are in different currencies"
        committed = None
    elif observed:
        est.currency = next(iter(observed))

    if committed is None:
        est.verdict = "unknown"
        return est
    est.remaining = budget - committed
    est.sum_lows = sum(est.lows.values(), Decimal(0))
    lower_bound = committed + est.sum_lows
    if lower_bound > budget:
        est.verdict = "over"
        return est
    if est.unknown_stages:
        est.verdict = "unknown"
        est.reason = est.reason or f"I have no price for {', '.join(est.unknown_stages)}"
        return est
    typical_total = committed + sum(est.typicals.values(), Decimal(0))
    est.verdict = "fits" if typical_total <= budget else "tight"
    return est


def left_for_stage(est: BasketEstimate, item_id: str) -> Decimal | None:
    """What this stage can spend while still leaving the cheapest option of every OTHER open stage."""
    if est.budget is None or est.committed is None:
        return None
    reserve = sum((low for k, low in est.lows.items() if k != item_id), Decimal(0))
    return est.budget - est.committed - reserve


def verdict_text(est: BasketEstimate) -> str:
    """Deterministic, short, honest. Never model-written."""
    if est.verdict == "none":
        return ""
    cur = est.currency
    if est.verdict == "over":
        return (f"Heads up: going by the options I found at today's prices, this list won't fit your {money_text(est.budget, cur)} budget - "
                f"{money_text(est.committed, cur)} is already committed and the cheapest I found for each remaining stage "
                f"adds about {money_text(est.sum_lows, cur)}.")
    if est.verdict == "unknown":
        why = est.reason or "I can't price every stage"
        return f"I can't tell yet whether the whole list fits your {money_text(est.budget, cur)} budget: {why}."
    if est.committed == 0:
        left = f"nothing is in your cart yet, so all of your {money_text(est.budget, cur)} budget is available"
    else:
        left = f"about {money_text(est.remaining, cur)} of your {money_text(est.budget, cur)} budget is left"
    if est.verdict == "tight":
        return f"It can fit, but only if you lean toward the cheaper options: {left} (line totals, before delivery and tax)."
    return (f"Good so far - {left}, and the rest of the list should fit, going by the options I found at today's prices "
            "(line totals, before delivery and tax).")


# --------------------------------------------------------------------------
# Compatibility (three-valued; no invented incompatibility)
# --------------------------------------------------------------------------

def compat_status(a, b, declared_incompatible: frozenset[str] = frozenset()) -> str:
    """"compatible" iff the merchant declared the pair compatible (either direction); "incompatible" ONLY for a pair the
    merchant explicitly declared incompatible; otherwise "unknown" - absence of data is never a verdict against."""
    if b.product_id in declared_incompatible or a.product_id in declared_incompatible:
        return "incompatible"
    return "compatible" if check_compatibility(a, b).verdict == "compatible" else "unknown"


def compat_counts(added: list, shown: list) -> dict[str, int]:
    counts = {"compatible": 0, "unknown": 0, "incompatible": 0}
    for a in added:
        for b in shown:
            if a.product_id == b.product_id:
                continue
            counts[compat_status(a, b)] += 1
    return counts


def compat_line(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if total == 0:
        return ""
    if counts["compatible"] == 0:
        return ("Compatibility with what's in your cart isn't confirmed either way for these options - "
                "that's missing data, not a sign of a problem.")
    return (f"The merchant confirms {counts['compatible']} of these options work with what's in your cart; "
            f"the other {counts['unknown']} aren't confirmed either way (missing data, not a problem).")


# --------------------------------------------------------------------------
# The view: reads the real cart and prices; returns text + alternatives + a trace record. Never writes.
# --------------------------------------------------------------------------

@dataclass
class BudgetView:
    estimate: BasketEstimate
    #: cards for the current stage's in-budget alternatives (read-only suggestions; the shopper adds them the usual way)
    alternatives: list = field(default_factory=list)
    alternatives_text: str = ""
    compat_text: str = ""
    compat_counts: dict = field(default_factory=lambda: {"compatible": 0, "unknown": 0, "incompatible": 0})
    exclude_ids: set = field(default_factory=set)
    duplicates_filtered: int = 0

    def trace(self) -> dict:
        e = self.estimate
        return {
            "budget": str(e.budget) if e.budget is not None else None,
            "currency": e.currency,
            "committed": str(e.committed) if e.committed is not None else None,
            "remaining": str(e.remaining) if e.remaining is not None else None,
            "verdict": e.verdict,
            "unknown_stages": list(e.unknown_stages),
            "alternatives_offered_ids": [p.product_id for p in self.alternatives],
            "compat_pairs": dict(self.compat_counts),
            "duplicates_filtered": self.duplicates_filtered,
        }


def committed_ids(cart, items: list) -> set[str]:
    """Products already in the REAL cart or chosen for some stage: never offered again."""
    ids = {ln.product_id for ln in cart.lines} if cart is not None else set()
    ids |= {i.added_product_id for i in items if getattr(i, "added_product_id", None)}
    return ids


async def build_budget_view(
    adapter, *, budget: Decimal | None, budget_currency: str | None, items: list, cart, cart_state: str,
    current=None, shown_products: list | None = None,
) -> BudgetView:
    """Everything the guided replies need about money, from fresh reads. `current` is the open stage the shopper is about to
    choose in; `shown_products` are the products on display for it (used for compatibility and to decide whether an
    alternative is warranted). Failures degrade to a quieter view, never an exception."""
    est = estimate(budget=budget, budget_currency=budget_currency, cart=cart, cart_state=cart_state, items=items)
    view = BudgetView(estimate=est, exclude_ids=committed_ids(cart, items))

    # compatibility: what is already in the cart (bounded) against what is on display for the open stage
    if shown_products and cart is not None and cart.lines:
        added = []
        for pid in [ln.product_id for ln in cart.lines][:6]:
            try:
                added.append(await adapter.get_product(pid))
            except Exception:  # noqa: BLE001 - missing product data is "unknown", not a failure
                logger.warning("basket_budget: get_product failed for %r", pid, exc_info=True)
        view.compat_counts = compat_counts(added, shown_products)
        view.compat_text = compat_line(view.compat_counts)

    # budget-aware alternatives for the open stage
    if budget is not None and current is not None and est.verdict in ("over", "tight", "fits", "unknown"):
        left = left_for_stage(est, current.item_id)
        if left is not None and est.currency:
            shown_prices = [p.price.amount for p in (shown_products or []) if getattr(p, "price", None) is not None]
            warranted = est.verdict in ("over", "tight") or any(Decimal(str(x)) > left for x in shown_prices)
            if warranted:
                stored = list(zip(current.candidates, current.candidate_prices))
                priced = sorted(((pid, Decimal(price)) for pid, price in stored if price and pid not in view.exclude_ids),
                                key=lambda t: t[1])
                offered, live_checked = [], 0
                for pid, _stored_price in priced[:_LIVE_CHECK_LIMIT]:
                    live_checked += 1
                    try:
                        product = await adapter.get_product(pid)
                    except Exception:  # noqa: BLE001 - an unfetchable product is never offered
                        logger.warning("basket_budget: live check failed for %r", pid, exc_info=True)
                        continue
                    price = getattr(product, "price", None)
                    in_stock = _buyable(product)
                    if (price is not None and str(price.currency) == est.currency and in_stock
                            and Decimal(str(price.amount)) <= left and product.product_id not in view.exclude_ids):
                        offered.append(product)
                    if len(offered) >= _MAX_ALTERNATIVES:
                        break
                view.alternatives = offered
                if offered:
                    listed = "; ".join(f"{p.title} ({p.price.amount} {p.price.currency})" for p in offered)
                    view.alternatives_text = f"Within what's left for {current.label} ({money_text(left, est.currency)}): {listed}."
                elif left <= 0 or est.verdict in ("over", "tight"):
                    view.alternatives_text = (f"None of the in-stock {current.label} options I found fit within what's left "
                                              f"({money_text(max(left, Decimal(0)), est.currency)}) - you can skip this one, "
                                              "raise the budget, or choose anyway.")
    return view


def _buyable(product) -> bool:
    variants = list(getattr(product, "variants", None) or [])
    if variants:
        return any(str(v.availability) != "OUT_OF_STOCK" for v in variants)
    return str(getattr(product, "availability", "UNKNOWN")) != "OUT_OF_STOCK"
