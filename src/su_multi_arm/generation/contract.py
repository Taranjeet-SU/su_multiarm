"""``AnswerContract`` (plan §3.9a): what a generated answer may claim, enforced by the engine.

Rules, applied after generation and before any guard:

1. every ``citations`` / ``Claim.evidence_ids`` entry exists in the supplied evidence;
2. a ``count``, ``exhaustive_list`` or ``absence`` claim must reference an aggregate evidence whose
   ``ScopeResult.exactness == "exact"`` (and that carries no unsupported constraint); ``absence``
   additionally needs an exhaustive scope (nothing missing or invalid) whose value is empty (a zero
   count, an empty list or group set);
3. when the intent pre-pass detected an aggregate intent and no exact ``ScopeResult`` exists, the
   answer must not contain a ``count`` / ``exhaustive_list`` claim and ``unsupported_constraints``
   is non-empty (the contract injects one per intent if the engine supplied none);
4. a factual answer (``insufficient_evidence: false``) without citations, or with an empty answer
   text (``empty_answer``), is rejected;
5. (QA-04) the user-facing ``answer`` text is checked too: every count, exhaustive-list or absence
   **statement** found in it (``statements.py``; deterministic, English-scoped) needs a typed claim of
   the same kind (an absence statement may be backed by a ``count`` claim) that rule 2 accepts.
   Violation: ``answer_text_without_exact_scope``. The check runs whether or not the model set
   ``insufficient_evidence`` (QQ-06): a hedge flag never exempts a stated count, list or absence.

``unsupported_constraints`` is populated here from the ``Decision`` and the ``ScopeResult``s; the
generator cannot set it (``Answer`` forbids the key).

Violation handling (``on_violation``):

* ``strip`` (default since 2.0.0a3, owner decision): a non-narrative claim that rules 2/3 reject is
  removed from ``claims`` (QA-08), and when the **question asks no aggregate** (no count, list,
  total, earliest/latest or absence intent, targeted or not) the sentences of the answer text that
  carry an unbacked statement are removed too; every removal is recorded (``action: strip``,
  ``contract: stripped``). When the question **does** ask an aggregate, an unbacked statement in the
  text is the answer itself, so the answer abstains instead. If nothing remains, or no citation
  remains for a factual answer, it abstains. Unknown evidence, missing citations, an empty answer
  and a stated number that differs from the cited exact aggregate always abstain.
* ``abstain_with_reason``: the answer becomes ``insufficient_evidence: true`` with the violated
  rules recorded (``contract: abstained``). One exception (QA-08): when the answer **text**
  contains no count, list or absence statement, a non-narrative claim that rules 2/3 reject is
  stripped from ``claims`` instead; the narrative answer stands.
* ``fail``: any violation raises ``AnswerContractViolation``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, Sequence

from pydantic import Field, create_model

from ..core.types import ScopeResult, UnsupportedConstraint
from ..decision.intent import Intents, untargeted_constraint
from ..models import Answer, Claim, ClaimKind, ContractRule, Coverage, StrictModel, Violation
from .statements import Statement, detect_statements, population_frame, stated_number, units_for

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.trace import Trace

AGGREGATE_CLAIMS: frozenset[str] = frozenset({"count", "exhaustive_list", "absence"})
# A preposition that introduces a population ("of", "among", "over" ...) right before a stated number.
_POPULATION_PREPOSITION = re.compile(r"(?<!\btotal )\b(?:of|among|over|across|within|from)\s+(?:(?:a|an|the|all|its|their|this|these)\s+)?$", re.I)
CLAIM_KINDS: tuple[str, ...] = ("narrative", "count", "exhaustive_list", "absence")
Rule = ContractRule
OnViolation = Literal["abstain_with_reason", "strip", "fail"]
__all__ = [
    "AGGREGATE_CLAIMS",
    "AggregateEvidence",
    "Answer",
    "AnswerContract",
    "AnswerContractViolation",
    "AnswerResult",
    "Claim",
    "ClaimKind",
    "Coverage",
    "Rule",
    "Violation",
    "answer_from_generator",
    "answer_schema",
]
_SENTENCE = re.compile(r"[^.!?\n]*(?:[.!?]+|\n|$)")


class AnswerResult(StrictModel):
    answer: Answer
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)  # engine-injected only
    coverage: Coverage | None = None
    violations: list[Violation] = Field(default_factory=list)
    contract: Literal["passed", "stripped", "abstained"] = "passed"


class AggregateEvidence(StrictModel):
    """An aggregate evidence id with the ``ScopeResult`` that produced it."""

    id: str = Field(min_length=1)
    result: ScopeResult

    def exact(self) -> bool:
        # Defence in depth: a result with a dropped predicate never backs a count/list claim,
        # whatever exactness label the adapter reported.
        return self.result.exactness == "exact" and not self.result.unsupported

    def exhaustive(self) -> bool:
        return self.exact() and not self.result.unsupported and self.result.missing_or_invalid == 0

    def empty(self) -> bool:
        """The scope matched nothing: what an absence claim needs."""
        value = self.result.value
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return value == 0
        if isinstance(value, (list, dict)):
            return not value
        # ``total`` too: under native aggregation ``matched_ids`` may be a truncated prefix.
        return value is None and not self.result.matched_ids and not self.result.groups and not self.result.total


class AnswerContractViolation(ValueError):
    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("; ".join(f"{v.rule}: {v.detail}" for v in violations))


def answer_schema(evidence_ids: Sequence[str], kinds: Sequence[str] = CLAIM_KINDS) -> type[StrictModel]:
    """The generator's reply schema for one request (QA-08): ``Answer`` with ``citations`` and
    ``evidence_ids`` restricted to the supplied evidence ids and ``kind`` to ``kinds``. Sent only
    where the provider enforces the schema (native structured output); the reply is converted to
    ``Answer`` and the contract still checks it."""
    ids = tuple(dict.fromkeys(evidence_ids))
    if not ids:
        raise ValueError("answer_schema needs at least one evidence id")
    id_type: Any = Literal[ids]  # type: ignore[valid-type]
    kind_type: Any = Literal[tuple(dict.fromkeys(kinds))]  # type: ignore[valid-type]
    claim = create_model("Claim", __base__=StrictModel, text=(str, Field(min_length=1)), kind=(kind_type, "narrative"), evidence_ids=(list[id_type], Field(default_factory=list)))
    return create_model(
        "Answer",
        __base__=StrictModel,
        __doc__=Answer.__doc__,
        answer=(str, ...),
        claims=(list[claim], Field(default_factory=list)),  # type: ignore[valid-type]
        citations=(list[id_type], Field(default_factory=list)),
        insufficient_evidence=(bool, False),
        short_answer=(str | None, None),
    )


class _Finding(StrictModel):
    violation: Violation
    claims: list[int] = Field(default_factory=list)  # indices of offending claims
    statements: list[Statement] = Field(default_factory=list)  # offending statements in the text


class AnswerContract:
    def __init__(self, on_violation: OnViolation = "strip"):
        self.on_violation = on_violation

    # --- checks ----------------------------------------------------------------------------------

    @staticmethod
    def _backs(claim: Claim, by_id: dict[str, AggregateEvidence], kind: str) -> bool:
        backing = [by_id[e] for e in claim.evidence_ids if e in by_id]
        if kind == "absence":
            return any(a.exhaustive() and a.empty() for a in backing)
        return any(a.exact() for a in backing)

    @staticmethod
    def _number_matches(statement: Statement, answer: Answer, by_id: dict[str, AggregateEvidence]) -> bool:
        """A backed ``count`` statement's number equals a value of an exact aggregate cited by a count
        claim (scalar value, a group count, or the number of listed ids). Vague counts pass (N2).
        A total/sum statement compares its ``amount`` (QQ-03)."""
        stated = statement.amount if statement.amount is not None else stated_number(statement.text)
        if stated is None:
            return True
        number_at = statement.start
        if statement.amount is not None:  # a total statement ends with its amount ("sum ... out of 5000")
            runs = list(re.finditer(r"\d[\d,]*(?:\.\d+)?", statement.text))
            number_at = statement.start + (runs[-1].start() if runs else 0)
        if population_frame(answer.answer, number_at, statement.end):
            return True  # "out of 2048 records", "a denominator of 2048": the population, not the count
        values: set[float] = set()
        for claim in answer.claims:
            if claim.kind != "count":
                continue
            for evidence in claim.evidence_ids:
                aggregate = by_id.get(evidence)
                if aggregate is None or not aggregate.exact():
                    continue
                value = aggregate.result.value
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    values.add(float(value))
                elif isinstance(value, dict):
                    values.update(float(v) for v in value.values() if isinstance(v, (int, float)) and not isinstance(v, bool))
                elif isinstance(value, list):
                    values.add(float(len(value)))
                result = aggregate.result
                numbers: list[float] = []
                for group in [*result.groups, *result.ties]:
                    values.add(float(group.count))
                    if isinstance(group.value, (int, float)) and not isinstance(group.value, bool):
                        values.add(float(group.value))
                        numbers.append(float(group.value))
                if result.groups:
                    values.add(float(sum(g.count for g in result.groups)))  # "these sum to 1400"
                # Query-compiler results (2.0.0a3): the matched total ("showing 10 of 42"), a share's
                # numerator, the records listed, null counts and a two-period difference.
                values.update(float(n) for n in (result.total, result.numerator) if n is not None)
                values.update(float(n) for n in result.unknown.values())
                if result.listed:
                    values.add(float(len(result.listed)))
                if len(numbers) >= 2 and all(g.key.keys() == {"period"} for g in result.groups[:2]):
                    values.add(abs(numbers[1] - numbers[0]))
                # The population the aggregate reports, restated after a preposition ("12 of 36
                # records", "among 2,048 records", "over 2,048 records in the snapshot"; 2.0.0a3
                # parity fix). A bare "there are 36 records" equal to the denominator stays a
                # mismatch: it states the population as the count.
                if _POPULATION_PREPOSITION.search(answer.answer[max(0, statement.start - 48) : statement.start]):
                    values.add(float(result.denominator))
        return not values or float(stated) in values

    def _findings(
        self,
        answer: Answer,
        *,
        evidence_ids: set[str],
        aggregates: list[AggregateEvidence],
        intents: Intents,
        statements: list[Statement],
    ) -> list[_Finding]:
        findings: list[_Finding] = []
        by_id = {a.id: a for a in aggregates}
        known = set(evidence_ids) | set(by_id)
        unknown = sorted((set(answer.citations) | {e for c in answer.claims for e in c.evidence_ids}) - known)
        if unknown:
            findings.append(_Finding(violation=Violation(rule="unknown_evidence", detail=f"cited evidence not supplied: {unknown}")))
        exact_exists = any(a.exact() for a in aggregates)
        for index, claim in enumerate(answer.claims):
            if claim.kind not in AGGREGATE_CLAIMS or self._backs(claim, by_id, claim.kind):
                continue
            needs = "an exact, exhaustive and empty" if claim.kind == "absence" else "an exact"
            findings.append(_Finding(violation=Violation(rule="claim_without_exact_scope", detail=f"{claim.kind} claim needs {needs} ScopeResult"), claims=[index]))
        if intents.aggregate() and not exact_exists:
            offending = [i for i, c in enumerate(answer.claims) if c.kind in {"count", "exhaustive_list"}]
            if offending:
                kinds = [answer.claims[i].kind for i in offending]
                findings.append(_Finding(violation=Violation(rule="aggregate_intent_without_scope", detail=f"aggregate intent without exact scope; claims {kinds}"), claims=offending))
        if not answer.insufficient_evidence and not answer.citations:
            findings.append(_Finding(violation=Violation(rule="factual_without_citations", detail="factual answer without citations")))
        if not answer.insufficient_evidence and not answer.answer.strip():
            findings.append(_Finding(violation=Violation(rule="empty_answer", detail="factual answer with empty text")))
        unbacked: list[Statement] = []
        for statement in statements:
            accepted = ("absence", "count") if statement.kind == "absence" else (statement.kind,)
            if not any(c.kind in accepted and self._backs(c, by_id, statement.kind) for c in answer.claims):
                unbacked.append(statement)
        mismatched = [s for s in statements if s.kind == "count" and s not in unbacked and not self._number_matches(s, answer, by_id)]
        # QQ-06: the model's insufficient_evidence flag does not exempt the text from these checks.
        if mismatched:
            findings.append(
                _Finding(
                    violation=Violation(rule="answer_number_mismatch", detail="the count stated in the answer text differs from the value of the exact aggregate its count claim cites"),
                    statements=mismatched,
                )
            )
        if unbacked:
            kinds = list(dict.fromkeys(s.kind for s in unbacked))
            findings.append(
                _Finding(
                    violation=Violation(rule="answer_text_without_exact_scope", detail=f"answer text states {kinds} without a matching typed claim backed by an exact ScopeResult"),
                    statements=unbacked,
                )
            )
        return findings

    def check(
        self,
        answer: Answer,
        *,
        evidence_ids: set[str],
        aggregates: list[AggregateEvidence],
        intents: Intents,
        statements: list[Statement] | None = None,
    ) -> list[Violation]:
        return [f.violation for f in self._findings(answer, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=statements or [])]

    # --- enforcement -----------------------------------------------------------------------------

    def enforce(
        self,
        answer: Answer,
        *,
        evidence_ids: set[str],
        aggregates: list[AggregateEvidence],
        intents: Intents,
        unsupported: list[UnsupportedConstraint] | None = None,
        coverage: Coverage | None = None,
        trace: Trace | None = None,
        question: str | None = None,
        targets: Sequence[str] = (),
        passages: Sequence[str] = (),
    ) -> AnswerResult:
        """Check ``answer`` and apply ``on_violation``. ``question``, ``targets`` (the deployment's
        structured targets) and ``passages`` (the non-aggregate evidence text) tune the text check:
        unit nouns and quotations (``statements.py``)."""
        flagged = list(unsupported or [])
        for aggregate in aggregates:
            for item in aggregate.result.unsupported:
                if item not in flagged:
                    flagged.append(item)
        if intents.aggregate() and not any(a.exact() for a in aggregates) and not flagged:
            flagged = [
                UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope", suggestion="no exact typed scope was executed")
                for a in intents.aggregates
            ]
        for item in intents.untargeted:  # QQ-04: a list of a noun no structured scope can express
            if not any(f.constraint_text == item.text for f in flagged):
                flagged.append(untargeted_constraint(item))
        statements = detect_statements(
            answer.answer,
            units=units_for(targets, question),
            list_intent=intents.asks_list(),
            passages=passages,
            fields=targets,
        )
        findings = self._findings(answer, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=statements)
        outcome, result_answer = self._apply(answer, findings, statements, aggregate_question=intents.asks_aggregate())
        violations = [f.violation.model_copy(update={"action": "strip" if outcome == "stripped" else "abstain"}) for f in findings]
        if trace is not None:
            trace.event(
                "answer_contract",
                violations=[v.model_dump() for v in violations],
                unsupported=flagged,
                on_violation=self.on_violation,
                exact_scope=any(a.exact() for a in aggregates),
                statements=[s.kind for s in statements],
                outcome=outcome,
            )
        if not findings:
            return AnswerResult(answer=answer, unsupported_constraints=flagged, coverage=coverage)
        if self.on_violation == "fail":
            raise AnswerContractViolation(violations)
        if outcome == "stripped":
            return AnswerResult(answer=result_answer, unsupported_constraints=flagged, coverage=coverage, violations=violations, contract="stripped")
        abstained = Answer(
            answer="Insufficient evidence for the requested claim: " + "; ".join(dict.fromkeys(v.rule for v in violations)) + ".",
            claims=[],
            citations=[],
            insufficient_evidence=True,
        )
        return AnswerResult(answer=abstained, unsupported_constraints=flagged, coverage=coverage, violations=violations, contract="abstained")

    def _apply(self, answer: Answer, findings: list[_Finding], statements: list[Statement], *, aggregate_question: bool = False) -> tuple[str, Answer]:
        """``("passed" | "stripped" | "abstained", answer)`` under the configured policy."""
        if not findings:
            return "passed", answer
        if self.on_violation == "fail":
            return "abstained", answer
        strippable = {"claim_without_exact_scope", "aggregate_intent_without_scope"}
        if self.on_violation == "strip":
            if not aggregate_question:  # a narrative answer: drop the unbacked sentences only
                strippable = strippable | {"answer_text_without_exact_scope"}
        elif statements:
            return "abstained", answer  # the text itself states a count/list/absence: never strip silently
        if any(f.violation.rule not in strippable for f in findings):
            return "abstained", answer
        drop_claims = {i for f in findings for i in f.claims}
        text = answer.answer
        spans = [(s.start, s.end) for f in findings for s in f.statements]
        if spans:
            kept = [m.group(0) for m in _SENTENCE.finditer(text) if m.group(0) and not any(a < m.end() and m.start() < b for a, b in spans)]
            text = "".join(kept).strip()
            if not text:
                return "abstained", answer
        claims = [c for i, c in enumerate(answer.claims) if i not in drop_claims]
        cited = {e for c in claims for e in c.evidence_ids}
        dropped_only = {e for i in drop_claims for e in answer.claims[i].evidence_ids} - cited
        citations = [c for c in answer.citations if c not in dropped_only or c in text]
        if not citations and not answer.insufficient_evidence:
            return "abstained", answer
        return "stripped", answer.model_copy(update={"answer": text, "claims": claims, "citations": citations})


def answer_from_generator(data: dict[str, Any]) -> Answer:
    """Validate a generator reply; any ``unsupported_constraints`` key is a schema violation."""
    return Answer.model_validate(data)
