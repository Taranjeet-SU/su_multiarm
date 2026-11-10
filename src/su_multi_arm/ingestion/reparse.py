"""Delta reparse of a sealed partition after a library change (addendum A2 §4.1.6, review B3).

A full reparse re-matches every head record of the window whenever the approved library identity
changes, which under auto-promotion is almost every day. The delta reparse re-matches only the head
records whose result can change, and copies every other match row (typed values included):

1. **Residue:** rows whose status is ``unparsed``, ``format_only`` or ``timeout``.
2. **Retired:** rows whose ``pattern_id`` is no longer approved, or whose entry's regex, kind,
   priority, signature or selectors changed (compared through the per-entry digest the partition
   stored at seal, ``patterns.json``).
3. **Stolen:** rows a **new or changed** message/noise/annotate entry could take. This is a proof
   over the stored lines, not a sample: the new entries' patterns are compiled into one engine
   ``Set`` (RE2; every pattern for the ``regex`` engine), and every stored head line the set flags
   is confirmed with a real search. Any flagged row is re-matched (a superset of the rows whose
   winner changes; never fewer).

``library_diff`` sorts the changes into classes A–H (``CHANGE_CLASSES``): format (A),
continuation (B), redaction (G) and engine (H) changes are a full reparse; an annotate change (C)
re-matches the rows its old or new regex matches; an edited (D), added (E) or retired (F)
message/noise entry is the retired set plus the steal scan. A partition without ``patterns.json``
(sealed before format 2) or a delta above ``max_delta_share`` of the rows is a full reparse. The result is ``v+1`` like any
reparse; day documents are content-addressed, so the rows that did not change keep their document
ids and content hashes and compose reuses them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

RESIDUE = frozenset({"unparsed", "format_only", "timeout"})
MAX_DELTA_SHARE = 0.3


ENGINE_KEY = "__engine__"


def library_digest(library: Any) -> dict[str, dict[str, Any]]:
    """``entry id -> {kind, sha, regex}`` of the approved entries of a compiled library (the sha
    covers everything that decides a match: regex, kind, priority, selectors, signature, fields),
    plus the engine identity under ``__engine__``. No line text."""
    out: dict[str, dict[str, Any]] = {}
    for loaded in library.library.approved():
        canonical = loaded.canonical()
        out[loaded.entry.id] = {"kind": loaded.entry.kind, "sha": hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest(), "regex": loaded.regex}
    engine = getattr(library, "engine", None)
    if engine is not None and hasattr(engine, "identity"):
        out[ENGINE_KEY] = {"kind": "engine", "sha": hashlib.sha256(json.dumps(engine.identity(), sort_keys=True).encode()).hexdigest()}
    return out


# Library-change classes (addendum A2 rev 3, R2-B3) and what each costs.
CHANGE_CLASSES = {
    "A": "format entry added, edited or retired: full reparse",
    "B": "continuation entry changed: full reparse (multiline assembly changes)",
    "C": "annotate entry added, edited or retired: rows its old or new regex matches are re-matched",
    "D": "message/noise entry edited: its rows (retired set) plus a steal scan of the new version",
    "E": "message/noise entry added: steal scan",
    "F": "message/noise entry retired (deprecated, superseded, disabled): its rows",
    "G": "redaction changed: full reparse from the raw tier (never a delta)",
    "H": "engine or engine version changed: full reparse",
}
FULL = frozenset({"A", "B", "G", "H"})


@dataclass(frozen=True)
class Change:
    cls: str
    entry: str


def library_diff(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]], *, redaction_changed: bool = False) -> list[Change]:
    """The classified changes from the digest a partition was parsed under to ``new``."""
    changes: list[Change] = []
    if redaction_changed:
        changes.append(Change("G", "redaction"))
    if (old.get(ENGINE_KEY) or {}).get("sha") != (new.get(ENGINE_KEY) or {}).get("sha") and ENGINE_KEY in old:
        changes.append(Change("H", ENGINE_KEY))
    for entry in sorted((set(old) | set(new)) - {ENGINE_KEY}):
        before, after = old.get(entry), new.get(entry)
        if before is not None and after is not None and before["sha"] == after["sha"]:
            continue
        kind = (after or before or {}).get("kind")
        if kind == "format":
            changes.append(Change("A", entry))
        elif kind == "continuation":
            changes.append(Change("B", entry))
        elif kind == "annotate":
            changes.append(Change("C", entry))
        elif before is None:
            changes.append(Change("E", entry))
        elif after is None:
            changes.append(Change("F", entry))
        else:
            changes.append(Change("D", entry))
    return changes


@dataclass
class DeltaPlan:
    mode: str  # "delta" | "full"
    reason: str = ""
    rows: int = 0
    delta: set[str] = field(default_factory=set)
    retired: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {"mode": self.mode, "reason": self.reason, "rows": self.rows, "delta_rows": len(self.delta), "retired": self.retired, "added": self.added, "classes": self.classes}


def plan_delta(old_digest: dict[str, dict[str, Any]] | None, library: Any, matches: list[dict[str, Any]], head_lines: dict[str, str], *, max_delta_share: float = MAX_DELTA_SHARE) -> DeltaPlan:
    """Which stored rows must be re-matched under ``library`` (a ``CompiledLibrary``)."""
    if old_digest is None:
        return DeltaPlan("full", "the partition has no stored library digest (sealed before format 2)", len(matches))
    new_digest = library_digest(library)
    changes = library_diff(old_digest, new_digest)
    retired = sorted(c.entry for c in changes if c.cls in {"D", "F"})
    added = sorted(c.entry for c in changes if c.cls in {"C", "D", "E"})
    classes = sorted({c.cls for c in changes})
    if FULL & set(classes):
        return DeltaPlan("full", f"change classes {classes}: {'; '.join(CHANGE_CLASSES[c] for c in classes if c in FULL)}", len(matches), retired=retired, added=added, classes=classes)
    if any(c.cls == "C" and c.entry in old_digest and "regex" not in old_digest[c.entry] for c in changes):
        return DeltaPlan("full", "an annotate entry changed and the stored digest has no regex", len(matches), retired=retired, added=added, classes=classes)
    delta = {m["id"] for m in matches if m["status"] in RESIDUE or (m.get("pattern_id") in set(retired))}
    # the regexes to scan: new versions of C/D/E entries, and the old version of a C entry (its
    # annotations must leave the rows it matched)
    patterns = [e.regex for e in library.library.approved() if e.entry.id in set(added) and e.entry.kind in {"message", "noise", "annotate"}]
    patterns += [old_digest[c.entry]["regex"] for c in changes if c.cls == "C" and c.entry in old_digest]
    if patterns:
        engine = library.engine
        compiled = [engine.compile(p) for p in patterns]
        prefilter = engine.build_set(patterns)
        for match in matches:
            if match["id"] in delta:
                continue
            line = head_lines.get(match["id"], "")
            for index in sorted(engine.prefilter(prefilter, line, len(patterns))):
                if engine.search(compiled[index], line) is not None:
                    delta.add(match["id"])
                    break
    plan = DeltaPlan("delta", "", len(matches), delta, retired, added, classes)
    if matches and len(delta) > max_delta_share * len(matches):
        return DeltaPlan("full", f"delta {len(delta)} of {len(matches)} rows exceeds max_delta_share {max_delta_share}", len(matches), delta, retired, added, classes)
    return plan


def reparse_partition(store: Any, key: str, library: Any, parse: Callable[..., Any], *, max_delta_share: float = MAX_DELTA_SHARE) -> tuple[Any, DeltaPlan]:
    """``v+1`` of partition ``key`` under ``library``: a delta reparse when it is provably enough,
    else a full one. ``parse(units, library, reuse=..., delta=...)`` is ``LogsPipeline.parse``."""
    base = store.get(key)
    old_digest = None
    matches: list[dict[str, Any]] = []
    if base is not None and "patterns.json" in base.artifacts:
        old_digest = store.read(key, "patterns.json")
        matches = store.read(key, "matches.jsonl.gz")
    head_lines: dict[str, str] = {}
    if old_digest is not None:
        for artifacts in store.units(key, base.version):
            for record in artifacts.records:
                head_lines[record.id] = record.message.split("\n", 1)[0]
    plan = plan_delta(old_digest, library, matches, head_lines, max_delta_share=max_delta_share)
    if plan.mode == "full":
        return store.reparse(key, library, lambda units, lib: parse(units, lib)), plan
    reuse = {m["id"]: m for m in matches if m["id"] not in plan.delta}
    return store.reparse(key, library, lambda units, lib: parse(units, lib, reuse=reuse)), plan
