// Cytoscape canvas (labels are drawn as plain text on a canvas, never parsed as markup).
import cytoscape, { type Core } from "cytoscape";
import { useEffect, useRef } from "react";
import type { GraphData } from "../lib/graph";
import { typeStyles } from "../lib/graphStyle";

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

/** Theme colours for the canvas (cytoscape draws on a canvas, so it needs concrete values). */
function token(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : fallback;
}

export function GraphCanvas({ data, onSelectEdge, onSelectNode, mini = false }: { data: GraphData; onSelectEdge: (id: string) => void; onSelectNode: (id: string) => void; mini?: boolean }) {
  const container = useRef<HTMLDivElement | null>(null);
  const cy = useRef<Core | null>(null);

  useEffect(() => {
    if (!container.current) return;
    provideCytoscapeStylesheet();
    const entity = token("--series-1", "#1d4ed8");
    const fallback = ["#1d4ed8", "#b45309", "#166534", "#7c3aed", "#0891b2", "#be123c"];
    const styles = typeStyles(data.nodes.map((n) => n.type));
    const typeRules = [...styles.entries()].map(([type, st]) => ({
      selector: `node[type = ${JSON.stringify(type)}]`,
      style: { "background-color": token(`--series-${st.series}`, fallback[st.series - 1]), shape: st.shape },
    }));
    const ink = token("--ink", "#1d2733");
    const muted = token("--muted", "#5b6573");
    const brand = token("--brand", "#0f6e66");
    const instance = cytoscape({
      container: container.current,
      elements: [
        ...data.nodes.map((n) => ({ data: { id: n.id, label: n.label, type: n.type } })),
        ...data.edges.map((e) => ({ data: { id: e.id, source: e.source, target: e.target, label: e.predicate, kind: e.edgeKind } })),
      ],
      layout: data.nodes.length > 80 ? { name: "grid" } : ({ name: "concentric", animate: false, minNodeSpacing: 85, padding: 40, concentric: (n: { degree: (b: boolean) => number }) => n.degree(false), levelWidth: () => 1 } as never),
      wheelSensitivity: 0.3,
      minZoom: 0.2,
      maxZoom: 1.6,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 10, "text-valign": "bottom", "text-margin-y": 4, "background-color": entity, color: ink, width: 20, height: 20, "border-width": 2, "border-color": "#ffffff" } },
        ...(typeRules as never[]),
        { selector: 'node[type = "unknown"]', style: { "background-color": muted } },
        { selector: "edge", style: { label: "data(label)", "font-size": 9, color: muted, "curve-style": "bezier", "target-arrow-shape": "triangle", width: 1.6, "line-color": ink, "target-arrow-color": ink, "text-rotation": "autorotate", "text-background-color": "#ffffff", "text-background-opacity": 0.85, "text-background-padding": "2px" } },
        { selector: 'edge[kind = "mention"]', style: { "line-style": "dashed", "line-color": muted, "target-arrow-color": muted } },
        { selector: 'edge[kind = "co_occurrence"], edge[kind = "co_firing"]', style: { "line-style": "dotted", "line-color": muted, "target-arrow-color": muted } },
        { selector: ":selected", style: { "border-width": 3, "border-color": brand, "line-color": brand, "target-arrow-color": brand } },
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

  return <div ref={container} className={mini ? "graph-canvas mini" : "graph-canvas"} data-testid="graph-canvas" role="img" aria-label={`graph with ${data.nodes.length} nodes and ${data.edges.length} edges`} />;
}
