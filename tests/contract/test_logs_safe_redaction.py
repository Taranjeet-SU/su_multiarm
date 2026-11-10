"""B11 wired through the daily log path (addendum A1 §3.2.3, §3.8.1): with ``privacy.engine: safe``
the records are redacted before any partition write, the partition manifest names the stamp and
counts per redactor, the leak scan finds no PII shape in the partitions or the state, the egress
policy admits the stored records as they are, and ``logs run-daily`` runs from the CLI. Without a
safe redactor the CLI still refuses (``test_logs_cli.py``). Synthetic data only: the generator's
``example.invalid`` addresses and documentation-range IPs."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import yaml

from su_multi_arm.core.config import validate_config
from su_multi_arm.privacy import safe_redactor, scan_paths
from tests.logs_support import ENGINE, Clock, cluster_for, config_data, pipeline, write_library
from tests.pattern_fixtures import engine_or_skip
from tests.unit.test_config_cli_a2 import run_main

DAY = date(2026, 1, 5)
PRIVACY = {"engine": "safe", "redactors": ["email", "ipv4", "ipv6", "bearer_token", "jwt", "api_key", "url_credentials", "uuid"], "regex_engine": ENGINE}


def safe_data(tmp_path):
    data = config_data(tmp_path)
    data["privacy"] = dict(PRIVACY)
    data["sources"]["synthetic"]["limits"] = {"requests_per_second": 50}
    return data


def test_safe_redaction_runs_before_persistence(tmp_path):
    engine_or_skip(ENGINE)
    data = safe_data(tmp_path)
    cfg = validate_config(data)
    redactor = safe_redactor(cfg.privacy)
    cluster = cluster_for([DAY], lines=60)
    raw = json.dumps(cluster.indices)
    assert "@example.invalid" in raw  # the synthetic day carries PII shapes
    pipe, _, backend = pipeline(tmp_path, cluster, redactor=redactor, data=data, now=Clock(DAY + timedelta(days=1)))
    report = pipe.run_daily(DAY)
    assert report.ok, report.error
    manifest = pipe.store.get(DAY.isoformat())
    assert manifest.redaction["mode"] == redactor.stamp and manifest.redaction["engine"] == "safe"
    assert dict(manifest.redaction["counts"])["email"] > 0
    scan_redactor = safe_redactor(cfg.privacy)
    # The fixture's PII shapes are e-mail and IPv4. Partitions must be clean for every shape; in the
    # state, ``api_key`` also flags the package's own lease ``"token"`` in generation manifests (a
    # known, content-free false positive), so only the fixture's shapes are asserted there.
    for folder, shapes in ((tmp_path / "partitions", None), (tmp_path / "state", {"email", "ipv4"})):
        found = scan_paths([folder], scan_redactor).to_dict()
        leaks = {k: v for k, v in found["by_redactor"].items() if shapes is None or k in shapes}
        assert leaks == {}, (folder.name, found["findings"][:5])


def test_stored_records_reach_the_egress_policy_without_rework(tmp_path):
    engine_or_skip(ENGINE)
    from su_multi_arm.core.config_learning import PatternEgressConfig
    from su_multi_arm.learning.patterns.egress import EgressPolicy, LineRedactor, RedactedLine, lines_from_records

    data = safe_data(tmp_path)
    cfg = validate_config(data)
    pipe, _, _ = pipeline(tmp_path, cluster_for([DAY], lines=40), redactor=safe_redactor(cfg.privacy), data=data, now=Clock(DAY + timedelta(days=1)))
    pipe.run_daily(DAY)
    records = [r for unit in pipe.store.units(DAY.isoformat()) for r in unit.records]
    line_redactor = LineRedactor(cfg.privacy)
    assert line_redactor.stamp == pipe.mapping.redaction
    lines = lines_from_records(records, pipe.mapping, redactor=line_redactor)
    assert lines and all(isinstance(line, RedactedLine) and line.redaction == line_redactor.stamp for line in lines)
    assert len(EgressPolicy(PatternEgressConfig(), line_redactor).admit(lines)) == len(lines)


def test_the_cli_runs_the_daily_pipeline_with_a_safe_redactor(tmp_path, monkeypatch, capsys):
    engine_or_skip(ENGINE)
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    cluster = cluster_for([yesterday], lines=20)
    server, url = cluster.serve()
    try:
        write_library(tmp_path / "patterns")
        path = tmp_path / "logs.yaml"
        path.write_text(yaml.safe_dump(safe_data(tmp_path)))
        monkeypatch.setenv("SU_TEST_LOGS_URL", url)
        code, out, err = run_main(monkeypatch, capsys, "--config", str(path), "logs", "run-daily", "--date", yesterday.isoformat())
    finally:
        server.shutdown()
    assert code == 0, err
    report = json.loads(out)
    assert report["ok"] and report["steps"]["partition"]["status"] == "sealed" and report["steps"]["compose"]["generation"]
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(path), "privacy", "scan", str(tmp_path / "partitions"), "--redactors", "email,ipv4")
    assert code == 0 and json.loads(out)["hits"] == 0


def test_the_cli_scan_exits_non_zero_on_a_leak(tmp_path, monkeypatch, capsys):
    engine_or_skip(ENGINE)
    write_library(tmp_path / "patterns")
    path = tmp_path / "logs.yaml"
    path.write_text(yaml.safe_dump(safe_data(tmp_path)))
    (tmp_path / "leak.txt").write_text("contact ops@example.invalid\n")
    code, out, _ = run_main(monkeypatch, capsys, "--config", str(path), "privacy", "scan", str(tmp_path / "leak.txt"))
    report = json.loads(out)
    assert code == 1 and report["hits"] == 1 and report["findings"] == [{"path": "leak.txt", "location": "line 1", "redactor": "email"}]
    assert "ops@" not in out
