"""Accounted coverage of log partitions (addendum A2 rev 2 §4.2.1–4.2.2; review r1 B1, N1, N2, N17).

**Counting unit: the source document** (one scroll hit, de-duplicated by ``source_id``). Four
identities per pulled unit; each side is counted by a different component, so a miscount in one
shows up as an imbalance instead of being absorbed:

====  ==================================================  ===============================================
 id   identity                                            sides counted by
====  ==================================================  ===============================================
 S    ``total = Σ_strata count_s + rest``                 ``total`` and each per-hour ``count_s`` are
                                                          separate ``_count`` calls. ``rest`` (documents
                                                          outside the records tier) is **derived** in this
                                                          slice (``total`` − the tier ``_count``), so S is
                                                          reported, not enforced (``rest_basis: derived``).
 Q    ``fetched_s = min(quota_s, count_s)`` per stratum   ``fetched`` by the puller, ``quota`` by the sample
      (hour) and ``docs_pulled = Σ fetched_s``            plan, ``count`` by the per-hour ``_count``;
                                                          ``docs_pulled`` counted separately by the caller
                                                          of the puller. A unit the partition budget capped
                                                          (flagged by the puller) may fetch less.
 D    ``docs_pulled = malformed + heads + joined +        the puller / the mapper and multiline assembly
      fragments + overflow``                              (``MultilineStats``)
 H    ``heads = Σ final_status``                          assembly / the accounting of every head record
====  ==================================================  ===============================================

``fragment``, ``joined`` and ``overflow`` are document-level categories, not line statuses: a
fragment has no head record. ``sampled_out_s = count_s − fetched_s`` is a reported consequence.
Strata are disjoint by construction (one hour each plus ``outside``); a records tier whose basis is
``token_match`` makes any statement about *levels* ``approximate``.

Units stored before this accounting existed (no ``docs_pulled``) report S, Q and D as
``not_recorded``, never as zero or as balanced. H always holds or the seal is refused
(``AccountingImbalance``); it is asserted again before a fallback overlay is written.

**Final status** (one per head record): ``parsed``, ``parsed_filled``, ``provisional``,
``model_line``, ``noise`` or ``unresolved`` with one reason from ``UNRESOLVED_REASONS``.

**Pattern owner** (N2): ``user`` when the matching entry is human-owned or human-approved
(``reviewed``), ``learned`` for a system-written entry nobody reviewed, ``none`` without a match.
``deterministic_coverage`` counts every ``parsed``/``noise`` head; ``deterministic_reviewed`` only
those whose owner is ``user``.

**Validators** (N17): ``logs.accounting.validators`` declare ``min``/``max``/``enum``/``regex`` per
field. A pattern value that fails its validator is a field gap; a model value that fails it is
dropped, and a line left with none is ``range_invalid``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal

from pydantic import Field

from ..core.errors import SuError
from ..core.types import JsonScalar
from ..models import StrictModel

FinalStatus = Literal["parsed", "parsed_filled", "provisional", "model_line", "noise", "unresolved"]
FINAL_STATUSES: tuple[str, ...] = FinalStatus.__args__  # type: ignore[attr-defined]
UnresolvedReason = Literal[
    "format_only",
    "no_cluster",
    "budget_deferred",
    "below_rank",
    "model_declined",
    "gate_failed",
    "grounding_failed",
    "type_invalid",
    "range_invalid",
    "egress_refused",
    "timeout",
    "truncated_unmatched",
    "fallback_pending",
    "capacity_deferred",  # no provider route was available (owner decision 2026-11-04); retried next run
    # Additions of this slice (recorded as deviations in the execution log):
    "not_attempted",  # the fallback is not enabled for this partition
    "field_gap",  # an approved pattern matched without a valid required field; no fill resolved it
    "provider_error",  # the call failed (transport or reply schema); never retried
]
UNRESOLVED_REASONS: tuple[str, ...] = UnresolvedReason.__args__  # type: ignore[attr-defined]
FieldSource = Literal["pattern", "provisional_pattern", "model_line", "model_fill"]
PatternOwner = Literal["user", "learned", "none"]
ExtractionSource = Literal["deterministic", "model_assisted"]
MODEL_ASSISTED = ("parsed_filled", "provisional", "model_line")


class AccountingImbalance(SuError):
    """A unit whose identities do not add up: the seal (or the overlay write) is refused."""

    def __init__(self, unit_id: str, identity: str, expected: int, got: int, detail: str = ""):
        self.unit_id, self.identity, self.expected, self.got = unit_id, identity, expected, got
        super().__init__(f"accounting imbalance in unit {unit_id}: identity {identity} expected {expected}, got {got}{(' (' + detail + ')') if detail else ''}")


class FieldValue(StrictModel):
    """One extracted value: always ``parse(line[start:end])`` of the redacted head line."""

    name: str
    value: JsonScalar
    type: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    source: FieldSource
    entry_id: str | None = None
    reply_sha: str | None = None
    model: str | None = None


class HeadAccount(StrictModel):
    head_id: str
    unit_id: str
    final_status: FinalStatus
    reason: UnresolvedReason | None = None
    stage_status: str
    signature_id: str | None = None
    pattern_owner: PatternOwner = "none"
    extraction_source: ExtractionSource | None = None
    fields: list[FieldValue] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class QuotaCheck(StrictModel):
    stratum: str
    count: int
    quota: int
    fetched: int
    ok: bool
    capped: bool = False


class UnitAccounting(StrictModel):
    unit_id: str
    recorded: bool  # S, Q and D were recorded by the puller (False for older units)
    total: int = 0
    strata: dict[str, int] = Field(default_factory=dict)
    rest: int = 0
    rest_basis: Literal["counted", "derived"] = "derived"
    exactness: Literal["exact", "approximate"] = "exact"
    quota_checks: list[QuotaCheck] = Field(default_factory=list)
    docs_pulled: int = 0
    malformed: int = 0
    heads: int = 0
    joined: int = 0
    fragments: int = 0
    overflow: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_reason: dict[str, int] = Field(default_factory=dict)
    by_owner: dict[str, int] = Field(default_factory=dict)
    service: str | None = None
    tier: str = "all"

    def violations(self) -> list[tuple[str, int, int, str]]:
        """``(identity, expected, got, detail)`` for D, H and Q, plus S when ``rest`` is counted."""
        out = []
        if self.recorded:
            if self.rest_basis == "counted":
                got = sum(self.strata.values()) + self.rest
                if got != self.total:
                    out.append(("S", self.total, got, "Σ strata counts + rest"))
            for check in self.quota_checks:
                if not check.ok:
                    out.append(("Q", min(check.quota, check.count), check.fetched, f"stratum {check.stratum}: fetched vs min(quota {check.quota}, count {check.count})"))
            fetched = sum(c.fetched for c in self.quota_checks)
            if self.quota_checks and fetched != self.docs_pulled:
                out.append(("Q", fetched, self.docs_pulled, "documents handed over vs Σ fetched per stratum"))
            got = self.malformed + self.heads + self.joined + self.fragments + self.overflow
            if got != self.docs_pulled:
                out.append(("D", self.docs_pulled, got, f"malformed {self.malformed} + heads {self.heads} + joined {self.joined} + fragments {self.fragments} + overflow {self.overflow}"))
        total = sum(self.by_status.values())
        if total != self.heads:
            out.append(("H", self.heads, total, "Σ final_status"))
        unresolved = self.by_status.get("unresolved", 0)
        if sum(self.by_reason.values()) != unresolved:
            out.append(("H", unresolved, sum(self.by_reason.values()), "Σ unresolved reasons"))
        return out

    def balanced(self) -> bool:
        return not self.violations()

    def check(self) -> None:
        problems = self.violations()
        if problems:
            raise AccountingImbalance(self.unit_id, *problems[0])

    def with_statuses(self, rows: Iterable[dict[str, Any]]) -> "UnitAccounting":
        """A copy whose H side is recomputed from ``rows`` (the overlay's final statuses)."""
        statuses, reasons, owners = _tally(rows)
        return self.model_copy(update={"by_status": statuses, "by_reason": reasons, "by_owner": owners})


def _tally(rows: Iterable[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    owners: Counter[str] = Counter()
    for row in rows:
        statuses[row["final_status"]] += 1
        if row.get("reason"):
            reasons[row["reason"]] += 1
        if row["final_status"] in ("parsed", "noise"):  # by_owner: deterministic heads by account owner
            owners[row.get("account_owner") or row.get("pattern_owner") or "none"] += 1
    return dict(sorted(statuses.items())), dict(sorted(reasons.items())), dict(sorted(owners.items()))


def account_unit(artifacts: Any, rows: Iterable[dict[str, Any]], *, heads: int | None = None) -> UnitAccounting:
    """The accounting of one stored unit from its artifacts and its head rows (``final_status``,
    ``reason``, ``pattern_owner``). ``heads`` defaults to the stored head records."""
    statuses, reasons, owners = _tally(rows)
    attrs = artifacts.unit.attributes
    base = dict(
        unit_id=artifacts.unit.id,
        heads=len(artifacts.records) if heads is None else heads,
        by_status=statuses,
        by_reason=reasons,
        by_owner=owners,
        service=attrs.get("service"),
        tier=artifacts.records_query or "all",
    )
    if getattr(artifacts, "docs_pulled", None) is None:
        return UnitAccounting(recorded=False, **base)
    count, sample = artifacts.count, artifacts.sample
    tier_total = count.by_query.get(artifacts.records_query, count.total) if artifacts.records_query else count.total
    checks = []
    for stratum in sample.strata:
        expected = min(stratum.quota, stratum.count)
        ok = stratum.fetched == expected or (artifacts.capped and stratum.fetched < expected)
        checks.append(QuotaCheck(stratum=stratum.key, count=stratum.count, quota=stratum.quota, fetched=stratum.fetched, ok=ok, capped=artifacts.capped and stratum.fetched < expected))
    multiline = artifacts.multiline or {}
    return UnitAccounting(
        recorded=True,
        total=count.total,
        strata={s.key: s.count for s in sample.strata},
        rest=count.total - tier_total,
        rest_basis="derived",
        exactness="approximate" if artifacts.records_basis == "token_match" else "exact",
        quota_checks=checks,
        docs_pulled=artifacts.docs_pulled,
        malformed=artifacts.malformed or 0,
        joined=int(multiline.get("joined", 0)),
        fragments=int(multiline.get("fragments", artifacts.fragments)),
        overflow=int(multiline.get("overflow", 0)),
        **base,
    )


# --- validators ----------------------------------------------------------------------------------


class Validators:
    """``logs.accounting.validators`` compiled once (regexes linted at config load)."""

    def __init__(self, config: dict[str, Any] | None):
        self.rules = dict(config or {})
        self._regex = {name: re.compile(rule.regex) for name, rule in self.rules.items() if rule.regex}

    def ok(self, name: str, value: JsonScalar) -> bool:
        rule = self.rules.get(name)
        if rule is None or value is None:
            return True
        if rule.min is not None or rule.max is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
            if (rule.min is not None and value < rule.min) or (rule.max is not None and value > rule.max):
                return False
        if rule.enum is not None and str(value) not in rule.enum:
            return False
        if name in self._regex and not self._regex[name].search(str(value)):
            return False
        return True


@dataclass
class AccountingPolicy:
    """What the deterministic parse needs to assign final statuses: required fields per category,
    the validators and whether the fallback is enabled (``fallback_pending`` vs ``not_attempted``)."""

    required: dict[str, list[str]] = dc_field(default_factory=dict)
    validator_config: dict[str, Any] = dc_field(default_factory=dict)
    pending: bool = False

    def __post_init__(self) -> None:
        self.validators = Validators(self.validator_config)

    @classmethod
    def from_config(cls, config: Any) -> "AccountingPolicy":
        logs = getattr(config, "logs", None)
        extraction = getattr(getattr(config, "learning", None), "extraction", None)
        accounting = getattr(logs, "accounting", None)
        return cls(
            required=dict(getattr(accounting, "required_fields", {}) or {}),
            validator_config=dict(getattr(accounting, "validators", {}) or {}),
            pending=bool(extraction is not None and extraction.enabled),
        )

    def to_json(self) -> str:
        return json.dumps({"required": self.required, "validators": {k: v.model_dump() for k, v in self.validator_config.items()}, "pending": self.pending}, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "AccountingPolicy":
        from ..core.config_logs import FieldValidatorConfig

        data = json.loads(text)
        return cls(data["required"], {k: FieldValidatorConfig.model_validate(v) for k, v in data["validators"].items()}, data["pending"])


def parse_value(raw: str, kind: str, allowed: list[str] | None = None) -> JsonScalar:
    """The typed value of a span (``ValueError`` when it does not parse; never coerced)."""
    from ..extraction.patterns.extractor import _typed

    return _typed(raw, kind, allowed)


def verified(value: FieldValue, line: str, validators: Validators | None = None) -> bool:
    """The span invariant: ``parse(line[start:end]) == value`` and the validator passes."""
    if not 0 <= value.start < value.end <= len(line):
        return False
    try:
        parsed = parse_value(line[value.start : value.end], value.type)
    except ValueError:
        return False
    return parsed == value.value and (validators is None or validators.ok(value.name, value.value))


# --- deterministic final status ------------------------------------------------------------------


def reviewed_ids(library: Any) -> set[str]:
    """Ids of entries a human approved through a decision record."""
    if library is None:
        return set()
    human = {d.decision_id() for d in getattr(library, "applied_decisions", []) if d.human}
    return {e.entry.id for e in library.entries.values() if any(d in human for d in e.decisions)}


def owner_of(loaded: Any, reviewed: set[str]) -> PatternOwner:
    return "user" if loaded.owner == "user" or loaded.entry.id in reviewed else "learned"


def pattern_fields(result: Any, validators: Validators | None = None) -> tuple[list[FieldValue], list[str]]:
    """Typed values of the format, annotate and message hits with their spans, and the names of
    declared fields whose value did not parse or failed its validator (never coerced)."""
    values: dict[str, FieldValue] = {}
    invalid: set[str] = set()
    for hit in [h for h in (result.format, *result.annotations, result.message) if h is not None]:
        for name, decl in hit.entry.entry.fields.items():
            span = hit.groups.get(name)
            if span is None:
                continue
            try:
                value = parse_value(result.text[span[0] : span[1]], decl.type, decl.values)
            except ValueError:
                invalid.add(name)
                values.pop(name, None)
                continue
            if validators is not None and not validators.ok(name, value):
                invalid.add(name)
                values.pop(name, None)
                continue
            invalid.discard(name)
            values[name] = FieldValue(name=name, value=value, type=decl.type, start=span[0], end=span[1], source="pattern", entry_id=hit.entry.entry.id)
    return list(values.values()), sorted(invalid)


def deterministic_status(result: Any, *, required: dict[str, list[str]], reviewed: set[str], validators: Validators | None = None, pending: bool = False) -> dict[str, Any]:
    """``final_status``, ``reason``, ``pattern_owner``, ``extraction_source`` and ``gaps`` of one
    head line before any fallback. ``pending``: the fallback is enabled, so an unparsed head is
    ``fallback_pending`` until its overlay is written (otherwise ``not_attempted``)."""
    hit = result.message or result.noise
    out: dict[str, Any] = {"final_status": "unresolved", "reason": None, "account_owner": "none", "extraction_source": None}
    if result.status == "timeout":
        out["reason"] = "timeout"
        return out
    if hit is not None:
        out["account_owner"] = owner_of(hit.entry, reviewed)
        out["extraction_source"] = "deterministic"
        if result.message is not None:
            signature = result.message.entry.entry.signature
            need = required.get(signature.category if signature else "unknown") or []
            if need:
                values, _ = pattern_fields(result, validators)
                have = {v.name for v in values}
                gaps = [n for n in need if n not in have]
                if gaps:
                    out.update(final_status="unresolved", reason="field_gap", required_gaps=gaps, extraction_source=None)
                    return out
        out["final_status"] = "parsed" if result.message is not None else "noise"
        return out
    if result.truncated:
        out["reason"] = "truncated_unmatched"
    elif pending:
        out["reason"] = "fallback_pending"  # residue (unparsed or format_only) until its overlay is sealed
    elif result.status == "format_only":
        out["reason"] = "format_only"
    else:
        out["reason"] = "not_attempted"
    return out


# --- coverage ------------------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def coverage_metrics(accounts: Iterable[UnitAccounting]) -> dict[str, Any]:
    """Coverage over head records; fragments are reported at document level."""
    status: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    owners: Counter[str] = Counter()
    heads = fragments = docs = units = unrecorded = violations = 0
    exactness = "exact"
    for account in accounts:
        units += 1
        heads += account.heads
        status.update(account.by_status)
        reasons.update(account.by_reason)
        owners.update(account.by_owner)
        fragments += account.fragments
        docs += account.docs_pulled
        violations += len(account.violations())
        if not account.recorded:
            unrecorded += 1
        elif account.exactness == "approximate" or any(c.count > c.fetched for c in account.quota_checks):
            exactness = "approximate"
    deterministic = status["parsed"] + status["noise"]
    reviewed = owners["user"]  # parsed/noise heads whose entry is user-owned or human-approved
    assisted = sum(status[s] for s in MODEL_ASSISTED)
    covered = deterministic + assisted
    return {
        "units": units,
        "heads": heads,
        "docs_pulled": docs,
        "fragments": fragments,
        "fragment_share": _ratio(fragments, docs),
        "by_status": {s: status[s] for s in FINAL_STATUSES},
        "unresolved_by_reason": {r: reasons[r] for r in UNRESOLVED_REASONS if reasons[r]},
        "by_owner": dict(sorted(owners.items())),
        "deterministic_coverage": _ratio(deterministic, heads),
        "deterministic_reviewed": _ratio(reviewed, heads),
        "extracted_coverage": _ratio(covered, heads),
        "model_assisted_share": _ratio(assisted, covered),
        "accounted": violations == 0,
        "violations": violations,
        "source_accounting": "not_recorded" if units and unrecorded == units else ("partial" if unrecorded else exactness),
    }


def basis_note(metrics: dict[str, Any]) -> str:
    """The basis sentence of a result that includes model-assisted rows."""
    heads = metrics["heads"]
    assisted = sum(metrics["by_status"][s] for s in MODEL_ASSISTED)
    if not assisted:
        return "deterministic pattern extraction only"
    return f"includes {assisted} of {heads} lines ({100 * assisted / heads:.1f} %) extracted with model assistance (provisional)"


def targets_check(metrics: dict[str, Any], targets: Any, *, service: str | None, tier: str) -> dict[str, Any]:
    """Owner targets (O-A2-0) for one service x tier: regex-only and with the model, kept apart."""
    core = not targets.core_services or (service in targets.core_services)
    info = tier.casefold() in {"info", "info_sample"}
    goal = targets.info_tier if info else targets.deterministic
    det, ext = metrics["deterministic_coverage"], metrics["extracted_coverage"]
    return {
        "core": core,
        "deterministic_target": goal,
        "deterministic_met": None if det is None or not core else det >= goal,
        "extracted_target": targets.extracted,
        "extracted_met": None if ext is None or not core else ext >= targets.extracted,
    }


def coverage_report(regex_only: Iterable[UnitAccounting], with_llm: Iterable[UnitAccounting] | None, targets: Any = None) -> dict[str, Any]:
    """Regex-only and with-LLM numbers side by side (never merged), per service x tier and in
    total, with the owner targets when given."""
    base_list, llm_list = list(regex_only), (list(with_llm) if with_llm is not None else None)

    def groups(accounts: list[UnitAccounting]) -> dict[tuple[str, str], list[UnitAccounting]]:
        out: dict[tuple[str, str], list[UnitAccounting]] = {}
        for a in accounts:
            out.setdefault((a.service or "unknown", a.tier), []).append(a)
        return out

    base_groups = groups(base_list)
    llm_groups = groups(llm_list) if llm_list is not None else {}
    rows = []
    for service, tier in sorted(base_groups):
        regex = coverage_metrics(base_groups[(service, tier)])
        llm = coverage_metrics(llm_groups[(service, tier)]) if (service, tier) in llm_groups else None
        row: dict[str, Any] = {"service": service, "tier": tier, "regex_only": regex, "with_llm": llm, "basis": basis_note(llm or regex)}
        if targets is not None:
            combined = dict(regex)
            if llm is not None:
                combined["extracted_coverage"] = llm["extracted_coverage"]
            row["targets"] = targets_check(combined, targets, service=service, tier=tier)
        rows.append(row)
    total_regex = coverage_metrics(base_list)
    total_llm = coverage_metrics(llm_list) if llm_list is not None else None
    return {"regex_only": total_regex, "with_llm": total_llm, "basis": basis_note(total_llm or total_regex), "by_service_tier": rows}
