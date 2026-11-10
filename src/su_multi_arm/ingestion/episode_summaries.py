"""Source-side rule summaries. Counts represent logs; evidence is a bounded subset.

One search supplies mutually exclusive rule counters and minute timelines. No raw
scroll fallback exists. Sessions are observed failure windows, not proven outages.

**Success heartbeats (O6, 2026-11-09).** A rule with ``category: success`` records a success
event (a crawl, index or job completed, a report delivered, a scheduler tick, an auth success).
Success rules come after every failure rule (``check_precedence``): the rule filters are exclusive
in precedence order, so a line that matches a failure rule stays a failure and every failure
rule's filter is the one it was before. Success rules carry no evidence. Their episodes are
*runs* (successes separated by more than the quiet gap) and are built like failure episodes, but
they never join failure counters: the coverage row counts them apart (``success_documents``,
``success_episodes``), their incidents carry ``episode_status: success_heartbeat`` and no
observed-rule community, and no shared-error community is built from them. A snapshot may carry
the collector's 5-minute ``heartbeats`` block (``rule_id``, ``bucket_start_ms``, ``count``,
``first``, ``last``); it must reconcile exactly with the minute series it was derived from, or the
snapshot is refused. Recipes without success rules produce exactly the documents they did before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from pydantic import Field, model_validator

from ..core.config_sources import LogFieldMap
from ..models import StrictModel

CONTRACT = "incident_episode_summaries_v1"
UNKNOWN = "unclassified"
SUCCESS = "success"  # the category of success-heartbeat rules
SUCCESS_STATUS = "success_heartbeat"  # ``episode_status`` of a success run


class SummaryRule(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.]{2,63}$")
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=48)
    alternatives: list[list[str]] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def literals(self):
        if self.id == UNKNOWN:
            raise ValueError("reserved rule id")
        for conjunction in self.alternatives:
            if not 1 <= len(conjunction) <= 5 or any(not p.strip() or len(p) > 160 for p in conjunction):
                raise ValueError("invalid rule phrases")
            if sum(len(p) for p in conjunction) < 8:
                raise ValueError("incident rules need a specific literal")
        return self


def is_success(rule: SummaryRule) -> bool:
    return rule.category == SUCCESS


def check_precedence(rules: list[SummaryRule]) -> None:
    """Success rules come after every failure rule (failure precedence), or ``ValueError``."""
    seen_success = None
    for rule in rules:
        if is_success(rule):
            seen_success = seen_success or rule.id
        elif seen_success is not None:
            raise ValueError(f"failure rule {rule.id} follows success rule {seen_success}: success rules must come after every failure rule (failure precedence)")


def success_phrases(rules: list[SummaryRule]) -> set[str]:
    """Phrases used only by success rules (their keyword counters are success counters)."""
    failure = {p for r in rules if not is_success(r) for terms in r.alternatives for p in terms}
    return {p for r in rules if is_success(r) for terms in r.alternatives for p in terms} - failure


def heartbeat_buckets(series: list[dict], success_ids: set[str], bucket_seconds: int = 300) -> list[dict]:
    """Success heartbeats per rule and ``bucket_seconds`` bucket: count, first and last timestamp
    (the collector's ``heartbeats`` block, recomputed from the minute series)."""
    if not 60 <= bucket_seconds <= 3600 or bucket_seconds % 60:
        raise ValueError("heartbeat buckets are whole minutes between 1 and 60")
    width = bucket_seconds * 1000
    cells: dict[tuple[str, int], dict] = {}
    for c in series:
        if c["rule_id"] not in success_ids:
            continue
        key = (c["rule_id"], c["minute"] - c["minute"] % width)
        b = cells.setdefault(key, {"rule_id": key[0], "bucket_start_ms": key[1], "count": 0, "first": c["first"], "last": c["last"]})
        b["count"] += c["count"]
        b["first"], b["last"] = min(b["first"], c["first"]), max(b["last"], c["last"])
    return [cells[k] for k in sorted(cells)]


def check_heartbeats(snap: dict, rules: list[SummaryRule]) -> None:
    """A snapshot's ``heartbeats`` block (optional) names success rules only and equals the
    buckets of its minute series (``ValueError`` otherwise: never trusted unreconciled)."""
    if "heartbeats" not in snap:
        return
    success_ids = {r.id for r in rules if is_success(r)}
    blocks = snap["heartbeats"]
    if not isinstance(blocks, list):
        raise ValueError("heartbeats must be a list of buckets")
    if blocks and not success_ids:
        raise ValueError("snapshot carries success heartbeats but the keyword recipe has no success rule")
    if any(not isinstance(b, dict) or b.get("rule_id") not in success_ids for b in blocks):
        raise ValueError("heartbeat bucket of a rule that is not a success rule")
    seconds = snap.get("heartbeat_bucket_seconds", 300)
    if type(seconds) is not int:
        raise ValueError("heartbeat_bucket_seconds must be an integer")

    def canonical(rows: list[dict]) -> list[tuple]:
        return sorted((b["rule_id"], b["bucket_start_ms"], b["count"], timestamp(b["first"]), timestamp(b["last"])) for b in rows)

    try:
        given = canonical(blocks)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid heartbeat bucket: {exc}") from exc
    if given != canonical(heartbeat_buckets(snap["summary"]["series"], success_ids, seconds)):
        raise ValueError("heartbeat buckets do not reconcile with the minute series")


def timestamp(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("explicit timezone required")
    return t.astimezone(timezone.utc)


def identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def phrase(text: str, field: str = "message") -> dict:
    return {"match_phrase": {field: {"query": text, "slop": 0}}}


def any_of(queries: list[dict]) -> dict:
    return {"bool": {"should": queries, "minimum_should_match": 1}}


def compile_summary(phrases: list[str], rules: list[SummaryRule], start: str, end: str, *, field_map: LogFieldMap | None = None) -> dict:
    a, b = timestamp(start), timestamp(end)
    if not timedelta(0) < b - a <= timedelta(days=1):
        raise ValueError("summary window must be positive and at most one day")
    if not phrases or len(phrases) != len(set(phrases)) or len(phrases) > 512:
        raise ValueError("invalid master phrase list")
    if not rules or len(rules) > 32 or len({r.id for r in rules}) != len(rules):
        raise ValueError("invalid rule catalog")
    check_precedence(rules)
    fields = field_map or LogFieldMap()
    message_field, time_field = fields.message, fields.timestamp
    base = any_of([phrase(p, message_field) for p in phrases])
    raw = [any_of([{"bool": {"filter": [phrase(p, message_field) for p in terms]}} for terms in r.alternatives]) for r in rules]
    filters = {r.id: {"bool": {"filter": [q], **({"must_not": [any_of(raw[:i])]} if i else {})}}
               for i, (r, q) in enumerate(zip(rules, raw))}
    unknown = {"bool": {"must_not": [any_of(raw)]}}
    bounded = {"range": {time_field: {"gte": a.isoformat(), "lt": b.isoformat()}}}
    request = {"size": 0, "_source": False, "track_total_hits": True,
               "query": {"bool": {"filter": [base, bounded]}},
               "aggs": {
                   "source_window": {"global": {}, "aggs": {"bounded": {"filter": bounded}}},
                   "unclassified": {"filter": unknown},
                   "keywords": {"filters": {"filters": {str(i): phrase(p, message_field) for i, p in enumerate(phrases)}}},
                   "rules": {"filters": {"filters": filters}, "aggs": {
                       "minutes": {"date_histogram": {"field": time_field, "fixed_interval": "1m", "min_doc_count": 1},
                                   "aggs": {"first": {"min": {"field": time_field}}, "last": {"max": {"field": time_field}}}}}}}}
    recipe = {"contract": CONTRACT, "start": a.isoformat(), "end": b.isoformat(),
              "phrases": phrases, "rules": [r.model_dump() for r in rules], "request": request}
    return {"request": request, "filters": filters, "unknown_filter": unknown,
            "query": request["query"], "source_fields": {"message": message_field, "timestamp": time_field}, "recipe": identity(recipe), "start": a.isoformat(), "end": b.isoformat()}


def complete_response(response: dict) -> None:
    shards = response.get("_shards", {})
    if (response.get("timed_out") is not False or response.get("terminated_early", False)
            or type(shards.get("total")) is not int or shards["total"] < 1
            or shards.get("failed") != 0 or shards.get("successful") != shards["total"]):
        raise ValueError("incomplete source aggregation")


def nonnegative(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid source counter")
    return value


def validate_summary(response: dict, compiled: dict, rules: list[SummaryRule], phrases: list[str]) -> dict:
    complete_response(response)
    total = response.get("hits", {}).get("total", {})
    if total.get("relation") != "eq":
        raise ValueError("keyword population is not exact")
    selected = nonnegative(total.get("value"))
    aggs = response.get("aggregations", {})
    population = nonnegative(aggs.get("source_window", {}).get("bounded", {}).get("doc_count"))
    unknown = nonnegative(aggs.get("unclassified", {}).get("doc_count"))
    buckets = aggs.get("rules", {}).get("buckets", {})
    keyword_buckets = aggs.get("keywords", {}).get("buckets", {})
    if set(buckets) != {r.id for r in rules} or set(keyword_buckets) != {str(i) for i in range(len(phrases))}:
        raise ValueError("missing rule/keyword counters")
    start, end = timestamp(compiled["start"]), timestamp(compiled["end"])
    series = []
    known = 0
    for rule in rules:
        bucket = buckets[rule.id]
        count = nonnegative(bucket.get("doc_count"))
        known += count
        seen = set()
        subtotal = 0
        for minute in bucket.get("minutes", {}).get("buckets", []):
            key = minute.get("key")
            if type(key) is not int or key % 60000 or key in seen:
                raise ValueError("invalid or duplicate minute")
            seen.add(key)
            n = nonnegative(minute.get("doc_count"))
            first, last = minute.get("first", {}).get("value"), minute.get("last", {}).get("value")
            if (not n or not isinstance(first, (float, int)) or not isinstance(last, (float, int))
                    or isinstance(first, bool) or isinstance(last, bool) or not key <= first <= last < key + 60000):
                raise ValueError("invalid minute interval")
            first_dt, last_dt = (datetime.fromtimestamp(v / 1000, timezone.utc) for v in (first, last))
            if not start <= first_dt <= last_dt < end:
                raise ValueError("minute outside query window")
            series.append({"rule_id": rule.id, "minute": key, "count": n,
                           "first": first_dt.isoformat(), "last": last_dt.isoformat()})
            subtotal += n
        if subtotal != count:
            raise ValueError("minute counts do not reconcile")
    if known + unknown != selected or selected > population:
        raise ValueError("exclusive rule counts do not reconcile")
    keywords = {p: nonnegative(keyword_buckets[str(i)].get("doc_count")) for i, p in enumerate(phrases)}
    if any(n > selected for n in keywords.values()) or sum(keywords.values()) < selected:
        raise ValueError("keyword counters do not reconcile")
    return {"contract": CONTRACT, "recipe": compiled["recipe"], "start": compiled["start"], "end": compiled["end"],
            "source_documents": population, "keyword_documents": selected, "rule_documents": known,
            "unclassified_documents": unknown, "keyword_counts_nonexclusive": keywords, "series": series,
            "count_basis": "exclusive_rule_matches_in_one_source_response", "all_source_complete": False}


def episodes(summary: dict, scope: dict, rules: list[SummaryRule], *, gap_seconds: int = 300) -> list[dict]:
    if not 60 <= gap_seconds <= 3600:
        raise ValueError("invalid episode gap")
    catalog = {r.id: r for r in rules}
    rows = []
    current = None
    for cell in sorted(summary["series"], key=lambda c: (c["rule_id"], c["first"])):
        if cell["rule_id"] not in catalog:
            raise ValueError("unknown rule in timeline")
        if (current is None or current["rule_id"] != cell["rule_id"]
                or (timestamp(cell["first"]) - timestamp(current["last_failure"])).total_seconds() > gap_seconds):
            rule = catalog[cell["rule_id"]]
            current = {**scope, "rule_id": rule.id, "issue": rule.title, "category": rule.category,
                       "first_failure": cell["first"], "last_failure": cell["last"], "occurrences": 0,
                       "recovered_at": None, "resolution_seconds": None, "recovery_basis": "not_established",
                       "evidence": [], "status": "observed_signal", "count_basis": summary["count_basis"],
                       "grouping_basis": "tenant_environment_service_rule_and_quiet_gap"}
            current["id"] = "episode:" + identity([scope, rule.id, cell["first"]])[:32]
            rows.append(current)
        current["last_failure"] = cell["last"]
        current["occurrences"] += cell["count"]
    for row in rows:
        row["observed_window_seconds"] = (timestamp(row["last_failure"]) - timestamp(row["first_failure"])).total_seconds()
        row["quiet_at_window_end"] = (timestamp(summary["end"]) - timestamp(row["last_failure"])).total_seconds() >= gap_seconds
        # Silence establishes neither recovery nor an outage duration.
    if sum(row["occurrences"] for row in rows) != summary["rule_documents"]:
        raise ValueError("episode counts do not reconcile")
    return rows


def evidence_request(compiled: dict, episode: dict, *, includes: list[str]) -> dict:
    first, last = timestamp(episode["first_failure"]), timestamp(episode["last_failure"])
    time_field = compiled.get("source_fields", {}).get("timestamp", "@timestamp")
    query = {"bool": {"filter": [compiled["query"], compiled["filters"][episode["rule_id"]],
                                  {"range": {time_field: {"gte": first.isoformat(), "lte": last.isoformat()}}}]}}
    orders = (("first", "asc"), ("last", "desc")) if episode["occurrences"] > 1 else (("first", "asc"),)
    return {"size": 0, "_source": False, "track_total_hits": True, "query": query,
            "aggs": {order: {"top_hits": {"size": 1, "sort": [{time_field: direction}],
                                             "_source": {"includes": includes}}}
                     for order, direction in orders}}


class EpisodeGroups:
    """In-memory episode buffer of ``SummaryBuilder``: episodes per (tenant, env, service, rule)."""

    def __init__(self) -> None:
        self._groups: dict[tuple, list[dict]] = {}

    def add(self, key: tuple, episode: dict) -> None:
        self._groups.setdefault(key, []).append(episode)

    def groups(self):
        for key, values in sorted(self._groups.items()):
            yield key, sorted(values, key=lambda v: v["first_failure"])


class SummaryBuilder:
    """``summary_documents`` one snapshot at a time (bounded-memory day close, plan K §4.1).

    ``add(snapshot)`` returns the snapshot's per-scope documents; ``finish()`` yields the
    cross-scope ones (merged incidents and their ``signature_day`` links, the rule catalog, the
    shared-rule communities). Only the episodes (in ``episodes``, an in-memory or spilled buffer)
    and the small shared-rule member counters are kept between snapshots. The documents are exactly
    those of ``summary_documents`` over the same snapshots (which is this builder over a list)."""

    def __init__(self, dataset: str, rules: list[SummaryRule], *, citations: bool = False, episodes: Any = None):
        self.dataset, self.citations = dataset, citations
        check_precedence(rules)
        self.rules = list(rules)
        self.rule_titles = {r.id: r.title for r in rules}
        # success heartbeats (O6): counted apart; empty for a failure-only recipe (documents unchanged)
        self.success_ids = {r.id for r in rules if is_success(r)}
        self.success_only_phrases = success_phrases(rules)
        self.episodes = episodes if episodes is not None else EpisodeGroups()
        self.seen_scopes: set[tuple[str, str]] = set()
        self.shared: dict[tuple[str, str], dict] = {}
        self.count_basis: str | None = None

    def add(self, snap: dict) -> list:
        from .rollups import _document

        dataset, docs = self.dataset, []
        scope, summary = snap["scope"], snap["summary"]
        date = timestamp(summary["start"]).date().isoformat()
        scope_key = (scope["index"], date)
        if scope_key in self.seen_scopes:
            raise ValueError("duplicate index/day summary snapshot")
        self.seen_scopes.add(scope_key)
        check_heartbeats(snap, self.rules)
        self.count_basis = summary["count_basis"]
        base = {k: scope[k] for k in ("tenant", "env", "service")}
        counts = {k: summary[k] for k in ("source_documents", "keyword_documents", "rule_documents", "unclassified_documents")}
        counts.update(evidence_documents=snap.get("evidence_documents", 0),
                      sample_completion_documents=snap.get("sample_completion_documents", 0),
                      source_requests=snap.get("source_requests", 0), response_bytes=snap.get("response_bytes", 0),
                      episodes=len(snap["episodes"]), recovery_confirmed=0)
        keyword_counts = summary["keyword_counts_nonexclusive"]
        coverage_fields = {}
        if self.success_ids:
            # the coverage counters describe the failure side; success heartbeats are counted apart
            success_documents = sum(c["count"] for c in summary["series"] if c["rule_id"] in self.success_ids)
            success_episodes = sum(1 for e in snap["episodes"] if e["rule_id"] in self.success_ids)
            counts.update(keyword_documents=counts["keyword_documents"] - success_documents,
                          rule_documents=counts["rule_documents"] - success_documents,
                          episodes=counts["episodes"] - success_episodes,
                          success_documents=success_documents, success_episodes=success_episodes)
            coverage_fields["success_keyword_counts"] = json.dumps(sorted((p, n) for p, n in keyword_counts.items() if p in self.success_only_phrases))
            keyword_counts = {p: n for p, n in keyword_counts.items() if p not in self.success_only_phrases}
        meta = {**base, **counts, "date": date, "collection_scope": CONTRACT,
                "window_start": summary["start"], "window_end": summary["end"],
                "observed_at": snap["observed_at"], "count_basis": summary["count_basis"]}
        docs.append(_document(dataset, "summary_coverage", [scope["index"], date], "Collection summary", meta,
                              {"summary": "Keyword-filtered source aggregation; counts include unclassified candidates.",
                               "keyword_counts": json.dumps(sorted(keyword_counts.items())), **coverage_fields}))
        docs.append(_document(dataset, "unit_day", [scope["index"], date], "Tenant and service source scope",
                              {**base, "date": date, "lines_total": summary["source_documents"], "collection_scope": CONTRACT}))
        hour_counts: dict[tuple[str, str], int] = {}
        for cell in summary["series"]:
            hour = timestamp(cell["first"]).replace(minute=0, second=0, microsecond=0).isoformat()
            hour_key = (cell["rule_id"], hour)
            hour_counts[hour_key] = hour_counts.get(hour_key, 0) + cell["count"]
            if cell["rule_id"] in self.success_ids:
                continue  # success heartbeats never form a shared-error community
            shared_key = (cell["rule_id"], timestamp(cell["first"]).date().isoformat())
            group = self.shared.setdefault(shared_key, {"title": self.rule_titles[cell["rule_id"]], "members": {}})
            member = (scope["tenant"], scope["env"], scope["service"])
            group["members"][member] = group["members"].get(member, 0) + cell["count"]
            bounds = group.setdefault("windows", {}).setdefault(member, {"first": cell["first"], "last": cell["last"]})
            bounds["first"] = min(bounds["first"], cell["first"])
            bounds["last"] = max(bounds["last"], cell["last"])
        # Minute series stay in the source snapshot for episode boundaries. The
        # query store needs exact hourly counters, not one document per minute.
        for (rule_id, hour), count in sorted(hour_counts.items()):
            cell_meta = {**base, "date": timestamp(hour).date().isoformat(), "ts_hour": hour,
                         "signature_id": rule_id, "occurrences": count, "collection_scope": CONTRACT,
                         **({"category": SUCCESS} if rule_id in self.success_ids else {})}
            docs.append(_document(dataset, "episode_cell", [scope["index"], rule_id, hour], "Rule match counter", cell_meta))
        for episode in snap["episodes"]:
            key = tuple(scope[k] for k in ("tenant", "env", "service")) + (episode["rule_id"],)
            self.episodes.add(key, episode)
        for i, sample in enumerate(snap.get("unclassified_examples", [])):
            docs.append(_document(dataset, "unclassified_example", [scope["index"], date, str(i)], "Unclassified candidate", {**base, "date": date, "collection_scope": CONTRACT, "evidence_truncated": bool(sample.get("evidence_truncated"))},
                                  {"exemplars": sample["message"], "summary": "Keyword candidate; an incident has not been established."}))
        return docs

    def finish(self):
        from .rollups import _document

        dataset, templates = self.dataset, {}
        # Preserve a continuous episode across UTC date boundaries. A tenant/service/rule
        # grouping is a triage signal, not proof of one physical failure or one affected job.
        for key, values in self.episodes.groups():
            merged: list[dict] = []
            for value in values:
                if merged and (timestamp(value["first_failure"]) - timestamp(merged[-1]["last_failure"])).total_seconds() <= 300:
                    current = merged[-1]
                    current["last_failure"] = max(current["last_failure"], value["last_failure"])
                    current["occurrences"] += value["occurrences"]
                    current["evidence"].extend(value["evidence"])
                    current.setdefault("recovery_samples", []).extend(value.get("recovery_samples", []))
                else:
                    merged.append({**value, "evidence": list(value["evidence"]), "recovery_samples": list(value.get("recovery_samples", []))})
            for value in merged:
                tenant, env, service, rule_id = key
                uid = identity([key, value["first_failure"]])[:32]
                if rule_id in self.success_ids:
                    yield from self._success_run(key, uid, value)
                    templates[rule_id] = value["issue"]
                    continue
                text = (f"{value['issue']}. Tenant {tenant}; service {service}; "
                        f"{value['occurrences']} rule-matching log messages between {value['first_failure']} and {value['last_failure']}. "
                        "Recovery and outage duration have not been established.")
                meta = {"tenant": tenant, "env": env, "service": service, "services": service,
                        "signature_id": rule_id, "signatures": rule_id, "category": value["category"],
                        "date": timestamp(value["first_failure"]).date().isoformat(),
                        "first_hour": value["first_failure"], "last_hour": value["last_failure"],
                        "occurrences": value["occurrences"], "lines": value["occurrences"],
                        "observed_window_seconds": (timestamp(value["last_failure"]) - timestamp(value["first_failure"])).total_seconds(),
                        "community_id": "observed_rule:" + identity([dataset, tenant, env, rule_id, timestamp(value["first_failure"]).date().isoformat()]),
                        "community_label": f"{value['issue']} for client {tenant}",
                        "episode_status": "observed_signal", "count_basis": self.count_basis,
                        "collection_scope": CONTRACT, "recovery_basis": "not_established", "issue": value["issue"]}
                unique = {item["id"]: item for item in value["evidence"]}
                evidence = sorted(unique.values(), key=lambda e: e.get("ts", ""))
                if len(evidence) > 3:
                    evidence = [evidence[0], evidence[len(evidence)//2], evidence[-1]]
                meta.update(evidence_count=len(evidence), evidence_truncated=any(e.get("evidence_truncated") for e in evidence))
                completions = list({e["id"]: e for e in value.get("recovery_samples", [])}.values())[:1]
                meta["completion_evidence_count"] = len(completions)
                if completions:
                    meta["recovery_basis"] = "sampled_operation_only"
                # Bounded examples remain explicitly examples; their count is never scaled
                # to claim every source log was parsed.
                incident_fields = {"summary": text, "exemplars": "\n\n".join(e["message"] for e in evidence),
                                   "sample_completion": "\n\n".join(e["message"] for e in completions)}
                if self.citations:
                    incident_fields["evidence_refs"] = "\n".join(f"{e['id']} {e.get('ts', '')}".strip() for e in [*evidence, *completions])
                yield _document(dataset, "incident", [uid], value["issue"], meta, incident_fields)
                templates[rule_id] = value["issue"]
                link_meta = {**{k: v for k, v in meta.items() if not k.startswith("community_")}, "date_basis": "event_time", "date_timezone": "UTC", "level": "rule"}
                yield _document(dataset, "signature_day", [uid], value["issue"], link_meta,
                                {"summary": text})  # ontology links, not parser coverage
        for rule_id, title in sorted(templates.items()):
            yield _document(dataset, "signature", [rule_id], title,
                            {"signature_id": rule_id, "pattern_id": rule_id, "collection_scope": CONTRACT,
                             **({"category": SUCCESS} if rule_id in self.success_ids else {})},
                            {"template": title})
        for (rule_id, day), group in sorted(self.shared.items()):
            tenants = {member[0] for member in group["members"]}
            if len(tenants) >= 2:
                for (tenant, env, service), count in sorted(group["members"].items()):
                    yield _document(dataset, "shared_error", [rule_id, day, tenant, env, service], group["title"],
                                    {"community_id": "shared_rule:" + identity([dataset, rule_id, day]),
                                     "community_family_id": "shared_rule:" + identity([dataset, rule_id]),
                                     "community_label": f"{group['title']} on {day}",
                                     "signature_id": rule_id, "tenants_affected": len(tenants), "tenant": tenant, "env": env,
                                     "service": service, "date": day, "level": "RULE", "row_exactness": "exact",
                                     "first_hour": group["windows"][(tenant, env, service)]["first"],
                                     "last_hour": group["windows"][(tenant, env, service)]["last"],
                                     "error_type": group["title"], "lines": count, "method": "source_rule_query", "collection_scope": CONTRACT},
                                    {"summary": f"{group['title']} matched {count} log messages for client {tenant}, service {service}, on {day}. Shared rule matches are associations, not proof of a common root cause."})


    def _success_run(self, key: tuple, uid: str, value: dict):
        """``incident`` + ``signature_day`` of one success run (no evidence, no community)."""
        from .rollups import _document

        tenant, env, service, rule_id = key
        first, last = value["first_failure"], value["last_failure"]
        text = (f"{value['issue']} (success heartbeat). Tenant {tenant}; service {service}; "
                f"{value['occurrences']} success messages between {first} and {last}.")
        meta = {"tenant": tenant, "env": env, "service": service, "services": service,
                "signature_id": rule_id, "signatures": rule_id, "category": SUCCESS,
                "date": timestamp(first).date().isoformat(), "first_hour": first, "last_hour": last,
                "occurrences": value["occurrences"], "lines": value["occurrences"],
                "observed_window_seconds": (timestamp(last) - timestamp(first)).total_seconds(),
                "episode_status": SUCCESS_STATUS, "count_basis": self.count_basis, "collection_scope": CONTRACT,
                "recovery_basis": "not_applicable", "issue": value["issue"], "evidence_count": 0,
                "evidence_truncated": False, "completion_evidence_count": 0}
        yield _document(self.dataset, "incident", [uid], value["issue"], meta, {"summary": text})
        link_meta = {**meta, "date_basis": "event_time", "date_timezone": "UTC", "level": "rule"}
        yield _document(self.dataset, "signature_day", [uid], value["issue"], link_meta, {"summary": text})


def summary_documents(dataset: str, snapshots: list[dict], rules: list[SummaryRule], *, citations: bool = False) -> list:
    """Canonical evidence and typed episode rows, never fabricated per-log artifacts.

    ``citations``: each ``incident`` also carries ``evidence_refs`` (one ``<source id> <timestamp>``
    line per retained example), so an archived row whose text is trimmed still cites its evidence."""
    builder = SummaryBuilder(dataset, rules, citations=citations)
    docs = []
    for snap in snapshots:
        docs.extend(builder.add(snap))
    docs.extend(builder.finish())
    return docs
