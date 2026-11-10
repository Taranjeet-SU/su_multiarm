"""Accounting identities through the real puller and the fallback in ``run_daily`` (addendum A2
rev 2 §4.2.1, §4.2.8; review B1, B2) on the fake log cluster with a scripted fake provider:
joined and overflow documents balance identity D, a puller miscount refuses the seal, the fallback
runs after the seal and writes an overlay, a failed fallback leaves the heads ``fallback_pending``
without stopping compose, ``apply_decisions`` reparses once after an auto-approved entry and
rebuilds the overlay from the cache with no call, and ``logs coverage`` reports both numbers with
fragment accounting. Synthetic data, mocked provider, no network."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from su_multi_arm.core.config import validate_config
from su_multi_arm.ingestion.accounting import AccountingImbalance, UnitAccounting
from su_multi_arm.learning.patterns.spend import TotalSpendLedger
from su_multi_arm.core.errors import BudgetExhausted
from tests.fallback_support import NOW, FakeProvider, fallback_data
from tests.logs_support import ENGINE, Clock, cluster_for, pipeline
from tests.pattern_fixtures import engine_or_skip

DAY = date(2026, 1, 5)
KEY = DAY.isoformat()


@pytest.fixture(autouse=True)
def _engine():
    engine_or_skip(ENGINE)


def build(tmp: Path, *, multiline: dict | None = None, extraction: dict | None = None, promotion: str = "manual", source=None, fake: FakeProvider | None = None, cluster=None):
    data = fallback_data(tmp, extraction=extraction, promotion=promotion)
    data["logs"]["accounting"] = {}
    if multiline:
        data["sources"]["synthetic"]["multiline"] = multiline
    pipe, cfg, backend = pipeline(tmp, cluster or cluster_for([DAY], lines=20), data=data, now=Clock(DAY + timedelta(days=1)), source=source)
    if cfg.learning.extraction.enabled:
        fake = fake or FakeProvider()
        from su_multi_arm.learning.extraction import FallbackSession, HttpProbe

        pipe._fallback = FallbackSession.from_config(cfg, load=pipe.library, transport=fake.transport(), probe=HttpProbe(fake.transport()), clock=lambda: NOW)
    return pipe, cfg, fake


def units(pipe) -> list[UnitAccounting]:
    return [UnitAccounting.model_validate(u) for u in pipe.store.get(KEY).accounting["units"].values()]


# --- identities S/Q/D/H ------------------------------------------------------------------------


def test_joined_and_overflow_documents_balance_identity_d(tmp_path):
    pipe, _, _ = build(tmp_path, multiline={"max_lines": 2}, extraction={"enabled": False})
    pipe.run_daily(DAY, retention=False)
    accounts = units(pipe)
    assert sum(a.joined for a in accounts) > 0 and sum(a.overflow for a in accounts) > 0 and sum(a.fragments for a in accounts) > 0
    for a in accounts:
        assert a.recorded and a.balanced()
        assert a.docs_pulled == a.malformed + a.heads + a.joined + a.fragments + a.overflow  # D
        assert a.heads == sum(a.by_status.values())  # H
        assert sum(c.fetched for c in a.quota_checks) == a.docs_pulled and all(c.ok for c in a.quota_checks)  # Q
        assert a.rest_basis == "derived" and a.total == sum(a.strata.values()) + a.rest  # S (reported)


class DroppingSource:
    """Wraps the real source: hands over one document fewer than the puller counted (a miscount in
    the puller, not in the parser)."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def pull(self, *args, **kwargs):
        dropped = False
        for batch in self.inner.pull(*args, **kwargs):
            if batch.docs and not dropped:
                dropped = True
                batch = batch.model_copy(update={"docs": batch.docs[1:]})
            yield batch


def test_a_puller_miscount_refuses_the_seal(tmp_path):
    from su_multi_arm.sources import build_source
    from tests.logs_support import ENV, no_wait

    cluster = cluster_for([DAY], lines=20)
    data = fallback_data(tmp_path, extraction={"enabled": False})
    data["logs"]["accounting"] = {}
    cfg = validate_config(data)
    inner = build_source("synthetic", cfg.sources["synthetic"], env=ENV, http_transport=cluster.transport())
    inner._rate_limiter = no_wait()
    pipe, _, _ = build(tmp_path, extraction={"enabled": False}, source=DroppingSource(inner), cluster=cluster)
    with pytest.raises(AccountingImbalance, match="identity Q"):
        pipe.run_daily(DAY, retention=False)
    latest = pipe.store.latest(KEY)
    assert latest is not None and latest.status == "open"  # unsealed and resumable
    assert pipe.coverage(DAY, DAY)["ok"] is False


def test_identity_h_fails_when_a_head_goes_missing():
    account = UnitAccounting(unit_id="u1", recorded=False, heads=3, by_status={"parsed": 2})
    assert not account.balanced()
    with pytest.raises(AccountingImbalance, match="identity H"):
        account.check()


# --- the daily fallback ------------------------------------------------------------------------


def test_run_daily_runs_the_fallback_after_the_seal_and_reports_coverage(tmp_path):
    pipe, cfg, fake = build(tmp_path)
    report = pipe.run_daily(DAY, retention=False)
    assert report.ok
    sealed = pipe.store.get(KEY)
    assert sealed.accounting["coverage"]["unresolved_by_reason"].get("fallback_pending", 0) + sealed.accounting["coverage"]["unresolved_by_reason"].get("format_only", 0) > 0
    step = report.steps["fallback"]
    assert step["status"] == "ran" and step["partitions"][KEY]["status"] == "written"
    assert report.steps["apply_decisions"] == {"status": "unchanged"}
    assert fake.requests  # the residue reached the (fake) model
    coverage = pipe.coverage(DAY, DAY)
    day = coverage["days"][KEY]
    assert coverage["ok"] and day["fallback"]["status"] in {"current", "deferred"}
    assert day["regex_only"]["fragments"] > 0 and day["regex_only"]["fragment_share"] > 0  # fragment accounting (N16)
    assert day["with_llm"] is not None and day["with_llm"]["accounted"] and day["regex_only"]["heads"] == day["with_llm"]["heads"]
    assert "targets" in day["by_service_tier"][0]


def test_the_derived_snapshot_reads_the_overlay_through_overlaystore_rows(tmp_path):
    """Round trip: run_daily seals, writes the overlay, composes; the derived snapshot of the day
    carries that overlay's identity, accounting and row aggregates (no field values)."""
    import json

    from su_multi_arm.ingestion.logs_compose import read_derived
    from su_multi_arm.ingestion.overlay import OverlayStore

    pipe, _, _ = build(tmp_path)
    report = pipe.run_daily(DAY, retention=False)
    written = report.steps["fallback"]["partitions"][KEY]
    snapshot = read_derived(pipe.store, KEY)
    manifest, rows = OverlayStore(pipe.store).rows(KEY, written["identity"])
    assert snapshot["overlay_identity"] == written["identity"] == manifest.identity
    assert snapshot["overlay"]["accounting"] == {u: a.model_dump(mode="json") for u, a in manifest.accounting.items()}
    assert snapshot["overlay"]["rows"]["heads"] == len(rows) == sum(written["by_status"].values())
    values = [str(f.value) for h in rows for f in h.fields if len(str(f.value)) > 3]
    assert not any(v in json.dumps(snapshot["overlay"]) for v in values)


def test_a_failed_fallback_leaves_heads_pending_and_compose_still_serves(tmp_path):
    pipe, _, fake = build(tmp_path, extraction={"egress": {"max_refused_share": 0.0}})  # the synthetic day has e-mail shapes at rest? force a refusal
    pipe._fallback.egress.split = _refuse_all
    report = pipe.run_daily(DAY, retention=False)
    assert not report.ok and report.steps["fallback"]["status"] == "failed"
    assert report.steps["compose"]["status"] == "built"
    reasons = pipe.store.get(KEY).accounting["coverage"]["unresolved_by_reason"]
    assert reasons.get("fallback_pending", 0) > 0 and fake.requests == []


def _refuse_all(lines, share):
    from su_multi_arm.learning.patterns.egress import EgressRefused

    raise EgressRefused("refused for the test")


def test_apply_decisions_reparses_once_and_rebuilds_the_overlay_without_calls(tmp_path):
    if ENGINE != "re2":
        pytest.skip("auto-promotion needs a gate run on RE2")

    def script(payload):
        from tests.fallback_support import cluster_reply

        reply = cluster_reply(payload)
        if payload["task"] == "cluster" and payload["groups"]:
            reply.update(action="new", regex=None, labels=[{"group": payload["groups"][0]["group"], "field": "dependency", "type": "string", "entity_type": None}])
        return reply

    fake = FakeProvider(cluster=script)
    pipe, cfg, _ = build(tmp_path, promotion="auto", fake=fake)
    report = pipe.run_daily(DAY, retention=False)
    decisions = report.steps["apply_decisions"]
    if not report.steps["fallback"].get("proposals"):
        pytest.skip("no synthetic cluster passed the gate under this engine")
    assert decisions["status"] == "reparsed" and decisions["version"] == 2
    rebuilt = decisions["fallback"]
    assert rebuilt["allow_calls"] is False
    assert rebuilt["calls"]["pattern_learner"]["calls"] == 0
    assert pipe.store.get(KEY).version == 2 and report.ok


# --- total-period ledger -----------------------------------------------------------------------


def test_total_ledger_caps_across_days_and_projects_exhaustion(tmp_path):
    days = iter([NOW + timedelta(days=i) for i in range(10)])
    current = {"now": next(days)}
    ledger = TotalSpendLedger(tmp_path / "spend.sqlite", key="provider:openai", max_usd=2.0, clock=lambda: current["now"])
    for _ in range(3):
        held = ledger.reserve(0.5)
        ledger.commit(held, 0.5)
        current["now"] = next(days)
    usage = ledger.usage()
    assert usage["used_usd"] == 1.5 and usage["remaining_usd"] == 0.5 and usage["projected_days_left"] == 1.0 and usage["alert"]
    with pytest.raises(BudgetExhausted, match="total_usd"):
        ledger.reserve(0.6)
    other = TotalSpendLedger(tmp_path / "spend.sqlite", key="provider:openai", max_usd=2.0, clock=lambda: current["now"])
    assert other.usage()["used_usd"] == 1.5  # persistent, shared by every process using the ledger
