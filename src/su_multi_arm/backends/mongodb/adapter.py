"""``MongoBackend``: catalog, lexical, records, cache, ledger and session facets over one database.

Graph is not declared. Vector search exists only on MongoDB Atlas; the matrix keeps it ``planned``
and this adapter exposes no ``vector`` facet, so configuration naming ``(mongodb, vector)`` is
rejected rather than silently served by a community server that lacks it.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from typing import Any, Callable

from .._scope import MAX_MATCHED_DEFAULT
from ...core.errors import BackendError
from ...core.types import GenerationId, MetadataSchema
from ._db import KIND, check_name, generation_name, mapped
from .catalog import MongoCatalog, MongoRecords
from .registry import MongoGenerationRegistry
from .runtime import MongoCache, MongoLedger, MongoSessions
from .search import MongoLexical

GENERATION_SUFFIXES = ("documents", "chunks", "lexical", "records")


class MongoBackend:
    """One database and one collection prefix, six facets. ``protect(gen)`` marks a generation as
    CURRENT/pinned so ``delete_generation`` refuses it. ``client`` may be injected (tests)."""

    kind = KIND
    max_matched = MAX_MATCHED_DEFAULT  # typed records-scan cap (backends/_scope.py); an instance may lower it

    def __init__(
        self,
        uri: str,
        database: str,
        collection_prefix: str = "su_multi_arm",
        timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
        client: Any = None,
        session_ttl_seconds: float | None = None,
        max_values_cardinality: int = 10000,
    ):
        if timeout_seconds <= 0:
            raise BackendError(KIND, "init", "timeout_seconds must be positive")
        if max_values_cardinality < 1:
            raise BackendError(KIND, "init", "max_values_cardinality must be positive")
        self.prefix = check_name(collection_prefix, "collection_prefix")
        self.database_name = check_name(database, "database")
        self.timeout = timeout_seconds
        self.clock = clock
        self.session_ttl_seconds = session_ttl_seconds
        self.max_values_cardinality = max_values_cardinality
        self.stats: Counter[str] = Counter()
        self._protected: set[str] = set()
        self._registry: MongoGenerationRegistry | None = None
        self._prepared: set[str] = set()
        if client is None:
            try:
                from pymongo import MongoClient
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise BackendError(KIND, "init", "pymongo is not installed; install the 'mongodb' extra") from exc
            millis = int(timeout_seconds * 1000)
            client = MongoClient(
                uri,
                serverSelectionTimeoutMS=millis,
                connectTimeoutMS=millis,
                socketTimeoutMS=millis,
                tz_aware=True,
            )
        self.client = client
        self.db = client[self.database_name]
        self.catalog = MongoCatalog(self)
        self.lexical = MongoLexical(self)
        self.records = MongoRecords(self)
        self.cache = MongoCache(self)
        self.ledger = MongoLedger(self)
        self.session = MongoSessions(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time) -> MongoBackend:
        uri = os.environ.get(config.uri_env)
        if not uri:
            raise BackendError(KIND, "init", f"environment variable {config.uri_env} (uri_env) is not set")
        return cls(uri, config.database, config.collection_prefix, config.timeout_seconds, clock)

    # --- collections ---------------------------------------------------------------------------

    def collection(self, name: str) -> Any:
        """A runtime collection under the prefix (``cache``, ``ledger``, ``jobs``, ...)."""
        return self.db[f"{self.prefix}_{name}"]

    def gen_collection(self, gen: GenerationId, suffix: str) -> Any:
        return self.db[f"{generation_name(self.prefix, gen.dataset, gen.generation)}_{suffix}"]

    def generations_collection(self) -> Any:
        return self.collection("generations")

    def generation_doc(self, gen: GenerationId) -> dict[str, Any] | None:
        with mapped("generation"):
            return self.generations_collection().find_one({"_id": gen.prefix()})

    def require_generation(self, gen: GenerationId, create: bool = False) -> None:
        """Every generation-scoped operation starts here: unknown generations are a typed error."""
        if self.generation_doc(gen) is not None:
            return
        if not create:
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
        with mapped("generation"):
            self.generations_collection().update_one(
                {"_id": gen.prefix()},
                {
                    "$setOnInsert": {
                        "dataset": gen.dataset,
                        "generation": gen.generation,
                        "index_format": gen.index_format,
                        "fingerprint": gen.fingerprint,
                        "created_at": self.clock(),
                    }
                },
                upsert=True,
            )
            self.ensure_generation_indexes(gen)

    def ensure_generation_indexes(self, gen: GenerationId) -> None:
        key = gen.prefix()
        if key in self._prepared:
            return
        with mapped("index"):
            self.gen_collection(gen, "documents").create_index([("fields.name", 1), ("fields.t", 1)])
            self.gen_collection(gen, "documents").create_index([("fields.name", 1), ("fields.n", 1)])
            self.gen_collection(gen, "chunks").create_index([("document_id", 1)])
            self.gen_collection(gen, "lexical").create_index([("text", "text"), ("title", "text")], weights={"text": 1, "title": 2}, default_language="english", name="text_index")
            self.gen_collection(gen, "lexical").create_index([("document_id", 1)])
            self.gen_collection(gen, "records").create_index([("fields.name", 1), ("fields.t", 1)])
            self.gen_collection(gen, "records").create_index([("fields.name", 1), ("fields.n", 1)])
        self._prepared.add(key)

    def metadata_schema(self, gen: GenerationId) -> MetadataSchema:
        doc = self.generation_doc(gen)
        if doc is None:
            raise BackendError(KIND, "generation", f"unknown generation {gen.prefix()}")
        raw = doc.get("metadata_schema")
        return MetadataSchema.model_validate_json(raw) if raw else MetadataSchema()

    def set_metadata_schema(self, gen: GenerationId, schema: MetadataSchema) -> None:
        with mapped("upsert"):
            self.generations_collection().update_one({"_id": gen.prefix()}, {"$set": {"metadata_schema": schema.model_dump_json()}})

    # --- lifecycle -----------------------------------------------------------------------------

    def probe(self) -> dict[str, Any]:
        """``doctor --probe``: server reachability and version (``ping``); failures are typed."""
        return self.ping()

    def generation_identity(self, gen: GenerationId) -> dict[str, Any]:
        return {"database": self.database_name, "collections": f"{generation_name(self.prefix, gen.dataset, gen.generation)}_*"}

    def protect(self, gen: GenerationId) -> None:
        self._protected.add(gen.prefix())

    def unprotect(self, gen: GenerationId) -> None:
        self._protected.discard(gen.prefix())

    def delete_generation(self, gen: GenerationId) -> None:
        """Drops the generation's collections and its cache keys; refused for a protected generation."""
        if gen.prefix() in self._protected:
            raise BackendError(KIND, "delete_generation", "generation is active or pinned by a live session")
        with mapped("delete_generation"):
            for suffix in GENERATION_SUFFIXES:
                self.gen_collection(gen, suffix).drop()
            self.generations_collection().delete_one({"_id": gen.prefix()})
        self._prepared.discard(gen.prefix())
        self.cache.invalidate(gen.prefix())

    def generations(self) -> list[str]:
        with mapped("generations"):
            docs = self.generations_collection().find({}, {"_id": 1})
            return sorted(d["_id"] for d in docs)

    def generation_registry(self) -> MongoGenerationRegistry:
        if self._registry is None:
            self._registry = MongoGenerationRegistry(self)
        return self._registry

    def ping(self) -> dict[str, Any]:
        """Server reachability and version; a typed error when the server is unavailable."""
        with mapped("ping"):
            info = self.client.server_info()
            return {"version": info.get("version"), "database": self.database_name, "prefix": self.prefix}

    def drop_all(self) -> None:
        """Test helper: removes every collection under this prefix (never another prefix's)."""
        with mapped("drop_all"):
            for name in self.db.list_collection_names():
                if name.startswith(self.prefix + "_"):
                    self.db[name].drop()
        self._prepared.clear()
