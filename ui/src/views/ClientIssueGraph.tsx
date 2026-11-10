import { useEffect, useMemo, useState } from "react";
import { z } from "zod";
import { ErrorBanner, ExactnessBadge, Text } from "../components/common";
import { GraphCanvas } from "../components/GraphCanvas";
import { useApp } from "../context";
import { IssueOutput, issueGraph, rowsForSelection } from "../lib/clientIssueGraph";
import { todayDate } from "../lib/dates";
import { displayText, formatNumber } from "../lib/text";

const Info = z.object({ as_of: z.string().optional() }).passthrough();
const Values = z.object({ values: z.array(z.union([z.string(), z.object({ value: z.string() }).passthrough()])) }).passthrough();
const Tool = z.object({ result: z.object({ output: IssueOutput }).passthrough() }).passthrough();

export function ClientIssueGraph() {
  const { api, navigate } = useApp();
  const [asOf, setAsOf] = useState("");
  const [latest, setLatest] = useState<string | null>(null);
  const [clients, setClients] = useState<string[]>([]);
  const [client, setClient] = useState("");
  const [clientDraft, setClientDraft] = useState("");
  const [window, setWindow] = useState("last 3 days");
  const [limit, setLimit] = useState(30);
  const [output, setOutput] = useState<z.infer<typeof IssueOutput> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [catalogError, setCatalogError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    setCatalogError(null);
    api.get("/v2/info", Info, abort.signal).then((info) => {
      if (abort.signal.aborted) return;
      setLatest(info.as_of ?? null);
      setAsOf((day) => day || info.as_of || todayDate());
    }, (exc) => { if (!abort.signal.aborted) { setCatalogError(exc); setAsOf((day) => day || todayDate()); } });
    api.get("/v2/values/tenant?limit=50", Values, abort.signal).then((values) => {
      if (!abort.signal.aborted) setClients(values.values.map((value) => typeof value === "string" ? value : value.value));
    }, (exc) => { if (!abort.signal.aborted) setCatalogError(exc); });
    return () => abort.abort();
  }, [api, retry]);
  useEffect(() => {
    if (!asOf) return;
    const abort = new AbortController();
    setBusy(true);
    setError(null);
    setOutput(null);
    setSelected(null);
    api.post("/v2/tools/log_top", {
      as_of: asOf, window, ...(client ? { tenant: client } : {}),
      group_by: ["tenant", "service", "signature_id", "level"],
      columns: ["tenant", "service", "error_type", "level", "error_count"],
      level: ["error", "fatal"], top_n: limit,
    }, Tool, abort.signal).then((call) => {
      if (!abort.signal.aborted) setOutput(call.result.output);
    }, (exc) => { if (!abort.signal.aborted) setError(exc); }).finally(() => { if (!abort.signal.aborted) setBusy(false); });
    return () => abort.abort();
  }, [api, asOf, client, window, limit, retry]);
  const graph = useMemo(() => issueGraph(output?.rows ?? [], output?.truncated ?? false), [output]);
  const rows = useMemo(() => rowsForSelection(output?.rows ?? [], selected, graph), [output, selected, graph]);
  const common = useMemo(() => {
    const signatures = new Map<string, Set<string>>();
    for (const row of output?.rows ?? []) signatures.set(row.signature_id, (signatures.get(row.signature_id) ?? new Set()).add(row.tenant));
    return [...signatures.values()].filter((set) => set.size > 1).length;
  }, [output]);
  const selectedLabel = graph.nodes.find((node) => node.id === selected)?.label ?? (selected ? "Selected connection" : "All displayed connections");
  return (
    <div className="view graph client-issue-graph">
      <div className="section-title"><h2>Clients, services & issues</h2><p>Follow a client to its services, shared error patterns and signal classes.</p></div>
      <form className="row graph-controls" onSubmit={(event) => { event.preventDefault(); setClient(clientDraft.trim()); }}>
        <label htmlFor="graph-client">Client</label>
        <input id="graph-client" list="graph-clients" value={clientDraft} placeholder="All clients" onChange={(event) => setClientDraft(event.target.value)} />
        <datalist id="graph-clients">{clients.map((name) => <option key={name} value={name} />)}</datalist>
        <button type="submit">Apply client</button>
        <label htmlFor="graph-as-of">As of</label><input id="graph-as-of" type="date" value={asOf} onChange={(event) => setAsOf(event.target.value)} />
        <label htmlFor="graph-window">Window</label><select id="graph-window" value={window} onChange={(event) => setWindow(event.target.value)}>
          <option>today</option><option>last 3 days</option><option>last 7 days</option>
        </select>
        <label htmlFor="graph-limit">Top combinations</label><select id="graph-limit" value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
          <option value={30}>30</option><option value={50}>50</option><option value={100}>100</option>
        </select>
      </form>
      {clients.length >= 50 ? <p className="muted small">Suggestions show the first 50 clients. You can enter any indexed client name, or leave it blank for all clients.</p> : null}
      <p className="muted small">Latest indexed day: <strong>{displayText(latest ?? output?.latest_available_day ?? "unknown")}</strong>. The result states whether counts use source severity or detection-rule matches. These signals are not monitored alerts or confirmed outages.</p>
      <ErrorBanner error={catalogError} onRetry={() => setRetry((value) => value + 1)} />
      <ErrorBanner error={error} onRetry={() => setRetry((value) => value + 1)} />
      {busy ? <p aria-live="polite">Loading client and service issues…</p> : null}
      {output?.status === "refused" ? <p role="alert" className="banner banner-warn">{displayText(output.refusal || output.rendering)}</p> : null}
      {output?.source_unavailable.length ? <p className="banner banner-warn">Missing data for {output.source_unavailable.map(displayText).join(", ")}. These days are not counted as zero.</p> : null}
      {output && output.status !== "refused" ? <>
        <div className="kpi graph-kpis">
          <div className="card"><div className="caption">Clients shown</div><div className="value">{new Set(output.rows.map((row) => row.tenant)).size}</div></div>
          <div className="card"><div className="caption">Services shown</div><div className="value">{new Set(output.rows.map((row) => row.service)).size}</div></div>
          <div className="card"><div className="caption">Patterns shared by clients</div><div className="value">{common}</div></div>
          <div className="card"><div className="caption">Errors in displayed rows</div><div className="value">{formatNumber(output.rows.reduce((sum, row) => sum + row.error_count, 0))}</div></div>
        </div>
        <p className="muted small"><ExactnessBadge exactness={output.exactness} /> Showing {output.rows.length} combinations, limited to the top {limit}. {output.truncated ? "More combinations exist outside this view. " : ""}Connections and counts use these rows only. A repeated service is scoped to its client; a shared pattern does not establish a shared cause.</p>
        {graph.nodes.length ? <>
          <div className="graph-lanes" aria-hidden="true"><span>Clients</span><span>Services per client</span><span>Common issues</span><span>Error signals</span></div>
          <GraphCanvas data={graph} onSelectNode={setSelected} onSelectEdge={setSelected} />
          <p className="muted small">Select a node or connection to inspect its counts and supporting patterns. Scroll to zoom; drag to pan.</p>
        </> : <p className="muted">No classified errors were returned for this scope. This does not establish that the services were healthy.</p>}
        <section className="card graph-evidence">
          <div className="card-head"><h3>{displayText(selectedLabel)}</h3>{selected ? <button onClick={() => setSelected(null)}>Show all connections</button> : null}</div>
          <div className="answer-table-scroll"><table className="data answer-table" aria-label="Client and service issue evidence"><caption>Client and service issue evidence</caption><thead><tr><th scope="col">Client</th><th scope="col">Service</th><th scope="col">Issue</th><th scope="col">Severity</th><th scope="col">Error count</th><th scope="col">Supporting pattern</th></tr></thead>
            <tbody>{rows.map((row, index) => <tr key={index}>
              <td>{displayText(row.tenant || "Unspecified")}</td><td>{displayText(row.service || "Unspecified")}</td><td>{displayText(row.error_type)}</td><td>{row.level.toUpperCase()}</td><td>{formatNumber(row.error_count)}</td>
              <td><details><summary>Pattern & evidence</summary><p><Text value={row.supporting_template || "No stored template available"} max={1000} /></p><p className="muted small">Stored pattern template, not a raw event quote.</p><code>{displayText(row.signature_id)}</code>{row.evidence_id ? <p><code>{displayText(row.evidence_id)}</code></p> : null}
                <button onClick={() => navigate("ask", { q: `What errors affected client "${row.tenant}" in service "${row.service}" in the ${window}?`, as_of: asOf })}>Ask about this</button>
              </details></td>
            </tr>)}</tbody></table></div>
        </section>
        <details><summary>Counting scope and limitations</summary><p><Text value={output.rendering} /></p>{output.bounded_by.map((reason) => <p key={reason}>{displayText(reason)}</p>)}</details>
      </> : null}
    </div>
  );
}
