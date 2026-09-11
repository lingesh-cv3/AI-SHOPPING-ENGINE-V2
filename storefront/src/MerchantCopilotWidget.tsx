import { useEffect, useState } from "react";
import { console_api, RECOVERY_ACTIONS } from "./api";
import { Copilot } from "./Copilot";

/** A handful of real questions the data can actually answer, shown as
 *  starter chips so a merchant sees what this can do before typing
 *  anything - the same job a placeholder was doing, done better. */
const SUGGESTED_QUESTIONS = [
  "How many shoppers did the engine help this month?",
  "What's out of stock right now?",
  "What are shoppers searching for that we don't carry?",
  "What's waiting on me right now?",
  "What should I pay attention to today?",
  "What can this platform do?",
];

/** The Merchant Copilot, factored out of MerchantConsole so it can be
 *  embedded directly on Overview (the reference design's "Copilot should
 *  feel like part of the product, not a standalone page") as well as kept
 *  as its own dedicated nav section for focused Q&A - the same component,
 *  never two copies that could drift.
 */
export function MerchantCopilot({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  // A merchant asking the copilot "what's out of stock" was told, with
  // nothing to do about it from that screen - the read-only-twin gap
  // CLAUDE.md names against the value bar (PROGRESS.md, "CV3 Merchant
  // Copilot"). The payments/recovery slice of that gap already has a real
  // action attached elsewhere (PaymentsPanel, approve/reject through the
  // risk gate) - what was still missing was the copilot ever pointing a
  // merchant there. Reads the identical queue PaymentsPanel itself reads,
  // filtered by the identical RECOVERY_ACTIONS set, so this can never
  // disagree with what that panel shows or claim a count PaymentsPanel
  // itself would not act on.
  const [pendingRecoveryCount, setPendingRecoveryCount] = useState<
    number | null
  >(null);
  // The second (catalogue/inventory/unmet-demand) slice of the same
  // read-only-twin gap - closed by a real, persisted MerchantTask record
  // rather than a fake inventory-write action (see
  // engine/db/models.py::MerchantTask). Fetched independently of asking
  // the Copilot anything, from the same /api/tasks route the Tasks panel
  // itself reads (and which itself triggers the deterministic sync), so
  // this can never disagree with what that panel shows.
  const [openTaskCount, setOpenTaskCount] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    console_api
      .queue()
      .then((q) => {
        if (cancelled) return;
        const count = q.approvals.filter(
          (a) => a.financial && RECOVERY_ACTIONS.has(a.action_type),
        ).length;
        setPendingRecoveryCount(count);
      })
      .catch(() => {
        // No count is honest here - "0" would claim nothing is waiting,
        // which is a different and stronger claim than "couldn't check".
      });
    console_api
      .tasks("OPEN")
      .then((r) => {
        if (cancelled) return;
        setOpenTaskCount(r.tasks_open_count);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const banners = [
    pendingRecoveryCount
      ? {
          text:
            pendingRecoveryCount === 1
              ? "1 payment recovery case is waiting for your approval."
              : `${pendingRecoveryCount} payment recovery cases are waiting for your approval.`,
          buttonLabel: "Review in Payments & Checkout",
          onClick: () => onNavigate("payments"),
        }
      : null,
    openTaskCount
      ? {
          text:
            openTaskCount === 1
              ? "1 merchant task needs your attention."
              : `${openTaskCount} merchant tasks need your attention.`,
          buttonLabel: "Review Merchant Tasks",
          onClick: () => onNavigate("tasks"),
        }
      : null,
  ].filter((b): b is NonNullable<typeof b> => b !== null);

  return (
    <Copilot
      eyebrow="Merchant copilot"
      title="Ask about your store"
      intro="Ask in plain language - shoppers helped, what's out of stock, what people search for and can't find, revenue and product performance, payment recovery, holdout results, or what this platform can do. Answers are grounded in your real data, never guessed."
      suggestions={SUGGESTED_QUESTIONS}
      placeholder="Ask a question about your store…"
      ask={console_api.askCopilot}
      actionBanners={banners}
    />
  );
}
