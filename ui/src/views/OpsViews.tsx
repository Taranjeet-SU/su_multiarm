// Runs and spend, generation info, and communities: read-only views over the inspect routes
// (addendum A2 §4.3). Each payload says whether its data exists; absence is shown, never zero-filled.
import { useEffect, useState } from "react";
import { z } from "zod";
import { ErrorBanner, JsonBlock, KeyValues, Text } from "../components/common";
import { useApp } from "../context";
import { displayText, formatNumber, formatUsd } from "../lib/text";

const Json = z.record(z.string(), z.unknown());
const Runs = z.object({ available: z.boolean(), reason: z.string().optional(), runs: z.array(Json) }).passthrough();
const Spend = z
  .object({
    available: z.boolean(),
    reason: z.string().optional(),
    caps: Json.nullable().optional(),
    rows: z.array(z.object({ scope: z.string(), role: z.string(), day: z.string(), committed_usd: z.number(), reserved_usd: z.number(), calls: z.number(), cap_usd: z.number().nullable().optional(), max_calls: z.number().nullable().optional() }).passthrough()),
  })
  .passthrough();
const Info = Json;
const Communities = z.object({ available: z.boolean(), reason: z.string().optional(), total: z.number().optional(), truncated: z.boolean().optional(), communities: z.array(Json) }).passthrough();

function useLoad<S extends z.ZodTypeAny>(path: string, schema: S) {
  const { api } = useApp();
  const [data, setData] = useState<z.output<S> | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const abort = new AbortController();
    setError(null);
    api.get(path, schema, abort.signal).then(setData, (e) => !abort.signal.aborted && setError(e));
    return () => abort.abort();
  }, [api, path, schema]);
  return { data, error };
}

/** Served vs archived days and the multi-library note (rev 4), from ``/v2/info``; nothing inferred. */
export function GenerationDays({ info }: { info: Record<string, unknown> }) {
  const served = Array.isArray(info.served_days) ? (info.served_days as unknown[]) : [];
  const archived = Array.isArray(info.archived_days) ? (info.archived_days as unknown[]) : null;
  const libraries = Array.isArray(info.library_identities) ? (info.library_identities as unknown[]) : [];
  return (
    <div data-testid="generation-days">
      <p>
        <span className="label">served days</span>
        {served.length ? `${served.length}: ${displayText(served.join(", "))}` : "none"}
        <span className="label"> · archived days</span>
        {archived === null ? <span className="muted">not recorded by this generation</span> : archived.length ? `${archived.length}: ${displayText(archived.join(", "))}` : "0"}
      </p>
      {libraries.length > 1 ? (
        <p className="banner banner-warn" data-testid="multi-library">
          The served days span {libraries.length} pattern-library identities: counts per signature may differ across days because the library changed, not only the logs.
        </p>
      ) : null}
    </div>
  );
}

export function RunsView() {
  const info = useLoad("/v2/info", Info);
  const runs = useLoad("/v2/runs?limit=31", Runs);
  const spend = useLoad("/v2/spend?days=31", Spend);
  return (
    <div className="view runs">
      <h2>Generation</h2>
      <ErrorBanner error={info.error} />
      {info.data ? (
        <KeyValues
          rows={[
            ["generation", info.data.generation],
            ["dataset", info.data.dataset],
            ["window", Array.isArray(info.data.window) ? info.data.window.join(" → ") : "not a logs generation"],
            ["as of", info.data.as_of],
            ["date basis", info.data.date_basis],
            ["timezone", info.data.timezone],
            ["built at", info.data.built_at],
            ["library identity", (info.data.pattern_provenance as Record<string, unknown> | null)?.library_identity],
          ]}
        />
      ) : null}
      {info.data ? <GenerationDays info={info.data} /> : null}
      <h2>Daily runs</h2>
      <ErrorBanner error={runs.error} />
      {runs.data && !runs.data.available ? <p className="muted">{displayText(runs.data.reason)}</p> : null}
      {runs.data?.runs.map((run, i) => (
        <details key={i} className="hit" open={i === 0}>
          <summary>
            <strong>{displayText(run.day ?? run.name)}</strong> {run.ok === true ? <span className="badge contract-passed">✓ ok</span> : run.ok === false ? <span className="badge contract-abstained">✗ failed</span> : <span className="badge badge-unknown">unknown</span>}
            {run.error ? <Text value={` ${displayText(run.error)}`} max={300} /> : null}
          </summary>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">step</th>
                <th scope="col">status</th>
                <th scope="col">numbers</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries((run.steps ?? {}) as Record<string, Record<string, unknown>>).map(([name, step]) => (
                <tr key={name}>
                  <td>{displayText(name)}</td>
                  <td>{displayText(step.status ?? "—")}</td>
                  <td>
                    <Text
                      value={Object.entries(step)
                        .filter(([k]) => k !== "status")
                        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
                        .join(" · ")}
                      max={800}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ))}
      <h2>Model spend (pattern learning, per day)</h2>
      <ErrorBanner error={spend.error} />
      {spend.data ? (
        spend.data.available ? (
          <table className="data" data-testid="spend-table">
            <thead>
              <tr>
                <th scope="col">day</th>
                <th scope="col">scope</th>
                <th scope="col">role</th>
                <th scope="col" className="num">calls</th>
                <th scope="col" className="num">committed</th>
                <th scope="col" className="num">reserved</th>
                <th scope="col" className="num">cap</th>
              </tr>
            </thead>
            <tbody>
              {spend.data.rows.map((r, i) => (
                <tr key={i}>
                  <td>{displayText(r.day)}</td>
                  <td>{displayText(r.scope)}</td>
                  <td>{displayText(r.role)}</td>
                  <td className="num">
                    {formatNumber(r.calls)}
                    {r.max_calls ? ` / ${formatNumber(r.max_calls)}` : ""}
                  </td>
                  <td className="num">{formatUsd(r.committed_usd)}</td>
                  <td className="num">{formatUsd(r.reserved_usd)}</td>
                  <td className="num">{r.cap_usd === null || r.cap_usd === undefined ? "no USD cap" : formatUsd(r.cap_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">{displayText(spend.data.reason)}</p>
        )
      ) : null}
    </div>
  );
}

export function CommunitiesView() {
  const { api } = useApp();
  const list = useLoad("/v2/communities?limit=200", Communities);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const open = (id: string) => {
    setError(null);
    api.get(`/v2/communities/${encodeURIComponent(id)}`, Json).then(setDetail, setError);
  };
  return (
    <div className="view communities">
      <ErrorBanner error={list.error ?? error} />
      {list.data && !list.data.available ? <p className="muted">{displayText(list.data.reason)}</p> : null}
      {list.data?.available ? (
        <>
          <p className="muted small">
            {list.data.total} communities{list.data.truncated ? " (list truncated)" : ""}. Members and statistics come from the generation; reports are shown only when generated.
          </p>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">community</th>
                <th scope="col">level</th>
                <th scope="col" className="num">documents</th>
                <th scope="col" className="num">entities</th>
                <th scope="col">report</th>
              </tr>
            </thead>
            <tbody>
              {list.data.communities.map((c) => (
                <tr key={String(c.id)}>
                  <td>
                    <button type="button" className="linklike" onClick={() => open(String(c.id))}>
                      {displayText(c.label || c.id)}
                    </button>
                  </td>
                  <td>{displayText(c.level)}</td>
                  <td className="num">{formatNumber(c.documents)}</td>
                  <td className="num">{formatNumber(c.entities)}</td>
                  <td>{displayText(c.report_status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {detail ? (
        <section className="hit">
          <h3>{displayText(detail.label || detail.id)}</h3>
          <KeyValues rows={[["id", detail.id], ["members truncated", String(detail.members_truncated)]]} />
          <JsonBlock value={detail} />
        </section>
      ) : null}
    </div>
  );
}
