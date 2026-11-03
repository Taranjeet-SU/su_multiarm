"""v1 configuration entry point. The models live in ``compat/config_v1.py``; v2 is ``core/config.py``.

``su_multi_arm.config.Config`` remains the v1 model that the current engine consumes. The v2 model
(``su_multi_arm.core.config.Config``) accepts v1 files through ``compat.config_v1.upgrade``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .compat.config_v1 import (
    ArmsConfig,
    CommunityConfig,
    Config,
    EmbeddingConfig,
    ExtractionConfig,
    FusionConfig,
    GenerationConfig,
    GroundingConfig,
    InputMapping,
    ModelConfig,
    Neo4jConfig,
    PostgresConfig,
    PrivacyConfig,
    RoutingConfig,
)

__all__ = [
    "ArmsConfig",
    "CommunityConfig",
    "Config",
    "EmbeddingConfig",
    "ExtractionConfig",
    "FusionConfig",
    "GenerationConfig",
    "GroundingConfig",
    "InputMapping",
    "ModelConfig",
    "Neo4jConfig",
    "PostgresConfig",
    "PrivacyConfig",
    "RoutingConfig",
    "secret",
    "load_config",
]


def secret(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    config = Config.model_validate(yaml.safe_load(path.read_text()) or {})
    config.storage_path = (path.parent / config.storage_path.expanduser()).resolve()
    for stage in (config.routing, config.generation):
        if stage.prompt_file:
            stage.prompt_file = (path.parent / stage.prompt_file).resolve()
            if stage.prompt:
                raise ValueError("use either prompt or prompt_file, not both")
            stage.prompt = stage.prompt_file.read_text()
    return config
