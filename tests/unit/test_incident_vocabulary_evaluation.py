"""Synthetic selector/accounting checks; no private helpers, source or provider calls."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "incident_vocabulary_evaluation", Path(__file__).parents[2] / "scripts/evaluate_incident_vocabulary.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SEED = {
    "families": [
        {"id": "failure", "phrases": ["error", "failed", "job failed"]},
        {"id": "recovery", "phrases": ["connection established"]},
    ]
}
PROPOSED = {
    "families": [
        {"id": "failure", "phrases": ["failed", "timeout"]},
        {"id": "recovery", "phrases": ["connection established"]},
    ]
}


def test_overlapping_phrases_and_literal_boundaries():
    assert m.Vocabulary(SEED).phrases("Job FAILED; errorCount=0; terror") == {"job failed", "failed"}
    assert m.Vocabulary(SEED).phrases("job\tfailed") == {"job failed", "failed"}


def test_recovery_is_context_and_zero_is_not_global_suppression():
    c = m.Comparison(SEED, PROPOSED)
    c.add({"text": "connection established", "level": "info", "count": 7})
    c.add({"text": "error=0; timeout", "level": "info", "count": 3})
    totals = c.report()["totals"]
    assert totals["seed_recovery_context"] == 7
    assert totals["seed_selected"] == totals["proposed_selected"] == 3
    assert totals["proposed_selected_zero_failure"] == 3


def test_structured_error_http_union_and_stratified_counts():
    c = m.Comparison(SEED, PROPOSED)
    c.add(
        {
            "text": "unfamiliar diagnostic",
            "level": "ERROR",
            "level_basis": "level_field",
            "day": "2026-01-01",
            "service": "synthetic",
            "count": 5,
        }
    )
    c.add({"text": "response received", "http_status": 503, "count": 2})
    c.add({"text": "response received", "http_status": "503"})
    report = c.report()
    assert report["totals"]["proposed_selected"] == 7
    assert report["totals"]["proposed_structured_error_retained"] == 5
    assert report["totals"]["proposed_selected_fraction"] == 7 / 8
    assert sum(s["heads"] for s in report["strata"]) == 8


def test_keyword_only_delta_does_not_claim_incident():
    c = m.Comparison(SEED, PROPOSED)
    c.add({"text": "error=0", "count": 9})
    totals = c.report()["totals"]
    assert totals["seed_only_selected"] == 9
    assert totals["seed_selected_zero_failure"] == 9
    assert totals["high_confidence_selector"] == 0


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_invalid_weight_is_visible(count):
    with pytest.raises(ValueError, match="positive integer"):
        m.Comparison(SEED, PROPOSED).add({"text": "failed", "count": count})


def test_policy_severity_and_context_only_configuration():
    proposed = {**PROPOSED, "severity_values": ["ERROR", "WARN", "SEVERE"], "context_phrases": ["resumed"]}
    c = m.Comparison(SEED, proposed)
    c.add({"text": "routine lifecycle", "level": "WARN", "count": 4})
    c.add({"text": "resumed", "level": "INFO", "count": 3})
    totals = c.report()["totals"]
    assert totals["proposed_selected"] == totals["proposed_severity_selector"] == 4
    assert totals["proposed_recovery_context"] == 3
    assert totals["seed_selected"] == 0
