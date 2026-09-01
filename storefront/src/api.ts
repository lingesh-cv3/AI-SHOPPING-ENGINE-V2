/**
 * The engine API client.
 *
 * The only file in the storefront that knows the engine exists. Everything else
 * takes plain props, so the UI can be reasoned about without thinking about HTTP.
 *
 * Note what these types do NOT contain: no price_paise, no stock_state, no
 * platform-specific anything. The adapter normalized all of that before it
 * reached us. A storefront on a completely different commerce platform would
 * use this identical file.
 */

const ENGINE = "http://localhost:8000";

/** The merchant connection every call is scoped to.
 *
 *  Mutable because the demo switches between two merchants on completely different
 *  platforms. In a real deployment this is fixed per storefront - a shop does not
 *  change which backend it runs on - and would come from the script tag's key.
 */
// Which merchant we are talking to, remembered across reloads.
//
// This was a plain variable, so every refresh reset it to Northfield - while the
// session id and cart id in storage still belonged to Kettle. The conversation came
// back empty because the transcript endpoint filters by connection and honestly had
// nothing, and the cart 404'd because Northfield had never issued it. Two symptoms,
// one cause.
let CONNECTION =
  (typeof sessionStorage !== "undefined" &&
    sessionStorage.getItem("cv3_connection")) ||
  "conn_demo";

export function setConnection(id: string) {
  sessionStorage.setItem("cv3_connection", id);
  CONNECTION = id;
}

export function getConnection() {
  return CONNECTION;
}

export interface Connection {
  connection_id: string;
  merchant_name: string;
  platform: string;
  mode: string;
  supported_count: number;
  unsupported: string[];
}

/** Money never crosses the wire as a float. `display` is pre-formatted. */
export interface Money {
  amount: string;
  currency: string;
  display: string;
}

export interface Variant {
  variant_id: string;
  title: string | null;
  availability: string;
  quantity_available: number | null;
}


/** A turn as stored, not as rendered. Polled so the widget can pick up messages
 *  written by something other than the shopper - an operator approving a recovery. */
export interface StoredTurn {
  turn_id: string;
  speaker: "shopper" | "assistant";
  text: string;
  case_id: string | null;
  at: string | null;
}

export interface Product {
  product_id: string;
  image_url: string | null;
  title: string;
  description: string | null;
  price: Money | null;
  compare_at_price: Money | null;
  availability: string;
  categories: string[];
  variants: Variant[];
}




export interface SearchResult {
  query: string;
  /** Zero results. The friction signal the engine acts on. */
  is_dead_search: boolean;
  total_available: number | null;
  products: Product[];
}

export interface CartLine {
  line_id: string;
  product_id: string;
  variant_id: string | null;
  title: string;
  quantity: number;
  unit_price: Money;
  line_total: Money;
}

export interface Cart {
  cart_id: string;
  item_count: number;
  is_empty: boolean;
  subtotal: Money;
  discount_total: Money | null;
  tax_total: Money | null;
  shipping_total: Money | null;
  grand_total: Money | null;
  applied_promotions: string[];
  lines: CartLine[];
}

export interface CheckoutResult {
  succeeded: boolean;
  payment_status: string;
  decline_reason: string | null;
  order: {
    order_id: string;
    status: string;
    grand_total: Money | null;
    amount_paid: Money | null;
  } | null;
}


export interface ChatTurn {
  /** A total and the ways to pay it. Rendered as buttons; tapping one is
   *  what charges. */
  payment?: PaymentOffer;
  /** Options offered with this message. Rendered as buttons: a shopper tapping
   *  "250g ground" cannot be misread, where typing "ground" can. */
  choices?: {
    variant_id: string;
    label: string;
    product_id: string;
    product_title: string;
    left: number | null;
  }[];
  speaker: "shopper" | "assistant";
  text: string;
  /** Products the engine actually fetched, not ones the model described. */
  products?: {
    product_id: string;
    title: string;
    description: string | null;
    price: string | null;
    categories: string[];
  }[];
  awaitingPerson?: boolean;
  usedModel?: boolean;
}

export interface PaymentOffer {
  cart_id?: string;
  item_count?: number;
  grand_total?: string;
  subtotal?: string;
  lines?: { title: string; quantity: number; total: string }[];
  cards?: { last4: string; label: string }[];
  /** Present on the reply to a payment rather than an offer of one. */
  paid?: boolean;
  /** The cart has been bought and should be replaced. */
  cart_retired?: boolean;
  order_id?: string | null;
}

export interface ChatReply {
  reply: string;
  session_id: string;
  case_id: string | null;
  used_model: boolean;
  diagnosis: string | null;
  action_taken: string | null;
  action_summary: string | null;
  products: {
    product_id: string;
    title: string;
    description: string | null;
    price: string | null;
    categories: string[];
  }[];
  awaiting_person: boolean;
  payment: PaymentOffer;
  /** Options the shopper must pick between. Rendered as buttons - tapping is
   *  unambiguous where typing is not. */
  choices: {
    variant_id: string;
    label: string;
    product_id: string;
    product_title: string;
    left: number | null;
  }[];
  remembered_turns: number;
  remembered_friction: number;
  cart_changed: boolean;
  proposed: string[];
  rejected: { action_type: string; reason: string; detail: string }[];
  selected_action: string | null;
  selection_reason: string | null;
  escalated_because_empty: boolean;
  risk_outcome: string | null;
  risk_rule: string | null;
  risk_reason: string | null;
  financial: boolean;
  model_reply: string | null;
  evidence: string[];
  model_name: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
}



export interface Order {
  order_id: string;
  status: string;
  payment_status: string;
  decline_reason: string | null;
  currency: string;
  grand_total: Money | null;
  amount_paid: Money | null;
  created_at: string | null;
  lines: CartLine[];
}


export interface Rejection {
  action_type: string;
  reason: string;
  detail: string;
}

/** The full pipeline trace: proposed, filtered, selected, classified. */
export interface Pipeline {
  friction: string | null;
  proposed: string[];
  rejected: Rejection[];
  selected_action: string;
  selection_reason: string;
  escalated_because_empty: boolean;
  risk_outcome: string;
  risk_rule: string;
  risk_reason: string;
  financial: boolean;
  reversible: boolean;
    /** Whether the AI actually reasoned about this, or rules stood in for it. */
  used_model: boolean;
  model_name: string | null;
  diagnosis: string | null;
  evidence: string[];
  /** What the model wanted to say. Kept for the audit trail. */
  reply: string | null;
  /** What the shopper actually sees. Differs from reply when the model
   *  proposed something this platform cannot do. */
  shopper_reply: string | null;
  fallback_reason: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
}



export interface QueueItem {
  approval_id: string;
  case_id: string;
  action_type: string;
  risk_rule: string | null;
  requested_at: string;
  expires_at: string | null;
  friction_type: string | null;
  diagnosis: string | null;
  evidence: string[];
  used_model: boolean;
  shopper_reply: string | null;
  model_reply: string | null;
  selection_reason: string | null;
  rejected: { action_type: string; reason: string; detail: string }[];
  financial: boolean;
  order_id: string | null;
  query: string | null;
}

export interface Executed {
  succeeded: boolean;
  summary: string;
  action_type: string;
  payload: Record<string, unknown>;
  error_code: string | null;
  latency_ms: number | null;
  final_state: string;
}

export interface Decision {
  approval_id: string;
  case_id: string;
  state: string;
  changed: boolean;
  executed: Executed | null;
}

export interface Stats {
  cases: number;
  pending_approvals: number;
  auto_cleared: number;
  reasoned_by_model: number;
}






/** A normalized error from the engine. Never a raw platform error. */
/** A normalized error from the engine. Never a raw platform error. */
export class EngineError extends Error {
  code: string;
  retryable: boolean;

  constructor(code: string, message: string, retryable: boolean) {
    super(message);
    this.code = code;
    this.retryable = retryable;
  }
}

/** The publishable key for a merchant.
 *
 *  Built into the bundle on purpose. A publishable key is not a secret - it ships to
 *  every browser and anyone can read it, which is true of every provider that issues
 *  one. What makes it safe is what it cannot do: it speaks for one merchant, and it
 *  cannot change a policy or decide an approval.
 *
 *  Chosen by connection, so switching merchant switches keys - and Kettle's key
 *  against Northfield is refused, which is the point of the whole exercise.
 */
function publishableKey(connectionId: string): string {
  const keys: Record<string, string | undefined> = {
    conn_demo: import.meta.env.VITE_CV3_PK_CONN_DEMO,
    conn_kettle: import.meta.env.VITE_CV3_PK_CONN_KETTLE,
  };
  return keys[connectionId] || "";
}


async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${ENGINE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      // Every shopper request carries the current merchant's publishable
      // key. The engine no longer takes the request's word for which
      // merchant it speaks for.
      Authorization: `Bearer ${publishableKey(getConnection())}`,
      ...init?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const d = body?.detail;
    if (d && typeof d === "object") {
      throw new EngineError(d.code, d.message, d.retryable);
    }
    throw new EngineError("UNKNOWN", `request failed (${res.status})`, false);
  }
  return res.json();
}

const shop = (p: string) => `/api/shop/${CONNECTION}${p}`;

export const api = {
  search: (q: string, dept?: string | null) => {
    const params = new URLSearchParams({ q, limit: "48" });
    if (dept) params.set("dept", dept);
    return call<SearchResult>(shop(`/search?${params}`));
  },
  connections: () => call<Connection[]>("/api/connections"),

  departments: () => call<{ departments: string[] }>(shop("/departments")),

  product: (id: string) => call<Product>(shop(`/product/${id}`)),

  createCart: () => call<Cart>(shop("/cart"), { method: "POST" }),

  getCart: (cartId: string) => call<Cart>(shop(`/cart/${cartId}`)),

  addLine: (cartId: string, productId: string, variantId: string | null, qty = 1) =>
    call<Cart>(shop(`/cart/${cartId}/lines`), {
      method: "POST",
      body: JSON.stringify({
        product_id: productId,
        variant_id: variantId,
        quantity: qty,
      }),
    }),

  /** A tap, carried out without a model call.
   *
   *  The shopper named a product and possibly an option, both as ids the engine
   *  issued. There is nothing to interpret, so sending it to be interpreted was
   *  slow, cost tokens, and broke under rate limiting - a shopper tapping a size
   *  was told the assistant was busy, when we knew exactly what they wanted.
   */
  /** Take payment. The only call in this file that spends money.
   *
   *  Reached only by a shopper tapping a card - there is no action type for it, so
   *  the AI cannot propose it and the risk gate never sees it. The guarantee was
   *  always that we do not spend other people's money unattended, not that a
   *  shopper cannot spend their own.
   */
  pay: (
    sessionId: string,
    cartId: string,
    cardLast4: string,
  ) =>
    call<ChatReply>("/api/chat/pay", {
      method: "POST",
      body: JSON.stringify({
        connection_id: getConnection(),
        session_id: sessionId,
        cart_id: cartId,
        card_last4: cardLast4,
      }),
    }),

  act: (
    sessionId: string,
    productId: string,
    said: string,
    variantId?: string,
    cartId?: string,
  ) =>
    call<ChatReply>("/api/chat/act", {
      method: "POST",
      body: JSON.stringify({
        connection_id: getConnection(),
        session_id: sessionId,
        product_id: productId,
        variant_id: variantId ?? null,
        cart_id: cartId ?? null,
        said,
      }),
    }),

  transcript: (sessionId: string) =>
    call<{ session_id: string; turns: StoredTurn[]; friction: string[] }>(
      `/api/chat/${getConnection()}/${sessionId}`,
    ),

  applyPromotion: (cartId: string, code: string) =>
    call<Cart>(shop(`/cart/${cartId}/promotion`), {
      method: "POST",
      body: JSON.stringify({ code }),
    }),

  /** Cards ending 0002, 0003, 0004 always decline. */
  checkout: (cartId: string, cardLast4: string) =>
    call<CheckoutResult>(shop(`/cart/${cartId}/checkout`), {
      method: "POST",
      body: JSON.stringify({ card_last4: cardLast4 }),
    }),

  order: (orderId: string) => call<Order>(shop(`/order/${orderId}`)),

  /** Ask the engine what it would do about a detected friction. */
  /** Ask the engine what to do about a detected friction.
   *
   *  Passing the query and order id matters: without the shopper's actual search
   *  term the model can only guess at what they wanted, and a guessed alternative
   *  is worse than none.
   */
    pipeline: (
    friction: string | null,
    opts: {
      query?: string;
      cartId?: string;
      orderId?: string;
      sessionId?: string;
    } = {},
  ) =>
    call<Pipeline>("/api/simulate", {
      method: "POST",
      body: JSON.stringify({
        connection_id: getConnection(),
        friction,
        query: opts.query ?? null,
        cart_id: opts.cartId ?? null,
        order_id: opts.orderId ?? null,
        session_id: opts.sessionId ?? null,
      }),
    }),
      /** One shopper turn.
   *
   *  `friction` is set when the storefront already knows what went wrong - a dead
   *  search, a declined payment. The shopper still gets a conversation; the engine
   *  just starts from a diagnosis rather than inferring one.
   */
  chat: (
    sessionId: string,
    message: string,
    opts: {
      cartId?: string;
      orderId?: string;
      friction?: string;
      query?: string;
      /** Facts we already hold. Cheaper and more reliable than asking the model to
       *  extract something the shopper literally typed into a field. */
      known?: Record<string, string>;
    } = {},
  ) =>
    call<ChatReply>("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        connection_id: getConnection(),
        session_id: sessionId,
        message,
        cart_id: opts.cartId ?? null,
        order_id: opts.orderId ?? null,
        friction: opts.friction ?? null,
        query: opts.query ?? null,
        known: opts.known ?? {},
      }),
    }),
};
  /** Ask the engine what it would do about a detected friction. */
 

/**
 * Turn a normalized error code into something a shopper should read.
 *
 * The engine's codes are precise and internal. A shopper needs to know what
 * happened and what to do about it - never which endpoint failed or what the
 * platform called it. Anything unmapped falls back to a plain sentence rather
 * than leaking the code.
 */
export function shopperMessage(err: unknown): string {
  const code = err instanceof EngineError ? err.code : "";
  switch (code) {
    case "PROMOTION_EXPIRED":
      return "That code has expired.";
    case "PROMOTION_NOT_FOUND":
      return "We don't recognise that code.";
    case "PROMOTION_INELIGIBLE":
      return "That code needs a higher order total.";
    case "INVENTORY_INSUFFICIENT":
      return "There isn't enough left in stock.";
    case "PRODUCT_UNAVAILABLE":
      return "That item is no longer available.";
    case "VARIANT_UNAVAILABLE":
      return "That size isn't available.";
    case "CART_NOT_FOUND":
    case "CART_INVALID":
      return "Something went wrong with your cart. Try reloading the page.";
    case "PAYMENT_DECLINED":
      return "Your payment didn't go through.";
    case "CAPABILITY_UNSUPPORTED":
      return "We can't do that here. Someone will pick this up.";
    case "TIMEOUT":
    case "RATE_LIMITED":
      return "The shop is being slow. Try that again.";
    default:
      return "Something went wrong. Try that again.";
  }
}

/* ---- merchant console ---- */

export interface OperationCapability {
  operation: string;
  supported: boolean;
  reason: string | null;
  constraints: Record<string, string | number | boolean>;
}

export interface Capabilities {
  connection_id: string;
  platform: string;
  supports_webhooks: boolean;
  payment_recovery_methods: string[];
  operations: OperationCapability[];
}

export interface Policy {
  connection_id: string;
  mode: string;
  auto_allowed: string[];
  blocked: string[];
  approval_timeout_minutes: number;
}

export interface Rule {
  order: number;
  rule: string;
  explanation: string;
}

/** An action and its fixed risk properties, straight from the engine's table. */
export interface ActionInfo {
  action_type: string;
  financial: boolean;
  reversible: boolean;
  touches_customer_data: boolean;
  /** False means no setting on this page can ever make it automatic. */
  can_ever_be_automatic: boolean;
}

/** The secret key for one merchant, held for the browser session.
 *
 *  Keyed by connection, because a secret key speaks for exactly one merchant. That
 *  is why switching merchant asks again: holding Kettle's credentials tells you
 *  nothing about Northfield, and pretending otherwise would undo the isolation the
 *  engine now enforces.
 *
 *  Typed rather than built into the bundle. Unlike a publishable key, this one can
 *  change a policy and decide an approval, so it must never ship to a shopper.
 */
export function merchantKey(connectionId: string): string | null {
  return sessionStorage.getItem(`cv3_merchant_key_${connectionId}`);
}

export function setMerchantKey(connectionId: string, key: string) {
  sessionStorage.setItem(`cv3_merchant_key_${connectionId}`, key.trim());
}

export function clearMerchantKey(connectionId: string) {
  sessionStorage.removeItem(`cv3_merchant_key_${connectionId}`);
}

async function merchantCall<T>(path: string, init?: RequestInit): Promise<T> {
  const connection = getConnection();
  const key = merchantKey(connection);
  if (!key) throw new NotAuthorised();

  const res = await fetch(`${ENGINE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
      ...init?.headers,
    },
  });

  if (res.status === 401) {
    // Forgotten rather than retried. A key the engine refuses will fail every
    // subsequent request, and leaving it in place gives the merchant no way to
    // correct course - it just looks broken.
    clearMerchantKey(connection);
    throw new NotAuthorised();
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `request failed (${res.status})`);
  }

  return res.json();
}

export const console_api = {
  capabilities: () =>
    merchantCall<Capabilities>(`/api/connections/${getConnection()}/capabilities`),

  policy: () => merchantCall<Policy>(`/api/policy/${getConnection()}`),

  savePolicy: (mode: string, autoAllowed: string[], blocked: string[]) =>
    merchantCall<Policy>(`/api/policy/${getConnection()}`, {
      method: "PUT",
      body: JSON.stringify({ mode, auto_allowed: autoAllowed, blocked }),
    }),

  rules: () => call<Rule[]>("/api/policy/rules"),

  actions: () => call<ActionInfo[]>("/api/policy/actions"),

  testAction: (actionType: string) =>
    merchantCall<Pipeline>("/api/simulate", {
      method: "POST",
      body: JSON.stringify({
        connection_id: getConnection(),
        friction: null,
        candidates: [actionType],
      }),
    }),

  queue: () => merchantCall<{ approvals: QueueItem[] }>(`/api/approvals/${getConnection()}`),

  decide: (approvalId: string, approved: boolean, note?: string) =>
    merchantCall<Decision>(`/api/approvals/${getConnection()}/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({
        approved,
        decided_by: "cv3-operator",
        note: note ?? null,
      }),
    }),

  stats: () => merchantCall<Stats>(`/api/stats/${getConnection()}`),

  report: () => merchantCall<MerchantReport>(`/api/report/${getConnection()}`),
};

export interface MerchantReport {
  days: number;
  shoppers_helped: number;
  problems_solved: number;
  handled_without_you: number;
  waiting_for_you: number;
  revenue_recovered: string;
  currency: string;
  median_resolution_ms: number | null;
  friction: { type: string; count: number }[];
  recent: {
    case_id: string;
    friction_type: string | null;
    diagnosis: string | null;
    selected_action: string | null;
    risk_outcome: string | null;
    shopper_reply: string | null;
    used_model: boolean;
    created_at: string;
  }[];
}


export interface OpsQueueItem {
  approval_id: string;
  case_id: string;
  connection_id: string;
  merchant_name: string;
  action_type: string;
  risk_rule: string | null;
  requested_at: string;
  waiting_minutes: number;
  expires_at: string | null;
  minutes_left: number | null;
  friction_type: string | null;
  diagnosis: string | null;
  evidence: string[];
  used_model: boolean;
  shopper_reply: string | null;
  model_reply: string | null;
  selection_reason: string | null;
  rejected: { action_type: string; reason: string; detail: string }[];
  financial: boolean;
  order_id: string | null;
  query: string | null;
}

export interface OpsDecision {
  approval_id: string;
  case_id: string;
  connection_id: string;
  merchant_name: string;
  state: string;
  action_type: string;
  decided_at: string | null;
  decided_by: string | null;
  note: string | null;
  friction_type: string | null;
  diagnosis: string | null;
  financial: boolean;
  order_id: string | null;
  resolved: boolean | null;
  final_state: string;
  revenue: string | null;
  currency: string | null;
}

export interface OpsStats {
  waiting: number;
  oldest_wait_minutes: number;
  by_merchant: Record<string, number>;
  today: number;
}

/** CV3's own view, across every merchant.
 *
 *  Separate from console_api, which is always scoped to one connection. These
 *  deliberately are not, because an operator covering several clients should not
 *  have to switch between them to find their work.
 */
/** The operator key, held for the browser session only.
 *
 *  Deliberately not a build-time constant. The operations console shares a bundle
 *  with the shopper storefront, so a key compiled in would ship to every shopper -
 *  and any of them could then approve their own refund. Typed once, kept in
 *  sessionStorage, never in the code anybody downloads.
 *
 *  The permanent answer is a separate application, which is on the roadmap. This is
 *  the honest version until then.
 */
export function operatorKey(): string | null {
  return sessionStorage.getItem("cv3_operator_key");
}

export function setOperatorKey(key: string) {
  sessionStorage.setItem("cv3_operator_key", key.trim());
}

export function clearOperatorKey() {
  sessionStorage.removeItem("cv3_operator_key");
}

/** Thrown when the engine refuses the key, so the console can ask again rather
 *  than showing a generic failure. */
export class NotAuthorised extends Error {
  constructor() {
    super("that key was refused");
  }
}

async function opsCall<T>(path: string, init?: RequestInit): Promise<T> {
  const key = operatorKey();
  if (!key) throw new NotAuthorised();

  const res = await fetch(`${ENGINE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
      ...(init?.headers || {}),
    },
  });

  if (res.status === 401) {
    // A refused key is worth forgetting. Keeping it would fail every subsequent
    // request with no way for the operator to correct it.
    clearOperatorKey();
    throw new NotAuthorised();
  }

  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || `request failed (${res.status})`);
  }

  return res.json();
}

export const ops_api = {
  queue: () => opsCall<{ approvals: OpsQueueItem[] }>("/api/ops/queue"),

  history: () => opsCall<{ decisions: OpsDecision[] }>("/api/ops/history"),

  stats: () => opsCall<OpsStats>("/api/ops/stats"),

  /** The connection id comes from the item, not from the current shop. */
  decide: (
    connectionId: string,
    approvalId: string,
    approved: boolean,
    note?: string,
  ) =>
    opsCall<Decision>(`/api/approvals/${connectionId}/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({
        approved,
        decided_by: "cv3-operator",
        note: note ?? null,
      }),
    }),
};
