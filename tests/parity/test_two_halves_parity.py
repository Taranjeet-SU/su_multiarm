"""Serving parity of the unified collection's coverage (unified merge M2b-2, §6.11, M10): the per-source
coverage of an answer is the same through the Python API, the answer stream, HTTP ``/v2/answer`` and
HTTP SSE (``trace.source_coverage``, an additive key), and absent when the two-halves planner is off.
Synthetic corpus; the generator is the loopback ``FakeChatServer``."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.core.config import validate_config
from su_multi_arm.serving.http import create_app
from su_multi_arm.testing.provider_fakes import FakeChatServer

from serving_support import DOCS, REQUEST, config_data, normalise


def unified_data(state, generator, *, planner: bool):
    data = config_data({"kind": "sqlite", "path": str(state)}, generator)
    data["schema_version"] = 3
    if planner:
        data["crosssource"] = {"planner": {"enabled": True, "knowledge_sources": ["demo"]}}
    return data


def test_coverage_is_the_same_on_every_http_transport_and_absent_when_off(tmp_path):
    with FakeChatServer() as fake:
        engine = Engine(validate_config(unified_data(tmp_path / "on", fake.role(streaming=True), planner=True)))
        engine.ingest(DOCS)
        request = SearchRequest.model_validate(REQUEST)
        python = engine.answer(request).model_dump(mode="json")
        coverage = python["trace"]["source_coverage"]
        assert coverage["plan"]["halves"] == ["knowledge"] and coverage["sources"]["logs"]["status"] == "not_served"
        assert set(coverage) >= {"version", "plan", "sources", "identity", "links", "limitations"}
        client = TestClient(create_app(engine=engine))
        http = client.post("/v2/answer", json=REQUEST).json()
        sse = [block for block in client.post("/v2/answer/stream", json=REQUEST).text.split("\n\n") if "event: done" in block]
        streamed = json.loads(sse[0].split("data: ", 1)[1])["result"]
        for transport, result in (("http", http), ("http_sse", streamed), ("python_stream", engine.answer_stream(request).result().model_dump(mode="json"))):
            assert normalise(result["trace"]["source_coverage"]) == normalise(coverage), transport
            assert normalise(result["retrieval"]["trace"]["source_coverage"]) == normalise(python["retrieval"]["trace"]["source_coverage"]), transport

        off = Engine(validate_config(unified_data(tmp_path / "off", fake.role(streaming=True), planner=False)))
        off.ingest(DOCS)
        plain = off.answer(request).model_dump(mode="json")
        assert "source_coverage" not in plain["trace"] and "source_coverage" not in plain["retrieval"]["trace"]
        assert normalise(plain["answer"]) == normalise(python["answer"])
