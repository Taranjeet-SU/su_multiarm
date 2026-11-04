"""Static capability matrix: adapter kind -> declared contracts, flags and status (plan §3.5).

Used by config validation (every arm/storage reference must name a backend whose kind *serves* the
capability) and by ``doctor``. Status is recorded per ``(adapter, capability)`` so the matrix is
honest at the granularity validation uses:

* ``contracts``: implements the frozen ``backends/contracts.py`` protocol in this release;
* ``legacy``: v1 code serves the capability (``adapters/``) and the port onto the contract is
  scheduled in Phase 1; configuration accepts it, the v2 engine refuses the backend kind;
* ``planned``: declared by the plan, no code; configuration naming the pair is rejected.

The adapter-level ``status`` is derived: ``contracts`` when every capability is on the contracts,
``planned`` when none is served, ``legacy`` otherwise.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ..models import StrictModel
from .contracts import CONTRACT_NAMES

Capability = Literal["catalog", "lexical", "vector", "records", "graph", "cache", "ledger", "session"]
CAPABILITIES: tuple[str, ...] = ("catalog", "lexical", "vector", "records", "graph", "cache", "ledger", "session")
CapabilityStatus = Literal["contracts", "legacy", "planned"]
Flags = dict[str, bool | int | str | None]


class CapabilityDecl(StrictModel):
    status: CapabilityStatus
    flags: Flags = Field(default_factory=dict)


class AdapterCapabilities(StrictModel):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    capabilities: dict[Capability, CapabilityDecl]
    notes: str = ""

    @property
    def status(self) -> CapabilityStatus:
        statuses = {c.status for c in self.capabilities.values()}
        if statuses == {"contracts"}:
            return "contracts"
        if statuses <= {"planned"}:
            return "planned"
        return "legacy"

    def declares(self, capability: str) -> bool:
        return capability in self.capabilities

    def serves(self, capability: str) -> bool:
        decl = self.capabilities.get(capability)
        return decl is not None and decl.status != "planned"

    def status_of(self, capability: str) -> CapabilityStatus | None:
        decl = self.capabilities.get(capability)
        return None if decl is None else decl.status

    def flags(self, capability: str) -> Flags:
        decl = self.capabilities.get(capability)
        return {} if decl is None else dict(decl.flags)

    @model_validator(mode="after")
    def required_flags(self):
        for name, decl in self.capabilities.items():
            if name == "vector" and "max_dimensions" not in decl.flags:
                raise ValueError(f"{self.kind}: vector capability must declare max_dimensions")
            if name == "lexical" and "phrase" not in decl.flags:
                raise ValueError(f"{self.kind}: lexical capability must declare phrase")
            if name == "records" and decl.flags.get("exactness") not in {"exact", "approximate"}:
                raise ValueError(f"{self.kind}: records capability must declare exactness")
            if name == "graph" and "memberships" not in decl.flags:
                raise ValueError(f"{self.kind}: graph capability must declare memberships")
        return self


def _c(status: CapabilityStatus, **flags: Any) -> CapabilityDecl:
    return CapabilityDecl(status=status, flags=flags)


MATRIX: dict[str, AdapterCapabilities] = {
    item.kind: item
    for item in (
        AdapterCapabilities(
            kind="memory",
            capabilities={
                "catalog": _c("contracts", registry=True),
                "lexical": _c("contracts", phrase=True, highlight=False, field_boost=False, analyzer_id="python_word"),
                "vector": _c("contracts", exact=True, ann=None, filter_pushdown=True, max_dimensions=4096, namespaces=True),
                "records": _c("contracts", exactness="exact", date_buckets=True, top_n=True),
                "graph": _c("contracts", templated=True, provenance=True, memberships=True, predicate_scope=True),
                "cache": _c("contracts", exact=True, semantic=False),
                "ledger": _c("contracts", raw_replies=True),
                "session": _c("contracts"),
            },
            notes="In-memory fake for tests and the conformance kit; not for production.",
        ),
        AdapterCapabilities(
            kind="sqlite",
            capabilities={
                "catalog": _c("contracts", registry=True),
                "lexical": _c("contracts", phrase=True, highlight=False, field_boost=False, analyzer_id="fts5_unicode61"),
                # exact scan over the generation's vector file with the scope checked per candidate
                # batch: correct, but not a pushed-down filter.
                "vector": _c("contracts", exact=True, ann=None, filter_pushdown=False, max_dimensions=65536, namespaces=True),
                "records": _c("contracts", exactness="exact", date_buckets=True, top_n=True),
                "graph": _c("contracts", templated=True, provenance=True, memberships=True, predicate_scope=True),
                "cache": _c("contracts", exact=True, semantic=False),
                "ledger": _c("contracts", raw_replies=True),
                "session": _c("contracts"),
            },
            notes="Local default on the frozen contracts (backends/sqlite/); conformance kit v0 passes with numpy and faiss.",
        ),
        AdapterCapabilities(
            kind="postgres",
            capabilities={
                "catalog": _c("contracts", registry=True),
                # analyzer_id names the family; each generation records pg_ts_<text_search_config>.
                "lexical": _c("contracts", phrase=True, highlight=False, field_boost=False, analyzer_id="pg_ts_config"),
                # exact by default (scope in the WHERE clause); vector.index: hnsw is opt-in behind the
                # filter-recall gate at refresh. 2000 = pgvector's HNSW limit (exact allows 16000).
                "vector": _c("contracts", exact=True, ann="hnsw_optional", filter_pushdown=True, max_dimensions=2000, namespaces=True),
                "records": _c("contracts", exactness="exact", date_buckets=True, top_n=True),
                "graph": _c("planned", templated=True, provenance=True, memberships=False),
                "cache": _c("contracts", exact=True, semantic=False),
                "ledger": _c("contracts", raw_replies=True),
                "session": _c("contracts"),
            },
            notes=(
                "backends/postgres/ on the contracts, one schema per generation; conformance passed against "
                "pgvector/pgvector:0.8.0-pg17. FTS scores are ts_rank_cd, not BM25. Graph is planned."
            ),
        ),
        AdapterCapabilities(
            kind="sqlalchemy",
            capabilities={
                "catalog": _c("contracts", registry=True),
                "lexical": _c("planned", phrase=False, highlight=False, field_boost=False, analyzer_id="dialect"),
                "vector": _c("planned", exact=True, ann=None, filter_pushdown=True, max_dimensions=None, namespaces=False),
                "records": _c("contracts", exactness="exact", date_buckets=True, top_n=True),
                "cache": _c("contracts", exact=True, semantic=False),
                "ledger": _c("contracts", raw_replies=True),
                "session": _c("contracts"),
            },
            notes=(
                "backends/sql/ (SQLAlchemy Core) on the contracts; conformance passed on SQLite and PostgreSQL. "
                "Other dialects build but are untested. Lexical and vector are dialect-specific and planned."
            ),
        ),
        AdapterCapabilities(
            kind="opensearch",
            capabilities={
                "catalog": _c("contracts", registry=False),
                "lexical": _c("contracts", phrase=True, highlight=True, field_boost=True, analyzer_id="lucene_standard"),
                "vector": _c("contracts", exact=True, ann="hnsw_optional", filter_pushdown=True, max_dimensions=16000, namespaces=True),
                "records": _c("contracts", exactness="exact", date_buckets=False, top_n=True, max_matched=10000),
            },
            notes=(
                "backends/opensearch/ on the frozen contracts (OpenSearch 2.x, opensearch-py). Generation-named indices "
                "+ dataset aliases; BM25 over the standard analyzer; exact cosine by default, HNSW (lucene engine) opt-in "
                "with exact rescore; records exact through native filter pushdown and a bounded scan (max_matched). "
                "graph/cache/ledger/session are not served (plan §3.5 table)."
            ),
        ),
        AdapterCapabilities(
            kind="elasticsearch",
            capabilities={
                "catalog": _c("contracts", registry=True),
                "lexical": _c("contracts", phrase=True, highlight=True, field_boost=True, analyzer_id="lucene_standard"),
                "vector": _c("contracts", exact=True, ann="hnsw_optional", filter_pushdown=True, max_dimensions=4096, namespaces=True),
                "records": _c("contracts", exactness="exact", date_buckets=False, top_n=True, max_matched=10000),
            },
            notes=(
                "backends/elasticsearch/ on the frozen contracts (Elasticsearch 8.x, elasticsearch-py), built on the "
                "OpenSearch adapter classes over backends/_lucene_common.py: same index layout, aliases, BM25 and typed "
                "pushdown; dense_vector exact cosine by default, HNSW (float, unquantised) opt-in with exact rescore; "
                "records exact through a bounded scan (max_matched). graph/cache/ledger/session are not served."
            ),
        ),
        AdapterCapabilities(
            kind="neo4j",
            capabilities={"graph": _c("contracts", templated=True, provenance=True, memberships=True, predicate_scope=False)},
            notes=(
                "backends/neo4j/ on the contracts; graph conformance passed against neo4j:5.26 community. Fixed "
                "parameterised Cypher only; eligibility is a pushed-down document-id allow/deny list "
                "(predicate_scope false: the arms resolve predicates through the catalog first)."
            ),
        ),
        AdapterCapabilities(
            kind="kuzu",
            capabilities={"graph": _c("contracts", templated=True, provenance=True, memberships=True, predicate_scope=False, embedded=True)},
            notes=(
                "backends/kuzu/ (embedded Kuzu, the 'kuzu' extra) on the contracts; graph conformance passes in-process "
                "(no service). Fixed parameterised Cypher only; eligibility is a document-id allow/deny list "
                "(predicate_scope false). mode: attach opens an existing database read-only through a declared "
                "mapping file (upsert/put_communities refused)."
            ),
        ),
        AdapterCapabilities(
            kind="mongodb",
            capabilities={
                "catalog": _c("contracts", registry=True),
                "lexical": _c("contracts", phrase=True, highlight=False, field_boost=False, analyzer_id="mongo_text_english"),
                "vector": _c("planned", exact=False, ann="atlas_only", filter_pushdown=True, max_dimensions=4096, namespaces=True),
                "records": _c("contracts", exactness="exact", date_buckets=True, top_n=True),
                "cache": _c("contracts", exact=True, semantic=False),
                "ledger": _c("contracts", raw_replies=True),
                "session": _c("contracts"),
            },
            notes=(
                "backends/mongodb/ on the contracts; conformance passed against a community server (mongo:7). "
                "Text-index scores are not BM25. Vector search exists only on MongoDB Atlas and stays planned: "
                "it cannot be verified against the community server and is never claimed."
            ),
        ),
    )
}

# Which (capability, required flag) each arm / storage slot needs.
ARM_CAPABILITY: dict[str, tuple[str, str | None]] = {
    "exact_id": ("catalog", None),
    "dense": ("vector", None),
    "lexical": ("lexical", None),
    "structured": ("records", None),
    "graph": ("graph", None),
    "community": ("graph", "memberships"),
    "cache": ("cache", None),
}
STORAGE_CAPABILITY: dict[str, tuple[str, str | None]] = {
    # The authoritative catalog also holds the generation registry (CURRENT pointer, manifests).
    "catalog": ("catalog", "registry"),
    "records": ("records", None),
    "ledger": ("ledger", None),
    "sessions": ("session", None),
    "cache": ("cache", None),
    "communities": ("graph", "memberships"),
}


def adapter(kind: str) -> AdapterCapabilities:
    try:
        return MATRIX[kind]
    except KeyError:
        raise KeyError(f"unknown backend kind {kind!r}; known: {sorted(MATRIX)}") from None


def declares(kind: str, capability: str) -> bool:
    return kind in MATRIX and MATRIX[kind].declares(capability)


def serves(kind: str, capability: str, flag: str | None = None) -> bool:
    """Declared, not ``planned``, and (when given) the flag is truthy."""
    if kind not in MATRIX or not MATRIX[kind].serves(capability):
        return False
    return flag is None or bool(MATRIX[kind].flags(capability).get(flag))


def status_of(kind: str, capability: str) -> CapabilityStatus | None:
    return adapter(kind).status_of(capability)


def flags(kind: str, capability: str) -> Flags:
    return adapter(kind).flags(capability)


def facet(adapter_obj: Any, capability: str) -> Any:
    """Adapters expose one facet per capability (``adapter.catalog``, ``adapter.lexical``, ...)."""
    return getattr(adapter_obj, capability, None)


def satisfied(adapter_obj: Any, capability: str) -> bool:
    """Runtime structural check of an adapter's facet against one contract."""
    target = facet(adapter_obj, capability)
    return target is not None and isinstance(target, CONTRACT_NAMES[capability])


def conformance(adapter_obj: Any) -> set[str]:
    """Every contract an adapter instance structurally satisfies through its facets."""
    return {name for name in CAPABILITIES if satisfied(adapter_obj, name)}


def check_matrix() -> list[str]:
    """Internal consistency of the static matrix; returns problems (empty when consistent)."""
    problems = []
    for kind, item in MATRIX.items():
        if item.kind != kind:
            problems.append(f"{kind}: key/kind mismatch")
        for capability in item.capabilities:
            if capability not in CAPABILITIES:
                problems.append(f"{kind}: unknown capability {capability}")
    for table in (ARM_CAPABILITY, STORAGE_CAPABILITY):
        for name, (capability, _flag) in table.items():
            if capability not in CAPABILITIES:
                problems.append(f"{name}: unknown capability {capability}")
    return problems
