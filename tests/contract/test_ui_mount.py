"""The ``/ui`` static mount and the browser-facing guards of the HTTP app (addendum A2 §4.4.2, B10,
N12, N20): strict CSP and security headers on the bundle, public assets but authenticated data,
the Host allow-list (421) and Origin check (403), ``nosniff`` on ``/v2/*``, a refused start without
the UI wheel or without auth, and ``serve`` resolving its address from ``serving.http.bind``.
Synthetic bundle and corpus; no model calls."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from su_multi_arm import Engine
from su_multi_arm.cli import build_parser, serve_address
from su_multi_arm.core.errors import ConfigError
from su_multi_arm.runtime.acl import Principal
from su_multi_arm.serving.auth import Unauthenticated
from su_multi_arm.serving.hosts import allowed_hosts, host_of
from su_multi_arm.serving.http import create_app
from su_multi_arm.serving.ui import CSP

from serving_support import DOCS, REQUEST, memory_config

TOKEN_ENV = "SU_TEST_UI_TOKEN"
LOCAL = "http://127.0.0.1:8099"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text('<!doctype html><html><head><script type="module" src="./assets/app-1a2b.js"></script></head><body><div id="root"></div></body></html>')
    (root / "assets" / "app-1a2b.js").write_text("console.log('synthetic bundle');")
    (root / "assets" / "app-1a2b.css").write_text("body{}")
    (tmp_path / "secret.txt").write_text("outside the bundle")
    return root


def engine_with(**serving):
    engine = Engine(memory_config(None, serving=serving))
    engine.ingest(DOCS)
    return engine


def bearer_app(monkeypatch, bundle, **ui):
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    serving = {"http": {"auth": "bearer_env", "token_env": TOKEN_ENV}, "ui": {"enabled": True, **ui}}
    return create_app(engine=engine_with(**serving), ui_static_dir=bundle)


def test_ui_assets_carry_csp_and_security_headers(monkeypatch, bundle):
    client = TestClient(bearer_app(monkeypatch, bundle), base_url=LOCAL)
    index = client.get("/ui/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert index.headers["content-security-policy"] == CSP
    for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert directive in CSP
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP
    assert index.headers["x-content-type-options"] == "nosniff"
    assert index.headers["referrer-policy"] == "no-referrer"
    assert index.headers["cache-control"] == "no-store"
    asset = client.get("/ui/assets/app-1a2b.js")
    assert asset.status_code == 200 and asset.headers["content-type"].startswith("text/javascript")
    assert "immutable" in asset.headers["cache-control"] and asset.headers["content-security-policy"] == CSP
    redirect = client.get("/ui", follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"] == "/ui/"


def test_ui_settings_name_the_auth_kind_and_nothing_secret(monkeypatch, bundle):
    client = TestClient(bearer_app(monkeypatch, bundle, session_storage_token=True), base_url=LOCAL)
    settings = client.get("/ui/ui-config.json")
    assert settings.status_code == 200 and settings.headers["cache-control"] == "no-store"
    assert settings.json() == {"auth": "bearer", "mount": "/ui", "session_storage_token": True, "display_timezone": None}
    assert "s3cret" not in settings.text and TOKEN_ENV not in settings.text


def test_assets_are_public_but_every_api_call_needs_the_token(monkeypatch, bundle):
    client = TestClient(bearer_app(monkeypatch, bundle), base_url=LOCAL)
    assert client.get("/ui/").status_code == 200  # the SPA must load before it can ask for the token
    for method, path, body in (("GET", "/v2/capabilities", None), ("POST", "/v2/search", REQUEST), ("POST", "/v2/answer/stream", REQUEST), ("GET", "/v2/tools", None)):
        assert client.request(method, path, json=body).status_code == 401, path
    auth = {"Authorization": "Bearer s3cret"}
    assert client.get("/v2/capabilities", headers=auth).status_code == 200
    assert client.post("/v2/search", json=REQUEST, headers=auth).status_code == 200


def test_only_files_inside_the_bundle_are_served(monkeypatch, bundle):
    client = TestClient(bearer_app(monkeypatch, bundle), base_url=LOCAL)
    for path in ("/ui/../secret.txt", "/ui/%2e%2e/secret.txt", "/ui/assets/../../secret.txt", "/ui/missing.js", "/ui/assets/", "/ui/..%5csecret.txt"):
        response = client.get(path)
        assert response.status_code == 404 or "outside the bundle" not in response.text, path
        assert "outside the bundle" not in response.text


def test_host_allow_list_refuses_a_rebinding_host_with_421(monkeypatch, bundle):
    client = TestClient(bearer_app(monkeypatch, bundle), base_url=LOCAL)
    auth = {"Authorization": "Bearer s3cret"}
    for path in ("/ui/", "/v2/capabilities", "/health"):
        assert client.get(path, headers={**auth, "Host": "attacker.example"}).status_code == 421, path
        assert client.get(path, headers={**auth, "Host": "attacker.example:8099"}).status_code == 421, path
    for host in ("127.0.0.1:8099", "localhost:8099", "[::1]:8099", "LOCALHOST"):
        assert client.get("/v2/capabilities", headers={**auth, "Host": host}).status_code == 200, host


def test_configured_allowed_hosts_and_bind_host_are_admitted(monkeypatch, bundle):
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    serving = {"http": {"auth": "bearer_env", "token_env": TOKEN_ENV, "bind": "10.1.2.3:8099"}, "ui": {"enabled": True}, "allowed_hosts": ["ui.internal.test"]}
    client = TestClient(create_app(engine=engine_with(**serving), ui_static_dir=bundle), base_url=LOCAL)
    for host in ("ui.internal.test", "10.1.2.3:8099"):
        assert client.get("/ui/", headers={"Host": host}).status_code == 200, host
    assert client.get("/ui/", headers={"Host": "other.internal.test"}).status_code == 421
    assert allowed_hosts("0.0.0.0:8099") == allowed_hosts("127.0.0.1:1")  # a wildcard bind adds no name
    assert host_of("[::1]:8099") == "[::1]" and host_of("Example.TEST:1") == "example.test"


def test_cross_origin_writes_and_streams_are_refused_with_403(monkeypatch, bundle):
    client = TestClient(bearer_app(monkeypatch, bundle), base_url=LOCAL)
    auth = {"Authorization": "Bearer s3cret"}
    foreign = {**auth, "Origin": "http://attacker.example"}
    assert client.post("/v2/search", json=REQUEST, headers=foreign).status_code == 403
    assert client.post("/v2/answer/stream", json=REQUEST, headers=foreign).status_code == 403
    assert client.post("/v2/search", json=REQUEST, headers={**auth, "Origin": "http://127.0.0.1:9999"}).status_code == 403  # other port
    assert client.post("/v2/search", json=REQUEST, headers={**auth, "Origin": "null"}).status_code == 403
    assert client.post("/v2/search", json=REQUEST, headers={**auth, "Origin": "http://127.0.0.1:8099"}).status_code == 200
    assert client.post("/v2/search", json=REQUEST, headers=auth).status_code == 200  # no Origin: non-browser client
    assert client.get("/v2/capabilities", headers=foreign).status_code == 200  # a plain GET is not origin-checked


def test_nosniff_on_every_v2_response_even_without_the_ui():
    client = TestClient(create_app(engine=engine_with()))
    assert client.post("/v2/search", json=REQUEST).headers["x-content-type-options"] == "nosniff"
    assert client.get("/v2/sessions/unknown").headers["x-content-type-options"] == "nosniff"  # errors too
    assert client.get("/ui/").status_code == 404  # not mounted unless enabled
    assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 200  # no host list without a browser surface


def test_ui_without_auth_is_refused_unless_explicitly_allowed(bundle):
    with pytest.raises(ConfigError, match="bearer_env"):
        create_app(engine=engine_with(ui={"enabled": True}), ui_static_dir=bundle)
    client = TestClient(create_app(engine=engine_with(ui={"enabled": True, "allow_unauthenticated_loopback": True}), ui_static_dir=bundle), base_url=LOCAL)
    assert client.get("/ui/ui-config.json").json()["auth"] == "none"
    assert client.get("/v2/capabilities").status_code == 200


class HeaderResolver:
    """An injected resolver (reverse-proxy style) satisfies the auth requirement."""

    def resolve(self, headers):
        if headers.get("x-test-user") != "operator":
            raise Unauthenticated("missing user")
        return Principal(id="operator", scopes=["search", "answer", "tools", "inspect"])


def test_an_injected_resolver_satisfies_the_ui_auth_rule(bundle):
    app = create_app(engine=engine_with(ui={"enabled": True}), principal_resolver=HeaderResolver(), ui_static_dir=bundle)
    client = TestClient(app, base_url=LOCAL)
    assert client.get("/ui/ui-config.json").json()["auth"] == "custom"
    assert client.get("/v2/capabilities").status_code == 401
    assert client.get("/v2/capabilities", headers={"x-test-user": "operator"}).status_code == 200


def test_missing_wheel_or_empty_bundle_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    serving = {"http": {"auth": "bearer_env", "token_env": TOKEN_ENV}, "ui": {"enabled": True}}
    monkeypatch.setitem(sys.modules, "su_multi_arm_ui", None)  # import fails: the wheel is absent
    with pytest.raises(ConfigError, match="su-multi-arm-ui"):
        create_app(engine=engine_with(**serving))
    (tmp_path / "empty").mkdir()
    with pytest.raises(ConfigError, match="index.html"):
        create_app(engine=engine_with(**serving), ui_static_dir=tmp_path / "empty")


@pytest.mark.parametrize("mount", ["/v2", "/mcp", "/health", "ui", "/UI", "/a/b"])
def test_ui_mount_must_not_collide(mount):
    with pytest.raises(ValueError):
        memory_config(None, serving={"ui": {"mount": mount}})


def test_allowed_hosts_are_exact_names():
    for bad in ("*", "*.example.test", "host:8080", "http://host", ""):
        with pytest.raises(ValueError):
            memory_config(None, serving={"allowed_hosts": [bad]})
    assert memory_config(None, serving={"allowed_hosts": ["UI.Example.Test", "[::1]"]}).serving.allowed_hosts == ["ui.example.test", "[::1]"]


def test_serve_resolves_its_address_from_the_configured_bind():
    """N20: ``serve`` without flags binds where ``serving.http.bind`` says; flags still win."""
    assert serve_address("10.0.0.5:9000", None, None) == ("10.0.0.5", 9000)
    assert serve_address("127.0.0.1:8099", "localhost", None) == ("localhost", 8099)
    assert serve_address("[::1]:8100", None, 8200) == ("::1", 8200)
    args = build_parser().parse_args(["--config", "c.yaml", "serve"])
    assert args.host is None and args.port is None


def test_built_bundle_has_no_inline_script_or_style():
    """When the real bundle has been built (``npm run build`` in ui/), its index.html must work under
    the CSP: no inline script, no inline style, only relative same-origin assets."""
    index = REPO / "ui" / "python" / "su_multi_arm_ui" / "static" / "index.html"
    if not index.is_file():
        pytest.skip("UI bundle not built (Node toolchain absent); covered by the ui CI job")
    import re

    html = index.read_text()
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S)
    assert scripts and all('src="./assets/' in attrs and not body.strip() for attrs, body in scripts)
    assert "<style" not in html and " style=" not in html and "http://" not in html and "https://" not in html


def test_core_distribution_excludes_the_ui():
    """N11: the core wheel packages only ``src/su_multi_arm`` and the core sdist excludes ``ui/``."""
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    targets = project["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["packages"] == ["src/su_multi_arm"]
    assert "/ui" in targets["sdist"]["exclude"]
    assert "su-multi-arm-ui==" + project["project"]["version"] in project["project"]["optional-dependencies"]["ui"]
    assert "su-multi-arm-ui" not in " ".join(project["project"]["optional-dependencies"]["all"])
