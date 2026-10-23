"""Catalog vocabulary: spelling normalization + domain-word neutralization
(`engine/shopper_v2/vocabulary.py`). Pure/model-free/network-free - stub
products only.

    python tests/shopper_v2/test_phase7_vocabulary.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.shopper_v2 import vocabulary as v  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class P:
    def __init__(self, title, categories, description=""):
        self.title, self.categories, self.description = title, categories, description


SHOES = [
    P("Trailblazer Running Shoe", ["Footwear", "Running Shoes"], "Lightweight daily trainer with a cushioned midsole."),
    P("Highridge Waterproof Trail Running Shoe", ["Footwear", "Running Shoes", "Trail Running Shoes"], "Sealed waterproof membrane and deep lugs."),
    P("Performance Socks Three Pack", ["Accessories"], "Cushioned heel, arch support."),
    P("Court Classic Tennis Shoe", ["Footwear", "Tennis Shoes"], "Flat sole, leather upper."),
]
COFFEE = [
    P("Ethiopia Guji Natural", ["Single origin"], "Blueberry, jasmine, and a syrupy body."),
    P("Morning Bell Espresso Blend", ["Blends"], "Built for milk. Chocolate, toffee."),
    P("Swiss Water Decaf", ["Blends"], "Chemical-free decaffeination. Tastes like coffee, not like decaf."),
    P("Pour Over Dripper", ["Equipment"], "Ceramic cone, spiral ribs."),
    P("Gooseneck Kettle 1L", ["Equipment"], "Precise pour, variable temperature."),
]

north = v.build_vocabulary(SHOES, None, "Northfield Running Co.")
kettle = v.build_vocabulary(COFFEE, ["Single origin", "Blends", "Equipment"], "Kettle & Bloom Coffee")


def norm(q, vocab):
    return v.normalize_query(q, vocab)


print("spelling: corrected only against REAL catalog words")
check("wateproof -> waterproof", norm("wateproof trail shoes", north).query == "waterproof trail shoes")
check("runnning -> running", norm("runnning shoes", north).query == "running shoes")
check("lightwieght -> lightweight", norm("lightwieght running shoes", north).query == "lightweight running shoes")
check("corrections are reported", norm("wateproof shoes", north).corrections == {"wateproof": "waterproof"})
check("a real word is never touched", norm("waterproof trail shoes", north).query == "waterproof trail shoes")
check("a word with no close catalog word is NOT force-corrected", norm("show me kayaks", north).query == "show me kayaks")
check("numbers/currency pass through untouched", norm("shoes under 5000", north).query == "shoes under 5000")
check("short tokens are never corrected", norm("shoe sox", north).query == "shoe sox")
check("filler words are never corrected", norm("please show me shoes", north).query == "please show me shoes")

print("domain words: derived from data, never hardcoded")
check("Kettle: 'coffee' is a domain word (merchant name, in no title/category)", "coffee" in kettle.domain_stems)
check("Northfield: 'running' is NOT a domain word (it discriminates in titles/categories)", "running" not in north.domain_stems)
check("Kettle: 'kettle' is NOT a domain word (a product title contains it)", "kettle" not in kettle.domain_stems)
n = norm("show me coffee blends", kettle)
check("'coffee blends' -> 'show me blends' (domain word neutralized)", n.query == "show me blends" and n.dropped_domain_terms == ["coffee"])
check("'coffee' alone is a browse, not a search", norm("coffee", kettle).browse_only)
check("'show me coffee' is a browse", norm("show me coffee", kettle).browse_only)
check("a typo of a domain word is corrected THEN neutralized", norm("cofffee", kettle).browse_only)
check("'coffee beans' keeps the distinctive word", norm("coffee beans", kettle).query == "beans" and not norm("coffee beans", kettle).browse_only)
check("a budget phrase is not 'something still being searched'", norm("coffee under 5000", kettle).browse_only)
# Request-framing verbs ("search", "searched", "browsing") were not filler, so
# the words left over made a pure domain-word request look like an unresolved
# query (0 products). Same class as "show"/"find", which already were.
check("a 'searched for' phrasing is a browse", norm('Searched for "coffee"', kettle).browse_only)
check("'search for coffee' is a browse", norm("search for coffee", kettle).browse_only)
check("'browsing coffee' is a browse", norm("browsing coffee", kettle).browse_only)
_d = norm('Searched for "decaf coffee"', kettle)
check("request-framing words never swallow a distinctive word (still a search, not a browse)", "decaf" in _d.query and not _d.browse_only)
check("Northfield 'running shoes' is unchanged", not norm("running shoes", north).changed)

print("category validation: the model may only SELECT real categories")
check("exact names accepted", v.validate_categories(["Blends", "Single origin"], kettle) == ["Blends", "Single origin"])
check("case-insensitive, canonical name returned", v.validate_categories(["single origin", "BLENDS"], kettle) == ["Single origin", "Blends"])
check("an invented category is dropped, never approximated", v.validate_categories(["Coffee Beans"], kettle) == [])
check("mixed valid + invented keeps only the real one", v.validate_categories(["Blends", "Invented"], kettle) == ["Blends"])
check("duplicates collapse", v.validate_categories(["blends", "Blends"], kettle) == ["Blends"])
check("empty claim -> empty", v.validate_categories([], kettle) == [])

print()
print(f"  {failures} FAILURE(S)" if failures else "  all vocabulary checks passed")
sys.exit(1 if failures else 0)
