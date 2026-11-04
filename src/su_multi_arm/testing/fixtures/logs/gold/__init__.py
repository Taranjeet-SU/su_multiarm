"""Synthetic 10-day logs gold set (addendum A2 §6.2): the generator plan, the pattern library for its
line styles and expected-answer helpers computed from the plan (never from the engine)."""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any, Iterable

from .generator import ENDPOINTS, INCIDENT, INCIDENT_HOURS, SERVICES, TENANTS, TOKEN_ONLY, Event, GoldSpec, day_of, index_name, indices, plan, pulled_days

TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:.,]{8,12}Z?"


def _msg(entry_id: str, signature: str, category: str, regex: str, example: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": entry_id,
        "kind": "message",
        "status": "approved",
        "priority": extra.pop("priority", 50),
        "regex": regex,
        "signature": {"id": signature, "category": category},
        "examples": {"positive": [example]},
        "provenance": {"author": "user"},
        **extra,
    }


LIBRARY: dict[str, Any] = {
    "library": "gold-logs",
    "format_version": 1,
    "engine": "re2",
    "vocabularies": {"service": list(SERVICES)},
    "patterns": [
        {
            "id": "fmt.bracket",
            "kind": "format",
            "status": "approved",
            "priority": 100,
            "applies_to": {"field": "message"},
            "regex": r"^\[(?P<ts>" + TS + r")\] \[(?P<level>[A-Z]{4,5})\] \[(?P<component>[a-z][a-z0-9_.-]{1,40})\] (?P<body>[^\n]{1,1000})$",
            "entities": [{"group": "component", "type": "component"}],
            "then": {"field_from_group": "body"},
            "examples": {"positive": ["[2026-03-01T10:00:00.000Z] [ERROR] [svc-api] db-client failed to connect redis after 100 ms"]},
            "provenance": {"author": "user"},
        },
        {
            "id": "fmt.log4j",
            "kind": "format",
            "status": "approved",
            "priority": 90,
            "applies_to": {"field": "message"},
            "regex": r"^(?P<ts>" + TS + r") (?P<level>[A-Z]{4,5}) {0,2}\[(?P<thread>[a-z0-9-]{1,20})\] (?P<component>[A-Za-z0-9_.]{1,80}) - (?P<body>[^\n]{1,1000})$",
            "then": {"field_from_group": "body"},
            "examples": {"positive": ["2026-03-01 10:00:00,000 ERROR [main-1] org.example.legacy.Report - db-client failed to connect redis after 100 ms"]},
            "provenance": {"author": "user"},
        },
        _msg(
            "msg.http.request",
            "http.request",
            "http",
            r"^(?P<http_method>GET|POST|PUT|PATCH|DELETE) (?P<endpoint>/[A-Za-z0-9/_.-]{1,200}) status=(?P<status_code>[0-9]{3}) latency_ms=(?P<latency_ms>[0-9]{1,7})$",
            "GET /api/orders/1001 status=200 latency_ms=80",
            fields={"http_method": {"type": "string"}, "endpoint": {"type": "string"}, "status_code": {"type": "integer"}, "latency_ms": {"type": "integer"}},
            priority=60,
        ),
        _msg(
            "msg.dependency.connect_failed",
            "dependency.connect_failed",
            "connectivity",
            r"(?P<client>[a-z]{2,10}-client) (?P<trigger>failed to connect) (?P<dep>kafka|mongo|postgres|redis) after (?P<ms>[0-9]{1,6}) ms",
            "db-client failed to connect redis after 300 ms",
            fields={"ms": {"type": "integer"}},
            entities=[{"group": "client", "type": "component"}, {"group": "dep", "type": "dependency"}],
            relations=[{"subject": "client", "predicate": "fails_to_connect", "object": "dep", "trigger_group": "trigger"}],
        ),
        _msg(
            "msg.handler.unhandled_exception",
            "handler.unhandled_exception",
            "error",
            r"unhandled exception=(?P<exception_class>[A-Za-z][A-Za-z0-9_.]{1,80}) in handler",
            "unhandled exception=TimeoutError in handler",
            fields={"exception_class": {"type": "string"}},
        ),
        _msg("msg.process.crashed", "process.crashed", "availability", r"process crashed: out of memory", "process crashed: out of memory"),
        _msg("msg.feature.flag_conflict", "feature.flag_conflict", "config", r"feature flag conflict for flag (?P<flag>f-[a-z-]{1,40})", "feature flag conflict for flag f-checkout"),
        _msg("msg.cache.evicted", "cache.evicted", "capacity", r"cache evicted (?P<n>[0-9]{1,7}) entries", "cache evicted 513 entries"),
        _msg("msg.queue.backlog", "queue.backlog", "capacity", r"queue backlog depth (?P<n>[0-9]{1,7})", "queue backlog depth 9013"),
        _msg("msg.worker.restarted", "worker.restarted", "availability", r"worker restarted after watchdog timeout", "worker restarted after watchdog timeout"),
        {
            "id": "noise.healthz",
            "kind": "noise",
            "status": "approved",
            "priority": 10,
            "regex": r"^GET /healthz 200 [0-9]{1,3}ms$",
            "examples": {"positive": ["GET /healthz 200 1ms"]},
            "provenance": {"author": "user"},
        },
    ],
}


def select(events: Iterable[Event], *, days: Iterable[date] | None = None, **filters: Any) -> list[Event]:
    """Events matching ``field=value`` (a tuple/list/set value means any of) on the given days."""
    wanted = None if days is None else set(days)
    out = []
    for event in events:
        if wanted is not None and event.day not in wanted:
            continue
        ok = True
        for name, value in filters.items():
            actual = getattr(event, name)
            if isinstance(value, (tuple, list, set, frozenset)):
                ok = actual in value
            else:
                ok = actual == value
            if not ok:
                break
        if ok:
            out.append(event)
    return out


def lines_by(events: Iterable[Event], key: str) -> dict[Any, int]:
    return dict(Counter(getattr(e, key) for e in events))


__all__ = [
    "ENDPOINTS",
    "INCIDENT",
    "INCIDENT_HOURS",
    "LIBRARY",
    "SERVICES",
    "TENANTS",
    "TOKEN_ONLY",
    "Event",
    "GoldSpec",
    "day_of",
    "index_name",
    "indices",
    "lines_by",
    "plan",
    "pulled_days",
    "select",
]
