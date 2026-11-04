import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { z } from "zod";
import { ApiError } from "../api/client";
import type { StreamEvent } from "../api/schema";
import { AnswerCard } from "../components/AnswerCard";
import { ErrorBanner } from "../components/common";
import { EvidenceTabs, type Focus } from "../components/EvidenceTabs";
import { EMPTY_RUN, reduceRun, ThinkingTimeline, type RunLog } from "../components/ThinkingTimeline";
import { useApp } from "../context";
import { displayText } from "../lib/text";

export function timeFieldsOf(fields: Array<{ name: string; type: string }> | undefined): Set<string> {
  return new Set((fields ?? []).filter((f) => f.type === "date" || f.type === "datetime").map((f) => f.name));
}

const Info = z.record(z.string(), z.unknown());

export function AskView({ initialQuestion = "", initialAsOf = "" }: { initialQuestion?: string; initialAsOf?: string }) {
  const { api, capabilities, setLastAnswer, features } = useApp();
  const [question, setQuestion] = useState(initialQuestion);
  const [asOf, setAsOf] = useState(initialAsOf);
  const [run, setRun] = useState<RunLog>(EMPTY_RUN);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [focus, setFocus] = useState<Focus | null>(null);
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const t0 = useRef(0);
  const timeFields = useMemo(() => timeFieldsOf(capabilities?.fields), [capabilities]);
  const hasInfo = features.routes.has("GET /v2/info");

  useEffect(() => setQuestion(initialQuestion), [initialQuestion]);
  useEffect(() => setAsOf(initialAsOf), [initialAsOf]);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => {
    if (!hasInfo) return;
    const abort = new AbortController();
    api.optional("/v2/info", Info, abort.signal).then(setInfo, () => undefined);
    return () => abort.abort();
  }, [api, hasInfo]);

  const onEvent = useCallback(
    (event: StreamEvent) => {
      if (event.event === "done") setLastAnswer(event.result);
      const at = (performance.now() - t0.current) / 1000;
      setRun((state) => reduceRun(state, event, at));
    },
    [setLastAnswer],
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const query = question.trim();
    if (!query || running) return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setError(null);
    setFocus(null);
    t0.current = performance.now();
    setRun({ ...EMPTY_RUN, startedAt: Date.now() });
    setRunning(true);
    try {
      await api.stream({ query, ...(asOf ? { as_of: asOf } : {}) }, { onEvent }, abort.signal);
    } catch (exc) {
      if (exc instanceof ApiError && exc.kind === "cancelled" && abort.signal.aborted) {
        setRun((s) => ({ ...s, stopped: true }));
      } else {
        setError(exc);
        setRun((s) => ({ ...s, transportError: exc instanceof Error ? exc.message : String(exc) }));
      }
    } finally {
      setRunning(false);
    }
  };

  const result = run.done?.event.result ?? null;
  const started = run.startedAt !== null;
  return (
    <div className="view ask">
      <form onSubmit={submit} className="ask-hero" aria-label="ask a question">
        <label htmlFor="question" className="muted small">
          Question
        </label>
        <textarea id="question" value={question} onChange={(e) => setQuestion(e.target.value)} rows={2} maxLength={16000} placeholder="Ask about the indexed logs and documents" />
        <div className="row">
          <span className="hint muted small">
            <label htmlFor="as-of">As of </label>
            <input id="as-of" type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
            {typeof info?.as_of === "string" && info.as_of !== asOf ? (
              <button type="button" className="ghost" onClick={() => setAsOf(String(info.as_of))}>
                use last composed day ({displayText(info.as_of)})
              </button>
            ) : null}
          </span>
          <button type="button" onClick={() => controller.current?.abort()} disabled={!running}>
            Stop
          </button>
          <button type="submit" disabled={running || !question.trim()}>
            Ask
          </button>
        </div>
      </form>
      <ErrorBanner error={error} />
      {!started ? (
        <div className="empty-state">
          <h2>Ask a question to see the run</h2>
          <p>The timeline shows the plan, each retrieval arm, the evidence and the contract check as they happen; the answer cites its evidence.</p>
        </div>
      ) : (
        <div className="ask-layout">
          <aside>
            <ThinkingTimeline run={run} running={running} />
          </aside>
          <div>
            {!result && run.provisional ? (
              <section className="answer provisional" aria-live="polite" aria-label="provisional answer">
                <p className="muted small">Drafting: provisional model output, replaced by the checked answer.</p>
                <pre className="pre dim">{displayText(run.provisional)}</pre>
              </section>
            ) : null}
            {!result && !run.provisional && running ? <section className="answer muted">Working… the answer appears here when the contract check is done.</section> : null}
            {result ? <AnswerCard result={result} history={info} onCite={(id) => setFocus({ id, nonce: Date.now() })} /> : null}
            {result ? <EvidenceTabs result={result.retrieval} answerTrace={result.trace} focus={focus} timeFields={timeFields} /> : null}
          </div>
        </div>
      )}
    </div>
  );
}
