"""Arm cards (opt-in ``pipeline.decision.arm_cards``): a generated description of every enabled arm.

A card states, for one arm the model may choose: its purpose, when to use it and when not, the
step fields it reads with their JSON shape, its limits, and the evidence it returns. Cards are
built from the code's arm kinds, the deployment's configuration objects (``pipeline.arms.*``,
``pipeline.decision.max_steps``, declared graph templates, log intents, the typed catalog limits)
and the **wire schema** the reply is validated against (``capability_plan_schema``), so the input
shapes are exactly the accepted ones. No per-deployment prose, no source values and no samples
enter a card; a disabled arm never has one. Output is deterministic (fixed order, no clock, no
randomness).

Cards go into the llm planner payload (``arm_cards``), the typed router payload (``routes``) and,
as a limits block, the SQL writer prompt. Off (default), every request is unchanged.
"""

from __future__ import annotations

from typing import Any

CARDS_VERSION = "arm_cards_v1"

# Step fields each arm reads (the coherence rules of ``LlmStep._coherent``); ``arm`` is implicit.
STEP_FIELDS: dict[str, tuple[str, ...]] = {
    "exact_id": ("query", "filters"),
    "dense": ("query", "filters"),
    "lexical": ("query", "filters"),
    "community": ("query", "filters"),
    "segment": ("query", "filters"),
    "graph": ("query", "filters", "template", "seeds"),
    "structured": ("scope",),
    "typed_query": ("typed",),
}
LOG_STEP_FIELDS: tuple[str, ...] = ("log_intent", "log_args")

# Domain-neutral text per arm kind (code-level, not per deployment).
_TEXT: dict[str, dict[str, Any]] = {
    "exact_id": {
        "purpose": "Resolve record identifiers written in the question (declared id patterns or alias names) to exactly those records.",
        "use_when": ["the question names one or more specific record identifiers"],
        "avoid_when": ["no identifier is written in the question", "the question asks a count, list or other aggregate"],
        "returns": "the identified records; an identifier that does not resolve is reported as unknown, never guessed",
    },
    "lexical": {
        "purpose": "Keyword search over record text.",
        "use_when": ["exact words matter: names, codes, error messages, quoted phrases, rare terms"],
        "avoid_when": ["the question asks a count, list or other aggregate over records"],
        "returns": "ranked records with their matching passages (no counts)",
    },
    "dense": {
        "purpose": "Semantic search over record text (embedding similarity).",
        "use_when": ["the question is paraphrased, conceptual or describes a situation in other words"],
        "avoid_when": ["the question asks a count, list or other aggregate over records"],
        "returns": "ranked records with their most similar passages (no counts)",
    },
    "graph": {
        "purpose": "Traverse relations between extracted entities, from start entities named in the question.",
        "use_when": ["the question asks how entities are related, connected or depend on each other"],
        "avoid_when": ["no entity is named", "the question asks a count or other aggregate"],
        "returns": "records connected to the start entities and the traversed paths",
    },
    "community": {
        "purpose": "Summaries of clusters of related entities.",
        "use_when": ["the question asks for a broad overview, themes or recurring topics"],
        "avoid_when": ["the question asks about one specific record or an exact number"],
        "returns": "community summary reports with their member records",
    },
    "segment": {
        "purpose": "Semantic and keyword search over child segments of records (for example a description, a comment or a resolution note), returning the parent records.",
        "use_when": ["the question describes a problem, a step or an outcome that is written in one part of a record"],
        "avoid_when": ["the question asks a count, list or other aggregate over records"],
        "returns": "parent records with their best matching segments (kind, text and offsets) as evidence (no counts)",
    },
    "structured": {
        "purpose": "Exact counts, lists and other aggregates over the declared metadata fields (filterable_fields).",
        "use_when": ["the question asks how many, a list, a total, an average, a maximum/minimum, a ranking or a share of records"],
        "avoid_when": ["the question asks no aggregate (a lookup or an explanation)", "a needed field is not in filterable_fields"],
        "returns": "an aggregate record: value or groups, exactness (exact or bounded), denominator, a bounded list of contributor ids; unsupported constraints are reported",
    },
    "structured_logs": {
        "purpose": "Typed log intents over the stored log data (log_intents); the scope is compiled from the intent arguments.",
        "use_when": ["the question asks about log events, counts, rankings or incidents that one of log_intents expresses"],
        "avoid_when": ["no log intent expresses the question"],
        "returns": "the intent's rows and a deterministic rendering of its numbers; unsupported arguments are reported",
    },
    "typed_query_ir": {
        "purpose": "Typed lookups, lists, counts and aggregates over the tables of the projection catalog (typed_query.catalog), with at most one declared relation.",
        "use_when": ["the question asks a value, a list, a count or another aggregate that the catalog's described columns express"],
        "avoid_when": ["the question asks for an explanation or free-text content", "a needed column or relation is not in the catalog (mark not_answerable instead)"],
        "returns": "rows cited as (document_id, field) or an aggregate with exactness (exact or bounded) and denominator; a refused or unsupported query is reported, never widened",
    },
    "typed_query_sql_surface": {
        "purpose": "Send one self-contained sub-question to a SQL route over the tables of the projection catalog; the SQL is written, validated and executed with the caller's access at execution time.",
        "use_when": ["the question asks a value, a list, a count or another aggregate that the catalog's described columns and declared relations express"],
        "avoid_when": ["the question asks for an explanation or free-text content", "a needed column, relation or value is not in the catalog (mark not_answerable instead)"],
        "returns": "rows cited as (document_id, table.field) or derived result rows with the parameterised SQL and exactness; a refused, unsupported or failed route is reported, never widened",
    },
}


def _resolve(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    return node


def shape(node: dict[str, Any], defs: dict[str, Any], depth: int = 3) -> Any:
    """Compact JSON shape of one wire-schema node: ``"string"``, ``"integer | null"``, ``[item]``,
    ``{field: shape}``; an enum lists its values (at most 12, else their count); below ``depth``
    an object is the list of its property names."""
    node = _resolve(node, defs)
    if "anyOf" in node or "oneOf" in node:
        parts = [shape(option, defs, depth) for option in node.get("anyOf") or node.get("oneOf")]
        if all(isinstance(p, str) for p in parts):
            return " | ".join(dict.fromkeys(parts))
        return {"any_of": parts}
    if "enum" in node or "const" in node:
        values = node.get("enum", [node.get("const")])
        if len(values) > 12:
            return f"one of {len(values)} names"
        return "one of: " + "|".join("null" if v is None else str(v) for v in values)
    kind = node.get("type")
    if kind == "array":
        item = shape(node.get("items", {}), defs, depth - 1)
        limit = node.get("maxItems")
        return [item] if limit is None else {"list_of": item, "max_items": limit}
    if kind == "object" or "properties" in node:
        properties = node.get("properties", {})
        if depth <= 0:
            return {"object_with": list(properties)}
        return {name: shape(sub, defs, depth - 1) for name, sub in properties.items()}
    if kind is None:
        return "scalar"
    return str(kind)


def step_properties(plan_schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The wire step's properties and the schema ``$defs`` (``capability_plan_schema(...).model_json_schema()``)."""
    defs = plan_schema.get("$defs", {})
    step = _resolve(plan_schema["properties"]["steps"]["items"], defs)
    return step["properties"], defs


def arm_limits(arms_config: Any, decision_config: Any, typed: Any = None) -> dict[str, dict[str, Any]]:
    """Per enabled arm, its configured limits (from ``pipeline.arms`` and ``pipeline.decision``)."""
    out: dict[str, dict[str, Any]] = {}
    enabled = arms_config.enabled()
    if "exact_id" in enabled:
        cfg = enabled["exact_id"]
        out["exact_id"] = {"max_ids_per_step": cfg.max_ids, "case_insensitive": cfg.casefold, "alias_fields": list(cfg.alias_fields)}
    for name in ("lexical", "dense"):
        if name in enabled:
            out[name] = {"candidates_per_step": enabled[name].candidates}
    if "graph" in enabled:
        cfg = enabled["graph"]
        out["graph"] = {"hops": cfg.hops, "direction": cfg.direction, "edge_kinds": list(cfg.edge_kinds)}
    if "community" in enabled:
        out["community"] = {}
    if "segment" in enabled:
        cfg = enabled["segment"]
        out["segment"] = {"candidates_per_step": cfg.candidates, "segment_kinds": list(cfg.kinds), "segments_per_record": cfg.max_segments_per_parent}
    if "structured" in enabled:
        out["structured"] = {}
    if "typed_query" in enabled and typed is not None:
        cfg = enabled["typed_query"]
        limits: dict[str, Any] = {"mode": cfg.mode, "row_limit": cfg.row_limit, "max_groups": cfg.max_groups}
        if cfg.mode == "sql_surface":
            sql = cfg.sql_surface
            limits.update(max_joins=sql.max_joins, max_subqueries=sql.max_subqueries, max_sql_chars=sql.max_sql_chars, sql_repair_attempts=sql.repair_attempts)
        else:
            limits.update(max_relations_per_query=1, max_group_by=2, text_conditions=cfg.text_backend is not None)
        out["typed_query"] = limits
    return out


def _text_key(arm: str, log_intents: bool, typed_mode: str | None) -> str:
    if arm == "structured" and log_intents:
        return "structured_logs"
    if arm == "typed_query":
        return f"typed_query_{typed_mode or 'ir'}"
    return arm


def planner_cards(
    arms: list[str],
    plan_schema: dict[str, Any],
    limits: dict[str, dict[str, Any]],
    *,
    max_steps: int,
    log_intents: list[str] | None = None,
    graph_templates: dict[str, str] | None = None,
    typed_mode: str | None = None,
) -> dict[str, Any]:
    """The planner's ``arm_cards`` section: one card per arm the wire schema offers, in its order."""
    properties, defs = step_properties(plan_schema)
    cards = []
    for arm in arms:
        text = _TEXT[_text_key(arm, bool(log_intents), typed_mode)]
        fields = LOG_STEP_FIELDS if arm == "structured" and log_intents else STEP_FIELDS[arm]
        if arm == "graph" and not graph_templates:
            fields = ("query", "filters")
        card: dict[str, Any] = {
            "arm": arm,
            "purpose": text["purpose"],
            "use_when": list(text["use_when"]),
            "avoid_when": list(text["avoid_when"]),
            "input": {"arm": arm, **{name: shape(properties[name], defs) for name in fields if name in properties}},
            "limits": dict(limits.get(arm, {})),
            "returns": text["returns"],
        }
        if arm == "graph" and graph_templates:
            card["limits"]["templates"] = sorted(graph_templates)
        if arm == "structured" and log_intents:
            card["limits"]["log_intents"] = sorted(log_intents)
        cards.append(card)
    return {
        "version": CARDS_VERSION,
        "rule": (
            "Each card describes one available arm: what it does, when to use it, the exact step fields it "
            "reads (`input`, the shape of the reply schema), its limits and the evidence it returns. Use only "
            "the fields a card lists for its arm. The request filters apply to every arm and cannot be changed."
        ),
        "max_steps": max_steps,
        "cards": cards,
    }


def router_routes(retrieval_arms: list[str], typed_limits: dict[str, Any], typed_mode: str, *, log_intents: bool = False) -> dict[str, Any]:
    """The router's two routes: ``typed`` (the typed card's text and limits) and ``retrieval`` (the
    main planner over the enabled non-typed arms, by purpose); ``reply`` is the router's wire shape."""
    from .typed_router import TypedRoute

    wire = TypedRoute.model_json_schema()
    reply = {name: shape(node, wire.get("$defs", {})) for name, node in wire["properties"].items()}
    text = _TEXT[f"typed_query_{typed_mode}"]
    retrieval = [{"arm": arm, "purpose": _TEXT[_text_key(arm, log_intents, None)]["purpose"]} for arm in retrieval_arms if arm != "typed_query"]
    return {
        "reply": reply,
        "typed": {
            "purpose": text["purpose"],
            "use_when": list(text["use_when"]),
            "avoid_when": [item.replace(" (mark not_answerable instead)", " (route retrieval instead)") for item in text["avoid_when"]],
            "input": {**reply, "route": "typed", "sub_question": "a self-contained question in plain words (no SQL or identifiers)"},
            "limits": dict(typed_limits),
            "returns": text["returns"] + "; packed ahead of the retrieval evidence, and the retrieval planner still runs",
        },
        "retrieval": {
            "purpose": "The retrieval planner answers with the enabled arms below (it runs on every route).",
            "use_when": ["the question asks for an explanation, a procedure, free-text content, or anything the catalog does not hold"],
            "input": {**reply, "route": "retrieval", "sub_question": "null"},
            "arms": retrieval,
            "returns": "ranked records and passages, and structured aggregates when the planner plans them",
        },
    }


def sql_limits_block(arm_config: Any) -> str:
    """The SQL writer's limits and refusal rules, from ``pipeline.arms.typed_query``."""
    sql = arm_config.sql_surface
    return (
        "\n**System limits (refused before execution)**\n"
        f"- One SELECT of at most {sql.max_sql_chars} characters; at most {sql.max_joins} joins and {sql.max_subqueries} subqueries.\n"
        f"- At most {arm_config.row_limit} rows reach the answer; more rows make the result bounded.\n"
        "- Final refusals (never repaired): comments, several statements, anything but SELECT, system objects.\n"
        "- Refused as repairable: UNION/INTERSECT/EXCEPT, schema-qualified names, unknown tables or columns, "
        f"functions or constructs outside the subset above (returned with the error at most {sql.repair_attempts} time(s)).\n"
        "- A compared value the catalog does not hold is reported unsupported; it is never replaced or repaired.\n"
    )
