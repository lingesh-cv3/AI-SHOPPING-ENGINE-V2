import sqlite3

# The stores keep products in memory, so read them from the catalogue source.
for module, label in (
    ("sample_merchant.seed.catalog", "Northfield"),
    ("sample_merchant_two.seed.catalog", "Kettle"),
):
    print(f"\n{label}")
    try:
        mod = __import__(module, fromlist=["*"])
    except Exception as e:
        print(f"  could not import: {e}")
        continue

    # Find whatever list of products the module exposes.
    for name in dir(mod):
        value = getattr(mod, name)
        if isinstance(value, (list, tuple)) and value:
            first = value[0]
            if isinstance(first, dict) and any(
                k in first for k in ("id", "sku", "product_id", "productId")
            ):
                for item in value:
                    pid = (
                        item.get("id")
                        or item.get("sku")
                        or item.get("product_id")
                        or item.get("productId")
                    )
                    title = item.get("name") or item.get("title") or ""
                    print(f"  {str(pid):28} {title}")
                break