"""Versioned, pinned link store (unified merge plan §6.9, §6.12; increment M3, lane LD).

A private, append-only SQLite file (``UPDATE``/``DELETE`` refused by triggers), versioned like the
identity registry: every mutating operation (``build``, ``review``, ``decide``) adds one
``link_version`` and every other row carries it. Served through ``LinkView.pinned(path, sha256)``:
a missing file or a digest mismatch serves **no** links, labelled (§6.12 "link store unavailable").

Tables: ``link`` (definition rows: pair, tier, builder, subject/object ids, evidence and its
digest; a new row only when the evidence changes), ``build_link`` (the links a build produced),
``retraction`` (links of the previous build that a build no longer produced, with the reason:
``tombstoned_subject`` / ``tombstoned_object`` when the record left the pinned collection — e.g. an
episode id that disappeared in a gap-filling merge — else ``not_rebuilt``), ``build_report``,
``review`` (model verdicts bound to the evidence digest they saw) and ``decision`` (coordinator or
owner ``accept | reject``).

Statuses (F0, seven): ``accepted`` (tier A explicit reference), ``candidate`` (unreviewed rule
link, or ``uncertain``), ``model_accepted`` / ``model_rejected`` (model verdict on the current
evidence), ``human_accepted`` / ``human_rejected`` (owner decision, or a coordinator decision that
does not contradict the model), ``contested`` (coordinator and model disagree: owner queue; only an
owner decision resolves it). A review of older evidence no longer counts (the link is re-queued).

Display (record pages and citations are rendered by LF; this module exposes the data only):
``accepted``/``human_accepted`` shown; ``candidate``/``model_accepted``/``contested`` shown
**labelled** (or hidden with ``show_candidates: hidden``); ``model_rejected``/``human_rejected``
and retracted links hidden. Links derived through a customer join are withheld when the pinned
identity view is disabled or its registry version differs from the one the links were built with.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ...core.errors import SuError
from ..identity import sha256_file
from ..review_ledger import append_only_triggers, read_only_uri
from .builders import BUILDER_VERSION, Link

FORMAT_VERSION = 1
KIND = "su_multi_arm.link_store"
STATUSES = ("accepted", "candidate", "model_accepted", "model_rejected", "human_accepted", "human_rejected", "contested")
DISPLAY = {"accepted": "shown", "human_accepted": "shown", "candidate": "labelled", "model_accepted": "labelled", "contested": "labelled",
           "model_rejected": "hidden", "human_rejected": "hidden"}
LABELS = {
    "candidate": "unreviewed candidate link",
    "model_accepted": "model-reviewed link, not confirmed by a person",
    "contested": "contested link: reviewer and coordinator disagree",
}
STATUS_ORDER = {s: i for i, s in enumerate(("accepted", "human_accepted", "model_accepted", "candidate", "contested", "model_rejected", "human_rejected"))}
ACTIONS = ("accept", "reject")
AUTHORITIES = ("coordinator", "owner")
VERDICTS = ("agree", "refutes", "uncertain")
RETRACTION_REASONS = ("tombstoned_subject", "tombstoned_object", "not_rebuilt")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS link_version (
    version INTEGER PRIMARY KEY, operation TEXT NOT NULL, actor TEXT, note TEXT,
    builder_version TEXT, registry_version TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, link_id TEXT NOT NULL, pair TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('A', 'B', 'C')), builder TEXT NOT NULL,
    subject_id TEXT NOT NULL, object_id TEXT NOT NULL, evidence TEXT NOT NULL, evidence_sha TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS link_by_id ON link (link_id, row_id);
CREATE TABLE IF NOT EXISTS build_link (version INTEGER NOT NULL, link_id TEXT NOT NULL, PRIMARY KEY (version, link_id));
CREATE TABLE IF NOT EXISTS retraction (
    version INTEGER NOT NULL, link_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('tombstoned_subject', 'tombstoned_object', 'not_rebuilt')),
    PRIMARY KEY (version, link_id)
);
CREATE TABLE IF NOT EXISTS build_report (version INTEGER PRIMARY KEY, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review (
    review_id TEXT PRIMARY KEY, link_id TEXT NOT NULL, evidence_sha TEXT NOT NULL, model TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('agree', 'refutes', 'uncertain')), rationale TEXT NOT NULL,
    cited_fields TEXT NOT NULL, usd REAL NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decision (
    decision_id TEXT PRIMARY KEY, link_id TEXT NOT NULL, action TEXT NOT NULL CHECK (action IN ('accept', 'reject')),
    authority TEXT NOT NULL CHECK (authority IN ('coordinator', 'owner')), rationale TEXT NOT NULL,
    evidence_sha TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL
);
"""
TABLES = ("meta", "link_version", "link", "build_link", "retraction", "build_report", "review", "decision")


class LinkStoreError(SuError, ValueError):
    """A link store operation was refused (nothing was changed)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hid(prefix: str, *parts: Any) -> str:
    return f"{prefix}-" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def content_sha256(data: dict[str, Any]) -> str:
    body = {k: v for k, v in data.items() if k != "content_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class LinkState:
    link_id: str
    pair: str
    tier: str
    builder: str
    subject_id: str
    object_id: str
    evidence: dict[str, Any] = field(compare=False)
    evidence_sha: str
    status: str
    served: bool
    retracted: str | None = None
    model_verdict: str | None = None
    review: str | None = None
    decision: str | None = None
    version: int = 0

    @property
    def customer_derived(self) -> bool:
        return "customer_id" in self.evidence

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def derive_status(tier: str, verdict: str | None, decisions: list[dict[str, Any]]) -> str:
    """The status from the tier, the model verdict on the current evidence and the decisions."""
    base = "accepted" if tier == "A" else {"agree": "model_accepted", "refutes": "model_rejected"}.get(verdict or "", "candidate")
    owner = [d for d in decisions if d["authority"] == "owner"]
    decision = (owner or decisions or [None])[-1]
    if decision is None:
        return base
    accept = decision["action"] == "accept"
    if decision["authority"] == "owner":
        return "human_accepted" if accept else "human_rejected"
    if (accept and verdict == "refutes") or (not accept and verdict == "agree"):
        return "contested"
    return "human_accepted" if accept else "human_rejected"


def load_state(db: sqlite3.Connection) -> dict[str, Any]:
    """Every link's current state plus the store versions (works on a read-only connection)."""
    version = int(db.execute("SELECT coalesce(max(version), 0) FROM link_version").fetchone()[0])
    build = db.execute("SELECT version, builder_version, registry_version FROM link_version WHERE operation = 'build' ORDER BY version DESC LIMIT 1").fetchone()
    build_version = None if build is None else int(build[0])
    served = {r[0] for r in db.execute("SELECT link_id FROM build_link WHERE version = ?", (build_version,))} if build_version else set()
    retracted = {r[0]: r[1] for r in db.execute("SELECT link_id, reason FROM retraction ORDER BY version")}
    reviews: dict[str, list[dict[str, Any]]] = {}
    for row in db.execute("SELECT review_id, link_id, evidence_sha, model, verdict, rationale, cited_fields, usd, version FROM review ORDER BY version, review_id"):
        reviews.setdefault(row[1], []).append({"id": row[0], "evidence_sha": row[2], "model": row[3], "verdict": row[4], "rationale": row[5],
                                               "cited_fields": json.loads(row[6]), "usd": row[7], "version": row[8]})
    decisions: dict[str, list[dict[str, Any]]] = {}
    for row in db.execute("SELECT decision_id, link_id, action, authority, rationale, evidence_sha, version FROM decision ORDER BY version, decision_id"):
        decisions.setdefault(row[1], []).append({"id": row[0], "action": row[2], "authority": row[3], "rationale": row[4], "evidence_sha": row[5], "version": row[6]})
    links: dict[str, LinkState] = {}
    for row in db.execute("SELECT link_id, pair, tier, builder, subject_id, object_id, evidence, evidence_sha, version FROM link ORDER BY row_id"):
        lid, pair, tier, builder, subj, obj, evidence, esha, v = row
        current = [r for r in reviews.get(lid, []) if r["evidence_sha"] == esha]
        review = current[-1] if current else None
        verdict = None if review is None else review["verdict"]
        ds = decisions.get(lid, [])
        is_served = lid in served
        links[lid] = LinkState(lid, pair, tier, builder, subj, obj, json.loads(evidence), esha, derive_status(tier, verdict, ds), is_served,
                               None if is_served else retracted.get(lid, "not_rebuilt"), verdict, None if review is None else review["id"],
                               ds[-1]["id"] if ds else None, int(v))
    return {
        "version": version,
        "build_version": build_version,
        "builder_version": None if build is None else build[1],
        "registry_version": None if build is None else build[2],
        "links": links,
        "reviews": reviews,
        "decisions": decisions,
    }


class LinkStore:
    """One link store file. Mutations run inside ``BEGIN IMMEDIATE`` and add one ``link_version``;
    reads open the file read-only (its digest is unchanged)."""

    def __init__(self, path: str | Path, *, now: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.now = now
        if not self.path.is_file():
            raise LinkStoreError(f"link store {self.path} does not exist (create it with `links build --create`)")
        with closing(self._read()) as db:
            try:
                meta = dict(db.execute("SELECT key, value FROM meta"))
            except sqlite3.DatabaseError as exc:
                raise LinkStoreError(f"{self.path} is not a link store ({exc})") from exc
        if meta.get("kind") != KIND or meta.get("format_version") != str(FORMAT_VERSION):
            raise LinkStoreError(f"{self.path} is not a link store of format {FORMAT_VERSION}")

    @classmethod
    def create(cls, path: str | Path, *, now: Callable[[], datetime] = utc_now) -> "LinkStore":
        path = Path(path)
        if path.exists():
            raise LinkStoreError(f"{path} already exists; nothing was changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO meta VALUES (?, ?)", [("kind", KIND), ("format_version", str(FORMAT_VERSION)), ("created_at", now().astimezone(timezone.utc).isoformat())])
            db.execute("COMMIT")
            db.executescript(append_only_triggers(TABLES))
        return cls(path, now=now)

    def _read(self) -> sqlite3.Connection:
        return sqlite3.connect(read_only_uri(self.path), uri=True)

    def _stamp(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat()

    def _mutate(self, operation: str, actor: str | None, body: Callable[[sqlite3.Connection, int], Any], *, note: str | None = None,
                builder_version: str | None = None, registry_version: str | None = None) -> Any:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=30)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute("INSERT INTO link_version (operation, actor, note, builder_version, registry_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                 (operation, actor, note, builder_version, registry_version, self._stamp()))
                result = body(db, int(cur.lastrowid))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return result

    # -- reads ---------------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with closing(self._read()) as db:
            return load_state(db)

    def link(self, link_id: str) -> LinkState | None:
        return self.state()["links"].get(link_id)

    # -- build ---------------------------------------------------------------------------------------

    def build(self, links: Iterable[Link], *, known_ids: Iterable[str], report: dict[str, Any] | None = None, registry_version: str | None = None,
              builder_version: str = BUILDER_VERSION, actor: str | None = None) -> dict[str, Any]:
        """Record one full build over the pinned collection: new/changed links get a definition
        row; links of the previous build that are not produced again are retracted (tombstoned when
        their subject or object is no longer in ``known_ids``)."""
        links = list(links)
        if len({x.link_id for x in links}) != len(links):
            raise LinkStoreError("a build must hold one link per (pair, subject, object)")
        known = set(known_ids)
        missing = [x.link_id for x in links if x.subject_id not in known or x.object_id not in known]
        if missing:
            raise LinkStoreError(f"{len(missing)} links name records outside the pinned collection (e.g. {missing[0]}); nothing was written")

        def body(db: sqlite3.Connection, version: int) -> dict[str, Any]:
            before = load_state(db)
            last = db.execute("SELECT max(version) FROM link_version WHERE operation = 'build' AND version < ?", (version,)).fetchone()[0]
            previous = {r[0] for r in db.execute("SELECT link_id FROM build_link WHERE version = ?", (last,))} if last is not None else set()
            counts: Counter[str] = Counter()
            for x in links:
                old = before["links"].get(x.link_id)
                if old is not None and (old.evidence_sha, old.tier, old.builder) == (x.evidence_sha, x.tier, x.builder):
                    counts["unchanged"] += 1
                else:
                    counts["added" if old is None else "changed"] += 1
                    db.execute("INSERT INTO link (link_id, pair, tier, builder, subject_id, object_id, evidence, evidence_sha, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (x.link_id, x.pair, x.tier, x.builder, x.subject_id, x.object_id, json.dumps(x.evidence, sort_keys=True, ensure_ascii=False), x.evidence_sha, version))
                db.execute("INSERT INTO build_link VALUES (?, ?)", (version, x.link_id))
            produced = {x.link_id for x in links}
            for lid in sorted(previous - produced):
                s = before["links"][lid]
                reason = "tombstoned_subject" if s.subject_id not in known else "tombstoned_object" if s.object_id not in known else "not_rebuilt"
                counts[reason] += 1
                db.execute("INSERT INTO retraction VALUES (?, ?, ?)", (version, lid, reason))
            db.execute("INSERT INTO build_report VALUES (?, ?)", (version, json.dumps(report or {}, sort_keys=True, default=str)))
            return {"link_version": f"l{version}", "links": len(links), **{k: counts.get(k, 0) for k in ("added", "changed", "unchanged", *RETRACTION_REASONS)}}

        return self._mutate("build", actor, body, builder_version=builder_version, registry_version=registry_version)

    # -- review and decisions ------------------------------------------------------------------------

    def record_review(self, link_id: str, evidence_sha: str, *, model: str, verdict: str, rationale: str, cited_fields: Iterable[str] = (),
                      usd: float = 0.0, actor: str | None = None) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise LinkStoreError(f"verdict must be one of {VERDICTS}")
        current = self.link(link_id)
        if current is None or not current.served:
            raise LinkStoreError(f"{link_id} is not a served link")
        if current.evidence_sha != evidence_sha:
            raise LinkStoreError(f"{link_id}: the reviewed evidence is not the current evidence (rebuilt meanwhile); nothing was recorded")
        cited = list(cited_fields)

        def body(db: sqlite3.Connection, version: int) -> str:
            rid = _hid("lrev", link_id, evidence_sha, model, version)
            db.execute("INSERT INTO review VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (rid, link_id, evidence_sha, model, verdict, rationale, json.dumps(cited), float(usd), version, self._stamp()))
            return rid

        rid = self._mutate("review", actor, body, note=link_id)
        state = self.link(link_id)
        return {"review": rid, "link": link_id, "status": None if state is None else state.status}

    def decide(self, link_id: str, action: str, *, rationale: str, authority: str = "coordinator", actor: str | None = None) -> dict[str, Any]:
        if action not in ACTIONS or authority not in AUTHORITIES:
            raise LinkStoreError(f"action must be one of {ACTIONS} and authority one of {AUTHORITIES}")
        if not rationale or not rationale.strip():
            raise LinkStoreError("a link decision needs a rationale")
        current = self.link(link_id)
        if current is None:
            raise LinkStoreError(f"unknown link {link_id!r}")

        def body(db: sqlite3.Connection, version: int) -> str:
            did = _hid("ldec", link_id, action, authority, version)
            db.execute("INSERT INTO decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (did, link_id, action, authority, rationale, current.evidence_sha, version, self._stamp()))
            return did

        did = self._mutate("decide", actor, body, note=link_id)
        state = self.link(link_id)
        return {"decision": did, "link": link_id, "status": None if state is None else state.status}

    # -- report / export -----------------------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        st = self.state()
        links = list(st["links"].values())
        served = [s for s in links if s.served]
        by_pair: dict[str, dict[str, int]] = {}
        for s in served:
            row = by_pair.setdefault(s.pair, {"total": 0, **{k: 0 for k in STATUSES}})
            row["total"] += 1
            row[s.status] += 1
        with closing(self._read()) as db:
            row = db.execute("SELECT report FROM build_report ORDER BY version DESC LIMIT 1").fetchone()
        reviews = [r for rs in st["reviews"].values() for r in rs]
        from .review import review_queue_states

        return {
            "link_version": f"l{st['version']}",
            "builder_version": st["builder_version"],
            "registry_version": st["registry_version"],
            "served": len(served),
            "by_status": {k: sum(1 for s in served if s.status == k) for k in STATUSES},
            "by_tier": dict(sorted(Counter(s.tier for s in served).items())),
            "by_pair": dict(sorted(by_pair.items())),
            "display": {k: sum(1 for s in served if DISPLAY[s.status] == k) for k in ("shown", "labelled", "hidden")},
            "retracted": dict(sorted(Counter(s.retracted for s in links if not s.served).items())),
            "owner_queue": sorted(s.link_id for s in served if s.status == "contested"),
            "review_queue": len(review_queue_states(st)),
            "reviews": len(reviews),
            "review_usd": round(sum(r["usd"] for r in reviews), 6),
            "last_build": None if row is None else json.loads(row[0]),
        }

    def export_data(self) -> dict[str, Any]:
        st = self.state()
        links = []
        for s in sorted(st["links"].values(), key=lambda x: (x.pair, x.subject_id, x.object_id)):
            entry = {k: v for k, v in s.as_dict().items() if v is not None}
            links.append(entry)
        data = {
            "format_version": FORMAT_VERSION,
            "kind": KIND,
            "link_version": f"l{st['version']}",
            "builder_version": st["builder_version"],
            "registry_version": st["registry_version"],
            "statuses": list(STATUSES),
            "display": DISPLAY,
            "links": links,
            "reviews": [{"link": lid, **r} for lid, rs in sorted(st["reviews"].items()) for r in rs],
            "decisions": [{"link": lid, **d} for lid, ds in sorted(st["decisions"].items()) for d in ds],
        }
        data["content_sha256"] = content_sha256(data)
        return data

    def export(self, out: str | Path, fmt: str = "yaml") -> dict[str, Any]:
        data = self.export_data()
        out = Path(out)
        if out.exists():
            raise LinkStoreError(f"{out} exists; exports never overwrite (pin a new file)")
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if fmt == "yaml" else json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return {"written": str(out), "format": fmt, "link_version": data["link_version"], "content_sha256": data["content_sha256"], "sha256": sha256_file(out)}


# --- serving view -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkView:
    """Query-time links of the pinned store. ``enabled`` is false (no links, ``reason`` set) for a
    missing store, a digest mismatch or an invalid export. ``withheld`` counts customer-derived
    links that the current identity view cannot vouch for (``limitations`` says why)."""

    enabled: bool
    reason: str | None = None
    link_version: str | None = None
    registry_version: str | None = None
    sha256: str | None = None
    links: tuple[LinkState, ...] = ()
    show_candidates: str = "labelled"
    max_per_record: int = 5
    limitations: tuple[str, ...] = ()
    withheld: int = 0

    @classmethod
    def pinned(cls, path: str | Path, sha256: str, *, identity: Any = None, show_candidates: str = "labelled", max_per_record: int = 5) -> "LinkView":
        path = Path(path)
        common = {"show_candidates": show_candidates, "max_per_record": max_per_record}
        if not path.is_file():
            return cls(False, "link_store_missing", **common)
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != sha256:
            return cls(False, "digest_mismatch", sha256=digest, **common)
        if blob.startswith(b"SQLite format 3\x00"):
            db = sqlite3.connect(":memory:")
            try:
                db.deserialize(blob)
                st = load_state(db)
            finally:
                db.close()
            states = [s for s in st["links"].values() if s.served]
            link_version, registry_version = f"l{st['version']}", st["registry_version"]
        else:
            data = yaml.safe_load(blob.decode("utf-8"))
            if not isinstance(data, dict) or data.get("kind") != KIND or data.get("format_version") != FORMAT_VERSION or data.get("content_sha256") != content_sha256(data):
                return cls(False, "export_invalid", sha256=digest, **common)
            fields = set(LinkState.__dataclass_fields__)
            states = [LinkState(**{k: v for k, v in e.items() if k in fields}) for e in data["links"] if e.get("served")]
            link_version, registry_version = str(data["link_version"]), data.get("registry_version")
        limitations: list[str] = []
        withheld = 0
        if any(s.customer_derived for s in states):
            joins = identity is not None and getattr(identity, "joins_enabled", False)
            if not joins:
                reason = "no identity view" if identity is None else f"identity joins disabled ({identity.reason})"
                limitations.append(f"links derived from a customer match are withheld: {reason}")
            elif identity.registry_version != registry_version:
                limitations.append(f"links derived from a customer match are withheld: built with identity registry {registry_version}, "
                                   f"pinned registry is {identity.registry_version} (rebuild the links)")
            if limitations:
                withheld = sum(1 for s in states if s.customer_derived)
                states = [s for s in states if not s.customer_derived]
        return cls(True, None, link_version, registry_version, digest, tuple(states), show_candidates, max_per_record, tuple(limitations), withheld)

    def visible(self, state: LinkState) -> bool:
        mode = DISPLAY[state.status]
        return mode == "shown" or (mode == "labelled" and self.show_candidates == "labelled")

    def links_for(self, record_id: str) -> list[dict[str, Any]]:
        """The display entries of one record (subject or object side), visible statuses only,
        ordered (status, tier, id) and capped at ``max_per_record``."""
        if not self.enabled:
            return []
        hits = [s for s in self.links if record_id in (s.subject_id, s.object_id) and self.visible(s)]
        hits.sort(key=lambda s: (STATUS_ORDER[s.status], s.tier, s.link_id))
        return [self.entry(s, record_id) for s in hits[: self.max_per_record]]

    def entry(self, s: LinkState, record_id: str | None = None) -> dict[str, Any]:
        other = None if record_id is None else (s.object_id if s.subject_id == record_id else s.subject_id)
        return {"link_id": s.link_id, "pair": s.pair, "tier": s.tier, "builder": s.builder, "subject_id": s.subject_id, "object_id": s.object_id,
                "other_id": other, "status": s.status, "label": LABELS.get(s.status), "labelled": DISPLAY[s.status] == "labelled",
                "evidence": s.evidence, "link_version": self.link_version}

    def rows(self) -> list[dict[str, Any]]:
        """Visible links as rows (the future ``link`` SQL view, M4)."""
        if not self.enabled:
            return []
        return [self.entry(s) for s in sorted(self.links, key=lambda s: (s.pair, s.subject_id, s.object_id)) if self.visible(s)]
