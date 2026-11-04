"""Multi-backend write path (plan §3.14, Phase 1 row "Multi-backend write path", §2.1 principle 1)
with in-process memory + sqlite backends, so it runs on every push:

* mixed placements through ``Engine.ingest``/``search``/``answer`` give the same arm results as a
  single-backend reference of the same kinds (scope mirrors make predicate pushdown work on the
  secondary backends);
* the manifest lists every backend's generation identity, role, activation mechanism and lexical
  analyzer id; every target projection is validated;
* activation order (secondaries first, authoritative ``CURRENT`` last) and compensation when any
  step fails, including a failed compensation;
* readers pin one generation and refuse a disagreement with ``GenerationDisagreement``;
* ``delete_generation`` across backends refuses the active/protected generation;
* ledger/session/cache backends take no part in generation scoping.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.backends import extras
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError, GenerationDisagreement, GenerationInUse
from su_multi_arm.core.types import GenerationId
from su_multi_arm.ingestion.generations import GenerationSet, placement
from su_multi_arm.ingestion.write import IngestError

STORAGE = ("catalog", "records", "ledger", "sessions", "cache", "communities")
BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in STORAGE},
    "metadata_schema": {"fields": [{"name": "product", "type": "string", "groupable": True}]},
    "pipeline": {
        "arms": {
            "dense": {"backend": "local"},
            "lexical": {"backend": "local"},
            "structured": {"backend": "local"},
            "graph": {"backend": "local"},
            "community": {"backend": "local"},
        }
    },
    "extraction": {"extractors": ["gazetteer"], "gazetteer": {"topic": ["exemption certificate", "rounding"], "product": ["CloudTax", "SearchDesk"]}},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32}}},
}

DOCS = [
    Document(id="kb:certificate", dataset="demo", source="article", title="Add an exemption certificate", fields={"details": "Open the record and add the exemption certificate. CloudTax rounding applies per line."}, metadata={"product": "CloudTax"}),
    Document(id="case:certificate", dataset="demo", source="case", title="Certificate expired", fields={"description": "An exemption certificate stopped applying in CloudTax."}, metadata={"product": "CloudTax"}),
    Document(id="kb:rounding", dataset="demo", source="article", title="Rounding configuration", fields={"details": "CloudTax rounding applies per invoice line."}, metadata={"product": "CloudTax"}),
    Document(id="kb:source", dataset="demo", source="manual", title="Create a content source", fields={"body": "In SearchDesk a content source connects a repository to the index."}, metadata={"product": "SearchDesk"}),
]
QUERIES = ("exemption certificate", "CloudTax rounding", "content source")
ARMS = ("dense", "lexical", "graph", "community")


def data(**backends) -> dict:
    out = json.loads(json.dumps(BASE))
    out["backends"] = {name: spec for name, spec in backends.items()}
    return out


def sqlite(path) -> dict:
    return {"kind": "sqlite", "path": str(path)}


def mixed_memory(tmp_path) -> dict:
    """Catalog + records on sqlite; every search projection on its own memory backend; runtime separate."""
    d = data(local=sqlite(tmp_path / "local"), lex={"kind": "memory"}, vec={"kind": "memory"}, graph={"kind": "memory"}, runtime={"kind": "memory"})
    d["storage"].update({"ledger": "runtime", "sessions": "runtime", "cache": "runtime", "communities": "graph"})
    arms = d["pipeline"]["arms"]
    arms["lexical"]["backend"], arms["dense"]["backend"] = "lex", "vec"
    arms["graph"]["backend"] = arms["community"]["backend"] = "graph"
    return d


def mixed_sqlite(tmp_path) -> dict:
    """Catalog on memory; lexical + dense on one sqlite, graph + communities on another sqlite."""
    d = data(local={"kind": "memory"}, search=sqlite(tmp_path / "search"), graph=sqlite(tmp_path / "graph"))
    d["storage"]["communities"] = "graph"
    arms = d["pipeline"]["arms"]
    arms["lexical"]["backend"] = arms["dense"]["backend"] = "search"
    arms["graph"]["backend"] = arms["community"]["backend"] = "graph"
    return d


def reference(kind: str, tmp_path) -> Engine:
    d = data(local={"kind": "memory"} if kind == "memory" else sqlite(tmp_path / "reference"))
    engine = Engine(validate_config(d))
    engine.ingest(DOCS)
    return engine


def arm_view(result) -> dict:
    return {arm: [(h.document_id, h.chunk_ids, h.provenance) for h in result.arm_hits.get(arm, [])] for arm in ARMS}


@pytest.mark.parametrize("build, search_kind", [(mixed_memory, "memory"), (mixed_sqlite, "sqlite")])
def test_mixed_placement_matches_a_single_backend_reference(build, search_kind, tmp_path):
    engine = Engine(validate_config(build(tmp_path)))
    manifest = engine.ingest(DOCS)
    ref = reference(search_kind, tmp_path)
    assert manifest["status"] == "complete"
    assert all(entry["verified"] for entry in manifest["projections"].values())
    for query in QUERIES:
        mine, theirs = engine.search(query), ref.search(query)
        assert mine.generation == manifest["generation"]
        assert arm_view(mine) == arm_view(theirs), query
        # request filters are predicates pushed down on the secondary backends via their scope mirror
        request = SearchRequest(query=query, filters={"source": "case"})
        filtered, expected = engine.search(request), ref.search(request)
        assert arm_view(filtered) == arm_view(expected), query
        assert all(h.source == "case" for h in filtered.hits)
    answer = engine.answer("exemption certificate")
    assert answer.retrieval.generation == manifest["generation"]
    assert "kb:certificate" in {h.document_id for h in answer.retrieval.hits}
    aggregate = engine.search(SearchRequest(query="x", aggregate={"operation": "group_count", "field": "product"}))
    assert aggregate.aggregates[0]["groups"] == ref.search(SearchRequest(query="x", aggregate={"operation": "group_count", "field": "product"})).aggregates[0]["groups"]


def test_manifest_lists_every_backend_generation_identity_and_analyzer(tmp_path):
    engine = Engine(validate_config(mixed_memory(tmp_path)))
    manifest = engine.ingest(DOCS)
    gen = manifest["generation"]
    generations = manifest["generations"]
    assert generations["activation_order"] == ["vec", "lex", "graph", "local"]  # derived first, catalog last
    backends = generations["backends"]
    assert set(backends) == {"vec", "lex", "graph", "local"}  # the runtime backend is not generation-scoped
    local = backends["local"]
    assert local["role"] == "authoritative" and local["activation"] == "current" and local["kind"] == "sqlite"
    assert set(local["projections"]) == {"catalog", "records"} and local["identity"] == {"snapshot": f"snapshots/{gen}"}
    assert backends["lex"] == {
        "kind": "memory",
        "projections": ["lexical"],
        "role": "secondary",
        "scope_mirror": True,
        "activation": "registry",
        "generation": gen,
        "key": f"demo/{gen}/",
        "identity": {"key": f"demo/{gen}/"},
        "analyzer_id": "python_word",
    }
    assert backends["graph"]["projections"] == ["graph", "communities"] and "analyzer_id" not in backends["graph"]
    assert all(entry["generation"] == gen for entry in backends.values())
    projections = manifest["projections"]
    assert projections["lexical"]["analyzer_id"] == "python_word" and projections["lexical"]["backend"] == "lex"
    for name in ("vec", "lex", "graph"):
        assert projections[f"scope_mirror:{name}"] == {"backend": name, "documents": 4, "chunks": manifest["chunks"], "verified": True}
    assert projections["graph"]["backend"] == "graph" and projections["communities"]["backend"] == "graph"
    # each secondary registry names the generation and holds the same manifest
    for name in ("vec", "lex", "graph"):
        registry = engine.backends[name].generation_registry()
        assert registry.current_generation() == gen and registry.manifest(gen)["generations"] == generations
    assert engine.backends["runtime"].generations() == []  # ledger/session/cache: no generation data


def test_sqlite_lexical_analyzer_is_recorded(tmp_path):
    engine = Engine(validate_config(mixed_sqlite(tmp_path)))
    manifest = engine.ingest(DOCS)
    assert manifest["generations"]["backends"]["search"]["analyzer_id"] == "fts5_unicode61"
    assert manifest["projections"]["lexical"]["analyzer_id"] == "fts5_unicode61"
    assert (tmp_path / "search" / "CURRENT").read_text() == manifest["generation"]


def test_records_go_to_storage_and_to_the_structured_arm_backend(tmp_path):
    d = data(local={"kind": "memory"}, rel=sqlite(tmp_path / "rel"))
    d["pipeline"]["arms"]["structured"]["backend"] = "rel"
    engine = Engine(validate_config(d))
    manifest = engine.ingest(DOCS)
    assert manifest["projections"]["records"] == {"backend": "local", "rows": 4, "verified": True}
    assert manifest["projections"]["records:rel"] == {"backend": "rel", "rows": 4, "verified": True}
    gen = engine.write_path.pin()[0]
    assert engine.backends["rel"].records.count(gen) == 4
    assert engine.search(SearchRequest(query="x", aggregate={"operation": "count"})).aggregates[0]["value"] == 4


# --- activation order and compensation --------------------------------------------------------


class PointerMemory(MemoryBackend):
    """A memory backend with its own persisted pointer (like neo4j's pointer node or the Lucene
    aliases) and injectable failures."""

    kind = "pointer_memory"

    def __init__(self, log: list):
        super().__init__()
        self.pointer: dict[str, str] = {}
        self.log = log
        self.fail_activate = False

    def activate_generation(self, gen):
        if self.fail_activate:
            raise OSError(f"injected activation failure on {self.name}")
        self.pointer[gen.dataset] = gen.generation
        self.log.append((self.name, gen.generation))

    def deactivate_generation(self, gen):
        if self.pointer.get(gen.dataset) == gen.generation:
            del self.pointer[gen.dataset]
        self.log.append((self.name, None))

    def active_generation(self, dataset):
        return self.pointer.get(dataset)

    def delete_generation(self, gen):  # like neo4j/opensearch: the generation the pointer names is refused
        if self.pointer.get(gen.dataset) == gen.generation:
            from su_multi_arm.core.errors import BackendError

            raise BackendError(self.kind, "delete_generation", "generation is the active one (pointer)")
        self.catalog.delete_generation(gen)


def pointer_engine(tmp_path, monkeypatch):
    """Catalog on sqlite; vector, lexical and graph each on a pointer-bearing memory backend."""
    log: list = []
    built = {"local": SqliteBackend(tmp_path / "local")}
    for name in ("vec", "lex", "graph"):
        built[name] = PointerMemory(log)
        built[name].name = name
    built["runtime"] = MemoryBackend()
    monkeypatch.setattr(Engine, "_build_backends", lambda self: dict(built))
    engine = Engine(validate_config(mixed_memory(tmp_path)))
    return engine, built, log


def pointers(built) -> dict:
    return {name: built[name].pointer.get("demo") for name in ("vec", "lex", "graph")}


def test_activation_order_is_derived_first_catalog_last(tmp_path, monkeypatch):
    engine, built, log = pointer_engine(tmp_path, monkeypatch)
    order = []
    registry = engine.write_path.registry
    original = registry.activate
    monkeypatch.setattr(registry, "activate", lambda manifest, **kw: (order.append(("local", [*log])), original(manifest, **kw))[1])
    manifest = engine.ingest(DOCS)
    gen = manifest["generation"]
    assert [name for name, _ in log] == ["vec", "lex", "graph"]
    assert order == [("local", [("vec", gen), ("lex", gen), ("graph", gen)])]  # CURRENT moved after every pointer
    assert manifest["generations"]["backends"]["graph"]["activation"] == "pointer"
    assert pointers(built) == {"vec": gen, "lex": gen, "graph": gen}


@pytest.mark.parametrize("failing", ["lex", "graph", "local"])
def test_activation_failure_rolls_back_switched_backends_and_keeps_current(failing, tmp_path, monkeypatch):
    engine, built, log = pointer_engine(tmp_path, monkeypatch)
    first = engine.ingest(DOCS)["generation"]
    if failing == "local":
        monkeypatch.setattr(engine.write_path.registry, "activate", lambda manifest, **kw: (_ for _ in ()).throw(OSError("injected CURRENT failure")))
    else:
        built[failing].fail_activate = True
    with pytest.raises(IngestError, match="activation failed") as info:
        engine.ingest(DOCS[:2], replace=True)
    failure = info.value.failure
    assert failure["category"] == f"activation:{failing}" and failure["stage"] == "activate"
    moved_before_failure = {"lex": ["vec"], "graph": ["vec", "lex"], "local": ["vec", "lex", "graph"]}[failing]
    assert [c["backend"] for c in failure["compensation"]] == list(reversed(moved_before_failure))
    assert all(c["ok"] and c["restored_to"] == first for c in failure["compensation"])
    assert pointers(built) == {"vec": first, "lex": first, "graph": first}
    assert engine.write_path.registry.current()["generation"] == first
    assert failure["left_for_cleanup"] == []
    staged = {(s["backend"], s["projection"]) for s in failure["staged"]}
    assert {("local", "catalog"), ("vec", "scope_mirror"), ("vec", "vector"), ("lex", "lexical"), ("graph", "graph"), ("graph", "communities")} <= staged
    failed = failure["generation"]
    for name in ("vec", "lex", "graph"):
        assert f"demo/{failed}/" not in built[name].generations()  # staged generation removed everywhere
    result = engine.search("exemption certificate")
    assert result.generation == first and "kb:certificate" in {h.document_id for h in result.hits}  # the old corpus is served


def test_first_build_activation_failure_removes_every_moved_pointer(tmp_path, monkeypatch):
    engine, built, log = pointer_engine(tmp_path, monkeypatch)
    built["graph"].fail_activate = True
    with pytest.raises(IngestError):
        engine.ingest(DOCS)
    assert pointers(built) == {"vec": None, "lex": None, "graph": None}
    assert engine.write_path.registry.current() is None


def test_failed_compensation_is_recorded_and_readers_are_refused(tmp_path, monkeypatch):
    engine, built, log = pointer_engine(tmp_path, monkeypatch)
    first = engine.ingest(DOCS)["generation"]
    built["graph"].fail_activate = True

    def refuse_restore(gen):
        raise OSError("injected restore failure")

    original = built["vec"].activate_generation
    monkeypatch.setattr(built["vec"], "activate_generation", lambda gen: original(gen) if gen.generation != first else refuse_restore(gen))
    with pytest.raises(IngestError, match=r"compensation FAILED on \['vec'\]") as info:
        engine.ingest(DOCS, replace=True)
    failure = info.value.failure
    failed = failure["generation"]
    assert {c["backend"]: c["ok"] for c in failure["compensation"]} == {"lex": True, "vec": False}
    assert built["vec"].pointer["demo"] == failed  # still names the failed generation ...
    assert any(entry["backend"] == "vec" for entry in failure["left_for_cleanup"])  # ... so its cleanup is refused
    assert engine.write_path.registry.current()["generation"] == first
    with pytest.raises(GenerationDisagreement) as refused:
        engine.search("exemption certificate")
    assert refused.value.pointers["vec"] == failed and refused.value.authoritative == first
    with pytest.raises(GenerationDisagreement):
        engine.answer("exemption certificate")
    # the operator (or the next successful build) realigns the pointer; reads resume
    monkeypatch.undo()
    built["vec"].pointer["demo"] = first
    assert engine.search("exemption certificate").generation == first


# --- pinning ----------------------------------------------------------------------------------


def test_reader_refuses_when_a_secondary_registry_disagrees(tmp_path):
    engine = Engine(validate_config(mixed_memory(tmp_path)))
    first = engine.ingest(DOCS)["generation"]
    second = engine.ingest(DOCS[:3], replace=True)["generation"]
    lex = engine.backends["lex"].generation_registry()
    lex.activate(lex.manifest(first))  # someone switched one backend back by hand
    with pytest.raises(GenerationDisagreement, match="refusing a mixed-generation read") as info:
        engine.search("exemption certificate")
    assert info.value.pointers == {"vec": second, "lex": first, "graph": second}
    with pytest.raises(GenerationDisagreement):
        engine.answer("exemption certificate")
    lex.deactivate(first)  # a missing pointer is a disagreement too
    with pytest.raises(GenerationDisagreement):
        engine.search("exemption certificate")
    lex.activate(lex.manifest(second))
    assert engine.search("exemption certificate").generation == second


def test_pin_with_a_single_backend_reads_no_extra_pointer(tmp_path):
    engine = Engine(validate_config(data(local={"kind": "memory"})))
    engine.ingest(DOCS)
    assert engine.write_path.generations.secondaries == []
    assert engine.write_path.generations.pointers("demo") == {}


# --- clean-up ---------------------------------------------------------------------------------


def test_delete_generation_across_backends_refuses_active_and_protected(tmp_path, monkeypatch):
    engine, built, log = pointer_engine(tmp_path, monkeypatch)
    path = engine.write_path
    first = engine.ingest(DOCS)["generation"]
    second = engine.ingest(DOCS[:3], replace=True)["generation"]
    with pytest.raises(GenerationInUse, match="CURRENT"):
        path.delete_generation(second)
    with pytest.raises(ValueError, match="unknown generation"):
        path.delete_generation("0" * 32)
    # a secondary pointer naming the generation blocks deletion before anything is removed
    built["lex"].pointer["demo"] = first
    with pytest.raises(GenerationInUse, match=r"\['lex'\]"):
        path.delete_generation(first)
    built["lex"].pointer["demo"] = second
    # protected (pinned by a live session) on the first backend asked: refused, nothing deleted
    gen_first = GenerationId(dataset="demo", generation=first, index_format=3, fingerprint=path.index_fingerprint())
    built["vec"].protect(gen_first)
    with pytest.raises(GenerationInUse, match="protected"):
        path.delete_generation(first)
    assert all(f"demo/{first}/" in built[name].generations() for name in ("vec", "lex", "graph"))
    built["vec"].unprotect(gen_first)
    report = path.delete_generation(first)
    assert report == {"generation": first, "deleted": ["vec", "lex", "graph", "local"], "left_for_cleanup": []}
    for name in ("vec", "lex", "graph"):
        assert f"demo/{first}/" not in built[name].generations() and f"demo/{second}/" in built[name].generations()
    assert not (tmp_path / "local" / "snapshots" / first / "catalog.sqlite").exists()
    assert engine.search("exemption certificate").generation == second


# --- placement rules --------------------------------------------------------------------------


def test_placement_table(tmp_path):
    cfg = validate_config(mixed_memory(tmp_path))
    assert placement(cfg) == {
        "catalog": ["local"],
        "records": ["local"],
        "graph": ["graph"],
        "communities": ["graph"],
        "lexical": ["lex"],
        "vector": ["vec"],
    }
    d = data(local={"kind": "memory"}, g={"kind": "memory"}, c={"kind": "memory"})
    d["pipeline"]["arms"]["graph"]["backend"], d["pipeline"]["arms"]["community"]["backend"] = "g", "c"
    cfg = validate_config(d)
    assert placement(cfg)["graph"] == ["g", "c", "local"] and placement(cfg)["communities"] == ["local", "c"]


def test_search_projection_on_a_backend_without_catalog_is_refused(tmp_path):
    """A lexical/vector facet refuses chunks of documents it does not hold, so its backend must take
    the scope mirror; one that cannot is a ``ConfigError`` at construction, not a failed build."""

    class LexicalOnly:
        kind = "lexical_only"

        def __init__(self):
            self.lexical = MemoryBackend().lexical

    cfg = validate_config(mixed_memory(tmp_path))
    backends = {name: MemoryBackend() for name in cfg.backends}
    backends["lex"] = LexicalOnly()
    with pytest.raises(ConfigError, match="scope mirror"):
        GenerationSet(cfg, backends)


@pytest.mark.parametrize("kind", ["file", "memory", "sql"])
def test_registry_deactivate_only_removes_its_own_generation(kind, tmp_path):
    """Secondary registries are moved back with ``activate(previous)`` or, on a failed first release,
    ``deactivate(generation)``, which must never remove a pointer naming another generation."""
    if kind == "file":
        registry = SqliteBackend(tmp_path / "s").generation_registry()
    elif kind == "memory":
        registry = MemoryBackend().generation_registry()
    else:
        pytest.importorskip("sqlalchemy")
        from su_multi_arm.backends.sql import SqlBackend

        registry = SqlBackend(f"sqlite:///{tmp_path / 'r.db'}", table_prefix="t").generation_registry()
    a, b = {"generation": "a" * 32, "dataset": "demo"}, {"generation": "b" * 32, "dataset": "demo"}
    for manifest in (a, b):
        registry.write_manifest(manifest)
    registry.activate(a)
    registry.deactivate("b" * 32)
    assert registry.current_generation() == "a" * 32
    registry.deactivate("a" * 32)
    assert registry.current_generation() is None and registry.current() is None


def test_pointer_protocols_are_structural():
    assert isinstance(PointerMemory([]), extras.GenerationPointer)
    assert isinstance(MemoryBackend(), extras.GenerationIdentity) and not isinstance(MemoryBackend(), extras.GenerationPointer)
