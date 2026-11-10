"""Per-line extraction by quotes (addendum A2 rev 2 §4.2.4): the capped tail for singletons and
alignment failures, and the line tier of the field-gap fill.

One ``log_extractor`` call per batch of at most ``lines.batch_size`` heads of one ``(service,
level, requested fields)``; each head keeps its id and a reply line naming an unknown id is dropped.
Every value is a **quote located in its own line** (``extraction/grounding.py``, levels ``exact``
and ``normalised``; an ambiguous quote only when the text is identical at every occurrence), then
parsed by the field's declared type and checked by its validator. A quote that cannot be located,
parsed or validated drops the field; nothing is repaired. Quotes are located in the redacted line,
never in the pseudonymised prompt text, so a quote that carries a pseudonym does not ground.

Each line's reply is cached on its own (``cache.line_key``), so a rebuilt overlay or a resumed run
re-grounds the cached quotes in the same line at no call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...core.errors import BudgetExhausted
from ...providers.base import ProviderError, SchemaViolation
from .cache import line_key
from .common import CachePending, Caller, Head, quoted_value, worst
from .replies import LINE_PROMPT, LineBatchReply, LineExtractionReply


def _keyf(head: Head, fields: dict[str, str]) -> Callable[[str, str], str]:
    return lambda provider, model: line_key(head.text, head.service, head.level, list(fields), provider, model)


def extract_lines(
    heads: list[Head],
    wanted: Callable[[Head], dict[str, str]],
    *,
    caller: Caller | None,
    pseudo: Any,
    batch_size: int,
    levels: list[str],
    validators: Any,
    source: str,
) -> dict[str, tuple[list[Any], str | None]]:
    """``head id -> (field values, reason when none)``. ``wanted(head)`` is the fields to ask for
    (name -> type). Reasons: ``budget_deferred``, ``capacity_deferred``, ``fallback_pending``,
    ``provider_error``, ``model_declined``, ``grounding_failed``, ``type_invalid``,
    ``range_invalid``; ``no_cluster`` without a ``log_extractor`` route."""
    out: dict[str, tuple[list[Any], str | None]] = {}
    if caller is None:
        return {h.id: ([], "no_cluster") for h in heads}
    pending: list[tuple[Head, dict[str, str]]] = []
    for head in heads:
        fields = wanted(head)
        hit = caller.cached(_keyf(head, fields), LineExtractionReply, [head.partition])
        if hit is not None:
            out[head.id] = _ground(head, hit[0], hit[1], fields, hit[2], levels, validators, source)
        else:
            pending.append((head, fields))
    groups: dict[tuple[Any, ...], list[tuple[Head, dict[str, str]]]] = {}
    for head, fields in pending:
        groups.setdefault((head.service or "", head.level or "", tuple(sorted(fields.items()))), []).append((head, fields))
    for group_key in sorted(groups):
        items = groups[group_key]
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            fields = batch[0][1]
            payload = {"task": "lines", "fields": dict(sorted(fields.items())), "lines": [{"id": h.id, "text": pseudo.text(h.text)} for h, _ in batch]}
            try:
                reply, route = caller.fresh(LINE_PROMPT, payload, LineBatchReply)
            except CachePending as exc:
                out.update({h.id: ([], exc.reason) for h, _ in batch})
                continue
            except BudgetExhausted:
                out.update({h.id: ([], "budget_deferred") for h, _ in batch})
                continue
            except (SchemaViolation, ProviderError):
                out.update({h.id: ([], "provider_error") for h, _ in batch})
                continue
            known = {h.id for h, _ in batch}
            by_id: dict[str, LineExtractionReply] = {}
            for line in reply.lines:
                if line.line_id in known and line.line_id not in by_id:  # unknown or repeated ids are dropped
                    by_id[line.line_id] = line
            for head, wanted_fields in batch:
                line = by_id.get(head.id) or LineExtractionReply(line_id=head.id)
                digest = caller.store(_keyf(head, wanted_fields)(route.provider, route.model_name), "line", line, [head.partition], route)
                out[head.id] = _ground(head, line, digest, wanted_fields, route.model_name, levels, validators, source)
    return out


def _ground(head: Head, reply: LineExtractionReply, digest: str, fields: dict[str, str], model: str, levels: list[str], validators: Any, source: str) -> tuple[list[Any], str | None]:
    values = []
    reasons: list[str] = []
    seen = set()
    for quoted in reply.fields:
        kind = fields.get(quoted.name)
        if kind is None or quoted.name in seen:
            continue  # a field that was not asked for (or twice) is dropped: it never overwrites anything
        seen.add(quoted.name)
        value, reason = quoted_value(quoted.name, kind, quoted.quote, head.text, levels=levels, validators=validators, source=source, reply_sha=digest, model=model)
        if value is None:
            reasons.append(reason or "grounding_failed")
        else:
            values.append(value)
    if values:
        return values, None
    return [], worst(reasons)
