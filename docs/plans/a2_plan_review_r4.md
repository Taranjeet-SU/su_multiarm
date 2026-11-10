# Review of addendum A2 rev 4, round 4

Reviewed: `docs/plans/v2_addendum_log_analytics_ui.md` rev 4 (its "Review response (round 3)", §4.8.6, §4.2.6 and
§11.R4) and the A1 changelog row "3a". I checked both against round 3 (`docs/plans/a2_plan_review_r3.md`) and the
code at `v2/logs` `fdb7c4a`.

**VERDICT: APPROVE**

Both round-3 blocking findings are resolved, and so are all six round-3 non-blocking items. I found no new
blocking contradiction with `fdb7c4a`. The differences from the code are planned work, and each one is assigned
in §11.R4.

## Round-3 blocking findings

| Id | Status | Evidence |
|---|---|---|
| R3-B1 interim derived archive | **Resolved** | §4.8.6 adopts the landed snapshots (`logs_compose.write_derived`) as the archive before H1 and as the source H1 imports from. The other gaps are closed as follows. **Identity:** `derived_format: 2`, and the identity is widened to cover partition format, fallback identity and compose format, so the snapshot is rewritten when any of them changes. **Payload:** it adds typed-value aggregates, accounting and overlay rows. **Missing inputs:** an older snapshot reports `source_unavailable` for them. **Serving bound:** `logs.history.serve_days` (default `keep_days`), plus `max_serve_rows` for wider bounds. **Window rollups:** computed over the masked window. **Labels:** an identity-span label. The "refuse `derived ≠ window`" instruction is withdrawn (§11.R4 bug-fix item 3, and §11.0 is marked superseded). A1 row 3a now describes snapshots before H1 and shards after. The tests are named. |
| R3-B2 uncapped paid tier | **Resolved** | §4.2.6 makes config load fail closed (typed `ConfigError`) for any `tier: paid` without a finite provider total and finite daily caps on every learning scope it can reach, and on `serving.answers` for serving chains. Bootstrap defaults to `false`. `require_measured_throughput` defaults to `true`. C4 has tests for both. |

Round-3 non-blocking items n-r3-1 to n-r3-6 are all resolved, as listed in rev 4's response table and §11.R4:

- n-r3-1: `raw_reparse.py` is the only path to the raw store, and the import test is extended.
- n-r3-2: §2.1 is re-based on `fdb7c4a`.
- n-r3-3: read-latency acceptance is added.
- n-r3-4: the Host guard turns on with the MCP mount or a non-empty `allowed_hosts`, not only with the UI.
- n-r3-5: bootstrap waits for a measured throughput `T`.
- n-r3-6: H1 builds lineage rows when it imports snapshots.

## Non-blocking (round 4)

| Id | Section | Note |
|---|---|---|
| n-r4-1 | §4.8.2, §9 | "Config refuses any backend without history support when `derived ≠ window`" (PostgreSQL, Kuzu, Neo4j `planned`) should apply only once H1 serves shards. The snapshot mode before H1 composes ordinary generations and runs on any backend, as `fdb7c4a` already does. Scope the refusal to H1 mode so the bug-fix slice does not add it early. |
| n-r4-2 | §11.R4, §5.3 | The merge order is bug-fix → L5 → fallback, but the bug-fix slice owns the snapshot payload. The format-2 typed-value aggregates and the format-3 accounting and overlay rows only exist after L5 and fallback merge. Say that L5 and fallback each extend the snapshot payload, with a round-trip test, in their own merges. The fallback slice already has this as item 5; L5 needs the same item for the typed-value aggregates. |

## Claims verified at `fdb7c4a`

- The snapshot path, the current key `(version, library identity)`, and the all-days compose are as rev 4 describes
  (`ingestion/logs_compose.py:46–68, 229–256`). Rev 4 changes them only through the §11.R4 deltas.
- `logs.history.serve_days` and `max_serve_rows` do not exist yet. They are new keys, refused until their consumer
  lands (§4.6).
- The Host guard is active only with the UI today (`serving/http.py:312`), which §11.R4 bug-fix item 5 extends.
- There are no private names, hosts or IPs in rev 4 or in A1 row 3a.
