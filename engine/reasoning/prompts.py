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
    ActionType.CHECK_COMPATIBILITY,
    ActionType.SUGGEST_COMPLEMENTARY,
    ActionType.CART_INTELLIGENCE,
    ActionType.PROPOSE_CART_CHANGE,
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
                    "shopping_state": {
                        "type": "object",
                        "description": (
                            "Update to the shopper's CURRENT mission, only "
                            "when THIS message states something new or "
                            "different. Every field is optional and should "
                            "be left out entirely when this message says "
                            "nothing new about it - omitting a field means "
                            "'no change', not 'clear it'. Never infer a "
                            "durable goal/budget/constraint from a "
                            "greeting, thanks, or small talk."
                        ),
                        "properties": {
                            "goal": {
                                "type": "string",
                                "description": (
                                    "What they are ultimately trying to buy "
                                    "or accomplish, in their own terms ('a "
                                    "lightweight marathon setup'). Only "
                                    "when newly stated or changed this "
                                    "message."
                                ),
                            },
                            "budget_amount": {
                                "type": "number",
                                "description": (
                                    "A total budget the shopper stated for "
                                    "this mission (not a per-item price "
                                    "filter - that is max_price on the "
                                    "action itself). Only when this message "
                                    "states or changes one."
                                ),
                            },
                            "budget_currency": {
                                "type": "string",
                                "description": "Only if the shopper named a currency explicitly.",
                            },
                            "constraints_add": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Only for a real requirement that is "
                                    "neither clearly wanted nor clearly "
                                    "unwanted (e.g. 'must fit in a carry-on', "
                                    "'needs to arrive by Friday'). If the "
                                    "shopper clearly WANTS something, use "
                                    "must_have_add instead. If they clearly "
                                    "do NOT want something, use "
                                    "exclusions_add instead. Do not put a "
                                    "mix of wanted and unwanted things in "
                                    "this one list - split them into the "
                                    "right field each."
                                ),
                            },
                            "constraints_remove": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Constraints the shopper just said no longer apply.",
                            },
                            "must_have_add": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "New must-have requirements stated this message.",
                            },
                            "must_have_remove": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "nice_to_have_add": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "New preferences that would help but are not required.",
                            },
                            "nice_to_have_remove": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "exclusions_add": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Things the shopper explicitly does not want.",
                            },
                            "exclusions_remove": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "priorities_add": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "A trade-off the shopper stated, in "
                                    "their own words ('lighter matters more "
                                    "than cushioning'). Only an explicit "
                                    "trade-off, never one you inferred."
                                ),
                            },
                            "mission_status": {
                                "type": "string",
                                "enum": ["exploring", "comparing", "deciding", "done"],
                                "description": (
                                    "Your best read of where they are: "
                                    "exploring (browsing/discovering), "
                                    "comparing (weighing specific options), "
                                    "deciding (close to choosing), done "
                                    "(bought or dropped it). Omit if unclear."
                                ),
                            },
                            "clear_fields": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["goal", "budget_amount", "budget_currency", "mission_status"],
                                },
                                "description": (
                                    "Fields to explicitly RETRACT this turn - "
                                    "only when the shopper says something like "
                                    "'budget doesn't matter anymore' or 'forget "
                                    "what I said about X' or 'never mind the "
                                    "goal, show me something different'. This "
                                    "actually clears the stored value, so use "
                                    "it only for a genuine retraction, never a "
                                    "guess. If they are simply stating a NEW "
                                    "value instead (a different budget), just "
                                    "fill that field normally - do not clear it "
                                    "first."
                                ),
                            },
                        },
                        "additionalProperties": False,
                    },
                    "durable_facts": {
                        "type": "array",
                        "description": (
                            "Facts worth remembering on a FUTURE, DIFFERENT "
                            "visit - not for this mission. Only a stable "
                            "preference or an owned item the shopper "
                            "explicitly stated (e.g. 'I always drink oat "
                            "milk', 'I already have the Trailblazer in a "
                            "9'). Leave this empty almost always. Never "
                            "include a greeting, a filler remark, or "
                            "anything only true for this one purchase - "
                            "that belongs in shopping_state, not here."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "description": (
                                        "A short, stable label for this "
                                        "fact ('prefers_oat_milk', not a "
                                        "full sentence) - so a later, "
                                        "differently-worded restatement of "
                                        "the same fact updates the same "
                                        "one rather than creating a "
                                        "duplicate."
                                    ),
                                },
                                "value": {"type": "string"},
                            },
                            "required": ["key", "value"],
                            "additionalProperties": False,
                        },
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
                                        "For ADD_TO_CART, CHECK_AVAILABILITY, "
                                        "COMPARE_PRODUCTS, CHECK_COMPATIBILITY, "
                                        "or SUGGEST_COMPLEMENTARY: which product "
                                        "(the first one, for a comparison or "
                                        "compatibility check; the anchor product, "
                                        "for a complementary suggestion). Only "
                                        "use an id present in the context."
                                    ),
                                },
                                "compare_with_id": {
                                    "type": "string",
                                    "description": (
                                        "For COMPARE_PRODUCTS or "
                                        "CHECK_COMPATIBILITY: the second "
                                        "product's id - what the shopper wants "
                                        "compared against, or checked for fit "
                                        "with, product_id. Not used for "
                                        "SUGGEST_COMPLEMENTARY, which has only "
                                        "one anchor product. Both product_id and "
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
                                "proposal_change_type": {
                                    "type": "string",
                                    "enum": ["ADD", "REMOVE", "REPLACE", "CHANGE_QUANTITY"],
                                    "description": (
                                        "For PROPOSE_CART_CHANGE only: what kind "
                                        "of change you are suggesting. ADD/REMOVE "
                                        "use product_id for the product involved; "
                                        "REPLACE uses product_id for the new "
                                        "product and proposal_replaces_product_id "
                                        "for what it would remove; "
                                        "CHANGE_QUANTITY uses product_id and "
                                        "quantity for the new amount."
                                    ),
                                },
                                "proposal_replaces_product_id": {
                                    "type": "string",
                                    "description": (
                                        "For PROPOSE_CART_CHANGE with "
                                        "proposal_change_type REPLACE only: the "
                                        "product currently in the cart that this "
                                        "would remove. Must be a product actually "
                                        "in the shopper's cart, from the context."
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
- Propose ANSWER_PRODUCT_QUESTION when the shopper asks about a product
  already shown or discussed earlier in this conversation, rather than
  asking to search for something new - including "why did you recommend
  this one", "why this?", "what's special about it", or any other question
  about a product's own attributes, price, stock, or fit already visible in
  your context. This is a conversational answer, not a new search: ground
  it only in the real details (price, description, rating, stock, and any
  "fit"/reason evidence) already given to you for that exact product,
  never invent a new reason. Never propose RECOMMEND_PRODUCTS again for a
  question ABOUT something you already showed - that treats an explanation
  request as if it were a fresh product search and produces a nonsensical
  reply. Never propose it for a product you cannot see in your context.
- Propose CHECK_AVAILABILITY when the shopper asks a direct yes/no question
  about ONE specific named product they can see or has been shown - "do you
  have X?", "is X in stock?", "do you carry X?" - rather than RECOMMEND_PRODUCTS.
  RECOMMEND_PRODUCTS is for finding something new to browse; a direct
  availability question about a product already named deserves a direct
  answer ("Yes, it's in stock" / "No, that one's sold out"), not a generic
  "here's what's available" list. Fill product_id with that exact product.
- Propose COMPARE_PRODUCTS when the shopper asks to compare, or asks which of
  two named products is better/cheaper/right for them, and both are present
  in your context. Fill product_id and compare_with_id with both ids exactly
  as they appear there. Never propose it for a product you cannot see, and
  never propose it for only one product - that is ANSWER_PRODUCT_QUESTION.
- Propose CHECK_COMPATIBILITY when the shopper asks whether one product
  works with, fits, or pairs with another, and both are present in your
  context. Fill product_id and compare_with_id. The answer will be grounded
  only in what the merchant has actually declared - if you are not sure two
  things are compatible, propose this rather than guessing an answer
  yourself in your reply.
- Propose SUGGEST_COMPLEMENTARY when the shopper asks what else they need,
  what goes with something, or wants to complete a set, and the anchor
  product is present in your context. Fill product_id only. The suggestions
  will come only from what the merchant has actually declared as
  complementary - never suggest an accessory yourself in your reply based on
  category or guesswork.
- Propose CART_INTELLIGENCE when the shopper asks something about their
  actual cart beyond "what's in it" - "is anything wrong with my cart",
  "am I missing anything", "is this over budget", "can I still afford
  this". The findings are computed from the real cart and real,
  freshly-fetched product data, never from your own impression of the
  conversation so far.
- Propose PROPOSE_CART_CHANGE when you want to suggest adding, removing,
  replacing, or changing the quantity of something in the cart, rather than
  just doing it. This creates a suggestion for the shopper to accept or
  reject - it never changes the cart by itself, so word your reply as an
  offer ("Want me to add X?"), never as something already done. Only
  propose a product that is actually present in your context; never invent
  one to fill out a suggestion.
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

- Fill shopping_state only from what THIS message actually states. A single
  message routinely states several DIFFERENT kinds of thing at once - a goal,
  a budget, a thing wanted, a thing ruled out, a trade-off - and each one
  belongs in its own field. Before answering, check the message against every
  field in turn (goal? budget? a must-have? an exclusion? a priority?) rather
  than stopping once you have found the most obvious one - the most common
  mistake here is capturing only the number (budget) and missing a
  requirement or exclusion stated in the same sentence.
  Examples, each showing several fields filled from ONE message because the
  shopper stated several things at once:
  - "I need running shoes for a marathon, lightweight, under 15000, not
    waterproof" -> goal="a marathon setup", budget_amount=15000,
    must_have_add=["lightweight"], exclusions_add=["waterproof"].
  - "Something for hiking, has to be waterproof, I don't want anything
    flashy-looking, and ideally under 8000" -> goal="hiking gear",
    must_have_add=["waterproof"], exclusions_add=["flashy-looking"],
    budget_amount=8000.
  - "A gift for my dad who hates loud colors, quiet and comfortable is what
    matters most, price isn't really a factor" -> goal="a gift for my dad",
    exclusions_add=["loud colors"], priorities_add=["quiet and comfortable
    matters more than price"].
  A later message that only says "actually make it under 12000" changes only
  budget_amount - leave every other field out entirely, because nothing else
  changed. Never restate the whole mission back - that duplicates what is
  already stored and risks contradicting it.
  A constraint does not have to be its own sentence, or come first, to count.
  It is just as real when it is a trailing clause, an aside, or tucked after
  "and" or "but" at the end of a long message - read the entire message
  before deciding you are done, not just its main clause:
  - "I want something for trail running, need it to handle mud okay, and
    honestly I'm not really into anything too flashy looking either" ->
    goal="trail running shoes", must_have_add=["handles mud"],
    exclusions_add=["flashy looking"] - the exclusion is stated last, as an
    aside ("honestly... either"), not as its own sentence, but it is just as
    real a constraint as the must-have stated before it.
  - "Budget's around 10000, my last pair fell apart after a month so
    durability actually matters more to me than looking good" ->
    budget_amount=10000, priorities_add=["durability matters more than
    looks"] - the priority is implied by the reason given ("fell apart"),
    not stated as a bare instruction, but the shopper is still telling you
    what to weigh higher.
- Fill durable_facts only for something that would still be true on a
  completely different visit, weeks from now - a stable preference or an
  owned item, and only when the shopper stated it themselves. Almost every
  turn should leave this empty. A shopper choosing a size for one order is
  not a durable fact; a shopper saying they always take a US 9 is.

Friction types you may see: {", ".join(str(f) for f in FrictionType)}.

Always answer by calling propose_actions. Do not reply in prose."""


#: The post-execution synthesis pass's own system prompt - deliberately
#: separate from SYSTEM_PROMPT above. This call happens *after* the
#: authoritative result already exists (see ReasoningService.synthesize),
#: so its whole job is different: explain real facts already in hand,
#: never propose anything, never guess at what has not run yet.
_SYNTHESIS_SYSTEM_PROMPT = """You write one short, grounded reply about a
result that has already happened. The facts below are real and already
fetched - never invent, guess, or add anything not listed.

Rules:
- Two to three sentences, plain and warm, present tense describing what is
  true right now ("The X is..."), never future tense about something not
  yet done.
- If the shopper stated a goal, budget, constraint, must-have, exclusion or
  priority, use it to say which of the real facts below actually matters for
  them - not a generic listing of specs. If nothing was stated, describe the
  most useful real difference or two without inventing a preference for them.
- Never state a price, stock level, or attribute that is not in the facts
  given to you.
- Never mention internal systems, action names, or that you are an AI.
- For a comparison: name which one fits better for what they said, and why,
  or say plainly that it depends on what matters most to them if the facts
  do not clearly favour one.
- For a discovery/recommendation list: say what ties the shown options
  together and, if a stated constraint ruled anything out, that it did -
  never claim something is shown that is not in the list.
"""


def _synthesis_prompt(
    kind: str,
    message: str | None,
    items: list[dict],
    shopping_state: dict | None,
) -> str:
    """The compact, plain-text user message for one synthesis call.

    Deliberately small - this is not the full Context Builder (see
    engine/reasoning/context.py). It carries exactly what this one judgement
    needs: the real fetched items, what the shopper said this turn, and the
    current mission state if one has been captured.
    """
    parts: list[str] = []
    if message:
        parts.append(f"THE SHOPPER JUST SAID: {message}")

    if shopping_state:
        bits = []
        if shopping_state.get("goal"):
            bits.append(f"goal: {shopping_state['goal']}")
        if shopping_state.get("budget_amount"):
            bits.append(
                f"budget: {shopping_state['budget_amount']} "
                f"{shopping_state.get('budget_currency') or ''}".strip()
            )
        for field, label in (
            ("must_have", "must have"),
            ("exclusions", "must not have"),
            ("nice_to_have", "would like"),
            ("priorities", "trade-off priorities"),
        ):
            values = shopping_state.get(field)
            if values:
                bits.append(f"{label}: {', '.join(values)}")
        if bits:
            parts.append("THEIR CURRENT MISSION: " + "; ".join(bits))

    label = "THE TWO PRODUCTS, FETCHED JUST NOW" if kind == "comparison" else "THE REAL OPTIONS, FETCHED JUST NOW"
    lines = []
    for item in items:
        bits = [item.get("title", "")]
        if item.get("price"):
            bits.append(str(item["price"]))
        if item.get("availability"):
            bits.append(str(item["availability"]).replace("_", " ").lower())
        if item.get("description"):
            bits.append(str(item["description"]))
        # Deterministic fit evidence, when the mission-aware shortlist
        # (engine/execution/mission_ranking.py) already computed it - real
        # matched keywords/budget facts, never an invented one. Handing this
        # to the model lets it cite the actual reason a product was shown
        # rather than re-deriving one from prose alone.
        if item.get("fit"):
            bits.append("already matched on: " + "; ".join(item["fit"]))
        lines.append("  - " + ", ".join(str(b) for b in bits if b))
    parts.append(f"{label}:\n" + "\n".join(lines))

    parts.append(
        "Write the reply now - two to three sentences, grounded only in the facts above. "
        "Then call write_synthesis with that reply, a true/false personalizes flag stating "
        "whether the reply attributes the pick to something the shopper said, and - "
        "whenever personalizes is true - the exact stated phrase you based it on "
        "(cited_reason must not be null when personalizes is true)."
    )
    return "\n\n".join(parts)


def synthesis_tool() -> dict[str, Any]:
    """The synthesis pass's own tool schema - a third, deliberately narrow
    structural fix after two rounds of regex-based attempts at the same
    problem were each defeated by adversarial rephrasing (see
    `engine/api/chat.py::_honest_synthesis`'s own docstring for the full
    history: a phrase blocklist, then an any-term-mentioned floor, then a
    causal-claim-pattern extractor - all three eventually bypassed by a
    reply worded around whatever the current check specifically looked for).

    Rather than inferring what the model's free text claims from the text
    itself, the model is now asked to state its own claimed reason directly,
    in a field the code can check exactly: `cited_reason` must be the shopper's
    own stated phrase, verbatim or near-verbatim, or null if this reply makes
    no personalization claim at all. `_honest_synthesis` then checks THIS
    field against `shopping_state`'s real stored terms - not the prose.

    **A FIFTH gap, found live: leaving `cited_reason` null was itself the
    bypass.** Nothing forced the model to fill it, so a reply could still
    make an implicit personalization claim in free prose ("reading between
    the lines of what you said about X, Y actually wins out for you") with
    `cited_reason` left null - and the null case was, by design, treated as
    "no claim being made," a free pass. An independent adversarial pass found
    5/5 fresh phrasings defeated it this way. A `personalizes` boolean is now
    ALSO required on every call: the model must explicitly declare whether
    this reply attributes its pick to something the shopper said. `personalizes:
    true` (or the field being missing/malformed, which is treated the same,
    fail-closed) with no valid `cited_reason` is now rejected outright -
    there is no longer a way to get an uncited personalization claim through
    by simply not filling in the optional field, because there are now two
    structured signals that both have to be honestly reconciled, not one that
    can be silently omitted. `personalizes: false` still passes through the
    older free-text backstop checks underneath (any-term-mentioned floor,
    causal-claim-pattern extraction) as a second line of defense against a
    model that lies on the boolean itself - a materially harder and rarer
    failure than simply not filling in a field, and worth verifying
    empirically rather than assuming closed.

    Deliberately has NO action-type, variant, or risk field of any kind - it
    cannot propose an action, cannot be classified by the risk gate, and
    remains exactly the "strictly narrower than reason()" capability the
    synthesis call has always been (see `ReasoningService.synthesize`'s own
    docstring), confirmed by `invariant-guard` for this exact reason each
    time this file changes.

    **A real, previously-undiscovered bug, found live while building the
    sixth fix this session (the semantic relevance judge below): this
    schema, and `relevance_tool()`'s, were both missing the OpenAI
    `{"type": "function", "function": {...}}` request wrapper that
    `propose_tool()` above correctly has - the tool was sent as a bare
    `{"name": ..., "parameters": ...}` dict, which the OpenAI-compatible API
    rejects outright with an HTTP 400.** `ReasoningService.synthesize()`
    already catches `LLMUnavailable` and degrades to the safe fallback
    sentence on ANY provider failure, by design - so this bug was
    completely silent: every real synthesis call under the OpenAI provider
    has been failing and falling back this entire session, and it read
    exactly like the already-documented Groq-throttle variance (a
    `model_reply` with real text from the separate, correctly-wrapped
    `propose_tool()` call, next to a `reply` that fell back to the plain
    engine sentence) rather than a schema bug, until confirmed directly by
    calling the provider and inspecting the raw error. Fixed by wrapping
    both this schema and `relevance_tool()`'s the same way `propose_tool()`
    already is.
    """
    return {
        "type": "function",
        "function": {
        "name": "write_synthesis",
        "description": (
            "Give the final shopper-facing reply and, if it personalizes the "
            "recommendation to something the shopper stated, the exact stated "
            "reason it is based on."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": "The two-to-three sentence reply, grounded only in the given facts.",
                },
                "personalizes": {
                    "type": "boolean",
                    "description": (
                        "True if the reply's wording, in any way, attributes or credits "
                        "the pick to something the shopper said or stated as a preference "
                        "(e.g. 'since you said...', 'because you prefer...', 'this matches "
                        "your need for...', 'given how much you care about...'). False if "
                        "the reply only states real facts/differences with no such "
                        "attribution to the shopper's own words."
                    ),
                },
                "cited_reason": {
                    "type": ["string", "null"],
                    "description": (
                        "REQUIRED to be a real, non-null value whenever personalizes is "
                        "true. The shopper's own stated phrase (verbatim or near-verbatim "
                        "from THEIR CURRENT MISSION above) that this reply's personalization "
                        "is actually based on. Must be null when personalizes is false."
                    ),
                },
            },
            "required": ["reply", "personalizes"],
        },
        },
    }


_RELEVANCE_SYSTEM_PROMPT = (
    "You judge whether a shop's real catalog plausibly carries something a "
    "shopper described, when a literal keyword/category match already failed "
    "to decide it. You are given the shop's REAL declared categories and a "
    "REAL sample of its actual product titles/descriptions - never invent a "
    "product or claim one exists beyond what is listed. Judge based on "
    "genuine semantic/use-case fit, not literal word overlap (that already "
    "failed, which is why you were asked). A coffee shop plausibly carries "
    "'something smooth and not too bitter' even though those words never "
    "appear in any title. A running-shoe shop does not plausibly carry a "
    "kayak, a tent, or ski goggles, even if one shared word coincidentally "
    "matched something in its catalog. Answer only about whether the "
    "CATEGORY of thing described is plausible for this shop to sell at all - "
    "not whether a specific named product exists."
)


def relevance_judgment_prompt(
    need: str, categories: list[str], sample_titles: list[str]
) -> str:
    """Prompt for the semantic relevance judge (`relevance_tool()` below).

    Deliberately narrow: this call cannot see anything not already fetched
    from the real adapter (`categories`/`sample_titles` are always real,
    never invented here or by the model), and its only output is a yes/no
    plausibility judgment used to gate an existing fallback decision - never
    a product list, never an action, never anything the risk gate would need
    to see. See `engine/execution/relevance.py::shop_carries`'s own docstring
    for why a purely literal keyword/category check cannot close this gap on
    its own (a coffee-shop taste question sharing no literal words with any
    title; a weak coincidental single-word match surviving for a clearly
    unrelated named product) - the two shapes this call exists to catch.
    """
    cats = ", ".join(categories[:30]) or "(none declared)"
    titles = "\n".join(f"- {t}" for t in sample_titles[:40]) or "(no sample available)"
    return (
        f"Shopper described wanting: {need!r}\n\n"
        f"This shop's real declared categories: {cats}\n\n"
        f"A real sample of this shop's actual products:\n{titles}\n\n"
        "Does this shop plausibly carry something matching what the shopper "
        "described, based on genuine category/use-case fit? Answer False "
        "unless there is a real, specific category or use-case match - a "
        "product merely sharing one incidental word with the description "
        "(e.g. a water bottle for a 'kayak' request, because both involve "
        "'water') is NOT a match; judge the shopper's actual need, not "
        "coincidental word overlap. Call judge_relevance with your answer."
    )


def relevance_tool() -> dict[str, Any]:
    """Tool schema for the semantic relevance judge. No action-type, variant,
    or risk field of any kind - it cannot propose an action or be classified
    by the risk gate, the same narrow-capability guarantee `synthesis_tool()`
    already carries, confirmed by `invariant-guard` for the same reason each
    time this file changes. Its only effect anywhere in the system is gating
    whether an existing, already-real candidate/fallback list is kept or
    discarded - it can never cause a product to be invented or shown that
    was not already fetched from the real adapter.
    """
    return {
        "type": "function",
        "function": {
            "name": "judge_relevance",
            "description": "State whether this shop plausibly carries something matching the shopper's description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plausible": {
                        "type": "boolean",
                        "description": "True if this shop plausibly carries something matching the description, based on real category/use-case fit.",
                    },
                },
                "required": ["plausible"],
            },
        },
    }


#: TWENTIETH-gap fix: `judge_relevance` only ever answers "does this shop
#: plausibly carry a CATEGORY of thing" - it was never asked to pick WHICH
#: real candidate best fits, so a shop confirmed to plausibly carry
#: "something smooth and not too bitter" still showed all four of its real
#: coffees undifferentiated every time, regardless of phrasing ("a mellow
#: coffee", "coffee with a gentle taste", "something for someone who
#: doesn't like bitter coffee" all produced the identical list). Reproduced
#: live and confirmed by a real shopper's own testing, not just synthetic
#: phrasing - a keyword/stem matcher structurally cannot rank a taste
#: description against product text with no shared literal word, which is
#: exactly why this needs the same LLM judgment `judge_relevance` already
#: uses for the yes/no case, extended here to a selection among real,
#: already-fetched candidates rather than a plain boolean.
_BEST_MATCH_SYSTEM_PROMPT = (
    "You pick which ONE of a shop's real, already-fetched products best "
    "matches a shopper's description, when a literal keyword search already "
    "failed to find one. You are given the shopper's own words and a REAL "
    "list of this shop's actual products with their real titles and "
    "descriptions - you may only ever return a product_id from that exact "
    "list, never invent one, never describe a product not shown to you. "
    "Judge genuine semantic/use-case fit (a coffee described as 'red apple "
    "and caramel, forgiving on any brewer' plausibly matches 'something "
    "mellow and easy to drink'), not literal word overlap - that already "
    "failed, which is why you were asked. If more than one candidate seems "
    "roughly equally good, prefer the one whose actual description most "
    "directly speaks to what the shopper said. If genuinely NONE of the "
    "real candidates fit better than the others - the shopper's request is "
    "too generic to distinguish between them, or none really fit at all - "
    "say so honestly rather than picking one arbitrarily."
)


def best_match_prompt(need: str, candidates: list[dict]) -> str:
    """Prompt for the semantic best-match judge (`best_match_tool()` below).

    `candidates` is always the real, already-fetched product list a
    deterministic check already found plausible for this shop to carry
    (via `judge_relevance`) but could not rank - each entry a dict with
    real `product_id`/`title`/`description` fields, never invented here.
    The model's only job is to return one of these ids, or say none stand
    out - it cannot add a product, alter a description, or see anything
    not already real and already fetched.
    """
    listed = "\n".join(
        f"- id={c['product_id']}: {c['title']} - {c.get('description') or '(no description)'}"
        for c in candidates[:12]
    )
    return (
        f"Shopper described wanting: {need!r}\n\n"
        f"Real candidates this shop actually has (already confirmed plausible "
        f"for this shop to carry):\n{listed}\n\n"
        "Which ONE product_id best matches what the shopper described, based "
        "on its real title/description? Only return an id from the list "
        "above. If none genuinely stands out as a better fit than the "
        "others, set matches to false and leave product_id empty rather "
        "than guessing. Call judge_best_match with your answer."
    )


def best_match_tool() -> dict[str, Any]:
    """Tool schema for the semantic best-match judge. Only ever selects
    among ids already present in the real candidate list handed to it in
    the prompt - the caller (`ReasoningService.judge_best_match`) validates
    the returned id is actually one of those real candidates before
    trusting it, the same never-trust-a-bare-id discipline already used
    for `cited_reason`/category validation elsewhere in this codebase. No
    action-type, variant, or risk field - cannot propose an action or be
    classified by the risk gate.
    """
    return {
        "type": "function",
        "function": {
            "name": "judge_best_match",
            "description": "Pick which real candidate product best matches the shopper's description, if any genuinely stands out.",
            "parameters": {
                "type": "object",
                "properties": {
                    "matches": {
                        "type": "boolean",
                        "description": "True if one real candidate genuinely stands out as the best fit.",
                    },
                    "product_id": {
                        "type": "string",
                        "description": "The id of the best-matching real candidate, exactly as given. Required when matches is true.",
                    },
                },
                "required": ["matches"],
            },
        },
    }


#: Shopper V2 Phase 4 continuation - the general, non-phrase-specific cart-
#: intent interpretation layer. Added here (engine/reasoning/), not inside
#: engine/shopper_v2/, for the same reason judge_relevance/judge_best_match
#: live here: this is a new bounded LLM capability, and this module is
#: where every existing bounded LLM capability's prompt/tool pair already
#: lives - putting a new one here reuses the established pattern rather
#: than inventing a parallel one inside shopper_v2/. V1 does not call this;
#: purely additive.
#:
#: The general problem this solves: the existing `propose_actions` tool
#: (used by both V1's `reason()` and V2's `understand()`) is a general-
#: purpose action proposer, and reliably classifying "add that", "add the
#: first one", "I'll take this", "put two of those in my cart", "get me
#: the Trailblazer", or a bare "add it" as ADD_TO_CART/PROPOSE_CART_CHANGE
#: is a narrower, different task than that tool's own job - exactly the
#: same reasoning that already justified giving relevance judgment and
#: best-match selection their own narrow tools instead of overloading
#: `propose_actions` further. This tool is deliberately as narrow as those:
#: it interprets a REFERENCE, never an action. It has no field for an
#: ActionType, a variant, a price, or anything the risk gate would need to
#: see - only whether the message plausibly refers to one of the REAL,
#: already-shown candidates it is given, and which one, structurally
#: (position/pronoun/name), never a product id it invents itself.
_CART_REFERENCE_SYSTEM_PROMPT = (
    "You interpret whether a shopper's message refers to changing their "
    "cart using one of two REAL lists you are given: products currently "
    "SHOWN to them (for adding something new) and lines currently IN their "
    "cart (for removing or changing the quantity of something already "
    "there, or clearing the whole cart). You may only ever refer to one of "
    "THESE by its position in the relevant list or by name matching one of "
    "its titles; you can never name, invent, or imply a product not in "
    "these exact lists. Determine the OPERATION first: 'add' (add a shown "
    "product - 'add that to my cart', 'I'll take this', 'get me the "
    "Trailblazer', 'put two of those in my cart'), 'remove' (take a cart "
    "line out - 'remove that', 'remove the shoes', 'take off the first "
    "item'), 'set_quantity' (change how many of a cart line - 'make that "
    "quantity two', 'change the socks to three'), or 'clear' (empty the "
    "whole cart - 'clear my cart', 'empty my basket'). A plain question, a "
    "search for something new, a comparison request, or any message that "
    "does not ask to change the cart is NOT cart intent - answer false "
    "rather than guess. For 'remove'/'set_quantity', resolve the reference "
    "against the CART lines list, not the shown-products list. When the "
    "message clearly expresses cart intent but you cannot tell WHICH item "
    "it means, still answer true for intent but leave the reference fields "
    "unset rather than picking one arbitrarily - a human will ask which "
    "one, not you. 'clear' needs no reference at all."
)


def cart_reference_prompt(
    message: str,
    active_products: list[dict],
    history: list[str] | None,
    cart_lines: list[dict] | None = None,
) -> str:
    """Prompt for the cart-reference interpreter (`cart_reference_tool()`
    below). `active_products` is always the real, already-shown/verified
    product list from the current `ShoppingStateV2`; `cart_lines` is the
    real, current contents of the shopper's actual cart (fetched fresh by
    the caller) - neither is ever invented here. `history` (a few recent
    turns, the same bound the rest of this codebase's Context Builder
    already uses) is included only so a bare 'add it'/'remove it' can be
    read against what was just discussed, never as a place new products
    could be introduced from."""
    shown = "\n".join(
        f"  {i + 1}. id={p['product_id']}: {p['title']}"
        for i, p in enumerate(active_products[:10])
    )
    cart_list = "\n".join(
        f"  {i + 1}. id={l['product_id']}: {l['title']} (qty {l.get('quantity', 1)})"
        for i, l in enumerate((cart_lines or [])[:20])
    )
    recent = "\n".join(f"  - {h}" for h in (history or [])[-4:])
    parts = [f"Shopper's message: {message!r}"]
    if recent:
        parts.append(f"Recent conversation:\n{recent}")
    parts.append(f"Products currently SHOWN to the shopper (for 'add'):\n{shown or '  (none)'}")
    parts.append(f"Lines currently IN the shopper's cart (for 'remove'/'set_quantity'/'clear'):\n{cart_list or '  (empty)'}")
    parts.append(
        "is_cart_intent is true when the shopper has made a clear DECISION "
        "to add, remove, change the quantity of, or clear something right "
        "now - 'I'll take this', 'add that', 'get me the first one', "
        "'I'll take it', 'remove the shoes', 'make that two', 'clear my "
        "cart' are all clearly is_cart_intent=true, even when the only "
        "reference is a bare pronoun like 'this'/'it'/'that one' next to "
        "one clearly identifiable item - a decision does not need extra "
        "verbs beyond the one it already has.\n\n"
        "is_cart_intent is false when the shopper is only stating or "
        "restating what kind of product they want, want to see, or are "
        "looking for - a search/discovery statement, not a decision - "
        "EVEN IF it happens to name the same category, or the same "
        "product, as something already shown. A live, real bug this "
        "distinction exists to prevent: a shopper already shown a running "
        "shoe who then said 'I need running shoes' again (still just "
        "describing what they want, no decision made) was wrongly read as "
        "is_cart_intent=true, fabricating a cart proposal nobody asked "
        "for. The test is whether the shopper is DECIDING to act versus "
        "still DESCRIBING what they want - 'I need running shoes'/'show me "
        "shoes'/'what do you have' are false; 'I'll take this'/'add that'/"
        "'I want to buy it' are true, because the second group states a "
        "decision about a specific thing while the first group only names "
        "a category or repeats a want.\n\n"
        "What operation does this message ask for - add/remove/set_quantity/"
        "clear/none? If add/remove/set_quantity and you can tell which item, "
        "give its 1-based position in the RELEVANT list above "
        "(reference_type='ordinal' with position), or its name "
        "(reference_type='name' with named_hint set to the exact title text "
        "you matched against), or 'pronoun' if it's a bare reference like "
        "'it'/'that one' with nothing else to go on. If a quantity is "
        "stated, give it; otherwise leave it unset. Call "
        "interpret_cart_reference."
    )
    return "\n\n".join(parts)


def cart_reference_tool() -> dict[str, Any]:
    """Tool schema for the cart-reference interpreter. Deliberately has no
    field for an ActionType, a product id it could invent, a variant, or a
    price - `position`/`named_hint` are only ever resolved against the
    REAL `active_products`/`cart_lines` lists by the caller
    (`engine/shopper_v2/intent_resolution.py`), which independently
    verifies the reference before anything is proposed; this tool cannot
    itself cause an id to reach the Decision Engine or Risk Gate. No
    action-type, variant, or risk field - the same narrow-capability
    guarantee `relevance_tool()`/`best_match_tool()` already carry.
    """
    return {
        "type": "function",
        "function": {
            "name": "interpret_cart_reference",
            "description": "State whether the shopper's message asks to change their cart, how (add/remove/set_quantity/clear), and which item if identifiable.",
            "parameters": {
                "type": "object",
                "properties": {
                    "is_cart_intent": {
                        "type": "boolean",
                        "description": (
                            "True ONLY if the shopper is explicitly asking to change "
                            "their cart right now (add/remove/change quantity/clear). "
                            "False for a message that merely states or restates what "
                            "product or category they want or are looking for, even "
                            "if it names something already shown - that is still "
                            "discovery, not cart intent."
                        ),
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["add", "remove", "set_quantity", "clear", "none"],
                        "description": "Which cart operation the message asks for. 'none' if is_cart_intent is false.",
                    },
                    "reference_type": {
                        "type": "string",
                        "enum": ["ordinal", "pronoun", "name", "none"],
                        "description": "How the target item was referred to, if applicable. 'none' if is_cart_intent is false, operation is 'clear', or the reference could not be resolved.",
                    },
                    "position": {
                        "type": "integer",
                        "description": "1-based position in the relevant list (shown products for 'add', cart lines otherwise), when reference_type is 'ordinal'.",
                    },
                    "named_hint": {
                        "type": "string",
                        "description": "The exact title text (or a clear substring of it) the shopper's words matched, when reference_type is 'name'.",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "The quantity requested, only if the shopper stated one explicitly.",
                    },
                },
                "required": ["is_cart_intent", "operation", "reference_type"],
            },
        },
    }


_MISSION_REFERENCE_SYSTEM_PROMPT = (
    "You interpret one shopper message against a multi-item shopping "
    "mission - a trip with several independent things to buy (e.g. "
    "'running shoes', 'socks', 'a hydration bottle'). You are given the "
    "REAL list of items already tracked this conversation, each shown only "
    "by its 1-based position and a short label - you never see or invent a "
    "real product or an internal id. Decide two independent things. "
    "First: does this message introduce any BRAND NEW item(s) not already "
    "in the list ('I need running shoes, socks and a hydration bottle' "
    "with an empty list -> three new labels; 'I also need a water bottle' "
    "with shoes already tracked -> one new label)? Give each as a short "
    "noun-phrase label, not a full sentence. Second: does this message "
    "REFINE or REFER TO an item already in the list ('make the shoes "
    "lightweight', 'for the socks, I want black', 'under 5000 for the "
    "shoes', 'I've decided on that one')? If so, resolve which one the "
    "same way you would resolve a cart reference - by its 1-based position "
    "(reference_type='ordinal'), by a name/label match "
    "(reference_type='name' with named_hint set to the matching label "
    "text), or as a bare pronoun with nothing else to go on "
    "(reference_type='pronoun') - and state what changed: any new "
    "constraint, must-have, nice-to-have, exclusion, or trade-off priority "
    "for THAT item only. Set item_completed true only when the shopper "
    "clearly indicated that item is settled/decided. Separately, decide "
    "propose_basket: true ONLY when the shopper explicitly asks to add the "
    "tracked items to their cart now ('add these to my cart', 'I'll take "
    "all of these', 'let's get the shoes and socks', 'add them all') - "
    "never inferred from silence or from merely mentioning an item. A "
    "message that is purely about the mission as a whole, or names "
    "nothing new and refers to nothing existing (e.g. a general browsing "
    "question), should return empty new_item_labels and "
    "reference_type='none' - never guess which item was meant."
)


def mission_reference_prompt(
    message: str,
    current_items: list[dict],
    history: list[str] | None,
) -> str:
    """Prompt for the mission-item interpreter (`mission_reference_tool()`
    below). `current_items` is always the real, already-tracked mission
    item list (`{"item_id": ..., "label": ...}` per item) from the current
    `ShoppingStateV2.mission_items` - the model is shown only positions and
    labels, never the real `item_id` (see `mission_reference_tool()`'s own
    docstring for why), so it can never echo back an id that was not
    independently resolved by the caller. `history` is the same bounded
    recent-turns window the rest of this codebase's Context Builder already
    uses."""
    items_list = "\n".join(
        f"  {i + 1}. {item['label']}" for i, item in enumerate(current_items[:10])
    )
    recent = "\n".join(f"  - {h}" for h in (history or [])[-4:])
    parts = [f"Shopper's message: {message!r}"]
    if recent:
        parts.append(f"Recent conversation:\n{recent}")
    parts.append(f"Mission items already tracked this conversation:\n{items_list or '  (none yet)'}")
    parts.append(
        "Does this message introduce any brand-new item(s) not in that "
        "list? List each as a short label, or leave empty. Does it refine "
        "or refer to an item already in the list? If so, give its "
        "reference (ordinal position / name / pronoun) and what changed "
        "for it (constraints/must_have/nice_to_have/exclusions/priorities "
        "additions, or item_completed). Does it ask to add the tracked "
        "items to the cart now (propose_basket)? Call "
        "interpret_mission_items."
    )
    return "\n\n".join(parts)


def mission_reference_tool() -> dict[str, Any]:
    """Tool schema for the mission-item interpreter. Deliberately shown no
    real mission-item id (only position/label) and has no field for one -
    `reference_type`/`position`/`named_hint` are only ever resolved against
    the REAL current item list by the caller
    (`engine/shopper_v2/missions.py`), which independently validates the
    reference before writing anything to state. No product id, no
    ActionType, no risk field - the same narrow-capability guarantee every
    other bounded judgment tool in this module carries."""
    return {
        "type": "function",
        "function": {
            "name": "interpret_mission_items",
            "description": "State any brand-new mission items this message introduces, and how it refines or refers to an item already being tracked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "new_item_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short noun-phrase labels for brand-new items this message introduces. Empty if none.",
                    },
                    "reference_type": {
                        "type": "string",
                        "enum": ["ordinal", "pronoun", "name", "none"],
                        "description": "How this message refers to an EXISTING tracked item, if at all. 'none' if it introduces only new items or refers to nothing existing.",
                    },
                    "position": {
                        "type": "integer",
                        "description": "1-based position in the tracked-items list, when reference_type is 'ordinal'.",
                    },
                    "named_hint": {
                        "type": "string",
                        "description": "The label text (or a clear substring of it) the shopper's words matched, when reference_type is 'name'.",
                    },
                    "constraints_add": {"type": "array", "items": {"type": "string"}},
                    "must_have_add": {"type": "array", "items": {"type": "string"}},
                    "nice_to_have_add": {"type": "array", "items": {"type": "string"}},
                    "exclusions_add": {"type": "array", "items": {"type": "string"}},
                    "priorities_add": {"type": "array", "items": {"type": "string"}},
                    "item_completed": {
                        "type": "boolean",
                        "description": "True only if the shopper clearly indicated the referenced item is now settled/decided.",
                    },
                    "propose_basket": {
                        "type": "boolean",
                        "description": "True only if the shopper explicitly asks to add the tracked mission items to their cart now.",
                    },
                },
                "required": ["new_item_labels", "reference_type"],
            },
        },
    }