# Validation report

## 2.0.0a1 status — 2026-11-03

Release candidate: `2.0.0a1` (branch `v2/phase-0`). Phase 0 and Phase 1 of
[the v2 implementation plan](plans/v2_implementation_plan.md) are implemented: the v2 kernel and
config, the six first-release adapters (SQLite, PostgreSQL, generic SQL, OpenSearch, Elasticsearch,
MongoDB) plus Neo4j for graph, OpenAI / Anthropic / OpenAI-compatible providers, rules / classifier /
LLM / cascade decision agents, declarative pipelines, sessions, tools and actions, and HTTP + SSE and
MCP (stdio and streamable HTTP) serving.

What the evidence is, by level (dated entries below give the detail):

- **Synthetic local suite** (`pytest -m 'not external'`): unit, contract, parity and conformance tests
  on synthetic fixtures with the in-memory, SQLite and SQLAlchemy-on-SQLite adapters and mocked
  providers. Count at the release-review fix commit: see the 2026-11-03 entry at the end.
- **Real-service conformance** (disposable local containers, `-m external`): one consolidated run of
  every adapter's conformance suite plus `tests/services/*` at source commit `8f6dd56` (100 passed,
  17 skipped, 0 failed; image digests recorded). See "2.0.0a1 release evidence" at the end.
- **Local-model provider smoke**: `tests/services/test_local_llm.py` 1 passed against a small local
  model (`qwen2.5:1.5b`) behind an OpenAI-compatible endpoint (coordinator run, 2026-11-03).
- **Lexical cross-engine agreement**: informational report in
  [LEXICAL_AGREEMENT.md](LEXICAL_AGREEMENT.md) (synthetic examples, 7 documents; not a gate).
- **Not measured**: relevance or answer quality on any non-synthetic corpus, paid-provider behaviour
  (hosted Anthropic and OpenAI remain mocked), scaling and concurrency.

The sections that follow, starting with the 0.1.0a1 alpha report, are kept as dated history; their
version numbers and test counts describe the code at that date, not this release.

## 0.1.0a1 alpha report — 2026-10-11 (historical)

Release: `0.1.0a1`, Python 3.13.5 on Linux. Exact installed package versions are in
[validation_environment.json](validation_environment.json). This report distinguishes actual local
and service tests from HTTP contract fixtures. No customer corpus or paid LLM request was used.

### Automated suite (0.1.0a1)

**40 tests passed**, including 3 real external-database tests, when their test environment variables
were supplied. The normal suite skips those 3 tests when disposable database endpoints are absent.
Ruff lint and formatting checks pass. The GitHub Actions matrix for Python 3.11/3.12/3.13 is supplied;
it has not been run on GitHub because the repository has not been published. Only Python 3.13.5 was
executed locally in this session.

Test coverage includes:

- Dense, lexical, structured count/date/grouping, graph, community, provenance and canonical dedup.
- Complete character coverage across field-aware chunks, real source titles/URLs, mapping and redaction.
- Upserts/deletes remove stale active text, graph facts and community memberships; extraction reuse;
  failed ingestion preserves the prior generation; incompatible recipes are rejected.
- Filters and seed-record exclusions; valid/invalid dates and missing counts; SQL field allowlists;
  planner filters cannot override request scope.
- All fusion options preserve the configured union; explicit final caps; Faiss/NumPy rank/score parity.
- Combined NER/POS/tag and LLM extraction keeps semantic relation endpoint types distinct.
- Separate context and original question; capability validation; no pointless agentic `missing=none`
  retry; bounded two-round follow-up with a cited answer.
- KB Details/resolution inclusion; bad generator citation IDs rejected; bad extraction evidence rejected.
- Community summary citations validated, budget omissions reported, fabricated references abort ingestion.
- Call-budget hard stops; model response truncation failures; embedding response order/count validation;
  OpenAI-compatible and Anthropic HTTP request/usage contracts using mocked responses.
- API auth, context-header/body parity and conflicts, unchanged no-context requests; MCP tool calls and
  actual stdio client/server initialization/tool invocation across a subprocess.
- Evaluation does not treat unlabelled queries as relevance failures.

### Actual backend and model checks

| Component | Evidence level | Result |
|---|---|---|
| SQLite catalog/FTS/aggregates/local graph | Real local storage/query tests | Passed |
| NumPy hash demo vectors | Real local computation | Passed; intentionally nonsemantic |
| Faiss exact cosine | Real library/index + NumPy parity/filter tests | Passed |
| PostgreSQL FTS + pgvector | Real disposable PostgreSQL 17 container | Passed build/query/filter/delete |
| Neo4j | Real disposable Neo4j 5 container | Passed mentions, explicit two-hop FACT traversal and excluded-bridge behavior |
| Louvain | Real NetworkX seeded clustering | Passed membership/retrieval |
| sentence-transformers | Real all-MiniLM-L6-v2 CPU inference | Passed embedding/ingestion/retrieval smoke |
| GLiNER | Real gliner_small-v2.1 CPU inference | Passed grounded person/organization/location extraction |
| spaCy | Real en_core_web_sm CPU inference | Passed NER and configured POS extraction |
| Anthropic / OpenAI-compatible chat | Mocked HTTP contract, schema/usage/truncation tests | Passed; no paid model-account validation |
| Remote embedding API | Mocked HTTP shape/order/error checks | Passed; no paid provider validation |
| Optional cross-encoder reranker | Implemented, no real-weight smoke in this session | Not certified on a particular model |
| LLM community summaries | Grounding/budget tests with deterministic model fixtures | Passed; quality needs a real-model held-out evaluation |
| FastAPI | TestClient and clean-wheel actual localhost server | Passed |
| MCP | Actual stdio client/server protocol | Passed |

Test images used:

- `pgvector/pgvector:pg17`, digest `sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d`.
- `neo4j:5-community`, digest `sha256:c7d25c0eeebfe125718d58b72ed5d663c85d0479047733845eb2237c67ce8069`.

These were separate containers on loopback ports 25432 and 27687. No production database was altered.
The test credentials in the test instructions are disposable examples, not production secrets.

### Synthetic semantic smoke

The six-document, three-question example retrieved all six labelled target documents within the top
five, with MRR 1.0. Median search time in that small CPU run was approximately **7.8 ms**, excluding
initial model loading/indexing. See [semantic_smoke.json](semantic_smoke.json). These figures are a
functional smoke test on a tiny synthetic corpus; they say nothing about relevance on any production corpus,
large-corpus latency, planner quality, or answer correctness. Reporting “100% relevance” from this
exercise would be misleading.

The [GLiNER smoke](gliner_smoke.json) extracted Alice/person, Acme/organization and California/location
from a synthetic sentence in about 5.9 seconds including model loading. That is not warm throughput
or a corpus-level NER accuracy measurement. The [spaCy smoke](spacy_smoke.json) also passed NER/POS extraction on a synthetic sentence.
Models ran on CPU; no GPU inference was used.

### Distribution checks

Wheel and source distribution build successfully. A fresh virtual environment without system site
packages installs the wheel plus API/MCP extras, passes `pip check`, and runs CLI ingestion, retrieval,
aggregates, deletion, an authenticated localhost API, and an MCP subprocess. The script is
`tests/smoke_wheel.py`. Archives were inspected for runtime indexes, virtual environments and `.env`
files; none were included. LICENSE and NOTICE are included in both distributions.

### Remaining release limits

- No customer relevance/answer benchmark or production traffic migration; no parity claim.
- No paid Sonnet/Haiku evaluation, extraction predicate factuality score, or community-summary quality score.
- No ANN/scaling/concurrency soak benchmark, stream ingestion, automatic external cleanup or rollback CLI.
- No per-user authorization platform, broad PII compliance guarantee, global request deadline or dollar cap.
- No independently verified answer judge, DSPy optimizer, hierarchical community/global map-reduce pipeline,
  GLiNER2 adapter, third-party plugin registry or existing-vector importer.

The implementation status and next steps are tracked in PLAN.md; [ADOPTING.md](ADOPTING.md) describes evaluating the package on your own corpus.

## Public-data local-model diagnostics — 2026-10-12

These checks used unmodified runtime source at `36d2394`, isolated working directories under the
owner's external test workspace, public data with recorded provenance, local model inference, and
CPU sentence-transformer embeddings. Dataset files, qrels, models, indexes, raw responses and
private endpoint configuration remain outside this repository. No paid pipeline inference,
service restart, model eviction or deployment was performed.

Codex and an actual Claude CLI collaborator proposed ten datasets and agreed to shortlist HotpotQA,
HybridQA, SciFact, QASPER and WikiTableQuestions. Only the first two were executed. The main pipeline
model was the installed `qwen2.5:14b-instruct`; embeddings were `all-MiniLM-L6-v2`, revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, on CPU. Six separate extraction probes used the already-loaded
`qwen3.8:27b`; these were not a second complete model evaluation.

**Neither original full-corpus semantic-extraction/cited-report pipeline completed successfully.**

| Dataset and frozen diagnostic scope | Original pipeline outcome | Explicitly separate follow-on checks |
|---|---|---|
| HotpotQA: 8 development questions, 72 pooled supplied context documents including distractors, 197 chunks | Strict ingestion failed on invalid extraction JSON. Per-document preflight accepted 1 document and rejected 71 at their first failing chunk. | Full-corpus source-title entity diagnostic with reports disabled after report-grounding failure; one grounded relation fragment; all retrieval arms, supported typed aggregates, raw/planned/agentic answering and independent community-report attempts. |
| HybridQA: 10 development questions, 11 released tables including 2 distractors, 313 table/row/passage records, 807 chunks | Strict ingestion failed on relation grounding. Independent all-chunk checks accepted 405 and rejected 402; 72 whole documents passed and 241 did not. | Full-corpus spaCy/report-disabled diagnostic; accepted-document LLM-extraction diagnostic with reports disabled after another grounding failure. The latter persisted 34 relation occurrences and 9 communities. All arms and answer modes were attempted. |

The follow-on configurations do not substitute for a successful original pipeline. A real local
provider transport is exercised here; this is stronger evidence than the earlier mocked chat
contracts, but it does not certify every local model, commercial provider or storage backend.

Independent peer checks reproduced the dataset mappings, selected gold references, retrieval
results, official-form answer scoring and structured counts. All 33 numeric aggregate probes in the
full HybridQA diagnostic matched expectations computed directly from raw tables; three source-date
probes returned null because the source strings do not satisfy the timestamp contract. The reduced
snapshot loses rows and therefore does not preserve full-table answers. Natural-language count probes yielded 0/9 correct answers in both HybridQA diagnostics despite
working explicit aggregates. Planner-inferred nonexistent table IDs produced confident zero counts,
and an unsupported numeric-total request was misrepresented using a record count. These are semantic
planning/scope failures, not incorrect count arithmetic.

In the full-corpus HotpotQA entity-only diagnostic, raw answer calls passed schema/citation-ID checks
on 8/8 questions; official answer exact match was 1/8 and F1 was approximately 0.252. Both reviewers
judged 5/8 answers semantically correct. These are different measures: verbose correct answers can
fail exact-form scoring, and valid citation IDs do not establish answer correctness. Planned and
agentic answer calls succeeded on 3/8 and 2/8 respectively. No sentence-support/joint HotpotQA score
or population accuracy claim is made.

Independent attempts on nine HotpotQA title-entity communities yielded four reports passing
reference validation, four rejected for grounding and one rejected for invalid JSON. The four
accepted reports contained eight claims and twelve exact source references. Reference validity
alone did not establish complete semantic support for every claim. The initial community-probe
harness used an incorrect database-column key and made no model calls; its corrected second version
is the source of these results. That initial harness error is not a package failure.

Separate HybridQA report attempts accepted 7/9 reports with 17 claims; one failed grounding and one
exhausted the completion cap. Independent review verified all 34 quoted references in the accepted
reports. These reports were not installed into an active snapshot and are not a semantic-quality score.

Graph review verified exact quote/endpoint containment for all 34 persisted HybridQA relation
occurrences and demonstrated one- and two-hop retrieval of additional documents in the reduced
snapshot. It also found an unsupported factual predicate extracted from a title, repeated across
five stored occurrences of one cached row-title extraction. The one-hop new-document example
depends on that unsupported edge; an independently verified two-hop example uses supported row-text
predicates. Thus successful traversal and verbatim quotes must not be represented as graph factual
precision or natural-question relevance. Repeated title extraction is also an efficiency and
evidence-duplication concern.

Peer review identified a measurement error in the reduced HybridQA harness: intersecting gold with
the surviving snapshot changes the evaluation denominator. Its conditional traced-node R@5 of 0.5
uses only 3 labelled questions and 4 surviving gold records. With all 10 original questions and
26 original gold records retained, macro R@5 is 0.125 and macro full-pool recall is approximately
0.1833. The corrected original-gold values are primary; traced-node labels themselves are weak.

Remaining blockers include reliable extraction/model recipes, explicit failure recovery and
coverage reporting, schema-constrained provider output, executable planner schemas, semantic
predicate/claim support, complete answer citations, generic context fields and typed structured
relationships. Strict grounding should remain; silent fallback, claim dropping or corpus reduction
would hide these failures. The six larger-model probes passed three titles and failed three body
chunks, including two completion-cap stops; they do not rank model quality.

Scope limits: 18 selected development questions, bounded corpus pools, source-derived aggregate and
graph/community probes, differing retrieval/prompt/budget configurations, no full-dataset or
leaderboard claim, and no scaling or production measurements. HotpotQA terms/provenance were
recorded from its [project](https://hotpotqa.github.io/) and pinned distribution. HybridQA's
[project](https://hybridqa.github.io/) states dataset CC BY 4.0; its separately hosted linked-table
repository has no standalone license, so the upstream Wikipedia/HybridQA terms caveat remains in
the external provenance record. This report contains aggregate findings only.

Regression checks for this session: **37 passed, 3 external service checks deselected**; Ruff check
of `src` and `tests` passed. All test temporary/cache paths were external and repository bytecode
writes were disabled. Runtime/config contracts and release artifacts were not changed, so this
session did not repeat the earlier wheel/sdist release validation.

Both the Claude execution owner and peer reviewer accepted the denominator, predicate-support,
answer-format and causal-interpretation corrections. Codex incorporated their amendments in the
external joint gap list and acceptance criteria. All original failures and review disagreements or
qualifications remain recorded. The original full-pipeline readiness conclusion is unchanged.

## Span-anchored grounding and predicate support — 2026-11-03 (branch `v2/phase-0`)

First Phase 0 slice of `docs/plans/v2_implementation_plan.md`, addressing the two ingestion failures
measured in the 2026-10-12 public-data diagnostics: exact-substring quote matching aborting builds on
case/whitespace drift, and relation predicates never being validated.

Implemented: `grounding.py` (NFKC/casefold/whitespace normalisation with a per-character offset map;
`locate()` returns verbatim source spans at `exact` or `normalised` level; ambiguous matches rejected
by default; matches never begin or end inside an expanded character group), `predicate_support.py`
(stemmed trigger check with negation window; unsupported predicates stored as `co_occurrence`, never
as facts), span-anchored `GroundedExtraction` output, catalog columns for offsets/match level/edge
kind/support, `index_format` 3 (previous snapshots and extraction caches are not reused), graph
traversal restricted to `fact` edges.

Evidence level: **synthetic unit and engine tests only.** `pytest -m 'not external'`: 64 passed,
3 deselected (previously 40). Fixture categories: exact, case-only, whitespace-only, NBSP, ligature,
full-width Latin, combining marks (decomposed and composed), sharp-s, ambiguous reject/first,
no-match, expanded-group boundary, 500 random offset-map property cases; predicate support on
camel/underscore predicates, declared triggers, three negation shapes; engine tests reproduce the
public-data shapes synthetically (drifted quote grounds at `normalised` level with verbatim span; the
title-only `hosted` relation across five records becomes five `co_occurrence` rows and zero facts;
the graph arm does not traverse them). `ruff check src tests` passed.

Not demonstrated: any change in acceptance rate on real corpora. That requires the owner-authorised
re-run of the frozen public cohorts (plan open decision 16). Relocation to another field of the same
record (`relocated` level), fact dedupe across inherited title chunks, and the quarantine/partial
ingestion policies are not yet implemented.

## Real-service test infrastructure — 2026-11-03

Phase 1 preparation (plan §6.1 real-service tier, §6.2 `services` job): `tests/services/docker-compose.yml`,
`tests/services/wait_ready.py`, `tests/services/README.md` and `.github/workflows/services.yml`.

Evidence level: **infrastructure verification only.** This entry shows that the throwaway stack
starts, reports healthy, answers protocol-level probes on non-default loopback ports and tears down
cleanly on one Linux host (Docker 29.0.4, Compose v2.40.3). No V2 adapter exists yet, so no backend
conformance test ran against OpenSearch, Elasticsearch or MongoDB; the three pre-existing
`external` tests (v1 PostgreSQL/pgvector and Neo4j adapters, `tests/test_external.py`) were run against
the stack as a smoke check of the wiring only. The GitHub Actions job has not been executed because the
repository is not published.

| Image (pinned tag) | Resolved digest |
|---|---|
| `opensearchproject/opensearch:2.19.1` | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` |
| `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56` |
| `pgvector/pgvector:0.8.0-pg17` | `sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87` |
| `neo4j:5.26.0-community` | `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5` |
| `mongo:7.0.16` | `sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a` |

Measured on the cold start (all five images pulled, about 3.6 GB on disk):

- `up -d --wait --wait-timeout 420` took 17 min 26 s wall clock, of which about 17 min 15 s was the
  image pull on this host's network; all five containers were created at 06:13:48 UTC and reported
  healthy within 11 s of creation (Mongo and PostgreSQL first, Neo4j last).
- `wait_ready.py` run 16 s after creation: every service ready on the first probe (0.0–0.1 s).

| service | endpoint | status | after | detail |
|---|---|---|---|---|
| elasticsearch | 127.0.0.1:19201 | ready | 0.0 s | cluster green |
| mongo | 127.0.0.1:27018 | ready | 0.1 s | ping ok |
| neo4j | 127.0.0.1:17687 | ready | 0.1 s | bolt 5.4 |
| opensearch | 127.0.0.1:19200 | ready | 0.0 s | cluster green |
| postgres | 127.0.0.1:15432 | ready | 0.0 s | accepting connections |

- Resident memory while idle and healthy: OpenSearch 975 MiB and Elasticsearch 998 MiB (512 MiB JVM
  heap each, 1.5 GiB container cap), Neo4j 827 MiB, MongoDB 376 MiB, PostgreSQL 67 MiB. Neo4j ran this
  check under a 1 GiB cap; the committed file raises it to 1.5 GiB because 827 MiB idle leaves too little
  headroom for test load. That raised value has not itself been re-measured.
- `pytest -q -m external` with `SU_TEST_POSTGRES_DSN`, `SU_TEST_NEO4J_URI`, `SU_TEST_NEO4J_PASSWORD`
  pointed at the stack: 3 passed, 75 deselected, 1.4 s. Run from the working tree with `PYTHONPATH=src`
  (the package is not installed in the interpreter used) while another agent's uncommitted Phase 0
  changes were present under `src/`; the three external tests themselves were unchanged.
- `down -v` completed in 10 s. `docker ps` before and after was identical (four unrelated containers
  on this host were never stopped); zero containers, networks or volumes labelled with the compose
  project remained. Because PostgreSQL, Neo4j and MongoDB data directories are tmpfs, no anonymous
  volume was created in the first place.
- Not changed on the host: `vm.max_map_count` stayed at 65530; both Lucene engines accept this in
  `discovery.type=single-node` mode. Nothing was exposed beyond loopback.
- Nothing failed. The only deviation from the plan's §6.2 image list is that tags are pinned to specific
  versions instead of major-version floats, and MySQL is omitted because it is not a first-release backend.

Not demonstrated: any adapter behaviour on OpenSearch, Elasticsearch or MongoDB; the `services` CI job
on GitHub; behaviour under the test load of the Phase 1 conformance suite; behaviour on a runner with
less memory than this host (the stack needs about 3.3 GiB resident idle, about 5.5 GiB of caps).

## Kernel: types, metadata schema, component/registry, config v2, backend contracts, runtime — 2026-11-03 (branch `v2/phase-0`, slice P0/S2)

Implemented (plan §2.3 layout, §3.1, §3.1a, §3.2, §3.3, §3.4, §3.5, §5): `core/{types,component,
registry,config,schema,errors}.py`, `compat/config_v1.py` (v1 model moved here; `upgrade`,
`downgrade`, `upgrade_text`), `backends/{contracts,capabilities,memory}.py`, `runtime/{context,
session,memory,tools,actions,budget,trace,acl}.py`, CLI `config upgrade|validate|schema`,
regenerated `docs/config.schema.json` (now v2) and new `docs/config_v1.schema.json`.

Evidence level: **synthetic unit tests and the in-memory fake only; no real service, no model
call.** `pytest -m 'not external'`: 154 passed, 2 skipped (`mcp` package absent in the interpreter
used for this run; both are the existing MCP tests), 3 deselected — previously 62 passed / 2 skipped
in the same interpreter (64 passed when `mcp` is installed). The 92 new tests cover: span/source
equality, containment and edge-kind invariants; metadata type/nullability/enum/timezone violations by
category and filterable/listable/groupable gating; registry registration, diagnostics, unknown
plugin, non-conforming plugin, pickle/dotted-path and sha256-mismatch rejection; config v2 invariants
(20 rejection cases with key paths, native structured output, plugin name rules, classifier artifact
rules, code references anywhere in the file); every `examples/*.yaml` upgrading deterministically
(byte-identical output, `downgrade(upgrade(v1))` equal to the v1 model modulo `arms.enabled` order
and the same v1 `index_fingerprint()` — N6), schema drift for both files, the CLI subcommands; the
fake adapter satisfying all eight protocols, matrix consistency against the config `kind` union,
generation scoping, paging, phrase/boolean/scope pushdown, refresh visibility, exact vector search
with namespaces, typed scope execution (count, count_distinct, sum, first_seen, bucket, group_by,
half-open date intervals, timezone-missing bounds reported as unsupported, zero-match flag, ACL
denominators), listable-only value enumeration, fact-only traversal with provenance, cache-key prefix
and TTL, ledger sequence/raw replies, session expiry; budget exhaustion for calls/tokens/USD (price
table required, failed call releases its reservations), deadline, trace masking of query/passages/
unsupported-constraint text/follow-up queries with a raw opt-in, principal-scoped sessions (403-class
error for another principal), `generation_changed` on the trace, episodic re-validation, audited
idempotent actions with surface gating, tool registry schemas. `ruff check src tests` passed.

Not demonstrated: any engine behaviour on the new contracts (arms, write path, sqlite adapter are
the next slices); real adapters against the frozen contracts; plugin loading from an installed
distribution (entry points are exercised with a monkeypatched entry-point list); any production
outcome.

### Kernel review fixes — 2026-11-03 (branch `v2/p0-s2-fix`, slice P0/S2-fix)

Addresses `docs/plans/reviews/p0_s2_kernel_review.md` (verdict REVISE). B1: the code-reference
scanner now requires a dotted module part for `module:attr` factories, so tagged model ids
(`mistral:latest`, `llama3:instruct`) and bare artifact names (`intent.onnx`) are accepted while
`pkg.module:Class`, `a.b.c:Klass` and pickle suffixes stay rejected. B2: the capability matrix carries
a status per `(adapter, capability)`; only pairs v1 code serves are non-`planned` (`sqlite` all
eight as `legacy` pending S3, `postgres` vector + lexical, `neo4j` graph without memberships); config
validation rejects `planned` pairs (`storage.catalog|ledger|sessions: pg`) and `community`/
`storage.communities` on a graph backend without memberships (`neo4j`). B3: plan rev 3 amends §3.1a,
§3.5 and §5 to the frozen code; `upgrade()` reports `lossy_upgrade` diagnostics for v1 settings v2
cannot carry and `config upgrade --strict` refuses them. Also fixed: page cap aligned with
`candidates` (over-limit is a `BackendError`), `SessionNotFound` typed error, the fake honours
`unknown_fields: reject` and gates non-filterable/undeclared predicates (`field_not_filterable`),
datetime `range` bounds report `unparseable_bound`/`timezone_missing`, unknown vector ids are a
`BackendError`, the vacuous USD `pricing_key` config check replaced by `ChatRole.price_key()`,
trace masking adds `highlights`, `title`, `snippet`, `content`, `question`.

Evidence level: **synthetic unit tests and the in-memory fake only.** Interpreter: repository
`.venv` (Python 3.13.5, `mcp` installed). `pytest -q -m 'not external'`: **169 passed, 3 deselected**
(S2 in the same interpreter: 156 passed, 3 deselected; the earlier 154/2 figure was the anaconda
interpreter without `mcp`). `ruff check src tests` passed. Schema files unchanged (no `Config` field
changed; drift check green).


## Decision agent, typed scope, answer contract, review stage, selection split — 2026-11-03 (branch `v2/phase-0`, slice P0/S3)

Implemented (plan §3.7, §3.7.1, §3.7.2, §3.7.3, §3.9, §3.9a, §3.9b): `decision/{intent,base,
values,scope,rules,classifier,llm,cascade}.py`, `selection/{types,collapse,limits,union,ordering,
diversity,pins,packing}.py`, `generation/contract.py`, `pipeline/review.py`,
`core/config_decision.py` (`stage_policies`), `core/config_selection.py` (ordering kinds, `packing`),
regenerated `docs/config.schema.json`.

Evidence level: **synthetic unit, contract and parity tests against the in-memory fake and
monkeypatched model calls only; no real service, no model call, no production outcome.**
`pytest -m 'not external'`: 300 passed, 3 deselected (previously 156 passed). The 144 new tests
cover: every row of the resolution table (enum, observed-value, `exists`, cap-0 id fields, caller
filters applied as given with `zero_match`, numeric/boolean parse, range bounds incl. offset policy,
half-open `date_interval` on datetime and date fields executed on the fake, `missing`, non-filterable,
group/sort flags, every measure rule with the "never substituted by count" check); every row of the
fail-policy table (pre-pass never fails; rules/classifier/llm/cascade standalone and as cascade
stages with default and configured outcomes; `raw_all_arms` carries one flag per aggregate intent
and `decision_fallback: true`); classifier artifact rules (pickle, `.joblib`, dotted path,
`module:attr`, sha256 mismatch, missing file, missing/invalid runtime, label not an arm, default off);
the LLM planner with a monkeypatched `JsonModel.call` (observed values only for listable fields under
the cap, compiled scope executed exactly on the fake, nine failure categories, total-as-count refused,
budget reserve/commit and trace event); the answer contract (count/exhaustive-list/absence against
exact or exhaustive `ScopeResult`s, engine-injected constraints, generator cannot set them, violation
handling both ways); the selection stages (membership preserved under all seven orderings, explicit
`max_documents` cut, collapse/limits/merge semantics, diversity with short-group fallback, pins,
packing fairness, min-per-record omission, configured field priority beating `details`); exact
parity of `reciprocal_rank`/`per_arm`/`rrf`/`minmax`/`zscore` with `fusion.fuse` on fixed inputs
with and without weights and caps; the review stage with scripted reviewers (both v1 agentic test
intents, all six stop reasons, re-plan through the rules agent, `raw_all_arms` re-plan refused, one
vote per record per arm across hops, masked follow-up queries, default `JsonModelReviewer`); and
the nine NL-count probe shapes from the HybridQA diagnostic reproduced as synthetic requests through
the rules agent and a `rules → llm` cascade whose planner is scripted to fabricate (invented
identifier, unobserved value, total-as-count, non-filterable qualifier, offset-less calendar bound,
unbound topic, absence of an unobserved value, earliest without a time basis, non-existent table):
every shape abstains under `fail_closed` and yields a raw plan with non-empty unsupported
constraints under `raw_all_arms`, and the contract refuses a scripted count answer in both modes
(zero numbers in the returned answer). Exactly resolvable shapes still produce exact counts, sums,
first-seen and lists on the fake. `ruff check src tests` passed.

Not demonstrated: any of this wired into the engine (the arms/engine slice consumes
`DecisionAgent.decide`, `selection.select`, `AnswerContract.enforce` and `ReviewStage.run`); a real
planner or reviewer model; a real classifier runtime (none ships); intent grammar coverage beyond the
synthetic English questions in the tests; retrieval quality of any ordering (parity, not relevance,
was checked).

## Storage and arms on the contracts, write path with policies, neutral profiles — 2026-11-03 (branch `v2/phase-0`, slice P0/S4)

Implemented (plan §3.5 sqlite row, §3.8, §3.12 relocated level + dedupe, §3.13 as needed, §3.14,
§3.14.1, Phase 0 rows "sqlite adapter on contracts", "Five arms as Components", "Ingestion write
path on contracts + per-projection validation", "Neutral prompts + profiles + voice option"):
`backends/sqlite/` (eight facets), `backends/_scope.py`, `backends/extras.py`,
`testing/backend_conformance.py` + `tests/conformance/`, `ingestion/{write,policy,release}.py`,
`extraction/` package (v2 `BuiltinExtractor`, `relocated` grounding, `dedupe.py`), `communities/`,
`arms/` (five components), `engine.py` on config v2 with `Engine(config, components=[...])`,
`profiles/` (`neutral`, `support_example`, neutral prompts, voices), `core/request.py`.

Evidence level: **synthetic unit, contract and conformance tests only; no real service, no model
call, no production outcome.** `pytest -m 'not external'`: 229 passed, 3 deselected (the external
postgres/neo4j tests are additionally skip-marked: those adapters are not on the v2 contracts yet),
previously 156 passed. `ruff check src tests` passed. `tests/smoke_wheel.py` (CLI ingest/search/
answer/serve/MCP over a v1 JSON config upgraded in memory) passed in the same interpreter.

What the tests show:
- Conformance kit v0 (7 capability checks + cache + protocol check) passes identically on the memory
  fake, sqlite (numpy) and sqlite (faiss): scope pushdown with no out-of-scope hit, determinism,
  phrase semantics under the `phrase` flag, stable non-overlapping pagination, generation isolation,
  refresh visibility, catalog reads bounded by requested ids, typed scope (count, count_distinct,
  sum, first_seen, bucket, group_by, half-open date intervals, naive bounds reported as unsupported,
  ACL denominators, listable-only values), fact-only traversal with provenance, cache-key prefix and
  TTL, ledger ordering and raw replies, session expiry.
- Write path (memory and sqlite): every projection validated before activation; a failed embedding
  stage or a projection count mismatch keeps the old generation active, removes the staged generation
  and records a failure manifest; metadata violations fail closed by category; delete/update rebuild
  with ledger reuse; fingerprint drift refused unless `replace` + `rebuild`.
- Policies: `strict` aborts on the first rejected unit (raw reply still kept); `quarantine` keeps the
  document in catalog/lexical/dense/structured, excludes all of its extraction outputs from graph and
  communities and records `coverage.extraction` by category; `partial` refuses activation without the
  flag, marks the stopped stage incomplete with processed/remaining/first unprocessed unit, replays
  earlier successes from the ledger, and arms depending on that stage refuse unless
  `pipeline.allow_incomplete_arms` (results then flagged `incomplete`).
- S1 follow-ups: five rows inheriting the same title collapse to one `co_occurrence` edge with five
  occurrences (never traversed); a quote found in another field of the same record grounds at the
  `relocated` level with verbatim field offsets; a quote found nowhere still fails closed.
- Arms: each arm reproduces the alpha's `test_core.py` behaviours on the v2 engine (filters and
  exclusions applied inside every arm before selection, document dedup, grounded traversal, community
  membership flagged as non-causal, typed aggregates with exactness and denominator); the 30 alpha
  tests are replaced by `tests/contract/test_engine_support_example.py` under the `support_example`
  profile (same edge cases; `test_relative_config_paths` is covered by `test_config_v2.py`).
- Domain decoupling: support, research-note and collection-record corpora run through one neutral
  engine; custom fields are chunked, retrieved and packed; custom context keys reach planner and
  generator and unknown keys are rejected; built-in prompts contain no domain-authority vocabulary;
  renaming source labels leaves hits, scores and arm pools identical; a field named `details` never
  outranks a configured field; `generation.voice` appends a register instruction only when set.

Not demonstrated: ranking quality or relevance (tiny synthetic fixtures); postgres/neo4j on the
contracts; real provider calls; the Phase 0 external evidence re-run over frozen cohorts (owner
decision pending); any production outcome. Lexical ranking parity across engines is not claimed
(FTS5 bm25 vs the fake's overlap score differ by design).

## Engine integration: harness end to end — 2026-11-03 (branch `v2/phase-0`, slice P0/S5)

Implemented (plan §2.2 request path, §3.7–§3.9b wiring, §3.15 parity, Phase 0 acceptance):
`engine.py` now runs `RunContext` → intent pre-pass → decision agent (`rules` | `classifier` |
`llm` | `cascade`, fail policy from config) → arms as components → `selection.select` →
`selection.pack` → optional `ReviewStage` → generation → `AnswerContract` with engine-populated
`unsupported_constraints` and `coverage`. The v1 planner/fusion/agentic code paths and the typed
stubs for `best_arm_rank`/`stable_interleave` are gone (`fusion.fuse` stays as the parity oracle).
`models.AnswerResult`/`SearchResult` gained additive fields; `mcp.py` exposes typed filters,
`exclude_ids` and the typed aggregate with a start-up parity check (`serving/parity.py`); HTTP adds
`/v2/search` and `/v2/answer`.

Evidence level: **synthetic unit, contract and parity tests only; no real service, no real model
call, no production outcome.** `pytest -m 'not external'`: 429 passed, 3 deselected (previously
386). `ruff check src tests` passed. CLI smoke in the external workspace over `examples/local.yaml`
(v1 file upgraded in memory): `doctor`, `ingest`, `search --request examples/aggregate.json`
(first_seen aggregate next to retrieval hits), `answer "how many cases are there"` (abstains with
`cases: field_not_filterable`; no number).

What the tests show (`tests/contract/test_engine_harness.py`, `tests/parity/test_request_parity.py`,
ported `tests/contract/test_engine_support_example.py` and `test_domains.py`):
- The nine NL-count probe shapes through `Engine.answer`, for `rules` and `llm` (fabricating
  planner reply) under `fail_closed` and `raw_all_arms`, with a generator fake that would state "7
  matching records" as a count claim: every answer is `insufficient_evidence`, contains no digit,
  carries non-empty `unsupported_constraints`, and no aggregate was executed. Under `fail_closed`
  no arm runs and no model is called; under `raw_all_arms` the raw plan runs on the non-structured
  arms and the contract abstains with `claim_without_exact_scope` + `aggregate_intent_without_scope`.
  Prose numerals are not screened (Phase 2 `exact_set` guard), so this evidence covers claims the
  generator declares, not free text.
- An exactly resolvable count ("How many records have status open?") still counts through the
  engine with `exactness: exact` and the count claim passes; an explicit typed `group_count`
  aggregate runs next to retrieval.
- Review through the engine: a scripted reviewer injected with `Engine(config, components=[...])`
  re-plans a follow-up through the `rules` agent with the request filters unchanged, merges new
  hits through selection (`sufficient` after one hop); a follow-up that is an unbound count stops
  the loop with `replan_abstained`. The two v1 agentic tests run on the integrated engine with the
  `llm` agent (`no_actionable_gap`, `hop_limit`, follow-up hits merged, citations checked).
- `ordering: reciprocal_rank` reproduces the alpha's `per_arm` order and scores (including source
  weights) on the shipped `examples/local.yaml` for five queries; every ordering kind preserves the
  union and `max_documents` is the only cut.
- Packing: `fair_share` truncates every record to its share with none omitted and none under
  `min_per_record`; `greedy` starves the tail; `selection.packing.field_priority` beats
  `generation.evidence_fields`, which beats the document's own order.
- `AnswerResult.coverage` is `None` for a strict complete generation and carries policy, document
  counts, incomplete stages and the manifest note under `partial`.
- Parity: the same request (typed filters, `exclude_ids`, record context, typed aggregate) through
  the Python API, `/search`, `/v2/search` and the MCP `search` tool returns the same hit order and
  aggregate; both MCP tool schemas match `SearchRequest` field-for-field, and the checker detects the
  alpha's `dict[str, str]` filters and a dropped `exclude_ids`.
- `support_example` fixtures: outcome unchanged (same hits, aggregates, evidence order, prompts); a
  generator that cites unsupplied evidence now abstains with `unknown_evidence` recorded (or raises
  under `on_violation: fail`) instead of a bare `ValueError`.

Not demonstrated: ranking quality; the classifier agent through the engine (interface only, no
runtime); `cascade` through the engine (unit-tested in `test_count_probes.py`); streaming;
`capabilities`/`list_values`/`lookup_record` MCP tools; real providers; any production outcome.

## MongoDB adapter on the contracts — 2026-11-03 (Phase 1, branch `worktree-agent-abd12a2ec9a5ab040`)

Implemented (plan §3.5 `mongodb` row, Phase 1 "mongodb adapter"): `backends/mongodb/` with the
`catalog`, `lexical`, `records`, `cache`, `ledger` and `session` facets, a generation registry with
an atomic pointer document, predicate pushdown, a `mongodb` pip extra and the engine wiring for
`kind: mongodb`. `graph` is not declared; `vector` stays `planned` (Atlas-only; see below).

Evidence level: **real-service conformance against a disposable community-edition MongoDB container,
synthetic fixtures only; no model call, no production outcome.**

- Service: `mongo:7.0.16` (`sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a`),
  `mongod` reports `db version v7.0.16`; started alone from `tests/services/docker-compose.yml`
  (`up -d --wait mongo`, healthy; `wait_ready.py --only mongo`: ready on the first probe), pymongo
  4.19.0, the repository's `.venv` interpreter with `PYTHONPATH=src`.
- `pytest -q -m external -k mongo`: **16 passed** (`tests/conformance/test_mongodb_conformance.py`),
  1.3 s. The kit's `check_catalog`, `check_lexical`, `check_records`, `check_ledger`,
  `check_session`, `check_cache` and `run_all` all passed unchanged against the server: request-ordered
  gets bounded by the requested ids, stable non-overlapping pagination, scope pushdown on
  `iter_scope` and on lexical search (predicates reach the server through a `$lookup` into the
  generation's documents), phrase never matching non-contiguous tokens, `must`/`must_not`, refresh
  visibility, generation isolation and typed errors for unknown generations, typed scope results
  identical to sqlite/memory (count, casefold eq, ACL denominators, zero-match flag, first_seen,
  group_by, sum, count_distinct, month buckets, half-open date intervals, naive bounds reported as
  `timezone_missing`, missing, range, negated in, listable-only values), cache-key prefix and TTL
  expiry, ledger ordering and raw replies, session expiry. Additional checks: generation-scoped
  collection names and drop-on-delete, `values` cardinality cap failing closed, TTL indexes present,
  registry pointer switched only by `activate` with a recorded failure leaving it untouched, write
  lease refused while held, `from_config` reading the URI from the environment, an unreachable server
  surfacing as `BackendError(mongodb, generation)`.
- Unit (no server): 12 tests in `tests/unit/test_mongodb_adapter.py` for query compilation, text
  search strings, naming, paging and driver-error mapping through a thin fake client (the fake is
  evidence of error mapping only). `pytest -q -m 'not external'`: 398 passed (386 before).
- Teardown: only the `mongo` service was stopped and removed (`docker compose ... rm -sf mongo`);
  another agent's OpenSearch container of the same compose project was left running; `docker ps`
  before and after otherwise identical.

Known limits, stated rather than claimed away:
- MongoDB text scores are term-frequency `textScore` values, not BM25; the conformance kit checks
  membership, scope, determinism and paging, never score parity. Phrase matching is MongoDB's
  case-insensitive contiguous match on the raw text, which is at least as strict as token adjacency.
- Vector search is **not** implemented or tested: it exists only on MongoDB Atlas, which this
  repository cannot exercise; the capability stays `planned` and configuration naming it is rejected.
- Measures and groups are computed over the matched rows with the shared `_scope` helpers (the rows
  are fetched anyway for `matched_ids`); only predicates, counts, `exists` and `values` run server-side.
- The engine wiring for `kind: mongodb` is constructed but no end-to-end engine run on MongoDB was
  performed in this slice; the write path on an external catalog is the multi-backend write-path slice.

## S3/S4 review fixes — 2026-11-03 (branch `v2/p0-review-fix`, base `9f26b88`)

Addresses `docs/plans/reviews/p0_s3_s4_review.md` (verdict REVISE). B1: temporal comparisons are
instant-based on both adapters (`backends/_scope.py::instant/temporal_key`; sqlite stores declared
and inferred temporal values as UTC instants with a sortable canonical text; `assume_utc` naive
values and bounds read as UTC; a `date` field compares calendar dates and truncates datetime bounds
to their UTC date; the resolver accepts a datetime bound on a date field by the same rule); the
conformance kit now carries an `assume_utc` field with naive stored values and a `date` field with
datetime bounds and asserts identical `ScopeResult`s on memory, sqlite and sqlite+faiss. B2:
`source` is an identity key (bypasses the schema while undeclared); `unknown_fields: reject` ingests
a declared schema and rejects a genuinely undeclared key as `metadata.unknown_field`. B3: documents
a stopped stage never reached are `extraction: not_processed:<stage>` with `graph: incomplete`,
listed in `coverage.stages.<stage>.documents_not_processed`, never `accepted`; the previous test
that codified `accepted` is corrected. B4: the intent grammar fires `total`/`list`/`absence`/
`earliest`/`latest` only next to a structured target (canonical unit, generic record words, declared
field names, `time_basis` labels) supplied by the decision agent from `Capabilities`; twelve plain
questions containing those words are retrieval decisions and the nine HybridQA probe shapes still
abstain or flag. Non-blocking: community arm checks entity/report overlap before any catalog paging
(N1); `PathEdge.occurrences` surfaces deduplicated occurrences (N2); `review.enabled` with
`max_hops: 1` is a configuration error (N4); sqlite `date_buckets: true`, `filter_pushdown: false`
(N5); the new generation is protected and the previous one released on activation (N6); the
rejected-document chunk counter no longer double counts (N7); a vendor product name removed from
the example profile (N9); the two execution-log inaccuracies corrected (N10).

Evidence level: **synthetic unit, contract and conformance tests only.** `pytest -q -m 'not
external'` with `PYTHONPATH=src`: **418 passed, 3 deselected** (386 before; +32); `tests/conformance`
31 passed for memory, sqlite, sqlite_faiss; `ruff check src tests` clean; schema files unchanged
(drift check green). Not demonstrated: N3/N8/N11/N12 (engine-wiring and contract-surface items left
to the integration slice), any real service or model call.
## OpenSearch adapter on the frozen contracts — 2026-11-03 (branch `worktree-agent-a1c794b75ac6aff4c`, slice P1/opensearch)

Implemented (plan §3.5 `opensearch` row, Phase 1 "opensearch adapter" row, §6.1 real-service tier):
`backends/opensearch/` (`adapter.py`, `transport.py`, `catalog.py`, `search.py`),
`backends/_lucene_common.py`, matrix rows (`catalog`, `lexical`, `vector`, `records` = `contracts`),
engine wiring for `kind: opensearch`, pyproject extra `opensearch`, services workflow install step,
`tests/unit/test_opensearch_backend.py`, `tests/conformance/` parametrised over `opensearch` and
`opensearch_hnsw` (`external`), `tests/services/test_opensearch_adapter.py`.

Evidence level: **real-service conformance on one containerised OpenSearch, plus fake-transport unit
tests; no model call, no production outcome, no relevance claim.**

| Item | Value |
|---|---|
| Image (pinned tag) | `opensearchproject/opensearch:2.19.1` |
| Resolved digest | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` |
| Server as reported by `GET /` | distribution `opensearch`, version `2.19.1`, Lucene `9.12.1`, security plugin disabled, single node, 512 MiB heap |
| Client | `opensearch-py` 3.2.0 (extra pins `>=2.8,<4`), Python 3.13.5 (`.venv`), Docker 29.0.4, Compose v2.40.3 |
| Stack | `tests/services/docker-compose.yml`, service `opensearch` only, host port 19200 (loopback); another agent's `mongo` service of the same compose project was running, so only the `opensearch` service was started and removed by this slice |

Results (interpreter `.venv`, `-o pythonpath=src`, pytest temp under `~/su_mutliarm_tests/claude/`):

| Suite | Command | Result | Evidence |
|---|---|---|---|
| Conformance kit on the real service | `pytest -q -m external tests/conformance -k opensearch` | **12 passed, 8 skipped, 0 failed** (2 adapter parametrisations × {protocols, catalog, lexical, vector, records, run_all}; the 8 skips are graph/cache/ledger/session, which the adapter does not declare) | real service |
| Adapter real-service checks | `pytest -q -m external tests/services/test_opensearch_adapter.py` | **6 passed**: server identity; generation-named indices, alias activation atomic, a staged second generation never touches the alias, failed-build deletion leaves the alias, delete refused for the served generation, old generation stays until deleted; mapping-version mismatch / foreign index / knn-method mismatch are typed errors; highlights and field filter; unreachable server is `server unavailable`; HNSW + exact rescore returns the same ids and scores (5 d.p.) as exact search on 60 random 8-d vectors under a document allow-list; records scan cap (`max_matched`) is a typed error | real service |
| Fake-transport unit tests | `pytest -q tests/unit/test_opensearch_backend.py` | **14 passed**: construction rules and `from_config` env handling; index naming and dataset refusal; typed field layout; every predicate op's DSL incl. casefold, negate, numeric/date/string ranges, half-open intervals; scope pushdown shapes; lexical templates never parse syntax; bulk shapes for documents, chunks and vectors; bulk item failure typed; vector validation; HNSW opt-in request sequence; lexical paging; error mapping (no reply, 404 generation, 400 refused, mapping mismatch, foreign index); index creation mapping; probe; atomic alias request and delete guards; records requests | synthetic |
| Whole non-external suite | `pytest -q -m 'not external'` | **401 passed, 29 deselected** (previously 386 passed, 3 deselected in the same interpreter; +14 unit, +1 config case; the 26 extra deselections are the new `external` conformance parametrisations) | synthetic |
| `ruff check src tests` | | passed | — |
| `docs/config.schema.json` | regenerated from `core.config.Config` | no drift (`Config` unchanged; `LuceneBackend` already existed) | — |

Observed engine facts the adapter relies on (probed by hand against the same container before
implementation and then by the suites): `cosineSimilarity` in a `script_score` works on
`knn_vector` fields without `index.knn` (exact mode builds no graph); the `knn` query with a
`filter` on a Lucene-engine HNSW field honours the filter (its raw score is `(1 + cos) / 2`, which is
why ANN scores are never returned without the exact rescore); a `terms` aggregation ordered by `_key`
on a one-shard index returns exact, ordered keys; `search_after` on the `id` keyword gives stable
pages; a missing index is `404 index_not_found_exception`.

Not demonstrated: behaviour on Elasticsearch (separate adapter, not written); the `services` GitHub
job (repository not published); the multi-backend write path calling `activate(gen)` (the engine's
release step still activates through the file registry only; the alias switch is exercised by the
adapter tests, not by `ingest`); any lexical ranking parity with FTS5 or PostgreSQL (none is claimed;
the cross-engine agreement report of plan §3.5 is not produced yet); HNSW recall on anything larger
than the 60-vector fixture; the plan's `cardinality`/`date_histogram` aggregations (deliberately not
used, see the execution log).


## Model providers and capability registry — 2026-11-03 (Phase 1, slice P1/providers)

`providers/` package: `ChatProvider` protocol with `anthropic` (Messages API; native structured output
as one forced tool; `cache_control` on system and tool blocks), `openai` (Chat Completions; native =
strict `json_schema`; passive caching read from `prompt_tokens_details.cached_tokens`),
`openai_compatible` (request hints only as declared) and `none`; embeddings `hash`,
`sentence_transformers`, `openai`, `openai_compatible`; per-`(provider, model-pattern)` capability
registry consulted by `ChatRole` validation and again at provider construction; normalised usage
(input incl. cached and cache-write tokens, output, reasoning) committed to `Budget` (calls, tokens,
USD via the price table) and emitted as masked `model_call` trace events.

What the tests show (all with `httpx.MockTransport`, no network): request shapes per provider and
mode (tool definition + forced `tool_choice`, `cache_control` placement, strict schema conversion,
`json_object`/`json_schema` hints only where declared, output-token parameter names,
`reasoning_effort` only on declared models, no `Authorization` header for a keyless local server);
usage parsing incl. Anthropic cache read/write and OpenAI cached tokens; 401/403/429/5xx/529/400,
timeouts, connection errors and non-JSON bodies mapped to typed errors with exactly one request (no
retry) and no response body or key in the message; truncation, refusal and a native reply without
its tool call are failures; schema-invalid replies are `SchemaViolation` with the raw reply; a
strict-incompatible schema and a missing key fail before any request or reservation; budget
exhaustion (calls, output ceiling) blocks the request; USD accounting with cached/cache-write rates;
planner failures map to `timeout`/`schema_violation`/`provider_error`; config rejects every
undeclared `native`/`json_mode`/`json_schema`/`controlled`/`reasoning_effort` pair with a key path.

Evidence level: **contract-mocked only for Anthropic and OpenAI** — the wire formats are taken from
the providers' published API documentation and were not exercised against the paid services (no
paid call authorised, assumed defaults #3 and #14); in particular, which JSON-schema keywords
OpenAI strict mode accepts for our schemas, and Anthropic's handling of `$defs` in `input_schema`,
are unverified. The local OpenAI-compatible smoke (`tests/services/test_local_llm.py`, `external`)
was **not run** in this session (`SU_TEST_LOCAL_LLM_URL` unset; no model server was started).
`pytest -q -m 'not external'` with `PYTHONPATH=src`: **558 passed, 20 deselected** (473 before;
+85: `tests/contract/test_providers.py`, `tests/unit/test_provider_capabilities.py`); ruff clean;
`docs/config.schema.json` regenerated.

## Elasticsearch adapter on the frozen contracts — 2026-11-03 (branch `worktree-agent-adc476aefbf7e9366`, slice P1/elasticsearch)

Implemented (plan §3.5 `elasticsearch` row, Phase 1 row "elasticsearch adapter"):
`backends/elasticsearch/` built on the OpenSearch adapter classes over `backends/_lucene_common.py`
(the OpenSearch classes were made engine-parametrised: typed errors carry `self.kind`, and four
override hooks exist for the transport, the index body, the vector field mapping/score script and the
ANN candidate query). Elasticsearch-specific: official-client transport (`transport.py`), `probe`
(8.x only; OpenSearch and other majors refused), `dense_vector` fields (exact `index: false` +
`script_score` cosine by default; float `hnsw` opt-in with candidates from the top-level `knn`
search and the same exact rescore), no `index.knn` setting, and an alias-backed
`generation_registry()` (`registry.py`: manifests, failure records and a create-only ingestion lock
in `<prefix>__registry`; `activate` is the adapter's atomic `_aliases` switch; `current()` resolves
the `docs` alias). Engine dispatch for `kind: elasticsearch`, pip extra `elasticsearch`, capability
matrix rows `contracts` for `catalog`/`lexical`/`vector`/`records`. Shared-module change: the typed
`t.*` fields and temporal predicates now use `_scope.temporal_key` (naive stamps and date-only strings
are UTC instants, `date` fields compare calendar dates, temporal `eq` is instant equality), which the
kit's `assume_utc`/`date` checks require; it applies to OpenSearch too.

Evidence level: **real-service conformance on one throwaway container plus synthetic unit tests
with a fake transport; no model call, no production outcome, no load or recall measurement.**

Real service: `docker.elastic.co/elasticsearch/elasticsearch:8.17.4`
(`sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56`, the digest recorded by
the infrastructure entry above), started service-scoped (`up -d --wait elasticsearch`) from
`tests/services/docker-compose.yml` on Docker 29.0.4 / Compose v2.40.3, client `elasticsearch`
8.19.3, server version `8.17.4` confirmed by `probe`. Teardown stopped and removed only that
container; `docker ps` otherwise matched the pre-run state (the OpenSearch and MongoDB containers of
the same compose project were not running during this run). No `su_test_*` index remained.

`pytest -q -m external -k elasticsearch` (worktree source on `PYTHONPATH`, after the merge of
`v2/phase-0` at 74b8595): **16 passed, 8 skipped** in 3.7 s. Through `tests/conformance/conftest.py`
the kit runs over `elasticsearch` and `elasticsearch_hnsw`: protocol check (declared set ==
implemented set == `{catalog, lexical, vector, records}`), the four kit checks and `run_all` for
each (the 8 skips are the graph/cache/ledger/session checks the adapter does not declare, skipped
explicitly). `tests/conformance/test_elasticsearch_conformance.py` adds the server probe, generation
isolation + listing + delete, the registry alias switch with a failed build that writes its manifest
and failure record and is removed without the alias moving (and the alias target refusing deletion,
and the lock being exclusive across registry objects), and exact-vector parity with the memory fake
(same membership, order and cosine scores within 1e-5 on a seeded 4×8 fixture with an exclusion
scope). The kit's records check covers typed scope incl. casefold `eq`, ACL denominators, zero-match,
`first_seen`, `group_by`, `sum`, `count_distinct`, monthly `bucket`, half-open `date_interval`, naive
bounds reported `timezone_missing`, `assume_utc` instants across offsets, `date`-field truncation and
unparseable bounds, `missing`, `range`, negated `in`, `exists`, listable-only `values`; the lexical,
vector and catalog checks cover scope pushdown with no out-of-scope hit, determinism, phrase
contiguity under the `standard` analyzer, boolean `must`/`must_not`, stable non-overlapping
pagination, generation isolation, `refresh` visibility, bounded catalog reads, exact cosine order,
namespace isolation and the dimension/duplicate-id rejections.

Unit (`tests/unit/test_elasticsearch_backend.py`, fake transport, no server): 10 tests on the
Elasticsearch-specific parts (probe gate for 7.x/9.x/OpenSearch, `from_config` and the registry's
dataset requirement, index body without `index.knn`, `dense_vector` exact/HNSW mappings and the
mapping-mismatch error, exact cosine script with the field literal, `knn` candidates then exact
rescore, every typed error naming `elasticsearch`, the client transport's `ApiError`/connection
mapping and ndjson content type, the atomic alias actions with a failed build never issuing
`_aliases`, lock conflict and stale-lock takeover). `pytest -q -m 'not external'`: 498 passed on the
merged tree (`tests/unit/test_opensearch_backend.py` included, 84 passed across the four touched unit
modules), `ruff check src tests` clean, `docs/config.schema.json` unchanged by this slice.

Not demonstrated: the OpenSearch external suite after the shared-module temporal-key change (its
container was not running; its fake-transport unit tests pass); HNSW recall on anything larger than
the kit fixture; behaviour under ingestion load; multi-shard indices; Elasticsearch 9.x; secured or
TLS clusters; the `services` CI job on GitHub; an end-to-end `ingest` through `Engine` on this
backend (the engine now constructs it, but the write path's `activate` through
`generation_registry()` is exercised by the adapter tests, not by `ingest`). The lexical cross-engine
agreement report (plan §3.5) has not been produced.


## Neo4j graph adapter on the contracts — 2026-11-03 (Phase 1, branch `worktree-agent-abe51c291233d8741`)

Scope: `backends/neo4j/` (graph facet with memberships and communities, generation pointer node),
graph cases added to the conformance kit, engine wiring for `kind: neo4j`, `BaseArm.graph_scope`
(predicate resolution for document-id-only graph backends), write-path pointer activation/rollback;
v1 `adapters/neo4j.py` and its two v1 external tests removed.

Evidence level: **real-service** for the adapter and the engine's graph slot, against
`neo4j:5.26.0-community` (`sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5`,
server agent `Neo4j/5.26.0`, Bolt 5.8, Python driver 6.3.1) started alone from
`tests/services/docker-compose.yml` (`up -d --wait neo4j`, removed afterwards with `rm -sf neo4j`;
other containers of the project left running). Synthetic fixtures only.

- Conformance (`tests/conformance/test_neo4j_conformance.py`, 10 `external` tests, all passed):
  `check_graph` (now shared with memory, sqlite and sqlite+faiss, which also pass it): one- and
  two-hop `fact` traversal; co-occurrence never traversed by default (also at three hops) and
  traversed only when requested; edge kind, support, status, span offsets/text/match level, full
  provenance and every occurrence surfaced unchanged; direction, per-hop predicate filter, limit,
  unknown seed; hop bound `0` empty and `4, -1, "2", 2.0, True` refused with a typed error;
  eligibility as allow-list, deny-list and empty allow-list on edges, mentions and seed lookup;
  scope predicates refused (neo4j) or honoured (sqlite/memory), never ignored; memberships, mention
  attributes (canonical id, aliases), communities round-trip; dangling endpoint rejected with the
  graph unchanged; unknown generation typed. Plus: `MERGE` idempotency (re-load leaves paths,
  mentions and memberships identical), a rejected call rolls back entities written in the same call,
  generation isolation, pointer refused for an unloaded generation and left unchanged, pointer and
  `protect` both block `delete_generation`, namespace isolation, unreachable server and wrong
  password as `BackendError(neo4j, …)`.
- Engine (`tests/services/test_neo4j_engine.py`, 3 `external` tests, all passed; replaces the v1
  `test_neo4j_real` / `test_neo4j_explicit_multihop_relations`): graph and community arms on neo4j
  return the same documents and provenance as the sqlite graph facet for three queries, and for
  `source: case` filters (predicates resolved through the catalog); deletions visible after
  re-ingest with the pointer following the new generation; two-hop fact traversal over a bridge
  document and its exclusion cutting the path; a failing graph load and a failing `CURRENT` switch
  both leave the previous generation active and the pointer on it, with the failed load cleaned up.
- Non-external: `tests/unit/test_neo4j_adapter.py` (17, fake driver: only the fixed templates ever
  run and hostile values travel as parameters, fact template cannot match co-occurrence, hop/
  predicate refusals before any statement, constraint/unavailable/auth errors typed with rollback,
  activation/delete refusals, config) and `tests/contract/test_graph_backend_seams.py` (7, memory
  fakes: predicate resolution parity with native pushdown for graph and community arms, cap → typed
  error, pointer moves only on successful activation and is moved back or removed on failure).
  `pytest -q -m 'not external'`: **496 passed** (473 before at `783d821`); `ruff check src tests`
  clean; `docs/config.schema.json` regenerated (`backends.*.namespace` for neo4j).

Known limits, stated rather than claimed away:
- Neo4j evaluates eligibility only as document-id lists. With request filters or ACL predicates the
  arms page the catalog for the eligible ids first, so catalog reads for that step are bounded by the
  eligible set (cap 10000, then a typed error), not by the hits (principle 2.1.7 holds only for
  unfiltered requests on this backend).
- One write transaction per `upsert` call: atomic, but the whole generation's graph must fit the
  server's transaction memory. Very large loads would need a staged multi-transaction load behind the
  existing `state = loaded` marker; not implemented.
- Re-loading an identical mention/relation/membership updates it in place on neo4j, while sqlite and
  memory append a duplicate relation row; the conformance kit therefore does not assert duplicate
  behaviour.
- Ranking/relevance is not measured here; parity with sqlite on a tiny synthetic corpus is evidence of
  identical semantics, not of retrieval quality. No vector index on neo4j (Phase 5).

## PostgreSQL and generic SQL adapters on the contracts — 2026-11-03 (Phase 1, branch `worktree-agent-ac47e981ef8e1495f`, base `783d821`)

Implemented (plan §3.5 `postgres` and `sqlalchemy` rows, Phase 1): `backends/postgres/` (catalog,
FTS lexical, pgvector exact with opt-in HNSW behind a filter-recall gate, typed records with native
`double precision`/`timestamptz` value columns, cache, ledger with raw replies, sessions; one schema
per generation; registry with a `CURRENT` pointer row and an advisory write lock; graph `planned`)
and `backends/sql/` (config kind `sqlalchemy`; SQLAlchemy Core; catalog, records, exact cache,
ledger, sessions; lexical/vector `planned`); engine wiring for both kinds; pip extra `sql`.

Evidence level: **real-service conformance against a disposable PostgreSQL + pgvector container and
a real SQLite database, synthetic fixtures only; no model call, no production outcome.**

- Service: `pgvector/pgvector:0.8.0-pg17` (`sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87`),
  server `PostgreSQL 17.6`, extension `vector` 0.8.0; started alone from
  `tests/services/docker-compose.yml` (`up -d --wait postgres`, healthy; `wait_ready.py --only
  postgres`: ready on the first probe); psycopg 3.3.6, SQLAlchemy 2.0.39 (SQLite through the
  standard library driver, PostgreSQL through `postgresql+psycopg`).
- Conformance kit, postgres (`tests/conformance/test_postgres_conformance.py`, `external`): 23
  passed — protocols, `check_catalog`, `check_lexical`, `check_vector`, `check_records`,
  `check_ledger`, `check_session`, `check_cache`, `run_all` with exact vectors **and** `run_all` with
  `vector_index: hnsw`, plus: one schema per generation and `DROP SCHEMA` on delete (cache keys of
  the generation removed), digest schema names for long/odd datasets, analyzer recorded per
  generation (`pg_ts_english`; a `simple` generation keeps `simple` when read by an `english`
  backend; an unknown configuration fails the write with a typed error), stemming and stop-word
  behaviour, hostile tsquery characters treated as plain tokens, exact vector order and scores equal
  to a NumPy cosine computation on 60×16 random vectors under a predicate scope, unknown/mis-sized/
  non-finite vectors refused, `values` cardinality cap failing closed, `timestamptz` storage of an
  `assume_utc` value, byte-order paging, registry pointer switched only by `activate` with a recorded
  failure leaving it untouched, advisory lock refused while held, `from_config`, unreachable server
  as `BackendError(postgres, get)`.
- HNSW gate (synthetic 400×32 normal vectors, 32 sampled queries × {unfiltered, allow-list}):
  measured recall@10 of the forced index path = **1.0** over 64 query/filter pairs with the defaults
  (`m=16`, `ef_construction=64`, `ef_search=100`); with deliberately degenerate settings (`m=2`,
  `ef_construction=4`, `ef_search=1`) it measured 0.24 and `refresh` failed with the typed
  `filter recall ... < min_filter_recall` error (the test asserts the failure at a 1.0 floor). Scope
  is never widened under ANN. This is a synthetic smoke of the gate, not a recall benchmark.
- Conformance kit, generic SQL (`tests/conformance/test_sql_conformance.py`): 12 cases per dialect
  (protocols, `check_catalog`, `check_records`, `check_ledger`, `check_session`, `check_cache`,
  `run_all`, generation tables and isolation, cardinality cap, byte-order paging, registry pointer
  and lease incl. stale-lease takeover, ping/dialect) — **sql[sqlite] 12 passed** (runs on every
  push) and **sql[postgres] 12 passed** (`external`), plus 2 dialect-independent tests
  (`from_config`, unusable URLs / unreachable database / over-long ids as typed errors). Tested
  dialects are exactly SQLite and PostgreSQL; MySQL/MariaDB and others are untested.
- Engine end to end (`tests/test_external.py::test_postgres_engine_end_to_end`, `external`, passed):
  the support example corpus ingested with catalog, records, ledger, sessions, cache, dense and
  lexical on postgres (communities on local sqlite): manifest `complete`, all projections verified,
  dense and lexical hits, filter pushdown, delete-and-rebuild switches `CURRENT` while the old
  generation is retained. Non-external contract test: records/ledger/sessions/cache on `sqlalchemy`
  (SQLite) through the engine, count aggregate 4/4.
- Unit (no server): 13 tests in `tests/unit/test_postgres_adapter.py` for naming, SQL and tsquery
  compilation (bound parameters only), and driver-error mapping through a thin fake connection
  (typed errors with operation names, no retry, broken connection discarded, validation before any
  I/O, held lock refused) — evidence of error mapping only.
- Totals: `pytest -q -m 'not external'` **506 passed** (473 at `783d821`); `-m external` on the
  postgres/sql/engine files **36 passed, 2 skipped** (neo4j, v1-only); `ruff check src tests` clean;
  `docs/config.schema.json` regenerated (`backends.*.vector` for postgres). Teardown: only the
  `postgres` service was stopped and removed; no other container of the project was touched.

Known limits, stated rather than claimed away:
- FTS scores are `ts_rank_cd` cover density, not BM25, and analysis differs from FTS5/Lucene
  (stemming, stop words); only membership, scope, determinism, phrase/boolean semantics and paging
  are asserted. No cross-engine agreement report yet.
- Measures and groups are computed over the matched rows with the shared `_scope` helpers on both
  adapters (rows are fetched for `matched_ids` anyway); predicates, population counts, `exists` and
  `values` run in SQL. Plan wording "SQL aggregates, date buckets, top-N" is therefore met by
  identical results, not by SQL `GROUP BY`.
- Projections on a backend other than the catalog's (e.g. sqlite lexical over a postgres catalog)
  fail the build with a typed error; that is the multi-backend write-path slice.
- The generation manifest does not yet record the lexical `analyzer_id`; the adapter exposes it
  (`PostgresBackend.analyzer_id(gen)`).

## Declarative pipelines, harness through the engine, `exact_id` arm — 2026-11-03 (Phase 1, slice P1/pipeline-harness)

Scope: named pipelines (`pipeline` + `pipelines` overlays, `default_pipeline`) with per-request
`pipeline`/`mode` overrides checked against `request.pipelines`/`request.modes`; aggregated static
validation (`pipeline/validate.py`) and aggregated component validation at engine construction;
`doctor --probe` (backend `probe()` on sqlite, memory, mongodb, opensearch; registry-declared
provider pairs; no model calls); sessions through `Engine.search/answer(session_id=...)` with a
per-session budget, generation-switch re-validation and turn recording; memory never evidence;
read-only built-in tools (`search`, `get_document`, `list_values`, `aggregate`) and allowlisted,
ledger-audited actions (`ingest_delta`, `invalidate_cache`); the `exact_id` arm (alias map,
`alias_fields`, `id`-field `pattern`s, pins, unknown identifiers reported).

What the tests show (synthetic four-record fixture, in-memory and sqlite backends, fake generator
and planner that book one call each on the request budget):
`tests/unit/test_pipeline_validation.py` (13) — overlays merge and round-trip, a profile overlay
contributes pipelines (explicit config wins), one config with eight different faults reports all of
them with their key paths, the named-pipeline projection rule, allowlists naming unknown pipelines
or unbuildable modes, budget coherence errors and the review-bound warning, tools/actions limited to
built-ins, `exact_id` identifier-source and alias-field checks, `FieldSpec.pattern` rules.
`tests/contract/test_pipeline_harness.py` (16) — a request selects an allowed pipeline and a mode
(`rules` → `llm` switches the agent); unlisted pipeline/mode is `RequestRejected`; session turns
recorded with hashes and IDs, principal binding (`AccessDenied`), closed/unknown session
(`SessionNotFound`); a re-ingest between two turns pins each answer to its own generation, reports
`generation_changed {from, to}` and the dropped ID; a generator citing the previous turn's record
abstains with `unknown_evidence` and its payload has no memory; the session budget spans turns and
refuses the third; tools respect a tenant ACL (`get_document` hides another tenant's record,
`list_values` reports truncation, `aggregate` refuses undeclared fields); actions: not allowlisted →
denied and audited, `ingest_delta` audited `started` then `applied` with generation before/after,
duplicate key skipped, failure recorded, HTTP/MCP need `serving.actions.allow` and the `actions`
scope; `exact_id`: pre-pass routes `REC-1003` to an `exact_id` step, the match is pinned first,
aliases resolve, `REC-9999` is an unsupported constraint, a misspelt alias or wrong case matches
nothing (unless `casefold`), an alias outside the caller's ACL or an excluded id is reported unknown,
`pins: []` keeps the hit unpinned. `tests/contract/test_doctor_probe.py` (5) — probe report on
memory and sqlite with every `JsonModel` entry point patched to fail if called; a failing probe is
reported (`probe: failed`); CLI `doctor --probe` on sqlite; sessions persist in the sqlite store
across engine instances and the CLI `session`/`search --session`/`tool`/`action` commands match the
Python API; the action audit is read back from the ledger by a new engine. MongoDB `probe()` is
covered with the fake client in `tests/unit/test_mongodb_adapter.py` (success and typed failure).

Evidence level: **synthetic / contract-mocked only.** No real service was started for this slice:
the MongoDB and OpenSearch probes were not exercised against containers, no model provider was
called, and nothing here measures retrieval quality. `pytest -q -m 'not external'` with
`PYTHONPATH=<worktree>/src`: **607 passed, 46 deselected** (573 before; +34); ruff clean;
`docs/config.schema.json` regenerated with `su_multi_arm.core.schema.write_all`.
`tests/test_protocol.py` now starts the MCP subprocess with `PYTHONPATH` pointing at the package
under test, so a worktree run no longer imports an editable install from another checkout.

### Merge with `v2/phase-0` at `16a3a17` — 2026-11-03
After the merge, `doctor --probe` calls `probe()` on all eight backend kinds (postgres, sqlalchemy,
neo4j delegate to their `ping()`). Evidence: delegation unit-tested with a stub only; no container
probe run in this session. `pytest -m 'not external'`: 675 passed, 116 deselected; ruff clean.

## Multi-backend write path — 2026-11-03 (Phase 1, slice P1/multi-backend-write)

Scope: each generation-scoped projection (catalog, records, lexical, vector, graph, communities) on
any backend that serves it; scope mirrors on secondary holders; per-target validation; activation
in a defined order with compensation; per-request generation pinning with refusal on disagreement;
manifest with every backend's generation identity and lexical analyzer id; `delete_generation`
across backends. Closes the two postgres follow-ups listed just above (projections off the
catalog's backend; analyzer id in the manifest).

Evidence levels:

- **Synthetic, in-process (every push):** `tests/contract/test_multi_backend_write.py` (20 tests) on
  memory + sqlite combinations through `Engine.ingest/search/answer`: two mixed placements return
  arm results identical (documents, chunk ids, provenance, order) to a single-backend reference of the
  same kinds, unfiltered and with a request filter pushed down on the mirrors, plus identical
  `group_count` aggregates; manifest contents (activation order, roles, mechanisms, identities,
  `analyzer_id` `python_word`/`fts5_unicode61`, verified mirror projections, secondary registry
  pointers and manifests, no generation data on the ledger/session/cache backend); records written
  to both `storage.records` and the structured arm's backend; activation order (every secondary
  before `CURRENT`); injected activation failure on a lexical holder, a graph holder and the
  authoritative `CURRENT` (moved pointers restored in reverse, `CURRENT` unchanged, staged generation
  removed everywhere, reads still served from the old generation); first-build failure removes every
  moved pointer; a failed compensation is recorded, its cleanup refused and readers refused with
  `GenerationDisagreement`; a secondary registry switched or removed by hand refuses `search` and
  `answer`; `delete_generation` refusals (CURRENT, named by a secondary pointer, protected — nothing
  deleted) and deletion on every holder; `ConfigError` for a lexical arm on a backend that cannot
  take a mirror; registry `deactivate` on file, memory and SQLAlchemy-on-SQLite registries.
  `tests/unit/test_opensearch_backend.py` +1 (fake transport: `activate_generation`,
  `deactivate_generation` removes only aliases still naming the generation, `generation_identity`).
  Non-external suite 638 → 659 passed; ruff clean; `Config` unchanged (schema not regenerated).
- **Real service (this machine, Docker):** `tests/services/test_multi_backend_engine.py` (1 test,
  `external`): catalog, records, dense and runtime stores on PostgreSQL, lexical on OpenSearch, graph
  and communities on Neo4j through `Engine` — manifest identities (`pg_ts_english` schema,
  OpenSearch indices and `lucene_standard`, Neo4j key), hits from every arm, request filter pushed
  down on the OpenSearch mirror and resolved for Neo4j, injected `CURRENT` failure with the alias and
  the pointer node restored (`left_for_cleanup` empty, failed generation absent from both), alias
  switched back by hand → `GenerationDisagreement`, `delete_generation` refusals and deletion on all
  three services. **1 passed.** The existing external suites for these three services were re-run
  with the change: 68 passed, 8 skipped (undeclared capabilities), elasticsearch/mongodb/local-LLM
  deselected. Images: `pgvector/pgvector:0.8.0-pg17` (`sha256:40b40496…`),
  `opensearchproject/opensearch:2.19.1` (`sha256:72fe2fc8…`), `neo4j:5.26.0-community`
  (`sha256:5a015e53…`); the three containers were started for the run and removed afterwards.

Not verified here: MongoDB and Elasticsearch as secondary holders against a real server (their
registry `deactivate` and the Elasticsearch registry `current_generation` are unit-trivial but not
service-tested); concurrency between two writers on different secondary backends (one write lock,
held on the catalog's registry); crash between two pointer switches (process-failure isolation only;
a crash leaves a disagreement that readers refuse until the next build or an operator realigns it).

## Serving parity and streaming — 2026-11-03 (Phase 1, slice P1/serving)

Scope: `Engine.answer_stream` (typed events, provisional token deltas, contract on the final answer,
cancellation), SSE streaming for the anthropic/openai/openai_compatible providers, the HTTP surface
(`/v2/answer/stream`, sessions, tools, actions, values, capabilities, `/health`, `/ready`), the
`PrincipalResolver` hook, MCP stdio and streamable HTTP with the tool set mirroring HTTP, session
binding and the start-up schema-drift check.

What the tests show (synthetic four-record corpus; memory and sqlite backends; the generator is a
loopback OpenAI-compatible fake, `testing/provider_fakes.py::FakeChatServer`, serving JSON and SSE):
`tests/contract/test_streaming_providers.py` (18) — OpenAI/OpenAI-compatible/Anthropic event streams
(text, Anthropic tool-use `input_json_delta`) give the same parsed reply and usage as the
non-streamed call; `length`/`max_tokens`, a missing `finish_reason`/`[DONE]`/`message_stop`, garbage
chunks, Anthropic `error` events (overloaded, rate limit) and deltas for unknown blocks are typed
failures and still recorded; a JSON reply to a stream request is refused (no fallback); a role
without `capabilities.streaming` fails before any request; `provider: none` never streams; a
cancelling delta consumer closes the call (`error_category: cancelled`); SSE parsing (comments,
multi-line data, CRLF). `tests/contract/test_answer_stream.py` (9) — event order `decision →
arm_result → evidence → token… → contract → done` with contiguous sequence numbers; the `done`
result equals `Engine.answer` (timings/ids removed); no deltas for a non-streaming role or without a
generator; a generator that streams a fabricated count claim → tokens, then `contract: abstained`
with `tokens_superseded`, final answer abstains; request problems raise before any event; cancel
after the first token → terminal `error: cancelled`, budget cancelled, the fake server sees the
connection closed before the reply ended; provider unreachable → `error: provider_connection`;
streamed turns recorded on the session. `tests/contract/test_serving_http.py` (9) — bearer
resolver (401/200, unset token variable fails closed), a custom header resolver with scopes (403)
and a tenant ACL applied to search and `/v2/values`; `/v2/answer` and `/answer` equal the Python
result; SSE names/ids/order and the final result equal `/v2/answer`; heartbeat comments while the
model is slow; error events carry a generic message; a raw-ASGI client disconnect (ASGI spec 2.4,
where Starlette itself would not listen) cancels the run, its budget and the provider stream;
session create/get/delete with principal binding (403/404); tools (enabled list, 404 for a disabled
tool, 422 for invalid input, 403 without scope); actions applied/duplicate/denied and audited;
values (`listable` only, truncation, `page_size_max`); `/ready` 200, 503 when a backend probe
raises (probe text not exposed), 503 without an active generation.
`tests/parity/test_serving_parity.py` (1) — one `SearchRequest` through the Python API,
`answer_stream`, HTTP, HTTP SSE, an MCP **stdio subprocess** and an MCP **streamable-HTTP** server
(uvicorn on loopback) yields identical `AnswerResult`s (and identical `SearchResult`s over both MCP
transports); MCP progress notifications carry the stream events in order with tokens; each MCP
session is bound to its own engine session holding both turns. `tests/parity/test_request_parity.py`
also checks every MCP tool mirror against its input model.

Evidence level: **synthetic / contract-mocked transports.** HTTP, SSE and both MCP transports ran for
real on loopback; the model is a fake, Anthropic/OpenAI streaming is checked against mocked event
streams in the documented shapes only (no paid provider called; owner decision #14), and nothing here
measures answer quality. Not tested: MCP client-side request cancellation (the handler's `finally`
cancels the stream, but no test sends `notifications/cancelled`), an in-flight provider read blocked
before its first byte (bounded by the role timeout, not interruptible), OAuth adapters (outside core).
`pytest -q -m 'not external'`: **712 passed, 116 deselected** (675 before; +37); ruff clean;
`docs/config.schema.json` regenerated with `su_multi_arm.core.schema.write_all` (no drift: `Config`
unchanged — `capabilities.streaming` already existed).

## MongoDB instant-based temporal fields (conformance fix) — 2026-11-03 (branch `v2/phase-0`, slice P1/mongodb-fix)

The MongoDB adapter was written before the S3/S4 review fix B1 (instant-based temporal scope) and
its external suite was not re-run afterwards: `tests/conformance/test_mongodb_conformance.py::
test_capability[records]` and `::test_run_all` failed with `assume_utc: naive stored values compare
as UTC instants`. `backends/mongodb/_db.py` now follows the shared `_scope` storage rule like
sqlite/postgres/sql/Lucene: typed rows classify temporal strings with `_scope.instant` (aware stamps
converted to UTC, naive `assume_utc` stamps and date-only strings read as UTC) and store the canonical
UTC ISO text plus epoch seconds; range bounds and temporal `eq`/`in` on typed records go through
`_scope.temporal_key` with the field spec (instant equality across offsets; `date` fields truncate
bounds to the UTC date and match equality on the UTC-day window); a non-temporal value against a
temporal field never matches; text bounds compare only string rows (temporal vs non-temporal is
incomparable, as in `_scope.compare`). Document scope (no schema) compares instants without date
truncation, as on the other adapters.

Evidence level: **real-service conformance on the existing throwaway test container plus a
driver-free unit test; no production outcome.** Service `mongo:7.0.16` (container
`su-multi-arm-tests-mongo-1` of the test compose project, digest as recorded above), pymongo 4.19.0.
`pytest -q -m external tests/conformance/test_mongodb_conformance.py`: **16 passed** (both previously
failing checks included). New unit test
`tests/unit/test_mongodb_adapter.py::test_temporal_fields_compare_as_instants_like_the_shared_scope_rules`
evaluates the compiled filters over the stored typed rows with a minimal in-test evaluator of the
emitted MongoDB operator subset and asserts parity with `_scope.matches` (the memory reference) on
the kit's `assume_utc`/`require_offset`/`date` records (13 predicates); it fails against the previous
`_db.py`. `pytest -q -m 'not external'`: **734 passed, 117 deselected** (733 before; +1); ruff clean;
no `Config` change. The container was removed afterwards (`compose rm -sf mongo`).

Other adapters checked for the same gap: sqlite, memory, postgres and generic SQL use the shared
`_scope` helpers and were written or re-run after B1; opensearch shares `_lucene_common`
(`temporal_key`) and its suite was re-run with the multi-backend write path; elasticsearch was
re-verified after B1; neo4j holds no records. No other adapter was found with the gap.

## Release-review fixes for 2.0.0a1 — 2026-11-03 (branch `v2/phase-0`, slice a1-fix)

Fixes for the release review (`docs/plans/reviews/a1_release_review.md`) B1–B6 (README excluded) and
N1–N5, N13. B1: `record_context.record_id` is resolved through the caller's ACL-scoped catalog read;
an out-of-scope or unknown id is dropped from the planner and generator payloads, traced
(`record_context.used: false`) and reported as `value_not_observed`. B2: a re-planned review hop that
starts with a structured step no longer crashes (`engine.answer`, `/v2/answer`, `/v2/answer/stream`).
B3: a non-empty `pipeline.guards` / `pipelines.*.guards` is a `ConfigError` (`not_executed`). B4:
origin-service and vendor names were removed from tracked files, and `docs/BUSINESS_SUMMARY.md` and
`docs/MIGRATION.md` were replaced by `docs/ADOPTING.md`. B5: version `2.0.0a1` in pyproject,
`__version__`, the HTTP app and `--version`; extras `openai`, `anthropic` (empty), `local`, `all`. B6:
`examples/v2/{local,mixed_backends,hosted_models}.yaml`, CONFIGURATION/PLAN/VALIDATION headers. N1:
SQL-family, Mongo and memory record scans fetch at most `max_matched + 1` rows and refuse above the
cap with a typed `BackendError`. N2: a scope with a dropped predicate is `bounded`, and the contract
never treats one as exact. N3: `opensearch` as `storage.catalog` is a validation error
(`catalog.registry` flag). N4: HTTP 503 for backend, generation and index-state errors, 502 for
provider errors, 499 for cancellation. N5: the `llm` decision agent re-raises package errors
(`RunCancelled`, `BackendError`) instead of mapping them to `provider_error`.

Evidence level: **synthetic unit, contract and conformance tests with fakes and the
memory/sqlite/SQLAlchemy-on-SQLite adapters; no real-service run.** The N1 cap and N2 exactness checks
were added to `testing/backend_conformance.py::check_records`. They ran on memory, sqlite,
sqlite_faiss and sqlalchemy-on-sqlite. They were **not** run against the PostgreSQL, MongoDB,
OpenSearch or Elasticsearch containers in this slice. That run belongs to the consolidated
release-commit run (B7). `pytest -q -m 'not external'`: **758 passed, 117 deselected** (734 before).
Ruff is clean. The `Config` schema is unchanged (regenerated with `core.schema.write_all` and diffed).
The wheel builds as `su_multi_arm-2.0.0a1-py3-none-any.whl` and declares the new extras. The new
tests fail against the previous code (B1, B2 checked by stashing the engine change).

## Landing README, documentation site and generated capability tables — 2026-11-03 (branch `v2/phase-0`)

Evidence level: **synthetic, offline execution of every documented command**; no real-service or
hosted-provider run. Scope: `README.md`, `docs/{index,quickstart,serving,schema,backends,providers,contributing}.md`,
`mkdocs.yml`, `scripts/gen_capability_tables.py`, `scripts/mkdocs_hooks.py`, `.github/workflows/docs.yml`,
`CITATION.cff`, `assets/logo.svg`.

- Every bash, Python, YAML and JSON block in the README quickstart and serving sections (and the
  identical blocks in `docs/quickstart.md`, `docs/serving.md`, `docs/schema.md`) was executed
  verbatim in a fresh clone of `8f6dd56` with a fresh virtual environment in the external test
  workspace, with no `*_API_KEY` variable set: `pip install -e '.[dev]'`; the six CLI commands on
  `examples/v2/local.yaml`; the Python ingest/search/answer example (status `complete`, three hits
  with their arms, contract `passed` with generation disabled); `config validate` of the 20-line
  mixed-backend YAML (valid, no warnings, no database driver installed; no service contacted);
  `config schema`; `pip install -e '.[api]'` and `'.[mcp]'`; the HTTP block (`/v2/search` returned hits,
  `/v2/answer/stream` emitted `decision` through `done`); the MCP stdio client configuration (an MCP
  client launched exactly that command and called `search`); and the streamable-HTTP command with
  `YOUR_CONFIG.yaml` replaced by `examples/v2/local.yaml` plus the documented `serving.mcp` block.
  Substitutions: the clone URL pointed at a local path (the public repository was not used), and the
  server examples ran on loopback ports 8099/8100. The "runs fully offline" callout rests on this run.
- Backend and provider tables are generated from `backends/capabilities.py`, the provider registry and
  the embedder literal; `tests/unit/test_capability_tables.py` (5 tests) fails on drift, on a backend
  kind without an evidence mapping, and on a "real-service conformance" claim whose test file is not
  marked `external`. "Real-service conformance" in the tables means such a suite exists; the recorded
  runs are the dated entries above.
- `mkdocs build --strict` (mkdocs 1.6.1, mkdocs-material 9.8.0) completed with no warnings.
  `scripts/mkdocs_hooks.py` rewrites links that leave `docs/` (three `../examples/...` links in
  `CONFIGURATION.md`) to the repository on GitHub instead of relaxing link validation.
- Not verified: GitHub Actions `docs.yml` and the Pages deployment (repository not yet published), the
  CI and star badges, rendering on github.com (Mermaid was checked only as emitted `class="mermaid"`
  blocks), and `pip install "su-multi-arm[all]"` from an index (not published; marked "once published").

## 2.0.0a1 release evidence — 2026-11-03 (branch `v2/phase-0`, release review B7)

Evidence level: **real-service** for the adapters below (disposable local containers from
`tests/services/docker-compose.yml`, project `su-multi-arm-tests`, ports on 127.0.0.1 only, started,
used and removed with `down -v` in this session); **synthetic** corpus throughout.

Source under test: commit `8f6dd56` (the release-review fixes `0cbbbfc` plus its log entry). The
evidence commit that records this section adds only tests, a script and docs; no file under `src/`
changed between the run and that commit.

Images (`docker image inspect --format '{{index .RepoDigests 0}}'`):

| service | tag | digest |
|---|---|---|
| OpenSearch | `opensearchproject/opensearch:2.19.1` | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` |
| Elasticsearch | `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56` |
| PostgreSQL + pgvector | `pgvector/pgvector:0.8.0-pg17` | `sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87` |
| Neo4j | `neo4j:5.26.0-community` | `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5` |
| MongoDB | `mongo:7.0.16` | `sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a` |

`tests/services/wait_ready.py`: all five ready (Elasticsearch and OpenSearch cluster green, Mongo ping,
Neo4j Bolt 5.4, PostgreSQL accepting connections).

**Consolidated run** — `pytest -m external -p no:cacheprovider -rs` with every `SU_TEST_*` service
variable from `tests/services/README.md` set and `SU_TEST_LOCAL_LLM_URL` unset:
**100 passed, 17 skipped, 0 failed** (763 deselected), green on the first run and identical on a
second run with `-rA`. No code fix was needed.

| adapter / suite | passed | skipped |
|---|---|---|
| opensearch (kit, exact kNN) | 6 | 4 |
| opensearch_hnsw (kit) | 6 | 4 |
| elasticsearch (kit, exact kNN) | 6 | 4 |
| elasticsearch_hnsw (kit) | 6 | 4 |
| elasticsearch extra (`test_elasticsearch_conformance.py`) | 4 | 0 |
| opensearch adapter (`tests/services/test_opensearch_adapter.py`) | 6 | 0 |
| postgres (`test_postgres_conformance.py`, incl. `run_all` exact and HNSW) | 23 | 0 |
| sqlalchemy on postgres (`test_sql_conformance.py[postgres]`) | 12 | 0 |
| mongodb (`test_mongodb_conformance.py`) | 16 | 0 |
| neo4j (`test_neo4j_conformance.py`) | 10 | 0 |
| neo4j engine (`tests/services/test_neo4j_engine.py`) | 3 | 0 |
| multi-backend engine (`tests/services/test_multi_backend_engine.py`) | 1 | 0 |
| postgres engine (`tests/test_external.py`) | 1 | 0 |
| local model (`tests/services/test_local_llm.py`) | 0 | 1 |

Skip reasons: the kit skips capabilities an adapter does not declare — OpenSearch and Elasticsearch
(both kNN modes) do not declare `graph`, `ledger`, `session` or `cache` (16); the local-model test
skips because `SU_TEST_LOCAL_LLM_URL` was deliberately unset in this run (1, see below).

The release-review N1/N2 conformance additions in `check_records` (a dropped predicate makes the result
`bounded`; a scope above `max_matched` is a typed `BackendError`, a scope at the cap answers exactly)
passed on postgres, sqlalchemy-on-postgres, mongodb, opensearch and elasticsearch (both kNN modes) in
this run; previously they had run only on memory, sqlite and sqlalchemy-on-sqlite.

**Local-model provider smoke** (run separately by the coordinator on 2026-11-03, not in this session):
`tests/services/test_local_llm.py` 1 passed against `qwen2.5:1.5b` served through an
OpenAI-compatible endpoint on a local GPU host. A small model was chosen because that host was nearly
full; this shows the provider wire contract works against a real local server, not answer quality.

**Lexical cross-engine agreement** (informational, plan §3.5; full table in
[LEXICAL_AGREEMENT.md](LEXICAL_AGREEMENT.md)): `scripts/lexical_agreement.py` indexed the same 19 chunks
(7 shipped example documents) into sqlite (`fts5_unicode61`), postgres (`pg_ts_english`), opensearch and
elasticsearch (`lucene_standard`) and mongodb (`mongo_text_english`), and ran 23 fixed queries (15
terms, 4 phrase, 4 boolean). Zero query errors on any engine. The complete match sets were identical
on all 23 queries within each analyzer family (sqlite / opensearch / elasticsearch, unstemmed; postgres
/ mongodb, English-stemmed) and on 18/23 across families (membership Jaccard 0.915); the five
differing queries are stemming cases (`certificates`, `expired`, `applying`, phrase `effective date`,
boolean `certificate -expired`). Mean Kendall tau on shared top-10 items ranges from 0.135
(sqlite / postgres) to 1.000 (opensearch / elasticsearch). On a 7-document fixture these numbers
describe analyzer behaviour only.

Still mocked or unmeasured: hosted Anthropic and OpenAI providers (HTTP contract fixtures only; no paid
request was made); relevance or answer quality on any non-synthetic corpus; scaling and concurrency.

Local suite at the evidence commit: `pytest -m 'not external'` 766 passed (includes the docs agent's
uncommitted capability-table tests in the working tree). Before this slice the same tree gave 762
passed and 1 failed: the tracked-file denylist test matched its own `DENYLIST` line once the test
file was committed (it had passed while untracked); the test now excludes its own file.

