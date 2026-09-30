import { OperationsShell } from "./OperationsShell";

/**
 * Not the old Operations console any more.
 *
 * The approval queue, the "already decided" history and the handover panel that
 * lived here were removed when Operations moved to OperationsShell: merchant-owned
 * approvals belong to the merchant, in their own Approvals section.
 *
 * This file survives only because `App.tsx` still imports it, for a
 * `view === "queue"` branch nothing can reach (no code ever sets that view).
 * `App.tsx` carries uncommitted Shopper V2 changes and is deliberately untouched, so
 * the import cannot be removed yet. Delete this file together with that branch.
 */
export function OpsConsole() {
  return <OperationsShell />;
}
