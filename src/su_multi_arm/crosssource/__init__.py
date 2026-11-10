"""Cross-source layer of the unified collection (docs/plans/unified_merge.md §6.9; lane LD).

* ``identity`` — the private, append-only identity registry (customers, aliases with statuses,
  evidence, decisions, reviews, registry versions), its report/export/import and the query-time
  ``IdentityView`` (only ``matched`` aliases join; a digest mismatch disables joins).
* ``identity_candidates`` — deterministic candidate rules (only exact/casefold unique auto-match).
* ``review_ledger`` — the persistent review spend ledger with the shared cumulative ceiling.

* ``links`` — tier A/B cross-source link builders, the versioned link store with its statuses and
  the pinned ``LinkView``, and link review through the shared ledger (M3).

Identity and links never grant access. The graph overlay (M5) arrives later.
"""
