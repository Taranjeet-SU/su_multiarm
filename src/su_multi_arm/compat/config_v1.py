"""v1 configuration model (``schema_version: 1``) kept for the compatibility window (plan §5).

The v1 ``Config`` and its hand-listed ``index_fingerprint()`` stay here so legacy-read can verify a
v1 generation. ``upgrade()`` maps a v1 config to v2 deterministically (mapping table below) and
``downgrade()`` maps the v2 subset that v1 can express back, so ``upgrade(v1) -> downgrade`` yields
the same v1 fingerprint (N6). v1 acceptance is kept for two minor releases, then removed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator
from ..models import StrictModel


class EmbeddingConfig(StrictModel):
    provider: Literal["hash", "sentence_transformers", "openai_compatible"] = "hash"
    model: str = "hash-demo-v1"
    dimensions: int = Field(default=256, ge=16, le=65536)
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str | None = None
    batch_size: int = Field(default=32, ge=1, le=1024)
    query_prefix: str = ""
    document_prefix: str = ""
    revision: str | None = None
    device: str = "cpu"


class ModelConfig(StrictModel):
    provider: Literal["none", "openai_compatible", "anthropic"] = "none"
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    max_tokens: int = Field(default=2048, ge=128, le=64000)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_tokens_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"

    @model_validator(mode="after")
    def model_required(self):
        if self.provider != "none" and not self.model:
            raise ValueError("model is required for a remote provider")
        if self.provider == "anthropic" and not self.api_key_env:
            raise ValueError("Anthropic requires api_key_env")
        return self


class GroundingConfig(StrictModel):
    """How a model quote is located in its source. The stored span is always verbatim source text."""

    normalisation: list[Literal["nfkc", "casefold", "whitespace"]] = Field(
        default_factory=lambda: ["nfkc", "casefold", "whitespace"]
    )
    ambiguous: Literal["reject", "first"] = "reject"


class ExtractionConfig(StrictModel):
    providers: list[Literal["tags", "regex", "spacy", "gliner", "llm"]] = Field(
        default_factory=lambda: ["tags"]
    )
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    # Extra phrases that evidence a predicate inside its span, e.g. {"hosted": ["held in", "venue"]}.
    predicate_triggers: dict[str, list[str]] = Field(default_factory=dict)
    stemmer_language: str = "english"
    tags: dict[str, list[str]] = Field(default_factory=dict)
    patterns: dict[str, str] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=lambda: ["product", "country", "state", "county", "error"])
    spacy_model: str = "en_core_web_sm"
    pos: list[str] = Field(default_factory=lambda: ["PROPN"])
    gliner_model: str = "urchade/gliner_small-v2.1"
    threshold: float = Field(default=0.5, ge=0, le=1)
    model: ModelConfig = Field(default_factory=ModelConfig)

    @model_validator(mode="after")
    def known_stemmer(self):
        from ..predicate_support import languages

        if self.stemmer_language not in languages():
            raise ValueError(f"unknown stemmer_language; choose one of {languages()}")
        return self


class PrivacyConfig(StrictModel):
    """Opt-in conservative redaction; not a complete PII detector."""

    redact_emails: bool = False
    patterns: dict[str, str] = Field(default_factory=dict)
    # Metadata IDs, dates, technical field names and URLs are not guessed to be PII.
    metadata_fields: list[str] = Field(default_factory=list)


class InputMapping(StrictModel):
    id: str = "id"
    title: str = "title"
    source: str | None = "source"
    source_value: str = "article"
    url: str | None = "url"
    fields: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)


class PostgresConfig(StrictModel):
    dsn_env: str = "SU_POSTGRES_DSN"
    table_prefix: str = "su_multi_arm"

    @model_validator(mode="after")
    def identifier(self):
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", self.table_prefix):
            raise ValueError("table_prefix must be a lowercase SQL identifier, at most 41 characters")
        return self


class Neo4jConfig(StrictModel):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password_env: str = "SU_NEO4J_PASSWORD"
    database: str = "neo4j"


class ArmsConfig(StrictModel):
    enabled: list[Literal["dense", "lexical", "sql", "graph", "community"]] = Field(
        default_factory=lambda: ["dense", "lexical", "sql", "graph", "community"]
    )
    dense: Literal["numpy", "faiss", "pgvector"] = "numpy"
    lexical: Literal["sqlite", "postgres"] = "sqlite"
    graph: Literal["sqlite", "neo4j"] = "sqlite"
    per_arm: int = Field(default=20, ge=1, le=1000)
    concurrency: int = Field(default=5, ge=1, le=32)
    graph_hops: int = Field(default=1, ge=0, le=3)
    sql_fields: list[str] = Field(
        default_factory=lambda: ["source", "product", "status", "country", "state", "county", "created_at"]
    )
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    neo4j: Neo4jConfig = Field(default_factory=Neo4jConfig)


class FusionConfig(StrictModel):
    strategy: Literal["per_arm", "rrf", "minmax", "zscore"] = "per_arm"
    rrf_k: int = Field(default=60, ge=1)
    source_weights: dict[str, float] = Field(default_factory=dict)
    arm_weights: dict[str, float] = Field(default_factory=dict)
    max_documents: int | None = Field(default=None, ge=1)
    reranker_model: str | None = None

    @model_validator(mode="after")
    def positive_weights(self):
        import math

        if any(
            not math.isfinite(v) or v <= 0
            for v in [*self.source_weights.values(), *self.arm_weights.values()]
        ):
            raise ValueError("fusion weights must be finite and positive")
        return self


class RoutingConfig(StrictModel):
    mode: Literal["raw", "planned", "agentic"] = "raw"
    model: ModelConfig = Field(default_factory=ModelConfig)
    prompt: str | None = None
    prompt_file: Path | None = None
    max_steps: int = Field(default=10, ge=1, le=20)
    max_hops: int = Field(default=2, ge=1, le=5)
    reviewer: ModelConfig = Field(default_factory=ModelConfig)


class GenerationConfig(StrictModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    prompt: str | None = None
    prompt_file: Path | None = None
    evidence_char_budget: int = Field(default=60000, ge=500, le=500000)


class CommunityConfig(StrictModel):
    strategy: Literal["entity", "louvain"] = "entity"
    min_documents: int = Field(default=2, ge=1)
    seed: int = 42
    model: ModelConfig = Field(default_factory=ModelConfig)
    evidence_char_budget: int = Field(default=30000, ge=500, le=500000)
    prompt: str | None = None


class Config(StrictModel):
    schema_version: Literal[1] = 1
    dataset: str = Field(default="default", min_length=1)
    storage_path: Path = Path("state")
    chunk_size: int = Field(default=1600, ge=100, le=16000)
    chunk_overlap: int = Field(default=160, ge=0)
    field_order: list[str] = Field(
        default_factory=lambda: ["issue", "details", "description", "resolution", "body"]
    )
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    input_mapping: InputMapping | None = None
    arms: ArmsConfig = Field(default_factory=ArmsConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    community: CommunityConfig = Field(default_factory=CommunityConfig)
    max_llm_calls: int = Field(default=12, ge=1, le=1000000)

    @model_validator(mode="after")
    def coherent(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if len(set(self.arms.enabled)) != len(self.arms.enabled) or not self.arms.enabled:
            raise ValueError("arms.enabled must be unique and nonempty")
        if self.routing.mode != "raw" and self.routing.model.provider == "none":
            raise ValueError("planned/agentic mode requires a router model")
        if self.routing.mode == "agentic" and self.routing.reviewer.provider == "none":
            raise ValueError("agentic mode requires a reviewer model")
        if "llm" in self.extraction.providers and self.extraction.model.provider == "none":
            raise ValueError("LLM extraction requires an extraction model")
        return self

    def index_fingerprint(self):
        # Retrieval ordering/prompts can change without re-embedding. Extraction/backends cannot.
        data = self.model_dump(
            mode="json",
            include={
                "dataset",
                "chunk_size",
                "chunk_overlap",
                "field_order",
                "embedding",
                "extraction",
                "community",
                "privacy",
            },
        )
        data["backends"] = {k: getattr(self.arms, k) for k in ("dense", "lexical", "graph")}
        data["index_format"] = 3
        data["postgres"] = self.arms.postgres.model_dump()
        data["neo4j"] = self.arms.neo4j.model_dump()
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# --- v1 -> v2 upgrade (plan §5 mapping table) -------------------------------------------------

from dataclasses import dataclass, field as dc_field  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402

from ..core.errors import ConfigError  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ..core.config import Config as ConfigV2

#: Deterministic mapping table (v1 key -> v2 key). Also the source of the upgrade change report.
MAPPING: tuple[tuple[str, str], ...] = (
    ("schema_version: 1", "schema_version: 2"),
    ("dataset (v1 built-in support conventions)", "profile: support_example (context keys, prompts, evidence field order, labels)"),
    ("storage_path", "backends.local.path (kind: sqlite); storage.* -> local"),
    ("arms.dense numpy|faiss", "backends.local.vector_engine; pipeline.arms.dense.backend: local"),
    ("arms.dense pgvector", "backends.pg (kind: postgres); pipeline.arms.dense.backend: pg"),
    ("arms.lexical sqlite|postgres", "pipeline.arms.lexical.backend: local|pg"),
    ("arms.graph sqlite|neo4j", "pipeline.arms.graph.backend: local|graph (kind: neo4j)"),
    ("arms.enabled", "pipeline.arms.<name> present (sql -> structured)"),
    ("arms.per_arm", "pipeline.selection.per_arm"),
    ("arms.concurrency", "pipeline.concurrency"),
    ("arms.graph_hops", "pipeline.arms.graph.hops"),
    ("arms.sql_fields", "metadata_schema.fields (type: string, filterable; declare real types)"),
    ("routing.mode raw", "pipeline.decision {kind: rules}"),
    ("routing.mode planned", "pipeline.decision {kind: llm}; pipeline.review.enabled: false"),
    ("routing.mode agentic", "pipeline.decision {kind: llm}; pipeline.review {enabled: true, max_hops}"),
    ("routing.model / routing.reviewer", "models.roles.planner / models.roles.reviewer"),
    ("routing.max_steps / prompt", "pipeline.decision.max_steps / prompt"),
    ("fusion.strategy", "pipeline.selection.ordering (per_arm -> reciprocal_rank)"),
    ("fusion.* weights / max_documents", "pipeline.selection.*"),
    ("fusion.reranker_model", "models.roles.reranker; pipeline.selection.rerank: true"),
    ("embedding", "models.roles.embedder"),
    ("extraction.providers", "extraction.extractors (tags -> gazetteer)"),
    ("extraction.tags", "extraction.gazetteer"),
    ("extraction.model", "models.roles.extractor"),
    ("generation.model / evidence_char_budget", "models.roles.generator (+ evidence_budget.chars)"),
    ("generation.prompt", "pipeline.generation.prompt"),
    ("community.model / evidence_char_budget", "models.roles.community (+ evidence_budget.chars)"),
    ("community.strategy / min_documents / seed / prompt", "communities.*"),
    ("chunk_size / chunk_overlap / field_order", "ingestion.chunking.size / overlap / field_order"),
    ("input_mapping", "ingestion.input_mapping"),
    ("max_llm_calls", "budgets.max_llm_calls"),
    ("privacy", "privacy"),
)
# v1 ``per_arm`` is the v2 default ``reciprocal_rank`` (plan §3.9); ``rrf`` keeps its own formula.
ORDERING = {"per_arm": "reciprocal_rank", "rrf": "rrf", "minmax": "minmax", "zscore": "zscore"}
ORDERING_BACK = {v: k for k, v in ORDERING.items()} | {"per_arm": "per_arm"}
V1_ARMS = ("dense", "lexical", "sql", "graph", "community")


@dataclass
class UpgradeResult:
    config: Any  # core.config.Config
    data: dict[str, Any]  # v2 mapping as written by ``config upgrade``
    warnings: list[str] = dc_field(default_factory=list)  # messages of the warning diagnostics
    changes: list[str] = dc_field(default_factory=list)
    diagnostics: list[Any] = dc_field(default_factory=list)  # core.component.Diagnostic

    @property
    def lossy(self) -> bool:
        return any(d.code == "lossy_upgrade" for d in self.diagnostics)


def lossy_fields(v1: Config) -> list[tuple[str, str]]:
    """v1 settings that v2 cannot carry: model blocks with ``provider: none`` but non-default fields,
    and evidence budgets of stages whose model is ``none``. They had no effect in v1, but
    ``community`` and ``extraction`` ones are part of the v1 ``index_fingerprint()``."""
    found = []
    for path, model in (
        ("routing.model", v1.routing.model),
        ("routing.reviewer", v1.routing.reviewer),
        ("extraction.model", v1.extraction.model),
        ("generation.model", v1.generation.model),
        ("community.model", v1.community.model),
    ):
        if model.provider == "none" and model != ModelConfig():
            fingerprint = path.startswith(("extraction", "community"))
            found.append((path, "non-default fields on a provider: none model block" + (" (changes the v1 fingerprint)" if fingerprint else "")))
    if v1.generation.model.provider == "none" and v1.generation.evidence_char_budget != GenerationConfig().evidence_char_budget:
        found.append(("generation.evidence_char_budget", "set without a generation model"))
    if v1.community.model.provider == "none" and v1.community.evidence_char_budget != CommunityConfig().evidence_char_budget:
        found.append(("community.evidence_char_budget", "set without a community model (changes the v1 fingerprint)"))
    return found


def _role(model: ModelConfig, evidence_chars: int | None = None) -> dict[str, Any] | None:
    if model.provider == "none":
        return None
    role: dict[str, Any] = {
        "provider": model.provider,
        "model": model.model,
        "base_url": model.base_url,
        "api_key_env": model.api_key_env,
        "max_tokens": model.max_tokens,
        "timeout_seconds": model.timeout_seconds,
        "max_tokens_parameter": model.max_tokens_parameter,
    }
    if evidence_chars is not None:
        role["evidence_budget"] = {"chars": evidence_chars}
    return role


def _model(role: Any) -> ModelConfig:
    if role is None:
        return ModelConfig()
    return ModelConfig(
        provider=role.provider,
        model=role.model,
        base_url=role.base_url,
        api_key_env=role.api_key_env,
        max_tokens=role.max_tokens,
        timeout_seconds=role.timeout_seconds,
        max_tokens_parameter=role.max_tokens_parameter,
    )


def _prompts(stage: Any) -> dict[str, Any]:
    # A loaded v1 config has prompt_file already inlined into prompt; carry one of them. An explicit
    # ``prompt: null`` next to ``prompt_file`` keeps the profile overlay from supplying a second prompt.
    if stage.prompt:
        return {"prompt": stage.prompt}
    if stage.prompt_file:
        return {"prompt": None, "prompt_file": str(stage.prompt_file)}
    return {}


def _overlay_prompt(section: str, key: str) -> str | None:
    """The prompt the ``support_example`` overlay supplies for a stage (v1's built-in prompt)."""
    from ..profiles import load_profile

    node: Any = load_profile("support_example")
    for part in (*section.split("."), key):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _v1_prompt(value: str | None, section: str, key: str = "prompt") -> str | None:
    """Inverse of the overlay: a prompt equal to v1's built-in (the overlay text) was ``None`` in v1."""
    return None if value is not None and value == _overlay_prompt(section, key) else value


def upgrade(v1: Config, present_keys: set[str] | None = None, *, strict: bool = False) -> UpgradeResult:
    """Map a v1 ``Config`` to a validated v2 ``Config``. Pure and deterministic.

    ``present_keys`` (top-level keys of the source file) limits the change report to what the file
    actually set; by default every mapping row is reported. Settings v2 cannot carry produce a
    ``lossy_upgrade`` warning diagnostic naming the v1 key; with ``strict=True`` they are a
    ``ConfigError`` instead. Nothing is dropped silently.
    """
    from ..core.component import Diagnostic
    from ..core.config import validate_config

    diagnostics: list[Diagnostic] = []
    for path, why in lossy_fields(v1):
        if strict:
            raise ConfigError(f"lossy upgrade: {why}; remove the setting or upgrade without strict", path)
        diagnostics.append(Diagnostic(severity="warning", code="lossy_upgrade", message=f"dropped: {why}", path=path))
    backends: dict[str, Any] = {
        "local": {
            "kind": "sqlite",
            "path": str(v1.storage_path),
            "vector_engine": v1.arms.dense if v1.arms.dense in {"numpy", "faiss"} else "numpy",
        }
    }
    uses_pg = v1.arms.dense == "pgvector" or v1.arms.lexical == "postgres"
    if uses_pg or v1.arms.postgres != PostgresConfig():
        backends["pg"] = {"kind": "postgres", "dsn_env": v1.arms.postgres.dsn_env, "table_prefix": v1.arms.postgres.table_prefix}
    if v1.arms.graph == "neo4j" or v1.arms.neo4j != Neo4jConfig():
        backends["graph"] = {"kind": "neo4j", **v1.arms.neo4j.model_dump()}
    arms: dict[str, Any] = {}
    for arm in V1_ARMS:
        if arm not in v1.arms.enabled:
            continue
        if arm == "dense":
            arms["dense"] = {"backend": "pg" if v1.arms.dense == "pgvector" else "local"}
        elif arm == "lexical":
            arms["lexical"] = {"backend": "pg" if v1.arms.lexical == "postgres" else "local"}
        elif arm == "sql":
            arms["structured"] = {"backend": "local"}
        elif arm == "graph":
            arms["graph"] = {"backend": "graph" if v1.arms.graph == "neo4j" else "local", "hops": v1.arms.graph_hops}
        else:
            arms["community"] = {"backend": "local"}
    fields = []
    for name in v1.arms.sql_fields:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ConfigError(f"sql field {name!r} is not a valid metadata field name", "arms.sql_fields")
        fields.append({"name": name, "type": "string", "filterable": True, "groupable": True, "sortable": True})
    if fields:
        diagnostics.append(
            Diagnostic(
                severity="warning",
                code="untyped_fields",
                path="arms.sql_fields",
                message=(
                    "metadata_schema.fields were derived from arms.sql_fields as type string; declare real types "
                    "(datetime, enum, integer...) to enable typed scope and date measures"
                ),
            )
        )
    roles: dict[str, Any] = {"embedder": v1.embedding.model_dump(mode="json")}
    for name, role in (
        ("planner", _role(v1.routing.model)),
        ("reviewer", _role(v1.routing.reviewer)),
        ("extractor", _role(v1.extraction.model)),
        ("generator", _role(v1.generation.model, v1.generation.evidence_char_budget)),
        ("community", _role(v1.community.model, v1.community.evidence_char_budget)),
    ):
        if role is not None:
            roles[name] = role
    if v1.fusion.reranker_model:
        roles["reranker"] = {"provider": "sentence_transformers", "model": v1.fusion.reranker_model}
    decision: dict[str, Any] = {"kind": "rules" if v1.routing.mode == "raw" else "llm", "max_steps": v1.routing.max_steps}
    decision.update(_prompts(v1.routing))
    data: dict[str, Any] = {
        "schema_version": 2,
        "dataset": v1.dataset,
        # v1 hardcoded the support-domain conventions (context keys, prompts, evidence order); the
        # faithful upgrade keeps them as the example overlay. Explicit v1 values still win over it.
        "profile": "support_example",
        "backends": backends,
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": fields},
        "models": {"roles": roles},
        "pipeline": {
            "decision": decision,
            "arms": arms,
            "selection": {
                "per_arm": v1.arms.per_arm,
                "ordering": ORDERING[v1.fusion.strategy],
                "rrf_k": v1.fusion.rrf_k,
                "source_weights": v1.fusion.source_weights,
                "arm_weights": v1.fusion.arm_weights,
                "max_documents": v1.fusion.max_documents,
                "rerank": bool(v1.fusion.reranker_model),
            },
            "review": {"enabled": v1.routing.mode == "agentic", "max_hops": v1.routing.max_hops},
            "generation": _prompts(v1.generation),
            "concurrency": v1.arms.concurrency,
        },
        "extraction": {
            "extractors": ["gazetteer" if p == "tags" else p for p in v1.extraction.providers],
            "grounding": v1.extraction.grounding.model_dump(),
            "predicate_triggers": v1.extraction.predicate_triggers,
            "stemmer_language": v1.extraction.stemmer_language,
            "gazetteer": v1.extraction.tags,
            "patterns": v1.extraction.patterns,
            "labels": v1.extraction.labels,
            "spacy_model": v1.extraction.spacy_model,
            "pos": v1.extraction.pos,
            "gliner_model": v1.extraction.gliner_model,
            "threshold": v1.extraction.threshold,
        },
        "communities": {
            "strategy": v1.community.strategy,
            "min_documents": v1.community.min_documents,
            "seed": v1.community.seed,
            **({"prompt": v1.community.prompt} if v1.community.prompt else {}),  # unset -> overlay prompt
        },
        "ingestion": {
            "chunking": {"size": v1.chunk_size, "overlap": v1.chunk_overlap, "field_order": v1.field_order},
            "input_mapping": None if v1.input_mapping is None else v1.input_mapping.model_dump(),
        },
        "budgets": {"max_llm_calls": v1.max_llm_calls},
        "privacy": v1.privacy.model_dump(),
    }
    config = validate_config(data)
    # Canonical, minimal document: defaults omitted, schema_version always first.
    minimal = {"schema_version": 2, **config.model_dump(mode="json", exclude_defaults=True)}
    minimal["backends"]["local"]["path"] = str(v1.storage_path)  # always explicit in the written file
    changes = [
        f"{old} -> {new}"
        for old, new in MAPPING
        if present_keys is None or old.split(" ")[0].split(".")[0].rstrip(":") in present_keys
    ]
    warnings_ = [f"{d.path}: {d.message}" if d.path else d.message for d in diagnostics]
    return UpgradeResult(config=config, data=minimal, warnings=warnings_, changes=changes, diagnostics=diagnostics)


def upgrade_mapping(data: dict[str, Any], *, strict: bool = False) -> UpgradeResult:
    """Validate a raw v1 mapping (paths unresolved) and upgrade it."""
    from pydantic import ValidationError

    try:
        v1 = Config.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigError(first.get("msg", "invalid v1 config"), ".".join(str(p) for p in first.get("loc", ()))) from None
    return upgrade(v1, present_keys=set(data), strict=strict)


def upgrade_text(text: str, *, strict: bool = False) -> tuple[str, UpgradeResult]:
    """YAML in, YAML out (what ``config upgrade`` writes). Lossy settings are written as WARNING
    comments at the top of the file (or refused with ``strict=True``)."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a mapping", "<root>")
    if data.get("schema_version", 1) != 1:
        raise ConfigError("only schema_version 1 configs can be upgraded", "schema_version")
    result = upgrade_mapping(data, strict=strict)
    header = "# Upgraded from schema_version 1 by `su-multi-arm config upgrade`.\n"
    if result.warnings:
        header += "".join(f"# WARNING: {w}\n" for w in result.warnings)
    return header + yaml.safe_dump(result.data, sort_keys=False, allow_unicode=True), result


def downgrade(v2: "ConfigV2") -> Config:
    """Inverse of ``upgrade`` for the v1-expressible subset; anything else is a ``ConfigError``."""
    from ..core.config import Neo4jBackend, PostgresBackend, SqliteBackend

    local = v2.backends.get("local")
    if not isinstance(local, SqliteBackend):
        raise ConfigError("v1 requires backends.local of kind sqlite", "backends.local")
    pg = v2.backends.get("pg")
    graph = v2.backends.get("graph")
    extra = set(v2.backends) - {"local", "pg", "graph"}
    if extra or (pg is not None and not isinstance(pg, PostgresBackend)) or (graph is not None and not isinstance(graph, Neo4jBackend)):
        raise ConfigError("v1 can only express backends local (sqlite), pg (postgres) and graph (neo4j)", "backends")
    if graph is not None and graph.namespace != Neo4jBackend.model_fields["namespace"].default:
        raise ConfigError("v1 has no neo4j namespace; only the default can be downgraded", "backends.graph.namespace")
    if pg is not None:
        for key in ("vector", "text_search_config", "timeout_seconds"):
            if getattr(pg, key) != PostgresBackend.model_fields[key].get_default(call_default_factory=True):
                raise ConfigError(f"v1 has no postgres {key}; only the default can be downgraded", f"backends.pg.{key}")
    for name, role in v2.models.roles.chat_roles().items():
        if role.provider == "openai":
            raise ConfigError("v1 has no openai chat provider (use openai_compatible)", f"models.roles.{name}.provider")
    if v2.models.roles.embedder.provider == "openai":
        raise ConfigError("v1 has no openai embedding provider (use openai_compatible)", "models.roles.embedder.provider")
    arms = v2.pipeline.arms
    if arms.exact_id is not None or arms.cache is not None:
        raise ConfigError("v1 has no exact_id or cache arm", "pipeline.arms")
    decision = v2.pipeline.decision
    if decision.kind == "rules":
        mode = "raw"
    elif decision.kind == "llm":
        mode = "agentic" if v2.pipeline.review.enabled else "planned"
    else:
        raise ConfigError(f"v1 cannot express decision kind {decision.kind!r}", "pipeline.decision.kind")
    if v2.pipeline.selection.rerank and v2.models.roles.reranker is None:
        raise ConfigError("rerank without a reranker role", "pipeline.selection.rerank")
    enabled = []
    for arm in V1_ARMS:
        spec = getattr(arms, "structured" if arm == "sql" else arm)
        if spec is not None:
            enabled.append(arm)
    dense = "pgvector" if arms.dense and arms.dense.backend == "pg" else local.vector_engine
    lexical = "postgres" if arms.lexical and arms.lexical.backend == "pg" else "sqlite"
    graph_backend = "neo4j" if arms.graph and arms.graph.backend == "graph" else "sqlite"
    roles = v2.models.roles
    generator = roles.generator
    community = roles.community
    if v2.pipeline.selection.ordering not in ORDERING_BACK:
        raise ConfigError(
            f"ordering {v2.pipeline.selection.ordering!r} has no v1 fusion strategy", "pipeline.selection.ordering"
        )
    return Config(
        schema_version=1,
        dataset=v2.dataset,
        storage_path=local.path,
        chunk_size=v2.ingestion.chunking.size,
        chunk_overlap=v2.ingestion.chunking.overlap,
        field_order=list(v2.ingestion.chunking.field_order),
        embedding=EmbeddingConfig(**roles.embedder.model_dump()),
        extraction=ExtractionConfig(
            providers=["tags" if e == "gazetteer" else e for e in v2.extraction.extractors],
            grounding=GroundingConfig(**v2.extraction.grounding.model_dump()),
            predicate_triggers=v2.extraction.predicate_triggers,
            stemmer_language=v2.extraction.stemmer_language,
            tags=v2.extraction.gazetteer,
            patterns=v2.extraction.patterns,
            labels=v2.extraction.labels,
            spacy_model=v2.extraction.spacy_model,
            pos=v2.extraction.pos,
            gliner_model=v2.extraction.gliner_model,
            threshold=v2.extraction.threshold,
            model=_model(roles.extractor),
        ),
        privacy=PrivacyConfig(**v2.privacy.model_dump(exclude={"trace_masking"})),
        input_mapping=None if v2.ingestion.input_mapping is None else InputMapping(**v2.ingestion.input_mapping.model_dump()),
        arms=ArmsConfig(
            enabled=enabled,
            dense=dense,
            lexical=lexical,
            graph=graph_backend,
            per_arm=v2.pipeline.selection.per_arm,
            concurrency=v2.pipeline.concurrency,
            graph_hops=arms.graph.hops if arms.graph else 1,
            sql_fields=[f.name for f in v2.metadata_schema.fields],
            postgres=PostgresConfig(dsn_env=pg.dsn_env, table_prefix=pg.table_prefix) if pg else PostgresConfig(),
            neo4j=Neo4jConfig(**graph.model_dump(exclude={"kind", "timeout_seconds", "namespace"})) if graph else Neo4jConfig(),
        ),
        fusion=FusionConfig(
            strategy=ORDERING_BACK[v2.pipeline.selection.ordering],
            rrf_k=v2.pipeline.selection.rrf_k,
            source_weights=v2.pipeline.selection.source_weights,
            arm_weights=v2.pipeline.selection.arm_weights,
            max_documents=v2.pipeline.selection.max_documents,
            reranker_model=roles.reranker.model if (roles.reranker and v2.pipeline.selection.rerank) else None,
        ),
        routing=RoutingConfig(
            mode=mode,
            model=_model(roles.planner),
            prompt=_v1_prompt(decision.prompt, "pipeline.decision"),
            prompt_file=decision.prompt_file,
            max_steps=decision.max_steps,
            max_hops=v2.pipeline.review.max_hops,
            reviewer=_model(roles.reviewer),
        ),
        generation=GenerationConfig(
            model=_model(generator),
            prompt=_v1_prompt(v2.pipeline.generation.prompt, "pipeline.generation"),
            prompt_file=v2.pipeline.generation.prompt_file,
            evidence_char_budget=generator.evidence_budget.chars if generator and generator.evidence_budget else 60000,
        ),
        community=CommunityConfig(
            strategy=v2.communities.strategy,
            min_documents=v2.communities.min_documents,
            seed=v2.communities.seed,
            model=_model(community),
            evidence_char_budget=community.evidence_budget.chars if community and community.evidence_budget else 30000,
            prompt=_v1_prompt(v2.communities.prompt, "communities"),
        ),
        max_llm_calls=v2.budgets.max_llm_calls,
    )
