# su-multi-arm-ui

Static assets of the su_multi_arm web UI. Install with `pip install "su-multi-arm[ui]"` and
enable `serving.ui.enabled: true`; `su-multi-arm serve` then serves the UI at `/ui` on the same
origin and behind the same authentication as the HTTP API. The wheel contains only static files
(HTML, JavaScript, CSS) and a `manifest.json` with their sha256.
