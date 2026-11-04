"""Wait for the throwaway real-service stack (tests/services/docker-compose.yml) to accept requests.

Standard library only, so it runs before the project or any driver is installed. Reads the same
``SU_TEST_*_PORT`` variables as the compose file (same non-default defaults) and probes each
service at the protocol level: HTTP cluster health for OpenSearch/Elasticsearch, a PostgreSQL
startup message (the ``pg_isready`` trick), the Bolt version handshake for Neo4j and a MongoDB
``ping`` over OP_MSG. Prints a readiness table and exits non-zero if any service is not ready
within ``--timeout`` seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

HOST = os.environ.get("SU_TEST_SERVICES_HOST", "127.0.0.1")


def _port(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _http_health(url: str) -> str:
    with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - loopback test endpoint
        body = json.loads(response.read().decode("utf-8"))
    status = body.get("status")
    if status not in {"yellow", "green"}:
        raise RuntimeError(f"cluster status {status!r}")
    return f"cluster {status}"


def check_opensearch() -> str:
    port = _port("SU_TEST_OPENSEARCH_PORT", 19200)
    return _http_health(f"http://{HOST}:{port}/_cluster/health?wait_for_status=yellow&timeout=1s")


def check_opensearch3() -> str:
    port = _port("SU_TEST_OPENSEARCH3_PORT", 19202)
    return _http_health(f"http://{HOST}:{port}/_cluster/health?wait_for_status=yellow&timeout=1s")


def check_elasticsearch() -> str:
    port = _port("SU_TEST_ES_PORT", 19201)
    return _http_health(f"http://{HOST}:{port}/_cluster/health?wait_for_status=yellow&timeout=1s")


def check_postgres() -> str:
    port = _port("SU_TEST_POSTGRES_PORT", 15432)
    payload = b"user\0su_test\0database\0su_test\0\0"
    message = struct.pack("!II", 8 + len(payload), 196608) + payload
    with socket.create_connection((HOST, port), timeout=3) as sock:
        sock.sendall(message)
        first = sock.recv(1)
    # 'R' = authentication request, 'E' = error response (e.g. startup still in progress).
    if first != b"R":
        raise RuntimeError(f"unexpected first byte {first!r}")
    return "accepting connections"


def check_neo4j() -> str:
    port = _port("SU_TEST_NEO4J_BOLT_PORT", 17687)
    # Bolt handshake: magic preamble + four proposed protocol versions (5.x and 4.4).
    handshake = b"\x60\x60\xb0\x17" + bytes([0, 0, 4, 5, 0, 0, 0, 5, 0, 0, 4, 4, 0, 0, 0, 0])
    with socket.create_connection((HOST, port), timeout=3) as sock:
        sock.sendall(handshake)
        chosen = sock.recv(4)
    if len(chosen) != 4 or chosen == b"\0\0\0\0":
        raise RuntimeError(f"no bolt version agreed: {chosen!r}")
    return f"bolt {chosen[3]}.{chosen[2]}"


def check_mongo() -> str:
    port = _port("SU_TEST_MONGO_PORT", 27018)
    # OP_MSG {ping: 1, $db: "admin"} (BSON int32 + string); ping needs no authentication.
    doc = b"\x10ping\x00" + struct.pack("<i", 1) + b"\x02$db\x00" + struct.pack("<i", 6) + b"admin\x00"
    body = struct.pack("<i", len(doc) + 5) + doc + b"\x00"
    section = b"\x00" + body
    flags = struct.pack("<I", 0)
    length = 16 + len(flags) + len(section)
    header = struct.pack("<iiii", length, 1, 0, 2013)
    with socket.create_connection((HOST, port), timeout=3) as sock:
        sock.sendall(header + flags + section)
        reply = sock.recv(4096)
    if len(reply) < 16 or struct.unpack("<i", reply[12:16])[0] != 2013:
        raise RuntimeError("no OP_MSG reply")
    if b"\x01ok\x00" + struct.pack("<d", 1.0) not in reply:
        raise RuntimeError("ping not ok")
    return "ping ok"


CHECKS = {
    "opensearch": check_opensearch,
    "opensearch3": check_opensearch3,
    "elasticsearch": check_elasticsearch,
    "postgres": check_postgres,
    "neo4j": check_neo4j,
    "mongo": check_mongo,
}
# Services behind a compose profile: waited for only when named with --only.
PROFILE_ONLY = {"opensearch3"}


def endpoint(name: str) -> str:
    ports = {
        "opensearch": _port("SU_TEST_OPENSEARCH_PORT", 19200),
        "opensearch3": _port("SU_TEST_OPENSEARCH3_PORT", 19202),
        "elasticsearch": _port("SU_TEST_ES_PORT", 19201),
        "postgres": _port("SU_TEST_POSTGRES_PORT", 15432),
        "neo4j": _port("SU_TEST_NEO4J_BOLT_PORT", 17687),
        "mongo": _port("SU_TEST_MONGO_PORT", 27018),
    }
    return f"{HOST}:{ports[name]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait per run (default 300)")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between probes (default 2)")
    parser.add_argument("--only", nargs="*", choices=sorted(CHECKS), help="subset of services to wait for (opensearch3 only when named)")
    args = parser.parse_args(argv)

    pending = {k: v for k, v in CHECKS.items() if k not in PROFILE_ONLY} if not args.only else {k: CHECKS[k] for k in args.only}
    results: dict[str, tuple[str, str, float]] = {}
    started = time.monotonic()
    last_error: dict[str, str] = {}
    while pending and time.monotonic() - started < args.timeout:
        for name, check in list(pending.items()):
            try:
                detail = check()
            except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
                last_error[name] = f"{type(exc).__name__}: {exc}"
                continue
            results[name] = ("ready", detail, time.monotonic() - started)
            del pending[name]
        if pending:
            time.sleep(args.interval)
    for name in pending:
        results[name] = ("NOT READY", last_error.get(name, "no attempt"), time.monotonic() - started)

    rows = [(n, endpoint(n), s, f"{t:6.1f}s", d) for n, (s, d, t) in sorted(results.items())]
    widths = [max(len(r[i]) for r in rows + [("service", "endpoint", "status", "after", "detail")]) for i in range(5)]
    header = ("service", "endpoint", "status", "after", "detail")
    print("  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
