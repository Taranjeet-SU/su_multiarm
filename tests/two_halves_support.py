"""The unified collection as one served engine for the two-halves tests (unified merge M2b-2).

Operations half: an episode archive with closed days 2026-11-04..08 and a provisional (rolling)
2026-11-09 up to the F0 clock (``FIXTURE_NOW``), under a synthetic failure keyword family and the
success heartbeat family, for the F0 log tenants (``alderbank-stg``, ``brightmoor``, ``cobalt``).
Knowledge half: the F0 support member (tickets and docs) plus a few synthetic tickets for the
question shapes F0 does not cover, published into the same generation as the archive's live window
(the stand-in for the log member that lane LB wires into the partitioned collection). Cross-source:
the F0 identity registry imported and pinned, and a small pinned link store (one explicit tier-A
link and one tier-B candidate). Question clock: F0's half-hour-offset zone.

Everything is synthetic (tenants, services, rules, phrases, tickets, docs). No model is called: the
planner and the generator are fake ``JsonModel.call`` replies installed by the tests.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document
from su_multi_arm.crosssource.identity import import_registry, sha256_file
from su_multi_arm.crosssource.links import Link, LinkStore
from su_multi_arm.engine import Engine
from su_multi_arm.ingestion.episode_archive import EpisodeArchive
from su_multi_arm.ingestion.episode_summaries import SummaryRule, heartbeat_buckets, summary_documents
from su_multi_arm.testing.fixtures.unified import load
from su_multi_arm.testing.fixtures.unified.collection import DISPLAY_TIMEZONE
from tests.history_support import stage, utc
from tests.log_policy_support import policy_logs, snapshot, write_family
from tests.logs_gold_support import gold_config
from tests.logs_support import write_library

F0 = Path(__file__).resolve().parent / "fixtures" / "unified"
NOW = datetime(2026, 11, 9, 10, 30, tzinfo=timezone.utc)  # F0 FIXTURE_NOW
TODAY = "2026-11-09"
CLOSED = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
DATASET = "unified-twohalves"

FAILURES = {
    "family": "synthetic_ops",
    "phrases": ["fetch timed out", "request unauthorized", "report job still queued", "search latency exceeded"],
    "rules": [
        {"id": "crawler.fetch_timeout", "title": "Crawler fetch timed out", "category": "crawl", "alternatives": [["fetch timed out"]]},
        {"id": "auth.unauthorized", "title": "Unauthorized request rejected", "category": "auth", "alternatives": [["request unauthorized"]]},
        {"id": "report.queue_stalled", "title": "Report job waiting in queue", "category": "jobs", "alternatives": [["report job still queued"]]},
        {"id": "search.slow_query", "title": "Search latency limit exceeded", "category": "search", "alternatives": [["search latency exceeded"]]},
    ],
}
SUCCESSES = {
    "family": "synthetic_success",
    "phrases": ["crawl run finished ok", "report mailed ok"],
    "rules": [
        {"id": "success.crawl_completed", "title": "Crawl completed", "category": "success", "alternatives": [["crawl run finished ok"]]},
        {"id": "success.report_delivered", "title": "Report delivered", "category": "success", "alternatives": [["report mailed ok"]]},
    ],
}
RULES = [SummaryRule.model_validate(r) for r in [*FAILURES["rules"], *SUCCESSES["rules"]]]
PHRASES = [*FAILURES["phrases"], *SUCCESSES["phrases"]]
SUCCESS_IDS = {r["id"] for r in SUCCESSES["rules"]}

VOCABULARY = [
    {"term": "crawl", "match": "prefix", "values": ["crawler"]},
    {"term": "mcp", "values": ["mcp-gateway"], "note": "the MCP connector is served by mcp-gateway"},
    {"term": "log in", "values": ["auth-service"]},
    {"term": "login", "values": ["auth-service"]},
    {"term": "report", "match": "prefix", "values": ["report-worker"]},
    {"term": "search", "match": "prefix", "values": ["search-api"]},
    {"term": "slow", "field": "symptom"},
]

# Synthetic tickets for shapes the F0 support member does not cover (MCP auth, login, reports).
EXTRA_TICKETS = [
    ("ticket:2001", "Brightmoor", "mcp-gateway", "MCP connector returns 401 after token rotation",
     "The MCP connector rejects every call with 401 unauthorized although the credentials were entered correctly.",
     "The connector cached the previous signing key; restarting the connector after the token rotation fixed it."),
    ("ticket:2002", "Alderbank Ltd", "auth-service", "New admin user cannot log in",
     "A user added in the admin console cannot log in; the login page says the credentials are invalid.",
     "The user had not accepted the invitation; resending the invitation and assigning a role resolved it."),
    ("ticket:2003", "Alderbank Ltd", "report-worker", "Usage report stays queued",
     "The scheduled usage report sits in the queue for hours and is never mailed.",
     "A stuck report worker was restarted; queued reports were delivered afterwards."),
]
EXTRA_DOCS = [
    ("https://docs.example.org/kb/mcp-authentication", "MCP connector authentication", "mcp-gateway",
     "The MCP connector authenticates with a client token; after a token rotation restart the connector so it reloads the signing key."),
]


def _events(day: str) -> dict[tuple[str, str], list[tuple[str, int, int]]]:
    """(tenant, service) -> events of ``day`` (rule id, minute of day, count)."""
    last = 24 * 60
    out: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    # alderbank-stg crawls every 30 minutes; on the provisional day it stops after 06:00 UTC
    stop = 360 if day == TODAY else last
    out[("alderbank-stg", "crawler")] = [("success.crawl_completed", m, 1) for m in range(0, stop, 30)]
    # brightmoor crawls hourly; a fetch-timeout episode at 03:30-03:50 UTC on the provisional day,
    # then the hourly crawl continues (recovered)
    crawl = [("success.crawl_completed", m, 1) for m in range(5, last, 60)]
    if day == TODAY:
        crawl += [("crawler.fetch_timeout", m, 4) for m in (210, 220, 230)]
    out[("brightmoor", "crawler")] = crawl
    # brightmoor MCP: unauthorized episodes on 11-08 and today
    if day in ("2026-11-08", TODAY):
        out[("brightmoor", "mcp-gateway")] = [("auth.unauthorized", m, 6) for m in (300, 302, 304)]
    # brightmoor login failures on 11-07
    if day == "2026-11-07":
        out[("brightmoor", "auth-service")] = [("auth.unauthorized", m, 3) for m in (600, 601)]
    # alderbank-stg reports queue today from 07:00 UTC (no delivery since)
    reports = [("success.report_delivered", m, 1) for m in range(30, 7 * 60 if day == TODAY else last, 120)]
    if day == TODAY:
        reports += [("report.queue_stalled", m, 2) for m in (420, 450, 480)]
    out[("alderbank-stg", "report-worker")] = reports
    # cobalt (a contested alias in the F0 registry) has slow searches on 11-08
    if day == "2026-11-08":
        out[("cobalt", "search-api")] = [("search.slow_query", m, 5) for m in (500, 510)]
        out[("alderbank-stg", "search-api")] = [("search.slow_query", m, 2) for m in (520,)]
    return out


def hb_snapshot(day: str, tenant: str, service: str, events: list[tuple[str, int, int]], *, until_minute: int | None = None) -> dict[str, Any]:
    from datetime import timedelta

    from tests.log_policy_support import day_start

    start = day_start(day)
    window = None
    if until_minute is not None:
        events = [e for e in events if e[1] < until_minute]
        window = (start.isoformat(), (start + timedelta(minutes=until_minute)).isoformat())
    snap = snapshot(day, tenant, service, events, phrases=PHRASES, rules=RULES, window=window)
    for row in snap["episodes"]:
        if row["rule_id"] in SUCCESS_IDS:
            row["evidence"] = []
            row["evidence_status"] = "not_collected_success_heartbeat"
    snap["evidence_documents"] = sum(len(r["evidence"]) for r in snap["episodes"])
    if until_minute is not None:
        snap["observed_at"] = (start + timedelta(minutes=until_minute)).isoformat()
    snap["heartbeats"] = heartbeat_buckets(snap["summary"]["series"], SUCCESS_IDS)
    snap["heartbeat_bucket_seconds"] = 300
    return snap


def day_snapshots(day: str, until_minute: int | None = None) -> list[dict[str, Any]]:
    return [hb_snapshot(day, tenant, service, events, until_minute=until_minute) for (tenant, service), events in sorted(_events(day).items())]


def support_documents() -> list[Document]:
    fx = load(F0)
    docs = [d for key, member in fx.members.items() if key.startswith("support/") for d in member]
    # the log schema owns ``status`` (an enum of episode states): the ticket status is renamed
    out = [d.model_copy(update={"dataset": DATASET, "metadata": {("ticket_status" if k == "status" else k): v for k, v in d.metadata.items()}}) for d in docs]
    for rid, account, service, title, description, resolution in EXTRA_TICKETS:
        out.append(Document(id=rid, dataset=DATASET, source="ticket", kind="ticket", title=title, fields={"description": description, "resolution": resolution},
                            metadata={"source": "ticket", "account": account, "service": service, "product": "Platform", "created_at": "2026-10-20T09:00:00Z", "ticket_status": "solved", "priority": "normal"},
                            url=f"https://support.example.org/tickets/{rid.split(':')[1]}"))
    for rid, title, service, body in EXTRA_DOCS:
        out.append(Document(id=rid, dataset=DATASET, source="doc", kind="doc", title=title, fields={"body": body},
                            metadata={"source": "doc", "service": service, "product": "Platform", "updated_at": "2026-10-01T00:00:00Z"}, url=rid))
    return out


SUPPORT_FIELDS = {"source": "string", "account": "string", "product": "string", "created_at": "datetime", "resolved_at": "datetime", "ticket_status": "string", "priority": "string", "updated_at": "datetime"}


def _schema(base: dict[str, Any], samples: list[Document]) -> None:
    fields = base["metadata_schema"]["fields"]
    existing = {f["name"]: f for f in fields}
    kinds = existing.get("kind", {})
    if kinds.get("enum_values") is not None:
        kinds["enum_values"] = sorted(set(kinds["enum_values"]) | {d.kind for d in samples} | {"ticket", "doc"})
    for doc in samples:
        for name, value in {**doc.metadata, "archive_text_trimmed": True}.items():
            if name in existing:
                continue
            kind = "datetime" if name in {"window_start", "window_end", "observed_at"} else "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "string"
            existing[name] = {"name": name, "type": kind, "nullable": True, "filterable": True, "groupable": True, "sortable": True}
            fields.append(existing[name])
    for name, kind in SUPPORT_FIELDS.items():
        if name not in existing:
            existing[name] = {"name": name, "type": kind, "nullable": True, "filterable": True, "groupable": True, "sortable": True}
            fields.append(existing[name])
    for f in fields:
        if f["name"] in {"category", "tenant", "service", "signature_id"} and f.get("filterable"):
            f["listable"] = True


def write_links(tmp: Path) -> tuple[Path, str]:
    """Explicit tier-A links (accepted, shown) and one tier-B candidate (labelled)."""
    store = LinkStore.create(tmp / "links.sqlite", now=lambda: NOW)
    links = [
        Link(pair="ticket_doc", tier="A", builder="explicit_url", subject_id="ticket:1001", object_id="https://docs.example.org/kb/crawler-timeouts", evidence={"field": "resolution"}),
        Link(pair="ticket_doc", tier="B", builder="title_mention", subject_id="ticket:2001", object_id="https://docs.example.org/kb/mcp-authentication", evidence={"mention": "MCP connector"}),
        # a link to a record that is not in the served collection: never shown (links grant no access)
        Link(pair="ticket_doc", tier="A", builder="explicit_url", subject_id="ticket:1001", object_id="https://docs.example.org/kb/retired-page", evidence={"field": "description"}),
    ]
    ids = {link.subject_id for link in links} | {link.object_id for link in links}
    store.build(links, known_ids=ids, registry_version=None)
    return store.path, sha256_file(store.path)


PLANNER_ROLE = {"provider": "openai_compatible", "model": "fake-planner", "base_url": "http://127.0.0.1:9/v1"}
GENERATOR_ROLE = {"provider": "openai_compatible", "model": "fake-generator", "base_url": "http://127.0.0.1:9/v1"}
LLM_DECISION = {"kind": "llm", "schema_repair_attempts": 1}


def unified_config(tmp: Path, *, planner: dict[str, Any] | None = None, identity_sha: str | None = None, links: bool = True, schema_version: int = 3, llm: bool = False) -> Any:
    files = [
        {"path": str(tmp / "policy" / "ops.yaml"), "sha256": write_family(tmp / "policy" / "ops.yaml", FAILURES)},
        {"path": str(tmp / "policy" / "success.yaml"), "sha256": write_family(tmp / "policy" / "success.yaml", SUCCESSES)},
    ]
    write_library(tmp / "patterns")
    data = gold_config(tmp)
    data["dataset"] = DATASET
    logs = policy_logs(tmp, files[0]["sha256"], provisional={"enabled": True})
    logs["collection"]["keywords"] = {"files": files}
    data["logs"].update(logs)
    data["models"]["roles"]["generator"] = dict(GENERATOR_ROLE)
    if llm:
        data["models"]["roles"]["planner"] = dict(PLANNER_ROLE)
        data["pipeline"]["decision"] = dict(LLM_DECISION)
    data["question"] = {"timezone": DISPLAY_TIMEZONE, "vocabulary": {"terms": VOCABULARY}}
    base = validate_config(data).model_dump(mode="json")
    base["tools"]["enabled"] = ["incident_episodes", "issue_history", "log_liveness", "log_top", "aggregate", "get_document"]
    sample = summary_documents(DATASET, day_snapshots(CLOSED[0]), RULES, citations=True)
    _schema(base, sample)
    if schema_version == 3:
        base["schema_version"] = 3
        registry = tmp / "ids.sqlite"
        if not registry.exists():
            import_registry(registry, load(F0).registry, now=lambda: NOW)
        cross: dict[str, Any] = {"identity": {"registry_path": str(registry), "sha256": identity_sha or sha256_file(registry)}}
        if links:
            path, sha = write_links(tmp) if not (tmp / "links.sqlite").exists() else (tmp / "links.sqlite", sha256_file(tmp / "links.sqlite"))
            cross["links"] = {"review": {"role": "generator", "approval_ref": "synthetic-approval"}, "store_path": str(path), "sha256": sha}
        cross["planner"] = {"enabled": True, "knowledge_sources": ["ticket", "doc"], **(planner or {})}
        base["crosssource"] = cross
    return validate_config(base)


def support_config(tmp: Path, *, planner: bool) -> Any:
    """Today's support deployment shape (tickets and docs, llm planner, no log member) as a schema
    v3 configuration; ``planner``: the two-halves switch (staged cutover step 1, before the log
    member joins)."""
    data: dict[str, Any] = {
        "schema_version": 3,
        "dataset": DATASET,
        "backends": {"local": {"kind": "sqlite", "path": str(tmp / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 16}, "planner": dict(PLANNER_ROLE), "generator": dict(GENERATOR_ROLE)}},
        "metadata_schema": {"fields": [
            {"name": "kind", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "source", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "account", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "service", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "product", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "ticket_status", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "priority", "type": "string", "filterable": True, "groupable": True, "listable": True},
            {"name": "created_at", "type": "datetime", "filterable": True, "sortable": True, "nullable": True},
            {"name": "resolved_at", "type": "datetime", "filterable": True, "sortable": True, "nullable": True},
            {"name": "updated_at", "type": "datetime", "filterable": True, "sortable": True, "nullable": True},
        ]},
        "pipeline": {"decision": dict(LLM_DECISION), "arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}, "structured": {"backend": "local"}}},
    }
    if planner:
        data["crosssource"] = {"planner": {"enabled": True, "knowledge_sources": ["ticket", "doc"]}}
    return validate_config(data)


def build(tmp: Path, config: Any) -> Engine:
    """Close the archive days, publish the generation (archive window + support documents) and
    refresh the provisional day; the engine's clock must be pinned to ``NOW`` by the caller."""
    engine = Engine(config)
    archive = EpisodeArchive(config, now=lambda: utc("2026-11-09T02:00:00"))
    for day in CLOSED:
        archive.close_day(day, day_snapshots(day))
    support = support_documents()
    original = archive.generation_documents
    archive.generation_documents = lambda today: [*original(today), *support]  # type: ignore[method-assign]
    archive.publish(engine.write_path, date(2026, 11, 9))
    stage(tmp / "staging", TODAY, day_snapshots(TODAY, until_minute=630))
    EpisodeArchive(config, now=lambda: NOW, store=archive.store).refresh_provisional()
    return engine


def citing_generator(seen: list[dict[str, Any]] | None = None):
    """A fake generator (and planner) for ``JsonModel.call``: the generator cites the first record
    and the first structured evidence; a planner call is refused (tests install their own)."""
    from su_multi_arm.models import Answer

    def respond(self, system, payload, schema, budget, stage):
        if seen is not None:
            seen.append({"stage": stage, "payload": payload})
        if stage != "generator":
            raise AssertionError(f"unexpected model stage {stage}")
        budget.reserve()
        evidence = payload["evidence"]
        records = [e["id"] for e in evidence if e.get("source") != "structured"][:1]
        structured = [e["id"] for e in evidence if e.get("source") == "structured"][:1]
        cited = [*records, *structured]
        if not cited:
            return Answer(answer="No evidence.", insufficient_evidence=True)
        text = " ".join(f"See [{i}]." for i in cited)
        return Answer(answer=text, citations=cited)

    return respond
