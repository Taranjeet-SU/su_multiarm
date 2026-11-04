import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import type { AnswerResult, StreamEvent, Violation } from "../api/schema";
import { ContractBadge, ErrorBanner, Text } from "../components/common";
import { EvidenceTabs, UnsupportedList, type Focus } from "../components/EvidenceTabs";
import { useApp } from "../context";
import { displayText } from "../lib/text";

type Phase = "idle" | "running" | "done" | "error" | "stopped";

interface RunState {
  phase: Phase;
  decision: Extract<StreamEvent, { event: "decision" }> | null;
  arms: Array<Extract<StreamEvent, { event: "arm_result" }>>;
  evidenceIds: string[];
  truncatedIds: string[];
  provisional: string;
  contract: Extract<StreamEvent, { event: "contract" }> | null;
  result: AnswerResult | null;
  streamError: { category: string; message: string } | null;
}

const INITIAL: RunState = { phase: "idle", decision: null, arms: [], evidenceIds: [], truncatedIds: [], provisional: "", contract: null, result: null, streamError: null };

export function timeFieldsOf(fields: Array<{ name: string; type: string }> | undefined): Set<string> {
  return new Set((fields ?? []).filter((f) => f.type === "date" || f.type === "datetime").map((f) => f.name));
}

export function AskView({ initialQuestion = "" }: { initialQuestion?: string }) {
  const { api, capabilities, setLastAnswer } = useApp();
  const [question, setQuestion] = useState(initialQuestion);
  const [run, setRun] = useState<RunState>(INITIAL);
  const [error, setError] = useState<unknown>(null);
  const [focus, setFocus] = useState<Focus | null>(null);
  const controller = useRef<AbortController | null>(null);
  const timeFields = useMemo(() => timeFieldsOf(capabilities?.fields), [capabilities]);

  useEffect(() => setQuestion(initialQuestion), [initialQuestion]);
  useEffect(() => () => controller.current?.abort(), []);

  const onEvent = useCallback(
    (event: StreamEvent) => {
      if (event.event === "done") setLastAnswer(event.result);
      setRun((state) => {
        switch (event.event) {
          case "decision":
            return { ...state, decision: event };
          case "arm_result":
            return { ...state, arms: [...state.arms, event] };
          case "evidence":
            return { ...state, evidenceIds: event.ids, truncatedIds: event.truncated_ids };
          case "token":
            return { ...state, provisional: state.provisional + event.text };
          case "contract":
            return { ...state, contract: event };
          case "done":
            return { ...state, phase: "done", result: event.result };
          case "error":
            return { ...state, phase: "error", streamError: { category: event.category, message: event.message } };
          default:
            return state;
        }
      });
    },
    [setLastAnswer],
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const query = question.trim();
    if (!query || run.phase === "running") return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setError(null);
    setFocus(null);
    setRun({ ...INITIAL, phase: "running" });
    try {
      await api.stream({ query }, { onEvent }, abort.signal);
    } catch (exc) {
      if (exc instanceof ApiError && exc.kind === "cancelled" && abort.signal.aborted) {
        setRun((s) => ({ ...s, phase: "stopped" }));
      } else {
        setError(exc);
        setRun((s) => ({ ...s, phase: "error" }));
      }
    }
  };

  const stop = () => controller.current?.abort();
  const result = run.result;
  return (
    <div className="view ask">
      <form onSubmit={submit} className="ask-form" aria-label="ask a question">
        <label htmlFor="question">Question</label>
        <textarea id="question" value={question} onChange={(e) => setQuestion(e.target.value)} rows={3} maxLength={16000} placeholder="Ask about the indexed data" />
        <div className="row">
          <button type="submit" disabled={run.phase === "running" || !question.trim()}>
            Ask
          </button>
          <button type="button" onClick={stop} disabled={run.phase !== "running"}>
            Stop
          </button>
          <span className="muted" aria-live="polite">
            {run.phase === "running" ? "streaming…" : run.phase === "stopped" ? "stopped: the run was cancelled on the server" : ""}
          </span>
        </div>
      </form>
      <ErrorBanner error={error} />
      {run.streamError ? (
        <div className="banner banner-error" role="alert">
          <strong>The answer failed ({displayText(run.streamError.category)})</strong>
          <div>
            <Text value={run.streamError.message} max={1000} />
          </div>
        </div>
      ) : null}
      {run.phase !== "idle" ? <Progress run={run} /> : null}
      {run.provisional && !result ? (
        <section className="provisional" aria-live="polite" aria-label="provisional answer">
          <p className="muted small">Provisional model output (not checked yet; replaced by the final answer):</p>
          <pre className="pre dim">{displayText(run.provisional)}</pre>
        </section>
      ) : null}
      {result ? <FinalAnswer result={result} onCite={(id) => setFocus({ id, nonce: Date.now() })} /> : null}
      {result ? <EvidenceTabs result={result.retrieval} answerTrace={result.trace} focus={focus} timeFields={timeFields} /> : null}
    </div>
  );
}

function Progress({ run }: { run: RunState }) {
  return (
    <ol className="progress" aria-label="run progress">
      <li className={run.decision ? "ok" : ""}>decision{run.decision ? `: ${displayText(run.decision.agent)} → ${displayText(run.decision.outcome)}${run.decision.intents.length ? ` (${displayText(run.decision.intents.join(", "))})` : ""}` : ""}</li>
      <li className={run.arms.length ? "ok" : ""}>arms: {run.arms.length ? run.arms.map((a) => `${displayText(a.arm)} (${a.record_ids.length}${a.aggregates ? ` + ${a.aggregates} agg` : ""})`).join(", ") : "—"}</li>
      <li className={run.evidenceIds.length ? "ok" : ""}>
        evidence: {run.evidenceIds.length}
        {run.truncatedIds.length ? ` (${run.truncatedIds.length} truncated to fit)` : ""}
      </li>
      <li className={run.contract ? "ok" : ""}>contract: {run.contract ? displayText(run.contract.status) : "—"}</li>
    </ol>
  );
}

function FinalAnswer({ result, onCite }: { result: AnswerResult; onCite: (id: string) => void }) {
  const { answer } = result;
  const citations = answer.citations;
  const index = new Map(citations.map((c, i) => [c, i + 1]));
  return (
    <section className="answer" aria-label="answer">
      <header className="row">
        <h2>Answer</h2>
        <ContractBadge status={result.contract} />
        {answer.insufficient_evidence ? <span className="badge badge-unknown">insufficient evidence</span> : null}
      </header>
      {result.contract !== "passed" ? <ViolationList status={result.contract} violations={result.violations} /> : null}
      <p className="answer-text" data-testid="answer-text">
        {displayText(answer.answer)}
      </p>
      {answer.short_answer ? (
        <p>
          <span className="label">short answer</span> <Text value={answer.short_answer} max={2000} />
        </p>
      ) : null}
      {citations.length ? (
        <div className="chips" aria-label="citations">
          <span className="label">citations:</span>
          {citations.map((id, i) => (
            <button type="button" className="chip cite" key={`${id}-${i}`} onClick={() => onCite(id)} title="show this evidence">
              [{i + 1}] {displayText(id)}
            </button>
          ))}
        </div>
      ) : null}
      {answer.claims.length ? (
        <ul className="claims">
          {answer.claims.map((c, i) => (
            <li key={i}>
              <span className="badge">{displayText(c.kind)}</span> <Text value={c.text} max={1000} />{" "}
              {c.evidence_ids.map((id) => (
                <button type="button" className="chip cite" key={id} onClick={() => onCite(id)}>
                  [{index.get(id) ?? "·"}] {displayText(id)}
                </button>
              ))}
            </li>
          ))}
        </ul>
      ) : null}
      {result.unsupported_constraints.length ? <UnsupportedList items={result.unsupported_constraints} /> : null}
      {result.coverage ? (
        <p className="banner banner-warn">
          Coverage ({displayText(result.coverage.policy)}): {result.coverage.documents_covered} of {result.coverage.documents_total} documents covered
          {result.coverage.note ? ` — ${displayText(result.coverage.note)}` : ""}
        </p>
      ) : null}
    </section>
  );
}

function ViolationList({ status, violations }: { status: string; violations: Violation[] }) {
  return (
    <div className="banner banner-warn" role="note" data-testid="violations">
      <strong>{status === "abstained" ? "The answer contract abstained:" : "The answer contract removed unbacked content:"}</strong>
      <ul>
        {violations.map((v, i) => (
          <li key={i}>
            <code>{displayText(v.rule)}</code> ({displayText(v.action)}): <Text value={v.detail} max={600} />
          </li>
        ))}
      </ul>
    </div>
  );
}
