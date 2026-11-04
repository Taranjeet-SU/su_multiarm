"""One fallback run over sealed log partitions (addendum A2 rev 2 §4.2.3, §4.2.8).

``FallbackSession.run(store, keys, library)``:

1. For each partition key: the newest sealed version ``v``. A version parsed under another library
   identity than ``library`` is ``stale_partition`` (``apply_decisions``/compose reparse it first).
   An overlay whose identity matches and that deferred nothing is ``current`` and skipped.
2. **Residue** from the stored units (never the capped ``unparsed.jsonl.gz`` sample): heads sealed
   ``format_only``/``fallback_pending``/``not_attempted`` and field gaps (``field_gap``). Each is
   re-matched with ``library`` for its spans. Up to ``held_out_per_pattern`` parsed bodies per
   approved message pattern are the gate's held-out lines.
3. **Queue cap**: at most ``max_residue_lines`` heads (newest partition, then level weight, then
   id); the rest are ``budget_deferred`` and reported.
4. **Egress** per line (N6): a head whose stored text the configured redaction would still change,
   or that shows a PII shape, is ``egress_refused`` and never sent; above
   ``egress.max_refused_share`` the run is refused (``EgressRefused``). Prompt text is pseudonymised.
5. **Tiers**: cluster (``slots.py``), field-gap fill (``fill.py``), per-line tail (``lines.py``).
6. **Overlay** per partition (``ingestion/overlay.py``): per-unit ``HeadAccount`` updates (every
   value re-verified: ``parse(line[start:end]) == value`` and its validator), identity H re-checked,
   then the manifest. A crash leaves the heads ``fallback_pending``; the next run rebuilds, mostly
   from the reply cache.

``allow_calls=False`` rebuilds overlays from the cache only (misses stay ``fallback_pending``).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ...core.config_learning import role_routes
from ...core.errors import ConfigError
from ...ingestion.accounting import AccountingPolicy, HeadAccount, account_unit, coverage_report, pattern_fields, verified
from ...ingestion.overlay import OverlayManifest, OverlayStore, fallback_identity, proposed_identity
from ..patterns.bootstrap import LogLine
from ..patterns.egress import EgressPolicy, LineRedactor
from ..patterns.loop import body_span
from .cache import ReplyCache, sha
from .common import Caller, Head, HttpProbe, Outcome, Route
from .fill import fill_tier
from .lines import extract_lines
from .replies import PROMPT_VERSION, SCHEMA_VERSION
from .slots import TierContext, cluster_tier

RESIDUE = {"format_only", "fallback_pending", "not_attempted"}
DEFERRED = {"budget_deferred", "capacity_deferred", "below_rank", "fallback_pending", "provider_error"}


@dataclass
class Partition:
    key: str
    version: int
    identity: str
    units: dict[str, Any]  # unit id -> UnitArtifacts
    rows: dict[str, dict[str, Any]]  # head id -> sealed match row
    heads: list[Head]


def _attributes(record: Any) -> dict[str, str]:
    from ...ingestion.logs_parse import _attributes as parse_attributes

    return parse_attributes(record)


class FallbackSession:
    """Everything a fallback run needs, built from a v2 ``Config`` (``from_config``)."""

    def __init__(
        self,
        config: Any,
        *,
        cluster_routes: list[Route],
        line_routes: list[Route],
        cache: ReplyCache,
        redactor: LineRedactor,
        egress: EgressPolicy,
        load: Callable[[], Any],
        probe: Any = None,
        clock: Callable[[], datetime] | None = None,
        stop_file: Path | None = None,
    ):
        self.config = config
        self.settings = config.learning.extraction
        self.cluster_routes, self.line_routes = cluster_routes, line_routes
        self.cache, self.redactor, self.egress, self.load = cache, redactor, egress, load
        self.probe = probe or HttpProbe()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = AccountingPolicy.from_config(config)
        self.stop_file: Path | None = stop_file
        self._term = False
        self.state_dir: Path = Path(stop_file).parent if stop_file is not None else Path(".")
        self.throughput_path = self.state_dir / "throughput.json"
        self.bootstrap_path = self.state_dir / ".bootstrap.json"

    def bootstrap(self, now: datetime) -> dict[str, Any]:
        """``learning.bootstrap`` for this run: ``{"active": False}`` or the cap and why. The first
        enabled run records its date; bootstrap lasts ``days`` calendar days (UTC) from it. An
        unmeasured bootstrap under ``require_measured_throughput`` refuses the run (``ConfigError``)."""
        import json as _json

        from .throughput import bootstrap_cap, read_measurements

        settings = self.config.learning.bootstrap
        if not settings.enabled:
            return {"active": False}
        started = None
        if self.bootstrap_path.exists():
            started = date.fromisoformat(_json.loads(self.bootstrap_path.read_text())["started"])
        today = now.astimezone(timezone.utc).date()
        if started is not None and (today - started).days >= settings.days:
            return {"active": False, "ended": (started + timedelta(days=settings.days)).isoformat()}
        cap, why = bootstrap_cap(read_measurements(self.throughput_path), settings, [r.tier for r in self.cluster_routes])
        if cap is None:
            raise ConfigError(f"learning.bootstrap refused: {why}", "learning.bootstrap.require_measured_throughput")
        if started is None:
            started = today
            self.bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
            self.bootstrap_path.write_text(_json.dumps({"started": started.isoformat()}))
        return {"active": True, "started": started.isoformat(), "day": (today - started).days + 1, "max_clusters": cap, "why": why, "self_hosted_only": True}

    # --- identity --------------------------------------------------------------------------------

    def models(self) -> dict[str, str]:
        return {f"{role}:{r.name}": f"{r.provider}/{r.model_name}" for role, routes in (("pattern_learner", self.cluster_routes), ("log_extractor", self.line_routes)) for r in routes}

    def settings_sha(self) -> str:
        return sha([self.settings.model_dump(mode="json", exclude={"spend_ledger", "reply_cache", "budget"}), self.policy.to_json(), self.config.extraction.pattern_library.gate.model_dump(mode="json")])

    def identity(self, key: str, version: int, library: Any) -> str:
        return fallback_identity(
            partition_key=key,
            version=version,
            library_identity=library.identity,
            proposed=proposed_identity(library.library),
            models=self.models(),
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            settings_sha=self.settings_sha(),
        )

    def status(self, store: Any, key: str, library: Any) -> dict[str, Any]:
        """``current`` | ``deferred`` (current overlay with deferred heads) | ``stale`` | ``missing``
        | ``stale_partition`` | ``unsealed``."""
        sealed = store.get(key)
        if sealed is None:
            return {"status": "unsealed"}
        if sealed.library_identity != library.identity:
            return {"status": "stale_partition", "version": sealed.version}
        identity = self.identity(key, sealed.version, library)
        overlay = OverlayStore(store).read(key, sealed.version, identity)
        if overlay is not None:
            deferred = overlay.report.get("deferred_heads", 0)
            return {"status": "deferred" if deferred else "current", "version": sealed.version, "identity": identity, "deferred_heads": deferred}
        others = OverlayStore(store).identities(key, sealed.version)
        return {"status": "stale" if others else "missing", "version": sealed.version, "identity": identity}

    # --- residue ---------------------------------------------------------------------------------

    def collect(self, store: Any, key: str, version: int, identity: str, library: Any, held_out: dict[str, list[str]]) -> Partition:
        rows = {r["id"]: r for r in store.read(key, "matches.jsonl.gz", version)}
        units: dict[str, Any] = {}
        heads: list[Head] = []
        cap = self.settings.held_out_per_pattern
        for artifacts in store.units(key, version):
            units[artifacts.unit.id] = artifacts
            for record in sorted(artifacts.records, key=lambda r: (r.ts, r.id)):
                row = rows.get(record.id)
                if row is None:
                    continue
                reason = row.get("reason")
                residue = row.get("final_status") == "unresolved" and reason in RESIDUE
                gap = row.get("final_status") == "unresolved" and reason == "field_gap"
                parsed = row.get("final_status") == "parsed" and row.get("pattern_id")
                if parsed and len(held_out.get(row["pattern_id"], [])) >= cap:
                    continue
                if not (residue or gap or parsed):
                    continue
                attributes = _attributes(record)
                result = library.match_line(record.message.split("\n", 1)[0], field_name="message", attributes=attributes)
                span = body_span(result) or (0, len(result.text))
                if parsed:
                    held_out.setdefault(row["pattern_id"], []).append(result.text[span[0] : span[1]])
                    continue
                head = Head(
                    id=record.id,
                    unit_id=artifacts.unit.id,
                    partition=key,
                    text=result.text,
                    body=span,
                    attributes={k: v for k, v in result.attributes.items() if isinstance(v, str)},
                    stage_status=row["status"],
                    reason=reason,
                    gaps=list(row.get("required_gaps") or []),
                    pattern_id=row.get("pattern_id"),
                    signature_id=row.get("signature"),
                    pattern_owner=row.get("account_owner") or "none",
                )
                if gap:
                    head.pattern_values, _ = pattern_fields(result, self.policy.validators)
                heads.append(head)
        return Partition(key, version, identity, units, rows, heads)

    # --- run -------------------------------------------------------------------------------------

    def stop_requested(self) -> bool:
        """A clean stop between calls: the stop file exists or SIGTERM arrived during the run."""
        return self._term or (self.stop_file is not None and Path(self.stop_file).exists())

    def run(
        self, store: Any, keys: list[str], library: Any, *, allow_calls: bool = True, now: datetime | None = None, progress: Callable[[dict[str, Any]], None] | None = None
    ) -> dict[str, Any]:
        """One run; ``progress(report)`` receives the report so far after every cluster and stage
        (the caller persists it, so a stopped run keeps its report)."""
        import signal
        import threading

        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_term", True))
        try:
            return self._run(store, keys, library, allow_calls=allow_calls, now=now, progress=progress)
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
            self._term = False

    def _run(self, store: Any, keys: list[str], library: Any, *, allow_calls: bool, now: datetime | None, progress: Callable[[dict[str, Any]], None] | None) -> dict[str, Any]:
        now = now or self.clock()
        overlays = OverlayStore(store)
        boot = self.bootstrap(now) if allow_calls else {"active": False}
        report: dict[str, Any] = {"status": "ran", "allow_calls": allow_calls, "library_identity": library.identity, "bootstrap": boot, "partitions": {}}

        def checkpoint(stage: str) -> None:
            report["stage"] = stage
            if progress is not None:
                progress(report)
        held_out: dict[str, list[str]] = {}
        partitions: list[Partition] = []
        for key in keys:
            state = self.status(store, key, library)
            report["partitions"][key] = dict(state)
            if state["status"] in {"current", "unsealed", "stale_partition"}:
                continue
            partitions.append(self.collect(store, key, state["version"], state["identity"], library, held_out))
        if not partitions:
            report["status"] = "nothing_to_do"
            return report
        outcomes: dict[tuple[str, str], Outcome] = {}
        residue = [h for p in partitions for h in p.heads if not h.gaps]
        gaps = [h for p in partitions for h in p.heads if h.gaps]

        def order(h: Head) -> tuple[int, int, str]:
            return (h.rank()[0], -date.fromisoformat(h.partition).toordinal(), h.id)

        residue.sort(key=order)
        cap = self.settings.max_residue_lines
        for head in residue[cap:]:
            outcomes[(head.partition, head.id)] = Outcome("unresolved", "budget_deferred")
        report["residue"] = {"heads": len(residue), "gaps": len(gaps), "over_queue_cap": max(len(residue) - cap, 0)}
        queued = residue[:cap] + gaps
        # egress, per line (N6)
        candidates = []
        for head in queued:
            line = LogLine(head.id, head.text, dict(head.attributes))
            redacted = self.redactor.redact(line)
            candidates.append(line if redacted.text != head.text or redacted.redactions else redacted)
        admitted_lines, refused = self.egress.split(candidates, self.settings.egress.max_refused_share)
        admitted_ids = {line.id for line in admitted_lines}
        for head in queued:
            if head.id in refused:
                outcomes[(head.partition, head.id)] = Outcome("unresolved", "egress_refused", signature_id=head.signature_id, pattern_owner=head.pattern_owner)
        report["egress"] = {"refused": len(refused), "redaction": self.redactor.stamp}
        pseudo = self.egress.pseudonymiser()
        admitted = [h for h in queued if h.id in admitted_ids]
        for head in admitted:
            pseudo.attributes(head.attributes)
        paid_ok = self.config.models.routes.allow_paid_fallback
        common = dict(cache=self.cache, now=now, allow_calls=allow_calls, allow_paid_fallback=paid_ok, probe=self.probe, stop=self.stop_requested)
        cluster_caller = Caller(routes=self.cluster_routes, stage="pattern_learner", self_hosted_only=bool(boot.get("self_hosted_only")), **common) if self.cluster_routes else None  # type: ignore[arg-type]
        line_caller = Caller(routes=self.line_routes, stage="log_extractor", **common) if self.line_routes else None  # type: ignore[arg-type]
        checkpoint("egress")
        ctx = TierContext(
            compiled=library,
            ref=self.config.extraction.pattern_library,
            settings=self.settings,
            validators=self.policy.validators,
            caller=cluster_caller if self.settings.template.enabled else None,
            pseudo=pseudo,
            held_out=held_out,
            nearest=self.config.learning.patterns.nearest_patterns if self.config.learning.patterns else 0,
            now=now,
            load=self.load,
            residue=[h for h in admitted if not h.gaps],
            max_clusters=boot.get("max_clusters"),
            progress=(lambda: (report.update(clusters=ctx.clusters, proposals=ctx.proposals), checkpoint("clusters"))) if progress is not None else None,
        )
        admitted_residue = [h for h in admitted if not h.gaps]
        admitted_gaps = [h for h in admitted if h.gaps]
        tail = admitted_residue
        if ctx.caller is not None:
            cluster_out, tail = cluster_tier(admitted_residue, ctx)
            outcomes.update({(by.partition, by.id): cluster_out[by.id] for by in admitted_residue if by.id in cluster_out})
        fill_report: dict[str, Any] = {}
        if admitted_gaps:
            fill_out = fill_tier(admitted_gaps, ctx, line_caller if self.settings.fill.enabled else None, fill_report)
            outcomes.update({(h.partition, h.id): fill_out[h.id] for h in admitted_gaps})
        tail_report = self._tail(tail, line_caller, pseudo, outcomes)
        report.update(
            clusters=ctx.clusters,
            proposals=ctx.proposals,
            fill=fill_report,
            tail=tail_report,
            calls={"pattern_learner": cluster_caller.report() if cluster_caller else None, "log_extractor": line_caller.report() if line_caller else None},
        )
        report["backlog"] = {
            "deferred_clusters": sum(1 for c in ctx.clusters if c.get("outcome") in DEFERRED),
            "deferred_heads": sum(1 for o in outcomes.values() if o.reason in DEFERRED),
        }
        checkpoint("tiers")
        for partition in partitions:
            report["partitions"][partition.key].update(self._write(overlays, partition, outcomes, report, now))
            checkpoint(f"overlay:{partition.key}")
        report["state"] = {k: (v or {}).get("state") for k, v in report["calls"].items() if v}
        checkpoint("done")
        return report

    def _tail(self, tail: list[Head], caller: Caller | None, pseudo: Any, outcomes: dict[tuple[str, str], Outcome]) -> dict[str, Any]:
        settings = self.settings.lines
        if not settings.enabled or caller is None:
            for head in tail:
                outcomes[(head.partition, head.id)] = Outcome("unresolved", "no_cluster")
            return {"enabled": False, "heads": len(tail)}
        long = [h for h in tail if len(h.text) > settings.max_line_chars]
        short = sorted((h for h in tail if len(h.text) <= settings.max_line_chars), key=lambda h: h.rank())
        sent, over = short[: settings.max_per_day], short[settings.max_per_day :]
        for head in long:
            outcomes[(head.partition, head.id)] = Outcome("unresolved", "no_cluster")
        for head in over:
            outcomes[(head.partition, head.id)] = Outcome("unresolved", "budget_deferred")
        fields = dict(self.settings.fields)
        results = extract_lines(sent, lambda _h: fields, caller=caller, pseudo=pseudo, batch_size=settings.batch_size, levels=list(self.settings.grounding.levels), validators=self.policy.validators, source="model_line")
        for head in sent:
            values, reason = results[head.id]
            outcomes[(head.partition, head.id)] = Outcome("model_line", None, values, None, "none") if values else Outcome("unresolved", reason)
        return {"enabled": True, "heads": len(tail), "sent": len(sent), "over_cap": len(over), "too_long": len(long)}

    def _write(self, overlays: OverlayStore, partition: Partition, outcomes: dict[tuple[str, str], Outcome], report: dict[str, Any], now: datetime) -> dict[str, Any]:
        by_unit: dict[str, list[HeadAccount]] = {u: [] for u in partition.units}
        final_rows: dict[str, dict[str, Any]] = {}
        dropped = 0
        texts = {h.id: h.text for h in partition.heads}
        for head in partition.heads:
            outcome = outcomes.get((partition.key, head.id))
            if outcome is None:  # a head no tier reached (should not happen): never silently resolved
                outcome = Outcome("unresolved", "fallback_pending")
            fields = []
            for value in outcome.fields:
                if verified(value, texts[head.id], self.policy.validators):
                    fields.append(value)
                else:
                    dropped += 1
            status, reason = outcome.final_status, outcome.reason
            if status in {"model_line", "parsed_filled"} and not any(f.source != "pattern" for f in fields):
                status, reason = "unresolved", "grounding_failed"
            account = HeadAccount(
                head_id=head.id,
                unit_id=head.unit_id,
                final_status=status,  # type: ignore[arg-type]
                reason=reason if status == "unresolved" else None,  # type: ignore[arg-type]
                stage_status=head.stage_status,
                signature_id=outcome.signature_id or head.signature_id,
                pattern_owner=outcome.pattern_owner if outcome.pattern_owner != "none" else head.pattern_owner,  # type: ignore[arg-type]
                extraction_source="model_assisted" if status in {"parsed_filled", "provisional", "model_line"} else None,
                fields=fields,
                gaps=head.gaps,
            )
            by_unit[head.unit_id].append(account)
            final_rows[head.id] = {"final_status": account.final_status, "reason": account.reason, "account_owner": account.pattern_owner}
        accounting = {}
        for unit_id, artifacts in partition.units.items():
            rows = [{**partition.rows[r.id], **final_rows.get(r.id, {})} for r in artifacts.records if r.id in partition.rows]
            account = account_unit(artifacts, rows)
            account.check()  # identity H again before the overlay is written
            accounting[unit_id] = account
            overlays.write_unit(partition.key, partition.version, partition.identity, unit_id, by_unit[unit_id])
        statuses = Counter(a.final_status for heads in by_unit.values() for a in heads)
        reasons = Counter(a.reason for heads in by_unit.values() for a in heads if a.reason)
        deferred = sum(n for r, n in reasons.items() if r in DEFERRED)
        regex_only = [account_unit(a, [partition.rows[r.id] for r in a.records if r.id in partition.rows]) for a in partition.units.values()]
        coverage = coverage_report(regex_only, list(accounting.values()), getattr(getattr(self.config.logs, "accounting", None), "targets", None))
        manifest = OverlayManifest(
            identity=partition.identity,
            partition_key=partition.key,
            version=partition.version,
            library_identity=report.get("library_identity", ""),
            units=sorted(partition.units),
            accounting=accounting,
            report={
                "heads": len(partition.heads),
                "by_status": dict(sorted(statuses.items())),
                "by_reason": dict(sorted(reasons.items())),
                "deferred_heads": deferred,
                "dropped_values": dropped,
                "coverage": coverage,
            },
            created_at=now,
        )
        overlays.seal(manifest)
        return {"status": "written", "identity": partition.identity, "version": partition.version, "by_status": manifest.report["by_status"], "by_reason": manifest.report["by_reason"], "deferred_heads": deferred, "coverage": coverage}

    # --- construction ----------------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any, *, load: Callable[[], Any] | None = None, transport: Any = None, transports: dict[str, Any] | None = None, probe: Any = None, clock: Callable[[], datetime] | None = None) -> "FallbackSession":
        """Routes, meters (local routes at USD 0; paid routes under the scope day caps and the
        provider/scope total caps), the reply cache, the redactor and the egress policy. ``transport``
        / ``transports`` (by route name) are for tests and the Python API only."""
        from ...providers.json_model import JsonModel
        from ...runtime.budget import load_price_table
        from ..patterns.spend import DaySpendLedger, LearnerMeter, TotalSpendLedger

        settings = config.learning.extraction
        if settings is None or not settings.enabled:
            raise ConfigError("learning.extraction is not enabled", "learning.extraction")
        patterns = config.learning.patterns
        ledger_path = Path(settings.spend_ledger or (patterns.spend_ledger if patterns and patterns.spend_ledger else None) or Path(config.logs.partitions.path) / ".spend_ledger.sqlite")
        cache = ReplyCache(settings.reply_cache or ledger_path.parent / ".fallback_replies.sqlite")
        prices = load_price_table(config.budgets.price_table) if config.budgets.price_table else None
        transports = dict(transports or {})

        def meter(name: str, route: Any) -> Any:
            paid = route.paid
            if name == "pattern_learner":
                assert patterns is not None
                calls_day, calls_run, usd_day, usd_run, tz, scope = patterns.max_calls_per_day, patterns.max_calls_per_day, patterns.max_usd_per_day, patterns.max_usd_per_run, patterns.timezone, "learning.patterns"
                scope_total = None
            else:
                budget = settings.budget
                calls_day, calls_run, usd_day, usd_run, tz, scope = budget.max_calls_per_day, budget.max_calls_per_run, budget.max_usd_per_day, budget.max_usd_per_run, budget.timezone, "learning.extraction"
                scope_total = budget.max_usd_total
            totals = []
            if paid:
                provider_cap = config.budgets.providers.get(route.role.provider)
                if provider_cap is None:
                    raise ConfigError(f"paid route {route.name!r} needs budgets.providers.{route.role.provider}.max_usd_total", f"models.routes.{name}")
                totals.append(TotalSpendLedger(ledger_path, key=f"provider:{route.role.provider}", max_usd=provider_cap.max_usd_total, tz=tz, clock=clock))
                if scope_total is not None:
                    totals.append(TotalSpendLedger(ledger_path, key=f"scope:{scope}", max_usd=scope_total, tz=tz, clock=clock))
            day = DaySpendLedger(ledger_path, max_calls=calls_day, max_usd=usd_day if paid else None, tz=tz, scope=scope, role=name, clock=clock)
            return LearnerMeter(
                max_calls=calls_run,
                max_usd=usd_run if paid else None,
                price_table=prices if paid else None,
                pricing_key=route.role.price_key() if paid else None,
                ledger=day,
                role=name,
                totals=totals,
            )

        def routes(name: str, needed: bool) -> list[Route]:
            if not needed:
                return []
            out = []
            for route in role_routes(config, name):
                model = JsonModel(route.role, transports.get(route.name, transports.get(name, transport)))
                source = route.source
                out.append(
                    Route(
                        route.name,
                        route.role,
                        model,
                        meter(name, route),
                        route.paid,
                        route.health_url,
                        route.capacity_url,
                        source.probe_timeout_seconds if source is not None else 3.0,
                        tier_name=route.tier,
                        windows=list(source.windows) if source is not None else [],
                        max_invalid_replies=source.max_invalid_replies if source is not None else 3,
                    )
                )
            return out

        line_calls = settings.lines.enabled or settings.fill.enabled
        redactor = LineRedactor(config.privacy)
        egress = EgressPolicy(settings.egress, redactor)
        cluster_routes = routes("pattern_learner", settings.template.enabled)
        line_routes = routes("log_extractor", line_calls)
        for route in cluster_routes:
            _check_route(route, patterns.egress if patterns else settings.egress, redactor, "pattern_learner", "learning.patterns.egress")
        for route in line_routes:
            _check_route(route, settings.egress, redactor, "log_extractor", "learning.extraction.egress")
        if load is None:
            from ...extraction.patterns.extractor import PatternExtractor

            ref = config.extraction.pattern_library

            def load() -> Any:
                return PatternExtractor(ref, None).load()

        stop_file = settings.stop_file or ledger_path.parent / ".fallback.stop"
        return cls(config, cluster_routes=cluster_routes, line_routes=line_routes, cache=cache, redactor=redactor, egress=egress, load=load, probe=probe, clock=clock, stop_file=stop_file)


def _check_route(route: Route, settings: Any, redactor: Any, name: str, path: str) -> None:
    """Egress per tier: ``local`` is always allowed, ``lan`` needs ``allow_lan``, ``paid`` needs
    ``allow_hosted`` (``EgressRefused`` before any call)."""
    from ..patterns.egress import EgressRefused

    if route.tier == "lan":
        if not settings.allow_lan:
            raise EgressRefused(f"the {name} route {route.name!r} is a lan model and {path}.allow_lan is false")
        return
    if route.tier == "paid":
        EgressPolicy(settings, redactor).check_role(route.role, name, f"{path}.allow_hosted")


def summary(report: dict[str, Any]) -> str:
    """One content-free line for logs."""
    return json.dumps({k: report.get(k) for k in ("status", "backlog")}, sort_keys=True)
