// Log analytics: overview tiles and log-intent shortcuts over the L5 log tools (addendum A2 §4.1.5,
// §4.4.3). Everything is feature-detected from ``GET /v2/tools``: a tool the server does not list is
// not offered; the view is hidden when no ``log_*`` tool exists. Forms come from each tool's input
// JSON Schema; results show the server's ``rendering`` sentence and its ``ScopeResult`` as-is.
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { z } from "zod";
import type { Features } from "../api/features";
import { Aggregate } from "../api/schema";
import { AggregateCard } from "../components/AggregateCard";
import { LogResult } from "../components/LogResult";
import { ErrorBanner, JsonBlock, Text } from "../components/common";
import { useApp } from "../context";
import { formFields, initialValues, toArgs, type FormField } from "../lib/schemaForm";
import { displayText } from "../lib/text";
import { timeFieldsOf } from "./AskView";

export const SHORTCUTS: Array<{ tool: string; label: string; presets: Record<string, string>; question: string }> = [
  { tool: "log_top", label: "Top", presets: { by: "service", window: "yesterday" }, question: "which services had the most errors yesterday" },
  { tool: "log_new", label: "New", presets: { window: "yesterday" }, question: "what new errors appeared yesterday" },
  { tool: "log_affected", label: "Affected", presets: { window: "last 7 days" }, question: "which tenants are affected by this signature" },
  { tool: "log_trend", label: "Trend", presets: { window: "last 10 days", granularity: "day" }, question: "how has this signature trended" },
  { tool: "log_compare", label: "Compare", presets: { periods: "yesterday, the day before yesterday" }, question: "errors yesterday vs the day before" },
  { tool: "log_tenants", label: "Tenants", presets: { window: "yesterday" }, question: "which tenants had fatal errors yesterday" },
];

export function hasLogTools(features: Features): boolean {
  return [...features.tools].some((t) => t.startsWith("log_"));
}

const ToolCall = z.object({ tool: z.string(), result: z.object({ output: z.record(z.string(), z.unknown()) }).passthrough() }).passthrough();
type Output = Record<string, unknown>;

/** The tool's ``ScopeResult`` in the aggregate-card shape (same badge, basis and truncation flag). */
export function asAggregate(tool: string, output: Output): Aggregate | null {
  const result = output.result as Record<string, unknown> | undefined;
  if (!result || typeof result !== "object") return null;
  const scope = (output.scope ?? {}) as Record<string, unknown>;
  const groupBy = Array.isArray(scope.group_by) ? (scope.group_by as string[]).join("|") : null;
  const parsed = Aggregate.safeParse({
    operation: tool,
    field: groupBy,
    value: result.value,
    exactness: result.exactness,
    denominator: result.denominator,
    basis: result.basis,
    groups: result.groups,
    total: result.total,
    missing_or_invalid: result.missing_or_invalid,
    zero_match: result.zero_match,
    unsupported: result.unsupported,
    document_ids: output.document_ids ?? result.matched_ids,
    document_ids_truncated: output.truncated ?? result.matched_ids_truncated,
    rendering: output.rendering,
  });
  return parsed.success ? parsed.data : null;
}

function ToolResult({ tool, output, compact = false }: { tool: string; output: Output; compact?: boolean }) {
  const { capabilities } = useApp();
  const timeFields = useMemo(() => timeFieldsOf(capabilities?.fields), [capabilities]);
  if (Array.isArray(output.rows) || typeof output.status === "string") return <LogResult tool={tool} output={output} compact={compact} />;
  const aggregate = asAggregate(tool, output);
  return aggregate ? (
    <AggregateCard aggregate={aggregate} timeFields={timeFields} />
  ) : (
    <div className="hit">
      {typeof output.rendering === "string" ? <Text value={output.rendering} max={1000} /> : null}
      <JsonBlock value={output} />
    </div>
  );
}

function Shortcut({ tool, presets, schema }: { tool: string; presets: Record<string, string>; schema: Record<string, unknown> | undefined }) {
  const { api } = useApp();
  const fields: FormField[] = useMemo(() => (schema ? formFields(schema) : []), [schema]);
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(fields, presets));
  const [output, setOutput] = useState<Output | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setValues(initialValues(fields, presets)), [fields, presets]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const call = await api.post(`/v2/tools/${encodeURIComponent(tool)}`, toArgs(fields, values), ToolCall);
      setOutput(call.result.output);
    } catch (exc) {
      setOutput(null);
      setError(exc);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="tile" data-testid={`shortcut-${tool}`}>
      <h3>{displayText(tool)}</h3>
      <form onSubmit={submit} className="row" aria-label={tool}>
        {fields.map((f) => (
          <label key={f.name} title={f.description}>
            {f.name}
            {f.required ? "*" : ""}{" "}
            {f.kind === "enum" ? (
              <select value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                {!f.required ? <option value="">(default)</option> : null}
                {f.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : f.kind === "boolean" ? (
              <select value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
                <option value="">(default)</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} placeholder={f.kind === "string_list" ? "a, b" : f.kind === "json" ? "JSON" : ""} maxLength={2000} />
            )}
          </label>
        ))}
        <button type="submit" disabled={busy}>
          Run
        </button>
      </form>
      <ErrorBanner error={error} />
      {output ? <ToolResult tool={tool} output={output} /> : null}
    </section>
  );
}

function Tile({ tool, args, question }: { tool: string; args: Record<string, unknown>; question: string }) {
  const { api, navigate } = useApp();
  const [output, setOutput] = useState<Output | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    const abort = new AbortController();
    api.post(`/v2/tools/${tool}`, args, ToolCall, abort.signal).then((c) => setOutput(c.result.output), (e) => !abort.signal.aborted && setError(e));
    return () => abort.abort();
  }, [api, tool, args]);
  return (
    <section className="tile" data-testid={`tile-${tool}`}>
      <h3>{displayText(tool)}</h3>
      <ErrorBanner error={error} />
      {output ? <ToolResult tool={tool} output={output} /> : !error ? <p className="muted">loading…</p> : null}
      <button type="button" onClick={() => navigate("ask", { q: question })}>
        Ask about this
      </button>
    </section>
  );
}

const NO_PRESETS: Record<string, string> = {};
const TILE_ARGS: Record<string, Record<string, unknown>> = { log_top: { by: "service", window: "yesterday" }, log_new: { window: "yesterday" } };

export function LogsView() {
  const { features } = useApp();
  const tiles = ["log_top", "log_new"].filter((t) => features.tools.has(t));
  const shortcuts = SHORTCUTS.filter((s) => features.tools.has(s.tool));
  const others = [...features.tools].filter((t) => t.startsWith("log_") && !SHORTCUTS.some((s) => s.tool === t)).sort();
  return (
    <div className="view logs">
      {tiles.length ? (
        <>
          <h2>Overview (yesterday)</h2>
          <div className="tile-grid">
            {tiles.map((t) => (
              <Tile key={t} tool={t} args={TILE_ARGS[t]} question={SHORTCUTS.find((s) => s.tool === t)?.question ?? t} />
            ))}
          </div>
        </>
      ) : null}
      <h2>Shortcuts</h2>
      <div className="tile-grid">
        {[...shortcuts, ...others.map((t) => ({ tool: t, presets: NO_PRESETS }))].map((s) => (
          <Shortcut key={s.tool} tool={s.tool} presets={s.presets} schema={features.toolSchemas.get(s.tool)} />
        ))}
      </div>
    </div>
  );
}
