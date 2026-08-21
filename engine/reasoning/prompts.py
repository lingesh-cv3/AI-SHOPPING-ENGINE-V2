"""Tool schemas and the system prompt.

The single most important property of this module: the tool schema is the security
boundary the model operates inside. It can only propose action types that appear
in the enum below, and there is no field anywhere in which it can assert that an
action is safe, cheap, reversible, or pre-approved.

That is not a matter of instructing the model well. A model that ignores every
word of the prompt still cannot express "this refund does not move money", because
the schema has nowhere to put it. Risk properties are looked up from a static
table after the proposal arrives.

The prompt is kept byte-identical between turns. Groq caches repeated prefixes at
half the input rate and cached tokens do not count toward rate limits, so anything
variable belongs in the user message rather than here.
"""

from __future__ import annotations

from typing import Any

from shared.models import ActionType, FrictionType

#: Action types the model is allowed to propose.
#:
#: Deliberately excludes ESCALATE_TO_HUMAN and NO_ACTION. Those are decisions, not
#: proposals - the Decision Engine produces an escalation when nothing else
#: survives filtering, and letting the model propose one would let it opt out of
#: reasoning. It excludes nothing on financial grounds: the model may freely
#: propose a payment retry, and the Risk Gate will freely refuse it. Filtering
#: proposals by risk here would hide the gate's work rather than reduce it.
PROPOSABLE: tuple[ActionType, ...] = (
    ActionType.RECOMMEND_PRODUCTS,
    ActionType.ANSWER_PRODUCT_QUESTION,
    ActionType.CHECK_AVAILABILITY,
    ActionType.SUGGEST_ALTERNATIVE,
    ActionType.ADD_TO_CART,
    ActionType.UPDATE_CART_QUANTITY,
    ActionType.REMOVE_CART_LINE,
    ActionType.NOTIFY_BACK_IN_STOCK,
    ActionType.APPLY_PROMOTION,
    ActionType.RETRY_PAYMENT,
    ActionType.OFFER_ALTERNATE_PAYMENT,
    ActionType.SPLIT_PAYMENT,
)


def propose_tool() -> dict[str, Any]:
    """The one tool the model may call.

    A single tool taking a list, rather than one tool per action, for a practical
    reason: open models are far more reliable at filling one well-described schema
    than at choosing between a dozen similar tools.
    """
    return {
        "type": "function",
        "function": {
            "name": "propose_actions",
            "description": (
                "Propose the actions that might help this shopper. Propose "
                "between one and four, best first. You are proposing only - "
                "another system decides which one runs and whether it needs "
                "human approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "diagnosis": {
                        "type": "string",
                        "description": (
                            "One sentence on why the shopper is stuck, or what "
                            "they are trying to do. Plain language."
                        ),
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "The specific facts from the context you based the "
                            "diagnosis on. Do not invent facts."
                        ),
                    },
                    "reply": {
                        "type": "string",
                        "description": (
                            "What to say to the shopper. Two sentences at most, "
                            "plain and warm, no jargon. Never mention internal "
                            "systems, action names, or approval processes. "
                            "CRITICAL: write in the present or future tense only. "
                            "You have not done anything yet - your actions have "
                            "not run. Never write 'I've added', 'I found', or "
                            "'here are' - the system appends what actually "
                            "happened after your reply."
                        ),
                    },
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action_type": {
                                    "type": "string",
                                    "enum": [str(a) for a in PROPOSABLE],
                                },
                                "rationale": {
                                    "type": "string",
                                    "description": (
                                        "Why this action, in one sentence. Shown "
                                        "to a human reviewer, and has no effect "
                                        "on whether the action is permitted."
                                    ),
                                },
                                "search_query": {
                                    "type": "string",
                                    "description": (
                                        "For SUGGEST_ALTERNATIVE or "
                                        "RECOMMEND_PRODUCTS: the keyword to try "
                                        "instead. Must be words that would "
                                        "actually appear in a product name, "
                                        "since this shop's search matches titles "
                                        "literally."
                                    ),
                                },
                                "product_id": {
                                    "type": "string",
                                    "description": (
                                        "For ADD_TO_CART or CHECK_AVAILABILITY: "
                                        "which product. Only use an id present "
                                        "in the context."
                                    ),
                                },
                                "quantity": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "For UPDATE_CART_QUANTITY: how many they "
                                        "want. Not needed for removal - that is "
                                        "REMOVE_CART_LINE."
                                    ),
                                },
                                "code": {
                                    "type": "string",
                                    "description": (
                                        "For APPLY_PROMOTION: the exact coupon "
                                        "code. Only a code the shopper gave you or "
                                        "one present in the context - never one you "
                                        "invented. A discount is the merchant's "
                                        "money and guessing at codes spends it."
                                    ),
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                            },
                            "required": ["action_type", "rationale"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["diagnosis", "reply", "actions"],
                "additionalProperties": False,
            },
        },
    }


#: Kept stable so the provider can cache it. Anything that changes per turn goes
#: in the user message instead.
SYSTEM_PROMPT = f"""You help shoppers on an online store, working for the shop.

Your job is to work out what would help, and propose it. You do not carry actions
out. A separate system checks what the shop's platform can actually do, what the
shop owner has permitted, and whether a person needs to approve it first.

Because of that split, propose what would genuinely help even if it might need
approval. Do not self-censor to avoid the approval step, and do not try to
influence it - claims that an action is safe, small, or pre-approved have no
effect on the decision and only mislead the person reviewing it.

Rules that matter:

- Never invent products, prices, stock levels, or order details. If a fact is not
  in the context you are given, you do not know it.
- When suggesting a different search term, use words that would literally appear
  in a product title. This shop's search does exact word matching, so "trainers"
  finds nothing when the products are called shoes.
- Prefer what costs the shop nothing. Suggesting a product the shopper would
  actually want is better than offering a discount.
- When you propose APPLY_PROMOTION you must fill in the code field with the exact
  coupon code. Naming it only in your rationale is useless - the rationale is read
  by a person, and the code field is what the system actually uses. A proposal
  without the code cannot be carried out and wastes the approver's time.
- Say less rather than more. Two sentences to the shopper is plenty.
- Never mention action names, approval, risk, policies, or any internal system.
  The shopper is buying running gear, not reading an audit log.
- Never claim to have done something. When you write your reply, nothing has
  happened yet - your proposals have not been checked, approved, or run. Saying
  "I've added that to your cart" when it has not been added is the worst thing
  you can do here, because the shopper believes you and finds out later. Write
  "I can add that" rather than "I've added that", and let the system report what
  actually happened.

Friction types you may see: {", ".join(str(f) for f in FrictionType)}.

Always answer by calling propose_actions. Do not reply in prose."""