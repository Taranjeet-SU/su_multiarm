## Incident-summary deployment addendum — 2026-11-07

Implemented opt-in source-side rule summary contract, bounded evidence, canonical hourly counts,
ACL-scoped `incident_episodes`, graph association documents, declared shared-rule communities,
and summary Overview. Private deployment scheduling is separate from public package source.
Uncaptured source scopes take priority over refreshes; bounded batches reserve limited current-day
freshness work and historical rollups are refreshed at most daily. Initial validation is scoped: no claim of full-service completeness or incident precision. Remaining
work is fleet capture/backlog completion, unclassified-candidate rule review, and operation-lifecycle
instrumentation sufficient to establish real recovery durations. No raw-export fallback is allowed.

# Implementation plan and status

The [Unified SU platform plan](plans/unified_su_platform.md) records the requested endgame:
SU tickets, KB/docs and ELK connected through governed identities, graph/community evidence,
one MCP/UI and federated multi-arm planning. It identifies current dataset and tenant-ACL
limits, compatibility gates and a staged migration. It is a roadmap, not a deployed
cross-source integration; ongoing coverage and learning work continues.

The shared [Observability V2 proposal](plans/observability_v2.md) records the next ontology/graph
direction for Codex and Claude review. **Service coverage remains the active priority**; the broader
proposal is deferred and does not replace the package implementation plan below.

The shared [Extraction learning plan V2](EXTRACTION_LEARNING_PLAN.md) defines the next
coverage-improvement workflow: one versioned semantic contract, identical evidence,
independent comparison/adjudication, mandatory tenant/day/template holdouts,
QA-gated rule promotion and affected-record reparse. Its status table distinguishes
implemented library/audit mechanisms from planned enforcement and excluded batch
experiments. Learning changes the extraction library, not model weights.

[NVIDIA context-layer lessons](plans/nvidia_context_layer_lessons.md) records the inspected
source/reuse decision and portable question-aware packing, metadata and aggregate-lineage changes.
The full configured planner/extractor experiment and packing-only ablation are separate from the
original pilot; record actual development and fresh-evaluation results in VALIDATION.md before
claiming a quality improvement or promoting a configuration.

The [2026-11-07 handover](plans/codex_handover_2026_11_07.md) separates current
general package changes, SU integration findings, completed experiments and active
source-review/larger-local-audit work. It records intentional uncommitted changes
and the remaining opt-in packing and semantic-validation gates.

**Status (2026-11-03): superseded for 2.0.0a1.** The current plan, phases and acceptance criteria are
in [plans/v2_implementation_plan.md](plans/v2_implementation_plan.md); per-slice progress and
recorded deviations are in [plans/execution_log.md](plans/execution_log.md); evidence by level is in
[VALIDATION.md](VALIDATION.md). Release 2.0.0a1 is Phases 0 and 1 of that plan. The 0.1.0a1 tables
below are kept as history: several items they list as follow-on work (OpenSearch and other SQL
backends, typed relational scopes, temporal scopes) shipped in 2.0.0a1, and the v1 `routing` modes
they describe are now decision agents and pipelines.

This package turns earlier multi-arm retrieval lessons into a reusable library in its own
repository. It does not replace any existing search service. Apache-2.0 was explicitly selected by
the owner.

## Release 0.1.0a1 scope (historical)

| Workstream | Acceptance condition | Status |
|---|---|---|
| Research | Primary-source review of configuration, vector/FTS/graph, extraction, communities and model interfaces | Complete; RESEARCH.md |
| Typed config and canonical data | Unknown options rejected, paths relative to config, independent model roles, source field mapping | Implemented |
| Index lifecycle | Field coverage, atomic generation activation, update/delete consistency, recipe mismatch rejection | Implemented; local tests |
| Privacy | Explicit opt-in pre-index redaction with counts, no heuristic masking of arbitrary field names | Complete; redaction/mapping tests passed |
| Local arms | Dense, lexical, canonical aggregates, grounded graph and community retrieval | Implemented; end-to-end tests |
| Optional databases | Faiss, PostgreSQL/pgvector and Neo4j | Complete; real Faiss/PostgreSQL/Neo4j tests passed |
| Extraction | Tags, regex, spaCy NER/POS, GLiNER, LLM with grounded quotes | Complete; real GLiNER/spaCy and mocked LLM contracts; quality limits disclosed |
| Routing and generation | Raw/planned/agentic modes, call/hop limits, separate context, citations and evidence budget | Implemented; provider-contract tests |
| Fusion/evaluation | Per-arm union, alternatives, source weights, recall/MRR/arm results | Implemented |
| Interfaces | CLI, Python, API auth/context parity, MCP tools | Complete; API and real MCP stdio checks passed |
| OSS packaging | License, examples, security/architecture docs, CI, wheel/sdist, clean-install test | Complete; wheel/sdist and clean-install smoke passed |

## 0.1.0a1 follow-on list (historical; see the v2 plan for current status)

1. Freeze a customer-approved export and held-out query set outside this public repository. Run
   parity checks against the existing service: per-arm IDs/ranks, union coverage, answer quality,
   latency and cost. Do not automatically deploy this alpha into production.
2. Add incremental embedding reuse, bounded-memory streaming, async extraction queues with resume,
   delta graph/community updates, external generation garbage collection and snapshot rollback CLI.
3. Benchmark approximate Faiss/pgvector indexes and add HNSW/IVF knobs only with filter/candidate
   recall tests. Add Qdrant, OpenSearch, other SQL/catalog backends through tested adapter contracts.
4. Add GLiNER2 structured extraction and externally tested spaCy/GLiNER recipes; pin model revisions,
   measure label accuracy and truncation/field coverage. Optional extraction concurrency and rate-limit
   backoff should be explicit budgeted features, not unbounded background retries.
5. Extend the implemented cited community summaries into hierarchical communities and global map-reduce
   answering. Add independent report-quality evaluation and freshness monitoring. Keep exact aggregates
   separate from model-generated reports.
6. Add configurable static routing predicates, typed relational filters beyond the implemented validated equality constraints, richer
   filter operators and temporal scopes. A free-text count must not silently count all records.
7. Add independent answer judging, frozen splits, DSPy/offline prompt experiments, NDCG where graded
   labels exist, token cost tables and tracing integrations without leaking raw customer content.
8. Harden production serving: async cancellation, request deadlines/admission limits, per-tenant ACLs,
   secured HTTP MCP transport, durable job queue, provider retry policy, metrics and audit retention.
9. Review package/API naming and contributor feedback before a stable 1.0 release or public publishing.

The first release is deliberately labelled alpha. Implemented code, mocked provider contracts,
real local tests and real external service tests are different evidence levels; VALIDATION.md records
them separately. No production relevance claim follows from synthetic test results.

Optional precomputed LLM community summaries were also implemented in this release: per-claim
passage references, quote validation, explicit partial-input status, and report-assisted community
retrieval. Paid-model semantic quality remains unevaluated.
