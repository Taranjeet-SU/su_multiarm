"""Canonical types (plan §3.1): span equality with source, containment, edge kind invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from su_multi_arm.core.types import (
    Chunk,
    Document,
    Entity,
    Evidence,
    Event,
    ExtractionOutput,
    GenerationId,
    Path,
    PathEdge,
    Predicate,
    Provenance,
    Relation,
    Span,
    StructuredRecord,
)
from su_multi_arm.models import Span as ChunkSpan

SOURCE = "The AACTA awarded\nthe Film."
PROV = Provenance(extractor="test")


def span(start: int, end: int, chunk: str = "c1", level: str = "exact") -> Span:
    return Span(document_id="d1", chunk_id=chunk, field="body", start=start, end=end, text=SOURCE[start:end], match_level=level)


def test_span_text_equals_source_slice_and_offsets_must_match():
    s = span(4, 9)
    assert s.text == "AACTA" and s.verify(SOURCE)
    with pytest.raises(ValidationError, match="offsets"):
        Span(document_id="d1", chunk_id="c1", field="body", start=0, end=3, text="AACTA", match_level="exact")
    with pytest.raises(ValidationError):
        Span(document_id="d1", chunk_id="c1", field="body", start=5, end=5, text="", match_level="exact")
    assert not span(4, 9).model_copy(update={"text": "AACTB"}).verify(SOURCE)


def test_containment_requires_same_chunk():
    outer, inner = span(0, 26), span(4, 9)
    assert outer.contains(inner) and not inner.contains(outer)
    assert not outer.contains(span(4, 9, chunk="c2"))


def test_from_chunk_span_anchors_offsets_to_the_field():
    chunk = Chunk(id="c7", document_id="d1", field="body", start=100, end=100 + len(SOURCE), text=SOURCE)
    local = ChunkSpan(start=4, end=9, text="AACTA", match_level="normalised")
    anchored = Span.from_chunk_span(chunk, local)
    assert (anchored.start, anchored.end, anchored.text, anchored.match_level) == (104, 109, "AACTA", "normalised")
    with pytest.raises(ValueError, match="inside the chunk"):
        Span.from_chunk_span(chunk, ChunkSpan(start=4, end=9, text="XXXXX", match_level="exact"))


def test_relation_edge_kind_and_support_invariants():
    with pytest.raises(ValidationError, match="predicate support"):
        Relation(subject_id="a", predicate="awarded", object_id="b", span=span(0, 26), edge_kind="fact", support="unchecked", provenance=PROV)
    with pytest.raises(ValidationError, match="reserved"):
        Relation(subject_id="a", predicate="awarded", object_id="b", span=span(0, 26), edge_kind="fact", support="entailment_checked", provenance=PROV)
    co = Relation(subject_id="a", predicate="at", object_id="b", span=span(0, 26), edge_kind="co_occurrence", support="unchecked", provenance=PROV)
    assert co.edge_kind == "co_occurrence"


def test_extraction_output_requires_endpoints_inside_relation_span():
    a = Entity(id="a", name="AACTA", type="org", span=span(4, 9))
    b = Entity(id="b", name="Film", type="work", span=span(22, 26))
    fact = Relation(subject_id="a", predicate="awarded", object_id="b", span=span(0, 26), edge_kind="fact", support="trigger_in_span", provenance=PROV)
    out = ExtractionOutput(entities=[a, b], relations=[fact])
    assert len(out.relations) == 1
    narrow = fact.model_copy(update={"span": span(0, 17)})  # does not contain "Film"
    with pytest.raises(ValidationError, match="contain both endpoint"):
        ExtractionOutput(entities=[a, b], relations=[narrow])
    with pytest.raises(ValidationError, match="typed entities"):
        ExtractionOutput(entities=[a], relations=[fact])
    other_chunk = b.model_copy(update={"span": span(22, 26, chunk="c2")})
    with pytest.raises(ValidationError, match="same chunk"):
        ExtractionOutput(entities=[a, other_chunk], relations=[fact])
    with pytest.raises(ValidationError, match="duplicate entity"):
        ExtractionOutput(entities=[a, a])
    with pytest.raises(ValidationError, match="participant"):
        ExtractionOutput(entities=[a], events=[Event(type="award", participants=[("winner", "zz")], span=span(0, 26), provenance=PROV)])


def test_records_paths_evidence_and_generation_id():
    record = StructuredRecord(schema_id="document", id="r1", values={"product": "X"}, spans={"product": span(4, 9)}, provenance=PROV)
    assert record.values["product"] == "X"
    with pytest.raises(ValidationError, match="anchor a value"):
        StructuredRecord(schema_id="document", id="r1", values={}, spans={"product": span(4, 9)}, provenance=PROV)
    edge = PathEdge(subject_id="a", predicate="p", object_id="b", edge_kind="fact", support="trigger_in_span", span=span(0, 26), provenance=PROV)
    assert Path(nodes=["a", "b"], edges=[edge]).edges[0].edge_kind == "fact"
    with pytest.raises(ValidationError, match="one more node"):
        Path(nodes=["a"], edges=[edge])
    with pytest.raises(ValidationError):
        Evidence(id="e", kind="cache")  # cached answers are never evidence
    gen = GenerationId(dataset="demo", generation="a" * 32, index_format=3, fingerprint="b" * 64)
    assert gen.prefix() == "demo/" + "a" * 32 + "/" and gen.is_prefix_of(gen.prefix() + "answer:x")
    with pytest.raises(ValidationError):
        GenerationId(dataset="demo", generation="xyz", index_format=3, fingerprint="b" * 64)


def test_document_and_chunk_invariants():
    doc = Document(id="d1", kind="note", metadata={"n": 1, "flag": True, "none": None})
    assert doc.kind == "note" and doc.source == "article"
    with pytest.raises(ValidationError):
        Document(id="d1", fields={"title": "x"})
    with pytest.raises(ValidationError, match="offsets"):
        Chunk(id="c", document_id="d", field="body", start=0, end=5, text="abc")
    with pytest.raises(ValidationError):
        Predicate(field="f", op="in", value="not-a-list")
    with pytest.raises(ValidationError):
        Predicate(field="f", op="date_interval", lower="2024-01-01T00:00:00+00:00")
