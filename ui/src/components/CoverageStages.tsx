import { useState } from "react";
import { rowsOf, type Output } from "../lib/logRows";
import { displayText, formatNumber } from "../lib/text";

const STAGES = [
  { key: "completeness", label: "Completeness", numerator: "documents_collected", denominator: "documents_matching", unit: "source documents", explanation: "Collected / matching source documents in indexed scope" },
  { key: "scanned", label: "Scanned", numerator: "records_scanned", denominator: "records_collected", unit: "assembled records", explanation: "Parser-processed / collected assembled records" },
  { key: "extracted", label: "Extracted", numerator: "records_extracted", denominator: "records_scanned", unit: "assembled records", explanation: "Useful non-noise parsed / scanned records" },
] as const;
type Stage = typeof STAGES[number];
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

function Metric({ row, stage, name }: { row: Record<string, unknown>; stage: Stage; name: string }) {
  const value = row[stage.key];
  const available = typeof value === "number" && value >= 0 && value <= 1;
  const status = row[`${stage.key}_status`];
  return <div className="coverage-metric">
    <strong>{available ? percent(value) : status === "scope_filtered" ? "Filtered scope" : status === "inconsistent" ? "Inconsistent" : status === "empty" ? "No records" : "Unavailable"}</strong>
    {available ? <progress value={value} max={1} aria-label={`${name}: ${stage.label}`} /> : null}
    <span className="small muted">{available ? `${formatNumber(Number(row[stage.numerator]))} / ${formatNumber(Number(row[stage.denominator]))} ${stage.unit}` : status === "scope_filtered" ? "Incident matches and bounded context; whole-source completeness is not measured" : status === "inconsistent" ? "Recorded counts do not agree" : status === "empty" ? "No denominator in this scope" : "Comparable counters not recorded"}</span>
  </div>;
}

export function CoverageStages({ output }: { output: Output }) {
  const [expanded, setExpanded] = useState(false);
  const rows = rowsOf(output);
  const shown = expanded ? rows : rows.slice(0, 10);
  const incidentRows = rows.filter(isIncident);
  const total: Record<string, unknown> = {};
  for (const stage of STAGES) {
    const known = rows.length > 0 && rows.every((row) => row[`${stage.key}_status`] === "available" || row[`${stage.key}_status`] === "empty");
    const numerator = rows.reduce((sum, row) => sum + (typeof row[stage.numerator] === "number" ? row[stage.numerator] as number : 0), 0);
    const denominator = rows.reduce((sum, row) => sum + (typeof row[stage.denominator] === "number" ? row[stage.denominator] as number : 0), 0);
    total[stage.numerator] = numerator;
    total[stage.denominator] = denominator;
    total[stage.key] = known && denominator > 0 ? numerator / denominator : null;
    total[`${stage.key}_status`] = stage.key === "completeness" && incidentRows.length ? "scope_filtered" : !known ? "unavailable" : denominator === 0 ? "empty" : "available";
  }
  const missing = Array.isArray(output.source_unavailable) ? output.source_unavailable.filter((day): day is string => typeof day === "string") : [];
  return <div className="coverage-stages">
    <p className="muted small"><strong>Indexed scope only.</strong> Indices not yet imported or discovered are outside these counts; this is not overall ELK completeness. Collection uses source documents. Scanning and extraction use assembled record heads after multiline assembly; malformed documents and orphan fragments are outside that record denominator. Noise never increases extraction.</p>
    {missing.length ? <p className="banner banner-warn">Missing days: {missing.map(displayText).join(", ")}. Metrics describe available indexed days only.</p> : null}
    {incidentRows.length ? <section className="card" aria-label="Incident collection scope"><h4>Incident collection</h4><p className="small muted">The keyword policy selects candidate issues. Recovery messages and nearby logs provide context. A match does not establish an outage, and no matches do not establish service health.</p><IncidentCounts rows={incidentRows} /></section> : null}
    <div className="coverage-stage-cards">{STAGES.map((stage) => <div className="card" key={stage.key}><h4>{stage.label}</h4><Metric row={total} stage={stage} name="Displayed services" /><p className="muted small">{stage.explanation}</p></div>)}</div>
    <p className="muted small">Totals cover the services returned below. {incidentRows.length ? "Incident selection and context use different snapshots; their union is not divided by the full-source population. Selected-match capture is shown separately." : "Within the indexed scope, completeness includes all matching source documents, even when collection selected only an error tier."} Historical counters that were not recorded remain unavailable.</p>
    {rows.length ? <>
      <div className="answer-table-scroll"><table className="data answer-table"><caption>Collection, scanning and extraction by service</caption><thead><tr><th scope="col">Service</th>{STAGES.map((stage) => <th scope="col" key={stage.key}>{stage.label}</th>)}{incidentRows.length ? <th scope="col">Incident matches / context</th> : null}</tr></thead><tbody>{shown.map((row, index) => <tr key={index}><td>{displayText(row.service ?? row.tenant ?? row.date)}{row.collection_scope === "includes_selected_records_tier" ? <p className="small muted">Selected-tier collection included</p> : isIncident(row) ? <p className="small muted">Incident policy</p> : null}</td>{STAGES.map((stage) => <td key={stage.key}><Metric row={row} stage={stage} name={String(row.service ?? row.tenant ?? row.date)} /></td>)}{incidentRows.length ? <td>{isIncident(row) ? <IncidentCounts rows={[row]} /> : "—"}</td> : null}</tr>)}</tbody></table></div>
      {rows.length > 10 ? <button className="ghost" onClick={() => setExpanded((value) => !value)}>{expanded ? "Show first 10 services" : `Show all ${rows.length} services`}</button> : null}
    </> : <p className="muted">No coverage records in this scope.</p>}
  </div>;
}

function isIncident(row: Record<string, unknown>): boolean {
  return (typeof row.collection_scope === "string" && row.collection_scope.startsWith("incident_")) || (typeof row.incident_units === "number" && row.incident_units > 0);
}

function IncidentCounts({ rows }: { rows: Record<string, unknown>[] }) {
  function sum(key: string): number | null {
    return rows.every((row) => typeof row[key] === "number" && Number.isFinite(row[key]) && Number(row[key]) >= 0)
      ? rows.reduce((n, row) => n + Number(row[key]), 0) : null;
  }
  const selected = sum("selected_seed_documents"), captured = sum("seeds_collected"), context = sum("context_documents");
  const pending = sum("context_pending_seeds");
  const label = (n: number | null) => n === null ? "unavailable" : formatNumber(n);
  return <div className="small"><p>{label(captured)} / {label(selected)} selected matches captured{selected !== null && captured !== null && selected > 0 && captured <= selected ? ` (${percent(captured / selected)})` : ""}</p><p>{label(context)} additional context documents</p><p className="muted">{pending !== null ? `${label(pending)} matches awaiting context` : "Context completion unavailable"}</p></div>;
}
