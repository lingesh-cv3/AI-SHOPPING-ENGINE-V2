import { useEffect, useState } from "react";
import { console_api, type Capabilities } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/**
 * Returns.
 *
 * Audited before writing a line of UI: neither adapter in this system
 * declares a return or refund capability, `ISSUE_REFUND` exists as a type
 * in `shared/models/action.py` but is not in the model's proposable-action
 * list (`engine/reasoning/prompts.py`), and no adapter implements it - so
 * it cannot actually be reached by anything in this pipeline. There is no
 * return data anywhere in this schema: no Return/Refund table, no return
 * reason, no return rate.
 *
 * So this is not a thin version of a Returns dashboard - it is an honest
 * statement that the capability does not exist yet, the same shape
 * `PaymentsPanel` already uses for Northfield's missing payment recovery.
 * Building real numbers here would mean inventing them.
 */
export function Returns({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .capabilities()
      .then((c) => {
        setCaps(c);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load platform capabilities."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Returns</h2>
        <p className="overview-subtitle">An honest statement of what this platform can&rsquo;t do yet.</p>
      </div>
    </div>
  );

  if (error) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">{error}</p>
      </div>
    );
  }
  if (!caps) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Returns</span>
          <span className="eyebrow">{caps.platform}</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            Returns are not currently supported on this platform. Neither this
            adapter nor the engine has a real return or refund capability
            today - there is no return data to show, so none is invented here.
          </p>
          <p className="note">
            If your platform gains real return/refund support, this section
            will show it using the same authoritative-data rule as everywhere
            else in this console: real numbers or an honest "unavailable"
            state, never a placeholder presented as a figure.
          </p>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
