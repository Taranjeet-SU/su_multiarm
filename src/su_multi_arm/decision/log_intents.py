"""Typed log intents (addendum A2 §4.1.5; slice L5d; review B5).

A log question is answered by one ``LogIntentCall{intent, args}``. The decision agents (rules,
classifier, ``llm`` and cascade) only **select** an intent and fill its typed arguments; the
``ScopeSpec``s are compiled here, from the arguments alone, and executed by ``runtime/log_tools.py``.
The same call made through the decision path, the Python tool API, ``POST /v2/tools/{name}`` or
MCP therefore runs the same plan and returns the same numbers.

``detect`` is the deterministic rules mapping (English-scoped, like the window grammar it reuses
from ``decision/windows.py``): it recognises the ten question families below and returns ``None``
for anything else (the agent then plans as usual, and the structured scope guard refuses a free
scope over the log kinds, ``decision/log_guard.py``).

=================  ==========================================================  ==================
intent             example question                                            rollup
=================  ==========================================================  ==================
``log_top``        "which services had FATAL yesterday", "top errors for X"     ``log_day``,
                                                                               ``exception_day``,
                                                                               ``endpoint_*``
``log_new``        "what's new today", "new signatures this week"               ``signature_catalog``
``log_affected``   "which tenants are affected by S"                            ``signature_day``
``log_trend``      "trend of S over 10 days", "errors for X per hour"           ``log_day``/``log_hour``
``log_compare``    "errors for X yesterday vs today"                            ``log_day`` periods
``log_tenants``    "which tenants had FATAL in X yesterday"                     ``unit_day`` counts
``log_around``     "what else broke around 14:00 for t02 on 2026-03-05"         ``log_hour`` + incidents
``log_endpoints``  "slowest endpoints of X today", "5xx by endpoint for X"      ``endpoint_day``
``log_signature``  "what does S mean", "show examples of S"                     ``signature`` docs
``log_coverage``   "how much of yesterday did we parse"                         ``coverage_day``
=================  ==========================================================  ==================
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from ..core.types import Measure, Period, Predicate, ScopeSpec, Sort
from ..models import StrictModel
from .windows import WindowError, find_windows, parse_window

LogIntentName = Literal[
    "log_top", "log_new", "log_affected", "log_trend", "log_compare", "log_tenants", "log_around", "log_endpoints", "log_signature", "log_coverage"
]  # fmt: skip
LEVELS = ("fatal", "error", "warn", "info", "debug")


class LogWindow(StrictModel):
    """A day window: ``window`` ("yesterday", "last 7 days", "2026-03-05") read against ``as_of``,
    or explicit ``start``/``end`` (inclusive), or the profile default."""

    window: str | None = Field(default=None, max_length=64)
    start: date | None = None
    end: date | None = None
    as_of: date | None = None  # the anchor day; the tool default is today in logs.timezone

    @model_validator(mode="after")
    def coherent(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("give both start and end, or neither")
        if self.start is not None and self.window is not None:
            raise ValueError("give window or start/end, not both")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("end must not be before start")
        return self


class LogTopInput(LogWindow):
    # ``deployment``: one row per (service, deployment_id), the instances of a service told apart
    # by the index-name grammar's numeric suffix (``index_name.service_suffix: numeric``)
    by: Literal["service", "signature", "tenant", "exception_class", "endpoint", "deployment"] = "service"
    level: list[str] | None = Field(default=None, max_length=5)
    service: list[str] | None = Field(default=None, max_length=50)
    tenant: str | None = None
    deployment_id: list[str] | None = Field(default=None, max_length=50)
    top_n: int = Field(default=10, ge=1, le=100)
    extraction: Literal["any", "deterministic"] = "any"
    owner: Literal["any", "reviewed"] = "any"
    # ``error_rate``: the source's lines at ``level`` (default ERROR and FATAL, from the unit's count
    # queries) over the source's total lines of the unit (``unit_day`` ``lines_total``), by
    # service, tenant or deployment; never over the pulled lines
    metric: Literal["lines", "error_rate"] = "lines"


class LogNewInput(LogWindow):
    top_n: int = Field(default=50, ge=1, le=500)


class LogAffectedInput(LogWindow):
    signature_id: str = Field(min_length=1, max_length=256)
    per_day: bool = False


class LogTrendInput(LogWindow):
    signature_id: str | None = Field(default=None, max_length=256)
    service: str | None = None
    deployment_id: str | None = None
    level: list[str] | None = Field(default=None, max_length=5)
    granularity: Literal["hour", "day"] = "day"


class LogCompareInput(StrictModel):
    periods: list[str] = Field(min_length=2, max_length=4)
    service: str | None = None
    signature_id: str | None = Field(default=None, max_length=256)
    level: list[str] | None = Field(default=None, max_length=5)
    as_of: date | None = None


class LogTenantsInput(LogWindow):
    service: str | None = None
    level: str = "fatal"
    deployment_id: str | None = None
    by: Literal["tenant", "deployment"] = "tenant"  # deployment: one row per (service, deployment_id)


class LogAroundInput(StrictModel):
    at: datetime
    tenant: str | None = None
    minutes: int = Field(default=60, ge=1, le=720)
    top_n: int = Field(default=20, ge=1, le=100)


class LogEndpointsInput(LogWindow):
    service: str | None = None
    metric: Literal["lines", "errors_5xx", "latency_max", "latency_avg", "latency_p95"] = "lines"
    top_n: int = Field(default=10, ge=1, le=100)


class LogSignatureInput(LogWindow):
    signature_id: str = Field(min_length=1, max_length=256)


class LogCoverageInput(LogWindow):
    service: str | None = None
    by: Literal["service", "date", "tenant"] = "service"


INPUT_MODELS: dict[str, type[StrictModel]] = {
    "log_top": LogTopInput,
    "log_new": LogNewInput,
    "log_affected": LogAffectedInput,
    "log_trend": LogTrendInput,
    "log_compare": LogCompareInput,
    "log_tenants": LogTenantsInput,
    "log_around": LogAroundInput,
    "log_endpoints": LogEndpointsInput,
    "log_signature": LogSignatureInput,
    "log_coverage": LogCoverageInput,
}


class LogIntentCall(StrictModel):
    """One typed log question: the intent and its arguments (validated against the intent's input
    model; stored normalised, so equal calls compare equal)."""

    intent: LogIntentName
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def typed(self):
        model = INPUT_MODELS[self.intent]
        self.args = model.model_validate(self.args).model_dump(mode="json", exclude_none=True)
        return self

    def typed_args(self) -> StrictModel:
        return INPUT_MODELS[self.intent].model_validate(self.args)


# --- compilation ---------------------------------------------------------------------------------


class CompileContext(StrictModel):
    """What compilation needs besides the arguments: the anchor day and the profile defaults."""

    today: date
    default_window: str = "today"
    count_queries: dict[str, list[str]] = Field(default_factory=dict)  # name -> levels (lower case)
    # The latest served day with data, set only when ``today`` has no composed data yet: a question
    # without any date (no window, start/end or as_of) is anchored on it instead of on an empty today.
    latest_day: date | None = None


class Window(StrictModel):
    start: date
    end: date  # exclusive
    label: str
    latest_anchor: date | None = None  # the default window was anchored on the latest served day

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days)]

    def predicate(self, field: str = "date") -> Predicate:
        return Predicate(field=field, op="date_interval", lower=self.start.isoformat(), upper=self.end.isoformat(), granularity="day")


class CompiledIntent(StrictModel):
    """The named scopes of one intent (``main`` first) and how ``log_tools`` combines them."""

    intent: LogIntentName
    args: dict[str, Any]
    window: Window | None = None
    scopes: dict[str, ScopeSpec] = Field(default_factory=dict)
    refusal: str | None = None  # typed refusal (never executed)
    notes: list[str] = Field(default_factory=list)


def resolve_window(args: Any, ctx: CompileContext) -> Window:
    anchor = getattr(args, "as_of", None) or ctx.today
    if getattr(args, "start", None) is not None:
        return Window(start=args.start, end=args.end + timedelta(days=1), label=f"{args.start.isoformat()} to {args.end.isoformat()}")
    undated = getattr(args, "window", None) is None and getattr(args, "as_of", None) is None
    if undated and ctx.latest_day is not None:
        # no date in the question and today not composed: the default window on the latest served
        # day (stated in the rendering), never "no lines today" for a day that has no data yet
        window = resolve_window(LogWindow(window=ctx.default_window, as_of=ctx.latest_day), ctx.model_copy(update={"latest_day": None}))
        return window.model_copy(update={"latest_anchor": ctx.latest_day})
    text = getattr(args, "window", None) or ctx.default_window
    try:
        found = parse_window(text, anchor)
    except WindowError:
        found = None
    if found is None:
        match = re.fullmatch(r"\s*(?:over|for|in|during)?\s*(?:the\s+)?(?:last|past)?\s*(\d{1,4})\s+days?\s*", text, re.I)
        if not match:
            raise ValueError(f"{text!r} is not a recognised window")
        n = int(match.group(1))
        return Window(start=anchor - timedelta(days=n - 1), end=anchor + timedelta(days=1), label=f"last {n} days")
    return Window(start=found.start, end=found.end, label=found.label)


def _kind(kind: str) -> Predicate:
    return Predicate(field="kind", op="eq", value=kind)


def _in(field: str, values: list[str] | None) -> list[Predicate]:
    if not values:
        return []
    return [Predicate(field=field, op="in", value=[v.casefold() if field == "level" else v for v in values])]


def _eq(field: str, value: str | None) -> list[Predicate]:
    return [] if value is None else [Predicate(field=field, op="eq", value=value)]


SIGNED = ["parsed", "provisional", "model_line"]


def _signed() -> Predicate:
    """Rows with a signature: deterministic ``parsed`` rows and the fallback's model-assisted
    ``provisional``/``model_line`` rows (those heads are ``unparsed`` in the deterministic rows, so
    no head is counted twice)."""
    return Predicate(field="status", op="in", value=list(SIGNED))


def _deterministic() -> Predicate:
    """Totals over all heads read the deterministic rows only (they count every pulled head once)."""
    return Predicate(field="extraction_source", op="eq", value="deterministic")


def _sum(field: str = "lines") -> Measure:
    return Measure(kind="sum", field=field)


DEPLOYMENT_GROUP = ["service", "deployment_id"]


def count_fields(levels: list[str], queries: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """The source count queries whose sum is exactly the lines at ``levels`` (lower case): one
    query declared for that level set, else one single-level query per level. Returns the query
    names and the levels no such query covers (a query over more levels would over-count)."""
    wanted = sorted({lv.casefold() for lv in levels})
    exact = next((name for name, lv in sorted(queries.items()) if sorted(lv) == wanted), None)
    if exact is not None:
        return [exact], []
    names, missing = [], []
    for level in wanted:
        name = next((n for n, lv in sorted(queries.items()) if lv == [level]), None)
        if name is None:
            missing.append(level)
        else:
            names.append(name)
    return names, missing


def compile_intent(call: LogIntentCall, ctx: CompileContext) -> CompiledIntent:
    args = call.typed_args()
    out = CompiledIntent(intent=call.intent, args=call.args)
    if call.intent == "log_top":
        assert isinstance(args, LogTopInput)
        window = out.window = resolve_window(args, ctx)
        common = [window.predicate(), *_in("service", args.service), *_eq("tenant", args.tenant), *_in("deployment_id", args.deployment_id)]
        if args.by == "exception_class":
            extra = [_deterministic()] if args.extraction == "deterministic" else []
            out.scopes["main"] = ScopeSpec(predicates=[_kind("exception_day"), *common, *extra], group_by=["exception_class"], measure=_sum(), order="desc", top_n=args.top_n)
        elif args.by == "endpoint":
            if not args.service:
                out.refusal = "ranking endpoints needs a service (endpoint cardinality, review N7); use log_endpoints without a service for the window ranking"
                return out
            preds = [_kind("endpoint_day"), *common]
            if args.level and set(lv.casefold() for lv in args.level) & {"error", "fatal"}:
                preds.append(Predicate(field="status_class", op="eq", value="5xx"))
            out.scopes["main"] = ScopeSpec(predicates=preds, group_by=["endpoint"], measure=_sum(), order="desc", top_n=args.top_n)
        elif args.metric == "error_rate":
            if args.by not in {"service", "tenant", "deployment"}:
                out.refusal = "error_rate ranks services, tenants or deployments (by: service, tenant or deployment)"
                return out
            levels = sorted({lv.casefold() for lv in args.level or FAILURE_LEVELS})
            names, missing = count_fields(levels, ctx.count_queries)
            if missing:
                # the pulled lines are only the pulled strata (capped): never a denominator
                out.refusal = (
                    "error_rate needs the source's own counts: no count query of the source counts exactly "
                    + ", ".join(lv.upper() for lv in missing)
                    + " (declare one per level under sources.<name>.counts); the pulled lines are not a denominator"
                )
                return out
            group = DEPLOYMENT_GROUP if args.by == "deployment" else [args.by]
            base = [_kind("unit_day"), *common]
            # a unit counts only with its source total and every count it needs; the others are
            # listed as excluded (``units`` vs ``units_complete``), never divided by pulled lines
            complete = [*base, Predicate(field="lines_total", op="range", lower=0), *(Predicate(field=f"count_{n}", op="range", lower=0) for n in names)]
            for i, name in enumerate(names):
                out.scopes["main" if i == 0 else f"count_{name}"] = ScopeSpec(predicates=complete, group_by=group, measure=_sum(f"count_{name}"))
            out.scopes["total"] = ScopeSpec(predicates=complete, group_by=group, measure=_sum("lines_total"))
            out.scopes["units"] = ScopeSpec(predicates=base, group_by=group, measure=Measure(kind="count"))
            out.scopes["units_complete"] = ScopeSpec(predicates=complete, group_by=group, measure=Measure(kind="count"))
            for name in names:
                out.scopes[f"basis_{name}"] = ScopeSpec(predicates=complete, group_by=[group[0], f"basis_{name}"], measure=Measure(kind="count"))
            out.notes.append(
                "error rate = source lines at "
                + ", ".join(lv.upper() for lv in levels)
                + f" (count queries {', '.join(names)}) / source total lines (unit_day lines_total), per unit summed by {args.by}"
            )
        else:
            field = {"service": "service", "tenant": "tenant", "signature": "signature_id"}.get(args.by)
            group = DEPLOYMENT_GROUP if args.by == "deployment" else [field]
            preds = [_kind("log_day"), *common, *_in("level", args.level)]
            if args.by == "signature":
                preds.append(_signed())
            if args.by != "signature" or args.extraction == "deterministic":
                preds.append(_deterministic())
            if args.owner == "reviewed":
                preds.append(Predicate(field="pattern_owner", op="eq", value="user"))
            out.scopes["main"] = ScopeSpec(predicates=preds, group_by=group, measure=_sum(), order="desc", top_n=args.top_n)
    elif call.intent == "log_new":
        assert isinstance(args, LogNewInput)
        window = out.window = resolve_window(args, ctx)
        out.scopes["main"] = ScopeSpec(
            predicates=[_kind("signature_catalog"), window.predicate("first_seen")],
            group_by=["signature_id", "first_seen"],
            measure=_sum("lines_history"),
            order="desc",
            top_n=args.top_n,
        )
        out.notes.append("first seen across the whole composed history (signature catalog)")
    elif call.intent == "log_affected":
        assert isinstance(args, LogAffectedInput)
        window = out.window = resolve_window(args, ctx)
        group = ["tenant", "date"] if args.per_day else ["tenant"]
        out.scopes["main"] = ScopeSpec(
            predicates=[_kind("log_day"), window.predicate(), _signed(), Predicate(field="signature_id", op="eq", value=args.signature_id)],
            group_by=group,
            measure=_sum(),
            **({"sort": Sort(field="date")} if args.per_day else {"order": "desc"}),
        )
    elif call.intent == "log_trend":
        assert isinstance(args, LogTrendInput)
        window = out.window = resolve_window(args, ctx)
        kind, key = ("log_hour", "ts_hour") if args.granularity == "hour" else ("log_day", "date")
        preds = [_kind(kind), window.predicate(), *_eq("service", args.service), *_eq("deployment_id", args.deployment_id), *_in("level", args.level)]
        if args.signature_id:
            preds += [_signed(), Predicate(field="signature_id", op="eq", value=args.signature_id)]
        else:
            preds.append(_deterministic())
        out.scopes["main"] = ScopeSpec(predicates=preds, group_by=[key], measure=_sum(), sort=Sort(field=key))
    elif call.intent == "log_compare":
        assert isinstance(args, LogCompareInput)
        periods = []
        for text in args.periods:
            window = resolve_window(LogWindow(window=text, as_of=args.as_of), ctx)
            periods.append(Period(label=text, lower=window.start.isoformat(), upper=window.end.isoformat()))
        first = min(date.fromisoformat(p.lower) for p in periods)
        last = max(date.fromisoformat(p.upper) for p in periods)
        out.window = Window(start=first, end=last, label=" vs ".join(args.periods))
        # the union of the periods bounds the rows read (the periods split them)
        preds = [_kind("log_day"), out.window.predicate(), *_eq("service", args.service), *_in("level", args.level)]
        if args.signature_id:
            preds += [_signed(), Predicate(field="signature_id", op="eq", value=args.signature_id)]
        else:
            preds.append(_deterministic())
        out.scopes["main"] = ScopeSpec(predicates=preds, measure=_sum(), time_basis="date", periods=periods)
    elif call.intent == "log_tenants":
        assert isinstance(args, LogTenantsInput)
        window = out.window = resolve_window(args, ctx)
        level = args.level.casefold()
        query = next((name for name, levels in sorted(ctx.count_queries.items()) if levels == [level]), None)
        group = DEPLOYMENT_GROUP if args.by == "deployment" else ["tenant"]
        filters = [window.predicate(), *_eq("service", args.service), *_eq("deployment_id", args.deployment_id)]
        if query is not None:
            out.scopes["main"] = ScopeSpec(
                predicates=[_kind("unit_day"), *filters, Predicate(field=f"count_{query}", op="range", lower=1)],
                group_by=group,
                measure=_sum(f"count_{query}"),
                order="desc",
            )
            out.notes.append(f"source counts of the {query!r} count query (basis per row)")
        else:
            out.scopes["main"] = ScopeSpec(
                predicates=[_kind("log_day"), *filters, Predicate(field="level", op="eq", value=level), _deterministic()],
                group_by=group,
                measure=_sum(),
                order="desc",
            )
            out.notes.append("pulled lines by their level")
    elif call.intent == "log_around":
        assert isinstance(args, LogAroundInput)
        at = args.at if args.at.tzinfo else args.at.replace(tzinfo=timezone.utc)
        lower = (at - timedelta(minutes=args.minutes)).replace(minute=0, second=0, microsecond=0)
        upper = at + timedelta(minutes=args.minutes)
        hour_window = Predicate(field="ts_hour", op="date_interval", lower=lower.isoformat(), upper=upper.isoformat(), granularity="day")
        out.window = Window(start=lower.date(), end=upper.date() + timedelta(days=1), label=f"{lower.isoformat()} to {upper.isoformat()}")
        out.scopes["main"] = ScopeSpec(
            predicates=[_kind("log_hour"), hour_window, _signed(), *_eq("tenant", args.tenant)],
            group_by=["signature_id"],
            measure=_sum(),
            order="desc",
            top_n=args.top_n,
        )
        out.scopes["incidents"] = ScopeSpec(
            predicates=[
                _kind("incident"),
                *_eq("tenant", args.tenant),
                Predicate(field="first_hour", op="range", upper=upper.isoformat()),
                Predicate(field="last_hour", op="range", lower=lower.isoformat()),
            ],
            limit=20,
            sort=Sort(field="first_hour"),
        )
        out.notes.append("incidents are statistical co-occurrence, not causality")
    elif call.intent == "log_endpoints":
        assert isinstance(args, LogEndpointsInput)
        window = out.window = resolve_window(args, ctx)
        if args.service is None:
            if args.metric in {"errors_5xx", "latency_p95"}:
                out.refusal = f"metric {args.metric} needs a service (endpoint cardinality, review N7)"
                return out
            base = [_kind("endpoint_window")]
            measure_field = {"lines": "lines_window", "latency_max": "latency_ms_max"}.get(args.metric, "lines_window")
            if args.metric == "latency_avg":
                out.scopes["main"] = ScopeSpec(predicates=base, group_by=["service", "endpoint"], measure=_sum("latency_ms_sum"))
                out.scopes["count"] = ScopeSpec(predicates=base, group_by=["service", "endpoint"], measure=_sum("latency_ms_count"))
            else:
                kind = "max" if args.metric == "latency_max" else "sum"
                out.scopes["main"] = ScopeSpec(predicates=base, group_by=["service", "endpoint"], measure=Measure(kind=kind, field=measure_field), order="desc", top_n=args.top_n)
            out.notes.append("endpoint_window covers the composed partition window")
            return out
        base = [_kind("endpoint_day"), window.predicate(), Predicate(field="service", op="eq", value=args.service)]
        if args.metric == "lines":
            out.scopes["main"] = ScopeSpec(predicates=base, group_by=["endpoint"], measure=_sum(), order="desc", top_n=args.top_n)
        elif args.metric == "errors_5xx":
            out.scopes["main"] = ScopeSpec(predicates=[*base, Predicate(field="status_class", op="eq", value="5xx")], group_by=["endpoint"], measure=_sum(), order="desc", top_n=args.top_n)
        elif args.metric == "latency_max":
            out.scopes["main"] = ScopeSpec(predicates=base, group_by=["endpoint"], measure=Measure(kind="max", field="latency_ms_max"), order="desc", top_n=args.top_n)
        elif args.metric == "latency_avg":
            out.scopes["main"] = ScopeSpec(predicates=base, group_by=["endpoint"], measure=_sum("latency_ms_sum"))
            out.scopes["count"] = ScopeSpec(predicates=base, group_by=["endpoint"], measure=_sum("latency_ms_count"))
        else:  # p95 is per row (not additive): the rows themselves, highest first
            out.scopes["main"] = ScopeSpec(predicates=[*base, Predicate(field="latency_ms_p95", op="range", lower=0)], limit=args.top_n, sort=Sort(field="latency_ms_p95", descending=True))
            out.notes.append("p95 is per row (day x tenant x method x status class); it is not added or averaged across rows")
    elif call.intent == "log_signature":
        assert isinstance(args, LogSignatureInput)
        window = out.window = resolve_window(args, ctx)
        out.scopes["main"] = ScopeSpec(
            predicates=[_kind("signature_day"), window.predicate(), Predicate(field="signature_id", op="eq", value=args.signature_id)],
            group_by=["tenant"],
            measure=_sum(),
            order="desc",
        )
        out.scopes["documents"] = ScopeSpec(
            predicates=[Predicate(field="kind", op="in", value=["signature", "signature_examples", "signature_catalog"]), Predicate(field="signature_id", op="eq", value=args.signature_id)],
            limit=50,
        )
    elif call.intent == "log_coverage":
        assert isinstance(args, LogCoverageInput)
        window = out.window = resolve_window(args, ctx)
        base = [_kind("coverage_day"), window.predicate(), *_eq("service", args.service)]
        for name in ("lines_pulled", "lines_parsed", "lines_noise", "lines_unparsed", "lines_format_only", "lines_total"):
            out.scopes["main" if name == "lines_pulled" else name] = ScopeSpec(predicates=base, group_by=[args.by], measure=_sum(name), sort=Sort(field=args.by))
    return out


# --- deterministic detection (rules) ---------------------------------------------------------------

FAILURE_LEVELS = ("error", "fatal")
# A named level is that level; a word for failure in general ("errors", "failing", "issues",
# "problems", "broken" ...) is ERROR and FATAL (owner decision, real-run vocabulary).
_LEVEL_WORDS: dict[str, tuple[str, ...]] = {
    "fatal": ("fatal",), "fatals": ("fatal",), "critical": ("fatal",), "error": ("error",),
    "warn": ("warn",), "warning": ("warn",), "warnings": ("warn",),
    **{w: FAILURE_LEVELS for w in ("errors", "failing", "fails", "failed", "failure", "failures", "issue", "issues", "problem", "problems", "broken", "breaking")},
}  # fmt: skip
_HOUR = re.compile(r"\baround\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def _known(text: str, values: list[str]) -> list[str]:
    """Known values named in ``text`` on word boundaries (longest first, non-overlapping)."""
    found: list[str] = []
    taken: list[tuple[int, int]] = []
    for value in sorted({v for v in values if v}, key=lambda v: (-len(v), v)):
        for m in re.finditer(rf"(?<![A-Za-z0-9_.\-]){re.escape(value)}(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])", text, re.I):
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                found.append(value)
                break
    return found


def _window_text(question: str, anchor: date | None) -> str | None:
    m = re.search(r"\b(?:over|for|in|during)\s+(?:the\s+)?(?:last|past)\s+(\d{1,4})\s+days?\b|\b(?:last|past)\s+(\d{1,4})\s+days?\b|\bover\s+(\d{1,4})\s+days?\b", question, re.I)
    if m:
        return f"last {next(g for g in m.groups() if g)} days"
    try:
        windows = find_windows(question, anchor)
    except WindowError:
        return None
    return windows[0].text.strip() if windows else None


def detect(question: str, *, as_of: date | None, values: Callable[[str], list[str]], pin: bool = True) -> LogIntentCall | None:
    """The ``LogIntentCall`` a plain-language log question asks, or ``None``. ``values(field)``
    lists the known values of a listable field (services, tenants, signatures).

    ``pin``: ``as_of`` was named by the caller and is stored in the call's arguments. With
    ``pin=False`` (``as_of`` is the engine's clock default) it only reads the question's window
    words; the call carries none, so a relative window ("yesterday") resolves against the clock at
    execution and a question without any date anchors on the latest served day."""
    q = question.strip()
    low = q.casefold()
    services = _known(q, values("service"))
    tenants = _known(q, values("tenant"))
    signatures = _known(q, values("signature_id"))
    # level words outside the named values ("connect_failed" names a signature, not a failure level)
    words = low
    for value in sorted({*services, *tenants, *signatures}, key=len, reverse=True):
        words = words.replace(value.casefold(), " ")
    levels = sorted({lv for w in re.findall(r"[a-z]+", words) if w in _LEVEL_WORDS for lv in _LEVEL_WORDS[w]})
    window = _window_text(q, as_of)
    base: dict[str, Any] = {"as_of": as_of.isoformat()} if as_of and pin else {}

    def call(intent: str, **args: Any) -> LogIntentCall:
        return LogIntentCall(intent=intent, args={**base, **{k: v for k, v in args.items() if v is not None}})  # type: ignore[arg-type]

    if re.search(r"\b(how much|coverage|parsed|did we (?:actually )?parse)\b", low) and re.search(r"\b(parse|parsed|coverage)\b", low):
        return call("log_coverage", window=window, service=services[0] if len(services) == 1 else None)
    hour = _HOUR.search(q)
    if hour and re.search(r"\b(broke|break|fail|wrong|happen|fired?)\w*\b", low):
        windows = find_windows(q, as_of) if as_of else []
        day = next((w.start for w in windows if (w.end - w.start).days == 1), as_of)
        if day is None:
            return None
        h = int(hour.group(1)) % 12 + (12 if (hour.group(3) or "").lower() == "pm" else 0) if hour.group(3) else int(hour.group(1))
        at = datetime(day.year, day.month, day.day, h, int(hour.group(2) or 0), tzinfo=timezone.utc)
        return LogIntentCall(intent="log_around", args={"at": at.isoformat(), **({"tenant": tenants[0]} if len(tenants) == 1 else {})})
    if signatures and re.search(r"\b(mean|means|meaning|examples?|explain|what is)\b", low):
        return call("log_signature", signature_id=signatures[0], window=window or "last 10 days")
    if signatures and re.search(r"\b(affect|affected|affects|impacted)\b", low):
        return call("log_affected", signature_id=signatures[0], window=window or "last 10 days", per_day=bool(re.search(r"\bper day|by day|each day\b", low)) or None)
    if re.search(r"\b(vs\.?|versus|compared? (?:to|with)|than)\b", low):
        try:
            found = [w.text.strip() for w in find_windows(q, as_of)]
        except WindowError:
            found = []
        if len(found) < 2 and re.search(r"\bthe day before\b", low) and found:
            first = parse_window(found[0], as_of)
            found.append((first.start - timedelta(days=1)).isoformat())
        if len(found) >= 2:
            return call("log_compare", periods=found[:4], service=services[0] if len(services) == 1 else None, signature_id=signatures[0] if signatures else None, level=levels or None)
    if re.search(r"\b(trend|trended|over time|per day|per hour|hourly|daily)\b", low):
        return call(
            "log_trend",
            window=window or "last 10 days",
            signature_id=signatures[0] if signatures else None,
            service=services[0] if len(services) == 1 else None,
            level=levels or None,
            granularity="hour" if re.search(r"\b(per hour|hourly|by hour)\b", low) else None,
        )
    if re.search(r"\bendpoints?\b", low):
        metric = "errors_5xx" if re.search(r"\b5xx|5\d\d|server errors?\b", low) else "latency_p95" if "p95" in low else "latency_max" if re.search(r"\b(slow|slowest|latency)\b", low) else "lines"
        return call("log_endpoints", window=window, service=services[0] if len(services) == 1 else None, metric=metric)
    if re.search(r"\bnew\b|first (?:seen|appeared)|appeared\b", low):
        return call("log_new", window=window)
    if re.search(r"\btenants?\b", low) and levels and services:
        return call("log_tenants", window=window, service=services[0], level=levels[0])
    if re.search(r"\b(which|top|most|what)\b", low) and (levels or re.search(r"\b(signatures?|errors?|exceptions?|services?)\b", low)):
        if re.search(r"\bexceptions?\b", low):
            by = "exception_class"
        elif re.search(r"\b(?:deployments?|instances?)\b", low):
            by = "deployment"
        elif re.search(r"\btenants?\b", low):
            by = "tenant"
        elif re.search(r"\bservices?\b", low) and not services:
            by = "service"
        else:
            by = "signature"
        top = re.search(r"\btop\s+(\d{1,3})\b", low)
        rate = bool(re.search(r"\b(?:error|failure|fail)\s+rates?\b|\brates?\s+of\s+(?:errors|failures)\b", low))
        if rate and by not in {"service", "tenant", "deployment"}:
            by = "service"
        return call(
            "log_top",
            window=window,
            by=by,
            level=None if rate and levels in ([], ["error"], list(FAILURE_LEVELS)) else (levels or None),  # "error rate": ERROR and FATAL
            service=services or None,
            tenant=tenants[0] if len(tenants) == 1 else None,
            top_n=int(top.group(1)) if top else None,
            metric="error_rate" if rate else None,
        )
    return None
