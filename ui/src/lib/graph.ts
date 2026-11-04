// Graph elements built only from what the server returned. Two sources, never mixed silently:
//
// * ``neighbourhood``: the inspect route ``POST /v2/graph/neighbourhood`` (addendum A2 §4.3), when
//   the server exposes it: its nodes and edges are used as given;
// * ``provenance``: the graph-arm evidence of a search (``arm_hits.graph[*].provenance``). A
//   ``graph_path`` row is a stored edge (subject, predicate, object, quote); a mention row links an
//   entity to the document whose passage mentions it (``mentioned_in``). Nothing else becomes an edge:
//   two entities in the same passage are **not** connected unless the server returned an edge.
import type { Hit } from "../api/schema";
import { short } from "./text";

export interface GraphNode {
  id: string;
  label: string;
  type: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  predicate: string;
  edgeKind: string;
  support: string;
  quotes: Array<{ documentId: string; quote: string; span?: [number, number] }>;
}

export interface GraphData {
  origin: "neighbourhood" | "provenance";
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  skippedRows: number; // provenance rows that carried no edge or mention (not drawn)
}

const str = (v: unknown): string | null => (typeof v === "string" && v.length ? v : null);

export function fromProvenance(hits: Hit[]): GraphData {
  const nodes = new Map<string, GraphNode>();
  const edges = new Map<string, GraphEdge>();
  let skipped = 0;
  const node = (id: string, type: string, label = id) => {
    if (!nodes.has(id)) nodes.set(id, { id, label: short(label, 48), type });
  };
  const edge = (source: string, target: string, predicate: string, edgeKind: string, support: string, documentId: string, quote: string, span?: [number, number]) => {
    const id = `${source}␟${predicate}␟${target}`;
    const existing = edges.get(id);
    const entry = { documentId, quote, span };
    if (existing) {
      if (existing.quotes.length < 20) existing.quotes.push(entry);
    } else {
      edges.set(id, { id, source, target, predicate, edgeKind, support, quotes: [entry] });
    }
  };
  for (const hit of hits) {
    for (const row of hit.provenance) {
      const subject = str(row.subject);
      const object = str(row.object);
      const predicate = str(row.predicate);
      const quote = typeof row.quote === "string" ? row.quote : "";
      const span = Array.isArray(row.span) && row.span.length === 2 && row.span.every((n) => typeof n === "number") ? (row.span as [number, number]) : undefined;
      if (subject && object && predicate) {
        node(subject, "entity");
        node(object, "entity");
        edge(subject, object, predicate, str(row.edge_kind) ?? "unknown", str(row.support) ?? "unknown", hit.document_id, quote, span);
        continue;
      }
      const entity = str(row.entity);
      if (entity) {
        const docNode = `doc:${hit.document_id}`;
        node(`entity:${entity}`, "entity", entity);
        node(docNode, "document", hit.title || hit.document_id);
        edge(`entity:${entity}`, docNode, "mentioned_in", "mention", str(row.match_level) ?? "unknown", hit.document_id, quote, span);
        continue;
      }
      skipped += 1;
    }
  }
  return { origin: "provenance", nodes: [...nodes.values()], edges: [...edges.values()], truncated: false, skippedRows: skipped };
}

/** The A2 §4.3 ``GraphView`` payload, validated by the caller's schema. */
export function fromNeighbourhood(view: {
  nodes: Array<{ id: string; type: string; name: string }>;
  edges: Array<{ id: string; source: string; target: string; predicate: string; edge_kind: string; support: string }>;
  truncated: boolean;
}): GraphData {
  const ids = new Set(view.nodes.map((n) => n.id));
  return {
    origin: "neighbourhood",
    nodes: view.nodes.map((n) => ({ id: n.id, label: short(n.name, 48), type: n.type })),
    // An edge whose endpoint the server did not return is dropped and counted, never drawn to a ghost.
    edges: view.edges.filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ id: e.id, source: e.source, target: e.target, predicate: e.predicate, edgeKind: e.edge_kind, support: e.support, quotes: [] })),
    truncated: view.truncated,
    skippedRows: view.edges.filter((e) => !ids.has(e.source) || !ids.has(e.target)).length,
  };
}
