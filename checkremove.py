from pathlib import Path

checks = [
    ("engine/api/shop.py", "async def change_line", "the remove route"),
    ("engine/api/shop.py", "class ChangeLine", "the request model"),
    ("storefront/src/api.ts", "changeLine", "the api call"),
    ("storefront/src/CartPanel.tsx", "onRemoveLine", "the panel control"),
    ("storefront/src/App.tsx", "onRemoveLine", "the App wiring"),
    ("storefront/src/styles.css", ".line-remove", "the styles"),
    ("fuzz.py", "lines/{line", "the fuzzer removing"),
]

print()
for path, marker, name in checks:
    p = Path(path)
    if not p.exists():
        print(f"  ?       {name} - {path} not found")
        continue
    present = marker in p.read_text(encoding="utf-8")
    print(f"  {'ok    ' if present else 'MISSING'}  {name}")
print()