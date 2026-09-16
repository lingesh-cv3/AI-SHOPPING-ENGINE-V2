"""Direct, in-process adversarial test of _honest_synthesis's fifth-gap fix
(the mandatory `personalizes` flag). Not a live model call - tests the
deterministic checking logic exactly as it ships, the same way the
value-auditor pass that found gap 5 did. Gitignored scratch, per project
convention for va_*.py test scripts."""
import sys
sys.path.insert(0, ".")

from engine.api.chat import _honest_synthesis

STATE = {"priorities": ["lightweight"]}
FALLBACK = "Here's how they compare."

# --- Previously-defeating phrasings (5 from the original adversarial pass) ---
PREVIOUS_5 = [
    "Since you said durability matters most to you (though it's also nice and light), here's our pick.",
    "You mentioned lightness matters a lot, so honestly durability is what should decide this - the Marathon Pro wins.",
    "Because comfort is what you need most, though it is admittedly light, I picked the Marathon Pro for you.",
    "Your priority of grip clearly matters here, and it happens to be lightweight too, so the Marathon Pro is the pick.",
    "You prioritized warmth above all, and it's also fairly light, making the Marathon Pro the better call.",
]

# --- 5 more fresh adversarial phrasings, different rhetorical shapes ---
FRESH_5 = [
    "Reading between the lines of what you've said about wanting something lightweight, durability actually wins out for you, making the Marathon Pro the better call.",
    "Given how much you care about durability above everything else, and since lightweight is nice too, the Marathon Pro wins.",
    "The Marathon Pro suits your need for a durable shoe, and it is also fairly lightweight.",
    "Isn't durability really what you're after here? That, plus it being nice and light, makes the Marathon Pro the clear choice.",
    "For someone clearly focused on toughness first (lightweight being a nice bonus), the Marathon Pro is the obvious pick.",
]

# --- Genuinely grounded reply: correctly cites the real stored term ---
GROUNDED_TEXT = "Since you said lightweight matters most to you, the Marathon Pro is the better fit."


def run(label, text, personalizes, cited_reason, state=STATE, expect_reject=True):
    result = _honest_synthesis(text, state, FALLBACK, cited_reason, personalizes)
    rejected = result == FALLBACK
    ok = rejected == expect_reject
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: rejected={rejected} (expected reject={expect_reject})")
    return ok


results = []

print("== Previous 5 defeating phrasings, personalizes unset (None, fail-closed) ==")
for i, text in enumerate(PREVIOUS_5, 1):
    results.append(run(f"previous-{i}", text, None, None, expect_reject=True))

print("\n== Previous 5 defeating phrasings, personalizes=True, no cited_reason ==")
for i, text in enumerate(PREVIOUS_5, 1):
    results.append(run(f"previous-{i}-explicit-true", text, True, None, expect_reject=True))

print("\n== 5 fresh adversarial phrasings, personalizes unset (None) ==")
for i, text in enumerate(FRESH_5, 1):
    results.append(run(f"fresh-{i}", text, None, None, expect_reject=True))

print("\n== 5 fresh adversarial phrasings, personalizes=True, no cited_reason ==")
for i, text in enumerate(FRESH_5, 1):
    results.append(run(f"fresh-{i}-explicit-true", text, True, None, expect_reject=True))

print("\n== Genuinely grounded reply, honestly cited - must NOT be rejected ==")
results.append(run("grounded-honest", GROUNDED_TEXT, True, "lightweight", expect_reject=False))

print("\n== No stated priority at all - must fall back regardless (existing behavior) ==")
results.append(run("no-priority-state", "Here's a neutral pick.", False, None, state=None, expect_reject=True))

print("\n== Neutral, non-personalizing reply that mentions the real term factually - must pass ==")
# Note: a reply mentioning NO stored term at all is rejected by the
# pre-existing `any_term_mentioned` floor regardless of `personalizes` -
# that floor predates this fix and is out of its scope.
results.append(run(
    "neutral-with-state",
    "The Marathon Pro weighs less (lightweight, 210g) and is $20 cheaper than the Trailblazer.",
    False, None, expect_reject=False,
))

total = len(results)
passed = sum(results)
print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
