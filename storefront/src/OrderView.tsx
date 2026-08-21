import { useEffect, useState } from "react";
import { api, shopperMessage, type Order } from "./api";

/**
 * An order, paid or not.
 *
 * The same component handles both outcomes, which is the point. A declined order
 * is not an error page - the order exists, the items are reserved against it, and
 * the amount outstanding is stated plainly. That framing is what makes the sale
 * recoverable rather than lost, and it is why the engine treats a decline as
 * friction rather than failure.
 *
 * Fetched by id rather than passed from checkout, so this page works equally as a
 * confirmation and as an order lookup later.
 */
export function OrderView({
  orderId,
  onBack,
}: {
  orderId: string;
  onBack: () => void;
}) {
  const [order, setOrder] = useState<Order | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setOrder(null);
    setError(null);
    api
      .order(orderId)
      .then(setOrder)
      .catch((e) => setError(shopperMessage(e)));
  }, [orderId]);

  if (error) {
    return (
      <div className="panel">
        <div className="panel-body">
          <p className="empty">{error}</p>
          <button className="add" onClick={onBack}>
            Back to the shop
          </button>
        </div>
      </div>
    );
  }

  if (!order) return <p className="empty">Loading…</p>;

  const paid = order.payment_status === "CAPTURED";
  const outstanding = !paid && order.amount_paid?.amount === "0.00";

  return (
    <div>
      <button className="backlink" onClick={onBack}>
        &larr; Back to the shop
      </button>

      <div className={paid ? "panel order-ok" : "panel order-unpaid"}>
        <div className="panel-body">
          <div className="eyebrow">
            {paid ? "Order confirmed" : "Order held — payment outstanding"}
          </div>
          <h1 className="detail-title num" style={{ fontSize: 26 }}>
            {order.order_id}
          </h1>

          {paid ? (
            <p className="detail-desc">
              Paid in full. You&rsquo;ll get a confirmation shortly.
            </p>
          ) : (
            <p className="detail-desc">
              Your items are held against this order, but the payment
              didn&rsquo;t complete
              {order.decline_reason
                ? ` — ${order.decline_reason.replace(/_/g, " ").toLowerCase()}`
                : ""}
              . Nothing has been charged.
            </p>
          )}

          <div className="orderstats">
            <div>
              <div className="gate-label">Order status</div>
              <div className="gate-value">
                {order.status.replace(/_/g, " ").toLowerCase()}
              </div>
            </div>
            <div>
              <div className="gate-label">Payment</div>
              <div className="gate-value">
                {order.payment_status.replace(/_/g, " ").toLowerCase()}
              </div>
            </div>
            <div>
              <div className="gate-label">Paid</div>
              <div className="gate-value num">{order.amount_paid?.display}</div>
            </div>
            <div>
              <div className="gate-label">Total</div>
              <div className="gate-value num">{order.grand_total?.display}</div>
            </div>
          </div>

          <div style={{ marginTop: 20 }}>
            <div className="gate-label" style={{ marginBottom: 4 }}>
              Items
            </div>
            {order.lines.map((line) => (
              <div key={line.line_id} className="line">
                <div>
                  {line.title}
                  <div className="line-qty">
                    {line.quantity} &times; {line.unit_price.display}
                    {line.variant_id ? ` · ${line.variant_id.split("-").pop()}` : ""}
                  </div>
                </div>
                <span className="num">{line.line_total.display}</span>
              </div>
            ))}
          </div>

          {outstanding && (
            <p className="note">
              Outstanding: <span className="num">{order.grand_total?.display}</span>.
              Someone from the shop will be in touch, or you can try a different
              card.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}