"""Pattern library: files, loader with decisions replay, lint and identity (addendum §3.3, §3.4.3,
owner policy amendment 2026-11-04).

One library directory holds four files:

=================  =========================  =================================================
File               Owner                      Written by
=================  =========================  =================================================
``library.yaml``   human                      humans only; the system never opens it for writing
``learned.yaml``   system (humans may edit)   the CAS-safe writer (``writer.py``), append-only
``proposed.yaml``  system                     the writer, append-only
``decisions.yaml`` system                     the writer (CLI or promotion policy), append-only
=================  =========================  =================================================

``library.yaml`` is a mapping (``library``, ``format_version``, ``engine``, ``vocabularies``,
``masks``, ``patterns``). The three system files are YAML sequences, so an append never rewrites
existing bytes.

**Merge.** Entries are read in the order user, learned, proposed. An id defined by more than one owner
resolves to the first of them (a human entry wins over a learned one, a learned entry over a pending
proposal) with an ``info`` diagnostic. Decisions are then replayed in file order:

- ``approve``: the entry becomes approved (a proposal is promoted).
- ``deprecate``: the entry stops matching.
- ``supersede`` (``id`` = new, ``target`` = old): the target is shadowed, the new entry approved.
- ``expire``: an unpromoted proposal is dropped (its literals leave the effective library).
- ``disable``: only by a human (``by: user:...``); final, it wins over every other record and file.

A decision written by the system (``by: system:...``) never changes a human-owned entry, and a
system ``disable`` is not applied; both are reported, not silently dropped.

**Identity.** ``library_identity`` is the sha256 of the canonical JSON of the effective approved
entries (expanded regexes; examples, provenance and evidence excluded: they do not change matching).
It is build provenance, never part of the live index fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from ...core.errors import ConfigError
from ...core.types import FieldType, JsonScalar
from ...models import StrictModel
from .engine import EngineKind, LintIssue, PatternEngine, PatternTimeout, group_names, validate_regex

LIBRARY_FILE = "library.yaml"
LEARNED_FILE = "learned.yaml"
PROPOSED_FILE = "proposed.yaml"
DECISIONS_FILE = "decisions.yaml"
WRITER_STATE_FILE = ".writer_state.json"
SYSTEM_FILES = (LEARNED_FILE, PROPOSED_FILE, DECISIONS_FILE)

PatternKind = Literal["index_name", "format", "continuation", "message", "annotate", "noise"]
EntryStatus = Literal["approved", "proposed", "deprecated"]
EffectiveStatus = Literal["approved", "proposed", "deprecated", "superseded", "disabled", "expired"]
Owner = Literal["user", "learned", "proposed"]
# ``$attribute`` endpoints of attribute links and the document metadata key each one reads.
ATTRIBUTE_ENDPOINTS = {"$service": "service", "$tenant": "tenant", "$env": "env", "$signature": "signature_id"}
ENTRY_ID = r"^[a-z0-9][a-z0-9_.\-]{0,127}$"
GROUP_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
ACTOR = r"^(user|system):[A-Za-z0-9_.@\-]{1,64}$"
_TEMPLATE = re.compile(r"\{\{(vocab|mask):([A-Za-z0-9_\-]+)\}\}")
_METACHARS = set(".^$*+?()[]{}|\\")
# Unredacted shapes refused in library examples (fixed, linear stdlib patterns over short strings).
_PII_SHAPES = {
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "ipv4": re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\."),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


class PatternLibraryError(ConfigError):
    """The library has lint errors (or cannot be read). Carries every issue."""

    def __init__(self, message: str, issues: list[LintIssue] | None = None):
        self.issues = list(issues or [])
        super().__init__(message, "extraction.pattern_library")


# --- entry model --------------------------------------------------------------------------------


class FieldDecl(StrictModel):
    type: FieldType
    values: list[str] | None = None  # enum values

    @model_validator(mode="after")
    def enum_values(self):
        if (self.type == "enum") != (self.values is not None):
            raise ValueError("enum fields declare values; other types do not")
        return self


class EntityGroup(StrictModel):
    group: str = Field(pattern=GROUP_NAME)
    type: str = Field(min_length=1, max_length=64)


class RelationTemplate(StrictModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1)
    trigger_group: str | None = None


class AttributeLink(StrictModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1)


class SignatureDecl(StrictModel):
    id: str = Field(pattern=ENTRY_ID)
    category: str = Field(default="unknown", min_length=1, max_length=64)


class Examples(StrictModel):
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)


class Then(StrictModel):
    field_from_group: str = Field(pattern=GROUP_NAME)


class PatternEntry(StrictModel):
    id: str = Field(pattern=ENTRY_ID)
    kind: PatternKind
    status: EntryStatus
    priority: int = 0
    # ``field`` selects chunk fields; any other key selects on a line attribute (a format group of
    # the same name, else document metadata). ``"*"`` matches anything, including a missing value.
    applies_to: dict[str, str | list[str]] = Field(default_factory=dict)
    regex: str = Field(min_length=1, max_length=4096)
    fields: dict[str, FieldDecl] = Field(default_factory=dict)
    entities: list[EntityGroup] = Field(default_factory=list)
    relations: list[RelationTemplate] = Field(default_factory=list)
    attribute_links: list[AttributeLink] = Field(default_factory=list)
    signature: SignatureDecl | None = None
    then: Then | None = None
    examples: Examples = Field(default_factory=Examples)
    provenance: dict[str, JsonScalar] = Field(default_factory=dict)
    evidence: dict[str, Any] | None = None  # gate summary of a system-written entry

    @field_validator("applies_to")
    @classmethod
    def selectors(cls, value):
        for key, allowed in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise ValueError(f"applies_to key {key!r} must be an identifier")
            if isinstance(allowed, list) and not allowed:
                raise ValueError(f"applies_to.{key} cannot be empty")
        return value

    def selector(self, key: str) -> list[str] | None:
        """Allowed values for ``key``; ``None`` when unrestricted."""
        value = self.applies_to.get(key)
        values = [value] if isinstance(value, str) else value
        if values is None or "*" in values:
            return None
        return values


class Decision(StrictModel):
    op: Literal["approve", "deprecate", "supersede", "expire", "disable"]
    id: str = Field(pattern=ENTRY_ID)
    target: str | None = Field(default=None, pattern=ENTRY_ID)
    by: str = Field(pattern=ACTOR)
    at: datetime
    reason: str = Field(default="", max_length=2000)
    examples: list[str] = Field(default_factory=list)  # redacted lines copied at approval (survive retention)
    gate: dict[str, Any] | None = None

    @model_validator(mode="after")
    def target_only_for_supersede(self):
        if (self.op == "supersede") != (self.target is not None):
            raise ValueError("supersede needs target; other decisions take none")
        return self

    @property
    def human(self) -> bool:
        return self.by.startswith("user:")

    def decision_id(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:16]


class LibraryHeader(StrictModel):
    library: str = Field(min_length=1, max_length=128)
    format_version: Literal[1]
    engine: EngineKind = "re2"
    vocabularies: dict[str, list[str]] = Field(default_factory=dict)
    masks: dict[str, str] = Field(default_factory=dict)
    patterns: list[Any] = Field(default_factory=list)


# --- loaded library -----------------------------------------------------------------------------


@dataclass
class LoadedEntry:
    entry: PatternEntry
    owner: Owner
    source: str
    regex: str  # vocabularies and masks expanded
    status: EffectiveStatus
    decisions: list[str] = field(default_factory=list)  # applied decision ids

    def canonical(self) -> dict[str, Any]:
        data = self.entry.model_dump(mode="json", exclude={"examples", "provenance", "evidence", "status"})
        data["regex"] = self.regex
        return data


@dataclass
class Library:
    path: Path
    name: str
    engine_hint: EngineKind
    vocabularies: dict[str, list[str]]
    masks: dict[str, str]
    entries: dict[str, LoadedEntry]
    applied_decisions: list[Decision]
    issues: list[LintIssue]
    file_shas: dict[str, str]

    def approved(self, kind: str | None = None) -> list[LoadedEntry]:
        """Effective approved entries, ordered by priority (high first) then id."""
        rows = [e for e in self.entries.values() if e.status == "approved" and (kind is None or e.entry.kind == kind)]
        return sorted(rows, key=lambda e: (-e.entry.priority, e.entry.id))

    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def identity(self) -> str:
        canonical = [e.canonical() for e in sorted(self.approved(), key=lambda e: e.entry.id)]
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def applied_decision_ids(self) -> list[str]:
        return sorted(d.decision_id() for d in self.applied_decisions)

    def snapshot(self, provenance: dict[str, Any]) -> dict[str, Any]:
        """The effective approved library as served with a generation (``library.snapshot.json``)."""
        return {
            "library": self.name,
            "format_version": 1,
            "provenance": provenance,
            "decisions": self.applied_decision_ids(),
            "entries": [e.canonical() for e in sorted(self.approved(), key=lambda e: e.entry.id)],
        }

    def lowest_priority(self, kind: str) -> int | None:
        rows = self.approved(kind)
        return min(e.entry.priority for e in rows) if rows else None


def file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def escape_literal(text: str) -> str:
    """Escape regex metacharacters only (RE2 and the stdlib agree on these escapes)."""
    return "".join("\\" + c if c in _METACHARS else c for c in text)


def vocabulary_alternation(values: list[str]) -> str:
    """Longest-first alternation of escaped literals."""
    ordered = sorted(set(values), key=lambda v: (-len(v), v))
    return "(?:" + "|".join(escape_literal(v) for v in ordered) + ")"


def expand(regex: str, vocabularies: dict[str, list[str]], masks: dict[str, str]) -> tuple[str, list[str]]:
    """Expand ``{{vocab:name}}`` and ``{{mask:name}}``; the second value lists unresolvable names."""
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        kind, name = match.groups()
        if kind == "vocab":
            if name not in vocabularies or not vocabularies[name]:
                missing.append(f"vocab:{name}")
                return match.group()
            return vocabulary_alternation(vocabularies[name])
        if name not in masks:
            missing.append(f"mask:{name}")
            return match.group()
        return "(?:" + masks[name] + ")"

    return _TEMPLATE.sub(repl, regex), missing


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PatternLibraryError(f"{path.name}: invalid YAML: {exc}") from None


def _schema_issue(exc: ValidationError, source: str, pattern_id: str | None) -> LintIssue:
    problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or '<entry>'}: {e['msg']}" for e in exc.errors())
    return LintIssue("error", "schema", problems, pattern_id, source)


def load_library(path: str | Path, engine: PatternEngine, *, min_literal_chars: int = 4, strict: bool = True) -> Library:
    """Read, merge, replay and lint the library directory. With ``strict`` (the default) any lint
    error raises ``PatternLibraryError``; otherwise the issues are returned on the library."""
    root = Path(path)
    if not root.is_dir():
        raise PatternLibraryError(f"pattern library directory {root} does not exist")
    issues: list[LintIssue] = []
    user_file = root / LIBRARY_FILE
    if not user_file.exists():
        raise PatternLibraryError(f"{root / LIBRARY_FILE} is missing (the human-owned file is required)")
    raw = _read_yaml(user_file) or {}
    if not isinstance(raw, dict):
        raise PatternLibraryError(f"{LIBRARY_FILE} must be a mapping")
    try:
        header = LibraryHeader.model_validate(raw)
    except ValidationError as exc:
        raise PatternLibraryError(f"{LIBRARY_FILE}: {_schema_issue(exc, LIBRARY_FILE, None).message}") from None
    for name, pattern in header.masks.items():
        # A mask is a fragment: checked whole-line here, and again in context once expanded.
        issues.extend(i for i in validate_regex(f"^(?:{pattern})$", pattern_id=f"mask:{name}", source=LIBRARY_FILE) if i.severity == "error")
    shas = {name: sha for name in (LIBRARY_FILE, *SYSTEM_FILES) if (sha := file_sha(root / name)) is not None}
    issues.extend(_hand_edits(root, shas))

    by_owner: list[tuple[Owner, str, list[Any]]] = [("user", LIBRARY_FILE, header.patterns)]
    for owner, name in (("learned", LEARNED_FILE), ("proposed", PROPOSED_FILE)):
        rows = _read_yaml(root / name) if (root / name).exists() else None
        if rows is not None and not isinstance(rows, list):
            raise PatternLibraryError(f"{name} must be a YAML sequence of entries")
        by_owner.append((owner, name, rows or []))

    entries: dict[str, LoadedEntry] = {}
    for owner, source, rows in by_owner:
        seen: set[str] = set()
        for index, row in enumerate(rows):
            rid = row.get("id") if isinstance(row, dict) else None
            try:
                entry = PatternEntry.model_validate(row)
            except ValidationError as exc:
                issues.append(_schema_issue(exc, source, rid if isinstance(rid, str) else f"#{index}"))
                continue
            if entry.id in seen:
                issues.append(LintIssue("error", "duplicate_id", f"id defined twice in {source}", entry.id, source))
                continue
            seen.add(entry.id)
            if entry.id in entries:
                winner = entries[entry.id]
                issues.append(
                    LintIssue("info", "shadowed_by_owner", f"{winner.owner} entry in {winner.source} overrides this {owner} entry", entry.id, source)
                )
                continue
            regex, missing = expand(entry.regex, header.vocabularies, header.masks)
            for name in missing:
                issues.append(LintIssue("error", "unresolved_template", f"unresolvable {{{{{name}}}}}", entry.id, source))
            status: EffectiveStatus = "proposed" if owner == "proposed" else entry.status
            if owner == "proposed" and entry.status != "proposed":
                issues.append(LintIssue("warning", "proposal_status", "proposed.yaml entries take their status from decisions", entry.id, source))
            entries[entry.id] = LoadedEntry(entry, owner, source, regex, status)

    applied = _replay(root, entries, issues)
    library = Library(root, header.library, header.engine, header.vocabularies, header.masks, entries, applied, issues, shas)
    _lint(library, engine, min_literal_chars)
    if strict and library.errors():
        rendered = "; ".join(i.render() for i in library.errors()[:20])
        raise PatternLibraryError(f"pattern library {root} has {len(library.errors())} lint error(s): {rendered}", library.issues)
    return library


def _hand_edits(root: Path, shas: dict[str, str]) -> list[LintIssue]:
    state_path = root / WRITER_STATE_FILE
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return [LintIssue("warning", "writer_state", f"{WRITER_STATE_FILE} is unreadable", None, WRITER_STATE_FILE)]
    issues = []
    for name in SYSTEM_FILES:
        if name in shas and name in state and state[name] != shas[name]:
            severity: Literal["warning", "info"] = "info" if name == LEARNED_FILE else "warning"
            issues.append(LintIssue(severity, "system_file_edited", "edited outside the writer (sha differs from its last write)", None, name))
    return issues


def _replay(root: Path, entries: dict[str, LoadedEntry], issues: list[LintIssue]) -> list[Decision]:
    path = root / DECISIONS_FILE
    rows = _read_yaml(path) if path.exists() else None
    if rows is not None and not isinstance(rows, list):
        raise PatternLibraryError(f"{DECISIONS_FILE} must be a YAML sequence of records")
    applied: list[Decision] = []
    disabled: set[str] = set()

    def note(severity: Literal["error", "warning", "info"], code: str, message: str, pid: str) -> None:
        issues.append(LintIssue(severity, code, message, pid, DECISIONS_FILE))

    for index, row in enumerate(rows or []):
        try:
            decision = Decision.model_validate(row)
        except ValidationError as exc:
            issues.append(_schema_issue(exc, DECISIONS_FILE, f"#{index}"))
            continue
        target_ids = [decision.id] + ([decision.target] if decision.target else [])
        unknown = [i for i in target_ids if i not in entries]
        if unknown:
            note("error", "unknown_decision_target", f"{decision.op} refers to unknown id(s) {unknown}", decision.id)
            continue
        if any(i in disabled for i in target_ids):
            note("info", "disabled_final", f"{decision.op} after a human disable is not applied", decision.id)
            continue
        if not decision.human and any(entries[i].owner == "user" for i in target_ids):
            note("warning", "system_decision_on_user_entry", f"system {decision.op} cannot change a human-owned entry; not applied", decision.id)
            continue
        entry = entries[decision.id]
        if decision.op == "disable":
            if not decision.human:
                note("warning", "system_disable", "disable is a human decision; not applied", decision.id)
                continue
            entry.status = "disabled"
            disabled.add(decision.id)
        elif decision.op == "approve":
            if entry.status in {"superseded", "expired"}:
                note("warning", "approve_inactive", f"cannot approve a {entry.status} entry; not applied", decision.id)
                continue
            entry.status = "approved"
        elif decision.op == "deprecate":
            entry.status = "deprecated"
        elif decision.op == "supersede":
            old = entries[decision.target]  # type: ignore[index]
            if decision.target == decision.id or old.entry.kind != entry.entry.kind:
                note("error", "bad_supersede", "supersede needs a different entry of the same kind", decision.id)
                continue
            old.status = "superseded"
            old.decisions.append(decision.decision_id())
            entry.status = "approved"
        elif decision.op == "expire":
            if entry.owner != "proposed" or entry.status != "proposed":
                note("warning", "expire_not_proposal", "only an unpromoted proposal can expire; not applied", decision.id)
                continue
            entry.status = "expired"
        entry.decisions.append(decision.decision_id())
        applied.append(decision)
    return applied


# --- lint ---------------------------------------------------------------------------------------


def pii_shapes(text: str) -> list[str]:
    return [name for name, shape in _PII_SHAPES.items() if shape.search(text)]


def _lint(library: Library, engine: PatternEngine, min_literal_chars: int) -> None:
    format_groups: set[str] = set()
    for loaded in library.entries.values():
        if loaded.entry.kind == "format" and loaded.status == "approved":
            format_groups.update(e.group for e in loaded.entry.entities)
    compiled: dict[str, Any] = {}
    for loaded in library.entries.values():
        found = lint_entry(loaded, engine, min_literal_chars, format_groups, compiled)
        if loaded.status != "approved":  # an inactive entry cannot break the library; it stays inert
            found = [LintIssue("warning", "inactive_" + i.code, i.message, i.pattern_id, i.source) if i.severity == "error" else i for i in found]
        library.issues.extend(found)
    _lint_ambiguity(library, engine, compiled)


def lint_entry(loaded: LoadedEntry, engine: PatternEngine, min_literal_chars: int, format_groups: set[str], compiled: dict[str, Any]) -> list[LintIssue]:
    entry, source = loaded.entry, loaded.source
    issues: list[LintIssue] = []

    def error(code: str, message: str) -> None:
        issues.append(LintIssue("error", code, message, entry.id, source))

    if "{{" in loaded.regex:
        return issues  # unresolved template already reported
    issues.extend(validate_regex(loaded.regex, min_literal_chars=min_literal_chars, pattern_id=entry.id, source=source))
    if any(i.severity == "error" for i in issues):
        return issues
    groups = set(group_names(loaded.regex))
    own_entities = {e.group for e in entry.entities}
    for name, what in [(e.group, "entity") for e in entry.entities] + [(k, "field") for k in entry.fields]:
        if name not in groups:
            error("undeclared_group", f"{what} group {name!r} is not a named group of the regex")
    if entry.then is not None and entry.then.field_from_group not in groups:
        error("undeclared_group", f"then.field_from_group {entry.then.field_from_group!r} is not a named group")
    kind = entry.kind
    if kind == "format" and entry.then is None:
        error("format_then", "a format pattern hands one group to the message stage (then.field_from_group)")
    if kind != "format" and entry.then is not None:
        error("then_not_format", "only format patterns take then")
    if kind == "message" and entry.signature is None:
        error("message_signature", "a message pattern declares its signature")
    if kind != "message" and entry.signature is not None:
        error("signature_not_message", "only message patterns set a signature")
    if kind not in {"format", "message"} and (entry.relations or entry.attribute_links):
        error("relations_kind", f"{kind} patterns cannot declare relations or attribute links")
    if kind == "noise" and entry.entities:
        error("noise_entities", "noise patterns mark lines; they declare no entities")
    for relation in entry.relations:
        ends = (relation.subject, relation.object)
        if any(end.startswith("$") for end in ends):
            if relation.trigger_group is not None:
                error("attribute_trigger", "a relation with a $attribute endpoint cannot have a trigger_group; use attribute_links")
            else:
                error("attribute_relation", "$attribute endpoints belong under attribute_links (header-scoped native links)")
            continue
        if relation.trigger_group is not None and relation.trigger_group not in groups:
            error("undeclared_group", f"trigger_group {relation.trigger_group!r} is not a named group")
        allowed = own_entities | (format_groups if kind == "message" else set())
        for end in ends:
            if end not in allowed:
                error("relation_endpoint", f"endpoint {end!r} is not an entity group of this pattern or of a format pattern")
    for link in entry.attribute_links:
        for end in (link.subject, link.object):
            if end not in ATTRIBUTE_ENDPOINTS:
                error("attribute_endpoint", f"attribute link endpoint {end!r} is not one of {sorted(ATTRIBUTE_ENDPOINTS)}")
    for text in entry.examples.positive + entry.examples.negative:
        shapes = pii_shapes(text)
        if shapes:
            error("example_pii", f"example contains an unredacted {', '.join(shapes)} shape")
    if any(i.severity == "error" for i in issues):
        return issues
    try:
        pattern = engine.compile(loaded.regex)
    except Exception as exc:  # engine-specific compile error
        error("compile", f"{engine.kind} refuses the pattern: {exc}")
        return issues
    compiled[entry.id] = pattern
    try:
        for text in entry.examples.positive:
            if engine.search(pattern, text) is None:
                error("example_fails", f"positive example does not match: {text[:120]!r}")
        for text in entry.examples.negative:
            if engine.search(pattern, text) is not None:
                error("example_fails", f"negative example matches: {text[:120]!r}")
    except PatternTimeout as exc:
        error("example_timeout", str(exc))
    return issues


def _selectors_overlap(a: PatternEntry, b: PatternEntry) -> bool:
    for key in set(a.applies_to) | set(b.applies_to):
        left, right = a.selector(key), b.selector(key)
        if left is not None and right is not None and not set(left) & set(right):
            return False
    return True


def _lint_ambiguity(library: Library, engine: PatternEngine, compiled: dict[str, Any]) -> None:
    rows = [e for e in library.approved() if e.entry.id in compiled and e.entry.examples.positive]
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if a.entry.kind != b.entry.kind or a.entry.priority != b.entry.priority or not _selectors_overlap(a.entry, b.entry):
                continue
            try:
                ab = all(engine.search(compiled[a.entry.id], t) for t in b.entry.examples.positive)
                ba = all(engine.search(compiled[b.entry.id], t) for t in a.entry.examples.positive)
            except PatternTimeout:
                continue  # already reported by the example check of the slower entry
            if ab and ba:
                library.issues.append(
                    LintIssue(
                        "error",
                        "ambiguous",
                        f"same kind, priority and selectors as {b.entry.id!r}, and each matches the other's positives",
                        a.entry.id,
                        a.source,
                    )
                )
