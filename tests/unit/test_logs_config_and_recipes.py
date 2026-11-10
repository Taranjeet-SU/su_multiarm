"""``logs`` configuration rules, the ``line`` chunk recipe and projection exclusions (addendum A1
§3.4.2 n1, §3.7, §3.8.4 n4)."""

from __future__ import annotations

import copy

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import Document
from su_multi_arm.extraction.chunking import LineRecordTooLong, chunk_with_recipes, line_chunks
from su_multi_arm.ingestion.write import chunking_recipe
from tests.logs_support import config_data


def test_the_example_logs_config_validates(tmp_path):
    cfg = validate_config(config_data(tmp_path))
    assert cfg.logs.retention.keep_days == 10 and cfg.logs.partitions.clock == "index_date"


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda d: d["logs"].update(source="missing"), "logs.source"),
        (lambda d: d["extraction"].update(extractors=["gazetteer"], pattern_library=None), "extraction.extractors"),
        (lambda d: d["ingestion"].pop("line_recipe"), "ingestion.line_recipe"),
        (lambda d: d["ingestion"]["line_recipe"].update(max_chunk_chars=4096), "ingestion.line_recipe.max_chunk_chars"),
        (lambda d: d["ingestion"]["projections"]["embed_fields"].update(signature=["template", "exemplars"]), "ingestion.projections"),
        (lambda d: d["ingestion"].pop("projections"), "ingestion.projections"),
        (lambda d: d.setdefault("privacy", {}).update(trace_masking="raw"), "privacy.trace_masking"),
        (lambda d: d.setdefault("sessions", {}).update(memory={"store_text": True}), "sessions.memory.store_text"),
    ],
)
def test_logs_rules_are_refused_with_their_key(tmp_path, mutate, path):
    data = config_data(tmp_path)
    mutate(data)
    with pytest.raises(ConfigError) as caught:
        validate_config(data)
    assert path in str(caught.value)


@pytest.mark.parametrize("kind", ["mongodb", "elasticsearch"])
def test_logs_is_refused_on_registries_without_pins(tmp_path, kind):
    data = config_data(tmp_path)
    if kind == "mongodb":
        data["backends"]["remote"] = {"kind": "mongodb", "uri_env": "SU_TEST_MONGO_URL", "database": "su"}
    else:
        data["backends"]["remote"] = {"kind": "elasticsearch", "hosts_env": "SU_TEST_ES_URL"}
    data["storage"]["catalog"] = "remote"
    with pytest.raises(ConfigError) as caught:
        validate_config(data)
    assert "pin registry" in str(caught.value) and "storage.catalog" in str(caught.value)


def test_unknown_logs_keys_are_refused(tmp_path):
    data = config_data(tmp_path)
    data["logs"]["window_pause"] = "01:00-05:00"
    with pytest.raises(ConfigError):
        validate_config(data)


def doc(**fields):
    return Document(id="d1", dataset="logs", kind="signature", title="sig", fields=fields)


def test_line_recipe_is_one_chunk_per_record_whatever_the_size():
    records = ["first record line\n  at frame one\n  at frame two", "second record", "x" * 3000]
    text = "\n\n".join(records)
    chunks = line_chunks(doc(exemplars=text), "exemplars", text, 24_576)
    assert [c.text for c in chunks] == records
    assert all(text[c.start : c.end] == c.text and c.recipe == "line" for c in chunks)


def test_line_recipe_refuses_a_record_over_the_bound():
    with pytest.raises(LineRecordTooLong):
        line_chunks(doc(exemplars="y" * 300), "exemplars", "y" * 300, 256)


def test_chunk_with_recipes_keeps_other_fields_sized():
    from su_multi_arm.core.config_logs import LineRecipeConfig

    document = doc(template="t " * 1000, exemplars="a\n\nb")
    chunks = chunk_with_recipes(document, 400, 40, [], LineRecipeConfig(fields=["exemplars"]))
    by_field: dict[str, list] = {}
    for chunk in chunks:
        by_field.setdefault(chunk.field, []).append(chunk)
    assert [c.text for c in by_field["exemplars"]] == ["a", "b"]
    assert len(by_field["template"]) > 1 and {c.recipe for c in by_field["template"]} == {"default"}
    assert [c.field for c in chunks][:2] == ["title", "template"]


def test_line_recipe_and_projections_enter_the_recipe_only_when_set(tmp_path):
    cfg = validate_config(config_data(tmp_path))
    recipe = chunking_recipe(cfg)
    assert "line_recipe" in recipe and "projections" in recipe
    plain = copy.deepcopy(cfg)
    plain.ingestion.line_recipe = None
    plain.ingestion.projections = None
    assert set(chunking_recipe(plain)) == {"size", "overlap", "field_order"}


def test_projection_helpers():
    from su_multi_arm.core.config_logs import ProjectionsConfig

    p = ProjectionsConfig(exclude_kinds={"vector": ["unit_day"], "lexical": ["unit_day"]}, embed_fields={"signature": ["template"]})
    assert p.excluded("lexical", "unit_day") and not p.excluded("graph", "unit_day")
    assert not p.embeds("unit_day", "header") and p.embeds("signature", "template") and not p.embeds("signature", "exemplars")
    assert p.embeds("document", "body")
    with pytest.raises(ValueError):
        ProjectionsConfig(embed_fields={"signature": []})
