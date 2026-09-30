/**
 * The Operations console's calls to the engine.
 *
 * Operator key only, held for the browser session (see `api.ts`: it is typed, never
 * built in, because this bundle is shared with the shopper storefront). Nothing here
 * can decide or change anything - these are reads, plus the read-only copilot.
 *
 * A second client rather than more of `ops_api` in `api.ts`: that file is shared
 * with the rest of the storefront and carries the old approval-queue calls this
 * console no longer makes.
 */

import { NotAuthorised, clearOperatorKey, operatorKey } from "./api";
import type { Connection, CopilotAnswer, OpsHandover, OpsStats } from "./api";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const key = operatorKey();
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
    // A refused key is worth forgetting: keeping it would fail every later request
    // with no way for the operator to correct it.
    clearOperatorKey();
    throw new NotAuthorised();
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string" ? body.detail : `request failed (${res.status})`,
    );
  }
  return res.json();
}

export interface HandoverPage {
  handovers: OpsHandover[];
  /** Every open handover across merchants, not the size of this page. */
  total: number;
}

/** How many handovers one request asks for. `total` says when there are more. */
export const HANDOVER_PAGE = 50;

export const opsApi = {
  /** Open handovers, oldest first. */
  handovers: () =>
    call<HandoverPage>(`/api/ops/handovers?offset=0&limit=${HANDOVER_PAGE}`),

  /** Workload counts. `by_merchant` is keyed by display name, not id. */
  stats: () => call<OpsStats>("/api/ops/stats"),

  /** Every merchant connection. Public route: lists what a storefront already shows. */
  connections: async (): Promise<Connection[]> => {
    const res = await fetch("/api/connections");
    if (!res.ok) throw new Error(`request failed (${res.status})`);
    return res.json();
  },

  askCopilot: (question: string) =>
    call<CopilotAnswer>("/api/ops/copilot", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
};
