"""``communities.strategy: imported``: communities built outside the package, admitted only with
grounded reports.

Input: a JSONL file pinned by ``communities.imported.sha256``, one community per line::

    {"id": "c1", "label": "...", "level": 0, "parent_id": null,
     "members": ["<document id>", ...],
     "claims": [{"text": "...", "evidence": [{"document_id": "<member id>", "quote": "<verbatim>"}]}],
     "summary": "...", "stats": {"<name>": <scalar>}}

Membership is the declared ``members`` that exist in the generation (absent ids are counted, never
guessed). A claim is admitted only when every evidence quote occurs verbatim in a chunk of its
cited document and that document is a member; the stored reference carries the chunk id. An
ungrounded claim is dropped and counted (``on_ungrounded: drop_claim``) or fails the build
(``fail``). Free summary prose (``summary``) is never stored as a claim: the package refuses
ungrounded summaries; it is counted as ``summary_refused``. With ``extractive_sentences: n`` a
community left without any grounded claim receives up to ``n`` extractive claims: verbatim member
sentences that share the most terms with the community label (deterministic order), each citing
itself. Reports are ``complete_input`` with ``origin: imported`` (or ``extractive``), so the
community arm and coverage accounting treat them like any grounded report. No model is called.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from ..core.types import Chunk, JsonScalar
from ..models import StrictModel
from ..providers import tokens
from .base import Community

SENTENCE = re.compile(r"[^.!?\n]{20,400}[.!?]")


class ImportedEvidence(StrictModel):
    document_id: str = Field(min_length=1)
    quote: str = Field(min_length=1, max_length=4000)


class ImportedClaim(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    evidence: list[ImportedEvidence] = Field(min_length=1, max_length=20)


class ImportedCommunity(StrictModel):
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(default="", max_length=500)
    level: int = Field(default=0, ge=0, le=2)
    parent_id: str | None = None
    members: list[str] = Field(default_factory=list)
    claims: list[ImportedClaim] = Field(default_factory=list)
    summary: str | None = None  # refused: never stored (ungrounded prose)
    stats: dict[str, JsonScalar] = Field(default_factory=dict)


class CommunityImportError(ValueError):
    """The community import file is missing, tampered with or (under ``fail``) ungrounded."""


def _locate(quote: str, chunks: list[Chunk]) -> str | None:
    for chunk in chunks:
        if quote in chunk.text:
            return chunk.id
    return None


def _extractive(community: ImportedCommunity, members: list[str], by_doc: dict[str, list[Chunk]], limit: int) -> list[dict[str, Any]]:
    label_terms = set(tokens(community.label))
    candidates: list[tuple[int, str, int, str, str]] = []
    for doc_id in members:
        for chunk in by_doc.get(doc_id, []):
            for match in SENTENCE.finditer(chunk.text):
                sentence = match.group(0).strip()
                overlap = len(label_terms & set(tokens(sentence)))
                if overlap:
                    candidates.append((-overlap, doc_id, match.start(), chunk.id, sentence))
    candidates.sort()
    out, seen = [], set()
    for _neg, doc_id, _start, chunk_id, sentence in candidates:
        if sentence in seen:
            continue
        seen.add(sentence)
        out.append({"text": sentence, "evidence": [{"document_id": doc_id, "chunk_id": chunk_id, "quote": sentence}], "kind": "extractive"})
        if len(out) >= limit:
            break
    return out


def imported_communities(cfg: Any, corpus_ids: set[str], chunks: list[Chunk]) -> tuple[list[Community], dict[str, Any]]:
    """``cfg``: ``CommunitiesConfig.imported``. Returns the communities and an import report."""
    path = Path(cfg.path)
    if not path.is_file():
        raise CommunityImportError(f"communities.imported.path {path} does not exist")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != cfg.sha256:
        raise CommunityImportError(f"communities.imported.path {path} does not match communities.imported.sha256")
    by_doc: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.document_id, []).append(chunk)
    report: dict[str, Any] = {"communities_in": 0, "communities": 0, "dropped_no_members": 0, "absent_members": 0, "claims_in": 0, "claims_grounded": 0, "claims_dropped": 0, "summary_refused": 0, "extractive_communities": 0}
    out: list[Community] = []
    seen: set[str] = set()
    for number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = ImportedCommunity.model_validate_json(line)
        except ValueError as exc:
            raise CommunityImportError(f"{path}:{number}: invalid community row: {exc}") from exc
        if row.id in seen:
            raise CommunityImportError(f"{path}:{number}: duplicate community id {row.id!r}")
        seen.add(row.id)
        report["communities_in"] += 1
        members = sorted({m for m in row.members if m in corpus_ids})
        report["absent_members"] += len(set(row.members)) - len(members)
        if len(members) < cfg.min_members:
            report["dropped_no_members"] += 1
            continue
        if row.summary:
            report["summary_refused"] += 1
        claims: list[dict[str, Any]] = []
        dropped = 0
        for claim in row.claims:
            report["claims_in"] += 1
            refs = []
            for evidence in claim.evidence:
                chunk_id = _locate(evidence.quote, by_doc.get(evidence.document_id, [])) if evidence.document_id in members else None
                if chunk_id is None:
                    refs = None
                    break
                refs.append({"document_id": evidence.document_id, "chunk_id": chunk_id, "quote": evidence.quote})
            if refs is None:
                if cfg.on_ungrounded == "fail":
                    raise CommunityImportError(f"community {row.id}: claim {claim.text[:60]!r} is not grounded in a member passage")
                dropped += 1
                continue
            claims.append({"text": claim.text, "evidence": refs})
        report["claims_grounded"] += len(claims)
        report["claims_dropped"] += dropped
        origin = "imported"
        if not claims and cfg.extractive_sentences:
            claims = _extractive(row, members, by_doc, cfg.extractive_sentences)
            origin = "extractive"
            report["extractive_communities"] += 1 if claims else 0
        status = "complete_input" if claims else "no_evidence_within_budget"
        out.append(
            Community(
                id=row.id,
                label=row.label,
                document_ids=members,
                entity_ids=[],
                level=row.level,
                parent_id=row.parent_id,
                stats=dict(row.stats),
                report={"status": status, "claims": claims, "origin": origin, "dropped_claims": dropped, "summary": "refused_ungrounded" if row.summary else None},
            )
        )
    report["communities"] = len(out)
    report["sha256"] = cfg.sha256
    return sorted(out, key=lambda c: c.id), report


def import_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


__all__ = ["CommunityImportError", "ImportedCommunity", "import_digest", "imported_communities"]


class ImportedCommunityLayer:
    """``CommunityLayerReader`` of an imported community layer (M1b): the communities an adopted
    store materialised from the pinned import file, read through a graph facet that answers for the
    unified generation (``PartitionedGraph``), which verifies the layer's digest first."""

    strategy = "imported"

    def __init__(self, name: str, sources: tuple[str, ...], graph: Any):
        self.name, self.sources, self._graph = name, tuple(sources), graph

    def communities(self, gen: Any) -> list[Community]:
        return list(self._graph.communities(gen))
