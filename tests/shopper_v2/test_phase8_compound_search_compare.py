"""A comparison whose other side is the result of a search stated in the SAME
message ("show me waterproof trail shoes under 10000, and compare them with the
Trailblazer") is a compound turn: search first, then compare.

Live root cause (real provider, repeated): the model reads that sentence two
ways. As `discover` + `secondary_operation=compare` the engine handled it. As
`compare` (or a relationship/compound_comparison question) with ONE resolved
side it did not: the compare/answer route needs both sides to exist already, but
"them" is the not-yet-retrieved search result, so the shopper got "I can see one
side of that..." or "I don't have two products in view" in roughly half the
runs. `verify()` now normalizes the second reading to the first, structurally:
a comparison with exactly one resolved side AND a newly stated search goal.

Model-free: hand-built claims and context, no network, no key.

    python tests/shopper_v2/test_phase8_compound_search_compare.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


from engine.reasoning.service import InterpretationResult, SecondaryOperation, SecondaryReference  # noqa: E402
from engine.shopper_v2.conversation_context import (  # noqa: E402
    CartSnapshot, ConversationContext, Topic,
)
from engine.shopper_v2.verification import verify  # noqa: E402
from shared.models.commerce import Money, Product  # noqa: E402


def P(pid, title, price, desc=""):
    return Product(product_id=pid, title=title, price=Money(amount=price, currency="INR"),
                   availability="IN_STOCK", description=desc)


trailblazer = P("P1001", "Trailblazer Running Shoe", 4299, "lightweight trainer")
marathon = P("P1002", "Marathon Pro Racing Shoe", 12499, "carbon plate")
canvas = P("P1003", "Everyday Canvas Sneaker", 1899, "canvas")
products = [trailblazer, marathon, canvas]

# the shopper was browsing lightweight trail shoes; the Trailblazer is in view
context = ConversationContext(
    session_id="s", connection_id="conn_demo",
    active_topic=Topic(topic_id="t1", kind="discovery", label="trail shoes",
                       product_ids=["P1001", "P1002", "P1003"]),
    cart_snapshot=CartSnapshot(cart_id="c", lines=[]),
)

GOAL = {"goal": "waterproof trail shoes", "budget_amount": 10000, "priorities_remove": ["lightweight"]}


def claim(**kw):
    base = dict(
        capability="compare", context_action="continue", reference_kind="named",
        reference_scope="named", reference_names=["Trailblazer"],
        conversational_repair="correction", question_type="relationship",
        shopping_state=dict(GOAL),
    )
    base.update(kw)
    return InterpretationResult(**base)


MSG = "Actually, I don't want lightweight anymore. Show me waterproof trail shoes under 10000, and compare them with the Trailblazer."

print("Normalized: one resolved side + a newly stated search goal")
v = verify(claim(), context, products, MSG)
check("compare + relationship + goal -> discover", v.capability == "discover")
check("  ...question_type becomes retrieve", v.question_type == "retrieve")
check("  ...a secondary compare carries the named product", v.secondary_operation is not None
      and v.secondary_operation.capability == "compare" and v.secondary_operation.reference_names == ["Trailblazer Running Shoe"])  # the REAL title, not the model's shorthand
check("  ...the named side stays resolved against the real product", v.resolved_reference is not None
      and v.resolved_reference.product_ids == ["P1001"])

v = verify(claim(question_type="retrieve", secondary_operation=SecondaryOperation(capability="compare", reference_names=["Trailblazer"])),
           context, products, MSG)
check("compare + retrieve + goal (model also filled the secondary) -> discover, secondary kept",
      v.capability == "discover" and v.secondary_operation is not None and v.secondary_operation.capability == "compare")

v = verify(claim(question_type="compound_comparison"), context, products, MSG)
check("compound_comparison + goal -> discover", v.capability == "discover" and v.question_type == "retrieve")

# The live claims do not always state a `goal`: the search intent can sit only in
# the requirements ("waterproof") plus a budget. That is still a new search.
v = verify(claim(shopping_state={"budget_amount": 10000, "constraints_add": ["waterproof"], "priorities_remove": ["lightweight"]}),
           context, products, MSG)
check("no goal but a new requirement (constraints_add) -> discover + secondary compare",
      v.capability == "discover" and v.secondary_operation is not None)
v = verify(claim(shopping_state={"budget_amount": 10000, "must_have_add": ["waterproof"]}), context, products, MSG)
check("no goal but a new must-have -> discover + secondary compare", v.capability == "discover")
v = verify(claim(shopping_state={"budget_amount": 10000}), context, products, "compare the Trailblazer within 10000")
check("a budget alone is not a new search: unchanged", v.capability == "compare" and v.secondary_operation is None)

# Shapes observed live from the real model for the SAME sentence:
#  - a `secondary_reference` for "them" (positional/descriptive, not a named
#    product) on top of the named Trailblazer;
#  - capability "mission" with a secondary compare.
v = verify(claim(shopping_state={"budget_amount": 10000, "must_have_add": ["waterproof"]},
                 secondary_reference=SecondaryReference(reference_scope="active_topic", reference_kind="all"),
                 secondary_operation=SecondaryOperation(capability="compare", reference_names=["Trailblazer"])),
           context, products, MSG)
check("a positional 'them' secondary (the old view) does not stop the search-then-compare reading",
      v.capability == "discover" and v.secondary_operation is not None)
v = verify(claim(capability="mission", question_type="retrieve", shopping_state={"must_have_add": ["waterproof"]},
                 secondary_operation=SecondaryOperation(capability="compare", reference_names=["Trailblazer"])),
           context, products, MSG)
check("mission + secondary compare + one resolved side + new requirement -> discover", v.capability == "discover")
v = verify(claim(shopping_state={"must_have_add": ["waterproof"]},
                 secondary_reference=SecondaryReference(reference_scope="named", reference_kind="named", reference_names=["Marathon"])),
           context, products, "compare the Trailblazer with the Marathon, I want waterproof")
check("TWO explicitly NAMED products are both resolved: still a plain compare",
      v.capability == "compare" and v.secondary_operation is None)

# Live (real provider, repeated): the model fills the second side with the SAME
# named product ("the Trailblazer"), so both sides resolve to ONE product. That
# is one side, not two.
v = verify(claim(shopping_state={"budget_amount": 10000, "must_have_add": ["waterproof"]},
                 secondary_reference=SecondaryReference(reference_scope="named", reference_kind="named", reference_names=["Trailblazer"])),
           context, products, MSG)
check("both sides resolve to the SAME single product -> still search-then-compare",
      v.capability == "discover" and v.secondary_operation is not None)
v = verify(claim(shopping_state={"goal": "waterproof trail shoes", "budget_amount": 10000},
                 secondary_reference=SecondaryReference(reference_scope="last_rendered", reference_kind="named", reference_names=["Trailblazer"])),
           context, products, MSG)
check("same single product via last_rendered scope -> still search-then-compare", v.capability == "discover")

print("\nPrimary search text of a compound turn")
from engine.shopper_v2.graph import compound_primary_query  # noqa: E402
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402
from engine.shopper_v2.understanding import StateDelta  # noqa: E402

d = StateDelta(goal="waterproof trail shoes", constraints_add=["waterproof"])
check("built from the stated goal + requirements, never the message (no 'Trailblazer', no duplicates)",
      compound_primary_query(d, ShoppingStateV2(session_id="s", connection_id="conn_demo")) == "waterproof trail shoes")
d = StateDelta(constraints_add=["waterproof"])
check("requirements only: uses them with the already-established goal",
      compound_primary_query(d, ShoppingStateV2(session_id="s", connection_id="conn_demo", goal="trail shoes")) == "trail shoes waterproof")
d = StateDelta(constraints_add=["waterproof"], priorities_remove=["lightweight"])
check("words the shopper explicitly removed are dropped from the established goal",
      compound_primary_query(d, ShoppingStateV2(session_id="s", connection_id="conn_demo", goal="lightweight trail shoes")) == "trail shoes waterproof")
d = StateDelta(constraints_add=["waterproof"], nice_to_have_remove=["Lightweight"])
check("  ...also for a removed nice-to-have, case-insensitively",
      compound_primary_query(d, ShoppingStateV2(session_id="s", connection_id="conn_demo", goal="lightweight trail shoes")) == "trail shoes waterproof")
check("nothing structured to search with -> None (caller keeps its normal query)",
      compound_primary_query(StateDelta(), ShoppingStateV2(session_id="s", connection_id="conn_demo")) is None)

print("\nLeft alone: every other shape")
v = verify(claim(shopping_state={}), context, products, "compare the Trailblazer")
check("one side, NO new goal: unchanged (the honest 'need a second thing' path stays)",
      v.capability == "compare" and v.secondary_operation is None)

v = verify(claim(reference_kind="ordinal", reference_scope="active_topic", reference_names=[],
                 reference_positions=[1, 2], shopping_state={}), context, products, "compare the first two")
check("plural reference, no goal: unchanged", v.capability == "compare" and v.secondary_operation is None)

v = verify(claim(reference_kind="ordinal", reference_scope="active_topic", reference_names=[],
                 reference_positions=[1, 2]), context, products, "compare the first two waterproof trail shoes")
check("two resolved sides even with a goal word: unchanged (both sides already exist)",
      v.capability == "compare" and v.secondary_operation is None)

v = verify(claim(capability="discover", question_type="retrieve",
                 secondary_operation=SecondaryOperation(capability="compare", reference_names=["Trailblazer"])),
           context, products, MSG)
check("already discover + secondary: unchanged", v.capability == "discover" and v.secondary_operation.reference_names == ["Trailblazer"])

v = verify(claim(capability="recommendation", question_type="recommendation"), context, products, "recommend one")
check("non-compare capability: unchanged", v.capability == "recommendation")

print()
print(f"  {failures} FAILURE(S)" if failures else "  all compound search+compare checks passed")
sys.exit(1 if failures else 0)
