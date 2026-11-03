"""``record_context.record_id`` is resolved through the caller's access scope (release review B1).

The canonical record shown to the planner and the generator is read through the same ACL-scoped
catalog path as ``documents_in``. An id that is unknown or outside the principal's scope is dropped:
it never reaches the planner or generator payload, and it is reported in the trace and as an
``unsupported_constraints`` entry (``value_not_observed``) without revealing which of the two cases
applied. Synthetic support-example corpus; the planner/generator are monkeypatched fakes."""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.models import Answer
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.acl import Principal, StaticPolicy

from tests.contract.test_engine_support_example import FAKE, plan

OUT_OF_SCOPE = "case:certificate"  # source=case
IN_SCOPE = "kb:certificate"  # source=article


def scoped_engine(config, documents):
    data = config.model_dump(mode="json")
    data["pipeline"]["decision"] = {"kind": "llm"}
    data["models"]["roles"]["planner"] = FAKE.model_dump(mode="json")
    data["models"]["roles"]["generator"] = FAKE.model_dump(mode="json")
    engine = Engine(validate_config(data), acl=StaticPolicy(field="source", attribute="tenant"))
    engine.ingest(documents)
    return engine


@pytest.fixture
def recorded(monkeypatch):
    seen: dict[str, list] = {"planner": [], "generator": []}

    def respond(self, system, payload, schema, budget, stage):
        seen[stage].append(payload)
        if stage == "planner":
            return plan({"arm": "lexical", "query": "certificate rounding"})
        cited = payload["evidence"][0]["id"]
        return Answer(answer=f"See [{cited}].", citations=[cited])

    monkeypatch.setattr(JsonModel, "call", respond)
    return seen


def leaked(payload, record_id, secret_fragments):
    text = json.dumps(payload)
    return record_id in text or any(fragment in text for fragment in secret_fragments)


SECRETS = ["renewed certificate", "stopped applying in California"]  # case:certificate description/resolution


def test_out_of_scope_record_never_reaches_planner_or_generator(config, documents, recorded):
    engine = scoped_engine(config, documents)
    who = Principal(id="u", tenant="article", scopes=["search", "answer"])
    request = SearchRequest(query="similar", record_context={"record_id": OUT_OF_SCOPE})
    result = engine.answer(request, principal=who)
    assert all(h.source == "article" for h in result.retrieval.hits)
    planner, generator = recorded["planner"][0], recorded["generator"][0]
    assert planner["canonical_record"] is None
    assert not leaked(planner, OUT_OF_SCOPE, SECRETS)
    assert "canonical_record" not in generator["record_context"]
    assert not leaked(generator, OUT_OF_SCOPE, SECRETS)
    reported = [u for u in result.unsupported_constraints if u.constraint_text == OUT_OF_SCOPE]
    assert len(reported) == 1 and reported[0].reason == "value_not_observed"
    assert result.trace["record_context"] == {"record_id": "unresolved", "used": False}
    search = engine.search(request, principal=who)
    assert search.trace["record_context"]["used"] is False
    assert any(u.constraint_text == OUT_OF_SCOPE for u in search.unsupported_constraints)


def test_unknown_record_is_reported_the_same_way_as_out_of_scope(config, documents, recorded):
    engine = scoped_engine(config, documents)
    who = Principal(id="u", tenant="article", scopes=["search"])
    unknown = engine.search(SearchRequest(query="similar", record_context={"record_id": "case:nope"}), principal=who)
    hidden = engine.search(SearchRequest(query="similar", record_context={"record_id": OUT_OF_SCOPE}), principal=who)
    assert recorded["planner"][0]["canonical_record"] is None and recorded["planner"][1]["canonical_record"] is None
    assert unknown.trace["record_context"] == hidden.trace["record_context"]
    shape = lambda r: [(u.reason, u.suggestion) for u in r.unsupported_constraints]  # noqa: E731
    assert shape(unknown) == shape(hidden)


def test_in_scope_record_is_resolved_for_planner_and_generator(config, documents, recorded):
    engine = scoped_engine(config, documents)
    who = Principal(id="u", tenant="article", scopes=["search", "answer"])
    result = engine.answer(SearchRequest(query="similar", record_context={"record_id": IN_SCOPE}), principal=who)
    assert recorded["planner"][0]["canonical_record"]["record_id"] == IN_SCOPE
    assert recorded["generator"][0]["record_context"]["canonical_record"]["record_id"] == IN_SCOPE
    assert "record_context" not in result.trace and result.unsupported_constraints == []
