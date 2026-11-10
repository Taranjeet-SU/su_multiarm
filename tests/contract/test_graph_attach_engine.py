"""An attached (read-only) Kuzu store behind the engine's graph arm: the write path never targets it
(no writes, not part of the index recipe) and ``graph.path_evidence`` turns free-text traversal
paths into document hits with the edge quotes as provenance. Synthetic store from
``tests/unit/test_kuzu_attach.py``."""

from __future__ import annotations

import pytest
import yaml

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.generations import attached_read_only, placement

pytest.importorskip("kuzu")

from tests.unit.test_kuzu_attach import MAPPING, store  # noqa: E402,F401  (module-scoped fixture)

DOCS = [
    Document(id="case:T1", dataset="ext", source="case", title="Login fails after upgrade", fields={"body": "Users cannot log in after the upgrade. Refreshing the token fixed it."}),
    Document(id="case:T2", dataset="ext", source="case", title="Login slow", fields={"body": "Login takes minutes on the Portal login page."}),
]


def config(tmp_path, store, path_evidence=True):  # noqa: F811
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(yaml.safe_dump({**{k: v for k, v in MAPPING.items() if k != "generations"}, "any_generation": True}))
    return validate_config(
        {
            "schema_version": 2,
            "dataset": "ext",
            "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}, "kz": {"kind": "kuzu", "path": str(store), "mode": "attach", "attach": {"mapping": str(mapping)}}},
            "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
            "pipeline": {"arms": {"lexical": {"backend": "local"}, "graph": {"backend": "kz", "hops": 1, "path_evidence": path_evidence}}},
        }
    )


def test_attached_store_is_never_a_write_target(tmp_path, store):  # noqa: F811
    cfg = config(tmp_path, store)
    assert attached_read_only(cfg, "kz") and not attached_read_only(cfg, "local")
    assert "kz" not in placement(cfg)["graph"]
    engine = Engine(cfg)
    manifest = engine.ingest(DOCS)  # would raise "attached read-only" if the write path targeted kz
    assert manifest["status"] == "complete"
    # the same index serves a config without the graph arm (the attach is not in the recipe)
    data = cfg.model_dump(mode="json")
    data["pipeline"]["arms"].pop("graph")
    data["backends"].pop("kz")
    assert Engine(validate_config(data)).write_path.current()[0].generation == manifest["generation"]


def test_path_evidence_returns_anchored_documents(tmp_path, store):  # noqa: F811
    engine = Engine(config(tmp_path, store))
    engine.ingest(DOCS)
    result = engine.search(SearchRequest(query="login timeout"))
    hits = result.arm_hits["graph"]
    assert {h.document_id for h in hits} >= {"case:T1"}
    rows = [p for h in hits for p in h.provenance]
    assert rows and all(p["kind"] == "graph_path" and p["quote"] for p in rows)
    assert all(h.chunk_ids == [] for h in hits)


def test_path_evidence_is_off_by_default(tmp_path, store):  # noqa: F811
    engine = Engine(config(tmp_path, store, path_evidence=False))
    engine.ingest(DOCS)
    result = engine.search(SearchRequest(query="login timeout"))
    assert result.arm_hits.get("graph") == []
    assert result.trace["steps"][-1]["paths"] or any(s["arm"] == "graph" and s["paths"] for s in result.trace["steps"])
