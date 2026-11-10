"""Optional candidate-library regression checks; all log examples are synthetic."""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from su_multi_arm.core.config import PatternLibraryRef
from su_multi_arm.extraction.patterns.extractor import PatternExtractor
from su_multi_arm.ingestion.logs_parse import match_records
from su_multi_arm.sources.logs import LogRecord

location = os.environ.get("SU_INCIDENT_PATTERN_REVIEW_LIBRARY")
if not location:
    pytest.skip("candidate incident library not supplied", allow_module_level=True)


@pytest.fixture(scope="module")
def library():
    return PatternExtractor(PatternLibraryRef(path=Path(location), engine="re2"), None).load()


def record(message, service="admin-search-admin", level=None):
    return LogRecord(id="synthetic-record", source_id="synthetic-source", index="synthetic-index", partition_key="2026-01-01",
                     ts=datetime(2026, 1, 1, tzinfo=timezone.utc), message=message, level=level,
                     attributes={"service": service, "tenant": "synthetic-tenant", "env": "synthetic"})


@pytest.mark.parametrize("service", ["admin", "admin-search-admin"])
def test_explicit_undefined_error_frame_recovers_level_without_inventing_cause(library, service):
    row, = match_records([record("2026-01-01T01:02:03+0000 <error> undefined (in app.js:1200)", service)], library)
    assert row["final_status"] == "parsed"
    assert row["level"] == "error" and row["category"] == "unknown"
    assert {f["name"]: f["value"] for f in row["fields"]}["source_line"] == 1200


@pytest.mark.parametrize("service", ["admin", "admin-search-admin"])
def test_exact_unauthorized_payload_has_typed401(library, service):
    row, = match_records([record('2026-01-01T01:02:03+0000 <error> {"code":401,"message":"Unauthorized"} (in app.js:1200)', service)], library)
    assert row["final_status"] == "parsed" and row["level"] == "error"
    assert row["category"] == "authentication"
    assert {f["name"]: f["value"] for f in row["fields"]}["status_code"] == 401


@pytest.mark.parametrize("message,service,level", [
    ('{"error":null,"errors":0}', "admin", "info"),
    ("2026-01-01T01:02:03+0000 <info> undefined (in app.js:1200)", "admin", None),
    ("2026-01-01T01:02:03+0000 <error> undefined (in other.js:1200)", "admin", None),
    ('2026-01-01T01:02:03+0000 <error> {"code":200,"message":"OK"} (in app.js:1200)', "admin", None),
    ("2026-01-01T01:02:03+0000 <error> undefined (in app.js:1200)", "synthetic-unrelated-service", None),
    ("undefined", "admin", "info"),
])
def test_routine_payloads_other_sources_and_wrong_service_are_not_new_failures(library, message, service, level):
    row, = match_records([record(message, service, level)], library)
    assert row.get("pattern_id") not in {"candidate.incident.admin_undefined_payload", "candidate.incident.admin_unauthorized_payload"}
