import { useEffect, useState } from "react";
import {
  console_api,
  type Capabilities,
  type CatalogAlerts,
  type Conversion,
  type MerchantReport as Report,
  type ProductPerformance as Performance,
  type SalesTrend,
  type UnmetDemand,
} from "./api";

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

/**
 * The Merchant tab's primary dashboard.
 *
 * Every figure here is read from the same routes the dedicated sections
 * use (`/api/report`, `/api/catalog`, `/api/conversion`, `/api/products`,
 * `/api/sales-trend`, `/api/unmet-demand`, `/api/approvals`), so this page
 * cannot show a number a section it links to would contradict - the
 * business-health statuses below are computed client-side from those same
 * fetched objects, never from a separate calculation that could disagree.
 */
export function Overview({ onNavigate }: { onNavigate: (section: string) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [catalog, setCatalog] = useState<CatalogAlerts | null>(null);
  const [conversion, setConversion] = useState<Conversion | null>(null);
  const [trend, setTrend] = useState<SalesTrend | null>(null);
  const [products, setProducts] = useState<Performance | null>(null);
  const [demand, setDemand] = useState<UnmetDemand | null>(null);
  const [pendingRecovery, setPendingRecovery] = useState<number>(0);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      console_api.report(),
      console_api.catalogAlerts().catch(() => null),
      console_api.conversion().catch(() => null),
      console_api.salesTrend(7).catch(() => null),
      console_api.productPerformance(30, 5).catch(() => null),
      console_api.unmetDemand(30, 5).catch(() => null),
      console_api.queue().catch(() => null),
      console_api.capabilities().catch(() => null),
    ])
      .then(([r, c, conv, t, p, d, q, cp]) => {
        setReport(r);
        setCatalog(c);
        setConversion(conv);
        setTrend(t);
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
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load your overview."));
  }, []);

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
  if (catalog?.reachable && catalog.out_of_stock.length > 0) {
    attention.push({
      label: `${catalog.out_of_stock.length} product${catalog.out_of_stock.length === 1 ? "" : "s"} out of stock`,
      detail: catalog.complete
        ? "Across your full catalog."
        : `Across the ${catalog.scanned} products scanned so far - the scan did not finish.`,
      section: "inventory",
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

  const opportunities: { title: string; evidence: string; section: string }[] = [];
  if (report.supports_payment_recovery) {
    const pending = report.recovery_opportunities - report.recovery_count;
    if (pending > 0) {
      opportunities.push({
        title: "Recoverable revenue is sitting in the queue",
        evidence: `${pending} of ${report.recovery_opportunities} recovery opportunities not yet completed`,
        section: "recovery",
      });
    }
  }
  if (products?.has_data && products.top_by_revenue.length > 0 && products.top_by_quantity.length > 0) {
    const topByRevenue = products.top_by_revenue[0];
    const topByQuantity = products.top_by_quantity[0];
    if (topByRevenue.product_id !== topByQuantity.product_id) {
      opportunities.push({
        title: `"${topByRevenue.product_name}" earns more per sale than your best seller by volume`,
        evidence: `${topByRevenue.revenue} ${report.total_sales_currency} vs. ${topByQuantity.quantity} units for "${topByQuantity.product_name}"`,
        section: "products",
      });
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div className="panel-head" style={{ border: 0, padding: 0 }}>
        <div>
          <span className="eyebrow">{caps.platform}</span>
          <h2 style={{ margin: "4px 0 0", fontSize: "var(--step-3)" }}>Overview</h2>
        </div>
        <span className="eyebrow">last {report.days} days</span>
      </div>

      {/* KPI row */}
      <div className="kpi-row">
        <KpiCard
          label="Total sales"
          value={noSalesYet ? `0.00 ${report.total_sales_currency}` : `${report.total_sales_amount} ${report.total_sales_currency}`}
          compare={
            trend && trend.prior_order_count > 0 && trend.change_pct !== null
              ? { pct: trend.change_pct, period: `${trend.days}d` }
              : null
          }
          onClick={() => onNavigate("sales")}
        />
        <KpiCard
          label="Completed orders"
          value={report.completed_order_count}
          onClick={() => onNavigate("sales")}
        />
        <KpiCard
          label="Checkout success rate"
          value={
            !conversion || conversion.checkout_success_rate === null
              ? "—"
              : `${conversion.checkout_success_rate}%`
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
          onClick={() => onNavigate("sales")}
        />
      </div>

      {/* Business health */}
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Business health</span>
        </div>
        <div className="panel-body">
          <div className="health-grid">
            {health.map((h) => (
              <button
                key={h.key}
                className="health-card"
                onClick={() => onNavigate(h.section)}
              >
                <div className="health-name">{h.label}</div>
                <Badge status={h.status} />
                <div className="note" style={{ margin: 0 }}>{h.detail}</div>
              </button>
            ))}
          </div>
        </div>
      </section>

      {/* Top products + Needs attention, side by side on wide screens */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Top products</span>
          </div>
          <div className="panel-body">
            {!products?.has_data ? (
              <p className="empty">No completed orders with product detail yet.</p>
            ) : (
              products.top_by_quantity.map((p) => (
                <div key={p.product_id} className="frictionrow">
                  <span>{p.product_name}</span>
                  <span className="num">
                    {p.quantity} sold · {p.revenue} {report.total_sales_currency}
                  </span>
                </div>
              ))
            )}
            <button className="navlink" style={{ marginTop: 12 }} onClick={() => onNavigate("products")}>
              Full product performance →
            </button>
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Needs your attention</span>
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
          <span className="eyebrow">Summary</span>
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
              title="AI outcomes"
              value={`${report.shoppers_helped} shoppers helped`}
              onClick={() => onNavigate("ai-commerce")}
            />
            <SummaryCard
              title="Recovery"
              value={report.supports_payment_recovery ? `${report.recovery_count} recovered` : "Unsupported"}
              onClick={() => onNavigate("recovery")}
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
            <span className="eyebrow">Business opportunities</span>
          </div>
          <div className="panel-body">
            {opportunities.map((o) => (
              <button
                key={o.title}
                className="opportunity-card"
                style={{ width: "100%", textAlign: "left", border: "none", borderLeft: "3px solid var(--accent)", cursor: "pointer" }}
                onClick={() => onNavigate(o.section)}
              >
                <div className="opportunity-title">{o.title}</div>
                <div className="note" style={{ margin: 0 }}>{o.evidence}</div>
              </button>
            ))}
          </div>
        </section>
      )}

      {/* Platform + Copilot entry points */}
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Store</span>
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
            <button className="navlink" onClick={() => onNavigate("copilot")}>
              Ask the Copilot →
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
}: {
  label: string;
  value: number | string;
  compare?: { pct: string; period: string } | null;
  onClick: () => void;
}) {
  const pctNum = compare ? Number(compare.pct) : 0;
  return (
    <button className="kpi-card" style={{ cursor: "pointer", textAlign: "left", font: "inherit", color: "inherit" }} onClick={onClick}>
      <div className="eyebrow">{label}</div>
      <div className="kpi-value">{value}</div>
      {compare && (
        <div className={`kpi-compare ${pctNum > 0 ? "up" : pctNum < 0 ? "down" : "flat"}`}>
          {pctNum > 0 ? "+" : ""}
          {compare.pct}% vs. prior {compare.period}
        </div>
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
