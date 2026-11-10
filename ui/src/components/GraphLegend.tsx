import { typeStyles } from "../lib/graphStyle";
import { displayText } from "../lib/text";

const EDGE_LABEL: Record<string, string> = { fact: "fact (solid)", mention: "mentioned in (dashed)", co_occurrence: "co-occurrence (dotted)", co_firing: "co-firing (dotted)", native_link: "native link (solid)", derived: "derived (solid)" };
const EDGE_CLASS: Record<string, string> = { fact: "edge-fact", mention: "edge-mention", co_occurrence: "edge-co", co_firing: "edge-co" };

/** Node types by shape and colour, edge kinds by line style (never colour alone). */
export function GraphLegend({ kinds, types }: { kinds: string[]; types: string[] }) {
  return (
    <div className="legend" aria-label="graph legend">
      {[...typeStyles(types).entries()].map(([t, st]) => (
        <span key={`t-${t}`}>
          <i className={t === "unknown" ? "swatch node-unknown" : `swatch s${st.series} shape-${st.shape}`} aria-hidden="true" />
          {displayText(t)}
        </span>
      ))}
      {kinds.map((k) => (
        <span key={`k-${k}`}>
          <i className={`swatch ${EDGE_CLASS[k] ?? "edge-fact"}`} aria-hidden="true" />
          {displayText(EDGE_LABEL[k] ?? k)}
        </span>
      ))}
    </div>
  );
}
