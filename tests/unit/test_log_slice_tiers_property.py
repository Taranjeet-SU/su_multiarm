"""Unified merge M2a property test (§10.4): random refresh / compaction / close interleavings over a
live window of several days, including gap-filling merges (full-day rebuilds that drop an id), ids
continuing across midnight and continuing out of an already archived day.

After every step, for the reader's pinned view (archive members of the closed days + the slice
vector of the others):

* every day is in exactly one tier (``PinnedView`` refuses otherwise; ``vector_for`` drops days the
  pinned generation already archives before ``release_day`` runs);
* the served rows (``ServedRows``: highest version per id, tombstones hide older rows) equal the
  ground-truth model, id by id and version by version — so no vanished id is ever served and
  nothing is counted twice;
* the query-time statistics count exactly the served rows (``N`` and per-document ``df``);
* the pinned units of a day stay within ``max_units_per_day``.

At the end the N-slice ranking equals the ranking after compacting each live day into one base.
Deterministic seeds (no hypothesis dependency)."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from su_multi_arm.backends.sqlite.query_stats import LogUnits, QueryTimeScorer
from su_multi_arm.core.types_members import PinnedView
from su_multi_arm.ingestion.log_members import close_day
from su_multi_arm.ingestion.log_slices import SliceRefused
from tests.log_slices_support import doc, make_ledger

START = date(2026, 11, 6)


class Model:
    """Ground truth: what each day's producer holds, and the latest emission per id."""

    def __init__(self):
        self.truth: dict[date, dict[str, str]] = {}
        self.latest: dict[str, tuple[int, date]] = {}  # id -> (version, day)
        self.vanished: set[str] = set()

    def served(self) -> dict[str, int]:
        return {i: v for i, (v, _d) in self.latest.items()}


def token(doc_id: str) -> str:
    return "u" + doc_id.rsplit(":", 1)[1].replace("-", "x")


def text(doc_id: str, rev: int) -> str:
    return f"{token(doc_id)} event revision r{rev} crawler"


def check(ledger, model, scorer, members, archived_days):
    view = PinnedView(generation="g", members=tuple(members), slices={"logs": ledger.vector_for(lambda d: d in archived_days)})
    readers = scorer.units.readers(view)
    masks = scorer.served.masks(readers)
    served: dict[str, int] = {}
    for reader in readers:
        doc_mask = masks[reader.unit_id][1]
        for doc_id, version, ok in zip(reader.ids, reader.versions, doc_mask):
            if ok:
                assert doc_id not in served, f"{doc_id} served twice"
                served[doc_id] = version
    assert served == model.served()
    assert not (model.vanished - set(model.latest)) & set(served)
    for day, ids in view.slices.get("logs", {}).items():
        assert len(ids) <= ledger.settings.max_units_per_day
    if served:
        sample = sorted(served)[:: max(1, len(served) // 5)]
        stats = scorer.statistics(readers, masks, [token(i) for i in sample])
        assert stats["N"] == 2 * len(served)  # title + summary chunk per document
        assert all(stats["df"][token(i)] == 1 for i in sample)
    return view


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_random_interleavings_keep_one_tier_and_the_ground_truth(tmp_path, seed):
    rng = random.Random(seed)
    ledger = make_ledger(tmp_path / "ledger")
    registry = tmp_path / "registry"
    scorer = QueryTimeScorer("logs", LogUnits(ledger, registry))
    model = Model()
    members, archived_days = [], set()
    serial = 0
    now = datetime(START.year, START.month, START.day, tzinfo=timezone.utc)
    end = now + timedelta(days=3, hours=8)
    step = timedelta(minutes=20)

    def refresh(day: date, stamp: datetime):
        nonlocal serial
        truth = model.truth.setdefault(day, {})
        upserts: dict[str, str] = {}
        for _ in range(rng.randrange(0, 4)):
            serial += 1
            upserts[f"logs:incident:{day.isoformat()}-{serial}"] = ""
        for doc_id in rng.sample(sorted(truth), min(len(truth), rng.randrange(0, 3))):
            upserts[doc_id] = ""
        if rng.random() < 0.08:  # an id continues across midnight (also out of an archived day)
            earlier = [i for i, (_v, d) in model.latest.items() if d < day]
            if earlier:
                upserts[rng.choice(sorted(earlier))] = ""
        texts = {i: text(i, rng.randrange(10_000)) for i in upserts}
        dropped: list[str] = []
        merged = truth and rng.random() < 0.15
        if merged:  # a gap fill merges a group away: the day is rebuilt in full
            dropped = [rng.choice(sorted(truth))]
        changed = {i for i, t in texts.items() if truth.get(i) != t}
        truth.update(texts)
        for gone in dropped:
            truth.pop(gone, None)
            changed.discard(gone)
        documents = [doc(i, t) for i, t in (truth.items() if merged else ((i, texts[i]) for i in texts if i not in dropped))]
        ref = ledger.seal(day, "five_minute", documents, full_day=bool(merged), as_of=stamp)
        tombstoned = {g for g in dropped if g in model.latest and model.latest[g][1] == day}
        if ref is None:
            assert not changed and not tombstoned
            return
        assert set(ref.tombstones) == tombstoned
        assert {d.id for d in documents if d.id in changed} == changed
        for doc_id in changed:
            model.latest[doc_id] = (ref.sequence, day)
        for gone in tombstoned:
            model.latest.pop(gone)
            model.vanished.add(gone)

    while now < end:
        now += step
        today = now.date()
        if today not in archived_days:
            refresh(today, now)
        yesterday = today - timedelta(days=1)
        if yesterday >= START and yesterday not in archived_days and rng.random() < 0.3:
            refresh(yesterday, datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, tzinfo=timezone.utc))
        ledger.run_due(now)
        # close the previous day after 06:00: archive member first, then (generation active) release
        if yesterday >= START and yesterday not in archived_days and now.hour >= 6 and rng.random() < 0.5:
            closed = close_day(ledger, yesterday, registry, as_of=now)
            members.append(closed.day)
            archived_days.add(yesterday)
            check(ledger, model, scorer, members, archived_days)  # new generation, slices not released yet
            ledger.release_day(yesterday)
            with pytest.raises(SliceRefused, match="closed"):
                ledger.seal(yesterday, "five_minute", [doc("logs:incident:late-0", "late")])
        check(ledger, model, scorer, members, archived_days)

    assert model.vanished, "the run exercised gap-filling merges"
    assert archived_days, "the run exercised close"
    view = check(ledger, model, scorer, members, archived_days)
    queries = ["crawler event", "revision r1", "event r42 crawler", " ".join(token(i) for i in sorted(model.latest)[:3])]
    before = {q: scorer.rank_rows(q, 15, view)[0] for q in queries}
    for day in sorted(view.slices["logs"]):
        ledger.compact(day, "base")
    after_view = check(ledger, model, scorer, members, archived_days)
    for q in queries:
        after = scorer.rank_rows(q, 15, after_view)[0]
        assert [r[0] for r in before[q]] == [r[0] for r in after]
        assert np.allclose([r[2] for r in before[q]], [r[2] for r in after], rtol=0, atol=1e-9)
