"""UI theme (brand colours, product name, logo, fonts) for the web UI (``serving.ui.theme``).

A theme is a JSON file named by ``serving.ui.theme``; without one the neutral default below is
served. The server validates it and turns it into two same-origin resources, so the strict CSP
(no inline style or script) holds:

* ``{mount}/theme.css``: CSS custom properties only (``:root { --brand: #...; ... }``). Colours must
  be ``#rrggbb``; font stacks are restricted to family names, quotes, commas and hyphens, so a theme
  can never inject a rule, a URL or an ``@import``. Fonts must be installed locally: the CSP allows no
  remote font.
* ``{mount}/theme-logo.svg``: the logo, from ``logo_svg_file`` (relative to the theme file), at most
  200 kB, an ``<svg>`` root with no script, ``foreignObject``, event attribute, external reference or
  ``javascript:`` URL. The UI shows it through ``<img>`` (an SVG image never runs script), and the
  response carries its own ``default-src 'none'`` policy.

Product name and tagline are plain text (the UI renders them as text nodes). Branding is deployment
data: keep customer names and logos in the deployment's theme file, outside this repository.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

from pydantic import Field, ValidationError, field_validator

from ..core.errors import ConfigError
from ..models import StrictModel

HEX = r"^#[0-9a-fA-F]{6}$"
FONT_STACK = re.compile(r"^[A-Za-z0-9 ,'\"\-]{1,200}$")
MAX_LOGO_BYTES = 200_000
_BAD_SVG = re.compile(r"<\s*script|foreignObject|\bon[a-z]+\s*=|javascript:|data:text/html|xlink:href\s*=\s*[\"'](?!#)|<!ENTITY|<!DOCTYPE", re.I)


class ThemeColors(StrictModel):
    brand: str = Field(default="#0f6e66", pattern=HEX)  # primary actions, active navigation, focus
    brand_ink: str = Field(default="#ffffff", pattern=HEX)  # text on brand
    brand_soft: str = Field(default="#e3f0ee", pattern=HEX)  # selected chips, active tab
    brand_tint: str = Field(default="#f2f8f7", pattern=HEX)  # highlighted rows, hero wash
    ink: str = Field(default="#1d2733", pattern=HEX)
    muted: str = Field(default="#5b6573", pattern=HEX)
    surface: str = Field(default="#f5f6f8", pattern=HEX)
    card: str = Field(default="#ffffff", pattern=HEX)
    border: str = Field(default="#dde2e9", pattern=HEX)


class ThemeFonts(StrictModel):
    sans: str = "\"Inter\", \"Segoe UI\", system-ui, -apple-system, sans-serif"
    mono: str = "\"JetBrains Mono\", ui-monospace, \"SFMono-Regular\", Menlo, monospace"

    @field_validator("sans", "mono")
    @classmethod
    def stack(cls, value: str) -> str:
        if not FONT_STACK.fullmatch(value):
            raise ValueError("a font stack may hold family names, quotes, commas, spaces and hyphens only")
        return value


class UiTheme(StrictModel):
    name: str = Field(default="neutral", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    product_name: str = Field(default="Multi-arm console", min_length=1, max_length=60)
    tagline: str = Field(default="Grounded answers over logs and documents", max_length=120)
    colors: ThemeColors = Field(default_factory=ThemeColors)
    fonts: ThemeFonts = Field(default_factory=ThemeFonts)
    # Chart series colours (categorical, unordered groups). Validate a custom list with a palette
    # checker for contrast on the card colour; the default passes at 2.5:1 on white.
    series: list[str] = Field(default_factory=lambda: ["#1d4ed8", "#b45309", "#166534", "#7c3aed", "#0891b2", "#be123c"], min_length=3, max_length=8)
    logo_svg_file: str | None = Field(default=None, max_length=255)

    @field_validator("series")
    @classmethod
    def hexes(cls, value: list[str]) -> list[str]:
        for colour in value:
            if not re.fullmatch(HEX, colour):
                raise ValueError(f"series colour {colour!r} must be #rrggbb")
        return value

    @field_validator("product_name", "tagline")
    @classmethod
    def plain(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value):
            raise ValueError("control characters are not allowed")
        return value


def load_theme(path: Path | None) -> tuple[UiTheme, bytes | None]:
    """The validated theme and its logo bytes; ``ConfigError`` naming the problem otherwise."""
    if path is None:
        return UiTheme(), None
    path = Path(path).expanduser()
    try:
        theme = UiTheme.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise ConfigError(f"cannot read the UI theme file: {exc.strerror}", path="serving.ui.theme") from exc
    except (ValueError, ValidationError) as exc:
        raise ConfigError(f"invalid UI theme: {exc}", path="serving.ui.theme") from exc
    logo = None
    if theme.logo_svg_file:
        logo_path = (path.parent / theme.logo_svg_file).resolve()
        if path.parent.resolve() not in logo_path.parents:
            raise ConfigError("logo_svg_file must stay inside the theme's directory", path="serving.ui.theme")
        try:
            logo = logo_path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"cannot read logo_svg_file: {exc.strerror}", path="serving.ui.theme") from exc
        check_logo(logo)
    return theme, logo


def check_logo(data: bytes) -> None:
    if len(data) > MAX_LOGO_BYTES:
        raise ConfigError(f"logo_svg_file is larger than {MAX_LOGO_BYTES} bytes", path="serving.ui.theme")
    text = data.decode("utf-8", errors="strict") if data else ""
    if _BAD_SVG.search(text):
        raise ConfigError("logo_svg_file contains script, event handlers, foreignObject, entities or external references", path="serving.ui.theme")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ConfigError(f"logo_svg_file is not well-formed SVG: {exc}", path="serving.ui.theme") from exc
    if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        raise ConfigError("logo_svg_file must have an <svg> root element", path="serving.ui.theme")


def theme_css(theme: UiTheme) -> bytes:
    c = theme.colors
    lines = [
        f"/* su_multi_arm UI theme: {theme.name} (generated; custom properties only) */",
        ":root {",
        *(f"  --{name.replace('_', '-')}: {value};" for name, value in c.model_dump().items()),
        f"  --font-sans: {theme.fonts.sans};",
        f"  --font-mono: {theme.fonts.mono};",
        *(f"  --series-{i + 1}: {colour};" for i, colour in enumerate(theme.series)),
        "}",
        "",
    ]
    return "\n".join(lines).encode()


def theme_public(theme: UiTheme, has_logo: bool) -> dict:
    """The theme fields the SPA reads from ``ui-config.json`` (text only)."""
    return {"name": theme.name, "product_name": theme.product_name, "tagline": theme.tagline, "logo": has_logo, "series": len(theme.series)}
