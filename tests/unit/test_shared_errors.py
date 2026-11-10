"""Shared-pattern membership is backed by joint scoped cells, never Cartesian products."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from su_multi_arm.communities.base import declared_communities
from su_multi_arm.communities.shared_errors import approved_error_labels, error_labels_identity, shared_error_documents
from su_multi_arm.core.config_logs import SharedErrorConfig
from su_multi_arm.extraction.patterns.library import Library, LoadedEntry, PatternEntry


def cell(tenant="alpha", service="api", signature="dependency.timeout", day="2026-03-10", level="ERROR", lines=3, **extra):
    return {"date": day, "hour": f"{day}T12:00:00+00:00", "attributes": {"tenant": tenant, "service": service, "env": "prod"}, "signature_id": signature, "level": level, "status": "parsed", "lines": lines, "lines_pulled": lines, "row_exactness": "exact", "method": "complete", **extra}


def build(cells, **cfg):
    return shared_error_documents("synthetic", cells, SharedErrorConfig(**cfg), {"date_basis": "index_date", "date_timezone": "UTC"})


def test_requires_observed_multiple_tenants_and_failure_severity():
    rows = [cell(), cell("beta", level="INFO"), cell("gamma", level="WARN"), cell("delta", level=None), cell("epsilon", status="noise"), cell("zeta", signature=None)]
    assert build(rows).documents == []
    rows.append(cell("beta", service="worker", level="FATAL"))
    result = build(rows)
    assert result.groups == 1 and result.ineligible_cells == 5
    assert {(doc.metadata["tenant"], doc.metadata["service"], doc.metadata["level"]) for doc in result.documents} == {("alpha", "api", "ERROR"), ("beta", "worker", "FATAL")}
    for doc in result.documents:
        other = "beta" if doc.metadata["tenant"] == "alpha" else "alpha"
        assert other not in json.dumps(doc.model_dump(mode="json"))
        assert "tenants_affected" not in doc.metadata


def test_day_membership_family_identity_exclusions_and_determinism():
    rows = [cell(), cell("beta"), cell(day="2026-03-11"), cell("beta", day="2026-03-11"), cell(signature="generic.error"), cell("beta", signature="generic.error")]
    result = build(rows)
    assert result.excluded_cells == 2 and result.groups == 2
    assert len({doc.metadata["community_id"] for doc in result.documents}) == 2
    assert len({doc.metadata["community_family_id"] for doc in result.documents}) == 1
    assert result == build(reversed(rows))
    assert build([cell(), cell("beta", day="2026-03-11")]).groups == 0
    assert build(rows, exclude_signatures=["dependency.timeout", "generic.error"]).groups == 0
    capped = build(rows, max_groups=1)
    assert capped.groups == 1 and capped.dropped_groups == 1


def test_support_threshold_uses_observations_not_scaled_estimates_and_stats_use_every_member():
    rows = [cell(lines=100, lines_pulled=1, row_exactness="approximate"), cell("beta", lines=7)]
    assert build(rows, min_lines_per_tenant=2).groups == 0
    result = build(rows)
    communities = declared_communities(result.documents, [], 1)
    assert len(communities) == 1
    stats = communities[0].stats
    assert stats["tenants_affected"] == 2 and stats["lines"] == 107 and stats["lines_pulled"] == 8
    assert stats["row_exactness"] == "approximate"
    assert "107" not in " ".join(document.fields["summary"] for document in result.documents)
    with pytest.raises(ValidationError):
        SharedErrorConfig(min_tenants=1)


def test_service_and_deployment_are_not_cross_joined():
    rows = [cell(), cell("beta", service="worker"), cell("beta", service="api", attributes={"tenant": "beta", "env": "dev", "service": "api", "deployment": "beta-dev-custom"})]
    result = build(rows)
    assert len(result.documents) == 3
    assert {(doc.metadata["tenant"], doc.metadata["service"], doc.metadata["deployment"]) for doc in result.documents} == {("alpha", "api", "alpha-prod"), ("beta", "worker", "beta-prod"), ("beta", "api", "beta-dev-custom")}


def test_exact_grouped_occurrences_support_membership_without_inventing_examples():
    rows = [cell(tenant, lines=100, lines_pulled=1, method="exact_groups") for tenant in ("alpha", "beta")]
    result = build(rows, min_lines_per_tenant=10)
    assert result.groups == 1
    for doc in result.documents:
        assert doc.metadata["lines"] == 100 and doc.metadata["lines_pulled"] == 1
        assert "1 downloaded examples" in doc.fields["summary"]
    assert build([{**row, "row_exactness": "approximate"} for row in rows], min_lines_per_tenant=10).groups == 0


def test_approved_issue_names_and_expressions_replace_hashes_without_copying_examples():
    signature = "llm.abcdef123456"
    expression = "^Dependency failure " + "synthetic " * 30 + "[0-9]+$"
    entry = PatternEntry(id="msg.synthetic", kind="message", status="approved", regex=expression,
                         signature={"id": signature, "category": "dependency"}, provenance={"issue_name": "Dependency connection failure"},
                         examples={"positive": ["DO_NOT_COPY_ANOTHER_CLIENT_EXAMPLE"]})
    proposed = entry.model_copy(update={"id": "msg.pending", "status": "proposed", "signature": entry.signature.model_copy(update={"id": "llm.pending"})})
    library = Library(path=Path("/synthetic"), name="synthetic", engine_hint="regex", vocabularies={}, masks={},
                      entries={entry.id: LoadedEntry(entry, "learned", "synthetic", expression, "approved"), proposed.id: LoadedEntry(proposed, "proposed", "synthetic", expression, "proposed")},
                      applied_decisions=[], issues=[], file_shas={})
    labels = approved_error_labels(SimpleNamespace(library=library))
    assert set(labels) == {signature}
    rows = [cell(signature=signature), cell("beta", signature=signature)]
    before = build(rows)
    after = shared_error_documents("synthetic", rows, SharedErrorConfig(), {"date_basis": "index_date", "date_timezone": "UTC"}, labels)
    assert [doc.id for doc in before.documents] == [doc.id for doc in after.documents]
    for doc in after.documents:
        assert doc.metadata["error_type"] == "Dependency connection failure"
        assert doc.metadata["pattern_origin"] == "learned" and doc.metadata["pattern_reviewed"] is False
        assert doc.metadata["template_preview_truncated"] is True
        assert doc.fields["template"] == expression
        assert "preview truncated" in doc.fields["summary"] and "provisional" in doc.fields["summary"]
        assert "Dependency connection failure" in doc.title and "DO_NOT_COPY_ANOTHER_CLIENT_EXAMPLE" not in doc.model_dump_json()
    matching_identity = library.identity()
    entry.provenance["issue_name"] = "Renamed dependency connection failure"
    assert library.identity() == matching_identity
    assert error_labels_identity(labels) != error_labels_identity(approved_error_labels(SimpleNamespace(library=library)))
