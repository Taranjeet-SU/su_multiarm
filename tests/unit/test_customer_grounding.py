"""Registry-grounded customer text (unified merge M2b-2, §6.9, §6.11): customers named in a question
are read through the pinned identity registry (matched aliases only) and joined to their matched log
tenants; candidate, contested, ambiguous and unmatched aliases never join; a disabled registry
resolves nothing and says why. F0 synthetic registry only."""

from __future__ import annotations

from su_multi_arm.crosssource.identity import IdentityView
from su_multi_arm.decision.values import ground_customers
from tests.links_support import f0, identity_view

NS = ["support.account"]


def test_matched_aliases_name_customers_and_their_matched_tenants(tmp_path):
    view = identity_view(f0(), tmp_path)
    found = ground_customers("Alderbank Ltd says the crawl stopped; brightmoor too", view, NS, "logs.tenant")
    assert found.joins_enabled and found.registry_version == view.registry_version
    by_id = {c.customer_id: c for c in found.customers}
    assert by_id["cust:alderbank"].tenants == ["alderbank-stg"]  # alderbank-prod is only a candidate alias
    assert by_id["cust:alderbank"].text == "Alderbank Ltd" and by_id["cust:alderbank"].namespace == "support.account"
    assert by_id["cust:brightmoor"].tenants == ["brightmoor"]
    assert found.tenants() == ["alderbank-stg", "brightmoor"] and not found.ungrounded()
    assert any("alderbank-stg" in s and "matched aliases only" in s for s in found.statements())


def test_a_customer_whose_tenant_alias_is_contested_has_no_tenant(tmp_path):
    view = identity_view(f0(), tmp_path)
    found = ground_customers("Cobaltfield Systems reports failing index writes", view, NS, "logs.tenant")
    assert [c.customer_id for c in found.customers] == ["cust:cobaltfield"] and found.tenants() == []
    assert [c.text for c in found.ungrounded()] == ["Cobaltfield Systems"]
    assert "no matched log tenant" in found.statements()[0]


def test_words_inside_other_words_and_unknown_names_are_not_customers(tmp_path):
    view = identity_view(f0(), tmp_path)
    assert ground_customers("the brightmoorish theme is broken", view, NS, "logs.tenant").customers == []
    assert ground_customers("Eastvale cannot log in", view, NS, "logs.tenant").customers == []  # ambiguous alias: not matched
    assert ground_customers("Alderbank Ltd", view, ["analytics.company"], "logs.tenant").customers == []  # a namespace without that alias
    tracker = ground_customers("Alderbank Ltd", view, ["tracker.customer"], "logs.tenant").customers
    assert [(c.namespace, c.customer_id) for c in tracker] == [("tracker.customer", "cust:alderbank")]


def test_a_disabled_or_missing_registry_resolves_nothing_and_says_why(tmp_path):
    view = identity_view(f0(), tmp_path)
    broken = IdentityView.pinned(tmp_path / "ids.sqlite", "0" * 64)
    found = ground_customers("Alderbank Ltd says the crawl stopped", broken, NS, "logs.tenant")
    assert not found.joins_enabled and found.reason == "digest_mismatch" and found.customers == []
    none = ground_customers("Alderbank Ltd says the crawl stopped", None, NS, "logs.tenant")
    assert not none.joins_enabled and "crosssource.identity" in none.reason
    assert view.joins_enabled
