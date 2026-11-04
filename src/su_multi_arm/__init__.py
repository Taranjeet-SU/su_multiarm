"""Configuration-first multi-arm retrieval. No network activity at import time.

``Config``/``load_config`` are configuration v2 (``core.config``); v1 files load through the
upgrade path. ``Document`` is the v2 canonical document; v1 documents are accepted by the engine.
"""

from .core.config import Config, load_config
from .core.types import Document
from .engine import Engine
from .models import SearchRequest

__all__ = ["Config", "load_config", "Document", "SearchRequest", "Engine"]
__version__ = "2.0.0a3"
