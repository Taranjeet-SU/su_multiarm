## Built-in prompt guide and seven small planner, contract and rendering fixes — 2026-11-19

Evidence level: **synthetic / contract-mocked tests, a wheel and sdist build, and an offline smoke test
of the built wheel.** Synthetic fixtures only, in-memory backends, mocked model transport, no live
service and no paid model call from this repository's tests. The contributor also measured the changes
against a real model and a real OpenSearch index on a private question set; those questions, answers
and figures are not part of this repository and are not repeated here. Nothing in the tests measures
relevance or production latency.

- `tests/unit/test_role_temperature.py` (6): the optional `temperature` on a chat role is passed to
  OpenAI-compatible providers, refused for Anthropic and together with `reasoning_effort`, bounded 0 to 2.
- `tests/unit/test_intent_thresholds.py` (10): "more than N", "at least N", "at most N", "between A and B"
  next to a structured target are aggregate questions; the same words without a target stay retrieval.
- `tests/unit/test_share_percent_rendering.py` (3): a single share renders its percentage next to the
  fraction; other measures render as before.
- `tests/unit/test_negation_preposition.py` (7): a negation cue may be followed by a preposition
  ("not from X", "not in X"); a preposition alone is not a negation.
- `tests/unit/test_intent_first_created.py` (9): "the first/last X record created, opened, filed ..." is
  an earliest/latest question; other uses of "first" stay retrieval questions.
- `tests/unit/test_bound_range.py` (6): a datetime or date bound that cannot be read in UTC is refused as
  `unparseable_bound`; ordinary bounds still parse.
- `tests/unit/test_typed_planner.py`: the two recorded planner request hashes are re-recorded for one
  intended change, the longer built-in planner prompt (about 22 KB more in both modes); the wire schema is
  unchanged. `tests/contract/test_domains.py` still passes, so the new prompt text uses no domain words.

Full run: 4,476 passed, 255 skipped, 0 failed; `ruff check src tests` clean. `python -m build` from a clean
export of the commit produced the wheel and the sdist; the README quickstart (ingest, search, an aggregate
request, `doctor`) ran from the built wheel on `examples/v2/local.yaml` with hash embeddings and returned
normal output.

Not verified: relevance on real corpora (not measured in this repository); the effect of the longer
planner prompt on token cost and latency; behaviour of the guide with log intents and the unified
collection; models other than the one used for the private measurement.

## Legacy QA adapter pre-cutover hardening — 2026-11-09

Evidence level: **synthetic / contract-mocked only.** Synthetic corpus (`tests/compat_support.py`),
in-memory and SQLite backends, hash embedder, loopback `FakeChatServer` generator, FastAPI
`TestClient` and loopback uvicorn servers for the disconnect and starvation checks. No live service,
no private store, no paid model call ($0). Nothing here measures relevance or production latency.

- `tests/contract/test_legacy_qa_hardening.py` (25): expired session replaced (200, `restarted`)
  and expired rows swept; adapter bug → logged 500 with request id on `/api/qa` and `/api/search`;
  enrich schema violation → 502; failure after `done` → terminal `error`; abandoned `POST /api/qa`
  stops the provider stream and logs 499 with `usd`; deadline → 504 on both answer routes; `/health`,
  `/ready`, `/api/datasets`, `/api/models` < 3 s with 42 answers in flight (all 42 answered);
  admission 503 + `Retry-After`; `record_context` measured in UTF-8 bytes, refusals name the limit;
  `/ready` 503 on a missing key variable, failed warm-up, exhausted day cap; planner failure (dead
  planner endpoint) → warning, `compat.planner_failure`, `insufficient_evidence`, log fields;
  synthetic bearer token / `password=` absent from `/api/qa` (answer, citations, panels), the stream
  and `/api/search`; hidden records removed everywhere and an answer that read one withheld;
  clusters `total_cases`; search notice consistency; blank `searchString` 422; request log
  `forwarded_for`/`user_agent`/`request_id`/`trace_id`.
- `tests/unit/test_legacy_qa_hardening_units.py` (23): surrogate-pair decoding at every chunking,
  lone surrogates → U+FFFD, encodable SSE; provisional text never releases part of a credential or an
  unterminated key block; `cases` panel from segment hits; measured `rerank_ms`; planner-failure
  envelope; content-free stream failure log line; stale spend reservations released and audited
  (day and total ledgers, late commit, refused reservation); SQL panel rows given as mappings.
- `tests/contract/test_index_adopt.py` (4): a copied SQLite store is refused, adopted (dry run first),
  served and audited; differing recipes, a wrong builder and another dataset are refused; imported
  file digests verified; CLI exit codes.
- `tests/contract/test_mcp_unknown_arguments.py` (4): unknown MCP arguments refused and named;
  advertised schemas `additionalProperties: false`.
- Golden index fingerprints of the shipped examples unchanged (`test_index_fingerprint_golden.py`).

Full `-m 'not external'` run: 3,634 passed, 57 skipped (baseline at `1348a6d`: 3,578 passed, 57
skipped). The external QA harness of 2026-11-09 (20 tests): 17 pass; the two answer-contract strip
tests are out of scope (another change set), and the relocation test expects a plain copy to serve
without the explicit `index adopt` step this change requires.

Not verified: behaviour against the live gateway, nginx or the private store; real provider
disconnect timing; masking coverage beyond the generic credential patterns.

## Legacy QA compatibility layer, paged listings, per-request evidence budget — 2026-11-08

Evidence levels, kept separate:

- **Synthetic / contract-mocked (repository).** `tests/unit/test_legacy_qa_units.py` (12),
  `tests/contract/test_legacy_qa_compat.py` (18, including a replay of 19 synthetic request bodies
  in the legacy shapes, `tests/fixtures/legacy_qa/requests.json`) and
  `tests/contract/test_search_page.py` (9) on a 12-record synthetic corpus with the in-memory
  backend and the loopback `FakeChatServer`. They pin: the legacy envelope keys/types, `[id]` →
  `[n]` renumbering with equal numbering in the stream's `evidence`, the final `citations` and the
  markers; relative/absolute link policy; SSE order `plan → evidence → token* → final`, decoded
  (non-JSON) provisional tokens, terminal `error` with public text, client disconnect cancelling the
  run and the provider stream; legacy error shapes (empty question, unknown and retired dataset,
  unmapped agentic mode, provider failure 502 without provider detail); sessions per (dataset,
  client, value); record view sections, visibility rule (hidden = unknown, same 404) and credential
  redaction; candidate pagination/date sort/multi-value filters/facet counts/text budget;
  `filtered_corpus` facets exact; per-request evidence budget refused unless enabled and above the
  maximum; fail-closed start-up checks. `tests/unit tests/contract`: see the execution log entry.
- **Real service, private staging (sanitised aggregates only).** The adapter served one private
  support-knowledge dataset (a copy of the 2026-11-04 parity store: 1,709 records, bge-m3 imported vectors)
  with real models (planner `claude-sonnet-5-5`, generator `claude-haiku-5-5`) on loopback, behind
  an unmodified copy of the earlier web UI and a copy of the MCP gateway (own credentials, no
  provider keys). Results: the UI copy answered 3/3 questions in headless Chromium (one with the
  agentic toggle; 0 page errors; citations listed and clickable; the retired dataset and an empty
  question return their legacy errors); the gateway copy's own test suite passed 81/81 (mocked) and
  7/7 staging checks against the adapter (health, mapping of captured adapter bodies, retired dataset
  as a typed 422, OAuth token, MCP `tools/list` equal to the declared tools, REST `answer` with a
  session header, REST `search`, MCP `answer`); one streamed answer had identical streamed and final
  text and equal numbering.
- **Structural side-by-side, not a quality claim.** 10 questions sampled from the legacy service's
  own request log were replayed through the adapter (the legacy service was not called; its recorded
  answers were compared). 10/10 HTTP 200; answer contract passed 5, stripped 5; 1 planner abstention
  with no evidence; median latency 21.3 s vs 16.5 s recorded; median distinct cited markers 5.5 vs
  4.5. No relevance or correctness was judged; the legacy service used extra arms (case segments,
  communities, KG graph, SQL) the package store does not have.
- Spend (real service): US$0.42 measured by the package's `serving.answers` ledger (38 model calls),
  under a US$2.00 cap with reservations before every call.

Known gaps (not closed here): case-segment and community evidence, KG v2/graph node export, LLM
`enrich` summaries, search guidance/facts/refusal notice, session-cache answers, reranker parity,
the earlier UI's record page renders only the retired dataset's id scheme, and `/health` is per
adapter (both gateway clients report "up" while one dataset is retired).

## Declared collection policy and long-term episode archive — 2026-11-08

Evidence level: **synthetic only** (unit and contract tests with synthetic keyword families,
synthetic source aggregations and the sqlite backends). No source, deployment or real service was
touched; nothing here measures a production archive, its size or query latency at scale.

`tests/unit tests/contract`: **3,063 passed, 1 skipped, 1 failed** (before the slice: 3,023
passed, 1 skipped, 1 failed). The failure is the pre-existing docs denylist hit in
`docs/plans/unified_su_platform.md` (`test_release_identity`). `test_episode_summaries` runs and
passes now that the `patterns` extra (`google-re2`) is installed in the local venv. Ruff clean;
`docs/config.schema.json` regenerated; wheel and sdist 2.0.0a3 built in the external workspace.

What the new tests show (`tests/unit/test_log_policy.py` 34, `tests/contract/test_episode_archive_serving.py` 6):

- Defaults: a `logs` block without `collection` keeps the earlier behaviour (no window, no keyword
  files, archive off, 31-day episode bound). Fifteen invalid policies are refused with a named
  reason (window across midnight or empty, unreachable day close, bad pins, duplicate files,
  epoch without files, archive without files or without `derived: forever`, long ranges without
  the archive, archive under staging or under `<partitions>/history`, archive with the compose
  history tier, unknown keys).
- Recipe pins: a keyword file edited after pinning is refused; re-pinning without
  `effective_from` refuses to archive a day of the new recipe next to archived days of the old one;
  snapshots captured under the old recipe are refused by the new pin; with `effective_from` after
  the last archived day both epochs coexist, each day labelled with its recipe, and a day before
  `effective_from` is refused. A long-range answer spanning two epochs or a gap record says so.
- Append-only day shards: closing day N+1 leaves the `rows` and `history_fields` of days <= N
  byte-identical and writes exactly day N+1's rows; re-closing identical content is a no-op,
  different content for an archived day and archive `gc` are refused.
- Day boundary: a snapshot window crossing midnight, another day's snapshots, an incomplete day
  (before `day_close_delay_minutes`) and an empty day (gap record required) are refused.
- Long range: 90 synthetic days (two tenants, one model service, three causes) archived as 90
  shards and served by one generation holding the rule catalog and the 10-day live window;
  `incident_episodes` over the 90 days returns all 120 episodes with issue, category, first/last
  failure, occurrences and one cited example each, no missing days, tenant filters apply, and
  `log_top` sums the 90 days' episode cells exactly. A range over `max_window_days` is refused.
  An episode crossing midnight is one row (`crosses_day_boundary`, both evidence ids).
- Retention: after four publications `retention run` deletes the two generations beyond
  `keep_generations`; the archive keeps all 93 days and rows, `verify_policy` is clean, and the
  oldest day is still served. Staging import closes complete days only, inside the heavy window,
  and deletes their staged copies.
- Verify detects: a closed day with neither rollup nor gap record (clean after an explicit gap
  record), an extra generation, a raw day older than `raw_days`, an archived day deleted from the
  archive (hash chain), a serving generation missing an archived day, an undeleted staging copy and
  a re-hashed keyword file.
- Schedule: the emitted daily heavy job starts at the later of the window start and the day-close
  delay and its `RuntimeMaxSec` ends it inside the window (without a window: before midnight UTC);
  the refresh job runs every `summary_refresh_minutes`.

Limitations: archive reads are EAV scans over every shard for predicates on `first_hour`/
`last_hour` (no pushdown), measured only at 90 synthetic days; search, the graph and the community
arm cover the live window; the collector that produces full-day snapshots is not part of the
package.

## Incident-summary recovery follow-up — 2026-11-07

A later authorized status check found that capture progress and publication had diverged: saved
captures continued while the serving generation remained at the last successful publication.
OpenSearch rejected a large bulk request with HTTP 413. A malformed source index path also raised
an uncaught read-only transport refusal, aborting some batches after earlier scopes were saved.
These failures invalidate any assumption that every completed capture was already visible in the UI.

The adapter now splits oversized bulk requests on UTF-8 byte boundaries while preserving whole
action/source pairs and operation ordering. Every subresponse is checked; later item failures remain
fatal. The private collector preflights index paths against the existing read-only allowlist, records
unsupported paths as deferred, and persists fatal run status with already completed scope counts.
Capture and publication have independent supervised services and locks. Completed source work
triggers publication asynchronously; a separate publication timer retries without requiring a successful
source batch. The capture service preserves its own failure status for systemd. No source transport permission was broadened.

Synthetic backend checks: **41 passed, 2 skipped** (optional dependency cases); source-summary
contract checks: **24 passed**; private scheduler/admission checks: **6 passed**. Full Ruff and
core wheel/sdist build pass. The recovery generation activated successfully at approximately 11:03 UTC. It contains **2,901
captured scopes**, **27,859,059 keyword matches**, **8,687,776 specific-rule matches**, **19,171,283
unclassified candidates**, **23,598 retained examples**, and **36,699 observed sessions**. Source
aggregation counted 415,388,348 source-window documents without downloading them all. The generation
has 109,730 canonical documents and 335,467 chunks; all 40,469 intended vectors were written.
The API was verified against the active generation. Its default three-calendar-day query covers
2,681 scopes and 26,446,678 keyword matches, since the rolling collector also has partial coverage
of the preceding UTC day. Overview/tool execution completed in 0.53 seconds and the representative
service/issue aggregation in 0.11 seconds. Live desktop/mobile Overview, Graph, service and outage
answer-table browser checks passed with no page errors/CSP failures and zero model calls.

At a subsequent 11:05 UTC check, saved collection had reached 3,081/5,802 scopes, 247 services and
56 tenants. Both independently supervised capture and publication services were running. One
unsupported source index path was explicitly deferred. First-pass completeness remains pending;
these counts are neither all-log extraction coverage nor measured incident accuracy.

Final delivery follow-up (2026-11-07): live desktop/mobile Overview, Graph, exact service/issue
question and the outage question all pass the extended browser check, with zero page errors/CSP
violations and zero model calls. Outage evidence uses both graph and community arms. Long citation
IDs now wrap inside the mobile page. The faster scheduler completed a third 50/50 batch in
193.62 seconds with no failures. Full-fleet completeness is still pending.

## Source-side incident-summary deployment — 2026-11-07

Evidence is separated into synthetic contract checks and a user-authorized private deployment.
No private source text, tenant names, endpoints or credentials are recorded here. Main public
checkout remained untouched; reviewed source changes are in the existing logs worktree, with test
artifacts and frozen build packaging in the owner's external test workspace.

- New synthetic summary/tool suites: **24 passed** (13 source invariants, 11 tool/HTTP/ACL/investigation checks).
  They cover repeat-count reconciliation, exclusive rule priority, missing/failed source responses,
  unknown recovery, tenant separation, safe keyword serialization, declared shared-rule membership,
  normal ACL filtering, Python/HTTP output parity and refusal of unavailable measures.
- Existing shared-error/outage compatibility suites: **16 passed**; pattern extractor contracts: **32 passed**. Earlier affected frozen-runtime
  surface checks passed **31 tests with 1 skip**; the optional MCP SDK was absent. Registry/schema
  and HTTP parity are covered; an actual MCP protocol handshake was not tested.
- UI: **101 passed**, TypeScript/Vite production build passes. Live desktop/mobile browser checks
  rendered 100 bounded overview rows and a nonempty client/service graph with 30 displayed groups;
  no page exceptions, CSP violations or root horizontal overflow. Evidence remains collapsed and
  unverified recovery is shown as Unknown. These checks do not measure incident detection accuracy.
- Full `ruff check src tests`, private collector/publisher lint and core/UI wheel+sdist builds pass.
  Config schema regenerated. Builds used the prior deployed core wheel plus an explicit twelve-file
  overlay, avoiding deployment of unrelated pending worktree changes.

Measured source pilot: **12 index/day scopes**, one complete calendar day, six source service
families. **399,584 keyword matches**, **56,505 specific-rule matches**, **343,079 unclassified
candidates**, **564 observed sessions**. **178 evidence hits** were downloaded using 125 read-only
requests, **13,585,391 response bytes**, in **99.08 seconds**. This is **99.955% fewer log documents**
than downloading every keyword match in those same scopes; it is not a measured byte or latency
saving versus a controlled baseline. Source aggregation counted 16,889,427 total time-window
messages without downloading them. No raw-scroll fallback and zero collector/extraction model calls.
Rule classification is not incident precision, and unclassified candidates are not proved false positives.

The isolated live dataset was published and activated separately from the prior dataset. The first
combined snapshot represented **416,878 keyword matches** with **282 retained examples**, **646
observed rule sessions**, **62 stored native association links**, and **12 declared rule/day
communities**. It covered only **28 captured index/day scopes** against a rolling inventory of
approximately **5,786**. Representative source evidence remained pending for 491 overlapping
sessions in the default calendar query. No recovery durations or full-fleet completeness were claimed.
The two original client/service failure-and-issue questions returned deterministic Markdown tables
from the live endpoint in 0.017 and 0.068 seconds, respectively, with no model calls. This validates
stored summary behavior, not the completeness or precision of the source incident rules.

The first automatic cycle completed 50/50 scheduled scopes in **237.95 seconds**, with 328
requests, 12,238,334 response bytes, 155,691 keyword matches, 41,893 specific-rule matches and
444 evidence hits, with no failures and zero model calls. The resulting snapshot contained
**572,569 keyword matches**, **724 retained examples**, and **1,426 observed episodes**, across
**78 captured scopes, 47 services and 10 tenants**. Its 63 service/rule aggregate counts and all
keyword counters matched independently read saved source snapshots. Comparison used no new
source requests. This is stored-count parity, not an independent incident-truth evaluation.

A second automatic cycle completed 50/50 scopes in **143.19 seconds**, with no failures.
The resulting snapshot had **617,957 keyword matches**, **117,701 specific-rule matches**,
**500,256 unclassified candidates**, **936 retained examples**, **1,537 observed sessions**,
and **21 cross-tenant shared-rule groups**, across **128 scopes, 77 services and 12 tenants**.
The calendar-day UI scope differed from the rolling collector window: 117 scopes and 580,777
keyword matches. All 88 service/rule counts and keyword counters in that calendar query matched
saved source snapshots. These counts must not be confused with full inventory completeness.

Large summary-mode investigations now disclose bounded selection rather than rejecting more than
500 overlapping episodes. Client/rule/day observed-session communities support the incident
retrieval path separately from cross-tenant shared-rule communities. Header-association extractor
versioning is included in build reuse keys, so changed graph recipes cannot reuse stale extractions.

The new collector/publisher timer is enabled; old bulk/context/extraction timers are disabled and
bulk wrapper guard markers prevent accidental resumption. Historical private data and the prior
runtime/config remain available for rollback. Full-fleet capture is incremental background work. Five synthetic private scheduler tests pass.
After measuring two batches, the private scheduler was changed to prefer uncaptured scopes,
reserve at most 10% of a batch for stale current-day refreshes, refresh closed days at most daily,
and pause 30 seconds between batches. All 128 recipe-verified capture states were preserved.
Zero-work cycles do not republish. A 6–10 hour first-pass ETA is an extrapolation, not a measured
completion result; source latency, new indices and retries can change it.

## Authorized deployment and representative evaluation — 2026-11-05

Follow-up on the owner's exact service-failure/issue question: synthetic independent event-plan
checks validate counts grouped by `(service, signature)`, supporting template IDs, a deterministic
three-column answer without model calls and preservation of the requested grouping when a model
proposes only service volume. Affected Python suites: 109 passed, followed by 44 scope/parity
checks passed. UI typecheck and 89 tests pass. The actual synthetic answer path passes desktop and
mobile browser checks: one table, Service/Error type/Error count columns, collapsed evidence,
no horizontal page overflow. Core/UI wheel and sdist builds pass. This is synthetic evidence;
the targeted deployed query is checked separately rather than rerunning the full question suite.
Final scope/gold checks after normalizing empty severity lists: 93 passed; Ruff and diff checks pass.
The follow-up was deployed as core wheel `179551c7a817609c93e028910f349abdd48aec0b6445ef6d7f1e42bd6d9b0f19`
and UI wheel `48ea5740d104b69a86827bf9fc617225754ea739571386c41b97afa3b2c79968`.
The exact live question returned in 0.10 seconds with zero model calls/spend: all 48 service/signature
counts matched the independently read stored severity rows, all templates/evidence IDs resolved,
and two unavailable days were disclosed. This validates stored-count parity, not completeness
against production source logs. Templates are global signature definitions rather than
service-specific event excerpts. Replaying that real response through the deployed browser UI
confirmed one three-column table, expansion to all 48 rows, citations, collapsed detail and zero
mobile page overflow. The other 20 evaluation patterns were not rerun.

The owner authorized deployment and replaced the exhaustive question sweep with representative
pattern checks. Core wheel SHA-256 `a55ccc83a5e4533aa6aea5a5e16ef8482070f16c823dc59274948ef7740c0751`
and UI wheel `eca2b097d8e54ff8fd2b069e7fd5893974d0d7e21bb093199ab7ccbae3aaaae9` were staged in
a separate environment, then deployed. The prior environment was retained for rollback.
Dependencies, indexed generation and spending limits were unchanged; no regex candidate promoted.

Staged and live readiness, UI asset identity, CSP, Host/Origin guards, unknown-service refusal and
stored graph checks passed. A LAN browser check confirmed Overview/Ask/Graph navigation, today's
default date, 23 stored graph nodes and 31 edges, with no page errors. One actual deployed answer
was replayed in the browser without another model request: its Markdown table, citation click,
desktop/mobile layout and zero page overflow passed. Its long supporting prose remains a usability
limitation. Private artifacts stay in `~/su_mutliarm_tests/codex/deploy-20261105/`.

The evaluation suite was reduced to 20 distinct patterns, with mappings to 1,764 generated and
60 historical questions. Equivalent entity substitutions are not executed repeatedly. Nine
distinct historical patterns are explicitly deferred; mapping a question to a related family
does not prove its answer correct. Passing patterns stop; only discriminating failures qualify
for a targeted follow-up. This is sampled validation, not a full-corpus correctness claim.
The 20-pattern baseline after deployment completed with six useful passes, seven safe refusals,
four partial answers and three failures; nine answers had Markdown tables. All returned citation
IDs resolved. Spend was $0.496507 across 25 model calls, without raising the daily cap. Failures
included an incomplete provider response, novelty answered as volume, and error rate answered as
volume. These results do not establish general answer correctness.

## Log answer presentation, scope guards and graph navigation — 2026-11-05

Evidence: **synthetic UI and Python tests, browser checks against a local synthetic engine**.
No private data, paid model calls or deployment changes. Work and artifacts are under
`~/su_mutliarm_tests/codex/answer-markdown/`.

- UI typecheck and full Vitest suite: **88 passed**. New cases cover Markdown tables, lists,
  headings, code, citation clicks, hostile HTML/URLs/images, multi-paragraph prose, structured rows,
  later-row columns, zero values, explicit row expansion and abstained/uncited evidence.
- Affected Python answer suites: **101 passed** (`test_rendered_answer_contract`,
  `test_logs_rendered_answers`, `test_answer_text_contract`, `test_contract_labelled_counts`).
  UI mount and theme contract suites: **41 passed**. `ruff check src tests` passes.
- `ui/e2e/answer_markdown.mjs`: built UI at **1360×1000 and 390×844**, synthetic checked-answer SSE
  with a multi-issue table; table/list/heading rendering, citation navigation, structured rows and
  no page overflow pass. No browser page errors, CSP violations or Markdown inline styles.
  Screenshots: `screenshots/answer-desktop.png` and `screenshots/answer-mobile.png` in the workspace.
  The extended browser check also passes today's date, the three main navigation entries and an
  automatically loaded actual stored graph from the synthetic gold engine (25 nodes, 45 edges;
  graph responses are not mocked). `screenshots/graph-desktop.png` records it.
- Production UI bundle builds, including asset hashes and runtime dependency notices. Core and UI
  **wheel and sdist builds pass**. The generated config schema includes the new bounded,
  opt-in `schema_repair_attempts` decision setting.
- Planner repair tests use a scripted provider: at most one repair, existing call/spend budget,
  original request preservation, filter override refusal, and content-free validation-path logging.
  Date/name scope tests include model-omitted filters, explicit date over a default window,
  inclusive ISO and same-month day ranges, unsupported dimensions, quoted unknown names, both
  requested severities and refusing a latest-event question answered by a rollup.
- Direct-tool regression tests cover unknown client/service names, mixed known/unknown service
  lists, a known client/service/date intersection, an unavailable future day and contradictory
  source count metadata. Missing coverage refuses rather than claiming zero. Comparable but
  inconsistent source counters produce bounded results and explicit provenance notes; no counter
  is guessed or rewritten. Affected scope/Ask/gold/parity suites: **101 passed, 1 skipped** (MCP
  dependency unavailable in that environment). This is synthetic tool parity, not a real MCP run.
- Deterministic API answer tables retain original scope/counting-basis text, escape untrusted
  cells, show explicit row-preview bounds and do not expand the generator's permitted numbers.
  Rendered-answer suites: **27 passed**.
- Full non-external Python suite: **2,549 passed, 99 skipped, 158 deselected** (135.15 seconds).
  After the final comparison-basis label/refusal and overlapping-severity counter refinements,
  affected scope, coverage, rendered-answer and gold suites: **114 passed**. Final Ruff and
  `git diff --check` pass. Comparison prose explicitly identifies parsed rows; a question explicitly
  requesting a source-count comparison is refused by that unsupported intent.
- Runtime dependency audit reports the pre-existing Zod 3 advisory `GHSA-mrhx-c7xc-m3w5`
  (high severity; the offered fix requires Zod 4). No existing dependency versions changed in this
  presentation fix, and no advisory was reported for the new Markdown dependencies. A Zod migration
  remains separate work; this is not a clean dependency-audit result.
- The generator prompt now requests readable Markdown and tables for multi-issue attention
  summaries. **Actual model adherence and live answer quality have not been measured.** The browser
  fixture supplies Markdown explicitly; deterministic structured rows need no model formatting.

### Separate read-only operational evaluation (baseline, before these changes)

Private artifacts are external under `~/su_mutliarm_tests/codex/elk-evaluation-20261105/`;
no private questions, corpora or names are included here. The suite contains six questions for
each of 238 services and 56 tenants (1,764), with 60 prior questions retained separately. Only
44 services and five tenants are indexed. **The generated suite has not been executed.** Twelve
additional representative live regression probes were run: zero produced Markdown tables;
manual review found one failed, seven wrong and four partial answers, none fully correct and
consumable. Spend was $0.087269; the service's daily limit was unchanged. These are baseline
failures, not a measurement of the local patch's model adherence.

296 read-only deterministic tool checks covered indexed and source-only entities plus combined
scopes. All 49 indexed entity rankings matched stored combined severity counters, but this does
not establish source accuracy: 20 of 302 unit-days had inconsistent same-basis severity counters.
Source-only entities incorrectly returned exact empty results on the baseline. The local guards
above reproduce and mitigate these classes; the upstream provenance issue remains unresolved.

Separately, five private regex candidates evaluated on 867,001 heads increased deterministic
parsed/noise coverage from 431,190 (49.73%) to 508,525 (58.65%), with zero prior classification
regressions. All five gates, 41 independent adversarial negatives and five wrong-service checks
passed. Synthetic extraction checks verified grounded spans and `emits` links, not causality.
Candidates are unpromoted; these figures do not establish incident-answer correctness. Reports
and candidate libraries remain external under `~/su_mutliarm_tests/codex/regex-coverage-20261105/`.

## Fallback overlay into the arms — 2026-11-04 (branch `v2-logs-overlay` from `4d5a652`; A2 L5/B7)

Evidence level: **contract** (synthetic overlay rows written with the fallback slice's
`OverlayStore` on one gold day; no model called).

- `tests/contract/test_logs_overlay_arms.py` (5 tests): 23 `provisional` heads (signature from a
  learned entry), 1 `model_line` head (no signature, model-extracted endpoint/method/status/latency)
  and 1 `parsed_filled` head (model-filled exception class) on day 9. The derived snapshot carries
  them as aggregate rows and the day's history shard as `extraction_source: model_assisted` rows
  (`applied_extraction == ["model_assisted"]`); signature answers include `payload.opaque` (23) and
  `unknown` (1) and state the model-assisted share; `extraction: deterministic` excludes them;
  deterministic per-service totals and the deterministic coverage are unchanged (no head counted
  twice), the model-assisted counts and `extracted_coverage` are separate and higher; the model
  endpoint reaches the 5xx ranking and the filled exception class the exception ranking; no graph
  edge comes from a model row (no `serves`/`raises` edge, no edge for the provisional signature).
- Found and fixed: a newly sealed overlay did not change the compose identity, so compose stayed
  `unchanged`; the identity now includes the window days' snapshot identities.
- `test_derived_archive` adjusted: a snapshot still holds no line-level rows (no head ids, reply
  shas or per-line values), but a model-extracted value can now appear as an aggregate dimension,
  like a deterministic endpoint template.
- Full suite: 2359 passed, 95 skipped (not external); ruff clean.

## History tier H1, lineage H2, read latency — 2026-11-04 (branch `v2-logs-h1` from `dacd166`; A2 rev 4 §4.8, §11.R4)

Evidence level: **synthetic / contract** on sqlite, **real-service (isolated)** for the history rows
on OpenSearch 3.9.0 (own compose project `su-l5-claude`, removed afterwards; synthetic gold data).
No paid call.

- Contract (`tests/contract/test_logs_history_tier.py`, 17 tests): with the tier, the generation holds
  only window kinds (125 documents for the 10-day gold) and references one day shard per served day;
  all 11 surface-parity questions give the same rows and rendering as without the tier; imported day
  rows equal the derived snapshot (signature lines per day, unit counts), including a **format-1**
  snapshot whose partition has expired (A1 kinds only, analytics `source_unavailable`); appending a
  day writes that day's rows only; a library change that alters no document reparses (delta) and
  writes 0 rows; a change of one day's documents writes one new shard, and the previous generation
  still reads its own shard (shard filter); gc keeps every shard a registry generation references
  and removes the rest; graph links cover the history with per-day weights from the shards; a
  supersede decision yields one `signature_lineage` row and answers fold the superseded signature
  into its successor; `signature_history` holds one row per signature and library identity.
- OpenSearch (`tests/services/test_history_tier_opensearch.py`, `external`): the history rows in a
  shard-tagged OpenSearch collection answer all 11 questions exactly like the sqlite tier; graph
  weights present; gc deletes a dead shard's rows by query (0 left).
- Day N+1 append (`scripts/bench_log_tools.py --days 180 --append`, external workspace): 180 pulled
  days (90,701 history rows, generation 125 documents), then day 181 pulled and composed: **504 rows
  written, all for day 181; every older shard unchanged**; 1.6 s (sqlite) / 12.9 s (OpenSearch
  history rows); peak RSS of the whole benchmark process 663 MiB / 786 MiB.
- Read latency, same questions with fixed 10-day windows, p95 over 20 calls, one process:

  | tool | sqlite 10 d | sqlite 180 d | OpenSearch 10 d | OpenSearch 180 d |
  |---|---:|---:|---:|---:|
  | log_top | 4.3 | 9.1 | 11.4 | 22.6 |
  | log_new | 2.2 | 2.0 | 2.9 | 2.1 |
  | log_affected | 2.2 | 4.6 | 8.0 | 9.7 |
  | log_trend | 3.6 | 8.1 | 8.1 | 13.7 |
  | log_compare | 2.3 | 4.3 | 7.5 | 15.0 |
  | log_tenants | 2.0 | 2.8 | 10.4 | 5.2 |
  | log_around | 2.7 | 3.7 | 6.1 | 8.4 |
  | log_endpoints | 2.7 | 5.7 | 8.4 | 13.6 |
  | log_signature | 2.8 | 3.6 | 6.3 | 5.6 |
  | log_coverage | 11.4 | 15.6 | 48.5 | 58.4 |
  | graph_neighbourhood | 11.3 | 36.3 | 35.1 | 61.2 |

  Every log tool is far under 200 ms and the graph neighbourhood under 500 ms at 180 days (before
  H1: `log_compare` 232 ms, `log_coverage` 433 ms). Ratio 180 d / 10 d: OpenSearch 0.5–2.0x for every
  tool (within 2x); sqlite 0.9–2.25x for the log tools (`log_top` 2.1x, `log_affected` 2.1x,
  `log_trend` 2.25x, `log_endpoints` 2.1x over 2x) and 3.2x for the graph neighbourhood. The gold
  generator's error ramp (svc-api logs `d` errors on day `d`) makes the last 10 days of the 180-day
  run hold more rows than the 10-day run's window, so part of that ratio is data volume, not
  history size; not separated in this run.
- Full suite: 2289 passed, 94 skipped (not external); ruff clean.

## Logs analytics profile, log intents and tools, MCP log tools, count semantics — 2026-11-04 (A2 L5a–L5d, M1, B2+B3; base `v2/logs` `106d4b0` merged with `fdb7c4a` and `42627b0`)

Evidence level: **synthetic / contract** (fake source, sqlite backend, hash embedder, scripted
planner; no model and no real service called). The gold set is the in-repo generator
`testing/fixtures/logs/gold` (10 days x 4 tenants x 6 services, planted structure; expected answers
computed from the generator's plan, never from the engine).

- Gold questions (`tests/contract/test_logs_gold_questions.py`): 66 cases over the 10 intents plus
  incidents, all equal to the plan: per-day top services by level (18), top error signatures per
  service (16), row-count order != sum order, FATAL tenants, exception classes, new signatures per
  day (9; first seen over all served history), affected tenants (per window and per day), signature
  template and examples, per-day trends with the missing day listed `source_unavailable` (never
  zero), hourly trend on the incident day, period comparisons (3), FATAL tenants on the
  `level_field` basis and the token-only service rendered "lines containing 'error'", the planted
  incident (the only one; 3 cells, exact), endpoint latency regression (max 904 ms), 5xx (20 lines),
  per-row p95 (sum of p95 refused, `measure_unsupported`), coverage equal to a recount; the capped
  unit is `approximate` with its sample method; endpoint ranking without a service is refused.
- Surface parity (`tests/contract/test_logs_surface_parity.py`): 11 plain-language questions give the
  same rows and rendering in `rules`, `llm` and `cascade` modes (planner never called) as
  `Engine.call_tool`; an llm-selected intent (scripted reply) equals the tool call; the HTTP tool route
  and in-process MCP are byte-identical to the tool call; an MCP stdio subprocess and streamable HTTP
  mounted at `/mcp` on the HTTP app return the same structured output and list the same tools;
  `Host: attacker.example` on `/mcp` is refused; all log tools carry `readOnlyHint: true`.
- Scope guard: kind-less, negated and multi-kind scopes refused with `intent_required` on the
  request aggregate, the Python, HTTP and MCP `aggregate` tool and an llm free scope; a single-kind
  scope passes. Single-tenant refusal in config validation, `Engine`, `create_app`, `create_mcp`.
- Graph (`test_logs_graph_and_communities.py`): every header-link edge's quote names both endpoints;
  per-day weights from the day documents; `raises` gives exactly the planted exception classes;
  exemplar facts are `fact`/`trigger_in_span` for a user entry and `co_occurrence`/`unchecked` for a
  learned entry (B7); `co_fires_with` edges only when `co_firing` is admitted. Co-firing incidents
  are byte-identical under `PYTHONHASHSEED` 0, 1 and 4242 and recover the planted incident only.
- Delta reparse (`test_logs_delta_reparse.py`): 12 seeded random library changes (1–3 of 6
  mutation kinds: steal, residue parse, retire, edit, annotate, noise edit): the delta reparse equals
  the full reparse in matches, aggregates and cells, and re-matches fewer rows than the partition
  holds; a format change is a full reparse (class A). A library change composed on day 10 of the gold
  window keeps >= 95 % of the dated day documents by id and content (delta mode on every partition).
  A format-1 partition in the window is reparsed (full) before compose; the derived snapshot round
  trip carries the typed-value aggregates. The property test is seeded `random`, not `hypothesis`
  (not installed).
- Count semantics (`test_count_semantics_b2b3.py`, client path): worst-row exactness, applied basis
  and extraction, `$measure` ranking by the sum (row count order differs), bucket-of-sum per day, hour
  buckets. The Lucene native path flags `bucket.of` as unsupported (bounded) and reads the role
  fields through a terms aggregation: **not run against OpenSearch/Elasticsearch in this slice**.
- Grouped-sum answers (`test_grouped_sum_contract.py`): stated group sums, their total, the number of
  groups and the population pass; a wrong number is replaced by the deterministic rendering
  (`stripped`), or abstains when no rendering exists. Contract level with a real memory-adapter
  result; no live model.
- Read latency (`scripts/bench_log_tools.py`, sqlite client path, one process, 20 calls each,
  external workspace): 10 days (4,582 documents): log tools p95 4–28 ms, graph neighbourhood 6 ms.
  180 days (`keep_days: 180`, 90,771 documents, build 189 s): log tools p95 70–433 ms (8 of 10 within
  200 ms; `log_compare` 232 ms and `log_coverage` 433 ms are not), graph neighbourhood 122 ms
  (within 500 ms). The "within 2x of the 10-day baseline" acceptance is **not met**: the sqlite typed
  scope is linear in the generation's records; the H1 shard filter that would bound it is not
  implemented. Before the sqlite owner-index fix the 10-day `log_around` took 1.8 s.
- Full suite: 2272 passed, 94 skipped (not external); ruff clean.

## Raw tier hardening and the interim derived archive — 2026-11-04 (branch `v2-logs-raw-hardening`, base `fdb7c4a`; A2 §11.0, §11.R4)

Evidence level: **synthetic** (contract tests on a local sqlite backend). No real service, no paid call.

- Raw tier: a group- or world-writable parent is refused; a raw path under the installed package
  is refused; `retention verify` fails on a raw file with mode 0640 or 0400 and on a raw day older
  than `raw_days`; an AST import-graph test asserts that only `ingestion/raw_reparse.py` imports the
  raw store, that `schedule.py` and `learning/` never do, and that raw content is read only there
  and by `privacy scan --raw`; a redaction change re-derives exactly the days still in the raw tier
  (4-day test: days 2–3 re-derived, day 1 listed `stale_redaction`).
- Derived archive: snapshot round trip with fallback-overlay rows (identity change → rewrite,
  unchanged → no rewrite), format-1 snapshots read and marked, the `serve_days` bound (7 days,
  keep 3: 3 served, 7 archived), `max_serve_rows`, window rollups equal to the masked window's day
  rows and smaller than all served days, and the multi-identity flag after a library change.
- Compose cost over **60 synthetic days** (`derived: forever`, default `serve_days` 10, 1 tenant x
  2 services x 4 lines a day): compose 0.105 s average for days 11–20 and 0.14 s for days 51–60;
  73 documents served on day 60; every unchanged day taken from the day-document cache; 0.13 GiB
  peak RSS for the whole test process. Small synthetic days: this shows the bound (no growth with
  history), not production throughput.
- Host guard: API-only process with `allowed_hosts` behind a LAN forwarder: the forwarder's Host
  passes, another Host gets 421, a cross-origin write 403; a loopback API without a list stays
  unguarded.

## Deployment-run package fixes, B11 safe redaction, retention scopes — 2026-11-04 (branch `v2/logs`, base `106d4b0`)

Evidence level: **synthetic and mocked**, plus three real-service checks on throwaway local
containers. No paid call, no private data: every bug from the private run reports was reproduced
with synthetic names and lines.

- Unit and contract tests (synthetic): probe PIT opt-in and `disallow`, one privilege request,
  request log, index-name prefix / `before_service` / `after_env` / `unparsed: exclude`,
  continuation rules and library continuation entries, multiline-aware `patterns coverage|mine`,
  noise precedence, full lint report, record-key filters, grammar false positives (both reproduced
  first), aggregate `measure`, manifest counts, retention semantics, safe redactors (each named
  redactor including the payload classes, custom redactors, ReDoS refusal, idempotence, timeout fails
  closed), leak scan, B11 through `run-daily` (CLI against a localhost fake cluster), egress
  admission of stamped records, serving pins (a stream holds its generation while a concurrent
  retention run reports it pinned), `serve` binding, readiness and logged 503 causes, retention
  scopes over 13 synthetic days (raw 3, partitions 10, derived forever: day-1 rollups still
  queryable; derived days count variant), the raw tier (modes, purposes, reparse `--from-raw`, no
  reader module references it, scan skips raw unless `--raw`).
- Louvain determinism: the base code gave 20, 18, 19, 20 communities on one synthetic graph under four
  `PYTHONHASHSEED` values; after the fix the output of both strategies is byte-identical across
  four seeds (subprocess test).
- RE2: the redaction, B11 pipeline and package-run test files pass with google-re2 1.1.20251105 from
  the isolated copy in the external workspace (100 passed, 1 skipped: the missing-engine test).
- PostgreSQL (`pgvector:0.8.0-pg17`, throwaway container): `tests/conformance/test_postgres_conformance.py`
  25 passed, including `test_records_use_native_types` (the failure was a test-ordering bug: the kit's
  text-count check rewrote the records before the type query).
- OpenSearch 3.9.0 (throwaway container): the new end-to-end test (dataset `my_data`, prefix with
  `_`: build, activate with pointer read-back, query) fails at base `106d4b0` with the
  "foreign index" BackendError and passes after the fix; `tests/services/test_opensearch_adapter.py`
  passes (7 passed together).
- N10 compose scale (`scripts/compose_scale.py`, sqlite, external workspace, 10 days x 100 units x
  1000 signatures = 10^6 aggregate rows, 1,113,100 documents): **1096.5 s compose, 3.94 GiB peak RSS**
  (budget: 20 min, 8 GB; met). Before the change: 1118.8 s, 10.28 GiB (previous entry). At 10^5 rows:
  1.07 GiB at base, 0.92 GiB with copy-on-write redaction only.
- Not measured: redaction throughput on a real log volume, the `after_env` grammar on a real index
  inventory, derived-forever growth over months, multi-shard or security-enabled clusters.



## Day partitions, compose, retention and pins — 2026-11-04 (addendum A1 slices L3a + L3b)

Evidence level: **implemented; synthetic and contract-mocked, with the ledger retention operations
and pins also run against throwaway PostgreSQL and MongoDB containers.** No log cluster was
contacted (the source is the in-process fake cluster), no paid call was made, and no customer data
was used.

- **Suite.** `python -m pytest -m 'not external'` gives **1856 passed, 73 skipped** (base `a27ab5a`:
  1779 passed, 73 skipped). Bare `pytest -q` gives 1859 passed and 198 skipped (external tests skip
  without their services). `ruff check src tests scripts` is clean; `docs/config.schema.json` was
  regenerated with `core.schema.write_all`; the golden index fingerprints are unchanged.
- **11 synthetic days, `keep_days: 10`, `keep_generations: 2`**
  (`tests/contract/test_retention_11_days.py`, sqlite backend, 6 indices x 16+ lines per day). Every
  run succeeds and verifies. After day 11: day 1 is a tombstone; the generations of days 9 and 10
  (both list day 1) are deleted under rule (a), so the active generation is the only one left; no
  remaining catalog document holds a day-1 line hash; the day-1-tagged ledger reply, every
  unreferenced `extraction:`/`embedding:`/`pattern_library:` key and the deleted generations' job
  entries are gone; cache entries of deleted generations are gone; the day-1 session expired; run
  reports rotate to the 10 window days; audit records, manifests, reports and the tombstone hold
  no day-1 line or source id. An injected unreferenced ledger entry holding a day-1 source id makes
  `verify` fail with both the content and the reference finding.
- **run-daily** (`tests/contract/test_run_daily.py`): refused without a redactor before any request
  (the fake cluster records none); partitions hold only redacted text; source counts equal the
  generator's truth; signature window counts equal the parsed matches; a second run pulls and
  composes nothing; a crash after two of six units resumes with no duplicate record and the same
  documents as a clean run; another source query on an open partition is refused; the schedule
  lock refuses a second run while compose takes the write lease itself; a library change reparses
  the day (`v2`) before compose; the spawn parse pool gives byte-identical matches and aggregates;
  `settle` re-pulls the one grown unit; salted extraction keys of a library change go with their
  generation.
- **Timezone mapping** (`tests/unit/test_log_day_window.py`): index-date and event-time windows,
  23- and 25-hour DST days in `America/New_York`, an eastern zone, and an end-to-end event-time
  day whose unit counts equal the fake cluster's lines inside the local day, read from two UTC
  index days.
- **Pins** (`tests/unit/test_pins_and_gc.py`): TTL, release and sweep on memory, the sqlite sidecar
  and SQL; a pin written by a **separate process** blocks `gc` (`pinned_expired_generation`, day
  kept, verify fails) until it expires, then `gc` deletes and verify passes; bounded retry;
  `ProcessPins` writes once for 100 requests and drops a non-current generation at refcount 0; a
  deletion that a backend leaves incomplete keeps the day and its ledger references and fails verify;
  compose failure skips expiry.
- **Real-service (throwaway containers `pgvector/pgvector:0.8.0-pg17`, `mongo:7.0.16`, removed
  afterwards).** PostgreSQL, MongoDB and `sql[postgres]` conformance including the new ledger
  retention checks, and the PostgreSQL pin test: 69 passed. One failure,
  `test_records_use_native_types`, fails identically at base `a27ab5a` (pre-existing, unrelated).
- **Compose scale (N10, `scripts/compose_scale.py`, external workspace, sqlite, hash embedder).**
  10 days x 100 units x 1000 signatures = **10^6 aggregate rows**, 1,113,100 documents, one
  compose in **1118.8 s (18.6 min, within the 20 min budget)** with **peak RSS 10.28 GiB (above the
  8 GB budget: N10 is not met on memory)**. 10^5 rows: 117.4 s, 1.07 GiB; 10^4 rows: 27.2 s, 0.27
  GiB. The write path holds the whole corpus and its chunks in memory, and the per-document
  ledger status entries (one sqlite transaction each) dominate the wall time. Streaming or
  per-partition delta compose (addendum §4.3, deferred "only if the scale acceptance fails") is
  the follow-up. Measured on one workstation (32 cores, NVMe), external workspace, regex engine.
- **Not established.** Any real log cluster, real redaction (slice B11; the tests inject a
  synthetic e-mail/IPv4 redactor), the learning step, communities, serving-path pin wiring, and
  the pull window.

## Read-only log sources and synthetic log fixtures — 2026-11-04 (addendum A1 slices L0 + L1)

Evidence level: **implemented, synthetic and contract-mocked only.** No real Elasticsearch or
OpenSearch cluster was contacted. No container ran, no paid call was made, and no customer data was
used.

- **Suite.** `python -m pytest -m 'not external'` gives **1627 passed** (base `9ba767f`: 1189 passed).
  `ruff check src tests` is clean, `docs/config.schema.json` was regenerated with
  `core.schema.write_all`, and the wheel ships `sources/` and `testing/fixtures/logs/` (including
  the ReDoS corpus YAML).
- **Allow-list** (`tests/unit/test_source_allowlist.py`, 351 cases).
  - Every combination of seven methods and 46 paths that is not in the §3.1.1 table is refused, and
    the fake cluster records **zero** requests (303 of the 351 cases).
  - Path tricks are refused, among them percent-encoded `_all`, `..` and `?`, double encoding,
    undiscovered or hidden indices and `//`.
  - Refused bodies include scripts at any depth, `runtime_mappings`, aggregations, `from` and
    oversize pages.
  - Query parameters are allow-listed and keepalives are bounded.
  - `DELETE` closes only ids that this transport opened and that the cursor lists, and never `_all`.
  - Redirects are not followed. Credentials and the URL never appear in errors, `repr` or request
    records.
- **Self-check and capabilities** (`test_source_self_check.py`). A granted write privilege or
  `cluster:all` gives a warning, or an error under `require_read_only_role`. A privilege endpoint
  returning 403, 404 or 500 gives `unknown`. OpenSearch is checked through the account endpoint.
  A 403 on `GET /` is non-fatal. The version floor and the `_alias` fallback for releases before
  7.9 are covered.
- **Pulls** (`tests/contract/test_source_pull.py`), against the in-process fake cluster.
  - Counts equal the generator's truth for each index: `level_field` where the mapping has `level`,
    `token_match` elsewhere.
  - Scroll and `search_after` (with resume) return every document exactly once.
  - An expired scroll restarts the unit without duplicates. An expiry beyond `max_restarts` is a
    typed error.
  - Scrolls are closed on completion, when the generator is abandoned, by the exit hook, and on a
    real `SIGTERM` in a subprocess.
  - The open-context limit is refused before I/O, and context fetches are capped per partition.
  - Mapping plus multiline over a pulled day reproduces the generator's head, continuation and
    fragment counts.
- **Sampling** (`test_source_sampling.py`). Largest-remainder quotas sum to the cap. Hourly
  strata, with an `outside` stratum, sum to the unit count. A seeded pull is reproducible. The
  partition budget stops the pull and marks it `capped`.
- **Fixtures** (`test_synthetic_logs.py`). The generator's digest is pinned. The three line styles,
  noise, multiline fragments and synthetic-only PII shapes (`example.invalid`, 192.0.2.0/24) are
  covered. The ReDoS corpus is well-formed, and its nested-quantifier case grows super-linearly under
  Python `re`, a small timing check of a few milliseconds.
- **CLI.** `sources check|discover|count|probe` ran against the fake cluster served on 127.0.0.1.
  The probe report contains no log line and no URL.
- **Not established.** Behaviour against any real Elasticsearch 7.x or OpenSearch 2.x. The isolated
  container tier named in addendum §4 L1 was not run. Throughput, permission to scroll or PIT, and
  `random_score` on a real cluster are what `sources probe` is for; an operator runs it outside this
  repository. Redaction of pulled lines (slice B11) and persistence of day partitions (L3a) are not
  implemented.

Release candidate: `2.0.0a3` (slice a3/contract-providers). This release makes `on_violation: strip`
the default and native structured output the Anthropic default. It also fixes the local-model QA
findings QQ-01 to QQ-06. CHANGELOG.md lists the changes, and the "2.0.0a3 contract and providers"
entry at the end has the evidence.

- **Synthetic local suite**: `python -m pytest -m 'not external'` gives **1069 passed** (the base at
  `b99eae2` gave 1031 passed and 1 skipped). Bare `pytest -q` gives 1072 passed and 123 skipped.
  `ruff check src tests` is clean.
- **First real Anthropic run.** One small smoke under the QA spend guard: `claude-haiku-5-5` and
  `claude-sonnet-5-5`, 44 requests, **$0.047** by the guard's ledger. It covered native
  `output_config.format`, forced tool use with `$defs`, prompt-cache write and read, thinking and
  truncation, LLM extraction with grounding, a planned answer, and a streamed answer.
- **Refusal** (`stop_reason: refusal`) is still contract-mocked only. No prompt was sent to provoke
  one.
- **Not measured**: answer quality or relevance on any non-synthetic corpus, and Anthropic
  behaviour beyond these single calls.

## 2.0.0a3 fixes from the final parity run — 2026-11-04

The final private parity run found five package defects; all five are fixed. CHANGELOG.md
(2.0.0a3, Fixed) lists them.

**Evidence level: synthetic and mocked only.** No paid or real-service call was made, and the
parity run was not repeated with the fixes.

- **Suite.** `pytest -m 'not external'` went from 1189 to **1225 passed**. Bare `pytest -q` gives
  1228 passed and 124 skipped. `ruff check src tests` is clean.
- **Golden index fingerprints are unchanged.** `examples/v2/hosted_models.yaml` now declares
  `structured_output: none` for its planner; chat roles are not part of the fingerprint.
- **Structured output**, mocked HTTP (`tests/contract/test_structured_output_fallback.py`):
  - the pre-flight counts the plan schema at 37 optional and 23 union-typed properties, over the
    ceilings of 24 and 16;
  - a registry-default Sonnet 5.x planner resolves to prompt mode with a warning, and a declared
    `native` one is refused;
  - a mocked 400 "compiled grammar is too large" is `ProviderSchemaTooComplex`. It produces exactly
    one recorded switch for a registry-default role and none for a declared one. Another 400 is
    never switched.
  - Whether the real API accepts every schema under the ceilings is **not verified**. The ceilings
    are the documented limits; the real 400 was observed for the plan schema only.
- **Answer-number check.** Synthetic phrasings of a cited denominator or ratio population pass,
  modelled on the parity run's patterns ("out of", "denominator is", "as the denominator", "among",
  "over", "%", "/"). Wrong counts next to a population, and the denominator stated alone, still
  abstain (`tests/unit/test_answer_text_contract.py`).
- **Rules path.** Synthetic questions with a dropped ordinal, state word or threshold abstain with
  `qualifier_not_applied`. "X-mention" is a topic. The existing grammar cases are unchanged
  (`tests/unit/test_query_compiler.py`).
- **Sort by count.** A planner scope sorting groups by `count` or by its measure compiles to the
  ranking and executes on the memory store; the ill-defined cases are refused.
- **Precomputed verify.** Rows built from the engine template pass the default spot check, and rows
  of the bare text fail it. With `input: chunk_text` the reverse holds (hash embedder,
  `tests/unit/test_precomputed_vectors.py`).

## OpenSearch 3.x support and the Lucene result window — 2026-11-04 (branch `v2/phase-0`, base `cf242ce`)

**Evidence level: real-service (isolated local containers) plus mocked transport.** All data is the
synthetic conformance kit and a synthetic 300-document corpus. No paid call was made.

Servers (compose project `su-multi-arm-tests`, all on 127.0.0.1, security plugin disabled):

| Service | Image | Digest | Lucene | Bundled JDK |
|---|---|---|---|---|
| `opensearch` (19200) | `opensearchproject/opensearch:2.19.1` | `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4` | 9.12.1 | 21.0.6 |
| `opensearch3` (19202, profile `opensearch3`) | `opensearchproject/opensearch:3.9.0` | `sha256:3b55f6b6249cfdb92b52ee59adc8a266203ba9ef9f5d82544fad6f1af2f3609e` | 10.5.1 | 25.0.4.1 |
| `elasticsearch` (19201) | `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56` | — | — |

3.9.0 was the latest stable 3.x tag on Docker Hub at the time of the run.

- **OpenSearch conformance** (`pytest -m external -k opensearch tests/services/test_opensearch_adapter.py
  tests/conformance`): **20 passed, 8 skipped** on 2.19.1 (`SU_TEST_OPENSEARCH_MAJOR=2`) and
  **20 passed, 8 skipped** on 3.9.0 (`SU_TEST_OPENSEARCH_MAJOR=3`). The skips are the capabilities
  OpenSearch does not declare (graph, ledger, session, cache). This covers exact and HNSW vectors,
  the alias pointer, and the records checks.
- **Elasticsearch conformance** (`-k elasticsearch tests/conformance`): **18 passed, 8 skipped**.
  The base `94d978e` code gave 4 failed on the same server (the result-window defect below).
- **Result-window defect, found by this run.** On base `94d978e`, `test_capability[opensearch-records]`,
  `[opensearch_hnsw-records]` and `test_run_all[opensearch|opensearch_hnsw]` failed on 2.19.1 with
  HTTP 400 "Result window is too large ... [10001]", and the same 4 failed on Elasticsearch. The
  structured arm's text count pages `lexical.search` with 10000-hit pages, and the look-ahead hit
  made `from + size` 10001. Fixed in the shared `OpenSearchLexical.search`, with a mocked
  regression test (`test_lexical_paging_never_crosses_the_result_window`).
- **Cross-major comparison** (workspace script, synthetic 300 documents, 5 lexical queries plus 1
  scoped, 5 vector queries, exact and HNSW): lexical rankings were identical on 6 of 6 queries. Every
  2.19.1 score was **exactly 2.2 ×** the 3.9.0 score over 143 hits, which is `k1 + 1` (3.0 replaced
  `LegacyBM25Similarity` with `BM25Similarity`). Exact and HNSW+rescore vector results were
  identical across majors (max |score difference| 0), and HNSW+rescore equalled exact on 5 of 5
  queries on both.
- **nmslib.** On 3.9.0, creating a `knn_vector` index with `engine: nmslib` returned
  `mapper_parsing_exception` ("cannot be used for new index creation in OpenSearch from 3.0.0"). The
  same request succeeded on 2.19.1. The adapter writes `lucene`. The probe guard for a blocked engine
  is unit-tested only.
- **3.x changes checked and not affecting the adapter.** These come from the 3.0.0 release notes and
  the k-NN plugin notes for 3.0–3.9:
  - The index-level `knn.algo_param.*` and `knn.space_type` settings and `knn.plugin.enabled` were
    removed. The adapter sets only `index.knn` and the per-field `method`.
  - `_bulk batch_size` was removed. The adapter does not use it.
  - Keyword `term` queries no longer score by default. The adapter uses keyword terms only in filter
    context.
  - Derived source for vectors is on by default for `index.knn: true` indices. The chunk updates and
    the painless re-denormalisation passed on 3.9.0.
  - Painless `cosineSimilarity` over `knn_vector` doc values (the exact path) is unchanged.
  - JDK 21 is the minimum. The image bundles it.
  - The security demo configuration needs `OPENSEARCH_INITIAL_ADMIN_PASSWORD`, which is not needed
    with `DISABLE_SECURITY_PLUGIN` and `DISABLE_INSTALL_DEMO_CONFIG`.
- **Suite.** `pytest -m 'not external'` gives **1237 passed**. The base `cf242ce` gave 1224 passed
  and 1 failed: a hygiene test on an execution-log line, fixed here. Bare `pytest -q` with the 2.19.1
  and Elasticsearch containers up gives **1278 passed, 86 skipped**. `ruff check src tests` is clean.
- **Not covered.** These were not tested:
  - OpenSearch 3.0–3.8 and 2.x other than 2.19.1;
  - an in-place 2.x → 3.x upgrade of an existing index (expected to keep working, with BM25 scores
    rescaled);
  - the multi-backend engine test (`tests/services/test_multi_backend_engine.py`), which needs
    PostgreSQL and Neo4j, against 3.x;
  - security-enabled clusters.

## 2.0.0a2 status — 2026-11-04 (historical)

Release candidate: `2.0.0a2` (branch `v2/phase-0`). This release fixes the 2.0.0a1 QA findings
(QA-01 to QA-16), the 2.0.0a1 completeness-review risks R1 to R3 and the 2.0.0a2 release-review
blockers B1 to B6. [CHANGELOG.md](https://github.com/Taranjeet-SU/su_multiarm/blob/main/CHANGELOG.md)
lists the changes. The scope of Phase 0 and Phase 1 is unchanged from 2.0.0a1.

Evidence by level. The "2.0.0a2 release evidence" entry at the end has the detail:

- **Synthetic local suite**: `python -m pytest -m 'not external'`, **1018 passed**, plus the bare
  `pytest -q` that CI runs in a fresh venv with the CI extras: 1007 passed, 131 skipped, 0 errors.
- **Real-service conformance**: one consolidated `-m external` run on the five disposable containers
  gave **108 passed, 18 skipped, 0 failed**. Image digests are recorded.
- **2.0.0a1 index compatibility**: an index built by the published 2.0.0a1 tree opens and serves
  search under 2.0.0a2. A golden test pins the 2.0.0a1 fingerprint of every shipped example.
- **Paid providers**: OpenAI (`gpt-4o-mini`) was exercised for real in the 2.0.0a2 QA-08 rerun
  (slice D entry). **Anthropic has still not completed a real call.** The 2.0.0a2 smoke got HTTP 429
  on its first request and spent $0.00.
- **Local-model provider smoke**: `tests/services/test_local_llm.py` passed against a small local
  model (2026-11-03). It was not re-run for 2.0.0a2.
- **Not measured**: relevance or answer quality on any non-synthetic corpus, Anthropic behaviour,
  scaling, and contention between writers on different hosts.

The sections that follow, starting with the 0.1.0a1 alpha report, are kept as dated history; their
version numbers and test counts describe the code at that date, not this release.

## Vector import, source order and source slots — 2026-11-04 (branch `parity/review-fix`)

Scope: synthetic unit tests only (`tests/unit/test_precomputed_vectors.py`); no customer data, no paid
calls. Covered: import matched by key with the provider never called; missing-row error vs
`on_missing: embed`; audit refusals (dimension, count, duplicate key, non-finite, zero row, vectors
digest, keys digest) with nothing activated; relocating the files keeps the index fingerprint and
recipe while a changed digest changes both, and no path appears in the recipe or manifest; manifest
records both digests, positional pairing and the imported input; a permuted keys file passes the file
audit (documented as undetectable without `verify`) and fails the spot check, while the correct
pairing passes it; FTS re-index with duplicate ids keeps one row per chunk; vector upsert replace path
keeps ids and rows aligned; source slots never displace a pinned exact-id hit and are traced; v1
downgrade refuses `source_order`/`source_slots`. The 2.0.0a1 golden index fingerprints are unchanged.
An aggregate-only production-workload parity summary is kept outside this repository; it is not a
claim of this release.

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


## 2.0.0a2 serving/config slice — 2026-11-04 (QA-05, 07, 10, 11, 12, 14, 16; review risk R3)

Evidence level: **synthetic and mocked**, plus one small real-service check. Fixes for the
2.0.0a1 QA findings named above (`docs/plans/reviews/qa_2.0.0a1.md`); each fixed finding's
`xfail(strict=True)` marker in `tests/qa/test_qa_2_0_0a1.py` was removed and the test now passes.

- QA-05 sessions: every session write is a compare-and-set on `SessionRecord.version`; the
  session-budget clip is a per-turn hold taken in the same CAS. Tests: 30 concurrent `search` turns
  on one session on sqlite and memory keep 30 turns; 30 concurrent `answer` turns under
  `budgets.session.max_llm_calls: 3` make at most 3 model calls (the generator is a fake that sleeps
  50 ms to force overlap); store-level CAS tests on memory, sqlite, sqlalchemy-on-sqlite (conformance
  kit) and a MongoDB document fake. **Real service**: the kit's `check_session` (now with the CAS
  checks) passed on PostgreSQL and SQLAlchemy-on-PostgreSQL against the local throwaway stack
  (`tests/services/`, already running, isolated random prefixes). MongoDB CAS was **not** run against a
  server: its conformance fixture drops a shared database name and the stack was in use by another run.
- QA-07 sqlite lexical: the query is no longer `str.casefold`ed (FTS5 `unicode61` folds it as it
  folded the text). `Straße`, `ﬁle`, `Œuvre` and Greek capitals match their own indexed words;
  `strasse`/`file` do not (unicode61 expands neither). `scripts/lexical_agreement.py` (sqlite only,
  no `SU_TEST_*` variable set): per-query results byte-identical before and after the change on the
  23-query list, so `docs/LEXICAL_AGREEMENT.md` stays valid.
- QA-10 `config upgrade --output` rebases relative paths for the output's directory (CLI test from
  another directory opens the v1 index). QA-11 schema errors are aggregated with key paths; the CLI
  prints concise errors with exit codes 1/2/3 and a traceback only with `--debug`. QA-12 the bearer
  scheme is case-insensitive. QA-14 keys used only by non-default pipelines/modes are checked on
  first use; `doctor` reports `missing_keys`.
- QA-16: `pytest -q` in a venv with every extra installed and **no** services: 823 passed, 115
  skipped, 12 xfailed, 0 errors, measured before the 4 `tests/unit/test_service_probe.py` tests
  were added and before the throwaway stack was started by another run (before the fix: 27 failed,
  46 errors from the OpenSearch/Elasticsearch conformance and OpenSearch services tests). The external search-engine tests now skip with the
  reason when their variable is unset and the default stack does not answer.
- Risk R3: `extraction.ontology`, `models.roles.judge`, `models.roles.cache_gate` and
  `pipeline.arms.cache` are refused at validation ("not implemented in this release"); a unit test
  walks every Config field and requires a reader (name-based).

Local suite at the slice commit: `pytest -q -m 'not external'` 824 passed, 12 xfailed (before: 766
passed, 20 xfailed at `95bb183`); ruff clean; `docs/config.schema.json` regenerated.
Not covered: MongoDB CAS on a real server; multi-process (as opposed to multi-thread and
multi-manager) session contention; hosted providers (all model calls are fakes).

## Decision agent, typed scope, request filters, answer contract, generation — 2026-11-04 (2.0.0a2, slice a2/decision-contract)

Fixes for QA-01, QA-02, QA-03, QA-04, QA-08, QA-09, QA-13 and QA-15 of
[the 2.0.0a1 QA report](plans/reviews/qa_2.0.0a1.md). Evidence levels:

- **Synthetic / mocked (offline)**: `pytest -m 'not external'` 846 passed, 9 xfailed (was 766 passed,
  20 xfailed). The QA reproductions for QA-01, QA-02 (3), QA-03 (3), QA-04, QA-09, QA-13 and QA-15
  now pass without `xfail`; model replies are replayed through a mocked HTTP transport. New tests:
  `tests/unit/test_answer_text_contract.py` (statement detection incl. non-count numbers: versions,
  dates, ids, decimals, percentages, durations, step numbers, quotations from evidence; strip/abstain/
  fail; absence needs an empty exact scope; per-request answer schema),
  `tests/contract/test_scope_qualifiers_and_filters.py` (the recorded QA-01 shape and four variants,
  exact scopes stay exact, request filters, caller-typed aggregates, plain retrieval, QA-09
  predicates, every QA-02 filter class, the aggregate tool, QA-03 on `examples/v2/local.yaml` and the
  v1 `examples/local.yaml` through search, answer, stream, the `search` tool and HTTP), and strict
  schema cases in `tests/unit/test_provider_capabilities.py` (every refused keyword, the accepted
  keyword set, every schema the package sends, validation and `doctor --probe` reporting). The nine
  NL-count probe tests still pass (their raw-fallback violation list now also names
  `answer_text_without_exact_scope`).
- **Real provider (paid, OpenAI `gpt-4o-mini`, QA-08 only)**: the same 25 plain how-to questions as
  the QA report (10 questions twice on the QA's synthetic 305-document corpus with its config:
  `rules` decision, native generator, neutral profile; plus the 5 questions of
  `test_qa08_narrative_questions_mostly_answer` on the committed synthetic fixture). Before (QA report,
  2.0.0a1): 24 of 25 abstained. After (this slice): **0 of 25 abstained**, all 25 `contract: passed`,
  every claim typed `narrative` (the per-request schema offered no other kind because no aggregate
  evidence was supplied), no invented ids. One reply (1 of 25) had an empty `answer` string with a cited
  narrative claim and passed; the contract now rejects that (`empty_answer`, unit-tested offline, not
  re-run against the provider), so the effective rate for that sample is 1 of 25. The opt-in
  `test_qa08_narrative_questions_mostly_answer` passed once more against the provider afterwards (5
  questions). Spend through the QA transport-level guard and ledger
  (`~/su_mutliarm_tests/qa/qa_ledger.jsonl`, pessimistic $2/$8 per 1M tokens): 30 calls, $0.3425
  (tags `a2_qa08` $0.2909, `a2_qa08_pytest` $0.0516); cumulative OpenAI QA spend $0.8641 of the $2.00
  cap; no call refused. Anthropic was not used (the supplied key is invalid).

Not measured: answer correctness or relevance of the 25 answers (only the contract outcome was
recorded), the QA-04 text detector's precision on real model output beyond these 25 replies,
languages other than English (the detector yields no statements; typed claims still bind), and
Anthropic native tool use with the per-request enum schema.

## Write path: leases, fencing, incremental embeddings, ingestion budget — 2026-11-04 (slice 2.0.0a2/W)

Fixes completeness-review risks R1 and R2 (`docs/plans/reviews/a1_completeness_review.md`) and QA-06
(`docs/plans/reviews/qa_2.0.0a1.md`). R1: every generation registry has an owner-token write lease
with heartbeat renewal, expiry only after missed renewals, owner-only renewal/release (Elasticsearch
no longer deletes another holder's lock document), and activation as a compare-and-set on the
expected previous generation (`ingestion/lease.py`, ARCHITECTURE "Writers"). R2: vectors are reused
by exact input and embedding recipe and streamed in `batch_size` windows; the manifest reports
`embedded`/`reused_embeddings` and what is still rebuilt (`incremental`). QA-06: `budgets.ingest`
(`max_llm_calls` default 10000, `max_embedding_calls`, `max_usd`) replaces the per-request cap on the
write path, with reservations before every LLM and embedding call.

Evidence level:

* **Synthetic unit/contract tests** (`pytest -m 'not external'`, bytecode off, external basetemp):
  **797 passed, 126 deselected, 19 xfailed** (before: 766 passed, 117 deselected, 20 xfailed; the
  QA-06 test lost its `xfail` and passes). New: `tests/unit/test_write_leases.py` (two concurrent
  writer threads on sqlite and memory — exactly one activates, the other gets `WriteConflict(held)`,
  its retry keeps every document; a writer whose lease expired and was taken over fails
  `write_conflict:lost`; a moved base fails `write_conflict:stale_base`; lease/CAS checks on the
  memory, file, SQLAlchemy-on-SQLite, MongoDB (fake collection) and PostgreSQL (fake connection)
  registries), `tests/unit/test_incremental_embeddings.py` (one changed document re-embeds only its
  changed chunks, counted at the embedder; a title change re-embeds that document; a change of prefix,
  dimension, model or revision re-embeds all; unchanged recipe reuses all; embedding-call budget;
  corrupt stored vector fails the build; vectors streamed per window), `tests/unit/test_ingest_budget.py`
  (offline `httpx.MockTransport`: 16 extraction calls succeed under a per-request cap of 2; the ingest
  cap stops at 3; `ingest.max_usd` stops extraction with committed spend ≤ the cap; embedding requests
  counted and priced from reported usage; unpriced spending role refused at construction; v1
  upgrade mapping), plus Elasticsearch lease/alias-fence unit tests with a fake transport.
* **Real-service tests** (throwaway `su-multi-arm-tests` containers, started with
  `up -d --wait postgres mongo elasticsearch opensearch neo4j` and removed with `rm -sf` of exactly
  those services): `pytest -m external tests/conformance tests/services/test_multi_backend_engine.py`
  **98 passed, 16 skipped (undeclared kit capabilities), 0 failed**, including the new lease/CAS tests
  on PostgreSQL (advisory lock seen in `pg_locks`), SQLAlchemy-on-PostgreSQL, MongoDB, Elasticsearch
  (lease document with `if_seq_no`, alias `must_exist` fence, concurrent-switch refusal) and the
  OpenSearch alias and Neo4j pointer compare-and-set. Images: `opensearchproject/opensearch@sha256:72fe2fc8…`,
  `elasticsearch@sha256:413d8df6…`, `pgvector/pgvector@sha256:40b40496…`, `neo4j@sha256:5a015e53…`,
  `mongo@sha256:c630c593…`.
* **Not measured**: no hosted provider was called (USD accounting is checked against an operator
  price table with mocked usage); no throughput or memory measurement at scale; multi-process
  writer contention was exercised with threads (and, on the servers, with separate registry objects),
  not with separate hosts.

Ruff clean. `docs/config.schema.json` regenerated with `core.schema.write_all` (adds `budgets.ingest`
and `models.roles.embedder.pricing_key`; the embedder `pricing_key` is excluded from the index
fingerprint, so existing generations stay valid).

## 2.0.0a2 release evidence — 2026-11-04 (branch `v2/phase-0`, release-review fixes)

Source: the release-review fix commits on `v2/phase-0` after `70dca3f`. Inputs: the blocking and
non-blocking items of `docs/plans/reviews/a2_release_review.md`. The review's own runs at `756b345`
were:

- `python -m pytest -q` with no services: 937 passed, 124 skipped.
- Bare `pytest -q`: 2 collection errors.
- External run on five containers: 107 passed, 1 failed, 18 skipped.

All runs below used `PYTHONDONTWRITEBYTECODE=1` with temporary paths under
`~/su_mutliarm_tests/claude/a2fix/`.

| Run | Result |
|---|---|
| `ruff check src tests` | All checks passed |
| `python -m pytest -q -m 'not external'` (repo venv) | **1018 passed**, 126 deselected. Before the fixes, at `70dca3f`: 934 passed, 1 skipped |
| Fresh venv from `git archive HEAD` with the CI extras `[dev,api,mcp,faiss,communities,sql,postgres,openai,anthropic]`, bare `pytest -q`, no `SU_TEST_*` | **1007 passed, 131 skipped, 0 errors**. With `-o pythonpath=` the collection fails with 2 errors (B4 reproduced and fixed). The first fresh run also had 8 errors: the Elasticsearch and OpenSearch conformance modules imported the client without the extra. They now skip unless a server URL is set. |
| `pytest -q -m external -rs`, all six `SU_TEST_*` variables, five containers (`su-multi-arm-tests`) | **108 passed, 18 skipped, 0 failed** (1018 deselected). Skips: undeclared capabilities (16), paid QA-08 (1), local LLM not configured (1) |
| Image IDs | elasticsearch 8.17.4 `sha256:413d8df69823e9f41f6008f025fcbaf31a5849ab9c74894731d7ded65543cd56`; mongo 7.0.16 `sha256:c630c59342c1493d50345136df2af14a76b9e827dd5316bfabee07a0880a5f3a`; neo4j 5.26.0-community `sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5`; opensearch 2.19.1 `sha256:72fe2fc84be8295906b8efca020b46c58ed45c8da60cd9b8b49e1991e38e89a4`; pgvector 0.8.0-pg17 `sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87` |
| Containers | Started with `up -d --wait` and removed with `down -v`. `docker ps` listed the same four unrelated containers before and after. |
| `su-multi-arm --version` (fresh venv); `python -m build` | `su-multi-arm 2.0.0a2`; `su_multi_arm-2.0.0a2-py3-none-any.whl` + `.tar.gz` (sdist includes CHANGELOG.md) |

**Blockers.**

- **B1 (synthetic, engine-level).** The reviewer's repro now gives `bounded` with
  `qualifier_not_applied: "priority 3"` and `contract: abstained`. The repro is "How many priority
  3 tickets are open?" with a planner scope of `status=open`, on 36 records where the truth is 4.
  Variants:
  - The same holds with `priority` listable, and for "have priority 3", "priority is 3", "priority
    above 2" and "priority: 3".
  - A scope or filter that applies the qualifier (`priority=3`, `"3"`, request filter, or `range
    lower 3`) stays exact at 4.
  - A wrong value (2) is flagged.
  - Unit tests cover `version 2.1` (number), `severity high` (non-listable string), `escalated` /
    `escalated false` (boolean), an id field, comparisons, and a negative list of 11 plain questions.

  The nine count probes (`tests/unit/test_count_probes.py`) and the plain-retrieval tests are
  unchanged and pass.
- **B2 (local, both trees).** `examples/v2/local.yaml` was ingested with the published 2.0.0a1 tree,
  a `git archive` of `public-main` = `a58df97`. `search` under 2.0.0a2 then exits 0 with top hits
  `case:certificate`, `kb:certificate`. Without the fix the same index gives `index/config fingerprint
  mismatch` (exit 3). The fingerprints of all 8 shipped examples are equal across the two trees,
  computed with backend paths fixed. `tests/unit/test_index_fingerprint_golden.py` pins them. With the
  fix reverted, 8 of its 9 tests fail.
- **B3.** The external run above has 0 failed (`test_failed_loads_never_move_the_pointer` passes).
- **B4.** See the fresh-venv row above.
- **B5 (contract, TestClient).**
  - `ingest_delta` while another writer holds the lease returns 409 with
    `result.category: write_conflict`, `reason: held` and no backend text. The audit record keeps the
    detail.
  - Invalid arguments return 422 (`invalid_request`).
  - `WriteConflict` and `SessionConflict` on `/v2/search` return 409 with a typed `detail`.
  - MCP `safe_message` returns `write_conflict: <reason>` / `session_conflict:
    cas_attempts_exhausted`.
- **B6.** `2.0.0a2` in pyproject, `__version__`, `--version`, the FastAPI app, CITATION (with
  `date-released`), and the README and docs banners. CHANGELOG.md was added.

**Non-blocking items done (synthetic).**

- **N1:** the reviewer's detector false positives are a negative fixture list. Real statements still
  fire, for example "Only 12 tickets", "Overall, 12 tickets", "That is the complete list of open
  tickets".
- **N2:** a stated count that differs from the cited exact aggregate is `answer_number_mismatch`
  ("99 open records" against 12 abstains; "12 of 36 records" passes). One existing harness test
  stated "Three" against a count of 2. Its text was corrected to "Two".
- **N4:** `product: "CloudTaxx"` and `"cloudtax"` (case-sensitive field) are reported as
  `value_not_observed`, the count is not exact, and an absence answer abstains.
- **N5:** a hosted embedder without `budgets.ingest.max_usd` or `max_embedding_calls` is refused at
  construction. Loopback `openai_compatible` and local embedders are accepted.
- **N8:** the key-file path was removed from `.claude/agents/*`.

**Not done:** N3, N6, N7, N9 beyond the action route, and N10. They are recorded as known
limitations in CHANGELOG.md.

**Real Anthropic smoke: not completed.** This was the first real Anthropic attempt with a key that
lists models.

- **Setup.** `claude-haiku-5-5` ran through the QA spend guard (`~/su_mutliarm_tests/qa/`, Anthropic
  cap $2.00, this run's budget $0.50) on a synthetic two-document corpus.
- **Result.** The first request was the LLM extraction call: native `tool_use`, `cache_control`
  on the system block and tool, `max_tokens` 1024. It returned **HTTP 429**. The package failed the
  build fail-closed: `IngestError`, category `provider`, no retry, and the provider body is not echoed.
- **Stopped.** Following the run rules, nothing was retried and no further call was made.
- **Spend.** The ledger shows 1 reservation ($0.0014) settled at **$0.00**. Cumulative Anthropic
  spend is $0.00 over 3 attempts: two 401s on 2026-11-03 and this 429.
- **Not established.** The run cannot tell a rate limit from a missing quota, because the guard does
  not log bodies. Still unsettled: whether `input_schema` with `$defs` is accepted (`LlmPlan` has 6
  `$defs`, `Answer` has 1), native planner and generator calls, streaming, and cache read/write token
  reporting. The Anthropic provider stays at "contract-mocked" evidence.

## Query-compiler operations (structured-question parity) — 2026-11-04 (slice parity/query-compiler)

Evidence level: **synthetic and mocked only**. No model call and no real service was used.
- **Records adapters.** `testing/backend_conformance.py::check_query_operations` holds every records
  adapter to the same results for ranking with ties, `nth`, calendar buckets, cross-tab, period
  comparison, shares, `count_distinct` with missing/null values (review B1), numeric measures with null
  coverage, negated states with their unknown count, lists with totals, record-identity predicates and
  an unresolved text condition. `check_text_count` checks whole-corpus text matches on every adapter
  with a lexical index. Ran here on memory, sqlite (numpy and faiss) and SQLAlchemy-on-SQLite; the
  PostgreSQL, MongoDB, OpenSearch and Elasticsearch modules run the same checks but were **not run**
  in this slice (no isolated service stack was started; they skip without one).
- **Grammar and planner.** `tests/unit/test_query_compiler.py` (41 tests) covers calendar
  expressions against a fixed anchor, zone handling, the rules grammar end to end over the in-memory
  store, refusals (median, ambiguous per-group measure, unstructured double windows, residual words,
  share without numerator), the strict-expressible planner schema with a mocked planner, the engine's
  text-topic count through the structured arm, the answer contract's number check and the packing cap.
- **Not measured.** The second historical service's structured-question gap (26/45) was not re-measured: that needs the
  live planner and the private corpus. The grammar is English-only and covers the phrasings listed in
  `docs/CONFIGURATION.md`; other phrasings still abstain with an `UnsupportedConstraint`.
- Suite: `pytest -q -m 'not external'` 1073 passed (was 1032 tests); `pytest -q` (with
  `-o pythonpath=src` in the worktree) 1076 passed, 123 skipped; ruff clean; golden index
  fingerprints unchanged.

## 2.0.0a3 contract and providers — 2026-11-04 (slice a3/contract-providers)

**Synthetic and mocked evidence** (offline, `httpx.MockTransport` replays of the observed reply
shapes):

- `tests/qa/test_qa_qwen_2_0_0a3.py` has 13 tests for QQ-01 to QQ-04 and QQ-06. They came from the
  local-model QA run (Ollama 0.33.0 `/v1`, a 27B open-weights model, synthetic support corpus). The
  four proposed failing tests from that run pass unchanged after the fix: they XPASS under their
  original `xfail(strict)` markers.
- `tests/unit/test_contract_strip_default.py` has 8 tests for the `strip` default.
- `tests/contract/test_anthropic_a3.py` has 16 tests: native default, `output_config.format`
  request/reply, the thinking minimum, the v1 raise, thinking-token usage, `stop_details`, and every
  package schema converting for `output_format`.
- Suite: 1031 passed and 1 skipped at the base, then **1069 passed** (`-m 'not external'`). Bare
  `pytest -q` gives 1072 passed and 123 skipped. The golden index-fingerprint test is green.

**Found while fixing QQ-06 (synthetic):** "there are 111 open CloudTax tickets" was not detected as
a count at all when the question was "How many open CloudTax tickets are there?". The cause: "open"
became a question unit, so "111 open" was rejected as a label and the farther unit was never tried.
The detector now retries the farther unit.

**Real Anthropic smoke (first successful real calls).**

Setup: `~/su_mutliarm_tests/claude/a3_smoke/smoke.py` ran through the QA spend guard
(`~/su_mutliarm_tests/qa/`). The guard's caps were Anthropic $2.00 total and $0.40 for this tag. The
guard now prices Sonnet at $2/$10 per 1M tokens and still prices cache reads at the full input rate,
so its ledger is pessimistic. Models: `claude-haiku-5-5` and `claude-sonnet-5-5`; no other model was
called. Corpus: 6 synthetic tickets. Each step ran once.

Results:

- **Planner, native `output_config.format`, Sonnet 5.5.** The `LlmPlan` reply was valid, with 2
  steps. Stop reason `end_turn`, 3081 input / 87 output tokens.
- **Planner, forced `tool_use`, Haiku 5.5.** The registry kind was overridden in the harness. The
  `LlmPlan` schema with `$defs` was accepted as `input_schema`, and the tool input validated. Stop
  reason `tool_use`, 0 thinking tokens: a forced tool call does not think.
- **Prompt caching (`controlled`, Haiku 5.5).** Two identical calls with a 42.7k-character system
  prompt. The first reported `cache_creation_input_tokens` 13873. The second reported
  `cache_read_input_tokens` 13873. Both are normalised into `Usage` (input 13898 including cached
  tokens).
- **Thinking (Haiku 5.5, `max_tokens` 1024, text mode).** Arithmetic prompt: `end_turn`, 469 output
  tokens, of which 464 were thinking (`Usage.reasoning_tokens`).
  - In an exploratory raw call before the fix, `max_tokens` 128 ended with `stop_reason:
    max_tokens`, all 128 tokens thinking and no text block. This is why the 1024 minimum exists.
  - Sonnet 5.5 did not think on the same prompt.
- **Schema limits found on the real API.** `output_config.format` refuses `minimum`/`maximum` on
  integers and `maxItems` on arrays with HTTP 400. It accepts `minLength`/`maxLength`, `anyOf`
  with null, `default` and `$defs`. These findings set what `output_format_schema` drops.
- **LLM extraction with grounding (ingest, Haiku 5.5 `output_format`).** 6 documents and 12 chunks:
  12 extraction calls, all `end_turn`, 117 to 379 thinking tokens each. 17 entities and 1 relation
  were accepted under the strict grounding policy, with 0 rejected.
- **Planned answer (Sonnet planner, Haiku generator).**
  - First run: the contract **abstained** (`factual_without_citations` plus
    `answer_text_without_exact_scope`). The raw reply was not captured on that run.
  - Second run, same question, raw reply recorded: `contract: passed`. 4 narrative claims, citations
    `case:0003` and `case:0000`, `insufficient_evidence: false`.
  - The model's output varies between runs. The first abstention is a fail-closed outcome, not a
    package error.
- **Streamed answer (`answer_stream`, Haiku generator `streaming: true`, `output_format`).** Events
  arrived as decision, arm_result, evidence, 50 token deltas, contract, then done.
  `contract: passed`, and the answer cites `case:0002` (the BOM fix).
- **Refusal.** `stop_reason: refusal` was not exercised; it remains contract-mocked.
- **Spend.** 44 requests: 37 Haiku 200s, 5 Sonnet 200s and 2 Haiku 400s from the schema-limit
  probes. The ledger shows **$0.0467** ($0.0342 Sonnet, $0.0125 Haiku). Cumulative Anthropic QA
  spend is $0.0467 of the $2.00 cap.

## Kuzu graph backend, typed traversal templates, hierarchical communities — 2026-11-04

Scope: `backends/kuzu/` (embedded Kuzu 0.11.3, extra `kuzu`; `mode: native` and read-only
`mode: attach`), `core/config_graph.py` + `arms/traversal.py` (typed traversal templates selected by
a `GraphIntent`), `communities/hierarchy.py` (`louvain_hierarchical`) and `communities/stats.py`.

Evidence level:

- **Synthetic, in-process (default test run).** The graph conformance kit passes on the native kuzu
  adapter with no service (`tests/conformance/test_kuzu_conformance.py`: fact-only traversal, hop
  bound, document-id eligibility, predicates refused, provenance/occurrences, memberships, communities,
  MERGE idempotency, atomic rejected upsert, generation isolation, compare-and-set pointer, namespaces
  sharing one file). One traversal-template fixture gives **identical results on the in-memory fake,
  sqlite and kuzu** (`tests/conformance/test_traversal_templates.py`); the kit's graph check now runs
  the template cases on every graph backend. Attach mode is tested against a synthetic external Kuzu
  database with its own schema (`tests/unit/test_kuzu_attach.py`): mapping validated against the
  store catalog, unverified/quote-less/undocumented edges never surfaced, scope applied per row,
  batched and per-node reads equal, writes refused, only built statements run. An engine run on
  sqlite + native kuzu (`tests/contract/test_graph_templates_engine.py`) ingests with a stand-in
  extractor, stores hierarchical communities with stats in kuzu, and a planner-selected template
  returns path provenance; an undeclared template abstains.
- **Not run:** neo4j template identity (`external`; no Neo4j test server was up), Leiden (Louvain
  dendrogram used instead; plan open decision 7).
- **Found and avoided:** Kuzu 0.11.3 `regexp_extract` returned a previous row's value for a
  non-matching row when combined with filters (non-deterministically). The attach adapter derives
  document ids in Python and runs no regex functions in Kuzu.
- Counts: `-m 'not external'` **1068 passed** (base 1031 passed, 1 skipped); bare `pytest -q`
  1071 passed, 124 skipped; ruff clean; golden index fingerprints unchanged (new community recipe
  keys enter the fingerprint only when set).

**Private parity measurement (aggregate only; store, mapping and questions stay outside the
repository).** A copy of an earlier service's Kuzu knowledge-graph store (~0.96 GB; ~600k nodes,
~1.4M relationships) was attached read-only through a private mapping (28 node tables, 30
relationship tables, communities). For the 36-question parity set, the graph queries the earlier
service actually executed in a recorded run (frozen plans; $0, no model call) were replayed as typed
templates through the package graph arm against the package catalog of the same corpus:

| | earlier service (recorded) | package, attached store |
|---|---|---|
| questions with graph queries | 12 | 11 replayed (1 used model-written Cypher: not replayable by design) |
| questions with graph evidence | 11 | 8 |
| graph evidence documents | 46 (as delivered) | 1,116 distinct (1,123 paths, all edges with quote + provenance; all in the package catalog) |
| overlap with the earlier evidence documents | — | 14 of 46 (4 questions) |
| retrieval targets reached through graph evidence | 0 | 1 (already in the package dense/lexical pool; 0 added) |
| community evidence | 48 unverified summaries (24 questions) | 0: the store's communities carry no quote and no entity members, so no grounded claim exists |

Read-out: graph evidence is now measurable through the package. Entity-neighbour and path questions
reproduce (overlap on 4 of 7 such questions); the 5 symptom/error "troubleshoot" replays (4 questions) return
nothing because the package resolves seeds by entity-name tokens while the earlier service matched
substrings of symptom descriptions; community summaries are not used because they are unverified.
Latency of the replayed graph steps: p50 1.0 s, max 53.5 s (a two-hop `connect` through hub nodes).
This is evidence about evidence volume and overlap only, not answer quality or relevance.

## Lexical `bm25` engine and request-path latency — 2026-11-04 (branch `worktree-agent-a0dd61f541791a8d6`, slice parity/lexical-latency)

Evidence levels: **synthetic** (unit, contract and conformance tests) and **measured on a private
corpus** (sanitised aggregates only; corpus, queries and per-query results stay outside the
repository). No paid model call was made.

- **Synthetic.** `rank_bm25.BM25Okapi` parity on a seeded 240-row Zipf corpus (words in more than
  half of the rows, so the idf epsilon floor applies; a word in exactly half; repeated and unknown
  query tokens): every score vector is bit-identical (`np.array_equal`; max |Δ| ≤ 1e-9 asserted),
  the idf table and `average_idf` are equal, and the ranked list is every row containing a query
  token by (−score, chunk id). The conformance kit passes on `sqlite_bm25` (and on `sqlite_faiss`
  with the new bitset scope). Tests: `not external` 1032 → 1066 passed; ruff clean; the 2.0.0a1
  golden index fingerprints are unchanged (the engine keys enter the recipe only with `bm25`).
- **Measured, private support corpus** (58.7k records, 521k chunks, 1024-d precomputed vectors,
  CPU only, shared host with load average ~15), 630 labelled queries replayed with frozen planner
  queries against the earlier recorded service lists (paired bootstrap 10k):
  - FTS5 path after the latency changes: all 630 lexical, dense, RRF and union lists are identical
    to the recorded package run (the latency work changes no ranking).
  - `bm25` alone (`ascii_alnum`, k1 1.5, b 0.75, ε 0.25): lexical R@10 0.668 / MRR 0.383 vs the
    service 0.698 / 0.426 — no better than FTS5 (0.676 / 0.389). The scorer was not the gap.
  - `bm25` + `verbatim_boost` + `pin_identifiers` (defaults) with front records kept first under
    `source_slots`: lexical R@10 **0.703** / MRR **0.426** (Δ +0.005 [−0.011, +0.022] /
    +0.000 [−0.018, +0.019]); fused RRF R@10 **0.837** / MRR **0.639** vs 0.826 / 0.645
    (Δ +0.011 [−0.000, +0.023] / −0.006 [−0.021, +0.008]). Without the `leads` ordering fix the
    same policies gave 0.672 / 0.384 (the slots re-ordered the front records).
- **Latency** (warm p50 / p95, retrieval only, query embedding on CPU included):
  - before (FTS5, 2 threads): 1.21 / 2.47 s — encode 0.39, vector search 0.24, two arm collapses
    0.24, FTS5 search 0.72 s;
  - after, `bm25`, 2 threads: **0.74 / 1.32 s** — encode 0.44, vector search 0.16, collapses 0.12,
    lexical search **0.028** s; 8 threads (630 queries): **0.41 / 1.03 s** (dense step 0.34,
    lexical step 0.11); excluding the query encode, ~0.25–0.29 s p50;
  - the FTS5 path with the same request-path changes: 1.10 / 2.32 s (2 threads) — the FTS5 query
    itself (0.88 s) stays the bottleneck.
  - **Target p50 ≤ 0.3 s not met** with the query encoder on CPU (0.19–0.44 s in-process). The p95
    tail is Python's full garbage collection over the ~0.5M objects the transformer stack loads
    (0.5 s per full collection); `gc.freeze()` after warm-up in the serving process cut the arm
    collapse p95 from 0.63 to 0.17 s in a 60-query check. The package does not do this itself.
- **Not established.** Relevance on other corpora; the policies' effect without `source_slots`;
  `bm25` build cost beyond this one ingest (886 s and peak RSS 10.4 GB, against 833 s and 10.4 GB
  for the FTS5 build of the same corpus; the segment held 23.4M postings and 229k terms);
  concurrent-request latency.

## Pattern extractor and deterministic pattern learning — 2026-11-04 (addendum A1, slices L2 and L4a)

Evidence level: **implemented, synthetic and contract-tested only.** No real log data, no source
cluster and no model provider was used, and nothing was measured on a non-synthetic corpus.

- **Suite.** `ruff check src tests` is clean. `pytest -m 'not external'` gives:
  - **1293 passed, 73 skipped** with the repository venv, which has no `google-re2`, so the RE2-parametrised cases
    skip;
  - **1366 passed** with `google-re2` 1.1.20251105 added from an isolated `--target` directory in the
    external workspace (the shared venv was not changed).

  The base at `9ba767f` gave 1189 passed.
- **Engine behaviour checked by tests**:
  - RE2 refuses backreferences and lookaround;
  - spans are character offsets on non-ASCII text, and `\w` is ASCII under both engines;
  - `Set.SearchSet` selects candidates, and only those are searched;
  - `(a+)+$` over 5 000 characters is linear under RE2;
  - a `regex`-engine timeout is typed and recorded as non-deterministic;
  - a missing engine is an error, with no fallback.
- **Lint**: a 13-pattern synthetic ReDoS corpus covering the four rules, the RE2-subset refusals, and
  every load-time lint class, including the `$attribute` rule.
- **Contract (sqlite write path)**:
  - approving a pattern makes the next build re-extract (0 reused) and adds its relation, while the
    index fingerprint stays unchanged;
  - deprecating the pattern removes the relation;
  - a library edit and restart keep `current()` serving;
  - `pattern_provenance` and the snapshot are recorded;
  - a lint error fails the build before any write;
  - the golden fingerprint test is unchanged and green.
- **Bootstrap (synthetic generator, 700 lines, 7 templates, 4 services)**. Coverage went from 0.143
  (one approved message pattern) to 1.0 with 24 synthesised proposals: 6 templates × 4 services, keyed
  per `(service, level)`. There were no rejections and no evictions, and every proposal is lint-clean.
  This shows the miner and synthesiser work on a fixture designed for them. It says nothing about
  coverage on real logs, which is an L0/L4 benchmark item.
- **Not established**: throughput per engine, the miner on public log corpora, and gate precision
  against real drift. The LLM proposer (L4b) is not built.

## Production-workload parity, final paired run — 2026-11-04 (2.0.0a3, commit `cec3ffb`)

Evidence level: **measured on private corpora** (aggregate only; corpora, questions, gold, prompts,
configs and per-query results stay outside this repository). Two previously served workloads were
replayed through the package on one CPU host; the old services were not called. The old side is the
services' own recorded results, with their own gold sets, metric definitions and denominators.
Answers on both sides were graded blind by the same small model judge (`claude-haiku-4-5`), each
alone under a random id. Paid model spend is capped; nothing in scope was skipped for budget.

**Corpus A** (support corpus: ~59k records / 521k chunks, four source types; imported 1024-d
vectors, alignment-audited; sqlite `lexical_engine: bm25` with `verbatim_boost` and
`pin_identifiers`; per-arm 20 with `source_slots` and `source_order`):

| Measure | Old | Package | Verdict |
|---|---|---|---|
| Frozen retrieval set (630 queries), dense R@10 / MRR | 0.673 / 0.357 | 0.678 / 0.384 | met |
| Lexical R@10 / MRR | 0.698 / 0.426 | 0.700 / 0.424 | met (CI includes 0) |
| Fused (RRF) R@10 / MRR | 0.826 / 0.645 | 0.834 / 0.637 | met (CI includes 0) |
| Diagnostic set (27 targets), hits @20 / @10 / pool | 12 / 12 / 15 | 12 / 9 / 13 | @20 met; @10 and pool not met |
| Retrieval latency p50, CPU, warm (frozen plan, in-process) | 0.10–0.27 s | 0.49 s (8 threads, `gc.freeze`) / 0.83 s (2 threads) | not met (CPU query encoding ~0.4 s; lexical 0.24 s) |
| Answer latency p50, planner + generator | 14–20 s | 16.9 s | met |
| Answers fully solving (36, rubric), newer planner + newer small generator | 12 | 6 | not met |
| Same, frozen planner output + newer small generator | 12 | 8 | not met; most of the loss is the generator model |
| Same, frozen planner output + the old generator model (2.0.0a2 run) | 12 | 15 | met |
| Graph evidence through a read-only attached Kuzu store | 12 questions, 46 documents, 0 targets | 36 questions, 6,232 documents, 1 target (0 beyond dense/lexical) | functional parity met; relevance difference not measurable |

**Corpus B** (ticket analytics: ~1.7k records; 100 owner questions with expected answers; old served
answers exist for 45):

| Measure | Old | Package | Verdict |
|---|---|---|---|
| Correct, questions with old answers (45) | 42 | 18 | not met |
| Correct, remaining questions (55) | not recorded | 4 | not measurable against the old service |
| Deterministic `rules` path (no model): exact counts correct / exact but wrong | – | 18 / 4 | the 4 are silent wrong "exact" numbers |
| Answer latency p50 | 10.3 s | 7.3 s | met |

**Gaps found** (classified):
- **Package:**
  - native `output_format` for the planner schema is rejected by the provider (grammar too large); prompt mode was used;
  - `answer_number_mismatch` treats a cited denominator as the stated count (10 abstentions with correct numbers);
  - the rules grammar returns exact aggregates while dropping qualifiers such as "second", mention filters and "active";
  - planners sort by the measure name, which the compiler rejects;
  - the vector-import spot check cannot use the producer's input template;
  - lexical/encode latency is above the old service.
- **Configuration:** ranking fields were not declared `sortable` for corpus B; corpus B used the FTS lexical engine.
- **Model change:** the newer small generator declines more often (fewer unsupported claims, fewer solved).

Corpus B's dense embedding model differs from the served one (an owner decision).

## LLM pattern-learning loop and index-name grammar — 2026-11-04 (addendum A1, slice L4b and the L1 parser fix)

Evidence level: **implemented, synthetic and mocked only.** Every model reply came from a scripted
fake provider served through `httpx.MockTransport` (the real `openai` / `openai_compatible` provider
code, metering and structured-output paths, but no network, no key and no spend). No real model, no
real log data and no source cluster was used. Nothing here measures proposal quality.

- **Suite.** `ruff check src tests` is clean. `pytest -m 'not external'` gives:
  - **1849 passed, 91 skipped** with the repository venv (no `google-re2`; the RE2 parameters skip);
  - **1940 passed, 0 skipped** with `google-re2` 1.1.20251105 from the isolated `--target`
    directory in the external workspace.

  The base `a27ab5a` gave 1778 passed, 74 skipped (repository venv).
- **Egress (tests):** plain lines, and lines redacted under another privacy configuration, are refused
  before any request; a residual IPv4 shape refuses the run (not dropped); a config without redaction
  is refused; a hosted role without `allow_hosted` is refused at config time and at run time; records
  from a `LogMapping` without a redactor are refused. In the request bodies no synthetic e-mail, IP,
  host or tenant value appears, only `[REDACTED_*]` markers and per-run aliases. That test first
  found the cluster template carrying a host value; the loop now pseudonymises before mining.
- **Gate (tests, both engines):** each rejection class through the loop, with nothing written:
  invented example id, matching negative (lint), over-broad (`out_of_cluster`), partial cluster
  (`cluster_coverage`), stealing an approved pattern's lines (`no_regressions`), ReDoS lint, too few
  literals, too little support, backreference; novelty against a human-disabled pattern; a fix of a
  disabled target is invalid; a fix that drops target positives fails `fix_target`.
- **Promotion and writer (tests):** `manual` keeps passing proposals in `proposed.yaml` (coverage
  0.16 before and after, 0.88 if approved, on a 25-line fixture); `auto` on RE2 learns both with
  `approve` records and the reparse reaches 0.88; `auto` on the `regex` engine waits for a human;
  `propose` never promotes; a fix of a learned pattern is a `supersede`; a fix of a human pattern
  waits and a human promote supersedes it. `library.yaml` is byte-identical in every case. A human
  save between read and rename is retried and kept. Two runs with the same fake replies give
  byte-identical files and reports. Git: only the changed system file is committed, with author
  `system:pattern_learner`; uncommitted hand edits stop the writer.
- **Spend (tests):** run and day call caps; a USD run cap refuses before the request; committed spend
  is the reported usage ($0.52 per call at the test price); the day ledger persists across runs and
  refuses the next run; two ledger handles on one file reserving concurrently grant exactly the cap.
- **Index-name grammar (tests):** synthetic deployment-prefixed and numbered infrastructure-prefixed
  names, and underscored service names, parse; undeclared prefixes, ambiguous readings, missing or
  invalid dates and vocabulary misses are `unparsed` with typed reasons.
- **Not established:** proposal quality, cost per day or coverage gain on real logs with a real
  model; the general spend ledger on other ledger backends (B10); raw-reply tagging by partition.

## Native aggregation pushdown for the Lucene records facets — 2026-11-04 (branch `v2/logs`, base `a27ab5a`)

Evidence level: **real-service on throwaway containers** (OpenSearch 2.19.1, OpenSearch 3.9.0 and
Elasticsearch 8.17.4 from `tests/services/docker-compose.yml`), **synthetic data only**, plus
fake-transport unit tests. No paid call; no private or production data; nothing was measured on a
non-synthetic corpus or on a multi-node or security-enabled cluster.

- **Suite.** `ruff check src tests scripts` is clean. `pytest -m 'not external'` gave **1804 passed,
  73 skipped**; the base gave 1778 passed, 74 skipped. The default stays `records.aggregation: client`,
  and the golden index fingerprints are unchanged because `records` is excluded from the backend recipe.
- **Conformance, both modes** (`tests/conformance/` with `-m external`):
  - the records kit runs natively on the new `opensearch_native` and `elasticsearch_native` adapters;
  - `test_lucene_native_aggregation.py` compares client and native on 176 specs × {no ACL, ACL} =
    352 `ScopeResult`s per server. It covers every measure alone and grouped, cross-tabs with nulls,
    calendar groups in `Europe/Berlin`, ranking with ties, periods (one with an unparseable bound),
    numerators (one unsupported), the three distinct-count units, the record list over typed,
    mixed-type and absent fields, negated-state null counts, and unsupported text;
  - all 352 results were **identical** (model equality and JSON dump) on each server;
  - the ACL check: a tenant-hidden record with a marker in every field never appears in any
    native aggregate, group, tie, id or list;
  - the caps check: `matched_ids_cap` gives a truncated, flagged list; `max_groups` gives a bounded
    result; above `distinct_exact_cap` the result is `approximate` with the reason in `basis`.
  - Runs: OS 2.19.1 + ES 8.17.4, 59 passed; OS 3.9.0 (`SU_TEST_OPENSEARCH_MAJOR=3`), 29 passed. The
    skips are other services and the env-gated scale test.
- **Scale** (`scripts/bench_native_aggregation.py`, 200 000 seeded synthetic log records, one
  records index, ACL on 10 of 20 tenants, so about 99.6k records match; median of 3; shared host; client path
  with `max_matched` raised so it can answer; ingest 9–12 s per server). Latency in seconds,
  client → native (default `matched_ids_cap` 10000); the last column is native with
  `matched_ids_cap: 100`:

  | Question (matched) | OS 2.19.1 | OS 3.9.0 | ES 8.17.4 | native, ids cap 100 (OS2 / OS3 / ES) |
  |---|---|---|---|---|
  | count (99.6k) | 2.49 → 0.065 | 2.53 → 0.065 | 2.44 → 0.041 | 0.002 / 0.002 / 0.002 |
  | count errors (16.8k) | 0.31 → 0.067 | 0.30 → 0.070 | 0.29 → 0.046 | 0.002 / 0.002 / 0.003 |
  | group by service | 2.58 → 0.067 | 2.61 → 0.071 | 2.27 → 0.047 | 0.004 / 0.003 / 0.006 |
  | top 5 services by errors | 0.33 → 0.071 | 0.32 → 0.079 | 0.26 → 0.047 | 0.004 / 0.003 / 0.004 |
  | cross-tab service × level | 2.55 → 0.069 | 2.59 → 0.075 | 2.34 → 0.056 | 0.005 / 0.005 / 0.006 |
  | errors per day | 0.33 → 0.070 | 0.35 → 0.073 | 0.28 → 0.049 | 0.003 / 0.004 / 0.004 |
  | avg latency by service | 2.58 → 0.071 | 2.66 → 0.069 | 2.69 → 0.044 | 0.004 / 0.004 / 0.004 |
  | distinct users (20k values, exact) | 2.55 → 0.086 | 2.62 → 0.093 | 2.29 → 0.066 | 0.023 / 0.022 / 0.024 |
  | week over week | 2.74 → 0.069 | 2.82 → 0.072 | 2.44 → 0.042 | 0.003 / 0.003 / 0.003 |
  | error share | 2.51 → 0.067 | 2.61 → 0.067 | 2.17 → 0.042 | 0.002 / 0.002 / 0.002 |
  | sum latency | 2.52 → 0.066 | 2.61 → 0.067 | 2.28 → 0.041 | 0.002 / 0.002 / 0.002 |
  | last seen 5xx (19.9k) | 0.68 → 0.072 | 0.68 → 0.072 | 0.32 → 0.046 | 0.003 / 0.003 / 0.003 |

  - Speed-up: 4–61× at the default ids cap, mostly the fetch of 10 000 ids, and up to ~1000× with the
    cap at 100.
  - The client cost grows with the matched count, while native stays roughly flat.
  - Every result **agreed** (equal, with float sums within a relative 1e-9; the native id list is the
    prefix of the client list and flagged truncated).
  - Every result was bit-identical except "avg latency by service" on OS 3.9.0 and ES 8.17.4. Those
    averages of non-integral doubles differ in the last ulps: the engine sums in its own order. This is
    a documented limitation.
- **Found and fixed by the scale run.** On OpenSearch 3.9.0 the count-ordered `terms` path returned a
  wrong top 5 (363 for a true 373, and one group missing). The cause: 3.x searches even a one-shard
  index in concurrent segment slices (`search.concurrent_segment_search.mode: auto`). The reply
  reported `doc_count_error_upper_bound` 648 at size 16, and the 352-spec fixture is too small to
  trigger it. The adapter now trusts a terms reply only when the engine reports error 0, grows the
  request otherwise, and gives a bounded result past `max_groups`. After the fix the 200k run is
  identical on 3.9.0. The regression is covered by a unit test and by the env-gated
  `tests/services/test_native_aggregation_scale.py` (`SU_TEST_AGG_SCALE_DOCS=200000`: 2 passed on
  OS 3.9.0 and ES 8.17.4).
- **Not established**:
  - multi-shard or multi-node indices (the adapter creates one-shard indices; terms ranking relies on
    the engine's error report, not on the shard count);
  - security-enabled clusters;
  - corpora above 200k records;
  - concurrent-request latency;
  - `percentiles` (not in the measure contract);
  - DST-gap zones for calendar buckets (the engine and Python may label a bucket whose local
    midnight does not exist differently; not tested).

## Web UI MVP (read-only) — 2026-11-04 (addendum A2 §4.4, branch `v2/logs` base `106d4b0`)

Evidence level: **synthetic + mocked model**. No real service, no real model, no customer data.

- **Python (mount and guards)**: `tests/contract/test_ui_mount.py`, 21 tests. A synthetic bundle (and
  the real built bundle when present) is served at `/ui` with the strict CSP and security headers;
  assets are public, while every `/v2/*` route the UI calls answers 401 without the bearer token;
  path traversal outside the bundle is a 404; `Host: attacker.example` gets 421 and a foreign
  `Origin` on a write or on the answer stream gets 403; `nosniff` is on `/v2/*` responses (errors
  included); `ui.enabled` without auth, without the wheel or with an empty bundle refuses to start;
  `serve` resolves host and port from `serving.http.bind`; the core distribution excludes `ui/`.
- **Frontend (vitest, jsdom)**: 40 tests. The XSS corpus (`<script>`, `<img onerror>`, `<svg
  onload>`, iframes, `javascript:`/`data:`/`vbscript:` URLs with case and whitespace tricks,
  markdown-image and link tricks, `<style>`, ANSI escapes, RTL overrides, a 20 000-character token)
  rendered through hit cards, every evidence tab, aggregate cards and the trace panel: no script,
  iframe, image or style element, no `on*` attribute, no non-http(s) `href`, `window.__xss` never set.
  A source guard rejects raw-HTML APIs and `localStorage`/cookie use. Also the SSE parser (chunk
  boundaries, CRLF split across chunks, heartbeats), typed error mapping, contract-shape rejection, a
  missing terminal event, token storage policy, feature detection, chart series (no coercion of
  non-numeric values), graph construction (no edge between co-mentioned entities; server edges to
  unknown nodes dropped) and trace spend (unpriced calls counted, not summed).
- **Browser smoke (manual harness, Playwright chromium, not in CI)**: `ui/e2e/serve_synthetic.py`
  built 2 synthetic log days with `LogsPipeline.run_daily` (fake cluster), served them with the
  loopback `FakeChatServer` as the streaming generator under bearer auth, and `ui/e2e/smoke.mjs`
  entered the token, streamed an answer (contract `passed`, citation jump), opened the evidence, graph
  and trace tabs, charted `group_count` by service (structured arm, exactness `exact`) and explored the
  entity `redis` in the graph explorer: **0 CSP violations, 0 page errors**, 6 screenshots (kept in the
  external workspace). The first run found cytoscape injecting an inline `<style>`; it is now
  pre-empted (rule in the bundle CSS), with no CSP relaxation.
- **Not established**: Playwright in CI, accessibility audit (axe), behaviour on a real deployment or
  a real model, the A2 inspect routes (`/v2/info`, graph neighbourhood, communities, patterns, spend,
  runs), log-intent views, `aggregates[].rendering` sentences (shown when present; the L5 slice adds
  them), and the large-graph layout.

## Accounted coverage and LLM fallback extraction — 2026-11-04 (addendum A2 rev 2–4, slices C1–C4; branch base `fdb7c4a`)

**Evidence level: synthetic data and mocked providers only** (`httpx.MockTransport` on the
OpenAI-compatible and OpenAI wire formats). No real model was called, no paid key was used, and no
real log was read. No coverage number below is a deployment result.

- **Accounting** (`tests/contract/test_fallback_daily.py`, 7 tests, real puller over the fake log
  cluster): with `multiline.max_lines: 2` the pulled documents include joined, overflow and fragment
  documents, and identities Q, D and H hold for every unit. A source wrapper that hands over one
  document fewer than the puller counted refuses the seal (`AccountingImbalance`, identity Q); the
  version stays open and `logs coverage` reports `ok: false`. A missing head fails identity H. The
  exact, `hourly_quota`, partition-capped and `random_score` pulls all balance (probe in the
  external workspace).
- **Fallback** (`tests/unit/test_fallback_extraction.py`, 27 tests; `test_fallback_realrun_fixes.py`,
  10 tests), on an 18-head synthetic day:
  - labelled clusters become `provisional` with `line[start:end] == value` for every value;
  - invalid labels (unknown group, wrong type) and a regex citing an unsupplied line are rejected by
    the gate;
  - the field-gap `fix` passes `fix_target` and keeps pattern values; the declined-fix line tier
    asks only for the gap;
  - hallucinated quotes, quotes containing a pseudonym and unknown reply ids are dropped;
  - an out-of-range value is `range_invalid`;
  - day 2 with new line ids makes **0 cluster calls** through the cache and is re-gated;
  - a pending proposal is reused without a call, while a changed template gets one;
  - the call cap gives `budget_deferred` and the retry resolves it;
  - no route gives `capacity_deferred` and the retry resolves it;
  - a down local route is skipped;
  - the paid route is unused by default and stops at the provider total cap;
  - a paid route without finite daily caps is refused at load;
  - an e-mail or tracking-id line is `egress_refused` on its own, and the run is refused above the
    share;
  - the stop file pauses between calls;
  - the report is persisted per cluster and stage;
  - two runs with the fixed mock give byte-identical overlay content;
  - the overlay keeps H, and `rows()` returns the same accounting.
- **Daily path**: `run_daily` runs `fallback` after the seal and writes an overlay. A failed
  fallback leaves heads `fallback_pending` and compose still builds. `apply_decisions` (RE2, auto
  promotion) reparses once into v2 and rebuilds the overlay with **0** calls.
- **Throughput**: per-route `calls_per_min`/`tokens_per_s` is measured around each call and reported
  per run. In the harness it measures the mock transport only and says nothing about a model; the
  real measurement belongs to the deployment.
- **Totals**: 44 new tests. The full suite (`-m 'not external'`) gives 2253 passed and 1 skipped
  (a base test that skips when google-re2 is installed), with `ruff check src tests` clean, on the
  merged base `fdb7c4a`.
- **Not established**: any real-model behaviour (label quality, regex quality, reply validity rate,
  cost per day); compose reading overlays (the L5 arm mapping); `ScopeResult.applied_extraction`;
  `learning.bootstrap` with measured throughput and `learn measure-throughput`; LAN tier and
  `egress.allow_lan`; time windows and invalid-reply limits per route; the `serving.answers` spend
  scope; a counted `rest` for identity S; the 11-day gold set (E1).

## Web UI follow-up: inspect routes, pattern review, ops and log views — 2026-11-04 (A2 S1a/S1b, U2, U3)

Evidence level: **synthetic + contract fakes**. No real deployment, model or customer data.

- `tests/contract/test_inspect_routes.py` (9): on a 2-day synthetic logs generation, `/v2/info`
  reports window, date basis, timezone, served days and one library identity; run reports are
  content-free and newest first; spend is `available: false` without a ledger and exact (micro-USD)
  with one; the patterns view equals the generation snapshot even after the working `library.yaml`
  is overwritten; the review queue lists synthetic noise proposals with examples, matched lines,
  gate checks and effective status, and leaves every library file byte-identical; the graph
  neighbourhood on this generation names the seed and draws no edge (the fixture has no relations),
  and against a contract fake store it returns only stored edges with bounded, deduplicated spans;
  missing communities are reported, not zero; `inspect` scope and the single-tenant 403 hold.
- `tests/contract/test_ui_forwarder.py` (2): a forwarder name passes the Host list on any port and
  its Origin is accepted when Host is rewritten; look-alike hosts, other ports on loopback, `null`
  and non-http origins are 403; unknown Hosts 421.
- vitest 54: review view (XSS strings inert in examples, gate details and reasons; copied command
  equals the shlex-quoted builder), served/archived days and multi-library note, schema-driven tool
  forms (no coercion), tool output → aggregate card with the server's `rendering`.
- Playwright smoke: review queue, runs & spend and communities pages added; 0 CSP violations.
- **Not established**: the real L5 log tools (not on this branch; the Logs view is hidden without
  them), real proposals at the owner's scale (63 noise proposals: rendering cost not measured), the
  rev 3 fallback-state/tier/burn panels (data not present), axe accessibility.

## Fallback rev 4: route tiers, bootstrap, throughput, answers scope, E1b gold set — 2026-11-04 (merged on `dacd166`)

**Evidence level: synthetic data and mocked providers only.** The gold set uses an oracle model built
from the generator's truth. It measures the machinery (clustering, gate, grounding, caps, backlog,
egress), not a model. Throughput figures time the mock transport.

- **E1b gold set** (`tests/contract/test_fallback_gold.py`, 4 synthetic days, 6 planted templates,
  3 singletons, 4 field gaps, 3 injection lines and 2 PII/tracking lines per day):

  | Run | Slot precision | Slot recall | Values span-grounded | PII/tracking refused | Injection patterns | Backlog (deferred clusters, heads) per day |
  |---|---|---|---|---|---|---|
  | no cap | 1.0 | 1.0 (268/268) | 300/300 | 8/8 | 0 | 0,0 all days |
  | cluster cap 4 | 1.0 | 1.0 (268/268) | 300/300 | 8/8 | 0 | (3, 13) on day 1, then 0 |
  | model obeys the injection (`^.*$`) | 1.0 | 1.0 | 300/300 | 8/8 | 0 (gate refuses) | 0 |

  The gold set found a ranking defect. The cluster cap counted clusters that cost nothing (pending
  proposals, cached replies), so the same top clusters filled the cap every day and the deferred
  ones never got a slot (backlog grew 13 → 64 heads over 4 days). The cap now counts only clusters
  that need a call.
- **Routes** (`tests/unit/test_fallback_routes_bootstrap.py`, 13 tests):
  - `lan` needs `allow_lan`, both at config load and at run time before any call;
  - a `lan` route takes work after a down local route, at USD 0;
  - `local` must be loopback;
  - a route outside its UTC window is skipped;
  - a route is marked down after its invalid-reply limit.
- **Bootstrap**:
  - off by default;
  - an unmeasured start is refused;
  - reaching a paid route without a finite `learning.bootstrap.max_usd_per_day` is refused at load;
  - `measure-throughput` (2 calls on the mock) records `throughput.json`, writes nothing to the
    library, and its replies make the next run's clusters free;
  - the measured cap binds clusters that need a call;
  - paid routes are skipped during bootstrap.
- **`serving.answers`** (`tests/unit/test_answers_spend.py`, 2 tests): two answer calls commit on the
  `serving.answers` day ledger and the provider total. The third is refused by the persistent day
  cap before its request, and the request budget is released. Config load refuses a paid answer role
  whose provider has a lifetime cap without a finite answers cap.
- **`patterns learn` run control** (`tests/unit/test_pattern_learning_run_control.py`, 3 tests):
  - the stop file pauses between calls (not a failure);
  - `--output` progress is written per cluster;
  - pending templates are not re-asked.
  The existing day-cap test now sees run 2 skip the two pending templates, and the persisted day cap
  is still asserted in run 3.
- **Merges**:
  - `42627b0`: snapshot round trip through `OverlayStore.rows()`; the snapshot holds row aggregates,
    never field values.
  - `dacd166`: match rows keep L5's `pattern_owner`/`pattern_reviewed`/`gaps`, and the accounting
    keys are `account_owner`/`required_gaps`.
- **Overlay selection**: a second full run under the `regex` engine found that `OverlayStore.rows()`
  without an identity broke ties on `created_at` by identity text, so a rebuilt overlay could lose to
  the stale one under one clock value. It now breaks ties on the manifest's write time, with a test.
  The gold scorer reads the current identity.
- **Suite on the merged head**: `regex` engine (no google-re2 on the path): 2338 passed, 95 skipped,
  0 failed. With the isolated google-re2 path: 2457 passed, 1 skipped and 1 failed. The failure is `test_mcp_stdio_subprocess_and_mounted_streamable_http_agree`, whose
  subprocess environment drops that path. The same file passes 24/24 when parent and child use the
  same engine. `ruff check src tests` is clean.
- **Not established**: real-model label and regex quality, reply validity, cost per day and real
  throughput per tier (the deployment measures these with `measure-throughput`); compose reading
  overlays into rollups (`applied_extraction` from overlays); a counted `rest` for identity S.

## Logs branch integration: history tier merge and a fresh `[dev]`-only test run — 2026-11-04 (branch `v2/logs`)

Evidence level: **synthetic / contract** (test-suite runs only; no service, no paid call).

- Merge of `v2-logs-h1` (H1/H2 and latency, built from `dacd166`) into `v2/logs` at `312e314`: no
  textual conflicts; the fallback slice's `account_owner`/`required_gaps` row keys and L5's
  `pattern_owner`/`pattern_reviewed`/`gaps` both stay. Regenerated `docs/config.schema.json`: no change.
  Full development venv (networkx, regex, fastapi, uvicorn, mcp; no google-re2), `-m "not external"`:
  **2355 passed, 95 skipped, 0 failed**, identical skip set with and without the test fixes below.
- Fresh venv with `pip install "<git archive of HEAD>[dev]"` (core + dev only), bare `pytest -q` from
  the archive directory: before the fixes 59 failed and 89 errors (at `312e314`, as reported); after
  them **1791 passed, 651 skipped, 0 failed, 0 errors**. The skips are tests that need an optional
  extra (communities, a pattern engine, api, mcp, backend clients); the ConfigError for a logs
  profile without networkx and the typed `PatternEngineUnavailable` are unchanged.
- Limitation: the fresh run covers what the core package does without extras; everything behind an
  extra is covered only by the full-venv run (and google-re2 paths by neither).

## Web UI design pass: theme, live timeline, charts, overview — 2026-11-04 (A2 U4-design)

Evidence level: **synthetic + mocked model** (the 10-day synthetic gold set, the loopback fake
streaming model). No real deployment, model or customer data; no paid calls.

- `tests/contract/test_ui_theme.py` (20): default and deployment themes become `theme.css` with
  custom properties only and an SVG logo with its own `default-src 'none'` policy; injection-shaped
  colours, font stacks, series, control characters, logo paths outside the theme directory and
  logos with script, event attributes, `foreignObject`, entities or external references refuse the
  start; the committed `ui/public/theme.css` equals the generated default.
- vitest 62: timeline steps advance only from events (no reasoning text written by the UI), an
  error marks the final step; inline citation chips; answer numbers with exactness and rendering;
  charts (sorting, ≈ hatching, line gaps for days without composed data, quantised heatmap,
  coverage numbers printed apart); log-row adapters; XSS corpus inert in every new component.
- Browser tour (`ui/e2e/serve_synthetic.py --fixture gold --theme …` + `ui/e2e/tour.mjs`, local
  Playwright chromium): overview, streamed log question, structured and lexical tabs, graph tab and
  explorer (stored `native_link` edges), log shortcuts, pattern review, runs: **0 CSP violations,
  0 page errors**. Screenshots and the reference-console comparison stay in the external workspace.
- **Not established**: the deployment theme on the real server, axe accessibility, large graphs
  (> 80 nodes fall back to a grid layout), the with-model coverage numbers (the gold set reports
  regex-only coverage; the gauge shows "model n/a").

## Real-deployment defect fixes: level answers vs source counts, regex length, stored examples, theme path, redactors, anchor and vocabulary, schema violations, index suffixes, planner context, Enter to submit — 2026-11-04 (branch `v2/logs`)

Evidence level: **synthetic and mocked** (the 10-day synthetic gold set through the real pipeline
with the fake source and the sqlite backend; scripted planners and loopback fake providers; jsdom
for the UI). The defects were seen on a real deployment; they are reproduced here with synthetic
data only, and none of the fixes has been re-run on that deployment. No model was called.

- `tests/contract/test_logs_gold_questions.py` (+8): the gold set now plants unparsed FATAL lines in
  its token-only service (2 + 1 on D9, 1 on D8). Level answers carry `source_counts` equal to a
  recount of the plan (source lines per service/tenant, unparsed share, basis and its exactness);
  D8 is answered "No FATAL-level lines in the parsed rows … bounded", never "none … exact"; fully
  parsed ERROR answers stay exact; a level without a count query is named in the notes; the source
  counts equal a pinned `unit_day` aggregate and a kind-less scope over `count_fatal` is still
  refused by the scope guard. `test_logs_surface_parity.py` now also compares `source_counts`
  across the decision modes, the tool call, HTTP and MCP (stdio and streamable HTTP).
- `tests/unit/test_bootstrap_synthesis.py` (+1 per engine): a 60-uuid template synthesises a regex
  over 4,096 characters; it is rejected as `regex_too_long` and the other cluster still yields a
  proposal.
- `tests/unit/test_pattern_learning_loop.py` (+3 per engine): bodies carrying their own host and
  tenant values (one in upper case) yield stored examples (proposed, learned and the decision copy)
  with aliases only, and the library still loads; a regex narrower than the aliases drops those
  examples and, with none left, writes nothing (scripted fake provider, no network).
- `tests/unit/test_config_cli_a2.py` (+2): a relative `serving.ui.theme` resolves against the config
  file from another working directory; `config validate` and `doctor` refuse a missing theme
  (`path: serving.ui.theme`); `rebase_paths` moves the theme with the config.
- `tests/unit/test_redaction_more_redactors.py` (new, 22 on the `regex` engine; RE2 is not installed
  in this venv, so the RE2 runs are not established here): phone numbers in seven international and
  keyed shapes and five near-misses kept (timezone offsets, versions, counts, 20 digits); person
  names in JSON, quoted and unquoted `key=value` text with key boundaries and a configured list;
  13–19 digit ids hashed and joinable, 12 and 20 digits kept; lists of record objects masked whole
  while scalar lists stay; product prose masked to the end of the line with later names kept
  visible, token-bounded and idempotent; config coupling of `product_prose`/`prose_products`;
  stamps change with the configured lists. The existing idempotence and safe-subset tests now
  include the new redactors.
- Vocabulary, error rate and the default day (synthetic): `test_log_intents_detect.py` (+12: failure
  words, named levels, error-rate phrasing, a level word inside a signature id, the error-rate
  scopes and refusal, the undated anchor vs a named window vs `as_of`) and
  `test_logs_gold_questions.py` (+2: error rate per service equals a recount of the plan; an
  undated question on the gold engine, whose latest day is far before the real clock, answers the
  latest served day and says so, while "yesterday" is `source_unavailable` and `bounded`). Two
  existing expectations changed by decision: "Top errors for svc-api" and "Errors for svc-api
  yesterday vs today" now resolve to level ERROR and FATAL.
- `tests/unit/test_index_name_grammar.py` (+4): a numeric service suffix becomes `deployment_id` with
  and without a service vocabulary and with a deployment prefix before the service; short digit
  runs stay in the service; the rule is off by default (synthetic names).
- `tests/contract/test_answer_stream.py` (+2, streaming and not): a loopback fake generator replies
  with a schema-violating answer; the stream ends with `done`, the evidence is returned with an
  abstaining answer, and one warning names `citations (list_type)` and the stop reason without the
  reply text (mocked provider over loopback HTTP).
- `tests/contract/test_logs_planner_context.py` (new, 6; scripted planner, no model call): "what are
  issues that need my attention" plans `log_top` with ERROR and FATAL in cascade and llm modes
  without a planner call; a phrasing the rules do not know reaches the planner in a cascade, whose
  payload carries the pack (arms, kinds, library entries incl. `process.crashed`, served days,
  vocabularies, all ten intents with input schemas, few-shot) and whose choice is compiled like a
  tool call; the pack is within its bound (about 11.7k characters on the gold set) and holds no
  API key value, key variable name, endpoint or line text; a tight bound cuts lists in order and
  names the cuts; a free scope from the planner is still refused (`intent_required`); a
  deployment few-shot file replaces the profile set and an invalid one is a `ConfigError` at load.
- UI: vitest 65 passed (+3 in `ui/src/test/keys.test.tsx`: Enter submits, Shift+Enter keeps the
  newline default, `isComposing`/keyCode 229 never submit; a Logs shortcut runs on Enter; the
  Patterns filter fields reload on Enter like the Reload button). Built with `npm ci` and
  `npm run build` in a copy under the external workspace; the rebuilt static bundle is committed.
  No browser run of this change.
- Full suite after all fixes: `not external` 2442 passed, 99 skipped, 0 failed (project venv, the
  `regex` pattern engine; google-re2 is not installed there, so RE2-only tests skip). Fresh
  `[dev]`-only install of the `git archive` of the branch head in an isolated venv (non-editable;
  the package imported from site-packages): 1814 passed, 563 skipped, 0 failed, 0 errors. ruff clean.

## Second real-deployment smoke fixes: Ask-path day, FATAL-only source counts, error rate, deployment ids — 2026-11-04 (branch `v2/logs`, base `6f8e6a4`)

Evidence level: **synthetic and mocked**. The four defects were reported by a smoke test on a real
deployment; they are reproduced here only with the synthetic 10-day gold set composed through the
real pipeline (fake source, sqlite backend, hash embedder), a scripted planner (no model called)
and a pinned engine clock. None of the fixes has been re-run on that deployment yet.

- `tests/contract/test_logs_ask_path_fixes.py` (new, 17). Ask path with the engine clock pinned
  after the last gold day: an undated "which services had FATAL"/"… ERROR" through `Engine.answer`
  (rules and cascade) and `answer_stream` carries no clock `as_of`, answers the latest served day and
  says "latest available day: …"; "yesterday" reads the clock (answered when served); "yesterday"
  and "today" after the last served day are `source_unavailable`, `bounded`, rendered "No composed
  data for index date <that day>" plus the latest available day (never shifted); a caller's `as_of`
  still disables the anchor; the cascade reaching the scripted planner (with and without the planner
  copying `time_anchor` into `as_of`) anchors the same way, and the payload says
  `time_anchor_source: clock`. A second gold build models the deployment's count setup (one
  `failures` query over ERROR+FATAL, one WARN query, no single-level FATAL query) and splits
  t01/svc-api into two numbered deployments: FATAL alone on D8 is "No FATAL-level lines in the parsed
  rows … bounded" with `coverage: superset` source counts (the base code looked up single-level queries
  only, so this setup had no cross-check; not re-run on the base); FATAL, ERROR, ERROR+FATAL, WARN and WARN+FATAL each use the
  unit source counts (bounded exactly where svc-legacy's unparsed FATAL lines are, WARN exact); the
  error rate works with the combined query; `deployment_id` is on `unit_day` (a pinned aggregate by
  deployment equals the split), `log_top by: deployment`, the `deployment_id` filter, the error rate
  by deployment and `log_tenants by: deployment` match a recount of the plan.
- `tests/contract/test_logs_gold_questions.py` (+2 net). The error rate is now source ERROR+FATAL
  lines over source total lines per service (svc-api 36/110 = 32.73% on D9, svc-legacy 7/47 token
  match, approximate) and equals a recount of the plan; on the capped day the tenant totals are the
  source totals (greater than what was pulled) and a level-field-only rate is exact; a WARN rate
  without a WARN count query is refused. Changed by decision: WARN answers on the two days whose
  token-only units hold lines with no parsed level are now `bounded` with that share (were exact);
  the old error-rate test (parsed lines / pulled lines) is replaced.
- `tests/unit/test_log_intents_detect.py` (+12 net): error-rate scopes read `unit_day` count and
  total fields only, refusal without an exact count query (a superset query would over-count),
  `count_fields` preference, `count_plan` for eight level/query combinations, deployment filter and
  grouping (and "deployments"/"instances" in the rules grammar), and `detect(pin=False)` keeping a
  clock day out of the call.
- Full suite: `not external` 2473 passed, 99 skipped, 0 failed (was 2442 passed, 99 skipped at
  `6f8e6a4`; project venv, `regex` engine). ruff clean. Fresh `[dev]`-only install of the
  archive of `9bebebf` in an isolated venv (non-editable, package imported from site-packages):
  1826 passed, 582 skipped, 0 failed, 0 errors; the gold-set tests skip there (no communities
  extra), so the new contract tests are established in the project venv only.

## Live-deployment follow-ups: rankings by source counts, rendered ranking answers — 2026-11-04 (branch `v2/logs`, base `84bf49d`)

Evidence level: **synthetic and mocked**. Both defects were seen on a real deployment ("which
service is failing the most" left out a service whose errors were almost all unparsed; pure
ranking answers read "No matching evidence was retrieved."). They are reproduced here only with
the synthetic 10-day gold set composed through the real pipeline (fake source, sqlite backend, hash
embedder) and a scripted generator (no model called). Neither fix has been re-run on that
deployment yet.

- `tests/contract/test_logs_rendered_answers.py` (new, 15). A gold variant plants 300 ERROR lines
  in a line style no pattern reads in the token-only service for one tenant on D10
  (`GoldSpec.error_flood`). Before the fix (reproduced at `84bf49d` with a scratch script) that
  service ranked last of three by parsed lines (3) while the rendering listed 301 source-counted
  unparsed lines. Now `log_top` by service at ERROR+FATAL ranks it first with its source count (304,
  equal to a recount of the plan), parsed lines and unparsed share alongside, `ranked_by:
  source_counts`, "ranked by source counts (the parse is incomplete)", `bounded`; the whole ranking
  equals the plan's source counts; `top_n: 1` keeps it; by tenant the planted tenant ranks first; a
  call without `by` behaves like `by: service`; a signature ranking keeps the parsed rows and names
  the unparsed share and the service; a complete parse and an `owner: reviewed` filter keep the
  parsed ranking. Answers: without a generator (rules and cascade) the answer text equals the log
  aggregate's rendering and cites it; with a scripted generator a sentence restating rendered
  numbers is appended (rules and cascade), a sentence with a foreign number is dropped
  (`answer_number_mismatch`, `strip`), two sentences are dropped, a refused budget skips it; with an
  evidence budget too small for the aggregate (the live failure: the aggregate was omitted and the
  generator saw nothing) the answer is the rendering, never "No matching evidence".
- `tests/unit/test_rendered_answer_contract.py` (new, 8): `numbers_outside` (digits, decimals,
  dates, number words), `labelled`, `rendered_answer` claims/citations, kept/dropped narratives,
  `fail` mode raising, refused aggregates `insufficient_evidence`.
- Changed by decision (4 existing tests updated, same edge cases): FATAL by tenant on D9 and FATAL
  by service on D8 (only unparsed FATAL lines) now rank by source counts (the D8 answer was "No
  FATAL-level lines in the parsed rows …" and is now svc-legacy with 1 source line, 0 parsed,
  bounded); the combined ERROR+FATAL count query ranks ERROR+FATAL by source counts while FATAL or
  ERROR alone (not separable from it) keep the parsed ranking; the llm-planner parity test expects
  the source ranking.
- Persona-evaluation follow-ups (same evidence level; the categories were read from the private
  evaluation report, no question text, names or numbers copied). `tests/unit/test_contract_labelled_counts.py`
  (new, 12): labelled counts ("approximately", "about", "at least", "in the parsed rows",
  "bounded") on approximate and bounded aggregates pass; unlabelled counts abstain; a labelled
  wrong number is `answer_number_mismatch`; a dropped predicate, a complete list and an absence
  still abstain; sentence boundaries keep identifiers and decimals whole, and a strip around
  `gen.error_word`-style identifiers leaves only whole sentences (the old splitter cut at the dot,
  which matches the garbled "… were gen." texts). `tests/unit/test_log_intents_persona_fixes.py`
  (new, 13): customers/clients/accounts rank by tenant; a tenant reaches `log_trend` and
  `log_compare`; increase/drop/change/worse questions are comparisons; later-first periods compile
  in date order. `tests/contract/test_logs_rendered_answers.py` (+2): a tenant per-day question and
  an "increase … compared to …" question on the Ask path keep the tenant, equal a recount of the
  plan and render the change earlier → later. `test_compare_two_days` (3 cases) now expects date
  order and the signed change (changed by decision). Whether the deployment's truncated texts were
  also partly `max_tokens` cut-offs was not measured (the evaluation stores answers cut at 300
  characters); the splitter defect alone reproduces the observed fragments.
- Not done: a re-prompt repair of planner schema violations (the slice rules forbid repair and
  fallback without an owner decision); the planner still abstains with `schema_violation`.
- Full suite (project venv, `regex` engine): `not external` **2523 passed, 99 skipped, 0 failed**
  (was 2473 passed, 99 skipped at `84bf49d`). ruff clean. Schema unchanged (`write_all` no diff).
  Fresh `[dev]`-only install of the archive of `af9dffd` (isolated venv, non-editable): **1859
  passed, 736 skipped, 0 failed, 0 errors** (skips: extras not installed — regex/re2 pattern
  engines, communities, backend clients, api).

### 2026-11-05: client issue tables and planner-selected dimensions

Local synthetic validation in the external owner test workspace: 227 affected Python tests passed
(scope/types, log question binding, gold answers, rendered contracts, planner context and native
aggregation). An additional four-dimension native composite transport test passed with its
26-test module. The gold source independently checks client/signature and
client/service/signature/severity counts. No model/provider calls were needed for these cases.
Ruff `src tests` passed. UI suite: 96 tests passed; TypeScript/Vite build passed. Browser checks
against the synthetic engine verified the client table has only Client / Error type / Error count,
one primary table, collapsed supporting patterns, and no mobile page overflow.

The planner can specify ordered computed columns without dropping grouping dimensions; invented
columns are rejected. The general structured scope supports up to four grouping fields, preserving
backend cardinality limits. Overview affected-service/client panels now request ERROR/FATAL counts;
parser coverage and indexed freshness are explained separately. Contract status is in answer
diagnostics, not presented as a semantic correctness guarantee. A failed view bundle displays a
reload action. These are implementation/synthetic checks; they do not establish complete source
coverage or correctness for every natural-language question.

Graph-specific validation: real synthetic HTTP + browser checks passed for the canvas, six evidence
columns, 18 combinations / 168 fixture events, client filtering, and desktop/mobile layout without
page overflow or JavaScript errors. The graph sums only returned bounded rows, scopes service nodes
to clients, and shares issue/severity nodes without inventing client paths. ERROR/FATAL signals are
not monitored alert states; templates are not raw event quotes. Client suggestions are capped at 50,
with manual client entry. Combined UI suite remains 96 passed after graph fixes. A browser-injected
missing GraphView bundle showed the recovery action and loaded the view after reload. Config/schema
checks: 52 passed. Final scope/question regression subset after compound-dimension binding: 106 passed.

Release artifacts: core and UI wheel/sdist builds passed. Deployment checks passed on staging and
live service with the previous data generation retained: readiness, UI asset, CSP, Host/Origin guards,
unknown-scope refusal and native graph regression. The new four-dimension log tool returned valid
bounded rows. Live browser checks passed for the client table, client graph/filter/evidence, mobile
layout and Overview failure-count requests. These read-only integration checks do not imply a new
ingestion cycle, improved live regex coverage, or correctness of every planner question.

Targeted live acceptance: all 32 client/type counts matched independent stored partition records;
service regression matched all 48 service/type counts. Both answers retained table columns,
supporting template identifiers and missing-day disclosure, with zero model calls or added spend.
This validates these aggregation patterns over the available day, not all clients' full histories.

### 2026-11-05: outage route and incomplete-provider diagnostics

Synthetic affected suite: 279 tests passed (gold log counts/planner context, outage investigation,
HTTP/SSE, provider contracts, config and scope binding). Additional arm regression suite: 20 passed.
The exact wording `last 3 day outage` was tested at a fixture date containing a planted incident:
community and graph both run, graph paths exist, a cited table passes evidence checks, and any model
call fails the test. Unknown subjects and mismatched clients/services return insufficient evidence
instead of unrelated incidents. Provider truncation is mocked: the terminal event carries a safe
stage/message and logs limit/stop diagnostics without source text. Original production truncation
cause was not recoverable from the old streaming logs; no claim is made that dense context overflow
was established. UI tests: 96 passed; TypeScript/build passed; user-facing contract jargon replaced.

Deployed integration: the exact outage query selected community and graph only, returned four
matching incident documents/citations, and made zero model calls. Independent comparison with
stored interval metadata passed, including incidents starting before the requested window. Staging
API elapsed about 0.06 seconds on this small existing generation; this is not a general latency claim.
Final browser checks passed on desktop/mobile, with readable pattern labels/UTC intervals, working
citations, no page overflow and no JavaScript errors. No new communities, index data or budget limits
were changed. Community grouping quality and confirmed outage detection were not established by
these identity/routing checks. A final four-case outage regression and release builds passed.

### Safe secret-key values (2026-11-05)

Synthetic regression coverage now includes terminal secret-key families, JSON/Python-style quoted
values with punctuation, backslash/doubled-quote escapes, unterminated values, adjacent diagnostic
preservation, descriptor-field negatives, idempotence, content-free findings and the learning egress
second pass. The affected safe-redaction, logs-persistence and pattern-learning suites passed
**95 tests**, with **21 optional-engine cases skipped** in the local environment. The safe-redactor
unit file itself passed all 56 cases there. A separate RE2 run passed 21 synthetic key/value cases,
including long quoted values; no engine substitution was used. `ruff check src tests` passed.
All temporary/cache files were outside the repository and Python bytecode was disabled. These checks
establish the specific key/value masking behavior, not that arbitrary payloads are free of private
content. No provider calls or deployment changes were made by this validation.

### Deterministic shared-error communities (2026-11-05)

The focused shared-error unit/compose contracts, existing logs graph/community contracts, arm
contracts and v2 configuration suite passed **85 tests**. `ruff check src tests` passed and the
configuration schema was regenerated. The isolated synthetic gold compose produced **20 daily
shared-error communities with 114 scoped supporting documents**, alongside the existing co-firing
incident. These counts describe the synthetic fixture, not production quality or relevance.

Checks cover ERROR/FATAL-only membership from real joint severity/hour cells; exclusion of INFO,
WARN, noise, missing severity and configurable generic signatures; observed support thresholds;
separate daily community IDs and a stable family ID; deterministic bounded selection with reported
omissions; exact/estimated count disclosure; recomputed all-member statistics; grounded scoped
entity mentions without duplicate graph links; missing-day reporting; unchanged-compose reuse and
recipe invalidation. A scoped arm check restricted tenant, service and day and injected an unsafe
global label/statistic: only eligible member documents survived and neither the global label nor
statistic appeared in the result. All test indexes, temporary files and caches were external, with
bytecode disabled. No provider calls or live changes were made by these checks. Shared membership
does not validate a common cause, a simultaneous outage, or monitored alert state.

Shared-error human-context follow-up: **8 focused unit/compose tests passed**. An approved learned
pattern with an opaque ID received its descriptive issue name, explicit unreviewed status, and
stored expression with a marked 240-character summary preview. Its example text was absent from
the support documents. Proposed patterns were excluded. Changing only the issue-name provenance
left the matching identity and supporting-document IDs unchanged but rebuilt the composed generation;
a repeat compose reused it. These are synthetic contract checks, not validation of learned labels'
semantic accuracy. No model calls or live changes were made by these tests.

### Shared-error question path and private builder check (2026-11-05)

The deterministic shared-error route uses scoped community and graph evidence, produces a cited
client/service/day/severity table, labels sampled counts and missing indexed days, and makes no
model calls. The integrated affected test selection passed 77 tests; the final window-predicate
selection is recorded below after completion. Ruff passed for src and tests.

An independent private aggregate-only builder check over 7,049 dated hourly rows found 10 daily
communities in five stable families, 54 supporting documents, and 26 distinct client/service/error
relationships across two indexed days. It found no missing or invented memberships or generic
catch-all documents. All document counts matched the hourly source; 33 documents were approximate
and 21 exact. Three one-line differences from a separate level-rollup projection reflect existing
approximate-rollup rounding, not builder changes. This validates composition on that bounded data,
not coverage of unparsed logs, retrieval completeness, or confirmed incidents.

The final window-predicate tests passed 3/3. Core and UI wheel/sdist builds succeeded.
The private deployment rebuild picked up pre-existing learned library additions and reparsed its
two retained days. Independent validation against those updated hourly inputs found 14 daily
shared-error groups, seven families and 64 support records, with all counts and joint memberships
matching; 33 records were approximate and 31 exact. Three deterministic live API checks used zero
model calls: global selection returned 20 of 32 eligible records with a limited-selection notice,
a client filter returned 5/5, and a service filter returned 12/12. Client-filtered payloads contained
no other client names or global support counts. Two added families use learned, unreviewed patterns;
these are regex classifications, not human-confirmed incidents.

The live browser check verified the seven-column shared-error table, evidence navigation, zero
model calls, zero JavaScript errors and no page overflow at a 390-pixel viewport. A second compose
returned unchanged with the same generation. The private 30-minute systemd refresh timer is enabled;
it imports completed local exports, skips active pulls, resumes saved selections, uses the served
code and stores, and invokes no pattern learning or provider generation. No source corpus, endpoints
or operational scripts are included in this public report.

### Diverse regex-learning samples (2026-11-05)

Synthetic tests for balanced tenant/day/message sampling, duplicate removal, same-scope negative
examples, explicit independent holdouts, existing learning behavior, promotion gates and CLI passed:
65 learning/sampling tests plus 30 gate/CLI tests, using both regex and RE2. `ruff check --no-cache src
tests` passed. RE2 was installed only in the external test workspace. A holdout that cannot be formed
is reported unavailable; repeated exact messages can still use the existing deterministic synthesis
path and must not be represented as independently validated novel variants. No provider-quality or
production-coverage claim follows from these synthetic checks.

The follow-up build includes balanced proposal sampling plus human-readable shared-pattern labels
and explicit origin/review status. The integrated selection passed 59 tests with 21 optional RE2
skips locally; the learner agent separately ran 65 learning/sampling and 30 gate/CLI tests with RE2.
Final browser checks confirmed readable labels and the provisional-label note. The first automated
import completed and the running server followed its new generation without restart. An older day
was correctly deferred while a source export remained active; its prior data stayed served.

### Separate collection, scanning and extraction metrics (2026-11-05)

The focused metrics, parser, gold log-tool and overlay suites passed **97 tests**; the final UI suite
passed **98 tests**, and TypeScript validation passed. `ruff check src tests` passed. Synthetic checks cover
selected-tier collection versus the full matching indexed-source denominator; separate source
document/assembled-head units; noise exclusion; unknown legacy and partly recorded aggregates;
empty denominators; inconsistent per-unit counts; and weighted totals rather than averaged ratios.
The coverage answer selects four table columns instead of dumping technical counters.

A real synthetic API/browser smoke passed on desktop and mobile: all three stage cards, four-column
service table, explicit indexed-scope limitation, collapsed recognition diagnostics, no JavaScript
errors and no page overflow. Existing Overview grid/date-control/heatmap mobile overflow was corrected
during that check. The UI production build passed. Artifacts remain in the external test workspace;
no provider calls or live deployment changes were made by these tests. These metrics cover available
indexed units only and do not establish completeness across unimported or undiscovered ELK indices.

### Full-collection and coverage-metrics deployment follow-up (2026-11-05)

The three-metric change passed 97 affected Python tests and 98 UI tests, full `ruff check src tests`, TypeScript checking, and core/UI wheel and sdist builds. The subsequent correction removed an Overview label that confused the compose reference date with source freshness; its affected dashboard test and UI build passed. An independent review checked mixed/unknown counters, invalid per-unit counts, selected-tier collection, and exclusion of noise from useful extraction.

Deployment checks confirmed readiness, the new UI asset, host/origin guards, refusal of an unknown scope, and nonempty graph results. A browser check against real indexed historical-day responses confirmed three metrics, a four-column service table, separate collapsed recognition diagnostics, and no desktop/mobile JavaScript errors or horizontal page overflow. This checked rendering and counting contracts, not retrieval relevance or extraction-field accuracy.

The private operational wrappers, outside this distribution, passed 21 synthetic collector checks and 20 synthetic import-adapter checks. Those covered paginated all-record capture, exact snapshot reconciliation, timeout/shard/cursor failures, checksum/recipe resume, long selected fields, secret masking, full-versus-sampled metadata, new service dimensions, and an actual synthetic pipeline seal. Collection was restarted without per-index or INFO sampling caps. Full exports are imported only after integrity checks; the long-running rerun is not a measured claim that all source data has already been collected or indexed. The indexed text mapping still has its separately reported content limits.

### Exact matcher reuse and timestamp-bearing formats (2026-11-05)

The affected parser, cache, partitions, delta-reparse, extraction, pattern-write, coverage and shared-error selection passed 92 tests with RE2. After adding a sealed-diagnostics assertion, all nine partition tests passed. Full `ruff check src tests` and the synthetic benchmark script passed. Cache keys include full matcher input and relevant scope; probation and adaptive bypass avoid retaining mostly unique inputs. Every occurrence, timestamp, count, evidence identity and accounting result is retained. Approved formats are parsed afresh before exact body-result reuse; unknown JSON layouts are not timestamp-normalized. Timeouts and exceptions are not cached as successful matches. Thread-local body caches and process-local head caches are bounded.

A private replay of 79,192 previously masked heads from five services used three alternating baseline/cached rounds with fresh RE2 libraries. Every per-record match result agreed. Median matcher-only speedups were 1.22x, 1.08x, 1.30x and 3.24x for four services; the fifth had no cache hits, so its timing difference is not evidence of benefit. These bounded retained windows do not establish whole-service performance, extraction accuracy or download speed. Diagnostics are stored separately in checksummed `parse_work.json`, leaving semantic aggregates unchanged. No provider calls or additional source-record downloads were used for this replay.

The core wheel and sdist built successfully. Staging and deployed API checks both passed readiness,
UI asset loading, CSP, host/origin guards, unknown-scope refusal and a nonempty graph (83 nodes,
98 edges). Deployment activates the cache for subsequent parser runs; it does not establish a
measured end-to-end ingestion improvement or replace the full-record downloader with aggregations.

### Exact grouped observations (2026-11-05)

The integrated grouped/raw parser, partition, delta-reparse, coverage, shared-error and question
contract selection passed 191 tests with RE2; full `ruff check src tests` passed. Independent synthetic
checks compare 18 downloaded records representing 84 occurrences across three joint tenant/service
scopes with all 84 expanded records. Signature, severity, hourly and exception counts agree; evidence
contains actual downloaded records only. Tests also cover weighted latency statistics, physical versus
represented accounting, unknown or inconsistent coverage, scope/time/identity failures, explicitly
keyed host/container provenance, exemplar-only offsets, and refusal of model fallback before provider
work. These checks establish contract behavior, not production extraction accuracy or throughput.

Seven initial private-adapter synthetic checks passed, including an actual grouped partition seal,
count/hash/scope/duplicate rejection and precedence of full records over grouped exports over older
samples. Live source compatibility is separate: bounded read-only aggregation probes succeeded, but
snapshot certification for a complete paginated export has not yet been established. No claim of
production grouped-download completeness follows from the first-page pilot.

Final private import-adapter validation passed 32 tests (12 grouped and 20 prior full-import checks),
including keyed host/container and exemplar-offset persistence, scoped manifests, continuation
isolation and an actual synthetic seal. The capture/optional-wrapper/transport selection passed
19 synthetic tests with no skips. These exercise all-page reconciliation, raw fallback, overlapping
bucket deduplication, owned snapshots, projection restrictions, partial/shard/cursor failures,
atomic masking/sealing, and the default compression gate's same-snapshot raw fallback. Full Ruff
passed again. The final source guard and snapshot execution have not passed real-service integration.

The new core and private importer were deployed after staging and live readiness/UI/graph/access
guard checks passed. The refresh dry-run passed and its timer remained enabled. At this initial
deployment, the grouped CLI was installed but unscheduled and refused unverified source capability
before connecting. The source rejected the tested snapshot request and denied version/statistics
discovery; full-record collection continued. This initial result established deployed ingestion
support, not an enabled grouped downloader or measured production transfer reduction. Core wheel
and sdist builds passed. The subsequent compatible activation is recorded below.

### Compatible adaptive collection (2026-11-05)

The adaptive implementation removes the PIT prerequisite for a bounded single-response grouping
attempt. Exact complete groups and any fully included raw fallback examples are accepted from that
response alone. Otherwise the candidate output is discarded and a fresh raw scroll supplies the
entire unit. The snapshot/count relationship, all-page unique-ID reconciliation and observed versus
represented units remain explicit. Subsequent scroll `terminated_early` flags are recorded and
permitted only with full reconciliation; initial responses remain strict. A live guard compilation
failure caused by unsupported Painless string escapes was corrected using numeric character checks.

The affected capture, compatibility, private-wrapper and transport selection passed 34 synthetic
tests, including fresh-snapshot population changes, discarded partial group candidates, missing
keyword/raw fallback, duplicate raw memberships, incomplete scrolls, initial versus subsequent early
termination, atomic export behavior and verified full-file hardlink reuse. Full Ruff passed. A bounded
real-source probe then collected exactly 26,379 unique documents, reconciled source/stratum totals,
masked the export and cleared the scroll. It took 123.83 seconds with 500-record raw pages and selected
raw fallback because an unsafe bucket required pagination. This validates compatible raw capture,
not a live grouped-output path, extraction quality or aggregation compression. It is one measured
index run, not an end-to-end ingestion benchmark or a comparison against a concurrent baseline.

The private adaptive loader was subsequently activated. Its configured
raw page size is now 1,000, separate from aggregation page size; later measurements are recorded
in the hybrid validation below. Completed full-index exports are validated and reused without another
download, including completed indices from a service whose full manifest was not yet sealed. Those
adopted records retain ordinary multiline processing. The activation and continuing backfill do not
establish that all source services or dates have finished collection.

Final release verification repeated the affected selection: 34 tests passed in 0.14 seconds, full
`ruff check src tests` and the capture script passed, and core wheel/sdist builds succeeded. All
test working files, caches, exports and build artifacts remained in the external test workspace.

### Service-pattern promotion replay (2026-11-05)

A private promotion added 51 patterns (two ML patterns, 29 other-service patterns and 20
addon-service patterns), increasing the approved library from 120 to 171 entries: 138 message,
15 format, 12 noise and six continuation patterns. Replay against retained November 2–3 records
produced 231,542 additional useful parsed records and, separately, 18,848 additional noise
classifications. The replay recorded no changes to previously parsed/noise classifications, typed
field errors or wrong-service matches. These are aggregate results on stored records, not evidence
of source completeness or completed extraction coverage for every service. The addon fixed-literal
patterns had only one retained day of evidence; their validation establishes observed recurrence,
not generalization to unseen days or message variants. Refresh was still running when this note
was recorded, so these replay gains are not claimed as newly served coverage metrics.


### Same-snapshot hybrid grouping (2026-11-05)

The hybrid implementation combines a bounded global candidate aggregation with a scroll over its
exact complement in one initial search snapshot. Candidate selection counts are hints only. The
source required bounded scripted terms rather than composite below global/filter parents. Live
checks also found that numeric `String.indexOf` arguments compiled but raised a dynamic-method
exception on older Painless; the previous guard classified those documents as unsafe and used raw
fallback. String parameters now handle newline/CR/tab without that exception. The environment guard
matches the existing index-derived canonical projection; it does not retain source `fields.env`.

Initial capture-only checks reduced physical rows by 17.23% and 42.82%, but the installed importer
correctly rejected a representative that was a continuation fragment. Those results are superseded
and are not valid end-to-end compression claims. The corrected selection uses the actual full-record
privacy projection, canonical mapping/ANSI cleaning/truncation and configured multiline classifier.
Its recipe identity includes mapping, multiline settings, approved continuation entries, engine and
privacy identity; unrelated message-pattern promotions do not change the head policy. At most 64
candidate keys are considered to retain at most 16 valid standalone heads. Rejected keys remain in
the raw complement; the importer’s standalone-head rejection was not weakened.

Two already completed source indices were recaptured with the standalone-head policy, before the
final continuation-evidence guard below, using masked exports in an isolated external workspace and
no full evaluation-arm rerun:

| Bounded source family | Exact source occurrences | Grouped occurrences / groups | Raw complement | Downloaded rows | Pre-final-guard hybrid seconds | Earlier raw fallback seconds |
|---|---:|---:|---:|---:|---:|---:|
| Mixed messages | 26,379 | 3,349 / 396 | 23,030 | 23,426 | 59.97 | 71.47 |
| More repeated messages | 21,847 | 6,413 / 120 | 15,434 | 15,554 | 45.08 | 59.004 |

The corrected rows fell by 11.19% and 28.80%, respectively. Source strata, candidate guards, zero
omitted buckets/count error and all unique raw IDs reconciled exactly. The runs used 27 and 19
transport requests and transferred 13,638,626 and 14,590,575 bytes. Relative to an ordinary raw
scroll with the same 1,000-record page size, their request plans save two and five requests including
candidate selection; discarded candidate searches and clears are included in the enforced positive
net-request-savings gate. The private hybrid ratio gate is 1.05 in addition to that request gate.
The earlier raw fallback runs include their rejected candidate overhead, and these timings predate
the final raw-context checks, so they establish neither a controlled baseline comparison nor final
policy throughput. The prior 500-record mixed-family capture took 123.83
seconds. None of these bounded results establishes all-service compression or source completeness.

Both corrected exports passed the installed private grouped reader, FileLogsPipeline mapping and
multiline assembly, typed group validation, and actual pattern parsing offline. The mixed-family
export produced 21,586 unique mapped/matched heads, 1,840 explicit fragments and zero malformed
records; its 396 groups carried 3,349 occurrences. The repeated-family export produced 9,677 unique
mapped/matched heads, 5,877 fragments and zero malformed records; its 120 groups carried 6,413
occurrences. Weighted matched heads plus explicit fragments reconciled to 26,379 and 21,847 source
occurrences, respectively. Fragment isolation is intentional: compressed streams do not establish
which removed head owned a continuation. These checks establish correct accounting and compatible
parsing, not relevance or useful-extraction coverage. No live index writes or provider generation
were involved.

A further offline comparison parsed each corrected hybrid export and its existing completed full raw
capture with the same current library and normal full-record assembly. Both indices had zero changed
per-signature totals, signature/hour cells, or level/status/signature/hour cells. Mixed-family weighted
heads were 24,539 in both paths, with 4,302 signature occurrences; repeated-family weighted heads
were 15,970 in both paths, with 3,720 signature occurrences. Full assembly joined the 1,840 and 5,877
continuations instead of reporting them as fragments. Thus aggregate counts matched on these samples,
while rendered stack/evidence assembly differs explicitly. The comparison does not establish behavior
for other continuation contexts or services.

The final policy additionally rejects candidates that could start traceback context and examines
masked canonical raw records before publication. A traceback/stack marker or any configured
continuation other than a purely structural closing delimiter discards the staged hybrid attempt
and starts a fresh full raw scroll. Meaningful indented JSON/payload text is not considered benign.
The same rule guards buffered raw rows in the original single-response path. This preserves
meaningful error-investigation evidence; full-stack equivalence is not claimed for structural-only
fragments.

Offline auditing of the already captured exports found **zero** meaningful continuations in the
mixed-family complement, so its 11.19% physical-row reduction remains eligible under the final
policy. The repeated-family complement contained **5,877** meaningful continuations, so that family
must use full raw capture. Its earlier 28.80% reduction is superseded as a deployment benefit despite
matching aggregate counts. No additional source capture was needed for this audit. The final
policy’s end-to-end runtime has not been measured.

The affected capture, compatibility, private-wrapper and transport selection passed **65 synthetic
tests** in 0.32 seconds; full Ruff passed. Coverage includes truncated/unsafe candidates, exact
population partitioning, partial/duplicate/overlapping scroll records, initial versus subsequent
early termination, one bounded fresh-snapshot pruning retry, Unicode tuple encoding, canonical
environment projection and cross-index rejection, staging disk reserve, configured head policy,
net-request overhead, canonical continuation preservation (including ANSI-cleaned payload text), a
traceback regression proving preserved final signature counts and no partial group publication, and
explicit prior-raw-recipe migration with checksum/count/projection
failures. Completed exports from an approved earlier raw-only recipe are reused without source
calls; no generic prior-recipe trust or reuse of older grouped observations is enabled.

### Normal-operation extraction follow-up (2026-11-05)

A subsequent private promotion added 20 entries (one format and 19 message patterns), bringing the
combined promotions to 71 entries and the approved library from 120 to 191: 157 message, 16 format,
12 noise and six continuation entries. This follow-up targeted newly retained normal-operation
records rather than repeating the earlier error-tier evaluation. It recognized bounded operational
outcomes and typed counts; request payload headers, fragments and debug chatter were not counted as
useful extraction.

The follow-up replay covered 3,685,076 actual stored heads from one service and identified 3,472,494
new heads by stable source-record IDs against the earlier retained version. It added 330,924 parsed
heads, all from that new-data delta. Across the two sequential promotion batches, measured replay
gains total **562,466 additional parsed heads**, with **18,848 benign-noise classifications reported
separately**. Previously parsed/noise classifications were unchanged; the follow-up recorded zero
typed-field errors, zero failures in 3,300 out-of-scope service-selector checks, and zero matches on
136 negative examples. No model calls were used for this follow-up.

These are retrospective stored-record replay results, not a controlled source-wide benchmark.
Family discovery used aggregate shapes from the full new-data delta; tenant/body partitions therefore
provide retrospective validation rather than an untouched blind holdout. Fixed literals establish
recurrence, not generalization to novel message text. One single-tenant, single-body candidate was
withheld. The new-data delta still contained 2,067,073 unparsed heads and 72 format-only heads after
the accepted follow-up patterns. Promotion and subsequent serving refresh do not establish completed
collection or extraction for all services; completeness, scanned heads and useful extraction remain
separate metrics. Corpora, examples, candidate regexes and detailed reports remain in the private
external workspace; this document records aggregate results only.

### Private collector activation and bounded proposal pilot (2026-11-05)

The final conservative adaptive collector was activated at 11:05:12 UTC, with one collector process
active and 5,340,564 previously completed source occurrences preserved through the explicit prior-raw
recipe allowlist. This activation does not establish completed backfill or newly served coverage.
A read-only inspection of its existing request log through 11:07:58 UTC found 74 requests since
activation, all HTTP 200: discovery, mapping, candidate selection, hybrid snapshot, scroll cleanup,
a fresh raw snapshot and 68 scroll pages. The process remained active. This check issued no source
requests and demonstrates request health only; it does not establish final index sealing, compression
or answer quality.

A separate private pilot promoted one reviewer-narrowed Haiku proposal after 15 synthetic worker
tests and independent privacy/budget checks. Stored-record validation added 183 INFO lifecycle events,
bringing the approved library to 192 entries: 16 format, 158 message, 12 noise and six continuation
patterns. This is a bounded stored-data result, not source-wide extraction coverage. The hourly
proposal-only timer was enabled with its next run scheduled for 11:15 UTC; existing limits remained
$0.50 per run, $1.00 per day and 150 calls. Timer enablement is not evidence that a scheduled run
completed or that proposals are automatically promoted. These are private operational deployments,
not newly shipped public package features. Serving refresh completed at 11:07:59 UTC: both retained
day partitions were sealed under the 192-entry library and the new generation was served, with 24
shared-error groups. Final smoke checks passed readiness, UI asset delivery and CSP, Host rejection
(421), Origin rejection (403), unknown-scope refusal, and a graph response containing 125 nodes and
140 edges. These checks establish serving and access behavior, not complete source coverage,
community quality or answer relevance.

The activation note changes documentation only. The previously passing 65 affected tests and Ruff
results were retained without repetition, and the final documentation source archive was rebuilt in
the external test workspace.

### Partition parse pool reuse (2026-11-05)

One lazy spawn pool now serves all units of a partition. JSON serialization is backpressured to
at most twice the worker count of pending batches. Single-worker and small-unit serial parsing,
record order, typed values, accounting, evidence, and worker-library identity checks remain intact.
The partition's accumulated records and final aggregates are still retained in memory.

Validation: **94 affected tests passed** (26 parser/cache/pool tests and 68 grouped/delta-reparse
tests); `ruff check src tests` passed; wheel and source archive built. New synthetic tests verify
one pool across units, delta-reparse parity, bounded serialization/submission, and pool cleanup on
worker failure. These checks required no external database or model calls.

An authorized retained-data timing check stayed in an isolated external workspace with a copied
library. The first sample contained 90,000 heads across four units and three service shapes:

| Parser | Workers | Wall time | Sampled peak process-tree RSS |
|---|---:|---:|---:|
| Previous | 1 | 2.22 s | 622 MiB |
| Previous | 4 | 5.33 s | 1,055 MiB |
| Previous | 8 | 5.55 s | 1,470 MiB |
| Reused bounded pool | 4 | 2.38 s | 1,061 MiB |
| Reused bounded pool | 8 | 2.37 s | 1,486 MiB |

All five runs had the same canonical digest covering IDs, typed fields and offsets, accounting,
aggregates, and evidence. The previous parser paid roughly 1.1–1.3 seconds per unit; after reuse,
only the first unit paid pool startup, with subsequent four-worker units taking 0.12–0.34 seconds.
Serial aggregation/sorting cost about 0.49–0.73 seconds. Input JSON was 83 MB: standalone encoding
took about 0.20–0.22 seconds and decoding about 1.14–1.21 seconds. More workers slightly reduced
cache reuse and did not improve this sample's wall time over serial parsing.

A second bounded case used one 90,000-head unit with 84,315 distinct heads: serial took 2.21 seconds,
the previous four-worker path 2.35 seconds, and the bounded pool 2.20 seconds, again with identical
output digests. Four-worker RSS stayed around 1,026 MiB versus 584 MiB serial. This case retained
strong message-body cache reuse despite distinct timestamped heads. These single-pass timings
exclude snapshot loading and are not end-to-end ingestion throughput or a general worker-count
recommendation. No source collection, live configuration, deployment, or model call was performed
by this benchmark; no customer examples or corpora were added to the repository.

### Private parallel collector staging (2026-11-05)

A separate private coordinator and shared request-budget helper were staged to run two collector
processes over disjoint whole-service jobs across all selected days. The existing capture,
projection and transport files remain unchanged. Requests share one start-rate and in-flight budget:
initially two requests per second, a ceiling of four, at most two in flight, and a shared three-second
p95/error backoff. Healthy windows allow a gradual rate increase. Existing count metadata affects
service ordering only; it does not cap targets or supply capture evidence. Credentials remain on
stdin/process memory, and the coordinator retains the existing collection lock.

Eleven synthetic checks passed in 16.67 seconds, including a measured rolling four-request ceiling
and two in-flight requests across real child processes, cross-worker error/latency backoff, stop and
owned-cleanup behavior, disjoint service ownership across dates, and restart after a hard crash,
including one while holding the shared controller lock. A crash stops the run with incomplete work
explicit; a new run revalidates completed manifests and rediscovers remaining jobs. Full Ruff passed.
The reusable optional test harness is public; the operational entrypoint/helpers remain private.

An offline check in the installed runtime found that importing the existing collector initially
started a native numerical-library thread pool. The entrypoint now sets numerical-library thread
counts to one before those imports, and checks Python plus native thread counts before every fork.
The corrected installed-runtime check observed one native coordinator thread and an identical
capture recipe with and without the shared-budget wrapper, with zero source requests. Thus existing
valid completed captures remain reusable without a pacing-only recipe migration. No source
benchmark, live deployment change or measured throughput claim is part of this staging validation.

### Private parallel importer and throughput rollout (2026-11-05)

The private file adapter was tested with four spawned mapping workers, at most two estimated
large units concurrently, parent-only partition persistence, and checksum-bound artifact reuse.
Ten isolated synthetic tests passed in 32.90 seconds. They covered serial/parallel artifact parity,
memory and cache admission, source/configuration invalidation, completed-index selection from
unfinished service exports, prior-index preservation, corruption rejection, and failure/resume.
No source or model calls were made by those tests.

A single synthetic benchmark with 80,000 documents across four units took 17.60 seconds through
the serial adapter, 9.01 seconds through the parallel adapter, and 3.81 seconds with its proven
artifact cache. Canonical artifact digests and counts matched. These measurements do not establish
production import throughput. Historical artifacts without that adapter's processing proof are
remapped on the first transition. Mapping memory reservations are estimates; partition-wide
parsing and aggregate retention remain separate memory costs.

The parser pool change was deployed after an isolated installation check: all 262 package files
matched the wheel, only the parser module differed from the previous installation, and dependencies
and UI contents were unchanged. Staged and live checks passed for readiness, UI loading, browser
origin/host restrictions, unknown-scope refusal, and a nonempty graph. The graph returned 125 nodes
and 149 edges; this is availability evidence, not a relevance evaluation.

The private two-worker collector was also deployed with the existing capture recipes unchanged.
Completed captures were preserved. In an early live observation, all 691 recorded requests returned
HTTP 200; the last 60 seconds contained 223 completed requests (3.72 per second), with p95 latency
about 814 ms. The earlier serial observation measured 0.44 requests per second. The new window
mostly covered small service populations, so these are request-throughput observations, not a
bulk-record speedup, extraction-accuracy result, or whole-backfill ETA. Source completeness remains
unfinished, and the shared four-request/two-in-flight limit remains enforced.

Eight additional isolated refresh-integration tests passed in 17.29 seconds against the deployed
parser build. They exercised the real importer and partition store with synthetic files and a
stubbed compose step: inherited/direct refresh locks, low-memory and oversized-day deferral,
pending-selection retries, legacy pending migration, dry run, and prior-index preservation.
The installed private scheduler uses four mapping workers, at most two large units, a 24 GiB
estimated mapping budget with an 8 GiB available-memory reserve, and a 32 GiB artifact cache cap.
Oversized days remain explicitly unfinished and retain their previous served version. These
guards do not bound the separate partition-wide parse stage. The first scheduled production
import was started after installation; its completion and end-to-end throughput are not yet
validated by these test results.

The first live import stopped before beginning a replacement partition: export metadata retained a
numeric deployment suffix in its service name, while the configured parser split that suffix into
`deployment_id`. The initial synthetic tests did not cover this naming case. The previous serving
generation remained readable. This failure is not counted as a successful production import.

The private adapter was corrected to validate raw export dimensions against the export index
grammar while preserving the configured parser's canonical service and deployment ID. Thirty-two
optional operational checks passed against the deployed core in 9.87 seconds, including fourteen
new full/grouped suffix and metadata-corruption cases. Full Ruff passed. The corrected live refresh
restarted with its frozen pending selection; after 52 seconds it had four active mapping workers,
no stderr output, and about 3.7 GiB of service memory. That observation confirms active mapping,
not a completed import or newly searchable generation.

### Incident vocabulary candidate (2026-11-05)

A reusable budgeted vocabulary proposer/reviewer accepts plain operational phrases and integer
aggregate counts only; extra payload fields and nonliteral phrase syntax are rejected. Eleven
synthetic input-contract tests passed. Hosted calls used the existing shared Haiku ledger and caps;
no raw log messages or customer dimensions were supplied. One proposal and three critique requests
were made: two critiques failed array-length validation, and the final critique succeeded after
relaxing metadata-only critique array limits. Such failures are not successful reviews.

The resulting private candidate has 145 incident phrases across 14 families and eight context-only
recovery phrases, supplemented by severity and HTTP-status selectors. Retrospective comparison on
3,543,812 existing unmatched/format-only heads selected 306,886 (8.660%) and retained all 5,433 supplied
error-level heads in that subset. That inventory spans 37 services and is not the full source or an
independently labeled incident set. It does not establish source-wide reduction, incident precision,
or recall. Broad high-recall phrases still selected 120,510 heads containing zero/no-failure wording.
The full-backfill downloader was stopped while mapping checks and incident-filtered collection are
prepared; previous captures and the running importer were preserved.

A bounded sealed-record check selected 91 day/service units from 397 retained units (359 met the
2 MiB compressed eligibility bound), read 10.8 MiB compressed and evaluated 5,817 deterministic
sample heads plus 193 separate structured-error sentinels. The candidate selected 2,776 sample heads
(47.72%) and retained all 1,116 structured-error sample heads and 193 sentinels. This stratified,
small-unit sample is not self-weighting; neither its selection share nor the residue-inventory
share estimates the whole source. Sixty-one sample heads with zero-failure wording still matched.

Four authorized live count-only checks returned exact totals from single search responses:
844 selected documents out of 17,660 source documents (4.78%). Individual source/selected counts
were 6,310/639, 1,059/1, 3,858/0 and 6,433/204 across four service scopes. This is 95.22% fewer raw
rows before adding context for these scopes only. Native severity was available in one scope and
HTTP-status fields were unavailable in all four; those gaps were reported explicitly. No raw log
fetch or source-wide reduction claim follows from the count checks alone. The previous full-capture
service and timer were disabled to prevent an unfiltered restart.

An authorized filtered-fetch pilot then retrieved one selected seed from a 1,059-document source
scope. The filtered scroll reconciled its same-snapshot exact total, unique source ID, masked
projection and output checksum. A separate bounded context search returned seven records within
the seed's exact stream and +/-120-second window; overlap deduplication emitted six additional
records. Both seed and context snapshots are recorded separately. Thus seven unique records were
retained for this pilot, not the entire source scope. This proves the small filtered export path,
not whole-source incident recall or import/serving of the new export contract. Recurring filtered
collection remains unconfigured pending the owner's recent-window choice.

The staged parallel collector was subsequently activated privately at 14:57:58 UTC. The serial
collector was stopped before startup; the existing launch configuration was backed up, and only the
two new coordination helpers were installed. The existing capture/projection/transport files and
capture recipe remained unchanged. One coordinator and two single-threaded workers were observed;
the shared rate increased from two to its four-request ceiling through healthy windows.

Existing request logs through 15:06:53 UTC contained 1,425 requests, all HTTP 200, no recorded error
events, and an overall p95 of 976.7 ms. The final two-minute window completed 258 requests (2.15 per
second), with p95 1,111.6 ms; an earlier minute reached 3.717 completed requests per second. Completed
index manifests increased from 83 to 343 and represented occurrences from 10,726,503 to 11,028,481,
including reuse of already downloaded full exports. No previously finished index was observed being
mapped again, and its export files remained present. Existing partial captures were retained. This
was a small-service-priority rollout window, not a bulk-download benchmark or proof of source-wide
completeness. Production captures in this window remained raw; no compression gain is inferred.

### Incident policy count interface (2026-11-05)

An optional private incident-query compiler was exercised through 38 synthetic-only
checks in `tests/operational/test_incident_query.py`, selected with
`SU_INCIDENT_QUERY_PATH`. All passed; `ruff check src tests` and the private compiler
also passed. Fixtures cover literal phrase construction, context phrases excluded
from seed selection, exact concrete index scope, native severity/status mapping
availability, rejection of keyword-only message mappings, exact selected versus
global source counts, and rejection of partial/truncated responses. Tests used
an external temporary directory with bytecode disabled. This is a count-only
interface validation; it establishes neither live incident precision nor context
completeness, and performs no source log fetch. Policy and operational code remain
outside the public repository.

The separate bounded context helper passed 13 additional synthetic checks through
`tests/operational/test_incident_context.py` (`SU_INCIDENT_CONTEXT_PATH`), bringing
the combined optional incident checks to 51 passing. It requires an exact mapped
stream selector in the same concrete index, searches a bounded timestamp window,
and rejects incomplete responses or duplicate identities. Context exceeding the
row budget is deferred without emitting partial rows. Overlapping windows retain
seed/member identity while avoiding repeated physical records. The context snapshot
is explicitly separate from the seed snapshot; a missing seed causes deferral.
These are synthetic contract checks, not measured live context retrieval quality.

### Windowed incident capture and rollup coordinator staging (2026-11-05)

A combined 69 affected synthetic checks passed for the optional incident compiler,
window guards, filtered capture, and private batch coordinator. Four additional
regression cases then passed for global interruption, disk, authorization, and
connection failure propagation. Full `ruff check src tests`, the affected capture
script, and private helpers passed. Shell syntax checks passed and the staged timer
calendar resolved to 01:15 UTC. Working directories remained external and bytecode
was disabled.

Checks include a pinned 72-hour UTC window across restart, matching half-open seed
and population filters, admission of intersecting daily indices with the newest
date first, fresh daily revisions for the previous three UTC days, atomic snapshot
replacement, and rejection of stale backfill publication. A synthetic 1,001-row
capture spans two pages without a sample cap; verified reuse checks scope, window,
recipe, revision, checksum, and physical count. Per-index count/capture publication
is interleaved, unsupported scopes remain deferred while others progress, and
systemic failures abort globally. These validate staging and synthetic contracts;
they do not establish deployed throughput, incident relevance, or context quality.
The seed coordinator leaves context explicitly pending. Late-arrival revisits are
limited to matching daily index names and timestamp windows, not arbitrary
misdated source indices.

Six further coordinator regressions passed after an index-admission failure was
identified during private activation. A synthetic unresolved placeholder index is
recorded as a deferred admission while a valid sibling index still captures and
publishes. Deferred admission counts prevent a complete-discovery claim, and the
transport path allowlist is unchanged. Migration tests permit only an explicitly
allowlisted failed predecessor with zero captured items and zero discovery progress;
the original pinned window is preserved with an old/new recipe audit record.
Unknown or progressed predecessor recipes remain rejected. The complete optional
batch test module passed 19 tests after this fix.

### Bounded incident context continuation staging (2026-11-05)

Seven synthetic checks passed for a private, separately scheduled context worker.
They cover merged same-stream window bounds, bounded overlap grouping, service
rotation, independent context state, exact context ID/count reconciliation and
physical deduplication, repeat-run reuse, collector-lock deferral before source
initialization, a global 20-window limit, and explicit deferral for retained stream
fields lacking exact query mappings. Full source/tests/private-helper Ruff and shell
syntax checks passed; the staged timer resolves each UTC quarter-hour.

The worker is separate from the immutable collector package and never alters seed
manifests or current seed pointers. Its context manifests attach through an index
keyed by seed-manifest checksum. It defers while the collector holds the shared
lock, bounds each context query to 1,000 rows, and reports uncovered or oversized
windows as pending. This is staged synthetic validation, not a deployed source
measurement or a claim that production context is complete.

### Filtered import and daily rollout (2026-11-05)

Typed incident provenance, compact accounting, seed-only event aggregates and explicit filtered
coverage passed 132 affected tests in 13.22 seconds, followed by 77 focused regressions after
per-day memory and compact-summary changes. The private importer additionally passed end-to-end
synthetic checks through sealing, composition, coverage and service/error tables, including
unchanged repeat import, multi-seed context and exclusion of context-only ERROR from seed counts.
Input conversion materializes one day at a time. Full Ruff and wheel/source-archive builds passed;
no Config field was added. Installed package files were checked against the release wheel.

The private incident dataset is isolated from earlier full captures. Staged and live checks passed
for readiness, UI assets, CSP, host/origin boundaries, unknown-scope refusal and nonempty graph
(65 nodes/64 edges at the initial stage). Coverage returned null whole-source completeness with
`scope_filtered`, exact selected-match capture, and separate context-pending counters. The first
live automatic refresh completed successfully and the API served its active generation.

The owner approved one pinned rolling 72-hour backfill and then daily rollups. The deployed window
is November 2 17:02:51.059800 through November 5 17:02:51.059800 UTC. Daily collection is enabled at
01:15 UTC for the preceding UTC day and two prior-day corrections, replacing per-index snapshots
rather than adding overlapping counts. A separate five-minute-after-completion refresh imports
completed exports, composes rollups/graph/shared-error communities and verifies serving state.
Unchanged inputs skip import/composition. Context continuation runs each quarter-hour but defers
while collection holds the shared source lock. Three unsupported index admissions remain explicit.
Partial completion exits do not endlessly restart the one-time job; systemic failures still retry.
The full backfill remains in progress. Captured/indexed counts do not imply its completion.

The overview distinguishes filtered scope, selected matches, context, scanning and useful
extraction; its recent coverage query includes partial boundary dates instead of showing only
yesterday. One hundred UI tests passed before the final date-query adjustment, followed by five
affected tests and TypeScript/production builds. Live desktop/mobile browser checks passed with
no page errors or viewport overflow. The display explicitly says imported-data figures do not
measure backfill completion.

### Fresh filtered-data parser worker measurement (2026-11-05)

An isolated check used 185,357 retained heads across 20 services and the 192 approved patterns.
One/four/eight/twelve workers took 6.20/6.20/5.78/6.33 seconds, with process-tree RSS approximately
906/1,376/1,798/2,214 MiB. Repeats took 6.24 seconds with four and 6.11 with eight workers. All six
semantic output digests matched. Eight workers were about 2–7% faster than four in these bounded
runs; twelve were slower. The private extraction configuration now uses eight processes.

Separate source mapping took 4.41 seconds and export loading/validation 1.37 seconds. Parent
aggregation/sorting cost about 3.2–3.6 seconds; regex/pool work about 2.6–2.8 seconds. Validation
remains enforced rather than removed for speed. Sample statuses were 49,951 parsed, 80,124
format-only, 55,281 unparsed and one noise record. These are bounded retained-data measurements,
not population coverage, a repeated full ingestion benchmark, or a 32-worker scaling claim.

### Parallel canonical mapping and verified reuse (2026-11-05)

A private importer improvement passed 28 synthetic checks for serial/parallel full-artifact parity,
parent-only partition writes, source/context/cache corruption refusal, context/config/core-recipe
invalidation, cache reuse and memory admission. Mapping workers finish before the independent
regex process pool starts. The mapping reservation excludes the existing day-sized parser/compose
retention; it is not an OS memory guarantee. The canonical cache has an explicit size cap and no
silent eviction. Source and artifact checksums remain mandatory on cache hits.

On the same bounded 185,357-head/20-unit retained input, direct serial conversion took 7.874 seconds,
four-process cold conversion 4.681 seconds, and verified cache reuse 2.840 seconds (40.5% and 63.9%
less wall time for this stage). All three canonical `UnitArtifacts` digests matched. Sampled
process-tree RSS was approximately 696/1,525/846 MiB, with shared pages potentially counted twice.
These are conversion-stage measurements, not end-to-end collection/extraction speedups. The private
helper was deployed; its first live run mapped 159 units with four worker processes, sealed the day,
composed a generation and verified that the API served it successfully. Regex extraction uses
eight processes and approved patterns only; unmatched logs do not invoke a per-log model fallback.

### Haiku incident-pattern expansion (2026-11-05)

An isolated incident-data worker made three Haiku calls costing $0.0028065 under the existing shared
daily ledger and caps. Actual outbound-payload privacy checks reported zero hits. Two proposals
passed their proposal gate; one with undeclared groups was rejected and remained rejected. System
review derived two exact message patterns and a narrow formatter for two explicit app.js ERROR
bodies, restricted to two services. The formatter preserves existing timestamp/line fields and
recovers the embedded error level; the undefined-payload signature does not assert a cause or outage.

A frozen retained snapshot of 638,521 heads increased useful parsed heads from 173,016 to 321,490
(27.10% to 50.35%, +148,474). All 173,019 previously parsed/noise **complete match dictionaries**
remained identical, including pattern identity/owner/review status, typed values, offsets and gaps.
Noise remained three. The two service holdouts contained 28,730 and eight occurrences, all recognized
at ERROR; each involved one held-out tenant and the same narrow payload shapes. Ten synthetic
positive/negative tests and three inventory-isolation checks passed; full Ruff passed. This is
retained-data recognition evidence, not novel-body generalization or incident precision.

Root system review approved the three entries after RE2 compilation/lint, negative checks and full
pipeline replay. This was an explicit system-reviewed promotion, not an automatic learning-gate
claim or a human review. The human-owned library file was unchanged and backed up. Approved counts
became 195: 17 formats, 160 message patterns, 12 noise and six continuation patterns. A live refresh
then served 648,401 scanned heads with 327,240 usefully extracted (50.47%); that live scope differs
from the frozen validation input and continues changing during the backfill. Final readiness,
security/scope checks and desktop/mobile overview checks passed after promotion.

### Lossless HTTP compression and deadline staging (2026-11-05)

Nine optional private-transport checks passed using synthetic responses on an
isolated loopback HTTP server. Plain and gzip request/response combinations preserve
original request bytes after decoding and identical returned JSON. A continuously
streaming response is interrupted by a short wall deadline, with its owned connection
closed and signal state restored. Corrupt/unsupported encodings fail closed, and
existing path/projection guards remain enforced before network access. Metrics
separate transferred payload bytes from decoded JSON bytes; incomplete transfer size
is explicitly unknown. Full source/tests/helper Ruff passed.

These are synthetic transport checks, not source compression capability or production
throughput measurements. Request compression and hard deadlines remain opt-in in the
staged helper. Enabling a deadline shorter than a valid existing page transfer would
cause capture failures. No live collector, source setting, query, field projection,
page size, or completed export recipe was changed by this stage.

The staged transport subsequently passed 21 checks after adding 12 scroll-lease and
cleanup regressions. An optional bounded lease override changes only the initial
scroll query parameter and subsequent scroll lease, preserving logical query and
projection content. Invalid lease values are refused before credential or network
access. Proven expired owned-scroll cleanup is idempotent; unrelated cleanup errors
still fail, and cleanup failures during an existing fetch error are diagnosed without
replacing that primary error. The immutable collector recipe and completed-export
identity are unchanged. This validation used synthetic loopback responses only.

A subsequent authorized live diagnosis found a 66.9 MB initial page taking 177 seconds, followed
by an expired-scroll response with the prior two-minute lease. The reviewed transport was deployed
with a ten-minute lease and the existing socket-inactivity timeout; the opt-in hard wall deadline
remained disabled. The immutable collector, fixed 72-hour window, query/projection/page settings
and 173 completed exports were preserved. A bounded one-record compression probe returned identity
encoding both with and without gzip negotiation, with identical record payloads. No source setting
was changed and no compression throughput improvement is claimed. At 19:18:25 UTC the restarted
collector completed a follow-up scroll with HTTP 200 (53.7 MB in 142.1 seconds), verifying
progress past the former lease-expiry failure. The backfill remained active at 19:19 UTC;
173 completed index exports were published, so this is recovery evidence rather than backfill
completion. The wheel and sdist builds succeeded.


### Extraction host migration (2026-11-06)

An authorized private deployment moved approved-pattern incident extraction to a separate
host with more available RAM. The API, database, embeddings of composed documents and
composition remain on the serving host. This is private operational integration, not a
new public Config contract. The extractor runs one immutable index per child, persists
hash-checked unit/match/summary checkpoints, and combines compact summaries while streaming
match rows during sealing. It preserves selected-seed versus context counts, rejects
corrupt caches and transfers every sealed artifact before publishing its manifest. The
serving-side composition worker explicitly refuses reparsing and has a separate RAM cap.
No source filtering, sample cap, approved regex or per-record provider policy changed.

Nine synthetic checks passed: split-index versus ordinary-parser equality for match
rows, all aggregate tables, hourly cells, exemplars, unparsed rows and accounting;
streamed unit model round trips; core sealing and repeat sealing; corruption of each
checkpoint artifact; duplicate units; incomplete checkpoints; and handoff verification
before partition visibility plus repeat handoff. Full `ruff check src tests` passed;
the three new private Python helpers and shell syntax checks also passed. No library
release or Config change was made.

On the authorized retained data, a 5,146-record index completed in 2.58 seconds with
155 MiB main-process peak RSS. The previously blocked 3,497,165-record index completed
in 299.09 seconds with 22.06 GiB main-process peak RSS. These are individual index
extraction checks, not end-to-end throughput, universal memory bounds, or retrieval
quality measurements. The service uses MemoryHigh 48 GiB / MemoryMax 56 GiB with
44 GiB estimated single-index admission; a larger index is refused explicitly.
A parent-only resume/seal correction was applied after those canaries; unchanged child
function ASTs, source hashes and output hashes were verified before reusing their
checkpoints under the new recipe identity.

A real sealed-baseline transfer was accepted as unchanged, composition succeeded and the
API served the active generation. The scheduled worker then started a frozen 805-index
snapshot on the extraction host. At this checkpoint the enlarged snapshot was still
being processed, so its new totals had not yet reached the UI. The former extraction
timer was disabled; scheduled extraction now invokes serving-side composition after
successful sealing and handoff. Downloads and the outstanding context/daily lock behavior
were unchanged. Backfill completion and new-snapshot publication are not claimed here.


### Oversized-example publication recovery (2026-11-06)

The private extraction worker finished its 805-index snapshot: 14,672,445 retained
records scanned, including 14,669,236 selected seeds and 3,209 additional context
records. There were 2,828,823 useful parsed records (19.28% of retained records),
160,171 noise records, 6,222,407 format-only records and 5,461,044 unparsed records.
These counts describe pattern recognition in the incident-filtered retained subset;
they do not measure whole-source completeness or answer relevance.

The partition transferred successfully, but composition explicitly refused an example
record over the configured 24,576-character line-chunk bound. The largest retained
example measured 125,385 characters. The authorized private deployment increased the
existing line-recipe limit to 262,144 characters and explicitly rebuilt the generation;
examples were preserved whole. Three synthetic checks verified old-limit refusal,
exact text and offsets under the new bound, and refusal above the new bound. All 17
existing log configuration/recipe tests passed. No public Config definition or schema
changed.

Composition then built 3,212 documents with 27 co-firing incidents and 13 shared-error
groups. After restarting the API with the matching recipe configuration, readiness
was HTTP 200 and live coverage reported 109 services and the same 14,672,445 scanned /
2,828,823 extracted counts. The API served the active generation. These are actual
publication and aggregation checks, not semantic quality evaluation of the groups.
The host extraction timer was resumed to process subsequent captures. At the source
checkpoint, 1,340 of 5,878 admitted indices had completed (22.80% by index count),
with 18,504,218 selected incident matches captured; backfill completion is not claimed.


### Local extraction comparison harness (2026-11-07)

`tools/log_extraction_audit.py` provides bounded local Ollama and OpenAI-compatible
requests, model/prompt/input provenance, shared case caching, exact citation checks,
explicit failure and truncation accounting, and provisional cross-scope label groups.
All private inputs and generated model replies remain outside the public repository.

Eleven synthetic checks passed, including two actual local HTTP fake servers serving
four simultaneous requests, both provider protocol shapes, and cache reuse without
additional requests. Unsupported quotes, scope values and causal proposals are rejected;
empty outputs and request failures cannot yield perfect coverage. Tests used isolated
external temporary/cache directories with repository bytecode disabled. Full
`ruff check src tests` and the standalone harness Ruff check passed.

This harness measures literal support and exact normalized finding agreement. It does
not establish independent semantic truth, regex precision/recall, incident correctness,
or whole-source coverage. Model-proposed gaps require semantic and equivalence review
before rule promotion; shared issue labels are community candidates only. No public
Config contract or release artifact changed in this validation.


### Portable integration candidate: UI and source fields (2026-11-07)

An isolated integration candidate preserved the committed logs foundation and reviewed
portable pending changes. Private helper-dependent operational tests were not copied
into this public candidate. Source summary/evidence compilation now accepts the
validated source field map; source paths affect the recipe. Standard navigation is
the default and retains operational views; reduced navigation is an explicit profile.

The candidate UI passed all102 unit checks in17 files, TypeScript checking and a
production bundle build. Manifest and third-party notices were regenerated from the
lockfile. These are synthetic component/build checks, not browser or live-service
validation. The Python CI extras now include both RE2 and regex pattern engines.
Full integration QA and release checks are tracked separately and remain pending
at this checkpoint. Large-context audit JSON completeness does not establish
per-log finding completeness; the batch tool and its prototype unit tests are excluded
from the public integration pending independent quality validation.


On the same candidate, real Chromium checks against isolated synthetic fixtures
passed for safe Markdown tables/lists/citations, today's date, standard navigation,
strict CSP, client/service issue graph with client filtering, grouped issue tables,
and completeness/scanned/extracted display at desktop and mobile widths. The
general synthetic smoke saved9 screenshots with zero CSP violations and zero page
errors. The Markdown browser regression was updated to match standard navigation
and leave graph assertions to the dedicated graph check. No live deployment was
queried by these checks; the streaming model is a loopback contract fake.

The UI wheel and sdist built successfully. A fresh isolated environment installed
the UI wheel and verified its version, entrypoint and all7 bundled manifest hashes.
The two batch-prototype source/test files were removed on independent QA advice;
those experimental checks are not part of the accepted public feature set.


### Independent QA for portable log integration (2026-11-07)

Independent QA tested the isolated candidate at `3e2a867`: **2,979 passed,1 skipped,
158 deselected** in169.20 seconds for the non-external Python suite. Both RE2 and
regex engines were installed. The single skip intentionally exercises missing-RE2
behavior and is inapplicable when RE2 is installed; external service cases were
explicitly deselected. Eight warnings were reported. This run includes7 prototype
batch-audit tests that were subsequently removed with the unvalidated batch tool.
It is not a claim that a fresh final-tree full suite ran after that removal.

Final source candidate `e5606dc` changes no package implementation relative to that
full run. Independent final checks passed: full source/tests/scripts/tools Ruff,
exact generated v1/v2 configuration schemas, all11 retained single-log auditor tests
(2.20 seconds), core wheel and sdist rebuild, and archive inspection excluding the
batch prototype, models, raw logs, private helper tests and environment directories.
The final core wheel SHA-256 is
`881d7a53f7b193b9de6151cd698e08ec43c4c792564a237152526a31e060c21d`,
identical to the independently clean-installed wheel. A plain isolated Python3.13
environment with freshly resolved declared dependencies (NumPy2.5.4, Pydantic2.14.3)
installed that wheel and passed synthetic example ingestion plus a3-labelled-query
evaluation. These are packaging/contract/small-fixture smoke checks, not measured
production retrieval relevance, extraction precision/recall, incident accuracy or
confirmed community quality. No live services or customer corpora were used by
these integration checks.

Independent QA accepted the reusable source/core with the batch prototype excluded.
This validation-record update changes documentation only; release archives must be
rebuilt from the final documented commit before distribution. The core wheel's
package bytes are unaffected by documentation-only changes.


### Shared extraction learning plan V2 (2026-11-07)

A documentation-only plan records the shared semantic contract, blinded comparison,
mandatory future tenant/day/template holdouts, independent QA promotion and affected
reparse workflow. It explicitly separates implemented helpers from planned enforcement
and excluded batch experiments. Eleven relative Markdown links across the plan and
its two crosslinks were checked; `git diff --check` passed. All examples are synthetic.
No provider calls, semantic evaluation, runtime changes or new production-quality
claims follow from publishing this plan.


### Unified SU platform plan and organization-name comparison (2026-11-07)

Added `docs/plans/unified_su_platform.md`, linked from the implementation plan and
architecture. This is a SU-only integration roadmap for the existing shared MCP/UI,
tickets/docs/ELK, identity, source releases, graph/community evidence and federated
multi-arm orchestration. No source/configuration contract or running service changed.
Read-only inventory identified existing single-dataset serving, operator-only log
profile constraints, legacy mode/context/session differences, private-comment visibility
and source freshness as explicit integration work rather than implemented federation.

Under the owner's declaration that ELK tenant name means SU organization name, a
read-only metadata cross-check considered 56 observed ELK labels from 4,674 successful
index/day scopes and 68 non-null organization labels across 1,231 SU tickets (6 missing
organization). Raw exact spelling matched 0; unique case/whitespace equivalence matched 6
labels / 106 tickets, and existing explicit curated aliases added 30 labels / 626 tickets.
Thus 36 labels on each side were alignable, covering 732 tickets. There were no ambiguous
matches in those tiers; 19 ELK labels lacked accepted counterparts, 1 represented shared
infrastructure, and 32 SU organization labels lacked counterparts. These are observed
snapshot comparisons across different source windows, not full-source completeness,
implemented joins or authorization validation. Names, native IDs and crosswalks remain
private. More aggressive normalization candidates are excluded from these totals.

Reviewed primary NVIDIA Auto-Ontology documentation/connector source at
`c447003f72a822301e927196a4ecb5b597c308d5` for an optional ontology evaluation milestone.
The proposed isolated synthetic pilot has a US$25 total Haiku API ceiling; no pilot
result, installed integration, deployment or benchmark is established by this review.
Ten relative documentation links resolved, `git diff --check` passed, and a narrow
private-path/address/hash string check passed for the new plan. This is not a full
secret or provenance audit. Independent senior QA accepted the corrected plan and
rechecked its ten relative links, scope, aggregate counts and privacy boundaries.
Full `ruff check src tests` passed with its cache outside the repository. Runtime
tests were not repeated for documentation-only work.


### Auto-Ontology isolated offline component pilot (2026-11-07)

Following the planning review, an external synthetic pilot executed the pinned
NVIDIA Auto-Ontology SQLite leaf connector, table/column/primary/foreign-key
introspection and term DTO/sanitizer at upstream revision
`c447003f72a822301e927196a4ecb5b597c308d5`. Six synthetic tables and eight
parameterized queries exercised organization aliases, distinct occurrence/ticket
measures, weighted request latency versus elapsed silence, collection/index and
code/prose distinctions, and internal-article visibility. The semantic specification
and query templates were handwritten; candidate terms were mocked. Selective leaf
module loading with an isolated dependency environment is not a full upstream
application installation or native model-pipeline integration.

The pilot recorded 24 synthetic tests passed in 0.37 seconds and Ruff passed on its
three Python files. Negative cases covered unknown identities, shared-infrastructure
labels, cross-organization access, forbidden/fanout joins, unsupported recovery
measures, invalid table-filter refusal, and durable/concurrent budget reservations.
Independent senior QA subsequently reran all 24 tests: 24 passed in 0.59 seconds,
using fresh external temporary/cache paths, bytecode disabled and socket connections
blocked so no provider/network/model call was possible. An initial QA invocation
needed its external temporary parent created before a clean rerun; its artifacts
were preserved. Neither run establishes production accuracy. Pilot source, fixtures,
databases, logs and reports remain in the external test workspace pending any
reviewed reusable integration.

Native Haiku inference returned provider quota errors; zero successful inference
calls and zero settled reported usage were recorded. Failed-call reservations
remain held under the owner's US$25 authorization instead of being silently
released. Real ontology generation, answerability/text-to-SQL, embedding/reranking,
MCP/UI integration and source-data relevance were not evaluated. No public runtime
code, Config definition, live service or GPU-audit process changed.


### First paired real SU context-layer experiment (2026-11-07)

The owner explicitly authorized real SU data evaluation using Haiku and later set
an initial US$5 cap per approach within the existing shared US$25 ceiling. Both
builders consumed the same privately frozen/redacted v2 slice: 179 records from
four approved organizations (48 tickets, 36 comments, 23 docs, 48 log examples,
24 observed episodes). A first freeze was withdrawn for stronger contact handling
before any real-data provider dispatch. Original and corrected artifacts remain
private; no records, source identifiers, aliases, prompts or model responses are
included here. Historical support and recent log windows differ.

Both approaches used the same actual cached BGE-M3 CPU model and a common outer
6,000-token Qwen tokenizer limit. Native Haiku usage was metered separately through
a shared hash-bound budget/rate gate. Two explicit projected views contained
179 operator records and 38 customer-reader records. Four declared configurations
produced 92 pre-answer contexts for 23 questions: deployed pattern control, supported
mixed core, native catalog diagnostic, and supported native source-query. The catalog diagnostic
control is not guaranteed to be every internal tool observation from the
native default information agent. Main comparison uses mixed core versus native
source-query. No best-configuration choice was made per question.

Independent QA curated source gold before viewing outputs and froze blinded ratings
before receiving the condition mapping. Output style means blinding is partial.
On 21 non-atomic source-grounded claim groups, mixed core supported 8 fully,
8 partly and 5 not at all; native source-query supported 10 fully, 2 partly and
9 not at all. Among 17 questions with scored claim groups, fully/partly/unsupported
question totals were 5/8/4 and 8/3/6 respectively. Those are ordinal context-support
measures, not atomic-fact precision/recall or final-answer accuracy. Other questions
include limits and authorization negatives. Question preferences were 7 mixed-core,
8 native-source, 1 substantive tie, 4 both insufficient and 3 scoped negatives
without detected prohibited evidence. No overall production winner was established.

All 92 context hashes and token counts were checked; none exceeded the shared cap
or listed explicit source IDs outside the declared view. QA replayed 893 returned
source passages/fields without literal/title mismatch and found no protected source
material in 16 reader-view packets. These are bounded wrapper-isolation/source-copy
checks, not production authorization or semantic truth. The native source-query
variant executed SQL in 20 cases and had 3 SQLite qualification errors; source
dialect configuration was verified. Both approaches preserved catalog records,
while extracted graph availability was narrower.

Our mixed extraction attempted 370 unique chunks: 129 validation rejections caused
113 documents to be quarantined from graph extraction; all 179 catalog records
remained available. Accepted output had 331 mention rows, 35 relations (11 fact,
24 explicitly unchecked co-occurrences) and 11 communities. QA inspected all 35
relations and all 11 communities, finding identity/type, event/polarity and topical
coherence limitations. A targeted 62-fact availability screen was not interpreted
as semantic or corpus recall. Native operator catalog output included 5 terms,
118 attributes and a schema FK; targeted review found time/version/recovery/offset
role ambiguities, partly tied to shared projection naming. Schema ontology and
instance incident/community graphs are different capabilities. No promotion passed.

A fixed common Haiku answerer subsequently processed 86 nonempty contexts once;
6 empty upstream errors were preserved without calls. Four answers reached the
1,800-output-token cap and one missed a required field. Citation/quote flags were
screening aids requiring source review. This secondary phase does not replace the
frozen primary context ratings or establish an answer-accuracy benchmark.

Reported settled family costs including those checks were US$0.1827164 for our
side and US$0.4385299 for NVIDIA's. Prior synthetic preflight settled US$0.0100028;
US$0.068746 in earlier failed-request reservations remained held. Overall ledger
commitment was US$0.6999951, not an invoice reconciliation. New paid admission was
paused with no active leases after completion. Tests, configurations, source/code
pins, per-question results and raw failures remain in the private external workspace.
Runtime/queue conditions changed during preparation, so latency is not a controlled
production comparison. No public runtime code, Config definition, live deployment
or automatic regex/graph/community promotion changed.

### Context-layer follow-up and capability-schema repair (2026-11-07)

Local uncommitted portable changes add bounded question-aware packing with exact
field/passage offsets, an explicit scalar metadata allowlist with budget/omission
accounting, bounded aggregate-level contributor lineage, and a capability-bound
planner wire/local schema. No upstream NVIDIA source or mandatory dependency was
copied. The generated v2 Config schema was updated; v1 was unchanged.

Actual synthetic checks: 117 packing/metadata/lineage affected tests passed and
were independently rerun; the isolated planner capability candidate passed 50
tests and strict-schema conversion checks. The parent combined planner, log
intent, metadata, packing and schema-repair regression passed 93 tests in 2.17s.
Ruff passed. Wheel/sdist passed for the packing snapshot and a subsequent
current-source build after the planner change also succeeded. These checks do not establish relevance.

The new development replay kept all 179 source IDs/titles/bodies/legacy metadata
and all 23 questions, while adding explicit field/time/version/span/status roles
for both builders. Separate fresh evaluation used 48 records from two additional
organizations and 12 questions, with source gold frozen before outputs. Native
IDs and exact log messages differed from the earlier sample, but heuristic
template overlap and prior collection/regex exposure remain disclosed; this is
not a strict template or globally unseen production benchmark.

V3 development ordinal support over 21 source groups was our 6 full/3 partial/12
absent versus NVIDIA 13/3/5. Primary question preferences were NVIDIA 11, our 2,
two ties, five both insufficient and three scoped negatives. Our execution was
5 complete/18 unsupported; NVIDIA 17 complete/6 SQL errors. Useful copied evidence
can coexist with a failed planner/scope guard, so nonempty output is not a clean
successful answer. No full-candidate production acceptance is claimed.

Packing-only ablation reused unchanged cached original retrieval with zero new
search/model/embedding/extraction calls. Original groups 8/8/5 became 7/8/6; two
questions favored each packer and nineteen tied. All 23 packets respected the
6,000-token limit; 453 literal source spans replayed without mismatch. Gains in
some recovery/index-scope evidence coexisted with lost role-removal/version
evidence. This does not support making question windows the default; see the opt-in
follow-up below. Allowlisted metadata
overhead can also displace relevant passage text and needs further evaluation.

Development inspection found reserved log-intent fields and disabled arm names
in the static planner schema. A separate V3.1 capability-schema candidate narrowed
those affordances while retaining all runtime coherence/filter/scope checks. It
reused existing source/index/extraction artifacts under normal compatibility
guards and made 35 planner calls without retries, repair or re-ingestion. Its
execution was 11 complete/12 unsupported development and 5 complete/7 unsupported
fresh. NVIDIA fresh execution was 10 complete/2 SQL errors. Further fresh/V3.1
semantic ratings remain pending and must not be inferred from these counters.

All scored runs retain original code/config/source pins and failures. Cumulative
reported provider-ledger cost was US$1.5842366 settled, with US$0.068746 historic
reservations held; US$1.6529826 committed under unchanged cumulative family/global
caps. This is not invoice reconciliation. Comparison admission was paused with
zero leases and only owned helper processes/container stopped; artifacts and
volumes were preserved. No new push, live deployment or automatic pattern/graph/
community promotion occurred.

The local extraction audit separately completed 18,003 eligible retained
representatives plus 68 separately counted extras, with 333 quarantined and zero
failed primary batches. An adapter rebaseline exposed omitted format captures;
29,182 residual proposals remain unadjudicated candidates, not proven gaps or a
coverage percentage. Independent source-only review of 160 stratified excerpts
is active, with frozen 24-record parent and 72-record QA splits and a further
64-record reviewer split in progress.
Useful operational facts, contextual prefixes and incidental payloads are distinct.
One residual sensitive payload is export-quarantined, not a negative fixture.
No regex-versus-LLM semantic parity/precision/recall result is claimed yet. A larger
incremental local audit was started over 26,103 eligible retained templates: 15,681
exact source-input/scope results reused and 10,422 new targets. It stopped about 23
minutes after start in state `failed_batch_requires_review`: 12 batches completed and
1 failed because the local model reply reached its completion-token limit
(non-stop finish reason). The controller made no automatic retry, as designed. Completed new
targets: 1,437 of 10,422; measured throughput 1.48 records/s on four local slots. These
are not whole-source coverage or semantic correctness statistics.

See the sanitized [handover](plans/codex_handover_2026_11_07.md) and
[NVIDIA reuse decision](plans/nvidia_context_layer_lessons.md). Private source,
gold, prompts, mappings and raw outputs remain outside this public repository.

### Opt-in passage selection and independent recheck (2026-11-08)

An independent agent rechecked the uncommitted 2026-11-07 changes. Question-window
packing was active by default, because the engine always supplies the question. It
is now behind `selection.packing.passage_selection: prefix | question_windows`
(default `prefix`). The trace records the configured `passage_recipe` and the
`windowed_ids` that actually used windows. The capability-bound planner schema
reports a disabled arm as `schema_violation` rather than `invalid_step`; two older
fail-policy tests that the earlier targeted runs missed were updated to this contract,
and ARCHITECTURE.md documents it. Repair remains opt-in (`schema_repair_attempts: 0`).

Actual checks: `tests/unit` plus `tests/contract` 2,676 passed, 99 skipped, 2 failed;
Ruff passed; generated config schema regenerated. Both failures predate these changes
and also fail on committed `db40268`: `test_episode_summaries` needs the optional
`google-re2` extra in the local environment, and `test_release_identity` flags
origin-service names in the committed `docs/plans/unified_su_platform.md`, which
must be sanitized before any public push. Wheel and sdist for 2.0.0a3 built
successfully in the external workspace after this change.

The recheck also confirmed from stored artifacts that the V3.1 schema fix left
fresh-evaluation execution unchanged (5 complete/7 unsupported before and after,
with different per-question outcomes). The development gain is on questions used to
design the fix. The first pilot's configured run (rules agent) completed 22 of 23
with 8/8/5 support, against NVIDIA 10/2/9, so the V3 configuration change (LLM
planner, fail-closed, fewer documents, metadata allowlist) and not retrieval explains
most of the V3 gap. No promotion follows.

### V3.2 candidate: unsolicited planner scopes, qualifier precision, metadata share — 2026-11-08

Scope: decision and packing contract changes driven by a real-data diagnosis of `kind: llm`
fail-closed refusals (planner-invented count/list steps on lookup questions; prose after a field
label read as a field value; a negated kind phrase read as a positive filter). Evidence level:
**synthetic contract checks only**. No real-data rerun, no provider call and no relevance or
answer-quality claim is made here; the effect on the refusal rate is unmeasured.

- `llm` agent: a structured scope on a question with no aggregate/untargeted-list intent, no
  aggregate operator word and no caller-typed aggregate/scope is dropped before compilation and
  recorded (`Decision.dropped_steps`, `planner_steps_dropped` event, `trace.decision.dropped_steps`);
  with no step left the original query runs on enabled `lexical`/`dense` arms under the request
  filters (`planner_fallback: retrieval_after_dropped_steps`). Neither arm enabled: `invalid_step`.
- Qualifiers: string/id field values need binding (explicit operator, digit-bearing token, or a
  resolver-known value); negation cues make a negated candidate applied only by `negate: true`.
- Packing: `metadata_max_share` (default 0.25) drops trailing allowlisted keys under contention,
  traced in `metadata_dropped_keys`; records are no longer omitted for metadata size.

Actual checks (`tests/unit` + `tests/contract`, external workspace): **2,703 passed, 99 skipped,
2 failed** (before: 2,676 passed, 99 skipped, 2 failed). The two failures are the pre-existing
`test_episode_summaries` (optional `google-re2`) and `test_release_identity` (a committed plan doc).
Replaced equivalents: the QA-01 "no operator" engine row now asserts the drop/fallback contract;
`severity high` (unbound string word) moved to the negative list and `severity = high` covers the
bound form; the oversized-metadata omission test now asserts key dropping with the record kept.
Ruff passed; `docs/config.schema.json` regenerated; wheel and sdist (2.0.0a3) built in the
external workspace.

Limitation: the operator-word check is deliberately broad (any of count, list, total, latest,
compare ...), so lookup questions containing such a word keep the planner's scope and its existing
guards; how many of the diagnosed refusals this removes needs a real-data rerun.

## V3.2 paid evaluation (2026-11-08)

Scope: the V3.2 candidate (`e077137`) against the stored V3, V3.1 and native relational
context-layer results on the private frozen sets, run in the external workspace. Only sanitized
aggregates are recorded here; no question, record, prompt or endpoint enters the repository.
Paid planner calls: **35, US$0.0525**.

| Set | Measure | V3 | V3.1 | V3.2 | Native |
|---|---|---|---|---|---|
| Fresh holdout (12 questions, 13 gold groups) | full / partial / absent group support | 1/0/12 | 2/3/8 | 5/3/5 | 9/2/2 |
| Dev replay (21 gold groups) | full / partial / absent group support | 6/3/12 | 6/5/10 | 13/3/5 | 13/3/5 |
| Fresh | execution complete / unsupported | — | 5/7 | 7/5 | — |
| Dev | execution complete / unsupported | — | 11/12 | 17/6 | — |

- Preference vs native on fresh: ours 2, tie 3, native 5 (+2 scope-negative questions; no leak
  detected on either).
- No question regressed versus V3.1. The question-window packing variant tied (no gain).
- Remaining fresh losses: a retrieval miss, a planner schema violation, an organisation bound to
  the wrong canonical customer, and a metadata-share drop: the `PackingConfig.metadata_max_share`
  default of 0.25 removed capture time, time roles, environment and identity-authority metadata
  needed by one question. **Fixed by changing the default to 1.0 (cap off, opt-in)**; the mechanism
  and its tests are kept (the tests now set 0.25 explicitly). The fix itself is not re-measured.

Limitations: a single AI rater; partial blinding; raters differ across versions; dev is a replay
(it shaped the design) and cannot show generalisation; small n; ordinal source-group support is
not answer accuracy.

## Typed-column query route MVP M1, M3-M5 — 2026-11-08 (branch `v2/phase-0`)

Scope: step-level failure policy and dropped-step qualifiers (M1), typed IR/config contracts (M3),
per-generation sqlite projection with per-tenant files (M4), compiler, SQL lowering and the
`typed_query` arm (M5), per `docs/plans/typed_query_arm.md` rev 2. Evidence level: **synthetic
only** (repository fixtures: four kinds, two tenants plus a canary, a comment->ticket relation with
one dangling key, NULLs, rejected values, injection-shaped strings). No provider call, no real data,
no planner integration (M6) and no fresh-set evaluation (M2/M7, gate G1); nothing here says
anything about answer quality or relevance.

Actual checks (external workspace, `tests/unit` + `tests/contract`): **2,787 passed, 99 skipped,
2 failed** (before M1: 2,703 passed, 99 skipped, 2 failed); the two failures are the pre-existing
`test_episode_summaries` (optional `google-re2`) and `test_release_identity` (committed plan doc
names). `tests/conformance` (memory/sqlite; services skipped) passes including the new
`check_relational`. Ruff clean; `docs/config.schema.json` regenerated; capability tables
regenerated. Notable synthetic results:

- Differential: 5,000 generated single-table queries (seeded) against `StructuredArm` over the same
  records; every compared query (>= 3,000 non-refused, with a measure) had equal totals, equal first
  8 contributor ids, equal group keys/values/counts, equal values (float tolerance 1e-9) and NULL
  counts. Limited to the synthetic schema (ASCII values; no temporal groups, ranking ties or shares).
- Canary: 5,000 generated queries (single-table and one-join) with the compiler's ACL injection
  disabled: zero rows, contributors, citations or group keys from another tenant or the canary;
  tenant aggregates never include the canary's values. Also with the sqlite file layer via the
  engine. A planted foreign row in a tenant file is caught by the returned-row check.
- Read-only store: writes, DDL, `ATTACH`, `PRAGMA` changes, unlisted columns and unlisted
  functions are refused; a long statement is interrupted by the deadline.
- Lifecycle: projection rebuilt per generation on ingest/upsert/delete; `delete_generation`
  removes the files; CURRENT/protected generations keep them; index fingerprints unchanged while
  disabled (golden examples pass).
- M1: `partial` and `retrieval_only` never leave an exact count for a dropped step (contract check
  refuses the count claim); caller-typed aggregates, disabled arms and overridden filters still
  fail closed; dropped-scope qualifiers are carried as filters or reported.

## Typed-column query route M6: planner integration — 2026-11-08 (branch `v2/phase-0`)

Scope: typed-query planning on the `llm` planner (`pipeline.decision.typed_query`), catalog-bound
wire schema, payload (reviewed descriptions, ACL-scoped samples), answerability, typed compile
errors with an opt-in single repair, engine execution of typed steps and typed evidence packing,
per `docs/plans/typed_query_arm.md` rev 2 §5.4/§5.8 M6. Evidence level: **synthetic and mocked
only**: repository fixtures (two tenants plus a canary, comment->ticket relation) and fake
planner/generator replies (`httpx.MockTransport` wire bodies or a patched `JsonModel.call`). No
provider call, no real data and no fresh-set evaluation (M2/M7, gate G1). The planner is a fake,
so nothing here measures whether a model plans typed steps well, nor answer quality or relevance.

Actual checks (external workspace, `tests/unit` + `tests/contract`): **2,863 passed, 99 skipped,
2 failed** (before M6: 2,787 passed, 99 skipped, 2 failed; the two failures are the pre-existing
`test_episode_summaries` and `test_release_identity`). `tests/conformance` 72 passed, 144 skipped.
Ruff clean. `docs/config.schema.json` regenerated (`pipeline.decision.typed_query`). Wheel and
sdist 2.0.0a3 built in the external workspace. What the new tests show (synthetic):

- Defaults unchanged: the planner HTTP request bodies (strict JSON-schema mode and prompt mode) for
  a fixed request without typed planning are byte-identical to `e1e925b` (sha256 frozen in
  `tests/unit/test_typed_planner.py`).
- Wire schema: `arm: typed_query` appears only with typed planning; table, column, relation,
  operation and measure names are catalog enums (undescribed columns excluded); `analysis` precedes
  the IR; the schema converts in both provider dialects (OpenAI strict, Anthropic output format).
  Replies naming an unknown column, table, relation or measure, free SQL text, or a
  `not_answerable` without gaps are `schema_violation` from a provider that does not enforce the
  schema.
- Payload: samples only for listable columns under their cap, read through the principal's ACL
  (another tenant's or the canary's values never appear); no manifest row counts.
- Decisions: `not_answerable` is a recorded dropped step with `planner_not_answerable` and
  `aggregate_intent_without_scope` constraints, and the `AnswerContract` refuses a count over the
  fallback evidence; compile errors abstain by default and are dropped under `partial`; a value
  present only in another tenant is refused by grounding; a contradicted request filter fails
  closed even under `partial`.
- Repair (opt-in): one re-plan with the typed errors; accepted when it keeps every bound question
  qualifier, rejected (original failure stands) when it drops one or drops the typed step; never a
  second repair.
- Engine end to end (sqlite projection, per-tenant files): the same question gives each tenant
  only its own rows and the canary never appears; the compiler runs twice per request (decision
  validation and the arm, both with the principal's ACL); with the compiler's ACL injection
  disabled the per-tenant file still holds the tenant's rows only. Typed rows are packed with
  `(document_id, field)` citations and the generation; an exact typed count backs a count claim,
  a wrong number is refused (`answer_number_mismatch`); a typed result that exceeds the aggregate
  reserve is omitted from the evidence; more rows than the pack cap make the result `bounded`.
- Frozen suite: 36 synthetic questions (counts, groups, ranks, sum/avg/min/max, first/last seen,
  lookups across tenants, child and parent joins, request filters, injection-shaped values, and
  refusals: unknown/other-tenant/canary values, undeclared join, fan-out sum, non-numeric sum,
  window without a schema timezone, non-groupable group, limit over cap, filter contradiction,
  not answerable) execute or refuse as frozen.

Limitations: typed results have no deterministic rendering (the generator states them); samples
are the field's observed values over all record kinds, not per table; `casefold_match` in SQL is
ASCII-only (M5); the `rules` agent plans no typed steps.

## SQL surface of the `typed_query` arm (NVIDIA Auto-Ontology port) — 2026-11-08 (branch `v2/sql-surface` from `e1e925b`)

Scope: opt-in `pipeline.arms.typed_query.mode: sql_surface`: catalog and text-to-SQL generation
adapted from NVIDIA Auto-Ontology (Apache-2.0, `c447003f72a822301e927196a4ecb5b597c308d5`),
executed only behind our guard (sqlglot parse, allowlists, value grounding, ACL/filter binding,
parameters, LIMIT) on the read-only per-tenant projection file. Evidence level: **synthetic and
mocked only**: the repository's typed fixtures (four kinds, two tenants plus a canary, one
dangling relation key, NULLs, injection-shaped strings) and a scripted structured-output model.
No provider call, no real or private data, no planner integration and no evaluation: nothing here
measures SQL-generation accuracy or answer quality. `sqlglot` 30.30.0 (MIT) was installed into the
external workspace only, for the tests.

Actual checks (external workspace, `tests/unit` + `tests/contract`, `-m 'not external'`):
**2,924 passed, 99 skipped, 2 failed** (base `e1e925b`, same interpreter, extracted without git: 2,787 passed, 100 skipped,
1 failed); the two failures are the pre-existing `test_episode_summaries` (optional `google-re2`) and
`test_release_identity` (a committed plan document). New SQL-surface tests: 137 (guard 95,
catalog/prompts/config 9, contract 33; counts include parametrised cases). Ruff clean;
`docs/config.schema.json` regenerated; wheel and sdist built in the external workspace.

- Tenant canary (layer 1, the ACL binding, disabled by the test hook): 5,000 seeded generated and
  adversarial statements over two tenants: **3,115 executed** (1,709 row-level, 1,406
  aggregate/derived) and **1,885 refused** (`value_not_observed` 515, `set_operation` 345,
  `not_select` 210, `comment` 186, `join_not_allowed` 184, `system_object` 149,
  `multiple_statements` 104, `qualified_name` 92, `recursive_cte` 88, `parse_error` 12). In every
  executed result: zero rows, document ids, citations, tenant values, `group_concat` ids or canary
  values from another tenant; counts with tenant-negating predicates equal the principal's own rows.
  Also on the sqlite backend through `Engine` + `engine_session`.
- Defence in depth with the guard bypassed (statements sent straight to the read-only connection
  with the SQL-surface function list): `ATTACH`, `PRAGMA`, `sqlite_master`, `INSERT`, `DELETE`,
  `load_extension`, `random()`, `_rejected` with an unlisted function and `json_each` are denied;
  a three-table `UNION ALL` on a tenant file returns that tenant's rows only. A planted foreign row
  in a tenant file is caught by the returned-row check (result discarded, `BackendError`).
- Guard: comments (incl. hints), several statements, non-SELECT statements, recursive CTEs, system
  objects and NUL bytes are final refusals; set operations, unknown/qualified names (`main.t`,
  3-part), placeholders, window functions, blobs, unlisted functions, cross/comma/natural/right
  joins, non-literal limits, reserved `__` aliases and CTEs shadowing tables are repairable; unknown
  values (incl. another tenant's ids/values, a case-only near miss), ill-typed values, partial
  datetime equality and text values in ungroundable filter positions are explicit unsupported
  constraints. Every literal in the executed SQL is a parameter; the ACL value is bound into every
  table reference (including subqueries and CTEs) and masked in lineage.
- Planner (scripted model): executed, `not_answerable` (nothing runs), one repair with the typed
  error, at most one repair, repair disabled, unknown values never repaired, security refusals
  final, provider timeout and schema violation reported as errors without retry.

Limitations: synthetic schema and values only (ASCII; small tables); the guard accepts a subset
of SQLite; sqlglot's re-rendering of the statement is trusted after a final single-statement,
comment-free check; aggregates carry no contributor ids; the M7 question set has not been run.

## `typed_query` SQL route wired to the `llm` planner — 2026-11-08 (branch `v2/phase-0`)

Evidence level: **synthetic and mocked only** (repository fixtures in the external workspace; fake
planner/SQL writer/generator patched over `JsonModel.call`, and one run through the real
`openai_compatible` provider behind `httpx.MockTransport`). No live model, no paid call, no
fresh-set (M7) evaluation. `sqlglot` 30.30.0 from the external workspace only.

- Routing end to end through `Engine.search`/`answer` on the two-tenant corpus with
  `mode: sql_surface` + `decision.typed_query.enabled`: the planner payload carries the described
  catalog and ACL-scoped samples (no other tenant's values, no SQL), the wire step is
  `{analysis, answerability, gaps, question}` (no IR enums); the sub-question (not the original
  question) reaches the SQL writer; per-tenant counts 1/1 and 3 for the unrestricted operator; an
  empty grant reads zero rows and grounds no value.
- ACL at execution: the arm receives the request's ACL (`t_alpha`, then `t_beta`) for the same
  plan; sums 6.5/7.5; different `acl_key` and fingerprint.
- Answer path: SQL evidence packed (`sql_aggregate` value, parameterised SQL, fingerprint; rows
  cited `(document_id, table.field)`), an exact count claim passes the `AnswerContract`, the
  cross-tenant total (3) is refused as `answer_number_mismatch`; a dropped `module` qualifier in
  the executed SQL, or a `module` comparison only under `OR`, `NOT IN` or `CASE`, makes the result
  `bounded` (`qualifier_not_applied`).
- Failures: refused (unknown column after one repair) -> `abstain`/`invalid_step` by default,
  `retrieval_after_dropped_steps` under `partial`, structured/typed steps dropped under
  `retrieval_only`, `raw_all_arms` under `on_failure: raw_all_arms`; unknown value -> `sql_unsupported`
  with no repair; SQL writer `not_answerable` and schema violations recorded without model text.
- Spend: one repair gives `model_calls` stages planner, sql_surface, sql_surface; through the real
  provider the SQL call's reported usage (700/40 tokens) is in the trace; a call limit of 1 stops
  the SQL call before it is sent (`sql_error`/`budget_exhausted`, or abstain). Config validation
  counts planner + SQL writer + generator (`max_llm_calls: 2` refused in this mode).
- Tenant canary through the routed path, guard ACL binding disabled: 300 seeded routed requests
  (cross-tenant predicates, `OR 1 = 1`, joins, subqueries, unions, multi-statements, qualified
  names), >= 100 executed; zero foreign ids, tenant values, canary values or hits.
- Mode `ir`: payload without `mode`, typed drafts and evidence unchanged; the sha256 pin of the
  planner request without typed planning still passes.

Tests: unit+contract 3,258 passed, 1 skipped, 0 failed (HEAD `de56e47` extracted without git in
the same interpreter: 3,207 passed, 2 skipped; the extra skip is `test_release_identity` without
git). New: `tests/contract/test_sql_route_engine.py` 29, `tests/unit/test_sql_route_planner.py` 11,
`test_sql_surface_guard.py` +10 (filter positions). Ruff clean; `docs/config.schema.json`
regenerated with no change (no new config keys).

Limitations: synthetic schema only; the executed-SQL qualifier check counts only a grounded
comparison that is a conjunct of the outermost `WHERE`/`HAVING` (`checked_values[].filter`;
`OR`, `CASE`, derived tables, subqueries and join conditions are `other` and leave the result
`bounded`; LIKE patterns never count), which is conservative for correct SQL that filters in a
derived table; SQL aggregates carry no contributor ids; no measured quality comparison of `ir` vs
`sql_surface` yet (M7 needs the owner's budget).

## Typed router, protected metadata, arm cards, projection dump fix — 2026-11-08 (branch `v2/phase-0`)

Evidence level: **synthetic and mocked only** (repository fixtures in the external workspace; fake
router/SQL writer/typed IR writer/planner/generator patched over `JsonModel.call`, and runs through
the real `openai_compatible` provider behind `httpx.MockTransport`). No live model, no paid call.
The motivating M7 aggregates (passage share and planner behaviour) come from the coordinator's
real-data run and are not reproduced here; nothing below measures answer quality.

- Packing: `metadata_max_share` default restored to 0.25 (the interim 1.0 was the M7 regression).
  `metadata_protected` keys survive any share (allowlist order kept; only unprotected keys dropped,
  reverse allowlist order, also when interleaved); with ten long synthetic records and three short
  protected keys the passage share stays >= 0.74 under the 0.25 cap vs < 0.5 with the cap off;
  protected keys over the share are traced (`metadata_protected_over_cap_ids`), protected keys over
  the whole allocation keep the record with an empty passage and the exact overflow traced
  (`metadata_protected_overflow`); unknown protected keys are refused at validation; the engine's
  packing trace carries the new fields.
- Typed router (`routing: router`), modes `ir` and `sql_surface`: the planner HTTP body is
  byte-identical to the same deployment with typed planning disabled (route typed and route
  retrieval, both modes), and carries no `typed_query`/catalog; the router body holds only the
  question, the request filters and table -> described column names (no samples or values);
  call order typed_router -> (sql_surface | typed_ir) -> planner -> generator; the routed result is
  step 1 (`typed_router: true`), its evidence is first in the generator payload (`leading_ids`) and
  backs an exact count through the `AnswerContract`; lexical evidence follows; a qualifier the
  sub-question dropped makes the result `bounded`; per-tenant sums 6.5/7.5 for the same route.
- Failures: refused, unknown value, writer `not_answerable` and writer schema errors (SQL), IR
  `not_answerable`, IR compile error and IR schema error all give one recorded `DroppedStep`, the
  planner's lexical step still runs, `aggregate_intent_without_scope` is added, no model text in the
  record; a router schema violation or budget failure gives `typed_router_error` and the planner
  runs; an executed route under an abstaining planner is `typed_route_discarded`.
- Tenant canary through the router, guard ACL binding off: 300 seeded routed requests, >= 100
  executed, zero foreign ids, tenant values, canary values or hits.
- Spend: `model_calls` stages typed_router, sql_surface, planner; through the real provider the
  router's reported usage (300/20) is in the trace; validation requires 5 calls (router + SQL writer
  + repair + planner + generator; 4 with `repair_attempts: 0`; 4 in mode `ir`).
- Arm cards (`pipeline.decision.arm_cards`): absent by default (the sha256 pin of the planner
  request still passes); one card per enabled arm in wire order; every card input shape equals the
  wire-schema shape; deterministic; router cards and detailed catalog (types, descriptions,
  relations); SQL writer limits block. Measured prompt sizes on the synthetic deployment (system +
  serialised payload, characters; tokens estimated as chars/4, no tokenizer): planner 4,382 -> 9,595
  (~1,095 -> ~2,398 tokens), router 1,281 -> 4,996 (~320 -> ~1,249), SQL writer 7,939 -> 8,531
  (~1,984 -> ~2,132).
- `Config.model_dump()` (python and json modes) writes the projection relation key as `from` and
  `validate_config` accepts the dump; the re-validated config dumps identically.

Tests: unit+contract 3,309 passed, 1 skipped, 0 failed (was 3,258/1/0). New:
`tests/unit/test_metadata_protected.py` 10, `tests/contract/test_typed_router_engine.py` 29,
`tests/contract/test_arm_cards.py` 12; `test_question_packing.py` default assertion updated (0.25).
Ruff clean; `docs/config.schema.json` regenerated.

Limitations: synthetic catalog only; no measurement of router accuracy, planner schema-violation
rates or answer quality with the router or the cards (needs an owner-budgeted real-data rerun);
token figures are character estimates; protected keys can exceed the evidence budget by the traced
overflow.

## Unified SU history I1: day ledger, provisional days, tiered aggregate — 2026-11-08 (branch `v2/phase-0`)

Evidence level: **synthetic only** (unit + contract tests with invented tenants/services/rules in
the repo; no real-service or real-data run). Plan: `docs/plans/unified_su_kg_phase1.md` rev 2, I1.

Checked (tests `tests/unit/test_episode_history.py` 36, `tests/contract/test_issue_history_tiers.py` 12):
- Streaming close: spilled builder documents equal `summary_documents`; the streamed close writes
  the same content-addressed shard id as before. 120 scopes x ~80 KB (9.8 MB input) close with a
  Python-heap peak (tracemalloc; SQLite's page cache not traced) of ~1.8 MB (~0.18 of the input;
  the test bound is 0.3). Not an RSS measurement.
- Count rule: a chain across two midnights counts once per range (5 ranges); continuation columns
  and `chain_first`; a gap before midnight gives `boundary_unknown` and a new episode; parity with
  the read-time `stitch` on 3 seeded random 8-day datasets x 12 ranges (rows = episodes, equal
  occurrences).
- Partial/overdue: a stale rolling capture with a missing expected scope waits through the grace,
  closes `partial` at D+1+2 with missing and stale scopes listed, flagged in rows and the ledger;
  a later complete set is refused (staging kept). An overdue day with no capture becomes a gap.
- Row-hash audit: adding day N+1 leaves earlier typed rows and digests byte-identical; an edited
  earlier `issue_day` row is reported by `check_chain`; pre-I1 archives backfill to identical digests.
- One endpoint: 6 unclosed days all served provisional and flagged next to 3 archived days with
  per-day sums equal to the inputs; a refresh carrying one scope keeps the other scope's version;
  the provisional->archive transition (closed but not served: still provisional; served: archive,
  provisional entry dropped) never double counts; gap and missing days are listed, never zero;
  3 seeded random refresh/close/publish interleavings keep every day in one tier with sums equal to
  the ground truth; HTTP tool output equals the Python call; operator-only refusal.
- Scale shape: 400 archived days aggregated with the EAV `execute` patched to fail and
  `MAX_MATCHED` = 10: answered, 3 SQL rows read, Python-heap peak < 5 MB; month grouping sums equal.
- Incremental publish (coordinator requirement): for N = 10 and N = 40 archived days, adding day
  N+1 writes exactly one day's rows, the archive pointer advance ingests nothing into a generation,
  and the following live-generation publish ingests the same document count for both N.
- Rules routing: "How many ML outages ... last 3 months ... reason?" -> `issue_history` with
  `category` = the `ml_*` values; reason note states classified signal, not cause.

Not measured: real-data latency (3/6/12-month p95), close RSS/time at ~80k rows/day, real
cross-midnight frequency, live-generation publish time (still a full rebuild of the `keep_days`
window; ~249 s per kept day measured externally by the coordinator, so keep `keep_days` small).

## Parity-rerun defect fixes (corpus B, ticket analytics) — 2026-11-08 (branch `v2/phase-0`)

Evidence level: **synthetic tests plus an offline replay of recorded replies**. The fixes come from
the 2026-11-08 production-workload parity rerun (corpus B; earlier service 42/45 judged correct on
q001-q045, package 15/45). No new model calls, no rerun of the corpus, no judge; whether the
package now matches the earlier service on corpus B is **not measured** (needs the owner-budgeted
rerun with the keys below).

Checked with synthetic fixtures (`tests/unit/test_parity_fixes_contract.py` 34,
`tests/unit/test_parity_fixes_decision.py` 37, `tests/contract/test_parity_fixes_engine.py` 10, plus
new and updated cases in `test_typed_router_engine.py`, `test_sql_surface.py`,
`test_sql_route_engine.py`, `test_query_compiler.py`):
- Contract: an exact count with a framed sample list (`examples` claim, 1-10 cited records) passes
  on a list question; the same list without the claim, next to a bounded count, unframed, or next to
  "That is the complete list." is refused; invalid `examples` claims (completeness, no records, over
  10 ids) are `examples_claim_invalid`. 12 denied/advised completeness phrasings are not
  statements; 4 asserted ones still are; field labels, double negatives, records without a value,
  passage references and auxiliaries after "how many" are not statements.
- SQL writer: a schema-violating reply gets the one repair round (field paths only, never the
  reply) and the repaired SQL executes; two violations are the `sql_error` drop; the round is
  shared with SQL repairs; the prompt example parses as JSON. `models.roles.query_writer` keeps a
  registry-default `native` mode for Sonnet 5.5 while the planner role resolves to prompt mode
  (`LlmPlan` over the `output_config.format` limits); router and SQL writer run on it when selected.
- Router concurrency: a barrier test proves the planner call starts while the router call is in
  flight (3 consecutive requests); the trace's `model_calls` order stays router, writer, planner;
  `parallel: false` or a call budget that cannot cover both runs sequentially; a planner error waits
  for the router chain.
- Planner abstention (invalid step or plan-schema violation) with an executed routed count on an
  aggregate question: the count is the plan and the contract passes; no aggregate question, or a
  caller-typed aggregate: still discarded/abstained.
- After a failed route (unknown value), an exact planner count removes the per-intent
  `aggregate_intent_without_scope` flags (the route's `value_not_observed` stays); a bounded count
  keeps them.
- 16 synthetic ranking/range/existence/trend phrasings keep the planner's structured step; 3 lookup
  questions still drop it. Rules: a ranking compiled to one total is refused; "which X currently
  has / has opened / filed the most Y" is a ranking; "escalated" (not a record verb) is not
  swallowed; mention counts are `bounded` (`qualifier_not_applied`), quoted phrases stay exact.
- SQL lineage: a scalar single-table aggregate carries its contributors and up to 8 ids under the
  request's ACL and filters; grouped, joined, nested and row-level SQL claim none.

Offline replay (private workspace, aggregates only): the recorded generator replies of the rerun,
re-checked with the new `AnswerContract` against aggregate evidence rebuilt from the recorded
aggregates (passages not supplied, so quoted-number exclusions are not applied): T1 (sql_surface,
84 parseable replies) abstained 36 -> 13, passed 32 -> 54; T3 (ir, 41) abstained 17 -> 5, passed
16 -> 29. One reply moved from stripped to abstained (a ranking question now detected as an
aggregate). This replays the contract only; it says nothing about answer correctness, and the
decision-side fixes (router/planner/rules) are not replayed.

Not measured: corpus-B correctness after the fixes, SQL-writer schema-violation rate in native
mode on the real provider, latency gain from the concurrent router (recorded T1 medians: router 2.4 s,
SQL writer 3.9 s, planner 2.5 s; the expected saving per routed question is about the shorter of
the planner call and the router chain, not measured), rules-path answers on the 100 corpus-B questions.

## Corpus-B loss-analysis fixes F1-F11 — 2026-11-09 (branch `v2/phase-0`)

Evidence level: **synthetic tests plus a $0 offline contract replay of recorded replies**. The
fixes come from a private per-question loss analysis of the 2026-11-08 corpus-B R3 rerun (package
31/45 judged correct on q001-q045, the earlier service 42/45). No model calls, no rerun, no judge:
corpus-B correctness after these fixes is **not measured**.

Checked with synthetic fixtures (`tests/unit/test_corpus_b_fixes.py` 60,
`tests/contract/test_corpus_b_engine.py` 6; two cases in `test_sql_surface_guard.py` updated to the
new positions, same edge cases):
- F1: a ranked SQL result's row cell backs the generator's count (engine generator path now passes
  rows/columns; the same answer without rows is `answer_number_mismatch`); a number no row holds
  still fails and the contract renders the **cited** SQL result (`SQL result: module=..., n=...
  (exact; 1 row)`), never an uncited structured rank; a cited exact aggregate without a rendering
  yields no rendered fallback (abstain); "8 contributing document IDs" is backed by the retained
  ids, "8 records" is not.
- F2: gerund/modal advice ("Getting the full list would require ...", "could be produced", a
  parenthetical advice label, "To see all ...") is not a completeness claim; "Here is the complete
  list:", "The full list is:" still are.
- F3: an aggregate `ORDER BY` the SQL did not project is added as `sort_value`; ties at a `LIMIT`
  are kept (also with a tie-breaking secondary key) and reported (`sql_ties`); an `OFFSET` reports a
  tie with the row before it; ties past the 50-row window are `result_truncated`/`bounded`; tenant
  binding holds for tie rows; row-level and unlimited statements are unchanged.
- F4: 9 positions (`COUNT(CASE ... END)`, `SUM ... ELSE 0`, `AVG(IIF(...))`, negated in `ORDER BY`
  -> numerator; `ELSE 0` under `COUNT`, `ELSE 2`, `OR`, a subquery, a bare `CASE` column -> other); a
  numerator applies the qualifier for a share question only.
- F5: six ranking phrasings detected ("spans the most distinct", "has the most open+pending",
  "who is second for", "appears most often", "next two highest", "second-most"); three prose
  questions stay retrieval; "ratios" is a share.
- F6: `text_columns` validation (name clash, source form, description required, shadowing a
  metadata field), unchanged spec fingerprint without them, builder refuses a batch without the
  documents, LIKE counts per tenant file, `=` refused (`free_text_equality`), no values or grouping
  for text columns, quoted phrase and (with a mention cue) a value applied by a LIKE; through the
  engine write path the column is built and a mention count is exact; without a mention cue a
  value matched only by LIKE stays `qualifier_not_applied`.
- F7: all ACL-scoped values of a listable column under `list_values_up_to` (marked `(all values)`),
  samples above it, none with `sample_values: 0`, the access column withheld; value hints map
  "double charge" / "Report Builder" to stored values and stay empty otherwise.
- F8-F11: a NULL-key group is never ranked or kept by a top-N unless the scope asks for missing
  values; a group-key sort puts NULL last; a top-N sorted by the group key is refused; a time-basis
  label maps to its field; `metadata_show_null` packs `null`; `validate` warns for a SQL writer
  under 2048 output tokens.

Offline replay (private workspace, aggregate counts only): the recorded generator replies of the R3
run re-checked with the new `AnswerContract`, aggregates rebuilt from the recorded records (rows
passed, SQL renderings computed with the new renderer; the recorded exactness labels are kept, so
the F3-F7 route/catalog fixes are **not** replayed). q001-q045: passed 27 -> 35, stripped 12 -> 5,
abstained 6 -> 5 (baseline = the same replay on the pre-fix code). q046-q100: passed 23 -> 23,
stripped 21 -> 19, abstained 11 -> 13. The three strip -> abstain moves (one in q001-q045, two in
q046-q100) are ranking questions the F5 grammar now detects as aggregate questions while their
recorded SQL evidence still carries the pre-fix `bounded` label (the label F4/F5 remove in a live
run); with that label the stricter outcome is the expected one. The replay checks the contract only,
not correctness.

Not measured: corpus-B correctness, the SQL writer's use of the new prompt rules, value hints and
text columns, latency, and the frozen-plan SQL replay on the rebuilt store.

## Legacy QA compatibility layer: UI/API parity gaps — 2026-11-09 (branch `v2/su-compat-parity` from `v2/phase-0` `bcebd31`)

Evidence levels, kept separate:

- **Synthetic / contract-mocked (repository).** `tests/contract/test_legacy_qa_parity.py` (17) and
  `tests/unit/test_legacy_qa_units.py` (+3) on the 12-record synthetic corpus with the loopback
  `FakeChatServer`. They pin: UI-channel alias links for records without a URL and canonical links
  on other channels, alias resolution only through the token and rules (unknown, hidden, unaliased
  or out-of-charset ids are the same 404), profile validation of overlapping prefixes and tokens;
  search `guidance` notes, a `limitation` notice for an unobserved filter value, `facts` citing their
  numbered structured block (page 1 only), `enrich` notes attributed per result with an invented id
  dropped, metered (one call; `compat.usage`) and off unless enabled; session follow-ups answered
  from the anchor's evidence with the anchor's numbering and provenance (one generator call, no
  planner or arms; the earlier answer is never sent), every gate outcome (other client, other
  session, no cue, new record id, changed record context, generator decline -> full search), the
  stream route and the disabled case; `Engine.answer_from_evidence` (order, missing ids, empty and
  unknown provenance refused) and its access scope (tenant policy drops out-of-scope ids);
  per-dataset health (200/410/404, combined map), `/ds/<id>` prefixes for answer and search,
  prefix/body disagreement 422; clusters from package metadata groups (exact counts,
  representatives, cache, fail-closed start-up); embedder warm-up reported in `/health`; the agentic
  envelope from rounds/review/hops; adapter stage timings.
- **Real service, private staging (sanitised aggregates only).** Same staging as 2026-11-08 (copy of
  the private support-knowledge parity store, planner `claude-sonnet-5-5`, generator
  `claude-haiku-5-5`, unmodified UI copy, gateway copy with one base URL per dataset). 5 paid calls:
  UI hybrid question (33 citations; 31 ticket citations rendered as "Open source"; the opened record
  page rendered title and 8 sections, not "Source unavailable"), UI agentic question (DAG with 3
  steps and grading "sufficient" visible in the agentic panel), gateway REST `answer` deep turn and
  its follow-up on the same `X-QA-Session` (`cache_hit`, 10.6 s, US$0.0033 vs 22.9 s, US$0.0194 for
  the deep turn), typed search with `enrich` (5/5 results annotated, 0 dropped; one aggregate fact
  "3 records, exact" citing S1). Free checks: gateway health reports the served dataset up and the retired
  dataset not up (410); `/api/clusters` served 20 metadata groups of 1,709 records; evolution and
  replay runs report `pending`. 0 page errors.
- **Latency (real service, small sample; not a benchmark).** 2026-11-08: median 21.3 s over 10
  replayed questions (the earlier service's recorded median 16.5 s). Stage timings now show the
  adapter's own work is 1-5 ms per answer (prepare, envelope, overhead). There was no stream-to-QA
  double work to remove: `/api/qa` drains one answer run. The cost is the model calls. The planner
  took 2.1-6.2 s. The generator's first token came 10.5-11.1 s after evidence was ready, and generation
  took 14.6-20.2 s in total. That pattern fits Haiku 5.x thinking by default, since thinking deltas
  are not streamed; this is inferred, because thinking token counts were not exported here.
  Fixed: a $0 profile on the same store measured 3.65 s to load the embedder on the first query
  (0.07 s when warm; warm retrieval 0.07 s). The start-up warm-up now takes this load off the first
  request. Deep answers after the change: 17.0 s (hybrid, UI), 22.9 s (hybrid, gateway) and 23.9 s
  (agentic). Session follow-ups: 10.6 s. No claim of a lower deep-path median.
- Spend: US$0.130 in this run (staging ledger, cumulative US$0.569 of the US$2.00 cap).

Still flagged (not grounded or no package equivalent): case-segment and community evidence, KG/graph
node export, reranker, `delivered_evidence` text, `agentic.memory`/`agentic.skill`, policy
refusal notices on search, the earlier service's model gate and standalone-question rewrite for
session follow-ups, embedding clusters (served as metadata groups), evolution and replay runs. The
gateway shows a retired dataset as "down/unavailable (HTTP 410)", not "retired": it reads the body
`status` only on 2xx.

## Retrieval-parity features (rerank, segment arm, imported communities, record attach, attached graph) — 2026-11-09 (branch `v2/su-retrieval-parity`)

Evidence levels, kept separate:

- **Synthetic (repository).** `tests/unit/test_rerank_stage.py` (14: ordering, pins, `top_k_in`,
  `top_k_out`, `fail` vs `fusion_order` for load/score errors, wrong score count, timeout budget,
  config defaults and the unused-`reranking` refusal), `tests/contract/test_retrieval_parity_features.py`
  (16: segment build/anchoring/kinds, parent-scope filters/ACL/exclusions, stale anchors, tampered or
  incompatible index, engine rerank and its failure policy, imported communities keeping only grounded
  claims, summary refusal, extractive fallback, claim hits with lineage and ACL, attach join and its
  fail-closed cases), `tests/contract/test_graph_attach_engine.py` (3: an attached Kuzu store is never
  a write target, `path_evidence` hits, off by default), `tests/unit/test_embedding_concurrency.py` (1).
  Hash embedder, fake scorers, sqlite and a synthetic Kuzu store; no model download.
- **Private $0 retrieval check (sanitised aggregates; no model calls).** Corpus B (support tickets +
  documentation, 1,709 records, imported bge-m3 vectors): a copy of the 2026-11-04 store rebuilt under
  this code, the 2026-11-04 rules configuration as baseline, and the same copy with the features
  enabled (segment index of the 9,363 case segments the earlier service's case arm served, 100%
  anchored, built on GPU in 14 s; 238 imported communities of which 218 kept, 236 of 998 claims
  grounded in the corpus (the others quote comments that are not part of the package records),
  every summary refused, 101 extractive; three one-to-one attribute tables of the earlier knowledge
  graph attached to all 1,231 tickets; its Kuzu store attached read-only). Retrieval-only variant
  (structured arm off, `rules` with `raw_all_arms`) on the 45 graded questions; gold = record ids the
  earlier service cited or delivered (214 ids over the 20 questions that have any; many are
  incidental citations of aggregate answers, so this measures overlap with the earlier service's
  evidence, not relevance):

  | variant | recall@20 (all / tickets / docs) | MRR (all) | questions hit@20 | search p50 / p95 |
  |---|---|---|---|---|
  | 2026-11-04 config (dense + lexical) | 0.200 / 0.280 / 0.091 | 0.226 | 15/20 | 0.08 / 0.10 s |
  | + segment, community claims, graph (attached), attach; no rerank | 0.235 / 0.357 / 0.081 | 0.241 | 15/20 | 0.79 / 1.28 s |
  | same + rerank (bge-reranker-v2-m3, CUDA fp16, top_k_in 60) | 0.194 / 0.264 / 0.126 | 0.307 | 15/20 | 0.91 / 1.28 s |

  Rerank latency on CUDA: p50 0.091 s, p95 0.108 s for about 49 pairs per query. On the CPU
  (fp32, no budget) p50 13.6 s, p95 20.3 s on a shared host with load average 18-35 (ranking
  metrics equal to CUDA within fp16 noise: MRR 0.298). With a 3 s budget on the CPU every query
  failed open to fusion order (metrics identical to no rerank). The first such run showed that
  abandoned scoring calls queued behind each other and kept the process busy for about 10 minutes
  after the last query; the stage now runs one call at a time in a daemon thread and later calls
  time out within their own budget (rerun: 13 `timeout` + 32 `busy` fail-opens, all within 3 s,
  process exit 215 s after start). Arms with hits: segment 45/45, graph
  24/45 (with `path_evidence`), community claims 10/45. Case-arm parity on the earlier plan's recorded
  semantic queries (9 questions, 56 tickets the earlier case arm returned): 19 overlap (34%), top-1
  equal 4/9; segment dense half alone 23 (41%). The earlier arm was often scoped by a SQL ticket set
  and a segment-type filter, which this check does not reproduce.

Not measured: answer quality with these features (needs the planner/generator; the llm deployment
variant only passed `doctor`), community-arm usefulness on aggregate questions, CPU rerank on an
unloaded host (the shared host's load average was 23-34 during the runs).

## Answer-contract B4 fixes, `on_violation: annotate`, Anthropic thinking control — 2026-11-09 (branch `v2/phase-0`)

Evidence levels, kept separate:

- **Synthetic (repository).** `tests/unit/test_contract_b4_fixes.py` (25: exact count without a
  typed claim, ranking/ratio thresholds, result sizes, inline-only citations, the requested N, advice
  row parameters, the OFFSET rank position, quoted question phrases, the status-breakdown intent,
  quoted-phrase punctuation, distinctive value words, multi-term mention + status bound in SQL and
  the unbound-module, "and"-question and unrequested-term refusals, conflicting mention counts),
  `tests/unit/test_contract_annotate.py` (10: annotate notes, hard guarantees, number correction,
  uncited removals), `tests/unit/test_sql_text_any_of.py` (5: guard `any_of` groups on a synthetic
  projection), `tests/contract/test_contract_annotate_engine.py` (3: strip vs annotate through the
  Engine, `trace.generator_latency`, an OR group applying the quoted phrase end to end),
  `tests/contract/test_anthropic_thinking.py` (23: per-model thinking/effort validation and wire
  form, `first_token_seconds` on a streamed call, Haiku 4.5 as the generator with forced tool use and
  a $1/$5 per MTok operator price table). Mocked HTTP and fake roles only. Unit + contract + qa:
  3,708 passed, 2 skipped, 0 failed (baseline 3,642 passed, 2 skipped).
- **Provider metadata ($0, no tokens).** The provider's model-capabilities endpoint, read once for
  `claude-haiku-4-5`, `claude-haiku-5-5` and `claude-sonnet-5-5`, reported the thinking types encoded
  in the registry (Haiku 4.5: enabled/disabled, no effort levels; Haiku 5.5: adaptive/disabled, effort
  low..max; Sonnet 5.5: adaptive/between_tools, effort low..max). No message was sent: the request
  bodies (`thinking: {type: between_tools}` for Sonnet `off`, `{type: disabled}` for Haiku 5.5,
  `output_config.effort`) are contract-mocked, not exercised against the real API. The rule "Haiku 5.5
  disables thinking only at effort <= high" is the owner's statement, not read from the endpoint.
- **$0 contract replay of recorded corpus-B generator outputs (private, sanitised counts).** The
  recorded generator JSON of runs B4_P1 (candidate 96a1b0e) and B3_P1 (bcebd31), q001-q045, re-enforced
  with this code. "recorded" keeps each aggregate's recorded exactness; "relabel" re-applies this
  slice's engine-side changes to the recorded aggregates (the `any_of` OR-group check on the recorded
  SQL with the store's observed values, quoted-phrase punctuation, distinctive value words, the
  breakdown intent vs `NO_OPERATOR`, `_reconcile_counts`); the generator text is the recorded one in
  both. Outcomes (passed / stripped / abstained / annotated):

  | run | recorded in the run | strip, recorded labels | strip, relabel | annotate, relabel |
  |---|---|---|---|---|
  | B4_P1 | 29 / 10 / 6 / - | 41 / 1 / 3 / - | 41 / 1 / 3 / - | 41 / - / - / 4 |
  | B3_P1 | 27 / 11 / 7 / - | 38 / 5 / 2 / - | 39 / 5 / 1 / - | 39 / - / - / 6 |

  B4 strip, relabel: q001 (status breakdown) now exact and passed; q028, q034, q042, q045 (exact
  answers refused for want of a typed claim or for restated thresholds/row counts) pass; q037 now
  abstains (the recorded text stated the all-chunk count 5, which is demoted against the exact
  text-column SQL count 1); q041 stays bounded (the SQL omits the module filter: the distinctive word
  of the module value is now an unapplied qualifier) and abstains; q043 abstains (its ratio numerator sits in a
  derived table, still `other`); q044 strips the all-chunk 28 and keeps the exact 27. Under annotate
  those four are answered with notes. q011's recorded text says the second-ranked row is missing (a
  generator misreading of a one-row OFFSET result); it now passes the contract unchanged; the new
  rendering states the rank position, which may prevent that misreading but was not re-run.

Not measured: answer correctness of the replayed outcomes (no judge call; the judge graded the
recorded contract outputs), any paid generator run with `annotate`, `thinking: off` or Haiku 4.5, the
real first-token latency, and the real-API wire acceptance of `between_tools`.

## Corpus-B B5 package-side losses: value grounding, superseded counts, bounded zeros, per-group shares — 2026-11-09 (branch `v2/phase-0`)

Scope: the four package-side corpus-B losses of run B5 (candidate `50f47e7`; q020, q023, q041, q043).
Evidence levels: synthetic unit/contract tests (fake roles, in-memory projection); a $0 private replay of
the recorded B5 SQL statements and planner scopes against a byte copy of the B5 store (no model call;
`JsonModel.call` raises); a $0 private contract replay of the recorded B5 answers. No paid call, no
judge call; no question was re-generated, so no answer correctness is claimed.

- **Synthetic tests.** `tests/unit/test_b5_fixes.py` (37: the value-normalization rule table, no
  fuzzy match, ambiguity refusals, request filters never rewritten, the question-grounded cross-field
  move and its three refusal conditions, the IR compiler refusing a move, the exact-count correction
  in strip and annotate, two differing exact counts left alone, bounded-zero rendering and removal,
  per-group shares with `min_count`, dense ranks and boundary ties), `tests/unit/test_b5_sql.py` (12: the
  guard on a synthetic ticket projection: `organization = 'Ledgerly'` moved to `module` only with
  the user's question, spellings rewritten and recorded, `ambiguous_value`, a multi-term OR group +
  status + moved value exact, numerators in a derived table and a CTE applied, a filter subquery
  still `other`), `tests/contract/test_b5_engine.py` (3: a bounded SQL count `superseded_by` the
  exact structured count and absent from the generator payload, the wrong number corrected under
  strip and annotate; a planner and SQL value on the wrong field run on the grounded field through the
  Engine). Unit + contract + qa: 3,762 passed, 2 skipped, 0 failed (baseline 3,708 passed, 2 skipped).
- **Replay of recorded SQL / planner scopes on the B5 store copy ($0).** Per question:

  | question | B5 recorded | replayed under this code |
  |---|---|---|
  | q020 count of a value the planner put on the wrong field | SQL refused (`value_not_observed`), structured step not executable; answer declined | SQL exact 71 (`module` = the one module value whose distinctive word the question names, `rule: question_field:token`, `column_as_written: ticket.organization`); planner scope exact 71; the expected value is 71 |
  | q023 bounded vs exact count | SQL bounded 24 and structured exact 35 both packed; annotate kept 24 | SQL 24 `superseded_by` the exact 35 (not packed); on the recorded text the contract removes the 24 and states "records: 35 (exact ...)" (annotate) or renders it (strip) |
  | q041 multi-term mention + "active" + wrong field | SQL refused; structured bounded 0 rendered "(exact; 0 ...)"; annotated "0 tickets" | SQL exact 3 (module moved, status IN grounded, OR group `any_of`, no unapplied qualifier); the structured bounded 0 is superseded by it, relabelled "(bounded, not an exact zero ...)", and annotate removes a zero count; the expected value is 3 |
  | q043 "next two" urgent ratios among groups with >= 20 tickets | SQL read "next two" as ranks 3-4 (OFFSET 2): exact, wrong pair; structured share step refused (group-key sort) | unchanged for the recorded B5 SQL and planner scope (the reading is the SQL writer's / router's). The B4-recorded derived-table SQL is now exact (numerator applied) and returns the expected second- and third-ranked organizations (0.238, then a 0.2 tie of two organizations, both listed). A corrected structured scope (`order: desc`, `top_n: 3`, `min_count: 20`; not a recorded plan) returns ranks 1-3 with numerators (0.3 = 12 of 40, 0.238 = 5 of 21, 0.2 = 6 of 30) and the other 0.2 organization in `ties` |

- **Contract replay of the recorded B5 answers ($0).** B5 used native tool use for the generator and
  its provider dump holds no generator text, so the delivered answer minus the annotate block is the
  input; for the six annotated answers this lacks the sentences the run's contract removed (a
  partial reconstruction). Outcomes (passed / stripped / abstained / annotated), recorded aggregates
  kept as the generator saw them: strip 42 / 2 / 1 / -, annotate 42 / - / - / 3 with recorded labels
  and with this slice's relabelling. q023 changes from an annotated 24 to a corrected answer; q009 and
  q017 pass only because their removed sentences are missing from the reconstruction.

Not measured: answer correctness after a re-generation (no paid run), the router/SQL-writer reading of
"next two" (q043 remains a likely loss until the plan or SQL ranks from position 2), and the planner's
use of `min_count` (the private planner prompt does not mention it).

## Corpus-B B7 answer-quality fixes: possessives, Markdown-safe strip, ratio parts, small-group lineage, exact-first precedence — 2026-11-09 (branch `v2/phase-0`, base `1348a6d`)

Scope: the package-side causes recorded for the corpus-B B7 rerun (candidate `1348a6d`: q027, q029,
q038, q040, q045 shapes) and QA findings QA-07/QA-08 of the 2026-11-09 staging QA. Evidence levels:
synthetic unit/contract/qa tests (fake roles, in-memory projection); a $0 private replay of the
recorded B7 SQL statements against the B5 store copy (no model call; `JsonModel.call` raises; output
in the private workspace). No paid call, no judge call, no question re-generated: no answer
correctness or parity change is claimed.

- **Synthetic tests.** `tests/unit/test_b7_answer_quality.py` (30: singular possessives are not
  counts while plural possessives and plain counts still are; Markdown-preserving strip for bullets,
  labels, numbered items, headings, blockquotes and table cells, an emptied item dropped whole; ratio
  parts added or reused, constant factors peeled, `DISTINCT`/constant divisions left alone, "N of D
  (rate)" rendering; small-group ids per returned group with ACL, `WHERE`, `HAVING`, `LIMIT`, NULL
  keys, the threshold and unprojected keys; the config default/bound; exact-first packing with
  `precedence`; prompt rules), `tests/qa/test_qa_20261109.py` (4: QA-07 and QA-08 reproduced on
  synthetic text; 3 of them fail on `1348a6d`). Updated: `test_corpus_b_fixes.py` F1/F3,
  `test_corpus_b_engine.py` (2 rows), `test_contract_annotate_engine.py` (1 row) and
  `test_sql_surface.py` (the grouped row of the no-lineage test moved to its own test, with the old
  behaviour kept under `group_lineage_max: 0`): grouped renderings now list small groups' records.
- **Replay of recorded B7 SQL ($0).** q045's ratio statement now returns its parts and renders
  "27 of 40 (0.675)" for the top organization (the parts were absent in B7); q043's ratio rows render
  "5 of 25 (0.2)" / "6 of 30 (0.2)"; q040's `LIMIT 1` group (4 records) lists its 4 contributor ids;
  q029's scalar count keeps all 3 ids; q027's recorded `status IN (...)` SQL still executes as
  written (the literal-value rule is a prompt rule, only a new SQL-writer run can show its effect);
  q038's exact one-row result is unchanged and is now packed ahead of bounded aggregates.

Not measured: whether the writer follows the new ratio and literal-value prompt rules, whether the
generator prefers exact over bounded evidence and lists only evidence records (all need a paid
rerun), and q040's "of 6" total (the recorded SQL never computes it; lineage does not add a total).

## Unified merge F0: synthetic multi-source fixtures and the 600k-chunk log-day generator — 2026-11-09 (branch `v2/phase-0` worktree, base `90271d4`)

Scope: increment F0 of `docs/plans/unified_merge.md` (rev 3). Evidence level: **synthetic only**. No
service, no model call, no private data; nothing here measures retrieval quality, latency on real
data or the §6.7 targets (those belong to M1a/M2a).

- **Small collection** (`tests/fixtures/unified/`, about 330 KB, generated by
  `su_multi_arm.testing.fixtures.unified.build_files()`; a test fails when the committed tree drifts
  from a fresh build). Members: adopted support (12 tickets, 6 docs), tracker snapshot (4 issues),
  log archive `month:2026-10` and days 2026-11-04..06 under two keyword epochs, provisional days
  2026-11-07..09 as base/hourly/five-minute/day-rebuild slices with a ledger, analytics days
  2026-11-07/08. Log documents are built with the write path's own `ingestion.rollups._document`, so
  the real id behaviour is present: `signature` ids repeat in every log member and slice, incident ids
  carry no day, one incident id is held by an archive day and a provisional slice (an episode crossing
  midnight into the live window), supersedes within and across days, and a gap-fill merge (two groups
  360 s apart joined by late lines) whose later id disappears from the day rebuild (tombstones listed).
  Identity registry with every status (matched by exact/casefold/curated+review, candidate, contested,
  ambiguous, excluded, unmatched, missing) and a per-namespace report; link candidates of tiers A/B/C
  with every status, planted links and negatives (outside window, contested/candidate/ambiguous alias,
  dangling click); traps; a §8.3 onboarding file. 23 tests check internal consistency, the shared
  metadata schema (`unknown_fields: reject`, zero violations) and one monolithic sqlite ingest + search.
- **Live-shape config** (`live_shape/`): the block structure of a live support config with invented
  values; materialized side files (attach sqlite, precomputed vectors) and `load_config` +
  `config_diagnostics` with zero errors. Not run as an `Engine` (no kuzu store or segment sidecar is
  built).
- **Large log day** (`su_multi_arm.testing.fixtures.unified.log_day`, `python -m …`): default spec
  measured once in the external workspace: 288 five-minute refreshes, 600,002 final chunks
  (185,675 documents, 712,819 versions emitted, max 3,613 upserts per refresh, 133,460 embeddable
  texts of which 45,195 unique), 54 MB gzip, 20.7 s, 176 MB peak RSS; a second run was byte-identical.
  The small-spec tests check the chunk count against `chunk_document`, determinism and the refusal to
  write inside the repository. The generated day is never committed.

## M0 log-side fixes: plan check, explicit provider retries, question grounding, bounded incident selection — 2026-11-09 (docs/plans/unified_merge.md rev 3, base `90271d4`)

Scope: increment M0 of the unified merge plan, code only. Evidence level: **synthetic and
contract-mocked** (unit tests, mocked HTTP provider replies, the synthetic logs gold pipeline). No
deployment, no live service, no log host, no paid model call. Nothing here shows that the log
deployment's answers changed; that needs the owner-approved deployment window (§9 NB11).

- **`budgets.plan_check`** (`tests/unit/test_plan_cost.py`, 15): slot list per the §6.13 formula
  incl. planner schema repair, typed re-plan (`typed_query.repair_attempts`), typed router + SQL
  writer with repair, review hops, generator + `on_incomplete` retry, the `community` answer role,
  transient-retry slots reported separately; `max_llm_calls` most expensive slots; evidence raised
  to `request.evidence_chars_max`; `warn` (default) logs and starts, `refuse` raises; unpriced roles;
  the engine runs it at startup; a synthetic copy of the large support shape exceeds a 0.25 cap and
  warns. With the built-in prompts and illustrative prices (strong model 2/10, small model
  0.10/0.50 USD per MTok), planner 2,048 out + one repair + small generator 6,000 out + one
  `retry_shorter`: W ≈ US$0.066 (5 calls); with a declared ~30k-token planner input W ≈ US$0.18
  (≤ 0.25, the plan's ≈ 0.17). Arm cards, typed/SQL catalogs and the question are not measurable from config and are
  listed as `unmeasured`: W is a lower bound for deployments that use them.
- **Provider retries** (`tests/contract/test_provider_resilience.py`, 13, mocked Anthropic HTTP and
  event streams): default `fail`; `retry_shorter` once with the brevity instruction then
  `ProviderIncomplete`; `retry_larger` with the larger ceiling; transient retry with backoff only
  inside the deadline; never for auth; a retry the budget refuses keeps the original error; a stream
  cut before `message_stop` is `incomplete_cause: stream_end` (retried as transient) unless it had a
  `max_tokens` stop; trace events carry `attempt`, `retry_reason`, `incomplete_cause`, and both
  attempts are committed on the request budget.
- **Question binding** (`tests/unit/test_log_question.py`, 22; `tests/contract/test_m0_log_answers.py`,
  5, gold pipeline, no model call): an undated outage question reads the last 3 days (default view)
  instead of today; "at 2" in a +05:30 question clock yields 02:00 and 14:00 with UTC equivalents,
  stated in the answer, and the day-level aggregate is `bounded`; ambiguous `log_around` runs once per
  reading; a vocabulary term grounds to indexed services only and a term naming only unindexed
  services refuses the scope; a pinned vocabulary file with a wrong digest refuses to load.
- **Bounded incident selection**: an over-limit selection (forced `total: 501`) is answered with a
  "Bounded selection" note and a per-client incident table instead of the former 503.

Suite: unit + contract + qa 3,560 passed, 14 skipped, 0 failed (baseline at `90271d4` in the
same venv: 3,504 passed, 15 skipped). Ruff clean. `docs/config.schema.json` regenerated.

Not measured: whether the log deployment's 502s were truncations or cut streams (the new
`incomplete_cause` must be read from the serve log after deployment), the real planner prompt size
on the log host (read `input_tokens` of `model_call` events), and any answer quality on the owner's
questions (needs the twin dataset, then the approved window).

## Unified merge S0: schema v3 skeleton, frozen member types, engine/CLI seams — 2026-11-09 (docs/plans/unified_merge.md rev 3.3, base `ec38b4a`)

Scope: increment S0 (lane L0). Evidence level: **synthetic only** (unit tests over the F0 fixtures
and the shipped examples). No service, no model call, no store outside pytest temporary
directories. Nothing here is served: the new configuration blocks are validated and refused by the
engine; no retrieval, ranking or answer path changed.

- **v2 compatibility** (`tests/unit/test_index_fingerprint_golden.py`, +21): every shipped example
  keeps its 2.0.0a1 index fingerprint; the backend-free `recipes.recipe_of(config)` hashes to the same
  golden values for all 8 examples (incl. postgres/neo4j ones, with backend construction forbidden)
  and equals `WritePath.index_recipe()` of a constructed engine for the 6 constructible examples and
  of a constructed write path for the F0 live-shape config (attached kuzu, projection, precomputed
  vectors, imported communities); a v2 example re-declared `schema_version: 3` keeps its fingerprint;
  `Config.fingerprint()` omits the unset v3 blocks.
- **Schema v3** (`tests/unit/test_config_v3_schema.py`, 47): the F0 trap `v2_with_collection` and every
  other v3 block in a v2 file are refused with the migration message; a v2 dump re-validates; all
  collection / crosssource / communities.layers rules (namespace overlap, retention invariant,
  adoption coherence, strata stats, quotas, layers, slice ladder and grace vs deadline, review role,
  approval and ceiling); v3 paths resolve against the YAML file; the engine refuses v3 blocks before
  opening a backend; the generated schema carries the new keys and the committed schema is current.
- **Frozen types** (`tests/unit/test_types_members.py`, 19): every F0 member instance is a
  `MemberRef`, every ledger entry a `SliceRef` (the gap-filling day rebuild carries the expected
  tombstones), the current ledger is a valid `PinnedView`, the archived-day-slice trap is refused (day
  and month members), frozen/strict models, key forms, catalog members, digests, relative facet
  locations, id-directory rows, protocol fakes (a tombstoned id is never resolved).
- **Seams** (`tests/unit/test_collection_seams.py`, 11): `Engine.view` is the single-member view;
  `collection describe` / `identity describe` read the config only (no store created), later
  operations exit 1 naming their increment; registry digest missing / mismatch / match.
- **Path fix** (`tests/unit/test_config_relative_paths.py`, 5): `ingestion.attach[].path`,
  `communities.imported.path` and `pipeline(s).arms.segment.path` resolve against the YAML file with
  a different working directory; the index fingerprint equals the pre-fix value.

Existing tests changed (equivalents kept): `test_config_v2.py` (schema version 4 is the refused
example; the schema's `schema_version` is `enum [2, 3]`), `test_config_cli_a2.py` (the inert v3 keys
are listed as not yet read, to be removed by the increment that reads them).

Suite: see the S0 entry of `docs/plans/execution_log.md` for the before/after counts.

Not shown: anything about partitioned serving (M1a onwards); the id directory, slices, adoption
and layers exist only as types and validated configuration.

## Unified merge M1a: partition core and equivalence kit — 2026-11-09 (docs/plans/unified_merge.md rev 3.3 §6.1, §6.2, §6.5, §10.2; base `2d4fed0`)

Scope: increment M1a (lane LA). Evidence level: **synthetic only** — F0 fixture documents
(`tests/fixtures/unified`) and `examples/documents.jsonl`, local sqlite (`lexical_engine: bm25`, NumPy
exact vectors), hash embedder, rules decision; no service, no model call, no real corpus. These
results show the partitioned engine reproduces the monolithic engine on these fixtures; they say
nothing about relevance or about the adopted live support store (M1b gate: QA100 on staging).

Equivalence kit (`tests/contract/test_partition_equivalence.py`, 23 tests):

- **Single member == monolithic**: support (ticket + doc), tracker and analytics (two day members)
  as one-member collections, and the example corpus: identical fused hits (ids, order, exact scores,
  chunk ids, text, title, source, url, arms, provenance), identical per-arm hits and aggregates for
  8 queries each (and a filtered request); identical answer results and identical packed answer
  evidence (support, 4 answers); identical grouped records result.
- **Per-stratum isolation (§6.5 guarantee shape)**: in the full F0 collection (4 members, 9 units),
  requests filtered to `ticket`, `doc`, `tracker` or `analytics` return exactly the hits of a
  monolithic engine holding that member alone; support-scoped lexical and dense candidates (ids,
  order, scores) are identical with and without the log/tracker/analytics members present.
- **Query-time statistics (B10)**: the log stratum (catalog + month + 3 day members) ranks like one
  store built from the same documents: identical chunk ids and order, scores within 1e-9 (11
  queries, > 50 compared candidates; the epsilon-floor average idf is summed in another order) — at
  backend level and through `Engine.search`.
- **Dense exact merge**: top-40 over all 9 units equals the monolithic exact top-40 (ids, scores,
  tie order) for 8 queries.
- **Records and listing**: 5 grouped/filtered `ScopeSpec`s over all members equal the monolithic
  results (groups, denominators, exactness); merged listings equal the monolithic listing for page
  sizes 1/7/50/500 with and without a kind scope; routed `get`, observed values (4 fields) and
  document/chunk counts equal.

Partition contracts (`tests/contract/test_partition_scope_pruning.py` 8, `tests/unit/test_id_directory.py`
12, `tests/unit/test_members_compose.py` 10, `tests/unit/test_partition_caches.py` 2,
`tests/unit/test_recipe_decomposition.py` 6, `tests/unit/test_cli_collection.py` 1): out-of-scope
members are never opened (per-unit read counters); id reads are one indexed directory lookup and an
unknown id is not found without a member scan; superseded and tombstoned rows are excluded from
lexical candidates and statistics, records, counts and listings (the view equals a monolithic store
of the latest versions); quotas reserve stratum slots and fused scores are RRF values; id-directory
uniqueness, same-day slice supersede, `replaces`, catalog-provisional exemption, tombstones relative
to the pinned view, incremental registration (statement count independent of the collection size),
fail-closed open; catalog kinds only in the catalog member; F0 duplicate-id trap refused with the
build removed; compose O(members) (no document read), CAS `stale_base`, refusals (foreign embedding
space, undeclared member, digest mismatch, directory inconsistency); `current()` compares only the
collection recipe; a second compose opens only the new member (cached segments/matrices reused);
partition recipes are location-free; CLI register/compose/status.

Suite (full `-m 'not external'`, same venv): before 3,831 passed, 58 skipped; after 3,893 passed, 58
skipped (+62: the new partition tests; one seam parametrization removed). Ruff clean. No `Config`
change (schema not regenerated).

Not shown: adopted stores, attached layers, slices, coverage labels, projection/SQL over members,
graph/community/segment arms over a partitioned collection, and any latency or scale measurement.

## Success heartbeats and `log_liveness` (O6) — 2026-11-09 (docs/plans/unified_merge.md decision O6, base `2d4fed0`)

Scope: package support for a success keyword family (`category: success`) in the log collection,
the failure views without success rows, and the liveness intent. Evidence level: **synthetic only**
(unit tests and engine contract tests over invented families, tenants and services). No service,
no log host, no model call. The collector change and its dry run on real data are a separate,
private proposal; nothing here measures real heartbeats, real cadences or real halts.

- **Failure precedence and unchanged failure data** (`tests/unit/test_success_heartbeats.py`, 30
  cases): success rules after every failure rule (refused otherwise by `compile_summary` and
  `load_keyword_recipe`); each failure rule's compiled filter is identical with and without the
  success family; a failure-only recipe builds documents byte-identical to the package before
  this change (golden digest of three fixture days); with the success family the failure-side
  documents equal those of the failure-only recipe; success runs carry no evidence, no community
  and no shared-error community; the coverage row counts success apart.
- **Heartbeat block**: 5-minute buckets recomputed from the minute series; a block that does not
  reconcile (count, missing bucket, non-success rule, bucket width, malformed row) refuses the
  snapshot; a block without success rules is refused; a snapshot without the block is accepted.
- **Engine** (`tests/contract/test_log_liveness.py`, 12): an archive with two failure-only days, the
  success family pinned without a new epoch (refused), then three days under
  `keywords.effective_from` and one provisional day. Old shards and recipes unchanged; `log_liveness`
  reports the hourly crawl that stopped as `halted` (quiet 1d 1h 59m vs 3 x 1h) and the other as
  `running`, lists the pre-heartbeat days, honours `liveness.*` settings, refuses a recipe without
  success rules and non-operators; recovery = first success after a failure episode (time to
  recovery 55 min on the fixture), `not_recovered`, `predates_heartbeats`; a lookback before heartbeat
  recording answers "unknown". `issue_history`, `incident_episodes`, summary `log_top` and the
  outage-investigation selection contain no success rows; the rules decision routes the liveness
  questions to `log_liveness` and the outage count still to `issue_history`.

Limits (stated): a halt is inferred from silence, not proven; quiet periods shorter than
`keywords.gap_seconds` are inside one run; recovery is a temporal sequence in one
tenant/env/service scope, not proof that the failed operation recovered; the 5-minute buckets are
validated but not stored separately (runs at `gap_seconds` resolution carry the same last-success
and interval information); tenant = the index's deployment tenant, not a customer id inside a
message.

## Unified merge M2a: live log path producer (slices, tombstones, compaction, archive members, query-time statistics, text-hash vectors, handoff) — 2026-11-09 (docs/plans/unified_merge.md rev 3.3 §6.7, base `2d4fed0`)

Scope: increment M2a (lane LB), library modules and tests; not reachable from the engine until the
partition core (M1a) wires them. Evidence level: **synthetic only** — unit/contract tests over the
F0 fixtures and seeded random interleavings, and a benchmark on the F0 synthetic 600k-chunk day
generator in the external workspace. No service, no paid model call, no live log host. The only
model used is the local `BAAI/bge-m3` (cached, offline) for the embedding measurement.

Tests (62 new; see `docs/plans/execution_log.md` for suite counts):

- **Ledger** (`test_log_slices.py`, 18): sealed slices verify; latest version wins with the supersede
  list and `excluded_stats`; unchanged documents are not resealed; refusals keep `CURRENT.json`;
  recipe change refused; log-line text in `embed_fields` refused; catalog kinds: first-seen →
  `catalog_provisional`, known → excluded, changed → parked; ladder (`due`) and the 19-unit bound
  (a seal beyond it is refused); closed days refuse refreshes; id-directory mirror and fail-closed
  rollback; cross-day supersede (F0 chain C).
- **Tombstones** (`test_slice_tombstones.py`, 5): the F0 gap merge — the full-day rebuild tombstones
  exactly the fixture's expected ids, the survivor holds 17 occurrences, a tombstoned id is never a
  candidate and leaves `N`; compaction keeps a tombstone only while an older row needs it; re-add.
- **Property** (`test_log_slice_tiers_property.py`, 3 seeds): random refresh / compaction / close
  interleavings over 3+ days with gap-filling merges and ids continuing across midnight (also out of
  an archived day): one tier per day, served rows equal the ground-truth model id by id and version by
  version (no vanished id served, nothing twice), `N` and per-document `df` equal the served rows,
  ≤ 19 units/day; at the end N-slice rankings equal the rankings after compacting each day.
- **Query-time statistics** (`test_query_time_stats.py`, 7): over 3 random days with merges, the
  ranking over N slices equals the compacted day and the monolithic reference `rank_bm25.BM25Okapi`
  (ids and order exact, scores within 1e-9); superseded/tombstoned rows leave `N`/`df`; the epsilon
  floor is reported as `average_idf_lag` and matches the reference after the base; a new slice warms
  only itself; dense ranking is stable under compaction and pending units are reported. LA's
  adopted-stratum scorer is not merged yet; `rank_bm25` is the reference scorer.
- **Members** (`test_log_members.py`, 5), **retention** (`test_slice_retention.py`, 7), **vectors**
  (`test_text_vectors.py`, 7), **handoff** (`test_handoff.py`, 5), **contract**
  (`tests/contract/test_live_window_scoring.py`, 5: F0 archive days closed through the ledger +
  catalog member + live slices in one pinned view; chains A–D and the gap merge served once).

Benchmark (`scripts/bench_log_slices.py`; one replayed day of 288 five-minute refreshes, 712,819
document versions, 600,002 final chunks, plus three earlier generated days loaded as one full-day
slice each; single process; GPU shared with other services):

| Quantity (§6.7) | Target | Measured (synthetic) |
|---|---|---|
| Refresh-to-servable, lexical/records | p95 ≤ 5 min | p95 0.38 s (lexical run), 0.59 s (dense run); max 1.16 s (writer held by a base compaction) |
| Refresh-to-servable, dense | p95 ≤ 15 min | p95 2.6 s, max 3.4 s (BGE-M3 on GPU, batch 16) |
| Generations activated per day by the log member | ≤ 6 (all sources) | 1 by construction (close; the catalog member rides in the same compose) — counted, not a compose measurement |
| Compose time | ≤ 10 s | **not measured** (compose is LA's, not merged); member-side inputs: archive day member build 12.6–14.8 s (background), transfer 0.23 s, full sha256 verification 0.17 s (200 MB member) |
| First request after a new slice | ≤ +1 s p95 vs warm | +0.43 s p95 (cold 0.45 s, warm 0.027 s p95), up to 21 pinned units over 4 days |
| Live-window lexical + dense | ≤ 3 s p95 | lexical p95 0.45 s cold / 0.027 s warm (≤ 21 units); dense per request p95 0.52 s, **max 7.1 s** on the first dense query after a base compaction (cold vector matrix load) |
| Units pinned per day | ≤ 19 | max 18 |
| Vector disk, live | ≤ 0.5 GB/day | 0.44 GB for the replayed day (216,465 unique texts × 2 KB float16, all intermediate versions); 4 days 0.69 GB on disk |
| Embedding throughput | GPU ≥ 300/s, CPU ≥ 30/s | GPU 476 texts/s over the day (shared GPU, per-slice batches), 916/s in an isolated probe; CPU 35.7 texts/s (probe, 400 texts) |
| Compaction (background) | — | hourly p95 1.9 s; base max 16 s (185k documents) |
| Slice GC | retired within one day after close | 289 slices collected during the day (grace 3,600 s on the simulated clock) |

Limits: the 76-unit worst case was not reached (the ladder kept ≤ 21 units); compose, activation
and engine routing are not measured (LA); the generated day has no gap-filling merges (they are
covered by the tests, not the benchmark); real log data and a staging week are not run; ledger disk
is 1.67 GB at the end of the dense run (documents, bm25 segments and embed texts per unit, three
earlier full days included).

## Unified merge: the live log member served by the partitioned collection (M2a wiring, lane LB) — 2026-11-09 (docs/plans/unified_merge.md rev 3.3 §6.5–§6.7, base `88d5928`)

Scope: the M2a live log path wired into the partition core (M1a/M1b): request pin with the slice
vector and slice pins, the log stratum through the real lexical/dense/structured arms, LA's id
directory under the ledger, close → one compose → release, the ledger built from `collection.*`.
Evidence level: **synthetic only** — tests on the F0 fixtures and a benchmark on the F0 600k-chunk
generated log day in the external workspace (`~/su_mutliarm_tests/claude/lb-wire/`), hash embedder
at 1,024 dimensions; no service, no model, no paid call, no real log data.

Tests (38 new: `tests/contract/test_live_log_engine.py` 21, `tests/unit/test_live_log_member.py` 17):

- F0 collection = adopted-shape support member + 3 archive day members + catalog member (closed
  through `LiveLogs.close_day`) + 9 live slices: searches return ticket, doc and log hits with hit
  text read from log units; `trace.collection.slices` lists the pinned slices.
- **B4 gate**: for 8 queries, support-scoped lexical and dense candidates (ids, order, scores), the
  unscoped support stratum ranking and engine searches filtered to `ticket` / `doc` (hits, per-arm
  hits, aggregates) are identical with and without the log member; no log unit is opened.
- **N slices == one compacted day** through the engine (6 queries, source `logs`): fused and per-arm
  ids/order exact, scores within 1e-9; the log stratum equals a monolithic engine built from the
  served documents (exact except where the declared `average_idf_lag` applies: same candidates).
- Served versions everywhere: chain B continuation served from the live slice, gap-merge tombstones
  never returned by `get`, records count = served incidents, paged listings = served ids once.
- Real id directory: chain B (`replaces=["logs/day:2026-11-06"]`, only the continuing incident and
  signature_day rows superseded), chain C, compaction retire, gc purge, a non-continuation duplicate
  still refused (`IdConflict`, ledger rolled back); close registers the day member and swaps the
  catalog unit; 1 activation per close.
- Pins: request leases hold slices (gc keeps them past the grace while a session holds them, deletes
  after release); `dense: pending` reported while vectors are missing, lexical still serves.

Benchmark (`scripts/bench_live_collection.py`; 2 days closed as archive members of 185.6k documents
each, 1 day live as a full-day slice, the replayed 600k-chunk day as 288 five-minute refreshes with
the compaction ladder; engine search = rules decision, lexical + dense arms, fusion, hit reads):

| Quantity (§6.7) | Target | Measured (synthetic) |
|---|---|---|
| Compose (close: day member + catalog, by reference) | ≤ 10 s | 0.007 s, 0.012 s, 0.018 s (3 closes; seal-level member verification) |
| Live-window engine search, warm | ≤ 3 s p95 | p95 0.073 s, max 0.077 s (≤ 18 pinned units) |
| First engine search after a new slice | ≤ +1 s p95 vs warm | p95 1.00 s (+0.93 s), max 3.97 s (one outlier) |
| First engine search after a base compaction (M2a: 7.1 s dense max) | — | max 0.86 s; dense facet alone max 0.029 s (warm hook) |
| Fresh process, first search over the whole window | ≤ 3 s p95 | **5.8 s first query** (cold open of three 185k-document units), then p50 0.044 s; gap reported |
| Refresh seal (now also `catalog.sqlite` + id directory) | p95 ≤ 5 min | p95 0.75 s, max 0.82 s |
| Base compaction / full-day seal (background) | — | base max 26.8 s; full-day seal 28–29 s (M2a: ≤ 16 s) |

Limits: one replayed day; cold-process start misses the 3 s target; the real-data run and the staging
week (§10.4) are not done; session pins are an API not yet wired into the serving layer.

## Live log member: start-up warm-up and session slice holds — 2026-11-09 (follow-up of the M2a wiring, base `c894999`)

Scope: `Engine.warmup` (pre-opens the request pin's units: partitioned members verified/opened,
live log strata via `LogStratum.warm_view` — readers, served-row masks, unit catalogs, dense
matrices; single sqlite stores: bm25 segment + vector matrix), `/ready` gated on it in the legacy
adapter (`warmup_timeout_seconds`) and `serve` (`--warmup-timeout`, `--no-warmup`), and session holds
(`LiveLogs.hold` union per turn, released by `Engine.close_session`). Evidence level: **synthetic
only** — F0 fixtures, fakes, and the same benchmark as the M2a wiring entry
(`scripts/bench_live_collection.py`, F0 600k-chunk generated day, hash embedder at 1,024 dims) in the
external workspace; no service, no model, no paid call, no real log data.

Profile before the fix (fresh engine, first search 4.7 s): dense matrices of the log units 4.6 s
(`TextVectorStore.get_unique` + unit embed rows), served-row masks and unit readers (JSON rows, bm25
segments) ~1.4 s in the lexical thread; catalog connections and the id directory were negligible.

Benchmark (same arguments as the M2a wiring entry; the live window at the end of the replay = 4
members + 2 slices, 6 units):

| Quantity | Before (M2a wiring) | After |
|---|---|---|
| Fresh **process**, first search, no warm-up | 5.8 s (fresh engine in-process) | 5.55 s, 3.54 s (2 processes) |
| Fresh **process**, `Engine.warmup` (before `/ready`, off the request path) | — | 3.21 s, 2.94 s |
| Fresh **process**, first search after warm-up (target ≤ 3 s) | — | **0.050 s, 0.050 s** |
| Fresh engine in the bench process: first search cold / after warm-up | 5.8 s | 5.94 s / max 0.051 s (warm-up 5.16 s) |
| Warm live-window search | p95 0.049 s | p95 0.050 s |

Interpreter start and imports are outside the measured first search. The OS page cache was warm
(same machine, files just written): a cold disk adds read time to the warm-up, not to the request.
The benchmark ran while the test suite ran for its first minutes (shared CPU).

Tests (+15): `tests/contract/test_live_log_warmup.py` 6 (warm engine's first searches open/compute
nothing more and return exactly the cold engine's hits; pending vectors reported, never filled; a
pinned slice missing on disk fails the warm-up; no generation → skipped; a single sqlite store warms
its segment and vectors; a session holds the union of its turns' slices past the gc grace until
`close_session`), `tests/unit/test_warmup.py` 5, legacy `/ready` 3 (done with the engine report,
failure names the error class without the message, a run past its bound is `timed_out` then `done`
+ `late`), v2 `/ready` 1.

Limits: a slice compacted by another process (the log host) is warmed on its first read in the
serving process (first search after a new base there remains ~0.9 s, as before); one replayed day;
the real-data run and the staging week (§10.4) are not done.

## Two-halves staging fixes (planner / coverage / serving side) — 2026-11-10

Evidence level: **synthetic / mocked only.** Fixes for findings F1, F2, F4, F6, F7 (and the
fallback label) of the private real-data staging run of 2026-11-09; that run was **not** repeated,
so nothing here measures the QA100 or owner-question outcomes on real data.

- Partitioned F0 collection (adopted support member + live log member with archive days and live
  slices), two-halves planner on, rules decision, hash embedder: for 8 support-shaped queries and
  3 caller-typed counts the decision, executed steps, fused hits (ids, order, scores, text,
  provenance), per-arm candidates and aggregates are **identical** with and without the log member;
  no log unit is opened for a knowledge step. With `scope_knowledge_steps: false` log records enter
  the hits and a `group_count` by source counts 89 log records (shown as `records_only` in
  coverage, never `not_served`).
- Coverage: a structured count is labelled the knowledge half; the log entry lists pinned members
  and live slices.
- Planner fallback (fake planner): a planned question has the same decision/hits with
  `fallback: deterministic_plan` and `abstain`; a schema violation after the repair gets the
  labelled fallback (trace, coverage, first limitation, legacy `plan.fallback`), scoped to
  tickets/docs.
- Spend (mocked transport): `ConnectError`/`ConnectTimeout`/`ProxyError` leave the
  `serving.answers` day ledger and provider total at 0 USD / 0 calls; a `ReadTimeout` or HTTP 502
  is still booked at its estimate.
- Links/identity: a ticket whose customer and time live in `organization`/`created_date` links
  only when the member declares them (`field_map`, `time_roles`); `identity describe` reports
  `served: true` only with a valid pinned registry and a consumer.
- Suite: 4,798 passed, 45 skipped (`-m "not external"`; baseline at `58d930d`: 4,764 passed,
  46 skipped); ruff clean.

Limits: the real-data staging (QA100 replay, owner questions) must be re-run to show the effect on
the 60/100 questions with log records in the knowledge hits; B1 (log intents over a partitioned
log member), B2, B3 and F3 belong to the data/retrieval lane and are not addressed here.

## 2026-11-10 — Staging fixes, data/retrieval side: logs over the log member, one SQL surface, B2/B3/F3/F5 (synthetic)

Evidence level: **synthetic only** (F0 fixtures, the two-halves synthetic episode summaries, the F0
identity registry, hash embedder, fake roles); no service, no model call, no real data.

- The three private staging repros (logs block on a partitioned collection, `values` of a field the
  adopted schema lacks, a text condition over two strata) no longer reproduce on this build.
- Log intents over a partitioned log member (archive day members + live slice) equal the episode-
  archive deployment on the same synthetic summaries: `issue_history` (5 argument shapes: rows,
  tiers, coverage, listed pieces), `log_liveness` (3 shapes: rows and rendering identical; halted /
  running crawls read), `incident_episodes` and `log_top` on closed days; the two-halves planner gets
  an operations half (`log_liveness`) over the member.
- Support SQL over the adopted projection: 9 QA100-shaped statements (counts, groups, filters, a
  self-join, two refusals) return identical columns, rows, values, cited ids and exactness with and
  without the log member (only the request generation id in citations differs); the support catalog
  table is identical; the projection's grounding values equal the support-only ones. Cross-source
  SQL: `issue_day`/`coverage_day` hold exactly the pin's served incident rows; a ticket→registry→log
  tenant join returns only matched-alias pairs; an undeclared join is `join_not_declared`; a
  missing pinned file is a labelled `projection_unavailable`.
- Full suite `-m 'not external'`: 4,853 passed, 45 skipped (55 new tests); ruff clean.

Limits: not run on the real staging data (the coordinator's $0 rerun is next); per-request history
tables are built in memory from the window's records (bounded at 200,000 records, refused above; not
measured at production scale); the per-pin SQL file is rebuilt on every new slice pin (cost not
measured on real data); liveness reads "heartbeat recorded" days from `success_documents` on
coverage records instead of per-day recipe identities.

## 2026-11-10 — Staging round 2 fixes, planner side: support-only questions keep today's path (synthetic)

Evidence level: **synthetic / mocked only** (the adopted F0 support store with and without the live
log member built from the two-halves synthetic episode summaries; fake planner, typed router and SQL
writer replies on `JsonModel.call`). No service, no model call, no real data; the private QA100
replay and owner questions were **not** re-run here.

- N1: a planned support aggregate naming no `kind` (`status = solved`, a bare count, `priority =
  high`) gets the same decision (policy, plan, unsupported, failure, note), executed steps and
  aggregate values on the unified collection with `logs.intents` as on the support store alone;
  a planned scope over `source: logs` without a kind is still refused (`intent_required`). Before the
  fix (base `90d0440`) the same contract tests fail with `intent_required` and the labelled fallback.
- Log values no longer reach the knowledge half: no log tenant / signature value in the planner
  payload, no `qualifier_not_applied` from a log category ("search") on a support question.
- N2: "How many are new?" and "Did <tenant> portal mentions increase per day?" plan no log intent
  (base: `log_new` / `log_trend`); "Has the crawl stopped?" still gets `log_liveness`.
- N5: with the two-halves planner on, a planner schema violation on a routed count question gives
  today's `typed_route_only` decision (identical trace decision, one `typed_query` step, same value).
- N6: the two-halves planner never sends the logs planner context; `plan_check` no longer counts it
  (synthetic test). On the private staging configuration (read-only, no store opened) `W` of the
  pipeline with two review hops moves from US$0.4174 to US$0.4111 and the default pipeline from
  US$0.2265 to US$0.2202: **the 0.25 cap is still exceeded by the review pipeline** (2 review hops ≈
  US$0.19 of `W`); the planner context was not the cause.
- Suite `-m 'not external'`: 4,896 passed, 45 skipped (before: 4,853 passed, 45 skipped; 43 new);
  ruff clean; schema regenerated.

Limits: the real-data QA100 replay (45/45 recorded plans identical) is not shown here; the planner
payload on the unified collection still lists the common schema's log fields (recorded-plan replay
unaffected; a live planner may plan differently); the operations gate is English-scoped.

## 2026-11-10 — Staging round 2 latency fixes: member history per pin, per-unit values (synthetic + private real-volume re-measure, $0)

Evidence level: **synthetic** for the repository tests (F0 fixtures and generated episode records,
hash embedder, rules decision, no provider call); **real-volume re-measure at $0** on a private copy
of the round-2 staging state (≈ 228k `issue_day` pieces, 14k coverage records over 10 days; the
private workspace, not in the repository). No model call left the process (network blocked;
planner replies replayed from recorded output or refused), no live service, nothing written outside
the copy. Search-only: no generator or judge ran, so these are retrieval latencies, not answer
latencies.

| Operation (one process, warm unless stated) | before (`90d0440`) | after |
|---|---:|---:|
| `issue_history`, 3 days, all tenants | 46.5 s | 0.009 s (first request on a cold pin with no unit caches: 13.2 s, once) |
| `issue_history`, 3 days, one tenant | 46.4 s | 0.005 s |
| `issue_history`, 10 days (all / one service) | refused after 97.9 s | answered in 0.02 s / 0.008 s (227,064 episodes) |
| first `issue_history` after a new live slice (unit caches warm) | — | 2.2 s (1 of 10 days recomputed, 9 copied) |
| support-only QA100 search (8 questions × 2 passes) | 12.0 s median (847 log-unit value scans each) | 0.6 s median (0 scans; the first search of a cold process: 8.2 s) |
| owner question search (7) | 43–58 s (Q4 6.3 s) | 0.29–0.54 s |
| restarted process (history file and values file on disk) | — | `issue_history` 3 days 0.018 s; first support search 4.7 s with 0 log-unit value scans, then 0.4 s median |

Equivalence on the real copy: the 3-day all-tenant and one-tenant renderings are identical (37,530
episodes / 9,540,562 messages; 265 / 20,628); the 7 owner-question aggregates are identical except
the provisional `source` label (was the in-memory database name, now `provisional`); the 16 QA
searches return identical hits, and aggregates identical except the SQL catalog fingerprint (the
per-pin file name changed with the cache format). A synthetic slice (one record re-sealed with
occurrences + 1) changed the 3-day total by exactly 1 message.

Repository tests (`tests/contract/test_staging_latency.py`, 17): the per-pin history file equals
the previous per-request construction (kept in the test as the oracle) for 4 windows × 5 specs
(groups, listed pieces, horizons, liveness pieces); a second request on the same pin reads no
catalog record and builds nothing; over a generated 10-day member (7 archive days, 3 live, 400
pieces/day) a 10-day window is answered with exact per-rule totals and the cross-midnight chains
across the archive/live boundary; a new slice recomputes one day, reuses nine, and derives only its
own unit cache; per-unit `values`/`exists` caching (limit rules, ACL keys, a new slice is the only
unit scanned, a restarted engine reads unscoped lists from the values file, an unreadable values
file falls back to the scan, retired units are dropped); the identity view is parsed once per file
state and re-checked when the file changes. Assertions are on scan/build counters, not wall time.

Limits: the per-pin view file is still rebuilt from all unit caches on each new pin (≈ 1 s of the
2.2 s above at this volume); the plan's "first request after a new slice ≤ +1 s" target is not met
for history requests. The first request of a process with no values file still scans every unit
(8.2 s here, once); a restarted process's first search still costs ≈ 4.7 s outside the value
scans (not profiled here). Remaining support-search time (≈ 0.5 s vs the support-only engine's ≈ 0.1 s) is
mostly qualifier regex compilation over the larger value lists and the typed route's SQL catalog
setup; not addressed here.

## 2026-11-10 — Staging round 5 fixes: support-only model inputs (N12, N13) and the rendered operations half (N14) (synthetic)

Evidence level: **synthetic / mocked only** in the repository (the adopted F0 support store with
its own sealed schema and typed projection, with and without the live log member; the two-halves
synthetic engine; fake planner, router, SQL writer and generator replies on `JsonModel.call`). No
service, no model call, no real data. The private round-5 paid outputs were read (not copied) to
find the causes and to calibrate the operations post-checks; the owner set and the 55 no-plan QA
questions were **not** re-run or re-graded here.

- **N13 / N12 cause (support-only inputs differed).** On the synthetic pair, before the fix (base
  `faf2c66`) a support question's planner payload on the unified collection carried the log
  member's fields (17,807 vs 15,544 characters), the router payload differed (log-intent arm text),
  and the SQL writer saw the log and identity views (10,399 vs 7,354 characters; `issue_day`
  present). The private round-5 records show the same shape at full size: +7.4k planner input
  tokens and +1.5k SQL-writer input tokens on every one of the 55 questions. After the fix the
  planner, router, SQL writer and generator payloads are identical (generator: lineage aside) for
  three support questions, one of which passes the operations gate (a failure word) but gets no
  operations step; the answers, contract outcome and violations are identical.
- **N12 contract sensitivity (annotate).** A count taken from a cited ranked SQL result (bounded,
  17) is kept with its limitation note whether the `[aggregate:1]` marker sits in its sentence or
  in another paragraph; before, the second form removed the count as uncited (the base fails that
  test). A number the cited aggregate does not hold is still removed.
- **N14.** With a generator that misstates the operations half, the answer starts with
  `Current logs:` (every operations aggregate's rows and rendering verbatim, cited and claimed, plus
  the operations notes and refusals), the generated part follows under `Past tickets/docs:`, and
  the current-outage opener, a "Recent Incidents" heading, "currently experiencing", an unbacked
  automatic recovery and a restatement citing an operations aggregate are removed and recorded
  (`operations_claim_unbacked`, `records_presented_as_current`, `operations_restated`; annotate
  states each as a limitation). The generator receives no operations aggregate as evidence, only
  `operations_shown` without ids and the operations rule. The section stands when the generator
  abstains; a customer without a matched tenant is stated in it; a support question gets no
  section. A halt that a liveness row states is not removed.
- Calibration on the private round-5 owner answers (read-only, offline): the post-checks flag the
  "confirms … has halted" sentence, the "Yes … service disruptions" opener, the "Specific Recent
  Incidents" heading and every restatement citing an operations aggregate; they keep record
  narratives ("users have reported that reports are getting stuck"), the asker's own report and
  hypothetical causes. This is a sentence-classifier check, not a re-grade.
- Suite `-m 'not external'` (m4g venv): 4,965 passed, 45 skipped (before at `faf2c66`: 4,916
  passed, 46 skipped; 48 new tests, one environment-dependent skip now runs); ruff clean; Config
  unchanged (schema regenerated to a temporary folder: identical).

Limits: the effect on answer quality is unmeasured until the owner set is re-graded (generator +
judge) and the 55 no-plan questions are re-run on both sides; the post-checks are English-scoped
patterns (they can miss a paraphrased cause such as "appears part of broader problems", and can
remove a legitimate sentence that frames earlier records with "recent incidents"); a question the
operations detector wrongly gives an operations half ("which account has the most failed
tickets?") still gets the cross-source SQL catalog and an operations section; streamed token
deltas show the generator's text before the operations section is composed (the final `done`
answer is the composed one).

## 2026-11-10 — Staging round 6 polish: operations reading line, generator schema repair (N15) (synthetic)

Evidence level: **synthetic / mocked only** (the two-halves synthetic engine of
`tests/two_halves_support.py`; fake generator replies on `JsonModel.call` that meter each call
through the real `RunBudgetMeter` path). No service, no model call, no real data. The private
round-6 report was read (not copied) for the two findings; nothing was re-run or re-graded.

- **Reading line.** Every two-halves answer with an executed operations aggregate now opens its
  `Current logs:` section with one line, e.g. on the synthetic engine
  `As of 2026-11-09T10:30:00+00:00; window 2026-11-07 to 2026-11-09; question clock
  Australia/Adelaide, log times in UTC; services read: mcp-gateway; tenant: brightmoor.` A liveness
  plus history question lists both windows; a closed-days-only read says so; an unfiltered read says
  `all (no service filter)`; a refused-only operations half gets no line. The generator's
  `operations_shown` carries the same line. Domain-neutral wording (test).
- **N15.** With `pipeline.generation.schema_repair_attempts: 1`, a first reply raising
  `SchemaViolation` is followed by one repair call (same payload plus `schema_repair`, prompt plus
  `GENERATOR_REPAIR_PROMPT`); a valid repair restores the `Past tickets/docs:` half; its
  `model_call` record says `attempt: 2`, `retry_reason: schema_violation`. A second violation, a
  provider 503 on the repair, or a call cap of 1 (the repair cannot be reserved) each keep today's
  "No answer was generated (... schema_violation)" text with the operations section standing.
  Default `0`: one call, unchanged. `budgets.plan_check` adds one generator slot.
- Suite `-m 'not external'` (m4g venv): 4,984 passed, 45 skipped (before at `0061c34`: 4,966
  passed, 45 skipped; 18 new tests); ruff clean; `docs/config.schema.json` regenerated (one field).

Limits: whether the reading line changes the judge's O-b/O-c verdicts and how often a repair
succeeds on the real generator are unmeasured until a paid owner-set re-run; the repair is opt-in,
so the staging config must set `schema_repair_attempts: 1` for N15 to take effect.

## 2026-11-10 — New dataset as a unified write-path member; fuzzy identity auto-match option (synthetic + private side-by-side staging, paid smoke US$0.76)

Code `60a06d8` (merge of `22bb3df`). Synthetic: `-m 'not external'` 4,998 passed, 45 skipped
(before 4,987 / 45); ruff clean. New contract tests cover online vectors beside a precomputed
import with `on_missing: error` (refused without `vectors: online`), the member store sealed with
its own fields, extra metadata refused, declared `embed_fields` only, recipe recording (log
members unchanged), knowledge-half scoping with the new source, structured counts over the new
kinds, the one-projection rule, and the fuzzy identity view (threshold, excluded/contested/
unmatched never joined, YAML export equal, registry file untouched).

Private staging (external workspace; the owner's private outage dataset, 298 records, never in the
repository): built as a write-path snapshot member in a **copy** of the owner test store
(composing in the served store would have made the running server refuse every request, since the
collection recipe changes with a new member); generation composed by reference (11 members,
562,270 documents); served side by side on a loopback port with `knowledge_sources` + the new
source and `auto_match: fuzzy` (0.8): the pinned registry view joins 113 aliases instead of 73
(40 coordinator-decided candidates promoted, 0 excluded/contested overridden).

- No-model retrieval (4 queries): the new records rank first for the three dataset-specific
  queries; no log record entered the knowledge-scoped hits.
- Paid smoke (8 questions, Sonnet planner + Haiku generator, ≈ US$0.63 on the side-by-side ledger
  + ≈ US$0.12 on the existing server for 2 comparison questions): 5 answers cite the new records
  correctly (cause-pattern question, customer outage list with RCA status, single-customer outage
  summary, root cause of a dated outage, 30-day outage list); "which outages had no RCA" is
  **wrong**: the typed SQL route counted support tickets (the new kinds have no typed projection;
  D6 deferred); a customer-named root-cause question returned no new records (insufficient
  evidence; cause not verified — likely a customer filter on knowledge steps). One owner question
  failed with HTTP 502 (generator stopped) on **both** servers (pre-existing).
- Fuzzy identity: no answer difference observed in the compared question (the tenant named in the
  question already grounded the operations half on the existing server).

Limits: smoke, not an evaluation (no gold, no judge); the frozen ≥ 10-question member set and QA
non-regression on the owner set (§8.2 D10) are not run.
