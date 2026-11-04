"""Declarative configuration v2 (plan §3.3). ``schema_version: 2``.

Top-level keys: ``dataset``, ``profile``, ``plugins``, ``backends`` (named instances), ``storage``
(authoritative backend per non-arm capability), ``metadata_schema``, ``models.roles``, ``pipeline``
(decision, arms, selection, review, guards, generation), ``extraction``, ``communities``,
``ingestion``, ``sessions``, ``budgets``, ``privacy``, ``serving``, ``sources`` (read-only log
sources, ``core/config_sources.py``), ``learning`` (LLM pattern learning, ``core/config_learning.py``).

Validation is static: it never connects to anything (that is ``doctor --probe``). Every failure is a
``ConfigError`` with the dotted path of the offending key. Unknown keys are errors. No value in a
config can name executable code: pickled artifacts, ``module:attr`` factories and dotted import
paths are rejected (``core/component.py``); third-party components come only from entry points
listed under ``plugins:``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import Field, PrivateAttr, ValidationError, field_validator, model_validator

from ..backends import capabilities as caps
from ..models import StrictModel
from ..profiles import apply_profile, available_profiles
from .component import scan_for_code_references
from .config_decision import StagePolicies
from .config_graph import GraphOntology, TraversalTemplate
from .config_learning import LearningConfig
from .config_selection import OrderingKind, PackingConfig
from .config_logs import LINE_TEXT_FIELDS, PIN_REGISTRY_KINDS, LineRecipeConfig, LogsConfig, ProjectionsConfig
from .config_sources import SOURCE_NAME, SourceConfig
from .errors import ConfigError
from .request import RequestConfig
from .types import EdgeKind, MetadataSchema

IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"
SQL_IDENTIFIER = r"^[a-z][a-z0-9_]{0,40}$"
# ``structured_output: native`` and ``prompt_caching`` are checked against the per-(provider,
# model) registry in ``providers/capabilities.py`` (imported lazily in ``ChatRole.coherent``).
# Built-in guards available in this release (Phase 2 ships the first ones).
BUILTIN_GUARDS: frozenset[str] = frozenset()
# Built-in read-only tools and write actions (``runtime/builtin_tools.py``, ``builtin_actions.py``).
BUILTIN_TOOLS: tuple[str, ...] = ("search", "get_document", "list_values", "aggregate")
# Typed read-only log tools (addendum A2 §4.1.5, §4.5.1; ``runtime/log_tools.py``); they need
# ``logs.intents.enabled``.
LOG_TOOLS: tuple[str, ...] = (
    "log_top",
    "log_new",
    "log_affected",
    "log_trend",
    "log_compare",
    "log_tenants",
    "log_around",
    "log_endpoints",
    "log_signature",
    "log_coverage",
    "incidents",
    "graph_neighbourhood",
)
BUILTIN_ACTIONS: tuple[str, ...] = ("ingest_delta", "invalidate_cache")
DEFAULT_PIPELINE = "default"
AVAILABLE_PROFILES: frozenset[str] = available_profiles()
# Keys whose string values are free text or patterns, exempt from the code-reference scan.
SCAN_EXEMPT_KEYS: frozenset[str] = frozenset({"prompt", "patterns", "gazetteer", "predicate_triggers", "description"})


# --- backends ----------------------------------------------------------------------------------


class Bm25Settings(StrictModel):
    """Okapi BM25 lexical engine (``lexical_engine: bm25``), the formula of ``rank_bm25.BM25Okapi``:
    ATIRE idf ``log(N - df + 0.5) - log(df + 0.5)`` with negative idf replaced by
    ``epsilon * mean(idf)``. ``tokenizer``: ``unicode_word`` = ``\\w+`` runs, ``ascii_alnum`` =
    ``[a-z0-9]+`` runs of the lower-cased text. ``stemming``: ``none`` or a Snowball algorithm name.
    ``include_title``: the document title's tokens are prepended to every chunk of the document.
    All of it is index recipe: changing any value needs a rebuild."""

    k1: float = Field(default=1.5, ge=0, le=10)
    b: float = Field(default=0.75, ge=0, le=1)
    epsilon: float = Field(default=0.25, ge=0, le=10)
    tokenizer: Literal["unicode_word", "ascii_alnum"] = "unicode_word"
    lowercase: bool = True
    stemming: str = "none"
    include_title: bool = False

    @field_validator("stemming")
    @classmethod
    def known_stemmer(cls, value: str) -> str:
        import snowballstemmer

        if value != "none" and value not in snowballstemmer.algorithms():
            raise ValueError(f"unknown stemmer {value!r}; use 'none' or one of {sorted(snowballstemmer.algorithms())}")
        return value


class SqliteBackend(StrictModel):
    kind: Literal["sqlite"]
    path: Path = Path("state")
    vector_engine: Literal["numpy", "faiss"] = "numpy"
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    # ``fts5`` (default): SQLite FTS5 ``unicode61`` with its bm25(); ``bm25``: an in-process Okapi
    # BM25 inverted index built at ingest into the generation (memory-mapped segment).
    lexical_engine: Literal["fts5", "bm25"] = "fts5"
    bm25: Bm25Settings | None = None

    @model_validator(mode="after")
    def bm25_settings_need_engine(self):
        if self.lexical_engine == "bm25" and self.bm25 is None:
            self.bm25 = Bm25Settings()
        if self.lexical_engine != "bm25" and self.bm25 is not None:
            raise ValueError("bm25 settings need lexical_engine: bm25")
        return self

    def recipe(self) -> dict[str, Any]:
        """The index-recipe dump. The lexical engine keys enter it only when ``bm25`` is selected, so
        indexes built before the option existed keep their fingerprint (2.0.0a1 golden values)."""
        exclude = {"lexical_engine", "bm25"} if self.lexical_engine == "fts5" else set()
        return self.model_dump(mode="json", exclude=exclude)


class MemoryBackendConfig(StrictModel):
    kind: Literal["memory"]


class PgVectorConfig(StrictModel):
    """pgvector search. ``exact`` (default) scans with the scope in the WHERE clause; ``hnsw`` builds
    per-namespace HNSW indexes at refresh and fails the refresh when the measured filter recall
    against exact search is below ``min_filter_recall``."""

    index: Literal["exact", "hnsw"] = "exact"
    hnsw_m: int = Field(default=16, ge=2, le=100)
    hnsw_ef_construction: int = Field(default=64, ge=4, le=1000)
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000)
    min_filter_recall: float = Field(default=0.95, ge=0, le=1)


class PostgresBackend(StrictModel):
    kind: Literal["postgres"]
    dsn_env: str = "SU_POSTGRES_DSN"
    table_prefix: str = Field(default="su_multi_arm", pattern=SQL_IDENTIFIER)
    text_search_config: str = Field(default="english", pattern=r"^[a-z_][a-z0-9_]{0,62}(\.[a-z_][a-z0-9_]{0,62})?$")
    vector: PgVectorConfig = Field(default_factory=PgVectorConfig)
    timeout_seconds: float = Field(default=30, gt=0, le=600)


class SqlAlchemyBackend(StrictModel):
    kind: Literal["sqlalchemy"]
    url_env: str
    table_prefix: str = Field(default="su_multi_arm", pattern=SQL_IDENTIFIER)
    timeout_seconds: float = Field(default=30, gt=0, le=600)


class KnnConfig(StrictModel):
    method: Literal["hnsw", "exact"] = "exact"
    exact_rescore: bool = True


class LuceneRecordsConfig(StrictModel):
    """How the ``records`` facet evaluates a typed scope. ``client`` (default): the matched records are
    scanned and the operations computed in Python, bounded by ``max_matched`` (typed refusal above
    it). ``native``: the operations are compiled to aggregations on the server (no record scan);
    ``matched_ids`` then holds at most ``matched_ids_cap`` ids (``matched_ids_truncated`` above it), more
    than ``max_groups`` groups is a ``bounded`` result, and ``count_distinct`` is exact up to
    ``distinct_exact_cap`` distinct values, ``approximate`` (engine ``cardinality``) above it. A query-time
    setting: not part of the index recipe."""

    aggregation: Literal["client", "native"] = "client"
    matched_ids_cap: int = Field(default=10000, ge=1, le=100000)
    max_groups: int = Field(default=10000, ge=1, le=50000)
    distinct_exact_cap: int = Field(default=100000, ge=1, le=1000000)


class LuceneBackend(StrictModel):
    kind: Literal["opensearch", "elasticsearch"]
    hosts_env: str
    index_prefix: str = Field(default="su_multi_arm", pattern=SQL_IDENTIFIER)
    knn: KnnConfig = Field(default_factory=KnnConfig)
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    records: LuceneRecordsConfig = Field(default_factory=LuceneRecordsConfig)

    def recipe(self) -> dict[str, Any]:
        """The index-recipe dump: ``records`` is query-time only, so it never changes an index identity."""
        return self.model_dump(mode="json", exclude={"records"})


class Neo4jBackend(StrictModel):
    kind: Literal["neo4j"]
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password_env: str = "SU_NEO4J_PASSWORD"
    database: str = "neo4j"
    # Generation keys are ``<namespace>|<dataset>|<generation>``; distinct namespaces never touch each other.
    namespace: str = Field(default="su_multi_arm", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    timeout_seconds: float = Field(default=30, gt=0, le=600)


class KuzuAttach(StrictModel):
    """Read-only attachment of an existing Kuzu database: ``mapping`` is a YAML file that maps its node
    and relationship tables onto the graph contract (``backends/kuzu/attach.py``). Private mappings
    stay outside the repository; the path is relative to this config file."""

    mapping: Path


class KuzuBackend(StrictModel):
    kind: Literal["kuzu"]
    # ``native``: the package's own embedded store (created when missing, generation-scoped, writable).
    # ``attach``: an existing database opened read-only through ``attach.mapping``; never written.
    path: Path = Path("state/graph.kuzu")
    mode: Literal["native", "attach"] = "native"
    namespace: str = Field(default="su_multi_arm", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    attach: KuzuAttach | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    buffer_pool_mb: int = Field(default=512, ge=16, le=65536)

    @model_validator(mode="after")
    def coherent_mode(self):
        if (self.mode == "attach") != (self.attach is not None):
            raise ValueError("mode: attach requires an attach.mapping block, and only attach mode takes one")
        return self


class MongoBackend(StrictModel):
    kind: Literal["mongodb"]
    uri_env: str
    database: str
    collection_prefix: str = Field(default="su_multi_arm", pattern=SQL_IDENTIFIER)
    timeout_seconds: float = Field(default=30, gt=0, le=600)


BackendConfig = Annotated[
    Union[SqliteBackend, MemoryBackendConfig, PostgresBackend, SqlAlchemyBackend, LuceneBackend, Neo4jBackend, KuzuBackend, MongoBackend],
    Field(discriminator="kind"),
]


class StorageConfig(StrictModel):
    """Authoritative backend per non-arm capability; each must name exactly one backend declaring it."""

    catalog: str = Field(pattern=IDENTIFIER)
    records: str = Field(pattern=IDENTIFIER)
    ledger: str = Field(pattern=IDENTIFIER)
    sessions: str = Field(pattern=IDENTIFIER)
    cache: str = Field(pattern=IDENTIFIER)
    communities: str = Field(pattern=IDENTIFIER)


# --- models ------------------------------------------------------------------------------------


class CapabilityDeclaration(StrictModel):
    """What a chat role relies on; validated against ``providers/capabilities.py``.

    ``structured_output``: ``none`` (schema in the system prompt, strict validation of the
    reply), ``json_mode`` (``response_format: json_object``; OpenAI-style providers only),
    ``json_schema`` (``response_format: json_schema``, ``strict: false``; OpenAI-style providers
    only), ``native`` (provider-enforced: Anthropic forced tool use or ``output_config.format``,
    OpenAI strict json_schema; only for registry-declared ``(provider, model)`` pairs). When a role
    does not set ``structured_output``, Anthropic models the registry declares capable get
    ``native`` (2.0.0a3); every other role gets ``none``. ``prompt_caching``: ``controlled`` sends
    ``cache_control`` (Anthropic); ``passive`` records cached tokens the provider reports.
    """

    # ``None`` (unset) resolves in ``ChatRole``: ``native`` for registry-capable Anthropic models,
    # ``none`` otherwise. A validated role always carries the resolved value.
    structured_output: Literal["none", "json_mode", "json_schema", "native"] | None = None
    prompt_caching: Literal["none", "passive", "controlled"] = "none"
    streaming: bool = False


class EvidenceBudget(StrictModel):
    chars: int = Field(default=60000, ge=500, le=500000)


class ChatRole(StrictModel):
    provider: Literal["openai_compatible", "openai", "anthropic"]
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str | None = None
    # Output ceiling. A model the registry marks as thinking by default (Anthropic Haiku 5.x)
    # rejects values below its ``min_max_tokens`` (thinking tokens count against the ceiling).
    max_tokens: int = Field(default=2048, ge=128, le=64000)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    # Output-limit parameter name for ``openai_compatible`` servers; hosted providers use the
    # registry's name (``openai`` must match it, ``anthropic`` always sends ``max_tokens``). Ollama's
    # ``/v1`` ignores ``max_completion_tokens``: set ``max_tokens`` for it (docs/CONFIGURATION.md).
    max_tokens_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    capabilities: CapabilityDeclaration = Field(default_factory=CapabilityDeclaration)
    evidence_budget: EvidenceBudget | None = None
    pricing_key: str | None = None  # key into budgets.price_table; ``price_key()`` falls back to the model id
    # How ``structured_output`` was resolved: ``declared`` (the config sets it) or ``registry_default``
    # (unset; resolved from the registry). Only a registry default may be moved to prompt mode for an
    # oversized schema (``ModelRoles``) or switched once at run time (``ProviderSchemaTooComplex``).
    _structured_output_source: str = PrivateAttr(default="declared")
    _structured_output_note: str | None = PrivateAttr(default=None)

    def price_key(self) -> str:
        """What the engine passes to ``Budget.reserve_call``; the documented default is the model id."""
        return self.pricing_key or self.model

    def __eq__(self, other: object) -> bool:
        # Equality is over the configured fields: how ``structured_output`` was resolved is
        # provenance (a dumped and reloaded role declares the resolved value), not configuration.
        if not isinstance(other, ChatRole):
            return NotImplemented
        return type(self) is type(other) and self.__dict__ == other.__dict__

    __hash__ = None  # type: ignore[assignment]  # mutable model, as pydantic's default

    @property
    def structured_output_defaulted(self) -> bool:
        """``structured_output`` was not set in the config (it came from the registry default)."""
        return self._structured_output_source == "registry_default"

    @property
    def structured_output_note(self) -> str | None:
        """Why the registry default was resolved to prompt mode at config time (``None`` otherwise)."""
        return self._structured_output_note

    def use_prompt_mode(self, note: str) -> None:
        """Resolve a registry-default ``native`` to ``none`` (prompt mode) and record why. Only for
        a role whose mode was not declared; a declared mode is never changed."""
        if not self.structured_output_defaulted:  # pragma: no cover - guarded by the caller
            raise ValueError("a declared structured_output is never changed")
        self.capabilities = self.capabilities.model_copy(update={"structured_output": "none"})
        self._structured_output_note = note

    @model_validator(mode="after")
    def coherent(self):
        from ..providers.capabilities import check_role, default_structured_output

        if self.provider == "anthropic" and not self.api_key_env:
            raise ValueError("Anthropic requires api_key_env")
        if self.provider == "openai" and not self.api_key_env:
            raise ValueError("OpenAI requires api_key_env")
        # Registry-derived default (2.0.0a3): an unset structured_output resolves here, so a
        # validated role always carries a concrete mode (and dumps it).
        if self.capabilities.structured_output is None:
            mode = default_structured_output(self.provider, self.model)
            self.capabilities = self.capabilities.model_copy(update={"structured_output": mode})
            self._structured_output_source = "registry_default"
        problems = check_role(
            self.provider,
            self.model,
            self.capabilities.structured_output,
            self.capabilities.prompt_caching,
            self.reasoning_effort,
            self.max_tokens_parameter if self.provider == "openai" else None,
            self.max_tokens,
        )
        if problems:
            raise ValueError("; ".join(f"{key}: {message}" for key, message in problems))
        return self


class PrecomputedVerify(StrictModel):
    """Optional content check of imported rows: re-embed ``sample`` matched chunks (seeded, through
    the configured provider with its ``document_prefix``) and require cosine ``>= min_cosine``
    against the imported row for every sampled chunk. This is the only check that can detect a
    vector row paired with the wrong key.

    ``input`` is what the producer embedded for a keyed chunk, and so what is re-embedded:
    ``engine_template`` (default) is the input the package's own embedder uses (``{title}\\n{field}:
    {text}``, ``ingestion.write.EMBEDDING_INPUT``; also what ``on_missing: embed`` embeds);
    ``chunk_text`` is the bare chunk text as keyed. A producer that used another template cannot be
    spot-checked (set ``sample: 0``); the check never guesses."""

    sample: int = Field(default=0, ge=0, le=10000)  # 0 = off
    min_cosine: float = Field(default=0.98, gt=0, le=1)
    seed: int = 0
    input: Literal["engine_template", "chunk_text"] = "engine_template"


class PrecomputedVectors(StrictModel):
    """Import document vectors computed elsewhere. ``vectors`` is a 2-D float32 ``.npy`` file;
    ``keys`` is a JSONL file with one object per row ``{"document_id", "field", "text_sha256"}``.

    What is verified: both files' SHA-256 equal the pinned ``sha256`` (vectors) and ``keys_sha256``
    (keys); row count, dimension, finite and non-zero rows, unique keys; and every chunk is matched
    to a key by document, field and the SHA-256 of its exact text (never fuzzily). What is NOT
    verifiable from the files alone: that row ``i`` of the vectors embeds the text named by line
    ``i`` of the keys. That pairing is positional; the two pinned digests fix the pair as published,
    and ``verify`` (a re-embedding spot check with the configured provider) is the check that
    detects a misaligned pair. Index identity comes from the two digests, never from file paths.
    A chunk with no row is an error unless ``on_missing: embed``. Queries are always encoded by the
    configured provider, which must be the model that produced the rows."""

    vectors: Path
    keys: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    keys_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    on_missing: Literal["error", "embed"] = "error"
    verify: PrecomputedVerify = Field(default_factory=PrecomputedVerify)

    def identity(self) -> dict[str, str]:
        """Content identity of the import (no paths): enters the index fingerprint and recipe."""
        return {"sha256": self.sha256, "keys_sha256": self.keys_sha256, "on_missing": self.on_missing}


class EmbedderRole(StrictModel):
    provider: Literal["hash", "sentence_transformers", "openai", "openai_compatible"] = "hash"
    model: str = "hash-demo-v1"
    dimensions: int = Field(default=256, ge=16, le=65536)
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str | None = None
    batch_size: int = Field(default=32, ge=1, le=1024)
    query_prefix: str = ""
    document_prefix: str = ""
    revision: str | None = None
    device: str = "cpu"
    precomputed: PrecomputedVectors | None = None
    # Key into budgets.price_table for hosted embedders (falls back to the model id). Not part of the
    # index recipe: the write path excludes it from the fingerprint.
    pricing_key: str | None = None

    def price_key(self) -> str | None:
        """Pricing key for the write-path USD budget; ``None`` for local embedders (no spend)."""
        if self.provider in {"hash", "sentence_transformers"}:
            return None
        return self.pricing_key or self.model

    @model_validator(mode="after")
    def keyed(self):
        if self.provider == "openai" and not self.api_key_env:
            raise ValueError("OpenAI embeddings require api_key_env")
        return self


class RerankerRole(StrictModel):
    provider: Literal["sentence_transformers"] = "sentence_transformers"
    model: str = Field(min_length=1)


# Configuration keys for features that do not exist in this release (completeness review risk R3).
# A configured key is refused with a clear message instead of being accepted and never read; each
# moves out of these tables when the phase that consumes it lands.
UNIMPLEMENTED_ROLES: dict[str, str] = {
    "judge": "the evaluation harness and its judges arrive in Phase 4",
    "cache_gate": "the cache arm and its gate arrive in Phase 2",
}
UNIMPLEMENTED_EXTRACTION: dict[str, str] = {
    "ontology": "the ontology contract arrives in Phase 3",
}


def reject_unimplemented(data: Any, keys: dict[str, str], prefix: str) -> Any:
    """``ConfigError`` (with the key path) for any key of ``keys`` present in a mapping."""
    if isinstance(data, dict):
        for key, why in keys.items():
            if key in data:
                raise ConfigError(f"not implemented in this release ({why}); remove the key", f"{prefix}.{key}")
    return data


class ModelRoles(StrictModel):
    planner: ChatRole | None = None
    reviewer: ChatRole | None = None
    extractor: ChatRole | None = None
    generator: ChatRole | None = None
    community: ChatRole | None = None
    pattern_learner: ChatRole | None = None  # ``patterns learn|propose`` (learning.patterns)
    log_extractor: ChatRole | None = None  # per-line quotes of the log fallback (learning.extraction)
    embedder: EmbedderRole = Field(default_factory=EmbedderRole)
    reranker: RerankerRole | None = None

    @model_validator(mode="before")
    @classmethod
    def unimplemented_roles(cls, data):
        return reject_unimplemented(data, UNIMPLEMENTED_ROLES, "models.roles")

    @model_validator(mode="after")
    def fit_native_schemas(self):
        """Deterministic pre-flight (2.0.0a3 parity fix): a role whose ``native`` mode is only the
        registry default, realised as Anthropic ``output_config.format``, falls back to prompt mode
        (``none``) when a schema the role sends is over the provider's complexity ceilings
        (``providers.base.output_format_problems``). The resolution is recorded on the role and
        reported by ``validate`` / ``doctor`` as a ``structured_output_fallback`` warning. A declared
        ``native`` is never changed; ``validate`` reports it as an error instead."""
        from ..pipeline.validate import output_format_role_problems

        for name, role in self.chat_roles().items():
            if not role.structured_output_defaulted or role.capabilities.structured_output != "native":
                continue
            problems = output_format_role_problems(role.provider, role.model, name)
            if problems:
                role.use_prompt_mode("structured_output resolved to none (prompt mode): " + "; ".join(problems))
        return self

    def chat_roles(self) -> dict[str, ChatRole]:
        names = ("planner", "reviewer", "extractor", "generator", "community", "pattern_learner", "log_extractor")
        return {n: getattr(self, n) for n in names if getattr(self, n) is not None}


class ModelRoute(StrictModel):
    """One provider route of a learning role (owner decision 2026-11-04: local models first).

    ``health_url`` is probed (HTTP GET, 2xx = ready) before the first call of a run; for an
    ``openai_compatible`` role it defaults to ``<base_url>/models``. ``capacity_url`` is an optional
    second GET whose JSON ``{"available": true|false}`` (or a 2xx without a body) says whether the
    server takes work now. A hosted route without a ``health_url`` is not probed. There is no probe
    *command*: config never names code."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    role: ChatRole
    # ``local`` (loopback), ``lan`` (a self-hosted server on the private network; needs
    # ``egress.allow_lan``) or ``paid`` (anything else). Unset: ``local`` for a loopback
    # openai_compatible role, else ``paid``. A route declared ``local`` must be loopback.
    tier: Literal["local", "lan", "paid"] | None = None
    health_url: str | None = Field(default=None, pattern=r"^https?://")
    capacity_url: str | None = Field(default=None, pattern=r"^https?://")
    probe_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    # UTC time windows ``HH:MM-HH:MM`` in which the route takes work (a window may wrap midnight);
    # empty: always. Outside every window the route is skipped (``outside_window``).
    windows: list[str] = Field(default_factory=list, max_length=8)
    # After this many invalid replies (schema violations) in one run the route is marked down.
    max_invalid_replies: int | None = Field(default=3, ge=1, le=1000)

    @field_validator("windows")
    @classmethod
    def window_syntax(cls, value):
        for window in value:
            if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]", window):
                raise ValueError(f"window {window!r} is not HH:MM-HH:MM (UTC)")
        return value


class ModelRoutes(StrictModel):
    """Ordered provider routes per learning role (``pattern_learner``: cluster calls;
    ``log_extractor``: line and fill calls). The first available route is used; when none is
    available the items are ``unresolved: capacity_deferred`` and the next run retries them. A paid
    (non-loopback) route after a local one is used only with ``allow_paid_fallback: true``. Empty
    lists mean the single route ``models.roles.<role>``."""

    pattern_learner: list[ModelRoute] = Field(default_factory=list)
    log_extractor: list[ModelRoute] = Field(default_factory=list)
    allow_paid_fallback: bool = False

    @model_validator(mode="after")
    def unique(self):
        for name in ("pattern_learner", "log_extractor"):
            routes = getattr(self, name)
            if len({r.name for r in routes}) != len(routes):
                raise ValueError(f"models.routes.{name}: route names must be unique")
        return self


class ModelsConfig(StrictModel):
    roles: ModelRoles = Field(default_factory=ModelRoles)
    routes: ModelRoutes = Field(default_factory=ModelRoutes)


# --- pipeline ----------------------------------------------------------------------------------


class ClassifierManifestConfig(StrictModel):
    features: str = Field(pattern=r"^(hashed_ngrams|embedding:[a-z][a-z0-9_]*)$")
    labels: list[str] = Field(min_length=1)
    threshold: float = Field(ge=0, le=1)


class ClassifierArtifactConfig(StrictModel):
    model_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: ClassifierManifestConfig

    @field_validator("model_path")
    @classmethod
    def safe_artifact(cls, value: Path):
        from .component import ArtifactRef

        return ArtifactRef.check_path(value)


class DecisionConfig(StrictModel):
    kind: Literal["rules", "classifier", "llm", "cascade"] = "rules"
    stages: list[Literal["rules", "classifier", "llm"]] = Field(default_factory=list)
    on_failure: Literal["fail_closed", "raw_all_arms"] = "fail_closed"
    stage_policies: StagePolicies = Field(default_factory=StagePolicies)  # cascade stages only (§3.7.1)
    max_steps: int = Field(default=10, ge=1, le=20)
    prompt: str | None = None
    prompt_file: Path | None = None
    artifact: ClassifierArtifactConfig | None = None  # built-in onnx_classifier
    component: str | None = Field(default=None, pattern=IDENTIFIER)  # plugin classifier component

    @model_validator(mode="after")
    def coherent(self):
        if self.kind == "cascade":
            if not self.stages or len(set(self.stages)) != len(self.stages):
                raise ValueError("cascade requires a nonempty list of distinct stages")
        elif self.stages:
            raise ValueError("stages is only valid for kind: cascade")
        uses_classifier = self.kind == "classifier" or "classifier" in self.stages
        if uses_classifier and (self.artifact is None) == (self.component is None):
            raise ValueError("a classifier needs exactly one of artifact (onnx) or component (plugin)")
        if not uses_classifier and (self.artifact or self.component):
            raise ValueError("artifact/component are only valid for a classifier decision agent")
        if self.prompt and self.prompt_file:
            raise ValueError("use either prompt or prompt_file, not both")
        return self

    def uses_llm(self) -> bool:
        return self.kind == "llm" or "llm" in self.stages


class ExactIdArm(StrictModel):
    """Exact identifier lookups (plan §3.8): ``alias_map`` is a YAML/JSON file ``{aliases: {alias:
    canonical_id}}``; ``alias_fields`` are declared metadata fields whose values identify a record;
    identifiers are recognised by the ``pattern`` of ``id`` fields in ``metadata_schema`` and by the
    alias map's keys. Matching is exact (``casefold`` is the only normalisation, opt-in); an unknown
    identifier is reported, never guessed."""

    alias_map: Path | None = None
    alias_fields: list[str] = Field(default_factory=list)
    casefold: bool = False
    max_ids: int = Field(default=20, ge=1, le=200)  # identifiers resolved per step; more are reported


class DenseArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    candidates: int = Field(default=200, ge=1, le=10000)


class PhraseBoost(StrictModel):
    enabled: bool = False
    window_tokens: int = Field(default=6, ge=1, le=64)


class VerbatimBoost(StrictModel):
    """Lexical front policy (off by default): documents whose text holds a quoted string of the query,
    or at least ``min_windows`` of its ``window_tokens``-token windows verbatim (error messages, pasted
    excerpts, exact subjects), lead the lexical list, at most ``max_records``, ranked by (quoted,
    windows matched). A window counts only if one of its tokens is in at most ``common_df`` chunks; a
    window found in more than ``max_window_hits`` chunks is boilerplate and ignored; a window whose
    tokens co-occur in more than ``max_candidates`` chunks is not verified (skipped, counted in the
    trace). Needs a lexical index that exposes its analyzer and term statistics (sqlite
    ``lexical_engine: bm25``); anything else is a validation error."""

    enabled: bool = False
    window_tokens: int = Field(default=6, ge=2, le=32)
    min_windows: int = Field(default=2, ge=1, le=32)
    common_df: int = Field(default=5000, ge=1)
    max_window_hits: int = Field(default=300, ge=1)
    max_candidates: int = Field(default=1200, ge=1)
    max_records: int = Field(default=5, ge=1, le=50)
    quoted: bool = True


class IdentifierPin(StrictModel):
    """Lexical front policy (off by default): documents named by an identifier in the query lead the
    lexical list. Identifiers are matches of the ``pattern`` of ``id`` fields in ``metadata_schema``
    (``fields`` narrows which; empty = all) and resolve by exact equality on that document field
    inside the request's scope. No normalisation, no fuzzy match; at most ``max_records``."""

    enabled: bool = False
    fields: list[str] = Field(default_factory=list)
    max_records: int = Field(default=5, ge=1, le=50)


class LexicalArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    candidates: int = Field(default=200, ge=1, le=10000)
    phrase_boost: PhraseBoost = Field(default_factory=PhraseBoost)
    verbatim_boost: VerbatimBoost = Field(default_factory=VerbatimBoost)
    pin_identifiers: IdentifierPin = Field(default_factory=IdentifierPin)


class StructuredArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    # Backend whose lexical index resolves literal topic conditions (``ScopeSpec.text``) as
    # whole-corpus matches; None = ``backend`` when it serves lexical, else topics are unsupported.
    text_backend: str | None = Field(default=None, pattern=IDENTIFIER)


class GraphArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    hops: int = Field(default=1, ge=0, le=3)
    predicates: list[str] = Field(default_factory=list)
    direction: Literal["out", "in", "any"] = "any"
    # Declared traversal templates a decision agent may select by name (``core/config_graph.py``).
    templates: dict[str, TraversalTemplate] = Field(default_factory=dict)
    # Optional ontology the templates are validated against (an attached store's mapping is used too).
    ontology: GraphOntology | None = None
    # Edge kinds the free-text mode traverses (A1 §3.12). ``co_firing`` is statistical
    # co-occurrence, never a fact: admit it only deliberately (the logs ``around`` question does).
    edge_kinds: list[EdgeKind] = Field(default_factory=lambda: ["fact"], min_length=1)

    @model_validator(mode="after")
    def templates_fit(self):
        for name, template in self.templates.items():
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"template name {name!r} must be an identifier")
            problems = template.problems(self.ontology)
            if problems:
                raise ValueError(f"template {name!r} does not fit the ontology: {'; '.join(problems)}")
        return self


class CommunityArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)


class CacheArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    mode: Literal["exact"] = "exact"  # semantic tier arrives with the cache arm (Phase 2)
    ttl_seconds: int = Field(default=3600, ge=1)


class ArmsConfig(StrictModel):
    exact_id: ExactIdArm | None = None
    dense: DenseArm | None = None
    lexical: LexicalArm | None = None
    structured: StructuredArm | None = None
    graph: GraphArm | None = None
    community: CommunityArm | None = None
    cache: CacheArm | None = None

    def enabled(self) -> dict[str, Any]:
        return {name: arm for name, arm in self.__dict__.items() if arm is not None}

    @model_validator(mode="after")
    def at_least_one_retrieval_arm(self):
        if not any(getattr(self, n) is not None for n in ("exact_id", "dense", "lexical", "structured", "graph", "community")):
            raise ValueError("at least one retrieval arm must be configured")
        return self

    @model_validator(mode="after")
    def cache_arm_unimplemented(self):
        # The key is part of the plan's schema, but no cache arm ships yet (risk R3): refuse it at
        # validation, not only at engine construction.
        if self.cache is not None:
            raise ValueError("cache: not implemented in this release (the cache arm arrives in Phase 2); remove the key")
        return self


class DiversityConfig(StrictModel):
    field: str = Field(pattern=IDENTIFIER)
    min_per_value: int = Field(default=1, ge=1)


class SelectionConfig(StrictModel):
    collapse: Literal["document", "chunk"] = "document"
    per_arm: int = Field(default=20, ge=1, le=1000)
    union: Literal["preserve"] = "preserve"
    ordering: OrderingKind = "reciprocal_rank"  # per_arm is a deprecated alias (plan §3.9)
    rrf_k: int = Field(default=60, ge=1)
    source_weights: dict[str, float] = Field(default_factory=dict)
    arm_weights: dict[str, float] = Field(default_factory=dict)
    max_documents: int | None = Field(default=None, ge=1)
    diversity: DiversityConfig | None = None
    pins: list[Literal["exact_id"]] = Field(default_factory=lambda: ["exact_id"])
    # Strict, stable source order applied to the selected union after ordering and diversity, before
    # pins: records whose ``source`` is listed come first in list order, unlisted sources keep their
    # relative order after them. It never changes membership.
    source_order: list[str] = Field(default_factory=list)
    # Reserved source slots inside each arm's ``per_arm`` budget (see ``selection.limits``): position
    # ``i`` takes the best remaining record of that source, then the budget fills by score.
    source_slots: list[str] = Field(default_factory=list, max_length=1000)
    rerank: bool = False  # uses models.roles.reranker; off by default (measured negative, plan §6.4)
    packing: PackingConfig = Field(default_factory=PackingConfig)

    @model_validator(mode="after")
    def positive_weights(self):
        import math

        if any(not math.isfinite(v) or v <= 0 for v in [*self.source_weights.values(), *self.arm_weights.values()]):
            raise ValueError("selection weights must be finite and positive")
        return self


class ReviewConfig(StrictModel):
    enabled: bool = False
    max_hops: int = Field(default=2, ge=1, le=5)  # total rounds incl. the first search
    prompt: str | None = None  # reviewer prompt; built-in neutral prompt when unset

    @model_validator(mode="after")
    def reviews_at_least_once(self):
        if self.enabled and self.max_hops < 2:
            raise ValueError("pipeline.review enabled with max_hops 1 would never review; use max_hops >= 2 or disable review")
        return self


class AnswerContractConfig(StrictModel):
    # strip (default since 2.0.0a3): remove unbacked aggregate claims and, when the question asks
    # no aggregate, the unbacked count/list/absence sentences of a narrative answer (recorded); an
    # unbacked statement answering an aggregate question still abstains. abstain_with_reason:
    # abstain (a non-narrative claim on an answer whose text states no count/list/absence is
    # stripped instead). fail: raise. See generation/contract.py.
    on_violation: Literal["abstain_with_reason", "strip", "fail"] = "strip"


class GenerationStageConfig(StrictModel):
    contract: AnswerContractConfig = Field(default_factory=AnswerContractConfig)
    voice: str = Field(default="none", pattern=IDENTIFIER)
    prompt: str | None = None
    prompt_file: Path | None = None
    evidence_fields: list[str] = Field(default_factory=list)  # packing priority; empty = chunking order, then document order

    @model_validator(mode="after")
    def coherent(self):
        if self.prompt and self.prompt_file:
            raise ValueError("use either prompt or prompt_file, not both")
        return self


class PipelineConfig(StrictModel):
    """One retrieval/answer pipeline. ``pipeline`` is the default (named ``default``); every entry of
    ``pipelines`` is an overlay deep-merged onto it before validation (an arm set to ``null`` is
    disabled in that pipeline)."""

    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    arms: ArmsConfig
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    guards: list[str] = Field(default_factory=list)
    generation: GenerationStageConfig = Field(default_factory=GenerationStageConfig)
    concurrency: int = Field(default=5, ge=1, le=32)
    allow_incomplete_arms: bool = False  # partial generations: let arms on an incomplete stage run, flagged

    @field_validator("guards")
    @classmethod
    def guard_names(cls, value):
        for name in value:
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"guard {name!r} must be an identifier (no paths, no dotted imports)")
        if len(set(value)) != len(value):
            raise ValueError("guards must be unique")
        return value


# --- extraction, communities, ingestion --------------------------------------------------------


class GroundingConfig(StrictModel):
    normalisation: list[Literal["nfkc", "casefold", "whitespace"]] = Field(
        default_factory=lambda: ["nfkc", "casefold", "whitespace"]
    )
    ambiguous: Literal["reject", "first"] = "reject"


class PatternGateConfig(StrictModel):
    """The deterministic validation gate a learned pattern must pass (``learning/patterns/gate.py``)."""

    min_support: int = Field(default=3, ge=1, le=1000)  # evidence lines the pattern must match
    require_re2: bool = True  # the gate compiles with RE2; the ``regex`` engine cannot pass it
    # Learning loop (L4b) only: share of the cluster's lines the pattern must match, and the absolute
    # bound on matches among unparsed lines outside the cluster.
    min_cluster_coverage: float = Field(default=0.9, gt=0, le=1)
    max_out_of_cluster: int = Field(default=0, ge=0, le=100_000)


class PatternLibraryRef(StrictModel):
    """``extraction.pattern_library``: the library directory of the ``pattern`` extractor.

    Only ``engine``, ``max_line_bytes`` and ``mode`` enter the index recipe. The library contents,
    the engine version and the promotion policy are build provenance (generation manifest), so a
    library edit never changes the live fingerprint; it changes the extraction reuse salt instead.
    """

    path: Path
    engine: Literal["re2", "regex"] = "re2"  # explicit; a missing engine is an error, never a fallback
    max_line_bytes: int = Field(default=8192, ge=256, le=1_048_576)
    mode: Literal["line"] = "line"  # every line of a chunk is matched on its own (line-scoped facts)
    # ``auto``: a proposal that passes the gate is appended to learned.yaml with its decision record;
    # ``manual``: it waits in proposed.yaml for a human ``approve`` decision.
    promotion: Literal["auto", "manual"] = "auto"
    min_literal_chars: int = Field(default=4, ge=0, le=64)
    regex_timeout_ms: int = Field(default=100, ge=1, le=10_000)  # ``regex`` engine only
    gate: PatternGateConfig = Field(default_factory=PatternGateConfig)
    # The logs profile's header ontology (addendum A2 §4.1.3): ``emits``/``runs``/``serves``/
    # ``raises`` native links from day-rollup headers and ``co_fires_with`` from incident headers
    # (``extraction/patterns/log_links.py``). Enters the extraction reuse salt, not the recipe.
    log_links: bool = False

    def recipe(self) -> dict[str, Any]:
        return {"engine": self.engine, "max_line_bytes": self.max_line_bytes, "mode": self.mode}


class ExtractionConfig(StrictModel):
    extractors: list[Literal["pattern", "gazetteer", "regex", "spacy", "gliner", "llm"]] = Field(default_factory=lambda: ["gazetteer"])
    # Omitted from the extractor recipe while unset, so existing index fingerprints are unchanged.
    pattern_library: PatternLibraryRef | None = None
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    policy: Literal["strict", "quarantine", "partial"] = "strict"
    prompt: str | None = None  # llm extractor prompt; built-in neutral prompt when unset
    predicate_triggers: dict[str, list[str]] = Field(default_factory=dict)
    stemmer_language: str = "english"
    gazetteer: dict[str, list[str]] = Field(default_factory=dict)
    patterns: dict[str, str] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)  # entity types for gliner/llm; neutral default is none
    spacy_model: str = "en_core_web_sm"
    pos: list[str] = Field(default_factory=lambda: ["PROPN"])
    gliner_model: str = "urchade/gliner_small-v2.1"
    threshold: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def unimplemented_keys(cls, data):
        return reject_unimplemented(data, UNIMPLEMENTED_EXTRACTION, "extraction")

    @model_validator(mode="after")
    def coherent(self):
        from ..predicate_support import languages

        if self.stemmer_language not in languages():
            raise ValueError(f"unknown stemmer_language; choose one of {languages()}")
        if len(set(self.extractors)) != len(self.extractors):
            raise ValueError("extractors must be unique")
        if any(e in self.extractors for e in ("gliner", "llm")) and not self.labels:
            raise ValueError("extraction.labels must declare entity types when gliner or llm extraction is selected")
        if ("pattern" in self.extractors) != (self.pattern_library is not None):
            raise ValueError("the pattern extractor and extraction.pattern_library are set together")
        return self


class CommunityHierarchy(StrictModel):
    """``louvain_hierarchical``: level 0 is the coarsest partition; each level-``n`` community with at
    least ``min_split`` documents is re-partitioned (seeded) into level ``n+1`` children."""

    levels: int = Field(default=3, ge=1, le=3)
    resolution: float = Field(default=1.0, gt=0, le=10)
    min_split: int = Field(default=4, ge=2)


class CommunityStats(StrictModel):
    """Deterministic per-community statistics from declared fields only (``communities/stats.py``)."""

    time_basis: str | None = Field(default=None, pattern=IDENTIFIER)  # a datetime/date metadata field
    granularity: Literal["month", "year"] = "month"
    top_entities: int = Field(default=5, ge=0, le=50)


class CommunitiesConfig(StrictModel):
    # ``declared``: one community per distinct ``community_id`` metadata value (the co-firing
    # incidents composed by the logs profile, addendum A2 §4.1.4); no graph clustering runs.
    strategy: Literal["entity", "louvain", "louvain_hierarchical", "declared"] = "entity"
    min_documents: int = Field(default=2, ge=1)
    seed: int = 42
    prompt: str | None = None
    # Both enter the index recipe only when set, so existing generations keep their fingerprint.
    hierarchy: CommunityHierarchy | None = None
    stats: CommunityStats | None = None

    @model_validator(mode="after")
    def hierarchy_only_when_hierarchical(self):
        if self.hierarchy is not None and self.strategy != "louvain_hierarchical":
            raise ValueError("communities.hierarchy applies to strategy louvain_hierarchical only")
        return self

    def recipe(self) -> dict[str, Any]:
        """The index-recipe view: unset optional blocks are omitted (2.0.0a1 golden values hold)."""
        return self.model_dump(mode="json", exclude={k for k in ("hierarchy", "stats") if getattr(self, k) is None})


class ChunkingConfig(StrictModel):
    size: int = Field(default=1600, ge=100, le=16000)
    overlap: int = Field(default=160, ge=0)
    field_order: list[str] = Field(default_factory=list)  # neutral: a document's own field order; profiles may set one

    @model_validator(mode="after")
    def coherent(self):
        if self.overlap >= self.size:
            raise ValueError("overlap must be smaller than size")
        return self


class InputMapping(StrictModel):
    id: str = "id"
    title: str = "title"
    source: str | None = "source"
    source_value: str = "article"
    url: str | None = "url"
    fields: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)


class IngestionConfig(StrictModel):
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    input_mapping: InputMapping | None = None
    # Addendum A1 (L3a). Both enter the index recipe only when set, so existing fingerprints hold.
    line_recipe: LineRecipeConfig | None = None
    projections: ProjectionsConfig | None = None


# --- sessions, budgets, privacy, serving -------------------------------------------------------


class MemoryConfig(StrictModel):
    episodic: bool = True
    store_text: bool = False
    # Add the IDs already shown in this session to ``exclude_ids`` (logged in the trace). Memory only
    # ever narrows a request; it is never evidence.
    exclude_shown: bool = False

    @model_validator(mode="before")
    @classmethod
    def semantic_deferred(cls, data):
        if isinstance(data, dict) and "semantic" in data:
            raise ValueError("sessions.memory.semantic is deferred: no storage, writer or retention is defined yet")
        return data


class SessionsConfig(StrictModel):
    store: str | None = Field(default=None, pattern=IDENTIFIER)  # defaults to storage.sessions
    ttl_seconds: int = Field(default=3600, ge=1)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class SessionBudget(StrictModel):
    """Cumulative limits over every turn of one session; each request's limits are clipped to what
    the session has left, and an exhausted session refuses further turns (``BudgetExhausted``)."""

    max_llm_calls: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_usd: float | None = Field(default=None, gt=0)


class IngestBudget(StrictModel):
    """Limits of one ingestion build (the write path), separate from the per-request limits.

    ``max_llm_calls`` caps extraction and community-report calls of the whole build;
    ``max_embedding_calls`` caps embedding provider requests (one per batch of
    ``models.roles.embedder.batch_size`` chunks; reused vectors cost none); ``max_usd`` caps the
    build's spend across both, reserved before every call from ``budgets.price_table`` and
    committed with the reported usage. Exhaustion stops the stage (``budget`` category) under the
    extraction policy; nothing is downgraded or retried."""

    max_llm_calls: int | None = Field(default=10000, ge=1)
    max_embedding_calls: int | None = Field(default=None, ge=1)
    max_usd: float | None = Field(default=None, gt=0)


class ProviderBudget(StrictModel):
    """A lifetime (total-period) USD cap for one provider key across every learning and extraction
    scope (addendum A2 §4.2.6). Reservations persist in the learning spend ledger; reaching the cap
    stops the step (``budget_deferred``), never a switch to another provider."""

    max_usd_total: float = Field(gt=0)


class BudgetsConfig(StrictModel):
    max_llm_calls: int = Field(default=12, ge=1, le=1000000)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_usd: float | None = Field(default=None, gt=0)
    deadline_seconds: float = Field(default=60, gt=0, le=3600)
    price_table: Path | None = None
    session: SessionBudget | None = None  # per-session limits on top of the per-request ones above
    ingest: IngestBudget = Field(default_factory=IngestBudget)  # whole-build limits of the write path
    providers: dict[Literal["anthropic", "openai", "openai_compatible"], ProviderBudget] = Field(default_factory=dict)

    @model_validator(mode="after")
    def usd_needs_prices(self):
        if self.providers and self.price_table is None:
            raise ValueError("budgets.providers caps require price_table (operator-supplied; no bundled prices)")
        if self.max_usd is not None and self.price_table is None:
            raise ValueError("max_usd requires price_table (operator-supplied; no bundled prices)")
        if self.ingest.max_usd is not None and self.price_table is None:
            raise ValueError("ingest.max_usd requires price_table (operator-supplied; no bundled prices)")
        if self.session is not None and self.session.max_usd is not None and self.price_table is None:
            raise ValueError("session.max_usd requires price_table (operator-supplied; no bundled prices)")
        return self


REDACTOR_NAMES = (
    "email", "ipv4", "ipv6", "bearer_token", "jwt", "api_key", "aws_key", "url_credentials", "query_values", "uuid", "request_id", "hex_secret",
    "llm_text", "content", "query", "tracking_id", "record_payload", "pg_row",
    "phone", "long_numeric_id", "person_name", "record_list", "product_prose",
)
FIELD_NAME = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"


class CustomRedactor(StrictModel):
    """A user redactor of the ``safe`` engine: a regular expression in the RE2 subset that passes
    the ReDoS lint (validated at load), and what replaces each match. ``mask`` writes
    ``[REDACTED_<NAME>]``; ``hash`` writes ``[<NAME>:<12 hex>]``, a keyed sha256 of the match, so
    equal values stay joinable without being readable."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    regex: str = Field(min_length=1, max_length=2048)
    action: Literal["mask", "hash"] = "mask"


class PrivacyConfig(StrictModel):
    """Opt-in conservative redaction; not a complete PII detector.

    ``engine: legacy`` (default) is the a1/a2 behaviour: ``redact_emails`` and ``patterns`` with
    Python ``re`` on documents. ``engine: safe`` (B11) runs the named built-in ``redactors``, the
    ``custom`` redactors and the ``patterns`` (as ``mask`` redactors) through the pattern engine's
    validator (RE2 subset, ReDoS lint) and runtime (``regex_engine``: ``re2``, or the ``regex``
    development engine with a per-call timeout that fails the redaction rather than skipping it).
    It redacts log records before any persistence, ingested documents and every prompt line. The
    new keys are omitted from every fingerprint while they hold their defaults."""

    redact_emails: bool = False
    patterns: dict[str, str] = Field(default_factory=dict)
    metadata_fields: list[str] = Field(default_factory=list)
    trace_masking: Literal["privacy", "raw"] = "privacy"
    engine: Literal["legacy", "safe"] = "legacy"
    redactors: list[Literal[REDACTOR_NAMES]] = Field(default_factory=list)  # type: ignore[valid-type]
    custom: list[CustomRedactor] = Field(default_factory=list)
    regex_engine: Literal["re2", "regex"] = "re2"
    regex_timeout_ms: int = Field(default=100, ge=1, le=10_000)
    # Environment variable holding the key of ``hash`` redactors (keyed sha256). Unset: plain sha256.
    hash_key_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")
    # Per built-in override of mask/hash (``query: hash`` keeps equal queries joinable).
    actions: dict[Literal[REDACTOR_NAMES], Literal["mask", "hash"]] = Field(default_factory=dict)  # type: ignore[valid-type]
    # ``record_payload``: JSON field names whose values are masked (``None``: the built-in list of
    # entity fields: names, ids, addresses, contact data), minus ``record_fields_allow``.
    record_fields: list[str] | None = None
    record_fields_allow: list[str] = Field(default_factory=list)
    # ``person_name``: JSON/key=value keys whose values are person names (``None``: name, firstName,
    # lastName, fullName, displayName, userName, author and their snake_case forms; case-insensitive).
    person_name_fields: list[str] | None = None
    # ``record_list``: JSON keys whose array-of-objects value is a list of business-entity records
    # (``None``: records, rows, items, results, entities, customers, accounts, companies, ...).
    record_list_fields: list[str] | None = None
    # ``product_prose``: product or technology names (an allow-list: they stay visible) whose
    # following free text is masked to the end of the line; required when ``product_prose`` is on.
    prose_products: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("prose_products")
    @classmethod
    def product_names(cls, value):
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9 ._+#-]{0,62}[A-Za-z0-9])?", name):
                raise ValueError(f"prose product {name!r} must be a name of letters, digits, spaces and . _ + # - (starting and ending with a letter or digit)")
        return value

    @field_validator("record_fields", "record_fields_allow", "person_name_fields", "record_list_fields")
    @classmethod
    def field_names(cls, value):
        for name in value or []:
            if not re.fullmatch(FIELD_NAME, name):
                raise ValueError(f"record field {name!r} must be a plain field name")
        return value

    @model_validator(mode="after")
    def coherent(self):
        safe_only = {
            "redactors": self.redactors,
            "custom": self.custom,
            "hash_key_env": self.hash_key_env,
            "actions": self.actions,
            "record_fields": self.record_fields is not None,
            "record_fields_allow": self.record_fields_allow,
            "person_name_fields": self.person_name_fields is not None,
            "record_list_fields": self.record_list_fields is not None,
            "prose_products": self.prose_products,
        }
        if self.engine == "legacy":
            used = sorted(k for k, v in safe_only.items() if v)
            if used:
                raise ValueError(f"{used} need privacy.engine: safe")
            return self
        if len(set(self.redactors)) != len(self.redactors):
            raise ValueError("privacy.redactors must be unique")
        names = [c.name for c in self.custom] + [label.casefold() for label in self.patterns]
        if len(set(names)) != len(names) or set(names) & set(self.redactors):
            raise ValueError("custom redactor and pattern names must be unique and differ from the built-in names")
        unused = sorted(set(self.actions) - set(self.redactors))
        if unused:
            raise ValueError(f"privacy.actions names redactors that are not enabled: {unused}")
        if self.record_fields == []:
            raise ValueError("privacy.record_fields cannot be empty (omit it for the built-in list)")
        for key in ("person_name_fields", "record_list_fields"):
            if getattr(self, key) == []:
                raise ValueError(f"privacy.{key} cannot be empty (omit it for the built-in list)")
        if ("product_prose" in self.redactors) != bool(self.prose_products):
            raise ValueError("privacy.prose_products and the product_prose redactor go together: list the product names when product_prose is enabled, and enable it when names are listed")
        if not (self.redactors or self.custom or self.patterns or self.redact_emails):
            raise ValueError("privacy.engine: safe needs at least one redactor (redactors, custom, patterns or redact_emails)")
        from ..extraction.patterns.engine import validate_regex

        for label, regex in [*((c.name, c.regex) for c in self.custom), *self.patterns.items()]:
            errors = [i for i in validate_regex(regex, pattern_id=label, source="privacy") if i.severity == "error"]
            if errors:
                raise ValueError(f"redactor {label!r} is outside the safe engine: {errors[0].message}")
        return self

    def recipe(self) -> dict[str, Any]:
        """The fingerprint dump: the a1/a2 keys, plus the B11 keys only when they differ from their
        defaults (so existing index identities are unchanged)."""
        data = self.model_dump(mode="json", exclude={"trace_masking"})
        defaults = {
            "engine": "legacy", "redactors": [], "custom": [], "regex_engine": "re2", "regex_timeout_ms": 100, "hash_key_env": None,
            "actions": {}, "record_fields": None, "record_fields_allow": [], "person_name_fields": None, "record_list_fields": None, "prose_products": [],
        }
        for key, default in defaults.items():
            if data.get(key) == default:
                data.pop(key, None)
        return data


class HttpConfig(StrictModel):
    bind: str = "127.0.0.1:8099"
    auth: Literal["none", "bearer_env"] = "none"
    token_env: str | None = None
    page_size_max: int = Field(default=50, ge=1, le=500)

    @model_validator(mode="after")
    def coherent(self):
        host = self.bind.rsplit(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "::1", "[::1]"} and self.auth == "none":
            raise ValueError("binding outside localhost requires auth: bearer_env")
        if self.auth == "bearer_env" and not self.token_env:
            raise ValueError("auth: bearer_env requires token_env")
        return self


class McpConfig(StrictModel):
    transports: list[Literal["stdio", "streamable_http"]] = Field(default_factory=lambda: ["stdio"])
    # the enabled log tools (tools.enabled) are MCP tools too, read-only (addendum A2 §4.5.1)
    log_tools: bool = True
    # mount the streamable-HTTP MCP app at /mcp on the HTTP app (one process, one engine; N14)
    mount_on_http: bool = False
    # a separate bearer token for /mcp (else the HTTP resolver is used)
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")

    @model_validator(mode="after")
    def mount_needs_http(self):
        if self.mount_on_http and "streamable_http" not in self.transports:
            raise ValueError("serving.mcp.mount_on_http needs streamable_http in transports")
        return self


class ActionsServingConfig(StrictModel):
    allow: list[str] = Field(default_factory=list)  # default denied on HTTP/MCP


class ToolsConfig(StrictModel):
    """Read-only tools exposed through the engine (plan §3.4). Only registered components can be
    named: the built-ins; there is no code loading from config."""

    enabled: list[str] = Field(default_factory=lambda: list(BUILTIN_TOOLS))

    @field_validator("enabled")
    @classmethod
    def names(cls, value):
        for name in value:
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"tool {name!r} must be an identifier (no paths, no dotted imports)")
        if len(set(value)) != len(value):
            raise ValueError("tools must be unique")
        return value


class ActionsConfig(StrictModel):
    """Write actions (plan §3.4). Default: none. Only built-in actions named here are registered;
    every invocation (also a denied one) is audited on ``storage.ledger`` before it runs."""

    allow: list[str] = Field(default_factory=list)
    audit_retention_seconds: int = Field(default=7 * 24 * 3600, ge=1)  # idempotency window

    @field_validator("allow")
    @classmethod
    def names(cls, value):
        for name in value:
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"action {name!r} must be an identifier (no paths, no dotted imports)")
        if len(set(value)) != len(value):
            raise ValueError("actions must be unique")
        return value


class AccessConfig(StrictModel):
    """The access policy (``runtime/acl.py``) selected by name; no class is loaded from config.

    * ``none``: every principal sees the whole dataset (single-tenant serving, the default).
    * ``tenant``: every arm, tool, value listing and graph read is restricted to records whose
      ``field`` equals the principal's ``tenant``. A principal without a tenant sees nothing
      (fail closed) unless its id is listed in ``unrestricted`` (an operator)."""

    policy: Literal["none", "tenant"] = "none"
    field: str = Field(default="tenant", pattern=IDENTIFIER)
    unrestricted: list[str] = Field(default_factory=list, max_length=100)


HOST_NAME = r"^(?:\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$"


class UiConfig(StrictModel):
    """The static web UI (addendum A2 §4.4; wheel ``su-multi-arm-ui``). ``enabled`` mounts it at
    ``mount`` on the HTTP app; without the wheel the server refuses to start (``ConfigError``). The
    UI needs bearer auth (or an injected resolver) unless ``allow_unauthenticated_loopback`` is set
    explicitly for single-user development."""

    enabled: bool = False
    mount: str = Field(default="/ui", pattern=r"^/[a-z][a-z0-9_-]{0,31}$")
    display_timezone: str | None = Field(default=None, max_length=64)  # display only; never changes partition dates
    session_storage_token: bool = False  # keep the token in sessionStorage (never localStorage or a cookie)
    allow_unauthenticated_loopback: bool = False
    # A JSON theme file (brand colours, product name, logo, fonts; ``serving/ui_theme.py``). None: the
    # neutral default. Branding is deployment data and stays outside this repository.
    theme: Path | None = None

    @field_validator("mount")
    @classmethod
    def not_api(cls, value):
        if value in {"/v2", "/mcp", "/health", "/ready", "/docs", "/redoc", "/openapi.json", "/search", "/answer"}:
            raise ValueError(f"ui mount {value!r} collides with a server route")
        return value

    @field_validator("display_timezone")
    @classmethod
    def known_zone(cls, value):
        if value is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"unknown IANA timezone {value!r}") from None
        return value


class ServingAnswersSpend(StrictModel):
    """``serving.answers`` (addendum A2 rev 4): answer-time model calls (planner, generator,
    reviewer) reserve on a persistent per-day ledger under their own scope ``serving.answers``,
    separate from the learning scopes, and on ``budgets.providers.<provider>.max_usd_total`` when
    the provider has one. Off by default (per-request ``budgets`` only). A refused reservation fails
    the call before it is sent; there is no fallback to another provider."""

    enabled: bool = False
    max_usd_per_day: float | None = Field(default=None, gt=0)
    max_calls_per_day: int = Field(default=10_000, ge=1, le=10_000_000)
    ledger: Path | None = None  # default <state dir of the first sqlite backend>/.answers_spend.sqlite, else ./.answers_spend.sqlite
    timezone: str = "UTC"


class ServingConfig(StrictModel):
    http: HttpConfig = Field(default_factory=HttpConfig)
    answers: ServingAnswersSpend = Field(default_factory=ServingAnswersSpend)
    mcp: McpConfig = Field(default_factory=McpConfig)
    actions: ActionsServingConfig = Field(default_factory=ActionsServingConfig)
    # Extra Host names admitted by the Host allow-list (reverse proxy names), exact match; the list
    # is active when the UI is enabled or the list is non-empty (addendum A2 B10, n-r3-4;
    # ``serving/hosts.py``).
    allowed_hosts: list[str] = Field(default_factory=list)
    ui: UiConfig = Field(default_factory=UiConfig)

    @field_validator("allowed_hosts")
    @classmethod
    def host_names(cls, value):
        for host in value:
            if not re.fullmatch(HOST_NAME, host) or host == "*":
                raise ValueError(f"allowed host {host!r} must be an exact host name or address (no port, no wildcard)")
        return [h.lower() for h in value]


# --- root --------------------------------------------------------------------------------------


class Config(StrictModel):
    schema_version: Literal[2] = 2
    dataset: str = Field(default="default", min_length=1)
    profile: str = Field(default="neutral", pattern=IDENTIFIER)
    plugins: list[str] = Field(default_factory=list)
    backends: dict[str, BackendConfig]
    storage: StorageConfig
    metadata_schema: MetadataSchema = Field(default_factory=MetadataSchema)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    pipeline: PipelineConfig
    pipelines: dict[str, PipelineConfig] = Field(default_factory=dict)  # named overlays on ``pipeline``
    default_pipeline: str = Field(default=DEFAULT_PIPELINE, pattern=IDENTIFIER)
    request: RequestConfig = Field(default_factory=RequestConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    communities: CommunitiesConfig = Field(default_factory=CommunitiesConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    serving: ServingConfig = Field(default_factory=ServingConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    # Read-only log sources (addendum A1 §3.1, slice L1). Consumed by ``su-multi-arm sources``; the
    # engine never opens a source on the request path.
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    # LLM pattern learning (addendum A1 §3.6, slice L4b; ``core/config_learning.py``).
    learning: LearningConfig = Field(default_factory=LearningConfig)
    # Daily log analytics (addendum A1 L3a/L3b; ``core/config_logs.py``): partitions, compose, retention.
    logs: LogsConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def log_count_fields(cls, data):
        """``logs_example`` profile: the per-unit source counts ``count_<query>`` (integer) and their
        basis ``basis_<query>`` (``role: basis``) are declared for every count query of the logs
        source unless the configuration declares them itself (the names depend on the source)."""
        if not isinstance(data, dict) or data.get("profile") != "logs_example" or not isinstance(data.get("logs"), dict):
            return data
        source = (data.get("sources") or {}).get(data["logs"].get("source"))
        schema = data.get("metadata_schema")
        if not isinstance(source, dict) or not isinstance(schema, dict) or not isinstance(schema.get("fields"), list):
            return data
        declared = {f.get("name") for f in schema["fields"] if isinstance(f, dict)}
        extra = []
        for name in sorted(source.get("counts") or {}):
            if f"count_{name}" not in declared:
                extra.append({"name": f"count_{name}", "type": "integer", "sortable": True})
            if f"basis_{name}" not in declared:
                extra.append({"name": f"basis_{name}", "type": "string", "role": "basis"})
        if not extra:
            return data
        return {**data, "metadata_schema": {**schema, "fields": [*schema["fields"], *extra]}}

    @model_validator(mode="before")
    @classmethod
    def overlay_named_pipelines(cls, data):
        """Every ``pipelines.<name>`` entry is deep-merged onto ``pipeline`` (idempotent on dumps)."""
        if not isinstance(data, dict) or not isinstance(data.get("pipelines"), dict):
            return data
        base = data.get("pipeline")
        if hasattr(base, "model_dump"):
            base = base.model_dump(mode="json")
        if not isinstance(base, dict):
            return data
        from ..profiles import deep_merge

        merged = {}
        for name, entry in data["pipelines"].items():
            if hasattr(entry, "model_dump"):
                entry = entry.model_dump(mode="json")
            merged[name] = deep_merge(base, entry) if isinstance(entry, dict) else entry
        return {**data, "pipelines": merged}

    def all_pipelines(self) -> dict[str, PipelineConfig]:
        """``default`` (the ``pipeline`` block) followed by the named pipelines."""
        return {DEFAULT_PIPELINE: self.pipeline, **self.pipelines}

    @field_validator("pipelines")
    @classmethod
    def pipeline_names(cls, value):
        for name in value:
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"pipeline name {name!r} must be an identifier")
            if name == DEFAULT_PIPELINE:
                raise ValueError(f"pipeline name {DEFAULT_PIPELINE!r} is reserved for the pipeline block")
        return value

    @field_validator("plugins")
    @classmethod
    def plugin_names(cls, value):
        for name in value:
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"plugin {name!r} must be an entry-point name, not a path or import string")
        if len(set(value)) != len(value):
            raise ValueError("plugins must be unique")
        return value

    @field_validator("sources")
    @classmethod
    def source_names(cls, value):
        for name in value:
            if not re.fullmatch(SOURCE_NAME, name):
                raise ValueError(f"source name {name!r} must be an identifier")
        return value

    @field_validator("backends")
    @classmethod
    def backend_names(cls, value):
        if not value:
            raise ValueError("at least one backend must be declared")
        for name in value:
            if not re.fullmatch(IDENTIFIER, name):
                raise ValueError(f"backend name {name!r} must be an identifier")
        return value

    @model_validator(mode="after")
    def coherent(self):
        """Every static check runs and all errors are reported together (``pipeline/validate.py``);
        warnings are emitted as Python warnings."""
        from ..pipeline.validate import config_diagnostics, raise_for

        diagnostics = config_diagnostics(self)
        for diagnostic in diagnostics:
            if diagnostic.severity == "warning":
                warnings.warn(f"{diagnostic.path}: {diagnostic.message}" if diagnostic.path else diagnostic.message, stacklevel=2)
        raise_for(diagnostics)
        scan_for_code_references(self.model_dump(mode="json"), skip_keys=SCAN_EXEMPT_KEYS)
        return self

    @model_validator(mode="after")
    def logs_coherent(self):
        """Cross-key rules of ``logs`` (addendum A1 §3.4.2 n1, §3.8.3, §3.8.4 n4)."""
        logs = self.logs
        if logs is None:
            return self
        if logs.source not in self.sources:
            raise ConfigError(f"logs.source {logs.source!r} is not a configured source ({sorted(self.sources)})", "logs.source")
        if "pattern" not in self.extraction.extractors:
            raise ConfigError("logs needs the pattern extractor (extraction.extractors: [pattern, ...])", "extraction.extractors")
        kind = self.backends[self.storage.catalog].kind
        if kind not in PIN_REGISTRY_KINDS:
            raise ConfigError(
                f"logs needs a pin registry on the catalog's generation registry; {kind!r} has none (planned), "
                f"use one of {list(PIN_REGISTRY_KINDS)}",
                "storage.catalog",
            )
        recipe = self.ingestion.line_recipe
        if recipe is None or "exemplars" not in recipe.fields:
            raise ConfigError("logs needs ingestion.line_recipe with the exemplars field (one chunk per log record)", "ingestion.line_recipe")
        source = self.sources[logs.source]
        bound = source.max_line_bytes + source.multiline.max_bytes
        if bound > recipe.max_chunk_chars:
            raise ConfigError(
                f"sources.{logs.source}.max_line_bytes + multiline.max_bytes ({bound}) exceeds "
                f"ingestion.line_recipe.max_chunk_chars ({recipe.max_chunk_chars}); a record could span two chunks",
                "ingestion.line_recipe.max_chunk_chars",
            )
        if self.pipeline.arms.dense is not None:
            projections = self.ingestion.projections or ProjectionsConfig()
            for kind, fields in LINE_TEXT_FIELDS.items():
                if kind == "signature_examples" and logs.compose.exemplars != "per_tenant":
                    continue  # not composed
                embedded = [f for f in fields if projections.embeds(kind, f)]
                if embedded:
                    raise ConfigError(
                        f"documents of kind {kind!r} carry log-line text in {embedded}; exclude the kind from vector "
                        f"or set ingestion.projections.embed_fields.{kind} without them (retention invariant)",
                        "ingestion.projections",
                    )
        raw = logs.partitions.raw
        if raw.enabled and raw.path is not None:
            from .config_logs import _absolute

            root = _absolute(raw.path)
            readers = {f"backends.{n}.path": b.path for n, b in self.backends.items() if getattr(b, "path", None) is not None and isinstance(b.path, Path)}
            if self.extraction.pattern_library is not None:
                readers["extraction.pattern_library.path"] = self.extraction.pattern_library.path
            readers["the installed package (served code)"] = Path(__file__).resolve().parents[1]
            try:  # the static UI bundle, when the wheel is installed
                import su_multi_arm_ui  # type: ignore[import-not-found]

                readers["the web UI bundle (served files)"] = Path(su_multi_arm_ui.STATIC_DIR)
            except ImportError:
                pass
            for key, path in readers.items():
                other = _absolute(path)
                if other == root or root in other.parents or other in root.parents:
                    raise ConfigError(f"logs.partitions.raw.path overlaps {key}; raw copies must sit where nothing else reads", "logs.partitions.raw.path")
        if self.privacy.trace_masking != "privacy":
            raise ConfigError("logs requires privacy.trace_masking: privacy (traces hold no passages)", "privacy.trace_masking")
        if self.sessions.memory.store_text:
            raise ConfigError("logs refuses sessions.memory.store_text (sessions keep ids and hashes only)", "sessions.memory.store_text")
        kinds = logs.compose.kinds
        if logs.intents.enabled:
            missing = [k for k in ("log_day", "endpoint_day", "exception_day", "coverage_day") if not getattr(kinds, k)]
            if missing:
                raise ConfigError(f"logs.intents needs the analytics kinds {missing} (logs.compose.kinds)", "logs.compose.kinds")
            if logs.compose.exemplars != "per_tenant":
                raise ConfigError("logs.intents needs logs.compose.exemplars: per_tenant (no cross-tenant lines in one document)", "logs.compose.exemplars")
        if kinds.incident:
            try:
                import networkx  # noqa: F401
            except ImportError:
                raise ConfigError("logs.compose.kinds.incident needs networkx (install the communities extra)", "logs.compose.kinds.incident") from None
        if self.profile == "logs_example" and self.access.policy != "none":
            raise ConfigError("the logs profile serves a single tenant (addendum A2 B4): access.policy must be none", "access.policy")
        if self.access.policy == "tenant":
            spec = self.metadata_schema.field(self.access.field)
            if spec is None or not spec.filterable:
                raise ConfigError(f"access.field {self.access.field!r} must be a declared filterable metadata field", "access.field")
        return self

    def capability_problem(self, backend_name: str, capability: str, flag: str | None = None) -> str | None:
        """Why ``backend_name`` cannot serve ``capability`` (and ``flag``), or ``None`` when it can."""
        backend = self.backends.get(backend_name)
        if backend is None:
            return f"unknown backend {backend_name!r}; declared: {sorted(self.backends)}"
        kind = backend.kind
        if not caps.declares(kind, capability):
            return f"backend {backend_name!r} ({kind}) does not declare capability {capability!r}"
        if caps.status_of(kind, capability) == "planned":
            return (
                f"backend {backend_name!r} ({kind}) declares capability {capability!r} in the matrix "
                "but no code serves it in this release (status: planned)"
            )
        if flag is not None and not caps.serves(kind, capability, flag):
            return f"backend {backend_name!r} ({kind}) serves {capability!r} without the required {flag!r} flag"
        return None

    def fingerprint(self) -> str:
        """Hash of the effective v2 config for traces/manifests only.

        It covers resolved absolute paths and inlined prompt text, so it differs per checkout. It is
        **not** an index identity: that is composed from component ``recipe_fingerprint()`` values.
        """
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def legacy_fingerprint(self) -> str:
        """v1 index fingerprint of the equivalent v1 config (``compat.config_v1.downgrade``)."""
        from ..compat.config_v1 import downgrade

        return downgrade(self).index_fingerprint()


def _path_of(error: dict[str, Any]) -> str:
    return ".".join(str(part) for part in error.get("loc", ())) or "<root>"


def validate_config(data: dict[str, Any]) -> Config:
    """Validate a v2 mapping (profile overlay applied first); pydantic errors become ``ConfigError``."""
    try:
        return Config.model_validate(apply_profile(data))
    except ValidationError as exc:
        errors = exc.errors()
        if len(errors) == 1 and isinstance((errors[0].get("ctx") or {}).get("error"), ConfigError):
            raise errors[0]["ctx"]["error"] from None
        problems = [_diagnostic(error) for error in errors]
        if len(problems) == 1:
            raise ConfigError(problems[0].message, problems[0].path, diagnostics=problems) from None
        listed = "; ".join(f"{d.path}: {d.message}" for d in problems)
        raise ConfigError(f"{len(problems)} configuration errors: {listed}", problems[0].path, diagnostics=problems) from None


def _diagnostic(error: dict[str, Any]):
    """One pydantic error as a ``Diagnostic`` (``unknown key`` for a forbidden extra key)."""
    from .component import Diagnostic

    original = (error.get("ctx") or {}).get("error")
    if isinstance(original, ConfigError):
        return Diagnostic(severity="error", code="invalid", message=str(original).removeprefix(f"{original.path}: "), path=original.path or _path_of(error))
    message = error.get("msg", "invalid").removeprefix("Value error, ")
    code = error.get("type", "invalid")
    if code == "extra_forbidden":
        message, code = "unknown key", "unknown_key"
    return Diagnostic(severity="error", code=code, message=message or "invalid", path=_path_of(error))


def _resolve_paths(config: Config, base: Path) -> Config:
    for name, backend in config.backends.items():
        if isinstance(backend, SqliteBackend):
            backend.path = (base / backend.path.expanduser()).resolve()
        if isinstance(backend, KuzuBackend):
            backend.path = (base / backend.path.expanduser()).resolve()
            if backend.attach is not None:
                backend.attach.mapping = (base / backend.attach.mapping.expanduser()).resolve()
    for pipeline in config.all_pipelines().values():
        for stage in (pipeline.decision, pipeline.generation):
            if stage.prompt_file:
                stage.prompt_file = (base / stage.prompt_file).resolve()
                stage.prompt = stage.prompt_file.read_text()
        if pipeline.arms.exact_id and pipeline.arms.exact_id.alias_map:
            pipeline.arms.exact_id.alias_map = (base / pipeline.arms.exact_id.alias_map).resolve()
    if config.budgets.price_table:
        config.budgets.price_table = (base / config.budgets.price_table).resolve()
    for source in config.sources.values():
        if source.ca_bundle is not None:
            source.ca_bundle = (base / source.ca_bundle.expanduser()).resolve()
    if config.extraction.pattern_library is not None:
        library = config.extraction.pattern_library
        library.path = (base / library.path.expanduser()).resolve()
    if config.learning.patterns is not None and config.learning.patterns.spend_ledger is not None:
        config.learning.patterns.spend_ledger = (base / config.learning.patterns.spend_ledger.expanduser()).resolve()
    if config.learning.patterns is not None and config.learning.patterns.stop_file is not None:
        config.learning.patterns.stop_file = (base / config.learning.patterns.stop_file.expanduser()).resolve()
    if config.serving.answers.ledger is not None:
        config.serving.answers.ledger = (base / config.serving.answers.ledger.expanduser()).resolve()
    if config.serving.ui.theme is not None:  # relative to the config file, like every other path
        config.serving.ui.theme = (base / config.serving.ui.theme.expanduser()).resolve()
    if config.learning.extraction is not None:
        for name in ("spend_ledger", "reply_cache", "stop_file"):
            value = getattr(config.learning.extraction, name)
            if value is not None:
                setattr(config.learning.extraction, name, (base / value.expanduser()).resolve())
    if config.logs is not None:
        config.logs.partitions.path = (base / config.logs.partitions.path.expanduser()).resolve()
        if config.logs.partitions.raw.path is not None:
            config.logs.partitions.raw.path = (base / config.logs.partitions.raw.path.expanduser()).resolve()
        if config.logs.intents.few_shot_file is not None:
            config.logs.intents.few_shot_file = (base / config.logs.intents.few_shot_file.expanduser()).resolve()
            config.logs.intents.examples()  # read and validated at load (ConfigError naming the file)
    embedder = config.models.roles.embedder
    if embedder is not None and embedder.precomputed is not None:
        embedder.precomputed.vectors = (base / embedder.precomputed.vectors.expanduser()).resolve()
        embedder.precomputed.keys = (base / embedder.precomputed.keys.expanduser()).resolve()
    return config


def _path_slots(data: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    """``(mapping, key, dotted path)`` of every file-path value in a v2 mapping (the same set as
    ``_resolve_paths``)."""
    slots: list[tuple[dict[str, Any], str, str]] = []

    def add(mapping: Any, key: str, path: str) -> None:
        if isinstance(mapping, dict) and isinstance(mapping.get(key), str):
            slots.append((mapping, key, path))

    for name, backend in (data.get("backends") or {}).items():
        if isinstance(backend, dict) and backend.get("kind") in {"sqlite", "kuzu"}:
            add(backend, "path", f"backends.{name}.path")
        if isinstance(backend, dict) and backend.get("kind") == "kuzu":
            add(backend.get("attach"), "mapping", f"backends.{name}.attach.mapping")
    pipelines = [("pipeline", data.get("pipeline"))] + [(f"pipelines.{n}", p) for n, p in (data.get("pipelines") or {}).items()]
    for prefix, pipeline in pipelines:
        if not isinstance(pipeline, dict):
            continue
        for stage in ("decision", "generation"):
            add(pipeline.get(stage), "prompt_file", f"{prefix}.{stage}.prompt_file")
        add((pipeline.get("arms") or {}).get("exact_id"), "alias_map", f"{prefix}.arms.exact_id.alias_map")
    add(data.get("budgets"), "price_table", "budgets.price_table")
    for name, source in (data.get("sources") or {}).items():
        add(source, "ca_bundle", f"sources.{name}.ca_bundle")
    add((data.get("extraction") or {}).get("pattern_library"), "path", "extraction.pattern_library.path")
    add((data.get("learning") or {}).get("patterns"), "spend_ledger", "learning.patterns.spend_ledger")
    add((data.get("learning") or {}).get("patterns"), "stop_file", "learning.patterns.stop_file")
    add((data.get("learning") or {}).get("extraction"), "spend_ledger", "learning.extraction.spend_ledger")
    add((data.get("learning") or {}).get("extraction"), "reply_cache", "learning.extraction.reply_cache")
    add((data.get("learning") or {}).get("extraction"), "stop_file", "learning.extraction.stop_file")
    add((data.get("serving") or {}).get("answers"), "ledger", "serving.answers.ledger")
    add((data.get("serving") or {}).get("ui"), "theme", "serving.ui.theme")
    add((data.get("logs") or {}).get("partitions"), "path", "logs.partitions.path")
    add(((data.get("logs") or {}).get("partitions") or {}).get("raw"), "path", "logs.partitions.raw.path")
    add(((data.get("logs") or {}).get("intents") or {}), "few_shot_file", "logs.intents.few_shot_file")
    embedder = ((data.get("models") or {}).get("roles") or {}).get("embedder")
    precomputed = embedder.get("precomputed") if isinstance(embedder, dict) else None
    add(precomputed, "vectors", "models.roles.embedder.precomputed.vectors")
    add(precomputed, "keys", "models.roles.embedder.precomputed.keys")
    return slots


def rebase_paths(data: dict[str, Any], source_dir: Path, target_dir: Path) -> list[str]:
    """Rewrite relative file paths of a v2 mapping written next to ``source_dir``'s config so they
    name the same files from a config written in ``target_dir`` (paths are relative to the YAML
    file). Absolute and ``~`` paths are kept. Returns the dotted keys that changed (QA-10)."""
    source_dir, target_dir = source_dir.resolve(), target_dir.resolve()
    changed: list[str] = []
    for mapping, key, dotted in _path_slots(data):
        value = mapping[key]
        if value.startswith("~") or Path(value).is_absolute():
            continue
        rebased = Path(os.path.relpath(source_dir / value, target_dir)).as_posix()
        if rebased != Path(value).as_posix():
            mapping[key] = rebased
            changed.append(dotted)
    return changed


def load_config(path: str | Path, *, check_plugins: bool = True, warn: bool = True) -> Config:
    """Load a v2 YAML file, or a v1 file through the deterministic upgrade (with a warning)."""
    path = Path(path).expanduser().resolve()
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a mapping", "<root>")
    version = data.get("schema_version", 1)
    if version == 1:
        from ..compat.config_v1 import upgrade_mapping

        result = upgrade_mapping(data)
        if warn:
            warnings.warn(
                f"{path.name} is a schema_version 1 config; upgraded in memory. "
                f"Run `config upgrade` to write v2. Changes: {'; '.join(result.changes)}",
                stacklevel=2,
            )
        config = result.config
    elif version == 2:
        config = validate_config(data)
    else:
        raise ConfigError(f"unsupported schema_version {version!r}", "schema_version")
    if check_plugins and config.plugins:
        from .registry import check_plugins_registered

        check_plugins_registered(config.plugins)
    return _resolve_paths(config, path.parent)


def secret(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value
