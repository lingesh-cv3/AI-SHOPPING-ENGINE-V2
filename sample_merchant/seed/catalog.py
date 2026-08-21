"""Catalog seed data for the sample merchant.

Written as a compact table and expanded by a builder, rather than as forty
literal dictionaries. Easier to scan, and much easier to see coverage gaps.

Prices are given in rupees here and converted to paise on build, because paise
is what the platform stores and rupees is what a human reads. Integer minor
units are how a lot of real payment-adjacent systems work, and it is
deliberately unlike the engine's Decimal-plus-currency Money - the adapter has
to convert.

The catalog is built to exercise specific cases rather than to look full:

- products with no variants, few variants, and many variants
- products whose every variant is sold out while the parent still says in stock
- products either side of the free-delivery threshold, so both paths get tested
- products either side of the FLAT500 minimum spend
- LOW_STOCK, OUT_OF_STOCK and IN_STOCK all present
- discounted and undiscounted products
- titles that make keyword search fail in realistic ways: "Racing Shoe" does not
  match "running", "Sneaker" does not match "shoe", nothing matches "trainers"
"""

# (id, title, dept, price, was, stock_state, qty, sizes, blurb)
# sizes: None, or a list of (label, qty). price/was in rupees.
_ROWS = [
    # ---- Footwear ----
    ("P1001", "Trailblazer Running Shoe", "Footwear", 4299, 5499, "Y", 42,
     [("7", 12), ("8", 18), ("9", 12), ("10", 0)],
     "Lightweight daily trainer with a cushioned midsole."),
    ("P1002", "Marathon Pro Racing Shoe", "Footwear", 12499, None, "LOW", 3,
     [("8", 1), ("9", 2)],
     "Carbon plate, built for race day."),
    ("P1003", "Everyday Canvas Sneaker", "Footwear", 1899, None, "Y", 60, None,
     "Simple canvas upper, rubber sole."),
    ("P1004", "Court Classic Tennis Shoe", "Footwear", 3499, None, "N", 0, None,
     "Flat sole, leather upper."),
    ("P1005", "Fell Runner Trail Shoe", "Footwear", 6799, 7999, "Y", 21,
     [("7", 4), ("8", 9), ("9", 8)],
     "Aggressive lugs for wet ground and loose rock."),
    ("P1006", "Recovery Slide", "Footwear", 1499, None, "Y", 88,
     [("S", 30), ("M", 34), ("L", 24)],
     "Contoured foam for after a long run."),
    # Every variant sold out while the parent still reports stock. The adapter
    # must let the count win over the flag.
    ("P1007", "Studio Training Flat", "Footwear", 2799, None, "Y", 5,
     [("7", 0), ("8", 0), ("9", 0)],
     "Low profile, wide toe box, gym floor grip."),
    ("P1008", "Winter Road Shoe", "Footwear", 5299, None, "LOW", 2,
     [("9", 1), ("10", 1)],
     "Water-resistant knit with a reflective heel."),

    # ---- Apparel ----
    ("P2001", "Merino Base Layer Tee", "Apparel", 2799, None, "Y", 25,
     [("S", 8), ("M", 11), ("L", 6)],
     "Odour-resistant merino wool, regular fit."),
    ("P2002", "Packable Rain Jacket", "Apparel", 5999, 7499, "Y", 14, None,
     "Folds into its own pocket. Taped seams."),
    ("P2003", "Training Shorts 7 inch", "Apparel", 1599, None, "Y", 33,
     [("S", 10), ("M", 14), ("L", 9)],
     "Four-way stretch with a zip pocket."),
    ("P2004", "Long Run Tights", "Apparel", 3299, 3999, "Y", 18,
     [("S", 5), ("M", 7), ("L", 6)],
     "High waist, four pockets, no side seams."),
    ("P2005", "Windproof Gilet", "Apparel", 4499, None, "LOW", 4, None,
     "Cuts the chill without trapping heat."),
    ("P2006", "Race Day Singlet", "Apparel", 1899, None, "Y", 40,
     [("S", 12), ("M", 16), ("L", 12)],
     "Featherweight mesh, chafe-free binding."),
    ("P2007", "Thermal Half Zip", "Apparel", 3799, None, "N", 0,
     [("S", 0), ("M", 0), ("L", 0)],
     "Brushed inner face for cold mornings."),
    ("P2008", "Reflective Arm Sleeves", "Apparel", 899, None, "Y", 65, None,
     "Visibility for early starts and late finishes."),

    # ---- Accessories ----
    ("P3001", "Insulated Water Bottle 750ml", "Accessories", 899, None, "Y", 88, None,
     "Keeps cold for 24 hours."),
    ("P3002", "Performance Socks Three Pack", "Accessories", 499, None, "Y", 120, None,
     "Cushioned heel, arch support."),
    ("P3003", "Foam Roller", "Accessories", 1299, None, "LOW", 4, None,
     "High-density EVA, 33cm."),
    ("P3004", "Hydration Vest 5L", "Accessories", 4999, 5999, "Y", 12, None,
     "Bounce-free fit with two soft flasks."),
    ("P3005", "Running Cap", "Accessories", 699, None, "Y", 54, None,
     "Perforated crown, adjustable rear."),
    ("P3006", "Race Belt", "Accessories", 599, None, "Y", 70, None,
     "Holds a bib and a gel without bouncing."),
    ("P3007", "Compression Calf Sleeves", "Accessories", 1199, None, "Y", 30,
     [("S", 10), ("M", 12), ("L", 8)],
     "Graduated compression for long efforts."),
    ("P3008", "Head Torch 400 Lumen", "Accessories", 2499, 2999, "Y", 16, None,
     "Rechargeable, tilts, IPX4."),
         # ---- Nutrition ----
    ("P4001", "Energy Gel Box of 12", "Nutrition", 1799, None, "Y", 45, None,
     "22g carbohydrate per gel, citrus."),
    ("P4002", "Electrolyte Tablets", "Nutrition", 549, None, "Y", 90, None,
     "Twenty tablets, low sugar."),
    ("P4003", "Recovery Protein 1kg", "Nutrition", 2899, 3299, "Y", 22, None,
     "Whey isolate with added carbohydrate."),
    ("P4004", "Salt Capsules", "Nutrition", 749, None, "LOW", 3, None,
     "For heat and heavy sweat rates."),
    ("P4005", "Chew Bar Pack of 6", "Nutrition", 899, None, "Y", 60, None,
     "Solid fuel for anything over an hour."),

    # ---- Tech ----
    ("P5001", "GPS Watch Series 4", "Tech", 24999, 28999, "Y", 8, None,
     "Multi-band GPS, 30 hour battery."),
    ("P5002", "Heart Rate Chest Strap", "Tech", 3499, None, "Y", 19, None,
     "Bluetooth and ANT+, washable strap."),
    ("P5003", "Bone Conduction Headphones", "Tech", 8999, None, "LOW", 2, None,
     "Open ear, so you can still hear traffic."),
    ("P5004", "Foot Pod Cadence Sensor", "Tech", 2299, None, "N", 0, None,
     "Cadence and indoor pace without GPS."),
    ("P5005", "Smart Scale", "Tech", 4799, 5499, "Y", 11, None,
     "Weight and body composition, syncs over wifi."),

    # ---- Recovery ----
    ("P6001", "Massage Gun", "Recovery", 6499, 7999, "Y", 9, None,
     "Four heads, five speeds, quiet motor."),
    ("P6002", "Resistance Band Set", "Recovery", 999, None, "Y", 48, None,
     "Three tensions with a door anchor."),
    ("P6003", "Cork Massage Ball", "Recovery", 449, None, "Y", 75, None,
     "Firm, grippy, and it does not roll away."),
    ("P6004", "Compression Boots", "Recovery", 18999, None, "LOW", 2, None,
     "Sequential compression, four chambers."),
]


def _build(row):
    pid, title, dept, price, was, state, qty, sizes, blurb = row
    return {
        "product_id": pid,
        "item_title": title,
        "blurb": blurb,
        "price_paise": price * 100,
        "was_price_paise": was * 100 if was else None,
        "dept": dept,
        "stock_state": state,
        "qty_available": qty,
        "variants": [
            {"variant_ref": f"{pid}-{label}", "opt_size": label, "qty_available": n}
            for label, n in (sizes or [])
        ],
    }


CATALOG = [_build(r) for r in _ROWS]

#: Every department, for the storefront's category navigation. Derived rather
#: than hardcoded, so adding a product in a new department needs no other change.
DEPARTMENTS = sorted({p["dept"] for p in CATALOG})

#: Vouchers. Note "voucher", not "promotion" - the engine's vocabulary and this
#: platform's vocabulary differ on purpose, so the adapter has to map it.
VOUCHERS = {
    "WELCOME10": {"kind": "PCT", "value": 10, "min_spend_paise": 0, "live": True},
    "FLAT500": {"kind": "FLAT", "value": 50000, "min_spend_paise": 300000, "live": True},
    "BIG20": {"kind": "PCT", "value": 20, "min_spend_paise": 1000000, "live": True},
    # Expired, so the PROMOTION_FAILED friction path is reachable.
    "SUMMER25": {"kind": "PCT", "value": 25, "min_spend_paise": 0, "live": False},
}