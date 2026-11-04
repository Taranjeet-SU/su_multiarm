"""Alias-backed generation registry for Elasticsearch (``ingestion/release.py::GenerationRegistry``).

``CURRENT`` is the dataset's ``docs`` alias: ``activate`` delegates to the adapter's atomic
``_aliases`` switch and is the only code that moves it (with ``expected``: the adapter checks the
aliases name the expected generation and removes them with ``must_exist``, so a concurrent switch
fails the whole request); ``current()`` resolves the alias through ``active_generation``. Manifests,
failure records and the write lease live in ``<prefix>__registry``. A failed build never calls
``activate``, so the alias keeps serving the previous generation.

The write lease (``ingestion/lease.py``) is the ``lock:<dataset>`` document with
``payload: {owner, expires_at}``: created with ``_create`` (a conflict with a live lease is a typed
``WriteConflict(held)``, never a wait); takeover of an expired lease, renewal and release are
conditional writes (``if_seq_no``/``if_primary_term``) on the document version just read and happen
only when the owner matches, so a writer never deletes another holder's lease.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator

from ...core.errors import BackendError
from ...ingestion.lease import LEASE_TTL_SECONDS, hold
from ...ingestion.release import ANY, generation_id
from .._lucene_common import MAX_MATCHED_DEFAULT

if TYPE_CHECKING:  # pragma: no cover
    from .adapter import ElasticsearchBackend

LOCK_TTL_SECONDS = LEASE_TTL_SECONDS
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

    def activate(self, manifest: dict[str, Any], *, expected: str | None = ANY) -> None:
        if self.manifest(manifest["generation"]) is None:
            raise ValueError("cannot activate a generation without a written manifest")
        if expected is ANY:
            self.backend.activate(generation_id(manifest))
        else:
            self.backend.activate(generation_id(manifest), expected=expected)

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

    # --- write lease -------------------------------------------------------------------------

    @property
    def _lock_id(self) -> str:
        return f"lock:{self.dataset}"

    def _lock_body(self, token: str, ttl: float) -> dict[str, Any]:
        now = self.backend.clock()
        return {"kind": "lock", "dataset": self.dataset, "generation": None, "created_at": now, "payload": {"owner": token, "expires_at": now + ttl}}

    def _read_lock(self, operation: str) -> dict[str, Any] | None:
        """The lease document with its version (``_seq_no``/``_primary_term``), or ``None``."""
        name = self._ensure()
        try:
            reply = self.backend.call(operation, "GET", f"/{name}/_doc/{self._lock_id}")
        except BackendError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return reply if reply.get("found") else None

    @staticmethod
    def _version(found: dict[str, Any]) -> dict[str, Any]:
        return {"if_seq_no": found.get("_seq_no"), "if_primary_term": found.get("_primary_term"), "refresh": "true"}

    def _conditional(self, operation: str, method: str, found: dict[str, Any], body: Any = None) -> bool:
        """One write conditional on the version read; a version conflict (someone else wrote) or a
        vanished document is ``False``."""
        try:
            self.backend.call(operation, method, f"/{self.index}/_doc/{self._lock_id}", params=self._version(found), body=body)
        except BackendError as exc:
            if "HTTP 409" in str(exc) or "HTTP 404" in str(exc):
                return False
            raise
        return True

    def lease_acquire(self, token: str, ttl: float) -> bool:
        name = self._ensure()
        try:
            self.backend.call("lock", "PUT", f"/{name}/_create/{self._lock_id}", params={"refresh": "true"}, body=self._lock_body(token, ttl))
            return True
        except BackendError as exc:
            if "HTTP 409" not in str(exc):
                raise
        found = self._read_lock("lock")
        if found is None:
            return False  # released between the two requests: refused now, never a retry loop
        source = found.get("_source") or {}
        payload = source.get("payload") or {}
        # A lease written before owner tokens existed (no ``expires_at``) expires ``ttl`` after it was taken.
        expires = float(payload["expires_at"]) if "expires_at" in payload else float(source.get("created_at") or 0.0) + ttl
        if expires > self.backend.clock():
            return False
        return self._conditional("lock", "PUT", found, self._lock_body(token, ttl))

    def _owned(self, operation: str, token: str) -> dict[str, Any] | None:
        found = self._read_lock(operation)
        if found is None or ((found.get("_source") or {}).get("payload") or {}).get("owner") != token:
            return None
        return found

    def lease_renew(self, token: str, ttl: float) -> bool:
        found = self._owned("renew", token)
        return found is not None and self._conditional("renew", "PUT", found, self._lock_body(token, ttl))

    def lease_release(self, token: str) -> bool:
        found = self._owned("unlock", token)
        return found is not None and self._conditional("unlock", "DELETE", found)

    @contextmanager
    def lock(self) -> Iterator[Any]:
        with hold(self, self.backend.kind, ttl=self.lock_ttl, held_message=f"another ingestion holds the lock for dataset {self.dataset!r}") as lease:
            yield lease
