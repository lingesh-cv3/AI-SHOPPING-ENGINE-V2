import { NotAuthorised, getConnection, merchantKey } from "./api";
import { MerchantSignIn } from "./MerchantSignIn";
import { useCallback, useEffect, useState } from "react";
import {
  console_api,
  type ActionInfo,
  type Capabilities,
  type Pipeline,
  type Policy,
  type Rule,
} from "./api";
import { Copilot } from "./Copilot";
import { Gates } from "./Gates";
import { MerchantReport } from "./MerchantReport";

/** A handful of real questions the data can actually answer, shown as
 *  starter chips so a merchant sees what this can do before typing
 *  anything - the same job a placeholder was doing, done better. */
const SUGGESTED_QUESTIONS = [
  "How many shoppers did the engine help this month?",
  "What's out of stock right now?",
  "What are shoppers searching for that we don't carry?",
  "What's waiting on me right now?",
  "What can this platform do?",
];

function MerchantCopilot() {
  return (
    <Copilot
      eyebrow="Merchant copilot"
      title="Ask about your store"
      intro="Ask in plain language - shoppers helped, what's out of stock, what people search for and can't find, or what this platform can do. Answers are grounded in your real data, never guessed."
      suggestions={SUGGESTED_QUESTIONS}
      placeholder="Ask a question about your store…"
      ask={console_api.askCopilot}
    />
  );
}
/**
 * The merchant's own view of how the engine behaves on their store.
 *
 * Two things here are worth reading carefully.
 *
 * The capability list is not a feature checklist we wrote. It comes from the
 * adapter, which reports what this merchant's platform can actually do. When it
 * says payment recovery is unavailable, that is a fact about their backend, not a
 * limitation of ours.
 *
 * The policy editor lets a merchant tick money-touching actions even though the
 * engine will refuse to automate them. That is deliberate. Hiding the option
 * would make the guarantee invisible; showing the override makes it something a
 * merchant can verify for themselves.
 */
export function MerchantConsole() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [actions, setActions] = useState<ActionInfo[]>([]);
  const [test, setTest] = useState<Pipeline | null>(null);
  const [tested, setTested] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Signed out is the normal first state, not a failure, so it is tracked
  // separately from `error`.
  const [signedIn, setSignedIn] = useState(
    () => merchantKey(getConnection()) !== null,
  );

  const refresh = useCallback(async () => {
    try {
      const [c, p, r, a] = await Promise.all([
        console_api.capabilities(),
        console_api.policy(),
        console_api.rules(),
        console_api.actions(),
      ]);
      setCaps(c);
      setPolicy(p);
      setRules(r);
      setActions(a);
    } catch (e) {
      // A refused key is not an outage. Conflating them has a merchant restarting
      // services when they need to sign in.
      if (e instanceof NotAuthorised) {
        setSignedIn(false);
        return;
      }
      setError("Could not reach the engine. Is it running on port 8000?");
    }
  }, []);

  useEffect(() => {
    if (!signedIn) return;
    refresh();
  }, [refresh, signedIn]);

  async function setMode(mode: string) {
    if (!policy) return;
    setPolicy(
      await console_api.savePolicy(mode, [], policy.blocked, policy.holdout_percent),
    );
    setTest(null);
  }

  async function onBlock(actionType: string, blocked: boolean) {
    if (!policy) return;
    const next = blocked
      ? [...policy.blocked, actionType]
      : policy.blocked.filter((a) => a !== actionType);
    // auto_allowed is left empty deliberately. It is now an optional restriction
    // rather than a permission list, and most merchants want every safe action.
    setPolicy(
      await console_api.savePolicy(policy.mode, [], next, policy.holdout_percent),
    );
    setTested(actionType);
    setTest(await console_api.testAction(actionType));
  }

  async function onHoldout(percent: number) {
    if (!policy) return;
    setPolicy(
      await console_api.savePolicy(policy.mode, [], policy.blocked, percent),
    );
  }
  if (!signedIn) {
    return (
      <MerchantSignIn
        connectionId={getConnection()}
        merchantName={caps?.platform ?? getConnection()}
        onSignedIn={() => {
          setSignedIn(true);
          setError(null);
        }}
      />
    );
  }

  if (error) return <p className="empty">{error}</p>;
  if (!caps || !policy) return <p className="empty">Loading…</p>;

  const unsupported = caps.operations.filter((o) => !o.supported);

  return (
    <div className="layout">
      <main style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <MerchantCopilot />
        <MerchantReport />

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">What your store can do</span>
            <span className="eyebrow">{caps.platform}</span>
          </div>
          <div className="panel-body">
            <p className="note" style={{ marginTop: 0, marginBottom: 12 }}>
              Read directly from your platform. The engine never attempts anything
              not listed as available here.
            </p>
            <div className="caps">
              {caps.operations.map((op) => (
                <div key={op.operation} className="cap">
                  <span className={`dot ${op.supported ? "on" : "off"}`} />
                  <div>
                    <div className="cap-name">
                      {op.operation.replace(/([A-Z])/g, " $1").toLowerCase()}
                    </div>
                    {op.reason && <div className="gate-note">{op.reason}</div>}
                  </div>
                </div>
              ))}
            </div>
            {unsupported.length > 0 && (
              <p className="note">
                {unsupported.length} of {caps.operations.length} operations are
                unavailable on your platform. When the engine needs one of them, it
                hands the case to a person instead of guessing.
              </p>
            )}
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <span className="eyebrow">How decisions are made</span>
            <span className="eyebrow">first match wins</span>
          </div>
          <div className="panel-body">
            <ol className="rules">
              {rules.map((r) => (
                <li key={r.rule}>
                  <span className="num rule-n">
                    {String(r.order).padStart(2, "0")}
                  </span>
                  <span>{r.explanation}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>
      </main>

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        <PolicyEditor
          policy={policy}
          actions={actions}
          onMode={setMode}
          onBlock={onBlock}
          onHoldout={onHoldout}
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
      </div>
    </div>
  );
}





/**
 * The policy editor.
 *
 * Rebuilt after the risk gate changed. It used to be an allowlist - tick an action
 * to let it run - which was a second gate on top of one that already works. Safe
 * actions are safe because the gate proved they move no money, can be undone, and
 * contact nobody; requiring a tick as well added no safety and made harmless things
 * queue for approval.
 *
 * So the two lists here are informational: this is what runs on its own, this is
 * what waits for you, and here is why. The only real control is switching an action
 * off entirely, which is a genuine choice a merchant might make - a shop that never
 * wants substitutions offered can say so.
 *
 * Money-touching actions appear in the second list with no way to promote them.
 * Showing them matters: a merchant asking "can this thing refund my customers
 * without asking?" deserves to see the answer rather than infer it from an absence.
 */
function PolicyEditor({
  policy,
  actions,
  onMode,
  onBlock,
  onHoldout,
}: {
  policy: Policy;
  actions: ActionInfo[];
  onMode: (mode: string) => void;
  onBlock: (actionType: string, blocked: boolean) => void;
  onHoldout: (percent: number) => void;
}) {
  const [holdoutDraft, setHoldoutDraft] = useState(
    String(policy.holdout_percent),
  );
  const [savingHoldout, setSavingHoldout] = useState(false);

  // Keep the draft in step with a policy change from outside this input
  // (mode switch, blocking an action, or the initial load) without an
  // effect - adjusted here, during render, rather than after it, the same
  // pattern React's own docs use for state derived from a prop.
  const [seenHoldoutPercent, setSeenHoldoutPercent] = useState(
    policy.holdout_percent,
  );
  if (policy.holdout_percent !== seenHoldoutPercent) {
    setSeenHoldoutPercent(policy.holdout_percent);
    setHoldoutDraft(String(policy.holdout_percent));
  }

  const modes: Array<[string, string, string]> = [
    ["CAUTIOUS", "Cautious", "Every action waits for you"],
    ["STANDARD", "Standard", "Safe actions run on their own"],
    ["SUSPENDED", "Paused", "Nothing runs at all"],
  ];

  // Actions with nothing to execute are hidden. "No action" and "escalate to human"
  // are outcomes, not things a merchant chooses to permit.
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