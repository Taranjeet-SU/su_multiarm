"""Retention scopes per data class and the optional raw-copy tier (owner decision 2026-11-04),
synthetic data only.

* ``raw_days: 3``, ``keep_days: 10``, ``derived: forever``: after 13 days the raw copies hold the
  last 3 days, the partitions the last 10, and every derived day (from day 1) is still in the
  serving generation and queryable; ``retention verify`` passes and reports the retained days.
* The raw store is off by default, ``0700``/``0600``, readable only for ``reparse``/``audit``, never
  imported by ingest, indexing, compose, egress, learning or serving, skipped by ``privacy scan``
  unless ``--raw``; ``logs reparse --from-raw`` re-runs redaction, and refuses without raw copies.
"""

from __future__ import annotations

import json
import re
import stat
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from su_multi_arm.core.config import validate_config
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.ingestion.raw_reparse import read_day, read_unit
from su_multi_arm.ingestion.raw_store import RawAccessRefused, RawStore, RawUnavailable
from su_multi_arm.ingestion.release import generation_id
from tests.logs_support import ENGINE, Clock, cluster_for, config_data, days, pipeline, redact, write_library
from tests.pattern_fixtures import engine_or_skip
from tests.unit.test_config_cli_a2 import run_main

ROOT = Path(__file__).resolve().parents[2]
FIRST = date(2026, 1, 1)


def scoped_data(tmp_path, serve_days=None, **retention):
    data = config_data(tmp_path)
    if serve_days is not None:
        data["logs"]["history"] = {"serve_days": serve_days}
    data["logs"]["partitions"]["raw"] = {"enabled": True, "path": str(tmp_path / "raw")}
    data["logs"]["retention"].update({"raw_days": 3, "partition_days": 10, "derived": "forever", **retention})
    data["logs"]["retention"].pop("keep_days", None)
    return data


@pytest.fixture(scope="module")
def thirteen_days(tmp_path_factory):
    engine_or_skip(ENGINE)
    tmp = tmp_path_factory.mktemp("scopes")
    span = days(FIRST, 13)
    clock = Clock(FIRST + timedelta(days=1))
    data = scoped_data(tmp, serve_days=30)
    pipe, cfg, backend = pipeline(tmp, cluster_for(span, lines=10), data=data, now=clock)
    reports = []
    for day in span:
        clock.set_day(day + timedelta(days=1))
        reports.append(pipe.run_daily(day))
    return {"pipe": pipe, "cfg": cfg, "backend": backend, "reports": reports, "tmp": tmp, "span": span}


def test_every_scoped_run_verifies(thirteen_days):
    for report in thirteen_days["reports"]:
        assert report.ok, report.error
        assert report.steps["verify"]["ok"], report.steps["verify"]["problems"]


def test_raw_keeps_three_days_partitions_ten(thirteen_days):
    pipe, span = thirteen_days["pipe"], thirteen_days["span"]
    assert pipe.raw.keys() == [d.isoformat() for d in span[-3:]]
    assert pipe.store.keys() == [d.isoformat() for d in span[-10:]]
    last = thirteen_days["reports"][-1].steps["verify"]
    assert last["checks"]["retained_derived_days"] == [d.isoformat() for d in span[:3]]


def test_derived_data_from_day_one_is_still_served_and_queryable(thirteen_days):
    backend, span = thirteen_days["backend"], thirteen_days["span"]
    manifest = backend.generation_registry().current()
    assert [d["key"] for d in manifest["logs"]["derived"]] == [d.isoformat() for d in span[:3]]
    assert [p["key"] for p in manifest["logs"]["partitions"]] == [d.isoformat() for d in span[-10:]]
    gen = generation_id(manifest)
    for day in span:
        assert backend.records.exists(gen, "date", day.isoformat(), []), day
    from su_multi_arm.core.types import Predicate, ScopeSpec

    first_day = backend.records.execute(gen, ScopeSpec(predicates=[Predicate(field="date", op="eq", value=span[0].isoformat()), Predicate(field="kind", op="eq", value="signature_day")]), [])
    assert first_day.total > 0 and first_day.exactness == "exact"


def test_retention_report_lists_the_scopes(thirteen_days):
    from su_multi_arm.ingestion.retention import Retention

    pipe, backend = thirteen_days["pipe"], thirteen_days["backend"]
    report = Retention(thirteen_days["cfg"], pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now).report(pipe.today())
    scopes = report["scopes"]
    assert scopes["derived"]["scope"] == "forever" and scopes["derived"]["cutoff"] is None
    assert scopes["raw"]["raw_days"] == 3 and scopes["raw"]["days"] == [d.isoformat() for d in thirteen_days["span"][-3:]]
    assert scopes["partitions"]["keep_days"] == 10


def test_a_derived_day_count_expires_derived_data_too(tmp_path):
    engine_or_skip(ENGINE)
    span = days(FIRST, 6)
    clock = Clock(FIRST + timedelta(days=1))
    data = scoped_data(tmp_path, serve_days=4, raw_days=1, partition_days=2, derived=4)
    pipe, _, backend = pipeline(tmp_path, cluster_for(span, lines=6), data=data, now=clock)
    for day in span:
        clock.set_day(day + timedelta(days=1))
        report = pipe.run_daily(day)
        assert report.ok and report.steps["verify"]["ok"], report.steps["verify"]["problems"]
    manifest = backend.generation_registry().current()
    assert [d["key"] for d in manifest["logs"]["derived"]] == [d.isoformat() for d in span[2:4]]
    gen = generation_id(manifest)
    assert not backend.records.exists(gen, "date", span[1].isoformat(), [])


def test_scope_settings_are_validated(tmp_path):
    with pytest.raises(ConfigError, match="at least keep_days"):
        validate_config(scoped_data(tmp_path, derived=5))
    with pytest.raises(ConfigError, match="shorter than keep_days"):
        validate_config(scoped_data(tmp_path, raw_days=10))
    data = scoped_data(tmp_path)
    data["logs"]["partitions"]["raw"]["path"] = str(tmp_path / "partitions" / "raw")
    with pytest.raises(ConfigError, match="outside logs.partitions.path"):
        validate_config(data)
    data = scoped_data(tmp_path)
    data["logs"]["partitions"]["raw"]["path"] = str(tmp_path / "state" / "raw")
    with pytest.raises(ConfigError, match="overlaps backends.local.path"):
        validate_config(data)
    plain = validate_config(config_data(tmp_path))
    assert plain.logs.partitions.raw.enabled is False and plain.logs.retention.derived == "window"


# --- the raw tier -----------------------------------------------------------------------------------


def test_raw_copies_are_private_and_unredacted(tmp_path):
    engine_or_skip(ENGINE)
    day = FIRST
    pipe, _, _ = pipeline(tmp_path, cluster_for([day], lines=40), data=scoped_data(tmp_path), now=Clock(day + timedelta(days=1)))
    pipe.run_daily(day)
    root = tmp_path / "raw"
    for path in [root, *root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path
    unit = pipe.raw.unit_ids(day.isoformat())[0]
    payload = read_unit(pipe.raw, day.isoformat(), unit, purpose="audit")
    assert payload["docs"] and "unit" in payload
    raw_text = json.dumps([read_unit(pipe.raw, day.isoformat(), u, purpose="audit") for u in pipe.raw.unit_ids(day.isoformat())])
    stored = json.dumps([r.message for a in pipe.store.units(day.isoformat()) for r in a.records])
    assert "@example.invalid" in raw_text and "@example.invalid" not in stored  # raw is pre-redaction
    with pytest.raises(RawAccessRefused):
        read_unit(pipe.raw, day.isoformat(), unit, purpose="serving")


def test_reparse_from_raw_reruns_redaction(tmp_path):
    engine_or_skip(ENGINE)
    day = FIRST
    pipe, cfg, backend = pipeline(tmp_path, cluster_for([day], lines=40), data=scoped_data(tmp_path), now=Clock(day + timedelta(days=1)), redactor=lambda t: t.replace("@", "(at)"))
    pipe.run_daily(day)
    before = pipe.store.get(day.isoformat()).version
    from su_multi_arm.ingestion.schedule import LogsPipeline

    stricter = LogsPipeline(cfg, source=pipe.source, write_path=pipe.write_path, redactor=redact, backends={"local": backend}, now=pipe.now)
    result = stricter.reparse(day, from_raw=True)
    assert result["from_raw"] and result["version"] == before + 1
    messages = " ".join(r.message for a in stricter.store.units(day.isoformat()) for r in a.records)
    assert "(at)" not in messages and "<email>" in messages


def test_reparse_from_raw_refuses_without_a_copy(tmp_path):
    engine_or_skip(ENGINE)
    day = FIRST
    pipe, _, _ = pipeline(tmp_path, cluster_for([day], lines=8), now=Clock(day + timedelta(days=1)))  # raw off
    pipe.run_daily(day)
    with pytest.raises(RawUnavailable, match="settle"):
        pipe.reparse(day, from_raw=True)
    assert pipe.reparse(day)["status"] == "reparsed"
    store = RawStore(tmp_path / "rawx", "synthetic")
    with pytest.raises(RawUnavailable, match="settle"):
        list(read_day(store, day.isoformat(), ["u1"], purpose="reparse"))


READERS = ["engine.py", "api.py", "mcp.py", "serving", "arms", "backends", "learning", "extraction", "decision", "ingestion/write.py", "ingestion/logs_compose.py", "ingestion/rollups.py", "ingestion/logs_parse.py", "privacy"]


def test_no_ingest_index_egress_or_serving_module_can_open_raw():
    package = ROOT / "src" / "su_multi_arm"
    for name in READERS:
        target = package / name
        files = [target] if target.is_file() else sorted(target.rglob("*.py"))
        for path in files:
            text = path.read_text()
            assert not re.search(r"raw_store|RawStore|partitions\.raw", text), f"{path.relative_to(package)} references the raw store"


def test_privacy_scan_skips_raw_unless_asked(tmp_path, monkeypatch, capsys):
    engine_or_skip(ENGINE)
    day = FIRST
    data = scoped_data(tmp_path)
    data["privacy"] = {"engine": "safe", "redactors": ["email"], "regex_engine": ENGINE}
    pipe, _, _ = pipeline(tmp_path, cluster_for([day], lines=40), data=data, now=Clock(day + timedelta(days=1)))
    pipe.run_daily(day)
    write_library(tmp_path / "patterns")
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data))
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(path), "privacy", "scan", str(tmp_path))
    report = json.loads(out)
    assert all("raw store" in s for s in report["skipped"] if s.startswith("raw/"))
    assert not any(f["path"].startswith("raw/") for f in report["findings"])
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(path), "privacy", "scan", "--raw")
    audit = json.loads(out)
    assert code == 1 and audit["raw"] and audit["by_redactor"]["email"] > 0


# --- raw tier hardening (plan rev 3, A2 §11.0) ------------------------------------------------------

# raw_store (file layout) is imported by the gateway module only (A2 §11.R4 n-r3-1)
RAW_STORE_IMPORTERS = {"su_multi_arm.ingestion.raw_reparse"}
# the gateway: the puller (open, write), reparse/rederive, retention (open: list/expire) and the audit
RAW_READ_IMPORTERS = {
    ("su_multi_arm.ingestion.schedule", "__init__"),
    ("su_multi_arm.ingestion.schedule", "_pull_unit"),
    ("su_multi_arm.ingestion.schedule", "reparse"),
    ("su_multi_arm.ingestion.schedule", "rederive"),
    ("su_multi_arm.ingestion.retention", "__init__"),
    ("su_multi_arm.cli", "_privacy_scan"),
}
RAW_READS = {"read_day", "read_unit", "audit_root"}


def _modules():
    import ast

    package = ROOT / "src"
    for path in sorted((package / "su_multi_arm").rglob("*.py")):
        parts = path.relative_to(package).with_suffix("").parts
        yield ".".join(parts[:-1] if parts[-1] == "__init__" else parts), path.name == "__init__.py", ast.parse(path.read_text())


def _resolve(module: str, is_package: bool, node) -> str:
    base = node.module or ""
    if node.level:
        anchor = module.split(".") if is_package else module.split(".")[:-1]
        anchor = anchor[: len(anchor) - (node.level - 1)]
        base = ".".join([*anchor, base]) if base else ".".join(anchor)
    return base


def _importing(target: str, modules) -> set[tuple[str, str]]:
    """``(module, enclosing function or "<module>")`` of every import of ``target``."""
    import ast

    found = set()
    for name, pkg, tree in modules:
        scopes = [("<module>", tree)] + [(f.name, f) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope, node in scopes:
            body = node.body if scope != "<module>" else [n for n in tree.body]
            for child in [n for b in body for n in ast.walk(b)] if scope != "<module>" else body:
                if isinstance(child, ast.ImportFrom):
                    base = _resolve(name, pkg, child)
                    if base == target or (base == target.rsplit(".", 1)[0] and any(a.name == target.rsplit(".", 1)[1] for a in child.names)):
                        found.add((name, scope))
                if isinstance(child, ast.Import) and any(a.name == target for a in child.names):
                    found.add((name, scope))
    return found


def test_the_import_graph_reaches_raw_only_from_its_entry_points():
    """``raw_store`` (the file layout, no read method) is imported only by ``ingestion/raw_reparse.py``;
    raw content is read only there and in the ``privacy scan --raw`` audit. The gateway is imported
    only by the puller, ``logs reparse --from-raw``, the ``rederive`` step, retention and the audit;
    ``schedule.py`` and ``learning/`` never import ``raw_store``."""
    import ast

    modules = list(_modules())
    store_importers = {m for m, _ in _importing("su_multi_arm.ingestion.raw_store", modules)} - {"su_multi_arm.ingestion.raw_store"}
    assert store_importers == RAW_STORE_IMPORTERS
    read_importers = _importing("su_multi_arm.ingestion.raw_reparse", modules)
    assert read_importers == RAW_READ_IMPORTERS
    callers = set()
    for name, _pkg, tree in modules:
        for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
            fn = call.func
            called = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if called in RAW_READS:
                callers.add(name)
    assert callers == {"su_multi_arm.ingestion.raw_reparse", "su_multi_arm.cli"}
    assert not any(hasattr(RawStore, attr) for attr in RAW_READS)
    # schedule.py and learning/ never touch the layout module (directly or at module level)
    for name, _pkg, tree in modules:
        if name == "su_multi_arm.ingestion.schedule" or name.startswith("su_multi_arm.learning"):
            assert "raw_store" not in ast.dump(tree), name
            if name.startswith("su_multi_arm.learning"):
                assert "raw_reparse" not in ast.dump(tree), name


def test_a_writable_parent_is_refused(tmp_path):
    from su_multi_arm.ingestion.raw_store import RawLocationUnsafe

    shared = tmp_path / "shared"
    shared.mkdir()
    for mode in (0o777, 0o775, 0o757):
        shared.chmod(mode)
        with pytest.raises(RawLocationUnsafe):
            RawStore(shared / "raw", "synthetic")
    shared.chmod(0o755)
    RawStore(shared / "raw", "synthetic")


def test_raw_inside_the_installed_package_is_refused(tmp_path):
    data = scoped_data(tmp_path)
    import su_multi_arm

    # the package that is actually imported (the source tree on an editable install, site-packages otherwise)
    data["logs"]["partitions"]["raw"]["path"] = str(Path(su_multi_arm.__file__).resolve().parent / "raw")
    with pytest.raises(ConfigError, match="installed package"):
        validate_config(data)


def test_verify_fails_on_a_wrong_mode_or_an_old_raw_day(tmp_path):
    engine_or_skip(ENGINE)
    from su_multi_arm.ingestion.retention import Retention

    day = FIRST
    pipe, cfg, backend = pipeline(tmp_path, cluster_for([day], lines=8), data=scoped_data(tmp_path), now=Clock(day + timedelta(days=1)))
    assert pipe.run_daily(day).steps["verify"]["ok"]
    retention = Retention(cfg, pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now)
    some = next((tmp_path / "raw").rglob("*.json.gz"))
    for mode in (0o640, 0o400):  # wider and narrower: only 600 passes
        some.chmod(mode)
        assert any("expected 0o600" in p for p in retention.verify(pipe.today())["problems"])
    some.chmod(0o600)
    assert retention.verify(pipe.today())["ok"]
    pipe.raw.put_unit((day - timedelta(days=30)).isoformat(), "0" * 32, {"docs": []})
    assert any("older than the raw cutoff" in p for p in retention.verify(pipe.today())["problems"])


def test_a_redaction_change_rederives_only_days_in_the_raw_tier(tmp_path):
    engine_or_skip(ENGINE)
    from su_multi_arm.core.config import PrivacyConfig
    from su_multi_arm.ingestion.schedule import LogsPipeline
    from su_multi_arm.privacy import safe_redactor

    span = days(FIRST, 4)
    clock = Clock(FIRST + timedelta(days=1))
    data = scoped_data(tmp_path, raw_days=2, partition_days=4, derived="window")
    first = safe_redactor(PrivacyConfig(engine="safe", redactors=["email"], regex_engine=ENGINE))
    pipe, cfg, backend = pipeline(tmp_path, cluster_for(span, lines=12), data=data, now=clock, redactor=first)
    for day in span[:3]:
        clock.set_day(day + timedelta(days=1))
        assert pipe.run_daily(day).steps["rederive"]["status"] == "current"
    assert pipe.raw.keys() == [d.isoformat() for d in span[1:3]]
    second = safe_redactor(PrivacyConfig(engine="safe", redactors=["email", "ipv4"], regex_engine=ENGINE))
    changed = LogsPipeline(cfg, source=pipe.source, write_path=pipe.write_path, redactor=second, backends={"local": backend}, now=clock)
    clock.set_day(span[3] + timedelta(days=1))
    report = changed.run_daily(span[3])
    assert report.ok, report.error
    assert report.steps["rederive"] == {"status": "rederived", "rederived": [d.isoformat() for d in span[1:3]], "stale_redaction": [span[0].isoformat()]}
    assert changed.store.get(span[2].isoformat()).redaction["mode"] == second.stamp
    assert changed.store.get(span[0].isoformat()).redaction["mode"] == first.stamp
