"""Refuse to build the su-multi-arm-ui wheel without a built bundle (addendum A2 N11)."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        static = Path(self.root) / "su_multi_arm_ui" / "static"
        if not (static / "index.html").is_file() or not any((static / "assets").glob("*.js")):
            raise RuntimeError(f"UI bundle missing or empty at {static}: run `npm ci && npm run build` in ui/ first")
