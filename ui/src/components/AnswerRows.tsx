import { useState } from "react";
import type { Aggregate } from "../api/schema";
import { displayText, formatNumber } from "../lib/text";

/** Show the server's actual rows beside its rendering; never infer rows from prose. */
export function AnswerRows({ aggregate }: { aggregate: Aggregate }) {
  const [expanded, setExpanded] = useState(false);
  const rows: Record<string, unknown>[] = aggregate.rows?.length ? aggregate.rows : (aggregate.groups ?? []).map(({ key, ...measures }) => ({ ...key, ...measures }));
  if (!rows.length) return null;
  const issues = rows.every((row) => "error_type" in row && "error_count" in row);
  const fallback = issues ? [...(rows.every((r) => "tenant" in r) ? ["tenant"] : []), ...(rows.every((r) => "service" in r) ? ["service"] : []), "error_type", "error_count"] : [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const columns = aggregate.columns?.length ? aggregate.columns.filter((c) => rows.some((r) => c in r)) : fallback;
  const labels: Record<string, string> = { service: "Service", tenant: "Client", level: "Severity", deployment_id: "Deployment", signature_id: "Error pattern", error_type: "Error type", error_count: "Error count" };
  const visible = expanded ? rows : rows.slice(0, 10);
  return (
    <div className="answer-rows">
      <div className="answer-table-scroll" role="region" aria-label="Supporting result rows" tabIndex={0}>
        <table className="data answer-table">
          <caption>{issues ? "Errors by " + columns.filter((c) => !["error_type", "error_count", "signature_id"].includes(c)).map((c) => (labels[c] ?? c).toLowerCase()).join(" and ") : displayText(aggregate.intent ?? aggregate.operation).replace(/_/g, " ")} · {displayText(aggregate.exactness ?? "exactness not reported")}</caption>
          <thead><tr>{columns.map((column) => <th scope="col" key={column}>{issues ? labels[column] : displayText(column).replace(/_/g, " ")}</th>)}</tr></thead>
          <tbody>{visible.map((row, i) => <tr key={i}>{columns.map((column) => <td key={column}>{row[column] == null ? "—" : typeof row[column] === "number" ? formatNumber(row[column]) : displayText(row[column])}</td>)}</tr>)}</tbody>
        </table>
      </div>
      {rows.length > 10 ? <button type="button" className="ghost" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "Show first 10 rows" : `Show all ${rows.length} returned rows`}</button> : null}
    </div>
  );
}
