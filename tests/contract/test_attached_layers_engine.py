"""Attached layers on the unified engine (unified merge M1b, §6.4, §6.12): an attached kuzu graph,
a segment sidecar and imported communities are digest-pinned ``LayerRef`` values of the unified
generation, exempt from the non-sqlite refusal, and serve exactly what today's engine serves over
the adopted store. Changed layer files fail that layer's arm (labelled) while the other arms keep
serving; a required layer refuses the compose; layers of later increments are refused.

Synthetic: the F0 support store (``tests/adopted_support.py``) and the attached kuzu store of
``tests/unit/test_kuzu_attach.py``.
"""

from __future__ import annotations

import pytest
import yaml

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.core.config import load_config, validate_config
from su_multi_arm.core.errors import BackendError, ConfigError
from su_multi_arm.ingestion.layers import LayerBackend, path_digest
from tests.adopted_support import QUERIES, adopted_engine, build_original, sha256, store_tree, todays_engine, unified_data, write_yaml
from tests.partition_support import arm_hits, hits

# --- segment sidecar and imported communities --------------------------------------------------------


@pytest.fixture
def built(tmp_path):
    return tmp_path, build_original(tmp_path)


def test_the_pinned_layers_enter_the_manifest_and_the_fingerprint(built):
    root, original = built
    engine = adopted_engine(root, original)
    manifest = engine.write_path.current()[1]
    layers = {layer["name"]: layer for layer in manifest["layers"]}
    assert layers["support_segments"]["digest"] == path_digest(root / "segments")
    assert layers["support_communities"]["digest"] == sha256(root / "communities.jsonl")
    assert layers["support_communities"]["version"].startswith("support/adopted:") and manifest["layers_unavailable"] == {}
    data = unified_data(root, original, layers=False)
    data["backends"]["local"]["path"] = str(root / "bare")
    bare = Engine(validate_config(data))
    bare.view.attach().adopt("support")
    assert bare.compose()["fingerprint"] != manifest["fingerprint"]  # layer recipes are part of it


def test_changed_layer_files_fail_only_that_arm(built):
    root, original = built
    today = todays_engine(original)
    engine = adopted_engine(root, original)
    query = QUERIES[1]
    (root / "segments" / "manifest.json").write_text((root / "segments" / "manifest.json").read_text() + " ")
    served = Engine(validate_config(unified_data(root, original)))  # a new process re-checks the pin
    result = served.search(SearchRequest(query=query))
    assert result.arm_hits["segment"] == []
    failed = [s for s in result.trace["steps"] if s["arm"] == "segment"]
    assert failed and all(s["layer_unavailable"] == ["support_segments"] and s["incomplete"] for s in failed)
    assert result.trace["coverage"]["incomplete"] is True
    assert any(r.get("kind") == "layer_unavailable" and r["layer"] == "support_segments" for r in result.trace["reports"])
    assert {layer["layer"]: layer["status"] for layer in result.trace["collection"]["layers"]}["support_segments"] == "unavailable"
    reference = today.search(SearchRequest(query=query))
    for arm in ("lexical", "dense", "community"):
        assert arm_hits(result)[arm] == arm_hits(reference)[arm]
    assert engine.write_path.current()  # the generation is unchanged


def test_an_unavailable_layer_is_left_out_or_refuses_when_required(built):
    root, original = built
    data = unified_data(root, original)
    data["collection"]["layers"][1]["sha256"] = "0" * 64  # segment pin does not match the files
    engine = Engine(validate_config(data))
    engine.view.attach().adopt("support")
    manifest = engine.compose()
    assert [layer["name"] for layer in manifest["layers"]] == ["support_communities"]
    assert "support_segments" in manifest["layers_unavailable"]
    data["collection"]["layers"][1]["required"] = True
    engine = Engine(validate_config(data))
    with pytest.raises(ValueError, match="required layer support_segments unavailable"):
        engine.compose()


def test_a_community_file_the_store_did_not_import_is_refused(built):
    root, original = built
    other = root / "other_communities.jsonl"
    other.write_text((root / "communities.jsonl").read_text().splitlines()[0] + "\n")
    data = unified_data(root, original)
    data["collection"]["layers"][0].update(path=str(other), sha256=sha256(other))
    engine = Engine(validate_config(data))
    engine.view.attach().adopt("support")
    with pytest.raises(ConfigError, match="imported communities with sha256"):
        engine.compose()


@pytest.mark.parametrize(
    ("change", "path"),
    [
        (lambda d: d["pipeline"]["arms"].update(graph={"backend": "local"}), "pipeline.arms.graph"),
        (lambda d: d["collection"].update(layers=[layer for layer in d["collection"]["layers"] if layer["kind"] != "segment_sidecar"]), "pipeline.arms.segment"),
        (lambda d: d["communities"].update(layers=[{"name": "errors", "strategy": "shared_errors", "sources": ["ticket"]}]), "communities.layers.errors"),
        (lambda d: d["collection"]["layers"].append({"name": "overlay", "kind": "graph_overlay", "sources": ["ticket"], "path": "overlay.sqlite", "sha256": "0" * 64}), "collection.layers.overlay"),
    ],
)
def test_what_m1b_does_not_serve_is_refused(built, change, path):
    root, original = built
    data = unified_data(root, original)
    change(data)
    with pytest.raises(ConfigError) as info:
        Engine(validate_config(data))
    assert info.value.path == path


def test_the_layer_member_binding_is_validated(built):
    root, original = built
    data = unified_data(root, original)
    data["collection"]["layers"][0]["member"] = "nobody"
    with pytest.raises(ConfigError, match="no member 'nobody'"):
        validate_config(data)


# --- attached kuzu graph -------------------------------------------------------------------------------

kuzu = pytest.importorskip("kuzu")

from tests.unit.test_kuzu_attach import MAPPING, store  # noqa: E402,F401  (module-scoped fixture)

DOCS = [
    Document(id="case:T1", dataset="ext", source="case", kind="case", title="Login fails after upgrade", fields={"body": "Users cannot log in after the upgrade. Refreshing the token fixed it."}),
    Document(id="case:T2", dataset="ext", source="case", kind="case", title="Login slow", fields={"body": "Login takes minutes on the Portal login page."}),
]
GRAPH_QUERIES = ["login timeout", "token expiry fix", "portal login page", "cache flush"]


def graph_original(tmp_path, store, generations):  # noqa: F811
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(yaml.safe_dump({**MAPPING, "generations": generations}))
    return {
        "schema_version": 2,
        "dataset": "ext",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "store"), "lexical_engine": "bm25"}, "kz": {"kind": "kuzu", "path": str(store), "mode": "attach", "attach": {"mapping": str(mapping)}}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32}}},
        "pipeline": {"decision": {"kind": "rules"}, "arms": {"lexical": {"backend": "local"}, "graph": {"backend": "kz", "hops": 1, "path_evidence": True}}},
    }


def graph_unified(tmp_path, original_path, data, unified_root=None, **layer):
    unified = {**data, "schema_version": 3}
    unified["backends"] = {**data["backends"], "local": {**data["backends"]["local"], "path": str(unified_root or tmp_path / "unified")}}
    unified["collection"] = {
        "mode": "partitioned",
        "members": {
            "support": {
                "kind": "support",
                "sources": ["case"],
                "record_kinds": ["case"],
                "key": "adopted",
                "cadence": "adopted",
                "producer": {"kind": "adopted"},
                "id_namespaces": ["case:"],
                "adopt": {"store": str(tmp_path / "store"), "original_config": str(original_path), "original_config_sha256": sha256(original_path)},
            }
        },
        "layers": [{"name": "support_graph", "kind": "graph_attach", "backend": "kz", "sources": ["case"], "arms": ["graph"], "member": "support", **layer}],
    }
    return unified


@pytest.fixture
def graph_engines(tmp_path, store):  # noqa: F811
    seed = Engine(validate_config(graph_original(tmp_path, store, ["a" * 32])))
    generation = seed.ingest(DOCS)["generation"]
    data = graph_original(tmp_path, store, [generation])  # the attached store answers for the built generation
    original_path = write_yaml(tmp_path / "original.yaml", data)
    today = Engine(load_config(original_path))
    unified = Engine(validate_config(graph_unified(tmp_path, original_path, data)))
    unified.view.attach().adopt("support")
    unified.compose()
    return unified, today, tmp_path, original_path, data


def test_the_graph_arm_reads_the_attached_layer_as_today(graph_engines):
    unified, today, tmp_path, *_ = graph_engines
    before = store_tree(tmp_path / "store")
    assert isinstance(unified.backends["kz"], LayerBackend)
    for query in GRAPH_QUERIES:
        a, b = unified.search(SearchRequest(query=query)), today.search(SearchRequest(query=query))
        assert hits(a) == hits(b) and arm_hits(a) == arm_hits(b), query
        steps = lambda r: [(s["arm"], s.get("status"), s.get("paths")) for s in r.trace["steps"] if s["arm"] == "graph"]  # noqa: E731
        assert steps(a) == steps(b), query  # graph evidence (paths) kept
    assert any(arm_hits(unified.search(SearchRequest(query=q)))["graph"] for q in GRAPH_QUERIES)
    assert store_tree(tmp_path / "store") == before
    layer = {x["layer"]: x for x in unified.search(SearchRequest(query=GRAPH_QUERIES[0])).trace["collection"]["layers"]}
    assert layer["support_graph"]["status"] == "available"


def test_an_attached_graph_must_answer_for_the_adopted_generation(graph_engines, store):  # noqa: F811
    _, _, tmp_path, original_path, data = graph_engines
    other = graph_unified(tmp_path, original_path, data, unified_root=tmp_path / "other")
    mapping = tmp_path / "other_mapping.yaml"
    mapping.write_text(yaml.safe_dump({**MAPPING, "generations": ["b" * 32]}))
    other["backends"]["kz"] = {**other["backends"]["kz"], "attach": {"mapping": str(mapping)}}
    engine = Engine(validate_config(other))
    engine.view.attach().adopt("support")
    with pytest.raises(ConfigError, match="not the adopted store generation"):
        engine.compose()


def test_a_changed_attached_graph_fails_the_graph_arm_only(graph_engines):
    unified, today, tmp_path, original_path, data = graph_engines
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(mapping.read_text() + "\n# edited\n")
    served = Engine(validate_config(graph_unified(tmp_path, original_path, data)))
    with pytest.raises(BackendError, match="unavailable"):
        served.backends["kz"].graph.find_entities(served.write_path.pin()[0], ["login"], None)
    result = served.search(SearchRequest(query=GRAPH_QUERIES[0]))
    assert [s for s in result.trace["steps"] if s["arm"] == "graph" and s.get("layer_unavailable") == ["support_graph"]]
    assert result.arm_hits["graph"] == []
    assert arm_hits(result)["lexical"] == arm_hits(today.search(SearchRequest(query=GRAPH_QUERIES[0])))["lexical"]
