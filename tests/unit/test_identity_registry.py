"""M2b-1 (unified merge plan §6.9, §6.12): the private, append-only identity registry.

Acceptance on the F0 registry (every status and trap): importing it reproduces every status and the
expected per-namespace report; replaying it as a process (deterministic candidates -> coordinator
decisions with evidence -> model review through a fake provider) ends in the same statuses; only
``matched`` aliases join through the pinned registry, and a digest mismatch disables joins.
No paid calls: the reviewer is a fake ``ChatProvider``. Synthetic only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from su_multi_arm.core.errors import ConfigError
from su_multi_arm.crosssource.identity import (
    STATUSES,
    Evidence,
    IdentityError,
    IdentityRegistry,
    IdentityView,
    ReviewReply,
    content_sha256,
    import_registry,
    review_plan,
    review_queue,
    run_review,
    sha256_file,
    subject,
)
from su_multi_arm.crosssource.identity_candidates import Customer
from su_multi_arm.crosssource.review_ledger import ReviewLedger
from su_multi_arm.providers.base import CallEstimate, ProviderServerError, Usage
from su_multi_arm.runtime.budget import PriceTable
from su_multi_arm.testing.fixtures.unified import load

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "unified"
FIELDS = {"support.account": ("ticket", "account"), "logs.tenant": (None, "tenant"), "analytics.company_id": ("search_query_day", "company_id"),
          "tracker.customer": ("tracker_issue", "customer")}
PRICES = PriceTable(version="test", prices={"review-model": {"input_per_million": 3.0, "output_per_million": 15.0}})
WEB = {"id": "ev-web", "kind": "web", "url": "https://www.example.com/about", "retrieved": "2026-11-09", "quote": "Synthetic page text"}


def clock():
    return datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def fx():
    return load(ROOT)


class FakeRole:
    provider, model, max_tokens = "fake", "review-model", 400


class FakeReviewer:
    """A ``ChatProvider`` stand-in: reserves and records through the meter like ``MeteredCall``."""

    name = "fake"
    role = FakeRole()

    def __init__(self, verdicts: dict[str, dict[str, Any]] | None = None, *, fail_on: str | None = None, reported: bool = True):
        self.verdicts, self.fail_on, self.reported = verdicts or {}, fail_on, reported
        self.calls: list[dict[str, Any]] = []

    def complete_json(self, system, payload, schema, budget, ctx):
        budget.reserve(CallEstimate.of(len(system) + len(json.dumps(payload)), self.role.max_tokens))
        subj = ctx.attributes["subject"]
        self.calls.append(payload)
        usage = Usage(input_tokens=1000, output_tokens=100, reported=self.reported)
        error = ProviderServerError("synthetic 500", stage=ctx.stage) if subj == self.fail_on else None
        budget.record({"stage": ctx.stage, "success": error is None, "usage": usage.as_dict()})
        if error is not None:
            raise error
        reply = self.verdicts.get(subj, {"verdict": "uncertain", "rationale": "no evidence either way", "cited_fields": []})
        return schema.model_validate(reply), usage


def documents_by_namespace(fx) -> dict[str, tuple[dict[str, int], list[str]]]:
    out = {}
    for ns, (kind, name) in FIELDS.items():
        counts: dict[str, int] = {}
        missing: list[str] = []
        for d in fx.documents():
            if (kind is None and d.source != "logs") or (kind is not None and d.kind != kind):
                continue
            value = d.metadata.get(name)
            if value is None:
                if kind is not None:
                    missing.append(d.id)
                continue
            counts[value] = counts.get(value, 0) + 1
        out[ns] = (counts, sorted(set(missing)))
    return out


def fixture_report(fx) -> dict[str, dict[str, int]]:
    return fx.registry["expected"]["report"]


def assert_report_matches(report, expected):
    assert set(report["namespaces"]) == set(expected)
    for ns, row in expected.items():
        got = report["namespaces"][ns]
        assert got["values"] == row["total"] and {s: got[s] for s in STATUSES} == {s: row[s] for s in STATUSES}, ns
        assert got["values"] == sum(got[s] for s in STATUSES)


# --- F0 acceptance: import --------------------------------------------------------------------------


def test_f0_import_reproduces_every_status_and_the_report(fx, tmp_path):
    registry = import_registry(tmp_path / "ids.sqlite", fx.registry, now=clock)
    report = registry.report()
    assert_report_matches(report, fixture_report(fx))
    assert report["grants_access"] is False and report["owner_queue"] == ["alias:logs.tenant:cobalt"]
    exported = {(a["namespace"], a["raw"]): a for a in registry.export_data()["aliases"]}
    for a in fx.registry["aliases"]:
        got = exported[(a["namespace"], a["raw"])]
        assert (got["status"], got["customer_id"]) == (a["status"], a["customer_id"]), a


def test_f0_only_matched_aliases_join_through_the_pinned_registry(fx, tmp_path):
    path = tmp_path / "ids.sqlite"
    import_registry(path, fx.registry, now=clock)
    view = IdentityView.pinned(path, sha256_file(path))
    assert view.joins_enabled and view.registry_version == "r1"
    joinable = {(j["namespace"], j["raw"]): j["customer_id"] for j in fx.registry["expected"]["joinable"]}
    assert view.matched == joinable
    for a in fx.registry["aliases"]:
        expected = a["customer_id"] if a["status"] == "matched" else None
        assert view.customer_id(a["namespace"], a["raw"]) == expected, a
    # contested, candidate and ambiguous never join (F0 link negatives)
    assert view.customer_id("logs.tenant", "cobalt") is None
    assert view.customer_id("logs.tenant", "alderbank-prod") is None
    assert view.customer_id("support.account", "Eastvale") is None
    assert view.customer_id("logs.tenant", "Brightmoor") is None  # verbatim raw: no implicit casefold at join time


def test_a_digest_mismatch_or_missing_registry_disables_every_join(fx, tmp_path):
    path = tmp_path / "ids.sqlite"
    import_registry(path, fx.registry, now=clock)
    disabled = IdentityView.pinned(path, "0" * 64)
    assert not disabled.joins_enabled and disabled.reason == "digest_mismatch" and disabled.customer_id("support.account", "Alderbank Ltd") is None
    assert IdentityView.pinned(tmp_path / "absent.sqlite", "0" * 64).reason == "registry_missing"


def test_the_yaml_export_pins_and_joins_the_same(fx, tmp_path):
    registry = import_registry(tmp_path / "ids.sqlite", fx.registry, now=clock)
    out = registry.export(tmp_path / "export.yaml")
    view = IdentityView.pinned(tmp_path / "export.yaml", out["sha256"])
    assert view.joins_enabled and view.matched == IdentityView.pinned(registry.path, sha256_file(registry.path)).matched
    # a hand-edited export with a re-computed file digest is still refused (content digest)
    data = yaml.safe_load((tmp_path / "export.yaml").read_text())
    next(a for a in data["aliases"] if a["raw"] == "cobalt")["status"] = "matched"  # promote a contested alias by hand
    (tmp_path / "edited.yaml").write_text(yaml.safe_dump(data))
    assert IdentityView.pinned(tmp_path / "edited.yaml", sha256_file(tmp_path / "edited.yaml")).reason == "export_invalid"
    with pytest.raises(IdentityError, match="never overwrite"):
        registry.export(tmp_path / "export.yaml")


def test_export_import_round_trip(fx, tmp_path):
    first = import_registry(tmp_path / "a.sqlite", fx.registry, now=clock)
    first.export(tmp_path / "a.json", "json")
    second = import_registry(tmp_path / "b.sqlite", json.loads((tmp_path / "a.json").read_text()), now=clock)
    strip = lambda r: {k: v for k, v in r.items() if k != "registry_version"}  # noqa: E731
    assert strip(second.report()) == strip(first.report())
    assert IdentityView.pinned(second.path, sha256_file(second.path)).matched == IdentityView.pinned(first.path, sha256_file(first.path)).matched


def test_import_refuses_an_inconsistent_registry_and_writes_nothing(fx, tmp_path):
    bad = yaml.safe_load(yaml.safe_dump(fx.registry))
    entry = next(a for a in bad["aliases"] if a["raw"] == "t-0007")
    entry.update(status="matched", customer_id="cust:alderbank", rule="exact")  # a deterministic claim the rules do not make
    with pytest.raises(IdentityError, match="t-0007"):
        import_registry(tmp_path / "bad.sqlite", bad, now=clock)
    assert not (tmp_path / "bad.sqlite").exists()
    refuted = yaml.safe_load(yaml.safe_dump(fx.registry))
    next(r for r in refuted["reviews"] if r["id"] == "rev-1")["verdict"] = "refutes"  # alderbank-stg would be contested, not matched
    with pytest.raises(IdentityError, match="alderbank-stg"):
        import_registry(tmp_path / "bad2.sqlite", refuted, now=clock)
    tampered = import_registry(tmp_path / "ok.sqlite", fx.registry, now=clock).export_data()
    tampered["customers"].append({"customer_id": "cust:x", "name": "X"})
    with pytest.raises(IdentityError, match="content_sha256"):
        import_registry(tmp_path / "bad3.sqlite", tampered, now=clock)


# --- F0 acceptance: the process ---------------------------------------------------------------------


def replay(fx, tmp_path) -> tuple[IdentityRegistry, ReviewLedger, FakeReviewer]:
    registry = IdentityRegistry.create(tmp_path / "ids.sqlite", now=clock)
    registry.add_customers([Customer(c["customer_id"], c["name"]) for c in fx.registry["customers"]], actor="test")
    for ns, (counts, missing) in documents_by_namespace(fx).items():
        registry.observe(ns, counts, missing_records=missing)
    evidence = {e["id"]: Evidence.model_validate(e) for e in fx.registry["evidence"]}
    for a in fx.registry["aliases"]:
        if a["authority"] != "coordinator":
            continue
        action = {"excluded": "exclude", "unmatched": "reject"}.get(a["status"], "match")
        registry.decide(a["namespace"], a["raw"], action, evidence=[evidence[e] for e in a["evidence"]], rationale=a.get("note") or "coordinator decision",
                        customer_id=a["customer_id"] if action == "match" else None, confidence=a["confidence"])
    verdicts = {r["subject"]: {"verdict": r["verdict"], "rationale": r["rationale"], "cited_fields": r["cited_fields"]} for r in fx.registry["reviews"]}
    reviewer = FakeReviewer(verdicts)
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=1.0, approval_ref="owner-approval-synthetic", now=clock)
    return registry, ledger, reviewer


def test_f0_replayed_as_a_process_ends_in_the_fixture_statuses(fx, tmp_path):
    registry, ledger, reviewer = replay(fx, tmp_path)
    before = {a["id"]: a["status"] for a in registry.export_data()["aliases"]}
    assert before[subject("logs.tenant", "alderbank-stg")] == "candidate"  # decided, not yet reviewed: no join
    assert before[subject("analytics.company_id", "CMP-00017")] == "candidate"
    result = run_review(registry, reviewer, ledger, PRICES, pricing_key="review-model", max_tokens=400, max_usd=0.5)
    assert result["stopped"] is None and result["reviewed"] == len(reviewer.calls)
    after = {(a["namespace"], a["raw"]): a for a in registry.export_data()["aliases"]}
    for a in fx.registry["aliases"]:
        got = after[(a["namespace"], a["raw"])]
        assert (got["status"], got["customer_id"]) == (a["status"], a["customer_id"]), a
    assert_report_matches(registry.report(), fixture_report(fx))
    assert ledger.usage()["calls"] == len(reviewer.calls) and ledger.usage()["committed_usd"] == pytest.approx(len(reviewer.calls) * (3000 + 1500) / 1e6)
    # every non-exact proposal was reviewed once; nothing is left in the queue
    assert review_queue(registry) == []
    reviewed = {c["alias"]["raw"] for c in reviewer.calls}
    assert {"alderbank-stg", "cobalt", "CMP-00017", "CMP-00042", "Dunmore Analytics Group", "alderbank-prod", "brightmoor"} <= reviewed
    assert "Alderbank Ltd" not in reviewed  # exact matches are not reviewed


def test_the_report_has_record_denominators_when_occurrences_are_known(fx, tmp_path):
    registry, _ledger, _ = replay(fx, tmp_path)
    support = registry.report()["namespaces"]["support.account"]
    tickets = [d for d in fx.documents() if d.kind == "ticket"]
    assert support["records"] == len(tickets) and support["missing_records"] == 1
    assert support["joinable_records"] == sum(1 for d in tickets if d.metadata.get("account") in {"Alderbank Ltd", "brightmoor", "Brightmoor", "Cobaltfield Systems"})


# --- decisions, reviews and the state machine -------------------------------------------------------


@pytest.fixture
def small(tmp_path) -> IdentityRegistry:
    registry = IdentityRegistry.create(tmp_path / "ids.sqlite", now=clock)
    registry.add_customers([Customer("cust:a", "Alder Bank"), Customer("cust:b", "Brightmoor")])
    registry.observe("logs.tenant", {"alder-bank-prod": 3, "brightmoor": 5, "zz-9": 1})
    return registry


def status(registry, ns, raw):
    return registry.alias(ns, raw)["status"]


def test_every_decision_needs_evidence_and_a_known_target(small):
    with pytest.raises(IdentityError, match="needs evidence"):
        small.decide("logs.tenant", "zz-9", "match", evidence=[], rationale="r", customer_id="cust:a")
    with pytest.raises(IdentityError, match="exactly one customer"):
        small.decide("logs.tenant", "zz-9", "match", evidence=[Evidence.model_validate(WEB)], rationale="r")
    with pytest.raises(IdentityError, match="unknown customer"):
        small.decide("logs.tenant", "zz-9", "match", evidence=[Evidence.model_validate(WEB)], rationale="r", customer_id="cust:nope")
    with pytest.raises(IdentityError, match="not been observed"):
        small.decide("logs.tenant", "never-seen", "exclude", evidence=[Evidence.model_validate(WEB)], rationale="r")
    with pytest.raises(IdentityError, match="unknown evidence id"):
        small.decide("logs.tenant", "zz-9", "exclude", evidence=["ev-missing"], rationale="r")
    with pytest.raises(IdentityError, match="rationale"):
        small.decide("logs.tenant", "zz-9", "exclude", evidence=[Evidence.model_validate(WEB)], rationale=" ")
    assert small.version() == 2  # nothing above was written


@pytest.mark.parametrize(("data", "message"), [
    ({"id": "e1", "kind": "web", "url": "https://www.example.com", "quote": "q"}, "retrieved"),
    ({"id": "e1", "kind": "web", "retrieved": "2026-11-09", "quote": "q"}, "url"),
    ({"id": "e1", "kind": "web", "url": "ftp://x", "retrieved": "2026-11-09", "quote": "q"}, "http"),
    ({"id": "e1", "kind": "source_field", "quote": "q"}, "field name"),
    ({"id": "e1", "kind": "curated_list", "quote": ""}, "quote"),
    ({"id": "e1", "kind": "rumour", "quote": "q"}, "kind"),
])
def test_evidence_fields_are_validated(data, message):
    with pytest.raises(ValueError, match=message):
        Evidence.model_validate(data)


def test_evidence_is_append_only_by_id(small):
    small.decide("logs.tenant", "zz-9", "exclude", evidence=[Evidence.model_validate(WEB)], rationale="internal test tenant")
    changed = Evidence.model_validate({**WEB, "quote": "other text"})
    with pytest.raises(IdentityError, match="append-only"):
        small.decide("logs.tenant", "zz-9", "exclude", evidence=[changed], rationale="again")
    small.decide("logs.tenant", "zz-9", "reject", evidence=["ev-web"], rationale="reuse by id")
    assert status(small, "logs.tenant", "zz-9") == "unmatched"


def test_a_match_decision_joins_only_after_an_agree_review(small):
    small.decide("logs.tenant", "zz-9", "match", evidence=[Evidence.model_validate(WEB)], rationale="tenant code listed for Alder Bank", customer_id="cust:a")
    assert status(small, "logs.tenant", "zz-9") == "candidate"
    small.record_review("logs.tenant", "zz-9", "cust:a", model="fake", verdict="uncertain", rationale="weak")
    assert status(small, "logs.tenant", "zz-9") == "candidate"
    small.record_review("logs.tenant", "zz-9", "cust:a", model="fake", verdict="agree", rationale="listed")
    alias = small.alias("logs.tenant", "zz-9")
    assert (alias["status"], alias["customer_id"], alias["authority"], alias["evidence"]) == ("matched", "cust:a", "coordinator", ["ev-web"])


def test_a_refuting_review_contests_and_only_the_owner_resolves(small):
    small.decide("logs.tenant", "alder-bank-prod", "match", evidence=[Evidence.model_validate(WEB)], rationale="suffix of the bank", customer_id="cust:a")
    small.record_review("logs.tenant", "alder-bank-prod", "cust:a", model="fake", verdict="refutes", rationale="shared code")
    assert status(small, "logs.tenant", "alder-bank-prod") == "contested"
    assert small.report()["owner_queue"] == ["alias:logs.tenant:alder-bank-prod"]
    small.decide("logs.tenant", "alder-bank-prod", "match", evidence=["ev-web"], rationale="coordinator insists", customer_id="cust:a")
    assert status(small, "logs.tenant", "alder-bank-prod") == "contested"  # a coordinator cannot override a refutation
    small.decide("logs.tenant", "alder-bank-prod", "match", evidence=["ev-web"], rationale="owner confirmed", customer_id="cust:a", authority="owner")
    alias = small.alias("logs.tenant", "alder-bank-prod")
    assert (alias["status"], alias["authority"]) == ("matched", "owner")
    small.decide("logs.tenant", "alder-bank-prod", "exclude", evidence=["ev-web"], rationale="a later coordinator decision does not override the owner")
    assert status(small, "logs.tenant", "alder-bank-prod") == "matched"


def test_a_refuted_casefold_auto_match_is_contested(small):
    assert status(small, "logs.tenant", "brightmoor") == "matched"
    assert any(i.raw == "brightmoor" for i in review_queue(small))  # casefold is non-exact: reviewed, non-gating
    small.record_review("logs.tenant", "brightmoor", "cust:b", model="fake", verdict="refutes", rationale="different company")
    assert status(small, "logs.tenant", "brightmoor") == "contested"


def test_exclude_and_reject(small):
    small.decide("logs.tenant", "zz-9", "exclude", evidence=[Evidence.model_validate(WEB)], rationale="internal")
    assert status(small, "logs.tenant", "zz-9") == "excluded"
    small.decide("logs.tenant", "brightmoor", "reject", evidence=["ev-web"], rationale="not the customer")
    alias = small.alias("logs.tenant", "brightmoor")
    assert (alias["status"], alias["customer_id"]) == ("unmatched", None)


def test_new_customers_reclassify_observed_values(small):
    assert status(small, "logs.tenant", "zz-9") == "unmatched"
    small.add_customers([Customer("cust:z", "ZZ 9")])
    # "zz-9" vs "ZZ 9": equal tokens but not casefold-equal -> a candidate only
    assert (status(small, "logs.tenant", "zz-9"), small.alias("logs.tenant", "zz-9")["customer_id"]) == ("candidate", "cust:z")
    small.add_customers([Customer("cust:z2", "zz-9")])
    assert (status(small, "logs.tenant", "zz-9"), small.alias("logs.tenant", "zz-9")["customer_id"]) == ("matched", "cust:z2")


def test_seed_customers_applies_the_owner_rule(tmp_path):
    registry = IdentityRegistry.create(tmp_path / "ids.sqlite", now=clock)
    registry.seed_customers(["Alder Bank", "alder bank", "Brightmoor", ""])
    assert [c.name for c in registry.state()["customers"]] == ["Alder Bank", "Brightmoor"]
    registry.observe("logs.tenant", ["alder bank", "BRIGHTMOOR"])
    assert {a["raw"]: a["status"] for a in registry.export_data()["aliases"]} == {"alder bank": "matched", "BRIGHTMOOR": "matched"}


def test_observing_needs_customers_and_a_valid_namespace(tmp_path):
    registry = IdentityRegistry.create(tmp_path / "ids.sqlite", now=clock)
    with pytest.raises(IdentityError, match="no customers"):
        registry.observe("logs.tenant", ["x"])
    with pytest.raises(IdentityError, match="namespace"):
        registry.observe("Logs", ["x"])


# --- append-only, versions, digests, access ---------------------------------------------------------


def test_tables_are_append_only_and_history_is_kept(small):
    small.decide("logs.tenant", "zz-9", "exclude", evidence=[Evidence.model_validate(WEB)], rationale="internal")
    with sqlite3.connect(small.path) as db:
        rows = db.execute("SELECT status, version FROM alias WHERE raw = 'zz-9' ORDER BY row_id").fetchall()
        assert rows == [("unmatched", 2), ("excluded", 3)]
        for statement in ("UPDATE alias SET status = 'matched'", "DELETE FROM alias", "DELETE FROM evidence", "UPDATE decision SET action = 'match'",
                          "DELETE FROM decision", "UPDATE customer SET name = 'x'", "DELETE FROM classification", "UPDATE registry_version SET operation = 'x'", "DELETE FROM customer"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(statement)
    versions = [v for (v,) in sqlite3.connect(small.path).execute("SELECT version FROM registry_version ORDER BY version")]
    assert versions == [1, 2, 3]


def test_reads_do_not_change_the_registry_file(small, tmp_path):
    before = sha256_file(small.path)
    small.report()
    small.export(tmp_path / "e.yaml")
    review_plan(small, price_table=PRICES, pricing_key="review-model", max_tokens=400)
    assert sha256_file(small.path) == before
    small.decide("logs.tenant", "zz-9", "exclude", evidence=[Evidence.model_validate(WEB)], rationale="internal")
    assert sha256_file(small.path) != before


def test_identity_never_grants_access(small):
    with sqlite3.connect(small.path) as db:
        columns = {c[1] for t in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") for c in db.execute(f"PRAGMA table_info({t[0]})")}
    assert not {c for c in columns if any(w in c for w in ("acl", "visib", "principal", "access", "role", "permission"))}
    assert small.report()["grants_access"] is False and small.export_data()["grants_access"] is False
    assert not hasattr(IdentityView, "visible") and not hasattr(IdentityView, "authorize")


def test_opening_a_non_registry_is_refused(tmp_path):
    with pytest.raises(IdentityError, match="does not exist"):
        IdentityRegistry(tmp_path / "none.sqlite")
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE meta (key TEXT, value TEXT)").connection.commit()
    with pytest.raises(IdentityError, match="not an identity registry"):
        IdentityRegistry(other)
    IdentityRegistry.create(tmp_path / "ids.sqlite")
    with pytest.raises(IdentityError, match="already exists"):
        IdentityRegistry.create(tmp_path / "ids.sqlite")


def test_paths_with_uri_characters_open_read_only(tmp_path):
    registry = IdentityRegistry.create(tmp_path / "a b?c#d%" / "ids.sqlite", now=clock)
    registry.add_customers([Customer("cust:a", "Alder")])
    assert registry.report()["customers"] == 1
    ledger = ReviewLedger(tmp_path / "a b?c#d%" / "ledger.sqlite", usd_ceiling=1.0, approval_ref="owner-approval-synthetic")
    from su_multi_arm.crosssource.review_ledger import read_usage

    assert read_usage(ledger.path)["ceiling_usd"] == 1.0


def test_content_digest_is_stable(fx, tmp_path):
    a = import_registry(tmp_path / "a.sqlite", fx.registry, now=clock).export_data()
    b = import_registry(tmp_path / "b.sqlite", fx.registry, now=clock).export_data()
    assert a["content_sha256"] == b["content_sha256"] == content_sha256(a)
    assert hashlib.sha256(b"x").hexdigest() != a["content_sha256"]


# --- model review -----------------------------------------------------------------------------------


def test_dry_run_lists_the_queue_with_worst_case_cost_and_calls_nothing(small):
    plan = review_plan(small, price_table=PRICES, pricing_key="review-model", max_tokens=400)
    assert plan["priced"] and plan["queue"] == plan["planned"] == 2  # the suffix candidate and the casefold match
    for item in plan["items"]:
        assert item["worst_case_usd"] == pytest.approx((-(-item["prompt_chars"] // 3) * 3.0 + 400 * 15.0) / 1e6, abs=1e-6)
    assert plan["worst_case_total_usd"] == pytest.approx(sum(i["worst_case_usd"] for i in plan["items"]))
    unpriced = review_plan(small, price_table=None, pricing_key="review-model", max_tokens=400, limit=1)
    assert unpriced["priced"] is False and unpriced["planned"] == 1 and unpriced["worst_case_total_usd"] is None


def test_review_payload_carries_evidence_and_allowed_citations(small):
    small.decide("logs.tenant", "zz-9", "match", evidence=[Evidence.model_validate(WEB)], rationale="tenant code listed", customer_id="cust:a")
    item = next(i for i in review_queue(small) if i.raw == "zz-9")
    payload = item.payload()
    assert payload["alias"]["customer_name"] == "Alder Bank" and payload["alias"]["decision_rationale"] == "tenant code listed"
    assert payload["evidence"] == [{"id": "ev-web", "kind": "web", "url": WEB["url"], "retrieved": "2026-11-09", "quote": WEB["quote"]}]
    assert "ev-web" in payload["allowed_citations"] and "raw" in payload["allowed_citations"]


def test_review_stops_at_the_ceiling_before_sending(small, tmp_path):
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=0.0001, approval_ref="owner-approval-synthetic")
    reviewer = FakeReviewer()
    result = run_review(small, reviewer, ledger, PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert result["stopped"]["reason"] == "budget_exhausted" and result["stopped"]["kind"] == "review_usd_ceiling"
    assert reviewer.calls == [] and result["reviewed"] == 0 and ledger.usage()["calls"] == 0
    assert review_queue(small)  # nothing recorded


def test_review_stops_at_the_run_cap(small, tmp_path):
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=10.0, approval_ref="owner-approval-synthetic")
    reviewer = FakeReviewer()
    result = run_review(small, reviewer, ledger, PRICES, pricing_key="review-model", max_tokens=400, max_usd=0.009)
    assert result["reviewed"] == 1 and result["stopped"]["kind"] == "review_run_usd" and len(reviewer.calls) == 1


def test_a_provider_error_stops_the_run_and_its_spend_is_committed(small, tmp_path):
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=1.0, approval_ref="owner-approval-synthetic")
    first = review_queue(small)[0].subject
    result = run_review(small, FakeReviewer(fail_on=first), ledger, PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert result["stopped"]["reason"] == "provider_failed" and result["reviewed"] == 0
    assert ledger.usage()["committed_usd"] == pytest.approx(0.0045) and ledger.usage()["open_reserved_usd"] == 0


def test_a_reply_citing_unknown_fields_is_not_recorded(small, tmp_path):
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=1.0, approval_ref="owner-approval-synthetic")
    first = review_queue(small)[0].subject
    reviewer = FakeReviewer({first: {"verdict": "agree", "rationale": "web says so", "cited_fields": ["ev-invented"]}})
    result = run_review(small, reviewer, ledger, PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert result["results"][0]["outcome"] == "invalid_citation" and result["stopped"]["reason"] == "invalid_citation"
    assert small.report()["reviews"] == 0


def test_unreported_usage_commits_the_worst_case(small, tmp_path):
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=1.0, approval_ref="owner-approval-synthetic")
    plan = review_plan(small, price_table=PRICES, pricing_key="review-model", max_tokens=400, limit=1)
    run_review(small, FakeReviewer(reported=False), ledger, PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0, limit=1)
    assert ledger.usage()["committed_usd"] == pytest.approx(plan["items"][0]["worst_case_usd"], abs=2e-6)


def test_review_needs_a_price(small, tmp_path):
    ledger = ReviewLedger(tmp_path / "ledger.sqlite", usd_ceiling=1.0, approval_ref="owner-approval-synthetic")
    with pytest.raises(ConfigError, match="price"):
        run_review(small, FakeReviewer(), ledger, PRICES, pricing_key="unpriced", max_tokens=400, max_usd=1.0)


def test_review_reply_schema_is_strict():
    with pytest.raises(ValueError):
        ReviewReply.model_validate({"verdict": "maybe", "rationale": "x"})
    with pytest.raises(ValueError):
        ReviewReply.model_validate({"verdict": "agree", "rationale": "x", "extra": 1})


# --- CLI: identity candidates | decide | review | report | export | import ---------------------------


def cli(monkeypatch, capsys, *argv) -> tuple[int, Any, str]:
    from su_multi_arm.cli import main

    monkeypatch.setattr("sys.argv", ["su-multi-arm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def v3_config(tmp_path: Path, *, links: bool = True, prices: bool = True, sha: str = "0" * 64) -> Path:
    data: dict[str, Any] = {
        "schema_version": 3,
        "dataset": "identity-cli",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "models": {"roles": {"reviewer": {"provider": "anthropic", "model": "review-model", "api_key_env": "SU_TEST_UNSET_KEY", "max_tokens": 400}}},
        "crosssource": {"identity": {"registry_path": "private/ids.sqlite", "sha256": sha}},
    }
    if links:
        data["crosssource"]["links"] = {"review": {"usd_ceiling": 1.0, "approval_ref": "owner-approval-synthetic"}}
    if prices:
        (tmp_path / "prices.yaml").write_text(yaml.safe_dump({"version": "t", "prices": {"review-model": {"input_per_million": 3.0, "output_per_million": 15.0}}}))
        data["budgets"] = {"price_table": "prices.yaml"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_cli_end_to_end_on_the_f0_members(fx, tmp_path, monkeypatch, capsys):
    config = v3_config(tmp_path)
    (tmp_path / "customers.yaml").write_text(yaml.safe_dump(fx.registry["customers"]))
    base = ["--config", str(config), "identity"]
    code, out, err = cli(monkeypatch, capsys, *base, "candidates", "--create", "--customers", str(tmp_path / "customers.yaml"),
                         "--documents", str(ROOT / "support" / "adopted.jsonl"), "--namespace", "support.account", "--field", "account", "--kind", "ticket")
    assert code == 0, err
    registry = tmp_path / "private" / "ids.sqlite"
    assert out["registry"] == str(registry) and out["pinned"] == "mismatch" and "joins stay disabled" in out["note"]
    support = out["namespaces"]["support.account"]
    assert {k: support[k] for k in ("values", "matched", "candidate", "ambiguous", "missing")} == {"values": 7, "matched": 4, "candidate": 1, "ambiguous": 1, "missing": 1}
    code, out, err = cli(monkeypatch, capsys, *base, "candidates", "--documents", str(ROOT / "tracker" / "snapshot-2026-11-08.jsonl"),
                         "--namespace", "tracker.customer", "--field", "customer")
    assert code == 0, err
    code, out, err = cli(monkeypatch, capsys, *base, "decide", "--namespace", "tracker.customer", "--raw", "fernhill-internal", "--action", "exclude",
                         "--rationale", "internal test account", "--evidence-new-id", "ev-int", "--evidence-kind", "curated_list",
                         "--evidence-quote", "fernhill-internal: internal test account")
    assert code == 0, err
    assert out["alias"]["status"] == "excluded" and out["alias"]["evidence"] == ["ev-int"]
    code, out, err = cli(monkeypatch, capsys, *base, "decide", "--namespace", "tracker.customer", "--raw", "fernhill-internal", "--action", "match",
                         "--customer", "cust:alderbank", "--rationale", "web says", "--evidence-new-id", "ev-w", "--evidence-kind", "web",
                         "--evidence-url", "https://www.example.com/x", "--evidence-quote", "q")  # web evidence without a retrieval date
    assert code == 1 and "retrieved" in err
    # review: the dry run lists the queue (Dunmore candidate, brightmoor casefold), calls nothing, creates no ledger
    calls: list[Any] = []
    monkeypatch.setattr("su_multi_arm.cli_identity.provider_for", lambda role: calls.append(role) or FakeReviewer())
    code, plan, err = cli(monkeypatch, capsys, *base, "review", "--dry-run")
    assert code == 0, err
    assert plan["queue"] == 2 and plan["priced"] and plan["run_refused_because"] == [] and plan["ledger_usage"] is None and calls == []
    assert not (tmp_path / "private" / "review_ledger.sqlite").exists()
    code, _, err = cli(monkeypatch, capsys, *base, "review")
    assert code == 1 and "--max-usd" in err
    code, result, err = cli(monkeypatch, capsys, *base, "review", "--max-usd", "0.05")
    assert code == 0, err
    assert result["reviewed"] == 2 and result["ledger"]["calls"] == 2 and result["ledger"]["approval_ref"] == "owner-approval-synthetic"
    assert result["model"] == "review-model" and len(calls) == 1
    code, report, err = cli(monkeypatch, capsys, *base, "report")
    assert code == 0, err
    assert report["reviews"] == 2 and report["review_queue"] == 0 and report["namespaces"]["tracker.customer"]["excluded"] == 1
    assert report["namespaces"]["support.account"]["records"] == 12 and report["grants_access"] is False
    code, exported, err = cli(monkeypatch, capsys, *base, "export", "--out", str(tmp_path / "export.yaml"))
    assert code == 0, err
    assert exported["sha256"] == sha256_file(tmp_path / "export.yaml")
    # pin the registry: the configured digest now matches (joins would be served through IdentityView)
    pinned = v3_config(tmp_path, sha=sha256_file(registry))
    code, out, _ = cli(monkeypatch, capsys, "--config", str(pinned), "identity", "report")
    assert out["pinned"] == "match" and "note" not in out
    code, out, _ = cli(monkeypatch, capsys, "--config", str(pinned), "identity", "describe")
    assert out["digest"] == "match"
    code, out, err = cli(monkeypatch, capsys, *base, "import", "--from", str(tmp_path / "export.yaml"), "--registry", str(tmp_path / "copy.sqlite"))
    assert code == 0, err
    assert out["report"]["namespaces"] == report["namespaces"] and out["pinned"] == "not_configured"


def test_cli_imports_the_f0_registry(tmp_path, monkeypatch, capsys):
    code, out, err = cli(monkeypatch, capsys, "--config", str(v3_config(tmp_path)), "identity", "import", "--from", str(ROOT / "identity" / "registry.yaml"),
                         "--registry", str(tmp_path / "f0.sqlite"))
    assert code == 0, err
    assert out["report"]["owner_queue"] == ["alias:logs.tenant:cobalt"]


def test_cli_review_is_refused_without_the_shared_ceiling_or_prices(tmp_path, monkeypatch, capsys, fx):
    monkeypatch.setattr("su_multi_arm.cli_identity.provider_for", lambda role: pytest.fail("no provider may be built"))
    for kwargs, message in (({"links": False}, "crosssource.links.review"), ({"prices": False}, "price_table")):
        root = tmp_path / message.replace(".", "_")
        root.mkdir()
        config = v3_config(root, **kwargs)
        import_registry(root / "private" / "ids.sqlite", fx.registry)
        code, _, err = cli(monkeypatch, capsys, "--config", str(config), "identity", "review", "--max-usd", "0.1")
        assert code == 2 and message in err
        code, plan, _ = cli(monkeypatch, capsys, "--config", str(config), "identity", "review", "--dry-run")
        assert code == 0 and plan["run_refused_because"] and plan["queue"] == 3


def test_cli_needs_a_registry(tmp_path, monkeypatch, capsys):
    path = tmp_path / "v2.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 2, "dataset": "x", "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "s")}},
                                    "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
                                    "pipeline": {"arms": {"lexical": {"backend": "local"}}}}))
    code, _, err = cli(monkeypatch, capsys, "--config", str(path), "identity", "report")
    assert code == 2 and "--registry" in err
    code, _, err = cli(monkeypatch, capsys, "--config", str(path), "identity", "report", "--registry", str(tmp_path / "none.sqlite"))
    assert code == 1 and "does not exist" in err


# --- crosssource.identity.auto_match: fuzzy (owner rule change 2026-11-10) -------------------------


def test_fuzzy_auto_match_joins_resolved_candidates_at_the_threshold_and_never_overrides_decisions(small, tmp_path):
    from su_multi_arm.core.config_crosssource import IdentityConfig

    small.add_customers([Customer("cust:z", "ZZ 9"), Customer("cust:c", "Cobalt Mills")])
    small.observe("logs.tenant", {"cobalt-mills-prod": 2, "cobalt": 1, "zz-10": 1})
    small.decide("logs.tenant", "cobalt", "match", evidence=[Evidence.model_validate(WEB)], rationale="tenant code of Cobalt Mills", customer_id="cust:c", confidence=0.85)
    small.decide("logs.tenant", "zz-10", "exclude", evidence=["ev-web"], rationale="internal test tenant")
    small.decide("logs.tenant", "alder-bank-prod", "match", evidence=["ev-web"], rationale="suffix of the bank", customer_id="cust:a", confidence=0.95)
    small.record_review("logs.tenant", "alder-bank-prod", "cust:a", model="fake", verdict="refutes", rationale="shared code")
    statuses = {raw: status(small, "logs.tenant", raw) for raw in ("zz-9", "cobalt", "cobalt-mills-prod", "zz-10", "alder-bank-prod", "brightmoor")}
    assert statuses == {"zz-9": "candidate", "cobalt": "candidate", "cobalt-mills-prod": "candidate", "zz-10": "excluded", "alder-bank-prod": "contested", "brightmoor": "matched"}
    assert small.alias("logs.tenant", "cobalt-mills-prod")["confidence"] < 0.8  # a deterministic suffix candidate (0.6)
    path = small.path
    sha = sha256_file(path)
    exact = IdentityView.pinned(path, sha)
    assert exact.auto_match == "exact" and exact.promoted == {} and set(exact.matched) == {("logs.tenant", "brightmoor")}
    fuzzy = IdentityView.from_config(IdentityConfig(registry_path=path, sha256=sha, auto_match="fuzzy", fuzzy_min_confidence=0.8))
    assert fuzzy.promoted == {("logs.tenant", "zz-9"): "fuzzy:token_set_jaccard", ("logs.tenant", "cobalt"): "fuzzy:decision"}
    assert fuzzy.customer_id("logs.tenant", "zz-9") == "cust:z" and fuzzy.customer_id("logs.tenant", "cobalt") == "cust:c"
    for raw in ("cobalt-mills-prod", "zz-10", "alder-bank-prod"):  # below the threshold, excluded, contested
        assert fuzzy.customer_id("logs.tenant", raw) is None
    assert fuzzy.customer_id("logs.tenant", "brightmoor") == "cust:b"
    lower = IdentityView.pinned(path, sha, auto_match="fuzzy", min_confidence=0.6)
    assert lower.customer_id("logs.tenant", "cobalt-mills-prod") == "cust:c"
    out = small.export(tmp_path / "export.yaml")  # the YAML export joins the same
    assert IdentityView.pinned(tmp_path / "export.yaml", out["sha256"], auto_match="fuzzy").matched == fuzzy.matched
    assert sha256_file(path) == sha  # the registry is not changed by reading it fuzzily
    with pytest.raises(IdentityError, match="auto_match"):
        IdentityView.pinned(path, sha, auto_match="loose")
    assert IdentityConfig(registry_path=path, sha256=sha).auto_match == "exact"
    with pytest.raises(ValueError):
        IdentityConfig(registry_path=path, sha256=sha, auto_match="fuzzy", fuzzy_min_confidence=0.3)
