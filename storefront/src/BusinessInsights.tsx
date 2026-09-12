import { useEffect, useState } from "react";
import {
  console_api,
  type CatalogAlerts,
  type Conversion,
  type MerchantReport as Report,
  type ProductPerformance as Performance,
  type UnmetDemand,
} from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

interface Insight {
  text: string;
  evidence: string;
}

/**
 * Business Insights: a synthesis layer over data already shown elsewhere in
 * this console, not a second dashboard of the same numbers.
 *
 * Deliberately rule-based rather than a model call: every insight below is
 * a deterministic threshold over a number this console already computed
 * (report/catalog/conversion/product-performance), each shown with the
 * exact figure behind it. No insight is generated when there isn't enough
 * evidence for it - an empty list here is the honest answer for a quiet
 * store, not a reason to invent generic advice.
 */
export function BusinessInsights({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [insights, setInsights] = useState<Insight[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      console_api.report(),
      console_api.catalogAlerts().catch(() => null),
      console_api.conversion().catch(() => null),
      console_api.productPerformance(30, 10).catch(() => null),
      console_api.unmetDemand(30, 5).catch(() => null),
    ])
      .then(([report, catalog, conversion, products, demand]) => {
        setInsights(buildInsights(report, catalog, conversion, products, demand));
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not build insights."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Business Insights</h2>
        <p className="overview-subtitle">
          Deterministic, evidence-backed observations - no insight without a real figure behind it.
        </p>
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
  if (!insights) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card" style={{ gridColumn: "span 4" }}>
          <div className="eyebrow">Insights surfaced</div>
          <div className="kpi-value">{insights.length}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>each backed by a real figure below</p>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Business insights</span>
        </div>
        <div className="panel-body">
          {insights.length === 0 ? (
            <p className="empty">
              Nothing stands out in this window - no insight is shown unless
              there is a real figure behind it.
            </p>
          ) : (
            insights.map((i) => (
              <div key={i.text} className="frictionrow" style={{ alignItems: "flex-start" }}>
                <span>{i.text}</span>
                <span className="note" style={{ margin: 0, textAlign: "right" }}>
                  {i.evidence}
                </span>
              </div>
            ))
          )}
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}

function buildInsights(
  report: Report,
  catalog: CatalogAlerts | null,
  conversion: Conversion | null,
  products: Performance | null,
  demand: UnmetDemand | null,
): Insight[] {
  const insights: Insight[] = [];

  if (report.supports_payment_recovery && report.recovery_opportunities > 0) {
    const pending = report.recovery_opportunities - report.recovery_count;
    if (pending > 0) {
      insights.push({
        text: `Payment declines are creating recoverable opportunities worth pursuing.`,
        evidence: `${report.recovery_count} of ${report.recovery_opportunities} recovery attempts completed so far`,
      });
    }
  }

  if (catalog?.reachable && catalog.complete && catalog.out_of_stock.length > 0) {
    insights.push({
      text: "Out-of-stock products may be constraining demand you'd otherwise capture.",
      evidence: `${catalog.out_of_stock.length} of ${catalog.scanned} products out of stock`,
    });
  }
  if (catalog?.reachable && !catalog.complete) {
    insights.push({
      text: "Inventory scan did not finish - out-of-stock/low-stock figures elsewhere in this console are a partial view.",
      evidence: `scanned ${catalog.scanned} products before stopping`,
    });
  }

  if (conversion && conversion.checkout_attempts >= 5 && conversion.checkout_success_rate !== null) {
    if (conversion.checkout_success_rate < 70) {
      insights.push({
        text: "A notable share of checkout attempts are failing rather than completing.",
        evidence: `${conversion.checkout_success_rate}% success rate over ${conversion.checkout_attempts} attempts`,
      });
    }
  }

  if (products?.has_data && products.top_by_revenue.length > 0 && products.top_by_quantity.length > 0) {
    const topByRevenue = products.top_by_revenue[0];
    const topByQuantity = products.top_by_quantity[0];
    if (topByRevenue.product_id !== topByQuantity.product_id) {
      insights.push({
        text: `Your best seller by volume isn't your top earner - "${topByQuantity.product_name}" sells the most units, but "${topByRevenue.product_name}" brings in the most revenue.`,
        evidence: `${topByQuantity.quantity} units vs ${topByRevenue.revenue} ${report.total_sales_currency}`,
      });
    }
  }

  if (demand && demand.queries.length > 0) {
    const top = demand.queries[0];
    if (top.times_asked >= 2) {
      insights.push({
        text: `Shoppers keep searching for "${top.query}" and finding nothing - a real gap in what you stock.`,
        evidence: `asked ${top.times_asked} time${top.times_asked === 1 ? "" : "s"} in the last ${demand.days} days`,
      });
    }
  }

  if (report.waiting_for_you > 0) {
    insights.push({
      text: "Cases are waiting on a human decision right now.",
      evidence: `${report.waiting_for_you} waiting on you`,
    });
  }

  return insights;
}
