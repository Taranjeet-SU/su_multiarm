"""``OpenSearchBackend``: catalog, lexical, vector and records facets on the frozen contracts.

Generation identity: every index is ``<prefix>_<dataset>_<generation>_<docs|chunks|records>``.
Dataset-level aliases (``<prefix>_<dataset>_<kind>``) are the serving pointer; ``activate(gen)``
switches all of them in one atomic ``_aliases`` request, so a build that never reaches activation
never touches what the alias serves and older generations stay until ``delete_generation``.
``delete_generation`` refuses a generation that is protected (CURRENT / pinned, reported by the
write path) or currently aliased. Reads always address a generation's own indices, never an alias.

Failure modes are typed ``BackendError``s: unreachable server, HTTP errors (with the engine's error
type and reason), a missing generation (``index_not_found``), an index whose mapping was written by
another mapping version, a refused query, a bulk reply with any failed item.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from typing import Any, Callable

from ...core.errors import BackendError
from ...core.types import GenerationId
from ...ingestion.lease import stale_base
from ...ingestion.release import ANY
from .._lucene_common import (
    DYNAMIC_TEMPLATES,
    INDEX_KINDS,
    MAPPING_VERSION,
    MAX_MATCHED_DEFAULT,
    NORMALIZER_SETTINGS,
    TYPED_PROPERTIES,
    Transport,
    TransportError,
    alias_name,
    check_dataset,
    check_prefix,
    generation_pattern,
    index_name,
    meta,
)
from .catalog import OpenSearchCatalog, OpenSearchRecords
from .search import OpenSearchLexical, OpenSearchVector

KIND = "opensearch"
ANALYZER_ID = "lucene_standard"
DOCS_PATTERN = re.compile(r"^(?P<prefix>[a-z][a-z0-9_]*)_(?P<dataset>[a-z0-9][a-z0-9_-]*)_(?P<generation>[0-9a-f]{32})_docs$")


def _mappings(index_kind: str, dataset: str, generation: str, knn: bool) -> dict[str, Any]:
    if index_kind == "docs":
        properties: dict[str, Any] = {"id": {"type": "keyword"}, "data": {"type": "object", "enabled": False}, **TYPED_PROPERTIES}
    elif index_kind == "chunks":
        properties = {
            "chunk_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "field": {"type": "keyword"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "recipe": {"type": "keyword"},
            "text": {"type": "text", "analyzer": "standard"},
            "title": {"type": "text", "analyzer": "standard"},
            "lexical": {"type": "boolean"},
            **TYPED_PROPERTIES,
        }
    else:
        properties = {"id": {"type": "keyword"}, "schema_id": {"type": "keyword"}, "data": {"type": "object", "enabled": False}, **TYPED_PROPERTIES}
    extra = {"analyzer_id": ANALYZER_ID, "knn": "hnsw" if knn else "exact"} if index_kind == "chunks" else {}
    return {
        "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0, **({"knn": True} if index_kind == "chunks" and knn else {})}, **NORMALIZER_SETTINGS},
        "mappings": {"_meta": meta(index_kind, dataset, generation, **extra), "dynamic": "strict", "dynamic_templates": DYNAMIC_TEMPLATES, "properties": properties},
    }


class OpenSearchBackend:
    """One cluster, one index prefix, four facets. ``protect(gen)`` marks CURRENT / pinned generations."""

    kind = KIND

    def __init__(
        self,
        hosts: str | list[str] | None = None,
        index_prefix: str = "su_multi_arm",
        knn_method: str = "exact",
        exact_rescore: bool = True,
        timeout_seconds: float = 30.0,
        max_matched: int = MAX_MATCHED_DEFAULT,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if knn_method not in {"exact", "hnsw"}:
            raise BackendError(self.kind, "init", f"unknown knn method {knn_method!r} (exact | hnsw)")
        if knn_method == "hnsw" and not exact_rescore:
            raise BackendError(self.kind, "init", "knn.method hnsw requires exact_rescore: true in this release (ANN scores are never returned as-is)")
        if max_matched < 1:
            raise BackendError(self.kind, "init", "max_matched must be positive")
        if transport is None:
            if not hosts:
                raise BackendError(self.kind, "init", "hosts are required")
            transport = self._default_transport([hosts] if isinstance(hosts, str) else list(hosts), timeout_seconds)
        self.prefix = check_prefix(self.kind, index_prefix)
        self.knn_method = knn_method
        self.exact_rescore = exact_rescore
        self.timeout = timeout_seconds
        self.max_matched = max_matched
        self.transport = transport
        self.clock = clock
        self.stats: Counter[str] = Counter()
        self._protected: set[str] = set()
        self._known: set[str] = set()  # index names verified to exist with this mapping version
        self.catalog = OpenSearchCatalog(self)
        self.lexical = OpenSearchLexical(self)
        self.vector = OpenSearchVector(self)
        self.records = OpenSearchRecords(self)

    def _default_transport(self, hosts: list[str], timeout_seconds: float) -> Transport:
        """The engine's client (overridden per engine)."""
        from .transport import OpenSearchTransport

        return OpenSearchTransport(hosts, timeout_seconds)

    def index_body(self, index_kind: str, gen: GenerationId) -> dict[str, Any]:
        """Settings + mappings of a generation index (overridden where an engine differs)."""
        return _mappings(index_kind, gen.dataset, gen.generation, self.knn_method == "hnsw")

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> OpenSearchBackend:
        raw = os.environ.get(config.hosts_env)
        if not raw:
            raise BackendError(cls.kind, "init", f"environment variable {config.hosts_env} (hosts) is not set")
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        return cls(hosts, config.index_prefix, config.knn.method, config.knn.exact_rescore, config.timeout_seconds, clock=clock)

    # --- transport ---------------------------------------------------------------------------

    def call(self, operation: str, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        """One request; every failure is a ``BackendError`` naming the operation."""
        self.stats[f"request.{operation}"] += 1
        try:
            return self.transport.request(method, path, params, body)
        except TransportError as exc:
            if exc.status == 404 and exc.error_type == "index_not_found_exception":
                raise BackendError(self.kind, operation, f"unknown generation ({exc.reason})") from exc
            if exc.status is None:
                raise BackendError(self.kind, operation, f"server unavailable: {exc.error_type}: {exc.reason}") from exc
            raise BackendError(self.kind, operation, f"HTTP {exc.status} {exc.error_type}: {exc.reason}") from exc

    def probe(self) -> dict[str, Any]:
        """Server identity (``GET /``); refuses anything that is not OpenSearch 2.x."""
        info = self.call("probe", "GET", "/")
        version = info.get("version", {}) if isinstance(info, dict) else {}
        distribution, number = version.get("distribution"), str(version.get("number", ""))
        if distribution != "opensearch" or not number.startswith("2."):
            raise BackendError(self.kind, "probe", f"expected an OpenSearch 2.x server, got {distribution!r} {number!r}")
        return {"distribution": distribution, "version": number, "lucene_version": version.get("lucene_version")}

    # --- naming and index lifecycle ----------------------------------------------------------

    def index(self, gen: GenerationId, index_kind: str) -> str:
        return index_name(self.prefix, check_dataset(self.kind, "index", gen.dataset), gen.generation, index_kind)

    def alias(self, dataset: str, index_kind: str) -> str:
        return alias_name(self.prefix, check_dataset(self.kind, "alias", dataset), index_kind)

    def ensure_index(self, gen: GenerationId, index_kind: str, operation: str) -> str:
        """Create the generation's index of ``index_kind`` when absent; verify its mapping version otherwise."""
        name = self.index(gen, index_kind)
        if name in self._known:
            return name
        try:
            existing = self.call(operation, "GET", f"/{name}/_mapping")
        except BackendError as exc:
            if "unknown generation" not in str(exc):
                raise
            body = self.index_body(index_kind, gen)
            self.call(operation, "PUT", f"/{name}", body=body)
            self._known.add(name)
            return name
        self._check_meta(operation, name, existing, index_kind)
        self._known.add(name)
        return name

    def require_index(self, gen: GenerationId, index_kind: str, operation: str) -> str:
        """The index must exist (reads never create); the first touch verifies the mapping version."""
        name = self.index(gen, index_kind)
        if name in self._known:
            return name
        existing = self.call(operation, "GET", f"/{name}/_mapping")
        self._check_meta(operation, name, existing, index_kind)
        self._known.add(name)
        return name

    def _check_meta(self, operation: str, name: str, mapping_reply: Any, index_kind: str) -> None:
        info = (mapping_reply or {}).get(name, {}).get("mappings", {}).get("_meta", {}).get("su_multi_arm") if isinstance(mapping_reply, dict) else None
        if not isinstance(info, dict):
            raise BackendError(self.kind, operation, f"index {name} was not created by this adapter (no mapping metadata)")
        if info.get("mapping_version") != MAPPING_VERSION or info.get("kind") != index_kind:
            raise BackendError(self.kind, operation, f"index {name}: mapping mismatch (version {info.get('mapping_version')!r} kind {info.get('kind')!r}; adapter expects {MAPPING_VERSION} {index_kind!r})")
        if index_kind == "chunks" and info.get("knn") != ("hnsw" if self.knn_method == "hnsw" else "exact"):
            raise BackendError(self.kind, operation, f"index {name}: built with knn {info.get('knn')!r}, adapter configured for {self.knn_method!r}")

    def refresh(self, gen: GenerationId, operation: str = "refresh") -> None:
        """Make every write to the generation visible to searches; unknown generation is an error."""
        names = [self.index(gen, k) for k in INDEX_KINDS]
        self.call(operation, "POST", f"/{','.join(names)}/_refresh", params={"ignore_unavailable": "true"})
        self.require_index(gen, "docs", operation)

    def generation_indices(self, gen: GenerationId) -> list[str]:
        pattern = generation_pattern(self.prefix, check_dataset(self.kind, "generations", gen.dataset), gen.generation)
        reply = self.call("generations", "GET", f"/_cat/indices/{pattern}", params={"format": "json", "h": "index"})
        return sorted(row["index"] for row in reply or [])

    # --- lifecycle -----------------------------------------------------------------------------

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def alias_indices(self, alias: str, operation: str) -> list[str]:
        """Indices currently behind ``alias`` (empty when the alias does not exist)."""
        try:
            reply = self.call(operation, "GET", f"/_alias/{alias}")
        except BackendError as exc:
            if "HTTP 404" in str(exc) or "unknown generation" in str(exc):
                return []
            raise
        return sorted(reply) if isinstance(reply, dict) else []

    def active_generation(self, dataset: str) -> str | None:
        """Generation the dataset's ``docs`` alias serves, or ``None`` (reads the adapter's alias, never CURRENT)."""
        names = self.alias_indices(self.alias(dataset, "docs"), "active_generation")
        generations = set()
        for name in names:
            match = DOCS_PATTERN.match(name)
            if match is None or match.group("prefix") != self.prefix or match.group("dataset") != dataset:
                raise BackendError(self.kind, "active_generation", f"alias serves a foreign index {name}")
            generations.add(match.group("generation"))
        if not generations:
            return None
        if len(generations) != 1:
            raise BackendError(self.kind, "active_generation", f"docs alias points at {len(generations)} generations")
        return generations.pop()

    def activate(self, gen: GenerationId, expected: str | None = ANY) -> None:
        """Atomically point the dataset aliases at this generation's indices (release step only).

        Every alias switch is one ``_aliases`` request with explicit ``remove`` + ``add`` actions, so
        readers of the alias never observe an empty or doubled alias; the previous generation's
        indices are left in place until ``delete_generation``. With ``expected`` (compare-and-set on
        what the pointer named before) the docs alias must name ``expected`` (or nothing), and the
        request removes the expected generation's docs index from the docs alias with
        ``must_exist``: if another writer switched the alias after the check, that action fails and
        the whole request (applied atomically or not at all) changes nothing. Moving an alias that
        names nothing cannot be made conditional on the server; there the write lease alone
        excludes a second writer.
        """
        conditional = expected is not ANY
        if conditional:
            actual = self.active_generation(gen.dataset)
            if actual != expected:
                raise stale_base(self.kind, actual, expected)
        fence = None
        if conditional and expected is not None and expected != gen.generation:
            fence = self.index(gen.model_copy(update={"generation": expected}), "docs")
        actions = []
        for index_kind in INDEX_KINDS:
            alias = self.alias(gen.dataset, index_kind)
            name = self.index(gen, index_kind)
            try:
                self.require_index(gen, index_kind, "activate")
            except BackendError as exc:
                if index_kind == "records" and "unknown generation" in str(exc):
                    continue  # a generation without records leaves the records alias untouched
                raise
            if index_kind == "docs" and fence is not None:
                actions.append({"remove": {"index": fence, "alias": alias, "must_exist": True}})
            else:
                for current in self.alias_indices(alias, "activate"):
                    if current != name:
                        actions.append({"remove": {"index": current, "alias": alias}})
            actions.append({"add": {"index": name, "alias": alias}})
        try:
            self.call("activate", "POST", "/_aliases", body={"actions": actions})
        except BackendError as exc:
            if fence is not None and "HTTP 404" in str(exc):
                raise stale_base(self.kind, self.active_generation(gen.dataset), expected) from exc
            raise

    # ``extras.GenerationPointer``: the dataset aliases are this backend's own pointer when it holds
    # derived projections of a generation whose authoritative ``CURRENT`` lives on another backend.

    def activate_generation(self, gen: GenerationId, expected: str | None = ANY) -> None:
        self.activate(gen, expected)

    def deactivate_generation(self, gen: GenerationId) -> None:
        """Remove the dataset aliases only where they still point at ``gen`` (rollback of a failed
        first release); an alias naming another generation is left untouched."""
        actions = []
        for index_kind in INDEX_KINDS:
            alias = self.alias(gen.dataset, index_kind)
            name = self.index(gen, index_kind)
            if name in self.alias_indices(alias, "deactivate"):
                actions.append({"remove": {"index": name, "alias": alias}})
        if actions:
            self.call("deactivate", "POST", "/_aliases", body={"actions": actions})

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"indices": [self.index(gen, k) for k in INDEX_KINDS], "aliases": [self.alias(gen.dataset, k) for k in INDEX_KINDS]}

    def delete_generation(self, gen: GenerationId) -> None:
        if gen.prefix() in self._protected:
            raise BackendError(self.kind, "delete_generation", "generation is active or pinned by a live session")
        if self.active_generation(gen.dataset) == gen.generation:
            raise BackendError(self.kind, "delete_generation", "generation is served by the dataset alias; activate another generation first")
        names = self.generation_indices(gen)
        if names:
            self.call("delete_generation", "DELETE", f"/{','.join(names)}")
        for name in [n for n in self._known if n.startswith(f"{self.prefix}_{gen.dataset}_{gen.generation}_")]:
            self._known.discard(name)
        self.vector.forget(gen)

    def generations(self) -> list[str]:
        reply = self.call("generations", "GET", f"/_cat/indices/{self.prefix}_*_docs", params={"format": "json", "h": "index"})
        found = set()
        for row in reply or []:
            match = DOCS_PATTERN.match(row["index"])
            if match is not None and match.group("prefix") == self.prefix:
                found.add(f"{match.group('dataset')}/{match.group('generation')}/")
        return sorted(found)

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close is not None:
            close()
