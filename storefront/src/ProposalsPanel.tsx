import { useEffect, useState } from "react";
import { api, type CartProposal } from "./api";

/**
 * What the assistant has suggested for the cart, waiting on the shopper's
 * own decision - never something already done. Nothing here mutates the
 * cart by itself: Accept re-checks the suggestion against live data and only
 * then runs it through the same Decision Engine and Risk Gate a tapped
 * "add to cart" button already goes through; Reject just marks it declined.
 *
 * Refetches whenever `refreshSignal` changes (the caller bumps this after
 * every chat turn, since a new proposal is created mid-conversation, not by
 * a dedicated button) and whenever the cart itself changes.
 */
export function ProposalsPanel({
  sessionId,
  cartId,
  refreshSignal,
  onCartChanged,
}: {
  sessionId: string;
  cartId: string | null;
  refreshSignal: number;
  /** Called after an accepted proposal actually changed the cart, so the
   *  caller can refetch it - this panel never guesses at cart state itself. */
  onCartChanged: () => void;
}) {
  const [proposals, setProposals] = useState<CartProposal[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [noteById, setNoteById] = useState<Record<string, string>>({});

  useEffect(() => {
    // No state write here when there is no cart yet - the render guard
    // below (`!cartId || proposals.length === 0`) already hides anything
    // stale, so there is nothing to synchronize.
    if (!cartId) return;
    let cancelled = false;
    api
      .listProposals(sessionId, cartId)
      .then((r) => {
        if (!cancelled) setProposals(r.proposals);
      })
      .catch(() => {
        // Best-effort. A shopper who never sees a suggestion is no worse
        // off than before this panel existed.
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, cartId, refreshSignal]);

  if (!cartId || proposals.length === 0) return null;

  const describe = (p: CartProposal) => {
    switch (p.change_type) {
      case "ADD":
        return `Add ${p.product_id}`;
      case "REMOVE":
        return `Remove ${p.product_id}`;
      case "REPLACE":
        return `Replace ${p.replaces_product_id} with ${p.product_id}`;
      case "CHANGE_QUANTITY":
        return `Set ${p.product_id} to ${p.quantity ?? "a new"} quantity`;
      default:
        return p.reason;
    }
  };

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Suggestions</span>
      </div>
      <div className="panel-body">
        {proposals.map((p) => (
          <div key={p.proposal_id} className="line" style={{ flexDirection: "column", alignItems: "stretch" }}>
            <div>{describe(p)}</div>
            {p.reason && <div className="line-qty">{p.reason}</div>}
            {noteById[p.proposal_id] ? (
              <p className="note">{noteById[p.proposal_id]}</p>
            ) : (
              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                <button
                  className="add"
                  disabled={busyId === p.proposal_id}
                  onClick={async () => {
                    setBusyId(p.proposal_id);
                    try {
                      const r = await api.acceptProposal(p.proposal_id, sessionId, cartId);
                      setNoteById((prev) => ({ ...prev, [p.proposal_id]: r.reply }));
                      setProposals((prev) => prev.filter((x) => x.proposal_id !== p.proposal_id));
                      if (r.cart_changed) onCartChanged();
                    } catch {
                      setNoteById((prev) => ({
                        ...prev,
                        [p.proposal_id]: "That didn't go through. Try again in a moment.",
                      }));
                    } finally {
                      setBusyId(null);
                    }
                  }}
                >
                  {busyId === p.proposal_id ? "Working…" : "Accept"}
                </button>
                <button
                  className="line-remove"
                  style={{ width: "auto", padding: "0 12px" }}
                  disabled={busyId === p.proposal_id}
                  onClick={async () => {
                    setBusyId(p.proposal_id);
                    try {
                      await api.rejectProposal(p.proposal_id, sessionId, cartId);
                      setProposals((prev) => prev.filter((x) => x.proposal_id !== p.proposal_id));
                    } finally {
                      setBusyId(null);
                    }
                  }}
                >
                  No thanks
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
