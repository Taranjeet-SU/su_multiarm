"""Native aggregation pushdown (``records.aggregation: native``) with a fake transport: config and
constructor rules, the ACL inside every aggregation request, decoding of engine replies (integral
doubles, canonical value text, calendar buckets), typed refusals and bounded results, and the
``matched_ids`` cap through the structured arm and the answer contract. No server: the parity of the
two modes on OpenSearch 2.x/3.x and Elasticsearch 8.x is ``tests/conformance/test_lucene_native_aggregation.py``."""

from __future__ import annotations

from typing import Any

import pytest

from su_multi_arm.backends import _lucene_aggs as na
from su_multi_arm.backends import _lucene_common as lc
from su_multi_arm.backends.elasticsearch import ElasticsearchBackend
from su_multi_arm.backends.opensearch import OpenSearchBackend
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import FieldSpec, GenerationId, Measure, MetadataSchema, Period, Predicate, ScopeResult, ScopeSpec, Sort

GEN = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="f" * 64)
RECORDS = f"su_demo_{'a' * 32}_records"
SCHEMA = MetadataSchema(
    fields=[
        FieldSpec(name="team", type="string", groupable=True),
        FieldSpec(name="tenant", type="string"),
        FieldSpec(name="score", type="number"),
        FieldSpec(name="opened_at", type="datetime", groupable=True),
        FieldSpec(name="org", type="string"),
    ]
)
ACL = [Predicate(field="tenant", op="eq", value="t1")]
ACL_CLAUSE = {"term": {"s.tenant": "t1"}}


def empty_aggs(spec: dict[str, Any]) -> dict[str, Any]:
    """An engine reply for ``spec`` over zero matching documents."""
    out: dict[str, Any] = {}
    for name, agg in spec.items():
        kind = next(k for k in agg if k != "aggs")
        if kind == "filter":
            out[name] = {"doc_count": 0}
        elif kind == "filters":
            out[name] = {"buckets": {key: {"doc_count": 0, **empty_aggs(agg.get("aggs", {}))} for key in agg["filters"]["filters"]}}
        elif kind == "terms":
            out[name] = {"buckets": [], "doc_count_error_upper_bound": 0}
        elif kind in {"composite", "date_histogram"}:
            out[name] = {"buckets": []}
        elif kind == "stats":
            out[name] = {"count": 0, "min": None, "max": None, "sum": 0.0, "avg": None}
        elif kind in {"value_count", "cardinality"}:
            out[name] = {"value": 0}
        elif kind == "top_hits":
            out[name] = {"hits": {"hits": []}}
    return out


class Engine:
    """Fake transport: answers ``_count``/``_search``/``_settings`` and records every request body."""

    def __init__(self, search: Any = None, count: int = 0):
        self.requests: list[tuple[str, str, Any]] = []
        self.search = search
        self.count = count

    def request(self, method, path, params=None, body=None):
        self.requests.append((method, path, body))
        if path.endswith("/_mapping"):
            return {RECORDS: {"mappings": {"_meta": {"metadata_schema": SCHEMA.model_dump(mode="json")}}}}
        if path.endswith("/_settings"):
            return {RECORDS: {"settings": {"index": {"number_of_shards": "1"}}}}
        if path.endswith("/_count"):
            return {"count": self.count}
        if self.search is not None:
            reply = self.search(body)
            if reply is not None:
                return reply
        return {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(body.get("aggs", {}))}

    def queries(self) -> list[Any]:
        return [b["query"] for m, p, b in self.requests if p.endswith(("/_search", "/_count")) and b]


def native(engine: Engine, **kwargs: Any) -> OpenSearchBackend:
    backend = OpenSearchBackend(index_prefix="su", transport=engine, aggregation="native", **kwargs)
    backend._known.add(RECORDS)
    return backend


def _contains(tree: Any, clause: dict[str, Any]) -> bool:
    if tree == clause:
        return True
    if isinstance(tree, dict):
        return any(_contains(v, clause) for v in tree.values())
    if isinstance(tree, list):
        return any(_contains(v, clause) for v in tree)
    return False


def test_config_block_defaults_validation_and_recipe():
    from su_multi_arm.core.config import LuceneBackend, LuceneRecordsConfig

    cfg = LuceneBackend(kind="opensearch", hosts_env="H")
    assert cfg.records == LuceneRecordsConfig() and cfg.records.aggregation == "client"
    assert "records" not in cfg.recipe(), "records is query-time: the index recipe (and its golden fingerprints) is unchanged"
    native_cfg = LuceneBackend(kind="elasticsearch", hosts_env="H", records={"aggregation": "native", "matched_ids_cap": 50})
    assert native_cfg.recipe() == LuceneBackend(kind="elasticsearch", hosts_env="H").recipe()
    for bad in ({"aggregation": "server"}, {"matched_ids_cap": 0}, {"max_groups": 50001}, {"distinct_exact_cap": 0}, {"unknown": 1}):
        with pytest.raises(ValueError):
            LuceneBackend(kind="opensearch", hosts_env="H", records=bad)


def test_from_config_passes_the_records_block(monkeypatch):
    from su_multi_arm.core.config import LuceneBackend

    monkeypatch.setenv("SU_TEST_FAKE_HOSTS", "http://127.0.0.1:1")
    records = {"aggregation": "native", "matched_ids_cap": 7, "max_groups": 9, "distinct_exact_cap": 11}
    for cls, kind in ((OpenSearchBackend, "opensearch"), (ElasticsearchBackend, "elasticsearch")):
        pytest.importorskip("opensearchpy" if kind == "opensearch" else "elasticsearch")
        backend = cls.from_config(LuceneBackend(kind=kind, hosts_env="SU_TEST_FAKE_HOSTS", records=records))
        assert (backend.aggregation, backend.matched_ids_cap, backend.max_groups, backend.distinct_exact_cap) == ("native", 7, 9, 11)
        backend.close()


def test_constructor_rules():
    with pytest.raises(BackendError, match="unknown records aggregation"):
        OpenSearchBackend(transport=Engine(), aggregation="server")
    with pytest.raises(BackendError, match="must be positive"):
        OpenSearchBackend(transport=Engine(), aggregation="native", max_groups=0)
    assert OpenSearchBackend(transport=Engine()).aggregation == "client"


@pytest.mark.parametrize(
    "spec",
    [
        ScopeSpec(),
        ScopeSpec(group_by=["team"], measure=Measure(kind="avg", field="score")),
        ScopeSpec(group_by=["team"], top_n=2),
        ScopeSpec(group_by=["opened_at", "team"], granularity="month"),
        ScopeSpec(measure=Measure(kind="count_distinct", field="org")),
        ScopeSpec(group_by=["team"], measure=Measure(kind="count_distinct", field="org")),
        ScopeSpec(time_basis="opened_at", periods=[Period(label="a", lower="2024-01-01T00:00:00Z", upper="2024-02-01T00:00:00Z")], measure=Measure(kind="bucket", field="opened_at", granularity="day")),
        ScopeSpec(measure=Measure(kind="share"), numerator=[Predicate(field="team", op="eq", value="A")]),
        ScopeSpec(measure=Measure(kind="first_seen", field="opened_at")),
        ScopeSpec(sort=Sort(field="score", descending=True), limit=5),
    ],
)
def test_the_acl_is_inside_every_native_request(spec):
    """Every search and count of a native execute carries the ACL clause: an ACL-hidden record can
    never reach an aggregation (the real-engine check is ``aggregation_parity.check_native_acl``)."""

    def kinds_present(body):  # make every probed field look populated so every code path runs
        aggs = body.get("aggs", {})
        if not any(name.startswith("k") for name in aggs):
            return None
        reply = {"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(aggs)}
        for name in aggs:
            if name.startswith("k") and name.endswith(("_s", "_t")):
                reply["aggregations"][name] = {"doc_count": 1}
        return reply

    engine = Engine(search=kinds_present)
    native(engine).records.execute(GEN, spec, ACL)
    queries = engine.queries()
    assert len(queries) >= 2
    assert all(_contains(q, ACL_CLAUSE) for q in queries), queries


def test_scalar_results_decode_engine_doubles_and_buckets():
    def reply(body):
        aggs = body.get("aggs", {})
        out = {"hits": {"total": {"value": 4, "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(aggs)}
        if "m_stats" in aggs:
            out["aggregations"]["m_stats"] = {"count": 3, "min": 1.0, "max": 5.5, "sum": 9.0, "avg": 3.0}
        if "m_hist" in aggs:
            assert aggs["m_hist"]["date_histogram"]["time_zone"] == "UTC" and aggs["m_hist"]["date_histogram"]["calendar_interval"] == "1M"
            out["aggregations"]["m_valid"] = {"value": 3}
            out["aggregations"]["m_hist"] = {"buckets": [{"key": 1704067200000, "doc_count": 2}, {"key": 1706745600000, "doc_count": 0}, {"key": 1709251200000, "doc_count": 1}]}
        if body.get("_source") is False:
            out["hits"]["hits"] = [{"_id": f"r{i}", "sort": [f"r{i}"]} for i in range(body["size"])]
        return out

    engine = Engine(search=reply, count=10)
    backend = native(engine)
    total = backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="sum", field="score")), [])
    assert total.value == 9 and isinstance(total.value, int) and total.missing_or_invalid == 1 and total.denominator == 10
    assert backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="max", field="score")), []).value == 5.5
    assert backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="avg", field="score")), []).value == 3.0
    bucket = backend.records.execute(GEN, ScopeSpec(measure=Measure(kind="bucket", field="opened_at", granularity="month")), [])
    assert bucket.value == {"2024-01-01T00:00:00+00:00": 2, "2024-03-01T00:00:00+00:00": 1} and bucket.missing_or_invalid == 1
    assert bucket.matched_ids == ["r0", "r1", "r2", "r3"] and not bucket.matched_ids_truncated


def test_matched_ids_are_capped_and_flagged_and_pages_use_search_after():
    def reply(body):
        if body.get("_source") is False:
            start = int(body["search_after"][0][1:]) + 1 if "search_after" in body else 0
            return {"hits": {"hits": [{"_id": f"r{start + i}", "sort": [f"r{start + i}"]} for i in range(body["size"])]}}
        return {"hits": {"total": {"value": 25000, "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(body.get("aggs", {}))}

    engine = Engine(search=reply, count=25000)
    result = native(engine, matched_ids_cap=12000, max_matched=10).records.execute(GEN, ScopeSpec(measure=Measure(kind="count")), [])
    assert (result.value, result.total, result.exactness) == (25000, 25000, "exact")
    assert len(result.matched_ids) == 12000 and result.matched_ids_truncated and result.matched_ids[-1] == "r11999"
    pages = [b for _, p, b in engine.requests if b and b.get("_source") is False]
    assert [b["size"] for b in pages] == [10000, 2000] and pages[1]["search_after"] == ["r9999"]


def test_a_lower_bound_hit_count_is_a_typed_error():
    engine = Engine(search=lambda body: {"hits": {"total": {"value": 10000, "relation": "gte"}, "hits": []}, "aggregations": {}})
    with pytest.raises(BackendError, match="lower-bound hit count"):
        native(engine).records.execute(GEN, ScopeSpec(), [])


def _grouped(buckets: list[dict[str, Any]], kinds: tuple[str, ...] = ("s",)):
    def reply(body):
        aggs = body.get("aggs", {})
        out = {"hits": {"total": {"value": sum(b["doc_count"] for b in buckets), "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(aggs)}
        for name in aggs:
            if name.startswith("k0_") and name[3:] in kinds:
                out["aggregations"][name] = {"doc_count": 1}
        if "groups" in aggs:
            out["aggregations"]["groups"] = {"buckets": [] if "after" in aggs["groups"]["composite"] else buckets, "after_key": {"g0": "zzz"}}
        return out

    return reply


def test_group_keys_decode_to_the_stored_json_type_and_python_collisions_are_refused():
    engine = Engine(search=_grouped([{"key": {"g0": "3"}, "doc_count": 2}, {"key": {"g0": "2.5"}, "doc_count": 1}, {"key": {"g0": None}, "doc_count": 1}], kinds=("n",)))
    result = native(engine).records.execute(GEN, ScopeSpec(group_by=["score"]), [])
    assert [(g.key["score"], g.count) for g in result.groups] == [(3, 2), (2.5, 1), (None, 1)]
    assert isinstance(result.groups[0].key["score"], int)
    clash = Engine(search=_grouped([{"key": {"g0": "3"}, "doc_count": 2}, {"key": {"g0": "3.0"}, "doc_count": 1}], kinds=("n",)))
    with pytest.raises(BackendError, match="use records.aggregation: client"):
        native(clash).records.execute(GEN, ScopeSpec(group_by=["score"]), [])


def test_more_groups_than_max_groups_is_a_bounded_result_never_a_cut():
    engine = Engine(search=_grouped([{"key": {"g0": t}, "doc_count": 1} for t in "ABC"]))
    result = native(engine, max_groups=2).records.execute(GEN, ScopeSpec(group_by=["team"]), [])
    assert result.exactness == "bounded" and result.groups == [] and result.value is None
    assert "max_groups" in (result.unsupported[-1].suggestion or "") and result.total == 3


def test_ranked_terms_grow_until_the_boundary_is_decided():
    """``top_n=2`` with the second rank tied across the page edge: the request grows until every
    group that can tie is known, then ``rank_groups`` reports the ties."""
    counts = [("A", 9), ("B", 5)] + [(f"t{i:02d}", 5) for i in range(20)] + [(f"u{i:02d}", 1) for i in range(30)]

    def reply(body):
        aggs = body.get("aggs", {})
        out = {"hits": {"total": {"value": sum(c for _, c in counts), "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(aggs)}
        if "k0_s" in aggs:
            out["aggregations"]["k0_s"] = {"doc_count": 1}
        if "top" in aggs:
            assert aggs["top"]["terms"]["order"] == [{"_count": "desc"}, {"_key": "asc"}]
            size = aggs["top"]["terms"]["size"]
            out["aggregations"]["top"] = {"buckets": [{"key": k, "doc_count": c} for k, c in counts[:size]], "doc_count_error_upper_bound": 0}
            out["aggregations"]["nulls"] = {"doc_count": 0}
        return out

    engine = Engine(search=reply)
    result = native(engine).records.execute(GEN, ScopeSpec(group_by=["team"], top_n=2), [])
    sizes = [b["aggs"]["top"]["terms"]["size"] for _, _, b in engine.requests if b and "top" in b.get("aggs", {})]
    assert sizes == [16, 32]
    assert [g.key["team"] for g in result.groups] == ["A", "B"] and len(result.ties) == 20 and result.exactness == "exact"


def test_ranked_terms_with_a_reported_count_error_are_never_trusted():
    """OpenSearch 3.x concurrent segment slices: a reply with ``doc_count_error_upper_bound > 0`` has
    inexact counts (measured: 363 reported for 373). The request grows until the engine reports 0;
    past ``max_groups`` the result is bounded, never the inexact ranking."""

    def reply(body, until: int):
        aggs = body.get("aggs", {})
        out = {"hits": {"total": {"value": 13, "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(aggs)}
        if "k0_s" in aggs:
            out["aggregations"]["k0_s"] = {"doc_count": 1}
        if "top" in aggs:
            size = aggs["top"]["terms"]["size"]
            exact = size >= until
            buckets = [("A", 9 if exact else 7), ("B", 4)] if exact else [("B", 4), ("A", 7)]
            out["aggregations"]["top"] = {"buckets": [{"key": k, "doc_count": c} for k, c in buckets], "doc_count_error_upper_bound": 0 if exact else 5}
            out["aggregations"]["nulls"] = {"doc_count": 0}
        return out

    engine = Engine(search=lambda body: reply(body, until=64))
    result = native(engine).records.execute(GEN, ScopeSpec(group_by=["team"], top_n=1), [])
    assert [(g.key["team"], g.count) for g in result.groups] == [("A", 9)] and result.exactness == "exact"
    assert [b["aggs"]["top"]["terms"]["size"] for _, _, b in engine.requests if b and "top" in b.get("aggs", {})] == [16, 32, 64]
    never = native(Engine(search=lambda body: reply(body, until=10**9)), max_groups=40).records.execute(GEN, ScopeSpec(group_by=["team"], top_n=1), [])
    assert never.exactness == "bounded" and never.groups == []


def test_exact_distinct_count_above_the_cap_goes_approximate_with_the_reason():
    def reply(body):
        aggs = body.get("aggs", {})
        out = {"hits": {"total": {"value": 5, "relation": "eq"}, "hits": []}, "aggregations": empty_aggs(aggs)}
        if "k0_s" in aggs:
            out["aggregations"]["k0_s"] = {"doc_count": 5}
        if "pairs" in aggs:
            page = [] if "after" in aggs["pairs"]["composite"] else [{"key": {"u": v}, "doc_count": 1} for v in ("a", "b", "c")]
            out["aggregations"]["pairs"] = {"buckets": page, "after_key": {"u": "c"}}
        if "d_card" in aggs:
            out["aggregations"]["d_card"] = {"value": 4}
            out["aggregations"]["d_missing"] = {"doc_count": 1}
        return out

    exact = native(Engine(search=reply)).records.execute(GEN, ScopeSpec(measure=Measure(kind="count_distinct", field="org")), [])
    assert (exact.value, exact.exactness) == (3, "exact")
    approx = native(Engine(search=reply), distinct_exact_cap=2).records.execute(GEN, ScopeSpec(measure=Measure(kind="count_distinct", field="org")), [])
    assert (approx.value, approx.missing_or_invalid, approx.exactness) == (4, 1, "approximate")
    assert any("HyperLogLog" in b and "distinct_exact_cap" in b for b in approx.basis)


def test_number_and_value_decoding():
    assert na.number(3.0) == 3 and isinstance(na.number(3.0), int) and na.number(2.5) == 2.5 and na.number(2.0**60) == 2.0**60
    decode = na._value_decoder({"s", "n", "b"}, {"v": "g", "s": "gs", "b": "gb"})
    assert decode({"g": "1", "gs": "1", "gb": None}) == "1"
    assert decode({"g": "1", "gs": None, "gb": None}) == 1
    assert decode({"g": "true", "gs": None, "gb": True}) is True
    assert decode({"g": None, "gs": None, "gb": None}) is None
    assert na._value_decoder({"b"}, {"v": "g"})({"g": "false"}) is False


def test_client_mode_never_aggregates_natively():
    engine = Engine(search=lambda body: {"hits": {"hits": []}})
    backend = OpenSearchBackend(index_prefix="su", transport=engine)
    backend._known.add(RECORDS)
    backend.records.execute(GEN, ScopeSpec(group_by=["team"]), [])
    assert not any(b and "aggs" in b for _, _, b in engine.requests) and backend.stats["records.execute.native"] == 0


def test_truncated_ids_flow_through_the_structured_arm_and_the_answer_contract():
    from su_multi_arm.arms.structured import aggregate_record
    from su_multi_arm.engine import scope_result_of
    from su_multi_arm.generation.contract import AggregateEvidence

    result = ScopeResult(exactness="exact", denominator=50000, value=None, matched_ids=["r1"], matched_ids_truncated=True, total=50000)
    record = aggregate_record(ScopeSpec(), result)
    assert record["document_ids_truncated"] is True
    assert scope_result_of(record).matched_ids_truncated
    assert "document_ids_truncated" not in aggregate_record(ScopeSpec(), result.model_copy(update={"matched_ids_truncated": False}))
    # An absence claim needs an empty scope: a truncated (or even empty) id list with a total is never empty.
    assert not AggregateEvidence(id="a", result=result.model_copy(update={"matched_ids": []})).empty()


def test_lucene_common_is_unchanged_for_the_client_path():
    assert lc.MAX_MATCHED_DEFAULT == na.MATCHED_IDS_CAP_DEFAULT == 10_000
