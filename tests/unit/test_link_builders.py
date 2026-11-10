"""M3 (unified merge plan §6.9, §6.12): tier A/B cross-source link builders.

Acceptance on the F0 link fixture: every planted tier A/B link is built with its tier, builder,
customer, aliases and window (the analytics click link L10 is the M7 click builder, stubbed here
and asserted as such); none of the five must-not-link traps is produced (outside the window,
contested alias, unreviewed candidate alias, ambiguous alias, dangling object). Identity joins only
through the pinned ``IdentityView`` (matched aliases); a disabled view disables the customer
builder and nothing else. Synthetic only."""

from __future__ import annotations

import pytest

from su_multi_arm.core.types import Document
from su_multi_arm.crosssource.identity import IdentityView
from su_multi_arm.crosssource.links import BUILDER_VERSION, LinkSettings, build_links
from su_multi_arm.crosssource.links.builders import link_id
from su_multi_arm.crosssource.links.signature_mentions import SignatureCatalog
from su_multi_arm.testing.fixtures.unified.collection import DOCS
from tests.links_support import f0, identity_view, incident, pinned_documents, ticket

BUILDERS = {"explicit_ref", "customer_window_signature", "signature_mention", "click"}


@pytest.fixture(scope="module")
def fx():
    return f0()


@pytest.fixture(scope="module")
def built(fx, tmp_path_factory):
    view = identity_view(fx, tmp_path_factory.mktemp("ids"))
    return build_links(pinned_documents(fx), view)


def pairs(result) -> dict[tuple[str, str], object]:
    return {(x.subject_id, x.object_id): x for x in result.links}


# --- F0 acceptance --------------------------------------------------------------------------------


def test_every_planted_tier_a_b_link_is_built(fx, built):
    got = pairs(built)
    candidates = {c["id"]: c for c in fx.links["candidates"]}
    planted = fx.links["expected"]["planted"]
    assert len(planted) == 7
    for p in planted:
        c = candidates[p["link"]]
        if c["builder"] == "click":  # analytics query <-> doc: the M7 click builder (stubbed until the analytics member exists)
            assert (p["subject_id"], p["object_id"]) not in got
            continue
        link = got[(p["subject_id"], p["object_id"])]
        assert (link.tier, link.builder, link.pair) == (c["tier"], c["builder"], c["pair"]), p["link"]
        if c["builder"] == "customer_window_signature":
            ev = c["evidence"]
            assert link.evidence["customer_id"] == ev["customer_id"]
            assert link.evidence["aliases"] == ev["aliases"]
            assert link.evidence["window"] == ev["window"]
            assert link.evidence["mention"] == ev["mention"]
        elif c["builder"] == "explicit_ref":
            assert link.evidence == {"mention": c["evidence"]["mention"], "field": c["evidence"]["field"]}
        else:
            assert link.evidence["mention"] == c["evidence"]["mention"]
    assert built.report["builders"]["click"]["status"] == "stubbed"


def test_none_of_the_must_not_link_traps_is_built(fx, built):
    got = pairs(built)
    negatives = fx.links["expected"]["negatives"]
    assert len(negatives) == 5
    assert {n["reason"] for n in negatives} == {"outside_window", "contested_alias", "unreviewed_candidate_alias", "ambiguous_alias", "dangling_object"}
    for n in negatives:
        assert (n["subject_id"], n["object_id"]) not in got, n["reason"]
        assert not any(x.object_id == n["object_id"] and x.subject_id == n["subject_id"] for x in built.links)
    # the trapped tickets never reach an episode through a customer join
    for subject in ("ticket:1006", "ticket:1007", "ticket:1009", "ticket:1010"):
        assert not [x for x in built.links if x.subject_id == subject and x.pair == "ticket-episode"], subject
    # nothing links to a record outside the pinned collection
    assert all(x.subject_id in built.known_ids and x.object_id in built.known_ids for x in built.links)


def test_trap_reasons_are_counted_with_denominators(built):
    skipped = built.report["skipped"]
    assert skipped["ticket:customer_not_matched"] == 2  # Dunmore Analytics Group (candidate), Eastvale (ambiguous); Cobaltfield is matched
    assert skipped["ticket:no_customer_value"] == 1  # ticket:1011
    assert skipped["pair:outside_window"] >= 1  # ticket:1006 vs the 2026-11-08 brightmoor episode
    assert skipped["episode:customer_not_matched"] > 0  # contested (cobalt), candidate (alderbank-prod), ambiguous (eastvale), unmatched (t-0007)
    assert built.report["inputs"]["by_kind"]["ticket"] == 12 and built.report["identity"]["registry_version"] == "r1"
    assert built.report["builder_version"] == BUILDER_VERSION and built.report["window"] == {"before_days": 1, "after_days": 7}


def test_only_matched_aliases_join(fx, built, tmp_path):
    customers = {x.evidence["customer_id"] for x in built.links if x.customer_derived}
    assert customers == {"cust:alderbank", "cust:brightmoor"}
    matched = {f"{ns}:{raw}" for (ns, raw) in identity_view(fx, tmp_path).matched}
    for x in built.links:
        if x.customer_derived:
            assert set(x.evidence["aliases"]) <= matched


def test_the_continuation_piece_of_a_cross_midnight_episode_is_linked_too(built):
    got = pairs(built)
    # ticket:1005 names crawler.fetch_timeout; alderbank-stg's episode spans 23:52 -> 00:14 in two pieces (F0 chain A)
    linked = sorted(o for (s, o), x in got.items() if s == "ticket:1005" and x.pair == "ticket-episode")
    assert len(linked) == 2


def test_a_disabled_identity_view_disables_the_customer_builder_only(fx):
    for view in (None, IdentityView(False, "digest_mismatch")):
        result = build_links(pinned_documents(fx), view)
        assert not [x for x in result.links if x.builder == "customer_window_signature"]
        assert result.report["builders"]["customer_window_signature"]["status"] == "disabled"
        assert {x.builder for x in result.links} == {"explicit_ref", "signature_mention"}
        assert ("ticket:1004", "logs:incident:252611907407520b065c5a06cf50cda7") in pairs(result)


def test_build_is_deterministic(fx, built, tmp_path):
    again = build_links(pinned_documents(fx), identity_view(fx, tmp_path))
    assert [(x.link_id, x.evidence_sha) for x in again.links] == [(x.link_id, x.evidence_sha) for x in built.links]
    assert all(x.link_id == link_id(x.pair, x.subject_id, x.object_id) for x in built.links)


# --- tier A ----------------------------------------------------------------------------------------


def _view(**matched: str) -> IdentityView:
    return IdentityView(True, None, "r9", "0" * 64, {tuple(k.split("|")): v for k, v in matched.items()})


def test_explicit_references_resolve_ids_and_urls_and_report_dangling():
    doc = Document(id=DOCS + "crawler-timeouts", dataset="unified", source="doc", kind="doc", title="Crawler timeouts", url=DOCS + "crawler-timeouts")
    other = Document(id="ticket:7", dataset="unified", source="ticket", kind="ticket", title="Other", url="https://support.example.org/tickets/7")
    t = ticket(8, None, "2026-11-01T00:00:00Z", f"See ({DOCS}crawler-timeouts/#fetch), https://support.example.org/tickets/7. "
               f"Also {DOCS}retired-page and ticket:999 and ticket:8 itself.")
    result = build_links([doc, other, t], None)
    got = pairs(result)
    assert got[("ticket:8", doc.id)].tier == "A" and got[("ticket:8", "ticket:7")].pair == "ticket-ticket"
    assert ("ticket:8", "ticket:8") not in got
    dangling = {d["mention"] for d in result.report["dangling"]["sample"]}
    assert dangling == {DOCS + "retired-page", "ticket:999"} and result.report["dangling"]["count"] == 2


def test_an_explicit_reference_beats_a_rule_for_the_same_pair():
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    t = ticket(1, "Acme", "2026-11-05T12:00:00Z", f"crawler.fetch_timeout again, alert {ep.id}")
    result = build_links([ep, t], _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"}))
    link = pairs(result)[("ticket:1", ep.id)]
    assert link.tier == "A" and link.builder == "explicit_ref"


def test_only_linkable_kinds_are_objects():
    cell = Document(id="logs:episode_cell:x1", dataset="unified", source="logs", kind="episode_cell", title="cell")
    result = build_links([cell, ticket(1, None, "2026-11-05T12:00:00Z", "see logs:episode_cell:x1")], None)
    assert result.links == [] and result.report["skipped"]["explicit_ref:object_kind_not_linkable"] == 1


# --- tier B: customer + window + signature ----------------------------------------------------------


@pytest.mark.parametrize(("created", "linked"), [
    ("2026-11-04T10:00:00Z", True),  # first - 1 d exactly
    ("2026-11-04T09:59:59Z", False),
    ("2026-11-12T11:00:00Z", True),  # last + 7 d exactly
    ("2026-11-12T11:00:01Z", False),
    ("2026-11-05T21:30:00+10:30", True),  # an offset other than UTC
])
def test_the_window_is_first_minus_before_to_last_plus_after(created, linked):
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    t = ticket(1, "Acme", created, "crawler.fetch_timeout on every crawl")
    result = build_links([ep, t], _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"}))
    assert ((t.id, ep.id) in pairs(result)) is linked


def test_window_settings_apply():
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    t = ticket(1, "Acme", "2026-11-08T12:00:00Z", "crawler.fetch_timeout")
    view = _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"})
    assert pairs(build_links([ep, t], view, LinkSettings(window_before_days=0, window_after_days=7)))
    assert not [x for x in build_links([ep, t], view, LinkSettings(window_before_days=0, window_after_days=2)).links if x.pair == "ticket-episode"]


def test_different_customers_signatures_or_untimed_tickets_do_not_link():
    view = _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a", "logs.tenant|other": "cust:b"})
    ep_other = incident("e2", "other", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    ep_rule = incident("e3", "acme", "index.write_failed", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z", title="Index write failed")
    naive = ticket(1, "Acme", "2026-11-05T12:00:00", "crawler.fetch_timeout")
    t = ticket(2, "Acme", "2026-11-05T12:00:00Z", "crawler.fetch_timeout")
    result = build_links([ep_other, ep_rule, naive, t], view)
    assert not [x for x in result.links if x.pair == "ticket-episode"]
    assert result.report["skipped"]["ticket:no_time_with_offset"] == 1
    assert result.report["skipped"]["ticket:no_episode_for_customer_and_signature"] == 1


def test_service_match_is_evidence_not_a_condition():
    view = _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"})
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z", service="crawler")
    same = ticket(1, "Acme", "2026-11-05T12:00:00Z", "crawler.fetch_timeout", service="crawler")
    other = ticket(2, "Acme", "2026-11-05T12:00:00Z", "crawler.fetch_timeout", service="indexer")
    got = pairs(build_links([ep, same, other], view))
    assert got[("ticket:1", ep.id)].evidence["service_match"] is True and got[("ticket:2", ep.id)].evidence["service_match"] is False


def test_rule_links_are_capped_per_record_nearest_first():
    view = _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"})
    eps = [incident(f"e{i}", "acme", "crawler.fetch_timeout", f"2026-11-0{i}T10:00:00Z", f"2026-11-0{i}T11:00:00Z") for i in range(1, 6)]
    t = ticket(1, "Acme", "2026-11-04T09:00:00Z", "crawler.fetch_timeout")
    result = build_links([*eps, t], view, LinkSettings(max_per_record=2))
    kept = sorted(x.object_id for x in result.links if x.pair == "ticket-episode")
    assert kept == ["logs:incident:e3", "logs:incident:e4"] and result.report["capped"] == 2


def test_customer_fields_are_overridable():
    view = _view(**{"support.account|Acme": "cust:a", "logs.tenant|acme": "cust:a"})
    ep = incident("e1", "acme", "crawler.fetch_timeout", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z")
    t = Document(id="ticket:1", dataset="unified", source="ticket", kind="ticket", title="x", fields={"description": "crawler.fetch_timeout"},
                 metadata={"organization": "Acme", "created_at": "2026-11-05T12:00:00Z"})
    assert not [x for x in build_links([ep, t], view).links if x.pair == "ticket-episode"]
    settings = LinkSettings.from_config(None, {"ticket": ("support.account", "organization")})
    assert [x for x in build_links([ep, t], view, settings).links if x.pair == "ticket-episode"]


# --- tier B: mentions ------------------------------------------------------------------------------


def test_signature_mentions_are_tokens_or_whole_titles():
    catalog = SignatureCatalog({"crawler.timeout": "Crawler request timed out", "index.write_failed": "Index write failed", "search.5xx": "Errors"})
    hit = lambda text: [m.rule for m in catalog.mentions({"description": text})]  # noqa: E731
    assert hit("the alert named crawler.timeout.") == ["crawler.timeout"]
    assert hit("crawler.timeouts everywhere") == [] and hit("x.crawler.timeout") == [] and hit("crawler.timeout_v2") == []
    assert hit("We suspect the INDEX  write\nfailed") == ["index.write_failed"]
    assert hit("errors everywhere") == []  # single-word titles are not matched
    assert hit("search.5xx/") == ["search.5xx"]
    assert [m.how for m in catalog.mentions({"a": "Index write failed", "b": "index.write_failed"})] == ["rule_id"]


def test_ticket_doc_title_and_family_doc_mentions():
    sig = Document(id="logs:signature:s1", dataset="unified", source="logs", kind="signature", title="Index write failed", metadata={"signature_id": "index.write_failed"})
    doc = Document(id=DOCS + "index-write-errors", dataset="unified", source="doc", kind="doc", title="Index write errors",
                   fields={"body": "When index.write_failed appears, retry the batch."})
    t = ticket(1, None, "2026-11-05T12:00:00Z", "Followed the index write errors page without luck")
    got = pairs(build_links([sig, doc, t], None))
    assert got[("ticket:1", doc.id)].builder == "title_mention" and got[("ticket:1", doc.id)].tier == "B"
    assert got[("family:index.write_failed", doc.id)].pair == "family-doc"


def test_families_come_from_the_pinned_log_documents(built):
    assert built.catalog.titles["crawler.fetch_timeout"] == "Crawler fetch timed out"
    assert {x.object_id for x in built.links if x.pair == "ticket-family"} <= built.catalog.families()


def test_later_versions_replace_earlier_ones(fx, built):
    assert built.report["inputs"]["replaced_versions"] > 0
    chain_b = "logs:incident:252611907407520b065c5a06cf50cda7"
    assert built.documents[chain_b].metadata["last_hour"] == "2026-11-07T00:25:00Z"  # the slice version, not the archive day's
