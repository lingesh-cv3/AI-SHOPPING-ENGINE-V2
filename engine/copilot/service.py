"""The merchant copilot.

Answers a merchant's own questions about their own store, in plain language,
grounded in the same figures the merchant console already shows them - never
in anything invented. Read-only by design: this module proposes no action,
decides nothing, and never touches the risk gate, because there is nothing
here to decide - a question about last week's resolution rate has no verdict
for the gate to reach. Acting on a merchant's instruction ("approve that
case", "turn off refunds") is a different, later capability - the roadmap's
own Phase 3 "Merchant Approval & Action Center" - and deliberately not what
this module does. Building it as a chatbot response is the right shape here
specifically because answering a question is all it is; CLAUDE.md's own rule
against isolated chatbot responses is about the actions/risk/outcome case,
which this is not.
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
their own shop, using only the figures given to you for this turn - never a number, an \
order id, or a claim you were not given. If the data does not answer their question, say so \
plainly rather than guessing or estimating.

Rules:
- Never invent a number, an order id, or a shopper's identity. Every figure you state must \
come from the data given to you this turn.
- You cannot take any action - you cannot approve anything, issue a refund, or change a \
setting. If asked to do one of those, say plainly that you can only report on what has \
already happened, and point them to the approval queue or their settings to actually do it.
- Be concise. A merchant asking a quick question wants a specific answer, not a report.
- Cite the actual figure when you have one ("74%", not "most of them").
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


async def ask(connection_id: str, question: str) -> CopilotReply:
    """Answer one question about one merchant's store.

    Every call re-reads the merchant's own current data - report figures,
    what is waiting on them right now - so an answer cannot go stale between
    questions. No conversation memory yet: each question stands alone, the
    same way /api/simulate does for the shopper side. Multi-turn memory is a
    natural extension, not something this first version needs to claim.
    """
    report = await db.merchant_report(connection_id, days=30)
    pending = await db.pending_approvals(connection_id, limit=10)
    context = _build_context(report, pending, question)

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
            system=SYSTEM_PROMPT,
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


def _build_context(report: dict, pending: list[dict], question: str) -> str:
    """The merchant's real figures, as the model's entire world for this turn.

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
    }
    return (
        f"Here is your shop's current data:\n{json.dumps(data, indent=2)}\n\n"
        f"Question: {question}"
    )
