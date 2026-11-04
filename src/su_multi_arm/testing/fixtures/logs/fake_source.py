"""In-process fake of the Elasticsearch/OpenSearch read API used by ``sources/elasticsearch_rest.py``.

It serves the allow-listed endpoints over synthetic indices (``generator.generate_day``) and records
**every** request it receives, so tests can assert that a refused request never reached it. It is a
test double, not a search engine: it evaluates the query subset the source sends (``match_all``,
``bool`` filter/must/must_not/should, ``range``, ``term``, ``terms``, ``match``, ``exists``,
``function_score`` with ``random_score``) and the sort, ``search_after``, scroll, PIT, ``_count``,
``_mapping``, ``_resolve/index``, ``_alias``, ``GET /`` and the privilege endpoints.

Use ``FakeLogCluster.transport()`` for an ``httpx.MockTransport``, or ``serve()`` for a real localhost
HTTP server (CLI tests). Knobs: ``distribution``/``version``, ``forbid_root``, ``forbid_pit``,
``privileges`` (granted index privileges), ``privileges_status``, ``expire_scroll_after`` (pages),
``redirect_paths``, ``resolve_supported``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

WORD = re.compile(r"[a-z0-9]+")


@dataclass
class Received:
    method: str
    path: str
    params: dict[str, str]
    body: Any
    headers: dict[str, str]


@dataclass
class FakeLogCluster:
    indices: dict[str, list[dict[str, Any]]]
    mappings: dict[str, dict[str, Any]]
    distribution: str = "elasticsearch"
    version: str = "7.17.0"
    forbid_root: bool = False
    forbid_pit: bool = False
    privileges: dict[str, bool] = field(default_factory=dict)  # e.g. {"write": True}
    cluster_all: bool = False
    privileges_status: int = 200
    expire_scroll_after: int | None = None
    redirect_paths: tuple[str, ...] = ()
    resolve_supported: bool = True
    requests: list[Received] = field(default_factory=list)
    scrolls: dict[str, dict[str, Any]] = field(default_factory=dict)
    pits: set[str] = field(default_factory=set)
    closed_scrolls: list[str] = field(default_factory=list)
    _n: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- adapters ------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            status, payload, headers = self.handle(request.method, request.url.path, dict(request.url.params), body, dict(request.headers))
            return httpx.Response(status, json=payload, headers=headers)

        return httpx.MockTransport(handler)

    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        """Start a localhost HTTP server; returns ``(server, base_url)``. Call ``server.shutdown()``."""
        cluster = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self):
                parts = urlsplit(self.path)
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                status, payload, headers = cluster.handle(self.command, unquote(parts.path), dict(parse_qsl(parts.query)), body, {k.lower(): v for k, v in self.headers.items()})
                data = json.dumps(payload).encode()
                self.send_response(status)
                for key, value in {**headers, "content-type": "application/json", "content-length": str(len(data))}.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = do_PUT = do_HEAD = _do

            def log_message(self, *args):  # silence
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    # --- dispatch ------------------------------------------------------------------------------

    def handle(self, method: str, path: str, params: dict[str, str], body: Any, headers: dict[str, str]) -> tuple[int, Any, dict[str, str]]:
        with self._lock:
            self.requests.append(Received(method, path, params, body, headers))
            return self._handle(method, path, params, body)

    def _handle(self, method, path, params, body):
        if path in self.redirect_paths:
            return 302, {}, {"location": "http://elsewhere.invalid/"}
        if path == "/" and method == "GET":
            if self.forbid_root:
                return 403, {"error": {"type": "security_exception"}}, {}
            version = {"number": self.version}
            if self.distribution == "opensearch":
                version["distribution"] = "opensearch"
            return 200, {"name": "fake", "version": version}, {}
        if path.startswith("/_resolve/index/") and method == "GET":
            if not self.resolve_supported:
                return 400, {"error": {"type": "illegal_argument_exception"}}, {}
            names = self._expand(path.removeprefix("/_resolve/index/"))
            return 200, {"indices": [{"name": n, "attributes": ["open"]} for n in names], "aliases": [], "data_streams": []}, {}
        if path == "/_security/user/_has_privileges":
            if self.privileges_status != 200:
                return self.privileges_status, {"error": {"type": "security_exception"}}, {}
            index = {}
            for entry in (body or {}).get("index", []):
                for name in entry.get("names", []):
                    index[name] = {p: bool(self.privileges.get(p)) for p in entry.get("privileges", [])}
            return 200, {"username": "reader", "has_all_requested": False, "cluster": {"all": self.cluster_all}, "index": index}, {}
        if path == "/_plugins/_security/api/account":
            return 200, {"user_name": "reader", "roles": ["readall"]}, {}
        if path == "/_search/scroll":
            return self._scroll(method, body)
        if path in {"/_pit", "/_search/point_in_time"} and method == "DELETE":
            ids = [body.get("id")] if path == "/_pit" else body.get("pit_id", [])
            found = [i for i in ids if i in self.pits]
            self.pits.difference_update(found)
            return (200 if found else 404), {"succeeded": bool(found), "num_freed": len(found)}, {}
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[1] == "_alias":
            return 200, {n: {"aliases": {}} for n in self._expand(parts[0])}, {}
        if len(parts) == 2 and parts[1] == "_mapping":
            if parts[0] not in self.indices:
                return 404, {"error": {"type": "index_not_found_exception"}}, {}
            return 200, {parts[0]: self.mappings[parts[0]]}, {}
        if len(parts) == 2 and parts[1] == "_count":
            names = self._expand(parts[0])
            query = (body or {}).get("query") or {"match_all": {}}
            return 200, {"count": sum(1 for n in names for d in self.indices[n] if self._score(d, query) is not None)}, {}
        if (len(parts) == 2 and parts[1] == "_pit") or (len(parts) == 3 and parts[1:] == ["_search", "point_in_time"]):
            if self.forbid_pit:
                return 403, {"error": {"type": "security_exception"}}, {}
            self._n += 1
            pit = f"pit-{self._n}"
            self.pits.add(pit)
            return 200, ({"pit_id": pit} if self.distribution == "opensearch" else {"id": pit}), {}
        if len(parts) == 2 and parts[1] == "_search":
            return self._search(parts[0], params, body or {})
        return 400, {"error": {"type": "no_handler_found_exception"}}, {}

    def _expand(self, expr: str) -> list[str]:
        out = []
        for item in expr.split(","):
            out.extend(n for n in sorted(self.indices) if fnmatch.fnmatchcase(n, item))
        return sorted(set(out))

    # --- search --------------------------------------------------------------------------------

    def _search(self, index: str, params: dict[str, str], body: dict[str, Any]):
        if index not in self.indices:
            return 404, {"error": {"type": "index_not_found_exception"}}, {}
        query = body.get("query") or {"match_all": {}}
        hits = []
        for seq, doc in enumerate(self.indices[index]):
            score = self._score({**doc, "_seq_no": seq}, query)
            if score is not None:
                hits.append({"_index": index, "_id": doc["_id"], "_score": score, "_source": doc["_source"], "_seq": seq})
        sort = body.get("sort") or [{"_score": "desc"}]
        keys = [self._sort_key(s) for s in sort]
        for field_name, order in reversed(keys):
            hits.sort(key=lambda h: self._value(h, field_name), reverse=order == "desc")
        for hit in hits:
            hit["sort"] = [self._value(hit, f) for f, _ in keys]
        if body.get("search_after") is not None:
            after = body["search_after"]
            hits = [h for h in hits if self._after(h["sort"], after, keys)]
        includes = body.get("_source")
        if isinstance(includes, list):
            for hit in hits:
                hit["_source"] = _project(hit["_source"], includes)
        for hit in hits:
            hit.pop("_seq", None)
        size = int(params.get("size", body.get("size", 10)))
        if "scroll" in params:
            self._n += 1
            scroll_id = f"scroll-{self._n}"
            self.scrolls[scroll_id] = {"rest": hits[size:], "size": size, "pages": 1}
            return 200, {"_scroll_id": scroll_id, "hits": {"hits": hits[:size]}}, {}
        return 200, {"hits": {"hits": hits[:size]}}, {}

    def _scroll(self, method: str, body: Any):
        if method == "DELETE":
            ids = body.get("scroll_id")
            ids = [ids] if isinstance(ids, str) else list(ids or [])
            found = [i for i in ids if i in self.scrolls]
            for i in found:
                del self.scrolls[i]
            self.closed_scrolls.extend(ids)
            return (200 if found else 404), {"succeeded": True, "num_freed": len(found)}, {}
        scroll_id = (body or {}).get("scroll_id")
        state = self.scrolls.get(scroll_id)
        if state is None:
            return 404, {"error": {"type": "search_context_missing_exception", "root_cause": [{"type": "search_context_missing_exception"}]}}, {}
        if self.expire_scroll_after is not None and state["pages"] >= self.expire_scroll_after:
            del self.scrolls[scroll_id]
            self.expire_scroll_after = None  # expire once
            return 404, {"error": {"type": "search_context_missing_exception"}}, {}
        page, state["rest"] = state["rest"][: state["size"]], state["rest"][state["size"]:]
        state["pages"] += 1
        return 200, {"_scroll_id": scroll_id, "hits": {"hits": page}}, {}

    @staticmethod
    def _sort_key(spec: Any) -> tuple[str, str]:
        if isinstance(spec, str):
            return spec, "desc" if spec == "_score" else "asc"
        (name, order), = spec.items()
        return name, order.get("order", "asc") if isinstance(order, dict) else order

    @staticmethod
    def _value(hit: dict[str, Any], name: str) -> Any:
        if name == "_score":
            return hit["_score"]
        if name in {"_doc", "_seq_no"}:
            return hit.get("_seq", 0)
        if name == "_id":
            return hit["_id"]
        value = _lookup(hit["_source"], name.removesuffix(".keyword"))
        if name == "@timestamp" or isinstance(value, str) and _is_time(value):
            parsed = _time(value)
            return int(parsed.timestamp() * 1000) if parsed else 0
        return value if value is not None else ""

    @staticmethod
    def _after(values: list[Any], after: list[Any], keys: list[tuple[str, str]]) -> bool:
        for value, mark, (_, order) in zip(values, after, keys):
            if value == mark:
                continue
            return value > mark if order == "asc" else value < mark
        return False

    # --- query evaluation (subset) -------------------------------------------------------------

    def _score(self, doc: dict[str, Any], query: dict[str, Any]) -> float | None:
        (kind, spec), = query.items()
        source = doc["_source"]
        if kind == "match_all":
            return 1.0
        if kind == "bool":
            for clause in _list(spec.get("filter")) + _list(spec.get("must")):
                if self._score(doc, clause) is None:
                    return None
            for clause in _list(spec.get("must_not")):
                if self._score(doc, clause) is not None:
                    return None
            should = _list(spec.get("should"))
            if should and not (spec.get("filter") or spec.get("must")) and not any(self._score(doc, c) is not None for c in should):
                return None
            return 1.0
        if kind == "range":
            (name, bounds), = spec.items()
            value = _time(_lookup(source, name))
            if value is None:
                return None
            for op, raw in bounds.items():
                if op == "format":
                    continue
                mark = _time(raw)
                ok = {"gte": value >= mark, "gt": value > mark, "lte": value <= mark, "lt": value < mark}[op]
                if not ok:
                    return None
            return 1.0
        if kind in {"term", "terms"}:
            (name, wanted), = spec.items()
            wanted = wanted.get("value") if isinstance(wanted, dict) else wanted
            values = wanted if isinstance(wanted, list) else [wanted]
            return 1.0 if _lookup(source, name.removesuffix(".keyword")) in values else None
        if kind == "match":
            (name, wanted), = spec.items()
            text = wanted.get("query") if isinstance(wanted, dict) else wanted
            operator = wanted.get("operator", "or") if isinstance(wanted, dict) else "or"
            value = _lookup(source, name)
            if not isinstance(value, str):
                return None
            tokens, have = set(WORD.findall(str(text).lower())), set(WORD.findall(value.lower()))
            ok = tokens <= have if operator == "and" else bool(tokens & have)
            return 1.0 if ok else None
        if kind == "exists":
            return 1.0 if _lookup(source, spec["field"]) is not None else None
        if kind == "function_score":
            if self._score(doc, spec.get("query") or {"match_all": {}}) is None:
                return None
            seed = (spec.get("random_score") or {}).get("seed", 0)
            digest = hashlib.sha256(f"{seed}:{doc.get('_seq_no', 0)}:{doc['_id']}".encode()).hexdigest()
            return int(digest[:12], 16) / float(16**12)
        raise ValueError(f"fake cluster does not evaluate query kind {kind!r}")


def _list(value: Any) -> list[Any]:
    return [] if value is None else value if isinstance(value, list) else [value]


def _lookup(source: dict[str, Any], path: str) -> Any:
    if path in source:
        return source[path]
    node: Any = source
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _project(source: dict[str, Any], includes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in includes:
        value = _lookup(source, path)
        if value is None:
            continue
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def _is_time(value: str) -> bool:
    return len(value) >= 19 and value[4] == "-" and value[10] == "T"


def _time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
