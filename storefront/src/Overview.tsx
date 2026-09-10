import { useEffect, useState } from "react";
import { console_api, type Capabilities, type MerchantReport as Report } from "./api";
import type { CatalogAlerts, Conversion } from "./api";

/**
 * The Merchant tab's front page.
 *
 * Answers "how is my store doing, what changed, what needs attention" in one
 * glance, then points at the section that has the detail - it does not try
 * to be the detail itself. Every figure here is read from the same routes
 * the dedicated sections use (`/api/report`, `/api/catalog`,
 * `/api/conversion`), so this page can never show a number the section it
 * links to would contradict.
 */
export function Overview({ onNavigate }: { onNavigate: (section: string) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [catalog, setCatalog] = useState<CatalogAlerts | null>(null);
  const [conversion, setConversion] = useState<Conversion | null>(null);
  const [pendingRecovery, setPendingRecovery] = useState<number | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      console_api.report(),
      console_api.catalogAlerts().catch(() => null),
      console_api.conversion().catch(() => null),
      console_api.queue().catch(() => null),
      console_api.capabilities().catch(() => null),
    ])
      .then(([r, c, conv, q, caps]) => {
        setReport(r);
        setCatalog(c);
        setConversion(conv);
        setCaps(caps);
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
  if (!report) return <p className="empty">Loading...</p>;

  const attention: { label: string; detail: string; section: string }[] = [];

  if (report.waiting_for_you > 0) {
    attention.push({
      label: `${report.waiting_for_you} case${report.waiting_for_you === 1 ? "" : "s"} waiting on you`,
      detail: "A person needs to decide something before the shopper can move on.",
      section: "ai-commerce",
    });
  }
  if (pendingRecovery !== null && pendingRecovery > 0) {
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

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Overview</span>
          <span className="eyebrow">last {report.days} days</span>
        </div>
        <div className="panel-body">
          <div className="figures">
            <OverviewFigure
              value={
                report.completed_order_count === 0
                  ? `0.00 ${report.total_sales_currency}`
                  : `${report.total_sales_amount} ${report.total_sales_currency}`
              }
              label="Total sales"
              onClick={() => onNavigate("sales")}
            />
            <OverviewFigure
              value={report.completed_order_count}
              label="Completed orders"
              onClick={() => onNavigate("orders")}
            />
            <OverviewFigure
              value={report.shoppers_helped}
              label="Shoppers helped"
              onClick={() => onNavigate("ai-commerce")}
            />
            {conversion && conversion.checkout_attempts > 0 && (
              <OverviewFigure
                value={
                  conversion.checkout_success_rate === null
                    ? "—"
                    : `${conversion.checkout_success_rate}%`
                }
                label="Checkout success rate"
                onClick={() => onNavigate("orders")}
              />
            )}
          </div>
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
                <span className="note" style={{ margin: 0 }}>
                  {a.detail}
                </span>
              </button>
            ))
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Store</span>
        </div>
        <div className="panel-body">
          <p className="note" style={{ marginTop: 0 }}>
            {caps ? caps.platform : "..."} — recovery{" "}
            {caps && caps.payment_recovery_methods.length > 0
              ? "supported"
              : "not supported on this platform"}
            .
          </p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button className="navlink" onClick={() => onNavigate("products")}>
              Product performance →
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

function OverviewFigure({
  value,
  label,
  onClick,
}: {
  value: number | string;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className="figure-btn" onClick={onClick}>
      <div className="figure num">{value}</div>
      <div className="eyebrow">{label}</div>
    </button>
  );
}
