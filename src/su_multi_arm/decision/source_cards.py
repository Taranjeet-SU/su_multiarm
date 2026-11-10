"""Source-aware arm cards of the two-halves planner (docs/plans/unified_merge.md §6.11; M2b-2).

``arm_cards`` describe what each arm does; source cards say **which sources** each arm reaches on
the unified collection and how their time works: the knowledge sources (tickets, docs, tracker
issues) with their members and cadence, the operations source (logs) with its live window
(``logs.collection.serving.default_view_days``, provisional days) and its history (episode archive
or none), and the fields joined through the identity registry (matched aliases only). They are built
from configuration only (members, layers, ``logs``, ``crosssource``): no record value, sample or
per-deployment prose enters a card, and the output is deterministic.

The llm planner receives them (``payload["source_cards"]``) only for a question with an operations
half, together with the operations half already planned deterministically, so the model plans the
knowledge half and does not re-plan log steps. A question without an operations half gets today's
payload unchanged.
"""

from __future__ import annotations

from typing import Any

CARDS_VERSION = "source_cards_v1"
KNOWLEDGE_ARMS = ("exact_id", "lexical", "dense", "graph", "community", "segment", "typed_query")
RULE = (
    "The question has two halves. Plan the knowledge half: use the enabled arms on the knowledge sources listed here "
    "(earlier tickets, documentation, tracker issues: causes, fixes, procedures). The operations half (current log "
    "evidence) is already planned in `operations_half` and runs beside your plan with the same request filters: do not "
    "plan log intents or scopes over log documents. A customer is joined across sources only through the identity "
    "registry (matched aliases); never assume that a customer name and a log tenant are the same."
)


def knowledge_sources(config: Any) -> list[str]:
    """The knowledge half's ``Document.source`` values: ``crosssource.planner.knowledge_sources``,
    else (partitioned collection) every source of a member not produced by a log host."""
    planner = getattr(getattr(config, "crosssource", None), "planner", None)
    if planner is not None and planner.knowledge_sources:
        return list(planner.knowledge_sources)
    collection = getattr(config, "collection", None)
    if collection is None:
        return []
    out: list[str] = []
    for name in sorted(collection.members):
        member = collection.members[name]
        if member.producer.kind == "log_host":
            continue
        out.extend(s for s in member.sources if s not in out)
    return out


def _members_of(config: Any, source: str) -> list[dict[str, Any]]:
    collection = getattr(config, "collection", None)
    if collection is None:
        return []
    out = []
    for name in sorted(collection.members):
        member = collection.members[name]
        if source in member.sources:
            out.append({"member": name, "kind": member.kind, "cadence": member.cadence, "key": member.key, "adopted": member.adopt is not None,
                        **({"time_roles": {k: dict(v) for k, v in sorted(member.time_roles.items())}} if member.time_roles else {})})
    return out


def _layers_of(config: Any, source: str) -> list[dict[str, Any]]:
    collection = getattr(config, "collection", None)
    if collection is None:
        return []
    return [{"layer": layer.name, "kind": layer.kind, "arms": list(layer.arms)} for layer in collection.layers if source in layer.sources]


def static_cards(config: Any, arms: list[str]) -> dict[str, Any]:
    """The configuration part of the cards (built once per engine): one card per source."""
    planner = config.crosssource.planner if getattr(config, "crosssource", None) is not None else None
    identity = getattr(getattr(config, "crosssource", None), "identity", None)
    customer_namespaces = list(planner.customer_namespaces) if planner is not None else []
    tenant_namespace = planner.tenant_namespace if planner is not None else None
    retrieval = [a for a in KNOWLEDGE_ARMS if a in arms]
    cards: list[dict[str, Any]] = []
    for source in knowledge_sources(config):
        card: dict[str, Any] = {
            "source": source,
            "half": "knowledge",
            "arms": retrieval,
            "members": _members_of(config, source),
            "layers": _layers_of(config, source),
            "time": {"tier": "snapshot", "note": "records as of their member's snapshot; not live"},
        }
        if identity is not None:
            namespaces = [n for n in customer_namespaces if n.split(".", 1)[0] in {source, "support"}] or customer_namespaces
            card["identity"] = {"namespaces": namespaces, "joined_field": "customer_id", "rule": "matched aliases of the pinned registry only"}
        cards.append(card)
    logs = getattr(config, "logs", None)
    if logs is not None and logs.intents.enabled:
        serving = logs.collection.serving
        card = {
            "source": "logs",
            "half": "operations",
            "arms": ["structured (typed log intents)"],
            "members": _members_of(config, "logs"),
            "layers": _layers_of(config, "logs"),
            "time": {
                "live_window_days": serving.default_view_days,
                "live": "the last default_view_days days are provisional (refreshed during the day)",
                "history": "episode archive (closed days)" if logs.collection.archive.enabled else "none beyond the retained days",
                "partition_timezone": logs.timezone,
                "question_timezone": config.question.timezone,
            },
        }
        if identity is not None and tenant_namespace is not None:
            card["identity"] = {"namespaces": [tenant_namespace], "joined_field": "customer_id", "rule": "matched aliases of the pinned registry only"}
        cards.append(card)
    return {"version": CARDS_VERSION, "rule": RULE, "sources": cards}


def request_cards(static: dict[str, Any], operations: list[dict[str, Any]], notes: list[str]) -> dict[str, Any]:
    """The per-request cards: the static cards plus the operations half already planned."""
    return {**static, "operations_half": {"steps": list(operations), "notes": list(notes)}}
