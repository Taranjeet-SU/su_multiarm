// Synthetic payloads in the server's wire shapes (test-only; the app never falls back to them).
import type { AnswerResult, Hit, SearchResult } from "../api/schema";

export function hit(over: Partial<Hit> = {}): Hit {
  return {
    document_id: "logs:signature:aaa",
    title: "dependency.connect_failed",
    source: "logs",
    url: null,
    text: "db-client failed to connect redis after 3000 ms",
    chunk_ids: ["c1"],
    score: 0.5,
    arms: ["lexical"],
    provenance: [{ chunk_id: "c1", field: "template", start: 0, end: 40 }],
    ...over,
  };
}

export function searchResult(over: Partial<SearchResult> = {}): SearchResult {
  const h = hit();
  return {
    query: "redis connect failed",
    generation: "g-0001",
    hits: [h],
    arm_hits: { lexical: [h], dense: [], graph: [] },
    aggregates: [],
    unsupported_constraints: [],
    trace: {
      decision: { kind: "rules", agent: "rules", outcome: "decided", intents: [], seconds: 0.004 },
      pipeline: "default",
      mode: "rules",
      trace_id: "t1",
      steps: [{ arm: "lexical", documents: 1, seconds: 0.002, aggregate: false, incomplete: false }],
      seconds: 0.01,
      model_calls: [],
    },
    ...over,
  };
}

export function answerResult(over: Partial<AnswerResult> = {}): AnswerResult {
  return {
    answer: { answer: "Synthetic answer grounded in logs:signature:aaa.", claims: [{ text: "Grounded", kind: "narrative", evidence_ids: ["logs:signature:aaa"] }], citations: ["logs:signature:aaa"], insufficient_evidence: false, short_answer: null },
    retrieval: searchResult(),
    trace: {
      seconds: 0.2,
      rounds: [{ steps: [{ arm: "lexical", documents: 1, seconds: 0.002, aggregate: false }] }],
      decision: { agent: "rules", outcome: "decided", intents: [] },
      pipeline: "default",
      mode: "rules",
      model_calls: [{ stage: "generator", provider: "openai_compatible", model: "fake-model", success: true, input_tokens: 40, output_tokens: 30, seconds: 0.05, usd: null }],
      contract: { status: "passed", violations: [] },
    },
    unsupported_constraints: [],
    coverage: null,
    violations: [],
    contract: "passed",
    ...over,
  };
}

export function groupedAggregate(over: Record<string, unknown> = {}) {
  return {
    id: "aggregate:1",
    operation: "group_count",
    field: "service",
    value: null,
    exactness: "exact" as const,
    denominator: 76,
    basis: ["records of kind service_day in generation g-0001"],
    document_ids: ["a", "b"],
    groups: [
      { key: { service: "doc-fetcher" }, value: 22, count: 22 },
      { key: { service: "ingest-worker" }, value: 22, count: 22 },
      { key: { service: "query-gateway" }, value: 20, count: 20 },
    ],
    total: 76,
    missing_or_invalid: 0,
    zero_match: false,
    unsupported: [],
    ...over,
  };
}

/** A fetch double that answers from a route table and records the requests it saw. */
export function fakeFetch(routes: Record<string, (init: RequestInit) => Response | Promise<Response>>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const impl = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, init });
    const key = `${(init.method ?? "GET").toUpperCase()} ${url}`;
    const handler = routes[key];
    if (!handler) return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404, headers: { "content-type": "application/json" } });
    return handler(init);
  };
  return { impl: impl as typeof fetch, calls };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

export function sse(events: Array<Record<string, unknown>>, split = 7): Response {
  const text = events.map((e, i) => `id: ${i + 1}\nevent: ${String(e.event)}\ndata: ${JSON.stringify({ seq: i + 1, ...e })}\n\n`).join(": heartbeat\n\n");
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < text.length; i += split) controller.enqueue(encoder.encode(text.slice(i, i + split)));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
}
