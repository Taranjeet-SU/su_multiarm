"""Shared helpers of the live-log-member engine tests (unified merge: M2a wired into M1a/M1b, lane LB).

An **F0 collection** here is: the F0 support documents in an adopted-shape store (built by a v2
configuration, then adopted read-only), plus the live log member ``logs`` (``cadence: minutes``)
whose closed days are archive day members + the catalog member built by ``LiveLogs.close_day``
(each close composes one generation) and whose live window is served from the F0 delta slices
sealed into the member's ledger. Synthetic only (``tests/fixtures/unified``): sqlite with
``lexical_engine: bm25``, the hash embedder, rules decision, no provider call.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from su_multi_arm import Engine
from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.log_slices import UnitReader, embed_pending_slices
from su_multi_arm.ingestion.text_vectors import embed_missing
from su_multi_arm.testing.fixtures.unified import load

ROOT = Path(__file__).resolve().parent
F0 = ROOT / "fixtures" / "unified"
STORAGE = ("catalog", "records", "ledger", "sessions", "cache", "communities")
NAMESPACES = ["ticket:", "https://docs.example.org/kb/"]
ARCHIVE_DAYS = ["2026-11-04", "2026-11-05", "2026-11-06"]
# the F0 live window: each day's slices in order, ``True`` = a full-day rebuild (tombstones)
LIVE = {"2026-11-07": [("base", False), ("h13", False), ("day", True)], "2026-11-08": [("base", False), ("h22", False), ("h23", False)],
        "2026-11-09": [("base", False), ("h09", False), ("m1025", False)]}
LOG_KINDS = ["episode_cell", "incident", "shared_error", "signature", "signature_day", "summary_coverage", "unclassified_example", "unit_day"]
EMBED_FIELDS = {"incident": ["title", "summary"], "shared_error": ["summary"], "unclassified_example": ["exemplars"]}
QUERIES = [
    "crawler fetch timeout",
    "index write failed after deploy",
    "search 5xx errors for the tenant",
    "scheduler job halted",
    "password reset email not delivered",
    "how do I configure the crawler",
    "render timeout crawler.render_timeout",
    "large sitemaps stop with timeouts",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)


def fixture() -> Any:
    return load(F0)


def support_documents(fx: Any) -> list[Any]:
    key = next(k for k in fx.members if k.startswith("support/"))
    return list(fx.members[key])


def original_data(root: Path, fx: Any, arms: tuple[str, ...]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "dataset": "unified",
        "backends": {"local": {"kind": "sqlite", "path": str(root / "store"), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in STORAGE},
        "metadata_schema": fx.manifest["metadata_schema"],
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {arm: {"backend": "local"} for arm in arms}},
    }


def build_support_store(root: Path, fx: Any, arms: tuple[str, ...] = ("lexical", "dense", "structured")) -> Path:
    """The validated support store (today's engine, v2) and its configuration file."""
    root.mkdir(parents=True, exist_ok=True)
    data = original_data(root, fx, arms)
    Engine(validate_config(data)).ingest(support_documents(fx))
    path = root / "original.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def unified_data(root: Path, fx: Any, original: Path, *, logs: bool, arms: tuple[str, ...] = ("lexical", "dense", "structured"), quotas: dict[str, float] | None = None) -> dict[str, Any]:
    data = original_data(root, fx, arms)
    data["schema_version"] = 3
    data["backends"]["local"]["path"] = str(root / "unified")
    members: dict[str, Any] = {
        "support": {
            "kind": "support", "sources": ["ticket", "doc"], "record_kinds": ["ticket", "doc"], "key": "adopted", "cadence": "adopted",
            "producer": {"kind": "adopted"}, "id_namespaces": NAMESPACES, "required": True,
            "adopt": {"store": str(root / "store"), "original_config": str(original), "original_config_sha256": sha256(original)},
        }
    }
    if logs:
        members["logs"] = {
            "kind": "logs", "sources": ["logs"], "record_kinds": LOG_KINDS, "key": "day", "cadence": "minutes", "producer": {"kind": "log_host"},
            "id_namespaces": ["logs:"], "embed_fields": EMBED_FIELDS,
        }
    data["collection"] = {"mode": "partitioned", "members": members}
    if quotas:
        data["collection"]["selection"] = {"source_quotas": quotas}
    return data


def seal_live_window(ledger: Any, fx: Any, *, compact: bool = False) -> None:
    """Seal the F0 delta slices of the live window; ``compact``: then compact each day to one base."""
    for day_s, slices in LIVE.items():
        day = date.fromisoformat(day_s)
        for name, full in slices:
            ledger.seal(day, "day" if full else "five_minute", fx.slices[f"slice:{day_s}:{name}"], full_day=full, as_of=at(day, 23, 59))
        if compact:
            ledger.compact(day, "base")


def embed_all(engine: Engine, member: str = "logs") -> None:
    """The dense worker: embed every pinned slice's and archive member's texts into the vector store."""
    live = engine.view.attach().live
    ledger, store = live.ledger(member), live.vector_store(member)

    class Docs:
        def encode(self, texts):
            return engine.embedder.encode(texts)

    embed_pending_slices(ledger, store, Docs())
    registry = engine.view.attach()
    manifest = registry.generations.current() or {}
    from su_multi_arm.ingestion.compose import members_of

    for ref in members_of(manifest):
        if ref.member == member:
            reader = UnitReader(registry.unit_dir(ref), ledger.bm25)
            try:
                embed_missing(store, reader.embed()[1], Docs())
            finally:
                reader.close()


def collection(root: Path, fx: Any, *, logs: bool = True, live: bool = True, compact: bool = False, embed: bool = True, arms: tuple[str, ...] = ("lexical", "dense", "structured"),
               quotas: dict[str, float] | None = None, clock: Any = None) -> Engine:
    """An F0 collection engine (module doc)."""
    original = build_support_store(root, fx, arms)
    engine = Engine(validate_config(unified_data(root, fx, original, logs=logs, arms=arms, quotas=quotas)))
    registry = engine.view.attach()
    registry.adopt("support")
    engine.compose()
    if not logs:
        return engine
    lives = registry.live
    if clock is not None:
        lives.clock = clock
    ledger = lives.ledger("logs")
    for day_s in ARCHIVE_DAYS:
        day = date.fromisoformat(day_s)
        ledger.seal(day, "five_minute", fx.members[f"logs/day:{day_s}"], as_of=at(day, 23, 59))
        lives.close_day("logs", day, as_of=at(day, 23, 59))
    if live:
        seal_live_window(ledger, fx, compact=compact)
    if embed:
        embed_all(engine)
    return engine


def hits(result: Any) -> list[tuple]:
    """Every observable field of the fused hits (order included)."""
    return [(h.document_id, h.score, tuple(h.chunk_ids), h.text, h.title, h.source, h.url, tuple(h.arms), repr(h.provenance)) for h in result.hits]


def arm_hits(result: Any) -> dict[str, list[tuple]]:
    return {arm: [(h.document_id, h.score, tuple(h.chunk_ids)) for h in hs] for arm, hs in result.arm_hits.items()}
