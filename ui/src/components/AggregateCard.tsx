import type { Aggregate } from "../api/schema";
import { seriesOf } from "../lib/charts";
import { clip, displayText, formatNumber } from "../lib/text";
import { RankedBars, TrendLine } from "./Charts";
import { ExactnessBadge, JsonBlock, KeyValues, Text } from "./common";

/** One structured aggregate: exactness badge, basis, denominator, truncated-ids flag, value/table, chart. */
export function AggregateCard({ aggregate, timeFields, flash = false }: { aggregate: Aggregate; timeFields: ReadonlySet<string>; flash?: boolean }) {
  const ids = aggregate.document_ids ?? [];
  const series = seriesOf(aggregate, timeFields);
  const scalar = aggregate.value === null || ["string", "number", "boolean"].includes(typeof aggregate.value);
  const title = `${aggregate.operation}${aggregate.field ? ` by ${aggregate.field}` : ""}`;
  return (
    <article className={flash ? "aggregate flash" : "aggregate"} id={aggregate.id ? `ev-${aggregate.id}` : undefined} data-evidence-id={aggregate.id} tabIndex={-1}>
      <header>
        <strong>{clip(title, 200)}</strong> <ExactnessBadge exactness={aggregate.exactness} />
        {aggregate.id ? <code className="muted"> {displayText(aggregate.id)}</code> : null}
      </header>
      {typeof aggregate.rendering === "string" && aggregate.rendering ? (
        // B5: the server's deterministic sentence (basis, time basis, timezone), identical on every surface.
        <p className="rendering" data-testid="aggregate-rendering">
          <Text value={aggregate.rendering} max={1000} />
        </p>
      ) : null}
      {aggregate.exactness && aggregate.exactness !== "exact" ? (
        <p className="banner banner-warn" role="note">
          This result is <strong>{displayText(aggregate.exactness)}</strong>
          {aggregate.exactness === "bounded" ? ": part of the request could not be applied exactly (see unsupported below)." : ": the backend estimated it (see basis)."}
        </p>
      ) : null}
      <KeyValues
        rows={[
          ["denominator", aggregate.denominator],
          ["total matched", aggregate.total],
          ["missing or invalid", aggregate.missing_or_invalid],
          ["zero match", aggregate.zero_match === undefined ? undefined : String(aggregate.zero_match)],
          ["scope", aggregate.scope],
          ["matched document ids", aggregate.document_ids_truncated ? `${ids.length} shown — TRUNCATED (list capped; ${aggregate.total ?? "?"} matched)` : `${ids.length}`],
        ]}
      />
      {aggregate.document_ids_truncated ? (
        <p className="banner banner-warn" data-testid="ids-truncated">
          ⚠ truncated ids: the matched-id list is capped by the backend; counts above are still computed over every matched record.
        </p>
      ) : null}
      <div>
        <span className="label">basis</span>
        {aggregate.basis && aggregate.basis.length ? (
          <ul className="basis">
            {aggregate.basis.map((b, i) => (
              <li key={i}>
                <Text value={b} max={500} />
              </li>
            ))}
          </ul>
        ) : (
          <span className="muted"> not reported</span>
        )}
      </div>
      {aggregate.unsupported && aggregate.unsupported.length ? (
        <div>
          <span className="label">unsupported</span>
          <ul>
            {aggregate.unsupported.map((u, i) => (
              <li key={i}>
                <Text value={`${displayText(u.reason)}: ${displayText(u.constraint_text)}${u.suggestion ? ` (${displayText(u.suggestion)})` : ""}`} max={500} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {aggregate.note ? <p className="muted"><Text value={aggregate.note} max={400} /></p> : null}
      {aggregate.shown ? <p className="muted"><Text value={aggregate.shown} max={200} /></p> : null}
      {scalar && !series ? (
        <p className="scalar">
          value: <strong>{aggregate.value === null || aggregate.value === undefined ? "none" : formatNumber(aggregate.value)}</strong>
        </p>
      ) : null}
      {series && series.kind === "line" ? <TrendLine title={title} points={series.points} unit={series.measure === "count" ? "records" : ""} /> : null}
      {series && series.kind === "bar" ? <RankedBars title={title} bars={series.points.map((p) => ({ ...p, approximate: aggregate.exactness === "approximate" }))} unit={series.measure === "count" ? "records" : ""} /> : null}
      {series && series.skipped.length ? <p className="muted small">Not plotted (non-numeric value): {clip(series.skipped.join(", "), 400)}</p> : null}
      <details>
        <summary>raw aggregate record</summary>
        <JsonBlock value={aggregate} />
      </details>
    </article>
  );
}
