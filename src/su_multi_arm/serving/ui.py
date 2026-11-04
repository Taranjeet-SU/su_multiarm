"""Static mount of the web UI (addendum A2 §4.4; wheel ``su-multi-arm-ui``, package
``su_multi_arm_ui``).

``serving.ui.enabled: true`` mounts the bundle at ``serving.ui.mount`` (default ``/ui``) on the HTTP
app: same origin, same port, same ``PrincipalResolver`` as ``/v2/*``. The wheel missing (or an empty
bundle) is a ``ConfigError`` at start-up, never a silent server without UI.

* **Assets are public, data is not.** ``/ui/*`` is served without a credential (the SPA has to load
  before it can ask for the bearer token); every ``/v2/*`` call it makes goes through the resolver.
  The bundle contains no data, configuration values or secrets.
* ``/ui/ui-config.json`` is generated here: the auth kind (``none``/``bearer``/``custom``), the mount
  and the token-storage policy. Nothing else.
* **Headers** on every ``/ui`` response: a strict ``Content-Security-Policy`` (no inline script or
  style, no foreign origin, no framing), ``X-Content-Type-Options: nosniff``,
  ``Referrer-Policy: no-referrer``, ``Cross-Origin-Opener-Policy: same-origin``.
  ``index.html`` and ``ui-config.json`` are ``Cache-Control: no-store``; hashed ``assets/*`` are
  ``immutable``.
* Only files inside the bundle directory are served (resolved path containment); there is no SPA
  fallback route (the UI uses hash routing), so an unknown path is a 404.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Frame-Options": "DENY",
}
TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}


def bundle_dir() -> Path:
    """The installed bundle (``su_multi_arm_ui.STATIC_DIR``); ``ConfigError`` when absent or empty."""
    try:
        import su_multi_arm_ui  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ConfigError("serving.ui.enabled needs the su-multi-arm-ui wheel (pip install 'su-multi-arm[ui]')", path="serving.ui.enabled") from exc
    return check_bundle(Path(su_multi_arm_ui.STATIC_DIR))


def check_bundle(static_dir: Path) -> Path:
    static_dir = Path(static_dir).resolve()
    if not (static_dir / "index.html").is_file():
        raise ConfigError(f"UI bundle at {static_dir.name!r} has no index.html (build ui/ first)", path="serving.ui.enabled")
    return static_dir


def auth_kind(resolver: Any) -> str:
    from .auth import BearerTokenResolver, LocalResolver

    if isinstance(resolver, LocalResolver):
        return "none"
    if isinstance(resolver, BearerTokenResolver):
        return "bearer"
    return "custom"


def check_auth(ui_config, resolver: Any) -> None:
    """``ui.enabled`` needs bearer auth or an injected resolver, unless explicitly opted out (B10)."""
    if auth_kind(resolver) == "none" and not ui_config.allow_unauthenticated_loopback:
        raise ConfigError(
            "serving.ui.enabled requires serving.http.auth: bearer_env (or an injected principal resolver); "
            "set serving.ui.allow_unauthenticated_loopback: true only for single-user development",
            path="serving.ui.enabled",
        )


def mount_ui(app, ui_config, resolver: Any, static_dir: Path | None = None) -> Path:
    """Add the ``GET {mount}`` and ``GET {mount}/{path}`` routes. ``static_dir`` is a Python-side
    injection for tests; by default the installed wheel's bundle is used."""
    from fastapi.responses import RedirectResponse, Response

    check_auth(ui_config, resolver)
    root = check_bundle(static_dir) if static_dir is not None else bundle_dir()
    mount = ui_config.mount
    settings = json.dumps({"auth": auth_kind(resolver), "mount": mount, "session_storage_token": ui_config.session_storage_token, "display_timezone": ui_config.display_timezone}).encode()

    def respond(body: bytes, media_type: str, cache: str, status: int = 200) -> Response:
        return Response(content=body, status_code=status, media_type=media_type, headers={**SECURITY_HEADERS, "Cache-Control": cache})

    def not_found() -> Response:
        return respond(b"not found", "text/plain; charset=utf-8", "no-store", 404)

    @app.get(mount, include_in_schema=False)
    def ui_root():
        return RedirectResponse(f"{mount}/", status_code=307, headers=SECURITY_HEADERS)

    @app.get(mount + "/{path:path}", include_in_schema=False)
    def ui_file(path: str):
        if path in {"", "index.html"}:
            return respond((root / "index.html").read_bytes(), TYPES[".html"], "no-store")
        if path == "ui-config.json":
            return respond(settings, TYPES[".json"], "no-store")
        if "\\" in path or "\x00" in path:
            return not_found()
        candidate = (root / path).resolve()
        if root not in candidate.parents or not candidate.is_file():
            return not_found()
        suffix = candidate.suffix.lower()
        media = TYPES.get(suffix) or mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
        return respond(candidate.read_bytes(), media, cache)

    return root
