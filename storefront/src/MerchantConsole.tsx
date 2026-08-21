import { useCallback, useEffect, useState } from "react";
import {
  console_api,
  type ActionInfo,
  type Capabilities,
  type Pipeline,
  type Policy,
  type Rule,
} from "./api";
import { Gates } from "./Gates";

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
    } catch {
      setError("Could not reach the engine. Is it running on port 8000?");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function setMode(mode: string) {
    if (!policy) return;
    setPolicy(await console_api.savePolicy(mode, [], policy.blocked));
    setTest(null);
  }

  async function onBlock(actionType: string, blocked: boolean) {
    if (!policy) return;
    const next = blocked
      ? [...policy.blocked, actionType]
      : policy.blocked.filter((a) => a !== actionType);
    // auto_allowed is left empty deliberately. It is now an optional restriction
    // rather than a permission list, and most merchants want every safe action.
    setPolicy(await console_api.savePolicy(policy.mode, [], next));
    setTested(actionType);
    setTest(await console_api.testAction(actionType));
  }
  if (error) return <p className="empty">{error}</p>;
  if (!caps || !policy) return <p className="empty">Loading…</p>;

  const unsupported = caps.operations.filter((o) => !o.supported);

  return (
    <div className="layout">
      <main style={{ display: "flex", flexDirection: "column", gap: 20 }}>
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
}: {
  policy: Policy;
  actions: ActionInfo[];
  onMode: (mode: string) => void;
  onBlock: (actionType: string, blocked: boolean) => void;
}) {
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