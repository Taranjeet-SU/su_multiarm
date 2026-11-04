"""History tier H1 and lineage H2 (addendum A2 rev 4 §4.8, §11.R4).

* the generation holds the window shard only; day documents are append-only, content-addressed day
  shards read through the generation's shard set; answers equal the no-tier answers;
* appending a day writes that day's rows only; an unchanged reparse writes nothing; a changed day is
  a new shard that only the new generation references (the shard filter keeps generations apart);
* gc by reference; import from format-1 and format-2 derived snapshots with counts equal to the
  snapshot's; graph links of the whole history with per-day weights from the shards;
* H2: ``signature_history`` per library identity, ``signature_lineage`` from a supersede decision,
  answers fold the superseded signature.
Synthetic gold set; evidence level: contract.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest
import yaml

from su_multi_arm.ingestion.history import DAY_KINDS
from su_multi_arm.ingestion.logs_compose import read_derived, referenced_shards
from su_multi_arm.testing.fixtures.logs.gold import LIBRARY, day_of, pulled_days

from tests.logs_gold_support import AS_OF, QUESTIONS, SPEC, build_gold, gold_config

D = AS_OF.isoformat()


def no_tier(tmp):
    data = gold_config(tmp)
    data["logs"]["history"] = {"tier": False}
    return data


@pytest.fixture(scope="module")
def tiered(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tier")
    engine, pipe, report = build_gold(tmp)
    return engine, pipe, report, tmp


@pytest.fixture(scope="module")
def flat(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("flat")
    engine, _, _ = build_gold(tmp, data=no_tier(tmp))
    return engine


def comparable(output):
    data = output.model_dump(mode="json")
    return {k: data[k] for k in ("rows", "rendering", "exactness", "status", "source_unavailable")}


def test_generation_holds_the_window_shard_only(tiered):
    engine, _, report, _ = tiered
    kinds = {d.kind for d in engine.write_path._load_corpus(engine.write_path.current()[0]).values()}
    assert not kinds & DAY_KINDS and {"signature", "signature_catalog", "graph_link", "incident"} <= kinds
    shards = engine.manifest()["logs"]["history"]["shards"]
    assert sorted(shards) == [d.isoformat() for d in pulled_days(SPEC)]
    assert report["history_rows"] == engine.history.row_count() and len(report["history_appended"]) == len(shards)


@pytest.mark.parametrize("question,intent,args", QUESTIONS, ids=[q[1] + str(i) for i, q in enumerate(QUESTIONS)])
def test_answers_equal_the_no_tier_answers(tiered, flat, question, intent, args):
    engine = tiered[0]
    tool_args = args if intent == "log_around" else {**args, "as_of": D}
    assert comparable(engine.call_tool(intent, tool_args).result.output) == comparable(flat.call_tool(intent, tool_args).result.output)


def test_import_counts_equal_the_snapshot(tiered):
    engine, pipe, _, _ = tiered
    shards = engine.manifest()["logs"]["history"]["shards"]
    for day in pulled_days(SPEC):
        snapshot = read_derived(pipe.store, day.isoformat())
        expected: dict[str, int] = {}
        for row in snapshot["aggregates"]["signatures"]:
            expected[row["signature_id"]] = expected.get(row["signature_id"], 0) + row["lines"]
        out = engine.call_tool("aggregate", {"scope": {"predicates": [{"field": "kind", "op": "eq", "value": "signature_day"}, {"field": "date", "op": "eq", "value": day.isoformat()}], "group_by": ["signature_id"], "measure": {"kind": "sum", "field": "lines"}}})
        got = {g.key["signature_id"]: g.value for g in out.result.output.result.groups}
        assert got == expected, day
        units = engine.history.execute([shards[day.isoformat()]], _kind_scope("unit_day"), [], engine.config.metadata_schema)
        assert units.total == len(snapshot["aggregates"]["units"])


def _kind_scope(kind):
    from su_multi_arm.core.types import Predicate, ScopeSpec

    return ScopeSpec(predicates=[Predicate(field="kind", op="eq", value=kind)])


def test_graph_links_cover_the_history_with_shard_weights(tiered):
    engine = tiered[0]
    view = engine.call_tool("graph_neighbourhood", {"entity": "svc-api"}).result.output
    names = {n.id: n.name for n in view.nodes}
    emits = [e for e in view.edges if e.predicate == "emits" and names[e.object] == "dependency.connect_failed"]
    assert emits and {w["date"]: w["lines"] for w in emits[0].weights} == {d.isoformat(): 4 * int(d.day) for d in pulled_days(SPEC)}


def _rewrite_library(tmp, mutate):
    data = copy.deepcopy(LIBRARY)
    mutate(data)
    (tmp / "patterns" / "library.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def test_append_unchanged_reparse_changed_day_and_gc(tmp_path):
    days = pulled_days(SPEC)
    engine, pipe, report = build_gold(tmp_path, days=days[:-1])
    first_gen = engine.write_path.current()[0]
    before = {s["shard_id"]: s["rows"] for s in engine.history.shards()}
    # append the last day: only its rows are written
    library = pipe.library()
    pipe.pull(days[-1], library)
    appended = pipe.compose(AS_OF, library)
    assert appended.history_appended == [days[-1].isoformat()]
    new_rows = engine.history.row_count() - sum(before.values())
    assert appended.history_rows == new_rows == engine.history.row_count([engine.manifest()["logs"]["history"]["shards"][days[-1].isoformat()]])
    assert all(s["shard_id"] in {x["shard_id"] for x in engine.history.shards()} for s in [{"shard_id": k} for k in before])
    # a library change that alters no document: reparse (delta) but no row written
    _rewrite_library(tmp_path, lambda d: d["patterns"].append({"id": "msg.never", "kind": "message", "status": "approved", "priority": 1, "regex": "zq never zq", "signature": {"id": "never.seen", "category": "x"}, "examples": {"positive": ["zq never zq"]}, "provenance": {"author": "user"}}))
    unchanged = pipe.compose(AS_OF, pipe.library())
    assert unchanged.status == "built" and unchanged.reparse and unchanged.history_rows == 0
    # a change of one day's documents: a new shard for the changed days only, old generations keep theirs
    _rewrite_library(tmp_path, lambda d: d["patterns"].append({"id": "msg.oom", "kind": "message", "status": "approved", "priority": 80, "regex": "process crashed: (?P<why>out of memory)", "signature": {"id": "process.oom", "category": "x"}, "examples": {"positive": ["process crashed: out of memory"]}, "provenance": {"author": "user"}}))
    changed = pipe.compose(AS_OF, pipe.library())
    assert changed.history_appended == [day_of(SPEC, 9).isoformat()]
    old = engine.history.execute(engine.history_shards(first_gen), _sig_scope("process.crashed"), [], engine.config.metadata_schema)
    new = engine.history.execute(engine.history_shards(engine.write_path.current()[0]), _sig_scope("process.crashed"), [], engine.config.metadata_schema)
    assert old.total == 2 and new.total == 0  # rows (two units); the shard filter keeps the generations apart
    # gc by reference: drop every generation but the active one, then nothing of the old shards stays
    registry = engine.write_path.registry
    active = registry.current()["generation"]
    referenced = referenced_shards(engine.write_path)
    assert engine.history.gc(referenced) == []  # all still referenced by a registry generation
    keep = set(((registry.manifest(active) or {}).get("logs") or {}).get("history", {}).get("shards", {}).values())
    removed = engine.history.gc(keep)
    assert removed and {s["shard_id"] for s in engine.history.shards()} == keep


def _sig_scope(sig):
    from su_multi_arm.core.types import Predicate, ScopeSpec

    return ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="signature_day"), Predicate(field="signature_id", op="eq", value=sig)])


def test_format_one_snapshot_imports_its_a1_kinds(tmp_path):
    days = pulled_days(SPEC)[:2]
    engine, pipe, _ = build_gold(tmp_path, days=days)
    store = pipe.store
    from su_multi_arm.ingestion.logs_compose import derived_dir
    import gzip

    path = derived_dir(store) / f"{days[0].isoformat()}.json.gz"
    payload = json.loads(gzip.decompress(path.read_bytes()))
    legacy = {k: payload[k] for k in ("key", "version", "library_identity", "aggregates", "hourly_cells", "exemplars")}
    legacy["aggregates"] = {k: v for k, v in legacy["aggregates"].items() if k in ("units", "signatures")}
    path.write_bytes(gzip.compress(json.dumps(legacy).encode()))
    store.expire(days[0].isoformat())  # the partition is gone: only the format-1 snapshot remains
    later = days[0] + timedelta(days=10)  # day 1 is beyond the 10-day window: served from its snapshot
    result = pipe.compose(later, pipe.library())
    assert days[0].isoformat() in result.analytics_unavailable
    shard = engine.manifest()["logs"]["history"]["shards"][days[0].isoformat()]
    kinds = {d.kind for d in engine.history.documents([shard], [r.id for r in engine.history.execute([shard], _kinds_scope(), [], engine.config.metadata_schema).listed])}
    assert kinds and kinds <= {"unit_day", "signature_day", "service_day", "signature_service_day"}
    got = engine.history.execute([shard], _sum_scope(), [], engine.config.metadata_schema).value
    assert got == sum(r["lines"] for r in legacy["aggregates"]["signatures"])


def _kinds_scope():
    from su_multi_arm.core.types import Predicate, ScopeSpec

    return ScopeSpec(predicates=[Predicate(field="kind", op="in", value=sorted(DAY_KINDS))], limit=10000)


def _sum_scope():
    from su_multi_arm.core.types import Measure, Predicate, ScopeSpec

    return ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="signature_day")], measure=Measure(kind="sum", field="lines"))


def test_lineage_and_history_fold_a_superseded_signature(tmp_path):
    engine, pipe, _ = build_gold(tmp_path)
    _rewrite_library(
        tmp_path,
        lambda d: d["patterns"].append(
            {"id": "msg.process.oom", "kind": "message", "status": "approved", "priority": 80, "regex": "process crashed: (?P<why>out of memory)", "signature": {"id": "process.oom", "category": "availability"}, "examples": {"positive": ["process crashed: out of memory"]}, "provenance": {"author": "user"}}
        ),
    )
    (tmp_path / "patterns" / "decisions.yaml").write_text(
        yaml.safe_dump([{"op": "supersede", "id": "msg.process.oom", "target": "msg.process.crashed", "by": "user:operator", "at": "2026-03-10T08:00:00Z", "reason": "renamed"}], sort_keys=False)
    )
    library = pipe.library()
    pipe.compose(AS_OF, library)
    lineage = engine.call_tool("aggregate", {"scope": {"predicates": [{"field": "kind", "op": "eq", "value": "signature_lineage"}], "limit": 10}}).result.output.result
    assert lineage.total == 1
    out = engine.call_tool("log_top", {"by": "signature", "level": ["fatal"], "window": "yesterday", "as_of": D}).result.output
    assert [(r["signature_id"], r["lines"]) for r in out.rows] == [("process.oom", 4)]
    affected = engine.call_tool("log_affected", {"signature_id": "process.oom", "window": "last 10 days", "as_of": D}).result.output
    assert sum(r["lines"] for r in affected.rows) == 4
    history = engine.call_tool("aggregate", {"scope": {"predicates": [{"field": "kind", "op": "eq", "value": "signature_history"}], "group_by": ["signature_id"]}}).result.output.result
    assert {g.key["signature_id"] for g in history.groups} >= {"http.request", "process.oom"}
