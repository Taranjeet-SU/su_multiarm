"""Field-gap fill (addendum A2 rev 2 §4.2.4).

A field gap is a head an approved pattern matched without a valid value (missing, unparsable or
failing its validator) for a field ``logs.accounting.required_fields`` requires for the pattern's
category. Two tiers:

1. **Cluster tier.** Gaps are grouped by ``(pattern id, gap set)``. A group of at least
   ``template.min_support`` heads is a systematic gap, so one ``pattern_learner`` call asks for a
   ``fix`` of that pattern; the fix goes through the existing gate (``fix_target``: it must keep
   every positive example of its target; ``out_of_cluster``: it may match no other residue). On a
   pass the missing values are the fixed regex's group spans (``provisional_pattern``).
2. **Line tier.** Heads still missing a value get one ``log_extractor`` quote call per batch, up
   to ``fill.max_per_day`` heads; over the cap they are ``budget_deferred``.

A model value only ever fills a gap: the pattern's own valid values are kept as they are
(``source: pattern``) and a reply naming another field is dropped. A head with at least one gap
filled is ``parsed_filled``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ...core.errors import BudgetExhausted
from ...providers.base import ProviderError, SchemaViolation
from ..patterns.mining import Cluster
from ..patterns.propose import proposal_entry
from .cache import cluster_key
from .common import CachePending, Caller, Head, Outcome
from .lines import extract_lines
from .replies import FIX_PROMPT, ClusterExtractionReply
from .slots import TierContext, gate_and_apply, outside_bodies, reason_of, write_entry


def fill_tier(gaps: list[Head], ctx: TierContext, line_caller: Caller | None, report: dict[str, Any]) -> dict[str, Outcome]:
    filled: dict[str, list[Any]] = {h.id: [] for h in gaps}
    reasons: dict[str, str] = {}
    groups: dict[tuple[str, tuple[str, ...]], list[Head]] = {}
    for head in gaps:
        groups.setdefault((head.pattern_id or "", tuple(head.gaps)), []).append(head)
    fixes = []
    for (pattern_id, gap_set), members in sorted(groups.items()):
        if ctx.caller is None or not ctx.settings.template.enabled or len(members) < ctx.settings.template.min_support or not pattern_id:
            continue
        row, values, reason = _fix(pattern_id, list(gap_set), members, ctx)
        fixes.append(row)
        for head in members:
            if values is not None and head.id in values:
                filled[head.id].extend(values[head.id])
            elif reason:
                reasons[head.id] = reason
    report["fix_calls"] = fixes
    remaining = [h for h in gaps if {v.name for v in filled[h.id]} != set(h.gaps)]
    remaining.sort(key=lambda h: h.rank())
    cap = ctx.settings.fill.max_per_day if ctx.settings.fill.enabled else 0
    sent, over = remaining[:cap], remaining[cap:]
    for head in over:
        reasons.setdefault(head.id, "budget_deferred")
    report["fill_line_deferred"] = len(over)
    if sent:
        types = ctx.fields

        def wanted(head: Head) -> dict[str, str]:
            have = {v.name for v in filled[head.id]}
            return {name: types[name] for name in head.gaps if name not in have and name in types}

        results = extract_lines(
            sent, wanted, caller=line_caller, pseudo=ctx.pseudo, batch_size=ctx.settings.lines.batch_size, levels=list(ctx.settings.grounding.levels), validators=ctx.validators, source="model_fill"
        )
        for head in sent:
            values, reason = results[head.id]
            gaps_left = set(wanted(head))
            filled[head.id].extend(v for v in values if v.name in gaps_left)
            if reason and not filled[head.id]:
                reasons[head.id] = "field_gap" if reason == "no_cluster" else reason
    out: dict[str, Outcome] = {}
    for head in gaps:
        if filled[head.id]:
            out[head.id] = Outcome("parsed_filled", None, list(head.pattern_values) + filled[head.id], head.signature_id, head.pattern_owner)
        else:
            out[head.id] = Outcome("unresolved", reasons.get(head.id, "field_gap"), [], head.signature_id, head.pattern_owner)
    return out


def _fix(pattern_id: str, gaps: list[str], members: list[Head], ctx: TierContext) -> tuple[dict[str, Any], dict[str, list[Any]] | None, str | None]:
    target = ctx.compiled.library.entries.get(pattern_id)
    group_id = "fix-" + hashlib.sha256(f"{pattern_id}\0{','.join(gaps)}".encode()).hexdigest()[:12]
    row: dict[str, Any] = {"group_id": group_id, "pattern_id": pattern_id, "gaps": gaps, "size": len(members)}
    if target is None:
        row["outcome"] = "target_missing"
        return row, None, None
    sent = members[: ctx.settings.template.max_lines_per_prompt]
    payload = {
        "task": "fix",
        "cluster_id": group_id,
        "target": {"id": pattern_id, "regex": target.regex},
        "missing_fields": {name: ctx.fields.get(name, "string") for name in gaps},
        "fields": ctx.fields,
        "lines": [{"id": h.id, "text": ctx.pseudo.text(h.body_text)} for h in sent],
    }
    service = members[0].service if len({h.service for h in members}) == 1 else None
    level = members[0].level if len({h.level for h in members}) == 1 else None

    def keyf(provider: str, model_name: str) -> str:
        return cluster_key(target.regex, service, level, provider, model_name, task="fix", extra=[pattern_id, gaps])

    partitions = sorted({h.partition for h in members})
    try:
        reply, reply_sha, origin, model = ctx.caller.call(keyf, "fix", FIX_PROMPT, payload, ClusterExtractionReply, partitions)  # type: ignore[union-attr]
    except (BudgetExhausted, CachePending, SchemaViolation, ProviderError) as exc:
        row["outcome"] = reason_of(exc)
        return row, None, reason_of(exc)
    row["reply"] = origin
    if origin == "call" and reply.cluster_id != group_id:
        row["outcome"] = "invalid_reply"
        return row, None, "gate_failed"
    reply = reply.model_copy(update={"cluster_id": group_id})
    if reply.action != "fix" or not reply.regex:
        row["outcome"] = "declined"
        return row, None, "model_declined"
    if origin == "cache":
        try:
            compiled = ctx.compiled.engine.compile(reply.regex)
            matching = [h.id for h in members if ctx.compiled.engine.search(compiled, h.body_text) is not None][: ctx.settings.template.max_lines_per_prompt]
        except Exception:
            matching = []
        reply = reply.model_copy(update={"positive_line_ids": matching, "negative_line_ids": []})
    cluster = Cluster(0, service, level, [pattern_id], size=len(members))
    made = proposal_entry(reply, cluster_id=group_id, cluster=cluster, supplied={h.id: h.body_text for h in members}, library=ctx.compiled.library, model=model, created=ctx.now)
    if made.entry is None or made.target_pattern_id != pattern_id:
        row["outcome"], row["detail"] = "invalid", made.reason or "not a fix of the gap's pattern"
        return row, None, "gate_failed"
    entry = made.entry
    labelled = {name: decl.type for name, decl in entry.fields.items() if name in gaps and ctx.fields.get(name) == decl.type}
    member_ids = {h.id for h in members}
    outside = outside_bodies(entry, member_ids, ctx)
    gate, values = gate_and_apply(entry, members, list(reply.positive_line_ids), ctx, target=pattern_id, labelled=labelled, source="provisional_pattern", reply_sha=reply_sha, model=model, out_of_cluster=outside)
    row["entry_id"], row["gate"] = entry.id, gate.summary()
    if values is None:
        row["outcome"] = "gate_failed"
        return row, None, "gate_failed"
    row["write"] = write_entry(entry, gate, ctx, pattern_id)
    ctx.proposals.append({"entry_id": entry.id, "kind": entry.kind, "write": row["write"], "case": "fix", "target": pattern_id})
    row["outcome"] = "filled"
    return row, {head_id: found for head_id, (found, _) in values.items()}, None
