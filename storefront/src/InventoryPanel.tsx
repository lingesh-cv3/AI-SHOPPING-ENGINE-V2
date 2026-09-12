import { useEffect, useState } from "react";
import { console_api, type CatalogAlerts } from "./api";
import { MerchantCopilot } from "./MerchantCopilotWidget";

/**
 * Out-of-stock and low-stock products, read fresh from the whole catalog.
 *
 * Follows MerchantReport.tsx's own panel shape exactly - same eyebrow/panel-head/
 * panel-body structure, same "every branch renders something" rule (see that
 * file's comment on why a vanished panel is the hardest bug to notice).
 *
 * The honesty fields drive what renders, not just the lists: a merchant with a
 * genuinely empty problem list ("checked, nothing wrong") must never look the
 * same as one whose catalog could not be read, or one whose scan only covered
 * part of a very large catalog.
 *
 * Open catalogue/inventory MerchantTasks (Store > Merchant Tasks) are the
 * actionable half of this same evidence - this panel is the read of record,
 * the Copilot below surfaces the same tasks with a link to act on them.
 */
export function InventoryPanel({
  onNavigate,
}: {
  onNavigate: (section: string) => void;
}) {
  const [data, setData] = useState<CatalogAlerts | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .catalogAlerts()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Could not load inventory."),
      );
  }, []);

  const header = (
    <div className="overview-header">
      <div>
        <h2 style={{ margin: 0, fontSize: "var(--step-4)" }}>Inventory &amp; Catalog</h2>
        <p className="overview-subtitle">
          Out-of-stock and low-stock products, scanned fresh from your live catalog.
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

  if (!data) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <p className="empty">Loading…</p>
      </div>
    );
  }

  if (!data.reachable) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {header}
        <section className="panel">
          <div className="panel-body">
            <p className="empty">
              Could not read your catalog right now - your platform did not
              respond. This is not the same as an empty catalog; try again in a
              moment.
            </p>
          </div>
        </section>
        <div className="overview-copilot-slot">
          <MerchantCopilot onNavigate={onNavigate} />
        </div>
      </div>
    );
  }

  const nothingToReport =
    data.scanned === 0 && data.out_of_stock.length === 0 && data.low_stock.length === 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {header}

      <div className="kpi-row">
        <div className="kpi-card">
          <div className="eyebrow">Products scanned</div>
          <div className="kpi-value">{data.scanned}</div>
          {!data.complete && (
            <p className="note" style={{ margin: "4px 0 0" }}>
              stopped at {data.truncated_at}
            </p>
          )}
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Out of stock</div>
          <div className="kpi-value" style={data.out_of_stock.length > 0 ? { color: "var(--friction)" } : undefined}>
            {data.out_of_stock.length}
          </div>
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Low stock</div>
          <div className="kpi-value">
            {data.low_stock_available === false ? "n/a" : data.low_stock.length}
          </div>
          {data.low_stock_available === false && (
            <p className="note" style={{ margin: "4px 0 0" }}>platform doesn&rsquo;t report this</p>
          )}
        </div>
        <div className="kpi-card">
          <div className="eyebrow">Scan status</div>
          <div className="kpi-value" style={{ fontSize: 16 }}>
            {data.complete ? "Complete" : "Partial"}
          </div>
        </div>
      </div>

      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Inventory &amp; catalog</span>
          <span className="eyebrow">
            {data.scanned} product{data.scanned === 1 ? "" : "s"} scanned
          </span>
        </div>
        <div className="panel-body">
          {!data.complete && (
            <p className="empty" style={{ marginBottom: 12 }}>
              Results may be incomplete beyond {data.truncated_at} products - your
              catalog is larger than one scan safely covers in a single request.
            </p>
          )}

          {data.scanned === 0 ? (
            <p className="empty">
              Nothing to report - your catalog has no products yet.
            </p>
          ) : nothingToReport ? (
            <p className="empty">
              Checked {data.scanned} products - nothing out of stock or low
              right now.
            </p>
          ) : (
            <>
              <div className="gate-label" style={{ marginBottom: 6 }}>
                Out of stock ({data.out_of_stock.length})
              </div>
              {data.out_of_stock.length === 0 ? (
                <p className="note" style={{ marginTop: 0 }}>
                  Nothing out of stock right now.
                </p>
              ) : (
                <table className="mini-table" style={{ marginBottom: 8 }}>
                  <thead>
                    <tr>
                      <th>Product</th>
                      <th>SKU</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.out_of_stock.map((p) => (
                      <tr key={p.product_id}>
                        <td>{p.title}</td>
                        <td className="num">{p.sku ?? p.product_id}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              <div className="gate-label" style={{ margin: "18px 0 6px" }}>
                Low stock
                {data.low_stock_available !== false && ` (${data.low_stock.length})`}
              </div>
              {data.low_stock_available === false ? (
                <p className="note" style={{ marginTop: 0 }}>
                  Your platform does not report a low-stock state - only
                  in-stock/out-of-stock. This is a limit of what your platform
                  exposes, not a claim that nothing is running low.
                </p>
              ) : data.low_stock.length === 0 ? (
                <p className="note" style={{ marginTop: 0 }}>
                  Nothing low on stock right now.
                </p>
              ) : (
                <table className="mini-table">
                  <thead>
                    <tr>
                      <th>Product</th>
                      <th>SKU</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.low_stock.map((p) => (
                      <tr key={p.product_id}>
                        <td>{p.title}</td>
                        <td className="num">{p.sku ?? p.product_id}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}

          {!nothingToReport && data.scanned > 0 && (
            <p className="note" style={{ marginTop: 14 }}>
              Every out-of-stock or low-stock product above is also tracked as
              a Merchant Task (Store &gt; Merchant Tasks) so it stays visible
              until you resolve or dismiss it.
            </p>
          )}
        </div>
      </section>

      <div className="overview-copilot-slot">
        <MerchantCopilot onNavigate={onNavigate} />
      </div>
    </div>
  );
}
