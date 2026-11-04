"""Declarative configuration v2 (plan §3.3). ``schema_version: 2``.

Top-level keys: ``dataset``, ``profile``, ``plugins``, ``backends`` (named instances), ``storage``
(authoritative backend per non-arm capability), ``metadata_schema``, ``models.roles``, ``pipeline``
(decision, arms, selection, review, guards, generation), ``extraction``, ``communities``,
``ingestion``, ``sessions``, ``budgets``, ``privacy``, ``serving``.

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
from pydantic import Field, ValidationError, field_validator, model_validator

from ..backends import capabilities as caps
from ..models import StrictModel
from ..profiles import apply_profile, available_profiles
from .component import scan_for_code_references
from .config_decision import StagePolicies
from .config_selection import OrderingKind, PackingConfig
from .errors import ConfigError
from .request import RequestConfig
from .types import MetadataSchema

IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"
SQL_IDENTIFIER = r"^[a-z][a-z0-9_]{0,40}$"
# ``structured_output: native`` and ``prompt_caching`` are checked against the per-(provider,
# model) registry in ``providers/capabilities.py`` (imported lazily in ``ChatRole.coherent``).
# Built-in guards available in this release (Phase 2 ships the first ones).
BUILTIN_GUARDS: frozenset[str] = frozenset()
# Built-in read-only tools and write actions (``runtime/builtin_tools.py``, ``builtin_actions.py``).
BUILTIN_TOOLS: tuple[str, ...] = ("search", "get_document", "list_values", "aggregate")
BUILTIN_ACTIONS: tuple[str, ...] = ("ingest_delta", "invalidate_cache")
DEFAULT_PIPELINE = "default"
AVAILABLE_PROFILES: frozenset[str] = available_profiles()
# Keys whose string values are free text or patterns, exempt from the code-reference scan.
SCAN_EXEMPT_KEYS: frozenset[str] = frozenset({"prompt", "patterns", "gazetteer", "predicate_triggers", "description"})


# --- backends ----------------------------------------------------------------------------------


class SqliteBackend(StrictModel):
    kind: Literal["sqlite"]
    path: Path = Path("state")
    vector_engine: Literal["numpy", "faiss"] = "numpy"
    timeout_seconds: float = Field(default=30, gt=0, le=600)


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


class LuceneBackend(StrictModel):
    kind: Literal["opensearch", "elasticsearch"]
    hosts_env: str
    index_prefix: str = Field(default="su_multi_arm", pattern=SQL_IDENTIFIER)
    knn: KnnConfig = Field(default_factory=KnnConfig)
    timeout_seconds: float = Field(default=30, gt=0, le=600)


class Neo4jBackend(StrictModel):
    kind: Literal["neo4j"]
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password_env: str = "SU_NEO4J_PASSWORD"
    database: str = "neo4j"
    # Generation keys are ``<namespace>|<dataset>|<generation>``; distinct namespaces never touch each other.
    namespace: str = Field(default="su_multi_arm", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    timeout_seconds: float = Field(default=30, gt=0, le=600)


class MongoBackend(StrictModel):
    kind: Literal["mongodb"]
    uri_env: str
    database: str
    collection_prefix: str = Field(default="su_multi_arm", pattern=SQL_IDENTIFIER)
    timeout_seconds: float = Field(default=30, gt=0, le=600)


BackendConfig = Annotated[
    Union[SqliteBackend, MemoryBackendConfig, PostgresBackend, SqlAlchemyBackend, LuceneBackend, Neo4jBackend, MongoBackend],
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

    ``structured_output``: ``none`` (default; schema in the system prompt, strict validation of the
    reply), ``json_mode`` (``response_format: json_object``; OpenAI-style providers only),
    ``json_schema`` (``response_format: json_schema``, ``strict: false``; OpenAI-style providers
    only), ``native`` (provider-enforced: Anthropic forced tool use, OpenAI strict json_schema; only
    for registry-declared ``(provider, model)`` pairs). ``prompt_caching``: ``controlled`` sends
    ``cache_control`` (Anthropic); ``passive`` records cached tokens the provider reports.
    """

    structured_output: Literal["none", "json_mode", "json_schema", "native"] = "none"
    prompt_caching: Literal["none", "passive", "controlled"] = "none"
    streaming: bool = False


class EvidenceBudget(StrictModel):
    chars: int = Field(default=60000, ge=500, le=500000)


class ChatRole(StrictModel):
    provider: Literal["openai_compatible", "openai", "anthropic"]
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str | None = None
    max_tokens: int = Field(default=2048, ge=128, le=64000)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    # Output-limit parameter name for ``openai_compatible`` servers; hosted providers use the
    # registry's name (``openai`` must match it, ``anthropic`` always sends ``max_tokens``).
    max_tokens_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    capabilities: CapabilityDeclaration = Field(default_factory=CapabilityDeclaration)
    evidence_budget: EvidenceBudget | None = None
    pricing_key: str | None = None  # key into budgets.price_table; ``price_key()`` falls back to the model id

    def price_key(self) -> str:
        """What the engine passes to ``Budget.reserve_call``; the documented default is the model id."""
        return self.pricing_key or self.model

    @model_validator(mode="after")
    def coherent(self):
        from ..providers.capabilities import check_role

        if self.provider == "anthropic" and not self.api_key_env:
            raise ValueError("Anthropic requires api_key_env")
        if self.provider == "openai" and not self.api_key_env:
            raise ValueError("OpenAI requires api_key_env")
        problems = check_role(
            self.provider,
            self.model,
            self.capabilities.structured_output,
            self.capabilities.prompt_caching,
            self.reasoning_effort,
            self.max_tokens_parameter if self.provider == "openai" else None,
        )
        if problems:
            raise ValueError("; ".join(f"{key}: {message}" for key, message in problems))
        return self


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
    embedder: EmbedderRole = Field(default_factory=EmbedderRole)
    reranker: RerankerRole | None = None

    @model_validator(mode="before")
    @classmethod
    def unimplemented_roles(cls, data):
        return reject_unimplemented(data, UNIMPLEMENTED_ROLES, "models.roles")

    def chat_roles(self) -> dict[str, ChatRole]:
        names = ("planner", "reviewer", "extractor", "generator", "community")
        return {n: getattr(self, n) for n in names if getattr(self, n) is not None}


class ModelsConfig(StrictModel):
    roles: ModelRoles = Field(default_factory=ModelRoles)


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


class LexicalArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    candidates: int = Field(default=200, ge=1, le=10000)
    phrase_boost: PhraseBoost = Field(default_factory=PhraseBoost)


class StructuredArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)


class GraphArm(StrictModel):
    backend: str = Field(pattern=IDENTIFIER)
    hops: int = Field(default=1, ge=0, le=3)
    predicates: list[str] = Field(default_factory=list)
    direction: Literal["out", "in", "any"] = "any"


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
    # abstain_with_reason: abstain (a non-narrative claim on an answer whose text states no
    # count/list/absence is stripped instead); strip: also remove unbacked sentences; fail: raise.
    on_violation: Literal["abstain_with_reason", "strip", "fail"] = "abstain_with_reason"


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


class ExtractionConfig(StrictModel):
    extractors: list[Literal["gazetteer", "regex", "spacy", "gliner", "llm"]] = Field(default_factory=lambda: ["gazetteer"])
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
        return self


class CommunitiesConfig(StrictModel):
    strategy: Literal["entity", "louvain"] = "entity"
    min_documents: int = Field(default=2, ge=1)
    seed: int = 42
    prompt: str | None = None


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


class BudgetsConfig(StrictModel):
    max_llm_calls: int = Field(default=12, ge=1, le=1000000)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_usd: float | None = Field(default=None, gt=0)
    deadline_seconds: float = Field(default=60, gt=0, le=3600)
    price_table: Path | None = None
    session: SessionBudget | None = None  # per-session limits on top of the per-request ones above
    ingest: IngestBudget = Field(default_factory=IngestBudget)  # whole-build limits of the write path

    @model_validator(mode="after")
    def usd_needs_prices(self):
        if self.max_usd is not None and self.price_table is None:
            raise ValueError("max_usd requires price_table (operator-supplied; no bundled prices)")
        if self.ingest.max_usd is not None and self.price_table is None:
            raise ValueError("ingest.max_usd requires price_table (operator-supplied; no bundled prices)")
        if self.session is not None and self.session.max_usd is not None and self.price_table is None:
            raise ValueError("session.max_usd requires price_table (operator-supplied; no bundled prices)")
        return self


class PrivacyConfig(StrictModel):
    """Opt-in conservative redaction; not a complete PII detector."""

    redact_emails: bool = False
    patterns: dict[str, str] = Field(default_factory=dict)
    metadata_fields: list[str] = Field(default_factory=list)
    trace_masking: Literal["privacy", "raw"] = "privacy"


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


class ServingConfig(StrictModel):
    http: HttpConfig = Field(default_factory=HttpConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    actions: ActionsServingConfig = Field(default_factory=ActionsServingConfig)


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
    for pipeline in config.all_pipelines().values():
        for stage in (pipeline.decision, pipeline.generation):
            if stage.prompt_file:
                stage.prompt_file = (base / stage.prompt_file).resolve()
                stage.prompt = stage.prompt_file.read_text()
        if pipeline.arms.exact_id and pipeline.arms.exact_id.alias_map:
            pipeline.arms.exact_id.alias_map = (base / pipeline.arms.exact_id.alias_map).resolve()
    if config.budgets.price_table:
        config.budgets.price_table = (base / config.budgets.price_table).resolve()
    return config


def _path_slots(data: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    """``(mapping, key, dotted path)`` of every file-path value in a v2 mapping (the same set as
    ``_resolve_paths``)."""
    slots: list[tuple[dict[str, Any], str, str]] = []

    def add(mapping: Any, key: str, path: str) -> None:
        if isinstance(mapping, dict) and isinstance(mapping.get(key), str):
            slots.append((mapping, key, path))

    for name, backend in (data.get("backends") or {}).items():
        if isinstance(backend, dict) and backend.get("kind") == "sqlite":
            add(backend, "path", f"backends.{name}.path")
    pipelines = [("pipeline", data.get("pipeline"))] + [(f"pipelines.{n}", p) for n, p in (data.get("pipelines") or {}).items()]
    for prefix, pipeline in pipelines:
        if not isinstance(pipeline, dict):
            continue
        for stage in ("decision", "generation"):
            add(pipeline.get(stage), "prompt_file", f"{prefix}.{stage}.prompt_file")
        add((pipeline.get("arms") or {}).get("exact_id"), "alias_map", f"{prefix}.arms.exact_id.alias_map")
    add(data.get("budgets"), "price_table", "budgets.price_table")
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
