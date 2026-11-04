// Dependency-free SVG charts (bars for categories, a line for time buckets). Labels are SVG text
// nodes (React text, never markup); colours are presentation attributes, so the strict CSP holds.
import { niceMax, type Series } from "../lib/charts";
import { clip, formatNumber, short } from "../lib/text";

const WIDTH = 640;
const ROW = 22;

export function Chart({ series, title }: { series: Series; title: string }) {
  return (
    <figure className="chart" aria-label={title}>
      <figcaption>
        {clip(title, 200)} <span className="muted">({series.measure} by {clip(series.field || "group", 80)})</span>
      </figcaption>
      {series.kind === "line" ? <LineChart series={series} /> : <BarChart series={series} />}
      {series.skipped.length ? <p className="muted">Not plotted (non-numeric value): {clip(series.skipped.join(", "), 400)}</p> : null}
      <table className="data">
        <thead>
          <tr>
            <th scope="col">{clip(series.field || "group", 80)}</th>
            <th scope="col" className="num">
              {series.measure}
            </th>
          </tr>
        </thead>
        <tbody>
          {series.points.map((p, i) => (
            <tr key={`${p.label}-${i}`}>
              <td>{clip(p.label, 200)}</td>
              <td className="num">{formatNumber(p.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

function BarChart({ series }: { series: Series }) {
  const max = niceMax(Math.max(...series.points.map((p) => p.value), 0));
  const labelWidth = 200;
  const barArea = WIDTH - labelWidth - 70;
  const height = series.points.length * ROW + 10;
  return (
    <svg className="svg-chart" viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-label="bar chart" data-testid="bar-chart">
      {series.points.map((p, i) => {
        const w = Math.max(0, (p.value / max) * barArea);
        const y = i * ROW + 4;
        return (
          <g key={`${p.label}-${i}`}>
            <text x={labelWidth - 6} y={y + 14} textAnchor="end" className="axis-label">
              {short(p.label, 28)}
            </text>
            <rect x={labelWidth} y={y} width={w} height={ROW - 6} className="bar" />
            <text x={labelWidth + w + 4} y={y + 14} className="value-label">
              {formatNumber(p.value)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function LineChart({ series }: { series: Series }) {
  const height = 220;
  const pad = { l: 56, r: 16, t: 12, b: 48 };
  const max = niceMax(Math.max(...series.points.map((p) => p.value), 0));
  const n = series.points.length;
  const x = (i: number) => pad.l + (n === 1 ? 0 : (i * (WIDTH - pad.l - pad.r)) / (n - 1));
  const y = (v: number) => pad.t + (1 - v / max) * (height - pad.t - pad.b);
  const path = series.points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
  const every = Math.max(1, Math.ceil(n / 8));
  return (
    <svg className="svg-chart" viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-label="line chart" data-testid="line-chart">
      <line x1={pad.l} x2={WIDTH - pad.r} y1={y(0)} y2={y(0)} className="axis" />
      <line x1={pad.l} x2={pad.l} y1={pad.t} y2={y(0)} className="axis" />
      <text x={pad.l - 6} y={y(max) + 4} textAnchor="end" className="axis-label">
        {formatNumber(max)}
      </text>
      <text x={pad.l - 6} y={y(0) + 4} textAnchor="end" className="axis-label">
        0
      </text>
      <path d={path} className="line" fill="none" />
      {series.points.map((p, i) => (
        <g key={`${p.label}-${i}`}>
          <circle cx={x(i)} cy={y(p.value)} r={3} className="dot">
            <title>{`${clip(p.label, 60)}: ${formatNumber(p.value)}`}</title>
          </circle>
          {i % every === 0 ? (
            <text x={x(i)} y={height - pad.b + 16} textAnchor="middle" className="axis-label">
              {short(p.label, 12)}
            </text>
          ) : null}
        </g>
      ))}
    </svg>
  );
}
