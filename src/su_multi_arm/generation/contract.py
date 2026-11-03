"""``AnswerContract`` (plan §3.9a): what a generated answer may claim, enforced by the engine.

Rules, applied after generation and before any guard:

1. every ``citations`` / ``Claim.evidence_ids`` entry exists in the supplied evidence;
2. a ``count``, ``exhaustive_list`` or ``absence`` claim must reference an aggregate evidence whose
   ``ScopeResult.exactness == "exact"``; ``absence`` additionally needs an exhaustive scope (no
   unsupported predicate, nothing missing or invalid);
3. when the intent pre-pass detected an aggregate intent and no exact ``ScopeResult`` exists, the
   answer must not contain a ``count`` / ``exhaustive_list`` claim and ``unsupported_constraints``
   is non-empty (the contract injects one per intent if the engine supplied none);
4. a factual answer (``insufficient_evidence: false``) without citations is rejected.

``unsupported_constraints`` is populated here from the ``Decision`` and the ``ScopeResult``s; the
generator cannot set it (``Answer`` forbids the key). Violation handling: ``abstain_with_reason``
(default) returns ``insufficient_evidence: true`` with the violated rules recorded; ``fail`` raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from ..core.types import ScopeResult, UnsupportedConstraint
from ..decision.intent import Intents
from ..models import Answer, Claim, ClaimKind, ContractRule, Coverage, StrictModel, Violation

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.trace import Trace

AGGREGATE_CLAIMS: frozenset[str] = frozenset({"count", "exhaustive_list", "absence"})
Rule = ContractRule
__all__ = ["AGGREGATE_CLAIMS", "AggregateEvidence", "Answer", "AnswerContract", "AnswerContractViolation", "AnswerResult", "Claim", "ClaimKind", "Coverage", "Rule", "Violation", "answer_from_generator"]


class AnswerResult(StrictModel):
    answer: Answer
    unsupported_constraints: list[UnsupportedConstraint] = Field(default_factory=list)  # engine-injected only
    coverage: Coverage | None = None
    violations: list[Violation] = Field(default_factory=list)
    contract: Literal["passed", "abstained"] = "passed"


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


class AnswerContractViolation(ValueError):
    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__("; ".join(f"{v.rule}: {v.detail}" for v in violations))


class AnswerContract:
    def __init__(self, on_violation: Literal["abstain_with_reason", "fail"] = "abstain_with_reason"):
        self.on_violation = on_violation

    def check(
        self,
        answer: Answer,
        *,
        evidence_ids: set[str],
        aggregates: list[AggregateEvidence],
        intents: Intents,
    ) -> list[Violation]:
        violations: list[Violation] = []
        by_id = {a.id: a for a in aggregates}
        known = set(evidence_ids) | set(by_id)
        unknown = sorted((set(answer.citations) | {e for c in answer.claims for e in c.evidence_ids}) - known)
        if unknown:
            violations.append(Violation(rule="unknown_evidence", detail=f"cited evidence not supplied: {unknown}"))
        exact_exists = any(a.exact() for a in aggregates)
        for claim in answer.claims:
            if claim.kind not in AGGREGATE_CLAIMS:
                continue
            backing = [by_id[e] for e in claim.evidence_ids if e in by_id]
            needed = (lambda a: a.exhaustive()) if claim.kind == "absence" else (lambda a: a.exact())
            if not any(needed(a) for a in backing):
                violations.append(
                    Violation(
                        rule="claim_without_exact_scope",
                        detail=f"{claim.kind} claim needs an exact{' and exhaustive' if claim.kind == 'absence' else ''} ScopeResult",
                    )
                )
        if intents.aggregate() and not exact_exists:
            offending = [c.kind for c in answer.claims if c.kind in {"count", "exhaustive_list"}]
            if offending:
                violations.append(
                    Violation(rule="aggregate_intent_without_scope", detail=f"aggregate intent without exact scope; claims {offending}")
                )
        if not answer.insufficient_evidence and not answer.citations:
            violations.append(Violation(rule="factual_without_citations", detail="factual answer without citations"))
        return violations

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
    ) -> AnswerResult:
        flagged = list(unsupported or [])
        for aggregate in aggregates:
            flagged.extend(aggregate.result.unsupported)
        if intents.aggregate() and not any(a.exact() for a in aggregates) and not flagged:
            flagged = [
                UnsupportedConstraint(constraint_text=a.text, reason="aggregate_intent_without_scope", suggestion="no exact typed scope was executed")
                for a in intents.aggregates
            ]
        violations = self.check(answer, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents)
        if trace is not None:
            trace.event(
                "answer_contract",
                violations=[v.model_dump() for v in violations],
                unsupported=flagged,
                on_violation=self.on_violation,
                exact_scope=any(a.exact() for a in aggregates),
            )
        if not violations:
            return AnswerResult(answer=answer, unsupported_constraints=flagged, coverage=coverage)
        if self.on_violation == "fail":
            raise AnswerContractViolation(violations)
        abstained = Answer(
            answer="Insufficient evidence for the requested claim: " + "; ".join(v.rule for v in violations) + ".",
            claims=[],
            citations=[],
            insufficient_evidence=True,
        )
        return AnswerResult(answer=abstained, unsupported_constraints=flagged, coverage=coverage, violations=violations, contract="abstained")


def answer_from_generator(data: dict[str, Any]) -> Answer:
    """Validate a generator reply; any ``unsupported_constraints`` key is a schema violation."""
    return Answer.model_validate(data)
