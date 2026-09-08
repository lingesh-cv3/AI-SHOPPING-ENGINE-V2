"""The order-confirmation email itself: content, send, and the record of it.

One function so the two checkout routes (`/api/chat/pay` and
`/api/shop/{connection}/checkout`) cannot drift into two different confirmation
emails for the same kind of event.
"""

from __future__ import annotations

from engine import db

from .mailer import Mailer


async def confirm_order(
    mailer: Mailer,
    *,
    connection_id: str,
    merchant_name: str,
    order_id: str,
    to_email: str,
) -> None:
    """Send the order confirmation, and record the attempt either way.

    Never raises: a mail failure is not a checkout failure, since the order is
    already placed and paid for by the time this runs. Called after the reply
    to the shopper is already decided, so nothing here can change what they are
    told about the payment itself.
    """
    subject = f"Your order {order_id} - {merchant_name}"
    body = (
        f"Thanks for your order with {merchant_name}.\n\n"
        f"Order: {order_id}\n\n"
        "This confirms your payment went through. Keep this email for your "
        "records - if anything about the order needs sorting out, we already "
        "have your details."
    )

    sent = await mailer.send(to_email, subject, body)
    await db.record_sent_mail(
        connection_id,
        order_id=order_id,
        to_email=to_email,
        subject=subject,
        body=body,
        delivered=sent.delivered,
    )
