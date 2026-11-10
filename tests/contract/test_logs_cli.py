"""``su-multi-arm logs ...`` and ``retention ...`` (addendum A1 L3a/L3b): the CLI refuses to pull
without a redactor (slice B11 supplies one) before any request, and reads sealed partitions for
``compose`` and ``retention``."""

from __future__ import annotations

import json
from datetime import date, timedelta

import yaml

from tests.logs_support import ENGINE, Clock, cluster_for, config_data, pipeline, write_library
from tests.pattern_fixtures import engine_or_skip

DAY = date(2026, 1, 5)


def run_main(monkeypatch, capsys, *argv):
    from su_multi_arm.cli import main

    monkeypatch.setattr("sys.argv", ["su-multi-arm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


def write_config(tmp_path):
    write_library(tmp_path / "patterns")
    path = tmp_path / "logs.yaml"
    path.write_text(yaml.safe_dump(config_data(tmp_path)))
    return path


def test_run_daily_refuses_without_a_redactor(tmp_path, monkeypatch, capsys):
    engine_or_skip(ENGINE)
    path = write_config(tmp_path)
    monkeypatch.setenv("SU_TEST_LOGS_URL", "http://127.0.0.1:9")  # never contacted
    code, out, err = run_main(monkeypatch, capsys, "--config", str(path), "logs", "run-daily", "--date", DAY.isoformat())
    assert code == 2 and "no redactor" in err and "Traceback" not in err
    assert not (tmp_path / "partitions" / "synthetic").exists()


def test_compose_and_retention_read_sealed_partitions(tmp_path, monkeypatch, capsys):
    engine_or_skip(ENGINE)
    path = write_config(tmp_path)
    pipe, _, _ = pipeline(tmp_path, cluster_for([DAY], lines=8), now=Clock(DAY + timedelta(days=1)))
    pipe.run_daily(DAY)
    code, out, err = run_main(monkeypatch, capsys, "--config", str(path), "logs", "compose", "--date", DAY.isoformat())
    assert code == 0, err
    assert json.loads(out)["status"] == "unchanged"
    code, out, err = run_main(monkeypatch, capsys, "--config", str(path), "retention", "report", "--date", DAY.isoformat())
    assert code == 0, err
    report = json.loads(out)
    assert [p["key"] for p in report["partitions"]] == [DAY.isoformat()] and report["generations"][0]["active"]
    code, out, err = run_main(monkeypatch, capsys, "--config", str(path), "retention", "verify", "--date", DAY.isoformat())
    assert code == 0 and json.loads(out)["ok"], out
