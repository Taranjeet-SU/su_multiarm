# Observability V2 proposal — shared Codex/Claude plan

Status: **proposal for joint review, 2026-11-05**. Coverage improvement is the active priority.
Writing this proposal does not authorize a graph-store migration, new telemetry collection,
unbounded provider spending, or automatic remediation. Keep implementation claims separate from
plans and measured results. This supplements, rather than replaces, the package's
[V2 implementation plan](v2_implementation_plan.md).

The shared [Extraction learning plan V2](../EXTRACTION_LEARNING_PLAN.md) makes the
current coverage priority concrete. It separates existing library/audit mechanisms
from planned shared semantic contracts, blind adjudication, mandatory independent
holdouts and QA-gated promotion/reparse. Large-context batch audits remain excluded
from the standard integration pending semantic and omission validation.

## 1. Objective and current priority

Build an investigation system that can explain which clients and service instances experienced
which problems, during which period, with useful counts and inspectable evidence. The same facts
must be usable through the UI, API and MCP. Improve the quality and coverage of extracted evidence
before adding more reasoning layers.

Immediate work, already authorized by the owner:

1. Improve recognition and extraction service by service using isolated candidate libraries.
2. Use balanced examples, bounded Haiku proposals where useful, and independent validation.
3. Promote passing candidates with an accurate audit trail; preserve failed proposals and reasons.
4. Reparse, refresh indexes and communities, and measure deployed changes on the same data scope.
5. Continue parallel service investigations with one coordinator for integration and live changes.

Open-source replacement experiments and the broader ontology implementation below are deferred
while this coverage work proceeds. Do not restart either merely because this proposal exists.

## 2. Coverage must have explicit denominators

| Metric | Meaning | What it does not establish |
| --- | --- | --- |
| Collection completeness | Downloaded source records / source records in the same declared query and time scope | That parsing succeeded; all severities were collected |
| Scanned | Collected records processed by the parser / collected records | That any useful fields were extracted |
| Extracted | Scanned records with useful, validated structured event extraction / scanned records | That every desired field or inferred relationship is correct |

These are the owner's three headline metrics. Separately report noise recognition, field-specific
validation results, evaluated precision and freshness as diagnostic details. Noise recognition must
not inflate extracted coverage. A field-specific metric needs its declared required fields; do not
invent success from a regex match alone. If source-document and multiline-event counts differ,
state their units and mapping explicitly rather than divide incompatible counts. Unknown counters
remain unavailable. The indexed snapshot must not claim to measure downloads still outside it.

Report parsed, noise, format-only, unparsed, truncated and failed records separately. A valid
timestamp prefix alone is format recognition, not message understanding. Preserve source sampling
methods, weights and limitations. Never call all downloaded records “all ELK logs” unless the
source-count reconciliation establishes that for the identical query scope. INFO sampling and
fatal/error/warning collection are separate scopes.

Do not target 100% by adding a catch-all. Fixed repeated messages may be validated as observed
formats without claiming generalization to unseen variants. Payload/configuration recognition is
not evidence of operational failure. A benign diagnostic logged at ERROR must not silently become
a failure community merely because of its upstream severity field.

## 3. Current implementation boundary

Implemented in the logs branch, with deployment-specific results recorded separately:

- Redaction, deterministic regex matching, typed captures and extraction validation.
- A Drain-style miner, deduplicated and balanced proposal examples, nearby negative examples,
  and a helper for independent date/client holdouts. This is not the Drain3 package.
- Source and parser accounting, typed rollups, scoped aggregation, graph and community retrieval.
- Temporal co-firing incident candidates and separate daily shared-error communities with actual
  joint client/service/signature support records. Neither proves a common cause.
- Cited tabular answers with missing-day and sampled-count disclosures.
- Readable pattern labels, origin and human-review status, with supporting pattern expressions.
- A private deployment refresh job that imports completed local exports and rebuilds derived data.
  The job itself is not a general-purpose scheduler shipped in the public package.

Not established: complete ELK collection, complete service recognition, log-wide extraction
precision, confirmed causal reasoning, broad metrics/traces integration, or production RCA quality.
Synthetic tests verify contracts; they do not establish those outcomes.

## 4. Proposed operational ontology

The reference motivating this proposal is Netflix's
[ontology-driven observability article](https://netflixtechblog.com/ontology-driven-observability-building-an-end-to-end-knowledge-graph-at-netflix-scale-421392cf5adf).
The design below is a proposal for this repository, not a claim of feature parity.

| Concept | Proposed identity and purpose |
| --- | --- |
| Client | Authoritative tenant identifier; no identity inferred from arbitrary message text |
| Service type | Catalog identity for the application/component, independent of its installations |
| Service instance | Client, environment, deployment and service identity together |
| Error pattern | Versioned classification and extraction definition; origin and review status retained |
| Event occurrence | Source record identity and event time; distinct from a pattern or aggregate |
| Dependency | Catalog-backed or explicitly typed observed target, with identity confidence |
| Deployment/change | Recorded change identity and effective time, when a source is integrated |
| Incident candidate | Observed temporal association requiring investigation |
| Confirmed incident | An incident from an authoritative source or recorded review decision |
| Shared-error community | Explicit pattern membership across supported client/service instances |
| Alert | A record from an actual alerting system; not a renamed error count |
| Hypothesis | Proposed explanation with evidence, confidence, status and rejection history |

Keep schema concepts separate from changing instances. Preserve aliases without merging similarly
named services automatically. A missing catalog match is an unresolved entity, not permission to
invent an application. Validate node and edge types before publication.

Example relationships, using synthetic identifiers:

```text
event:e1  observed_for       client:alpha
event:e1  emitted_by         instance:alpha/prod/worker
event:e1  matches_pattern    pattern:dependency_timeout/v1
event:e1  names_dependency   dependency:queue
pattern:dependency_timeout/v1  member_of  community:shared_timeouts
```

Each asserted relationship needs source references, applicable client scope, observation time,
extractor/pattern version, and an evidence class. Co-occurrence edges must remain distinguishable
from explicit relationships and hypotheses. Reserve confirmed-cause predicates for an appropriate
confirmation process; a plausible LLM explanation or temporal sequence alone is insufficient.

## 5. Proposed pipeline after coverage stabilizes

1. **Observe:** ingest declared source scopes with collection reconciliation and freshness records.
2. **Normalize identity:** resolve clients, service types and instances against explicit catalogs.
3. **Extract:** apply validated rules; use bounded model extraction only for unsupported structures.
4. **Validate:** enforce typed fields, grounded spans, permitted predicates and source provenance.
5. **Publish:** atomically activate a versioned graph and associated rollups. Preserve the previous
   valid generation if required checks fail; disclose optional-source gaps.
6. **Correlate:** build temporal associations and shared-pattern communities with exact scope and
   approximation labels. Counts come from aggregates, not from fitting raw logs into model context.
7. **Investigate:** agents query bounded evidence and return observed facts separately from hypotheses.
8. **Learn:** reviewed findings become candidate rules or ontology changes, with regression checks.

Client-restricted queries must filter member evidence before calculating visible support. Global
community labels or counts must not reveal hidden membership. Do not construct client/service
cross-products from separate lists. Graph updates must be idempotent and retract or supersede
changed evidence, not accumulate duplicate facts indefinitely.

## 6. Staged delivery and acceptance

| Stage | Work | Acceptance evidence |
| --- | --- | --- |
| C0 — active | Service coverage and field quality | Per-service baseline and after-results on identical records; failure/diagnostic separation; held-out and negative tests; no unexplained regressions |
| C1 — proposed | Versioned ontology and identity rules | Synthetic schema tests; alias collision and cross-client isolation tests; migration and rollback design reviewed by Codex and Claude |
| C2 — proposed | Grounded occurrence and dependency graph | Every edge traces to supporting records; missing, sampled and truncated evidence stays visible; incremental replay matches rebuild |
| C3 — proposed | Additional observability sources | Each connector has declared completeness, freshness, identity mapping and source-specific validation |
| C4 — proposed | Investigation hypotheses | Independent judged cases; distinguish observation, correlation and causal claim; record accepted and rejected hypotheses |
| C5 — deferred | Action proposals | Separate authorization, preview and audit design; this plan grants no remediation permission |

Before choosing RDF/OWL/SHACL/SPARQL or changing graph stores, evaluate concrete needs for
portability, validation and querying against the current typed graph contracts. Standards adoption
is an open design decision, not a prerequisite for completing C0. Likewise, Drain3, GLiNER2 and
other extraction libraries require a bounded benchmark before any replacement decision.

## 7. Codex/Claude collaboration and review

- This file is the shared proposal. Link review notes here rather than maintaining divergent plans.
- Codex coordinates current coverage work and records measured results in `docs/VALIDATION.md`;
  private artifacts remain in the external test/deployment workspace.
- Claude review is **pending**. Review ontology identities, event versus aggregate semantics,
  scope isolation, incomplete-source behavior, promotion provenance and migration risks.
- Parallel service workers use isolated candidate libraries and shared enforced provider budgets.
  A coordinator serializes promotion batches and index activation; no worker rewrites human-owned
  library files or claims human review for automated validation.
- Every change must identify whether it is proposed, implemented, synthetically tested,
  externally integrated, or measured on a specified data scope.
- Preserve the active priority: improve useful service coverage before implementing C1–C5.

## 8. Open decisions for joint review

1. Which catalog is authoritative for each entity type, and how are unresolved aliases represented?
2. Which fields are required for useful extraction by service/event family?
3. How are proposed, automated-reviewed and human-reviewed patterns exposed consistently?
4. Which additional data source would most improve investigations after C0?
5. Does an interoperable RDF export satisfy the need before a graph-store migration?
6. How are confirmed incident outcomes and rejected explanations fed back without erasing history?

Review log: 2026-11-05 — initial Codex proposal created at the owner's request; Claude review pending.
