"""``su-multi-arm collection register|compose|status`` (unified merge M1a; ``cli_collection.py``).

Synthetic F0 documents; local sqlite + hash embedder; no service, no model call.
"""

from __future__ import annotations

import json

import yaml

from su_multi_arm.cli import main
from tests.partition_support import config, f0, f0_members, member_block


def run(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.argv", ["su-multi-arm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, out.out, out.err


def test_register_compose_status_and_search(tmp_path, monkeypatch, capsys):
    fx = f0()
    members = f0_members(fx)
    subset = {"tracker": members["tracker"], "analytics": members["analytics"]}
    cfg = config(tmp_path / "state", schema=fx.manifest["metadata_schema"], collection=member_block(fx, subset))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(json.loads(cfg.model_dump_json())))
    for name, units in subset.items():
        for key, docs in units:
            data = tmp_path / f"{name}-{key.replace(':', '-')}.jsonl"
            data.write_text("\n".join(d.model_dump_json() for d in docs))
            code, out, err = run(monkeypatch, capsys, "--config", str(path), "collection", "register", str(data), "--member", name, "--key", key)
            assert code == 0, err
            assert json.loads(out)["registered"]["unit_id"] == f"{name}/{key}"
    code, out, err = run(monkeypatch, capsys, "--config", str(path), "collection", "compose", "--unit", "tracker/snapshot:2026-11-08", "--expected", "none")
    assert code == 0, err
    first = json.loads(out)
    assert first["members"] == ["tracker/snapshot:2026-11-08"] and first["base"] is None
    code, out, err = run(monkeypatch, capsys, "--config", str(path), "collection", "compose", "--expected", first["generation"])
    assert code == 0, err
    second = json.loads(out)
    assert len(second["members"]) == 3 and second["documents"] == sum(len(d) for units in subset.values() for _, d in units)
    code, out, _ = run(monkeypatch, capsys, "--config", str(path), "collection", "compose", "--expected", first["generation"])
    assert code != 0  # stale base: compare-and-set refused
    code, out, _ = run(monkeypatch, capsys, "--config", str(path), "collection", "status")
    status = json.loads(out)
    assert code == 0 and status["generation"]["generation"] == second["generation"] and len(status["members"]) == 3
    code, out, _ = run(monkeypatch, capsys, "--config", str(path), "search", "crawler timeout")
    assert code == 0 and json.loads(out)["generation"] == second["generation"]
    code, out, _ = run(monkeypatch, capsys, "--config", str(path), "collection", "describe")
    assert json.loads(out)["served"] is True
