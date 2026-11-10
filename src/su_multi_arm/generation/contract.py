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
6. (2026-11-08 parity fix) an ``examples`` claim is a bounded sample of records shown next to an
   aggregate: it cites between one and ``MAX_EXAMPLES`` record (non-aggregate) evidence ids and its
   text claims no completeness (``examples_claim_invalid`` otherwise; the claim is stripped). It
   never backs a count, an absence or a completeness phrase. It backs only the **enumerated block**
   of a list question (the ``enumerated list`` statement) and only when the answer frames the block
   as examples ("for example", "examples", "sample", "such as", "including", "for instance") and a
   ``count`` claim is backed by an exact aggregate: "exactly 35 records match; examples: A, B, C" is
   an exact count with a sample. A completeness phrase ("the complete list", "these are all the
   tickets", "no other tickets") still needs an ``exhaustive_list`` claim on an exact scope, whatever
   the examples claim says. Completeness phrases that the sentence denies ("not a complete list")
   are disclaimers, not statements (``statements.completeness_disclaimed``).

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
* ``annotate`` (opt-in, 2026-11-09): keep the generator's evidence-cited answer and append one
  deterministic limitation note per violated claim/statement (``contract: annotated``,
  ``limitations``); unknown evidence, missing citations and an empty answer still abstain, a number
  contradicting the exact aggregate is removed and corrected, uncited claims are removed
  (``AnswerContract._annotate``).
* ``fail``: any violation raises ``AnswerContractViolation``.

2026-11-09 (corpus-B rerun B4): inline ``[id]`` markers of supplied evidence are citations (an
id-shaped unknown marker is ``unknown_evidence``); a ``count`` statement whose number is a value of a
cited exact aggregate needs no typed claim; result sizes, retained-id counts, the question's own
thresholds / requested N / quoted phrases and counts of the cited aggregates are not record counts
(``result_statements``).

2026-11-09 (corpus-B rerun B5): for a question that asks one aggregate kind, ``count``, whose exact
``scalar_count`` aggregates all state one number (``_count_target``), an unbacked count statement or
count claim stating another number is ``answer_number_mismatch`` (corrected: strip falls back to the
rendering, annotate removes it and states the exact count with its citation), never a kept,
annotated alternative; in ``annotate`` a zero count without an exact scope is removed (``NOTE_ZERO``).
"""

from __future__ import annotations

import re
from html import escape
from typing import TYPE_CHECKING, Any, Literal, Sequence

from pydantic import Field, create_model

from ..core.types import ScopeResult, UnsupportedConstraint
from ..decision.intent import Intents, untargeted_constraint
from ..models import Answer, Claim, ClaimKind, ContractRule, Coverage, StrictModel, Violation
from .statements import (
    _ADVICE_START,
    _ADVISORY,
    _WORD_VALUES,
    Statement,
    completeness_disclaimed,
    detect_statements,
    population_frame,
    stated_number,
    units_for,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.trace import Trace

AGGREGATE_CLAIMS: frozenset[str] = frozenset({"count", "exhaustive_list", "absence"})
MAX_EXAMPLES = 10  # record ids an ``examples`` claim may cite (a sample, never a full listing)
# How an answer frames an enumerated block as a sample (rule 6).
_EXAMPLE_FRAME = re.compile(r"\b(?:for\s+example|for\s+instance|e\.g\.|examples?|samples?|sampled|such\s+as|including|illustrative)\b", re.I)
# A completeness phrase inside an ``examples`` claim's own text (rule 6).
_COMPLETENESS = re.compile(
    r"\b(?:complete|full|exhaustive|entire|whole)\s+(?:list|set|listing)\b|\b(?:these|those|here|they)\s+are\s+(?:all|every)\b|\bthe\s+only\b|\bno\s+other\b|\ball\s+(?:of\s+)?the\s+(?:matching|matched)\b",
    re.I,
)
# A preposition that introduces a population ("of", "among", "over" ...) right before a stated number.
_POPULATION_PREPOSITION = re.compile(r"(?<!\btotal )\b(?:of|among|over|across|within|from)\s+(?:(?:a|an|the|all|its|their|this|these)\s+)?$", re.I)
CLAIM_KINDS: tuple[str, ...] = ("narrative", "count", "exhaustive_list", "absence", "examples")
Rule = ContractRule
ENUMERATED = "enumerated list"  # the text of the enumerated-block statement (``statements.py``)
OnViolation = Literal["abstain_with_reason", "strip", "annotate", "fail"]
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
    contract: Literal["passed", "stripped", "abstained", "annotated"] = "passed"
    limitations: list[str] = Field(default_factory=list)  # ``annotate``: the notes appended to the answer


class AggregateEvidence(StrictModel):
    """An aggregate evidence id with the ``ScopeResult`` that produced it."""

    id: str = Field(min_length=1)
    result: ScopeResult
    # The deterministic sentence of the aggregate (``generation/rendering.py`` or a log tool's
    # rendering): what replaces model-written numbers that the aggregate does not back.
    rendering: str | None = None
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    # 2026-11-09 (B5 q023): a record count with one scalar value (``Engine._scalar_count``); the
    # contract corrects a count statement that differs from the one exact such count of a count
    # question (``AnswerContract._count_target``)
    scalar_count: bool = False

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
    correction: str | None = None  # the exact count aggregate a mismatch is corrected to (B5 q023)


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
    def _aggregate_values(aggregate: AggregateEvidence, answer: Answer, statement: Statement) -> set[float]:
        """The numbers ``aggregate`` backs for ``statement`` (scalar value, group counts and measures,
        totals, numerators, null counts, listed ids, SQL row cells, a restated population)."""
        values: set[float] = set()
        value = aggregate.result.value
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            values.add(float(value))
        elif isinstance(value, dict):
            values.update(float(v) for v in value.values() if isinstance(v, (int, float)) and not isinstance(v, bool))
        elif isinstance(value, list):
            values.add(float(len(value)))
        result = aggregate.result
        numbers: list[float] = []
        for group in [*result.groups, *result.ties]:
            values.add(float(group.count))
            if group.numerator is not None:  # a per-group share's numerator ("12 of 40", B5 q043)
                values.add(float(group.numerator))
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
        # SQL/typed result rows (2026-11-09 corpus-B fix F1): a ranked or grouped SQL result
        # has no scalar value; its numbers are the numeric cells of the returned rows.
        for row in aggregate.rows:
            values.update(float(v) for v in row_values(row).values() if isinstance(v, (int, float)) and not isinstance(v, bool))
        # The retained contributor ids ("8 document ids are listed"): only when the
        # statement counts ids, never as a count of the matched records.
        if result.matched_ids and _ID_COUNT.match(answer.answer, statement.start):
            values.add(float(len(result.matched_ids)))
        return values

    @staticmethod
    def _cited(answer: Answer, by_id: dict[str, AggregateEvidence]) -> list[AggregateEvidence]:
        """The aggregates the answer cites anywhere (citations, inline markers merged into them, or
        any claim's evidence ids), in first-cited order."""
        ids = dict.fromkeys([*answer.citations, *(e for c in answer.claims for e in c.evidence_ids)])
        return [by_id[e] for e in ids if e in by_id]

    @staticmethod
    def _number_matches(statement: Statement, answer: Answer, by_id: dict[str, AggregateEvidence], labelled: bool = False) -> bool:
        """A backed ``count`` statement's number equals a value of an exact aggregate cited by a count
        claim (scalar value, a group count, or the number of listed ids) or, since 2026-11-09 (B4),
        of any exact aggregate the answer cites (a number that equals no cited exact aggregate's value
        is still a mismatch). Vague counts pass (N2). A total/sum statement compares its ``amount``
        (QQ-03)."""
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
        claimed = [e for c in answer.claims if c.kind == "count" for e in c.evidence_ids]
        for evidence in dict.fromkeys([*claimed, *(a.id for a in AnswerContract._cited(answer, by_id))]):
            aggregate = by_id.get(evidence)
            if aggregate is not None and (aggregate.exact() or (labelled and aggregate.labelled_backing())):
                values |= AnswerContract._aggregate_values(aggregate, answer, statement)
        return not values or float(stated) in values

    @staticmethod
    def _implicitly_backed(statement: Statement, answer: Answer, by_id: dict[str, AggregateEvidence], labelled: bool = False) -> bool:
        """2026-11-09 (B4 q028/q034/q042 shapes): a ``count`` statement without a typed ``count`` claim
        is backed when the answer cites an exact aggregate (or, for a labelled statement, an
        approximate/bounded one) and the statement's **number** is a value of it. A vague count
        ("several tickets") never is: only a number can be checked against the aggregate."""
        if statement.kind != "count":
            return False
        stated = statement.amount if statement.amount is not None else stated_number(statement.text)
        if stated is None:
            return False
        usable = [a for a in AnswerContract._cited(answer, by_id) if a.exact() or (labelled and a.labelled_backing())]
        if population_frame(answer.answer, statement.start, statement.end):
            return any(float(stated) == float(a.result.denominator) for a in usable) or any(
                float(stated) in AnswerContract._aggregate_values(a, answer, statement) for a in usable
            )
        return any(float(stated) in AnswerContract._aggregate_values(a, answer, statement) for a in usable)

    @staticmethod
    def _cited_number(statement: Statement, answer: Answer, by_id: dict[str, AggregateEvidence]) -> bool:
        """Staging round 5 (N12, ``annotate``): an unbacked ``count`` statement whose number is a value
        of an aggregate the answer cites (citations or claims, any exactness) rests on supplied
        evidence even when its paragraph carries no inline marker: it is kept with its limitation
        note, as when the marker sits in the same sentence, instead of being removed as uncited."""
        if statement.kind != "count":
            return False
        stated = statement.amount if statement.amount is not None else stated_number(statement.text)
        if stated is None:
            return False
        return any(float(stated) in AnswerContract._aggregate_values(a, answer, statement) for a in AnswerContract._cited(answer, by_id))

    @staticmethod
    def _count_target(aggregates: list[AggregateEvidence], intents: Intents) -> AggregateEvidence | None:
        """The exact count that answers a count question (2026-11-09, corpus-B B5 q023): the question
        asks one aggregate kind, ``count``, and every exact ``scalar_count`` aggregate states the
        same number; the first of them. ``None`` otherwise (two exact counts that differ answer
        different claims, so neither corrects the other)."""
        if intents.kinds() != ["count"]:
            return None
        exact = [a for a in aggregates if a.scalar_count and a.exact() and isinstance(a.result.value, (int, float)) and not isinstance(a.result.value, bool)]
        if len({float(a.result.value) for a in exact}) != 1:  # type: ignore[arg-type]
            return None
        return exact[0]

    @staticmethod
    def _contradicts(text: str, start: int, end: int, answer: Answer, target: AggregateEvidence, statement: Statement | None = None) -> bool:
        """A stated count (``text``) that differs from every number of ``target`` and is not a
        population ("out of 1,709 records")."""
        stated = stated_number(text)
        if stated is None or population_frame(answer.answer, start, end):
            return False
        probe = statement or Statement(kind="count", text=text, start=start, end=end)
        return float(stated) not in AnswerContract._aggregate_values(target, answer, probe)

    @staticmethod
    def _examples_problem(claim: Claim, by_id: dict[str, AggregateEvidence]) -> str | None:
        """Why an ``examples`` claim is not a bounded sample of records (rule 6), else ``None``."""
        records = [e for e in claim.evidence_ids if e not in by_id]
        if not records:
            return "an examples claim cites no record evidence"
        if len(set(claim.evidence_ids)) > MAX_EXAMPLES:
            return f"an examples claim cites more than {MAX_EXAMPLES} evidence ids; a sample is bounded"
        for match in _COMPLETENESS.finditer(claim.text):
            if not completeness_disclaimed(claim.text, match.start(), match.end()):
                return "an examples claim states completeness; a complete list needs an exhaustive_list claim on an exact scope"
        return None

    def _findings(
        self,
        answer: Answer,
        *,
        evidence_ids: set[str],
        aggregates: list[AggregateEvidence],
        intents: Intents,
        statements: list[Statement],
        unknown_inline: Sequence[str] = (),
    ) -> list[_Finding]:
        findings: list[_Finding] = []
        by_id = {a.id: a for a in aggregates}
        known = set(evidence_ids) | set(by_id)
        # an id-shaped inline marker ("[ticket:9]") with a known prefix that names no supplied evidence
        # is an unknown citation too (2026-11-09: inline markers count as citations)
        # 2026-11-09 (cutover): a record id a supplied SQL aggregate lists as a contributor of one of its
        # rows ("records: ticket:1, ticket:2") is backed by that aggregate; citing it is not unknown.
        contributors = {cid for a in aggregates for row in a.rows for cid in (row.get("contributor_ids") or []) if isinstance(cid, str)}
        unknown = sorted((((set(answer.citations) | {e for c in answer.claims for e in c.evidence_ids}) - known) | set(unknown_inline)) - contributors)
        if unknown:
            findings.append(_Finding(violation=Violation(rule="unknown_evidence", detail=f"cited evidence not supplied: {unknown}")))
        exact_exists = any(a.exact() for a in aggregates)
        text_labelled = has_label(answer.answer)

        def claim_labelled(claim: Claim) -> bool:
            return has_label(claim.text) or text_labelled

        target = self._count_target(aggregates, intents)
        corrected_claims: list[int] = []
        for index, claim in enumerate(answer.claims):
            if claim.kind not in AGGREGATE_CLAIMS or self._backs(claim, by_id, claim.kind, claim_labelled(claim)):
                continue
            if claim.kind == "count" and target is not None and self._contradicts(claim.text, 0, len(claim.text), Answer(answer=claim.text), target):
                corrected_claims.append(index)  # B5 q023: a number the exact count of the question contradicts
                continue
            needs = "an exact, exhaustive and empty" if claim.kind == "absence" else "an exact"
            findings.append(_Finding(violation=Violation(rule="claim_without_exact_scope", detail=f"{claim.kind} claim needs {needs} ScopeResult"), claims=[index]))
        valid_examples = False
        for index, claim in enumerate(answer.claims):
            if claim.kind != "examples":
                continue
            problem = self._examples_problem(claim, by_id)
            if problem is None:
                valid_examples = True
            else:
                findings.append(_Finding(violation=Violation(rule="examples_claim_invalid", detail=problem), claims=[index]))
        exact_count = any(c.kind == "count" and any(by_id[e].exact() for e in c.evidence_ids if e in by_id) for c in answer.claims)
        sample_list = valid_examples and exact_count and bool(_EXAMPLE_FRAME.search(answer.answer))
        if intents.aggregate() and not exact_exists:
            # a labelled count on an approximate/bounded aggregate is a usable answer (fix5)
            offending = [i for i, c in enumerate(answer.claims) if c.kind in {"count", "exhaustive_list"} and not self._backs(c, by_id, c.kind, claim_labelled(c)) and i not in corrected_claims]
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
            if statement.kind == "exhaustive_list" and statement.text == ENUMERATED and sample_list:
                continue  # rule 6: an enumerated sample next to an exact count, framed as examples
            accepted = ("absence", "count") if statement.kind == "absence" else (statement.kind,)
            if any(c.kind in accepted and self._backs(c, by_id, statement.kind, labelled[i]) for c in answer.claims):
                continue
            if self._implicitly_backed(statement, answer, by_id, labelled[i]):
                continue  # B4: the number is a value of a cited exact aggregate (no typed claim needed)
            unbacked.append(statement)
        mismatched = [st for i, st in enumerate(statements) if st.kind == "count" and st not in unbacked and not self._number_matches(st, answer, by_id, labelled[i])]
        # 2026-11-09 (B5 q023): an unbacked count statement whose number the one exact count of a
        # count question contradicts is a wrong number for the same claim: corrected (strip and
        # annotate alike), never kept with a limitation note
        contradicted = [st for st in unbacked if st.kind == "count" and target is not None and self._contradicts(st.text, st.start, st.end, answer, target, st)]
        unbacked = [st for st in unbacked if st not in contradicted]
        # QQ-06: the model's insufficient_evidence flag does not exempt the text from these checks.
        if mismatched:
            findings.append(
                _Finding(
                    violation=Violation(rule="answer_number_mismatch", detail="the count stated in the answer text differs from the value of the exact aggregate its count claim cites"),
                    statements=mismatched,
                )
            )
        if contradicted or corrected_claims:
            findings.append(
                _Finding(
                    violation=Violation(rule="answer_number_mismatch", detail="the count stated in the answer differs from the exact count of the question"),
                    statements=contradicted,
                    claims=corrected_claims,
                    correction=target.id if target is not None else None,
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
        question: str | None = None,
    ) -> list[Violation]:
        known = set(evidence_ids) | {a.id for a in aggregates}
        answer, unknown_inline = with_inline_citations(answer, known)
        kept = result_statements(statements or [], answer, question, aggregates)
        return [f.violation for f in self._findings(answer, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=kept, unknown_inline=unknown_inline)]

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
        rendered = "\n\n".join(parts)
        presentation = "\n\n".join(markdown_rows(a) + part if a.rows and not refused else part for a, part in zip(aggregates, parts))
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
            answer=presentation + (f"\n\n{kept}" if kept else ""),
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
        unit nouns and quotations (``statements.py``), and the question's own thresholds
        (``result_statements``). Inline ``[id]`` markers of supplied evidence count as citations."""
        flagged = self._flagged(aggregates, intents, unsupported)
        known = set(evidence_ids) | {a.id for a in aggregates}
        answer, unknown_inline = with_inline_citations(answer, known)
        detected = detect_statements(
            answer.answer,
            units=units_for(targets, question),
            list_intent=intents.asks_list(),
            passages=passages,
            fields=targets,
        )
        statements = result_statements(detected, answer, question, aggregates)
        findings = self._findings(answer, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=statements, unknown_inline=unknown_inline)
        notes: list[str] = []
        if self.on_violation == "annotate":
            outcome, result_answer, notes = self._annotate(answer, findings, statements, aggregates, known)
        else:
            outcome, result_answer = self._apply(answer, findings, statements, aggregate_question=intents.asks_aggregate())
        if outcome == "abstained" and self.on_violation != "fail" and any(f.violation.rule == "answer_number_mismatch" for f in findings):
            rendered = self._rendered(answer, aggregates, statements)
            if rendered is not None:
                checked = result_statements(
                    detect_statements(rendered.answer, units=units_for(targets, question), list_intent=intents.asks_list(), passages=passages, fields=targets),
                    rendered,
                    question,
                    aggregates,
                )
                if not self._findings(rendered, evidence_ids=evidence_ids, aggregates=aggregates, intents=intents, statements=checked):
                    # The model's numbers are replaced by the aggregate's own rendering (the numbers
                    # of an aggregate answer come from the aggregate); recorded as a strip.
                    outcome, result_answer, notes = "stripped", rendered, []
        violations = [f.violation.model_copy(update={"action": _action(f, outcome)}) for f in findings]
        if trace is not None:
            trace.event(
                "answer_contract",
                violations=[v.model_dump() for v in violations],
                unsupported=flagged,
                on_violation=self.on_violation,
                exact_scope=any(a.exact() for a in aggregates),
                statements=[s.kind for s in statements],
                outcome=outcome,
                limitations=len(notes),
            )
        if not findings:
            return AnswerResult(answer=answer, unsupported_constraints=flagged, coverage=coverage)
        if self.on_violation == "fail":
            raise AnswerContractViolation(violations)
        if outcome in {"stripped", "annotated"}:
            return AnswerResult(answer=result_answer, unsupported_constraints=flagged, coverage=coverage, violations=violations, contract=outcome, limitations=notes)  # type: ignore[arg-type]
        abstained = Answer(
            answer="Insufficient evidence for the requested claim: " + "; ".join(dict.fromkeys(v.rule for v in violations)) + ".",
            claims=[],
            citations=[],
            insufficient_evidence=True,
        )
        return AnswerResult(answer=abstained, unsupported_constraints=flagged, coverage=coverage, violations=violations, contract="abstained")

    def _annotate(
        self, answer: Answer, findings: list[_Finding], statements: list[Statement], aggregates: list[AggregateEvidence], known: set[str]
    ) -> tuple[str, Answer, list[str]]:
        """``on_violation: annotate`` (opt-in, 2026-11-09; the earlier service's behaviour): keep the
        generator's evidence-cited answer and append one deterministic limitation note per violated
        claim or statement instead of stripping it or abstaining. Hard guarantees stay:

        * unknown evidence (also an unknown inline ``[id]``), a factual answer without citations and
          an empty answer abstain, as in every mode;
        * a stated number that differs from the exact aggregate its claim cites
          (``answer_number_mismatch``) is removed with its sentence and the exact result's rendering
          is stated in the note (the correction); nothing is left -> the rendered fallback or abstain;
        * an aggregate claim that cites no evidence, an ``examples`` claim that cites no record, and
          an unbacked statement whose paragraph cites no supplied evidence are removed (never an
          uncited claim about records); each removal is noted.

        Returns ``(outcome, answer, notes)``; outcome ``annotated`` (or ``abstained``)."""
        if not findings:
            return "passed", answer, []
        if any(f.violation.rule in _HARD_RULES for f in findings):
            return "abstained", answer, []
        by_id = {a.id: a for a in aggregates}
        notes: list[str] = []
        drop_claims: set[int] = set()
        spans: list[tuple[int, int]] = []
        add_citation: str | None = None
        for finding in findings:
            rule = finding.violation.rule
            if rule == "answer_number_mismatch" and finding.correction in by_id and by_id[finding.correction].rendering:
                add_citation = finding.correction
            if rule == "answer_number_mismatch":
                drop_claims.update(finding.claims)  # B5 q023: a count claim the exact count contradicts
                corrected = by_id.get(finding.correction or "")
                if finding.claims and not finding.statements:
                    notes.append(self._correction_note(corrected))
                for statement in finding.statements:
                    spans.append((statement.start, statement.end))
                    exact = [a for a in self._cited(answer, by_id) if a.exact() and a.rendering]
                    if corrected is not None and corrected.rendering:
                        notes.append(self._correction_note(corrected))
                        add_citation = corrected.id
                    elif exact:
                        shown = " ".join(f"{labelled(a)} [{a.id}]" for a in exact[:3])
                        notes.append(f"A stated count that differs from the exact result was removed; the exact result is: {shown}")
                    else:
                        notes.append("A stated count that differs from the exact result was removed.")
            elif rule == "examples_claim_invalid":
                for index in finding.claims:
                    claim = answer.claims[index]
                    if all(e in by_id for e in claim.evidence_ids):
                        drop_claims.add(index)  # a sample that cites no record: removed, never uncited
                        notes.append(NOTE_REMOVED)
                    else:
                        notes.append(NOTE_SAMPLE)
            elif rule in {"claim_without_exact_scope", "aggregate_intent_without_scope"}:
                for index in finding.claims:
                    claim = answer.claims[index]
                    if not [e for e in claim.evidence_ids if e in known]:
                        drop_claims.add(index)
                        notes.append(NOTE_REMOVED)
                    elif claim.kind == "count" and stated_number(claim.text) == 0:
                        drop_claims.add(index)  # B5 q041: a zero is never stated without an exact scope
                        notes.append(NOTE_ZERO)
                    else:
                        notes.append(limitation_note(claim.kind, claim.text))
            elif rule == "answer_text_without_exact_scope":
                for statement in finding.statements:
                    if statement.kind == "count" and stated_number(statement.text) == 0:
                        spans.append((statement.start, statement.end))  # B5 q041: no bounded "0"
                        notes.append(NOTE_ZERO)
                    elif cites_known(paragraph_of(answer.answer, statement.start), known) or self._cited_number(statement, answer, by_id):
                        notes.append(limitation_note(statement.kind, None if statement.text == ENUMERATED else statement.text))
                    else:
                        spans.append((statement.start, statement.end))
                        notes.append(NOTE_REMOVED)
        text = answer.answer
        if spans:
            text = strip_sentences(text, spans)
        claims = [c for i, c in enumerate(answer.claims) if i not in drop_claims]
        cited = {e for c in claims for e in c.evidence_ids}
        dropped_only = {e for i in drop_claims for e in answer.claims[i].evidence_ids} - cited
        citations = [c for c in answer.citations if c not in dropped_only or c in text]
        if add_citation is not None and add_citation not in citations:
            citations.append(add_citation)  # the correction note states and cites the exact count
        if not text.strip():
            return "abstained", answer, []
        if not citations and not answer.insufficient_evidence:
            return "abstained", answer, []
        notes = list(dict.fromkeys(notes))
        block = "\n\n" + LIMITATIONS_HEADING + "\n" + "\n".join(f"- {n}" for n in notes)
        return "annotated", answer.model_copy(update={"answer": text + block, "claims": claims, "citations": citations}), notes

    @staticmethod
    def _correction_note(corrected: AggregateEvidence | None) -> str:
        if corrected is not None and corrected.rendering:
            return f"A stated count that differs from the exact count of the question was removed; the exact result is: {labelled(corrected)} [{corrected.id}]"
        return "A stated count that differs from the exact result was removed."

    @staticmethod
    def _rendered(answer: Answer, aggregates: list[AggregateEvidence], statements: list[Statement]) -> Answer | None:
        """The answer rebuilt from the deterministic renderings of the exact aggregates the model
        cited (all exact rendered aggregates when it cited none), followed by the model's
        sentences that state no number. ``None`` when no exact aggregate has a rendering."""
        cited = {e for c in answer.claims for e in c.evidence_ids} | set(answer.citations)
        usable = [a for a in aggregates if a.exact() and a.rendering]
        # Never an aggregate the model did not cite while it cited an exact one (F1): a cited exact
        # aggregate without a rendering means no rendered answer, not another aggregate's numbers.
        if any(a.exact() and a.id in cited for a in aggregates):
            chosen = [a for a in usable if a.id in cited]
        else:
            chosen = usable
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
        strippable = {"claim_without_exact_scope", "aggregate_intent_without_scope", "examples_claim_invalid"}
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
            text = strip_sentences(text, spans)
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


# A count statement about listed/retained ids ("8 contributing document IDs"), not about records.
_ID_COUNT = re.compile(
    r"[^\d.!?\n]{0,24}?(?:\d[\d,]*|\b(?:" + "|".join(w for w in _WORD_VALUES if " " not in w) + r"))\s+(?:[\w-]+\s+){0,2}?(?:ids?|identifiers?)\b",
    re.I,
)
_ROW_KEYS = frozenset({"evidence", "document_id", "values", "types", "nulls", "citations", "rejected", "tie", "contributor_ids"})


def row_values(row: dict[str, Any]) -> dict[str, Any]:
    """The cells of a result row: a SQL/typed row's ``values`` (its other keys are citations and
    flags), a flat row (log tools) as is."""
    values = row.get("values")
    return values if isinstance(values, dict) and set(row) <= _ROW_KEYS else row


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


# Markdown structure of one line (QA-20261109-08): a blockquote/heading/list-item prefix, then an
# optional bold label that ends in a colon ("**Check the connection:**", "**Status**:").
_LINE_PREFIX = re.compile(r"^[ \t]*(?:>[ \t]*)*(?:#{1,6}[ \t]+|(?:[-*+•]|\d+[.)])[ \t]+)?")
_LINE_LABEL = re.compile(r"(?:\*\*[^*\n]{1,120}?(?::\*\*|\*\*:)|__[^_\n]{1,120}?(?::__|__:))[ \t]*")
_TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
_TABLE_RULE = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*:?-*:?[ \t]*\|?[ \t]*$")


def _kept(body: str, offset: int, spans: Sequence[tuple[int, int]]) -> str:
    """``body`` (at ``offset`` in the answer) without the sentences that overlap ``spans``."""
    return "".join(m.group(0) for m in _SENTENCE.finditer(body) if m.group(0) and not any(a < offset + m.end() and offset + m.start() < b for a, b in spans))


def strip_sentences(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """``text`` without the sentences that overlap ``spans``, keeping the Markdown structure
    (QA-20261109-08). Line by line: a line no span touches is kept verbatim; otherwise its list
    marker, heading/blockquote prefix and a bold ``**Label:**`` are kept around the remaining
    sentences, and a line with no sentence left (or whose label itself carries the span) is dropped
    whole, never left as a bare marker or a dangling label. A table row keeps its cells: each cell
    loses its overlapping sentences, and a row whose cells all end up empty is dropped. The result
    is stripped; three or more line breaks left by a dropped line become two."""
    if not spans:
        return text.strip()
    out: list[str] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        start, content = offset, line.rstrip("\r\n")
        newline = line[len(content) :]
        offset += len(line)
        end = start + len(content)
        if not any(a < end and start < b for a, b in spans) or not content.strip():
            out.append(line)
            continue
        if _TABLE_ROW.match(content) and not _TABLE_RULE.match(content):
            cells, position = [], start
            for part in content.split("|"):
                cells.append((part, position))
                position += len(part) + 1
            inner = cells[1:-1]
            rebuilt = [" " + _kept(part, at, spans).strip() + " " if part.strip() else part for part, at in inner]
            if not any(cell.strip() for cell in rebuilt):
                continue  # every cell of the row was a stripped sentence
            out.append(cells[0][0] + "|" + "|".join(rebuilt) + "|" + cells[-1][0] + newline)
            continue
        prefix = _LINE_PREFIX.match(content).group(0)  # type: ignore[union-attr]
        label_match = _LINE_LABEL.match(content, len(prefix))
        label = label_match.group(0) if label_match else ""
        head = len(prefix) + len(label)
        if any(a < start + head and start < b for a, b in spans):
            continue  # the label itself carries the statement: the item goes as a whole
        body = _kept(content[head:], start + head, spans).strip()
        if not body:
            continue  # nothing left after the marker/label: drop the whole line
        out.append(prefix + label + body + newline)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def sentence_of(text: str, start: int, end: int) -> str:
    """The sentence(s) of ``text`` that overlap ``[start, end)``."""
    parts = [m.group(0) for m in _SENTENCE.finditer(text) if m.group(0) and m.start() < end and start < m.end()]
    return "".join(parts) or text[start:end]


def numbers_outside(text: str, reference: str) -> list[str]:
    """The numbers (digits or number words) of ``text`` whose value is not a number of
    ``reference``: a narrative around a rendering may only restate the rendering's numbers."""
    allowed = set(_numbers(reference).values())
    return [token for token, value in _numbers(text).items() if value not in allowed]


def markdown_rows(aggregate: AggregateEvidence) -> str:
    """A bounded table of actual tool rows for API/MCP consumers; prose retains full scope notes."""
    rows = [row_values(row) for row in aggregate.rows[:10]]
    columns = list(dict.fromkeys(key for row in rows for key in row))
    if rows and all({"tenant", "error_type", "error_count"} <= row.keys() for row in rows):
        columns = ["tenant", "error_type", "error_count"]
    elif rows and all({"service", "error_type", "error_count"} <= row.keys() for row in rows):
        columns = ["service", "error_type", "error_count"]
    if aggregate.columns:
        columns = [c for c in aggregate.columns if any(c in row for row in rows)]
    if not columns:
        return ""

    def cell(value: Any) -> str:
        text = "—" if value is None else str(value)
        text = escape(" ".join(text.split()), quote=False)
        return re.sub(r"([\\`*_{}\[\]()#+.!|~>\-])", r"\\\1", text)

    lines = [f"Result rows ({aggregate.result.exactness}) [{aggregate.id}]", "",
             "| " + " | ".join(cell("client" if c == "tenant" and "error_count" in columns else c.replace("_", " ")) for c in columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    lines.extend("| " + " | ".join(cell(row.get(c)) for c in columns) + " |" for row in rows)
    if len(aggregate.rows) > len(rows):
        lines.extend(["", f"Showing {len(rows)} of {len(aggregate.rows)} returned rows; full rows are in the structured result."])
    return "\n".join(lines) + "\n\n"


def labelled(aggregate: AggregateEvidence) -> str:
    """An aggregate's rendering with its exactness label (appended when the rendering lacks it)."""
    text = (aggregate.rendering or "").strip()
    label = aggregate.result.exactness
    if re.search(rf"\b{label}\b", text):
        return text
    return f"{text.rstrip('.')} ({label})."


# --- 2026-11-09 (corpus-B B4): inline citations, result-shape/threshold statements, annotate notes ---

_HARD_RULES = frozenset({"unknown_evidence", "factual_without_citations", "empty_answer"})
LIMITATIONS_HEADING = "Limitations:"
NOTE_REMOVED = "A statement that cited no supplied evidence was removed."
NOTE_SAMPLE = "These examples may not be the complete set."
# 2026-11-09 (B5 q041): a zero count is an absence claim; without an exact scope it is removed
NOTE_ZERO = "A zero count that no exact result backs was removed; the scope did not express the whole question."
_NOTES = {
    "count": "I can't verify this count is exact for all records",
    "exhaustive_list": "This list may not be complete",
    "absence": "I can't confirm that no matching records exist",
}
_INLINE = re.compile(r"\[([^\[\]\n]{1,200})\](?!\()")  # "[aggregate:1]"; a markdown link "[text](url)" is not one
_INLINE_ID = re.compile(r"^[A-Za-z][\w.-]*:[^\s\[\],;]+$")
# A count statement whose unit is a result row ("1 row", "3 result rows"): the size of a cited result.
_ROW_UNIT = re.compile(r"\b(?:rows?|results?)\s*$", re.I)
# A comparison right before a stated number: a threshold ("at least 20 tickets", ">= 20 tickets").
_THRESHOLD_BEFORE = re.compile(
    r"(?:\bat\s+least|\bat\s+most|\b(?:no\s+)?(?:more|fewer|less)\s+than|\bover|\bunder|\b(?:a\s+)?(?:minimum|maximum)\s+of|\bup\s+to|>=|<=|≥|≤|>|<)\s*$",
    re.I,
)
_REQUESTED_N_BEFORE = re.compile(r"\b(?:next|top|first|last)\s+$", re.I)
_PARAGRAPH = re.compile(r"\n\s*\n")
# A count of evidence items, not of records: "two aggregate results", "one exact count that matches".
_EVIDENCE_COUNT = re.compile(r"^\S+\s+(?:[\w-]+\s+){0,2}?(?:aggregates?|counts?|results?|quer(?:y|ies))\b", re.I)
_QUESTION_QUOTED = re.compile(r"\"([^\"]{2,200})\"|'([^']{2,200})'|“([^”]{2,200})”")


def _inline_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _INLINE.finditer(text):
        tokens.extend(t for t in re.split(r"[,;]\s*|\s+", match.group(1).strip()) if t)
    return tokens


def inline_ids(text: str, known: set[str]) -> tuple[list[str], list[str]]:
    """``(known ids cited inline, unknown id-shaped markers)`` of ``text``: ``[aggregate:1]``,
    ``[ticket:7, ticket:9]``. A marker counts as an unknown citation only when it is id-shaped
    (``prefix:rest``) and its prefix is the prefix of a supplied id ("[aggregate:7]" when only
    ``aggregate:1`` was supplied); other bracketed text is prose."""
    prefixes = {k.split(":", 1)[0] for k in known if ":" in k}
    found: list[str] = []
    unknown: list[str] = []
    for token in _inline_tokens(text):
        token = token.strip().rstrip(".")
        if token in known:
            found.append(token)
        elif _INLINE_ID.match(token) and token.split(":", 1)[0] in prefixes:
            unknown.append(token)
    return list(dict.fromkeys(found)), list(dict.fromkeys(unknown))


def with_inline_citations(answer: Answer, known: set[str]) -> tuple[Answer, list[str]]:
    """The answer with its inline ``[id]`` markers of supplied evidence merged into ``citations``
    (2026-11-09, B4 q043: a reply that cited inline but left ``citations`` empty is cited), and the
    unknown id-shaped markers (``unknown_evidence``)."""
    found, unknown = inline_ids(answer.answer, known)
    extra = [i for i in found if i not in answer.citations]
    if extra:
        answer = answer.model_copy(update={"citations": [*answer.citations, *extra]})
    return answer, unknown


def cites_known(text: str, known: set[str]) -> bool:
    return bool(inline_ids(text, known)[0])


def paragraph_of(text: str, position: int) -> str:
    """The blank-line-separated block of ``text`` around ``position``."""
    starts = [m.end() for m in _PARAGRAPH.finditer(text) if m.end() <= position]
    start = starts[-1] if starts else 0
    after = _PARAGRAPH.search(text, position)
    return text[start : after.start() if after else len(text)]


def _question_numbers(question: str | None) -> set[float]:
    if not question:
        return set()
    numbers = {float(m.group(0).replace(",", "")) for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", question)}
    numbers.update(float(_WORD_VALUES[w.casefold()]) for w in re.findall(r"[A-Za-z]+", question) if w.casefold() in _WORD_VALUES)
    return numbers


def result_statements(statements: list[Statement], answer: Answer, question: str | None, aggregates: Sequence[AggregateEvidence]) -> list[Statement]:
    """The statements the contract binds, without the ``count`` statements that state no record
    count (2026-11-09, corpus-B B4 q011/q042/q043/q045 shapes):

    * the size of a cited result ("1 row", "3 rows") when it equals a cited aggregate's returned
      row count;
    * the question's own threshold restated after a comparison ("with at least 20 tickets" when the
      question says ">= 20 tickets") or its requested N ("the next two organizations" for "the next
      two highest ..."): the number must occur in the question;
    * a row-count parameter of advice ("Advice: rerun ... and return at least two rows");
    * the number of ids a cited aggregate retains ("8 contributing record IDs");
    * a number inside a phrase the question quotes ("'500 Internal Server Error'") and a count of
      the cited aggregates themselves ("two aggregate results").

    Every other count statement is still checked (a number not in the question is never exempt)."""
    text = answer.answer
    asked = _question_numbers(question)
    by_id = {a.id: a for a in aggregates}
    cited_ids = dict.fromkeys([*answer.citations, *(e for c in answer.claims for e in c.evidence_ids)])
    sizes = {float(len(by_id[e].rows)) for e in cited_ids if e in by_id and by_id[e].rows}
    cited = [by_id[e] for e in cited_ids if e in by_id]
    retained = {float(len(a.result.matched_ids)) for a in cited if a.result.matched_ids}
    quoted = [q.casefold() for q in _QUESTION_QUOTED.findall(question or "") for q in q if q]
    kept: list[Statement] = []
    for statement in statements:
        number = statement.amount if statement.amount is not None else stated_number(statement.text)
        if statement.kind != "count" or number is None:
            kept.append(statement)
            continue
        before = text[max(0, statement.start - 24) : statement.start]
        threshold = bool(_THRESHOLD_BEFORE.search(before))
        if float(number) in asked and (threshold or _REQUESTED_N_BEFORE.search(before)):
            continue
        if _ID_COUNT.match(text, statement.start) and float(number) in retained:
            continue  # "eight contributing record IDs": the ids an aggregate retains, whatever its exactness
        if any(statement.text.casefold() in phrase for phrase in quoted):
            continue  # "500 Internal Server Error": part of a phrase the question quotes, not a count
        if _EVIDENCE_COUNT.search(statement.text) and float(number) == float(len(cited)):
            continue  # "two aggregate results", "one exact count that matches": the cited aggregates
        if _ROW_UNIT.search(statement.text):
            if float(number) in sizes:
                continue
            sentence = sentence_of(text, statement.start, statement.end)
            if threshold and (_ADVICE_START.match(sentence) or _ADVISORY.search(sentence)):
                continue
        kept.append(statement)
    return kept


def limitation_note(kind: str, text: str | None) -> str:
    """The deterministic limitation note of an annotated claim/statement (``annotate`` mode)."""
    base = _NOTES.get(kind, "I can't verify this statement against an exact result")
    if not text:
        return base + "."
    snippet = " ".join(text.split())
    snippet = snippet if len(snippet) <= 80 else snippet[:77] + "..."
    return f'{base}: "{snippet}".'


def _action(finding: _Finding, outcome: str) -> str:
    if outcome == "annotated":
        return "strip" if finding.violation.rule == "answer_number_mismatch" else "annotate"
    return "strip" if outcome == "stripped" else "abstain"


def answer_from_generator(data: dict[str, Any]) -> Answer:
    """Validate a generator reply; any ``unsupported_constraints`` key is a schema violation."""
    return Answer.model_validate(data)
