from pathlib import Path

for path in ("engine/api/chat.py", "engine/api/schemas.py"):
    p = Path(path)
    if not p.exists():
        continue
    lines = p.read_text(encoding="utf-8").split("\n")
    for i, l in enumerate(lines):
        if l.startswith("class ChatRequest"):
            print(f"\n=== {path}, line {i + 1} ===")
            j = i
            while j < len(lines) and (j == i or lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                print(f"{j + 1:4}  {lines[j].rstrip()}")
                j += 1
                if j - i > 40:
                    break
            break

print("\n=== what pydantic sees ===")
try:
    from engine.api.chat import ChatRequest

    print(" ", list(ChatRequest.model_fields.keys()))
except Exception as e:
    print(f"  could not import: {e}")