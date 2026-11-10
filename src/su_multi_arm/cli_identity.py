"""``su-multi-arm identity`` (docs/plans/unified_merge.md §6.9, §10.7; lane LD from M2b-1).

Config-only commands (no engine, no backend). The registry is ``--registry`` or
``crosssource.identity.registry_path``; it is private and lives outside the repository.

* ``describe`` — the configured registry path and whether its sha256 equals the pinned digest.
* ``candidates`` — add/seed customers, curated aliases and classify the raw values of a namespace
  with the deterministic rules (``--create`` makes a new registry).
* ``decide`` — a coordinator (or owner) decision ``match | exclude | reject`` with evidence.
* ``review`` — the model review of non-exact matches: ``--dry-run`` lists the queue and its worst
  case (no call); a run needs ``--max-usd`` and ``crosssource.links.review`` (shared cumulative
  ceiling with its approval reference, recorded in the review ledger at first use).
* ``report`` — per-namespace counts with denominators, owner and review queues.
* ``export`` — a YAML/JSON export (never overwrites) with its sha256 for pinning; ``import``
  creates a registry from an export or the F0 fixture format.

A mutating command changes the registry file, so its digest no longer equals the pinned one until
the configuration is re-pinned: cross-source joins stay disabled meanwhile (fail closed, §6.12).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from .core.errors import ConfigError
from .crosssource.identity import (
    Evidence,
    IdentityError,
    IdentityRegistry,
    import_registry,
    review_plan,
    run_review,
)
from .crosssource.identity_candidates import Customer
from .crosssource.review_ledger import ReviewLedger, read_usage

COMMANDS = ("describe", "candidates", "decide", "review", "report", "export", "import")
LEDGER_NAME = "review_ledger.sqlite"


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("identity", help="identity registry (schema v3 crosssource.identity): candidates, decide, review, report, export")
    sub = parser.add_subparsers(dest="identity_command", required=True)
    sub.add_parser("describe", help="registry path and digest check (reads only the registry file)")

    def registry_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--registry", type=Path, help="registry file (default: crosssource.identity.registry_path)")
        p.add_argument("--actor", help="free-text actor recorded with the registry version")

    cand = sub.add_parser("candidates", help="add customers / curated aliases and classify raw values deterministically")
    registry_arg(cand)
    cand.add_argument("--create", action="store_true", help="create a new registry at --registry")
    cand.add_argument("--customers", type=Path, help="YAML/JSON list of {customer_id, name}")
    cand.add_argument("--seed-customers", action="store_true", help="owner rule (tenant name = organization name): the values of --namespace become customers")
    cand.add_argument("--curated", type=Path, help="YAML/JSON {aliases: [{namespace, raw, customer_id, evidence}], evidence: [...]} (candidates only)")
    cand.add_argument("--values", type=Path, help="YAML/JSON {namespaces: {<ns>: {values: {raw: count} | [raw], missing_records: [id]}}}")
    cand.add_argument("--documents", type=Path, help="JSONL documents; the raw value is metadata[--field]")
    cand.add_argument("--namespace", help="namespace of --documents (e.g. support.account, logs.tenant)")
    cand.add_argument("--field", help="metadata field of --documents holding the raw value")
    cand.add_argument("--kind", action="append", default=[], help="only documents of this kind (repeatable)")

    decide = sub.add_parser("decide", help="record a coordinator/owner decision with evidence")
    registry_arg(decide)
    decide.add_argument("--namespace", required=True)
    target = decide.add_mutually_exclusive_group(required=True)
    target.add_argument("--raw", help="the verbatim raw value")
    target.add_argument("--missing", action="store_true", help="the namespace's records without a value")
    decide.add_argument("--action", required=True, choices=["match", "exclude", "reject"])
    decide.add_argument("--customer", help="customer id (match only)")
    decide.add_argument("--authority", default="coordinator", choices=["coordinator", "owner"])
    decide.add_argument("--rationale", required=True)
    decide.add_argument("--confidence", type=float, default=0.9)
    decide.add_argument("--evidence-id", action="append", default=[], help="existing evidence id (repeatable)")
    decide.add_argument("--evidence-file", type=Path, help="YAML/JSON list of new evidence entries")
    decide.add_argument("--evidence-new-id", help="id of one inline evidence entry")
    decide.add_argument("--evidence-kind", choices=["web", "source_field", "curated_list"])
    decide.add_argument("--evidence-url")
    decide.add_argument("--evidence-retrieved", help="retrieval date YYYY-MM-DD (web evidence)")
    decide.add_argument("--evidence-quote")
    decide.add_argument("--evidence-field", help="source field name (source_field evidence)")

    review = sub.add_parser("review", help="model review of non-exact matches (shared cumulative ceiling)")
    registry_arg(review)
    review.add_argument("--dry-run", action="store_true", help="list the queue and its worst-case cost; no model call")
    review.add_argument("--max-usd", type=float, help="spend cap of this run (required unless --dry-run)")
    review.add_argument("--limit", type=int, help="review at most this many entries")
    review.add_argument("--ledger", type=Path, help=f"review ledger (default: {LEDGER_NAME} next to the registry)")

    report = sub.add_parser("report", help="per-namespace counts with denominators, owner and review queues")
    registry_arg(report)

    export = sub.add_parser("export", help="write a YAML/JSON export (never overwrites) and print its sha256")
    registry_arg(export)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--format", choices=["yaml", "json"], default="yaml")

    imp = sub.add_parser("import", help="create a registry from an export (or the F0 fixture format)")
    registry_arg(imp)
    imp.add_argument("--from", dest="source", type=Path, required=True)


def run(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    command = args.identity_command
    if command == "describe":
        return describe(config)
    path = _registry_path(args, config)
    if command == "import":
        registry = import_registry(path, _load(args.source), actor=args.actor)
        return {"imported": str(args.source), "registry": str(path), "report": registry.report(), **pin_status(config, path)}
    if command == "candidates":
        return candidates(args, config, path)
    registry = IdentityRegistry(path)
    if command == "decide":
        return {**_decide(args, registry), **pin_status(config, path)}
    if command == "review":
        return review(args, config, registry)
    if command == "report":
        return {**registry.report(), **pin_status(config, path)}
    if command == "export":
        return registry.export(args.out, args.format)
    raise IdentityError(f"unknown identity command {command!r}")


def _registry_path(args: argparse.Namespace, config: Any) -> Path:
    if getattr(args, "registry", None) is not None:
        return Path(args.registry).expanduser().resolve()
    identity = _identity_config(config)
    if identity is None:
        raise ConfigError("no registry: pass --registry or configure crosssource.identity.registry_path (schema v3)", "crosssource.identity")
    return identity.registry_path


def _identity_config(config: Any) -> Any:
    crosssource = getattr(config, "crosssource", None)
    return None if crosssource is None else crosssource.identity


def _load(path: Path) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pin_status(config: Any, path: Path) -> dict[str, Any]:
    """Whether ``path`` is the configured, pinned registry and whether its digest still matches."""
    identity = _identity_config(config)
    out: dict[str, Any] = {"sha256": _sha256(path) if path.is_file() else None}
    if identity is None or Path(identity.registry_path) != path:
        out["pinned"] = "not_configured"
    else:
        out["pinned"] = "match" if out["sha256"] == identity.sha256 else "mismatch"
        if out["pinned"] == "mismatch":
            out["note"] = "the registry differs from crosssource.identity.sha256: cross-source joins stay disabled until it is re-pinned"
    return out


def describe(config: Any) -> dict[str, Any]:
    identity = _identity_config(config)
    if identity is None:
        return {"configured": False}
    from .crosssource.identity import IdentityView

    path = identity.registry_path
    status = "missing" if not path.is_file() else ("match" if _sha256(path) == identity.sha256 else "mismatch")
    # what an engine started on this configuration serves (staging F7): the pinned view the engine
    # opens (``Engine.identity_view``) and the parts that read it
    view = IdentityView.from_config(identity, path)
    crosssource = getattr(config, "crosssource", None)
    consumers = []
    if crosssource is not None and crosssource.planner is not None and crosssource.planner.enabled:
        consumers.append("crosssource.planner")  # customer grounding of the operations half
    if crosssource is not None and crosssource.links is not None and crosssource.links.store_path is not None:
        consumers.append("crosssource.links")  # customer-derived links
    out: dict[str, Any] = {
        "configured": True,
        "registry_path": str(path),
        "sha256": identity.sha256,
        "digest": status,
        "served": bool(view.joins_enabled and consumers),
        "joins_enabled": view.joins_enabled,
        "consumers": consumers,
    }
    if view.joins_enabled:
        out.update(registry_version=view.registry_version, joinable_aliases=len(view.matched), auto_match=view.auto_match,
                   fuzzy_aliases=len(view.promoted), fuzzy_rules=dict(sorted(Counter(view.promoted.values()).items())))
    else:
        out["reason"] = view.reason
    if view.joins_enabled and not consumers:
        out["note"] = "the registry is pinned but nothing reads it (enable crosssource.planner or pin crosssource.links)"
    return out


def declared_field(config: Any, namespace: str, kinds: list[str]) -> str | None:
    """The metadata field holding ``namespace``'s raw values, from the collection's member
    declarations (``field_map.customer_ref`` under ``identity_namespace``; the same names the link
    builders read), or ``None`` when no single field is declared for it."""
    from .crosssource.links.builders import CUSTOMER_FIELDS, collection_fields

    customers, _times = collection_fields(getattr(config, "collection", None))
    merged = {**CUSTOMER_FIELDS, **customers}
    found = {spec[1] for kind, spec in merged.items() if spec is not None and spec[0] == namespace and (not kinds or kind in kinds)}
    return found.pop() if len(found) == 1 else None


def _document_values(args: argparse.Namespace, config: Any = None) -> tuple[dict[str | None, int], list[str]]:
    if not args.namespace:
        raise IdentityError("--documents needs --namespace (and --field unless the collection declares the namespace's field)")
    if not args.field:
        args.field = declared_field(config, args.namespace, list(args.kind))
        if not args.field:
            raise IdentityError(f"--documents needs --field: no single field is declared for {args.namespace} (collection.members.*.field_map.customer_ref)")
    counts: dict[str | None, int] = {}
    missing: list[str] = []
    with Path(args.documents).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            doc = json.loads(line)
            if args.kind and doc.get("kind") not in args.kind:
                continue
            value = (doc.get("metadata") or {}).get(args.field)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(str(doc["id"]))
                continue
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts, missing


def candidates(args: argparse.Namespace, config: Any, path: Path) -> dict[str, Any]:
    if args.create:
        registry = IdentityRegistry.create(path)
    else:
        registry = IdentityRegistry(path)
    steps: list[dict[str, Any]] = []
    if args.customers is not None:
        steps.append(registry.add_customers([Customer(str(c["customer_id"]), str(c["name"])) for c in _load(args.customers)], actor=args.actor))
    batches: list[tuple[str, dict[str | None, int | None] | list[str | None], list[str]]] = []
    if args.documents is not None:
        counts, missing = _document_values(args, config)
        batches.append((args.namespace, counts, missing))
    if args.values is not None:
        data = _load(args.values) or {}
        for ns, entry in sorted((data.get("namespaces") or {}).items()):
            values = entry.get("values") or []
            batches.append((ns, dict(values) if isinstance(values, dict) else list(values), list(entry.get("missing_records") or [])))
    if args.seed_customers:
        if not batches or not args.namespace:
            raise IdentityError("--seed-customers needs --namespace and its values (--documents or --values)")
        names = [str(v) for ns, values, _ in batches if ns == args.namespace for v in values if v is not None]
        steps.append(registry.seed_customers(names, actor=args.actor))
    if args.curated is not None:
        data = _load(args.curated) or {}
        steps.append(registry.add_curated(data.get("aliases") or [], [Evidence.model_validate(e) for e in data.get("evidence") or []], actor=args.actor))
    for ns, values, missing in batches:
        steps.append(registry.observe(ns, values, missing_records=missing, actor=args.actor))
    if not steps and not args.create:
        raise IdentityError("nothing to do: pass --customers, --curated, --values or --documents")
    report = registry.report()
    return {"registry": str(path), "steps": steps, "registry_version": report["registry_version"], "namespaces": report["namespaces"], **pin_status(config, path)}


def _decide(args: argparse.Namespace, registry: IdentityRegistry) -> dict[str, Any]:
    evidence: list[Evidence | str] = list(args.evidence_id)
    if args.evidence_file is not None:
        evidence += [Evidence.model_validate(e) for e in _load(args.evidence_file) or []]
    if args.evidence_kind is not None:
        if not args.evidence_new_id:
            raise IdentityError("inline evidence needs --evidence-new-id")
        evidence.append(Evidence.model_validate({"id": args.evidence_new_id, "kind": args.evidence_kind, "url": args.evidence_url,
                                                 "retrieved": args.evidence_retrieved, "quote": args.evidence_quote or "", "field": args.evidence_field}))
    return registry.decide(args.namespace, None if args.missing else args.raw, args.action, evidence=evidence, rationale=args.rationale,
                           customer_id=args.customer, authority=args.authority, confidence=args.confidence, actor=args.actor)


def provider_for(role: Any) -> Any:
    """The chat provider of the review role (tests replace this; config can never name code)."""
    from .providers.json_model import chat_provider

    return chat_provider(role)


def _review_settings(config: Any) -> tuple[Any, Any, Any]:
    links = getattr(getattr(config, "crosssource", None), "links", None)
    review_cfg = None if links is None else links.review
    role = None if review_cfg is None else config.models.roles.chat_roles().get(review_cfg.role)
    table = None
    if config.budgets.price_table is not None:
        from .runtime.budget import load_price_table

        table = load_price_table(config.budgets.price_table)
    return review_cfg, role, table


def review(args: argparse.Namespace, config: Any, registry: IdentityRegistry) -> dict[str, Any]:
    review_cfg, role, table = _review_settings(config)
    ledger_path = args.ledger.expanduser().resolve() if args.ledger is not None else registry.path.with_name(LEDGER_NAME)
    pricing_key = None if role is None else role.price_key()
    max_tokens = 0 if role is None else role.max_tokens
    if args.dry_run:
        plan = review_plan(registry, price_table=table, pricing_key=pricing_key, max_tokens=max_tokens, limit=args.limit)
        refusals = []
        if review_cfg is None:
            refusals.append("crosssource.links.review is not configured (shared ceiling and approval reference)")
        elif role is None:
            refusals.append(f"review role {review_cfg.role!r} is not configured")
        if not plan["priced"]:
            refusals.append("budgets.price_table does not price the review role")
        return {"dry_run": True, "role": None if review_cfg is None else review_cfg.role, "model": None if role is None else role.model, "pricing_key": pricing_key,
                "ledger": str(ledger_path), "ledger_usage": read_usage(ledger_path, None if review_cfg is None else review_cfg.usd_ceiling),
                "run_refused_because": refusals, **plan}
    if review_cfg is None:
        raise ConfigError("identity review needs crosssource.links.review (the shared cumulative ceiling and its approval_ref)", "crosssource.links.review")
    if role is None:
        raise ConfigError(f"review role {review_cfg.role!r} is not configured", "crosssource.links.review.role")
    if table is None or pricing_key not in table.prices:
        raise ConfigError(f"identity review needs budgets.price_table with a price for {pricing_key!r}", "budgets.price_table")
    if args.max_usd is None:
        raise IdentityError("identity review needs --max-usd (the spend cap of this run) or --dry-run")
    ledger = ReviewLedger(ledger_path, usd_ceiling=review_cfg.usd_ceiling, approval_ref=review_cfg.approval_ref)
    result = run_review(registry, provider_for(role), ledger, table, pricing_key=pricing_key, max_tokens=role.max_tokens, max_usd=args.max_usd,
                        limit=args.limit, actor=args.actor)
    return {"dry_run": False, "role": review_cfg.role, "model": role.model, "ledger_path": str(ledger_path), **result, **pin_status(config, registry.path)}
