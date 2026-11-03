"""OpenSearch adapter with a fake transport: request shapes (index names, mappings, scope filters,
lexical and vector query DSL, bulk actions, alias switch) and error mapping (unreachable server,
unknown generation, refused query, bulk item failure, mapping mismatch, config errors). No server."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from su_multi_arm.backends import _lucene_common as lc
from su_multi_arm.backends.opensearch import OpenSearchBackend
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Chunk, Document, GenerationId, LexicalQuery, Page, Predicate, Scope

GEN = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="f" * 64)
DOCS = f"su_demo_{'a' * 32}_docs"
CHUNKS = f"su_demo_{'a' * 32}_chunks"
META = {"su_multi_arm": {"mapping_version": lc.MAPPING_VERSION}}


def mapping_reply(name: str, kind: str, knn: str = "exact", properties: dict | None = None) -> dict:
    meta = {"su_multi_arm": {"mapping_version": lc.MAPPING_VERSION, "kind": kind, "dataset": "demo", "generation": "a" * 32, "knn": knn}}
    return {name: {"mappings": {"_meta": meta, "properties": properties or {}}}}


class FakeTransport:
    """Records every request; answers from a queue of canned replies or raises queued errors."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None, Any]] = []
        self.replies: list[Any] = []
        self.handlers: dict[tuple[str, str], Any] = {}

    def request(self, method, path, params=None, body=None):
        self.requests.append((method, path, params, body))
        handler = self.handlers.get((method, path))
        if handler is not None:
            reply = handler(params, body) if callable(handler) else handler
        elif self.replies:
            reply = self.replies.pop(0)
        else:
            reply = {}
        if isinstance(reply, Exception):
            raise reply
        return reply

    def bodies(self, path_part: str) -> list[Any]:
        return [b for _, p, _, b in self.requests if path_part in p]


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def backend(fake):
    b = OpenSearchBackend(index_prefix="su", transport=fake)
    b._known.update({DOCS, CHUNKS})  # mapping verified
    return b


def test_construction_rules_and_from_config(monkeypatch):
    with pytest.raises(BackendError, match="hosts are required"):
        OpenSearchBackend()
    with pytest.raises(BackendError, match="exact_rescore"):
        OpenSearchBackend(transport=FakeTransport(), knn_method="hnsw", exact_rescore=False)
    with pytest.raises(BackendError, match="index_prefix"):
        OpenSearchBackend(transport=FakeTransport(), index_prefix="Bad-Prefix")
    with pytest.raises(BackendError, match="unknown knn method"):
        OpenSearchBackend(transport=FakeTransport(), knn_method="ivf")
    from su_multi_arm.core.config import LuceneBackend

    cfg = LuceneBackend(kind="opensearch", hosts_env="SU_TEST_FAKE_HOSTS", index_prefix="pkg")
    monkeypatch.delenv("SU_TEST_FAKE_HOSTS", raising=False)
    with pytest.raises(BackendError, match="SU_TEST_FAKE_HOSTS"):
        OpenSearchBackend.from_config(cfg)


def test_index_names_carry_dataset_and_generation_and_refuse_bad_datasets(backend):
    assert backend.index(GEN, "docs") == DOCS and backend.alias("demo", "chunks") == "su_demo_chunks"
    with pytest.raises(BackendError, match="cannot name an index"):
        backend.index(GEN.model_copy(update={"dataset": "Demo/Prod"}), "docs")


def test_typed_fields_and_predicate_dsl():
    fields = lc.typed_fields({"product": "CloudTax", "size": 3, "ok": True, "created_at": "2024-01-01T00:00:00+00:00", "gone": None})
    assert fields["s"] == {"product": "CloudTax", "created_at": "2024-01-01T00:00:00+00:00"}
    assert fields["n"] == {"size": 3.0} and fields["b"] == {"ok": True} and fields["t"] == {"created_at": 1704067200000}
    assert fields["v"] == {"product": "CloudTax", "size": "3", "ok": "true", "created_at": "2024-01-01T00:00:00+00:00"}
    assert fields["p"] == ["product", "size", "ok", "created_at"]
    assert lc.predicate_query(Predicate(field="product", op="eq", value="x")) == {"term": {"s.product": "x"}}
    assert lc.predicate_query(Predicate(field="product", op="eq", value="X"), casefold=True) == {"term": {"s.product.fold": "X"}}
    assert lc.predicate_query(Predicate(field="size", op="eq", value=3)) == {"term": {"n.size": 3.0}}
    assert lc.predicate_query(Predicate(field="ok", op="eq", value=True)) == {"term": {"b.ok": True}}
    assert lc.predicate_query(Predicate(field="size", op="missing")) == {"bool": {"must_not": [{"term": {"p": "size"}}]}}
    assert lc.predicate_query(Predicate(field="size", op="in", value=[])) == lc.MATCH_NONE
    negated = lc.predicate_query(Predicate(field="source", op="in", value=["a", "b"], negate=True))
    assert negated == {"bool": {"must_not": [{"bool": {"should": [{"term": {"s.source": "a"}}, {"term": {"s.source": "b"}}], "minimum_should_match": 1}}]}}
    rng = lc.predicate_query(Predicate(field="size", op="range", lower=4, upper=9, inclusive=False))
    assert rng == {"bool": {"filter": [{"term": {"p": "size"}}, {"range": {"n.size": {"gt": 4.0}}}, {"range": {"n.size": {"lt": 9.0}}}]}}
    interval = lc.predicate_query(Predicate(field="created_at", op="date_interval", lower="2024-01-01T00:00:00+00:00", upper="2024-01-02T00:00:00Z", granularity="day"))
    assert interval["bool"]["filter"][1:] == [{"range": {"t.created_at": {"gte": 1704067200000}}}, {"range": {"t.created_at": {"lt": 1704153600000}}}]
    assert lc.predicate_query(Predicate(field="name", op="range", lower="m"))["bool"]["filter"][1] == {"range": {"s.name": {"gte": "m"}}}
    assert lc.predicate_query(Predicate(field="name", op="range", lower=True))["bool"]["filter"][1] == lc.MATCH_NONE


def test_scope_filter_pushdown_shapes():
    assert lc.scope_filter(None, "id") == []
    assert lc.scope_filter(Scope(document_ids=[]), "id") == [lc.MATCH_NONE]
    scope = Scope(document_ids=["d1", "d2"], exclude_ids=["d2"], predicates=[Predicate(field="source", op="eq", value="case")])
    assert lc.scope_filter(scope, "document_id") == [
        {"terms": {"document_id": ["d1", "d2"]}},
        {"bool": {"must_not": [{"terms": {"document_id": ["d2"]}}]}},
        {"term": {"s.source": "case"}},
    ]


def test_lexical_query_templates_never_parse_syntax():
    fields = ("text", "title^2")
    assert lc.lexical_query(LexicalQuery(text="a AND b OR (c:d)"), fields) == {"multi_match": {"query": "a AND b OR (c:d)", "fields": ["text", "title^2"], "type": "most_fields", "operator": "or"}}
    assert lc.lexical_query(LexicalQuery(kind="phrase", text="exemption certificate"), fields)["multi_match"]["type"] == "phrase"
    boolean = lc.lexical_query(LexicalQuery(kind="boolean", text="rounding", must=["rounding error"], must_not=["   "]), fields)
    assert boolean["bool"]["must"][1]["multi_match"] == {"query": "rounding error", "fields": ["text", "title^2"], "type": "phrase"} and boolean["bool"]["must_not"] == []
    assert lc.lexical_query(LexicalQuery(text="   "), fields) is None
    assert len(lc.bounded_text(" ".join(["w"] * 500)).split()) == lc.MAX_QUERY_WORDS


def test_put_documents_bulk_shape_and_chunk_reference_check(backend, fake):
    doc = Document(id="d1", dataset="demo", source="case", title="T", metadata={"product": "CloudTax", "n": 2})
    fake.handlers[("POST", f"/{DOCS}/_mget")] = {"docs": [{"_id": "d1", "found": False}]}
    fake.handlers[("POST", f"/{DOCS}/_bulk")] = {"errors": False, "items": []}
    fake.handlers[("POST", f"/{CHUNKS}/_bulk")] = {"errors": False, "items": []}
    backend.catalog.put_documents(GEN, [doc], [Chunk(id="c1", document_id="d1", field="body", start=0, end=2, text="hi")])
    doc_lines = fake.bodies(f"{DOCS}/_bulk")[0].splitlines()
    assert json.loads(doc_lines[0]) == {"index": {"_id": "d1"}}
    source = json.loads(doc_lines[1])
    assert source["id"] == "d1" and source["s"] == {"product": "CloudTax", "id": "d1", "source": "case", "dataset": "demo", "kind": "document"} and source["n"] == {"n": 2.0}
    chunk_lines = fake.bodies(f"{CHUNKS}/_bulk")[0].splitlines()
    assert json.loads(chunk_lines[0]) == {"update": {"_id": "c1"}}
    update = json.loads(chunk_lines[1])
    assert update["doc_as_upsert"] is True and update["doc"]["title"] == "T" and update["doc"]["s"]["product"] == "CloudTax" and "lexical" not in update["doc"]
    with pytest.raises(BackendError, match="unknown document"):
        backend.catalog.put_documents(GEN, [], [Chunk(id="c9", document_id="ghost", field="body", start=0, end=1, text="x")])


def test_bulk_item_failure_is_a_typed_error(backend, fake):
    fake.handlers[("POST", f"/{CHUNKS}/_bulk")] = {"errors": True, "items": [{"update": {"_id": "c1", "status": 200}}, {"update": {"_id": "ghost", "error": {"type": "document_missing_exception", "reason": "[ghost]: document missing"}}}]}
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", properties={"vec_default": {"type": "knn_vector", "dimension": 2}})
    with pytest.raises(BackendError, match="vector id 'ghost': not a catalogued chunk"):
        backend.vector.upsert(GEN, ["c1", "ghost"], np.ones((2, 2)))
    lines = fake.bodies(f"{CHUNKS}/_bulk")[0].splitlines()
    assert json.loads(lines[0]) == {"update": {"_id": "c1"}} and "doc_as_upsert" not in json.loads(lines[1])
    assert json.loads(lines[1])["doc"]["vec_default"] == pytest.approx([0.7071, 0.7071], abs=1e-4)


def test_vector_validation_and_request_shapes(backend, fake):
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", properties={"vec_default": {"type": "knn_vector", "dimension": 2}})
    for bad, message in ((np.ones(3), "2-D"), (np.ones((1, 3)), "differs"), (np.array([[np.inf, 0.0]]), "finite")):
        with pytest.raises(BackendError, match=message):
            backend.vector.upsert(GEN, ["c1"], bad)
    with pytest.raises(BackendError, match="duplicate"):
        backend.vector.upsert(GEN, ["c1", "c1"], np.ones((2, 2)))
    with pytest.raises(BackendError, match="dimension mismatch"):
        backend.vector.search(GEN, np.ones(3), None, 2)
    assert backend.vector.search(GEN, np.zeros(2), None, 2) == []
    fake.handlers[("POST", f"/{CHUNKS}/_search")] = {"hits": {"hits": [{"_score": 2.0, "_source": {"chunk_id": "c1", "document_id": "d1"}}]}}
    hits = backend.vector.search(GEN, np.array([2.0, 0.0]), Scope(exclude_ids=["d9"]), 3)
    assert [(h.chunk_id, h.score) for h in hits] == [("c1", 1.0)]
    body = fake.bodies("_search")[-1]
    script = body["query"]["script_score"]
    assert script["script"]["params"] == {"field": "vec_default", "query_value": [1.0, 0.0]} and "cosineSimilarity" in script["script"]["source"]
    assert script["query"]["bool"]["filter"] == [{"exists": {"field": "vec_default"}}, {"bool": {"must_not": [{"terms": {"document_id": ["d9"]}}]}}]
    assert body["size"] == 3 and body["sort"] == [{"_score": "desc"}, {"chunk_id": "asc"}]
    # a namespace never written is empty, not an error; namespaces hash to their own field
    assert backend.vector.search(GEN, np.ones(2), None, 2, namespace="other") == []
    fake.handlers[("PUT", f"/{CHUNKS}/_mapping")] = {"acknowledged": True}
    fake.handlers[("POST", f"/{CHUNKS}/_bulk")] = {"errors": False, "items": []}
    backend.vector.upsert(GEN, ["c1"], np.ones((1, 4)), namespace="other")
    field = next(iter(fake.bodies("_mapping")[-1]["properties"]))
    assert field.startswith("vec_") and field != "vec_default" and fake.bodies("_mapping")[-1]["properties"][field] == {"type": "knn_vector", "dimension": 4}


def test_hnsw_opt_in_uses_knn_query_then_exact_rescore(fake):
    backend = OpenSearchBackend(index_prefix="su", transport=fake, knn_method="hnsw")
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", knn="hnsw", properties={"vec_default": {"type": "knn_vector", "dimension": 2}})
    replies = iter([{"hits": {"hits": [{"_source": {"chunk_id": "c3"}}, {"_source": {"chunk_id": "c1"}}]}}, {"hits": {"hits": [{"_score": 2.0, "_source": {"chunk_id": "c1", "document_id": "d1"}}]}}])
    fake.handlers[("POST", f"/{CHUNKS}/_search")] = lambda params, body: next(replies)
    hits = backend.vector.search(GEN, np.array([1.0, 0.0]), Scope(document_ids=["d1"]), 1)
    assert [(h.chunk_id, h.score) for h in hits] == [("c1", 1.0)]
    ann, exact = fake.bodies("_search")
    assert ann["query"]["knn"]["vec_default"] == {"vector": [1.0, 0.0], "k": 50, "filter": {"bool": {"filter": [{"terms": {"document_id": ["d1"]}}]}}}
    assert exact["query"]["script_score"]["query"]["bool"]["filter"][-1] == {"terms": {"chunk_id": ["c3", "c1"]}} and exact["size"] == 1
    fake.handlers[("PUT", f"/{CHUNKS}/_mapping")] = {"acknowledged": True}
    fake.handlers[("POST", f"/{CHUNKS}/_bulk")] = {"errors": False, "items": []}
    backend.vector.upsert(GEN, ["c1"], np.ones((1, 3)), namespace="n2")
    mapping = next(iter(fake.bodies("_mapping")[-1]["properties"].values()))
    assert mapping["method"]["name"] == "hnsw" and mapping["method"]["engine"] == "lucene" and mapping["method"]["space_type"] == "cosinesimil"


def test_lexical_search_request_and_paging(backend, fake):
    fake.handlers[("POST", f"/{CHUNKS}/_search")] = {"hits": {"hits": [{"_score": 1.5, "_source": {"chunk_id": "c1", "document_id": "d1"}, "highlight": {"text": ["<em>x</em>"]}}, {"_score": 1.0, "_source": {"chunk_id": "c2", "document_id": "d2"}}]}}
    page = backend.lexical.search(GEN, LexicalQuery(text="x", fields=["body"]), Scope(predicates=[Predicate(field="source", op="eq", value="case")]), 10, Page(size=1))
    assert [h.chunk_id for h in page.items] == ["c1"] and page.items[0].highlights == ["<em>x</em>"] and page.next_cursor == "1"
    body = fake.bodies("_search")[-1]
    assert body["from"] == 0 and body["size"] == 2 and body["sort"] == [{"_score": "desc"}, {"chunk_id": "asc"}]
    assert body["query"]["bool"]["filter"] == [{"term": {"lexical": True}}, {"term": {"s.source": "case"}}, {"terms": {"field": ["body"]}}]
    assert body["query"]["bool"]["must"][0]["multi_match"]["fields"] == ["text", "title^2"] and "highlight" in body
    with pytest.raises(BackendError, match="invalid page"):
        backend.lexical.search(GEN, LexicalQuery(text="x"), None, 100000)
    with pytest.raises(BackendError, match="invalid cursor"):
        backend.lexical.search(GEN, LexicalQuery(text="x"), None, 10, Page(cursor="zz"))
    assert backend.lexical.search(GEN, LexicalQuery(text="x"), None, 1, Page(cursor="1")).items == []


def test_error_mapping(backend, fake):
    fake.handlers[("POST", f"/{DOCS}/_mget")] = lc.TransportError(None, "ConnectionError", "refused")
    with pytest.raises(BackendError, match="opensearch.get: server unavailable: ConnectionError: refused"):
        backend.catalog.get(GEN, ["d1"])
    fake.handlers[("POST", f"/{DOCS}/_mget")] = lc.TransportError(404, "index_not_found_exception", "no such index")
    with pytest.raises(BackendError, match="unknown generation"):
        backend.catalog.get(GEN, ["d1"])
    fake.handlers[("POST", f"/{DOCS}/_mget")] = lc.TransportError(400, "parsing_exception", "unknown key")
    with pytest.raises(BackendError, match="HTTP 400 parsing_exception: unknown key"):
        backend.catalog.get(GEN, ["d1"])
    fresh = OpenSearchBackend(index_prefix="su", transport=fake)
    fake.handlers[("GET", f"/{DOCS}/_mapping")] = {DOCS: {"mappings": {"_meta": {"su_multi_arm": {"mapping_version": 0, "kind": "docs"}}}}}
    with pytest.raises(BackendError, match="mapping mismatch"):
        fresh.catalog.get(GEN, ["d1"])
    fake.handlers[("GET", f"/{DOCS}/_mapping")] = {DOCS: {"mappings": {}}}
    with pytest.raises(BackendError, match="not created by this adapter"):
        fresh.catalog.get(GEN, ["d1"])
    assert lc.error_reason({"error": {"root_cause": [{"type": "t", "reason": "r"}]}}) == ("t", "r")
    assert lc.error_reason("plain") == ("error", "plain")
    with pytest.raises(BackendError, match="unexpected bulk reply"):
        lc.check_bulk("opensearch", "op", "nope")


def test_index_creation_mapping_and_probe(fake):
    backend = OpenSearchBackend(index_prefix="su", transport=fake)
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = lc.TransportError(404, "index_not_found_exception", "no such index")
    fake.handlers[("PUT", f"/{CHUNKS}")] = {"acknowledged": True}
    backend.ensure_index(GEN, "chunks", "index")
    body = fake.bodies(CHUNKS)[-1]
    assert body["settings"]["index"] == {"number_of_shards": 1, "number_of_replicas": 0} and body["mappings"]["dynamic"] == "strict"
    assert body["mappings"]["_meta"]["su_multi_arm"] == {"mapping_version": 1, "kind": "chunks", "dataset": "demo", "generation": "a" * 32, "analyzer_id": "lucene_standard", "knn": "exact"}
    assert body["mappings"]["properties"]["text"] == {"type": "text", "analyzer": "standard"} and body["mappings"]["properties"]["p"] == {"type": "keyword"}
    hnsw = OpenSearchBackend(index_prefix="su", transport=fake, knn_method="hnsw")
    hnsw.ensure_index(GEN, "chunks", "index")
    assert fake.bodies(CHUNKS)[-1]["settings"]["index"]["knn"] is True and fake.bodies(CHUNKS)[-1]["mappings"]["_meta"]["su_multi_arm"]["knn"] == "hnsw"
    fake.handlers[("GET", "/")] = {"version": {"distribution": "opensearch", "number": "2.19.1"}}
    assert backend.probe()["version"] == "2.19.1"
    fake.handlers[("GET", "/")] = {"version": {"number": "8.17.4"}}
    with pytest.raises(BackendError, match="expected an OpenSearch 2.x"):
        backend.probe()


def test_activation_is_one_alias_request_and_delete_guards(backend, fake):
    records = f"su_demo_{'a' * 32}_records"
    old = f"su_demo_{'b' * 32}_docs"
    fake.handlers[("GET", f"/{records}/_mapping")] = lc.TransportError(404, "index_not_found_exception", "no such index")
    fake.handlers[("GET", "/_alias/su_demo_docs")] = {old: {"aliases": {"su_demo_docs": {}}}}
    fake.handlers[("GET", "/_alias/su_demo_chunks")] = lc.TransportError(404, "alias_missing", "missing")
    fake.handlers[("POST", "/_aliases")] = {"acknowledged": True}
    backend.activate(GEN)
    assert fake.bodies("/_aliases")[-1] == {
        "actions": [
            {"remove": {"index": old, "alias": "su_demo_docs"}},
            {"add": {"index": DOCS, "alias": "su_demo_docs"}},
            {"add": {"index": CHUNKS, "alias": "su_demo_chunks"}},
        ]
    }
    backend.protect(GEN)
    with pytest.raises(BackendError, match="active or pinned"):
        backend.catalog.delete_generation(GEN)
    backend.unprotect(GEN)
    fake.handlers[("GET", "/_alias/su_demo_docs")] = {DOCS: {"aliases": {"su_demo_docs": {}}}}
    with pytest.raises(BackendError, match="served by the dataset alias"):
        backend.catalog.delete_generation(GEN)
    assert backend.active_generation("demo") == "a" * 32
    fake.handlers[("GET", "/_alias/su_demo_docs")] = {"other_index": {}}
    with pytest.raises(BackendError, match="foreign index"):
        backend.active_generation("demo")
    fake.handlers[("GET", "/_alias/su_demo_docs")] = lc.TransportError(404, "alias_missing", "missing")
    fake.handlers[("GET", f"/_cat/indices/su_demo_{'a' * 32}_*")] = [{"index": CHUNKS}, {"index": DOCS}]
    fake.handlers[("DELETE", f"/{CHUNKS},{DOCS}")] = {"acknowledged": True}
    backend.catalog.delete_generation(GEN)
    assert fake.requests[-1][:2] == ("DELETE", f"/{CHUNKS},{DOCS}") and DOCS not in backend._known


def test_generation_pointer_methods_for_a_secondary_role(backend, fake):
    """As a secondary holder (catalog elsewhere) the dataset aliases are this backend's pointer:
    ``activate_generation`` is the atomic switch, ``deactivate_generation`` removes only the aliases
    that still name the generation (rollback of a failed first release)."""
    from su_multi_arm.backends import extras

    assert isinstance(backend, extras.GenerationPointer) and isinstance(backend, extras.GenerationIdentity)
    records = f"su_demo_{'a' * 32}_records"
    assert backend.generation_identity(GEN) == {"indices": [DOCS, CHUNKS, records], "aliases": ["su_demo_docs", "su_demo_chunks", "su_demo_records"]}
    fake.handlers[("GET", f"/{records}/_mapping")] = lc.TransportError(404, "index_not_found_exception", "no such index")
    fake.handlers[("GET", "/_alias/su_demo_docs")] = lc.TransportError(404, "alias_missing", "missing")
    fake.handlers[("GET", "/_alias/su_demo_chunks")] = lc.TransportError(404, "alias_missing", "missing")
    fake.handlers[("POST", "/_aliases")] = {"acknowledged": True}
    backend.activate_generation(GEN)
    assert fake.bodies("/_aliases")[-1]["actions"] == [{"add": {"index": DOCS, "alias": "su_demo_docs"}}, {"add": {"index": CHUNKS, "alias": "su_demo_chunks"}}]
    other = f"su_demo_{'b' * 32}_chunks"
    fake.handlers[("GET", "/_alias/su_demo_docs")] = {DOCS: {"aliases": {"su_demo_docs": {}}}}
    fake.handlers[("GET", "/_alias/su_demo_chunks")] = {other: {"aliases": {"su_demo_chunks": {}}}}
    fake.handlers[("GET", "/_alias/su_demo_records")] = lc.TransportError(404, "alias_missing", "missing")
    sent = len(fake.bodies("/_aliases"))
    backend.deactivate_generation(GEN)
    assert len(fake.bodies("/_aliases")) == sent + 1
    assert fake.bodies("/_aliases")[-1] == {"actions": [{"remove": {"index": DOCS, "alias": "su_demo_docs"}}]}
    fake.handlers[("GET", "/_alias/su_demo_docs")] = lc.TransportError(404, "alias_missing", "missing")
    backend.deactivate_generation(GEN)  # nothing names it any more: no request
    assert len(fake.bodies("/_aliases")) == sent + 1


def test_records_requests(backend, fake):
    from su_multi_arm.core.types import Provenance, ScopeSpec, StructuredRecord
    from su_multi_arm.testing.backend_conformance import SCHEMA

    records = f"su_demo_{'a' * 32}_records"
    backend._known.add(records)
    fake.handlers[("GET", f"/{records}/_mapping")] = {records: {"mappings": {"_meta": {"su_multi_arm": META["su_multi_arm"]}}}}
    fake.handlers[("PUT", f"/{records}/_mapping")] = {"acknowledged": True}
    fake.handlers[("POST", f"/{records}/_bulk")] = {"errors": False, "items": []}
    row = StructuredRecord(schema_id="document", id="d1", values={"product": "CloudTax", "size": 3}, provenance=Provenance(extractor="t"))
    backend.records.upsert(GEN, [row], SCHEMA)
    assert fake.bodies(f"{records}/_mapping")[-1]["_meta"]["metadata_schema"]["fields"][0]["name"] == "source"
    assert [p for _, p, params, _ in fake.requests if "_bulk" in p and params == {"refresh": "true"}]
    with pytest.raises(BackendError, match="created_at type"):
        backend.records.upsert(GEN, [row.model_copy(update={"values": {"created_at": 5}})], SCHEMA)
    fake.handlers[("POST", f"/{records}/_count")] = {"count": 1}
    fake.handlers[("POST", f"/{records}/_search")] = {"hits": {"hits": [{"_source": {"data": row.model_dump(mode="json")}, "sort": ["d1"]}]}}
    result = backend.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="cloudtax"), Predicate(field="nope", op="eq", value=1)]), [Predicate(field="source", op="eq", value="case")])
    assert result.value == 1 and result.matched_ids == ["d1"] and result.unsupported[0].reason == "field_not_filterable" and result.exactness == "bounded"  # a dropped predicate is never exact (review N2)
    search = fake.bodies(f"{records}/_search")[-1]
    assert search["query"] == {"bool": {"filter": [{"term": {"s.source": "case"}}, {"term": {"s.product.fold": "cloudtax"}}]}} and search["sort"] == [{"id": "asc"}]
    fake.handlers[("POST", f"/{records}/_count")] = {"count": 20000}
    with pytest.raises(BackendError, match="above max_matched"):
        backend.records.execute(GEN, ScopeSpec(), [])
    with pytest.raises(BackendError, match="not listable"):
        backend.records.values(GEN, "secret_id", [], 5)
    with pytest.raises(BackendError, match="cardinality cap"):
        backend.records.values(GEN, "product", [], 10001)
    assert backend.records.values(GEN, "product", [], 0) == []
    fake.handlers[("POST", f"/{records}/_search")] = {"aggregations": {"values": {"buckets": [{"key": "A"}, {"key": "B"}]}}}
    assert backend.records.values(GEN, "product", [], 2) == ["A", "B"]
    assert fake.bodies(f"{records}/_search")[-1]["aggs"] == {"values": {"terms": {"field": "v.product", "size": 2, "order": {"_key": "asc"}}}}
