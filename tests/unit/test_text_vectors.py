"""Unified merge M2a, §6.7/§6.10: the text-hash vector store (unique-text dedupe, float16, one
embedding space, crash-safe appends) and the retention invariant (log-line text never embedded)."""

from __future__ import annotations

import numpy as np
import pytest

from su_multi_arm.core.types import Chunk
from su_multi_arm.core.types_members import EmbeddingSpaceRef
from su_multi_arm.ingestion import text_vectors
from su_multi_arm.ingestion.text_vectors import (
    RetentionViolation,
    TextVectorsRefused,
    TextVectorStore,
    embed_missing,
    embed_rows,
    pick_device,
    text_hash,
)

SPACE = EmbeddingSpaceRef(id="space-a", model="hash-demo-v1", dimensions=8)


class CountingEncoder:
    def __init__(self, dims=8):
        self.dims, self.calls, self.texts = dims, 0, 0

    def encode(self, texts):
        self.calls += 1
        self.texts += len(texts)
        out = np.zeros((len(texts), self.dims), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, len(t) % self.dims] = 1.0
            out[i, (len(t) + 3) % self.dims] = 0.5
        return out


def test_unique_texts_are_embedded_and_stored_once(tmp_path):
    store = TextVectorStore(tmp_path / "v", SPACE)
    texts = {text_hash(t): t for t in ["alpha", "beta", "gamma"]}
    encoder = CountingEncoder()
    report = embed_missing(store, texts, encoder, batch_size=2)
    assert report.texts == 3 and report.stored == 3 and encoder.calls == 2
    again = embed_missing(store, {**texts, text_hash("delta"): "delta"}, encoder, batch_size=2)
    assert again.texts == 1 and store.count() == 4
    vectors = store.get([text_hash("alpha"), text_hash("alpha"), text_hash("delta")])
    assert vectors.shape == (3, 8) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)
    assert np.array_equal(vectors[0], vectors[1])
    assert (tmp_path / "v" / "shards" / "000000.f16").stat().st_size == 4 * 8 * 2  # float16, 2 bytes per value


def test_another_embedding_space_is_refused(tmp_path):
    TextVectorStore(tmp_path / "v", SPACE)
    with pytest.raises(TextVectorsRefused, match="space"):
        TextVectorStore(tmp_path / "v", SPACE.model_copy(update={"epoch": "e2"}))
    with pytest.raises(TextVectorsRefused, match="space"):
        TextVectorStore(tmp_path / "v", EmbeddingSpaceRef(id="space-a", model="other", dimensions=8))


def test_shape_unknown_hash_and_hash_mismatch_are_refused(tmp_path):
    store = TextVectorStore(tmp_path / "v", SPACE)
    with pytest.raises(TextVectorsRefused, match="shape"):
        store.put(["a" * 32], np.zeros((1, 4)))
    with pytest.raises(TextVectorsRefused, match="finite"):
        store.put(["a" * 32], np.full((1, 8), np.nan))
    with pytest.raises(TextVectorsRefused, match="no vector"):
        store.get([text_hash("never")])
    with pytest.raises(TextVectorsRefused, match="does not hash"):
        embed_missing(store, {text_hash("x"): "y"}, CountingEncoder())


def test_a_crash_between_append_and_index_leaves_no_bad_row(tmp_path):
    store = TextVectorStore(tmp_path / "v", SPACE)
    store.put([text_hash("a")], CountingEncoder().encode(["a"]))
    shard = tmp_path / "v" / "shards" / "000000.f16"
    with open(shard, "ab") as handle:  # bytes appended, index row never written
        handle.write(b"\x00" * 16 * 3)
    reopened = TextVectorStore(tmp_path / "v", SPACE)
    reopened.put([text_hash("b")], CountingEncoder().encode(["bb"]))
    assert shard.stat().st_size == 2 * 16
    assert np.allclose(reopened.get([text_hash("b")]), CountingEncoder().encode(["bb"]) / np.linalg.norm(CountingEncoder().encode(["bb"])), atol=1e-3)


def test_shards_roll_over(tmp_path, monkeypatch):
    monkeypatch.setattr(text_vectors, "SHARD_ROWS", 3)
    store = TextVectorStore(tmp_path / "v", SPACE)
    words = [f"w{i}" for i in range(7)]
    store.put([text_hash(w) for w in words], CountingEncoder().encode(words))
    assert sorted(p.name for p in (tmp_path / "v" / "shards").iterdir()) == ["000000.f16", "000001.f16", "000002.f16"]
    assert store.get([text_hash(w) for w in words]).shape == (7, 8)


def test_only_declared_fields_are_embedded_and_line_text_never():
    chunks = [Chunk(id="c1", document_id="i1", field="summary", start=0, end=3, text="abc"),
              Chunk(id="c2", document_id="i1", field="exemplars", start=0, end=3, text="xyz"),
              Chunk(id="c3", document_id="s1", field="exemplars", start=0, end=3, text="raw"),
              Chunk(id="c4", document_id="u1", field="exemplars", start=0, end=3, text="abc")]
    kinds = {"i1": "incident", "s1": "signature", "u1": "unclassified_example"}
    rows, texts = embed_rows(chunks, kinds, {"incident": ["summary"], "unclassified_example": ["exemplars"]})
    assert [r.chunk_id for r in rows] == ["c1", "c4"] and texts == {text_hash("abc"): "abc"}
    with pytest.raises(RetentionViolation, match="log-line text"):
        embed_rows(chunks, kinds, {"signature": ["exemplars"]})


def test_pick_device_respects_the_switch():
    assert pick_device(allow_gpu=False) == "cpu"
    assert pick_device(min_free_bytes=1 << 62) == "cpu"  # never claims a GPU that lacks the free memory
