"""The incremental `reply` decoder for streamed tool-call JSON (model-free, pure; no database, no network).

The model's reply arrives as fragments of a JSON document. The decoder must hand the UI exactly the decoded `reply` value - no JSON syntax, no other
field, no half-decoded escape - however the fragments are cut. Asserted by cutting every payload at EVERY position and at random multi-cuts, and
comparing with `json.loads` on the whole document.

    python tests/shopper_v2/test_stream_parse.py
"""
from __future__ import annotations

import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.reasoning.stream_parse import ReplyExtractor  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got!r}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def run(fragments, key="reply"):
    ex = ReplyExtractor(key)
    pieces = [ex.feed(f) for f in fragments]
    return pieces, ex


def joined(fragments, key="reply"):
    pieces, ex = run(fragments, key)
    return "".join(pieces), ex


PAYLOADS = {
    "plain": {"reply": "Here are two pairs of running socks.", "cited_reason": "you asked for socks", "personalizes": False},
    "reply last": {"cited_reason": "from your cart", "personalizes": True, "reply": "The Trailblazer is lighter."},
    "reply middle": {"a": 1, "reply": "middle", "z": [1, 2, {"reply": "NOT THIS"}]},
    "escapes": {"reply": 'She said "hi"\nthen\ttabbed \\ and a slash / ok'},
    "unicode": {"reply": "Café crème – naïve £500 ₹499 日本語"},
    "emoji (surrogate pair)": {"reply": "Great pick \U0001F600 and \U0001F45F"},
    "empty reply": {"reply": "", "x": "y"},
    "long": {"reply": "word " * 200 + "end"},
    "the word reply inside another value": {"cited_reason": 'the "reply" key is not this one: {"reply": "nope"}', "reply": "the real one"},
    "nested before": {"meta": {"k": ["}", "]", '"'], "reply": "NESTED"}, "reply": "top level"},
}


print("1. Whole documents decode to exactly json.loads(...)['reply']")
for name, payload in PAYLOADS.items():
    for ensure_ascii in (True, False):
        doc = json.dumps(payload, ensure_ascii=ensure_ascii)
        out, ex = joined([doc])
        check(f"{name} (ensure_ascii={ensure_ascii})", out == payload["reply"] and ex.finished, out)

print("\n2. Cut at EVERY position (two fragments): identical result, nothing JSON-shaped, deltas never retract")
bad = []
for name, payload in PAYLOADS.items():
    for ensure_ascii in (True, False):
        doc = json.dumps(payload, ensure_ascii=ensure_ascii)
        for i in range(len(doc) + 1):
            pieces, ex = run([doc[:i], doc[i:]])
            if "".join(pieces) != payload["reply"] or not ex.finished:
                bad.append((name, ensure_ascii, i))
check("every single cut position gives the full, exact reply", not bad, bad[:3])

print("\n3. One character at a time, and random multi-cuts")
for name, payload in PAYLOADS.items():
    doc = json.dumps(payload)
    out, ex = joined(list(doc))
    check(f"char-by-char: {name}", out == payload["reply"], out)
rng = random.Random(20261024)
bad = []
for trial in range(400):
    payload = rng.choice(list(PAYLOADS.values()))
    doc = json.dumps(payload, ensure_ascii=rng.random() < 0.5)
    cuts = sorted(rng.sample(range(len(doc) + 1), k=min(len(doc) + 1, rng.randint(1, 8))))
    frags = [doc[a:b] for a, b in zip([0, *cuts], [*cuts, len(doc)])]
    out, _ = joined(frags)
    if out != payload["reply"]:
        bad.append((trial, frags[:3]))
check("400 random multi-cut trials agree with json.loads", not bad, bad[:2])

print("\n4. Incremental delivery: text is available BEFORE the document ends")
doc = json.dumps({"reply": "Here are two pairs of running socks.", "cited_reason": "x" * 400})
pieces, ex = run([doc[:25], doc[25:60], doc[60:]])
check("the first fragment already yields visible text", pieces[0] != "" and pieces[0] == "Here are two pairs"[: len(pieces[0])], pieces)
check("the reply is finished long before the trailing field is read", ex.finished)
check("nothing is returned after the closing quote of the reply", "".join(pieces) == "Here are two pairs of running socks.", pieces)
out, ex2 = joined([doc[:15], doc[15:]])
check("a cited_reason that comes AFTER the reply never leaks into the output", "xxxx" not in out and "cited" not in out, out)

print("\n5. Only the top-level `reply`, only the first occurrence, never another field")
out, _ = joined([json.dumps({"cited_reason": "secret grounding", "reply": "visible", "personalizes": True})])
check("reply is emitted, cited_reason and personalizes are not", out == "visible", out)
out, _ = joined([json.dumps({"reply": "first", "other": 1}).replace("}", ', "reply": "second"}')])
check("a duplicate key: the first occurrence wins", out == "first", out)
out, _ = joined([json.dumps({"nested": {"reply": "inner"}})])
check("a reply that only exists nested is not emitted", out == "", out)
out, _ = joined([json.dumps({"reply": None, "x": 1})])
check("a null reply emits nothing", out == "", out)
out, _ = joined([json.dumps({"reply": 42})])
check("a numeric reply emits nothing", out == "", out)
out, _ = joined([json.dumps({"other": "x"})])
check("a missing reply emits nothing", out == "", out)

print("\n6. Malformed input never raises, never emits garbage")
for name, frags in {
    "not json": ["hello world"],
    "truncated mid-reply": ['{"reply": "abc'],
    "bad escape": ['{"reply": "a\\qb"}'],
    "bad unicode": ['{"reply": "a\\u12zzb"}'],
    "garbage after key": ['{"reply" 5}'],
    "empty": [""],
    "only whitespace": ["   \n"],
    "array root": ['["reply"]'],
}.items():
    try:
        out, ex = joined(frags)
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok, out = False, repr(exc)
    check(f"{name}: no exception", ok, out)
out, ex = joined(['{"reply": "abc'])
check("a truncated reply keeps what it decoded (the caller decides about an unfinished stream)", out == "abc" and not ex.finished, out)
out, ex = joined(['{"reply": "a\\qb"}'])
check("a bad escape stops the decoder (failed) and emits no corrupt text", ex.failed, out)

print("\n7. Lone surrogates become the replacement character, never an exception")
out, _ = joined(['{"reply": "x\\ud83dy"}'])
check("a lone high surrogate", out == "x�y", out)
out, _ = joined(['{"reply": "x\\ude00y"}'])
check("a lone low surrogate", out == "x�y", out)

print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
