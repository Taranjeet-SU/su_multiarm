"""Typed read-only log tools (addendum A2 §4.1.5, §4.5.1; slices L5d, M1).

Every tool runs one ``LogIntentCall``: ``decision/log_intents.compile_intent`` builds the scopes
from the arguments alone, the engine executes them on the record store inside the caller's
``RunContext`` (pinned generation, ACL, trace), and ``run_intent`` combines the results into a
``LogToolOutput`` with a deterministic ``rendering``. The decision path (a ``StructuredStep`` with
``intent``), ``Engine.call_tool``, ``POST /v2/tools/{name}`` and the MCP tools all call
``run_intent``, so they return the same rows and the same sentence.

**Rendering rules.** A dated result states its days and their basis ("index dates 2026-03-08 to
2026-03-09 (index_date, UTC)"). A ``token_match`` basis is rendered "lines containing '<token>'",
never "errors". An approximate result names its sample method. Days of the requested range without
composed data are listed as ``source_unavailable``, never counted as zero. A refused or bounded
result says why. A ``model_assisted`` extraction share is stated when present.

``incidents`` and ``graph_neighbourhood`` are the two non-intent log tools: the co-firing incidents
of a window, and a bounded neighbourhood of one graph entity with each edge's span quote and its
per-day weights (the ``lines`` of the day documents the edge was seen in).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from pydantic import Field

from ..core.component import Dependencies, Diagnostic, Environment
from ..core.errors import BackendError
from ..core.types import Document, EdgeKind, ScopeResult, ScopeSpec
from ..decision.log_intents import INPUT_MODELS, CompileContext, CompiledIntent, LogIntentCall, compile_intent
from ..models import StrictModel
from .tools import ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from .context import RunContext

NOT_CAUSAL = "statistical co-occurrence, not causality"


class LogOperations(Protocol):
    def log_context(self, ctx: RunContext) -> CompileContext: ...

    def log_execute(self, ctx: RunContext, spec: ScopeSpec) -> ScopeResult: ...

    def log_manifest(self, ctx: RunContext) -> dict[str, Any]: ...

    def documents_in(self, ctx: RunContext, ids: list[str]) -> list[Document]: ...

    def graph_view(self, ctx: RunContext, request: NeighbourhoodRequest) -> GraphView: ...


class LogToolOutput(StrictModel):
    intent: str
    args: dict[str, Any]
    status: Literal["answered", "empty", "bounded", "refused"]
    exactness: Literal["exact", "approximate", "bounded"]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    scope: ScopeSpec | None = None  # the main scope
    result: ScopeResult | None = None  # its result
    window: dict[str, str] | None = None
    source_unavailable: list[str] = Field(default_factory=list)  # requested days without composed data
    basis: list[str] = Field(default_factory=list)  # the counting basis of the rows (level_field, token_match, ...)
    methods: list[str] = Field(default_factory=list)  # sample methods of approximate rows
    notes: list[str] = Field(default_factory=list)
    refusal: str | None = None
    rendering: str
    document_ids: list[str] = Field(default_factory=list)
    truncated: bool = False


# --- execution -------------------------------------------------------------------------------------


def _value(group: Any) -> Any:
    value = group.value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _days_unavailable(window: Any, manifest: dict[str, Any]) -> list[str]:
    if window is None:
        return []
    known = set((manifest.get("logs") or {}).get("days") or [])
    if not known:
        return []
    return [d.isoformat() for d in window.days() if d.isoformat() not in known and d <= date.fromisoformat(max(known))]


def _methods(ops: Any, ctx: Any, spec: ScopeSpec) -> list[str]:
    """Sample methods of the approximate rows of ``spec`` (exactness label of the rendering)."""
    probe = spec.model_copy(update={"group_by": ["method"], "measure": None, "order": None, "top_n": None, "sort": None, "limit": None, "periods": [], "time_basis": None})
    probe = probe.model_copy(update={"predicates": [*probe.predicates, _approx()]})
    try:
        result = ops.log_execute(ctx, probe)
    except BackendError:
        return []
    return sorted(str(g.key.get("method")) for g in result.groups if g.key.get("method"))


def _approx():
    from ..core.types import Predicate

    return Predicate(field="row_exactness", op="eq", value="approximate")


def lineage(ops: Any, ctx: Any) -> dict[str, str]:
    """Superseded signature -> its final successor (H2, ``signature_lineage`` from the decisions
    replay); empty when the generation has none."""
    from ..core.types import Predicate

    try:
        result = ops.log_execute(ctx, ScopeSpec(predicates=[Predicate(field="kind", op="eq", value="signature_lineage")], limit=1000))
    except BackendError:
        return {}
    if not result.listed:
        return {}
    step = {}
    for doc in ops.documents_in(ctx, [r.id for r in result.listed]):
        old, new = doc.metadata.get("signature_id"), doc.metadata.get("successor_id")
        if isinstance(old, str) and isinstance(new, str):
            step[old] = new
    final = {}
    for old in step:
        seen, current = {old}, step[old]
        while current in step and current not in seen:
            seen.add(current)
            current = step[current]
        final[old] = current
    return final


def _fold_scopes(compiled: CompiledIntent, successors: dict[str, str]) -> list[str]:
    """A signature filter also matches the signatures it superseded."""
    from ..core.types import Predicate

    folded: list[str] = []
    for name, spec in list(compiled.scopes.items()):
        predicates = []
        for p in spec.predicates:
            if p.field == "signature_id" and p.op == "eq" and not p.negate:
                older = sorted(old for old, new in successors.items() if new == p.value)
                if older:
                    folded.extend(older)
                    p = Predicate(field="signature_id", op="in", value=[p.value, *older])
            predicates.append(p)
        compiled.scopes[name] = spec.model_copy(update={"predicates": predicates})
    return sorted(set(folded))


def _fold_rows(rows: list[dict[str, Any]], successors: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows keyed by a superseded signature merge into the successor's row (numbers added,
    first_seen the earliest)."""
    if not rows or "signature_id" not in rows[0] or not any(r.get("signature_id") in successors for r in rows):
        return rows, []
    merged: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    folded: list[str] = []
    for row in rows:
        row = dict(row)
        sig = row.get("signature_id")
        if sig in successors:
            folded.append(sig)
            row["signature_id"] = successors[sig]
        key = tuple((k, v) for k, v in row.items() if not isinstance(v, (int, float)) or isinstance(v, bool) or k in {"first_seen"})
        key = tuple(kv for kv in key if kv[0] not in {"first_seen"})
        if key not in merged:
            merged[key] = row
            order.append(key)
            continue
        target = merged[key]
        for k, v in row.items():
            if k == "first_seen":
                target[k] = min(target[k], v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(target.get(k), (int, float)):
                target[k] = target[k] + v
    out = [merged[k] for k in order]
    numeric = next((k for k in ("lines", "lines_history") if k in out[0]), None)
    if numeric:
        out.sort(key=lambda r: -(r.get(numeric) or 0))
    return out, sorted(set(folded))


def run_intent(ops: LogOperations, ctx: RunContext, call: LogIntentCall) -> LogToolOutput:
    compiled = compile_intent(call, ops.log_context(ctx))
    successors = lineage(ops, ctx)
    folded = _fold_scopes(compiled, successors) if successors else []
    manifest = ops.log_manifest(ctx)
    logs = manifest.get("logs") or {}
    basis_note = f"({logs.get('date_basis', 'index_date')}, {logs.get('date_timezone', 'UTC')})"
    window = {"start": compiled.window.start.isoformat(), "end": (compiled.window.end - timedelta(days=1)).isoformat(), "label": compiled.window.label} if compiled.window else None
    unavailable = _days_unavailable(compiled.window, manifest) if compiled.intent != "log_around" else []
    if compiled.refusal is not None:
        return LogToolOutput(intent=call.intent, args=call.args, status="refused", exactness="bounded", refusal=compiled.refusal, window=window, rendering=f"Not answered: {compiled.refusal}.", notes=compiled.notes)
    results: dict[str, ScopeResult] = {}
    try:
        for name, spec in compiled.scopes.items():
            results[name] = ops.log_execute(ctx, spec)
    except BackendError as exc:
        reason = str(exc)
        return LogToolOutput(intent=call.intent, args=call.args, status="refused", exactness="bounded", refusal=reason, window=window, rendering=f"Not answered: {reason}.", notes=compiled.notes)
    main = results["main"]
    rows = _rows(compiled, results, ops, ctx)
    if successors:
        rows, more = _fold_rows(rows, successors)
        folded = sorted(set(folded) | set(more))
    if folded:
        compiled.notes.append("folds superseded signatures: " + ", ".join(f"{old} -> {successors[old]}" for old in folded))
    documents: list[str] = []
    if compiled.intent == "log_signature":
        documents = [r.id for r in results["documents"].listed]
    if compiled.intent == "log_around":
        documents = [r.id for r in results["incidents"].listed]
    exactness = main.exactness
    for result in results.values():
        if result.exactness == "bounded":
            exactness = "bounded"
        elif result.exactness == "approximate" and exactness == "exact":
            exactness = "approximate"
    methods = _methods(ops, ctx, compiled.scopes["main"]) if exactness == "approximate" else []
    unsupported = [u for r in results.values() for u in r.unsupported]
    status = "bounded" if exactness == "bounded" else ("empty" if not rows else "answered")
    output = LogToolOutput(
        intent=call.intent,
        args=call.args,
        status=status,
        exactness=exactness,
        rows=rows,
        scope=compiled.scopes["main"],
        result=main,
        window=window,
        source_unavailable=unavailable,
        basis=sorted({b for r in results.values() for b in r.applied_basis}),
        methods=methods,
        notes=[*compiled.notes, *(f"{u.reason}: {u.suggestion or u.constraint_text}" for u in unsupported)],
        rendering="",
        document_ids=documents,
        truncated=bool(main.ties) or any(r.matched_ids_truncated for r in results.values()),
    )
    output.rendering = render(output, basis_note, ops, ctx, compiled)
    return output


def _rows(compiled: CompiledIntent, results: dict[str, ScopeResult], ops: Any, ctx: Any) -> list[dict[str, Any]]:
    main = results["main"]
    intent = compiled.intent
    if intent == "log_compare":
        return [{"period": g.key["period"], "lines": _value(g)} for g in main.groups]
    if intent == "log_coverage":
        by = compiled.scopes["main"].group_by[0]
        extra = {name: {tuple(g.key.values()): _value(g) for g in r.groups} for name, r in results.items() if name != "main"}
        rows = []
        for g in main.groups:
            key = tuple(g.key.values())
            pulled = _value(g) or 0
            parsed, noise = extra["lines_parsed"].get(key) or 0, extra["lines_noise"].get(key) or 0
            rows.append(
                {
                    by: g.key[by],
                    "lines_total": extra["lines_total"].get(key),
                    "lines_pulled": pulled,
                    "lines_parsed": parsed,
                    "lines_noise": noise,
                    "lines_unparsed": extra["lines_unparsed"].get(key) or 0,
                    "lines_format_only": extra["lines_format_only"].get(key) or 0,
                    "deterministic_coverage": round((parsed + noise) / pulled, 4) if pulled else None,
                }
            )
        return rows
    if intent == "log_endpoints" and "count" in results:
        counts = {tuple(g.key.values()): _value(g) for g in results["count"].groups}
        rows = []
        for g in main.groups:
            n = counts.get(tuple(g.key.values())) or 0
            rows.append({**g.key, "latency_ms_avg": round(_value(g) / n, 3) if n else None, "latency_ms_count": n})
        rows.sort(key=lambda r: (-(r["latency_ms_avg"] or 0), str(r.get("endpoint"))))
        top_n = compiled.args.get("top_n", 10)
        return rows[:top_n]
    if intent == "log_endpoints" and compiled.args.get("metric") == "latency_p95" and compiled.args.get("service"):
        docs = {d.id: d for d in ops.documents_in(ctx, [r.id for r in main.listed])}
        out = []
        for r in main.listed:
            m = docs[r.id].metadata if r.id in docs else {}
            out.append({k: m.get(k) for k in ("date", "tenant", "endpoint", "http_method", "status_class", "latency_ms_p95", "lines", "row_exactness")})
        return out
    if intent == "log_new":
        return [{"signature_id": g.key["signature_id"], "first_seen": g.key["first_seen"], "lines_history": _value(g)} for g in main.groups]
    measure = "lines"
    if intent == "log_endpoints" and compiled.args.get("metric") == "latency_max":
        measure = "latency_ms_max"
    if intent == "log_tenants" and main.groups:
        measure = "lines"
    rows = [{**g.key, measure: _value(g)} for g in main.groups]
    if intent in {"log_trend"} and compiled.window is not None:
        key = compiled.scopes["main"].group_by[0]
        if key == "date":
            present = {r["date"]: r for r in rows}
            known = set((ops.log_manifest(ctx).get("logs") or {}).get("days") or [])
            rows = []
            for day in compiled.window.days():
                label = day.isoformat()
                if label in present:
                    rows.append(present[label])
                elif label in known:
                    rows.append({"date": label, "lines": 0})
    return rows


# --- rendering -------------------------------------------------------------------------------------


def _counted(output: LogToolOutput, compiled: CompiledIntent) -> str:
    """What the numbers count: never "errors" for a token-match basis (A1 B2)."""
    if "token_match" in output.basis:
        if compiled.intent == "log_tenants":
            level = compiled.args.get("level", "fatal")
            return f"lines containing '{level}' (token match) or at level {level.upper()} (level field)" if "level_field" in output.basis else f"lines containing '{level}' (token match)"
        return "lines (some rows counted by token match)"
    levels = compiled.args.get("level")
    if compiled.intent == "log_tenants":
        return f"{compiled.args.get('level', 'fatal').upper()}-level lines"
    if levels:
        return ", ".join(lv.upper() for lv in levels) + "-level lines"
    return "lines"


def _exactness_text(output: LogToolOutput) -> str:
    if output.exactness == "approximate":
        return "approximate (" + (", ".join(output.methods) or "sampled") + ")"
    return output.exactness


def render(output: LogToolOutput, basis_note: str, ops: Any, ctx: Any, compiled: CompiledIntent) -> str:
    window = output.window
    where = ""
    if window is not None and compiled.intent != "log_around":
        where = f" for index dates {window['start']} to {window['end']} {basis_note}" if window["start"] != window["end"] else f" for index date {window['start']} {basis_note}"
    tail = []
    span = (ops.log_manifest(ctx).get("logs") or {}).get("identity_span") if compiled.window is not None else None
    if span:  # H2: days built under different library versions
        tail.append(f"spans {span['identities']} library identities")
    tail.extend(n for n in output.notes if n.startswith("folds superseded"))
    if output.source_unavailable:
        tail.append("source_unavailable (no composed data, not zero): " + ", ".join(output.source_unavailable))
    if output.status == "bounded":
        tail.append("bounded: " + "; ".join(output.notes[-1:]) if output.notes else "bounded")
    tail.append(_exactness_text(output) + ".")
    counted = _counted(output, compiled)
    rows = output.rows
    intent = compiled.intent
    if not rows:
        head = f"No {counted}{where}"
    elif intent == "log_top":
        key = compiled.scopes["main"].group_by[0]
        head = f"Top {len(rows)} {key} by {counted}{where}: " + "; ".join(f"{r[key]}: {r['lines']}" for r in rows)
    elif intent == "log_new":
        head = f"{len(rows)} signatures first seen{where} (across all composed history): " + "; ".join(f"{r['signature_id']} (first seen {r['first_seen']}, {r['lines_history']} lines)" for r in rows)
    elif intent == "log_affected":
        key = "tenant"
        sig = compiled.args["signature_id"]
        if "date" in rows[0]:
            head = f"{sig} by tenant and day{where}: " + "; ".join(f"{r['tenant']} {r['date']}: {r['lines']}" for r in rows)
        else:
            head = f"{len(rows)} tenants affected by {sig}{where}: " + "; ".join(f"{r[key]}: {r['lines']}" for r in rows)
    elif intent == "log_trend":
        key = compiled.scopes["main"].group_by[0]
        head = f"{counted.capitalize()} per {'hour' if key == 'ts_hour' else 'day'}{where}: " + "; ".join(f"{r[key]}: {r['lines']}" for r in rows)
    elif intent == "log_compare":
        head = f"{counted.capitalize()} by period: " + "; ".join(f"{r['period']}: {r['lines']}" for r in rows)
        if len(rows) >= 2 and isinstance(rows[0]["lines"], (int, float)) and isinstance(rows[1]["lines"], (int, float)):
            head += f" (change {rows[1]['lines'] - rows[0]['lines']:+d})" if isinstance(rows[1]["lines"] - rows[0]["lines"], int) else ""
    elif intent == "log_tenants":
        service = compiled.args.get("service")
        head = f"{len(rows)} tenants with {counted}" + (f" in {service}" if service else "") + f"{where}: " + "; ".join(f"{r['tenant']}: {r['lines']}" for r in rows)
    elif intent == "log_around":
        head = f"Signatures between {window['label'].replace(' to ', ' and ')}" + (f" for tenant {compiled.args['tenant']}" if compiled.args.get("tenant") else "") + ": " + "; ".join(f"{r['signature_id']}: {r['lines']}" for r in rows)
        if output.document_ids:
            docs = ops.documents_in(ctx, output.document_ids)
            head += ". Co-firing incidents (" + NOT_CAUSAL + "): " + "; ".join(f"{d.metadata.get('signatures', '').replace(',', ', ')} (peak {d.metadata.get('peak_hour')})" for d in docs)
    elif intent == "log_endpoints":
        metric = compiled.args.get("metric", "lines")
        if metric == "latency_p95" and compiled.args.get("service"):
            head = f"Highest per-row p95 latency (ms) of {compiled.args['service']}{where} (p95 is per row, never added or averaged): " + "; ".join(
                f"{r['endpoint']} {r['http_method']} {r['status_class']} {r['tenant']} {r['date']}: {r['latency_ms_p95']:g}" for r in rows
            )
        elif "latency_ms_avg" in rows[0]:
            head = f"Average latency (ms) by endpoint{where}: " + "; ".join(f"{r['endpoint']}: {r['latency_ms_avg']}" for r in rows)
        else:
            value = "latency_ms_max" if metric == "latency_max" else "lines"
            label = {"lines": "lines", "errors_5xx": "5xx lines", "latency_max": "maximum latency (ms)"}.get(metric, metric)
            head = f"Endpoints by {label}{where}: " + "; ".join(f"{r['endpoint']}: {r[value]:g}" if isinstance(r[value], float) else f"{r['endpoint']}: {r[value]}" for r in rows)
    elif intent == "log_signature":
        docs = {d.kind: d for d in ops.documents_in(ctx, output.document_ids)}
        template = docs.get("signature").fields.get("template") if docs.get("signature") else None
        catalog = docs.get("signature_catalog")
        total = sum(r["lines"] for r in rows if isinstance(r.get("lines"), int))
        head = f"{compiled.args['signature_id']}: {template or 'no template'}; {total} lines{where} across {len(rows)} tenants"
        if catalog is not None:
            head += f"; first seen {catalog.metadata.get('first_seen')}, last seen {catalog.metadata.get('last_seen')}"
    elif intent == "log_coverage":
        by = compiled.scopes["main"].group_by[0]
        head = f"Deterministic coverage (parsed + noise of pulled lines) by {by}{where}: " + "; ".join(f"{r[by]}: {r['deterministic_coverage']} ({r['lines_parsed'] + r['lines_noise']} of {r['lines_pulled']})" for r in rows)
    else:  # pragma: no cover - every intent is handled above
        head = intent
    return head + ". " + "; ".join(tail)


# --- the incidents and graph tools -----------------------------------------------------------------


class IncidentsInput(StrictModel):
    window: str | None = Field(default=None, max_length=64)
    as_of: date | None = None
    tenant: str | None = None
    signature_id: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=20, ge=1, le=50)


class IncidentRow(StrictModel):
    community_id: str
    tenant: str | None
    signatures: list[str]
    services: list[str]
    first_hour: str | None
    peak_hour: str | None
    last_hour: str | None
    lines: int | None
    cells: int | None
    row_exactness: str | None
    top_pairs: list[dict[str, Any]]
    document_id: str


class IncidentsOutput(StrictModel):
    incidents: list[IncidentRow]
    total: int
    rendering: str


def incidents(ops: LogOperations, ctx: RunContext, args: IncidentsInput) -> IncidentsOutput:
    from ..core.types import Predicate, Sort
    from ..decision.log_intents import LogWindow, resolve_window

    preds = [Predicate(field="kind", op="eq", value="incident")]
    if args.tenant:
        preds.append(Predicate(field="tenant", op="eq", value=args.tenant))
    if args.window:
        window = resolve_window(LogWindow(window=args.window, as_of=args.as_of), ops.log_context(ctx))
        preds.append(Predicate(field="last_hour", op="range", lower=f"{window.start.isoformat()}T00:00:00+00:00"))
        preds.append(Predicate(field="first_hour", op="range", upper=f"{window.end.isoformat()}T00:00:00+00:00", inclusive=False))
    result = ops.log_execute(ctx, ScopeSpec(predicates=preds, limit=500, sort=Sort(field="first_hour")))
    docs = {d.id: d for d in ops.documents_in(ctx, [r.id for r in result.listed])}
    rows: list[IncidentRow] = []
    for listed in result.listed:
        doc = docs.get(listed.id)
        if doc is None:
            continue
        m = doc.metadata
        signatures = [s for s in str(m.get("signatures") or "").split(",") if s]
        if args.signature_id and args.signature_id not in signatures:
            continue
        pairs = []
        for item in [p for p in str(m.get("pairs") or "").split(";") if p]:
            a, b, lift, cells = item.split("|")
            pairs.append({"a": a, "b": b, "lift": float(lift), "cells": int(cells)})
        rows.append(
            IncidentRow(
                community_id=str(m.get("community_id")),
                tenant=m.get("tenant"),  # type: ignore[arg-type]
                signatures=signatures,
                services=[s for s in str(m.get("services") or "").split(",") if s],
                first_hour=m.get("first_hour"),  # type: ignore[arg-type]
                peak_hour=m.get("peak_hour"),  # type: ignore[arg-type]
                last_hour=m.get("last_hour"),  # type: ignore[arg-type]
                lines=m.get("lines"),  # type: ignore[arg-type]
                cells=m.get("cells"),  # type: ignore[arg-type]
                row_exactness=m.get("row_exactness"),  # type: ignore[arg-type]
                top_pairs=pairs,
                document_id=doc.id,
            )
        )
    total = len(rows)
    rows = rows[: args.limit]
    text = f"{total} co-firing incidents ({NOT_CAUSAL})" + (": " + "; ".join(f"{r.tenant} {', '.join(r.signatures)} peak {r.peak_hour} ({r.lines} lines, {r.row_exactness})" for r in rows) if rows else "") + "."
    return IncidentsOutput(incidents=rows, total=total, rendering=text)


class NeighbourhoodRequest(StrictModel):
    entity: str = Field(min_length=1, max_length=256)
    hops: int = Field(default=1, ge=1, le=2)
    edge_kinds: list[EdgeKind] = Field(default_factory=lambda: ["native_link", "fact"], min_length=1)
    limit: int = Field(default=100, ge=1, le=500)
    # per-day weights cover the last ``days`` days of the served history (the edges themselves
    # cover all of it); bounded so the cost does not grow with the history kept
    days: int = Field(default=30, ge=1, le=3660)
    as_of: date | None = None


class GraphNode(StrictModel):
    id: str
    name: str
    type: str


class GraphEdge(StrictModel):
    subject: str
    predicate: str
    object: str
    edge_kind: str
    support: str
    quote: str
    document_id: str
    occurrences: int
    weights: list[dict[str, Any]] = Field(default_factory=list)  # [{date, lines}] from the day documents


class GraphView(StrictModel):
    seeds: list[str]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = False
    note: str = ""


# --- tool classes ----------------------------------------------------------------------------------


class _LogTool:
    kind: ClassVar[str] = "tool"
    config_model: ClassVar[type[StrictModel]] = StrictModel
    description: ClassVar[str] = ""
    name: ClassVar[str] = ""

    def __init__(self, config: StrictModel, deps: Dependencies):
        self.config = config
        self.engine: Any = deps.components.get("engine")

    def validate(self, env: Environment, probe: bool = False) -> list[Diagnostic]:
        if self.engine is None:
            return [Diagnostic(severity="error", code="engine_missing", message=f"tool {self.name}: engine operations not provided", component=self.name)]
        return []

    def recipe_fingerprint(self) -> str | None:
        return None

    def run(self, ctx: RunContext, inputs: Any) -> ToolResult:
        return self.invoke(ctx, inputs)

    def invoke(self, ctx: RunContext, args: StrictModel) -> ToolResult:  # pragma: no cover - overridden
        raise NotImplementedError


DESCRIPTIONS = {
    "log_top": "Top services, signatures, tenants, exception classes or endpoints by lines in a day window.",
    "log_new": "Signatures first seen (across all composed history) within a window.",
    "log_affected": "Tenants affected by a signature in a window (optionally per day).",
    "log_trend": "Lines per day or per hour for a signature, service or level.",
    "log_compare": "Lines in two to four periods (yesterday vs the day before, ...).",
    "log_tenants": "Tenants with lines of a level (default FATAL), with the counting basis.",
    "log_around": "Signatures firing around a time, and the co-firing incidents (not causality).",
    "log_endpoints": "Endpoints by lines, 5xx lines or latency; p95 is per row, never added.",
    "log_signature": "A signature's masked template, examples and per-tenant lines.",
    "log_coverage": "Deterministic parse coverage of the pulled lines by service, day or tenant.",
}


def _intent_tool(intent: str) -> type:
    def invoke(self, ctx, args):
        output = run_intent(self.engine, ctx, LogIntentCall(intent=intent, args=args.model_dump(mode="json", exclude_none=True)))  # type: ignore[arg-type]
        return ToolResult(output=output, evidence_ids=list(output.document_ids))

    return type(
        f"{intent.title().replace('_', '')}Tool",
        (_LogTool,),
        {"name": intent, "description": DESCRIPTIONS[intent], "input_model": INPUT_MODELS[intent], "output_model": LogToolOutput, "invoke": invoke},
    )


class IncidentsTool(_LogTool):
    name: ClassVar[str] = "incidents"
    description: ClassVar[str] = "Co-firing incidents (statistical co-occurrence, not causality) with their statistics."
    input_model: ClassVar[type[StrictModel]] = IncidentsInput
    output_model: ClassVar[type[StrictModel]] = IncidentsOutput

    def invoke(self, ctx: RunContext, args: StrictModel) -> ToolResult:
        output = incidents(self.engine, ctx, args)  # type: ignore[arg-type]
        return ToolResult(output=output, evidence_ids=[r.document_id for r in output.incidents])


class GraphNeighbourhoodTool(_LogTool):
    name: ClassVar[str] = "graph_neighbourhood"
    description: ClassVar[str] = "A bounded graph neighbourhood of one entity with span quotes and per-day edge weights."
    input_model: ClassVar[type[StrictModel]] = NeighbourhoodRequest
    output_model: ClassVar[type[StrictModel]] = GraphView

    def invoke(self, ctx: RunContext, args: StrictModel) -> ToolResult:
        view = self.engine.graph_view(ctx, args)
        return ToolResult(output=view, evidence_ids=sorted({e.document_id for e in view.edges}))


LOG_TOOL_CLASSES: tuple[type, ...] = (*(_intent_tool(name) for name in INPUT_MODELS), IncidentsTool, GraphNeighbourhoodTool)
