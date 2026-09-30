import { useEffect, type ReactNode } from "react";
import type { Connection } from "./api";
import { COVERAGE } from "./opsCoverage";
import {
  refreshOverview,
  startOverviewPolling,
  useOverview,
  type Panel,
} from "./opsOverviewStore";

function clock(ms: number | null): string {
  return ms === null
    ? ""
    : new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function platformLabel(c: Connection): string {
  return c.platform;
}

/**
 * Across every merchant CV3 covers: what needs attention, and what Operations can
 * actually see.
 *
 * Every figure here is read from an engine route and is shown only once it has
 * really been fetched. There is deliberately nothing on this page that the engine
 * cannot back: no incidents, no health states, no revenue figures, no priorities.
 * Where a thing is not measured it says so in words, and the Coverage panel lists
 * what is out of view.
 *
 * Merchants' own decisions and their shoppers' follow-ups are shown as counts only,
 * with no controls. They belong to each merchant; an operator sees them here to know
 * which merchant is not keeping up, not to work them.
 */
export function OpsOverview() {
  const s = useOverview();

  useEffect(() => startOverviewPolling(), []);

  const refreshing =
    s.handovers.refreshing || s.stats.refreshing || s.connections.refreshing;
  const latest = Math.max(
    s.handovers.asOf ?? 0,
    s.stats.asOf ?? 0,
    s.connections.asOf ?? 0,
  );

  return (
    <div className="ops-overview">
      <div className="overview-header">
        <div>
          <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Overview</h2>
          <p className="overview-subtitle">
            What needs attention across CV3&rsquo;s merchants, and what Operations can
            see.
          </p>
        </div>
        <div className="overview-header-right ops-refresh">
          {latest > 0 && <span className="appr-status">as of {clock(latest)}</span>}
          <button
            className="refresh"
            onClick={() => void refreshOverview()}
            disabled={refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      <Attention />
      <Portfolio />
      <Coverage />
    </div>
  );
}

/** One line of a panel's status: what is stale, what failed. */
function PanelStatus<T>({ p }: { p: Panel<T> }) {
  if (!p.error || p.phase === "error") return null;
  return (
    <p className="appr-stale ops-stale">
      Could not refresh ({p.error}) - showing the last data loaded at {clock(p.asOf)}.
    </p>
  );
}

function Attention() {
  const { handovers, stats } = useOverview();

  return (
    <section className="panel" aria-labelledby="ops-attention">
      <div className="panel-head">
        <span className="eyebrow" id="ops-attention">
          Needs attention
        </span>
      </div>
      <div className="panel-body ops-rows">
        <Row
          label="Shopper follow-ups nobody has picked up"
          note="Owned by each merchant. Operations does not contact shoppers."
          panel={handovers}
          render={(d) =>
            d.total === 0 ? (
              <strong className="num">0</strong>
            ) : (
              <>
                <strong className="num">{d.total}</strong>
                <span className="ops-sub">
                  oldest waiting {d.handovers[0]?.waiting_minutes ?? 0}m
                </span>
              </>
            )
          }
        />
        <Row
          label="Decisions waiting on merchants"
          note="Count only. Each merchant decides their own, in their Approvals."
          panel={stats}
          render={(d) =>
            d.waiting === 0 ? (
              <strong className="num">0</strong>
            ) : (
              <>
                <strong className="num">{d.waiting}</strong>
                <span className="ops-sub">oldest waiting {d.oldest_wait_minutes}m</span>
              </>
            )
          }
        />
        <div className="ops-row">
          <div>
            <div className="ops-row-label">Incidents</div>
            <div className="ops-row-note">
              No detector exists yet, so nothing here can be said to be open or
              clear.
            </div>
          </div>
          <div className="ops-row-value">
            <span className="ops-unavailable">Not tracked yet</span>
          </div>
        </div>
      </div>
    </section>
  );
}

function Row<T>({
  label,
  note,
  panel,
  render,
}: {
  label: string;
  note: string;
  panel: Panel<T>;
  render: (data: T) => ReactNode;
}) {
  return (
    <div className="ops-row">
      <div>
        <div className="ops-row-label">{label}</div>
        <div className="ops-row-note">{note}</div>
        <PanelStatus p={panel} />
      </div>
      <div className="ops-row-value">
        {panel.data !== null ? (
          render(panel.data)
        ) : panel.phase === "error" ? (
          <span className="ops-unavailable" role="alert">
            Could not load
          </span>
        ) : (
          <span className="ops-loading">Loading…</span>
        )}
      </div>
    </div>
  );
}

function Portfolio() {
  const { connections, stats, handovers } = useOverview();

  // Per-merchant decisions come from stats, which is keyed by display name. Joining
  // on a name is only safe while names are unique, so the column is shown only then.
  const names = (connections.data ?? []).map((c) => c.merchant_name);
  const namesUnique = new Set(names).size === names.length;

  // Per-merchant follow-ups come from the page of handovers. They are exact only when
  // every open one was returned; otherwise the column says so instead of undercounting.
  const followUpsComplete =
    handovers.data !== null && handovers.data.total <= handovers.data.handovers.length;

  return (
    <section className="panel" aria-labelledby="ops-portfolio">
      <div className="panel-head">
        <span className="eyebrow" id="ops-portfolio">
          Merchant portfolio
          {connections.data ? ` (${connections.data.length})` : ""}
        </span>
      </div>
      <div className="panel-body">
        <PanelStatus p={connections} />
        {connections.data === null ? (
          connections.phase === "error" ? (
            <p className="empty" role="alert">
              Could not load the merchant list ({connections.error}). This is not the
              same as no merchants.
            </p>
          ) : (
            <p className="empty">Loading…</p>
          )
        ) : connections.data.length === 0 ? (
          <p className="empty">No merchants are connected.</p>
        ) : (
          <div className="ops-table-wrap">
            <table className="ops-table">
              <thead>
                <tr>
                  <th>Merchant</th>
                  <th>Platform</th>
                  <th>Automation</th>
                  <th>Unsupported operations</th>
                  <th>Decisions waiting</th>
                  <th>Open follow-ups</th>
                </tr>
              </thead>
              <tbody>
                {connections.data.map((c) => (
                  <tr key={c.connection_id} data-testid="portfolio-row">
                    <td>{c.merchant_name}</td>
                    <td>{platformLabel(c)}</td>
                    <td>{c.mode.toLowerCase()}</td>
                    <td title={c.unsupported.join(", ") || undefined}>
                      {c.unsupported.length}
                    </td>
                    <td className="num">
                      {stats.data !== null && namesUnique
                        ? (stats.data.by_merchant[c.merchant_name] ?? 0)
                        : "—"}
                    </td>
                    <td className="num">
                      {followUpsComplete && handovers.data
                        ? handovers.data.handovers.filter(
                            (h) => h.connection_id === c.connection_id,
                          ).length
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {connections.data && connections.data.length > 0 && (
          <div className="ops-footnotes">
            {!namesUnique && (
              <p className="note">
                Decisions waiting are not broken down by merchant: two merchants share a
                display name, and that count is keyed by name.
              </p>
            )}
            {handovers.data !== null && !followUpsComplete && (
              <p className="note">
                Open follow-ups are not broken down by merchant: {handovers.data.total}{" "}
                are open and only the oldest {handovers.data.handovers.length} are
                listed, so a per-merchant count would undercount.
              </p>
            )}
            {(stats.phase === "error" || handovers.phase === "error") && (
              <p className="note">
                A dash means that figure could not be loaded - not zero.
              </p>
            )}
          </div>
        )}
        <p className="ops-health-line">
          Operational health (API, webhooks, orders, payments, inventory) is not
          measured yet: no source feeds it, so none is shown.
        </p>
      </div>
    </section>
  );
}

function Coverage() {
  return (
    <section className="panel" aria-labelledby="ops-coverage">
      <div className="panel-head">
        <span className="eyebrow" id="ops-coverage">
          What Operations can see today
        </span>
      </div>
      <div className="panel-body ops-coverage">
        <div>
          <div className="gate-label">Sees</div>
          <ul className="evidence">
            {COVERAGE.sees.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </div>
        <div>
          <div className="gate-label">Cannot see</div>
          <ul className="evidence">
            {COVERAGE.cannotSee.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </div>
        <p className="note ops-coverage-note">
          This list is maintained by hand and describes which sources exist, not a live
          reading of them.
        </p>
      </div>
    </section>
  );
}
