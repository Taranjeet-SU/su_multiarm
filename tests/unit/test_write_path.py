"""Ingestion write path on the contracts (plan §3.14, §3.14.1): staged generation, per-projection
validation, failure manifest with the old generation kept active, strict/quarantine/partial
policies, ledger reuse, relation dedupe across inherited text, relocated grounding."""

from __future__ import annotations

import json

import numpy as np
import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document
from su_multi_arm.extraction.base import BuiltinExtractor, ExtractionInput, entity_key
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.extraction.dedupe import dedupe_relations
from su_multi_arm.ingestion.policy import ExtractionPolicy, PolicyViolation
from su_multi_arm.ingestion.write import IngestError, WritePath
from su_multi_arm.providers import CallBudget, Embedder, JsonModel

BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "metadata_schema": {"fields": [{"name": "source", "type": "string"}, {"name": "created_at", "type": "datetime"}]},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}, "graph": {"backend": "local"}, "community": {"backend": "local"}}},
    "extraction": {"extractors": ["gazetteer"], "gazetteer": {"topic": ["exemption certificate", "rounding"], "product": ["CloudTax"]}},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32}}},
}


def config(**overrides):
    data = json.loads(json.dumps(BASE))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return validate_config(data)


def documents():
    return [
        Document(id="kb:certificate", dataset="demo", source="article", title="Add an exemption certificate", fields={"details": "Open the record and add the exemption certificate. CloudTax rounding applies per line."}, metadata={"created_at": "2025-01-01T00:00:00+00:00"}),
        Document(id="case:certificate", dataset="demo", source="case", title="Certificate expired", fields={"description": "An exemption certificate stopped applying in CloudTax."}, metadata={"created_at": "2025-02-03T12:00:00+00:00"}),
        Document(id="kb:rounding", dataset="demo", source="article", title="Rounding configuration", fields={"details": "CloudTax rounding applies per invoice line."}),
    ]


class FakeModel:
    """Stands in for ``JsonModel``: returns canned raw text per stage."""

    def __init__(self, replies):
        self.replies, self.config = replies, type("C", (), {"model": "fake"})()
        self.calls = 0

    def call_text(self, system, payload, schema, budget, stage):
        budget.reserve()
        budget.record({"stage": stage, "provider": "fake", "model": "fake", "success": True})
        self.calls += 1
        reply = self.replies(payload) if callable(self.replies) else self.replies
        if isinstance(reply, Exception):
            raise reply
        return reply

    def call(self, system, payload, schema, budget, stage):
        return schema.model_validate_json(self.call_text(system, payload, schema, budget, stage))


def write_path(cfg, backend=None, extractor_model=None, community_model=None):
    backend = backend or MemoryBackend()
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={"extractor": extractor_model}))
    return WritePath(cfg, {"local": backend}, extractor=extractor, embedder=Embedder(cfg.models.roles.embedder), community_model=community_model), backend


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    return MemoryBackend() if request.param == "memory" else SqliteBackend(tmp_path / "state")


def test_build_validates_every_projection_and_activates(backend):
    cfg = config()
    path, backend = write_path(cfg, backend)
    manifest = path.ingest(documents())
    assert manifest["schema_version"] == 2 and manifest["index_format"] == 3 and manifest["status"] == "complete"
    assert manifest["documents"] == 3 and manifest["chunks"] == 6 and manifest["entities"] > 0
    assert set(manifest["projections"]) == {"catalog", "records", "lexical", "vector", "graph", "communities"}
    assert all(p["verified"] for p in manifest["projections"].values())
    assert manifest["coverage"]["extraction"]["accepted"] == 3 and manifest["coverage"]["incomplete"] is False
    assert manifest["coverage"]["stages"]["extraction"]["status"] == "complete"
    gen, current = path.current()
    assert current["generation"] == manifest["generation"] and gen.fingerprint == path.index_fingerprint()
    status = path.status()
    assert status["documents"]["kb:certificate"]["extraction"] == "accepted"
    assert status["documents"]["kb:certificate"]["community_memberships"] >= 1
    assert backend.graph.communities(gen) and backend.graph.communities(gen)[0].status() == "disabled"
    assert backend.vector.count(gen) == 6 and backend.lexical.count(gen) == 6 and backend.records.count(gen) == 3


def test_failed_projection_keeps_old_generation_and_records_failure(backend, monkeypatch):
    cfg = config()
    path, backend = write_path(cfg, backend)
    first = path.ingest(documents())
    monkeypatch.setattr(Embedder, "encode", lambda self, texts, query=False: (_ for _ in ()).throw(RuntimeError("simulated embedding failure")))
    with pytest.raises(IngestError, match="embedding failure") as info:
        path.ingest([Document(id="new", dataset="demo", fields={"body": "fresh text"})])
    failure = info.value.failure
    assert failure["category"] == "provider" and failure["stage"] == "embeddings" and failure["policy"] == "strict"
    assert failure["staged_projections"] == ["local"] and failure["left_for_cleanup"] == []
    assert path.current()[1]["generation"] == first["generation"]
    assert path.registry.failures()[0]["generation"] == failure["generation"]
    assert path.registry.generations() == [first["generation"]]
    assert backend.generations() == [f"demo/{first['generation']}/"]
    assert backend.ledger.status(failure["generation"]) == "failed"


def test_projection_count_mismatch_is_a_typed_failure(backend, monkeypatch):
    cfg = config()
    path, backend = write_path(cfg, backend)
    monkeypatch.setattr(type(backend.lexical), "count", lambda self, gen: 0)
    with pytest.raises(IngestError, match="projection holds 0") as info:
        path.ingest(documents())
    assert info.value.failure["category"] == "projection:lexical"
    with pytest.raises(ValueError, match="no active index"):
        path.current()


def test_unknown_fields_reject_ingests_declared_schema_and_rejects_undeclared_keys(backend):
    schema = {"unknown_fields": "reject", "fields": [{"name": "created_at", "type": "datetime"}, {"name": "source", "type": "string"}]}
    path, backend = write_path(config(metadata_schema=schema), backend)
    manifest = path.ingest(documents())  # engine-injected identity keys (id, source, dataset, kind) are not "unknown"
    assert manifest["documents"] == 3 and manifest["projections"]["records"]["verified"]
    gen, _ = path.current()
    assert backend.records.count(gen) == 3
    with pytest.raises(IngestError, match="unknown_field") as info:
        path.ingest([Document(id="stray", dataset="demo", fields={"body": "x"}, metadata={"colour": "blue"})])
    assert info.value.failure["category"] == "metadata.unknown_field" and info.value.failure["unit"] == "stray"
    assert path.current()[1]["generation"] == manifest["generation"]
    undeclared_source = {"unknown_fields": "reject", "fields": [{"name": "created_at", "type": "datetime"}]}
    other, _ = write_path(config(metadata_schema=undeclared_source), MemoryBackend())
    assert other.ingest(documents())["documents"] == 3  # source undeclared: still an identity key, not a violation


def test_metadata_violations_fail_closed_by_category():
    cfg = config(metadata_schema={"fields": [{"name": "created_at", "type": "datetime", "timezone_policy": "require_offset"}]})
    path, _ = write_path(cfg)
    with pytest.raises(IngestError, match="created_at") as info:
        path.ingest([Document(id="d", dataset="demo", fields={"body": "x"}, metadata={"created_at": "2025-01-01T00:00:00"})])
    assert info.value.failure["category"] == "metadata.timezone" and info.value.failure["unit"] == "d"


def test_update_delete_and_reuse(backend):
    cfg = config()
    path, backend = write_path(cfg, backend)
    first = path.ingest(documents())
    updated = documents()[0].model_copy(update={"title": "Unrelated bookkeeping", "fields": {"details": "Archive entry with a unique marker."}})
    second = path.ingest([updated], delete_ids=["case:certificate"])
    assert second["generation"] != first["generation"] and second["documents"] == 2
    assert second["reused_extractions"] > 0  # unchanged chunks came back from the ledger
    gen, _ = path.current()
    assert backend.catalog.count_documents(gen) == 2
    assert path.registry.manifest(first["generation"]) is not None  # old generation kept for rollback
    with pytest.raises(ValueError, match="cannot upsert and delete"):
        path.ingest([updated], delete_ids=[updated.id])


def test_fingerprint_mismatch_refuses_unless_rebuild():
    cfg = config()
    backend = MemoryBackend()
    path, _ = write_path(cfg, backend)
    path.ingest(documents())
    drifted = config(models={"roles": {"embedder": {"provider": "hash", "dimensions": 64}}})
    other, _ = write_path(drifted, backend)
    with pytest.raises(ValueError, match="fingerprint"):
        other.current()
    with pytest.raises(ValueError, match="fingerprint"):
        other.ingest(documents())
    assert other.ingest(documents(), replace=True, rebuild=True)["documents"] == 3
    assert other.current()[0].fingerprint == other.index_fingerprint()


# --- policies (§3.14.1) --------------------------------------------------------------------------

BAD_QUOTE = json.dumps({"entities": [{"name": "fabricated", "type": "topic", "quote": "fabricated"}], "relations": []})
GOOD = json.dumps({"entities": [{"name": "CloudTax", "type": "product", "quote": "CloudTax"}], "relations": []})


def llm_config(policy="strict"):
    return config(extraction={"extractors": ["llm"], "labels": ["product", "topic"], "policy": policy}, models={"roles": {"embedder": {"provider": "hash", "dimensions": 32}, "extractor": {"provider": "openai_compatible", "model": "fake"}}})


def test_strict_aborts_on_first_rejected_unit_and_keeps_old_generation():
    model = FakeModel(lambda payload: BAD_QUOTE if "expired" in payload["passage"] else GOOD)
    path, backend = write_path(llm_config("strict"), extractor_model=model)
    with pytest.raises(IngestError, match="not grounded") as info:
        path.ingest(documents())
    assert info.value.failure["category"] == "grounding" and info.value.failure["stage"] == "extraction"
    assert path.registry.current() is None and backend.generations() == []
    raw = [backend.ledger.get_raw(k) for k in list(backend._state.raw)]
    assert any(r.key.startswith("raw:") and r.valid for r in raw)  # raw replies are kept even for the failed build


def test_quarantine_excludes_rejected_document_from_graph_and_records_coverage():
    model = FakeModel(lambda payload: BAD_QUOTE if "expired" in payload["passage"] else GOOD)
    path, backend = write_path(llm_config("quarantine"), extractor_model=model)
    manifest = path.ingest(documents())
    coverage = manifest["coverage"]
    assert manifest["policy"] == "quarantine" and manifest["status"] == "complete"
    assert coverage["extraction"] == {"accepted": 2, "rejected_by_category": {"grounding": 1}, "documents_without_graph": ["case:certificate"]}
    gen, _ = path.current()
    status = path.status()["documents"]["case:certificate"]
    assert status["extraction"] == "rejected:grounding" and status["graph"] == "excluded" and status["entities"] == 0
    assert status["lexical"] == "indexed" and status["dense"] == "indexed" and status["structured"] == "canonical_record"
    assert backend.graph.mentions(gen, [entity_key("product", "CloudTax")], None, 50)
    assert all(m.span.document_id != "case:certificate" for m in backend.graph.mentions(gen, [entity_key("product", "CloudTax")], None, 50))
    assert backend.catalog.get(gen, ["case:certificate"])  # still in the catalog
    assert all("case:certificate" not in c.document_ids for c in backend.graph.communities(gen))


def test_quarantine_schema_failure_keeps_invalid_raw_reply():
    model = FakeModel(lambda payload: "{not json" if "expired" in payload["passage"] else GOOD)
    path, backend = write_path(llm_config("quarantine"), extractor_model=model)
    manifest = path.ingest(documents())
    assert manifest["coverage"]["extraction"]["rejected_by_category"] == {"schema": 1}
    assert any(r.valid is False and r.raw_reply == "{not json" for r in backend._state.raw.values())


def test_partial_refuses_activation_without_flag_and_marks_stage_incomplete():
    calls = {"n": 0}

    def reply(payload):
        calls["n"] += 1
        return GOOD if calls["n"] < 3 else RuntimeError("provider HTTP 503")

    model = FakeModel(reply)
    path, backend = write_path(llm_config("partial"), extractor_model=model)
    with pytest.raises(IngestError, match="incomplete") as info:
        path.ingest(documents())
    assert info.value.failure["category"] == "incomplete_stage" and info.value.failure["stage"] == "extraction"
    assert path.registry.current() is None
    calls["n"] = 0
    manifest = path.ingest(documents(), allow_partial=True)
    stage = manifest["coverage"]["stages"]["extraction"]
    assert manifest["status"] == "partial" and manifest["coverage"]["incomplete"] is True
    # the two chunks extracted during the refused build replay from the ledger without inference
    assert manifest["reused_extractions"] == 2 and model.calls == 6
    assert stage["status"] == "incomplete" and stage["processed"] == 4 and stage["remaining"] == 2 and stage["first_unprocessed"]
    assert "provider" in stage["reason"]
    assert backend.ledger.status(manifest["generation"]) == "partial"
    # Documents the stopped stage never reached are reported explicitly, never as accepted (review B3).
    assert path.status()["documents"]["kb:rounding"]["extraction"] == "not_processed:extraction"
    assert path.status()["documents"]["kb:rounding"]["graph"] == "incomplete"
    assert path.status()["documents"]["case:certificate"]["extraction"] == "accepted"
    assert stage["documents_not_processed"] == ["kb:rounding"]
    assert "kb:rounding" in manifest["coverage"]["extraction"]["documents_without_graph"]
    assert manifest["coverage"]["extraction"]["accepted"] == 2


def test_partial_embedding_outage_leaves_vector_projection_empty_and_flagged(monkeypatch):
    path, backend = write_path(config(extraction={"policy": "partial"}))
    monkeypatch.setattr(Embedder, "encode", lambda self, texts, query=False: (_ for _ in ()).throw(RuntimeError("embedding provider down")))
    manifest = path.ingest(documents(), allow_partial=True)
    assert manifest["coverage"]["stages"]["embeddings"]["status"] == "incomplete"
    assert manifest["projections"]["vector"] == {"backend": "local", "rows": 0, "verified": False, "incomplete": True}
    assert path.status()["documents"]["kb:rounding"]["dense"] == "incomplete"


def test_strict_never_absorbs_budget_or_provider_failures():
    policy = ExtractionPolicy("quarantine")
    with pytest.raises(PolicyViolation):
        policy.reject_document("d", "budget", "c", "LLM call budget exhausted")
    with pytest.raises(PolicyViolation):
        ExtractionPolicy("quarantine").stage_failed("extraction", "provider", "down", 0, 1, "c")
    policy.reject_document("d", "grounding", "c", "bad quote")
    assert policy.document_status("d") == "rejected:grounding" and policy.coverage.extraction.documents_without_graph == ["d"]


def test_budget_exhaustion_is_a_stage_failure_under_partial():
    model = FakeModel(GOOD)
    cfg = llm_config("partial")
    ingest = cfg.budgets.ingest.model_copy(update={"max_llm_calls": 2})  # the whole-build cap, not the per-request one (QA-06)
    cfg = cfg.model_copy(update={"budgets": cfg.budgets.model_copy(update={"ingest": ingest})})
    path, _ = write_path(cfg, extractor_model=model)
    manifest = path.ingest(documents(), allow_partial=True)
    assert "budget" in manifest["coverage"]["stages"]["extraction"]["reason"] and manifest["llm_calls"] and len(manifest["llm_calls"]) == 2


# --- community reports ---------------------------------------------------------------------------


def community_config(policy="strict"):
    return config(extraction={"policy": policy}, models={"roles": {"embedder": {"provider": "hash", "dimensions": 32}, "community": {"provider": "openai_compatible", "model": "fake", "evidence_budget": {"chars": 500}}}})


def test_community_report_grounded_and_partial_coverage():
    def summary(payload):
        passage = payload["passages"][0]
        return json.dumps({"claims": [{"text": "Synthetic topic", "evidence": [{"document_id": passage["document_id"], "chunk_id": passage["id"], "quote": passage["text"]}]}]})

    path, backend = write_path(community_config(), community_model=FakeModel(summary))
    manifest = path.ingest(documents())
    gen, _ = path.current()
    reports = [c.report for c in backend.graph.communities(gen)]
    assert manifest["communities"] == len(reports) and any(r["status"] in {"partial", "complete_input"} and r["claims"] for r in reports)
    assert manifest["coverage"]["stages"]["community_reports"]["status"] == "complete"


def test_community_report_bad_quote_aborts_under_strict_and_is_rejected_under_quarantine():
    bad = json.dumps({"claims": [{"text": "Unsupported", "evidence": [{"document_id": "fake", "chunk_id": "fake", "quote": "fabricated"}]}]})
    path, backend = write_path(community_config("strict"), community_model=FakeModel(bad))
    with pytest.raises(IngestError, match="not grounded") as info:
        path.ingest(documents())
    assert info.value.failure["category"] == "report_grounding" and path.registry.current() is None
    path, backend = write_path(community_config("quarantine"), community_model=FakeModel(bad))
    manifest = path.ingest(documents())
    gen, _ = path.current()
    assert all(c.report["status"] == "rejected" for c in backend.graph.communities(gen))
    assert manifest["coverage"]["extraction"]["rejected_by_category"]["report_grounding"] == manifest["communities"]


# --- dedupe and relocated grounding (S1 follow-ups) ---------------------------------------------


def test_five_inherited_title_rows_collapse_to_one_co_occurrence_with_five_occurrences():
    title = "Austria at the 1988 Winter Olympics"
    reply = json.dumps({
        "entities": [{"name": "Austria", "type": "country", "quote": "Austria"}, {"name": "1988 Winter Olympics", "type": "event", "quote": "1988 Winter Olympics"}],
        "relations": [{"subject": "Austria", "predicate": "hosted", "object": "1988 Winter Olympics", "quote": title}],
    })
    cfg = config(extraction={"extractors": ["llm"], "labels": ["country", "event"]}, models={"roles": {"embedder": {"provider": "hash", "dimensions": 32}, "extractor": {"provider": "openai_compatible", "model": "fake"}}})
    path, backend = write_path(cfg, extractor_model=FakeModel(reply))
    manifest = path.ingest([Document(id=f"row{i}", dataset="demo", fields={"body": title}) for i in range(5)])
    assert manifest["relations"] == 0 and manifest["co_occurrence_relations"] == 1 and manifest["co_occurrence_occurrences"] == 5
    gen, _ = path.current()
    stored = backend._state.gens[gen.prefix()].relations
    assert len(stored) == 1 and stored[0].edge_kind == "co_occurrence" and stored[0].support == "unchecked"
    assert sorted(o.span.document_id for o in stored[0].occurrences) == [f"row{i}" for i in range(5)]
    assert all(o.span.text == title for o in stored[0].occurrences)
    from su_multi_arm.core.types import TraversalSpec

    assert backend.graph.traverse(gen, TraversalSpec(seed_entity_ids=[entity_key("country", "Austria")], hops=1), None) == []


def test_dedupe_keeps_distinct_span_text_apart():
    from su_multi_arm.core.types import Provenance, Relation, Span

    def rel(doc, text):
        span = Span(document_id=doc, chunk_id=doc, field="body", start=0, end=len(text), text=text, match_level="exact")
        return Relation(subject_id="a", predicate="uses", object_id="b", span=span, edge_kind="fact", support="trigger_in_span", provenance=Provenance(extractor="t"))

    merged = dedupe_relations([rel("d1", "A uses B"), rel("d2", "A uses B"), rel("d3", "A uses B now")])
    assert [len(r.occurrences) for r in merged] == [2, 1] and merged[0].span.document_id == "d1"


def test_relocated_quote_is_grounded_in_another_field_of_the_same_record():
    reply = json.dumps({
        "entities": [{"name": "Alpha", "type": "product", "quote": "Alpha"}, {"name": "Beta", "type": "component", "quote": "Beta"}],
        "relations": [{"subject": "Alpha", "predicate": "uses", "object": "Beta", "quote": "Alpha uses Beta"}],
    })
    cfg = llm_config()
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={"extractor": FakeModel(reply)}))
    doc = Document(id="r", dataset="demo", title="Alpha notes", fields={"summary": "Alpha and Beta are parts.", "details": "Alpha uses Beta in production."})
    chunks = chunk_document(doc, 1600, 100, [])
    summary = next(c for c in chunks if c.field == "summary")
    result = extractor.extract(ExtractionInput(chunk=summary, document=doc, chunks=chunks, budget=CallBudget(3)))
    relation = result.output.relations[0]
    assert relation.span.match_level == "relocated" and relation.span.field == "details" and relation.span.text == "Alpha uses Beta"
    assert doc.fields["details"][relation.span.start : relation.span.end] == relation.span.text
    assert relation.edge_kind == "fact"
    ends = {e.id: e for e in result.output.entities}
    assert ends[relation.subject_id].span.chunk_id == relation.span.chunk_id and relation.span.contains(ends[relation.subject_id].span)
    assert {e.span.match_level for e in result.output.entities} == {"exact", "relocated"}
    # a quote found nowhere in the record still fails closed
    missing = json.dumps({"entities": [{"name": "Gamma", "type": "product", "quote": "Gamma"}], "relations": []})
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={"extractor": FakeModel(missing)}))
    from su_multi_arm.extraction.base import ExtractionFailure

    with pytest.raises(ExtractionFailure, match="grounding"):
        extractor.extract(ExtractionInput(chunk=summary, document=doc, chunks=chunks, budget=CallBudget(3)))


def test_locate_in_record_levels_and_ambiguity():
    from su_multi_arm.extraction.grounding import locate_in_record

    fields = {"body": "The AACTA award", "notes": "the aacta award again", "other": "AACTA award"}
    assert locate_in_record("AACTA award", "body", fields, ["casefold"])[1].match_level == "exact"
    assert locate_in_record("the aacta award", "body", fields, ["casefold"])[1].match_level == "normalised"
    assert locate_in_record("award again", "body", fields, ["casefold"]) == ("notes", locate_in_record("award again", "body", fields, ["casefold"])[1])
    assert locate_in_record("award again", "body", fields, ["casefold"])[1].match_level == "relocated"
    assert locate_in_record("AACTA", "missing", fields, []) is None  # found in three fields: ambiguous
    assert locate_in_record("AACTA", "missing", fields, [], "first")[0] == "body"


def test_sqlite_layout_is_a_superset_of_the_v1_catalog(tmp_path):
    import sqlite3

    backend = SqliteBackend(tmp_path / "state")
    path, _ = write_path(config(), backend)
    manifest = path.ingest(documents())
    folder = tmp_path / "state" / "snapshots" / manifest["generation"]
    assert {p.name for p in folder.iterdir()} >= {"catalog.sqlite", "vectors.npy", "chunk_ids.json", "manifest.json"}
    assert (tmp_path / "state" / "CURRENT").read_text() == manifest["generation"]
    db = sqlite3.connect(folder / "catalog.sqlite")
    columns = {row[1] for row in db.execute("PRAGMA table_info(entities)")}
    assert {"key", "name", "kind", "document_id", "chunk_id", "quote", "start", "end", "match_level"} <= columns
    assert db.execute("SELECT count(*) FROM relations").fetchone()[0] == 0
    for chunk in db.execute("SELECT document_id, field, start, end, text FROM chunks"):
        doc = next(d for d in documents() if d.id == chunk[0])
        full = doc.title if chunk[1] == "title" else doc.fields[chunk[1]]
        assert full[chunk[2] : chunk[3]] == chunk[4]
    vectors = np.load(folder / "vectors.npy", allow_pickle=False)
    assert vectors.shape == (manifest["chunks"], 32)
    db.close()


def test_json_model_call_text_returns_raw_and_call_validates(monkeypatch):
    import httpx

    from su_multi_arm.core.config import ChatRole

    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"entities\": [], \"relations\": []}\n```"}, "finish_reason": "stop"}]}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    from su_multi_arm.models import Extraction

    model = JsonModel(ChatRole(provider="openai_compatible", model="fake"))
    budget = CallBudget(2)
    assert model.call_text("s", {}, Extraction, budget, "extraction") == '{"entities": [], "relations": []}'
    assert model.call("s", {}, Extraction, budget, "extraction") == Extraction()
    assert all(c["success"] for c in budget.calls)
