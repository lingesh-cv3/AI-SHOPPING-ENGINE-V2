/**
 * The Operations Overview's data: three independent panels, one store.
 *
 * Each panel loads, fails and goes stale on its own. One slow or failing call does
 * not blank the others (the old console fetched four things in one Promise.all and
 * replaced the whole page with "Could not reach the engine" if any failed), and
 * none of them renders a number until it has really been fetched - "loading" and
 * "could not load" are never drawn as zero.
 *
 * A failed refresh keeps the last good data and says it is stale.
 */

import { useSyncExternalStore } from "react";
import { NotAuthorised } from "./api";
import type { Connection, OpsStats } from "./api";
import { opsApi, type HandoverPage } from "./opsApi";

export interface Panel<T> {
  /** idle: never asked. loading: first fetch in flight. ready: has data. error: first fetch failed. */
  phase: "idle" | "loading" | "ready" | "error";
  data: T | null;
  /** When the data on screen was fetched, ms since epoch. */
  asOf: number | null;
  refreshing: boolean;
  /** The last fetch failed. Any data shown is stale. */
  error: string | null;
}

export interface OverviewState {
  handovers: Panel<HandoverPage>;
  stats: Panel<OpsStats>;
  connections: Panel<Connection[]>;
  /** The engine refused the operator key: back to sign-in. */
  signedOut: boolean;
}

const POLL_MS = 30_000;

function panel<T>(): Panel<T> {
  return { phase: "idle", data: null, asOf: null, refreshing: false, error: null };
}

function initial(): OverviewState {
  return {
    handovers: panel(),
    stats: panel(),
    connections: panel(),
    signedOut: false,
  };
}

let state: OverviewState = initial();
const listeners = new Set<() => void>();

function set(next: OverviewState) {
  state = next;
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

type PanelKey = "handovers" | "stats" | "connections";

// A later request for the same panel wins, and a panel already being fetched is
// joined rather than asked twice.
const seq: Record<PanelKey, number> = { handovers: 0, stats: 0, connections: 0 };
const inflight: Partial<Record<PanelKey, Promise<void>>> = {};

function load<K extends PanelKey>(
  key: K,
  fetcher: () => Promise<NonNullable<OverviewState[K]["data"]>>,
): Promise<void> {
  const existing = inflight[key];
  if (existing) return existing;

  const promise = (async () => {
    const mine = ++seq[key];
    const before = state[key];
    set({
      ...state,
      [key]: {
        ...before,
        phase: before.phase === "idle" ? "loading" : before.phase,
        refreshing: true,
      },
    });
    try {
      const data = await fetcher();
      if (mine !== seq[key]) return;
      set({
        ...state,
        [key]: { phase: "ready", data, asOf: Date.now(), refreshing: false, error: null },
      });
    } catch (e) {
      if (mine !== seq[key]) return;
      const cur = state[key];
      set({
        ...state,
        signedOut: e instanceof NotAuthorised ? true : state.signedOut,
        [key]: {
          ...cur,
          phase: cur.phase === "loading" ? "error" : cur.phase,
          refreshing: false,
          error: e instanceof NotAuthorised
            ? "The engine refused the operator key."
            : e instanceof Error
              ? e.message
              : "Could not reach the engine.",
        },
      });
    }
  })().finally(() => {
    // Only clear our own entry: a reset may already have replaced it.
    if (inflight[key] === promise) delete inflight[key];
  });

  inflight[key] = promise;
  return promise;
}

/** Refresh every panel. Each succeeds or fails on its own. */
export function refreshOverview(): Promise<unknown> {
  return Promise.all([
    load("handovers", () => opsApi.handovers()),
    load("stats", () => opsApi.stats()),
    load("connections", () => opsApi.connections()),
  ]);
}

/** Forget everything: after signing in or out, so nothing from one session shows in the next. */
export function resetOverview() {
  seq.handovers += 1;
  seq.stats += 1;
  seq.connections += 1;
  delete inflight.handovers;
  delete inflight.stats;
  delete inflight.connections;
  set(initial());
}

// -- polling --------------------------------------------------------------

let refs = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function tick() {
  if (document.hidden) return;
  void refreshOverview();
}

function onVisible() {
  if (!document.hidden) tick();
}

/** Keep the Overview fresh while it is on screen. Pauses while the tab is hidden. */
export function startOverviewPolling(): () => void {
  refs += 1;
  if (refs === 1) {
    timer = setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", onVisible);
  }
  void refreshOverview();
  return () => {
    refs -= 1;
    if (refs === 0) {
      if (timer) clearInterval(timer);
      timer = null;
      document.removeEventListener("visibilitychange", onVisible);
    }
  };
}

export function useOverview(): OverviewState {
  return useSyncExternalStore(subscribe, () => state);
}
