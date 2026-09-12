import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report, type UnmetDemand } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

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
export function CustomerInsights({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [demand, setDemand] = useState<UnmetDemand | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([console_api.unmetDemand(30, 10), console_api.report()])
      .then(([d, r]) => {
        setDemand(d);
        setReport(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load shopping insights."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Customer &amp; Shopping Insights</h2>
        <p className="overview-subtitle">
          What shoppers wanted and couldn&rsquo;t find, and where they hit friction.
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
  if (!demand || !report) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  const totalFriction = report.friction.reduce((sum, f) => sum + f.count, 0);
  const topDeadSearch = demand.queries[0];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Dead searches</div>
          <div className="kpi-value">{demand.queries.length}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>distinct queries, last {demand.days} days</p>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Most-asked-for miss</div>
          <div className="kpi-value" style={{ fontSize: 16 }}>
            {topDeadSearch ? topDeadSearch.query : "—"}
          </div>
          {topDeadSearch && (
            <p className="note" style={{ margin: "4px 0 0" }}>
              asked {topDeadSearch.times_asked} time{topDeadSearch.times_asked === 1 ? "" : "s"}
            </p>
          )}
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Friction events</div>
          <div className="kpi-value">{totalFriction}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>last {report.days} days</p>
        </div>
      </div>

      <div className="overview-trend-row">
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">What shoppers searched for and didn&rsquo;t find</span>
            <span className="eyebrow">last {demand.days} days</span>
          </div>
          <div className="panel-body">
            {demand.queries.length === 0 ? (
              <p className="empty">No repeated dead searches in this window.</p>
            ) : (
              <table className="mini-table">
                <thead>
                  <tr>
                    <th>Query</th>
                    <th>Asked</th>
                  </tr>
                </thead>
                <tbody>
                  {demand.queries.map((q) => (
                    <tr key={q.query}>
                      <td>{q.query}</td>
                      <td className="num">{q.times_asked}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">What this can&rsquo;t tell you yet</span>
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

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
