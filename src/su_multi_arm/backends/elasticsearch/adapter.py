"""``ElasticsearchBackend``: the OpenSearch adapter classes (``backends/opensearch/`` over
``backends/_lucene_common.py``) with the Elasticsearch-specific parts replaced.

Shared with OpenSearch: index naming (``<prefix>_<dataset>_<generation>_<docs|chunks|records>``),
dataset aliases switched atomically by ``activate(gen)``, the typed field layout and native
``bool.filter`` pushdown, BM25 ``multi_match`` lexical queries, bulk/bulk-check, paging, the records
scan with the shared ``_scope`` measure code, mapping-version metadata, the delete guards.
Elasticsearch-specific: the transport (official client), ``probe`` (8.x only; OpenSearch and other
majors are refused), ``dense_vector`` fields and the top-level ``knn`` search (``search.py``), no
``index.knn`` setting, and an alias-backed ``generation_registry()`` so the write path can own
``storage.catalog`` on this backend (``registry.py``).
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from ...core.errors import BackendError
from ...core.types import GenerationId
from .._lucene_common import MAX_MATCHED_DEFAULT, Transport
from ..opensearch.adapter import OpenSearchBackend
from ..opensearch.catalog import OpenSearchCatalog, OpenSearchRecords
from .search import MAX_DIMENSIONS, ElasticsearchLexical, ElasticsearchVector

KIND = "elasticsearch"
SUPPORTED_MAJOR = "8"


class ElasticsearchBackend(OpenSearchBackend):
    """One cluster, one index prefix, four facets; ``dataset`` enables ``generation_registry()``."""

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
        dataset: str | None = None,
    ):
        super().__init__(hosts, index_prefix, knn_method, exact_rescore, timeout_seconds, max_matched, transport, clock)
        self.dataset = dataset
        self._registry: Any = None
        self.catalog = OpenSearchCatalog(self)
        self.lexical = ElasticsearchLexical(self)
        self.vector = ElasticsearchVector(self)
        self.records = OpenSearchRecords(self)

    @classmethod
    def from_config(cls, config: Any, clock: Callable[[], float] = time.time, dataset: str | None = None) -> ElasticsearchBackend:
        raw = os.environ.get(config.hosts_env)
        if not raw:
            raise BackendError(KIND, "init", f"environment variable {config.hosts_env} (hosts) is not set")
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        return cls(hosts, config.index_prefix, config.knn.method, config.knn.exact_rescore, config.timeout_seconds, clock=clock, dataset=dataset)

    def _default_transport(self, hosts: list[str], timeout_seconds: float) -> Transport:
        from .transport import ElasticsearchTransport

        return ElasticsearchTransport(hosts, timeout_seconds)

    def index_body(self, index_kind: str, gen: GenerationId) -> dict[str, Any]:
        """OpenSearch's index body without the ``index.knn`` setting Elasticsearch does not know."""
        body = super().index_body(index_kind, gen)
        body["settings"]["index"].pop("knn", None)
        return body

    def probe(self) -> dict[str, Any]:
        """Server identity (``GET /``); refuses anything that is not Elasticsearch 8.x."""
        info = self.call("probe", "GET", "/")
        version = info.get("version", {}) if isinstance(info, dict) else {}
        number = str(version.get("number", ""))
        distribution = version.get("distribution")
        if distribution is not None or not number.startswith(f"{SUPPORTED_MAJOR}."):
            raise BackendError(KIND, "probe", f"expected an Elasticsearch {SUPPORTED_MAJOR}.x server, got {distribution or 'elasticsearch'!r} {number!r}")
        return {"distribution": "elasticsearch", "version": number, "lucene_version": version.get("lucene_version"), "max_dimensions": MAX_DIMENSIONS}

    def generation_registry(self):
        """Alias-backed pointer/manifest registry (non-contract; see ``ingestion/release.py``)."""
        from .registry import ElasticsearchGenerationRegistry

        if self._registry is None:
            if self.dataset is None:
                raise BackendError(KIND, "registry", "the backend needs a dataset to own a generation registry")
            self._registry = ElasticsearchGenerationRegistry(self, self.dataset)
        return self._registry
