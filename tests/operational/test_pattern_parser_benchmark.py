"""Exercise the offline harness with synthetic context and process-worker parity."""
import gzip
import json
from pathlib import Path
import subprocess
import sys

import yaml

from tests.contract.test_incident_log_ingestion import incident_unit
from tests.logs_support import config_data, write_library


def test_benchmark_reports_same_typed_results_for_serial_and_pool(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    with gzip.open(units / "synthetic.json.gz", "wt") as handle:
        handle.write(incident_unit().model_dump_json())
    library = write_library(tmp_path / "patterns")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(config_data(tmp_path, parse={"batch_size": 1})))
    reports = []
    for workers in (1, 2):
        output = tmp_path / f"workers-{workers}.json"
        subprocess.run([
            sys.executable, str(Path(__file__).parents[2] / "scripts/benchmark_pattern_parser.py"),
            "--config", str(config), "--units", str(units), "--library", str(library),
            "--workers", str(workers), "--output", str(output),
        ], check=True, capture_output=True, text=True)
        reports.append(json.loads(output.read_text()))
    assert reports[0]["output_sha256"] == reports[1]["output_sha256"]
    assert reports[0]["statuses"] == {"parsed": 2, "noise": 1}
    assert all(row["heads"] == 3 and row["approved_patterns"] > 0 for row in reports)
    assert reports[1]["parse_work"]["worker_decode_us"] > 0
