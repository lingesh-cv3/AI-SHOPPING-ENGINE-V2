"""Drive the reference adapter against a live sample merchant.

Start the sample merchant first, in a separate terminal:

    python -m uvicorn sample_merchant.api.main:app --port 8001

Then run this from the project root:

    python verify_adapter.py
"""

import asyncio

from adapters.sample import SampleMerchantAdapter
from shared.models import (
    CapabilityUnsupported,
    CommerceError,
    Operation,
    PaymentRecoveryMethod,
)


async def main() -> None:
    a = SampleMerchantAdapter(
        connection_id="conn_demo",
        base_url="http://127.0.0.1:8001",
        currency="INR",
        storefront_url="https://shop.example",
    )

    print("== capabilities ==")
    caps = await a.get_capabilities()
    print("supports checkout:", caps.supports(Operation.CHECKOUT))
    print("supports recoverPayment:", caps.supports(Operation.RECOVER_PAYMENT))
    print("unsupported:", [str(o) for o in caps.unsupported()])

    print("\n== paise -> Money ==")
    r = await a.search_products("running")
    p = r.products[0]
    print(f"{p.title}: price={p.price} was={p.compare_at_price} avail={p.availability}")
    print(
        "variants:",
        [(v.variant_id, str(v.availability), v.quantity_available) for v in p.variants],
    )

    print("\n== dead search ==")
    d = await a.search_products("running shoes under 5000")
    print("products:", len(d.products), "is_dead_search:", d.is_dead_search)

    print("\n== HTTP-200-error trap ==")
    try:
        await a.get_product("NOPE")
        print("LEAK: 200-with-error slipped through")
    except CommerceError as e:
        print("caught:", e.code, "| retryable:", e.retryable)

    print("\n== out-of-stock variant ==")
    inv = await a.check_inventory("P1001", variant_id="P1001-10")
    print("P1001-10:", inv.availability, "qty:", inv.quantity_available)

    print("\n== cart ==")
    cart = await a.create_cart()
    cart = await a.add_to_cart(cart.cart_id, "P1001", variant_id="P1001-8", quantity=2)
    print(
        f"{cart.cart_id}: items={cart.item_count} subtotal={cart.subtotal} "
        f"tax={cart.tax_total} total={cart.grand_total}"
    )

    print("\n== promotion paths ==")
    for code in ("SUMMER25", "NOSUCHCODE"):
        try:
            await a.apply_promotion(cart.cart_id, code)
        except CommerceError as e:
            print(f"{code} ->", e.code)
    pr = await a.apply_promotion(cart.cart_id, "WELCOME10")
    print("WELCOME10 applied, discount:", pr.discount)

    print("\n== checkout ==")
    res = await a.checkout(cart.cart_id, idempotency_key="idem-ok-1")
    print(
        "succeeded:", res.succeeded,
        "| status:", res.order.status,
        "| payment:", res.payment_status,
    )
    print("paid:", res.order.amount_paid, "of", res.order.grand_total)
    res2 = await a.checkout(cart.cart_id, idempotency_key="idem-ok-1")
    print("idempotent replay same order:", res.order.order_id == res2.order.order_id)

    print("\n== DECLINE (the recovery case) ==")
    c2 = await a.create_cart()
    c2 = await a.add_to_cart(c2.cart_id, "P1002", quantity=1)
    dec = await a.checkout_with_card(
        c2.cart_id, card_last4="0002", idempotency_key="idem-dec-1"
    )
    print(
        "succeeded:", dec.succeeded,
        "| order status:", dec.order.status,
        "| payment:", dec.payment_status,
    )
    print("decline reason:", dec.decline_reason)
    print("order exists, unpaid:", dec.order.order_id, "paid:", dec.order.amount_paid)

    print("\n== recoverPayment where platform cannot ==")
    try:
        await a.recover_payment(
            dec.order.order_id,
            method=PaymentRecoveryMethod.SPLIT_PAYMENT,
            idempotency_key="x",
        )
        print("LEAK: improvised a recovery")
    except CapabilityUnsupported as e:
        print("refused:", e.code, "|", e.message)

    await a.close()


if __name__ == "__main__":
    asyncio.run(main())