"""Typed traversal templates, the graph ontology view and the typed graph intent (plan §3.8 graph
arm, Phase 3 "traversal extensions").

A **template** is a declared, parameterised, read-only traversal pattern. The decision agent selects
one by name through a typed ``GraphIntent`` (template name + seed strings); it never writes a query.
Templates compile to calls on the frozen ``GraphStore.traverse`` contract and the ``GraphExtras``
lookups only, so one template has the same semantics on every graph backend that passes the
conformance kit (sqlite, neo4j, kuzu, the in-memory fake). Kinds (closed set):

* ``neighbors`` — seed -> ``hop`` (predicate set, direction, edge kinds) -> entity, ``hops`` 1..3;
* ``chain`` — an ordered list of 1..3 hops, each with its own predicate set and direction
  (e.g. issue -has_root_cause-> cause, or symptom <-has_symptom- issue -resolved_by-> fix);
* ``connect`` — paths of at most ``hops`` hops from the first seed that end at the second seed;
* ``mentioned_in`` — the span-anchored mentions (documents) of the seed entities.

Seeds are resolved through ``GraphExtras.find_entities``: ``seed_match: exact`` keeps entities whose
name tokens equal the seed's tokens (a seed that is a stored entity id resolves to itself);
``tokens`` keeps every entity whose name tokens are all in the seed text (the graph arm's rule).

A ``GraphOntology`` (configured on the arm, or exposed by an attached store's mapping) lets static
validation refuse templates that name unknown predicates or entity types, or chain hops whose
endpoint types cannot meet.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..models import StrictModel
from .types import EdgeKind

IDENT = r"^[a-z][a-z0-9_]{0,63}$"
MAX_SEEDS = 10


class PredicateDecl(StrictModel):
    """Entity types a predicate may connect (empty = any)."""

    subject_types: list[str] = Field(default_factory=list)
    object_types: list[str] = Field(default_factory=list)


class GraphOntology(StrictModel):
    entity_types: list[str] = Field(default_factory=list)
    predicates: dict[str, PredicateDecl] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_types(self):
        types = set(self.entity_types)
        for name, decl in self.predicates.items():
            unknown = (set(decl.subject_types) | set(decl.object_types)) - types
            if types and unknown:
                raise ValueError(f"predicate {name!r} names undeclared entity types {sorted(unknown)}")
        return self


class TraversalHop(StrictModel):
    predicates: list[str] = Field(default_factory=list)  # empty = any predicate
    direction: Literal["out", "in", "any"] = "any"
    edge_kinds: list[EdgeKind] = Field(default_factory=lambda: ["fact"], min_length=1)

    @field_validator("edge_kinds")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("edge_kinds must be unique")
        return value


class TraversalTemplate(StrictModel):
    kind: Literal["neighbors", "chain", "connect", "mentioned_in"]
    description: str = Field(default="", max_length=500)
    seed_match: Literal["exact", "tokens"] = "exact"
    seed_types: list[str] = Field(default_factory=list)  # entity types a seed must have (empty = any)
    target_types: list[str] = Field(default_factory=list)  # entity types the last node must have (empty = any)
    hop: TraversalHop = Field(default_factory=TraversalHop)  # neighbors, connect
    hops: int = Field(default=1, ge=1, le=3)  # neighbors, connect
    steps: list[TraversalHop] = Field(default_factory=list, max_length=3)  # chain
    limit: int = Field(default=50, ge=1, le=1000)

    @model_validator(mode="after")
    def coherent(self):
        if self.kind == "chain" and not self.steps:
            raise ValueError("a chain template needs 1..3 steps")
        if self.kind != "chain" and self.steps:
            raise ValueError(f"only chain templates carry steps, not {self.kind}")
        if self.kind == "mentioned_in" and self.target_types:
            raise ValueError("mentioned_in returns mentions of the seeds; target_types does not apply")
        return self

    def all_hops(self) -> list[TraversalHop]:
        if self.kind == "chain":
            return list(self.steps)
        if self.kind in {"neighbors", "connect"}:
            return [self.hop] * self.hops
        return []

    def problems(self, ontology: GraphOntology | None) -> list[str]:
        """Why this template does not fit ``ontology`` (empty when it fits or there is none)."""
        if ontology is None:
            return []
        out: list[str] = []
        types = set(ontology.entity_types)
        for label, values in (("seed_types", self.seed_types), ("target_types", self.target_types)):
            unknown = sorted(set(values) - types) if types else []
            if unknown:
                out.append(f"{label} names unknown entity types {unknown}")
        for index, hop in enumerate(self.all_hops()):
            unknown = sorted(set(hop.predicates) - set(ontology.predicates))
            if unknown:
                out.append(f"hop {index} names unknown predicates {unknown}")
        if self.kind == "chain":
            reach = set(self.seed_types)
            for index, hop in enumerate(self.steps):
                reach = _reachable(ontology, hop, reach)
                if reach is not None and not reach:
                    out.append(f"step {index} cannot follow the previous step: no declared predicate connects the types")
                    break
            if reach and self.target_types and not (reach & set(self.target_types)):
                out.append("the last step cannot reach target_types")
        return out


def _reachable(ontology: GraphOntology, hop: TraversalHop, sources: set[str] | None) -> set[str] | None:
    """Entity types reachable from ``sources`` over ``hop``; ``None`` = unconstrained (any type)."""
    names = hop.predicates or list(ontology.predicates)
    reach: set[str] = set()
    for name in names:
        decl = ontology.predicates.get(name)
        if decl is None:
            continue
        forward = [(decl.subject_types, decl.object_types)] if hop.direction in {"out", "any"} else []
        backward = [(decl.object_types, decl.subject_types)] if hop.direction in {"in", "any"} else []
        for start, end in forward + backward:
            if sources and start and not (set(start) & sources):
                continue
            if not end:
                return None
            reach |= set(end)
    return reach


class GraphIntent(StrictModel):
    """A typed graph step: which declared template, applied to which seeds. Never query text."""

    template: str = Field(pattern=IDENT)
    seeds: list[str] = Field(min_length=1, max_length=MAX_SEEDS)

    @field_validator("seeds")
    @classmethod
    def bounded(cls, value):
        for seed in value:
            if not seed.strip() or len(seed) > 300:
                raise ValueError("each seed is a non-empty string of at most 300 characters")
        return value
