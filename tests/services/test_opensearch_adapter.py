"""Real-service checks of the OpenSearch adapter beyond the conformance kit (``external``):
server identity, generation-named indices, atomic alias activation, failed builds never touching the
alias, delete refusal for the served generation, mapping-version mismatch, highlights, HNSW + exact
rescore agreeing with exact search, and the records scan cap. Uses the stack in ``tests/services``.
"""

from __future__ import annotations

import os
import secrets

import numpy as np
import pytest

from service_probe import require_search_service
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import LexicalQuery, Predicate, Scope, ScopeSpec
from su_multi_arm.testing import backend_conformance as kit

pytestmark = pytest.mark.external


@pytest.fixture
def backend():
    from su_multi_arm.backends.opensearch import OpenSearchBackend

    url = require_search_service("opensearch")  # skip with the reason when unset and unreachable
    prefix = f"su_test_{secrets.token_hex(4)}"
    made = []

    def make(**kwargs):
        made.append(OpenSearchBackend(url, index_prefix=prefix, **kwargs))
        return made[-1]

    yield make
    for b in made:
        b.transport.request("DELETE", f"/{prefix}_*", params={"ignore_unavailable": "true"})
        b.close()


def test_probe_reports_a_supported_major(backend):
    """2.x and 3.x are accepted; ``SU_TEST_OPENSEARCH_MAJOR`` pins which one the configured server must be."""
    info = backend().probe()
    assert info["distribution"] == "opensearch" and info["major"] in (2, 3) and info["version"].startswith(f"{info['major']}.")
    expected = os.environ.get("SU_TEST_OPENSEARCH_MAJOR")
    if expected:
        assert info["major"] == int(expected), f"SU_TEST_OPENSEARCH_MAJOR={expected} but the server is {info['version']}"
    assert backend(knn_method="hnsw").probe()["major"] == info["major"]


def test_generation_identity_alias_activation_and_delete_guard(backend):
    b = backend()
    kit.seed(b, kit.GEN)
    assert b.generation_indices(kit.GEN) == [f"{b.prefix}_conformance_{kit.GEN.generation}_chunks", f"{b.prefix}_conformance_{kit.GEN.generation}_docs"]
    assert b.active_generation("conformance") is None
    b.activate(kit.GEN)
    assert b.active_generation("conformance") == kit.GEN.generation
    # A second generation is staged next to the first; the alias is untouched until activation.
    kit.seed(b, kit.GEN2)
    b.catalog.put_documents(kit.GEN2, [kit.document(7)], [])
    assert b.active_generation("conformance") == kit.GEN.generation
    assert b.catalog.count_documents(kit.GEN) == 4 and b.catalog.count_documents(kit.GEN2) == 5
    with pytest.raises(BackendError, match="served by the dataset alias"):
        b.catalog.delete_generation(kit.GEN)
    # A failed build is removed without touching the alias.
    b.catalog.delete_generation(kit.GEN2)
    assert b.active_generation("conformance") == kit.GEN.generation and b.generation_indices(kit.GEN2) == []
    # Activation of a new generation is one alias switch; the old generation stays until deleted.
    kit.seed(b, kit.GEN2)
    b.activate(kit.GEN2)
    assert b.active_generation("conformance") == kit.GEN2.generation
    alias_docs = b.transport.request("GET", f"/_alias/{b.alias('conformance', 'docs')}")
    assert list(alias_docs) == [b.index(kit.GEN2, "docs")]
    assert b.catalog.count_documents(kit.GEN) == 4
    assert sorted(b.generations()) == [kit.GEN.prefix(), kit.GEN2.prefix()]
    b.catalog.delete_generation(kit.GEN)
    with pytest.raises(BackendError, match="unknown generation"):
        b.catalog.get(kit.GEN, ["d1"])


def test_mapping_mismatch_and_knn_method_mismatch_are_typed_errors(backend):
    b = backend()
    name = b.index(kit.GEN, "docs")
    b.transport.request("PUT", f"/{name}", body={"mappings": {"_meta": {"su_multi_arm": {"mapping_version": 99, "kind": "docs"}}}})
    with pytest.raises(BackendError, match="mapping mismatch"):
        b.catalog.get(kit.GEN, ["d1"])
    foreign = b.index(kit.GEN2, "docs")
    b.transport.request("PUT", f"/{foreign}", body={"mappings": {"properties": {"x": {"type": "keyword"}}}})
    with pytest.raises(BackendError, match="not created by this adapter"):
        b.catalog.get(kit.GEN2, ["d1"])
    exact = backend()
    kit.seed(exact, kit.GEN2.model_copy(update={"generation": "c" * 32}))
    hnsw = backend(knn_method="hnsw")
    with pytest.raises(BackendError, match="built with knn 'exact'"):
        hnsw.lexical.count(kit.GEN2.model_copy(update={"generation": "c" * 32}))


def test_highlights_field_filter_and_unknown_server(backend):
    b = backend()
    kit.seed(b, kit.GEN)
    hits = b.lexical.search(kit.GEN, LexicalQuery(text="certificate"), None, 10)
    assert hits.items and all("<em>certificate</em>" in " ".join(h.highlights) for h in hits.items)
    assert b.lexical.search(kit.GEN, LexicalQuery(text="certificate", fields=["title"]), None, 10).items == []
    from su_multi_arm.backends.opensearch import OpenSearchBackend

    down = OpenSearchBackend("http://127.0.0.1:1", index_prefix=b.prefix, timeout_seconds=1)
    with pytest.raises(BackendError, match="server unavailable"):
        down.catalog.get(kit.GEN, ["d1"])


def test_hnsw_with_exact_rescore_matches_exact_search(backend):
    rng = np.random.default_rng(7)
    vectors = rng.normal(size=(60, 8)).astype(np.float32)
    docs = [kit.document(1), kit.document(2, "SearchDesk"), kit.document(3, "CloudTax", "article")]
    chunks = [kit.chunk(i).model_copy(update={"id": f"c{i}_{j}"}) for i in (1, 2, 3) for j in range(20)]
    results = {}
    for method, gen in (("exact", kit.GEN), ("hnsw", kit.GEN2)):  # one generation per knn method: the mapping records it
        b = backend(knn_method=method)
        b.catalog.put_documents(gen, docs, chunks)
        b.vector.upsert(gen, [c.id for c in chunks], vectors)
        b.vector.refresh(gen)
        scope = Scope(document_ids=["d1", "d3"])
        results[method] = [(h.chunk_id, round(h.score, 5)) for h in b.vector.search(gen, vectors[5] + 0.1, scope, 5)]
        assert all(cid.startswith(("c1_", "c3_")) for cid, _ in results[method])
    assert results["hnsw"] == results["exact"]


def test_records_scan_cap_is_a_typed_error(backend):
    b = backend(max_matched=2)
    kit.seed(b, kit.GEN)
    from su_multi_arm.core.types import Provenance, StructuredRecord

    rows = [StructuredRecord(schema_id="document", id=f"d{i}", values={"source": "case"}, provenance=Provenance(extractor="t")) for i in (1, 2, 3)]
    b.records.upsert(kit.GEN, rows, kit.SCHEMA)
    assert b.records.execute(kit.GEN, ScopeSpec(), [Predicate(field="source", op="eq", value="nothing")]).denominator == 0
    with pytest.raises(BackendError, match="above max_matched 2"):
        b.records.execute(kit.GEN, ScopeSpec(), [])
