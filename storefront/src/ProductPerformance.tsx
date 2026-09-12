import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report, type ProductPerformance as Performance } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

const RANGE_OPTIONS = [7, 30, 90] as const;

/**
 * Product Performance.
 *
 * Sourced entirely from `OrderLine` - one row per product per *completed*
 * (paid) order (see `OrderLine`'s own docstring: written once, at the same
 * call sites as the payment ledger, never for a decline or an abandoned
 * cart, and a retried already-paid cart upserts rather than duplicates by
 * construction). The Merchant Copilot reads the identical
 * `db.product_performance` call this page does, so the two can never
 * disagree about what's selling best or generating the most revenue.
 *
 * "Selling best" means quantity. "Highest revenue" means revenue. The two
 * rankings are kept in separate tabs rather than one blended list, because
 * the same product topping both is a coincidence worth noticing, not an
 * assumption worth baking in.
 */
export function ProductPerformance({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<Performance | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [view, setView] = useState<"quantity" | "revenue" | "lowest">("quantity");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      console_api.productPerformance(days, 10, true),
      console_api.report(days).catch(() => null),
    ])
      .then(([p, r]) => {
        if (cancelled) return;
        setData(p);
        setReport(r);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Could not load product performance.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Product Performance</h2>
        <p className="overview-subtitle">
          See which products are actually selling, by quantity and by revenue.
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

  if (error) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">{error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  if (!data.has_data) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              No completed orders with product detail in this window yet.
            </p>
            <p className="note">{data.historical_note}</p>
          </div>
        </section>
        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>
    );
  }

  const currency = report?.total_sales_currency ?? "";
  const topSeller = data.top_by_quantity[0];
  const topRevenue = data.top_by_revenue[0];

  // Real, deterministic: what share of ALL product revenue this window the
  // top-5-by-revenue list accounts for - against total_revenue (every
  // distinct product summed), never against the list's own sum.
  const top5Revenue = data.top_by_revenue
    .slice(0, 5)
    .reduce((sum, p) => sum + Number(p.revenue), 0);
  const totalRevenueNum = Number(data.total_revenue);
  const top5Share = totalRevenueNum > 0 ? Math.round((top5Revenue / totalRevenueNum) * 100) : null;

  const rows =
    view === "quantity"
      ? data.top_by_quantity
      : view === "revenue"
        ? data.top_by_revenue
        : data.lowest_performers;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Distinct products sold</div>
          <div className="kpi-value">{data.distinct_products_sold}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Top seller (units)</div>
          <div className="kpi-value" style={{ fontSize: 16 }}>
            {topSeller ? `${topSeller.product_name}` : "—"}
          </div>
          {topSeller && <p className="note" style={{ margin: "4px 0 0" }}>{topSeller.quantity} sold</p>}
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Top by revenue</div>
          <div className="kpi-value" style={{ fontSize: 16 }}>
            {topRevenue ? topRevenue.product_name : "—"}
          </div>
          {topRevenue && (
            <p className="note" style={{ margin: "4px 0 0" }}>
              {topRevenue.revenue} {currency}
            </p>
          )}
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Top 5 revenue share</div>
          <div className="kpi-value">{top5Share === null ? "—" : `${top5Share}%`}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>of all product revenue</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Ranked products</span>
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
            <table className="mini-table" style={{ marginTop: 8 }}>
              <thead>
                <tr>
                  <th>Product</th>
                  <th>Quantity</th>
                  <th>Revenue</th>
                  <th>Orders</th>
                  {view !== "lowest" && <th>vs. prior</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => {
                  const changePct = view === "revenue" ? p.revenue_change_pct : p.quantity_change_pct;
                  return (
                    <tr key={p.product_id}>
                      <td>{p.product_name}</td>
                      <td className="num">{p.quantity}</td>
                      <td className="num">
                        {p.revenue} {currency}
                      </td>
                      <td className="num">{p.order_count}</td>
                      {view !== "lowest" && (
                        <td className="num">
                          {changePct === undefined ? (
                            "—"
                          ) : changePct === null ? (
                            <span style={{ color: "var(--ink-3)" }}>new</span>
                          ) : (
                            <span style={{ color: changePct >= 0 ? "var(--ok)" : "var(--friction)" }}>
                              {changePct >= 0 ? "+" : ""}
                              {changePct}%
                            </span>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          <p className="note" style={{ marginTop: 14 }}>
            {data.distinct_products_sold} distinct product
            {data.distinct_products_sold === 1 ? "" : "s"} sold in this window.
            "Lowest selling" only ranks products that sold at least once - it
            cannot name a product with zero sales without re-scanning the
            full catalog, which is not attempted here.
          </p>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
