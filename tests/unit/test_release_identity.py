"""Release identity and shipped examples (release review B4, B5, B6, N10).

One version everywhere (pyproject, ``__version__``, the HTTP app, ``--version``); the provider and
``local``/``all`` extras exist; every ``examples/v2`` file is a schema_version 2 config that loads,
and the local one ingests and searches end to end in a temporary directory; the v1 examples keep
loading through the upgrade path; tracked files carry no origin-service names."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

import su_multi_arm
from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import load_config
from su_multi_arm.ingestion.normalize import parse_documents

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
V2_EXAMPLES = sorted((ROOT / "examples/v2").glob("*.yaml"))
V1_EXAMPLES = sorted(p for p in (ROOT / "examples").glob("*.yaml"))


def test_one_version_everywhere(monkeypatch, capsys):
    version = PYPROJECT["project"]["version"]
    assert version == "2.0.0a3" and su_multi_arm.__version__ == version
    pytest.importorskip("fastapi")
    from su_multi_arm.serving.http import create_app
    from serving_support import memory_config

    assert create_app(engine=Engine(memory_config(None))).version == version
    from su_multi_arm import cli

    monkeypatch.setattr(sys, "argv", ["su-multi-arm", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0 and capsys.readouterr().out.strip() == f"su-multi-arm {version}"


def test_provider_and_bundle_extras_exist():
    extras = PYPROJECT["project"]["optional-dependencies"]
    assert extras["openai"] == [] and extras["anthropic"] == []  # providers use httpx (core dependency)
    assert extras["local"] == extras["semantic"]
    bundled = extras["all"][0].split("[", 1)[1].rstrip("]").split(",")
    for name in ("postgres", "sql", "neo4j", "elasticsearch", "mongodb", "opensearch", "api", "mcp", "semantic"):
        assert name in bundled and name in extras, name
    assert "dev" not in bundled


@pytest.mark.parametrize("path", V2_EXAMPLES, ids=lambda p: p.name)
def test_every_v2_example_loads(path):
    assert yaml.safe_load(path.read_text())["schema_version"] == 2
    config = load_config(path, check_plugins=False, warn=False)
    assert config.schema_version == 2 and config.dataset == "demo"


def test_v2_examples_cover_local_mixed_and_hosted():
    assert {p.stem for p in V2_EXAMPLES} >= {"local", "mixed_backends", "hosted_models"}
    mixed = load_config(ROOT / "examples/v2/mixed_backends.yaml", check_plugins=False, warn=False)
    assert {b.kind for b in mixed.backends.values()} == {"postgres", "opensearch", "neo4j"}


def test_local_v2_example_runs_end_to_end(tmp_path):
    shutil.copy(ROOT / "examples/v2/local.yaml", tmp_path / "local.yaml")  # state/ lands in tmp_path
    config = load_config(tmp_path / "local.yaml", warn=False)
    engine = Engine(config)
    lines = (ROOT / "examples/documents.jsonl").read_text().splitlines()
    import json

    engine.ingest(parse_documents([json.loads(line) for line in lines], config))
    result = engine.search(SearchRequest(query="exemption certificate"))
    assert "kb:certificate" in {h.document_id for h in result.hits}


@pytest.mark.parametrize("path", V1_EXAMPLES, ids=lambda p: p.name)
def test_v1_examples_still_load_through_the_upgrade(path):
    data = yaml.safe_load(path.read_text())
    assert data.get("schema_version", 1) == 1  # top-level examples are v1 on purpose (upgrade path)
    assert load_config(path, check_plugins=False, warn=False).schema_version == 2


DENYLIST = r"vertex|searchunify|grazitti|salesforce|jira|zoomin|192\.168\.|/home/"


def test_tracked_files_carry_no_origin_service_names():
    """Docs hygiene denylist (plan §6.2 docs job). Review reports under docs/plans/reviews quote the
    findings they raised and are excluded; everything else that ships must be clean."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    this = Path(__file__).resolve().relative_to(ROOT).as_posix()  # carries the denylist itself
    files = [f for f in tracked if not f.startswith("docs/plans/reviews/") and f != this]
    hits = subprocess.run(["grep", "-nIiE", DENYLIST, "--", *files], cwd=ROOT, capture_output=True, text=True)
    assert hits.stdout == "", hits.stdout
