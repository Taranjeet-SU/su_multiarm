"""JSON schema generation and drift check for the configuration models.

``docs/config.schema.json`` is the v2 schema; ``docs/config_v1.schema.json`` is the v1 schema kept
for the compatibility window. CI regenerates both and fails on drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import StrictModel

V2_SCHEMA = Path("docs/config.schema.json")
V1_SCHEMA = Path("docs/config_v1.schema.json")


def generate(model: type[StrictModel] | None = None) -> dict[str, Any]:
    if model is None:
        from .config import Config

        model = Config
    return model.model_json_schema()


def render(model: type[StrictModel] | None = None) -> str:
    return json.dumps(generate(model), indent=2) + "\n"


def write(path: str | Path, model: type[StrictModel] | None = None) -> Path:
    target = Path(path)
    target.write_text(render(model))
    return target


def drift(path: str | Path, model: type[StrictModel] | None = None) -> bool:
    """True when the file differs from the generated schema (or is missing)."""
    target = Path(path)
    return not target.exists() or target.read_text() != render(model)


def write_all(docs: str | Path = "docs") -> list[Path]:
    from ..compat.config_v1 import Config as ConfigV1
    from .config import Config

    docs = Path(docs)
    return [write(docs / V2_SCHEMA.name, Config), write(docs / V1_SCHEMA.name, ConfigV1)]
