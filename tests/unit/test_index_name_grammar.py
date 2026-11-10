"""Index-name grammar (addendum A1 §3.2.1, L1 parser fix): optional prefix, optional deployment
prefixes (``{n}`` = digits), segment vocabularies (longest entry first) and the date suffix. A name
that does not fit exactly once is ``unparsed`` with a typed reason, never a guess. Synthetic names."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from su_multi_arm.core.config_sources import IndexNameConfig
from su_multi_arm.sources.logs import IndexNameParser

SERVICES = ["crawler", "slack_bot", "gateway", "query-gateway", "doc-fetcher"]


def grammar(**overrides) -> IndexNameParser:
    data = {"deployment_prefixes": ["search-admin", "os-data-{n}"], "vocabularies": {"service": SERVICES}}
    data.update(overrides)
    return IndexNameParser(IndexNameConfig.model_validate(data))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("search-admin-acme-prod-crawler-2026.01.02", {"deployment": "search-admin", "tenant": "acme", "env": "prod", "service": "crawler"}),
        ("os-data-1-acme-prod-crawler-2026.01.02", {"deployment": "os-data-1", "tenant": "acme", "env": "prod", "service": "crawler"}),
        ("os-data-12-t9-dev-query-gateway-2026.01.02", {"deployment": "os-data-12", "tenant": "t9", "env": "dev", "service": "query-gateway"}),
        ("acme-prod-slack_bot-2026.01.02", {"deployment": "none", "tenant": "acme", "env": "prod", "service": "slack_bot"}),
        ("acme-stage-gateway-2026.12.31", {"deployment": "none", "tenant": "acme", "env": "stage", "service": "gateway"}),
    ],
)
def test_deployment_prefixes_and_underscored_services_parse(name, expected):
    parsed = grammar().parse(name)
    assert parsed.status == "parsed", parsed.problem
    assert parsed.attributes == {**expected, "index_date": parsed.index_date.isoformat()}
    assert parsed.index_date == date(int(name[-10:-6]), int(name[-5:-3]), int(name[-2:]))


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("x-acme-prod-crawler-2026.01.02", "segments"),  # undeclared left-side prefix: not a tenant guess
        ("os-data-acme-prod-crawler-2026.01.02", "segments"),  # {n} needs digits
        ("acme-prod-unknown_svc-2026.01.02", "service"),
        ("acme-prod-crawler", "no_date"),
        ("acme-prod-crawler-2026.02.30", "invalid_date"),
        ("search-admin-crawler-2026.01.02", "ambiguous"),  # fits only without its declared prefix
    ],
)
def test_names_outside_the_grammar_are_unparsed_with_a_typed_reason(name, reason):
    parsed = grammar().parse(name)
    assert parsed.status == "unparsed" and parsed.reason == reason, parsed
    assert parsed.problem and {parsed.attributes[k] for k in ("deployment", "tenant", "env", "service")} == {"unknown"}


def test_the_old_right_split_misreading_is_gone():
    """Without the deployment list, the previous parser read a deployment prefix as part of the tenant."""
    parsed = grammar(deployment_prefixes=[]).parse("search-admin-acme-prod-crawler-2026.01.02")
    assert parsed.status == "unparsed" and parsed.attributes["tenant"] == "unknown"
    assert "deployment" not in parsed.attributes


def test_a_both_ways_parse_is_ambiguous():
    # 'eu' is a deployment prefix, and 'eu-acme' is also a declared tenant: the name fits both readings.
    parser = grammar(deployment_prefixes=["eu"], prefix_fields=["tenant"], vocabularies={"service": SERVICES, "tenant": ["acme", "eu-acme"]})
    parsed = parser.parse("eu-acme-crawler-2026.01.02")
    assert parsed.status == "unparsed" and parsed.reason == "ambiguous"


def test_segment_vocabularies_take_the_longest_entry():
    parser = grammar(vocabularies={"service": SERVICES, "tenant": ["big", "big-tenant"], "env": ["prod", "stage"]})
    parsed = parser.parse("big-tenant-stage-doc-fetcher-2026.01.05")
    assert parsed.ok and parsed.attributes["tenant"] == "big-tenant" and parsed.attributes["env"] == "stage"
    parsed = parser.parse("small-stage-doc-fetcher-2026.01.05")
    assert parsed.reason == "vocabulary"
    parsed = parser.parse("big-qa-doc-fetcher-2026.01.05")
    assert parsed.reason == "vocabulary"


def test_a_literal_prefix_is_required_when_declared():
    parser = grammar(prefix="logs", deployment_prefixes=[])
    assert parser.parse("logs-acme-prod-crawler-2026.01.02").attributes["tenant"] == "acme"
    parsed = parser.parse("acme-prod-crawler-2026.01.02")
    assert parsed.reason == "prefix" and parsed.attributes["index_date"] == "2026-01-02"


@pytest.mark.parametrize(
    "bad",
    [
        {"deployment_prefixes": ["(a+)+"]},
        {"deployment_prefixes": ["-lead"]},
        {"deployment_prefixes": ["trail-"]},
        {"deployment_prefixes": ["dup", "dup"]},
        {"vocabularies": {"region": ["eu"]}},  # names no segment
        {"vocabularies": {"service": ["Bad Name"]}},
        {"prefix_fields": ["deployment"]},
        {"prefix": "logs-"},
    ],
)
def test_the_grammar_is_validated(bad):
    with pytest.raises(ValidationError):
        IndexNameConfig.model_validate(bad)


def test_fields_list_the_deployment_attribute_only_when_declared():
    assert grammar().fields() == ["deployment", "tenant", "env", "service", "index_date"]
    assert grammar(deployment_prefixes=[]).fields() == ["tenant", "env", "service", "index_date"]


# --- numeric service suffix (real-run fix) ---------------------------------------------------------


@pytest.mark.parametrize("vocabulary", [True, False])
def test_a_numeric_service_suffix_is_a_deployment_id(vocabulary):
    vocab = {"vocabularies": {"service": ["ml-service", "crawler"]}} if vocabulary else {"vocabularies": {}}
    parser = grammar(deployment_prefixes=[], service_suffix="numeric", **vocab)
    parsed = parser.parse("acme-prod-ml-service-1658254295-2026.01.02") if vocabulary else parser.parse("acme-prod-mlservice-1658254295-2026.01.02")
    assert parsed.status == "parsed", parsed.problem
    assert parsed.attributes["service"] == ("ml-service" if vocabulary else "mlservice") and parsed.attributes["deployment_id"] == "1658254295"
    plain = parser.parse("acme-prod-crawler-2026.01.02")
    assert plain.attributes["service"] == "crawler" and plain.attributes["deployment_id"] == "none"
    assert parser.fields() == ["tenant", "env", "service", "deployment_id", "index_date"]


def test_short_digit_runs_stay_in_the_service_and_the_rule_is_off_by_default():
    parser = grammar(deployment_prefixes=[], service_suffix="numeric", vocabularies={"service": ["svc-2"]})
    assert parser.parse("acme-prod-svc-2-2026.01.02").attributes["service"] == "svc-2"  # 1 digit < min 6
    off = grammar(deployment_prefixes=[], vocabularies={})
    assert off.parse("acme-prod-ml-service-1658254295-2026.01.02").status == "unparsed"  # without the rule: leftover segments
    assert "deployment_id" not in off.fields()


def test_numeric_suffix_with_deployment_prefixes_before_the_service():
    parser = grammar(deployment_position="before_service", deployment_prefixes=["os-data-{n}"], service_suffix="numeric", vocabularies={"service": ["ml-service"]})
    parsed = parser.parse("acme-prod-os-data-3-ml-service-1658254295-2026.01.02")
    assert parsed.status == "parsed", parsed.problem
    assert parsed.attributes["deployment"] == "os-data-3" and parsed.attributes["service"] == "ml-service" and parsed.attributes["deployment_id"] == "1658254295"
