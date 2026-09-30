/**
 * The merchant's approvals: one client, one store, one count.
 *
 * Every place that shows "how many decisions are waiting" - the nav badge, the
 * Approvals page, Overview, Payments & Checkout, the Copilot banner - reads the
 * same number from here, so two screens can never disagree. Before this, three of
 * them each filtered the queue by a payment-only set of action types and each
 * reported a different count of the same thing.
 *
 * Why a module-level store rather than component state: the badge has to stay
 * live on every Merchant screen, not only on the page that lists the approvals,
 * and the list and the count must come from the same fetch when both are on
 * screen.
 *
 * Honest by construction:
 *   - `total` is null until it has really been fetched. Nothing renders "0 waiting"
 *     for "not loaded yet" or "could not check".
 *   - a failed refresh keeps the last good data and says it is stale; it never
 *     blanks the page or turns into an empty list.
 *   - a response that arrives after a newer request, or after the merchant
 *     changed, is dropped.
 *
 * This file does not decide anything. A decision is the same POST the console has
 * always sent; every safety rule runs server-side, unchanged.
 */

import { useSyncExternalStore } from "react";
import {
  NotAuthorised,
  clearMerchantKey,
  getConnection,
  merchantKey,
} from "./api";
import type { Decision, QueueItem } from "./api";

export const PAGE_SIZE = 25;
const POLL_MS = 20_000;

export interface Executable {
  can_run: boolean;
  reason: string | null;
}

/** A pending approval, as `GET /api/approvals/{connection}` returns it. */
export interface ApprovalItem extends QueueItem {
  waiting_minutes: number;
  minutes_left: number | null;
  executable: Executable;
}

/** A decided approval, as `GET /api/approvals/{connection}/history` returns it. */
export interface DecidedItem {
  approval_id: string;
  case_id: string;
  state: string;
  action_type: string;
  risk_rule: string | null;
  requested_at: string | null;
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

interface ListState<T> {
  /** idle: never asked. loading: first fetch in flight. ready: has data. error: first fetch failed. */
  phase: "idle" | "loading" | "ready" | "error";
  items: T[];
  total: number | null;
  offset: number;
  /** When the data on screen was fetched, ms since epoch. */
  asOf: number | null;
  /** A refresh is in flight while older data is still shown. */
  refreshing: boolean;
  /** The last fetch failed. Any data shown is stale. */
  error: string | null;
}

export interface RecentDecision {
  approvalId: string;
  actionType: string;
  approved: boolean;
  result: Decision;
  at: number;
}

export interface ApprovalsState {
  connection: string;
  pending: ListState<ApprovalItem>;
  history: ListState<DecidedItem>;
  /** What the merchant has just decided this session, so a result is not lost when
   *  the card leaves the queue. Most recent first. */
  recent: RecentDecision[];
  /** Rule name -> the engine's own plain-words explanation. */
  rules: Record<string, string>;
  /** The engine refused the merchant's key. */
  unauthorised: boolean;
}

function emptyList<T>(): ListState<T> {
  return {
    phase: "idle",
    items: [],
    total: null,
    offset: 0,
    asOf: null,
    refreshing: false,
    error: null,
  };
}

function initial(connection: string): ApprovalsState {
  return {
    connection,
    pending: emptyList(),
    history: emptyList(),
    recent: [],
    rules: {},
    unauthorised: false,
  };
}

let state: ApprovalsState = initial("");
const listeners = new Set<() => void>();

function set(next: ApprovalsState) {
  state = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Reset if the merchant changed underneath us (a different address, a new tab). */
function current(): ApprovalsState {
  const connection = getConnection();
  if (state.connection !== connection) {
    state = initial(connection);
  }
  return state;
}

// How many 401s in a row before the key is treated as genuinely refused. The engine
// can answer 401 to a perfectly good key when its database is briefly locked (it
// writes "last used" while resolving a key, and a locked write is reported as a
// refusal). Forgetting the key on the first one would silently stop this store's
// polling for the rest of the session and, for a moment's contention, sign the
// merchant out. Three in a row - a minute of polling - is a real refusal.
const REFUSALS_BEFORE_FORGETTING = 3;
let refusals = 0;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const connection = getConnection();
  const key = merchantKey(connection);
  if (!key) throw new NotAuthorised();

  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
      ...init?.headers,
    },
  });

  if (res.status === 401) {
    refusals += 1;
    if (refusals >= REFUSALS_BEFORE_FORGETTING) {
      clearMerchantKey(connection);
    }
    throw new NotAuthorised();
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string" ? body.detail : `request failed (${res.status})`,
    );
  }
  refusals = 0;
  return res.json();
}

/** True once the key has really been given up on - not on a single refusal. */
function keyGone(connection: string): boolean {
  return merchantKey(connection) === null;
}

// -- requests -------------------------------------------------------------

// A later request wins. Without this a slow page-1 response could land after a
// fast page-2 response and put the wrong rows on screen.
let pendingSeq = 0;
let historySeq = 0;

// Bumped whenever a list page is applied. A count-only refresh that started before
// that point is older than what is on screen and is dropped, so a one-row count
// fetch can never cancel, or overwrite, a list load.
let listVersion = 0;

// One request per page at a time. Asking for the page already being fetched joins
// it instead of sending a second identical request - React StrictMode runs effects
// twice in development, and a busy engine answers the duplicate slowly.
let pendingInflight: { offset: number; promise: Promise<void> } | null = null;
let historyInflight: { offset: number; promise: Promise<void> } | null = null;

function describe(e: unknown): string {
  return e instanceof Error ? e.message : "Could not reach the engine.";
}

/** Load (or refresh) a page of pending approvals. Keeps showing older data while it runs. */
export function loadPending(offset?: number, force = false): Promise<void> {
  const at = offset ?? current().pending.offset;
  // `force` is for right after a decision: a request already in flight may have been
  // answered before it, and would put the decided card back on screen.
  if (!force && pendingInflight && pendingInflight.offset === at) {
    return pendingInflight.promise;
  }
  const promise = fetchPending(at).finally(() => {
    if (pendingInflight?.promise === promise) pendingInflight = null;
  });
  pendingInflight = { offset: at, promise };
  return promise;
}

async function fetchPending(offset: number): Promise<void> {
  const before = current();
  const connection = before.connection;
  const at = offset;
  const seq = ++pendingSeq;

  set({
    ...before,
    pending: {
      ...before.pending,
      phase: before.pending.phase === "idle" ? "loading" : before.pending.phase,
      refreshing: true,
    },
  });

  try {
    const page = await call<{
      approvals: ApprovalItem[];
      total: number;
      offset: number;
    }>(`/api/approvals/${connection}?limit=${PAGE_SIZE}&offset=${at}`);
    if (seq !== pendingSeq || state.connection !== connection) return;

    // The queue shrank under a page we were on (someone decided the last row): go
    // back to the last page that exists rather than showing an empty one.
    if (page.approvals.length === 0 && page.total > 0 && at > 0) {
      const lastPage = Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE;
      return loadPending(lastPage);
    }

    listVersion += 1;
    set({
      ...state,
      unauthorised: false,
      pending: {
        phase: "ready",
        items: page.approvals,
        total: page.total,
        offset: page.offset,
        asOf: Date.now(),
        refreshing: false,
        error: null,
      },
    });
  } catch (e) {
    if (seq !== pendingSeq || state.connection !== connection) return;
    const cur = state;
    set({
      ...cur,
      unauthorised: e instanceof NotAuthorised && keyGone(connection) ? true : cur.unauthorised,
      pending: {
        ...cur.pending,
        phase: cur.pending.phase === "loading" ? "error" : cur.pending.phase,
        refreshing: false,
        error: e instanceof NotAuthorised
            ? keyGone(connection)
              ? "Your key was refused."
              : "The engine refused the request just now - retrying."
            : describe(e),
      },
    });
  }
}

/** Refresh only the number - one row fetched - when no list is on screen. */
export async function refreshCount(): Promise<void> {
  const before = current();
  const connection = before.connection;
  const version = listVersion;
  try {
    const page = await call<{ total: number }>(
      `/api/approvals/${connection}?limit=1&offset=0`,
    );
    if (version !== listVersion || state.connection !== connection) return;
    set({
      ...state,
      unauthorised: false,
      pending: {
        ...state.pending,
        // Only the count is known. If there is no list yet the phase stays idle, so
        // the Approvals page still loads its list when opened.
        total: page.total,
        asOf: Date.now(),
        error: null,
        // A count-only refresh must not claim a list it never fetched.
        items: state.pending.items,
      },
    });
  } catch (e) {
    if (version !== listVersion || state.connection !== connection) return;
    const cur = state;
    set({
      ...cur,
      unauthorised: e instanceof NotAuthorised && keyGone(connection) ? true : cur.unauthorised,
      pending: {
        ...cur.pending,
        error: e instanceof NotAuthorised
            ? keyGone(connection)
              ? "Your key was refused."
              : "The engine refused the request just now - retrying."
            : describe(e),
      },
    });
  }
}

export function loadHistory(offset?: number): Promise<void> {
  const at = offset ?? current().history.offset;
  if (historyInflight && historyInflight.offset === at) return historyInflight.promise;
  const promise = fetchHistory(at).finally(() => {
    if (historyInflight?.promise === promise) historyInflight = null;
  });
  historyInflight = { offset: at, promise };
  return promise;
}

async function fetchHistory(at: number): Promise<void> {
  const before = current();
  const connection = before.connection;
  const seq = ++historySeq;

  set({
    ...before,
    history: {
      ...before.history,
      phase: before.history.phase === "idle" ? "loading" : before.history.phase,
      refreshing: true,
    },
  });

  try {
    const page = await call<{
      decisions: DecidedItem[];
      total: number;
      offset: number;
    }>(`/api/approvals/${connection}/history?limit=${PAGE_SIZE}&offset=${at}`);
    if (seq !== historySeq || state.connection !== connection) return;
    set({
      ...state,
      history: {
        phase: "ready",
        items: page.decisions,
        total: page.total,
        offset: page.offset,
        asOf: Date.now(),
        refreshing: false,
        error: null,
      },
    });
  } catch (e) {
    if (seq !== historySeq || state.connection !== connection) return;
    const cur = state;
    set({
      ...cur,
      unauthorised: e instanceof NotAuthorised && keyGone(connection) ? true : cur.unauthorised,
      history: {
        ...cur.history,
        phase: cur.history.phase === "loading" ? "error" : cur.history.phase,
        refreshing: false,
        error: e instanceof NotAuthorised
            ? keyGone(connection)
              ? "Your key was refused."
              : "The engine refused the request just now - retrying."
            : describe(e),
      },
    });
  }
}

/** The engine's own explanation of each risk rule, fetched once. Public route. */
export async function loadRules(): Promise<void> {
  if (Object.keys(current().rules).length > 0) return;
  try {
    const res = await fetch("/api/policy/rules");
    if (!res.ok) return;
    const list: { rule: string; explanation: string }[] = await res.json();
    set({
      ...state,
      rules: Object.fromEntries(list.map((r) => [r.rule, r.explanation])),
    });
  } catch {
    // Without it the page shows the rule's own name. Not worth an error.
  }
}

/**
 * Approve or reject one approval. Throws on failure so the card can say so; the
 * store is left as it was. On success the result is remembered (so it is not lost
 * when the card leaves the queue) and both lists are refreshed.
 */
export async function decide(
  item: ApprovalItem,
  approved: boolean,
  note?: string,
): Promise<Decision> {
  const connection = current().connection;
  const result = await call<Decision>(`/api/approvals/${connection}/${item.approval_id}`, {
    method: "POST",
    body: JSON.stringify({
      approved,
      decided_by: "merchant",
      note: note?.trim() ? note.trim() : null,
    }),
  });

  if (state.connection === connection) {
    const entry: RecentDecision = {
      approvalId: item.approval_id,
      actionType: item.action_type,
      approved,
      result,
      at: Date.now(),
    };
    set({
      ...state,
      recent: [entry, ...state.recent].slice(0, 5),
      // History changed; make the next visit to it fetch again.
      history: { ...state.history, asOf: null },
    });
  }
  await loadPending(undefined, true);
  return result;
}

export function dismissRecent(approvalId: string) {
  set({ ...state, recent: state.recent.filter((r) => r.approvalId !== approvalId) });
}

// -- polling --------------------------------------------------------------

let pollRefs = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let listViewers = 0;

function poll() {
  if (typeof document !== "undefined" && document.hidden) return;
  if (merchantKey(getConnection()) === null) return;
  // With the list on screen refresh the list (and so the count with it); otherwise
  // fetch a single row, which is all the badge needs.
  void (listViewers > 0 ? loadPending() : refreshCount());
}

function onVisible() {
  if (!document.hidden) poll();
}

/**
 * Keep the count live. Reference-counted: the Merchant console starts it once and
 * stops it on unmount. Pauses while the tab is hidden and refreshes the moment it
 * is visible again.
 */
export function startApprovalPolling(): () => void {
  pollRefs += 1;
  if (pollRefs === 1) {
    pollTimer = setInterval(poll, POLL_MS);
    document.addEventListener("visibilitychange", onVisible);
  }
  poll();
  return () => {
    pollRefs -= 1;
    if (pollRefs === 0) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      document.removeEventListener("visibilitychange", onVisible);
    }
  };
}

/** The Approvals page registers while mounted so polling refreshes its list. */
export function registerPendingView(): () => void {
  listViewers += 1;
  return () => {
    listViewers -= 1;
  };
}

// -- hooks ----------------------------------------------------------------

export function useApprovals(): ApprovalsState {
  return useSyncExternalStore(subscribe, () => current());
}

/** How many decisions are waiting, or null when that is not known. Never 0 for "unknown". */
export function useApprovalTotal(): number | null {
  return useSyncExternalStore(subscribe, () => current().pending.total);
}

// A shared clock, so every countdown on the page ticks together and nothing runs a
// timer per card.
let nowValue = Date.now();
let tick: ReturnType<typeof setInterval> | null = null;
const tickListeners = new Set<() => void>();

function subscribeNow(listener: () => void) {
  tickListeners.add(listener);
  if (tickListeners.size === 1) {
    nowValue = Date.now();
    tick = setInterval(() => {
      nowValue = Date.now();
      tickListeners.forEach((l) => l());
    }, 30_000);
  }
  return () => {
    tickListeners.delete(listener);
    if (tickListeners.size === 0 && tick) {
      clearInterval(tick);
      tick = null;
    }
  };
}

/** Milliseconds since epoch, refreshed every 30 seconds. */
export function useNow(): number {
  return useSyncExternalStore(subscribeNow, () => nowValue);
}
