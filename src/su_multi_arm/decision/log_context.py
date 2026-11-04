"""The logs planner context pack (owner request, real-run fix; ``logs.intents.planner_context``).

Under the logs profile the ``llm`` planner (alone, or as the stage the cascade reaches when the
rules find no intent) sees, next to the question and the usual payload, a ``log_context`` object:

* ``arms``: what each enabled arm stores (dense and lexical: signature and incident documents;
  structured: the rollup kinds and their fields; graph: node types and edge kinds; communities:
  co-firing incidents);
* ``pattern_library``: a capped summary of the approved message entries (signature id, issue
  name, category, services, field names);
* ``data_dates``: the served days of the active generation (window, every composed day, the
  missing ones, history shards);
* ``vocabularies``: known values of ``service``, ``tenant``, ``env`` and ``level`` (capped);
* ``intents``: the ten log intents as typed tools (description and input JSON schema);
* ``few_shot``: question -> intent call examples (the profile's synthetic set, or the deployment's
  ``few_shot_file``).

The pack holds identifiers and aggregates only (no log-line text, no configuration secrets) and is
bounded by ``planner_context_max_chars``: lists are cut in a fixed order and the cuts are named in
``truncated``. The planner still answers with a ``StructuredStep`` intent (``log_intent`` +
``log_args``), compiled by ``log_intents.compile_intent``; a free scope over the log kinds is still
refused by the scope guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARM_STORES: dict[str, str] = {
    "dense": "signature documents (masked template per signature) and incident summaries; never log-line text",
    "lexical": "signature documents, per-tenant redacted examples (signature_examples) and incident documents",
    "structured": "typed day/hour rollups with exactness and counting basis; ask through a log intent, never a free scope over these kinds",
    "graph": "entities from the pattern library (components, dependencies, services, tenants) linked by span-anchored facts and header links",
    "community": "co-firing incidents: signatures that fire together per tenant and hour (statistical co-occurrence, not causality)",
}
STRUCTURED_KINDS: dict[str, str] = {
    "log_day": "lines per day x tenant x env x service x level x status x signature (basis level_field or format_group)",
    "log_hour": "the same per UTC hour",
    "unit_day": "the source's own counts per day x unit: lines_total, count_<query> with basis_<query> (level_field or token_match)",
    "signature_day": "lines per day x unit x signature",
    "endpoint_day": "lines, latency sum/count/max and per-row p95 per endpoint, method and status class",
    "exception_day": "lines per exception class",
    "coverage_day": "pulled, parsed, noise and unparsed lines per day x unit",
    "signature_catalog": "first_seen, last_seen, days_seen, lines_history per signature (whole history)",
    "incident": "co-firing incidents with their hours, signatures and statistics",
}
GRAPH_EDGE_KINDS = ("native_link", "fact", "co_occurrence")
VOCABULARY_FIELDS = ("service", "tenant", "env", "level")
_LIBRARY_CACHE: dict[tuple[str, tuple[tuple[str, float], ...]], list[dict[str, Any]]] = {}


def library_summary(ref: Any, cap: int) -> dict[str, Any]:
    """Approved message entries of the configured pattern library (cached per file set)."""
    if ref is None:
        return {"available": False, "reason": "extraction.pattern_library is not configured"}
    root = Path(ref.path)
    try:
        stamp = tuple(sorted((p.name, p.stat().st_mtime) for p in root.glob("*.yaml")))
    except OSError as exc:
        return {"available": False, "reason": f"cannot read the library directory: {exc.strerror}"}
    key = (str(root), stamp)
    if key not in _LIBRARY_CACHE:
        from ..extraction.patterns.engine import select_engine
        from ..extraction.patterns.library import load_library

        try:
            library = load_library(root, select_engine(ref.engine), strict=False)
        except Exception as exc:  # an engine that is not installed, an unreadable file: reported, not guessed
            return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
        rows = []
        for loaded in library.approved("message"):
            entry = loaded.entry
            provenance = entry.provenance or {}
            rows.append(
                {
                    "signature": entry.signature.id if entry.signature else entry.id,
                    "issue_name": provenance.get("issue_name"),
                    "category": entry.signature.category if entry.signature else None,
                    "services": entry.selector("service"),
                    "fields": sorted(entry.fields),
                }
            )
        _LIBRARY_CACHE.clear()
        _LIBRARY_CACHE[key] = sorted(rows, key=lambda r: str(r["signature"]))
    rows = _LIBRARY_CACHE[key]
    return {"available": True, "entries": [{k: v for k, v in r.items() if v not in (None, [], {})} for r in rows[:cap]], "total": len(rows)}


def _dates(manifest: dict[str, Any], cap: int) -> dict[str, Any]:
    logs = manifest.get("logs") or {}
    days = sorted(logs.get("days") or [])
    out: dict[str, Any] = {
        "date_basis": logs.get("date_basis", "index_date"),
        "timezone": logs.get("date_timezone", "UTC"),
        "served_days": {"first": days[0] if days else None, "last": days[-1] if days else None, "count": len(days)},
        "partition_window": logs.get("window"),
        "missing_days": list(logs.get("missing") or [])[:cap],
    }
    shards = (logs.get("history") or {}).get("shards") or {}
    if shards:
        out["history_shards"] = len(shards)
    return out


def _intents() -> dict[str, Any]:
    from ..runtime.log_tools import DESCRIPTIONS
    from .log_intents import INPUT_MODELS

    return {name: {"description": DESCRIPTIONS[name], "input_schema": _compact(model.model_json_schema())} for name, model in INPUT_MODELS.items()}


def _compact(schema: dict[str, Any]) -> dict[str, Any]:
    """The input schema without titles (they repeat the field names)."""
    if isinstance(schema, dict):
        return {k: _compact(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_compact(v) for v in schema]  # type: ignore[return-value]
    return schema


def build(engine: Any, ctx: Any, *, arms: list[str], max_chars: int, values_cap: int = 50, library_cap: int = 120) -> dict[str, Any]:
    """The pack for one request (``engine`` provides ``values_in``, ``log_manifest`` and ``config``)."""
    config = engine.config
    intents = config.logs.intents
    vocab: dict[str, Any] = {}
    for name in VOCABULARY_FIELDS:
        try:
            values = engine.values_in(ctx, name, values_cap + 1)
        except Exception as exc:  # a field the backend cannot list: named, never guessed
            vocab[name] = {"unavailable": type(exc).__name__}
            continue
        vocab[name] = {"values": values[:values_cap], "truncated": len(values) > values_cap}
    pack: dict[str, Any] = {
        "arms": {arm: ARM_STORES[arm] for arm in arms if arm in ARM_STORES},
        "structured_kinds": STRUCTURED_KINDS,
        "graph_edge_kinds": list(GRAPH_EDGE_KINDS),
        "pattern_library": library_summary(config.extraction.pattern_library, library_cap),
        "data_dates": _dates(engine.log_manifest(ctx), 60),
        "vocabularies": vocab,
        "intents": _intents(),
        "few_shot": [{"question": e.question, "intent": e.intent, "args": e.args} for e in intents.examples()],
        "few_shot_source": "deployment file" if intents.few_shot_file is not None else "profile",
        "rules": [
            "answer log questions with one structured step: log_intent + log_args (never a free scope over these kinds)",
            "failure words (errors, failing, issues, problems, broken) mean levels error and fatal",
            "a question without a date is about the latest served day",
        ],
    }
    return bounded(pack, max_chars)


def bounded(pack: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Cut lists in a fixed order until the JSON fits ``max_chars``; the cuts are named."""
    cuts: list[str] = []

    def size() -> int:
        return len(json.dumps(pack, ensure_ascii=False, default=str))

    library = pack.get("pattern_library") or {}
    while size() > max_chars and len(library.get("entries") or []) > 10:
        library["entries"] = library["entries"][: len(library["entries"]) // 2]
        if "pattern_library" not in cuts:
            cuts.append("pattern_library")
    for name, item in (pack.get("vocabularies") or {}).items():
        if size() <= max_chars:
            break
        if isinstance(item, dict) and len(item.get("values") or []) > 10:
            item["values"], item["truncated"] = item["values"][:10], True
            cuts.append(f"vocabularies.{name}")
    if size() > max_chars:
        pack["intents"] = {name: {"description": v["description"], "fields": sorted((v["input_schema"].get("properties") or {}))} for name, v in pack["intents"].items()}
        cuts.append("intents.input_schema")
    while size() > max_chars and len(pack.get("few_shot") or []) > 3:
        pack["few_shot"] = pack["few_shot"][:-1]
        if "few_shot" not in cuts:
            cuts.append("few_shot")
    if cuts:
        pack["truncated"] = cuts
    return pack
