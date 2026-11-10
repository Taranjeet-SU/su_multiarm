# Review of addendum A2 rev 2, round 2

Reviewed: `docs/plans/v2_addendum_log_analytics_ui.md` rev 2 (untracked, worktree on `v2/logs` at `106d4b0`). It is
checked against round 1 (`docs/plans/a2_plan_review_r1.md`), the code at `106d4b0`, and a read-only look at the four
implementation worktrees that are building from the rev 2 deltas (L5, fallback, UI, bug-fix). It also covers two owner
decisions made after rev 2: the retention scopes, and a 3-day raw tier. Deployment specifics are in a private
companion review. Line references such as `A2:466` point into rev 2.

**VERDICT: REVISE**

Rev 2 is a large and honest improvement. Eight of the ten round-1 blocking findings are fully resolved, and the
implementation worktrees confirm three of the fixes are buildable: the deterministic Louvain constructor, serving
pins, and the Host guard. Two round-1 findings are only partly resolved:

- **B2 (overlay):** a rebuilt overlay cannot restore line-tier replies.
- **B3 (delta reparse):** the delta set misses several kinds of library change.

One round-1 non-blocking finding, N5 (bootstrap and backlog), is not resolved. The new budget numbers contradict the
daily caps.

Two of the new blocking findings come from owner decisions made after rev 2 that conflict with A1 and A2 as written:

- **Retention:** derived arm data that never expires conflicts with A1's "delete everywhere on day 11" and with
  compose as a whole-window generation.
- **Raw tier:** an unredacted raw tier conflicts with A1's "nothing unredacted is ever persisted" contract.

I did not miss these in round 1: they are new inputs. The remaining new findings are on sections that rev 2 changed.

## Status of round-1 blocking findings

| Id | Status | Evidence |
|---|---|---|
| B1 accounting identity | **Resolved** | §4.2.1 (A2:466–496) has four identities, S/Q/D/H, on source documents, with `docs_pulled` counted by the puller and `fragment` counted at document level. Three alignment points remain (n1–n3). |
| B2 fallback placement | **Partly resolved** | Seal → overlay → apply decisions → compose is specified (§4.2.3, §4.2.8), and `run_daily` integration is in C2. Rebuilding the overlay from the template cache cannot restore line-tier replies, and pending heads on older days are never refilled. See R2-B2. |
| B3 daily reparse cost | **Partly resolved** | Delta reparse, content-addressed day documents, a cost model and the day-11 acceptance are all added (§4.1.6). The delta set is incomplete for format, continuation, annotate, priority and scoping changes. See R2-B3. |
| B4 tenancy | **Resolved** | Single-tenant serving under `profile: logs`, with `tenant` as a dimension and a refusal at start-up (§4.3). See n4 for where the refusal must be enforced. |
| B5 shared plan path | **Resolved for the decision path** | `StructuredStep.intent`, compile-only through `log_intents`, `rendering` in aggregates, and `llm`/`cascade` acceptance (§4.1.5). The refusal rule misses free scopes without a kind predicate and the direct `aggregate` entry points. See R2-B4. |
| B6 typed values and format gate | **Resolved** | `matches.fields` and `gaps`, plus `PartitionManifest.format` 1/2/3, with a compose rule and a mixed-format test (§4.1.2). The L5 worktree already adds `format: int = 1`. See n1 for the upgrade semantics of format-1 units. |
| B7 model-assisted graph and arm mapping | **Resolved** | Unreviewed relations are emitted as `co_occurrence`/`unchecked`, and `model_line` maps to `unknown` buckets with no edges (§4.1.3, §4.2.5). |
| B8 serving pins | **Resolved** | The P0-PIN slice and its acceptance are added. The bug-fix worktree wires `ProcessPins.for_config` into `Engine` and the request paths. |
| B9 `_` dataset and Louvain | **Resolved** | Both are in P0. The bug-fix worktree adds `communities.base.louvain_partitions` (integer node ids in sorted label order, sorted edges). L5b must reuse it (§11.1 item 6). |
| B10 DNS rebinding | **Resolved** | §4.4.2 adds a Host allow-list (421), an Origin check (403) and requires bearer auth whenever the UI is enabled. The UI worktree's `serving/hosts.py` implements this, ignoring the port when it matches Host. |

## Status of round-1 non-blocking findings

| Id | Status | Note |
|---|---|---|
| N1 | Resolved | O-A2-0 records the owner's decision, and the convergence target is a proposal. |
| N2 | Resolved | Adds `pattern_owner` and the `deterministic_reviewed` metric. |
| N3 | Resolved | Labels use `vN` and `maskN`. |
| N4 | Resolved | The cache key and gate re-run are specified, but they cover template replies only (R2-B2). |
| N5 | **Not resolved** | The bootstrap and runway numbers contradict the caps (R2-B1). |
| N6 | Resolved | |
| N7 | Resolved | Adds `endpoint_window` and cardinality-aware rollup choice. |
| N8 | Resolved | |
| N9 | Resolved | |
| N10 | Resolved | MVP in weeks 6–7 is plausible. B2+B3 and B10 still have no owner (O-A2-12); see n7. |
| N11 | Resolved | |
| N12 | Resolved | |
| N13 | Resolved | |
| N14 | Resolved in design | The SDK floor needs raising (n6). |
| N15 | Resolved | |
| N16 | Resolved | |
| N17 | Resolved | |
| N18 | Resolved | |
| N19 | Resolved | |
| N20 | Resolved | |

## Blocking findings (round 2)

| Id | Plan section | Finding | Evidence | Required change |
|---|---|---|---|---|
| R2-B1 | §4.2.6 (A2:631–661), O-A2-2; N5 | **The spend model contradicts its own caps, so N5 is not resolved.** (1) The table runs bootstrap in "days 1–2" with "≤ 3×10³ cluster calls ≤ USD 20". But `learning.patterns` is capped at USD 2/day, which is about 307 calls/day at USD 0.0065, and `template.max_clusters_per_day` defaults to 150. Bootstrapping 1–3×10³ clusters therefore takes about 4–20 days. That is as long as or longer than the 10-day masked-partition life, and deferred clusters "expire with their partitions". (2) "The total cap lasts about 2 weeks" at the steady-state upper bound: USD 20 at ≤ USD 2/day is at most 10 days on one provider, and bootstrap may have used most of it already. (3) "The total-budget decision removes the bootstrap backlog" (A2:60) is false under the daily cap. (4) Nothing defines what happens after the total cap is exhausted: whether the fallback is off, whether `extracted_coverage` targets are suspended, and how answers and `coverage_day` label it. | A2:636–653; §4.6 `template.max_clusters_per_day` (rev 1 default 150) | Restate the timeline under the caps. Pick one of these: (a) a separate `learning.bootstrap` scope with its own daily and total caps; (b) bootstrap over a ranked subset that fits the cap before partitions expire; (c) re-key deferred clusters to their newest member partition so they survive expiry. Correct the runway figure. Add an "exhausted" state with a defined behaviour and rendering, and test it in C4. |
| R2-B2 | §4.2.3 item 7 (A2:569–575), §4.2.8, C2 acceptance | **Rebuilding the overlay with 0 calls is impossible for line-tier results.** The cache is keyed by template, and only labels and regex are reused. `model_line` and line-tier `parsed_filled` heads come from per-line replies, which no cache holds, so after any identity change (daily under `auto`) they fall back to `fallback_pending`. The fallback step runs only over "v's residue", today's partition, so pending heads of the other 9 in-window days are never refilled. `extracted_coverage` on older days would drop after every library change, and C2's "stale overlay rebuilt with 0 calls" acceptance cannot pass for these heads. | A2:542–576, 673–675 | Add a line-reply cache keyed by `(redacted line sha, requested field set, model id, prompt version, reply schema version)` with the partition's retention tags, so overlay rebuild is 0-call for every model source. Specify that the `fallback` step also ranks `fallback_pending` heads of every in-window partition under the caps. Extend C2's acceptance to `model_line` and line-fill heads. |
| R2-B3 | §4.1.6 delta set (A2:424–437) | **The delta set misses several kinds of library change, so rows are copied silently stale.** Only residue, retired `pattern_id`s (including a changed regex sha) and steals by **new** entries are re-matched. These changes also alter results: (a) `format` entries change level and body extraction for every head; (b) `continuation` entries and the multiline rules change heads, joined documents and fragments, and therefore identity D; (c) `annotate` entries change typed values, and §4.1.2 persists annotate hits, without changing `pattern_id`; (d) a priority, `applies_to`, kind, category, signature or noise `precedence` change on an **existing** entry without a regex change can move matches; (e) a change to the redactor or B11 rules. The ≥ 95 % reuse acceptance and the steal test do not cover any of these. | A2:424–437; §4.7 rows for noise precedence and continuation (A2:839–840) | Classify library diffs. Classes (a), (b) and (e) force a full reparse of the affected units. Class (c) re-matches every head whose entries' annotate scope changed. Class (d) puts every head matched by the changed entry into the retired set and runs the `Set` steal scan with the changed entry. Add the property test the risks table promises (delta equals full reparse) over a generated corpus of diffs covering every class, as an L5c acceptance. |
| R2-B4 | §4.1.5 refusal rule (A2:392–394) | **The "same numbers" guard is bypassable.** The rule refuses a `ScopeDraft` "whose kind predicate **names** a log kind". Under `profile: logs` the records are rollups of several kinds, so a scope with **no** kind predicate (or with a negated one) sums `unit_day`, `service_day`, `signature_day` and other rows together. That silently over-counts. The rule also covers decision steps only. `SearchRequest.aggregate`, `POST /v2/tools/aggregate` and the MCP `aggregate` tool take a caller `ScopeSpec` directly. | `decision/base.py:56` (`DecisionRequest.scope`, a caller-typed scope); `serving/mcp.py` `aggregate` tool | Under `profile: logs`, refuse every `ScopeSpec` that **can match** more than one log kind, or any log kind, without a `LogIntentCall`, at every entry point: decision steps, `SearchRequest.aggregate`/`scope`, and the HTTP and MCP `aggregate` tool. Use `intent_required` with the matched kinds named. Allow an explicit single-kind scope only if the owner wants ad-hoc analysis, and then label it "not an intent". Test each entry point. |
| R2-B5 | §4.1.6, §4.2.3 item 7, §5 L5c; A1 L3b rule (a) and verify | **The owner's retention decision (derived arm data never expires; 6-month analysis) conflicts with the inherited design.** (1) A1 rule (a) and its content-based `verify` delete each day everywhere on day 11, and A2 inherits this, including "overlays share the partition's retention". (2) Compose builds the whole window into a new generation that copies the corpus forward (`ingestion/write.py`; execution log L3a). Six months of derived documents (orders of 10⁷ rows with `signature_hour`, about 20× the 10-day sizing) cannot be rebuilt or copied nightly within ≤ 20 min and ≤ 4 GB, and `keep_generations: 2` doubles storage. (3) N11 refuses mixed library identities. Days older than the 10-day masked partitions can never be reparsed, so history necessarily spans identities, and superseded or renamed signatures break 6-month trends. (4) Window rollups (`signature_window`, `incident`) are defined over the 10-day window, and no history-scale kind or question window is defined. (5) Masked exemplar lines persisted for 6 months change the privacy posture; verify must stop asserting their absence. | `ingestion/logs_compose.py:5–15`; `ingestion/retention.py` (rule (a), `verify`); A2:338 sizing (10-day window only) | Add a **history tier** section. It needs: retention scopes as config (`retention.raw_days`, `retention.masked_days`, `retention.derived: never \| <days>`). The **public default stays finite**, and the deployment opts in. History is append-only per-day documents or indices that generations reference without copying. Frozen days carry their library identity, and a signature lineage table (supersede and fix maps) lets trends join across identities, with answers labelled "spans N library identities". History-scale window kinds or on-the-fly window queries over day rollups are defined. `verify` is scoped to raw and masked tiers. A 6-month sizing table per store with an alert threshold is added; the owner's figure is about 60 GB. The later hourly-to-daily roll-up gets a placeholder contract. Re-derive the compose and RSS acceptance for history (for example, append one day to 180 days of history within budget). Update A1 §3.8.3 by changelog. |
| R2-B6 | §3.1 principles, B11 dependency; A1 §3.2.3 | **The owner's 3-day raw (unredacted) tier conflicts with the package contract.** A1 and the code refuse to persist anything unredacted (`partitions.py` docstring; `run_daily` refuses a mapping without a redactor), and A2 does not mention a raw tier. The plans must say whether the raw tier is (a) **deployment-only tooling** that the package never reads, or (b) a package feature. If (b), it needs an opt-in store with its own retention (3 days by index date), a refusal to be read by ingest, serving, the UI, MCP or any model call, a re-redaction entry point that writes a new masked partition version, a `verify` that the tier is gone after 3 days, and a public default of off. In either case, state that re-redaction is possible only within 3 days, while reparse remains possible for 10 days from masked partitions. | `ingestion/partitions.py:26–27`; `ingestion/schedule.py:117` (`require_redaction`) | Choose (a) or (b) and write the contract. Under (b), add a `raw_tier` config block (default off), the store, the re-redaction command, and tests that no read path can open it. Under either option, add the raw tier to the retention scopes of R2-B5. |

## Non-blocking findings (round 2)

| Id | Section | Finding | Suggested change |
|---|---|---|---|
| n1 | §4.2.1–4.2.2, §4.1.2 | The fallback worktree's identity D includes a `malformed` term (documents the mapper could not map), which the plan lacks. The worktree also marks units stored before accounting as `recorded: false` ("not recorded, never balanced"). The plan's "format < 3 is reparsed to 3" would otherwise assert D on units with no `docs_pulled` and refuse. | Adopt `malformed` in D and the `recorded: false` semantics in §4.2.1/§4.2.2 and in the compose rule. |
| n2 | §4.2.1 S | With an INFO-sample stratum defined as ¬(F∨E∨W), "`rest` = the complement of every stratum query" is empty by construction. | Say what `rest` counts (for example documents outside the hourly window, the `outside` stratum, or documents without a level token), or drop it and make S `total = Σ strata` with independent counts. |
| n3 | §4.2.1 Q | "± settle growth" is not a crisp seal rule. Late writes between the counts tier and the pull can make `fetched > count` when `quota ≥ count`. | Define the tolerance (for example `count ≤ fetched ≤ count × (1 + settle.max_growth)` when uncapped), and test it with a late-write fake. Note that `random_score` plans have no hourly strata. |
| n4 | §4.3 | The access policy is injected from Python (`Engine(acl=…)`), so "config validation refuses a non-default policy" cannot see it. | Enforce the refusal in `Engine.__init__` (and `create_app`/`create_mcp`) when `profile: logs` is set. |
| n5 | §4.2.6 | The owner said "per provider key", but the config keys the cap by provider name, so a key rotation or a second key is undefined. Serving-time model spend (answer generator, `llm` planner, community reports) on the same key sits outside the total. | Key the ledger on `(provider, api_key_env)`, or say that a rotation keeps the total. State explicitly that serving spend is outside the total, or include it. |
| n6 | §4.5.2 | `TransportSecuritySettings` and `streamable_http_path` need a newer `mcp` than the declared floor `mcp>=1.10,<2`. The repository venv has 1.30. | Raise the floor to the first release that provides both, and run M1 at that floor. |
| n7 | §5.3, §8 | All four running slices edit `core/config.py`. The bug-fix and L5 slices both edit `engine.py` and `communities/base.py`; L5 and fallback both edit `logs_parse.py` and `partitions.py`; fallback and bug-fix both edit `schedule.py`. The format numbers belong to L5 (2) and C1 (3). B2+B3 and B10 still have no owner. | Fix a merge order (bug-fix → L5 → fallback → UI), name an owner for each shared file and for each format bump, and assign B2+B3 and B10 now. |
| n8 | §4.4.2 | The plan says "exact match"; the implementation matches the host and ignores the port, which is correct for tunnels on another local port. | Align the wording. |
| n9 | §4.2.6 | The default `max_clusters_per_day` (150) differs from the 300 implied by "≤ 300 cluster calls/day". | Make them one number. |

## Requirements coverage (round 2)

| Owner requirement | Status | Section |
|---|---|---|
| UI like the earlier consoles; represents the arms | covered | §4.4 |
| Dense, lexical, structured, graph and community arms fed from regex output with provenance | covered | §4.1 |
| Servable via MCP | covered (n6) | §4.5 |
| 100 % accounted (owner decision O-A2-0) | covered (n1–n3) | §4.2.1 |
| LLM where regex fails; grounded | partial (R2-B2) | §4.2.3–4.2.5 |
| Same numbers on every path | partial (R2-B4) | §4.1.5 |
| Daily library change affordable and correct | partial (R2-B3) | §4.1.6 |
| USD 20 total per provider key | partial: arithmetic and exhaustion behaviour (R2-B1), key semantics (n5) | §4.2.6 |
| Single-tenant serving, read-only, rebinding-safe | covered (n4) | §4.3, §4.4.2 |
| Derived arm data kept for 6 months; masked partitions 10 days; raw 3 days | **missing** (R2-B5, R2-B6) | — |
| Public-repo hygiene | covered (scan clean; provider names appear only as config keys) | whole document |

## Claims verified against code and worktrees

| Claim | Result |
|---|---|
| `louvain_partitions` deterministic constructor exists (bug-fix slice) | true: sorted integer ids and sorted edges, in the bug-fix worktree `communities/base.py` |
| `ProcessPins` wired into `Engine` (P0-PIN) | in progress: `ProcessPins.for_config` and per-request pins in the bug-fix worktree `engine.py` |
| Host allow-list (421) and Origin check (403) | true: the UI worktree `serving/hosts.py` (port ignored, `nosniff` on `/v2/*`) |
| `PartitionManifest.format` | in progress in the L5 worktree (`format: int = 1`) |
| `docs_pulled` counted by the puller; seal refuses on imbalance | in progress in the fallback worktree, with an extra `malformed` term (n1) |
| The embedder device is not part of the index recipe | true (`providers/embeddings.py:68`): a CPU/GPU switch does not change fingerprints |
| `Relation` accepts `co_occurrence` with `support: unchecked` | true (`core/types.py:163` forbids only `fact` + `unchecked`) |
| The MCP SDK supports `transport_security` and `streamable_http_path` | true in 1.30 (installed); not guaranteed at the declared floor 1.10 (n6) |
| Every generation copies the corpus forward | true (`ingestion/write.py`; execution log L3a): this is the basis of R2-B5 |
| Nothing unredacted is persisted | true today (`partitions.py:26–27`, `require_redaction`): this is the basis of R2-B6 |
| Budget arithmetic | USD 0.0065 per cluster call; USD 2/day ≈ 307 calls; USD 20 ≈ 3,077 calls ≈ 10 days at the USD 2 cap (R2-B1) |
| No private names, hosts or IPs in rev 2 | true |
