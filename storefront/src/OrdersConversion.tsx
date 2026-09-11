import { useEffect, useState } from "react";
import { console_api, type Conversion } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

const RANGE_OPTIONS = [7, 30, 90] as const;

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
 * stated as such rather than guessed - the reference design's "Conversion
 * Rate" is deliberately not shown as a single ambiguous figure here; it is
 * split into the two rates this schema can actually name (cart->checkout,
 * checkout->completed).
 */
export function OrdersConversion({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<Conversion | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    console_api
      .conversion(days, true)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Could not load conversion data.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  if (error) return <p className="empty">{error}</p>;
  if (!data) return <p className="empty">Loading...</p>;

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Orders &amp; Conversion</h2>
        <p className="overview-subtitle">
          See how many carts turn into checkouts, and how many checkouts turn
          into orders.
        </p>
      </div>
      <div className="range-picker" role="group" aria-label="Date range">
        {RANGE_OPTIONS.map((n) => (
          <button
            key={n}
            className={`range-btn ${n === days ? "active" : ""}`}
            onClick={() => setDays(n)}
          >
            Last {n} days
          </button>
        ))}
      </div>
    </div>
  );

  if (!data.funnel_has_history) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              No cart-creation history yet. This connection hasn't had a cart
              created since funnel instrumentation shipped - figures will
              appear as shoppers start browsing.
            </p>
          </div>
        </section>
      </div>
    );
  }

  if (data.carts_created === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">No carts created in this window yet.</p>
          </div>
        </section>
      </div>
    );
  }

  // A real, deterministic "biggest drop" signal: which of the two real
  // stages lost more carts/attempts in absolute terms. Not a guess - both
  // counts come from the same window this page already displays.
  const cartDrop = data.abandoned_before_checkout;
  const checkoutDrop = data.failed_checkout_attempts;
  const biggestDrop =
    cartDrop === 0 && checkoutDrop === 0
      ? null
      : cartDrop >= checkoutDrop
        ? { label: "Cart created but never reached checkout", value: cartDrop, section: "customers" }
        : { label: "Checkout attempted but declined or failed", value: checkoutDrop, section: "payments" };

  const prior = data.prior;
  const cartRateDelta =
    prior && prior.cart_to_checkout_rate !== null && data.cart_to_checkout_rate !== null
      ? Math.round((data.cart_to_checkout_rate - prior.cart_to_checkout_rate) * 10) / 10
      : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Carts created</div>
          <div className="kpi-value">{data.carts_created}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Checkout attempts</div>
          <div className="kpi-value">{data.checkout_attempts}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Completed orders</div>
          <div className="kpi-value">{data.completed_orders}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Cart → checkout rate</div>
          <div className="kpi-value">
            {data.cart_to_checkout_rate === null ? "—" : `${data.cart_to_checkout_rate}%`}
          </div>
          {cartRateDelta !== null ? (
            <div className={`kpi-compare ${cartRateDelta > 0 ? "up" : cartRateDelta < 0 ? "down" : "flat"}`}>
              {cartRateDelta > 0 ? "+" : ""}
              {cartRateDelta} pts vs. prior {days}d
            </div>
          ) : (
            <p className="note" style={{ margin: "4px 0 0", fontSize: 11 }}>
              No prior-period data yet.
            </p>
          )}
        </div>
      </div>

      <div className="overview-trend-row">
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Funnel</span>
          </div>
          <div className="panel-body">
            <div className="funnel">
              <FunnelStage value={data.carts_created} label="Carts created" />
              <FunnelArrow rate={data.cart_to_checkout_rate} />
              <FunnelStage value={data.checkout_attempts} label="Checkout attempted" />
              <FunnelArrow rate={data.checkout_success_rate} />
              <FunnelStage value={data.completed_orders} label="Completed" emphasis />
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Biggest drop-off</span>
          </div>
          <div className="panel-body">
            {!biggestDrop ? (
              <p className="empty">No drop-off in this window - every cart reached a completed order.</p>
            ) : (
              <button
                className="attention-row"
                onClick={() => onNavigate(biggestDrop.section)}
                style={{ width: "100%" }}
              >
                <span className="attention-label">{biggestDrop.label}</span>
                <span className="note" style={{ margin: 0 }}>
                  {biggestDrop.value} {biggestDrop.value === 1 ? "case" : "cases"} in this window
                </span>
              </button>
            )}
          </div>
        </section>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Funnel detail</span>
        </div>
        <div className="panel-body">
          <div className="summary-card-row">
            <div className="summary-card">
              <div className="summary-card-title">Abandoned before checkout</div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>{data.abandoned_before_checkout}</div>
            </div>
            <div className="summary-card">
              <div className="summary-card-title">Failed / declined at checkout</div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>{data.failed_checkout_attempts}</div>
            </div>
            <div className="summary-card">
              <div className="summary-card-title">Checkout success rate</div>
              <div style={{ fontWeight: 600, fontSize: 14 }}>
                {data.checkout_success_rate === null ? "—" : `${data.checkout_success_rate}%`}
              </div>
            </div>
          </div>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>

      <p className="note" style={{ margin: 0 }}>{data.scope_note}</p>
    </div>
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
