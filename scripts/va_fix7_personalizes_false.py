"""Deterministic verification for the SEVENTH-gap fix in
engine/api/chat.py::_honest_synthesis - the personalizes=False bypass.

Calls _honest_synthesis directly (no model call, no throttle) so results
are reproducible and not subject to live-model variance.
"""
import sys

sys.path.insert(0, ".")

from engine.api.chat import _honest_synthesis  # noqa: E402

STATE = {"priorities": ["lightweight"]}
FALLBACK = "Here's how they compare."

cases = [
    # --- The two exact bypasses from the audit ---
    (
        "Bypass A: valid cited_reason + personalizes=False",
        "The lightweight build is a nice bonus, but what truly seals the "
        "deal here is the Marathon Pro's outstanding durability.",
        "lightweight",
        False,
        "CLAUSE",  # valid cited_reason -> deterministic clause used, raw false-attribution text discarded either way
    ),
    (
        "Bypass B: cited_reason=None + personalizes=False, implicit claim",
        "Honestly, between the two, the real winner here is durability "
        "above everything else -- the lightweight angle barely factors in.",
        None,
        False,
        "REJECT",
    ),
    # --- Regression: previously-passing round-6/7 adversarial cases ---
    (
        "Round6#1 rhetorical question, cited=lightweight, personalizes=True",
        "Isn't it the durability you're really after? The lightweight "
        "frame is a nice bonus, but durability is why the Marathon Pro wins.",
        "lightweight",
        True,
        "CLAUSE",  # cited_reason no longer trusts prose at all -> deterministic clause used, never this raw text
    ),
    (
        "Grounded honest case, personalizes=True",
        "Since lightweight matters most to you, the Marathon Pro is the "
        "pick - it's the lightest option in stock.",
        "lightweight",
        True,
        "CLAUSE",
    ),
    (
        "Grounded honest case, personalizes=False (model inconsistency)",
        "Since lightweight matters most to you, the Marathon Pro is the "
        "pick - it's the lightest option in stock.",
        "lightweight",
        False,
        "CLAUSE",  # cited_reason present and valid -> now ALWAYS uses deterministic clause regardless of personalizes
    ),
    (
        "Dishonest citation (not a real stored term)",
        "Since durability matters most to you, the Marathon Pro wins.",
        "durability",
        True,
        "REJECT",
    ),
    (
        "No cited_reason, personalizes=None (fail-closed, has stated priority)",
        "The Marathon Pro is a great all-around pick for road racing.",
        None,
        None,
        "REJECT",
    ),
    (
        "No stated priority at all -> plain fallback regardless of text",
        "Here's a neutral fact-only description of the product.",
        None,
        False,
        "REJECT_NO_STATE",
    ),
]

fails = 0
for name, text, cited, personalizes, expect in cases:
    state = {} if expect == "REJECT_NO_STATE" else STATE
    result = _honest_synthesis(text, state, FALLBACK, cited_reason=cited, personalizes=personalizes)
    if expect in ("REJECT", "REJECT_NO_STATE"):
        ok = result == FALLBACK
    elif expect == "CLAUSE":
        ok = result != text and result != FALLBACK and FALLBACK[0].lower() + FALLBACK[1:] in result
    else:
        ok = False
    status = "PASS" if ok else "FAIL"
    if not ok:
        fails += 1
    print(f"[{status}] {name}\n    -> {result!r}\n")

print(f"\n{len(cases) - fails}/{len(cases)} passed")
sys.exit(1 if fails else 0)
