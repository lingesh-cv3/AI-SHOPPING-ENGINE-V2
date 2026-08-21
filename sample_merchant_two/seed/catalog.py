"""Catalog for Kettle & Bloom, a coffee roaster.

Deliberately unlike Northfield in every dimension the adapter has to handle:

- money is a decimal *string* with a separate currency code, not integer paise
- stock is a boolean, with no counts at all - this platform simply does not expose
  how many are left
- field names are camelCase, GraphQL convention
- the vertical is coffee rather than running gear

That second point is the one worth watching. The engine's InventoryStatus has a
quantity_available that is None when a platform does not expose a number, and None
is deliberately distinct from 0. Northfield never exercises that path because it
always gives a count. This merchant never gives one.
"""

_ROWS = [
    ("KB-ETH-01", "Ethiopia Guji Natural", "Single origin", "1450.00", True,
     [("250g whole bean", True), ("250g ground", True), ("1kg whole bean", False)],
     "Blueberry, jasmine, and a syrupy body. Natural process, altitude 2100m."),
    ("KB-COL-02", "Colombia Huila Washed", "Single origin", "1250.00", True,
     [("250g whole bean", True), ("250g ground", True), ("1kg whole bean", True)],
     "Red apple and caramel. Forgiving on any brewer."),
    ("KB-KEN-03", "Kenya Nyeri AA", "Single origin", "1690.00", False,
     [("250g whole bean", False), ("250g ground", False)],
     "Blackcurrant and tomato leaf. Sold out until the next landing."),
    ("KB-BRA-04", "Brazil Cerrado Pulped Natural", "Single origin", "1100.00", True,
     [("250g whole bean", True), ("1kg whole bean", True)],
     "Hazelnut, milk chocolate, low acidity."),
    ("KB-BLD-05", "Morning Bell Espresso Blend", "Blends", "1190.00", True,
     [("250g whole bean", True), ("250g ground", True), ("1kg whole bean", True)],
     "Built for milk. Chocolate, toffee, a clean finish."),
    ("KB-BLD-06", "Nightshift Dark Roast", "Blends", "990.00", True,
     [("250g whole bean", True), ("1kg whole bean", True)],
     "Deep, smoky, unapologetic. For people who take it black."),
    ("KB-BLD-07", "House Filter Blend", "Blends", "1050.00", True,
     [("250g whole bean", True), ("250g ground", True)],
     "The everyday one. Balanced, sweet, hard to get wrong."),
    ("KB-DEC-08", "Swiss Water Decaf", "Blends", "1150.00", True,
     [("250g whole bean", True), ("250g ground", True)],
     "Chemical-free decaffeination. Tastes like coffee, not like decaf."),
    ("KB-EQP-09", "Pour Over Dripper", "Equipment", "1890.00", True, [],
     "Ceramic cone, spiral ribs, one size fits most carafes."),
    ("KB-EQP-10", "Burr Hand Grinder", "Equipment", "4990.00", True, [],
     "Stainless burrs, 36 clicks, holds enough for two cups."),
    ("KB-EQP-11", "Gooseneck Kettle 1L", "Equipment", "3450.00", False, [],
     "Precise pour, variable temperature. Back in stock next month."),
    ("KB-EQP-12", "Digital Brew Scale", "Equipment", "2290.00", True, [],
     "0.1g resolution with a built-in timer."),
    ("KB-SUB-13", "Roaster's Choice Subscription", "Subscriptions", "1350.00", True,
     [("Every week", True), ("Every fortnight", True), ("Every month", True)],
     "We pick, you drink. Cancel whenever."),
    ("KB-SUB-14", "Espresso Subscription", "Subscriptions", "1290.00", True,
     [("Every fortnight", True), ("Every month", True)],
     "Always a blend that works in milk."),
    ("KB-GFT-15", "Taster Gift Box", "Gifts", "2450.00", True, [],
     "Four 100g bags across our current lineup."),
    ("KB-GFT-16", "Brew Kit Gift Set", "Gifts", "6990.00", True, [],
     "Dripper, filters, scale, and a bag of the House Filter."),
]


def _build(row):
    pid, name, collection, price, in_stock, options, story = row
    return {
        "id": pid,
        "name": name,
        "story": story,
        "price": {"amount": price, "currencyCode": "INR"},
        "collection": collection,
        # A boolean and nothing else. There is no count to give.
        "inStock": in_stock,
        "options": [
            {"id": f"{pid}::{label}", "label": label, "inStock": ok}
            for label, ok in options
        ],
    }


CATALOG = [_build(r) for r in _ROWS]
COLLECTIONS = sorted({p["collection"] for p in CATALOG})

#: Note "discountCode", not "voucher". Third vocabulary for the same concept.
DISCOUNT_CODES = {
    "FIRSTBAG": {"kind": "PERCENT", "value": 15, "minSpend": "0.00", "active": True},
    "BREWKIT500": {"kind": "FIXED", "value": "500.00", "minSpend": "4000.00", "active": True},
    "HARVEST": {"kind": "PERCENT", "value": 20, "minSpend": "0.00", "active": False},
}