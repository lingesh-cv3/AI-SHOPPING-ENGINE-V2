"""The Focus Stack's own transition procedure - CONTINUE / PUSH / RESUME,
and the deterministic gate for whether mission-item interpretation is
allowed to run this turn.

Pure, synchronous, no I/O, no LLM call of its own - every input here is
either already-persisted Focus Stack state or something Stage 1's existing
`understand()` call already extracted for free (`delta.goal`, whether the
turn has any new shopping-shaped content, `message_intent`). This is
deliberate: topic SELECTION (which topic is relevant) is answered entirely
by structural comparison and the SAME closed-class historical-reference
check `capability.py` already uses - never a fresh LLM call asking "is
this relevant." A capability-specific interpretation
(`interpret_cart_reference`/`interpret_mission_items`) still runs
afterward, but only once this function has already decided a topic is
worth asking about - see `decide_transition`'s own docstring for the exact
guarantee this closes.

Topic-level matching is LABEL-based (a topic's own `label` string, e.g.
"running shoes", "coffee"), not product-title-based - deciding WHICH topic
is relevant needs no adapter fetch; deciding which SPECIFIC product within
that topic a reference means is each capability's own job, using real
fetched candidates, exactly as it already does today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .capability import looks_like_historical_reference
from .reference_resolution import _distinctive_title_words
from .state import MAX_FOCUS_STACK_DEPTH, Topic

TransitionAction = Literal["continue", "push", "resume", "ambiguous_mission_check"]


@dataclass
class TransitionResult:
    """`action`:
    - "continue": stay on the active topic (or no topic exists yet).
      `topic_id` names it, `None` if the stack is empty.
    - "push": start a brand-new topic. The caller creates it (this module
      never invents a topic_id or infers a `kind` - that's the caller's own
      concern, informed by whatever else this turn's delta contains).
    - "resume": promote the named paused topic back to active.
    - "ambiguous_mission_check": a mission topic exists in the stack but is
      not active, and this turn's new content does not obviously belong to
      whatever IS active - genuinely ambiguous between "unrelated new
      topic" and "mission continuation." The caller MUST invoke
      `interpret_mission_items` to resolve it (see module docstring) and
      then finalize as either a "resume" of the named mission topic
      (handled=True) or fall through to ordinary PUSH logic (handled=False)
      - this function deliberately cannot decide alone, since resolving
      genuine language ambiguity is exactly the LLM's job, not this
      function's.

    `mission_call_allowed`: true only when the active (for "continue"/
    "resume") or candidate (for "ambiguous_mission_check") topic is
    `kind="mission"` - the single gate `interpret_mission_items` is called
    behind. Always false for "push" (a brand-new topic has no mission item
    history to interpret yet)."""

    action: TransitionAction
    topic_id: str | None
    mission_call_allowed: bool


def _label_matches(text: str, label: str) -> bool:
    """True when `text` contains a real, distinctive word from `label` -
    the same exclusivity-free word-overlap check `resolve_named_subset`
    uses, applied here to a single label rather than choosing among many.
    Deliberately permissive at this single-label granularity: distinguishing
    MULTIPLE plausible paused-topic matches is `decide_transition`'s own
    job (most-recently-paused wins, see its docstring), not this helper's."""
    words = _distinctive_title_words(label)
    if not words:
        return False
    lowered = text.lower()
    return any(w in lowered for w in words)


def decide_transition(
    *,
    focus_stack: list[Topic],
    message: str,
    delta_goal: str | None,
    has_new_shopping_content: bool,
) -> TransitionResult:
    """The five-rule procedure. See `topic_transition.py`'s module
    docstring and this project's own architecture review for the full
    case analysis; summarized here:

    1/2. The message names a paused topic (by label match, or by the
         existing closed-class `looks_like_historical_reference` check
         when exactly one paused topic exists or one is unambiguously most
         recent) -> RESUME it.
    3.   A mission topic exists elsewhere in the stack, this turn has new
         shopping-shaped content, and that content does not match the
         active topic -> AMBIGUOUS_MISSION_CHECK (caller resolves via
         `interpret_mission_items`).
    4.   New content that matches nothing above -> PUSH.
    5.   Nothing new -> CONTINUE.

    PROOF this cannot under-trigger mission interpretation: it is skipped
    entirely only when (a) no mission topic exists anywhere in the stack -
    correctly nothing to interpret, (b) this turn has no new shopping
    content at all - correctly nothing for any interpreter to act on, or
    (c) rules 1/2 already resolved the turn to something other than the
    mission, by an EXPLICIT, real label/historical-reference match, not a
    default. Every other case reaches rule 3 (which invokes it) or rule 5
    (which, when the active topic IS the mission, always invokes it
    unconditionally - no ambiguity to gate there at all)."""
    active = next((t for t in focus_stack if t.state == "active"), None)
    paused = [t for t in focus_stack if t.state == "paused"]

    if paused:
        name_matches = [t for t in paused if _label_matches(message, t.label)]
        if len(name_matches) == 1:
            t = name_matches[0]
            return TransitionResult("resume", t.topic_id, mission_call_allowed=t.kind == "mission")
        if len(name_matches) > 1:
            # Genuinely ambiguous by label alone - fall through rather than
            # guess; a capability's own product-level resolution downstream
            # may still disambiguate using real fetched titles.
            pass
        elif looks_like_historical_reference(message):
            most_recent = max(paused, key=lambda t: t.last_active_at)
            return TransitionResult(
                "resume", most_recent.topic_id, mission_call_allowed=most_recent.kind == "mission",
            )

    # The active topic already being a mission is never itself ambiguous,
    # and never a reason to push away - a mission absorbs new item
    # statements as part of what it structurally IS. This must be checked
    # BEFORE rule 4's label-mismatch push check below: "water bottle" not
    # matching a mission topic's own label ("shoes and socks") is not
    # evidence of an unrelated new topic the way it would be for an
    # ordinary discovery topic - it's exactly the shape of turn
    # `interpret_mission_items` exists to adjudicate (a genuinely new
    # tracked item vs. something unrelated), and rule 5's unconditional
    # mission-call-when-active guarantee is what lets it do so.
    if active is not None and active.kind == "mission":
        return TransitionResult("continue", active.topic_id, mission_call_allowed=True)

    mission_topic = next((t for t in focus_stack if t.kind == "mission"), None)
    if (
        mission_topic is not None
        and mission_topic.state != "active"
        and has_new_shopping_content
        and not (active is not None and _label_matches(delta_goal or message, active.label))
    ):
        return TransitionResult(
            "ambiguous_mission_check", mission_topic.topic_id, mission_call_allowed=True,
        )

    if delta_goal and not (active is not None and _label_matches(delta_goal, active.label)):
        return TransitionResult("push", None, mission_call_allowed=False)

    return TransitionResult(
        "continue",
        active.topic_id if active else None,
        mission_call_allowed=active is not None and active.kind == "mission",
    )


def _enforce_depth_cap(stack: list[Topic]) -> list[Topic]:
    """Keeps the one active topic plus the `MAX_FOCUS_STACK_DEPTH` most-
    recently-paused topics - the oldest paused topic is evicted silently.
    Never an error case: a later reference to an evicted topic degrades
    honestly (see `Topic`'s own docstring) rather than crashing or
    mismatching."""
    active = [t for t in stack if t.state == "active"]
    paused = sorted((t for t in stack if t.state == "paused"), key=lambda t: t.last_active_at, reverse=True)
    return active + paused[:MAX_FOCUS_STACK_DEPTH]


def apply_transition(
    *,
    focus_stack: list[Topic],
    decision: TransitionResult,
    new_topic_id: str | None = None,
    new_topic_kind: str | None = None,
    new_topic_label: str | None = None,
    new_topic_product_ids: list[str] | None = None,
    new_topic_mission_item_ids: list[str] | None = None,
) -> list[Topic]:
    """Applies a `TransitionResult` to produce the new stack. Never called
    with `decision.action == "ambiguous_mission_check"` - the caller must
    resolve that to a concrete "resume" or "push" first (see
    `TransitionResult`'s own docstring on why this module cannot decide
    that alone). "continue" is a no-op by construction: refinements to the
    active topic's own fields are applied by the caller directly against
    that `Topic` object, not through this function."""
    now = datetime.now(UTC)

    if decision.action == "continue":
        return focus_stack

    if decision.action == "resume":
        new_stack = []
        for t in focus_stack:
            if t.topic_id == decision.topic_id:
                new_stack.append(t.model_copy(update={"state": "active", "last_active_at": now}))
            elif t.state == "active":
                new_stack.append(t.model_copy(update={"state": "paused", "last_active_at": now}))
            else:
                new_stack.append(t)
        return _enforce_depth_cap(new_stack)

    if decision.action == "push":
        # Bug fix (2026-09, live-reproduced): an empty label ("") is a
        # real, valid "no real label yet" state - the SAME state the
        # legacy-bridge synthetic topics and this migration's own
        # grounding fix (`graph.py::node_topic_transition`'s push site)
        # already use deliberately, backfilled with a real, product-
        # grounded label once `node_finalize` knows real products. Only
        # a genuinely MISSING label (`None` - a caller that forgot to
        # pass one at all) is an error; a caller that explicitly chose
        # "" is not.
        if not new_topic_id or not new_topic_kind or new_topic_label is None:
            raise ValueError("push requires new_topic_id/kind/label")
        new_stack = [
            t.model_copy(update={"state": "paused", "last_active_at": now}) if t.state == "active" else t
            for t in focus_stack
        ]
        new_stack.append(
            Topic(
                topic_id=new_topic_id, kind=new_topic_kind, label=new_topic_label,
                product_ids=new_topic_product_ids or [],
                mission_item_ids=new_topic_mission_item_ids or [],
                state="active", created_at=now, last_active_at=now,
            )
        )
        return _enforce_depth_cap(new_stack)

    raise ValueError(f"apply_transition cannot be called with action={decision.action!r}")
