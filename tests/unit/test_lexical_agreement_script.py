"""``scripts/lexical_agreement.py``: metric definitions and an offline (sqlite-only) report run."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lexical_agreement.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("lexical_agreement", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jaccard_and_kendall_tau(mod):
    assert mod.jaccard([], []) == 1.0
    assert mod.jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    assert mod.kendall_tau(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    assert mod.kendall_tau(["a", "b", "c"], ["c", "b", "a"]) == -1.0
    assert mod.kendall_tau(["a", "x", "b"], ["b", "y", "a"]) == -1.0  # only shared items count
    assert mod.kendall_tau(["a"], ["a", "b"]) is None  # fewer than two shared items


def test_query_list_covers_every_kind(mod):
    assert {q.kind for _, q in mod.QUERIES} == {"terms", "phrase", "boolean"}
    assert len(mod.QUERIES) >= 20


def test_unset_engines_are_listed_not_silently_dropped(mod, tmp_path, monkeypatch):
    for name in ("SU_TEST_POSTGRES_DSN", "SU_TEST_OPENSEARCH_URL", "SU_TEST_ELASTICSEARCH_URL", "SU_TEST_MONGO_URI"):
        monkeypatch.delenv(name, raising=False)
    out = tmp_path / "report.md"
    assert mod.main(["--workdir", str(tmp_path / "state"), "--output", str(out)]) == 0
    text = out.read_text()
    assert "sqlite (`fts5_unicode61`)" in text
    assert "Not run (variable unset): postgres, opensearch, elasticsearch, mongodb" in text
    assert "| `t:certificate` | 9 |" in text  # the shipped examples, chunked once
