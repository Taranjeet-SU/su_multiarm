"""Hierarchical communities and deterministic statistics (plan §3.13, R17, minimal version)."""

from __future__ import annotations

import pytest

from su_multi_arm.communities.base import STORAGE_KEY, Community, build_communities
from su_multi_arm.communities.stats import community_stats
from su_multi_arm.core.config import CommunitiesConfig, validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import Document, FieldSpec, MetadataSchema

pytest.importorskip("networkx")

def _memberships() -> dict[str, set[str]]:
    """Two groups (A, B) of four dense sub-clusters each (three entities on four documents), linked
    inside a group by one cross entity per sub-cluster pair and across groups by one bridge."""
    out: dict[str, set[str]] = {}
    for group in "AB":
        for sub in range(4):
            for j in range(3):
                out[f"{group}{sub}e{j}"] = {f"{group}{sub}d{i}" for i in range(4)}
        for s in range(4):
            for t in range(s + 1, 4):
                out[f"{group}x{s}{t}"] = {f"{group}{s}d0", f"{group}{t}d1"}
    out["bridge"] = {"A0d2", "B0d2"}
    return out


MEMBERSHIPS = _memberships()
NAMES = {k: k.upper() for k in MEMBERSHIPS}


def build(seed=42, min_split=2, **options):
    from su_multi_arm.communities.hierarchy import build_hierarchical

    return build_hierarchical(MEMBERSHIPS, NAMES, 1, seed, min_split=min_split, **options)


def test_hierarchy_is_deterministic_and_levels_nest():
    first, second = build(), build()
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]
    by_id = {c.id: c for c in first}
    levels = {c.level for c in first}
    assert {0, 1} <= levels <= {0, 1, 2}
    for community in first:
        if community.level == 0:
            assert community.parent_id is None
        else:
            parent = by_id[community.parent_id]
            assert parent.level == community.level - 1
            assert set(community.document_ids) <= set(parent.document_ids)
    roots = [c for c in first if c.level == 0]
    assert sorted(d for c in roots for d in c.document_ids) == sorted({d for docs in MEMBERSHIPS.values() for d in docs})


def test_levels_option_and_min_split():
    assert {c.level for c in build(levels=1)} == {0}
    assert {c.level for c in build(min_split=100)} == {0}
    assert build_communities("louvain_hierarchical", {}, {}, 1, 1) == []
    flat = build_communities("louvain_hierarchical", MEMBERSHIPS, NAMES, 1, 42)
    assert [c.id for c in flat] == [c.id for c in build(min_split=4)]


def test_stats_from_declared_fields_only():
    schema = MetadataSchema(fields=[FieldSpec(name="opened", type="datetime"), FieldSpec(name="team", type="string")])
    docs = {
        "d1": Document(id="d1", kind="ticket", metadata={"opened": "2024-01-05T00:00:00+00:00", "team": "x"}),
        "d2": Document(id="d2", kind="ticket", metadata={"opened": "2024-01-20T00:00:00+00:00"}),
        "d3": Document(id="d3", kind="article", metadata={"opened": "2024-03-01T00:00:00+00:00"}),
        "d4": Document(id="d4", kind="ticket", metadata={"opened": "not a date"}),
        "d9": Document(id="d9", kind="ticket"),
    }
    community = Community(id="c", document_ids=["d1", "d2", "d3", "d4"], entity_ids=["e1", "e2"])
    entity_docs = {"e1": {"d1", "d2", "d3"}, "e2": {"d1", "d2", "d3", "d4", "d9"}, "e3": {"d4"}, "e4": {"d9"}}
    stats = community_stats(community, docs, entity_docs, 5, schema, "opened", "month", 5, {"e1": "One"})
    assert stats["size"] == 4 and stats["entities"] == 2 and stats["members_by_kind"] == {"article": 1, "ticket": 3}
    assert stats["trend"] == {
        "field": "opened",
        "granularity": "month",
        "buckets": {"2024-01-01": 2, "2024-03-01": 1},
        "slope": -1.0,
        "first": "2024-01-01",
        "last": "2024-03-01",
        "missing_or_invalid": 1,
    }
    # e1: (3/4)/(3/5) = 1.25; e2: (4/4)/(5/5) = 1.0; e3 has support 1 in a 4-document community: not reported
    assert stats["top_entities"] == [{"entity_id": "e1", "name": "One", "support": 3, "lift": 1.25}, {"entity_id": "e2", "name": "e2", "support": 4, "lift": 1.0}]
    assert "trend" not in community_stats(community, docs, entity_docs, 5, schema, None, "month", 0, {}) and "team" not in str(stats)
    assert [r["entity_id"] for r in community_stats(community, docs, entity_docs, 5, schema, None, "year", 1, {})["top_entities"]] == ["e1"]


def test_storage_round_trip_keeps_old_rows_compatible():
    plain = Community(id="c", report={"status": "disabled", "claims": []})
    assert plain.storage_report() == {"status": "disabled", "claims": []}  # unchanged for existing rows
    rich = Community(id="c2", level=1, parent_id="c", stats={"size": 2}, report={"status": "valid", "claims": []})
    stored = rich.storage_report()
    assert stored[STORAGE_KEY] == {"level": 1, "parent_id": "c", "stats": {"size": 2}} and STORAGE_KEY not in rich.report
    back = Community.from_storage("c2", "", [], [], stored)
    assert back == rich and Community.from_storage("c", "", [], [], plain.storage_report()) == plain


def test_hierarchy_and_stats_persist_on_sqlite_and_memory(tmp_path):
    from su_multi_arm.backends.memory import MemoryBackend
    from su_multi_arm.backends.sqlite import SqliteBackend
    from su_multi_arm.testing import backend_conformance as kit

    entities, relations = kit.graph_fixture()
    rich = Community(id="k2", level=2, parent_id="k1", stats={"size": 1, "trend": {"buckets": {}}}, document_ids=["d6"])
    for backend in (MemoryBackend(), SqliteBackend(tmp_path / "s")):
        if kit.caps.satisfied(backend, "catalog"):
            kit.seed(backend)
        backend.graph.upsert(kit.GEN, entities, relations, [])
        backend.graph.put_communities(kit.GEN, [rich])
        assert backend.graph.communities(kit.GEN) == [rich]


def test_config_rules_and_recipe_stability(tmp_path):
    assert CommunitiesConfig().recipe() == {"strategy": "entity", "min_documents": 2, "seed": 42, "prompt": None}  # 2.0.0a1 recipe keys
    with pytest.raises(ValueError, match="louvain_hierarchical only"):
        CommunitiesConfig(strategy="louvain", hierarchy={})
    base = {
        "schema_version": 2,
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "metadata_schema": {"fields": [{"name": "opened", "type": "datetime"}, {"name": "team", "type": "string"}]},
    }
    validate_config({**base, "communities": {"strategy": "louvain_hierarchical", "stats": {"time_basis": "opened"}}})
    with pytest.raises(ConfigError, match="datetime or date field"):
        validate_config({**base, "communities": {"stats": {"time_basis": "team"}}})
    from su_multi_arm.compat.config_v1 import downgrade

    with pytest.raises(ConfigError, match="hierarchical"):
        downgrade(validate_config({**base, "backends": {"local": {"kind": "sqlite", "path": str(tmp_path)}}, "communities": {"strategy": "louvain_hierarchical"}}))
