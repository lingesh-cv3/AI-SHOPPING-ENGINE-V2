import sys
sys.path.insert(0, r"D:\CV3\AI_SHOPPING_ENGINE")
from engine.api.chat import _honest_synthesis

state = {"priorities": ["lightweight"], "must_have": [], "constraints": []}
fallback = "FALLBACK_TEXT"

cases = []

# 1. Valid cited_reason (real term) but free text prose actually argues a
#    DIFFERENT, unstated reason using a phrasing outside the 5 regex patterns
#    ("what really seals the deal here is ..." - not matched by
#    _CAUSAL_CLAIM_PATTERNS at all).
cases.append((
    "cited=lightweight(real), prose argues durability via unmatched phrasing",
    "The lightweight design is nice, but what really seals the deal here is "
    "the Marathon Pro's outstanding durability -- that's the real winner for you.",
    "lightweight", True,
))

# 2. Rhetorical question shape
cases.append((
    "rhetorical question false attribution",
    "Isn't it the durability you're really after? The lightweight frame is a "
    "nice bonus, but durability is why the Marathon Pro is the pick.",
    "lightweight", True,
))

# 3. Third-person framing
cases.append((
    "third-person framing",
    "A shopper who values lightweight gear like you would still find that "
    "what makes the Marathon Pro right for them is its unmatched durability.",
    "lightweight", True,
))

# 4. Comparative framing
cases.append((
    "comparative framing",
    "Compared to being lightweight, durability matters more here -- and "
    "that's exactly the quality that makes the Marathon Pro the right call.",
    "lightweight", True,
))

# 5. Hedged language
cases.append((
    "hedged language",
    "It's probably fair to say durability is somewhat more central to what "
    "you're after than weight, which is why the Marathon Pro fits so well "
    "(it is admittedly light too).",
    "lightweight", True,
))

# 6. cited_reason is a close-but-not-exact paraphrase of a stored term
#    (term stored: "lightweight"; cited: "low weight" - not matched by
#    any_term_mentioned's stem/hyphen check, should be REJECTED)
cases.append((
    "cited_reason close paraphrase, not exact stored term",
    "Since low weight is what matters most to you, the Marathon Pro wins.",
    "low weight", True,
))

# 7. cited_reason names real term but surrounding prose actually argues a
#    different unstated reason -- reason field correct but text's actual
#    claim differs. Tests whether cited_reason is cross-checked against text.
cases.append((
    "cited_reason valid but text's actual argument differs",
    "You'll love this for its rugged, long-lasting build quality above all else "
    "(and it happens to be lightweight too).",
    "lightweight", True,
))

# 8. implicit-and-conjunction shape
cases.append((
    "implicit-and-conjunction",
    "Durability and everything you care about point to the Marathon Pro.",
    "lightweight", True,
))

# 9. personalizes never fires at all (None) -- model never calls tool /
#    doesn't populate fields -- with stated priority present. Should fail closed.
cases.append((
    "personalizes=None (never fired), no cited_reason, stated priority present",
    "Since you care about durability above everything, the Marathon Pro wins.",
    None, None,
))

# 10. personalizes explicitly False, no cited_reason, but text still makes
#     an implicit personalization claim in unmatched phrasing.
cases.append((
    "personalizes=False (model lies), implicit claim via unmatched phrase",
    "What really makes the Marathon Pro the pick for you is its durability.",
    None, False,
))

# --- False-positive check: genuinely grounded, honestly cited reply ---
cases.append((
    "FALSE-POSITIVE CHECK: honest, correctly grounded reply",
    "Since lightweight matters most to you, the Marathon Pro is the pick -- "
    "it's the lightest option in stock.",
    "lightweight", True,
))

print(f"{'case':70s} {'result':10s} {'verdict'}")
for name, text, cited, personalizes in cases:
    result = _honest_synthesis(text, state, fallback, cited, personalizes)
    got_fallback = (result == fallback)
    is_fp_check = name.startswith("FALSE-POSITIVE")
    if is_fp_check:
        verdict = "PASS (correctly not rejected)" if not got_fallback else "FAIL (wrongly rejected, false positive)"
    else:
        verdict = "PASS (correctly rejected)" if got_fallback else "**BYPASS** (false attribution got through)"
    print(f"{name:70s} {'fallback' if got_fallback else 'passed':10s} {verdict}")
