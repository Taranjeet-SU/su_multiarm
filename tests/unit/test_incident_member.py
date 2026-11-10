"""A new write-path dataset as a member of the unified collection (unified merge plan §8.3b; the
incident/outage onboarding example, 2026-11-10). Synthetic only (``tests/fixtures/onboarding/incident``).

Contract rows:

* ``collection.members.<m>.vectors: online`` builds a member whose text no precomputed import holds
  (``on_missing: error``) in the collection's embedding space; without it the build is refused;
* ``metadata_fields`` seals the member store with the collection schema restricted to the member's
  fields (+ ``source``/``kind``), refuses a document carrying another key, builds no typed projection
  and is validated against ``metadata_schema``; ``time_roles`` must name declared member fields;
* the member's ``embed_fields`` decide which of its fields are embedded (write-path members too);
* the member's partition recipe records these settings (``member_build``) in the collection's space;
  a member without them keeps today's recipe;
* composed beside a support member and a log-host member, the knowledge sources (``ticket``, ``doc``
  and the new source) reach the member's records through lexical, dense and structured steps, the
  knowledge half's declared fields are the knowledge members' own (never the log member's), and the
  log member stays outside the knowledge scope.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import Document, Predicate
from su_multi_arm.decision.base import RetrievalStep
from su_multi_arm.decision.source_cards import knowledge_sources, static_cards
from su_multi_arm.decision.two_halves import operation_sources, scoped_arms
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.ingestion.members import MemberRegistry
from su_multi_arm.ingestion.precomputed import text_sha256
from su_multi_arm.ingestion.recipes import partition_recipe, store_recipe

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "onboarding" / "incident"
DIM = 16
DATASET = "unified"
INCIDENT_FIELDS = [
    "incident_monitor", "tenant_candidate", "incident_started_at", "incident_ended_at", "incident_created_at",
    "incident_day", "incident_declared", "incident_failed_polls", "incident_status", "incident_has_rca",
]  # fmt: skip
SCHEMA = {"fields": [
    {"name": "source", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "kind", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "account", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "ticket_status", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "ops_cells", "type": "integer", "filterable": True},
    {"name": "incident_monitor", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "tenant_candidate", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "incident_started_at", "type": "datetime", "filterable": True, "sortable": True},
    {"name": "incident_ended_at", "type": "datetime", "filterable": True, "sortable": True},
    {"name": "incident_created_at", "type": "datetime", "filterable": True, "sortable": True},
    {"name": "incident_day", "type": "date", "filterable": True, "sortable": True},
    {"name": "incident_declared", "type": "boolean", "filterable": True},
    {"name": "incident_failed_polls", "type": "integer", "filterable": True},
    {"name": "incident_status", "type": "string", "filterable": True, "listable": True, "groupable": True},
    {"name": "incident_has_rca", "type": "boolean", "filterable": True},
]}  # fmt: skip
EMBED = {"incident_event": ["summary"], "incident_ticket": ["rca"], "incident_rca": ["root_cause"], "incident_pattern": ["symptom", "likely_cause"]}


def incident_documents() -> list[Document]:
    return [Document.model_validate(json.loads(line)) for line in (FIXTURE / "records.jsonl").read_text().splitlines() if line.strip()]


def support_documents() -> list[Document]:
    return [
        Document(id="ticket:1", dataset=DATASET, source="ticket", kind="ticket", title="Search page shows 503",
                 fields={"description": "Customers see HTTP 503 on the search page after a reindex."}, metadata={"account": "Alderbank Ltd", "ticket_status": "solved"}),
        Document(id="ticket:2", dataset=DATASET, source="ticket", kind="ticket", title="Login loop",
                 fields={"description": "Users are sent back to the login page."}, metadata={"account": "Brightmoor", "ticket_status": "open"}),
        Document(id="https://docs.example.org/reindex", dataset=DATASET, source="doc", kind="doc", title="Reindexing safely",
                 fields={"body": "Throttle bulk reindex jobs so the search cluster keeps heap headroom."}),
    ]  # fmt: skip


def ops_documents() -> list[Document]:
    return [Document(id="logs:incident:2026-11-01:alderbank", dataset=DATASET, source="logs", kind="ops_episode", title="search-api 5xx burst",
                     fields={"summary": "search-api returned 5xx for alderbank between 03:10 and 03:40"}, metadata={"ops_cells": 4})]


def write_import(tmp: Path) -> dict:
    """A precomputed import holding the support member's chunks only (``on_missing: error``)."""
    chunks = [c for d in support_documents() for c in chunk_document(d, 400, 20, [])]
    rows = np.random.default_rng(3).normal(size=(len(chunks), DIM)).astype(np.float32)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    np.save(tmp / "vec.npy", rows)
    (tmp / "keys.jsonl").write_text("".join(json.dumps({"document_id": c.document_id, "field": c.field, "text_sha256": text_sha256(c.text)}) + "\n" for c in chunks))
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    return {"vectors": str(tmp / "vec.npy"), "keys": str(tmp / "keys.jsonl"), "sha256": digest(tmp / "vec.npy"), "keys_sha256": digest(tmp / "keys.jsonl")}


def incident_member(**overrides) -> dict:
    member = {
        "kind": "incident", "sources": ["incident"], "record_kinds": sorted(EMBED), "key": "snapshot", "cadence": "manual",
        "producer": {"kind": "write_path"}, "id_namespaces": ["incident:"], "identity_namespace": "incident.monitor",
        "field_map": {"customer_ref": "tenant_candidate", "status": "incident_status", "url": "url", "component": None},
        "time_roles": {"incident_event": {"event_time": "incident_started_at"}, "incident_ticket": {"event_time": "incident_created_at"}, "incident_rca": {"event_time": "incident_day"}},
        "canonical_units": {"incident_event": "outage", "incident_ticket": "ticket"},
        "embed_fields": EMBED, "vectors": "online", "metadata_fields": INCIDENT_FIELDS,
    }  # fmt: skip
    member.update(overrides)
    return {k: v for k, v in member.items() if v is not ...}


def data(tmp: Path, *, incident: dict | None = None, knowledge: list[str] | None = None) -> dict:
    members = {
        "support": {"kind": "support", "sources": ["ticket", "doc"], "record_kinds": ["ticket", "doc"], "key": "snapshot", "cadence": "daily",
                    "id_namespaces": ["ticket:", "https://docs.example.org/"], "identity_namespace": "support.account", "field_map": {"customer_ref": "account"},
                    "metadata_fields": ["account", "ticket_status"]},
        "incident": incident if incident is not None else incident_member(),
        "ops": {"kind": "logs", "sources": ["logs"], "record_kinds": ["ops_episode"], "key": "day", "cadence": "daily", "producer": {"kind": "log_host"}, "id_namespaces": ["logs:"]},
    }  # fmt: skip
    return {
        "schema_version": 3,
        "dataset": DATASET,
        "backends": {"local": {"kind": "sqlite", "path": str(tmp / "state"), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": SCHEMA,
        # the log stand-in has no vectors (a log unit's text is embedded by the slice ledger, not here)
        "ingestion": {"chunking": {"size": 400, "overlap": 20}, "projections": {"exclude_kinds": {"vector": ["ops_episode"]}}},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": DIM, "precomputed": write_import(tmp)}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}, "structured": {"backend": "local"}}},
        "collection": {"mode": "partitioned", "members": members},
        "crosssource": {"planner": {"enabled": True, "knowledge_sources": knowledge or ["ticket", "doc", "incident"]}},
    }


def built(tmp: Path, **kwargs) -> Engine:
    engine = Engine(validate_config(data(tmp, **kwargs)))
    engine.build_member("support", "snapshot:2026-11-10", support_documents())
    engine.build_member("incident", "snapshot:2026-11-10", incident_documents())
    engine.build_member("ops", "day:2026-11-01", ops_documents())
    engine.compose()
    return engine


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    return built(tmp_path_factory.mktemp("incident"))


# --- declaration -------------------------------------------------------------------------------


def test_member_settings_are_validated(tmp_path):
    with pytest.raises(ConfigError, match="metadata_schema does not declare"):
        validate_config(data(tmp_path, incident=incident_member(metadata_fields=[*INCIDENT_FIELDS, "incident_owner"])))
    with pytest.raises(ValueError, match="time_roles.incident_event names fields outside metadata_fields"):
        validate_config(data(tmp_path, incident=incident_member(metadata_fields=["incident_monitor", "tenant_candidate"], time_roles={"incident_event": {"event_time": "incident_started_at"}})))
    with pytest.raises(ValueError, match="shared write path"):
        validate_config(data(tmp_path, incident=incident_member(producer={"kind": "log_host"})))
    with pytest.raises(ValueError, match="metadata_fields must be unique"):
        validate_config(data(tmp_path, incident=incident_member(metadata_fields=["incident_monitor", "incident_monitor"], time_roles={})))


def test_a_member_outside_the_precomputed_import_needs_online_vectors(tmp_path):
    engine = Engine(validate_config(data(tmp_path, incident=incident_member(vectors=...))))
    with pytest.raises(Exception, match="no precomputed vector"):
        engine.build_member("incident", "snapshot:2026-11-10", incident_documents())
    assert MemberRegistry(engine.config).members() == []  # nothing half registered


def test_a_document_outside_metadata_fields_is_refused(tmp_path):
    engine = Engine(validate_config(data(tmp_path)))
    docs = incident_documents()
    docs[0] = docs[0].model_copy(update={"metadata": {**docs[0].metadata, "ops_cells": 3}})
    with pytest.raises(ValueError, match=r"metadata \['ops_cells'\] outside member 'incident'"):
        engine.build_member("incident", "snapshot:2026-11-10", docs)


# --- build -------------------------------------------------------------------------------------


def test_the_member_store_is_sealed_with_its_own_fields(engine):
    from su_multi_arm.backends.sqlite import SqliteBackend
    from su_multi_arm.ingestion.release import FileGenerationRegistry, generation_id

    registry = MemberRegistry(engine.config)
    ref = next(r for r in registry.members() if r.member == "incident")
    store = registry.store_dir(ref)
    backend = SqliteBackend.from_config(engine.config.backends["local"].model_copy(update={"path": store}))
    schema = backend.records.schema(generation_id(FileGenerationRegistry(store).current()))
    assert sorted(f.name for f in schema.fields) == sorted([*INCIDENT_FIELDS, "source", "kind"])
    assert "projection" not in ref.facets


def test_only_the_declared_embed_fields_are_embedded(engine):
    from su_multi_arm.backends.sqlite import SqliteBackend
    from su_multi_arm.ingestion.release import FileGenerationRegistry, generation_id

    registry = MemberRegistry(engine.config)
    ref = next(r for r in registry.members() if r.member == "incident")
    store = registry.store_dir(ref)
    gen = generation_id(FileGenerationRegistry(store).current())
    backend = SqliteBackend.from_config(engine.config.backends["local"].model_copy(update={"path": store}))
    kinds = {d.id: d.kind for d in incident_documents()}
    chunks = [c for d in incident_documents() for c in chunk_document(d, 400, 20, [])]
    expected = {c.id for c in chunks if c.field in EMBED[kinds[c.document_id]]}
    assert backend.vector.count(gen) == len(expected) < len(chunks)
    assert set(backend.vector.rows(gen, sorted(expected))) == expected


def test_the_partition_recipe_records_the_member_build(tmp_path):
    config = validate_config(data(tmp_path))
    recipe = partition_recipe(config, "incident")
    assert recipe.content["member_build"] == {"vectors": "online", "metadata_fields": sorted(INCIDENT_FIELDS), "embed_fields": EMBED}
    assert recipe.embedding_space == store_recipe(config).embedding_space  # the collection's space
    assert recipe.projection is None
    assert partition_recipe(config, "ops") == store_recipe(config)  # unchanged without settings
    # a log-host member's embed_fields never change its recipe (a live member's slice ledger identity)
    raw = data(tmp_path)
    raw["collection"]["members"]["ops"]["embed_fields"] = {"ops_episode": ["summary"]}
    assert partition_recipe(validate_config(raw), "ops") == store_recipe(config)


# --- serving -----------------------------------------------------------------------------------


def test_the_knowledge_half_reads_the_new_source_and_never_the_log_member(engine):
    config = engine.config
    assert knowledge_sources(config) == ["ticket", "doc", "incident"]
    assert operation_sources(config, knowledge_sources(config)) == ["logs"]
    assert {"lexical", "dense", "structured"} <= scoped_arms(config, knowledge_sources(config))
    cards = static_cards(config, ["lexical", "dense", "structured"])
    incident = next(c for c in cards["sources"] if c["source"] == "incident")
    assert incident["half"] == "knowledge" and incident["members"][0]["member"] == "incident"
    planner = engine.two_halves
    assert planner is not None and planner.knowledge_sources == ["ticket", "doc", "incident"]
    step = RetrievalStep(arm="lexical", query="heapgauge search cluster heap reindex")
    assert planner.knowledge_predicate(step, {}) == Predicate(field="source", op="in", value=["ticket", "doc", "incident"])
    hits = engine.search(SearchRequest(query="heapgauge search cluster heap reindex", filters={"source": "incident"})).hits
    assert {h.source for h in hits} == {"incident"}
    assert "incident:rca:2026-11-01-alderbank" in {h.document_id for h in hits}


def test_the_knowledge_half_declares_the_knowledge_members_fields_only(engine):
    gen, _ = engine.write_path.pin()
    declared = engine.records.declared_fields(gen, ["ticket", "doc", "incident"])
    assert declared is not None
    assert set(INCIDENT_FIELDS) <= declared and "ops_cells" not in declared


def test_structured_steps_count_the_new_kinds(engine):
    from su_multi_arm.core.types import ScopeSpec

    gen, _ = engine.write_path.pin()
    scope = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident_ticket"), Predicate(field="incident_has_rca", op="eq", value=False)])
    result = engine.records.execute(gen, scope, [])
    assert result.matched_ids == ["incident:ticket:OPS-102"]


def test_a_member_without_a_projection_does_not_count_against_the_single_projection_rule(tmp_path):
    from su_multi_arm.runtime.collection import partitioned_refusals

    def refusals(incident: dict) -> list[tuple[str, str]]:
        raw = data(tmp_path, incident=incident)
        raw["collection"]["members"].pop("ops")
        raw["collection"]["members"]["support"].pop("metadata_fields")
        config = validate_config(raw)
        on = config.model_copy(update={"projection": config.projection.model_copy(update={"enabled": True, "backend": "local"})})
        return [r for r in partitioned_refusals(on) if r[0] == "projection"]

    assert refusals(incident_member()) == []
    assert "exactly one non-log member" in refusals(incident_member(metadata_fields=..., time_roles=...))[0][1]
