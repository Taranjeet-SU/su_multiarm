"""Three separate coverage stages with explicit, comparable counting units.

Collection compares source documents with source documents across the full matching unit scope.
Parser scanning and useful extraction compare assembled head records with head records. Joined
continuations are not additional heads. Noise is never useful extraction. Old counters are unknown.
"""
from __future__ import annotations

from typing import Any

COVERAGE_METRICS_VERSION = 3
METRIC_FIELDS = (
    "documents_collected", "documents_in_collection_scope", "collection_known_units", "selected_tier_units",
    "records_collected", "records_scanned", "records_extracted", "scanning_known_units",
    "collection_invalid_units", "scanning_invalid_units", "extraction_invalid_units",
    "documents_represented", "representation_known_units", "representation_invalid_units", "grouped_units",
)
INCIDENT_METRIC_FIELDS = ("incident_units", "source_window_documents", "selected_seed_documents", "seeds_collected", "context_documents", "context_pending_seeds")


def coverage_counters(unit: dict[str, Any]) -> dict[str, Any]:
    collected = unit.get("documents_collected")
    scanned = unit.get("parser_heads_scanned")
    # Presence, not a nonzero count, establishes whether the counter was recorded.
    collection_known = isinstance(collected, int) and not isinstance(collected, bool)
    scanning_known = isinstance(scanned, int) and not isinstance(scanned, bool)
    grouped = unit.get("method") == "exact_groups"
    incident = unit.get("method") == "incident_subset"
    represented = unit.get("documents_represented") if grouped else collected
    representation_known = type(represented) is int
    return {
        "documents_collected": collected if collection_known else None,
        "documents_in_collection_scope": unit.get("documents_in_collection_scope") if collection_known else None,
        "collection_known_units": int(collection_known),
        "selected_tier_units": int(collection_known and unit.get("collection_scope") == "selected_records_tier"),
        "records_collected": unit["pulled"] if scanning_known else None,
        "records_scanned": scanned if scanning_known else None,
        "records_extracted": unit["statuses"]["parsed"] if scanning_known else None,
        "scanning_known_units": int(scanning_known),
        "collection_invalid_units": int(collection_known and not incident and not 0 <= collected <= unit["lines_total"]),
        "scanning_invalid_units": int(scanning_known and not 0 <= scanned <= unit["pulled"]),
        "extraction_invalid_units": int(scanning_known and not 0 <= unit["statuses"]["parsed"] <= scanned),
        "documents_represented": represented if representation_known else None,
        "representation_known_units": int(representation_known),
        "representation_invalid_units": int(representation_known and (
            (not incident and not 0 <= represented <= unit["lines_total"]) or collection_known and represented < collected
        )),
        "grouped_units": int(grouped),
        **{key: unit.get(key, 0) if incident else 0 for key in ("incident_units", "source_window_documents", "selected_seed_documents", "seeds_collected", "context_documents", "context_pending_seeds")},
    }


def _ratio(numerator: Any, denominator: Any, known: bool) -> tuple[float | None, str]:
    if not known or numerator is None or denominator is None:
        return None, "unavailable"
    if numerator < 0 or denominator < 0 or numerator > denominator:
        return None, "inconsistent"
    if denominator == 0:
        return None, "empty"
    return round(numerator / denominator, 4), "available"


def coverage_metrics(sums: dict[str, Any], units: int) -> dict[str, Any]:
    collection_known = units > 0 and sums.get("collection_known_units") == units
    scanning_known = units > 0 and sums.get("scanning_known_units") == units
    representation_known = units > 0 and sums.get("representation_known_units") == units
    result = {
        "coverage_units": units,
        "completeness_unit": "source_documents",
        "scanned_unit": "assembled_records",
        "extracted_unit": "assembled_records",
        "completeness_scope": "all_matching_source_documents_in_indexed_units",
        "collection_scope": "unavailable" if not collection_known else ("includes_selected_records_tier" if sums.get("selected_tier_units", 0) else "all_source_documents"),
        "documents_matching": sums.get("lines_total"),
        **{key: sums.get(key) if collection_known else None for key in ("documents_collected", "documents_in_collection_scope")},
        **{key: sums.get(key) if scanning_known else None for key in ("records_collected", "records_scanned", "records_extracted")},
    }
    result["documents_represented"] = sums.get("documents_represented") if representation_known else None
    result["representation_status"] = "available" if representation_known else "unavailable"
    if sums.get("representation_invalid_units"):
        result["documents_represented"] = None
        result["representation_status"] = "inconsistent"
    result["grouped_units"] = sums.get("grouped_units", 0)
    if result["grouped_units"]:
        result["collection_scope"] = "includes_grouped_examples"
        result["collection_note"] = "Completeness counts downloaded source documents; grouped occurrence counts are reported separately as documents_represented. Scanned and extracted describe downloaded assembled records."
    result["completeness"], result["completeness_status"] = _ratio(result["documents_collected"], result["documents_matching"], collection_known)
    result["scanned"], result["scanned_status"] = _ratio(result["records_scanned"], result["records_collected"], scanning_known)
    result["extracted"], result["extracted_status"] = _ratio(result["records_extracted"], result["records_scanned"], scanning_known)
    if sums.get("incident_units", 0):
        result.update({key: sums.get(key, 0) for key in ("incident_units", "source_window_documents", "selected_seed_documents", "seeds_collected", "context_documents", "context_pending_seeds")})
        result.update(collection_scope="incident_seed_policy_with_bounded_context",
                      completeness_scope="source_population_at_seed_snapshot_only",
                      completeness=None, completeness_status="scope_filtered",
                      context_status="pending" if sums.get("context_pending_seeds", 0) else "complete_for_requested_windows",
                      collection_note="Deliberately filtered incident seeds and bounded context; exact selected counts do not establish whole-source completeness. Context snapshots are separate.")
        result["seed_completeness"], result["seed_completeness_status"] = _ratio(sums.get("seeds_collected"), sums.get("selected_seed_documents"), collection_known)
        result["seed_completeness_percent"] = round(result["seed_completeness"] * 100, 2) if result["seed_completeness"] is not None else None
    for metric, invalid in (("completeness", "collection_invalid_units"), ("scanned", "scanning_invalid_units"), ("extracted", "extraction_invalid_units")):
        if sums.get(invalid):
            result[metric], result[f"{metric}_status"] = None, "inconsistent"
        result[f"{metric}_percent"] = round(result[metric] * 100, 2) if result[metric] is not None else None
    return result
