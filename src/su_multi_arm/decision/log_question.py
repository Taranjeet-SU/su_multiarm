"""Question-level grounding of a typed log intent (M0; docs/plans/unified_merge.md §6.11, §9).

Applied after ``log_intents.bind_question_scope`` to every log intent a decision agent selects
(rules, classifier, cascade and the ``llm`` planner alike), deterministically:

* **Default view for outage/presence questions.** A question that asks whether something happened
  ("is there any outage", "any errors", "did ... fail") without naming a period reads the last
  ``logs.collection.serving.default_view_days`` days, not just today; the answer states it.
* **Question clock.** A clock hour without a date ("at 2", "around 14:00", "2 pm") is read in
  ``question.timezone`` (default UTC), on the named day or else at its most recent occurrence, and
  converted to the log partition clock. An hour from 1 to 12 without am/pm is ambiguous: both
  12-hour readings are kept and stated. ``log_around`` runs once per reading; a day-level intent
  covers the readings' days, states that day rollups do not isolate the hour, and its aggregate is
  reported ``bounded`` (``qualifier_not_applied``).
* **Vocabulary.** ``question.vocabulary`` terms ground surface words to indexed service values
  ("crawl" -> the crawler services the deployment lists). Only values the generation has indexed
  are applied; a term whose values are all unindexed refuses the scope (never an unfiltered
  all-service answer). ``symptom`` terms are stated, never filtered on. A service named in the
  question wins over a vocabulary term.

No domain words are built in: the vocabulary is deployment configuration. English-scoped, like
``decision/windows.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..models import UnsupportedConstraint
from .log_intents import INPUT_MODELS, LogIntentCall
from .windows import WindowError, find_windows

PRESENCE = re.compile(
    r"\b(?:is|are|was|were)\s+there\s+(?:any|an?)\b|\bany\s+(?:outages?|downtime|incidents?|failures?|errors?|issues?|problems?)\b"
    r"|\b(?:outages?|downtime)\b|\bdid\s+(?:\S+\s+){0,4}?(?:fail|break|crash|go down|stop)\w*\b",
    re.I,
)
_UNIT_AFTER = r"(?!\s*(?:days?|hours?|hrs?|h\b|minutes?|mins?|seconds?|secs?|weeks?|months?|years?|%|percent|times?|x\b|errors?|requests?|users?|clients?|tenants?|services?|tickets?|records?|lines?|logs?))"
_CLOCK = re.compile(
    r"\b(?P<prep>at|around|about|near)\s+(?P<h>\d{1,2})(?::(?P<m>[0-5]\d))?(?:\s*(?P<ampm>[ap])\.?m\b\.?)?(?![\d:/.-]\d)" + _UNIT_AFTER
    + r"|\b(?P<h2>\d{1,2})(?::(?P<m2>[0-5]\d))?\s*(?P<ampm2>[ap])\.?m\b\.?",
    re.I,
)


@dataclass(frozen=True)
class QuestionSettings:
    """What the binding needs from the deployment (set by the engine on ``Capabilities``)."""

    timezone: str = "UTC"  # question.timezone (the question clock)
    logs_timezone: str = "UTC"  # logs.timezone (the partition clock)
    view_days: int = 3  # logs.collection.serving.default_view_days
    vocabulary: tuple[Any, ...] = ()  # core.config_m0.VocabularyTerm entries
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    not_values: dict[str, frozenset[str]] = field(default_factory=dict)  # logs.intents.not_values (casefold)

    def keep(self, field: str, values: list[str]) -> list[str]:
        """``values`` without the words ``logs.intents.not_values`` excludes for ``field``."""
        drop = self.not_values.get(field)
        return [v for v in values if v.casefold() not in drop] if drop else values


@dataclass(frozen=True)
class ClockReading:
    text: str
    local: tuple[datetime, ...]  # candidates in the question clock
    ambiguous: bool
    dated: bool  # the question named the day

    def utc(self) -> list[datetime]:
        return [c.astimezone(timezone.utc) for c in self.local]

    def statement(self) -> str:
        zone = self.local[0].tzname() if str(self.local[0].tzinfo) == "UTC" else str(self.local[0].tzinfo)
        why = []
        if not self.dated:
            why.append("no date")
        if self.ambiguous:
            why.append("no am/pm")
        head = f'"{self.text}" names ' + " and ".join(why) + ": read" if why else f'"{self.text}" is read'
        readings = []
        for local in self.local:
            utc = local.astimezone(timezone.utc)
            readings.append(f"{local:%Y-%m-%d %H:%M} {zone}" + ("" if local.utcoffset() == timedelta(0) else f" ({utc:%Y-%m-%d %H:%M} UTC)"))
        where = "on the named day" if self.dated else "at its most recent occurrence"
        return f"{head} in the question clock {where} as " + " and as ".join(readings) + "."


def read_clock(question: str, settings: QuestionSettings, as_of: date | None = None) -> ClockReading | None:
    """The first bare clock hour of ``question`` with its candidate readings, or ``None``."""
    match = None
    for found in _CLOCK.finditer(question):
        hour = int(found.group("h") or found.group("h2"))
        ampm = (found.group("ampm") or found.group("ampm2") or "").lower()
        if (ampm and 1 <= hour <= 12) or (not ampm and 0 <= hour <= 23):
            match = found
            break
    if match is None:
        return None
    raw_hour = match.group("h") or match.group("h2")
    hour, minute = int(raw_hour), int(match.group("m") or match.group("m2") or 0)
    ampm = (match.group("ampm") or match.group("ampm2") or "").lower()
    if ampm:
        hours, ambiguous = [hour % 12 + (12 if ampm == "p" else 0)], False
    elif 1 <= hour <= 12 and not raw_hour.startswith("0"):
        hours, ambiguous = sorted({hour % 12, hour % 12 + 12}), True
    else:
        hours, ambiguous = [hour], False
    zone = ZoneInfo(settings.timezone)
    now = settings.now().astimezone(zone)
    day = None
    try:
        windows = find_windows(question, as_of or now.date())
    except WindowError:
        windows = []
    day = next((w.start for w in windows if (w.end - w.start).days == 1), None)
    local = []
    for h in hours:
        if day is not None:
            local.append(datetime.combine(day, time(h, minute), zone))
            continue
        candidate = datetime.combine(now.date(), time(h, minute), zone)
        local.append(candidate if candidate <= now else candidate - timedelta(days=1))
    return ClockReading(text=match.group(0).strip(), local=tuple(local), ambiguous=ambiguous, dated=day is not None)


@dataclass
class Grounding:
    services: list[str] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)  # question words the vocabulary explained
    unindexed: list[str] = field(default_factory=list)  # service terms none of whose values are indexed


def ground_terms(question: str, vocabulary: tuple[Any, ...] | list[Any], known_services: list[str]) -> Grounding:
    """Vocabulary terms named in ``question``: grounded service values (indexed only), symptom
    statements, and the matched words (for literal-term filters to skip)."""
    out = Grounding()
    known = {v.casefold(): v for v in known_services}
    for term in vocabulary:
        hits = [m.group(0) for m in term.pattern().finditer(question)]
        if not hits:
            continue
        out.matched.extend(hits)
        if term.field == "symptom":
            out.statements.append(term.note or f'"{hits[0]}" describes a symptom; it is not a recorded field, so it is not used as a filter.')
            continue
        values = [known[v.casefold()] for v in term.values if v.casefold() in known]
        if not values:
            out.unindexed.append(hits[0])
            continue
        out.services.extend(v for v in values if v not in out.services)
        out.statements.append(f'"{hits[0]}" was read as service {", ".join(values)} (question.vocabulary)' + (f"; {term.note}" if term.note else "") + ".")
    return out


def strip_terms(text: str, vocabulary: tuple[Any, ...] | list[Any]) -> str:
    """``text`` without the words the vocabulary explains (for literal-term matching)."""
    for term in vocabulary:
        text = term.pattern().sub(" ", text)
    return text


@dataclass
class BoundQuestion:
    calls: list[LogIntentCall]
    notes: list[str] = field(default_factory=list)  # stated in the answer, informational
    unapplied: list[UnsupportedConstraint] = field(default_factory=list)  # stated and bounding


def bind_question(call: LogIntentCall, question: str, settings: QuestionSettings, *, as_of: date | None, values: Callable[[str], list[str]]) -> BoundQuestion:
    """The M0 question binding of one log intent call (see the module docstring). ``ValueError``
    when a vocabulary term cannot be honoured (the caller refuses the scope)."""
    fields = INPUT_MODELS[call.intent].model_fields
    args = dict(call.args)
    bound = BoundQuestion(calls=[])
    # vocabulary: a named service wins, unless every named service is itself one of the services a
    # vocabulary term in the question reads ("crawler failures": the word names the service
    # ``crawler`` and the term ``crawl`` reads all crawl services; owner test 2026-11-10) -- then the
    # term's services are read; a term whose values are all unindexed refuses the scope
    if settings.vocabulary:
        grounding = ground_terms(question, settings.vocabulary, values("service"))
        named = args.get("service")
        named_list = [named] if isinstance(named, str) else list(named or [])
        if named_list and grounding.services and "service" in fields and "list" in str(fields["service"].annotation):
            folded = {g.casefold() for g in grounding.services}
            if all(isinstance(n, str) and n.casefold() in folded for n in named_list):
                widened = [*named_list, *(g for g in grounding.services if g.casefold() not in {n.casefold() for n in named_list})]
                if widened != named_list:
                    args.pop("service")
        if grounding.unindexed and not grounding.services and not args.get("service"):
            raise ValueError(f"the term {grounding.unindexed[0]!r} names services that are not in the indexed vocabulary; coverage is unknown")
        if grounding.services and not args.get("service"):
            if "service" not in fields:
                bound.unapplied.append(UnsupportedConstraint(constraint_text=", ".join(grounding.matched), reason="qualifier_not_applied", suggestion=f"{call.intent} cannot filter by service; " + " ".join(s for s in grounding.statements if "read as service" in s)))
            elif "list" in str(fields["service"].annotation):
                args["service"] = list(grounding.services)
            elif len(grounding.services) == 1:
                args["service"] = grounding.services[0]
            else:
                raise ValueError(f"{call.intent} supports only one service; {grounding.matched[0]!r} names several ({', '.join(grounding.services)})")
            bound.notes.extend(s for s in grounding.statements if "read as service" in s)
        bound.notes.extend(s for s in grounding.statements if "read as service" not in s)
    explicit = "window" in args or "start" in args
    named_period = _names_period(question, as_of)
    reading = read_clock(question, settings, as_of)
    if reading is not None and call.intent == "log_around":
        # a dated, unambiguous hour on a UTC question clock is read as written: nothing to state
        if reading.ambiguous or not reading.dated or settings.timezone != "UTC":
            bound.notes.append(reading.statement() + (" Each reading is shown separately." if len(reading.local) > 1 else ""))
        for moment in reading.utc():
            bound.calls.append(LogIntentCall(intent="log_around", args={**args, "at": moment.isoformat()}))
        return bound
    if reading is not None and "window" in fields:
        partition = ZoneInfo(settings.logs_timezone)
        days = sorted({m.astimezone(partition).date() for m in reading.local})
        if not named_period:
            args.pop("window", None)
            args.update(start=days[0].isoformat(), end=days[-1].isoformat())
        bound.unapplied.append(UnsupportedConstraint(constraint_text=reading.text, reason="qualifier_not_applied",
                                                     suggestion=reading.statement() + f" Day-level counts cover whole days ({settings.logs_timezone}); the hour is not isolated."))
    elif "window" in fields and not explicit and not named_period and PRESENCE.search(question):
        args["window"] = f"last {settings.view_days} days"
        bound.notes.append("No period was named, so the default view is shown (logs.collection.serving.default_view_days).")
    bound.calls.append(LogIntentCall(intent=call.intent, args=args))
    return bound


def _names_period(question: str, as_of: date | None) -> bool:
    if re.search(r"\b(?:last|past|over)\s+\d{1,4}\s+days?\b|\b\d{4}-\d{2}-\d{2}\b", question, re.I):
        return True
    try:
        return bool(find_windows(question, as_of or date.today()))
    except WindowError:
        return True  # an invalid explicit range is refused by bind_question_scope, never defaulted
