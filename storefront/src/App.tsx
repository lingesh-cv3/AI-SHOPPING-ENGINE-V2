import { useCallback, useEffect, useState } from "react";
import {
  api,
  getConnection,
  setConnection,
  shopperMessage,
  type Cart,
  type ChatReply,
  type ChatTurn,
  type CheckoutResult,
  type Connection,
  type Pipeline,
  type Product,
} from "./api";
import { OpsConsole } from "./OpsConsole";
import { CartPanel } from "./CartPanel";
import { ChatWidget } from "./ChatWidget";
import { Gates } from "./Gates";
import { MerchantConsole } from "./MerchantConsole";
import { OrderView } from "./OrderView";
import { ProductCard } from "./ProductCard";
import { ProductDetail } from "./ProductDetail";
import { Hero } from "./Hero";
import { themeFor } from "./theme";

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
export default function App() {
  const [view, setView] = useState<
    "shop" | "console" | "queue" | "product" | "order"
  >("shop");
  const [openProduct, setOpenProduct] = useState<string | null>(null);
  const [openOrder, setOpenOrder] = useState<string | null>(null);

  // One session id for the visit, shared by the friction path and the chat. This is
  // the mechanism behind shared memory - both report against it, so the assistant
  // knows about a declined payment nobody mentioned.
  const [sessionId] = useState(
    () => `sess_${Math.random().toString(36).slice(2, 12)}`,
  );

  const [connections, setConnections] = useState<Connection[]>([]);
  const [connection, setConnectionState] = useState(getConnection());
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
        // explains - which is what a person in a shop would do.
        setDeadSearch(q);

        const reply = await api.chat(
          sessionId,
          `I searched for "${q}" and nothing came up.`,
          { friction: "DEAD_SEARCH", query: q, cartId: cart?.cart_id },
        );

        setChatTurns((prev) => [
          ...prev,
          { speaker: "shopper", text: `Searched for "${q}"` },
          {
            speaker: "assistant",
            text: reply.reply,
            products: reply.products,
            awaitingPerson: reply.awaiting_person,
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

        setPipeline(fromChat(reply, "DEAD_SEARCH"));
      } catch (e) {
        setError(shopperMessage(e));
      } finally {
        setBusy(false);
      }
    },
       [sessionId, cart?.cart_id, openedForDeadSearch, chatOpen],
  );
  // Set on the root rather than passed as props. Every token in the stylesheet keys
  // off this, so one attribute changes the whole shop and no component needs to know
  // which merchant it is rendering.
  useEffect(() => {
    document.documentElement.dataset.theme = merchant.theme;
    document.title = merchant.name;
  }, [merchant]);
  useEffect(() => {
    load("", null);
    api.connections().then(setConnections).catch(() => {});
    api.departments().then((d) => setDepts(d.departments)).catch(() => {});
    api.createCart().then(setCart).catch(() => setError("Could not start a cart"));
    // Runs once on mount. load is intentionally not a dependency here - it changes
    // when the cart arrives, and re-running the initial load then would be wasteful.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  /** Report friction to the engine as a conversation rather than a diagnostic.
   *
   *  Dead search, a declined card and a rejected coupon are all the same shape from
   *  the shopper's side: something went wrong and they need to know what to do. They
   *  get an assistant that explains. The pipeline trace comes back on the same
   *  response, so the demo panel costs no extra model call.
   */
  const reportFriction = async (
    friction: string,
    shopperLine: string,
    opts: { query?: string; orderId?: string; known?: Record<string, string> } = {},
  ) => {
    const reply = await api.chat(sessionId, shopperLine, {
      friction,
      query: opts.query,
      cartId: cart?.cart_id,
      orderId: opts.orderId,
      known: opts.known,
    });

    setChatTurns((prev) => [
      ...prev,
      { speaker: "shopper", text: shopperLine },
      {
        speaker: "assistant",
        text: reply.reply,
        products: reply.products,
        awaitingPerson: reply.awaiting_person,
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

    setPipeline(fromChat(reply, friction));
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
        await reportFriction(
          "PROMOTION_FAILED",
          `My code "${code.toUpperCase()}" didn't work.`,
          { known: { code: code.toUpperCase() } },
        );
        throw e;
      }
    });

    
  const checkout = (cardLast4: string) =>
    guard(async () => {
      if (!cart) return;
      const r = await api.checkout(cart.cart_id, cardLast4);
      setResult(r);

      if (r.succeeded) {
        setPipeline(null);
        setCart(await api.createCart());
      } else {
        // A declined card is the moment a shopper is most likely to leave. They get
        // told what happened and what can be done about it, which differs by
        // platform - one can retry the payment, the other cannot.
        await reportFriction(
          "PAYMENT_DECLINED",
          "My payment didn't go through.",
          { orderId: r.order?.order_id },
        );
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
  const switchMerchant = async (id: string) => {
    setConnection(id);
    setConnectionState(id);
    setDept(null);
    setQuery("");
    setProducts([]);
    setDeadSearch(null);
    setPipeline(null);
    setResult(null);
    setOpenOrder(null);
    setOpenProduct(null);
    setChatTurns([]);
    setChatOpen(false);
    setOpenedForDeadSearch(false);
    setView("shop");
    setCart(null);
    setUnread(0);

    try {
      const [d, c] = await Promise.all([api.departments(), api.createCart()]);
      setDepts(d.departments);
      setCart(c);
    } catch {
      setError("Could not switch merchant");
    }
    load("", null);
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

      <CartPanel
        cart={cart}
        result={view === "order" ? null : result}
        busy={busy}
        error={error}
        onPromo={applyPromo}
        onCheckout={checkout}
        couponHint={merchant.couponHint}
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

        <div className="tabs">
          <button aria-selected={shopView} onClick={() => setView("shop")}>
            Shop
          </button>
          <button
            aria-selected={view === "queue"}
            onClick={() => setView("queue")}
          >
            Operations
          </button>
          <button
            aria-selected={view === "console"}
            onClick={() => setView("console")}
          >
            Merchant
          </button>
        </div>
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
          onOpen={() => {
            setChatOpen(true);
            setUnread(0);
          }}
          onClose={() => setChatOpen(false)}
          onTurns={setChatTurns}
          onReply={(r) => showEngine && setPipeline(fromChat(r, null))}
          onCartChanged={refreshCart}
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