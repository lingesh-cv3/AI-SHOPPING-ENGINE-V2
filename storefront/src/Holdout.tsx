import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate)}%`;
}

/**
 * Holdout / Experiment.
 *
 * The one comparison in this console that can defensibly claim the
 * assistant caused a difference, rather than merely observed one - because
 * a slice of sessions genuinely get no help, assigned once per session and
 * never revisited (`db.holdout_status` / `resolve_unresolved_payment_cases_for_cart`).
 * This view does not touch that mechanism - it only reads
 * `merchant_report()`'s existing `holdout` block.
 *
 * A revenue-per-group comparison is deliberately not shown: the backend
 * does not currently compute recovered revenue split by holdout/assisted
 * group, only resolution-rate. Showing a revenue number here would mean
 * inventing a comparison nothing computes.
 */
export function Holdout({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .report()
      .then((r) => {
        setReport(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load holdout data."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Holdout / Experiment</h2>
        <p className="overview-subtitle">
          The difference the assistant actually made, against a genuinely unassisted control group.
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
  if (!report) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  if (!report.holdout) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              No holdout sessions in this window. Turn on a holdout percentage
              in Store &gt; Settings to start comparing assisted outcomes
              against a genuinely unassisted control group.
            </p>
          </div>
        </section>
        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>
    );
  }

  const h = report.holdout;
  const smallSample = h.holdout_cases < 20 || h.assisted_cases < 20;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Resolved, assisted</div>
          <div className="kpi-value">{pct(h.assisted_resolution_rate)}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>
            {h.assisted_resolved} of {h.assisted_cases}
          </p>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Resolved, holdout</div>
          <div className="kpi-value">{pct(h.holdout_resolution_rate)}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>
            {h.holdout_resolved} of {h.holdout_cases}, no assistance given
          </p>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Holdout cases</div>
          <div className="kpi-value">{h.holdout_cases}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Assisted cases</div>
          <div className="kpi-value">{h.assisted_cases}</div>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Holdout / experiment</span>
          <span className="eyebrow">last {report.days} days</span>
        </div>
        <div className="panel-body">
          <p className="note" style={{ marginTop: 0 }}>
            A slice of your shoppers get no help at all, so this comparison is
            the difference the assistant actually made - not a number that
            includes sales that would have happened anyway.
          </p>
          <p className="note">
            {smallSample
              ? "This sample is small enough that the difference above should be treated as directional, not proof - a few more or fewer resolved cases would move it noticeably."
              : "Large enough to treat the difference above as a real signal, though this is still one merchant's data, not a controlled study."}
          </p>
          <p className="note">
            Revenue recovered is not split by holdout/assisted group - only
            resolution rate is compared here, because that is the only
            per-group figure this engine actually computes.
          </p>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
