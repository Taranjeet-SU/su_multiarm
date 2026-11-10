"""Schema v3: the unified collection surface, validated but inert (unified merge plan S0, §7).

Covers: v2 files load unchanged and a v2 file with any v3 block is refused with a migration
message; ``schema_version: 3`` loads; every rule of ``collection`` (``core/config_collection.py``),
``crosssource`` (``core/config_crosssource.py``) and ``communities.layers``; relative paths of the
v3 blocks resolve against the YAML file; the engine refuses to serve v3 blocks (no silent
monolithic serving); the generated schema carries the new keys. Synthetic values only.
"""

from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path

import pytest
import yaml

from su_multi_arm.core.config import Config, load_config, validate_config
from su_multi_arm.core.errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]
TRAPS = ROOT / "tests" / "fixtures" / "unified" / "traps" / "traps.yaml"
SHA = "a" * 64


def base(**extra) -> dict:
    data = {
        "schema_version": 3,
        "dataset": "unified",
        "backends": {"local": {"kind": "sqlite", "path": "state"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
    }
    data.update(extra)
    return data


def collection() -> dict:
    """A synthetic four-member collection shaped like the F0 fixture."""
    return {
        "mode": "partitioned",
        "members": {
            "support": {
                "kind": "support",
                "sources": ["ticket", "doc"],
                "record_kinds": ["ticket", "doc"],
                "key": "adopted",
                "cadence": "adopted",
                "producer": {"kind": "adopted"},
                "id_namespaces": ["ticket:", "https://docs.example.org/kb/"],
                "identity_namespace": "support.account",
                "time_roles": {"ticket": {"event_time": "created_at", "update_time": "resolved_at"}},
                "canonical_units": {"ticket": "ticket", "doc": "doc"},
                "embed_fields": {"ticket": ["title", "description"]},
                "field_map": {"customer_ref": "account", "url": None},
                "adopt": {"store": "support-store", "original_config": "support.yaml", "original_config_sha256": SHA},
                "required": True,
            },
            "logs": {
                "kind": "logs",
                "sources": ["logs"],
                "record_kinds": ["incident", "episode_cell", "signature", "signature_day"],
                "key": "day",
                "cadence": "minutes",
                "producer": {"kind": "log_host"},
                "id_namespaces": ["logs:"],
                "identity_namespace": "logs.tenant",
                "canonical_units": {"incident": "episode", "signature": "signature"},
                "embed_fields": {"incident": ["title", "summary"]},
            },
            "analytics": {
                "kind": "analytics",
                "sources": ["analytics"],
                "record_kinds": ["search_query_day", "search_click"],
                "key": "day",
                "id_namespaces": ["analytics:"],
            },
        },
        "embedding_space": {"id": "fixture-space-1"},
        "lexical": {"support": {"stats": "adopted"}, "logs": {"stats": "query_time"}},
        "selection": {"source_quotas": {"ticket": 0.3, "doc": 0.2, "logs": 0.4}},
        "layers": [
            {"name": "support_segments", "kind": "segment_sidecar", "sources": ["ticket"], "arms": ["segment"], "path": "segments/manifest.json", "sha256": SHA},
            {"name": "support_communities", "kind": "communities_imported", "sources": ["ticket", "doc"], "arms": ["community"], "path": "communities.jsonl", "sha256": SHA},
        ],
        "slices": {"gc_grace_seconds": 3600},
        "id_directory": {"path": "registry/ids.sqlite"},
    }


def v3(**extra) -> Config:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return validate_config(base(**extra))


def refused(data: dict, match: str) -> ConfigError:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ConfigError, match=match) as info:
            validate_config(data)
    return info.value


# --- v2 compatibility -----------------------------------------------------------------------------


def test_v2_defaults_are_unchanged():
    config = v3(schema_version=2)
    assert config.schema_version == 2 and config.collection is None and config.crosssource is None and config.communities.layers == []


def test_the_f0_trap_v2_with_collection_is_refused_with_a_migration_message():
    trap = next(t for t in yaml.safe_load(TRAPS.read_text())["traps"] if t["id"] == "v2_with_collection")
    error = refused({**base(), **trap["config"]}, "schema_version 3")
    assert error.path == "collection"
    assert "Migrate by setting schema_version: 3" in str(error)


@pytest.mark.parametrize(
    ("key", "value", "path"),
    [
        ("crosssource", {"identity": {"registry_path": "ids.sqlite", "sha256": SHA}}, "crosssource"),
        ("communities", {"layers": [{"name": "c", "strategy": "imported", "sources": ["ticket"]}]}, "communities.layers"),
    ],
)
def test_every_v3_block_is_refused_in_a_v2_file(key, value, path):
    assert refused(base(schema_version=2, **{key: value}), "belong to schema_version 3").path == path


def test_a_v2_dump_revalidates():
    config = v3(schema_version=2)
    assert Config.model_validate(config.model_dump(mode="json")).schema_version == 2


def test_unsupported_schema_versions_are_refused(tmp_path):
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(base(schema_version=4)))
    with pytest.raises(ConfigError, match="unsupported schema_version 4"):
        load_config(tmp_path / "c.yaml")


# --- schema v3 ------------------------------------------------------------------------------------


def test_a_v3_file_without_v3_blocks_loads(tmp_path):
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(base()))
    config = load_config(tmp_path / "c.yaml")
    assert config.schema_version == 3 and config.collection is None


def test_the_full_collection_validates():
    config = v3(collection=collection(), models={"roles": {"reviewer": {"provider": "anthropic", "model": "review-model", "api_key_env": "SU_TEST_KEY"}}},
                crosssource={"identity": {"registry_path": "ids.sqlite", "sha256": SHA}, "links": {"review": {"approval_ref": "owner-2026-11-09"}}},
                communities={"layers": [{"name": "support", "strategy": "imported", "sources": ["ticket", "doc"], "layer": "support_communities"}]})
    c = config.collection
    assert c.mode == "partitioned" and set(c.members) == {"support", "logs", "analytics"}
    assert c.sources() == {"ticket", "doc", "logs", "analytics"}
    assert c.stats_of("support") == "adopted" and c.stats_of("logs") == "query_time" and c.stats_of("analytics") == "query_time"
    assert c.members["logs"].all_catalog_kinds() == ("signature",)  # the default catalog kind (§6.1)
    assert c.slices.compaction.max_units_per_day() == 19 and c.slices.max_units_per_day == 19
    links = config.crosssource.links
    assert (links.window_before_days, links.window_after_days, links.max_per_record, links.show_candidates) == (1, 7, 5, "labelled")
    assert links.review.role == "reviewer" and links.review.usd_ceiling == 10.0


def test_v3_paths_resolve_against_the_config_directory(tmp_path):
    data = base(collection=collection(), crosssource={"identity": {"registry_path": "private/ids.sqlite", "sha256": SHA}})
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(data))
    config = load_config(tmp_path / "c.yaml")
    support = config.collection.members["support"].adopt
    assert support.store == tmp_path / "support-store" and support.original_config == tmp_path / "support.yaml"
    assert [layer.path for layer in config.collection.layers] == [tmp_path / "segments" / "manifest.json", tmp_path / "communities.jsonl"]
    assert config.collection.id_directory.path == tmp_path / "registry" / "ids.sqlite"
    assert config.crosssource.identity.registry_path == tmp_path / "private" / "ids.sqlite"


def mutate(change) -> dict:
    data = collection()
    change(data)
    return base(collection=data)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda c: c["members"]["analytics"].update(id_namespaces=["ticket:extra"]), "overlaps"),
        (lambda c: c["members"]["logs"].update(embed_fields={"signature": ["exemplars"]}), "retention invariant"),
        (lambda c: c["members"]["logs"].update(embed_fields={"unknown": ["title"]}), "not record_kinds"),
        (lambda c: c["members"]["logs"].update(catalog_kinds=["nope"]), "catalog_kinds must be record_kinds"),
        (lambda c: c["members"]["logs"].update(field_map={"tenant": "tenant"}), "core fields"),
        (lambda c: c["members"]["logs"].update(visibility="tenant"), "operator"),
        (lambda c: c["members"]["logs"].update(id_namespaces=[]), "at least 1"),
        (lambda c: c["members"]["support"].pop("adopt"), "adopt is required"),
        (lambda c: c["members"]["support"].update(producer={"kind": "write_path"}), "producer.kind: adopted"),
        (lambda c: c["members"]["analytics"].update(adopt={"store": "s", "original_config": "o", "original_config_sha256": SHA}), "adopt is required by, and only read with"),
        (lambda c: c["lexical"].update(analytics={"stats": "adopted"}), "only valid for, an adopted member"),
        (lambda c: c["lexical"].update(logs={"stats": "member_rrf"}, analytics={"stats": "member_rrf"}) or c["members"]["logs"].update(key="snapshot"), "member_rrf"),
        (lambda c: c["lexical"].update(nobody={"stats": "query_time"}), "no member 'nobody'"),
        (lambda c: c["selection"].update(source_quotas={"ticket": 0.7, "logs": 0.4}), "at most 1"),
        (lambda c: c["selection"].update(source_quotas={"tracker": 0.1}), "no member declares"),
        (lambda c: c["selection"].update(source_quotas={"ticket": 0}), r"\(0, 1\]"),
        (lambda c: c["layers"].append(dict(c["layers"][0])), "unique"),
        (lambda c: c["layers"][0].update(sources=["tracker"]), "no member declares"),
        (lambda c: c["layers"][0].pop("sha256"), "pinned by sha256"),
        (lambda c: c["layers"][0].update(backend="local"), "exactly one of backend or path"),
        (lambda c: c["slices"].update(max_units_per_day=5), "compaction ladder"),
        (lambda c: c["slices"].update(compaction={"refresh_seconds": 700}), "multiple of refresh_seconds"),
        (lambda c: c["members"]["logs"].update(cadence="daily") or c["slices"].update(embed_lag_seconds=60), "cadence: minutes"),
        (lambda c: c.update(mode="monolithic"), "partitioned"),
        (lambda c: c.update(unknown=True), "unknown key"),
        (lambda c: c["members"].update(Bad={**c["members"]["analytics"], "id_namespaces": ["x:"]}), "member id"),
    ],
)
def test_collection_rules(change, match):
    refused(mutate(change), match)


def test_a_graph_attach_layer_names_an_attached_kuzu_backend(tmp_path):
    layer = {"name": "graph", "kind": "graph_attach", "sources": ["ticket"], "arms": ["graph"], "backend": "local"}
    refused(mutate(lambda c: c["layers"].append(layer)), "kuzu backend with mode: attach")
    refused(mutate(lambda c: c["layers"].append({**layer, "backend": "nope"})), "no backend instance")
    refused(mutate(lambda c: c["layers"].append({**{k: v for k, v in layer.items() if k != "backend"}, "path": "g", "sha256": SHA})), "names the attached graph backend")


def test_a_producer_source_must_be_a_configured_log_source():
    error = refused(mutate(lambda c: c["members"]["logs"]["producer"].update(source="elk")), "not a configured source")
    assert error.path == "collection.members.logs.producer.source"


def test_slice_grace_is_at_least_the_request_deadline():
    data = mutate(lambda c: c["slices"].update(gc_grace_seconds=120))
    data["budgets"] = {"deadline_seconds": 600}
    refused(data, "at least budgets.deadline_seconds")


# --- crosssource and communities.layers -------------------------------------------------------------


def test_link_review_needs_a_configured_role_an_approval_and_the_approved_ceiling():
    links = {"review": {"approval_ref": "owner-2026-11-09"}}
    refused(base(crosssource={"links": links}), "not a configured chat role")
    refused(base(crosssource={"links": {"review": {}}}), "approval_ref")
    refused(base(crosssource={"links": {"review": {"approval_ref": "x", "usd_ceiling": 10.5}}}), "less than or equal to 10")
    refused(base(crosssource={"identity": {"registry_path": "ids", "sha256": "nope"}}), "pattern")


def test_community_layers_need_a_collection_and_known_sources_and_layers():
    layer = {"name": "support", "strategy": "imported", "sources": ["ticket"]}
    refused(base(communities={"layers": [layer]}), "needs a collection block")
    refused(base(collection=collection(), communities={"layers": [{**layer, "sources": ["tracker"]}]}), "no member declares")
    refused(base(collection=collection(), communities={"layers": [{**layer, "layer": "missing"}]}), "no collection layer named")
    refused(base(collection=collection(), communities={"layers": [layer, layer]}), "unique")


def test_community_layers_enter_the_recipe_only_when_set():
    config = v3(collection=collection(), communities={"layers": [{"name": "support", "strategy": "imported", "sources": ["ticket"]}]})
    assert config.communities.recipe()["layers"] == [{"name": "support", "strategy": "imported", "sources": ["ticket"], "layer": None}]
    assert "layers" not in v3().communities.recipe()


# --- inert: the engine refuses to serve v3 blocks ----------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        # M1b serves adoption and the segment/community layers; a links layer lands with M3
        ({"collection": {**collection(), "layers": [*collection()["layers"], {"name": "support_links", "kind": "links", "sources": ["ticket"], "path": "links.sqlite", "sha256": SHA}]}}, "collection.layers.support_links"),
    ],
)
def test_the_engine_refuses_v3_blocks_it_does_not_serve(extra, path, tmp_path):
    from su_multi_arm import Engine

    data = base(**extra)
    data["backends"]["local"]["path"] = str(tmp_path / "state")
    with pytest.raises(ConfigError, match="not served yet") as info:
        Engine(v3(**{k: v for k, v in data.items() if k not in {"schema_version"}}))
    assert info.value.path == path
    assert not (tmp_path / "state").exists()  # refused before any backend was opened


def test_a_v3_file_without_v3_blocks_builds_todays_engine(tmp_path):
    from su_multi_arm import Engine
    from su_multi_arm.runtime.collection import SingleMemberView

    data = base()
    data["backends"]["local"]["path"] = str(tmp_path / "state")
    engine = Engine(v3(**{k: v for k, v in data.items() if k != "schema_version"}))
    assert isinstance(engine.view, SingleMemberView) and engine.view.member_ids() == ("default",)


# --- generated schema --------------------------------------------------------------------------------


def test_the_generated_schema_carries_the_v3_surface():
    from su_multi_arm.core.schema import generate

    schema = generate()
    assert schema["properties"]["schema_version"]["enum"] == [2, 3]
    assert {"collection", "crosssource"} <= set(schema["properties"])
    defs = schema["$defs"]
    assert {"CollectionConfig", "CollectionMember", "CollectionLayer", "SliceSettings", "CrossSourceConfig", "LinksConfig", "CommunityLayer"} <= set(defs)
    assert "layers" in defs["CommunitiesConfig"]["properties"]


def test_the_committed_schema_is_current():
    from su_multi_arm.core.schema import render

    assert json.loads((ROOT / "docs" / "config.schema.json").read_text()) == json.loads(render())


def test_collection_blocks_carry_no_code_references():
    data = mutate(lambda c: c["members"]["logs"].update(kind="logs"))
    data = copy.deepcopy(data)
    data["collection"]["members"]["logs"]["field_map"] = {"title": "os_system"}  # an identifier, not code
    assert v3(**{k: v for k, v in data.items() if k != "schema_version"}).collection.members["logs"].field_map == {"title": "os_system"}
    refused(mutate(lambda c: c["members"]["logs"].update(field_map={"title": "os:system"})), "identifier")
