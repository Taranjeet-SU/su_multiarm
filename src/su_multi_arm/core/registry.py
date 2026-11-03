"""Component registries (plan §3.2).

Built-ins are registered by explicit imports. Third-party components come only from the entry-point
group ``su_multi_arm.components`` and only for the names listed under ``plugins:`` in the config.
There is no import-by-string from YAML.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Generic, Iterable, TypeVar

from .component import COMPONENT_KINDS, COMPONENT_NAME, is_component_class
from .errors import PluginError, RegistryError

T = TypeVar("T")
ENTRY_POINT_GROUP = "su_multi_arm.components"


class Registry(Generic[T]):
    """Name -> component class for one component kind. Registration is explicit and checked."""

    def __init__(self, kind: str):
        if kind not in COMPONENT_KINDS:
            raise RegistryError(f"unknown component kind {kind!r}")
        self.kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, cls: type[T]) -> type[T]:
        if not is_component_class(cls):
            raise RegistryError(
                f"{getattr(cls, '__name__', cls)!r} does not satisfy the Component contract "
                "(name, kind, config_model, validate, run, recipe_fingerprint)"
            )
        name, kind = cls.name, cls.kind  # type: ignore[attr-defined]
        if kind != self.kind:
            raise RegistryError(f"component {name!r} has kind {kind!r}, registry accepts {self.kind!r}")
        existing = self._items.get(name)
        if existing is not None and existing is not cls:
            raise RegistryError(f"component name {name!r} is already registered for kind {self.kind!r}")
        self._items[name] = cls
        return cls

    def get(self, name: str) -> type[T]:
        try:
            return self._items[name]
        except KeyError:
            raise RegistryError(f"no {self.kind} component named {name!r}; known: {self.names()}") from None

    def names(self) -> list[str]:
        return sorted(self._items)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)


class Registries:
    """One registry per component kind plus plugin loading restricted to the configured names."""

    def __init__(self):
        self.by_kind: dict[str, Registry[Any]] = {kind: Registry(kind) for kind in COMPONENT_KINDS}
        self.plugins: dict[str, type] = {}

    def __getitem__(self, kind: str) -> Registry[Any]:
        try:
            return self.by_kind[kind]
        except KeyError:
            raise RegistryError(f"unknown component kind {kind!r}") from None

    def register(self, cls: type) -> type:
        if not is_component_class(cls):
            raise RegistryError(f"{getattr(cls, '__name__', cls)!r} does not satisfy the Component contract")
        return self.by_kind[cls.kind].register(cls)  # type: ignore[attr-defined]

    def load_plugins(self, names: Iterable[str], group: str = ENTRY_POINT_GROUP) -> list[type]:
        """Load exactly the named entry points. Unknown or non-conforming names are errors."""
        loaded = []
        for name in names:
            if not COMPONENT_NAME.fullmatch(name):
                raise PluginError(f"plugin name {name!r} must be an identifier, not a path or import string", "plugins")
            if name in self.plugins:
                loaded.append(self.plugins[name])
                continue
            matches = [ep for ep in available_plugins(group) if ep.name == name]
            if not matches:
                raise PluginError(f"plugin {name!r} is not registered in entry-point group {group!r}", "plugins")
            cls = matches[0].load()
            if not is_component_class(cls) or cls.name != name:
                raise PluginError(
                    f"plugin {name!r} must be a Component class whose name equals the entry point name", "plugins"
                )
            self.register(cls)
            self.plugins[name] = cls
            loaded.append(cls)
        return loaded


def available_plugins(group: str = ENTRY_POINT_GROUP) -> list[Any]:
    """Entry points in the component group. Reads metadata only; nothing is imported."""
    return list(entry_points(group=group))


def plugin_names(group: str = ENTRY_POINT_GROUP) -> set[str]:
    return {ep.name for ep in available_plugins(group)}


def check_plugins_registered(names: Iterable[str], group: str = ENTRY_POINT_GROUP) -> None:
    """Config-time check: every configured plugin name must exist as an entry point (no import)."""
    known = plugin_names(group)
    for name in names:
        if name not in known:
            raise PluginError(f"plugin {name!r} is not registered in entry-point group {group!r}", "plugins")
