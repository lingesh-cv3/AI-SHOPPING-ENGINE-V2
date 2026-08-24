import { useState } from "react";
import type { Cart, CheckoutResult } from "./api";

/**
 * The cart, coupon field, and checkout.
 *
 * The card picker is not something a real storefront would have. It exists so a
 * decline can be produced on demand, because waiting for a real card to fail is not
 * a demo. The three listed here fail the same way on both platforms; merchant
 * specific ones were removed after a card that did nothing on one shop made the
 * demo look broken.
 *
 * A declined checkout renders as an unpaid order rather than an error, because that
 * is what it is: the order exists, the money did not move, and the sale is still
 * recoverable.
 */
export function CartPanel({
  cart,
  result,
  busy,
  error,
  couponHint,
  onPromo,
  onCheckout,
}: {
  cart: Cart | null;
  result: CheckoutResult | null;
  busy: boolean;
  error: string | null;
  couponHint?: string;
  onPromo: (code: string) => void;
  onCheckout: (cardLast4: string) => void;
}) {
  const [code, setCode] = useState("");
  const [card, setCard] = useState("1111");

  return (
    <aside className="panel">
      <div className="panel-head">
        <span className="eyebrow">Cart</span>
        <span className="num" style={{ fontSize: 12 }}>
          {cart?.item_count ?? 0} {cart?.item_count === 1 ? "item" : "items"}
        </span>
      </div>
      <div className="panel-body">
        {!cart || cart.is_empty ? (
          <p className="empty">Nothing here yet. Add something to get started.</p>
        ) : (
          <>
            {cart.lines.map((line) => (
              <div key={line.line_id} className="line">
                <div>
                  {line.title}
                  <div className="line-qty">
                    {line.quantity} &times; {line.unit_price.display}
                  </div>
                </div>
                <span className="num">{line.line_total.display}</span>
              </div>
            ))}

            <div className="totals">
              <div className="total-row">
                <span>Subtotal</span>
                <span className="num">{cart.subtotal.display}</span>
              </div>
              {cart.discount_total && (
                <div className="total-row" style={{ color: "var(--ok)" }}>
                  <span>Discount {cart.applied_promotions.join(", ")}</span>
                  <span className="num">&minus;{cart.discount_total.display}</span>
                </div>
              )}
              {cart.tax_total && (
                <div className="total-row">
                  <span>GST</span>
                  <span className="num">{cart.tax_total.display}</span>
                </div>
              )}
              {cart.shipping_total && (
                <div className="total-row">
                  <span>Delivery</span>
                  <span className="num">{cart.shipping_total.display}</span>
                </div>
              )}
              <div className="total-row grand">
                <span>Total</span>
                <span className="num">{cart.grand_total?.display}</span>
              </div>
            </div>

            <div className="field">
              <input
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="Coupon code"
                aria-label="Coupon code"
              />
              <button disabled={busy || !code} onClick={() => onPromo(code)}>
                Apply
              </button>
            </div>

            {cart.applied_promotions.length === 0 && couponHint && (
              <p className="note">{couponHint}</p>
            )}

            <select
              className="card-picker"
              value={card}
              onChange={(e) => setCard(e.target.value)}
              aria-label="Card"
            >
              <option value="1111">Card ending 1111 — approves</option>
              <option value="0002">Card ending 0002 — no funds</option>
              <option value="0003">Card ending 0003 — expired</option>
            </select>

            <button
              className="add"
              style={{ width: "100%", marginTop: 10 }}
              disabled={busy}
              onClick={() => onCheckout(card)}
            >
              {busy ? "Working…" : "Pay now"}
            </button>
          </>
        )}

        {/* Coupon problems are handled by the assistant, which explains and offers
            an alternative. Repeating a bare error line here would say less. */}
        {error && !error.includes("code") && (
          <p className="note" style={{ color: "var(--friction)" }}>
            {error}
          </p>
        )}

        {result && (
          <div
            style={{
              marginTop: 16,
              borderTop: "1px solid var(--ink)",
              paddingTop: 14,
            }}
          >
            <div className="gate-label">
              {result.succeeded ? "Order placed" : "Payment did not go through"}
            </div>
            <div className="num" style={{ fontSize: 15, fontWeight: 700 }}>
              {result.order?.order_id}
            </div>
            <div className="gate-note">
              {result.order?.status.replace(/_/g, " ").toLowerCase()} &middot; paid{" "}
              {result.order?.amount_paid?.display} of{" "}
              {result.order?.grand_total?.display}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}