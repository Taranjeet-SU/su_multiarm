"""``ProjectionCatalog``: the names, types, flags and reviewed meaning a typed query may use.

Built from the ``ProjectionSpec`` and the generation's sealed manifest. A catalog exists only for a
sealed projection whose manifest was built from the same spec (``spec_fingerprint``); anything
else is ``projection_unavailable`` (``load`` returns ``None``). Column flags come from the
``FieldSpec`` (``filterable``/``groupable``/``sortable``); every listed column is projectable; the
key ``id`` is filterable and projectable. Descriptions are reviewed, untrusted planner data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field

from ..core.config_projection import KEY
from ..core.types import FieldType, GenerationId
from ..models import StrictModel
from .builder import MANIFEST
from .spec import ProjectionSpec
from .store import served_generation


class CatalogColumn(StrictModel):
    type: FieldType
    filterable: bool = True
    groupable: bool = False
    sortable: bool = False
    projectable: bool = True
    casefold: bool = False
    time_basis: str | None = None
    additive: bool = True
    description: str = ""
    rejected: int = 0


class CatalogTable(StrictModel):
    kind: str
    key: str = KEY
    rows: int = 0
    description: str | None = None
    columns: dict[str, CatalogColumn] = Field(default_factory=dict)


class CatalogRelation(StrictModel):
    child: str
    column: str
    parent: str
    cardinality: Literal["many_to_one"] = "many_to_one"
    description: str
    dangling: int = 0  # child rows without a resolvable parent (all-rows file)
    dangling_by_file: dict[str, int] = Field(default_factory=dict)


class ProjectionCatalog(StrictModel):
    generation: str
    tables: dict[str, CatalogTable]
    relations: dict[str, CatalogRelation] = Field(default_factory=dict)
    access_column: str | None = None
    tenant_files: dict[str, int] = Field(default_factory=dict)
    fingerprint: str

    @classmethod
    def from_manifest(cls, spec: ProjectionSpec, manifest: dict[str, Any]) -> ProjectionCatalog:
        tables: dict[str, CatalogTable] = {}
        for name, table in spec.tables.items():
            counts = manifest["tables"].get(name, {})
            columns = {KEY: CatalogColumn(type="id", description=table.descriptions.get(KEY, "document id (table key)"))}
            for column, field in table.columns.items():
                columns[column] = CatalogColumn(
                    type=field.type,
                    filterable=field.filterable,
                    groupable=field.groupable,
                    sortable=field.sortable,
                    casefold=field.casefold_match,
                    time_basis=field.time_basis,
                    additive=field.additive,
                    description=table.descriptions.get(column, ""),
                    rejected=int(counts.get("rejected", {}).get(column, 0)),
                )
            tables[name] = CatalogTable(kind=table.kind, rows=int(counts.get("rows", 0)), description=table.description, columns=columns)
        relations = {
            name: CatalogRelation(child=r.child, column=r.column, parent=r.parent, description=r.description,
                dangling=int(manifest["relations"].get(name, {}).get("dangling", 0)),
                dangling_by_file={k: int(v) for k, v in manifest["relations"].get(name, {}).get("dangling_by_file", {}).items()},
            )
            for name, r in spec.relations.items()
        }
        digest = hashlib.sha256(json.dumps([spec.fingerprint, manifest["fingerprint"]]).encode()).hexdigest()
        return cls(
            generation=manifest["generation"],
            tables=tables,
            relations=relations,
            access_column=spec.access_column,
            tenant_files=dict(manifest.get("tenant_files", {})),
            fingerprint=digest,
        )

    @classmethod
    def load(cls, facet: Any, gen: GenerationId, spec: ProjectionSpec) -> ProjectionCatalog | None:
        """The catalog of a sealed projection built from ``spec``, else ``None``."""
        if facet is None:
            return None
        raw = facet.read_file(gen, MANIFEST)
        if raw is None:
            return None
        manifest = json.loads(raw)
        # a partitioned collection serves one member's sealed files: they name that member's
        # store generation, not the unified one (``projection.store.served_generation``, M1b)
        if not manifest.get("sealed") or manifest.get("spec_fingerprint") != spec.fingerprint or manifest.get("generation") != served_generation(facet, gen).generation:
            return None
        return cls.from_manifest(spec, manifest)

    def grant(self) -> dict[str, set[str]]:
        """table -> readable columns (the read-only authorizer's allowlist)."""
        return {name: set(table.columns) for name, table in self.tables.items()}

    def planner_view(self) -> dict[str, Any]:
        """Tables, columns (type, flags, reviewed description) and relations; no values (M6 adds
        ACL-scoped samples for low-cardinality columns)."""
        return {
            "tables": {
                name: {"kind": t.kind, "description": t.description, "columns": {c: {k: v for k, v in col.model_dump().items() if k not in {"rejected"}} for c, col in t.columns.items()}}
                for name, t in self.tables.items()
            },
            "relations": {name: r.model_dump(exclude={"dangling", "dangling_by_file"}) for name, r in self.relations.items()},
        }
