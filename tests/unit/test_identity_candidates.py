"""M2b-1 (unified merge plan §6.9): deterministic identity candidates. Only an exact or casefold
**unique** hit auto-matches; environment-suffix stripping, curated aliases, token-set Jaccard >= 0.6
and leading-token prefixes only propose candidates; two or more customers make a value ambiguous.
Acceptance: the F0 registry's deterministic entries (and its traps) are reproduced. Synthetic only."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from su_multi_arm.crosssource.identity_candidates import (
    AUTO_MATCH_RULES,
    Customer,
    classify,
    classify_values,
    jaccard,
    normalize,
    strip_environment_suffix,
    tokens,
)
from su_multi_arm.testing.fixtures.unified import load

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "unified"
CUSTOMERS = [
    Customer("cust:alder", "Alder Bank Ltd"),
    Customer("cust:bright", "Brightmoor"),
    Customer("cust:cobalt", "Cobaltfield Systems"),
    Customer("cust:east-h", "Eastvale Health"),
    Customer("cust:east-f", "Eastvale Freight"),
]


@pytest.fixture(scope="module")
def fx():
    return load(ROOT)


def test_normalization_tokens_and_suffixes():
    assert normalize("  Alder   BANK ") == "alder bank" and normalize(None) is None
    assert tokens("Alder-Bank_Ltd.") == ("alder", "bank", "ltd")
    assert jaccard(("a", "b"), ("a", "b", "c")) == pytest.approx(2 / 3) and jaccard((), ("a",)) == 0.0
    assert strip_environment_suffix("alderbank-stg") == "alderbank"
    assert strip_environment_suffix("Alder Bank PROD") == "alder bank"
    assert strip_environment_suffix("alderbank_uat") == "alderbank"
    assert strip_environment_suffix("stg") is None and strip_environment_suffix("fernhill-internal") is None


def test_exact_and_casefold_unique_auto_match():
    exact = classify("support.account", "Brightmoor", CUSTOMERS)
    assert (exact.status, exact.rule, exact.customer_id, exact.confidence) == ("matched", "exact", "cust:bright", 1.0)
    folded = classify("logs.tenant", "  BRIGHTMOOR ", CUSTOMERS)
    assert (folded.status, folded.rule, folded.customer_id) == ("matched", "casefold", "cust:bright")
    assert set(AUTO_MATCH_RULES) == {"exact", "casefold"}


def test_a_casefold_collision_is_ambiguous_never_matched():
    twins = [*CUSTOMERS, Customer("cust:bright-2", "BRIGHTMOOR")]
    c = classify("logs.tenant", "brightmoor", twins)
    assert c.status == "ambiguous" and c.customer_id is None and set(c.candidates) == {"cust:bright", "cust:bright-2"}
    exact_twins = [Customer("a", "Same"), Customer("b", "Same")]
    assert classify("x.y", "Same", exact_twins).status == "ambiguous"


def test_environment_suffix_is_a_candidate_only_even_when_the_base_is_exact():
    c = classify("logs.tenant", "brightmoor-prod", CUSTOMERS)
    assert (c.status, c.rule, c.customer_id) == ("candidate", "environment_suffix", "cust:bright")
    c = classify("logs.tenant", "alder-bank-stg", CUSTOMERS)
    assert (c.status, c.customer_id) == ("candidate", "cust:alder")


def test_token_rules_propose_candidates_and_ambiguity():
    c = classify("support.account", "Cobaltfield Systems Group", CUSTOMERS)
    assert (c.status, c.rule, c.customer_id) == ("candidate", "token_set_jaccard", "cust:cobalt") and c.confidence == pytest.approx(0.6667)
    e = classify("support.account", "Eastvale", CUSTOMERS)
    assert e.status == "ambiguous" and e.customer_id is None and e.candidates == ("cust:east-f", "cust:east-h")
    # a non-leading token never proposes a customer (precision trap)
    assert classify("support.account", "Systems", CUSTOMERS).status == "unmatched"
    assert classify("logs.tenant", "cobalt", CUSTOMERS).status == "unmatched"  # a prefix of a token is not a token
    assert classify("logs.tenant", "t-0007", CUSTOMERS).status == "unmatched"


def test_curated_list_proposes_a_candidate_only_for_known_customers():
    curated = {("analytics.company_id", "CMP-1"): "cust:alder", ("analytics.company_id", "CMP-2"): "cust:nobody"}
    c = classify("analytics.company_id", "CMP-1", CUSTOMERS, curated)
    assert (c.status, c.rule, c.customer_id) == ("candidate", "curated", "cust:alder")
    assert classify("analytics.company_id", "CMP-2", CUSTOMERS, curated).status == "unmatched"
    assert classify("tracker.customer", "CMP-1", CUSTOMERS, curated).status == "unmatched"  # namespace-scoped


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_values_keep_their_records(raw):
    c = classify("tracker.customer", raw, CUSTOMERS, records=["tracker:2", "tracker:1", "tracker:1"])
    assert (c.status, c.raw, c.customer_id, c.records) == ("missing", None, None, ("tracker:1", "tracker:2"))


def test_classify_values_is_distinct_sorted_and_adds_one_missing_entry():
    out = classify_values("support.account", ["Brightmoor", "brightmoor", None, "Brightmoor", ""], CUSTOMERS, missing_records=["ticket:9"])
    assert [(c.raw, c.status) for c in out] == [("Brightmoor", "matched"), ("brightmoor", "matched"), (None, "missing")]
    assert out[-1].records == ("ticket:9",)
    assert classify_values("support.account", ["Brightmoor"], CUSTOMERS)[-1].status == "matched"


def test_classification_does_not_depend_on_customer_order():
    shuffled = CUSTOMERS[:]
    random.Random(7).shuffle(shuffled)
    for raw in ("Eastvale", "brightmoor-prod", "Cobaltfield Systems Group", "x"):
        assert classify("a.b", raw, CUSTOMERS) == classify("a.b", raw, shuffled)


# --- F0 acceptance ----------------------------------------------------------------------------------


def _customers(fx):
    return [Customer(c["customer_id"], c["name"]) for c in fx.registry["customers"]]


def test_f0_deterministic_entries_are_reproduced(fx):
    customers = _customers(fx)
    deterministic = [a for a in fx.registry["aliases"] if a["authority"] == "deterministic"]
    assert {a["status"] for a in deterministic} == {"matched", "candidate", "ambiguous", "unmatched", "missing"}
    for a in deterministic:
        c = classify(a["namespace"], a["raw"], customers, records=a.get("records") or ())
        assert (c.status, c.customer_id, set(c.candidates)) == (a["status"], a["customer_id"], set(a.get("candidates") or ())), a
        if c.status == "matched":
            assert c.rule == a["rule"] and c.rule in AUTO_MATCH_RULES


def test_f0_coordinator_entries_are_never_auto_matched(fx):
    customers = _customers(fx)
    for a in fx.registry["aliases"]:
        if a["authority"] == "coordinator":
            assert classify(a["namespace"], a["raw"], customers).status in {"candidate", "unmatched"}, a


def test_f0_traps(fx):
    customers = _customers(fx)
    # environment-suffixed tenant: candidate only (no join until decided and reviewed)
    assert classify("logs.tenant", "alderbank-prod", customers).status == "candidate"
    assert classify("logs.tenant", "alderbank-stg", customers).status == "candidate"
    # a value shared by two customers: ambiguous in every namespace
    assert classify("logs.tenant", "eastvale", customers).status == "ambiguous"
    assert classify("support.account", "Eastvale", customers).status == "ambiguous"
    # owner rule: an organization name equal to the tenant name auto-matches (casefold)
    assert classify("logs.tenant", "brightmoor", customers).customer_id == classify("support.account", "Brightmoor", customers).customer_id == "cust:brightmoor"
