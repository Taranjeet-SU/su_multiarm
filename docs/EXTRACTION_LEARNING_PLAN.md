# Extraction learning plan V2

Status: shared implementation proposal, 2026-11-07. This plan is the common reference
for Codex, Claude and reviewers. Improving useful service coverage remains the active
priority; the broader [Observability V2 proposal](plans/observability_v2.md) is separate.
The [implementation plan](PLAN.md), [architecture](ARCHITECTURE.md) and
[validation record](VALIDATION.md) remain authoritative about shipped behavior.

Learning updates a versioned extraction library and deterministic parsing logic. It
does **not** train or update model weights. An LLM proposes evidence-backed findings
and possible rules; it is not the ground-truth labeler or the promotion authority.

## 1. What exists, and what is still planned

| Capability | State | Actual boundary |
| --- | --- | --- |
| Versioned pattern libraries, typed captures, grounded links, proposal/review/promotion lifecycle and reparse mechanisms | Implemented | Existing syntax, support, out-of-cluster and regression gates are useful safeguards; they do not establish independent semantic precision or recall. |
| Single-record comparison tool, literal citation checks, provenance, failures, cache reuse and per-service reports | Implemented | `tools/log_extraction_audit.py` measures literal support and normalized exact agreement. Its aliases and field names are not an enforced shared semantic ontology. |
| Distinct-message balancing and independent split helper | Implemented helpers | The ordinary learning loop does not automatically reserve or enforce independent holdouts. The current helper can select a day or tenant; mandatory three-axis validation below is planned. |
| Source completeness, parser scanning and useful extraction counters | Implemented where recorded | Units, scope and unavailable counters remain explicit; source-summary populations and sampled evidence require their own denominators. |
| Large-context batches with at least 40k input tokens and JSON per log | Experimental, excluded from the standard integration | Earlier local experiments produced category omissions despite complete JSON; output-budget truncation is also an explicit failure. Later repaired local pilots remain experimental under independent QA and are not accepted standard replacements for single-record auditing. |
| Shared field/unit/edge contract enforced by regex, model output and evaluator | Planned | Existing canonical types and evidence rules are the foundation, not completion of this contract. |
| Automatic independent holdout reservation and safe gap-to-candidate pipeline | Planned | No automatic end-to-end semantic learning or quality claim is implied by the existing library lifecycle. |

Public source contains reusable contracts, harnesses, synthetic fixtures, workflow,
gates and sanitized progress. Operator corpora, learned customer rules, source
credentials, private prompts, retained excerpts and model replies remain outside the
public repository. A plan or commit is not a push, publication or deployment.

## 2. Freeze one shared extraction contract

Before a new comparison campaign, publish an explicit contract version and digest.
Regex output, model output, adapters and the evaluator must use that same contract.
Changing its meaning requires a new version and compatibility/reparse review.

The contract must declare:

- Field names, descriptions, data types, cardinality, allowed null/unknown states,
  canonical units, and approved aliases with conversion rules.
- Distinct identities for tenant, service type, service instance, endpoint, queue,
  collection, index, request/operation, error code and error message. Scope supplied
  by a trusted source remains separate from facts extracted from message text.
- Closed relation predicates with allowed subject/object types, direction,
  cardinality and supporting-evidence requirements. A mention, co-occurrence,
  declared dependency and explicitly reported causal expression are different
  evidence classes. A reported cause is not a verified operational root cause.
- Exact source field and character spans for observations, plus provenance for
  derived facts. No quote can be borrowed from another log in the same batch.
- Unit conversions that preserve the original text, value and unit. Unknown units
  remain unknown; normalization must not turn ambiguous durations into latency.
- Extraction status for unsupported, omitted, rejected, truncated or unavailable
  outputs. Empty findings do not mean that a log has no error or operational issue.

The current audit validator can accept a triplet endpoint from supplied trusted
scope even when it is absent from the quote. Such a proposal is **not** automatically
a core graph fact: `ExtractionOutput` requires typed same-chunk endpoints within the
relation span. Keep a scope-based proposal as a derived association candidate until
it can satisfy that span contract; changing its edge kind does not waive grounding.
Explicit canonical header spans can preserve scope provenance with the appropriate
native-link evidence class. Operational facts still need their own supporting
message evidence. Never bulk-insert audit triplets as facts merely because their
syntax and literal checks passed. A payload product mention likewise does not
establish a deployed dependency or causal relationship.

Only approved equivalences enter the evaluator. Lowercasing or similar field names
alone cannot establish semantic equivalence. For example, a contract may equate an
explicit `http_status` with `status_code`; it must not automatically equate every
field named `status` or `latency` with those meanings.

Synthetic examples of distinctions the contract must protect:

| Source meaning | Correct distinction | Incorrect interpretation |
| --- | --- | --- |
| `error_code=E42 error_message="queue rejected"` | Code and explanatory message are separate fields. | Store the whole message as the error code. |
| `request_latency_ms=125` versus `no response observed for 8 minutes` | Request latency has an explicit operation/unit; elapsed observation silence is a different duration with unknown recovery. | Convert silence into request latency or time to recover. |
| `collection=sample_records index=sample_records_by_id` | Collection and index are separate resource types. | Treat both names as interchangeable indexes. |
| `payload={"note":"cache-a unavailable"}` versus `client target=cache-a connect refused` | Payload text is a mention; the latter explicitly describes an attempted connection. | Infer that the logging service depends on every resource mentioned in a payload. |

## 3. Freeze evidence and reserve holdouts before learning

Each campaign records source-policy/window identity, trusted scope, field mapping,
masking recipe, extraction-library identity, contract version and the exact selected
input digest. Source filtering and example selection must be described separately.

Both extractors receive **identical source excerpts**, IDs, boundaries and trusted
scope. Neither sees a longer message than the other. Existing truncation is flagged;
any additional excerpting is explicit, reproducible and applied before both paths.
Model context/output truncation, provider failures and missing batch results are
failures or incomplete observations, not silent omissions from the denominator.

Sampling balances services, explicit tenants, dates, severity and unresolved shapes.
Record both deduplicated-family counts and represented occurrence counts. Exact
duplicate reuse may preserve occurrence counts and source memberships; same-length
or same-prefix messages are not safe cache identities. Template grouping is a
sampling/proposal aid, not proof that messages have the same operational meaning.

For the planned promotion pipeline, independent validation is mandatory across:

1. **Tenant holdout:** tenant evidence not used to mine templates, prompt the proposer
   or tune the candidate.
2. **Day holdout:** a later explicit day, reserved before proposal work; do not choose
   an easier older day after seeing results.
3. **Template-family holdout:** unseen variants/families, separated using a frozen
   grouping recipe, with exact duplicates removed from training across all splits.

Evaluate each axis separately and report any combined disjoint test set. Define
splits before mining and prompting; review related-variant leakage as well as exact
text leakage. If an axis lacks sufficient independent evidence, record validation
unavailable and keep the candidate unpromoted. Do not substitute a synthetic fixture
for a missing operational holdout or claim cross-tenant generalization from one
tenant. Repeated tuning on a holdout retires it as a blind test set.

## 4. Compare independently, then adjudicate disagreements

Run the LLM without regex findings, learned candidate rules or expected answers in
its prompt. Run the deterministic extractor independently under the same frozen
contract. Keep provider/model revision, input/output schema, prompt digest, token
counts, finish reason, timing and all failures in the private run record.

Normalize only contract-approved aliases and units, retaining raw values and spans.
Present source evidence and anonymized A/B findings to adjudicators without revealing
which producer supplied each answer. Review missing findings as well as extra ones;
two producers agreeing or both omitting a fact does not establish correctness.

Classify each disagreement before proposing a rule:

| Outcome | Action |
| --- | --- |
| Equivalent approved aliases/units with the same meaning and evidence | Correct comparison normalization; do not create a redundant regex. |
| Model error: wrong type, invented fact, unsupported relation, borrowed quote or omitted category | Reject or repair the model/evaluator path; do not teach the regex that mistake. |
| Deterministic extraction error | Record the affected scope, wrong field/value/span and regression cases. |
| Genuine deterministic gap supported by independent adjudication | Queue a scoped candidate with evidence and expected typed output. |
| Both wrong, both incomplete, or insufficient source evidence | Retain as unresolved; obtain labels/evidence rather than counting it as a gap or success. |

Large-batch acceptance requires exact one-result-per-input accounting, no duplicate
or unknown IDs, category-omission checks, no cross-record evidence transfer, and no
input/output truncation. Compare against an adjudicated single-record reference
across entity, metadata and relation categories and batch positions. Throughput is
accepted complete logs per wall-clock time, with output tokens and retries included;
JSON validity or a large input-token count alone cannot pass the quality gate.

## 5. Propose one scoped rule per genuine pattern family

Group adjudicated gaps by compatible service scope, message shape and semantic
meaning. Preserve distinct error/status values and operation roles where they change
meaning. Deduplication reduces repeated proposal work; it must not merge unrelated
issues or inflate independent support.

The proposer receives training positives with expected typed captures and difficult
negative examples from nearby nonmember messages. Include minimal synthetic
contrasts for ambiguous fields, routine informational text and payload mentions.
Holdout examples remain outside proposal prompts and repair feedback.

Every candidate records pattern/library versions, contract version, scope, author
and review provenance, evidence class, typed captures, unit conversions, positive
and negative references, expected failure modes and comparison results. Generated
patterns must pass the configured safe-engine/RE2-subset validation, anchoring and
size/runtime limits; proposal text is never executable code or arbitrary queries.
Rejected candidates and reasons remain auditable rather than silently retried until
a favorable score appears.

## 6. QA-gated versioned promotion

Before tuning, register target populations, independent-support requirements,
per-field/edge precision and recall targets, and regression tolerances. No automatic
promotion is permitted without those criteria and independent QA acceptance.

Promotion requires:

- Passing syntax/safety, typed-span, positive, nearby-negative and existing-library
  regression checks with no unreported errors or truncation.
- Passing all three required holdout axes; field/entity/relation semantics are
  independently adjudicated, including difficult negatives and prior misses.
- Comparison against the pinned baseline, with new false positives, lost captures,
  changed units, wrong resource roles and relation direction reported separately.
- A signed review decision naming exact candidate/library/contract identities,
  tested scope, thresholds, limitations and rollback version. A proposer cannot
  approve its own candidate merely because its examples match.

The underlying library review/promotion lifecycle already exists. Enforcement of
this entire semantic/independent-holdout gate in the ordinary learner is planned.
Until implemented, operators must not describe existing automatic promotion as
having passed this stronger workflow.

## 7. Reparse affected records and invalidate the right caches

After approval, publish an immutable library version and retain the prior version
for rollback. Determine affected records from the candidate's allowed scope, not
only records the old regex matched: prior unmatched records may contain the gap.

Reparse affected records with original provenance and explicit masking/field
boundaries. Invalidate deterministic caches when input, trusted scope, library,
contract, field mapping, units or extractor recipe changes. Reuse raw model replies
only when their exact input/model/prompt/schema/contract identity still matches;
reassess them against the new deterministic baseline instead of reusing stale
comparison scores.

Rebuild affected typed records, graph links, rollups and community memberships.
Check count conservation, duplicate accounting, source spans, filters/ACLs, recipe
compatibility and unchanged services. Stage a complete generation and atomically
switch serving only after validation; never mix incompatible ontology or library
versions in one active generation. Record partial failures, pending reparses,
freshness and rollback availability. A regex change alone cannot establish recovery
times, actual alerts or causal community membership.

## 8. Report operational coverage and semantic quality separately

| Metric | Counting basis | What it does not establish |
| --- | --- | --- |
| Completeness | Downloaded source documents divided by matching source documents in explicitly known indexed units; retain source-scope/missing-unit status. Grouped represented occurrences are a separate counter. | Whole-fleet capture when units are absent, or whole-source capture under keyword/incident filtering. |
| Scanned | Assembled records scanned divided by assembled records collected. | Extraction correctness, or scanning every source occurrence represented by a summary/example. |
| Extracted | Assembled records with useful deterministic extraction divided by assembled records scanned; noise/format-only recognition is separate. | Every entity/field/relation being found correctly, or complete operational understanding. |
| Semantic precision/recall | Correct accepted findings versus all accepted findings, and correct findings versus independently adjudicated gold findings, on the reserved holdouts. | Generalization outside the disclosed sample/population, or truth inferred from model agreement. |

Unknown, empty, inconsistent and deliberately filtered denominators remain explicit.
Source-summary counts describe their declared keyword/rule population; exemplar
scanning cannot be multiplied into extraction of every represented log. Report
semantic quality by field, unit, edge type, service and holdout axis, with sample
support, adjudication disagreement and uncertainty. A raw exact-match ratio between
regex and LLM findings is an agreement metric, not precision or recall.

Also track proposal yield, validated gap yield, rule promotion/rejection reasons,
pending reparse counts, cache reuse, failure/truncation rates, batch completeness,
category omissions, accepted logs/hour and tokens/calls per accepted result. These
support planning without turning ingestion volume into a relevance claim.

## 9. Shared delivery checkpoints

| Checkpoint | State | Exit evidence |
| --- | --- | --- |
| V2-L0: publish this common plan and status boundaries | Documentation | Plan linked from the package and Observability V2 plans; no private evidence shipped. |
| V2-L1: enforce the shared field/unit/edge contract | Planned | Versioned schemas, adapters and synthetic semantic contrast tests across both extractors/evaluator. |
| V2-L2: blind comparison and independent adjudicated holdouts | Planned | Frozen tenant/day/template splits, omission cases, producer-blind labels and reproducible per-category metrics. |
| V2-L3: scoped gap grouping and candidate proposal | Planned | Positive/negative evidence, safe patterns, failed-candidate accounting and no holdout leakage. |
| V2-L4: mandatory QA gate in the ordinary learning loop | Planned | Promotion refuses missing axes/criteria; approved identities and rollback are recorded. |
| V2-L5: affected reparse and publication verification | Existing mechanisms; stronger orchestration planned | Cache/recipe invalidation, count/filter/ACL regressions, staged graph/community rebuild and atomic serving switch. |
| Large-context audit optimization | Experimental and separate | Must pass the same semantic/omission gates before entering the standard package. |

Codex and Claude should update these states only with links to reviewed code and
actual validation scope. Sanitized aggregate progress can be committed; private
examples, learned customer patterns and provider replies cannot. GitHub inclusion,
remote publication and deployment remain separate actions.
