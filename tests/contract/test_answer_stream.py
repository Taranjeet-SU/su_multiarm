"""``Engine.answer_stream`` (plan §3.15, execution-log default #12): typed events in a fixed order,
provisional token deltas only from a role that declares streaming, the ``AnswerContract`` applied to
the final answer even after tokens streamed, the same ``AnswerResult`` as ``Engine.answer``,
cancellation that stops the run and its budget, and failures as a terminal ``error`` event.
Synthetic corpus; the generator is the loopback ``FakeChatServer`` (contract-mocked)."""

from __future__ import annotations

import json

import pytest

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.errors import RunCancelled
from su_multi_arm.models import Answer, Claim
from su_multi_arm.pipeline.spec import RequestRejected
from su_multi_arm.providers import JsonModel
from su_multi_arm.runtime.budget import Budget, BudgetLimits
from su_multi_arm.runtime.events import ANSWER_EVENT, ContractEvent, DoneEvent, ErrorEvent, TokenEvent, error_category
from su_multi_arm.testing.provider_fakes import FakeChatServer, chunks, synthetic_answer

from serving_support import DOCS, REQUEST, memory_config, normalise


@pytest.fixture
def fake():
    with FakeChatServer() as server:
        yield server


def engine_with(generator, **overrides):
    engine = Engine(memory_config(generator, **overrides))
    engine.ingest(DOCS)
    return engine


def names(events):
    return [e.event for e in events]


def test_event_order_and_final_result_equals_the_non_streaming_answer(fake):
    engine = engine_with(fake.role(streaming=True))
    request = SearchRequest.model_validate(REQUEST)
    events = list(engine.answer_stream(request))
    order = names(events)
    assert order[0] == "decision" and order[-1] == "done" and order[-2] == "contract"
    first_arm, evidence = order.index("arm_result"), order.index("evidence")
    first_token = order.index("token")
    assert first_arm < evidence < first_token and set(order[first_token:-2]) == {"token"}
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    tokens = [e for e in events if isinstance(e, TokenEvent)]
    assert all(t.provisional for t in tokens) and json.loads("".join(t.text for t in tokens))["citations"]  # raw reply, provisional
    contract = events[-2]
    assert isinstance(contract, ContractEvent) and contract.status == "passed" and contract.tokens_streamed == len(tokens) and contract.tokens_superseded
    streamed = events[-1].result
    plain = engine.answer(request)
    assert normalise(streamed.model_dump(mode="json")) == normalise(plain.model_dump(mode="json"))
    assert streamed.answer.citations and "r2" not in [h.document_id for h in streamed.retrieval.hits]
    assert fake.requests[0]["stream"] is True and "stream" not in fake.requests[1]
    for event in events:  # every event round-trips through the typed union (wire contract)
        assert ANSWER_EVENT.validate_python(event.model_dump(mode="json")) == event


def test_role_without_declared_streaming_and_provider_none_emit_no_deltas(fake):
    engine = engine_with(fake.role(streaming=False))
    events = list(engine.answer_stream(SearchRequest.model_validate(REQUEST)))
    assert "token" not in names(events) and events[-1].result.answer.citations
    assert events[-2].tokens_streamed == 0 and not events[-2].tokens_superseded
    assert "stream" not in fake.requests[0]
    disabled = engine_with(None)  # no generator role: provider none
    events = list(disabled.answer_stream(SearchRequest.model_validate(REQUEST)))
    assert "token" not in names(events) and events[-1].result.answer.insufficient_evidence


def test_contract_violation_is_representable_after_tokens_streamed(fake, monkeypatch):
    def fabricate(self, system, payload, schema, budget, stage, on_delta):
        first = payload["evidence"][0]["id"]
        for part in ("There are ", "7 matching ", "records."):
            on_delta(part)
        return Answer(answer="There are 7 matching records.", claims=[Claim(text="7 records", kind="count", evidence_ids=[first])], citations=[first])

    monkeypatch.setattr(JsonModel, "call_stream", fabricate)
    engine = engine_with(fake.role(streaming=True))
    events = list(engine.answer_stream(SearchRequest.model_validate(REQUEST)))
    tokens = [e.text for e in events if isinstance(e, TokenEvent)]
    assert "".join(tokens) == "There are 7 matching records."
    contract = next(e for e in events if isinstance(e, ContractEvent))
    assert contract.status == "abstained" and contract.violations and contract.tokens_superseded
    final = events[-1].result
    assert final.contract == "abstained" and final.answer.insufficient_evidence and "7" not in final.answer.answer


def test_request_problems_raise_before_any_event(fake):
    engine = engine_with(fake.role())
    with pytest.raises(RequestRejected):
        engine.answer_stream(SearchRequest(query="x", pipeline="unknown"))
    with pytest.raises(ValueError):
        engine.answer_stream(SearchRequest(query="x", record_context={"undeclared": "1"}))
    assert fake.requests == []


def test_cancel_stops_the_run_closes_the_provider_stream_and_cancels_the_budget():
    with FakeChatServer(delay=0.05, chunk_size=4) as fake:
        engine = engine_with(fake.role(streaming=True))
        stream = engine.answer_stream(SearchRequest.model_validate(REQUEST))
        captured = {}
        original = stream._cancel_budget

        def spy(reason):
            captured["reason"] = reason
            original(reason)

        stream._cancel_budget = spy
        seen = []
        for event in stream:
            seen.append(event)
            if isinstance(event, TokenEvent):
                stream.cancel()
                break
        rest = []
        while not stream.finished:
            event = stream.next_event(5)
            if event is not None:
                rest.append(event)
        assert isinstance(rest[-1], ErrorEvent) and rest[-1].category == "cancelled"
        assert not any(isinstance(e, DoneEvent) for e in rest) and captured["reason"] == "cancelled"
        assert stream.worker_done.wait(5) and stream.cancelled
        total = len(chunks(json.dumps(synthetic_answer({"evidence": [{"id": "r1"}]})), 4))
        assert fake.disconnected.wait(5)  # the provider stream was closed by the client side
        assert fake.chunks_sent < total  # the reply was not read to the end


def test_budget_cancel_refuses_every_further_reservation():
    budget = Budget(BudgetLimits())
    budget.reserve("calls", 1)
    budget.cancel("client_disconnected")
    with pytest.raises(RunCancelled):
        budget.reserve("calls", 1)
    with pytest.raises(RunCancelled):
        budget.reserve_call(stage="generator")
    assert budget.snapshot()["cancelled"] == "client_disconnected"


def test_provider_failure_ends_the_stream_with_an_error_event(fake):
    role = {**fake.role(), "base_url": "http://127.0.0.1:9/v1"}  # nothing listens there
    engine = engine_with(role)
    events = list(engine.answer_stream(SearchRequest.model_validate(REQUEST)))
    assert names(events)[-1] == "error" and events[-1].category == "provider_connection"
    assert "done" not in names(events) and "contract" not in names(events)


def test_streamed_turn_is_recorded_on_the_session(fake):
    engine = engine_with(fake.role())
    session_id = engine.create_session()
    result = engine.answer_stream(SearchRequest.model_validate(REQUEST), session_id=session_id).result()
    record = engine.session(session_id).record
    assert result.trace["session"]["id"] == session_id and len(record.turns) == 1
    assert record.turns[0].shown_ids == [h.document_id for h in result.retrieval.hits]


def test_error_categories_are_stable():
    from su_multi_arm.core.errors import BudgetExhausted, SessionNotFound
    from su_multi_arm.providers import ProviderTimeout, SchemaViolation

    assert error_category(RunCancelled("x")) == "cancelled"
    assert error_category(BudgetExhausted("calls", 1, 0)) == "budget_exhausted"
    assert error_category(SessionNotFound("s")) == "session_not_found"
    assert error_category(ProviderTimeout("t")) == "provider_timeout"
    assert error_category(SchemaViolation("bad", raw="{}")) == "schema_violation"
    assert error_category(RequestRejected("no")) == "rejected"
    assert error_category(KeyError("k")) == "internal"
