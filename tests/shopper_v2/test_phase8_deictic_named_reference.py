"""A pronoun the model labels as a product NAME must still resolve.

Live root cause (real provider, ~30% of runs): for "add it" right after
"recommend one", the model emitted `reference_kind="named"`,
`reference_names=["it"]`. `resolve_reference` looked for a product titled "it",
found none, and the shopper was asked "I don't see that among what I can add -
did you mean Starter Road Running Shoe?" about the one product in view.

A name is only a name if its words can occur in this store's product titles or
categories. When none can (per-merchant, data-grounded - no pronoun list), it
is a deictic reference: with exactly ONE product in the scope's view it means
that product; with several it stays ambiguous (the shopper is asked). A name
that IS a real catalog word ("the Marathon") is never rebound.

Model-free: hand-built claims/context. Needs no network and no key.

    python tests/shopper_v2/test_phase8_deictic_named_reference.py
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


from engine.reasoning.service import InterpretationResult  # noqa: E402
from engine.shopper_v2 import vocabulary  # noqa: E402
from engine.shopper_v2.conversation_context import CartSnapshot, ConversationContext, Topic  # noqa: E402
from engine.shopper_v2.verification import resolve_reference  # noqa: E402
from shared.models.commerce import Money, Product  # noqa: E402


def P(pid, title, price, cats=()):
    return Product(product_id=pid, title=title, price=Money(amount=price, currency="INR"),
                   availability="IN_STOCK", description="", categories=list(cats))


starter = P("P1009", "Starter Road Running Shoe", 1999, ["Footwear"])
trailblazer = P("P1001", "Trailblazer Running Shoe", 4299, ["Footwear"])
marathon = P("P1002", "Marathon Pro Racing Shoe", 12499, ["Footwear"])
catalog = [starter, trailblazer, marathon]
CID = "conn_deictic_test"
vocabulary._CACHE[CID] = vocabulary.build_vocabulary(catalog, ["Footwear"], "Northfield Running Co.")


def ctx(product_ids):
    return ConversationContext(
        session_id="s", connection_id=CID,
        active_topic=Topic(topic_id="t", kind="discovery", label="running shoes", product_ids=list(product_ids)),
        cart_snapshot=CartSnapshot(cart_id="c", lines=[]),
    )


def claim(names, scope="last_rendered", **kw):
    base = dict(capability="cart_action", context_action="continue", reference_kind="named",
                reference_scope=scope, reference_names=list(names), cart_operation="add")
    base.update(kw)
    return InterpretationResult(**base)


print("A pronoun labelled as a name")
r = resolve_reference(claim(["it"]), ctx(["P1009"]), [starter], "add it")
check("'it' with ONE product in view resolves to that product", r.product_ids == ["P1009"])
r = resolve_reference(claim(["this"]), ctx(["P1009"]), [starter], "add this")
check("'this' (any non-catalog word) resolves the same way - no pronoun list", r.product_ids == ["P1009"])
r = resolve_reference(claim(["it"], scope="active_topic"), ctx(["P1009"]), [starter], "add it")
check("also from the active-topic scope", r.product_ids == ["P1009"])
r = resolve_reference(claim(["it"]), ctx(["P1009", "P1001"]), [starter, trailblazer], "add it")
check("with SEVERAL products in view it stays ambiguous (never picks one)", r.product_ids == [])

print("\nReal product names are never rebound")
r = resolve_reference(claim(["Marathon"]), ctx(["P1009"]), [starter], "add the Marathon")
check("a real catalog word that is not in view does NOT bind to the lone product in view", r.product_ids == [])
r = resolve_reference(claim(["Starter"]), ctx(["P1009", "P1001"]), [starter, trailblazer], "add the Starter")
check("a name that matches still resolves normally", r.product_ids == ["P1009"])

print("\nConservative when the store vocabulary is unknown")
vocabulary._CACHE.pop(CID, None)
r = resolve_reference(claim(["it"]), ctx(["P1009"]), [starter], "add it")
check("no cached vocabulary -> behaviour unchanged (no guess)", r.product_ids == [])

print()
print(f"  {failures} FAILURE(S)" if failures else "  all deictic named-reference checks passed")
sys.exit(1 if failures else 0)
