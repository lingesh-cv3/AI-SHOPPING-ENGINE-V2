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
- **Performance figures**: shoppers helped, resolution rate, revenue recovered, what
  shoppers ran into, what is waiting on them right now.
- **Platform capabilities**: what their specific commerce platform can actually do through
  this engine (e.g. whether it supports payment recovery, webhooks, which operations are
  available and why an unsupported one isn't).
- **Risk policy**: their current automation mode, which actions are set to run
  automatically vs. always need a person, and their holdout percentage.
- **Risk rules and action types**: the fixed rules that decide automatic vs.
  needs-approval vs. blocked, and which actions exist and their risk properties
  (financial, reversible, whether they touch customer data).
- **Catalog alerts**: products currently out of stock or low, read fresh from their
  live catalog. This is the answer to "what's out of stock" or "what should I restock" -
  a specific list of product titles, not a vague estimate.
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
    catalog_alerts: list[dict] | None = None,
    unmet_demand: list[dict] | None = None,
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
    pending = await db.pending_approvals(connection_id, limit=10)
    context = _build_context(
        report, pending, capabilities, policy, rules, actions,
        catalog_alerts, unmet_demand, question,
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
    catalog_alerts: list[dict] | None,
    unmet_demand: list[dict] | None,
    question: str,
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

This is a workload and triage question, not any one merchant's own performance report -
a question like "how is my shop doing" or "what's my resolution rate" is a Merchant
Copilot question, not yours; say so and point them to the merchant console rather than
answering it from operator-side data that doesn't measure that.

There is no cross-merchant revenue, incident, or SLA-breach data available yet (that's
future roadmap work) - say so plainly if asked, never estimate one from workload counts.

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

    context = _build_ops_context(stats, pending, handovers, handover_total, decisions, question)
    return await _complete(OPS_SYSTEM_PROMPT, context)


def _build_ops_context(
    stats: dict,
    pending: list[dict],
    handovers: list[dict],
    handover_total: int,
    decisions: list[dict],
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
