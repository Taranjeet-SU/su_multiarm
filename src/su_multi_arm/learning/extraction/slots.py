"""Cluster-first fallback (addendum A2 rev 2 §4.2.3; review N3, N4, N5).

Per run, over the residue heads of the partitions being processed (today's plus earlier days with
deferred heads, ranked together):

1. **Mine** the redacted bodies with the existing ``TemplateMiner`` per ``(service, level)``.
   Clusters below ``template.min_support`` (and lines the miner skips) go to the per-line tail.
2. **Rank** by size × level weight, then cluster id; the first ``template.max_clusters_per_day``
   are processed, the rest are ``below_rank`` (reported as backlog and re-ranked on the next run).
3. **Synthesise** the anchored regex with the existing synthesiser: groups ``v1..`` (variable
   tokens) and ``<mask>1..`` (masks).
4. **One cluster call** (role ``pattern_learner``) unless the reply cache holds one for the same
   ``(template, service, level, provider, model, prompt version, schema version)``. A cached reply
   contributes only its **labels and regex**; its cluster id and line ids are re-anchored on today's
   cluster.
5. **Case A**, labels for the synthesised groups: the regex is deterministic and only names and
   types come from the model. **Case B**, a regex of the model's own: built by the L4b
   ``proposal_entry``. Either way the entry goes through the **full gate** on today's member lines
   (``cluster_coverage`` and ``out_of_cluster`` included) before anything is applied.
6. **Apply provisionally**: values are group spans of each member line, parsed by the label's type
   and validated. Members the entry does not match (alignment failures) go to the per-line tail.
7. **Write** a passing entry through the existing promotion policy (``learned.yaml`` + ``approve``
   under ``auto`` for ``message`` entries, otherwise ``proposed.yaml``), unless an entry with the
   same id already exists (a cached reply of an earlier day).

Labels are matched against the group names of the synthesised regex, the field-name pattern and the
declared field types (``learning.extraction.fields``); a reply with any invalid label is rejected
whole (``gate_failed`` with the reason in the report), never partly applied.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...core.errors import BudgetExhausted
from ...extraction.patterns.library import PatternEntry
from ...ingestion.accounting import FieldValue, parse_value
from ...providers.base import ProviderError, SchemaViolation
from ..patterns.gate import GateEvidence, run_configured_gate
from ..patterns.mining import Cluster, TemplateMiner
from ..patterns.promotion import submit
from ..patterns.propose import ISSUE_CATEGORIES, nearest_patterns, proposal_entry
from ..patterns.synthesis import synthesise
from .cache import cluster_key, template_sha
from .common import LEVEL_WEIGHT, CachePending, Caller, Head, Outcome
from .replies import CLUSTER_PROMPT, ClusterExtractionReply

ACTOR = "system:pattern_learner"


@dataclass
class TierContext:
    compiled: Any  # CompiledLibrary
    ref: Any  # extraction.pattern_library
    settings: Any  # learning.extraction
    validators: Any
    caller: Caller | None
    pseudo: Any
    held_out: dict[str, list[str]]
    nearest: int
    now: datetime
    load: Callable[[], Any]
    residue: list[Head] = field(default_factory=list)  # every admitted residue head (out_of_cluster evidence)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    progress: Callable[[], Any] | None = None  # called after every cluster (incremental run report)
    max_clusters: int | None = None  # learning.bootstrap cap for this run (else template.max_clusters_per_day)

    @property
    def fields(self) -> dict[str, str]:
        return dict(self.settings.fields)


def reason_of(exc: Exception) -> str:
    if isinstance(exc, BudgetExhausted):
        return "budget_deferred"
    if isinstance(exc, CachePending):
        return exc.reason
    return "provider_error"


def _rank(cluster: Cluster) -> tuple[int, str]:
    return -cluster.size * LEVEL_WEIGHT.get((cluster.level or "").casefold(), 1), cluster.id


def cluster_tier(heads: list[Head], ctx: TierContext) -> tuple[dict[str, Outcome], list[Head]]:
    """Outcomes of clustered heads and the heads left for the per-line tail."""
    outcomes: dict[str, Outcome] = {}
    by_id = {h.id: h for h in heads}
    settings = ctx.settings.template
    miner = TemplateMiner(ctx.compiled.engine, ctx.compiled.library.masks)
    membership: dict[int, list[str]] = {}
    tail: list[Head] = []
    for head in sorted(heads, key=lambda h: (h.partition, h.id)):
        cluster = miner.add(head.id, head.body_text, head.service, head.level)
        if cluster is None:
            tail.append(head)
        else:
            membership.setdefault(cluster.seq, []).append(head.id)
    live = {c.seq: c for c in miner.result()}
    for seq, members in membership.items():
        if seq not in live:  # evicted cluster: its lines are singletons now
            tail.extend(by_id[i] for i in members)
    eligible = sorted((c for c in live.values() if c.size >= settings.min_support), key=_rank)
    for cluster in live.values():
        if cluster.size < settings.min_support:
            tail.extend(by_id[i] for i in membership.get(cluster.seq, []))
    # The cap counts clusters that need a model call. A cluster resolvable at no cost (a pending
    # proposal for its template, or a cached reply) never takes a slot, so yesterday's deferred
    # clusters get today's slots instead of the same top clusters every day (found by E1b).
    cap = ctx.max_clusters or settings.max_clusters_per_day
    ranked, below = [], []
    for cluster in eligible:
        if _free(cluster, ctx):
            ranked.append(cluster)
        elif cap > 0:
            ranked.append(cluster)
            cap -= 1
        else:
            below.append(cluster)
    for cluster in below:
        for head_id in membership.get(cluster.seq, []):
            outcomes[head_id] = Outcome("unresolved", "below_rank")
        ctx.clusters.append({"cluster_id": cluster.id, "size": cluster.size, "outcome": "below_rank"})
    for cluster in ranked:
        members = [by_id[i] for i in membership.get(cluster.seq, [])]
        row, results, aligned_fail = _one_cluster(cluster, members, ctx)
        ctx.clusters.append(row)
        outcomes.update(results)
        tail.extend(aligned_fail)
        if ctx.progress is not None:
            ctx.progress()
    return outcomes, sorted(tail, key=lambda h: h.rank())


def _free(cluster: Cluster, ctx: TierContext) -> bool:
    """The cluster resolves without a model call: a pending proposal or a cached reply on any route."""
    if ctx.caller is None:
        return False
    if pending_proposal(cluster, ctx) is not None:
        return True
    template = template_sha(cluster.tokens)
    return any(ctx.caller.cache.get(cluster_key(template, cluster.service, cluster.level, r.provider, r.model_name)) is not None for r in ctx.caller.routes)


def _payload(cluster: Cluster, members: list[Head], synthesis: Any, ctx: TierContext) -> dict[str, Any]:
    sent = members[: ctx.settings.template.max_lines_per_prompt]
    groups = []
    if synthesis.entry is not None:
        compiled = ctx.compiled.engine.compile(synthesis.entry.regex)
        examples: dict[str, list[str]] = {name: [] for name in synthesis.entry.fields}
        for head in sent:
            hit = ctx.compiled.engine.search(compiled, head.body_text)
            for name, span in (hit.groups.items() if hit else []):
                if span is not None and name in examples and len(examples[name]) < 3:
                    examples[name].append(ctx.pseudo.text(head.body_text[span[0] : span[1]]))
        groups = [{"group": name, "examples": values} for name, values in examples.items()]
    attributes = ctx.pseudo.attributes({k: v for k, v in (("service", cluster.service), ("level", cluster.level)) if v})
    return {
        "task": "cluster",
        "cluster_id": cluster.id,
        "template": ctx.pseudo.text(cluster.template()),
        "cluster_size": cluster.size,
        "attributes": attributes,
        "groups": groups,
        "synthesis": "ok" if synthesis.entry is not None else f"rejected: {synthesis.reason}",
        "fields": ctx.fields,
        "lines": [{"id": h.id, "text": ctx.pseudo.text(h.body_text)} for h in sent],
        "nearest_patterns": nearest_patterns(ctx.compiled.library, cluster, ctx.nearest),
        "issue_categories": list(ISSUE_CATEGORIES),
    }


def label_problems(reply: ClusterExtractionReply, groups: set[str], fields: dict[str, str]) -> list[str]:
    problems = []
    seen_groups, seen_fields = set(), set()
    for label in reply.labels:
        if label.group not in groups:
            problems.append(f"label names unknown group {label.group!r}")
        if label.field not in fields:
            problems.append(f"field {label.field!r} is not in learning.extraction.fields")
        elif fields[label.field] != label.type:
            problems.append(f"field {label.field!r} is declared {fields[label.field]}, labelled {label.type}")
        if label.group in seen_groups or label.field in seen_fields:
            problems.append(f"group {label.group!r} or field {label.field!r} labelled twice")
        if label.field in groups and label.field != label.group:
            problems.append(f"field name {label.field!r} collides with a group of the regex")
        seen_groups.add(label.group)
        seen_fields.add(label.field)
    return problems


def labelled_entry(synthesis_entry: PatternEntry, reply: ClusterExtractionReply, cluster: Cluster, cited: list[Head], model: str, reply_sha: str, now: datetime) -> PatternEntry:
    """Case A: the synthesised entry with the model's labels (groups renamed to field names)."""
    regex = synthesis_entry.regex
    for label in reply.labels:
        regex = regex.replace(f"(?P<{label.group}>", f"(?P<{label.field}>", 1)
    applies = dict(synthesis_entry.applies_to)
    digest = hashlib.sha256(json.dumps([regex, sorted((k, str(v)) for k, v in applies.items())]).encode()).hexdigest()[:12]
    entry_id = f"lbl.{digest}"
    noise = reply.issue_category == "noise"  # the model's own noise label: a noise entry for a human, never a message
    return PatternEntry.model_validate(
        {
            "id": entry_id,
            "kind": "noise" if noise else "message",
            "status": "proposed",
            "priority": 0,
            "applies_to": applies,
            "regex": regex,
            "fields": {} if noise else {label.field: {"type": label.type} for label in reply.labels},
            "entities": [] if noise else [{"group": label.field, "type": label.entity_type} for label in reply.labels if label.entity_type],
            **({} if noise else {"signature": {"id": entry_id, "category": reply.issue_category}}),
            "examples": {"positive": [h.body_text for h in cited]},
            "provenance": {
                "author": ACTOR,
                "created": now.isoformat(),
                "model": model,
                "labels_by": "model",
                "reply_sha": reply_sha,
                "synthesis_id": synthesis_entry.id,
                "cluster_id": cluster.id,
                "cluster_size": cluster.size,
                "template": cluster.template(),
                # Labelled metadata (N3 of A1): never evidence text, never matched.
                "issue_name": reply.issue_name,
                "issue_category": reply.issue_category,
                "rationale": reply.rationale,
            },
        }
    )


def gate_and_apply(
    entry: PatternEntry,
    members: list[Head],
    positives: list[str],
    ctx: TierContext,
    *,
    target: str | None,
    labelled: dict[str, str],
    source: str,
    reply_sha: str,
    model: str,
    out_of_cluster: list[str] | None,
) -> tuple[Any, dict[str, tuple[list[FieldValue], str | None]] | None]:
    """Run the full gate on today's members; on a pass, the group-span values of every member
    (``None`` for a member the entry does not match). ``labelled``: group -> declared type of the
    values to record."""
    lines = {h.id: h.body_text for h in members}
    evidence = GateEvidence(
        lines=lines,
        positive_ids=positives,
        held_out=ctx.held_out,
        target_pattern_id=target,
        cluster=dict(lines),
        out_of_cluster=out_of_cluster,
    )
    gate = run_configured_gate(entry, evidence, ctx.compiled.library, ctx.compiled.engine, ctx.ref)
    if not gate.passed:
        return gate, None
    compiled = ctx.compiled.engine.compile(entry.regex)
    values: dict[str, tuple[list[FieldValue], str | None]] = {}
    for head in members:
        hit = ctx.compiled.engine.search(compiled, head.body_text)
        if hit is None:
            continue
        found, reasons = [], []
        for name, kind in labelled.items():
            span = hit.groups.get(name)
            if span is None:
                continue
            start, end = span[0] + head.body[0], span[1] + head.body[0]
            try:
                value = parse_value(head.text[start:end], kind)
            except ValueError:
                reasons.append("type_invalid")
                continue
            if not ctx.validators.ok(name, value):
                reasons.append("range_invalid")
                continue
            found.append(FieldValue(name=name, value=value, type=kind, start=start, end=end, source=source, entry_id=entry.id, reply_sha=reply_sha, model=model))  # type: ignore[arg-type]
        failed = None if found or not labelled else ("range_invalid" if "range_invalid" in reasons else ("type_invalid" if reasons else None))
        values[head.id] = (found, failed)
    return gate, values


def outside_bodies(entry: PatternEntry, member_ids: set[str], ctx: TierContext) -> list[str]:
    """``out_of_cluster`` evidence: bodies of residue heads outside the cluster **that the entry
    applies to** (its ``applies_to`` selectors). The same template in another service is another
    cluster the entry can never match, so it is not over-breadth."""
    out = []
    for head in ctx.residue:
        if head.id in member_ids:
            continue
        if all(_selected(entry, key, head) for key in entry.applies_to):
            out.append(head.body_text)
    return out


def _selected(entry: PatternEntry, key: str, head: Head) -> bool:
    allowed = entry.selector(key)
    if allowed is None:
        return True
    value = "message" if key == "field" else head.attributes.get(key)
    return value is not None and value.casefold() in {a.casefold() for a in allowed}


def write_entry(entry: PatternEntry, gate: Any, ctx: TierContext, target: str | None) -> str:
    """Write through the promotion policy; ``exists`` when an entry with this id is already in the
    library (a cached reply of an earlier run)."""
    if not ctx.settings.template.write_proposals:
        return "not_written"
    if entry.id in ctx.compiled.library.entries:
        return "exists"
    from pathlib import Path

    result = submit(Path(ctx.ref.path), entry, gate, policy=ctx.ref.promotion, library=ctx.compiled.library, target_pattern_id=target, actor=ACTOR, now=ctx.now)
    if result.action in {"learned", "proposed"}:
        ctx.compiled = ctx.load()  # the next gate sees the new entry (novelty, priority)
    return result.action


def pending_proposal(cluster: Cluster, ctx: TierContext) -> Any:
    """A proposed (not yet reviewed) entry written for the same template, service and level; a
    changed template is a new cluster and gets a call."""
    for loaded in ctx.compiled.library.entries.values():
        provenance = loaded.entry.provenance or {}
        if loaded.status != "proposed" or provenance.get("template") != cluster.template():
            continue
        if loaded.entry.selector("service") == ([cluster.service] if cluster.service else None) and loaded.entry.selector("level") == ([cluster.level] if cluster.level else None):
            return loaded
    return None


def _apply_pending(pending: Any, cluster: Cluster, members: list[Head], row: dict[str, Any], ctx: TierContext) -> tuple[dict[str, Any], dict[str, Outcome], list[Head]]:
    row["reply"] = "pending_proposal"
    compiled = ctx.compiled.engine.compile(pending.regex)
    cited = [h for h in members if ctx.compiled.engine.search(compiled, h.body_text) is not None][: ctx.settings.template.max_lines_per_prompt]
    entry = pending.entry.model_copy(update={"examples": pending.entry.examples.model_copy(update={"positive": [h.body_text for h in cited], "negative": []})})
    labelled = {name: decl.type for name, decl in entry.fields.items() if ctx.fields.get(name) == decl.type}
    provenance = entry.provenance or {}
    gate, values = gate_and_apply(
        entry, members, [h.id for h in cited], ctx, target=None, labelled=labelled, source="provisional_pattern",
        reply_sha=str(provenance.get("reply_sha") or ""), model=str(provenance.get("model") or ""), out_of_cluster=outside_bodies(entry, {h.id for h in members}, ctx),
    )
    row["entry_id"], row["gate"] = entry.id, gate.summary()
    if values is None:
        row["outcome"], row["detail"] = "gate_failed", f"pending proposal fails today's gate: {', '.join(gate.failed())}"
        return row, {h.id: Outcome("unresolved", "gate_failed") for h in members}, []
    signature = entry.signature.id if entry.signature else None
    outcomes, misaligned = {}, []
    for head in members:
        if head.id not in values:
            misaligned.append(head)
            continue
        found, failed = values[head.id]
        outcomes[head.id] = Outcome("unresolved", failed) if failed else Outcome("provisional", None, found, signature, "learned")
    row["outcome"] = "provisional"
    return row, outcomes, misaligned


def _one_cluster(cluster: Cluster, members: list[Head], ctx: TierContext) -> tuple[dict[str, Any], dict[str, Outcome], list[Head]]:
    row: dict[str, Any] = {"cluster_id": cluster.id, "size": cluster.size, "service": cluster.service, "level": cluster.level}
    outcomes: dict[str, Outcome] = {}

    def all_members(reason: str) -> tuple[dict[str, Any], dict[str, Outcome], list[Head]]:
        row["outcome"] = reason
        return row, {h.id: Outcome("unresolved", reason) for h in members}, []

    if ctx.caller is None:
        return row | {"outcome": "no_caller"}, {}, list(members)
    pending = pending_proposal(cluster, ctx)
    if pending is not None:  # real-run fix: no new call for a template already waiting for review
        return _apply_pending(pending, cluster, members, row, ctx)
    synthesis = synthesise(cluster, ctx.compiled.library.masks, ctx.compiled.engine, created=ctx.now)
    row["synthesis"] = "ok" if synthesis.entry is not None else synthesis.reason
    payload = _payload(cluster, members, synthesis, ctx)
    template = template_sha(cluster.tokens)

    def keyf(provider: str, model_name: str) -> str:
        return cluster_key(template, cluster.service, cluster.level, provider, model_name)

    partitions = sorted({h.partition for h in members})
    try:
        reply, reply_sha, origin, model = ctx.caller.call(keyf, "cluster", CLUSTER_PROMPT, payload, ClusterExtractionReply, partitions)
    except (BudgetExhausted, CachePending, SchemaViolation, ProviderError) as exc:
        row["error"] = str(exc)[:200]
        return all_members(reason_of(exc))
    row["reply"] = origin
    if origin == "call" and reply.cluster_id != cluster.id:
        row["detail"] = "reply names another cluster"
        return all_members("gate_failed")
    reply = reply.model_copy(update={"cluster_id": cluster.id})  # re-anchor (N4): only labels and regex are reused
    if reply.action == "none" or (reply.regex is None and not reply.labels):
        return all_members("model_declined")
    k = ctx.settings.template.max_lines_per_prompt
    if reply.regex is None:
        if synthesis.entry is None:
            row["detail"] = "labels without a synthesised regex"
            return all_members("gate_failed")
        problems = label_problems(reply, set(synthesis.entry.fields), ctx.fields)
        if problems:
            row["detail"] = "; ".join(problems[:5])
            return all_members("gate_failed")
        cited = members[:k]
        entry = labelled_entry(synthesis.entry, reply, cluster, cited, model, reply_sha, ctx.now)
        labelled = {label.field: label.type for label in reply.labels}
        positives = [h.id for h in cited]
        target = None
    else:
        if origin == "cache":  # re-anchor the cited lines on today's members the regex matches
            try:
                compiled = ctx.compiled.engine.compile(reply.regex)
                matching = [h.id for h in members if ctx.compiled.engine.search(compiled, h.body_text) is not None][:k]
            except Exception:
                matching = []
            reply = reply.model_copy(update={"positive_line_ids": matching, "negative_line_ids": []})
        supplied = {h.id: h.body_text for h in members}
        made = proposal_entry(reply, cluster_id=cluster.id, cluster=cluster, supplied=supplied, library=ctx.compiled.library, model=model, created=ctx.now)
        if made.entry is None:
            row["detail"] = made.reason
            return all_members("model_declined" if made.outcome == "declined" else "gate_failed")
        entry = made.entry
        labelled = {name: decl.type for name, decl in entry.fields.items() if ctx.fields.get(name) == decl.type}
        positives = list(reply.positive_line_ids)
        target = made.target_pattern_id
    member_ids = {h.id for h in members}
    outside = outside_bodies(entry, member_ids, ctx)
    gate, values = gate_and_apply(entry, members, positives, ctx, target=target, labelled=labelled, source="provisional_pattern", reply_sha=reply_sha, model=model, out_of_cluster=outside)
    row["entry_id"], row["gate"] = entry.id, gate.summary()
    if values is None:
        row["detail"] = f"gate failed: {', '.join(gate.failed())}"
        return all_members("gate_failed")
    row["write"] = write_entry(entry, gate, ctx, target)
    ctx.proposals.append({"entry_id": entry.id, "kind": entry.kind, "write": row["write"], "case": "labels" if reply.regex is None else "regex", "cluster_id": cluster.id})
    signature = entry.signature.id if entry.signature else None
    misaligned = []
    for head in members:
        if head.id not in values:
            misaligned.append(head)
            continue
        found, failed = values[head.id]
        outcomes[head.id] = Outcome("unresolved", failed) if failed else Outcome("provisional", None, found, signature, "learned")
    row["outcome"] = "provisional"
    row["provisional"] = sum(1 for o in outcomes.values() if o.final_status == "provisional")
    row["misaligned"] = len(misaligned)
    return row, outcomes, misaligned

