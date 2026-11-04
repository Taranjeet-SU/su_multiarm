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
from .statements import _WORD_VALUES, Statement, detect_statements, population_frame, stated_number, units_for

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
    "has_label",
    "labelled",
    "numbers_outside",
]
# A sentence ends at ``.``/``!``/``?`` followed by whitespace or the end of the text, or at a line
# break; a terminator inside a token ("gen.error_word", "99.0", "e.g.x") does not end it (live fix:
# strips split identifiers and left fragments such as "... signatures were gen.").
_SENTENCE = re.compile(r"(?:[^.!?\n]|[.!?]+(?=[^\s.!?]))*(?:[.!?]+(?=\s|$)|\n|$)")
# The qualifier that labels a number of an approximate or bounded aggregate (fix5): a word before
# the number ("approximately 304", "at least 304", "~304") or an exactness word in the sentence.
_LABEL = re.compile(
    r"(?:\b(?:approximately|approx\.?|about|around|roughly|nearly|at\s+least|more\s+than|estimated)|~)\s*\d"
    r"|\b(?:approximate|approximately|estimated|estimate|sampled|bounded|lower\s+bound|at\s+least|parsed|unparsed|source\s+counts?|token_match)\b",
    re.I,
)


def has_label(text: str) -> bool:
    """``text`` labels its numbers as approximate or bounded (``_LABEL``)."""
    return bool(_LABEL.search(text or ""))


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
    # The deterministic sentence of the aggregate (``generation/rendering.py`` or a log tool's
    # rendering): what replaces model-written numbers that the aggregate does not back.
    rendering: str | None = None

    def exact(self) -> bool:
        # Defence in depth: a result with a dropped predicate never backs a count/list claim,
        # whatever exactness label the adapter reported.
        return self.result.exactness == "exact" and not self.result.unsupported

    def labelled_backing(self) -> bool:
        """An approximate or bounded result that may back a **labelled** count (fix5): its numbers
        are usable when the answer says they are approximate or a lower bound. Never an
        exhaustive list or an absence; never a result with a dropped predicate."""
        return self.result.exactness in {"approximate", "bounded"} and not self.result.unsupported

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
    def _backs(claim: Claim, by_id: dict[str, AggregateEvidence], kind: str, labelled: bool = False) -> bool:
        """``labelled``: the claim (or the answer text) labels its numbers as approximate or
        bounded, so a ``count`` may rest on an approximate/bounded aggregate (fix5)."""
        backing = [by_id[e] for e in claim.evidence_ids if e in by_id]
        if kind == "absence":
            return any(a.exhaustive() and a.empty() for a in backing)
        if any(a.exact() for a in backing):
            return True
        return kind == "count" and labelled and any(a.labelled_backing() for a in backing)

    @staticmethod
    def _number_matches(statement: Statement, answer: Answer, by_id: dict[str, AggregateEvidence], labelled: bool = False) -> bool:
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
                if aggregate is None or not (aggregate.exact() or (labelled and aggregate.labelled_backing())):
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
                    # Grouped measures (a sum of ``lines`` per signature, B2+B3 regression from a
                    # live deployment): the groups' measure values add up ("the top two account
                    # for 2,878 lines"), and a grouped result's population is a number the answer
                    # may restate ("900 records were counted"): neither is a group count, both are
                    # backed by the cited exact aggregate.
                    group_values = [float(g.value) for g in result.groups if isinstance(g.value, (int, float)) and not isinstance(g.value, bool)]
                    if group_values:
                        values.add(sum(group_values))
                    values.add(float(result.denominator))
                    # "the top 10 signatures": the number of groups shown (with boundary ties)
                    values.update({float(len(result.groups)), float(len(result.groups) + len(result.ties))})
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
        text_labelled = has_label(answer.answer)

        def claim_labelled(claim: Claim) -> bool:
            return has_label(claim.text) or text_labelled

        for index, claim in enumerate(answer.claims):
            if claim.kind not in AGGREGATE_CLAIMS or self._backs(claim, by_id, claim.kind, claim_labelled(claim)):
                continue
            needs = "an exact, exhaustive and empty" if claim.kind == "absence" else "an exact"
            findings.append(_Finding(violation=Violation(rule="claim_without_exact_scope", detail=f"{claim.kind} claim needs {needs} ScopeResult"), claims=[index]))
        if intents.aggregate() and not exact_exists:
            # a labelled count on an approximate/bounded aggregate is a usable answer (fix5)
            offending = [i for i, c in enumerate(answer.claims) if c.kind in {"count", "exhaustive_list"} and not self._backs(c, by_id, c.kind, claim_labelled(c))]
            if offending:
                kinds = [answer.claims[i].kind for i in offending]
                findings.append(_Finding(violation=Violation(rule="aggregate_intent_without_scope", detail=f"aggregate intent without exact scope; claims {kinds}"), claims=offending))
        if not answer.insufficient_evidence and not answer.citations:
            findings.append(_Finding(violation=Violation(rule="factual_without_citations", detail="factual answer without citations")))
        if not answer.insufficient_evidence and not answer.answer.strip():
            findings.append(_Finding(violation=Violation(rule="empty_answer", detail="factual answer with empty text")))
        # a count statement may rest on an approximate/bounded aggregate when its own sentence
        # labels it ("approximately 304", "at least 304", "304 in the parsed rows"); lists and
        # absences always need an exact scope
        labelled = {i: st.kind == "count" and has_label(sentence_of(answer.answer, st.start, st.end)) for i, st in enumerate(statements)}
        unbacked: list[Statement] = []
        for i, statement in enumerate(statements):
            accepted = ("absence", "count") if statement.kind == "absence" else (statement.kind,)
            if not any(c.kind in accepted and self._backs(c, by_id, statement.kind, labelled[i]) for c in answer.claims):
                unbacked.append(statement)
        mismatched = [st for i, st in enumerate(statements) if st.kind == "count" and st not in unbacked and not self._number_matches(st, answer, by_id, labelled[i])]
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

    @staticmethod
    def _flagged(aggregates: list[AggregateEvidence], intents: Intents, unsupported: list[UnsupportedConstraint] | None) -> list[UnsupportedConstraint]:
        """``unsupported_constraints`` of an answer: the engine's, each aggregate's, one per aggregate
        intent without an exact scope, and one per untargeted list noun (QQ-04)."""
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
        return flagged

    def rendered_answer(
        self,
        aggregates: list[AggregateEvidence],
        *,
        intents: Intents,
        narrative: str | None = None,
        refused: bool = False,
        unsupported: list[UnsupportedConstraint] | None = None,
        coverage: Coverage | None = None,
        trace: Trace | None = None,
    ) -> AnswerResult:
        """The answer of a pure ranking/count plan (fix5): the aggregates' deterministic renderings
        (each with its exactness label) are the answer text; nothing the model wrote carries a
        number of it. ``narrative`` is the generator's optional one-sentence summary: kept (after
        the renderings, and as ``short_answer``) only when it is one sentence and every number in it
        appears in the renderings (``answer_number_mismatch``, action ``strip``, otherwise; the
        renderings stand). ``refused``: every aggregate was refused, the answer is
        ``insufficient_evidence``. ``fail`` raises on a dropped narrative like on any violation."""
        if not aggregates or not all(a.rendering for a in aggregates):
            raise ValueError("rendered_answer needs aggregates with a rendering")
        flagged = self._flagged(aggregates, intents, unsupported)
        parts = [labelled(a) for a in aggregates]
        rendered = " ".join(parts)
        violations: list[Violation] = []
        kept: str | None = None
        status = "none"
        if narrative is not None and narrative.strip():
            text = narrative.strip()
            # a sentence ends at . ! ? before whitespace (a decimal point is not an end) or a line break
            sentences = [p for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
            foreign = numbers_outside(text, rendered)
            if foreign:
                violations.append(
                    Violation(rule="answer_number_mismatch", detail=f"the narrative sentence states numbers the rendering does not: {foreign}", action="strip")
                )
                status = "dropped: numbers not in the rendering"
            elif len(sentences) != 1:
                status = f"dropped: {len(sentences)} sentences, one is allowed"  # never cut to its first sentence
            else:
                kept, status = text, "kept"
        if violations and self.on_violation == "fail":
            raise AnswerContractViolation(violations)
        claims = [Claim(text=a.rendering or a.id, kind="count" if a.exact() else "narrative", evidence_ids=[a.id]) for a in aggregates]
        if kept:
            claims.append(Claim(text=kept, kind="narrative", evidence_ids=[a.id for a in aggregates]))
        answer = Answer(
            answer=rendered + (f"\n\n{kept}" if kept else ""),
            claims=claims,
            citations=[a.id for a in aggregates],
            insufficient_evidence=refused,
            short_answer=kept,
        )
        outcome = "stripped" if violations else "passed"
        if trace is not None:
            trace.event(
                "answer_contract",
                violations=[v.model_dump() for v in violations],
                unsupported=flagged,
                on_violation=self.on_violation,
                exact_scope=any(a.exact() for a in aggregates),
                statements=[],
                outcome=outcome,
                rendered=True,
                narrative=status,
            )
        return AnswerResult(answer=answer, unsupported_constraints=flagged, coverage=coverage, violations=violations, contract=outcome)

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
        flagged = self._flagged(aggregates, intents, unsupported)
        statements = detect_statements(
            answer.answer,
            units=units_for(targets, question),
            list_intent=intents.asks_list(),
            passages=passages,
            fields=targets,
        )
        findings = self._findings(answer, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=statements)
        outcome, result_answer = self._apply(answer, findings, statements, aggregate_question=intents.asks_aggregate())
        if outcome == "abstained" and self.on_violation != "fail" and any(f.violation.rule == "answer_number_mismatch" for f in findings):
            rendered = self._rendered(answer, aggregates, statements)
            if rendered is not None:
                checked = detect_statements(rendered.answer, units=units_for(targets, question), list_intent=intents.asks_list(), passages=passages, fields=targets)
                if not self._findings(rendered, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=checked):
                    # The model's numbers are replaced by the aggregate's own rendering (the numbers
                    # of an aggregate answer come from the aggregate); recorded as a strip.
                    outcome, result_answer = "stripped", rendered
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

    @staticmethod
    def _rendered(answer: Answer, aggregates: list[AggregateEvidence], statements: list[Statement]) -> Answer | None:
        """The answer rebuilt from the deterministic renderings of the exact aggregates the model
        cited (all exact rendered aggregates when it cited none), followed by the model's
        sentences that state no number. ``None`` when no exact aggregate has a rendering."""
        cited = {e for c in answer.claims for e in c.evidence_ids} | set(answer.citations)
        usable = [a for a in aggregates if a.exact() and a.rendering]
        chosen = [a for a in usable if a.id in cited] or usable
        if not chosen:
            return None
        spans = [(st.start, st.end) for st in statements]
        narrative = [
            m.group(0).strip()
            for m in _SENTENCE.finditer(answer.answer)
            if m.group(0).strip() and not re.search(r"\d", m.group(0)) and not any(a < m.end() and m.start() < b for a, b in spans)
        ]
        text = " ".join([*(a.rendering for a in chosen), *narrative])  # type: ignore[misc]
        claims = [Claim(text=a.rendering or a.id, kind="count", evidence_ids=[a.id]) for a in chosen]
        citations = list(dict.fromkeys([*(a.id for a in chosen), *(c for c in answer.citations if c not in {a.id for a in aggregates})]))
        return Answer(answer=text, claims=claims, citations=citations, insufficient_evidence=False, short_answer=None)

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


_NUMBER_TOKEN = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_NUMBER_WORD = re.compile(r"\b(" + "|".join(sorted((w for w in _WORD_VALUES if " " not in w), key=len, reverse=True)) + r")\b", re.I)


def _numbers(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for m in _NUMBER_TOKEN.finditer(text):
        token = m.group(0).rstrip(",")
        try:
            found[token] = float(token.replace(",", ""))
        except ValueError:  # pragma: no cover - the pattern only matches numbers
            continue
    for m in _NUMBER_WORD.finditer(text):
        found[m.group(0)] = float(_WORD_VALUES[m.group(0).casefold()])
    return found


def sentence_of(text: str, start: int, end: int) -> str:
    """The sentence(s) of ``text`` that overlap ``[start, end)``."""
    parts = [m.group(0) for m in _SENTENCE.finditer(text) if m.group(0) and m.start() < end and start < m.end()]
    return "".join(parts) or text[start:end]


def numbers_outside(text: str, reference: str) -> list[str]:
    """The numbers (digits or number words) of ``text`` whose value is not a number of
    ``reference``: a narrative around a rendering may only restate the rendering's numbers."""
    allowed = set(_numbers(reference).values())
    return [token for token, value in _numbers(text).items() if value not in allowed]


def labelled(aggregate: AggregateEvidence) -> str:
    """An aggregate's rendering with its exactness label (appended when the rendering lacks it)."""
    text = (aggregate.rendering or "").strip()
    label = aggregate.result.exactness
    if re.search(rf"\b{label}\b", text):
        return text
    return f"{text.rstrip('.')} ({label})."


def answer_from_generator(data: dict[str, Any]) -> Answer:
    """Validate a generator reply; any ``unsupported_constraints`` key is a schema violation."""
    return Answer.model_validate(data)
