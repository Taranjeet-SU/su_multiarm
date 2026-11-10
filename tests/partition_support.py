"""Shared helpers of the partition-core tests (unified merge M1a): the F0 fixture as member
partitions, configurations for a partitioned engine and its monolithic twin, and comparators.

Synthetic only (``tests/fixtures/unified``; ``examples/documents.jsonl``). Every engine is local:
sqlite with ``lexical_engine: bm25``, the hash embedder, rules decision, no provider call.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from su_multi_arm import Engine
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document
from su_multi_arm.ingestion.members import catalog_documents
from su_multi_arm.testing.fixtures.unified import load

ROOT = Path(__file__).resolve().parent
F0 = ROOT / "fixtures" / "unified"
EXAMPLES = ROOT.parent / "examples" / "documents.jsonl"
STORAGE = ("catalog", "records", "ledger", "sessions", "cache", "communities")
SUPPORT_KEY = "snapshot:2026-11-09"  # the support member as a write-path snapshot (adoption: M1b)


def f0():
    return load(F0)


def f0_members(fx: Any) -> dict[str, list[tuple[str, list[Document]]]]:
    """F0's archive members as ``{member: [(key, documents)]}``: support as a snapshot, the log
    rule catalog split out of the day/month members into ``logs/catalog`` (latest-wins)."""
    out: dict[str, list[tuple[str, list[Document]]]] = {}
    log_units = []
    for member in fx.manifest["members"]:
        docs = fx.members[f"{member['id']}/{member['key']}"]
        key = SUPPORT_KEY if member["id"] == "support" else member["key"]
        if member["id"] == "logs":
            log_units.append(docs)
            docs = [d for d in docs if d.kind != "signature"]
        out.setdefault(member["id"], []).append((key, docs))
    out["logs"].append(("catalog", catalog_documents(log_units, ())))
    return out


def union(members: dict[str, list[tuple[str, list[Document]]]], only: set[str] | None = None) -> list[Document]:
    """The documents of ``members`` (latest wins by id), for the monolithic twin."""
    view: dict[str, Document] = {}
    for name, units in members.items():
        if only is not None and name not in only:
            continue
        for _, docs in units:
            for d in docs:
                view[d.id] = d
    return [view[k] for k in sorted(view)]


def member_block(fx: Any, members: dict[str, list[tuple[str, list[Document]]]], quotas: dict[str, float] | None = None, stats: dict[str, str] | None = None) -> dict[str, Any]:
    declared = {m["id"]: m for m in fx.manifest["members"]}
    block: dict[str, Any] = {"mode": "partitioned", "members": {}}
    for name, units in members.items():
        kinds = sorted({d.kind for _, docs in units for d in docs})
        meta = declared[name]
        scheme = "snapshot" if name in {"support", "tracker"} else "day"
        block["members"][name] = {
            "kind": meta["kind"],
            "sources": sorted({d.source for _, docs in units for d in docs}),
            "record_kinds": kinds,
            "key": scheme,
            "cadence": "daily",
            "id_namespaces": meta["id_namespaces"],
        }
    if quotas:
        block["selection"] = {"source_quotas": quotas}
    if stats:
        block["lexical"] = {name: {"stats": value} for name, value in stats.items()}
    return block


def config(path: Path, *, schema: dict[str, Any] | None = None, collection: dict[str, Any] | None = None, arms: tuple[str, ...] = ("lexical", "dense", "structured"), dataset: str = "unified", candidates: int | None = None) -> Any:
    pipeline_arms: dict[str, Any] = {}
    for arm in arms:
        pipeline_arms[arm] = {"backend": "local", **({"candidates": candidates} if candidates and arm in {"lexical", "dense"} else {})}
    data: dict[str, Any] = {
        "schema_version": 3 if collection is not None else 2,
        "dataset": dataset,
        "backends": {"local": {"kind": "sqlite", "path": str(path), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in STORAGE},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": pipeline_arms},
    }
    if schema is not None:
        data["metadata_schema"] = schema
    if collection is not None:
        data["collection"] = collection
    return validate_config(data)


def partitioned(tmp: Path, fx: Any, members: dict[str, list[tuple[str, list[Document]]]], **kwargs: Any) -> Engine:
    """A partitioned engine with every member of ``members`` built and composed."""
    quotas, stats = kwargs.pop("quotas", None), kwargs.pop("stats", None)
    engine = Engine(config(tmp, schema=fx.manifest["metadata_schema"], collection=member_block(fx, members, quotas, stats), **kwargs))
    for name, units in members.items():
        for key, docs in units:
            engine.build_member(name, key, docs)
    engine.compose()
    return engine


def monolithic(tmp: Path, fx: Any, documents: list[Document], **kwargs: Any) -> Engine:
    engine = Engine(config(tmp, schema=fx.manifest["metadata_schema"], **kwargs))
    engine.ingest(documents)
    return engine


def hits(result: Any) -> list[tuple]:
    """Every observable field of the fused hits (order included)."""
    return [(h.document_id, h.score, tuple(h.chunk_ids), h.text, h.title, h.source, h.url, tuple(h.arms), repr(h.provenance)) for h in result.hits]


def arm_hits(result: Any) -> dict[str, list[tuple]]:
    return {arm: [(h.document_id, h.score, tuple(h.chunk_ids)) for h in hs] for arm, hs in result.arm_hits.items()}


def close_hits(a: list[tuple], b: list[tuple], tolerance: float = 1e-9) -> bool:
    """Same ids and order; scores within ``tolerance`` (the declared epsilon-floor rounding)."""
    return [x[0] for x in a] == [x[0] for x in b] and all(abs(x[1] - y[1]) <= tolerance for x, y in zip(a, b))


def capture_evidence(engine: Engine) -> list[list[dict[str, Any]]]:
    """Record the evidence rows every answer packs for the generator (instance-level wrapper)."""
    captured: list[list[dict[str, Any]]] = []
    original = engine._pack

    def pack(ctx, state):
        out = original(ctx, state)
        captured.append(out[0])
        return out

    engine._pack = pack
    return captured


def kinds_of(documents: list[Document]) -> dict[str, int]:
    return dict(Counter(d.kind for d in documents))


def examples() -> list[Document]:
    return [Document.model_validate_json(line) for line in EXAMPLES.read_text().splitlines() if line.strip()]
