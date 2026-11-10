"""Per-generation typed projection (typed-query plan §5.2; sqlite only in the MVP).

``spec``: the validated table/column/relation layout derived from ``projection`` config and the
metadata schema. ``builder``: rows from the write path's ``StructuredRecord`` batches into one
all-rows file plus one file per tenant value, sealed by a manifest. ``catalog``: what a compiler
may name (types, flags, reviewed descriptions, relations, manifest counts). ``store``: read-only
connections (``mode=ro``/``query_only``, an authorizer allowing only ``SELECT``/``READ`` of
catalog tables and columns, a progress-handler timeout). Written only by ingestion.
"""

from .builder import MANIFEST, ProjectionBuilder, tenant_file
from .catalog import ProjectionCatalog
from .spec import ProjectionSpec
from .store import MemoryProjectionFiles, FileProjectionFiles, open_readonly

__all__ = ["MANIFEST", "FileProjectionFiles", "MemoryProjectionFiles", "ProjectionBuilder", "ProjectionCatalog", "ProjectionSpec", "open_readonly", "tenant_file"]
