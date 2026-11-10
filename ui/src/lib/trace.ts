// A read-only summary of the engine trace (``Engine._result`` / ``Engine.answer`` trace dicts).
// Missing values stay missing ("not reported"); spend is summed only over calls that reported a price.

type Dict = Record<string, unknown>;
const dict = (v: unknown): Dict => (v && typeof v === "object" && !Array.isArray(v) ? (v as Dict) : {});
const list = (v: unknown): Dict[] => (Array.isArray(v) ? v.filter((x) => x && typeof x === "object").map((x) => x as Dict) : []);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export interface ArmStep {
  round: number;
  arm: string;
  documents: number | null;
  seconds: number | null;
  aggregate: boolean;
  incomplete: boolean;
}

export interface ModelCall {
  stage: string;
  provider: string;
  model: string;
  success: boolean | null;
  inputTokens: number | null;
  outputTokens: number | null;
  seconds: number | null;
  usd: number | null;
  errorCategory: string | null;
}

export interface TraceSummary {
  decision: Dict;
  pipeline: string | null;
  mode: string | null;
  traceId: string | null;
  totalSeconds: number | null;
  retrievalSeconds: number | null;
  decisionSeconds: number | null;
  steps: ArmStep[];
  armsFired: string[];
  calls: ModelCall[];
  usdTotal: number;
  callsWithoutPrice: number;
  stopReason: string | null;
}

export function summarise(answerTrace: unknown, retrievalTrace: unknown): TraceSummary {
  const answer = dict(answerTrace);
  const retrieval = dict(retrievalTrace);
  const decision = dict(answer.decision ?? retrieval.decision);
  const steps: ArmStep[] = [];
  const rounds = list(answer.rounds);
  if (rounds.length) {
    rounds.forEach((round, i) => list(round.steps).forEach((s) => steps.push(step(s, i + 1))));
  } else {
    list(retrieval.steps).forEach((s) => steps.push(step(s, 1)));
  }
  const calls = list(answer.model_calls ?? retrieval.model_calls).map(
    (c): ModelCall => ({
      stage: String(c.stage ?? "—"),
      provider: String(c.provider ?? "—"),
      model: String(c.returned_model ?? c.model ?? "—"),
      success: typeof c.success === "boolean" ? c.success : null,
      inputTokens: num(c.input_tokens),
      outputTokens: num(c.output_tokens),
      seconds: num(c.seconds),
      usd: num(c.usd),
      errorCategory: typeof c.error_category === "string" ? c.error_category : null,
    }),
  );
  const priced = calls.filter((c) => c.usd !== null);
  return {
    decision,
    pipeline: typeof (answer.pipeline ?? retrieval.pipeline) === "string" ? String(answer.pipeline ?? retrieval.pipeline) : null,
    mode: typeof (answer.mode ?? retrieval.mode) === "string" ? String(answer.mode ?? retrieval.mode) : null,
    traceId: typeof (answer.trace_id ?? retrieval.trace_id) === "string" ? String(answer.trace_id ?? retrieval.trace_id) : null,
    totalSeconds: num(answer.seconds) ?? num(retrieval.seconds),
    retrievalSeconds: num(retrieval.seconds),
    decisionSeconds: num(decision.seconds),
    steps,
    armsFired: [...new Set(steps.map((s) => s.arm))],
    calls,
    usdTotal: priced.reduce((sum, c) => sum + (c.usd ?? 0), 0),
    callsWithoutPrice: calls.length - priced.length,
    stopReason: typeof answer.stop_reason === "string" ? answer.stop_reason : null,
  };
}

function step(s: Dict, round: number): ArmStep {
  return { round, arm: String(s.arm ?? "—"), documents: num(s.documents), seconds: num(s.seconds), aggregate: s.aggregate === true, incomplete: s.incomplete === true };
}
