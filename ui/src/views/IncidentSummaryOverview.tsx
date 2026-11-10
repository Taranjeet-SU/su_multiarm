import { useEffect, useState } from "react";
import { z } from "zod";
import { ErrorBanner } from "../components/common";
import { useApp } from "../context";
import { todayDate } from "../lib/dates";
import { formatNumber } from "../lib/text";

const Row = z.object({
  id: z.string(), tenant: z.string(), service: z.string(), issue: z.string(),
  first_failure: z.string(), last_failure: z.string(), occurrences: z.number(),
  observed_window_seconds: z.number(), resolution_seconds: z.number().nullable(),
  evidence: z.string(), evidence_count: z.number(), evidence_truncated: z.boolean().default(false),
  sample_completion: z.string().default(""),
}).passthrough();
const Output = z.object({
  rows: z.array(Row), statistics: z.record(z.string(), z.unknown()),
  notes: z.array(z.string()), rendering: z.string(), source_unavailable: z.array(z.string()), truncated: z.boolean(),
});
const Call = z.object({ result: z.object({ output: Output }) }).passthrough();
type Output = z.infer<typeof Output>;

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function IncidentSummaryOverview() {
  const { api } = useApp();
  const [asOf, setAsOf] = useState(todayDate);
  const [window, setWindow] = useState("last 3 days");
  const [client, setClient] = useState("");
  const [service, setService] = useState("");
  const [output, setOutput] = useState<Output | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const timer = globalThis.setInterval(() => setRefresh((r) => r + 1), 60000);
    return () => globalThis.clearInterval(timer);
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    setBusy(true);
    setError(null);
    api.post("/v2/tools/incident_episodes", {
      as_of: asOf, window, top_n: 100,
      ...(client.trim() ? { tenant: client.trim() } : {}),
      ...(service.trim() ? { service: [service.trim()] } : {}),
    }, Call, abort.signal).then((r) => setOutput(r.result.output), (e) => {
      if (!abort.signal.aborted) setError(e);
    }).finally(() => { if (!abort.signal.aborted) setBusy(false); });
    return () => abort.abort();
  }, [api, asOf, window, client, service, refresh]);
  const stats = output?.statistics ?? {};
  const number = (name: string) => typeof stats[name] === "number" ? Number(stats[name]) : null;
  const count = (name: string) => number(name) === null ? "—" : formatNumber(number(name)!);
  return <div className="view dashboard incident-summary-overview">
    <div className="section-title"><h2>Incident overview</h2><p>What happened, when it was observed, and the evidence behind it.</p></div>
    <div className="row">
      <label>As of <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} /></label>
      <label>Window <select value={window} onChange={(e) => setWindow(e.target.value)}><option>today</option><option>yesterday</option><option>last 3 days</option><option>last 7 days</option></select></label>
      <label>Client <input value={client} onChange={(e) => setClient(e.target.value)} placeholder="All clients" /></label>
      <label>Service <input value={service} onChange={(e) => setService(e.target.value)} placeholder="All services" /></label>
      <button type="button" onClick={() => setRefresh((r) => r+1)}>Refresh</button>
    </div>
    <ErrorBanner error={error} />
    {busy ? <p aria-live="polite" className="muted">Refreshing incident summaries…</p> : null}
    <div className="kpi">
      <div className="card"><div className="caption">Keyword-matching logs</div><div className="value">{count("keyword_documents")}</div><div className="caption">Counted at the source</div></div>
      <div className="card"><div className="caption">Evidence logs retained</div><div className="value">{count("evidence_documents")}</div><div className="caption">Repeated logs stay in ELK</div></div>
      <div className="card"><div className="caption">Observed signal episodes</div><div className="value">{count("episodes_total")}</div><div className="caption">Grouped by client, service and issue rule</div></div>
      <div className="card"><div className="caption">Documents not downloaded</div><div className="value">{number("document_download_reduction_percent") === null ? "—" : `${number("document_download_reduction_percent")!.toFixed(2)}%`}</div><div className="caption">Compared with fetching every keyword match</div></div>
    </div>
    {output ? <>
      <p className="muted">{count("rule_documents")} logs matched specific issue rules; {count("unclassified_documents")} keyword candidates remain unclassified. Unclassified candidates are not confirmed incidents.</p>
      <p className="small muted">{count("source_scopes")} client/service/day combinations captured in the selected window.{number("inventory_scopes") !== null ? ` The rolling source inventory contains ${count("inventory_scopes")} combinations.` : ""} Latest capture: {String(stats.latest_capture ?? "unknown")}. Collection is partial while other combinations are pending.</p>
      {number("capture_failures") ? <p className="banner banner-warn">{count("capture_failures")} source captures failed in the latest run. Results retain completed captures; failed sources are queued for retry.</p> : null}
      {number("episodes_without_evidence") ? <p className="small muted">{count("episodes_without_evidence")} episodes are awaiting representative evidence. Their rule-match counts are retained.</p> : null}
      {output.source_unavailable.length ? <p className="banner banner-warn">No captured data for {output.source_unavailable.join(", ")}. Missing data does not mean zero incidents.</p> : null}
      <section className="card panel wide"><h3>Observed issues</h3>
        <p className="muted small">Times are UTC. The observed window is the time between first and last errors; it is not an outage duration. Recovery stays unknown until established.</p>
        <div className="table-wrap"><table aria-label="Observed incident signals"><thead><tr><th scope="col">Client / service</th><th scope="col">Issue</th><th scope="col">First → last observed</th><th scope="col">Matching logs</th><th scope="col">Observed window</th><th scope="col">Recovery duration</th><th scope="col">Evidence</th></tr></thead><tbody>
          {output.rows.map((row) => <tr key={row.id}>
            <td><strong>{row.tenant}</strong><br /><span className="muted">{row.service}</span></td>
            <td>{row.issue}</td><td>{row.first_failure}<br />→ {row.last_failure}</td>
            <td>{formatNumber(row.occurrences)}</td><td>{duration(row.observed_window_seconds)}</td><td>{row.resolution_seconds === null ? "Unknown" : duration(row.resolution_seconds)}</td>
            <td>{row.evidence ? <details><summary>{row.evidence_count} example{row.evidence_count === 1 ? "" : "s"}{row.evidence_truncated ? " (excerpt)" : ""}</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxWidth: "36rem", maxHeight: "20rem", overflow: "auto" }}>{row.evidence}</pre></details> : "Evidence pending"}
              {row.sample_completion ? <details><summary>Sampled-operation completion</summary><p>Completion was observed for a sampled operation. Recovery of the overall episode remains unverified.</p><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxWidth: "36rem" }}>{row.sample_completion}</pre></details> : null}</td>
          </tr>)}
        </tbody></table></div>
        {!output.rows.length ? <p>No observed rule episodes are available for this scope. This does not establish absence of failures.</p> : null}
        {output.truncated ? <p className="muted">Showing a bounded set. Narrow the client, service or time window for more detail.</p> : null}
      </section>
    </> : null}
  </div>;
}
