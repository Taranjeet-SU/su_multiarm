"""Read-only log sources (addendum A1 §3.1–3.2.2): the source contract, the allow-list transport,
the ``elasticsearch_rest`` adapter (Elasticsearch ≥ 7.9, OpenSearch ≥ 1.0), log mapping, multiline
assembly, stratified sampling and the operator probe. No network activity at import time."""

from __future__ import annotations

from typing import Any, Mapping

from ..core.config_sources import SourceConfig
from .contracts import (
    PartitionBudget,
    SamplePlan,
    Source,
    SourceBatch,
    SourceCapabilities,
    SourceCursor,
    SourceDoc,
    SourceUnit,
    SourceWindow,
    Stratum,
    UnitCount,
)
from .elasticsearch_rest import ElasticsearchRestSource
from .errors import (
    SourceAuthError,
    SourceCursorExpired,
    SourceError,
    SourceForbidden,
    SourceQueryRejected,
    SourceRateLimited,
    SourceRequestRefused,
    SourceSchemaDrift,
    SourceTimeout,
    SourceUnavailable,
)
from .logs import IndexNameParser, LogMapping, LogRecord

SOURCE_KINDS: dict[str, type] = {"elasticsearch_rest": ElasticsearchRestSource}


def build_source(name: str, config: SourceConfig, *, env: Mapping[str, str] | None = None, http_transport: Any = None) -> ElasticsearchRestSource:
    """Construct (never connect) the source named ``name``."""
    return SOURCE_KINDS[config.kind](config, source_name=name, env=env, http_transport=http_transport)


__all__ = [
    "ElasticsearchRestSource", "IndexNameParser", "LogMapping", "LogRecord", "PartitionBudget", "SamplePlan", "Source",
    "SourceAuthError", "SourceBatch", "SourceCapabilities", "SourceCursor", "SourceCursorExpired", "SourceDoc", "SourceError",
    "SourceForbidden", "SourceQueryRejected", "SourceRateLimited", "SourceRequestRefused", "SourceSchemaDrift", "SourceTimeout",
    "SourceUnavailable", "SourceUnit", "SourceWindow", "Stratum", "UnitCount", "build_source", "SOURCE_KINDS",
]
