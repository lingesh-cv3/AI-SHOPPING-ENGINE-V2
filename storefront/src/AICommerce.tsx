import { useEffect, useState } from "react";
import { console_api, type CaseRow, type MerchantReport as Report } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/** A percentage figure. Null (no problems opened yet) must never render as
 *  "0%" - that would claim a 0% resolution rate where there is really no
 *  rate to report at all. */
function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate)}%`;
}

function when(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + "h ago";
  return Math.round(hours / 24) + "d ago";
}

/**
 * AI Commerce: what the engine actually did for shoppers this window, and
 * the recent activity that produced it. These are observed outcomes, not a
 * claim of causal lift - see the Holdout / Experiment section for the one
 * comparison in this product that can defensibly claim that.
 */
export function AICommerce({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([console_api.report(), console_api.cases(20)])
      .then(([r, c]) => {
        setReport(r);
        setCases(c.cases);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load AI commerce data."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>AI Commerce</h2>
        <p className="overview-subtitle">
          What the assistant actually did for shoppers this window - observed, not projected.
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
  if (!report || !cases) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  if (report.shoppers_helped === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              Nothing yet. As shoppers run into problems, what the assistant
              did about them shows up here - these are observed outcomes, not
              a projection.
            </p>
          </div>
        </section>
        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Shoppers helped</div>
          <div className="kpi-value">{report.shoppers_helped}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Resolved</div>
          <div className="kpi-value">{pct(report.resolution_rate)}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Without your time</div>
          <div className="kpi-value">{report.handled_without_you}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Waiting on you</div>
          <div className="kpi-value" style={report.waiting_for_you > 0 ? { color: "var(--friction)" } : undefined}>
            {report.waiting_for_you}
          </div>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Recent activity</span>
          <span className="eyebrow">last {report.days} days · observed, not causal</span>
        </div>
        <div className="panel-body">
          {cases.length === 0 ? (
            <p className="empty">No cases in this window.</p>
          ) : (
            cases.map((c) => (
              <div key={c.case_id} className="recentrow">
                <div className="recent-head">
                  <span className="eyebrow">
                    {c.friction_type?.replace(/_/g, " ") ?? "question"}
                  </span>
                  <span className="eyebrow">{when(c.created_at)}</span>
                </div>
                {c.diagnosis && <p className="recent-diag">{c.diagnosis}</p>}
                <p className="note" style={{ margin: "2px 0 0" }}>
                  {c.state}
                  {c.risk_outcome ? ` · ${c.risk_outcome.toLowerCase()}` : ""}
                  {c.selected_action
                    ? ` · ${c.selected_action.replace(/_/g, " ").toLowerCase()}`
                    : ""}
                </p>
              </div>
            ))
          )}
          <p className="note" style={{ marginTop: 14 }}>
            These count what happened, not what would have happened without
            the assistant - for that comparison, see Holdout / Experiment.
          </p>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
