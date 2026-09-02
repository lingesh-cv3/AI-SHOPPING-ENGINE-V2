import { useCallback, useEffect, useState } from "react";
import { NotAuthorised, operatorKey } from "./api";
import { OperatorSignIn } from "./OperatorSignIn";
import {
  ops_api,
  type Decision,
  type OpsDecision,
  type OpsHandover,
  type OpsQueueItem,
  type OpsStats,
} from "./api";

/**
 * Where a CV3 operator works.
 *
 * The old queue was scoped to one merchant, so somebody covering several clients
 * had to switch between them to find their work - and the oldest case on a quiet
 * shop could sit unseen while they worked a busy one. This is every merchant in one
 * list, ordered by how long a shopper has been waiting.
 *
 * Two things the per-merchant queue could not do:
 *
 * A rejection can carry a note. The database column existed and nothing ever asked
 * for one, so "why did we say no to that refund" had no answer.
 *
 * Decided work is visible. The queue showed pending only, which meant an operator
 * could not check what they had already done, who did it, or whether it worked.
 *
 * It refreshes on a timer. New work appearing only when somebody clicks is a queue
 * that quietly grows while nobody is looking at it.
 */
export function OpsConsole() {
  const [queue, setQueue] = useState<OpsQueueItem[]>([]);
  const [handovers, setHandovers] = useState<OpsHandover[]>([]);
  const [history, setHistory] = useState<OpsDecision[]>([]);
  const [stats, setStats] = useState<OpsStats | null>(null);
  const [results, setResults] = useState<Record<string, Decision>>({});
  const [rejecting, setRejecting] = useState<string | null>(null);
  // The handover being closed, and what the operator is writing about it.
  // Kept separate from the rejection note: one is a reason for saying no,
  // the other is a message to a customer, and they read very differently.
  const [closing, setClosing] = useState<string | null>(null);
  const [closingNote, setClosingNote] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Whether we hold a key at all. Separate from `error` because being
  // signed out is not a failure - it is the normal first state.
  const [signedIn, setSignedIn] = useState(() => operatorKey() !== null);

  const refresh = useCallback(async () => {
    try {
      const [q, h, s, hand] = await Promise.all([
        ops_api.queue(),
        ops_api.history(),
        ops_api.stats(),
        ops_api.handovers(),
      ]);
      setQueue(q.approvals);
      setHistory(h.decisions);
      setStats(s);
      setHandovers(hand.handovers);
      setError(null);
    } catch (e) {
      // A refused key is not an outage. Conflating them would have an operator
      // restarting services when they simply need to sign in again.
      if (e instanceof NotAuthorised) {
        setSignedIn(false);
        return;
      }
      setError("Could not reach the engine.");
    }
  }, []);

  useEffect(() => {
    if (!signedIn) return;
    refresh();
    // Twenty seconds. Fast enough that a case appears while an operator is still
    // looking at the page, slow enough not to redraw under their cursor.
    const id = window.setInterval(refresh, 20000);
    return () => window.clearInterval(id);
  }, [refresh, signedIn]);


  /** Mark a handover dealt with.
   *
   *  No approve or reject here, because there is nothing to decide. The engine has
   *  already said a person should handle this; the only question is whether one
   *  has. So the button says done.
   */
  async function closeHandover(item: OpsHandover, note: string) {
    setBusy(item.case_id);
    try {
      await ops_api.closeHandover(
        item.connection_id,
        item.case_id,
        note.trim() || undefined,
      );
      setClosing(null);
      setClosingNote("");
      await refresh();
    } catch {
      setError("Could not close that one.");
    } finally {
      setBusy(null);
    }
  }

  async function decide(item: OpsQueueItem, approved: boolean, why?: string) {
    setBusy(item.approval_id);
    try {
      const result = await ops_api.decide(
        item.connection_id,
        item.approval_id,
        approved,
        why,
      );
      setResults((prev) => ({ ...prev, [item.approval_id]: result }));
      setRejecting(null);
      setNote("");
      await refresh();
    } catch {
      setError("That decision did not go through.");
    } finally {
      setBusy(null);
    }
  }

  if (!signedIn) {
    return (
      <OperatorSignIn
        onSignedIn={() => {
          setSignedIn(true);
          setError(null);
        }}
      />
    );
  }

  if (error) return <p className="empty">{error}</p>;

  return (
    <div>
      {stats && (
        <div className="statgrid">
          <Stat label="Waiting on you" value={stats.waiting} warn={stats.waiting > 0} />
          <Stat
            label="Longest wait"
            value={stats.oldest_wait_minutes}
            suffix="m"
            warn={stats.oldest_wait_minutes >= 10}
          />
          <Stat label="Decided today" value={stats.today} />
          <Stat
            label="Merchants"
            value={Object.keys(stats.by_merchant).length || 0}
          />
        </div>
      )}

      {handovers.length > 0 && (
        <section className="panel handovers">
          <div className="panel-head">
            <span className="eyebrow">Needs someone to contact the shopper</span>
            <span className="eyebrow">
              {handovers.length} {handovers.length === 1 ? "person" : "people"} were
              promised help and have not had it
            </span>
          </div>
          <div className="panel-body">
            {handovers.map((item) => (
              <article key={item.case_id} className="handcard">
                <div className="qhead">
                  <div>
                    <span className="eyebrow">{item.merchant_name}</span>
                    <h3 className="qaction">
                      {item.friction_type
                        ? item.friction_type.replace(/_/g, " ").toLowerCase()
                        : "needs a person"}
                    </h3>
                  </div>
                  <span
                    className={
                      item.waiting_minutes > 30 ? "waitbadge late" : "waitbadge"
                    }
                  >
                    {item.waiting_minutes}m
                  </span>
                </div>

                {item.diagnosis && <p className="qdiag">{item.diagnosis}</p>}

                {item.evidence?.length > 0 && (
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

                {item.rejected?.length > 0 && (
                  <>
                    <div className="gate-label" style={{ marginTop: 10 }}>
                      The engine could not do this itself
                    </div>
                    {item.rejected.map((r) => (
                      <div key={r.action_type} className="gate-note">
                        <strong>
                          {r.action_type.replace(/_/g, " ").toLowerCase()}
                        </strong>{" "}
                        &mdash; {r.detail}
                      </div>
                    ))}
                  </>
                )}

                {item.order_id && (
                  <div className="rule">order {item.order_id}</div>
                )}

                {closing === item.case_id ? (
                  <div className="closebox">
                    <div className="gate-label" style={{ marginBottom: 6 }}>
                      What should the shopper be told?
                    </div>
                    <input
                      value={closingNote}
                      onChange={(e) => setClosingNote(e.target.value)}
                      placeholder="We have sent you a payment link by email"
                      aria-label="Message to the shopper"
                      autoFocus
                    />
                    <div className="qbuttons" style={{ border: 0, paddingTop: 10 }}>
                      <button
                        className="add"
                        disabled={busy === item.case_id}
                        onClick={() => closeHandover(item, closingNote)}
                      >
                        {busy === item.case_id ? "Sending..." : "Send and close"}
                      </button>
                      <button
                        className="reject"
                        onClick={() => {
                          setClosing(null);
                          setClosingNote("");
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                    <p className="note">
                      This goes straight to the shopper, so write it to them rather
                      than about them. Leave it blank to close without telling them
                      anything.
                    </p>
                  </div>
                ) : (
                  <div className="qbuttons">
                    <button
                      className="add"
                      onClick={() => {
                        setClosing(item.case_id);
                        setClosingNote("");
                      }}
                    >
                      Mark as handled
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        </section>
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
              Nothing waiting across any merchant. New work appears here on its own.
            </p>
          )}

          {queue.map((item) => {
            const result = results[item.approval_id];
            const overridden =
              item.model_reply &&
              item.shopper_reply &&
              item.model_reply !== item.shopper_reply;
            const urgent = item.minutes_left !== null && item.minutes_left <= 5;

            return (
              <article
                key={item.approval_id}
                className={urgent ? "qcard urgent" : "qcard"}
              >
                <div className="qhead">
                  <div>
                    <span className="eyebrow">{item.merchant_name}</span>
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

                <div className="waitline">
                  <span className="num">{item.waiting_minutes}m</span> waiting
                  {item.minutes_left !== null && (
                    <>
                      {" - "}
                      <span className={urgent ? "num warn" : "num"}>
                        {item.minutes_left}m
                      </span>{" "}
                      before it times out
                    </>
                  )}
                  {item.friction_type && ` - ${item.friction_type.replace(/_/g, " ").toLowerCase()}`}
                </div>

                {item.diagnosis && (
                  <>
                    <div className="gate-label" style={{ marginTop: 10 }}>
                      Why
                    </div>
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
                    The AI wanted to say: {item.model_reply}
                    {item.rejected.length > 0
                      ? " - replaced because the actions it suggested are not available on this platform."
                      : " - replaced because it wrote as though the action had already run, and it has not."}
                  </div>
                )}

                {item.rejected.length > 0 && (
                  <>
                    <div className="gate-label" style={{ marginTop: 10 }}>
                      Ruled out
                    </div>
                    {item.rejected.map((r) => (
                      <div key={r.action_type} className="gate-note">
                        <strong>
                          {r.action_type.replace(/_/g, " ").toLowerCase()}
                        </strong>{" "}
                        - {r.detail}
                      </div>
                    ))}
                  </>
                )}

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
                    <p className="qsummary">
                      {result.executed?.summary ?? "Recorded. Nothing was run."}
                    </p>
                  </div>
                ) : rejecting === item.approval_id ? (
                  <div className="rejectbox">
                    <div className="gate-label" style={{ marginBottom: 6 }}>
                      Why are you turning this down?
                    </div>
                    <input
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      placeholder="Customer already paid by transfer"
                      aria-label="Reason"
                      autoFocus
                    />
                    <div className="qbuttons" style={{ marginTop: 10, paddingTop: 0, border: 0 }}>
                      <button
                        className="add"
                        disabled={busy === item.approval_id}
                        onClick={() => decide(item, false, note || undefined)}
                      >
                        Reject
                      </button>
                      <button
                        className="reject"
                        onClick={() => {
                          setRejecting(null);
                          setNote("");
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                    <p className="note">
                      The next person to see this case reads what you write here.
                    </p>
                  </div>
                ) : (
                  <div className="qbuttons">
                    <button
                      className="add"
                      disabled={busy === item.approval_id}
                      onClick={() => decide(item, true)}
                    >
                      {busy === item.approval_id ? "Working..." : "Approve and run"}
                    </button>
                    <button
                      className="reject"
                      onClick={() => setRejecting(item.approval_id)}
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

      <section className="panel" style={{ marginTop: 20 }}>
        <div className="panel-head">
          <span className="eyebrow">Already decided</span>
          <span className="eyebrow">{history.length} most recent</span>
        </div>
        <div className="panel-body">
          {history.length === 0 && (
            <p className="empty">Nothing decided yet.</p>
          )}
          {history.map((d) => (
            <div key={d.approval_id} className="histrow">
              <div className="hist-head">
                <span>
                  <span className={`tag ${badge(d.state)}`}>{d.state.toLowerCase()}</span>{" "}
                  <span className="hist-action">
                    {d.action_type.replace(/_/g, " ").toLowerCase()}
                  </span>
                </span>
                <span className="eyebrow">{d.merchant_name}</span>
              </div>
              <div className="gate-note">
                {d.decided_by === "expired"
                  ? "Nobody got to it in time"
                  : `by ${d.decided_by}`}
                {d.revenue && ` - recovered ${d.revenue} ${d.currency}`}
                {d.order_id && ` - ${d.order_id}`}
              </div>
              {d.note && <p className="hist-note">{d.note}</p>}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

/** Expiries get their own colour. They are not rejections - nobody decided. */
function badge(state: string): string {
  if (state === "APPROVED") return "ok";
  if (state === "EXPIRED") return "money";
  return "";
}

function Stat({
  label,
  value,
  suffix,
  warn,
}: {
  label: string;
  value: number;
  suffix?: string;
  warn?: boolean;
}) {
  return (
    <div className="stat">
      <div className={warn && value > 0 ? "statnum warn" : "statnum"}>
        {value}
        {suffix}
      </div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}
