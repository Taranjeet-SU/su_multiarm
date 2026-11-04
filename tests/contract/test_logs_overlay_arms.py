"""The fallback overlay reaches the arms (addendum A2 L5/B7; overlay from the fallback slice).

Synthetic overlay rows on one gold day, end to end: derived snapshot -> day shard -> records and
documents with ``extraction_source: model_assisted``; heads the model named no signature for go to
``signature_id: unknown``; model rows produce no graph edges; ``applied_extraction`` and the
rendered share say when an answer includes them; deterministic totals and the deterministic coverage
are unchanged, the model-assisted coverage is a separate number. Evidence level: contract.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from su_multi_arm.ingestion.accounting import FieldValue, HeadAccount
from su_multi_arm.ingestion.logs_compose import read_derived
from su_multi_arm.ingestion.overlay import OverlayManifest, OverlayStore
from su_multi_arm.testing.fixtures.logs.gold import day_of

from tests.logs_gold_support import SPEC, build_gold

DAY = day_of(SPEC, 9)
AS_OF = day_of(SPEC, 10)
D = AS_OF.isoformat()


def _tool(engine, name, **args):
    return engine.call_tool(name, {"as_of": D, "start": DAY.isoformat(), "end": DAY.isoformat(), **args}).result.output


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("overlay")
    engine, pipe, _ = build_gold(tmp, days=[day_of(SPEC, 8), DAY, AS_OF])
    before = {
        "services": _tool(engine, "log_top", by="service", level=["info"]).rows,
        "coverage": _tool(engine, "log_coverage").rows,
    }
    store = pipe.store
    key = DAY.isoformat()
    manifest = store.get(key)
    units = {u["unit_id"]: u["attributes"] for u in store.read(key, "aggregates.json")["units"]}
    matches = store.read(key, "matches.jsonl.gz")
    texts = {r.id: r.message for u in store.units(key, manifest.version) for r in u.records}
    heads: dict[str, list[HeadAccount]] = {}  # the opaque lines are format_only (format matched, no message)
    plan = {"provisional": 0, "model_line": 0, "parsed_filled": 0}
    for match in matches:
        attrs = units[match["unit_id"]]
        text = texts[match["id"]]
        if match["status"] in {"unparsed", "format_only"} and "opaque payload marker" in text:
            if (attrs["tenant"], attrs["service"]) == ("t01", "svc-api"):
                body = "GET /api/opaque/77 status=500 latency_ms=1234"
                fields = [
                    FieldValue(name="endpoint", value="/api/opaque/77", type="string", start=0, end=1, source="model_line"),
                    FieldValue(name="http_method", value="GET", type="string", start=0, end=1, source="model_line"),
                    FieldValue(name="status_code", value=500, type="integer", start=0, end=1, source="model_line"),
                    FieldValue(name="latency_ms", value=1234, type="integer", start=0, end=1, source="model_line"),
                ]
                assert body  # the synthetic quotes are not checked here (the fallback slice grounds them)
                account = HeadAccount(head_id=match["id"], unit_id=match["unit_id"], final_status="model_line", stage_status=match["status"], fields=fields, extraction_source="model_assisted")
                plan["model_line"] += 1
            else:
                account = HeadAccount(head_id=match["id"], unit_id=match["unit_id"], final_status="provisional", stage_status=match["status"], signature_id="payload.opaque", pattern_owner="learned", extraction_source="model_assisted")
                plan["provisional"] += 1
        elif match["signature"] == "dependency.connect_failed" and (attrs["tenant"], attrs["service"]) == ("t02", "svc-api") and not plan["parsed_filled"]:
            fill = FieldValue(name="exception_class", value="ConnTimeout", type="string", start=0, end=1, source="model_fill")
            account = HeadAccount(head_id=match["id"], unit_id=match["unit_id"], final_status="parsed_filled", stage_status="parsed", signature_id=match["signature"], pattern_owner="user", extraction_source="model_assisted", fields=[fill])
            plan["parsed_filled"] += 1
        else:
            continue
        heads.setdefault(match["unit_id"], []).append(account)
    overlays = OverlayStore(store)
    identity = "a" * 32
    for unit_id, rows in heads.items():
        overlays.write_unit(key, manifest.version, identity, unit_id, rows)
    overlays.seal(OverlayManifest(identity=identity, partition_key=key, version=manifest.version, library_identity=manifest.library_identity or "", units=sorted(heads), created_at=datetime(2026, 3, 10, tzinfo=timezone.utc)))
    result = pipe.compose(AS_OF, pipe.library())
    return engine, pipe, result, before, plan


def test_snapshot_and_history_carry_the_model_rows(built):
    engine, pipe, result, _, plan = built
    assert plan == {"provisional": 23, "model_line": 1, "parsed_filled": 1}
    snapshot = read_derived(pipe.store, DAY.isoformat())
    rows = snapshot["overlay"]["aggregates"]
    assert sum(r["lines_pulled"] for r in rows["levels"] if r["status"] == "provisional") == plan["provisional"]
    assert [r["signature_id"] for r in rows["levels"] if r["status"] == "model_line"] == ["unknown"]
    assert DAY.isoformat() in result.history_appended  # the day's shard changed (overlay identity)
    shard = engine.manifest()["logs"]["history"]["shards"][DAY.isoformat()]
    from su_multi_arm.core.types import Predicate, ScopeSpec

    spec = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="log_day"), Predicate(field="extraction_source", op="eq", value="model_assisted")])
    stored = engine.history.execute([shard], spec, [], engine.config.metadata_schema)
    assert stored.total == len(rows["levels"]) and stored.applied_extraction == ["model_assisted"]


def test_signature_answers_include_model_rows_and_say_so(built):
    engine, _, _, _, plan = built
    out = _tool(engine, "log_top", by="signature", level=["info"])
    rows = {r["signature_id"]: r["lines"] for r in out.rows}
    assert rows["payload.opaque"] == plan["provisional"] and rows["unknown"] == plan["model_line"]
    assert "model_assisted" in out.result.applied_extraction and "model-assisted lines" in out.rendering
    deterministic = _tool(engine, "log_top", by="signature", level=["info"], extraction="deterministic")
    assert "payload.opaque" not in {r["signature_id"] for r in deterministic.rows} and "model-assisted" not in deterministic.rendering


def test_deterministic_totals_and_coverage_are_unchanged(built):
    engine, _, _, before, plan = built
    assert _tool(engine, "log_top", by="service", level=["info"]).rows == before["services"]  # never counted twice
    coverage = {r["service"]: r for r in _tool(engine, "log_coverage").rows}
    for row in before["coverage"]:
        assert coverage[row["service"]]["deterministic_coverage"] == row["deterministic_coverage"]
    from su_multi_arm.core.types import Measure, Predicate, ScopeSpec

    spec = ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="coverage_day"), Predicate(field="date", op="eq", value=DAY.isoformat())], measure=Measure(kind="sum", field="lines_provisional"))
    assert engine.call_tool("aggregate", {"scope": spec.model_dump(mode="json")}).result.output.result.value == plan["provisional"]
    extracted = engine.call_tool("aggregate", {"scope": {**spec.model_dump(mode="json"), "measure": {"kind": "max", "field": "extracted_coverage"}}}).result.output.result.value
    deterministic = engine.call_tool("aggregate", {"scope": {**spec.model_dump(mode="json"), "measure": {"kind": "max", "field": "coverage"}}}).result.output.result.value
    assert extracted > deterministic


def test_model_fields_reach_endpoints_and_exceptions(built):
    engine, _, _, _, _ = built
    errors = _tool(engine, "log_endpoints", service="svc-api", metric="errors_5xx")
    assert {"endpoint": "/api/opaque/{id}", "lines": 1} in [{k: r[k] for k in ("endpoint", "lines")} for r in errors.rows]
    exceptions = {r["exception_class"]: r["lines"] for r in _tool(engine, "log_top", by="exception_class").rows}
    assert exceptions["ConnTimeout"] == 1 and "TimeoutError" in exceptions


def test_model_rows_produce_no_graph_edges(built):
    engine, _, _, _, _ = built
    view = engine.call_tool("graph_neighbourhood", {"entity": "svc-api", "as_of": D}).result.output
    names = {n.id: n.name for n in view.nodes}
    assert "/api/opaque/{id}" not in {names[e.object] for e in view.edges}
    raised = engine.call_tool("graph_neighbourhood", {"entity": "dependency.connect_failed", "as_of": D}).result.output
    assert not [e for e in raised.edges if e.predicate == "raises"]
    unknown = engine.call_tool("graph_neighbourhood", {"entity": "payload.opaque", "as_of": D}).result.output
    assert unknown.edges == []
