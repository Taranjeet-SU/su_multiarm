"""Retrieval arms as components (plan §3.8). Built-ins register by explicit import here."""

from ..core.registry import Registry
from .base import ArmInput, ArmResult, BaseArm, IncompleteStageError
from .community import CommunityArm
from .dense import DenseArm
from .exact_id import ExactIdArm
from .graph import GraphArm
from .lexical import LexicalArm
from .structured import StructuredArm

BUILTIN_ARMS: tuple[type, ...] = (ExactIdArm, DenseArm, LexicalArm, StructuredArm, GraphArm, CommunityArm)


def builtin_registry() -> Registry:
    registry: Registry = Registry("arm")
    for cls in BUILTIN_ARMS:
        registry.register(cls)
    return registry


__all__ = [
    "ArmInput",
    "ArmResult",
    "BaseArm",
    "IncompleteStageError",
    "BUILTIN_ARMS",
    "builtin_registry",
    "DenseArm",
    "ExactIdArm",
    "LexicalArm",
    "StructuredArm",
    "GraphArm",
    "CommunityArm",
]
