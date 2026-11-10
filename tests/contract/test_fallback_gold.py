"""E1b: the synthetic fallback gold set (addendum A2 §6.2) run day by day with an oracle model built
from the generator's truth. It measures the machinery (clustering, gate, grounding, caps, backlog
re-ranking, egress), not a model: slot precision and recall against the planted values, 100 % span
grounding, every PII/tracking line refused, injection text never becoming a pattern, and the
backlog trajectory under a cluster cap. Synthetic data, mocked provider, no network."""

from __future__ import annotations

from datetime import date

import pytest

from su_multi_arm.testing.fixtures.logs.fallback_gold import FIELDS, digest, generate, oracle_cluster, oracle_lines
from tests.fallback_support import FakeProvider, config, load, seal_day, session, write_library
from tests.logs_support import ENGINE
from tests.pattern_fixtures import engine_or_skip

FIRST = date(2026, 1, 5)
DAYS = 4


@pytest.fixture(autouse=True)
def _engine():
    engine_or_skip(ENGINE)


def gold_config(tmp_path, cap: int):
    write_library(tmp_path / "patterns")
    return config(tmp_path, extraction={"fields": dict(FIELDS), "template": {"max_clusters_per_day": cap}, "egress": {"max_refused_share": 0.2}})


def line_text(store, key, head_id):
    for unit in store.units(key):
        for record in unit.records:
            if record.id == head_id:
                return record.message.split("\n", 1)[0]
    raise KeyError(head_id)


def run_days(tmp_path, *, cap: int, obey_injection: bool = False):
    cfg = gold_config(tmp_path, cap)
    gold = generate(FIRST, DAYS)
    store = None
    trajectory, metrics = [], {"tp": 0, "fp": 0, "fn": 0, "values": 0, "grounded": 0, "pii": 0, "pii_refused": 0, "injection_patterns": 0}
    for day in gold:
        store = seal_day(cfg, day.day, rows=day.rows())
        fallback = session(cfg, FakeProvider(cluster=lambda p: oracle_cluster(p, obey_injection=obey_injection), lines=oracle_lines))
        library = load(cfg)
        key = day.day.isoformat()
        # the daily path: today plus earlier days whose overlay deferred heads or is stale (ranked together)
        keys = [key] + [k for k in sorted(store.keys(), reverse=True) if k < key and fallback.status(store, k, library)["status"] in {"deferred", "stale", "missing"}]
        report = fallback.run(store, keys, library)
        trajectory.append({"day": key, "deferred_clusters": report["backlog"]["deferred_clusters"], "deferred_heads": report["backlog"]["deferred_heads"]})
        metrics["injection_patterns"] += sum(1 for p in report["proposals"] if "ignore" in str(p))
    # score the final overlays of every day against the truth
    for day in gold:
        key = day.day.isoformat()
        state = session(cfg, FakeProvider()).status(store, key, load(cfg))
        from su_multi_arm.ingestion.overlay import OverlayStore

        _, heads = OverlayStore(store).rows(key, state.get("identity"), version=state["version"])  # the current overlay
        by_id = {h.head_id: h for h in heads}
        for index, line in enumerate(day.lines):
            head_id = f"{key}-h{index:03d}"
            head = by_id.get(head_id)
            if line.kind == "pii":
                metrics["pii"] += 1
                metrics["pii_refused"] += int(head is not None and head.reason == "egress_refused")
                continue
            got = {f.name: f.value for f in (head.fields if head else []) if f.source != "pattern"}
            text = line_text(store, key, head_id)
            for value in head.fields if head else []:
                metrics["values"] += 1
                metrics["grounded"] += int(text[value.start : value.end] == str(value.value))
            for name, expected in line.fields.items():
                if got.get(name) == expected:
                    metrics["tp"] += 1
                else:
                    metrics["fn"] += 1
            metrics["fp"] += sum(1 for name, value in got.items() if line.fields.get(name) != value)
    return trajectory, metrics


def test_the_generator_is_deterministic():
    assert digest(generate(FIRST, DAYS)) == digest(generate(FIRST, DAYS))
    assert digest(generate(FIRST, DAYS)) != digest(generate(FIRST, DAYS, seed=12))


def test_gold_slots_precision_recall_and_grounding_without_a_cap(tmp_path):
    trajectory, m = run_days(tmp_path, cap=150)
    precision = m["tp"] / (m["tp"] + m["fp"])
    recall = m["tp"] / (m["tp"] + m["fn"])
    assert precision == 1.0 and recall == 1.0, m
    assert m["grounded"] == m["values"] > 0  # 100 % span grounding
    assert m["pii_refused"] == m["pii"] == 2 * DAYS
    assert m["injection_patterns"] == 0
    assert all(t["deferred_heads"] == 0 for t in trajectory)


def test_backlog_under_a_cap_is_reranked_and_drains(tmp_path):
    # 7 clusters a day (6 templates + the injection cluster) against a cap of 4: day 1 defers
    # clusters; recurring templates are then pending proposals (no call) and the backlog drains.
    trajectory, m = run_days(tmp_path, cap=4)
    assert trajectory[0]["deferred_clusters"] > 0
    assert trajectory[-1]["deferred_heads"] == 0, trajectory
    assert m["tp"] / (m["tp"] + m["fn"]) == 1.0 and m["fp"] == 0  # every deferred head was filled later


def test_an_obedient_model_cannot_turn_injection_text_into_a_pattern(tmp_path):
    trajectory, m = run_days(tmp_path, cap=150, obey_injection=True)
    assert m["injection_patterns"] == 0 and m["fp"] == 0  # the gate refuses ^.*$ (over-broad)
    assert m["tp"] / (m["tp"] + m["fn"]) == 1.0
