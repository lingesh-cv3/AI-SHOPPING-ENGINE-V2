/**
 * What Operations can and cannot see today.
 *
 * HAND-MAINTAINED, and shown to the operator as exactly that. It is a statement of
 * which data sources exist, not a live reading of them, so it can drift from the
 * engine if a source is added and this file is not. A server-provided detector
 * registry replaces it once one exists; until then, changing what Operations can
 * see means changing this list in the same commit.
 *
 * Kept in its own module so the claim is reviewed as a claim, and so a test can
 * assert that nothing here overstates what the screen does.
 */

export const COVERAGE = {
  sees: [
    "Shopper cases that needed a person (handovers), across merchants",
    "How many decisions each merchant has waiting, as a count",
    "Each connection's automation mode and the platform operations it does not support",
  ],
  cannotSee: [
    "API and webhook health, latency, or error rates",
    "Order and payment failures",
    "Stuck or failed executions",
    "Inventory and catalogue sync",
    "Incidents and alerts",
    "Revenue impact",
  ],
} as const;
