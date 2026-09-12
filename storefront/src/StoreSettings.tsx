import { useState } from "react";
import type { ActionInfo, Pipeline, Policy } from "./api";
import { Gates } from "./Gates";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/**
 * Store > Settings. Extracted from `MerchantConsole.tsx`'s former sidebar,
 * same logic, unchanged - see that file's history for why this exists as
 * an informational "what runs on its own vs. what waits for you" view
 * rather than an allowlist: the risk gate already decides safety, and a
 * second tick-box gate on top of it added no safety, only friction.
 *
 * `approval_timeout_minutes` is now genuinely mutable, 1-120 minutes. It
 * used to be shown read-only because `PUT /api/policy/{id}` silently reset
 * it to a hardcoded default on every unrelated save (fixed in
 * `engine/api/routes.py::set_policy` - see that function's own docstring
 * for the bug this was) - a real fix, not just an unlocked control.
 */
export function StoreSettings({
  policy,
  actions,
  test,
  tested,
  onMode,
  onBlock,
  onHoldout,
  onApprovalTimeout,
  onNavigate,
}: {
  policy: Policy;
  actions: ActionInfo[];
  test: Pipeline | null;
  tested: string | null;
  onMode: (mode: string) => void;
  onBlock: (actionType: string, blocked: boolean) => void;
  onHoldout: (percent: number) => void;
  onApprovalTimeout: (minutes: number) => void;
  onNavigate: (section: string) => void;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div className="overview-header">
        <div>
          <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Settings</h2>
          <p className="overview-subtitle">
            What runs on its own, what always waits for you, and holdout/approval timing.
          </p>
        </div>
      </div>

      <PolicyEditor
        policy={policy}
        actions={actions}
        onMode={onMode}
        onBlock={onBlock}
        onHoldout={onHoldout}
        onApprovalTimeout={onApprovalTimeout}
      />

      {test && (
        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">What happens now</span>
            <span className="eyebrow">{tested?.replace(/_/g, " ")}</span>
          </div>
          <div className="panel-body">
            <Gates pipeline={test} />
          </div>
        </section>
      )}

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}

function PolicyEditor({
  policy,
  actions,
  onMode,
  onBlock,
  onHoldout,
  onApprovalTimeout,
}: {
  policy: Policy;
  actions: ActionInfo[];
  onMode: (mode: string) => void;
  onBlock: (actionType: string, blocked: boolean) => void;
  onHoldout: (percent: number) => void;
  onApprovalTimeout: (minutes: number) => void;
}) {
  const [holdoutDraft, setHoldoutDraft] = useState(
    String(policy.holdout_percent),
  );
  const [savingHoldout, setSavingHoldout] = useState(false);

  const [seenHoldoutPercent, setSeenHoldoutPercent] = useState(
    policy.holdout_percent,
  );
  if (policy.holdout_percent !== seenHoldoutPercent) {
    setSeenHoldoutPercent(policy.holdout_percent);
    setHoldoutDraft(String(policy.holdout_percent));
  }

  const [timeoutDraft, setTimeoutDraft] = useState(
    String(policy.approval_timeout_minutes),
  );
  const [savingTimeout, setSavingTimeout] = useState(false);
  const [seenTimeout, setSeenTimeout] = useState(
    policy.approval_timeout_minutes,
  );
  if (policy.approval_timeout_minutes !== seenTimeout) {
    setSeenTimeout(policy.approval_timeout_minutes);
    setTimeoutDraft(String(policy.approval_timeout_minutes));
  }

  const modes: Array<[string, string, string]> = [
    ["CAUTIOUS", "Cautious", "Every action waits for you"],
    ["STANDARD", "Standard", "Safe actions run on their own"],
    ["SUSPENDED", "Paused", "Nothing runs at all"],
  ];

  const meaningful = actions.filter(
    (a) => !["NO_ACTION", "ESCALATE_TO_HUMAN"].includes(a.action_type),
  );
  const automatic = meaningful.filter((a) => a.can_ever_be_automatic);
  const gated = meaningful.filter((a) => !a.can_ever_be_automatic);

  const why = (a: ActionInfo) =>
    a.financial
      ? "moves money"
      : a.touches_customer_data
        ? "contacts your customer"
        : "cannot be undone";

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Your settings</span>
      </div>
      <div className="panel-body">
        <div className="gate-label" style={{ marginBottom: 6 }}>
          Automation
        </div>
        <div className="modes">
          {modes.map(([value, label, hint]) => (
            <button
              key={value}
              className="mode"
              aria-pressed={policy.mode === value}
              onClick={() => onMode(value)}
            >
              <span className="mode-label">{label}</span>
              <span className="mode-hint">{hint}</span>
            </button>
          ))}
        </div>

        <ActionList
          title={
            policy.mode === "STANDARD"
              ? "Runs on its own"
              : "Would run on its own in Standard"
          }
          note="Safe, reversible, and never touches money."
          actions={automatic}
          policy={policy}
          onBlock={onBlock}
        />

        <ActionList
          title="Always waits for you"
          note="No setting can change these."
          actions={gated}
          policy={policy}
          onBlock={onBlock}
          reason={why}
        />

        <p className="note">
          Switching something off means the engine will never do it here &mdash; it
          won&rsquo;t even ask.
        </p>

        <div className="gate-label" style={{ marginTop: 18, marginBottom: 6 }}>
          Holdout
        </div>
        <p className="note" style={{ marginTop: 0 }}>
          Hold back a percentage of new sessions from getting any help at all,
          so their outcome can be compared against the ones the engine did
          assist. This is how you tell a recovered sale from one that would
          have happened anyway.
        </p>
        <div className="field">
          <input
            type="number"
            min={0}
            max={100}
            value={holdoutDraft}
            onChange={(e) => setHoldoutDraft(e.target.value)}
            aria-label="Holdout percentage"
            style={{ maxWidth: 90 }}
          />
          <button
            disabled={
              savingHoldout ||
              holdoutDraft === String(policy.holdout_percent) ||
              Number.isNaN(Number(holdoutDraft))
            }
            onClick={async () => {
              setSavingHoldout(true);
              try {
                const clamped = Math.max(
                  0,
                  Math.min(100, Math.round(Number(holdoutDraft) || 0)),
                );
                onHoldout(clamped);
              } finally {
                setSavingHoldout(false);
              }
            }}
          >
            {savingHoldout ? "Saving…" : "Save"}
          </button>
        </div>
        {policy.holdout_percent > 0 && (
          <p className="note">
            {policy.holdout_percent}% of new sessions get no assistance right
            now.
          </p>
        )}

        <div className="gate-label" style={{ marginTop: 18, marginBottom: 6 }}>
          Approval timeout
        </div>
        <p className="note" style={{ marginTop: 0 }}>
          How long a pending approval waits before it expires and the
          shopper is told nobody got to it in time. A shopper staring at a
          declined card will not wait long - this cannot be set above two
          hours.
        </p>
        <div className="field">
          <input
            type="number"
            min={1}
            max={120}
            value={timeoutDraft}
            onChange={(e) => setTimeoutDraft(e.target.value)}
            aria-label="Approval timeout minutes"
            style={{ maxWidth: 90 }}
          />
          <button
            disabled={
              savingTimeout ||
              timeoutDraft === String(policy.approval_timeout_minutes) ||
              Number.isNaN(Number(timeoutDraft))
            }
            onClick={async () => {
              setSavingTimeout(true);
              try {
                const clamped = Math.max(
                  1,
                  Math.min(120, Math.round(Number(timeoutDraft) || 15)),
                );
                onApprovalTimeout(clamped);
              } finally {
                setSavingTimeout(false);
              }
            }}
          >
            {savingTimeout ? "Saving…" : "Save"}
          </button>
        </div>
        <p className="note">
          {policy.approval_timeout_minutes} minute
          {policy.approval_timeout_minutes === 1 ? "" : "s"} today.
        </p>
      </div>
    </section>
  );
}

function ActionList({
  title,
  note,
  actions,
  policy,
  onBlock,
  reason,
}: {
  title: string;
  note: string;
  actions: ActionInfo[];
  policy: Policy;
  onBlock: (actionType: string, blocked: boolean) => void;
  reason?: (a: ActionInfo) => string;
}) {
  if (actions.length === 0) return null;

  return (
    <>
      <div className="gate-label" style={{ margin: "18px 0 3px" }}>
        {title}
      </div>
      <p className="note" style={{ margin: "0 0 8px" }}>
        {note}
      </p>
      <div className="checks">
        {actions.map((a) => {
          const off = policy.blocked.includes(a.action_type);
          return (
            <div key={a.action_type} className={off ? "actrow off" : "actrow"}>
              <span>
                <span className="check-name">
                  {a.action_type.replace(/_/g, " ").toLowerCase()}
                </span>
                {reason && <span className="check-why">{reason(a)}</span>}
              </span>
              <button
                className="offswitch"
                aria-pressed={off}
                onClick={() => onBlock(a.action_type, !off)}
              >
                {off ? "Off" : "On"}
              </button>
            </div>
          );
        })}
      </div>
    </>
  );
}
