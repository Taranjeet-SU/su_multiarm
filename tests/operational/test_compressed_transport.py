"""Optional private transport; loopback synthetic HTTP only, no ELK/provider calls."""
from contextlib import contextmanager
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import signal
import threading
import time

import pytest


@pytest.fixture
def transport(tmp_path, monkeypatch):
    location = os.environ.get("SU_COMPRESSED_TRANSPORT_PATH")
    if not location:
        pytest.skip("private compressed transport not supplied")
    spec = importlib.util.spec_from_file_location("synthetic_compressed_transport", Path(location))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "_read_auth_from_stdin", lambda: "Basic synthetic-test-only")
    return module


@contextmanager
def server(response, *, encoding="identity", drip=False, status=200):
    captures = []
    payload = json.dumps(response).encode()
    if encoding == "gzip":
        payload = gzip.compress(payload)
    elif encoding == "broken":
        payload = b"not gzip"
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            decoded = gzip.decompress(body) if self.headers.get("Content-Encoding") == "gzip" else body
            captures.append({"body": decoded, "wire": body, "headers": dict(self.headers), "path": self.path})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip" if encoding == "broken" else encoding)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                if drip:
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.025)
                else:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_DELETE = do_POST

        def log_message(self, *args):
            pass
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield instance.server_address, captures, payload
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(2)


def bind(client, address):
    client._conn = http.client.HTTPConnection(*address, timeout=2)
    client._connect = lambda: client._conn


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
@pytest.mark.parametrize("compress_request", [False, True])
def test_compression_preserves_exact_request_and_response_json(transport, encoding, compress_request):
    body = {"size": 1000, "_source": {"includes": ["message"]},
            "query": {"match_phrase": {"message": "synthetic repeated error " * 100}}}
    response = {"hits": {"total": {"value": 1000, "relation": "eq"},
                         "hits": [{"_id": str(i), "_source": {"message": "synthetic repeated error " * 30}} for i in range(1000)]}}
    with server(response, encoding=encoding) as (address, captures, wire):
        client = transport.ReadOnlyES("parity", min_interval=0, request_gzip=compress_request, hard_deadline_s=2)
        bind(client, address)
        assert client.request("POST", "/synthetic-index/_search", body) == response
        assert captures[0]["body"] == json.dumps(body).encode()
        assert captures[0]["headers"]["Accept-Encoding"] == "gzip"
        assert client.stats["wire_bytes"] == len(wire)
        if encoding == "gzip":
            assert client.stats["wire_bytes"] < client.stats["bytes"] / 10
        client.close()
        log = json.loads((transport.ROOT / "logs/parity.es_requests.jsonl").read_text())
        assert log["decoded_bytes"] == len(json.dumps(response).encode())
        assert log["wire_bytes"] == len(wire) and log["wire_bytes_complete"]
        assert log["request_decoded_bytes"] == len(json.dumps(body).encode())
        assert "synthetic repeated error" not in json.dumps(log)
        assert "Authorization" not in json.dumps(log)


def test_dripping_response_cannot_extend_hard_wall_deadline(transport):
    response = {"message": "synthetic drip " * 50}
    with server(response, drip=True) as (address, _, _):
        client = transport.ReadOnlyES("deadline", min_interval=0, hard_deadline_s=.12)
        bind(client, address)
        previous = signal.getsignal(signal.SIGALRM)
        start = time.monotonic()
        with pytest.raises(transport.TimeoutCancelled):
            client.request("POST", "/synthetic-index/_search", {"size": 0})
        elapsed = time.monotonic() - start
        assert .09 <= elapsed < .8
        assert client._conn is None and client.stats["timeouts"] == 1
        assert signal.getsignal(signal.SIGALRM) == previous
        assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
        client.close()
        log = json.loads((transport.ROOT / "logs/deadline.es_requests.jsonl").read_text())
        assert log["timeout_kind"] == "hard_response_deadline"
        assert log["wire_bytes"] is None and not log["wire_bytes_complete"]


@pytest.mark.parametrize("encoding", ["broken", "br"])
def test_invalid_or_unsupported_response_encoding_fails_closed(transport, encoding):
    with server({"value": "synthetic"}, encoding=encoding) as (address, _, _):
        client = transport.ReadOnlyES("invalid", min_interval=0)
        bind(client, address)
        with pytest.raises(transport.ResponseEncodingError):
            client.request("POST", "/synthetic-index/_search", {"size": 0})
        assert client._conn is None
        client.close()


def test_existing_process_alarm_is_never_overwritten(transport, monkeypatch):
    monkeypatch.setattr(transport.signal, "getitimer", lambda which: (10, 0))
    with pytest.raises(RuntimeError, match="existing timer"):
        with transport.response_deadline(1):
            pytest.fail("deadline should not enter")


def test_readonly_guards_unchanged_before_compression_or_network(transport):
    client = transport.ReadOnlyES("guards", min_interval=0, request_gzip=True)
    client._connect = lambda: pytest.fail("source transport unexpectedly used")
    with pytest.raises(transport.Forbidden):
        client.request("DELETE", "/synthetic-index")
    with pytest.raises(transport.Forbidden):
        client.request("POST", "/synthetic-index/_search", {"size": 1, "_source": {"includes": ["body"]}})
    client.close()


@pytest.mark.parametrize("lease", ["0m", "31m", "2h", "60s", " 10m", "10m&size=0", "1.5m"])
def test_invalid_scroll_lease_refused_before_credentials_or_socket(transport, monkeypatch, lease):
    monkeypatch.setenv("ELK_SCROLL_KEEP_ALIVE", lease)
    monkeypatch.setattr(transport, "_read_auth_from_stdin", lambda: pytest.fail("credentials read before validation"))
    with pytest.raises(ValueError, match="KEEP_ALIVE"):
        transport.ReadOnlyES("invalid-lease")


def test_scroll_lease_only_changes_initial_parameter_and_scroll_lease(transport, monkeypatch):
    monkeypatch.setenv("ELK_SCROLL_KEEP_ALIVE", "10m")
    body = {"size": 0, "query": {"bool": {"filter": [{"term": {"synthetic": "literal"}}]}}}
    original = json.dumps(body)
    with server({"_scroll_id": "owned", "hits": {"hits": []}}) as (address, calls, _):
        client = transport.ReadOnlyES("lease", min_interval=0)
        bind(client, address)
        client.request("POST", "/synthetic-index/_search?scroll=2m&allow_partial_search_results=false", body)
        client._conn = http.client.HTTPConnection(*address, timeout=2)
        client.scroll("owned", "2m")
        assert calls[0]["path"] == "/synthetic-index/_search?scroll=10m&allow_partial_search_results=false"
        assert calls[0]["body"] == original.encode() and json.dumps(body) == original
        assert json.loads(calls[1]["body"]) == {"scroll": "10m", "scroll_id": "owned"}
        client._scrolls.clear()
        client.close()


def test_unset_lease_preserves_original_requests_exactly(transport, monkeypatch):
    monkeypatch.delenv("ELK_SCROLL_KEEP_ALIVE", raising=False)
    body = {"scroll": "2m", "scroll_id": "owned"}
    path = "/synthetic-index/_search?scroll=2m&allow_partial_search_results=false"
    assert transport.with_scroll_lease("POST", path, body, None) == (path, body)
    assert transport.with_scroll_lease("POST", "/synthetic-index/_search", {"size": 0}, "10m") == ("/synthetic-index/_search", {"size": 0})


def test_owned_expired_scroll_cleanup404_is_idempotent(transport):
    with server({"succeeded": True, "num_freed": 0}, status=404) as (address, calls, _):
        client = transport.ReadOnlyES("expired", min_interval=0)
        bind(client, address)
        client._scrolls.add("owned")
        client.clear_scroll("owned")
        assert "owned" not in client._scrolls
        client.clear_scroll("owned")
        assert len(calls) == 1
        client.close()


def test_unrelated_cleanup404_still_raises_without_primary_error(transport):
    with server({"error": {"type": "index_not_found_exception"}}, status=404) as (address, _, _):
        client = transport.ReadOnlyES("unrelated", min_interval=0)
        bind(client, address)
        client._scrolls.add("owned")
        with pytest.raises(transport.HTTPResponseError) as raised:
            client.clear_scroll("owned")
        assert not raised.value.expired_owned_scroll
        client.close()


def test_cleanup_failure_preserves_primary_scroll_error_with_diagnostic(transport):
    client = transport.ReadOnlyES("primary", min_interval=0)
    client._scrolls.add("owned")
    def cleanup(*args, **kwargs):
        raise transport.HTTPResponseError(500, "synthetic_cleanup_failure", "DELETE", "/_search/scroll")
    client.request = cleanup
    with pytest.raises(ValueError, match="primary fetch failure"):
        try:
            raise ValueError("primary fetch failure")
        finally:
            client.clear_scroll("owned")
    assert not client._scrolls
    client.close()
    log = json.loads((transport.ROOT / "logs/primary.es_requests.jsonl").read_text())
    assert log["event"] == "cleanup_failed_preserved_primary_error"
