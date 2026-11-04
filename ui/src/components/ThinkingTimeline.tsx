// The live run timeline, driven only by the server's stream events (``runtime/events.py``):
// plan → arms → review → evidence → drafting → contract → answer. Every line is a field of an
// event or of the final trace; the UI never writes reasoning text of its own. A model narrative is
// shown only if the API sends one (today it does not: tokens are the raw provisional reply).
import type { ReactNode } from "react";
import type { AnswerResult, StreamEvent } from "../api/schema";
import { summarise } from "../lib/trace";
import { displayText, formatNumber, formatSeconds, formatUsd } from "../lib/text";
import { ContractBadge, JsonBlock, Text } from "./common";

export interface Timed<E> {
  at: number; // seconds since the request was sent (client clock)
  event: E;
}

type Ev<K extends StreamEvent["event"]> = Extract<StreamEvent, { event: K }>;

export interface RunLog {
  startedAt: number | null;
  decision: Timed<Ev<"decision">> | null;
  arms: Array<Timed<Ev<"arm_result">>>;
  reviews: Array<Timed<Ev<"review_round">>>;
  evidence: Timed<Ev<"evidence">> | null;
  firstToken: number | null;
  tokens: number;
  provisional: string;
  contract: Timed<Ev<"contract">> | null;
  done: Timed<Ev<"done">> | null;
  error: Timed<Ev<"error">> | null;
  transportError: string | null;
  stopped: boolean;
}

export const EMPTY_RUN: RunLog = { startedAt: null, decision: null, arms: [], reviews: [], evidence: null, firstToken: null, tokens: 0, provisional: "", contract: null, done: null, error: null, transportError: null, stopped: false };

export function reduceRun(run: RunLog, event: StreamEvent, at: number): RunLog {
  switch (event.event) {
    case "decision":
      return { ...run, decision: { at, event } };
    case "arm_result":
      return { ...run, arms: [...run.arms, { at, event }] };
    case "review_round":
      return { ...run, reviews: [...run.reviews, { at, event }] };
    case "evidence":
      return { ...run, evidence: { at, event } };
    case "token":
      return { ...run, firstToken: run.firstToken ?? at, tokens: run.tokens + 1, provisional: run.provisional + event.text };
    case "contract":
      return { ...run, contract: { at, event } };
    case "done":
      return { ...run, done: { at, event } };
    case "error":
      return { ...run, error: { at, event } };
    default:
      return run;
  }
}

type State = "done" | "active" | "pending" | "failed" | "warn";

function Step({ state, title, at, children }: { state: State; title: string; at?: number | null; children?: ReactNode }) {
  return (
    <li className={`tl-step ${state}`} data-state={state}>
      <span className="tl-dot" aria-hidden="true" />
      <div className="tl-title">
        <span>{title}</span>
        {at !== undefined && at !== null ? <span className="tl-time">+{at.toFixed(2)} s</span> : null}
      </div>
      {children ? <div className="tl-body">{children}</div> : null}
    </li>
  );
}

export function ThinkingTimeline({ run, running }: { run: RunLog; running: boolean }) {
  const failed = Boolean(run.error || run.transportError);
  const result: AnswerResult | null = run.done?.event.result ?? null;
  const trace = result ? summarise(result.trace, result.retrieval.trace) : null;
  const order: Array<[boolean, boolean]> = [
    [Boolean(run.decision), true],
    [run.arms.length > 0, Boolean(run.decision)],
    [Boolean(run.evidence), run.arms.length > 0 || Boolean(run.decision)],
    [run.tokens > 0 || Boolean(run.contract), Boolean(run.evidence)],
    [Boolean(run.contract), Boolean(run.evidence)],
    [Boolean(run.done), Boolean(run.contract)],
  ];
  const firstPending = order.findIndex(([reached]) => !reached);
  const state = (index: number): State => {
    if (order[index][0]) return "done";
    if (running && index === firstPending) return "active";
    return "pending";
  };
  const maxRecords = Math.max(1, ...run.arms.map((a) => a.event.record_ids.length));
  const serverStep = (arm: string, round: number) => trace?.steps.find((s) => s.arm === arm && s.round === round) ?? null;
  return (
    <section className="timeline" aria-label="run timeline" aria-live="polite" data-testid="timeline">
      <div className="card-head">
        <h3>Run timeline</h3>
        <span className="muted small">{running ? "streaming" : run.done ? "complete" : failed ? "failed" : run.stopped ? "stopped" : ""}</span>
      </div>
      <ol>
        <Step state={state(0)} title="Plan" at={run.decision?.at}>
          {run.decision ? (
            <>
              {displayText(run.decision.event.agent)} decision ({displayText(run.decision.event.mode)}): {displayText(run.decision.event.outcome)}
              {run.decision.event.intents.length ? (
                <div className="chips">
                  {run.decision.event.intents.map((i) => (
                    <span className="chip" key={i}>
                      intent {displayText(i)}
                    </span>
                  ))}
                </div>
              ) : null}
              <details>
                <summary>decision and planned steps</summary>
                <JsonBlock value={run.decision.event} />
              </details>
            </>
          ) : (
            "choosing arms and scope"
          )}
        </Step>
        <Step state={state(1)} title="Retrieval arms" at={run.arms.at(-1)?.at}>
          {run.arms.length ? (
            <div className="tl-arms">
              {run.arms.map(({ at, event }, i) => {
                const server = serverStep(event.arm, event.round);
                return [
                  <span key={`n${i}`}>
                    {displayText(event.arm)}
                    {event.round > 1 ? ` r${event.round}` : ""}
                  </span>,
                  <span key={`b${i}`} className="tl-bar" title={`${event.record_ids.length} records`}>
                    <BarFill ratio={event.record_ids.length / maxRecords} />
                  </span>,
                  <span key={`c${i}`} className="muted">
                    {event.record_ids.length}
                    {event.aggregates ? ` + ${event.aggregates} agg` : ""} · {server ? formatSeconds(server.seconds) : `+${at.toFixed(2)} s`}
                  </span>,
                ];
              })}
            </div>
          ) : (
            "waiting for the first arm"
          )}
        </Step>
        {run.reviews.map(({ at, event }, i) => (
          <Step key={`r${i}`} state="done" title={`Review round ${i + 2}`} at={at}>
            <details>
              <summary>what the reviewer asked for</summary>
              <JsonBlock value={event.round} />
            </details>
          </Step>
        ))}
        <Step state={state(2)} title="Evidence" at={run.evidence?.at}>
          {run.evidence ? (
            <>
              {run.evidence.event.ids.length} item(s) sent to the generator
              {run.evidence.event.truncated_ids.length ? `, ${run.evidence.event.truncated_ids.length} truncated to fit` : ""}
              {run.evidence.event.omitted_ids.length ? `, ${run.evidence.event.omitted_ids.length} omitted` : ""}
            </>
          ) : null}
        </Step>
        <Step state={state(3)} title="Drafting the answer" at={run.firstToken}>
          {run.tokens ? (
            <>
              {formatNumber(run.tokens)} provisional fragment(s) streamed
              <details>
                <summary>raw provisional output (replaced by the checked answer)</summary>
                <pre className="pre dim">{displayText(run.provisional)}</pre>
              </details>
            </>
          ) : run.contract ? (
            "the model replied without streaming"
          ) : null}
        </Step>
        <Step state={run.contract && run.contract.event.status !== "passed" ? "warn" : state(4)} title="Answer contract" at={run.contract?.at}>
          {run.contract ? (
            <>
              <ContractBadge status={run.contract.event.status} />
              {run.contract.event.violations.map((v, i) => (
                <div key={i}>
                  <code>{displayText(v.rule)}</code>: <Text value={v.detail} max={300} />
                </div>
              ))}
            </>
          ) : null}
        </Step>
        <Step state={failed ? "failed" : state(5)} title={failed ? "Failed" : "Answer"} at={run.done?.at ?? run.error?.at}>
          {run.error ? (
            <>
              {displayText(run.error.event.category)}: <Text value={run.error.event.message} max={400} />
            </>
          ) : run.transportError ? (
            <Text value={run.transportError} max={400} />
          ) : trace ? (
            <>
              server time {formatSeconds(trace.totalSeconds)} · {trace.calls.length} model call(s) · {trace.calls.length > trace.callsWithoutPrice ? formatUsd(trace.usdTotal) : "spend not reported"}
              {trace.callsWithoutPrice ? ` (+${trace.callsWithoutPrice} unpriced)` : ""}
            </>
          ) : run.stopped ? (
            "stopped: the run was cancelled on the server"
          ) : null}
        </Step>
      </ol>
    </section>
  );
}

/** The fill of a proportion bar: an SVG rect (no inline style attribute under the CSP). */
function BarFill({ ratio }: { ratio: number }) {
  const width = Math.max(2, Math.round(Math.min(1, Math.max(0, ratio)) * 100));
  return (
    <svg viewBox="0 0 100 6" preserveAspectRatio="none" width="100%" height="6" aria-hidden="true" className="tl-bar-svg">
      <rect x="0" y="0" width={width} height="6" rx="3" className="tl-bar-fill" />
    </svg>
  );
}
