"""Profiles: optional YAML overlays applied before configuration validation (plan §3.3, notes §4).

``neutral`` is the default and ships no domain behaviour: no context keys, no field priority, no
extraction labels, neutral prompts. ``support_example`` carries the support-domain conventions of
the alpha (prompts, field order, context keys, extraction labels) as an example overlay. A profile
can set any configuration key except the ones that identify a deployment (``dataset``, ``backends``,
``storage``, ``plugins``); explicit configuration values always win over the overlay.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROFILE_DIR = Path(__file__).parent
RESERVED_KEYS = frozenset({"schema_version", "dataset", "profile", "plugins", "backends", "storage"})


@lru_cache(maxsize=1)
def available_profiles() -> frozenset[str]:
    return frozenset(p.stem for p in PROFILE_DIR.glob("*.yaml"))


def load_profile(name: str) -> dict[str, Any]:
    if name not in available_profiles():
        raise ValueError(f"profile {name!r} is not available; built-in profiles: {sorted(available_profiles())}")
    data = yaml.safe_load((PROFILE_DIR / f"{name}.yaml").read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"profile {name!r} must be a mapping")
    overlay = data.get("overlay") or {}
    if set(overlay) & RESERVED_KEYS:
        raise ValueError(f"profile {name!r} may not set {sorted(set(overlay) & RESERVED_KEYS)}")
    return overlay


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def apply_profile(data: dict[str, Any]) -> dict[str, Any]:
    """Overlay the named profile under the raw config mapping (config values win). Idempotent."""
    name = data.get("profile", "neutral")
    if not isinstance(name, str) or name not in available_profiles():
        return data  # validation reports the unknown profile with its key path
    overlay = load_profile(name)
    return deep_merge(overlay, data) if overlay else data
