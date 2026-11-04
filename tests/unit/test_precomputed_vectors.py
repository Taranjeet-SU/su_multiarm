"""Imported document vectors (``models.roles.embedder.precomputed``), strict source order and the
bounded Faiss search. Synthetic data only.

* every chunk is matched to an imported row by (document, field, SHA-256 of its exact text), never by
  position; the audit refuses row/key count mismatches, wrong dimensions, non-finite or zero rows,
  duplicate keys and a wrong file digest before anything is written;
* a chunk without a row fails the build unless ``on_missing: embed`` is chosen, and the manifest
  counts matched, provider-embedded and unused rows;
* ``selection.source_order`` is a stable sort that never changes membership and runs before pins;
* the Faiss engine answers from a cached index and grows its search depth when a scope filter
  leaves too few eligible chunks.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document, Scope
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.ingestion.precomputed import text_sha256
from su_multi_arm.ingestion.write import IngestError, WritePath
from su_multi_arm.providers import Embedder
from su_multi_arm.selection import Candidate, SelectionPolicy, select
from su_multi_arm.selection.pins import order_by_source

DIM = 16


def base(precomputed: dict | None = None, engine: str = "numpy") -> dict:
    embedder = {"provider": "hash", "dimensions": DIM, "batch_size": 3}
    if precomputed is not None:
        embedder["precomputed"] = precomputed
    return {
        "schema_version": 2,
        "dataset": "demo",
        "backends": {"local": {"kind": "sqlite", "path": "state", "vector_engine": engine}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": {"fields": [{"name": "source", "type": "string", "filterable": True}]},
        "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}}},
        "ingestion": {"chunking": {"size": 400, "overlap": 20}},
        "models": {"roles": {"embedder": embedder}},
    }


def corpus(n: int = 5) -> list[Document]:
    return [
        Document(id=f"d{i}", dataset="demo", title="", fields={"body": f"Synthetic note {i} about widget calibration step {i}."}, metadata={"source": "kb" if i % 2 else "case"})
        for i in range(n)
    ]


class Counting:
    def __init__(self, cfg):
        self.inner = Embedder(cfg.models.roles.embedder)
        self.provider = self.inner.provider
        self.texts: list[str] = []

    def encode(self, texts, query=False):
        self.texts.extend(texts)
        return self.inner.encode(texts, query)


def write_vectors(tmp_path, docs, *, drop: int | None = None, rows: np.ndarray | None = None, keys: list[dict] | None = None):
    chunks = [c for d in docs for c in chunk_document(d, 400, 20, [])]
    rng = np.random.default_rng(7)
    matrix = rng.normal(size=(len(chunks), DIM)).astype(np.float32) if rows is None else rows
    entries = keys if keys is not None else [{"document_id": c.document_id, "field": c.field, "text_sha256": text_sha256(c.text)} for c in chunks]
    if drop is not None:
        matrix = np.delete(matrix, drop, axis=0)
        entries = [e for i, e in enumerate(entries) if i != drop]
    # reversed on disk: alignment must come from the keys, not from the row position
    order = list(range(len(entries)))[::-1]
    np.save(tmp_path / "vec.npy", matrix[order])
    (tmp_path / "keys.jsonl").write_text("".join(json.dumps(entries[i]) + "\n" for i in order))
    return chunks, matrix


def build(tmp_path, precomputed: dict | None, engine: str = "numpy"):
    data = base(precomputed, engine)
    data["backends"]["local"]["path"] = str(tmp_path / "state")
    cfg = validate_config(data)
    backend = SqliteBackend(tmp_path / "state", vector_engine=engine)
    embedder = Counting(cfg)
    path = WritePath(cfg, {"local": backend}, extractor=BuiltinExtractor(cfg.extraction, Dependencies(providers={})), embedder=embedder)
    return path, backend, embedder


def spec(tmp_path, **extra):
    """Pins both files' current digests unless a test overrides them."""
    from su_multi_arm.ingestion.precomputed import file_sha256

    pins = {"sha256": file_sha256(tmp_path / "vec.npy"), "keys_sha256": file_sha256(tmp_path / "keys.jsonl")}
    return {"vectors": str(tmp_path / "vec.npy"), "keys": str(tmp_path / "keys.jsonl"), **pins, **extra}


def test_imported_rows_are_matched_by_key_and_nothing_is_embedded(tmp_path):
    docs = corpus()
    chunks, matrix = write_vectors(tmp_path, docs)
    path, backend, embedder = build(tmp_path, spec(tmp_path))
    manifest = path.ingest(docs)
    assert embedder.texts == []  # the provider is not called for imported rows
    assert manifest["imported_embeddings"] == len(chunks) and manifest["embedded"] == 0
    audit = manifest["precomputed_vectors"]
    assert audit["matched"] == len(chunks) and audit["unused_rows"] == 0 and audit["embedded_by_provider"] == 0
    gen, _ = path.current()
    for chunk, row in zip(chunks, matrix):
        hit = backend.vector.search(gen, row, Scope(document_ids=[chunk.document_id]), 1)[0]
        assert hit.chunk_id == chunk.id and hit.score == pytest.approx(1.0, abs=1e-5)


def test_missing_row_fails_unless_embedding_is_explicitly_allowed(tmp_path):
    docs = corpus()
    chunks, _ = write_vectors(tmp_path, docs, drop=2)
    path, _, _ = build(tmp_path, spec(tmp_path))
    with pytest.raises(IngestError, match="no precomputed vector"):
        path.ingest(docs)
    path, _, embedder = build(tmp_path / "b", spec(tmp_path, on_missing="embed"))
    manifest = path.ingest(docs)
    assert len(embedder.texts) == 1 and manifest["precomputed_vectors"]["embedded_by_provider"] == 1
    assert manifest["precomputed_vectors"]["matched"] == len(chunks) - 1


@pytest.mark.parametrize(
    "problem, match",
    [
        ("dimension", "dimensions"),
        ("count", "keys for"),
        ("duplicate", "duplicate precomputed key"),
        ("nan", "non-finite"),
        ("zero", "zero vector"),
        ("digest", "precomputed vectors sha256"),
        ("keys_digest", "precomputed keys sha256"),
    ],
)
def test_audit_refuses_inconsistent_files_before_writing(tmp_path, problem, match):
    docs = corpus()
    chunks = [c for d in docs for c in chunk_document(d, 400, 20, [])]
    keys = [{"document_id": c.document_id, "field": c.field, "text_sha256": text_sha256(c.text)} for c in chunks]
    rows = np.random.default_rng(1).normal(size=(len(chunks), DIM)).astype(np.float32)
    extra = {}
    if problem == "dimension":
        rows = rows[:, : DIM - 1]
    elif problem == "count":
        keys = keys[:-1]
        rows = rows[:-1]
        keys = keys + [keys[0] | {"field": "other"}, keys[0] | {"field": "third"}]
    elif problem == "duplicate":
        keys[1] = keys[0]
    elif problem == "nan":
        rows[0, 0] = np.nan
    elif problem == "zero":
        rows[0] = 0
    elif problem == "digest":
        extra = {"sha256": "0" * 64}
    elif problem == "keys_digest":
        extra = {"keys_sha256": "0" * 64}
    np.save(tmp_path / "vec.npy", rows)
    (tmp_path / "keys.jsonl").write_text("".join(json.dumps(k) + "\n" for k in keys))
    path, _, embedder = build(tmp_path, spec(tmp_path, **extra))
    with pytest.raises(IngestError, match=match):
        path.ingest(docs)
    assert embedder.texts == []
    with pytest.raises(Exception):
        path.current()  # nothing was activated


def test_content_digests_not_paths_are_the_import_identity(tmp_path):
    import shutil

    docs = corpus()
    write_vectors(tmp_path, docs)
    first, _, _ = build(tmp_path / "a", spec(tmp_path))
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    shutil.copy(tmp_path / "vec.npy", moved / "vec.npy")
    shutil.copy(tmp_path / "keys.jsonl", moved / "keys.jsonl")
    relocated, _, _ = build(tmp_path / "a", spec(moved))
    assert relocated.index_fingerprint() == first.index_fingerprint()  # moving the files keeps identity
    assert relocated.embedding_recipe() == first.embedding_recipe()
    for change in ({"sha256": "1" * 64}, {"keys_sha256": "2" * 64}):
        other, _, _ = build(tmp_path / "a", spec(tmp_path, **change))
        assert other.index_fingerprint() != first.index_fingerprint()  # changed content changes it
        assert other.embedding_recipe() != first.embedding_recipe()
    assert str(tmp_path) not in json.dumps(first.embedding_recipe())


def test_manifest_records_both_digests_and_the_imported_recipe(tmp_path):
    docs = corpus()
    write_vectors(tmp_path, docs)
    path, _, _ = build(tmp_path, spec(tmp_path))
    manifest = path.ingest(docs)
    audit = manifest["precomputed_vectors"]
    pins = spec(tmp_path)
    assert audit["sha256"] == pins["sha256"] and audit["keys_sha256"] == pins["keys_sha256"]
    assert audit["pairing"].startswith("positional") and audit["input"].startswith("imported")
    assert manifest["embedding_recipe"]["input"].startswith("imported")
    assert "vectors" not in manifest["embedding"]["precomputed"] and str(tmp_path) not in json.dumps(manifest["embedding"])


def provider_rows(cfg, chunks):
    return Embedder(cfg.models.roles.embedder).encode([c.text for c in chunks])


def test_permuted_keys_file_passes_the_file_audit_but_fails_the_spot_check(tmp_path):
    """A keys file reordered against its vectors: every structural check passes (the pairing is
    positional), so only the re-embedding spot check can detect it."""
    docs = corpus(8)
    chunks = [c for d in docs for c in chunk_document(d, 400, 20, [])]
    cfg = validate_config(base())
    rows = provider_rows(cfg, chunks)
    keys = [{"document_id": c.document_id, "field": c.field, "text_sha256": text_sha256(c.text)} for c in chunks]
    np.save(tmp_path / "vec.npy", rows)
    permuted = keys[1:] + keys[:1]  # every row now paired with its neighbour's key
    (tmp_path / "keys.jsonl").write_text("".join(json.dumps(k) + "\n" for k in permuted))
    unchecked, _, _ = build(tmp_path / "u", spec(tmp_path))
    assert unchecked.ingest(docs)["precomputed_vectors"]["matched"] == len(chunks)  # undetectable without verify
    checked, _, _ = build(tmp_path / "c", spec(tmp_path, verify={"sample": 5, "min_cosine": 0.99}))
    with pytest.raises(IngestError, match="spot check"):
        checked.ingest(docs)
    (tmp_path / "keys.jsonl").write_text("".join(json.dumps(k) + "\n" for k in keys))  # correct pairing
    good, _, _ = build(tmp_path / "g", spec(tmp_path, verify={"sample": 5, "min_cosine": 0.99}))
    report = good.ingest(docs)["precomputed_vectors"]["verify"]
    assert report["checked"] == 5 and report["below"] == 0 and report["min"] >= 0.99


def cand(record_id, arm, score, source, pinned=False):
    return Candidate(record_id=record_id, arm=arm, score=score, source=source, pinned=pinned)


def test_source_order_is_stable_keeps_membership_and_pins_still_lead():
    pools = {
        "dense": [cand("j1", "dense", 0.9, "ticket"), cand("c1", "dense", 0.8, "case"), cand("k1", "dense", 0.7, "kb")],
        "lexical": [cand("k2", "lexical", 9.0, "kb"), cand("x1", "lexical", 8.0, "other"), cand("c2", "lexical", 7.0, "case")],
        "exact_id": [cand("j9", "exact_id", 1.0, "ticket", pinned=True)],
    }
    base_order, _, _ = select(pools, SelectionPolicy(ordering="stable_interleave"))
    ordered, _, trace = select(pools, SelectionPolicy(ordering="stable_interleave", source_order=["kb", "case", "ticket"]))
    assert {r.record_id for r in ordered} == {r.record_id for r in base_order}
    assert ordered[0].record_id == "j9"  # the exact-id pin leads despite its source rank
    rest = [r.record_id for r in ordered[1:]]
    kb = [r for r in rest if r.startswith("k")]
    assert rest[: len(kb)] == [r.record_id for r in base_order if r.record_id.startswith("k")]  # stable within a source
    assert rest[-1] == "x1"  # unlisted source after every listed one
    assert trace.source_order == ["kb", "case", "ticket"]
    assert order_by_source([], ["kb"]) == []


def test_faiss_search_is_cached_and_grows_depth_under_a_scope_filter(tmp_path):
    pytest.importorskip("faiss")
    docs = [Document(id=f"d{i}", dataset="demo", title="", fields={"body": f"row {i}"}, metadata={"source": "kb"}) for i in range(1500)]
    path, backend, _ = build(tmp_path, None, engine="faiss")
    path.ingest(docs)
    gen, _ = path.current()
    query = Embedder(path.cfg.models.roles.embedder).encode(["row 3"], query=True)[0]
    every = backend.vector.search(gen, query, None, 5)
    assert len(every) == 5 and len(backend.vector._faiss) == 1
    far = backend.vector.search(gen, query, Scope(document_ids=[every[-1].document_id, "d1499"]), 2)
    assert {h.document_id for h in far} == {every[-1].document_id, "d1499"}
    scores = [h.score for h in backend.vector.search(gen, query, None, 50)]
    assert scores == sorted(scores, reverse=True)


def test_source_slots_reserve_representation_then_fill_by_score():
    from su_multi_arm.selection import limit

    pool = {"dense": [cand(f"t{i}", "dense", 1.0 - i / 100, "ticket") for i in range(8)] + [cand("k1", "dense", 0.5, "kb"), cand("k2", "dense", 0.4, "kb")]}
    plain = limit(pool, 4)["dense"]
    assert [c.record_id for c in plain] == ["t0", "t1", "t2", "t3"]
    slotted = limit(pool, 4, ["kb", "ticket", "doc", "kb"])["dense"]
    assert [c.record_id for c in slotted] == ["k1", "t0", "k2", "t1"]  # missing 'doc' slot skipped, filled by score
    assert len({c.record_id for c in slotted}) == 4
    ordered, _, _ = select(pool, SelectionPolicy(per_arm=4, ordering="stable_interleave", source_slots=["kb"]))
    assert [r.record_id for r in ordered] == ["k1", "t0", "t1", "t2"]


def test_aggregate_evidence_is_packed_before_documents_on_a_full_budget():
    from su_multi_arm.core.config_selection import PackingConfig
    from su_multi_arm.selection import PackExtra, PackRecord, pack

    records = [PackRecord(id=f"d{i}", fields={"body": "x" * 5000}) for i in range(10)]
    extra = PackExtra(id="aggregate:1", source="structured", passage='{"value": 1231}')
    packed = pack(records, PackingConfig(budget_chars=8000), [extra])
    assert packed.evidence[-1].id == "aggregate:1" and "aggregate:1" not in packed.omitted_ids
    assert packed.passage_characters <= 8000
    tiny = pack(records, PackingConfig(budget_chars=500, min_per_record=0), [PackExtra(id="aggregate:2", source="structured", passage="y" * 600)])
    assert "aggregate:2" in tiny.omitted_ids  # an aggregate larger than the whole budget is reported, never truncated


def test_count_distinct_named_by_field_counts_distinct_values_not_records():
    from su_multi_arm.core.types import Measure

    assert Measure(kind="count_distinct", field="organization").unit == "organization"
    assert Measure(kind="count_distinct", unit="organization").unit == "organization"
    with pytest.raises(ValueError):
        Measure(kind="count_distinct", field="organization", unit="product")
    from su_multi_arm.backends._scope import measure_of
    from su_multi_arm.core.types import MetadataSchema, Provenance, ScopeSpec, StructuredRecord

    rows = [StructuredRecord(schema_id="document", id=f"r{i}", values={"organization": "A" if i % 2 else "B"}, provenance=Provenance(extractor="catalog")) for i in range(6)]
    value, _ = measure_of(ScopeSpec(measure=Measure(kind="count_distinct", field="organization")), rows, 6, MetadataSchema())
    assert value == 2


def test_fts_index_replaces_rows_and_ignores_duplicate_ids_in_one_call(tmp_path):
    docs = corpus(4)
    path, backend, _ = build(tmp_path, None)
    path.ingest(docs)
    gen, _ = path.current()
    chunks = [c for d in docs for c in chunk_document(d, 400, 20, [])]
    before = backend.lexical.count(gen)
    backend.lexical.index(gen, chunks + chunks[:2], [])  # re-index everything, two ids repeated
    assert backend.lexical.count(gen) == before == len(chunks)


def test_vector_upsert_replace_path_keeps_ids_and_rows_aligned(tmp_path):
    from su_multi_arm.core.types import GenerationId

    backend = SqliteBackend(tmp_path / "state")
    gen = GenerationId(dataset="demo", generation="0" * 32, index_format=3, fingerprint="f" * 64)
    eye = np.eye(DIM, dtype=np.float32)
    backend.vector.upsert(gen, ["a", "b", "c"], eye[:3])
    backend.vector.upsert(gen, ["d"], eye[3:4])
    backend.vector.upsert(gen, ["b"], eye[5:6])  # replace a pending id
    backend.vector.refresh(gen)
    backend.vector.upsert(gen, ["a"], eye[6:7])  # replace a stored id
    backend.vector.refresh(gen)
    ids, matrix = backend.vector._load(gen, "default")
    rows = {cid: int(np.argmax(matrix[i])) for i, cid in enumerate(ids)}
    assert rows == {"a": 6, "b": 5, "c": 2, "d": 3} and len(ids) == 4


def test_source_slots_never_displace_a_pinned_exact_hit_and_are_traced():
    from su_multi_arm.selection import limit

    pool = {"exact_id": [cand("t1", "exact_id", 1.0, "ticket", pinned=True)] + [cand(f"k{i}", "exact_id", 0.5 - i / 100, "kb") for i in range(5)]}
    kept = limit(pool, 3, ["kb", "kb", "kb", "kb"])["exact_id"]
    assert kept[0].record_id == "t1" and [c.record_id for c in kept] == ["t1", "k0", "k1"]
    _, _, trace = select(pool, SelectionPolicy(per_arm=3, source_slots=["kb"]))
    assert trace.source_slots == ["kb"]


def test_v1_downgrade_refuses_source_order_and_slots():
    from su_multi_arm.compat.config_v1 import downgrade
    from su_multi_arm.core.errors import ConfigError

    for key, value in (("source_order", ["kb"]), ("source_slots", ["kb"])):
        data = base()
        data["backends"] = {"local": {"kind": "sqlite", "path": "state"}}
        data["pipeline"]["selection"] = {key: value}
        with pytest.raises(ConfigError, match="source order or source slots"):
            downgrade(validate_config(data))
