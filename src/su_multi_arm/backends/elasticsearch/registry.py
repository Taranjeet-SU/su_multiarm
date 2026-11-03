"""Alias-backed generation registry for Elasticsearch (``ingestion/release.py::GenerationRegistry``).

``CURRENT`` is the dataset's ``docs`` alias: ``activate`` delegates to the adapter's atomic
``_aliases`` switch and is the only code that moves it; ``current()`` resolves the alias through
``active_generation``. Manifests, failure records and the ingestion lock live in
``<prefix>__registry``. A failed build never calls ``activate``, so the alias keeps serving the
previous generation.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from ...core.errors import BackendError
from ...ingestion.release import generation_id
from .._lucene_common import MAX_MATCHED_DEFAULT

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import ElasticsearchBackend

LOCK_TTL_SECONDS = 3600.0
REGISTRY_BODY: dict[str, Any] = {
    "settings": {"index": {"number_of_shards": 1, "number_of_replicas": 0}},
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "kind": {"type": "keyword"},
            "dataset": {"type": "keyword"},
            "generation": {"type": "keyword"},
            "created_at": {"type": "double"},
            "payload": {"type": "object", "enabled": False},
        },
    },
}


class ElasticsearchGenerationRegistry:
    def __init__(self, backend: ElasticsearchBackend, dataset: str, lock_ttl_seconds: float = LOCK_TTL_SECONDS):
        self.backend = backend
        self.dataset = dataset
        self.lock_ttl = lock_ttl_seconds
        self._ready = False

    @property
    def index(self) -> str:
        return f"{self.backend.prefix}__registry"

    def _ensure(self) -> str:
        if self._ready:
            return self.index
        try:
            self.backend.call("registry", "GET", f"/{self.index}/_mapping")
        except BackendError as exc:
            if "unknown generation" not in str(exc):
                raise
            self.backend.call("registry", "PUT", f"/{self.index}", body=REGISTRY_BODY)
        self._ready = True
        return self.index

    def _doc(self, kind: str, generation: str) -> str:
        return f"{kind}:{self.dataset}:{generation}"

    def _get(self, operation: str, doc_id: str) -> dict[str, Any] | None:
        name = self._ensure()
        try:
            reply = self.backend.call(operation, "GET", f"/{name}/_doc/{doc_id}")
        except BackendError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return reply.get("_source") if reply.get("found") else None

    def _put(self, operation: str, doc_id: str, kind: str, generation: str | None, payload: dict[str, Any]) -> None:
        name = self._ensure()
        body = {"kind": kind, "dataset": self.dataset, "generation": generation, "created_at": self.backend.clock(), "payload": payload}
        self.backend.call(operation, "PUT", f"/{name}/_doc/{doc_id}", params={"refresh": "true"}, body=body)

    def _search(self, operation: str, kind: str) -> list[dict[str, Any]]:
        name = self._ensure()
        body = {
            "size": MAX_MATCHED_DEFAULT,
            "query": {"bool": {"filter": [{"term": {"kind": kind}}, {"term": {"dataset": self.dataset}}]}},
            "sort": [{"generation": {"order": "asc"}}],
        }
        return [h["_source"] for h in self.backend.call(operation, "POST", f"/{name}/_search", body=body)["hits"]["hits"]]

    # --- GenerationRegistry ------------------------------------------------------------------

    def current(self) -> dict[str, Any] | None:
        generation = self.backend.active_generation(self.dataset)
        if generation is None:
            return None
        manifest = self.manifest(generation)
        if manifest is None:
            raise ValueError(f"CURRENT names generation {generation} but its manifest is missing")
        return manifest

    def manifest(self, generation: str) -> dict[str, Any] | None:
        found = self._get("manifest", self._doc("manifest", generation))
        return None if found is None else dict(found["payload"])

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        self._put("write_manifest", self._doc("manifest", manifest["generation"]), "manifest", manifest["generation"], dict(manifest))

    def activate(self, manifest: dict[str, Any]) -> None:
        if self.manifest(manifest["generation"]) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        self.backend.activate(generation_id(manifest))

    def current_generation(self) -> str | None:
        return self.backend.active_generation(self.dataset)

    def deactivate(self, generation: str) -> None:
        manifest = self.manifest(generation)
        if manifest is not None:
            self.backend.deactivate_generation(generation_id(manifest))

    def generations(self) -> list[str]:
        return sorted(row["generation"] for row in self._search("generations", "manifest"))

    def record_failure(self, failure: dict[str, Any]) -> None:
        self._put("record_failure", self._doc("failure", failure["generation"]), "failure", failure["generation"], dict(failure))

    def failures(self) -> list[dict[str, Any]]:
        return [dict(row["payload"]) for row in self._search("failures", "failure")]

    @contextmanager
    def lock(self) -> Iterator[None]:
        name = self._ensure()
        doc_id = f"lock:{self.dataset}"
        body = {"kind": "lock", "dataset": self.dataset, "generation": None, "created_at": self.backend.clock(), "payload": {}}
        path = f"/{name}/_create/{doc_id}"
        try:
            self.backend.call("lock", "PUT", path, params={"refresh": "true"}, body=body)
        except BackendError as exc:
            if "HTTP 409" not in str(exc):
                raise
            holder = self._get("lock", doc_id) or {}
            held_since = float(holder.get("created_at") or 0.0)
            if held_since + self.lock_ttl > self.backend.clock():
                raise BackendError(self.backend.kind, "lock", f"another ingestion holds the lock for dataset {self.dataset!r} (since {held_since})") from exc
            self._release(name, doc_id)
            self.backend.call("lock", "PUT", path, params={"refresh": "true"}, body=body)
        try:
            yield
        finally:
            self._release(name, doc_id)

    def _release(self, name: str, doc_id: str) -> None:
        try:
            self.backend.call("lock", "DELETE", f"/{name}/_doc/{doc_id}", params={"refresh": "true"})
        except BackendError as exc:
            if "HTTP 404" not in str(exc):
                raise
