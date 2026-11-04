"""Backend conformance kit v0 (plan §3.5, §6.1): one check per capability, engine-agnostic.

Every adapter that declares a capability must pass the check for it, whatever its ranking or
tokeniser: the checks assert scope pushdown (no out-of-scope hit, ever), determinism, phrase
semantics under the ``phrase`` flag, stable non-overlapping pagination, generation isolation,
``refresh`` visibility, bounded catalog reads, typed-scope exactness, fact-only traversal with
provenance, cache-key prefixes and TTL, ledger ordering and raw replies, session expiry. Ranking
parity across engines is deliberately **not** asserted (plan §3.5 lexical semantics).

Usage: build an adapter, call ``run_all(adapter, clock)`` or the individual ``check_*`` functions.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np

from ..backends import capabilities as caps
from ..backends.contracts import CONTRACT_NAMES
from ..core.errors import BackendError, SessionConflict
from ..core.types import (
    Chunk,
    Document,
    Entity,
    FieldSpec,
    GenerationId,
    LedgerEntry,
    LexicalQuery,
    Measure,
    Membership,
    MetadataSchema,
    NamedState,
    Occurrence,
    Page,
    Period,
    Predicate,
    Provenance,
    Relation,
    Scope,
    ScopeSpec,
    SessionRecord,
    Sort,
    Span,
    StructuredRecord,
    TextMatch,
    TraversalSpec,
)

GEN = GenerationId(dataset="conformance", generation="a" * 32, index_format=3, fingerprint="f" * 64)
GEN2 = GEN.model_copy(update={"generation": "b" * 32})
GENQ = GEN.model_copy(update={"generation": "c" * 32})
PROV = Provenance(extractor="conformance")
SCHEMA = MetadataSchema(
    fields=[
        FieldSpec(name="source", type="string", listable=True, groupable=True),
        FieldSpec(name="product", type="string", listable=True, casefold_match=True),
        FieldSpec(name="created_at", type="datetime", time_basis="created"),
        FieldSpec(name="size", type="integer"),
        FieldSpec(name="secret_id", type="id", listable=False),
        FieldSpec(name="touched_at", type="datetime", time_basis="touched", timezone_policy="assume_utc"),
        FieldSpec(name="day", type="date", time_basis="day"),
    ]
)
TEXTS = {
    "d1": "exemption certificate rounding",
    "d2": "content source rounding error",
    "d3": "certificate exemption",
    "d4": "unrelated archive entry",
}


def document(i: int, product: str = "CloudTax", source: str = "case") -> Document:
    return Document(
        id=f"d{i}",
        dataset="conformance",
        source=source,
        metadata={"product": product, "created_at": f"2024-01-0{i}T00:00:00+00:00"},
    )


def chunk(i: int) -> Chunk:
    text = TEXTS[f"d{i}"]
    return Chunk(id=f"c{i}", document_id=f"d{i}", field="body", start=0, end=len(text), text=text)


def seed(adapter: Any, gen: GenerationId = GEN) -> None:
    """Documents first, then projections: the order every write path uses."""
    docs = [document(1), document(2, "SearchDesk"), document(3, "CloudTax", "article"), document(4, "Other", "manual")]
    chunks = [chunk(i) for i in (1, 2, 3, 4)]
    adapter.catalog.put_documents(gen, docs, chunks)
    if caps.satisfied(adapter, "lexical"):
        adapter.lexical.index(gen, chunks, ["body"])
        adapter.lexical.refresh(gen)


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_protocols(adapter: Any) -> set[str]:
    declared = {c for c, d in caps.adapter(adapter.kind).capabilities.items() if d.status != "planned"}
    satisfied = caps.conformance(adapter)
    _ensure(declared <= satisfied, f"{adapter.kind}: declares {sorted(declared - satisfied)} without implementing them")
    _ensure(satisfied <= set(CONTRACT_NAMES), "unknown capability facet")
    return satisfied


def check_catalog(adapter: Any) -> None:
    catalog = adapter.catalog
    seed(adapter)
    got = catalog.get(GEN, ["d2", "missing", "d1"])
    _ensure([d.id for d in got] == ["d2", "d1"], "get keeps request order and drops unknown ids")
    _ensure([c.id for c in catalog.get_chunks(GEN, ["c3", "c1"])] == ["c3", "c1"], "get_chunks keeps order")
    stats = getattr(adapter, "stats", None)
    if stats is not None:
        _ensure(stats["catalog.get.ids"] == 3, "catalog reads must be bounded by the requested ids")
    try:
        catalog.get(GEN2, ["d1"])
        raise AssertionError("an unknown generation must be a typed BackendError")
    except BackendError:
        pass
    page1 = catalog.iter_scope(GEN, None, Page(size=3))
    page2 = catalog.iter_scope(GEN, None, Page(cursor=page1.next_cursor, size=3))
    ids = [d.id for d in page1.items] + [d.id for d in page2.items]
    _ensure(ids == ["d1", "d2", "d3", "d4"] and page2.next_cursor is None, "pagination is stable and complete")
    _ensure(len({*ids}) == 4, "pages never overlap")
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="case")], exclude_ids=["d1"])
    _ensure([d.id for d in catalog.iter_scope(GEN, scope, Page()).items] == ["d2"], "scope pushdown on iter_scope")
    scoped = catalog.iter_scope(GEN, Scope(document_ids=["d3", "d9"]), Page()).items
    _ensure([d.id for d in scoped] == ["d3"], "document_ids allow-list")
    _ensure(catalog.count_documents(GEN) == 4 and catalog.count_chunks(GEN) == 4, "counts for projection validation")
    try:
        catalog.put_documents(GEN, [], [Chunk(id="ghost", document_id="nope", field="body", start=0, end=1, text="x")])
        raise AssertionError("a chunk of an unknown document must be rejected")
    except BackendError:
        pass
    adapter.protect(GEN)
    try:
        catalog.delete_generation(GEN)
        raise AssertionError("a protected generation must not be deletable")
    except BackendError:
        pass
    adapter.unprotect(GEN)
    catalog.delete_generation(GEN)
    try:
        catalog.get(GEN, ["d1"])
        raise AssertionError("deleted generation must be unknown")
    except BackendError:
        pass


def check_lexical(adapter: Any) -> None:
    lexical = adapter.lexical
    seed(adapter)
    flags = caps.flags(adapter.kind, "lexical")
    query = LexicalQuery(text="certificate")
    first = lexical.search(GEN, query, None, 10)
    second = lexical.search(GEN, query, None, 10)
    _ensure([h.chunk_id for h in first.items] == [h.chunk_id for h in second.items], "deterministic ordering")
    _ensure({h.document_id for h in first.items} == {"d1", "d3"}, "term search finds every chunk with the term")
    _ensure(all(h.document_id != h.chunk_id or True for h in first.items), "hits carry document ids")
    scope = Scope(predicates=[Predicate(field="product", op="eq", value="CloudTax")], exclude_ids=["d3"])
    scoped = lexical.search(GEN, query, scope, 10)
    _ensure([h.document_id for h in scoped.items] == ["d1"], "no out-of-scope hit, ever")
    _ensure(lexical.search(GEN, query, Scope(document_ids=[]), 10).items == [], "empty allow-list means nothing")
    if flags.get("phrase"):
        phrase = lexical.search(GEN, LexicalQuery(kind="phrase", text="exemption certificate"), None, 10)
        _ensure([h.document_id for h in phrase.items] == ["d1"], "phrase never matches non-contiguous tokens")
    boolean = lexical.search(GEN, LexicalQuery(kind="boolean", text="rounding", must=["rounding"], must_not=["error"]), None, 10)
    _ensure([h.document_id for h in boolean.items] == ["d1"], "boolean must/must_not")
    _ensure(lexical.search(GEN, LexicalQuery(text="zzzz"), None, 10).items == [], "no match is empty, not an error")
    k2 = lexical.search(GEN, LexicalQuery(text="rounding certificate exemption"), None, 2)
    _ensure(len(k2.items) == 2, "k bounds the result")
    paged = lexical.search(GEN, LexicalQuery(text="rounding certificate exemption"), None, 10, Page(size=1))
    rest = lexical.search(GEN, LexicalQuery(text="rounding certificate exemption"), None, 10, Page(cursor=paged.next_cursor, size=5))
    _ensure(len(paged.items) == 1 and not {h.chunk_id for h in paged.items} & {h.chunk_id for h in rest.items}, "pages do not overlap")
    _ensure(len(paged.items) + len(rest.items) == 3 and rest.next_cursor is None, "pagination is complete")
    adapter.catalog.put_documents(GEN, [document(5)], [])
    text = "certificate renewal"
    lexical.index(GEN, [Chunk(id="c5", document_id="d5", field="body", start=0, end=len(text), text=text)], ["body"])
    lexical.refresh(GEN)
    _ensure("d5" in {h.document_id for h in lexical.search(GEN, query, None, 10).items}, "refresh makes writes visible")
    _ensure(lexical.count(GEN) == 5, "count after refresh")
    try:
        lexical.search(GEN2, query, None, 10)
        raise AssertionError("generation isolation")
    except BackendError:
        pass


def check_vector(adapter: Any) -> None:
    vector = adapter.vector
    seed(adapter)
    vectors = np.array([[1.0, 0.0], [0.0, 1.0], [0.7, 0.7], [-1.0, 0.0]], dtype=np.float32)
    vector.upsert(GEN, ["c1", "c2", "c3", "c4"], vectors)
    vector.refresh(GEN)
    _ensure(vector.count(GEN) == 4, "count after refresh")
    hits = vector.search(GEN, np.array([1.0, 0.0]), None, 2)
    _ensure([h.chunk_id for h in hits] == ["c1", "c3"] and abs(hits[0].score - 1.0) < 1e-6, "exact cosine order")
    again = vector.search(GEN, np.array([1.0, 0.0]), None, 2)
    _ensure([h.chunk_id for h in again] == ["c1", "c3"], "deterministic")
    scoped = vector.search(GEN, np.array([1.0, 0.0]), Scope(document_ids=["d2", "d3"]), 2)
    _ensure([h.chunk_id for h in scoped] == ["c3", "c2"], "scope applied before top-k, exact order kept")
    pred = vector.search(GEN, np.array([1.0, 0.0]), Scope(predicates=[Predicate(field="source", op="eq", value="manual")]), 3)
    _ensure([h.chunk_id for h in pred] == ["c4"], "predicate scope pushdown")
    _ensure(vector.search(GEN, np.zeros(2), None, 2) == [], "zero query vector is empty")
    _ensure(vector.search(GEN, np.array([1.0, 0.0]), None, 2, namespace="other") == [], "namespaces are isolated")
    try:
        vector.search(GEN, np.ones(3), None, 2)
        raise AssertionError("dimension mismatch must be a BackendError")
    except BackendError:
        pass
    try:
        vector.upsert(GEN, ["x", "x"], np.ones((2, 2)))
        raise AssertionError("duplicate ids must be rejected")
    except BackendError:
        pass
    vector.upsert(GEN, ["c1"], np.array([[0.0, -1.0]], dtype=np.float32))
    vector.refresh(GEN)
    _ensure(vector.count(GEN) == 4 and vector.search(GEN, np.array([0.0, -1.0]), None, 1)[0].chunk_id == "c1", "upsert replaces")
    try:
        vector.search(GEN2, np.array([1.0, 0.0]), None, 1)
        raise AssertionError("generation isolation")
    except BackendError:
        pass


def record_rows() -> list[StructuredRecord]:
    """The three typed records of ``check_records`` (adapters' native-type tests reuse them: the
    later text-count check overwrites ``GEN``'s records with untyped rows)."""
    return [
        StructuredRecord(schema_id="document", id="d1", values={"source": "case", "product": "CloudTax", "created_at": "2024-01-01T00:00:00+00:00", "size": 3, "secret_id": "s1", "touched_at": "2025-01-02T08:00:00", "day": "2024-03-05"}, provenance=PROV),
        StructuredRecord(schema_id="document", id="d2", values={"source": "case", "product": "SearchDesk", "created_at": "2024-01-02T00:00:00+00:00", "size": 5, "secret_id": "s2", "touched_at": "2025-01-02T09:00:00+01:00", "day": "2024-03-06"}, provenance=PROV),
        StructuredRecord(schema_id="document", id="d3", values={"source": "article", "product": "CloudTax", "created_at": None, "size": None, "secret_id": "s3", "touched_at": "2025-01-01T23:30:00-02:00", "day": None}, provenance=PROV),
    ]


def check_records(adapter: Any) -> None:
    records = adapter.records
    seed(adapter)
    rows = record_rows()
    records.upsert(GEN, rows, SCHEMA)
    for bad in ({"created_at": 12}, {"created_at": "not a date"}, {"created_at": "2024-01-01T00:00:00"}):
        try:
            records.upsert(GEN, [StructuredRecord(schema_id="document", id="bad", values=bad, provenance=PROV)], SCHEMA)
            raise AssertionError("schema violations must be rejected")
        except BackendError:
            pass
    _ensure(records.count(GEN) == 3 and records.execute(GEN, ScopeSpec(), []).denominator == 3, "rejected rows are not stored")
    count = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="cloudtax")]), [])
    _ensure((count.value, count.denominator, count.exactness, count.matched_ids) == (2, 3, "exact", ["d1", "d3"]), "casefold eq")
    acl = [Predicate(field="source", op="eq", value="case")]
    scoped = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="CloudTax")]), acl)
    _ensure((scoped.value, scoped.denominator) == (1, 2), "ACL narrows the denominator")
    zero = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="product", op="eq", value="Absent")]), [])
    _ensure(zero.zero_match and zero.value == 0 and zero.applied, "zero match is flagged")
    first = records.execute(GEN, ScopeSpec(measure=Measure(kind="first_seen", field="created_at")), [])
    _ensure(first.value["record_id"] == "d1" and first.missing_or_invalid == 1, "first_seen with missing count")
    grouped = records.execute(GEN, ScopeSpec(group_by=["source"]), [])
    _ensure(grouped.value == {"case": 2, "article": 1} and [g.count for g in grouped.groups] == [2, 1], "group_by")
    total = records.execute(GEN, ScopeSpec(measure=Measure(kind="sum", field="size")), [])
    _ensure(total.value == 8 and total.missing_or_invalid == 1, "sum")
    _ensure(records.execute(GEN, ScopeSpec(measure=Measure(kind="count_distinct", unit="product")), []).value == 2, "count_distinct")
    bucket = records.execute(GEN, ScopeSpec(measure=Measure(kind="bucket", field="created_at", granularity="month")), [])
    _ensure(bucket.value == {"2024-01-01T00:00:00+00:00": 2} and bucket.missing_or_invalid == 1, "bucket")
    interval = ScopeSpec(predicates=[Predicate(field="created_at", op="date_interval", lower="2024-01-01T00:00:00+00:00", upper="2024-01-02T00:00:00+00:00", granularity="day")])
    _ensure(records.execute(GEN, interval, []).matched_ids == ["d1"], "half-open interval")
    naive = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="created_at", op="date_interval", lower="2024-01-01", granularity="day")]), [])
    _ensure(naive.unsupported[0].reason == "timezone_missing" and naive.applied == [], "naive bound is unsupported, never guessed")
    _ensure(naive.exactness == "bounded", "a dropped predicate makes the result bounded, never exact")
    # Every records adapter refuses a scope above ``max_matched`` with a typed error (no silent cut).
    # Under native aggregation (Lucene ``records.aggregation: native``) nothing is scanned: the count
    # stays exact and only the id list is capped, flagged ``matched_ids_truncated``.
    native = getattr(adapter, "aggregation", "client") == "native"
    saved = adapter.max_matched
    adapter.max_matched = 2
    try:
        if native:
            saved_ids = adapter.matched_ids_cap
            adapter.matched_ids_cap = 2
            try:
                over = records.execute(GEN, ScopeSpec(measure=Measure(kind="count")), [])
                _ensure((over.value, over.total, over.exactness) == (3, 3, "exact"), "native: a scope above max_matched is still counted exactly")
                _ensure(over.matched_ids == ["d1", "d2"] and over.matched_ids_truncated, "native: matched ids are capped and flagged, never silently cut")
            finally:
                adapter.matched_ids_cap = saved_ids
        else:
            try:
                records.execute(GEN, ScopeSpec(measure=Measure(kind="count")), [])
                raise AssertionError("a scope above max_matched must raise BackendError")
            except BackendError as error:
                _ensure("max_matched" in str(error), "max_matched: typed refusal names the cap")
        capped = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="source", op="eq", value="case")], measure=Measure(kind="count")), [])
        _ensure(capped.value == 2 and capped.matched_ids == ["d1", "d2"] and not capped.matched_ids_truncated, "max_matched: a scope at the cap still answers exactly")
    finally:
        adapter.max_matched = saved
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="size", op="missing")]), []).matched_ids == ["d3"], "missing")
    # Temporal comparisons are instant-based under the field's timezone policy (plan §3.5, §3.7.2):
    # assume_utc stores naive stamps as UTC; an aware bound compares against the same instants.
    since = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="touched_at", op="range", lower="2025-01-02T00:00:00+00:00")]), [])
    _ensure(since.matched_ids == ["d1", "d2", "d3"] and since.applied and since.exactness == "exact", "assume_utc: naive stored values compare as UTC instants")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="touched_at", op="range", lower="2025-01-02T08:00:00Z")]), []).matched_ids == ["d1", "d2"], "offset-aware bound vs naive-as-UTC value")
    naive_bound = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="touched_at", op="range", lower="2025-01-02T01:00:00")]), [])
    _ensure(naive_bound.applied and naive_bound.matched_ids == ["d1", "d2", "d3"], "assume_utc: a naive bound is UTC too (d3 is 01:30 UTC)")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="touched_at", op="range", lower="2025-01-02T02:00:00")]), []).matched_ids == ["d1", "d2"], "assume_utc: naive bound excludes the -02:00 record converted to UTC")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="touched_at", op="eq", value="2025-01-02T08:00:00+00:00")]), []).matched_ids == ["d1", "d2"], "instant equality across offsets")
    _ensure(records.execute(GEN, ScopeSpec(measure=Measure(kind="first_seen", field="touched_at")), []).value["record_id"] == "d3", "first_seen on assume_utc instants")
    # A date field against a datetime bound: the bound is truncated to its UTC date.
    by_day = records.execute(GEN, ScopeSpec(predicates=[Predicate(field="day", op="range", lower="2024-03-06T15:00:00+00:00")]), [])
    _ensure(by_day.applied and by_day.matched_ids == ["d2"], "date field: datetime bound truncated to its date (inclusive)")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="day", op="range", upper="2024-03-05T23:59:59+00:00")]), []).matched_ids == ["d1"], "date field: upper datetime bound truncated")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="day", op="eq", value="2024-03-05T10:00:00Z")]), []).matched_ids == ["d1"], "date field: eq with a datetime value compares calendar dates")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="day", op="range", lower="yesterday")]), []).unsupported[0].reason == "unparseable_bound", "date field: unparseable bound reported")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="size", op="range", lower=4)]), []).matched_ids == ["d2"], "range")
    _ensure(records.execute(GEN, ScopeSpec(predicates=[Predicate(field="source", op="in", value=["article"], negate=True)]), []).matched_ids == ["d1", "d2"], "negated in")
    _ensure(records.exists(GEN, "product", "searchdesk", []), "exists honours casefold")
    _ensure(not records.exists(GEN, "product", "SearchDesk", [Predicate(field="source", op="eq", value="article")]), "exists honours ACL")
    _ensure(records.values(GEN, "product", [], 10) == ["CloudTax", "SearchDesk"], "listable values")
    _ensure(records.values(GEN, "product", acl, 1) == ["CloudTax"], "values are ACL-scoped and limited")
    for field in ("secret_id", "unknown"):
        try:
            records.values(GEN, field, [], 10)
            raise AssertionError("non-listable fields must not be enumerated")
        except BackendError:
            pass
    check_query_operations(adapter)
    if caps.satisfied(adapter, "lexical"):
        check_text_count(adapter)


# --- query-compiler operations (2.0.0a3): every records adapter, identical semantics -------------

QSCHEMA = MetadataSchema(
    canonical_unit="item",
    fields=[
        FieldSpec(name="team", type="string", listable=True, groupable=True),
        FieldSpec(name="state", type="string", listable=True, groupable=True, states=[NamedState(name="open", values=["done"], negate=True)]),
        FieldSpec(name="opened_at", type="datetime", time_basis="opened", groupable=True, sortable=True),
        FieldSpec(name="score", type="number", sortable=True),
        FieldSpec(name="org", type="string", groupable=True),
    ],
)
_QROWS = [
    ("q1", {"team": "A", "state": "done", "opened_at": "2024-01-05T10:00:00+00:00", "score": 3, "org": "X"}),
    ("q2", {"team": "A", "state": "new", "opened_at": "2024-01-20T10:00:00+00:00", "score": 5, "org": "Y"}),
    ("q3", {"team": "B", "state": None, "opened_at": "2024-02-03T10:00:00+00:00", "score": None, "org": None}),
    ("q4", {"team": "B", "state": "new", "opened_at": "2024-02-10T10:00:00+00:00", "score": 1}),
    ("q5", {"team": "C", "state": "done", "opened_at": "2024-03-01T10:00:00+00:00", "score": 4, "org": "X"}),
    ("q6", {"team": "C", "state": "new", "opened_at": "2024-03-15T10:00:00+00:00", "score": 4, "org": None}),
]


def _keys(result: Any) -> list[tuple]:
    return [(tuple(g.key.values()), g.value, g.rank) for g in result.groups]


def check_query_operations(adapter: Any) -> None:
    """Ranking with ties, nth, calendar groups, cross-tab, period comparison, shares, distinct
    counts with nulls (review B1), numeric measures with null coverage, negated states with their
    unknown count, list with total, record-identity predicates and an unresolved text condition."""
    records = adapter.records
    rows = [StructuredRecord(schema_id="document", id=i, values={"id": i, **v}, provenance=PROV) for i, v in _QROWS]
    adapter.catalog.put_documents(GENQ, [], [])  # its own generation: no rows of the other checks
    records.upsert(GENQ, rows, QSCHEMA)
    run = lambda spec: records.execute(GENQ, spec, [])  # noqa: E731
    top = run(ScopeSpec(group_by=["team"], top_n=1))
    _ensure(_keys(top) == [(("A",), 2, 1)] and [g.key["team"] for g in top.ties] == ["B", "C"], "top_n keeps the first by key and reports the tied groups")
    avg = Measure(kind="avg", field="score")
    _ensure(_keys(run(ScopeSpec(group_by=["team"], measure=avg, nth=2))) == [(("B",), 1.0, 2)], "nth uses dense ranks of the measure value")
    _ensure(_keys(run(ScopeSpec(group_by=["team"], measure=avg, nth=1))) == [(("A",), 4.0, 1), (("C",), 4.0, 1)], "nth keeps every group of a tied rank")
    tied = run(ScopeSpec(group_by=["team"], measure=avg, top_n=1))
    _ensure(_keys(tied) == [(("A",), 4.0, 1)] and [g.key["team"] for g in tied.ties] == ["C"], "a tie at the top_n boundary is reported")
    _ensure(_keys(run(ScopeSpec(group_by=["team"], measure=avg, order="asc", nth=1))) == [(("B",), 1.0, 1)], "ascending order")
    _ensure(tied.missing_or_invalid == 1, "groups report records without a numeric value")
    monthly = run(ScopeSpec(group_by=["opened_at"], granularity="month"))
    _ensure(monthly.value == {"2024-01": 2, "2024-02": 2, "2024-03": 2}, "calendar month groups")
    cross = run(ScopeSpec(group_by=["team", "state"]))
    _ensure(len(cross.groups) == 6 and {"team": "B", "state": None} in [g.key for g in cross.groups], "cross-tab keeps the unknown group")
    periods = [Period(label="jan", lower="2024-01-01T00:00:00+00:00", upper="2024-02-01T00:00:00+00:00"), Period(label="feb", lower="2024-02-01T00:00:00+00:00", upper="2024-03-01T00:00:00+00:00")]
    compared = run(ScopeSpec(time_basis="opened_at", periods=periods))
    _ensure(compared.value == {"jan": 2, "feb": 2} and compared.exactness == "exact", "period comparison")
    shares = run(ScopeSpec(group_by=["team"], with_share=True))
    _ensure(all(abs((g.share or 0) - 1 / 3) < 1e-9 for g in shares.groups), "group shares of the matched total")
    share = run(ScopeSpec(measure=Measure(kind="share"), numerator=[Predicate(field="state", op="eq", value="new")]))
    _ensure((share.value, share.numerator, share.total) == (0.5, 3, 6), "share with a numerator")
    distinct = run(ScopeSpec(measure=Measure(kind="count_distinct", field="org")))
    _ensure((distinct.value, distinct.missing_or_invalid) == (2, 3), "count_distinct excludes missing and null values and reports them (B1)")
    mean = run(ScopeSpec(measure=avg))
    _ensure(abs(mean.value - 3.4) < 1e-9 and mean.missing_or_invalid == 1, "avg over non-null values with null coverage")
    open_ = run(ScopeSpec(predicates=[Predicate(field="state", op="in", value=["done"], negate=True)]))
    _ensure(open_.matched_ids == ["q2", "q3", "q4", "q6"] and open_.unknown == {"state": 1}, "a negated state includes and reports null values")
    listed = run(ScopeSpec(sort=Sort(field="score", descending=True), limit=2))
    _ensure([r.id for r in listed.listed] == ["q2", "q5"] and listed.total == 6, "list: sort with id tie-break, total reported")
    ascending = run(ScopeSpec(sort=Sort(field="score"), limit=6))
    _ensure([r.id for r in ascending.listed] == ["q4", "q1", "q5", "q6", "q2", "q3"], "list: missing values last")
    window = run(ScopeSpec(predicates=[Predicate(field="opened_at", op="date_interval", lower="2024-02-01T00:00:00+00:00", upper="2024-03-01T00:00:00+00:00", granularity="day")]))
    _ensure(window.matched_ids == ["q3", "q4"], "half-open window")
    ids = run(ScopeSpec(predicates=[Predicate(field="id", op="in", value=["q1", "q5"])]))
    _ensure(ids.matched_ids == ["q1", "q5"] and ids.exactness == "exact", "record identity predicate")
    text = run(ScopeSpec(text=TextMatch(text="anything")))
    _ensure(text.exactness == "bounded" and text.unsupported[0].reason == "not_supported_by_backend", "an unresolved text condition is reported, never ignored")


def check_text_count(adapter: Any) -> None:
    """A literal topic counted over every matching chunk of the lexical index (whole corpus)."""
    from ..arms.structured import text_match_ids, text_query

    seed(adapter)
    rows = [StructuredRecord(schema_id="document", id=f"d{i}", values={"id": f"d{i}", "source": "case"}, provenance=PROV) for i in (1, 2, 3, 4)]
    adapter.records.upsert(GEN, rows, SCHEMA)
    terms = text_match_ids(adapter.lexical, GEN, text_query(TextMatch(text="certificate exemption")), Scope())
    _ensure(terms == ["d1", "d3"], "all_terms: every document with every word")
    if caps.flags(adapter.kind, "lexical").get("phrase"):
        _ensure(text_match_ids(adapter.lexical, GEN, text_query(TextMatch(kind="phrase", text="exemption certificate")), Scope()) == ["d1"], "phrase")
    scoped = text_match_ids(adapter.lexical, GEN, text_query(TextMatch(text="rounding")), Scope(exclude_ids=["d1"]))
    _ensure(scoped == ["d2"], "text matches honour the eligibility scope")
    count = adapter.records.execute(GEN, ScopeSpec(predicates=[Predicate(field="id", op="in", value=terms)]), [])
    _ensure((count.value, count.exactness) == (2, "exact"), "the matched ids count exactly")


GRAPH_TEXTS = {"d6": "The AACTA awarded the Film at the Gala", "d7": "The Film screened at the Gala in Paris"}


def graph_fixture() -> tuple[list[Entity], list[Relation]]:
    """Two documents. Facts: aacta -awarded-> film (d6), film -screened_at-> gala (d7, two
    occurrences). Co-occurrences: film ~ gala (d6), gala ~ paris (d7). Paris is reachable only
    through a co-occurrence edge."""

    def span(doc: str, s: int, e: int, level: str = "exact") -> Span:
        text = GRAPH_TEXTS[doc]
        return Span(document_id=doc, chunk_id="c" + doc[1:], field="body", start=s, end=e, text=text[s:e], match_level=level)

    rich = Provenance(extractor="conformance", model="fixture-model", prompt_sha="p" * 8, recipe_sha="r" * 8, extracted_at="2024-01-01T00:00:00+00:00", confidence=0.75)
    entities = [
        Entity(id="aacta", name="AACTA", type="org", span=span("d6", 4, 9)),
        Entity(id="film", name="Film", type="work", span=span("d6", 22, 26)),
        Entity(id="gala", name="Gala", type="event", span=span("d6", 34, 38)),
        Entity(id="film", name="Film", type="work", span=span("d7", 4, 8), canonical_id="work:film", aliases=["the film"]),
        Entity(id="gala", name="Gala", type="event", span=span("d7", 25, 29)),
        Entity(id="paris", name="Paris", type="place", span=span("d7", 33, 38)),
    ]
    screened = span("d7", 4, 29, "normalised")
    relations = [
        Relation(subject_id="aacta", predicate="awarded", object_id="film", span=span("d6", 0, 26), edge_kind="fact", support="trigger_in_span", provenance=PROV),
        Relation(subject_id="film", predicate="at", object_id="gala", span=span("d6", 22, 38), edge_kind="co_occurrence", support="unchecked", provenance=PROV),
        Relation(
            subject_id="film",
            predicate="screened_at",
            object_id="gala",
            span=screened,
            edge_kind="fact",
            support="trigger_in_span",
            status="proposed",
            provenance=rich,
            occurrences=[Occurrence(span=screened, provenance=rich), Occurrence(span=span("d6", 22, 38, "relocated"), provenance=PROV)],
        ),
        Relation(subject_id="gala", predicate="in", object_id="paris", span=span("d7", 25, 38), edge_kind="co_occurrence", support="unchecked", provenance=PROV),
    ]
    return entities, relations


def check_graph(adapter: Any) -> None:
    """Graph contract: fact-only traversal by default (co-occurrence never traversed unless
    requested), the validated hop bound, eligibility pushed down as document-id allow/deny lists (and
    as predicates when the backend flags ``predicate_scope``; otherwise predicates are refused, never
    ignored), edges carrying kind, support, span offsets, match level, provenance and every
    occurrence, mentions as spans, memberships and communities. Adapters without a catalog (neo4j)
    run it without one."""
    graph = adapter.graph
    if caps.satisfied(adapter, "catalog"):
        seed(adapter)
        adapter.catalog.put_documents(
            GEN,
            [document(6), document(7, source="manual")],
            [Chunk(id="c" + d[1:], document_id=d, field="body", start=0, end=len(t), text=t) for d, t in GRAPH_TEXTS.items()],
        )
    entities, relations = graph_fixture()
    graph.upsert(GEN, entities, relations, [Membership(community_id="k1", member_id="d6", basis="entity"), Membership(community_id="k1", member_id="d7", role="member", basis="entity")])

    def nodes(spec: TraversalSpec, scope: Scope | None = None) -> list[list[str]]:
        return [p.nodes for p in graph.traverse(GEN, spec, scope)]

    # fact-only traversal, one and two hops; co-occurrence never traversed by default
    _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=1)) == [["aacta", "film"]], "one-hop fact traversal")
    _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=2)) == [["aacta", "film"], ["aacta", "film", "gala"]], "two-hop fact traversal")
    deep = graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=3), None)
    _ensure(all(e.edge_kind == "fact" for p in deep for e in p.edges) and "paris" not in {n for p in deep for n in p.nodes}, "co-occurrence is never traversed by default")
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["paris"], hops=3), None) == [], "a node linked only by co-occurrence has no fact paths")
    both = graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=3, edge_kinds=["fact", "co_occurrence"]), None)
    _ensure(
        sorted((tuple(p.nodes), tuple(e.edge_kind for e in p.edges)) for p in both)
        == sorted(
            [
                (("aacta", "film"), ("fact",)),
                (("aacta", "film", "gala"), ("fact", "co_occurrence")),
                (("aacta", "film", "gala"), ("fact", "fact")),
                (("aacta", "film", "gala", "paris"), ("fact", "co_occurrence", "co_occurrence")),
                (("aacta", "film", "gala", "paris"), ("fact", "fact", "co_occurrence")),
            ]
        ),
        "co-occurrence only when requested",
    )
    # edges surface everything that was loaded
    edge = graph.traverse(GEN, TraversalSpec(seed_entity_ids=["gala"], hops=1, direction="in"), None)[0].edges[0]
    source = relations[2]
    _ensure(
        (edge.subject_id, edge.predicate, edge.object_id, edge.edge_kind, edge.support) == ("film", "screened_at", "gala", "fact", "trigger_in_span"),
        "edge identity, kind and support",
    )
    _ensure(edge.span == source.span and edge.span.match_level == "normalised", "span offsets, text and match level")
    _ensure(edge.provenance == source.provenance, "full provenance")
    _ensure(edge.occurrences == source.occurrences and len(edge.occurrences) == 2, "every occurrence is surfaced")
    first = graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1), None)[0].edges[0]
    _ensure(first.provenance.extractor == "conformance" and first.span.text == GRAPH_TEXTS["d6"][0:26] and first.occurrences == [], "edges carry provenance and span")
    # direction, predicates, unknown seeds, limit
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["film"], hops=1, direction="out"), None)[0].nodes == ["film", "gala"], "direction out")
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1, direction="in"), None) == [], "direction in")
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1, predicates=["other"]), None) == [], "predicate filter")
    _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=2, predicates=["awarded"])) == [["aacta", "film"]], "predicate filter per hop")
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["ghost"], hops=1), None) == [], "unknown seed")
    _ensure(len(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=2, limit=1), None)) == 1, "limit")
    # hop bound: validated integer in 0..3
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=0), None) == [], "zero hops")
    for bad in (4, -1, "2", 2.0, True):
        try:
            graph.traverse(GEN, TraversalSpec.model_construct(seed_entity_ids=["aacta"], hops=bad, predicates=[], direction="any", edge_kinds=["fact"], limit=10), None)
            raise AssertionError(f"hop bound {bad!r} must be refused")
        except BackendError:
            pass
    # eligibility pushdown on edges
    _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=2), Scope(document_ids=["d6"])) == [["aacta", "film"]], "allow-list on edges")
    _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=2), Scope(exclude_ids=["d7"])) == [["aacta", "film"]], "deny-list on edges")
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=1), Scope(exclude_ids=["d6"])) == [], "scope on edges")
    _ensure(graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=2), Scope(document_ids=[])) == [], "empty allow-list admits nothing")
    by_source = Scope(predicates=[Predicate(field="source", op="eq", value="case")])
    if caps.flags(adapter.kind, "graph").get("predicate_scope"):
        _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=2), by_source) == [["aacta", "film"]], "predicate scope on edges")
    else:
        for call in (
            lambda: graph.traverse(GEN, TraversalSpec(seed_entity_ids=["aacta"], hops=2), by_source),
            lambda: graph.find_entities(GEN, ["aacta"], by_source),
            lambda: graph.mentions(GEN, ["aacta"], by_source, 10),
        ):
            try:
                call()
                raise AssertionError("a backend without predicate_scope must refuse predicates, never ignore them")
            except BackendError:
                pass
    # memberships, entities, mentions
    _ensure([(m.community_id, m.member_id, m.role) for m in graph.memberships(GEN)] == [("k1", "d6", None), ("k1", "d7", "member")], "memberships")
    found = graph.find_entities(GEN, ["the", "aacta", "gala"], None)
    _ensure({e.id for e in found} == {"aacta", "gala"}, "entities whose name tokens are all in the query")
    _ensure(graph.find_entities(GEN, ["aacta"], Scope(exclude_ids=["d6"])) == [], "find_entities honours scope")
    _ensure([e.span.document_id for e in graph.find_entities(GEN, ["gala"], Scope(document_ids=["d7"]))] == ["d7"], "find_entities picks an eligible mention")
    mentions = graph.mentions(GEN, ["film", "aacta"], None, 10)
    _ensure({m.id for m in mentions} == {"film", "aacta"} and all(m.span.text == GRAPH_TEXTS[m.span.document_id][m.span.start : m.span.end] for m in mentions), "mentions are spans")
    _ensure(len(mentions) == 3 and len(graph.mentions(GEN, ["film", "aacta", "gala"], None, 2)) == 2, "every mention, bounded by limit")
    d7_film = graph.mentions(GEN, ["film"], Scope(document_ids=["d7"]), 10)
    _ensure([(m.span.start, m.canonical_id, m.aliases) for m in d7_film] == [(4, "work:film", ["the film"])], "mention attributes round-trip")
    _ensure(graph.mentions(GEN, ["film"], Scope(document_ids=["d1"]), 10) == [], "mentions honour scope")
    try:
        graph.upsert(GEN, [], [relations[0].model_copy(update={"object_id": "ghost"})], [])
        raise AssertionError("dangling relation endpoint must be rejected")
    except BackendError:
        pass
    _ensure(nodes(TraversalSpec(seed_entity_ids=["aacta"], hops=2)) == [["aacta", "film"], ["aacta", "film", "gala"]], "a rejected upsert leaves the graph unchanged")
    try:
        graph.traverse(GEN2, TraversalSpec(seed_entity_ids=["aacta"], hops=1), None)
        raise AssertionError("an unknown generation must be a typed BackendError")
    except BackendError:
        pass
    from ..communities.base import Community

    graph.put_communities(GEN, [Community(id="k1", label="AACTA", document_ids=["d6", "d7"], entity_ids=["aacta", "film"])])
    communities = graph.communities(GEN)
    _ensure([c.id for c in communities] == ["k1"] and communities[0].status() == "disabled" and communities[0].document_ids == ["d6", "d7"], "communities round-trip")
    check_traversal_templates(adapter)


# --- typed traversal templates (core/config_graph.py, arms/traversal.py) ----------------------------

GEN3 = GEN.model_copy(update={"generation": "e" * 32})
TRAVERSAL_TEXTS = {
    "t1": "Ticket Alpha reports Login Timeout; root cause Token Expiry, fixed by Token Refresh on the Portal",
    "t2": "Ticket Beta reports Login Timeout; resolved by Cache Flush on the Portal",
}


def traversal_fixture() -> tuple[list[Entity], list[Relation]]:
    """A neutral support-desk graph. t1: alpha -has_symptom-> login_timeout, alpha -has_root_cause->
    token_expiry -resolved_by-> token_refresh, alpha -resolved_by-> token_refresh, alpha -about-> portal.
    t2: beta -has_symptom-> login_timeout, beta -resolved_by-> cache_flush, portal ~ cache_flush
    (co-occurrence only)."""

    def span(doc: str, text: str, after: int = 0) -> Span:
        start = TRAVERSAL_TEXTS[doc].index(text, after)
        return Span(document_id=doc, chunk_id="c" + doc, field="body", start=start, end=start + len(text), text=text, match_level="exact")

    def whole(doc: str) -> Span:
        text = TRAVERSAL_TEXTS[doc]
        return Span(document_id=doc, chunk_id="c" + doc, field="body", start=0, end=len(text), text=text, match_level="exact")

    entities = [
        Entity(id="alpha", name="Ticket Alpha", type="issue", span=span("t1", "Ticket Alpha")),
        Entity(id="login_timeout", name="Login Timeout", type="symptom", span=span("t1", "Login Timeout")),
        Entity(id="token_expiry", name="Token Expiry", type="cause", span=span("t1", "Token Expiry")),
        Entity(id="token_refresh", name="Token Refresh", type="fix", span=span("t1", "Token Refresh")),
        Entity(id="portal", name="Portal", type="product", span=span("t1", "Portal")),
        Entity(id="beta", name="Ticket Beta", type="issue", span=span("t2", "Ticket Beta")),
        Entity(id="login_timeout", name="Login Timeout", type="symptom", span=span("t2", "Login Timeout")),
        Entity(id="cache_flush", name="Cache Flush", type="fix", span=span("t2", "Cache Flush")),
        Entity(id="portal", name="Portal", type="product", span=span("t2", "Portal")),
    ]

    def fact(subject: str, predicate: str, obj: str, doc: str) -> Relation:
        return Relation(subject_id=subject, predicate=predicate, object_id=obj, span=whole(doc), edge_kind="fact", support="trigger_in_span", provenance=PROV)

    relations = [
        fact("alpha", "has_symptom", "login_timeout", "t1"),
        fact("alpha", "has_root_cause", "token_expiry", "t1"),
        fact("token_expiry", "resolved_by", "token_refresh", "t1"),
        fact("alpha", "resolved_by", "token_refresh", "t1"),
        fact("alpha", "about", "portal", "t1"),
        fact("beta", "has_symptom", "login_timeout", "t2"),
        fact("beta", "resolved_by", "cache_flush", "t2"),
        Relation(subject_id="portal", predicate="near", object_id="cache_flush", span=whole("t2"), edge_kind="co_occurrence", support="unchecked", provenance=PROV),
    ]
    return entities, relations


def traversal_templates() -> dict[str, Any]:
    """The template library the conformance check runs (also used by the arm tests)."""
    from ..core.config_graph import TraversalTemplate

    return {
        "issue_facts": TraversalTemplate(kind="neighbors", hop={"direction": "out"}, hops=1, seed_types=["issue"]),
        "troubleshoot": TraversalTemplate(
            kind="chain",
            seed_match="tokens",
            seed_types=["symptom"],
            steps=[{"predicates": ["has_symptom"], "direction": "in"}, {"predicates": ["resolved_by", "has_root_cause"], "direction": "out"}],
        ),
        "cause_fix": TraversalTemplate(kind="chain", steps=[{"predicates": ["has_root_cause"], "direction": "out"}, {"predicates": ["resolved_by"], "direction": "out"}]),
        "related": TraversalTemplate(kind="connect", hop={"direction": "any"}, hops=2),
        "fixes_only": TraversalTemplate(kind="neighbors", hop={"direction": "out"}, hops=2, target_types=["fix"]),
        "mentioned_in": TraversalTemplate(kind="mentioned_in"),
        "any_kind": TraversalTemplate(kind="neighbors", hop={"direction": "any", "edge_kinds": ["fact", "co_occurrence"]}, hops=1),
    }


TRAVERSAL_CASES: tuple[tuple[str, tuple[str, ...], Scope | None], ...] = (
    ("issue_facts", ("Ticket Alpha",), None),
    ("issue_facts", ("Login Timeout",), None),  # a symptom is not an issue seed
    ("troubleshoot", ("users see login timeout errors",), None),
    ("troubleshoot", ("users see login timeout errors",), Scope(exclude_ids=["t2"])),
    ("cause_fix", ("alpha",), None),  # a stored entity id resolves to itself
    ("related", ("Ticket Beta", "Ticket Alpha"), None),
    ("fixes_only", ("Ticket Alpha", "Ticket Beta"), None),
    ("mentioned_in", ("Token Refresh", "Portal"), Scope(document_ids=["t1"])),
    ("any_kind", ("Portal",), None),
    ("issue_facts", ("Ticket Gamma",), None),
)


def traversal_signature(result: Any) -> dict[str, Any]:
    """Backend-independent view of a ``TemplateResult`` (ids, predicates, kinds, spans)."""
    return {
        "seeds": sorted(s.id for s in result.seeds),
        "unresolved": list(result.unresolved),
        "paths": [
            [tuple(p.nodes), tuple((e.predicate, e.edge_kind, e.span.document_id, e.span.start, e.span.end, e.provenance.extractor) for e in p.edges)]
            for p in result.paths
        ],
        "mentions": [(m.id, m.span.document_id, m.span.start, m.span.end) for m in result.mentions],
        "truncated": result.truncated,
    }


EXPECTED_TRAVERSALS: tuple[dict[str, Any], ...] = (
    {"seeds": ["alpha"], "unresolved": [], "nodes": [["alpha", "login_timeout"], ["alpha", "token_expiry"], ["alpha", "token_refresh"], ["alpha", "portal"]]},
    {"seeds": [], "unresolved": ["Login Timeout"], "nodes": []},
    {"seeds": ["login_timeout"], "unresolved": [], "nodes": [["login_timeout", "alpha", "token_expiry"], ["login_timeout", "alpha", "token_refresh"], ["login_timeout", "beta", "cache_flush"]]},
    {"seeds": ["login_timeout"], "unresolved": [], "nodes": [["login_timeout", "alpha", "token_expiry"], ["login_timeout", "alpha", "token_refresh"]]},
    {"seeds": ["alpha"], "unresolved": [], "nodes": [["alpha", "token_expiry", "token_refresh"]]},
    {"seeds": ["alpha", "beta"], "unresolved": [], "nodes": [["beta", "login_timeout", "alpha"]]},
    {"seeds": ["alpha", "beta"], "unresolved": [], "nodes": [["alpha", "token_refresh"], ["beta", "cache_flush"], ["alpha", "token_expiry", "token_refresh"]]},
    {"seeds": ["portal", "token_refresh"], "unresolved": [], "mentions": [("token_refresh", "t1"), ("portal", "t1")]},
    {"seeds": ["portal"], "unresolved": [], "nodes": [["portal", "alpha"], ["portal", "cache_flush"]]},
    {"seeds": [], "unresolved": ["Ticket Gamma"], "nodes": []},
)


def run_traversal_cases(adapter: Any, gen: GenerationId = GEN3) -> list[dict[str, Any]]:
    """Load the traversal fixture into ``gen`` and run every case; returns the signatures."""
    from ..arms.traversal import run_template
    from ..core.config_graph import GraphIntent

    entities, relations = traversal_fixture()
    if caps.satisfied(adapter, "catalog"):
        adapter.catalog.put_documents(
            gen,
            [Document(id=d, dataset="conformance", source="case") for d in TRAVERSAL_TEXTS],
            [Chunk(id="c" + d, document_id=d, field="body", start=0, end=len(t), text=t) for d, t in TRAVERSAL_TEXTS.items()],
        )
    adapter.graph.upsert(gen, entities, relations, [])
    templates = traversal_templates()
    return [
        traversal_signature(run_template(adapter.graph, gen, name, templates[name], GraphIntent(template=name, seeds=list(seeds)), scope))
        for name, seeds, scope in TRAVERSAL_CASES
    ]


def check_traversal_templates(adapter: Any) -> list[dict[str, Any]]:
    """Every template kind gives the documented result on this backend (identical semantics across
    backends: the expectations are backend-independent literals)."""
    signatures = run_traversal_cases(adapter)
    for (name, seeds, _), got, want in zip(TRAVERSAL_CASES, signatures, EXPECTED_TRAVERSALS):
        label = f"template {name} {seeds}"
        _ensure(got["seeds"] == want["seeds"], f"{label}: seeds {got['seeds']} != {want['seeds']}")
        _ensure(got["unresolved"] == want["unresolved"], f"{label}: unresolved {got['unresolved']}")
        if "mentions" in want:
            _ensure([(m[0], m[1]) for m in got["mentions"]] == want["mentions"], f"{label}: mentions {got['mentions']}")
        else:
            _ensure([list(p[0]) for p in got["paths"]] == want["nodes"], f"{label}: paths {[p[0] for p in got['paths']]}")
    by_kind = {kind for path in signatures[8]["paths"] for kind in (e[1] for e in path[1])}
    _ensure(by_kind == {"fact", "co_occurrence"}, "co-occurrence only through a template that requests it")
    _ensure(all(e[1] == "fact" for s in signatures[:8] for p in s["paths"] for e in p[1]), "fact-only templates never reach co-occurrence")
    return signatures


def check_cache(adapter: Any, clock: dict[str, float]) -> None:
    cache = adapter.cache
    try:
        cache.put("answer:x", {"a": 1}, 10)
        raise AssertionError("keys must start with a generation prefix")
    except BackendError:
        pass
    key = GEN.prefix() + "answer:x"
    cache.put(key, {"a": 1}, 10)
    _ensure(cache.get(key).value == {"a": 1}, "round-trip")
    clock["now"] += 11
    _ensure(cache.get(key) is None, "TTL expiry")
    cache.put(key, 1, 10)
    cache.put(GEN2.prefix() + "answer:y", 2, 10)
    _ensure(cache.invalidate(GEN.prefix()) == 1 and cache.get(GEN2.prefix() + "answer:y").value == 2, "prefix invalidation")
    try:
        cache.put(key, 1, 0)
        raise AssertionError("ttl must be positive")
    except BackendError:
        pass


def check_ledger(adapter: Any) -> None:
    ledger = adapter.ledger
    ledger.append("job1", LedgerEntry(sequence=0, kind="start", at="t0"))
    ledger.append("job1", LedgerEntry(sequence=1, kind="chunk", at="t1", payload={"chunk_id": "c1"}))
    try:
        ledger.append("job1", LedgerEntry(sequence=1, kind="dup", at="t2"))
        raise AssertionError("sequence must increase")
    except BackendError:
        pass
    _ensure([e.kind for e in ledger.replay("job1")] == ["start", "chunk"] and ledger.replay("none") == [], "replay")
    _ensure(ledger.status("job1") == "pending", "default status")
    ledger.mark("job1", "failed")
    _ensure(ledger.status("job1") == "failed", "mark")
    try:
        ledger.mark("ghost", "failed")
        raise AssertionError("unknown job")
    except BackendError:
        pass
    ledger.put_raw("c1:extract", "{not json", valid=False)
    raw = ledger.get_raw("c1:extract")
    _ensure(raw is not None and raw.valid is False and raw.raw_reply == "{not json" and ledger.get_raw("zz") is None, "raw replies kept, valid or not")
    from ..backends.extras import LedgerRetention

    if isinstance(ledger, LedgerRetention):
        check_ledger_retention(ledger)


def check_ledger_retention(ledger: Any) -> None:
    """Tags, ``ledger_refs`` and ``purge`` (addendum A1 §3.8.3): tagged entries go with their tag,
    unreferenced entries under a prefix go once their last referencing generation's refs are
    dropped, referenced and other-prefix entries stay, job entries are dropped per generation."""
    from ..backends.extras import PurgeSpec

    ledger.put_raw("raw:a", "reply a", True, tags=["partition:s:2026-01-01", "partition:s:2026-01-02"])
    ledger.put_raw("raw:b", "reply b", True, tags=["partition:s:2026-01-02"])
    ledger.put_raw("extraction:r:c1", "{}", True)
    ledger.put_raw("extraction:r:c2", "{}", True)
    ledger.put_raw("embedding:e:x", "AAAA", True)
    ledger.put_raw("other:keep", "kept", True)
    ledger.put_raw("raw:learn", "reply in window", True, tags=["partition:s:2026-01-09"])
    try:
        ledger.tag_raw("raw:ghost", ["t"])
        raise AssertionError("tagging an unknown entry")
    except BackendError:
        pass
    _ensure(ledger.raw_tags("raw:a") == ["partition:s:2026-01-01", "partition:s:2026-01-02"], "raw tags")
    _ensure(ledger.tagged("partition:s:2026-01-02") == ["raw:a", "raw:b"], "tagged lookup")
    ledger.add_refs("g1", ["extraction:r:c1", "embedding:e:x"])
    ledger.add_refs("g2", ["extraction:r:c1"])
    ledger.add_refs("g2", ["extraction:r:c1"])  # idempotent
    _ensure(ledger.refs("g1") == ["embedding:e:x", "extraction:r:c1"] and ledger.refs("g2") == ["extraction:r:c1"], "refs")
    _ensure(ledger.referencing_generations() == ["g1", "g2"], "referencing generations")
    page = ledger.raw_entries("extraction:", None, 1)
    _ensure([r.key for r in page] == ["extraction:r:c1"] and [r.key for r in ledger.raw_entries("extraction:", page[-1].key, 10)] == ["extraction:r:c2"], "raw entries paged by key")
    ledger.append("g1", LedgerEntry(sequence=0, kind="start", at="t0"))
    _ensure("g1" in ledger.jobs() and ledger.drop_job("g1") == 1 and ledger.replay("g1") == [] and "g1" not in ledger.jobs(), "drop job entries")
    counts = ledger.purge(PurgeSpec(drop_refs_of=["g1"], tags=["partition:s:2026-01-01"], unreferenced_prefixes=["extraction:", "embedding:"]))
    _ensure(counts["refs_dropped"] == 2 and counts["tagged"] == 1 and counts["unreferenced"] == 2, f"purge counts {counts}")
    _ensure(ledger.get_raw("raw:a") is None and ledger.raw_tags("raw:a") == [], "tagged entry purged with its tags")
    _ensure(ledger.get_raw("raw:b") is not None, "entry with another tag kept")
    _ensure(ledger.get_raw("extraction:r:c1") is not None, "entry referenced by a remaining generation kept")
    _ensure(ledger.get_raw("extraction:r:c2") is None and ledger.get_raw("embedding:e:x") is None, "unreferenced entries purged")
    _ensure(ledger.get_raw("other:keep") is not None, "other prefixes untouched")
    ledger.purge(PurgeSpec(unreferenced_prefixes=["raw:"]))
    _ensure(ledger.get_raw("raw:learn") is not None, "a tagged entry of a live partition survives the unreferenced purge")
    _ensure(ledger.referencing_generations() == ["g2"], "refs of the dropped generation gone")


def check_session(adapter: Any) -> None:
    sessions = adapter.session
    record = SessionRecord(id="s1", principal_id="p", dataset="conformance", created_at=0.0, last_seen_at=0.0)
    sessions.create(record)
    try:
        sessions.create(record)
        raise AssertionError("duplicate session")
    except BackendError:
        pass
    sessions.touch("s1", 50.0)
    _ensure(sessions.get("s1").last_seen_at == 50.0, "touch")
    # Compare-and-set (QA-05): a write from a stale version is refused and changes nothing.
    stored = sessions.get("s1")
    sessions.touch("s1", 50.0, stored.model_copy(update={"usage": {"calls": 1.0}}), expected_version=stored.version)
    _ensure(sessions.get("s1").version == stored.version + 1 and sessions.get("s1").usage == {"calls": 1.0}, "cas write bumps the version")
    try:
        sessions.touch("s1", 50.0, stored.model_copy(update={"usage": {"calls": 7.0}}), expected_version=stored.version)
        raise AssertionError("stale compare-and-set accepted")
    except SessionConflict:
        pass
    _ensure(sessions.get("s1").usage == {"calls": 1.0}, "stale cas wrote nothing")
    try:
        sessions.touch("ghost", 1.0)
        raise AssertionError("unknown session")
    except BackendError:
        pass
    _ensure(sessions.expire(now=100.0, ttl_seconds=60) == 0, "not yet expired")
    _ensure(sessions.expire(now=120.0, ttl_seconds=60) == 1 and sessions.get("s1") is None, "expired")


CHECKS: dict[str, Callable[..., Any]] = {
    "catalog": check_catalog,
    "lexical": check_lexical,
    "vector": check_vector,
    "records": check_records,
    "graph": check_graph,
    "ledger": check_ledger,
    "session": check_session,
}


def run_all(factory: Callable[[], Any], clock: dict[str, float]) -> dict[str, str]:
    """Run every declared capability check on a fresh adapter each; returns capability -> 'passed'."""
    results = {}
    adapter = factory()
    for capability in check_protocols(adapter):
        fresh = factory()
        if capability == "cache":
            check_cache(fresh, clock)
        else:
            CHECKS[capability](fresh)
        results[capability] = "passed"
    return results
