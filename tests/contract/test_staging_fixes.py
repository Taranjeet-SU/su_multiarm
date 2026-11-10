"""Staging fixes of the unified collection (2026-11-10; synthetic regressions of the private staging
repros, docs/plans/unified_merge.md rev 3.3 §6.7, §6.11).

* B1: the v2 ``logs`` block is served over a partitioned collection — log intents (incident
  episodes, ``issue_history``, ``log_top``/``log_trend``, ``log_liveness``) read the live log
  member's archive day members and the live slices of the request pin; ``config validate`` accepts
  logs + projection together; refusals stay for what is not served.
* B2: ``PartitionedRecords.values`` over an adopted member whose own schema lacks a log-only field
  contributes nothing instead of raising (every llm-planned request failed); records of a scope on a
  log-only field are not flagged unsupported by that member.
* B3: a text condition over ≥ 2 lexical strata pages the per-unit ranking (``k`` above one page).
* F3: the graph arm over an attached layer with a ``source: logs`` filter degrades (no graph
  evidence, a trace reason) instead of failing the request.

Synthetic only: F0 fixtures, hash embedder, rules decision, no provider call, no service.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone

import pytest

import tests.live_log_support as L
import tests.staging_support as S
from su_multi_arm import Engine, SearchRequest
from su_multi_arm.arms.structured import text_match_ids
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.core.types import LexicalQuery, Predicate, Scope
from su_multi_arm.runtime.collection import partitioned_refusals

NOW = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)


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
    return S.collection(tmp_path_factory.mktemp("staging_unified"), fx)


def tool(engine: Engine, name: str, args: dict) -> dict:
    return engine.call_tool(name, args).result.output.model_dump(mode="json")


# --- B1: logs over the partitioned log member ---------------------------------------------------------


def test_logs_and_projection_validate_and_construct_over_the_partitioned_collection(tmp_path, fx):
    original = S.build_support_store(tmp_path, fx)
    config = validate_config(S.unified_data(tmp_path, fx, original, logs=True))
    assert config.logs is not None and config.projection.enabled
    assert partitioned_refusals(config) == []
    Engine(config)  # constructs (before: "the v2 logs block ... is not served over a partitioned collection")


def test_what_stays_refused_for_the_logs_block(tmp_path, fx):
    original = S.build_support_store(tmp_path, fx)
    no_member = S.unified_data(tmp_path, fx, original, logs=False)
    S.add_logs(tmp_path, no_member)
    assert [p for p, _ in partitioned_refusals(validate_config(no_member))] == ["logs"]
    tier = S.unified_data(tmp_path, fx, original, logs=True)
    tier["logs"]["history"] = {"tier": True}
    assert ("logs.history.tier" in [p for p, _ in partitioned_refusals(validate_config(tier))])
    with pytest.raises(ConfigError, match="history tier"):
        Engine(validate_config(tier))


def test_the_log_manifest_section_is_derived_from_the_request_pin(unified):
    from su_multi_arm.runtime.member_logs import MEMBER_STORE

    gen, _ = unified.write_path.pin()

    class Ctx:
        generation = gen

    section = unified.log_manifest(Ctx())["logs"]
    assert section["collection_scope"] == "incident_episode_summaries_v1" and section["history"]["store"] == MEMBER_STORE
    # archive day members (closed) and the live slice days, one tier each
    assert section["days"] == [*L.ARCHIVE_DAYS, *sorted(L.LIVE)]
    assert {d: e["tier"] for d, e in section["ledger"].items()} == {**{d: "archive" for d in L.ARCHIVE_DAYS}, **{d: "provisional" for d in L.LIVE}}
    assert section["as_of"] == "2026-11-09"


def test_incident_episodes_read_archive_members_and_live_slices(unified):
    out = tool(unified, "incident_episodes", {"window": "last 7 days"})
    assert out["status"] == "bounded" and out["rows"]
    days = {str(r["first_failure"])[:10] for r in out["rows"]}
    assert days & set(L.ARCHIVE_DAYS) and days & set(L.LIVE)  # both tiers answer
    assert out["source_unavailable"] == ["2026-11-03"]  # a day without a pinned unit: missing, never zero


def test_issue_history_builds_the_typed_tables_from_the_member(unified):
    out = tool(unified, "issue_history", {"window": "last 7 days", "list_episodes": 5})
    assert out["status"] == "bounded" and out["rows"], out.get("refusal")
    coverage = out["history"]["coverage"]
    assert coverage["archive_days"] == 3 and sorted(coverage["provisional_days"]) == sorted(L.LIVE)
    assert coverage["missing_days"] == ["2026-11-03"]
    assert all(set(row["tiers"]) <= {"archive", "provisional"} for row in out["rows"])
    assert any(row["provisional"] for row in out["rows"]) and any(not row["provisional"] for row in out["rows"])
    assert out["history"]["episodes"] and all(p["tier"] in {"archive", "provisional"} for p in out["history"]["episodes"])


def test_issue_history_counts_equal_the_member_records(unified):
    """Occurrences per rule over the window equal the sum over the served incident records."""
    out = tool(unified, "issue_history", {"window": "last 7 days", "group_by": ["signature_id"]})
    totals = {r["signature_id"]: r["occurrences"] for r in out["rows"]}
    from su_multi_arm.core.types import Measure, ScopeSpec

    gen, _ = unified.write_path.pin()
    spec = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="incident"), Predicate(field="category", op="in", value=["success"], negate=True),
                                 Predicate(field="date", op="range", lower="2026-11-03", upper="2026-11-09")],
                     group_by=["signature_id"], measure=Measure(kind="sum", field="occurrences"))
    agg = unified.records.execute(gen, spec, [])
    assert not agg.unsupported
    expected = {g.key["signature_id"]: int(g.value) for g in agg.groups}
    assert totals == {k: v for k, v in expected.items() if v}


def test_log_top_and_trend_answer_from_the_member(unified):
    top = tool(unified, "log_top", {"by": "service", "window": "last 7 days"})
    assert top["status"] == "bounded" and top["rows"] and top["basis"] == ["source_rule_query"]
    trend = tool(unified, "log_trend", {"window": "last 7 days"})
    assert trend["rows"] and {r["date"] for r in trend["rows"]} & set(L.LIVE)


def test_log_liveness_without_a_keyword_recipe_is_refused_labelled(unified):
    out = tool(unified, "log_liveness", {})
    assert out["status"] == "refused" and "keyword recipe" in out["refusal"]


def test_a_scope_on_a_log_only_field_is_not_flagged_unsupported_by_the_support_member(unified):
    """B2 sibling: the adopted support store lacks ``tenant``; its member contributes population only."""
    gen, _ = unified.write_path.pin()
    from su_multi_arm.core.types import ScopeSpec

    result = unified.records.execute(gen, ScopeSpec(predicates=[Predicate(field="tenant", op="eq", value="alderbank-prod")]), [])
    assert not result.unsupported and result.exactness == "exact"
    assert result.total and all(r.id.startswith("logs:") for r in result.listed)


# --- B2: values over a member whose schema lacks the field --------------------------------------------


def test_values_skip_a_member_whose_own_schema_lacks_the_field(unified):
    gen, _ = unified.write_path.pin()
    tenants = unified.records.values(gen, "tenant", [], 50)
    assert tenants and "alderbank-prod" in tenants  # the log member's values; the support member skipped
    assert unified.records.values(gen, "source", [], 10) == ["doc", "logs", "ticket"]
    assert unified.backends["local"].stats["collection.values_skipped:tenant"] >= 1


def test_values_of_a_log_only_field_without_the_log_member_are_empty(tmp_path, fx):
    """The staging repro: the collection schema lists ``tenant``, the adopted store does not."""
    engine = S.collection(tmp_path, fx, logs=False)
    data = copy.deepcopy(engine.config.model_dump(mode="json"))
    assert any(f["name"] == "tenant" and f.get("listable") for f in data["metadata_schema"]["fields"])
    gen, _ = engine.write_path.pin()
    assert engine.records.values(gen, "tenant", [], 10) == []
    assert engine.records.values(gen, "source", [], 10) == ["doc", "ticket"]


def test_planner_values_no_longer_fail(unified):
    from su_multi_arm.decision.values import ValueResolver

    gen, _ = unified.write_path.pin()
    values = ValueResolver(unified.config.metadata_schema, unified.records, gen, []).planner_values()
    assert values  # before: "sqlite.values: field 'category' is not listable"


# --- B3: text conditions over several strata ------------------------------------------------------------


def test_text_condition_pages_over_two_strata(unified):
    gen, _ = unified.write_path.pin()
    lexical = unified.backends["local"].lexical
    ids = text_match_ids(lexical, gen, LexicalQuery(text="crawler"), Scope())
    assert any(i.startswith("ticket:") for i in ids) and any(i.startswith("logs:") for i in ids)


def test_ranked_pages_a_large_k_and_keeps_small_k_unchanged(unified):
    from su_multi_arm.backends.sqlite.members import MAX_PAGE, ranked

    gen, _ = unified.write_path.pin()
    backend = unified.backends["local"]
    unit = next(u for u in backend.units(gen) if u.ref.member == "support")
    small = ranked(unit, LexicalQuery(text="crawler timeout"), None, 5)
    assert [h.chunk_id for h in small] == [h.chunk_id for h in unit.backend.lexical.search(unit.gen, LexicalQuery(text="crawler timeout"), None, 5).items]
    large = ranked(unit, LexicalQuery(text="crawler timeout"), None, MAX_PAGE * 5 + 1)
    assert [h.chunk_id for h in large[:5]] == [h.chunk_id for h in small] and len(large) == len({h.chunk_id for h in large})


def test_exact_phrase_count_question_runs_over_the_collection(unified):
    result = unified.search(SearchRequest(query="how many tickets contain the exact phrase crawler", mode="rules"))
    assert result is not None  # before: sqlite.page: invalid page (Page(size=50001))


# --- F3: graph arm over a layer with a source filter outside it ------------------------------------------


def test_graph_arm_layer_scope_degrades_for_a_source_outside_the_layer():
    from types import SimpleNamespace

    from su_multi_arm.arms.graph import GraphArm

    events: list[tuple[str, dict]] = []
    arm = GraphArm.__new__(GraphArm)
    arm.backend = SimpleNamespace(kind="kuzu", covered_sources=lambda: ["ticket", "doc"], layer=SimpleNamespace(name="support_graph"))
    ctx = SimpleNamespace(trace=SimpleNamespace(event=lambda name, **kw: events.append((name, kw))))
    outside = Scope(predicates=[Predicate(field="source", op="eq", value="logs")])
    assert arm.layer_scope(ctx, outside) is None and events[0][0] == "graph_scope_outside_layer"
    # a scope without predicates is unchanged (no resolution, today's path)
    assert arm.layer_scope(ctx, Scope()) == Scope()
    # other predicates: narrowed to the covered sources (the layer holds no other document)
    narrowed = arm.layer_scope(ctx, Scope(predicates=[Predicate(field="product", op="eq", value="Platform")]))
    assert narrowed.predicates[-1] == Predicate(field="source", op="in", value=["doc", "ticket"])
    # already inside the layer: unchanged
    inside = Scope(predicates=[Predicate(field="source", op="eq", value="ticket")])
    assert arm.layer_scope(ctx, inside) is inside
    # not a layer backend: unchanged
    arm.backend = SimpleNamespace(kind="kuzu")
    assert arm.layer_scope(ctx, outside) is outside


def test_graph_arm_run_returns_no_evidence_and_a_report_outside_the_layer():
    from types import SimpleNamespace

    from su_multi_arm.arms.base import ArmInput
    from su_multi_arm.arms.graph import GraphArm
    from su_multi_arm.backends import extras

    class Graph(extras.GraphExtras):
        def find_entities(self, *a, **k):
            raise AssertionError("the graph store must not be read")

        def mentions(self, *a, **k):
            raise AssertionError("the graph store must not be read")

        def traverse(self, *a, **k):
            raise AssertionError("the graph store must not be read")

    arm = GraphArm.__new__(GraphArm)
    arm.backend = SimpleNamespace(kind="kuzu", graph=Graph(), covered_sources=lambda: ["ticket", "doc"], layer=SimpleNamespace(name="g"))
    events: list[str] = []
    ctx = SimpleNamespace(trace=SimpleNamespace(event=lambda name, **kw: events.append(name)))
    inputs = ArmInput(query="crawler", scope=Scope(predicates=[Predicate(field="source", op="eq", value="logs")]), candidates=10)
    result = arm._run(ctx, inputs)
    assert result.hits == [] and result.reports[0]["kind"] == "scope_outside_layer" and events == ["graph_scope_outside_layer"]


def test_close_day_still_composes_with_the_logs_block(unified):
    manifest = unified.view.attach().generations.current()
    assert {m["member"] for m in manifest["members"]} == {"support", "logs"}
    assert date.fromisoformat(L.ARCHIVE_DAYS[-1])
