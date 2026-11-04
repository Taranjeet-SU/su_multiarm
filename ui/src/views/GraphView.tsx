import { useCallback, useMemo, useState, type FormEvent } from "react";
import { z } from "zod";
import { hasRoute } from "../api/features";
import { SearchResult } from "../api/schema";
import { ErrorBanner, KeyValues, Text } from "../components/common";
import { GraphCanvas } from "../components/GraphCanvas";
import { useApp } from "../context";
import { fromNeighbourhood, fromProvenance, type GraphData } from "../lib/graph";
import { displayText } from "../lib/text";

// A2 §4.3 GraphView / EdgeProvenance (used only when the server exposes the routes).
const GraphViewSchema = z
  .object({
    nodes: z.array(z.object({ id: z.string(), type: z.string(), name: z.string() }).passthrough()),
    edges: z.array(z.object({ id: z.string(), source: z.string(), target: z.string(), predicate: z.string(), edge_kind: z.string(), support: z.string() }).passthrough()),
    truncated: z.boolean(),
  })
  .passthrough();
const EdgeProvenanceSchema = z
  .object({ predicate: z.string(), edge_kind: z.string(), support: z.unknown(), spans: z.array(z.object({ document_id: z.string(), quote: z.string(), start: z.number().optional(), end: z.number().optional() }).passthrough()) })
  .passthrough();

const NEIGHBOURHOOD = "/v2/graph/neighbourhood";

export function GraphView() {
  const { api, features, lastAnswer, setLastSearch } = useApp();
  const [entity, setEntity] = useState("");
  const [data, setData] = useState<GraphData | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [edgeDetail, setEdgeDetail] = useState<z.infer<typeof EdgeProvenanceSchema> | null>(null);
  const native = hasRoute(features, "POST", NEIGHBOURHOOD);

  const explore = useCallback(
    async (name: string) => {
      const query = name.trim();
      if (!query) return;
      setBusy(true);
      setError(null);
      setSelected(null);
      setEdgeDetail(null);
      try {
        if (native) {
          setData(fromNeighbourhood(await api.post(NEIGHBOURHOOD, { query, hops: 1 }, GraphViewSchema)));
        } else {
          const result = await api.post("/v2/search", { query }, SearchResult);
          setLastSearch(result);
          setData(fromProvenance(result.arm_hits.graph ?? []));
        }
      } catch (exc) {
        setData(null);
        setError(exc);
      } finally {
        setBusy(false);
      }
    },
    [api, native, setLastSearch],
  );

  const submit = (e: FormEvent) => {
    e.preventDefault();
    void explore(entity);
  };

  const kinds = useMemo(() => [...new Set((data?.edges ?? []).map((e) => e.edgeKind))].sort(), [data]);
  const visible = useMemo<GraphData | null>(() => {
    if (!data) return null;
    const edges = data.edges.filter((e) => !hiddenKinds.has(e.edgeKind));
    return { ...data, edges };
  }, [data, hiddenKinds]);

  const onSelectEdge = useCallback(
    (id: string) => {
      setSelected(id);
      setEdgeDetail(null);
      if (native && hasRoute(features, "GET", "/v2/graph/edges/{edge_id}")) {
        api.get(`/v2/graph/edges/${encodeURIComponent(id)}`, EdgeProvenanceSchema).then(setEdgeDetail, setError);
      }
    },
    [api, features, native],
  );
  const onSelectNode = useCallback(
    (id: string) => {
      const node = data?.nodes.find((n) => n.id === id);
      if (node && node.type !== "document") {
        const name = id.startsWith("entity:") ? id.slice("entity:".length) : node.label;
        setEntity(name);
        void explore(name);
      }
    },
    [data, explore],
  );
  const edge = data?.edges.find((e) => e.id === selected) ?? null;
  const lastGraphHits = lastAnswer?.retrieval.arm_hits.graph ?? [];

  return (
    <div className="view graph">
      <form onSubmit={submit} className="row" aria-label="explore an entity">
        <label htmlFor="entity">Entity</label>
        <input id="entity" value={entity} onChange={(e) => setEntity(e.target.value)} placeholder="entity name, e.g. a dependency" maxLength={500} />
        <button type="submit" disabled={busy || !entity.trim()}>
          Explore
        </button>
        <button type="button" disabled={!lastGraphHits.length} onClick={() => setData(fromProvenance(lastGraphHits))} title="graph-arm evidence of the last answer">
          Use last answer ({lastGraphHits.length} graph hits)
        </button>
      </form>
      <p className="muted small" data-testid="graph-origin">
        {native
          ? "Source: the server's neighbourhood route (POST /v2/graph/neighbourhood)."
          : "Source: graph-arm evidence of a search (this server has no /v2/graph/neighbourhood route). Edges are the stored edges and mentions in that evidence only; this is not the full neighbourhood."}
      </p>
      <ErrorBanner error={error} />
      {busy ? <p aria-live="polite">loading…</p> : null}
      {data && !data.nodes.length ? <p className="muted">The graph arm returned no entities or edges for this entity in the current generation.</p> : null}
      {data && data.truncated ? <p className="banner banner-warn">Truncated: the server applied its node/edge bounds.</p> : null}
      {data && data.skippedRows ? <p className="muted small">{data.skippedRows} provenance row(s) carried no edge or mention and are not drawn.</p> : null}
      {kinds.length ? (
        <fieldset className="row">
          <legend>edge kinds</legend>
          {kinds.map((k) => (
            <label key={k}>
              <input
                type="checkbox"
                checked={!hiddenKinds.has(k)}
                onChange={() =>
                  setHiddenKinds((prev) => {
                    const next = new Set(prev);
                    if (next.has(k)) next.delete(k);
                    else next.add(k);
                    return next;
                  })
                }
              />{" "}
              {displayText(k)}
            </label>
          ))}
        </fieldset>
      ) : null}
      {visible && visible.nodes.length ? (
        <div className="graph-layout">
          <GraphCanvas data={visible} onSelectEdge={onSelectEdge} onSelectNode={onSelectNode} />
          <aside className="graph-side">
            <h3>
              {visible.nodes.length} nodes · {visible.edges.length} edges
            </h3>
            {edge ? (
              <div data-testid="edge-detail">
                <KeyValues
                  rows={[
                    ["subject", edge.source],
                    ["predicate", edge.predicate],
                    ["object", edge.target],
                    ["edge kind", edge.edgeKind],
                    ["support / match", edge.support],
                  ]}
                />
                <h4>Quotes</h4>
                <ul className="provenance">
                  {(edgeDetail ? edgeDetail.spans.map((s) => ({ documentId: s.document_id, quote: s.quote })) : edge.quotes).map((q, i) => (
                    <li key={i}>
                      <code>{displayText(q.documentId)}</code>
                      <blockquote>
                        <Text value={q.quote || "(no quote recorded)"} max={800} />
                      </blockquote>
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="muted">Select an edge to see its provenance quotes; select an entity to explore it.</p>
            )}
            <details>
              <summary>edge list</summary>
              <ul className="edge-list">
                {visible.edges.map((e) => (
                  <li key={e.id}>
                    <button type="button" className="linklike" onClick={() => onSelectEdge(e.id)}>
                      {displayText(e.source)} —{displayText(e.predicate)}→ {displayText(e.target)}
                    </button>
                  </li>
                ))}
              </ul>
            </details>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
