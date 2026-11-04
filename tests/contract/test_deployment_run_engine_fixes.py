"""Engine-level fixes from the real deployment runs, on synthetic data: record-key request filters
(P10), grammar false positives that made complete results ``bounded`` (P11) without hiding real
dropped qualifiers, the measure kind on aggregates (P12), manifest counts (P13), OpenSearch index
names with ``_`` in the dataset (O1), logged 503 causes and readiness of an unservable generation
(O2), and Louvain communities reproducible across processes (O3)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import BackendError

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ["alpha", "beta", "gamma"]
LEVELS = ["FATAL", "ERROR", "INFO"]


def rows() -> list[Document]:
    return [
        Document.model_validate(
            {
                "id": f"d{i:02d}",
                "dataset": "logs",
                "source": "unit_rows" if i % 2 else "rollups",
                "title": f"row {i}",
                "fields": {"header": f"row {i} of {SERVICES[i % 3]}"},
                "metadata": {"service": SERVICES[i % 3], "level": LEVELS[i % 3], "lines_matching_level_fatal": i, "date": f"2026-01-0{1 + i % 3}", "category": "unit_day"},
            }
        )
        for i in range(12)
    ]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("p11")
    data = {
        "schema_version": 2,
        "dataset": "logs",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {
            "unknown_fields": "reject",
            "canonical_unit": "row",
            "fields": [
                {"name": "service", "type": "string", "listable": True, "groupable": True},
                {"name": "level", "type": "enum", "enum_values": LEVELS, "groupable": True},
                {"name": "lines_matching_level_fatal", "type": "integer"},
                {"name": "category", "type": "string", "listable": True},
                {"name": "date", "type": "date", "time_basis": "event", "sortable": True},
            ],
        },
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
    }
    built = Engine(validate_config(data))
    built.ingest(rows())
    return built


def aggregate(engine, query, **filters):
    response = engine.search(SearchRequest(query=query, filters=filters, as_of="2026-01-03T12:00:00+00:00"))
    assert response.aggregates, query
    return response.aggregates[0], response


# --- P10 ------------------------------------------------------------------------------------------


def test_source_is_filterable_without_a_schema_entry(engine):
    agg, response = aggregate(engine, "how many rows by service", source="unit_rows")
    assert agg["exactness"] == "exact" and response.unsupported_constraints == []
    assert sum(agg["value"].values()) == 6
    agg, _ = aggregate(engine, "how many rows", kind="document")
    assert agg["value"] == 12 and agg["exactness"] == "exact"


def refused(engine, **filters):
    response = engine.search(SearchRequest(query="how many rows", filters=filters))
    assert all(a["exactness"] != "exact" for a in response.aggregates)  # the rules agent abstains
    return [u.reason for u in response.unsupported_constraints]


def test_a_non_string_record_key_filter_is_a_type_mismatch(engine):
    assert "type_mismatch" in refused(engine, source=7)


def test_an_undeclared_field_is_still_refused(engine):
    assert "field_not_filterable" in refused(engine, host="node-1")


# --- P11 ------------------------------------------------------------------------------------------


def test_a_group_by_followed_by_a_time_word_is_not_a_dropped_qualifier(engine):
    agg, response = aggregate(engine, "how many rows with level FATAL by service yesterday")
    assert agg["exactness"] == "exact" and response.unsupported_constraints == []
    assert agg["value"] == {}  # yesterday (2026-01-02) has no FATAL row: an exact empty answer


def test_an_enum_value_inside_a_field_name_is_not_a_qualifier(engine):
    agg, response = aggregate(engine, "total lines_matching_level_fatal by service", category="unit_day")
    assert agg["exactness"] == "exact" and response.unsupported_constraints == []
    assert agg["value"] == {"alpha": 18, "beta": 22, "gamma": 26}


def test_real_dropped_qualifiers_still_bound_the_result(engine):
    from su_multi_arm.core.types import MetadataSchema, ScopeSpec
    from su_multi_arm.decision.intent import detect_intents
    from su_multi_arm.decision.qualifiers import qualifier_candidates, unapplied

    schema = engine.config.metadata_schema
    assert isinstance(schema, MetadataSchema)
    for question, expected in [
        ("how many rows with level fatal by service", "fatal"),
        ("how many rows for service alpha", "alpha"),
        ("how many rows yesterday", "yesterday"),
    ]:
        candidates = qualifier_candidates(question, schema, None, detect_intents(question))
        missing = unapplied(candidates, ScopeSpec.model_validate({"group_by": ["service"]}), {}, schema)
        assert any(expected in m.constraint_text.casefold() for m in missing), question


# --- P12 / P13 ------------------------------------------------------------------------------------


def test_aggregates_name_their_measure(engine):
    agg, _ = aggregate(engine, "total lines_matching_level_fatal by service", category="unit_day")
    assert agg["operation"] == "group_count" and agg["measure"] == {"kind": "sum", "field": "lines_matching_level_fatal"}
    agg, _ = aggregate(engine, "how many rows by service")
    assert agg["measure"] == {"kind": "count", "of": "records"}


def test_manifest_counts_native_links_and_omits_a_local_base_url(engine):
    manifest = engine.write_path.registry.current()
    assert manifest["native_link_relations"] == 0 and "base_url" not in manifest["embedding"]


# --- O1 -------------------------------------------------------------------------------------------


class AliasStub:
    def __init__(self, names):
        self.names = names

    def request(self, method, path, params=None, body=None):
        if path.startswith("/_alias/"):
            return {name: {"aliases": {}} for name in self.names}
        if path.startswith("/_cat/indices/"):
            return [{"index": name} for name in self.names]
        raise AssertionError(path)


@pytest.mark.parametrize(("prefix", "dataset"), [("demo", "my_data"), ("su_multi_arm", "logs"), ("su_multi_arm", "a_b_c"), ("x", "with-dash_and_under")])
def test_opensearch_index_names_round_trip_with_underscores(prefix, dataset):
    from su_multi_arm.backends._lucene_common import index_name
    from su_multi_arm.backends.opensearch import OpenSearchBackend
    from su_multi_arm.backends.opensearch.adapter import docs_index_parts

    generation = "ab" * 16
    name = index_name(prefix, dataset, generation, "docs")
    assert docs_index_parts(name, prefix) == (dataset, generation)
    backend = OpenSearchBackend(index_prefix=prefix, transport=AliasStub([name]))
    assert backend.active_generation(dataset) == generation
    assert backend.generations() == [f"{dataset}/{generation}/"]


def test_an_index_of_another_prefix_is_still_foreign():
    from su_multi_arm.backends._lucene_common import index_name
    from su_multi_arm.backends.opensearch import OpenSearchBackend

    name = index_name("other", "my_data", "cd" * 16, "docs")
    backend = OpenSearchBackend(index_prefix="demo", transport=AliasStub([name]))
    with pytest.raises(BackendError, match="foreign index"):
        backend.active_generation("my_data")


# --- O2 -------------------------------------------------------------------------------------------


def test_an_unservable_generation_is_logged_and_not_ready(engine, monkeypatch, caplog):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient  # the api extra

    from su_multi_arm.api import create_app

    client = TestClient(create_app(engine=engine))
    assert client.get("/ready").json()["status"] == "ready"

    def broken():
        raise BackendError("opensearch", "active_generation", "alias serves a foreign index demo_x_docs")

    monkeypatch.setattr(engine.write_path, "pin", broken)
    with caplog.at_level(logging.WARNING, logger="su_multi_arm.serving"):
        ready = client.get("/ready")
        assert ready.status_code == 503 and ready.json()["reason"] == "active generation cannot be served"
        searched = client.post("/v2/search", json={"query": "how many rows"})
    assert searched.status_code == 503
    logged = [r.getMessage() for r in caplog.records if r.name == "su_multi_arm.serving"]
    assert any("BackendError" in m and "opensearch.active_generation" in m for m in logged)
    assert not any("how many rows" in m for m in logged)  # never request content


def test_ready_reports_the_cause_of_a_pointer_disagreement(engine, monkeypatch):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient  # the api extra

    from su_multi_arm.api import create_app
    from su_multi_arm.core.errors import GenerationDisagreement

    def disagree(gen):
        raise GenerationDisagreement(gen.dataset, gen.generation, {"os": "f" * 32})

    monkeypatch.setattr(engine.write_path.generations, "check", disagree)
    body = TestClient(create_app(engine=engine)).get("/ready").json()
    assert body["status"] == "not_ready" and body["reason"] == "active generation cannot be served"
    assert body["cause"]["error"] == "GenerationDisagreement"


# --- O3 -------------------------------------------------------------------------------------------

O3_SCRIPT = """
import json, random
from su_multi_arm.communities.base import build_communities
rng = random.Random(5)
memberships = {}
for e in range(150):
    docs = {f"doc-{rng.randint(0, 400):03d}" for _ in range(rng.randint(1, 6))}
    memberships[f"entity-{e:03d}"] = docs
names = {k: k for k in memberships}
out = {}
for strategy in ("louvain", "louvain_hierarchical"):
    out[strategy] = [(c.id, c.document_ids, c.entity_ids) for c in build_communities(strategy, memberships, names, 2, 42)]
print(json.dumps(out))
"""


def test_louvain_communities_are_identical_across_processes():
    pytest.importorskip("networkx")
    outputs = set()
    for hashseed in ("0", "1", "12345", "999"):
        env = {**os.environ, "PYTHONHASHSEED": hashseed, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", O3_SCRIPT], capture_output=True, text=True, env=env, timeout=120, check=True)
        outputs.add(result.stdout)
    assert len(outputs) == 1
    communities = json.loads(outputs.pop())
    assert len(communities["louvain"]) > 1 and communities["louvain_hierarchical"]
