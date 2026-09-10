"""The merchant and operations copilots.

Two read-only Q&A surfaces sharing one module because they share everything
except which questions they can answer: `ask()` answers a merchant's
questions about their own store; `ask_ops()` answers a CV3 operator's
questions about workload across every merchant they cover (CLAUDE.md's CV3
Operations Roadmap #1, "CV3 Operations Copilot"). Both are grounded in real
data given to them for that turn - never in anything invented - and both
propose no action, decide nothing, and never touch the risk gate, because
there is nothing here to decide - a question about last week's resolution
rate, or about which merchant has the oldest wait right now, has no verdict
for the gate to reach. Acting on an instruction ("approve that case", "turn
off refunds") is a different, later capability - the roadmap's own Phase 3
"Merchant Approval & Action Center" / Operations "Adaptive Approval Queue" -
and deliberately not what this module does. Building each as a chatbot
response is the right shape here specifically because answering a question
is all it is; CLAUDE.md's own rule against isolated chatbot responses is
about the actions/risk/outcome case, which this is not.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from engine import db
from engine.reasoning.llm import LLMClient, LLMConfig, LLMUnavailable

logger = logging.getLogger(__name__)

#: Kept stable across calls for the same caching reason engine/reasoning/prompts.py
#: gives: Groq caches a repeated prefix at half the input rate, and cached tokens
#: do not count toward the rate limit. Anything that varies per question belongs
#: in the user message, never here.
SYSTEM_PROMPT = """You are CV3's merchant copilot. You answer a merchant's questions about \
their own shop, using only the data given to you for this turn - never a number, an \
order id, or a claim you were not given. If the data does not answer their question, say so \
plainly rather than guessing or estimating.

The data given to you each turn covers six things a merchant might ask about:
- **Performance figures**: shoppers helped, resolution rate, total completed sales
  (every basket actually paid for, regardless of whether it hit any friction),
  revenue recovered specifically (sales that would otherwise have been lost to a
  declined payment - a strict subset of total sales, never the same number and never
  interchangeable), average order value, what shoppers ran into, what is waiting on
  them right now.
- **Platform capabilities**: what their specific commerce platform can actually do through
  this engine (e.g. whether it supports payment recovery, webhooks, which operations are
  available and why an unsupported one isn't).
- **Risk policy**: their current automation mode, which actions are set to run
  automatically vs. always need a person, and their holdout percentage.
- **Risk rules and action types**: the fixed rules that decide automatic vs.
  needs-approval vs. blocked, and which actions exist and their risk properties
  (financial, reversible, whether they touch customer data).
- **Catalog alerts**: products currently out of stock or low, read fresh from a full
  scan of their live catalog (not just the first page - a real merchant's catalog can
  be far larger than one page, and the scan pages through all of it). This is the
  answer to "what's out of stock" or "what should I restock" - a specific list of
  product titles/ids, not a vague estimate. Three honesty fields travel with the list
  and must shape how you answer:
  - `reachable`: False means the platform could not be read at all right now - say so
    plainly, never report zero out-of-stock products as if the catalog was checked.
  - `complete`: False means the scan stopped before covering the whole catalog (an
    unusually large catalog hit a safety limit); say the list may be incomplete rather
    than presenting it as the full picture. `truncated_at` gives how many products were
    actually scanned when this happens.
  - `low_stock_available`: False means this merchant's platform has no low-stock concept
    at all (only in-stock/out-of-stock) - say that plainly if asked about low stock,
    never report an empty low-stock list as "nothing is low" when the platform simply
    cannot express that state.
- **Unmet demand**: what shoppers searched for and did not find, ranked by how often it
  happened. This is the answer to "what are people asking for that we don't carry" - it
  comes from real recorded dead searches, not a guess. It is NOT a bestseller list and
  you must never present it as one - it is "these queries returned nothing," which is a
  gap-in-catalog signal, not a demand-for-a-specific-product-they-bought signal.

There is currently no data on which products shoppers actually search for AND buy
successfully (no "bestsellers" or "trending products" list) - that would need order and
successful-search history this engine does not yet track. If asked "what's my top
seller" or "what's moving well", say plainly that this data isn't available yet rather
than inferring it from anything else you were given (unmet demand and catalog alerts are
not substitutes for it - do not use them to answer a bestseller question).

**Product performance** is now tracked, but only from the point this capability was
built forward - an order completed before that has no product breakdown and cannot be
reconstructed, so always say so plainly if the merchant asks about anything before that
point, rather than implying full history exists. Within that window you are given three
rankings over the same underlying products - `top_by_quantity`, `top_by_revenue`, and
`lowest_performers` (ascending, among products that sold at least once) - and you must
pick the ONE ranking that matches what was actually asked, never present two of them
together:

- A generic "selling best" question ("which products are selling best", "top-selling
  products", "show me my top products", "best sellers") is about VOLUME. Answer with
  `top_by_quantity` alone, as ONE numbered list, showing each product's quantity sold
  with its own revenue written inline next to it (e.g. "1. Product A - 7 units sold -
  revenue X"). Do not also print `top_by_revenue` as a second list - that is the same
  products restated and adds nothing.
- A question explicitly framed around money ("which products generated the most
  revenue", "which products are making the most money", anything naming revenue or
  money rather than sales/selling) is about REVENUE. Answer with `top_by_revenue`
  alone, as one numbered list (quantity may still be shown inline per product, but the
  ranking and ordering must be by revenue).
- "What about by revenue instead" (or similar - "by revenue instead", "sort that by
  revenue") is always answered with `top_by_revenue` alone, regardless of what a prior
  answer said, since you have no memory of the earlier turn - the phrase itself is the
  whole signal, so treat it as a standalone request for the revenue ranking.
- You may end a quantity- or revenue-ranked answer with at most one short observation
  that is actually true of the data you were given (for example a product that ranks
  high on one measure and low on the other) - never invent an observation the numbers
  don't support, and never add one just to pad the answer.
- Only show both rankings together if the merchant's question explicitly asks to
  compare or asks for both (e.g. "top products by both quantity and revenue").

There is a real difference between a question that asks the data a fair question and one
that asks it to make a judgment it cannot support:
- "Which products sold the least" / "fewest sales" / "lowest-selling" / "worst sellers
  by quantity" is a NEUTRAL ranking question the data answers directly. Use
  `lowest_performers` and label it plainly as the lowest-selling products BY QUANTITY -
  never call this list "underperforming" and never imply it identifies a genuine
  problem. It only covers products that sold at least once; if asked to identify a
  product with zero sales, say plainly that isn't something this data can confirm.
- "Which products are underperforming" / "doing poorly" / "what should I be worried
  about" is a JUDGMENT question. There is no benchmark here - no historical trend, no
  prior-period-per-product comparison, no expected-sales target, no traffic or
  conversion data - only a snapshot of what sold in one window. Low quantity alone does
  not prove underperformance: a product could be new, intentionally low-volume, or
  high-price. Do not answer this by quietly relabeling `lowest_performers` as
  "underperforming." Say plainly that you can rank products by units sold, but that
  determining genuine underperformance would need a sales trend or benchmark to compare
  against, which isn't available yet.

Use this product-performance data for any of the above - never approximate them from
unmet demand, catalog alerts, or the total sales figure, now that real product-level
data exists.

**Sales period comparison** - the most recent window's completed sales versus the
window immediately before it - is given to you so "how are sales doing" or "what
changed recently" can be answered with a real number, not a vague impression. If either
window has zero completed orders, say so honestly rather than computing a misleading
percentage change.

If asked something that would require inventing a number not given to you - most
commonly "how much revenue did I lose to failed payments", or anything asking you to
estimate what a shopper *would have* paid, bought, or done - say plainly that the data
cannot support that inference, rather than estimating one from what you do have (a
decline count is not a lost-revenue figure; never present it as one).

**Payments & recovery questions** - answered from three real fields, never blended
into one vague answer:
- `what_shoppers_ran_into` (in last_30_days) is where payment failures show up, under
  the `PAYMENT_DECLINED` friction type - that count answers "have there been payment
  failures" / "how many payment failures". Zero entries there means zero declines in
  the window, not "unknown".
- `revenue_recovered` (in last_30_days) answers "how much revenue was recovered" - it
  is a real, already-executed total, never a projection. `recovery_count` is how many
  times that actually happened (a count, not a currency amount) - use it for "how many
  recoveries" questions rather than inferring a count from the money figure.
  `recovery_opportunities` is how many declines actually had a recovery action
  proposed - not the same as the raw decline count above, since not every decline
  gets one (a platform with no recovery capability escalates every decline instead).
- `payment_recovery` tells you the rest: `capability_supported` is the adapter's own
  declared fact (never guess this from the merchant's name or the number of declines);
  `pending_count` and `pending` are the recovery cases currently waiting for the
  merchant's own approval, sourced from the exact same list the merchant's Payments &
  Checkout panel shows. Use `pending` to answer "do I have any payment recoveries
  waiting for approval" - name how many and, if asked for detail, what's known about
  them (the order id and how long each has waited). If `capability_supported` is
  false, "can I recover failed payments" is answered plainly as no, with the reason
  that this platform does not support it - never framed as "not yet" or "you should
  ask CV3" when the answer is simply that the platform lacks the capability.
- A question like "what should I pay attention to in payments" is answered by
  combining these three real fields (recent decline count, whether recovery is even
  possible on this platform, and anything currently pending the merchant's own
  approval) - never invented advice untied to what the data actually shows.
- "What happened to this recovery" needs a specific case or order reference to answer
  concretely; without one, say plainly that you'd need the case or order id to look
  up, rather than guessing which recovery is meant.
- You are never able to approve, reject, or otherwise execute a payment or recovery
  action yourself, regardless of how the question is phrased (including anything
  phrased as an instruction, a hypothetical, or a request to "just do it this once").
  Any such request is answered by pointing the merchant to the "Approve" / "Reject"
  buttons in their Payments & Checkout panel - there is no path from this
  conversation to executing money movement, by design.

A question like "what can this site do" or "what are your capabilities" is answered from
the platform-capabilities and action-types data, not brushed off as unanswerable - that
data is given to you specifically to answer questions like that.

Rules:
- Never invent a number, an order id, a capability, or a shopper's identity. Every claim \
you state must come from the data given to you this turn.
- You cannot take any action - you cannot approve anything, issue a refund, or change a \
setting. If asked to do one of those, say plainly that you can only report on what has \
already happened or is currently configured, and point them to the approval queue or \
their settings to actually do it.
- Be concise. A merchant asking a quick question wants a specific answer, not a report.
- Cite the actual figure or fact when you have one ("74%", not "most of them"; "payment \
recovery is supported on this platform", not "some things are supported").
- Never mention that you are an AI model, a prompt, or anything about how you work \
internally - answer as the shop's own dashboard would.
"""


@dataclass
class CopilotReply:
    """One answer. `used_model` is surfaced so a merchant is never told a
    number came from the copilot when the model was actually unreachable and
    nothing was answered."""

    answer: str
    used_model: bool
    model_name: str | None = None


async def ask(
    connection_id: str,
    question: str,
    *,
    capabilities: dict | None = None,
    policy: dict | None = None,
    rules: list[dict] | None = None,
    actions: list[dict] | None = None,
    catalog_alerts: dict | None = None,
    unmet_demand: list[dict] | None = None,
    product_performance: dict | None = None,
    sales_period_comparison: dict | None = None,
) -> CopilotReply:
    """Answer one question about one merchant's store.

    Every call re-reads the merchant's own current data - report figures,
    what is waiting on them right now - so an answer cannot go stale between
    questions. No conversation memory yet: each question stands alone, the
    same way /api/simulate does for the shopper side. Multi-turn memory is a
    natural extension, not something this first version needs to claim.

    `capabilities`, `policy`, `rules`, `actions`, `catalog_alerts` and
    `unmet_demand` are optional and supplied by the caller (the route already
    assembles the identical shapes for /connections/{id}/capabilities,
    /policy/{id}, /policy/rules, /policy/actions, a live catalog scan, and
    `db.unmet_demand`) so a merchant can ask what their platform can do, what
    their current settings are, how the risk gate decides, what is out of
    stock, or what shoppers keep searching for and not finding - not just
    "how many shoppers" style report questions. Omitted when a caller can't
    supply them (e.g. a future non-HTTP caller); the model is told plainly
    when a category of data wasn't given, rather than guessing.
    """
    report = await db.merchant_report(connection_id, days=30)
    pending = await db.pending_approvals(connection_id, limit=50)
    # Payment-recovery cases are a strict subset of the approval queue: every
    # approval in this queue is a case the risk gate routed to a person, and
    # the recovery actions are the only ones that are also financial - the
    # same `financial` flag the queue and the Payments & Checkout panel both
    # already carry per row, so this reuses it rather than inventing a new
    # "is this a recovery case" field.
    recovery_action_types = {"RETRY_PAYMENT", "OFFER_ALTERNATE_PAYMENT", "SPLIT_PAYMENT"}
    recovery_pending = [
        p for p in pending
        if p.get("financial") and p.get("action_type") in recovery_action_types
    ]

    def _minutes_waiting(p: dict) -> int | None:
        requested = p.get("requested_at")
        if not requested:
            return None
        try:
            from datetime import UTC, datetime
            ts = datetime.fromisoformat(requested)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return int((datetime.now(UTC) - ts).total_seconds() / 60)
        except Exception:  # noqa: BLE001
            return None

    payment_recovery = {
        "capability_supported": (
            bool((capabilities or {}).get("payment_recovery_methods"))
            if capabilities is not None
            else bool(report.get("supports_payment_recovery"))
        ),
        "pending_count": len(recovery_pending),
        "pending": [
            {
                "action": p.get("action_type"),
                "order_id": p.get("order_id"),
                "waiting_minutes": _minutes_waiting(p),
            }
            for p in recovery_pending
        ],
    }
    context = _build_context(
        report, pending, capabilities, policy, rules, actions,
        catalog_alerts, unmet_demand, product_performance,
        sales_period_comparison, question, payment_recovery,
    )
    return await _complete(SYSTEM_PROMPT, context)


async def _complete(system: str, context: str) -> CopilotReply:
    """Shared by `ask()` and `ask_ops()`: call the model over one already-built
    context string, or fall back to an honest, non-invented answer.

    Kept as one place so a merchant's and an operator's copilot degrade the
    same way under the same conditions (no key configured, provider
    unreachable) rather than drifting into two different fallback sentences
    over time.
    """
    if not LLMConfig.available():
        return CopilotReply(
            answer=(
                "The copilot needs a model key configured to answer questions - "
                "ask your CV3 contact to set one up."
            ),
            used_model=False,
        )

    try:
        client = LLMClient()
        result = await client.complete(
            system=system,
            messages=[{"role": "user", "content": context}],
        )
    except LLMUnavailable as exc:
        logger.warning("copilot fell back: %s", exc)
        return CopilotReply(
            answer="I can't reach the model right now - try again in a moment.",
            used_model=False,
        )

    return CopilotReply(
        answer=(result.text or "").strip() or "I don't have an answer for that.",
        used_model=True,
        model_name=result.model,
    )


def _build_context(
    report: dict,
    pending: list[dict],
    capabilities: dict | None,
    policy: dict | None,
    rules: list[dict] | None,
    actions: list[dict] | None,
    catalog_alerts: dict | None,
    unmet_demand: list[dict] | None,
    product_performance: dict | None,
    sales_period_comparison: dict | None,
    question: str,
    payment_recovery: dict | None = None,
) -> str:
    """The merchant's real figures and settings, as the model's entire world for this turn.

    Serialized as JSON rather than folded into prose, so the model reads
    exact values rather than a paraphrase of them that could drift from the
    real number - the same reasoning `Signal.raw` and `CommerceError.platform_detail`
    already use elsewhere in this codebase for "the source's own shape, kept
    exact rather than summarized".
    """
    data = {
        "last_30_days": {
            "shoppers_helped": report.get("shoppers_helped"),
            "problems_solved": report.get("problems_solved"),
            "resolution_rate_percent": report.get("resolution_rate"),
            "revenue_recovered": f"{report.get('revenue_recovered')} {report.get('currency')}",
            "recovery_count": report.get("recovery_count"),
            "recovery_opportunities": report.get("recovery_opportunities"),
            "total_completed_sales": (
                f"{report.get('total_sales_amount')} {report.get('total_sales_currency')}"
            ),
            "completed_order_count": report.get("completed_order_count"),
            "average_order_value": (
                f"{report.get('average_order_value')} {report.get('total_sales_currency')}"
                if report.get("average_order_value") is not None
                else "not enough priced orders yet to compute one"
            ),
            "resolved_without_a_person": report.get("handled_without_you"),
            "waiting_on_a_person_right_now": report.get("waiting_for_you"),
            "what_shoppers_ran_into": report.get("friction"),
            "holdout_comparison": report.get("holdout"),
        },
        "approvals_waiting_on_you_right_now": [
            {
                "action": p.get("action_type"),
                "friction": p.get("friction_type"),
                "waiting_minutes": p.get("waiting_minutes"),
                "order_id": p.get("order_id"),
            }
            for p in pending
        ],
        "platform_capabilities": capabilities
        if capabilities is not None
        else "not supplied this turn",
        "current_risk_policy": policy if policy is not None else "not supplied this turn",
        "risk_rules_in_order": rules if rules is not None else "not supplied this turn",
        "action_types_and_their_risk_properties": (
            actions if actions is not None else "not supplied this turn"
        ),
        "catalog_alerts_out_of_stock_or_low": (
            catalog_alerts if catalog_alerts is not None else "not supplied this turn"
        ),
        "unmet_demand_searched_for_and_not_found_last_30_days": (
            unmet_demand if unmet_demand is not None else "not supplied this turn"
        ),
        "product_performance": (
            product_performance if product_performance is not None else "not supplied this turn"
        ),
        "sales_period_comparison_recent_vs_prior_window": (
            sales_period_comparison
            if sales_period_comparison is not None
            else "not supplied this turn"
        ),
        "payment_recovery": (
            payment_recovery if payment_recovery is not None else "not supplied this turn"
        ),
    }
    return (
        f"Here is your shop's current data:\n{json.dumps(data, indent=2)}\n\n"
        f"Question: {question}"
    )


#: The operator's own system prompt. Deliberately separate from the merchant
#: one rather than a shared template with a flag: an operator's data is cross-
#: merchant, and conflating the two prompts risks the model answering an
#: operator's question with one merchant's figures, or vice versa - clearer
#: to keep the two prompts textually distinct than to parametrize one.
OPS_SYSTEM_PROMPT = """You are CV3's operations copilot. You answer a CV3 operator's \
questions about workload across every merchant they cover, using only the data given to \
you for this turn - never a number, a merchant name, or a claim you were not given. If \
the data does not answer their question, say so plainly rather than guessing.

The data given to you each turn covers:
- **Workload right now**: how many approvals are waiting across all merchants, the
  longest anyone has been waiting, and today's decided-so-far count.
- **The approval queue**: what's waiting on a person right now, oldest first, each with
  which merchant it belongs to, the action proposed, and how long it's waited.
- **Handovers**: cases handed to a person where the decision is already made and someone
  just needs to act (distinct from an approval, which is still asking "may I?").
- **Recent decisions**: what was approved or rejected recently and what came of it,
  including cases where an approval later failed on the platform.
- **Shopper issues, by real category**: how often each kind of problem (a declined
  payment, a dead search, an escalation, etc - `Case.friction_type`, the actual
  recorded reason a case was opened) has happened across every merchant you cover,
  in the last 30 days. THIS is the field to use for "what are the most common
  issues" or "what do shoppers keep running into" - never the approval queue or
  recent-decisions lists for that question. Those are keyed by `action_type`, which
  is what the engine proposed doing about a problem (e.g. offering an alternate
  payment method), not what the problem was - "OFFER_ALTERNATE_PAYMENT happened a
  lot" is not an issue, it is a response to one, and answering a "most common
  issues" question with action-type counts is wrong even though the counts
  themselves are real. Always answer that question from the issue-category field.

This is a workload and triage question, not any one merchant's own performance report -
a question like "how is my shop doing" or "what's my resolution rate" is a Merchant
Copilot question, not yours; say so and point them to the merchant console rather than
answering it from operator-side data that doesn't measure that.

There is no cross-merchant revenue, incident, or SLA-breach data available yet (that's
future roadmap work) - say so plainly if asked, never estimate one from workload counts.

There is also no incident or adapter-health tracking of any kind - no record of
whether a platform's API was up or down, or when. If asked about "the latest
incident", whether a merchant's payment API "went down", or anything of that shape,
say plainly that this is not currently tracked - never infer an incident happened
(or didn't) from decline counts, friction counts, or anything else you were given.

Rules:
- Never invent a number, a merchant name, a case id, or a shopper's identity. Every claim \
you state must come from the data given to you this turn.
- You cannot take any action - you cannot approve anything, close a handover, or change a \
setting. If asked to do one of those, say plainly that you can only report on what's \
waiting or what's already happened, and point them to the queue to actually act.
- Be concise. An operator triaging their queue wants a specific, actionable answer, not a
  report.
- Cite the actual figure or merchant name when you have one ("Kettle & Bloom, waiting 14
  minutes", not "one of them, waiting a while").
- Never mention that you are an AI model, a prompt, or anything about how you work \
internally - answer as the operations console's own dashboard would.
"""


async def ask_ops(
    question: str, connection_ids: list[str], merchant_names: dict[str, str]
) -> CopilotReply:
    """Answer one operator's question about workload across every merchant
    they cover.

    `connection_ids` is the same set `_operator_connections()` already
    resolves for /ops/queue, /ops/history, /ops/stats and /ops/handovers -
    passed in rather than resolved here, so this module never has to know
    how an operator's visibility is determined; that stays the route's job.
    `merchant_names` is the same `MERCHANT_NAMES` lookup those routes already
    use to turn a connection id into a name a human reads - the repository
    functions here return bare `connection_id`, same as they do for those
    routes, and each one is resolved through this the same way before the
    model ever sees it, so the answer names "Kettle & Bloom" rather than
    "conn_kettle". Every call re-reads current data, the same "never go
    stale between questions" reasoning as the merchant copilot's `ask()`.
    """
    stats = await db.ops_stats(connection_ids)
    pending = await db.pending_across(connection_ids, limit=20)
    handovers, handover_total = await db.handovers_across(
        connection_ids, offset=0, limit=20
    )
    decisions = await db.decided_across(connection_ids, limit=20)
    issues = await db.friction_summary_across(connection_ids, days=30)

    def name_for(cid: str | None) -> str | None:
        return merchant_names.get(cid, cid) if cid else cid

    stats = {
        **stats,
        "by_merchant": {
            name_for(cid): count for cid, count in (stats.get("by_merchant") or {}).items()
        },
    }
    for row in pending:
        row["merchant_name"] = name_for(row.get("connection_id"))
    for row in handovers:
        row["merchant_name"] = name_for(row.get("connection_id"))
    for row in decisions:
        row["merchant_name"] = name_for(row.get("connection_id"))

    issues = {
        **issues,
        "by_type_by_merchant": {
            name_for(cid): counts
            for cid, counts in (issues.get("by_type_by_merchant") or {}).items()
        },
    }

    context = _build_ops_context(
        stats, pending, handovers, handover_total, decisions, issues, question
    )
    return await _complete(OPS_SYSTEM_PROMPT, context)


def _build_ops_context(
    stats: dict,
    pending: list[dict],
    handovers: list[dict],
    handover_total: int,
    decisions: list[dict],
    issues: dict,
    question: str,
) -> str:
    """The operator's real cross-merchant workload, as the model's entire
    world for this turn. Same JSON-not-prose reasoning as `_build_context`."""
    data = {
        "workload_right_now": {
            "approvals_waiting": stats.get("waiting"),
            "oldest_wait_minutes": stats.get("oldest_wait_minutes"),
            "waiting_by_merchant": stats.get("by_merchant"),
            "decided_today": stats.get("today"),
        },
        "approval_queue_oldest_first": [
            {
                "merchant": p.get("merchant_name"),
                "action": p.get("action_type"),
                "friction": p.get("friction_type"),
                "waiting_minutes": p.get("waiting_minutes"),
                "order_id": p.get("order_id"),
            }
            for p in pending
        ],
        "handovers_someone_needs_to_act_on": {
            "total_open": handover_total,
            "shown": [
                {
                    "merchant": h.get("merchant_name"),
                    "friction": h.get("friction_type"),
                    "waiting_minutes": h.get("waiting_minutes"),
                }
                for h in handovers
            ],
        },
        "shopper_issues_by_real_category_last_30_days": issues.get("by_type"),
        "shopper_issues_by_real_category_per_merchant_last_30_days": (
            issues.get("by_type_by_merchant")
        ),
        "recent_decisions": [
            {
                "merchant": d.get("merchant_name"),
                "action": d.get("action_type"),
                "state": d.get("state"),
                "final_state": d.get("final_state"),
            }
            for d in decisions
        ],
    }
    return (
        f"Here is the current cross-merchant workload:\n{json.dumps(data, indent=2)}\n\n"
        f"Question: {question}"
    )
