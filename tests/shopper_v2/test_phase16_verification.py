"""Phase 3 of the Rev. 2 architecture migration: `verification.py`.

Pure, synchronous, no model call, no adapter call - deterministic, fast.
Run: python -m tests.shopper_v2.test_phase16_verification
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from engine.reasoning.service import DurableFactCandidate, InterpretationResult  # noqa: E402
from engine.shopper_v2.conversation_context import ConversationContext  # noqa: E402
from engine.shopper_v2.state import LastRendered, Topic  # noqa: E402
from engine.shopper_v2.verification import resolve_context_action, resolve_reference, verify  # noqa: E402
from shared.models import Money, Product  # noqa: E402

_checks = []


def check(label, cond):
    _checks.append((label, bool(cond)))


def _product(pid, title, price=None, rating=None):
    return Product(
        product_id=pid, title=title,
        price=Money(amount=price, currency="INR") if price is not None else None,
        rating=rating,
    )


def _context(*, active=None, paused=None, last_rendered=None):
    return ConversationContext(
        session_id="s1", connection_id="conn_demo",
        active_topic=active, paused_topics=paused or [], last_rendered=last_rendered,
    )


def _claim(**overrides):
    base = dict(capability="discover", context_action="continue", rationale="")
    base.update(overrides)
    return InterpretationResult(**base)


# --- resolve_context_action ------------------------------------------------

active = Topic(topic_id="t2", kind="discovery", label="socks", product_ids=["S1"], state="active")
paused_shoes = Topic(topic_id="t1", kind="discovery", label="running shoes", product_ids=["P1", "P2"], state="paused")
ctx = _context(active=active, paused=[paused_shoes])

action, target = resolve_context_action(_claim(context_action="resume", context_target_label="running shoes"), ctx)
check("resume claim matching a real paused topic's label -> resume + real topic_id", action == "resume" and target == "t1")

ctx_two_paused = _context(active=active, paused=[paused_shoes, Topic(topic_id="t3", kind="discovery", label="jackets", product_ids=["J1"], state="paused")])
action, target = resolve_context_action(_claim(context_action="resume", context_target_label="something nonexistent"), ctx_two_paused)
check("resume claim naming NO real paused topic, with 2+ real candidates -> never guessed, downgrades to push", action == "push" and target is None)

action, target = resolve_context_action(_claim(context_action="resume", context_target_label="something nonexistent"), ctx)
check("resume claim naming NO real paused topic, with exactly 1 real candidate -> resumes it (no ambiguity possible)", action == "resume" and target == "t1")

action, target = resolve_context_action(_claim(context_action="resume", context_target_label="socks"), ctx)
check("resume claim actually echoing the ACTIVE topic's own label -> continue, not a fabricated resume", action == "continue" and target == "t2")

action, target = resolve_context_action(_claim(context_action="push"), ctx)
check("push claim passes through untouched", action == "push" and target is None)

action, target = resolve_context_action(_claim(context_action="continue"), ctx)
check("continue claim -> the real active topic id, never invented", action == "continue" and target == "t2")

empty_ctx = _context()
action, target = resolve_context_action(_claim(context_action="continue"), empty_ctx)
check("continue with no active topic at all -> topic_id is None, not fabricated", action == "continue" and target is None)


# --- resolve_reference: ordinal against last_rendered -----------------------

lr = LastRendered(product_ids=["P1", "P2", "P3"], source_capability="discovery")
ctx_lr = _context(last_rendered=lr)
products = [_product("P1", "Trailblazer Running Shoe", 4299), _product("P2", "Starter Road Running Shoe", 1999), _product("P3", "Cloudstep Max Cushion Running Shoe", 9499)]

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 3]),
    ctx_lr, products,
)
check("ordinal [1,3] against last_rendered resolves to P1 and P3 (0-based 0,2)", ref.product_ids == ["P1", "P3"])

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[99]),
    ctx_lr, products,
)
check("out-of-range ordinal position resolves to nothing, never guessed", ref.product_ids == [])

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, -1]),
    ctx_lr, products,
)
check(
    "'-1' resolves to the REAL last item, never a guessed/counted position (the 'first and last' bug)",
    ref.product_ids == ["P1", "P3"],
)


# --- resolve_reference: superlative (the "high price"/"costliest" case) ----

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="superlative", reference_attribute="price", reference_direction="max"),
    ctx_lr, products,
)
check("superlative max price resolves to the REAL most expensive product", ref.product_ids == ["P3"])

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="superlative", reference_attribute="price", reference_direction="min"),
    ctx_lr, products,
)
check("superlative min price resolves to the REAL cheapest product", ref.product_ids == ["P2"])


# --- resolve_reference: cross-topic paused scope (the socks/shoes bug) ------

paused_shoes_all3 = Topic(topic_id="t1b", kind="discovery", label="running shoes", product_ids=["P1", "P2", "P3"], state="paused")
ctx_cross = _context(
    active=Topic(topic_id="t2", kind="discovery", label="socks", product_ids=["S1"], state="active"),
    paused=[paused_shoes_all3],
)
ref = resolve_reference(
    _claim(reference_scope="paused_topic", reference_kind="superlative", reference_attribute="price",
           reference_direction="max", context_target_label="running shoes"),
    ctx_cross, products,
)
check(
    "superlative against paused_topic scope resolves within THAT topic's own ids, never the active (socks) topic",
    ref.product_ids == ["P3"],
)


# --- resolve_reference: named ------------------------------------------------

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="named", reference_names=["cloudstep"]),
    ctx_lr, products,
)
check("named reference matches the real product by a distinctive word", ref.product_ids == ["P3"])

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="named", reference_names=["running shoe"]),
    ctx_lr, products,
)
check("a name matching MULTIPLE real candidates is flagged ambiguous, never guessed", ref.ambiguous)


# --- resolve_reference: all ---------------------------------------------------

ref = resolve_reference(
    _claim(reference_scope="last_rendered", reference_kind="all"),
    ctx_lr, products,
)
check("'all' resolves to every real candidate in scope", set(ref.product_ids) == {"P1", "P2", "P3"})


# --- verify(): end-to-end, needs_clarification ------------------------------

result = verify(
    _claim(reference_scope="last_rendered", reference_kind="named", reference_names=["nonexistent product"]),
    ctx_lr, products,
)
check("a reference that resolves to nothing real -> needs_clarification, never a silent wrong answer", result.needs_clarification)

result = verify(
    _claim(reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1]),
    ctx_lr, products,
)
check("a real, unambiguous resolution -> needs_clarification is False", not result.needs_clarification)

result = verify(
    _claim(
        capability="cart_action", context_action="continue", cart_operation="add",
        durable_fact_candidates=[DurableFactCandidate(subject="taste", statement="prefers decaf")],
        rationale="",
    ),
    empty_ctx, [],
)
check("durable_fact_candidates pass through unresolved (storage normalization is a separate, later step)", len(result.durable_fact_candidates) == 1)
check("cart_operation passes through", result.cart_operation == "add")


def main() -> bool:
    ok = True
    for label, passed in _checks:
        print(f"  {'ok' if passed else 'FAILED'}\t{label}")
        ok = ok and passed
    print("\n  all verification checks passed" if ok else "\n  FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
