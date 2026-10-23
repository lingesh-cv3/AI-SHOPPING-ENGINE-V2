/**
 * The merchant's shopper follow-ups: one client, one store, one count.
 *
 * A shopper was told a person from the shop would follow up. That follow-up is the
 * merchant's - it is their customer - and this store is how the console sees their
 * own open ones, reads the detail, and closes them. The nav badge and the page read
 * the same number from here, so they cannot disagree.
 *
 * Honest by construction, the same way the approvals store is:
 *   - `total` is null until it has really been fetched. Nothing renders "0 waiting"
 *     for "not loaded yet" or "could not check".
 *   - a failed refresh keeps the last good data and says it is stale.
 *   - a response that arrives after a newer request, or after the merchant changed,
 *     is dropped; a request already in flight is joined, not repeated.
 *
 * Reading and closing touch no platform, so this works while the merchant's platform
 * is down. This file decides nothing: closing is one POST, and what it is allowed to
 * do is enforced on the server.
 */

import { useSyncExternalStore } from "react";
import {
  NotAuthorised,
  clearMerchantKey,
  getConnection,
  merchantKey,
} from "./api";

export const PAGE_SIZE = 25;
const POLL_MS = 20_000;

/** One row of the list, as `GET /api/handovers/{connection}` returns it. */
export interface FollowUp {
  case_id: string;
  created_at: string;
  /** Null for a follow-up that has been handled. */
  waiting_minutes: number | null;
  friction_type: string | null;
  diagnosis: string | null;
  evidence: string[];
  used_model: boolean;
  shopper_reply: string | null;
  rejected: { action_type: string; reason: string; detail: string }[];
  order_id: string | null;
  query: string | null;
  has_conversation: boolean;
  handled_at: string | null;
  handled_by: string | null;
  handled_note: string | null;
}

export interface FollowUpDetail {
  case_id: string;
  conversation: { speaker: string; text: string; at: string | null }[];
  conversation_limit: number;
  shopper: {
    account: { display_name: string; username: string; email: string | null } | null;
    /** Why there is no account, when there is none. */
    why: string | null;
  };
}

interface ListState {
  /** idle: never asked. loading: first fetch in flight. ready: has data. error: first fetch failed. */
  phase: "idle" | "loading" | "ready" | "error";
  items: FollowUp[];
  total: number | null;
  offset: number;
  asOf: number | null;
  refreshing: boolean;
  /** The last fetch failed. Any data shown is stale. */
  error: string | null;
}

interface DetailState {
  phase: "loading" | "ready" | "error";
  data: FollowUpDetail | null;
  error: string | null;
}

export interface RecentClose {
  caseId: string;
  /** Whether the shopper was sent a message. */
  toldShopper: boolean;
  /** The merchant said they had already contacted the shopper directly. */
  contactedDirectly: boolean;
  /** Someone else had already closed it. */
  alreadyClosed: boolean;
  at: number;
}

export interface FollowUpsState {
  connection: string;
  open: ListState;
  handled: ListState;
  details: Record<string, DetailState>;
  recent: RecentClose[];
  unauthorised: boolean;
}

function emptyList(): ListState {
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

function initial(connection: string): FollowUpsState {
  return {
    connection,
    open: emptyList(),
    handled: emptyList(),
    details: {},
    recent: [],
    unauthorised: false,
  };
}

let state: FollowUpsState = initial("");
const listeners = new Set<() => void>();

function set(next: FollowUpsState) {
  state = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Reset if the merchant changed underneath us. */
function current(): FollowUpsState {
  const connection = getConnection();
  if (state.connection !== connection) state = initial(connection);
  return state;
}

// Three 401s in a row before the key is forgotten. The engine has answered 401 to a
// good key when its database was briefly locked; forgetting it on the first would
// silently end the session and stop this store's polling.
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
    if (refusals >= REFUSALS_BEFORE_FORGETTING) clearMerchantKey(connection);
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

function keyGone(connection: string): boolean {
  return merchantKey(connection) === null;
}

function describe(e: unknown, connection: string): string {
  if (e instanceof NotAuthorised) {
    return keyGone(connection)
      ? "Your key was refused."
      : "The engine refused the request just now - retrying.";
  }
  return e instanceof Error ? e.message : "Could not reach the engine.";
}

// -- lists ------------------------------------------------------------------

type Which = "open" | "handled";

const seq: Record<Which, number> = { open: 0, handled: 0 };
// Bumped whenever a list page is applied, so an older one-row count refresh is
// dropped rather than overwriting it (or cancelling it).
let openVersion = 0;
const inflight: Partial<Record<Which, { offset: number; promise: Promise<void> }>> = {};

export function loadList(which: Which, offset?: number, force = false): Promise<void> {
  const at = offset ?? current()[which].offset;
  const existing = inflight[which];
  if (!force && existing && existing.offset === at) return existing.promise;
  const promise = fetchList(which, at).finally(() => {
    if (inflight[which]?.promise === promise) delete inflight[which];
  });
  inflight[which] = { offset: at, promise };
  return promise;
}

async function fetchList(which: Which, at: number): Promise<void> {
  const before = current();
  const connection = before.connection;
  const mine = ++seq[which];

  set({
    ...before,
    [which]: {
      ...before[which],
      phase: before[which].phase === "idle" ? "loading" : before[which].phase,
      refreshing: true,
    },
  });

  try {
    const page = await call<{ follow_ups: FollowUp[]; total: number; offset: number }>(
      `/api/handovers/${connection}?handled=${which === "handled"}&limit=${PAGE_SIZE}&offset=${at}`,
    );
    if (mine !== seq[which] || state.connection !== connection) return;

    // The list shrank under a page we were on (someone closed the last row): go back
    // to the last page that exists rather than showing an empty one.
    if (page.follow_ups.length === 0 && page.total > 0 && at > 0) {
      return loadList(which, Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE, true);
    }

    if (which === "open") openVersion += 1;
    set({
      ...state,
      unauthorised: false,
      [which]: {
        phase: "ready",
        items: page.follow_ups,
        total: page.total,
        offset: page.offset,
        asOf: Date.now(),
        refreshing: false,
        error: null,
      },
    });
  } catch (e) {
    if (mine !== seq[which] || state.connection !== connection) return;
    const cur = state;
    set({
      ...cur,
      unauthorised:
        e instanceof NotAuthorised && keyGone(connection) ? true : cur.unauthorised,
      [which]: {
        ...cur[which],
        phase: cur[which].phase === "loading" ? "error" : cur[which].phase,
        refreshing: false,
        error: describe(e, connection),
      },
    });
  }
}

/** Refresh only the open count - one row fetched - when no list is on screen. */
export async function refreshCount(): Promise<void> {
  const before = current();
  const connection = before.connection;
  const version = openVersion;
  try {
    const page = await call<{ total: number }>(
      `/api/handovers/${connection}?handled=false&limit=1&offset=0`,
    );
    if (version !== openVersion || state.connection !== connection) return;
    set({
      ...state,
      unauthorised: false,
      open: { ...state.open, total: page.total, asOf: Date.now(), error: null },
    });
  } catch (e) {
    if (version !== openVersion || state.connection !== connection) return;
    const cur = state;
    set({
      ...cur,
      unauthorised:
        e instanceof NotAuthorised && keyGone(connection) ? true : cur.unauthorised,
      open: { ...cur.open, error: describe(e, connection) },
    });
  }
}

// -- detail -------------------------------------------------------------------

export async function loadDetail(caseId: string): Promise<void> {
  const connection = current().connection;
  set({
    ...state,
    details: { ...state.details, [caseId]: { phase: "loading", data: null, error: null } },
  });
  try {
    const data = await call<FollowUpDetail>(`/api/handovers/${connection}/${caseId}`);
    if (state.connection !== connection) return;
    set({
      ...state,
      details: { ...state.details, [caseId]: { phase: "ready", data, error: null } },
    });
  } catch (e) {
    if (state.connection !== connection) return;
    set({
      ...state,
      details: {
        ...state.details,
        [caseId]: {
          phase: "error",
          data: null,
          error:
            e instanceof Error && !(e instanceof NotAuthorised)
              ? e.message
              : describe(e, connection),
        },
      },
    });
  }
}

// -- closing ---------------------------------------------------------------------

/**
 * Close one follow-up. Throws on failure so the card can say so, leaving the store as
 * it was. `note` goes to the shopper verbatim; with none, `contactedDirectly` records
 * that the merchant reached the shopper another way (the server is told nothing extra:
 * the requirement to choose one is the console's, and the API stays permissive).
 */
export async function closeFollowUp(
  caseId: string,
  options: { note: string; contactedDirectly: boolean },
): Promise<RecentClose> {
  const connection = current().connection;
  const note = options.note.trim();
  const result = await call<{ changed: boolean; told_shopper: boolean }>(
    `/api/handovers/${connection}/${caseId}/close`,
    { method: "POST", body: JSON.stringify({ note: note || null }) },
  );

  const entry: RecentClose = {
    caseId,
    toldShopper: result.told_shopper,
    contactedDirectly: !note && options.contactedDirectly,
    alreadyClosed: !result.changed,
    at: Date.now(),
  };
  if (state.connection === connection) {
    set({
      ...state,
      recent: [entry, ...state.recent].slice(0, 5),
      // The handled list changed; make the next visit to it fetch again.
      handled: { ...state.handled, asOf: null },
    });
  }
  await loadList("open", undefined, true);
  return entry;
}

export function dismissRecent(caseId: string) {
  set({ ...state, recent: state.recent.filter((r) => r.caseId !== caseId) });
}

// -- polling ---------------------------------------------------------------------

let pollRefs = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let listViewers = 0;

function poll() {
  if (typeof document !== "undefined" && document.hidden) return;
  if (merchantKey(getConnection()) === null) return;
  void (listViewers > 0 ? loadList("open") : refreshCount());
}

function onVisible() {
  if (!document.hidden) poll();
}

/** Keep the count live on every Merchant screen. Pauses while the tab is hidden. */
export function startFollowUpPolling(): () => void {
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

/** The page registers while mounted so polling refreshes its list. */
export function registerOpenView(): () => void {
  listViewers += 1;
  return () => {
    listViewers -= 1;
  };
}

// -- hooks -----------------------------------------------------------------------

export function useFollowUps(): FollowUpsState {
  return useSyncExternalStore(subscribe, () => current());
}

/** How many follow-ups are open, or null when that is not known. Never 0 for "unknown". */
export function useFollowUpTotal(): number | null {
  return useSyncExternalStore(subscribe, () => current().open.total);
}
