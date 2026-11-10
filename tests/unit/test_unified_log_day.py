"""F0: the deterministic large-log-day generator (run here on a small spec; the default 600k-chunk
day is generated on demand in the external workspace for the M2a benchmark)."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from su_multi_arm.core.config_logs import LINE_TEXT_FIELDS
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.testing.fixtures.unified import LogDaySpec, generate_log_day
from su_multi_arm.testing.fixtures.unified.log_day import document_chunks, field_chunks, main, read_refresh

SMALL = LogDaySpec(day=date(2026, 11, 8), target_chunks=20_000, tenants=20, services_per_tenant=2, rules=30, refresh_minutes=60, text_pool=200)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("logday") / "a"
    return out, generate_log_day(SMALL, out)


def final_state(out: Path, manifest: dict) -> dict:
    state = {}
    for r in manifest["refreshes"]:
        path = out / r["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == r["sha256"]
        docs = read_refresh(path)
        assert len(docs) == r["upserts"]
        for d in docs:
            state[d.id] = d
    return state


def test_default_spec_is_the_benchmark_day():
    spec = LogDaySpec()
    assert spec.target_chunks == 600_000 and spec.refresh_minutes == 5 and 1440 // spec.refresh_minutes == 288


def test_final_state_hits_the_chunk_target_and_matches_the_chunker(generated):
    out, manifest = generated
    state = final_state(out, manifest)
    totals = manifest["totals"]
    assert totals["documents_final"] == len(state) and totals["refreshes"] == 24
    assert abs(totals["chunks_final"] - SMALL.target_chunks) < 10
    real = sum(len(chunk_document(d, SMALL.chunk_size, SMALL.chunk_overlap, [])) for d in state.values())
    assert real == totals["chunks_final"] == sum(document_chunks(d, SMALL.chunk_size, SMALL.chunk_overlap) for d in state.values())
    assert totals["versions_emitted"] > totals["documents_final"]  # updates supersede earlier versions
    assert sum(r["updated"] for r in manifest["refreshes"]) > 0
    assert set(totals["documents_by_kind"]) >= {"incident", "signature_day", "episode_cell", "signature", "unit_day", "summary_coverage", "unclassified_example"}
    assert totals["unique_embed_texts_final"] < totals["embed_texts_final"]  # dedupe has something to remove


def test_ids_follow_the_write_path(generated):
    out, manifest = generated
    state = final_state(out, manifest)
    signatures = [d for d in state.values() if d.kind == "signature"]
    assert len(signatures) == SMALL.rules
    day = SMALL.day.isoformat()
    for d in state.values():
        assert d.id.startswith(f"logs:{d.kind}:")
        if d.kind in {"incident", "signature_day", "signature"}:
            assert day not in d.id and day.replace("-", ".") not in d.id


def test_embed_fields_exclude_log_line_text():
    for kind, fields in LogDaySpec().embed_fields:
        assert not set(fields) & set(LINE_TEXT_FIELDS.get(kind, ()))


def test_generation_is_deterministic(generated, tmp_path):
    out, manifest = generated
    again = generate_log_day(SMALL, tmp_path / "b")
    assert (tmp_path / "b" / "manifest.json").read_bytes() == (out / "manifest.json").read_bytes()
    assert [r["sha256"] for r in again["refreshes"]] == [r["sha256"] for r in manifest["refreshes"]]
    other = generate_log_day(LogDaySpec(**{**SMALL.__dict__, "seed": 1}), tmp_path / "c")
    assert other["refreshes"][0]["sha256"] != manifest["refreshes"][0]["sha256"]


def test_refuses_the_repository_and_non_empty_targets(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="inside the repository"):
        generate_log_day(SMALL, repo / "build" / "logday")
    assert not (repo / "build" / "logday").exists()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        generate_log_day(SMALL, tmp_path / "full")


def test_spec_validation_and_chunk_arithmetic():
    with pytest.raises(ValueError):
        LogDaySpec(refresh_minutes=7)
    with pytest.raises(ValueError):
        LogDaySpec(target_chunks=1000)
    assert [field_chunks(n, 1600, 160) for n in (1, 1600, 1601, 3040, 3041)] == [1, 1, 2, 2, 3]


def test_cli_with_the_default_shape_on_a_smaller_day(tmp_path, capsys):
    assert main(["--out", str(tmp_path / "cli"), "--chunks", "40000", "--seed", "3"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["refreshes"] == 288 and abs(printed["chunks_final"] - 40_000) < 10
