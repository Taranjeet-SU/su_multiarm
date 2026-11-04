// Overview dashboard over the log tools (addendum A2 §4.4.3): every panel is one read-only tool call
// whose rows are drawn as returned, with the tool's own rendering sentence. A panel whose tool the
// server does not advertise is not drawn; a failing call shows its error in that panel only. The
// anchor day defaults to the generation's last composed day (from /v2/info) and is always shown.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { z } from "zod";
import { CoverageGauges, Heatmap, IntervalTimeline, RankedBars, TrendLine, type Gauge } from "../components/Charts";
import { ErrorBanner, ExactnessBadge, Text } from "../components/common";
import { useApp } from "../context";
import { barsOf, pointsOf, rowsOf, type Output } from "../lib/logRows";
import { displayText, formatNumber } from "../lib/text";

const ToolCall = z.object({ tool: z.string(), result: z.object({ output: z.record(z.string(), z.unknown()) }).passthrough() }).passthrough();
const Info = z.record(z.string(), z.unknown());
const Values = z.union([z.array(z.unknown()), z.record(z.string(), z.unknown())]);

type Load = { output: Output | null; error: unknown; loading: boolean };

function useTool(tool: string, args: Record<string, unknown> | null): Load {
  const { api, features } = useApp();
  const [state, setState] = useState<Load>({ output: null, error: null, loading: false });
  const key = JSON.stringify(args);
  useEffect(() => {
    if (!args || !features.tools.has(tool)) return;
    const abort = new AbortController();
    setState({ output: null, error: null, loading: true });
    api.post(`/v2/tools/${tool}`, args, ToolCall, abort.signal).then(
      (call) => setState({ output: call.result.output, error: null, loading: false }),
      (error) => !abort.signal.aborted && setState({ output: null, error, loading: false }),
    );
    return () => abort.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, tool, key, features.tools]);
  return state;
}

function Panel({ title, load, question, asOf, children, wide = false }: { title: string; load: Load; question?: string; asOf: string; children?: (o: Output) => ReactNode; wide?: boolean }) {
  const { navigate } = useApp();
  return (
    <section className={wide ? "card panel wide" : "card panel"} data-testid={`panel-${title}`}>
      <div className="card-head">
        <h3>{title}</h3>
        {load.output && typeof load.output.exactness === "string" ? <ExactnessBadge exactness={load.output.exactness} /> : null}
      </div>
      <ErrorBanner error={load.error} />
      {load.loading ? <p className="muted">loading…</p> : null}
      {load.output ? (
        <>
          {children ? children(load.output) : null}
          <p className="muted small">
            <Text value={load.output.rendering} max={600} />
          </p>
        </>
      ) : null}
      {question ? (
        <button type="button" className="ghost" onClick={() => navigate("ask", { q: question, as_of: asOf })}>
          Ask about this
        </button>
      ) : null}
    </section>
  );
}

function num(row: Record<string, unknown> | undefined, key: string): number | null {
  const v = row?.[key];
  return typeof v === "number" ? v : null;
}

export function DashboardView() {
  const { api, features } = useApp();
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const [asOf, setAsOf] = useState("");
  const [services, setServices] = useState<string[]>([]);
  useEffect(() => {
    const abort = new AbortController();
    api.optional("/v2/info", Info, abort.signal).then((i) => {
      setInfo(i);
      if (i && typeof i.as_of === "string") setAsOf((current) => current || String(i.as_of));
    }, () => undefined);
    api.get("/v2/values/service?limit=8", Values, abort.signal).then(
      (v) => {
        const list = Array.isArray(v) ? v : Array.isArray((v as Record<string, unknown>).values) ? ((v as Record<string, unknown>).values as unknown[]) : [];
        setServices(list.map((x) => (typeof x === "object" && x && "value" in x ? String((x as { value: unknown }).value) : String(x))).slice(0, 8));
      },
      () => undefined,
    );
    return () => abort.abort();
  }, [api]);

  const ready = asOf !== "" || !features.routes.has("GET /v2/info");
  const base = useMemo(() => (asOf ? { as_of: asOf } : {}), [asOf]);
  const compare = useTool("log_compare", ready ? { ...base, periods: ["yesterday", "today"] } : null);
  const fresh = useTool("log_new", ready ? { ...base, window: "today" } : null);
  const topServices = useTool("log_top", ready ? { ...base, by: "service", window: "yesterday" } : null);
  const topTenants = useTool("log_top", ready ? { ...base, by: "tenant", window: "last 7 days" } : null);
  const trend = useTool("log_trend", ready ? { ...base, window: "last 10 days" } : null);
  const coverage = useTool("log_coverage", ready ? { ...base, window: "yesterday" } : null);
  const incidents = useTool("incidents", ready ? { limit: 20 } : null);

  const compareRows = compare.output ? rowsOf(compare.output) : [];
  const yesterday = num(compareRows[0], "lines");
  const today = num(compareRows[1], "lines");
  const freshCount = fresh.output ? rowsOf(fresh.output).length : null;
  const has = (t: string) => features.tools.has(t);
  return (
    <div className="view dashboard">
      <div className="section-title">
        <h2>Overview</h2>
        <p>
          as of <strong>{displayText(asOf || "today (server clock)")}</strong>
          {typeof info?.as_of === "string" && asOf === info.as_of ? " (the last composed day)" : ""}
        </p>
        <label htmlFor="dash-as-of" className="small">
          change
        </label>
        <input id="dash-as-of" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
      </div>
      <div className="kpi">
        {has("log_compare") ? (
          <div className="card" data-testid="kpi-today">
            <div className="caption">Lines today</div>
            <div className="value">{today === null ? "—" : formatNumber(today)}</div>
            <div className="caption">
              {yesterday === null || today === null ? <ErrorOrMuted load={compare} /> : <Delta now={today} before={yesterday} />} vs yesterday {yesterday === null ? "" : formatNumber(yesterday)}
            </div>
          </div>
        ) : null}
        {has("log_new") ? (
          <div className="card" data-testid="kpi-new">
            <div className="caption">New signatures today</div>
            <div className="value">{freshCount === null ? "—" : formatNumber(freshCount)}</div>
            <div className="caption">first seen in the composed history</div>
          </div>
        ) : null}
        {has("incidents") ? (
          <div className="card" data-testid="kpi-incidents">
            <div className="caption">Co-firing incidents</div>
            <div className="value">{incidents.output && typeof incidents.output.total === "number" ? formatNumber(incidents.output.total) : "—"}</div>
            <div className="caption">statistical co-occurrence, not causality</div>
          </div>
        ) : null}
      </div>
      <div className="tile-grid wide">
        {has("log_top") ? (
          <Panel title="Most affected services (yesterday)" load={topServices} question="Which services had the most errors yesterday?" asOf={asOf}>
            {(o) => <RankedBars title="Lines by service" bars={barsOf(o)} unit="lines" />}
          </Panel>
        ) : null}
        {has("log_top") ? (
          <Panel title="Most affected tenants (last 7 days)" load={topTenants} question="Which tenants had the most errors in the last 7 days?" asOf={asOf}>
            {(o) => <RankedBars title="Lines by tenant" bars={barsOf(o)} unit="lines" />}
          </Panel>
        ) : null}
        {has("log_trend") ? (
          <Panel title="Trend (last 10 days)" load={trend} question="How have errors trended over the last 10 days?" asOf={asOf}>
            {(o) => <TrendLine title="Lines per day" points={pointsOf(o)} />}
          </Panel>
        ) : null}
        {has("log_coverage") ? (
          <Panel title="Parse coverage (yesterday)" load={coverage} question="How much of yesterday did we parse?" asOf={asOf}>
            {(o) => <CoverageGauges title="Coverage by service" gauges={gaugesOf(o)} />}
          </Panel>
        ) : null}
        {has("log_new") ? (
          <Panel title="New signatures (today)" load={fresh} question="What new errors appeared today?" asOf={asOf}>
            {(o) => (rowsOf(o).length ? <RankedBars title="New signatures by lines" bars={barsOf(o)} unit="lines" /> : <p className="muted">No signature was seen for the first time on this day.</p>)}
          </Panel>
        ) : null}
        {has("log_top") && services.length ? <TenantServiceHeatmap services={services} base={base} ready={ready} /> : null}
        {has("incidents") ? (
          <Panel title="Incidents" load={incidents} wide asOf={asOf}>
            {(o) => <Incidents output={o} />}
          </Panel>
        ) : null}
      </div>
    </div>
  );
}

function ErrorOrMuted({ load }: { load: Load }) {
  return <span className="muted">{load.loading ? "loading" : load.error ? "unavailable" : "no data"}</span>;
}

function Delta({ now, before }: { now: number; before: number }) {
  const diff = now - before;
  if (!diff) return <span>no change</span>;
  return <span className={diff > 0 ? "delta-up" : "delta-down"}>{diff > 0 ? `▲ +${formatNumber(diff)}` : `▼ ${formatNumber(diff)}`}</span>;
}

export function gaugesOf(output: Output): Gauge[] {
  return rowsOf(output)
    .filter((r) => typeof r.service === "string")
    .map((r) => ({
      label: String(r.service),
      regex: typeof r.deterministic_coverage === "number" ? r.deterministic_coverage : null,
      withModel: typeof r.extracted_coverage === "number" ? r.extracted_coverage : null,
    }));
}

/** Tenant × service: one ``log_top by tenant`` call per service (the tool's own rows, no re-aggregation). */
function TenantServiceHeatmap({ services, base, ready }: { services: string[]; base: Record<string, unknown>; ready: boolean }) {
  const { api } = useApp();
  const [cells, setCells] = useState<Map<string, Map<string, number>> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const key = JSON.stringify([services, base]);
  useEffect(() => {
    if (!ready) return;
    const abort = new AbortController();
    setError(null);
    Promise.all(services.map((s) => api.post("/v2/tools/log_top", { ...base, by: "tenant", service: [s], window: "last 7 days", top_n: 20 }, ToolCall, abort.signal)))
      .then((calls) => {
        const grid = new Map<string, Map<string, number>>();
        calls.forEach((call, i) => {
          for (const row of rowsOf(call.result.output)) {
            if (typeof row.tenant !== "string" || typeof row.lines !== "number") continue;
            if (!grid.has(row.tenant)) grid.set(row.tenant, new Map());
            grid.get(row.tenant)!.set(services[i], row.lines);
          }
        });
        setCells(grid);
      })
      .catch((e) => !abort.signal.aborted && setError(e));
    return () => abort.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, key, ready]);
  const tenants = cells ? [...cells.keys()].sort() : [];
  return (
    <section className="card panel wide" data-testid="panel-heatmap">
      <div className="card-head">
        <h3>Tenants × services (last 7 days)</h3>
      </div>
      <ErrorBanner error={error} />
      {cells && tenants.length ? (
        <Heatmap title="Lines per tenant and service" rows={tenants} cols={services} value={(t, s) => cells.get(t)?.get(s) ?? null} rowLabel="tenant" colLabel="service" />
      ) : cells ? (
        <p className="muted">No tenant rows for these services in the window.</p>
      ) : !error ? (
        <p className="muted">loading…</p>
      ) : null}
      <p className="muted small">One top-tenants call per service; a blank cell means the tenant was not in that service's top 20 (not zero).</p>
    </section>
  );
}

function Incidents({ output }: { output: Output }) {
  const rows = Array.isArray(output.incidents) ? (output.incidents as Record<string, unknown>[]) : [];
  const intervals = rows
    .filter((r) => typeof r.first_hour === "string" && typeof r.last_hour === "string")
    .map((r) => ({
      label: Array.isArray(r.signatures) ? (r.signatures as unknown[]).slice(0, 3).join(", ") : String(r.community_id),
      sublabel: `${r.tenant ? `tenant ${r.tenant} · ` : ""}${Array.isArray(r.services) ? (r.services as unknown[]).join(", ") : ""}`,
      start: Date.parse(String(r.first_hour)),
      end: Date.parse(String(r.last_hour)) + 3600_000,
      peak: typeof r.peak_hour === "string" ? Date.parse(r.peak_hour) + 1800_000 : null,
      value: typeof r.lines === "number" ? r.lines : null,
    }))
    .filter((iv) => Number.isFinite(iv.start) && Number.isFinite(iv.end));
  if (!rows.length) return <p className="muted">No co-firing incident in the composed window.</p>;
  return <IntervalTimeline title="Co-firing incidents (first to last hour, ● peak hour; lines at right)" intervals={intervals} finding={`${rows.length} incident(s); the bar spans the hours where the member signatures fired together.`} />;
}
