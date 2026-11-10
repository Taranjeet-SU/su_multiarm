"""M0 log-side answers on the synthetic gold pipeline (docs/plans/unified_merge.md §9, §10.1).

* an outage question without a period reads the default view (last 3 days), not today only;
* a bounded incident selection (more incidents than one selection reads) answers with a bounded
  note and a grouped count instead of a 503 refusal;
* a bare clock hour is read in ``question.timezone`` with both 12-hour readings, stated in the
  answer, and the day-level aggregate is ``bounded``;
* a ``question.vocabulary`` term grounds to indexed services; one naming only unindexed services
  refuses the scope instead of answering for every service.
No model is called. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest

from su_multi_arm import SearchRequest
from su_multi_arm.core.types import Scope
from su_multi_arm.decision.log_intents import LogEvidenceScope
from su_multi_arm.providers import JsonModel
from tests.logs_gold_support import AS_OF, build_gold, gold_config

ANCHOR = AS_OF - timedelta(days=4)


@pytest.fixture(scope="module")
def gold(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("m0")
    config = gold_config(tmp)
    config["pipeline"]["arms"]["community"] = {"backend": "local"}
    config["question"] = {
        "timezone": "Asia/Kolkata",
        "vocabulary": {"terms": [
            {"term": "work", "match": "prefix", "values": ["svc-worker"]},
            {"term": "ghost", "values": ["svc-not-indexed"]},
            {"term": "slow", "field": "symptom"},
        ]},
    }
    engine, _, _ = build_gold(tmp, data=config)
    return engine


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("these answers must not call a model")
    monkeypatch.setattr(JsonModel, "call", forbidden)


def reports(result):
    return [r for r in result.retrieval.trace["reports"] if r.get("kind") == "log_investigation_scope"]


def test_an_undated_outage_question_reads_the_default_view(gold):
    result = gold.answer(SearchRequest(query="is there any outage", as_of=ANCHOR.isoformat(), mode="rules"))
    found = reports(result)
    assert found and all(r["start"] == (ANCHOR - timedelta(days=2)).isoformat() and r["end_exclusive"] == (ANCHOR + timedelta(days=1)).isoformat() for r in found)
    assert "No period was named" in result.answer.answer
    assert result.trace["model_calls"] == []


def test_a_bounded_selection_answers_instead_of_refusing(gold):
    from su_multi_arm.runtime.log_investigation import prepare

    class Ops:
        def __getattr__(self, name):
            return getattr(gold, name)

        def log_execute(self, ctx, spec):
            result = gold.log_execute(ctx, spec)
            return result.model_copy(update={"total": 501}) if spec.limit else result

    scope, _, report = prepare(Ops(), gold._context_for(None), LogEvidenceScope(as_of=ANCHOR, window="last 3 days"), Scope(), "outage")
    assert report["bounded"] is True and report["selection_total"] == 501 and report["limited"] is True
    assert any(n.startswith("Bounded selection:") for n in report["notes"])
    assert report["grouped"]["by"] == "tenant" and report["grouped"]["rows"]
    from su_multi_arm.runtime.log_investigation import answer

    text = answer(Ops(), gold._context_for(None), [], [report]).answer
    assert "Bounded selection:" in text and "| Client | Incidents |" in text


def test_a_bare_hour_is_stated_with_both_readings_and_bounds_the_day_counts(gold):
    result = gold.answer(SearchRequest(query="which services had errors at 2", as_of=ANCHOR.isoformat(), mode="rules"))
    flagged = [u for u in result.unsupported_constraints if u.reason == "qualifier_not_applied" and u.constraint_text == "at 2"]
    assert flagged and "Asia/Kolkata" in flagged[0].suggestion and "02:00" in flagged[0].suggestion and "14:00" in flagged[0].suggestion
    assert "hour is not isolated" in result.answer.answer
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["exactness"] == "bounded" and any(u["reason"] == "qualifier_not_applied" for u in aggregate["unsupported"])


def test_a_vocabulary_term_grounds_to_the_indexed_service(gold):
    result = gold.answer(SearchRequest(query="top errors for working jobs yesterday", as_of=ANCHOR.isoformat(), mode="rules"))
    aggregate = result.retrieval.aggregates[0]
    assert aggregate["args"]["service"] == ["svc-worker"]
    assert "read as service svc-worker" in result.answer.answer


def test_a_term_naming_only_unindexed_services_refuses_the_scope(gold):
    result = gold.answer(SearchRequest(query="top errors for ghost jobs yesterday", as_of=ANCHOR.isoformat(), mode="rules"))
    assert result.retrieval.aggregates == [] or result.answer.insufficient_evidence
    assert any("not in the indexed vocabulary" in (u.suggestion or "") for u in result.unsupported_constraints)
