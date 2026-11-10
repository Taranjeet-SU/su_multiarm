"""Synthetic citation, gap-denominator and provisional-community checks."""
from pathlib import Path
import runpy

import pytest

api = runpy.run_path(str(Path(__file__).resolve().parents[2]/"tools/log_extraction_audit.py"))


def finding(**changes):
    return {**dict.fromkeys(api["FIELDS"], ""), "kind": "entity", "type": "dependency", "value": "redis",
            "quote": "connection refused redis", "source": "message", **changes}


def record():
    return {"id": "synthetic-1", "message": "ERROR connection refused redis; latency=125ms", "scope": {"tenant": "synthetic", "service": "worker"},
            "regex_findings": [finding()], "regex_status": {"format_only": 1}}


def test_exact_grounding_duplicate_and_scope_counts_do_not_inflate_denominator():
    data = record()
    proposal = {"issue_family": "connection refusal", "findings": [finding(), finding(),
                finding(kind="metadata", type="latency_ms", value="125", quote="latency=125ms"),
                finding(kind="metadata", type="tenant", value="synthetic", source="scope", quote="")]}
    assessed = api["assess"](data, proposal)
    summary = api["report"]([assessed], [data], {})
    assert summary["totals"]["supported_message_proposals"] == 2
    assert summary["totals"]["supported_scope_dimensions"] == 1
    assert summary["exact_regex_recovery_percent"] == 50
    assert summary["totals"]["gap_candidates"] == 1


@pytest.mark.parametrize("change,reason", [
    ({"value": "kafka"}, "value_not_in_quote"),
    ({"quote": "invented source"}, "quote_not_in_message"),
    ({"source": "scope", "kind": "metadata", "type": "tenant", "value": "other"}, "unsupported_scope_value"),
    ({"type": ""}, "empty_finding_type"),
    ({"kind": "triplet", "subject": "worker", "predicate": "caused_by", "object": "redis"}, "no_explicit_causal_expression"),
])
def test_unsupported_proposals_are_rejected(change, reason):
    result = api["assess"](record(), {"issue_family": "unknown", "findings": [finding(**change)]})
    assert not result["valid"] and result["rejected"][0]["reason"] == reason


def test_failures_and_empty_model_output_never_become_perfect_coverage():
    data = record()
    empty = api["assess"](data, {"findings": [], "issue_family": ""})
    summary = api["report"]([empty, {"error": "timeout"}], [data, data], {})
    assert summary["exact_regex_recovery_percent"] is None
    assert summary["totals"]["failed_samples"] == 1
    assert summary["totals"]["completed_samples"] == 1


def test_shared_labels_are_only_provisional_candidates_with_source_ids():
    data = record()
    proposal = {"findings": [finding()], "issue_family": "Redis refusal"}
    one = api["assess"](data, proposal)
    other = {**data, "id": "synthetic-2", "scope": {"tenant": "other", "service": "worker"}}
    two = api["assess"](other, proposal)
    out = api["report"]([one, two], [data, other], {})
    assert out["community_candidates"][0]["tenant_count"] == 2
    assert out["community_candidates"][0]["status"] == "provisional_model_label_agreement"


def test_over_budget_input_is_rejected_before_any_model_request():
    data = {**record(), "message": "x"*10000}
    with pytest.raises(ValueError, match="token budget"):
        api["invoke"]("http://127.0.0.1:9", "synthetic-model", data)


@pytest.mark.parametrize("protocol", ["ollama", "openai"])
def test_four_worker_http_path_and_cache_reuse(tmp_path, protocol):
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json
    import os
    import subprocess
    import threading

    barrier = threading.Barrier(4)
    lock = threading.Lock()
    stats = [{"active": 0, "peak": 0, "requests": 0} for _ in range(2)]
    def handler(index):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    stats[index]["active"] += 1
                    stats[index]["requests"] += 1
                    stats[index]["peak"] = max(stats[index]["peak"], stats[index]["active"])
                barrier.wait(timeout=5)
                payload = {"done_reason": "stop", "eval_count": 20, "message": {"content": json.dumps({"findings": [finding()], "issue_family": "redis refusal"})}}
                data = json.dumps(payload).encode()
                if protocol == "ollama":
                    assert self.path == "/api/chat"
                    assert body["think"] is False
                else:
                    assert self.path == "/v1/chat/completions"
                    assert body["chat_template_kwargs"]["enable_thinking"] is False
                    assert body["response_format"]["type"] == "json_schema"
                    payload = {"choices": [{"message": payload["message"], "finish_reason": "stop"}],
                               "usage": {"completion_tokens": 20, "prompt_tokens": 10}}
                    data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
                with lock:
                    stats[index]["active"] -= 1
        return Handler
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler(i)) for i in range(2)]
    pool = ThreadPoolExecutor(max_workers=2)
    for server in servers:
        pool.submit(server.serve_forever)
    try:
        source = tmp_path/"synthetic.jsonl"
        source.write_text("".join(json.dumps({**record(), "id": f"synthetic-{i}"})+"\n" for i in range(4)))
        script = Path(__file__).resolve().parents[2]/"tools/log_extraction_audit.py"
        cmd = [__import__("sys").executable, "-I", str(script), "--input", str(source), "--output", str(tmp_path/"run"),
               "--case-cache", str(tmp_path/"shared"), "--model-digest", "synthetic-digest", "--limit", "4", "--protocol", protocol]
        for server in servers:
            cmd.extend(["--endpoint", f"http://127.0.0.1:{server.server_port}"])
        for _ in range(2):
            subprocess.run(cmd, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True, capture_output=True, timeout=20)
        result = json.loads((tmp_path/"run/report.json").read_text())
        assert [s["peak"] for s in stats] == [2, 2]
        assert [s["requests"] for s in stats] == [2, 2]
        assert result["totals"]["cached_samples"] == 4
        assert result["totals"]["failed_samples"] == 0
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        pool.shutdown(wait=True)
