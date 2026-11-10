"""Corpus-B B7 answer-quality fixes (2026-11-09), reproduced on synthetic data only.

1. QA-20261109-07: a singular possessive ("one ticket's", "a record's") is not a count statement.
2. QA-20261109-08: strip keeps Markdown structure (list markers, labels, headings, table rows).
3. SQL rate/ratio/share results carry their numerator and denominator; the rendering states
   "N of D (rate)".
4. Small-group lineage: grouped SQL results list the record ids of every returned group with at
   most ``sql_surface.group_lineage_max`` contributing records (ACL applied; read-only).
5. Exact-before-bounded precedence in packing and in the generator prompt.
6. The generator completeness rule names only records present in the evidence ("N shown of M").
7. The SQL writer filters a question word that is a stored value literally (prompt + value hint).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlglot")

from su_multi_arm.core.config import SqlSurfaceConfig  # noqa: E402
from su_multi_arm.core.types import ScopeResult  # noqa: E402
from su_multi_arm.decision.intent import detect_intents  # noqa: E402
from su_multi_arm.engine import Engine  # noqa: E402
from su_multi_arm.generation.contract import AggregateEvidence, AnswerContract, strip_sentences  # noqa: E402
from su_multi_arm.generation.rendering import render_sql_rows  # noqa: E402
from su_multi_arm.generation.statements import detect_statements  # noqa: E402
from su_multi_arm.models import Answer, Claim  # noqa: E402
from su_multi_arm.profiles.prompts import GENERATOR_PROMPT  # noqa: E402
from su_multi_arm.sql_surface.catalog import SqlCatalog, SqlColumn, SqlTable  # noqa: E402
from su_multi_arm.sql_surface.planner import open_session  # noqa: E402
from su_multi_arm.sql_surface.prompts import PROJECTION_RULES, USER_PROMPT, user_prompt, value_hints  # noqa: E402

import sql_surface_support as ss  # noqa: E402

ALPHA = ss.tenant("t_alpha")
TARGETS = ["record", "status", "product"]


@pytest.fixture(scope="module")
def world():
    return ss.world()


def execute(world, sql: str, acl=(), **limits):
    arm = ss.make_arm(world, **limits)
    return open_session(arm, ss.context(), list(acl)).execute(sql)


# --- 1. possessives -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "This is one ticket's resolution, not a general fix for every cause of this symptom.",
        "That is a record's owner, as shown in the history.",
        "This is one record’s timeline only.",
    ],
)
def test_a_singular_possessive_is_not_a_count_statement(sentence):
    assert detect_statements(sentence, units=["ticket", "tickets", "record", "records"]) == []


@pytest.mark.parametrize(
    ("sentence", "number"),
    [
        ("There are 12 tickets' owners listed.", "12 tickets"),
        ("One ticket is affected.", "One ticket"),
        ("We found 3 open tickets.", "3 open tickets"),
    ],
)
def test_counts_and_plural_possessives_are_still_count_statements(sentence, number):
    found = detect_statements(sentence, units=["ticket", "tickets"])
    assert [s.kind for s in found] == ["count"] and found[0].text.startswith(number)


# --- 2. Markdown-preserving strip ---------------------------------------------------------------


def _strip(text: str, needle: str) -> str:
    start = text.index(needle)
    return strip_sentences(text, [(start, start + len(needle))])


def test_stripping_a_bullets_first_sentence_keeps_the_marker_and_label():
    text = "Intro line.\n\n- **First check:** There are 14 open records about it. The setting was off [kb:a].\n- **Second check:** Sign in again [kb:b]."
    out = _strip(text, "14 open records")
    assert out == "Intro line.\n\n- **First check:** The setting was off [kb:a].\n- **Second check:** Sign in again [kb:b]."


def test_a_bullet_left_without_content_is_dropped_whole():
    text = "Intro line.\n\n- **Count:** There are 14 open records.\n- **Next:** Sign in again [kb:b].\n\nClosing line."
    out = _strip(text, "14 open records")
    assert out == "Intro line.\n\n- **Next:** Sign in again [kb:b].\n\nClosing line."
    assert "**Count:**" not in out


def test_a_label_that_carries_the_statement_drops_the_item():
    text = "- **14 open records:** see the list [kb:a].\n- Other item [kb:b]."
    assert _strip(text, "14 open records") == "- Other item [kb:b]."


def test_numbered_items_headings_and_quotes_keep_their_prefix():
    assert _strip("1. There are 3 records. Keep this [kb:a].\n2. Next [kb:b].", "3 records") == "1. Keep this [kb:a].\n2. Next [kb:b]."
    assert _strip("## Summary of 3 records\nBody [kb:a].", "3 records") == "Body [kb:a]."
    assert _strip("> There are 3 records. Quoted rest [kb:a].", "3 records") == "> Quoted rest [kb:a]."


def test_table_rows_keep_their_cells():
    text = "| Issue | Evidence |\n| --- | --- |\n| Login | There are 9 records. Token expired [kb:a]. |\n| Sync | Retry [kb:b]. |"
    out = _strip(text, "9 records")
    assert out == "| Issue | Evidence |\n| --- | --- |\n| Login | Token expired [kb:a]. |\n| Sync | Retry [kb:b]. |"
    lone = "| Issue | Evidence |\n| --- | --- |\n| There are 9 records. | |\n| Sync | Retry [kb:b]. |"
    assert _strip(lone, "9 records") == "| Issue | Evidence |\n| --- | --- |\n| Sync | Retry [kb:b]. |"


def test_plain_paragraphs_strip_as_before():
    text = "First sentence. There are 4 records here. Last sentence [kb:a]."
    assert _strip(text, "4 records") == "First sentence. Last sentence [kb:a]."
    assert strip_sentences("  untouched text  ", []) == "untouched text"


def test_the_contract_strip_keeps_the_list_structure():
    text = (
        "Check these leads.\n\n"
        "- **Check the sync update:** There are 14 open records about it. The sync was disabled [kb:a].\n"
        "- **Check the connection:** Re-authenticate the content source [kb:b]."
    )
    answer = Answer(answer=text, claims=[Claim(text="Narrative.", kind="narrative", evidence_ids=["kb:a", "kb:b"])], citations=["kb:a", "kb:b"])
    result = AnswerContract().enforce(answer, evidence_ids={"kb:a", "kb:b"}, aggregates=[], intents=detect_intents("What should I check?", [], TARGETS), question="What should I check?", targets=TARGETS)
    assert result.contract == "stripped"
    lines = [line for line in result.answer.answer.splitlines() if line.strip()]
    assert lines[0] == "Check these leads." and all(line.startswith("- **") for line in lines[1:]) and len(lines) == 3


# --- 3. ratio parts -----------------------------------------------------------------------------


def test_a_ratio_carries_its_numerator_and_denominator(world):
    evidence = execute(world, "SELECT CAST(COUNT(CASE WHEN status = 'open' THEN 1 END) AS REAL) / COUNT(*) AS open_share FROM ticket", ALPHA)
    assert evidence["columns"] == ["open_share", "open_share_numerator", "open_share_denominator"]
    assert evidence["rows"][0]["values"] == {"open_share": pytest.approx(2 / 3), "open_share_numerator": 2, "open_share_denominator": 3}
    assert evidence["sql_ratios"] == [{"ratio": "open_share", "numerator": "open_share_numerator", "denominator": "open_share_denominator", "parts_added": True}]
    assert evidence["value"] == pytest.approx(2 / 3)  # the scalar keeps its value
    assert evidence["rendering"] == "SQL result: open_share=2 of 3 (0.6667) (exact; 1 row)."
    assert evidence["exactness"] == "exact"


def test_projected_parts_are_reused_and_percentages_peeled(world):
    sql = "SELECT module, COUNT(CASE WHEN status = 'open' THEN 1 END) AS opened, COUNT(*) AS total, ROUND(100.0 * COUNT(CASE WHEN status = 'open' THEN 1 END) / COUNT(*), 1) AS pct FROM ticket GROUP BY module ORDER BY module"
    evidence = execute(world, sql, ALPHA, sql_surface={"group_lineage_max": 0})
    assert evidence["columns"] == ["module", "opened", "total", "pct"]
    assert evidence["sql_ratios"] == [{"ratio": "pct", "numerator": "opened", "denominator": "total", "parts_added": False}]
    assert evidence["rendering"] == "SQL result: module=Billing, pct=1 of 2 (50); module=Search, pct=1 of 1 (100) (exact; 2 rows)."


def test_a_ratio_of_constants_or_distinct_statements_is_left_alone(world):
    plain = execute(world, "SELECT AVG(hours) / 2 AS half FROM ticket", ALPHA)
    assert plain["columns"] == ["half"] and plain["sql_ratios"] == []
    distinct = execute(world, "SELECT DISTINCT module FROM ticket", ALPHA)
    assert distinct["sql_ratios"] == []


def test_render_sql_rows_states_n_of_d():
    text = render_sql_rows(["share", "num", "den"], [{"share": 0.25, "num": 3, "den": 12}], exactness="exact", ratios=[("share", "num", "den")])
    assert text == "SQL result: share=3 of 12 (0.25) (exact; 1 row)."


# --- 4. small-group lineage ---------------------------------------------------------------------


def test_small_groups_list_their_contributing_records(world):
    evidence = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module ORDER BY n DESC", ALPHA)
    rows = {r["values"]["module"]: r.get("contributor_ids") for r in evidence["rows"]}
    assert rows == {"Billing": ["tk-1", "tk-2"], "Search": ["tk-3"]}
    assert evidence["lineage"] == "groups" and evidence["group_lineage"]["status"] == "computed"
    assert evidence["document_ids"] == ["tk-1", "tk-2", "tk-3"]
    assert "(records: tk-1, tk-2)" in evidence["rendering"] and "all of its contributing records" in evidence["rendering"]


def test_group_lineage_respects_the_acl_filters_having_and_limit(world):
    beta = execute(world, "SELECT module, COUNT(*) AS n FROM ticket WHERE status = 'open' GROUP BY module ORDER BY n DESC LIMIT 1", ss.tenant("t_beta"))
    assert [(r["values"]["module"], r.get("contributor_ids")) for r in beta["rows"]] == [("Billing", ["tk-4"])]
    having = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module HAVING COUNT(*) > 1", ALPHA)
    assert [(r["values"]["module"], r.get("contributor_ids")) for r in having["rows"]] == [("Billing", ["tk-1", "tk-2"])]
    every = {i for r in execute(world, "SELECT status, COUNT(*) AS n FROM ticket GROUP BY status", ALPHA)["rows"] for i in r.get("contributor_ids", [])}
    assert every == {"tk-1", "tk-2", "tk-3"}  # never a record of another tenant


def test_a_null_group_key_gets_its_records_too(world):
    evidence = execute(world, "SELECT priority, COUNT(*) AS n FROM ticket GROUP BY priority ORDER BY priority", ALPHA)
    assert [(r["values"]["priority"], r.get("contributor_ids")) for r in evidence["rows"]] == [(None, ["tk-3"]), (1, ["tk-1"]), (2, ["tk-2"])]


def test_groups_over_the_threshold_or_unprojected_keys_get_no_ids(world):
    big = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module", ALPHA, sql_surface={"group_lineage_max": 1})
    assert {r["values"]["module"]: r.get("contributor_ids") for r in big["rows"]} == {"Billing": None, "Search": ["tk-3"]}
    hidden = execute(world, "SELECT COUNT(*) AS n FROM ticket GROUP BY module", ALPHA)
    assert hidden["group_lineage"] is None and all("contributor_ids" not in r for r in hidden["rows"])
    off = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module", ALPHA, sql_surface={"group_lineage_max": 0})
    assert off["group_lineage"] is None and off["lineage"] == "not_derivable" and off["document_ids"] == []


def test_a_filtered_scalar_keeps_up_to_the_threshold_of_ids(world):
    evidence = execute(world, "SELECT COUNT(*) AS n FROM ticket WHERE module = 'Billing'")
    assert evidence["contributors"] == 4 and evidence["document_ids"] == ["tk-1", "tk-2", "tk-4", "tk-6"] and not evidence["document_ids_truncated"]


def test_group_lineage_config_default_and_bounds():
    assert SqlSurfaceConfig().group_lineage_max == 10
    with pytest.raises(ValueError):
        SqlSurfaceConfig(group_lineage_max=51)


# --- 5. exact before bounded --------------------------------------------------------------------


def test_exact_results_are_packed_first_with_a_precedence_note():
    bounded = {"id": "aggregate:1", "exactness": "bounded", "value": 5, "total": 5, "denominator": 5}
    exact = {"id": "aggregate:2", "exactness": "exact", "value": 1, "total": 1, "denominator": 1}
    ordered, notes = Engine._primary_exact([bounded, exact])
    assert [a["id"] for a in ordered] == ["aggregate:2", "aggregate:1"]
    assert notes["aggregate:2"]["rank"] == "primary" and notes["aggregate:1"] == {"rank": "secondary", "exact_ids": ["aggregate:2"], "note": notes["aggregate:1"]["note"]}
    refused = {"id": "aggregate:3", "exactness": "exact", "refused": True}
    assert Engine._primary_exact([bounded, refused])[1] == {}  # nothing exact: no precedence claimed
    assert Engine._primary_exact([exact])[1] == {}


def test_packed_extras_put_the_exact_sql_result_first(world):
    bounded = {"id": "aggregate:1", "exactness": "bounded", "value": 5, "total": 5, "denominator": 5, "document_ids": ["r-1"]}
    sql = execute(world, "SELECT module, COUNT(*) AS n FROM ticket WHERE status = 'closed' GROUP BY module", ALPHA)
    sql["id"] = "aggregate:2"
    extras = Engine._pack_extras(Engine, ss.context(), [bounded, sql])  # the helpers are static
    assert [e.id for e in extras] == ["aggregate:2", "aggregate:1"]
    first, second = (json.loads(e.passage) for e in extras)
    assert first["precedence"]["rank"] == "primary" and first["exactness"] == "exact"
    assert second["precedence"]["rank"] == "secondary" and second["precedence"]["exact_ids"] == ["aggregate:2"]


def test_the_generator_prompt_prefers_exact_and_names_only_evidence_records():
    text = " ".join(GENERATOR_PROMPT.split())
    assert "answer from the exact one" in text and "never instead of the exact one" in text
    assert "Never name a record the evidence does not contain" in text and '("2 shown of 3")' in text
    assert "when a number is part of a whole, state both" in text  # the rest of the 1348a6d rule stays
    assert "state each ranked item with its value" in text


def test_a_shown_of_total_answer_passes_the_contract():
    result = ScopeResult(exactness="exact", denominator=3, value=3, total=3, matched_ids=["r-1", "r-2", "r-3"])
    aggregate = AggregateEvidence(id="aggregate:1", result=result, rendering="records: 3 (exact; 3 records matched).")
    answer = Answer(
        answer="There are 3 records [aggregate:1]. Examples (2 shown of 3): r-1 [r-1], r-2 [r-2].",
        claims=[Claim(text="3 records", kind="count", evidence_ids=["aggregate:1"]), Claim(text="Examples r-1, r-2", kind="examples", evidence_ids=["r-1", "r-2"])],
        citations=["aggregate:1", "r-1", "r-2"],
    )
    question = "How many records are there?"
    checked = AnswerContract().enforce(answer, evidence_ids={"aggregate:1", "r-1", "r-2"}, aggregates=[aggregate], intents=detect_intents(question, [], TARGETS), question=question, targets=TARGETS)
    assert checked.contract == "passed", checked.violations


# --- 6/7. SQL writer prompt rules ---------------------------------------------------------------


def _status_catalog() -> SqlCatalog:
    status = SqlColumn(source_column="status", name="status", display_name="Status", datatype="TEXT", field_type="enum", source_field="status", sample_values=["new", "open", "pending", "on hold", "closed"], samples="observed", values_complete=True)
    table = SqlTable(name="ticket", kind="ticket", display_name="Ticket", columns=[status])
    return SqlCatalog(generation="g", projection_fingerprint="p", acl_key="k", tables={"ticket": table})


def test_a_question_word_that_is_a_stored_value_is_a_literal_hint():
    catalog = _status_catalog()
    assert value_hints("How many open records are there?", catalog) == ['- "open" is the stored value ticket.status = "open" (literal match)']
    assert value_hints("Which records are on hold?", catalog) == ['- "on hold" is the stored value ticket.status = "on hold" (literal match)']
    prompt = user_prompt("How many open records are there?", catalog, row_limit=10)
    assert "filter on exactly that value" in prompt and "(literal match)" in prompt


def test_prompt_rules_ask_for_ratio_parts():
    assert "also SELECTs its numerator and its denominator" in USER_PROMPT
    assert "a rate/ratio/share also selects its numerator and denominator" in PROJECTION_RULES


def test_packed_sql_rows_carry_contributor_ids(world):
    evidence = execute(world, "SELECT module, COUNT(*) AS n FROM ticket GROUP BY module", ALPHA)
    evidence["id"] = "aggregate:1"
    payload = Engine._sql_extra(ss.context(), evidence)
    assert payload["rows"][0]["contributor_ids"] == ["tk-1", "tk-2"]
    assert payload["contributor_lineage"]["scope"] == "small_group_contributors"
    assert payload["contributor_lineage"]["document_ids"] == ["tk-1", "tk-2", "tk-3"]
    assert json.dumps(payload, default=str)


def test_citing_a_contributor_record_of_a_supplied_sql_aggregate_is_not_unknown_evidence():
    """Cutover finding: the SQL rendering lists a small group's records ("records: t:1, t:2"); a
    generator that cites one of them inline is backed by the aggregate, not an unknown citation."""
    result = ScopeResult(exactness="exact", denominator=2, value=2, total=2)
    aggregate = AggregateEvidence(
        id="aggregate:1", result=result, rendering="SQL result: org=Acme, n=2 (records: t:1, t:2) (exact; 1 row).",
        columns=["org", "n"], rows=[{"values": {"org": "Acme", "n": 2}, "contributor_ids": ["t:1", "t:2"]}],
    )
    answer = Answer(answer="Acme raised the most, 2 [aggregate:1]: [t:1] and [t:2].", claims=[], citations=["aggregate:1", "t:1"])
    question = "Which org raised the most records?"
    checked = AnswerContract().enforce(answer, evidence_ids={"aggregate:1"}, aggregates=[aggregate], intents=detect_intents(question, [], TARGETS), question=question, targets=TARGETS)
    assert "unknown_evidence" not in {v.rule for v in checked.violations}, checked.violations
    assert checked.contract != "abstained"
