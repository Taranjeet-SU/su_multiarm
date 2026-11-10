"""One output filter for every text the adapter returns (M7): the record page's rules everywhere.

``GET /source`` masks credential-like strings (``source.redact``) and refuses records matched by the
visibility rule (``source.hide``). The same two rules apply to every other surface:

* ``/api/qa`` (and ``final``): the answer text, citation titles and snippets, the per-arm panels
  (``dense``/``lexical``/``graph``/``cases``), ``fusion`` titles and the ``sql`` rows;
* ``/api/qa/stream``: the ``evidence`` event and the provisional ``token`` text (masked as it is
  released; see ``stream.ProvisionalText``);
* ``/api/search``: ``results`` (title, summary, enriched note, metadata), ``evidence``, ``facts`` and
  the ``structured`` rows.

Hidden records are removed from every list (citation numbers are not reused: the remaining numbers
keep their packing order, so stream and ``final`` agree). The generator may already have read a
hidden record when it was packed as evidence, so an answer whose packed evidence contains one is
**withheld** (fail closed): the answer text is replaced by a fixed notice, ``insufficient_evidence``
is set and the reason is reported in ``warnings`` and ``compat.withheld``. Records are checked with
the engine's ``get_document`` tool (pinned generation, the caller's access scope); a record that
cannot be read back is treated as hidden. Without hide rules no record is read.

Masking is pattern-based (``source.redact``): it lowers exposure, it is not a guarantee. Redacting
the corpus before indexing (``privacy`` redactors) keeps secrets away from the generator as well.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from ...core.types import Document
from .profile import DatasetProfile
from .source import hidden, redact
from .stream import utf8_safe

WITHHELD = "This answer was withheld: it drew on records that are not available on this server."
GET_DOCUMENT_BATCH = 50


def text_filter(profile: DatasetProfile) -> Callable[[str], str]:
    """The masking applied to one outgoing string (lone surrogates are always replaced)."""
    if profile.source.redact:
        return lambda text: redact(utf8_safe(text))
    return utf8_safe


def _value(value: Any, mask: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [_value(v, mask) for v in value]
    if isinstance(value, dict):
        return {k: _value(v, mask) for k, v in value.items()}
    return value


def hidden_ids(profile: DatasetProfile, engine: Any, ids: Iterable[str], who: Any) -> set[str]:
    """The ids among ``ids`` the profile's visibility rule hides (or that cannot be read back)."""
    if not profile.source.hide:
        return set()
    wanted = list(dict.fromkeys(str(i) for i in ids if i))
    out: set[str] = set()
    for start in range(0, len(wanted), GET_DOCUMENT_BATCH):
        batch = wanted[start : start + GET_DOCUMENT_BATCH]
        output = engine.call_tool("get_document", {"ids": batch}, who).result.output
        documents = output.get("documents") if isinstance(output, dict) else getattr(output, "documents", [])
        found = {}
        for item in documents or []:
            document = item if isinstance(item, Document) else Document.model_validate(item)
            found[document.id] = document
        out.update(i for i in batch if i not in found or hidden(profile, found[i]))
    return out


def add_links(env: dict[str, Any], engine: Any, profile: DatasetProfile, who: Any) -> None:
    """Labelled cross-source links on the citations of a unified-collection answer (M2b-2): read
    through the engine's pinned link view under the caller's access (``Engine.links_for``), then
    the profile's visibility rule on the linked records. Links never grant access."""
    from .envelope import attach_links

    citations = env.get("citations") or []
    ids = [str(c["record_id"]) for c in citations if c.get("record_type") != "aggregate" and c.get("record_id")]
    links = engine.links_for(ids, who) if hasattr(engine, "links_for") else {}
    others = {e.get("other_id") for entries in links.values() for e in entries}
    hidden = hidden_ids(profile, engine, [o for o in others if o], who) if others else set()
    shown = attach_links(citations, links, hidden)
    coverage = env.get("coverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("links"), dict):
        coverage["links"]["shown"] = shown


def qa_record_ids(env: dict[str, Any]) -> set[str]:
    """Every record id an answer envelope lists (citations, panels, fusion)."""
    ids = {c.get("record_id") for c in env.get("citations") or [] if c.get("record_type") != "aggregate"}
    for panel in ("dense", "lexical", "cases"):
        ids.update(r.get("record_id") for r in (env.get(panel) or {}).get("results") or [])
    ids.update(r.get("record_id") for r in (env.get("graph") or {}).get("records") or [])
    ids.update(r.get("record_id") for r in (env.get("fusion") or {}).get("results") or [])
    ids.update(v for v in ((env.get("compat") or {}).get("evidence_ids") or {}).values())
    return {i for i in ids if i}


def filter_citations(citations: list[dict[str, Any]], hide: set[str], mask: Callable[[str], str]) -> list[dict[str, Any]]:
    out = []
    for citation in citations:
        if citation.get("record_id") in hide:
            continue
        item = dict(citation)
        for key in ("title", "snippet"):
            if isinstance(item.get(key), str):
                item[key] = mask(item[key])
        out.append(item)
    return out


def filter_qa(env: dict[str, Any], profile: DatasetProfile, hide: set[str]) -> dict[str, Any]:
    """The answer envelope with the visibility rule and the masking applied (in place; returned)."""
    mask = text_filter(profile)
    compat = env.setdefault("compat", {})
    packed = set((compat.get("evidence_ids") or {}).values())
    withheld = sorted(packed & hide)
    env["citations"] = filter_citations(env.get("citations") or [], hide, mask)
    compat["evidence_ids"] = {n: rid for n, rid in (compat.get("evidence_ids") or {}).items() if rid not in hide}
    for panel in ("dense", "lexical", "cases"):
        block = env.get(panel)
        if block:
            block["results"] = [{**r, "title": mask(r.get("title") or ""), "text": mask(r.get("text") or ""), **({"subject": mask(r["subject"])} if isinstance(r.get("subject"), str) else {})} for r in block.get("results") or [] if r.get("record_id") not in hide]
    graph = env.get("graph")
    if graph:
        graph["records"] = [{**r, "title": mask(r.get("title") or ""), "text": mask(r.get("text") or "")} for r in graph.get("records") or [] if r.get("record_id") not in hide]
    fusion = env.get("fusion")
    if fusion:
        fusion["results"] = [{**r, "title": mask(r.get("title") or "")} for r in fusion.get("results") or [] if r.get("record_id") not in hide]
    if env.get("sql"):
        env["sql"]["rows"] = _value(env["sql"].get("rows") or [], mask)
    if withheld:
        env["answer"] = WITHHELD
        env["insufficient_evidence"] = True
        env.setdefault("warnings", []).append(f"the answer was withheld: {len(withheld)} packed evidence record(s) are hidden on this server (source.hide)")
        compat["withheld"] = {"reason": "hidden_evidence", "records": len(withheld)}
        compat["cited"] = [n for n in compat.get("cited") or [] if str(n) in compat["evidence_ids"]]
    else:
        env["answer"] = mask(env.get("answer") or "")
    if hide:
        compat["hidden_records"] = len(hide)
    return env


def filter_search(env: dict[str, Any], profile: DatasetProfile, hide: set[str]) -> dict[str, Any]:
    """The search envelope with the visibility rule and the masking applied (in place; returned)."""
    mask = text_filter(profile)
    env["searchString"] = utf8_safe(env.get("searchString") or "")
    results = []
    for result in env.get("results") or []:
        if result.get("id") in hide:
            continue
        item = dict(result)
        for key in ("title", "summary", "enrichedSummary"):
            if isinstance(item.get(key), str):
                item[key] = mask(item[key])
        item["metadata"] = _value(item.get("metadata") or {}, mask)
        results.append(item)
    env["results"] = results
    evidence = []
    for entry in env.get("evidence") or []:
        if entry.get("type") != "sql" and entry.get("id") in hide:
            continue
        evidence.append({**entry, "title": mask(entry.get("title") or ""), "text": mask(entry.get("text") or "")})
    env["evidence"] = evidence
    env["facts"] = [{**f, "text": mask(f.get("text") or "")} for f in env.get("facts") or []]
    for block in env.get("structured") or []:
        block["rows"] = _value(block.get("rows") or [], mask)
    if hide:
        env.setdefault("compat", {})["hidden_records"] = len(hide)
    return env
