"""``su-multi-arm collection`` (docs/plans/unified_merge.md §10.7; S0 stub, lane LA from M1a).

``collection describe`` prints the validated ``collection`` block (schema v3) without opening any
store, backend or provider; a configuration without it is described as the single-member view.

M1a (partition core):

* ``collection register --member M --key K FILE`` builds one member partition from a canonical
  JSONL/JSON document file with the shared write path and registers its ids (refused on any
  declaration or id-uniqueness violation; nothing is half registered);
* ``collection compose [--unit UNIT ...] [--expected GEN]`` activates a unified generation over the
  named member units (default: every live registered member), by reference, compare-and-set on
  ``--expected``;
* ``collection status`` lists the registered members and the active unified generation.

M1b:

* ``collection adopt --member M`` adopts the store declared by ``collection.members.<M>.adopt``
  **read-only** (``ingestion/adopt_member.py``): the original configuration is checked against its
  pinned sha256, the store's fingerprint is recomputed backend-free and compared with its manifest,
  every backing file is hashed, and the member is recorded in the unified registry. No engine,
  backend or provider is constructed and nothing under the store is written;
* ``collection layers`` prints the digest of every declared attached layer as computed now (the
  value to pin as ``sha256`` for a file layer), without opening any backend.

Staging fixes (2026-11-10): ``collection verify-space --member M`` re-embeds a stratified sample of
the adopted member's chunks and records the embedding-space equivalence its
``adopt.embedding_space`` mapping needs before compose accepts it (``ingestion/space_equivalence.py``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core.errors import ConfigError, SuError
from .runtime.collection import partitioned_refusals, unserved_blocks

PENDING: dict[str, str] = {}


class NotImplementedInRelease(SuError):
    """A declared command whose increment has not landed; nothing was read or written."""


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("collection", help="unified collection (schema v3): describe, register and compose member partitions")
    sub = parser.add_subparsers(dest="collection_command", required=True)
    sub.add_parser("describe", help="print the validated collection block (opens nothing)")
    register = sub.add_parser("register", help="build and register one member partition from a canonical JSONL/JSON document file")
    register.add_argument("input", type=Path, help="canonical documents (JSONL, or a JSON list)")
    register.add_argument("--member", required=True, help="declared member id (collection.members.<id>)")
    register.add_argument("--key", required=True, help="member key: snapshot:YYYY-MM-DD, day:YYYY-MM-DD, month:YYYY-MM or catalog")
    register.add_argument("--replaces", action="append", default=[], help="registered unit id whose overlapping ids this member supersedes (repeatable)")
    register.add_argument("--keyword-epoch", default=None, help="keyword-policy epoch of the member's content (log members)")
    compose = sub.add_parser("compose", help="activate a unified generation over member units, by reference")
    compose.add_argument("--unit", action="append", default=None, help="member unit id <member>/<key> (repeatable; default: every live member)")
    compose.add_argument("--expected", default=None, help="generation the composition starts from (compare-and-set); 'none' for the first")
    sub.add_parser("status", help="registered members and the active unified generation")
    adopt = sub.add_parser("adopt", help="adopt an existing store read-only as a member (never writes, re-stamps or relocates it)")
    adopt.add_argument("--member", required=True, help="declared adopted member id (collection.members.<id>.adopt)")
    sub.add_parser("layers", help="digests of the declared attached layers as computed now (opens no backend)")
    verify = sub.add_parser("verify-space", help="verify an adopted member's embedding space against the collection's (re-embeds a sample; records the result)")
    verify.add_argument("--member", required=True, help="adopted member id whose adopt.embedding_space maps its space")
    verify.add_argument("--seed", type=int, default=0, help="seed of the stratified sample")
    for name, increment in PENDING.items():
        sub.add_parser(name, help=f"not implemented in this release (unified merge {increment})")


def run(args: argparse.Namespace, config: Any) -> dict[str, Any]:
    command = args.collection_command
    if command in PENDING:
        raise NotImplementedInRelease(f"collection {command} is not implemented in this release (unified merge {PENDING[command]}); nothing was changed")
    if command == "describe":
        return describe(config)
    if getattr(config, "collection", None) is None:
        raise ConfigError(f"collection {command} needs a partitioned configuration (schema_version: 3, collection.mode: partitioned)", "collection")
    if command == "adopt":
        from .ingestion.members import MemberRegistry, describe_member

        ref = MemberRegistry(config).adopt(args.member)
        return {"adopted": describe_member(ref), "store_generation": ref.stats.get("generation"), "files": len(ref.digests)}
    if command == "layers":
        from .ingestion.layers import LayerUnavailable, compute_digest

        out = []
        for layer in config.collection.layers:
            try:
                digest = compute_digest(config, layer)
            except (FileNotFoundError, LayerUnavailable) as exc:
                digest = f"missing: {exc}"
            out.append({"name": layer.name, "kind": layer.kind, "digest": digest, "pinned": layer.sha256, "matches": layer.sha256 is None or layer.sha256 == digest})
        return {"layers": out}
    if command == "verify-space":
        from .ingestion.members import MemberRegistry
        from .ingestion.space_equivalence import verify
        from .providers import Embedder

        record = verify(config, MemberRegistry(config), args.member, Embedder(config.models.roles.embedder).encode, seed=args.seed)
        return {"member": args.member, **{k: record[k] for k in ("passed", "reason", "checked", "store_chunks", "mean_cosine", "min_cosine", "below_min")}}
    from .engine import Engine
    from .ingestion.members import describe_member

    engine = Engine(config)
    registry = engine.view.attach()
    if command == "register":
        from .ingestion.normalize import parse_documents

        text = args.input.read_text()
        records = json.loads(text) if text.lstrip().startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
        ref = engine.build_member(args.member, args.key, parse_documents(records, config), replaces=list(args.replaces), keyword_epoch=args.keyword_epoch)
        return {"registered": describe_member(ref)}
    if command == "compose":
        from .ingestion.release import ANY

        members = None
        if args.unit:
            live = {r.unit_id: r for r in registry.live_members()}
            missing = [u for u in args.unit if u not in live]
            if missing:
                raise ConfigError(f"collection compose: no live registered member unit {missing}", "collection")
            members = [live[u] for u in args.unit]
        expected = ANY if args.expected is None else (None if args.expected == "none" else args.expected)
        manifest = engine.compose(members, expected=expected)
        return {
            "generation": manifest["generation"],
            "base": manifest["base"],
            "members": [f"{m['member']}/{m['key']}" for m in manifest["members"]],
            "documents": manifest["documents"],
        }
    current = registry.generations.current()
    return {
        "members": [describe_member(r) for r in registry.members()],
        "live_units": [u["unit_id"] for u in registry.directory.units(status="live")],
        "generation": None
        if current is None
        else {"generation": current["generation"], "members": [f"{m['member']}/{m['key']}" for m in current["members"]], "documents": current["documents"]},
    }


def describe(config: Any) -> dict[str, Any]:
    """The collection as declared: members, layers, strata statistics and quotas. ``served`` says
    whether this release serves it (yes, unless it declares cross-source blocks, layer kinds or
    community strategies of later increments, or an engine feature the partition core refuses —
    ``runtime/collection.py``)."""
    collection = getattr(config, "collection", None)
    if collection is None:
        return {"schema_version": config.schema_version, "dataset": config.dataset, "mode": "single", "members": ["default"], "served": True}
    members = {
        name: {
            "kind": member.kind,
            "sources": member.sources,
            "record_kinds": member.record_kinds,
            "key": member.key,
            "cadence": member.cadence,
            "producer": member.producer.model_dump(mode="json"),
            "catalog_kinds": list(member.all_catalog_kinds()),
            "adopted": member.adopt is not None,
            "lexical_stats": collection.stats_of(name),
            "required": member.required,
        }
        for name, member in collection.members.items()
    }
    return {
        "schema_version": config.schema_version,
        "dataset": config.dataset,
        "mode": collection.mode,
        "served": not unserved_blocks(config) and not partitioned_refusals(config),
        "members": members,
        "layers": [{"name": layer.name, "kind": layer.kind, "sources": layer.sources, "arms": layer.arms, "member": layer.member} for layer in collection.layers],
        "source_quotas": collection.selection.source_quotas,
        "embedding_space": None if collection.embedding_space is None else collection.embedding_space.id,
        "community_layers": [layer.name for layer in config.communities.layers],
    }
