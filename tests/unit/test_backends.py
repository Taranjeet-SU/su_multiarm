"""Backend contracts + capability matrix (plan §3.5, frozen): the in-memory fake satisfies every
protocol; matrix consistency; generation scoping, paging, cache-key prefix, destructive guard."""

from __future__ import annotations

import numpy as np
import pytest

from su_multi_arm.backends import capabilities as caps
from su_multi_arm.backends.contracts import CONTRACT_NAMES
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import (
    Chunk,
    Document,
    Entity,
    FieldSpec,
    GenerationId,
    LedgerEntry,
    LexicalQuery,
    Measure,
    Membership,
    MetadataSchema,
    Page,
    Predicate,
    Provenance,
    Relation,
    Scope,
    ScopeSpec,
    SessionRecord,
    Span,
    StructuredRecord,
    TraversalSpec,
)

GEN = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="f" * 64)
GEN2 = GEN.model_copy(update={"generation": "b" * 32})
PROV = Provenance(extractor="test")
SCHEMA = MetadataSchema(
    fields=[
        FieldSpec(name="source", type="string", listable=True, groupable=True),
        FieldSpec(name="product", type="string", listable=True, casefold_match=True),
        FieldSpec(name="created_at", type="datetime", time_basis="created"),
        FieldSpec(name="size", type="integer"),
        FieldSpec(name="secret_id", type="id", listable=False),
        FieldSpec(name="note", type="string", filterable=False),
        FieldSpec(name="day", type="date"),
    ]
)


def doc(i: int, product: str = "CloudTax", source: str = "case") -> Document:
    return Document(id=f"d{i}", dataset="demo", source=source, metadata={"product": product, "created_at": f"2024-01-0{i}T00:00:00+00:00"})


def chunk(i: int, text: str) -> Chunk:
    return Chunk(id=f"c{i}", document_id=f"d{i}", field="body", start=0, end=len(text), text=text)


@pytest.fixture
def backend():
    b = MemoryBackend(clock=lambda: 1000.0)
    docs = [doc(1), doc(2, "SearchDesk"), doc(3, "CloudTax", "article")]
    chunks = [chunk(1, "exemption certificate rounding"), chunk(2, "content source rounding error"), chunk(3, "certificate exemption")]
    b.catalog.put_documents(GEN, docs, chunks)
    b.lexical.index(GEN, chunks, ["body"])
    b.lexical.refresh(GEN)
    return b


def test_fake_adapter_satisfies_every_frozen_protocol():
    adapter = MemoryBackend()
    assert caps.conformance(adapter) == set(caps.CAPABILITIES) == set(CONTRACT_NAMES)
    assert caps.conformance(adapter) == set(caps.adapter("memory").capabilities)
    assert caps.conformance(object()) == set()


def test_matrix_is_consistent_with_contracts_and_config_kinds():
    from su_multi_arm.core.config import BackendConfig
    from pydantic import TypeAdapter

    assert caps.check_matrix() == []
    assert set(caps.conformance(MemoryBackend())) == {c for c, d in caps.MATRIX["memory"].capabilities.items() if d.status == "contracts"}
    kinds = set(caps.MATRIX)
    schema = TypeAdapter(BackendConfig).json_schema()
    kind_props = [d["properties"]["kind"] for d in schema["$defs"].values() if "kind" in d.get("properties", {})]
    config_kinds = {k for prop in kind_props for k in prop.get("enum", [prop.get("const")])}
    assert config_kinds == kinds
    assert {k for k, a in caps.MATRIX.items() if a.status == "contracts"} == {"memory", "sqlite", "opensearch", "elasticsearch", "neo4j"}
    assert {k for k, a in caps.MATRIX.items() if a.status == "planned"} == set()
    # mongodb: every declared capability is on the contracts except the Atlas-only vector row, which stays planned.
    assert caps.MATRIX["mongodb"].status == "legacy" and caps.status_of("mongodb", "vector") == "planned"
    assert {c for c, d in caps.MATRIX["mongodb"].capabilities.items() if d.status == "contracts"} == {"catalog", "lexical", "records", "cache", "ledger", "session"}
    assert set(caps.MATRIX["sqlite"].capabilities) == set(caps.CAPABILITIES) and caps.MATRIX["sqlite"].status == "contracts"
    assert set(caps.MATRIX["neo4j"].capabilities) == {"graph"}
    assert caps.status_of("postgres", "graph") == "planned" and caps.declares("postgres", "vector")
    # postgres: everything but graph on the contracts; sqlalchemy: lexical/vector are dialect-specific and planned.
    assert {c for c, d in caps.MATRIX["postgres"].capabilities.items() if d.status == "contracts"} == {"catalog", "lexical", "vector", "records", "cache", "ledger", "session"}
    assert {c for c, d in caps.MATRIX["sqlalchemy"].capabilities.items() if d.status == "contracts"} == {"catalog", "records", "cache", "ledger", "session"}
    assert caps.status_of("sqlalchemy", "lexical") == "planned" and caps.status_of("sqlalchemy", "vector") == "planned"
    # Honest per-(adapter, capability) status: every served pair is listed here.
    served = {(k, c) for k, a in caps.MATRIX.items() for c, d in a.capabilities.items() if d.status != "planned"}
    mongo_served = {("mongodb", c) for c in ("catalog", "lexical", "records", "cache", "ledger", "session")}
    opensearch_served = {("opensearch", c) for c in ("catalog", "lexical", "vector", "records")}
    elasticsearch_served = {("elasticsearch", c) for c in ("catalog", "lexical", "vector", "records")}
    pg_served = {("postgres", c) for c in ("catalog", "lexical", "vector", "records", "cache", "ledger", "session")}
    sql_served = {("sqlalchemy", c) for c in ("catalog", "records", "cache", "ledger", "session")}
    assert served == (
        {("memory", c) for c in caps.CAPABILITIES} | {("sqlite", c) for c in caps.CAPABILITIES} | {("neo4j", "graph")}
        | mongo_served | opensearch_served | elasticsearch_served | pg_served | sql_served
    )
    assert set(caps.MATRIX["opensearch"].capabilities) == set(caps.MATRIX["elasticsearch"].capabilities) == {"catalog", "lexical", "vector", "records"}  # graph/cache/ledger/session: not served
    assert caps.serves("postgres", "vector") and caps.serves("postgres", "catalog") and not caps.serves("postgres", "graph")
    assert caps.serves("sqlite", "graph", "memberships") and caps.serves("neo4j", "graph", "memberships")
    assert caps.flags("neo4j", "graph")["predicate_scope"] is False and caps.flags("sqlite", "graph")["predicate_scope"] is True
    assert caps.status_of("neo4j", "catalog") is None
    assert caps.flags("sqlite", "lexical")["analyzer_id"] == "fts5_unicode61" and caps.flags("mongodb", "vector")["ann"] == "atlas_only"
    assert {c for c, _ in caps.ARM_CAPABILITY.values()} <= set(caps.CAPABILITIES)
    assert {c for c, _ in caps.STORAGE_CAPABILITY.values()} <= set(caps.CAPABILITIES)
    with pytest.raises(KeyError, match="unknown backend kind"):
        caps.adapter("redis")


def test_catalog_is_generation_scoped_pages_and_guards_delete(backend):
    assert [d.id for d in backend.catalog.get(GEN, ["d2", "zz", "d1"])] == ["d2", "d1"]
    assert [c.id for c in backend.catalog.get_chunks(GEN, ["c3", "zz", "c1"])] == ["c3", "c1"]
    assert backend.stats["catalog.get.ids"] == 3  # reads bounded by requested ids, never the corpus
    with pytest.raises(BackendError, match="unknown generation"):
        backend.catalog.get(GEN2, ["d1"])
    page1 = backend.catalog.iter_scope(GEN, None, Page(size=2))
    page2 = backend.catalog.iter_scope(GEN, None, Page(cursor=page1.next_cursor, size=2))
    assert [d.id for d in page1.items] == ["d1", "d2"] and [d.id for d in page2.items] == ["d3"] and page2.next_cursor is None
    scoped = backend.catalog.iter_scope(GEN, Scope(predicates=[Predicate(field="source", op="eq", value="case")], exclude_ids=["d1"]), Page())
    assert [d.id for d in scoped.items] == ["d2"]
    backend.protect(GEN)
    with pytest.raises(BackendError, match="active or pinned"):
        backend.catalog.delete_generation(GEN)
    backend.unprotect(GEN)
    backend.catalog.delete_generation(GEN)
    assert backend.generations() == []
    with pytest.raises(BackendError):
        backend.catalog.put_documents(GEN, [], [Chunk(id="c", document_id="ghost", field="body", start=0, end=1, text="x")])


def test_lexical_scope_pushdown_phrase_boolean_and_refresh_visibility(backend):
    hits = backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 10)
    assert {h.document_id for h in hits.items} == {"d1", "d3"} and hits.next_cursor is None
    scoped = backend.lexical.search(GEN, LexicalQuery(text="certificate"), Scope(predicates=[Predicate(field="product", op="eq", value="CloudTax")], exclude_ids=["d3"]), 10)
    assert [h.document_id for h in scoped.items] == ["d1"]
    phrase = backend.lexical.search(GEN, LexicalQuery(kind="phrase", text="exemption certificate"), None, 10)
    assert [h.document_id for h in phrase.items] == ["d1"]  # d3 has the tokens, not the contiguous phrase
    boolean = backend.lexical.search(GEN, LexicalQuery(kind="boolean", text="rounding", must=["rounding"], must_not=["error"]), None, 10)
    assert [h.document_id for h in boolean.items] == ["d1"]
    backend.lexical.index(GEN, [chunk(4, "certificate")], ["body"])
    assert {h.document_id for h in backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 10).items} == {"d1", "d3"}
    backend.catalog.put_documents(GEN, [doc(4)], [])
    backend.lexical.refresh(GEN)
    assert {h.document_id for h in backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 10).items} == {"d1", "d3", "d4"}
    assert len(backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 5000).items) == 3  # k above the page default is fine
    with pytest.raises(BackendError, match="invalid page"):
        backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 100000)
    paged = backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 10, Page(size=1))
    rest = backend.lexical.search(GEN, LexicalQuery(text="certificate"), None, 10, Page(cursor=paged.next_cursor, size=5))
    assert len(paged.items) == 1 and not {h.chunk_id for h in paged.items} & {h.chunk_id for h in rest.items}


def test_vector_search_is_exact_scoped_and_namespaced(backend):
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]], dtype=np.float32)
    backend.vector.upsert(GEN, ["c1", "c2", "c3"], vectors)
    assert backend.vector.search(GEN, np.array([1.0, 0.0]), None, 2) == []  # not visible before refresh
    backend.vector.refresh(GEN)
    hits = backend.vector.search(GEN, np.array([1.0, 0.0]), None, 2)
    assert [h.chunk_id for h in hits] == ["c1", "c3"] and hits[0].score == pytest.approx(1.0)
    scoped = backend.vector.search(GEN, np.array([1.0, 0.0]), Scope(document_ids=["d2", "d3"]), 2)
    assert [h.chunk_id for h in scoped] == ["c3", "c2"]  # d1 scoped out; exact cosine order kept
    assert backend.vector.search(GEN, np.array([1.0, 0.0]), None, 2, namespace="cache") == []
    assert backend.vector.search(GEN, np.zeros(2), None, 2) == []
    with pytest.raises(BackendError, match="dimension"):
        backend.vector.search(GEN, np.ones(3), None, 2)
    with pytest.raises(BackendError, match="duplicate"):
        backend.vector.upsert(GEN, ["x", "x"], np.ones((2, 2)))
    backend.vector.upsert(GEN, ["ghost"], np.ones((1, 2)))
    backend.vector.refresh(GEN)
    with pytest.raises(BackendError, match="not a catalogued chunk"):
        backend.vector.search(GEN, np.array([1.0, 1.0]), None, 5)


def test_record_store_typed_scope_exists_and_listable_values(backend):
    records = [
        StructuredRecord(schema_id="document", id="d1", values={"source": "case", "product": "CloudTax", "created_at": "2024-01-01T00:00:00+00:00", "size": 3, "secret_id": "s1"}, provenance=PROV),
        StructuredRecord(schema_id="document", id="d2", values={"source": "case", "product": "SearchDesk", "created_at": "2024-01-02T00:00:00+00:00", "size": 5, "secret_id": "s2"}, provenance=PROV),
        StructuredRecord(schema_id="document", id="d3", values={"source": "article", "product": "CloudTax", "created_at": None, "size": None, "secret_id": "s3"}, provenance=PROV),
    ]
    backend.records.upsert(GEN, records, SCHEMA)
    for bad in ({"created_at": 12}, {"created_at": "not a date"}, {"created_at": "2024-01-01T00:00:00"}):
        with pytest.raises(BackendError, match="created_at"):
            backend.records.upsert(GEN, [StructuredRecord(schema_id="document", id="bad", values=bad, provenance=PROV)], SCHEMA)
    assert backend.records.execute(GEN, ScopeSpec(), []).denominator == 3  # the rejected record was not stored
    strict = SCHEMA.model_copy(update={"unknown_fields": "reject"})
    with pytest.raises(BackendError, match="undeclared"):
        backend.records.upsert(GEN, [StructuredRecord(schema_id="document", id="bad", values={"zzz": 1}, provenance=PROV)], strict)
    backend.records.upsert(GEN, [], SCHEMA)
    # Predicates on undeclared or non-filterable fields are reported, never applied (fail closed on the result).
    for field in ("zzz", "note"):
        gated = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field=field, op="eq", value="x")]), [])
        assert gated.applied == [] and gated.unsupported[0].reason == "field_not_filterable" and gated.value == 3
    not_nullable = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="source", op="missing")]), [])
    assert not_nullable.applied and not_nullable.value == 0  # source is nullable: applied normally
    for bound, reason in (("2024-01-01T00:00:00", "timezone_missing"), ("yesterday", "unparseable_bound"), (5, "unparseable_bound")):
        bad_range = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="created_at", op="range", lower=bound)]), [])
        assert bad_range.unsupported[0].reason == reason and bad_range.applied == []
    ok_range = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="created_at", op="range", lower="2024-01-02T00:00:00+00:00")]), [])
    assert ok_range.matched_ids == ["d2"]
    assert backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="day", op="range", lower="2024-01-01")]), []).applied
    count = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="cloudtax")]), [])
    assert (count.value, count.denominator, count.exactness, count.matched_ids) == (2, 3, "exact", ["d1", "d3"])
    acl = [Predicate(field="source", op="eq", value="case")]
    scoped = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="CloudTax")]), acl)
    assert (scoped.value, scoped.denominator) == (1, 2)
    zero = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="Absent")]), [])
    assert zero.zero_match and zero.value == 0 and zero.applied
    first = backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="first_seen", field="created_at")), [])
    assert first.value["record_id"] == "d1" and first.missing_or_invalid == 1
    grouped = backend.records.execute(GEN, ScopeSpec(group_by=["source"]), [])
    assert grouped.value == {"case": 2, "article": 1} and [g.count for g in grouped.groups] == [2, 1]
    total = backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="sum", field="size")), [])
    assert total.value == 8 and total.missing_or_invalid == 1
    assert backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="count_distinct", unit="product")), []).value == 2
    bucket = backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="bucket", field="created_at", granularity="month")), [])
    assert bucket.value == {"2024-01-01T00:00:00+00:00": 2} and bucket.missing_or_invalid == 1
    interval = ScopeSpec(predicates=[Predicate(field="created_at", op="date_interval", lower="2024-01-01T00:00:00+00:00", upper="2024-01-02T00:00:00+00:00", granularity="day")])
    assert backend.records.execute(GEN, interval, []).matched_ids == ["d1"]  # half-open
    naive = ScopeSpec(predicates=[Predicate(field="created_at", op="date_interval", lower="2024-01-01", granularity="day")])
    naive_result = backend.records.execute(GEN, naive, [])
    assert naive_result.unsupported[0].reason == "timezone_missing" and naive_result.applied == []
    assert backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="size", op="missing")]), []).matched_ids == ["d3"]
    assert backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="size", op="range", lower=4)]), []).matched_ids == ["d2"]
    assert backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="source", op="in", value=["article"], negate=True)]), []).matched_ids == ["d1", "d2"]
    assert backend.records.exists(GEN, "product", "searchdesk", []) and not backend.records.exists(GEN, "product", "SearchDesk", [Predicate(field="source", op="eq", value="article")])
    assert backend.records.values(GEN, "product", [], 10) == ["CloudTax", "SearchDesk"]
    assert backend.records.values(GEN, "product", acl, 1) == ["CloudTax"]
    with pytest.raises(BackendError, match="not listable"):
        backend.records.values(GEN, "secret_id", [], 10)
    with pytest.raises(BackendError, match="not listable"):
        backend.records.values(GEN, "unknown", [], 10)


def test_graph_traversal_follows_fact_edges_only_with_provenance(backend):
    text = "The AACTA awarded the Film at the Gala"
    chunks = [Chunk(id="c1", document_id="d1", field="body", start=0, end=len(text), text=text)]
    backend.catalog.put_documents(GEN, [doc(1)], chunks)

    def span(s, e):
        return Span(document_id="d1", chunk_id="c1", field="body", start=s, end=e, text=text[s:e], match_level="exact")

    entities = [Entity(id="aacta", name="AACTA", type="org", span=span(4, 9)), Entity(id="film", name="Film", type="work", span=span(22, 26)), Entity(id="gala", name="Gala", type="event", span=span(34, 38))]
    relations = [
        Relation(subject_id="aacta", predicate="awarded", object_id="film", span=span(0, 26), edge_kind="fact", support="trigger_in_span", provenance=PROV),
        Relation(subject_id="film", predicate="at", object_id="gala", span=span(22, 38), edge_kind="co_occurrence", support="unchecked", provenance=PROV),
    ]
    backend.graph.upsert(GEN, entities, relations, [Membership(community_id="k1", member_id="aacta", basis="entity")])
    paths = backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=2), None)
    assert [p.nodes for p in paths] == [["aacta", "film"]] and paths[0].edges[0].provenance.extractor == "test"
    both = backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=2, edge_kinds=["fact", "co_occurrence"]), None)
    assert [p.nodes for p in both] == [["aacta", "film"], ["aacta", "film", "gala"]]
    assert backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["film"], hops=1, direction="out"), None) == []
    assert backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1), Scope(exclude_ids=["d1"])) == []
    assert backend.graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1, predicates=["other"]), None) == []
    assert backend.graph.memberships(GEN)[0].community_id == "k1"
    with pytest.raises(BackendError, match="stored entities"):
        backend.graph.upsert(GEN, [], [relations[0].model_copy(update={"object_id": "ghost"})], [])


def test_cache_requires_generation_prefix_and_honours_ttl():
    clock = {"now": 100.0}
    backend = MemoryBackend(clock=lambda: clock["now"])
    with pytest.raises(BackendError, match="GenerationId prefix"):
        backend.cache.put("answer:x", {"a": 1}, 10)
    key = GEN.prefix() + "answer:x"
    backend.cache.put(key, {"a": 1}, 10)
    assert backend.cache.get(key).value == {"a": 1}
    clock["now"] = 111.0
    assert backend.cache.get(key) is None
    backend.cache.put(key, 1, 10)
    backend.cache.put(GEN2.prefix() + "answer:y", 2, 10)
    assert backend.cache.invalidate(GEN.prefix()) == 1 and backend.cache.get(GEN2.prefix() + "answer:y").value == 2
    with pytest.raises(BackendError, match="ttl"):
        backend.cache.put(key, 1, 0)


def test_ledger_replay_marks_and_raw_replies():
    backend = MemoryBackend()
    backend.ledger.append("job1", LedgerEntry(sequence=0, kind="start", at="t0"))
    backend.ledger.append("job1", LedgerEntry(sequence=1, kind="chunk", at="t1", payload={"chunk_id": "c1"}))
    with pytest.raises(BackendError, match="sequence"):
        backend.ledger.append("job1", LedgerEntry(sequence=1, kind="dup", at="t2"))
    assert [e.kind for e in backend.ledger.replay("job1")] == ["start", "chunk"] and backend.ledger.replay("none") == []
    assert backend.ledger.status("job1") == "pending"
    backend.ledger.mark("job1", "failed")
    assert backend.ledger.status("job1") == "failed"
    with pytest.raises(BackendError):
        backend.ledger.mark("ghost", "failed")
    backend.ledger.put_raw("c1:extract", "{not json", valid=False)
    assert backend.ledger.get_raw("c1:extract").valid is False and backend.ledger.get_raw("zz") is None


def test_session_store_create_get_touch_expire():
    backend = MemoryBackend()
    record = SessionRecord(id="s1", principal_id="p", dataset="demo", created_at=0.0, last_seen_at=0.0)
    backend.session.create(record)
    with pytest.raises(BackendError, match="already exists"):
        backend.session.create(record)
    backend.session.touch("s1", 50.0)
    assert backend.session.get("s1").last_seen_at == 50.0
    with pytest.raises(BackendError):
        backend.session.touch("ghost", 1.0)
    assert backend.session.expire(now=100.0, ttl_seconds=60) == 0
    assert backend.session.expire(now=120.0, ttl_seconds=60) == 1 and backend.session.get("s1") is None
