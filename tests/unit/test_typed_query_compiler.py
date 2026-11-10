"""Typed query compiler and SQL lowering (typed-query plan M5): refusals, fan-out, golden SQL.

Synthetic catalog (``tests/typed_support.py``) over an in-memory record store for value resolution.
"""

from __future__ import annotations

import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.types import GenerationId, Measure, Predicate
from su_multi_arm.core.types_typed import TypedQueryDraft
from su_multi_arm.decision.typed_query import TypedQueryCompiler, TypedStepInvalid
from su_multi_arm.decision.typed_sql import lower
from su_multi_arm.decision.values import ValueResolver
from su_multi_arm.projection import ProjectionBuilder, ProjectionCatalog, ProjectionSpec

import typed_support as ts

GEN = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)
ALPHA = [Predicate(field="tenant", op="eq", value="t_alpha")]


@pytest.fixture(scope="module")
def world():
    schema = ts.schema()
    backend = MemoryBackend()
    backend.records.upsert(GEN, ts.records(), schema)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), schema, "tenant")
    builder = ProjectionBuilder(spec, schema)
    builder.add(ts.records())
    files, manifest = builder.seal(GEN)
    ProjectionBuilder.write(backend.relational, GEN, files, manifest)
    return backend, schema, ProjectionCatalog.load(backend.relational, GEN, spec)


def compile_(world, draft, filters=None, acl=ALPHA, **kwargs):
    backend, schema, catalog = world
    compiler = TypedQueryCompiler(catalog, ValueResolver(schema, backend.records, GEN, acl), row_limit=50, max_groups=100)
    return compiler.compile(TypedQueryDraft.model_validate(draft), filters, acl, **kwargs)


def reasons(compiled):
    return [(e.path, e.reason) for e in compiled.errors]


# --- refusals: one per reason ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "draft, expected",
    [
        ({"grain": "invoice"}, [("grain", "column_not_projectable")]),
        ({"grain": "ticket", "join": "nope"}, [("join", "join_not_declared")]),
        ({"grain": "asset", "join": "comment_parent"}, [("join", "join_not_declared")]),  # relation does not touch the grain
        ({"grain": "ticket", "where": [{"table": "asset", "predicate": {"field": "module", "op": "eq", "value": "Billing"}}]}, [("where[0].table", "filter_not_applicable")]),
        ({"grain": "ticket", "where": [{"table": "ticket", "predicate": {"field": "visibility", "op": "eq", "value": "public"}}]}, [("where[0].predicate.field", "column_not_projectable")]),
        ({"grain": "ticket", "where": [{"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billng"}}]}, [("where[0]", "value_not_observed")]),  # never fuzzy
        ({"grain": "ticket", "where": [{"table": "ticket", "predicate": {"field": "priority", "op": "eq", "value": "high"}}]}, [("where[0]", "type_mismatch")]),
        ({"grain": "ticket", "measure": {"kind": "sum", "field": "status"}}, [("measure.field", "measure_unsupported")]),
        ({"grain": "ticket", "measure": {"kind": "share"}}, [("measure.kind", "measure_unsupported")]),
        ({"grain": "ticket", "measure": {"kind": "sum", "field": "nothing"}}, [("measure.field", "column_not_projectable")]),
        ({"grain": "ticket", "join": "comment_parent", "measure": {"kind": "sum", "field": "hours"}}, [("measure.field", "fanout_unsafe")]),
        ({"grain": "comment", "join": "comment_parent", "measure": {"kind": "sum", "field": "ticket.hours"}}, [("measure.field", "fanout_unsafe")]),
        ({"grain": "ticket", "join": "comment_parent", "select": [{"table": "comment", "column": "visibility"}]}, [("select[0]", "fanout_unsafe")]),
        ({"grain": "ticket", "measure": {"kind": "count"}, "group_by": [{"table": "ticket", "column": "opened_at"}]}, [("group_by[0]", "field_not_filterable")]),
        ({"grain": "ticket", "measure": {"kind": "count"}, "group_by": [{"table": "ticket", "column": "hours"}]}, [("group_by[0]", "field_not_filterable")]),
        ({"grain": "ticket", "limit": 51}, [("limit", "result_truncated")]),
        ({"grain": "ticket", "measure": {"kind": "count"}, "group_by": [{"table": "ticket", "column": "module"}], "order": "desc", "top_n": 101}, [("top_n", "result_truncated")]),
        ({"grain": "ticket", "measure": {"kind": "first_seen", "field": "hours"}}, [("measure.field", "measure_unsupported")]),
        ({"grain": "ticket", "windows": [{"table": "ticket", "column": "module", "expression": "2024"}]}, [("windows[0]", "filter_not_applicable")]),
    ],
)
def test_every_refusal_is_a_typed_compile_error(world, draft, expected):
    assert reasons(compile_(world, draft)) == expected


def test_request_filters_apply_to_every_alias_or_refuse(world):
    compiled = compile_(world, {"grain": "comment", "join": "comment_parent"}, filters={"tenant": "t_alpha"}, acl=[])
    assert sorted(p.table for p in compiled.filters_applied) == ["comment", "ticket"]
    compiled = compile_(world, {"grain": "asset"}, filters={"visibility": "public"})
    assert reasons(compiled) == [("request_filters.visibility", "filter_not_applicable")]
    # a draft predicate identical to the request filter is applied once; a different value is invalid
    same = {"grain": "ticket", "where": [{"table": "ticket", "predicate": {"field": "module", "op": "eq", "value": "Billing"}}]}
    assert compile_(world, same, filters={"module": "Billing"}).executable()
    with pytest.raises(TypedStepInvalid):
        compile_(world, same, filters={"module": "Search"})


def test_acl_is_lowered_onto_every_alias_and_into_the_fingerprint(world):
    draft = {"grain": "comment", "join": "comment_parent", "measure": {"kind": "count"}}
    alpha = compile_(world, draft)
    assert [(p.table, p.predicate.value) for p in alpha.filters_applied] == [("comment", "t_alpha"), ("ticket", "t_alpha")]
    beta = compile_(world, draft, acl=[Predicate(field="tenant", op="eq", value="t_beta")])
    assert alpha.fingerprint != beta.fingerprint and alpha.acl_key != beta.acl_key
    off = compile_(world, draft, inject_acl=False)
    assert off.filters_applied == [] and off.acl_applied is False and off.fingerprint == alpha.fingerprint


def test_an_acl_on_a_column_some_table_lacks_fails_closed(world):
    compiled = compile_(world, {"grain": "ticket"}, acl=[Predicate(field="visibility", op="eq", value="public")])
    assert reasons(compiled) == [("acl", "filter_not_applicable")]


def test_fanout_rules(world):
    parent_count = compile_(world, {"grain": "ticket", "join": "comment_parent", "measure": {"kind": "count"}})
    assert parent_count.fanout == "distinct_guarded" and parent_count.executable()
    child = compile_(world, {"grain": "comment", "join": "comment_parent", "measure": {"kind": "sum", "field": "hours"}})
    assert child.fanout == "none" and child.executable()
    minimum = compile_(world, {"grain": "ticket", "join": "comment_parent", "measure": {"kind": "max", "field": "comment.hours"}})
    assert minimum.executable()  # min/max are fan-out safe


GOLDEN_COUNT = (
    'SELECT COUNT(DISTINCT "g"."id"), COUNT(DISTINCT "g"."id"), 0, SUM(CASE WHEN instr(COALESCE("j"."_rejected", \'\'), ?) > 0 THEN 1 ELSE 0 END) '
    'FROM "ticket" AS "g" JOIN "comment" AS "j" ON "j"."parent_id" = "g"."id" '
    'WHERE ("g"."tenant" = ?) AND ("j"."tenant" = ?) AND ("j"."visibility" = ?)'
)
GOLDEN_LIST = (
    'SELECT "g"."id", "j"."status", "j"."id", "g"."tenant", "j"."tenant", "g"."_rejected", "j"."_rejected" '
    'FROM "comment" AS "g" JOIN "ticket" AS "j" ON "g"."parent_id" = "j"."id" '
    'WHERE ("g"."tenant" = ?) AND ("j"."tenant" = ?) AND (NOT COALESCE((lower("j"."module") IN (SELECT lower(value) FROM json_each(?))), 0)) '
    'ORDER BY "g"."id" LIMIT 6'
)


def test_golden_sql_is_parameterised_and_deterministic(world):
    _, _, catalog = world
    compiled = compile_(world, {"grain": "ticket", "join": "comment_parent", "measure": {"kind": "count"}, "where": [{"table": "comment", "predicate": {"field": "visibility", "op": "eq", "value": "private"}}]})
    sql, params = lower(compiled, catalog, row_limit=50, max_groups=100).statements["main"]
    assert sql == GOLDEN_COUNT and params == ['"visibility"', "t_alpha", "t_alpha", "private"]
    assert lower(compiled, catalog, row_limit=50, max_groups=100).statements == lower(compiled, catalog, row_limit=50, max_groups=100).statements
    listing = compile_(
        world,
        {"grain": "comment", "join": "comment_parent", "limit": 5, "select": [{"table": "ticket", "column": "status"}],
         "where": [{"table": "ticket", "predicate": {"field": "module", "op": "in", "value": ["billing"], "negate": True}}]},
    )
    assert reasons(listing) == []
    sql, params = lower(listing, catalog, row_limit=50, max_groups=100).statements["main"]
    assert sql == GOLDEN_LIST and params == ["t_alpha", "t_alpha", '["Billing"]']  # value resolved to the observed spelling


def test_injection_strings_are_values_never_sql(world):
    _, _, catalog = world
    compiled = compile_(world, {"grain": "ticket", "where": [{"table": "ticket", "predicate": {"field": "title_text", "op": "eq", "value": ts.INJECTION}}]}, acl=[])
    if compiled.errors:  # high-cardinality value grounded by literal lookup: it exists, so it resolves
        pytest.fail(str(compiled.errors))
    statements = lower(compiled, catalog, row_limit=50, max_groups=100).statements
    assert all(ts.INJECTION not in sql for sql, _ in statements.values())
    assert any(ts.INJECTION in params for _, params in statements.values())


def test_names_never_come_from_model_text():
    with pytest.raises(ValueError):
        TypedQueryDraft(grain='ticket" --')
    with pytest.raises(ValueError):
        TypedQueryDraft(grain="ticket", group_by=[{"table": "ticket", "column": "a b"}])
    assert Measure(kind="count_distinct", field="ticket.module").unit == "ticket.module"
