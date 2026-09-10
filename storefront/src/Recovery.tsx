import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report } from "./api";

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
      .then(setReport)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load recovery data."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!report) return <p className="empty">Loading...</p>;

  if (!report.supports_payment_recovery) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Recovery</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            Payment recovery is not supported on this platform. The adapter
            does not declare a recovery method, so no recovery cases can
            exist here - a declined payment is handed to a person as an
            escalation instead.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Recovery</span>
        <span className="eyebrow">last {report.days} days</span>
      </div>
      <div className="panel-body">
        <div className="figures">
          <Figure value={report.recovery_opportunities} label="Recovery opportunities" />
          <Figure value={report.recovery_count} label="Recovered" />
        </div>
        <div className="headline-figure" style={{ marginTop: 14 }}>
          <div className="eyebrow">Revenue recovered</div>
          <div className="bignum num">
            {report.revenue_recovered} {report.currency}
          </div>
          <p className="note" style={{ margin: "4px 0 0" }}>
            Money actually captured from a declined payment, never a
            projection.
          </p>
        </div>
        <button
          className="navlink"
          style={{ marginTop: 16 }}
          onClick={() => onNavigate("payments")}
        >
          Go to Payments &amp; Checkout to approve pending recoveries →
        </button>
      </div>
    </section>
  );
}

function Figure({ value, label }: { value: number; label: string }) {
  return (
    <div>
      <div className="figure num">{value}</div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}
