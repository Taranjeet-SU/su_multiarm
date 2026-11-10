"""Read-only self-check and capability probe (addendum A1 §3.1.1–3.1.2): ``_has_privileges`` with a
granted write privilege is a warning, or an error under ``require_read_only_role``; an unavailable
endpoint is ``unknown`` (warning); 403 on ``GET /`` is recorded and not fatal. Fake cluster only."""

from __future__ import annotations

import pytest

from su_multi_arm.core.component import Environment
from tests.sources_support import ENV, wired


def codes(diagnostics):
    return [(d.severity, d.code) for d in diagnostics]


def test_read_only_account_passes_with_an_info_diagnostic():
    source, cluster, _ = wired()
    assert codes(source.self_check()) == [("info", "source_read_only")]
    body = next(r.body for r in cluster.requests if r.path == "/_security/user/_has_privileges")
    assert body["cluster"] == ["all"]
    assert set(body["index"][0]["privileges"]) == {"write", "delete", "index", "manage"}
    assert body["index"][0]["names"] == ["*-*"]


@pytest.mark.parametrize("require, severity", [(False, "warning"), (True, "error")])
def test_granted_write_privilege_is_a_warning_or_an_error(require, severity):
    source, _, _ = wired(cluster_options={"privileges": {"write": True}}, require_read_only_role=require)
    [diagnostic] = source.self_check()
    assert (diagnostic.severity, diagnostic.code) == (severity, "source_write_privilege")
    assert "*-*:write" in diagnostic.message


def test_cluster_all_is_reported():
    source, _, _ = wired(cluster_options={"cluster_all": True}, require_read_only_role=True)
    [diagnostic] = source.self_check()
    assert diagnostic.severity == "error" and "cluster:all" in diagnostic.message


@pytest.mark.parametrize("status", [403, 404, 500])
def test_unavailable_privilege_endpoint_is_unknown_not_fatal(status):
    source, _, _ = wired(cluster_options={"privileges_status": status}, require_read_only_role=True)
    assert codes(source.self_check()) == [("warning", "source_privileges_unknown")]


def test_opensearch_uses_the_account_endpoint_and_reports_unknown():
    source, cluster, _ = wired(cluster_options={"distribution": "opensearch", "version": "2.11.0"})
    caps = source.capabilities()
    assert caps.server == "opensearch" and caps.pit and caps.resolve_index
    assert codes(source.self_check()) == [("warning", "source_privileges_unknown")]
    assert "/_plugins/_security/api/account" in [r.path for r in cluster.requests]


def test_forbidden_root_is_recorded_and_not_fatal():
    source, _, _ = wired(cluster_options={"forbid_root": True})
    caps = source.capabilities()
    assert caps.version is None and caps.server == "unknown"
    assert any("403" in note for note in caps.notes)
    assert source.discover(__import__("su_multi_arm.sources.probe", fromlist=["day_window"]).day_window("2026-01-05", "UTC"))


@pytest.mark.parametrize(
    "distribution, version, resolve, pit",
    [("elasticsearch", "7.8.1", False, False), ("elasticsearch", "7.9.3", True, False), ("elasticsearch", "7.17.0", True, True), ("opensearch", "1.3.0", True, False)],
)
def test_capabilities_follow_the_version_floor(distribution, version, resolve, pit):
    source, _, _ = wired(cluster_options={"distribution": distribution, "version": version})
    caps = source.capabilities()
    assert (caps.resolve_index, caps.pit, caps.probed) == (resolve, pit, False)


def test_old_releases_discover_through_the_alias_fallback():
    from su_multi_arm.sources.probe import day_window

    source, cluster, day = wired(cluster_options={"version": "7.8.1"})
    units = source.discover(day_window("2026-01-05", "UTC"))
    assert len(units) == len(day.indices)
    paths = [r.path for r in cluster.requests]
    assert not any(p.startswith("/_resolve") for p in paths) and "/*-2026.01.05/_alias" in paths


def test_resolve_rejection_falls_back_and_says_so():
    from su_multi_arm.sources.probe import day_window

    source, _, day = wired(cluster_options={"resolve_supported": False})
    units = source.discover(day_window("2026-01-05", "UTC"))
    assert len(units) == len(day.indices)
    assert any("_alias" in note for note in source.capabilities().notes)


def test_validate_reports_missing_environment_variables_without_network():
    source, cluster, _ = wired(auth="api_key", api_key_env="SU_TEST_API_KEY")
    diagnostics = source.validate(Environment(env=ENV))
    assert codes(diagnostics) == [("error", "missing_env")]
    assert diagnostics[0].path == "sources.synthetic.api_key_env"
    assert cluster.requests == []


def test_probe_validation_runs_the_self_check():
    source, cluster, _ = wired()
    diagnostics = source.validate(Environment(env=ENV), probe=True)
    assert codes(diagnostics) == [("info", "source_read_only")]
    assert source.capabilities().probed and source.capabilities().has_privileges
