"""Request-model parity (plan §3.15): HTTP, MCP and the Python API accept the same ``SearchRequest``.

The HTTP body **is** the model. The MCP tools expose its fields as flat arguments; ``tool_differences``
compares a tool's generated input schema field-for-field (names and value shapes, including the
typed ``filters`` values and ``exclude_ids``) with the model's JSON schema, and ``create_mcp`` refuses
to start when they drift. ``tests/parity`` asserts the same from the outside.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..models import SearchRequest

REQUEST_FIELDS: tuple[str, ...] = tuple(SearchRequest.model_fields)
_SHAPE_KEYS = ("type", "items", "additionalProperties", "properties", "enum", "const")


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return _resolve(defs[name], defs)
    return schema


def normalise_schema(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> Any:
    """Shape of a JSON schema node: optionality (``null``), titles, defaults and descriptions removed."""
    defs = defs or {}
    schema = _resolve(schema, defs)
    if "anyOf" in schema:
        options = [normalise_schema(o, defs) for o in schema["anyOf"] if o.get("type") != "null"]
        if len(options) == 1:
            return options[0]
        return {"anyOf": sorted(options, key=repr)}
    shape: dict[str, Any] = {}
    for key in _SHAPE_KEYS:
        if key not in schema:
            continue
        value = schema[key]
        if key in {"items", "additionalProperties"} and isinstance(value, dict):
            value = normalise_schema(value, defs)
        elif key == "properties":
            value = {name: normalise_schema(sub, defs) for name, sub in value.items()}
        shape[key] = value
    return shape


def tool_differences(tool_schema: dict[str, Any], model_class: type[BaseModel] = SearchRequest) -> list[str]:
    """Field-for-field differences between an MCP tool's input schema and the model it mirrors
    (``SearchRequest`` by default; the built-in tools' input models for the tool mirrors)."""
    model = model_class.model_json_schema()
    model_defs = model.get("$defs", {})
    tool_defs = tool_schema.get("$defs", {})
    expected = model["properties"]
    actual = tool_schema.get("properties", {})
    differences = [f"missing field {name!r}" for name in expected if name not in actual]
    differences += [f"unexpected field {name!r}" for name in actual if name not in expected]
    for name in expected:
        if name not in actual:
            continue
        want = normalise_schema(expected[name], model_defs)
        got = normalise_schema(actual[name], tool_defs)
        if want != got:
            differences.append(f"field {name!r}: tool accepts {got} but the request model declares {want}")
    required = set(tool_schema.get("required", []))
    if required != set(model.get("required", [])):
        differences.append(f"required fields differ: tool {sorted(required)} vs model {sorted(model.get('required', []))}")
    return differences


def request_from_arguments(**arguments: Any) -> SearchRequest:
    """Build the request from flat tool arguments; ``None`` means the model default."""
    return SearchRequest.model_validate({k: v for k, v in arguments.items() if v is not None})
