import { useEffect, useState } from "react";
import { console_api, type CaseRow, type MerchantReport as Report } from "./api";

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
export function AICommerce() {
  const [report, setReport] = useState<Report | null>(null);
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([console_api.report(), console_api.cases(20)])
      .then(([r, c]) => {
        setReport(r);
        setCases(c.cases);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load AI commerce data."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!report || !cases) return <p className="empty">Loading...</p>;

  if (report.shoppers_helped === 0) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">AI commerce</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            Nothing yet. As shoppers run into problems, what the assistant
            did about them shows up here - these are observed outcomes, not
            a projection.
          </p>
        </div>
      </section>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">AI-assisted outcomes</span>
          <span className="eyebrow">last {report.days} days · observed, not causal</span>
        </div>
        <div className="panel-body">
          <div className="figures">
            <Figure value={report.shoppers_helped} label="Shoppers helped" />
            <Figure value={pct(report.resolution_rate)} label="Resolved" />
            <Figure value={report.problems_solved} label="Problems solved" />
            <Figure value={report.handled_without_you} label="Without your time" />
            <Figure
              value={report.waiting_for_you}
              label="Waiting on you"
              warn={report.waiting_for_you > 0}
            />
          </div>
          <p className="note" style={{ marginTop: 14 }}>
            These count what happened, not what would have happened without
            the assistant - for that comparison, see Holdout / Experiment.
          </p>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Recent activity</span>
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
        </div>
      </section>
    </div>
  );
}

function Figure({ value, label, warn }: { value: number | string; label: string; warn?: boolean }) {
  return (
    <div>
      <div className={warn && Number(value) > 0 ? "figure num warn" : "figure num"}>{value}</div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}
