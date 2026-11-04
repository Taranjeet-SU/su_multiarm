"""Safe redaction engine (addendum A1 §3.2.3, slice B11; ``privacy.engine: safe``).

Every redactor is a regular expression in the RE2 subset that passes the pattern library's ReDoS
lint (``extraction.patterns.engine.validate_regex``), compiled and run by the configured pattern
engine (``privacy.regex_engine``: ``re2``, or the ``regex`` development engine whose per-call
timeout makes the redaction **fail**, never pass text through). There is no fallback from one
engine to the other and no Python ``re`` on this path.

**Built-in redactors** (``BUILTIN``; applied in this fixed order, so an earlier one's token is
never re-matched by a later one):

=================  =====================================================  ==========================
name               matches                                                replaced by
=================  =====================================================  ==========================
``url_credentials`` ``scheme://user:password@``                           ``scheme://[REDACTED_URL_CREDENTIALS]@``
``bearer_token``   ``Bearer <token>``                                     ``Bearer [REDACTED_BEARER_TOKEN]``
``jwt``            ``eyJ….….…`` JSON web tokens                           ``[REDACTED_JWT]``
``api_key``        ``api_key=…``, ``password: …``, ``token=…`` (the key   value: ``[REDACTED_API_KEY]``
                   is kept) and prefixed key shapes (``sk-``, ``ghp_``,
                   ``xoxb-``)
``aws_key``        ``AKIA…``/``ASIA…`` access key ids                     ``[REDACTED_AWS_KEY]``
``request_id``     ``request_id=…``, ``X-Request-Id: …``, ``trace_id`` …  value: ``[REQUEST_ID:<hash>]``
``uuid``           RFC 4122 shape                                         ``[UUID:<hash>]``
``email``          ``local@domain.tld``                                   ``[REDACTED_EMAIL]``
``query_values``   every ``?key=value`` / ``&key=value`` value            value: ``[REDACTED_QUERY_VALUE]``
``ipv6``           full or ``::``-compressed IPv6 (validated)             ``[REDACTED_IPV6]``
``ipv4``           dotted quad                                            ``[REDACTED_IPV4]``
``phone``          ``+<cc>`` digit groups, ``(415) 555-0100``, phone keys  ``[REDACTED_PHONE]``
``long_numeric_id`` 13–19 digit runs                                      ``[LONG_NUMERIC_ID:<hash>]``
``hex_secret``     32–128 hex digits (noisy on hashes; opt-in)            ``[REDACTED_HEX_SECRET]``
=================  =====================================================  ==========================

The payload redactors follow (``llm_text``, ``content``, ``query``, ``tracking_id``, ``pg_row``,
``record_payload``), then ``person_name`` (JSON and ``key=value`` person-name fields),
``record_list`` (JSON arrays of records under list keys, ``<record>``) and ``product_prose`` (free
text after a configured product name, ``<prose>``; exists only with ``privacy.prose_products``).

``<hash>`` is the first 12 hex digits of a sha256 of the value, keyed with the secret in
``privacy.hash_key_env`` when set: equal ids stay joinable, the value is not readable. User
redactors (``privacy.custom``) and the legacy ``privacy.patterns`` (as case-insensitive ``mask``
redactors) run after the built-ins, in declaration order. A value that already is a redaction
token (``[REDACTED_…]``, ``[UUID:…]``) is left alone, so redaction is idempotent.

``SafeRedactor.stamp`` (``safe:<sha16>``) names the redaction recipe; records, partitions and
prompt lines carry it, and the egress policy admits only lines with the current stamp.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..core.errors import SuError
from ..extraction.patterns.engine import Hit, PatternEngine, PatternTimeout, select_engine, validate_regex

REDACTOR_VERSION = "1"
# A redaction token written by this module (never redacted again).
MARKERS = ("llm_text", "content", "query", "record", "value", "prose")  # payload markers: ``<llm_text>`` …
_TOKEN = r"\[(?:REDACTED_[A-Z0-9_]{1,40}|[A-Z0-9_]{1,40}:[0-9a-f]{12})\]|<(?:" + "|".join(MARKERS) + r")>"
TOKEN = re.compile(r"^(?:" + _TOKEN + r")$")
TOKENS = re.compile(_TOKEN)
# Field names whose values ``record_payload`` masks by default (structured entity rows: names, ids,
# addresses, contact data). ``privacy.record_fields`` replaces the list, ``record_fields_allow``
# removes names from it.
RECORD_FIELDS = (
    "name", "full_name", "first_name", "last_name", "display_name", "company", "company_name", "business_name", "legal_name",
    "entity_name", "account_name", "customer_name", "contact", "contact_name", "address", "address_line1", "address_line2",
    "street", "city", "zip", "zipcode", "postal_code", "phone", "phone_number", "mobile", "fax", "tax_id", "taxid", "ein",
    "tin", "ssn", "vat", "vat_id", "registration_number", "date_of_birth", "dob",
)


class RedactionFailed(SuError, RuntimeError):
    """A redactor could not finish (engine timeout): the text is refused, never passed through."""


@dataclass(frozen=True)
class Redactor:
    """One named redactor: its regexes, what replaces a match (``mask`` or ``hash``), and an
    optional ``accept`` check on the matched text (``ipv6`` validates the address)."""

    name: str
    regexes: tuple[str, ...]
    action: str = "mask"  # "mask" | "hash"
    accept: Callable[[str], str | None] | None = None  # the part to redact, or None to keep the match
    # How far the ``value`` group's payload reaches (scanned in Python, linear, no regex), one mode
    # for every regex or a tuple aligned with ``regexes``: ``smart`` (a quoted string to its closing
    # quote, a JSON object/array to its matching bracket, an SQL literal to its closing quote,
    # otherwise to the next delimiter), ``smart_eol`` (the same, but an unquoted value runs to the
    # end of the line), ``eol`` (to the end of the line), ``paren`` (to the parenthesis that closes
    # the one before the value). ``None``: the regex match itself.
    extend: str | tuple[str | None, ...] | None = None
    marker: str | None = None  # mask written as ``<marker>`` instead of ``[REDACTED_<NAME>]``
    stops: tuple[str, ...] = ()  # ``prose`` mode: lower-case names that end one masked span (kept visible)

    def mode(self, index: int) -> str | None:
        return self.extend[index] if isinstance(self.extend, tuple) else self.extend

    def with_action(self, action: str) -> "Redactor":
        return Redactor(self.name, self.regexes, action, self.accept, self.extend, self.marker, self.stops)

    def token(self, value: str, key: bytes | None) -> str:
        label = self.name.upper()
        if self.action == "hash":
            digest = hmac.new(key, value.encode(), hashlib.sha256).hexdigest() if key else hashlib.sha256(value.encode()).hexdigest()
            return f"[{label}:{digest[:12]}]"
        if self.marker:
            return f"<{self.marker}>"
        return f"[REDACTED_{label}]"

    def recipe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "regexes": list(self.regexes),
            "action": self.action,
            "accept": self.accept.__name__ if self.accept else None,
            **({"extend": list(self.extend) if isinstance(self.extend, tuple) else self.extend} if self.extend else {}),
            **({"marker": self.marker} if self.marker else {}),
            **({"stops": list(self.stops)} if self.stops else {}),
        }


_DELIMITERS = set(" \t\r\n,;&)}]")
_CLOSE = {"{": "}", "[": "]", "(": ")"}


def _string_end(text: str, start: int, quote: str, sql: bool) -> int:
    """Index just after the closing ``quote`` of the string opened at ``start`` (``\\`` escapes in
    JSON strings, doubled quotes in SQL literals); the end of the line when unterminated."""
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            return i
        if not sql and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if sql and text[i + 1 : i + 2] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def _bracket_end(text: str, start: int) -> int:
    """Index just after the bracket matching the one at ``start`` (strings skipped); end of text
    when unbalanced."""
    depth, i = 0, start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _string_end(text, i, ch, False)
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _prose_end(text: str, start: int, stops: tuple[str, ...]) -> int:
    """End of the free text starting at ``start``: the end of the line, or just before the next
    word-bounded ``stops`` name (case-insensitive), trailing spaces excluded."""
    stop = text.find("\n", start)
    stop = len(text) if stop < 0 else stop
    low = text[start:stop].casefold()
    for name in stops:
        i = low.find(name)
        while i >= 0:
            before = low[i - 1] if i else " "
            after = low[i + len(name)] if i + len(name) < len(low) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                stop = min(stop, start + i)
                break
            i = low.find(name, i + 1)
    while stop > start and text[stop - 1] in " \t":
        stop -= 1
    return stop


def extend_value(text: str, start: int, end: int, mode: str, stops: tuple[str, ...] = ()) -> tuple[int, int]:
    """The payload span of a value that starts at ``start`` (see ``Redactor.extend``)."""
    if mode == "prose":
        return start, _prose_end(text, start, stops)
    if mode == "eol":
        stop = text.find("\n", start)
        return start, len(text) if stop < 0 else stop
    if mode == "paren":  # the value sits right after an opening "(": stop before its closing ")"
        depth, i = 1, start  # row values are rendered unquoted: quotes are text, parentheses nest
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return start, i
            elif ch == "\n":
                return start, i
            i += 1
        return start, len(text)
    first = text[start : start + 1]
    if mode == "smart_eol" and first not in ("\"", "'") and first not in _CLOSE:
        return extend_value(text, start, end, "eol")
    if first in ("\"", "'"):
        close = _string_end(text, start, first, first == "'")
        inner_end = close - 1 if close <= len(text) and text[close - 1 : close] == first and close - 1 > start else close
        return start + 1, inner_end  # inside the quotes: the quotes stay, the JSON stays valid
    if first in _CLOSE:
        return start, _bracket_end(text, start)
    i = start
    while i < len(text) and text[i] not in _DELIMITERS:
        i += 1
    return start, max(i, end)


def _ipv6(text: str) -> str | None:
    """The IPv6 address in a candidate run of hex digits and colons (a trailing ``:`` is dropped),
    when it is one and is unmistakably IPv6 (``::`` or eight groups): never a clock time."""
    candidate = text.rstrip(":") if not text.endswith("::") else text
    if candidate.count(":") < 2 or ("::" not in candidate and candidate.count(":") != 7):
        return None
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return None
    return candidate


_KV_SEP = r"""(?P<sep>["']?[ \t]{0,3}[:=][ \t]{0,3}["']?)"""
# without the opening quote of the value: the payload extension reads a quoted value whole
_KV_SEP_BARE = r"""(?P<sep>["']?[ \t]{0,3}[:=][ \t]{0,3})"""
BUILTIN: dict[str, Redactor] = {
    r.name: r
    for r in (
        Redactor("url_credentials", (r"(?P<keep>\b[A-Za-z][A-Za-z0-9+.-]{0,20}://)(?P<value>[^\s/:@]{1,256}:[^\s/@]{1,256})@",)),
        Redactor("bearer_token", (r"(?i)(?P<keep>\bbearer[ \t]{1,4})(?P<value>[A-Za-z0-9._~+/=-]{8,1000})",)),
        Redactor("jwt", (r"\beyJ[A-Za-z0-9_-]{4,1000}\.[A-Za-z0-9_-]{4,1000}\.[A-Za-z0-9_-]{0,1000}",)),
        Redactor(
            "api_key",
            (
                r"(?i)(?P<keep>\b(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|passwd|pwd|token)"
                + _KV_SEP
                + r")(?P<value>[^\s\"'&,;]{1,1000})",
                r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,200}",
                r"\bgh[pousr]_[A-Za-z0-9]{20,255}",
                r"\bxox[abprs]-[A-Za-z0-9-]{10,200}",
            ),
        ),
        Redactor("aws_key", (r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",)),
        Redactor(
            "request_id",
            (r"(?i)(?P<keep>\b(?:x-)?(?:request|req|trace|span|correlation|transaction)[-_]?id" + _KV_SEP + r")(?P<value>[A-Za-z0-9._:-]{6,128})",),
            "hash",
        ),
        Redactor("uuid", (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",), "hash"),
        Redactor("email", (r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}",)),
        Redactor("query_values", (r"(?P<keep>[?&][A-Za-z0-9_.%\[\]-]{1,128}=)(?P<value>[^&#\s\"'<>]{1,1000})",)),
        Redactor("ipv6", (r"[0-9A-Fa-f:]{2,39}",), accept=_ipv6),
        Redactor("ipv4", (r"\b[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b",)),
        Redactor("hex_secret", (r"\b[0-9a-fA-F]{32,128}\b",)),
        Redactor(
            "llm_text",
            (
                r"(?i)(?P<keep>\b(?:prompt|completion|system_prompt|user_prompt|llm_input|llm_output|llm_response|model_output|generated_text)"
                + _KV_SEP_BARE
                + r")(?P<value>[^\s])",
                r"(?i)(?P<keep>\"(?:content|messages|prompt|completion|choices)\"[ \t]{0,3}:[ \t]{0,3})(?P<value>[\"\[{])",
            ),
            extend=("smart_eol", "smart"),
            marker="llm_text",
        ),
        Redactor(
            "content",
            (
                r"(?i)(?P<keep>\"(?:_source|body|passage|passages|snippet|snippets|page_content|chunk_text|document_text|doc_text|highlight|fulltext|full_text)\"[ \t]{0,3}:[ \t]{0,3})(?P<value>[\"\[{])",
                r"(?i)(?P<keep>\b(?:passage|snippet|document_text|page_content|chunk_text)[ \t]{0,3}[:=][ \t]{0,3})(?P<value>[^\s])",
            ),
            extend=("smart", "smart_eol"),
            marker="content",
        ),
        Redactor(
            "query",
            (
                r"(?i)(?P<keep>\b(?:q|query|search_query|searchstring|search_string|search_term|searchterm|keyword|keywords|user_query|original_query|corrected_query|didyoumean|did_you_mean|suggestion|suggestions|spellcheck|spell_check|misspelled|correction)"
                + _KV_SEP_BARE
                + r")(?P<value>[^\s{])",
            ),
            extend="smart",
            marker="query",
        ),
        Redactor(
            "tracking_id",
            (
                r"(?i)(?P<keep>\b(?:set-)?cookie[ \t]{0,3}:[ \t]{0,3})(?P<value>[^\s])",
                r"(?i)(?P<keep>\b(?:_ga|_gid|_gat|_fbp|_fbc|sessionid|session_id|sid|jsessionid|phpsessid|visitor_?id|tracking_?id|anonymous_?id|anon_?id|device_?id|client_?id|user_?token)"
                + _KV_SEP
                + r")(?P<value>[A-Za-z0-9._%:+/=-]{4,512})",
            ),
            "hash",
            extend=("eol", None),
        ),
        Redactor(
            "pg_row",
            (
                r"(?P<keep>\bKey \([^()\n]{1,500}\)=\()(?P<value>[^)\n])",
                r"(?P<keep>\bFailing row contains \()(?P<value>[^)\n])",
                r"(?P<keep>\$[0-9]{1,4} = )(?P<value>[^\s,])",
            ),
            extend=("paren", "paren", "smart"),
            marker="value",
        ),
    )
}



def builtin_redactors(names: list[str] | tuple[str, ...]) -> list[Redactor]:
    """The named built-ins in the fixed application order."""
    unknown = sorted(set(names) - set(ORDER))
    if unknown:
        raise ValueError(f"unknown redactors {unknown}; known: {list(ORDER)}")
    # ``product_prose`` needs its configured names: built by ``configured_redactors`` only
    return [BUILTIN[name] for name in ORDER if name in names and name in BUILTIN]


def _disjoint(spans: Any) -> list[tuple[int, int]]:
    """Extended spans in order, dropping any that starts inside the previous one (a JSON object
    whose inner fields matched too is replaced once, as a whole)."""
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start < out[-1][1]:
            continue
        out.append((start, end))
    return out


def record_payload(fields: list[str] | tuple[str, ...]) -> Redactor:
    """``record_payload``: the values of the named JSON fields of a structured record
    (``"legal_name": "…"`` → ``"legal_name": "<record>"``), objects and arrays included."""
    names = sorted({f.casefold() for f in fields}, key=lambda f: (-len(f), f))
    alternation = "|".join(re.escape(n) for n in names)
    return Redactor("record_payload", (r"(?i)(?P<keep>\"(?:" + alternation + r")\"[ \t]{0,3}:[ \t]{0,3})(?P<value>[\"\[{0-9A-Za-z])",), extend="smart", marker="record")


BUILTIN["record_payload"] = record_payload(RECORD_FIELDS)


def _phone(text: str) -> str | None:
    """The phone number in a candidate (trailing separators and an unclosed parenthesis dropped)
    when it has 7–15 digits (E.164 allows 15), and 8 or more after a ``+``."""
    end = len(text)
    while end and not text[end - 1].isdigit():
        end -= 1
    candidate = text[:end]
    digits = sum(ch.isdigit() for ch in candidate)
    low = 8 if candidate.startswith("+") else 7
    return candidate if low <= digits <= 15 else None


BUILTIN["phone"] = Redactor(
    "phone",
    (
        # international: +<country code> then digit groups (spaces, dots, dashes, an area code in parentheses)
        r"(?:^|[^0-9A-Za-z_+])(?P<value>\+[0-9]{1,3}(?:[ .-]{0,2}\(?[0-9]{1,5}\)?){1,6})",
        # national with the area code in parentheses: (415) 555-0100
        r"(?:^|[^0-9A-Za-z_(])(?P<value>\([0-9]{2,5}\)[ .-]{0,2}[0-9]{2,5}(?:[ .-][0-9]{2,5}){1,3})",
        # any shape after a phone key: phone=0049 30 1234567, "mobile": "030-1234567"
        r"(?i)(?P<keep>\b(?:phone|phone_?number|telephone|tel|mobile|cell|fax|msisdn)[\"']?[ \t]{0,3}[:=][ \t]{0,3}[\"']?)(?P<value>\+?[0-9(][0-9 ().-]{5,30}[0-9])",
    ),
    accept=_phone,
)
# 13–19 digit runs (card numbers, account and national ids, epoch-millisecond timestamps too): hashed,
# so equal ids stay joinable. A longer run is not an id of this class and is left alone.
BUILTIN["long_numeric_id"] = Redactor("long_numeric_id", (r"\b[0-9]{13,19}\b",), "hash")

# Person-name fields of JSON objects and key=value text. ``privacy.person_name_fields`` replaces the
# key list (matched case-insensitively, so ``firstName`` is ``firstname``).
PERSON_NAME_FIELDS = ("name", "firstname", "first_name", "lastname", "last_name", "fullname", "full_name", "displayname", "display_name", "username", "user_name", "author")


def person_name(fields: list[str] | tuple[str, ...]) -> Redactor:
    """``person_name``: a JSON string value (``"firstName": "…"``), a quoted ``key="…"`` value, or an
    unquoted ``key=Value`` with up to three following capitalised words (``author=Jane Q Doe``)."""
    names = sorted({f.casefold() for f in fields}, key=lambda f: (-len(f), f))
    alternation = "|".join(re.escape(n) for n in names)
    return Redactor(
        "person_name",
        (
            r"(?P<keep>\"(?i:" + alternation + r")\"[ \t]{0,3}:[ \t]{0,3})(?P<value>\")",
            r"(?P<keep>\b(?i:" + alternation + r")[ \t]{0,3}[:=][ \t]{0,3})(?P<value>[\"'])",
            r"(?P<keep>\b(?i:" + alternation + r")[ \t]{0,3}[:=][ \t]{0,3})(?P<value>[^\s\"',;&{}\[\]()<>=]{1,100}(?: [A-Z][A-Za-z'.-]{0,60}){0,3})",
        ),
        extend=("smart", "smart", None),
    )


BUILTIN["person_name"] = person_name(PERSON_NAME_FIELDS)

# JSON arrays of objects under a list key (``"customers": [{…}, …]``): the whole array becomes
# ``<record>``. ``privacy.record_list_fields`` replaces the key list.
RECORD_LIST_FIELDS = (
    "records", "rows", "items", "results", "entities", "accounts", "customers", "clients", "companies", "organizations",
    "organisations", "businesses", "merchants", "vendors", "suppliers", "partners", "contacts", "users", "members", "employees",
)  # fmt: skip


def record_list(fields: list[str] | tuple[str, ...]) -> Redactor:
    """``record_list``: the value of a named JSON key when it is an array of objects (a list of
    business-entity records), replaced whole by ``<record>``; arrays of scalars are kept."""
    names = sorted({f.casefold() for f in fields}, key=lambda f: (-len(f), f))
    alternation = "|".join(re.escape(n) for n in names)
    return Redactor("record_list", (r"(?P<keep>\"(?i:" + alternation + r")\"[ \t]{0,3}:[ \t]{0,3})(?P<value>\[)[ \t\r\n]{0,16}\{",), extend="smart", marker="record")


BUILTIN["record_list"] = record_list(RECORD_LIST_FIELDS)


def product_prose(tokens: list[str] | tuple[str, ...]) -> Redactor:
    """``product_prose``: the free text that follows a known product or technology name, to the end
    of the line, becomes ``<prose>``. The names themselves (``privacy.prose_products``, an
    allow-list) stay visible, and a later name in the same text does not end the masking: the prose
    after it is masked again (``Kafka <prose> Redis <prose>``)."""
    from ..extraction.patterns.library import escape_literal

    names = sorted({t.casefold() for t in tokens}, key=lambda t: (-len(t), t))
    if not names:
        raise ValueError("product_prose needs privacy.prose_products")
    alternation = "|".join(escape_literal(n) for n in names)
    return Redactor(
        "product_prose",
        (r"(?P<keep>\b(?i:" + alternation + r")\b[ \t:,;-]{0,3})(?P<value>[^\s])",),
        extend="prose",
        marker="prose",
        stops=tuple(names),
    )


# ``product_prose`` has no default token list: it exists only when configured (``configured_redactors``).
CONFIGURED_ONLY = frozenset({"product_prose"})
ORDER = (
    "url_credentials", "bearer_token", "jwt", "api_key", "aws_key", "request_id", "uuid", "email", "query_values", "ipv6", "ipv4",
    "phone", "long_numeric_id", "hex_secret", "llm_text", "content", "query", "tracking_id", "pg_row", "record_payload",
    "person_name", "record_list", "product_prose",
)  # fmt: skip
assert set(ORDER) == set(BUILTIN) | CONFIGURED_ONLY


def configured_redactors(privacy: Any) -> list[Redactor]:
    """Built-ins (``redact_emails`` adds ``email``) with ``privacy.actions`` applied and
    ``record_payload`` built from ``record_fields``/``record_fields_allow``, then ``custom``, then
    ``patterns`` (masks, case-insensitive)."""
    names = list(privacy.redactors)
    if privacy.redact_emails and "email" not in names:
        names.append("email")
    builtins = builtin_redactors(names)
    fields = [f for f in (privacy.record_fields if privacy.record_fields is not None else RECORD_FIELDS) if f.casefold() not in {a.casefold() for a in privacy.record_fields_allow}]
    person = getattr(privacy, "person_name_fields", None)
    lists = getattr(privacy, "record_list_fields", None)
    configured = {
        "record_payload": lambda: record_payload(fields),
        "person_name": lambda: person_name(person) if person is not None else BUILTIN["person_name"],
        "record_list": lambda: record_list(lists) if lists is not None else BUILTIN["record_list"],
        "product_prose": lambda: product_prose(getattr(privacy, "prose_products", []) or []),
    }
    by_name = {r.name: r for r in builtins}
    out = [configured[n]() if n in configured else by_name[n] for n in ORDER if n in names]
    out = [r.with_action(privacy.actions[r.name]) if r.name in privacy.actions else r for r in out]
    out += [Redactor(c.name, (c.regex,), c.action) for c in privacy.custom]
    out += [Redactor(label.casefold(), ("(?i)" + regex,)) for label, regex in privacy.patterns.items()]
    return out


@dataclass
class SafeRedactor:
    """The configured safe redaction: ``__call__(text) -> text``; per-redactor ``counts``."""

    redactors: list[Redactor]
    engine: PatternEngine
    hash_key: bytes | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.redactors:
            raise ValueError("a safe redactor needs at least one redactor")
        self._compiled: list[tuple[Redactor, list[Any]]] = []
        for redactor in self.redactors:
            for regex in redactor.regexes:
                errors = [i for i in validate_regex(regex, pattern_id=redactor.name, source="privacy") if i.severity == "error"]
                if errors:
                    raise ValueError(f"redactor {redactor.name!r} is outside the safe engine: {errors[0].message}")
            self._compiled.append((redactor, [self.engine.compile(regex) for regex in redactor.regexes]))
        recipe = {"version": REDACTOR_VERSION, "engine": self.engine.kind, "redactors": [r.recipe() for r in self.redactors], "keyed": self.hash_key is not None}
        self.stamp = "safe:" + hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()[:16]
        self.counts = {r.name: 0 for r in self.redactors}

    @property
    def names(self) -> list[str]:
        return [r.name for r in self.redactors]

    # --- matching ------------------------------------------------------------------------------

    def _hits(self, compiled: Any, text: str) -> list[Hit]:
        try:
            return self.engine.finditer(compiled, text)
        except PatternTimeout as exc:
            raise RedactionFailed(f"redaction did not finish: {exc}") from None

    def _targets(self, redactor: Redactor, hit: Hit, text: str, tokens: list[tuple[int, int]], mode: str | None = None) -> tuple[int, int] | None:
        """The span to replace for one hit (``value`` group when present), or ``None`` to keep it
        (empty, or overlapping a redaction token already in the text)."""
        start, end = hit.groups.get("value") or (hit.start, hit.end)
        if mode and hit.groups.get("value"):
            start, end = extend_value(text, start, end, mode, redactor.stops)
        if end <= start or any(a < hit.end and hit.start < b for a, b in tokens) or TOKEN.match(text[start:end]):
            return None
        if redactor.accept is not None:
            part = redactor.accept(text[start:end])
            if part is None:
                return None
            end = start + len(part)
        return start, end

    def findings(self, text: str) -> list[tuple[str, int, int]]:
        """``(redactor, start, end)`` of every span the redactors would replace in ``text`` (used by
        the leak scan; the text itself is not changed or returned)."""
        found = []
        tokens = [m.span() for m in TOKENS.finditer(text)]
        for redactor, patterns in self._compiled:
            for index, compiled in enumerate(patterns):
                spans = (self._targets(redactor, hit, text, tokens, redactor.mode(index)) for hit in self._hits(compiled, text))
                found.extend((redactor.name, *span) for span in _disjoint(s for s in spans if s is not None))
        return sorted(found, key=lambda f: (f[1], f[0]))

    def redact(self, text: str) -> tuple[str, dict[str, int]]:
        """``(redacted text, replacements per redactor)``."""
        made: dict[str, int] = {}
        for redactor, patterns in self._compiled:
            for index, compiled in enumerate(patterns):
                tokens = [m.span() for m in TOKENS.finditer(text)]
                spans = _disjoint(s for s in (self._targets(redactor, hit, text, tokens, redactor.mode(index)) for hit in self._hits(compiled, text)) if s is not None)
                if not spans:
                    continue
                pieces, last = [], 0
                for start, end in spans:
                    pieces.append(text[last:start])
                    pieces.append(redactor.token(text[start:end], self.hash_key))
                    last = end
                pieces.append(text[last:])
                text = "".join(pieces)
                made[redactor.name] = made.get(redactor.name, 0) + len(spans)
        for name, count in made.items():
            self.counts[name] = self.counts.get(name, 0) + count
        return text, made

    def __call__(self, text: str) -> str:
        return self.redact(text)[0]

    def report(self) -> dict[str, Any]:
        """Content-free: the stamp, engine and replacement counts per redactor. The counts are
        ``[name, count]`` pairs, not a mapping: ``"api_key": 0`` would itself look like a secret
        assignment to the leak scan."""
        return {"engine": "safe", "stamp": self.stamp, "regex_engine": self.engine.kind, "counts": [[name, count] for name, count in self.counts.items()]}


def safe_redactor(privacy: Any, env: Mapping[str, str] | None = None, engine: PatternEngine | None = None) -> SafeRedactor | None:
    """The configured safe redactor, or ``None`` under ``privacy.engine: legacy``. The engine is the
    configured one (``PatternEngineUnavailable`` when it is not installed: never another engine)."""
    if getattr(privacy, "engine", "legacy") != "safe":
        return None
    environ = os.environ if env is None else env
    key = None
    if privacy.hash_key_env:
        value = environ.get(privacy.hash_key_env)
        if not value:
            raise ValueError(f"privacy.hash_key_env names {privacy.hash_key_env}, which is not set")
        key = value.encode()
    engine = engine or select_engine(privacy.regex_engine, timeout_ms=privacy.regex_timeout_ms)
    return SafeRedactor(configured_redactors(privacy), engine, key)
