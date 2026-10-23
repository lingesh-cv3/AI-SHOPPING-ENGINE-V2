import { useEffect, useState } from "react";
import {
  PAGE_SIZE,
  closeFollowUp,
  dismissRecent,
  loadDetail,
  loadList,
  registerOpenView,
  useFollowUps,
  type FollowUp,
} from "./merchantFollowUpsStore";

const NOTE_MAX = 400;

function words(text: string): string {
  return text.replace(/_/g, " ").toLowerCase();
}

function clock(ms: number | null): string {
  return ms === null
    ? ""
    : new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** "42 min", "3 h 5 min", "2 days". Age only - there is no SLA, so nothing is called late. */
function age(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 60 * 24) {
    const m = minutes % 60;
    return `${Math.floor(minutes / 60)} h${m ? ` ${m} min` : ""}`;
  }
  const days = Math.floor(minutes / (60 * 24));
  return `${days} day${days === 1 ? "" : "s"}`;
}

/**
 * The shoppers the engine told a person from this shop would follow up with.
 *
 * Each one is a promise already made to the merchant's own customer, so this is the
 * merchant's to work: see who is waiting and why, read the conversation, and close it
 * once it has been dealt with. Closing needs one of two things - a message that goes
 * to the shopper, or an explicit confirmation that the shopper has already been
 * contacted directly - because a follow-up closed in silence leaves the shopper
 * waiting for the help they were promised.
 *
 * What this will not do: show "nothing waiting" while loading or after a failed first
 * load, hide the rest of a long list (the total is always shown), call anything late
 * (no SLA exists), or show a shopper's contact unless the engine truly knows it.
 */
export function MerchantFollowUps() {
  const s = useFollowUps();
  const [tab, setTab] = useState<"open" | "handled">("open");

  useEffect(() => {
    const unregister = registerOpenView();
    void loadList("open", 0);
    return unregister;
  }, []);

  function showHandled() {
    setTab("handled");
    if (s.handled.asOf === null) void loadList("handled", 0);
  }

  return (
    <div className="appr">
      <div className="overview-header">
        <div>
          <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Shopper follow-ups</h2>
          <p className="overview-subtitle">
            Shoppers the assistant told that a person from your shop would follow up.
            They were promised help; closing one records that you dealt with it.
          </p>
        </div>
      </div>

      <div className="appr-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "open"}
          className="appr-tab"
          onClick={() => setTab("open")}
        >
          Waiting for a follow-up{s.open.total !== null ? ` (${s.open.total})` : ""}
        </button>
        <button
          role="tab"
          aria-selected={tab === "handled"}
          className="appr-tab"
          onClick={showHandled}
        >
          Already handled
        </button>
      </div>

      {s.unauthorised && (
        <p className="empty">
          The engine refused your key. Reload this page to sign in again.
        </p>
      )}

      {tab === "open" ? <OpenTab /> : <HandledTab />}
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

function OpenTab() {
  const s = useFollowUps();
  const p = s.open;

  return (
    <>
      {s.recent.length > 0 && (
        <div className="appr-recent">
          {s.recent.map((r) => (
            <div key={r.caseId} className="qresult ok">
              <div className="gate-label">
                {r.alreadyClosed ? "Already closed" : "Closed"}
              </div>
              <p className="qsummary">
                {r.alreadyClosed
                  ? "That follow-up had already been closed, so nothing was changed."
                  : r.toldShopper
                    ? "Closed, and your message was sent to the shopper in their conversation."
                    : r.contactedDirectly
                      ? "Closed. You said you had already contacted the shopper directly, so no message was sent."
                      : "Closed. No message was sent to the shopper."}
              </p>
              <button
                className="appr-dismiss"
                onClick={() => dismissRecent(r.caseId)}
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
          Could not load your follow-ups{p.error ? ` (${p.error})` : ""}. This is not the
          same as nobody waiting - try again shortly.
        </p>
      )}

      {p.phase === "ready" && (
        <>
          <StatusLine asOf={p.asOf} refreshing={p.refreshing} error={p.error} />
          {p.total === 0 ? (
            <p className="empty">
              No shoppers are waiting for a follow-up. New ones appear here on their own;
              this page checks every 20 seconds.
            </p>
          ) : (
            <>
              {p.items.map((item) => (
                <FollowUpCard key={item.case_id} item={item} />
              ))}
              <Pager
                offset={p.offset}
                total={p.total ?? 0}
                onPage={(o) => void loadList("open", o)}
              />
            </>
          )}
        </>
      )}
    </>
  );
}

function FollowUpCard({ item }: { item: FollowUp }) {
  const s = useFollowUps();
  const detail = s.details[item.case_id];
  const [showDetail, setShowDetail] = useState(false);
  const [closing, setClosing] = useState(false);
  const [note, setNote] = useState("");
  const [contacted, setContacted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Closing needs a message for the shopper, or an explicit confirmation that the
  // shopper has already been contacted directly. Nothing else closes it from here.
  const canClose = note.trim().length > 0 || contacted;

  function toggleDetail() {
    const next = !showDetail;
    setShowDetail(next);
    if (next && detail === undefined) void loadDetail(item.case_id);
  }

  async function close() {
    if (!canClose) return;
    setBusy(true);
    setError(null);
    try {
      await closeFollowUp(item.case_id, { note, contactedDirectly: contacted });
    } catch (e) {
      setError(
        e instanceof Error
          ? `That did not go through: ${e.message}. Nothing was changed.`
          : "That did not go through. Nothing was changed.",
      );
      setBusy(false);
    }
  }

  return (
    <article className="qcard" data-testid="follow-up-card">
      <div className="qhead">
        <div>
          <span className="eyebrow">
            {item.friction_type ? words(item.friction_type) : "needs a person"}
          </span>
          <h3 className="qaction">
            {item.order_id ? `Order ${item.order_id}` : "Shopper waiting for a follow-up"}
          </h3>
        </div>
        <div className="qmeta">
          {item.diagnosis && (
            <span className={item.used_model ? "prov ai" : "prov rules"}>
              {item.used_model ? "AI" : "rules"}
            </span>
          )}
        </div>
      </div>

      <div className="waitline">
        waiting <span className="num">{age(item.waiting_minutes ?? 0)}</span>
        {item.query && (
          <>
            <span className="fu-sep" aria-hidden="true">
              &middot;
            </span>
            searched &ldquo;{item.query}&rdquo;
          </>
        )}
      </div>

      {item.diagnosis && (
        <>
          <div className="gate-label" style={{ marginTop: 10 }}>
            What happened
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
      {item.rejected?.length > 0 && (
        <>
          <div className="gate-label" style={{ marginTop: 10 }}>
            The engine could not do this itself
          </div>
          {item.rejected.map((r) => (
            <div key={r.action_type} className="gate-note">
              <strong>{words(r.action_type)}</strong> - {r.detail}
            </div>
          ))}
        </>
      )}

      {item.has_conversation && (
        <button className="appr-link" onClick={toggleDetail} aria-expanded={showDetail}>
          {showDetail ? "Hide conversation and contact" : "Show conversation and contact"}
        </button>
      )}
      {showDetail && <Detail state={detail} />}

      {error && <p className="note">{error}</p>}

      {closing ? (
        <div className="rejectbox fu-close">
          <div className="gate-label" style={{ marginBottom: 6 }}>
            Message to the shopper
          </div>
          <textarea
            className="fu-note"
            value={note}
            maxLength={NOTE_MAX}
            rows={3}
            onChange={(e) => setNote(e.target.value)}
            placeholder="We have emailed you a payment link."
            aria-label="Message to the shopper"
            autoFocus
          />
          <div className="fu-counter">
            {note.length} / {NOTE_MAX}
          </div>
          <p className="note">
            This is sent to the shopper in their conversation, word for word - write it
            to them, not about them.
          </p>
          <label className="fu-confirm">
            <input
              type="checkbox"
              checked={contacted}
              onChange={(e) => setContacted(e.target.checked)}
            />
            <span>I&rsquo;ve already contacted the shopper directly</span>
          </label>
          {!canClose && (
            <p className="note">
              Write a message, or confirm you have already been in touch. Otherwise the
              shopper is left waiting for the help they were promised.
            </p>
          )}
          <div className="qbuttons" style={{ marginTop: 10, paddingTop: 0, border: 0 }}>
            <button className="add" disabled={busy || !canClose} onClick={close}>
              {busy ? "Closing..." : "Close follow-up"}
            </button>
            <button
              className="reject"
              disabled={busy}
              onClick={() => {
                setClosing(false);
                setNote("");
                setContacted(false);
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="qbuttons">
          <button className="add" onClick={() => setClosing(true)}>
            Mark as handled
          </button>
        </div>
      )}
    </article>
  );
}

function Detail({ state }: { state: ReturnType<typeof useFollowUps>["details"][string] | undefined }) {
  if (state === undefined || state.phase === "loading") {
    return <p className="note">Loading…</p>;
  }
  if (state.phase === "error" || state.data === null) {
    return (
      <p className="note" role="alert">
        Could not load the conversation{state.error ? ` (${state.error})` : ""}.
      </p>
    );
  }
  const { shopper, conversation, conversation_limit } = state.data;
  return (
    <div className="fu-detail">
      <div className="gate-label">Shopper</div>
      {shopper.account ? (
        <p className="fu-contact">
          {shopper.account.display_name}
          {shopper.account.username !== shopper.account.display_name &&
            ` (${shopper.account.username})`}
          {" - "}
          {shopper.account.email ?? "no email on file"}
        </p>
      ) : (
        <p className="note">No contact on record: {shopper.why}.</p>
      )}

      <div className="gate-label" style={{ marginTop: 10 }}>
        Conversation
      </div>
      {conversation.length === 0 ? (
        <p className="note">No conversation is recorded for this case.</p>
      ) : (
        <>
          <ul className="fu-turns">
            {conversation.map((t, i) => (
              <li key={i} className={t.speaker === "shopper" ? "fu-turn shopper" : "fu-turn"}>
                <span className="fu-speaker">
                  {t.speaker === "shopper" ? "Shopper" : "Assistant"}
                </span>{" "}
                {t.text}
              </li>
            ))}
          </ul>
          {conversation.length >= conversation_limit && (
            <p className="note">Showing the last {conversation_limit} messages.</p>
          )}
        </>
      )}
    </div>
  );
}

function HandledTab() {
  const s = useFollowUps();
  const h = s.handled;

  if (h.phase === "idle" || h.phase === "loading") return <p className="empty">Loading…</p>;
  if (h.phase === "error") {
    return (
      <p className="empty">
        Could not load what has been handled{h.error ? ` (${h.error})` : ""}. This is not
        the same as nothing handled - try again shortly.
      </p>
    );
  }

  return (
    <>
      <StatusLine asOf={h.asOf} refreshing={h.refreshing} error={h.error} />
      {h.items.length === 0 ? (
        <p className="empty">
          Nothing handled yet. Follow-ups you close are listed here, with who closed them
          and what the shopper was told.
        </p>
      ) : (
        <section className="panel">
          <div className="panel-body">
            {h.items.map((d) => (
              <div key={d.case_id} className="histrow" data-testid="handled-row">
                <div className="hist-head">
                  <span>
                    <span className="tag ok">handled</span>{" "}
                    <span className="hist-action">
                      {d.order_id ? `order ${d.order_id}` : words(d.friction_type ?? "follow-up")}
                    </span>
                  </span>
                  <span className="eyebrow">
                    {d.handled_at ? new Date(d.handled_at).toLocaleString() : ""}
                  </span>
                </div>
                <div className="gate-note">by {d.handled_by ?? "unknown"}</div>
                {d.handled_note ? (
                  <p className="hist-note">Told the shopper: {d.handled_note}</p>
                ) : (
                  <p className="hist-note">No message was sent to the shopper.</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
      {(h.total ?? 0) > 0 && (
        <Pager offset={h.offset} total={h.total ?? 0} onPage={(o) => void loadList("handled", o)} />
      )}
    </>
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
