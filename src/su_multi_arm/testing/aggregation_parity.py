"""Client/native aggregation parity kit for the Lucene records facet (``records.aggregation``).

``check_parity(client, native)`` writes the same synthetic records to two adapters (one per
aggregation mode) and asserts that every ``ScopeSpec`` of ``parity_specs()`` gives an identical
``ScopeResult`` on both: counts, groups and cross-tabs (nulls included), calendar groups in a
declared zone, ranking with ties, period comparison, shares and numerators, distinct counts
(values, equivalence unit, record identity), numeric and temporal measures, the record list, the
null counts of negated predicates, unsupported constraints and the ACL. ``check_native_acl`` asserts
that a record hidden by the ACL contributes to no aggregate; ``check_native_bounds`` the native-only
caps (matched ids, groups, exact distinct counts), each of which is reported, never a silent cut.

Synthetic data only. Usage: build two adapters on the same server, one with
``aggregation="native"``, and call the functions; the external conformance suite does this.
"""

from __future__ import annotations

from typing import Any

from ..core.types import (
    FieldSpec,
    GenerationId,
    Measure,
    MetadataSchema,
    NamedState,
    Period,
    Predicate,
    Provenance,
    ScopeSpec,
    Sort,
    StructuredRecord,
    TextMatch,
)

GENP = GenerationId(dataset="conformance", generation="d" * 32, index_format=3, fingerprint="f" * 64)
PROV = Provenance(extractor="conformance")
PSCHEMA = MetadataSchema(
    canonical_unit="item",
    equivalence_field="ticket",
    timezone="Europe/Berlin",
    fields=[
        FieldSpec(name="team", type="string", listable=True, groupable=True, casefold_match=True),
        FieldSpec(name="state", type="string", groupable=True, states=[NamedState(name="open", values=["done"], negate=True)]),
        FieldSpec(name="opened_at", type="datetime", time_basis="opened", groupable=True, sortable=True),
        FieldSpec(name="day", type="date", time_basis="day", groupable=True, sortable=True),
        FieldSpec(name="score", type="number", groupable=True, sortable=True),
        FieldSpec(name="size", type="integer", groupable=True, sortable=True),
        FieldSpec(name="flag", type="boolean", groupable=True, sortable=True),
        FieldSpec(name="org", type="string", groupable=True, sortable=True),
        FieldSpec(name="ticket", type="id"),
        FieldSpec(name="tenant", type="string"),
    ],
)
# ``extra`` is undeclared and holds several JSON types; ``tenant`` drives the ACL checks.
_ROWS: list[tuple[str, dict[str, Any]]] = [
    ("r01", {"team": "A", "state": "done", "opened_at": "2024-01-05T10:00:00+00:00", "day": "2024-01-05", "score": 3, "size": 10, "flag": True, "org": "X", "ticket": "T1", "tenant": "t1", "extra": "x"}),
    ("r02", {"team": "A", "state": "new", "opened_at": "2024-01-31T23:30:00+00:00", "day": "2024-01-31", "score": 5.5, "size": 20, "flag": False, "org": "Y", "ticket": "T1", "tenant": "t1", "extra": 2}),
    ("r03", {"team": "B", "state": None, "opened_at": "2024-02-03T10:00:00+01:00", "day": None, "score": None, "size": None, "flag": None, "org": None, "ticket": None, "tenant": "t1", "extra": 2.5}),
    ("r04", {"team": "B", "state": "new", "opened_at": "2024-02-10T10:00:00-05:00", "day": "2024-02-10", "score": 1, "size": 5, "flag": True, "ticket": "T2", "tenant": "t1", "extra": True}),
    ("r05", {"team": "C", "state": "done", "opened_at": "2024-03-01T00:30:00+01:00", "day": "2024-03-01", "score": 4, "size": 7, "flag": False, "org": "X", "ticket": None, "tenant": "t1"}),
    ("r06", {"team": "C", "state": "new", "opened_at": "2024-03-15T10:00:00+00:00", "day": "2024-03-15", "score": 4, "size": 7, "flag": True, "org": None, "ticket": "T3", "tenant": "t1", "extra": "x"}),
    ("r07", {"team": "A", "state": "closed", "opened_at": "2024-03-15T10:00:00+00:00", "day": "2024-03-15", "score": 2.25, "size": 1, "org": "Z", "ticket": "T3", "tenant": "t1"}),
    ("r08", {"team": "D", "state": "new", "opened_at": None, "day": "2023-12-31", "score": 0, "size": 0, "flag": False, "org": "Y", "ticket": "T4", "tenant": "t1"}),
    ("r09", {"team": "b", "state": "done", "opened_at": "2023-12-31T23:59:00+00:00", "day": "2024-01-01", "score": -1, "size": 3, "org": "X", "ticket": None, "tenant": "t1"}),
    # Visible only without the tenant ACL: a distinctive value in every field.
    ("r10", {"team": "HIDDEN", "state": "secret", "opened_at": "2030-06-01T00:00:00+00:00", "day": "2030-06-01", "score": 1000, "size": 999, "flag": True, "org": "SECRET", "ticket": "T999", "tenant": "t2", "extra": "hidden"}),
]
ACL = [Predicate(field="tenant", op="eq", value="t1")]


def parity_rows() -> list[StructuredRecord]:
    return [StructuredRecord(schema_id="document", id=i, values={"id": i, **v}, provenance=PROV) for i, v in _ROWS]


def parity_specs() -> list[ScopeSpec]:
    """Every compiled operation, alone and combined (each spec runs with and without the ACL)."""
    measures = [
        None,
        Measure(kind="count"),
        Measure(kind="sum", field="score"),
        Measure(kind="avg", field="score"),
        Measure(kind="min", field="size"),
        Measure(kind="max", field="score"),
        Measure(kind="first_seen", field="opened_at"),
        Measure(kind="last_seen", field="opened_at"),
        Measure(kind="first_seen", field="day"),
        Measure(kind="bucket", field="opened_at", granularity="day"),
        Measure(kind="bucket", field="opened_at", granularity="month"),
        Measure(kind="bucket", field="day", granularity="year"),
        Measure(kind="count_distinct", field="org"),
        Measure(kind="count_distinct", unit="ticket"),
        Measure(kind="count_distinct"),
        Measure(kind="count_distinct", unit="id"),
        Measure(kind="count_distinct", unit="extra"),
        Measure(kind="share"),
    ]
    periods = [
        Period(label="jan", lower="2024-01-01T00:00:00+01:00", upper="2024-02-01T00:00:00+01:00"),
        Period(label="feb", lower="2024-02-01T00:00:00+01:00", upper="2024-03-01T00:00:00+01:00"),
        Period(label="bad", lower="yesterday", upper="2024-03-01T00:00:00+01:00"),
    ]
    specs: list[ScopeSpec] = []
    for measure in measures:
        specs.append(ScopeSpec(measure=measure))
        specs.append(ScopeSpec(group_by=["team"], measure=measure))
        specs.append(ScopeSpec(group_by=["team", "state"], measure=measure))
        specs.append(ScopeSpec(time_basis="opened_at", periods=periods, measure=measure))
    for field in ("team", "state", "score", "size", "flag", "org", "extra", "ticket"):
        specs.append(ScopeSpec(group_by=[field]))
        specs.append(ScopeSpec(group_by=[field], with_share=True, top_n=2))
        specs.append(ScopeSpec(group_by=[field], nth=2))
        specs.append(ScopeSpec(group_by=[field], order="asc", nth=1))
        specs.append(ScopeSpec(group_by=[field], sort=Sort(field=field, descending=True), top_n=3))
    for granularity in ("day", "month", "year"):
        specs.append(ScopeSpec(group_by=["opened_at"], granularity=granularity))
        specs.append(ScopeSpec(group_by=["day"], granularity=granularity, top_n=1))
        specs.append(ScopeSpec(group_by=["opened_at", "team"], granularity=granularity, measure=Measure(kind="avg", field="score")))
    avg = Measure(kind="avg", field="score")
    specs += [
        ScopeSpec(group_by=["team"], top_n=1),
        ScopeSpec(group_by=["team"], top_n=10),
        ScopeSpec(group_by=["team"], measure=avg, nth=2),
        ScopeSpec(group_by=["team"], measure=avg, top_n=1, order="asc"),
        ScopeSpec(group_by=["org"], measure=Measure(kind="count_distinct", unit="ticket"), top_n=2),
        ScopeSpec(group_by=["opened_at"]),  # raw stamps without granularity
        ScopeSpec(measure=Measure(kind="share"), numerator=[Predicate(field="state", op="eq", value="new")]),
        ScopeSpec(measure=Measure(kind="share"), numerator=[Predicate(field="id", op="in", value=["r01", "r05", "r10"])]),
        ScopeSpec(measure=Measure(kind="share"), numerator=[Predicate(field="undeclared", op="eq", value=1)]),
        ScopeSpec(measure=Measure(kind="share"), text=TextMatch(text="anything", role="numerator")),
        ScopeSpec(predicates=[Predicate(field="state", op="in", value=["done"], negate=True)]),
        ScopeSpec(predicates=[Predicate(field="state", op="eq", value="done", negate=True), Predicate(field="org", op="eq", value="X", negate=True)], group_by=["team"]),
        ScopeSpec(predicates=[Predicate(field="team", op="eq", value="a")], measure=Measure(kind="count")),
        ScopeSpec(predicates=[Predicate(field="team", op="eq", value="nobody")], group_by=["team"]),
        ScopeSpec(predicates=[Predicate(field="opened_at", op="date_interval", lower="2024-02-01T00:00:00+00:00", upper="2024-03-01T00:00:00+00:00", granularity="day")], measure=Measure(kind="sum", field="size")),
        ScopeSpec(predicates=[Predicate(field="opened_at", op="date_interval", lower="2024-02-01", granularity="day")]),
        ScopeSpec(text=TextMatch(text="anything")),
        ScopeSpec(limit=3),
        ScopeSpec(limit=20, predicates=[Predicate(field="score", op="range", lower=1)]),
    ]
    for field in ("score", "size", "opened_at", "day", "team", "flag", "org", "extra", "absent"):
        for descending in (False, True):
            specs.append(ScopeSpec(sort=Sort(field=field, descending=descending), limit=4))
            specs.append(ScopeSpec(sort=Sort(field=field, descending=descending), limit=20))
    return specs


def _seed(adapter: Any) -> None:
    adapter.catalog.put_documents(GENP, [], [])
    adapter.records.upsert(GENP, parity_rows(), PSCHEMA)


def check_parity(client: Any, native: Any) -> int:
    """Identical ``ScopeResult`` on both modes for every parity spec, with and without the ACL;
    returns the number of compared results. Mismatches raise ``AssertionError`` listing each."""
    _seed(client)
    _seed(native)
    mismatches: list[str] = []
    compared = 0
    for spec in parity_specs():
        for acl in ([], ACL):
            left = client.records.execute(GENP, spec, list(acl))
            right = native.records.execute(GENP, spec, list(acl))
            compared += 1
            if left != right or left.model_dump_json() != right.model_dump_json():
                diff = {k: (v, getattr(right, k)) for k, v in left if getattr(right, k) != v or repr(getattr(right, k)) != repr(v)}
                mismatches.append(f"{spec.model_dump(exclude_defaults=True)} acl={bool(acl)}: {diff}")
    if mismatches:
        raise AssertionError(f"{len(mismatches)} client/native mismatches:\n" + "\n".join(mismatches[:20]))
    return compared


def check_native_acl(native: Any) -> None:
    """A record hidden by the ACL never contributes to a native aggregate (value, groups, ids, list)."""
    _seed(native)
    hidden = dict(_ROWS)["r10"]
    for spec in parity_specs():
        result = native.records.execute(GENP, spec, list(ACL))
        text = result.model_dump_json()
        for marker in ("HIDDEN", "SECRET", "T999", "r10", "2030-06", "hidden"):
            if marker in text:
                raise AssertionError(f"ACL-hidden record leaked {marker!r} into {spec.model_dump(exclude_defaults=True)}")
        if result.denominator != len(_ROWS) - 1:
            raise AssertionError("the ACL narrows the denominator")
        for group in [*result.groups, *result.ties]:
            if group.value in (hidden["score"], hidden["size"]):
                raise AssertionError(f"hidden value in a group of {spec.model_dump(exclude_defaults=True)}")
    total = native.records.execute(GENP, ScopeSpec(measure=Measure(kind="sum", field="size")), list(ACL))
    if total.value != sum(v.get("size") or 0 for i, v in _ROWS if i != "r10"):
        raise AssertionError("an ACL-hidden record contributed to a sum")


def check_native_bounds(native: Any) -> None:
    """Native-only caps are reported, never silent: ids, groups and exact distinct counts."""
    _seed(native)
    saved = (native.matched_ids_cap, native.max_groups, native.distinct_exact_cap, native.max_matched)
    try:
        native.max_matched = 1  # the client-path scan bound does not apply: nothing is scanned
        native.matched_ids_cap = 3
        result = native.records.execute(GENP, ScopeSpec(), [])
        if (result.value, result.total, result.matched_ids, result.matched_ids_truncated, result.exactness) != (10, 10, ["r01", "r02", "r03"], True, "exact"):
            raise AssertionError(f"matched_ids_cap: {result}")
        native.matched_ids_cap = 10
        if native.records.execute(GENP, ScopeSpec(), []).matched_ids_truncated:
            raise AssertionError("ids at the cap are not truncated")
        native.max_groups = 2
        over = native.records.execute(GENP, ScopeSpec(group_by=["team"]), [])
        if over.exactness != "bounded" or over.groups or over.value is not None or "max_groups" not in (over.unsupported[-1].suggestion or ""):
            raise AssertionError(f"max_groups must give a bounded result without groups: {over}")
        # top_n of a count over one field is decided by a terms request of at most max_groups + 1
        # groups: the leader (3 records) is known exactly; the second rank needs more groups.
        ranked = native.records.execute(GENP, ScopeSpec(group_by=["team"], top_n=1), [])
        if ranked.exactness != "exact" or [(g.key["team"], g.count) for g in ranked.groups] != [("A", 3)] or ranked.ties:
            raise AssertionError(f"top_n decided within max_groups is exact: {ranked}")
        second = native.records.execute(GENP, ScopeSpec(group_by=["team"], nth=2), [])
        if second.exactness != "bounded" or second.groups:
            raise AssertionError(f"nth needing more than max_groups groups is bounded: {second}")
        native.max_groups = saved[1]
        native.distinct_exact_cap = 1
        approx = native.records.execute(GENP, ScopeSpec(measure=Measure(kind="count_distinct", field="org")), [])
        if approx.exactness != "approximate" or approx.value != 4 or not any("approximate" in b for b in approx.basis):
            raise AssertionError(f"distinct count above the exact cap is approximate and labelled: {approx}")
        grouped = native.records.execute(GENP, ScopeSpec(group_by=["team"], measure=Measure(kind="count_distinct", field="org")), [])
        if grouped.exactness != "approximate" or {g.key["team"]: g.value for g in grouped.groups}.get("A") != 3:
            raise AssertionError(f"grouped approximate distinct count: {grouped}")
    finally:
        native.matched_ids_cap, native.max_groups, native.distinct_exact_cap, native.max_matched = saved
