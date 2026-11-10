"""Optional private rollup coordinator; synthetic clocks, inventories, rows, and transport."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

location = os.environ.get("SU_INCIDENT_BATCH_DIR")
if not location:
    pytest.skip("private incident rollup collector not supplied", allow_module_level=True)
sys.path.insert(0, str(Path(location).resolve()))
spec = importlib.util.spec_from_file_location("tested_incident_batch", Path(location) / "incident_batch.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOW = datetime(2026, 1, 5, 16, 45, tzinfo=timezone.utc)
POLICY = {"version": 1, "families": [{"id": "failure", "phrases": ["error"]}],
          "severity_values": ["ERROR"], "http_status_min": 500, "http_status_max": 599,
          "context_seconds": 120, "context_phrases": ["recovered"]}


def scope(name="api"):
    return dict(index=f"demo-{name}-2026.01.04", day="2026-01-04", tenant="demo", env="test", service=name)


def result(index_scope, observed, manifest="run-one", total=3):
    return dict(scope=index_scope, observed_at=observed, finished=observed, manifest=manifest,
                complete_for_selected_seeds=True, count_reconciled=True, policy_id="policy", query_id="query",
                recipe="recipe", collection_window={"start": "2026-01-04T00:00:00Z", "end": "2026-01-05T00:00:00Z"},
                refresh_revision=manifest, source_total=20, selected_total=total, sha256="checksum")


def test_rolling72h_pinned_across_resume_and_four_intersecting_dates(tmp_path):
    path, plan = m.load_plan(tmp_path, "backfill", POLICY, NOW)
    assert plan["windows"] == [{"start": "2026-01-02T16:45:00Z", "end": "2026-01-05T16:45:00Z"}]
    assert m.window_days(plan["windows"][0]) == ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    other_path, resumed = m.load_plan(tmp_path, "backfill", POLICY, NOW.replace(day=6))
    assert other_path == path and resumed["windows"] == plan["windows"]


def test_daily_three_complete_utc_days_and_distinct_revision(tmp_path):
    windows = m.windows_for("daily", NOW)
    assert windows[0] == {"start": "2026-01-04T00:00:00Z", "end": "2026-01-05T00:00:00Z"}
    assert [m.window_days(window) for window in windows] == [["2026-01-04"], ["2026-01-03"], ["2026-01-02"]]
    _, first = m.load_plan(tmp_path, "daily", POLICY, NOW)
    _, next_day = m.load_plan(tmp_path, "daily", POLICY, NOW.replace(day=6))
    assert first["refresh_revision"] != next_day["refresh_revision"]


def test_index_admission_requires_matching_date_and_service_grammar():
    item = scope()
    info = {**item, "date": item["day"], "in_scope": True}
    window = m.windows_for("daily", NOW)[0]
    assert m.admit_scope(info, item["day"], window) == item
    for broken in ({**info, "in_scope": False}, {**info, "date": "2026-01-03"}):
        with pytest.raises(ValueError):
            m.admit_scope(broken, item["day"], window)
    with pytest.raises(ValueError):
        m.admit_scope(info, item["day"], m.windows_for("daily", NOW)[1])


def test_current_snapshot_replacement_not_addition_and_old_backfill_cannot_regress(tmp_path):
    first = result(scope(), "2026-01-05T12:00:00Z", "backfill", total=3)
    newer = result(scope(), "2026-01-06T01:20:00Z", "daily", total=5)
    assert m.publish_current(tmp_path, first)
    assert m.publish_current(tmp_path, newer)
    assert not m.publish_current(tmp_path, first)
    current = json.loads((tmp_path / "current" / (scope()["index"] + ".json")).read_text())
    assert current["selected_total"] == 5 and current["manifest"] == "daily"


def test_serial_rate_cap_and_error_backoff(tmp_path):
    now, starts = [0.0], []
    def request(*args, **kwargs):
        starts.append(now[0])
        return {}
    es = SimpleNamespace(request=request)
    def sleep(seconds):
        now[0] += seconds
    budget = m.SerialBudget(es, tmp_path, 0, clock=lambda: now[0], sleeper=sleep)
    for _ in range(6):
        es.request("GET", "/safe/_mapping")
    assert starts == [0, .5, 1, 1.5, 2, 2.5]
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic429")
    budget.original = failed
    with pytest.raises(RuntimeError):
        es.request("GET", "/safe/_mapping")
    assert budget.interval == 1
    budget.stopped = True
    with pytest.raises(InterruptedError):
        es.request("GET", "/safe/_mapping")


def test_interleaved_capture_and_unsupported_scope_does_not_starve_others(tmp_path, monkeypatch):
    path, plan = m.load_plan(tmp_path / "state", "backfill", POLICY, NOW)
    scopes = [scope("bad"), scope("api"), scope("worker")]
    events = []
    monkeypatch.setattr(m, "discover", lambda *args: scopes)
    def count(es, item, policy, mapping, **kwargs):
        events.append("count:" + item["service"])
        if item["service"] == "bad":
            raise m.query.PolicyError("unsupported mapping")
        return {"source_total": 20, "selected_total": 7}
    monkeypatch.setattr(m.query, "count_scope", count)
    def export(es, item, policy, cfg, adapter, output, **kwargs):
        assert kwargs["max_selected"] is None
        events.append("capture:" + item["service"])
        return result(item, "2026-01-05T17:00:00Z", item["service"], total=3)
    monkeypatch.setattr(m.export_seeds, "export", export)
    es = SimpleNamespace(request=lambda *args, **kwargs: {})
    finished = m.run(es, None, None, POLICY, tmp_path / "state", tmp_path / "exports", path, plan, fetch=True)
    assert events == ["count:bad", "count:api", "capture:api", "count:worker", "capture:worker"]
    assert finished["status"] == "partial" and finished["deferred_indices"] == 1
    assert finished["capture_summary"]["selected_total"] == 6  # Fresh snapshot, not preliminary count14.
    assert not finished["capture_summary"]["complete_for_selected_seeds"]
    assert (tmp_path / "state/current" / (scope("worker")["index"] + ".json")).exists()
    events.clear()
    m.run(es, None, None, POLICY, tmp_path / "state", tmp_path / "exports", path, plan, fetch=True)
    assert events == ["count:bad", "capture:api", "capture:worker"]  # Verify/reuse successful exports.


def test_counted_but_uncaptured_item_is_captured_after_restart(tmp_path, monkeypatch):
    path, plan = m.load_plan(tmp_path / "state", "backfill", POLICY, NOW)
    monkeypatch.setattr(m, "discover", lambda *args: [scope()])
    monkeypatch.setattr(m.query, "count_scope", lambda *args, **kwargs: {"source_total": 20, "selected_total": 3})
    es = SimpleNamespace(request=lambda *args, **kwargs: {})
    m.run(es, None, None, POLICY, tmp_path / "state", tmp_path / "exports", path, plan, fetch=False)
    calls = []
    def export(*args, **kwargs):
        calls.append(kwargs)
        return result(scope(), "2026-01-05T17:00:00Z")
    monkeypatch.setattr(m.export_seeds, "export", export)
    m.run(es, None, None, POLICY, tmp_path / "state", tmp_path / "exports", path, plan, fetch=True)
    assert len(calls) == 1 and calls[0]["resume_manifest"] is None
    assert plan["status"] == "done"


def test_uncapped_export_and_verified_resume_with_corruption_rejection(tmp_path, monkeypatch):
    import zstandard
    item = scope()
    window = m.windows_for("daily", NOW)[0]
    props = {"message": {"type": "text"}, "@timestamp": {"type": "date"}}
    compiled = m.query.compile_query(POLICY, props, window=window)
    monkeypatch.setattr(m.export_seeds, "prepare", lambda *args, **kwargs: (item, None, compiled, "synthetic-recipe"))
    adapter = SimpleNamespace(zstd=zstandard, pull_day=SimpleNamespace(atomic_json=m.atomic_json),
        full_pull=SimpleNamespace(record=lambda hit, *args: {**item, "id": hit["_id"], "message": "MASKED"}))
    class Source:
        def __init__(self):
            self.calls, self.cleared = [], []

        def page(self, start, end):
            return {"timed_out": False, "_scroll_id": "synthetic-owned",
                    "_shards": {"total": 1, "successful": 1, "failed": 0},
                    "hits": {"total": {"value": 1001, "relation": "eq"}, "hits": [
                        {"_index": item["index"], "_id": str(i), "_source": {}} for i in range(start, end)]},
                    "aggregations": {"source_scope": {"doc_count": 9000, "window": {"doc_count": 1200}},
                                     "signals": {"buckets": {"phrase_failure": {"doc_count": 1001}}}}}

        def request(self, *args, **kwargs):
            self.calls.append(args)
            return self.page(0, 1000)

        def scroll(self, *args):
            return self.page(1000, 1001)

        def clear_scroll(self, sid):
            self.cleared.append(sid)
    source = Source()
    output = tmp_path / "exports"
    first = m.export_seeds.export(source, item, POLICY, None, adapter, output,
                                  max_selected=None, window=window, revision="day-one")
    assert first["selected_total"] == first["records_fetched"] == 1001
    assert first["source_total"] == 1200 and first["sample_cap"] is None
    assert first["collection_window"] == window and source.cleared == ["synthetic-owned"]
    count_before = len(source.calls)
    resumed = m.export_seeds.export(source, item, POLICY, None, adapter, output,
        window=window, revision="day-one", resume_manifest=first["manifest"])
    assert resumed["resumed"] and len(source.calls) == count_before
    with pytest.raises(ValueError, match="identity"):
        m.export_seeds.export(source, item, POLICY, None, adapter, output,
            window=window, revision="day-two", resume_manifest=first["manifest"])
    artifact = Path(first["manifest"]).parent / first["file"]
    artifact.write_bytes(artifact.read_bytes()[:-1] + b"X")
    with pytest.raises(ValueError, match="checksum"):
        m.export_seeds.export(source, item, POLICY, None, adapter, output,
            window=window, revision="day-one", resume_manifest=first["manifest"])


def test_discovery_prioritizes_recent_day_without_dropping_old_indices():
    def parse(name):
        day = name[-10:].replace(".", "-")
        return dict(index=name, tenant="demo", env="test", service="api", date=day, in_scope=True)
    def request(method, path, **kwargs):
        day = path[-10:]
        return {"indices": [{"name": "demo-api-" + day}]}
    output = m.discover(SimpleNamespace(request=request), SimpleNamespace(parse=parse), m.windows_for("backfill", NOW)[0])
    assert [item["day"] for item in output] == ["2026-01-05", "2026-01-04", "2026-01-03", "2026-01-02"]


@pytest.mark.parametrize("error", [InterruptedError, OSError, PermissionError, ConnectionError])
def test_systemic_failure_aborts_before_other_scopes(tmp_path, monkeypatch, error):
    path, plan = m.load_plan(tmp_path / "state", "backfill", POLICY, NOW)
    monkeypatch.setattr(m, "discover", lambda *args: [scope("api"), scope("worker")])
    counted = []
    def count(*args, **kwargs):
        counted.append(args[1]["service"])
        return {"source_total": 20, "selected_total": 3}
    monkeypatch.setattr(m.query, "count_scope", count)
    def fail(*args, **kwargs):
        raise error("synthetic stop/disk/auth/network failure")
    monkeypatch.setattr(m.export_seeds, "export", fail)
    with pytest.raises(error):
        m.run(SimpleNamespace(request=lambda *args, **kwargs: {}), None, None, POLICY,
              tmp_path / "state", tmp_path / "exports", path, plan, fetch=True)
    assert counted == ["api"]


def test_unresolved_placeholder_index_is_deferred_without_stopping_valid_capture(tmp_path, monkeypatch):
    window = m.windows_for("daily", NOW)[0]
    valid = scope()
    malformed = "demo-%{[container][name]}-2026.01.04"
    def parse(name):
        return {**valid, "index": name, "date": valid["day"], "in_scope": True}
    def request(method, path, body=None, **kwargs):
        if "_resolve/index" in path:
            return {"indices": [{"name": malformed}, {"name": valid["index"]}]}
        return {}
    es = SimpleNamespace(request=request)
    deferred = []
    admitted = m.discover(es, SimpleNamespace(parse=parse), window, deferred)
    assert admitted == [valid] and len(deferred) == 1
    assert deferred[0]["reason"] == "unsupported_index_admission"
    assert malformed not in json.dumps(deferred)
    path, plan = m.load_plan(tmp_path / "state", "backfill", POLICY, NOW)
    plan["windows"] = [window]
    monkeypatch.setattr(m.query, "count_scope", lambda *args, **kwargs: {"source_total": 20, "selected_total": 3})
    monkeypatch.setattr(m.export_seeds, "export", lambda *args, **kwargs: result(valid, "2026-01-05T17:00:00Z"))
    output = m.run(es, SimpleNamespace(parse=parse), None, POLICY, tmp_path / "state", tmp_path / "exports",
                   path, plan, fetch=True)
    assert output["status"] == "partial" and output["deferred_indices"] == 1
    assert output["capture_summary"]["admitted_indices"] == 1
    assert output["capture_summary"]["completed_indices"] == 1
    assert output["capture_summary"]["admission_deferred_indices"] == 1
    assert not output["capture_summary"]["complete_for_selected_seeds"]
    assert not output["count_summary"]["complete_for_requested_discovery"]
    assert (tmp_path / "state/current" / (valid["index"] + ".json")).exists()


def test_explicit_zero_progress_migration_preserves_original_window(tmp_path):
    path, plan = m.load_plan(tmp_path, "backfill", POLICY, NOW)
    original = plan["windows"]
    predecessor = next(iter(m.ZERO_PROGRESS_PREDECESSORS))
    plan.update(package_id=predecessor, status="failed")
    m.atomic_json(path, plan)
    _, migrated = m.load_plan(tmp_path, "backfill", POLICY, NOW.replace(day=6))
    assert migrated["windows"] == original
    assert migrated["package_migrations"][-1]["from"] == predecessor
    assert migrated["package_migrations"][-1]["to"] == m.package_identity()
    assert migrated["package_migrations"][-1]["pinned_windows_preserved"]


@pytest.mark.parametrize("change", [{"items": {"one": {}}}, {"discovery": {"one": []}}, {"package_id": "unknown"}, {"status": "done"}])
def test_recipe_migration_refuses_progress_or_unknown_predecessor(tmp_path, change):
    path, plan = m.load_plan(tmp_path, "backfill", POLICY, NOW)
    plan.update(package_id=next(iter(m.ZERO_PROGRESS_PREDECESSORS)), status="failed")
    plan.update(change)
    m.atomic_json(path, plan)
    with pytest.raises(ValueError, match="migration"):
        m.load_plan(tmp_path, "backfill", POLICY, NOW.replace(day=6))
