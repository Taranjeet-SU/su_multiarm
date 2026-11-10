# Third-party notices

This file lists third-party source code that is copied or adapted into the `su_multi_arm`
distribution. Optional runtime dependencies installed through extras (for example `sqlglot`,
MIT) are not vendored and keep their own licenses.

## NVIDIA Auto-Ontology (Apache-2.0)

- Project: NVIDIA/auto-ontology
- Revision: `c447003f72a822301e927196a4ecb5b597c308d5` (read-only checkout; not modified)
- Copyright: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
- License: Apache License, Version 2.0 (the same license as this distribution; full text in
  `LICENSE`). Upstream backend files carry `SPDX-License-Identifier: Apache-2.0`.
- Ported 2026-11-08 by su_multi_arm contributors (owner decision: adopt the upstream catalog and
  text-to-SQL approach). Each adapted file keeps the upstream copyright and SPDX header and adds a
  "Modified by ... 2026-11-08" note naming the upstream files and the changes:

| File in this distribution | Adapted from (upstream path) |
|---|---|
| `src/su_multi_arm/sql_surface/catalog.py` | `auto_ontology/semantic/models.py` (`ColumnAttributeSpec`, `TermProposal`, `TermAttributeAssignment`); `auto_ontology/utils/embedding_rows.py` (`build_table_text`, `build_column_text`); `auto_ontology/retrieval/text_to_sql/formatters_util.py` (`format_tables_for_prompt`, `_column_row`, `_pipe_table`, `_plain_cell`) |
| `src/su_multi_arm/sql_surface/prompts.py` | `auto_ontology/retrieval/text_to_sql/prompts.py` (`create_sql_user_prompt`, `_SQLITE_DIALECT_RULES`, `_PROJECTION_RULES`, `_SQL_GENERATION_OUTPUT_SPEC`, `create_sql_from_candidates_prompt`); `auto_ontology/retrieval/text_to_sql/agents/sql_reconstruction.py` (repair prompt) |
| `src/su_multi_arm/sql_surface/values.py` | `auto_ontology/retrieval/text_to_sql/agents/sql_value_validation.py` (`extract_value_checks`) |
| `src/su_multi_arm/sql_surface/planner.py` | `auto_ontology/retrieval/text_to_sql/agents/sql_from_semantic.py`, `agents/sql_reconstruction.py`, `agents/sql_unconstructable.py`, `retrieval/text_to_sql/models.py` (`SQLGenerationModel`); answerability idea from `mcp/auto_ontology_mcp/tools.py` (`check_answerable`) |

Not ported: the native runtime (LangChain, LangGraph, Alembic, pgvector, psycopg, SQLAlchemy),
global caches, connector registries (including the unrestricted filter returned for a malformed
scope in `connectors/registry.py`), table discovery, error classification and literal substitution.
The safety layer (`sql_surface/guard.py`, `sql_surface/executor.py`) is original to this project.

Upstream `NOTICE`-equivalent text (`OPEN_SOURCE_NOTICE`): "This distribution includes open source
which is archived at the following URL: https://opensource.nvidia.com/oss/teams/nvidia".
