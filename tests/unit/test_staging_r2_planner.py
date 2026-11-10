"""Staging round 2 findings of 2026-11-10 on the planner side (synthetic regressions).

* N1: the log scope guard passes a knowledge-half structured step whose executed source scope
  admits no log source (a support aggregate keeps today's plan); steps that can read the log
  member are still refused without one kind; one store (no ``collection``) is guarded as before;
* N2: an operations half needs operations evidence in the question (``operations_terms`` or a
  failure symptom); "how many are new?" and "mentions increase" are not evidence;
* N6: the worst-case spend check does not count the logs planner context under the two-halves
  planner (it is never sent there) and counts the source cards instead;
* vocabulary: ``match: word`` is whole-word ("report" never matches "reported"); ``prefix`` is the
  declared opt-in to continuations.

Synthetic configurations and values only; no provider call.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from su_multi_arm.core.config_collection import CollectionConfig
from su_multi_arm.core.config_crosssource import OPERATIONS_TERMS, PlannerConfig
from su_multi_arm.core.config_m0 import VocabularyTerm
from su_multi_arm.core.types import GenerationId, Predicate, ScopeSpec
from su_multi_arm.decision.base import Capabilities, Decision, DecisionRequest, Plan, RetrievalStep, StructuredStep
from su_multi_arm.decision.log_guard import LogGuard, admitted_sources, guard_for, log_scope_guard, log_sources
from su_multi_arm.decision.log_intents import LogIntentCall, operations_evidence, term_pattern
from su_multi_arm.decision.log_question import ground_terms
from su_multi_arm.decision.two_halves import TwoHalvesPlanner
from su_multi_arm.runtime.context import new_context

SUPPORT = {"kind": "support", "sources": ["ticket", "doc"], "record_kinds": ["ticket", "doc"], "id_namespaces": ["ticket:", "https://docs.example.org/"]}
LOGS = {"kind": "logs", "sources": ["logs"], "record_kinds": ["incident", "summary_coverage"], "id_namespaces": ["logs:"], "cadence": "minutes", "producer": {"kind": "log_host"}}
GEN = GenerationId(dataset="synthetic", generation="c" * 32, index_format=3, fingerprint="f" * 64)

# the staging repro's shape: a support aggregate pinned to the knowledge sources, naming no kind
KNOWLEDGE_AGGREGATE = ScopeSpec(predicates=[Predicate(field="source", op="in", value=["ticket", "doc"]), Predicate(field="status", op="eq", value="solved")])
KINDLESS = ScopeSpec(predicates=[Predicate(field="status", op="eq", value="solved")])


def collection(**members) -> CollectionConfig:
    return CollectionConfig.model_validate({"mode": "partitioned", "members": members})


def unified_config(*, scope: bool = True, logs: bool = True):
    members = {"support": SUPPORT, **({"logs": LOGS} if logs else {})}
    planner = PlannerConfig(enabled=True, scope_knowledge_steps=scope)
    intents = SimpleNamespace(enabled=True)
    return SimpleNamespace(collection=collection(**members), crosssource=SimpleNamespace(planner=planner),
                           logs=SimpleNamespace(intents=intents, serving=SimpleNamespace(allow_adhoc_scopes=False)))


# --- N1: the log scope guard ---------------------------------------------------------------------


def test_the_staging_repro_passes_when_the_log_sources_are_declared():
    assert log_scope_guard(KNOWLEDGE_AGGREGATE, log_sources={"logs"}) is None
    # without declared log sources (one store) the source does not separate log records: as before
    refused = log_scope_guard(KNOWLEDGE_AGGREGATE)
    assert refused is not None and refused.reason == "intent_required"


def test_the_executed_context_scopes_a_kindless_step():
    knowledge = Predicate(field="source", op="in", value=["ticket", "doc"])
    assert log_scope_guard(KINDLESS, context=[knowledge], log_sources={"logs"}) is None
    assert log_scope_guard(KINDLESS, log_sources={"logs"}).reason == "intent_required"  # nothing restricts the source


@pytest.mark.parametrize("predicates", [
    [Predicate(field="source", op="eq", value="logs")],
    [Predicate(field="source", op="in", value=["ticket", "logs"])],
    [Predicate(field="source", op="eq", value="ticket", negate=True)],  # a negation admits every other source
    [Predicate(field="source", op="missing", value=None)],
])
def test_a_scope_that_can_read_the_log_member_is_still_guarded(predicates):
    refused = log_scope_guard(ScopeSpec(predicates=predicates), log_sources={"logs"})
    assert refused is not None and refused.reason == "intent_required"


def test_one_log_kind_still_passes_and_several_are_refused():
    one = ScopeSpec(predicates=[Predicate(field="source", op="eq", value="logs"), Predicate(field="kind", op="eq", value="incident")])
    assert log_scope_guard(one, log_sources={"logs"}) is None
    several = ScopeSpec(predicates=[Predicate(field="kind", op="in", value=["incident", "log_day"])])
    assert log_scope_guard(several, log_sources={"logs"}).reason == "intent_required"


def test_admitted_sources_intersects_positive_conditions():
    assert admitted_sources([]) is None
    assert admitted_sources([Predicate(field="source", op="in", value=["ticket", "doc"]), Predicate(field="source", op="eq", value="doc")]) == {"doc"}
    assert admitted_sources([Predicate(field="source", op="eq", value="ticket", negate=True)]) is None


def test_log_sources_are_the_members_that_can_hold_log_records():
    assert log_sources(SimpleNamespace(collection=None)) is None
    assert log_sources(SimpleNamespace(collection=collection(support=SUPPORT, logs=LOGS))) == {"logs"}
    # a member declaring a log kind is a log source whoever produces it
    archive = {"kind": "archive", "sources": ["archive"], "record_kinds": ["incident"], "id_namespaces": ["archive:"]}
    assert log_sources(SimpleNamespace(collection=collection(support=SUPPORT, archive=archive))) == {"archive"}
    assert log_sources(SimpleNamespace(collection=collection(support=SUPPORT))) == frozenset()


def test_the_engine_component_carries_the_log_sources():
    guard = guard_for(unified_config())
    assert isinstance(guard, LogGuard) and guard.log_sources == {"logs"}
    off = unified_config()
    off.logs.serving.allow_adhoc_scopes = True
    assert guard_for(off) is None


def planner_of(config) -> TwoHalvesPlanner:
    return TwoHalvesPlanner(SimpleNamespace(config=config), config.crosssource.planner)


def guarded(planner: TwoHalvesPlanner, step: StructuredStep, filters: dict | None = None) -> Decision:
    ctx = new_context(GEN)
    request = DecisionRequest(query="How many solved tickets are there?", filters=filters or {})
    capabilities = Capabilities(arms=["lexical", "dense", "structured"], log_intents=True)
    decision = Decision(plan=Plan(steps=[step]), scope=step.scope, outcome="decided", agent="llm", policy_applied="llm")
    return planner._guard(ctx, request, capabilities, decision, inner=None)


def test_a_knowledge_half_support_aggregate_keeps_its_plan():
    planner = planner_of(unified_config())
    for scope in (KINDLESS, KNOWLEDGE_AGGREGATE, ScopeSpec()):
        step = StructuredStep(arm="structured", scope=scope)
        assert guarded(planner, step).outcome == "decided", scope


def test_a_step_naming_the_log_source_without_a_kind_is_refused():
    planner = planner_of(unified_config())
    step = StructuredStep(arm="structured", scope=ScopeSpec(predicates=[Predicate(field="source", op="eq", value="logs")]))
    decision = guarded(planner, step)
    assert decision.outcome == "abstain" and decision.failure == "invalid_step"
    assert [u.reason for u in decision.unsupported] == ["intent_required"]


def test_without_the_knowledge_scope_a_kindless_step_is_refused_as_before():
    planner = planner_of(unified_config(scope=False))
    assert guarded(planner, StructuredStep(arm="structured", scope=KINDLESS)).outcome == "abstain"
    # the step's own source condition still separates it from the log member
    assert guarded(planner, StructuredStep(arm="structured", scope=KNOWLEDGE_AGGREGATE)).outcome == "decided"
    # so does a request filter on a knowledge source
    assert guarded(planner, StructuredStep(arm="structured", scope=KINDLESS), {"source": "ticket"}).outcome == "decided"


def test_typed_log_intents_are_never_guarded():
    planner = planner_of(unified_config())
    step = StructuredStep(arm="structured", scope=ScopeSpec(), intent=LogIntentCall(intent="log_top", args={"by": "service"}))
    assert guarded(planner, step).outcome == "decided"


def test_the_knowledge_half_lists_the_knowledge_sources_values_only():
    planner = planner_of(unified_config())
    caps = planner.knowledge_capabilities(Capabilities(arms=["lexical", "structured"], log_intents=True), None)
    assert caps.value_scope == [Predicate(field="source", op="in", value=["ticket", "doc"])]
    assert "value_scope" not in caps.model_dump()  # never serialised into a payload
    # no log member: nothing to scope, today's capabilities
    alone = planner_of(unified_config(logs=False))
    assert alone.knowledge_capabilities(Capabilities(arms=["lexical"], log_intents=True), None).value_scope == []


# --- N2: operations evidence in the question ----------------------------------------------------


@pytest.mark.parametrize("question", [
    "How many are new?",
    "How many tickets are new this month?",
    "Did Acme portal mentions increase compared to last week?",
    "Which reported tickets mention the dashboard?",
    "How do I configure a new search tab?",
])
def test_generic_words_are_not_operations_evidence(question):
    assert operations_evidence(question, OPERATIONS_TERMS) == []


@pytest.mark.parametrize("question,expected", [
    ("Has the crawl halted for all tenants?", "crawl"),
    ("Users get unauthorized errors from the connector", "errors"),
    ("Why is the report stuck in the queue for 3 hours?", "stuck"),
    ("Are there new errors in the logs today?", "logs"),
    ("Did the service have an outage last night?", "service"),
])
def test_operations_words_and_symptoms_are_evidence(question, expected):
    assert expected in operations_evidence(question, OPERATIONS_TERMS)


def test_the_terms_are_configured_and_whole_word():
    assert operations_evidence("Did portal mentions increase?", ["portal"]) == ["portal"]
    assert operations_evidence("Did the portals increase?", ["portal"]) == []  # no prefix matching
    assert operations_evidence("Is the crawl slow?", []) == ["slow"]  # an empty list leaves the symptoms
    assert operations_evidence("Any stack-trace in the job?", ["stack trace"]) == ["stack trace"]


def test_operations_terms_are_validated():
    assert PlannerConfig().operations_terms == list(OPERATIONS_TERMS)
    with pytest.raises(ValidationError, match="lower-case"):
        PlannerConfig(operations_terms=["Logs"])
    with pytest.raises(ValidationError, match="unique"):
        PlannerConfig(operations_terms=["logs", "logs"])


# --- vocabulary: whole-word matching -----------------------------------------------------------


def test_a_word_term_never_matches_a_longer_word():
    term = VocabularyTerm(term="report", values=["report-worker"])
    assert term.match == "word"
    assert term.pattern().search("The crawl failed and was reported at 2") is None
    assert term.pattern().search("Is the Report queued?") is not None
    assert term.pattern().search("report-worker logs") is not None  # a hyphen is a word boundary
    grounding = ground_terms("The crawl failed and was reported at 2", [term], ["report-worker"])
    assert grounding.services == [] and grounding.matched == []


def test_prefix_is_the_declared_opt_in_to_continuations():
    term = VocabularyTerm(term="report", match="prefix", values=["report-worker"])
    assert term.pattern().search("it was reported") is not None
    assert term_pattern("report").search("it was reported") is None


# --- N6: the worst-case spend check under the two-halves planner ---------------------------------


def _priced(tmp_path, *, max_chars: int, two_halves: bool):
    from su_multi_arm.core.config import validate_config
    from tests import two_halves_support as th

    data = th.unified_config(tmp_path, llm=True).model_dump(mode="json", exclude_none=True)
    prices = tmp_path / "prices.yaml"
    prices.write_text(yaml.safe_dump({"version": "synthetic-1", "prices": {m: {"input_per_million": 3.0, "output_per_million": 15.0} for m in ("fake-planner", "fake-generator")}}))
    data["budgets"] = {**data.get("budgets", {}), "price_table": str(prices), "max_usd": 0.25, "plan_check": "warn"}
    data["logs"]["intents"].update(planner_context=True, planner_context_max_chars=max_chars)
    data["crosssource"]["planner"]["enabled"] = two_halves
    return validate_config(data), prices


def test_the_logs_planner_context_is_not_counted_under_the_two_halves_planner(tmp_path):
    from su_multi_arm.runtime.budget import load_price_table
    from su_multi_arm.runtime.plan_cost import slots_for

    def planner_tokens(max_chars: int, two_halves: bool) -> int:
        cfg, prices = _priced(tmp_path, max_chars=max_chars, two_halves=two_halves)
        slots, _, _ = slots_for(cfg, load_price_table(prices))
        return next(s.input_tokens for s in slots if s.stage == "planner")

    # the logs planner context is sent without the two-halves planner: its bound is counted
    assert planner_tokens(200_000, False) - planner_tokens(2_000, False) == pytest.approx(198_000 / 3, abs=2)
    # under the two-halves planner it is never sent: the bound changes nothing
    assert planner_tokens(200_000, True) == planner_tokens(2_000, True)
    # the source cards and the operations-half allowance are counted instead (smaller than 24k chars)
    from su_multi_arm.runtime.plan_cost import OPERATIONS_HALF_CHARS

    assert planner_tokens(24_000, True) < planner_tokens(24_000, False)
    assert planner_tokens(2_000, True) * 3 > OPERATIONS_HALF_CHARS * 8


def test_the_two_halves_planner_payload_never_carries_the_log_context():
    planner = planner_of(unified_config())
    built = []
    caps = Capabilities(arms=["lexical", "structured"], log_intents=True, log_context=lambda: built.append(1) or {"x": 1})
    with_ops = SimpleNamespace(steps=[RetrievalStep(arm="lexical", query="q")], calls=lambda: [], notes=[])
    planner._static_cards["lexical,structured"] = {"version": "v", "sources": []}
    for operations in (None, with_ops):
        knowledge = planner.knowledge_capabilities(caps, operations)
        assert knowledge.log_context is None and not knowledge.log_intents
    assert built == []
