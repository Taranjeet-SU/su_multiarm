"""Promotion policy ``extraction.pattern_library.promotion: auto | manual`` (owner amendment 2026-11-04).

``submit`` takes a proposal and its gate result:

- a proposal that **failed** the gate is rejected and nothing is written (the result carries why);
- under ``auto``, a passing proposal that is eligible for auto-promotion is appended to
  ``learned.yaml`` as ``approved`` (with the gate summary as ``evidence``), followed by an ``approve``
  decision record (``by: system:auto``) that copies the cited example lines;
- otherwise (``manual``, or not eligible) it is appended to ``proposed.yaml`` with its evidence and
  waits for a human ``approve`` decision.

Eligibility for auto-promotion (addendum §3.6.2): only ``message`` patterns; never a fix of a
human-owned entry; the gate must have run on RE2. ``noise``, ``format``, ``index_name``,
``continuation`` and ``annotate`` proposals always wait for a human.

The writer assigns the priority: below every approved entry of the same kind, so a learned pattern
never takes a line from an existing one by priority.

A passing **fix** (``target_pattern_id``) of a system-owned entry is auto-promoted with a
``supersede`` record (``id`` = the fix, ``target`` = the old entry), so the old entry is shadowed
without being edited. A fix of a human-owned entry always waits in ``proposed.yaml``; when a human
promotes it (``human_decision``), the record is a ``supersede`` as well, still without touching
``library.yaml``.

``human_decision`` writes the CLI-only human records ``approve`` (``patterns promote``),
``deprecate`` and ``disable`` (``by: user:<name>``). They are not ``Action``s and have no HTTP/MCP
surface. A human ``disable`` is final at load time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ...core.errors import SuError
from ...extraction.patterns.library import Decision, Library, PatternEntry
from ...extraction.patterns.writer import append_decision, append_learned, append_proposal
from .gate import GateResult

Promotion = Literal["auto", "manual"]


@dataclass
class PromotionOutcome:
    action: Literal["learned", "proposed", "rejected"]
    id: str
    priority: int | None = None
    reason: str = ""


def auto_eligible(entry: PatternEntry, library: Library, gate: GateResult, target_pattern_id: str | None = None) -> tuple[bool, str]:
    if entry.kind != "message":
        return False, f"{entry.kind} proposals need a human decision"
    if gate.engine != "re2":
        return False, "auto-promotion needs a gate run on RE2"
    if target_pattern_id is not None:
        target = library.entries.get(target_pattern_id)
        if target is None or target.owner == "user":
            return False, "fixes to human-owned patterns are never auto-promoted"
    return True, ""


def assigned_priority(library: Library, kind: str) -> int:
    lowest = library.lowest_priority(kind)
    return 0 if lowest is None else lowest - 1


def submit(
    root: str | Path,
    entry: PatternEntry,
    gate: GateResult,
    *,
    policy: Promotion,
    library: Library,
    target_pattern_id: str | None = None,
    actor: str = "system:auto",
    now: datetime | None = None,
) -> PromotionOutcome:
    if not gate.passed:
        return PromotionOutcome("rejected", entry.id, reason=f"gate failed: {', '.join(gate.failed())}")
    if entry.id in library.entries:
        return PromotionOutcome("rejected", entry.id, reason="an entry with this id already exists")
    at = now or datetime.now(timezone.utc)
    priority = assigned_priority(library, entry.kind)
    evidence = {"gate": gate.summary(), **({"target": target_pattern_id} if target_pattern_id else {})}
    eligible, why = auto_eligible(entry, library, gate, target_pattern_id)
    if policy == "auto" and eligible:
        learned = entry.model_copy(update={"status": "approved", "priority": priority, "evidence": evidence, "provenance": {**entry.provenance, "promoted_by": actor}})
        append_learned(root, learned)
        op = "supersede" if target_pattern_id else "approve"
        append_decision(
            root,
            Decision(
                op=op,
                id=entry.id,
                target=target_pattern_id,
                by=actor,
                at=at,
                reason="passed the validation gate (auto promotion)",
                examples=list(entry.examples.positive),
                gate=gate.summary(),
            ),
        )
        return PromotionOutcome("learned", entry.id, priority)
    proposal = entry.model_copy(update={"status": "proposed", "priority": priority, "evidence": evidence})
    append_proposal(root, proposal)
    return PromotionOutcome("proposed", entry.id, priority, why if policy == "auto" else "manual promotion")


class DecisionRefused(SuError, ValueError):
    """A human decision that does not apply to the entry's current state (nothing is written)."""


HumanOp = Literal["promote", "deprecate", "disable"]


def human_decision(root: str | Path, op: HumanOp, entry_id: str, *, by: str, library: Library, reason: str = "", now: datetime | None = None) -> Decision:
    """Append a human ``approve`` (promote), ``deprecate`` or ``disable`` record for ``entry_id``.

    - ``promote``: a ``proposed`` (or ``deprecated``) entry becomes approved. A proposal that is a fix
      (``evidence.target``) is promoted with ``supersede``, so its target is shadowed. The entry's
      positive examples are copied into the record (they survive retention).
    - ``deprecate``: an ``approved`` entry stops matching.
    - ``disable``: any entry not already disabled; final, it wins over every later record.
    """
    if not by.startswith("user:"):
        raise DecisionRefused(f"human decisions are recorded as user:<name>, not {by!r}")
    loaded = library.entries.get(entry_id)
    if loaded is None:
        raise DecisionRefused(f"{entry_id!r} is not in the library")
    status = loaded.status
    target = None
    if op == "promote":
        if status not in {"proposed", "deprecated"}:
            raise DecisionRefused(f"{entry_id!r} is {status}; only a proposed or deprecated entry can be promoted")
        target = (loaded.entry.evidence or {}).get("target")
        if target is not None:
            old = library.entries.get(target)
            if old is None or old.status in {"disabled", "superseded"}:
                raise DecisionRefused(f"{entry_id!r} fixes {target!r}, which is {'missing' if old is None else old.status}")
    elif op == "deprecate" and status != "approved":
        raise DecisionRefused(f"{entry_id!r} is {status}; only an approved entry can be deprecated")
    elif op == "disable" and status == "disabled":
        raise DecisionRefused(f"{entry_id!r} is already disabled")
    record_op = {"promote": "supersede" if target else "approve", "deprecate": "deprecate", "disable": "disable"}[op]
    decision = Decision(
        op=record_op,  # type: ignore[arg-type]
        id=entry_id,
        target=target,
        by=by,
        at=now or datetime.now(timezone.utc),
        reason=reason or f"patterns {op} (human)",
        examples=list(loaded.entry.examples.positive) if op == "promote" else [],
    )
    append_decision(root, decision)
    return decision
