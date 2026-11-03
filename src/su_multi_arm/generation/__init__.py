"""Generation: the answer contract (plan §3.9a). The generator component lands with the engine port."""

from .contract import (
    AggregateEvidence,
    Answer,
    AnswerContract,
    AnswerContractViolation,
    AnswerResult,
    Claim,
    Coverage,
    Violation,
    answer_from_generator,
)

__all__ = [
    "AggregateEvidence",
    "Answer",
    "AnswerContract",
    "AnswerContractViolation",
    "AnswerResult",
    "Claim",
    "Coverage",
    "Violation",
    "answer_from_generator",
]
