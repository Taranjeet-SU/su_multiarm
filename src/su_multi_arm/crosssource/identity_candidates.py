"""Deterministic identity candidates (unified merge plan §6.9, increment M2b-1).

A raw customer value of a namespace (``support.account``, ``logs.tenant``, ``analytics.company_id``,
``tracker.customer`` ...) is classified against the registry's customers with fixed rules, in this
order:

1. ``missing`` — the record carries no value (the records are kept for the report).
2. ``exact`` — the raw value equals a customer name; ``casefold`` — equal after casefolding and
   whitespace collapsing. Only a **unique** hit of these two rules auto-matches (owner rule: a
   tenant name equal to the organization name is the same customer). Two or more customers hit by
   the same rule make the value ``ambiguous``.
3. Candidate-only rules (never a join until a coordinator decides and a review agrees):
   ``curated`` (a coordinator-supplied curated alias list), ``environment_suffix`` (``-prod``,
   ``-stg`` ... stripped, then compared by casefold or leading tokens), ``token_set_jaccard``
   (token-set Jaccard >= 0.6) and ``token_prefix`` (the value's tokens are the leading tokens of a
   customer name, e.g. ``eastvale`` -> ``Eastvale Health``). One customer -> ``candidate``; two or
   more -> ``ambiguous``; none -> ``unmatched``.

Pure functions: no clock, no I/O, no model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping

AUTO_MATCH_RULES = ("exact", "casefold")
CANDIDATE_RULES = ("curated", "environment_suffix", "token_set_jaccard", "token_prefix")
JACCARD_MIN = 0.6
# Environment suffixes that are stripped to propose (never to auto-match) a candidate.
ENVIRONMENT_SUFFIXES = ("prod", "production", "prd", "stg", "staging", "stage", "dev", "development", "test", "testing", "qa", "uat", "sandbox", "preprod", "demo")
_SUFFIX = re.compile(r"^(?P<base>.*?[^\W_])[\s._-]+(?:" + "|".join(ENVIRONMENT_SUFFIXES) + r")$")
_TOKEN = re.compile(r"[^\W_]+")
# Fixed candidate confidences (an ordering hint for the coordinator; never a threshold for a join).
CONFIDENCE = {"exact": 1.0, "casefold": 1.0, "curated": 0.9, "environment_suffix": 0.6}


def normalize(raw: str | None) -> str | None:
    """Casefolded, whitespace-collapsed form (``None`` stays ``None``)."""
    return None if raw is None else " ".join(raw.casefold().split())


def tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(text.casefold()))


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    left, right = set(a), set(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def strip_environment_suffix(raw: str) -> str | None:
    """The value without a trailing environment suffix, or ``None`` when it has none."""
    match = _SUFFIX.match(raw.strip().casefold())
    return match.group("base") if match else None


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str


@dataclass(frozen=True)
class Classification:
    """The deterministic outcome for one ``(namespace, raw)``. ``customer_id`` is set for
    ``matched`` and ``candidate``; ``candidates`` lists every customer of an ``ambiguous`` value."""

    namespace: str
    raw: str | None
    normalized: str | None
    status: str  # matched | candidate | ambiguous | unmatched | missing
    rule: str
    confidence: float
    customer_id: str | None = None
    candidates: tuple[str, ...] = ()
    records: tuple[str, ...] = field(default=())


def _prefix(raw_tokens: tuple[str, ...], name_tokens: tuple[str, ...]) -> bool:
    return 0 < len(raw_tokens) < len(name_tokens) and name_tokens[: len(raw_tokens)] == raw_tokens


def classify(
    namespace: str,
    raw: str | None,
    customers: Iterable[Customer],
    curated: Mapping[tuple[str, str], str] | None = None,
    records: Iterable[str] = (),
) -> Classification:
    """Classify one raw value (see the module docstring for the rules and their order)."""
    customers = list(customers)
    if raw is None or not raw.strip():
        return Classification(namespace, None, None, "missing", "none", 0.0, records=tuple(sorted(set(records))))
    norm = normalize(raw)
    for rule, hits in (
        ("exact", sorted({c.customer_id for c in customers if c.name == raw})),
        ("casefold", sorted({c.customer_id for c in customers if normalize(c.name) == norm})),
    ):
        if len(hits) == 1:
            return Classification(namespace, raw, norm, "matched", rule, 1.0, customer_id=hits[0])
        if len(hits) > 1:
            return Classification(namespace, raw, norm, "ambiguous", rule, 1.0 / len(hits), candidates=tuple(hits))
    best: dict[str, tuple[float, str]] = {}  # customer -> (confidence, rule)

    def offer(customer_id: str, rule: str, confidence: float) -> None:
        if customer_id not in best or confidence > best[customer_id][0]:
            best[customer_id] = (round(confidence, 4), rule)

    known = {c.customer_id for c in customers}
    target = (curated or {}).get((namespace, raw))
    if target is not None and target in known:
        offer(target, "curated", CONFIDENCE["curated"])
    base = strip_environment_suffix(raw)
    raw_tokens = tokens(raw)
    for customer in customers:
        name_tokens = tokens(customer.name)
        if base is not None:
            base_tokens = tokens(base)
            if normalize(base) == normalize(customer.name) or _prefix(base_tokens, name_tokens):
                offer(customer.customer_id, "environment_suffix", CONFIDENCE["environment_suffix"])
        score = jaccard(raw_tokens, name_tokens)
        if score >= JACCARD_MIN:
            offer(customer.customer_id, "token_set_jaccard", score)
        elif _prefix(raw_tokens, name_tokens):
            offer(customer.customer_id, "token_prefix", score)
    if not best:
        return Classification(namespace, raw, norm, "unmatched", "none", 0.0)
    ranked = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))
    top_confidence, top_rule = ranked[0][1]
    if len(ranked) == 1:
        return Classification(namespace, raw, norm, "candidate", top_rule, top_confidence, customer_id=ranked[0][0])
    return Classification(namespace, raw, norm, "ambiguous", top_rule, top_confidence, candidates=tuple(sorted(best)))


def classify_values(
    namespace: str,
    values: Iterable[str | None],
    customers: Iterable[Customer],
    curated: Mapping[tuple[str, str], str] | None = None,
    missing_records: Iterable[str] = (),
) -> list[Classification]:
    """Classify the distinct raw values of a namespace (sorted; one ``missing`` entry when any
    record lacks a value)."""
    customers, values = list(customers), list(values)
    distinct = sorted({v for v in values if v is not None and v.strip()})
    out = [classify(namespace, v, customers, curated) for v in distinct]
    missing = sorted(set(missing_records))
    if missing or any(v is None or not v.strip() for v in values):
        out.append(classify(namespace, None, customers, records=missing))
    return out
