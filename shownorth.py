from sample_merchant.seed import catalog

# The module didn't yield titles by the generic route, so inspect it directly.
for name in dir(catalog):
    if name.startswith("_"):
        continue
    value = getattr(catalog, name)
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
        print(f"\n{name}: {len(value)} entries")
        print("keys:", list(value[0].keys()))
        for item in value[:40]:
            bits = [str(v)[:40] for k, v in item.items() if isinstance(v, str)]
            print("  " + " | ".join(bits[:3]))
        break