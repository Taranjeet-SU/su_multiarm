"""Engine end to end on the embedded kuzu graph backend: ingest writes the graph (fact edges with
provenance), hierarchical communities with deterministic stats and the generation pointer into kuzu;
a planner selects a declared traversal template by name through a typed graph intent (never query
text), and the graph arm returns path provenance as document evidence. Unknown templates are refused
at the decision; a template that does not fit the configured ontology is refused at validation."""

from __future__ import annotations

import re

import pytest

from su_multi_arm import Document, Engine
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.decision import LlmPlan
from su_multi_arm.decision.base import RetrievalStep
from su_multi_arm.models import Entity as RawEntity, Extraction, Relation as RawRelation
from su_multi_arm.providers import JsonModel

pytest.importorskip("kuzu")
pytest.importorskip("networkx")

FAKE = {"provider": "openai_compatible", "model": "fake"}
TEXTS = [
    ("t1", "Login Timeout on the Portal was fixed by Token Refresh.", "2024-01-10T00:00:00+00:00"),
    ("t2", "Login Timeout again, it was fixed by Cache Flush on the Portal.", "2024-02-11T00:00:00+00:00"),
    ("t3", "Export Failure on the Portal was fixed by Token Refresh.", "2024-02-20T00:00:00+00:00"),
    ("t4", "Export Failure in Reports was fixed by Cache Flush.", None),
]
DOCS = [
    Document(id=i, dataset="demo", source="case", title=f"Ticket {i}", fields={"body": text}, metadata={"created_at": created} if created else {})
    for i, text, created in TEXTS
]
VOCAB = {"Login Timeout": "symptom", "Export Failure": "symptom", "Token Refresh": "fix", "Cache Flush": "fix", "Portal": "product", "Reports": "product"}
FIXED = re.compile(r"(Login Timeout|Export Failure)[^.]*?fixed by (Token Refresh|Cache Flush)")


def extraction_reply(*args):
    """A deterministic stand-in for the LLM extractor: vocabulary entities plus one
    ``resolved_by`` relation whose quote holds both endpoints and the trigger."""
    text = next(a for a in args if isinstance(a, dict) and "passage" in a)["passage"]
    entities = [RawEntity(name=name, type=kind, quote=name) for name, kind in VOCAB.items() if name in text]
    relations = [RawRelation(subject=m.group(1), predicate="resolved_by", object=m.group(2), quote=m.group(0)) for m in FIXED.finditer(text)]
    return Extraction(entities=entities, relations=relations).model_dump_json()


TEMPLATES = {
    "fixes": {"kind": "neighbors", "description": "symptom -> resolved_by -> fix", "hop": {"predicates": ["resolved_by"], "direction": "out"}, "hops": 1, "seed_types": ["symptom"]},
    "mentions": {"kind": "mentioned_in", "description": "where an entity is mentioned"},
}


def config(tmp_path, **arm):
    return validate_config(
        {
            "schema_version": 2,
            "dataset": "demo",
            "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}, "kg": {"kind": "kuzu", "path": str(tmp_path / "graph.kuzu"), "buffer_pool_mb": 64}},
            "storage": {**{k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache")}, "communities": "kg"},
            "metadata_schema": {"fields": [{"name": "created_at", "type": "datetime", "time_basis": "created"}]},
            "pipeline": {
                "decision": {"kind": "llm", "on_failure": "fail_closed"},
                "arms": {"lexical": {"backend": "local"}, "graph": {"backend": "kg", "templates": TEMPLATES, **arm}, "community": {"backend": "kg"}},
            },
            "extraction": {"extractors": ["llm"], "labels": ["symptom", "fix", "product"], "predicate_triggers": {"resolved_by": ["fixed by"]}},
            "communities": {"strategy": "louvain_hierarchical", "min_documents": 1, "hierarchy": {"levels": 2, "min_split": 2}, "stats": {"time_basis": "created_at", "top_entities": 3}},
            "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}, "planner": FAKE, "generator": FAKE, "extractor": FAKE}},
        }
    )


def planner(steps):
    def respond(self, system, payload, schema, budget, stage):
        assert stage == "planner"
        respond.payloads.append(payload)
        return LlmPlan.model_validate({"steps": steps})

    respond.payloads = []
    return respond


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(JsonModel, "call_text", extraction_reply)
    engine = Engine(config(tmp_path))
    engine.ingest(DOCS)
    yield engine
    engine.close() if hasattr(engine, "close") else None


def test_planner_selects_a_template_and_the_arm_returns_path_provenance(monkeypatch, engine):
    reply = planner([{"arm": "graph", "query": "fixes for login timeout", "template": "fixes", "seeds": ["Login Timeout"]}])
    monkeypatch.setattr(JsonModel, "call", reply)
    result = engine.search("What fixed the login timeout?")
    assert reply.payloads[0]["graph_templates"] == {"fixes": "symptom -> resolved_by -> fix", "mentions": "where an entity is mentioned"}
    step = result.trace["steps"][0] if "steps" in result.trace else result.trace["rounds"][0]["steps"][0]
    assert step["arm"] == "graph" and step["graph_template"] == "fixes" and step["paths"] >= 2
    hits = result.arm_hits["graph"]
    assert {h.document_id for h in hits} == {"t1", "t2"}
    for hit in hits:
        edge = hit.provenance[0]
        assert edge["kind"] == "graph_path" and edge["predicate"] == "resolved_by" and edge["edge_kind"] == "fact"
        assert edge["support"] == "trigger_in_span" and edge["quote"] in hit.text and edge["extractor"]


def test_mentioned_in_template_and_unresolved_seeds(monkeypatch, engine):
    monkeypatch.setattr(JsonModel, "call", planner([{"arm": "graph", "query": "where", "template": "mentions", "seeds": ["Cache Flush", "Nothing Here"]}]))
    result = engine.search("Where is cache flush mentioned?")
    assert {h.document_id for h in result.arm_hits["graph"]} == {"t2", "t4"}


def test_unknown_template_is_refused_at_the_decision(monkeypatch, engine):
    monkeypatch.setattr(JsonModel, "call", planner([{"arm": "graph", "query": "x", "template": "free_cypher", "seeds": ["Portal"]}]))
    result = engine.search("anything about the portal")
    assert result.trace["decision"]["outcome"] == "abstain" and result.hits == []


def test_template_fields_are_only_for_graph_steps():
    with pytest.raises(ValueError):
        LlmPlan.model_validate({"steps": [{"arm": "lexical", "query": "x", "template": "fixes", "seeds": ["a"]}]})
    with pytest.raises(ValueError):
        LlmPlan.model_validate({"steps": [{"arm": "graph", "query": "x", "template": "fixes"}]})
    with pytest.raises(ValueError):
        RetrievalStep(arm="dense", query="x", graph={"template": "fixes", "seeds": ["a"]})


def test_hierarchical_communities_with_stats_are_stored_in_kuzu(engine):
    backend = engine.backends["kg"] if hasattr(engine, "backends") else None
    gen = engine.write_path.current()[0]
    communities = engine.write_path.generations.targets("communities")[0].backend.graph.communities(gen) if backend is None else backend.graph.communities(gen)
    assert communities and {c.level for c in communities} <= {0, 1}
    by_id = {c.id: c for c in communities}
    for community in communities:
        assert community.parent_id is None if community.level == 0 else by_id[community.parent_id].level == 0
        stats = community.stats
        assert stats["size"] == len(community.document_ids) and stats["members_by_kind"] == {"document": len(community.document_ids)}
        trend = stats["trend"]
        assert trend["field"] == "created_at" and sum(trend["buckets"].values()) + trend["missing_or_invalid"] == stats["size"]
        assert all(row["support"] >= 1 and row["lift"] > 0 for row in stats["top_entities"])


def test_templates_are_validated_against_a_configured_ontology(tmp_path):
    ontology = {"entity_types": ["symptom", "fix"], "predicates": {"resolved_by": {"subject_types": ["symptom"], "object_types": ["fix"]}}}
    config(tmp_path, ontology=ontology)  # fits
    with pytest.raises(ConfigError, match="unknown predicates"):
        config(tmp_path, ontology=ontology, templates={"bad": {"kind": "neighbors", "hop": {"predicates": ["caused_by"]}}})
    with pytest.raises(ConfigError, match="unknown entity types"):
        config(tmp_path, ontology=ontology, templates={"bad": {"kind": "neighbors", "seed_types": ["ticket"]}})
    with pytest.raises(ConfigError, match="cannot follow"):
        config(
            tmp_path,
            ontology=ontology,
            templates={"bad": {"kind": "chain", "seed_types": ["symptom"], "steps": [{"predicates": ["resolved_by"], "direction": "out"}, {"predicates": ["resolved_by"], "direction": "out"}]}},
        )
