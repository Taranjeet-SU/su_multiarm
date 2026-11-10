"""Protected metadata keys under the share cap, the restored 0.25 default, leading extras, and the
projection relation dump round trip. Synthetic records only.

M7 (real data, aggregates): with ``metadata_max_share`` 1.0 allowlisted metadata took most of the
evidence budget (passage share fell from about two thirds to under a quarter), while the earlier
0.25 cap dropped time/environment/identity keys a question needed. ``metadata_protected`` keeps
those keys whatever the cap; every other key is still capped.
"""

from __future__ import annotations

import json

import pytest

from su_multi_arm.core.config_selection import PackingConfig
from su_multi_arm.selection.packing import PackExtra, PackRecord, pack

FIELDS = ["captured_at", "environment", "account", "owner_note", "free_tags"]
PROTECTED = ["captured_at", "environment", "account"]


def _meta(i: int = 0) -> dict:
    return {
        "captured_at": f"2026-10-0{1 + i % 9}T09:00:00Z",
        "environment": "staging",
        "account": f"acct-{i:03d}",
        "owner_note": "n" * 300,
        "free_tags": "t" * 300,
    }


def _record(i: int = 0, body: int = 200) -> PackRecord:
    return PackRecord(id=f"rec:{i}", fields={"body": "source text " * body}, metadata=_meta(i))


def _size(metadata: dict) -> int:
    return len(json.dumps({"metadata": metadata}, ensure_ascii=False, separators=(",", ":"))) if metadata else 0


def test_default_share_is_the_v32_setting_and_protected_defaults_to_none():
    config = PackingConfig()
    assert config.metadata_max_share == 0.25 and config.metadata_protected == []


def test_protected_keys_survive_the_cap_and_others_drop_in_reverse_allowlist_order():
    config = PackingConfig(budget_chars=1000, metadata_fields=FIELDS, metadata_protected=PROTECTED, metadata_max_share=0.05)
    result = pack([_record()], config)
    [evidence] = result.evidence
    assert list(evidence.metadata) == PROTECTED  # allowlist order kept, protected never dropped
    assert result.metadata_dropped_keys == {"rec:0": ["free_tags", "owner_note"]}
    assert result.metadata_omitted_ids == ["rec:0"]
    # the protected keys alone exceed 5% of the allocation: kept anyway and reported
    assert result.metadata_protected_over_cap_ids == ["rec:0"] and result.metadata_protected_overflow == {}
    assert result.metadata_characters == _size(evidence.metadata)
    assert len(evidence.passage) == 1000 - result.metadata_characters  # the passage keeps the rest
    unprotected = pack([_record()], config.model_copy(update={"metadata_protected": []}))
    assert unprotected.evidence[0].metadata == {} and unprotected.metadata_dropped_keys["rec:0"] == ["free_tags", "owner_note", "account", "environment", "captured_at"]


def test_unprotected_order_is_reverse_allowlist_even_when_protected_keys_are_interleaved():
    config = PackingConfig(budget_chars=1000, metadata_fields=["owner_note", "captured_at", "free_tags", "account"], metadata_protected=["captured_at"], metadata_max_share=0.01)
    result = pack([_record()], config)
    assert result.metadata_dropped_keys == {"rec:0": ["account", "free_tags", "owner_note"]}
    assert result.evidence[0].metadata == {"captured_at": "2026-10-01T09:00:00Z"}


def test_a_protected_key_is_not_reported_when_the_cap_does_not_bind():
    config = PackingConfig(budget_chars=5000, metadata_fields=FIELDS, metadata_protected=PROTECTED)
    short = PackRecord(id="short", fields={"body": "short"}, metadata=_meta())
    result = pack([short], config)
    assert result.evidence[0].metadata == _meta() and result.metadata_dropped_keys == {} and result.metadata_protected_over_cap_ids == []


def test_passage_share_stays_bounded_with_protected_keys():
    records = [_record(i) for i in range(10)]
    protected = PackingConfig(budget_chars=8000, metadata_fields=FIELDS, metadata_protected=PROTECTED)
    capped = pack(records, protected)
    uncapped = pack(records, protected.model_copy(update={"metadata_max_share": 1.0, "metadata_protected": []}))
    share = capped.passage_characters / capped.evidence_characters
    assert share >= 0.75 - 0.01  # metadata bounded by the 0.25 share (protected keys here fit under it)
    assert uncapped.passage_characters / uncapped.evidence_characters < 0.5  # the M7 regression shape
    for evidence in capped.evidence:
        assert set(PROTECTED) <= set(evidence.metadata)
        assert _size(evidence.metadata) <= 0.25 * 800 + 1
    assert capped.evidence_characters <= 8000


def test_protected_keys_larger_than_the_allocation_trim_the_passage_and_are_traced():
    huge = PackRecord(id="huge", fields={"body": "source text " * 50}, metadata={"captured_at": "x" * 900, "owner_note": "n"})
    config = PackingConfig(budget_chars=500, metadata_fields=["captured_at", "owner_note"], metadata_protected=["captured_at"])
    result = pack([huge], config)
    [evidence] = result.evidence
    assert evidence.metadata == {"captured_at": "x" * 900} and evidence.passage == "" and evidence.truncated
    over = _size(evidence.metadata) - 500
    assert result.metadata_protected_overflow == {"huge": over} and result.metadata_dropped_keys == {"huge": ["owner_note"]}
    assert result.evidence_characters == 500 + over  # the overflow is the only excess, and it is reported
    assert result.omitted_ids == []


def test_protected_keys_must_be_allowlisted_and_unique():
    with pytest.raises(ValueError, match="metadata_protected keys must be listed in metadata_fields"):
        PackingConfig(metadata_fields=["captured_at"], metadata_protected=["environment"])
    with pytest.raises(ValueError):
        PackingConfig(metadata_fields=["captured_at"], metadata_protected=["captured_at", "captured_at"])
    with pytest.raises(ValueError):
        PackingConfig(metadata_fields=["captured_at"], metadata_protected=["Bad-Name"])


def test_leading_extras_are_reserved_first_and_placed_ahead_of_records():
    lead = PackExtra(id="aggregate:1", source="structured", passage="L" * 100)
    other = PackExtra(id="aggregate:2", source="structured", passage="O" * 100)
    config = PackingConfig(budget_chars=1000, aggregate_reserve_max=0.15)
    result = pack([_record()], config, [other], leading=[lead])
    assert [e.id for e in result.evidence] == ["aggregate:1", "rec:0"]  # lead first; the other did not fit the reserve
    assert result.leading_ids == ["aggregate:1"] and result.omitted_ids == ["aggregate:2"]
    plain = pack([_record()], config, [other, lead])
    assert [e.id for e in plain.evidence] == ["rec:0", "aggregate:2"] and plain.leading_ids == []


def test_engine_packing_trace_reports_protected_keys(tmp_path):
    from su_multi_arm import Engine, SearchRequest
    from su_multi_arm.core.config import validate_config
    from su_multi_arm.core.types import Document

    cfg = validate_config({
        "schema_version": 2, "dataset": "synthetic",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}, "selection": {"packing": {
            "budget_chars": 600, "metadata_fields": ["captured_at", "owner_note"], "metadata_protected": ["captured_at"], "metadata_max_share": 0.05,
        }}},
    })
    engine = Engine(cfg)
    engine.ingest([Document(id="case:1", dataset="synthetic", fields={"body": "retry failure " * 200}, metadata={"captured_at": "2026-10-02T09:00:00Z", "owner_note": "n" * 200})])
    search = engine.search(SearchRequest(query="retry failure"))
    evidence, trace = engine._evidence(engine._context_for(None), search)
    assert evidence[0]["metadata"] == {"captured_at": "2026-10-02T09:00:00Z"}
    assert trace["metadata_dropped_keys"] == {"case:1": ["owner_note"]} and trace["metadata_protected_over_cap_ids"] == ["case:1"]
    assert trace["metadata_protected_overflow"] == {} and trace["leading_ids"] == []


def test_config_dump_round_trips_the_projection_relation(tmp_path):
    import typed_support as ts

    from su_multi_arm.core.config import validate_config

    config = ts.config(tmp_path / "state")
    for dumped in (config.model_dump(mode="json"), config.model_dump()):
        assert dumped["projection"]["relations"]["comment_parent"]["from"] == "comment.parent_id"
        assert "from_" not in dumped["projection"]["relations"]["comment_parent"]
        again = validate_config(dumped)
        assert again.projection.relations["comment_parent"].from_ == "comment.parent_id"
        assert again.model_dump(mode="json") == config.model_dump(mode="json")
