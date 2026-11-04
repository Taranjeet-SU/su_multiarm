"""2.0.0a2 serving/config slice: unimplemented keys refused (review risk R3), schema errors
aggregated with key paths (QA-11), concise CLI errors (QA-11), ``config upgrade --output`` rebasing
relative paths (QA-10), and lazily checked API keys of non-default pipelines (QA-14). Synthetic
configs only; no network, no model calls."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from su_multi_arm import Document, Engine, SearchRequest
from su_multi_arm.compat.config_v1 import upgrade_text
from su_multi_arm.core.config import Config, rebase_paths, validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.providers._chat import MissingSecret

ROOT = Path(__file__).resolve().parents[2]
PLANNER_ENV_NAME = "SU_TEST_A2_PLANNER_KEY"
BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
    "pipeline": {"arms": {"lexical": {"backend": "local"}}},
}
ROLE = {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": PLANNER_ENV_NAME, "capabilities": {"structured_output": "json_schema"}}


def data(**overrides):
    out = copy.deepcopy(BASE)
    out.update(overrides)
    return out


# --- risk R3: configured-but-unconsumed keys ------------------------------------------------------


@pytest.mark.parametrize(
    "extra, path",
    [
        ({"extraction": {"ontology": "ontology.yaml"}}, "extraction.ontology"),
        ({"models": {"roles": {"embedder": {"provider": "hash"}, "judge": ROLE}}}, "models.roles.judge"),
        ({"models": {"roles": {"embedder": {"provider": "hash"}, "cache_gate": ROLE}}}, "models.roles.cache_gate"),
        ({"pipeline": {"arms": {"lexical": {"backend": "local"}}, "guards": ["exact_set"]}}, "pipeline.guards"),
        ({"sessions": {"memory": {"semantic": {}}}}, "sessions.memory"),
    ],
)
def test_unimplemented_keys_are_refused_with_their_path(extra, path):
    with pytest.raises(ConfigError) as caught:
        validate_config(data(**extra))
    assert caught.value.path == path or path in str(caught.value)
    if "roles" in str(extra) or "ontology" in str(extra):
        assert "not implemented in this release" in str(caught.value)


def test_cache_arm_is_refused():
    with pytest.raises(ConfigError, match="pipeline.arms: cache: not implemented in this release"):
        validate_config(data(pipeline={"arms": {"lexical": {"backend": "local"}, "cache": {"backend": "local"}}}))


def test_unimplemented_keys_are_not_in_the_schema():
    roles = Config.model_json_schema()["$defs"]["ModelRoles"]["properties"]
    extraction = Config.model_json_schema()["$defs"]["ExtractionConfig"]["properties"]
    assert "judge" not in roles and "cache_gate" not in roles and "ontology" not in extraction


# Fields read only through their parent object or by an injected component; reviewed by hand.
UNREAD_BY_NAME = {
    # The classifier artifact manifest describes the artifact; the injected ClassifierModel owns
    # feature extraction (decision/classifier.py). Follow-up: check it against the runtime (Phase 2).
    "pipeline.decision.artifact.manifest.features",
}


def test_every_config_key_has_a_reader():
    """Walk the v2 model and require each field name to be read somewhere outside the config
    modules (name-based, so it catches a wholly unread key, not a key read under a shared name)."""
    import typing

    from pydantic import BaseModel

    source = "\n".join(
        p.read_text()
        for p in (ROOT / "src/su_multi_arm").rglob("*.py")
        if not any(part in p.as_posix() for part in ("core/config.py", "core/config_", "core/request.py", "compat/", "core/schema.py"))
    )
    seen: set[type] = set()
    unread: list[str] = []

    def annotations(annotation):
        yield annotation
        for arg in typing.get_args(annotation):
            yield from annotations(arg)

    def walk(model, prefix):
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            if not any(token in source for token in (f".{name}", f'"{name}"', f"'{name}'", f"{name}=")):
                unread.append(prefix + name)
            for t in annotations(info.annotation):
                if isinstance(t, type) and issubclass(t, BaseModel):
                    walk(t, f"{prefix}{name}.")

    walk(Config, "")
    assert set(unread) <= UNREAD_BY_NAME, sorted(set(unread) - UNREAD_BY_NAME)


# --- QA-11: aggregated schema errors and concise CLI errors ---------------------------------------


def test_schema_errors_are_aggregated_with_key_paths():
    bad = data(bogus_one=1, bogus_two=2, budgets={"max_llm_calls": 0})
    with pytest.raises(ConfigError) as caught:
        validate_config(bad)
    paths = {d.path for d in caught.value.diagnostics}
    assert {"bogus_one", "bogus_two", "budgets.max_llm_calls"} <= paths
    assert "3 configuration errors" in str(caught.value)
    assert {d.message for d in caught.value.diagnostics if d.path.startswith("bogus")} == {"unknown key"}


def test_a_single_schema_error_keeps_its_message_and_path():
    with pytest.raises(ConfigError) as caught:
        validate_config(data(bogus=1))
    assert caught.value.path == "bogus" and str(caught.value) == "bogus: unknown key"


def run_main(monkeypatch, capsys, *argv):
    from su_multi_arm.cli import main

    monkeypatch.setattr("sys.argv", ["su-multi-arm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_config_validate_lists_every_problem_without_traceback(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 2\ndataset: x\nbogus_one: 1\nbogus_two: 2\n")
    code, out, err = run_main(monkeypatch, capsys, "--config", str(path), "config", "validate")
    assert code == 2 and "Traceback" not in err
    assert "bogus_one: unknown key" in err and "bogus_two: unknown key" in err and "storage" in err
    with pytest.raises(ConfigError):  # --debug re-raises with the traceback
        run_main(monkeypatch, capsys, "--debug", "--config", str(path), "config", "validate")


def test_cli_runtime_errors_are_one_line_with_an_exit_code(tmp_path, monkeypatch, capsys):
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump(data(backends={"local": {"kind": "sqlite", "path": "state"}})))
    code, out, err = run_main(monkeypatch, capsys, "--config", str(config), "search", "anything")
    assert code == 3 and err.startswith("error: index not ready") and "Traceback" not in err
    code, out, err = run_main(monkeypatch, capsys, "--config", str(tmp_path / "missing.yaml"), "status")
    assert code == 1 and err.startswith("error: FileNotFoundError") and "Traceback" not in err


# --- QA-10: upgrade --output rebases relative paths ----------------------------------------------


def test_rebase_paths_keeps_absolute_and_home_paths(tmp_path):
    mapping = {
        "backends": {"local": {"kind": "sqlite", "path": "state"}, "abs": {"kind": "sqlite", "path": "/srv/state"}, "home": {"kind": "sqlite", "path": "~/s"}},
        "pipeline": {"generation": {"prompt_file": "prompts/g.txt"}, "arms": {"exact_id": {"alias_map": "aliases.yaml"}}},
        "pipelines": {"p": {"decision": {"prompt_file": "prompts/d.txt"}}},
        "budgets": {"price_table": "prices.yaml"},
    }
    changed = rebase_paths(mapping, tmp_path / "examples", tmp_path)
    assert mapping["backends"]["local"]["path"] == "examples/state"
    assert mapping["backends"]["abs"]["path"] == "/srv/state" and mapping["backends"]["home"]["path"] == "~/s"
    assert mapping["pipeline"]["generation"]["prompt_file"] == "examples/prompts/g.txt"
    assert mapping["pipeline"]["arms"]["exact_id"]["alias_map"] == "examples/aliases.yaml"
    assert mapping["pipelines"]["p"]["decision"]["prompt_file"] == "examples/prompts/d.txt"
    assert mapping["budgets"]["price_table"] == "examples/prices.yaml"
    assert sorted(changed) == sorted(
        ["backends.local.path", "pipeline.generation.prompt_file", "pipeline.arms.exact_id.alias_map", "pipelines.p.decision.prompt_file", "budgets.price_table"]
    )
    deeper = {"backends": {"local": {"kind": "sqlite", "path": "state"}}}
    rebase_paths(deeper, tmp_path, tmp_path / "out" / "v2")
    assert deeper["backends"]["local"]["path"] == "../../state"


def test_upgrade_to_another_directory_names_the_same_state(tmp_path):
    text = (ROOT / "examples/local.yaml").read_text()
    same, _ = upgrade_text(text, source_dir=tmp_path, target_dir=tmp_path)
    moved, _ = upgrade_text(text, source_dir=tmp_path / "examples", target_dir=tmp_path)
    v1_path = Path(yaml.safe_load(text).get("storage_path", "state"))
    assert Path(yaml.safe_load(same)["backends"]["local"]["path"]) == v1_path
    assert Path(yaml.safe_load(moved)["backends"]["local"]["path"]) == Path("examples") / v1_path
    assert "# Relative paths rebased" in moved and "# Relative paths rebased" not in same


def test_cli_upgrade_output_elsewhere_resolves_to_the_original_state(tmp_path, monkeypatch, capsys):
    from su_multi_arm.core.config import load_config

    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "local.yaml").write_text((ROOT / "examples/local.yaml").read_text())
    for name in ("prompts",):
        if (ROOT / "examples" / name).exists():
            subprocess.run(["cp", "-r", str(ROOT / "examples" / name), str(examples / name)], check=True)
    out = tmp_path / "elsewhere" / "local_v2.yaml"
    out.parent.mkdir()
    code, stdout, err = run_main(monkeypatch, capsys, "--config", str(examples / "local.yaml"), "config", "upgrade", "--output", str(out))
    assert code == 0, err
    original = load_config(examples / "local.yaml", warn=False)
    assert load_config(out).backends["local"].path == original.backends["local"].path


# --- QA-14: keys of non-default pipelines are checked on first use --------------------------------


def two_pipelines(default_kind="rules"):
    roles = {"embedder": {"provider": "hash", "dimensions": 64}, "planner": ROLE}
    return validate_config(
        data(
            models={"roles": roles},
            pipeline={"decision": {"kind": default_kind}, "arms": {"lexical": {"backend": "local"}}},
            pipelines={"planned": {"decision": {"kind": "llm"}}},
            request={"pipelines": ["planned"]},
        )
    )


def test_missing_key_of_a_named_pipeline_does_not_block_ingest_or_default_search(monkeypatch):
    monkeypatch.delenv(PLANNER_ENV_NAME, raising=False)
    engine = Engine(two_pipelines())
    assert engine.ingest([Document(id="d1", dataset="demo", title="t", fields={"body": "rounding note"})])["status"] == "complete"
    assert [h.document_id for h in engine.search(SearchRequest(query="rounding")).hits] == ["d1"]
    assert engine.missing_keys() == {"pipeline:planned": {"planner": [PLANNER_ENV_NAME]}}
    with pytest.raises(MissingSecret, match=f"pipeline 'planned'.*{PLANNER_ENV_NAME} is not set"):
        engine.search(SearchRequest(query="rounding", pipeline="planned"))
    with pytest.raises(MissingSecret):  # still checked (fail closed) until the key appears
        engine.answer(SearchRequest(query="rounding", pipeline="planned"))
    monkeypatch.setenv(PLANNER_ENV_NAME, "sk-test-not-a-key")
    engine._require_keys("planned", "llm")  # present now: the deferred check passes and is dropped
    assert ("planned", "llm") not in engine._deferred_keys and engine.missing_keys() == {}


def test_missing_key_of_the_default_pipeline_still_fails_construction(monkeypatch):
    monkeypatch.delenv(PLANNER_ENV_NAME, raising=False)
    with pytest.raises(ConfigError, match=PLANNER_ENV_NAME):
        Engine(two_pipelines(default_kind="llm"))


def test_doctor_reports_missing_keys_per_pipeline(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(PLANNER_ENV_NAME, raising=False)
    config = tmp_path / "c.yaml"
    raw = data(
        models={"roles": {"embedder": {"provider": "hash", "dimensions": 64}, "planner": ROLE}},
        pipelines={"planned": {"decision": {"kind": "llm"}}},
        request={"pipelines": ["planned"]},
    )
    config.write_text(yaml.safe_dump(raw))
    code, out, err = run_main(monkeypatch, capsys, "--config", str(config), "doctor")
    assert code == 0, err
    assert json.loads(out)["missing_keys"] == {"pipeline:planned": {"planner": [PLANNER_ENV_NAME]}}


# --- serving.ui.theme path (real-run fix) -----------------------------------------------------------


def test_ui_theme_path_is_relative_to_the_config_and_checked_by_validate_and_doctor(tmp_path, monkeypatch, capsys):
    """A relative ``serving.ui.theme`` resolves against the config file's directory (like every other
    path) and ``config validate``/``doctor`` refuse a missing theme instead of ``serve`` failing."""
    from su_multi_arm.core.config import load_config

    conf = tmp_path / "conf"
    (conf / "brand").mkdir(parents=True)
    (conf / "brand" / "theme.json").write_text(json.dumps({"name": "synthetic", "colors": {"brand": "#225588"}}))
    good = conf / "good.yaml"
    good.write_text(yaml.safe_dump(data(serving={"ui": {"theme": "brand/theme.json"}})))
    monkeypatch.chdir(tmp_path)  # another working directory: the path must not depend on it
    assert load_config(good).serving.ui.theme == (conf / "brand" / "theme.json").resolve()
    code, out, err = run_main(monkeypatch, capsys, "--config", str(good), "config", "validate")
    assert code == 0 and json.loads(out)["config"] == "valid", err
    code, out, err = run_main(monkeypatch, capsys, "--config", str(good), "doctor")
    assert code == 0, err
    bad = conf / "bad.yaml"
    bad.write_text(yaml.safe_dump(data(serving={"ui": {"theme": "brand/absent.json"}})))
    code, out, err = run_main(monkeypatch, capsys, "--config", str(bad), "config", "validate")
    assert code != 0 and "theme" in err + out and "Traceback" not in err
    code, out, err = run_main(monkeypatch, capsys, "--config", str(bad), "doctor")
    assert code == 1 and json.loads(out)["config"] == "invalid" and json.loads(out)["path"] == "serving.ui.theme"


def test_rebase_paths_moves_the_ui_theme_with_the_config(tmp_path):
    raw = data(serving={"ui": {"theme": "brand/theme.json"}})
    changed = rebase_paths(raw, tmp_path / "a", tmp_path / "b")
    assert "serving.ui.theme" in changed and raw["serving"]["ui"]["theme"] == "../a/brand/theme.json"
