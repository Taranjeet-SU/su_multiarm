// Runtime shapes of the public HTTP contract the UI reads (``su_multi_arm.models`` and
// ``su_multi_arm.runtime.events``). Objects are ``passthrough``: additive server fields never break
// the UI, but a missing or mistyped required field fails parsing and is shown as an error; the UI
// never fills a gap with invented data.
import { z } from "zod";

const Json = z.record(z.string(), z.unknown());

export const UnsupportedConstraint = z
  .object({ constraint_text: z.string(), reason: z.string(), suggestion: z.string().nullable().optional() })
  .passthrough();

export const Hit = z
  .object({
    document_id: z.string(),
    title: z.string(),
    source: z.string(),
    url: z.string().nullable(),
    text: z.string(),
    chunk_ids: z.array(z.string()).default([]),
    score: z.number(),
    arms: z.array(z.string()),
    provenance: z.array(Json).default([]),
  })
  .passthrough();
export type Hit = z.infer<typeof Hit>;

export const GroupRow = z
  .object({
    key: z.record(z.string(), z.unknown()),
    value: z.unknown(),
    count: z.number(),
    rank: z.number().nullable().optional(),
    share: z.number().nullable().optional(),
  })
  .passthrough();
export type GroupRow = z.infer<typeof GroupRow>;

/** A structured-arm aggregate record (``arms/structured.py::aggregate_record``). */
export const Aggregate = z
  .object({
    id: z.string().optional(),
    operation: z.string(),
    field: z.string().nullable().optional(),
    value: z.unknown().optional(),
    exactness: z.enum(["exact", "bounded", "approximate"]).optional(),
    denominator: z.number().optional(),
    basis: z.array(z.string()).optional(),
    scope: z.string().optional(),
    document_ids: z.array(z.string()).optional(),
    document_ids_truncated: z.boolean().optional(),
    groups: z.array(GroupRow).optional(),
    rows: z.array(Json).optional(),
    total: z.number().nullable().optional(),
    missing_or_invalid: z.number().optional(),
    zero_match: z.boolean().optional(),
    unsupported: z.array(Json).optional(),
    note: z.string().optional(),
    shown: z.string().optional(),
    numerator: z.unknown().optional(),
    comparison: Json.optional(),
    rendering: z.string().optional(), // log intents (addendum A2 B5): the deterministic sentence
  })
  .passthrough();
export type Aggregate = z.infer<typeof Aggregate>;

export const SearchResult = z
  .object({
    query: z.string(),
    generation: z.string(),
    hits: z.array(Hit),
    arm_hits: z.record(z.string(), z.array(Hit)),
    aggregates: z.array(Aggregate).default([]),
    unsupported_constraints: z.array(UnsupportedConstraint).default([]),
    trace: Json.default({}),
  })
  .passthrough();
export type SearchResult = z.infer<typeof SearchResult>;

export const Violation = z.object({ rule: z.string(), detail: z.string(), action: z.string().default("abstain") }).passthrough();
export type Violation = z.infer<typeof Violation>;

export const ContractStatus = z.enum(["passed", "stripped", "abstained"]);
export type ContractStatus = z.infer<typeof ContractStatus>;

export const Answer = z
  .object({
    answer: z.string(),
    claims: z.array(z.object({ text: z.string(), kind: z.string(), evidence_ids: z.array(z.string()).default([]) }).passthrough()).default([]),
    citations: z.array(z.string()).default([]),
    insufficient_evidence: z.boolean().default(false),
    short_answer: z.string().nullable().optional(),
  })
  .passthrough();

export const Coverage = z
  .object({ policy: z.string(), documents_total: z.number(), documents_covered: z.number(), note: z.string().nullable().optional(), incomplete_stages: z.array(z.string()).default([]) })
  .passthrough();

export const AnswerResult = z
  .object({
    answer: Answer,
    retrieval: SearchResult,
    trace: Json,
    unsupported_constraints: z.array(UnsupportedConstraint).default([]),
    coverage: Coverage.nullable().optional(),
    violations: z.array(Violation).default([]),
    contract: ContractStatus.default("passed"),
  })
  .passthrough();
export type AnswerResult = z.infer<typeof AnswerResult>;

// --- stream events (runtime/events.py) -----------------------------------------------------------

const seq = { seq: z.number() };
export const StreamEvent = z.discriminatedUnion("event", [
  z.object({ ...seq, event: z.literal("decision"), pipeline: z.string(), mode: z.string(), agent: z.string(), outcome: z.string(), intents: z.array(z.string()).default([]) }).passthrough(),
  z.object({ ...seq, event: z.literal("arm_result"), round: z.number(), arm: z.string(), record_ids: z.array(z.string()).default([]), aggregates: z.number().default(0) }).passthrough(),
  z.object({ ...seq, event: z.literal("review_round"), round: Json }).passthrough(),
  z.object({ ...seq, event: z.literal("evidence"), ids: z.array(z.string()), omitted_ids: z.array(z.string()).default([]), truncated_ids: z.array(z.string()).default([]) }).passthrough(),
  z.object({ ...seq, event: z.literal("token"), stage: z.string().default("generator"), text: z.string(), provisional: z.literal(true) }).passthrough(),
  z
    .object({
      ...seq,
      event: z.literal("contract"),
      status: ContractStatus,
      violations: z.array(Violation).default([]),
      unsupported_constraints: z.array(UnsupportedConstraint).default([]),
      tokens_streamed: z.number().default(0),
      tokens_superseded: z.boolean().default(false),
    })
    .passthrough(),
  z.object({ ...seq, event: z.literal("done"), result: AnswerResult }).passthrough(),
  z.object({ ...seq, event: z.literal("error"), category: z.string(), message: z.string() }).passthrough(),
]);
export type StreamEvent = z.infer<typeof StreamEvent>;

// --- discovery ---------------------------------------------------------------------------------

export const Capabilities = z
  .object({
    datasets: z.array(z.object({ name: z.string(), generation: z.string().nullable(), profile: z.string().optional() }).passthrough()),
    default_pipeline: z.string(),
    pipelines: z.record(z.string(), z.object({ arms: z.array(z.string()), default_mode: z.string(), modes: z.array(z.string()) }).passthrough()),
    fields: z.array(z.object({ name: z.string(), type: z.string(), filterable: z.boolean(), listable: z.boolean(), groupable: z.boolean(), sortable: z.boolean() }).passthrough()),
    tools: z.array(z.string()).default([]),
    answer_streaming: z.object({ tokens: z.boolean() }).passthrough().optional(),
  })
  .passthrough();
export type Capabilities = z.infer<typeof Capabilities>;

export const UiSettings = z.object({ session_storage_token: z.boolean(), mount: z.string(), auth: z.enum(["none", "bearer", "custom"]) }).passthrough();
export type UiSettings = z.infer<typeof UiSettings>;
