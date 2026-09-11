import { useEffect, useState } from "react";
import {
  console_api,
  type MerchantReport as Report,
  type ProductPerformance as Performance,
  type SalesSeries,
  type SalesTrend,
} from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";
import { RevenueTrendChart } from "./RevenueTrendChart";

const RANGE_OPTIONS = [7, 30, 90] as const;

/**
 * Sales & Revenue - the analytics-focused twin of Overview's executive
 * summary. Same visual system (KPI row, trend chart, mini-tables, embedded
 * Copilot), different information architecture: this page exists to
 * answer "where is revenue actually coming from and how is it moving",
 * not to summarize every Merchant area the way Overview does.
 *
 * Every figure here is read from the same routes `/api/report`,
 * `/api/sales-trend`, `/api/sales-series` and `/api/products` already
 * give Overview and Product Performance, so this page cannot show a
 * number those pages would contradict. `report`/`trend`/`series`/
 * `products` all re-fetch on every date-range change - there is no
 * client-side recomputation of a figure the backend already aggregates.
 */
export function MerchantReport({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [days, setDays] = useState<number>(30);
  const [report, setReport] = useState<Report | null>(null);
  const [trend, setTrend] = useState<SalesTrend | null>(null);
  const [series, setSeries] = useState<SalesSeries | null>(null);
  const [products, setProducts] = useState<Performance | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      console_api.report(days),
      console_api.salesTrend(days).catch(() => null),
      console_api.salesSeries(days).catch(() => null),
      console_api.productPerformance(days, 5).catch(() => null),
    ])
      .then(([r, t, s, p]) => {
        if (cancelled) return;
        setReport(r);
        setTrend(t);
        setSeries(s);
        setProducts(p);
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Could not load your figures.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  if (error) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Sales &amp; revenue</span>
        </div>
        <div className="panel-body">
          <p className="empty">{error}</p>
        </div>
      </section>
    );
  }

  if (!report) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Sales &amp; revenue</span>
        </div>
        <div className="panel-body">
          <p className="empty">Loading...</p>
        </div>
      </section>
    );
  }

  const noSalesYet = report.completed_order_count === 0;

  // A real, deterministic concentration signal, not an invented one: what
  // share of this window's revenue the single best product accounts for.
  // Shown only when it is high enough to be worth a merchant's attention
  // (>=40% of total sales) and only when there is enough revenue to make
  // the ratio meaningful - never on a single order.
  let concentration: { product: string; pct: number } | null = null;
  if (
    products?.has_data &&
    products.top_by_revenue.length > 0 &&
    report.completed_order_count >= 3 &&
    Number(report.total_sales_amount) > 0
  ) {
    const top = products.top_by_revenue[0];
    const pct = (Number(top.revenue) / Number(report.total_sales_amount)) * 100;
    if (pct >= 40) {
      concentration = { product: top.product_name, pct: Math.round(pct) };
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="overview-header">
        <div>
          <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Sales &amp; Revenue</h2>
          <p className="overview-subtitle">
            Understand where your revenue is coming from and how it is changing.
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

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Revenue</div>
          <div className="kpi-value">
            {noSalesYet ? "0.00" : report.total_sales_amount}
            <span className="kpi-value-unit">{report.total_sales_currency}</span>
          </div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Orders</div>
          <div className="kpi-value">{report.completed_order_count}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Average order value</div>
          <div className="kpi-value">
            {report.average_order_value ?? "—"}
            {report.average_order_value && (
              <span className="kpi-value-unit">{report.total_sales_currency}</span>
            )}
          </div>
          {report.average_order_value &&
            report.priced_order_count < report.completed_order_count && (
              <p className="note" style={{ margin: "4px 0 0", fontSize: 11 }}>
                Based on {report.priced_order_count} of {report.completed_order_count}{" "}
                orders with a recorded amount.
              </p>
            )}
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Revenue change</div>
          {trend && trend.change_pct !== null ? (
            <div
              className={`kpi-value ${Number(trend.change_amount) >= 0 ? "" : ""}`}
              style={{ color: Number(trend.change_amount) >= 0 ? "var(--ok)" : "var(--friction)" }}
            >
              {Number(trend.change_amount) >= 0 ? "+" : ""}
              {trend.change_pct}%
            </div>
          ) : (
            <div className="kpi-value" style={{ color: "var(--ink-3)" }}>
              Not enough data
            </div>
          )}
        </div>
      </div>

      <div className="overview-trend-row">
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Revenue trend</span>
            <span className="eyebrow">
              {trend && trend.prior_order_count > 0
                ? `vs. prior ${trend.days} days`
                : "no prior-period data yet"}
            </span>
          </div>
          <div className="panel-body">
            <RevenueTrendChart series={series} />
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Top by revenue</span>
          </div>
          <div className="panel-body">
            {!products?.has_data ? (
              <p className="empty">No completed orders with product detail yet.</p>
            ) : (
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Product</th>
                    <th>Revenue</th>
                  </tr>
                </thead>
                <tbody>
                  {products.top_by_revenue.slice(0, 5).map((p) => (
                    <tr key={p.product_id}>
                      <td>{p.product_name}</td>
                      <td className="num">
                        {p.revenue} {report.total_sales_currency}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <button className="navlink" style={{ marginTop: 12 }} onClick={() => onNavigate("products")}>
              Full product performance →
            </button>
          </div>
        </section>
      </div>

      {/* Revenue performance: top by revenue vs top by quantity, deliberately
       * not merged into one list - the same product topping both would be
       * an unremarkable coincidence to hide, and different products topping
       * each is the more common, more informative case. */}
      <div className="overview-trend-row">
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Top by quantity sold</span>
          </div>
          <div className="panel-body">
            {!products?.has_data ? (
              <p className="empty">No completed orders with product detail yet.</p>
            ) : (
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Product</th>
                    <th>Quantity</th>
                    <th>Orders</th>
                  </tr>
                </thead>
                <tbody>
                  {products.top_by_quantity.slice(0, 5).map((p) => (
                    <tr key={p.product_id}>
                      <td>{p.product_name}</td>
                      <td className="num">{p.quantity}</td>
                      <td className="num">{p.order_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Attention</span>
          </div>
          <div className="panel-body">
            {!concentration && (!trend || trend.change_pct === null || Number(trend.change_amount) >= 0) ? (
              <p className="empty">No sales signals need your attention right now.</p>
            ) : (
              <>
                {trend && trend.change_pct !== null && Number(trend.change_amount) < 0 && (
                  <div className="frictionrow">
                    <span>Revenue is down vs. the prior period</span>
                    <span className="num warn">{trend.change_pct}%</span>
                  </div>
                )}
                {concentration && (
                  <div className="frictionrow">
                    <span>"{concentration.product}" is a large share of revenue</span>
                    <span className="num">{concentration.pct}%</span>
                  </div>
                )}
              </>
            )}
          </div>
        </section>
      </div>

      {report.supports_payment_recovery && (
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Recovered revenue</span>
          </div>
          <div className="panel-body">
            <p className="note" style={{ margin: 0 }}>
              {report.revenue_recovered} {report.currency} recovered so far -{" "}
              {report.recovery_count} of {report.recovery_opportunities} recovery
              opportunities completed.
            </p>
            <button className="navlink" style={{ marginTop: 8 }} onClick={() => onNavigate("recovery")}>
              Full recovery detail →
            </button>
          </div>
        </section>
      )}

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
