// Pattern library review (addendum A2 §4.4.3): the owner reviews proposals with their redacted
// examples, matched-line counts and gate results, and copies a shlex-quoted CLI command to decide.
// The UI never writes: there is no decision endpoint; the command runs where the library lives.
import { useEffect, useMemo, useState } from "react";
import { z } from "zod";
import { ErrorBanner, JsonBlock, KeyValues, Pre, Tabs, Text } from "../components/common";
import { useApp } from "../context";
import { decisionCommand, type DecisionOp } from "../lib/cli";
import { submitOnEnter } from "../lib/keys";
import { GenerationDays } from "./OpsViews";
import { displayText, formatNumber } from "../lib/text";

const Json = z.record(z.string(), z.unknown());
const GateSchema = z.object({ passed: z.boolean(), engine: z.string().optional(), matched: z.number().optional(), checks: z.record(z.string(), z.object({ passed: z.boolean(), detail: z.unknown().optional() }).passthrough()) }).passthrough();
const ReviewRow = z
  .object({
    id: z.string(),
    kind: z.string(),
    status: z.string(),
    owner: z.string(),
    regex: z.string(),
    signature: z.string().nullable(),
    category: z.string().nullable(),
    precedence: z.string().nullable().optional(),
    target: z.string().nullable().optional(),
    examples: z.array(z.string()),
    examples_total: z.number(),
    negative_examples: z.number(),
    matched_lines: z.number().nullable(),
    gate: GateSchema.nullable(),
    provenance: Json,
    decisions: z.array(z.string()),
  })
  .passthrough();
export type ReviewRow = z.infer<typeof ReviewRow>;
export const ReviewQueue = z
  .object({
    available: z.boolean(),
    reason: z.string().optional(),
    source: z.string().optional(),
    library: z.string().optional(),
    file_shas: z.record(z.string(), z.string()).optional(),
    lint_issues: z.number().optional(),
    kinds: z.array(z.string()).optional(),
    proposal_status_counts: z.record(z.string(), z.number()).optional(),
    proposals: z.array(ReviewRow),
    learned: z.array(ReviewRow),
    decisions: z.array(Json),
    truncated: z.boolean().optional(),
  })
  .passthrough();
const SnapshotView = z.object({ available: z.boolean(), reason: z.string().optional(), entries: z.array(Json), decisions: z.array(z.unknown()), snapshot_key: z.string().optional() }).passthrough();

const OPS: Array<{ op: DecisionOp; label: string }> = [
  { op: "promote", label: "promote" },
  { op: "deprecate", label: "deprecate" },
  { op: "disable", label: "disable" },
];

type Tab = "review" | "learned" | "generation" | "decisions";

export function PatternsView() {
  const { api } = useApp();
  const [queue, setQueue] = useState<z.infer<typeof ReviewQueue> | null>(null);
  const [snapshot, setSnapshot] = useState<z.infer<typeof SnapshotView> | null>(null);
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [tab, setTab] = useState<Tab>("review");
  const [kind, setKind] = useState<string>("");
  const [status, setStatus] = useState<string>("proposed");
  const [config, setConfig] = useState("config.yaml");
  const [by, setBy] = useState("");
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const abort = new AbortController();
    setError(null);
    api.get("/v2/patterns/review", ReviewQueue, abort.signal).then(setQueue, (e) => !abort.signal.aborted && setError(e));
    api.optional("/v2/patterns/view", SnapshotView, abort.signal).then(setSnapshot, () => undefined);
    api.optional("/v2/info", Json, abort.signal).then(setInfo, () => undefined);
    return () => abort.abort();
  }, [api, reload]);

  useEffect(() => {
    if (queue && !kind) setKind(queue.kinds?.includes("noise") ? "noise" : "");
  }, [queue, kind]);

  const proposals = useMemo(() => (queue?.proposals ?? []).filter((p) => (!kind || p.kind === kind) && (!status || p.status === status)), [queue, kind, status]);
  if (error) return <ErrorBanner error={error} onRetry={() => setReload((n) => n + 1)} />;
  if (!queue) return <p aria-live="polite">loading…</p>;
  if (!queue.available) return <p className="banner banner-muted">{displayText(queue.reason)}</p>;
  const statuses = Object.keys(queue.proposal_status_counts ?? {}).sort();
  return (
    <div className="view patterns">
      <p className="muted small">
        Library <code>{displayText(queue.library)}</code>, read from the working files ({displayText(Object.entries(queue.file_shas ?? {}).map(([f, s]) => `${f} ${s.slice(0, 10)}`).join(", "))}); lint issues: {queue.lint_issues ?? 0}.
        Decisions are made with the CLI; the UI only builds the command. A decision takes effect at the next compose.
      </p>
      {info ? <GenerationDays info={info} /> : null}
      <Tabs
        label="pattern library"
        active={tab}
        onChange={setTab}
        tabs={[
          { id: "review", label: "Review queue", count: queue.proposals.length },
          { id: "learned", label: "Learned", count: queue.learned.length },
          { id: "generation", label: "Served by the generation", count: snapshot?.entries.length },
          { id: "decisions", label: "Decision history", count: queue.decisions.length },
        ]}
      />
      {tab === "review" ? (
        <>
          <form
            className="row"
            aria-label="review filters"
            onSubmit={(e) => {
              e.preventDefault();
              setReload((n) => n + 1); // Enter in a field: the same as clicking Reload
            }}
          >
            <label htmlFor="kind">kind</label>
            <select id="kind" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">all</option>
              {(queue.kinds ?? []).map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <label htmlFor="status">status</label>
            <select id="status" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all</option>
              {statuses.map((s) => (
                <option key={s} value={s}>
                  {s} ({queue.proposal_status_counts?.[s]})
                </option>
              ))}
            </select>
            <label htmlFor="cfg">config path</label>
            <input id="cfg" value={config} onChange={(e) => setConfig(e.target.value)} onKeyDown={submitOnEnter} maxLength={1000} />
            <label htmlFor="by">your name (--by)</label>
            <input id="by" value={by} onChange={(e) => setBy(e.target.value)} onKeyDown={submitOnEnter} maxLength={64} placeholder="letters, digits, _ . @ -" />
            <button type="submit">
              Reload
            </button>
          </form>
          <p className="muted small" data-testid="review-count">
            {proposals.length} of {queue.proposals.length} proposals shown
          </p>
          {proposals.map((p) => (
            <ProposalCard key={p.id} row={p} config={config} by={by} />
          ))}
        </>
      ) : null}
      {tab === "learned" ? queue.learned.map((p) => <ProposalCard key={p.id} row={p} config={config} by={by} />) : null}
      {tab === "generation" ? (
        snapshot ? (
          snapshot.available ? (
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">id</th>
                  <th scope="col">kind</th>
                  <th scope="col">signature</th>
                  <th scope="col">category</th>
                  <th scope="col">priority</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.entries.map((e, i) => (
                  <tr key={i}>
                    <td>{displayText(e.id)}</td>
                    <td>{displayText(e.kind)}</td>
                    <td>{displayText(e.signature)}</td>
                    <td>{displayText(e.category)}</td>
                    <td className="num">{formatNumber(e.priority)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">{displayText(snapshot.reason)}</p>
          )
        ) : (
          <p className="muted">The generation snapshot route is not available on this server.</p>
        )
      ) : null}
      {tab === "decisions" ? (
        <table className="data">
          <thead>
            <tr>
              <th scope="col">at</th>
              <th scope="col">op</th>
              <th scope="col">id</th>
              <th scope="col">by</th>
              <th scope="col">reason</th>
            </tr>
          </thead>
          <tbody>
            {[...queue.decisions].reverse().map((d, i) => (
              <tr key={i}>
                <td>{displayText(d.at)}</td>
                <td>{displayText(d.op)}</td>
                <td>{displayText(d.id)}</td>
                <td>{displayText(d.by)}</td>
                <td>
                  <Text value={d.reason} max={300} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function ProposalCard({ row, config, by }: { row: ReviewRow; config: string; by: string }) {
  const [reason, setReason] = useState("");
  const [shown, setShown] = useState<{ op: DecisionOp; text: string; copied: boolean } | null>(null);
  const author = by.trim() || "YOUR_NAME";
  const copy = async (op: DecisionOp) => {
    const text = decisionCommand({ config, op, id: row.id, by: author, reason });
    let copied = false;
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch {
      copied = false; // clipboard unavailable (plain http on a LAN name): the text is shown to copy by hand
    }
    setShown({ op, text, copied });
  };
  const gate = row.gate;
  return (
    <article className="hit proposal" data-testid="proposal" data-id={row.id}>
      <header>
        <strong>{displayText(row.id)}</strong> <span className="badge">{displayText(row.kind)}</span> <span className="badge">{displayText(row.status)}</span>{" "}
        {gate ? <span className={gate.passed ? "badge contract-passed" : "badge contract-abstained"}>{gate.passed ? "✓ gate passed" : "✗ gate failed"}</span> : <span className="badge badge-unknown">no gate recorded</span>}
      </header>
      <KeyValues
        rows={[
          ["matched lines (gate)", row.matched_lines],
          ["signature", row.signature],
          ["category", row.category],
          ["precedence", row.precedence],
          ["fixes entry", row.target],
          ["examples", `${row.examples.length} shown of ${row.examples_total} (+${row.negative_examples} negative)`],
          ["author", row.provenance.author],
        ]}
      />
      <span className="label">regex</span>
      <Pre value={row.regex} max={4096} />
      <span className="label">redacted example lines</span>
      {row.examples.length ? row.examples.map((line, i) => <Pre key={i} value={line} max={2000} />) : <p className="muted">none recorded</p>}
      {gate ? (
        <details>
          <summary>gate checks ({Object.values(gate.checks).filter((c) => c.passed).length}/{Object.keys(gate.checks).length} passed, engine {displayText(gate.engine)})</summary>
          <table className="data">
            <tbody>
              {Object.entries(gate.checks).map(([name, c]) => (
                <tr key={name}>
                  <td>{c.passed ? "✓" : "✗"}</td>
                  <td>{displayText(name)}</td>
                  <td>
                    <Text value={c.detail ?? ""} max={500} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
      <div className="row">
        <label htmlFor={`reason-${row.id}`}>reason</label>
        <input id={`reason-${row.id}`} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} />
        {OPS.map(({ op, label }) => (
          <button type="button" key={op} onClick={() => void copy(op)} data-testid={`copy-${op}`}>
            Copy {label} command
          </button>
        ))}
      </div>
      {shown ? (
        <div data-testid="copied-command">
          <span className="muted small">{shown.copied ? "copied to the clipboard:" : "clipboard unavailable; copy this text:"}</span>
          <pre className="pre">{shown.text}</pre>
        </div>
      ) : null}
      <details>
        <summary>raw entry</summary>
        <JsonBlock value={row} />
      </details>
    </article>
  );
}
