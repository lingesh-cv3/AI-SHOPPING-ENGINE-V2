import type { Capabilities, Rule } from "./api";

/**
 * Platform / Capabilities. Extracted unchanged from `MerchantConsole.tsx`'s
 * former single-page layout - same data, same rendering, just its own
 * section now instead of always being on screen.
 *
 * The capability list is not a feature checklist we wrote. It comes from
 * the adapter, which reports what this merchant's platform can actually
 * do. When it says payment recovery is unavailable, that is a fact about
 * their backend, not a limitation of ours.
 */
export function PlatformCapabilities({ caps, rules }: { caps: Capabilities; rules: Rule[] }) {
  const unsupported = caps.operations.filter((o) => !o.supported);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
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
  );
}
