"""Serving the UI through a LAN forwarder (addendum A2 rev 3 §4.4.2): the forwarder's name in
``serving.allowed_hosts`` passes the Host allow-list on any port, and its ``Origin`` is accepted
even when the forwarder rewrites the Host header; any other origin is still refused. Synthetic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from su_multi_arm import Engine
from su_multi_arm.serving.http import create_app

from serving_support import DOCS, REQUEST, memory_config

TOKEN_ENV = "SU_TEST_UI_FWD_TOKEN"
FORWARDER = "logs-ui.lan.example"


@pytest.fixture
def client(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>")
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    serving = {"http": {"auth": "bearer_env", "token_env": TOKEN_ENV, "bind": "0.0.0.0:8187"}, "ui": {"enabled": True}, "allowed_hosts": [FORWARDER]}
    engine = Engine(memory_config(None, serving=serving))
    engine.ingest(DOCS)
    return TestClient(create_app(engine=engine, ui_static_dir=tmp_path), base_url="http://127.0.0.1:8187")


def test_forwarder_host_and_origin_are_accepted(client):
    auth = {"Authorization": "Bearer s3cret"}
    for host in (FORWARDER, f"{FORWARDER}:8187", f"{FORWARDER}:443"):
        assert client.get("/ui/", headers={"Host": host}).status_code == 200, host
    # the forwarder rewrote Host to the backend address; the browser's Origin is the forwarder's
    for origin in (f"http://{FORWARDER}:8187", f"https://{FORWARDER}"):
        response = client.post("/v2/search", json=REQUEST, headers={**auth, "Host": "127.0.0.1:8187", "Origin": origin})
        assert response.status_code == 200, origin


def test_other_origins_and_hosts_are_still_refused(client):
    auth = {"Authorization": "Bearer s3cret"}
    assert client.get("/ui/", headers={"Host": "other.lan.example"}).status_code == 421
    for origin in ("http://attacker.example", f"http://{FORWARDER}.attacker.example", "http://localhost:9999", "ftp://" + FORWARDER, "null"):
        response = client.post("/v2/answer/stream", json=REQUEST, headers={**auth, "Origin": origin})
        assert response.status_code == 403, origin
