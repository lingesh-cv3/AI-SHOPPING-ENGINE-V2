import { useEffect, useRef, useState } from "react";
import {
  api,
  shopperMessage,
  type ChatReply,
  type ChatReplyV2,
  type ChatTurn,
  type ReturnRiskFinding,
} from "./api";
import { type Account } from "./account";
import {
  HOLD_COPY,
  afterPaymentFailure,
  holdFromPayment,
  holdKey,
  holdOf,
  markHeld,
  recheck,
  useHolds,
} from "./paymentHold";

/** Maps a real `ChatReplyV2` (`/api/v2/chat`, `engine/shopper_v2/`) onto
 *  this widget's existing `ChatTurn` shape - the same shape V1's `/api/chat`
 *  already filled, so every piece of UI below (product cards, the
 *  comparison table, the payment picker, "why this") renders unchanged,
 *  from real V2 data, never a value invented here.
 *
 *  - `products`: real, adapter-fetched products this turn showed, with
 *    `fit` filled from `evidence.products[].evidence_fields` (V2's own
 *    grounded reasons) when present - never left absent when a real reason
 *    exists.
 *  - `comparison`: only when the turn's capability was an actual
 *    comparison and exactly two products came back - the same "exactly two,
 *    fetched fresh" shape V1's own comparison table requires.
 *  - `payment`: only on a real checkout turn, built from
 *    `trace.checkout_payload` - the unmodified `PREPARE_CHECKOUT` dispatch's
 *    own payload (cart id, lines, grand total, the real demo card list) -
 *    never invented, and this widget's existing `payNow()` (the real
 *    `/api/chat/pay` route, unrelated to which engine drove the
 *    conversation) is what actually spends anything, unchanged.
 *  - `awaitingPerson`: true for a payment-recovery turn - that capability
 *    is always financial, so the Risk Gate always classifies it HUMAN;
 *    the real case is already in the ops queue either way.
 *  - A cart proposal this turn created is never rendered inline here - the
 *    existing `ProposalsPanel` (unchanged, already generic over who created
 *    a proposal) is what a shopper approves/rejects it through, exactly as
 *    it already does for any other proposal.
 *  - `choices`: a pending `engine/shopper_v2/fulfillment.py` question
 *    ("what size for the Trailblazer?"), reusing this SAME `choices`
 *    button mechanism V1's own single-item variant picker already renders
 *    (`trace.fulfillment_pending`, built server-side from the real
 *    product's own real buyable variants - never invented here). Tapping
 *    one sends `selected_variant_id` back on the NEXT `/api/v2/chat` call
 *    (see `tapOption`/`act` below) - the tap itself is what adds that one
 *    item directly, per `fulfillment.py`'s own docstring.
 */
export function mapV2Reply(r: ChatReplyV2): {
  products?: ChatTurn["products"];
  comparison?: ChatTurn["comparison"];
  payment?: ChatTurn["payment"];
  awaitingPerson?: boolean;
  why?: ChatTurn["why"];
  choices?: ChatTurn["choices"];
  categoryChoices?: string[];
} {
  const capability = r.trace?.capability as string | undefined;
  const evidenceById = new Map(
    (r.evidence?.products ?? []).map((e) => [e.product_id, e.evidence_fields]),
  );
  const isComparison = capability === "compare" && r.products.length === 2;

  const products = !isComparison && r.products.length > 0
    ? r.products.map((p) => ({
        product_id: p.product_id,
        title: p.title,
        description: p.description,
        price: p.price,
        categories: [],
        fit: evidenceById.get(p.product_id)?.length
          ? evidenceById.get(p.product_id)
          : undefined,
      }))
    : undefined;

  const comparison = isComparison
    ? r.products.map((p) => ({
        product_id: p.product_id,
        title: p.title,
        description: p.description,
        price: p.price,
        availability: p.availability ?? "IN_STOCK",
      }))
    : undefined;

  const checkoutPayload = capability === "checkout"
    ? (r.trace?.checkout_payload as ChatTurn["payment"] | null | undefined)
    : undefined;

  // Conversational-flow review fix (Issue #7): `r.why` is now built
  // server-side (`engine/shopper_v2/evidence.py::build_why_v2`) from this
  // SAME turn's real evidence/shopping-state, the same grounded-only
  // discipline V1's own `engine/api/why.py::explain` already applies -
  // never reconstructed here from raw `evidence_fields` (which used to
  // leak internal-only tags like "title_or_description_overlap" straight
  // into the panel, and never distinguished "found" from "declined").
  const why = r.why ?? undefined;

  const fulfillmentPending = r.trace?.fulfillment_pending as
    | {
        product_id: string;
        product_title: string;
        dimension_label: string;
        options: { variant_id: string; label: string; left: number | null }[];
      }
    | null
    | undefined;
  // A typed approval of a proposal whose product still needs a size/option: the
  // same accept-flow buttons the Suggestions panel shows (the shopper's tap adds it).
  const acceptChoices = r.trace?.accept_choices as
    | { variant_id: string; label: string; product_id: string; product_title: string; left: number | null }[]
    | undefined;
  const choices = acceptChoices?.length
    ? acceptChoices.map((c) => ({
        variant_id: c.variant_id,
        label: c.label,
        product_id: c.product_id,
        product_title: c.product_title,
        left: c.left ?? null,
        source: "accept_flow" as const,
      }))
    : fulfillmentPending?.options?.length
    ? fulfillmentPending.options.map((o) => ({
        variant_id: o.variant_id,
        label: o.label,
        product_id: fulfillmentPending.product_id,
        product_title: fulfillmentPending.product_title,
        left: o.left,
        source: "fulfillment" as const,
      }))
    : undefined;

  // Store-level discovery ("what do you have?"): the merchant's own departments, each verified server-side to hold
  // something buyable (`engine/shopper_v2/store_discovery.py`). Rendered by the existing category chips, whose tap
  // sends "Show me <category>" - nothing is invented here.
  const categoryChoices = Array.isArray(r.trace?.category_choices) && (r.trace.category_choices as string[]).length > 0
    ? (r.trace.category_choices as string[])
    : undefined;

  return {
    products,
    comparison,
    categoryChoices,
    payment: checkoutPayload ?? undefined,
    awaitingPerson: capability === "payment_recovery" ? true : undefined,
    why,
    choices,
  };
}

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
  depts,
}: {
  sessionId: string;
  cartId?: string;
  /** Accepted for interface compatibility with existing callers - no longer
   *  read internally. V1's `act()` forwarded this to `/api/chat` as order
   *  context; V2's order-status capability instead resolves "the order
   *  from this visit" itself, from the same session-turn history this
   *  widget already restores/polls (`engine/session/store.py`,
   *  unmodified), so a shopper can still just ask "what did I order?"
   *  without this prop. */
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
  /** Real, merchant-declared departments (already fetched by the storefront
   *  for its own nav) - shown as tappable buttons alongside the opening
   *  greeting, on a session with no turns yet. Never invented here. */
  depts?: string[];
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // The card a shopper has selected but not yet confirmed. Every other tap
  // in this widget is reversible; this one is not, so it asks twice.
  const [confirming, setConfirming] = useState<string | null>(null);
  // Baskets whose payment is held (outcome unknown / going through / being checked). A payment picker that is still on
  // screen for one of them - an old turn, a stale tab - shows why it is off instead of offering a second payment.
  const holds = useHolds();
  const heldCart = (id?: string | null) => (id ? holds[holdKey(id)] : undefined);
  // Which message has its reasoning open. One at a time: a shopper checking
  // why is checking one thing, and leaving several expanded turns the
  // conversation into a log.
  const [openWhy, setOpenWhy] = useState<number | null>(null);
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
            // Restored from V1's own transcript storage - the ONLY
            // mechanism that ever writes choices there is the accept-flow
            // variant check (see `tap()`'s own comment on why this tag
            // exists); V2's fulfillment_pending never persists here.
            choices: t.choices.length > 0
              ? t.choices.map((ch) => ({ ...ch, source: "accept_flow" as const }))
              : undefined,
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

  /** Send a free-text message on the shopper's behalf. A tap (product card,
   *  size option) never comes through here - see `tap()` below, which goes
   *  straight to trusted execution, no model involved. */
  async function act(said: string) {
    if (busy) return;

    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text: said }];
    onTurns(withShopper);
    setBusy("sending");

    try {
      // Powered by /api/v2/chat (engine/shopper_v2/) - `known` (a tapped
      // reference's real id) has no V2 equivalent parameter; every tap in
      // this widget already goes through `tap()`/`api.act` below instead,
      // never through here, so this is never actually needed for a real
      // shopper action - see the `known`-carrying callers' own comments.
      const r: ChatReplyV2 = await api.chatV2(sessionId, said, cartId);
      const mapped = mapV2Reply(r);

      // A V2 chat turn never mutates the cart on its own authority - it creates
      // proposals. The one exception is the shopper's own typed approval of a
      // pending proposal ("yes"), which runs the same accept path as the Accept
      // button; when that changed the cart, refresh it like the button does.
      if (r.trace?.cart_changed) onCartChanged?.();
      onTurns([
        ...withShopper,
        {
          speaker: "assistant",
          text: r.reply,
          products: mapped.products,
          comparison: mapped.comparison,
          awaitingPerson: mapped.awaitingPerson,
          usedModel: r.used_model,
          payment: mapped.payment,
          why: mapped.why,
          choices: mapped.choices,
          categoryChoices: mapped.categoryChoices,
        },
      ]);
    } catch (e) {
      onTurns([...withShopper, { speaker: "assistant", text: shopperMessage(e) }]);
    } finally {
      setBusy(null);
    }
  }

  /** Answers a pending `engine/shopper_v2/fulfillment.py` question - "what
   *  size for the Trailblazer?" - with a tapped option. Goes straight to
   *  `/api/v2/chat` with `selected_variant_id` set, the SAME trusted-tap
   *  path `fulfillment.py::_match_answer` checks against that item's own
   *  real options before adding anything - never through the model, and
   *  never through V1's `api.act`/`tap()` (unreachable from this widget;
   *  see `App.tsx`'s own routing - V2 is the only path here). The reply
   *  may itself carry the NEXT product's own choices (fulfillment.py
   *  chains through every item in one turn until one needs an answer),
   *  so this keeps the same "choices in, choices out" shape `act()` uses.
   */
  async function tapFulfillmentChoice(c: {
    variant_id: string;
    label: string;
    product_id: string;
    product_title: string;
  }) {
    if (busy) return;
    const said = `The ${c.label}, for the ${c.product_title}`;
    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text: said }];
    onTurns(withShopper);
    setBusy("tapping");

    try {
      const r = await api.chatV2(sessionId, said, cartId, undefined, c.variant_id);
      const mapped = mapV2Reply(r);
      onTurns([
        ...withShopper,
        {
          speaker: "assistant",
          text: r.reply,
          products: mapped.products,
          usedModel: r.used_model,
          why: mapped.why,
          choices: mapped.choices,
        },
      ]);
      // A `fulfillment.py` tap can add a real line to the cart directly
      // (see that module's own docstring) - the cart panel needs to
      // refresh even though nothing here went through a proposal.
      onCartChanged?.();
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
      const reply: ChatTurn = {
        speaker: "assistant",
        text: r.reply,
        awaitingPerson: r.awaiting_person,
        // The real, live accept-flow variant check (`/api/chat/act` /
        // `/api/chat/proposals/{id}/accept`) - see `ChatTurn.choices`'s
        // own docstring for why this tag must always be set explicitly.
        choices: r.choices?.map((ch) => ({ ...ch, source: "accept_flow" as const })),
        payment: r.payment,
        why: r.why,
      };
      onTurns([...withShopper, reply]);

      // A guided mission moves on once the REAL cart holds the product. A tap
      // is not a chat turn, so ask the engine to check the cart now; it
      // answers with the next stage (or nothing when there is no mission).
      // Optional: the add itself already succeeded either way.
      if (r.cart_changed && cartId) {
        try {
          const adv = await api.advanceMission(sessionId, cartId);
          if (adv.reply) {
            onTurns([
              ...withShopper,
              reply,
              {
                speaker: "assistant",
                text: adv.reply,
                products: adv.products.length
                  ? adv.products.map((p) => ({
                      product_id: p.product_id,
                      title: p.title,
                      description: p.description,
                      price: p.price,
                      categories: [],
                    }))
                  : undefined,
              },
            ]);
          }
        } catch {
          /* the mission follow-up is best-effort */
        }
      }
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
    // Not just a disabled button: a stale handler must not pay a held basket either.
    if (holdOf(cartId)) return;
    setConfirming(null);
    setBusy("paying");

    const said = `Pay with the card ending ${cardLast4}`;
    const withShopper: ChatTurn[] = [...turns, { speaker: "shopper", text: said }];
    onTurns(withShopper);

    try {
      const r = await api.pay(sessionId, cartId, cardLast4);
      const held = holdFromPayment(r.payment);
      if (held) markHeld(cartId, held);
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
      // The request may have been processed even though no clean answer came back: fail closed and ask the server.
      void afterPaymentFailure(cartId, e);
      onTurns([...withShopper, { speaker: "assistant", text: shopperMessage(e) }]);
    } finally {
      setBusy(null);
    }
  }

  function tapProduct(p: { product_id: string; title: string }) {
    return tap(p.product_id, `Add the ${p.title}`);
  }

  /** The real, live accept-flow variant answer - a shopper picking a
   *  size/option for a cart proposal that turned out to need one
   *  (`/api/chat/act` -> `chosen_variant`). Restored 2026-09: this used
   *  to be the only handler `turn.choices`' button ever called; it was
   *  briefly replaced outright by `tapFulfillmentChoice` when that
   *  mechanism was added, which broke this real, still-live flow. Both
   *  now coexist, dispatched by the choice's own `source` tag - see
   *  `ChatTurn.choices`'s docstring. */
  function tapAcceptFlowChoice(c: {
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
        </div>
        <button className="chatclose" onClick={onClose}>
          ×
        </button>
      </div>

      <div className="chatbody">
        {turns.length === 0 && (
          <>
            <p className="chathint">
              Hi! Ask about a product, a size, or an order - if something
              went wrong earlier in your visit, I already know about it.
            </p>
            {/* Real, merchant-declared departments (the same list the
                storefront's own nav already fetched) - not invented
                categories, and reuses the exact `act("Show me X")` path
                the mid-conversation category buttons already use below. */}
            {depts && depts.length > 0 && (
              <div className="optionrow">
                {depts.slice(0, 6).map((d) => (
                  <button
                    key={d}
                    className="optionbtn"
                    disabled={busy !== null}
                    onClick={() => act(`Show me ${d}`)}
                  >
                    {d}
                  </button>
                ))}
              </div>
            )}
          </>
        )}

        {turns.map((turn, i) => (
          <div key={i} className={`bubble ${turn.speaker}`}>
            <p className="bubbletext">{turn.text}</p>

            {/* Products the engine actually fetched, never ones the model merely
                described. Tappable, because reading a name and then typing it back
                is work the shopper should not have to do.
                Root-cause fix (2026-09): this used to hard-cap at 4 cards
                regardless of how many the engine actually returned - a second,
                unrelated display-count decision living here, independent of
                (and silently overriding) the backend's own real count
                (`engine/shopper_v2/graph.py::_apply_query_intent`, the single
                source of truth for "how many"). The backend is now the ONLY
                place that decides a count ("show me two" -> 2, "show me all" ->
                every real match, no count stated -> its own honest default) -
                this renders exactly what it was sent, never a second opinion. */}
            {turn.products && turn.products.length > 0 && (
              <div className="chatproducts">
                {turn.products.map((p) => (
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
                    {p.return_risk && p.return_risk.length > 0 && (
                      <ReturnRiskNotice findings={p.return_risk} />
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
                    {turn.comparison.some(
                      (p) => p.return_risk && p.return_risk.length > 0,
                    ) && (
                      <tr>
                        <th scope="row">Return risk</th>
                        {turn.comparison.map((p) => (
                          <td key={p.product_id}>
                            {p.return_risk && p.return_risk.length > 0 ? (
                              <ReturnRiskNotice findings={p.return_risk} />
                            ) : (
                              <span className="cpreturnrisk-none">
                                Nothing flagged
                              </span>
                            )}
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
                    onClick={() =>
                      ch.source === "fulfillment"
                        ? tapFulfillmentChoice(ch)
                        : tapAcceptFlowChoice(ch)
                    }
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

                {heldCart(turn.payment.cart_id) && (
                  <div className="payhold" role="status" data-testid="chat-payment-hold">
                    <p className="paynote">
                      <strong>{HOLD_COPY[heldCart(turn.payment.cart_id)!].label}.</strong>{" "}
                      {HOLD_COPY[heldCart(turn.payment.cart_id)!].note}
                    </p>
                    {heldCart(turn.payment.cart_id) !== "checking" && (
                      <button
                        className="navlink"
                        data-testid="chat-payment-hold-recheck"
                        onClick={() => void recheck(turn.payment!.cart_id!)}
                      >
                        Check again
                      </button>
                    )}
                  </div>
                )}

                {confirming && !heldCart(turn.payment.cart_id) ? (
                  <div className="payconfirm">
                    <p className="paynote">
                      Charge {turn.payment.grand_total} to the card ending{" "}
                      {confirming}?
                    </p>
                    <div className="payactions">
                      <button
                        className="add"
                        disabled={busy !== null || !!heldCart(turn.payment.cart_id)}
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
                        disabled={
                          busy !== null ||
                          i !== turns.length - 1 ||
                          !!heldCart(turn.payment!.cart_id)
                        }
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

/** Renders engine/execution/return_risk.py's findings for one product - a
 *  real, evidence-grounded pre-purchase friction signal, never a claim
 *  about actual return history (no such data exists anywhere in this
 *  system). Advisory only: it never removes the product from the list
 *  above it or blocks the "Buy Now"/tap action next to it, and it
 *  distinguishes what the platform actually declared (a real rating) from
 *  what the engine merely could not confirm (a stated requirement with no
 *  matching declared attribute) rather than presenting both the same way. */
function ReturnRiskNotice({ findings }: { findings: ReturnRiskFinding[] }) {
  return (
    <span className="cpreturnrisk">
      {findings.map((f, i) => (
        <span
          key={i}
          className={
            f.evidence_type === "known_fact"
              ? "cpreturnrisk-fact"
              : "cpreturnrisk-inferred"
          }
        >
          <span className="cpreturnrisk-label">
            {f.evidence_type === "known_fact" ? "Known: " : "Unconfirmed: "}
          </span>
          {f.message}
        </span>
      ))}
    </span>
  );
}