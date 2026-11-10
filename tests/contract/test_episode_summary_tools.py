"""Summary source -> canonical generation -> the same HTTP/MCP-facing tool registry."""
from copy import deepcopy

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.episode_summaries import CONTRACT, episodes, summary_documents, validate_summary
from tests.logs_gold_support import gold_config
from tests.logs_support import write_library
from tests.unit.test_episode_summaries import PHRASES, RULES, SCOPE, compiled, response


@pytest.fixture
def engine(tmp_path):
    summary = validate_summary(response(), compiled(), RULES, PHRASES)
    snapshots = []
    for tenant in ("synthetic", "other"):
        scope = {**SCOPE, "tenant": tenant, "index": f"{tenant}-2026.01.01"}
        rows = episodes(summary, scope, RULES)
        rows[0]["evidence"] = [{"id": tenant, "ts": rows[0]["first_failure"], "message": "connection refused redis"}]
        snapshots.append({"scope": scope, "summary": summary, "episodes": rows,
                          "observed_at": "2026-01-02T00:00:00Z", "evidence_documents": 1})
    docs = summary_documents("synthetic-summaries", snapshots, RULES)
    data = gold_config(tmp_path)
    write_library(tmp_path / "patterns")
    data["dataset"] = "synthetic-summaries"
    data["logs"]["history"] = {"tier": False}
    base = validate_config(data).model_dump(mode="json")
    base["pipeline"]["arms"]["community"] = {"backend": "local"}
    base["extraction"]["pattern_library"]["log_links"] = True
    base["tools"]["enabled"] = ["incident_episodes", "log_top", "log_signature", "log_coverage", "aggregate", "get_document"]
    fields = base["metadata_schema"]["fields"]
    existing = {f["name"]: f for f in fields}
    if existing.get("kind", {}).get("enum_values") is not None:
        existing["kind"]["enum_values"] = sorted(set(existing["kind"]["enum_values"]) | {d.kind for d in docs})
    for doc in docs:
        for name, value in doc.metadata.items():
            if name in existing:
                continue
            kind = "datetime" if name in {"window_start", "window_end", "observed_at"} else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
            f = {"name": name, "type": kind, "nullable": True, "filterable": True, "groupable": True, "sortable": True}
            existing[name] = f
            fields.append(f)
    eng = Engine(validate_config(base))
    eng.write_path.ingest(docs, replace=True, rebuild=True, manifest_extra={"logs": {
        "collection_scope": CONTRACT, "source": "gold", "days": ["2026-01-01"], "as_of": "2026-01-01",
        "date_basis": "event_time", "date_timezone": "UTC", "partitions": [],
    }})
    return eng


def test_summary_tool_counts_repeats_but_never_invents_recovery(engine):
    out = engine.call_tool("incident_episodes", {"as_of": "2026-01-01", "window": "today"}).result.output
    assert out.statistics["keyword_documents"] == 20002
    assert out.statistics["evidence_documents"] == 2
    assert out.statistics["episodes_total"] == 2
    assert len(out.rows) == 2
    assert all(r["occurrences"] == 10000 and r["resolution_seconds"] is None for r in out.rows)
    assert all(r["observed_window_seconds"] == 119 for r in out.rows)
    assert out.keywords["connection refused"] == 20000
    assert engine.tools.schemas()["incident_episodes"]["input"]["properties"]["tenant"]


def test_tenant_filter_applies_to_statistics_and_evidence(engine):
    out = engine.call_tool("incident_episodes", {"as_of": "2026-01-01", "window": "today", "tenant": "synthetic"}).result.output
    assert out.statistics["keyword_documents"] == 10001
    assert len(out.rows) == 1 and out.rows[0]["tenant"] == "synthetic"
    assert out.rows[0]["evidence"] == "connection refused redis"
    absent = engine.call_tool("incident_episodes", {"as_of": "2026-01-01", "window": "today", "tenant": "absent"}).result.output
    assert not absent.rows and absent.statistics["keyword_documents"] == 0


def test_http_tool_is_identical_to_python_registry_output(engine):
    from fastapi.testclient import TestClient
    from su_multi_arm.serving.http import create_app

    args = {"as_of": "2026-01-01", "window": "today", "tenant": "synthetic"}
    expected = engine.call_tool("incident_episodes", args).result.output.model_dump(mode="json")
    with TestClient(create_app(engine=engine)) as client:
        response = client.post("/v2/tools/incident_episodes", json=args)
    assert response.status_code == 200
    assert response.json()["result"]["output"] == expected


def test_principal_policy_scopes_counts_keywords_and_evidence(engine):
    from su_multi_arm.runtime.acl import Principal, StaticPolicy

    engine.acl = StaticPolicy("tenant")
    principal = Principal(id="synthetic-reader", tenant="synthetic", scopes=["search", "answer"])
    out = engine.call_tool("incident_episodes", {"as_of": "2026-01-01", "window": "today"}, principal=principal).result.output
    assert out.statistics["keyword_documents"] == 10001
    assert out.statistics["source_scopes"] == 1
    assert out.keywords["connection refused"] == 10000
    assert {r["tenant"] for r in out.rows} == {"synthetic"}


def test_existing_planner_intent_uses_summary_cells_and_rule_label(engine):
    args = {"as_of": "2026-01-01", "window": "today", "by": "service_signature", "tenant": "synthetic", "level": ["error", "fatal"]}
    out = engine.call_tool("log_top", args).result.output
    assert len(out.rows) == 1
    assert out.rows[0]["error_count"] == 10000
    assert out.rows[0]["error_type"] == "Redis connection refused"
    assert out.rows[0]["level"] == "RULE"
    assert out.columns == ["service", "error_type", "error_count"]
    assert out.exactness == "bounded"
    assert "not confirmed outages" in " ".join(out.notes)


@pytest.mark.parametrize("tool,args", [
    ("log_coverage", {}),
    ("log_top", {"level": ["fatal"]}),
    ("log_top", {"metric": "error_rate"}),
])
def test_unrecorded_measurements_are_refused_instead_of_falling_back(engine, tool, args):
    args = {"as_of": "2026-01-01", "window": "today", **deepcopy(args)}
    output = engine.call_tool(tool, args).result.output
    assert output.status == "refused" and output.refusal


def test_outage_question_reads_stored_graph_and_community_episode_evidence(engine):
    from su_multi_arm.models import SearchRequest

    result = engine.answer(SearchRequest(query="last 3 day outage", as_of="2026-01-01"))
    assert not result.answer.insufficient_evidence
    assert "Matching logs" in result.answer.answer and "Unknown" in result.answer.answer
    assert set(result.retrieval.arm_hits) == {"graph", "community"}
    assert any(result.retrieval.arm_hits.values())
    assert result.trace["model_calls"] == []


def test_summary_investigation_marks_large_selections_bounded(engine):
    from su_multi_arm.core.types import Scope
    from su_multi_arm.decision.log_intents import LogEvidenceScope
    from su_multi_arm.runtime.log_investigation import prepare

    class Ops:
        def __getattr__(self, name):
            return getattr(engine, name)
        def log_execute(self, ctx, spec):
            result = engine.log_execute(ctx, spec)
            return result.model_copy(update={"total": 501})

    scope, _, report = prepare(Ops(), engine._context_for(None),
                              LogEvidenceScope(as_of="2026-01-01", window="today", service=["worker"]),
                              Scope(), "outage")
    assert scope.document_ids and report["limited"] is True


def test_build_salt_includes_header_link_recipe_version(engine):
    extractor = engine.write_path.extractor
    prepared = extractor.prepare_build()
    assert prepared["salt"] == extractor.reuse_salt()
    assert prepared["salt"] != extractor._pattern.compiled().reuse_salt()
