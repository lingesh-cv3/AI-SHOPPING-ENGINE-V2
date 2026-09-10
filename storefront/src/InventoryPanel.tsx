import { useEffect, useState } from "react";
import { console_api, type CatalogAlerts } from "./api";

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
 */
export function InventoryPanel() {
  const [data, setData] = useState<CatalogAlerts | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .catalogAlerts()
      .then(setData)
      .catch((e) =>
        setError(e instanceof Error ? e.message : "Could not load inventory."),
      );
  }, []);

  if (error) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Inventory &amp; catalog</span>
        </div>
        <div className="panel-body">
          <p className="empty">{error}</p>
        </div>
      </section>
    );
  }

  if (!data) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Inventory &amp; catalog</span>
        </div>
        <div className="panel-body">
          <p className="empty">Loading…</p>
        </div>
      </section>
    );
  }

  if (!data.reachable) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Inventory &amp; catalog</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            Could not read your catalog right now - your platform did not
            respond. This is not the same as an empty catalog; try again in a
            moment.
          </p>
        </div>
      </section>
    );
  }

  const nothingToReport =
    data.scanned === 0 && data.out_of_stock.length === 0 && data.low_stock.length === 0;

  return (
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
              data.out_of_stock.map((p) => (
                <div key={p.product_id} className="frictionrow">
                  <span>{p.title}</span>
                  <span className="num">{p.sku ?? p.product_id}</span>
                </div>
              ))
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
              data.low_stock.map((p) => (
                <div key={p.product_id} className="frictionrow">
                  <span>{p.title}</span>
                  <span className="num">{p.sku ?? p.product_id}</span>
                </div>
              ))
            )}
          </>
        )}
      </div>
    </section>
  );
}
