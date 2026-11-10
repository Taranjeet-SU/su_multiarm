"""Principal and access policy (plan §3.4 ``acl.py``).

An ``AccessPolicy`` turns a principal into scope predicates that every arm, hop, cache key and
value listing applies. Eligibility filters are not authorisation; caller context never grants
access. The kernel ships two policies: ``NoConstraints`` (single-tenant default) and
``StaticPolicy`` (fixed predicates per principal attribute). OAuth2/identity resolution is outside
the core (``PrincipalResolver`` hook, later phase).
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, runtime_checkable

from pydantic import Field

from ..core.types import Predicate, ScopePredicates
from ..models import StrictModel


class Principal(StrictModel):
    id: str = Field(min_length=1)
    tenant: str | None = None
    scopes: list[str] = Field(default_factory=list)  # e.g. "search", "answer", "actions", "ingest"
    attributes: dict[str, str] = Field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def key(self) -> str:
        """Stable identity for session keys and cache keys (no attributes, no secrets)."""
        return hashlib.sha256(json.dumps([self.id, self.tenant], sort_keys=True).encode()).hexdigest()[:32]


ANONYMOUS = Principal(id="anonymous", scopes=["search", "answer"])


@runtime_checkable
class AccessPolicy(Protocol):
    def constraints(self, principal: Principal) -> ScopePredicates: ...


class NoConstraints:
    """Single-tenant default: every principal sees the whole dataset."""

    def constraints(self, principal: Principal) -> ScopePredicates:
        return []


class StaticPolicy:
    """Fixed predicates derived from a principal attribute, e.g. ``tenant`` -> ``eq`` on a metadata field."""

    def __init__(self, field: str, attribute: str = "tenant", deny_when_missing: bool = True):
        self.field, self.attribute, self.deny_when_missing = field, attribute, deny_when_missing

    def constraints(self, principal: Principal) -> ScopePredicates:
        value = principal.tenant if self.attribute == "tenant" else principal.attributes.get(self.attribute)
        if value is None:
            if self.deny_when_missing:
                # An impossible predicate: nothing matches a missing-and-equal field at once.
                return [Predicate(field=self.field, op="in", value=[])]
            return []
        return [Predicate(field=self.field, op="eq", value=value)]


def acl_key(constraints: ScopePredicates) -> str:
    """Deterministic hash of constraints for cache keys and value-listing scoping."""
    data = [p.model_dump(mode="json") for p in constraints]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


class TenantPolicy(StaticPolicy):
    """``access.policy: tenant`` (configuration): ``StaticPolicy`` on ``access.field`` with the
    principal's tenant; principals listed in ``access.unrestricted`` (operators) see everything.
    A principal without a tenant that is not listed sees nothing (fail closed)."""

    def __init__(self, field: str, unrestricted: list[str] | tuple[str, ...] = ()):
        super().__init__(field, "tenant", deny_when_missing=True)
        self.unrestricted = frozenset(unrestricted)

    def constraints(self, principal: Principal) -> ScopePredicates:
        if principal.id in self.unrestricted:
            return []
        return super().constraints(principal)


def policy_from_config(access) -> AccessPolicy:
    """The access policy a configuration names (a closed set; no class loading)."""
    if access is None or access.policy == "none":
        return NoConstraints()
    return TenantPolicy(access.field, list(access.unrestricted))


LOGS_PROFILES = frozenset({"logs_example"})


def require_single_tenant(config, acl) -> None:
    """Addendum A2 rev 2 B4 / rev 3 n4: serving under the logs profile is single-tenant (the
    operator sees all tenants; ``tenant`` is a dimension, not an ACL field). A tenant-scoped policy,
    configured or injected, is refused at start-up instead of serving cross-tenant rollups to it."""
    from ..core.errors import ConfigError

    if getattr(config, "profile", None) in LOGS_PROFILES and not isinstance(acl, NoConstraints):
        raise ConfigError(
            "the logs profile serves a single tenant (addendum A2 B4): remove the access policy; per-tenant ACL over log rollups is deferred",
            "access.policy",
        )
