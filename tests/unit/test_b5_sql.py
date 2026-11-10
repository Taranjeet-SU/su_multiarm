"""Corpus-B rerun B5 SQL-route shapes (2026-11-09) on a synthetic one-tenant ticket projection.

* q020: ``WHERE organization = 'Ledgerly'`` executes as ``module = 'market_place__ledgerly'`` only
  when the user's question grounds it (and does not name ``organization``); recorded on the checked
  value. Case/punctuation/plural spellings execute the stored value.
* q041: a value on the wrong field + a status filter + an OR group of LIKEs over the text columns
  compiles, applies every question qualifier and is exact.
* q043: a ratio ranking whose conditional numerator sits in a derived table (or a CTE) that is the
  outer statement's only source is ``filter: numerator`` (applied); the derived table's ``WHERE`` is a
  positive conjunct; the tie at the LIMIT boundary is reported.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm.backends._scope import document_values  # noqa: E402
from su_multi_arm.backends.memory import MemoryBackend  # noqa: E402
from su_multi_arm.core.config_projection import ProjectionConfig  # noqa: E402
from su_multi_arm.core.types import Document, FieldSpec, MetadataSchema, Predicate, Provenance, StructuredRecord  # noqa: E402
from su_multi_arm.decision.intent import detect_intents  # noqa: E402
from su_multi_arm.decision.values import ValueResolver  # noqa: E402
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec  # noqa: E402
from su_multi_arm.sql_surface import route  # noqa: E402
from su_multi_arm.sql_surface.planner import open_session  # noqa: E402

import sql_surface_support as ss  # noqa: E402

ACL = [Predicate(field="tenant", op="eq", value="t_one")]
TARGETS = ["ticket", "tickets", "record", "records", "status", "priority", "module", "organization"]
FIELDS: list[dict[str, Any]] = [
    {"name": "tenant", "description": "Owning tenant key (access column).", "type": "string", "listable": True, "groupable": True},
    {"name": "status", "description": "Workflow status.", "type": "string", "listable": True, "groupable": True, "casefold_match": True},
    {"name": "priority", "description": "Priority set on the ticket.", "type": "string", "listable": True, "groupable": True, "casefold_match": True},
    {"name": "module", "description": "Product module.", "type": "string", "listable": True, "groupable": True, "casefold_match": True},
    {"name": "organization", "description": "Customer organization.", "type": "string", "listable": True, "groupable": True, "max_catalog_cardinality": 200},
]
PROJECTION: dict[str, Any] = {
    "enabled": True,
    "backend": "local",
    "tables": {
        "ticket": {
            "kind": "ticket",
            "columns": ["tenant", "status", "priority", "module", "organization"],
            "text_columns": {"subject": {"source": "title", "description": "Subject line."}, "body": {"source": "fields.body", "description": "Description."}},
        }
    },
}
# organization -> (tickets, urgent); module ledgerly on the first three of Acme and Globex
PLAN = {"Acme Corp": (6, 3), "Globex LLC": (5, 1), "Initech": (5, 1), "Umbrella Group": (2, 2), "Hooli": (4, 0)}
SUBJECTS = ["Sync is intermittent", "Connector misconfigured", "Export failing", "Billing question", "Login slow", "Report layout"]


def documents() -> list[Document]:
    docs, n = [], 0
    for org, (total, urgent) in PLAN.items():
        for k in range(total):
            n += 1
            module = "market_place__ledgerly" if org in {"Acme Corp", "Globex LLC"} and k < 3 else ("llms" if k == 3 else "search")
            if org == "Hooli" and k == 0:
                module = "market_place__chartwise"
            status = "solved" if k == 1 and org == "Globex LLC" else "open"
            if org == "Initech" and k < 3:
                status = ("new", "pending", "hold")[k]
            metadata = {"tenant": "t_one", "status": status, "priority": "urgent" if k < urgent else "normal", "module": module, "organization": org}
            docs.append(Document(id=f"t-{n:03d}", dataset="demo", source="tracker", kind="ticket", title=SUBJECTS[k % len(SUBJECTS)], fields={"body": f"synthetic ticket {n}"}, metadata=metadata))
    return docs


def schema() -> MetadataSchema:
    return MetadataSchema(canonical_unit="ticket", fields=[FieldSpec.model_validate(f) for f in FIELDS])


def records(docs: list[Document]) -> list[StructuredRecord]:
    return [StructuredRecord(schema_id="document", id=d.id, values=document_values(d), provenance=Provenance(extractor="catalog")) for d in docs]


@pytest.fixture(scope="module")
def world():
    sch = schema()
    backend = MemoryBackend()
    docs = documents()
    backend.records.upsert(ss.GEN, records(docs), sch)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(PROJECTION), sch, "tenant")
    builder = ProjectionBuilder(spec, sch)
    builder.add(records(docs), docs)
    files, manifest = builder.seal(ss.GEN)
    ProjectionBuilder.write(backend.relational, ss.GEN, files, manifest)
    return backend, sch


def execute(world, sql: str, question: str | None = None) -> dict:
    from su_multi_arm.arms.typed_query import TypedQueryArm, TypedQueryRuntime
    from su_multi_arm.core.component import Dependencies

    backend, sch = world
    config = TypedQueryRuntime(backend="m", records="m", projection=ProjectionConfig.model_validate(PROJECTION), access_column="tenant", mode="sql_surface", row_limit=50, max_groups=100)
    arm = TypedQueryArm(config, Dependencies(backends={"m": backend}, components={"catalog": backend.catalog}, metadata_schema=sch))
    return open_session(arm, ss.context(), list(ACL), grounding=question).execute(sql)


def unapplied(world, evidence: dict, question: str) -> list:
    backend, sch = world
    resolver = ValueResolver(sch, backend.records, ss.GEN, ACL)
    return route.executed_unapplied(evidence, question, {}, sch, resolver, detect_intents(question, targets=TARGETS))


# --- q020 -----------------------------------------------------------------------------------------

Q020 = "How many belong to Ledgerly?"
SQL_020 = "SELECT COUNT(*) AS tickets FROM ticket WHERE organization = 'Ledgerly'"


def test_q020_the_value_moves_to_the_field_the_question_grounds_and_is_exact(world):
    evidence = execute(world, SQL_020, Q020)
    assert not evidence.get("refused"), evidence
    assert evidence["value"] == 6 and evidence["exactness"] == "exact"
    checked = evidence["sql_surface"]["checked_values"]
    assert checked == [{"column": "ticket.module", "op": "eq", "value": "market_place__ledgerly", "grounded_by": "observed", "as_written": "Ledgerly", "rule": "question_field:token", "column_as_written": "ticket.organization", "filter": "positive"}]
    assert '"module"' in evidence["sql_surface"]["sql"] and "organization" not in evidence["sql_surface"]["sql"]
    assert unapplied(world, evidence, Q020) == []


@pytest.mark.parametrize("question", [None, "How many tickets from the organization Ledgerly?", "How many belong to that app?"])
def test_q020_without_grounding_the_value_is_refused(world, question):
    evidence = execute(world, SQL_020, question)
    assert evidence["refused"] and evidence["sql_surface"]["error"]["code"] == "value_not_observed"


def test_a_value_under_a_function_or_a_derived_projection_never_moves(world):
    for sql in ("SELECT COUNT(*) AS n FROM ticket WHERE lower(organization) = 'ledgerly'", "SELECT COUNT(*) AS n FROM (SELECT organization AS o FROM ticket) AS d WHERE d.o = 'Ledgerly'"):
        evidence = execute(world, sql, Q020)
        assert evidence["refused"], sql


def test_spellings_execute_the_stored_value(world):
    evidence = execute(world, "SELECT COUNT(*) AS n FROM ticket WHERE module = 'LLM' AND organization = 'acme corp'", None)
    assert evidence["value"] == 1
    rules = {c["column"]: c.get("rule") for c in evidence["sql_surface"]["checked_values"]}
    assert rules == {"ticket.module": "plural", "ticket.organization": "normalized"}


def test_an_ambiguous_spelling_is_refused(world):
    evidence = execute(world, "SELECT COUNT(*) AS n FROM ticket WHERE module = 'market place'", None)
    assert evidence["refused"] and evidence["sql_surface"]["error"]["code"] == "value_not_observed"  # two words, no whole value
    # "market" is a word of market_place__ledgerly and market_place__chartwise: never a pick
    evidence = execute(world, "SELECT COUNT(*) AS n FROM ticket WHERE module = 'market'", None)
    assert evidence["refused"] and evidence["sql_surface"]["error"]["code"] == "ambiguous_value"
    assert evidence["unsupported"][0]["reason"] == "value_not_observed"


# --- q041 -----------------------------------------------------------------------------------------

Q041 = "Among active Ledgerly tickets, how many literally mention 'intermittent,' 'misconfigured,' or 'failing'?"
SQL_041 = (
    "SELECT COUNT(*) AS tickets FROM ticket WHERE organization = 'Ledgerly' AND status IN ('new', 'open', 'pending', 'hold') AND "
    "(subject LIKE '%intermittent%' OR subject LIKE '%misconfigured%' OR subject LIKE '%failing%' OR body LIKE '%intermittent%' OR body LIKE '%misconfigured%' OR body LIKE '%failing%')"
)


def test_q041_multi_term_mention_status_and_moved_value_apply_and_are_exact(world):
    evidence = execute(world, SQL_041, Q041)
    assert not evidence.get("refused"), evidence
    # ledgerly tickets: Acme k=0,1,2 (open), Globex k=0,2 open, k=1 solved -> 5 active, all subjects match
    assert evidence["value"] == 5 and evidence["exactness"] == "exact"
    assert {p["filter"] for p in evidence["sql_surface"]["text_patterns"]} == {"any_of"}
    assert unapplied(world, evidence, Q041) == []


def test_q041_an_and_question_over_the_or_group_stays_bounded(world):
    question = "Among active Ledgerly tickets, how many mention both 'intermittent' and 'failing'?"
    sql = "SELECT COUNT(*) AS n FROM ticket WHERE organization = 'Ledgerly' AND (subject LIKE '%intermittent%' OR subject LIKE '%failing%')"
    evidence = execute(world, sql, question)
    assert {u.constraint_text for u in unapplied(world, evidence, question)} == {"intermittent", "failing"}


# --- q043 -----------------------------------------------------------------------------------------

Q043 = "What are the next two highest urgent ratios among organizations with at least 4 tickets?"
DERIVED = (
    "SELECT r.organization, r.ratio FROM (SELECT organization, CAST(COUNT(CASE WHEN priority = 'urgent' THEN 1 END) AS REAL) / COUNT(*) AS ratio "
    "FROM ticket WHERE organization IS NOT NULL AND status = 'open' GROUP BY organization HAVING COUNT(*) >= 4) AS r ORDER BY r.ratio DESC LIMIT 2"
)


def test_q043_a_numerator_in_a_derived_table_is_applied(world):
    evidence = execute(world, DERIVED, Q043)
    assert not evidence.get("refused"), evidence
    checked = {c["value"]: c["filter"] for c in evidence["sql_surface"]["checked_values"]}
    assert checked == {"urgent": "numerator", "open": "positive"}
    assert evidence["exactness"] == "exact" and unapplied(world, evidence, Q043) == []
    rows = [r["values"]["organization"] for r in evidence["rows"]]
    assert rows[0] == "Acme Corp" and evidence["sql_ties"]["columns"] == ["ratio"]


def test_q043_a_numerator_in_a_cte_read_as_the_only_source_is_applied(world):
    sql = (
        "WITH r AS (SELECT organization, CAST(COUNT(CASE WHEN priority = 'urgent' THEN 1 END) AS REAL) / COUNT(*) AS ratio FROM ticket "
        "GROUP BY organization HAVING COUNT(*) >= 4) SELECT organization, ratio FROM r ORDER BY ratio DESC LIMIT 3"
    )
    evidence = execute(world, sql, Q043)
    assert [c["filter"] for c in evidence["sql_surface"]["checked_values"]] == ["numerator"]
    ratios = [round(r["values"]["ratio"], 3) for r in evidence["rows"]]
    assert ratios[:3] == [0.5, 0.2, 0.2]  # Globex/Initech tie at 0.2 (Umbrella has 2 tickets: excluded)


def test_a_derived_table_beside_a_join_or_a_filter_subquery_stays_other(world):
    sql = "SELECT COUNT(*) AS n FROM ticket AS t WHERE t.organization IN (SELECT organization FROM ticket WHERE priority = 'urgent')"
    evidence = execute(world, sql, "How many urgent tickets are there?")
    assert {c["value"]: c["filter"] for c in evidence["sql_surface"]["checked_values"]} == {"urgent": "other"}
