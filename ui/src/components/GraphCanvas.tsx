// Cytoscape canvas (labels are drawn as plain text on a canvas, never parsed as markup).
import cytoscape, { type Core } from "cytoscape";
import { useEffect, useRef } from "react";
import type { GraphData } from "../lib/graph";

// cytoscape injects an inline <style> (".__________cytoscape_container { position: relative }")
// unless an element with this id exists; the strict CSP (style-src 'self') would block it. The rule
// lives in styles.css instead, and this placeholder tells cytoscape it is already provided.
const CYTOSCAPE_STYLESHEET_ID = "__________cytoscape_stylesheet";
function provideCytoscapeStylesheet(): void {
  if (document.getElementById(CYTOSCAPE_STYLESHEET_ID)) return;
  const marker = document.createElement("meta");
  marker.id = CYTOSCAPE_STYLESHEET_ID;
  marker.setAttribute("name", "cytoscape-stylesheet-in-bundle");
  document.head.appendChild(marker);
}

const TYPE_SHAPES: Record<string, string> = { entity: "ellipse", document: "round-rectangle" };

export function GraphCanvas({ data, onSelectEdge, onSelectNode }: { data: GraphData; onSelectEdge: (id: string) => void; onSelectNode: (id: string) => void }) {
  const container = useRef<HTMLDivElement | null>(null);
  const cy = useRef<Core | null>(null);

  useEffect(() => {
    if (!container.current) return;
    provideCytoscapeStylesheet();
    const instance = cytoscape({
      container: container.current,
      elements: [
        ...data.nodes.map((n) => ({ data: { id: n.id, label: n.label, type: n.type } })),
        ...data.edges.map((e) => ({ data: { id: e.id, source: e.source, target: e.target, label: e.predicate, kind: e.edgeKind } })),
      ],
      layout: { name: data.nodes.length > 60 ? "grid" : "cose", animate: false },
      wheelSensitivity: 0.3,
      minZoom: 0.2,
      maxZoom: 1.6,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, "text-valign": "bottom", "text-margin-y": 4, "background-color": "#3b6ea8", color: "#1f2328", width: 18, height: 18 } },
        ...Object.entries(TYPE_SHAPES).map(([type, shape]) => ({ selector: `node[type = "${type}"]`, style: { shape: shape as "ellipse" } })),
        { selector: 'node[type = "document"]', style: { "background-color": "#c98a1b" } },
        { selector: "edge", style: { label: "data(label)", "font-size": 9, "curve-style": "bezier", "target-arrow-shape": "triangle", width: 1.5, "line-color": "#6e7781", "target-arrow-color": "#6e7781", "text-rotation": "autorotate" } },
        { selector: 'edge[kind = "mention"]', style: { "line-style": "dashed" } },
        { selector: 'edge[kind = "co_occurrence"]', style: { "line-style": "dotted" } },
        { selector: ":selected", style: { "border-width": 3, "border-color": "#cf222e", "line-color": "#cf222e", "target-arrow-color": "#cf222e" } },
      ],
    });
    instance.on("tap", "edge", (evt) => onSelectEdge(evt.target.id()));
    instance.on("tap", "node", (evt) => onSelectNode(evt.target.id()));
    cy.current = instance;
    return () => {
      instance.destroy();
      cy.current = null;
    };
  }, [data, onSelectEdge, onSelectNode]);

  return <div ref={container} className="graph-canvas" data-testid="graph-canvas" role="img" aria-label={`graph with ${data.nodes.length} nodes and ${data.edges.length} edges`} />;
}
