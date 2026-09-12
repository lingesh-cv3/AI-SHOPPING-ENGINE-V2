import type { Capabilities, Rule } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/**
 * Platform / Capabilities.
 *
 * The capability list is not a feature checklist we wrote. It comes from
 * the adapter, which reports what this merchant's platform can actually
 * do. When it says payment recovery is unavailable, that is a fact about
 * their backend, not a limitation of ours.
 */
export function PlatformCapabilities({
  caps,
  rules,
  onNavigate,
}: {
  caps: Capabilities;
  rules: Rule[];
  onNavigate: (section: string) => void;
}) {
  const unsupported = caps.operations.filter((o) => !o.supported);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="overview-header">
        <div>
          <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Platform &amp; Capabilities</h2>
          <p className="overview-subtitle">
            What your platform can actually do, and the rules that decide every action.
          </p>
        </div>
      </div>

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Platform</div>
          <div className="kpi-value" style={{ fontSize: 18 }}>{caps.platform}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Operations available</div>
          <div className="kpi-value">
            {caps.operations.length - unsupported.length}
            <span className="kpi-value-unit">of {caps.operations.length}</span>
          </div>
        </div>
        <div className="kpi-card" style={{ gridColumn: "span 2" }}>
          <div className="eyebrow">Decision rules</div>
          <div className="kpi-value">{rules.length}</div>
          <p className="note" style={{ margin: "4px 0 0" }}>first match wins</p>
        </div>
      </div>

      <div className="overview-trend-row">
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
      </div>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
