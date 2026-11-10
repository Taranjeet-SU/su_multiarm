"""Optional real compiler/projection integration on synthetic mocked source pages."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest
from su_multi_arm.core.config import validate_config
from tests.logs_support import ENGINE, config_data, write_library

location = os.environ.get("SU_INCIDENT_EXPORT_DIR")
adapter_location = os.environ.get("SU_INCIDENT_ADAPTER_DIR")
if not location or not adapter_location or not (Path(location) / "export_seeds.py").is_file():
    pytest.skip("optional incident export helpers not supplied", allow_module_level=True)
sys.path.insert(0, str(Path(adapter_location).resolve()))
sys.path.insert(0, str(Path(location).resolve()))
import grouped_pull as adapter  # noqa: E402

spec = importlib.util.spec_from_file_location("synthetic_seed_export", Path(location) / "export_seeds.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SCOPE = {
    "index": "su-synthetic-production-admin-2026.01.01",
    "day": "2026-01-01",
    "tenant": "synthetic",
    "env": "production",
    "service": "admin",
}
POLICY = {
    "version": 1,
    "families": [{"id": "failure", "phrases": ["failed"]}],
    "severity_values": ["ERROR"],
    "http_status_min": 500,
    "http_status_max": 599,
    "context_seconds": 120,
    "context_phrases": ["recovered"],
}


class FakeSource:
    def __init__(self, selected=1):
        self.selected = selected
        self.calls = []
        self.cleared = []

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path.endswith("/_mapping"):
            return {
                SCOPE["index"]: {
                    "mappings": {"properties": {"message": {"type": "text"}, "level": {"type": "keyword"}}}
                }
            }
        return {
            "_scroll_id": "synthetic-scroll",
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {
                "total": {"value": self.selected, "relation": "eq"},
                "hits": [
                    {
                        "_index": SCOPE["index"],
                        "_id": "synthetic-id",
                        "_source": {
                            "@timestamp": "2026-01-01T10:00:00Z",
                            "level": "ERROR",
                            "message": "request failed person@example.com",
                        },
                    }
                ],
            },
            "aggregations": {
                "source_scope": {"doc_count": 7},
                "signals": {
                    "buckets": {
                        "phrase_failure": {"doc_count": self.selected},
                        "native_severity": {"doc_count": self.selected},
                    }
                },
            },
        }

    def clear_scroll(self, sid):
        self.cleared.append(sid)

    def scroll(self, sid, keepalive):
        pytest.fail("unexpected second page")


def config(tmp_path):
    write_library(tmp_path / "patterns")
    data = config_data(tmp_path)
    data["privacy"] = {"engine": "safe", "regex_engine": ENGINE, "redactors": ["api_key", "email"]}
    return validate_config(data)


def test_real_compiler_and_projection_seal_separate_filtered_artifact(tmp_path):
    source = FakeSource()
    result = m.export(source, SCOPE, POLICY, config(tmp_path), adapter, tmp_path / "exports", max_selected=1)
    assert result["source_total"] == 7 and result["selected_total"] == result["unique_source_ids"] == 1
    assert result["status"] == "done" and result["complete_for_selected_seeds"]
    assert not result["full_capture"] and not result["complete_for_scope"] and result["context_pending"]
    manifest = Path(result["manifest"])
    artifact = manifest.parent / result["file"]
    assert result["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    with artifact.open("rb") as stream, adapter.zstd.ZstdDecompressor().stream_reader(stream) as decoded:
        data = decoded.read().decode()
    assert "person@example.com" not in data
    assert json.loads(data)["kind"] == "incident_seed"
    assert result["recipe"] in str(manifest) and source.cleared == ["synthetic-scroll"]
    request = source.calls[1][2]
    assert request["size"] == 1000 and "match_all" not in json.dumps(request["query"])
    assert request["aggs"]["source_scope"] == {"global": {}}


def test_selected_budget_excess_cannot_seal_or_fallback(tmp_path):
    source = FakeSource(selected=2)
    with pytest.raises(ValueError, match="capture budget"):
        m.export(source, SCOPE, POLICY, config(tmp_path), adapter, tmp_path / "exports", max_selected=1)
    manifests = list((tmp_path / "exports").rglob("manifest.json"))
    assert len(manifests) == 1
    report = json.loads(manifests[0].read_text())
    assert report["status"] == "failed" and not report["complete_for_selected_seeds"]
    assert not list((tmp_path / "exports").rglob("seeds.jsonl.zst"))
    assert source.cleared == ["synthetic-scroll"] and len(source.calls) == 2


def test_scope_corruption_refused_before_source_call(tmp_path):
    source = FakeSource()
    with pytest.raises(ValueError, match="grammar"):
        m.export(
            source, {**SCOPE, "tenant": "other"}, POLICY, config(tmp_path), adapter, tmp_path / "exports"
        )
    assert source.calls == []
