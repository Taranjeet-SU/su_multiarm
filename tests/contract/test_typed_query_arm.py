"""``typed_query`` arm contract (typed-query plan M5), synthetic fixtures only.

* lookups/lists cite (document id, field, generation); aggregates carry the typed fingerprint;
* ACL from ``ArmInput.acl`` is authoritative (recompiled; fingerprint includes ``acl_key``);
* layer 2: the ACL selects the per-tenant file; with compiler ACL injection **disabled** (test
  hook) generated queries still never return another tenant's or the canary's rows;
* layer 3: a planted foreign row in a tenant file discards the result;
* differential: single-table queries equal ``StructuredArm`` over the same records.
"""

from __future__ import annotations

import json
import math
import random

import pytest

from su_multi_arm.arms.base import ArmInput
from su_multi_arm.arms.structured import StructuredArm
from su_multi_arm.arms.typed_query import TypedQueryArm, TypedQueryRuntime
from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import StructuredArm as StructuredArmConfig
from su_multi_arm.core.config_projection import ProjectionConfig
from su_multi_arm.core.errors import BackendError
from su_multi_arm.core.types import Chunk, GenerationId, Predicate, ScopeSpec
from su_multi_arm.core.types_typed import TypedQueryDraft
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec, tenant_file
from su_multi_arm.runtime.context import new_context

import typed_support as ts

GEN = GenerationId(dataset="demo", generation="d" * 32, index_format=3, fingerprint="e" * 64)
OTHER = GEN.model_copy(update={"generation": "9" * 32})


def tenant(value: str) -> list[Predicate]:
    return [Predicate(field="tenant", op="eq", value=value)]


@pytest.fixture(scope="module")
def world():
    schema = ts.schema()
    backend = MemoryBackend()
    docs = ts.documents()
    backend.records.upsert(GEN, ts.records(docs), schema)
    chunks = [Chunk(id=f"{d.id}#0", document_id=d.id, field="body", start=0, end=len(d.fields["body"]), text=d.fields["body"]) for d in docs]
    backend.catalog.put_documents(GEN, docs, chunks)
    backend.lexical.index(GEN, chunks, [])
    backend.lexical.refresh(GEN)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), schema, "tenant")
    builder = ProjectionBuilder(spec, schema)
    builder.add(ts.records(docs))
    files, manifest = builder.seal(GEN)
    ProjectionBuilder.write(backend.relational, GEN, files, manifest)
    return backend, schema, files


def make_arm(world, **limits):
    backend, schema, _ = world
    config = TypedQueryRuntime(backend="m", records="m", projection=ProjectionConfig.model_validate(ts.PROJECTION), access_column="tenant", **{"row_limit": 50, "max_groups": 100, **limits})
    return TypedQueryArm(config, Dependencies(backends={"m": backend}, components={"catalog": backend.catalog}, metadata_schema=schema))


def run(arm, draft, acl, filters=None, gen=GEN):
    ctx = new_context(gen)
    result = arm.run(ctx, ArmInput(query="typed", typed=TypedQueryDraft.model_validate(draft), acl=acl, filters=filters or {}))
    events = {e["name"]: e for s in ctx.trace.spans for e in s.events}
    return result.aggregates[0], events


def test_lookup_rows_cite_document_field_and_generation(world):
    evidence, events = run(make_arm(world), {"grain": "ticket", "limit": 1, "where": [{"table": "ticket", "predicate": {"field": "id", "op": "eq", "value": "tk-1"}}], "select": [{"table": "ticket", "column": "status"}, {"table": "ticket", "column": "priority"}]}, tenant("t_alpha"))
    assert evidence["evidence"] == "typed_rows" and evidence["operation"] == "lookup" and evidence["exactness"] == "exact"
    row = evidence["rows"][0]
    assert row["document_id"] == "tk-1" and row["values"]["status"] == "open" and row["values"]["priority"] == 1
    assert {"document_id": "tk-1", "field": "status", "generation": GEN.generation} in row["citations"]
    assert events["typed_query_executed"]["file_grant"] == "tenant" and events["typed_query_compiled"]["acl_applied"] is True


def test_child_rows_with_parent_columns_cite_the_parent(world):
    evidence, _ = run(make_arm(world), {"grain": "comment", "join": "comment_parent", "select": [{"table": "comment", "column": "visibility"}, {"table": "ticket", "column": "status"}]}, tenant("t_alpha"))
    first = evidence["rows"][0]
    assert first["document_id"] == "cm-1" and first["values"]["ticket.status"] == "open"
    assert {"document_id": "tk-1", "field": "status", "generation": GEN.generation} in first["citations"]
    assert evidence["total"] == 3 and evidence["exactness"] == "exact"


def test_aggregates_carry_the_typed_fingerprint_and_contributors(world):
    evidence, _ = run(make_arm(world), {"grain": "ticket", "measure": {"kind": "count"}}, tenant("t_alpha"))
    assert evidence["value"] == 3 and evidence["exactness"] == "exact" and evidence["document_ids"] == ["tk-1", "tk-2", "tk-3"]
    assert len(evidence["typed_query"]["fingerprint"]) == 64 and evidence["generation"] == GEN.generation
    hours, _ = run(make_arm(world), {"grain": "ticket", "measure": {"kind": "sum", "field": "hours"}}, tenant("t_alpha"))
    assert hours["value"] == 6.5 and hours["missing_or_invalid"] == 1
    last, _ = run(make_arm(world), {"grain": "ticket", "measure": {"kind": "last_seen", "field": "opened_at"}}, tenant("t_alpha"))
    assert last["value"]["record_id"] == "tk-3"


def test_ranked_groups_and_fanout_guarded_counts(world):
    evidence, _ = run(make_arm(world), {"grain": "ticket", "measure": {"kind": "count"}, "group_by": [{"table": "ticket", "column": "module"}], "order": "desc", "top_n": 1}, tenant("t_alpha"))
    assert evidence["operation"] == "rank" and evidence["groups"] == [{"key": {"module": "Billing"}, "value": 2, "count": 2, "rank": 1}]
    # tickets with a private comment: the parent grain is counted once per ticket
    guarded, _ = run(make_arm(world), {"grain": "ticket", "join": "comment_parent", "measure": {"kind": "count"}}, tenant("t_alpha"))
    assert guarded["value"] == 2 and guarded["typed_query"]["fanout"] == "distinct_guarded"


def test_unresolvable_child_keys_make_a_child_join_bounded(world):
    evidence, _ = run(make_arm(world), {"grain": "comment", "join": "comment_parent", "measure": {"kind": "count"}}, tenant("t_beta"))
    assert evidence["value"] == 1 and evidence["exactness"] == "bounded" and "no resolvable ticket" in evidence["bounded_by"][0]


def test_request_filters_apply_to_the_child_alias(world):
    evidence, _ = run(make_arm(world), {"grain": "ticket", "join": "comment_parent", "measure": {"kind": "count"}}, tenant("t_alpha"), filters={"visibility": "private"})
    assert evidence["value"] == 1 and evidence["document_ids"] == ["tk-1"]


def test_file_layer_selection(world):
    arm = make_arm(world)
    count = {"grain": "ticket", "measure": {"kind": "count"}}
    assert run(arm, count, [])[0]["value"] == 6  # unrestricted principal: all rows incl. the canary
    absent = run(arm, count, tenant("t_unknown"))[0]
    assert absent["value"] == 0 and absent["file_grant"] == "absent" and absent["exactness"] == "exact"
    nothing = run(arm, count, [Predicate(field="tenant", op="in", value=[])])[0]
    assert nothing["value"] == 0 and nothing["file_grant"] == "none"
    other = run(arm, count, [Predicate(field="tenant", op="in", value=["t_alpha", "t_beta"])])[0]
    assert other["refused"] and other["unsupported"][0]["reason"] == "projection_unavailable"
    unsealed = run(arm, count, tenant("t_alpha"), gen=OTHER)[0]
    assert unsealed["refused"] and unsealed["unsupported"][0]["reason"] == "projection_unavailable"


def test_a_listed_tenant_whose_file_is_missing_is_an_error(world):
    backend, schema, files = world
    clone = MemoryBackend()
    for name, data in files.items():
        if name != tenant_file("t_beta"):
            clone.relational.put_file(GEN, name, data)
    clone.relational.put_file(GEN, "manifest.json", backend.relational.read_file(GEN, "manifest.json"))
    arm = TypedQueryArm(make_arm(world).cfg, Dependencies(backends={"m": clone}, components={"catalog": clone.catalog}, metadata_schema=schema))
    with pytest.raises(BackendError, match="missing"):
        run(arm, {"grain": "ticket", "measure": {"kind": "count"}}, tenant("t_beta"))


def test_a_plan_compiled_for_another_principal_is_recompiled(world):
    arm = make_arm(world)
    draft = {"grain": "ticket", "measure": {"kind": "sum", "field": "hours"}}
    alpha, _ = run(arm, draft, tenant("t_alpha"))
    beta, _ = run(arm, draft, tenant("t_beta"))
    assert alpha["typed_query"]["fingerprint"] != beta["typed_query"]["fingerprint"]
    assert (alpha["value"], beta["value"]) == (6.5, 7.5)


def test_compile_errors_are_returned_as_refusals(world):
    evidence, events = run(make_arm(world), {"grain": "ticket", "measure": {"kind": "sum", "field": "status"}}, tenant("t_alpha"))
    assert evidence["refused"] and evidence["exactness"] == "bounded" and evidence["unsupported"][0]["reason"] == "measure_unsupported"
    assert events["typed_query_compiled"]["errors"][0]["reason"] == "measure_unsupported"


def test_truncated_lists_are_bounded(world):
    evidence, _ = run(make_arm(world, row_limit=2), {"grain": "ticket"}, [])
    assert evidence["value"] == 2 and evidence["total"] == 6 and evidence["truncated"] and evidence["exactness"] == "bounded"
    assert evidence["unsupported"][-1]["reason"] == "result_truncated"


def test_text_conditions_need_a_lexical_backend(world):
    evidence, _ = run(make_arm(world), {"grain": "ticket", "measure": {"kind": "count"}, "text": {"text": "Billing"}}, tenant("t_alpha"))
    assert evidence["exactness"] == "bounded" and evidence["unsupported"][0]["reason"] == "not_supported_by_backend"
    arm = make_arm(world, text_backend="m")
    evidence, _ = run(arm, {"grain": "ticket", "measure": {"kind": "count"}, "text": {"text": "Billing"}}, tenant("t_alpha"))
    assert evidence["value"] == 2 and evidence["exactness"] == "exact" and evidence["basis"]


def test_the_returned_row_check_fires_on_a_planted_foreign_row(world):
    backend, schema, files = world
    planted = MemoryBackend()
    for name, data in files.items():
        planted.relational.put_file(GEN, name, data)
    planted.relational.put_file(GEN, tenant_file("t_alpha"), files[tenant_file("t_beta")])  # a corrupted tenant file
    planted.relational.put_file(GEN, "manifest.json", backend.relational.read_file(GEN, "manifest.json"))
    planted.records.upsert(GEN, ts.records(), schema)
    arm = TypedQueryArm(make_arm(world).cfg, Dependencies(backends={"m": planted}, components={"catalog": planted.catalog}, metadata_schema=schema))
    assert run(arm, {"grain": "ticket"}, tenant("t_alpha"))[0]["rows"] == []  # layer 1 alone filters the rows
    arm.inject_acl = False
    with pytest.raises(BackendError, match="does not belong to the granted tenant"):
        run(arm, {"grain": "ticket"}, tenant("t_alpha"))


# --- generated suites --------------------------------------------------------------------------

TRUTH = {t: [d for d in ts.documents() if d.metadata.get("tenant") == t] for t in (*ts.TENANTS, ts.CANARY)}
GROUPABLE = {"ticket": ["tenant", "status", "module", "priority"], "comment": ["tenant", "visibility"], "asset": ["tenant", "module"]}
NUMERIC = {"ticket": ["hours", "priority"], "comment": ["hours"], "asset": ["hours"]}


def random_draft(rng: random.Random, allow_join: bool = True) -> dict:
    grain = rng.choice(["ticket", "comment", "asset"])
    where = []
    pool = {
        "ticket": [("status", "eq", ["open", "closed", "pending"]), ("module", "eq", ["Billing", "billing", "Search", "Reports"]), ("priority", "range", [1, 2, 3]), ("hours", "range", [1.0, 2.5, 5.0]), ("priority", "missing", [None])],
        "comment": [("visibility", "eq", ["public", "private"]), ("hours", "range", [0.5, 1.0, 2.0]), ("hours", "missing", [None])],
        "asset": [("module", "in", [["Billing"], ["Search", "Billing"]]), ("hours", "missing", [None])],
    }[grain]
    for _ in range(rng.randint(0, 2)):
        field, op, values = rng.choice(pool)
        value = rng.choice(values)
        predicate: dict = {"field": field, "op": op, "negate": rng.random() < 0.25}
        if op == "range":
            predicate["lower" if rng.random() < 0.5 else "upper"] = value
            predicate["inclusive"] = rng.random() < 0.7
        elif op != "missing":
            predicate["value"] = value
        where.append({"table": grain, "predicate": predicate})
    draft: dict = {"grain": grain, "where": where}
    if allow_join and grain in {"ticket", "comment"} and rng.random() < 0.3:
        draft["join"] = "comment_parent"
    shape = rng.choice(["count", "count_distinct", "numeric", "group", "rows"])
    if shape == "count":
        draft["measure"] = {"kind": "count"}
    elif shape == "count_distinct":
        draft["measure"] = {"kind": "count_distinct", "unit": rng.choice(GROUPABLE[grain])}
    elif shape == "numeric":
        kind = rng.choice(["sum", "avg", "min", "max"])
        draft["measure"] = {"kind": kind, "field": rng.choice(NUMERIC[grain])}
    elif shape == "group":
        draft["measure"] = {"kind": "count"}
        draft["group_by"] = [{"table": grain, "column": rng.choice(GROUPABLE[grain])}]
    return draft


def test_canary_with_compiler_acl_injection_disabled(world):
    """Layer 2 alone: 5000 generated queries per the plan; zero foreign/canary rows, tenant-only totals."""
    arm = make_arm(world)
    arm.inject_acl = False
    rng = random.Random(20261108)
    executed = 0
    for _ in range(5000):
        principal = rng.choice(ts.TENANTS)
        draft = random_draft(rng)
        try:
            evidence, _ = run(arm, draft, tenant(principal))
        except BackendError:
            raise
        if evidence.get("refused"):
            continue
        executed += 1
        assert evidence["typed_query"]["acl_applied"] is False and "acl_not_injected" in evidence["bounded_by"]
        owned = {d.id for d in TRUTH[principal]}
        assert set(evidence["document_ids"]) <= owned
        for row in evidence.get("rows", []):
            assert row["document_id"] in owned and row["values"].get("tenant", principal) == principal
            assert all(c["document_id"] in owned for c in row["citations"])
        if draft.get("measure", {}).get("kind") == "count" and not draft.get("group_by"):
            assert evidence["value"] <= len([d for d in TRUTH[principal] if d.kind == draft["grain"]])
        for group in evidence.get("groups", []):
            assert group["key"].get("tenant", principal) == principal
        if draft.get("measure", {}).get("kind") in {"sum", "max"} and isinstance(evidence["value"], (int, float)):
            assert evidence["value"] < 99.0  # the canary's hours never leak into a tenant aggregate
    assert executed >= 4000


def _structured(world):
    backend, schema, _ = world
    return StructuredArm(StructuredArmConfig(backend="m"), Dependencies(backends={"m": backend}, components={"catalog": backend.catalog}, metadata_schema=schema))


def _close(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def test_single_table_differential_against_the_structured_arm(world):
    """5000 generated single-table queries: equal values, totals, contributors and groups."""
    typed = make_arm(world)
    structured = _structured(world)
    rng = random.Random(7)
    compared = 0
    for _ in range(5000):
        draft = random_draft(rng, allow_join=False)
        if "measure" not in draft:
            continue
        acl = rng.choice([tenant("t_alpha"), tenant("t_beta"), []])
        evidence, _ = run(typed, draft, acl)
        if evidence.get("refused"):
            continue
        grain_kind = draft["grain"]
        predicates = [Predicate(field="kind", op="eq", value=grain_kind), *(Predicate.model_validate(w["predicate"]) for w in draft["where"])]
        measure = draft["measure"]
        spec = ScopeSpec(predicates=predicates, measure=measure, group_by=[g["column"] for g in draft.get("group_by", [])])
        ctx = new_context(GEN)
        result = structured.run(ctx, ArmInput(query="x", aggregate=spec, acl=acl)).aggregates[0]
        assert result["exactness"] == "exact", result["unsupported"]
        assert evidence["total"] == result["total"], (draft, acl)
        assert evidence["document_ids"] == sorted(result["document_ids"])[:8]
        if draft.get("group_by"):
            column = draft["group_by"][0]["column"]
            ours = {g["key"][column]: (g["value"], g["count"]) for g in evidence["groups"]}
            theirs = {g["key"][column]: (g["value"], g["count"]) for g in result["groups"]}
            assert ours == theirs, (draft, acl)
        else:
            assert _close(evidence["value"], result["value"]), (draft, acl, evidence["value"], result["value"])
            assert evidence["missing_or_invalid"] == result["missing_or_invalid"], (draft, acl)
        compared += 1
    assert compared >= 3000


def test_evidence_is_json_serialisable(world):
    evidence, _ = run(make_arm(world), {"grain": "comment", "join": "comment_parent"}, tenant("t_alpha"))
    json.dumps(evidence)


def test_engine_binds_the_arm_to_the_sqlite_projection_without_offering_it_to_planners(tmp_path):
    from su_multi_arm import Engine

    engine = Engine(ts.config(tmp_path / "state"))
    engine.ingest(ts.documents())
    arm = engine.pipelines["default"].arms["typed_query"]
    assert isinstance(arm, TypedQueryArm) and "typed_query" not in engine.capabilities().arms
    gen = engine.write_path.current()[0]
    assert arm.validate(None) == []
    ctx = new_context(gen)
    draft = TypedQueryDraft.model_validate({"grain": "ticket", "measure": {"kind": "count"}, "group_by": [{"table": "ticket", "column": "status"}]})
    evidence = arm.run(ctx, ArmInput(query="typed", typed=draft, acl=tenant("t_beta"))).aggregates[0]
    assert {g["key"]["status"]: g["value"] for g in evidence["groups"]} == {"open": 1, "pending": 1}
    assert evidence["exactness"] == "exact" and evidence["file_grant"] == "tenant"
    arm.inject_acl = False
    evidence = arm.run(ctx, ArmInput(query="typed", typed=draft, acl=tenant("t_beta"))).aggregates[0]
    assert sum(g["value"] for g in evidence["groups"]) == 2  # sqlite tenant file holds t_beta only
