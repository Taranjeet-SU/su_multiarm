"""Cutover smoke findings (2026-11-09): SQL-surface row envelopes in the legacy SQL panel, and
credential masking that must not eat ordinary prose or Markdown."""

from su_multi_arm.compat.legacy_qa.envelope import sql_block
from su_multi_arm.compat.legacy_qa.source import REDACTED, redact


def test_sql_surface_row_envelopes_become_value_rows():
    agg = {
        "sql": "SELECT org, n FROM t",
        "columns": ["org", "n"],
        "rows": [
            {"values": {"org": "Acme", "n": 3}, "contributor_ids": ["r:1", "r:2", "r:3"]},
            {"values": {"org": "Beta", "n": 1}},
        ],
    }
    block, _ = sql_block([agg])
    assert block["columns"] == ["org", "n", "contributor_ids"]
    assert block["rows"][0] == ["Acme", 3, "r:1, r:2, r:3"]
    assert block["rows"][1][:2] == ["Beta", 1]
    assert "values" not in block["columns"]


def test_prose_and_markdown_after_token_label_are_not_masked():
    for text in ("**To create an API token:**\n\n1. Log in", "Enter the token: copy it from the admin page", "password: see below"):
        assert REDACTED not in redact(text), text


def test_secret_shaped_values_are_still_masked():
    for text in ("api_key=ab12cd34ef", 'password: "hunter2x"', "token: AbCdEfGhIjKlMnOpQrSt", "Authorization: Bearer abcdefgh12345678"):
        assert REDACTED in redact(text), text


def test_anaphoric_followups_match_the_default_cues():
    import re

    from su_multi_arm.compat.legacy_qa.profile import SessionCache

    cues = SessionCache().followup_cues
    for q in ("Show me the ticket numbers for those.", "List their owners", "What is the status of those?"):
        assert any(re.search(c, q) for c in cues), q
    for q in ("How do I configure a crawler for Confluence?", "Which organization raised the most tickets?"):
        assert not any(re.search(c, q) for c in cues), q
