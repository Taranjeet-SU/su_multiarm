"""Bounded, snapshot-consistent grouped log export with exact raw fallback.

Transport, privacy projection and output storage are injected. This module contains no
source endpoints or credentials. Pagination keys and unmasked source data stay in memory;
only the injected sanitizer's output reaches ``emit``. A failed capture must never seal.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable

CONTRACT = "elk_grouped_v1"
KEY_FIELDS = ["index", "hour_start", "message", "level"]
SOURCE_FIELDS = ["@timestamp", "message", "level", "logType", "code", "method", "api", "app", "time", "stack",
                 "container.name", "host.name", "log.offset", "fields.env"]


class CaptureError(RuntimeError):
    """Content-free failure: no partial result is eligible for activation."""


def check_response(response: dict) -> None:
    shards = response.get("_shards")
    if response.get("timed_out") or response.get("terminated_early"):
        raise CaptureError("source response was incomplete")
    if not isinstance(shards, dict) or shards.get("failed") != 0:
        raise CaptureError("source shard completion was not established")
    if shards.get("successful", 0) != shards.get("total"):
        raise CaptureError("source did not complete every shard")


def exact_total(response: dict) -> int:
    check_response(response)
    total = response.get("hits", {}).get("total")
    if not isinstance(total, dict) or total.get("relation") != "eq" or type(total.get("value")) is not int or total["value"] < 0:
        raise CaptureError("source total is not exact")
    return total["value"]


def keyword_field(props: dict, field: str) -> str | None:
    node = props
    for part in field.split("."):
        node = node.get(part, {})
        if part != field.split(".")[-1]:
            node = node.get("properties", {})
    if node.get("type") == "keyword" and not node.get("normalizer"):
        return field
    kw = node.get("fields", {}).get("keyword", {})
    if kw.get("type") == "keyword" and not kw.get("normalizer"):
        return field + ".keyword"
    return None


def iso_hour(milliseconds: int) -> str:
    if type(milliseconds) is not int or milliseconds % 3_600_000:
        raise CaptureError("invalid UTC hour bucket")
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def source_guard(message_field: str, level_field: str | None, *, env: str, selector_fields: dict | None = None) -> dict:
    """All bucket documents must pass, not merely the representative hit.

    Source scalar checks are necessary: single doc-value entries do not distinguish
    scalar fields from arrays containing repeated values. This guard deliberately
    rejects extra selected semantic/provenance fields in v1. Environment is the
    validated index scope supplied to the projection, not unretained source.fields.env;
    that unretained field is checked for scalar type only.
    """
    return {"lang": "painless", "params": {"message_field": message_field, "level_field": level_field, "env": env, "selector_fields": selector_fields or {}, "lf": "\n", "cr": "\r", "tab": "\t"},
            "source": """
try {
  def s = params['_source'];
  if (!(s instanceof Map)) { return 1; }
  def m = s['message'];
  if (!(m instanceof String) || m.length()==0 || m.indexOf(params.lf)>=0 || m.indexOf(params.cr)>=0 || m.startsWith(' ') || m.startsWith(params.tab)) { return 1; }
  if (doc[params.message_field].size()!=1 || !m.equals(doc[params.message_field].value)) { return 1; }
  def ts = s['@timestamp'];
  if (!(ts instanceof String) || doc['@timestamp'].size()!=1 || ZonedDateTime.parse(ts).getNano()%1000000!=0 || ZonedDateTime.parse(ts).toInstant().toEpochMilli()!=doc['@timestamp'].value.toInstant().toEpochMilli()) { return 1; }
  def lvl = s['level'];
  if (lvl!=null) {
    if (!(lvl instanceof String) || params.level_field==null || doc[params.level_field].size()!=1 || !lvl.equals(doc[params.level_field].value)) { return 1; }
  } else if (params.level_field!=null && doc[params.level_field].size()!=0) { return 1; }
  for (def k : ['logType','code','method','api','app','time','stack']) { if (s.containsKey(k) && s[k]!=null) { return 1; } }
  for (def k : ['host','container']) {
    def v=s[k]; if(v!=null && !(v instanceof Map)) { return 1; }
    def value=v==null ? null : v['name']; def field=params.selector_fields[k];
    if(value!=null) {
      if(!(value instanceof String) || field==null || doc[field].size()!=1 || !value.equals(doc[field].value)) { return 1; }
    } else if(field!=null && doc[field].size()!=0) { return 1; }
  }
  def log=s['log'];
  if(log!=null) {
    if(!(log instanceof Map)) { return 1; }
    def offset=log['offset'];
    if(offset!=null && (!(offset instanceof Integer || offset instanceof Long) || offset<0)) { return 1; }
  }
  def f=s['fields']; if (f!=null && (!(f instanceof Map) || (f['env']!=null && !(f['env'] instanceof String)))) { return 1; }
  return 0;
} catch (Exception e) { return 1; }
"""}


class SeenKeys:
    """Disk-backed hashes bound Python memory; no source keys/cursors are persisted."""
    def __init__(self, db, namespace):
        self.db, self.namespace = db, namespace

    def __contains__(self, value):
        digest = hashlib.sha256(value.encode()).hexdigest()
        return self.db.execute("SELECT 1 FROM seen WHERE namespace=? AND digest=?", (self.namespace, digest)).fetchone() is not None

    def add(self, value):
        digest = hashlib.sha256(value.encode()).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (self.namespace, digest))


class PITSnapshot:
    """Only a genuine source PIT permits multi-request exact counts.

    The injected transport must restrict search/close to PITs it opened. Unsupported
    PITs fail closed; count equality is not a substitute for snapshot consistency.
    """
    def __init__(self, es: Any, index: str):
        self.es = es
        self.pid = es.open_pit(index, "2m")
        self.requests = 0

    def search(self, body: dict, *, note: str) -> dict:
        response = self.es.request("POST", "/_search", {**body, "pit": {"id": self.pid, "keep_alive": "2m"}}, note=note)
        self.pid = response.get("pit_id", self.pid)
        self.requests += 1
        check_response(response)
        return response

    def close(self) -> None:
        self.es.close_pit(self.pid)

    def metadata(self) -> dict:
        return {"protocol": "pit", "snapshot_consistent": True, "requests": self.requests}


def capture_index(es: Any, info: dict, props: dict, emit: Callable[[dict], None],
                  sanitize: Callable[[dict], dict], *, page_size: int = 100,
                  strata_queries: dict | None = None, snapshot_factory: Callable = PITSnapshot, work_dir: Path | None = None, min_first_page_reduction: float = 1.2) -> dict:
    """Capture all documents as proven homogeneous groups or exact raw occurrences.

    ``sanitize`` receives a hit and must return a detached, masked record with a stable
    ``id``. ``emit`` writes only masked records. Source counts, privacy provenance and
    recipe/checksum sealing are the caller's responsibility; this return value records
    reconciled count units explicitly. A caller must discard or retain-as-unsealed any
    emitted partial data if this function raises. Failed indexes restart from a new PIT;
    no raw composite/search cursor is ever persisted.
    """
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("page_size must be 1..1000")
    if not isinstance(min_first_page_reduction, (int, float)) or not 1 <= min_first_page_reduction <= 100:
        raise ValueError("min_first_page_reduction must be 1..100")
    for name in ("index", "date", "tenant", "env", "service"):
        if not isinstance(info.get(name), str) or not info[name]:
            raise ValueError("complete index scope is required")
    message_field = keyword_field(props, "message")
    level_field = keyword_field(props, "level")
    selector_fields = {key: field for key in ("host", "container") if (field := keyword_field(props, key + ".name"))}
    key_fields = KEY_FIELDS + list(selector_fields)
    groupable_mapping = bool(message_field and props.get("@timestamp", {}).get("type") == "date")
    if work_dir is None:
        raise ValueError("an external work_dir is required for bounded capture state")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.TemporaryDirectory(prefix="grouped-capture-", dir=work_dir)
    db = sqlite3.connect(str(Path(scratch.name) / "seen.sqlite3"))
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA cache_size=-2048")
    db.execute("CREATE TABLE seen (namespace TEXT, digest TEXT, PRIMARY KEY(namespace,digest)) WITHOUT ROWID")
    try:
        snapshot = snapshot_factory(es, info["index"])
    except BaseException:
        db.close()
        scratch.cleanup()
        raise
    seen_raw = SeenKeys(db, "raw")
    seen_group_reps = SeenKeys(db, "group-reps")
    counters = {"source_total": 0, "grouped_occurrences": 0, "raw_occurrences": 0,
                "groups": 0, "downloaded_examples": 0, "downloaded_documents": 0,
                "raw_hits_transferred": 0, "raw_duplicate_hits": 0, "unsafe_buckets": 0,
                "compression_gate_raw": False}
    base = {"track_total_hits": True, "timeout": "10s"}

    def emit_raw(query: dict, expected: int) -> None:
        after = None
        bucket_id = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
        seen_after = SeenKeys(db, "raw-cursors:" + bucket_id)
        seen_hits = SeenKeys(db, "raw-hits:" + bucket_id)
        count = 0
        while True:
            body = {**base, "size": page_size, "sort": ["_shard_doc"], "_source": {"includes": SOURCE_FIELDS}, "query": query}
            if after is not None:
                body["search_after"] = after
            response = snapshot.search(body, note="grouped_raw_fallback")
            if exact_total(response) != expected:
                raise CaptureError("raw fallback count differs from bucket count")
            hits = response["hits"].get("hits")
            if not isinstance(hits, list):
                raise CaptureError("source omitted raw hits")
            if not hits:
                break
            for hit in hits:
                if hit.get("_index") != info["index"] or not isinstance(hit.get("_id"), str):
                    raise CaptureError("raw hit source identity differs")
                identity = hit["_id"]
                if identity in seen_hits:
                    raise CaptureError("raw fallback repeated a document within one bucket")
                seen_hits.add(identity)
                count += 1
                counters["raw_hits_transferred"] += 1
                if identity in seen_group_reps:
                    raise CaptureError("raw and grouped evidence overlap")
                if identity in seen_raw:
                    counters["raw_duplicate_hits"] += 1
                    continue
                seen_raw.add(identity)
                emit({"contract": CONTRACT, "kind": "raw", "record": sanitize(hit)})
                counters["raw_occurrences"] += 1
            if count > expected:
                raise CaptureError("raw fallback exceeds bucket count")
            after = hits[-1].get("sort")
            if not isinstance(after, list) or not after:
                raise CaptureError("source omitted raw pagination cursor")
            cursor = json.dumps(after, sort_keys=True)
            if cursor in seen_after:
                raise CaptureError("raw pagination cursor repeated")
            seen_after.add(cursor)
        if count != expected:
            raise CaptureError("raw fallback ended before exact count")

    try:
        initial = {**base, "size": 0, "_source": False, "query": {"match_all": {}}}
        if strata_queries is not None:
            initial["aggs"] = {"strata": {"filters": {"filters": strata_queries}}}
        response = snapshot.search(initial, note="grouped_snapshot_count")
        total = exact_total(response)
        counters["source_total"] = total
        es_counts = {"total": total}
        if strata_queries is not None:
            buckets = response.get("aggregations", {}).get("strata", {}).get("buckets", {})
            es_counts.update({k: v.get("doc_count") for k, v in buckets.items()})
            if set(buckets) != set(strata_queries) or any(type(es_counts[k]) is not int or es_counts[k] < 0 for k in buckets) or sum(es_counts[k] for k in buckets) != total:
                raise CaptureError("source strata do not reconcile")
        if not groupable_mapping:
            emit_raw({"match_all": {}}, total)
        else:
            sources = [{"message": {"terms": {"field": message_field, "missing_bucket": True}}},
                       {"hour": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h", "time_zone": "UTC", "missing_bucket": True}}}]
            if level_field:
                sources.append({"level": {"terms": {"field": level_field, "missing_bucket": True}}})
            for key, field in selector_fields.items():
                sources.append({key: {"terms": {"field": field, "missing_bucket": True}}})
            after = None
            seen_after = SeenKeys(db, "composite-cursors")
            seen_keys = SeenKeys(db, "composite-keys")
            while True:
                composite = {"size": page_size, "sources": sources}
                if after is not None:
                    composite["after"] = after
                response = snapshot.search({**base, "size": 0, "_source": False, "query": {"match_all": {}}, "aggs": {
                    "groups": {"composite": composite, "aggs": {
                        "unsafe": {"sum": {"script": source_guard(message_field, level_field, env=info["env"], selector_fields=selector_fields) }},
                        "first_seen": {"min": {"field": "@timestamp"}},
                        "last_seen": {"max": {"field": "@timestamp"}},
                        "example": {"top_hits": {"size": 1, "sort": [{"@timestamp": {"order": "asc", "missing": "_last"}}], "_source": {"includes": SOURCE_FIELDS}}}
                    }}}}, note="grouped_composite_page")
                if exact_total(response) != total:
                    raise CaptureError("snapshot source count changed")
                groups = response.get("aggregations", {}).get("groups", {})
                buckets = groups.get("buckets")
                if not isinstance(buckets, list):
                    raise CaptureError("source omitted composite buckets")
                if not buckets:
                    if groups.get("after_key") is not None:
                        raise CaptureError("empty composite page has a continuation")
                    break
                if after is None and min_first_page_reduction > 1:
                    # A bounded first-page heuristic may choose a lossless raw scan.
                    # This is not a whole-index compression estimate. It runs before emit.
                    represented = 0
                    transferred = 0
                    unsafe_count = 0
                    for bucket in buckets:
                        n = bucket.get("doc_count")
                        unsafe = bucket.get("unsafe", {}).get("value")
                        if type(n) is not int or n < 1 or not isinstance(unsafe, (int, float)) or isinstance(unsafe, bool) or not 0 <= unsafe <= n or unsafe != int(unsafe):
                            raise CaptureError("invalid first-page bucket or guard count")
                        represented += n
                        unsafe_count += bool(unsafe)
                        transferred += n if unsafe else 1
                    if represented / transferred < min_first_page_reduction or unsafe_count > len(buckets) / 2:
                        counters["compression_gate_raw"] = True
                        emit_raw({"match_all": {}}, total)
                        break
                for bucket in buckets:
                    key = bucket.get("key")
                    if not isinstance(key, dict) or set(key) != {next(iter(s)) for s in sources}:
                        raise CaptureError("invalid composite key fields")
                    # Only hashes, never unmasked key text, retained beyond the current page.
                    key_hash = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
                    if key_hash in seen_keys:
                        raise CaptureError("composite bucket repeated")
                    seen_keys.add(key_hash)
                    n = bucket.get("doc_count")
                    if type(n) is not int or n < 1:
                        raise CaptureError("invalid composite bucket count")
                    unsafe = bucket.get("unsafe", {}).get("value")
                    if not isinstance(unsafe, (int, float)) or isinstance(unsafe, bool) or not 0 <= unsafe <= n or unsafe != int(unsafe):
                        raise CaptureError("source guard did not return a complete count")
                    if unsafe or key["message"] is None or key["hour"] is None:
                        counters["unsafe_buckets"] += 1
                        filters = []
                        for dimension, field in (("message", message_field), ("level", level_field), *selector_fields.items()):
                            if field:
                                value = key.get(dimension)
                                filters.append({"bool": {"must_not": [{"exists": {"field": field}}]}} if value is None else {"term": {field: value}})
                        hour = key["hour"]
                        filters.append({"bool": {"must_not": [{"exists": {"field": "@timestamp"}}]}} if hour is None else {"range": {"@timestamp": {"gte": hour, "lt": hour + 3_600_000, "format": "epoch_millis"}}})
                        emit_raw({"bool": {"filter": filters}}, n)
                        continue
                    hour_start = iso_hour(key["hour"])
                    first, last = bucket.get("first_seen", {}).get("value"), bucket.get("last_seen", {}).get("value")
                    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)) or not key["hour"] <= first <= last < key["hour"] + 3_600_000:
                        raise CaptureError("group timestamp bounds escape its hour")
                    hits = bucket.get("example", {}).get("hits", {}).get("hits", [])
                    if len(hits) != 1 or hits[0].get("_index") != info["index"] or not isinstance(hits[0].get("_id"), str):
                        raise CaptureError("group representative identity missing")
                    hit = hits[0]
                    if hit["_id"] in seen_raw or hit["_id"] in seen_group_reps:
                        raise CaptureError("representative identity overlaps another output")
                    seen_group_reps.add(hit["_id"])
                    src = hit.get("_source", {})
                    if src.get("message") != key["message"] or src.get("level") != key.get("level"):
                        raise CaptureError("representative differs from group key")
                    for dimension in selector_fields:
                        if (src.get(dimension) or {}).get("name") != key[dimension]:
                            raise CaptureError("representative selector differs from group key")
                    record = sanitize(hit)
                    group_id = hashlib.sha256((info["index"] + ":" + key_hash).encode()).hexdigest()
                    emit({"contract": CONTRACT, "kind": "group", "group_id": group_id,
                          **{k: info[k] for k in ("index", "date", "tenant", "env", "service")},
                          "hour_start": hour_start,
                          "first_seen": datetime.fromtimestamp(first / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                          "last_seen": datetime.fromtimestamp(last / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                          "occurrence_count": n, "key_fields": key_fields,
                          "exemplar_only_fields": ["offset"] if record.get("offset") is not None else [], "examples": [record]})
                    counters["grouped_occurrences"] += n
                    counters["groups"] += 1
                    counters["downloaded_examples"] += 1
                after = groups.get("after_key")
                if after is None:
                    break
                cursor = hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()
                if cursor in seen_after:
                    raise CaptureError("composite pagination cursor repeated")
                seen_after.add(cursor)
        if counters["grouped_occurrences"] + counters["raw_occurrences"] != total:
            raise CaptureError("group and raw occurrences do not reconcile with source")
        counters["downloaded_documents"] = counters["raw_occurrences"] + counters["downloaded_examples"]
        snapshot_metadata = snapshot.metadata()
        if snapshot_metadata.get("snapshot_consistent") is not True:
            raise CaptureError("snapshot consistency was not established")
        return {"contract": CONTRACT, **counters, "es_counts": es_counts, "snapshot": snapshot_metadata,
                "snapshot_consistent": True, "grouped_capture": True, "capped": False}
    finally:
        try:
            snapshot.close()
        finally:
            db.close()
            scratch.cleanup()


class _SingleResponseSnapshot:
    """Replay one complete source response through the shared group validator.

    No replay operation contacts ELK. Raw fallback is permitted only for buckets whose
    complete hit set was included in that same response; no later count query is used.
    """
    def __init__(self, response, raw_buckets):
        from copy import deepcopy
        self.response = deepcopy(response)
        self.response['aggregations']['groups'].pop('after_key', None)
        self.raw_buckets = iter(raw_buckets)
        self.raw = None

    def search(self, body, *, note):
        if note in {'grouped_snapshot_count', 'grouped_composite_page'}:
            return self.response
        if note != 'grouped_raw_fallback':
            raise CaptureError('unexpected single-response operation')
        if self.raw is None:
            self.raw = next(self.raw_buckets)
            hits = [{**hit, 'sort': [i]} for i, hit in enumerate(self.raw)]
        else:
            hits = []
        result = {'_shards': self.response['_shards'], 'timed_out': False,
                  'hits': {'total': {'value': len(self.raw), 'relation': 'eq'}, 'hits': hits}}
        if not hits:
            self.raw = None
        return result

    def close(self):
        pass

    def metadata(self):
        return {'protocol': 'single_search', 'snapshot_consistent': True, 'requests': 1}


def _compatible_query(props, info, page_size, strata_queries):
    message = keyword_field(props, 'message')
    if not message or props.get('@timestamp', {}).get('type') != 'date':
        return None
    level = keyword_field(props, 'level')
    selectors = {k: f for k in ('host', 'container') if (f := keyword_field(props, k + '.name'))}
    sources = [{'message': {'terms': {'field': message, 'missing_bucket': True}}},
               {'hour': {'date_histogram': {'field': '@timestamp', 'fixed_interval': '1h', 'time_zone': 'UTC', 'missing_bucket': True}}}]
    for name, field in [('level', level), *selectors.items()]:
        if field:
            sources.append({name: {'terms': {'field': field, 'missing_bucket': True}}})
    aggs = {'groups': {'composite': {'size': page_size, 'sources': sources}, 'aggs': {
        'unsafe': {'sum': {'script': source_guard(message, level, env=info['env'], selector_fields=selectors)}},
        'first_seen': {'min': {'field': '@timestamp'}}, 'last_seen': {'max': {'field': '@timestamp'}},
        'example': {'top_hits': {'size': 1, 'sort': [{'@timestamp': {'order': 'asc', 'missing': '_last'}}],
                                 '_source': {'includes': SOURCE_FIELDS}}}}}}
    if strata_queries is not None:
        aggs['strata'] = {'filters': {'filters': strata_queries}}
    return {'size': 0, '_source': False, 'query': {'match_all': {}}, 'track_total_hits': True,
            'timeout': '10s', 'aggs': aggs}


def _complete_response(response, min_reduction):
    total = exact_total(response)
    buckets = response.get('aggregations', {}).get('groups', {}).get('buckets')
    if not isinstance(buckets, list):
        raise CaptureError('aggregation_buckets_missing')
    raw_buckets, raw_ids, grouped = [], set(), 0
    for bucket in buckets:
        n, key, unsafe = bucket.get('doc_count'), bucket.get('key'), bucket.get('unsafe', {}).get('value')
        if type(n) is not int or n < 1 or not isinstance(key, dict) or not isinstance(unsafe, (int, float)) or isinstance(unsafe, bool) or not 0 <= unsafe <= n or unsafe != int(unsafe):
            raise CaptureError('invalid_aggregation_bucket')
        if unsafe or key.get('message') is None or key.get('hour') is None:
            hits = bucket.get('example', {}).get('hits', {}).get('hits', [])
            if len(hits) != n:
                raise CaptureError('unsafe_bucket_requires_pagination')
            raw_buckets.append(hits)
            for hit in hits:
                identity = hit.get('_id')
                if not isinstance(identity, str):
                    raise CaptureError('raw_identity_missing')
                raw_ids.add(identity)
        else:
            grouped += n
    if grouped + len(raw_ids) != total:
        raise CaptureError('aggregation_requires_pagination')
    physical = len(buckets) - len(raw_buckets) + len(raw_ids)
    if physical and total / physical < min_reduction:
        raise CaptureError('compression_gate_raw')
    return raw_buckets


def capture_index_compatible(es: Any, info: dict, props: dict, emit: Callable[[dict], None],
                             sanitize: Callable[[dict], dict], *, page_size: int = 100,
                             raw_page_size: int = 1000, strata_queries: dict | None = None,
                             work_dir: Path | None = None, min_first_page_reduction: float = 1.2,
                             hybrid_candidates: int = 0, min_staging_free_bytes: int = 0, candidate_is_head: Callable[[str], bool] | None = None,
                             require_request_reduction: bool = False,
                             raw_record_requires_context: Callable[[dict], bool] | None = None) -> dict:
    """Capture complete native groups, optionally with a same-snapshot complement scroll.

    No PIT/version/monitor privilege is required. A response requiring further buckets
    or unsafe-hit pages is discarded before emitting any records. Grouped results are
    not combined with a later snapshot. This may choose raw capture for high-cardinality
    indices; the selected protocol and fallback reason are always reported.
    """
    if type(page_size) is not int or not 1 <= page_size <= 1000 or type(raw_page_size) is not int or not 1 <= raw_page_size <= 1000:
        raise ValueError('page sizes must be 1..1000')
    if not isinstance(min_first_page_reduction, (int, float)) or not 1 <= min_first_page_reduction <= 100:
        raise ValueError('min_first_page_reduction must be 1..100')
    if work_dir is None:
        raise ValueError('an external work_dir is required')
    for name in ('index', 'date', 'tenant', 'env', 'service'):
        if not isinstance(info.get(name), str) or not info[name]:
            raise ValueError('complete index scope is required')
    if type(min_staging_free_bytes) is not int or min_staging_free_bytes < 0:
        raise ValueError('min_staging_free_bytes must be a nonnegative integer')
    if type(hybrid_candidates) is not int or not 0 <= hybrid_candidates <= 64:
        raise ValueError('hybrid_candidates must be 0..64')
    query = _compatible_query(props, info, page_size, strata_queries)
    reason = 'mapping_not_groupable'
    if hybrid_candidates and query is not None:
        try:
            return _capture_hybrid_scroll(es, info, props, emit, sanitize,
                raw_page_size=raw_page_size, strata_queries=strata_queries, work_dir=Path(work_dir),
                candidate_limit=hybrid_candidates, min_reduction=min_first_page_reduction,
                min_staging_free_bytes=min_staging_free_bytes, candidate_is_head=candidate_is_head,
                require_request_reduction=require_request_reduction,
                raw_record_requires_context=raw_record_requires_context)
        except CaptureError as exc:
            reason = str(exc)
        except RuntimeError as exc:
            reason = 'hybrid_unavailable:' + type(exc).__name__
        return _capture_raw_scroll(es, info, emit, sanitize, page_size=raw_page_size,
            strata_queries=strata_queries, work_dir=Path(work_dir), reason=reason)
    if query is not None:
        try:
            response = es.request('POST', '/' + info['index'] + '/_search', query, note='compatible_single_aggregation')
            if candidate_is_head is not None:
                for bucket in response.get('aggregations', {}).get('groups', {}).get('buckets', []):
                    key = bucket.get('key', {}).get('message')
                    if isinstance(key, str) and not candidate_is_head(key):
                        raise CaptureError('group_is_not_standalone_head')
            raw_buckets = _complete_response(response, min_first_page_reduction)
            buffered = []
            result = capture_index(None, info, props, buffered.append, sanitize, page_size=page_size,
                strata_queries=strata_queries, work_dir=work_dir, min_first_page_reduction=1,
                snapshot_factory=lambda *_: _SingleResponseSnapshot(response, raw_buckets))
            if raw_record_requires_context is not None and any(
                row.get('kind') == 'raw' and raw_record_requires_context(row['record']) for row in buffered
            ):
                raise CaptureError('grouped_raw_requires_full_context')
        except CaptureError as exc:
            reason = str(exc)  # CaptureError messages deliberately contain no source text.
        except RuntimeError as exc:
            reason = 'aggregation_unavailable:' + type(exc).__name__
        else:
            for row in buffered:
                emit(row)
            return {**result, 'capture_mode': 'single_response_groups', 'fallback_reason': None}
    return _capture_raw_scroll(es, info, emit, sanitize, page_size=raw_page_size,
                               strata_queries=strata_queries, work_dir=Path(work_dir), reason=reason)


def _capture_raw_scroll(es, info, emit, sanitize, *, page_size, strata_queries, work_dir, reason,
                        initial_response=None, forbidden_ids=()):
    work_dir.mkdir(parents=True, exist_ok=True)
    sid, got, requests, early_pages = None, 0, 0, 0
    with tempfile.TemporaryDirectory(prefix='compatible-scroll-', dir=work_dir) as scratch:
        db = sqlite3.connect(str(Path(scratch) / 'seen.sqlite3'))
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA cache_size=-2048')
        db.execute('CREATE TABLE seen (namespace TEXT, digest TEXT, PRIMARY KEY(namespace,digest)) WITHOUT ROWID')
        seen = SeenKeys(db, 'raw')
        try:
            body = {'size': page_size, 'sort': ['_doc'], 'track_total_hits': True, 'timeout': '50s',
                    '_source': {'includes': SOURCE_FIELDS}, 'query': {'match_all': {}}}
            if strata_queries is not None:
                body['aggs'] = {'strata': {'filters': {'filters': strata_queries}}}
            response = initial_response if initial_response is not None else es.request(
                'POST', '/' + info['index'] + '/_search?scroll=2m', body, note='compatible_raw_snapshot')
            requests += 1
            sid = response.get('_scroll_id')
            total = exact_total(response)
            es_counts = {'total': total}
            if strata_queries is not None:
                buckets = response.get('aggregations', {}).get('strata', {}).get('buckets', {})
                es_counts.update({k: v.get('doc_count') for k, v in buckets.items()})
                if set(buckets) != set(strata_queries) or any(type(es_counts[k]) is not int or es_counts[k] < 0 for k in buckets) or sum(es_counts[k] for k in buckets) != total:
                    raise CaptureError('source strata do not reconcile')
            while True:
                # Some older sources mark size-limited scroll pages terminated_early.
                # The initial snapshot must be complete; later pages are accepted only
                # if every unique document eventually reconciles to that exact total.
                if requests > 1 and response.get('terminated_early'):
                    early_pages += 1
                checked = {**response, 'terminated_early': False} if requests > 1 else response
                if exact_total(checked) != total:
                    raise CaptureError('scroll snapshot count changed')
                hits = response.get('hits', {}).get('hits')
                if not isinstance(hits, list):
                    raise CaptureError('source omitted raw hits')
                for hit in hits:
                    identity = hit.get('_id')
                    if hit.get('_index') != info['index'] or not isinstance(identity, str) or identity in seen or identity in forbidden_ids:
                        raise CaptureError('raw source identity missing or repeated')
                    seen.add(identity)
                    emit({'contract': CONTRACT, 'kind': 'raw', 'record': sanitize(hit)})
                    got += 1
                if got > total:
                    raise CaptureError('scroll exceeds exact snapshot count')
                if got == total:
                    break
                if not hits or not sid:
                    raise CaptureError('scroll ended before exact snapshot count')
                response = es.scroll(sid, '2m')
                sid = response.get('_scroll_id', sid)
                requests += 1
            return {'contract': CONTRACT, 'source_total': total, 'grouped_occurrences': 0, 'raw_occurrences': got,
                    'groups': 0, 'downloaded_examples': 0, 'downloaded_documents': got, 'raw_hits_transferred': got,
                    'raw_duplicate_hits': 0, 'unsafe_buckets': 0, 'compression_gate_raw': reason == 'compression_gate_raw',
                    'es_counts': es_counts, 'snapshot': {'protocol': 'scroll', 'snapshot_consistent': True, 'requests': requests},
                    'snapshot_consistent': True, 'grouped_capture': True, 'capped': False,
                    'capture_mode': 'raw_scroll', 'fallback_reason': reason, 'scroll_terminated_early_pages': early_pages}
        finally:
            try:
                if sid:
                    es.clear_scroll(sid)
            finally:
                db.close()


def _hybrid_terms_groups(props, info):
    """Older ES forbids composite below global; bounded terms must prove no loss."""
    base = _compatible_query(props, info, 1000, None)['aggs']['groups']
    fields = [('message', keyword_field(props, 'message')), ('hour', '@timestamp')]
    fields += [(name, field) for name in ('level', 'host', 'container')
               if (field := keyword_field(props, name if name == 'level' else name + '.name'))]
    script = {'lang': 'painless', 'params': {'fields': [field for _, field in fields]}, 'source': """
String result = '';
for (int i=0; i<params.fields.size(); i++) {
  def values = doc[params.fields[i]];
  if (values.size()==0) { result += '-1:'; continue; }
  String value;
  if (i==1) {
    long millis=values.value.toInstant().toEpochMilli();
    long remainder=millis%3600000L;
    if (remainder<0) { remainder+=3600000L; }
    value=Long.toString(millis-remainder);
  } else { value=values.value.toString(); }
  result += Integer.toString(value.length()) + ':' + value;
}
return result;
"""}
    return {'terms': {'script': script, 'size': 1000, 'shard_size': 1000,
                      'order': {'_key': 'asc'}, 'show_term_doc_count_error': True},
            'aggs': base['aggs']}, [name for name, _ in fields]


def _decode_hybrid_key(encoded, names):
    # Painless String.length counts UTF-16 units, including astral characters.
    if not isinstance(encoded, str):
        raise CaptureError('hybrid_group_key_invalid')
    remaining, result = encoded.encode('utf-16-le'), {}
    for name in names:
        split = remaining.find(b':\x00')
        if split < 0 or split % 2:
            raise CaptureError('hybrid_group_key_invalid')
        try:
            size = int(remaining[:split].decode('utf-16-le'))
        except (ValueError, UnicodeError):
            raise CaptureError('hybrid_group_key_invalid') from None
        remaining = remaining[split + 2:]
        if size == -1:
            result[name] = None
        elif size < 0 or size * 2 > len(remaining):
            raise CaptureError('hybrid_group_key_invalid')
        else:
            try:
                value = remaining[:size * 2].decode('utf-16-le')
                result[name] = int(value) if name == 'hour' else value
            except (ValueError, UnicodeError):
                raise CaptureError('hybrid_group_key_invalid') from None
            remaining = remaining[size * 2:]
    if remaining:
        raise CaptureError('hybrid_group_key_invalid')
    return result


def _capture_hybrid_scroll(es, info, props, emit, sanitize, *, raw_page_size, strata_queries,
                           work_dir, candidate_limit, min_reduction, min_staging_free_bytes=0, candidate_is_head=None, require_request_reduction=False, raw_record_requires_context=None, _selected_keys=None, _retry_budget=1, _attempt=1):
    """Group bounded repeated keys and scroll their exact complement in ONE snapshot.

    A preliminary terms query selects keys only; its approximate counts never enter
    evidence or totals. Global aggregation and complement hits share the initial search
    execution context. All masked output is staged externally until complete proof.
    """
    message = keyword_field(props, 'message')
    coarse = {'bool': {'filter': [{'exists': {'field': message}}, {'exists': {'field': '@timestamp'}}],
                       'must_not': [{'exists': {'field': f}} for f in
                                    ('logType', 'code', 'method', 'api', 'app', 'time', 'stack')]}}
    keys = _selected_keys
    selection_limit = min(64, candidate_limit * 4)
    if keys is None:
        selection = es.request('POST', '/' + info['index'] + '/_search', {
            'size': 0, '_source': False, 'track_total_hits': True, 'timeout': '10s', 'query': coarse,
            'aggs': {'candidates': {'terms': {'field': message, 'size': selection_limit,
                                             'shard_size': max(100, candidate_limit * 10)}}}},
            note='compatible_candidate_selection')
        exact_total(selection)
        buckets = selection.get('aggregations', {}).get('candidates', {}).get('buckets')
        if not isinstance(buckets, list) or len(buckets) > selection_limit:
            raise CaptureError('hybrid_candidate_selection_invalid')
        keys = []
        for bucket in buckets:
            key, count = bucket.get('key'), bucket.get('doc_count')
            if not isinstance(key, str) or type(count) is not int or count < 1:
                raise CaptureError('hybrid_candidate_selection_invalid')
            if len(keys) < candidate_limit and count > 1 and key and '\n' not in key and '\r' not in key and not key.startswith((' ', '\t')) and (candidate_is_head is None or candidate_is_head(key)):
                keys.append(key)
        if not keys:
            raise CaptureError('hybrid_no_repeated_candidates')
    candidate = {'bool': {'filter': [coarse, {'terms': {message: keys}}]}}
    grouped_terms, key_names = _hybrid_terms_groups(props, info)
    # A global aggregation intentionally ignores the complement query. Its filter
    # and that query are logical complements, so every source document has one arm.
    all_aggs = {'eligible': {'filter': candidate, 'aggs': {'groups': grouped_terms}}}
    if strata_queries is not None:
        all_aggs['strata'] = {'filters': {'filters': strata_queries}}
    body = {'size': raw_page_size, 'sort': ['_doc'], 'track_total_hits': True, 'timeout': '50s',
            '_source': {'includes': SOURCE_FIELDS}, 'query': {'bool': {'must_not': [candidate]}},
            'aggs': {'all_source': {'global': {}, 'aggs': all_aggs}}}
    sid = None
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        response = es.request('POST', '/' + info['index'] + '/_search?scroll=2m', body,
                              note='compatible_hybrid_snapshot')
        sid = response.get('_scroll_id')
        raw_total = exact_total(response)
        whole = response.get('aggregations', {}).get('all_source', {})
        eligible = whole.get('eligible', {})
        total, grouped_total = whole.get('doc_count'), eligible.get('doc_count')
        if type(total) is not int or type(grouped_total) is not int or min(total, grouped_total) < 0 or total != grouped_total + raw_total:
            raise CaptureError('hybrid_population_counts_do_not_reconcile')
        groups = eligible.get('groups', {})
        group_buckets = groups.get('buckets')
        if not isinstance(group_buckets, list) or len(group_buckets) > 1000:
            raise CaptureError('hybrid_groups_missing')
        if groups.get('sum_other_doc_count') != 0 or groups.get('doc_count_error_upper_bound') != 0:
            raise CaptureError('hybrid_groups_require_pagination')
        represented, unsafe_keys = 0, set()
        for bucket in group_buckets:
            if bucket.get('doc_count_error_upper_bound') != 0:
                raise CaptureError('hybrid_group_count_inexact')
            bucket['key'] = _decode_hybrid_key(bucket.get('key'), key_names)
            n, key, unsafe = bucket.get('doc_count'), bucket.get('key'), bucket.get('unsafe', {}).get('value')
            if type(n) is not int or n < 1 or not isinstance(key, dict):
                raise CaptureError('hybrid_group_invalid')
            if not isinstance(unsafe, (int, float)) or isinstance(unsafe, bool) or not 0 <= unsafe <= n or unsafe != int(unsafe) or key.get('message') not in keys:
                raise CaptureError('hybrid_candidate_guard_invalid')
            if unsafe or key.get('hour') is None:
                unsafe_keys.add(key['message'])
            represented += n
        if represented != grouped_total:
            raise CaptureError('hybrid_groups_require_pagination')
        if unsafe_keys:
            retained = [key for key in keys if key not in unsafe_keys]
            if _retry_budget and retained:
                if sid:
                    es.clear_scroll(sid)
                    sid = None
                # Keys are selection hints only: discard every count/hit and acquire
                # a fresh complete snapshot whose complement now includes bad keys.
                return _capture_hybrid_scroll(es, info, props, emit, sanitize,
                    raw_page_size=raw_page_size, strata_queries=strata_queries, work_dir=work_dir,
                    candidate_limit=candidate_limit, min_reduction=min_reduction,
                    min_staging_free_bytes=min_staging_free_bytes, candidate_is_head=candidate_is_head,
                    require_request_reduction=require_request_reduction, raw_record_requires_context=raw_record_requires_context, _selected_keys=retained, _retry_budget=_retry_budget - 1, _attempt=_attempt + 1)
            raise CaptureError('hybrid_candidate_source_unsafe')
        physical = len(group_buckets) + raw_total
        if not grouped_total or (physical and total / physical < min_reduction):
            raise CaptureError('hybrid_compression_gate_raw')
        full_pages = max(1, (total + raw_page_size - 1) // raw_page_size)
        complement_pages = max(1, (raw_total + raw_page_size - 1) // raw_page_size)
        # Final clear-scroll and mapping requests cancel between paths. A discarded
        # candidate costs its initial search and clear; selection adds one request.
        estimated_request_saving = full_pages - complement_pages - 1 - 2 * (_attempt - 1)
        if require_request_reduction and estimated_request_saving < 1:
            raise CaptureError('hybrid_no_request_reduction')
        es_counts = {'total': total}
        if strata_queries is not None:
            strata = whole.get('strata', {}).get('buckets', {})
            es_counts.update({k: v.get('doc_count') for k, v in strata.items()})
            if set(strata) != set(strata_queries) or any(type(es_counts[k]) is not int or es_counts[k] < 0 for k in strata) or sum(es_counts[k] for k in strata) != total:
                raise CaptureError('source strata do not reconcile')
        # Reuse the complete typed group validator, with a local replay of the
        # eligible subset. This does not open another source snapshot or request.
        replay = {'_shards': response['_shards'], 'timed_out': False,
                  'hits': {'total': {'value': grouped_total, 'relation': 'eq'}, 'hits': []},
                  'aggregations': {'groups': groups}}
        rows = []
        group_result = capture_index(None, info, props, rows.append, sanitize, page_size=1000,
            work_dir=work_dir, min_first_page_reduction=1,
            snapshot_factory=lambda *_: _SingleResponseSnapshot(replay, []))
        representatives = {b['example']['hits']['hits'][0]['_id'] for b in group_buckets}
        with tempfile.TemporaryFile(mode='w+', encoding='utf8', dir=work_dir) as staged:
            staged_rows = 0
            def stage(row):
                nonlocal staged_rows
                if staged_rows % 1000 == 0 and shutil.disk_usage(work_dir).free < min_staging_free_bytes:
                    raise OSError('masked staging disk reserve unavailable')
                if row.get('kind') == 'raw' and raw_record_requires_context is not None and raw_record_requires_context(row['record']):
                    raise CaptureError('hybrid_raw_requires_full_context')
                staged_rows += 1
                staged.write(json.dumps(row, separators=(',', ':')) + '\n')
            for row in rows:
                stage(row)
            # Scroll helper owns clearing the latest cursor once called.
            sid = None
            raw_result = _capture_raw_scroll(es, info, stage, sanitize, page_size=raw_page_size,
                strata_queries=None, work_dir=work_dir, reason=None, initial_response=response,
                forbidden_ids=representatives)
            if raw_result['raw_occurrences'] + group_result['grouped_occurrences'] != total:
                raise CaptureError('hybrid_final_counts_do_not_reconcile')
            staged.seek(0)
            for line in staged:
                emit(json.loads(line))
        return {**group_result, 'source_total': total, 'raw_occurrences': raw_total,
                'raw_hits_transferred': raw_total, 'downloaded_documents': physical,
                'es_counts': es_counts, 'capture_mode': 'hybrid_groups_scroll', 'fallback_reason': None,
                'candidate_keys': len(keys), 'selection_counts_used_for_evidence': False,
                'discarded_candidate_snapshots': _attempt - 1,
                'estimated_request_saving': estimated_request_saving,
                'scroll_terminated_early_pages': raw_result['scroll_terminated_early_pages'],
                'snapshot': {'protocol': 'single_search_global_with_scroll', 'snapshot_consistent': True,
                             'requests': raw_result['snapshot']['requests'], 'selection_requests': 1,
                             'discarded_candidate_snapshots': _attempt - 1}}
    finally:
        if sid:
            es.clear_scroll(sid)
