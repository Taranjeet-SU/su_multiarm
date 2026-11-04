"""The interim derived archive (A2 §4.8.6, rev 4): per-day snapshots with ``derived_format: 2`` and a
widened identity, format-1 compatibility, the pre-H1 serving bound (``logs.history.serve_days`` and
``max_serve_rows``), window rollups over the masked window only, the multi-identity flag, and the
compose cost bounded by ``serve_days`` (60 synthetic days). Synthetic data only."""

from __future__ import annotations

import gzip
import json
import resource
import time
from datetime import date, timedelta

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.ingestion.logs_compose import DERIVED_FORMAT, derived_dir, derived_keys, read_derived, snapshot_identity, write_derived
from su_multi_arm.ingestion.release import generation_id
from tests.logs_support import ENGINE, MESSAGES, Clock, cluster_for, config_data, days, pipeline, write_library
from tests.pattern_fixtures import engine_or_skip

FIRST = date(2026, 1, 1)


def data_for(tmp_path, *, keep=3, derived="forever", serve=None, max_rows=None):
    data = config_data(tmp_path)
    data["logs"]["retention"].update({"keep_days": keep, "derived": derived})
    history = {}
    if serve is not None:
        history["serve_days"] = serve
    if max_rows is not None:
        history["max_serve_rows"] = max_rows
    if history:
        data["logs"]["history"] = history
    return data


def run(tmp_path, count, **kwargs):
    span = days(FIRST, count)
    clock = Clock(FIRST + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp_path, cluster_for(span, lines=6, tenants=("t001",), services=("query-gateway", "doc-fetcher")), data=data_for(tmp_path, **kwargs), now=clock)
    reports = []
    for day in span:
        clock.set_day(day + timedelta(days=1))
        reports.append(pipe.run_daily(day))
    return pipe, cfg, backend, span, reports


# --- snapshots ------------------------------------------------------------------------------------


def test_snapshot_round_trip_with_overlay_rows_and_identity(tmp_path):
    engine_or_skip(ENGINE)
    pipe, _, _, span, _ = run(tmp_path, 1, keep=3, derived="window")
    key = span[0].isoformat()
    snapshot = read_derived(pipe.store, key)
    assert snapshot["derived_format"] == DERIVED_FORMAT == 2
    assert snapshot["overlay"]["status"] == "source_unavailable" and snapshot["typed_inputs"]["status"] == "source_unavailable"
    assert snapshot["accounting"]["counts"]["records"] > 0 and set(snapshot["accounting"]) == {"counts", "unit_counts", "sample_methods", "capped", "redaction"}
    assert "matches.jsonl.gz" not in snapshot["artifacts"] and "unparsed.jsonl.gz" not in snapshot["artifacts"]  # never line-level rows
    manifest = pipe.store.get(key)
    assert snapshot["identity"] == snapshot_identity(manifest)
    # a fallback overlay is sealed for the version (``OverlayStore``, the final interface): the identity
    # changes and the snapshot is rewritten with the overlay's accounting and row aggregates
    from datetime import datetime, timezone

    from su_multi_arm.ingestion.accounting import FieldValue, HeadAccount
    from su_multi_arm.ingestion.overlay import OverlayManifest, OverlayStore

    overlays = OverlayStore(pipe.store)
    identity = "0123456789abcdef0123456789abcdef"
    unit = manifest.units[0]
    head = HeadAccount(
        head_id="h1", unit_id=unit, final_status="model_line", stage_status="unparsed", extraction_source="model_assisted",
        fields=[FieldValue(name="endpoint", value="/secret-path", type="string", start=0, end=12, source="model_line", reply_sha="r", model="m")],
    )
    overlays.write_unit(key, manifest.version, identity, unit, [head])
    for other in manifest.units[1:]:
        overlays.write_unit(key, manifest.version, identity, other, [])
    overlays.seal(OverlayManifest(identity=identity, partition_key=key, version=manifest.version, library_identity=manifest.library_identity, units=list(manifest.units), created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)))
    entry = write_derived(pipe.store, manifest)
    rewritten = read_derived(pipe.store, key)
    assert entry["identity"] != snapshot["identity"] == snapshot_identity(manifest)
    assert rewritten["overlay_identity"] == identity
    assert rewritten["overlay"]["rows"] == {"heads": 1, "by_status": {"model_line": 1}, "by_signature_source": {}, "by_field_source": {"endpoint|model_line": 1}}
    # a snapshot holds no line-level rows: no head ids, reply shas or per-line values; a model-extracted
    # value appears only as an aggregate dimension, like a deterministic endpoint template (A2 L5/B7)
    text = json.dumps(rewritten)
    assert '"head_id"' not in text and '"reply_sha"' not in text and '"h1"' not in text
    assert [(r["endpoint"], r["lines"]) for r in rewritten["overlay"]["aggregates"]["endpoints"]] == [("/secret-path", 1)]
    assert write_derived(pipe.store, manifest)["identity"] == entry["identity"]  # unchanged: not rewritten


def test_format_one_snapshots_are_read_and_marked(tmp_path):
    engine_or_skip(ENGINE)
    pipe, _, _, span, _ = run(tmp_path, 1, keep=3, derived="window")
    key = span[0].isoformat()
    current = read_derived(pipe.store, key)
    legacy = {"key": key, "version": current["version"], "library_identity": current["library_identity"], "aggregates": current["aggregates"], "hourly_cells": current["hourly_cells"], "exemplars": current["exemplars"]}
    (derived_dir(pipe.store) / f"{key}.json.gz").write_bytes(gzip.compress(json.dumps(legacy).encode()))
    read = read_derived(pipe.store, key)
    assert read["derived_format"] == 1 and read["legacy"] and read["typed_inputs"]["status"] == "source_unavailable"
    assert read["accounting"]["status"] == "source_unavailable" and read["aggregates"] == current["aggregates"]


# --- serving bound --------------------------------------------------------------------------------


def served_dates(backend):
    manifest = backend.generation_registry().current()
    return [p["key"] for p in manifest["logs"]["partitions"]] + [d["key"] for d in manifest["logs"].get("derived", [])], manifest


def test_serve_days_bounds_what_is_served_and_archives_the_rest(tmp_path):
    engine_or_skip(ENGINE)
    pipe, _, backend, span, _ = run(tmp_path, 7, keep=3, derived="forever")  # serve_days defaults to keep_days
    served, manifest = served_dates(backend)
    assert sorted(served) == [d.isoformat() for d in span[-3:]] and "derived" not in manifest["logs"]
    assert derived_keys(pipe.store) == [d.isoformat() for d in span]  # archived, not served
    gen = generation_id(manifest)
    assert not backend.records.exists(gen, "date", span[0].isoformat(), [])


def test_serve_days_and_max_serve_rows(tmp_path):
    engine_or_skip(ENGINE)
    pipe, _, backend, span, reports = run(tmp_path, 7, keep=3, derived="forever", serve=5)
    served, manifest = served_dates(backend)
    assert sorted(served) == [d.isoformat() for d in span[-5:]] and manifest["logs"]["serve_days"] == 5
    def rows(day):
        snapshot = read_derived(pipe.store, day.isoformat())
        return len(snapshot["aggregates"]["units"]) + len(snapshot["aggregates"]["signatures"]) + len(snapshot["hourly_cells"])

    budget = sum(rows(d) for d in span[-3:]) + rows(span[-4])  # the masked window plus the newest derived day
    tight = pipeline(tmp_path, cluster_for(span, lines=6, tenants=("t001",), services=("query-gateway", "doc-fetcher")), data=data_for(tmp_path, keep=3, derived="forever", serve=5, max_rows=budget), now=pipe.now)[0]
    result = tight.compose(span[-1], tight.library())
    assert result.derived and len(result.derived) == 1 and result.archived_unserved  # the newest derived day only
    assert result.derived[0]["key"] == span[-4].isoformat()


def test_serve_days_is_validated(tmp_path):
    with pytest.raises(ConfigError, match="at least retention.keep_days"):
        validate_config(data_for(tmp_path, keep=3, serve=2))
    with pytest.raises(ConfigError, match="exceeds the derived data"):
        validate_config(data_for(tmp_path, keep=3, derived=4, serve=6))


def test_window_rollups_cover_the_masked_window_only(tmp_path):
    engine_or_skip(ENGINE)
    _, _, backend, span, _ = run(tmp_path, 6, keep=2, derived="forever", serve=6)
    gen = generation_id(backend.generation_registry().current())
    from su_multi_arm.core.types import Predicate, ScopeSpec

    def total(kind, extra):
        result = backend.records.execute(gen, ScopeSpec(predicates=[Predicate(field="kind", op="eq", value=kind), *extra], measure={"kind": "sum", "field": extra and "lines" or "lines_window"}), [])
        return result.value

    masked = [d.isoformat() for d in span[-2:]]
    windowed = total("signature_window", [])
    day_lines = total("signature_day", [Predicate(field="date", op="in", value=masked)])
    everything = total("signature_day", [Predicate(field="date", op="in", value=[d.isoformat() for d in span])])
    assert windowed == day_lines < everything  # archived and derived days do not enter the window rollups


def test_mixed_library_identities_are_flagged(tmp_path):
    engine_or_skip(ENGINE)
    span = days(FIRST, 4)
    clock = Clock(FIRST + timedelta(days=1))
    cluster = cluster_for(span, lines=6, tenants=("t001",), services=("query-gateway", "doc-fetcher"))
    pipe, cfg, backend = pipeline(tmp_path, cluster, data=data_for(tmp_path, keep=2, derived="forever", serve=4), now=clock)
    for day in span[:3]:
        clock.set_day(day + timedelta(days=1))
        pipe.run_daily(day)
    assert "identity_span" not in backend.generation_registry().current()["logs"]
    extra = dict(MESSAGES[2], id="msg.dependency.slow.v2", priority=31, signature={"id": "dependency.slow", "category": "latency"})
    write_library(tmp_path / "patterns", [extra])  # a new library identity
    clock.set_day(span[3] + timedelta(days=1))
    pipe.run_daily(span[3])
    section = backend.generation_registry().current()["logs"]
    assert section["identity_span"]["identities"] == 2 and len(section["library_identities"]) == 2


# --- compose cost over 60 days --------------------------------------------------------------------


def test_compose_cost_is_bounded_by_serve_days_over_60_days(tmp_path):
    """60 synthetic days with ``derived: forever`` and the default serve window (10): every day is
    archived, compose keeps building at most 10 days, unchanged days come from the cache, and the
    compose time does not grow with history. Times and peak RSS are printed for VALIDATION."""
    engine_or_skip(ENGINE)
    span = days(FIRST, 60)
    clock = Clock(FIRST + timedelta(days=1))
    cluster = cluster_for(span, lines=4, tenants=("t001",), services=("query-gateway", "doc-fetcher"))
    pipe, _, backend = pipeline(tmp_path, cluster, data=data_for(tmp_path, keep=10, derived="forever"), now=clock)
    seconds, reused, documents = [], [], []
    for day in span:
        clock.set_day(day + timedelta(days=1))
        library = pipe.library()
        with pipe.store.schedule_lock():
            pipe.pull(day, library)
        started = time.perf_counter()
        result = pipe.compose(day, library)
        seconds.append(time.perf_counter() - started)
        reused.append(len(result.reused_days))
        documents.append(result.documents)
        pipe.retention(pipe.today())
    peak_gib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    early, late = sum(seconds[10:20]) / 10, sum(seconds[50:60]) / 10
    print(json.dumps({"days": 60, "compose_s_days_11_20": round(early, 3), "compose_s_days_51_60": round(late, 3), "documents_last": documents[-1], "peak_rss_gib": round(peak_gib, 2)}))
    assert len(derived_keys(pipe.store)) == 60
    assert max(documents[10:]) <= max(documents[:10]) * 1.5  # served documents bounded by serve_days
    assert min(reused[11:]) >= 9  # every day but the new one comes from the cache
    assert late <= early * 2 + 0.05  # no growth with history
