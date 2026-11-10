"""Cross-source blocks (``crosssource``; schema v3) — docs/plans/unified_merge.md §6.9, §7.

* ``crosssource.identity``: the private, versioned identity registry (customer, alias, evidence,
  review), pinned by sha256. Only ``matched`` aliases ever materialize a ``customer_id``; identity
  never grants access.
* ``crosssource.links``: tiered cross-source links with statuses and a model review whose spend is
  bounded by a cumulative ceiling shared with identity review. The ceiling is recorded with its
  approval reference in a persistent ledger at first use; a configuration edit alone never raises
  the approved figure (the ledger keeps the first recorded one).

* ``crosssource.planner`` (M2b-2): two-halves planning over the unified collection. A question with
  an operations intent (log evidence: liveness, failures, outages) is answered with a knowledge half
  (today's plan over the knowledge sources) and an operations half (typed log intents grounded
  deterministically: vocabulary, question clock, default view, customers through the registry).
  Off by default: every request is unchanged.

Increment S0 validated these blocks; M2b-1/M3 built the registry and the link store; M2b-2 serves
them: customer text is grounded through ``IdentityView.pinned`` (matched aliases only) and links are
displayed through ``LinkView.pinned`` (``crosssource.links.{store_path, sha256}``), labelled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..models import StrictModel

IDENTIFIER = r"^[a-z][a-z0-9_]{0,63}$"
SHA256 = r"^[0-9a-f]{64}$"
# The review ceiling the owner approved for link and identity review together (plan §6.9, §14).
REVIEW_USD_CEILING_MAX = 10.0


class IdentityConfig(StrictModel):
    """``crosssource.identity``: the registry file (sqlite or YAML export) and its digest; a digest
    mismatch disables every cross-source join (fail closed, §6.12)."""

    registry_path: Path
    sha256: str = Field(pattern=SHA256)
    # Which aliases of the pinned registry join (§6.9; owner rule change 2026-11-10). ``exact``
    # (default): ``matched`` aliases only (exact/casefold unique hits, or a decided match an agreeing
    # review or the owner confirmed). ``fuzzy``: also a ``candidate`` alias resolved to one customer
    # with ``confidence >= fuzzy_min_confidence`` (a fuzzy rule or a coordinator match awaiting
    # review); it joins as matched with its rule recorded as ``fuzzy:<rule>``. Excluded, unmatched,
    # contested and ambiguous aliases never join; the registry file is not changed.
    auto_match: Literal["exact", "fuzzy"] = "exact"
    fuzzy_min_confidence: float = Field(default=0.8, ge=0.5, le=1.0)


class LinkReview(StrictModel):
    """Model review of non-exact aliases and candidate links: ``role`` is a configured chat role;
    ``usd_ceiling`` is cumulative across runs (persistent ledger) and shared with identity review;
    ``approval_ref`` records who approved the ceiling (required, recorded in the ledger)."""

    role: str = Field(default="reviewer", pattern=IDENTIFIER)
    usd_ceiling: float = Field(default=REVIEW_USD_CEILING_MAX, gt=0, le=REVIEW_USD_CEILING_MAX)
    approval_ref: str = Field(min_length=1, max_length=200)


class LinksConfig(StrictModel):
    """``crosssource.links`` (§6.9): the tier-B time window around a ticket's span, the per-record
    cap, how unreviewed candidates are displayed (``labelled``) and the review settings."""

    window_before_days: int = Field(default=1, ge=0, le=90)
    window_after_days: int = Field(default=7, ge=0, le=365)
    max_per_record: int = Field(default=5, ge=1, le=100)
    show_candidates: Literal["labelled", "hidden"] = "labelled"
    review: LinkReview
    # The pinned link store served at query time (M2b-2): an exported or sqlite link store and its
    # sha256. A missing file or a digest mismatch serves no links (labelled, §6.12); both or neither.
    store_path: Path | None = None
    sha256: str | None = Field(default=None, pattern=SHA256)

    @model_validator(mode="after")
    def pinned_together(self):
        if (self.store_path is None) != (self.sha256 is None):
            raise ValueError("crosssource.links.store_path and crosssource.links.sha256 are set together")
        return self


NAMESPACE = r"^[a-z][a-z0-9_]{0,63}\.[a-z][a-z0-9_]{0,63}$"
# Words and phrases that make a question ask for operations evidence (``operations_terms``):
# whole words/phrases, case-insensitive; each inflection is listed (no prefix matching).
OPERATIONS_TERMS = (
    "log", "logs", "logging", "logged", "service", "services", "crawl", "crawls", "crawled", "crawling", "crawler", "crawlers",
    "error", "errors", "outage", "outages", "downtime", "incident", "incidents", "exception", "exceptions", "failure", "failures",
    "signature", "signatures", "endpoint", "endpoints", "heartbeat", "heartbeats", "latency", "timeout", "timeouts", "stack trace",
    "5xx", "cron", "indexing", "indexer",
)  # fmt: skip
OPERATIONS_TERM = r"^[a-z0-9][a-z0-9 _-]{0,47}$"


class PlannerConfig(StrictModel):
    """``crosssource.planner`` (M2b-2, §6.11): two-halves planning. ``knowledge_sources``: the
    ``Document.source`` values of the knowledge half (tickets, docs, tracker issues); empty on a
    partitioned collection = every member not produced by a log host. ``customer_namespaces``: the
    registry namespaces whose raw values name customers in questions; ``tenant_namespace``: the
    namespace of log tenants (a named customer filters the operations half to its matched tenants,
    or the operations half is refused for it, never widened). ``fallback``: what a failed planner
    leaves (``deterministic_plan``: a labelled plan of both halves; ``abstain``: today's policy).
    ``scope_knowledge_steps`` (default on): when the collection holds sources outside
    ``knowledge_sources`` (a log member), every knowledge-half retrieval and structured step reads
    the knowledge sources only (a ``source in knowledge_sources`` scope applied at execution, never
    written into the plan), so the operations half alone reads logs; without such a source nothing
    changes. ``false`` restores unscoped knowledge steps (log records may then enter the hits).

    ``operations_terms`` (staging round 2, N2): a question gets an operations half only when it
    asks for operations evidence: one of these words or phrases (whole word, case-insensitive; list
    every inflection, there is no prefix matching) or a failure symptom of the unified detector
    (``decision/log_intents.OPS_SYMPTOM``: "unauthorized", "timed out", "stuck", "failed", "slow",
    "down" ...). Generic words such as "new", "mentions" or "increase" are not evidence, so "how many
    are new?" stays a support question. An empty list leaves the symptoms only. The rule gates both
    today's log detectors and the unified detector; a log-only deployment (no ``crosssource``) is
    unchanged."""

    enabled: bool = False
    knowledge_sources: list[str] = Field(default_factory=list, max_length=16)
    customer_namespaces: list[str] = Field(default_factory=lambda: ["support.account"], max_length=8)
    tenant_namespace: str = Field(default="logs.tenant", pattern=NAMESPACE)
    fallback: Literal["deterministic_plan", "abstain"] = "deterministic_plan"
    max_operations_calls: int = Field(default=3, ge=1, le=6)
    scope_knowledge_steps: bool = True
    operations_terms: list[str] = Field(default_factory=lambda: list(OPERATIONS_TERMS), max_length=200)

    @model_validator(mode="after")
    def namespaces(self):
        import re

        bad = [n for n in self.customer_namespaces if not re.fullmatch(NAMESPACE, n)]
        if bad:
            raise ValueError(f"customer_namespaces must be <source>.<field> namespaces: {bad}")
        terms = [t for t in self.operations_terms if not re.fullmatch(OPERATIONS_TERM, t)]
        if terms:
            raise ValueError(f"operations_terms must be lower-case words or phrases: {terms}")
        if len(set(self.operations_terms)) != len(self.operations_terms):
            raise ValueError("operations_terms must be unique")
        return self


class CrossSourceConfig(StrictModel):
    identity: IdentityConfig | None = None
    links: LinksConfig | None = None
    planner: PlannerConfig | None = None
