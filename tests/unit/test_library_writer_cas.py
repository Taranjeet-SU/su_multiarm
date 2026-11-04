"""CAS-safe writer for the system-owned library files (addendum §3.6.3; owner amendment 2026-11-04)."""

from __future__ import annotations

import json

import pytest
import yaml

from su_multi_arm.extraction.patterns.library import Decision, PatternEntry
from su_multi_arm.extraction.patterns.writer import (
    SystemFileConflict,
    SystemFileCorrupt,
    SystemFileRefused,
    append_decision,
    append_learned,
    append_proposal,
    append_rows,
)
from tests.pattern_fixtures import FORMAT, write_library

ROW = {"op": "approve", "id": "msg.a", "by": "user:a", "at": "2026-11-04T00:00:00Z"}


def test_library_yaml_and_unknown_files_are_never_written(tmp_path):
    root = write_library(tmp_path, [FORMAT])
    before = (root / "library.yaml").read_bytes()
    for name in ("library.yaml", "other.yaml", "../library.yaml"):
        with pytest.raises(SystemFileRefused):
            append_rows(root, name, [ROW])
    assert (root / "library.yaml").read_bytes() == before


def test_appends_keep_existing_bytes(tmp_path):
    root = write_library(tmp_path, [FORMAT])
    (root / "decisions.yaml").write_text("# reviewed by hand\n- {op: approve, id: msg.a, by: 'user:a', at: '2026-11-03T00:00:00Z'}")
    before = (root / "decisions.yaml").read_bytes()
    sha = append_rows(root, "decisions.yaml", [ROW])
    after = (root / "decisions.yaml").read_bytes()
    assert after.startswith(before) and len(yaml.safe_load(after)) == 2
    assert json.loads((root / ".writer_state.json").read_text())["decisions.yaml"] == sha


def test_concurrent_save_between_read_and_rename_is_detected_and_retried(tmp_path):
    root = write_library(tmp_path, [FORMAT])
    append_rows(root, "proposed.yaml", [{"id": "first"}])
    saves = []

    def concurrent_save():
        if not saves:  # a human (or another writer) saves once, between our read and our rename
            with open(root / "proposed.yaml", "a") as handle:
                handle.write("- {id: by-hand}\n")
            saves.append(1)

    append_rows(root, "proposed.yaml", [{"id": "second"}], _before_replace=concurrent_save)
    assert [r["id"] for r in yaml.safe_load((root / "proposed.yaml").read_text())] == ["first", "by-hand", "second"]


def test_persistent_contention_is_a_conflict_and_writes_nothing(tmp_path):
    root = write_library(tmp_path, [FORMAT])
    counter = iter(range(100))

    def always():
        with open(root / "proposed.yaml", "a") as handle:
            handle.write(f"- {{id: noise{next(counter)}}}\n")

    with pytest.raises(SystemFileConflict):
        append_rows(root, "proposed.yaml", [{"id": "mine"}], retries=3, _before_replace=always)
    assert "mine" not in (root / "proposed.yaml").read_text()
    assert not list(root.glob(".*.tmp"))


def test_corrupt_system_file_is_refused_not_repaired(tmp_path):
    root = write_library(tmp_path, [FORMAT])
    (root / "learned.yaml").write_text("not: a list\n")
    with pytest.raises(SystemFileCorrupt):
        append_rows(root, "learned.yaml", [{"id": "x"}])
    assert (root / "learned.yaml").read_text() == "not: a list\n"


def test_typed_appenders_check_status(tmp_path):
    root = write_library(tmp_path, [FORMAT])
    entry = PatternEntry(id="msg.a", kind="message", status="proposed", regex=r"^a (?P<x>[a-z]+)$", signature={"id": "a"})
    append_proposal(root, entry)
    with pytest.raises(ValueError):
        append_learned(root, entry)
    append_learned(root, entry.model_copy(update={"status": "approved"}))
    append_decision(root, Decision.model_validate(ROW))
    assert {p.name for p in root.iterdir()} >= {"proposed.yaml", "learned.yaml", "decisions.yaml"}
