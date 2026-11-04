"""UI theme (``serving.ui.theme``, ``serving/ui_theme.py``): the neutral default and a deployment
theme become ``theme.css`` (custom properties only) and ``theme-logo.svg`` (an image with its own
``default-src 'none'`` policy); product name and tagline reach the SPA as text; colour, font and logo
values that could inject CSS, script or external references refuse the start. The committed
``ui/public/theme.css`` stays equal to the generated default. Synthetic theme and logo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from su_multi_arm import Engine  # noqa: E402
from su_multi_arm.core.errors import ConfigError  # noqa: E402
from su_multi_arm.serving.http import create_app  # noqa: E402
from su_multi_arm.serving.ui_theme import UiTheme, load_theme, theme_css  # noqa: E402

from serving_support import DOCS, memory_config  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TOKEN_ENV = "SU_TEST_UI_THEME_TOKEN"
LOGO = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="#123456"/></svg>'


def theme_file(tmp: Path, **overrides) -> Path:
    data = {"name": "acme", "product_name": "Acme Log Desk <b>", "tagline": "Synthetic tagline", "colors": {"brand": "#aa5500", "brand_soft": "#fff1e0"}, "logo_svg_file": "logo.svg", **overrides}
    (tmp / "logo.svg").write_bytes(LOGO)
    path = tmp / "theme.json"
    path.write_text(json.dumps(data))
    return path


def client_for(monkeypatch, tmp: Path, theme: Path | None):
    (tmp / "static").mkdir(exist_ok=True)
    (tmp / "static" / "index.html").write_text("<!doctype html><title>x</title>")
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    ui = {"enabled": True, **({"theme": str(theme)} if theme else {})}
    engine = Engine(memory_config(None, serving={"http": {"auth": "bearer_env", "token_env": TOKEN_ENV}, "ui": ui}))
    engine.ingest(DOCS)
    return TestClient(create_app(engine=engine, ui_static_dir=tmp / "static"), base_url="http://127.0.0.1:8099")


def test_committed_default_theme_css_matches_the_generated_default():
    assert (REPO / "ui" / "public" / "theme.css").read_bytes() == theme_css(UiTheme())


def test_default_theme_is_served_without_a_logo(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path, None)
    css = client.get("/ui/theme.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and "--brand: #0f6e66;" in css.text
    assert css.headers["content-security-policy"].startswith("default-src 'self'")
    assert client.get("/ui/theme-logo.svg").status_code == 404
    assert client.get("/ui/ui-config.json").json()["theme"] == {"name": "neutral", "product_name": "Multi-arm console", "tagline": "Grounded answers over logs and documents", "logo": False, "series": 6}


def test_deployment_theme_colours_name_and_logo(monkeypatch, tmp_path):
    client = client_for(monkeypatch, tmp_path, theme_file(tmp_path))
    css = client.get("/ui/theme.css").text
    assert "--brand: #aa5500;" in css and "--brand-soft: #fff1e0;" in css and "--ink: #1d2733;" in css
    assert all(line.strip().startswith(("--", ":root", "}", "/*")) or not line.strip() for line in css.splitlines())
    logo = client.get("/ui/theme-logo.svg")
    assert logo.status_code == 200 and logo.content == LOGO and logo.headers["content-type"] == "image/svg+xml"
    assert logo.headers["content-security-policy"] == "default-src 'none'; style-src 'unsafe-inline'" and logo.headers["x-content-type-options"] == "nosniff"
    theme = client.get("/ui/ui-config.json").json()["theme"]
    assert theme["product_name"] == "Acme Log Desk <b>" and theme["logo"] is True  # text; the SPA renders it as a text node


@pytest.mark.parametrize(
    "overrides",
    [
        {"colors": {"brand": "red; } body { display:none"}},
        {"colors": {"brand": "#12345"}},
        {"fonts": {"sans": "x; } @import url(http://attacker.example/a.css); {"}},
        {"fonts": {"mono": "url(http://attacker.example/f.woff)"}},
        {"series": ["#111111", "#222222", "blue"]},
        {"product_name": "line\u0007bell"},
        {"logo_svg_file": "../outside.svg"},
        {"unknown_key": 1},
    ],
)
def test_injection_shaped_theme_values_refuse_the_start(tmp_path, overrides):
    (tmp_path.parent / "outside.svg").write_bytes(LOGO)
    with pytest.raises(ConfigError):
        load_theme(theme_file(tmp_path, **overrides))


@pytest.mark.parametrize(
    "svg",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div>x</div></foreignObject></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="http://attacker.example/x.png"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><text>x</text></a></svg>',
        b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>',
        b"<html><body>not svg</body></html>",
        b"<svg",
    ],
)
def test_unsafe_logos_refuse_the_start(tmp_path, svg):
    path = theme_file(tmp_path)
    (tmp_path / "logo.svg").write_bytes(svg)
    with pytest.raises(ConfigError):
        load_theme(path)


def test_missing_theme_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="theme"):
        load_theme(tmp_path / "absent.json")
