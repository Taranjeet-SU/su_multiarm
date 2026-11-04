"""Read-only inspect routes (addendum A2 §4.3; slices S1a and S1b) for the UI and operators.

| Route | Response | Source |
|---|---|---|
| ``GET /v2/info`` | ``GenerationInfo`` | the active generation's manifest (dataset, window, date basis, timezone, pattern provenance, coverage) |
| ``GET /v2/runs?limit=`` | ``list[RunSummary]`` | the content-free run reports under ``logs.partitions.path/reports/<source>`` |
| ``GET /v2/spend?days=`` | ``SpendView`` | the pattern learner's per-day spend ledger (``learning.patterns.spend_ledger``), read-only |
| ``GET /v2/patterns/view`` | ``PatternsView`` | the library snapshot the **generation** was built with (ledger ``pattern_library:<sha>``) |
| ``POST /v2/graph/neighbourhood`` | ``GraphView`` | ``find_entities`` + ``traverse`` on the generation's graph, bounded |
| ``GET /v2/graph/edges/{edge_id}`` | ``EdgeProvenance`` | the edge's stored spans (≤ 20), re-read through ``traverse`` |
| ``GET /v2/communities?limit=`` / ``GET /v2/communities/{id}`` | community summaries / detail | the generation's community store |

Rules: every route needs the ``inspect`` scope; serving is **single-tenant** (A2 B4): with a
non-default access policy the routes answer 403 instead of guessing a tenant filter. Nothing here
writes, reads working library files or calls a model. Missing optional data (no ``logs:`` section,
no ledger file, no communities) is reported as such (``available: false`` with a reason), never as
zeros. Edge ids are self-describing (base64url of subject, predicate, object, edge kind), so the
provenance route needs no server-side state.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..models import StrictModel

MAX_SPANS = 20


class NeighbourhoodRequest(StrictModel):
    entity_ids: list[str] = Field(default_factory=list, max_length=50)
    query: str | None = Field(default=None, min_length=1, max_length=500)
    hops: int = Field(default=1, ge=0, le=3)
    edge_kinds: list[Literal["fact", "co_occurrence"]] = Field(default_factory=lambda: ["fact"], min_length=1)
    predicates: list[str] = Field(default_factory=list, max_length=50)
    max_nodes: int = Field(default=300, ge=1, le=300)
    max_edges: int = Field(default=1000, ge=1, le=1000)


def edge_id(subject: str, predicate: str, obj: str, kind: str) -> str:
    raw = json.dumps([subject, predicate, obj, kind], ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_edge_id(value: str) -> tuple[str, str, str, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("malformed edge id") from exc
    if not (isinstance(parts, list) and len(parts) == 4 and all(isinstance(p, str) and p for p in parts)):
        raise ValueError("malformed edge id")
    return parts[0], parts[1], parts[2], parts[3]


def register(app, engine, scoped, call) -> None:
    """Add the inspect routes to the FastAPI ``app`` (``scoped``/``call`` are ``create_app``'s helpers)."""
    from fastapi import Depends, HTTPException, Query

    from ..runtime.acl import NoConstraints, Principal

    def single_tenant(who: Principal = Depends(scoped("inspect"))) -> Principal:
        if not isinstance(engine.acl, NoConstraints):
            raise HTTPException(status_code=403, detail="inspect routes are single-tenant (addendum A2 B4): this deployment has an access policy")
        return who

    def generation():
        current = engine.write_path.current()
        return current[0], current[1]

    def graph_store():
        _gen, manifest = generation()
        name = manifest.get("backends", {}).get("graph")
        backend = engine.backends.get(name) if name else None
        store = getattr(backend, "graph", None)
        if store is None or not hasattr(store, "traverse"):
            raise HTTPException(status_code=404, detail="this generation has no graph store")
        return store

    # --- S1a ------------------------------------------------------------------------------------

    @app.get("/v2/info")
    async def info(who: Principal = Depends(single_tenant)):
        return await call(generation_info, engine)

    @app.get("/v2/runs")
    async def runs(limit: int = Query(default=10, ge=1, le=31), who: Principal = Depends(single_tenant)):
        return await call(run_summaries, engine.config, limit)

    @app.get("/v2/spend")
    async def spend(days: int = Query(default=7, ge=1, le=31), who: Principal = Depends(single_tenant)):
        return await call(spend_view, engine.config, days)

    @app.get("/v2/patterns/view")
    async def patterns_view(who: Principal = Depends(single_tenant)):
        return await call(patterns_snapshot, engine)

    @app.get("/v2/patterns/review")
    async def patterns_review(kind: str | None = Query(default=None, pattern=r"^[a-z_]{1,32}$"), who: Principal = Depends(single_tenant)):
        return await call(review_queue, engine.config, kind)

    # --- S1b ------------------------------------------------------------------------------------

    @app.post("/v2/graph/neighbourhood")
    async def neighbourhood(body: NeighbourhoodRequest, who: Principal = Depends(single_tenant)):
        if not body.entity_ids and not body.query:
            raise HTTPException(status_code=422, detail="give entity_ids or query")
        return await call(graph_neighbourhood, graph_store(), generation()[0], body)

    @app.get("/v2/graph/edges/{edge}")
    async def edge_provenance(edge: str, who: Principal = Depends(single_tenant)):
        try:
            parts = parse_edge_id(edge)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        found = await call(graph_edge, graph_store(), generation()[0], parts)
        if found is None:
            raise HTTPException(status_code=404, detail="edge not found in the active generation")
        return found

    @app.get("/v2/communities")
    async def communities(limit: int = Query(default=50, ge=1, le=200), who: Principal = Depends(single_tenant)):
        return await call(community_list, graph_store(), generation()[0], limit)

    @app.get("/v2/communities/{community_id}")
    async def community(community_id: str, who: Principal = Depends(single_tenant)):
        found = await call(community_detail, graph_store(), generation()[0], community_id)
        if found is None:
            raise HTTPException(status_code=404, detail="unknown community in the active generation")
        return found


# --- implementations (plain functions: unit-testable without HTTP) ------------------------------


def generation_info(engine) -> dict[str, Any]:
    gen, manifest = engine.write_path.current()
    logs = manifest.get("logs") or {}
    window = logs.get("window") or {}
    coverage = manifest.get("coverage") or {}
    return {
        "generation": gen.generation,
        "dataset": manifest.get("dataset"),
        "profile": manifest.get("profile"),
        "status": manifest.get("status"),
        "window": [window.get("first"), window.get("last")] if window else None,
        "as_of": logs.get("as_of"),
        "date_basis": logs.get("date_basis"),
        "timezone": logs.get("date_timezone"),
        "display_timezone": engine.config.serving.ui.display_timezone,
        "source": logs.get("source"),
        "partitions": logs.get("partitions", []),
        "built_at": logs.get("composed_at"),
        "pattern_provenance": manifest.get("pattern_provenance"),
        "coverage_summary": {"policy": coverage.get("policy"), "incomplete": coverage.get("incomplete"), "extraction": coverage.get("extraction")},
        "counts": {k: manifest.get(k) for k in ("documents", "chunks", "entities", "relations", "communities") if isinstance(manifest.get(k), int)},
        "logs": bool(logs),
        # Rev 4: which days the generation serves, which are archived (when the manifest records it)
        # and how many library versions the served partitions span (a count is never re-based).
        "served_days": [p.get("key") for p in logs.get("partitions", []) if isinstance(p, dict)],
        "archived_days": _first_list(logs, ("archived_days", "archived", "history_days")),
        "library_identities": sorted({p.get("library_identity") for p in logs.get("partitions", []) if isinstance(p, dict) and p.get("library_identity")}),
        "flags": {k: v for k, v in logs.items() if isinstance(v, (bool, int, float, str)) and k not in {"composed_at"}},
    }


def _first_list(section: dict[str, Any], keys: tuple[str, ...]) -> list[Any] | None:
    for key in keys:
        value = section.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get("days"), list):
            return value["days"]
    return None


def run_summaries(config, limit: int) -> dict[str, Any]:
    if config.logs is None:
        return {"available": False, "reason": "no logs: section is configured", "runs": []}
    folder = Path(config.logs.partitions.path) / "reports" / config.logs.source
    if not folder.is_dir():
        return {"available": False, "reason": "no run report has been written yet", "runs": []}
    runs = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            runs.append({"name": path.stem, "unreadable": True})
            continue
        steps = report.get("steps") or {}
        runs.append(
            {
                "name": path.stem,
                "day": report.get("day"),
                "ok": report.get("ok"),
                "error": report.get("error"),
                "started_at": report.get("started_at"),
                "finished_at": report.get("finished_at"),
                "steps": {name: _step_summary(step) for name, step in steps.items() if isinstance(step, dict)},
            }
        )
    return {"available": True, "runs": runs}


def _step_summary(step: dict[str, Any]) -> dict[str, Any]:
    """Status, counts and numbers only (run reports are content-free by invariant)."""
    out: dict[str, Any] = {}
    for key, value in step.items():
        if isinstance(value, (bool, int, float)) or value is None:
            out[key] = value
        elif isinstance(value, str) and len(value) <= 200:
            out[key] = value
        elif isinstance(value, dict) and all(isinstance(v, (int, float)) for v in value.values()):
            out[key] = value
        elif isinstance(value, list):
            out[f"{key}_count"] = len(value)
    return out


def spend_view(config, days: int) -> dict[str, Any]:
    settings = config.learning.patterns
    library = config.extraction.pattern_library
    default = Path(library.path) / ".spend_ledger.sqlite" if library is not None else None
    path = (settings.spend_ledger if settings is not None else None) or default
    # Caps come from the configuration that serves now (None when learning.patterns is not configured).
    caps = {"max_usd_per_day": settings.max_usd_per_day, "max_calls_per_day": settings.max_calls_per_day, "timezone": settings.timezone} if settings is not None else None
    if path is None or not Path(path).is_file():
        return {"available": False, "reason": "no spend ledger file (no learning run has reserved spend)", "caps": caps, "rows": []}
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    uri = f"{Path(path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        rows = db.execute(
            "SELECT scope, role, day, committed_micro, reserved_micro, calls FROM spend WHERE day >= ? ORDER BY day, scope, role", (since,)
        ).fetchall()
    return {
        "available": True,
        "caps": caps,
        "since": since,
        "rows": [
            {"scope": s, "role": r, "day": d, "committed_usd": c / 1_000_000, "reserved_usd": rv / 1_000_000, "calls": n, "cap_usd": caps and caps["max_usd_per_day"], "max_calls": caps and caps["max_calls_per_day"]}
            for s, r, d, c, rv, n in rows
        ],
    }


def patterns_snapshot(engine) -> dict[str, Any]:
    _gen, manifest = engine.write_path.current()
    key = manifest.get("pattern_library_snapshot")
    if not key:
        return {"available": False, "reason": "this generation was not built with a pattern library", "entries": [], "proposals": None, "decisions": []}
    ledger = getattr(engine.backends[engine.config.storage.ledger], "ledger")
    raw = ledger.get_raw(key)
    if raw is None:
        return {"available": False, "reason": "the generation's library snapshot is no longer in the ledger", "entries": [], "proposals": None, "decisions": []}
    snapshot = json.loads(raw.raw_reply)
    entries = []
    for entry in snapshot.get("entries", []):
        provenance = entry.get("provenance") or {}
        signature = entry.get("signature") or {}
        entries.append(
            {
                "id": entry.get("id"),
                "kind": entry.get("kind"),
                "status": entry.get("status"),
                # The snapshot carries provenance only when the entry recorded one; never inferred.
                "owner": {"user": "user"}.get(str(provenance.get("author")), "learned" if provenance.get("author") else None),
                "priority": entry.get("priority"),
                "signature": signature.get("id") if isinstance(signature, dict) else None,
                "category": signature.get("category") if isinstance(signature, dict) else None,
                "regex": entry.get("regex"),
                "provenance": {k: v for k, v in provenance.items() if isinstance(v, (str, int, float, bool)) or v is None},
            }
        )
    return {
        "available": True,
        "library": snapshot.get("library"),
        "snapshot_key": key,
        "provenance": manifest.get("pattern_provenance"),
        "entries": entries,
        "decisions": list(snapshot.get("decisions", [])),
        # Proposals and their gate results live in working files (proposed.yaml), which inspect routes
        # never read (A2 §4.3): they arrive with the compose-time view of slice S1a.
        "proposals": None,
        "proposals_reason": "proposals are not part of the generation snapshot at this version",
    }


MAX_EXAMPLES = 5
MAX_REVIEW_ENTRIES = 5000


def review_queue(config, kind: str | None = None) -> dict[str, Any]:
    """The human review queue: proposals (``proposed.yaml``) with their redacted examples, gate result
    and matched-line count, the learned entries and the decision history.

    Deliberate exception to "generation only": proposals are working state that exists precisely to
    be reviewed before any generation uses them. The files are read, never written (the loader is
    read-only); the response names its source and the file shas, so a reviewer can tell which state
    the copied CLI command applies to. Example lines are the redacted lines the proposer was given
    (egress policy), shown as text."""
    from ..extraction.patterns.engine import select_engine
    from ..extraction.patterns.library import load_library

    ref = config.extraction.pattern_library
    if ref is None:
        return {"available": False, "reason": "extraction.pattern_library is not configured", "proposals": [], "learned": [], "decisions": []}
    if not Path(ref.path).is_dir():
        return {"available": False, "reason": "the pattern library directory does not exist", "proposals": [], "learned": [], "decisions": []}
    engine = select_engine(ref.engine, timeout_ms=ref.regex_timeout_ms)
    library = load_library(ref.path, engine, min_literal_chars=ref.min_literal_chars, strict=False)
    proposals, learned = [], []
    for loaded in sorted(library.entries.values(), key=lambda e: e.entry.id):
        if kind is not None and loaded.entry.kind != kind:
            continue
        if loaded.owner == "proposed":
            proposals.append(_review_row(loaded))
        elif loaded.owner == "learned":
            learned.append(_review_row(loaded))
    decisions = [d.model_dump(mode="json", exclude={"examples"}) | {"examples": len(d.examples)} for d in library.applied_decisions]
    counts: dict[str, int] = {}
    for row in proposals:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "available": True,
        "source": "working_files",
        "library": library.name,
        "file_shas": dict(library.file_shas),
        "lint_issues": len(library.issues),
        "kinds": sorted({loaded.entry.kind for loaded in library.entries.values()}),
        "proposal_status_counts": counts,
        "proposals": proposals[:MAX_REVIEW_ENTRIES],
        "learned": learned[:MAX_REVIEW_ENTRIES],
        "decisions": decisions[-MAX_REVIEW_ENTRIES:],
        "truncated": len(proposals) > MAX_REVIEW_ENTRIES or len(learned) > MAX_REVIEW_ENTRIES,
    }


def _review_row(loaded) -> dict[str, Any]:
    entry = loaded.entry
    evidence = entry.evidence or {}
    gate = evidence.get("gate") if isinstance(evidence.get("gate"), dict) else None
    positive = list(entry.examples.positive)
    return {
        "id": entry.id,
        "kind": entry.kind,
        "status": loaded.status,
        "owner": loaded.owner,
        "regex": loaded.regex,
        "signature": entry.signature.id if entry.signature is not None else None,
        "category": entry.signature.category if entry.signature is not None else None,
        "precedence": entry.precedence,
        "target": evidence.get("target"),
        "examples": [line[:2000] for line in positive[:MAX_EXAMPLES]],
        "examples_total": len(positive),
        "negative_examples": len(entry.examples.negative),
        "matched_lines": gate.get("matched") if gate else None,
        "gate": gate,
        "provenance": {k: v for k, v in entry.provenance.items() if isinstance(v, (str, int, float, bool)) or v is None},
        "decisions": list(loaded.decisions),
    }


def graph_neighbourhood(store, gen, body: NeighbourhoodRequest) -> dict[str, Any]:
    from ..core.types import TraversalSpec
    from ..providers.base import tokens

    seeds = list(dict.fromkeys(body.entity_ids))
    named: dict[str, tuple[str, str]] = {}
    if body.query:
        for entity in store.find_entities(gen, tokens(body.query), None):
            seeds.append(entity.id)
            named[entity.id] = (entity.name, entity.type)
    seeds = list(dict.fromkeys(seeds))
    if not seeds:
        return {"nodes": [], "edges": [], "truncated": False, "seeds": []}
    paths = store.traverse(gen, TraversalSpec(seed_entity_ids=seeds, hops=body.hops, predicates=list(body.predicates), direction="any", edge_kinds=list(body.edge_kinds), limit=body.max_edges), None) if body.hops else []
    truncated = len(paths) >= body.max_edges
    nodes: list[str] = list(seeds)
    edges: dict[str, dict[str, Any]] = {}
    for path in paths:
        for node in path.nodes:
            if node not in nodes:
                nodes.append(node)
        for edge in path.edges:
            ident = edge_id(edge.subject_id, edge.predicate, edge.object_id, edge.edge_kind)
            if ident not in edges:
                edges[ident] = {"id": ident, "source": edge.subject_id, "target": edge.object_id, "predicate": edge.predicate, "edge_kind": edge.edge_kind, "support": edge.support, "occurrences": max(1, len(edge.occurrences))}
    if len(nodes) > body.max_nodes:
        nodes, truncated = nodes[: body.max_nodes], True
    kept = set(nodes)
    edge_rows = [e for e in edges.values() if e["source"] in kept and e["target"] in kept]
    if len(edge_rows) > body.max_edges:
        edge_rows, truncated = edge_rows[: body.max_edges], True
    missing = [n for n in nodes if n not in named]
    for mention in store.mentions(gen, missing, None, max(len(missing) * 4, 1)) if missing else []:
        named.setdefault(mention.id, (mention.name, mention.type))
    degree: dict[str, int] = {}
    for e in edge_rows:
        degree[e["source"]] = degree.get(e["source"], 0) + 1
        degree[e["target"]] = degree.get(e["target"], 0) + 1
    return {
        "seeds": seeds,
        "nodes": [{"id": n, "name": named.get(n, (n, "unknown"))[0], "type": named.get(n, (n, "unknown"))[1], "degree": degree.get(n, 0)} for n in nodes],
        "edges": edge_rows,
        "truncated": truncated,
    }


def graph_edge(store, gen, parts: tuple[str, str, str, str]) -> dict[str, Any] | None:
    from ..core.types import TraversalSpec

    subject, predicate, obj, kind = parts
    if kind not in {"fact", "co_occurrence"}:
        return None
    paths = store.traverse(gen, TraversalSpec(seed_entity_ids=[subject], hops=1, predicates=[predicate], direction="out", edge_kinds=[kind], limit=1000), None)
    for path in paths:
        for edge in path.edges:
            if (edge.subject_id, edge.predicate, edge.object_id, edge.edge_kind) == parts:
                spans = [edge.span, *[o.span for o in edge.occurrences]]
                seen, rows = set(), []
                for span in spans:
                    key = (span.document_id, span.chunk_id, span.start, span.end)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append({"document_id": span.document_id, "chunk_id": span.chunk_id, "field": span.field, "start": span.start, "end": span.end, "quote": span.text, "match_level": span.match_level})
                return {
                    "id": edge_id(*parts),
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "edge_kind": kind,
                    "support": edge.support,
                    "extractor": edge.provenance.extractor,
                    "spans": rows[:MAX_SPANS],
                    "spans_truncated": len(rows) > MAX_SPANS,
                }
    return None


def community_list(store, gen, limit: int) -> dict[str, Any]:
    rows = store.communities(gen)
    if not rows:
        return {"available": False, "reason": "this generation has no communities (communities disabled or none detected)", "communities": []}
    rows = sorted(rows, key=lambda c: (-len(c.document_ids) - len(c.entity_ids), c.id))
    return {
        "available": True,
        "total": len(rows),
        "communities": [
            {"id": c.id, "label": c.label, "level": c.level, "parent_id": c.parent_id, "documents": len(c.document_ids), "entities": len(c.entity_ids), "report_status": c.status(), "stats": c.stats}
            for c in rows[:limit]
        ],
        "truncated": len(rows) > limit,
    }


def community_detail(store, gen, community_id: str) -> dict[str, Any] | None:
    for c in store.communities(gen):
        if c.id == community_id:
            return {
                "id": c.id,
                "label": c.label,
                "level": c.level,
                "parent_id": c.parent_id,
                "stats": c.stats,
                "document_ids": c.document_ids[:500],
                "entity_ids": c.entity_ids[:500],
                "members_truncated": len(c.document_ids) > 500 or len(c.entity_ids) > 500,
                "report": c.report,
            }
    return None


__all__ = ["NeighbourhoodRequest", "edge_id", "parse_edge_id", "register"]
