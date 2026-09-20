import { useEffect, useState } from "react";
import {
  console_api,
  type CartProposalActivity,
  type CaseRow,
  type MerchantReport as Report,
  type ShopperV2Activity,
} from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/** A percentage figure. Null (no problems opened yet) must never render as
 *  "0%" - that would claim a 0% resolution rate where there is really no
 *  rate to report at all. */
function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate)}%`;
}

function when(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + "h ago";
  return Math.round(hours / 24) + "d ago";
}

/** Real counts from the cart-proposal event stream - shown independent of
 *  whether any friction case exists this window, since a shopper can see
 *  and act on a cart suggestion without ever hitting a friction case. */
function CartSuggestionsPanel({ proposals }: { proposals: CartProposalActivity }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Cart suggestions</span>
        <span className="eyebrow">last {proposals.days} days · observed, not causal</span>
      </div>
      <div className="panel-body">
        {!proposals.has_history ? (
          <p className="empty">
            No cart suggestions have been created yet - this fills in once
            shoppers start seeing them.
          </p>
        ) : (
          <>
            <div className="kpi-row">
              <div className="kpi-card">
                <div className="eyebrow">Suggested</div>
                <div className="kpi-value">{proposals.created}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Accepted</div>
                <div className="kpi-value">{proposals.accepted}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Declined</div>
                <div className="kpi-value">{proposals.rejected}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Acceptance rate</div>
                <div className="kpi-value">{pct(proposals.acceptance_rate)}</div>
              </div>
            </div>
            {proposals.most_suggested_products.length > 0 && (
              <p className="note" style={{ marginTop: 12 }}>
                Most often suggested:{" "}
                {proposals.most_suggested_products
                  .slice(0, 5)
                  .map((p) => `${p.product_id} (${p.times_suggested})`)
                  .join(", ")}
              </p>
            )}
            <p className="note" style={{ marginTop: 8 }}>
              What shoppers were suggested and what they did about it - not
              a claim that an accepted suggestion caused a sale.
            </p>
          </>
        )}
      </div>
    </section>
  );
}

/** Real counts from Shopper V2's own event stream (missions, basket
 *  proposals, trade-off re-ranking, payment recovery, order status) - shown
 *  independent of whether any friction case exists this window, the same
 *  reasoning CartSuggestionsPanel already applies. */
function ShopperV2ActivityPanel({ activity }: { activity: ShopperV2Activity }) {
  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Shopper V2 activity</span>
        <span className="eyebrow">last {activity.days} days · observed, not causal</span>
      </div>
      <div className="panel-body">
        {!activity.has_history ? (
          <p className="empty">
            No Shopper V2 activity yet - this fills in once shoppers start
            using missions, basket proposals, or trade-off comparisons on
            the V2 assistant.
          </p>
        ) : (
          <>
            <div className="kpi-row">
              <div className="kpi-card">
                <div className="eyebrow">Mission items refined</div>
                <div className="kpi-value">{activity.missions_refined}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Baskets proposed</div>
                <div className="kpi-value">{activity.baskets_proposed}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Trade-offs applied</div>
                <div className="kpi-value">{activity.trade_offs_applied}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Payment recovery offers</div>
                <div className="kpi-value">{activity.payment_recovery_offers}</div>
              </div>
              <div className="kpi-card">
                <div className="eyebrow">Order status checks</div>
                <div className="kpi-value">{activity.order_status_checks}</div>
              </div>
            </div>
            <p className="note" style={{ marginTop: 8 }}>
              What shoppers actually did with Shopper V2's capabilities -
              not a claim that any of it drove a sale.
            </p>
          </>
        )}
      </div>
    </section>
  );
}

/**
 * AI Commerce: what the engine actually did for shoppers this window, and
 * the recent activity that produced it. These are observed outcomes, not a
 * claim of causal lift - see the Holdout / Experiment section for the one
 * comparison in this product that can defensibly claim that.
 */
export function AICommerce({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [report, setReport] = useState<Report | null>(null);
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [proposals, setProposals] = useState<CartProposalActivity | null>(null);
  const [v2Activity, setV2Activity] = useState<ShopperV2Activity | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      console_api.report(),
      console_api.cases(20),
      console_api.cartProposalActivity(),
      console_api.shopperV2Activity(),
    ])
      .then(([r, c, p, v2]) => {
        setReport(r);
        setCases(c.cases);
        setProposals(p);
        setV2Activity(v2);
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load AI commerce data."));
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>AI Commerce</h2>
        <p className="overview-subtitle">
          What the assistant actually did for shoppers this window - observed, not projected.
        </p>
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
  if (!report || !cases || !proposals || !v2Activity) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading...</p>
      </div>
    );
  }

  if (report.shoppers_helped === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              Nothing yet. As shoppers run into problems, what the assistant
              did about them shows up here - these are observed outcomes, not
              a projection.
            </p>
          </div>
        </section>
        {/* Cart-proposal activity is recorded on a separate path from
            friction cases (a shopper can see and act on a cart suggestion
            without ever hitting a friction case) - shown here too, so a
            merchant with zero cases but real proposal history is not
            silently shown nothing. This is the exact gap a live browser
            check found: this panel used to only exist in the branch below,
            invisible whenever shoppers_helped was 0 even with real
            proposal activity. */}
        <CartSuggestionsPanel proposals={proposals} />
        <ShopperV2ActivityPanel activity={v2Activity} />
        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Shoppers helped</div>
          <div className="kpi-value">{report.shoppers_helped}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Resolved</div>
          <div className="kpi-value">{pct(report.resolution_rate)}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Without your time</div>
          <div className="kpi-value">{report.handled_without_you}</div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Waiting on you</div>
          <div className="kpi-value" style={report.waiting_for_you > 0 ? { color: "var(--friction)" } : undefined}>
            {report.waiting_for_you}
          </div>
        </div>
      </div>

      <CartSuggestionsPanel proposals={proposals} />
      <ShopperV2ActivityPanel activity={v2Activity} />

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Recent activity</span>
          <span className="eyebrow">last {report.days} days · observed, not causal</span>
        </div>
        <div className="panel-body">
          {cases.length === 0 ? (
            <p className="empty">No cases in this window.</p>
          ) : (
            cases.map((c) => (
              <div key={c.case_id} className="recentrow">
                <div className="recent-head">
                  <span className="eyebrow">
                    {c.friction_type?.replace(/_/g, " ") ?? "question"}
                  </span>
                  <span className="eyebrow">{when(c.created_at)}</span>
                </div>
                {c.diagnosis && <p className="recent-diag">{c.diagnosis}</p>}
                <p className="note" style={{ margin: "2px 0 0" }}>
                  {c.state}
                  {c.risk_outcome ? ` · ${c.risk_outcome.toLowerCase()}` : ""}
                  {c.selected_action
                    ? ` · ${c.selected_action.replace(/_/g, " ").toLowerCase()}`
                    : ""}
                </p>
              </div>
            ))
          )}
          <p className="note" style={{ marginTop: 14 }}>
            These count what happened, not what would have happened without
            the assistant - for that comparison, see Holdout / Experiment.
          </p>
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
