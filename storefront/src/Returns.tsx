import { useEffect, useState } from "react";
import { console_api, type Capabilities } from "./api";

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
export function Returns() {
  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .capabilities()
      .then(setCaps)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load platform capabilities."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!caps) return <p className="empty">Loading...</p>;

  return (
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
  );
}
