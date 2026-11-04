"""Seams a separate graph backend (neo4j) relies on, exercised with in-memory fakes so they run on
every push: (1) the graph and community arms resolve scope predicates to a document-id allow-list
through the catalog for a graph facet that cannot evaluate predicates (capability flag
``predicate_scope: false``), with the same results as predicate pushdown and a typed error instead of
truncation above the cap; (2) the write path moves a backend's own generation pointer
(``extras.GenerationPointer``) only for a build that reaches activation, and moves it back when the
authoritative ``CURRENT`` switch fails."""

from __future__ import annotations

import json

import pytest

from su_multi_arm.arms import ArmInput, base as arm_base
from su_multi_arm.arms.community import CommunityArm
from su_multi_arm.arms.graph import GraphArm
from su_multi_arm.backends import extras
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Predicate, Scope
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.ingestion.write import IngestError, WritePath
from su_multi_arm.providers import Embedder
from su_multi_arm.runtime.context import new_context

BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}, "graph": {"kind": "memory"}},
    "storage": {**{k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache")}, "communities": "graph"},
    "metadata_schema": {"fields": [{"name": "product", "type": "string"}]},
    "pipeline": {"arms": {"lexical": {"backend": "local"}, "graph": {"backend": "graph"}, "community": {"backend": "graph"}}},
    "extraction": {"extractors": ["gazetteer"], "gazetteer": {"topic": ["exemption certificate", "rounding"], "product": ["CloudTax"]}},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}}},
}

DOCS = [
    {"id": "kb:certificate", "source": "article", "title": "Add an exemption certificate", "fields": {"details": "Add the exemption certificate. CloudTax rounding applies per line."}},
    {"id": "case:certificate", "source": "case", "title": "Certificate expired", "fields": {"description": "An exemption certificate stopped applying in CloudTax."}},
    {"id": "kb:rounding", "source": "article", "title": "Rounding configuration", "fields": {"details": "CloudTax rounding applies per invoice line."}},
]


class DocIdsOnlyGraph:
    """Wraps the memory graph facet and, like neo4j, refuses scope predicates."""

    def __init__(self, inner):
        self.inner, self.scopes = inner, []

    def _check(self, scope):
        self.scopes.append(scope)
        if scope is not None and scope.predicates:
            raise BackendError("docids_only", "scope", "predicates are not evaluated here")

    def upsert(self, gen, entities, relations, memberships):
        return self.inner.upsert(gen, entities, relations, memberships)

    def traverse(self, gen, spec, scope):
        self._check(scope)
        return self.inner.traverse(gen, spec, scope)

    def find_entities(self, gen, tokens, scope):
        self._check(scope)
        return self.inner.find_entities(gen, tokens, scope)

    def mentions(self, gen, entity_ids, scope, limit):
        self._check(scope)
        return self.inner.mentions(gen, entity_ids, scope, limit)

    def memberships(self, gen):
        return self.inner.memberships(gen)

    def put_communities(self, gen, communities):
        return self.inner.put_communities(gen, communities)

    def communities(self, gen):
        return self.inner.communities(gen)


class PointerGraphBackend(MemoryBackend):
    """A separate graph backend of a kind outside the matrix (so predicates are resolved), with its own
    generation pointer like neo4j's pointer node."""

    kind = "docids_only"

    def __init__(self):
        super().__init__()
        self.graph = DocIdsOnlyGraph(self.graph)
        self.pointer: dict[str, str] = {}
        self.moves: list[tuple[str, str]] = []

    def activate_generation(self, gen):
        self.pointer[gen.dataset] = gen.generation
        self.moves.append(("activate", gen.generation))

    def deactivate_generation(self, gen):
        if self.pointer.get(gen.dataset) == gen.generation:
            del self.pointer[gen.dataset]
        self.moves.append(("deactivate", gen.generation))

    def active_generation(self, dataset):
        return self.pointer.get(dataset)


def documents():
    from su_multi_arm.core.types import Document

    return [Document(dataset="demo", **d) for d in DOCS]


def build(graph_backend=None):
    cfg = validate_config(json.loads(json.dumps(BASE)))
    local, graph = MemoryBackend(), graph_backend or PointerGraphBackend()
    backends = {"local": local, "graph": graph}
    extractor = BuiltinExtractor(cfg.extraction, Dependencies())
    path = WritePath(cfg, backends, extractor=extractor, embedder=Embedder(cfg.models.roles.embedder))
    manifest = path.ingest(documents())
    getattr(graph.graph, "scopes", []).clear()  # build-time validation reads are not arm reads
    return cfg, path, backends, manifest


def arm(cls, cfg, backends, name):
    return cls(getattr(cfg.pipeline.arms, name), Dependencies(backends=backends, components={"catalog": backends["local"].catalog}, metadata_schema=cfg.metadata_schema))


def run(arm_obj, path, query, scope):
    gen, _ = path.current()
    return arm_obj.run(new_context(gen), ArmInput(query=query, scope=scope, candidates=50))


def test_pointer_protocol_is_structural():
    assert isinstance(PointerGraphBackend(), extras.GenerationPointer)
    assert not isinstance(MemoryBackend(), extras.GenerationPointer)


@pytest.mark.parametrize("name, cls", [("graph", GraphArm), ("community", CommunityArm)])
def test_predicates_are_resolved_to_document_ids_with_pushdown_parity(name, cls):
    cfg, path, backends, _ = build()
    resolved_arm = arm(cls, cfg, backends, name)
    # reference: one memory backend holding catalog and graph, predicates pushed down natively
    ref_data = json.loads(json.dumps(BASE))
    ref_data["backends"] = {"local": {"kind": "memory"}}
    ref_data["storage"]["communities"] = "local"
    ref_data["pipeline"]["arms"] = {"lexical": {"backend": "local"}, "graph": {"backend": "local"}, "community": {"backend": "local"}}
    ref_cfg, ref_local = validate_config(ref_data), MemoryBackend()
    ref_path = WritePath(ref_cfg, {"local": ref_local}, extractor=BuiltinExtractor(ref_cfg.extraction, Dependencies()), embedder=Embedder(ref_cfg.models.roles.embedder))
    ref_path.ingest(documents())
    native_arm = arm(cls, ref_cfg, {"local": ref_local}, name)
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="case")])
    for query in ("exemption certificate", "CloudTax rounding"):
        got, expected = run(resolved_arm, path, query, scope), run(native_arm, ref_path, query, scope)
        assert [h.document_id for h in got.hits] == [h.document_id for h in expected.hits] == ["case:certificate"]
    seen = backends["graph"].graph.scopes
    assert seen and all(s is not None and not s.predicates and s.document_ids == ["case:certificate"] for s in seen)


def test_unfiltered_scope_is_passed_through_untouched():
    cfg, path, backends, _ = build()
    graph_arm = arm(GraphArm, cfg, backends, "graph")
    scope = Scope(exclude_ids=["kb:rounding"])
    hits = run(graph_arm, path, "CloudTax rounding", scope).hits
    assert "kb:rounding" not in {h.document_id for h in hits} and hits
    assert all(s == scope for s in backends["graph"].graph.scopes)


def test_resolution_above_the_cap_is_a_typed_error(monkeypatch):
    cfg, path, backends, _ = build()
    monkeypatch.setattr(arm_base, "MAX_RESOLVED_SCOPE_DOCUMENTS", 1)
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="article")])
    with pytest.raises(BackendError, match="exceeds 1 documents"):
        run(arm(GraphArm, cfg, backends, "graph"), path, "CloudTax rounding", scope)


def test_pointer_moves_only_with_a_successful_activation(monkeypatch):
    cfg, path, backends, first = build()
    graph = backends["graph"]
    assert graph.pointer == {"demo": first["generation"]}
    docs = documents()
    # a failure before activation never touches the pointer
    monkeypatch.setattr(graph.graph, "upsert", lambda *a: (_ for _ in ()).throw(BackendError("docids_only", "upsert", "injected")))
    moves = len(graph.moves)
    with pytest.raises(IngestError, match="injected"):
        path.ingest(docs, replace=True)
    assert graph.pointer == {"demo": first["generation"]} and len(graph.moves) == moves
    monkeypatch.undo()
    # the authoritative switch fails after the pointer moved: it is moved back
    monkeypatch.setattr(path.registry, "activate", lambda manifest, **_: (_ for _ in ()).throw(OSError("injected CURRENT failure")))
    with pytest.raises(IngestError, match="CURRENT"):
        path.ingest(docs, replace=True)
    assert graph.pointer == {"demo": first["generation"]} and graph.moves[-1] == ("activate", first["generation"])
    assert path.current()[0].generation == first["generation"]
    monkeypatch.undo()
    second = path.ingest(docs, replace=True)
    assert graph.pointer == {"demo": second["generation"]}


def test_first_build_failure_removes_the_pointer(monkeypatch):
    cfg = validate_config(json.loads(json.dumps(BASE)))
    local, graph = MemoryBackend(), PointerGraphBackend()
    path = WritePath(cfg, {"local": local, "graph": graph}, extractor=BuiltinExtractor(cfg.extraction, Dependencies()), embedder=Embedder(cfg.models.roles.embedder))
    monkeypatch.setattr(path.registry, "activate", lambda manifest, **_: (_ for _ in ()).throw(OSError("injected CURRENT failure")))
    with pytest.raises(IngestError):
        path.ingest(documents())
    assert graph.pointer == {} and graph.moves[-1][0] == "deactivate"
