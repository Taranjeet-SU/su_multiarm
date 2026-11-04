import { useMemo, useState, type FormEvent } from "react";
import { SearchResult } from "../api/schema";
import { AggregateCard } from "../components/AggregateCard";
import { ErrorBanner } from "../components/common";
import { TracePanel } from "../components/TracePanel";
import { useApp } from "../context";
import { timeFieldsOf } from "./AskView";

/** Grouped and time aggregates through the typed ``SearchRequest.aggregate`` (``group_count``): the
 * server's structured arm computes them; the UI only plots the returned groups. */
export function ChartsView() {
  const { api, capabilities, lastAnswer, setLastSearch } = useApp();
  const groupable = useMemo(() => (capabilities?.fields ?? []).filter((f) => f.groupable), [capabilities]);
  const timeFields = useMemo(() => timeFieldsOf(capabilities?.fields), [capabilities]);
  const [field, setField] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<SearchResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const chosen = field || groupable[0]?.name || "";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!chosen) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.post("/v2/search", { query: query.trim() || `count by ${chosen}`, aggregate: { operation: "group_count", field: chosen } }, SearchResult);
      setResult(r);
      setLastSearch(r);
    } catch (exc) {
      setResult(null);
      setError(exc);
    } finally {
      setBusy(false);
    }
  };

  const fromAnswer = lastAnswer?.retrieval.aggregates ?? [];
  return (
    <div className="view charts">
      <form onSubmit={submit} className="row" aria-label="grouped aggregate">
        <label htmlFor="group-field">Group by</label>
        <select id="group-field" value={chosen} onChange={(e) => setField(e.target.value)} disabled={!groupable.length}>
          {groupable.map((f) => (
            <option key={f.name} value={f.name}>
              {f.name} ({f.type})
            </option>
          ))}
        </select>
        <label htmlFor="chart-query">Question (optional)</label>
        <input id="chart-query" value={query} onChange={(e) => setQuery(e.target.value)} maxLength={2000} placeholder="scopes the request; the grouping is typed" />
        <button type="submit" disabled={busy || !chosen}>
          Chart
        </button>
      </form>
      {!groupable.length ? <p className="muted">No groupable field is declared in this deployment's metadata schema.</p> : null}
      <ErrorBanner error={error} />
      {result ? (
        result.aggregates.length ? (
          <>
            {result.aggregates.map((a, i) => (
              <AggregateCard key={a.id ?? i} aggregate={a} timeFields={timeFields} />
            ))}
            <details>
              <summary>trace of this request</summary>
              <TracePanel retrievalTrace={result.trace} />
            </details>
          </>
        ) : (
          <p className="muted">The server returned no aggregate for this request.</p>
        )
      ) : null}
      {fromAnswer.length ? (
        <section>
          <h2>Aggregates of the last answer</h2>
          {fromAnswer.map((a, i) => (
            <AggregateCard key={a.id ?? i} aggregate={a} timeFields={timeFields} />
          ))}
        </section>
      ) : null}
    </div>
  );
}
