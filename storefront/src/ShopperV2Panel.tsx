import { useState } from "react";
import {
  api,
  getConnection,
  shopperMessage,
  type ChatReplyV2,
  type MissionItemV2,
  type ShoppingStateV2,
} from "./api";

/**
 * Shopper V2 - the decision-workspace panel for the new conversational
 * architecture (`engine/shopper_v2/`), connected to the actual storefront.
 *
 * Deliberately a SEPARATE, self-contained panel rather than a modification
 * of `ChatWidget.tsx` (V1's own assistant) - V1's own code is completely
 * untouched. As of the main-Shopper cutover, `App.tsx` renders this
 * component (not `ChatWidget`) as the default assistant a normal shopper
 * sees (`defaultOpen`), with `ChatWidget` kept fully intact behind an
 * explicit rollback control (`assistantMode` in App.tsx) rather than
 * deleted - exactly the "V1 stays live as a fallback while V2 is the main
 * path" shape CLAUDE.md's migration section describes. This component
 * itself does not know or care whether it is the main path or an opt-in
 * extra; that decision belongs entirely to its caller.
 *
 * Every section below renders ONLY fields the real `/api/v2/chat` response
 * actually returned this turn - `shopping_state`, `evidence`, `products`,
 * `trace` - never a value invented client-side. A cart proposal this panel
 * receives is approved/rejected through the EXISTING
 * `api.acceptProposal`/`api.rejectProposal` calls - the same ones
 * `ChatWidget.tsx` already uses for V1 - never a new endpoint. This panel
 * cannot mutate a cart by itself; only that explicit tap can, and a
 * PROPOSED product is always shown visibly distinct from one actually IN
 * the cart (a real `cart_changed`/accept response, never assumed).
 */

interface TurnV2 {
  who: "shopper" | "assistant";
  text: string;
  products?: { product_id: string; title: string; price: string | null }[];
  evidenceByProduct?: Record<string, string[]>;
  /** Every real, persisted proposal id this turn created - one for a
   *  single-item cart proposal, or several for a multi-item basket
   *  proposal. Always the real ids `db.cart_proposals.create` returned,
   *  never invented client-side. */
  proposalIds?: string[];
  capability?: string;
  state?: ShoppingStateV2;
  /** The real order id this turn's ORDER_STATUS answer named, if any -
   *  straight from `trace.order_id` (engine/shopper_v2/orchestrator.py's
   *  ORDER_STATUS branch), never invented client-side. Lets the panel
   *  show order status as its own distinct, labeled fact rather than
   *  indistinguishable prose. */
  orderId?: string;
  /** The real item count `trace.cart_item_count` returned for a
   *  CART_STATUS turn (engine/shopper_v2/cart_status.py), so "what's in
   *  my cart" reads as a distinct, labeled fact too. */
  cartItemCount?: number;
}

const CAPABILITY_LABELS: Record<string, string> = {
  discover: "Discovery",
  compare: "Comparison",
  compatibility: "Compatibility",
  complementary: "Complete the set",
  trade_off: "Trade-off re-rank",
  mission: "Mission tracking",
  cart_action: "Cart proposal",
  cart_status: "Your cart",
  checkout: "Checkout preparation",
  payment_recovery: "Payment recovery",
  order_status: "Order status",
};

function CapabilityBadge({ capability }: { capability?: string }) {
  if (!capability) return null;
  const label = CAPABILITY_LABELS[capability] ?? capability;
  return (
    <span className="v2-capability-badge" data-testid="shopper-v2-capability">
      {label}
    </span>
  );
}

function MissionSidebar({ state }: { state: ShoppingStateV2 }) {
  const hasSingleItemMission =
    state.goal || state.constraints.length > 0 || state.must_have.length > 0 ||
    state.priorities.length > 0 || state.budget_amount;
  const hasMultiItem = state.mission_items.length > 0;

  if (!hasSingleItemMission && !hasMultiItem) return null;

  return (
    <div className="v2-mission-sidebar" data-testid="shopper-v2-mission-sidebar">
      <h4>Your mission</h4>
      {hasSingleItemMission && (
        <ul className="v2-mission-facts">
          {state.goal && <li>Goal: {state.goal}</li>}
          {state.budget_amount && (
            <li>
              Budget: {state.budget_amount} {state.budget_currency ?? ""}
            </li>
          )}
          {state.constraints.length > 0 && (
            <li>Constraints: {state.constraints.join(", ")}</li>
          )}
          {state.must_have.length > 0 && (
            <li>Must have: {state.must_have.join(", ")}</li>
          )}
          {state.priorities.length > 0 && (
            <li>Priorities (most recent last): {state.priorities.join(" -> ")}</li>
          )}
        </ul>
      )}
      {hasMultiItem && (
        <div className="v2-mission-items" data-testid="shopper-v2-mission-items">
          <h5>Tracked items</h5>
          {state.mission_items.map((item: MissionItemV2) => (
            <div
              key={item.item_id}
              className={`v2-mission-item v2-mission-item-${item.status}`}
              data-testid="shopper-v2-mission-item"
            >
              <strong>{item.label}</strong>{" "}
              <span className="v2-mission-item-status">({item.status})</span>
              {item.constraints.length > 0 && (
                <div className="v2-mission-item-detail">
                  constraints: {item.constraints.join(", ")}
                </div>
              )}
              <div className="v2-mission-item-detail">
                {item.candidates.length === 0
                  ? "nothing found yet"
                  : `${item.candidates.length} option(s) found`}
                {item.selected_product_id ? " - proposed" : ""}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function ShopperV2Panel({
  cartId,
  onCartChanged,
  defaultOpen = false,
  onSwitchToClassic,
}: {
  cartId?: string;
  onCartChanged?: () => void;
  /** Main-Shopper cutover: the caller (App.tsx) passes `true` here to make
   *  this the default-open assistant a normal shopper sees, rather than a
   *  "beta" a shopper has to opt into - see App.tsx's own `assistantMode`
   *  state and CLAUDE.md's V1<->V2 migration notes. Defaults to `false` so
   *  nothing about this component's own behavior changes for any other
   *  caller that doesn't pass it. */
  defaultOpen?: boolean;
  /** Rollback control, rendered only when the caller wants one (App.tsx's
   *  main-Shopper path does) - switches the caller back to the V1
   *  ChatWidget. This component never decides on its own to hide itself;
   *  it only ever calls back up to whoever owns that decision. */
  onSwitchToClassic?: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [sessionId] = useState(
    () =>
      `sess_v2_${crypto.randomUUID?.() ?? Math.random().toString(36).slice(2)}`,
  );
  const [turns, setTurns] = useState<TurnV2[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decided, setDecided] = useState<Record<string, "accepted" | "rejected">>({});
  const [latestState, setLatestState] = useState<ShoppingStateV2 | null>(null);

  async function send(message: string) {
    if (!message.trim() || busy) return;
    setError(null);
    setTurns((prev) => [...prev, { who: "shopper", text: message }]);
    setDraft("");
    setBusy(true);
    try {
      const reply: ChatReplyV2 = await api.chatV2(sessionId, message, cartId);
      // A single-item cart proposal (`cart_proposal_id`) or a multi-item
      // basket proposal (`mission_proposal_ids`) - either way, every real,
      // persisted proposal id this turn actually created, never invented.
      const singleProposalId = reply.trace?.cart_proposal_id as string | undefined;
      const basketProposalIds = reply.trace?.mission_proposal_ids as string[] | undefined;
      const proposalIds = singleProposalId
        ? [singleProposalId]
        : basketProposalIds && basketProposalIds.length > 0
          ? basketProposalIds
          : undefined;
      const evidenceByProduct: Record<string, string[]> = {};
      for (const e of reply.evidence?.products ?? []) {
        if (e.evidence_fields?.length) evidenceByProduct[e.product_id] = e.evidence_fields;
      }
      setLatestState(reply.shopping_state);
      setTurns((prev) => [
        ...prev,
        {
          who: "assistant",
          text: reply.reply,
          products: reply.products,
          evidenceByProduct,
          proposalIds,
          capability: reply.trace?.capability as string | undefined,
          state: reply.shopping_state,
          orderId: reply.trace?.order_id as string | undefined,
          cartItemCount: reply.trace?.cart_item_count as number | undefined,
        },
      ]);
    } catch (err) {
      setError(shopperMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function approve(proposalId: string) {
    if (!cartId) return;
    setBusy(true);
    try {
      const result = await api.acceptProposal(proposalId, sessionId, cartId);
      setDecided((prev) => ({ ...prev, [proposalId]: "accepted" }));
      setTurns((prev) => [
        ...prev,
        { who: "assistant", text: `${result.reply} (now in your real cart)` },
      ]);
      onCartChanged?.();
    } catch (err) {
      setError(shopperMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function reject(proposalId: string) {
    if (!cartId) return;
    setBusy(true);
    try {
      await api.rejectProposal(proposalId, sessionId, cartId);
      setDecided((prev) => ({ ...prev, [proposalId]: "rejected" }));
    } catch (err) {
      setError(shopperMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="v2-panel-root" data-testid="shopper-v2-root">
      <button
        type="button"
        className="v2-toggle"
        data-testid="shopper-v2-toggle"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Close" : "Chat with us"}
      </button>

      {open && (
        <div className="v2-panel" data-testid="shopper-v2-panel">
          <div className="v2-panel-head">
            <span className="eyebrow">Assistant - {getConnection()}</span>
            {onSwitchToClassic && (
              <button
                type="button"
                className="v2-switch-classic"
                data-testid="shopper-v2-switch-classic"
                onClick={onSwitchToClassic}
              >
                Switch to classic assistant
              </button>
            )}
          </div>

          <div className="v2-panel-body">
            {latestState && <MissionSidebar state={latestState} />}

            <div className="v2-panel-turns" data-testid="shopper-v2-turns">
              {turns.map((t, i) => (
                <div key={i} className={`v2-turn v2-turn-${t.who}`}>
                  {t.who === "assistant" && <CapabilityBadge capability={t.capability} />}
                  <p>{t.text}</p>
                  {t.products && t.products.length > 0 && (
                    <div className="v2-products">
                      {t.products.map((p) => (
                        <div
                          className="v2-product-card"
                          key={p.product_id}
                          data-testid="shopper-v2-product-card"
                        >
                          <strong>{p.title}</strong>
                          {p.price && <span> - {p.price}</span>}
                          {t.evidenceByProduct?.[p.product_id] && (
                            <ul
                              className="v2-product-why"
                              data-testid="shopper-v2-product-why"
                            >
                              {t.evidenceByProduct[p.product_id].map((reason, ri) => (
                                <li key={ri}>{reason}</li>
                              ))}
                            </ul>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {t.proposalIds?.map((proposalId) => (
                    <div key={proposalId} className="v2-proposal-block">
                      {!decided[proposalId] && (
                        <>
                          <span
                            className="v2-proposal-badge"
                            data-testid="shopper-v2-proposal-badge"
                          >
                            PROPOSED - not yet in your cart
                          </span>
                          <div className="v2-proposal-actions">
                            <button
                              type="button"
                              data-testid="shopper-v2-approve"
                              disabled={busy}
                              onClick={() => approve(proposalId)}
                            >
                              Approve
                            </button>
                            <button
                              type="button"
                              data-testid="shopper-v2-reject"
                              disabled={busy}
                              onClick={() => reject(proposalId)}
                            >
                              Reject
                            </button>
                          </div>
                        </>
                      )}
                      {decided[proposalId] === "accepted" && (
                        <p
                          className="v2-proposal-decided v2-proposal-in-cart"
                          data-testid="shopper-v2-in-cart"
                        >
                          ✓ In your real cart now.
                        </p>
                      )}
                      {decided[proposalId] === "rejected" && (
                        <p className="v2-proposal-decided">Proposal rejected.</p>
                      )}
                    </div>
                  ))}
                  {t.capability === "checkout" && (
                    <p className="v2-checkout-note" data-testid="shopper-v2-checkout-note">
                      This is a prepared total, not a completed payment - payment
                      only ever happens from your own explicit tap on the real
                      checkout screen.
                    </p>
                  )}
                  {t.capability === "payment_recovery" && (
                    <p
                      className="v2-payment-recovery-note"
                      data-testid="shopper-v2-payment-recovery-note"
                    >
                      Nothing gets charged automatically - any payment fix needs
                      someone at the shop to approve it first.
                    </p>
                  )}
                  {t.capability === "order_status" && t.orderId && (
                    <p
                      className="v2-order-status-note"
                      data-testid="shopper-v2-order-status-note"
                    >
                      Order {t.orderId}
                    </p>
                  )}
                  {t.capability === "cart_status" && t.cartItemCount !== undefined && (
                    <p
                      className="v2-cart-status-note"
                      data-testid="shopper-v2-cart-status-note"
                    >
                      {t.cartItemCount === 0
                        ? "Your real cart is empty."
                        : `${t.cartItemCount} item(s) in your real cart.`}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>

          {error && <p className="v2-error">{error}</p>}

          <form
            className="v2-panel-form"
            onSubmit={(e) => {
              e.preventDefault();
              send(draft);
            }}
          >
            <input
              data-testid="shopper-v2-input"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Ask the new assistant..."
              disabled={busy}
            />
            <button type="submit" data-testid="shopper-v2-send" disabled={busy}>
              Send
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
