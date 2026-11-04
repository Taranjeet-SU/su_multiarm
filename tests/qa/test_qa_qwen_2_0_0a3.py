"""QA findings from the local-model run (Ollama ``/v1``, a 27B open-weights model) on 2.0.0a2,
fixed in 2.0.0a3 (QQ-01..QQ-06). The replies are replayed offline (``httpx.MockTransport`` with the
reply shapes observed from Ollama 0.33.0, or the answer text the model produced); no network, no
key. Synthetic data only. QQ-05 is a documentation finding (docs/CONFIGURATION.md, local servers).
"""

from __future__ import annotations

import json

import httpx
import pytest

from su_multi_arm.core.config import ChatRole
from su_multi_arm.core.types import ScopeResult
from su_multi_arm.decision.intent import detect_intents
from su_multi_arm.generation import Answer, AnswerContract, Claim
from su_multi_arm.generation.contract import AggregateEvidence
from su_multi_arm.providers import CallBudget, CallContext, ProviderError, ProviderLimitIgnored
from su_multi_arm.providers.json_model import JsonModel

pytestmark = pytest.mark.qa

OLLAMA = "http://127.0.0.1:11434/v1"
TARGETS = ["record", "records", "document", "documents", "product", "state", "status", "priority", "created at", "created", "ticket id"]
REPLY = {"answer": "Rebuilt the synonym table [kb:1].", "claims": [{"text": "Rebuilt the synonym table.", "kind": "narrative", "evidence_ids": ["kb:1"]}], "citations": ["kb:1"]}


def ollama_like(seen: list, completion_tokens: int = 717, reasoning: str | None = None):
    """Ollama 0.33.0 ``/v1`` ignores ``max_completion_tokens`` (finishes with ``stop``) and honours ``max_tokens``."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({k: body.get(k) for k in ("max_tokens", "max_completion_tokens")})
        if body.get("max_tokens") is not None and completion_tokens > body["max_tokens"]:
            usage = {"prompt_tokens": 60, "completion_tokens": body["max_tokens"]}
            return httpx.Response(200, json={"model": "local", "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": ""}}], "usage": usage})
        message = {"role": "assistant", "content": json.dumps(REPLY)}
        if reasoning is not None:
            message["reasoning"] = reasoning
        usage = {"prompt_tokens": 60, "completion_tokens": completion_tokens}
        return httpx.Response(200, json={"model": "local", "choices": [{"index": 0, "finish_reason": "stop", "message": message}], "usage": usage})

    return handler


def local_role(**kw) -> ChatRole:
    return ChatRole(provider="openai_compatible", model="local", base_url=OLLAMA, capabilities={"structured_output": "json_schema"}, **kw)


# --- QQ-01: a reply longer than the role's ceiling is a typed failure -----------------------------


def test_qq01_reply_longer_than_the_role_ceiling_is_a_typed_failure_and_still_metered():
    seen: list = []
    provider = JsonModel(local_role(max_tokens=128), transport=httpx.MockTransport(ollama_like(seen))).provider
    budget = CallBudget(2)
    with pytest.raises(ProviderLimitIgnored, match="717 output tokens, more than max_tokens 128 sent as max_completion_tokens") as raised:
        provider.complete_json("Answer.", {"question": "q", "evidence": []}, Answer, budget, CallContext("generator"))
    assert isinstance(raised.value, ProviderError) and raised.value.category == "output_limit_ignored"
    assert seen == [{"max_tokens": None, "max_completion_tokens": 128}]
    assert budget.calls[0]["error_category"] == "output_limit_ignored" and budget.calls[0]["usage"]["output_tokens"] == 717


def test_qq01_max_tokens_parameter_max_tokens_is_honoured_and_truncation_stays_incomplete():
    seen: list = []
    provider = JsonModel(local_role(max_tokens=128, max_tokens_parameter="max_tokens"), transport=httpx.MockTransport(ollama_like(seen))).provider
    with pytest.raises(ProviderError) as raised:
        provider.complete_json("Answer.", {}, Answer, CallBudget(2), CallContext("generator"))
    assert raised.value.category == "incomplete" and seen == [{"max_tokens": 128, "max_completion_tokens": None}]
    # Within the ceiling the reply is used; the thinking text's size is recorded (QQ-05).
    ok = JsonModel(local_role(max_tokens=1024, max_tokens_parameter="max_tokens"), transport=httpx.MockTransport(ollama_like([], 300, reasoning="x" * 900))).provider
    budget = CallBudget(2)
    reply, usage = ok.complete_json("Answer.", {}, Answer, budget, CallContext("generator"))
    assert reply.citations == ["kb:1"] and usage.output_tokens == 300 and budget.calls[0]["reasoning_chars"] == 900


# --- QQ-02: a numeric filter value echoed in the answer is not a count ----------------------------


def test_qq02_numeric_filter_value_echo_is_not_a_count():
    text = "Yes, there are priority 3 tickets. The corpus contains 61 records matching the priority 3 filter [aggregate:1]."
    question = "Are there any priority 3 tickets?"
    aggregate = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=305, value=61, applied=[{"field": "priority", "op": "eq", "value": 3}]))
    answer = Answer(answer=text, claims=[Claim(text="There are 61 records with priority 3 in this snapshot.", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    result = AnswerContract().enforce(answer, evidence_ids={"aggregate:1"}, aggregates=[aggregate], intents=detect_intents(question, [], TARGETS), unsupported=[], question=question, targets=TARGETS, passages=[])
    assert result.contract == "passed", [v.rule for v in result.violations]
    # A stated count that differs from the aggregate is still refused.
    wrong = answer.model_copy(update={"answer": "There are 3 priority tickets in this snapshot [aggregate:1]."})
    refused = AnswerContract().enforce(wrong, evidence_ids={"aggregate:1"}, aggregates=[aggregate], intents=detect_intents(question, [], TARGETS), question=question, targets=TARGETS)
    assert refused.contract == "abstained" and [v.rule for v in refused.violations] == ["answer_number_mismatch"]


# --- QQ-03: total/sum phrasing with currency or units is a count statement -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "The total amount for project Delta is 4,200 USD.",
        "The sum of the open invoices was $1,250.50 according to the notes.",
        "Project Delta's combined spend comes to 12 thousand euros.",
    ],
)
def test_qq03_unbacked_total_in_answer_text_is_refused_without_a_numeric_field(text):
    question = "What is the total amount for project Delta?"
    intents = detect_intents(question, [], [])  # no numeric field: no aggregate intent
    answer = Answer(answer=text, claims=[Claim(text=text, kind="narrative", evidence_ids=["kb:a"])], citations=["kb:a"])
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a"}, aggregates=[], intents=intents, unsupported=[], question=question, targets=[], passages=["Project Delta kickoff notes."])
    assert result.contract == "abstained" and "answer_text_without_exact_scope" in [v.rule for v in result.violations]


def test_qq03_a_total_backed_by_an_exact_sum_passes_and_a_different_amount_does_not():
    aggregate = AggregateEvidence(id="aggregate:1", result=ScopeResult(exactness="exact", denominator=10, value=4200.0))
    claim = Claim(text="The total is 4200.", kind="count", evidence_ids=["aggregate:1"])
    intents = detect_intents("What is the total amount for Delta?", [], ["amount"])
    ok = Answer(answer="The total amount for Delta is $4,200 [aggregate:1].", claims=[claim], citations=["aggregate:1"])
    assert AnswerContract().enforce(ok, evidence_ids=set(), aggregates=[aggregate], intents=intents, targets=["amount"]).contract == "passed"
    off = ok.model_copy(update={"answer": "The total amount for Delta is $4,900 [aggregate:1]."})
    result = AnswerContract().enforce(off, evidence_ids=set(), aggregates=[aggregate], intents=intents, targets=["amount"])
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["answer_number_mismatch"]


def test_qq03_a_total_quoted_from_a_passage_is_not_a_corpus_count():
    text = "The runbook says the total size limit is 10 MB per upload [kb:a]."
    answer = Answer(answer=text, claims=[Claim(text=text, kind="narrative", evidence_ids=["kb:a"])], citations=["kb:a"])
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a"}, aggregates=[], intents=detect_intents("What is the upload limit?", [], []), passages=["Note: the total size limit is 10 MB per upload."])
    assert result.contract == "passed"


# --- QQ-04: a list of an undeclared record noun ---------------------------------------------------


def test_qq04_list_operator_on_an_undeclared_record_noun_is_an_untargeted_list_intent():
    intents = detect_intents("List every pending LedgerOne ticket in Oregon.", [], TARGETS)
    assert intents.kinds() == [] and [u.text for u in intents.untargeted] == ["List every pending LedgerOne ticket"]
    assert intents.asks_list() and intents.asks_aggregate() and intents.primary() == "retrieve"
    # Not a list of records: ordinary retrieval questions are unaffected.
    for question in ("List all the steps to reset single sign-on.", "Show me all configuration options for exports."):
        assert not detect_intents(question, [], TARGETS).asks_aggregate()
    # A declared noun stays a targeted list intent.
    assert detect_intents("List every pending LedgerOne record.", [], TARGETS).kinds() == ["list"]


def test_qq04_an_enumerated_answer_to_an_untargeted_list_question_abstains_with_the_right_reason():
    question = "List every pending LedgerOne ticket in Oregon."
    intents = detect_intents(question, [], TARGETS)
    text = "Pending LedgerOne tickets in Oregon:\n1. case:0005 [kb:a]\n2. case:0014 [kb:b]"
    answer = Answer(answer=text, claims=[Claim(text="Two pending tickets were found.", kind="narrative", evidence_ids=["kb:a", "kb:b"])], citations=["kb:a", "kb:b"])
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a", "kb:b"}, aggregates=[], intents=intents, question=question, targets=TARGETS)
    assert result.contract == "abstained" and [v.rule for v in result.violations] == ["answer_text_without_exact_scope"]
    [constraint] = result.unsupported_constraints
    assert constraint.reason == "aggregate_intent_without_scope" and constraint.constraint_text == "List every pending LedgerOne ticket"
    assert "not a declared structured target" in constraint.suggestion


# --- QQ-06: the model's insufficient_evidence flag does not exempt the answer text ----------------


def test_qq06_insufficient_flag_does_not_exempt_answer_text():
    question = "How many open CloudTax tickets are there?"
    answer = Answer(answer="I cannot verify this exactly, but there are 111 open CloudTax tickets.", insufficient_evidence=True)
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a"}, aggregates=[], intents=detect_intents(question, [], TARGETS), unsupported=[], question=question, targets=TARGETS, passages=["CloudTax ticket"])
    assert result.contract == "abstained" and "111" not in result.answer.answer
    assert "answer_text_without_exact_scope" in [v.rule for v in result.violations]


def test_qq06_hedged_narrative_answer_keeps_its_hedge_and_loses_the_unbacked_sentence():
    question = "What do the notes say about billing?"
    text = "Among the 20 records provided, the notes discuss tax codes. Billing is not discussed in the supplied notes."
    answer = Answer(answer=text, insufficient_evidence=True)
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a"}, aggregates=[], intents=detect_intents(question, [], TARGETS), question=question, targets=TARGETS)
    assert result.contract == "stripped" and result.answer.answer == "Billing is not discussed in the supplied notes."
    assert result.answer.insufficient_evidence and [v.action for v in result.violations] == ["strip"]


def test_qq_engine_trace_names_the_undeclared_list_noun(tmp_path, monkeypatch):
    """QQ-04 through ``Engine``: a planner's structured step for an untargeted list question is
    bounded with the list constraint (not "no aggregate operator") and the answer abstains."""
    from tests.contract.test_scope_qualifiers_and_filters import CLOUDTAX, STATUS_OPEN, count_step, engine_for, scripted

    monkeypatch.setattr(JsonModel, "call", scripted([count_step(STATUS_OPEN, CLOUDTAX)], "1. case:0000\n2. case:0009\nThose are the open CloudTax tickets."))
    engine = engine_for(tmp_path)
    result = engine.answer("List every open CloudTax ticket.")
    assert result.retrieval.aggregates[0]["exactness"] == "bounded"
    texts = [u.constraint_text for u in result.unsupported_constraints]
    assert "List every open CloudTax ticket" in texts and "structured scope without an aggregate operator in the question" not in texts
    assert result.contract == "abstained"
