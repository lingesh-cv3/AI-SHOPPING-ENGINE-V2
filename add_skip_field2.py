"""Insert skip_model after line 68, and remove any stray copy.

The previous attempt walked the class looking for the last field and put it in the
wrong place, so pydantic never saw it. The class ends at line 68 with `known`, so it
goes directly after that - by line number, and verified against pydantic rather than
against the file text.
"""

from pathlib import Path

f = Path("engine/api/chat.py")
lines = f.read_text(encoding="utf-8").split("\n")

# Clear any stray copy the last attempt left behind.
before = len(lines)
lines = [
    l
    for l in lines
    if l.strip() != "skip_model: bool = False"
    and "Handle this without calling the model" not in l
    and "be kept waiting - a declined payment, where the recovery options come" not in l
    and "from the capability table and the model would only have phrased them." not in l
    and "#: Set when the answer does not depend on judgement and the shopper cannot" not in l
]
if len(lines) != before:
    print(f"removed {before - len(lines)} stray line(s)")

# Find the `known` field, which is the last one in the class.
target = None
for i, l in enumerate(lines):
    if l.strip().startswith("known: dict[str, str] = Field"):
        target = i
        break

if target is None:
    print("FAILED: could not find the known field")
    raise SystemExit(1)

lines[target + 1 : target + 1] = [
    "",
    "    #: Handle this without calling the model.",
    "    #:",
    "    #: Set when the answer does not need judgement and the shopper cannot be",
    "    #: kept waiting. A declined payment is the case: the recovery options come",
    "    #: from the capability table, so the model would only have phrased them -",
    "    #: and a fixed sentence now beats a nicer one in thirty seconds, or never,",
    "    #: while the provider is busy.",
    "    skip_model: bool = False",
]

f.write_text("\n".join(lines), encoding="utf-8", newline="\n")

# Verified against pydantic, not against the text.
import importlib
import sys

for name in list(sys.modules):
    if name.startswith("engine."):
        del sys.modules[name]

from engine.api.chat import ChatRequest

fields = list(ChatRequest.model_fields.keys())
print(f"\nChatRequest fields: {fields}")

if "skip_model" in fields:
    print("\napplied")
else:
    print("\nFAILED: pydantic still does not see it")
    raise SystemExit(1)