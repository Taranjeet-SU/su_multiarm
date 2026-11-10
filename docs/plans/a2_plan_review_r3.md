# Review of addendum A2 rev 3, round 3

Reviewed: `docs/plans/v2_addendum_log_analytics_ui.md` rev 3 (untracked) and the A1 changelog row "3a" (uncommitted
edit of `docs/plans/v2_addendum_logs_and_patterns.md`). Both were checked against round 2
(`docs/plans/a2_plan_review_r2.md`) and against the code on `v2/logs` at **`fdb7c4a`**. That commit merged the
bug-fix branch (safe redaction, the raw-copy tier, retention scopes with derived per-day snapshots, serving pins,
OpenSearch naming, deterministic Louvain) and the web UI MVP. Deployment specifics are in a private companion review.
Line references such as `A2:1402` point into rev 3.

**VERDICT: REVISE**

All six round-2 blocking findings are resolved in the design, and so are n1–n9. The history tier, raw tier, change
classes, scope guard, line cache and capacity chain are specified well, and the raw tier and the Host guard have
already landed with tests.

Two new blocking findings remain, both narrow:

- **R3-B1:** the plan's interim rule for derived retention contradicts the code that has already landed. The landed
  per-day derived snapshots are the right interim archive, but the plan must say so and bound the interim serving
  path.
- **R3-B2:** the provider-chain configuration lets a paid tier run with no cap at all, which is an unsafe default.

Both can be fixed in the plan text and §11 without restructuring.

## Status of round-2 blocking findings

| Id | Status | Evidence |
|---|---|---|
| R2-B1 spend versus caps | **Resolved** (superseded by the owner's capacity decision) | §4.2.6 (A2:743–838). The provider chain runs local, then LAN, then paid overflow. The bootstrap is capacity-bound, the paid total is USD 2 per key (about 307 cluster calls at USD 0.0065, which checks out), and the `paid_exhausted`/`capacity_limited`/`paused` states are defined with no automatic raise. New issue with the defaults: R3-B2. |
| R2-B2 overlay rebuild | **Resolved** | §4.2.3 (A2:678–687): two caches, including the line-reply cache, and window-wide ranking of pending heads. The C2 acceptance covers the D−5 case. |
| R2-B3 delta classes | **Resolved** | §4.1.6 (A2:507–549): change classes A–H, each with its delta, and a property test that delta reparse equals full reparse over generated diffs. |
| R2-B4 scope guard | **Resolved** | §4.1.5 (A2:464–477): `log_scope_guard` at the records choke point. It covers kind-less, negated and multi-kind scopes at every entry point listed. |
| R2-B5 history tier | **Resolved in design; conflicts with the landed interim** | §4.8 (A2:1062–1163) covers retention scopes, day shards, shard-set generations, frozen days, lineage, sizing and acceptance. §11.0 bug-fix delta 2 (A2:1400–1403) says to refuse `derived ≠ window` until H1; the code at `fdb7c4a` accepts it. See R3-B1. |
| R2-B6 raw tier | **Resolved, and landed** | §4.9. At `fdb7c4a`, `ingestion/raw_store.py` limits reads to the `reparse` and `audit` purposes with 0700/0600 modes, `raw_days < keep_days` is enforced (`core/config_logs.py:239`), and `tests/contract/test_retention_scopes_and_raw.py:189` asserts that no other module references the raw store. See n-r3-1. |

## Status of round-2 non-blocking findings

| Id | Status | Note |
|---|---|---|
| n1 | Resolved | |
| n2 | Resolved | |
| n3 | Resolved | |
| n4 | Resolved | |
| n5 | Resolved | |
| n6 | Resolved with evidence | The 1.10.0 floor is kept, and a CI job at `mcp==1.10.0` will prove it. I did not re-verify the 1.10.0 contents; the job is the check. |
| n7 | Resolved | §5.3 sets the merge order, file owners and format owners. |
| n8 | Resolved | |
| n9 | Resolved | |

## Blocking findings (round 3)

| Id | Plan section | Finding | Evidence | Required change |
|---|---|---|---|---|
| R3-B1 | §11.0 bug-fix item 2 (A2:1400–1403); §4.8; A1 row 3a | **The interim derived-retention rule contradicts the landed code, and the landed interim has three gaps.** The plan says "until H1 lands, refuse `derived ≠ window`". At `fdb7c4a`, `derived: forever \| N` is accepted. Compose writes a masked **per-day derived snapshot** (`<partitions>/derived/<source>/<day>.json.gz`, holding aggregates, hourly cells and exemplars, keyed by `(version, library identity)`), and retention expires partitions while keeping snapshots per the derived scope. Then, on every compose, it **re-composes every retained snapshot day** into a new generation, mixing library identities. The snapshots make a good interim archive and a natural H1 import source. As landed, though, three things go wrong. (1) Pre-H1 compose cost grows with history (a full rebuild of all days nightly). At full width that passes the measured compose budget (10⁶ rows ≈ 19 min and 10.3 GiB, per N10) within roughly 1–2 weeks of `forever`. (2) `window_documents` runs over all composed days, so `signature_window`, `signature_tenant_window` and `first_seen` silently change meaning from "masked window" to "all retained history", and no rendering states that the result spans older library identities. (3) The snapshot payload and key exclude the fallback overlay identity, the partition format, and the format-2/3 inputs (typed-value aggregates for `endpoint_day`/`exception_day`, and accounting for `coverage_day`). Once a partition expires, its model-assisted results and the new kinds are lost from history before H1 can import them. | `ingestion/logs_compose.py:46–68` (`write_derived`; idempotent on version and library identity only), `:229–256` (all retained snapshot days composed, including `window_documents` over all days), `:87–90` (downsampling hook unimplemented); `ingestion/retention.py:120–167, 316–321` (derived cut-off and retained days); `core/config_logs.py:150–197` | Replace the §11.0 refusal with an explicit **interim archive contract**: (a) The snapshot is the derived archive until H1, and H1 imports day shards from snapshots for days whose partitions are gone; drop the "refuse" instruction and fix A1 row 3a, which currently says "stored as append-only day shards". (b) Give the snapshot a format and an identity that include the partition format, the overlay or fallback identity and the compose format. Make it contain every input H1 needs per day (the typed-value aggregates of format 2, the accounting of format 3, and the overlay's model-assisted rows), and rewrite it when the overlay changes. A day archived before those inputs existed gets `source_unavailable` for the new kinds in history, never zero. (c) Add a pre-H1 serving bound, for example `logs.history.serve_days` (default `keep_days`): compose serves at most that many days and the rest stay archived but unserved, or compose refuses with a typed error above a row budget. (d) Compute window rollups over the masked window, or over a declared question window. Pre-H1, add a rendering note when served days span more than one library identity. Add tests: snapshot round trip including overlay rows; the serve bound; window rollups unchanged by archived days. |
| R3-B2 | §4.2.6 (A2:748–769), §4.6 | **A paid tier can run with no cap.** The public config shape allows `models.roles.<role>.chain[]` to include `tier: paid`, while `budgets.providers.<p>.max_usd_total`, `learning.*.budget.max_usd_per_day` and `learning.bootstrap.max_usd_total` all default to `null`. `learning.bootstrap.max_calls_per_day` defaults to 3000 with `enabled: true`. A user who adds a paid tier and no cap gets unbounded paid spend: up to about USD 19.5/day of bootstrap at the illustrative price, then the steady-state calls. "Public default is no paid tier" only holds as long as nobody adds one. | A2:761–766 | Fail closed in config validation: a chain containing `tier: paid` requires a finite `budgets.providers.<p>.max_usd_total` **and** a finite daily cap on every learning scope that can reach it (`learning.bootstrap`, `learning.patterns`, `learning.extraction`). Otherwise raise a typed `ConfigError`. Default `learning.bootstrap.enabled` to `false`, or apply it only to self-hosted tiers unless a paid cap is set. Add a C4 test: a paid tier without caps is refused at load. |

## Non-blocking findings (round 3)

| Id | Section | Finding | Suggested change |
|---|---|---|---|
| n-r3-1 | §4.9, §4.2.8 | `ingestion/schedule.py` imports the raw store (the puller writes there). C2 adds the `fallback` step, which reaches the egress path, to the same module, so the module-level import test no longer separates raw reads from egress. | Move the raw-tier write and `--from-raw` calls into a dedicated module (for example `ingestion/raw_reparse.py`), keep `schedule.py` free of `raw_store`, and extend the import test to `schedule.py` and `learning/`. |
| n-r3-2 | §2.1, §1 "Evidence status", §8 | These sections are stale at `fdb7c4a`. "Nothing in this addendum is implemented" is false: the raw tier, retention scopes, pins, Louvain, Host guard, UI MVP and shell-quoted copy commands have landed. §2.1 is still "read at `106d4b0`". In the risks table, "Total provider caps exhausted mid-window … `budget_deferred`" predates the capacity chain. | Re-base §2.1 on `fdb7c4a`, with the evidence label of each landed item, and update the stale risk rows. |
| n-r3-3 | §4.8.5 | The H1 acceptance has no read-latency criterion, yet every read path gains a shard-set filter: an `IN` over up to about 180 shard ids on a SQLite graph of 10⁶–10⁷ edges, and a terms filter on monthly OpenSearch indices. | Add an acceptance: log-tool p95 ≤ 200 ms and graph neighbourhood p95 ≤ 500 ms at 180 days of synthetic history. |
| n-r3-4 | §4.4.2 | The Host guard is wired only when `serving.ui.enabled` is set (`serving/http.py:312`). The plan also requires it with `mcp.mount_on_http` and for LAN-forwarded API-only use. | Activate the guard whenever `allowed_hosts` is non-empty or the MCP mount is on (M1), and test it. |
| n-r3-5 | §4.2.6 | The capacity timeline rests on an unmeasured throughput `T` for self-hosted tiers (about 3,700 tokens per call on mid-size models). | Make "measure `T` per tier" an explicit E1b or R0 deliverable before the bootstrap is enabled. |
| n-r3-6 | §4.8.3 | Lineage maps supersede, fix and rename, but frozen snapshot days (R3-B1) hold signature ids from older library identities with no lineage rows yet. | When H1 imports snapshots, build lineage rows from the decisions replay up to each day's identity. |

## Requirements coverage (round 3)

| Requirement | Status | Section |
|---|---|---|
| UI like the earlier consoles; represents the arms | covered (MVP landed) | §4.4 |
| All five arms fed from regex output with provenance | covered | §4.1 |
| MCP | covered | §4.5 |
| 100 % accounted (O-A2-0) | covered | §4.2.1 |
| LLM fallback where regex fails; grounded | covered | §4.2.3–4.2.5 |
| Same numbers on every path | covered | §4.1.5 |
| Self-hosted first, paid overflow USD 2 per key, reschedule (owner) | covered; default unsafe (R3-B2) | §4.2.6 |
| Raw 3 days / masked 10 days / derived never (owner) | covered in design; interim contract conflicts with code (R3-B1) | §4.8, §4.9, §11.0 |
| LAN forwarder serving, plain HTTP with bearer (owner; TLS later) | covered (`allowed_hosts`, Origin, `doctor` warning) | §4.4.2 |
| Public-repo hygiene | covered (scan clean; provider names appear only as config keys) | whole document |

## Claims verified against code at `fdb7c4a`

| Claim | Result |
|---|---|
| Retention scopes `keep_days` (alias `partition_days`), `raw_days < keep_days`, `derived: window \| forever \| N` | true (`core/config_logs.py:150–197, 239`) |
| `derived ≠ window` is refused until H1 (§11.0) | **false**: it is accepted and served from per-day snapshots (R3-B1) |
| Derived snapshots are kept past partition expiry and listed by retention | true (`ingestion/retention.py:120–167, 285, 316–321`) |
| The snapshot includes overlay and format-2/3 inputs | **false**: aggregates, hourly cells and exemplars only; key `(version, library identity)` (R3-B1) |
| Raw tier: purpose-gated reader, modes, import test | true (`ingestion/raw_store.py`; `tests/contract/test_retention_scopes_and_raw.py:189`) |
| Host allow-list (421), Origin check, nosniff; UI requires bearer unless opted out | true (`serving/hosts.py`; `serving/ui.py:83`; `serving/http.py:312`); the guard is only active with the UI (n-r3-4) |
| `serving.allowed_hosts` config key | true (`core/config.py:1186`) |
| Downsampling hook exists, unimplemented | true (`ingestion/logs_compose.py:87–90`) |
| Paid USD 2 ≈ 300 cluster calls at USD 0.0065 | true (≈ 307) |
| No private names, hosts or IPs in rev 3 | true |
