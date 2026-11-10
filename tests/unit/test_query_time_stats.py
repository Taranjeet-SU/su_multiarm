"""Unified merge M2a, §6.5 (B10): query-time statistics of the log stratum. The ranking over N
pinned slices equals the ranking over one compacted day built from the same final documents and
the monolithic reference (``rank_bm25.BM25Okapi``): exact ids and order, scores within 1e-9;
superseded and tombstoned rows are excluded; the epsilon floor is the declared approximation.

``StratumScorer`` here is LB's ``QueryTimeScorer``; LA's adopted-stratum scorer is not merged
yet, so the reference is ``rank_bm25`` over the final documents (the scorer the bm25 segment is
proven bit-identical to)."""

from __future__ import annotations

import random
from datetime import timedelta

import numpy as np
import pytest

from su_multi_arm.backends.sqlite.query_stats import AVERAGE_IDF_LAG, DenseLogScorer, LogUnits, QueryTimeScorer
from su_multi_arm.core.types_members import StratumScorer
from su_multi_arm.ingestion.log_slices import embed_pending_slices
from su_multi_arm.ingestion.text_vectors import TextVectorStore
from tests.log_slices_support import DAY, SPACE, at, doc, hash_query_encoder, make_ledger, reference_rank, served_documents, view_of

VOCAB = [f"w{i:03d}" for i in range(150)]


def random_text(rng: random.Random, n: int) -> str:
    return " ".join(VOCAB[min(int(rng.paretovariate(1.1)) - 1, len(VOCAB) - 1)] if rng.random() < 0.6 else rng.choice(VOCAB) for _ in range(n))


def build_day(ledger, seed: int, refreshes: int = 30):
    """Random refreshes with new docs, updates and gap-filling merges (full-day builds)."""
    rng = random.Random(seed)
    truth: dict[str, str] = {}
    serial = 0
    for r in range(refreshes):
        stamp = at(DAY, 0) + timedelta(minutes=5 * (r + 1))
        upserts: dict[str, str] = {}
        for _ in range(rng.randrange(1, 8)):
            serial += 1
            upserts[f"logs:incident:{serial:05d}"] = random_text(rng, rng.randrange(3, 25))
        for doc_id in rng.sample(sorted(truth), min(len(truth), rng.randrange(0, 4))):
            upserts[doc_id] = truth[doc_id] + " " + random_text(rng, 3)
        truth.update(upserts)
        if truth and rng.random() < 0.25:  # a gap fill merges one group into another: an id vanishes
            gone = rng.choice(sorted(set(truth) - set(upserts)) or sorted(truth))
            truth.pop(gone)
            upserts.pop(gone, None)
            ledger.seal(DAY, "five_minute", [doc(i, t) for i, t in truth.items()], full_day=True, as_of=stamp)
        else:
            ledger.seal(DAY, "five_minute", [doc(i, t) for i, t in upserts.items()], as_of=stamp)
        if r % 12 == 11:
            ledger.compact(DAY, "hourly", hour=(5 * (r + 1) - 1) // 60)
    return truth


def queries(rng: random.Random, n: int = 25) -> list[str]:
    return [" ".join(rng.sample(VOCAB[:60], rng.randrange(1, 4))) for _ in range(n)]


def assert_same(ranked, expected):
    assert [r[0] for r in ranked] == [e[0] for e in expected]
    assert np.allclose([r[2] for r in ranked], [e[1] for e in expected], rtol=0, atol=1e-9)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_n_slices_equal_one_compacted_day_and_the_monolithic_reference(tmp_path, seed):
    ledger = make_ledger(tmp_path / "ledger")
    truth = build_day(ledger, seed)
    view = view_of(ledger)
    assert len(view.slices["logs"][DAY]) > 3
    served = served_documents(ledger, view)
    assert {i: d.fields["summary"] for i, d in served.items()} == truth
    scorer = QueryTimeScorer("logs", LogUnits(ledger))
    assert isinstance(scorer, StratumScorer)
    rng = random.Random(seed)
    asked = queries(rng)
    before = {q: scorer.rank_rows(q, 20, view) for q in asked}
    ledger.compact(DAY, "base")
    compacted = view_of(ledger)
    assert len(compacted.slices["logs"][DAY]) == 1
    for q in asked:
        ranked, info = before[q]
        assert info["approximations"] == []
        after, _ = scorer.rank_rows(q, 20, compacted)
        assert [r[0] for r in ranked] == [r[0] for r in after]
        assert np.allclose([r[2] for r in ranked], [r[2] for r in after], rtol=0, atol=1e-9)
        assert_same(ranked, reference_rank(served.values(), q, 20, ledger))


def test_superseded_and_tombstoned_rows_leave_the_statistics(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "alpha beta"), doc("logs:incident:b", "alpha gamma")], as_of=at(DAY, 0, 5))
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "delta beta")], as_of=at(DAY, 0, 10))
    ledger.seal(DAY, "five_minute", [doc("logs:incident:a", "delta beta"), doc("logs:incident:c", "epsilon")], full_day=True, as_of=at(DAY, 0, 15))
    scorer = QueryTimeScorer("logs", LogUnits(ledger))
    ranked, info = scorer.rank_rows("alpha delta", 10, view_of(ledger))
    # served: a (delta beta) + c (epsilon); b tombstoned, a's first version superseded
    assert info["df"] == {"alpha": 0, "delta": 1}
    assert info["N"] == 4  # two documents, title + summary chunk each
    assert {r[1] for r in ranked} == {"logs:incident:a"}


def test_the_epsilon_floor_is_the_declared_approximation(tmp_path):
    """A term in more than half of the rows takes ``epsilon * average_idf``; the ledger's
    ``average_idf`` is maintained at base compaction, so before it the floor lags (declared), and
    right after it the ranking equals the monolithic reference again."""
    ledger = make_ledger(tmp_path / "ledger")
    for i in range(6):
        ledger.seal(DAY, "five_minute", [doc(f"logs:incident:{i}", f"common term{i}", title="common")], as_of=at(DAY, 0, 5 * (i + 1)))
    scorer = QueryTimeScorer("logs", LogUnits(ledger))
    ranked, info = scorer.rank_rows("common", 12, view_of(ledger))
    assert info["approximations"] == [AVERAGE_IDF_LAG] and info["df"]["common"] == info["N"]
    assert scorer.rank("common", 12, view_of(ledger)).approximations == (AVERAGE_IDF_LAG,)
    lagging = ledger.average_idf()  # from the first slice alone
    ledger.compact(DAY, "base")
    assert ledger.average_idf() != lagging
    served = served_documents(ledger, view_of(ledger)).values()
    after, info = scorer.rank_rows("common", 12, view_of(ledger))
    assert info["approximations"] == [AVERAGE_IDF_LAG]
    assert_same(after, reference_rank(served, "common", 12, ledger))
    assert ranked != after  # the lag was visible before the base


def test_a_new_slice_warms_only_itself(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    for i in range(5):
        ledger.seal(DAY, "five_minute", [doc(f"logs:incident:{i}", f"alpha w{i}")], as_of=at(DAY, 0, 5 * (i + 1)))
    units = LogUnits(ledger)
    scorer = QueryTimeScorer("logs", units)
    scorer.rank("alpha", 5, view_of(ledger))
    assert units.opened == 5
    ledger.seal(DAY, "five_minute", [doc("logs:incident:new", "alpha new")], as_of=at(DAY, 0, 30))
    scorer.rank("alpha", 5, view_of(ledger))
    assert units.opened == 6
    computed = scorer.served.computed
    scorer.rank("alpha w1", 5, view_of(ledger))
    assert scorer.served.computed == computed  # the served-row mask is cached per pinned unit set


def test_dense_ranking_is_stable_under_compaction_and_reports_pending(tmp_path):
    ledger = make_ledger(tmp_path / "ledger")
    build_day(ledger, 7, refreshes=14)
    embedder, encode_query = hash_query_encoder(SPACE.dimensions)
    store = TextVectorStore(tmp_path / "vectors", SPACE)
    dense = DenseLogScorer("logs", LogUnits(ledger), store, encode_query)
    pending = dense.rank("w001 w002", 10, view_of(ledger))
    assert pending.ids == () and all(a.startswith("dense_pending:") for a in pending.approximations)

    class Docs:
        def encode(self, texts):
            return embedder.encode(texts)

    reports = embed_pending_slices(ledger, store, Docs(), batch_size=7)
    assert len(reports) == len(ledger.vector()[DAY]) and ledger.pending_dense() == []
    before = dense.rank_rows("w001 w002", 10, view_of(ledger))[0]
    assert before
    ledger.compact(DAY, "base")
    after = dense.rank_rows("w001 w002", 10, view_of(ledger))[0]
    assert [r[0] for r in before] == [r[0] for r in after]
    assert np.allclose([r[2] for r in before], [r[2] for r in after], atol=1e-6)
    served = served_documents(ledger, view_of(ledger))
    assert {r[1] for r in after} <= set(served)
    again = embed_pending_slices(ledger, store, Docs())
    assert [r["texts"] for r in again] == [0]  # the base's texts were embedded when first sealed
