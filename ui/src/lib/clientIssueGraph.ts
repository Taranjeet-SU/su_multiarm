import { z } from "zod";
import type { GraphData, GraphEdge, GraphNode } from "./graph";
import { formatNumber, short } from "./text";

export const IssueRow = z.object({
  tenant: z.string(), service: z.string(), signature_id: z.string(),
  error_type: z.string(), error_count: z.number().nonnegative(), level: z.enum(["ERROR", "FATAL", "error", "fatal", "RULE"]),
  supporting_template: z.string().optional(), evidence_id: z.string().optional(),
}).passthrough();
export type IssueRow = z.infer<typeof IssueRow>;

export const IssueOutput = z.object({
  status: z.enum(["answered", "empty", "bounded", "refused"]), rows: z.array(IssueRow),
  rendering: z.string(), exactness: z.string(), source_unavailable: z.array(z.string()).default([]),
  bounded_by: z.array(z.string()).default([]), notes: z.array(z.string()).default([]),
  truncated: z.boolean().default(false), refusal: z.string().nullable().optional(),
  window: z.record(z.string(), z.string()).nullable().optional(), latest_available_day: z.string().nullable().optional(),
}).passthrough();

// JSON tuple identifiers prevent user-controlled names or delimiters from colliding. A service
// node belongs to one client: merging services globally would invent client→issue paths.
export const clientId = (row: IssueRow) => JSON.stringify(["client", row.tenant]);
export const serviceId = (row: IssueRow) => JSON.stringify(["service", row.tenant, row.service]);
export const issueId = (row: IssueRow) => JSON.stringify(["issue", row.signature_id, row.level.toUpperCase()]);
export const signalId = (row: IssueRow) => JSON.stringify(["signal", row.level.toUpperCase()]);

export function issueGraph(rows: IssueRow[], truncated: boolean): GraphData {
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge & { count: number }>();
  const node = (id: string, type: string, label: string) => nodes.set(id, { id, type, label: short(label, 45) });
  const link = (source: string, target: string, count: number) => {
    const id = JSON.stringify([source, target]);
    const previous = edges.get(id);
    const total = count + (previous?.count ?? 0);
    edges.set(id, { id, source, target, count: total, predicate: `${formatNumber(total)} errors`, edgeKind: "derived", support: "Sum of the displayed classified event rows; association, not cause", quotes: [] });
  };
  for (const row of rows) {
    node(clientId(row), "client", row.tenant || "Unspecified client");
    node(serviceId(row), "service", row.service || "Unspecified service");
    node(issueId(row), "issue", row.error_type);
    node(signalId(row), "signal", `${row.level.toUpperCase()} signal`);
    link(clientId(row), serviceId(row), row.error_count);
    link(serviceId(row), issueId(row), row.error_count);
    link(issueId(row), signalId(row), row.error_count);
  }
  return { origin: "aggregate", nodes: [...nodes.values()], edges: [...edges.values()], truncated, skippedRows: 0 };
}

export function rowsForSelection(rows: IssueRow[], id: string | null, graph: GraphData): IssueRow[] {
  if (!id) return rows;
  const edge = graph.edges.find((e) => e.id === id);
  return rows.filter((row) => {
    const ids = [clientId(row), serviceId(row), issueId(row), signalId(row)];
    return edge ? ids.includes(edge.source) && ids.includes(edge.target) : ids.includes(id);
  });
}
