"""The generated backend/provider tables (README, docs/backends.md, docs/providers.md) match the code."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_capability_tables.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_capability_tables", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_tables_are_current():
    module = _load()
    stale = [str(path.relative_to(ROOT)) for path, content in module.outputs().items() if path.read_text() != content]
    assert not stale, f"run python scripts/gen_capability_tables.py; stale: {stale}"


def test_every_backend_kind_has_evidence_and_every_capability_a_column():
    module = _load()
    from su_multi_arm.backends.capabilities import CAPABILITIES, MATRIX

    table = module.backends_table()
    for kind in MATRIX:
        assert f"| `{kind}` |" in table
    header = table.splitlines()[0]
    for capability in CAPABILITIES:
        assert f" {capability} " in header


def test_evidence_refuses_unmapped_backend(monkeypatch):
    module = _load()
    monkeypatch.delitem(module.BACKEND_TESTS, "neo4j")
    with pytest.raises(SystemExit, match="no evidence mapping"):
        module.backends_table()


def test_evidence_refuses_real_service_claim_without_external_marker(monkeypatch, tmp_path):
    module = _load()
    fake_root = tmp_path
    (fake_root / "tests").mkdir()
    (fake_root / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    monkeypatch.setattr(module, "ROOT", fake_root)
    monkeypatch.setitem(module.BACKEND_TESTS, "postgres", "tests/test_x.py")
    with pytest.raises(SystemExit, match="not marked external"):
        module._backend_evidence("postgres")


def test_readme_without_markers_is_refused():
    module = _load()
    with pytest.raises(SystemExit, match="markers"):
        module.render_readme("# no markers here\n")
