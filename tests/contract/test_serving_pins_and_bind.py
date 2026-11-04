"""Serving-path generation pins (B8) and the ``serve`` binding from ``serving.http.bind``.

* An ``Engine`` over a ``logs`` configuration pins the generation each request reads in the
  cross-process pin registry (``ProcessPins``), so a retention run in another process sees the pin
  and does not delete that generation mid-request; the pin is released when the request ends and
  the generation is no longer current.
* ``su-multi-arm serve`` binds to ``serving.http.bind`` (and its bearer token variable) unless the
  command line overrides them."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml

from su_multi_arm import Engine, SearchRequest
from su_multi_arm.ingestion.pins import SqlitePins
from tests.logs_support import ENGINE, Clock, cluster_for, pipeline
from tests.pattern_fixtures import engine_or_skip
from tests.unit.test_config_cli_a2 import run_main

DAY = date(2026, 1, 5)


def test_a_served_generation_is_pinned_for_the_request(tmp_path, monkeypatch):
    engine_or_skip(ENGINE)
    pipe, cfg, backend = pipeline(tmp_path, cluster_for([DAY], lines=8), now=Clock(DAY + timedelta(days=1)))
    generation = pipe.run_daily(DAY).steps["compose"]["generation"]
    engine = Engine(cfg)
    other_process = SqlitePins(backend.root / "pins.sqlite")  # what a retention run elsewhere reads
    assert other_process.pinned(generation) == []
    seen = []
    original = engine._retrieve

    def spy(*args, **kwargs):
        seen.append([t.holder for t in other_process.pinned(generation)])
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_retrieve", spy)
    engine.search(SearchRequest(query="connect failed"))
    assert seen == [[engine.process_pins.holder]]
    assert engine.process_pins.refcount[generation] == 0
    # still current: kept pinned between requests; once another generation is current, the idle pin goes
    monkeypatch.setattr(engine.process_pins, "current", lambda: "f" * 32)
    engine.process_pins.beat()
    assert other_process.pinned(generation) == []
    engine.process_pins.close()


def test_a_streaming_answer_holds_its_generation_against_retention(tmp_path):
    """A stream opened on generation 1 keeps it while a daily run in between composes generation 2
    and retention wants generation 1 gone: retention reports it pinned, the stream completes on
    generation 1, and the next retention run (pin released) deletes it."""
    engine_or_skip(ENGINE)
    span = [DAY, DAY + timedelta(days=1)]
    clock = Clock(DAY + timedelta(days=1))
    pipe, cfg, backend = pipeline(tmp_path, cluster_for(span, lines=8), now=clock, retention={"keep_days": 2, "keep_generations": 1, "max_pin_wait_seconds": 0})
    first = pipe.run_daily(DAY).steps["compose"]["generation"]
    engine = Engine(cfg)
    stream = engine.answer_stream(SearchRequest(query="connect failed"))
    clock.set_day(span[1] + timedelta(days=1))
    report = pipe.run_daily(span[1])  # composes generation 2; retention wants generation 1 gone
    assert report.steps["compose"]["generation"] != first
    assert {p["generation"]: p["status"] for p in report.steps["retention"]["pinned"]} == {first: "pinned"}
    assert first in backend.generation_registry().generations()
    events = list(stream)
    assert events[-1].event == "done" and events[-1].result.retrieval.generation == first
    engine.process_pins.beat()  # released after the stream: the idle, non-current pin goes
    from su_multi_arm.ingestion.retention import Retention

    again = Retention(cfg, pipe.store, pipe.write_path, backends={"local": backend}, now=pipe.now).run(pipe.today())
    assert first in [d["generation"] for d in again["deleted_generations"]]
    engine.process_pins.close()


def test_a_failed_request_releases_its_pin(tmp_path, monkeypatch):
    engine_or_skip(ENGINE)
    pipe, cfg, _ = pipeline(tmp_path, cluster_for([DAY], lines=8), now=Clock(DAY + timedelta(days=1)))
    generation = pipe.run_daily(DAY).steps["compose"]["generation"]
    engine = Engine(cfg)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(engine, "_retrieve", boom)
    with pytest.raises(RuntimeError):
        engine.search(SearchRequest(query="x"))
    assert engine.process_pins.refcount[generation] == 0
    engine.process_pins.close()


def test_an_engine_without_logs_has_no_pins(tmp_path):
    from su_multi_arm.core.config import validate_config

    cfg = validate_config({"schema_version": 2, "dataset": "d", "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "s")}}, "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")}, "pipeline": {"arms": {"lexical": {"backend": "local"}}}})
    assert Engine(cfg).process_pins is None


def serve_config(tmp_path, **http):
    data = {
        "schema_version": 2,
        "dataset": "d",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "s")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "serving": {"http": http},
    }
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def captured(monkeypatch):
    pytest.importorskip("fastapi")  # `serve` builds the HTTP app: the api extra
    uvicorn = pytest.importorskip("uvicorn")

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: calls.append((host, port)))
    import su_multi_arm.api as api

    tokens = []
    original = api.create_app
    monkeypatch.setattr(api, "create_app", lambda **kw: tokens.append(kw.get("token_env")) or original(**kw))
    return calls, tokens


def test_serve_binds_to_the_configured_address(tmp_path, monkeypatch, capsys, captured):
    calls, tokens = captured
    config = serve_config(tmp_path, bind="127.0.0.1:18321")
    code, _, err = run_main(monkeypatch, capsys, "--config", str(config), "serve")
    assert code == 0, err
    assert calls == [("127.0.0.1", 18321)]
    code, _, err = run_main(monkeypatch, capsys, "--config", str(config), "serve", "--port", "18322")
    assert calls[-1] == ("127.0.0.1", 18322)


def test_serve_uses_the_configured_token_off_localhost(tmp_path, monkeypatch, capsys, captured):
    calls, tokens = captured
    monkeypatch.setenv("SU_TEST_SERVE_TOKEN", "synthetic-token")
    config = serve_config(tmp_path, bind="0.0.0.0:18323", auth="bearer_env", token_env="SU_TEST_SERVE_TOKEN")
    code, _, err = run_main(monkeypatch, capsys, "--config", str(config), "serve")
    assert code == 0, err
    assert calls == [("0.0.0.0", 18323)] and tokens == ["SU_TEST_SERVE_TOKEN"]


def lan_engine(tmp_path, **serving):
    from su_multi_arm.core.config import validate_config

    data = {
        "schema_version": 2,
        "dataset": "d",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "s")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "serving": serving,
    }
    return Engine(validate_config(data))


def test_api_only_behind_a_lan_forwarder_is_host_guarded(tmp_path, monkeypatch):
    """No UI, API only, behind a LAN forwarder: a non-empty ``allowed_hosts`` turns the Host/Origin
    guard on (n-r3-4): the forwarder's name passes, any other Host is 421, a cross-origin write 403."""
    TestClient = pytest.importorskip("fastapi.testclient").TestClient  # the api extra

    from su_multi_arm.api import create_app

    monkeypatch.setenv("SU_TEST_LAN_TOKEN", "synthetic-token")
    engine = lan_engine(tmp_path, http={"bind": "0.0.0.0:18330", "auth": "bearer_env", "token_env": "SU_TEST_LAN_TOKEN"}, allowed_hosts=["logs.lan.test"])
    client = TestClient(create_app(engine=engine, token_env="SU_TEST_LAN_TOKEN", bind="0.0.0.0:18330"))
    auth = {"Authorization": "Bearer synthetic-token"}
    assert client.get("/health", headers={"Host": "logs.lan.test"}).status_code == 200
    assert client.get("/health", headers={"Host": "attacker.test"}).status_code == 421
    write = client.post("/v2/search", json={"query": "x"}, headers={**auth, "Host": "logs.lan.test", "Origin": "http://attacker.test"})
    assert write.status_code == 403


def test_a_loopback_api_without_a_host_list_stays_unguarded(tmp_path):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient  # the api extra

    from su_multi_arm.api import create_app
    from su_multi_arm.serving.hosts import guard_hosts

    client = TestClient(create_app(engine=lan_engine(tmp_path)))
    assert client.get("/health", headers={"Host": "anything.test"}).status_code == 200
    assert guard_hosts("127.0.0.1:8099") is None
    assert guard_hosts("127.0.0.1:8099", mcp_on_http=True) is not None and guard_hosts("0.0.0.0:1", ["a.test"]) is not None
