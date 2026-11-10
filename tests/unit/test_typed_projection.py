"""Typed projection (typed-query plan M4): builder, read-only store, catalog and lifecycle.

Synthetic fixtures (``tests/typed_support.py``); sqlite and in-memory projection files.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from su_multi_arm import Engine
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.errors import BackendError, GenerationInUse
from su_multi_arm.core.types import GenerationId, Provenance, StructuredRecord
from su_multi_arm.projection import MANIFEST, ProjectionBuilder, ProjectionCatalog, ProjectionSpec, open_readonly, tenant_file
from su_multi_arm.projection.builder import ALL_ROWS

import typed_support as ts

GEN = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)


def spec(access: str | None = "tenant") -> ProjectionSpec:
    return ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), ts.schema(), access)


def build(records=None, access: str | None = "tenant"):
    builder = ProjectionBuilder(spec(access), ts.schema())
    builder.add(records if records is not None else ts.records())
    return builder.seal(GEN)


def rows(data: bytes, sql: str):
    db = sqlite3.connect(":memory:")
    db.deserialize(data)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_rows_are_split_per_tenant_and_counted():
    files, manifest = build()
    assert manifest["tables"]["ticket"]["rows"] == 6 and manifest["tables"]["comment"]["rows"] == 6 and manifest["tables"]["asset"]["rows"] == 2
    assert manifest["skipped_records"] == 2  # notes have no table
    by_file = manifest["relations"]["comment_parent"]["dangling_by_file"]
    assert manifest["relations"]["comment_parent"]["dangling"] == 1 == by_file[ALL_ROWS] == by_file[tenant_file("t_beta")]
    assert by_file[tenant_file("t_alpha")] == 0 and by_file[tenant_file(ts.CANARY)] == 0
    names = {tenant_file(t) for t in (*ts.TENANTS, ts.CANARY)}
    assert set(manifest["tenant_files"]) == names and set(files) == names | {ALL_ROWS}
    assert all(t not in name for name in files for t in (*ts.TENANTS, ts.CANARY))  # no tenant value in a path
    alpha = files[tenant_file("t_alpha")]
    assert rows(alpha, 'SELECT DISTINCT "tenant" FROM "ticket" UNION SELECT DISTINCT "tenant" FROM "comment"') == [("t_alpha",)]
    assert rows(files[ALL_ROWS], 'SELECT COUNT(*) FROM "ticket"') == [(6,)]
    assert manifest["tenant_files"][tenant_file("t_beta")] == 2 + 2 + 1


def test_rows_without_an_access_value_go_only_to_the_all_rows_file():
    extra = StructuredRecord(schema_id="document", id="tk-x", values={"kind": "ticket", "status": "open"}, provenance=Provenance(extractor="t"))
    files, manifest = build([*ts.records(), extra])
    assert manifest["unscoped_rows"] == 1
    assert rows(files[ALL_ROWS], "SELECT id FROM ticket WHERE tenant IS NULL") == [("tk-x",)]
    assert all(rows(files[name], "SELECT COUNT(*) FROM ticket WHERE tenant IS NULL") == [(0,)] for name in manifest["tenant_files"])


def test_rejected_values_become_null_and_are_counted():
    bad = StructuredRecord(
        schema_id="document",
        id="tk-bad",
        values={"kind": "ticket", "tenant": "t_alpha", "status": "unknown", "priority": "high", "opened_at": "2024-03-01T10:00:00", "hours": True},
        provenance=Provenance(extractor="t"),
    )
    files, manifest = build([*ts.records(), bad])
    rejected = manifest["tables"]["ticket"]["rejected"]
    assert rejected["status"] == 1 and rejected["priority"] == 1 and rejected["opened_at"] == 1 and rejected["hours"] == 1
    row = rows(files[ALL_ROWS], "SELECT status, priority, opened_at, hours, _rejected FROM ticket WHERE id = 'tk-bad'")[0]
    assert row[:4] == (None, None, None, None) and json.loads(row[4]) == ["hours", "opened_at", "priority", "status"]
    assert rows(files[ALL_ROWS], "SELECT opened_at FROM ticket WHERE id = 'tk-4'") == [("2024-03-02T10:00:00.000000+00:00",)]


def test_the_build_is_deterministic():
    first_files, first = build()
    second_files, second = build(list(reversed(ts.records())))
    assert first == second and first_files == second_files


def test_without_an_access_column_there_is_one_file():
    files, manifest = build(access=None)
    assert set(files) == {ALL_ROWS} and manifest["tenant_files"] == {}


def test_read_only_store_refuses_writes_and_unlisted_columns():
    files, manifest = build()
    backend = MemoryBackend()
    ProjectionBuilder.write(backend.relational, GEN, files, manifest)
    catalog = ProjectionCatalog.load(backend.relational, GEN, spec())
    assert catalog is not None and catalog.generation == GEN.generation and catalog.relations["comment_parent"].dangling == 1
    grant = catalog.grant()
    grant["ticket"].discard("title_text")
    conn = open_readonly(backend.relational, GEN, ALL_ROWS, grant, 1000)
    try:
        assert conn.execute('SELECT COUNT(*) FROM "ticket"').fetchone() == (6,)
        for statement in ('UPDATE "ticket" SET "status" = 1', 'DELETE FROM "ticket"', 'SELECT "title_text" FROM "ticket"', "CREATE TABLE z (a)", "ATTACH ':memory:' AS x", "PRAGMA query_only=0", 'SELECT hex("id") FROM "ticket"'):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(statement).fetchall()
    finally:
        conn.close()


def test_a_long_statement_is_interrupted():
    big = [StructuredRecord(schema_id="document", id=f"tk-{i:05d}", values={"kind": "ticket", "tenant": "t_alpha"}, provenance=Provenance(extractor="t")) for i in range(3000)]
    files, manifest = build(big)
    backend = MemoryBackend()
    ProjectionBuilder.write(backend.relational, GEN, files, manifest)
    conn = open_readonly(backend.relational, GEN, ALL_ROWS, {"ticket": {"id", "tenant"}}, 10)
    with pytest.raises(sqlite3.OperationalError, match="interrupt"):
        conn.execute('SELECT COUNT(*) FROM "ticket" a, "ticket" b, "ticket" c').fetchone()


def test_catalog_needs_a_sealed_manifest_of_the_same_spec():
    files, manifest = build()
    backend = MemoryBackend()
    for name, data in files.items():
        backend.relational.put_file(GEN, name, data)
    assert ProjectionCatalog.load(backend.relational, GEN, spec()) is None  # files without the seal
    backend.relational.put_file(GEN, MANIFEST, json.dumps(manifest).encode())
    assert ProjectionCatalog.load(backend.relational, GEN, spec()) is not None
    assert ProjectionCatalog.load(backend.relational, GEN, spec(access=None)) is None  # other spec
    other = GEN.model_copy(update={"generation": "f" * 32})
    assert ProjectionCatalog.load(backend.relational, other, spec()) is None


# --- write path and lifecycle (sqlite backend) -----------------------------------------------------


def _engine(tmp_path, **kwargs):
    return Engine(ts.config(tmp_path / "state", typed_arm=False, **kwargs))


def test_ingest_builds_the_projection_per_generation(tmp_path):
    engine = _engine(tmp_path)
    manifest = engine.ingest(ts.documents())
    backend = engine.write_path.backends["local"]
    gen = engine.write_path.current()[0]
    names = backend.relational.file_names(gen)
    assert MANIFEST in names and ALL_ROWS in names and len(names) == 5
    assert (backend.snapshot_dir(gen) / "projection" / ALL_ROWS).exists()
    assert manifest["projection"]["tables"] == {"ticket": 6, "comment": 6, "asset": 2} and manifest["projection"]["dangling"] == {"comment_parent": 1}
    assert manifest["projections"]["projection"]["verified"] is True and "projection" in manifest["incremental"]["rebuilt"]


def test_upsert_and_delete_yield_a_new_projection_and_delete_generation_removes_files(tmp_path):
    engine = _engine(tmp_path)
    engine.ingest(ts.documents())
    first = engine.write_path.current()[0]
    changed = [d.model_copy(update={"metadata": {**d.metadata, "status": "closed"}}) if d.id == "tk-1" else d for d in ts.documents()]
    engine.ingest([changed[0]], delete_ids=["tk-5"])
    second = engine.write_path.current()[0]
    backend = engine.write_path.backends["local"]
    assert second.generation != first.generation
    catalog = ProjectionCatalog.load(backend.relational, second, engine.write_path.projection_spec)
    assert catalog.tables["ticket"].rows == 5
    conn = open_readonly(backend.relational, second, ALL_ROWS, catalog.grant(), 1000)
    assert conn.execute("SELECT status FROM ticket WHERE id = 'tk-1'").fetchone() == ("closed",)
    conn.close()
    with pytest.raises(GenerationInUse):
        engine.write_path.delete_generation(second.generation)
    assert backend.relational.file_names(second)
    result = engine.write_path.delete_generation(first.generation)
    assert "local" in result["deleted"] and backend.relational.file_names(first) == []
    assert not (backend.snapshot_dir(first) / "projection").exists()


def test_a_protected_generation_keeps_its_projection(tmp_path):
    engine = _engine(tmp_path)
    engine.ingest(ts.documents())
    first = engine.write_path.current()[0]
    engine.ingest([ts.documents()[0]])
    backend = engine.write_path.backends["local"]
    backend.protect(first)  # a live pin in this process
    with pytest.raises((GenerationInUse, BackendError)):
        engine.write_path.delete_generation(first.generation)
    assert backend.relational.file_names(first)


def test_index_recipe_is_unchanged_while_disabled(tmp_path):
    enabled = _engine(tmp_path / "a").write_path.index_fingerprint()
    data = ts.config_dict(tmp_path / "b" / "state", typed_arm=False)
    from su_multi_arm.core.config import validate_config

    disabled = Engine(validate_config({**data, "projection": {"enabled": False}})).write_path.index_fingerprint()
    data.pop("projection")
    absent = Engine(validate_config(data)).write_path.index_fingerprint()
    assert disabled == absent != enabled
