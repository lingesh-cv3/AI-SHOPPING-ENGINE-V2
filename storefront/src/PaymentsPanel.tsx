import { useCallback, useEffect, useState } from "react";
import {
  console_api,
  type Capabilities,
  type MerchantReport as Report,
} from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";
import { useApprovalTotal } from "./merchantApprovalsStore";

export function PaymentsPanel({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  // Approvals are decided in one place - the Approvals section - and counted in
  // one place. This panel used to carry its own approve/reject buttons over a
  // payment-only slice of the queue, which left every other kind of approval with
  // no screen at all on a platform like Northfield, and gave this page a count
  // that disagreed with the rest of the console.
  const waiting = useApprovalTotal();
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [r, c] = await Promise.all([
        console_api.report(),
        console_api.capabilities(),
      ]);
      setReport(r);
      setCaps(c);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load payments data.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Payments &amp; Checkout</h2>
        <p className="overview-subtitle">
          Completed orders, declines, and any recovery actions waiting on you.
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

  if (!report || !caps) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  const recoverySupported = report.supports_payment_recovery;

  const declines =
    report.friction.find((f) => f.type === "PAYMENT_DECLINED")?.count ?? 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Completed orders</div>
          <div className="kpi-value">{report.completed_order_count}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>
            {report.total_sales_amount} {report.total_sales_currency} total
          </p>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Payment failures</div>
          <div className="kpi-value" style={declines > 0 ? { color: "var(--friction)" } : undefined}>
            {declines}
          </div>
        </div>
        {recoverySupported ? (
          <>
            <div className="kpi-card">
              <div className="eyebrow">Decisions waiting</div>
              <div className="kpi-value" style={waiting ? { color: "var(--friction)" } : undefined}>
                {waiting ?? "—"}
              </div>
              <p className="note" style={{ margin: "4px 0 0" }}>
                all types, in Approvals
              </p>
            </div>
            <div className="kpi-card">
              <div className="eyebrow">Revenue recovered</div>
              <div className="kpi-value">
                {report.revenue_recovered}
                <span className="kpi-value-unit">{report.currency}</span>
              </div>
            </div>
          </>
        ) : (
          <div className="kpi-card" style={{ gridColumn: "span 2" }}>
            <div className="eyebrow">Payment recovery</div>
            <div className="kpi-value" style={{ fontSize: 16 }}>Not supported</div>
            <p className="note" style={{ margin: "4px 0 0" }}>this platform&rsquo;s adapter</p>
          </div>
        )}
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Payments &amp; checkout</span>
          <span className="eyebrow">last {report.days} days</span>
        </div>
        <div className="panel-body">
          {!recoverySupported ? (
            <p className="empty">
              Payment recovery is not supported on this platform. The
              adapter does not declare a recovery method, so no recovery cases
              can exist here. A declined payment on this platform is handed to
              a person as an escalation instead.
            </p>
          ) : (
            <>
              <p className="note" style={{ marginTop: 0 }}>
                Recovery opportunities from declined payments are held for your
                approval, alongside every other decision the engine is waiting on.
              </p>
              <button
                type="button"
                className="add"
                onClick={() => onNavigate("approvals")}
              >
                {waiting === null
                  ? "Open Approvals"
                  : waiting === 0
                    ? "Open Approvals - nothing waiting right now"
                    : `Review ${waiting} decision${waiting === 1 ? "" : "s"} in Approvals`}
              </button>
            </>
          )}
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
