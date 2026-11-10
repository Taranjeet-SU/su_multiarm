"""Shared helpers for the source tests: a synthetic day on the fake cluster and a source wired to it
through ``httpx.MockTransport``. No network."""

from __future__ import annotations

from datetime import date
from typing import Any

from su_multi_arm.core.config_sources import SourceConfig
from su_multi_arm.sources import build_source
from su_multi_arm.sources.transport import RateLimiter
from su_multi_arm.testing.fixtures.logs import SERVICES, SyntheticLogSpec, fake_cluster_for

DAY = date(2026, 1, 5)
URL_ENV = "SU_TEST_SOURCE_URL"
ENV = {URL_ENV: "http://source.invalid:9200"}


def source_config(**overrides: Any) -> SourceConfig:
    data: dict[str, Any] = {
        "kind": "elasticsearch_rest",
        "url_env": URL_ENV,
        "indices": ["*-{date}"],
        "mapping": {"level": "level", "host": "host.name", "container": "kubernetes.container.name", "file": "log.file.path", "offset": "log.offset"},
        "index_name": {"vocabularies": {"service": list(SERVICES)}},
        "counts": {"errors": {"levels": ["ERROR"], "tokens": ["error"]}},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return SourceConfig.model_validate(data)


def no_wait() -> RateLimiter:
    return RateLimiter(1e9, clock=lambda: 0.0, sleep=lambda _s: None)


def wired(lines: int = 120, cluster_options: dict | None = None, env: dict | None = None, **config):
    """``(source, cluster, synthetic_day)``."""
    cluster, day = fake_cluster_for(SyntheticLogSpec(day=DAY, lines_per_index=lines), **(cluster_options or {}))
    cfg = source_config(**config)
    source = build_source("synthetic", cfg, env=env or ENV, http_transport=cluster.transport())
    source._rate_limiter = no_wait()
    return source, cluster, day
