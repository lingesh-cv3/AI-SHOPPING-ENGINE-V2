import { useCallback, useEffect, useState } from "react";
import { console_api, type MerchantTaskRow } from "./api";

const KIND_LABEL: Record<string, string> = {
  OUT_OF_STOCK: "Out of stock",
  LOW_STOCK: "Low stock",
  UNMET_DEMAND: "Unmet demand",
};

/** A merchant's real, persisted work items - flagged deterministically
 *  from live catalogue/unmet-demand data, never by the model, and never a
 *  commerce action: resolving or dismissing a task writes nothing to the
 *  merchant's platform, it only records that a person handled the
 *  underlying problem themselves. See `engine/db/models.py::MerchantTask`
 *  for why this category of problem gets a CV3-owned task instead of a
 *  fake inventory-write capability.
 */
export function TasksPanel() {
  const [tasks, setTasks] = useState<MerchantTaskRow[] | null>(null);
  const [filter, setFilter] = useState<"OPEN" | "RESOLVED" | "DISMISSED">("OPEN");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (state: string) => {
    try {
      const r = await console_api.tasks(state);
      setTasks(r.tasks);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load tasks.");
    }
  }, []);

  useEffect(() => {
    refresh(filter);
  }, [refresh, filter]);

  async function decide(taskId: string, resolved: boolean) {
    setBusy(taskId);
    try {
      await console_api.decideTask(taskId, resolved);
      await refresh(filter);
    } catch {
      setError("That decision did not go through. Nothing was changed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Merchant tasks</span>
        <div className="range-picker" role="group" aria-label="Task state">
          {(["OPEN", "RESOLVED", "DISMISSED"] as const).map((s) => (
            <button
              key={s}
              className={`range-btn ${s === filter ? "active" : ""}`}
              onClick={() => setFilter(s)}
            >
              {s.charAt(0) + s.slice(1).toLowerCase()}
            </button>
          ))}
        </div>
      </div>
      <div className="panel-body">
        {error && <p className="empty">{error}</p>}
        {!error && !tasks && <p className="empty">Loading...</p>}
        {!error && tasks && tasks.length === 0 && (
          <p className="empty">
            {filter === "OPEN"
              ? "No open tasks right now - flagged automatically from real catalogue and unmet-demand data."
              : `No ${filter.toLowerCase()} tasks yet.`}
          </p>
        )}
        {!error && tasks && tasks.length > 0 && (
          <div className="task-list">
            {tasks.map((t) => (
              <article key={t.task_id} className="task-card">
                <div className="task-card-head">
                  <span className={`status-badge ${t.kind === "UNMET_DEMAND" ? "attention" : "attention"}`}>
                    {KIND_LABEL[t.kind] ?? t.kind}
                  </span>
                  <span className="note" style={{ margin: 0 }}>
                    {t.state === "OPEN"
                      ? `Seen ${new Date(t.last_seen_at).toLocaleDateString()}`
                      : `${t.state.charAt(0) + t.state.slice(1).toLowerCase()} ${
                          t.decided_at ? new Date(t.decided_at).toLocaleDateString() : ""
                        }`}
                  </span>
                </div>
                <div className="task-card-label">{t.label}</div>
                {t.state === "OPEN" && (
                  <div className="task-card-actions">
                    <button
                      className="add"
                      disabled={busy === t.task_id}
                      onClick={() => decide(t.task_id, true)}
                    >
                      Resolve
                    </button>
                    <button
                      className="navlink"
                      disabled={busy === t.task_id}
                      onClick={() => decide(t.task_id, false)}
                    >
                      Dismiss
                    </button>
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
