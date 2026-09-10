import { useEffect, useState } from "react";
import { console_api, type ProductPerformance as Performance } from "./api";

/**
 * Full product-performance breakdown: quantity ranking, revenue ranking,
 * lowest performers. Reads `/api/products`, the same repository function
 * (`db.product_performance`) the Merchant Copilot already answers from -
 * the UI and the Copilot cannot disagree because they read the same call.
 */
export function ProductPerformance() {
  const [data, setData] = useState<Performance | null>(null);
  const [view, setView] = useState<"quantity" | "revenue" | "lowest">("quantity");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .productPerformance(30, 10)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load product performance."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!data) return <p className="empty">Loading...</p>;

  if (!data.has_data) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Product performance</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            No completed orders with product detail in this window yet.
          </p>
          <p className="note">{data.historical_note}</p>
        </div>
      </section>
    );
  }

  const rows =
    view === "quantity"
      ? data.top_by_quantity
      : view === "revenue"
        ? data.top_by_revenue
        : data.lowest_performers;

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Product performance</span>
        <span className="eyebrow">last {data.days} days</span>
      </div>
      <div className="panel-body">
        <div className="tabs" role="tablist">
          <button aria-selected={view === "quantity"} onClick={() => setView("quantity")}>
            Best selling
          </button>
          <button aria-selected={view === "revenue"} onClick={() => setView("revenue")}>
            By revenue
          </button>
          <button aria-selected={view === "lowest"} onClick={() => setView("lowest")}>
            Lowest selling
          </button>
        </div>

        <p className="note" style={{ marginTop: 12 }}>
          {view === "lowest" ? data.lowest_performers_note : data.historical_note}
        </p>

        {rows.length === 0 ? (
          <p className="empty">Nothing to show for this view.</p>
        ) : (
          rows.map((p) => (
            <div key={p.product_id} className="frictionrow">
              <span>{p.product_name}</span>
              <span className="num">
                {p.quantity} sold · {p.revenue} · {p.order_count} order
                {p.order_count === 1 ? "" : "s"}
              </span>
            </div>
          ))
        )}

        <p className="note" style={{ marginTop: 14 }}>
          {data.distinct_products_sold} distinct product
          {data.distinct_products_sold === 1 ? "" : "s"} sold in this window.
          "Lowest selling" only ranks products that sold at least once - it
          cannot name a product with zero sales without re-scanning the full
          catalog, which is not attempted here.
        </p>
      </div>
    </section>
  );
}
