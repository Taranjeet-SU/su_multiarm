"""SQL surface of the ``typed_query`` arm (``pipeline.arms.typed_query.mode: sql_surface``; opt-in).

Ports NVIDIA Auto-Ontology's catalog + text-to-SQL generation (Apache-2.0, pinned revision
``c447003f72a822301e927196a4ecb5b597c308d5``; see ``NOTICE`` and ``docs/THIRD_PARTY_NOTICES.md``)
and executes the model's SQL only behind this package's safety layer:

* ``catalog``  (adapted): the logical schema per principal: terms = projection tables, columns with
  physical binding, types, reviewed descriptions and ACL-scoped sample values.
* ``prompts``  (adapted): SQLite generation and one-error repair prompts with an answerability field.
* ``values``   (adapted): column-bound literals of a parsed statement.
* ``planner``  (adapted): the generate -> execute -> (one repair) flow and the evaluation entry points.
* ``guard``    (ours): ``sqlglot`` parse, allowlists, value grounding, ACL/filter binding, parameters.
* ``executor`` (ours): per-tenant file selection, read-only execution, returned-row check, evidence.
* ``route``    (ours): the ``llm`` planner's routed sub-question at execution (engine hook), metering,
  executed-SQL qualifier check and the ``on_step_failure`` mapping of non-executed routes.
* ``setup``    (ours, unified merge M4 guard): the multi-source setup contract — read-only
  attachments and pinned TEMP views created before the authorizer, the per-source catalog with
  units and enforced join paths, table-aware value grounding and execution. Not yet wired into the
  arm (M4 completion).

``guard``/``values`` need the optional ``sqlglot`` dependency (``su-multi-arm[sql-surface]``);
without it every statement is refused with ``sqlglot_missing`` and the arm's ``validate`` reports it.
"""

from .catalog import SqlCatalog, SqlColumn, SqlJoin, SqlTable, build_sql_catalog
from .guard import SQL_FUNCTIONS, GuardedSql, SqlRefusal, available
from .setup import SetupValues, SqlSetupError, SqlSurfaceSetup, execute_setup, open_surface, setup_catalog
from .planner import (
    SqlAttempt,
    SqlDraft,
    SqlSurfacePlanner,
    SqlSurfaceResult,
    SqlSurfaceSession,
    SqlSurfaceUnavailable,
    engine_session,
    open_session,
    role_model,
)

__all__ = [
    "SQL_FUNCTIONS",
    "GuardedSql",
    "SqlAttempt",
    "SqlCatalog",
    "SqlColumn",
    "SqlDraft",
    "SetupValues",
    "SqlJoin",
    "SqlRefusal",
    "SqlSetupError",
    "SqlSurfacePlanner",
    "SqlSurfaceResult",
    "SqlSurfaceSession",
    "SqlSurfaceSetup",
    "SqlSurfaceUnavailable",
    "SqlTable",
    "available",
    "build_sql_catalog",
    "engine_session",
    "execute_setup",
    "open_session",
    "open_surface",
    "role_model",
    "setup_catalog",
]
