"""Stage percentages never mix source documents with assembled records or count noise as extraction."""
from su_multi_arm.runtime.coverage_metrics import coverage_counters, coverage_metrics


def unit(**overrides):
    return {"lines_total": 100, "documents_collected": 20, "documents_in_collection_scope": 20,
            "collection_scope": "selected_records_tier", "pulled": 10, "parser_heads_scanned": 10,
            "statuses": {"parsed": 4, "noise": 6}, **overrides}


def result(value):
    return coverage_metrics({**coverage_counters(value), "lines_total": value["lines_total"]}, 1)


def test_selected_tier_is_not_complete_and_noise_does_not_count_as_extracted():
    metrics = result(unit())
    assert metrics["completeness"] == 0.2
    assert metrics["scanned"] == 1 and metrics["extracted"] == 0.4
    assert metrics["documents_matching"] == 100 and metrics["documents_in_collection_scope"] == 20
    assert metrics["completeness_unit"] == "source_documents"
    assert metrics["scanned_unit"] == metrics["extracted_unit"] == "assembled_records"
    assert metrics["collection_scope"] == "includes_selected_records_tier"


def test_old_or_partly_recorded_counters_are_unavailable_not_zero_or_hundred_percent():
    legacy = unit()
    legacy.pop("documents_collected")
    legacy.pop("parser_heads_scanned")
    metrics = result(legacy)
    assert all(metrics[key] is None and metrics[f"{key}_status"] == "unavailable" for key in ("completeness", "scanned", "extracted"))
    sums = {**coverage_counters(unit()), "lines_total": 200}
    assert coverage_metrics(sums, 2)["completeness"] is None
    assert coverage_metrics(sums, 2)["scanned"] is None


def test_empty_and_inconsistent_counters_are_not_clamped_into_success():
    empty = result(unit(lines_total=0, documents_collected=0, pulled=0, parser_heads_scanned=0, statuses={"parsed": 0}))
    assert empty["completeness_status"] == empty["scanned_status"] == empty["extracted_status"] == "empty"
    bad = result(unit(documents_collected=110, parser_heads_scanned=11))
    assert bad["completeness"] is None and bad["completeness_status"] == "inconsistent"
    assert bad["scanned"] is None and bad["scanned_status"] == "inconsistent"
    # A bad unit cannot be hidden by a large denominator contributed by another good unit.
    left, right = coverage_counters(unit(documents_collected=110)), coverage_counters(unit())
    sums = {key: (left[key] or 0) + (right[key] or 0) for key in left}
    sums["lines_total"] = 200
    assert coverage_metrics(sums, 2)["completeness_status"] == "inconsistent"


def test_grouped_counts_do_not_inflate_downloaded_or_scanned_metrics():
    metrics = result(unit(method="exact_groups", documents_represented=100))
    assert metrics["completeness"] == 0.2
    assert metrics["documents_represented"] == 100
    assert metrics["records_scanned"] == 10 and metrics["records_extracted"] == 4
    assert metrics["collection_scope"] == "includes_grouped_examples"
    assert metrics["grouped_units"] == 1
    assert metrics["representation_status"] == "available"
    invalid = result(unit(method="exact_groups", documents_represented=10))
    assert invalid["representation_status"] == "inconsistent"
    assert invalid["documents_represented"] is None
    assert result(unit(method="exact_groups"))["representation_status"] == "unavailable"


def test_mixed_raw_and_grouped_representation_is_additive():
    left = coverage_counters(unit(method="exact_groups", documents_represented=100))
    right = coverage_counters(unit())
    sums = {key: (left[key] or 0) + (right[key] or 0) for key in left}
    sums["lines_total"] = 200
    metrics = coverage_metrics(sums, 2)
    assert metrics["documents_represented"] == 120
    assert metrics["documents_collected"] == 40
    assert metrics["completeness"] == 0.2
