import { useEffect, useState } from "react";
import { console_api, type MerchantReport as Report } from "./api";

function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate)}%`;
}

/**
 * Holdout / Experiment.
 *
 * The one comparison in this console that can defensibly claim the
 * assistant caused a difference, rather than merely observed one - because
 * a slice of sessions genuinely get no help, assigned once per session and
 * never revisited (`db.holdout_status` / `resolve_unresolved_payment_cases_for_cart`).
 * This view does not touch that mechanism - it only reads
 * `merchant_report()`'s existing `holdout` block.
 *
 * A revenue-per-group comparison is deliberately not shown: the backend
 * does not currently compute recovered revenue split by holdout/assisted
 * group, only resolution-rate. Showing a revenue number here would mean
 * inventing a comparison nothing computes.
 */
export function Holdout() {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console_api
      .report()
      .then(setReport)
      .catch((e) => setError(e instanceof Error ? e.message : "Could not load holdout data."));
  }, []);

  if (error) return <p className="empty">{error}</p>;
  if (!report) return <p className="empty">Loading...</p>;

  if (!report.holdout) {
    return (
      <section className="panel">
        <div className="panel-head">
          <span className="eyebrow">Holdout / experiment</span>
        </div>
        <div className="panel-body">
          <p className="empty">
            No holdout sessions in this window. Turn on a holdout percentage
            in Store &gt; Settings to start comparing assisted outcomes
            against a genuinely unassisted control group.
          </p>
        </div>
      </section>
    );
  }

  const h = report.holdout;
  const smallSample = h.holdout_cases < 20 || h.assisted_cases < 20;

  return (
    <section className="panel">
      <div className="panel-head">
        <span className="eyebrow">Holdout / experiment</span>
        <span className="eyebrow">last {report.days} days</span>
      </div>
      <div className="panel-body">
        <p className="note" style={{ marginTop: 0 }}>
          A slice of your shoppers get no help at all, so this comparison is
          the difference the assistant actually made - not a number that
          includes sales that would have happened anyway.
        </p>
        <div className="figures">
          <Figure
            value={pct(h.assisted_resolution_rate)}
            label="Resolved, assisted"
            title={`${h.assisted_resolved} of ${h.assisted_cases}`}
          />
          <Figure
            value={pct(h.holdout_resolution_rate)}
            label="Resolved, holdout"
            title={`${h.holdout_resolved} of ${h.holdout_cases} - no assistance given`}
          />
        </div>
        <p className="note" style={{ marginTop: 14 }}>
          Sample size: {h.holdout_cases} holdout case
          {h.holdout_cases === 1 ? "" : "s"}, {h.assisted_cases} assisted
          case{h.assisted_cases === 1 ? "" : "s"}.
          {smallSample
            ? " This is small enough that the difference above should be treated as directional, not proof - a few more or fewer resolved cases would move it noticeably."
            : " Large enough to treat the difference above as a real signal, though this is still one merchant's data, not a controlled study."}
        </p>
        <p className="note">
          Revenue recovered is not split by holdout/assisted group - only
          resolution rate is compared here, because that is the only
          per-group figure this engine actually computes.
        </p>
      </div>
    </section>
  );
}

function Figure({ value, label, title }: { value: string; label: string; title: string }) {
  return (
    <div title={title}>
      <div className="figure num">{value}</div>
      <div className="eyebrow">{label}</div>
    </div>
  );
}
