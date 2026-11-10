"""Record-level source view (legacy ``GET /source``): ``{id, kind, source_label, title, title_derived,
external_url, sections[{heading, text}], metadata}`` from a catalog ``Document``.

The record is read with the engine's ``get_document`` tool (pinned generation, access scope). A record
hidden by the profile's visibility rule is answered exactly like an unknown id (404, the same text),
so hidden records cannot be probed here. Credential-like strings are masked before anything is
returned. The same two rules apply to every other response (answers, streams, search): see
``outgoing.py``.
"""

from __future__ import annotations

import re
from typing import Any

from ...core.types import Document
from .profile import DatasetProfile

NOT_AVAILABLE = "source is not available"
REDACTED = "[redacted]"
# Conservative, generic credential shapes: key=value secrets, bearer/basic auth headers, URL userinfo,
# private-key blocks and long opaque tokens with a known vendor-neutral prefix shape. A key=value value must
# look like a secret (quoted, >=3 chars with a digit, or >=16 token chars) so prose such as
# "API token: log in ..." or Markdown ("token:**") is not masked.
_REDACTIONS = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|token)\b(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|(?=[^\s,;*]*\d)[^\s,;*]{3,}|[A-Za-z0-9._~+/=-]{16,})"),
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic))\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|pk|rk|xox[abpr])[-_][A-Za-z0-9_-]{16,}\b"),
]


def redact(text: str) -> str:
    out = _REDACTIONS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    out = _REDACTIONS[1].sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _REDACTIONS[2].sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _REDACTIONS[3].sub(lambda m: f"{m.group(1)}{REDACTED}@", out)
    out = _REDACTIONS[4].sub(REDACTED, out)
    return _REDACTIONS[5].sub(REDACTED, out)


def hidden(profile: DatasetProfile, document: Document) -> bool:
    """The profile's visibility predicate: any rule matching hides the record."""
    for rule in profile.source.hide:
        if rule.field == "source":
            value: Any = document.source
        elif rule.field == "kind":
            value = document.kind
        else:
            value = document.metadata.get(rule.field)
        if value is None:
            continue
        if str(value).casefold() in {v.casefold() for v in rule.values}:
            return True
    return False


def _sections(profile: DatasetProfile, document: Document) -> list[dict[str, str]]:
    view = profile.source
    fields = document.fields
    out: list[dict[str, str]] = []
    if view.sections:
        for section in view.sections:
            text = fields.get(section.field)
            if text:
                out.append({"heading": section.heading, "text": text})
    else:
        prefix = view.repeated_field_prefix
        for name, text in fields.items():
            if prefix and name.startswith(prefix):
                continue
            if text:
                out.append({"heading": name.replace("_", " ").strip().capitalize(), "text": text})
    if view.repeated_field_prefix:
        prefix = view.repeated_field_prefix
        numbered = []
        for name, text in fields.items():
            suffix = name[len(prefix) :]
            if name.startswith(prefix) and suffix.isdigit() and text:
                numbered.append((int(suffix), text))
        for n, text in sorted(numbered):
            out.append({"heading": f"{prefix.rstrip('_').replace('_', ' ').capitalize()} {n}", "text": text})
    return out[: view.section_limit]


def source_record(profile: DatasetProfile, document: Document) -> dict[str, Any]:
    view = profile.source
    title = document.title.strip()
    derived = False
    if not title and view.title_fallback_field:
        first = (document.fields.get(view.title_fallback_field) or "").strip().splitlines()
        if first:
            title, derived = first[0][:200], True
    metadata = {k: v for k, v in document.metadata.items() if not view.metadata_keys or k in view.metadata_keys}
    sections = _sections(profile, document)
    if view.redact:
        sections = [{"heading": s["heading"], "text": redact(s["text"])} for s in sections]
        metadata = {k: redact(v) if isinstance(v, str) else v for k, v in metadata.items()}
        title = redact(title)
    kind = view.kind_labels.get(document.source) or view.kind_labels.get(document.kind) or document.kind
    return {
        "id": document.id,
        "kind": kind,
        "source_label": profile.source_label(document.source),
        "title": title or None,
        "title_derived": derived,
        "external_url": document.url,
        "sections": sections,
        "metadata": metadata,
        "section_count": len(sections),
    }
