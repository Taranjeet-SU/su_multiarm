"""Staging round-2 latency fixes of the unified collection (2026-11-10; synthetic regressions of the
private real-volume measurements, docs/plans/unified_merge.md rev 3.3 §6.7, §6.8).

* N3: ``issue_history`` / ``log_liveness`` over the live log member read a **per-pin history file**
  (``runtime/member_logs.history_file``) derived from the pin's SQL-surface file instead of building
  the typed tables per request from the window's catalog records. Asserted here: the file's
  aggregates equal the per-request reference construction (the previous algorithm, kept below as
  the oracle) for windows, filters and listings; a second request on the same pin reads no catalog
  record and builds nothing; a new slice recomputes only its own day (and the days after it) and
  derives only its own unit cache; a 10-day window over a synthetic scaled member is answered.
* N4: ``PartitionedRecords.values`` / ``exists`` answers are cached per immutable unit: a repeated
  request (planner values, qualifier checks, typed route) scans no unit again; a new slice is the
  only unit scanned.

Behaviour is asserted on scan/build counters, never on wall time. Synthetic only (F0 fixtures and
generated episode records, hash embedder, rules decision, no provider call, no service).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import tests.live_log_support as L
import tests.staging_support as S
from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document, Page, Predicate, Scope
from su_multi_arm.ingestion.episode_history import (
    DayTier,
    HistorySpec,
    TieredAggregate,
    coverage_of,
    ensure_schema,
    insert_rows,
    issue_rows,
    link_continuations,
    midnight,
    piece_of,
)
from su_multi_arm.ingestion.episode_summaries import timestamp
from su_multi_arm.runtime.member_logs import MemberHistory, served_days

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)
GAP = 300


@pytest.fixture(scope="module")
def clock():
    mp = pytest.MonkeyPatch()
    mp.setattr(Engine, "_clock", staticmethod(lambda zone: NOW.astimezone(zone)))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def fx():
    return L.fixture()


@pytest.fixture(scope="module")
def unified(tmp_path_factory, fx, clock):
    return S.collection(tmp_path_factory.mktemp("latency_unified"), fx)


def tool(engine: Engine, name: str, args: dict) -> dict:
    return engine.call_tool(name, args).result.output.model_dump(mode="json")


class Ctx:
    def __init__(self, gen: Any):
        self.generation = gen

    def constraints(self) -> list:
        return []


# --- the previous per-request construction (the oracle) -------------------------------------------------


class _MemoryAggregate(TieredAggregate):
    def __init__(self, db: sqlite3.Connection, tiers: dict[str, DayTier]):
        super().__init__(Path("reference"), tiers)
        self._db = db

    def _connect(self, path):  # type: ignore[override]
        class Keep:
            def __init__(self, db):
                self.db = db

            def execute(self, *a):
                return self.db.execute(*a)

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

        return Keep(self._db)

    def check_built(self) -> None:
        return None


def reference(engine: Engine, gen: Any, first: str, last: str) -> _MemoryAggregate:
    """The typed tables of ``[first, last]`` (plus the day before, for the cross-midnight rule) built
    from the pinned catalog records, as ``MemberHistory`` built them per request before N3."""
    backend = engine.backends[engine.config.storage.catalog]
    days = served_days(backend.view(gen), "logs")
    tiers: dict[str, DayTier] = {}
    day = date.fromisoformat(first)
    while day.isoformat() <= last:
        entry = days.get(day.isoformat())
        tiers[day.isoformat()] = DayTier("missing") if entry is None else DayTier("archive", entry["closure"], entry["unit"]) if entry["tier"] == "archive" else DayTier("provisional", None, "provisional")
        day += timedelta(days=1)
    wanted = sorted(d for d, t in tiers.items() if t.tier != "missing")
    if wanted and (date.fromisoformat(wanted[0]) - timedelta(days=1)).isoformat() in days:
        wanted = [(date.fromisoformat(wanted[0]) - timedelta(days=1)).isoformat(), *wanted]
    scope = Scope(predicates=[Predicate(field="source", op="eq", value="logs"), Predicate(field="kind", op="in", value=["incident", "summary_coverage"])])
    by_day: dict[str, dict[str, list[Any]]] = {d: {"incident": [], "summary_coverage": []} for d in wanted}
    cursor = None
    while True:
        page = backend.catalog.iter_scope(gen, scope, Page(cursor=cursor, size=1000))
        for doc in page.items:
            d = str(doc.metadata.get("date") or "")
            if d in by_day:
                by_day[d][doc.kind].append(doc)
        cursor = page.next_cursor
        if cursor is None or not page.items:
            break
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    previous = previous_day = None
    for d in wanted:
        entry = days[d]
        provisional = entry["tier"] == "provisional"
        source = "provisional" if provisional else entry["unit"]
        pieces = [piece_of(d, source, x.metadata, x.id) for x in sorted(by_day[d]["incident"], key=lambda x: x.id)]
        coverage = [coverage_of(d, source, x.metadata, x.id) for x in sorted(by_day[d]["summary_coverage"], key=lambda x: x.id)]
        as_of = max((str(c["observed_at"]) for c in coverage if c.get("observed_at")), default=None) if provisional else None
        for row in [*pieces, *coverage]:
            row.update(partial=int(entry.get("closure") == "partial"), provisional=int(provisional), as_of=as_of)
        adjacent = previous_day is not None and date.fromisoformat(d) - date.fromisoformat(previous_day) == timedelta(days=1)
        tail = None
        if adjacent:
            cutoff = midnight(previous_day) + timedelta(days=1) - timedelta(seconds=GAP + 60)
            tail = [p for p in previous if timestamp(p["last_failure"]) >= cutoff]
        link_continuations(d, pieces, tail, GAP)
        insert_rows(db, pieces, issue_rows(d, source, pieces), coverage)
        if provisional and d in tiers:
            tiers[d] = DayTier("provisional", None, "provisional", as_of=as_of)
        previous, previous_day = pieces, d
    return _MemoryAggregate(db, tiers)


def via_file(engine: Engine, gen: Any, first: str, last: str) -> tuple[MemberHistory, Any]:
    member = MemberHistory(engine, Ctx(gen))
    tiers = member.tiers(first, last)
    return member, member.aggregate(tiers, GAP)


def normal(result: Any) -> tuple:
    return result.groups, result.pieces, result.boundary_unknown, result.excluded_occurrences


SPECS = [
    {"group_by": ["signature_id"], "list_k": 20},
    {"group_by": ["tenant", "service"], "list_k": 5, "exclude": {"category": ["success"]}},
    {"group_by": ["day"], "filters": {"tenant": ["alderbank-prod"]}},
    {"group_by": ["signature_id"], "filters": {"service": ["crawler", "search"]}, "list_k": 3},
    {"group_by": [], "filters": {"category": ["crawl"]}},
]


@pytest.mark.parametrize("window", [("2026-11-03", "2026-11-09"), ("2026-11-05", "2026-11-08"), ("2026-11-07", "2026-11-07"), ("2026-11-08", "2026-11-09")])
def test_history_file_equals_the_per_request_construction(unified, window):
    gen, _ = unified.write_path.pin()
    first, last = window
    oracle = reference(unified, gen, first, last)
    member, built = via_file(unified, gen, first, last)
    with member:
        assert {d: (t.tier, t.closure, t.as_of) for d, t in built.tiers.items()} == {d: (t.tier, t.closure, t.as_of) for d, t in oracle.tiers.items()}
        for raw in SPECS:
            spec = HistorySpec(first=first, last=last, **raw)
            expected, got = oracle.execute(spec), built.execute(spec)
            assert normal(got) == normal(expected), raw
            assert built.horizons(spec) == oracle.horizons(spec)
            assert built.pieces(spec) == oracle.pieces(spec)


def test_a_second_request_on_the_same_pin_reads_no_record_and_builds_nothing(unified, monkeypatch):
    stats = unified.backends["local"].stats
    first = tool(unified, "issue_history", {"window": "last 7 days", "list_episodes": 5})
    assert first["status"] == "bounded", first.get("refusal")
    built, reused = stats["collection.history_files_built"], stats["collection.history_files_reused"]
    from su_multi_arm.backends.sqlite import log_units

    reads = []
    original = log_units.UnitCatalog.iter_scope
    monkeypatch.setattr(log_units.UnitCatalog, "iter_scope", lambda self, *a, **k: reads.append(1) or original(self, *a, **k))
    again = tool(unified, "issue_history", {"window": "last 7 days", "list_episodes": 5})
    tenant = tool(unified, "issue_history", {"window": "last 7 days", "tenant": "alderbank-prod"})
    assert again["rows"] == first["rows"] and again["history"] == first["history"]
    assert tenant["status"] == "bounded" and all(r["occurrences"] for r in tenant["rows"])
    assert reads == []  # no catalog record is read: the pin's history file answers
    assert stats["collection.history_files_built"] == built and stats["collection.history_files_reused"] >= reused + 2


def test_the_history_file_lives_next_to_the_view_file_and_is_pruned_like_it(unified):
    from su_multi_arm.runtime import member_logs
    from su_multi_arm.sql_surface import members as surface

    tool(unified, "issue_history", {"window": "last 3 days"})
    folder = Path(unified.backends["local"].registry.root) / "sql"
    histories = [f for f in folder.glob("v*.sqlite") if member_logs._is_history(f)]
    views = [f for f in folder.glob("v*.sqlite") if surface._is_view(f)]
    assert histories and views and not set(histories) & set(views)
    assert len(histories) <= surface.MAX_VIEW_FILES


# --- N3 at (synthetic) scale: 10 days, archive + live, a new slice -------------------------------------

DAYS = [(date(2026, 10, 31) + timedelta(days=i)).isoformat() for i in range(10)]  # 10-31 .. 11-09
ARCHIVED, LIVE_DAYS = DAYS[:7], DAYS[7:]
TENANTS = ["alderbank-prod", "brightmoor", "cobalt", "deltaware", "ember"]
SERVICES = ["crawler", "search", "auth-service", "report-worker"]
RULES = [("crawler.fetch_timeout", "crawl"), ("auth.unauthorized", "auth"), ("search.slow_query", "search"), ("report.queue_stalled", "jobs")]
PER_DAY = 400  # incident pieces per day (synthetic scale; the counters, not the wall time, are asserted)


def _incident(template: Document, day: str, i: int, *, occurrences: int | None = None) -> Document:
    tenant, service = TENANTS[i % len(TENANTS)], SERVICES[(i // len(TENANTS)) % len(SERVICES)]
    rule, category = RULES[i % len(RULES)]
    start = midnight(day) + timedelta(minutes=(i * 11) % 1430)
    if i % 40 == 0:  # heads: start just after midnight (key tenant 0 / service 0 / rule 0)
        start = midnight(day) + timedelta(seconds=60)
    end = start + timedelta(minutes=3)
    if i % 40 == 20:  # tails of the same key: end just before midnight (the next day's head continues one)
        start, end = midnight(day) + timedelta(hours=23, minutes=50), midnight(day) + timedelta(hours=23, minutes=58)
    metadata = {**template.metadata, "tenant": tenant, "service": service, "services": service, "signature_id": rule, "signatures": rule, "category": category,
                "issue": f"Synthetic {rule}", "date": day, "first_hour": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "last_hour": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "occurrences": occurrences if occurrences is not None else 1 + i % 7, "lines": 1 + i % 7, "evidence_count": 1}
    return template.model_copy(update={"id": f"logs:incident:scale-{day}-{i:04d}", "metadata": metadata, "title": f"Synthetic {rule}"})


def _coverage(template: Document, day: str, j: int) -> Document:
    tenant, service = TENANTS[j % len(TENANTS)], SERVICES[j // len(TENANTS)]
    metadata = {**template.metadata, "tenant": tenant, "service": service, "date": day, "window_start": f"{day}T00:00:00Z", "window_end": f"{day}T23:59:59Z", "observed_at": f"{day}T23:59:59Z"}
    return template.model_copy(update={"id": f"logs:summary_coverage:scale-{day}-{j:03d}", "metadata": metadata})


def _day_documents(fx: Any, day: str) -> list[Document]:
    docs = fx.members["logs/day:2026-11-04"]
    incident = next(d for d in docs if d.kind == "incident")
    coverage = next(d for d in docs if d.kind == "summary_coverage")
    return [*(_incident(incident, day, i) for i in range(PER_DAY)), *(_coverage(coverage, day, j) for j in range(len(TENANTS) * len(SERVICES)))]


@pytest.fixture(scope="module")
def scaled(tmp_path_factory, fx, clock):
    root = tmp_path_factory.mktemp("latency_scaled")
    original = S.build_support_store(root, fx)
    engine = Engine(validate_config(S.unified_data(root, fx, original, logs=True)))
    registry = engine.view.attach()
    registry.adopt("support")
    engine.compose()
    lives = registry.live
    ledger = lives.ledger("logs")
    for day_s in DAYS:
        day = date.fromisoformat(day_s)
        ledger.seal(day, "five_minute", _day_documents(fx, day_s), as_of=L.at(day, 23, 59))
        if day_s in ARCHIVED:
            lives.close_day("logs", day, as_of=L.at(day, 23, 59))
    return engine, ledger, fx


def _expected(docs: list[Document]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in docs:
        if d.kind == "incident":
            out[d.metadata["signature_id"]] = out.get(d.metadata["signature_id"], 0) + int(d.metadata["occurrences"])
    return out


def test_ten_days_are_answered_from_the_history_file_and_a_new_slice_recomputes_one_day(scaled):
    engine, ledger, fx = scaled
    stats = engine.backends["local"].stats
    folder = Path(engine.backends["local"].registry.root) / "sql"
    out = tool(engine, "issue_history", {"start": DAYS[0], "end": DAYS[-1], "group_by": ["signature_id"]})
    assert out["status"] == "bounded", out.get("refusal")  # before N3: built per request (refused above 200k records at real volume)
    every = [d for day in DAYS for d in _day_documents(fx, day)]
    assert {r["signature_id"]: r["occurrences"] for r in out["rows"]} == _expected(every)
    coverage = out["history"]["coverage"]
    assert coverage["archive_days"] == len(ARCHIVED) and sorted(coverage["provisional_days"]) == LIVE_DAYS
    # the cross-midnight rule holds on every boundary, the archive/live one included: one head per
    # key and day continues the previous day's tail (``link_continuations``), so 9 chains
    assert sum(r["episodes"] for r in out["rows"]) == PER_DAY * len(DAYS) - (len(DAYS) - 1)
    assert stats["collection.history_days_built"] == len(DAYS) and stats["collection.history_files_built"] == 1
    assert stats["collection.history_days_reused"] == 0

    # a new live slice on the last day: an updated record (same id, new count) and one new record
    template = fx.members["logs/day:2026-11-04"]
    incident = next(d for d in template if d.kind == "incident")
    last = date.fromisoformat(DAYS[-1])
    update = [_incident(incident, DAYS[-1], 5, occurrences=1000), _incident(incident, DAYS[-1], PER_DAY)]
    before_files = set(folder.glob("v*.sqlite"))
    from su_multi_arm.runtime import member_logs
    from su_multi_arm.sql_surface import members as surface

    assert ledger.seal(last, "five_minute", update, as_of=L.at(last, 23, 59)) is not None
    out2 = tool(engine, "issue_history", {"start": DAYS[0], "end": DAYS[-1], "group_by": ["signature_id"]})
    changed = [d for d in every if d.id != update[0].id] + update
    assert {r["signature_id"]: r["occurrences"] for r in out2["rows"]} == _expected(changed)
    assert stats["collection.history_files_built"] == 2
    assert stats["collection.history_days_built"] == len(DAYS) + 1  # only the slice's day is recomputed
    assert stats["collection.history_days_reused"] == len(DAYS) - 1
    new_files = set(folder.glob("v*.sqlite")) - before_files
    # the new slice's unit cache (no other unit is derived again), the pin's view file and history file
    assert len(new_files) == 3
    assert sorted((surface._is_view(f), member_logs._is_history(f)) for f in new_files) == [(False, False), (False, True), (True, False)]


def test_a_filtered_ten_day_window_pushes_the_filter_down(scaled):
    engine, _ledger, fx = scaled
    out = tool(engine, "issue_history", {"start": DAYS[0], "end": DAYS[-1], "tenant": "cobalt", "service": ["crawler"], "group_by": ["signature_id"]})
    assert out["status"] == "bounded"
    assert all(r["occurrences"] for r in out["rows"]) and out["rows"]


# --- N4: values / exists cached per unit --------------------------------------------------------------


def test_values_are_scanned_once_per_unit(unified):
    gen, _ = unified.write_path.pin()
    records = unified.backends["local"].records
    stats = unified.backends["local"].stats
    first = records.values(gen, "tenant", [], 50)
    scans, hits = stats["collection.values_unit_scans"], stats["collection.values_cache_hits"]
    assert records.values(gen, "tenant", [], 50) == first
    assert records.values(gen, "tenant", [], 2) == first[:2]  # a smaller limit is served from the cached list
    assert stats["collection.values_unit_scans"] == scans and stats["collection.values_cache_hits"] > hits
    acl = [Predicate(field="tenant", op="eq", value="alderbank-prod")]
    assert records.values(gen, "tenant", acl, 50) == ["alderbank-prod"]  # another ACL is another key
    assert stats["collection.values_unit_scans"] > scans


def test_a_truncated_cached_list_does_not_answer_a_larger_limit(unified):
    gen, _ = unified.write_path.pin()
    records = unified.backends["local"].records
    stats = unified.backends["local"].stats
    small = records.values(gen, "service", [], 1)
    scans = stats["collection.values_unit_scans"]
    full = records.values(gen, "service", [], 500)
    assert len(small) == 1 and len(full) > 1 and full[0] == small[0]
    assert stats["collection.values_unit_scans"] > scans  # re-read: the 1-value lists were truncated


def test_exists_is_cached_per_unit(unified):
    gen, _ = unified.write_path.pin()
    records = unified.backends["local"].records
    stats = unified.backends["local"].stats
    assert records.exists(gen, "tenant", "alderbank-prod", [])
    reads = stats["collection.exists_unit_reads"]
    assert records.exists(gen, "tenant", "alderbank-prod", [])
    assert not records.exists(gen, "tenant", "no-such-tenant", [])
    reads2 = stats["collection.exists_unit_reads"]
    assert not records.exists(gen, "tenant", "no-such-tenant", [])
    assert stats["collection.exists_unit_reads"] == reads2 and reads2 > reads


def test_a_repeated_search_scans_no_unit_values_and_returns_the_same(unified):
    stats = unified.backends["local"].stats
    request = SearchRequest(query="crawler fetch timeout for the tenant")
    first = unified.search(request)
    scans = stats["collection.values_unit_scans"]
    second = unified.search(request)
    assert stats["collection.values_unit_scans"] == scans
    assert L.hits(second) == L.hits(first)


def test_a_new_slice_is_the_only_unit_scanned_for_values(scaled):
    engine, ledger, fx = scaled
    stats = engine.backends["local"].stats
    gen, _ = engine.write_path.pin()
    records = engine.backends["local"].records
    records.values(gen, "tenant", [], 100)
    scans = stats["collection.values_unit_scans"]
    template = next(d for d in fx.members["logs/day:2026-11-04"] if d.kind == "incident")
    last = date.fromisoformat(DAYS[-1])
    assert ledger.seal(last, "five_minute", [_incident(template, DAYS[-1], PER_DAY + 7)], as_of=L.at(last, 23, 59)) is not None
    gen2, _ = engine.write_path.pin()
    assert gen2.generation != gen.generation
    assert records.values(gen2, "tenant", [], 100) == records.values(gen, "tenant", [], 100)
    assert stats["collection.values_unit_scans"] == scans + 1


def test_a_restarted_process_reads_the_unscoped_value_lists_from_the_unit_values_file(unified):
    gen, _ = unified.write_path.pin()
    expected = unified.backends["local"].records.values(gen, "service", [], 200)
    fresh = Engine(unified.config)  # a new process over the same collection
    gen2, _ = fresh.write_path.pin()
    stats = fresh.backends["local"].stats
    assert fresh.backends["local"].records.values(gen2, "service", [], 200) == expected
    assert stats["collection.values_unit_scans"] == 0 and stats["collection.values_file_hits"] > 0
    # a scoped (ACL) list is never persisted: the new process scans for it
    acl = [Predicate(field="source", op="eq", value="logs")]
    fresh.backends["local"].records.values(gen2, "service", acl, 200)
    assert stats["collection.values_unit_scans"] > 0


def test_an_unreadable_unit_values_file_falls_back_to_the_scan(unified, tmp_path):
    from su_multi_arm.backends.sqlite.members import UnitValuesFile

    gen, _ = unified.write_path.pin()
    expected = unified.backends["local"].records.values(gen, "env", [], 50)
    fresh = Engine(unified.config)
    records = fresh.backends["local"].records
    broken = tmp_path / "sql" / UnitValuesFile.NAME
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a sqlite file" * 64)
    records._file = UnitValuesFile(broken, fresh.backends["local"].stats)
    gen2, _ = fresh.write_path.pin()
    assert records.values(gen2, "env", [], 50) == expected  # the cache failing never changes the answer
    stats = fresh.backends["local"].stats
    assert stats["collection.values_unit_scans"] > 0
    assert stats["collection.values_file_unreadable"] + stats["collection.values_file_unwritable"] > 0


def test_the_unit_values_file_keeps_only_pinned_units(unified):
    from su_multi_arm.backends.sqlite.members import UnitValuesFile

    gen, _ = unified.write_path.pin()
    backend = unified.backends["local"]
    backend.records.values(gen, "service", [], 200)
    path = Path(backend.registry.root) / "sql" / UnitValuesFile.NAME
    with sqlite3.connect(path) as db:
        db.execute("INSERT OR REPLACE INTO unit_values VALUES ('retired-unit', 'service|x', 10, '[]')")
    keys = {u.key for u in backend.units(gen)}
    fresh = Engine(unified.config)
    gen2, _ = fresh.write_path.pin()
    fresh.backends["local"].records.values(gen2, "service", [], 200)
    with sqlite3.connect(path) as db:
        held = {r[0] for r in db.execute("SELECT unit FROM unit_values")}
    assert "retired-unit" not in held and held <= keys


def test_the_identity_view_is_parsed_once_per_file_state(tmp_path, fx, monkeypatch):
    from types import SimpleNamespace

    from su_multi_arm.crosssource import identity as I
    from su_multi_arm.sql_surface import members as surface

    registry = tmp_path / "ids.sqlite"
    I.import_registry(registry, fx.registry, now=lambda: NOW)
    config = SimpleNamespace(crosssource=SimpleNamespace(identity=SimpleNamespace(registry_path=str(registry), sha256=I.sha256_file(registry))))
    parsed = []
    original = I.IdentityView.pinned.__func__
    monkeypatch.setattr(I.IdentityView, "pinned", classmethod(lambda cls, path, sha, **kw: parsed.append(1) or original(cls, path, sha, **kw)))
    first = surface._identity_view(config)
    assert surface._identity_view(config) is first and len(parsed) == 1 and first.joins_enabled
    registry.write_bytes(registry.read_bytes() + b"\0")  # the file changes: read and digest-checked again
    changed = surface._identity_view(config)
    assert len(parsed) == 2 and not changed.joins_enabled and changed.reason == "digest_mismatch"
