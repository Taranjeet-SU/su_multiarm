"""Embedding reuse on the write path (risk R2 of the 2.0.0a1 completeness review; plan §3.14
"embedding reuse keyed by the exact effective input").

A vector is reused when the exact embedded input (title + field + chunk text) and the embedding
recipe (provider, model, revision, dimension, endpoint, document prefix, normalisation, input
template) were embedded before; everything else is embedded. Probed by counting the texts the
embedder receives; the manifest reports ``embedded`` / ``reused_embeddings``.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from su_multi_arm.backends.memory import MemoryBackend
from su_multi_arm.backends.sqlite import SqliteBackend
from su_multi_arm.core.component import Dependencies
from su_multi_arm.core.config import validate_config
from su_multi_arm.core.types import Document, Scope
from su_multi_arm.extraction.base import BuiltinExtractor
from su_multi_arm.extraction.chunking import chunk_document
from su_multi_arm.ingestion.write import IngestError, WritePath
from su_multi_arm.providers import Embedder

BASE = {
    "schema_version": 2,
    "dataset": "demo",
    "backends": {"local": {"kind": "memory"}},
    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
    "metadata_schema": {"fields": [{"name": "source", "type": "string"}]},
    "pipeline": {"arms": {"dense": {"backend": "local"}, "lexical": {"backend": "local"}, "structured": {"backend": "local"}}},
    "ingestion": {"chunking": {"size": 120, "overlap": 20}},
    "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 32, "batch_size": 4}}},
}


def config(embedder: dict | None = None, extraction_policy: str | None = None, ingest: dict | None = None):
    data = json.loads(json.dumps(BASE))
    if embedder:
        data["models"]["roles"]["embedder"].update(embedder)
    if extraction_policy:
        data["extraction"] = {"policy": extraction_policy}
    if ingest is not None:
        data["budgets"] = {"ingest": ingest}
    return validate_config(data)


def corpus(changed: str | None = None, title: str | None = None) -> list[Document]:
    docs = []
    for i in range(6):
        body = " ".join(f"Record {i} sentence {j} about rounding and exemption certificates." for j in range(5))
        if f"d{i}" == changed:
            body = body.replace("rounding", "currency conversion")
        docs.append(Document(id=f"d{i}", dataset="demo", title=title if title and i == 2 else f"Title {i}", fields={"body": body}, metadata={"source": "kb"}))
    return docs


class CountingEmbedder:
    def __init__(self, cfg):
        self.inner = Embedder(cfg.models.roles.embedder)
        self.provider = self.inner.provider
        self.calls: list[int] = []
        self.texts: list[str] = []

    def encode(self, texts, query=False):
        self.calls.append(len(texts))
        self.texts.extend(texts)
        return self.inner.encode(texts, query)

    def reset(self):
        self.calls, self.texts = [], []


def make(cfg, backend):
    embedder = CountingEmbedder(cfg)
    extractor = BuiltinExtractor(cfg.extraction, Dependencies(providers={}))
    return WritePath(cfg, {"local": backend}, extractor=extractor, embedder=embedder), embedder


def chunks_of(cfg, document):
    chunking = cfg.ingestion.chunking
    return chunk_document(document, chunking.size, chunking.overlap, chunking.field_order)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    return MemoryBackend() if request.param == "memory" else SqliteBackend(tmp_path / "state")


def test_one_document_change_reembeds_only_that_documents_chunks(backend):
    cfg = config()
    path, embedder = make(cfg, backend)
    first = path.ingest(corpus())
    total = first["chunks"]
    assert first["embedded"] == total and first["reused_embeddings"] == 0 and sum(embedder.calls) == total
    assert len(embedder.calls) == -(-total // 4)  # one provider request per batch_size window
    embedder.reset()
    changed = corpus(changed="d3")[3]
    second = path.ingest([changed])  # upsert without --replace
    expected = len(chunks_of(cfg, changed))
    changed_texts = {f"{changed.title}\n{c.field}: {c.text}" for c in chunks_of(cfg, changed)}
    old = {f"{corpus()[3].title}\n{c.field}: {c.text}" for c in chunks_of(cfg, corpus()[3])}
    new_inputs = changed_texts - old
    assert sum(embedder.calls) == len(new_inputs) <= expected and set(embedder.texts) == new_inputs
    assert second["embedded"] == len(new_inputs) and second["reused_embeddings"] == second["chunks"] - len(new_inputs)
    assert second["projections"]["vector"]["verified"] and second["incremental"]["rebuilt"]
    # reused vectors are the ones the first build stored: byte-identical to a fresh encode
    gen, _ = path.current()
    probe = chunks_of(cfg, corpus()[0])[0]
    fresh = Embedder(cfg.models.roles.embedder).encode([f"Title 0\n{probe.field}: {probe.text}"])[0]
    hits = backend.vector.search(gen, fresh, Scope(document_ids=["d0"]), 1)
    assert hits and hits[0].chunk_id == probe.id and hits[0].score == pytest.approx(1.0, abs=1e-6)
    embedder.reset()
    path.ingest([], delete_ids=["d5"])  # a delete embeds nothing
    assert embedder.calls == []


def test_title_change_reembeds_that_documents_chunks_when_the_title_is_embedded(backend):
    cfg = config()
    path, embedder = make(cfg, backend)
    path.ingest(corpus())
    embedder.reset()
    renamed = corpus(title="A brand new title")[2]
    manifest = path.ingest([renamed])
    expected = len(chunks_of(cfg, renamed))
    assert sum(embedder.calls) == expected and manifest["embedded"] == expected
    assert all(t.startswith("A brand new title\n") for t in embedder.texts)
    assert manifest["reused_embeddings"] == manifest["chunks"] - expected


def test_recipe_change_reembeds_everything(backend):
    cfg = config()
    path, embedder = make(cfg, backend)
    first = path.ingest(corpus())
    for change in ({"document_prefix": "passage: "}, {"dimensions": 48}, {"model": "hash-demo-v2"}, {"revision": "r2"}):
        changed_cfg = config(embedder=change)
        rebuilt, counting = make(changed_cfg, backend)
        manifest = rebuilt.ingest(corpus(), replace=True, rebuild=True)
        assert manifest["embedded"] == first["chunks"] and manifest["reused_embeddings"] == 0, change
        assert sum(counting.calls) == first["chunks"]
        again, counting = make(changed_cfg, backend)
        repeat = again.ingest(corpus(), replace=True)
        assert repeat["embedded"] == 0 and counting.calls == [], change  # same recipe: all reused


def test_embedding_calls_are_budgeted_on_the_write_path():
    cfg = config(extraction_policy="partial", ingest={"max_embedding_calls": 2})
    path, embedder = make(cfg, MemoryBackend())
    manifest = path.ingest(corpus(), allow_partial=True)
    stage = manifest["coverage"]["stages"]["embeddings"]
    assert stage["status"] == "incomplete" and "embedding_calls" in stage["reason"]
    assert len(embedder.calls) == 2 and manifest["budget"]["embedding_calls"] == 2
    assert manifest["projections"]["vector"]["rows"] == 8 and manifest["projections"]["vector"]["incomplete"] is True
    strict = config(ingest={"max_embedding_calls": 2})
    path, _ = make(strict, MemoryBackend())
    with pytest.raises(IngestError) as info:
        path.ingest(corpus())
    assert info.value.failure["category"] == "budget" and info.value.failure["stage"] == "embeddings"


def test_a_corrupt_stored_vector_fails_the_build(backend):
    cfg = config()
    path, _ = make(cfg, backend)
    path.ingest(corpus())
    keys = [k for k in _ledger_keys(backend) if k.startswith("embedding:")]
    assert keys
    backend.ledger.put_raw(keys[0], "AAAAAA==", True)  # one float32 instead of 32
    with pytest.raises(IngestError, match="stored embedding has 1 values"):
        path.ingest(corpus(), replace=True)


def _ledger_keys(backend) -> list[str]:
    if isinstance(backend, MemoryBackend):
        return list(backend._state.raw)
    with backend.runtime_db() as db:
        return [r[0] for r in db.execute("SELECT key FROM raw_replies")]


def test_vectors_are_streamed_not_held_for_the_whole_corpus(monkeypatch):
    """The write path holds one ``batch_size`` window plus the sampled vectors, never the corpus matrix."""
    cfg = config()
    path, _ = make(cfg, MemoryBackend())
    seen: list[int] = []
    original = MemoryBackend().vector.__class__.upsert

    def upsert(self, gen, ids, vectors, namespace="default"):
        seen.append(np.asarray(vectors).shape[0])
        return original(self, gen, ids, vectors, namespace)

    monkeypatch.setattr(MemoryBackend().vector.__class__, "upsert", upsert)
    manifest = path.ingest(corpus())
    assert max(seen) <= 4 and sum(seen) == manifest["chunks"]
