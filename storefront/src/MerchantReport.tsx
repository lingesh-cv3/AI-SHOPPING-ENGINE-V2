import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report, type SalesTrend } from "./api";

/**
 * Sales & Revenue.
 *
 * Scoped deliberately narrow now that this console has dedicated sections
 * for the things that used to live here too: product-level detail moved to
 * Product Performance, the holdout comparison to Holdout / Experiment,
 * recent case activity to AI Commerce, and search/friction detail to
 * Customer & Shopping Insights. What stays here is the money: total sales,
 * completed orders, AOV, recovered revenue, and the period-over-period
 * trend - all sourced from the same `ExecutionAttempt` ledger, so none of
 * these figures can disagree with each other.
 */
export function MerchantReport() {
  const [report, setReport] = useState<Report | null>(null);
  const [trend, setTrend] = useState<SalesTrend | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([console_api.report(), console_api.salesTrend(7).catch(() => null)])
      .then(([r, t]) => {
        setReport(r);
        setTrend(t);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Could not load your figures."),
      );
  }, []);

  // Every branch renders something. Returning null on failure made the panel vanish
  // with no console error, which is the hardest kind of bug to find: nothing looks
  // wrong, there is just less of the page than there should be.
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

  // Total sales is a straight count of completed orders, independent of whether
  // any of them ever hit friction - a shop with zero cases opened can still have
  // real completed sales, so this must never be hidden behind any assistant-activity
  // branch.
  const noSalesYet = report.completed_order_count === 0;

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Sales &amp; revenue</span>
        <span className="eyebrow">last {report.days} days</span>
      </div>

      <div className="panel-body">
        <div className="headline-figure">
          <div className="eyebrow">Total sales</div>
          <div className="bignum num">
            {noSalesYet
              ? `0.00 ${report.total_sales_currency}`
              : `${report.total_sales_amount} ${report.total_sales_currency}`}
          </div>
          <p className="note" style={{ margin: "4px 0 0" }}>
            {noSalesYet
              ? "No completed orders in this window yet."
              : `${report.completed_order_count} completed order${
                  report.completed_order_count === 1 ? "" : "s"
                }${
                  report.average_order_value
                    ? `, averaging ${report.average_order_value} ${report.total_sales_currency}`
                    : ""
                }.`}
          </p>
        </div>

        {trend && (trend.recent_order_count > 0 || trend.prior_order_count > 0) && (
          <div className="headline-figure" style={{ marginTop: 14 }}>
            <div className="eyebrow">
              Last {trend.days} days vs. the {trend.days} before
            </div>
            <div className="bignum num">
              {trend.recent_total} {report.total_sales_currency}
              {trend.change_pct !== null && (
                <span
                  className={
                    Number(trend.change_amount) >= 0 ? "trend up" : "trend down"
                  }
                  style={{ marginLeft: 10, fontSize: "0.5em" }}
                >
                  {Number(trend.change_amount) >= 0 ? "+" : ""}
                  {trend.change_pct}%
                </span>
              )}
            </div>
            <p className="note" style={{ margin: "4px 0 0" }}>
              {trend.prior_order_count > 0
                ? `versus ${trend.prior_total} ${report.total_sales_currency} in the prior period`
                : "no completed sales in the prior period to compare against"}
            </p>
          </div>
        )}

        {report.supports_payment_recovery && (
          <div className="headline-figure" style={{ marginTop: 14 }}>
            <div className="eyebrow">Sales recovered</div>
            <div className="bignum num">
              {report.revenue_recovered} {report.currency}
            </div>
            <p className="note" style={{ margin: "4px 0 0" }}>
              Money that would otherwise have been lost to a failed payment -
              {" "}{report.recovery_count} of {report.recovery_opportunities}{" "}
              recovery opportunities completed. Full detail in AI &amp;
              Outcomes &gt; Recovery.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
