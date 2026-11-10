"""Synthetic compose/grounded-community/scoped retrieval checks; no production relevance claim."""
from __future__ import annotations

import json

import pytest

from su_multi_arm.arms import ArmInput
from su_multi_arm.core.types import Predicate, Scope
from su_multi_arm.ingestion.logs_compose import compose_recipe

from tests.logs_gold_support import AS_OF, build_gold, gold_config


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    root = tmp_path_factory.mktemp("shared-errors")
    data = gold_config(root)
    data["logs"]["compose"] = {"kinds": {"shared_error": True}}
    data["pipeline"]["arms"]["community"] = {"backend": "local"}
    engine, pipe, report = build_gold(root, data=data)
    return engine, pipe, report


def test_opt_in_compose_has_separate_shared_groups_and_grounded_members(shared):
    engine, pipe, report = shared
    assert report["shared_error"]["groups"] > 0
    assert report["shared_error"]["unavailable_days"] == report["missing"]
    gen = engine.write_path.current()[0]
    graph = engine.backends["local"].graph
    communities = [community for community in graph.communities(gen) if community.id.startswith("shared_error:")]
    assert communities and any(community.id.startswith("cofire:") for community in graph.communities(gen))
    ctx = engine._context_for(None)
    for community in communities:
        docs = engine.documents_in(ctx, community.document_ids)
        assert community.entity_ids
        assert all(doc.kind == "shared_error" and doc.metadata["level"] in {"ERROR", "FATAL"} for doc in docs)
        assert len({doc.metadata["tenant"] for doc in docs}) >= 2
        assert len({doc.metadata["date"] for doc in docs}) == 1
        assert community.stats["lines"] == sum(doc.metadata["lines"] for doc in docs)
        assert community.stats["tenants_affected"] == len({doc.metadata["tenant"] for doc in docs})
        mentions = graph.mentions(gen, community.entity_ids, Scope(document_ids=community.document_ids), 1000)
        assert mentions and all(mention.span.document_id in community.document_ids for mention in mentions)
        assert {mention.type for mention in mentions} >= {"tenant", "service", "signature"}
    assert pipe.compose(AS_OF, pipe.library()).status == "unchanged"
    before = compose_recipe(engine.config)
    changed = engine.config.model_copy(deep=True)
    changed.logs.communities.shared_error.min_tenants = 3
    assert compose_recipe(changed) != before


def test_changed_label_recomposes_without_changing_pattern_matching_or_member_ids(shared):
    engine, pipe, _ = shared
    library = pipe.library()
    loaded = next(entry for entry in library.library.approved("message") if entry.entry.signature and entry.entry.signature.id == "dependency.connect_failed")
    old_name = loaded.entry.provenance.get("issue_name")
    matching_identity = library.library.identity()
    gen_before = engine.write_path.current()[0]
    ids_before = {doc.id for community in engine.backends["local"].graph.communities(gen_before) for doc in engine.documents_in(engine._context_for(None), community.document_ids) if doc.kind == "shared_error"}
    try:
        loaded.entry.provenance["issue_name"] = "Synthetic dependency connection failure"
        assert library.library.identity() == matching_identity
        composed = pipe.compose(AS_OF, library)
        assert composed.status == "built" and composed.generation != gen_before.generation
        gen_after = engine.write_path.current()[0]
        docs = [doc for community in engine.backends["local"].graph.communities(gen_after) for doc in engine.documents_in(engine._context_for(None), community.document_ids) if doc.kind == "shared_error"]
        assert {doc.id for doc in docs} == ids_before
        assert all(doc.metadata["error_type"] == "Synthetic dependency connection failure" for doc in docs if doc.metadata["signature_id"] == "dependency.connect_failed")
        assert pipe.compose(AS_OF, library).status == "unchanged"
    finally:
        if old_name is None:
            loaded.entry.provenance.pop("issue_name", None)
        else:
            loaded.entry.provenance["issue_name"] = old_name


def test_scoped_arm_never_returns_global_label_other_tenant_documents_or_statistics(shared, monkeypatch):
    engine, _, _ = shared
    graph = engine.backends["local"].graph
    communities = graph.communities(engine.write_path.current()[0])
    poisoned = [community.model_copy(update={"label": "SECRET_TENANT unrestricted label", "stats": {"lines": 987654321}}) for community in communities]
    monkeypatch.setattr(graph, "communities", lambda _generation: poisoned)
    scope = Scope(predicates=[Predicate(field="tenant", op="eq", value="t01"), Predicate(field="service", op="eq", value="svc-api"), Predicate(field="date", op="date_interval", granularity="day", lower="2026-03-10", upper="2026-03-11")])
    assert graph.find_entities(engine.write_path.current()[0], ["dependency", "connect_failed"], scope)
    result = engine.arms["community"].run(engine._context_for(None), ArmInput(query="dependency.connect_failed", scope=scope, candidates=100))
    assert result.hits and result.reports
    text = json.dumps(result.model_dump(mode="json"))
    assert "SECRET_TENANT" not in text and "987654321" not in text
    docs = engine.documents_in(engine._context_for(None), [hit.document_id for hit in result.hits])
    assert all(doc.metadata["tenant"] == "t01" and doc.metadata["service"] == "svc-api" and doc.metadata["date"] == "2026-03-10" for doc in docs)
    assert all(report["label"] == "Scoped community" and report["status"] == "scoped" for report in result.reports)
