"""Add skip_model to ChatRequest, which reads it and does not have it.

The field was meant to go in with the pay-timeout fix. That patch anchored on a
blank-line pattern in the model and matched nothing, and I then verified by checking
for a string that the *other* half of the patch had written - so it reported success
while the chat endpoint was left calling for a field that did not exist.

Every /api/chat request has been failing since.
"""

from pathlib import Path

f = Path("engine/api/schemas.py")
if "class ChatRequest" not in f.read_text(encoding="utf-8"):
    # It may live in chat.py instead.
    f = Path("engine/api/chat.py")

s = f.read_text(encoding="utf-8")

if "skip_model" in s and "class ChatRequest" in s:
    print(f"already present in {f}")
    raise SystemExit(0)

if "class ChatRequest" not in s:
    print("FAILED: could not find ChatRequest in schemas.py or chat.py")
    raise SystemExit(1)

lines = s.split("\n")

# Find the class, then the last indented field line in it.
start = next(i for i, l in enumerate(lines) if l.startswith("class ChatRequest"))

last_field = start
i = start + 1
while i < len(lines):
    line = lines[i]
    if line and not line.startswith((" ", "\t")):
        break  # left the class
    if line.strip() and not line.strip().startswith(("#", '"""', "'")):
        if ":" in line or "=" in line:
            last_field = i
    i += 1

addition = [
    "",
    "    #: Handle this without calling the model.",
    "    #:",
    "    #: Set when the answer does not depend on judgement and the shopper cannot",
    "    #: be kept waiting - a declined payment, where the recovery options come",
    "    #: from the capability table and the model would only have phrased them.",
    "    skip_model: bool = False",
]

lines[last_field + 1 : last_field + 1] = addition
f.write_text("\n".join(lines), encoding="utf-8", newline="\n")

# Verified against the real thing, not against a string.
try:
    import importlib

    for name in ("engine.api.schemas", "engine.api.chat"):
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, "ChatRequest"):
                fields = mod.ChatRequest.model_fields.keys()
                if "skip_model" in fields:
                    print(f"applied - ChatRequest in {name} now has skip_model")
                    raise SystemExit(0)
        except ImportError:
            continue
    print("FAILED: skip_model is not on ChatRequest")
    raise SystemExit(1)
except SystemExit:
    raise
except Exception as e:
    print(f"could not verify by import: {e}")
    print("Check by hand: python -c \"from engine.api.chat import ChatRequest; print(ChatRequest.model_fields.keys())\"")