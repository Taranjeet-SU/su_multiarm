"""``elasticsearch_rest``: a read-only source for Elasticsearch ≥ 7.9 and OpenSearch ≥ 1.0 over plain
REST (httpx; no client library), behind the allow-list transport (addendum A1 §3.1.1–3.1.3).

* **Construction never connects.** The URL and credentials are read from the ``*_env`` variables
  when the first request is made; they never appear in config values, errors, records or ``repr``.
* **Discovery** resolves each index expression (``{date}`` filled from the partition key with
  ``index_name.date_format``) through ``_resolve/index`` (row 1), or ``/{expr}/_alias`` (row 2) on
  releases older than 7.9 or when ``_resolve`` is not served. Only discovered names may be used as
  ``{idx}`` afterwards. A dated expression gives units without a time slice (the index date selects
  the day); an undated expression gives units sliced to the window (event time).
* **Counts** are built per index from its mapping (``logs.count_query``).
* **Pulls** use ``scroll`` (default) or ``search_after`` with a declared tiebreaker. Every scroll is
  registered and closed in ``finally``; a context leaked by a kill expires after the bounded
  keepalive. A cursor that expires restarts its stratum (at most ``pull.max_restarts`` times,
  de-duplicating by ``source_id``) and the restart count is reported in the batch.
* PIT is probed (open and close on one unit) and reported as a capability; pulls do not use it in
  this slice, because a PIT search needs an index-less ``/_search`` path that is not on the
  allow-list.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import date, datetime, timezone
from typing import Any, ClassVar, Iterator, Mapping
from zoneinfo import ZoneInfo

import httpx

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.config_sources import SourceConfig, duration_seconds
from .contracts import (
    CountQuery,
    PartitionBudget,
    SamplePlan,
    SourceBatch,
    SourceCapabilities,
    SourceCursor,
    SourceDoc,
    SourceUnit,
    SourceWindow,
    UnitCount,
    stable_sha,
)
from .cursor import CursorRegistry
from .errors import (
    SourceAuthError,
    SourceCursorExpired,
    SourceError,
    SourceQueryRejected,
    SourceRequestRefused,
    SourceSchemaDrift,
    SourceUnavailable,
)
from .logs import IndexNameParser, count_query, exact_field, flatten_mapping, lookup, window_filter
from .sampling import hour_bounds, plan as sample_plan, random_score_query
from .transport import IDX, RateLimiter, SourceTransport

DISCOVERED_NAME = re.compile(rf"^{IDX}$")
WRITE_INDEX_PRIVILEGES = ("write", "delete", "index", "manage")
SELF_CHECK_TOLERATED = (400, 403, 404, 405, 500, 501)


def _version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts = []
    for piece in version.split("-")[0].split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


class ElasticsearchRestSource:
    name: ClassVar[str] = "elasticsearch_rest"
    kind: ClassVar[str] = "source"
    config_model: ClassVar[type[SourceConfig]] = SourceConfig

    def __init__(
        self,
        config: SourceConfig,
        deps: Dependencies | None = None,
        *,
        source_name: str = "source",
        env: Mapping[str, str] | None = None,
        http_transport: httpx.BaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
    ):
        self.config = config
        self.source_name = source_name
        self._env = env
        self._http_transport = http_transport
        self._rate_limiter = rate_limiter
        self._transport: SourceTransport | None = None
        self._caps: SourceCapabilities | None = None
        self.parser = IndexNameParser(config.index_name)
        self.mappings: dict[str, dict[str, str]] = {}
        self.units: dict[str, dict[str, Any]] = {}  # unit id -> {"query", "window", "includes"}
        self.diagnostics: list[Diagnostic] = []
        # at most max_concurrency x slices open contexts per process (§3.1.1)
        self.cursors = CursorRegistry(config.pull.max_concurrency * config.pull.slices, self.close)
        self._context_used = 0

    def __repr__(self) -> str:
        return f"ElasticsearchRestSource(name={self.source_name!r})"

    # --- component contract ----------------------------------------------------------------

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        out = []
        needed = [("url_env", self.config.url_env), ("username_env", self.config.username_env), ("password_env", self.config.password_env), ("api_key_env", self.config.api_key_env)]
        for key, name in needed:
            if name and not env.has(name):
                out.append(Diagnostic(severity="error", code="missing_env", message=f"environment variable {name} is not set", path=f"sources.{self.source_name}.{key}", component=self.name))
        if probe and not out:
            self.capabilities(probe=True)
            out.extend(self.self_check())
        return out

    def run(self, ctx: Any, inputs: SourceWindow) -> list[SourceUnit]:
        return self.discover(inputs)

    def recipe_fingerprint(self) -> str | None:
        return None  # a source never affects index identity; the partition manifest records its query sha

    # --- transport -------------------------------------------------------------------------

    @property
    def transport(self) -> SourceTransport:
        if self._transport is None:
            env = os.environ if self._env is None else self._env
            url = env.get(self.config.url_env)
            if not url:
                raise SourceUnavailable(f"environment variable {self.config.url_env} is not set", "connect")
            headers = {"accept": "application/json"}
            if self.config.auth == "basic":
                user, password = env.get(self.config.username_env or ""), env.get(self.config.password_env or "")
                if not user or not password:
                    raise SourceAuthError("basic auth variables are not set", "connect")
                headers["authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
            elif self.config.auth == "api_key":
                key = env.get(self.config.api_key_env or "")
                if not key:
                    raise SourceAuthError("api key variable is not set", "connect")
                headers["authorization"] = f"ApiKey {key}"
            verify: bool | str = str(self.config.ca_bundle) if self.config.ca_bundle else self.config.verify_tls
            self._transport = SourceTransport(
                url,
                headers=headers,
                timeout=self.config.limits.timeout_seconds,
                verify=verify,
                max_response_bytes=self.config.limits.max_response_bytes,
                keepalive_max_seconds=duration_seconds(self.config.pull.keepalive_max),
                rate_limiter=self._rate_limiter or RateLimiter(self.config.limits.requests_per_second),
                http_transport=self._http_transport,
            )
        return self._transport

    def includes(self) -> list[str]:
        """``_source`` includes: the mapped fields; a ``drop_fields`` entry is never requested."""
        dropped = set(self.config.mapping.drop_fields)
        return [f for f in self.config.mapping.requested() if f not in dropped]

    def partition_budget(self) -> PartitionBudget:
        """A fresh ``max_docs_per_partition`` budget, shared by every unit pull of one partition."""
        return PartitionBudget(limit=self.config.pull.max_docs_per_partition)

    def shutdown(self) -> None:
        """Close every open cursor, then the HTTP client."""
        self.cursors.close_all()
        if self._transport is not None:
            self._transport.close()

    # --- capabilities and self-check -------------------------------------------------------

    def capabilities(self, probe: bool = False) -> SourceCapabilities:
        if self._caps is not None and (self._caps.probed or not probe):
            return self._caps
        status, payload = self.transport.request("GET", "/", tolerate=(401, 403))
        notes = []
        if status in (401, 403):
            server, version = "unknown", None
            notes.append(f"GET / returned HTTP {status}; version unknown (tolerated)")
        else:
            info = (payload or {}).get("version") or {}
            version = info.get("number")
            server = "opensearch" if info.get("distribution") == "opensearch" else "elasticsearch"
        v = _version_tuple(version)
        if server == "opensearch":
            resolve, pit = v >= (1, 0), v >= (2, 4)
        elif server == "elasticsearch":
            resolve, pit = v >= (7, 9), v >= (7, 10)
        else:
            resolve, pit = True, False
        caps = SourceCapabilities(server=server, version=version, pit=pit, scroll=True, sliced_scroll=bool(v) or server == "unknown", search_after=True, count=True, resolve_index=resolve, notes=notes)
        if probe:
            caps.has_privileges = self._privileges_response() is not None
            caps.probed = True
        self._caps = caps
        return caps

    def _privilege_patterns(self) -> list[str]:
        out = []
        for expr in self.config.indices:
            for item in expr.replace("{date}", "*").split(","):
                if item not in out:
                    out.append(item)
        return out

    def _privileges_response(self) -> tuple[str, Any] | None:
        server = self._caps.server if self._caps else "unknown"
        if server == "opensearch":
            status, payload = self.transport.request("GET", "/_plugins/_security/api/account", tolerate=SELF_CHECK_TOLERATED)
            return ("account", payload) if 200 <= status < 300 else None
        body = {"cluster": ["all"], "index": [{"names": self._privilege_patterns(), "privileges": list(WRITE_INDEX_PRIVILEGES)}]}
        status, payload = self.transport.request("POST", "/_security/user/_has_privileges", body=body, tolerate=SELF_CHECK_TOLERATED)
        return ("has_privileges", payload) if 200 <= status < 300 and isinstance(payload, dict) else None

    def self_check(self) -> list[Diagnostic]:
        """Read-only role check (§3.1.1). A granted write privilege is a ``warning``, or an ``error``
        under ``require_read_only_role``. When the endpoint is not available the result is
        ``unknown`` (warning) and the allow-list stays the guarantee."""
        path = f"sources.{self.source_name}"
        if self._caps is None:
            self.capabilities()
        response = self._privileges_response()
        if response is None:
            return [Diagnostic(severity="warning", code="source_privileges_unknown", message="the privilege endpoint is not available; read-only role unknown, the transport allow-list remains the guarantee", path=path, component=self.name)]
        kind, payload = response
        if kind == "account":
            roles = payload.get("roles") if isinstance(payload, dict) else None
            return [Diagnostic(severity="warning", code="source_privileges_unknown", message=f"OpenSearch account endpoint lists {len(roles or [])} role(s) but not privileges; read-only role unknown", path=path, component=self.name)]
        granted = []
        if (payload.get("cluster") or {}).get("all"):
            granted.append("cluster:all")
        for pattern, privileges in (payload.get("index") or {}).items():
            for privilege, allowed in (privileges or {}).items():
                if allowed and privilege in WRITE_INDEX_PRIVILEGES:
                    granted.append(f"{pattern}:{privilege}")
        if not granted:
            return [Diagnostic(severity="info", code="source_read_only", message="no write, delete, index, manage or cluster:all privilege is granted", path=path, component=self.name)]
        severity = "error" if self.config.require_read_only_role else "warning"
        return [Diagnostic(severity=severity, code="source_write_privilege", message=f"the source account holds write privileges: {sorted(granted)}", path=f"{path}.require_read_only_role", component=self.name)]

    # --- discovery -------------------------------------------------------------------------

    def window_date(self, window: SourceWindow) -> date:
        try:
            return date.fromisoformat(window.partition_key)
        except ValueError:
            return window.start.astimezone(ZoneInfo(self.config.timezone)).date()

    def resolve(self, expression: str) -> list[str]:
        caps = self.capabilities()
        names: list[str] = []
        if caps.resolve_index:
            try:
                _, payload = self.transport.request("GET", f"/_resolve/index/{expression}", params={"expand_wildcards": "open"})
                names = [entry.get("name") for entry in (payload or {}).get("indices", [])]
            except SourceQueryRejected:
                caps.notes.append("_resolve/index rejected; used the /{expr}/_alias discovery fallback")
                caps.resolve_index = False
        if not caps.resolve_index:
            _, payload = self.transport.request("GET", f"/{expression}/_alias", params={"expand_wildcards": "open"})
            names = list((payload or {}).keys())
        accepted = []
        for name in names:
            if isinstance(name, str) and DISCOVERED_NAME.fullmatch(name):
                accepted.append(name)
            else:
                self.diagnostics.append(Diagnostic(severity="warning", code="source_index_skipped", message=f"discovered name {name!r} is not a plain index name (hidden, data stream or invalid); skipped", path=f"sources.{self.source_name}.indices", component=self.name))
        return sorted(set(accepted))

    def mapping(self, index: str) -> dict[str, str]:
        if index not in self.mappings:
            _, payload = self.transport.request("GET", f"/{index}/_mapping")
            self.mappings[index] = flatten_mapping(payload, index)
        return self.mappings[index]

    def discover(self, window: SourceWindow, *, index_dates: list[date] | None = None) -> list[SourceUnit]:
        """Units of the window. With ``index_dates`` (the event-time partition clock, addendum A1
        §3.8.2) a dated expression is resolved for **each** given index date and every unit is
        sliced to the window by event time, so a reporting day in another timezone than the index
        dates reads exactly its own hours from the (up to two) index days it overlaps."""
        stamps = [d.strftime(self.config.index_name.date_format) for d in index_dates] if index_dates else [self.window_date(window).strftime(self.config.index_name.date_format)]
        units: list[SourceUnit] = []
        seen: set[str] = set()
        for expression, stamp in [(e, s) for e in self.config.indices for s in (stamps if "{date}" in e else stamps[:1])]:
            dated = "{date}" in expression and not index_dates
            names = self.resolve(expression.replace("{date}", stamp))
            self.transport.register_indices(names)
            for index in names:
                if index in seen:
                    continue
                seen.add(index)
                parsed = self.parser.parse(index)
                if not parsed.ok:
                    self.diagnostics.append(Diagnostic(severity="warning", code="source_index_name_unparsed", message=parsed.problem or "unparsed", path=f"sources.{self.source_name}.index_name", component=self.name))
                fields = self.mapping(index)
                slice_ = None if dated else (window.start, window.end)
                query = self.unit_query(index, fields, slice_)
                includes = self.includes()
                query_sha = stable_sha({"index": index, "query": query, "includes": includes, "cursor": self.config.pull.cursor})
                unit_id = stable_sha([index, [s.isoformat() for s in slice_] if slice_ else None, query_sha])[:32]
                self.units[unit_id] = {"query": query, "window": (window.start, window.end), "index": index}
                units.append(SourceUnit(id=unit_id, index=index, slice=slice_, attributes=parsed.attributes, query_sha=query_sha))
        return units

    def unit_query(self, index: str, fields: dict[str, str], slice_: tuple[datetime, datetime] | None) -> dict[str, Any]:
        """The records-tier query of a unit: the slice (if any) and the ``records_query`` count query."""
        filters: list[dict[str, Any]] = []
        if fields.get(self.config.mapping.timestamp) is None:
            raise SourceSchemaDrift(f"index {index!r} has no {self.config.mapping.timestamp!r} field", "discover")
        if slice_ is not None:
            filters.append(window_filter(self.config.mapping, fields, *slice_))
        name = self.config.pull.records_query
        if name is not None:
            filters.append(count_query(name, self.config.counts[name], self.config.mapping, fields)[0])
        return {"bool": {"filter": filters}} if filters else {"match_all": {}}

    def _state(self, unit: SourceUnit) -> dict[str, Any]:
        state = self.units.get(unit.id)
        if state is None:
            raise SourceRequestRefused("POST", f"/{unit.index}/_search", "unit was not produced by discover() in this process")
        return state

    # --- counts ----------------------------------------------------------------------------

    def _count(self, index: str, query: dict[str, Any]) -> int:
        _, payload = self.transport.request("POST", f"/{index}/_count", body={"query": query})
        count = (payload or {}).get("count")
        if not isinstance(count, int):
            raise SourceUnavailable("count response has no integer count", f"POST /{index}/_count")
        return count

    def count(self, unit: SourceUnit, queries: dict[str, CountQuery] | None = None) -> UnitCount:
        queries = self.config.counts if queries is None else queries
        fields = self.mapping(unit.index)
        base: list[dict[str, Any]] = []
        if unit.slice is not None:
            base.append(window_filter(self.config.mapping, fields, *unit.slice))
        total = self._count(unit.index, {"bool": {"filter": base}} if base else {"match_all": {}})
        by_query, basis = {}, {}
        for name, spec in queries.items():
            query, kind = count_query(name, spec, self.config.mapping, fields)
            by_query[name] = self._count(unit.index, {"bool": {"filter": [*base, query]}})
            basis[name] = kind
        return UnitCount(unit_id=unit.id, query_sha=unit.query_sha, total=total, by_query=by_query, basis=basis, at=datetime.now(timezone.utc))

    def plan_sample(self, unit: SourceUnit) -> SamplePlan:
        """Count the unit's records tier and, when over ``max_docs_per_unit``, build the stratified
        plan (one ``_count`` per hour of the window plus one for the rest of the unit)."""
        state = self._state(unit)
        pull = self.config.pull
        total = self._count(unit.index, state["query"])
        if total <= pull.max_docs_per_unit or pull.sample_method == "random_score":
            return sample_plan(pull.sample_method, cap=pull.max_docs_per_unit, seed=pull.seed, unit_total=total)
        start, end = unit.slice or state["window"]
        fields = self.mapping(unit.index)
        hourly = []
        for key, lower, upper in hour_bounds(start, end):
            query = {"bool": {"filter": [state["query"], window_filter(self.config.mapping, fields, lower, upper)]}}
            hourly.append((key, lower, upper, self._count(unit.index, query)))
        inside = sum(c for *_, c in hourly)
        if unit.slice is None:
            outside = {"bool": {"filter": [state["query"]], "must_not": [window_filter(self.config.mapping, fields, start, end)]}}
            hourly.append(("outside", None, None, self._count(unit.index, outside)))
            inside += hourly[-1][3]
        if inside != total:
            self.diagnostics.append(Diagnostic(severity="warning", code="source_strata_drift", message=f"unit {unit.id}: strata sum {inside} != unit count {total} (the index changed between counts)", path=f"sources.{self.source_name}.pull", component=self.name))
        return sample_plan("hourly_quota", cap=pull.max_docs_per_unit, seed=pull.seed, unit_total=total, hourly=hourly)

    # --- pulls -----------------------------------------------------------------------------

    def _sort(self, ordered_by_score: bool) -> list[Any]:
        tiebreak = self.config.pull.tiebreaker
        sort: list[Any] = [{"_score": "desc"}] if ordered_by_score else [{self.config.mapping.timestamp: "asc"}]
        if tiebreak:
            sort.append({tiebreak: "asc"})
        elif not ordered_by_score:
            sort.append("_doc")
        return sort

    def pull(
        self,
        unit: SourceUnit,
        cursor: SourceCursor | None = None,
        page_size: int | None = None,
        sample: SamplePlan | None = None,
        budget: PartitionBudget | None = None,
    ) -> Iterator[SourceBatch]:
        state = self._state(unit)
        page_size = page_size or self.config.pull.page_size
        sample = sample or self.plan_sample(unit)
        mode = self.config.pull.cursor
        if cursor is not None and cursor.mode == "scroll" and not cursor.closed:
            self.close(cursor)  # a scroll cannot resume across runs; the unit restarts from its start
        resume = cursor.state.get("search_after") if cursor is not None and cursor.mode == "search_after" else None
        seen: set[str] = set()
        fields = self.mapping(unit.index)
        strata = [s for s in sample.strata if s.quota > 0]
        if not strata:
            yield SourceBatch(unit_id=unit.id, cursor=SourceCursor(unit_id=unit.id, mode=mode, closed=True), done=True)
            return
        for position, stratum in enumerate(strata):
            query = state["query"]
            if stratum.key == "outside":
                query = {"bool": {"filter": [query], "must_not": [window_filter(self.config.mapping, fields, *state["window"])]}}
            elif stratum.start is not None and stratum.end is not None:
                query = {"bool": {"filter": [query, window_filter(self.config.mapping, fields, stratum.start, stratum.end)]}}
            scored = sample.method != "none"
            if scored:
                query = random_score_query(query, sample.seed)
            restarts = 0
            while True:
                current = SourceCursor(unit_id=unit.id, mode=mode)
                try:
                    yield from self._pull_stratum(unit, current, query, scored, stratum, page_size, budget, seen, restarts, resume, last=position == len(strata) - 1)
                    break
                except SourceCursorExpired:
                    self.close(current)
                    restarts += 1
                    resume = None
                    if restarts > self.config.pull.max_restarts:
                        raise
                if budget is not None and budget.capped:
                    break
            if budget is not None and budget.capped:
                return

    def _pull_stratum(self, unit, cursor, query, scored, stratum, page_size, budget, seen, restarts, resume, last) -> Iterator[SourceBatch]:
        includes = self.includes()
        sort = self._sort(scored)
        keepalive = self.config.pull.keepalive
        fetched_here = 0
        search_after = resume
        try:
            while True:
                want = min(page_size, stratum.quota - fetched_here)
                if budget is not None:
                    want = min(want, budget.remaining())
                if want <= 0:
                    capped = budget is not None and budget.remaining() <= 0 and fetched_here < stratum.quota
                    yield SourceBatch(unit_id=unit.id, cursor=cursor.model_copy(deep=True), stratum=stratum.key, done=last or capped, capped=capped, restarts=restarts)
                    return
                if cursor.mode == "scroll" and cursor.state.get("scroll_id"):
                    _, payload = self.transport.request("POST", "/_search/scroll", body={"scroll": keepalive, "scroll_id": cursor.state["scroll_id"]})
                else:
                    body: dict[str, Any] = {"query": query, "sort": sort, "size": want, "_source": includes, "track_total_hits": False}
                    cursor.state["size"] = want  # a scroll keeps its opening page size
                    params: dict[str, Any] = {}
                    if cursor.mode == "scroll":
                        params["scroll"] = keepalive
                        self.cursors.reserve(f"/{unit.index}/_search")
                    if search_after is not None:
                        body["search_after"] = search_after
                    _, payload = self.transport.request("POST", f"/{unit.index}/_search", params=params, body=body)
                    if cursor.mode == "scroll":
                        self.cursors.track(cursor)
                if cursor.mode == "scroll":
                    scroll_id = (payload or {}).get("_scroll_id")
                    if not isinstance(scroll_id, str):
                        raise SourceUnavailable("scroll response has no _scroll_id", f"POST /{unit.index}/_search")
                    cursor.state["scroll_id"] = scroll_id
                    if scroll_id not in cursor.opened_ids:
                        cursor.opened_ids.append(scroll_id)
                raw_hits = ((payload or {}).get("hits") or {}).get("hits") or []
                requested = cursor.state.get("size", want)
                # quota and partition budget bound what is kept from a page (scroll pages keep their size)
                hits = raw_hits[: want]
                docs = []
                for hit in hits:
                    source_id = hit.get("_id")
                    if not isinstance(source_id, str):
                        raise SourceUnavailable("hit without _id", f"POST /{unit.index}/_search")
                    if source_id in seen:
                        continue
                    seen.add(source_id)
                    docs.append(SourceDoc(source_id=source_id, index=hit.get("_index") or unit.index, fields=hit.get("_source") or {}))
                fetched_here += len(hits)
                stratum.fetched += len(docs)
                cursor.fetched += len(docs)
                if budget is not None:
                    budget.spend(unit.id, len(docs))
                if hits and cursor.mode == "search_after":
                    search_after = hits[-1].get("sort")
                    cursor.state["search_after"] = search_after
                exhausted = len(raw_hits) < requested or fetched_here >= stratum.quota
                capped = budget is not None and budget.capped and not (exhausted and last)
                if exhausted or capped:
                    self.close(cursor)
                yield SourceBatch(unit_id=unit.id, docs=docs, cursor=cursor.model_copy(deep=True), stratum=stratum.key, done=(exhausted and last) or capped, capped=capped, restarts=restarts)
                if exhausted or capped:
                    return
        finally:
            self.close(cursor)

    # --- context ---------------------------------------------------------------------------

    def reset_context_budget(self) -> None:
        self._context_used = 0

    def context(self, doc: SourceDoc, before: int, after: int) -> list[SourceDoc]:
        """Neighbouring lines of the same container (when mapped) around ``doc``; capped per
        partition by ``limits.max_context_per_partition`` (refused before I/O once spent)."""
        path = f"/{doc.index}/_search"
        if self._context_used + 1 > self.config.limits.max_context_per_partition:
            raise SourceRequestRefused("POST", path, "context fetch cap per partition reached")
        if doc.index not in self.transport.known_indices:
            raise SourceRequestRefused("POST", path, f"index {doc.index!r} was not returned by discovery")
        self._context_used += 1
        mapping = self.config.mapping
        fields = self.mapping(doc.index)
        ts = lookup(doc.fields, mapping.timestamp)
        if ts is None:
            raise SourceSchemaDrift(f"document has no {mapping.timestamp!r}; no context can be fetched", "context")
        filters: list[dict[str, Any]] = []
        if mapping.container:
            target = exact_field(fields, mapping.container)
            value = lookup(doc.fields, mapping.container)
            if target and isinstance(value, str):
                filters.append({"term": {target: value}})
        out: list[SourceDoc] = []
        for count, op, order in ((before, "lte", "desc"), (after, "gte", "asc")):
            if count <= 0:
                continue
            query = {"bool": {"filter": [*filters, {"range": {mapping.timestamp: {op: ts}}}]}}
            body = {"query": query, "sort": [{mapping.timestamp: order}], "size": count + 1, "_source": self.includes()}
            _, payload = self.transport.request("POST", path, body=body)
            hits = [h for h in ((payload or {}).get("hits") or {}).get("hits") or [] if h.get("_id") != doc.source_id][:count]
            docs = [SourceDoc(source_id=h["_id"], index=h.get("_index") or doc.index, fields=h.get("_source") or {}) for h in hits]
            out.extend(reversed(docs) if order == "desc" else docs)
        return out

    # --- close -----------------------------------------------------------------------------

    def close(self, cursor: SourceCursor) -> None:
        """Idempotent. Only ids listed in ``cursor.opened_ids`` (and opened by this transport)."""
        if cursor.closed:
            return
        try:
            if cursor.mode == "scroll" and cursor.opened_ids:
                ids = [i for i in cursor.opened_ids if i in self.transport.own_ids]
                if ids:
                    self.transport.request("DELETE", "/_search/scroll", body={"scroll_id": ids}, close_ids=cursor.opened_ids, tolerate=(404,))
                    self.transport.own_ids.difference_update(ids)
        finally:
            cursor.closed = True
            self.cursors.release(cursor)

    # --- PIT probe (capability only) -------------------------------------------------------

    def probe_pit(self, unit: SourceUnit) -> bool:
        """Open and close one PIT on ``unit`` (rows 8 and 9). False when the cluster refuses it."""
        caps = self.capabilities()
        os_flavor = caps.server == "opensearch"
        open_path = f"/{unit.index}/_search/point_in_time" if os_flavor else f"/{unit.index}/_pit"
        self.cursors.reserve(open_path)
        try:
            _, payload = self.transport.request("POST", open_path, params={"keep_alive": self.config.pull.keepalive})
        except SourceRequestRefused:
            raise
        except SourceError:
            return False
        pit = (payload or {}).get("pit_id" if os_flavor else "id")
        if not isinstance(pit, str):
            return False
        if os_flavor:
            self.transport.request("DELETE", "/_search/point_in_time", body={"pit_id": [pit]}, close_ids=[pit], tolerate=(404,))
        else:
            self.transport.request("DELETE", "/_pit", body={"id": pit}, close_ids=[pit], tolerate=(404,))
        self.transport.own_ids.discard(pit)
        return True
