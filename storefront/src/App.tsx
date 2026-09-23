import { useCallback, useEffect, useRef, useState } from "react";
import { AccountMenu } from "./AccountMenu";
import {
  api,
  getConnection,
  shopperMessage,
  type Cart,
  type ChatReply,
  type ChatTurn,
  type CheckoutResult,
  type Connection,
  type Pipeline,
  type Product,
  pathFor
} from "./api";


import { OpsConsole } from "./OpsConsole";
import { CartPanel } from "./CartPanel";
import { ProposalsPanel } from "./ProposalsPanel";
import { ChatWidget, mapV2Reply } from "./ChatWidget";
import { Gates } from "./Gates";
import { MerchantConsole } from "./MerchantConsole";
import { OrderView } from "./OrderView";
import { ProductCard } from "./ProductCard";
import { ProductDetail } from "./ProductDetail";
import { Hero } from "./Hero";
import { themeFor } from "./theme";
import { type Account, claimCart, setEmail, whoAmI } from "./account";

/** Reshape a chat reply into the engine panel's Pipeline view.
 *
 *  The chat endpoint returns the whole trace, so the demo panel costs no extra
 *  model call - which matters on a per-minute token budget.
 */
function fromChat(r: ChatReply, friction: string | null): Pipeline {
  return {
    friction,
    proposed: r.proposed,
    rejected: r.rejected,
    selected_action: r.selected_action ?? "NO_ACTION",
    selection_reason: r.selection_reason ?? "",
    escalated_because_empty: r.escalated_because_empty,
    risk_outcome: r.risk_outcome ?? "HUMAN",
    risk_rule: r.risk_rule ?? "",
    risk_reason: r.risk_reason ?? "",
    financial: r.financial,
    reversible: true,
    used_model: r.used_model,
    model_name: r.model_name,
    diagnosis: r.diagnosis,
    evidence: r.evidence,
    reply: r.model_reply,
    shopper_reply: r.reply,
    fallback_reason: null,
    prompt_tokens: r.prompt_tokens,
    completion_tokens: r.completion_tokens,
  };
}

/**
 * Northfield Running Co. and Kettle & Bloom - two test clients' storefronts, with
 * the CV3 engine running underneath both.
 *
 * The engine panel is diagnostic output and sits behind a toggle, off by default.
 * A shopper should never see pipeline stages or rule names; when a search finds
 * nothing, they get an assistant that explains, not a trace.
 */
/** The conversation id for one merchant, created on first visit and kept.
 *
 *  Keyed by connection, exactly like the cart. Each merchant keeps its own thread,
 *  nothing leaks between them, and coming back to a shop finds where you left off.
 *  Switching used to discard the conversation entirely, which left the odd state
 *  where the cart remembered and the chat did not.
 */
function sessionFor(connectionId: string): string {
  const key = `cv3_session_${connectionId}`;
  const existing = sessionStorage.getItem(key);
  if (existing) return existing;
  // crypto.randomUUID(), not Math.random(): a session id used to be guessable
  // in principle, and combined with the (now-fixed) claim-on-first-GET
  // behaviour on the transcript route, a predicted id could be pre-claimed
  // before the real shopper's first message. Every evergreen browser this
  // storefront targets implements crypto.randomUUID() natively.
  const fresh = `sess_${crypto.randomUUID().replace(/-/g, "")}`;
  sessionStorage.setItem(key, fresh);
  return fresh;
}

export default function App() {
  const [view, setView] = useState<
    "shop" | "console" | "queue" | "product" | "order"
  >("shop");
  const [openProduct, setOpenProduct] = useState<string | null>(null);
  const [openOrder, setOpenOrder] = useState<string | null>(null);


  const [connections, setConnections] = useState<Connection[]>([]);
  const [connection] = useState(getConnection());

  // One session id for the visit, shared by the friction path and the chat. This is
  // the mechanism behind shared memory - both report against it, so the assistant
  // knows about a declined payment nobody mentioned.
  // Read from storage rather than held in state.
  //
  // This was useState, initialised at mount from whichever merchant loaded
  // first. The first switch then lost the chat and every switch afterwards
  // worked - because setSessionId had not applied when the message went out,
  // and after that the two agreed.
  //
  // Third bug today from two places holding one value. Storage is the source
  // now: it cannot be stale because there is nothing to be stale against, and
  // the first switch behaves like every other one because nothing has to
  // catch up.
  // Who is signed in here, if anybody. Asked once per merchant on load.
  const [account, setAccount] = useState<Account | null>(null);
  // See the mount effect's own StrictMode comment below - this survives the
  // double-invoke because refs, unlike the request itself, are shared
  // across it (same mounted instance, not a remount).
  const whoAmIRanFor = useRef<string | null>(null);

  useEffect(() => {
    // React 18 StrictMode (dev only) runs this effect twice in a row on
    // mount - `cancelled` above only ever guarded the STATE UPDATE against
    // a stale response, not the request itself, so both invocations still
    // fired a real `whoAmI` call. Live-reproduced root cause of the visitor-
    // cookie race below: this browser's very first request carries no
    // `cv3_visitor` cookie yet, so the server mints one and returns it via
    // `Set-Cookie` (engine/api/auth.py::visitor) - firing that TWICE in
    // parallel makes the server mint two DIFFERENT cookies, and the browser
    // keeps whichever `Set-Cookie` it processed last, silently orphaning
    // whatever the other request's cookie had already claimed (a cart, a
    // chat session) - surfacing later as an otherwise-inexplicable 404
    // ("no such conversation") on the shopper's first real message. Guarded
    // by the last connection actually fetched, not a bare boolean, so a
    // genuine merchant switch still re-fetches - only the immediate
    // StrictMode duplicate for the SAME connection is skipped.
    if (whoAmIRanFor.current === connection) return;
    whoAmIRanFor.current = connection;
    let cancelled = false;
    whoAmI(connection).then((who) => {
      if (!cancelled) setAccount(who);
    });
    return () => {
      cancelled = true;
    };
  }, [connection]);

  // A signed-in shopper's conversation, or this tab's.
  //
  // Derived rather than stored, so there is nothing to keep in step - which is the
  // mistake behind three separate bugs this week. Signed in, the id comes from the
  // engine and follows them across visits; as a guest it is per-tab, because there
  // is no identity to remember a guest by.
  const sessionId = account?.session_id ?? sessionFor(connection);
  const merchant = themeFor(connection);
    // Unread assistant messages while the chat is shut. Drives the badge, so a
  // shopper who closed the chat still knows something arrived for them.
  const [unread, setUnread] = useState(0);

  const [query, setQuery] = useState("");
  const [depts, setDepts] = useState<string[]>([]);
  const [dept, setDept] = useState<string | null>(null);
  const [products, setProducts] = useState<Product[]>([]);
  const [deadSearch, setDeadSearch] = useState<string | null>(null);

  const [cart, setCart] = useState<Cart | null>(null);
  const [result, setResult] = useState<CheckoutResult | null>(null);
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // See this effect's own StrictMode comment below.
  const bootstrappedRef = useRef(false);

  // The chat is controlled here so the storefront can start a conversation the
  // shopper did not - when a search finds nothing, the assistant opens itself.
  const [chatOpen, setChatOpen] = useState(false);
  const [chatTurns, setChatTurns] = useState<ChatTurn[]>([]);
  const [openedForDeadSearch, setOpenedForDeadSearch] = useState(false);

  // Pipeline stages, rule names, token counts. Off by default, on for demos.
  const [showEngine, setShowEngine] = useState(false);
  

  const load = useCallback(
    async (q: string, d: string | null) => {
      setBusy(true);
      setError(null);
      try {
        const r = await api.search(q, d);
        setProducts(r.products);

        if (!r.is_dead_search) {
          setDeadSearch(null);
          setPipeline(null);
          return;
        }

        // A search that found nothing. Rather than leaving the shopper looking at an
        // empty page with a diagnostic panel beside it, the assistant opens and
        // explains - which is what a person in a shop would do. Powered by
        // /api/v2/chat, with the real "friction=DEAD_SEARCH" signal now sent
        // explicitly (chat_v2.py's own `ChatRequestV2.friction` field) rather
        // than left for the model to guess from the sentence alone - routes to
        // the real FRICTION_RECOVERY capability (engine/shopper_v2/
        // friction_recovery.py), which offers the merchant's own real
        // department list instead of a generic apology.
        setDeadSearch(q);

        const reply = await api.chatV2(
          sessionId,
          `I searched for "${q}" and nothing came up.`,
          cart?.cart_id,
          "DEAD_SEARCH",
        );
        const mapped = mapV2Reply(reply);

        setChatTurns((prev) => [
          ...prev,
          { speaker: "shopper", text: `Searched for "${q}"` },
          {
            speaker: "assistant",
            text: reply.reply,
            products: mapped.products,
            awaitingPerson: mapped.awaitingPerson,
            usedModel: reply.used_model,
          },
        ]);

        // Opens once per visit. A shopper who mistypes twice does not want the panel
        // thrown at them again - after the first time it stays available without
        // interrupting.
        // Opens once per visit. After that it stays shut and shows a badge instead
        // - a shopper who mistypes twice does not want the panel thrown at them
        // again, but they should still know an answer is waiting.
        if (!openedForDeadSearch) {
          setChatOpen(true);
          setOpenedForDeadSearch(true);
        } else if (!chatOpen) {
          setUnread((n) => n + 1);
        }
      } catch (e) {
        setError(shopperMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [sessionId, cart, openedForDeadSearch, chatOpen],
  );
  // Set on the root rather than passed as props. Every token in the stylesheet keys
  // off this, so one attribute changes the whole shop and no component needs to know
  // which merchant it is rendering.
  useEffect(() => {
    document.documentElement.dataset.theme = merchant.theme;
    document.title = merchant.name;
  }, [merchant]);
  useEffect(() => {
    // Same StrictMode double-invoke as the `whoAmI` effect above, and the
    // same real consequence here: with no stored cart yet, BOTH invocations
    // read the same (empty) `cartKey` and each call `api.createCart()`,
    // creating two real baskets from one page load - live-reproduced via
    // Playwright network tracing, alongside the matching double
    // `/api/account/me` fetch, both racing to mint the visitor cookie (see
    // `whoAmIRanFor`'s own comment). `[]` deps means mount-only, so a bare
    // once-guard is enough here - no connection-switch case to preserve.
    if (bootstrappedRef.current) return;
    bootstrappedRef.current = true;
    load("", null);
    api.connections().then(setConnections).catch(() => {});
    api.departments().then((d) => setDepts(d.departments)).catch(() => {});
    // Reuse the visit's cart rather than starting a new one on every page load.
    //
    // Creating one unconditionally orphaned whatever the shopper had. The session
    // survived the reload and the cart did not, so the two disagreed: asked what
    // was in the cart, the assistant answered from the conversation while the panel
    // showed nothing. Both were right about different carts.
    //
    // The stored id is verified before it is trusted. A cart the platform has since
    // dropped - expired, or the merchant restarted - would otherwise leave every
    // request pointing at something that no longer exists.
    // Keyed by merchant. One key meant a Kettle cart could be handed to
    // Northfield, which answered 404 - correctly, since it never issued it.
        // Keyed by merchant. One key meant a Kettle cart could be handed to
    // Northfield, which answered 404 - correctly, since it never issued it.
    const cartKey = `cv3_cart_${connection}`;

    // A signed-in shopper's basket first, then this tab's.
    //
    // The conversation already followed a shopper and the basket did not, which is
    // the kind of inconsistency somebody notices at once: their chat is where they
    // left it and their cart is empty. Their own comes first because it is the more
    // specific claim - a tab's cart could belong to anybody who used this browser.
    const storedCart = account?.cart_id ?? sessionStorage.getItem(cartKey);

    // A cart the engine does not recognise is not a cart. This already fell back
    // to creating a new one; the missing half was clearing the stale id, so the
    // next reload tried the dead one again.
    const cartReady = storedCart
      ? api.getCart(storedCart).catch(() => {
          sessionStorage.removeItem(cartKey);
          return api.createCart();
        })
      : api.createCart();
      
    // Structural fix, live-reproduced: this used to also clear the session
    // key on ANY transcript-fetch failure, including the completely
    // ordinary "nothing sent yet" 404 every brand-new session gets (the
    // transcript route is read-only and structurally never claims an
    // untouched session - see engine/api/auth.py::may_touch's own
    // docstring). That 404 is indistinguishable at the HTTP level from a
    // genuinely orphaned id (server data wiped under a still-remembered
    // browser id), so this was deleting a session `sessionFor()` had just
    // freshly created, moments after creating it - and since `sessionId`
    // is deliberately DERIVED fresh on every render (not held in state,
    // see that comment above), a later render would then mint a SECOND,
    // different id before the shopper's first real message ever went out,
    // and the composer's own POST could land on either one depending on
    // timing. Reproduced live via Playwright: three different session ids
    // observed within one page load, and an intermittent 404 on the
    // shopper's own first chat message as a direct result.
    //
    // No replacement recovery is needed here: a genuinely orphaned id
    // (server data wiped) already self-heals on the shopper's next REAL
    // write - `require_owner`'s claim-on-first-touch treats an id the
    // server has never seen as simply unclaimed, and claims it fresh
    // (`engine/db/owners.py::may_touch`). This read-only pre-check's only
    // job was cosmetic (skip an empty "checking history" flash), never
    // load-bearing for correctness - so removing it trades a nonexistent
    // benefit for closing a real, reproduced bug.

    cartReady
      .then((c) => {
        sessionStorage.setItem(cartKey, c.cart_id);
        setCart(c);
      })
      .catch(() => setError("Could not start a cart"));
    // Runs once on mount. load is intentionally not a dependency here - it changes
    // when the cart arrives, and re-running the initial load then would be wasteful.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

    // Swap to the shopper's own basket once we know who they are.
  //
  // The effect above runs on mount, before whoAmI has answered - so account is
  // always null there and their basket was never used. Adding account to its
  // dependencies would re-run the whole thing: products, departments,
  // connections, and a second cart.
  //
  // So this is its own effect, and it only acts when there is something to act on.
  useEffect(() => {
    const theirs = account?.cart_id;
    if (!theirs) return;
    if (cart?.cart_id === theirs) return;

    api
      .getCart(theirs)
      .then((c) => {
        sessionStorage.setItem(`cv3_cart_${connection}`, c.cart_id);
        setCart(c);
      })
      .catch(() => {
        // Their remembered basket is gone from the platform - expired, or the
        // merchant restarted. Whatever this tab has is better than nothing, and
        // silence is better than an error about a cart they did not ask about.
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account?.cart_id, connection]);

  // Keep the engine's idea of their basket in step with this one.
  //
  // Only sign-in recorded it before, so a basket filled afterwards was never
  // theirs. Runs on every change, and does nothing for a guest.
  // Keep the engine's idea of their basket in step with this one.
  //
  // Never claims an empty cart, and that guard is the whole fix. The mount effect
  // creates a cart before whoAmI has answered, so this fired with a fresh empty one
  // and recorded it as their basket - overwriting the real id and losing everything
  // in it. An empty cart is never worth remembering as somebody's, so refusing to
  // record one removes the race rather than trying to order it.
  useEffect(() => {
    if (!account || !cart?.cart_id) return;
    if (account.cart_id === cart.cart_id) return;
    if ((cart.item_count ?? 0) === 0) return;
    claimCart(connection, cart.cart_id);
  }, [account, cart?.cart_id, cart?.item_count, connection]);

  async function guard(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (e) {
      setError(shopperMessage(e));
    } finally {
      setBusy(false);
    }
  }

  /** Report friction to the engine as a conversation rather than a
   *  diagnostic - a declined card or a rejected coupon get a real,
   *  grounded explanation from the assistant, the same as any other
   *  question. Powered by /api/v2/chat, with the real friction type now
   *  sent explicitly (chat_v2.py's own `ChatRequestV2.friction` field) so
   *  it routes to the real FRICTION_RECOVERY/PAYMENT_RECOVERY capability
   *  (engine/shopper_v2/friction_recovery.py,
   *  engine/shopper_v2/payment_recovery.py) instead of leaving the model
   *  to guess the kind of help needed from the sentence alone.
   */
  const reportFriction = async (shopperLine: string, friction: string) => {
    const reply = await api.chatV2(sessionId, shopperLine, cart?.cart_id, friction);
    const mapped = mapV2Reply(reply);

    setChatTurns((prev) => [
      ...prev,
      { speaker: "shopper", text: shopperLine },
      {
        speaker: "assistant",
        text: reply.reply,
        products: mapped.products,
        awaitingPerson: mapped.awaitingPerson,
        usedModel: reply.used_model,
      },
    ]);

    // Opens once per visit. After that a badge is enough - a shopper hitting a
    // second problem does not want the panel thrown at them again.
    if (!openedForDeadSearch) {
      setChatOpen(true);
      setOpenedForDeadSearch(true);
    } else if (!chatOpen) {
      setUnread((n) => n + 1);
    }

    return reply;
  };

  const addToCart = (productId: string, variantId: string | null) =>
    guard(async () => {
      if (!cart) return;
      setCart(await api.addLine(cart.cart_id, productId, variantId));
      setResult(null);
    });

  const refreshCart = () => {
    if (!cart) return;
    api.getCart(cart.cart_id).then(setCart).catch(() => {});
  };

  const applyPromo = (code: string) =>
    guard(async () => {
      if (!cart) return;
      try {
        setCart(await api.applyPromotion(cart.cart_id, code));
        setPipeline(null);
      } catch (e) {
        // A rejected coupon is friction, not a dead end. The shopper gets an
        // explanation and an alternative rather than a red error line.
        await reportFriction(`My code "${code.toUpperCase()}" didn't work.`, "PROMOTION_FAILED");
        throw e;
      }
    });

    
  const checkout = (cardLast4: string) =>
    guard(async () => {
      if (!cart) return;
      const r = await api.checkout(cart.cart_id, cardLast4, sessionId);
      setResult(r);

      if (r.succeeded) {
        setPipeline(null);
        // A fresh cart, stored under this merchant's key. Same as
        // onCartRetired does after a chat-driven checkout - without this,
        // sessionStorage keeps pointing at the now-paid cart, so the next
        // reload (or remount from switching tabs) reloads it and the next
        // add-to-cart is rejected as CART_ALREADY_PAID.
        const fresh = await api.createCart();
        sessionStorage.setItem(`cv3_cart_${connection}`, fresh.cart_id);
        setCart(fresh);
        if (account) {
          // A signed-in shopper's account.cart_id still names the cart that
          // was just paid for - the ordinary claim effect below only claims
          // a cart once it holds an item, so a reload before adding anything
          // to this brand-new one would otherwise read account.cart_id first
          // (mount effect) and reload the paid cart. This is a known-safe,
          // deliberate replacement, not the race that guard protects against.
          await claimCart(connection, fresh.cart_id);
          setAccount((prev) =>
            prev ? { ...prev, cart_id: fresh.cart_id } : prev,
          );
        }
      } else if (r.chat_reply) {
        // The backend already ran this decline through the real pipeline -
        // a case, the honest specific reason, recovery or escalation - and
        // wrote its own turn into this same session. Showing that turn here
        // is display only; posting a second, separately-reasoned message
        // (as this used to do via reportFriction) would tell the shopper
        // two different things about the one decline.
        setChatTurns((prev) => [
          ...prev,
          { speaker: "shopper", text: "My payment didn't go through." },
          { speaker: "assistant", text: r.chat_reply as string, usedModel: false },
        ]);
        if (!openedForDeadSearch) {
          setChatOpen(true);
          setOpenedForDeadSearch(true);
        } else if (!chatOpen) {
          setUnread((n) => n + 1);
        }
      } else {
        // No session_id reached the backend (should not happen from this
        // storefront, but the field is optional) - fall back to the old
        // generic path rather than showing nothing.
        await reportFriction("My payment didn't go through.", "PAYMENT_DECLINED");
      }

      // Take the shopper to the order either way. A declined order still exists and
      // still has a page - that is the whole premise of recovery.
      if (r.order) {
        setOpenOrder(r.order.order_id);
        setView("order");
      }
    });

  const openDetail = (id: string) => {
    setOpenProduct(id);
    setView("product");
  };

  const browse = (d: string | null) => {
    setDept(d);
    setQuery("");
    setView("shop");
    load("", d);
  };

  /** Switch merchant.
   *
   *  Everything resets because nothing carries across: a cart on one platform means
   *  nothing on another, and a session's memory belongs to one merchant. Carrying
   *  state over would be a bug that looks like a feature until someone's coffee
   *  order shows up in a running shop.
   */
  const switchMerchant = (id: string) => {
    // Navigate rather than set state.
    //
    // The merchant comes from the address now, so changing it means going
    // somewhere - and a full load rather than a router push, because the cart, the
    // session and the theme all derive from it. Unpicking each by hand is how one
    // of them ends up stale, which is the mistake behind three bugs this week.
    window.location.assign(pathFor(id));
  };

  // Defined once and reused across the shop, product and order views, so the cart
  // and engine panel cannot drift apart between them.
  const sidebar = (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {showEngine && pipeline && (
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">Engine</span>
            <span className="eyebrow">{pipeline.friction?.replace(/_/g, " ")}</span>
          </div>
          <div className="panel-body">
            <Gates pipeline={pipeline} />
          </div>
        </section>
      )}

      {cart && (
        <ProposalsPanel
          sessionId={sessionId}
          cartId={cart.cart_id}
          refreshSignal={chatTurns.length}
          onCartChanged={() => {
            api.getCart(cart.cart_id).then(setCart).catch(() => {});
          }}
        />
      )}

      <CartPanel
        cart={cart}
        result={view === "order" ? null : result}
        busy={busy}
        error={error}
        account={account}
        onSetEmail={async (email) => {
          // Add the email to a legacy account, and reflect it here so the
          // payment buttons appear without a reload.
          await setEmail(connection, email);
          setAccount((prev) => (prev ? { ...prev, email } : prev));
        }}
        onPromo={applyPromo}
        onCheckout={checkout}
        couponHint={merchant.couponHint}
        onRemoveLine={async (lineId) => {
          if (!cart) return;
          // No model involved. Emptying your own basket should not depend on a
          // provider being available, which until now it did - the only way out
          // of a cart was to ask the assistant.
          setCart(await api.changeLine(cart.cart_id, lineId, 0));
        }}
      />
    </div>
  );

  const shopView = view !== "console" && view !== "queue";

  return (
    <>
      <header className="masthead">
        <div className="brandbar">
          <div className="wordmark">{merchant.name}</div>
          {connections.length > 1 && (
            <select
              className="merchantpick"
              value={connection}
              onChange={(e) => switchMerchant(e.target.value)}
              aria-label="Merchant"
            >
              {connections.map((c) => (
                <option key={c.connection_id} value={c.connection_id}>
                  {c.merchant_name} — {c.platform}
                </option>
              ))}
            </select>
          )}
        </div>

        {shopView && (
          <form
            className="searchbar"
            onSubmit={(e) => {
              e.preventDefault();
              setView("shop");
              load(query, dept);
            }}
          >
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={merchant.searchPlaceholder}
              aria-label="Search the shop"
            />
            <button type="submit">SEARCH</button>
          </form>
        )}

        <label className="enginetoggle">
          <input
            type="checkbox"
            checked={showEngine}
            onChange={(e) => setShowEngine(e.target.checked)}
          />
          Show engine
        </label>

        <nav className="tabs">
          <AccountMenu account={account} onAccount={setAccount} />
        </nav>
      </header>

      {view === "console" ? (
        <MerchantConsole />
      ) : view === "queue" ? (
        <div className="layout" style={{ gridTemplateColumns: "1fr" }}>
          <main>
            <OpsConsole />
          </main>
        </div>
      ) : view === "product" && openProduct ? (
        <div className="layout">
          <main>
            <ProductDetail
              productId={openProduct}
              busy={busy}
              onAdd={addToCart}
              onBack={() => setView("shop")}
            />
          </main>
          {sidebar}
        </div>
      ) : view === "order" && openOrder ? (
        <div className="layout">
          <main>
            <OrderView orderId={openOrder} onBack={() => setView("shop")} />
          </main>
          {sidebar}
        </div>
      ) : (
        <>
          {!deadSearch && !query && !dept && <Hero theme={merchant} />}
          <div className="layout">
            <main>
            <nav className="depts">
              <button aria-pressed={dept === null} onClick={() => browse(null)}>
                All
              </button>
              {depts.map((d) => (
                <button
                  key={d}
                  aria-pressed={dept === d}
                  onClick={() => browse(d)}
                >
                  {d}
                </button>
              ))}
            </nav>

            {deadSearch && (
              <div className="banner">
                <div className="eyebrow">Nothing matched</div>
                <h2>No results for &ldquo;{deadSearch}&rdquo;</h2>
                <p>
                  Our assistant has some suggestions &mdash; have a look at the
                  chat.
                </p>
              </div>
            )}

            {products.length === 0 && !deadSearch && !busy && (
              <p className="empty">Nothing in this category yet.</p>
            )}

            <div className="grid">
              {products.map((p) => (
                <ProductCard
                  key={p.product_id}
                  product={p}
                  onAdd={addToCart}
                  onOpen={openDetail}
                  busy={busy}
                />
              ))}
            </div>
          </main>

          {sidebar}
          </div>
        </>
      )}

      {shopView && (
        <ChatWidget
          sessionId={sessionId}
          cartId={cart?.cart_id}
          orderId={openOrder ?? undefined}
          open={chatOpen}
          turns={chatTurns}
          depts={depts}
          onOpen={() => {
            setChatOpen(true);
            setUnread(0);
          }}
          account={account}
          onAccount={setAccount}
          onClose={() => setChatOpen(false)}
          onTurns={setChatTurns}
          onReply={(r) => showEngine && setPipeline(fromChat(r, null))}
          onCartChanged={refreshCart}
          onCartRetired={async () => {
            // A fresh cart, stored under this merchant's key. Same as
            // the sidebar checkout does after a successful order.
            const fresh = await api.createCart();
            sessionStorage.setItem(`cv3_cart_${connection}`, fresh.cart_id);
            setCart(fresh);
          }}
          unread={unread}
          merchantName={
            connections.find((c) => c.connection_id === connection)?.merchant_name
          }
          onUnread={(n) => setUnread((prev) => prev + n)}
        />
      )}
    </>
  );
}