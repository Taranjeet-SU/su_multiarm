"""S0 seams (unified merge plan §10.7.2): ``runtime/collection.py`` single-member view, the engine
attribute that constructs it (R6), and the ``collection`` / ``identity`` CLI stubs.

The single-member view is not called by any existing path; the engine's behaviour is unchanged
(every existing engine test is the evidence). The stubs read the configuration only, never build an
engine, and refuse the operations of later increments explicitly with exit code 1.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from su_multi_arm.cli import build_parser, main
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types_members import PinnedView
from su_multi_arm.runtime.collection import CollectionView, SingleMemberView, unserved_blocks

SHA = "d" * 64


def v2(tmp_path: Path) -> dict:
    return {
        "schema_version": 2,
        "dataset": "seams",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
    }


def v3(tmp_path: Path, registry: str | None = None) -> dict:
    data = {**v2(tmp_path), "schema_version": 3}
    data["collection"] = {
        "mode": "partitioned",
        "members": {
            "tickets": {"kind": "ticket", "sources": ["ticket"], "record_kinds": ["ticket"], "id_namespaces": ["ticket:"]},
            "logs": {"kind": "logs", "sources": ["logs"], "record_kinds": ["incident", "signature"], "key": "day", "cadence": "minutes", "id_namespaces": ["logs:"]},
        },
        "selection": {"source_quotas": {"ticket": 0.5, "logs": 0.5}},
    }
    if registry is not None:
        data["crosssource"] = {"identity": {"registry_path": registry, "sha256": SHA}}
    return data


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def run(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.argv", ["su-multi-arm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


# --- the view ----------------------------------------------------------------------------------------


def test_the_single_member_view_is_todays_engine(tmp_path):
    from su_multi_arm import Engine

    engine = Engine(validate_config(v2(tmp_path)))
    view = engine.view
    assert isinstance(view, SingleMemberView) and not view.partitioned
    assert view.member_ids() == ("default",)
    assert view.pin("f" * 32) == PinnedView(generation="f" * 32)
    assert view.pin("f" * 32).unit_ids() == ()
    assert view.describe() == {"mode": "single", "dataset": "seams", "members": ["default"]}


def test_the_view_serves_crosssource_blocks(tmp_path):
    # M2b-2 serves ``crosssource`` (registry grounding, two-halves planner, link display); other
    # unserved layer kinds and community strategies stay refused (test_config_v3_schema)
    config = validate_config(v3(tmp_path, registry="ids.sqlite"))
    assert unserved_blocks(config) == []
    try:
        CollectionView.for_config(config)
    except Exception as exc:  # this fixture's members are refused for their own reasons (fts5), not for crosssource
        assert "crosssource" not in str(exc)
    assert unserved_blocks(validate_config(v2(tmp_path))) == []
    assert unserved_blocks(validate_config(v3(tmp_path))) == []


def test_a_served_partitioned_config_gets_the_partitioned_view(tmp_path):
    from su_multi_arm.runtime.collection import PartitionedView

    data = v3(tmp_path)
    data["backends"]["local"]["lexical_engine"] = "bm25"
    view = CollectionView.for_config(validate_config(data))
    assert isinstance(view, PartitionedView) and view.partitioned
    assert view.member_ids() == ("logs", "tickets") and view.pin(None) == PinnedView()
    assert view.describe() == {"mode": "partitioned", "dataset": "seams", "members": ["logs", "tickets"], "adopted": [], "layers": []}
    assert not (tmp_path / "state").exists()  # constructing the view opens nothing


# --- CLI stubs ----------------------------------------------------------------------------------------


def test_the_stub_commands_parse():
    parser = build_parser()
    for argv in (["collection", "describe"], ["collection", "compose"], ["collection", "adopt", "--member", "support"], ["collection", "layers"], ["identity", "describe"], ["identity", "review"]):
        args = parser.parse_args(["--config", "c.yaml", *argv])
        assert args.command == argv[0]


def test_collection_describe_on_a_v2_config_is_the_single_member_view(tmp_path, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "--config", str(write(tmp_path, v2(tmp_path))), "collection", "describe")
    assert code == 0 and json.loads(out) == {"schema_version": 2, "dataset": "seams", "mode": "single", "members": ["default"], "served": True}
    assert not (tmp_path / "state").exists()  # no engine, no backend


def test_collection_describe_on_a_v3_config_lists_the_members(tmp_path, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, "--config", str(write(tmp_path, v3(tmp_path))), "collection", "describe")
    report = json.loads(out)
    assert code == 0 and report["mode"] == "partitioned" and report["served"] is False
    assert report["members"]["logs"]["catalog_kinds"] == ["signature"] and report["members"]["logs"]["lexical_stats"] == "query_time"
    assert report["source_quotas"] == {"ticket": 0.5, "logs": 0.5}
    assert not (tmp_path / "state").exists()


# ``identity review`` / ``identity report`` landed with M2b-1; ``collection compose`` with M1a;
# ``collection adopt`` with M1b (tests/unit/test_adopt_member_readonly.py): no pending command is left.
def test_no_collection_command_is_pending():
    from su_multi_arm.cli_collection import PENDING

    assert PENDING == {}


def test_collection_adopt_on_a_member_that_is_not_adopted_is_refused(tmp_path, monkeypatch, capsys):
    code, out, err = run(monkeypatch, capsys, "--config", str(write(tmp_path, v3(tmp_path))), "collection", "adopt", "--member", "tickets")
    assert code != 0 and out == "" and "not an adopted member" in err
    assert not (tmp_path / "state" / "collection" / "members").exists()


def test_identity_describe_checks_the_registry_digest(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "ids.sqlite"
    path = write(tmp_path, v3(tmp_path, registry="ids.sqlite"))
    code, out, _ = run(monkeypatch, capsys, "--config", str(path), "identity", "describe")
    assert code == 0 and json.loads(out)["digest"] == "missing"
    registry.write_bytes(b"synthetic registry")
    assert json.loads(run(monkeypatch, capsys, "--config", str(path), "identity", "describe")[1])["digest"] == "mismatch"
    data = v3(tmp_path, registry="ids.sqlite")
    data["crosssource"]["identity"]["sha256"] = hashlib.sha256(b"synthetic registry").hexdigest()
    report = json.loads(run(monkeypatch, capsys, "--config", str(write(tmp_path, data)), "identity", "describe")[1])
    # the digest matches but the file is not a registry: joins stay disabled, so nothing is served
    assert report == {"configured": True, "registry_path": str(registry), "sha256": data["crosssource"]["identity"]["sha256"], "digest": "match", "served": False,
                      "joins_enabled": False, "consumers": [], "reason": "export_invalid"}
    assert json.loads(run(monkeypatch, capsys, "--config", str(write(tmp_path, v2(tmp_path))), "identity", "describe")[1]) == {"configured": False}


def test_doctor_reports_a_v3_collection_as_not_served(tmp_path, monkeypatch, capsys):
    # M1a serves partitioned collections; this one keeps the fts5 default, which new members refuse
    code, out, _ = run(monkeypatch, capsys, "--config", str(write(tmp_path, v3(tmp_path))), "doctor")
    report = json.loads(out)
    assert code == 1 and "fts5 is refused" in report["error"] and report["path"] == "backends.local.lexical_engine"
    code, out, _ = run(monkeypatch, capsys, "--config", str(write(tmp_path, v3(tmp_path, registry="ids.sqlite"))), "doctor")
    assert code == 1 and "fts5 is refused" in json.loads(out)["error"]  # crosssource is served since M2b-2
