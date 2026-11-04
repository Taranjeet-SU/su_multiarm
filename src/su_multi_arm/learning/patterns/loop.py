"""The pattern-learning loop (addendum §3.6.1–3.6.3, slice L4b): mine → propose → gate → write →
reparse.

One run:

1. **Egress.** The ``pattern_learner`` role must be allowed (``egress.allow_hosted``), and every
   input line must be a ``RedactedLine`` stamped by the configured redaction with no PII shape left;
   otherwise ``EgressRefused`` before any call.
2. **Match** every line with the current library: coverage *before*; the bodies of ``unparsed`` and
   ``format_only`` lines form the queue; the bodies each approved pattern parses (plus its positive
   examples) are its held-out lines.
3. **Mine** the queue (``TemplateMiner``), rank clusters by size × level weight and keep the top
   ``max_clusters_per_run`` with at least ``gate.min_support`` lines.
4. **Propose**: one structured ``pattern_learner`` call per cluster with ≤ ``max_lines_per_cluster``
   pseudonymised lines (``propose.py``), reserved against the run and day caps first. A refused
   reservation or a provider error stops the run; an invalid reply is recorded for its cluster.
   Nothing is retried.
5. **Gate** each entry deterministically (``gate.py``: the L4a checks plus ``cluster_coverage``,
   ``out_of_cluster`` and ``fix_target``).
6. **Write** passing entries under the promotion policy (``promotion.submit``): ``auto`` appends
   eligible ones to ``learned.yaml`` with a decision record, otherwise ``proposed.yaml``.
   ``patterns propose`` always writes to ``proposed.yaml``. ``library.yaml`` is never written.
   Optionally the system files are committed to git.
7. **Reparse**: coverage *after* with the reloaded library, plus *after if approved* (pending
   proposals evaluated as approved).

``dry_run`` stops before step 4's call: it reports the clusters and what would be sent (line ids,
prompt size, estimated USD), spends nothing and writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ...core.errors import BudgetExhausted, ConfigError
from ...extraction.patterns.coverage import CoverageReport, coverage
from ...extraction.patterns.extractor import CompiledLibrary, LineResult
from ...extraction.patterns.writer import commit_system_files, git_work_tree, require_clean_system_files
from ...providers.base import CallEstimate, ProviderError, SchemaViolation
from .bootstrap import with_proposals
from .egress import EgressPolicy, RedactedLine
from .gate import GateEvidence, run_configured_gate
from .mining import Cluster, MinerConfig, TemplateMiner
from .promotion import submit
from .propose import SYSTEM_PROMPT, PromptLine, nearest_patterns, prompt_payload, proposal_entry

ACTOR = "system:pattern_learner"
LEVEL_WEIGHT = {"fatal": 3, "critical": 3, "error": 3, "warn": 2, "warning": 2}
Mode = Literal["learn", "propose"]


@dataclass
class ClusterOutcome:
    cluster_id: str
    size: int
    service: str | None
    level: str | None
    outcome: str  # would_send | not_sent | provider_error | invalid_reply | declined | invalid | rejected | duplicate | would_learn | would_propose | learned | proposed
    sent_lines: list[str] = field(default_factory=list)
    entry_id: str | None = None
    target: str | None = None
    reason: str | None = None
    gate: dict[str, Any] | None = None
    issue_name: str | None = None
    prompt_chars: int | None = None
    estimate_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != []}


@dataclass
class LearnReport:
    mode: Mode
    policy: str
    dry_run: bool
    lines: int
    queue: int
    before: CoverageReport
    after: CoverageReport
    after_if_approved: CoverageReport
    clusters: list[ClusterOutcome]
    miner: dict[str, int]
    egress: dict[str, Any]
    stopped: str | None = None
    spend: dict[str, Any] | None = None
    git_commit: str | None = None

    def written(self) -> dict[str, list[str]]:
        return {
            "learned": [c.entry_id for c in self.clusters if c.outcome == "learned" and c.entry_id],
            "proposed": [c.entry_id for c in self.clusters if c.outcome == "proposed" and c.entry_id],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "policy": self.policy,
            "dry_run": self.dry_run,
            "lines": self.lines,
            "queue": self.queue,
            "coverage": {"before": self.before.to_dict(), "after": self.after.to_dict(), "after_if_approved": self.after_if_approved.to_dict()},
            "clusters": [c.to_dict() for c in self.clusters],
            "written": self.written(),
            "miner": self.miner,
            "egress": self.egress,
            "stopped": self.stopped,
            "spend": self.spend,
            "git_commit": self.git_commit,
        }


def body_span(result: LineResult) -> tuple[int, int] | None:
    """The message-stage text of a matched line (the format's ``then`` group, or the whole line)."""
    if result.format is None:
        return 0, len(result.text)
    then = result.format.entry.entry.then
    return result.format.groups.get(then.field_from_group) if then else None


def _rank(cluster: Cluster) -> tuple[int, int]:
    weight = LEVEL_WEIGHT.get((cluster.level or "").casefold(), 1)
    return -cluster.size * weight, cluster.seq


def learn(
    lines: Sequence[Any],
    *,
    load: Callable[[], CompiledLibrary],
    ref: Any,
    settings: Any,
    egress: EgressPolicy,
    role: Any,
    proposer: Any = None,
    meter: Any = None,
    mode: Mode = "learn",
    dry_run: bool = False,
    now: datetime | None = None,
    miner: MinerConfig | None = None,
    estimate_usd: Callable[[CallEstimate], float] | None = None,
) -> LearnReport:
    """One loop. ``load`` (re)loads the compiled library from disk; ``proposer.propose(payload,
    meter)`` returns a ``PatternProposalReply``; ``meter`` is the ``LearnerMeter`` (run + day caps)."""
    egress.check_role(role)
    admitted: list[RedactedLine] = egress.admit(lines)
    if not dry_run and (proposer is None or meter is None):
        raise ConfigError("a learning run needs a proposer and a spend meter", "learning.patterns")
    root = Path(ref.path)
    at = now or datetime.now(timezone.utc)
    commit = bool(settings.git_commit) and not dry_run and git_work_tree(root)
    if commit:
        require_clean_system_files(root)
    policy = "manual" if mode == "propose" else ref.promotion

    compiled = load()
    results = [compiled.match_line(line.text, field_name=line.field_name, attributes=line.attributes) for line in admitted]
    before = coverage(results)
    held_out: dict[str, dict[str, list[str]]] = {"message": {}, "noise": {}}
    for kind in held_out:
        for loaded in compiled.library.approved(kind):
            held_out[kind][loaded.entry.id] = list(loaded.entry.examples.positive)
    queue: dict[str, tuple[RedactedLine, str, dict[str, str]]] = {}
    for line, result in zip(admitted, results):
        span = body_span(result)
        if span is None:
            continue
        body = result.text[span[0] : span[1]]
        hit = result.message or result.noise
        if hit is not None:
            held_out[hit.entry.entry.kind].setdefault(hit.entry.entry.id, []).append(body)
            continue
        if result.status not in {"unparsed", "format_only"}:
            continue
        queue[line.id] = (line, body, result.attributes)
    # Pseudonymise before mining, so neither the prompt lines nor the cluster template carry an
    # attribute value. The gate still evaluates on the redacted (not pseudonymised) bodies.
    pseudo = egress.pseudonymiser()
    for line, _, attributes in queue.values():
        pseudo.attributes(line.attributes)
        pseudo.attributes(attributes)
    mining = TemplateMiner(compiled.engine, compiled.library.masks, miner)
    membership: dict[int, list[str]] = {}
    for line_id, (_, body, attributes) in queue.items():
        cluster = mining.add(line_id, pseudo.text(body), attributes.get("service"), attributes.get("level"))
        if cluster is not None:
            membership.setdefault(cluster.seq, []).append(line_id)

    min_support = ref.gate.min_support
    ranked = sorted((c for c in mining.result() if c.size >= min_support), key=_rank)[: settings.max_clusters_per_run]
    outcomes: list[ClusterOutcome] = []
    pending = []  # passing entries not (yet) approved: dry-run passes and proposals
    stopped: str | None = None
    for cluster in ranked:
        cluster_id = cluster.id
        members = [i for i in membership.get(cluster.seq, []) if i in queue]
        sent = members[: settings.max_lines_per_cluster]
        outcome = ClusterOutcome(cluster_id, cluster.size, cluster.service, cluster.level, "would_send", sent_lines=list(sent))
        outcomes.append(outcome)
        supplied = {i: queue[i][1] for i in sent}
        prompt_lines = [PromptLine(i, pseudo.text(supplied[i])) for i in sent]
        attributes = pseudo.attributes({k: v for k, v in (("service", cluster.service), ("level", cluster.level)) if v})
        payload = prompt_payload(cluster_id, cluster, prompt_lines, attributes, nearest_patterns(compiled.library, cluster, settings.nearest_patterns))
        outcome.prompt_chars = len(SYSTEM_PROMPT) + len(json.dumps(payload, ensure_ascii=False))
        if dry_run:
            estimate = CallEstimate.of(outcome.prompt_chars, role.max_tokens)
            outcome.estimate_usd = estimate_usd(estimate) if estimate_usd is not None else None
            continue
        try:
            reply = proposer.propose(payload, meter)
        except BudgetExhausted as exc:
            outcome.outcome, outcome.reason = "not_sent", str(exc)
            stopped = f"budget_exhausted:{exc.kind}"
            break
        except SchemaViolation as exc:
            outcome.outcome, outcome.reason = "invalid_reply", str(exc)
            continue
        except ProviderError as exc:
            outcome.outcome, outcome.reason = "provider_error", str(exc)
            stopped = f"provider_error:{exc.category}"
            break
        outcome.issue_name = reply.issue_name
        made = proposal_entry(reply, cluster_id=cluster_id, cluster=cluster, supplied=supplied, library=compiled.library, model=role.model, created=at)
        if made.entry is None:
            outcome.outcome, outcome.reason = made.outcome, made.reason
            continue
        entry = made.entry
        outcome.entry_id, outcome.target = entry.id, made.target_pattern_id
        evidence = GateEvidence(
            lines=dict(supplied),
            positive_ids=list(reply.positive_line_ids),
            negative_ids=list(reply.negative_line_ids),
            held_out=held_out[entry.kind],
            target_pattern_id=made.target_pattern_id,
            cluster={i: queue[i][1] for i in members},
            out_of_cluster=[body for i, (_, body, _) in queue.items() if i not in set(members)],
        )
        gate = run_configured_gate(entry, evidence, compiled.library, compiled.engine, ref)
        outcome.gate = gate.summary()
        if not gate.passed:
            outcome.outcome, outcome.reason = "rejected", f"gate failed: {', '.join(gate.failed())}"
            continue
        if any(p.regex == entry.regex or p.id == entry.id for p, _ in pending):
            outcome.outcome, outcome.reason = "duplicate", "same regex as another proposal of this run"
            continue
        result = submit(root, entry, gate, policy=policy, library=compiled.library, target_pattern_id=made.target_pattern_id, actor=ACTOR, now=at)
        outcome.outcome, outcome.reason = result.action, result.reason or None
        if result.action == "proposed":
            pending.append((entry, made.target_pattern_id))
        if result.action in {"learned", "proposed"}:
            compiled = load()  # the next gate sees the new entry (novelty, priority)

    final = load() if not dry_run else compiled
    after = coverage(final.match_line(line.text, field_name=line.field_name, attributes=line.attributes) for line in admitted)
    candidates = [e for e, _ in pending if e.id not in final.library.entries or final.library.entries[e.id].status != "approved"]
    hypothetical = with_proposals(final, candidates) if candidates else final
    after_if = coverage(hypothetical.match_line(line.text, field_name=line.field_name, attributes=line.attributes) for line in admitted)
    report = LearnReport(
        mode=mode,
        policy=policy,
        dry_run=dry_run,
        lines=len(admitted),
        queue=len(queue),
        before=before,
        after=after,
        after_if_approved=after_if,
        clusters=outcomes,
        miner=mining.stats(),
        egress={
            "redaction": egress.redactor.stamp,
            "redactions": sum(line.redactions for line in admitted),
            "allow_hosted": egress.settings.allow_hosted,
            "pseudonymised": len(pseudo.aliases),
        },
        stopped=stopped,
        spend=meter.summary() if meter is not None and not dry_run else None,
    )
    if commit and any(report.written().values()):
        written = report.written()
        message = f"patterns {mode}: {len(written['learned'])} learned, {len(written['proposed'])} proposed"
        report.git_commit = commit_system_files(root, ACTOR, message)
    return report


@dataclass
class LearningSession:
    """Everything one run needs, built from a v2 ``Config`` (``session_from_config``)."""

    load: Callable[[], CompiledLibrary]
    ref: Any
    settings: Any
    egress: EgressPolicy
    role: Any
    redactor: Any
    proposer: Any = None
    meter: Any = None
    estimate_usd: Callable[[CallEstimate], float] | None = None

    def run(self, lines: Sequence[Any], *, mode: Mode = "learn", dry_run: bool = False, now: datetime | None = None) -> LearnReport:
        redacted = [line if isinstance(line, RedactedLine) else self.redactor.redact(line) for line in lines]
        return learn(
            redacted,
            load=self.load,
            ref=self.ref,
            settings=self.settings,
            egress=self.egress,
            role=self.role,
            proposer=self.proposer,
            meter=self.meter,
            mode=mode,
            dry_run=dry_run,
            now=now,
            estimate_usd=self.estimate_usd,
        )


def session_from_config(config: Any, *, dry_run: bool = False, max_usd: float | None = None, transport: Any = None, clock: Callable[[], datetime] | None = None) -> LearningSession:
    """Build the redactor, egress policy, spend meter (run + day caps) and proposer from config.

    ``max_usd`` (``--max-usd``) lowers the per-run USD cap; it can never raise it. A dry run builds no
    meter, no ledger and no provider: it only estimates."""
    from ...extraction.patterns.extractor import PatternExtractor
    from ...extraction.predicate_support import PredicateSupport
    from ...runtime.budget import load_price_table
    from .egress import LineRedactor
    from .propose import LlmProposer
    from .spend import DaySpendLedger, LearnerMeter

    settings = config.learning.patterns
    ref = config.extraction.pattern_library
    role = config.models.roles.pattern_learner
    if settings is None or ref is None or role is None:
        raise ConfigError("patterns learn/propose need learning.patterns, extraction.pattern_library and models.roles.pattern_learner", "learning.patterns")
    redactor = LineRedactor(config.privacy)
    egress = EgressPolicy(settings.egress, redactor)
    price_table = load_price_table(config.budgets.price_table) if config.budgets.price_table else None
    run_usd = settings.max_usd_per_run
    if max_usd is not None:
        if price_table is None:
            raise ConfigError("--max-usd needs budgets.price_table", "budgets.price_table")
        run_usd = max_usd if run_usd is None else min(run_usd, max_usd)
    pricing_key = role.price_key()
    priced = price_table is not None and pricing_key in price_table.prices
    if run_usd is not None and not priced:
        raise ConfigError(f"a USD cap needs a price for {pricing_key!r} in budgets.price_table", "models.roles.pattern_learner.pricing_key")
    estimate = (lambda e: price_table.cost(pricing_key, e.input_tokens, e.output_tokens)) if priced else None  # type: ignore[union-attr]
    support = PredicateSupport(config.extraction.stemmer_language)

    def load() -> CompiledLibrary:
        return PatternExtractor(ref, support).load()

    session = LearningSession(load, ref, settings, egress, role, redactor, estimate_usd=estimate)
    if dry_run:
        return session
    ledger = DaySpendLedger(
        settings.spend_ledger or Path(ref.path) / ".spend_ledger.sqlite",
        max_calls=settings.max_calls_per_day,
        max_usd=settings.max_usd_per_day,
        tz=settings.timezone,
        clock=clock,
    )
    session.meter = LearnerMeter(max_calls=settings.max_calls_per_run, max_usd=run_usd, price_table=price_table, pricing_key=pricing_key, ledger=ledger)
    session.proposer = LlmProposer(role, transport)
    return session
