"""Answer-number check on grouped sums (B2+B3 regression from a live deployment, synthetic here).

"Top 10 signature_id by total lines" is a ``sum`` of ``lines`` grouped by signature with ``top_n``.
An answer that states the group sums, their total, the number of groups shown or the grouped
population passes; a number no part of the cited exact aggregate backs still abstains."""

from __future__ import annotations

import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.core.types import Document, GenerationId, Measure, MetadataSchema, ScopeSpec
from su_multi_arm.decision import detect_intents
from su_multi_arm.generation import AggregateEvidence, Answer, AnswerContract, Claim
from su_multi_arm.generation.statements import detect_statements, units_for

TARGETS = ["signature_id", "signature id", "lines", "records"]
QUESTION = "Top 10 signature_id by total lines"
SCHEMA = MetadataSchema(
    fields=[
        {"name": "signature_id", "type": "string", "groupable": True},
        {"name": "lines", "type": "integer"},
    ]
)


def _evidence() -> AggregateEvidence:
    """A real grouped sum with ``top_n`` from the in-memory records adapter (client path)."""
    backend = MemoryBackend()
    gen = GenerationId(dataset="d", generation="0" * 32, index_format=1, fingerprint="0" * 64)
    docs = []
    plan = {"sig.a": [1000, 726], "sig.b": [1152], "sig.c": [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]}  # row count order != sum order
    for sig, values in plan.items():
        for i, lines in enumerate(values):
            docs.append(Document(id=f"{sig}-{i}", dataset="d", metadata={"signature_id": sig, "lines": lines}))
    from su_multi_arm.core.types import Provenance, StructuredRecord

    records = [StructuredRecord(schema_id="doc", id=d.id, values=d.metadata, provenance=Provenance(extractor="test")) for d in docs]
    backend.records.upsert(gen, records, SCHEMA)
    spec = ScopeSpec(group_by=["signature_id"], measure=Measure(kind="sum", field="lines"), sort={"field": "$measure", "descending": True}, top_n=2)
    result = backend.records.execute(gen, spec, [])
    assert [(g.key["signature_id"], g.value) for g in result.groups] == [("sig.a", 1726), ("sig.b", 1152)]
    assert result.exactness == "exact"
    return AggregateEvidence(id="aggregate:1", result=result)


def _check(text: str):
    units = units_for(TARGETS, QUESTION)
    statements = detect_statements(text, units=units)
    answer = Answer(answer=text, citations=["aggregate:1"], claims=[Claim(text=text, kind="count", evidence_ids=["aggregate:1"])])
    return AnswerContract().check(answer, evidence_ids=set(), aggregates=[_evidence()], intents=detect_intents(QUESTION, targets=TARGETS), statements=statements)


@pytest.mark.parametrize(
    "text",
    [
        "The top signatures by total lines are sig.a with 1726 lines and sig.b with 1152 lines.",
        "Top 10 signature_id by total lines:\n1. sig.a: 1,726 lines\n2. sig.b: 1,152 lines",
        "sig.a has a total of 1726 lines, followed by sig.b with 1152 lines.",
        "Together the top two account for 2,878 lines.",
        "The top 2 signatures account for 1726 and 1152 lines respectively.",
        "sig.a leads with 1726 lines; 15 records were counted in total.",
    ],
)
def test_stated_group_sums_pass(text):
    assert _check(text) == []


@pytest.mark.parametrize("text", ["sig.a has 1999 lines.", "The top signature sig.a has 1152 lines and sig.b 1153 lines."])
def test_a_wrong_number_still_abstains(text):
    assert [v.rule for v in _check(text)] == ["answer_number_mismatch"]


# --- the numbers of an aggregate answer come from the aggregate (rendering) ------------------------


def _enforce(text: str, *, rendered: bool = True):
    from su_multi_arm.arms.structured import aggregate_record

    evidence = _evidence()
    spec = ScopeSpec(group_by=["signature_id"], measure=Measure(kind="sum", field="lines"), order="desc", top_n=2)
    rendering = aggregate_record(spec, evidence.result)["rendering"] if rendered else None
    evidence = evidence.model_copy(update={"rendering": rendering})
    answer = Answer(answer=text, citations=["aggregate:1"], claims=[Claim(text=text, kind="count", evidence_ids=["aggregate:1"])])
    intents = detect_intents(QUESTION, targets=TARGETS)
    return AnswerContract().enforce(answer, evidence_ids={"aggregate:1"}, aggregates=[evidence], intents=intents, question=QUESTION, targets=TARGETS), rendering


def test_the_rendering_states_every_group_value():
    _, rendering = _enforce("sig.a has 1726 lines.")
    assert rendering == "sum of lines by signature_id: sig.a: 1726; sig.b: 1152 (exact; 15 records matched)."


def test_a_correct_answer_passes_unchanged():
    result, _ = _enforce("The top signature is sig.a with 1726 lines, then sig.b with 1152 lines.")
    assert result.contract == "passed" and "1726" in result.answer.answer


def test_an_invented_number_is_replaced_by_the_rendering():
    result, rendering = _enforce("sig.a has 1999 lines. It is the noisiest signature by far.")
    assert result.contract == "stripped"
    assert [v.rule for v in result.violations] == ["answer_number_mismatch"] and result.violations[0].action == "strip"
    assert result.answer.answer.startswith(rendering) and "1999" not in result.answer.answer
    assert "noisiest signature" in result.answer.answer  # narrative without numbers is kept
    assert result.answer.claims[0].evidence_ids == ["aggregate:1"]


def test_an_invented_number_without_a_rendering_abstains():
    result, _ = _enforce("sig.a has 1999 lines.", rendered=False)
    assert result.contract == "abstained" and result.answer.insufficient_evidence
