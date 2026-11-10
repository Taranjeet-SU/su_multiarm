"""The small synthetic unified collection (unified merge plan rev 3, F0).

``build_files()`` returns every committed file of ``tests/fixtures/unified/`` as ``{relative path:
bytes}``; it is a pure function (no clock, no randomness), so a test can compare the committed tree
with a fresh build. ``load(root)`` reads the tree back.

What the collection holds (the manifest, ``collection.yaml``, states the expectations):

* member ``support`` (tier ``adopted``): tickets ``ticket:{n}`` and docs whose ids are their URLs;
* member ``tracker`` (``snapshot:2026-11-08``): tracker issues ``tracker:ENG-{n}``;
* member ``logs``: archive members ``month:2026-10``, ``day:2026-11-04`` (keyword epoch ``kw-e1``),
  ``day:2026-11-05``/``day:2026-11-06`` (epoch ``kw-e2``, which splits ``crawler.timeout`` into
  ``crawler.fetch_timeout`` and ``crawler.render_timeout``); provisional days 2026-11-07..09 served
  from delta slices registered in ``logs/ledger.yaml`` (base, hourly and five-minute slices with a
  supersede list);
* member ``analytics``: ``day:2026-11-07`` and ``day:2026-11-08`` (``search_query_day``,
  ``search_click``);
* ``identity/registry.yaml``: customers, aliases with every status, evidence, reviews and the
  expected per-namespace report with denominators;
* ``links/candidates.yaml``: link candidates of tiers A/B/C with every status, the planted links a
  builder must find and the negatives it must not produce;
* ``traps/``: inputs that the partition core must refuse (duplicate id, slice for an archived day,
  log-line text embedded, foreign embedding space, v2 config with a ``collection`` block, ids that
  route nowhere);
* ``onboarding/analytics.yaml``: the plan's §8.3 worked example as a D1–D11 onboarding file.

Log documents are built with ``ingestion.rollups._document`` (the write path's own id and header
functions): ``signature`` ids are ``logs:signature:<sha(rule)>`` and are emitted **every day**, and
``incident``/``signature_day`` ids hash the episode key and its first failure, so they carry no
day. The collection therefore contains the real collision cases (``expected.collisions``): the same
signature id in every log member and slice, and one incident id in an archive member and a
provisional slice (an episode that crosses midnight from a closed day into the live window).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ....core.types import Document
from ....ingestion.episode_summaries import CONTRACT, identity
from ....ingestion.rollups import _document

DATASET = "unified"
FIXTURE_NOW = "2026-11-09T10:30:00Z"
DISPLAY_TIMEZONE = "Australia/Adelaide"  # half-hour offset: hour-only questions must be read in the question clock
DEFAULT_VIEW_DAYS = 3
FORMAT_VERSION = 1
DOCS = "https://docs.example.org/kb/"

RULES: dict[str, tuple[str, str]] = {
    "crawler.timeout": ("Crawler request timed out", "availability"),
    "crawler.fetch_timeout": ("Crawler fetch timed out", "availability"),
    "crawler.render_timeout": ("Crawler render timed out", "availability"),
    "index.write_failed": ("Index write failed", "storage"),
    "search.5xx": ("Search API returned a server error", "availability"),
    "scheduler.job_halted": ("Scheduled job stopped before completion", "jobs"),
}
EPOCHS: dict[str, dict[str, Any]] = {
    "kw-e1": {"rules": ["crawler.timeout", "index.write_failed", "search.5xx", "scheduler.job_halted"], "effective_from": "2026-10-01"},
    "kw-e2": {
        "rules": ["crawler.fetch_timeout", "crawler.render_timeout", "index.write_failed", "search.5xx", "scheduler.job_halted"],
        "effective_from": "2026-11-05",
        "supersedes": {"crawler.timeout": ["crawler.fetch_timeout", "crawler.render_timeout"]},
    },
}


# --- log episodes ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Piece:
    """One version of an episode as a day member or slice holds it."""

    name: str
    tenant: str
    env: str
    service: str
    rule: str
    first: str
    last: str
    occurrences: int
    recovery: bool = False

    @property
    def key(self) -> list[str]:
        return [self.tenant, self.env, self.service, self.rule]

    @property
    def uid(self) -> str:  # as ``SummaryBuilder.documents``: the episode key and its first failure, no day
        return identity([self.key, self.first])[:32]


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _index(tenant: str, env: str, service: str, day: str) -> str:
    return f"{tenant}-{env}-{service}-{day.replace('-', '.')}"


def incident_documents(piece: Piece) -> list[Document]:
    """``incident`` + ``signature_day`` of one episode version (metadata as ``SummaryBuilder``)."""
    title = RULES[piece.rule][0]
    first, last = _ts(piece.first), _ts(piece.last)
    text = (f"{title}. Tenant {piece.tenant}; service {piece.service}; {piece.occurrences} rule-matching log messages "
            f"between {piece.first} and {piece.last}. Recovery and outage duration have not been established.")
    meta: dict[str, Any] = {
        "tenant": piece.tenant, "env": piece.env, "service": piece.service, "services": piece.service,
        "signature_id": piece.rule, "signatures": piece.rule, "category": RULES[piece.rule][1],
        "date": first.date().isoformat(), "first_hour": piece.first, "last_hour": piece.last,
        "occurrences": piece.occurrences, "lines": piece.occurrences,
        "observed_window_seconds": (last - first).total_seconds(),
        "community_id": "observed_rule:" + identity([DATASET, piece.tenant, piece.env, piece.rule, first.date().isoformat()]),
        "community_label": f"{title} for client {piece.tenant}",
        "episode_status": "observed_signal", "count_basis": "rule_query_count", "collection_scope": CONTRACT,
        "recovery_basis": "sampled_operation_only" if piece.recovery else "not_established", "issue": title,
        "evidence_count": 1, "evidence_truncated": False, "completion_evidence_count": 1 if piece.recovery else 0,
    }
    exemplar = f"[{piece.first}] ERROR {piece.service}: {title.lower()} (request <id> on host <host>)"
    fields = {"summary": text, "exemplars": exemplar, "sample_completion": f"[{piece.last}] INFO {piece.service}: operation completed" if piece.recovery else ""}
    link_meta = {**{k: v for k, v in meta.items() if not k.startswith("community_")}, "date_basis": "event_time", "date_timezone": "UTC", "level": "rule"}
    return [_document(DATASET, "incident", [piece.uid], title, meta, fields), _document(DATASET, "signature_day", [piece.uid], title, link_meta, {"summary": text})]


def episode_cells(pieces: list[Piece], day: str) -> list[Document]:
    """Hourly ``episode_cell`` counters of ``pieces`` inside ``day``: one row per (unit, rule, hour),
    summed over the pieces as ``SummaryBuilder`` does (each piece's occurrences spread over its hours)."""
    cells: dict[tuple[str, str, str, str, str], int] = {}
    for piece in pieces:
        first, last = _ts(piece.first), _ts(piece.last)
        hours = []
        hour = first.replace(minute=0, second=0, microsecond=0)
        while hour <= last:
            hours.append(hour)
            hour += timedelta(hours=1)
        for i, h in enumerate(hours):
            if h.date().isoformat() != day:
                continue
            share = piece.occurrences // len(hours) + (1 if i < piece.occurrences % len(hours) else 0)
            key = (piece.tenant, piece.env, piece.service, piece.rule, h.isoformat())
            cells[key] = cells.get(key, 0) + share
    docs = []
    for (tenant, env, service, rule, hour_iso), count in sorted(cells.items()):
        meta = {"tenant": tenant, "env": env, "service": service, "date": day, "ts_hour": hour_iso, "signature_id": rule, "occurrences": count, "collection_scope": CONTRACT}
        docs.append(_document(DATASET, "episode_cell", [_index(tenant, env, service, day), rule, hour_iso], "Rule match counter", meta))
    return docs


def signature_documents(epoch: str) -> list[Document]:
    """The rule catalog: day-independent ids, emitted by every day's build."""
    return [_document(DATASET, "signature", [rule], RULES[rule][0], {"signature_id": rule, "pattern_id": rule, "collection_scope": CONTRACT}, {"template": RULES[rule][0]})
            for rule in EPOCHS[epoch]["rules"]]


def unit_documents(units: list[tuple[str, str, str]], day: str, *, lines: int, partial: bool) -> list[Document]:
    docs = []
    for tenant, env, service in units:
        index = _index(tenant, env, service, day)
        base = {"tenant": tenant, "env": env, "service": service}
        end = f"{day}T10:25:00Z" if partial else f"{day}T23:59:59Z"
        meta = {**base, "source_documents": lines, "keyword_documents": lines // 10, "rule_documents": lines // 20, "unclassified_documents": lines // 50,
                "date": day, "collection_scope": CONTRACT, "window_start": f"{day}T00:00:00Z", "window_end": end, "observed_at": end,
                "count_basis": "rule_query_count"}
        docs.append(_document(DATASET, "summary_coverage", [index, day], "Collection summary", meta,
                              {"summary": "Keyword-filtered source aggregation; counts include unclassified candidates.", "keyword_counts": "[]"}))
        docs.append(_document(DATASET, "unit_day", [index, day], "Tenant and service source scope", {**base, "date": day, "lines_total": lines, "collection_scope": CONTRACT}))
    return docs


def shared_error_documents(pieces: list[Piece], day: str) -> list[Document]:
    """``shared_error`` rows: a rule matched for two or more tenants on ``day``."""
    groups: dict[str, list[Piece]] = {}
    for p in pieces:
        if _ts(p.first).date().isoformat() == day:
            groups.setdefault(p.rule, []).append(p)
    docs = []
    for rule, members in sorted(groups.items()):
        if len({m.tenant for m in members}) < 2:
            continue
        title = RULES[rule][0]
        for m in sorted(members, key=lambda m: (m.tenant, m.env, m.service)):
            meta = {"community_id": "shared_rule:" + identity([DATASET, rule, day]), "community_family_id": "shared_rule:" + identity([DATASET, rule]),
                    "community_label": f"{title} on {day}", "signature_id": rule, "tenants_affected": len({x.tenant for x in members}),
                    "tenant": m.tenant, "env": m.env, "service": m.service, "date": day, "level": "RULE", "row_exactness": "exact",
                    "first_hour": m.first, "last_hour": m.last, "error_type": title, "lines": m.occurrences, "method": "source_rule_query",
                    "collection_scope": CONTRACT}
            docs.append(_document(DATASET, "shared_error", [rule, day, m.tenant, m.env, m.service], title, meta,
                                  {"summary": f"{title} matched {m.occurrences} log messages for client {m.tenant}, service {m.service}, on {day}. "
                                              "Shared rule matches are associations, not proof of a common root cause."}))
    return docs


def unclassified_documents(tenant: str, env: str, service: str, day: str, messages: list[str]) -> list[Document]:
    base = {"tenant": tenant, "env": env, "service": service}
    return [_document(DATASET, "unclassified_example", [_index(tenant, env, service, day), day, str(i)], "Unclassified candidate",
                      {**base, "date": day, "collection_scope": CONTRACT, "evidence_truncated": False},
                      {"exemplars": message, "summary": "Keyword candidate; an incident has not been established."})
            for i, message in enumerate(messages)]


# Episode versions. Chain A crosses midnight between two archive days (two pieces, two ids); chain B
# crosses from archive day 2026-11-06 into the provisional window (one id in both tiers); chain C
# crosses midnight inside the provisional window (the 2026-11-09 base slice supersedes the 2026-11-08
# version, whose ``date`` stays 2026-11-08); D is superseded inside one day by a five-minute slice.
P = {
    "oct_alderbank_crawler": Piece("oct_alderbank_crawler", "alderbank-stg", "stg", "crawler", "crawler.timeout", "2026-10-30T13:10:00Z", "2026-10-30T13:50:00Z", 40),
    "oct_brightmoor_index": Piece("oct_brightmoor_index", "brightmoor", "prod", "indexer", "index.write_failed", "2026-10-14T02:00:00Z", "2026-10-14T02:30:00Z", 12),
    "oct_eastvale_index": Piece("oct_eastvale_index", "eastvale", "prod", "indexer", "index.write_failed", "2026-10-14T02:05:00Z", "2026-10-14T02:20:00Z", 7),
    "d04_alderbank_sched": Piece("d04_alderbank_sched", "alderbank-prod", "prod", "scheduler", "scheduler.job_halted", "2026-11-04T03:00:00Z", "2026-11-04T03:05:00Z", 3),
    "d04_brightmoor_crawler": Piece("d04_brightmoor_crawler", "brightmoor", "prod", "crawler", "crawler.timeout", "2026-11-04T10:00:00Z", "2026-11-04T10:40:00Z", 22, recovery=True),
    "chainA_1": Piece("chainA_1", "alderbank-stg", "stg", "crawler", "crawler.fetch_timeout", "2026-11-05T23:52:00Z", "2026-11-05T23:59:30Z", 9),
    "d05_eastvale_search": Piece("d05_eastvale_search", "eastvale", "prod", "search-api", "search.5xx", "2026-11-05T12:00:00Z", "2026-11-05T12:10:00Z", 5),
    "chainA_2": Piece("chainA_2", "alderbank-stg", "stg", "crawler", "crawler.fetch_timeout", "2026-11-06T00:00:10Z", "2026-11-06T00:14:00Z", 5),
    "chainB_archive": Piece("chainB_archive", "cobalt", "prod", "indexer", "index.write_failed", "2026-11-06T23:40:00Z", "2026-11-06T23:59:50Z", 6),
    "d06_unknown_search": Piece("d06_unknown_search", "t-0007", "prod", "search-api", "search.5xx", "2026-11-06T15:00:00Z", "2026-11-06T15:20:00Z", 8),
    "chainB_live": Piece("chainB_live", "cobalt", "prod", "indexer", "index.write_failed", "2026-11-06T23:40:00Z", "2026-11-07T00:25:00Z", 15),
    "d07_cobalt_render": Piece("d07_cobalt_render", "cobalt", "prod", "crawler", "crawler.render_timeout", "2026-11-07T09:00:00Z", "2026-11-07T09:30:00Z", 11),
    "d07_alderbank_sched": Piece("d07_alderbank_sched", "alderbank-prod", "prod", "scheduler", "scheduler.job_halted", "2026-11-07T06:00:00Z", "2026-11-07T06:02:00Z", 2),
    "d07_eastvale_index": Piece("d07_eastvale_index", "eastvale", "prod", "indexer", "index.write_failed", "2026-11-07T10:00:00Z", "2026-11-07T10:20:00Z", 6),
    "d08_brightmoor_search": Piece("d08_brightmoor_search", "brightmoor", "prod", "search-api", "search.5xx", "2026-11-08T08:40:00Z", "2026-11-08T09:30:00Z", 30),
    "d08_alderbank_crawler": Piece("d08_alderbank_crawler", "alderbank-stg", "stg", "crawler", "crawler.fetch_timeout", "2026-11-08T22:10:00Z", "2026-11-08T22:30:00Z", 6),
    "chainC_v1": Piece("chainC_v1", "eastvale", "prod", "search-api", "search.5xx", "2026-11-08T23:55:00Z", "2026-11-08T23:59:00Z", 4),
    "chainC_v2": Piece("chainC_v2", "eastvale", "prod", "search-api", "search.5xx", "2026-11-08T23:55:00Z", "2026-11-09T00:10:00Z", 7),
    "d09_brightmoor_crawler": Piece("d09_brightmoor_crawler", "brightmoor", "prod", "crawler", "crawler.fetch_timeout", "2026-11-09T03:00:00Z", "2026-11-09T03:20:00Z", 5),
    "D_v1": Piece("D_v1", "t-0007", "prod", "search-api", "search.5xx", "2026-11-09T09:12:00Z", "2026-11-09T09:58:00Z", 14),
    "D_v2": Piece("D_v2", "t-0007", "prod", "search-api", "search.5xx", "2026-11-09T09:12:00Z", "2026-11-09T10:24:00Z", 21),
    # Gap fill: the 2026-11-07 h13 slice sees two groups 360 s apart (two ids); late lines at 13:12,
    # 13:14 and 13:14:30 close the gap (each step <= 300 s), so the full-day rebuild holds one merged
    # group with E1's id and E2's id disappears (a tombstone is needed: latest-wins alone keeps E2).
    "E1": Piece("E1", "alderbank-stg", "stg", "search-api", "search.5xx", "2026-11-07T13:00:00Z", "2026-11-07T13:10:00Z", 6),
    "E2": Piece("E2", "alderbank-stg", "stg", "search-api", "search.5xx", "2026-11-07T13:16:00Z", "2026-11-07T13:30:00Z", 8),
    "E_merged": Piece("E_merged", "alderbank-stg", "stg", "search-api", "search.5xx", "2026-11-07T13:00:00Z", "2026-11-07T13:30:00Z", 17),
}
MERGE_GAP_SECONDS = 300  # ``SummaryBuilder``: groups merge when the quiet gap is at most 300 s
LATE_LINES = ["2026-11-07T13:12:00Z", "2026-11-07T13:14:00Z", "2026-11-07T13:14:30Z"]
UNCLASSIFIED_07 = {("cobalt", "prod", "indexer"): ["segment merge stalled for <num> s on shard <id>", "segment merge stalled for <num> s on shard <id>"]}


def episode_row_id(piece: Piece) -> str:
    """``episode:`` row id of the summary snapshot (``episode_summaries`` timeline), computed for the
    fixture scope ``{index, tenant, env, service}`` of the piece's first day."""
    day = _ts(piece.first).date().isoformat()
    scope = {"index": _index(piece.tenant, piece.env, piece.service, day), "tenant": piece.tenant, "env": piece.env, "service": piece.service}
    return "episode:" + identity([scope, piece.rule, piece.first])[:32]


def _units(pieces: list[Piece]) -> list[tuple[str, str, str]]:
    return sorted({(p.tenant, p.env, p.service) for p in pieces})


def _log_unit(day: str, pieces: list[Piece], epoch: str | None, *, partial: bool = False, unclassified: dict | None = None,
              coverage: bool = True, cell_day: str | None = None) -> list[Document]:
    docs: list[Document] = []
    if epoch is not None:
        docs += signature_documents(epoch)
    if coverage:
        docs += unit_documents(_units(pieces), day, lines=1000, partial=partial)
    for p in pieces:
        docs += incident_documents(p)
    docs += episode_cells(pieces, cell_day or day)
    docs += shared_error_documents(pieces, day)
    for (tenant, env, service), messages in (unclassified or {}).items():
        docs += unclassified_documents(tenant, env, service, day, messages)
    return docs


def log_units() -> dict[str, dict[str, Any]]:
    """Every log member and slice: ``{file: {member key or slice, docs}}``."""
    month = [P["oct_alderbank_crawler"], P["oct_brightmoor_index"], P["oct_eastvale_index"]]
    month_docs = signature_documents("kw-e1")
    for day in ("2026-10-14", "2026-10-30"):
        on_day = [p for p in month if p.first.startswith(day)]
        month_docs += _log_unit(day, on_day, None)
    return {
        "logs/archive/month-2026-10.jsonl": {"key": "month:2026-10", "epoch": "kw-e1", "docs": month_docs},
        "logs/archive/day-2026-11-04.jsonl": {"key": "day:2026-11-04", "epoch": "kw-e1", "docs": _log_unit("2026-11-04", [P["d04_alderbank_sched"], P["d04_brightmoor_crawler"]], "kw-e1")},
        "logs/archive/day-2026-11-05.jsonl": {"key": "day:2026-11-05", "epoch": "kw-e2", "docs": _log_unit("2026-11-05", [P["chainA_1"], P["d05_eastvale_search"]], "kw-e2")},
        "logs/archive/day-2026-11-06.jsonl": {"key": "day:2026-11-06", "epoch": "kw-e2", "docs": _log_unit("2026-11-06", [P["chainA_2"], P["chainB_archive"], P["d06_unknown_search"]], "kw-e2")},
        "logs/slices/2026-11-07/base.jsonl": {"slice": "2026-11-07/base", "epoch": "kw-e2", "docs": _log_unit(
            "2026-11-07", [P["chainB_live"], P["d07_cobalt_render"], P["d07_alderbank_sched"], P["d07_eastvale_index"]], "kw-e2", unclassified=UNCLASSIFIED_07)},
        "logs/slices/2026-11-07/h13.jsonl": {"slice": "2026-11-07/h13", "epoch": "kw-e2", "docs": _log_unit("2026-11-07", [P["E1"], P["E2"]], None, coverage=False)},
        "logs/slices/2026-11-07/day.jsonl": {"slice": "2026-11-07/day", "epoch": "kw-e2", "replaces": ["2026-11-07/base", "2026-11-07/h13"], "docs": _log_unit(
            "2026-11-07", [P["chainB_live"], P["d07_cobalt_render"], P["d07_alderbank_sched"], P["d07_eastvale_index"], P["E_merged"]], "kw-e2",
            unclassified=UNCLASSIFIED_07)},
        "logs/slices/2026-11-08/base.jsonl": {"slice": "2026-11-08/base", "epoch": "kw-e2", "docs": _log_unit(
            "2026-11-08", [P["d08_brightmoor_search"]], "kw-e2", unclassified={("brightmoor", "prod", "search-api"): ["upstream reset by peer after <num> ms"]})},
        "logs/slices/2026-11-08/h22.jsonl": {"slice": "2026-11-08/h22", "epoch": "kw-e2", "docs": _log_unit("2026-11-08", [P["d08_alderbank_crawler"]], None, coverage=False)},
        "logs/slices/2026-11-08/h23.jsonl": {"slice": "2026-11-08/h23", "epoch": "kw-e2", "docs": _log_unit("2026-11-08", [P["chainC_v1"]], None, coverage=False)},
        "logs/slices/2026-11-09/base.jsonl": {"slice": "2026-11-09/base", "epoch": "kw-e2", "docs": [
            *_log_unit("2026-11-09", [P["d09_brightmoor_crawler"]], "kw-e2", partial=True),
            *incident_documents(P["chainC_v2"]), *episode_cells([P["chainC_v2"]], "2026-11-09")]},
        "logs/slices/2026-11-09/h09.jsonl": {"slice": "2026-11-09/h09", "epoch": "kw-e2", "docs": _log_unit("2026-11-09", [P["D_v1"]], None, coverage=False)},
        "logs/slices/2026-11-09/m1025.jsonl": {"slice": "2026-11-09/m1025", "epoch": "kw-e2", "docs": [*incident_documents(P["D_v2"]), *episode_cells([P["D_v2"]], "2026-11-09")]},
    }


def incident_id(name: str) -> str:
    return incident_documents(P[name])[0].id


# --- support, tracker, analytics ----------------------------------------------------------------------

TICKETS = [
    # n, account, service, product, created, resolved, status, priority, subject, description, resolution
    (1001, "Alderbank Ltd", "crawler", "Crawl", "2026-10-12T08:30:00Z", "2026-10-13T16:00:00Z", "solved", "normal",
     "Crawler fetch timeouts on large sitemaps", "Crawls of large sitemaps stop with timeouts after about forty minutes.",
     f"Raised the fetch timeout as described in {DOCS}crawler-timeouts and split the sitemap."),
    (1002, "Alderbank Ltd", "crawler", "Crawl", "2026-10-30T14:00:00Z", "2026-10-31T10:00:00Z", "solved", "high",
     "Staging crawl keeps timing out", "The staging crawl failed repeatedly today; the log alert named crawler.timeout.",
     "The staging proxy dropped idle connections; the proxy idle limit was raised."),
    (1003, "brightmoor", "search-api", "Search", "2026-11-08T09:00:00Z", None, "open", "urgent",
     "Search returns HTTP 500 errors", "Users see server errors on every search since this morning (search.5xx in the alerts).", ""),
    (1004, "Cobaltfield Systems", "indexer", "Index", "2026-11-07T01:10:00Z", None, "pending", "high",
     "Index writes failing overnight", f"Writes to the index failed around midnight; the operations alert links {incident_id('chainB_live')}.", ""),
    (1005, "Alderbank Ltd", "crawler", "Crawl", "2026-11-06T08:00:00Z", "2026-11-06T12:00:00Z", "solved", "normal",
     "Crawl stopped just before midnight", "The nightly staging crawl stopped with crawler.fetch_timeout shortly before midnight.",
     "The crawl was restarted after the upstream host recovered."),
    (1006, "brightmoor", "search-api", "Search", "2026-10-20T11:00:00Z", "2026-10-20T15:00:00Z", "solved", "normal",
     "Intermittent search errors", "A few search requests returned search.5xx errors.", "A cache node was replaced."),
    (1007, "Cobaltfield Systems", "crawler", "Crawl", "2026-11-07T10:00:00Z", None, "open", "normal",
     "Rendering of pages times out", "Page rendering hits crawler.render_timeout for script-heavy pages.", ""),
    (1008, "Dunmore Analytics Group", "indexer", "Index", "2026-11-07T15:00:00Z", None, "open", "low",
     "Documents missing after update", "Some updated documents did not appear; we suspect the index write failed.", ""),
    (1009, "Alderbank Ltd", "scheduler", "Jobs", "2026-11-07T07:30:00Z", None, "open", "normal",
     "Nightly export did not finish", "The scheduled export stopped; scheduler.job_halted appeared in the job history.", ""),
    (1010, "Eastvale", "search-api", "Search", "2026-11-05T13:00:00Z", "2026-11-05T17:00:00Z", "solved", "low",
     "How do I start with search", "We are new and need an introduction to search setup.", f"Shared {DOCS}getting-started."),
    (1011, None, "indexer", "Index", "2026-11-02T09:00:00Z", "2026-11-03T09:00:00Z", "solved", "low",
     "Reindex question", "How long does a full reindex take?", "About one hour per million documents."),
    (1012, "Brightmoor", "crawler", "Crawl", "2026-11-09T04:00:00Z", None, "open", "normal",
     "Fetch timeouts again", "We see crawler.fetch_timeout errors on the nightly crawl.", ""),
]
DOC_PAGES = [
    ("crawler-timeouts", "Crawler timeouts", "crawler", "Crawl", "Fetch and render timeouts: raise the fetch timeout for slow hosts and split very large sitemaps."),
    ("sitemap-limits", "Sitemap limits", "crawler", "Crawl", "A sitemap may list at most fifty thousand URLs; split larger sitemaps into an index."),
    ("index-write-errors", "Index write errors", "indexer", "Index", "When an index write fails, retry the batch; persistent failures usually mean the disk is full."),
    ("search-errors", "Search server errors", "search-api", "Search", "Server errors from the search API are usually caused by an unavailable shard; check shard health."),
    ("scheduler-jobs", "Scheduled jobs", "scheduler", "Jobs", "Scheduled jobs run nightly; a halted job is resumed on the next run."),
    ("getting-started", "Getting started", None, "Search", "Create a collection, add a source and run the first crawl."),
]
TRACKER = [
    ("ENG-101", "Render timeouts on script-heavy pages", "crawler", "Alderbank Ltd", "open", "2026-11-02T10:00:00Z", "2026-11-08T09:00:00Z",
     "Rendering exceeds the time limit for pages with heavy scripts."),
    ("ENG-102", "Retry index writes on transient errors", "indexer", "fernhill-internal", "in_progress", "2026-10-20T10:00:00Z", "2026-11-06T09:00:00Z",
     "Add bounded retries for transient index write failures."),
    ("ENG-103", "Search 500 after shard move", "search-api", None, "open", "2026-11-08T10:00:00Z", "2026-11-08T12:00:00Z",
     "Reported in ticket:1003; shard relocation leaves the search API without a primary."),
    ("ENG-104", "Halted scheduler jobs are not resumed", "scheduler", "Alderbank Ltd", "done", "2026-10-01T10:00:00Z", "2026-10-28T09:00:00Z",
     "Jobs that stop mid-run are now resumed on the next run."),
]
SEARCHES = [
    # day, query text (masked), company, client, searches, zero results, clicks [(doc url, rank)]
    ("2026-11-07", "crawler timeout", "CMP-00042", "portal", 12, 0, [(DOCS + "crawler-timeouts", 1), (DOCS + "sitemap-limits", 3)]),
    ("2026-11-07", "reindex <num> documents", "CMP-00099", "widget", 4, 4, []),
    ("2026-11-08", "search 500 error", "CMP-00017", "portal", 9, 1, [(DOCS + "search-errors", 2), (DOCS + "retired-page", 1)]),
    ("2026-11-08", "crawler timeout", "CMP-00042", "portal", 5, 0, [(DOCS + "crawler-timeouts", 1)]),
]


def _hash(*parts: str, n: int = 16) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:n]


def ticket_documents() -> list[Document]:
    out = []
    for n, account, service, product, created, resolved, status, priority, subject, description, resolution in TICKETS:
        meta = {"source": "ticket", "account": account, "service": service, "product": product, "created_at": created, "resolved_at": resolved, "status": status, "priority": priority}
        fields = {"description": description, **({"resolution": resolution} if resolution else {})}
        out.append(Document(id=f"ticket:{n}", dataset=DATASET, source="ticket", kind="ticket", title=subject, fields=fields,
                            metadata={k: v for k, v in meta.items() if v is not None}, url=f"https://support.example.org/tickets/{n}"))
    return out


def doc_documents() -> list[Document]:
    out = []
    for slug, title, service, product, body in DOC_PAGES:
        meta = {"source": "doc", "service": service, "product": product, "updated_at": "2026-10-01T00:00:00Z"}
        out.append(Document(id=DOCS + slug, dataset=DATASET, source="doc", kind="doc", title=title, fields={"body": body},
                            metadata={k: v for k, v in meta.items() if v is not None}, url=DOCS + slug))
    return out


def tracker_documents() -> list[Document]:
    out = []
    for key, title, component, customer, status, created, updated, body in TRACKER:
        meta = {"source": "tracker", "component": component, "service": component, "customer": customer, "status": status, "created_at": created, "updated_at": updated}
        out.append(Document(id=f"tracker:{key}", dataset=DATASET, source="tracker", kind="tracker_issue", title=title, fields={"description": body},
                            metadata={k: v for k, v in meta.items() if v is not None}, url=f"https://tracker.example.org/browse/{key}"))
    return out


def query_id(text: str) -> str:
    """Day-free query identity (like a log signature): the same masked text on two days shares it."""
    return "q:" + _hash(text)


def analytics_documents(day: str) -> list[Document]:
    out = []
    for d, text, company, client, searches, zero, clicks in SEARCHES:
        if d != day:
            continue
        qid = query_id(text)
        ranks = [r for _u, r in clicks]
        meta = {"source": "analytics", "company_id": company, "search_client": client, "date": day, "query_id": qid, "searches": searches,
                "zero_results": zero, "clicks": len(clicks), "mean_click_rank": (sum(ranks) / len(ranks)) if ranks else None}
        out.append(Document(id=f"analytics:search_query_day:{day}:{qid}:{client}:{company}", dataset=DATASET, source="analytics", kind="search_query_day",
                            title=text, fields={"query_text": text}, metadata={k: v for k, v in meta.items() if v is not None}))
        for url, rank in clicks:
            out.append(Document(id=f"analytics:search_click:{day}:{qid}:{client}:{company}:{rank}", dataset=DATASET, source="analytics", kind="search_click",
                                title=f"{text} -> rank {rank}", fields={"query_text": text},
                                metadata={"source": "analytics", "company_id": company, "search_client": client, "date": day, "query_id": qid, "doc_id": url, "rank": rank}))
    return out


def search_query_id(day: str, text: str, client: str, company: str) -> str:
    return f"analytics:search_query_day:{day}:{query_id(text)}:{client}:{company}"


# --- shared metadata schema ------------------------------------------------------------------------

SOURCES = ["ticket", "doc", "tracker", "logs", "analytics"]
_STRING = "string"
FIELD_TYPES: dict[str, dict[str, Any]] = {
    "source": {"type": "enum", "enum_values": SOURCES, "listable": True, "groupable": True},
    # tickets / docs / tracker
    "account": {"type": _STRING, "listable": True, "groupable": True, "description": "Raw customer account name as entered in the ticket (identity namespace support.account)."},
    "customer": {"type": _STRING, "listable": True, "description": "Raw customer field of a tracker issue (identity namespace tracker.customer)."},
    "product": {"type": _STRING, "listable": True, "groupable": True},
    "service": {"type": _STRING, "listable": True, "groupable": True, "description": "Service family (crawler, indexer, search-api, scheduler)."},
    "component": {"type": _STRING, "listable": True, "groupable": True},
    "status": {"type": _STRING, "listable": True, "groupable": True},
    "priority": {"type": _STRING, "listable": True, "groupable": True},
    "created_at": {"type": "datetime", "sortable": True, "time_basis": "created"},
    "resolved_at": {"type": "datetime", "sortable": True, "time_basis": "resolved"},
    "updated_at": {"type": "datetime", "sortable": True, "time_basis": "updated"},
    # analytics
    "company_id": {"type": "id", "listable": True, "groupable": True, "description": "Raw company id of the search analytics upload (identity namespace analytics.company_id)."},
    "search_client": {"type": _STRING, "listable": True, "groupable": True},
    "query_id": {"type": "id"},
    "doc_id": {"type": "id"},
    "searches": {"type": "integer", "sortable": True},
    "zero_results": {"type": "integer", "sortable": True},
    "clicks": {"type": "integer", "sortable": True},
    "mean_click_rank": {"type": "number", "additive": False},
    "rank": {"type": "integer", "sortable": True},
    # logs (as the compose output)
    "tenant": {"type": _STRING, "listable": True, "groupable": True, "description": "Log tenant code (identity namespace logs.tenant)."},
    "env": {"type": _STRING, "listable": True, "groupable": True},
    "services": {"type": _STRING},
    "signature_id": {"type": _STRING, "listable": True, "groupable": True},
    "signatures": {"type": _STRING},
    "pattern_id": {"type": _STRING},
    "category": {"type": _STRING, "listable": True, "groupable": True},
    "date": {"type": "date", "sortable": True, "groupable": True, "time_basis": "event"},
    "first_hour": {"type": "datetime", "sortable": True, "time_basis": "first_failure"},
    "last_hour": {"type": "datetime", "sortable": True, "time_basis": "last_failure"},
    "ts_hour": {"type": "datetime", "sortable": True, "time_basis": "hour"},
    "window_start": {"type": "datetime"},
    "window_end": {"type": "datetime"},
    "observed_at": {"type": "datetime"},
    "occurrences": {"type": "integer", "sortable": True},
    "lines": {"type": "integer", "sortable": True},
    "lines_total": {"type": "integer", "sortable": True},
    "source_documents": {"type": "integer"},
    "keyword_documents": {"type": "integer"},
    "rule_documents": {"type": "integer"},
    "unclassified_documents": {"type": "integer"},
    "observed_window_seconds": {"type": "number", "additive": False},
    "community_id": {"type": _STRING},
    "community_family_id": {"type": _STRING},
    "community_label": {"type": _STRING},
    "episode_status": {"type": _STRING},
    "count_basis": {"type": _STRING},
    "collection_scope": {"type": _STRING},
    "recovery_basis": {"type": _STRING, "listable": True},
    "issue": {"type": _STRING},
    "evidence_count": {"type": "integer"},
    "evidence_truncated": {"type": "boolean"},
    "completion_evidence_count": {"type": "integer"},
    "date_basis": {"type": _STRING},
    "date_timezone": {"type": _STRING},
    "level": {"type": _STRING},
    "tenants_affected": {"type": "integer"},
    "row_exactness": {"type": "enum", "enum_values": ["exact", "approximate"], "role": "exactness"},
    "error_type": {"type": _STRING},
    "method": {"type": _STRING},
}


def metadata_schema() -> dict[str, Any]:
    fields = []
    for name in sorted(FIELD_TYPES):
        spec = {"name": name, **FIELD_TYPES[name]}
        if spec.get("type") in {"datetime"}:
            spec.setdefault("timezone_policy", "require_offset")
        fields.append(spec)
    return {"canonical_unit": "document", "unknown_fields": "reject", "timezone": "UTC", "fields": fields}


# --- identity registry -------------------------------------------------------------------------------

CUSTOMERS = [
    ("cust:alderbank", "Alderbank Ltd"),
    ("cust:brightmoor", "Brightmoor"),
    ("cust:cobaltfield", "Cobaltfield Systems"),
    ("cust:dunmore", "Dunmore Analytics"),
    ("cust:eastvale-health", "Eastvale Health"),
    ("cust:eastvale-freight", "Eastvale Freight"),
]
# namespace, raw, customer id (or candidates), status, rule, authority, confidence, evidence, review verdict
ALIASES: list[dict[str, Any]] = [
    {"namespace": "support.account", "raw": "Alderbank Ltd", "customer_id": "cust:alderbank", "status": "matched", "rule": "exact", "authority": "deterministic", "confidence": 1.0},
    {"namespace": "support.account", "raw": "brightmoor", "customer_id": "cust:brightmoor", "status": "matched", "rule": "casefold", "authority": "deterministic", "confidence": 1.0},
    {"namespace": "support.account", "raw": "Brightmoor", "customer_id": "cust:brightmoor", "status": "matched", "rule": "exact", "authority": "deterministic", "confidence": 1.0},
    {"namespace": "support.account", "raw": "Cobaltfield Systems", "customer_id": "cust:cobaltfield", "status": "matched", "rule": "exact", "authority": "deterministic", "confidence": 1.0},
    {"namespace": "support.account", "raw": "Dunmore Analytics Group", "customer_id": "cust:dunmore", "status": "candidate", "rule": "token_set_jaccard", "authority": "deterministic",
     "confidence": 0.67, "review": "rev-3", "note": "model agrees; coordinator has not decided: no join"},
    {"namespace": "support.account", "raw": "Eastvale", "customer_id": None, "candidates": ["cust:eastvale-freight", "cust:eastvale-health"], "status": "ambiguous",
     "rule": "token_set_jaccard", "authority": "deterministic", "confidence": 0.5},
    {"namespace": "support.account", "raw": None, "customer_id": None, "status": "missing", "rule": "none", "authority": "deterministic", "confidence": 0.0,
     "records": ["ticket:1011"]},
    {"namespace": "logs.tenant", "raw": "alderbank-stg", "customer_id": "cust:alderbank", "status": "matched", "rule": "curated", "authority": "coordinator",
     "confidence": 0.95, "evidence": ["ev-1"], "review": "rev-1"},
    {"namespace": "logs.tenant", "raw": "alderbank-prod", "customer_id": "cust:alderbank", "status": "candidate", "rule": "environment_suffix", "authority": "deterministic",
     "confidence": 0.6, "note": "environment-suffix stripping is a candidate only; unreviewed: no join"},
    {"namespace": "logs.tenant", "raw": "brightmoor", "customer_id": "cust:brightmoor", "status": "matched", "rule": "casefold", "authority": "deterministic", "confidence": 1.0},
    {"namespace": "logs.tenant", "raw": "cobalt", "customer_id": "cust:cobaltfield", "status": "contested", "rule": "curated", "authority": "coordinator",
     "confidence": 0.5, "evidence": ["ev-2"], "review": "rev-2", "note": "reviewer refutes: contested, no joins, owner queue"},
    {"namespace": "logs.tenant", "raw": "eastvale", "customer_id": None, "candidates": ["cust:eastvale-freight", "cust:eastvale-health"], "status": "ambiguous",
     "rule": "casefold", "authority": "deterministic", "confidence": 0.5},
    {"namespace": "logs.tenant", "raw": "t-0007", "customer_id": None, "status": "unmatched", "rule": "none", "authority": "deterministic", "confidence": 0.0},
    {"namespace": "analytics.company_id", "raw": "CMP-00017", "customer_id": "cust:brightmoor", "status": "matched", "rule": "curated", "authority": "coordinator",
     "confidence": 0.9, "evidence": ["ev-3"], "review": "rev-4"},
    {"namespace": "analytics.company_id", "raw": "CMP-00042", "customer_id": "cust:alderbank", "status": "matched", "rule": "curated", "authority": "coordinator",
     "confidence": 0.9, "evidence": ["ev-4"], "review": "rev-5"},
    {"namespace": "analytics.company_id", "raw": "CMP-00099", "customer_id": None, "status": "unmatched", "rule": "none", "authority": "deterministic", "confidence": 0.0},
    {"namespace": "tracker.customer", "raw": "Alderbank Ltd", "customer_id": "cust:alderbank", "status": "matched", "rule": "exact", "authority": "deterministic", "confidence": 1.0},
    {"namespace": "tracker.customer", "raw": "fernhill-internal", "customer_id": None, "status": "excluded", "rule": "curated", "authority": "coordinator",
     "confidence": 1.0, "evidence": ["ev-5"], "note": "internal test account"},
    {"namespace": "tracker.customer", "raw": None, "customer_id": None, "status": "missing", "rule": "none", "authority": "deterministic", "confidence": 0.0,
     "records": ["tracker:ENG-103"]},
]
EVIDENCE = [
    {"id": "ev-1", "kind": "curated_list", "url": "https://registry.example.org/tenants/alderbank-stg", "retrieved": "2026-11-09", "quote": "alderbank-stg: staging tenant of Alderbank Ltd"},
    {"id": "ev-2", "kind": "web", "url": "https://www.example.com/cobalt", "retrieved": "2026-11-09", "quote": "Cobalt is a product line name used by several vendors"},
    {"id": "ev-3", "kind": "source_field", "url": "https://analytics.example.org/companies/CMP-00017", "retrieved": "2026-11-09", "quote": "company_name: Brightmoor"},
    {"id": "ev-4", "kind": "source_field", "url": "https://analytics.example.org/companies/CMP-00042", "retrieved": "2026-11-09", "quote": "company_name: Alderbank Ltd"},
    {"id": "ev-5", "kind": "curated_list", "url": "https://registry.example.org/internal", "retrieved": "2026-11-09", "quote": "fernhill-internal: internal test account"},
]
REVIEWS = [
    {"id": "rev-1", "subject": "alias:logs.tenant:alderbank-stg", "model": "fixture-reviewer", "verdict": "agree", "rationale": "curated list names the tenant", "cited_fields": ["ev-1"], "usd": 0.0},
    {"id": "rev-2", "subject": "alias:logs.tenant:cobalt", "model": "fixture-reviewer", "verdict": "refutes", "rationale": "the name is shared by unrelated vendors", "cited_fields": ["ev-2"], "usd": 0.0},
    {"id": "rev-3", "subject": "alias:support.account:Dunmore Analytics Group", "model": "fixture-reviewer", "verdict": "agree", "rationale": "same name with a group suffix", "cited_fields": [], "usd": 0.0},
    {"id": "rev-4", "subject": "alias:analytics.company_id:CMP-00017", "model": "fixture-reviewer", "verdict": "agree", "rationale": "source company name field", "cited_fields": ["ev-3"], "usd": 0.0},
    {"id": "rev-5", "subject": "alias:analytics.company_id:CMP-00042", "model": "fixture-reviewer", "verdict": "agree", "rationale": "source company name field", "cited_fields": ["ev-4"], "usd": 0.0},
]
ALIAS_STATUSES = ["matched", "candidate", "contested", "ambiguous", "excluded", "unmatched", "missing"]


def _normalize(raw: str | None) -> str | None:
    return None if raw is None else " ".join(raw.casefold().split())


def registry() -> dict[str, Any]:
    aliases = []
    for i, a in enumerate(ALIASES, 1):
        entry = {"id": f"alias-{i:02d}", "namespace": a["namespace"], "raw": a["raw"], "normalized": _normalize(a["raw"]), "customer_id": a["customer_id"],
                 "status": a["status"], "rule": a["rule"], "authority": a["authority"], "confidence": a["confidence"],
                 "evidence": a.get("evidence", []), "review": a.get("review"), "effective_from": "2026-11-09", "effective_to": None, "version": 1}
        for extra in ("candidates", "records", "note"):
            if extra in a:
                entry[extra] = a[extra]
        aliases.append(entry)
    report: dict[str, dict[str, int]] = {}
    for a in aliases:
        row = report.setdefault(a["namespace"], {"total": 0, **{s: 0 for s in ALIAS_STATUSES}})
        row["total"] += 1
        row[a["status"]] += 1
    joins = sorted({(a["namespace"], a["raw"], a["customer_id"]) for a in aliases if a["status"] == "matched"})
    return {
        "format_version": FORMAT_VERSION,
        "registry_version": "fixture-r1",
        "synthetic": True,
        "statuses": ALIAS_STATUSES,
        "auto_match_rules": ["exact", "casefold"],
        "customers": [{"customer_id": c, "name": n} for c, n in CUSTOMERS],
        "aliases": aliases,
        "evidence": EVIDENCE,
        "reviews": REVIEWS,
        "expected": {
            "report": report,
            "joinable": [{"namespace": ns, "raw": raw, "customer_id": cid} for ns, raw, cid in joins],
            "not_joinable_statuses": [s for s in ALIAS_STATUSES if s != "matched"],
        },
    }


# --- links -----------------------------------------------------------------------------------------

LINK_STATUSES = ["accepted", "candidate", "model_accepted", "model_rejected", "human_accepted", "human_rejected", "contested"]


def links() -> dict[str, Any]:
    q_crawler = search_query_id("2026-11-07", "crawler timeout", "portal", "CMP-00042")
    q_search = search_query_id("2026-11-08", "search 500 error", "portal", "CMP-00017")
    candidates = [
        {"id": "L01", "pair": "ticket-episode", "tier": "A", "status": "accepted", "subject_id": "ticket:1004", "object_id": incident_id("chainB_live"),
         "builder": "explicit_ref", "evidence": {"mention": incident_id("chainB_live"), "field": "description"},
         "note": "the object id is held by archive day 2026-11-06 and by the 2026-11-07 base slice"},
        {"id": "L02", "pair": "ticket-episode", "tier": "B", "status": "model_accepted", "subject_id": "ticket:1005", "object_id": incident_id("chainA_1"),
         "builder": "customer_window_signature", "evidence": {"customer_id": "cust:alderbank", "aliases": ["support.account:Alderbank Ltd", "logs.tenant:alderbank-stg"],
                                                              "window": ["2026-11-04T23:52:00Z", "2026-11-12T23:59:30Z"], "mention": "crawler.fetch_timeout"}, "review": "lrev-1"},
        {"id": "L03", "pair": "ticket-episode", "tier": "B", "status": "human_accepted", "subject_id": "ticket:1002", "object_id": incident_id("oct_alderbank_crawler"),
         "builder": "customer_window_signature", "evidence": {"customer_id": "cust:alderbank", "aliases": ["support.account:Alderbank Ltd", "logs.tenant:alderbank-stg"],
                                                              "window": ["2026-10-29T13:10:00Z", "2026-11-06T13:50:00Z"], "mention": "crawler.timeout", "epoch": "kw-e1"}},
        {"id": "L04", "pair": "ticket-episode", "tier": "B", "status": "candidate", "subject_id": "ticket:1003", "object_id": incident_id("d08_brightmoor_search"),
         "builder": "customer_window_signature", "evidence": {"customer_id": "cust:brightmoor", "aliases": ["support.account:brightmoor", "logs.tenant:brightmoor"],
                                                              "window": ["2026-11-07T08:40:00Z", "2026-11-15T09:30:00Z"], "mention": "search.5xx"}},
        {"id": "L05", "pair": "ticket-family", "tier": "B", "status": "model_accepted", "subject_id": "ticket:1012", "object_id": "family:crawler.fetch_timeout",
         "builder": "signature_mention", "evidence": {"mention": "crawler.fetch_timeout"}, "review": "lrev-2"},
        {"id": "L06", "pair": "ticket-doc", "tier": "A", "status": "accepted", "subject_id": "ticket:1001", "object_id": DOCS + "crawler-timeouts",
         "builder": "explicit_ref", "evidence": {"mention": DOCS + "crawler-timeouts", "field": "resolution"}},
        {"id": "L07", "pair": "ticket-doc", "tier": "C", "status": "model_rejected", "subject_id": "ticket:1010", "object_id": DOCS + "search-errors",
         "builder": "similarity", "evidence": {"score": 0.41}, "review": "lrev-3"},
        {"id": "L08", "pair": "family-doc", "tier": "B", "status": "human_rejected", "subject_id": "family:scheduler.job_halted", "object_id": DOCS + "getting-started",
         "builder": "service_mention", "evidence": {"mention": "scheduler"}},
        {"id": "L09", "pair": "family-doc", "tier": "C", "status": "candidate", "subject_id": "family:index.write_failed", "object_id": DOCS + "index-write-errors",
         "builder": "similarity", "evidence": {"score": 0.72}},
        {"id": "L10", "pair": "query-doc", "tier": "A", "status": "accepted", "subject_id": q_crawler, "object_id": DOCS + "crawler-timeouts",
         "builder": "click", "evidence": {"rank": 1, "clicks": 1}},
        {"id": "L11", "pair": "query-ticket", "tier": "C", "status": "candidate", "subject_id": q_search, "object_id": "ticket:1003",
         "builder": "similarity", "evidence": {"score": 0.66}},
        {"id": "L12", "pair": "ticket-episode", "tier": "C", "status": "contested", "subject_id": "ticket:1008", "object_id": incident_id("d07_eastvale_index"),
         "builder": "similarity", "evidence": {"score": 0.58}, "review": "lrev-4", "note": "reviewer and coordinator disagree"},
    ]
    return {
        "format_version": FORMAT_VERSION,
        "link_version": "fixture-l1",
        "registry_version": "fixture-r1",
        "synthetic": True,
        "statuses": LINK_STATUSES,
        "window": {"before_days": 1, "after_days": 7},
        "display": {"shown_labelled": ["candidate", "model_accepted", "contested"], "shown": ["accepted", "human_accepted"], "hidden": ["model_rejected", "human_rejected"]},
        "candidates": candidates,
        "reviews": [
            {"id": "lrev-1", "link": "L02", "model": "fixture-reviewer", "verdict": "agree", "usd": 0.0},
            {"id": "lrev-2", "link": "L05", "model": "fixture-reviewer", "verdict": "agree", "usd": 0.0},
            {"id": "lrev-3", "link": "L07", "model": "fixture-reviewer", "verdict": "refutes", "usd": 0.0},
            {"id": "lrev-4", "link": "L12", "model": "fixture-reviewer", "verdict": "agree", "usd": 0.0},
        ],
        "expected": {
            "planted": [{"link": c["id"], "tier": c["tier"], "subject_id": c["subject_id"], "object_id": c["object_id"]} for c in candidates if c["tier"] in {"A", "B"}
                        and c["status"] not in {"human_rejected"}],
            "negatives": [
                {"subject_id": "ticket:1006", "object_id": incident_id("d08_brightmoor_search"), "reason": "outside_window",
                 "detail": "ticket created 2026-10-20, episode 2026-11-08: outside [first - 1 d, last + 7 d]"},
                {"subject_id": "ticket:1007", "object_id": incident_id("d07_cobalt_render"), "reason": "contested_alias", "detail": "logs.tenant:cobalt is contested"},
                {"subject_id": "ticket:1009", "object_id": incident_id("d07_alderbank_sched"), "reason": "unreviewed_candidate_alias",
                 "detail": "logs.tenant:alderbank-prod is an environment-suffix candidate"},
                {"subject_id": "ticket:1010", "object_id": incident_id("d05_eastvale_search"), "reason": "ambiguous_alias", "detail": "support.account:Eastvale is ambiguous"},
                {"subject_id": search_query_id("2026-11-08", "search 500 error", "portal", "CMP-00017"), "object_id": DOCS + "retired-page", "reason": "dangling_object",
                 "detail": "clicked URL is not a document of the collection"},
            ],
        },
    }


# --- manifest, ledger, traps -----------------------------------------------------------------------


def _jsonl(docs: list[Document]) -> bytes:
    return "".join(d.model_dump_json(exclude_none=True) + "\n" for d in docs).encode()


def _yaml(data: Any) -> bytes:
    return ("# Synthetic fixture (unified merge plan F0). Generated by su_multi_arm.testing.fixtures.unified; do not edit by hand.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=200)).encode()


def _counts(docs: list[Document]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in docs:
        out[d.kind] = out.get(d.kind, 0) + 1
    return dict(sorted(out.items()))


def ledger(units: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every registered slice per day (``status: current | replaced``) and the current vector. A
    full-day rebuild (``level: day``) replaces the earlier slices of its day; ``retires`` lists the
    ids those slices held that the rebuild no longer has (the tombstones latest-wins cannot give)."""
    days: dict[str, list[dict[str, Any]]] = {}
    as_of = {"2026-11-07/base": "T12:00:00Z", "2026-11-07/h13": "T14:00:00Z", "2026-11-08/base": "T12:00:00Z", "2026-11-08/h22": "T23:00:00Z",
             "2026-11-08/h23": "T23:59:59Z", "2026-11-09/base": "T06:00:00Z", "2026-11-09/h09": "T10:00:00Z", "2026-11-09/m1025": "T10:25:00Z"}
    seen: dict[str, str] = {}
    replaced: set[str] = set()
    by_slice = {u["slice"]: u for u in units.values() if "slice" in u}
    for path, unit in units.items():
        if "slice" not in unit:
            continue
        day, name = unit["slice"].split("/")
        ids = [d.id for d in unit["docs"]]
        level = {"base": "base", "day": "day"}.get(name) or ("hourly" if name.startswith("h") else "five_minute")
        stamp = (_ts(day + "T00:00:00Z") + timedelta(days=1)).date().isoformat() + "T06:00:00Z" if level == "day" else day + as_of[unit["slice"]]
        entry: dict[str, Any] = {"slice_id": f"slice:{day}:{name}", "level": level, "file": path, "as_of": stamp, "documents": len(ids),
                                 "sha256": hashlib.sha256(_jsonl(unit["docs"])).hexdigest(), "keyword_epoch": unit["epoch"]}
        if level == "day":
            before = {d.id for r in unit["replaces"] for d in by_slice[r]["docs"]}
            entry["replaces"] = [f"slice:{r.replace('/', ':')}" for r in unit["replaces"]]
            entry["retires"] = sorted(before - set(ids))
            replaced.update(entry["replaces"])
        else:
            entry["supersedes"] = [{"id": i, "previous": seen[i]} for i in sorted({i for i in ids if i in seen and d_kind(i) != "signature"})]
        days.setdefault(day, []).append(entry)
        for i in ids:
            seen[i] = entry["slice_id"]
    for slices in days.values():
        for s in slices:
            s["status"] = "replaced" if s["slice_id"] in replaced else "current"
    return {"format_version": FORMAT_VERSION, "member": "logs", "tier": "provisional", "now": FIXTURE_NOW,
            "current": {day: [s["slice_id"] for s in slices if s["status"] == "current"] for day, slices in days.items()}, "days": days,
            "rule": ("latest current slice wins per document id across the pinned slices of the live window; a day rebuild replaces "
                     "its day's slices and retires the ids it no longer holds")}


def d_kind(doc_id: str) -> str:
    return doc_id.split(":")[1] if doc_id.startswith("logs:") else ""


def manifest(units: dict[str, dict[str, Any]], files: dict[str, bytes]) -> dict[str, Any]:
    support = [*ticket_documents(), *doc_documents()]
    members = [
        {"id": "support", "kind": "support", "tier": "adopted", "key": "adopted:" + hashlib.sha256(files["support/adopted.jsonl"]).hexdigest()[:16],
         "files": ["support/adopted.jsonl"], "record_kinds": ["ticket", "doc"], "sources": ["ticket", "doc"], "cadence": "adopted",
         "id_namespaces": ["ticket:", DOCS], "identity_namespace": "support.account",
         "time_roles": {"ticket": {"event_time": "created_at", "update_time": "resolved_at"}, "doc": {"update_time": "updated_at"}},
         "canonical_units": {"ticket": "ticket", "doc": "doc"}, "embed_fields": {"ticket": ["title", "description", "resolution"], "doc": ["title", "body"]},
         "recipe": {"embedding_space": "fixture-space-1", "analyzer": {"engine": "bm25", "tokenizer": "ascii_alnum"}, "chunking": {"size": 1600, "overlap": 160}},
         "layers": ["graph_attach", "segment_sidecar", "communities_imported"], "counts": _counts(support)},
        {"id": "tracker", "kind": "tracker", "tier": "snapshot", "key": "snapshot:2026-11-08", "files": ["tracker/snapshot-2026-11-08.jsonl"],
         "record_kinds": ["tracker_issue"], "sources": ["tracker"], "cadence": "daily", "id_namespaces": ["tracker:"], "identity_namespace": "tracker.customer",
         "time_roles": {"tracker_issue": {"event_time": "created_at", "update_time": "updated_at"}}, "canonical_units": {"tracker_issue": "issue"},
         "embed_fields": {"tracker_issue": ["title", "description"]},
         "recipe": {"embedding_space": "fixture-space-1", "analyzer": {"engine": "bm25", "tokenizer": "unicode_word"}, "chunking": {"size": 1600, "overlap": 160}},
         "counts": _counts(tracker_documents())},
    ]
    for path, unit in units.items():
        if "key" in unit:
            tier = "archive"
            members.append({"id": "logs", "kind": "logs", "tier": tier, "key": unit["key"], "files": [path], "sources": ["logs"], "cadence": "close_day",
                            "id_namespaces": ["logs:"], "identity_namespace": "logs.tenant", "keyword_epoch": unit["epoch"],
                            "time_roles": {"incident": {"event_time": "first_hour"}, "episode_cell": {"event_time": "ts_hour"}},
                            "canonical_units": {"incident": "episode", "episode_cell": "episode", "shared_error": "episode", "signature": "signature",
                                                "signature_day": "episode", "unit_day": "unit_day", "summary_coverage": "unit_day"},
                            "embed_fields": {"incident": ["title", "summary"], "shared_error": ["title", "summary"]},
                            "recipe": {"embedding_space": "fixture-space-1", "analyzer": {"engine": "bm25", "tokenizer": "ascii_alnum"},
                                       "chunking": {"size": 1600, "overlap": 160}, "content": {"keyword_epoch": unit["epoch"]}},
                            "counts": _counts(unit["docs"])})
    for day in ("2026-11-07", "2026-11-08"):
        path = f"analytics/day-{day}.jsonl"
        members.append({"id": "analytics", "kind": "analytics", "tier": "snapshot", "key": f"day:{day}", "files": [path], "sources": ["analytics"], "cadence": "daily",
                        "id_namespaces": ["analytics:"], "identity_namespace": "analytics.company_id",
                        "time_roles": {"search_query_day": {"event_time": "date"}, "search_click": {"event_time": "date"}},
                        "canonical_units": {"search_query_day": "search", "search_click": "click"}, "embed_fields": {"search_query_day": ["title"]},
                        "recipe": {"embedding_space": "fixture-space-1", "analyzer": {"engine": "bm25", "tokenizer": "unicode_word"}, "chunking": {"size": 1600, "overlap": 160}},
                        "counts": _counts(analytics_documents(day))})

    # Collisions: the same id in more than one member/slice (real log id behaviour).
    holders: dict[str, list[str]] = {}
    for path, unit in units.items():
        for d in unit["docs"]:
            holders.setdefault(d.id, []).append(unit.get("key") or "slice:" + unit["slice"].replace("/", ":"))
    signature_ids = sorted(i for i in holders if d_kind(i) == "signature")
    archive_vs_live = sorted(i for i, h in holders.items() if d_kind(i) != "signature" and any(x.startswith(("day:", "month:")) for x in h)
                             and any(x.startswith("slice:") for x in h))
    within_live = sorted(i for i, h in holders.items() if d_kind(i) != "signature" and len(h) > 1 and all(x.startswith("slice:") for x in h))
    routing = [
        {"id": "ticket:1003", "member": "support"},
        {"id": DOCS + "crawler-timeouts", "member": "support"},
        {"id": "tracker:ENG-103", "member": "tracker"},
        {"id": search_query_id("2026-11-08", "search 500 error", "portal", "CMP-00017"), "member": "analytics", "key": "day:2026-11-08"},
        {"id": incident_id("chainA_1"), "member": "logs", "holders": holders[incident_id("chainA_1")]},
        {"id": incident_id("chainA_2"), "member": "logs", "holders": holders[incident_id("chainA_2")]},
        {"id": incident_id("chainB_live"), "member": "logs", "holders": holders[incident_id("chainB_live")]},
        {"id": incident_id("D_v2"), "member": "logs", "holders": holders[incident_id("D_v2")]},
        {"id": "ticket:9999", "member": None, "reason": "namespace routes to support; id absent: not found"},
        {"id": "zzz:1", "member": None, "reason": "no declared namespace: not found, never a scan"},
        {"id": DOCS + "retired-page", "member": None, "reason": "namespace routes to support; id absent: not found"},
    ]
    return {
        "format_version": FORMAT_VERSION,
        "synthetic": True,
        "description": "Synthetic unified collection for the partition core, live slices, identity registry and link tests (unified merge plan F0).",
        "dataset": DATASET,
        "now": FIXTURE_NOW,
        "question_clock": {"display_timezone": DISPLAY_TIMEZONE, "partition_clock": "UTC", "default_view_days": DEFAULT_VIEW_DAYS},
        "embedding_space": {"id": "fixture-space-1", "model": "hash-demo-v1", "dimensions": 256, "template": "engine_template"},
        "keyword_epochs": EPOCHS,
        "metadata_schema": metadata_schema(),
        "members": members,
        "layers": [
            {"id": "graph_attach", "covers": ["ticket", "doc"], "arm": "graph", "note": "the live_shape config attaches a kuzu store; no kuzu file in this fixture"},
            {"id": "segment_sidecar", "covers": ["ticket"], "arm": "segment"},
            {"id": "communities_imported", "covers": ["ticket", "doc"], "arm": "community"},
            {"id": "communities_shared_errors", "covers": ["logs"], "arm": "community"},
            {"id": "identity", "file": "identity/registry.yaml", "version": "fixture-r1"},
            {"id": "links", "file": "links/candidates.yaml", "version": "fixture-l1"},
        ],
        "slices": {"ledger": "logs/ledger.yaml"},
        "expected": {
            "tiers": {"2026-10": "archive", "2026-11-04": "archive", "2026-11-05": "archive", "2026-11-06": "archive",
                      "2026-11-07": "provisional", "2026-11-08": "provisional", "2026-11-09": "provisional"},
            "partial_days": ["2026-11-09"],
            "live_window": ["2026-11-07", "2026-11-08", "2026-11-09"],
            "bounded_kinds_only_in_slices": ["unclassified_example"],
            "line_text_fields": {"signature": ["exemplars"], "signature_examples": ["exemplars"]},
            "collisions": {
                "signature_catalog_every_day": {"ids": signature_ids, "holders": {i: holders[i] for i in signature_ids},
                                                "note": "rule-catalog documents have day-independent ids and are emitted by every day's build"},
                "archive_and_live": {"ids": archive_vs_live, "holders": {i: holders[i] for i in archive_vs_live},
                                     "note": "an episode crossing midnight from a closed day into the live window keeps its id (hash of key + first failure)"},
                "superseded_in_live": {"ids": within_live, "holders": {i: holders[i] for i in within_live},
                                       "note": "a later slice supersedes the version of an earlier slice (latest wins), also across days"},
            },
            "chains": [
                {"id": "A", "kind": "two pieces across archive days", "pieces": [incident_id("chainA_1"), incident_id("chainA_2")], "days": ["2026-11-05", "2026-11-06"]},
                {"id": "B", "kind": "one id in archive and provisional tiers", "pieces": [incident_id("chainB_live")], "days": ["2026-11-06", "2026-11-07"],
                 "versions": {"day:2026-11-06": 6, "slice:2026-11-07:base": 15, "slice:2026-11-07:day": 15}},
                {"id": "C", "kind": "one id superseded across provisional days", "pieces": [incident_id("chainC_v2")], "days": ["2026-11-08", "2026-11-09"],
                 "versions": {"slice:2026-11-08:h23": 4, "slice:2026-11-09:base": 7}, "metadata_date": "2026-11-08"},
                {"id": "D", "kind": "superseded within one day", "pieces": [incident_id("D_v2")], "days": ["2026-11-09"],
                 "versions": {"slice:2026-11-09:h09": 14, "slice:2026-11-09:m1025": 21}},
            ],
            "gap_merge": {
                "day": "2026-11-07", "merge_gap_seconds": MERGE_GAP_SECONDS, "late_lines": LATE_LINES,
                "before": {"slice": "slice:2026-11-07:h13", "incident_ids": [incident_id("E1"), incident_id("E2")],
                           "signature_day_ids": [incident_documents(P["E1"])[1].id, incident_documents(P["E2"])[1].id],
                           "episode_row_ids": [episode_row_id(P["E1"]), episode_row_id(P["E2"])], "occurrences": [6, 8]},
                "after": {"slice": "slice:2026-11-07:day", "incident_ids": [incident_id("E_merged")],
                          "signature_day_ids": [incident_documents(P["E_merged"])[1].id], "episode_row_ids": [episode_row_id(P["E_merged"])], "occurrences": [17]},
                "survivor": incident_id("E1"),
                "tombstones": sorted([incident_id("E2"), incident_documents(P["E2"])[1].id]),
                "note": "the merged group keeps E1's id (same key and first failure); E2's ids vanish from the full-day build while the h13 slice still holds them",
            },
            "routing": routing,
            "service_family_terms": {"crawler": ["crawl", "crawler", "fetch", "render"], "search-api": ["search", "query"], "indexer": ["index", "indexing"], "scheduler": ["job", "export", "schedule"]},
        },
    }


def traps() -> dict[str, Any]:
    dup = Document(id="ticket:1003", dataset=DATASET, source="ticket", kind="ticket", title="Duplicate of an adopted ticket", fields={"description": "Same id as the adopted member."},
                   metadata={"source": "ticket", "status": "open"})
    late = [*incident_documents(Piece("late", "brightmoor", "prod", "indexer", "index.write_failed", "2026-11-06T18:00:00Z", "2026-11-06T18:05:00Z", 2))]
    return {
        "traps/duplicate_id_member.jsonl": _jsonl([dup]),
        "traps/archived_day_slice.jsonl": _jsonl(late),
        "traps/traps.yaml": _yaml({
            "format_version": FORMAT_VERSION,
            "synthetic": True,
            "traps": [
                {"id": "duplicate_id_member", "file": "traps/duplicate_id_member.jsonl", "member": {"id": "ticket", "tier": "snapshot", "key": "snapshot:2026-11-09", "id_namespaces": ["ticket:"]},
                 "expect": "refused", "reason": "document id ticket:1003 already belongs to member support"},
                {"id": "archived_day_slice", "file": "traps/archived_day_slice.jsonl", "slice": {"day": "2026-11-06", "slice_id": "slice:2026-11-06:late", "level": "five_minute"},
                 "expect": "refused", "reason": "day 2026-11-06 is archived; a day is in exactly one tier (archive wins)"},
                {"id": "line_text_embedded", "member": {"id": "logs", "embed_fields": {"signature": ["exemplars"]}},
                 "expect": "refused", "reason": "log-line text fields are never embedded (retention invariant)"},
                {"id": "foreign_embedding_space", "member": {"id": "tracker", "recipe": {"embedding_space": "other-space-2"}},
                 "expect": "dense_refused", "reason": "dense arm refuses a member from another embedding space; lexical and records still serve"},
                {"id": "v2_with_collection", "config": {"schema_version": 2, "collection": {"mode": "partitioned"}},
                 "expect": "refused", "reason": "a v2 config with a collection block is refused with a migration message"},
                {"id": "unroutable_ids", "ids": ["zzz:1", "", "logs:"], "expect": "not_found", "reason": "never a scan of all members"},
            ],
        }),
    }


def onboarding() -> bytes:
    """The plan's §8.3 worked example (search analytics) as an onboarding file (D1–D11)."""
    return _yaml({
        "format_version": FORMAT_VERSION,
        "synthetic": True,
        "member": "analytics",
        "D1_provenance": {"owner": "analytics team (synthetic)", "terms": "internal upload", "privacy_class": "internal", "retention_days": 400,
                          "masking": ["query_text: numbers and e-mail addresses masked"]},
        "D2_kinds": {"search_query_day": {"grain": "company x client x masked query x day", "id": "analytics:search_query_day:{day}:{query_id}:{client}:{company}",
                                          "unit": "search", "time_roles": {"event_time": "date", "ingest_time": "upload"}},
                     "search_click": {"grain": "query x clicked document x rank x day", "id": "analytics:search_click:{day}:{query_id}:{client}:{company}:{rank}",
                                      "unit": "click", "time_roles": {"event_time": "date"}}},
        "D3_field_map": {"core": {"source": "analytics", "kind": "kind", "title": "query_text", "event_time": "date", "customer_ref": "company_id"},
                         "absent": ["service", "component", "version", "status", "url"],
                         "source_fields": {"search_client": "string", "searches": "integer", "zero_results": "integer", "clicks": "integer",
                                           "mean_click_rank": "number (non-additive)", "doc_id": "id", "rank": "integer"}},
        "D4_identity": {"customer_field": "company_id", "namespace": "analytics.company_id", "candidates": ["curated from the source company name field"], "review": "every non-exact"},
        "D5_text": {"lexical_fields": ["query_text"], "dense_fields": ["title"], "chunking": {"size": 1600, "overlap": 160}, "dedupe": "query_id"},
        "D6_sql": {"tables": ["search_query_day", "search_click"], "relations": ["search_click.doc_id -> doc.doc_id", "search_query_day.customer_id -> customer.customer_id"],
                   "measures": {"searches": "search", "zero_results": "search", "clicks": "click"}},
        "D7_graph": {"nodes": ["search_query", "doc"], "edges": ["clicked"]},
        "D8_communities": "none at first",
        "D9_links": [{"pair": "query-doc", "tier": "A", "basis": "clicks"}, {"pair": "query-ticket", "tier": "C", "basis": "similarity"}],
        "D10_cadence": {"key": "day:{date}", "producer": "daily upload", "first_member": "external workspace"},
        "D11_evolution": {"epochs": "a masking change opens a new epoch", "offboarding": "members dropped from compose, links tombstoned, namespace frozen"},
    })


def build_files() -> dict[str, bytes]:
    """Every committed fixture file, keyed by its path relative to ``tests/fixtures/unified``."""
    units = log_units()
    files: dict[str, bytes] = {
        "support/adopted.jsonl": _jsonl([*ticket_documents(), *doc_documents()]),
        "tracker/snapshot-2026-11-08.jsonl": _jsonl(tracker_documents()),
        "analytics/day-2026-11-07.jsonl": _jsonl(analytics_documents("2026-11-07")),
        "analytics/day-2026-11-08.jsonl": _jsonl(analytics_documents("2026-11-08")),
    }
    for path, unit in units.items():
        files[path] = _jsonl(unit["docs"])
    files["logs/ledger.yaml"] = _yaml(ledger(units))
    files["identity/registry.yaml"] = _yaml(registry())
    files["links/candidates.yaml"] = _yaml(links())
    files.update(traps())
    files["onboarding/analytics.yaml"] = onboarding()
    files["collection.yaml"] = _yaml(manifest(units, files))
    from .live_shape import template_files

    files.update({f"live_shape/{name}": data for name, data in template_files().items()})
    return dict(sorted(files.items()))


def write_files(root: str | Path) -> list[str]:
    root = Path(root)
    written = []
    for name, data in build_files().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        written.append(name)
    return written


# --- loading -----------------------------------------------------------------------------------------


def read_documents(path: Path) -> list[Document]:
    return [Document.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


@dataclass
class UnifiedFixture:
    root: Path
    manifest: dict[str, Any]
    ledger: dict[str, Any]
    registry: dict[str, Any]
    links: dict[str, Any]
    traps: dict[str, Any]
    members: dict[str, list[Document]] = field(default_factory=dict)  # "<member>/<key>" -> documents
    slices: dict[str, list[Document]] = field(default_factory=dict)  # slice id -> documents

    def documents(self) -> list[Document]:
        """Every document version of every member and slice (ids repeat: see ``expected.collisions``)."""
        return [d for docs in (*self.members.values(), *self.slices.values()) for d in docs]

    def live_view(self, day: str) -> dict[str, Document]:
        """The pinned slices of ``day`` in ledger order, latest version per id."""
        view: dict[str, Document] = {}
        for slice_id in self.ledger["current"].get(day, []):
            for d in self.slices[slice_id]:
                view[d.id] = d
        return view


def load(root: str | Path) -> UnifiedFixture:
    root = Path(root)
    read = lambda name: yaml.safe_load((root / name).read_text())  # noqa: E731
    fixture = UnifiedFixture(root, read("collection.yaml"), read("logs/ledger.yaml"), read("identity/registry.yaml"), read("links/candidates.yaml"), read("traps/traps.yaml"))
    for member in fixture.manifest["members"]:
        fixture.members[f"{member['id']}/{member['key']}"] = [d for f in member["files"] for d in read_documents(root / f)]
    for slices in fixture.ledger["days"].values():
        for s in slices:
            fixture.slices[s["slice_id"]] = read_documents(root / s["file"])
    return fixture
