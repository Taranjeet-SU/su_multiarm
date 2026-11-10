# General package and SU integration handover — 2026-11-07

Status: local work in progress. This is the public, sanitized companion to the
owner's private operational handover. No real records, prompts, organization
aliases, credentials, endpoints, model files or deployment configuration belong
here. Preserve the private experiment artifacts in the external test workspace.

## General repository state

Branch `v2/phase-0`, committed HEAD `db40268`. Portable logs integration, the shared
extraction-learning plan, SU unification roadmap and first paired comparison are
already committed locally. No new push or deployment occurred in this follow-up.
The current intentional working-tree changes are not yet committed:

- Question-aware packing with exact field/passage offsets and bounded windows.
- Explicit empty-by-default `metadata_fields` allowlist with scalar preservation,
  budget accounting and oversized-metadata omission diagnostics.
- Bounded aggregate-level contributor lineage: up to eight provided IDs, native
  matched-record total, generation and upstream/local truncation flags.
- Capability-bound LLM planner schema: only enabled arms and known log intents;
  neutral deployments cannot populate reserved log-intent fields. Existing
  coherence, immutable-filter and scope validation remain active.
- Focused synthetic packing/planner tests, generated v2 Config schema and related
  ARCHITECTURE/CONFIGURATION/PLAN documentation. The v1 schema is unchanged.
- [NVIDIA source review and reuse decision](nvidia_context_layer_lessons.md).

Actual checks: packing/metadata/lineage suite 117 passed and independently rerun;
isolated capability-schema suite 50 passed; combined planner/log-intent/metadata/
packing/schema-repair regression 93 passed. Ruff passed. Current-source wheel and sdist also built successfully after the
planner change. Any later opt-in/code changes require their affected checks and
final build. These tests establish scoped local
contracts, not production relevance.

**Update 2026-11-08:** question-window selection is now opt-in via
`selection.packing.passage_selection` (default `prefix`), with schema, tests and docs
updated. Two older fail-policy tests now expect `schema_violation` for a disabled arm.
See the 2026-11-08 section of VALIDATION.md for the actual checks. The real packing-only replay showed mixed tradeoffs, so
the experiment does not support replacing the default. Metadata overhead also
needs better allocation where it displaces the requested source text. Update the
generated schema/tests/docs for any new Config field and preserve prior run pins.

## SU-specific work and findings

The [unified SU platform plan](unified_su_platform.md) remains a roadmap: combine
support, docs and ELK behind governed identities, graph/community evidence and one
MCP/UI. Reuse serving mechanisms from the earlier UI without importing the earlier
corpus. The kernel remains dataset-scoped; cross-dataset orchestration and native
customer-scoped logs authorization are not implemented by this experiment.

NVIDIA was inspected and executed at the pinned revision documented in the source
review. Adapt reviewed field meanings, validated physical references, compact
source projections and typed aggregates. No NVIDIA source or mandatory runtime
dependency was added. Its schema/business ontology does not replace an incident
instance graph or operational shared-error communities.

The first paired real-data pilot remains immutable. The follow-up used the same
179 development records and 23 questions with additive explicit metadata roles,
plus a separate 48-record, two-organization, 12-question evaluation. Source titles,
bodies and legacy values stayed fixed in development. Event, capture, unknown
native-update, frozen-status, source-version and post-redaction span roles were
made explicit. Historical tickets and recent logs were not declared one incident.

Development V3 source support, on 21 ordinal groups:

| Condition | Full | Partial | Absent |
|---|---:|---:|---:|
| Our full configured V3 | 6 | 3 | 12 |
| NVIDIA V3 source-query | 13 | 3 | 5 |

Our full V3 had 5 complete and 18 unsupported development executions; NVIDIA had
17 complete and 6 SQL errors. These statuses are separate from source support.
The packing-only ablation retained exactly the original retrieval results and
made no new model/search calls: original support groups 8/8/5 versus changed
7/8/6, with two question preferences each and nineteen ties. No blanket packing
improvement or full-candidate production acceptance follows.

A separately versioned capability-schema fix reused the existing indexes and
extractions without bypassing compatibility checks. Its 35 planner attempts
yielded 11 complete/12 unsupported development and 5 complete/7 unsupported fresh
executions. Fresh NVIDIA execution was 10 complete/2 SQL errors. Further semantic
ratings for the fresh data and the schema-fix candidate remain pending; execution
counts are not answer correctness. No retries, repaired SQL or per-question
best-result selection changed the stored runs.

All approaches retained the common 6,000-token outer evidence limit and explicit
view isolation. Physically separate evaluation views do not establish native
production row-level authorization. Source-grounded QA is agent-reviewed, not a
double-human production benchmark. No graph/community/regex promotion passed.

## Local extraction audit and active review

The original local-model first-pass audit completed all 18,003 eligible retained
representatives, plus 68 new tail-fill examples counted separately. Three hundred
thirty-three sensitive examples were quarantined; 182 primary batches completed
with zero failed batches. This is not all source ELK records or complete semantic
extraction coverage.

The comparison adapter initially omitted format fields already captured by regex.
An offline rebaseline preserved source equality and separated format metadata,
declared useful extraction, ontology disagreements and scope-derived relations.
The remaining 29,182 proposals are unadjudicated gap candidates, not proven missing
rules. Neither exact agreement nor the LLM output is independent ground truth.

The owner requested independent correctness/omission/false-positive review. A
160-example source-only sample spans 140 services and 55 tenants, with predictions
withheld until source gold freezes. Three reviewers own disjoint 72/64/24 splits;
the 24-example parent split and 72-example QA split are frozen and mechanically
validated. The remaining 64-example split is active. Format metadata and incidental payloads are separate from useful
operational facts; missing units, time roles, masked identities and scope links
remain qualified. One residual sensitive payload is export-quarantined and must
be excluded symmetrically rather than treated as a negative. No parity score or
rule promotion has been produced from this incomplete review.

The owner also requested a larger local-model audit over currently retained
examples. A new retained-source freeze contains 53,838 examples, 30,557 distinct normalized
families, 4,454 conservatively quarantined families and 26,103 eligible templates.
The supervised local audit started with 15,681 exact-input/scope results reused and
10,422 new targets. **It is not running:** it stopped at 18:29 UTC on 2026-11-07
(`failed_batch_requires_review`) after 12 completed batches, when one batch reply hit
the local model's completion-token limit. 1,437 new targets were completed at a
measured 1.48 records/s. No automatic retry occurred. Resuming needs an explicit
decision (raise the reply limit or split the failed batch). Reuse
completed results, freeze new input independently, and retain separate denominators.
Do not infer source collection completeness from either audit's size.

## Resume checklist

1. Decide how to resume the stopped larger local audit (failed batch, reply limit);
   reuse completed results and keep separate denominators.
2. Finish/freeze the disjoint source-only gold, then compare both extraction
   methods and adjudicate semantic aliases/omissions. Preserve privacy quarantine.
3. (Opt-in done 2026-11-08.) Resolve metadata-versus-passage allocation and
   remaining scope/identity projection defects as explicit later candidates.
4. Complete pending semantic QA without rerunning paid extraction unnecessarily.
5. Update validation with actual outcomes, run affected tests/Ruff/schema checks,
   build final artifacts externally, review the diff, then decide promotion.
6. Preserve the SU-only integration/authorization boundaries and existing services.
