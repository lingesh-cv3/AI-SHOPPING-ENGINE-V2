"""Round 7 verification: deterministic causal-attribution clause for _honest_synthesis.
Direct, deterministic calls - no model variance."""
import sys
sys.path.insert(0, ".")
from engine.api.chat import _honest_synthesis

state = {"priorities": ["lightweight"]}
fallback = "Here's how they compare."

# The 5 that bypassed in round 6, all with cited_reason="lightweight" (real, valid)
bypasses = [
    ("The lightweight design is nice, but what really seals the deal here is the Marathon Pro's outstanding durability", "lightweight"),
    ("Isn't it the durability you're really after? The lightweight frame is a nice bonus, but durability is why the Marathon Pro wins.", "lightweight"),
    ("A shopper who values lightweight gear like you would still find that what makes the Marathon Pro right for them is its unmatched durability.", "lightweight"),
    ("Compared to being lightweight, durability matters more here - and that's exactly the quality that makes the Marathon Pro the right call.", "lightweight"),
    ("You'll love this for its rugged, long-lasting build quality above all else (and it happens to be lightweight too).", "lightweight"),
]

# 10 more fresh adversarial phrasings, same shape (valid cited_reason, prose argues something else)
fresh = [
    ("Honestly? Grip is what makes this shoe special, though it is admittedly lightweight as well.", "lightweight"),
    ("The real story here isn't the weight - it's the cushioning, which is second to none.", "lightweight"),
    ("People who care about lightweight shoes tend to actually end up choosing this one for the arch support.", "lightweight"),
    ("Sure it's light, but let's be real - the price is the reason to buy this one.", "lightweight"),
    ("If I'm being honest, breathability is the standout quality, with lightweight construction as a side note.", "lightweight"),
    ("Between being lightweight and being waterproof, it's the waterproofing that seals this recommendation.", "lightweight"),
    ("This pick wins on style alone - lightweight is just a nice extra.", "lightweight"),
    ("What ultimately matters here is traction on wet trails, not how light it is.", "lightweight"),
    ("Some shoppers focus on weight, but the color options are honestly the highlight of this one.", "lightweight"),
    ("Durability first, weight second - that's the actual reasoning, even though it is indeed lightweight.", "lightweight"),
]

print("=== Bypass phrasings (round 6, must now be handled - no false claim reaches shopper) ===")
for text, reason in bypasses:
    out = _honest_synthesis(text, state, fallback, cited_reason=reason, personalizes=True)
    ok = out != text  # must NOT pass the model's raw prose through unchanged
    print(f"{'PASS' if ok else 'FAIL'}: {out!r}")

print("\n=== 10 fresh adversarial phrasings ===")
for text, reason in fresh:
    out = _honest_synthesis(text, state, fallback, cited_reason=reason, personalizes=True)
    ok = out != text
    print(f"{'PASS' if ok else 'FAIL'}: {out!r}")

print("\n=== False-positive check: genuine grounded citation must still read naturally ===")
good_text = "Since lightweight matters most to you, the Marathon Pro is the pick - it's the lightest option in stock."
out = _honest_synthesis(good_text, state, fallback, cited_reason="lightweight", personalizes=True)
print(f"Output: {out!r}")

print("\n=== No stated priority at all: must still fall back ===")
out = _honest_synthesis("Some model text.", {}, fallback, cited_reason=None, personalizes=False)
print(f"Output (expect fallback): {out!r}  {'PASS' if out == fallback else 'FAIL'}")

print("\n=== personalizes=False, no citation, no implicit claim: text should pass ===")
neutral = "This one's in stock and ships in two days."
out = _honest_synthesis(neutral, state, fallback, cited_reason=None, personalizes=False)
print(f"Output: {out!r}  {'PASS' if out == neutral else 'FAIL (unexpected fallback)'}")
