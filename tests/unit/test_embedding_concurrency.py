"""The local sentence-transformers provider serialises tokenizer + encode: two arms (dense and
segment) encode the same query concurrently, and the fast tokenizer is not thread-safe."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np

from su_multi_arm.providers.embeddings import SentenceTransformerEmbeddings


class Unsafe:
    """Fails like a fast tokenizer entered by two threads at once ("Already borrowed")."""

    max_seq_length = 512

    def __init__(self):
        self.busy = False
        self.overlaps = 0

    def _enter(self):
        if self.busy:
            self.overlaps += 1
        self.busy = True
        time.sleep(0.01)
        self.busy = False

    def tokenizer(self, texts, truncation=False, padding=False):
        self._enter()
        return {"input_ids": [[1, 2]] * len(texts)}

    def encode(self, texts, **_):
        self._enter()
        return np.ones((len(texts), 4), dtype="float32")


def test_concurrent_encodes_never_overlap():
    provider = SentenceTransformerEmbeddings(SimpleNamespace(model="m", revision=None, device="cpu", batch_size=8))
    provider._model = Unsafe()
    threads = [threading.Thread(target=provider.raw, args=(["q"],)) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert provider._model.overlaps == 0
