import { useEffect, useState } from "react";
import {
  api,
  console_api,
  getConnection,
  type Capabilities,
  type CatalogAlerts,
  type Connection,
  type Conversion,
  type MerchantReport as Report,
  type ProductPerformance as Performance,
  type SalesSeries,
  type SalesTrend,
  type UnmetDemand,
} from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";
import { RevenueTrendChart } from "./RevenueTrendChart";

type Status = "healthy" | "attention" | "unavailable" | "unsupported";

function Badge({ status }: { status: Status }) {
  const label =
    status === "healthy"
      ? "Healthy"
      : status === "attention"
        ? "Attention"
        : status === "unsupported"
          ? "Unsupported"
          : "Unavailable";
  return <span className={`status-badge ${status}`}>{label}</span>;
}

interface HealthArea {
  key: string;
  label: string;
  status: Status;
  detail: string;
  section: string;
}

const RANGE_OPTIONS = [7, 30, 90] as const;

/**
 * The Merchant tab's primary dashboard.
 *
 * Every figure here is read from the same routes the dedicated sections
 * use (`/api/report`, `/api/catalog`, `/api/conversion`, `/api/products`,
 * `/api/sales-trend`, `/api/sales-series`, `/api/unmet-demand`,
 * `/api/approvals`), so this page cannot show a number a section it links
 * to would contradict - the business-health statuses below are computed
 * client-side from those same fetched objects, never from a separate
 * calculation that could disagree.
 */
export function Overview({ onNavigate }: { onNavigate: (section: string) => void }) {
  const [days, setDays] = useState<number>(30);
  const [report, setReport] = useState<Report | null>(null);
  const [catalog, setCatalog] = useState<CatalogAlerts | null>(null);
  const [conversion, setConversion] = useState<Conversion | null>(null);
  const [trend, setTrend] = useState<SalesTrend | null>(null);
  const [series, setSeries] = useState<SalesSeries | null>(null);
  const [products, setProducts] = useState<Performance | null>(null);
  const [demand, setDemand] = useState<UnmetDemand | null>(null);
  const [pendingRecovery, setPendingRecovery] = useState<number>(0);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [merchantName, setMerchantName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The merchant's real display name ("Kettle & Bloom Coffee"), not the
  // technical platform id `caps.platform` gives ("kettle-graphql") - from
  // the same public connections list the shopper-facing header already
  // reads, not a second, invented source of the merchant's name.
  useEffect(() => {
    let cancelled = false;
    api
      .connections()
      .then((list: Connection[]) => {
        if (cancelled) return;
        const mine = list.find((c) => c.connection_id === getConnection());
        setMerchantName(mine?.merchant_name ?? null);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      console_api.report(days),
      console_api.catalogAlerts().catch(() => null),
      console_api.conversion(days).catch(() => null),
      console_api.salesTrend(days).catch(() => null),
      console_api.salesSeries(days).catch(() => null),
      console_api.productPerformance(days, 5).catch(() => null),
      console_api.unmetDemand(days, 5).catch(() => null),
      console_api.queue().catch(() => null),
      console_api.capabilities().catch(() => null),
    ])
      .then(([r, c, conv, t, s, p, d, q, cp]) => {
        if (cancelled) return;
        setReport(r);
        setCatalog(c);
        setConversion(conv);
        setTrend(t);
        setSeries(s);
        setProducts(p);
        setDemand(d);
        setCaps(cp);
        if (q) {
          const recoveryActions = new Set([
            "RETRY_PAYMENT",
            "OFFER_ALTERNATE_PAYMENT",
            "SPLIT_PAYMENT",
          ]);
          setPendingRecovery(
            q.approvals.filter((a) => recoveryActions.has(a.action_type)).length,
          );
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Could not load your overview.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  if (error) return <p className="empty">{error}</p>;
  if (!report || !caps) return <p className="empty">Loading...</p>;

  const noSalesYet = report.completed_order_count === 0;

  // Business health, computed from data already fetched above - never a
  // second, independent calculation that could disagree with the section
  // it summarizes.
  const health: HealthArea[] = [
    {
      key: "sales",
      label: "Sales & Revenue",
      section: "sales",
      ...(noSalesYet
        ? { status: "unavailable" as Status, detail: "No completed orders yet" }
        : trend && trend.change_pct !== null && Number(trend.change_amount) < 0
          ? { status: "attention" as Status, detail: `Down ${trend.change_pct}% vs. the prior period` }
          : { status: "healthy" as Status, detail: `${report.completed_order_count} completed orders` }),
    },
    {
      key: "orders",
      label: "Orders & Conversion",
      section: "orders",
      ...(!conversion || conversion.checkout_attempts === 0
        ? { status: "unavailable" as Status, detail: "No checkout attempts yet" }
        : conversion.checkout_success_rate !== null && conversion.checkout_success_rate < 60
          ? { status: "attention" as Status, detail: `${conversion.checkout_success_rate}% checkout success rate` }
          : { status: "healthy" as Status, detail: `${conversion.checkout_success_rate}% checkout success rate` }),
    },
    {
      key: "inventory",
      label: "Inventory & Catalog",
      section: "inventory",
      ...(!catalog || !catalog.reachable
        ? { status: "unavailable" as Status, detail: "Platform unreachable for inventory data" }
        : catalog.out_of_stock.length > 0
          ? { status: "attention" as Status, detail: `${catalog.out_of_stock.length} product${catalog.out_of_stock.length === 1 ? "" : "s"} out of stock` }
          : { status: "healthy" as Status, detail: `${catalog.scanned} products scanned, none out of stock` }),
    },
    {
      key: "payments",
      label: "Payments & Checkout",
      section: "payments",
      ...(report.friction.find((f) => f.type === "PAYMENT_DECLINED") === undefined
        ? { status: "healthy" as Status, detail: "No payment failures in this window" }
        : { status: "attention" as Status, detail: `${report.friction.find((f) => f.type === "PAYMENT_DECLINED")?.count} payment failure(s)` }),
    },
    {
      key: "recovery",
      label: "AI / Recovery",
      section: "recovery",
      ...(!report.supports_payment_recovery
        ? { status: "unsupported" as Status, detail: "Not supported on this platform" }
        : pendingRecovery > 0
          ? { status: "attention" as Status, detail: `${pendingRecovery} pending your approval` }
          : { status: "healthy" as Status, detail: `${report.recovery_count} recovered so far` }),
    },
    {
      key: "returns",
      label: "Returns",
      section: "returns",
      status: "unsupported" as Status,
      detail: "No adapter implements returns yet",
    },
  ];

  const overallHealthy = health.every(
    (h) => h.status === "healthy" || h.status === "unsupported",
  );
  const anyAttention = health.some((h) => h.status === "attention");

  const attention: { label: string; detail: string; section: string }[] = [];
  if (report.waiting_for_you > 0) {
    attention.push({
      label: `${report.waiting_for_you} case${report.waiting_for_you === 1 ? "" : "s"} waiting on you`,
      detail: "A person needs to decide something before the shopper can move on.",
      section: "ai-commerce",
    });
  }
  if (pendingRecovery > 0) {
    attention.push({
      label: `${pendingRecovery} payment ${pendingRecovery === 1 ? "recovery" : "recoveries"} pending your approval`,
      detail: "A declined payment can be recovered, but only you can approve it.",
      section: "payments",
    });
  }
  if (catalog && !catalog.reachable) {
    attention.push({
      label: "Your platform could not be reached for inventory data",
      detail: "The last catalog scan failed. Figures below may be stale or missing.",
      section: "inventory",
    });
  }
  if (demand && demand.queries.length > 0) {
    attention.push({
      label: `Shoppers searched for "${demand.queries[0].query}" and found nothing`,
      detail: `Asked ${demand.queries[0].times_asked} time${demand.queries[0].times_asked === 1 ? "" : "s"} in this window.`,
      section: "customers",
    });
  }

  // Product-specific attention, deliberately narrower than the reference's
  // "Products Needing Attention" - only the two conditions this schema can
  // actually name per product (out of stock, low stock). No per-product
  // conversion or returns figure exists anywhere in this data model, so
  // none is invented here; see PROGRESS.md for why.
  const productsNeedingAttention: { title: string; issue: string; section: string }[] = [];
  if (catalog?.reachable) {
    for (const p of catalog.out_of_stock.slice(0, 4)) {
      productsNeedingAttention.push({ title: p.title, issue: "Out of stock", section: "inventory" });
    }
    if (catalog.low_stock_available) {
      for (const p of catalog.low_stock.slice(0, 4 - productsNeedingAttention.length)) {
        productsNeedingAttention.push({ title: p.title, issue: "Low stock", section: "inventory" });
      }
    }
  }

  const opportunities: { title: string; evidence: string; section: string; action: string }[] = [];
  if (report.supports_payment_recovery) {
    const pending = report.recovery_opportunities - report.recovery_count;
    if (pending > 0) {
      opportunities.push({
        title: "Recoverable revenue is sitting in the queue",
        evidence: `${pending} of ${report.recovery_opportunities} recovery opportunities not yet completed.`,
        section: "payments",
        action: "View Payments & Checkout",
      });
    }
  }
  if (products?.has_data && products.top_by_revenue.length > 0 && products.top_by_quantity.length > 0) {
    const topByRevenue = products.top_by_revenue[0];
    const topByQuantity = products.top_by_quantity[0];
    if (topByRevenue.product_id !== topByQuantity.product_id) {
      opportunities.push({
        title: `"${topByRevenue.product_name}" earns more per sale than your best seller by volume`,
        evidence: `${topByRevenue.revenue} ${report.total_sales_currency} vs. ${topByQuantity.quantity} units for "${topByQuantity.product_name}".`,
        section: "products",
        action: "View Product Performance",
      });
    }
  }
  if (catalog?.reachable && catalog.out_of_stock.length > 0) {
    opportunities.push({
      title: `${catalog.out_of_stock.length} product${catalog.out_of_stock.length === 1 ? "" : "s"} out of stock right now`,
      evidence: catalog.complete
        ? "Restocking these keeps sales you'd otherwise lose to a dead product page."
        : `Across the ${catalog.scanned} products scanned so far.`,
      section: "inventory",
      action: "View Inventory",
    });
  }
  if (demand && demand.queries.length > 0) {
    opportunities.push({
      title: `Shoppers keep searching for "${demand.queries[0].query}"`,
      evidence: `Asked ${demand.queries[0].times_asked} time${demand.queries[0].times_asked === 1 ? "" : "s"} in this window - nothing in the catalog answers it.`,
      section: "customers",
      action: "View Shopping Insights",
    });
  }

  const displayName = merchantName ?? caps.platform;
  const initials = displayName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("") || "?";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="overview-header">
        <div>
          <div className="overview-wordmark">CV3 · MERCHANT</div>
          <h2 style={{ margin: "4px 0 0", fontSize: "var(--step-4)" }}>{displayName}</h2>
          <p className="overview-subtitle">See how your business is going, and sell accordingly.</p>
        </div>
        <div className="overview-header-right">
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
          <div className="overview-account-chip">
            <span className="overview-account-avatar">{initials}</span>
            <span className="overview-account-text">
              <span className="overview-account-name">{displayName}</span>
              <span className="eyebrow">Merchant account</span>
            </span>
          </div>
        </div>
      </div>

      {/* KPI row (own full row) + Trend/Health, stacked left; Copilot a
       * tall single panel spanning that whole height on the right. */}
      <div className="overview-hero">
        <div className="overview-hero-left">
          <div className="kpi-row">
            <KpiCard
              label="Revenue"
              value={noSalesYet ? `0.00 ${report.total_sales_currency}` : `${report.total_sales_amount} ${report.total_sales_currency}`}
              compare={
                trend && trend.prior_order_count > 0 && trend.change_pct !== null
                  ? { pct: trend.change_pct, period: `${trend.days}d` }
                  : null
              }
              onClick={() => onNavigate("sales")}
            />
            <KpiCard
              label="Orders"
              value={report.completed_order_count}
              onClick={() => onNavigate("sales")}
            />
            <KpiCard
              label="Cart → checkout rate"
              value={
                !conversion || conversion.cart_to_checkout_rate === null
                  ? "—"
                  : `${conversion.cart_to_checkout_rate}%`
              }
              onClick={() => onNavigate("orders")}
            />
            <KpiCard
              label="Average order value"
              value={
                report.average_order_value
                  ? `${report.average_order_value} ${report.total_sales_currency}`
                  : "—"
              }
              note={
                report.average_order_value &&
                report.priced_order_count < report.completed_order_count
                  ? `Based on ${report.priced_order_count} of ${report.completed_order_count} orders with a recorded amount.`
                  : null
              }
              onClick={() => onNavigate("sales")}
            />
          </div>

          <div className="overview-trend-row">
            <section className="panel">
              <div className="panel-head">
                <span className="eyebrow">Revenue trend</span>
              </div>
              <div className="panel-body">
                <RevenueTrendChart series={series} currency={report.total_sales_currency} />
              </div>
            </section>

            <section className="panel">
              <div className="panel-head">
                <span className="eyebrow">Business health</span>
                <span className={`status-badge ${overallHealthy ? "healthy" : anyAttention ? "attention" : "unavailable"}`}>
                  {overallHealthy ? "Healthy" : anyAttention ? "Attention" : "Mixed"}
                </span>
              </div>
              <div className="panel-body">
                <div className="health-list">
                  {health.map((h) => (
                    <button key={h.key} className="health-list-row" onClick={() => onNavigate(h.section)}>
                      <span className={`health-dot ${h.status}`} />
                      <span className="health-list-name">{h.label}</span>
                      <Badge status={h.status} />
                    </button>
                  ))}
                </div>
              </div>
            </section>
          </div>
        </div>

        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>

      {/* Top products / Products needing attention / What needs attention */}
      <div className="overview-triple-row">
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Top performing products</span>
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
                    <th>Orders</th>
                  </tr>
                </thead>
                <tbody>
                  {products.top_by_revenue.map((p) => (
                    <tr key={p.product_id}>
                      <td>{p.product_name}</td>
                      <td className="num">{p.revenue} {report.total_sales_currency}</td>
                      <td className="num">{p.order_count}</td>
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

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Products needing attention</span>
          </div>
          <div className="panel-body">
            {!catalog?.reachable ? (
              <p className="empty">Platform unreachable for inventory data.</p>
            ) : productsNeedingAttention.length === 0 ? (
              <p className="empty">No stock issues found in the last scan.</p>
            ) : (
              productsNeedingAttention.map((p) => (
                <div key={`${p.title}-${p.issue}`} className="frictionrow">
                  <span>{p.title}</span>
                  <span className={`num ${p.issue === "Out of stock" ? "warn" : ""}`}>{p.issue}</span>
                </div>
              ))
            )}
            <button className="navlink" style={{ marginTop: 12 }} onClick={() => onNavigate("inventory")}>
              Full inventory →
            </button>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">What needs attention?</span>
          </div>
          <div className="panel-body">
            {attention.length === 0 ? (
              <p className="empty">Nothing needs you right now.</p>
            ) : (
              attention.map((a) => (
                <button
                  key={a.label}
                  className="attention-row"
                  onClick={() => onNavigate(a.section)}
                >
                  <span className="attention-label">{a.label}</span>
                  <span className="note" style={{ margin: 0 }}>{a.detail}</span>
                </button>
              ))
            )}
          </div>
        </section>
      </div>

      {/* Summary cards */}
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Commerce health</span>
        </div>
        <div className="panel-body">
          <div className="summary-card-row">
            <SummaryCard
              title="Inventory"
              value={catalog?.reachable ? `${catalog.out_of_stock.length} out of stock` : "Unavailable"}
              onClick={() => onNavigate("inventory")}
            />
            <SummaryCard
              title="Payments"
              value={`${report.friction.find((f) => f.type === "PAYMENT_DECLINED")?.count ?? 0} failures`}
              onClick={() => onNavigate("payments")}
            />
            <SummaryCard
              title="Returns"
              value="Unsupported"
              onClick={() => onNavigate("returns")}
            />
            <SummaryCard
              title="AI & Commerce outcomes"
              value={
                report.supports_payment_recovery
                  ? `${report.shoppers_helped} helped · ${report.recovery_count} recovered`
                  : `${report.shoppers_helped} shoppers helped`
              }
              onClick={() => onNavigate("ai-commerce")}
            />
            <SummaryCard
              title="Holdout"
              value={report.holdout ? `${report.holdout.holdout_cases} in holdout` : "Not running"}
              onClick={() => onNavigate("holdout")}
            />
          </div>
        </div>
      </section>

      {/* Opportunities */}
      {opportunities.length > 0 && (
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Sell better — opportunities</span>
          </div>
          <div className="panel-body">
            <div className="opportunity-grid">
              {opportunities.map((o) => (
                <div key={o.title} className="opportunity-card">
                  <div className="opportunity-title">{o.title}</div>
                  <div className="note" style={{ margin: 0 }}>{o.evidence}</div>
                  <button className="opportunity-card-action" onClick={() => onNavigate(o.section)}>
                    {o.action} →
                  </button>
                </div>
              ))}
            </div>
          </div>
        </section>
      )}

      {/* Platform */}
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Store / Platform</span>
        </div>
        <div className="panel-body">
          <p className="note" style={{ marginTop: 0 }}>
            {caps.platform} — recovery{" "}
            {caps.payment_recovery_methods.length > 0 ? "supported" : "not supported on this platform"}.
          </p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="navlink" onClick={() => onNavigate("platform")}>
              Platform &amp; capabilities →
            </button>
            <button className="navlink" onClick={() => onNavigate("customers")}>
              Shopping insights →
            </button>
            <button className="navlink" onClick={() => onNavigate("insights")}>
              Business insights →
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function KpiCard({
  label,
  value,
  compare,
  onClick,
  note,
}: {
  label: string;
  value: number | string;
  compare?: { pct: string; period: string } | null;
  onClick: () => void;
  note?: string | null;
}) {
  const pctNum = compare ? Number(compare.pct) : 0;
  // A money value's currency code goes on its own smaller line rather than
  // inline - "164666.80" + "INR" fits a compact card; "164666.80 INR" as
  // one string does not, without shrinking the number itself unreadably.
  const str = String(value);
  const moneyMatch = str.match(/^(-?[\d.,]+)\s+([A-Z]{3})$/);
  return (
    <button className="kpi-card" style={{ cursor: "pointer", textAlign: "left", font: "inherit", color: "inherit" }} onClick={onClick}>
      <div className="eyebrow">{label}</div>
      {moneyMatch ? (
        <div className="kpi-value">
          {moneyMatch[1]}
          <span className="kpi-value-unit">{moneyMatch[2]}</span>
        </div>
      ) : (
        <div className="kpi-value">{value}</div>
      )}
      {compare && (
        <div className={`kpi-compare ${pctNum > 0 ? "up" : pctNum < 0 ? "down" : "flat"}`}>
          {pctNum > 0 ? "+" : ""}
          {compare.pct}% vs. prior {compare.period}
        </div>
      )}
      {note && (
        <p className="note" style={{ margin: "4px 0 0", fontSize: 11 }}>
          {note}
        </p>
      )}
    </button>
  );
}

function SummaryCard({ title, value, onClick }: { title: string; value: string; onClick: () => void }) {
  return (
    <button className="summary-card" style={{ cursor: "pointer", textAlign: "left", font: "inherit", color: "inherit" }} onClick={onClick}>
      <div className="summary-card-title">{title}</div>
      <div style={{ fontWeight: 600, fontSize: 14 }}>{value}</div>
    </button>
  );
}
