# Review: V2 plan addendum A1, revision 3

Reviewed: `docs/plans/v2_addendum_logs_and_patterns.md` rev 3 ("Review response (round 2)", §3.4.2,
§3.4.3, §3.7, §3.8.3–3.8.4, §3.9.2, §3.12, §4). The scope was limited to the B7 residual and
n1–n9 from `addendum_logs_review_r2.md`, checked against `src/su_multi_arm/ingestion/write.py` at
`cccd8d2`. Date: 2026-11-04.

VERDICT: APPROVE

The B7 residual is resolved and no blocking findings remain. n1–n9 are addressed. I raised no new
blocking findings on unchanged sections.

## B7 residual: resolved

| Check | Result |
|---|---|
| Current reuse key | `write.py:566-572`: `recipe = (self.extractor.recipe_fingerprint() or "none")[:16]`, then `key = f"extraction:{recipe}:{chunk.id}"`. |
| Rev 3 key | §3.4.3: `extraction:{recipe[:16]}:{salt[:16]}:{chunk_id}` when the salt is non-empty, and the **unchanged** format when the salt is `""` (the default for all extractors other than `pattern`). Existing ledgers therefore keep their hits. The new key slots into the existing line. |
| Salt content | `sha256(pattern_provenance + sorted applied decision ids)`. It covers library identity (after decisions replay), engine, engine version and validator version, so approve, deprecate, supersede, user edits and engine upgrades all invalidate reuse. |
| Kept out of the live fingerprint | Stated explicitly: never in `recipe_fingerprint()` or `index_fingerprint()` (`write.py:154-168`). Consistent with B7 round 1 (serving survives library edits). |
| Retention | `ledger_refs(generation, key)` records the salted keys, so ref-GC removes stale variants once their generations are deleted. |
| Tests | `test_library_change_invalidates_extraction_reuse` (approve → new relation; deprecate → relation gone; golden fingerprints unchanged), plus a traceability row. L2 +1 ed. |

Non-blocking note: when `pattern` runs inside the residue composite (§3.4.4), the composite's
`reuse_salt()` must include its members' salts, or the dispatcher must key each member separately.
Stating that in one line in §3.4.4 is enough.

## n1–n9 spot-check

| ID | Status | Evidence |
|---|---|---|
| n1 | Resolved | The `line` recipe emits one chunk per `LogRecord`, whatever `chunking.size` is. Validation requires `max_line_bytes + multiline.max_bytes ≤ line_recipe.max_chunk_chars` (24 KiB). The deployment's 8 KiB + 16 KiB equals exactly 24 KiB. |
| n2 | Resolved | `ledger_refs` side table; the manifest keeps only count and sha. |
| n3 | Resolved | Per-generation ledger job entries are purged with the generation and are content-free by invariant. |
| n4 | Resolved | Pins and spend ledger on sqlite/postgres/sql/memory, with a sqlite sidecar for the file registry. On mongodb and Elasticsearch they are `planned`, and config validation refuses `logs:` there with a typed error. |
| n5 | Resolved | Baseline 328 − 61 + (92…97) = 359–364. B10 is on stream B. |
| n6 | Resolved | `signature_tenant_window` rollup; the bound is ≤ the number of tenants. |
| n7 | Resolved (per response table; private file not re-read) | The runbook points to the spend ledger and `retention report`. |
| n8 | Resolved | `expire` decisions remove unpromoted proposals and their literals from the effective library and from `retention report`. Approved literals are listed under O7. |
| n9 | Resolved | One pin per (process, generation), renewed by heartbeat (30 s / 120 s TTL) with an in-process refcount. Per-request pins are taken only for detached streams. |

## Estimates

The phase rows sum to 100 ed. The credits are 22 ed, so the net is 78. Stream A (L0, L1, L3a, L3b)
is 38 ed and stream B is 54 ed. The usable set is 69 ed. The usable deployment takes about 8 weeks:
stream A reaches L3a around day 14, but L3a waits for L2 until about day 17, and L3b finishes around
day 41, which is about 8.2 weeks. For full scope, stream B's 54 ed plus a docs share is about 11
weeks, at the top of the stated "10–11 weeks". One engineer at 100 ed needs about 20 weeks. The
figures are internally consistent.
