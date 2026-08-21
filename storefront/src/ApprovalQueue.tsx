import { useCallback, useEffect, useState } from "react";
import { console_api, type Decision, type QueueItem, type Stats } from "./api";

/**
 * Where a CV3 operator works.
 *
 * The design decision that matters here: every card carries the full reasoning
 * that produced it - the diagnosis, the evidence, the term the AI chose, and what
 * the shopper was actually told. A queue that shows only an action name is asking
 * someone to approve a thing they cannot evaluate, which produces either rubber
 * stamping or paralysis.
 *
 * When the AI's reply was overridden, both versions are shown side by side. The
 * operator should be able to see that the engine corrected its own model, because
 * that is exactly the sort of thing a person reviewing the case needs to know.
 */
export function ApprovalQueue() {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [results, setResults] = useState<Record<string, Decision>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [q, s] = await Promise.all([console_api.queue(), console_api.stats()]);
      setQueue(q.approvals);
      setStats(s);
    } catch {
      setError("Could not reach the engine. Is it running on port 8000?");
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
      // Refresh the counts, but keep the card on screen showing its result. A card
      // that vanishes the instant you click it leaves the operator unsure whether
      // anything happened.
      const s = await console_api.stats();
      setStats(s);
    } catch {
      setError("That decision did not go through.");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <p className="empty">{error}</p>;

  return (
    <div>
      {stats && (
        <div className="statgrid">
          <Stat label="Cases" value={stats.cases} />
          <Stat label="Waiting on you" value={stats.pending_approvals} warn />
          <Stat label="Ran automatically" value={stats.auto_cleared} />
          <Stat label="Reasoned by AI" value={stats.reasoned_by_model} />
        </div>
      )}

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Waiting for a decision</span>
          <button className="refresh" onClick={refresh}>
            Refresh
          </button>
        </div>
        <div className="panel-body">
          {queue.length === 0 && (
            <p className="empty">
              Nothing waiting. Trigger a dead search or a declined payment in the
              shop and it will appear here.
            </p>
          )}

          {queue.map((item) => {
            const result = results[item.approval_id];
            const overridden =
              item.model_reply &&
              item.shopper_reply &&
              item.model_reply !== item.shopper_reply;

            return (
              <article key={item.approval_id} className="qcard">
                <div className="qhead">
                  <div>
                    <span className="eyebrow">
                      {item.friction_type?.replace(/_/g, " ") ?? "assistance"}
                    </span>
                    <h3 className="qaction">
                      {item.action_type.replace(/_/g, " ").toLowerCase()}
                    </h3>
                  </div>
                  <div className="qmeta">
                    {item.financial && <span className="tag money">moves money</span>}
                    <span className={item.used_model ? "prov ai" : "prov rules"}>
                      {item.used_model ? "AI" : "rules"}
                    </span>
                  </div>
                </div>

                {item.diagnosis && (
                  <>
                    <div className="gate-label">Why</div>
                    <p className="qdiag">{item.diagnosis}</p>
                  </>
                )}

                {item.evidence.length > 0 && (
                  <ul className="evidence">
                    {item.evidence.map((e) => (
                      <li key={e}>{e}</li>
                    ))}
                  </ul>
                )}

                {item.shopper_reply && (
                  <>
                    <div className="gate-label">The shopper was told</div>
                    <p className="said">{item.shopper_reply}</p>
                  </>
                )}

                {overridden && (
                  <div className="gate-note swapped">
                    The AI wanted to say: &ldquo;{item.model_reply}&rdquo; — the
                    engine replaced it because the actions it suggested
                    aren&rsquo;t available on this platform.
                  </div>
                )}

                {item.rejected.length > 0 && (
                  <>
                    <div className="gate-label" style={{ marginTop: 10 }}>
                      Ruled out
                    </div>
                    {item.rejected.map((r) => (
                      <div key={r.action_type} className="gate-note">
                        <strong>{r.action_type.replace(/_/g, " ").toLowerCase()}</strong>{" "}
                        — {r.detail}
                      </div>
                    ))}
                  </>
                )}

                <div className="gate-note" style={{ marginTop: 8 }}>
                  {item.selection_reason}
                </div>
                <div className="rule">{item.risk_rule}</div>

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
                          {result.executed.latency_ms !== null &&
                            ` · ${result.executed.latency_ms}ms`}
                        </div>
                      </>
                    ) : (
                      <p className="qsummary">
                        Recorded. Nothing was executed.
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
                      {busy === item.approval_id ? "Working…" : "Approve and run"}
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
          })}
        </div>
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  warn,
}: {
  label: string;
  value: number;
  warn?: boolean;
}) {
  return (
    <div className="stat">
      <div className={warn && value > 0 ? "statnum warn" : "statnum"}>{value}</div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}