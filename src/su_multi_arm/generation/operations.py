"""The operations half of a two-halves answer (staging round 5, N14; docs/plans/unified_merge.md §6.11).

On the unified collection a question with an operations half gets typed log evidence beside the
knowledge evidence. In the private round-5 evaluation the retrieval built the right operations half
for every owner question, but the generator dropped it, restated it wrongly ("has halted" from an
earlier record), presented earlier records as current incidents, or tied failure counts to a cause.
This module is the generator contract for that half; ``Engine._answer`` applies it:

1. **Rendered, never written by the model.** ``OperationsSection`` is built from the operations
   aggregates (their deterministic renderings and result rows, as the pure-aggregate answer shows
   them: ``contract.labelled`` / ``contract.markdown_rows``) and the operations half's own notes:
   window, question clock, services read, refusals and limitation reasons (``OperationsHalf``
   notes and unsupported constraints), not-served analytics. It is shown verbatim, first, under its
   own label (``Current <operation sources>:``), opened by one reading line (``header_line``,
   staging round 6): as-of, window, question clock, the services read and the tenant, from the
   aggregates' bound arguments and coverage.
2. **The generator writes the other half only.** The operations aggregates are not in its
   evidence; it receives the section as ``operations_shown`` (shown to the reader, not citable, ids
   removed) with a domain-neutral rule (``profiles/prompts.OPERATIONS_RULE``). Its text follows under
   ``Past <knowledge sources>:``.
3. **Deterministic post-checks** on the model's text, claims and short answer (``check``), before
   the answer contract runs:

   * ``operations_claim_unbacked``: a sentence that asserts a current state (halted / stopped /
     stuck, an outage / disruption / downtime, a recovery) or a cause of the current problem,
     while no operations evidence item states it (a halted or recovered row of a liveness reading;
     an outage or a cause is never established by the typed log intents), is removed. Negated
     ("no outage is established") and hypothetical ("crawls can stop when ...") sentences are not
     claims;
   * ``records_presented_as_current``: a sentence or heading that presents the other evidence as
     current or recent incidents ("Recent incidents", "customers are currently experiencing") is
     removed;
   * ``operations_restated``: a sentence that cites an operations aggregate is removed (the section
     already shows it verbatim; the model's restatement is where the misstatements were).

   Under ``on_violation: annotate`` each removal is also stated as a limitation; ``fail`` raises.
   Nothing is repaired by rewording: a flagged sentence goes, the rendered evidence stays.

The section never depends on the model: when the generator abstains or fails, the operations
evidence is still shown. English-scoped patterns, like ``statements.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from ..models import Answer, Claim, Violation
from .contract import _SENTENCE, AggregateEvidence, labelled, markdown_rows, strip_sentences

# --- the post-check patterns ------------------------------------------------------------------------

# A current-state family -> its terms. ``halt``/``recovery`` can be stated by an operations item
# (a liveness row); ``outage``/``cause`` are never established by the typed log intents.
_STATE: dict[str, re.Pattern[str]] = {
    "halt": re.compile(r"\b(?:halt(?:ed|s|ing)?|stopped(?:\s+running)?|stall(?:ed|s)?|stuck|hung|not\s+running|no\s+longer\s+running)\b", re.I),
    "outage": re.compile(r"\b(?:outages?|(?:service\s+)?disruptions?|downtime|(?:is|are|was|were|went|been)\s+down|unavailable|degraded)\b", re.I),
    "recovery": re.compile(
        r"\b(?:recover(?:ed|s|y)|restored|back\s+(?:up|online|to\s+normal)|(?:fixed|resolved|healed|cleared)\s+(?:itself|automatically|on\s+its\s+own|by\s+itself)|auto(?:matically|-)?\s*(?:fixed|recovered|resolved))\b",
        re.I,
    ),
    "cause": re.compile(r"\b(?:root\s+cause|caused\s+by|because\s+of|due\s+to|(?:is|are|was|were)\s+(?:the|a)\s+(?:cause|reason)|explains?|explained\s+by|led\s+to|result(?:ed|s)?\s+(?:of|from)|responsible\s+for)\b", re.I),
}
# Present-tense / confirmation framing: the sentence asserts the state of the subject now.
_ASSERTED = re.compile(
    r"^\W*yes\b|\b(?:confirm(?:s|ed)?|shows?\s+that|indicates?\s+that|evidence\s+(?:of|that))\b"
    r"|\b(?:is|are|has|have|has\s+been|have\s+been)\s+(?:\w+\s+){0,3}?(?:halt|stop|stall|stuck|hung|not\s+running|down|unavailable|degraded|disrupt|recover|restored|back|fixed|resolved|caused|due|because|the\s+cause|the\s+reason)",
    re.I,
)
_CURRENT = re.compile(
    r"\b(?:current(?:ly)?|right\s+now|at\s+the\s+moment|now|ongoing|today|this\s+(?:morning|afternoon|week)|recent(?:ly)?|active|live"
    r"|in\s+the\s+(?:last|past)\s+\w+\s+(?:minutes?|hours?|days?))\b",
    re.I,
)
# Negated or hypothetical sentences state no current state.
_NEGATED = re.compile(
    r"\b(?:not|no|never|none|cannot|can't|isn't|aren't|hasn't|haven't|wasn't|weren't|doesn't|don't|didn't|unknown|unclear|unconfirmed|whether|neither)\b",
    re.I,
)
# A recovery "on its own" is the operations question itself ("was it fixed automatically"): a claim
# in any tense.
_AUTO_RECOVERY = re.compile(
    r"\b(?:recover(?:ed|s)?|fixed|resolved|healed|cleared|restarted)\s+(?:itself|automatically|on\s+its\s+own|by\s+itself|without\s+intervention)\b|\bauto(?:matically|-)?\s*(?:fixed|recovered|resolved)\b",
    re.I,
)
_HYPOTHETICAL = re.compile(r"\b(?:may|might|could|can|would|if|possible|possibly|typically|usually|often|sometimes|generally)\b", re.I)
# A sentence that reports what a record says ("users reported that ...") is that record's narrative
# unless it frames it as current.
_ATTRIBUTED = re.compile(r"\b(?:reported|reports|reporting|described|describes|documented|documents|document|according\s+to|mentioned|mentions|noted|notes|stated|states)\b", re.I)
# The other evidence presented as current incidents.
_AS_CURRENT = re.compile(
    r"\b(?:recent|current|ongoing|active|live|today's|new)\s+(?:\w+\s+)?(?:incidents?|outages?|disruptions?|failures?|downtime)\b"
    r"|\b(?:are|is)\s+(?:currently|now|still)\s+(?:experiencing|affected\s+by|facing|seeing)\b|\b(?:currently|now|still)\s+(?:experiencing|affected\s+by|facing)\b",
    re.I,
)
_INLINE_ID = re.compile(r"\[([^\[\]\n]{1,200})\]")
_HEADING = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+\S.*|\*\*[^*\n]{1,120}\*\*:?[ \t]*)$")


def _plural(word: str) -> str:
    return word if word.endswith("s") else word + "s"


def section_labels(operation_sources: Sequence[str], knowledge_sources: Sequence[str]) -> tuple[str, str]:
    """The two halves' labels from configuration: ``Current logs:`` and ``Past tickets/docs:``
    (``Document.source`` names, never a domain default)."""
    current = "/".join(operation_sources) or "operations evidence"
    past = "/".join(_plural(s) for s in knowledge_sources) or "records"
    return f"Current {current}:", f"Past {past}:"


@dataclass
class OperationsSection:
    """The rendered operations half of one answer (module doc)."""

    aggregates: list[AggregateEvidence]
    notes: list[str]
    label: str
    knowledge_label: str
    backing: set[str] = field(default_factory=set)  # state families an operations item states
    header: str | None = None  # the reading line (``header_line``): as-of, window, clock, services, tenant

    @property
    def ids(self) -> set[str]:
        return {a.id for a in self.aggregates}

    def text(self, *, cited: bool = True) -> str:
        """The section shown to the reader (``cited``) or to the generator (ids removed)."""
        parts: list[str] = [f"**{self.label}**", self.header or ""]
        for aggregate in self.aggregates:
            rows = markdown_rows(aggregate) if aggregate.rows else ""
            body = rows + labelled(aggregate) + (f" [{aggregate.id}]" if cited else "")
            parts.append(body if cited else _INLINE_ID.sub("", body))
        if self.notes:
            parts.append("\n".join(f"- {note}" for note in self.notes))
        return "\n\n".join(p.strip() for p in parts if p.strip())

    def claims(self) -> list[Claim]:
        return [Claim(text=a.rendering or a.id, kind="count" if a.exact() and a.scalar_count else "narrative", evidence_ids=[a.id]) for a in self.aggregates]

    # --- the post-checks -------------------------------------------------------------------------

    def flags(self, sentence: str) -> str | None:
        """The rule a sentence of the model's text violates, or ``None``."""
        ids = {t.strip().rstrip(".") for m in _INLINE_ID.finditer(sentence) for t in re.split(r"[,;]\s*|\s+", m.group(1)) if t.strip()}
        if ids & self.ids:
            return "operations_restated"
        if _AS_CURRENT.search(sentence) and not _NEGATED.search(sentence):
            return "records_presented_as_current"
        families = [name for name, pattern in _STATE.items() if pattern.search(sentence)]
        if not families or _NEGATED.search(sentence) or _HYPOTHETICAL.search(sentence):
            return None
        current = bool(_CURRENT.search(sentence))
        if not (_ASSERTED.search(sentence) or current or _AUTO_RECOVERY.search(sentence)):
            return None  # an earlier record's narrative ("the crawl had stopped on 3 May")
        if _ATTRIBUTED.search(sentence) and not current and not _AUTO_RECOVERY.search(sentence):
            return None  # what a record reports ("users reported that reports are stuck")
        if all(name in self.backing for name in families):
            return None
        return "operations_claim_unbacked"

    def check(self, answer: Answer) -> tuple[Answer, list[Violation]]:
        """The model's answer without its flagged sentences, claims and short answer."""
        spans: list[tuple[int, int]] = []
        rules: dict[str, int] = {}
        for match in _SENTENCE.finditer(answer.answer):
            sentence = match.group(0)
            if not sentence.strip():
                continue
            rule = self.flags(sentence)
            if rule is not None:
                spans.append((match.start(), match.end()))
                rules[rule] = rules.get(rule, 0) + 1
        text = drop_empty_headings(strip_sentences(answer.answer, spans)) if spans else answer.answer
        claims = []
        for claim in answer.claims:
            rule = "operations_restated" if set(claim.evidence_ids) & self.ids else self.flags(claim.text)
            if rule is None:
                claims.append(claim)
            else:
                rules[rule] = rules.get(rule, 0) + 1
        short = answer.short_answer
        if short and self.flags(short) is not None:
            rules[self.flags(short)] = rules.get(self.flags(short), 0) + 1  # type: ignore[index]
            short = None
        citations = [c for c in answer.citations if c not in self.ids]
        violations = [Violation(rule=rule, detail=f"{count} statement(s) of the generated text {DETAILS[rule]}", action="strip") for rule, count in rules.items()]  # type: ignore[arg-type]
        if not violations and citations == answer.citations:
            return answer, []
        return answer.model_copy(update={"answer": text, "claims": claims, "citations": citations, "short_answer": short}), violations


DETAILS = {
    "operations_claim_unbacked": "asserted a current halt, outage, recovery or cause that no operations evidence item states",
    "records_presented_as_current": "presented earlier records as current or recent incidents",
    "operations_restated": "restated the operations evidence, which is shown as rendered",
}
NOTES = {
    "operations_claim_unbacked": "A statement about the current state (halted, outage, recovery or cause) that the operations evidence does not establish was removed.",
    "records_presented_as_current": "A statement presenting earlier records as current incidents was removed.",
    "operations_restated": "A restatement of the operations evidence was removed; the operations evidence is shown as recorded.",
}


def drop_empty_headings(text: str) -> str:
    """Drop heading lines (``#`` headings, bold labels) left with no content before the next
    heading or the end of the text."""
    lines = text.splitlines()
    out: list[str] = []
    for index, line in enumerate(lines):
        if _HEADING.match(line):
            rest = next((later for later in lines[index + 1 :] if later.strip()), None)
            if rest is None or _HEADING.match(rest):
                continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _values(arg: Any) -> list[str]:
    if arg is None:
        return []
    items = arg if isinstance(arg, (list, tuple, set)) else [arg]
    return [str(item) for item in items if str(item).strip()]


def _moment(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def header_line(aggregates: Sequence[dict[str, Any]], *, question_timezone: str, log_timezone: str) -> str | None:
    """The one reading line that opens the operations section (staging round 6): as-of, window,
    clock, the services read and the tenant, taken from the executed operations aggregates (their
    bound ``args`` and ``operations_coverage``) only. ``None`` when no aggregate was read (every
    one refused, or none planned): the section's refusal notes say why.

    * **as of**: the latest data time the aggregates report (a provisional day's as-of time, a
      liveness row's ``observed until``); without one, the last day of the latest window (closed
      days only).
    * **window**: every distinct ``start to end`` of the aggregates' windows (the bound ``window``
      text when an aggregate reports none).
    * **clock**: the question clock (``question.timezone``) and, when it differs, the log clock
      (``logs.timezone``) in which the stated times are recorded.
    * **services read**: the union of the bound ``service`` arguments in order; ``all (no service
      filter)`` when an aggregate read without one. **tenant**: only when one was bound.

    Domain-neutral: field names of the typed log intents, no source or service names of its own."""
    read = [a for a in aggregates if a.get("status") != "refused"]
    if not read:
        return None
    moments: list[datetime] = []
    windows: list[str] = []
    ends: list[str] = []
    services: list[str] = []
    tenants: list[str] = []
    unfiltered = False
    for aggregate in read:
        meta = aggregate.get("operations_coverage") or {}
        tiers = meta.get("tiers") or {}
        moments.extend(m for m in map(_moment, (tiers.get("provisional_days") or {}).values()) if m is not None)
        for row in aggregate.get("rows") or []:
            values = (row.get("values") or row) if isinstance(row, dict) else {}
            moment = _moment(values.get("observed_until"))
            if moment is not None:
                moments.append(moment)
        window = meta.get("window") or {}
        start, end = window.get("start"), window.get("end")
        if start and end:
            text = str(start) if start == end else f"{start} to {end}"
            ends.append(str(end))
        else:
            text = str((aggregate.get("args") or {}).get("window") or "").strip()
        if text and text not in windows:
            windows.append(text)
        args = aggregate.get("args") or {}
        bound = _values(args.get("service"))
        if not bound:
            unfiltered = True
        services.extend(s for s in bound if s not in services)
        tenants.extend(t for t in _values(args.get("tenant")) if t not in tenants)
    parts: list[str] = []
    if moments:
        parts.append(f"As of {max(moments).isoformat()}")
    elif ends:
        parts.append(f"As of {max(ends)} (closed days only)")
    if windows:
        parts.append(("window " if len(windows) == 1 else "windows ") + " and ".join(windows))
    parts.append(f"clock {question_timezone}" if question_timezone == log_timezone else f"question clock {question_timezone}, log times in {log_timezone}")
    read_services = [*services, *(["all (no service filter)"] if unfiltered else [])]
    parts.append("services read: " + ", ".join(read_services))
    if tenants:
        parts.append(("tenant: " if len(tenants) == 1 else "tenants: ") + ", ".join(tenants))
    line = "; ".join(parts)
    return line[0].upper() + line[1:] + "."


def backing_of(aggregates: Sequence[dict[str, Any]]) -> set[str]:
    """The current-state families an operations item states: a liveness row (or heartbeat series)
    whose status is ``halted`` (halt) or ``recovered`` (recovery)."""
    out: set[str] = set()
    for aggregate in aggregates:
        history = aggregate.get("history") or {}
        rows = [*(aggregate.get("rows") or []), *(history.get("series") or []), *(history.get("recovery") or [])]
        statuses = {str((row.get("values") or row).get("status") or "") for row in rows if isinstance(row, dict)}
        if "halted" in statuses:
            out.add("halt")
        if "recovered" in statuses:
            out.add("recovery")
    return out


def compose(section: OperationsSection, answer: Answer, *, answered: bool) -> Answer:
    """The final answer: the operations section first, then the model's (checked) text under the
    knowledge label. The section's aggregates are cited and claimed; the answer is
    ``insufficient_evidence`` only when the model's part is and no operations aggregate was
    ``answered`` (every one refused)."""
    knowledge = answer.answer.strip()
    text = section.text() + ("\n\n" + f"**{section.knowledge_label}**" + "\n\n" + knowledge if knowledge else "")
    citations = list(dict.fromkeys([*(a.id for a in section.aggregates), *answer.citations]))
    abstained = answer.insufficient_evidence
    return Answer(
        answer=text,
        claims=[*section.claims(), *answer.claims],
        citations=citations,
        insufficient_evidence=bool(abstained and not answered),
        short_answer=answer.short_answer,
    )


__all__ = ["NOTES", "OperationsSection", "backing_of", "compose", "drop_empty_headings", "header_line", "section_labels"]
