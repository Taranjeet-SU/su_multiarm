# Applying lessons from NVIDIA Auto-Ontology

Status: source review and versioned evaluation, 2026-11-07. The first paired
pilot found complementary strengths, not a production winner. This follow-up
adds portable evidence-packing mechanisms and evaluates explicit semantic
configuration. It does not deploy NVIDIA or replace the existing kernel.

## Integration decision

Adapt useful mechanisms independently inside the existing contracts. Keep a
separately versioned NVIDIA relational-catalog service as a possible later option.
Do not make the complete native application a required kernel dependency.

| Option | Fit | Decision |
|---|---|---|
| Embed the native Python application | Relational catalog, business terms/attributes, schema FK graph and SQL retrieval; brings its own DAL, migrations, provider/configuration caches and broad dependencies | Defer; unnecessary for the current portable changes |
| Integrate an isolated service | Potential reviewed SQL schema/join catalog with explicit source, principal, version and failure contracts | Future candidate; no running integration claimed |
| Independently adapt selected mechanisms | Reviewed field meanings, validated source references, bounded question-relevant evidence and exact aggregation | Implement and evaluate in the existing core |
| Copy isolated upstream source | Requires useful leaf boundaries, pinned provenance and applicable license/notice preservation | No NVIDIA source copied in this change |

## Findings in the actual source

The inspected revision is
[`c447003f72a822301e927196a4ecb5b597c308d5`](https://github.com/NVIDIA/auto-ontology/tree/c447003f72a822301e927196a4ecb5b597c308d5).
These are revision-specific findings, not claims about every later version.

- [`semantic/models.py`](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/auto_ontology/semantic/models.py)
  binds attributes to physical source columns and retains datatype/description.
  [`semantic/term_extractor.py`](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/auto_ontology/semantic/term_extractor.py)
  sanitizes assignments against the supplied columns. Transfer explicit semantics
  and validated references while preserving our literal spans and edge kinds.
- The term extractor's domain block includes domains and core entities, but not
  the accepted `DomainSummary.business_rules`. It also sends at most 40 candidate
  column specifications. Supplying a rule object does not prove the model saw it;
  fallback assignment does not prove semantic review. Record these limits.
- [`retrieval/text_to_sql/agents/sql_from_semantic.py`](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/auto_ontology/retrieval/text_to_sql/agents/sql_from_semantic.py)
  uses relevant schema and source dialect to query source rows. Transfer compact
  typed evidence and native aggregation instead of treating retrieved snippets as
  exhaustive counts. Keep units, scope, source identity and contributor lineage.
- [`semantic/pipeline.py`](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/auto_ontology/semantic/pipeline.py)
  and [`semantic/semantic_fk.py`](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/auto_ontology/semantic/semantic_fk.py)
  compile table/business semantics and schema relationships. These executed paths
  are not an incident-instance extractor or shared-error community builder.
- Native PostgreSQL/pgvector storage, Alembic lifecycle, eager connector imports,
  LangChain/LangGraph and catalog-zone authorization require an explicit boundary.
  Business customer identity and physical evaluation views do not establish native
  customer row authorization. Invalid scope expressions must fail closed before
  native sampling; never adopt an unrestricted fallback as an access policy.

The inspected backend uses Apache-2.0 with NVIDIA copyright/SPDX headers and
separate dependency notices. No upstream code or runtime dependency is included
here. Future source copying must preserve applicable headers, license/notices,
modification provenance and actually distributed third-party dependencies; see
the pinned [LICENSE](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/LICENSE)
and [third-party notices](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/THIRD_PARTY_NOTICES.md).

## Implemented portable changes

The existing packer now accepts an optional question. For truncated records it
selects up to three literal field windows using bounded lexical/English intent
cues, rather than always taking the opening prefix. It records exact field and
packed-passage offsets and marks gaps. This is a retrieval heuristic, not semantic
entailment or guaranteed resolution coverage. Allocation, aggregate reservation
and configured field order remain in force; no-question packing retains prefixes.

`selection.packing.metadata_fields` is an explicit, empty-by-default allowlist.
Only those canonical scalar values enter answer evidence. Their compact JSON
wrapper is charged against the record allocation. Oversized metadata omits the
record with an explicit diagnostic. Values, units and timestamps are never
inferred or silently normalized by packing.

Aggregate evidence carries up to eight existing contributor IDs, matched-record
total, generation and separate upstream/local truncation flags. This is bounded
aggregate-level lineage, not per-group membership or a complete contributor list.
The whole aggregate remains indivisible under its existing reserve. Response
context/answer caches must be regenerated for the changed packing behavior;
packing alone requires no extraction/vector rebuild.

## Prompt and evaluation configuration

The private comparison uses the supported `extraction.prompt` override to carry
reviewed entity and field meanings; its existing prompt fingerprint invalidates
extraction reuse. No SU-specific meaning becomes a neutral default. Event time,
snapshot/seed observation time, unknown native update time, frozen workflow state,
source version and product applicability remain separate. Post-redaction field
offsets do not identify original private-source characters. Approved organization
aliases establish business identity, not access authority or incident causality.

The versioned experiment also tests the existing LLM planner against declared
typed fields. That is configuration, not newly implemented SQL execution. Generated
plans still pass native schema/scope validation; failures stay in the denominator.
Do not substitute a successful packing-only replay for a failed full candidate.

Both changed builders receive the same clarified source generation and bounded
context budget. Preserve the first pilot, report development replay separately
from fresh organization/day/record evaluation, and freeze source-grounded QA before
new outputs. Runtime counts, exact quote agreement and reduced extraction rejection
do not establish semantic precision, recall, good communities or a production win.
Actual results belong in [VALIDATION.md](../VALIDATION.md).
