"""Unknown direct-tool scopes and conflicting count provenance; synthetic pipeline, no providers."""

from __future__ import annotations

import pytest

from tests.logs_gold_support import AS_OF, build_gold, gold_config


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    root = tmp_path_factory.mktemp("scope-coverage")
    data = gold_config(root)
    data["sources"]["gold"]["counts"]["failures"] = {"levels": ["ERROR", "FATAL"], "tokens": ["error", "fatal"]}
    engine, _, _ = build_gold(root, days=[AS_OF], data=data)
    return engine


def output(engine, **args):
    window = {} if "start" in args else {"window": "today"}
    return engine.call_tool("log_top", {"as_of": AS_OF.isoformat(), **window, "by": "service", "level": ["ERROR", "FATAL"], **args}).result.output


@pytest.mark.parametrize("filters", [{"tenant": "missing-client"}, {"service": ["missing-service"]}, {"service": ["svc-api", "missing-service"]}])
def test_unknown_names_refuse_without_claiming_zero(gold, filters):
    result = output(gold, **filters)
    assert result.status == "refused" and result.exactness == "bounded"
    assert result.rows == [] and "no observed coverage" in result.refusal
    assert "not evidence of zero errors" in result.rendering
    assert result.scope is not None  # MCP consumers can inspect the requested scope


def test_known_service_and_tenant_keep_the_explicit_day(gold):
    result = output(gold, service=["svc-api"], tenant="t01", start=AS_OF.isoformat(), end=AS_OF.isoformat())
    assert result.status != "refused" and result.rows
    assert {r["service"] for r in result.rows} == {"svc-api"}
    assert result.window["start"] == result.window["end"] == AS_OF.isoformat()
    assert not any("inconsistent source count" in note for note in result.notes)


def test_known_names_on_an_unserved_day_report_missing_day(gold):
    result = output(gold, service=["svc-api"], tenant="t01", start="2030-01-01", end="2030-01-01")
    assert result.status == "bounded" and result.source_unavailable == ["2030-01-01"]
    assert "No composed data" in result.rendering


def test_inconsistent_source_counts_are_bounded_and_disclosed(gold, monkeypatch):
    execute = gold.log_execute

    def inconsistent(ctx, spec):
        result = execute(ctx, spec)
        if spec.measure and spec.measure.kind == "sum" and spec.measure.field == "count_failures":
            result = result.model_copy(update={"groups": [g.model_copy(update={"value": (g.value or 0) + 1000}) for g in result.groups]})
        return result

    monkeypatch.setattr(gold, "log_execute", inconsistent)
    result = output(gold, service=["svc-api"], tenant="t01")
    assert result.status == result.exactness == "bounded"
    assert any("inconsistent source count metadata" in note for note in result.bounded_by)
    assert "source accuracy is unverified" in result.rendering
    assert all(r["source_exactness"] == "bounded" for r in result.source_counts)


def test_overlapping_level_counts_do_not_imply_inconsistent_union(gold, monkeypatch):
    """A multi-valued severity or token match can overlap: max <= union <= sum is valid."""
    execute = gold.log_execute

    def overlap(ctx, spec):
        result = execute(ctx, spec)
        if spec.measure and spec.measure.kind == "sum" and spec.measure.field == "count_failures":
            values = []
            for field in ("count_errors", "count_fatal"):
                separate = execute(ctx, spec.model_copy(update={"measure": spec.measure.model_copy(update={"field": field})}))
                values.append({tuple(sorted(g.key.items())): g.value or 0 for g in separate.groups})
            result = result.model_copy(update={"groups": [g.model_copy(update={"value": max(v.get(tuple(sorted(g.key.items())), 0) for v in values)}) for g in result.groups]})
        return result

    monkeypatch.setattr(gold, "log_execute", overlap)
    result = output(gold, service=["svc-api"], tenant="t01")
    assert not any("inconsistent source count metadata" in note for note in result.notes)
