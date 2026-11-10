"""Deterministic generator of a large synthetic log day (unified merge plan F0, for the M2a benchmark).

One UTC day of log-member documents, delivered as the refresh deltas a collector would produce:
every ``refresh_minutes`` (default 5, so 288 refreshes) one gzip JSONL file of upserts — new
episodes (``incident`` + ``signature_day`` + hourly ``episode_cell``), updated versions of recent
episodes (same id, later ``last_hour``, more occurrences), masked ``unclassified_example`` texts drawn
from a bounded pool (so text-hash dedupe has something to remove) and, hourly, updated
``unit_day``/``summary_coverage`` rows. The first refresh also carries the rule catalog
(``signature``, day-independent ids). Documents are built with the write path's own
``ingestion.rollups._document``, so ids and headers match the real compose output.

The **final state** (latest version per id) holds ``target_chunks`` chunks under the declared
chunking (``chunk_size``/``chunk_overlap``; every field fits one chunk, so a document's chunks are
its non-empty text fields plus the title), within one chunk. ``manifest.json`` records the spec,
per-refresh files with sha256, counts per kind, chunk totals, embeddable chunks (``embed_fields``)
and unique embeddable texts. The output is a pure function of the spec: the same spec gives
byte-identical files. Output goes outside the repository (refused inside it).

Usage::

    python -m su_multi_arm.testing.fixtures.unified.log_day --out ~/su_mutliarm_tests/<agent>/logday
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ....core.types import Document
from ....ingestion.episode_summaries import CONTRACT, identity
from ....ingestion.rollups import _document

GENERATOR_VERSION = 1
SERVICES = ("crawler", "indexer", "search-api", "scheduler", "gateway", "renderer", "auth", "billing", "exporter", "notifier", "ingest", "admin")
CATEGORIES = ("availability", "storage", "jobs", "capacity", "connectivity")
DEFAULT_EMBED_FIELDS = (("incident", ("title", "summary")), ("unclassified_example", ("exemplars",)))


@dataclass(frozen=True)
class LogDaySpec:
    day: date = date(2026, 11, 8)
    seed: int = 20261109
    target_chunks: int = 600_000
    tenants: int = 400
    services_per_tenant: int = 3
    rules: int = 300
    refresh_minutes: int = 5
    text_pool: int = 4000  # distinct masked unclassified texts
    unclassified_share: float = 0.35  # of each refresh's new chunk budget
    update_share: float = 0.3  # of the episodes opened in the previous hour, updated per refresh
    chunk_size: int = 1600
    chunk_overlap: int = 160
    embed_fields: tuple[tuple[str, tuple[str, ...]], ...] = DEFAULT_EMBED_FIELDS
    dataset: str = "unified"

    def __post_init__(self):
        if 1440 % self.refresh_minutes:
            raise ValueError("refresh_minutes must divide a day")
        if self.chunk_overlap >= self.chunk_size or self.chunk_size < 1000:
            raise ValueError("chunk_size must be >= 1000 (every generated field fits one chunk) and larger than chunk_overlap")
        if self.services_per_tenant > len(SERVICES):
            raise ValueError(f"services_per_tenant is at most {len(SERVICES)}")
        if self.tenants * self.services_per_tenant * 6 + self.rules * 3 > self.target_chunks // 2:
            raise ValueError("target_chunks too small: the catalog and unit rows of the first refresh would exceed half of it")


def field_chunks(length: int, size: int, overlap: int) -> int:
    """Chunks ``chunk_document`` cuts from a non-blank field of ``length`` characters."""
    if length <= size:
        return 1
    return 1 + math.ceil((length - size) / (size - overlap))


def document_chunks(doc: Document, size: int, overlap: int) -> int:
    texts = [doc.title, *doc.fields.values()]
    return sum(field_chunks(len(t), size, overlap) for t in texts if t.strip())


@dataclass
class _Episode:
    tenant: str
    env: str
    service: str
    rule: str
    first: datetime
    last: datetime
    occurrences: int

    @property
    def uid(self) -> str:
        return identity([[self.tenant, self.env, self.service, self.rule], self.first.isoformat().replace("+00:00", "Z")])[:32]


@dataclass
class _State:
    chunks: dict[str, int] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    embed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    versions: int = 0


class _Builder:
    def __init__(self, spec: LogDaySpec):
        self.spec = spec
        self.rng = random.Random(spec.seed)
        self.start = datetime(spec.day.year, spec.day.month, spec.day.day, tzinfo=timezone.utc)
        self.rules = [(f"rule.{i:04d}", f"Synthetic failure pattern {i}", CATEGORIES[i % len(CATEGORIES)]) for i in range(spec.rules)]
        self.rule_info = {rule: (title, cat) for rule, title, cat in self.rules}
        self.units = []
        for t in range(spec.tenants):
            tenant = f"t-{t:04d}"
            for s in self.rng.sample(SERVICES, spec.services_per_tenant):
                self.units.append((tenant, "prod", s))
        self.pool = [f"unexpected response <num> from upstream <host> while handling <id> (variant {i})" for i in range(spec.text_pool)]
        self.embed = dict(spec.embed_fields)
        self.state = _State()
        self.recent: list[_Episode] = []
        self.unclassified_seq: dict[str, int] = {}

    def _iso(self, value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    def _record(self, doc: Document, out: list[Document]) -> int:
        """Register a version; returns the change in final-state chunks."""
        chunks = document_chunks(doc, self.spec.chunk_size, self.spec.chunk_overlap)
        previous = self.state.chunks.get(doc.id, 0)
        self.state.chunks[doc.id] = chunks
        self.state.kinds[doc.id] = doc.kind
        fields = self.embed.get(doc.kind, ())
        values = {"title": doc.title, **doc.fields}
        self.state.embed[doc.id] = tuple(hashlib.sha256(values[f].encode()).hexdigest()[:16] for f in fields if values.get(f, "").strip())
        self.state.versions += 1
        out.append(doc)
        return chunks - previous

    def catalog(self, out: list[Document]) -> int:
        added = 0
        for rule, title, _cat in self.rules:
            added += self._record(_document(self.spec.dataset, "signature", [rule], title, {"signature_id": rule, "pattern_id": rule, "collection_scope": CONTRACT}, {"template": title}), out)
        return added

    def unit_rows(self, units: list[tuple[str, str, str]], end: datetime, out: list[Document]) -> int:
        added, day = 0, self.spec.day.isoformat()
        for tenant, env, service in units:
            index = f"{tenant}-{env}-{service}-{day.replace('-', '.')}"
            lines = 500 + self.rng.randrange(5000)
            base = {"tenant": tenant, "env": env, "service": service}
            meta = {**base, "source_documents": lines, "keyword_documents": lines // 10, "rule_documents": lines // 20, "unclassified_documents": lines // 50, "date": day,
                    "collection_scope": CONTRACT, "window_start": self._iso(self.start), "window_end": self._iso(end), "observed_at": self._iso(end), "count_basis": "rule_query_count"}
            added += self._record(_document(self.spec.dataset, "summary_coverage", [index, day], "Collection summary", meta,
                                            {"summary": "Keyword-filtered source aggregation; counts include unclassified candidates.", "keyword_counts": "[]"}), out)
            added += self._record(_document(self.spec.dataset, "unit_day", [index, day], "Tenant and service source scope", {**base, "date": day, "lines_total": lines, "collection_scope": CONTRACT}), out)
        return added

    def episode_docs(self, ep: _Episode, out: list[Document]) -> int:
        title, category = self.rule_info[ep.rule]
        first, last = self._iso(ep.first), self._iso(ep.last)
        text = (f"{title}. Tenant {ep.tenant}; service {ep.service}; {ep.occurrences} rule-matching log messages between {first} and {last}. "
                "Recovery and outage duration have not been established.")
        day = ep.first.date().isoformat()
        meta = {"tenant": ep.tenant, "env": ep.env, "service": ep.service, "services": ep.service, "signature_id": ep.rule, "signatures": ep.rule, "category": category,
                "date": day, "first_hour": first, "last_hour": last, "occurrences": ep.occurrences, "lines": ep.occurrences,
                "observed_window_seconds": (ep.last - ep.first).total_seconds(), "community_id": "observed_rule:" + identity([self.spec.dataset, ep.tenant, ep.env, ep.rule, day]),
                "community_label": f"{title} for client {ep.tenant}", "episode_status": "observed_signal", "count_basis": "rule_query_count", "collection_scope": CONTRACT,
                "recovery_basis": "not_established", "issue": title, "evidence_count": 1, "evidence_truncated": False, "completion_evidence_count": 0}
        exemplar = f"[{first}] ERROR {ep.service}: {title.lower()} (request <id> on host <host>)"
        added = self._record(_document(self.spec.dataset, "incident", [ep.uid], title, meta, {"summary": text, "exemplars": exemplar}), out)
        link = {**{k: v for k, v in meta.items() if not k.startswith("community_")}, "date_basis": "event_time", "date_timezone": "UTC", "level": "rule"}
        added += self._record(_document(self.spec.dataset, "signature_day", [ep.uid], title, link, {"summary": text}), out)
        hour = ep.last.replace(minute=0, second=0, microsecond=0)
        index = f"{ep.tenant}-{ep.env}-{ep.service}-{day.replace('-', '.')}"
        cell = {"tenant": ep.tenant, "env": ep.env, "service": ep.service, "date": day, "ts_hour": hour.isoformat(), "signature_id": ep.rule, "occurrences": ep.occurrences,
                "collection_scope": CONTRACT}
        added += self._record(_document(self.spec.dataset, "episode_cell", [index, ep.rule, hour.isoformat()], "Rule match counter", cell), out)
        return added

    def unclassified(self, now: datetime, out: list[Document]) -> int:
        tenant, env, service = self.rng.choice(self.units)
        day = self.spec.day.isoformat()
        index = f"{tenant}-{env}-{service}-{day.replace('-', '.')}"
        seq = self.unclassified_seq.get(index, 0)
        self.unclassified_seq[index] = seq + 1
        text = self.pool[min(int(self.rng.paretovariate(1.2)) - 1, len(self.pool) - 1) if self.rng.random() < 0.7 else self.rng.randrange(len(self.pool))]
        return self._record(_document(self.spec.dataset, "unclassified_example", [index, day, str(seq)], "Unclassified candidate",
                                      {"tenant": tenant, "env": env, "service": service, "date": day, "collection_scope": CONTRACT, "evidence_truncated": False},
                                      {"exemplars": text, "summary": "Keyword candidate; an incident has not been established."}), out)

    def refresh(self, r: int, budget: int) -> tuple[list[Document], dict[str, int]]:
        spec, out = self.spec, []
        lo = self.start + timedelta(minutes=r * spec.refresh_minutes)
        hi = lo + timedelta(minutes=spec.refresh_minutes)
        added, stats = 0, {"new": 0, "updated": 0}
        if r == 0:
            added += self.catalog(out)
            added += self.unit_rows(self.units, hi, out)
        elif r % (60 // spec.refresh_minutes if spec.refresh_minutes <= 60 else 1) == 0:
            self.unit_rows(self.rng.sample(self.units, min(len(self.units), 200)), hi, out)
        horizon = lo - timedelta(hours=1)  # episodes active within the previous hour may still grow
        self.recent = [e for e in self.recent if e.last >= horizon]
        for ep in self.rng.sample(self.recent, int(len(self.recent) * spec.update_share)):
            ep.last = min(hi - timedelta(seconds=1), ep.last + timedelta(seconds=60 + self.rng.randrange(240)))
            ep.occurrences += 1 + self.rng.randrange(5)
            added += self.episode_docs(ep, out)
            stats["updated"] += 1
        unclassified_budget = int(budget * spec.unclassified_share)
        while added < budget - unclassified_budget:
            tenant, env, service = self.rng.choice(self.units)
            first = lo + timedelta(seconds=self.rng.randrange(spec.refresh_minutes * 60 - 60))
            ep = _Episode(tenant, env, service, self.rng.choice(self.rules)[0], first, first + timedelta(seconds=self.rng.randrange(60)), 1 + self.rng.randrange(20))
            added += self.episode_docs(ep, out)
            self.recent.append(ep)
            stats["new"] += 1
        while added < budget:
            added += self.unclassified(lo, out)
            stats["new"] += 1
        stats["chunks_added"] = added
        return out, stats


def _refuse_repository(out: Path) -> None:
    for parent in [out, *out.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "su_multi_arm").is_dir():
            raise ValueError(f"refusing to write the generated log day inside the repository ({parent}); use the external test workspace")


def _gzip(path: Path, docs: list[Document]) -> str:
    payload = "".join(json.dumps({"op": "upsert", "document": json.loads(d.model_dump_json(exclude_none=True))}, sort_keys=True, separators=(",", ":")) + "\n" for d in docs).encode()
    with path.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
        gz.write(payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_log_day(spec: LogDaySpec, out: str | Path) -> dict[str, Any]:
    """Write the refresh files and ``manifest.json`` into ``out`` (must not exist or be empty)."""
    out = Path(out).expanduser().resolve()
    _refuse_repository(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} is not empty")
    (out / "refreshes").mkdir(parents=True, exist_ok=True)
    builder = _Builder(spec)
    n = 1440 // spec.refresh_minutes
    # Diurnal weights: busier in the working hours, never zero; budgets sum to the target.
    weights = [1.0 + 0.8 * math.sin(math.pi * ((r + 0.5) / n)) ** 2 for r in range(n)]
    total_w = sum(weights)
    cumulative, refreshes, done = 0.0, [], 0
    started = time.perf_counter()
    for r in range(n):
        cumulative += weights[r]
        budget = round(spec.target_chunks * cumulative / total_w) - done
        docs, stats = builder.refresh(r, max(budget, 0))
        done += stats["chunks_added"]
        name = f"refreshes/r{r:04d}.jsonl.gz"
        lo = builder.start + timedelta(minutes=r * spec.refresh_minutes)
        refreshes.append({"index": r, "start": lo.isoformat().replace("+00:00", "Z"), "end": (lo + timedelta(minutes=spec.refresh_minutes)).isoformat().replace("+00:00", "Z"),
                          "file": name, "sha256": _gzip(out / name, docs), "upserts": len(docs), "distinct_ids": len({d.id for d in docs}), **stats})
    state = builder.state
    by_kind_docs: dict[str, int] = {}
    by_kind_chunks: dict[str, int] = {}
    for doc_id, kind in state.kinds.items():
        by_kind_docs[kind] = by_kind_docs.get(kind, 0) + 1
        by_kind_chunks[kind] = by_kind_chunks.get(kind, 0) + state.chunks[doc_id]
    embed_texts = [h for hashes in state.embed.values() for h in hashes]
    manifest = {
        "generator": "su_multi_arm.testing.fixtures.unified.log_day",
        "generator_version": GENERATOR_VERSION,
        "synthetic": True,
        "spec": {**asdict(spec), "day": spec.day.isoformat(), "embed_fields": {k: list(v) for k, v in spec.embed_fields}},
        "refreshes": refreshes,
        "totals": {
            "refreshes": n,
            "versions_emitted": state.versions,
            "documents_final": len(state.chunks),
            "chunks_final": sum(state.chunks.values()),
            "documents_by_kind": dict(sorted(by_kind_docs.items())),
            "chunks_by_kind": dict(sorted(by_kind_chunks.items())),
            "embed_texts_final": len(embed_texts),
            "unique_embed_texts_final": len(set(embed_texts)),
            "max_upserts_per_refresh": max(r["upserts"] for r in refreshes),
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    manifest["seconds"] = round(time.perf_counter() - started, 2)  # not written: the files stay byte-identical
    return manifest


def read_refresh(path: str | Path) -> list[Document]:
    with gzip.open(path, "rt") as handle:
        return [Document.model_validate(json.loads(line)["document"]) for line in handle if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--chunks", type=int, default=LogDaySpec.target_chunks)
    parser.add_argument("--day", type=date.fromisoformat, default=LogDaySpec.day)
    parser.add_argument("--seed", type=int, default=LogDaySpec.seed)
    args = parser.parse_args(argv)
    manifest = generate_log_day(LogDaySpec(day=args.day, seed=args.seed, target_chunks=args.chunks), args.out)
    print(json.dumps({"out": str(args.out), "seconds": manifest["seconds"], **manifest["totals"]}, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
