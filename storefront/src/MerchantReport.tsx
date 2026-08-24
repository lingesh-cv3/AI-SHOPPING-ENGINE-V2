import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report } from "./api";

/**
 * What the engine did for this shop.
 *
 * The console had settings and no reporting, so a merchant could configure the
 * assistant and never learn whether it had helped anyone. We were recording revenue
 * recovered and not showing it to the person whose revenue it was.
 *
 * Written for a shop owner rather than an operator. The headline number is money,
 * because that is the one a merchant checks against their own books. Case counts
 * and throughput belong on the operations console, which is a different page for a
 * different person.
 *
 * Every number here is real. Nothing is projected, annualised or extrapolated, and
 * an empty shop reads as empty rather than as a demo dataset.
 */
export function MerchantReport() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .report()
      .then(setReport)
      .catch(() => setError("Could not load your figures."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!report) return null;

  const nothingYet = report.shoppers_helped === 0;

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Last {report.days} days</span>
        {report.median_resolution_ms !== null && (
          <span className="eyebrow">
            typically {formatMs(report.median_resolution_ms)}
          </span>
        )}
      </div>

      <div className="panel-body">
        {nothingYet ? (
          <p className="empty">
            Nothing yet. As shoppers run into problems, what the assistant did about
            them shows up here.
          </p>
        ) : (
          <>
            <div className="headline-figure">
              <div className="eyebrow">Sales recovered</div>
              <div className="bignum num">
                {report.revenue_recovered} {report.currency}
              </div>
              <p className="note" style={{ margin: "4px 0 0" }}>
                Money that would otherwise have been lost to a failed payment.
              </p>
            </div>

            <div className="figures">
              <Figure
                value={report.shoppers_helped}
                label="Shoppers helped"
              />
              <Figure
                value={report.problems_solved}
                label="Problems solved"
              />
              <Figure
                value={report.handled_without_you}
                label="Without your time"
              />
              <Figure
                value={report.waiting_for_you}
                label="Waiting on you"
                warn={report.waiting_for_you > 0}
              />
            </div>

            {report.friction.length > 0 && (
              <>
                <div className="gate-label" style={{ margin: "22px 0 8px" }}>
                  What shoppers ran into
                </div>
                {report.friction.map((f) => (
                  <div key={f.type} className="frictionrow">
                    <span>{f.type.replace(/_/g, " ").toLowerCase()}</span>
                    <span className="num">{f.count}</span>
                  </div>
                ))}
              </>
            )}

            <div className="gate-label" style={{ margin: "22px 0 8px" }}>
              Recently
            </div>
            {report.recent.map((c) => (
              <div key={c.case_id} className="recentrow">
                <div className="recent-head">
                  <span className="eyebrow">
                    {c.friction_type?.replace(/_/g, " ") ?? "question"}
                  </span>
                  <span className="eyebrow">{when(c.created_at)}</span>
                </div>
                {c.diagnosis && <p className="recent-diag">{c.diagnosis}</p>}
                {c.shopper_reply && (
                  <p className="recent-said">
                    Told them: &ldquo;{trim(c.shopper_reply)}&rdquo;
                  </p>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </section>
  );
}

function Figure({
  value,
  label,
  warn,
}: {
  value: number;
  label: string;
  warn?: boolean;
}) {
  return (
    <div>
      <div className={warn && value > 0 ? "figure num warn" : "figure num"}>
        {value}
      </div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}

/** Milliseconds are an engineering unit. A merchant wants "under a second". */
function formatMs(ms: number): string {
  if (ms < 1000) return "under a second";
  if (ms < 60_000) return `${Math.round(ms / 1000)} seconds`;
  return `${Math.round(ms / 60_000)} minutes`;
}

function when(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** The assistant's replies run to a couple of sentences. One is enough here. */
function trim(text: string): string {
  const first = text.split("\n")[0];
  return first.length > 110 ? `${first.slice(0, 110)}…` : first;
}