"""Measured throughput per route tier (addendum A2 rev 4; ``patterns measure-throughput``).

``measure`` sends up to ``clusters`` real cluster calls (the top residue clusters of one sealed day,
ranked as the fallback ranks them) to the routes of **one tier**, under the same egress policy,
probes and spend caps as a run, and records calls/hour, tokens/s and the invalid-reply count in
``throughput.json`` next to the spend ledger. Replies are stored in the reply cache, so the
measurement is not wasted: the next fallback run reuses them. Nothing is gated, applied or
written to the library.

``learning.bootstrap.require_measured_throughput`` reads this record: bootstrap refuses to start
without a measurement of a self-hosted tier, and its daily cluster cap is bounded by the measured
calls/hour over ``bootstrap.hours_per_day``.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ...core.errors import BudgetExhausted, ConfigError
from ...providers.base import ProviderError, SchemaViolation
from ..patterns.mining import TemplateMiner
from ..patterns.synthesis import synthesise
from .cache import cluster_key, template_sha
from .common import CachePending, Caller
from .replies import CLUSTER_PROMPT, ClusterExtractionReply
from .slots import TierContext, _payload, _rank

FILE = "throughput.json"
SELF_HOSTED = ("local", "lan")


def read_measurements(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def bootstrap_cap(measurements: dict[str, Any], bootstrap: Any, tiers: list[str]) -> tuple[int | None, str]:
    """``(daily cluster cap, why)``; ``None`` when bootstrap may not start (unmeasured)."""
    measured = [measurements[t] for t in tiers if t in SELF_HOSTED and t in measurements and measurements[t].get("calls_per_hour")]
    if not measured:
        if bootstrap.require_measured_throughput:
            return None, "no measured throughput for a self-hosted tier (run `su-multi-arm patterns measure-throughput --tier local`)"
        return bootstrap.max_clusters_per_day, "configured cap (throughput not measured)"
    calls = sum(m["calls_per_hour"] for m in measured) * bootstrap.hours_per_day
    cap = min(bootstrap.max_clusters_per_day, max(1, math.floor(calls)))
    return cap, f"min(max_clusters_per_day {bootstrap.max_clusters_per_day}, measured {calls:.0f} calls/day over {bootstrap.hours_per_day} h)"


def measure(session: Any, store: Any, key: str, library: Any, *, tier: str, clusters: int = 50, now: datetime | None = None) -> dict[str, Any]:
    now = now or session.clock()
    routes = [r for r in session.cluster_routes if r.tier == tier]
    if not routes:
        raise ConfigError(f"no pattern_learner route of tier {tier!r}", "models.routes.pattern_learner")
    state = session.status(store, key, library)
    if state["status"] in {"unsealed", "stale_partition"}:
        raise ConfigError(f"partition {key} is {state['status']}; measure on a sealed, current day", "logs")
    partition = session.collect(store, key, state["version"], state["identity"], library, {})
    residue = [h for h in partition.heads if not h.gaps]
    candidates = []
    from ..patterns.bootstrap import LogLine

    for head in residue:
        line = LogLine(head.id, head.text, dict(head.attributes))
        redacted = session.redactor.redact(line)
        candidates.append(line if redacted.text != head.text or redacted.redactions else redacted)
    admitted, refused = session.egress.split(candidates, session.settings.egress.max_refused_share)
    ids = {line.id for line in admitted}
    heads = [h for h in residue if h.id in ids]
    pseudo = session.egress.pseudonymiser()
    for head in heads:
        pseudo.attributes(head.attributes)
    miner = TemplateMiner(library.engine, library.library.masks)
    members: dict[int, list[Any]] = {}
    for head in sorted(heads, key=lambda h: h.id):
        cluster = miner.add(head.id, head.body_text, head.service, head.level)
        if cluster is not None:
            members.setdefault(cluster.seq, []).append(head)
    ranked = sorted((c for c in miner.result() if c.size >= session.settings.template.min_support), key=_rank)[:clusters]
    caller = Caller(routes=routes, cache=session.cache, stage="pattern_learner", now=now, allow_paid_fallback=True, probe=session.probe, stop=session.stop_requested)
    ctx = TierContext(compiled=library, ref=session.config.extraction.pattern_library, settings=session.settings, validators=session.policy.validators, caller=caller, pseudo=pseudo, held_out={}, nearest=0, now=now, load=session.load)
    sent = errors = 0
    stopped = None
    started = time.perf_counter()
    for cluster in ranked:
        rows = members.get(cluster.seq, [])
        payload = _payload(cluster, rows, synthesise(cluster, library.library.masks, library.engine, created=now), ctx)
        try:
            reply, route = caller.fresh(CLUSTER_PROMPT, payload, ClusterExtractionReply)
        except (CachePending, BudgetExhausted) as exc:
            stopped = getattr(exc, "reason", None) or "budget_deferred"
            break
        except (SchemaViolation, ProviderError):
            errors += 1
            continue
        sent += 1
        template = template_sha(cluster.tokens)
        caller.store(cluster_key(template, cluster.service, cluster.level, route.provider, route.model_name), "cluster", reply, [key], route)
    elapsed = time.perf_counter() - started
    tokens = sum(r.tokens for r in routes)
    result = {
        "tier": tier,
        "partition": key,
        "clusters_ranked": len(ranked),
        "calls": sent,
        "errors": errors,
        "egress_refused": len(refused),
        "seconds": round(elapsed, 3),
        "calls_per_hour": round(3600 * sent / elapsed, 1) if sent and elapsed > 0 else None,
        "tokens_per_s": round(tokens / elapsed, 2) if tokens and elapsed > 0 else None,
        "stopped": stopped,
        "routes": [r.report() for r in routes],
        "measured_at": now.isoformat(),
    }
    path = session.throughput_path
    record = read_measurements(path)
    if result["calls_per_hour"] is not None:  # an empty or failed measurement never replaces a real one
        record[tier] = {k: result[k] for k in ("calls_per_hour", "tokens_per_s", "calls", "errors", "measured_at", "partition")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True))
    result["recorded"] = str(path) if result["calls_per_hour"] is not None else None
    return result
