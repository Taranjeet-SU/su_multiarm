"""Retention across every store (addendum A1 §3.8.3–3.8.4; slice L3b).

**Window.** ``as_of`` is the calendar day the run happens on ("today" in ``logs.timezone``).
``keep_days: N`` keeps the N most recent **complete** days, ``as_of - N`` through ``as_of - 1``
(``logs_compose.expiry_cutoff``); a partition with an older key expires. With ``keep_days: 10`` the
run on day 12 deletes day 1, and 10 days (2–11) remain.

**Rule (a).** A partition older than the window expires. ``gc`` deletes **every** non-active
generation whose ``logs.partitions`` lists an expired day, whatever ``keep_generations`` says;
``keep_generations`` (the active one included) only bounds how many in-window generations stay for
rollback, so a rollback can never resurrect expired data. The active generation never lists an
expired day: retention refuses to run when it does (compose did not exclude the day yet).

=================================== ==============================================================
store                               what ``run`` does
=================================== ==============================================================
catalog, lexical, vector, records,  ``WritePath.delete_generation`` per doomed generation (it takes
graph, communities                  the write lease per call); a live cross-process pin is honoured
                                    (``GenerationPinned``), retried until ``max_pin_wait_seconds``
cache                               ``invalidate(<generation prefix>)`` on ``storage.cache``
ledger job entries                  ``drop_job(<generation>)`` (content-free by invariant)
ledger ``extraction:``/``embedding:`` reference-based: refs of deleted generations are dropped, then
/``raw:``/``pattern_library:``      every key no remaining generation references is purged
ledger tagged replies               every entry tagged ``partition:<source>:<day>`` of an expired day
partition artifacts                 ``PartitionStore.expire``: a content-free tombstone with hashes
sessions                            ``SessionStore.expire`` with ``ttl <= window``
run reports                         rotated at the window length (``schedule.py``)
generation manifests                kept by the registry as content-free audit records; for a
                                    registry that removes them with the generation (sqlite), a
                                    content-free summary goes to ``<partitions>/audit/``
=================================== ==============================================================

Not present in this release, reported as ``not_applicable``: ``proposals/<run>/`` and proposal
``expire`` records (the learning loop is L4b), exported trace files (the package writes none).

**Verify** (``verify``) checks: (1) no remaining generation and no live partition is older than
the cutoff; (2) no ledger entry tagged with an expired day remains and no ``extraction:``/
``embedding:``/``raw:``/``pattern_library:`` key is unreferenced; (3) content: the tombstones'
line and source-id hashes occur in no text-bearing row of the remaining generations (catalog
documents of every remaining generation, ledger raw entries, live partition artifacts), and no
cache entry of a deleted generation is left; (4) manifests, the audit records and the run reports
hold none of those hashes. Content kept by owner decision O7 (approved pattern examples) is
reported separately and never counted as a failure.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..backends import extras
from ..core.errors import BackendError, ConfigError
from ..core.types import Page
from .logs_compose import derived_keys, expire_derived, expiry_cutoff
from .partitions import FilePartitionStore, line_hash, source_id_hash
from .pins import GenerationPinned, pin_registry
from .release import generation_id

LEDGER_PREFIXES = ("extraction:", "embedding:", "raw:", "pattern_library:")
TOKEN = re.compile(r"[A-Za-z0-9._:-]+")
AUDIT = "audit"


def partition_tag(source: str, key: str) -> str:
    """The ledger tag of a raw reply that carries text of partition ``key`` (learning, reports)."""
    return f"partition:{source}:{key}"


class Retention:
    def __init__(
        self,
        config: Any,
        store: FilePartitionStore,
        write_path: Any,
        *,
        backends: dict[str, Any] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        pins: Any = None,
    ):
        if config.logs is None:
            raise ConfigError("no logs: section is configured", "logs")
        self.config, self.logs, self.store, self.write_path = config, config.logs, store, write_path
        self.backends = backends or write_path.backends
        self.now = now or (lambda: datetime.now(timezone.utc))
        import time

        self.sleep = sleep or time.sleep
        self.registry = write_path.registry
        self.pins = pins if pins is not None else pin_registry(self.registry)
        from .raw_reparse import open_raw

        self.raw: Any = open_raw(config)  # listing, expiry and mode checks only
        self.ledger = self.backends[config.storage.ledger].ledger
        if not isinstance(self.ledger, extras.LedgerRetention):
            raise ConfigError(f"the ledger on {config.storage.ledger!r} has no retention operations (tags, refs, purge)", "storage.ledger")

    # --- inventory -----------------------------------------------------------------------------

    def log_generations(self) -> dict[str, dict[str, Any]]:
        """Manifests of this source's log generations, by generation id."""
        out = {}
        for generation in self.registry.generations():
            manifest = self.registry.manifest(generation)
            section = (manifest or {}).get("logs")
            if section and section.get("source") == self.logs.source:
                out[generation] = manifest
        return out

    def cutoffs(self, as_of: date) -> dict[str, str | None]:
        """The oldest key kept per data class on calendar day ``as_of`` (``None``: kept forever)."""
        retention = self.logs.retention
        span = retention.derived_days()
        return {
            "partitions": expiry_cutoff(as_of, retention.keep_days),
            "derived": None if span is None else expiry_cutoff(as_of, span),
            "raw": expiry_cutoff(as_of, retention.raw_days) if self.raw is not None else None,
        }

    def plan(self, as_of: date) -> dict[str, Any]:
        """What ``run`` would delete (no side effects)."""
        oldest = expiry_cutoff(as_of, self.logs.retention.keep_days)
        cuts = self.cutoffs(as_of)
        derived_cut = cuts["derived"]
        active = self.registry.current()
        active_id = None if active is None else active["generation"]
        expired_keys = sorted(k for k in self.store.keys() if k < oldest)
        generations = self.log_generations()
        if active_id in generations and any(p["key"] < oldest for p in generations[active_id]["logs"]["partitions"]):
            raise ConfigError(f"the active generation {active_id} still lists a day before {oldest}; compose the window first (expiry is skipped)", "logs")
        if derived_cut is not None and active_id in generations and any(d["key"] < derived_cut for d in generations[active_id]["logs"].get("derived", [])):
            raise ConfigError(f"the active generation {active_id} still lists a derived day before {derived_cut}; compose first (expiry is skipped)", "logs")
        doomed, kept = [], []
        in_window = []
        for generation, manifest in generations.items():
            if generation == active_id:
                continue
            keys = [p["key"] for p in manifest["logs"]["partitions"]]
            derived = [d["key"] for d in manifest["logs"].get("derived", [])]
            if any(k < oldest for k in keys):
                doomed.append({"generation": generation, "reason": "lists_expired_partition"})
            elif derived_cut is not None and any(k < derived_cut for k in derived):
                doomed.append({"generation": generation, "reason": "lists_expired_derived"})
            else:
                in_window.append((manifest["logs"].get("as_of", ""), manifest["logs"].get("composed_at", ""), generation))
        room = max(0, self.logs.retention.keep_generations - (1 if active_id else 0))
        for _, _, generation in sorted(in_window, reverse=True):
            if room > 0:
                kept.append(generation)
                room -= 1
            else:
                doomed.append({"generation": generation, "reason": "beyond_keep_generations"})
        expired_derived = [k for k in derived_keys(self.store) if (derived_cut is not None and k < derived_cut)]
        expired_raw = [k for k in self.raw.keys() if k < cuts["raw"]] if self.raw is not None else []
        return {
            "cutoff": oldest,
            "cutoffs": cuts,
            "active": active_id,
            "expired_partitions": expired_keys,
            "expired_derived": expired_derived,
            "expired_raw": expired_raw,
            "delete_generations": doomed,
            "keep_generations": kept,
        }

    # --- run -----------------------------------------------------------------------------------

    def run(self, as_of: date) -> dict[str, Any]:
        plan = self.plan(as_of)
        report: dict[str, Any] = {"cutoff": plan["cutoff"], "active": plan["active"], "deleted_generations": [], "pinned": [], "left_for_cleanup": []}
        for item in plan["delete_generations"]:
            generation = item["generation"]
            try:
                deleted = self._delete(generation)
            except GenerationPinned as exc:
                report["pinned"].append({"generation": generation, "holders": exc.holders, "status": "pinned_expired_generation" if item["reason"] == "lists_expired_partition" else "pinned"})
                continue
            if deleted.get("left_for_cleanup"):
                # a backend refused or failed: the generation is not gone everywhere, so its days and
                # ledger references stay until an operator removes the rest (never reported as deleted)
                report["left_for_cleanup"].extend(deleted["left_for_cleanup"])
                report["pinned"].append({"generation": generation, "holders": [], "status": "incomplete_deletion"})
                continue
            report["deleted_generations"].append({**item, **deleted})
        deleted_ids = [d["generation"] for d in report["deleted_generations"]]
        blocked = {p["generation"] for p in report["pinned"] if p["status"] in {"pinned_expired_generation", "incomplete_deletion"}}
        expired = list(plan["expired_partitions"])
        tags = [partition_tag(self.logs.source, k) for k in expired]
        refs_complete = self._refs_complete()
        spec = extras.PurgeSpec(drop_refs_of=deleted_ids, tags=tags, unreferenced_prefixes=list(LEDGER_PREFIXES) if refs_complete else [])
        report["ledger"] = self.ledger.purge(spec)
        if not refs_complete:
            report["ledger"]["unreferenced_purge"] = "skipped: a remaining generation predates ledger_refs"
        keep = set()
        for key in self.store.keys():
            if key >= plan["cutoff"]:
                keep |= self.store.line_hashes(key)
        tombstones = []
        for key in expired:
            if blocked:
                # a pinned generation still lists expired days: its artifacts stay until it can go
                continue
            tombstones.append(self.store.expire(key, self.logs.retention.verify_sample, keep).model_dump(mode="json", exclude={"verify_sample", "verify_source_ids"}))
        report["expired_partitions"] = tombstones
        report["expired_derived"] = [] if blocked else [k for k in plan["expired_derived"] if expire_derived(self.store, k)]
        # raw copies are referenced by no generation: they go on their own (shorter) clock
        report["expired_raw"] = [k for k in plan["expired_raw"] if self.raw.expire(k)] if self.raw is not None else []
        report["cutoffs"] = plan["cutoffs"]
        report["sessions"] = self._expire_sessions()
        report["not_applicable"] = {"proposals": "learning loop (L4b) not present", "trace_exports": "the package writes no trace files"}
        if blocked:
            statuses = {p["status"] for p in report["pinned"] if p["generation"] in blocked}
            report["status"] = "incomplete_deletion" if "incomplete_deletion" in statuses else "pinned_expired_generation"
        return report

    def _delete(self, generation: str) -> dict[str, Any]:
        """Delete one generation everywhere, honouring cross-process pins (retry until the wait bound)."""
        waited = 0.0
        while True:
            holders = self.pins.pinned(generation)
            if not holders:
                break
            if waited >= self.logs.retention.max_pin_wait_seconds:
                raise GenerationPinned(generation, [h.holder for h in holders])
            step = self.logs.retention.pin_retry_seconds
            self.sleep(step)
            waited += step
        manifest = self.registry.manifest(generation)
        self._audit(manifest)
        result = self.write_path.delete_generation(generation)
        gen = generation_id(manifest)
        cache_backend = self.backends.get(self.config.storage.cache)
        invalidated = cache_backend.cache.invalidate(gen.prefix()) if cache_backend is not None and hasattr(cache_backend, "cache") else 0
        jobs = self.ledger.drop_job(generation)
        return {"backends": result["deleted"], "left_for_cleanup": result["left_for_cleanup"], "cache_invalidated": invalidated, "ledger_job_entries": jobs}

    def _refs_complete(self) -> bool:
        return all("ledger_refs_count" in (self.registry.manifest(g) or {}) for g in self.registry.generations())

    def _expire_sessions(self) -> dict[str, Any]:
        store_name = self.config.sessions.store or self.config.storage.sessions
        backend = self.backends.get(store_name)
        if backend is None or not hasattr(backend, "session"):
            return {"status": "not_configured"}
        ttl = min(float(self.config.sessions.ttl_seconds), self.logs.retention.keep_days * 86_400.0)
        return {"expired": backend.session.expire(self.now().timestamp(), ttl), "ttl_seconds": ttl}

    def _audit(self, manifest: dict[str, Any] | None) -> None:
        """A content-free audit record of a generation about to be deleted."""
        if manifest is None:
            return
        keep = {k: manifest.get(k) for k in ("generation", "dataset", "fingerprint", "created_at", "documents", "chunks", "logs", "ledger_refs_count", "ledger_refs_sha", "pattern_provenance")}
        keep["deleted_at"] = self.now().isoformat()
        folder = self.logs.partitions.path / AUDIT / self.logs.source
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"generation-{manifest['generation']}.json").write_text(json.dumps(keep, indent=2, sort_keys=True, default=str))

    # --- report --------------------------------------------------------------------------------

    def report(self, as_of: date) -> dict[str, Any]:
        """``retention report``: what is retained (partitions, generations, ledger refs), what the
        next run would delete, and content kept beyond the window by owner decision O7."""
        plan = self.plan(as_of)
        generations = self.log_generations()
        return {
            "cutoff": plan["cutoff"],
            "keep_days": self.logs.retention.keep_days,
            "keep_generations": self.logs.retention.keep_generations,
            "partitions": [{"key": m.key, "version": m.version, "library_identity": m.library_identity, "records": m.counts.get("records", 0)} for m in self.store.list()],
            "tombstones": [t.key for t in self.store.tombstones()],
            "generations": [
                {"generation": g, "active": g == plan["active"], "days": [p["key"] for p in m["logs"]["partitions"]], "ledger_refs": m.get("ledger_refs_count")}
                for g, m in sorted(generations.items(), key=lambda item: item[1]["logs"].get("as_of", ""))
            ],
            "next_run": {"expire": plan["expired_partitions"], "expire_derived": plan["expired_derived"], "expire_raw": plan["expired_raw"], "delete_generations": plan["delete_generations"]},
            "scopes": {
                "partitions": {"keep_days": self.logs.retention.keep_days, "cutoff": plan["cutoffs"]["partitions"]},
                "derived": {"scope": self.logs.retention.derived, "cutoff": plan["cutoffs"]["derived"], "days": derived_keys(self.store), "note": "masked exemplar lines in derived days are kept as long as the derived scope"},
                "raw": None if self.raw is None else {"raw_days": self.logs.retention.raw_days, "cutoff": plan["cutoffs"]["raw"], **self.raw.report(), "note": "unredacted source documents"},
            },
            "retained_beyond_window": self._o7(),
        }

    def _o7(self) -> dict[str, Any]:
        """Approved pattern examples (redacted lines) kept under owner decision O7, listed."""
        ref = self.config.extraction.pattern_library
        out: dict[str, Any] = {"decision": "O7", "approved_examples": []}
        if ref is None:
            return out
        try:
            from ..extraction.patterns.extractor import PatternExtractor

            library = PatternExtractor(ref, None).load().library
        except Exception as exc:  # reported, never hidden
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        for loaded in library.approved():
            for example in loaded.entry.examples.positive:
                out["approved_examples"].append({"pattern": loaded.entry.id, "owner": loaded.owner, "sha256": line_hash(example)})
        return out

    # --- verify --------------------------------------------------------------------------------

    def verify(self, as_of: date) -> dict[str, Any]:
        oldest = expiry_cutoff(as_of, self.logs.retention.keep_days)
        cuts = self.cutoffs(as_of)
        problems: list[str] = []
        checks: dict[str, Any] = {}
        # Days whose partition expired but whose derived data the derived scope keeps: their masked
        # lines (exemplars) are retained by configuration, reported, not counted as leaks.
        retained = {t.key for t in self.store.tombstones() if t.key < oldest and (cuts["derived"] is None or t.key >= cuts["derived"])}
        tombstones = [t for t in self.store.tombstones() if t.key < oldest and t.key not in retained]
        checks["retained_derived_days"] = sorted(retained)
        if cuts["derived"] is not None:
            stale = [k for k in derived_keys(self.store) if k < cuts["derived"]]
            if stale:
                problems.append(f"derived snapshots {stale} are older than the derived cutoff {cuts['derived']}")
        if self.raw is not None:
            stale_raw = [k for k in self.raw.keys() if k < cuts["raw"]]
            if stale_raw:
                problems.append(f"raw copies {stale_raw} are older than the raw cutoff {cuts['raw']}")
            problems.extend(f"raw store: {p}" for p in self.raw.mode_problems())
            checks["raw"] = {"days": self.raw.keys(), "cutoff": cuts["raw"]}
        expired_keys = {t.key for t in tombstones}
        # 1. manifests and partitions
        generations = self.log_generations()
        for generation, manifest in generations.items():
            old = sorted(p["key"] for p in manifest["logs"]["partitions"] if p["key"] < oldest)
            if old:
                problems.append(f"generation {generation} still lists expired days {old}")
            if cuts["derived"] is not None:
                old_derived = sorted(d["key"] for d in manifest["logs"].get("derived", []) if d["key"] < cuts["derived"])
                if old_derived:
                    problems.append(f"generation {generation} still lists expired derived days {old_derived}")
        live_old = [k for k in self.store.keys() if k < oldest]
        if live_old:
            problems.append(f"partitions {live_old} are older than the cutoff {oldest} and not expired")
        checks["generations"] = {"remaining": sorted(generations), "cutoff": oldest}
        # 2. ledger
        tagged = {k: self.ledger.tagged(partition_tag(self.logs.source, k)) for k in sorted(expired_keys)}
        leftover = {k: v for k, v in tagged.items() if v}
        if leftover:
            problems.append(f"ledger entries tagged with expired days remain: {leftover}")
        referenced: set[str] = set()
        for generation in self.ledger.referencing_generations():
            referenced.update(self.ledger.refs(generation))
        # expired content: the tombstones' hashes, minus lines that also occur in a live day
        line_hashes = {h for t in tombstones for h in t.verify_sample}
        id_hashes = {h for t in tombstones for h in t.verify_source_ids}
        live_lines: set[str] = set()
        for key in self.store.keys():
            live_lines |= self.store.line_hashes(key)
        line_hashes -= live_lines
        hits: list[str] = []
        scanned = [0]

        def scan(where: str, text: str) -> None:  # streamed: rows are checked as they are read
            scanned[0] += 1
            hits.extend(_content_hits([(where, text)], line_hashes, id_hashes))

        orphans: list[str] = []
        for prefix in LEDGER_PREFIXES:
            after = None
            while True:
                page = self.ledger.raw_entries(prefix, after, 500)
                if not page:
                    break
                for entry in page:
                    # a tagged entry (a learning reply) lives until a tagged partition expires (checked above)
                    if entry.key not in referenced and not self.ledger.raw_tags(entry.key):
                        orphans.append(entry.key)
                    if prefix != "embedding:":
                        scan(f"ledger:{entry.key}", entry.raw_reply)
                after = page[-1].key
        if orphans and self._refs_complete():
            problems.append(f"{len(orphans)} ledger entries are referenced by no remaining generation (first: {orphans[0]})")
        dangling = sorted(set(self.ledger.referencing_generations()) - set(self.registry.generations()))
        if dangling:
            problems.append(f"ledger_refs name deleted generations {dangling[:3]}")
        checks["ledger"] = {"referenced": len(referenced), "unreferenced": len(orphans), "tagged_expired": sum(len(v) for v in tagged.values())}
        # 3. content of every remaining generation's documents and of partitions that should be gone
        catalog = self.backends[self.config.storage.catalog].catalog
        for generation, manifest in generations.items():
            gen = generation_id(manifest)
            cursor = None
            while True:
                page = catalog.iter_scope(gen, None, Page(cursor=cursor, size=500))
                for document in page.items:
                    for name, value in {"title": document.title, **document.fields}.items():
                        scan(f"{generation}:{document.id}:{name}", value)
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor
        for key in live_old:  # already a problem above; scanned so the report shows what leaked
            for artifacts in self.store.units(key):
                scan(f"partition:{key}", "\n".join(r.message for r in artifacts.records))
        if hits:
            problems.append(f"expired content found in {len(hits)} rows (first: {hits[0]})")
        cache_left = self._cache_leftovers()
        if cache_left:
            problems.append(f"cache entries of deleted generations remained: {cache_left}")
        checks["content"] = {"expired_line_hashes": len(line_hashes), "expired_source_ids": len(id_hashes), "rows_scanned": scanned[0], "hits": len(hits)}
        # 4. content-free invariants
        manifests = [json.dumps(m, sort_keys=True, default=str) for m in generations.values()]
        audit = self.logs.partitions.path / AUDIT / self.logs.source
        manifests += [p.read_text() for p in sorted(audit.glob("*.json"))] if audit.exists() else []
        reports = self.logs.partitions.path / "reports" / self.logs.source
        manifests += [p.read_text() for p in sorted(reports.glob("*.json"))] if reports.exists() else []
        leaks = _content_hits([(f"manifest:{i}", text) for i, text in enumerate(manifests)], line_hashes | live_lines, set())
        if leaks:
            problems.append(f"manifests, audit records or run reports hold log-line text ({leaks[0]})")
        checks["content_free"] = {"documents_checked": len(manifests), "hits": len(leaks)}
        return {"ok": not problems, "cutoff": oldest, "cutoffs": cuts, "problems": problems, "checks": checks, "retained_beyond_window": {"decision": "O7", "reported_by": "retention report"}}

    def _cache_leftovers(self) -> list[str]:
        """Invalidate the prefix of every audited (deleted) generation again: removing anything means
        the cache still held it (counted as a failure; idempotent otherwise)."""
        audit = self.logs.partitions.path / AUDIT / self.logs.source
        backend = self.backends.get(self.config.storage.cache)
        if backend is None or not hasattr(backend, "cache") or not audit.exists():
            return []
        left = []
        for path in sorted(audit.glob("generation-*.json")):
            record = json.loads(path.read_text())
            if record["generation"] in set(self.registry.generations()):
                continue
            prefix = f"{record['dataset']}/{record['generation']}/"
            try:
                if backend.cache.invalidate(prefix):
                    left.append(record["generation"])
            except BackendError:
                continue
        return left


def _content_hits(texts: list[tuple[str, str]], line_hashes: set[str], id_hashes: set[str]) -> list[str]:
    """Rows whose lines (or tokens, for source ids) hash to an expired sample."""
    hits = []
    if not line_hashes and not id_hashes:
        return hits
    for where, text in texts:
        if not text:
            continue
        candidates = _lines_of(text)
        if line_hashes and any(line_hash(c) in line_hashes for c in candidates):
            hits.append(where)
            continue
        if id_hashes and any(source_id_hash(token) in id_hashes for token in TOKEN.findall(text)):
            hits.append(where)
    return hits


def _lines_of(text: str) -> list[str]:
    """Lines of a text and of any JSON string values in it (ledger replies, manifests)."""
    out = [line for line in text.split("\n") if line.strip()]
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            data = json.loads(stripped)
        except ValueError:
            return out
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, str):
                out.extend(line for line in node.split("\n") if line.strip())
    return out


def audit_dir(config: Any) -> Path:
    return config.logs.partitions.path / AUDIT / config.logs.source
