import { useEffect, useRef, useState } from "react";
import { api, shopperMessage, type ChatReply, type ChatTurn } from "./api";
import { type Account } from "./account";

/**
 * The always-on assistant.
 *
 * Controlled rather than self-contained: the parent owns whether it is open and
 * what has been said. That is what lets the storefront start a conversation the
 * shopper did not - when a search finds nothing, the assistant opens itself.
 *
 * Everything tappable goes through one path. A product card and a size button are
 * the same gesture, and both send an id alongside the shopper's words. The words
 * make the transcript read naturally; the id is what execution acts on. That split
 * is why tapping is unambiguous where typing is not - "whole bean" fits two of
 * Kettle's three options, and a tap fits one.
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
  onCartRetired,
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
  onReply?: (reply: ChatReply) => void;
  onCartChanged?: () => void;
  account?: Account | null;
  onAccount?: (next: Account | null) => void;
  /** Called after a successful payment. The cart is finished and a new one
   *  is needed - the same thing the sidebar checkout does. */
  onCartRetired?: () => void;
  unread?: number;
  merchantName?: string;
  onUnread?: (count: number) => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // The card a shopper has selected but not yet confirmed. Every other tap
  // in this widget is reversible; this one is not, so it asks twice.
  const [confirming, setConfirming] = useState<string | null>(null);
  // Which message has its reasoning open. One at a time: a shopper checking
  // why is checking one thing, and leaving several expanded turns the
  // conversation into a log.
  const [openWhy, setOpenWhy] = useState<number | null>(null);
  const [memory, setMemory] = useState<{ turns: number; friction: number } | null>(
    null,
  );
  const endRef = useRef<HTMLDivElement>(null);

  // Always the latest `turns`/`open`/`onTurns`/`onUnread`, for the poll below
  // to read at the moment it actually resolves - not whatever they were when
  // the interval was set up. See the poll effect's own comment for why this
  // exists. `onTurns` and `onUnread` need this too, not just `turns`/`open`:
  // `onTurns` is `setChatTurns` (stable), but `onUnread` is passed from
  // App.tsx as a new inline arrow function on every render
  // (`onUnread={(n) => setUnread((prev) => prev + n)}`) - putting it in the
  // poll effect's own dependency array would rebuild that effect on nearly
  // every render of the parent, for reasons having nothing to do with this
  // widget, which is the same "effect depends on something that changes too
  // often" shape this whole fix exists to remove.
  const turnsRef = useRef(turns);
  useEffect(() => {
    turnsRef.current = turns;
  }, [turns]);
  const openRef = useRef(open);
  useEffect(() => {
    openRef.current = open;
  }, [open]);
  const onTurnsRef = useRef(onTurns);
  useEffect(() => {
    onTurnsRef.current = onTurns;
  }, [onTurns]);
  const onUnreadRef = useRef(onUnread);
  useEffect(() => {
    onUnreadRef.current = onUnread;
  }, [onUnread]);
  // The session the poll below is actually running for, read at resolution
  // time so an in-flight fetch started before a merchant/session switch
  // cannot apply its result after the switch. Without this, a poll for the
  // old session that resolves after `sessionId` has already changed would
  // append the old session's turns onto the new session's transcript -
  // `turnsRef`/`onTurnsRef` are correct "current", but "current" had by
  // then become a different conversation.
  const currentSessionRef = useRef(sessionId);
  useEffect(() => {
    currentSessionRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  // Load the conversation once, on mount.
  //
  // Separate from the poll below, and that separation is the point. Polling takes
  // assistant turns only, because a shopper's own messages are already on screen
  // the moment they send them and echoing them back would duplicate every line.
  // After a reload nothing is on screen, so that same filter dropped exactly the
  // half that was missing - the transcript came back as the assistant talking to
  // itself.
  //
  // Only when there is nothing yet. A shopper who closes and reopens the panel
  // mid-visit should not have their conversation rebuilt underneath them.
  // Which session we have already restored.
  //
  // A boolean was not enough once each merchant kept its own conversation:
  // switching back changes the session id, and a one-shot flag meant the second
  // one never loaded. Tracking the id restores each thread exactly once.
  const restoreFor = useRef<string | null>(null);
  useEffect(() => {
    if (restoreFor.current === sessionId) return;
    restoreFor.current = sessionId;

    api
      .transcript(sessionId)
      .then(({ turns: stored }) => {
        // Replaces rather than merges, and runs even when the result is empty.
        //
        // Returning early on an empty result left the previous merchant's
        // conversation on screen after a switch, which is worse than showing
        // nothing: a shopper on Northfield reading their coffee order.
        if (stored.length === 0) {
          onTurns([]);
          return;
        }
        // Both speakers. The poll filters to assistant turns because a shopper's
        // own messages are already on screen when they send them; on a restore
        // nothing is on screen, and reusing that filter made the conversation read
        // as the assistant talking to itself.
        //
        // Choices, products, comparison and payment are all carried over too.
        // Without them a reload mid-choice, mid-browse, mid-compare or
        // mid-pay restored the sentence but not what it was offering - the
        // offer was still live, only the way to act on it was gone.
        onTurns(
          stored.map((t) => ({
            speaker: t.speaker,
            text: t.text,
            choices: t.choices.length > 0 ? t.choices : undefined,
            products: t.products.length > 0 ? t.products : undefined,
            comparison: t.comparison.length > 0 ? t.comparison : undefined,
            payment: t.payment ?? undefined,
            categoryChoices:
              t.category_choices.length > 0 ? t.category_choices : undefined,
          })),
        );
      })
      .catch(() => {
        // Nothing to show is better than an error about restoring history the
        // shopper never asked to have restored.
      });
    // Runs once. Depending on `turns` would re-trigger on every message.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Poll for turns written by something other than this shopper.
  //
  // When an operator approves a payment recovery, the outcome is written into the
  // session by the engine, not returned as a reply to anything typed here. Without
  // polling, a shopper told "someone will pick this up" would sit there while the
  // money was recovered and never learn it happened.
  useEffect(() => {
    async function poll() {
      try {
        const { turns: stored } = await api.transcript(sessionId);
        // This poll was set up for `sessionId` (this effect's own closed-over
        // value, fixed for its lifetime). If the shopper has since switched
        // merchants/sessions, `currentSessionRef` now holds a different id -
        // the fetch above was already in flight when that happened, so its
        // result belongs to a conversation that is no longer on screen.
        // Applying it anyway would append the old session's turns onto the
        // new session's transcript. Discard rather than apply.
        if (sessionId !== currentSessionRef.current) return;
        // `seen` is read from turnsRef *here*, at the moment the network
        // response actually lands - not captured once when the interval was
        // set up. The old code built `seen` (and the base to append onto)
        // from the `turns` closed over when this effect last ran, and this
        // effect re-ran on every single message (it depended on `turns`).
        // A poll that was already in flight when the shopper's next message
        // landed - a real, reachable window: a card decline or a checkout
        // both take real network/model time - resolved holding a `turns`
        // array from *before* that message and its reply. Calling
        // `onTurns([...staleTurns, ...fresh])` then overwrote the screen
        // with that stale base plus whatever the poll found "fresh" (often
        // the very reply that had just been handled directly), silently
        // discarding the shopper's newer message and its real answer - a
        // question answered correctly by the direct response, then clobbered
        // back to a duplicate of the turn before it. Reading both `seen` and
        // the append base from a ref that a separate effect keeps current on
        // every render closes that window: whatever the poll finds is always
        // compared and appended against what is actually on screen right now.
        const seen = new Set(turnsRef.current.map((t) => t.text));
        // Matched on a prefix as well as the whole string. The pay endpoint
        // prepends a sentence to what the pipeline said, so the stored turn is a
        // suffix of what is already on screen - identical in substance, different
        // in bytes, and the naive check let it through as a new message.
        const fresh = stored
          .filter(
            (t) =>
              t.speaker === "assistant" &&
              !seen.has(t.text) &&
              ![...seen].some((shown) => shown.endsWith(t.text)),
          )
          .map((t) => ({ speaker: "assistant" as const, text: t.text }));

        if (fresh.length > 0) {
          onTurnsRef.current([...turnsRef.current, ...fresh]);
          if (!openRef.current) onUnreadRef.current?.(fresh.length);
        }
      } catch {
        // A failed poll is not worth surfacing. The next one may work.
      }
    }

    const id = window.setInterval(poll, 10_000);
    return () => window.clearInterval(id);
    // Deliberately depending on nothing but `sessionId` - that was the bug.
    // `turns`/`open` changed on every message; `onTurns`/`onUnread` are
    // props, and `onUnread` in particular is a new function identity on
    // every parent render. Any of those in this array meant tearing the
    // interval down and rebuilding it (with a fresh `seen` snapshot)
    // constantly instead of once per session. Current values are read
    // through the refs above instead.
  }, [sessionId]);

  /** Send a message on the shopper's behalf, with whatever they picked.
   *
   *  `known` carries ids the engine trusts outright, so a tap never depends on
   *  matching text. Used by the composer, the option buttons and the product cards
   *  alike - one path, so they cannot drift apart.
   */
  async function act(said: string, known?: Record<string, string>) {
    if (busy) return;

    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text: said }];
    onTurns(withShopper);
    setBusy("sending");

    try {
      const r: ChatReply = await api.chat(sessionId, said, {
        cartId,
        orderId,
        known,
      });
      setMemory({ turns: r.remembered_turns, friction: r.remembered_friction });
      onReply?.(r);

      // Put their message back. The model was never reached, so nothing was
      // answered - and retyping a sentence into a limit that has not lifted is
      // what makes this feel broken rather than busy.
      if (r.rate_limited) {
        setDraft(said);
      }
      if (r.cart_changed) onCartChanged?.();
      onTurns([
        ...withShopper,
        {
          speaker: "assistant",
          text: r.reply,
          products: r.products,
          comparison: r.comparison,
          awaitingPerson: r.awaiting_person,
          usedModel: r.used_model,
          choices: r.choices,
          payment: r.payment,
          why: r.why,
          categoryChoices: r.category_choices,
        },
      ]);
    } catch (e) {
      onTurns([...withShopper, { speaker: "assistant", text: shopperMessage(e) }]);
    } finally {
      setBusy(null);
    }
  }

  /** A tap goes straight to execution, never to the model.
   *
   *  Both handlers below share this. It records what the shopper did in the
   *  transcript and returns whatever came back - options to pick from, or a filled
   *  cart. No thinking pause, no tokens, and it keeps working while the model is
   *  rate limited, because the model is not involved.
   */
  async function tap(productId: string, said: string, variantId?: string) {
    if (busy) return;

    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text: said }];
    onTurns(withShopper);
    setBusy("tapping");

    try {
      const r = await api.act(sessionId, productId, said, variantId, cartId);
      onReply?.(r);
      if (r.cart_changed) onCartChanged?.();
      onTurns([
        ...withShopper,
        {
          speaker: "assistant",
          text: r.reply,
          awaitingPerson: r.awaiting_person,
          choices: r.choices,
          payment: r.payment,
          why: r.why,
        },
      ]);
    } catch (e) {
      onTurns([...withShopper, { speaker: "assistant", text: shopperMessage(e) }]);
    } finally {
      setBusy(null);
    }
  }


  /** Pay. Kept apart from tap() because this one spends money.
   *
   *  Sharing a handler with the reversible taps would have been tidier and would
   *  have meant one careless edit could make a product card charge a card.
   */
  async function payNow(cartId: string, cardLast4: string) {
    if (busy) return;
    setConfirming(null);
    setBusy("paying");

    const said = `Pay with the card ending ${cardLast4}`;
    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text: said }];
    onTurns(withShopper);

    try {
      const r = await api.pay(sessionId, cartId, cardLast4);
      onReply?.(r);
      if (r.payment?.cart_retired) {
        onCartRetired?.();
      } else {
        // A declined card leaves the cart intact, so it only needs re-reading.
        onCartChanged?.();
      }
      onTurns([
        ...withShopper,
        { speaker: "assistant", text: r.reply, payment: r.payment },
      ]);
    } catch (e) {
      onTurns([...withShopper, { speaker: "assistant", text: shopperMessage(e) }]);
    } finally {
      setBusy(null);
    }
  }

  function tapProduct(p: { product_id: string; title: string }) {
    return tap(p.product_id, `Add the ${p.title}`);
  }

  /** The option carries its own product id, set when the engine offered it. Relying
   *  on parsing it out of the variant id would break on any platform using opaque
   *  ids, and Kettle's happen to be readable only by accident. */
  function tapOption(c: {
    variant_id: string;
    label: string;
    product_id: string;
    product_title: string;
  }) {
    // A real sentence, not the bare label. "8" recorded as the shopper's own
    // words read as if they had typed just that - the product it answered for
    // was gone from the transcript entirely.
    return tap(c.product_id, `The ${c.label}, for the ${c.product_title}`, c.variant_id);
  }

  function send() {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    return act(text);
  }

  // Restoring the draft is handled inside act(), because only the reply knows
  // whether the model was reached.

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

            {/* Products the engine actually fetched, never ones the model merely
                described. Tappable, because reading a name and then typing it back
                is work the shopper should not have to do. */}
            {turn.products && turn.products.length > 0 && (
              <div className="chatproducts">
                {turn.products.slice(0, 4).map((p) => (
                  <button
                    key={p.product_id}
                    className="chatproduct tappable"
                    disabled={busy !== null}
                    onClick={() => tapProduct(p)}
                  >
                    <span className="cptitle">
                      <span>{p.title}</span>
                      <span className="num">{p.price}</span>
                    </span>
                    {p.description && (
                      <span className="cpdesc">{p.description}</span>
                    )}
                  </button>
                ))}
              </div>
            )}

            {/* Exactly two products, fetched fresh - never the model's memory
                of them. A table row per fact, so a shopper reads what
                differs at a glance instead of re-reading two card
                paragraphs to find it, and each column ends in its own
                Buy Now - the point of comparing is to end able to act on
                it, not to leave with a decision and no next step. */}
            {turn.comparison && turn.comparison.length === 2 && (
              <div className="comparetable-wrap">
                <table className="comparetable">
                  <thead>
                    <tr>
                      <th scope="col" className="comparetable-rowlabel" />
                      {turn.comparison.map((p) => (
                        <th scope="col" key={p.product_id}>
                          {p.title}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">Price</th>
                      {turn.comparison.map((p) => (
                        <td key={p.product_id} className="num">
                          {p.price}
                        </td>
                      ))}
                    </tr>
                    <tr>
                      <th scope="row">Stock</th>
                      {turn.comparison.map((p) => (
                        <td key={p.product_id}>
                          <span
                            className={
                              p.availability === "OUT_OF_STOCK"
                                ? "comparestock out"
                                : "comparestock"
                            }
                          >
                            {p.availability === "OUT_OF_STOCK"
                              ? "Out of stock"
                              : "In stock"}
                          </span>
                        </td>
                      ))}
                    </tr>
                    {(turn.comparison[0].description ||
                      turn.comparison[1].description) && (
                      <tr>
                        <th scope="row">Details</th>
                        {turn.comparison.map((p) => (
                          <td key={p.product_id} className="cpdesc">
                            {p.description}
                          </td>
                        ))}
                      </tr>
                    )}
                    <tr>
                      <th scope="row" />
                      {turn.comparison.map((p) => (
                        <td key={p.product_id}>
                          <button
                            className="cpbuy"
                            disabled={
                              busy !== null || p.availability === "OUT_OF_STOCK"
                            }
                            onClick={() => tapProduct(p)}
                          >
                            Buy now
                          </button>
                        </td>
                      ))}
                    </tr>
                  </tbody>
                </table>
              </div>
            )}

            {turn.choices && turn.choices.length > 0 && (
              <div className="optionrow">
                {turn.choices.map((ch) => (
                  <button
                    key={ch.variant_id}
                    className="optionbtn"
                    // Only the most recent offer is live. Every row
                    // stayed tappable before, so a shopper could scroll
                    // up and pick a size for a product they had moved on
                    // from - and the conversation and the cart would then
                    // disagree. A stale offer is history, not a control.
                    disabled={busy !== null || i !== turns.length - 1}
                    onClick={() => tapOption(ch)}
                  >
                    {ch.label}
                    {ch.left !== null && (
                      <span className="optionleft">{ch.left} left</span>
                    )}
                  </button>
                ))}
              </div>
            )}

            {/* A general "what's available" is answered with the shop's real
                categories rather than an arbitrary six products - tapping one
                asks the same question again, narrowed. */}
            {turn.categoryChoices && turn.categoryChoices.length > 0 && (
              <div className="optionrow">
                {turn.categoryChoices.map((cat) => (
                  <button
                    key={cat}
                    className="optionbtn"
                    disabled={busy !== null || i !== turns.length - 1}
                    onClick={() => act(`Show me ${cat}`)}
                  >
                    {cat}
                  </button>
                ))}
              </div>
            )}

            {turn.payment?.cards && turn.payment.cards.length > 0 && (
              <div className="payblock">
                <div className="paytotal">
                  <span>{turn.payment.item_count} item(s)</span>
                  <span className="num">{turn.payment.grand_total}</span>
                </div>

                {turn.payment.lines?.map((line) => (
                  <div key={line.title} className="payline">
                    <span>
                      {line.quantity} &times; {line.title}
                    </span>
                    <span className="num">{line.total}</span>
                  </div>
                ))}

                {confirming ? (
                  <div className="payconfirm">
                    <p className="paynote">
                      Charge {turn.payment.grand_total} to the card ending{" "}
                      {confirming}?
                    </p>
                    <div className="payactions">
                      <button
                        className="add"
                        disabled={busy !== null}
                        onClick={() =>
                          payNow(turn.payment!.cart_id!, confirming)
                        }
                      >
                        {busy ? "Paying..." : "Yes, pay now"}
                      </button>
                      <button
                        className="reject"
                        onClick={() => setConfirming(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="paycards">
                    {turn.payment.cards.map((card) => (
                      <button
                        key={card.last4}
                        className="paycard"
                        disabled={busy !== null || i !== turns.length - 1}
                        onClick={() => setConfirming(card.last4)}
                      >
                        {card.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}


            {turn.why &&
              (turn.why.found.length > 0 || turn.why.declined.length > 0) && (
                <div className="whyblock">
                  <button
                    className="whytoggle"
                    aria-expanded={openWhy === i}
                    onClick={() => setOpenWhy(openWhy === i ? null : i)}
                  >
                    {openWhy === i ? "Hide" : "Why this?"}
                  </button>

                  {openWhy === i && (
                    <div className="whybody">
                      {turn.why.found.map((line) => (
                        <p key={line} className="whyline">
                          {line}
                        </p>
                      ))}

                      {turn.why.evidence.length > 0 && (
                        <ul className="whyevidence">
                          {turn.why.evidence.map((e) => (
                            <li key={e}>{e}</li>
                          ))}
                        </ul>
                      )}

                      {turn.why.declined.length > 0 && (
                        <div className="whydeclined">
                          <span className="whylabel">I did not offer</span>
                          {turn.why.declined.map((d) => (
                            <div key={d} className="whydeclinedline">
                              {d}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
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
          disabled={busy !== null}
        />
        <button type="submit" disabled={busy !== null || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}