"""``Engine.search_page`` / ``POST /v2/search/page`` (paged listing over the retrieved candidates) and
the per-request evidence budget ``SearchRequest.evidence_chars``. Synthetic corpus on the in-memory
backend; generator is the loopback ``FakeChatServer`` (contract-mocked)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from su_multi_arm import SearchRequest
from su_multi_arm.pipeline.spec import RequestRejected
from su_multi_arm.runtime.listing import SearchPageRequest
from su_multi_arm.serving.http import create_app
from su_multi_arm.testing.provider_fakes import FakeChatServer

from compat_support import engine as make_engine

QUERY = SearchRequest(query="billing invoices")


def page(**kwargs) -> SearchPageRequest:
    return SearchPageRequest(request=kwargs.pop("request", QUERY), **kwargs)


def test_search_is_unchanged_and_pages_partition_the_candidates():
    engine = make_engine()
    plain = engine.search(QUERY)
    pages = [engine.search_page(page(page=n, page_size=5)) for n in (1, 2, 3)]
    assert [p.total for p in pages] == [len(plain.hits)] * 3 and pages[0].pages == 3 and pages[0].total_relation == "candidates"
    ids = [i.document_id for p in pages for i in p.items]
    assert ids == [h.document_id for h in plain.hits]  # relevance order, no overlap, nothing lost
    assert [i.rank for p in pages for i in p.items] == list(range(1, len(ids) + 1))
    assert engine.search_page(page(page=9, page_size=5)).items == []
    assert all(i.metadata for i in pages[0].items) and pages[0].trace["listing"]["candidates"] == len(plain.hits)


def test_sort_by_a_sortable_field_with_missing_values_last():
    engine = make_engine()
    by_date = engine.search_page(page(sort="created_at", page_size=12))
    dates = [i.metadata["created_at"] for i in by_date.items]
    assert dates == sorted(dates, reverse=True) and by_date.sort_missing == 0
    ascending = engine.search_page(page(sort="created_at", order="asc", page_size=12))
    assert [i.metadata["created_at"] for i in ascending.items] == sorted(dates)
    amounts = engine.search_page(page(sort="amount", page_size=12))
    present = [i.metadata.get("amount") for i in amounts.items if "amount" in i.metadata]
    assert present == sorted(present, reverse=True) and amounts.sort_missing == 3
    assert all("amount" not in i.metadata for i in amounts.items[-3:])  # records without a value go last
    with pytest.raises(RequestRejected, match="not declared sortable"):
        engine.search_page(page(sort="status"))


def test_any_of_is_pushed_down_and_facets_count_the_candidates():
    engine = make_engine()
    narrowed = engine.search_page(page(page_size=12, any_of={"status": ["open", "closed"]}, facets=["project", "kind"]))
    assert narrowed.total == 8 and {i.metadata["status"] for i in narrowed.items} == {"open", "closed"}
    facets = {f.field: f for f in narrowed.facets}
    assert {v.value: v.count for v in facets["project"].values} == {"Alpha": 4, "Beta": 4}
    assert facets["kind"].exactness == "bounded" and facets["kind"].basis == "candidates" and sum(v.count for v in facets["kind"].values) == 8
    both = engine.search_page(page(page_size=12, any_of={"status": ["open", "closed"], "project": ["Alpha"]}))
    assert both.total == 4 and {(i.metadata["status"], i.metadata["project"]) for i in both.items} <= {("open", "Alpha"), ("closed", "Alpha")}
    limited = engine.search_page(page(facets=["project"], facet_limit=1))
    assert len(limited.facets[0].values) == 1 and limited.facets[0].other == 6
    with pytest.raises(RequestRejected, match="not declared groupable"):
        engine.search_page(page(facets=["status"]))
    with pytest.raises(RequestRejected, match="not declared in metadata_schema"):
        engine.search_page(page(any_of={"nope": ["x"]}))
    with pytest.raises(ValueError):
        page(any_of={"status": []})


def test_unobserved_any_of_values_are_applied_and_reported():
    engine = make_engine()
    result = engine.search_page(page(any_of={"project": ["Alpha", "Zeta"]}, facets=["project"]))
    assert {i.metadata["project"] for i in result.items} == {"Alpha"}
    assert any(u.reason == "value_not_observed" for u in result.unsupported_constraints)


def test_filtered_corpus_facets_are_exact_and_query_independent():
    engine = make_engine()
    corpus = engine.search_page(page(facets=["project"], facet_scope="filtered_corpus", any_of={"kind": ["report"]}, include_items=False))
    facet = corpus.facets[0]
    assert facet.basis == "filtered_corpus" and facet.exactness == "exact"
    assert {v.value: v.count for v in facet.values} == {"Alpha": 3} and corpus.items == []


def test_text_budget_cuts_page_texts_in_order():
    engine = make_engine()
    full = engine.search_page(page(page_size=4))
    total = sum(len(i.text) for i in full.items)
    cut = engine.search_page(page(page_size=4, text_chars=max(200, total // 2)))
    assert cut.text_budget.used <= cut.text_budget.chars and cut.text_budget.truncated_ids
    assert [i.document_id for i in cut.items if i.text_truncated] == cut.text_budget.truncated_ids


def test_http_route_and_page_size_bound():
    engine = make_engine(serving={"http": {"page_size_max": 5}})
    client = TestClient(create_app(engine=engine))
    body = client.post("/v2/search/page", json={"request": {"query": "billing invoices"}, "page": 2, "page_size": 5, "sort": "created_at", "facets": ["project"]}).json()
    assert body["page"] == 2 and body["total"] == 12 and len(body["items"]) == 5 and body["facets"][0]["field"] == "project"
    assert client.post("/v2/search/page", json={"request": {"query": "billing"}, "page_size": 6}).status_code == 422
    assert client.post("/v2/search/page", json={"request": {"query": "billing"}, "sort": "status"}).status_code == 422
    assert client.post("/v2/search", json={"query": "billing", "page": 1}).status_code == 422  # /v2/search itself is unchanged


# --- per-request evidence budget ----------------------------------------------------------------------


@pytest.fixture
def fake():
    with FakeChatServer() as server:
        yield server


def test_evidence_chars_is_refused_unless_enabled(fake):
    engine = make_engine(fake.role(streaming=False))
    with pytest.raises(RequestRejected, match="not enabled"):
        engine.answer(SearchRequest(query="billing invoices", evidence_chars=1000))
    default = engine.answer(SearchRequest(query="billing invoices"))
    assert default.trace["evidence"]["budget_chars"] != 1000


def test_evidence_chars_replaces_the_budget_within_the_configured_maximum(fake):
    engine = make_engine(fake.role(streaming=False), request={"evidence_chars_max": 5000})
    small = engine.answer(SearchRequest(query="billing invoices", evidence_chars=600))
    large = engine.answer(SearchRequest(query="billing invoices", evidence_chars=5000))
    assert small.trace["evidence"]["budget_chars"] == 600 and large.trace["evidence"]["budget_chars"] == 5000
    assert small.trace["evidence"]["evidence_characters"] <= large.trace["evidence"]["evidence_characters"]
    with pytest.raises(RequestRejected, match="exceeds request.evidence_chars_max"):
        engine.answer(SearchRequest(query="billing invoices", evidence_chars=5001))
    client = TestClient(create_app(engine=engine))
    assert client.post("/v2/answer", json={"query": "billing invoices", "evidence_chars": 9000}).status_code == 422
    assert client.post("/v2/answer", json={"query": "billing invoices", "evidence_chars": 100}).status_code == 422  # below the wire minimum
    assert client.post("/v2/answer", json={"query": "billing invoices", "evidence_chars": 800}).json()["trace"]["evidence"]["budget_chars"] == 800
