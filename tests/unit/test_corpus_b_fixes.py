"""Corpus-B loss-analysis fixes (2026-11-09), reproduced on synthetic data only.

F1 SQL/typed row cells back a cited count; SQL results carry a deterministic rendering; the strip
fallback never renders an aggregate the model did not cite. F2 advisory/hypothetical completeness
phrases. F3 ranking SQL projects its ORDER BY measure and keeps LIMIT-boundary ties. F4 a share's
numerator condition counts as an applied qualifier. F5 ranking phrasings. F6 free-text projection
columns (title / text fields) matched with LIKE only. F7 complete value lists and value hints in the
SQL catalog. F8 group-key sort / NULL groups in structured ranks. F9 time-basis role labels. F10
``packing.metadata_show_null``. F11 SQL-writer ``max_tokens`` warning.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm.backends._scope import order_groups, rank_groups  # noqa: E402
from su_multi_arm.core.config_projection import ProjectionConfig, ProjectionTable  # noqa: E402
from su_multi_arm.core.config_selection import PackingConfig  # noqa: E402
from su_multi_arm.core.types import GroupRow, Predicate, ScopeResult, ScopeSpec, Sort  # noqa: E402
from su_multi_arm.decision.intent import detect_intents  # noqa: E402
from su_multi_arm.decision.scope import ScopeCompiler, ScopeDraft  # noqa: E402
from su_multi_arm.decision.values import ValueResolver  # noqa: E402
from su_multi_arm.generation.contract import AggregateEvidence, AnswerContract, markdown_rows  # noqa: E402
from su_multi_arm.generation.rendering import render_sql_rows  # noqa: E402
from su_multi_arm.generation.statements import detect_statements  # noqa: E402
from su_multi_arm.models import Answer, Claim  # noqa: E402
from su_multi_arm.projection import ProjectionBuilder, ProjectionSpec  # noqa: E402
from su_multi_arm.selection.packing import PackRecord, pack  # noqa: E402
from su_multi_arm.sql_surface import route  # noqa: E402
from su_multi_arm.sql_surface.catalog import SqlCatalog, SqlColumn, SqlTable  # noqa: E402
from su_multi_arm.sql_surface.executor import sql_catalog  # noqa: E402
from su_multi_arm.sql_surface.guard import guard  # noqa: E402
from su_multi_arm.sql_surface.planner import open_session  # noqa: E402
from su_multi_arm.sql_surface.prompts import PROJECTION_RULES, user_prompt, value_hints  # noqa: E402

import sql_surface_support as ss  # noqa: E402
import typed_support as ts  # noqa: E402

ALPHA = ss.tenant("t_alpha")
TARGETS = ["record", "records", "status", "module", "priority", "hours", "opened at", "opened", "tenant"]


@pytest.fixture(scope="module")
def world():
    return ss.world()


def execute(world, sql: str, acl=(), **limits):
    arm = ss.make_arm(world, **limits)
    session = open_session(arm, ss.context(), list(acl))
    return session.execute(sql)


def exact_sql(rows: list[dict], columns: list[str], *, ident: str = "aggregate:1", ids: list[str] | None = None) -> AggregateEvidence:
    result = ScopeResult(exactness="exact", denominator=len(rows), value=None, total=len(rows), matched_ids=ids or [])
    return AggregateEvidence(id=ident, result=result, rendering=render_sql_rows(columns, [r["values"] for r in rows], exactness="exact"), rows=rows, columns=columns)


def enforce(answer: Answer, aggregates: list[AggregateEvidence], question: str = "Which module has the second most records?"):
    intents = detect_intents(question, targets=TARGETS)
    ids = {a.id for a in aggregates} | set(answer.citations)
    return AnswerContract("strip").enforce(answer, evidence_ids=ids, aggregates=aggregates, intents=intents, question=question, targets=TARGETS)


# --- F1: row cells back a count; SQL renderings; the fallback stays on the cited aggregate ---------


def test_f1_a_ranked_sql_row_cell_backs_the_stated_count():
    sql = exact_sql([{"values": {"module": "Search", "n": 73}}], ["module", "n"])
    answer = Answer(answer="Search has the second most records, with 73 records [aggregate:1].", claims=[Claim(text="73 records", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    checked = enforce(answer, [sql])
    assert checked.contract == "passed", checked.violations


def test_f1_without_rows_the_same_answer_is_a_mismatch():
    """The pre-fix shape (no rows on the generator path): 73 is backed by nothing."""
    sql = exact_sql([{"values": {"module": "Search", "n": 73}}], ["module", "n"]).model_copy(update={"rows": []})
    answer = Answer(answer="Search has the second most records, with 73 records [aggregate:1].", claims=[Claim(text="73 records", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    assert "answer_number_mismatch" in {v.rule for v in enforce(answer, [sql]).violations}


def test_f1_a_number_no_row_holds_still_fails_and_renders_the_cited_sql_result_only():
    sql = exact_sql([{"values": {"module": "Search", "n": 73}}], ["module", "n"])
    rank = AggregateEvidence(
        id="aggregate:2",
        result=ScopeResult(exactness="exact", denominator=9, value={"None": 6}, groups=[GroupRow(key={"module": None}, value=6, count=6)], total=9),
        rendering="records by module: None: 6 (exact; 9 records matched).",
    )
    answer = Answer(answer="Search has the second most records, with 74 records [aggregate:1].", claims=[Claim(text="74 records", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    checked = enforce(answer, [sql, rank])
    assert "answer_number_mismatch" in {v.rule for v in checked.violations}
    assert checked.contract == "stripped"
    assert "module=Search, n=73" in checked.answer.answer and "None: 6" not in checked.answer.answer
    assert checked.answer.citations == ["aggregate:1"]


def test_f1_the_fallback_never_switches_to_an_uncited_aggregate():
    """A cited exact aggregate without a rendering: no rendered answer from another aggregate."""
    cited = exact_sql([{"values": {"module": "Search", "n": 73}}], ["module", "n"]).model_copy(update={"rendering": None})
    other = AggregateEvidence(id="aggregate:2", result=ScopeResult(exactness="exact", denominator=9, value=6, total=6), rendering="records: 6 (exact).")
    answer = Answer(answer="Search has 74 records [aggregate:1].", claims=[Claim(text="74 records", kind="count", evidence_ids=["aggregate:1"])], citations=["aggregate:1"])
    checked = enforce(answer, [cited, other], question="How many records are in module Search?")
    assert checked.contract == "abstained"


def test_f1_retained_id_count_backs_only_an_id_statement():
    ids = [f"tk-{i}" for i in range(8)]
    sql = exact_sql([{"values": {"n": 52}}], ["n"], ids=ids).model_copy(update={"result": ScopeResult(exactness="exact", denominator=1, value=52, total=1, matched_ids=ids)})
    claim = [Claim(text="52 records", kind="count", evidence_ids=["aggregate:1"])]
    ok = Answer(answer="There are 52 records [aggregate:1]. Only 8 contributing document IDs are listed.", claims=claim, citations=["aggregate:1"])
    assert enforce(ok, [sql], question="How many records are open?").contract == "passed"
    wrong = Answer(answer="There are 8 records [aggregate:1].", claims=claim, citations=["aggregate:1"])
    assert "answer_number_mismatch" in {v.rule for v in enforce(wrong, [sql], question="How many records are open?").violations}


def test_f1_typed_row_values_are_read_and_tables_show_cells():
    row = {"evidence": "typed_row", "document_id": "tk-1", "values": {"module": "Billing", "hours": 2.5}, "types": {}, "nulls": [], "rejected": [], "citations": []}
    sql = exact_sql([row], ["module", "hours"])
    table = markdown_rows(sql)
    assert "| module | hours |" in table and "| Billing | 2\\.5 |" in table


def test_f1_executed_sql_carries_a_rendering(world):
    sql = "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC LIMIT 1"
    evidence = execute(world, sql, sql_surface={"group_lineage_max": 0})
    assert evidence["rendering"] == "SQL result: module=Billing, n=4 (exact; 1 row)."
    # B7 (default group_lineage_max 10): a small group also lists its records
    grouped = execute(world, sql)
    assert grouped["rendering"] == "SQL result: module=Billing, n=4 (records: tk-1, tk-2, tk-4, tk-6) (exact; 1 row; the records listed for a row are all of its contributing records)."
    refused = execute(world, "SELECT nope FROM ticket")
    assert refused.get("refused") and "rendering" not in refused


# --- F2: advisory / hypothetical completeness ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "There are **270** records without a value. **Getting the full list would require** a records export (advice, not part of the answer).",
        "Obtaining the complete list requires a records export.",
        "The full list could be produced with an export.",
        "To see all of them, run an export of the records.",
        "The complete list (advice: export the records) is not part of this answer.",
    ],
)
def test_f2_advice_and_hypotheticals_are_not_completeness_claims(text):
    statements = detect_statements(text, units="records", list_intent=False)
    assert "exhaustive_list" not in {s.kind for s in statements}


@pytest.mark.parametrize("text", ["Here is the complete list: A, B and C.", "The full list is: A, B, C.", "Getting the full list was easy: these are all the records."])
def test_f2_real_completeness_claims_stay_detected(text):
    statements = detect_statements(text, units="records", list_intent=True)
    assert "exhaustive_list" in {s.kind for s in statements}


# --- F3: ranking measure projection and LIMIT-boundary ties --------------------------------------


def test_f3_the_order_by_measure_is_projected(world):
    evidence = execute(world, "SELECT module FROM ticket GROUP BY module ORDER BY COUNT(*) DESC LIMIT 1")
    assert evidence["columns"] == ["module", "sort_value"] and evidence["sort_projected"] == ["sort_value"]
    # B7: the small group (4 records) also lists its contributors
    assert evidence["rows"] == [{"values": {"module": "Billing", "sort_value": 4}, "contributor_ids": ["tk-1", "tk-2", "tk-4", "tk-6"]}]
    assert evidence["exactness"] == "exact"


def test_f3_a_projected_measure_is_not_added_twice(world):
    evidence = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY COUNT(*) DESC LIMIT 1")
    assert evidence["columns"] == ["module", "n"] and evidence["sort_projected"] == []


@pytest.mark.parametrize("order", ["n ASC", "n ASC, module DESC"])
def test_f3_ties_at_the_limit_are_kept_and_reported(world, order):
    evidence = execute(world, f"SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY {order} LIMIT 1")
    rows = evidence["rows"]
    assert {r["values"]["module"] for r in rows} == {"Reports", "Search"} and [r.get("tie", False) for r in rows] == [False, True]
    assert evidence["sql_ties"] == {"columns": ["n"], "limit": 1, "after_limit": 1, "before_offset": False, "complete": True}
    assert evidence["exactness"] == "exact" and "tie the last row within the LIMIT" in evidence["rendering"]


def test_f3_a_tie_with_the_row_before_the_offset_is_reported(world):
    evidence = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n ASC LIMIT 1 OFFSET 1")
    assert len(evidence["rows"]) == 1 and evidence["rows"][0]["values"]["n"] == 1
    assert evidence["sql_ties"]["before_offset"] is True and evidence["sql_ties"]["after_limit"] == 0
    second = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC LIMIT 1 OFFSET 1")
    assert second["sql_ties"]["before_offset"] is False and second["sql_ties"]["after_limit"] == 1 and len(second["rows"]) == 2


def test_f3_ties_past_the_window_are_truncated_and_bounded(world):
    evidence = execute(world, "SELECT id FROM ticket GROUP BY id ORDER BY COUNT(*) LIMIT 1", row_limit=3)
    assert evidence["truncated"] is True and evidence["exactness"] == "bounded"
    assert evidence["sql_ties"]["complete"] is False
    assert any(u["reason"] == "result_truncated" for u in evidence["unsupported"])


def test_f3_tenant_binding_holds_for_tie_rows(world):
    evidence = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n ASC LIMIT 1", acl=ALPHA)
    assert {r["values"]["module"] for r in evidence["rows"]} == {"Search"}  # t_beta's Reports never appears


def test_f3_row_level_and_unlimited_statements_are_unchanged(world):
    rows = execute(world, "SELECT id FROM ticket ORDER BY priority LIMIT 1", acl=ALPHA)
    assert rows["sql_ties"] is None and len(rows["rows"]) == 1
    grouped = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC")
    assert grouped["sql_ties"] is None and len(grouped["rows"]) == 3


def test_f3_prompt_asks_for_the_measure_and_no_arbitrary_tie_break():
    assert "SELECT the ranked item AND the value it is ranked by" in PROJECTION_RULES
    assert "Never break a tie arbitrarily" in PROJECTION_RULES


# --- F4: a share's numerator condition is an applied qualifier -----------------------------------


@pytest.fixture(scope="module")
def setting(world):
    arm = ss.make_arm(world)
    _, catalog, resolver = sql_catalog(arm, ss.context(), ALPHA)
    return arm, catalog, resolver


def positions(setting, sql: str) -> list[str]:
    _, catalog, resolver = setting
    return [c["filter"] for c in guard(sql, catalog, resolver, acl=ALPHA).checked if c["column"] == "ticket.status"]


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT module, CAST(COUNT(CASE WHEN status = 'open' THEN 1 END) AS REAL) / COUNT(*) AS r FROM ticket GROUP BY module", "numerator"),
        ("SELECT module, SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) * 1.0 / COUNT(*) AS r FROM ticket GROUP BY module", "numerator"),
        ("SELECT module, AVG(IIF(status = 'open', 1, 0)) AS r FROM ticket GROUP BY module", "numerator"),
        ("SELECT module FROM ticket GROUP BY module ORDER BY SUM(CASE WHEN status <> 'open' THEN 1 ELSE 0 END) DESC", "numerator_negated"),
        ("SELECT module, COUNT(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS r FROM ticket GROUP BY module", "other"),
        ("SELECT module, SUM(CASE WHEN status = 'open' THEN 1 ELSE 2 END) AS r FROM ticket GROUP BY module", "other"),
        ("SELECT module, COUNT(CASE WHEN status = 'open' OR priority = 1 THEN 1 END) AS r FROM ticket GROUP BY module", "other"),
        # 2026-11-09 (B5 q043): a derived table that is the outer statement's only source counts
        # (guard ``_effective_roots``); one beside a join or in a filter subquery stays ``other``
        ("SELECT m, r FROM (SELECT module AS m, COUNT(CASE WHEN status = 'open' THEN 1 END) AS r FROM ticket GROUP BY module) AS s", "numerator"),
        ("SELECT s.m, s.r FROM (SELECT module AS m, COUNT(CASE WHEN status = 'open' THEN 1 END) AS r FROM ticket GROUP BY module) AS s JOIN asset AS a ON a.module = s.m", "other"),
        ("SELECT module, CASE WHEN status = 'open' THEN 1 END AS flag FROM ticket", "other"),
    ],
)
def test_f4_numerator_positions(setting, sql, expected):
    assert positions(setting, sql) == [expected]


def test_f4_executed_unapplied_accepts_a_numerator_only_for_a_share_question(world, setting):
    arm, _, resolver = setting
    sql = "SELECT module, CAST(COUNT(CASE WHEN status = 'open' THEN 1 END) AS REAL) / COUNT(*) AS r FROM ticket GROUP BY module ORDER BY r DESC LIMIT 1"
    evidence = execute(world, sql, acl=ALPHA)
    share_q = "Which module has the highest ratio of open records?"
    share = route.executed_unapplied(evidence, share_q, {}, arm.schema, resolver, detect_intents(share_q, targets=TARGETS))
    assert share == []
    rank_q = "Which module has the most open records?"
    rank = route.executed_unapplied(evidence, rank_q, {}, arm.schema, resolver, detect_intents(rank_q, targets=TARGETS))
    assert any(u.reason == "qualifier_not_applied" for u in rank)


# --- F5: ranking phrasings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Which tenant spans the most distinct module values?",
        "Which customer has the most open+pending records overall?",
        "Who is second for open/pending record volume?",
        "Which module appears most often among priority 1 records?",
        "What are the next two highest ratios among tenants with at least 2 records?",
        "Which module has the second-most records?",
    ],
)
def test_f5_ranking_phrasings_are_rank_intents(question):
    assert "rank" in detect_intents(question, targets=TARGETS).kinds()


@pytest.mark.parametrize("question", ["How do I reset the module settings?", "What does the most recent note say?", "The tenant shares its records with partners."])
def test_f5_ordinary_prose_stays_retrieval(question):
    assert detect_intents(question, targets=TARGETS).kinds() == []


def test_f5_plural_ratio_is_a_share_intent():
    assert "share" in detect_intents("What are the next two highest open ratios among tenants?", targets=TARGETS).kinds()


# --- F6: free-text projection columns -----------------------------------------------------------

TEXT_PROJECTION = {
    **ts.PROJECTION,
    "tables": {
        **ts.PROJECTION["tables"],
        "ticket": {
            **ts.PROJECTION["tables"]["ticket"],
            "text_columns": {
                "subject": {"source": "title", "description": "Ticket subject line."},
                "body": {"source": "fields.body", "description": "Original customer description."},
            },
        },
    },
}


@pytest.fixture(scope="module")
def text_world():
    from su_multi_arm.backends.memory import MemoryBackend

    schema = ts.schema()
    backend = MemoryBackend()
    docs = ts.documents()
    backend.records.upsert(ss.GEN, ts.records(docs), schema)
    spec = ProjectionSpec.build(ProjectionConfig.model_validate(TEXT_PROJECTION), schema, "tenant")
    builder = ProjectionBuilder(spec, schema)
    builder.add(ts.records(docs), docs)
    files, manifest = builder.seal(ss.GEN)
    ProjectionBuilder.write(backend.relational, ss.GEN, files, manifest)
    return backend, schema, spec


def text_session(text_world, acl=ALPHA):
    from su_multi_arm.arms.typed_query import TypedQueryArm, TypedQueryRuntime
    from su_multi_arm.core.component import Dependencies

    backend, schema, _ = text_world
    config = TypedQueryRuntime(backend="m", records="m", projection=ProjectionConfig.model_validate(TEXT_PROJECTION), access_column="tenant", mode="sql_surface", row_limit=50, max_groups=100)
    arm = TypedQueryArm(config, Dependencies(backends={"m": backend}, components={"catalog": backend.catalog}, metadata_schema=schema))
    return open_session(arm, ss.context(), list(acl))


def test_f6_text_columns_validate():
    with pytest.raises(ValueError, match="text column"):
        ProjectionTable.model_validate({"kind": "ticket", "columns": ["status"], "text_columns": {"status": {"source": "title", "description": "x"}}})
    with pytest.raises(ValueError):
        ProjectionTable.model_validate({"kind": "ticket", "text_columns": {"subject": {"source": "metadata.title", "description": "x"}}})
    with pytest.raises(ValueError):
        ProjectionTable.model_validate({"kind": "ticket", "text_columns": {"subject": {"source": "title"}}})
    with pytest.raises(ValueError, match="shadows"):
        ProjectionSpec.build(ProjectionConfig.model_validate({**TEXT_PROJECTION, "tables": {"ticket": {"kind": "ticket", "columns": ["tenant"], "text_columns": {"module": {"source": "title", "description": "x"}}}}, "relations": {}}), ts.schema(), "tenant")


def test_f6_spec_fingerprint_is_unchanged_without_text_columns():
    plain = ProjectionSpec.build(ProjectionConfig.model_validate(ts.PROJECTION), ts.schema(), "tenant")
    texts = ProjectionSpec.build(ProjectionConfig.model_validate(TEXT_PROJECTION), ts.schema(), "tenant")
    assert all(not t.text_sources for t in plain.tables.values()) and plain.fingerprint != texts.fingerprint
    assert texts.tables["ticket"].text_sources == {"subject": "title", "body": "fields.body"}


def test_f6_builder_needs_the_documents_for_text_columns(text_world):
    _, schema, spec = text_world
    with pytest.raises(ValueError, match="without its document"):
        ProjectionBuilder(spec, schema).add(ts.records(ts.documents()))


def test_f6_like_on_a_text_column_counts_mentions_per_tenant(text_world):
    session = text_session(text_world)
    evidence = session.execute("SELECT COUNT(*) AS n FROM ticket WHERE body LIKE '%billing%' OR subject LIKE '%Ticket 3%'")
    assert evidence["rows"] == [{"values": {"n": 3}}] and evidence["exactness"] == "exact"  # tk-1, tk-2 (billing) and tk-3 (subject)
    assert {p["column"] for p in evidence["sql_surface"]["text_patterns"]} == {"ticket.subject", "ticket.body"}
    beta = text_session(text_world, ss.tenant("t_beta")).execute("SELECT COUNT(*) AS n FROM ticket WHERE body LIKE '%billing%'")
    assert beta["rows"] == [{"values": {"n": 1}}]  # tk-4 only: the tenant file


def test_f6_text_columns_refuse_equality_and_grouping_values(text_world):
    session = text_session(text_world)
    refused = session.execute("SELECT COUNT(*) AS n FROM ticket WHERE subject = 'Ticket 1'")
    assert refused["refused"] and refused["sql_surface"]["error"]["code"] == "free_text_equality"
    column = session.catalog.tables["ticket"].column("subject")
    assert column is not None and column.sample_values == [] and "free text" in column.description and not column.groupable


def test_f6_a_quoted_phrase_is_applied_by_a_like_pattern(text_world):
    session = text_session(text_world)
    evidence = session.execute("SELECT COUNT(*) AS n FROM ticket WHERE body LIKE '%about Billing%'")
    question = 'How many records mention "about Billing"?'
    schema = text_world[1]
    resolver = ValueResolver(schema, text_world[0].records, ss.GEN, ALPHA)
    missing = route.executed_unapplied(evidence, question, {}, schema, resolver, detect_intents(question, targets=TARGETS))
    assert not any(u.constraint_text == "about Billing" for u in missing)


# --- F7: complete value lists and value hints ---------------------------------------------------


def test_f7_listable_columns_show_every_value_marked_complete(world):
    arm = ss.make_arm(world, sql_surface={"sample_values": 1, "list_values_up_to": 64})
    _, catalog, _ = sql_catalog(arm, ss.context(), ALPHA)
    module = catalog.tables["ticket"].column("module")
    assert module.sample_values == ["Billing", "Search"] and module.values_complete  # ACL-scoped: no t_beta "Reports"
    assert catalog.tables["ticket"].column("status").sample_values == ["open", "closed", "pending"]
    assert catalog.tables["ticket"].column("tenant").sample_values == []  # the access column is withheld
    assert '"Billing", "Search" (all values)' in catalog.render()


def test_f7_over_the_listing_cap_only_samples_are_shown(world):
    arm = ss.make_arm(world, sql_surface={"sample_values": 1, "list_values_up_to": 1})
    _, catalog, _ = sql_catalog(arm, ss.context(), ALPHA)
    module = catalog.tables["ticket"].column("module")
    assert module.sample_values == ["Billing"] and not module.values_complete
    disabled = ss.make_arm(world, sql_surface={"sample_values": 0})
    _, off, _ = sql_catalog(disabled, ss.context(), ALPHA)
    assert off.tables["ticket"].column("module").sample_values == []


def _catalog(values: list[str]) -> SqlCatalog:
    column = SqlColumn(source_column="rca", name="rca", display_name="Rca", datatype="TEXT", field_type="string", source_field="rca", sample_values=values, samples="observed", values_complete=True)
    table = SqlTable(name="ticket", kind="ticket", display_name="Ticket", columns=[column])
    return SqlCatalog(generation="g", projection_fingerprint="p", acl_key="k", tables={"ticket": table})


def test_f7_value_hints_map_question_words_to_stored_values():
    catalog = _catalog(["general__double_charge", "report_builder_1", "data_import_1", "misc"])
    hints = value_hints("Which modules have the most double-charge cases for the Report Builder?", catalog)
    assert hints == ['- "double charge" may mean ticket.rca = "general__double_charge"', '- "report builder" may mean ticket.rca = "report_builder_1"']
    assert value_hints("How many records are there?", catalog) == []
    assert "VALUE HINTS (stored values" in user_prompt("double charge counts", catalog, row_limit=10)
    assert "VALUE HINTS (stored values" not in user_prompt("How many records?", catalog, row_limit=10)


# --- F8: structured rank semantics --------------------------------------------------------------


def _groups() -> list[GroupRow]:
    return [GroupRow(key={"module": None}, value=9, count=9), GroupRow(key={"module": "Billing"}, value=5, count=5), GroupRow(key={"module": "Search"}, value=3, count=3)]


def test_f8_a_null_group_is_never_ranked_as_a_value():
    kept, ties = rank_groups(ScopeSpec(group_by=["module"], top_n=1), _groups())
    assert [g.key["module"] for g in kept] == ["Billing"] and kept[0].rank == 1 and ties == []
    listed, _ = rank_groups(ScopeSpec(group_by=["module"], order="desc"), _groups())
    assert [g.key["module"] for g in listed] == ["Billing", "Search", None] and listed[-1].rank is None


def test_f8_a_null_group_is_ranked_when_the_scope_asks_for_missing_values():
    scope = ScopeSpec(group_by=["module"], top_n=1, predicates=[Predicate(field="module", op="missing")])
    kept, _ = rank_groups(scope, _groups())
    assert kept[0].key["module"] is None


def test_f8_group_key_sort_puts_null_last_and_drops_it_from_a_cut():
    groups, _, _ = order_groups(ScopeSpec(group_by=["module"], sort=Sort(field="module", descending=True)), _groups())
    assert [g.key["module"] for g in groups] == ["Search", "Billing", None]
    top, _, _ = order_groups(ScopeSpec(group_by=["module"], sort=Sort(field="module", descending=True), top_n=1), _groups())
    assert [g.key["module"] for g in top] == ["Search"]


def test_f8_compiler_refuses_a_top_n_sorted_by_the_group_key(world):
    schema = ts.schema()
    compiler = ScopeCompiler(schema, ValueResolver(schema, world[0].records, ss.GEN, ALPHA))
    compiled = compiler.compile(ScopeDraft(group_by=["module"], top_n=1, sort=Sort(field="module", descending=True)))
    assert compiled.spec is None and compiled.unsupported[0].reason == "measure_unsupported"
    by_count = compiler.compile(ScopeDraft(group_by=["module"], top_n=1, sort=Sort(field="count", descending=True)))
    assert by_count.spec is not None and by_count.spec.order == "desc"


# --- F9: time-basis role labels -----------------------------------------------------------------


def test_f9_a_time_basis_label_names_its_field(world):
    schema = ts.schema()
    compiler = ScopeCompiler(schema, ValueResolver(schema, world[0].records, ss.GEN, ALPHA))
    compiled = compiler.compile(ScopeDraft(time_basis="opened"))
    assert compiled.spec is not None and compiled.spec.time_basis == "opened_at"
    unknown = compiler.compile(ScopeDraft(time_basis="resolved"))
    assert unknown.spec is None and unknown.unsupported[0].reason == "measure_unsupported"


# --- F10: packed null metadata ------------------------------------------------------------------


def test_f10_metadata_show_null_packs_an_empty_field():
    record = PackRecord(id="tk-1", fields={"body": "text"}, metadata={"status": "open"})
    config = PackingConfig(metadata_fields=["status", "resolution"], metadata_show_null=["resolution"])
    packed = pack([record], config).evidence[0]
    assert packed.metadata == {"status": "open", "resolution": None}
    plain = pack([record], PackingConfig(metadata_fields=["status", "resolution"])).evidence[0]
    assert plain.metadata == {"status": "open"}
    with pytest.raises(ValueError, match="metadata_show_null"):
        PackingConfig(metadata_fields=["status"], metadata_show_null=["resolution"])


# --- F11: SQL-writer output ceiling -------------------------------------------------------------


def test_f11_a_low_sql_writer_max_tokens_is_a_validate_warning(tmp_path):
    from su_multi_arm.core.config import validate_config
    from su_multi_arm.pipeline.validate import config_diagnostics

    data = ts.config_dict(tmp_path / "state")
    data["pipeline"]["decision"] = {"kind": "llm", "typed_query": {"enabled": True}}
    data["pipeline"]["arms"]["typed_query"].update({"mode": "sql_surface", "sql_surface": {"role": "query_writer"}})
    data.setdefault("models", {}).setdefault("roles", {}).update({"planner": {"provider": "openai_compatible", "model": "fake"}, "query_writer": {"provider": "openai_compatible", "model": "fake", "max_tokens": 1500}})
    codes = {d.code for d in config_diagnostics(validate_config(data))}
    assert "sql_writer_max_tokens_low" in codes
    data["models"]["roles"]["query_writer"]["max_tokens"] = 3000
    assert "sql_writer_max_tokens_low" not in {d.code for d in config_diagnostics(validate_config(data))}
