"""``su-multi-arm links`` (docs/plans/unified_merge.md §6.9, §10.7; lane LD from M3).

Config-only commands (no engine, no backend). The link store is ``--store`` (private, outside the
repository); the pinned documents are JSONL exports passed with ``--documents`` in pin order (a
later file's version of an id wins). Identity comes only from the pinned registry
(``crosssource.identity`` or ``--registry`` + ``--registry-sha256``) through ``IdentityView``.

* ``build`` — tier A/B links over the documents (``--create`` makes a new store).
* ``review`` — model review of candidate links: ``--dry-run`` lists the queue and its worst case
  (no call); a run needs ``--max-usd`` and ``crosssource.links.review``. The ledger is the **same**
  file as identity review (``review_ledger.sqlite`` next to the identity registry, or ``--ledger``):
  one cumulative ceiling.
* ``decide`` — a coordinator/owner ``accept | reject`` of one link.
* ``report`` — counts by status/pair/tier, display classes, retractions, queues.
* ``show`` — the display data of one record through the pinned ``LinkView`` (labelled candidates).
* ``export`` — a YAML/JSON export (never overwrites) with its sha256 for pinning.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .cli_identity import LEDGER_NAME, _review_settings
from .core.errors import ConfigError
from .core.types import Document
from .crosssource.identity import IdentityView, sha256_file
from .crosssource.links.builders import LinkSettings, build_links, pinned_documents
from .crosssource.links.review import review_plan, run_review
from .crosssource.links.store import LinkStore, LinkStoreError, LinkView
from .crosssource.review_ledger import ReviewLedger, read_usage

COMMANDS = ("build", "review", "decide", "report", "show", "export")


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("links", help="cross-source links (schema v3 crosssource.links): build, review, decide, report, show, export")
    sub = parser.add_subparsers(dest="links_command", required=True)

    def store_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--store", type=Path, required=True, help="link store file (private; outside the repository)")
        p.add_argument("--actor", help="free-text actor recorded with the link version")

    def documents_arg(p: argparse.ArgumentParser, required: bool) -> None:
        p.add_argument("--documents", type=Path, action="append", default=[], required=required,
                       help="JSONL documents of the pinned collection, in pin order (repeatable; a later version of an id wins)")

    def identity_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--registry", type=Path, help="identity registry (default: crosssource.identity.registry_path)")
        p.add_argument("--registry-sha256", help="pinned digest of --registry (default: crosssource.identity.sha256)")

    build = sub.add_parser("build", help="tier A/B links over the pinned documents")
    store_arg(build)
    documents_arg(build, True)
    identity_arg(build)
    build.add_argument("--create", action="store_true", help="create a new link store at --store")

    review = sub.add_parser("review", help="model review of candidate links (shared cumulative ceiling)")
    store_arg(review)
    documents_arg(review, True)
    review.add_argument("--dry-run", action="store_true", help="list the queue and its worst-case cost; no model call")
    review.add_argument("--max-usd", type=float, help="spend cap of this run (required unless --dry-run)")
    review.add_argument("--limit", type=int, help="review at most this many links")
    review.add_argument("--ledger", type=Path, help=f"review ledger (default: {LEDGER_NAME} next to the identity registry, shared with identity review)")

    decide = sub.add_parser("decide", help="coordinator/owner decision on one link")
    store_arg(decide)
    decide.add_argument("--link", required=True, help="link id (lnk-...)")
    decide.add_argument("--action", required=True, choices=["accept", "reject"])
    decide.add_argument("--authority", default="coordinator", choices=["coordinator", "owner"])
    decide.add_argument("--rationale", required=True)

    report = sub.add_parser("report", help="counts by status, pair and tier; retractions; queues")
    store_arg(report)

    show = sub.add_parser("show", help="display data of one record through the pinned link view")
    store_arg(show)
    identity_arg(show)
    show.add_argument("--record", required=True, help="record id (ticket:..., logs:incident:..., family:<rule>, a doc URL)")
    show.add_argument("--sha256", help="pinned digest of --store (default: the store's current digest, reported as unpinned)")

    export = sub.add_parser("export", help="write a YAML/JSON export (never overwrites) and print its sha256")
    store_arg(export)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--format", choices=["yaml", "json"], default="yaml")


def run(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    command = args.links_command
    path = Path(args.store).expanduser().resolve()
    if command == "build":
        return build(args, config, path)
    store = LinkStore(path)
    if command == "review":
        return review(args, config, store)
    if command == "decide":
        return {**store.decide(args.link, args.action, rationale=args.rationale, authority=args.authority, actor=args.actor), "sha256": sha256_file(path)}
    if command == "report":
        return {**store.report(), "sha256": sha256_file(path)}
    if command == "show":
        return show(args, config, path)
    if command == "export":
        return store.export(args.out, args.format)
    raise LinkStoreError(f"unknown links command {command!r}")


def _links_config(config: Any) -> Any:
    crosssource = getattr(config, "crosssource", None)
    return None if crosssource is None else crosssource.links


def _identity(args: argparse.Namespace, config: Any) -> IdentityView:
    identity = getattr(getattr(config, "crosssource", None), "identity", None)
    path = args.registry if args.registry is not None else (None if identity is None else identity.registry_path)
    sha = args.registry_sha256 if args.registry_sha256 is not None else (None if identity is None else identity.sha256)
    if path is None or sha is None:
        return IdentityView(False, "identity_not_configured")
    if identity is not None and args.registry_sha256 is None:
        return IdentityView.from_config(identity, Path(path).expanduser().resolve())
    return IdentityView.pinned(Path(path).expanduser().resolve(), sha)


def _documents(paths: list[Path]) -> list[Document]:
    out: list[Document] = []
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            out.extend(Document.model_validate_json(line) for line in handle if line.strip())
    return out


def build(args: argparse.Namespace, config: Any, path: Path) -> dict[str, Any]:
    links_cfg = _links_config(config)
    identity = _identity(args, config)
    result = build_links(_documents(args.documents), identity, LinkSettings.from_config(links_cfg, collection=getattr(config, "collection", None)))
    store = LinkStore.create(path) if args.create else LinkStore(path)
    summary = store.build(result.links, known_ids=result.known_ids, report=result.report,
                          registry_version=identity.registry_version if identity.joins_enabled else None, actor=args.actor)
    return {"store": str(path), **summary, "report": result.report, "sha256": sha256_file(path)}


def _ledger_path(args: argparse.Namespace, config: Any) -> Path:
    if args.ledger is not None:
        return args.ledger.expanduser().resolve()
    identity = getattr(getattr(config, "crosssource", None), "identity", None)
    if identity is None:
        raise ConfigError("links review shares the identity review ledger: pass --ledger or configure crosssource.identity.registry_path", "crosssource.identity")
    return Path(identity.registry_path).with_name(LEDGER_NAME)


def review(args: argparse.Namespace, config: Any, store: LinkStore) -> dict[str, Any]:
    review_cfg, role, table = _review_settings(config)
    documents, _replaced = pinned_documents(_documents(args.documents))
    pricing_key = None if role is None else role.price_key()
    max_tokens = 0 if role is None else role.max_tokens
    if args.dry_run:
        plan = review_plan(store, documents, price_table=table, pricing_key=pricing_key, max_tokens=max_tokens, limit=args.limit)
        refusals = []
        if review_cfg is None:
            refusals.append("crosssource.links.review is not configured (shared ceiling and approval reference)")
        elif role is None:
            refusals.append(f"review role {review_cfg.role!r} is not configured")
        if not plan["priced"]:
            refusals.append("budgets.price_table does not price the review role")
        try:
            ledger_path: Path | None = _ledger_path(args, config)
        except ConfigError as exc:
            ledger_path = None
            refusals.append(str(exc))
        return {"dry_run": True, "role": None if review_cfg is None else review_cfg.role, "model": None if role is None else role.model, "pricing_key": pricing_key,
                "ledger": None if ledger_path is None else str(ledger_path),
                "ledger_usage": None if ledger_path is None else read_usage(ledger_path, None if review_cfg is None else review_cfg.usd_ceiling),
                "run_refused_because": refusals, **plan}
    if review_cfg is None:
        raise ConfigError("links review needs crosssource.links.review (the shared cumulative ceiling and its approval_ref)", "crosssource.links.review")
    if role is None:
        raise ConfigError(f"review role {review_cfg.role!r} is not configured", "crosssource.links.review.role")
    if table is None or pricing_key not in table.prices:
        raise ConfigError(f"links review needs budgets.price_table with a price for {pricing_key!r}", "budgets.price_table")
    if args.max_usd is None:
        raise LinkStoreError("links review needs --max-usd (the spend cap of this run) or --dry-run")
    ledger = ReviewLedger(_ledger_path(args, config), usd_ceiling=review_cfg.usd_ceiling, approval_ref=review_cfg.approval_ref)
    result = run_review(store, documents, provider_for(role), ledger, table, pricing_key=pricing_key, max_tokens=role.max_tokens, max_usd=args.max_usd,
                        limit=args.limit, actor=args.actor)
    return {"dry_run": False, "role": review_cfg.role, "model": role.model, "ledger_path": str(ledger.path), **result, "sha256": sha256_file(store.path)}


def provider_for(role: Any) -> Any:
    """The chat provider of the review role (tests replace this; config can never name code)."""
    from .providers.json_model import chat_provider

    return chat_provider(role)


def show(args: argparse.Namespace, config: Any, path: Path) -> dict[str, Any]:
    links_cfg = _links_config(config)
    current = sha256_file(path) if path.is_file() else None
    sha = args.sha256 or current or ""
    view = LinkView.pinned(path, sha, identity=_identity(args, config), show_candidates="labelled" if links_cfg is None else links_cfg.show_candidates,
                           max_per_record=5 if links_cfg is None else links_cfg.max_per_record)
    return {"record": args.record, "enabled": view.enabled, "reason": view.reason, "pinned": "explicit" if args.sha256 else "current_file",
            "link_version": view.link_version, "limitations": list(view.limitations), "withheld": view.withheld, "links": view.links_for(args.record)}
