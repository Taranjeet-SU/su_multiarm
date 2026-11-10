"""Synthetic passage-selection regressions; exact source slices, no model or live data."""

import json

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.config_selection import PackingConfig
from su_multi_arm.core.types import Document, ScopeSpec
from su_multi_arm.arms.structured import aggregate_record
from su_multi_arm.engine import Engine
from su_multi_arm.models import SearchRequest
from su_multi_arm.selection.packing import PackExtra, PackRecord, pack, render


def check_spans(record, evidence):
    for span in evidence.spans:
        assert record.fields[span.field][span.start:span.end] == span.text
        assert evidence.passage[span.passage_start:span.passage_end] == span.text


def test_no_query_keeps_prefix_field_priority_and_full_fit_rendering():
    record = PackRecord(id="ticket:1", fields={"body": "body " * 600, "resolution": "Reset the synthetic queue."})
    cfg = PackingConfig(budget_chars=500, field_priority=["body"])
    prefix = pack([record], cfg)
    assert prefix.evidence[0].passage == render(record, ["body"])[:500]
    assert prefix.passage_recipe == "prefix_v1"
    check_spans(record, prefix.evidence[0])
    whole = pack([record], PackingConfig(passage_selection="question_windows", budget_chars=4000), query="How was this resolved?")
    assert whole.evidence[0].passage == render(record, [])
    assert not whole.evidence[0].truncated
    check_spans(record, whole.evidence[0])


def test_resolution_at_tail_reaches_context_without_inventing_recovery():
    text = "Initial symptom: retries failed.\n" + "Diagnostic detail without an outcome.\n" * 150
    text += "Resolution: the retry key was corrected. Recovery time was not recorded."
    record = PackRecord(id="ticket:2", fields={"body": text})
    packed = pack([record], PackingConfig(passage_selection="question_windows", budget_chars=500), query="How was the retry issue fixed?")
    evidence = packed.evidence[0]
    assert "retry key was corrected" in evidence.passage
    assert "Recovery time was not recorded" in evidence.passage
    assert evidence.spans[0].start > 0 and evidence.truncated
    assert packed.passage_characters == len(evidence.passage) <= 500
    check_spans(record, evidence)


def test_question_can_select_explicit_resolution_field_after_long_details():
    record = PackRecord(id="ticket:3", fields={"details": "Unrelated diagnostic history. " * 300, "resolution": "Resolved by replacing the expired test token."})
    result = pack([record], PackingConfig(passage_selection="question_windows", budget_chars=500, field_priority=["details"]), query="How was authentication resolved?")
    assert "replacing the expired test token" in result.evidence[0].passage
    assert any(span.field == "resolution" for span in result.evidence[0].spans)
    check_spans(record, result.evidence[0])


def test_time_question_selects_literal_tail_date_without_inferred_freshness():
    record = PackRecord(id="kb:1", fields={"body": "General setup procedure. " * 200 + "\nVersion 2 was updated on 2026-10-02. Applicability to version 3 is unknown."})
    result = pack([record], PackingConfig(passage_selection="question_windows", budget_chars=500), query="When was the version updated?")
    assert "2026-10-02" in result.evidence[0].passage
    assert "version 3 is unknown" in result.evidence[0].passage
    check_spans(record, result.evidence[0])


def test_multiple_windows_keep_source_order_unicode_offsets_and_budget():
    text = "αβγ setup failure.\n" + "Routine trace detail. " * 120 + "\nResolution: café worker fixed the setup failure."
    record = PackRecord(id="ticket:4", fields={"body": text})
    result = pack([record], PackingConfig(passage_selection="question_windows", budget_chars=900), query="What was the setup failure and resolution?")
    evidence = result.evidence[0]
    assert "café worker fixed" in evidence.passage
    assert 1 <= len(evidence.spans) <= 2
    assert [s.start for s in evidence.spans] == sorted(s.start for s in evidence.spans)
    assert result.passage_characters == len(evidence.passage) <= 900
    check_spans(record, evidence)


def test_question_selection_keeps_record_fairness_and_indivisible_aggregate():
    records = [PackRecord(id=f"r{i}", fields={"body": "Routine text. " * 100 + f" Resolution: synthetic action {i} fixed it."}) for i in range(4)]
    extra = PackExtra(id="exact-count", passage='{"count":4}')
    result = pack(records, PackingConfig(passage_selection="question_windows", budget_chars=1000, min_per_record=200), [extra], query="How was it fixed?")
    assert [e.id for e in result.evidence] == ["r0", "r1", "r2", "r3", "exact-count"]
    assert result.evidence[-1].passage == extra.passage and not result.evidence[-1].spans
    assert result.passage_characters == sum(len(e.passage) for e in result.evidence) <= 1000
    for record, evidence in zip(records, result.evidence[:4], strict=True):
        check_spans(record, evidence)


def test_unmatched_query_and_unanchored_fallback_keep_existing_prefix():
    record = PackRecord(id="kb:2", fields={"body": "synthetic material " * 200})
    cfg = PackingConfig(budget_chars=500, passage_selection="question_windows")
    assert pack([record], cfg, query="unseenword").evidence[0].passage == render(record, [])[:500]
    fallback = pack([PackRecord(id="fallback", fallback_text="passage " * 200)], cfg, query="passage")
    assert fallback.evidence[0].passage == ("passage " * 200)[:500]
    assert fallback.evidence[0].spans == []


def test_engine_passes_question_to_native_packer():
    cfg = validate_config({
        "schema_version": 2, "dataset": "synthetic",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}, "selection": {"packing": {"budget_chars": 500, "passage_selection": "question_windows"}}},
    })
    engine = Engine(cfg)
    body = "Retry failure. " + "Unrelated diagnostics. " * 200 + " Resolution: retry configuration was fixed."
    engine.ingest([Document(id="case:1", dataset="synthetic", fields={"body": body})])
    search = engine.search(SearchRequest(query="How was retry fixed?"))
    evidence, trace = engine._evidence(engine._context_for(None), search)
    assert trace["passage_recipe"] == "question_windows_v1" and trace["windowed_ids"] == ["case:1"]
    assert "retry configuration was fixed" in evidence[0]["passage"]
    span = evidence[0]["spans"][0]
    assert "text" not in span  # avoid repeating the same quote in the token-bounded context
    assert body[span["start"]:span["end"]] == evidence[0]["passage"][span["passage_start"]:span["passage_end"]]


def test_metadata_is_explicit_typed_and_budgeted_without_dumping_other_keys():
    record = PackRecord(id="case:meta", fields={"body": "source text " * 100}, metadata={"event_time": "2026-10-02T09:00:00Z", "version": 2, "confirmed": False, "private_value": "excluded"})
    result = pack([record], PackingConfig(budget_chars=500, metadata_fields=["event_time", "version", "confirmed"]))
    assert result.evidence[0].metadata == {"event_time": "2026-10-02T09:00:00Z", "version": 2, "confirmed": False}
    assert type(result.evidence[0].metadata["confirmed"]) is bool
    expected = len(json.dumps({"metadata": result.evidence[0].metadata}, ensure_ascii=False, separators=(",", ":")))
    assert result.metadata_characters == expected
    assert result.evidence_characters == result.passage_characters + expected <= 500
    assert "private_value" not in result.evidence[0].context_dict()["metadata"]
    default = pack([record], PackingConfig(budget_chars=500))
    assert "metadata" not in default.evidence[0].context_dict() and default.metadata_characters == 0
    with pytest.raises(ValueError):
        PackingConfig(metadata_fields=["secret", "secret"])


def test_oversized_allowlisted_metadata_is_dropped_by_key_and_the_record_is_kept():
    # V3.2 (replaces the pre-V3.2 "metadata omits the record" check): the record keeps its passage
    record = PackRecord(id="too-large", fields={"body": "source " * 200}, metadata={"version": "x" * 600})
    result = pack([record], PackingConfig(budget_chars=500, metadata_fields=["version"]))
    assert [e.id for e in result.evidence] == ["too-large"] and result.omitted_ids == []
    assert result.evidence[0].metadata == {} and result.evidence[0].passage.startswith("body: source")
    assert result.metadata_omitted_ids == ["too-large"] and result.metadata_dropped_keys == {"too-large": ["version"]}
    assert result.metadata_characters == 0 and result.evidence_characters == result.passage_characters <= 500


def test_metadata_share_drops_keys_from_the_end_of_the_allowlist():
    metadata = {"event_time": "2026-10-02T09:00:00Z", "version": 2, "owner_note": "n" * 120}
    record = PackRecord(id="case:share", fields={"body": "source text " * 100}, metadata=metadata)
    assert PackingConfig().metadata_max_share == 0.25  # V3.2 default restored after M7 (1.0 cut the passage share)
    config = PackingConfig(budget_chars=500, metadata_fields=["event_time", "version", "owner_note"], metadata_max_share=0.25)
    result = pack([record], config)
    assert result.evidence[0].metadata == {"event_time": "2026-10-02T09:00:00Z", "version": 2}
    assert result.metadata_dropped_keys == {"case:share": ["owner_note"]}
    assert result.metadata_characters <= 0.25 * 500
    assert len(result.evidence[0].passage) == 500 - result.metadata_characters  # text keeps the rest
    tight = pack([record], config.model_copy(update={"metadata_max_share": 0.05}))
    assert tight.evidence[0].metadata == {} and tight.metadata_dropped_keys == {"case:share": ["owner_note", "version", "event_time"]}
    loose = pack([record], config.model_copy(update={"metadata_max_share": 1.0}))
    assert loose.evidence[0].metadata == metadata and loose.metadata_dropped_keys == {}
    # a record whose passage and metadata both fit keeps every key whatever the share
    short = PackRecord(id="case:short", fields={"body": "short"}, metadata=metadata)
    assert pack([short], config).evidence[0].metadata == metadata
    for bad in (0, 1.5):
        with pytest.raises(ValueError):
            PackingConfig(metadata_max_share=bad)


def test_native_engine_metadata_and_bounded_aggregate_level_lineage():
    cfg = validate_config({
        "schema_version": 2, "dataset": "synthetic",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "metadata_schema": {"fields": [{"name": "status", "type": "string", "filterable": True}]},
        "pipeline": {"arms": {"lexical": {"backend": "local"}, "structured": {"backend": "local"}}, "selection": {"packing": {"metadata_fields": ["event_time"]}}},
    })
    engine = Engine(cfg)
    engine.ingest([Document(id=f"case:{i}", dataset="synthetic", fields={"body": "Synthetic record."}, metadata={"status": "open", "event_time": "2026-10-02", "excluded": "private"}) for i in range(3)])
    ctx = engine._context_for(None)
    search = engine.search("Synthetic record")
    evidence, _ = engine._evidence(ctx, search)
    assert evidence[0]["metadata"] == {"event_time": "2026-10-02"}
    spec = ScopeSpec(predicates=[{"field": "status", "op": "eq", "value": "open"}], measure={"kind": "count"})
    count = engine.aggregate_in(ctx, spec, {})
    native = {"id": "aggregate:native", **aggregate_record(spec, count)}
    actual = json.loads(engine._pack_extras(ctx, [native])[0].passage)
    assert actual["value"] == actual["contributor_lineage"]["total_matched_records"] == 3
    assert set(actual["contributor_lineage"]["document_ids"]) == {"case:0", "case:1", "case:2"}
    assert actual["contributor_lineage"]["generation"]["generation"] == ctx.generation.generation
    assert not actual["contributor_lineage"]["truncated"]
    aggregate = {"id": "aggregate:capped", "total": 37, "value": 100, "document_ids": [f"case:{i}" for i in range(10)], "groups": [{"value": 100}]}
    capped = json.loads(engine._pack_extras(ctx, [aggregate])[0].passage)
    lineage = capped["contributor_lineage"]
    assert lineage["document_ids"] == aggregate["document_ids"][:8]
    assert lineage["retained_count"] == 8 and lineage["total_matched_records"] == 37
    assert lineage["scope"] == "aggregate_level_only" and lineage["local_cap_truncated"] and lineage["truncated"]
    assert "groups" not in capped
    aggregate.update(document_ids=[], document_ids_truncated=True)
    upstream = json.loads(engine._pack_extras(ctx, [aggregate])[0].passage)["contributor_lineage"]
    assert upstream["upstream_truncated"] and upstream["truncated"] and upstream["retained_count"] == 0


def test_question_windows_are_opt_in_and_default_keeps_prefix_with_a_question():
    text = "Initial symptom: retries failed.\n" + "Diagnostic detail without an outcome.\n" * 150
    text += "Resolution: the retry key was corrected."
    record = PackRecord(id="ticket:opt", fields={"body": text})
    default = pack([record], PackingConfig(budget_chars=500), query="How was the retry issue fixed?")
    assert PackingConfig().passage_selection == "prefix"
    assert default.passage_recipe == "prefix_v1" and default.windowed_ids == []
    assert default.evidence[0].passage == render(record, [])[:500]
    check_spans(record, default.evidence[0])
    opted = pack([record], PackingConfig(budget_chars=500, passage_selection="question_windows"), query="How was the retry issue fixed?")
    assert opted.passage_recipe == "question_windows_v1" and opted.windowed_ids == ["ticket:opt"]
    assert "retry key was corrected" in opted.evidence[0].passage


def test_engine_default_packing_reports_prefix_recipe():
    cfg = validate_config({
        "schema_version": 2, "dataset": "synthetic",
        "backends": {"local": {"kind": "memory"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}, "selection": {"packing": {"budget_chars": 500}}},
    })
    engine = Engine(cfg)
    body = "Retry failure. " + "Unrelated diagnostics. " * 200 + " Resolution: retry configuration was fixed."
    engine.ingest([Document(id="case:1", dataset="synthetic", fields={"body": body})])
    search = engine.search(SearchRequest(query="How was retry fixed?"))
    evidence, trace = engine._evidence(engine._context_for(None), search)
    assert trace["passage_recipe"] == "prefix_v1" and trace["windowed_ids"] == []
    assert evidence[0]["passage"].startswith("body: Retry failure.")
