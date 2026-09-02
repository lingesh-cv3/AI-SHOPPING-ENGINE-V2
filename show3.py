from pathlib import Path

print("\n=== App.tsx: switchMerchant ===")
lines = Path("storefront/src/App.tsx").read_text(encoding="utf-8").split("\n")
for i, l in enumerate(lines):
    if "switchMerchant = " in l:
        for j in range(i, min(i + 26, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break

print("\n=== ChatWidget.tsx: the restore ===")
lines = Path("storefront/src/ChatWidget.tsx").read_text(encoding="utf-8").split("\n")
for i, l in enumerate(lines):
    if "api\n" in l or ".transcript(sessionId)" in l:
        for j in range(max(0, i - 3), min(i + 20, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break

print("\n=== prompts.py: end of SYSTEM_PROMPT ===")
text = Path("engine/reasoning/prompts.py").read_text(encoding="utf-8")
lines = text.split("\n")
for i, l in enumerate(lines):
    if "Say less rather than more" in l or "say less" in l.lower():
        for j in range(max(0, i - 3), min(i + 6, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break
else:
    print("  'Say less rather than more' not found - here are the last 12 lines of")
    print("  the prompt block:")
    idx = text.find("SYSTEM_PROMPT")
    if idx >= 0:
        block = text[idx : idx + 3000].split("\n")
        for n, l in enumerate(block[-14:]):
            print(f"      {l.rstrip()}")