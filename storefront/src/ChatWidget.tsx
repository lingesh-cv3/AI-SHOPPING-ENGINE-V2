import { useEffect, useRef, useState } from "react";
import { api, shopperMessage, type ChatReply, type ChatTurn } from "./api";

/**
 * The always-on assistant.
 *
 * Controlled rather than self-contained: the parent owns whether it is open and
 * what has been said. That is what lets the storefront start a conversation the
 * shopper did not - when a search finds nothing, the assistant opens itself and
 * explains, instead of leaving them looking at an empty page.
 *
 * Two honesty rules the interface enforces:
 *
 * Products shown are ones the engine actually fetched, never ones the model
 * described. If the model mentions a coffee and the search did not return it, no
 * card appears.
 *
 * When an action needs approval the shopper is told plainly. A cheerful
 * acknowledgement of something that has not happened would be worse than silence.
 */
export function ChatWidget({
  sessionId,
  cartId,
  orderId,
  open,
  turns,
  onOpen,
  onClose,
  onTurns,
  onReply,
  onCartChanged,
  unread,
  merchantName,
  onUnread,
}: {
  sessionId: string;
  cartId?: string;
  orderId?: string;
  open: boolean;
  turns: ChatTurn[];
  onOpen: () => void;
  onClose: () => void;
  onTurns: (next: ChatTurn[]) => void;
  /** Handed the whole reply so the parent can keep the engine panel in step. */
  onReply?: (reply: ChatReply) => void;
  onCartChanged?: () => void;
  /** Assistant messages that arrived while the chat was shut. */
  unread?: number;
  /** The merchant's own name. A coffee roaster's assistant should not introduce
   *  itself as a running shop. */
  merchantName?: string;
  /** Called when messages arrive while the chat is shut, so the parent can badge. */
  onUnread?: (count: number) => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [memory, setMemory] = useState<{ turns: number; friction: number } | null>(
    null,
  );
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  // Poll for turns written by something other than this shopper.
  //
  // When an operator approves a payment recovery, the outcome is written into the
  // session by the engine, not returned as a reply to anything typed here. Without
  // polling, a shopper told "someone will pick this up" would sit there while the
  // money was recovered and never learn it happened.
  //
  // Ten seconds is a compromise: fast enough that an approval feels prompt, slow
  // enough not to hammer the engine for a shopper who has wandered off. A real
  // deployment would use a websocket and drop this entirely.
  useEffect(() => {
    const seen = new Set(turns.map((t) => t.text));

    async function poll() {
      try {
        const { turns: stored } = await api.transcript(sessionId);
        const fresh = stored
          .filter((t) => t.speaker === "assistant" && !seen.has(t.text))
          .map((t) => ({ speaker: "assistant" as const, text: t.text }));

        if (fresh.length > 0) {
          onTurns([...turns, ...fresh]);
          if (!open) onUnread?.(fresh.length);
        }
      } catch {
        // A failed poll is not worth surfacing. The next one may work, and an error
        // about background syncing would only confuse a shopper.
      }
    }

    const id = window.setInterval(poll, 10_000);
    return () => window.clearInterval(id);
  }, [sessionId, turns, open, onTurns, onUnread]);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;

    setDraft("");
    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text }];
    onTurns(withShopper);
    setBusy(true);

    try {
      const r: ChatReply = await api.chat(sessionId, text, { cartId, orderId });
      setMemory({ turns: r.remembered_turns, friction: r.remembered_friction });
      onReply?.(r);
      if (r.cart_changed) onCartChanged?.();
      onTurns([
        ...withShopper,
        {
          speaker: "assistant",
          text: r.reply,
          products: r.products,
          awaitingPerson: r.awaiting_person,
          usedModel: r.used_model,
        },
      ]);
    } catch (e) {
      onTurns([
        ...withShopper,
        { speaker: "assistant", text: shopperMessage(e) },
      ]);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        className={unread ? "chatlaunch nudging" : "chatlaunch"}
        onClick={onOpen}
      >
        {unread ? "Need a hand?" : "Ask us anything"}
        {unread ? <span className="chatbadge">{unread}</span> : null}
      </button>
    );
  }
  return (
    <div className="chatwidget">
      <div className="chathead">
        <div>
          <div className="eyebrow">{merchantName ?? "Assistant"}</div>
          {memory && (memory.friction > 0 || memory.turns > 0) && (
            <div className="chatmem">
              remembering {memory.turns} message{memory.turns === 1 ? "" : "s"}
              {memory.friction > 0 &&
                ` and ${memory.friction} problem${
                  memory.friction === 1 ? "" : "s"
                } from this visit`}
            </div>
          )}
        </div>
        <button className="chatclose" onClick={onClose}>
          ×
        </button>
      </div>

      <div className="chatbody">
        {turns.length === 0 && (
          <p className="chathint">
            Ask about a product, a size, or an order. If something went wrong
            earlier in your visit, I already know about it.
          </p>
        )}

        {turns.map((turn, i) => (
          <div key={i} className={`bubble ${turn.speaker}`}>
            <p className="bubbletext">{turn.text}</p>

            {turn.products && turn.products.length > 0 && (
              <div className="chatproducts">
                {turn.products.slice(0, 4).map((p) => (
                  <div key={p.product_id} className="chatproduct">
                    <span>{p.title}</span>
                    <span className="num">{p.price}</span>
                  </div>
                ))}
              </div>
            )}

            {turn.awaitingPerson && (
              <div className="chatwait">Waiting on someone at the shop</div>
            )}
          </div>
        ))}

        {busy && <div className="bubble assistant thinking">Thinking…</div>}
        <div ref={endRef} />
      </div>

      <form
        className="chatform"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type a message"
          aria-label="Message the assistant"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}