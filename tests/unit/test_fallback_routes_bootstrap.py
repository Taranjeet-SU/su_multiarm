"""Provider route tiers and bootstrap (addendum A2 rev 4): the ``lan`` tier and ``egress.allow_lan``,
per-route UTC time windows, the invalid-reply limit, ``learning.bootstrap`` (off by default,
refused when unmeasured or when it can reach an uncapped paid route, self-hosted only, capped by
measured throughput) and ``patterns measure-throughput``. Scripted fake provider, synthetic data."""

from __future__ import annotations

import json

import pytest
import yaml

from su_multi_arm.core.errors import ConfigError
from su_multi_arm.learning.patterns.egress import EgressRefused
from tests.fallback_support import DAY, NOW, FakeProvider, config, heads_of, load, seal_day, session, write_library
from tests.logs_support import ENGINE
from tests.pattern_fixtures import engine_or_skip

KEY = DAY.isoformat()


@pytest.fixture(autouse=True)
def _engine():
    engine_or_skip(ENGINE)


def route(name, host, **extra):
    return {"name": name, "role": {"provider": "openai_compatible", "model": f"model-{name}", "base_url": f"http://{host}:9/v1", "max_tokens": 512}, **extra}


def setup(tmp_path, **kwargs):
    write_library(tmp_path / "patterns")
    cfg = config(tmp_path, **kwargs)
    return cfg, seal_day(cfg)


def run(cfg, store, fake=None, **kwargs):
    fake = fake or FakeProvider()
    return session(cfg, fake).run(store, [KEY], load(cfg), **kwargs), fake


# --- lan tier ----------------------------------------------------------------------------------


def test_a_lan_route_needs_allow_lan(tmp_path):
    write_library(tmp_path / "patterns")
    routes = {"pattern_learner": [route("lan_gpu", "10.0.0.5", tier="lan")]}
    with pytest.raises(ConfigError, match="allow_lan: true"):
        config(tmp_path, routes=routes)


def test_a_lan_route_is_self_hosted_and_takes_work_after_a_down_local_route(tmp_path):
    routes = {"pattern_learner": [route("local_a", "127.0.0.1"), route("lan_gpu", "gpu.lan.invalid", tier="lan")]}
    cfg, store = setup(tmp_path, routes=routes, patterns={"egress": {"allow_lan": True}})
    report, fake = run(cfg, store, FakeProvider(down={"127.0.0.1"}))
    hosts = {r["host"] for r in fake.requests if r["payload"]["task"] != "lines"}
    assert hosts == {"gpu.lan.invalid"}
    lan = next(r for r in report["calls"]["pattern_learner"]["routes"] if r["name"] == "lan_gpu")
    assert lan["tier"] == "lan" and not lan["paid"] and lan["calls"] > 0  # USD 0: no price table needed


def test_a_route_declared_local_must_be_loopback(tmp_path):
    write_library(tmp_path / "patterns")
    with pytest.raises(ConfigError, match="declared local"):
        config(tmp_path, routes={"pattern_learner": [route("fake_local", "10.0.0.5", tier="local")]})


def test_lan_egress_is_refused_at_run_time_without_allow_lan(tmp_path):
    # the config check is bypassed by building the session from a config that was valid, then
    # flipping the setting: the session itself refuses before any call
    routes = {"pattern_learner": [route("lan_gpu", "gpu.lan.invalid", tier="lan")]}
    cfg, _ = setup(tmp_path, routes=routes, patterns={"egress": {"allow_lan": True}})
    cfg.learning.patterns.egress.allow_lan = False
    with pytest.raises(EgressRefused, match="allow_lan is false"):
        session(cfg, FakeProvider())


# --- windows and invalid replies ---------------------------------------------------------------


def test_a_route_outside_its_window_is_skipped(tmp_path):
    # NOW is 06:00 UTC: the first route works 08:00-17:00 only
    routes = {"pattern_learner": [route("day_only", "127.0.0.1", windows=["08:00-17:00"]), route("night", "night.localhost", windows=["22:00-07:00"])]}
    cfg, store = setup(tmp_path, routes=routes)
    report, fake = run(cfg, store)
    assert {r["host"] for r in fake.requests if r["payload"]["task"] != "lines"} == {"night.localhost"}
    statuses = {r["name"]: r["status"] for r in report["calls"]["pattern_learner"]["routes"]}
    assert statuses == {"day_only": "outside_window", "night": "ready"}


def test_invalid_window_syntax_is_refused(tmp_path):
    write_library(tmp_path / "patterns")
    with pytest.raises(ConfigError, match="HH:MM-HH:MM"):
        config(tmp_path, routes={"pattern_learner": [route("a", "127.0.0.1", windows=["8-17"])]})


def test_a_route_is_marked_down_after_its_invalid_reply_limit(tmp_path):
    routes = {"pattern_learner": [route("flaky", "127.0.0.1", max_invalid_replies=1), route("steady", "b.localhost")]}
    cfg, store = setup(tmp_path, routes=routes)
    fake = FakeProvider()
    original = fake.__call__

    def broken(request):
        if request.method == "POST" and request.url.host == "127.0.0.1":
            import httpx

            body = json.loads(request.content)
            return httpx.Response(200, json={"model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "not json"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return original(request)

    import httpx

    sess = session(cfg, fake)
    for r in sess.cluster_routes:
        from su_multi_arm.providers.json_model import JsonModel

        r.model = JsonModel(r.role, httpx.MockTransport(broken))
    report = sess.run(store, [KEY], load(cfg))
    routes_report = {r["name"]: r for r in report["calls"]["pattern_learner"]["routes"]}
    assert routes_report["flaky"]["invalid_replies"] == 1 and routes_report["flaky"]["status"].startswith("down")
    assert routes_report["steady"]["calls"] >= 1  # later clusters went to the next route


# --- bootstrap ---------------------------------------------------------------------------------


def test_bootstrap_is_off_by_default(tmp_path):
    cfg, store = setup(tmp_path)
    assert cfg.learning.bootstrap.enabled is False and cfg.learning.bootstrap.require_measured_throughput is True
    report, _ = run(cfg, store)
    assert report["bootstrap"] == {"active": False}


def test_an_unmeasured_bootstrap_is_refused(tmp_path):
    cfg, store = setup(tmp_path)
    cfg.learning.bootstrap.enabled = True
    with pytest.raises(ConfigError, match="no measured throughput"):
        run(cfg, store)


def test_bootstrap_reaching_an_uncapped_paid_route_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    write_library(tmp_path / "patterns")
    prices = tmp_path / "prices.yaml"
    prices.write_text(yaml.safe_dump({"version": "test-1", "prices": {"gpt-4o-mini": {"input_per_million": 1.0, "output_per_million": 5.0}}}))
    paid = {"name": "paid", "role": {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 256}}
    data_patterns = {"egress": {"allow_hosted": True}, "max_usd_per_run": 0.5, "max_usd_per_day": 1.0}
    budgets = {"price_table": str(prices), "providers": {"openai": {"max_usd_total": 2.0}}}
    from tests.fallback_support import fallback_data
    from su_multi_arm.core.config import validate_config

    data = fallback_data(tmp_path, routes={"pattern_learner": [route("local_a", "127.0.0.1"), paid], "allow_paid_fallback": True}, patterns=data_patterns, budgets=budgets)
    data["learning"]["bootstrap"] = {"enabled": True}
    with pytest.raises(ConfigError, match="learning.bootstrap is enabled and a paid pattern_learner route exists"):
        validate_config(data)
    data["learning"]["bootstrap"]["max_usd_per_day"] = 0.5
    assert validate_config(data).learning.bootstrap.max_usd_per_day == 0.5


def test_measure_throughput_then_bootstrap_runs_self_hosted_within_the_measured_cap(tmp_path, monkeypatch):
    from su_multi_arm.cli import measure_throughput
    from su_multi_arm.learning.extraction import HttpProbe

    cfg, store = setup(tmp_path, extraction={"template": {"write_proposals": False}})
    fake = FakeProvider()
    result = measure_throughput(cfg, tier="local", clusters=50, day=KEY, transport=fake.transport(), probe=HttpProbe(fake.transport()))
    assert result["calls"] == 2 and result["calls_per_hour"] > 0 and result["recorded"]
    recorded = json.loads((tmp_path / "throughput.json").read_text())
    assert recorded["local"]["calls"] == 2
    assert not list((tmp_path / "patterns").glob("proposed.yaml"))  # measuring writes nothing to the library
    # bootstrap: the cap is min(max_clusters_per_day, measured); here max_clusters_per_day=1 binds
    cfg.learning.bootstrap.enabled = True
    cfg.learning.bootstrap.max_clusters_per_day = 1
    report, fake2 = run(cfg, store)
    assert report["bootstrap"]["active"] and report["bootstrap"]["max_clusters"] == 1 and report["bootstrap"]["day"] == 1
    # the measured replies were cached: both clusters are free (they take no slot) and need no call
    assert report["calls"]["pattern_learner"]["cache_hits"] == 2 and [r for r in fake2.requests if r["payload"]["task"] == "cluster"] == []
    assert "below_rank" not in [c["outcome"] for c in report["clusters"]]
    assert json.loads((tmp_path / ".bootstrap.json").read_text())["started"] == NOW.date().isoformat()


def test_the_bootstrap_cap_binds_clusters_that_need_a_call(tmp_path):
    cfg, store = setup(tmp_path, extraction={"template": {"write_proposals": False}})
    (tmp_path / "throughput.json").write_text(json.dumps({"local": {"calls_per_hour": 0.1}}))  # 0.8 calls/day: floor -> 1
    cfg.learning.bootstrap.enabled = True
    report, fake = run(cfg, store)
    assert report["bootstrap"]["max_clusters"] == 1 and "measured" in report["bootstrap"]["why"]
    assert [c["outcome"] for c in report["clusters"]].count("below_rank") == 1
    assert [r["payload"]["task"] for r in fake.requests].count("cluster") == 1


def test_bootstrap_skips_paid_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    prices = tmp_path / "prices.yaml"
    tmp_path.mkdir(exist_ok=True)
    prices.write_text(yaml.safe_dump({"version": "test-1", "prices": {"gpt-4o-mini": {"input_per_million": 1.0, "output_per_million": 5.0}}}))
    paid = {"name": "paid", "role": {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 256, "capabilities": {"structured_output": "native"}}}
    routes = {"pattern_learner": [route("local_a", "127.0.0.1"), paid], "allow_paid_fallback": True}
    cfg, store = setup(
        tmp_path,
        routes=routes,
        patterns={"egress": {"allow_hosted": True}, "max_usd_per_run": 0.5, "max_usd_per_day": 1.0},
        budgets={"price_table": str(prices), "providers": {"openai": {"max_usd_total": 2.0}}},
    )
    (tmp_path / "throughput.json").write_text(json.dumps({"local": {"calls_per_hour": 600.0}}))
    cfg.learning.bootstrap.enabled = True
    cfg.learning.bootstrap.max_usd_per_day = 0.5
    report, fake = run(cfg, store, FakeProvider(down={"127.0.0.1"}))
    assert not [r for r in fake.requests if r["host"] == "api.openai.com"]
    statuses = {r["name"]: r["status"] for r in report["calls"]["pattern_learner"]["routes"]}
    assert statuses["paid"] == "bootstrap_self_hosted_only"
    assert heads_of(store, report)[f"{KEY}-h000"].reason == "capacity_deferred"
