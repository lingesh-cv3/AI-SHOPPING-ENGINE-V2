import type { SalesSeries } from "./api";

/** A real, data-driven line+area chart, hand-rolled in SVG - this codebase
 *  has no charting library, and one point on a line isn't worth adding one
 *  for. "This period" is a solid filled line; "Previous period" is a
 *  dashed line at the same day-offset, so a merchant can see whether today
 *  is ahead of or behind the equivalent day last period - never the
 *  fabricated numbers a screenshot reference might show, always whatever
 *  `daily_revenue_series` actually returned for this connection.
 *
 *  Renders nothing false: with no data at all it says so rather than
 *  drawing a flat line at zero that could be mistaken for "zero revenue
 *  measured" instead of "nothing to chart yet".
 */
export function RevenueTrendChart({ series }: { series: SalesSeries | null }) {
  if (!series || !series.has_data) {
    return (
      <p className="empty" style={{ margin: 0 }}>
        No completed sales in this window yet - the trend will appear once
        there is something to chart.
      </p>
    );
  }

  const width = 640;
  const height = 220;
  const padLeft = 8;
  const padRight = 8;
  const padTop = 12;
  const padBottom = 24;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;

  const currentValues = series.current.map((d) => Number(d.amount));
  const priorValues = series.prior.map((d) => Number(d.amount));
  const max = Math.max(1, ...currentValues, ...priorValues);

  const n = series.current.length;
  const xFor = (i: number) => padLeft + (n <= 1 ? 0 : (i / (n - 1)) * plotW);
  const yFor = (v: number) => padTop + plotH - (v / max) * plotH;

  const currentPoints = currentValues.map((v, i) => [xFor(i), yFor(v)] as const);
  const priorPoints = priorValues.map((v, i) => [xFor(i), yFor(v)] as const);

  const toPolyline = (pts: readonly (readonly [number, number])[]) =>
    pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");

  const areaPath =
    currentPoints.length > 0
      ? `M ${currentPoints[0][0].toFixed(1)},${(padTop + plotH).toFixed(1)} ` +
        currentPoints.map(([x, y]) => `L ${x.toFixed(1)},${y.toFixed(1)}`).join(" ") +
        ` L ${currentPoints[currentPoints.length - 1][0].toFixed(1)},${(padTop + plotH).toFixed(1)} Z`
      : "";

  // A handful of x-axis labels, not one per day - dense date labels on a
  // 30-day chart would overlap and read as noise.
  const labelCount = Math.min(6, n);
  const labelIndices = Array.from({ length: labelCount }, (_, i) =>
    Math.round((i / Math.max(1, labelCount - 1)) * (n - 1)),
  );

  return (
    <div className="trend-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        preserveAspectRatio="none"
        role="img"
        aria-label="Revenue trend for this period compared to the previous period"
      >
        {areaPath && <path d={areaPath} className="trend-chart-area" />}
        <polyline points={toPolyline(priorPoints)} className="trend-chart-line prior" />
        <polyline points={toPolyline(currentPoints)} className="trend-chart-line current" />
      </svg>
      <div className="trend-chart-axis">
        {labelIndices.map((i) => (
          <span key={i}>
            {new Date(series.current[i].date).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
            })}
          </span>
        ))}
      </div>
      <div className="trend-chart-legend">
        <span className="trend-chart-legend-item">
          <span className="trend-chart-swatch current" /> This period
        </span>
        <span className="trend-chart-legend-item">
          <span className="trend-chart-swatch prior" /> Previous period
        </span>
      </div>
    </div>
  );
}
