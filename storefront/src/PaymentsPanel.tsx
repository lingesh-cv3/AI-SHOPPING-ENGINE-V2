import { useCallback, useEffect, useState } from "react";
import {
  console_api,
  type Capabilities,
  type Decision,
  type MerchantReport as Report,
  type QueueItem,
} from "./api";

const RECOVERY_ACTIONS = new Set([
  "RETRY_PAYMENT",
  "OFFER_ALTERNATE_PAYMENT",
  "SPLIT_PAYMENT",
]);

export function PaymentsPanel() {
  const [report, setReport] = useState<Report | null>(null);
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [results, setResults] = useState<Record<string, Decision>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [r, c, q] = await Promise.all([
        console_api.report(),
        console_api.capabilities(),
        console_api.queue(),
      ]);
      setReport(r);
      setCaps(c);
      setQueue(q.approvals);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load payments data.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function decide(id: string, approved: boolean) {
    setBusy(id);
    try {
      const result = await console_api.decide(id, approved);
      setResults((prev) => ({ ...prev, [id]: result }));
      await refresh();
    } catch {
      setError("That decision did not go through. Nothing was changed.");
    } finally {
      setBusy(null);
    }
  }

  if (error) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Payments &amp; checkout</span>
        </div>
        <div className="panel-body">
          <p className="empty">{error}</p>
        </div>
      </section>
    );
  }

  if (!report || !caps || !queue) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Payments &amp; checkout</span>
        </div>
        <div className="panel-body">
          <p className="empty">Loading...</p>
        </div>
      </section>
    );
  }

  const recoverySupported = report.supports_payment_recovery;

  const declines =
    report.friction.find((f) => f.type === "PAYMENT_DECLINED")?.count ?? 0;

  const recoveryQueue = queue.filter(
    (item) => item.financial && RECOVERY_ACTIONS.has(item.action_type),
  );

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Payments &amp; checkout</span>
        <span className="eyebrow">last {report.days} days</span>
      </div>
      <div className="panel-body">
        <div className="figures">
          <Figure
            value={report.completed_order_count}
            label="Completed orders"
            title={`${report.total_sales_amount} ${report.total_sales_currency} total`}
          />
          <Figure
            value={declines}
            label="Payment failures"
            warn={declines > 0}
          />
          {recoverySupported && (
            <>
              <Figure
                value={report.recovery_opportunities}
                label="Recovery opportunities"
                title="Declines where a recovery action was proposed - not every decline gets one."
              />
              <Figure
                value={recoveryQueue.length}
                label="Pending recovery"
                warn={recoveryQueue.length > 0}
              />
              <Figure
                value={report.recovery_count}
                label="Recoveries completed"
              />
            </>
          )}
        </div>

        {recoverySupported && (
          <div className="headline-figure" style={{ marginTop: 14 }}>
            <div className="eyebrow">Revenue recovered</div>
            <div className="bignum num">
              {report.revenue_recovered} {report.currency}
            </div>
            <p className="note" style={{ margin: "4px 0 0" }}>
              Money actually captured from a declined payment, never a
              projection - never counted until the payment genuinely went
              through.
            </p>
          </div>
        )}

        {!recoverySupported ? (
          <p className="empty" style={{ marginTop: 14 }}>
            Payment recovery is not supported on this platform. The
            adapter does not declare a recovery method, so no recovery cases
            can exist here. A declined payment on this platform is handed to
            a person as an escalation instead.
          </p>
        ) : (
          <>
            <div className="gate-label" style={{ margin: "18px 0 6px" }}>
              Waiting for your decision ({recoveryQueue.length})
            </div>
            {recoveryQueue.length === 0 ? (
              <p className="note" style={{ marginTop: 0 }}>
                Nothing waiting right now. A recovery opportunity from a
                declined payment will appear here.
              </p>
            ) : (
              recoveryQueue.map((item) => {
                const result = results[item.approval_id];
                return (
                  <article key={item.approval_id} className="qcard">
                    <div className="qhead">
                      <div>
                        <span className="eyebrow">
                          {item.friction_type?.replace(/_/g, " ") ?? "payment issue"}
                        </span>
                        <h3 className="qaction">
                          {item.action_type.replace(/_/g, " ").toLowerCase()}
                        </h3>
                      </div>
                      <div className="qmeta">
                        <span className="tag money">moves money</span>
                        {item.order_id && (
                          <span className="eyebrow">order {item.order_id}</span>
                        )}
                      </div>
                    </div>

                    {item.diagnosis && (
                      <>
                        <div className="gate-label">Why</div>
                        <p className="qdiag">{item.diagnosis}</p>
                      </>
                    )}

                    {item.shopper_reply && (
                      <>
                        <div className="gate-label">The shopper was told</div>
                        <p className="said">{item.shopper_reply}</p>
                      </>
                    )}

                    {result ? (
                      <div
                        className={
                          result.executed?.succeeded ? "qresult ok" : "qresult bad"
                        }
                      >
                        <div className="gate-label">
                          {result.state === "APPROVED" ? "Approved" : "Rejected"}
                        </div>
                        {result.executed ? (
                          <>
                            <p className="qsummary">{result.executed.summary}</p>
                            <div className="gate-note num">
                              {result.executed.final_state}
                            </div>
                          </>
                        ) : (
                          <p className="qsummary">
                            {result.reason ?? "Recorded. Nothing was executed."}
                          </p>
                        )}
                      </div>
                    ) : (
                      <div className="qbuttons">
                        <button
                          className="add"
                          disabled={busy === item.approval_id}
                          onClick={() => decide(item.approval_id, true)}
                        >
                          {busy === item.approval_id ? "Working..." : "Approve"}
                        </button>
                        <button
                          className="reject"
                          disabled={busy === item.approval_id}
                          onClick={() => decide(item.approval_id, false)}
                        >
                          Reject
                        </button>
                      </div>
                    )}
                  </article>
                );
              })
            )}
          </>
        )}
      </div>
    </section>
  );
}

function Figure({
  value,
  label,
  warn,
  title,
}: {
  value: number;
  label: string;
  warn?: boolean;
  title?: string;
}) {
  return (
    <div title={title}>
      <div className={warn && value > 0 ? "figure num warn" : "figure num"}>
        {value}
      </div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}
