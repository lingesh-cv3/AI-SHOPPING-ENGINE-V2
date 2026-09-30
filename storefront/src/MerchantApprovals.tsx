import { useEffect, useState } from "react";
import {
  PAGE_SIZE,
  decide,
  dismissRecent,
  loadHistory,
  loadPending,
  loadRules,
  registerPendingView,
  useApprovals,
  useNow,
  type ApprovalItem,
  type DecidedItem,
} from "./merchantApprovalsStore";

function words(actionType: string): string {
  return actionType.replace(/_/g, " ").toLowerCase();
}

function clock(ms: number | null): string {
  return ms === null
    ? ""
    : new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * Every decision the engine is waiting on this merchant for.
 *
 * All of them, not only payment recovery: a declined-card recovery, a promotion,
 * anything held because the merchant chose Cautious mode or restricted the
 * allowlist. The merchant owns every one of these decisions - it is their money,
 * their customer and their policy - so this is the one place they are made.
 *
 * What it will not do: show "nothing waiting" while it is still loading or when
 * it could not check, hide the rest of a long queue (the total is always shown),
 * or offer an Approve button on something approving cannot run.
 */
export function MerchantApprovals() {
  const s = useApprovals();
  const [tab, setTab] = useState<"pending" | "history">("pending");

  useEffect(() => {
    // While this page is open, the 20-second poll refreshes the list (and with it
    // the count) rather than fetching a single row for the badge.
    const unregister = registerPendingView();
    void loadPending(0);
    void loadRules();
    return unregister;
  }, []);

  function showHistory() {
    setTab("history");
    // Fetched again whenever the merchant has decided something since last time.
    if (s.history.asOf === null) void loadHistory(0);
  }

  const waiting = s.pending.total;

  return (
    <div className="appr">
      <div className="overview-header">
        <div>
          <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Approvals</h2>
          <p className="overview-subtitle">
            Decisions the engine is holding for you. Each one expires if nobody acts,
            and the shopper waiting on it is told either way.
          </p>
        </div>
      </div>

      <div className="appr-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "pending"}
          className="appr-tab"
          onClick={() => setTab("pending")}
        >
          Waiting for you{waiting !== null ? ` (${waiting})` : ""}
        </button>
        <button
          role="tab"
          aria-selected={tab === "history"}
          className="appr-tab"
          onClick={showHistory}
        >
          Already decided
        </button>
      </div>

      {s.unauthorised && (
        <p className="empty">
          The engine refused your key. Reload this page to sign in again.
        </p>
      )}

      {tab === "pending" ? <PendingTab /> : <HistoryTab />}
    </div>
  );
}

function StatusLine({
  asOf,
  refreshing,
  error,
}: {
  asOf: number | null;
  refreshing: boolean;
  error: string | null;
}) {
  return (
    <div className="appr-status" aria-live="polite">
      {asOf !== null && <span>as of {clock(asOf)}</span>}
      {refreshing && <span> · refreshing…</span>}
      {error && (
        <span className="appr-stale">
          {" "}
          · could not refresh ({error}) - showing the last data loaded
        </span>
      )}
    </div>
  );
}

function PendingTab() {
  const s = useApprovals();
  const p = s.pending;

  return (
    <>
      {s.recent.length > 0 && (
        <div className="appr-recent">
          {s.recent.map((r) => (
            <div
              key={r.approvalId}
              className={
                r.approved && !r.result.executed?.succeeded ? "qresult bad" : "qresult ok"
              }
            >
              <div className="gate-label">
                {words(r.actionType)} -{" "}
                {r.result.state === "APPROVED"
                  ? "approved"
                  : r.result.state === "EXPIRED"
                    ? "too late"
                    : r.result.state === "REJECTED"
                      ? "rejected"
                      : r.result.state.toLowerCase()}
              </div>
              <p className="qsummary">
                {r.result.reason ??
                  r.result.executed?.summary ??
                  "Recorded. Nothing was run."}
              </p>
              <button
                className="appr-dismiss"
                onClick={() => dismissRecent(r.approvalId)}
                aria-label="Dismiss"
              >
                Dismiss
              </button>
            </div>
          ))}
        </div>
      )}

      {(p.phase === "idle" || p.phase === "loading") && <p className="empty">Loading…</p>}

      {p.phase === "error" && (
        <p className="empty">
          Could not load your approvals{p.error ? ` (${p.error})` : ""}. This is not
          the same as nothing waiting - try again shortly.
        </p>
      )}

      {p.phase === "ready" && (
        <>
          <StatusLine asOf={p.asOf} refreshing={p.refreshing} error={p.error} />

          {p.total === 0 ? (
            <p className="empty">
              Nothing is waiting for your decision. New ones appear here on their own;
              this page checks every 20 seconds.
            </p>
          ) : (
            <>
              {p.items.map((item) => (
                <ApprovalCard key={item.approval_id} item={item} />
              ))}
              <Pager
                offset={p.offset}
                total={p.total ?? 0}
                onPage={(o) => void loadPending(o)}
              />
            </>
          )}
        </>
      )}
    </>
  );
}

function ApprovalCard({ item }: { item: ApprovalItem }) {
  const s = useApprovals();
  const now = useNow();
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remaining = item.expires_at
    ? Math.floor((Date.parse(item.expires_at) - now) / 60000)
    : null;
  const lapsed = remaining !== null && remaining <= 0;
  const urgent = remaining !== null && remaining <= 5;
  const canRun = item.executable.can_run;
  const overridden =
    item.model_reply && item.shopper_reply && item.model_reply !== item.shopper_reply;
  const rule = item.risk_rule ? (s.rules[item.risk_rule] ?? item.risk_rule) : null;

  async function act(approved: boolean) {
    setBusy(true);
    setError(null);
    try {
      await decide(item, approved, approved ? undefined : note);
    } catch (e) {
      setError(
        e instanceof Error
          ? `That decision did not go through: ${e.message}. Nothing was changed.`
          : "That decision did not go through. Nothing was changed.",
      );
      setBusy(false);
    }
  }

  return (
    <article className={urgent ? "qcard urgent" : "qcard"} data-testid="approval-card">
      <div className="qhead">
        <div>
          <span className="eyebrow">
            {item.friction_type ? item.friction_type.replace(/_/g, " ").toLowerCase() : "held for approval"}
          </span>
          <h3 className="qaction">{words(item.action_type)}</h3>
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
        {remaining !== null && (
          <>
            {" - "}
            {lapsed ? (
              <span className="num warn">timing out - clears at the next refresh</span>
            ) : (
              <>
                <span className={urgent ? "num warn" : "num"}>{remaining}m</span> before it
                times out
              </>
            )}
          </>
        )}
        {item.order_id && ` - order ${item.order_id}`}
      </div>

      {item.diagnosis && (
        <>
          <div className="gate-label" style={{ marginTop: 10 }}>
            Why
          </div>
          <p className="qdiag">{item.diagnosis}</p>
        </>
      )}

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

      {overridden && (
        <div className="gate-note swapped">
          The AI wanted to say: {item.model_reply}
          {item.rejected?.length > 0
            ? " - replaced because the actions it suggested are not available on this platform."
            : " - replaced because it wrote as though the action had already run, and it has not."}
        </div>
      )}

      {item.rejected?.length > 0 && (
        <>
          <div className="gate-label" style={{ marginTop: 10 }}>
            Ruled out
          </div>
          {item.rejected.map((r) => (
            <div key={r.action_type} className="gate-note">
              <strong>{words(r.action_type)}</strong> - {r.detail}
            </div>
          ))}
        </>
      )}

      {rule && <div className="rule">Held because: {rule}</div>}

      {!canRun && (
        <div className="gate-note swapped" role="note">
          Cannot be run: {item.executable.reason} You can still reject it, and the
          shopper will be told the shop could not do it.
        </div>
      )}

      {error && <p className="note">{error}</p>}

      {rejecting ? (
        <div className="rejectbox">
          <div className="gate-label" style={{ marginBottom: 6 }}>
            Why are you turning this down? (optional)
          </div>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Customer already paid by transfer"
            aria-label="Reason for rejecting"
            autoFocus
          />
          <div className="qbuttons" style={{ marginTop: 10, paddingTop: 0, border: 0 }}>
            <button className="add" disabled={busy} onClick={() => act(false)}>
              {busy ? "Working..." : "Reject"}
            </button>
            <button
              className="reject"
              disabled={busy}
              onClick={() => {
                setRejecting(false);
                setNote("");
              }}
            >
              Cancel
            </button>
          </div>
          <p className="note">
            Your reason is kept for whoever reads this case next. It is not sent to the
            shopper.
          </p>
        </div>
      ) : (
        <div className="qbuttons">
          <button
            className="add"
            disabled={busy || lapsed || !canRun}
            onClick={() => act(true)}
            title={!canRun ? "This cannot be run, so approving it would do nothing" : undefined}
          >
            {busy ? "Working..." : "Approve and run"}
          </button>
          <button
            className="reject"
            disabled={busy || lapsed}
            onClick={() => setRejecting(true)}
          >
            Reject
          </button>
        </div>
      )}
    </article>
  );
}

function HistoryTab() {
  const s = useApprovals();
  const h = s.history;

  if (h.phase === "idle" || h.phase === "loading") return <p className="empty">Loading…</p>;
  if (h.phase === "error") {
    return (
      <p className="empty">
        Could not load your decision history{h.error ? ` (${h.error})` : ""}. This is not
        the same as nothing decided - try again shortly.
      </p>
    );
  }

  return (
    <>
      <StatusLine asOf={h.asOf} refreshing={h.refreshing} error={h.error} />
      {h.items.length === 0 ? (
        <p className="empty">
          Nothing decided yet. Approvals you approve or reject, and ones that time out,
          are listed here.
        </p>
      ) : (
        <section className="panel">
          <div className="panel-body">
            {h.items.map((d) => (
              <HistoryRow key={d.approval_id} d={d} />
            ))}
          </div>
        </section>
      )}
      {(h.total ?? 0) > 0 && (
        <Pager
          offset={h.offset}
          total={h.total ?? 0}
          onPage={(o) => void loadHistory(o)}
        />
      )}
    </>
  );
}

function HistoryRow({ d }: { d: DecidedItem }) {
  const ranButNot = d.state === "APPROVED" && d.final_state !== "OUTCOME";
  const label = ranButNot ? "approved, did not run" : d.state.toLowerCase();
  const tone =
    d.state === "APPROVED" ? (d.final_state === "OUTCOME" ? "ok" : "bad") : d.state === "EXPIRED" ? "money" : "";
  const when = d.decided_at ? new Date(d.decided_at).toLocaleString() : "";

  return (
    <div className="histrow" data-testid="history-row">
      <div className="hist-head">
        <span>
          <span className={`tag ${tone}`}>{label}</span>{" "}
          <span className="hist-action">{words(d.action_type)}</span>
        </span>
        <span className="eyebrow">{when}</span>
      </div>
      <div className="gate-note">
        {d.decided_by === "expired" ? "Nobody got to it in time" : `by ${d.decided_by ?? "unknown"}`}
        {d.revenue && ` - recovered ${d.revenue} ${d.currency ?? ""}`}
        {d.order_id && ` - ${d.order_id}`}
      </div>
      {ranButNot && (
        <p className="hist-note warn">
          Approved, but it did not go through
          {d.final_state === "UNSUPPORTED"
            ? " - not supported on this platform"
            : d.final_state === "DIAGNOSED"
              ? " - stalled waiting on a choice"
              : d.final_state === "FAILED"
                ? " - the platform could not complete it"
                : ""}
          . The shopper has not been told it worked.
        </p>
      )}
      {d.note && <p className="hist-note">{d.note}</p>}
    </div>
  );
}

function Pager({
  offset,
  total,
  onPage,
}: {
  offset: number;
  total: number;
  onPage: (offset: number) => void;
}) {
  if (total <= PAGE_SIZE) return null;
  const last = Math.min(offset + PAGE_SIZE, total);
  return (
    <div className="pager">
      <button
        disabled={offset === 0}
        className={offset === 0 ? "muted" : ""}
        onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))}
      >
        Earlier
      </button>
      <span className="pager-count">
        {offset + 1}&ndash;{last} of {total}
      </span>
      <button
        disabled={last >= total}
        className={last >= total ? "muted" : ""}
        onClick={() => onPage(offset + PAGE_SIZE)}
      >
        Later
      </button>
    </div>
  );
}
