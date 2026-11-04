// Chart inputs from a log tool's output (``runtime/log_tools.py::LogToolOutput``): its ``rows`` as
// returned. Nothing is re-aggregated or filled: a day the server lists in ``source_unavailable`` is a
// gap (null), never 0.
import type { Bar, Point } from "../components/Charts";

export type Output = Record<string, unknown>;

const DIMENSIONS = ["date", "hour", "period", "service", "signature_id", "tenant", "endpoint", "exception_class", "level", "category"];
const MEASURES = ["lines", "lines_total", "tenants_affected", "count", "latency_ms_max", "latency_ms_p95", "deterministic_coverage"];

export function rowsOf(output: Output): Record<string, unknown>[] {
  return Array.isArray(output.rows) ? (output.rows as unknown[]).filter((r): r is Record<string, unknown> => !!r && typeof r === "object" && !Array.isArray(r)) : [];
}

export function keysOf(rows: Record<string, unknown>[]): { label: string | null; value: string | null } {
  if (!rows.length) return { label: null, value: null };
  const keys = Object.keys(rows[0]);
  const label = DIMENSIONS.find((k) => keys.includes(k)) ?? keys.find((k) => rows.every((r) => typeof r[k] === "string")) ?? null;
  const value = MEASURES.find((k) => keys.includes(k) && rows.some((r) => typeof r[k] === "number")) ?? keys.find((k) => k !== label && rows.some((r) => typeof r[k] === "number")) ?? null;
  return { label, value };
}

export function isTemporal(label: string | null): boolean {
  return label === "date" || label === "hour";
}

export function barsOf(output: Output): Bar[] {
  const rows = rowsOf(output);
  const { label, value } = keysOf(rows);
  if (!label || !value) return [];
  const approximateAll = output.exactness === "approximate";
  return rows
    .filter((r) => typeof r[value] === "number")
    .map((r) => ({ label: String(r[label]), value: r[value] as number, approximate: approximateAll || r.row_exactness === "approximate" }));
}

export function pointsOf(output: Output): Point[] {
  const rows = rowsOf(output);
  const { label, value } = keysOf(rows);
  if (!label || !value) return [];
  const byLabel = new Map<string, number | null>();
  for (const r of rows) byLabel.set(String(r[label]), typeof r[value] === "number" ? (r[value] as number) : null);
  for (const day of Array.isArray(output.source_unavailable) ? (output.source_unavailable as unknown[]) : []) {
    if (typeof day === "string" && !byLabel.has(day)) byLabel.set(day, null);
  }
  return [...byLabel.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([l, v]) => ({ label: l, value: v }));
}
