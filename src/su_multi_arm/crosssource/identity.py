"""Identity registry v1 (unified merge plan §6.9, increment M2b-1).

A **private**, append-only SQLite registry that maps raw customer values of each namespace
(``support.account`` ticket organization, ``logs.tenant`` log client/tenant,
``analytics.company_id``, ``tracker.customer`` ...) to registry customers. It lives outside the
repository; only synthetic fixtures are committed.

Tables (every one append-only: ``UPDATE``/``DELETE`` are refused by triggers; the current state of
an alias is its latest row):

* ``registry_version`` — one row per mutating operation; every other row carries its version.
* ``customer`` (id, name), ``curated`` (a coordinator-supplied curated alias list: candidates only).
* ``classification`` — the deterministic outcome of ``identity_candidates.classify`` per
  ``(namespace, raw)`` (plus records lacking a value and occurrence counts when known).
* ``evidence`` — web / source field / curated list, with URL, retrieval date and quote (private).
* ``decision`` — coordinator (or owner) decisions ``match | exclude | reject``; **every** decision
  needs evidence.
* ``review`` — model reviews ``agree | refutes | uncertain`` with rationale, cited fields and cost.
* ``alias`` — the derived state (status ``matched | candidate | contested | ambiguous | excluded |
  unmatched | missing``, rule, authority, confidence, evidence ids, review/decision ids, effective
  dates, version).

Derivation (``derive``): only an ``exact``/``casefold`` unique hit auto-matches; any other match
needs a coordinator ``match`` decision with evidence **and** an ``agree`` review (unless the serving
configuration opts into ``crosssource.identity.auto_match: fuzzy``: then ``IdentityView`` also joins
``candidate`` aliases resolved to one customer at or above ``fuzzy_min_confidence``, rule
``fuzzy:<rule>``; the registry rows are unchanged); a ``refutes``
review makes the alias ``contested`` (no join; owner queue) and only an owner decision resolves it.
Casefold auto-matches are reviewed too (non-gating: a ``refutes`` contests them). Only ``matched``
aliases join (``IdentityView``), through the pinned registry file: a digest mismatch disables every
cross-source join (fail closed, §6.12). Identity never grants access: nothing here reads or writes
visibility, ACLs or principals.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from ..core.errors import BudgetExhausted, SuError
from ..models import StrictModel
from .identity_candidates import AUTO_MATCH_RULES, Classification, Customer, classify, normalize
from .review_ledger import ReviewLedger, ReviewMeter, RunCap, append_only_triggers, read_only_uri, worst_case_usd

FORMAT_VERSION = 1
STATUSES = ("matched", "candidate", "contested", "ambiguous", "excluded", "unmatched", "missing")
JOINABLE_STATUSES = ("matched",)
EVIDENCE_KINDS = ("web", "source_field", "curated_list")
ACTIONS = ("match", "exclude", "reject")
AUTHORITIES = ("coordinator", "owner")
VERDICTS = ("agree", "refutes", "uncertain")
NAMESPACE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
CUSTOMER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
REVIEW_SCOPE = "identity"


class IdentityError(SuError, ValueError):
    """A registry operation was refused (nothing was changed)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def subject(namespace: str, raw: str | None) -> str:
    """``alias:<namespace>:<raw>`` (empty raw for the ``missing`` entry of a namespace)."""
    return f"alias:{namespace}:{raw or ''}"


def parse_subject(value: str) -> tuple[str, str | None]:
    prefix, _, rest = value.partition(":")
    namespace, sep, raw = rest.partition(":")
    if prefix != "alias" or not sep or not NAMESPACE.match(namespace):
        raise IdentityError(f"not an alias subject: {value!r}")
    return namespace, raw or None


def _hid(prefix: str, *parts: Any) -> str:
    return f"{prefix}-" + hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def check_namespace(namespace: str) -> str:
    if not NAMESPACE.match(namespace):
        raise IdentityError(f"namespace {namespace!r} must look like 'source.field' (lower case)")
    return namespace


# --- input models ----------------------------------------------------------------------------------


class Evidence(StrictModel):
    """One piece of evidence. ``web`` needs an http(s) URL, the retrieval date and a quote;
    ``source_field`` a URL or a field name; ``curated_list`` a quote. Private: it stays in the
    registry and is sent only to the configured reviewer role."""

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
    kind: Literal["web", "source_field", "curated_list"]
    url: str | None = Field(default=None, max_length=2000)
    retrieved: date | None = None
    quote: str = Field(min_length=1, max_length=2000)
    field: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^https?://[^\s]+$", value):
            raise ValueError("evidence url must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def kind_fields(self):
        if self.kind == "web" and (self.url is None or self.retrieved is None):
            raise ValueError("web evidence needs url and retrieved (retrieval date)")
        if self.kind == "source_field" and self.url is None and self.field is None:
            raise ValueError("source_field evidence needs the field name or a url")
        return self

    def row(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "url": self.url, "retrieved": None if self.retrieved is None else self.retrieved.isoformat(),
                "quote": self.quote, "field": self.field}


class ReviewReply(StrictModel):
    """The reviewer's structured reply."""

    verdict: Literal["agree", "refutes", "uncertain"]
    rationale: str = Field(min_length=1, max_length=1000)
    cited_fields: list[str] = Field(default_factory=list, max_length=20)


# --- derived state ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    decision_id: str
    namespace: str
    raw: str | None
    action: str
    customer_id: str | None
    authority: str
    rationale: str
    evidence: tuple[str, ...]
    confidence: float
    version: int


@dataclass(frozen=True)
class Review:
    review_id: str
    namespace: str
    raw: str | None
    customer_id: str | None
    model: str
    verdict: str
    rationale: str
    cited_fields: tuple[str, ...]
    usd: float
    version: int


@dataclass(frozen=True)
class AliasState:
    namespace: str
    raw: str | None
    normalized: str | None
    status: str
    rule: str
    authority: str
    confidence: float
    customer_id: str | None = None
    candidates: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    review: str | None = None
    decision: str | None = None
    records: tuple[str, ...] = ()

    @property
    def subject(self) -> str:
        return subject(self.namespace, self.raw)

    def same(self, other: "AliasState | None") -> bool:
        return other is not None and asdict(self) == asdict(other)


def derive(c: Classification, decisions: list[Decision], reviews: list[Review]) -> AliasState:
    """The alias state from its classification, decisions and reviews (module docstring)."""
    base = dict(namespace=c.namespace, raw=c.raw, normalized=c.normalized, records=c.records)

    def latest_review(customer: str | None) -> Review | None:
        hits = [r for r in reviews if r.customer_id == customer]
        return hits[-1] if hits else None

    owner = [d for d in decisions if d.authority == "owner"]
    decision = (owner or decisions or [None])[-1]
    if decision is not None:
        common = dict(base, rule="decision", authority=decision.authority, confidence=decision.confidence, evidence=decision.evidence, decision=decision.decision_id)
        if decision.action == "exclude":
            return AliasState(status="excluded", **common)
        if decision.action == "reject":
            return AliasState(status="unmatched", **common)
        review = latest_review(decision.customer_id)
        rid = None if review is None else review.review_id
        if decision.authority == "owner" or (review is not None and review.verdict == "agree"):
            status = "matched"
        elif review is not None and review.verdict == "refutes":
            status = "contested"
        else:
            status = "candidate"
        return AliasState(status=status, customer_id=decision.customer_id, review=rid, **common)
    common = dict(base, rule=c.rule, authority="deterministic", confidence=c.confidence)
    if c.status in ("matched", "candidate"):
        review = latest_review(c.customer_id)
        status = "contested" if review is not None and review.verdict == "refutes" else c.status
        return AliasState(status=status, customer_id=c.customer_id, review=None if review is None else review.review_id, **common)
    return AliasState(status=c.status, candidates=c.candidates, **common)


# --- the registry ----------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS registry_version (
    version INTEGER PRIMARY KEY, operation TEXT NOT NULL, actor TEXT, note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS curated (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, raw TEXT NOT NULL, customer_id TEXT NOT NULL,
    evidence_id TEXT, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS classification (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, raw_key TEXT NOT NULL, raw TEXT, normalized TEXT,
    status TEXT NOT NULL, rule TEXT NOT NULL, confidence REAL NOT NULL, customer_id TEXT, candidates TEXT NOT NULL,
    records TEXT NOT NULL, occurrences INTEGER, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('web', 'source_field', 'curated_list')), url TEXT,
    retrieved TEXT, quote TEXT NOT NULL, field TEXT, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS decision (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id TEXT NOT NULL UNIQUE, namespace TEXT NOT NULL, raw_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('match', 'exclude', 'reject')), customer_id TEXT,
    authority TEXT NOT NULL CHECK (authority IN ('coordinator', 'owner')), rationale TEXT NOT NULL, evidence TEXT NOT NULL,
    confidence REAL NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE, namespace TEXT NOT NULL, raw_key TEXT NOT NULL,
    customer_id TEXT, model TEXT NOT NULL, verdict TEXT NOT NULL CHECK (verdict IN ('agree', 'refutes', 'uncertain')),
    rationale TEXT NOT NULL, cited_fields TEXT NOT NULL, usd REAL NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alias (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, raw_key TEXT NOT NULL, raw TEXT, normalized TEXT,
    customer_id TEXT, candidates TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('matched', 'candidate', 'contested', 'ambiguous', 'excluded', 'unmatched', 'missing')),
    rule TEXT NOT NULL, authority TEXT NOT NULL, confidence REAL NOT NULL, evidence TEXT NOT NULL, review_id TEXT, decision_id TEXT,
    records TEXT NOT NULL, effective_from TEXT NOT NULL, version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alias_key ON alias (namespace, raw_key, row_id);
CREATE INDEX IF NOT EXISTS classification_key ON classification (namespace, raw_key, row_id);
"""
TABLES = ("meta", "registry_version", "customer", "curated", "classification", "evidence", "decision", "review", "alias")


def _key(raw: str | None) -> str:
    return raw or ""


def _j(value: Any) -> str:
    return json.dumps(list(value), ensure_ascii=False)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class IdentityRegistry:
    """One registry file. Mutating operations run inside ``BEGIN IMMEDIATE`` and add one
    ``registry_version``; read operations open the file read-only (its digest is unchanged)."""

    def __init__(self, path: str | Path, *, now: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.now = now
        if not self.path.is_file():
            raise IdentityError(f"identity registry {self.path} does not exist (create it with `identity candidates --create` or `identity import`)")
        with closing(self._read()) as db:
            meta = dict(db.execute("SELECT key, value FROM meta"))
        if meta.get("format_version") != str(FORMAT_VERSION) or meta.get("kind") != "su_multi_arm.identity_registry":
            raise IdentityError(f"{self.path} is not an identity registry of format {FORMAT_VERSION}")

    @classmethod
    def create(cls, path: str | Path, *, now: Callable[[], datetime] = utc_now) -> "IdentityRegistry":
        path = Path(path)
        if path.exists():
            raise IdentityError(f"{path} already exists; nothing was changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.executemany("INSERT INTO meta VALUES (?, ?)", [("kind", "su_multi_arm.identity_registry"), ("format_version", str(FORMAT_VERSION)),
                                                             ("created_at", now().astimezone(timezone.utc).isoformat())])
            db.execute("COMMIT")
            db.executescript(append_only_triggers(TABLES))
        return cls(path, now=now)

    # -- connections -------------------------------------------------------------------------------

    def _read(self) -> sqlite3.Connection:
        return sqlite3.connect(read_only_uri(self.path), uri=True)

    def _write(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN IMMEDIATE")
        return db

    def _version(self, db: sqlite3.Connection, operation: str, actor: str | None, note: str | None = None) -> int:
        cur = db.execute("INSERT INTO registry_version (operation, actor, note, created_at) VALUES (?, ?, ?, ?)",
                         (operation, actor, note, self.now().astimezone(timezone.utc).isoformat()))
        return int(cur.lastrowid)

    def _mutate(self, operation: str, actor: str | None, body: Callable[[sqlite3.Connection, int], Any], note: str | None = None) -> Any:
        with closing(self._write()) as db:
            try:
                version = self._version(db, operation, actor, note)
                result = body(db, version)
                self._rederive(db, version)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return result

    # -- loading -----------------------------------------------------------------------------------

    @staticmethod
    def _customers(db: sqlite3.Connection) -> list[Customer]:
        latest: dict[str, str] = {}
        for cid, name in db.execute("SELECT customer_id, name FROM customer ORDER BY row_id"):
            latest[cid] = name
        return [Customer(cid, name) for cid, name in sorted(latest.items())]

    @staticmethod
    def _curated(db: sqlite3.Connection) -> dict[tuple[str, str], str]:
        return {(ns, raw): cid for ns, raw, cid in db.execute("SELECT namespace, raw, customer_id FROM curated ORDER BY row_id")}

    @staticmethod
    def _classifications(db: sqlite3.Connection) -> dict[tuple[str, str], tuple[Classification, int | None]]:
        out: dict[tuple[str, str], tuple[Classification, int | None]] = {}
        for row in db.execute("SELECT namespace, raw_key, raw, normalized, status, rule, confidence, customer_id, candidates, records, occurrences "
                              "FROM classification ORDER BY row_id"):
            ns, key, raw, norm, status, rule, conf, cid, cands, records, occ = row
            out[(ns, key)] = (Classification(ns, raw, norm, status, rule, conf, cid, tuple(json.loads(cands)), tuple(json.loads(records))), occ)
        return out

    @staticmethod
    def _decisions(db: sqlite3.Connection) -> dict[tuple[str, str], list[Decision]]:
        out: dict[tuple[str, str], list[Decision]] = {}
        for row in db.execute("SELECT decision_id, namespace, raw_key, action, customer_id, authority, rationale, evidence, confidence, version FROM decision ORDER BY row_id"):
            did, ns, key, action, cid, authority, rationale, ev, conf, version = row
            out.setdefault((ns, key), []).append(Decision(did, ns, key or None, action, cid, authority, rationale, tuple(json.loads(ev)), conf, version))
        return out

    @staticmethod
    def _reviews(db: sqlite3.Connection) -> dict[tuple[str, str], list[Review]]:
        out: dict[tuple[str, str], list[Review]] = {}
        for row in db.execute("SELECT review_id, namespace, raw_key, customer_id, model, verdict, rationale, cited_fields, usd, version FROM review ORDER BY row_id"):
            rid, ns, key, cid, model, verdict, rationale, cited, usd, version = row
            out.setdefault((ns, key), []).append(Review(rid, ns, key or None, cid, model, verdict, rationale, tuple(json.loads(cited)), usd, version))
        return out

    @staticmethod
    def _aliases(db: sqlite3.Connection) -> dict[tuple[str, str], tuple[AliasState, str, int]]:
        out: dict[tuple[str, str], tuple[AliasState, str, int]] = {}
        for row in db.execute("SELECT namespace, raw_key, raw, normalized, customer_id, candidates, status, rule, authority, confidence, evidence, "
                              "review_id, decision_id, records, effective_from, version FROM alias ORDER BY row_id"):
            ns, key, raw, norm, cid, cands, status, rule, authority, conf, ev, rid, did, records, eff, version = row
            state = AliasState(ns, raw, norm, status, rule, authority, conf, cid, tuple(json.loads(cands)), tuple(json.loads(ev)), rid, did, tuple(json.loads(records)))
            out[(ns, key)] = (state, eff, version)
        return out

    def _rederive(self, db: sqlite3.Connection, version: int) -> None:
        current = self._aliases(db)
        decisions, reviews = self._decisions(db), self._reviews(db)
        today = self.now().astimezone(timezone.utc).date().isoformat()
        for key, (c, _occ) in sorted(self._classifications(db).items()):
            state = derive(c, decisions.get(key, []), reviews.get(key, []))
            if state.same(current.get(key, (None,))[0]):
                continue
            db.execute("INSERT INTO alias (namespace, raw_key, raw, normalized, customer_id, candidates, status, rule, authority, confidence, evidence, "
                       "review_id, decision_id, records, effective_from, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (state.namespace, key[1], state.raw, state.normalized, state.customer_id, _j(state.candidates), state.status, state.rule,
                        state.authority, state.confidence, _j(state.evidence), state.review, state.decision, _j(state.records), today, version))

    def _classify_into(self, db: sqlite3.Connection, version: int, items: Iterable[tuple[Classification, int | None]]) -> dict[str, int]:
        existing = self._classifications(db)
        changed = 0
        for c, occ in items:
            key = (c.namespace, _key(c.raw))
            if key in existing and existing[key] == (c, occ):
                continue
            db.execute("INSERT INTO classification (namespace, raw_key, raw, normalized, status, rule, confidence, customer_id, candidates, records, "
                       "occurrences, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (c.namespace, key[1], c.raw, c.normalized, c.status, c.rule, c.confidence, c.customer_id, _j(c.candidates), _j(c.records), occ, version))
            changed += 1
        return {"classified": changed}

    def _reclassify_all(self, db: sqlite3.Connection, version: int) -> dict[str, int]:
        customers, curated = self._customers(db), self._curated(db)
        items = [(classify(c.namespace, c.raw, customers, curated, c.records), occ) for c, occ in self._classifications(db).values()]
        return self._classify_into(db, version, items)

    def version(self) -> int:
        with closing(self._read()) as db:
            return int(db.execute("SELECT coalesce(max(version), 0) FROM registry_version").fetchone()[0])

    # -- mutations ---------------------------------------------------------------------------------

    def add_customers(self, customers: Iterable[Customer | dict[str, str]], *, actor: str | None = None) -> dict[str, Any]:
        items = [c if isinstance(c, Customer) else Customer(str(c["customer_id"]), str(c["name"])) for c in customers]
        for c in items:
            if not CUSTOMER_ID.match(c.customer_id) or not c.name.strip():
                raise IdentityError(f"invalid customer {c.customer_id!r} / {c.name!r}")

        def body(db: sqlite3.Connection, version: int) -> dict[str, Any]:
            known = {c.customer_id: c.name for c in self._customers(db)}
            added = [c for c in items if known.get(c.customer_id) != c.name]
            db.executemany("INSERT INTO customer (customer_id, name, version) VALUES (?, ?, ?)", [(c.customer_id, c.name, version) for c in added])
            return {"customers_added": len(added), **self._reclassify_all(db, version)}

        return self._mutate("customers", actor, body)

    def seed_customers(self, names: Iterable[str], *, prefix: str = "cust:", actor: str | None = None) -> dict[str, Any]:
        """Owner rule (tenant name = organization name): every organization name without a
        casefold-equal customer becomes a customer ``<prefix><slug>``."""
        with closing(self._read()) as db:
            known = self._customers(db)
        seen = {normalize(c.name) for c in known}
        ids = {c.customer_id for c in known}
        new: list[Customer] = []
        for name in sorted({n for n in names if n and n.strip()}):
            if normalize(name) in seen:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", normalize(name) or "").strip("-")[:100] or _hid("c", name)
            cid = prefix + slug
            if cid in ids:
                cid = f"{cid}-{_hid('c', name)[2:8]}"
            seen.add(normalize(name))
            ids.add(cid)
            new.append(Customer(cid, name))
        return self.add_customers(new, actor=actor)

    def add_curated(self, entries: Iterable[dict[str, Any]], evidence: Iterable[Evidence] = (), *, actor: str | None = None) -> dict[str, Any]:
        rows = [(check_namespace(str(e["namespace"])), str(e["raw"]), str(e["customer_id"]), e.get("evidence")) for e in entries]

        def body(db: sqlite3.Connection, version: int) -> dict[str, Any]:
            self._add_evidence(db, version, list(evidence))
            known = {c.customer_id for c in self._customers(db)}
            unknown = sorted({cid for _, _, cid, _ in rows if cid not in known})
            if unknown:
                raise IdentityError(f"curated aliases name unknown customers: {unknown}")
            db.executemany("INSERT INTO curated (namespace, raw, customer_id, evidence_id, version) VALUES (?, ?, ?, ?, ?)", [(*r, version) for r in rows])
            return {"curated_added": len(rows), **self._reclassify_all(db, version)}

        return self._mutate("curated", actor, body)

    def observe(self, namespace: str, values: dict[str | None, int | None] | Iterable[str | None], *, missing_records: Iterable[str] = (),
                actor: str | None = None) -> dict[str, Any]:
        """Classify the raw values seen in a namespace (``{raw: occurrences}`` or a list of raws);
        ``missing_records`` are the record ids that carry no value."""
        check_namespace(namespace)
        counts = dict(values) if isinstance(values, dict) else {v: None for v in values}
        missing = sorted(set(missing_records))

        def body(db: sqlite3.Connection, version: int) -> dict[str, Any]:
            customers, curated = self._customers(db), self._curated(db)
            if not customers:
                raise IdentityError("the registry has no customers; add or seed them first")
            items: list[tuple[Classification, int | None]] = []
            for raw in sorted(r for r in counts if r is not None and r.strip()):
                items.append((classify(namespace, raw, customers, curated), counts[raw]))
            if missing or any(r is None or not r.strip() for r in counts):
                previous = self._classifications(db).get((namespace, ""))
                records = sorted(set(missing) | (set(previous[0].records) if previous else set()))
                items.append((classify(namespace, None, customers, records=records), len(records) or None))
            result = self._classify_into(db, version, items)
            statuses: dict[str, int] = {}
            for c, _ in items:
                statuses[c.status] = statuses.get(c.status, 0) + 1
            return {"namespace": namespace, "values": len(items), **result, "deterministic": dict(sorted(statuses.items()))}

        return self._mutate("candidates", actor, body, note=namespace)

    @staticmethod
    def _add_evidence(db: sqlite3.Connection, version: int, evidence: list[Evidence]) -> list[str]:
        ids = []
        for ev in evidence:
            row = ev.row()
            existing = db.execute("SELECT kind, url, retrieved, quote, field FROM evidence WHERE evidence_id = ?", (ev.id,)).fetchone()
            if existing is not None:
                if existing != (row["kind"], row["url"], row["retrieved"], row["quote"], row["field"]):
                    raise IdentityError(f"evidence {ev.id!r} already exists with other content (evidence is append-only; use a new id)")
            else:
                db.execute("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", (ev.id, row["kind"], row["url"], row["retrieved"], row["quote"], row["field"], version))
            ids.append(ev.id)
        return ids

    def decide(self, namespace: str, raw: str | None, action: str, *, evidence: Iterable[Evidence | str], rationale: str,
               customer_id: str | None = None, authority: str = "coordinator", confidence: float = 0.9, actor: str | None = None) -> dict[str, Any]:
        """Record a coordinator (or owner) decision. Every decision needs evidence; ``match`` needs a
        known customer and becomes ``matched`` only after an ``agree`` review (owner: at once)."""
        check_namespace(namespace)
        if action not in ACTIONS or authority not in AUTHORITIES:
            raise IdentityError(f"action must be one of {ACTIONS} and authority one of {AUTHORITIES}")
        if (action == "match") != (customer_id is not None):
            raise IdentityError("a match decision names exactly one customer; exclude/reject name none")
        if not rationale or not rationale.strip():
            raise IdentityError("a decision needs a rationale")
        if not 0.0 <= confidence <= 1.0:
            raise IdentityError("confidence must be in [0, 1]")
        new = [e for e in evidence if isinstance(e, Evidence)]
        refs = [e for e in evidence if isinstance(e, str)]
        if not new and not refs:
            raise IdentityError("every identity decision needs evidence (web, source field or curated list)")
        key = (namespace, _key(raw))

        def body(db: sqlite3.Connection, version: int) -> dict[str, Any]:
            if key not in self._classifications(db):
                raise IdentityError(f"{subject(namespace, raw)} has not been observed; run `identity candidates` for it first")
            if customer_id is not None and customer_id not in {c.customer_id for c in self._customers(db)}:
                raise IdentityError(f"unknown customer {customer_id!r}")
            ids = self._add_evidence(db, version, new)
            for ref in refs:
                if db.execute("SELECT 1 FROM evidence WHERE evidence_id = ?", (ref,)).fetchone() is None:
                    raise IdentityError(f"unknown evidence id {ref!r}")
            ev = tuple(dict.fromkeys([*ids, *refs]))
            did = _hid("dec", namespace, raw, action, customer_id, authority, version)
            db.execute("INSERT INTO decision (decision_id, namespace, raw_key, action, customer_id, authority, rationale, evidence, confidence, version, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (did, namespace, key[1], action, customer_id, authority, rationale.strip(), _j(ev), confidence, version,
                        self.now().astimezone(timezone.utc).isoformat()))
            return {"decision": did}

        result = self._mutate("decide", actor, body, note=subject(namespace, raw))
        return {**result, "alias": self.alias(namespace, raw)}

    def record_review(self, namespace: str, raw: str | None, customer_id: str | None, *, model: str, verdict: str, rationale: str,
                      cited_fields: Iterable[str] = (), usd: float = 0.0, review_id: str | None = None, actor: str | None = None) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise IdentityError(f"verdict must be one of {VERDICTS}")
        key = (namespace, _key(raw))

        def body(db: sqlite3.Connection, version: int) -> str:
            if key not in self._classifications(db):
                raise IdentityError(f"{subject(namespace, raw)} has not been observed")
            rid = review_id or _hid("rev", namespace, raw, customer_id, model, verdict, version)
            db.execute("INSERT INTO review (review_id, namespace, raw_key, customer_id, model, verdict, rationale, cited_fields, usd, version, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (rid, namespace, key[1], customer_id, model, verdict, rationale, _j(cited_fields), float(usd), version,
                        self.now().astimezone(timezone.utc).isoformat()))
            return rid

        rid = self._mutate("review", actor, body, note=subject(namespace, raw))
        return {"review": rid, "alias": self.alias(namespace, raw)}

    # -- reads -------------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with closing(self._read()) as db:
            version = int(db.execute("SELECT coalesce(max(version), 0) FROM registry_version").fetchone()[0])
            evidence = [dict(zip(("id", "kind", "url", "retrieved", "quote", "field"), row))
                        for row in db.execute("SELECT evidence_id, kind, url, retrieved, quote, field FROM evidence ORDER BY evidence_id")]
            curated = [dict(zip(("namespace", "raw", "customer_id", "evidence"), row))
                       for row in db.execute("SELECT namespace, raw, customer_id, evidence_id FROM curated ORDER BY row_id")]
            return {
                "version": version,
                "customers": self._customers(db),
                "curated": curated,
                "classifications": self._classifications(db),
                "aliases": self._aliases(db),
                "decisions": self._decisions(db),
                "reviews": self._reviews(db),
                "evidence": evidence,
            }

    def alias(self, namespace: str, raw: str | None) -> dict[str, Any] | None:
        with closing(self._read()) as db:
            hit = self._aliases(db).get((namespace, _key(raw)))
        return None if hit is None else alias_dict(*hit)

    def report(self) -> dict[str, Any]:
        """Per-namespace counts with denominators, the owner queue and the review queue."""
        st = self.state()
        namespaces: dict[str, dict[str, Any]] = {}
        for (ns, key), (state, _eff, _v) in sorted(st["aliases"].items()):
            row = namespaces.setdefault(ns, {"values": 0, **{s: 0 for s in STATUSES}, "_occ": [], "missing_records": 0})
            row["values"] += 1
            row[state.status] += 1
            c, occ = st["classifications"][(ns, key)]
            if state.status == "missing":
                row["missing_records"] = len(c.records)
            else:
                row["_occ"].append((state.status, occ))
        for row in namespaces.values():
            occ = row.pop("_occ")
            known = all(o is not None for _, o in occ)
            row["joinable_values"] = row["matched"]
            row["matched_share_of_values"] = round(row["matched"] / row["values"], 4) if row["values"] else None
            row["records"] = (sum(o for _, o in occ) + row["missing_records"]) if known else None
            row["joinable_records"] = sum(o for s, o in occ if s in JOINABLE_STATUSES) if known else None
        totals = {s: sum(r[s] for r in namespaces.values()) for s in ("values", *STATUSES)}
        queue = review_queue(self)
        reviews = [r for rs in st["reviews"].values() for r in rs]
        return {
            "registry_version": f"r{st['version']}",
            "grants_access": False,
            "customers": len(st["customers"]),
            "namespaces": namespaces,
            "totals": totals,
            "owner_queue": [s.subject for s, _e, _v in st["aliases"].values() if s.status == "contested"],
            "review_queue": len(queue),
            "awaiting_coordinator": [s.subject for s, _e, _v in st["aliases"].values() if s.status in ("candidate", "ambiguous") and s.decision is None],
            "decisions": sum(len(d) for d in st["decisions"].values()),
            "reviews": len(reviews),
            "review_usd": round(sum(r.usd for r in reviews), 6),
        }

    # -- export / import ---------------------------------------------------------------------------

    def export_data(self) -> dict[str, Any]:
        st = self.state()
        aliases = []
        for (ns, key), (state, eff, version) in sorted(st["aliases"].items()):
            entry = alias_dict(state, eff, version)
            occ = st["classifications"][(ns, key)][1]
            if occ is not None:
                entry["occurrences"] = occ
            aliases.append(entry)
        decisions = [{"id": d.decision_id, "subject": subject(d.namespace, d.raw), "action": d.action, "customer_id": d.customer_id, "authority": d.authority,
                      "rationale": d.rationale, "evidence": list(d.evidence), "confidence": d.confidence, "version": d.version}
                     for ds in st["decisions"].values() for d in ds]
        reviews = [{"id": r.review_id, "subject": subject(r.namespace, r.raw), "customer_id": r.customer_id, "model": r.model, "verdict": r.verdict,
                    "rationale": r.rationale, "cited_fields": list(r.cited_fields), "usd": r.usd, "version": r.version}
                   for rs in st["reviews"].values() for r in rs]
        data = {
            "format_version": FORMAT_VERSION,
            "registry_version": f"r{st['version']}",
            "grants_access": False,
            "statuses": list(STATUSES),
            "auto_match_rules": list(AUTO_MATCH_RULES),
            "customers": [{"customer_id": c.customer_id, "name": c.name} for c in st["customers"]],
            "curated": st["curated"],
            "aliases": aliases,
            "evidence": [{k: v for k, v in e.items() if v is not None} for e in st["evidence"]],
            "decisions": sorted(decisions, key=lambda d: (d["version"], d["id"])),
            "reviews": sorted(reviews, key=lambda r: (r["version"], r["id"])),
        }
        data["content_sha256"] = content_sha256(data)
        return data

    def export(self, out: str | Path, fmt: str = "yaml") -> dict[str, Any]:
        data = self.export_data()
        out = Path(out)
        if out.exists():
            raise IdentityError(f"{out} exists; exports never overwrite (pin a new file)")
        text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True) if fmt == "yaml" else json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return {"written": str(out), "format": fmt, "registry_version": data["registry_version"], "content_sha256": data["content_sha256"], "sha256": sha256_file(out)}


IMPORT_KEYS = {"format_version", "registry_version", "synthetic", "statuses", "auto_match_rules", "customers", "aliases", "evidence", "reviews",
               "expected", "decisions", "curated", "grants_access", "content_sha256"}


def import_registry(path: str | Path, data: dict[str, Any], *, now: Callable[[], datetime] = utc_now, actor: str | None = None) -> IdentityRegistry:
    """Create a registry at ``path`` from an export (or the F0 fixture format). Deterministic
    entries are re-classified and must agree; coordinator entries become decisions; the derived
    state must reproduce every imported status, or nothing is written."""
    unknown = sorted(set(data) - IMPORT_KEYS)
    if unknown or data.get("format_version") != FORMAT_VERSION:
        raise IdentityError(f"not an identity export of format {FORMAT_VERSION} (unknown keys {unknown})")
    if "content_sha256" in data and data["content_sha256"] != content_sha256(data):
        raise IdentityError("the export's content_sha256 does not match its content")
    path = Path(path)
    registry = IdentityRegistry.create(path, now=now)
    try:
        _import_into(registry, data, actor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return registry


def _import_into(registry: IdentityRegistry, data: dict[str, Any], actor: str | None) -> None:
    customers = [Customer(str(c["customer_id"]), str(c["name"])) for c in data.get("customers") or []]
    evidence = [Evidence.model_validate(e) for e in data.get("evidence") or []]
    aliases = data.get("aliases") or []
    curated = {(c["namespace"], c["raw"]): c["customer_id"] for c in data.get("curated") or []}
    problems: list[str] = []

    def body(db: sqlite3.Connection, version: int) -> None:
        db.executemany("INSERT INTO customer (customer_id, name, version) VALUES (?, ?, ?)", [(c.customer_id, c.name, version) for c in customers])
        registry._add_evidence(db, version, evidence)
        db.executemany("INSERT INTO curated (namespace, raw, customer_id, evidence_id, version) VALUES (?, ?, ?, ?, ?)",
                       [(c["namespace"], c["raw"], c["customer_id"], c.get("evidence"), version) for c in data.get("curated") or []])
        items = []
        for a in aliases:
            ns = check_namespace(str(a["namespace"]))
            c = classify(ns, a.get("raw"), customers, curated, a.get("records") or ())
            items.append((c, a.get("occurrences")))
            if a.get("authority") == "deterministic" and (c.status, c.customer_id, set(c.candidates)) != (a["status"], a.get("customer_id"), set(a.get("candidates") or ())):
                problems.append(f"{subject(ns, a.get('raw'))}: imported {a['status']} / {a.get('customer_id')} but the deterministic rules give {c.status} / {c.customer_id or list(c.candidates)}")
        registry._classify_into(db, version, items)
        decided = {d["subject"] for d in data.get("decisions") or []}
        decisions = list(data.get("decisions") or [])
        for a in aliases:  # the fixture format: coordinator statuses without explicit decisions
            subj = subject(a["namespace"], a.get("raw"))
            if a.get("authority") in AUTHORITIES and subj not in decided:
                action = {"excluded": "exclude", "unmatched": "reject"}.get(a["status"], "match")
                decisions.append({"id": _hid("dec", subj, action), "subject": subj, "action": action, "customer_id": a.get("customer_id") if action == "match" else None,
                                  "authority": a["authority"], "rationale": a.get("note") or "imported decision", "evidence": a.get("evidence") or [],
                                  "confidence": a.get("confidence", 0.9)})
        for d in decisions:
            ns, raw = parse_subject(d["subject"])
            if not d.get("evidence"):
                problems.append(f"{d['subject']}: a decision without evidence")
            db.execute("INSERT INTO decision (decision_id, namespace, raw_key, action, customer_id, authority, rationale, evidence, confidence, version, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (d["id"], ns, _key(raw), d["action"], d.get("customer_id"), d["authority"], d["rationale"], _j(d.get("evidence") or []),
                        float(d.get("confidence", 0.9)), version, registry.now().astimezone(timezone.utc).isoformat()))
        by_subject = {subject(a["namespace"], a.get("raw")): a for a in aliases}
        for r in data.get("reviews") or []:
            ns, raw = parse_subject(r["subject"])
            cid = r.get("customer_id", (by_subject.get(r["subject"]) or {}).get("customer_id"))
            db.execute("INSERT INTO review (review_id, namespace, raw_key, customer_id, model, verdict, rationale, cited_fields, usd, version, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (r["id"], ns, _key(raw), cid, r["model"], r["verdict"], r.get("rationale") or "", _j(r.get("cited_fields") or []), float(r.get("usd") or 0.0),
                        version, registry.now().astimezone(timezone.utc).isoformat()))
        registry._rederive(db, version)
        derived = registry._aliases(db)
        for a in aliases:
            state = derived[(a["namespace"], _key(a.get("raw")))][0]
            if (state.status, state.customer_id) != (a["status"], a.get("customer_id")):
                problems.append(f"{subject(a['namespace'], a.get('raw'))}: imported {a['status']} / {a.get('customer_id')} but derives {state.status} / {state.customer_id}")
        if problems:
            raise IdentityError("import refused (nothing was written): " + "; ".join(problems))

    registry._mutate("import", actor, body, note=str(data.get("registry_version") or ""))


def alias_dict(state: AliasState, effective_from: str, version: int) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": state.subject, "namespace": state.namespace, "raw": state.raw, "normalized": state.normalized, "customer_id": state.customer_id,
        "status": state.status, "rule": state.rule, "authority": state.authority, "confidence": state.confidence, "evidence": list(state.evidence),
        "review": state.review, "decision": state.decision, "effective_from": effective_from, "effective_to": None, "version": version,
    }
    if state.candidates:
        entry["candidates"] = list(state.candidates)
    if state.records:
        entry["records"] = list(state.records)
    return entry


def content_sha256(data: dict[str, Any]) -> str:
    body = {k: v for k, v in data.items() if k != "content_sha256"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# --- serving view (query-time join through the pinned registry) -------------------------------------


@dataclass(frozen=True)
class IdentityView:
    """Query-time identity: ``customer_id(namespace, raw)`` is set only for ``matched`` aliases of
    the pinned registry. A missing file or a digest mismatch yields a view with joins disabled
    (fail closed, §6.12). The view grants no access: callers apply their own visibility first."""

    joins_enabled: bool
    reason: str | None = None
    registry_version: str | None = None
    sha256: str | None = None
    matched: dict[tuple[str, str], str] = field(default_factory=dict)
    # ``auto_match: fuzzy``: the candidate aliases admitted as matched, with their rule (``fuzzy:<rule>``)
    promoted: dict[tuple[str, str], str] = field(default_factory=dict)
    auto_match: str = "exact"

    @classmethod
    def from_config(cls, identity: Any, path: str | Path | None = None) -> "IdentityView":
        """The pinned view of ``crosssource.identity`` (``auto_match`` and its threshold applied)."""
        return cls.pinned(identity.registry_path if path is None else path, identity.sha256,
                          auto_match=getattr(identity, "auto_match", "exact"), min_confidence=getattr(identity, "fuzzy_min_confidence", 0.8))

    @staticmethod
    def _joinable(rows: Iterable[tuple[str, str | None, str | None, str, str, float]], auto_match: str, min_confidence: float) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
        """``(namespace, raw, customer_id, status, rule, confidence)`` rows -> (matched, promoted)."""
        matched: dict[tuple[str, str], str] = {}
        promoted: dict[tuple[str, str], str] = {}
        for namespace, raw, customer, status, rule, confidence in rows:
            if raw is None or customer is None:
                continue
            if status in JOINABLE_STATUSES:
                matched[(namespace, raw)] = customer
            elif auto_match == "fuzzy" and status == "candidate" and confidence >= min_confidence:
                matched[(namespace, raw)] = customer
                promoted[(namespace, raw)] = f"fuzzy:{rule}"
        return matched, promoted

    def customer_id(self, namespace: str, raw: str | None) -> str | None:
        if not self.joins_enabled or raw is None:
            return None
        return self.matched.get((namespace, raw))

    @classmethod
    def pinned(cls, path: str | Path, sha256: str, *, auto_match: str = "exact", min_confidence: float = 0.8) -> "IdentityView":
        if auto_match not in ("exact", "fuzzy"):
            raise IdentityError(f"auto_match must be exact or fuzzy, not {auto_match!r}")
        path = Path(path)
        if not path.is_file():
            return cls(False, "registry_missing")
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        if digest != sha256:
            return cls(False, "digest_mismatch", sha256=digest)
        if blob.startswith(b"SQLite format 3\x00"):
            db = sqlite3.connect(":memory:")
            try:
                db.deserialize(blob)
                version = int(db.execute("SELECT coalesce(max(version), 0) FROM registry_version").fetchone()[0])
                aliases = IdentityRegistry._aliases(db)
            finally:
                db.close()
            matched, promoted = cls._joinable(((ns, s.raw, s.customer_id, s.status, s.rule, s.confidence) for (ns, _k), (s, _e, _v) in aliases.items()), auto_match, min_confidence)
            return cls(True, None, f"r{version}", digest, matched, promoted, auto_match)
        data = yaml.safe_load(blob.decode("utf-8"))
        if not isinstance(data, dict) or data.get("format_version") != FORMAT_VERSION or data.get("content_sha256") != content_sha256(data):
            return cls(False, "export_invalid", sha256=digest)
        rows = ((a["namespace"], a.get("raw"), a.get("customer_id"), a["status"], a.get("rule", ""), float(a.get("confidence", 0.0))) for a in data["aliases"])
        matched, promoted = cls._joinable(rows, auto_match, min_confidence)
        return cls(True, None, str(data["registry_version"]), digest, matched, promoted, auto_match)


# --- model review ----------------------------------------------------------------------------------

REVIEW_SYSTEM = (
    "You review one proposed identity alias for a cross-source customer registry. A raw value seen in a source namespace "
    "(for example a ticket organization, a log tenant, an analytics company id or a tracker customer field) is proposed to "
    "denote a registry customer. Judge only from the supplied fields and evidence. Reply 'agree' when the evidence supports "
    "that the raw value denotes this customer, 'refutes' when the evidence contradicts it or shows the value is shared, "
    "generic or internal, and 'uncertain' otherwise. Cite only the evidence ids or field names you used. Do not use outside "
    "knowledge and do not invent evidence."
)
PAYLOAD_FIELDS = ("namespace", "raw", "customer_id", "customer_name", "rule", "authority", "confidence", "decision_rationale")


@dataclass(frozen=True)
class ReviewItem:
    namespace: str
    raw: str | None
    customer_id: str
    customer_name: str
    status: str
    rule: str
    authority: str
    confidence: float
    decision_rationale: str | None
    evidence: tuple[dict[str, Any], ...]

    @property
    def subject(self) -> str:
        return subject(self.namespace, self.raw)

    def payload(self) -> dict[str, Any]:
        return {
            "alias": {k: getattr(self, k) for k in PAYLOAD_FIELDS},
            "evidence": [{k: v for k, v in e.items() if v is not None} for e in self.evidence],
            "allowed_citations": sorted({*PAYLOAD_FIELDS, *(e["id"] for e in self.evidence)}),
        }

    def prompt_chars(self) -> int:
        schema = json.dumps(ReviewReply.model_json_schema())
        return len(REVIEW_SYSTEM) + len(json.dumps(self.payload(), ensure_ascii=False)) + len(schema)


def review_queue(registry: IdentityRegistry) -> list[ReviewItem]:
    """Every non-exact proposed match without a review of its current customer: candidates
    (deterministic or coordinator-decided) and casefold auto-matches. Exact matches, contested,
    ambiguous, excluded, unmatched and missing entries are not queued."""
    st = registry.state()
    names = {c.customer_id: c.name for c in st["customers"]}
    evidence = {e["id"]: e for e in st["evidence"]}
    decisions = st["decisions"]
    reviews = st["reviews"]
    queue = []
    for key, (state, _eff, _v) in sorted(st["aliases"].items()):
        if state.customer_id is None or state.status not in ("candidate", "matched") or state.rule == "exact":
            continue
        if any(r.customer_id == state.customer_id for r in reviews.get(key, [])):
            continue
        if state.status == "matched" and state.authority == "owner":
            continue
        decision = next((d for d in reversed(decisions.get(key, [])) if d.decision_id == state.decision), None)
        queue.append(ReviewItem(state.namespace, state.raw, state.customer_id, names.get(state.customer_id, ""), state.status, state.rule, state.authority,
                                state.confidence, None if decision is None else decision.rationale,
                                tuple(evidence[e] for e in state.evidence if e in evidence)))
    return queue


def review_plan(registry: IdentityRegistry, *, price_table: Any, pricing_key: str | None, max_tokens: int, limit: int | None = None) -> dict[str, Any]:
    """``identity review --dry-run``: the queue and the worst-case cost of reviewing it; no call."""
    queue = review_queue(registry)
    chosen = queue if limit is None else queue[:limit]
    priced = price_table is not None and pricing_key is not None and pricing_key in price_table.prices
    items = []
    for item in chosen:
        items.append({"subject": item.subject, "customer_id": item.customer_id, "status": item.status, "rule": item.rule, "prompt_chars": item.prompt_chars(),
                      "worst_case_usd": round(worst_case_usd(price_table, pricing_key, item.prompt_chars(), max_tokens), 6) if priced else None})
    return {
        "queue": len(queue),
        "planned": len(items),
        "items": items,
        "worst_case_total_usd": round(sum(i["worst_case_usd"] for i in items), 6) if priced else None,
        "priced": priced,
    }


def run_review(registry: IdentityRegistry, provider: Any, ledger: ReviewLedger, price_table: Any, *, pricing_key: str, max_tokens: int, max_usd: float,
               limit: int | None = None, actor: str | None = None) -> dict[str, Any]:
    """Review the queue with ``provider`` (a ``ChatProvider``). Each call reserves its worst case on
    the shared ledger and the run cap first; the run stops at the first refusal, provider error or
    invalid reply (no retry, no repair). A reply citing anything outside the supplied fields and
    evidence is not recorded."""
    from ..providers.base import CallContext, ProviderError, SchemaViolation

    cap = RunCap(max_usd)
    model = getattr(getattr(provider, "role", None), "model", None) or getattr(provider, "name", "reviewer")
    results: list[dict[str, Any]] = []
    stopped: dict[str, Any] | None = None
    queue = review_queue(registry)
    for item in queue if limit is None else queue[:limit]:
        meter = ReviewMeter(ledger, price_table, pricing_key, scope=REVIEW_SCOPE, subject=item.subject, model=model,
                            prompt_chars=item.prompt_chars(), max_tokens=max_tokens, run_cap=cap)
        payload = item.payload()
        try:
            reply, _usage = provider.complete_json(REVIEW_SYSTEM, payload, ReviewReply, meter, CallContext("identity_review", {"subject": item.subject}))
        except BudgetExhausted as exc:
            stopped = {"subject": item.subject, "reason": "budget_exhausted", "kind": exc.kind, "requested_usd": exc.requested, "remaining_usd": exc.remaining}
            break
        except (ProviderError, SchemaViolation) as exc:
            stopped = {"subject": item.subject, "reason": "provider_failed", "error": type(exc).__name__, "usd": meter.committed_usd}
            break
        invalid = sorted(set(reply.cited_fields) - set(payload["allowed_citations"]))
        if invalid:
            results.append({"subject": item.subject, "outcome": "invalid_citation", "cited": invalid, "usd": meter.committed_usd})
            stopped = {"subject": item.subject, "reason": "invalid_citation"}
            break
        recorded = registry.record_review(item.namespace, item.raw, item.customer_id, model=str(model), verdict=reply.verdict, rationale=reply.rationale,
                                          cited_fields=reply.cited_fields, usd=meter.committed_usd, actor=actor)
        results.append({"subject": item.subject, "outcome": reply.verdict, "review": recorded["review"], "status": recorded["alias"]["status"], "usd": meter.committed_usd})
    return {"reviewed": sum(1 for r in results if r["outcome"] in VERDICTS), "results": results, "stopped": stopped,
            "run_usd": round(sum(r["usd"] for r in results) + (stopped or {}).get("usd", 0.0), 6), "ledger": ledger.usage()}
