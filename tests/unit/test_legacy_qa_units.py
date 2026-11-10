"""Pure helpers of the legacy-QA compatibility layer: citation renumbering, link policy, streamed
answer decoding, credential redaction, visibility rule and error-status mapping (no engine)."""

from __future__ import annotations

import json

import pytest

from su_multi_arm.compat.legacy_qa.envelope import citation_url, legacy_modes, renumber, sql_block
from su_multi_arm.compat.legacy_qa.errors import stream_status
from su_multi_arm.compat.legacy_qa.profile import DatasetProfile
from su_multi_arm.compat.legacy_qa.source import hidden, redact, source_record
from su_multi_arm.compat.legacy_qa.stream import AnswerTextExtractor, ProvisionalText
from su_multi_arm.core.types import Document

NUMBERS = {"doc:a/b#c0": 1, "ticket:42": 2, "aggregate:1": 3}


def test_renumber_known_ids_only():
    text = "See [doc:a/b#c0] and [ticket:42, aggregate:1]; also [ticket:42;doc:a/b#c0]. Keep [note] and [link](https://x.org) and [ticket:42, unknown]."
    assert renumber(text, NUMBERS) == "See [1] and [2][3]; also [2][1]. Keep [note] and [link](https://x.org) and [ticket:42, unknown]."
    assert renumber("dup [ticket:42, ticket:42]", NUMBERS) == "dup [2]"
    assert renumber("no markers", NUMBERS) == "no markers"


@pytest.mark.parametrize(
    ("policy", "external", "expected"),
    [
        ("external_first", "https://example.org/x", "https://example.org/x"),
        ("external_first", None, "/source?dataset=kb&id=rec%3A1"),
        ("source_first", "https://example.org/x", "/source?dataset=kb&id=rec%3A1"),
        ("external_only", None, ""),
    ],
)
def test_citation_url_policy(policy, external, expected):
    profile = DatasetProfile(id="kb", citation_url=policy, source={"enabled": True})
    assert citation_url(profile, "rec:1", external) == expected


def test_source_links_need_an_enabled_view_and_a_viewable_id():
    assert citation_url(DatasetProfile(id="kb"), "rec:1", None) == ""  # view disabled
    profile = DatasetProfile(id="kb", source={"enabled": True, "id_pattern": r"^rec:[0-9]+$"})
    assert citation_url(profile, "other:1", None) == "" and citation_url(profile, "rec:7", None).endswith("id=rec%3A7")


def test_extractor_decodes_the_answer_string_across_chunks():
    reply = json.dumps({"answer": 'Line "one"\nLine two é [doc:a/b#c0] \\ end', "claims": [], "citations": ["doc:a/b#c0"]})
    extractor = AnswerTextExtractor()
    for i in range(0, len(reply), 3):
        text = extractor.feed(reply[i : i + 3])
    assert text == json.loads(reply)["answer"] and extractor.done
    plain = AnswerTextExtractor()
    assert plain.feed("Plain ") == "Plain " and plain.feed("text [1]") == "Plain text [1]"
    late = AnswerTextExtractor()
    assert late.feed('{"claims": [], "answer"') == "" and late.feed(': "ok"}') == "ok"
    non_string = AnswerTextExtractor()
    assert non_string.feed('{"answer": 7}') == "" and non_string.done


def test_provisional_text_holds_back_unfinished_markers_and_renumbers():
    reply = json.dumps({"answer": "Invoices [doc:a/b#c0] and totals [ticket:42, aggregate:1].", "citations": []})
    stream = ProvisionalText(NUMBERS)
    deltas = [stream.feed(reply[i : i + 4]) for i in range(0, len(reply), 4)]
    joined = "".join(deltas)
    assert joined == "Invoices [1] and totals [2][3]."
    assert all("doc:" not in d and "ticket" not in d for d in deltas)
    assert stream.tokens == sum(1 for d in deltas if d)


def test_redaction_masks_credential_shapes():
    text = "password=hunter2 api_key: 'abc' Authorization: Bearer abcdefghijklmnop https://user:pw@host.example/x sk-ABCDEFGHIJKLMNOPQRSTU token=t0k"
    out = redact(text)
    for secret in ("hunter2", "'abc'", "abcdefghijklmnop", "user:pw", "ABCDEFGHIJKLMNOPQRSTU", "t0k"):
        assert secret not in out
    assert "https://[redacted]@host.example/x" in out and redact("plain words") == "plain words"


def test_visibility_rule_and_record_view():
    profile = DatasetProfile(
        id="kb",
        source={
            "enabled": True,
            "hide": [{"field": "visibility", "values": ["Internal"]}],
            "title_fallback_field": "issue",
            "sections": [{"field": "issue", "heading": "Issue"}, {"field": "resolution", "heading": "Resolution"}],
            "repeated_field_prefix": "comment_",
            "section_limit": 3,
            "kind_labels": {"kb": "Knowledge article"},
        },
        content_source={"kb": "Knowledge"},
    )
    doc = Document(id="kb:1", source="kb", title="", fields={"issue": "Sync stops\nmore", "resolution": "Restart", "comment_2": "second", "comment_1": "first"}, metadata={"visibility": "public"}, url="https://example.org/kb/1")
    assert not hidden(profile, doc) and hidden(profile, doc.model_copy(update={"metadata": {"visibility": "INTERNAL"}}))
    view = source_record(profile, doc)
    assert view["title"] == "Sync stops" and view["title_derived"] is True and view["kind"] == "Knowledge article"
    assert [s["heading"] for s in view["sections"]] == ["Issue", "Resolution", "Comment 1"]  # capped at section_limit
    assert view["source_label"] == "Knowledge" and view["external_url"] == "https://example.org/kb/1"


def test_stream_status_and_mode_names():
    assert stream_status("provider_connection") == 502 and stream_status("rejected") == 422
    assert stream_status("unavailable") == 503 and stream_status("internal") == 500 and stream_status("weird") == 500
    assert legacy_modes(["dense", "structured", "typed_query", "lexical", "dense"]) == ["dense", "sql", "lexical"]


def test_sql_block_labels_typed_aggregates():
    sql, note = sql_block([{"operation": "group_count", "groups": [{"key": {"project": "A"}, "count": 2}], "rendering": "A: 2", "exactness": "exact"}])
    assert sql["columns"] == ["project", "count"] and sql["rows"] == [["A", 2]] and sql["generated"].startswith("-- typed group_count")
    assert note
    real, none = sql_block([{"sql": "SELECT 1", "columns": ["x"], "rows": [[1]]}])
    assert real["generated"] == "SELECT 1" and none is None
    assert sql_block([]) == (None, None)


# --- parity slice: UI link aliases, session follow-up gate, anchor store ---------------------------


def test_ui_link_alias_round_trip_and_bounds():
    from su_multi_arm.compat.legacy_qa.profile import UiLink

    link = UiLink(dataset="ui_kb", id_pattern=r"^case:[A-Za-z0-9_-]+$", rules=[{"package_prefix": "ticket:", "alias_prefix": "case:t-"}, {"package_prefix": "kb:", "alias_prefix": "case:k-"}])
    assert link.alias("ticket:42") == "case:t-42" and link.resolve("case:t-42") == "ticket:42"
    assert link.alias("kb:a_b") == "case:k-a_b" and link.resolve("case:k-a_b") == "kb:a_b"
    assert link.alias("doc:1") is None and link.alias("ticket:4/2") is None  # no rule / outside the charset
    assert link.resolve("case:x-1") is None and link.resolve("case:t-") is None and link.resolve("case:t-4 2") is None
    profile = DatasetProfile(id="kb", source={"enabled": True, "ui_link": link.model_dump()})
    assert citation_url(profile, "ticket:42", None, "ui") == "/source?dataset=ui_kb&id=case%3At-42"
    assert citation_url(profile, "ticket:42", None, "mcp") == "/source?dataset=kb&id=ticket%3A42"
    assert citation_url(profile, "doc:1", None, "ui") == "/source?dataset=kb&id=doc%3A1"  # no alias: canonical link
    assert citation_url(profile, "ticket:42", "https://example.org/t/42", "ui") == "https://example.org/t/42"


def test_session_gate_preconditions_in_order():
    from su_multi_arm.compat.legacy_qa.profile import SessionCache
    from su_multi_arm.compat.legacy_qa.session_cache import Anchor, gate

    cfg = SessionCache(enabled=True, record_id_patterns=[r"\bT-\d+\b"])
    anchor = Anchor(question="why does T-1 fail", evidence_ids=["T-2", "doc:a"], generation="g1", context_hash=None, turn=1)
    assert gate(cfg, "summarize that", None, "g1", None) == (False, "deep_no_session_evidence")
    assert gate(cfg, "summarize that", Anchor(question="q", evidence_ids=[], generation="g1", context_hash=None, turn=1), "g1", None)[1] == "deep_no_session_evidence"
    assert gate(cfg, "summarize that", anchor, "g2", None) == (False, "deep_generation_changed")
    assert gate(cfg, "summarize that", anchor, "g1", "h") == (False, "deep_record_context_changed")
    assert gate(cfg, "summarize that for T-9", anchor, "g1", None) == (False, "deep_new_record_id")
    assert gate(cfg, "summarize that for T-1 and T-2", anchor, "g1", None) == (True, "followup")  # ids already in the anchor
    assert gate(cfg, "how do I reset a password", anchor, "g1", None) == (False, "new_search")
    for question in ("Can you shorten it?", "tell me more", "what does source 3 say", "explain the second step", "translate that to French"):
        assert gate(cfg, question, anchor, "g1", None) == (True, "followup"), question


def test_anchor_store_ttl_and_eviction():
    from su_multi_arm.compat.legacy_qa.profile import SessionCache
    from su_multi_arm.compat.legacy_qa.session_cache import Anchor, AnchorStore

    now = [1000.0]
    store = AnchorStore(SessionCache(enabled=True, ttl_seconds=60, max_sessions=2), clock=lambda: now[0])
    for sid in ("a", "b", "c"):
        store.put(sid, Anchor(question=sid, evidence_ids=["x"], generation="g", context_hash=None, turn=1))
    assert store.get("a") is None and store.get("b") is not None and len(store) == 2  # oldest evicted
    now[0] += 61
    assert store.get("c") is None  # expired


# --- M2b-2: the unified-view switch, coverage and labelled links (additive keys only) ---------------


def test_the_unified_switch_is_off_by_default_and_needs_a_config(tmp_path):
    from su_multi_arm.compat.legacy_qa.profile import load_profile

    off = DatasetProfile(id="support-qa", config=tmp_path / "support.yaml")
    assert off.unified.enabled is False and off.served_config() == tmp_path / "support.yaml"
    on = DatasetProfile(id="support-qa", config=tmp_path / "support.yaml", unified={"enabled": True, "config": tmp_path / "unified.yaml"})
    assert on.served_config() == tmp_path / "unified.yaml"
    with pytest.raises(ValueError, match="unified.config"):
        DatasetProfile(id="support-qa", unified={"enabled": True})
    (tmp_path / "profile.yaml").write_text("datasets:\n  - id: support-qa\n    config: support.yaml\n    unified: {enabled: true, config: unified.yaml}\n")
    loaded = load_profile(tmp_path / "profile.yaml")
    assert loaded.datasets[0].served_config() == (tmp_path / "unified.yaml").resolve()


def test_a_switched_dataset_refuses_an_engine_without_the_planner():
    pytest.importorskip("fastapi")
    from types import SimpleNamespace

    from su_multi_arm.compat.legacy_qa.app import create_legacy_app
    from su_multi_arm.compat.legacy_qa.profile import LegacyProfile

    profile = LegacyProfile.model_validate({"warmup": False, "datasets": [{"id": "support-qa", "unified": {"enabled": True, "config": "unified.yaml"}}]})
    with pytest.raises(ValueError, match="crosssource.planner.enabled"):
        create_legacy_app(profile, engines={"support-qa": SimpleNamespace(two_halves=None)})


def test_links_are_attached_labelled_and_hidden_records_dropped():
    from su_multi_arm.compat.legacy_qa.envelope import attach_links, link_status

    citations = [{"id": 1, "record_id": "ticket:1", "record_type": "ticket"}, {"id": 2, "record_id": "aggregate:1", "record_type": "aggregate"}, {"id": 3, "record_id": "ticket:2", "record_type": "ticket"}]
    links = {
        "ticket:1": [{"link_id": "a", "other_id": "doc:1", "pair": "ticket_doc", "tier": "A", "status": "accepted", "label": None, "labelled": False, "evidence": {"x": 1}},
                     {"link_id": "b", "other_id": "doc:hidden", "pair": "ticket_doc", "tier": "B", "status": "candidate", "label": "candidate", "labelled": True}],
        "ticket:2": [{"link_id": "c", "other_id": "doc:2", "pair": "ticket_doc", "tier": "B", "status": "candidate", "label": "candidate", "labelled": True}],
    }
    shown = attach_links(citations, links, hidden={"doc:hidden"})
    assert shown == 2 and "links" not in citations[1]
    assert citations[0]["link_status"] == "reviewed" and [e["link_id"] for e in citations[0]["links"]] == ["a"]
    assert "evidence" not in citations[0]["links"][0]  # display fields only
    assert citations[2]["link_status"] == "candidate" and citations[2]["links"][0]["labelled"] is True
    assert link_status([]) is None
