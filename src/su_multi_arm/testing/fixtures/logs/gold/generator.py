"""Synthetic 10-day logs gold set (addendum A2 §6.2, slice E1 subset). Synthetic only: tenants,
services, endpoints, classes and messages are invented.

The generator works from an explicit **plan**: a list of ``Event``s (day, tenant, service, hour,
minute, level, signature, text, typed values). The indices it writes for the fake source and the
expected answer of every gold question are both computed from that plan, never from the engine.

Planted structure (``GoldSpec`` defaults, days ``D1``–``D10`` from ``first_day``):

* every (tenant, service) unit logs HTTP requests at hours 0, 3, …, 21 (INFO 2xx, latency by
  endpoint), plus health-check noise and one unparsed opaque line a day;
* ``svc-api`` dependency errors ramp: ``d`` ``dependency.connect_failed`` lines on day ``d``;
  ``svc-billing`` raises ``handler.unhandled_exception`` (``TimeoutError`` / ``KeyError``) at hours
  6 and 18; ``svc-quiet`` never errors;
* a signature ``feature.flag_conflict`` appears first on day 7 for tenants ``t02`` and ``t03``;
* on day 5 tenant ``t02`` has a three-signature incident (``cache.evicted``, ``queue.backlog``,
  ``worker.restarted``) co-firing at hours 13, 14 and 16 only;
* ``svc-api`` endpoint ``/api/orders/{id}`` regresses on day 8 (latency ×10) with a 5xx burst at hour 9;
* ``process.crashed`` FATAL lines on day 9 for ``svc-auth`` of ``t01`` and ``t03``;
* ``svc-legacy`` has no level field (its counts use the ``token_match`` basis) and one ERROR line a day;
* ``svc-legacy`` also writes FATAL lines in a line style no format pattern knows (``raw`` events:
  unparsed, so the parse cannot see their level while the source's token count does): two for
  ``t02`` and one for ``t04`` on day 9, one for ``t01`` on day 8 (the real-run "FATAL answered as
  none" defect);
* ``t04/svc-worker`` on day 8 logs more lines than ``max_docs_per_unit`` (a capped, sampled unit);
* day 4 is never pulled (``source_unavailable``).

Optional plant (off by default, so the default plan and its expected answers do not change):

* ``GoldSpec.error_flood = (tenant, service, day number, n)``: ``n`` ERROR lines of that unit in a
  line style no format pattern knows (``raw``), more than ``max_docs_per_unit``: the source's own
  ERROR count holds all ``n`` while the pull is capped and the parse cannot read the lines (the
  real-run "a service failing with mostly unparsed errors drops out of the ranking" defect).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

TENANTS = ("t01", "t02", "t03", "t04")
SERVICES = ("svc-api", "svc-auth", "svc-billing", "svc-worker", "svc-quiet", "svc-legacy")
TOKEN_ONLY = ("svc-legacy",)
ENDPOINTS = {
    "svc-api": [("GET", "/api/orders/{n}", 80), ("POST", "/api/orders", 120)],
    "svc-auth": [("POST", "/auth/login", 40)],
    "svc-billing": [("GET", "/billing/invoices/{n}", 60)],
    "svc-worker": [("GET", "/jobs/{n}", 30)],
    "svc-quiet": [("GET", "/status", 5)],
    "svc-legacy": [("GET", "/legacy/report", 200)],
}
INCIDENT = ("cache.evicted", "queue.backlog", "worker.restarted")
INCIDENT_HOURS = (13, 14, 16)
UNPARSED_FATAL = {(9, "t02"): 2, (9, "t04"): 1, (8, "t01"): 1}  # (day number, tenant) -> raw FATAL lines of svc-legacy


@dataclass(frozen=True)
class Event:
    day: date
    tenant: str
    service: str
    hour: int
    minute: int
    level: str
    signature: str | None  # None: noise or unparsed
    status: str  # parsed | noise | unparsed
    text: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GoldSpec:
    first_day: date = date(2026, 3, 1)
    days: int = 10
    missing_days: tuple[int, ...] = (4,)  # 1-based day numbers never pulled
    capped: tuple[str, str, int] = ("t04", "svc-worker", 8)
    capped_extra: int = 140
    max_docs_per_unit: int = 120
    error_flood: tuple[str, str, int, int] | None = None  # (tenant, service, day number, raw ERROR lines)


def day_of(spec: GoldSpec, n: int) -> date:
    return spec.first_day + timedelta(days=n - 1)


def plan(spec: GoldSpec = GoldSpec()) -> list[Event]:
    events: list[Event] = []
    for n in range(1, spec.days + 1):
        d = day_of(spec, n)
        for tenant in TENANTS:
            for service in SERVICES:
                events.extend(_unit_events(spec, n, d, tenant, service))
    return events


def _http(d: date, tenant: str, service: str, hour: int, minute: int, method: str, path: str, status: int, latency: int) -> Event:
    level = "ERROR" if status >= 500 else "INFO"
    text = f"{method} {path} status={status} latency_ms={latency}"
    return Event(d, tenant, service, hour, minute, level, "http.request", "parsed", text, {"http_method": method, "endpoint": path, "status_code": status, "latency_ms": latency})


def _unit_events(spec: GoldSpec, n: int, d: date, tenant: str, service: str) -> list[Event]:
    out: list[Event] = []
    t = TENANTS.index(tenant) + 1
    for i, hour in enumerate(range(0, 24, 3)):
        for j, (method, path, base) in enumerate(ENDPOINTS[service]):
            concrete = path.replace("{n}", str(1000 + 7 * i + j + t))
            latency = base + i
            if service == "svc-api" and n == 8 and path == "/api/orders/{n}":
                latency = base * 10 + i
            out.append(_http(d, tenant, service, hour, 1 + j, method, concrete, 200, latency))
    out.append(Event(d, tenant, service, 1, 30, "INFO", None, "noise", "GET /healthz 200 1ms"))
    out.append(Event(d, tenant, service, 2, 30, "INFO", None, "unparsed", f"opaque payload marker {n}{t}"))
    if service == "svc-api":
        deps = ("postgres", "redis", "kafka")
        hours = list(range(0, 24, 3))
        for k in range(n):
            hour = hours[k % len(hours)]
            dep = deps[k % 3]
            out.append(Event(d, tenant, service, hour, 10 + k % 40, "ERROR", "dependency.connect_failed", "parsed", f"db-client failed to connect {dep} after {100 * (k + 1)} ms"))
        if n == 8:
            for k in range(5):
                out.append(_http(d, tenant, service, 9, 20 + k, "GET", f"/api/orders/{2000 + k}", 503, 900 + k))
    if service in TOKEN_ONLY:  # level only in the text: counted by token match
        out.append(Event(d, tenant, service, 12, 15, "ERROR", "dependency.connect_failed", "parsed", "db-client failed to connect redis after 700 ms"))
    if service == "svc-billing":
        for hour, klass in ((6, "TimeoutError"), (18, "KeyError")):
            out.append(Event(d, tenant, service, hour, 40, "ERROR", "handler.unhandled_exception", "parsed", f"unhandled exception={klass} in handler", {"exception_class": klass}))
    unparsed_fatal = UNPARSED_FATAL.get((n, tenant), 0) if service in TOKEN_ONLY else 0
    for k in range(unparsed_fatal):
        out.append(Event(d, tenant, service, 4, 20 + k, "FATAL", None, "unparsed", f"FATAL batch {n}{t}{k} aborted: storage quota exhausted", {"raw": True}))
    if service == "svc-auth" and n == 9 and tenant in ("t01", "t03"):
        for hour in (2, 5):
            out.append(Event(d, tenant, service, hour, 45, "FATAL", "process.crashed", "parsed", "process crashed: out of memory"))
    if service == "svc-api" and n >= 7 and tenant in ("t02", "t03"):
        out.append(Event(d, tenant, service, 10, 50, "WARN", "feature.flag_conflict", "parsed", "feature flag conflict for flag f-checkout"))
    if n == 5 and tenant == "t02" and service in ("svc-worker", "svc-auth"):
        for hour in INCIDENT_HOURS:
            if service == "svc-worker":
                out.append(Event(d, tenant, service, hour, 5, "WARN", "cache.evicted", "parsed", f"cache evicted {500 + hour} entries"))
                out.append(Event(d, tenant, service, hour, 6, "WARN", "queue.backlog", "parsed", f"queue backlog depth {9000 + hour}"))
            else:
                out.append(Event(d, tenant, service, hour, 7, "ERROR", "worker.restarted", "parsed", "worker restarted after watchdog timeout"))
    if spec.error_flood is not None and spec.error_flood[:3] == (tenant, service, n):
        for k in range(spec.error_flood[3]):
            out.append(Event(d, tenant, service, k % 24, k // 24 % 60, "ERROR", None, "unparsed", f"ERROR batch {k:05d} failed: upstream reset by peer", {"raw": True}))
    cap_tenant, cap_service, cap_day = spec.capped
    if (tenant, service, n) == (cap_tenant, cap_service, cap_day):
        for k in range(spec.capped_extra):
            out.append(_http(d, tenant, service, k % 24, 2 + (k // 24), "GET", f"/jobs/{5000 + k}", 200, 30))
    return out


# --- indices for the fake source ------------------------------------------------------------------


def index_name(tenant: str, service: str, d: date) -> str:
    return f"{tenant}-prod-{service}-{d:%Y.%m.%d}"


def _line(event: Event) -> str:
    if event.values.get("raw"):  # an unrecognised line style: the text alone, no timestamp or level prefix
        return event.text
    ts = datetime(event.day.year, event.day.month, event.day.day, event.hour, event.minute, tzinfo=timezone.utc)
    if event.service in TOKEN_ONLY:
        return f"{ts:%Y-%m-%d %H:%M:%S},000 {event.level:<5} [main-1] org.example.legacy.Report - {event.text}"
    return f"[{ts.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}] [{event.level}] [{event.service}] {event.text}"


def indices(spec: GoldSpec = GoldSpec(), events: list[Event] | None = None) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    events = plan(spec) if events is None else events
    by_index: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        by_index[index_name(event.tenant, event.service, event.day)].append(event)
    out: dict[str, list[dict[str, Any]]] = {}
    mappings: dict[str, dict[str, Any]] = {}
    for name in sorted(by_index):
        docs = []
        rows = sorted(by_index[name], key=lambda e: (e.hour, e.minute, e.text))
        for k, event in enumerate(rows):
            ts = datetime(event.day.year, event.day.month, event.day.day, event.hour, event.minute, k % 60, tzinfo=timezone.utc)
            source: dict[str, Any] = {
                "@timestamp": ts.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "message": _line(event),
                "host": {"name": "node-01"},
                "kubernetes": {"container": {"name": event.service}},
                "log": {"file": {"path": f"/var/log/app/{event.service}.log"}, "offset": k * 100},
            }
            if event.service not in TOKEN_ONLY:
                source["level"] = event.level
            docs.append({"_id": f"{name}-{k:06d}", "_source": source})
        out[name] = docs
        props: dict[str, Any] = {"@timestamp": {"type": "date"}, "message": {"type": "text"}, "host": {"properties": {"name": {"type": "keyword"}}}}
        if not any(s in name for s in TOKEN_ONLY):
            props["level"] = {"type": "text", "fields": {"keyword": {"type": "keyword", "ignore_above": 32}}}
        mappings[name] = {"mappings": {"properties": props}}
    return out, mappings


def pulled_days(spec: GoldSpec = GoldSpec()) -> list[date]:
    return [day_of(spec, n) for n in range(1, spec.days + 1) if n not in spec.missing_days]
