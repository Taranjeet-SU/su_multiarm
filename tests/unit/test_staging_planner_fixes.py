"""Staging findings of 2026-11-10 on the planner / coverage / serving side (synthetic regressions).

* F1/F2: a step's half is labelled by the sources it reads (a ticket count is the knowledge half);
  which arms need the knowledge-source scope, and only when a non-knowledge source exists;
* F4: the link builders and ``identity candidates`` read the declared ``field_map`` /
  ``time_roles`` / ``identity_namespace`` instead of hardcoded ``account`` / ``created_at``;
* F6: a model call that never reached the provider is not booked on the ``serving.answers`` day
  ledger (a call that reached it still is);
* F7: ``identity describe`` reports what an engine serves; log records get a legacy UI label.

Synthetic data only; mocked transports, no network.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from su_multi_arm.core.config_collection import CollectionConfig
from su_multi_arm.core.config_crosssource import CrossSourceConfig
from su_multi_arm.core.types import Predicate, ScopeSpec
from su_multi_arm.crosssource.identity import IdentityView, import_registry, sha256_file
from su_multi_arm.crosssource.links import LinkSettings, build_links
from su_multi_arm.crosssource.links.builders import collection_fields
from su_multi_arm.decision.base import RetrievalStep, StructuredStep, TypedStep
from su_multi_arm.decision.log_intents import LogIntentCall
from su_multi_arm.decision.two_halves import operation_sources, scoped_arms, step_half
from su_multi_arm.learning.patterns.spend import DaySpendLedger, TotalSpendLedger
from su_multi_arm.providers import JsonModel, RunBudgetMeter
from su_multi_arm.providers.base import ProviderConnectionError, ProviderServerError, ProviderTimeout
from su_multi_arm.runtime.answers_spend import SCOPE, AnswerSpend
from su_multi_arm.runtime.budget import BudgetLimits, PriceTable
from su_multi_arm.runtime.context import new_context
from tests.contract.test_providers import GEN, KEY, SECRET, Recorder, Reply, openai_role
from tests.links_support import incident, ticket

KNOWLEDGE = ["ticket", "doc"]
SUPPORT = {"kind": "support", "sources": ["ticket", "doc"], "record_kinds": ["ticket", "doc"], "id_namespaces": ["ticket:", "https://docs.example.org/"]}
LOGS = {"kind": "logs", "sources": ["logs"], "record_kinds": ["incident", "signature"], "id_namespaces": ["logs:"], "cadence": "minutes", "producer": {"kind": "log_host"}}


def collection(**members) -> CollectionConfig:
    return CollectionConfig.model_validate({"mode": "partitioned", "members": members})


# --- F1/F2: halves by sources; which arms are scoped --------------------------------------------


def test_a_structured_count_is_the_knowledge_half_and_log_intents_are_operations():
    kinds = {"ticket": {"ticket"}, "doc": {"doc"}, "incident": {"logs"}}
    count = StructuredStep(arm="structured", scope=ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="ticket")]))
    assert step_half(count, KNOWLEDGE, kinds) == "knowledge"
    assert step_half(StructuredStep(arm="structured", scope=ScopeSpec()), KNOWLEDGE, kinds) == "knowledge"
    incidents = StructuredStep(arm="structured", scope=ScopeSpec(predicates=[Predicate(field="kind", op="in", value=["incident"])]))
    assert step_half(incidents, KNOWLEDGE, kinds) == "operations"
    from su_multi_arm.decision.base import log_decision
    from su_multi_arm.decision.intent import Intents

    intent = log_decision(LogIntentCall(intent="issue_history", args={"window": "last 3 days"}), Intents(), agent="t").plan.steps[0]
    assert step_half(intent, KNOWLEDGE, kinds) == "operations"
    assert step_half(RetrievalStep(arm="lexical", query="q"), KNOWLEDGE) == "knowledge"
    assert step_half(RetrievalStep(arm="lexical", query="q", filters={"source": "logs"}), KNOWLEDGE) == "operations"
    assert step_half(TypedStep(arm="typed_query", question="how many tickets per account"), KNOWLEDGE) == "knowledge"


def test_arms_are_scoped_only_when_a_non_knowledge_source_exists():
    config = SimpleNamespace(collection=collection(support=SUPPORT), logs=None)
    assert scoped_arms(config, KNOWLEDGE) == frozenset() and operation_sources(config, KNOWLEDGE) == []
    config = SimpleNamespace(collection=collection(support=SUPPORT, logs=LOGS), logs=None)
    assert scoped_arms(config, KNOWLEDGE) == {"lexical", "dense", "graph", "community", "segment", "structured"}
    assert "exact_id" not in scoped_arms(config, KNOWLEDGE)  # a named identifier resolves where it lives
    assert operation_sources(config, KNOWLEDGE) == ["logs"]
    assert scoped_arms(config, KNOWLEDGE, enabled=False) == frozenset()
    # a graph arm served only by a support layer needs no scope
    data = {"mode": "partitioned", "members": {"support": SUPPORT, "logs": LOGS},
            "layers": [{"name": "kg", "kind": "graph_attach", "sources": ["ticket", "doc"], "arms": ["graph"], "backend": "kg"}]}
    config = SimpleNamespace(collection=CollectionConfig.model_validate(data), logs=None)
    assert "graph" not in scoped_arms(config, KNOWLEDGE) and "lexical" in scoped_arms(config, KNOWLEDGE)
    # one store with a logs block: every scoped arm
    assert scoped_arms(SimpleNamespace(collection=None, logs=object()), KNOWLEDGE) and not scoped_arms(SimpleNamespace(collection=None, logs=None), KNOWLEDGE)


# --- F4: declared field names in links and identity ---------------------------------------------


def _view(**matched: str) -> IdentityView:
    return IdentityView(True, None, "r9", "0" * 64, {tuple(k.split("|")): v for k, v in matched.items()})


def test_the_link_builder_reads_the_declared_customer_and_time_fields():
    declared = collection(
        support={**SUPPORT, "identity_namespace": "support.account", "field_map": {"customer_ref": "organization"}, "time_roles": {"ticket": {"event_time": "created_date"}}},
        logs={**LOGS, "identity_namespace": "logs.tenant"},
    )
    customers, times = collection_fields(declared)
    assert customers["ticket"] == ("support.account", "organization") and times["ticket"] == "created_date"
    assert customers["incident"] == ("logs.tenant", "tenant")  # the default field under the declared namespace
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    t = ticket(1, None, "2026-11-05T12:00:00Z", "crawler.fetch_timeout on every crawl")
    t = t.model_copy(update={"metadata": {"source": "ticket", "organization": "Acme", "created_date": "2026-11-05T12:00:00Z"}})
    view = _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"})
    # the defaults read ``account`` / ``created_at``: nothing links
    default = build_links([ep, t], view)
    assert not [x for x in default.links if x.pair == "ticket-episode"] and default.report["skipped"]["ticket:no_customer_value"] == 1
    settings = LinkSettings.from_config(None, collection=declared)
    result = build_links([ep, t], view, settings)
    [link] = [x for x in result.links if x.pair == "ticket-episode"]
    assert link.evidence["customer_id"] == "cust:a" and link.evidence["ticket_time"] == "2026-11-05T12:00:00Z"
    assert result.report["fields"]["customer"]["ticket"] == ["support.account", "organization"] and result.report["fields"]["event_time"]["ticket"] == "created_date"


def test_a_declared_absent_customer_field_never_joins():
    declared = collection(support={**SUPPORT, "field_map": {"customer_ref": None, "event_time": "opened_at"}}, logs=LOGS)
    customers, times = collection_fields(declared)
    assert customers["ticket"] is None and times["ticket"] == "opened_at"
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    t = ticket(1, "Acme", "2026-11-05T12:00:00Z", "crawler.fetch_timeout")
    result = build_links([ep, t], _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"}), LinkSettings.from_config(None, collection=declared))
    assert not [x for x in result.links if x.pair == "ticket-episode"] and result.report["skipped"]["ticket:no_customer_value"] == 1


def test_identity_candidates_read_the_declared_field(tmp_path):
    from su_multi_arm.cli_identity import _document_values, declared_field

    config = SimpleNamespace(collection=collection(support={**SUPPORT, "identity_namespace": "support.account", "field_map": {"customer_ref": "organization"}}, logs=LOGS))
    assert declared_field(config, "support.account", ["ticket"]) == "organization"
    assert declared_field(SimpleNamespace(collection=None), "support.account", []) == "account"  # the default
    docs = tmp_path / "docs.jsonl"
    docs.write_text("\n".join(json.dumps({"id": f"ticket:{i}", "kind": "ticket", "metadata": {"organization": org}}) for i, org in enumerate(["Acme", "Acme", "Beta"])) + "\n")
    args = argparse.Namespace(documents=docs, namespace="support.account", field=None, kind=["ticket"])
    counts, missing = _document_values(args, config)
    assert counts == {"Acme": 2, "Beta": 1} and missing == [] and args.field == "organization"


# --- F6: calls that never reached the provider are not booked ------------------------------------

PRICES = PriceTable(version="test-1", prices={"gpt-4.1-mini": {"input_per_million": 100.0, "output_per_million": 100.0}})


def _spend(tmp_path: Path) -> AnswerSpend:
    ledger = tmp_path / "answers.sqlite"
    day = DaySpendLedger(ledger, max_calls=10, max_usd=5.0, scope=SCOPE, role="answers")
    return AnswerSpend(day, {"openai": TotalSpendLedger(ledger, key="provider:openai", max_usd=2.0)}, {"gpt-4.1-mini": "openai"})


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("connect timed out"), httpx.ProxyError("tunnel refused")])
def test_a_call_refused_before_sending_is_not_booked(tmp_path, monkeypatch, error):
    monkeypatch.setenv(KEY, SECRET)
    spend = _spend(tmp_path)
    model = JsonModel(openai_role(), transport=Recorder(raises=error).transport)
    ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=5, max_usd=1.0), price_table=PRICES, spend=spend)
    with pytest.raises((ProviderConnectionError, ProviderTimeout)) as raised:
        model.call("S", {}, Reply, RunBudgetMeter(ctx, "generator", "gpt-4.1-mini"), "generator")
    assert raised.value.sent is False
    usage = spend.usage()
    assert usage["day"]["committed_usd"] == 0 and usage["day"]["reserved_usd"] == 0 and usage["day"]["calls"] == 0
    assert usage["totals"]["openai"]["used_usd"] == 0
    snapshot = ctx.budget.snapshot()
    assert snapshot["reserved"].get("usd", 0) == 0 and snapshot["used"].get("usd", 0) == 0 and snapshot["used"]["calls"] == 1
    [event] = [e for s in ctx.trace.spans for e in s.events if e.get("name") == "model_call"]
    assert event["sent"] is False and event["success"] is False


def test_a_call_that_reached_the_provider_is_still_booked(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY, SECRET)
    spend = _spend(tmp_path)
    for error in (httpx.ReadTimeout("read timed out"), None):
        recorder = Recorder(raises=error) if error is not None else Recorder({"error": {"message": "bad gateway"}}, status=502)
        model = JsonModel(openai_role(), transport=recorder.transport)
        ctx = new_context(GEN, limits=BudgetLimits(max_llm_calls=5, max_usd=1.0), price_table=PRICES, spend=spend)
        with pytest.raises((ProviderTimeout, ProviderServerError)) as raised:
            model.call("S", {}, Reply, RunBudgetMeter(ctx, "generator", "gpt-4.1-mini"), "generator")
        assert raised.value.sent is True
    usage = spend.usage()["day"]
    assert usage["calls"] == 2 and usage["committed_usd"] > 0 and usage["reserved_usd"] == 0  # booked at the estimate (unreported usage)


# --- F7: identity describe and the legacy label ---------------------------------------------------


def test_identity_describe_reports_what_is_served(tmp_path):
    from su_multi_arm.cli_identity import describe
    from su_multi_arm.testing.fixtures.unified import load

    registry = tmp_path / "ids.sqlite"
    import_registry(registry, load(Path(__file__).resolve().parents[1] / "fixtures" / "unified").registry, now=lambda: datetime(2026, 11, 9, tzinfo=timezone.utc))
    identity = {"registry_path": str(registry), "sha256": sha256_file(registry)}
    alone = describe(SimpleNamespace(crosssource=CrossSourceConfig.model_validate({"identity": identity})))
    assert alone["joins_enabled"] is True and alone["served"] is False and alone["consumers"] == [] and "note" in alone
    planned = describe(SimpleNamespace(crosssource=CrossSourceConfig.model_validate({"identity": identity, "planner": {"enabled": True, "knowledge_sources": ["ticket"]}})))
    assert planned["served"] is True and planned["consumers"] == ["crosssource.planner"] and planned["registry_version"].startswith("r") and planned["joinable_aliases"] > 0
    stale = describe(SimpleNamespace(crosssource=CrossSourceConfig.model_validate({"identity": {**identity, "sha256": "0" * 64}, "planner": {"enabled": True, "knowledge_sources": ["ticket"]}})))
    assert stale["served"] is False and stale["digest"] == "mismatch" and stale["reason"] == "digest_mismatch"


def test_log_records_get_a_legacy_label_on_the_unified_view(tmp_path):
    from su_multi_arm.compat.legacy_qa.envelope import content_source
    from su_multi_arm.compat.legacy_qa.profile import DatasetProfile

    unified = DatasetProfile.model_validate({"id": "support-qa", "content_source": {"ticket": "Tickets"}, "unified": {"enabled": True, "config": str(tmp_path / "u.yaml")}})
    assert content_source(unified, "logs") == "Logs" and content_source(unified, "ticket") == "Tickets" and content_source(unified, "other") == "Other"
    named = unified.model_copy(update={"content_source": {"logs": "Platform logs"}})
    assert content_source(named, "logs") == "Platform logs"  # the profile always wins
    plain = DatasetProfile.model_validate({"id": "support-qa"})
    assert content_source(plain, "logs") == "Other"  # a dataset not served from the unified view is unchanged
