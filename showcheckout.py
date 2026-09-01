from pathlib import Path

for path in ("shared/interfaces/commerce.py", "adapters/kettle/adapter.py"):
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    print(f"\n--- {path} ---")
    for i, l in enumerate(lines):
        if "def checkout" in l:
            for j in range(i, min(i + 8, len(lines))):
                print(f"{j + 1:4}  {lines[j].rstrip()}")
            break