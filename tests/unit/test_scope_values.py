"""Resolution table (plan §3.7.2): one test per row, plus scope compilation and the zero-match flag."""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import Measure, Predicate, Sort
from su_multi_arm.decision import DecisionFailure, ScopeCompiler, ScopeDraft, ValueResolver, resolve_request_filters


@pytest.fixture
def resolver(v2_schema, v2_backend, v2_gen):
    return ValueResolver(v2_schema, v2_backend.records, v2_gen, [])


@pytest.fixture
def compiler(v2_schema, resolver):
    return ScopeCompiler(v2_schema, resolver)


def eq(field, value):
    return Predicate(field=field, op="eq", value=value)


# --- eq / in on string, enum, id ----------------------------------------------------------------


def test_enum_value_must_be_declared(resolver):
    assert resolver.resolve(eq("kind", "note")).ok()
    bad = resolver.resolve(eq("kind", "memo"))
    assert bad.unsupported.reason == "value_not_observed" and bad.predicate is None
    assert resolver.resolve(eq("kind", "Note")).unsupported.reason == "value_not_observed"  # no casefold declared


def test_string_under_cap_matches_observed_values_with_casefold(resolver):
    assert resolver.observed("project") == ["Alpha", "Beta", "Gamma"]
    assert resolver.resolve(eq("project", "alpha")).predicate.value == "Alpha"
    assert resolver.resolve(eq("project", "Delta")).unsupported.reason == "value_not_observed"
    assert resolver.resolve(Predicate(field="project", op="in", value=["alpha", "Beta"])).predicate.value == ["Alpha", "Beta"]
    assert resolver.resolve(eq("status", "Open")).unsupported.reason == "value_not_observed"  # status has no casefold


def test_above_cap_uses_record_store_exists(resolver, v2_schema):
    assert resolver.observed("tag") is None  # three values, cap two
    assert resolver.resolve(eq("tag", "t3")).ok()
    missing = resolver.resolve(eq("tag", "t9"))
    assert missing.unsupported.reason == "value_not_observed" and "does not exist" in missing.unsupported.suggestion


def test_id_fields_are_never_enumerated_but_are_verified(resolver):
    assert "external_id" not in resolver.planner_values() and "tag" not in resolver.planner_values()
    assert set(resolver.planner_values()) == {"kind", "project", "status"}
    assert resolver.resolve(eq("external_id", "REC-1001")).ok()
    assert resolver.resolve(eq("external_id", "REC-9999")).unsupported.reason == "value_not_observed"
    assert resolver.resolve(eq("external_id", 1001)).unsupported.reason == "type_mismatch"


def test_caller_filter_applied_as_given_and_zero_match_reported(resolver):
    given = resolver.resolve(eq("external_id", "REC-9999"), from_request=True)
    assert given.ok() and given.zero_match is True and given.origin == "request"
    resolved = resolve_request_filters(resolver, {"project": "Delta", "status": "open", "colour": "red"})
    assert [p.field for p in resolved.predicates] == ["project", "status"]
    assert resolved.zero_match_fields == ["project"]
    assert resolved.unsupported[0].reason == "field_not_filterable"


def test_without_record_store_values_cannot_be_verified(v2_schema):
    lone = ValueResolver(v2_schema)
    assert lone.resolve(eq("project", "Alpha")).unsupported.reason == "value_not_observed"
    assert lone.resolve(eq("kind", "note")).ok()  # enum needs no store


# --- eq / in on integer, number, boolean --------------------------------------------------------


def test_numeric_and_boolean_type_parse_only(resolver):
    assert resolver.resolve(eq("priority", "2")).predicate.value == 2
    assert resolver.resolve(eq("priority", 2.0)).predicate.value == 2
    assert resolver.resolve(eq("priority", "two")).unsupported.reason == "type_mismatch"
    assert resolver.resolve(eq("priority", True)).unsupported.reason == "type_mismatch"
    assert resolver.resolve(eq("amount", "7.25")).predicate.value == 7.25
    assert resolver.resolve(eq("amount", "lots")).unsupported.reason == "type_mismatch"
    assert resolver.resolve(eq("archived", "true")).predicate.value is True
    assert resolver.resolve(eq("archived", "yes")).unsupported.reason == "type_mismatch"
    assert resolver.resolve(Predicate(field="priority", op="in", value=["1", 3])).predicate.value == [1, 3]


# --- range ---------------------------------------------------------------------------------------


def test_range_bounds_typed_offset_required(resolver):
    numeric = resolver.resolve(Predicate(field="amount", op="range", lower="5", upper=20))
    assert numeric.predicate.lower == 5.0 and numeric.predicate.upper == 20
    naive = resolver.resolve(Predicate(field="created_at", op="range", lower="2024-03-01T00:00:00"))
    assert naive.unsupported.reason == "timezone_missing"
    assert resolver.resolve(Predicate(field="created_at", op="range", lower="2024-03-01T00:00:00+00:00")).ok()
    assumed = resolver.resolve(Predicate(field="updated_at", op="range", lower="2024-03-01T00:00:00"))
    assert assumed.predicate.lower == "2024-03-01T00:00:00+00:00"
    assert resolver.resolve(Predicate(field="created_at", op="range", lower="garbage")).unsupported.reason == "unparseable_bound"
    assert resolver.resolve(Predicate(field="resolved_on", op="range", lower="2024-03-01")).ok()
    assert resolver.resolve(Predicate(field="resolved_on", op="range", lower="2024/03/01")).unsupported.reason == "unparseable_bound"
    assert resolver.resolve(Predicate(field="project", op="range", lower="A")).unsupported.reason == "type_mismatch"
    assert resolver.resolve(Predicate(field="amount", op="range", lower=9, upper=1)).unsupported.reason == "unparseable_bound"


# --- date_interval -------------------------------------------------------------------------------


def test_date_interval_half_open_under_timezone_policy(resolver, v2_backend, v2_gen):
    month = resolver.resolve(Predicate(field="created_at", op="date_interval", lower="2024-03-01T00:00:00+00:00", granularity="month"))
    assert (month.predicate.lower, month.predicate.upper) == ("2024-03-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00")
    calendar = resolver.resolve(Predicate(field="created_at", op="date_interval", lower="2024-03", granularity="month"))
    assert calendar.unsupported.reason == "timezone_missing"  # require_offset: a calendar bound has no offset
    assumed = resolver.resolve(Predicate(field="updated_at", op="date_interval", lower="2024-03", granularity="month"))
    assert (assumed.predicate.lower, assumed.predicate.upper) == ("2024-03-01T00:00:00+00:00", "2024-04-01T00:00:00+00:00")
    year = resolver.resolve(Predicate(field="updated_on", op="date_interval", lower="2024", granularity="year"))
    assert year.unsupported.reason == "field_not_filterable"
    year = resolver.resolve(Predicate(field="updated_at", op="date_interval", lower="2024", granularity="year"))
    assert (year.predicate.lower, year.predicate.upper) == ("2024-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00")
    days = resolver.resolve(Predicate(field="resolved_on", op="date_interval", lower="2024-03-02", upper="2024-03-05", granularity="day"))
    assert (days.predicate.lower, days.predicate.upper) == ("2024-03-02", "2024-03-06")  # upper day included, half-open
    bad = resolver.resolve(Predicate(field="resolved_on", op="date_interval", lower="03/2024", granularity="month"))
    assert bad.unsupported.reason == "unparseable_bound"
    assert resolver.resolve(Predicate(field="amount", op="date_interval", lower="2024", granularity="year")).unsupported.reason == "type_mismatch"
    # executed on the fake: March 2024 (UTC) holds r1, r2, r5; r3 starts on 1 April and is excluded (half-open).
    from su_multi_arm.core.types import ScopeSpec

    result = v2_backend.records.execute(v2_gen, ScopeSpec(predicates=[month.predicate], measure=Measure(kind="count")), [])
    assert result.exactness == "exact" and result.value == 3 and result.matched_ids == ["r1", "r2", "r5"]
    dates = v2_backend.records.execute(v2_gen, ScopeSpec(predicates=[days.predicate], measure=Measure(kind="count")), [])
    assert dates.value == 1 and dates.matched_ids == ["r1"] and not dates.unsupported


# --- missing, non-filterable, group/sort, measure --------------------------------------------------


def test_missing_requires_nullable_field(resolver):
    assert resolver.resolve(Predicate(field="amount", op="missing")).ok()
    assert resolver.resolve(Predicate(field="kind", op="missing")).unsupported.reason == "field_not_nullable"


def test_unknown_or_non_filterable_field(resolver):
    assert resolver.resolve(eq("colour", "red")).unsupported.reason == "field_not_filterable"
    assert resolver.resolve(eq("region", "north")).unsupported.reason == "field_not_filterable"
    assert resolver.resolve(Predicate(field="region", op="missing")).unsupported.reason == "field_not_filterable"


def test_group_by_and_sort_flags(compiler):
    assert compiler.compile(ScopeDraft(group_by=["project"], measure=Measure(kind="count"))).executable()
    grouped = compiler.compile(ScopeDraft(group_by=["status"], measure=Measure(kind="count")))
    assert grouped.spec is None and "group by status" in grouped.unsupported[0].constraint_text
    assert compiler.compile(ScopeDraft(sort=Sort(field="amount"), top_n=2)).executable()
    sorted_bad = compiler.compile(ScopeDraft(sort=Sort(field="region")))
    assert sorted_bad.spec is None and sorted_bad.unsupported[0].reason == "field_not_filterable"


def test_measure_rules_never_substitute_count(compiler):
    assert compiler.compile(ScopeDraft(measure=Measure(kind="sum", field="amount"))).spec.measure.kind == "sum"
    text_sum = compiler.compile(ScopeDraft(measure=Measure(kind="sum", field="project")))
    assert text_sum.spec is None and text_sum.unsupported[0].reason == "measure_unsupported"
    no_basis = compiler.compile(ScopeDraft(measure=Measure(kind="first_seen", field="created_at")))
    assert no_basis.spec is None and no_basis.unsupported[0].reason == "measure_unsupported"
    with_basis = compiler.compile(ScopeDraft(measure=Measure(kind="first_seen", field="created_at"), time_basis="created_at"))
    assert with_basis.spec.measure.field == "created_at" and with_basis.spec.time_basis == "created_at"
    inferred = compiler.compile(ScopeDraft(measure=Measure(kind="last_seen", field="resolved_on"), time_basis="resolved_on"))
    assert inferred.spec.measure.field == "resolved_on"
    bad_basis = compiler.compile(ScopeDraft(measure=Measure(kind="count"), time_basis="amount"))
    assert bad_basis.spec is None and bad_basis.unsupported[0].reason == "measure_unsupported"
    bucket = compiler.compile(ScopeDraft(measure=Measure(kind="bucket", field="created_at", granularity="month"), time_basis="created_at"))
    assert bucket.executable()
    unit = compiler.compile(ScopeDraft(measure=Measure(kind="count_distinct", unit="colour")))
    assert unit.spec is None and unit.unsupported[0].reason == "measure_unsupported"
    for compiled in (text_sum, no_basis, bad_basis, unit):
        assert compiled.spec is None  # an unsupported total is never a count


def test_compile_prepends_request_filters_and_refuses_overrides(compiler, v2_backend, v2_gen):
    compiled = compiler.compile(ScopeDraft(predicates=[eq("status", "open")], measure=Measure(kind="count")), {"project": "alpha"})
    assert [(p.field, p.value) for p in compiled.spec.predicates] == [("project", "alpha"), ("status", "open")]
    same = compiler.compile(ScopeDraft(predicates=[eq("project", "alpha")]), {"project": "alpha"})
    assert len(same.spec.predicates) == 1
    with pytest.raises(DecisionFailure, match="overrides a request filter"):
        compiler.compile(ScopeDraft(predicates=[eq("project", "Beta")]), {"project": "alpha"})
    dropped = compiler.compile(ScopeDraft(predicates=[eq("region", "north")], measure=Measure(kind="count")))
    assert dropped.spec is None and dropped.unsupported[0].reason == "field_not_filterable"  # never a wider count


def test_zero_count_with_absent_filter_value_is_flagged(compiler, v2_backend, v2_gen):
    compiled = compiler.compile(ScopeDraft(measure=Measure(kind="count")), {"project": "Delta"})
    assert compiled.executable() and compiled.zero_match_fields == ["project"]
    result = v2_backend.records.execute(v2_gen, compiled.spec, [])
    assert result.value == 0 and result.zero_match is True and result.exactness == "exact"
