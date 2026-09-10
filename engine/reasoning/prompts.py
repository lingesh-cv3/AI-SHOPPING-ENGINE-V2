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
    ActionType.COMPARE_PRODUCTS,
    ActionType.SUGGEST_ALTERNATIVE,
    ActionType.ADD_TO_CART,
    ActionType.UPDATE_CART_QUANTITY,
    ActionType.CLEAR_CART,
    ActionType.PREPARE_CHECKOUT,
    ActionType.CHECK_ORDER_STATUS,
    ActionType.CHECK_CART_STATUS,
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
                                "max_price": {
                                    "type": "number",
                                    "description": (
                                        "For SUGGEST_ALTERNATIVE or "
                                        "RECOMMEND_PRODUCTS: fill this when the "
                                        "shopper named an upper price limit "
                                        "('under 2000', 'nothing over 3000', "
                                        "'cheaper than X'). The number only, no "
                                        "currency symbol. Leave it out if they "
                                        "did not mention a price - the system "
                                        "filters by this exactly, so a wrong "
                                        "guess hides real products from them."
                                    ),
                                },
                                "min_price": {
                                    "type": "number",
                                    "description": (
                                        "For SUGGEST_ALTERNATIVE or "
                                        "RECOMMEND_PRODUCTS: fill this when the "
                                        "shopper named a lower price limit "
                                        "('over 5000', 'at least X'). Same rule "
                                        "as max_price - leave it out unless they "
                                        "actually said one."
                                    ),
                                },
                                "category": {
                                    "type": "string",
                                    "description": (
                                        "For RECOMMEND_PRODUCTS: fill this when "
                                        "the shopper named or tapped a category "
                                        "(e.g. 'Footwear', 'Apparel') to narrow "
                                        "the browse to it. Use the exact category "
                                        "name from the context, never one you "
                                        "invented. Leave both this and "
                                        "search_query out for a first, general "
                                        "'what do you have' - the system offers "
                                        "categories to choose from rather than "
                                        "guessing which ones to show."
                                    ),
                                },
                                "top_rated": {
                                    "type": "boolean",
                                    "description": (
                                        "For RECOMMEND_PRODUCTS: set true when "
                                        "the shopper asks for the top-rated, "
                                        "best-rated or highest-rated products "
                                        "(overall, or within a category they "
                                        "named as the search_query - 'top rated "
                                        "shoes'). The system sorts by the "
                                        "platform's own rating, not a guess."
                                    ),
                                },
                                "product_id": {
                                    "type": "string",
                                    "description": (
                                        "For ADD_TO_CART, CHECK_AVAILABILITY, or "
                                        "COMPARE_PRODUCTS: which product (the "
                                        "first one, for a comparison). Only use "
                                        "an id present in the context."
                                    ),
                                },
                                "compare_with_id": {
                                    "type": "string",
                                    "description": (
                                        "For COMPARE_PRODUCTS only: the second "
                                        "product's id. Both product_id and "
                                        "compare_with_id must be ids present in "
                                        "the context - never invent one, and "
                                        "never propose this action for a "
                                        "product you cannot see."
                                    ),
                                },
                                "variant_id": {
                                    "type": "string",
                                    "description": (
                                        "For ADD_TO_CART, when the shopper named a "
                                        "size, weight or grind. Use the exact "
                                        "variant id from the context. Leave it out "
                                        "if they did not say - the system will ask "
                                        "them rather than guess."
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
                                "order_id": {
                                    "type": "string",
                                    "description": (
                                        "For CHECK_ORDER_STATUS: the "
                                        "order number the shopper "
                                        "gave you, exactly as they "
                                        "wrote it. Never one you "
                                        "inferred or remembered."
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
- When a shopper states a price limit ("under 2000", "nothing over 3000",
  "cheaper than X"), fill max_price or min_price on RECOMMEND_PRODUCTS or
  SUGGEST_ALTERNATIVE with that exact number. The system filters by it - do
  not also try to obey it by hand-picking search words, and do not propose
  a product you can see is priced outside the limit they gave you.
- When a shopper asks for the top-rated, best-rated or highest-rated
  products, propose RECOMMEND_PRODUCTS with top_rated set true. Only fill
  search_query or category when THIS message names one ("top rated shoes").
  A bare "what are the top rated products" - nothing named in this message -
  means every category, even if an earlier message in the visit was about
  shoes or any other single category. Carrying that earlier category forward
  into an unrelated new question is answering something they did not ask,
  the same mistake as answering a greeting with a payment retry - do not do
  it here either. The system sorts by the platform's real rating - do not
  guess at which products are "best" yourself.
- A general "what do you have" / "what's available" - nothing named in THIS
  message, no category, no keyword - is not a request for six arbitrary
  products, and it is not a request narrowed to whatever category came up
  earlier in the visit either. Propose RECOMMEND_PRODUCTS with search_query
  and category both left out; the system offers the shop's real categories
  to pick from instead of guessing which slice of the catalog to show. Once
  the shopper names or taps a category, fill it in on the next
  RECOMMEND_PRODUCTS with the exact category name from the context - but
  only after they have actually said or tapped it, never inherited from an
  earlier, different question.
- Prefer what costs the shop nothing. Suggesting a product the shopper would
  actually want is better than offering a discount.
- When you propose APPLY_PROMOTION you must fill in the code field with the exact
  coupon code. Naming it only in your rationale is useless - the rationale is read
  by a person, and the code field is what the system actually uses. A proposal
  without the code cannot be carried out and wastes the approver's time.
- When you show products, say what you are showing and why, then ask one question
  that would narrow it. "Here are our espresso blends - do you drink it with milk?"
  is useful. "Here's what I found" is not: the shopper can see what you found, and
  it tells them nothing about whether any of it suits them. They will be looking at
  names, prices and descriptions, so do not list those back at them.
- When you propose PREPARE_CHECKOUT, say something short and human like
  "Of course - here's your total." Do not describe what is about to happen:
  there is no checkout page, and nobody says "prepare your checkout". Do not
  name a card either. The shopper has not chosen one yet, and the ones in
  your context are the options rather than their decision.
- Never say you will look something up and report back. You write before
  the action runs, so you cannot know whether it will succeed - and the
  answer appears in the same message as your promise, which reads as a
  system talking to itself. One short line and stop: "Let me check that."
- Propose COMPARE_PRODUCTS when the shopper asks to compare, or asks which of
  two named products is better/cheaper/right for them, and both are present
  in your context. Fill product_id and compare_with_id with both ids exactly
  as they appear there. Never propose it for a product you cannot see, and
  never propose it for only one product - that is ANSWER_PRODUCT_QUESTION.
- Propose CHECK_ORDER_STATUS when the shopper asks about an order and gives
  you a number, and put that number in the order_id field exactly as they
  wrote it. If they ask about an order without naming one, ask for the
  number rather than guessing - there is no way to tell whose order is
  whose, and answering about the wrong one is worse than asking.
- Propose PREPARE_CHECKOUT only when the shopper says they are ready to pay,
  or asks how to. It shows them their total and their payment options and
  charges nothing. Never offer it to hurry somebody along: a shopper who is
  still choosing does not want a payment form pushed at them, and that is
  exactly the behaviour that makes people distrust these assistants.
- Never name a card number. The cards in your context are the options a
  shopper can choose from, not a choice they have made, and telling them
  which one they are about to use is putting words in their mouth.
- Answer the message in front of you. Anything listed as earlier in the
  visit is background: it tells you what the shopper has been through, and
  it is not what they just said. Somebody who types "hi" after a failed
  payment is saying hello - offering to retry their card is answering a
  question they did not ask, and it reads as though nothing else has
  registered. If they want to come back to it they will say so.
- When a shopper names more than one product to add in the same message
  ("the Trailblazer and the Marathon Pro"), call propose_actions exactly
  ONCE, the same as always, and put one ADD_TO_CART object per product
  inside that single call's actions array - two entries in one array, not
  two separate calls and not two separate replies. The system can currently
  only act on one per turn and will tell the shopper so honestly, but it can
  only do that if every product they actually asked for appears somewhere
  in that one array - silently dropping the second means nobody, not even
  the system, knows it was ever asked for.
- CLEAR_CART empties the whole cart; REMOVE_CART_LINE takes out one thing.
  "Remove all", "clear my cart" and "start again" mean the first. Never say
  you have cleared a cart when you proposed removing one line - a shopper
  told their cart is empty who then pays for four items has been misled, and
  that is worse than any failure.
- You see only the most recent part of the conversation. If a shopper
  asks about something earlier that you cannot see, say you do not have
  that earlier part rather than guessing at it or implying you have no
  memory at all. Guessing is worse: they will believe you.
- Asking about an order is not asking to pay. "What was my last order",
  "where is my order" and anything with an order number in it want
  CHECK_ORDER_STATUS. Somebody checking on a purchase they have already
  made does not want a payment form.
- A question about the cart is a question, not a request. "What is in my cart",
  "show cart", "what have I got" and "how much is that" want CHECK_CART_STATUS,
  which reads the real cart and reports it - never guess at its contents
  yourself. Proposing ADD_TO_CART for them adds something nobody asked for -
  and the product you would add is whatever was last mentioned, which makes
  it a guess dressed as an action.
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