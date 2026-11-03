"""What a client may ask for (``GET /v2/capabilities``, MCP ``capabilities``): the dataset and its
active generation, the selectable pipelines and modes, the declared metadata fields with their
filter/list/group/sort flags, the request context keys, enabled tools and remotely allowed actions.
Configuration names only; no paths, endpoints, prompts or secrets."""

from __future__ import annotations

from typing import Any

from ..pipeline.spec import selectable_pipelines


def describe(engine: Any) -> dict[str, Any]:
    config = engine.config
    pipelines = {}
    for name in selectable_pipelines(config):
        runtime = engine.pipelines.get(name)
        if runtime is None:
            continue
        modes = [m for m in runtime.decisions if m == runtime.default_mode or m in config.request.modes]
        pipelines[name] = {"arms": runtime.enabled_arms(), "default_mode": runtime.default_mode, "modes": modes, "review": runtime.review_enabled()}
    fields = [
        {
            "name": f.name,
            "type": f.type,
            "filterable": f.filterable,
            "listable": f.listable,
            "groupable": f.groupable,
            "sortable": f.sortable,
            **({"enum_values": list(f.enum_values)} if f.enum_values else {}),
        }
        for f in config.metadata_schema.fields
    ]
    generator = config.models.roles.generator
    return {
        "datasets": [{"name": config.dataset, "generation": engine._current_generation(), "profile": config.profile}],
        "default_pipeline": config.default_pipeline,
        "pipelines": pipelines,
        "fields": fields,
        "filterable_fields": [f["name"] for f in fields if f["filterable"]],
        "listable_fields": [f["name"] for f in fields if f["listable"]],
        "record_context_keys": list(config.request.context_keys),
        "tools": engine.tools.names(),
        "remote_actions": sorted(set(config.serving.actions.allow) & set(engine.actions.names())),
        "answer_streaming": {"tokens": bool(generator is not None and generator.capabilities.streaming)},
    }
