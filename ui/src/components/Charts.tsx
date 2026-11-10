// Dependency-free SVG charts following one method: the simplest form for the task (ranking → sorted
// bars, change over time → a line, two categorical dimensions → a heatmap, part of a target → a
// bullet gauge, intervals → a timeline), the finding written in the caption, direct labels instead
// of legends, subtle gridlines, bars from zero, and exactness never by colour alone (approximate bars
// are hatched and their label starts with ≈). Colours come from theme variables via CSS classes.
import type { ReactNode } from "react";
import { niceMax } from "../lib/charts";
import { clip, formatNumber, short } from "../lib/text";

export interface Bar {
  label: string;
  value: number;
  approximate?: boolean;
}

const W = 720;

function Frame({ title, finding, children, footer, testId }: { title: string; finding?: string; children: ReactNode; footer?: ReactNode; testId?: string }) {
  return (
    <figure className="chart" data-testid={testId}>
      <figcaption>{clip(title, 200)}</figcaption>
      {finding ? <p className="finding">{clip(finding, 300)}</p> : null}
      {children}
      {footer}
    </figure>
  );
}

function HatchDefs() {
  return (
    <defs>
      <pattern id="hatch-approx" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="6" className="hatch-line" />
      </pattern>
    </defs>
  );
}

/** Ranked horizontal bars (top-N). Sorted descending; ties keep server order. */
export function RankedBars({ title, bars, unit = "", testId = "ranked-bars" }: { title: string; bars: Bar[]; unit?: string; testId?: string }) {
  const sorted = [...bars].sort((a, b) => b.value - a.value);
  const max = niceMax(Math.max(0, ...sorted.map((b) => b.value)));
  const labelW = 190;
  const area = W - labelW - 80;
  const row = 26;
  const height = sorted.length * row + 8;
  const leader = sorted[0];
  const approx = sorted.some((b) => b.approximate);
  const finding = leader ? `${leader.label} leads with ${leader.approximate ? "≈" : ""}${formatNumber(leader.value)}${unit ? ` ${unit}` : ""}${sorted.length > 1 ? `, ${formatNumber(leader.value - sorted[1].value)} ahead of ${sorted[1].label}` : ""}.` : undefined;
  return (
    <Frame
      title={title}
      finding={finding}
      testId={testId}
      footer={
        approx ? (
          <div className="legend">
            <span>
              <i className="swatch approx" aria-hidden="true" />≈ hatched bars are approximate (estimated from a sample)
            </span>
          </div>
        ) : null
      }
    >
      <svg className="svg-chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label={title} data-testid="bar-chart">
        <HatchDefs />
        {[0.25, 0.5, 0.75, 1].map((f) => (
          <line key={f} x1={labelW + f * area} x2={labelW + f * area} y1={0} y2={height} className="grid" />
        ))}
        {sorted.map((b, i) => {
          const w = Math.max(1, (b.value / max) * area);
          const y = i * row + 4;
          return (
            <g key={`${b.label}-${i}`}>
              <text x={labelW - 8} y={y + 14} textAnchor="end" className="axis-label">
                {short(b.label, 30)}
              </text>
              <rect x={labelW} y={y} width={w} height={row - 9} rx={3} className={b.approximate ? "bar approx" : "bar"} />
              <text x={labelW + w + 6} y={y + 14} className="value-label">
                {b.approximate ? "≈" : ""}
                {formatNumber(b.value)}
              </text>
            </g>
          );
        })}
      </svg>
    </Frame>
  );
}

export interface Point {
  label: string; // ISO day or hour
  value: number | null; // null: no composed data for this bucket (drawn as a gap, never 0)
}

/** A line over day or hour buckets; missing buckets break the line and are marked. */
export function TrendLine({ title, points, unit = "lines", testId = "trend-line" }: { title: string; points: Point[]; unit?: string; testId?: string }) {
  const height = 240;
  const pad = { l: 52, r: 18, t: 16, b: 46 };
  const values = points.map((p) => p.value).filter((v): v is number => v !== null);
  const max = niceMax(Math.max(0, ...values));
  const n = points.length;
  const x = (i: number) => pad.l + (n <= 1 ? (W - pad.l - pad.r) / 2 : (i * (W - pad.l - pad.r)) / (n - 1));
  const y = (v: number) => pad.t + (1 - v / max) * (height - pad.t - pad.b);
  const segments: string[] = [];
  let current = "";
  points.forEach((p, i) => {
    if (p.value === null) {
      if (current) segments.push(current);
      current = "";
      return;
    }
    current += `${current ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`;
  });
  if (current) segments.push(current);
  const peakIndex = points.reduce((best, p, i) => (p.value !== null && (best < 0 || p.value > (points[best].value ?? -1)) ? i : best), -1);
  const first = points.find((p) => p.value !== null);
  const last = [...points].reverse().find((p) => p.value !== null);
  const gaps = points.filter((p) => p.value === null).length;
  const finding =
    first && last && first !== last
      ? `${formatNumber(first.value)} → ${formatNumber(last.value)} ${unit} from ${first.label} to ${last.label}; peak ${formatNumber(points[peakIndex].value)} on ${points[peakIndex].label}.${gaps ? ` ${gaps} bucket(s) without data.` : ""}`
      : undefined;
  const every = Math.max(1, Math.ceil(n / 8));
  return (
    <Frame title={title} finding={finding} testId={testId}>
      <svg className="svg-chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label={title} data-testid="line-chart">
        {[0, 0.5, 1].map((f) => (
          <g key={f}>
            <line x1={pad.l} x2={W - pad.r} y1={y(max * f)} y2={y(max * f)} className="grid" />
            <text x={pad.l - 8} y={y(max * f) + 4} textAnchor="end" className="axis-label muted">
              {formatNumber(max * f)}
            </text>
          </g>
        ))}
        {segments.map((d, i) => (
          <path key={i} d={d} className="line" />
        ))}
        {points.map((p, i) => (
          <g key={`${p.label}-${i}`}>
            {p.value === null ? (
              <circle cx={x(i)} cy={y(0)} r={4} className="dot gap">
                <title>{`${short(p.label, 40)}: no composed data`}</title>
              </circle>
            ) : (
              <circle cx={x(i)} cy={y(p.value)} r={i === peakIndex ? 5 : 3.5} className={i === peakIndex ? "dot peak" : "dot"}>
                <title>{`${short(p.label, 40)}: ${formatNumber(p.value)} ${unit}`}</title>
              </circle>
            )}
            {i === peakIndex && p.value !== null ? (
              <text x={x(i)} y={y(p.value) - 10} textAnchor="middle" className="value-label">
                {formatNumber(p.value)}
              </text>
            ) : null}
            {i % every === 0 || i === n - 1 ? (
              <text x={x(i)} y={height - pad.b + 18} textAnchor="middle" className="axis-label muted">
                {short(p.label.length > 10 ? p.label.slice(5, 16).replace("T", " ") : p.label.slice(5), 12)}
              </text>
            ) : null}
          </g>
        ))}
      </svg>
    </Frame>
  );
}

/** Rows × columns of counts on a sequential ramp (quantised to 5 steps, values printed). */
export function Heatmap({ title, rows, cols, value, rowLabel, colLabel }: { title: string; rows: string[]; cols: string[]; value: (r: string, c: string) => number | null; rowLabel: string; colLabel: string }) {
  const all = rows.flatMap((r) => cols.map((c) => value(r, c))).filter((v): v is number => v !== null && v > 0);
  const max = Math.max(1, ...all);
  const level = (v: number | null) => (v === null || v <= 0 ? 0 : Math.min(5, Math.max(1, Math.ceil((v / max) * 5))));
  let best: [string, string, number] | null = null;
  for (const r of rows) for (const c of cols) {
    const v = value(r, c);
    if (v !== null && (!best || v > best[2])) best = [r, c, v];
  }
  return (
    <Frame title={title} finding={best ? `Most lines: ${best[0]} × ${best[1]} (${formatNumber(best[2])}).` : undefined} testId="heatmap">
      <div className="heat-wrap">
        <table className="heat" aria-label={title}>
          <thead>
            <tr>
              <th scope="col">
                {short(rowLabel, 16)} \ {short(colLabel, 16)}
              </th>
              {cols.map((c) => (
                <th key={c} scope="col">
                  {short(c, 14)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r}>
                <th scope="row">{short(r, 18)}</th>
                {cols.map((c) => {
                  const v = value(r, c);
                  return (
                    <td key={c} className={`h${level(v)}`} title={`${r} × ${c}: ${v === null ? "no data" : formatNumber(v)}`}>
                      {v === null ? "·" : formatNumber(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Frame>
  );
}

export interface Gauge {
  label: string;
  regex: number | null; // deterministic coverage 0..1
  withModel: number | null; // extracted coverage 0..1 (null: not reported)
  detail?: string;
}

/** One bullet per service: regex-only coverage solid, with-model coverage as the lighter extension,
 * target ticks at 0.90 (regex) and 0.99 (with model). The two numbers are always printed apart. */
export function CoverageGauges({ title, gauges, regexTarget = 0.9, modelTarget = 0.99 }: { title: string; gauges: Gauge[]; regexTarget?: number; modelTarget?: number }) {
  const labelW = 150;
  const area = W - labelW - 210;
  const row = 30;
  const height = gauges.length * row + 10;
  const below = gauges.filter((g) => g.regex !== null && g.regex < regexTarget);
  return (
    <Frame
      title={title}
      testId="coverage-gauges"
      finding={gauges.length ? (below.length ? `${below.length} of ${gauges.length} service(s) below the ${Math.round(regexTarget * 100)} % regex-only target: ${below.map((g) => g.label).join(", ")}.` : `Every service meets the ${Math.round(regexTarget * 100)} % regex-only target.`) : undefined}
      footer={
        <div className="legend">
          <span>
            <i className="swatch" aria-hidden="true" />
            regex only (deterministic)
          </span>
          <span>
            <i className="swatch s2" aria-hidden="true" />
            with model (extracted)
          </span>
          <span>| targets {Math.round(regexTarget * 100)} % and {Math.round(modelTarget * 100)} %</span>
        </div>
      }
    >
      <svg className="svg-chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label={title}>
        {gauges.map((g, i) => {
          const y = i * row + 6;
          const regexW = (g.regex ?? 0) * area;
          const modelW = g.withModel !== null && g.regex !== null ? Math.max(0, g.withModel - g.regex) * area : 0;
          return (
            <g key={g.label}>
              <text x={labelW - 8} y={y + 14} textAnchor="end" className="axis-label">
                {short(g.label, 22)}
              </text>
              <rect x={labelW} y={y} width={area} height={18} rx={4} className="gauge-track" />
              <rect x={labelW} y={y} width={regexW} height={18} rx={4} className="gauge-regex" />
              {modelW ? <rect x={labelW + regexW} y={y} width={modelW} height={18} className="gauge-model" /> : null}
              <line x1={labelW + regexTarget * area} x2={labelW + regexTarget * area} y1={y - 2} y2={y + 20} className="target" />
              <line x1={labelW + modelTarget * area} x2={labelW + modelTarget * area} y1={y - 2} y2={y + 20} className="target" />
              <text x={labelW + area + 10} y={y + 14} className="value-label">
                {g.regex === null ? "n/a" : `${(g.regex * 100).toFixed(1)} %`}
                <tspan className="axis-label muted"> regex · {g.withModel === null ? "model n/a" : `${(g.withModel * 100).toFixed(1)} % with model`}</tspan>
              </text>
            </g>
          );
        })}
      </svg>
    </Frame>
  );
}

export interface Interval {
  label: string;
  sublabel?: string;
  start: number; // epoch ms
  end: number;
  peak?: number | null;
  value?: number | null;
}

/** Intervals on one time axis (incidents: first → last hour, peak marked). */
export function IntervalTimeline({ title, intervals, finding }: { title: string; intervals: Interval[]; finding?: string }) {
  if (!intervals.length) return null;
  const lo = Math.min(...intervals.map((i) => i.start));
  const hi = Math.max(...intervals.map((i) => i.end), lo + 3600_000);
  const labelW = 230;
  const area = W - labelW - 70;
  const row = 34;
  const height = intervals.length * row + 30;
  const x = (t: number) => labelW + ((t - lo) / (hi - lo)) * area;
  const fmt = (t: number) => new Date(t).toISOString().slice(5, 16).replace("T", " ");
  return (
    <Frame title={title} finding={finding} testId="interval-timeline">
      <svg className="svg-chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label={title}>
        <line x1={labelW} x2={labelW + area} y1={height - 22} y2={height - 22} className="axis" />
        <text x={labelW} y={height - 6} className="axis-label muted">
          {fmt(lo)} UTC
        </text>
        <text x={labelW + area} y={height - 6} textAnchor="end" className="axis-label muted">
          {fmt(hi)} UTC
        </text>
        {intervals.map((iv, i) => {
          const y = i * row + 6;
          return (
            <g key={`${iv.label}-${i}`}>
              <text x={labelW - 8} y={y + 12} textAnchor="end" className="axis-label">
                {short(iv.label, 32)}
              </text>
              {iv.sublabel ? (
                <text x={labelW - 8} y={y + 25} textAnchor="end" className="axis-label muted">
                  {short(iv.sublabel, 36)}
                </text>
              ) : null}
              <rect x={x(iv.start)} y={y + 3} width={Math.max(4, x(iv.end) - x(iv.start))} height={14} rx={7} className="span" />
              {iv.peak ? <circle cx={x(iv.peak)} cy={y + 10} r={5} className="peak" /> : null}
              {iv.value !== undefined && iv.value !== null ? (
                <text x={Math.max(x(iv.end), x(iv.start) + 4) + 6} y={y + 14} className="value-label">
                  {formatNumber(iv.value)}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
    </Frame>
  );
}
