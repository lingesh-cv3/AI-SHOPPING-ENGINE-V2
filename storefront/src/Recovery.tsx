import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/**
 * Recovery, framed as an AI-outcomes story rather than a queue to act on.
 *
 * The action itself - approve/reject a pending recovery - lives in exactly
 * one place, Commerce > Payments & Checkout, through the real risk-gated
 * route. Duplicating Approve/Reject buttons here would be the same data
 * shown twice with two chances to disagree; this view exists to answer "is
 * the recovery mechanism working", not to be a second place to act.
 */
export function Recovery({ onNavigate }: { onNavigate: (section: string) => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .report()
      .then((r) => {
        setReport(r);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load recovery data."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Recovery</h2>
        <p className="overview-subtitle">
          Is the recovery mechanism actually converting declines into revenue?
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

  if (!report.supports_payment_recovery) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              Payment recovery is not supported on this platform. The adapter
              does not declare a recovery method, so no recovery cases can
              exist here - a declined payment is handed to a person as an
              escalation instead.
            </p>
          </div>
        </section>
        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>
    );
  }

  const pending = report.recovery_opportunities - report.recovery_count;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Recovery opportunities</div>
          <div className="kpi-value">{report.recovery_opportunities}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Recovered</div>
          <div className="kpi-value">{report.recovery_count}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Still pending</div>
          <div className="kpi-value" style={pending > 0 ? { color: "var(--friction)" } : undefined}>
            {pending}
          </div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Revenue recovered</div>
          <div className="kpi-value">
            {report.revenue_recovered}
            <span className="kpi-value-unit">{report.currency}</span>
          </div>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Recovery</span>
          <span className="eyebrow">last {report.days} days</span>
        </div>
        <div className="panel-body">
          <p className="note" style={{ marginTop: 0 }}>
            Money actually captured from a declined payment, never a
            projection - only counted once the payment genuinely went
            through.
          </p>
          <button
            className="navlink"
            style={{ marginTop: 16 }}
            onClick={() => onNavigate("payments")}
          >
            Go to Payments &amp; Checkout to approve pending recoveries →
          </button>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
