"""Count semantics and structured-contract additions (A1 §3.9.1-3.9.2, B2+B3; owned by the A2 L5 slice).

``FieldSpec.role`` (worst-row exactness, applied basis/extraction), ``Sort("$measure")``,
``Measure(kind="bucket", of=...)``, the ``hour`` granularity and the non-additive guard, on the
shared client aggregation path (memory and sqlite adapters)."""

from __future__ import annotations

import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.types import FieldSpec, GenerationId, Measure, MetadataSchema, Predicate, Provenance, ScopeSpec, StructuredRecord

SCHEMA = MetadataSchema(
    fields=[
        {"name": "service", "type": "string", "groupable": True},
        {"name": "date", "type": "date", "groupable": True},
        {"name": "ts_hour", "type": "datetime", "groupable": True},
        {"name": "lines", "type": "integer"},
        {"name": "p95", "type": "number", "additive": False},
        {"name": "row_exactness", "type": "enum", "enum_values": ["exact", "approximate"], "role": "exactness"},
        {"name": "basis", "type": "string", "role": "basis"},
        {"name": "extraction_source", "type": "enum", "enum_values": ["deterministic", "model_assisted"], "role": "extraction"},
    ]
)
GEN = GenerationId(dataset="d", generation="1" * 32, index_format=1, fingerprint="1" * 64)
ROWS = [
    ("a1", "api", "2026-03-01", "2026-03-01T10:00:00+00:00", 5, 100.0, "exact", "level_field", "deterministic"),
    ("a2", "api", "2026-03-02", "2026-03-02T11:00:00+00:00", 7, 300.0, "exact", "level_field", "deterministic"),
    ("b1", "auth", "2026-03-01", "2026-03-01T10:00:00+00:00", 1, 50.0, "exact", "token_match", "deterministic"),
    ("b2", "auth", "2026-03-01", "2026-03-01T10:30:00+00:00", 1, 50.0, "approximate", "token_match", "model_assisted"),
    ("b3", "auth", "2026-03-02", "2026-03-02T12:00:00+00:00", 1, 50.0, "exact", "token_match", "deterministic"),
]


@pytest.fixture()
def store():
    backend = MemoryBackend()
    keys = ("service", "date", "ts_hour", "lines", "p95", "row_exactness", "basis", "extraction_source")
    records = [StructuredRecord(schema_id="doc", id=r[0], values=dict(zip(keys, r[1:])), provenance=Provenance(extractor="t")) for r in ROWS]
    backend.records.upsert(GEN, records, SCHEMA)
    return backend.records


def test_worst_row_exactness_and_applied_basis(store):
    exact = store.execute(GEN, ScopeSpec(predicates=[Predicate(field="service", op="eq", value="api")], measure=Measure(kind="sum", field="lines")), [])
    assert exact.exactness == "exact" and exact.value == 12 and exact.applied_basis == ["level_field"] and exact.applied_extraction == ["deterministic"]
    mixed = store.execute(GEN, ScopeSpec(measure=Measure(kind="sum", field="lines")), [])
    assert mixed.exactness == "approximate"  # one capped row inside the sum
    assert mixed.applied_basis == ["level_field", "token_match"] and mixed.applied_extraction == ["deterministic", "model_assisted"]


def test_measure_sort_ranks_by_the_sum_not_the_row_count(store):
    spec = ScopeSpec(group_by=["service"], measure=Measure(kind="sum", field="lines"), sort={"field": "$measure", "descending": True}, top_n=1)
    assert spec.order == "desc" and spec.sort is None
    result = store.execute(GEN, spec, [])
    assert [(g.key["service"], g.value, g.count) for g in result.groups] == [("api", 12, 2)]  # auth has more rows (3) but fewer lines


def test_measure_sort_needs_groups():
    with pytest.raises(ValueError, match="group_by"):
        ScopeSpec(sort={"field": "$measure"})


def test_bucket_of_a_sum_per_day(store):
    spec = ScopeSpec(measure=Measure(kind="bucket", field="date", granularity="day", of=Measure(kind="sum", field="lines")))
    result = store.execute(GEN, spec, [])
    assert result.value == {"2026-03-01T00:00:00+00:00": 7, "2026-03-02T00:00:00+00:00": 8}


def test_hour_granularity_buckets_and_groups(store):
    buckets = store.execute(GEN, ScopeSpec(measure=Measure(kind="bucket", field="ts_hour", granularity="hour")), [])
    assert buckets.value["2026-03-01T10:00:00+00:00"] == 3
    grouped = store.execute(GEN, ScopeSpec(group_by=["ts_hour"], granularity="hour", measure=Measure(kind="sum", field="lines")), [])
    assert {g.key["ts_hour"]: g.value for g in grouped.groups}["2026-03-01T10:00:00+00:00"] == 7


def test_bucket_of_requires_a_bucket():
    with pytest.raises(ValueError, match="bucket"):
        Measure(kind="sum", field="lines", of=Measure(kind="count"))


def test_role_needs_a_text_field():
    with pytest.raises(ValueError, match="role"):
        FieldSpec(name="x", type="integer", role="basis")
    with pytest.raises(ValueError, match="additive"):
        FieldSpec(name="y", type="string", additive=False)


def test_non_additive_guard():
    from su_multi_arm.backends._scope import non_additive

    refused = non_additive(ScopeSpec(measure=Measure(kind="avg", field="p95")), SCHEMA)
    assert refused is not None and refused.reason == "measure_unsupported"
    assert non_additive(ScopeSpec(measure=Measure(kind="max", field="p95")), SCHEMA) is None
