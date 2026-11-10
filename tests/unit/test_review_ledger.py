"""M2b-1 (unified merge plan §6.9, §6.13, NB15): the persistent review ledger shared by identity and
link review. The ceiling and its approval reference are recorded at first use; a configuration
edit alone never raises it; every call reserves its worst case before it is sent; unsettled
reservations stay counted. Synthetic only; no provider is called."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from su_multi_arm.core.errors import BudgetExhausted, ConfigError
from su_multi_arm.crosssource.review_ledger import ReviewLedger, ReviewMeter, RunCap, read_usage, worst_case_usd
from su_multi_arm.providers.base import CallEstimate
from su_multi_arm.runtime.budget import PriceTable

PRICES = PriceTable(version="test", prices={"m": {"input_per_million": 3.0, "output_per_million": 15.0}})
REF = "owner-approval-synthetic-1"


def ledger(tmp_path, usd=1.0, ref=REF):
    return ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=usd, approval_ref=ref)


def reserve(book, usd, subject="alias:a.b:x"):
    return book.reserve(usd, scope="identity", subject=subject, model="m", pricing_key="m")


def test_first_use_records_the_ceiling_and_its_approval(tmp_path):
    usage = ledger(tmp_path, 2.0).usage()
    assert (usage["approved_usd"], usage["approval_ref"], usage["ceiling_usd"], usage["remaining_usd"]) == (2.0, REF, 2.0, 2.0)
    with sqlite3.connect(tmp_path / "ledger.sqlite") as db:
        assert db.execute("SELECT source, approval_ref FROM ceiling").fetchall() == [("first_use", REF)]


def test_a_config_edit_never_raises_the_approved_ceiling(tmp_path):
    ledger(tmp_path, 2.0)
    raised = ledger(tmp_path, 10.0, ref="someone-else")
    usage = raised.usage()
    assert usage["ceiling_usd"] == 2.0 and usage["approval_ref"] == REF and "exceeds the approved" in usage["notes"][0]
    with pytest.raises(BudgetExhausted):
        reserve(raised, 2.5)
    assert ledger(tmp_path, 0.5).usage()["ceiling_usd"] == 0.5  # tightening applies at once


def test_only_a_new_approval_raises_it_and_never_above_ten(tmp_path):
    book = ledger(tmp_path, 10.0)
    first = ReviewLedger(tmp_path / "x.sqlite", usd_ceiling=1.0, approval_ref=REF)
    with pytest.raises(ConfigError, match="new approval reference"):
        first.approve_ceiling(5.0, REF)
    with pytest.raises(ConfigError):
        first.approve_ceiling(10.5, "owner-approval-2")
    first.approve_ceiling(5.0, "owner-approval-2")
    assert ReviewLedger(tmp_path / "x.sqlite", usd_ceiling=10.0, approval_ref="owner-approval-2").usage()["ceiling_usd"] == 5.0
    assert book.usage()["ceiling_usd"] == 10.0


@pytest.mark.parametrize(("usd", "ref"), [(0.0, REF), (10.01, REF), (1.0, ""), (1.0, "  ")])
def test_invalid_ceilings_are_refused(tmp_path, usd, ref):
    with pytest.raises(ConfigError):
        ReviewLedger(tmp_path / "l.sqlite", usd_ceiling=usd, approval_ref=ref)


def test_reservations_are_cumulative_across_processes_and_bounded(tmp_path):
    a = ledger(tmp_path, 1.0)
    held = reserve(a, 0.4)
    a.commit(held, 0.3)
    b = ledger(tmp_path, 1.0)  # another run, later
    reserve(b, 0.6)  # never settled: a crashed process
    usage = b.usage()
    assert (usage["committed_usd"], usage["open_reserved_usd"], usage["remaining_usd"], usage["calls"]) == (0.3, 0.6, 0.1, 2)
    with pytest.raises(BudgetExhausted) as info:
        reserve(b, 0.2)
    assert info.value.kind == "review_usd_ceiling" and info.value.remaining == pytest.approx(0.1)
    assert b.usage()["calls"] == 2  # a refused reservation leaves no row


def test_commit_without_usage_keeps_the_worst_case_and_release_frees_it(tmp_path):
    book = ledger(tmp_path, 1.0)
    assert book.commit(reserve(book, 0.25), None) == 0.25
    book.release(reserve(book, 0.5))
    usage = book.usage()
    assert (usage["committed_usd"], usage["open_reserved_usd"], usage["committed_by_scope"]) == (0.25, 0.0, {"identity": 0.25})


def test_ledger_tables_are_append_only(tmp_path):
    book = ledger(tmp_path)
    book.commit(reserve(book, 0.1), 0.05)
    with sqlite3.connect(tmp_path / "ledger.sqlite") as db:
        for statement in ("UPDATE ceiling SET usd_micro = 99000000", "DELETE FROM ceiling", "DELETE FROM reservation", "UPDATE settlement SET usd_micro = 0"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(statement)


def test_concurrent_reservations_never_overshoot(tmp_path):
    ledger(tmp_path, 1.0)
    granted, refused = [], []

    def worker():
        book = ledger(tmp_path, 1.0)
        for _ in range(5):
            try:
                granted.append(reserve(book, 0.07))
            except BudgetExhausted:
                refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 14 and len(refused) == 6
    assert ledger(tmp_path, 1.0).usage()["open_reserved_usd"] == pytest.approx(0.98)


def test_read_usage_is_read_only(tmp_path):
    assert read_usage(tmp_path / "absent.sqlite") is None and not (tmp_path / "absent.sqlite").exists()
    ledger(tmp_path, 3.0)
    assert read_usage(tmp_path / "ledger.sqlite", 1.0)["ceiling_usd"] == 1.0


def test_worst_case_uses_chars_over_three_and_max_tokens():
    assert worst_case_usd(PRICES, "m", 3000, 500) == pytest.approx((1000 * 3 + 500 * 15) / 1e6)
    assert worst_case_usd(PRICES, "m", 1, 0) == pytest.approx(3 / 1e6)


def test_the_meter_reserves_the_worst_case_and_commits_priced_usage(tmp_path):
    book = ledger(tmp_path, 1.0)
    meter = ReviewMeter(book, PRICES, "m", scope="identity", subject="s", model="m", prompt_chars=3000, max_tokens=500)
    meter.reserve(CallEstimate(input_tokens=10, output_tokens=10))  # the provider's lower estimate is raised to the floor
    assert book.usage()["open_reserved_usd"] == pytest.approx((1000 * 3 + 500 * 15) / 1e6)
    meter.record({"usage": {"reported": True, "input_tokens": 800, "output_tokens": 100}})
    assert book.usage()["committed_usd"] == pytest.approx((800 * 3 + 100 * 15) / 1e6) and book.usage()["open_reserved_usd"] == 0
    assert meter.calls == 1 and meter.committed_usd == pytest.approx(0.0039)


def test_the_meter_refuses_unpriced_roles_and_respects_the_run_cap(tmp_path):
    book = ledger(tmp_path, 1.0)
    with pytest.raises(ConfigError, match="price"):
        ReviewMeter(book, PRICES, "other", scope="identity", subject="s", model=None, prompt_chars=10, max_tokens=10)
    with pytest.raises(ConfigError, match="price"):
        ReviewMeter(book, None, "m", scope="identity", subject="s", model=None, prompt_chars=10, max_tokens=10)
    cap = RunCap(0.005)
    meter = ReviewMeter(book, PRICES, "m", scope="identity", subject="s", model="m", prompt_chars=3000, max_tokens=500, run_cap=cap)
    with pytest.raises(BudgetExhausted) as info:
        meter.reserve()
    assert info.value.kind == "review_run_usd" and book.usage()["calls"] == 0
    with pytest.raises(ValueError):
        RunCap(0)
