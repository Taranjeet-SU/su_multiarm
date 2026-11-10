// A small map of the graph arm's evidence for one answer (stored edges and mentions only), with the
// legend for node types and edge kinds. Loaded lazily (cytoscape is a separate chunk).
import { useCallback, useMemo, useState } from "react";
import type { Hit } from "../api/schema";
import { fromProvenance } from "../lib/graph";
import { displayText } from "../lib/text";
import { GraphLegend } from "./GraphLegend";
import { GraphCanvas } from "./GraphCanvas";
import { Text } from "./common";

export default function MiniGraph({ hits }: { hits: Hit[] }) {
  const data = useMemo(() => fromProvenance(hits), [hits]);
  const [edge, setEdge] = useState<string | null>(null);
  const onEdge = useCallback((id: string) => setEdge(id), []);
  const onNode = useCallback(() => undefined, []);
  if (!data.nodes.length) return <p className="muted">The graph arm's evidence carries no entities or edges.</p>;
  const selected = data.edges.find((e) => e.id === edge) ?? null;
  return (
    <div className="chart-card" data-testid="mini-graph">
      <div className="card-head">
        <h3>
          {data.nodes.length} nodes and {data.edges.length} edges in this answer's graph evidence
        </h3>
      </div>
      <GraphLegend kinds={[...new Set(data.edges.map((e) => e.edgeKind))]} types={[...new Set(data.nodes.map((n) => n.type))]} />
      <GraphCanvas data={data} onSelectEdge={onEdge} onSelectNode={onNode} mini />
      {selected ? (
        <p className="small">
          <code>{displayText(selected.source)}</code> {displayText(selected.predicate)} <code>{displayText(selected.target)}</code>:{" "}
          <Text value={selected.quotes[0]?.quote || "(no quote recorded)"} max={400} />
        </p>
      ) : (
        <p className="muted small">Select an edge to see the passage it was read from.</p>
      )}
    </div>
  );
}
