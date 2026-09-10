import { useEffect, useState } from "react";
import { console_api, type Conversion } from "./api";

/**
 * Orders & Conversion.
 *
 * This engine does not timestamp cart creation as a distinct event, so a
 * real session -> cart -> checkout -> paid funnel cannot be reported
 * honestly from existing data - see `db.checkout_conversion`'s own
 * docstring. What CAN be reported honestly, because every checkout attempt
 * (successful or declined) already writes a ledger row before the platform
 * is called, is checkout-attempt-to-completion. That is what this surface
 * shows, labeled as exactly that rather than dressed up as a full funnel.
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

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Orders &amp; conversion</span>
        <span className="eyebrow">last {data.days} days</span>
      </div>
      <div className="panel-body">
        {data.checkout_attempts === 0 ? (
          <p className="empty">No checkout attempts recorded in this window yet.</p>
        ) : (
          <div className="figures">
            <Figure value={data.checkout_attempts} label="Checkout attempts" />
            <Figure value={data.completed_orders} label="Completed" />
            <Figure
              value={data.failed_checkout_attempts}
              label="Failed / declined"
              warn={data.failed_checkout_attempts > 0}
            />
            <Figure
              value={data.checkout_success_rate === null ? "—" : `${data.checkout_success_rate}%`}
              label="Success rate"
            />
          </div>
        )}
        <p className="note" style={{ marginTop: 14 }}>{data.scope_note}</p>
      </div>
    </section>
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
