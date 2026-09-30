import { useEffect, useState } from "react";
import { console_api } from "./api";
import { useApprovalTotal } from "./merchantApprovalsStore";
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
  // Every decision waiting on the merchant - not only payment recovery - counted
  // from the same store as the nav badge and the Approvals page, so this banner can
  // never disagree with them. Null is "not known yet", which shows no banner rather
  // than claiming nothing is waiting.
  const approvalsWaiting = useApprovalTotal();
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
    approvalsWaiting
      ? {
          text:
            approvalsWaiting === 1
              ? "1 decision is waiting for your approval."
              : `${approvalsWaiting} decisions are waiting for your approval.`,
          buttonLabel: "Review Approvals",
          onClick: () => onNavigate("approvals"),
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
