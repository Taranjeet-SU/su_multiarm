"""Shared helpers of the read-only adoption tests (unified merge M1b): a synthetic "validated support
store" built by a v2 configuration (F0 support documents, imported communities and segment rows of
the F0 live-shape fixture), and the v3 configuration that adopts it with attached layers.

Synthetic only (``tests/fixtures/unified``). Every engine is local: sqlite with ``lexical_engine:
bm25``, the hash embedder, rules decision, no provider call.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from su_multi_arm import Engine
from su_multi_arm.core.config import load_config, validate_config
from su_multi_arm.core.types import Document
from su_multi_arm.ingestion.layers import path_digest
from su_multi_arm.segments import SegmentRecord
from su_multi_arm.segments.build import build_from_config
from su_multi_arm.testing.fixtures.unified.live_shape import SCHEMA_FIELDS

ROOT = Path(__file__).resolve().parent
SUPPORT = ROOT / "fixtures" / "unified" / "support" / "adopted.jsonl"
LIVE_SHAPE = ROOT / "fixtures" / "unified" / "live_shape"
STORAGE = ("catalog", "records", "ledger", "sessions", "cache", "communities")
NAMESPACES = ["ticket:", "https://docs.example.org/kb/"]
PROJECTION = {
    "enabled": True,
    "backend": "local",
    "tables": {"ticket": {"kind": "ticket", "description": "One row per support ticket.", "columns": ["source", "account", "product", "service", "status", "priority", "created_at"]}},
}
QUERIES = [
    "crawler fetch timeout",
    "large sitemaps stop with timeouts",
    "search 5xx errors",
    "index write failed after deploy",
    "how do I configure the crawler",
    "scheduler job halted",
    "password reset email not delivered",
    "getting started",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def documents() -> list[Document]:
    return [Document.model_validate_json(line) for line in SUPPORT.read_text().splitlines() if line.strip()]


def original_data(root: Path, *, segments: Path | None = None) -> dict[str, Any]:
    """The v2 configuration that builds and serves the store (today's monolithic engine)."""
    communities = root / "communities.jsonl"
    arms: dict[str, Any] = {
        "lexical": {"backend": "local"},
        "dense": {"backend": "local"},
        "structured": {"backend": "local"},
        "community": {"backend": "local", "claim_hits": True},
    }
    if segments is not None:
        arms["segment"] = {"path": str(segments), "lexical": True}
    return {
        "schema_version": 2,
        "dataset": "unified",
        "backends": {"local": {"kind": "sqlite", "path": str(root / "store"), "lexical_engine": "bm25", "bm25": {"tokenizer": "ascii_alnum"}}},
        "storage": {k: "local" for k in STORAGE},
        "metadata_schema": {"canonical_unit": "ticket", "fields": SCHEMA_FIELDS},
        "projection": PROJECTION,
        "communities": {"strategy": "imported", "imported": {"path": str(communities), "sha256": sha256(communities), "on_ungrounded": "drop_claim", "extractive_sentences": 3}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": arms},
    }


def build_original(root: Path) -> Path:
    """Build the store with the v2 configuration, then its segment sidecar; returns the
    configuration file (``original.yaml``) that serves both (the segment arm is not in the index
    recipe, so adding it after the build keeps the store's fingerprint)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "communities.jsonl").write_bytes((LIVE_SHAPE / "communities.jsonl").read_bytes())
    seed = Engine(validate_config(original_data(root)))
    seed.ingest(documents())
    rows = [SegmentRecord.model_validate_json(line) for line in (LIVE_SHAPE / "segment_rows.jsonl").read_text().splitlines() if line.strip()]
    (root / "segments.jsonl").write_text("\n".join(r.model_dump_json() for r in rows) + "\n")
    build_from_config(seed.config, root / "segments.jsonl", root / "segments", engine=seed)
    path = root / "original.yaml"
    path.write_text(yaml.safe_dump(original_data(root, segments=root / "segments"), sort_keys=False))
    return path


def store_tree(store: Path) -> dict[str, tuple[str, int]]:
    """sha256 and mtime of every file under the store (the 'never writes' evidence)."""
    out = {}
    for path in sorted(store.rglob("*")):
        if path.is_file():
            out[path.relative_to(store).as_posix()] = (sha256(path), path.stat().st_mtime_ns)
    return out


def unified_data(root: Path, original: Path, *, layers: bool = True, **member: Any) -> dict[str, Any]:
    """The v3 configuration adopting the store built by ``original`` (read-only), with the imported
    communities and the segment sidecar as attached layers."""
    data = original_data(root)
    data["schema_version"] = 3
    data["backends"]["local"]["path"] = str(root / "unified")
    data["pipeline"]["arms"]["segment"] = {"path": str(root / "segments"), "lexical": True}
    support = {
        "kind": "support",
        "sources": ["ticket", "doc"],
        "record_kinds": ["ticket", "doc"],
        "key": "adopted",
        "cadence": "adopted",
        "producer": {"kind": "adopted"},
        "id_namespaces": NAMESPACES,
        "adopt": {"store": str(root / "store"), "original_config": str(original), "original_config_sha256": sha256(original)},
        "required": True,
        **member,
    }
    data["collection"] = {"mode": "partitioned", "members": {"support": support}}
    if layers:
        communities = root / "communities.jsonl"
        data["collection"]["layers"] = [
            {"name": "support_communities", "kind": "communities_imported", "path": str(communities), "sha256": sha256(communities), "sources": ["ticket", "doc"], "arms": ["community"], "member": "support"},
            {"name": "support_segments", "kind": "segment_sidecar", "path": str(root / "segments"), "sha256": path_digest(root / "segments"), "sources": ["ticket"], "arms": ["segment"]},
        ]
        data["communities"]["layers"] = [{"name": "support", "strategy": "imported", "sources": ["ticket", "doc"], "layer": "support_communities"}]
    else:
        data["pipeline"]["arms"].pop("segment")
        data["pipeline"]["arms"].pop("community")
    return data


def adopted_engine(root: Path, original: Path, **kwargs: Any) -> Engine:
    """A partitioned engine whose only member is the adopted store, composed."""
    engine = Engine(validate_config(unified_data(root, original, **kwargs)))
    engine.view.attach().adopt("support")
    engine.compose()
    return engine


def todays_engine(original: Path) -> Engine:
    return Engine(load_config(original))


def write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)

