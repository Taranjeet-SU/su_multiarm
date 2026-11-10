"""Cross-source link builders, tiers A and B (unified merge plan §6.9; increment M3, lane LD).

Input: the **pinned** documents of the collection (members in order, then the pinned slices of
each live day; a later version of an id replaces an earlier one, exactly as the id directory
serves them) and the pinned ``IdentityView``. Output: one ``Link`` per ``(pair, subject, object)``
plus a build report with denominators and skip reasons. Nothing here calls a model.

Tier A — explicit references (``explicit_ref``, status ``accepted``): a ticket or tracker issue
whose text names another record of the collection by its id or URL (``ticket:1003``, an incident
id, a documentation URL). A reference that looks like an id or URL but names nothing in the pinned
collection is reported as ``dangling`` and never becomes a link.

Tier B — rules (status ``candidate`` until reviewed):

* ``customer_window_signature`` (ticket <-> episode): the ticket's customer and the episode's
  customer are the **same matched** registry customer (``IdentityView``: matched aliases only), the
  ticket was created inside ``[episode first - window_before_days, episode last +
  window_after_days]`` and the ticket text mentions the episode's error signature
  (``signature_mentions``). A disabled identity view (missing registry, digest mismatch) disables
  this builder (fail closed, §6.12); it is reported, never approximated.
* ``signature_mention`` (ticket <-> issue family ``family:<rule>``; family <-> doc): the text
  mentions the rule.
* ``title_mention`` (ticket <-> doc): the ticket text contains the documentation page title as a
  whole phrase (titles of at least two words).

Field names: the customer field and a kind's event time are read from the member declarations
(``collection_fields``: ``field_map.customer_ref`` under the member's ``identity_namespace``,
``time_roles.<kind>.event_time`` or ``field_map.event_time``); undeclared kinds keep the defaults
(``account`` / ``tenant`` / ``customer`` / ``company_id``; ``created_at``). The build report's
``fields`` states which names were read.

Analytics query <-> doc/ticket (clicks, tier A) is **stubbed** until the analytics member exists
(M7): ``click_links`` returns nothing and the report says so. Tier C (similarity) is M9.

Tier-B links are capped at ``max_per_record`` per subject and builder (nearest in time first);
the cap is reported. One link per pair key: an explicit reference wins over a rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .signature_mentions import FAMILY_PREFIX, SignatureCatalog, _norm, family_id

BUILDER_VERSION = "links-b1"
TIER_A_SUBJECT_KINDS = ("ticket", "tracker_issue")
KIND_GROUP = {"ticket": "ticket", "doc": "doc", "incident": "episode", "tracker_issue": "tracker", "search_query_day": "query"}
# kind -> (identity namespace, metadata field holding the raw customer value)
CUSTOMER_FIELDS: dict[str, tuple[str, str]] = {
    "ticket": ("support.account", "account"),
    "incident": ("logs.tenant", "tenant"),
    "tracker_issue": ("tracker.customer", "customer"),
    "search_query_day": ("analytics.company_id", "company_id"),
}
TICKET_TIME_FIELD = "created_at"
DANGLING_SAMPLE = 20
_LEAD = "([{<\"'"
_TRAIL = ".,;:!?)]}>\"'"
_ID_LIKE = re.compile(r"^[a-z][a-z0-9_]*:[A-Za-z0-9][A-Za-z0-9._:\-]*$")


def link_id(pair: str, subject_id: str, object_id: str) -> str:
    return "lnk-" + hashlib.sha256(json.dumps([pair, subject_id, object_id]).encode()).hexdigest()[:16]


def evidence_sha(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    """An ISO timestamp **with** an offset; anything else is ``None`` (counted, never guessed)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def collection_fields(collection: Any) -> tuple[dict[str, tuple[str, str] | None], dict[str, str | None]]:
    """Per record kind, the customer field (identity namespace, metadata field) and the event-time
    field the builders read, from the declared members (staging finding F4: ``field_map`` was
    validated but never read). Customer: ``field_map.customer_ref`` (``None`` = declared absent)
    under the member's ``identity_namespace`` (else the kind's default namespace). Event time:
    ``time_roles.<kind>.event_time``, else ``field_map.event_time``. Kinds a member does not map
    keep the defaults (``CUSTOMER_FIELDS``, ``TICKET_TIME_FIELD``)."""
    customers: dict[str, tuple[str, str] | None] = {}
    times: dict[str, str | None] = {}
    if collection is None:
        return customers, times
    for name in sorted(collection.members):
        member = collection.members[name]
        mapped = member.field_map
        for kind in member.record_kinds:
            if "customer_ref" in mapped:
                namespace = member.identity_namespace or (CUSTOMER_FIELDS[kind][0] if kind in CUSTOMER_FIELDS else None)
                target = mapped["customer_ref"]
                if target is None:
                    customers[kind] = None
                elif namespace is not None:
                    customers[kind] = (namespace, target)
            elif member.identity_namespace is not None and kind in CUSTOMER_FIELDS:
                customers[kind] = (member.identity_namespace, CUSTOMER_FIELDS[kind][1])
            roles = member.time_roles.get(kind) or {}
            if "event_time" in roles:
                times[kind] = roles["event_time"]
            elif "event_time" in mapped:
                times[kind] = mapped["event_time"]
    return customers, times


@dataclass(frozen=True)
class LinkSettings:
    """``crosssource.links`` values used by the builders (defaults = the schema defaults).
    ``customer_fields`` maps a record kind to (identity namespace, metadata field), ``None`` when the
    kind declares no customer; ``time_fields`` a kind to its event-time field (``None``: absent)."""

    window_before_days: int = 1
    window_after_days: int = 7
    max_per_record: int = 5
    customer_fields: dict[str, tuple[str, str] | None] = field(default_factory=lambda: dict(CUSTOMER_FIELDS))
    time_fields: dict[str, str | None] = field(default_factory=lambda: {"ticket": TICKET_TIME_FIELD})

    @classmethod
    def from_config(cls, links: Any, customer_fields: dict[str, tuple[str, str] | None] | None = None, collection: Any = None) -> "LinkSettings":
        """Settings from ``crosssource.links``; the member declarations of ``collection`` (field map,
        time roles, identity namespace) override the default field names; ``customer_fields``
        overrides both."""
        base = cls() if links is None else cls(links.window_before_days, links.window_after_days, links.max_per_record)
        declared_customers, declared_times = collection_fields(collection)
        customers = {**base.customer_fields, **declared_customers, **(customer_fields or {})}
        times = {**base.time_fields, **declared_times}
        return cls(base.window_before_days, base.window_after_days, base.max_per_record, customers, times)

    def time_field(self, kind: str) -> str | None:
        return self.time_fields.get(kind, TICKET_TIME_FIELD if kind == "ticket" else None)

    def described(self) -> dict[str, Any]:
        """The field names the build read (recorded in the build report)."""
        return {"customer": {k: (list(v) if v is not None else None) for k, v in sorted(self.customer_fields.items())}, "event_time": dict(sorted(self.time_fields.items()))}


@dataclass(frozen=True)
class Link:
    pair: str
    tier: str
    builder: str
    subject_id: str
    object_id: str
    evidence: dict[str, Any] = field(compare=False)
    rank: float = field(default=0.0, compare=False)  # ordering inside a capped group (lower first)

    @property
    def link_id(self) -> str:
        return link_id(self.pair, self.subject_id, self.object_id)

    @property
    def evidence_sha(self) -> str:
        return evidence_sha(self.evidence)

    @property
    def customer_derived(self) -> bool:
        return "customer_id" in self.evidence


@dataclass
class BuildResult:
    links: list[Link]
    report: dict[str, Any]
    known_ids: frozenset[str]
    documents: dict[str, Any]
    catalog: SignatureCatalog


def pinned_documents(documents: Iterable[Any]) -> tuple[dict[str, Any], int]:
    """Latest version per id, in input order; returns the map and the number of replaced versions."""
    view: dict[str, Any] = {}
    replaced = 0
    for d in documents:
        if d.id in view:
            replaced += 1
        view[d.id] = d
    return view, replaced


def texts_of(document: Any) -> dict[str, str]:
    return {"title": document.title or "", **{k: v for k, v in sorted((document.fields or {}).items()) if isinstance(v, str)}}


def _normalize_url(value: str) -> str:
    return value.split("#", 1)[0].rstrip("/")


class _Resolver:
    def __init__(self, view: dict[str, Any], families: set[str]):
        self.ids = set(view) | families
        self.urls: dict[str, str] = {}
        for d in view.values():
            if d.id.startswith(("http://", "https://")):
                self.urls.setdefault(_normalize_url(d.id), d.id)
        for d in view.values():
            if d.url:
                self.urls.setdefault(_normalize_url(d.url), d.id)
        self.prefixes = {i.split(":", 1)[0] for i in self.ids if ":" in i and not i.startswith(("http://", "https://"))}

    def resolve(self, token: str) -> tuple[str | None, bool]:
        """(document id, looks like a reference)."""
        if token in self.ids:
            return token, True
        if token.startswith(("http://", "https://")):
            return self.urls.get(_normalize_url(token)), True
        if _ID_LIKE.match(token) and token.split(":", 1)[0] in self.prefixes:
            return None, True
        return None, False


def _tokens(text: str) -> Iterable[str]:
    for raw in text.split():
        token = raw.lstrip(_LEAD).rstrip(_TRAIL)
        if token:
            yield token


def explicit_links(view: dict[str, Any], resolver: _Resolver, report: dict[str, Any]) -> list[Link]:
    out: list[Link] = []
    dangling: list[dict[str, str]] = []
    for d in view.values():
        if d.kind not in TIER_A_SUBJECT_KINDS:
            continue
        seen: set[str] = set()
        for name, text in texts_of(d).items():
            for token in _tokens(text):
                target, looks = resolver.resolve(token)
                if target is None:
                    if looks:
                        dangling.append({"subject_id": d.id, "mention": token, "field": name})
                    continue
                if target == d.id or target in seen:
                    continue
                obj = view.get(target)
                group = None if obj is None else KIND_GROUP.get(obj.kind)
                if group is None:
                    report["skipped"]["explicit_ref:object_kind_not_linkable"] += 1
                    continue
                seen.add(target)
                out.append(Link(f"{KIND_GROUP[d.kind]}-{group}", "A", "explicit_ref", d.id, target, {"mention": token, "field": name}))
    report["dangling"] = {"count": len(dangling), "sample": dangling[:DANGLING_SAMPLE]}
    return out


def _customer(identity: Any, settings: LinkSettings, d: Any) -> tuple[str | None, str | None, str | None]:
    """(customer id, alias label, raw) through the pinned identity view; matched aliases only."""
    spec = settings.customer_fields.get(d.kind)
    if spec is None:
        return None, None, None
    namespace, name = spec
    raw = (d.metadata or {}).get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None, None
    raw = str(raw)
    return identity.customer_id(namespace, raw), f"{namespace}:{raw}", raw


def customer_window_links(view: dict[str, Any], catalog: SignatureCatalog, identity: Any, settings: LinkSettings, report: dict[str, Any]) -> list[Link]:
    skipped: Counter[str] = report["skipped"]
    if identity is None or not getattr(identity, "joins_enabled", False):
        report["builders"]["customer_window_signature"] = {"status": "disabled", "reason": "identity_unavailable" if identity is None else f"identity_{identity.reason}"}
        return []
    before, after = timedelta(days=settings.window_before_days), timedelta(days=settings.window_after_days)
    buckets: dict[tuple[str, str], list[tuple[datetime, datetime, Any, str]]] = defaultdict(list)
    for d in view.values():
        if d.kind != "incident":
            continue
        rule = (d.metadata or {}).get("signature_id")
        first, last = parse_time((d.metadata or {}).get("first_hour")), parse_time((d.metadata or {}).get("last_hour"))
        customer, alias, raw = _customer(identity, settings, d)
        if raw is None:
            skipped["episode:no_customer_value"] += 1
        elif customer is None:
            skipped["episode:customer_not_matched"] += 1
        elif not rule or first is None or last is None:
            skipped["episode:no_signature_or_time"] += 1
        else:
            buckets[(customer, str(rule))].append((first, last, d, alias))
    out: list[Link] = []
    for d in view.values():
        if d.kind != "ticket":
            continue
        customer, alias, raw = _customer(identity, settings, d)
        time_field = settings.time_field(d.kind)
        when = parse_time((d.metadata or {}).get(time_field)) if time_field is not None else None
        if raw is None:
            skipped["ticket:no_customer_value"] += 1
            continue
        if customer is None:
            skipped["ticket:customer_not_matched"] += 1
            continue
        if when is None:
            skipped["ticket:no_time_with_offset"] += 1
            continue
        mentions = catalog.mentions(texts_of(d))
        if not mentions:
            skipped["ticket:no_signature_mention"] += 1
            continue
        found = bucketed = False
        for m in mentions:
            episodes = buckets.get((customer, m.rule), [])
            if not episodes:
                continue
            bucketed = True
            for first, last, ep, ep_alias in episodes:
                lo, hi = first - before, last + after
                if not lo <= when <= hi:
                    skipped["pair:outside_window"] += 1
                    continue
                found = True
                service = (d.metadata or {}).get("service")
                evidence = {
                    "customer_id": customer, "aliases": [alias, ep_alias], "window": [iso_z(lo), iso_z(hi)], "ticket_time": iso_z(when),
                    "mention": m.rule, "mention_how": m.how, "mention_field": m.field,
                    "service_match": bool(service) and service == (ep.metadata or {}).get("service"),
                }
                out.append(Link("ticket-episode", "B", "customer_window_signature", d.id, ep.id, evidence, abs((when - first).total_seconds())))
        if not bucketed:
            skipped["ticket:no_episode_for_customer_and_signature"] += 1
        elif not found:
            skipped["ticket:no_episode_in_window"] += 1
    report["builders"]["customer_window_signature"] = {"status": "ran", "registry_version": identity.registry_version}
    return out


def family_links(view: dict[str, Any], catalog: SignatureCatalog) -> list[Link]:
    out: list[Link] = []
    for d in view.values():
        if d.kind not in ("ticket", "doc"):
            continue
        for m in catalog.mentions(texts_of(d)):
            evidence = {"mention": m.rule, "mention_how": m.how, "mention_field": m.field}
            if d.kind == "ticket":
                out.append(Link("ticket-family", "B", "signature_mention", d.id, family_id(m.rule), evidence, 0.0 if m.how == "rule_id" else 1.0))
            else:
                out.append(Link("family-doc", "B", "signature_mention", family_id(m.rule), d.id, evidence, 0.0 if m.how == "rule_id" else 1.0))
    return out


def title_links(view: dict[str, Any]) -> list[Link]:
    titles = {}
    for d in view.values():
        if d.kind == "doc" and d.title and len(_norm(d.title).split()) >= 2:
            titles[d.id] = re.compile(r"(?<!\w)" + re.escape(_norm(d.title)) + r"(?!\w)")
    out: list[Link] = []
    for d in view.values():
        if d.kind != "ticket":
            continue
        texts = {k: _norm(v) for k, v in texts_of(d).items()}
        for doc_id, pattern in titles.items():
            hit = next((k for k, v in texts.items() if v and pattern.search(v)), None)
            if hit is not None:
                out.append(Link("ticket-doc", "B", "title_mention", d.id, doc_id, {"mention": view[doc_id].title, "field": hit}))
    return out


def click_links(view: dict[str, Any], report: dict[str, Any]) -> list[Link]:
    """Analytics query <-> doc/ticket links: stubbed until the analytics member is onboarded (M7)."""
    present = sum(1 for d in view.values() if d.kind in ("search_query_day", "search_click"))
    report["builders"]["click"] = {"status": "stubbed", "until": "M7 analytics member", "analytics_documents_ignored": present}
    return []


def _cap(links: list[Link], limit: int, report: dict[str, Any]) -> list[Link]:
    groups: dict[tuple[str, str], list[Link]] = defaultdict(list)
    kept: list[Link] = []
    for link in links:
        if link.tier == "A":
            kept.append(link)
        else:
            groups[(link.subject_id, link.builder)].append(link)
    capped = 0
    for _key, group in sorted(groups.items()):
        group.sort(key=lambda x: (x.rank, x.object_id))
        kept.extend(group[:limit])
        capped += max(0, len(group) - limit)
    report["capped"] = capped
    return kept


def build_links(documents: Iterable[Any], identity: Any, settings: LinkSettings | None = None) -> BuildResult:
    """Every tier A/B link over the pinned ``documents`` (see the module docstring)."""
    settings = settings or LinkSettings()
    view, replaced = pinned_documents(documents)
    catalog = SignatureCatalog.from_documents(view.values())
    families = catalog.families()
    report: dict[str, Any] = {
        "builder_version": BUILDER_VERSION,
        "inputs": {"documents": len(view), "replaced_versions": replaced, "by_kind": dict(sorted(Counter(d.kind for d in view.values()).items())),
                   "families": len(families)},
        "identity": {"joins_enabled": bool(getattr(identity, "joins_enabled", False)), "registry_version": getattr(identity, "registry_version", None),
                     "reason": "identity_unavailable" if identity is None else identity.reason},
        "window": {"before_days": settings.window_before_days, "after_days": settings.window_after_days},
        "fields": settings.described(),
        "builders": {"explicit_ref": {"status": "ran"}, "signature_mention": {"status": "ran"}, "title_mention": {"status": "ran"}},
        "skipped": Counter(),
    }
    resolver = _Resolver(view, families)
    produced = [
        *explicit_links(view, resolver, report),
        *customer_window_links(view, catalog, identity, settings, report),
        *family_links(view, catalog),
        *title_links(view),
        *click_links(view, report),
    ]
    best: dict[str, Link] = {}
    for link in produced:  # one link per pair key; tier A beats tier B, the first producer otherwise
        current = best.get(link.link_id)
        if current is None or link.tier < current.tier:
            best[link.link_id] = link
    links = _cap(list(best.values()), settings.max_per_record, report)
    links.sort(key=lambda x: (x.pair, x.subject_id, x.object_id))
    report["skipped"] = dict(sorted(report["skipped"].items()))
    report["links"] = {"total": len(links), "by_tier": dict(sorted(Counter(x.tier for x in links).items())),
                       "by_builder": dict(sorted(Counter(x.builder for x in links).items())), "by_pair": dict(sorted(Counter(x.pair for x in links).items()))}
    return BuildResult(links, report, frozenset(resolver.ids), view, catalog)


def is_family(record_id: str) -> bool:
    return record_id.startswith(FAMILY_PREFIX)


__all__ = ["BUILDER_VERSION", "BuildResult", "Link", "LinkSettings", "build_links", "click_links", "collection_fields", "evidence_sha", "link_id", "parse_time"]
