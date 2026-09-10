import { useEffect, useState } from "react";
import { console_api, type Conversion } from "./api";

/**
 * Orders & Conversion.
 *
 * A real two-stage funnel: cart created -> checkout attempted -> completed
 * order. Cart creation is genuinely instrumented (`FunnelEvent`, written at
 * `shop.py::create_cart` for every visitor, guest or signed-in - see that
 * model's own docstring), and checkout-attempt-to-completion was already
 * real (`ExecutionAttempt`). Still explicitly NOT a sessions/visits funnel:
 * this engine has no page-view event for a guest before they create a
 * cart, so "how many people looked at the shop" stays unanswerable and is
 * stated as such rather than guessed.
 */
export function OrdersConversion() {
  const [data, setData] = useState<Conversion | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .conversion(30)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load conversion data."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!data) return <p className="empty">Loading...</p>;

  if (!data.funnel_has_history) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Orders &amp; conversion</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            No cart-creation history yet. This connection hasn't had a cart
            created since funnel instrumentation shipped - figures will
            appear as shoppers start browsing.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Orders &amp; conversion</span>
        <span className="eyebrow">last {data.days} days</span>
      </div>
      <div className="panel-body">
        {data.carts_created === 0 ? (
          <p className="empty">No carts created in this window yet.</p>
        ) : (
          <>
            <div className="funnel">
              <FunnelStage value={data.carts_created} label="Carts created" />
              <FunnelArrow rate={data.cart_to_checkout_rate} />
              <FunnelStage value={data.checkout_attempts} label="Checkout attempted" />
              <FunnelArrow rate={data.checkout_success_rate} />
              <FunnelStage value={data.completed_orders} label="Completed" emphasis />
            </div>

            <div className="figures" style={{ marginTop: 20 }}>
              <Figure
                value={data.abandoned_before_checkout}
                label="Abandoned before checkout"
                warn={data.abandoned_before_checkout > 0}
              />
              <Figure
                value={data.failed_checkout_attempts}
                label="Failed / declined at checkout"
                warn={data.failed_checkout_attempts > 0}
              />
              <Figure
                value={data.cart_to_checkout_rate === null ? "—" : `${data.cart_to_checkout_rate}%`}
                label="Cart → checkout rate"
              />
              <Figure
                value={data.checkout_success_rate === null ? "—" : `${data.checkout_success_rate}%`}
                label="Checkout success rate"
              />
            </div>
          </>
        )}
        <p className="note" style={{ marginTop: 16 }}>{data.scope_note}</p>
      </div>
    </section>
  );
}

function FunnelStage({
  value,
  label,
  emphasis,
}: {
  value: number;
  label: string;
  emphasis?: boolean;
}) {
  return (
    <div className="funnel-stage">
      <div className={emphasis ? "figure num" : "figure num"}>{value}</div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}

function FunnelArrow({ rate }: { rate: number | null }) {
  return (
    <div className="funnel-arrow" aria-hidden="true">
      <span>→</span>
      <span className="funnel-arrow-rate">{rate === null ? "—" : `${rate}%`}</span>
    </div>
  );
}

function Figure({ value, label, warn }: { value: number | string; label: string; warn?: boolean }) {
  return (
    <div>
      <div className={warn ? "figure num warn" : "figure num"}>{value}</div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}
