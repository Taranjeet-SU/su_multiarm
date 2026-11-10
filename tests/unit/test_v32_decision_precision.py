"""V3.2 decision precision (synthetic; no engine, no provider).

* Unsolicited structured steps: an ``llm`` planner scope on a question that asks no aggregate (and
  carries no caller aggregate/scope) is dropped and recorded (``Decision.dropped_steps``, the
  ``planner_steps_dropped`` trace event); if nothing remains the plan is the original query on the
  enabled lexical/dense arms (``Decision.fallback``). Requested aggregates keep the old behaviour.
* Field-qualifier precision: a word after a ``string``/``id`` field label is a constraint only when
  bound to the field (explicit operator, identifier-shaped token, or a value the resolver knows).
* Negation: a negated value ("not a report") is a negated constraint, never a positive filter.
"""

from __future__ import annotations

import pytest

from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.types import Measure, MetadataSchema, ScopeSpec
from su_multi_arm.decision import Decision, DecisionRequest, DroppedStep, LlmAgent, LlmConfig, LlmPlan
from su_multi_arm.decision.base import Capabilities
from su_multi_arm.decision.intent import detect_intents, mentions_aggregate_operator
from su_multi_arm.decision.qualifiers import qualifier_candidates, unapplied
from su_multi_arm.runtime.context import new_context

from conftest import V2_GEN  # tests/conftest.py

ROLE = {"provider": "openai_compatible", "model": "fake"}
COUNT_REPORTS = {"arm": "structured", "scope": {"predicates": [{"field": "kind", "op": "eq", "value": "report"}], "measure": {"kind": "count"}}}
LIST_UNKNOWN = {"arm": "structured", "scope": {"predicates": [{"field": "project", "op": "eq", "value": "NoSuchProject"}], "limit": 5}}


def _agent(monkeypatch, v2_backend, steps):
    agent = LlmAgent(LlmConfig(role=ROLE), Dependencies(backends={"records": v2_backend.records}))
    monkeypatch.setattr(LlmAgent, "call_model", lambda self, ctx, payload: LlmPlan.model_validate({"steps": steps}))
    return agent


def _decide(agent, caps, question, **request):
    ctx = new_context(V2_GEN)
    decision = agent.decide(ctx, DecisionRequest(query=question, **request), detect_intents(question, [], caps.structured_targets()), caps)
    events = [e for s in ctx.trace.spans for e in s.events if e["name"] == "planner_steps_dropped"]
    span = next(s for s in ctx.trace.spans if s.name == "decision")
    return decision, events, span.attributes


# --- unsolicited structured steps -----------------------------------------------------------------


def test_a_lone_unsolicited_structured_step_falls_back_to_lexical_and_dense(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [COUNT_REPORTS])
    question = "How do I rotate the signing key on the staging gateway?"
    decision, events, span = _decide(agent, v2_caps, question, filters={"project": "Alpha"})
    assert decision.outcome == "decided" and decision.unsupported == [] and decision.scope is None
    assert decision.plan.arms() == ["dense", "lexical"]  # capability order; graph/community are not fallback arms
    assert all(s.query == question and s.filters == {} for s in decision.plan.steps)  # engine applies request filters
    assert decision.fallback == "retrieval_after_dropped_steps"
    assert [(d.arm, d.reason) for d in decision.dropped_steps] == [("structured", "unsolicited_structured")]
    assert "count" in decision.dropped_steps[0].detail and "kind" in decision.dropped_steps[0].detail
    assert "report" not in decision.dropped_steps[0].detail  # field names only, never values
    assert events and events[0]["fallback"] == "retrieval_after_dropped_steps" and events[0]["count"] == 1
    assert span["planner_fallback"] == "retrieval_after_dropped_steps" and span["decision_fallback"] is False
    assert span["dropped_steps"][0]["reason"] == "unsolicited_structured"


def test_planner_retrieval_steps_are_kept_and_no_fallback_is_needed(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [{"arm": "lexical", "query": "signing key rotation"}, COUNT_REPORTS])
    decision, events, _ = _decide(agent, v2_caps, "Why does the signing key rotation fail on staging?")
    assert decision.plan.arms() == ["lexical"] and decision.plan.steps[0].query == "signing key rotation"
    assert decision.fallback is None and len(decision.dropped_steps) == 1 and events[0]["fallback"] is None


def test_an_unsolicited_step_is_dropped_before_compilation(monkeypatch, v2_backend, v2_caps):
    # an unresolvable value in an invented scope is not a reason to refuse a lookup question
    agent = _agent(monkeypatch, v2_backend, [LIST_UNKNOWN])
    decision, _, _ = _decide(agent, v2_caps, "Where is the retry setting documented?")
    assert decision.outcome == "decided" and decision.failure is None and decision.fallback == "retrieval_after_dropped_steps"
    assert "list" in decision.dropped_steps[0].detail


def test_no_lexical_or_dense_arm_refuses_instead_of_inventing_arms(monkeypatch, v2_backend, v2_schema):
    caps = Capabilities(arms=["structured", "graph"], metadata_schema=v2_schema)
    agent = _agent(monkeypatch, v2_backend, [COUNT_REPORTS])
    decision, _, _ = _decide(agent, caps, "Where is the retry setting documented?")
    assert decision.outcome == "abstain" and decision.failure == "invalid_step"


@pytest.mark.parametrize(
    "question",
    [
        "How many reports are there?",  # targeted pre-pass intent
        "Count reports for project Alpha",  # operator word without a bound target
        "List every report",
        "numbers by component please",
    ],
)
def test_requested_aggregates_keep_their_structured_step(monkeypatch, v2_backend, v2_caps, question):
    agent = _agent(monkeypatch, v2_backend, [COUNT_REPORTS])
    decision, events, _ = _decide(agent, v2_caps, question)
    assert decision.plan is not None and decision.plan.structured_steps() and decision.dropped_steps == [] and events == []


def test_a_caller_typed_aggregate_keeps_the_structured_step(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [COUNT_REPORTS])
    decision, _, _ = _decide(agent, v2_caps, "reports please", aggregate=Measure(kind="count"))
    assert decision.plan.structured_steps() and decision.dropped_steps == []


def test_a_requested_aggregate_with_an_unapplied_qualifier_is_still_bounded(monkeypatch, v2_backend, v2_caps):
    agent = _agent(monkeypatch, v2_backend, [{"arm": "structured", "scope": {"measure": {"kind": "count"}}}])
    decision, _, _ = _decide(agent, v2_caps, "How many Beta records are open?")
    assert {u.constraint_text for u in decision.unsupported} >= {"Beta", "open"}


def test_operator_words_and_decision_coherence():
    assert mentions_aggregate_operator("Compare March and April")
    assert not mentions_aggregate_operator("Why does the service restart every night after the first deploy?")
    with pytest.raises(ValueError):
        Decision(outcome="abstain", agent="llm", policy_applied="llm", fallback="retrieval_after_dropped_steps", dropped_steps=[DroppedStep(arm="structured", reason="unsolicited_structured", detail="x")])


# --- field-qualifier precision --------------------------------------------------------------------

SCHEMA = MetadataSchema.model_validate(
    {
        "canonical_unit": "entry",
        "fields": [
            {"name": "source", "type": "string"},
            {"name": "service", "type": "string"},
            {"name": "build_ref", "type": "id"},
            {"name": "kind", "type": "enum", "enum_values": ["report", "note"]},
        ],
    }
)


class KnownValues:
    """Resolver stub: no listable universe; ``RecordStore.exists`` confirms these values."""

    def __init__(self, known: dict[str, set[str]]):
        self.known = known

    def observed(self, field):
        return None

    def known_value(self, field, value):
        return value in self.known.get(field, set())


def fields_of(question, resolver=None):
    return [(c.field, c.value) for c in qualifier_candidates(question, SCHEMA, resolver, detect_intents(question)) if c.kind == "field"]


@pytest.mark.parametrize(
    "question",
    [
        "How many entries mention the source association step?",
        "How many entries list source versions in the changelog?",
        "How many entries describe service authentication failures?",
        "How many entries say the service is healthy after restart?",
    ],
)
def test_prose_after_a_field_label_is_not_a_constraint(question):
    assert fields_of(question) == []
    assert fields_of(question, KnownValues({"service": {"gateway"}, "source": {"mirror"}})) == []


@pytest.mark.parametrize(
    "question,expected",
    [
        ("How many entries have service = authentication?", ("service", "authentication")),
        ("How many entries have source: mirror?", ("source", "mirror")),
        ("How many entries have build ref BR-17?", ("build_ref", "BR-17")),  # identifier-shaped
        ("How many entries came from source v2?", ("source", "v2")),
    ],
)
def test_bound_field_values_stay_constraints(question, expected):
    assert fields_of(question) == [expected]


def test_a_value_the_store_confirms_is_a_constraint():
    resolver = KnownValues({"service": {"gateway"}})
    assert fields_of("How many entries for service gateway?", resolver) == [("service", "gateway")]
    assert fields_of("How many entries for service gateway?") == []


# --- negation -------------------------------------------------------------------------------------


def _missing(question, predicates=(), filters=None):
    candidates = qualifier_candidates(question, SCHEMA, None, detect_intents(question))
    scope = ScopeSpec.model_validate({"predicates": list(predicates), "measure": {"kind": "count"}})
    return candidates, [u.constraint_text for u in unapplied(candidates, scope, filters or {}, SCHEMA)]


@pytest.mark.parametrize("question,text", [
    ("How many entries are not a report?", "not a report"),
    ("How many non-report entries exist?", "non-report"),
    ("How many entries that aren't a report mention retries?", "aren't a report"),
    ("How many entries other than report ones mention retries?", "other than report"),
])  # fmt: skip
def test_a_negated_value_is_a_negated_constraint(question, text):
    candidates, missing = _missing(question)
    negated = [c for c in candidates if c.field == "kind"]
    assert negated and all(c.negated for c in negated) and missing == [text]
    # a positive filter on the excluded value never applies it (and a request filter is positive)
    assert _missing(question, [{"field": "kind", "op": "eq", "value": "report"}])[1] == [text]
    assert _missing(question, filters={"kind": "report"})[1] == [text]
    # the negated predicate does
    assert _missing(question, [{"field": "kind", "op": "eq", "value": "report", "negate": True}])[1] == []
    assert _missing(question, [{"field": "kind", "op": "in", "value": ["report"], "negate": True}])[1] == []


def test_a_positive_value_is_not_applied_by_a_negated_predicate_and_no_is_the_absence_operator():
    assert _missing("How many report entries mention retries?", [{"field": "kind", "op": "eq", "value": "report", "negate": True}])[1] == ["report"]
    candidates, missing = _missing("Are there no report entries for the gateway?", [{"field": "kind", "op": "eq", "value": "report"}])
    assert [c.negated for c in candidates if c.field == "kind"] == [False] and missing == []
