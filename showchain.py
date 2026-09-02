from pathlib import Path

lines = Path("engine/api/chat.py").read_text(encoding="utf-8").split("\n")

start = None
for i, l in enumerate(lines):
    if "executed.needs_choice" in l:
        start = i - 4
        break

if start is None:
    print("could not find the executed handling")
else:
    for i in range(start, min(start + 60, len(lines))):
        print(f"{i + 1:4}  {lines[i].rstrip()}")
