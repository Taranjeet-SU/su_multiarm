// Chart series derived from a structured aggregate record. Only values present in the record are
// plotted: a group with a non-numeric value is listed as such and never coerced to 0.
import type { Aggregate } from "../api/schema";

export interface Point {
  label: string;
  value: number;
}

export interface Series {
  kind: "bar" | "line";
  field: string;
  measure: string;
  points: Point[];
  skipped: string[]; // group labels whose value is not a number (shown, not plotted)
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?$/;

function keyLabel(key: Record<string, unknown>, field: string | null | undefined): string {
  if (field && field in key) return String(key[field]);
  const values = Object.values(key);
  return values.length === 1 ? String(values[0]) : values.map(String).join(" | ");
}

export function seriesOf(aggregate: Aggregate, timeFields: ReadonlySet<string> = new Set()): Series | null {
  const field = aggregate.field ?? "";
  const points: Point[] = [];
  const skipped: string[] = [];
  let measure = "count";
  if (aggregate.groups && aggregate.groups.length) {
    const numericValues = aggregate.groups.every((g) => typeof g.value === "number");
    measure = numericValues ? "value" : "count";
    for (const g of aggregate.groups) {
      const label = keyLabel(g.key, field.split("|")[0]);
      const value = numericValues ? (g.value as number) : g.count;
      if (typeof value === "number" && Number.isFinite(value)) points.push({ label, value });
      else skipped.push(label);
    }
  } else if (aggregate.value && typeof aggregate.value === "object" && !Array.isArray(aggregate.value)) {
    for (const [label, value] of Object.entries(aggregate.value as Record<string, unknown>)) {
      if (typeof value === "number" && Number.isFinite(value)) points.push({ label, value });
      else skipped.push(label);
    }
    measure = "value";
  } else {
    return null;
  }
  if (!points.length) return skipped.length ? { kind: "bar", field, measure, points, skipped } : null;
  const temporal = timeFields.has(field) || aggregate.operation === "compare_periods" || points.every((p) => ISO_DAY.test(p.label));
  if (temporal) points.sort((a, b) => (a.label < b.label ? -1 : a.label > b.label ? 1 : 0));
  return { kind: temporal && points.length > 1 ? "line" : "bar", field, measure, points, skipped };
}

export function niceMax(max: number): number {
  if (max <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(max));
  for (const step of [1, 2, 2.5, 5, 10]) if (step * power >= max) return step * power;
  return 10 * power;
}
