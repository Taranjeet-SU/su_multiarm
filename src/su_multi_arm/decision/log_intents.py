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
``issue_history``  "how many ML outages in the last 3 months and the reason"    ``issue_day``
                   (episode archive only: archived + provisional days)          (tiered, I1)
``log_liveness``   "has the crawl for t01 halted", "when did indexing last      ``episode_piece``
                   complete", "did it recover on its own" (success heartbeats)  (success, O6)
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
    "log_top", "log_new", "log_affected", "log_trend", "log_compare", "log_tenants", "log_around", "log_endpoints", "log_signature", "log_coverage", "issue_history", "log_liveness"
]  # fmt: skip
LEVELS = ("fatal", "error", "warn", "info", "debug")
ISSUE_GROUPS = {"service_signature": ["service", "signature_id"], "tenant_signature": ["tenant", "signature_id"]}


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


class LogEvidenceScope(LogWindow):
    """Date and entity scope for incident-seeded graph/community investigations."""
    tenant: str | None = None
    service: list[str] | None = None


class LogTopInput(LogWindow):
    # ``deployment``: one row per (service, deployment_id), the instances of a service told apart
    # by the index-name grammar's numeric suffix (``index_name.service_suffix: numeric``)
    by: Literal["service", "signature", "service_signature", "tenant_signature", "tenant", "exception_class", "endpoint", "deployment"] = "service"
    group_by: list[Literal["service", "tenant", "signature_id", "deployment_id", "level"]] | None = Field(default=None, min_length=1, max_length=4, description="Explicit aggregation dimensions selected by the planner; overrides the by shorthand. Include signature_id to report observed error types.")
    columns: list[Literal["service", "tenant", "signature_id", "deployment_id", "level", "error_type", "error_count", "lines"]] | None = Field(default=None, min_length=1, max_length=6, description="Ordered output fields; must retain the requested grouping and count. Derived error_type/error_count are available when grouping ERROR/FATAL events by signature_id.")
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

    @model_validator(mode="after")
    def issue_levels(self):
        if self.group_by is None and self.by in ISSUE_GROUPS:
            self.group_by = list(ISSUE_GROUPS[self.by])
        if self.group_by is not None:
            if len(set(self.group_by)) != len(self.group_by):
                raise ValueError("group_by dimensions must be unique")
            if self.metric != "lines" or self.by in {"exception_class", "endpoint"}:
                raise ValueError("explicit group_by uses log event counts; use the dedicated rate/exception/endpoint operation for those measures")
        issue = self.group_by is not None and "signature_id" in self.group_by
        if issue:
            if "top_n" not in self.model_fields_set:
                self.top_n = 100
            if not self.level:
                self.level = ["error", "fatal"]
            if not {v.casefold() for v in self.level} <= {"error", "fatal"}:
                raise ValueError("issue breakdowns count ERROR/FATAL events only")
        if self.group_by is not None:
            required = {g for g in self.group_by if g != "signature_id"} | ({"error_count"} if issue else {"lines"})
            allowed = required | ({"signature_id", "error_type"} if issue else set())
            if self.columns is None:
                self.columns = ["error_type" if g == "signature_id" else g for g in self.group_by] + ["error_count" if issue else "lines"]
            if len(set(self.columns)) != len(self.columns) or not set(self.columns) <= allowed or not required <= set(self.columns) or (issue and not {"signature_id", "error_type"} & set(self.columns)):
                raise ValueError("columns must be computed fields and retain every grouping dimension and the count")
        elif self.columns is not None:
            raise ValueError("columns requires explicit group_by dimensions")
        return self


class LogNewInput(LogWindow):
    top_n: int = Field(default=50, ge=1, le=500)


class LogAffectedInput(LogWindow):
    signature_id: str = Field(min_length=1, max_length=256)
    per_day: bool = False


class LogTrendInput(LogWindow):
    signature_id: str | None = Field(default=None, max_length=256)
    service: str | None = None
    tenant: str | None = None
    deployment_id: str | None = None
    level: list[str] | None = Field(default=None, max_length=5)
    granularity: Literal["hour", "day"] = "day"


class LogCompareInput(StrictModel):
    # compared in date order (earliest first) whatever order they are named in: the change is
    # always from the earlier period to the later one
    periods: list[str] = Field(min_length=2, max_length=4)
    service: str | None = None
    tenant: str | None = None
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


class IssueHistoryInput(LogWindow):
    """Observed rule episodes over any range of the episode archive and the provisional days
    (plan K §4.1, I1). Without a window: the last ``logs.collection.serving.default_view_days``."""

    tenant: str | None = None
    service: list[str] | None = Field(default=None, max_length=50)
    signature_id: list[str] | None = Field(default=None, max_length=50)
    category: list[str] | None = Field(default=None, max_length=50)
    group_by: list[Literal["tenant", "env", "service", "signature_id", "category", "day", "month"]] = Field(default_factory=lambda: ["category", "signature_id"], max_length=4)
    top_n: int = Field(default=50, ge=1, le=500)
    # most recent episode pieces to list (k-way merged across tiers), 0: counts only
    list_episodes: int = Field(default=0, ge=0, le=200)

    @model_validator(mode="after")
    def unique_groups(self):
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by dimensions must be unique")
        return self


class LivenessInput(StrictModel):
    """Liveness from success heartbeats (rules with ``category: success``, O6): per tenant x env x
    service x success rule the last success, the expected cadence (median interval between success
    runs over the lookback), the current quiet time and whether it exceeds
    ``logs.collection.liveness.halt_factor`` x the cadence; with ``recovery`` the failure episodes
    of the lookback and the first success after each (time to recovery). Episode archive only."""

    tenant: str | None = None
    service: list[str] | None = Field(default=None, max_length=50)
    # success rule ids, or event kinds (the last dotted part of a rule id, e.g. ``crawl_completed``)
    event: list[str] | None = Field(default=None, max_length=20)
    # days of heartbeats read (default ``logs.collection.liveness.cadence_days``)
    lookback_days: int | None = Field(default=None, ge=1, le=366)
    as_of: date | None = None  # the day the question is read on (its end is "now" when in the past)
    at: datetime | None = None  # the instant "now" is read at (default: the current time)
    recovery: bool = False
    top_n: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def aware(self):
        if self.at is not None and self.at.tzinfo is None:
            raise ValueError("at needs an explicit timezone")
        return self


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
    "issue_history": IssueHistoryInput,
    "log_liveness": LivenessInput,
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
    collection_scope: str | None = None


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
            group = args.group_by or (DEPLOYMENT_GROUP if args.by == "deployment" else [field])
            preds = [_kind("log_day"), *common, *_in("level", args.level)]
            if "signature_id" in group:
                preds.append(_signed())
            if "signature_id" not in group or args.extraction == "deterministic":
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
        preds = [_kind(kind), window.predicate(), *_eq("service", args.service), *_eq("tenant", args.tenant), *_eq("deployment_id", args.deployment_id), *_in("level", args.level)]
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
        # earliest first (persona eval: "November 3 compared to November 2" reported the change
        # with the sign inverted because the periods kept the question's order)
        periods.sort(key=lambda p: (p.lower, p.upper))
        first = min(date.fromisoformat(p.lower) for p in periods)
        last = max(date.fromisoformat(p.upper) for p in periods)
        out.window = Window(start=first, end=last, label=" vs ".join(args.periods))
        # the union of the periods bounds the rows read (the periods split them)
        preds = [_kind("log_day"), out.window.predicate(), *_eq("service", args.service), *_eq("tenant", args.tenant), *_in("level", args.level)]
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
        from ..runtime.coverage_metrics import INCIDENT_METRIC_FIELDS, METRIC_FIELDS

        assert isinstance(args, LogCoverageInput)
        window = out.window = resolve_window(args, ctx)
        base = [_kind("coverage_day"), window.predicate(), *_eq("service", args.service)]
        incident_fields = INCIDENT_METRIC_FIELDS if ctx.collection_scope == "incident_seed_policy_with_bounded_context" else ()
        for name in ("lines_pulled", "lines_parsed", "lines_noise", "lines_unparsed", "lines_format_only", "lines_total", *METRIC_FIELDS, *incident_fields):
            out.scopes["main" if name == "lines_pulled" else name] = ScopeSpec(predicates=base, group_by=[args.by], measure=_sum(name), sort=Sort(field=args.by))
        out.scopes["coverage_units"] = ScopeSpec(predicates=base, group_by=[args.by], measure=Measure(kind="count"), sort=Sort(field=args.by))
    elif call.intent == "issue_history":
        out.window = resolve_window(args, ctx)
        if ctx.collection_scope != "incident_episode_summaries_v1":
            out.refusal = "issue_history reads the episode archive and the provisional days (logs.collection.archive); this generation is not an episode-summary generation"
    elif call.intent == "log_liveness":
        if ctx.collection_scope != "incident_episode_summaries_v1":
            out.refusal = "log_liveness reads success heartbeats of the episode archive and the provisional days (logs.collection.archive); this generation is not an episode-summary generation"
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
# Words for the tenant dimension (persona eval: "which customers ..." was ranked by signature).
_TENANT_WORDS = r"\b(?:tenants?|customers?|clients?|accounts?)\b"
# A change between two named periods ("increase", "drop", "worse" ...): a period comparison.
_CHANGE_WORDS = r"\b(?:vs\.?|versus|compared? (?:to|with)|than|increased?|increases|decreased?|decreases|drop(?:ped|s)?|rise|rose|grew|grow|change[ds]?|worse|better)\b"
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


def issue_breakdown(question: str) -> str | None:
    """Choose the requested grouping, keeping a named client/service as a separate filter."""
    nouns = r"services?|clients?|tenants?|customers?|accounts?"
    detail = re.search(r"\b(?:what (?:(?:was|were|is|are) (?:the )?)?(?:issues?|problems?|errors?)|error types?|issues? (?:by|per) (?:" + nouns + r"))\b", question, re.I)
    if not detail:
        return None
    dimension = re.search(r"\b(?:which|what|by|per)\s+(" + nouns + r")\b", question, re.I)
    if dimension is None:
        dimension = re.search(r"\b(" + nouns + r")\b", question, re.I)
    if dimension is None:
        return None
    return "service_signature" if dimension.group(1).casefold().startswith("service") else "tenant_signature"


def issue_dimensions(question: str, breakdown: str) -> list[str]:
    if re.search(r"\b(?:services?\s+(?:and|&)\s+(?:clients?|tenants?|customers?|accounts?)|(?:clients?|tenants?|customers?|accounts?)\s+(?:and|&)\s+services?)\b", question, re.I):
        return ["tenant", "service", "signature_id"]
    return list(ISSUE_GROUPS[breakdown])


def bind_question_scope(call: LogIntentCall, question: str, *, as_of: date | None, values: Callable[[str], list[str]]) -> LogIntentCall:
    """Bind named known entities and calendar windows, or refuse an intent that cannot honour them.

    This applies to both rules and model plans. A model cannot turn a named tenant/service into an
    unfiltered aggregate; unsupported dimensions are reported rather than silently discarded.
    """
    args = dict(call.args)
    fields = INPUT_MODELS[call.intent].model_fields
    if (breakdown := issue_breakdown(question)) and call.intent == "log_top":
        args["by"] = breakdown
        expected = issue_dimensions(question, breakdown)
        if args.get("group_by") is None:
            args["group_by"] = expected
        elif not set(expected) <= set(args["group_by"]):
            raise ValueError("the planned grouping omits a dimension requested by the question")
        if not re.search(r"\btop\s+\d+\b", question, re.I):
            args["top_n"] = 100
    if call.intent == "log_compare" and re.search(r"\bsource\b", question, re.I):
        raise ValueError("log_compare compares parsed rows; a source-count comparison is not supported by this intent")
    if re.search(r"\b(?:latest|most recent|last (?:error|issue|failure))\b", question, re.I):
        raise ValueError("log rollups cannot identify the latest individual event; event timestamp evidence is required")
    for field in ("service", "tenant"):
        known = values(field)
        named = _known(question, known)
        label = r"(?:client|tenant|customer|account)" if field == "tenant" else "service"
        explicit = re.findall(rf"\b{label}\s+[\"']([^\"']+)[\"']", question, re.I)
        if any(value.casefold() not in {v.casefold() for v in known} for value in explicit):
            raise ValueError(f"the explicitly named {field} is not in the indexed vocabulary; coverage is unknown")
        if not named:
            continue
        if field not in fields:
            raise ValueError(f"{call.intent} cannot apply the named {field} filter")
        many = "list" in str(fields[field].annotation)
        if len(named) > 1 and not many:
            raise ValueError(f"{call.intent} supports only one {field}; the question names several")
        supplied = args.get(field)
        if supplied is not None:
            actual = supplied if isinstance(supplied, list) else [supplied]
            if {str(v).casefold() for v in actual} != {v.casefold() for v in named}:
                raise ValueError(f"planned {field} filter contradicts the named {field}")
        args[field] = named if many else named[0]
    # Explicit uppercase severity names are a scope constraint, including ERROR OR FATAL.
    levels = sorted({m.casefold() for m in re.findall(r"\b(?:ERROR|FATAL|WARN|INFO|DEBUG)\b", question)})
    if levels:
        if "level" not in fields:
            raise ValueError(f"{call.intent} cannot apply the requested severity filter")
        many = "list" in str(fields["level"].annotation)
        if len(levels) > 1 and not many:
            raise ValueError(f"{call.intent} cannot apply multiple requested severities")
        args["level"] = levels if many else levels[0]
    if "window" in fields:
        find_windows(question, as_of)  # invalid explicit ranges must not become a default period
        # Explicit inclusive ISO ranges are two dates, not just the first date find_windows sees.
        interval = re.search(r"\b(?:between|from)\s+(\d{4}-\d{2}-\d{2})\s+(?:and|to|through)\s+(\d{4}-\d{2}-\d{2})\b", question, re.I)
        window = _window_text(question, as_of)
        if interval:
            first, last = (date.fromisoformat(value) for value in interval.groups())
            if last < first:
                raise ValueError("the requested date range ends before it starts")
            args.pop("window", None)
            args.update(start=first.isoformat(), end=last.isoformat())
        elif window:
            # The question's explicit period wins over a planner's default period; as_of remains
            # the relative-date anchor. No service/tenant qualifier is changed by this binding.
            args.pop("start", None)
            args.pop("end", None)
            args["window"] = window
    return LogIntentCall(intent=call.intent, args=args)


_HISTORY_NOUNS = r"\b(?:outages?|incidents?|failures?|episodes?|issues?|problems?)\b"
_HISTORY_ASKS = r"\b(?:how many|how often|number of|count|which|when|what|list|show|any|were there|was there|have there been|has there been|give|tell)\b"
# a short noun-phrase question ("outages in last 30 days") asks for the history of its period too
# (owner test 2026-11-10); longer statements without an ask word stay support questions
_HISTORY_PHRASE_WORDS = 8


def history_categories(question: str, known: list[str]) -> list[str]:
    """Category values a question names: exactly, or by their leading token (``ML`` names
    ``ml_serving`` and ``ml_training``). Deterministic; the match is stated in the answer."""
    exact = _known(question, known)
    words = {w.casefold() for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", question)}
    prefixed = [v for v in known if re.split(r"[_.\-]", v.casefold())[0] in words and len(re.split(r"[_.\-]", v)[0]) >= 2]
    return sorted(set(exact) | set(prefixed))


_LIVENESS_HALT = re.compile(
    r"\b(?:has|have|had|did|is|are|was|were)\s+(?:\S+\s+){0,6}?(?:halted|halt|stopped|stop|stalled|stuck|hung)\b"
    r"|\bstill\s+(?:running|working|alive)\b|\b(?:not|stopped)\s+running\b|\b(?:is|are)\s+(?:\S+\s+){0,5}?(?:running|alive)\b",
    re.I,
)
_LIVENESS_LAST = re.compile(
    r"\blast\s+(?:complet\w*|finish\w*|succe\w*|ran|run|delivered|sent|tick\w*|crawl\w*|index\w*)\b"
    r"|\blast\s+(?:successful|success|completion|good)\b|\bwhen\s+did\s+(?:\S+\s+){0,6}?(?:last\s+)?(?:complete|finish|succeed|run)\b",
    re.I,
)
_LIVENESS_RECOVERY = re.compile(
    r"\b(?:fixed|resolved|recovered|healed|cleared)\s+(?:itself|automatically|on\s+its\s+own|by\s+itself)\b|\bauto(?:matically|-)?\s*(?:fixed|recovered|resolved)\b"
    r"|\bself[- ]?heal\w*\b|\b(?:did|has|have)\s+(?:\S+\s+){0,6}?(?:recover(?:ed)?|come\s+back|came\s+back)\b|\brecover(?:y|ed)\b|\bback\s+to\s+normal\b",
    re.I,
)


def _event_words(text: str) -> set[str]:
    """Word stems of a question or rule id part (``crawling``/``crawls``/``crawler`` -> ``crawl``)."""
    out = set()
    for word in re.findall(r"[a-z]{3,}", text.casefold()):
        for suffix in ("ing", "ers", "er", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        out.add(word)
    return out


def detect_liveness(question: str, *, as_of: date | None, values: Callable[[str], list[str]], pin: bool = True) -> LogIntentCall | None:
    """``log_liveness`` for "has X halted / stopped / is it running", "when did X last complete"
    and "was it fixed automatically / did it recover", or ``None``. Event kinds are the known rule
    ids whose event part (after the last dot) starts with a word of the question (``crawl`` ->
    ``*.crawl_completed``); the runtime keeps the success rules among them."""
    q = question.strip()
    recovery = bool(_LIVENESS_RECOVERY.search(q))
    if not (recovery or _LIVENESS_HALT.search(q) or _LIVENESS_LAST.search(q)):
        return None
    args: dict[str, Any] = {"as_of": as_of.isoformat()} if as_of and pin else {}
    services, tenants = _known(q, values("service")), _known(q, values("tenant"))
    if services:
        args["service"] = services
    if len(tenants) == 1:
        args["tenant"] = tenants[0]
    words = _event_words(q)
    named = _known(q, values("signature_id"))
    events = sorted(set(named) | {v for v in values("signature_id") if v and (_event_words(v.rsplit(".", 1)[-1].split("_")[0]) & words)})
    if events:
        args["event"] = events[:20]
    days = re.search(r"\b(?:last|past|over)\s+(\d{1,3})\s+days?\b", q, re.I)
    if days and 1 <= int(days.group(1)) <= 366:
        args["lookback_days"] = int(days.group(1))
    if recovery:
        args["recovery"] = True
    return LogIntentCall(intent="log_liveness", args=args)


def detect_issue_history(question: str, *, as_of: date | None, values: Callable[[str], list[str]], pin: bool = True) -> LogIntentCall | None:
    """``issue_history`` for a count/which/when question about outages, incidents or failures over
    an explicit period, or ``None``. Used only when the generation is an episode-archive one.
    A liveness question (``detect_liveness``) is answered from the success heartbeats instead."""
    q = question.strip()
    low = q.casefold()
    if (liveness := detect_liveness(q, as_of=as_of, values=values, pin=pin)) is not None:
        return liveness
    if not re.search(_HISTORY_NOUNS, low) or not (re.search(_HISTORY_ASKS, low) or len(low.split()) <= _HISTORY_PHRASE_WORDS):
        return None
    interval = re.search(r"\b(?:between|from)\s+(\d{4}-\d{2}-\d{2})\s+(?:and|to|through)\s+(\d{4}-\d{2}-\d{2})\b", q, re.I)
    window = None if interval else _window_text(q, as_of)
    if interval is None and window is None:
        return None
    args: dict[str, Any] = {"as_of": as_of.isoformat()} if as_of and pin else {}
    if interval:
        args.update(start=interval.group(1), end=interval.group(2))
    else:
        args["window"] = window
    services, tenants = _known(q, values("service")), _known(q, values("tenant"))
    categories = history_categories(q, values("category"))
    if services:
        args["service"] = services
    if len(tenants) == 1:
        args["tenant"] = tenants[0]
    if categories:
        args["category"] = categories
    group = ["category", "signature_id"]
    if re.search(_TENANT_WORDS, low) and not tenants:
        group = ["tenant", *group]
    if re.search(r"\b(?:per|by|each)\s+(?:day|daily)\b|\bdaily\b", low):
        group = ["day", *group]
    elif re.search(r"\b(?:per|by|each)\s+month\b|\bmonthly\b", low):
        group = ["month", *group]
    args["group_by"] = group[:4]
    if re.search(r"\b(?:when|list|show)\b", low):
        args["list_episodes"] = 20
    return LogIntentCall(intent="issue_history", args=args)


def detect(question: str, *, as_of: date | None, values: Callable[[str], list[str]], pin: bool = True) -> LogIntentCall | None:
    """The ``LogIntentCall`` a plain-language log question asks, or ``None``. ``values(field)``
    lists the known values of a listable field (services, tenants, signatures).

    ``pin``: ``as_of`` was named by the caller and is stored in the call's arguments. With
    ``pin=False`` (``as_of`` is the engine's clock default) it only reads the question's window
    words; the call carries none, so a relative window ("yesterday") resolves against the clock at
    execution and a question without any date anchors on the latest served day."""
    q = question.strip()
    low = q.casefold()
    if re.search(r"\b(?:latest|most recent|last (?:error|issue|failure))\b", low):
        return None  # frequency ranking is not a latest-event answer; let the planner select a time operation
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
    if re.search(_CHANGE_WORDS, low):
        try:
            found = [w.text.strip() for w in find_windows(q, as_of)]
        except WindowError:
            found = []
        if len(found) < 2 and re.search(r"\bthe day before\b", low) and found:
            first = parse_window(found[0], as_of)
            found.append((first.start - timedelta(days=1)).isoformat())
        if len(found) >= 2:
            if not levels and re.search(r"\b(?:worse|better|healthier)\b", low):
                levels = list(FAILURE_LEVELS)  # "did things get worse": failures
            return call(
                "log_compare",
                periods=found[:4],
                service=services[0] if len(services) == 1 else None,
                tenant=tenants[0] if len(tenants) == 1 else None,
                signature_id=signatures[0] if signatures else None,
                level=levels or None,
            )
    if re.search(r"\b(trend|trended|over time|per day|per hour|hourly|daily)\b", low):
        return call(
            "log_trend",
            window=window or "last 10 days",
            signature_id=signatures[0] if signatures else None,
            service=services[0] if len(services) == 1 else None,
            tenant=tenants[0] if len(tenants) == 1 else None,
            level=levels or None,
            granularity="hour" if re.search(r"\b(per hour|hourly|by hour)\b", low) else None,
        )
    if re.search(r"\bendpoints?\b", low):
        metric = "errors_5xx" if re.search(r"\b5xx|5\d\d|server errors?\b", low) else "latency_p95" if "p95" in low else "latency_max" if re.search(r"\b(slow|slowest|latency)\b", low) else "lines"
        return call("log_endpoints", window=window, service=services[0] if len(services) == 1 else None, metric=metric)
    if re.search(r"\bnew\b|first (?:seen|appeared)|appeared\b", low):
        return call("log_new", window=window)
    if re.search(_TENANT_WORDS, low) and levels and services and not tenants and len(levels) == 1 and not issue_breakdown(q):
        return call("log_tenants", window=window, service=services[0], level=levels[0])
    if re.search(r"\b(which|top|most|what)\b", low) and (levels or re.search(r"\b(signatures?|errors?|exceptions?|services?)\b", low)):
        if breakdown := issue_breakdown(q):
            by = breakdown
        elif re.search(r"\bexceptions?\b", low):
            by = "exception_class"
        elif re.search(r"\b(?:deployments?|instances?)\b", low):
            by = "deployment"
        elif re.search(_TENANT_WORDS, low):
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
            group_by=issue_dimensions(q, by) if by in ISSUE_GROUPS else None,
            level=None if rate and levels in ([], ["error"], list(FAILURE_LEVELS)) else (levels or None),  # "error rate": ERROR and FATAL
            service=services or None,
            tenant=tenants[0] if len(tenants) == 1 else None,
            top_n=int(top.group(1)) if top else None,
            metric="error_rate" if rate else None,
        )
    return None


# --- unified intents (unified merge M2b-2, §6.11; lane LF) ----------------------------------------
# The operations half of a question asked on the unified collection: what today's detectors above
# recognise, plus the support-shaped questions they do not ("unauthorized despite correct
# credentials", "report stuck in the queue", "cannot log in"): a failure symptom about a grounded
# subject (a known service or tenant, a vocabulary term, a registry customer) reads the observed
# failure episodes of the default view (``issue_history`` on an episode archive, else ``log_top``
# by signature). Without a grounded subject there is no operations half: a symptom alone never
# becomes an all-service answer. English-scoped and deterministic, like the detectors above.

OPS_SYMPTOM = _OPS_SYMPTOM = re.compile(
    r"\b(?:unauthori[sz]ed|unauthenticated|forbidden|401|403|access\s+denied|invalid\s+(?:credentials|token|password)"
    r"|auth(?:entication|orization)?\s+(?:errors?|fail\w*)|(?:log\s?in|sign\s?in|login|signin)\s+(?:fail\w*|errors?)"
    r"|(?:not\s+able|unable|can'?t|cannot|could\s?n[o']t)\s+(?:to\s+)?(?:log\s?in|login|sign\s?in|connect|authenticate|access)"
    r"|time[sd]?\s?out|timeouts?|stuck|queued|(?:in|into)\s+(?:the\s+)?que(?:ue)?|backlog|not\s+(?:running|working|completing|finishing|progressing)"
    r"|fail(?:s|ed|ing|ure|ures)?|errors?|exceptions?|outages?|downtime|down|slow(?:ness)?|latency|halted|stopped|stalled|crash\w*|5xx|50[0234])\b",
    re.I,
)
_HOURS = re.compile(r"\b(?:last|past|for(?:\s+the)?(?:\s+last|\s+past)?)\s+(\d{1,2})\s+(?:hours?|hrs?|minutes?|mins?)\b", re.I)
_ANALYTICS = re.compile(
    r"\bsearch(?:ed|es|ing)?\b.{0,80}?\b(?:not\s+(?:coming|showing|appearing|shown|found|returned)|(?:first|top)\s+(?:page|results?)|rank(?:s|ed|ing)?|zero\s+results?|no\s+results?|clicks?)\b",
    re.I,
)
# Words that ask for knowledge (a cause, a fix, a procedure, earlier cases): a rollup-only log
# question without them is answered from the logs alone.
_KNOWLEDGE = re.compile(
    r"\b(?:why|how\s+(?:do|can|to|should|did)|fix(?:ed|es)?|resolv\w*|workaround|cause[sd]?|despite|still|unable|not\s+able|can'?t|cannot"
    r"|tickets?|known\s+issues?|documentation|docs?|guide|steps?|reported|supposed\s+to|expected|understand)\b",
    re.I,
)
ROLLUP_INTENTS = frozenset({"log_top", "log_new", "log_affected", "log_trend", "log_compare", "log_tenants", "log_around", "log_endpoints", "log_signature", "log_coverage"})
_ASKS_ERROR = re.compile(r"\b(?:what|which)\b.{0,40}?\b(?:errors?|issues?|problems?|failures?|exceptions?)\b|\bwhy\b", re.I)


def term_pattern(term: str) -> re.Pattern[str]:
    """A whole-word, case-insensitive pattern of a word or phrase (spaces, ``_`` and ``-`` between
    its parts are interchangeable); never a prefix: ``report`` does not match "reported"."""
    words = r"[\s_-]+".join(re.escape(part) for part in re.split(r"[\s_-]+", term.strip()))
    return re.compile(rf"(?<![A-Za-z0-9]){words}(?![A-Za-z0-9])", re.I)


def operations_evidence(question: str, terms: list[str] | tuple[str, ...]) -> list[str]:
    """What makes ``question`` ask for operations evidence (``crosssource.planner.operations_terms``
    staging round 2, N2): the configured terms it names (whole words) and a failure symptom
    (``OPS_SYMPTOM``); empty: the question has no operations half."""
    found = [t for t in terms if term_pattern(t).search(question)]
    symptom = OPS_SYMPTOM.search(question)
    if symptom is not None:
        found.append(symptom.group(0))
    return found


def has_symptom(question: str) -> bool:
    """The question describes a failure, an outage or a stall (operations evidence may exist)."""
    return bool(_OPS_SYMPTOM.search(question))


def asks_search_analytics(question: str) -> bool:
    """The question is about how a search query performed (ranking, results, clicks): its
    operations evidence is search analytics, not logs."""
    return bool(_ANALYTICS.search(question))


def asks_knowledge(question: str, intents: list[str]) -> bool:
    """Whether the question wants a knowledge half beside its operations half: always for
    liveness, outage and episode questions; for a rollup-only log question only when it asks for a
    cause, a fix, a procedure or earlier cases."""
    if not intents or any(i not in ROLLUP_INTENTS for i in intents):
        return True
    return bool(_KNOWLEDGE.search(question))


def asks_failure_detail(question: str) -> bool:
    """"what was the error", "which failure", "why": the failure episodes are wanted beside a
    liveness reading."""
    return bool(_ASKS_ERROR.search(question))


def hour_span(question: str) -> str | None:
    """"last 3 hours" and the like: day rollups cannot isolate it (stated, never narrowed)."""
    m = _HOURS.search(question)
    return m.group(0) if m else None


def detect_operations(
    question: str,
    *,
    as_of: date | None,
    values: Callable[[str], list[str]],
    vocabulary_services: list[str],
    customers: list[str],
    history: bool,
    view_days: int,
    pin: bool = True,
) -> LogIntentCall | None:
    """The operations-half call of a support-shaped question that the detectors above do not
    recognise, or ``None``. ``vocabulary_services``: indexed services the ``question.vocabulary``
    grounds in the question (the question binding applies and states them); ``customers``: the
    customers the identity registry finds in the question (a subject: the caller filters the call to
    their tenants or refuses it). A known service or tenant written in the question is bound here
    and checked by ``bind_question_scope``."""
    q = question.strip()
    if not has_symptom(q):
        return None
    services = _known(q, values("service"))
    named_tenants = _known(q, values("tenant"))
    if not (services or vocabulary_services or named_tenants or customers):
        return None
    args: dict[str, Any] = {"as_of": as_of.isoformat()} if as_of and pin else {}
    interval = re.search(r"\b(?:between|from)\s+(\d{4}-\d{2}-\d{2})\s+(?:and|to|through)\s+(\d{4}-\d{2}-\d{2})\b", q, re.I)
    if interval:
        args.update(start=interval.group(1), end=interval.group(2))
    else:
        args["window"] = _window_text(q, as_of) or f"last {view_days} days"
    if services:
        args["service"] = services
    if len(named_tenants) == 1:
        args["tenant"] = named_tenants[0]
    if history:
        args.update(group_by=["category", "signature_id"], list_episodes=20)
        return LogIntentCall(intent="issue_history", args=args)
    args.update(by="signature", level=list(FAILURE_LEVELS))
    return LogIntentCall(intent="log_top", args=args)
