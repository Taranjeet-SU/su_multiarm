"""Elasticsearch adapter with a fake transport: what differs from the OpenSearch classes it is built
on (probe, index body, ``dense_vector`` mappings, exact script, top-level ``knn`` candidates, the
client transport's error mapping, the alias-backed generation registry) and that every typed error
names ``elasticsearch``. The shared request shapes are covered by ``test_opensearch_backend.py``;
behaviour against a real server by ``tests/conformance/`` (``external``)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from su_multi_arm.backends import _lucene_common as lc
from su_multi_arm.backends.elasticsearch import ElasticsearchBackend
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import GenerationId
from su_multi_arm.testing import backend_conformance as kit

GEN = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="f" * 64)
DOCS = f"es_demo_{'a' * 32}_docs"
CHUNKS = f"es_demo_{'a' * 32}_chunks"
RECORDS = f"es_demo_{'a' * 32}_records"
REGISTRY = "es__registry"


def mapping_reply(name: str, index_kind: str, knn: str = "exact", properties: dict | None = None) -> dict:
    meta = {"su_multi_arm": {"mapping_version": lc.MAPPING_VERSION, "kind": index_kind, "dataset": "demo", "generation": "a" * 32, "knn": knn}}
    return {name: {"mappings": {"_meta": meta, "properties": properties or {}}}}


class FakeTransport:
    """Records every request; answers from ``handlers`` keyed by (method, path) or a reply queue."""

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

    def bodies(self, path_part: str, method: str | None = None) -> list[Any]:
        return [b for m, p, _, b in self.requests if path_part in p and (method is None or m == method)]


@pytest.fixture
def fake():
    return FakeTransport()


@pytest.fixture
def backend(fake):
    b = ElasticsearchBackend(index_prefix="es", transport=fake, dataset="demo")
    b._known.update({DOCS, CHUNKS, RECORDS})
    return b


def test_probe_accepts_only_elasticsearch_8(fake, backend):
    fake.handlers[("GET", "/")] = {"version": {"number": "8.17.4", "lucene_version": "9.12.0"}, "cluster_name": "c"}
    assert backend.probe() == {"distribution": "elasticsearch", "version": "8.17.4", "lucene_version": "9.12.0", "max_dimensions": 4096}
    for version in ({"number": "7.17.0"}, {"number": "9.0.0"}, {"number": "2.19.1", "distribution": "opensearch"}):
        fake.handlers[("GET", "/")] = {"version": version}
        with pytest.raises(BackendError, match="elasticsearch.probe: expected an Elasticsearch 8.x server"):
            backend.probe()


def test_from_config_and_registry_need_hosts_and_dataset(monkeypatch):
    pytest.importorskip("elasticsearch", reason="optional extra `elasticsearch` not installed")
    from su_multi_arm.core.config import LuceneBackend

    cfg = LuceneBackend(kind="elasticsearch", hosts_env="SU_TEST_ES_FAKE_HOSTS", index_prefix="pkg", knn={"method": "hnsw"})
    monkeypatch.delenv("SU_TEST_ES_FAKE_HOSTS", raising=False)
    with pytest.raises(BackendError, match="elasticsearch.init: environment variable SU_TEST_ES_FAKE_HOSTS"):
        ElasticsearchBackend.from_config(cfg)
    monkeypatch.setenv("SU_TEST_ES_FAKE_HOSTS", "http://127.0.0.1:1, http://127.0.0.1:2")
    built = ElasticsearchBackend.from_config(cfg, dataset="demo")
    assert built.kind == "elasticsearch" and built.transport.hosts == ["http://127.0.0.1:1", "http://127.0.0.1:2"]
    assert built.knn_method == "hnsw" and built.prefix == "pkg" and built.dataset == "demo"
    built.close()
    with pytest.raises(BackendError, match="needs a dataset"):
        ElasticsearchBackend(transport=FakeTransport()).generation_registry()
    with pytest.raises(BackendError, match="elasticsearch.init: unknown knn method"):
        ElasticsearchBackend(transport=FakeTransport(), knn_method="ivf")


def test_index_body_drops_the_knn_setting_but_records_the_method(fake):
    exact = ElasticsearchBackend(index_prefix="es", transport=fake)
    hnsw = ElasticsearchBackend(index_prefix="es", transport=fake, knn_method="hnsw")
    for b in (exact, hnsw):
        body = b.index_body("chunks", GEN)
        assert "knn" not in body["settings"]["index"] and body["settings"]["index"]["number_of_shards"] == 1
        assert body["mappings"]["_meta"]["su_multi_arm"]["knn"] == b.knn_method
        assert body["mappings"]["dynamic"] == "strict" and body["mappings"]["dynamic_templates"] == lc.DYNAMIC_TEMPLATES
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = lc.TransportError(404, "index_not_found_exception", "no such index")
    hnsw.ensure_index(GEN, "chunks", "index")
    created = fake.bodies(f"/{CHUNKS}", "PUT")[0]
    assert "knn" not in created["settings"]["index"] and created["mappings"]["_meta"]["su_multi_arm"]["knn"] == "hnsw"


def test_dense_vector_mappings_exact_by_default_and_hnsw_on_opt_in(fake, backend):
    assert backend.vector._field_mapping(3) == {"type": "dense_vector", "dims": 3, "index": False}
    hnsw = ElasticsearchBackend(index_prefix="es", transport=fake, knn_method="hnsw")
    mapping = hnsw.vector._field_mapping(3)
    assert mapping["index"] is True and mapping["similarity"] == "cosine" and mapping["index_options"] == {"type": "hnsw", "m": 16, "ef_construction": 128}
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", properties={"vec_default": {"type": "dense_vector", "dims": 2, "index": False}})
    assert backend.vector._dimension(GEN, "default", "search") == 2
    from su_multi_arm.backends.opensearch.search import vector_field

    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", properties={vector_field("other"): {"type": "knn_vector", "dimension": 2}})
    with pytest.raises(BackendError, match="elasticsearch.search: field vec_.* is not a dense_vector \\(mapping mismatch\\)"):
        backend.vector._dimension(GEN, "other", "search")


def test_exact_search_uses_cosine_script_with_the_field_literal(fake, backend):
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", properties={"vec_default": {"type": "dense_vector", "dims": 2, "index": False}})
    fake.handlers[("POST", f"/{CHUNKS}/_search")] = {"hits": {"hits": [{"_source": {"chunk_id": "c1", "document_id": "d1"}, "_score": 2.0}]}}
    found = backend.vector.search(GEN, np.array([1.0, 0.0]), kit.Scope(exclude_ids=["d2"]), 3)
    assert [(h.chunk_id, h.score) for h in found] == [("c1", 1.0)]
    body = fake.bodies("_search")[0]
    script = body["query"]["script_score"]["script"]
    assert script["source"] == "Math.max(0.0, 1.0 + cosineSimilarity(params.query_value, 'vec_default'))"
    assert body["query"]["script_score"]["query"]["bool"]["filter"][0] == {"exists": {"field": "vec_default"}}
    assert body["sort"] == [{"_score": "desc"}, {"chunk_id": "asc"}]
    assert backend.vector.search(GEN, np.zeros(2), None, 3) == [] and len(fake.bodies("_search")) == 1
    with pytest.raises(BackendError, match="elasticsearch.search: query vector dimension mismatch"):
        backend.vector.search(GEN, np.ones(3), None, 3)


def test_hnsw_search_takes_candidates_from_the_knn_option_then_rescores_exactly(fake):
    hnsw = ElasticsearchBackend(index_prefix="es", transport=fake, knn_method="hnsw")
    hnsw._known.add(CHUNKS)
    fake.handlers[("GET", f"/{CHUNKS}/_mapping")] = mapping_reply(CHUNKS, "chunks", "hnsw", {"vec_default": {"type": "dense_vector", "dims": 2, "index": True}})
    fake.replies = [
        {"hits": {"hits": [{"_source": {"chunk_id": "c3"}}, {"_source": {"chunk_id": "c1"}}]}},
        {"hits": {"hits": [{"_source": {"chunk_id": "c1", "document_id": "d1"}, "_score": 2.0}, {"_source": {"chunk_id": "c3", "document_id": "d3"}, "_score": 1.7}]}},
    ]
    found = hnsw.vector.search(GEN, np.array([1.0, 0.0]), kit.Scope(document_ids=["d1", "d3"]), 2)
    assert [(h.chunk_id, round(h.score, 6)) for h in found] == [("c1", 1.0), ("c3", 0.7)]
    ann, exact = fake.bodies("_search")
    assert ann["knn"]["field"] == "vec_default" and ann["knn"]["k"] == 50 and ann["knn"]["num_candidates"] == 100
    assert ann["knn"]["filter"] == {"bool": {"filter": [{"terms": {"document_id": ["d1", "d3"]}}]}} and "query" not in ann
    assert exact["query"]["script_score"]["query"]["bool"]["filter"][-1] == {"terms": {"chunk_id": ["c3", "c1"]}}
    with pytest.raises(BackendError, match="exact_rescore"):
        ElasticsearchBackend(index_prefix="es", transport=fake, knn_method="hnsw", exact_rescore=False)


def test_errors_name_the_elasticsearch_adapter(fake, backend):
    fake.handlers[("POST", f"/{DOCS}/_mget")] = lc.TransportError(404, "index_not_found_exception", "no such index [x]")
    with pytest.raises(BackendError, match="elasticsearch.get: unknown generation"):
        backend.catalog.get(GEN, ["d1"])
    fake.handlers[("POST", f"/{DOCS}/_mget")] = lc.TransportError(None, "ConnectionError", "refused")
    with pytest.raises(BackendError, match="elasticsearch.get: server unavailable: ConnectionError: refused"):
        backend.catalog.get(GEN, ["d1"])
    fake.handlers[("POST", f"/{CHUNKS}/_search")] = lc.TransportError(400, "search_phase_execution_exception", "all shards failed")
    with pytest.raises(BackendError, match="elasticsearch.search: HTTP 400 search_phase_execution_exception"):
        backend.lexical.search(GEN, kit.LexicalQuery(text="x"), None, 5)
    fake.handlers[("POST", f"/{DOCS}/_mget")] = {"docs": [{"_id": "d1", "found": False}]}
    with pytest.raises(BackendError, match="elasticsearch.put_documents: chunk c1 references unknown document"):
        backend.catalog.put_documents(GEN, [], [kit.chunk(1)])
    with pytest.raises(BackendError, match="elasticsearch.upsert: duplicate ids"):
        backend.vector.upsert(GEN, ["x", "x"], np.ones((2, 2)))
    with pytest.raises(BackendError, match="elasticsearch.page: invalid cursor"):
        backend.lexical.search(GEN, kit.LexicalQuery(text="x"), None, 5, kit.Page(cursor="zz"))


def test_client_transport_maps_api_and_connection_errors():
    elasticsearch = pytest.importorskip("elasticsearch")
    from elastic_transport import ApiResponseMeta, HttpHeaders, NodeConfig

    from su_multi_arm.backends.elasticsearch.transport import ElasticsearchTransport

    transport = ElasticsearchTransport(["http://127.0.0.1:1"], 1.0)
    seen: list[tuple[str, str, dict, dict | None, Any]] = []

    class Stub:
        def __init__(self, outcome):
            self.outcome = outcome

        def perform_request(self, method, path, params=None, headers=None, body=None):
            seen.append((method, path, dict(headers or {}), params, body))
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders(), duration=0.0, node=NodeConfig("http", "127.0.0.1", 1))
    transport._client = Stub(elasticsearch.ApiError("index_not_found_exception", meta, {"error": {"type": "index_not_found_exception", "reason": "no such index [x]"}}))
    with pytest.raises(lc.TransportError) as info:
        transport.request("GET", "/x/_mapping")
    assert (info.value.status, info.value.error_type, info.value.reason) == (404, "index_not_found_exception", "no such index [x]")
    transport._client = Stub(elasticsearch.ConnectionError("refused"))
    with pytest.raises(lc.TransportError) as info:
        transport.request("GET", "/")
    assert info.value.status is None and info.value.error_type == "ConnectionError"

    class Reply:
        body = {"ok": True}

    transport._client = Stub(Reply())
    assert transport.request("POST", "/x/_bulk", body='{"index":{}}\n{}\n') == {"ok": True}
    assert seen[-1][2]["content-type"] == "application/x-ndjson" and seen[-1][4].startswith('{"index"')
    transport.request("POST", "/x/_search", body={"size": 0})
    assert seen[-1][2]["content-type"] == "application/json"


def test_registry_activation_is_the_adapters_alias_switch_and_failures_never_touch_it(fake, backend):
    registry = backend.generation_registry()
    manifest = {"generation": GEN.generation, "dataset": "demo", "index_format": 3, "fingerprint": GEN.fingerprint}
    fake.handlers[("GET", f"/{REGISTRY}/_mapping")] = {}
    fake.handlers[("GET", f"/{REGISTRY}/_doc/manifest:demo:{GEN.generation}")] = {"found": True, "_source": {"payload": manifest}}
    fake.handlers[("GET", "/_alias/es_demo_docs")] = lc.TransportError(404, "alias_not_found", "missing")
    fake.handlers[("GET", "/_alias/es_demo_chunks")] = {f"es_demo_{'b' * 32}_chunks": {"aliases": {"es_demo_chunks": {}}}}
    fake.handlers[("GET", "/_alias/es_demo_records")] = lc.TransportError(404, "alias_not_found", "missing")
    registry.activate(manifest)
    actions = fake.bodies("/_aliases")[0]["actions"]
    assert actions == [
        {"add": {"index": DOCS, "alias": "es_demo_docs"}},
        {"remove": {"index": f"es_demo_{'b' * 32}_chunks", "alias": "es_demo_chunks"}},
        {"add": {"index": CHUNKS, "alias": "es_demo_chunks"}},
        {"add": {"index": RECORDS, "alias": "es_demo_records"}},
    ]
    fake.handlers[("GET", "/_alias/es_demo_docs")] = {DOCS: {"aliases": {"es_demo_docs": {}}}}
    assert registry.current() == manifest
    with pytest.raises(BackendError, match="elasticsearch.delete_generation: generation is served by the dataset alias"):
        backend.catalog.delete_generation(GEN)
    # A failed build: manifest and failure record written, never activated.
    other = FakeTransport()
    failing = ElasticsearchBackend(index_prefix="es", transport=other, dataset="demo")
    other.handlers[("GET", f"/{REGISTRY}/_mapping")] = lc.TransportError(404, "index_not_found_exception", "no such index")
    other.handlers[("GET", f"/{REGISTRY}/_doc/manifest:demo:{GEN.generation}")] = lc.TransportError(404, "not_found", "missing")
    reg = failing.generation_registry()
    reg.write_manifest(manifest)
    reg.record_failure({"generation": GEN.generation, "unit": "embeddings", "category": "provider"})
    with pytest.raises(ValueError, match="without a written manifest"):
        reg.activate(manifest)
    assert other.bodies("/_aliases") == [] and other.bodies(f"/{REGISTRY}", "PUT")[0]["mappings"]["dynamic"] == "strict"
    assert [p for _, p, _, _ in other.requests if "_doc/" in p] == [f"/{REGISTRY}/_doc/manifest:demo:{GEN.generation}", f"/{REGISTRY}/_doc/failure:demo:{GEN.generation}", f"/{REGISTRY}/_doc/manifest:demo:{GEN.generation}"]


def test_registry_lock_conflicts_are_typed_and_stale_locks_expire(fake):
    clock = {"now": 1000.0}
    backend = ElasticsearchBackend(index_prefix="es", transport=fake, dataset="demo", clock=lambda: clock["now"])
    registry = backend.generation_registry()
    attempts = {"n": 0}

    def create(params, body):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return lc.TransportError(409, "version_conflict_engine_exception", "document already exists")
        return {"result": "created"}

    fake.handlers[("GET", f"/{REGISTRY}/_mapping")] = {}
    fake.handlers[("PUT", f"/{REGISTRY}/_create/lock:demo")] = create
    fake.handlers[("GET", f"/{REGISTRY}/_doc/lock:demo")] = {"found": True, "_source": {"created_at": 900.0}}
    with pytest.raises(BackendError, match="elasticsearch.lock: another ingestion holds the lock for dataset 'demo'"):
        with registry.lock():
            pass
    clock["now"] = 900.0 + 3600.0 + 1
    with registry.lock():
        pass
    deletes = [p for m, p, _, _ in fake.requests if m == "DELETE"]
    assert deletes == [f"/{REGISTRY}/_doc/lock:demo"] * 2
