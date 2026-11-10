"""OR groups of text matches in executed SQL (2026-11-09, corpus-B B4 q037/q041/q044 shapes).

``(subject LIKE '%x%' OR body LIKE '%x%')`` as a conjunct of the outermost ``WHERE`` is reported as
``filter: any_of`` (with a group number) by the guard; the route then applies the question's quoted
phrase. A group mixed with a non-text disjunct, one under ``NOT`` or inside a subquery stays
``other``. Synthetic two-tenant projection only.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm.core.config_projection import ProjectionConfig  # noqa: E402
from su_multi_arm.decision.intent import detect_intents  # noqa: E402
from su_multi_arm.decision.values import ValueResolver  # noqa: E402
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec  # noqa: E402
from su_multi_arm.sql_surface import route  # noqa: E402
from su_multi_arm.sql_surface.planner import open_session  # noqa: E402

import sql_surface_support as ss  # noqa: E402
import typed_support as ts  # noqa: E402

ALPHA = ss.tenant("t_alpha")
TARGETS = ["record", "records", "status", "module", "priority", "hours", "opened at", "opened", "tenant"]
PROJECTION = {
    **ts.PROJECTION,
    "tables": {
        **ts.PROJECTION["tables"],
        "ticket": {
            **ts.PROJECTION["tables"]["ticket"],
            "text_columns": {"subject": {"source": "title", "description": "Ticket subject line."}, "body": {"source": "fields.body", "description": "Customer description."}},
        },
    },
}


@pytest.fixture(scope="module")
def world():
    from su_multi_arm.backends.memory import MemoryBackend

    schema = ts.schema()
    backend = MemoryBackend()
    docs = ts.documents()
    backend.records.upsert(ss.GEN, ts.records(docs), schema)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(PROJECTION), schema, "tenant")
    builder = ProjectionBuilder(spec, schema)
    builder.add(ts.records(docs), docs)
    files, manifest = builder.seal(ss.GEN)
    ProjectionBuilder.write(backend.relational, ss.GEN, files, manifest)
    return backend, schema


def execute(world, sql: str) -> dict:
    from su_multi_arm.arms.typed_query import TypedQueryArm, TypedQueryRuntime
    from su_multi_arm.core.component import Dependencies

    backend, schema = world
    config = TypedQueryRuntime(backend="m", records="m", projection=ProjectionConfig.model_validate(PROJECTION), access_column="tenant", mode="sql_surface", row_limit=50, max_groups=100)
    arm = TypedQueryArm(config, Dependencies(backends={"m": backend}, components={"catalog": backend.catalog}, metadata_schema=schema))
    return open_session(arm, ss.context(), list(ALPHA)).execute(sql)


def filters(evidence: dict) -> list[tuple[str, str, int | None]]:
    return sorted((p["column"], p["filter"], p.get("group")) for p in evidence["sql_surface"]["text_patterns"])


def test_an_or_group_of_text_matches_is_any_of(world):
    evidence = execute(world, "SELECT COUNT(*) AS n FROM ticket WHERE status = 'open' AND (subject LIKE '%billing%' OR body LIKE '%billing%')")
    assert evidence["exactness"] == "exact" and evidence["rows"] == [{"values": {"n": 1}}]  # tk-1 (alpha, open, Billing)
    assert filters(evidence) == [("ticket.body", "any_of", 1), ("ticket.subject", "any_of", 1)]


@pytest.mark.parametrize(
    "where",
    [
        "status = 'open' OR body LIKE '%billing%'",  # a non-text disjunct
        "NOT (subject LIKE '%billing%' OR body LIKE '%billing%')",  # negated group
        "(subject LIKE '%billing%' OR body LIKE '%billing%') OR status = 'closed'",  # the group is not a conjunct
    ],
)
def test_other_or_shapes_stay_other(world, where):
    evidence = execute(world, f"SELECT COUNT(*) AS n FROM ticket WHERE {where}")
    assert {f for _, f, _ in filters(evidence)} == {"other"}


def test_the_route_applies_the_quoted_phrase_of_an_any_of_group(world):
    backend, schema = world
    resolver = ValueResolver(schema, backend.records, ss.GEN, ALPHA)
    evidence = execute(world, "SELECT COUNT(*) AS n FROM ticket WHERE subject LIKE '%about Billing%' OR body LIKE '%about Billing%'")
    question = "How many records contain the phrase 'about Billing'?"
    missing = route.executed_unapplied(evidence, question, {}, schema, resolver, detect_intents(question, targets=TARGETS))
    assert not any(u.constraint_text == "about Billing" for u in missing)
    other = "How many records contain the phrase 'about Search'?"  # the group matches another phrase
    missing = route.executed_unapplied(evidence, other, {}, schema, resolver, detect_intents(other, targets=TARGETS))
    assert any(u.constraint_text == "about Search" for u in missing)
