"""Calendar expressions → half-open date windows against a declared anchor (plan §3.7.2).

Deterministic and English-scoped, like the intent pre-pass. ``find_windows`` locates every
expression in a question; ``parse_window`` reads one expression. Both return ``Window`` objects with
an inclusive ``start`` date and an **exclusive** ``end`` date in the schema's calendar zone
(``MetadataSchema.timezone``); ``bounds`` turns a window into ISO bounds for a ``datetime`` field
(aware, midnight in that zone) or a ``date`` field.

Recognised (``anchor`` = the request's ``as_of`` date in the declared zone, else today there):

* ``today``, ``yesterday``; ``year to date`` / ``ytd``;
* ``this|current|last|previous week|month|quarter|year`` (calendar units; weeks start on Monday;
  ``this`` ends at the end of the anchor day);
* ``past|last N days|weeks|months|years`` and ``past week|month|quarter|year`` (rolling, ending at
  the end of the anchor day);
* ``since <year>``, ``since <month> [<year>]`` (up to the end of the anchor day);
* ISO ``YYYY-MM-DD`` / ``YYYY-MM`` / ``YYYY``; ``<month> <day>[, <year>]``; ``<month> [of] <year>``;
* a bare month name (``October``, ``in March``): the most recent such month that does not start
  after the anchor (this year's when it has begun, else last year's). ``may``/``march`` need a
  preposition (``in May``) because they are also ordinary words.

Anything else is not a window; a relative expression without an anchor is refused (never guessed).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal

from pydantic import Field

from ..models import StrictModel

Granularity = Literal["day", "week", "month", "quarter", "year"]
_MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}  # fmt: skip
_MON = r"(?:" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True)) + r")"
_AMBIGUOUS_MONTHS = {"may", "mar", "march"}
_UNIT = {"day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month", "year": "year", "years": "year", "quarter": "quarter", "quarters": "quarter"}

# Order matters: longer, more specific expressions first; matches never overlap.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rolling_n", re.compile(r"\b(?:in\s+the\s+|over\s+the\s+|during\s+the\s+|within\s+the\s+)?(?:last|past|previous)\s+(?P<n>\d{1,3})\s+(?P<unit>days?|weeks?|months?|quarters?|years?)\b", re.I)),
    ("relative", re.compile(r"\b(?P<which>this|current|last|previous|past)\s+(?P<unit>week|month|quarter|year)\b", re.I)),
    ("ytd", re.compile(r"\b(?:year[- ]to[- ]date|ytd)\b", re.I)),
    ("day", re.compile(r"\b(?P<word>today|yesterday)\b", re.I)),
    ("since", re.compile(r"\bsince\s+(?:(?P<mon>" + _MON + r")\s+)?(?P<year>(?:19|20)\d{2})\b|\bsince\s+(?P<mon2>" + _MON + r")\b", re.I)),
    ("iso_day", re.compile(r"(?<![\w-])(?P<y>(?:19|20)\d{2})-(?P<m>\d{2})-(?P<d>\d{2})(?![\w-])")),
    ("iso_month", re.compile(r"(?<![\w-])(?P<y>(?:19|20)\d{2})-(?P<m>\d{2})(?![\w-])")),
    ("month_day", re.compile(r"\b(?P<mon>" + _MON + r")\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<y>(?:19|20)\d{2}))?\b(?!\s*(?:days?|weeks?|months?|years?|%))", re.I)),
    ("month_year", re.compile(r"\b(?P<mon>" + _MON + r")\.?\s+(?:of\s+)?(?P<y>(?:19|20)\d{2})\b", re.I)),
    ("year", re.compile(r"(?<![\w.:/-])(?P<y>(?:19|20)\d{2})(?![\w.:/%-])")),
    ("month", re.compile(r"\b(?P<prep>in|during|for|of|from|throughout)?\s*\b(?P<mon>" + _MON + r")\b", re.I)),
)


class Window(StrictModel):
    """``[start, end)`` in calendar days of the declared zone, with the matched text."""

    start: date
    end: date
    label: str = Field(min_length=1)
    text: str = Field(min_length=1)  # the matched fragment
    begin: int = Field(default=0, ge=0)  # span in the source text
    finish: int = Field(default=0, ge=0)
    relative: bool = False  # depends on the anchor


class WindowError(ValueError):
    pass


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _need(anchor: date | None, text: str) -> date:
    if anchor is None:
        raise WindowError(f"{text!r} is relative and no anchor is declared")
    return anchor


def _from_match(kind: str, m: re.Match[str], anchor: date | None) -> Window | None:
    text = m.group(0).strip()
    if kind == "rolling_n":
        today = _need(anchor, text)
        n, unit = int(m.group("n")), _UNIT[m.group("unit").casefold()]
        if n < 1:
            return None
        if unit == "day":
            start = today - timedelta(days=n - 1)
        elif unit == "week":
            start = today - timedelta(days=7 * n - 1)
        else:
            start = _add_months(today, -n * {"month": 1, "quarter": 3, "year": 12}[unit]) + timedelta(days=1)
        return Window(start=start, end=today + timedelta(days=1), label=f"last {n} {unit}{'s' if n != 1 else ''} (rolling, to {today.isoformat()})", text=text, relative=True)
    if kind == "relative":
        today = _need(anchor, text)
        which, unit = m.group("which").casefold(), m.group("unit").casefold()
        if which == "past":  # rolling window ending with the anchor day
            if unit == "week":
                start = today - timedelta(days=6)
            else:
                start = _add_months(today, -{"month": 1, "quarter": 3, "year": 12}[unit]) + timedelta(days=1)
            return Window(start=start, end=today + timedelta(days=1), label=f"past {unit} (rolling, to {today.isoformat()})", text=text, relative=True)
        previous = which in {"last", "previous"}
        if unit == "week":
            start = today - timedelta(days=today.weekday() + (7 if previous else 0))
            end = start + timedelta(days=7)
        else:
            months = {"month": 1, "quarter": 3, "year": 12}[unit]
            index = today.year * 12 + ((today.month - 1) // months) * months - (months if previous else 0)
            start = date(index // 12, index % 12 + 1, 1)
            end_index = index + months
            end = date(end_index // 12, end_index % 12 + 1, 1)
        if not previous:
            end = min(end, today + timedelta(days=1))
        return Window(start=start, end=end, label=f"{'last' if previous else 'this'} {unit} ({start.isoformat()} to {(end - timedelta(days=1)).isoformat()})", text=text, relative=True)
    if kind == "ytd":
        today = _need(anchor, text)
        return Window(start=date(today.year, 1, 1), end=today + timedelta(days=1), label=f"year to date ({today.year})", text=text, relative=True)
    if kind == "day":
        today = _need(anchor, text)
        start = today - timedelta(days=1 if m.group("word").casefold() == "yesterday" else 0)
        return Window(start=start, end=start + timedelta(days=1), label=f"{m.group('word').casefold()} ({start.isoformat()})", text=text, relative=True)
    if kind == "since":
        today = _need(anchor, text)
        if m.group("year"):
            month = _MONTH_NAMES[m.group("mon").casefold()] if m.group("mon") else 1
            start = date(int(m.group("year")), month, 1)
        else:
            month = _MONTH_NAMES[m.group("mon2").casefold()]
            start = date(today.year if month <= today.month else today.year - 1, month, 1)
        if start > today:
            return None
        return Window(start=start, end=today + timedelta(days=1), label=f"since {start.isoformat()} (to {today.isoformat()})", text=text, relative=True)
    if kind == "iso_day":
        try:
            start = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except ValueError:
            return None
        return Window(start=start, end=start + timedelta(days=1), label=start.isoformat(), text=text)
    if kind == "iso_month":
        month = int(m.group("m"))
        if not 1 <= month <= 12:
            return None
        start = date(int(m.group("y")), month, 1)
        return Window(start=start, end=_add_months(start, 1), label=start.strftime("%Y-%m"), text=text)
    if kind == "month_day":
        month, day = _MONTH_NAMES[m.group("mon").casefold()], int(m.group("d"))
        if m.group("y"):
            year = int(m.group("y"))
            relative = False
        else:
            today = _need(anchor, text)
            year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
            relative = True
        try:
            start = date(year, month, day)
        except ValueError:
            return None
        return Window(start=start, end=start + timedelta(days=1), label=start.isoformat(), text=text, relative=relative)
    if kind == "month_year":
        start = date(int(m.group("y")), _MONTH_NAMES[m.group("mon").casefold()], 1)
        return Window(start=start, end=_add_months(start, 1), label=start.strftime("%Y-%m"), text=text)
    if kind == "year":
        year = int(m.group("y"))
        return Window(start=date(year, 1, 1), end=date(year + 1, 1, 1), label=str(year), text=text)
    if kind == "month":
        name = m.group("mon").casefold()
        if name in _AMBIGUOUS_MONTHS and not m.group("prep"):
            return None
        today = _need(anchor, text)
        month = _MONTH_NAMES[name]
        start = date(today.year if month <= today.month else today.year - 1, month, 1)
        return Window(start=start, end=_add_months(start, 1), label=start.strftime("%Y-%m") + " (most recent)", text=text, relative=True)
    return None


def find_windows(text: str, anchor: date | None) -> list[Window]:
    """Every non-overlapping calendar expression in ``text``, in text order. A relative expression
    without an anchor raises ``WindowError`` (the caller reports it as unsupported)."""
    found: list[Window] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            if any(a < span[1] and span[0] < b for a, b in taken):
                continue
            window = _from_match(kind, m, anchor)
            if window is None:
                continue
            lead = len(m.group(0)) - len(m.group(0).lstrip())
            taken.append(span)
            found.append(window.model_copy(update={"begin": m.start() + lead, "finish": m.end()}))
    found.sort(key=lambda w: w.begin)
    return found


def parse_window(expression: str, anchor: date | None) -> Window:
    """Exactly one window covering the whole expression (surrounding prepositions allowed)."""
    text = expression.strip()
    windows = find_windows(text, anchor)
    rest = text
    for window in windows:
        rest = rest.replace(window.text, " ")
    rest = re.sub(r"\b(?:in|during|for|of|from|throughout|on|the|within|over)\b", " ", rest, flags=re.I)
    if len(windows) != 1 or rest.strip(" ,.;:"):
        raise WindowError(f"{expression!r} is not one recognised calendar expression")
    return windows[0]


def anchor_date(as_of: datetime, zone: tzinfo) -> date:
    """The anchor day in the declared zone (a naive ``as_of`` is read in that zone)."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=zone)
    return as_of.astimezone(zone).date()


def bounds(window: Window, field_type: str, zone: tzinfo) -> tuple[str, str]:
    """ISO ``(lower, upper)`` of the half-open window for a ``datetime`` (aware, midnight in
    ``zone``) or ``date`` field."""
    if field_type == "date":
        return window.start.isoformat(), window.end.isoformat()
    return datetime.combine(window.start, time.min, zone).isoformat(), datetime.combine(window.end, time.min, zone).isoformat()
