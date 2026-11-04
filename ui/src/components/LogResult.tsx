// One log tool result as the server returned it: its rendering sentence (the same sentence on every
// surface), status and exactness, the chart that fits its rows, the rows table, basis, methods and
// notes. Days without composed data are listed, never shown as zero.
import { barsOf, isTemporal, keysOf, pointsOf, rowsOf, type Output } from "../lib/logRows";
import { displayText, formatNumber } from "../lib/text";
import { RankedBars, TrendLine } from "./Charts";
import { ExactnessBadge, JsonBlock, Text } from "./common";

export function LogResult({ tool, output, compact = false }: { tool: string; output: Output; compact?: boolean }) {
  const rows = rowsOf(output);
  const { label, value } = keysOf(rows);
  const status = typeof output.status === "string" ? output.status : null;
  const exactness = typeof output.exactness === "string" ? output.exactness : undefined;
  const windowLabel = (output.window as { label?: string } | undefined)?.label;
  const title = `${tool.replace(/^log_/, "")}${value ? `: ${value.replace(/_/g, " ")}` : ""}${label ? ` by ${label.replace(/_/g, " ")}` : ""}${windowLabel ? `, ${windowLabel}` : ""}`;
  const unavailable = Array.isArray(output.source_unavailable) ? (output.source_unavailable as unknown[]).map(String) : [];
  const listOf = (k: string) => (Array.isArray(output[k]) ? (output[k] as unknown[]).map(String) : []);
  return (
    <div className="log-result" data-testid={`log-result-${tool}`}>
      <div className="rendering">
        <ExactnessBadge exactness={exactness} /> {status && status !== "answered" ? <span className="badge">{displayText(status)}</span> : null}{" "}
        <Text value={output.rendering} max={1200} />
      </div>
      {rows.length && label && value ? (
        isTemporal(label) ? (
          <TrendLine title={title} points={pointsOf(output)} unit={value.replace(/_/g, " ")} />
        ) : (
          <RankedBars title={title} bars={barsOf(output).slice(0, compact ? 8 : 25)} unit={value.replace(/_/g, " ")} />
        )
      ) : null}
      {unavailable.length ? <p className="banner banner-warn">No composed data for: {displayText(unavailable.join(", "))} (not counted as zero).</p> : null}
      {!compact && rows.length ? (
        <details>
          <summary>{rows.length} row(s)</summary>
          <table className="data">
            <thead>
              <tr>
                {Object.keys(rows[0]).map((k) => (
                  <th key={k} scope="col" className={typeof rows[0][k] === "number" ? "num" : undefined}>
                    {displayText(k)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  {Object.keys(rows[0]).map((k) => (
                    <td key={k} className={typeof r[k] === "number" ? "num" : undefined}>
                      {typeof r[k] === "number" ? formatNumber(r[k]) : <Text value={r[k] ?? "—"} max={200} />}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
      {!compact && (listOf("basis").length || listOf("methods").length || listOf("notes").length) ? (
        <p className="muted small">
          {listOf("basis").length ? `basis: ${displayText(listOf("basis").join(", "))}. ` : ""}
          {listOf("methods").length ? `methods: ${displayText(listOf("methods").join(", "))}. ` : ""}
          {displayText(listOf("notes").join(" "))}
        </p>
      ) : null}
      {!compact ? (
        <details>
          <summary>raw tool output</summary>
          <JsonBlock value={output} />
        </details>
      ) : null}
    </div>
  );
}
