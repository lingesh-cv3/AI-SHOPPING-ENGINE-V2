import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report, type UnmetDemand } from "./api";

/**
 * Customer & Shopping Insights.
 *
 * Deliberately narrow. This schema has no customer-level model beyond a
 * shopper's account and their cases - no repeat-purchase index, no
 * lifetime-value figure, no segment or cohort concept. Rather than invent
 * one, this surface shows only what is genuinely backed: what shoppers
 * searched for and did not find (`db.unmet_demand`, built from real
 * DEAD_SEARCH cases), and the same shopping-friction breakdown the report
 * already aggregates. Deeper customer analytics would need new
 * instrumentation this engine does not have yet - said here rather than
 * faked.
 */
export function CustomerInsights() {
  const [demand, setDemand] = useState<UnmetDemand | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([console_api.unmetDemand(30, 10), console_api.report()])
      .then(([d, r]) => {
        setDemand(d);
        setReport(r);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load shopping insights."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!demand || !report) return <p className="empty">Loading...</p>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">What shoppers searched for and didn't find</span>
          <span className="eyebrow">last {demand.days} days</span>
        </div>
        <div className="panel-body">
          {demand.queries.length === 0 ? (
            <p className="empty">No repeated dead searches in this window.</p>
          ) : (
            demand.queries.map((q) => (
              <div key={q.query} className="frictionrow">
                <span>{q.query}</span>
                <span className="num">
                  asked {q.times_asked} time{q.times_asked === 1 ? "" : "s"}
                </span>
              </div>
            ))
          )}
          <p className="note" style={{ marginTop: 14 }}>
            A real, grounded signal for "should I stock this" - built only
            from searches that genuinely came back empty, never guessed.
          </p>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Shopping friction</span>
        </div>
        <div className="panel-body">
          {report.friction.length === 0 ? (
            <p className="empty">No friction recorded in this window.</p>
          ) : (
            report.friction.map((f) => (
              <div key={f.type} className="frictionrow">
                <span>{f.type.replace(/_/g, " ").toLowerCase()}</span>
                <span className="num">{f.count}</span>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">What this can't tell you yet</span>
        </div>
        <div className="panel-body">
          <p className="note" style={{ marginTop: 0 }}>
            Repeat-purchase behavior, customer lifetime value, and segments
            or cohorts are not shown here because no schema exists yet to
            support them honestly - this engine currently tracks a
            shopper's account, their cart, and their cases, not an
            order-history index per customer. Building those would need new
            instrumentation, not a new query over existing data.
          </p>
        </div>
      </section>
    </div>
  );
}
