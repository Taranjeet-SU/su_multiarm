"""M3 (unified merge plan §6.9, §6.12, §6.13): link store statuses, display data, pinning and the
model review of candidate links through the shared review ledger.

Contract rows: the seven F0 statuses and their derivation (tier A accepted; model verdicts on the
current evidence; coordinator vs model -> contested; owner resolves); display classes equal F0's
(shown / labelled / hidden) and unreviewed candidates carry a label; the store is append-only and
versioned; ``LinkView`` pins by sha256 (missing / mismatch / edited export -> no links, labelled)
and withholds customer-derived links when the identity view cannot vouch for them; review reserves
its worst case on the **shared** ledger before sending, stops at the ceiling / run cap / provider
error / invalid citation, never reviews a record it was not given; ``links`` CLI end to end. The
reviewer is a fake ``ChatProvider``: no paid call."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from su_multi_arm.core.errors import ConfigError
from su_multi_arm.crosssource.identity import IdentityView, sha256_file
from su_multi_arm.crosssource.links import DISPLAY, LABELS, STATUSES, LinkStore, LinkStoreError, LinkView, build_links
from su_multi_arm.crosssource.links.review import LinkReviewReply, review_plan, review_queue, run_review
from su_multi_arm.crosssource.links.store import derive_status
from su_multi_arm.crosssource.review_ledger import ReviewLedger
from tests.links_support import F0, PRICES, FakeReviewer, clock, f0, identity_view, pinned_documents

APPROVAL = "owner-approval-synthetic"


@pytest.fixture(scope="module")
def fx():
    return f0()


@pytest.fixture()
def setup(fx, tmp_path):
    view = identity_view(fx, tmp_path)
    result = build_links(pinned_documents(fx), view)
    store = LinkStore.create(tmp_path / "links.sqlite", now=clock)
    store.build(result.links, known_ids=result.known_ids, report=result.report, registry_version=view.registry_version)
    return store, result, view


def find(store: LinkStore, subject: str, obj: str):
    return next(s for s in store.state()["links"].values() if (s.subject_id, s.object_id) == (subject, obj))


# --- statuses and display -------------------------------------------------------------------------


def test_statuses_and_display_classes_are_f0s(fx):
    assert list(STATUSES) == fx.links["statuses"]
    display = fx.links["display"]
    assert {s for s, m in DISPLAY.items() if m == "labelled"} == set(display["shown_labelled"])
    assert {s for s, m in DISPLAY.items() if m == "shown"} == set(display["shown"])
    assert {s for s, m in DISPLAY.items() if m == "hidden"} == set(display["hidden"])
    assert set(LABELS) == set(display["shown_labelled"])


@pytest.mark.parametrize(("tier", "verdict", "decisions", "status"), [
    ("A", None, [], "accepted"),
    ("B", None, [], "candidate"),
    ("B", "uncertain", [], "candidate"),
    ("B", "agree", [], "model_accepted"),
    ("B", "refutes", [], "model_rejected"),
    ("B", None, [("accept", "coordinator")], "human_accepted"),
    ("B", "agree", [("accept", "coordinator")], "human_accepted"),
    ("B", "refutes", [("reject", "coordinator")], "human_rejected"),
    ("B", "agree", [("reject", "coordinator")], "contested"),
    ("B", "refutes", [("accept", "coordinator")], "contested"),
    ("B", "agree", [("reject", "coordinator"), ("accept", "owner")], "human_accepted"),
    ("B", "agree", [("accept", "owner"), ("reject", "coordinator")], "human_accepted"),  # the owner decision prevails
    ("A", None, [("reject", "coordinator")], "human_rejected"),
])
def test_status_derivation(tier, verdict, decisions, status):
    assert derive_status(tier, verdict, [{"action": a, "authority": w} for a, w in decisions]) == status


def test_a_build_stores_tier_a_accepted_and_rules_as_candidates(setup):
    store, result, _ = setup
    st = store.state()
    assert len(st["links"]) == len(result.links) and all(s.served for s in st["links"].values())
    assert {s.status for s in st["links"].values() if s.tier == "A"} == {"accepted"}
    assert {s.status for s in st["links"].values() if s.tier == "B"} == {"candidate"}
    report = store.report()
    assert report["link_version"] == "l1" and report["registry_version"] == "r1" and report["builder_version"] == "links-b1"
    assert report["display"]["labelled"] == report["by_status"]["candidate"] and report["review_queue"] == report["by_status"]["candidate"]
    assert report["last_build"]["builders"]["click"]["status"] == "stubbed"


def test_a_rebuild_without_changes_adds_no_definition_rows(setup):
    store, result, view = setup
    out = store.build(result.links, known_ids=result.known_ids, registry_version=view.registry_version)
    assert out["unchanged"] == len(result.links) and out["added"] == out["changed"] == 0 and out["link_version"] == "l2"


def test_a_build_refuses_links_outside_the_pinned_collection(setup):
    store, result, _ = setup
    with pytest.raises(LinkStoreError, match="outside the pinned collection"):
        store.build(result.links, known_ids=set(result.known_ids) - {result.links[0].object_id})
    assert store.report()["link_version"] == "l1"


def test_the_store_is_append_only_and_reads_do_not_change_it(setup):
    store, _, _ = setup
    before = sha256_file(store.path)
    store.report()
    store.state()
    store.export_data()
    assert sha256_file(store.path) == before
    s = find(store, "ticket:1003", "logs:incident:6315c589646fc2812bb563115bcda8e3")
    store.record_review(s.link_id, s.evidence_sha, model="m", verdict="agree", rationale="same outage")
    store.decide(s.link_id, "accept", rationale="confirmed")
    db = sqlite3.connect(store.path)
    try:
        for table in ("link", "review", "decision", "link_version", "build_link", "build_report"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f"DELETE FROM {table}")
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                db.execute(f"UPDATE {table} SET version = version + 1")
    finally:
        db.close()
    with pytest.raises(LinkStoreError, match="already exists"):
        LinkStore.create(store.path)
    (store.path.parent / "x.sqlite").write_bytes(b"not a store")
    with pytest.raises(LinkStoreError):
        LinkStore(store.path.parent / "x.sqlite")


def test_decisions_and_the_owner_queue(setup):
    store, _, _ = setup
    s = find(store, "ticket:1003", "logs:incident:6315c589646fc2812bb563115bcda8e3")
    store.record_review(s.link_id, s.evidence_sha, model="m", verdict="agree", rationale="same outage")
    assert store.link(s.link_id).status == "model_accepted"
    out = store.decide(s.link_id, "reject", rationale="different shard", authority="coordinator")
    assert out["status"] == "contested" and store.report()["owner_queue"] == [s.link_id]
    assert store.decide(s.link_id, "accept", rationale="owner confirmed", authority="owner")["status"] == "human_accepted"
    with pytest.raises(LinkStoreError, match="rationale"):
        store.decide(s.link_id, "accept", rationale=" ")
    with pytest.raises(LinkStoreError, match="unknown link"):
        store.decide("lnk-0", "accept", rationale="x")


def test_a_review_of_older_evidence_is_refused(setup):
    store, _, _ = setup
    s = find(store, "ticket:1003", "logs:incident:6315c589646fc2812bb563115bcda8e3")
    with pytest.raises(LinkStoreError, match="not the current evidence"):
        store.record_review(s.link_id, "0" * 16, model="m", verdict="agree", rationale="x")


# --- the pinned view --------------------------------------------------------------------------------


def test_the_view_pins_by_digest_and_shows_labelled_candidates(setup):
    store, _, view = setup
    lv = LinkView.pinned(store.path, sha256_file(store.path), identity=view)
    assert lv.enabled and lv.link_version == "l1" and lv.limitations == ()
    entries = lv.links_for("ticket:1003")
    by_other = {e["other_id"]: e for e in entries}
    assert by_other["tracker:ENG-103"]["status"] == "accepted" and by_other["tracker:ENG-103"]["label"] is None
    episode = by_other["logs:incident:6315c589646fc2812bb563115bcda8e3"]
    assert episode["labelled"] and episode["label"] == LABELS["candidate"]
    assert [e["status"] for e in entries][0] == "accepted"  # shown links first
    assert LinkView.pinned(store.path, "0" * 64).reason == "digest_mismatch"
    assert LinkView.pinned(store.path.parent / "absent.sqlite", "0" * 64).reason == "link_store_missing"
    assert LinkView.pinned(store.path, "0" * 64).links_for("ticket:1003") == []


def test_rejected_links_are_hidden_and_candidates_can_be_hidden(setup):
    store, _, view = setup
    s = find(store, "ticket:1003", "logs:incident:6315c589646fc2812bb563115bcda8e3")
    store.record_review(s.link_id, s.evidence_sha, model="m", verdict="refutes", rationale="different outage")
    lv = LinkView.pinned(store.path, sha256_file(store.path), identity=view)
    assert s.link_id not in {e["link_id"] for e in lv.links_for("ticket:1003")}
    assert s.link_id not in {r["link_id"] for r in lv.rows()}
    hidden = LinkView.pinned(store.path, sha256_file(store.path), identity=view, show_candidates="hidden")
    assert all(not e["labelled"] for r in ("ticket:1005", "ticket:1012") for e in hidden.links_for(r))
    assert hidden.links_for("ticket:1012") == []


def test_display_is_capped_per_record(setup):
    store, _, view = setup
    lv = LinkView.pinned(store.path, sha256_file(store.path), identity=view, max_per_record=1)
    assert len(lv.links_for("ticket:1005")) == 1


def test_customer_derived_links_are_withheld_without_a_vouching_identity_view(setup):
    store, _, view = setup
    sha = sha256_file(store.path)
    for identity, needle in ((None, "no identity view"), (IdentityView(False, "digest_mismatch"), "digest_mismatch"),
                             (IdentityView(True, None, "r7", "0" * 64, dict(view.matched)), "built with identity registry r1")):
        lv = LinkView.pinned(store.path, sha, identity=identity)
        assert lv.enabled and lv.withheld == 5 and needle in lv.limitations[0]
        assert not [r for r in lv.rows() if "customer_id" in r["evidence"]]
        assert [e["other_id"] for e in lv.links_for("ticket:1004")] == ["logs:incident:252611907407520b065c5a06cf50cda7"]  # explicit ref kept


def test_the_yaml_export_pins_like_the_store(setup, tmp_path):
    store, _, view = setup
    out = store.export(tmp_path / "links.yaml")
    assert out["sha256"] == sha256_file(tmp_path / "links.yaml")
    a = LinkView.pinned(store.path, sha256_file(store.path), identity=view)
    b = LinkView.pinned(tmp_path / "links.yaml", out["sha256"], identity=view)
    assert b.enabled and a.rows() == b.rows() and b.link_version == "l1"
    with pytest.raises(LinkStoreError, match="never overwrite"):
        store.export(tmp_path / "links.yaml")
    data = yaml.safe_load((tmp_path / "links.yaml").read_text())
    data["links"][0]["status"] = "human_accepted"
    (tmp_path / "edited.yaml").write_text(yaml.safe_dump(data))
    assert LinkView.pinned(tmp_path / "edited.yaml", sha256_file(tmp_path / "edited.yaml")).reason == "export_invalid"


# --- model review -------------------------------------------------------------------------------------


def ledger(tmp_path: Path, usd: float = 1.0) -> ReviewLedger:
    return ReviewLedger(tmp_path / "review_ledger.sqlite", usd_ceiling=usd, approval_ref=APPROVAL)


def test_dry_run_lists_the_queue_with_worst_case_and_calls_nothing(setup):
    store, result, _ = setup
    plan = review_plan(store, result.documents, price_table=PRICES, pricing_key="review-model", max_tokens=400)
    assert plan["queue"] == plan["planned"] == store.report()["review_queue"] == 13 and plan["unavailable"] == []
    for item in plan["items"]:
        assert item["worst_case_usd"] == pytest.approx((-(-item["prompt_chars"] // 3) * 3.0 + 400 * 15.0) / 1e6, abs=1e-6)
    assert plan["worst_case_total_usd"] == pytest.approx(sum(i["worst_case_usd"] for i in plan["items"]))
    assert review_plan(store, result.documents, price_table=None, pricing_key="review-model", max_tokens=400, limit=2)["worst_case_total_usd"] is None


def test_review_payload_carries_both_records_and_allowed_citations(setup):
    store, result, _ = setup
    items, _ = review_queue(store, result.documents)
    item = next(i for i in items if i.pair == "ticket-episode" and i.object["id"].endswith("6315c589646fc2812bb563115bcda8e3"))
    payload = item.payload()
    assert payload["subject"]["fields"]["description"].startswith("Users see server errors")
    assert payload["object"]["metadata"]["tenant"] == "brightmoor" and payload["link"]["evidence"]["window"]
    assert {"subject.fields.description", "object.metadata.first_hour", "link.evidence.window"} <= set(payload["allowed_citations"])
    family = next(i for i in items if i.pair == "ticket-family")
    assert family.object["kind"] == "issue_family" and family.object["title"]


def test_long_fields_are_cut_and_the_cut_is_stated(setup):
    store, result, _ = setup
    items, _ = review_queue(store, result.documents)
    docs = dict(result.documents)
    subject = docs[items[0].subject["id"]]  # every queued F0 subject is a ticket
    docs[subject.id] = subject.model_copy(update={"fields": {"description": "x" * 5000}})
    cut, _ = review_queue(store, docs)
    assert cut[0].subject["truncated"] == {"fields": ["description"], "max_chars": 1200} and len(cut[0].subject["fields"]["description"]) == 1200
    assert "truncated" not in items[0].subject


def test_review_records_verdicts_and_statuses(setup, tmp_path):
    store, result, _ = setup
    items, _ = review_queue(store, result.documents)
    a, b, c = items[0].link_id, items[1].link_id, items[2].link_id
    reviewer = FakeReviewer({a: {"verdict": "agree", "rationale": "same problem", "cited_fields": ["subject.fields.description"]},
                             b: {"verdict": "refutes", "rationale": "different problem", "cited_fields": []}})
    out = run_review(store, result.documents, reviewer, ledger(tmp_path), PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0, limit=3)
    assert out["reviewed"] == 3 and out["stopped"] is None and len(reviewer.calls) == 3
    assert (store.link(a).status, store.link(b).status, store.link(c).status) == ("model_accepted", "model_rejected", "candidate")
    assert out["ledger"]["committed_by_scope"] == {"links": pytest.approx(3 * 0.0045)} and out["ledger"]["approval_ref"] == APPROVAL
    assert store.report()["review_queue"] == 10  # an uncertain verdict is not re-queued automatically


def test_review_shares_the_identity_ceiling(setup, tmp_path):
    store, result, _ = setup
    shared = ledger(tmp_path, usd=0.01)
    r = shared.reserve(0.0095, scope="identity", subject="alias:x:y", model="m", pricing_key="review-model")
    shared.commit(r, 0.0095)
    reviewer = FakeReviewer()
    out = run_review(store, result.documents, reviewer, shared, PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert out["stopped"]["reason"] == "budget_exhausted" and out["stopped"]["kind"] == "review_usd_ceiling"
    assert reviewer.calls == [] and out["reviewed"] == 0


def test_review_stops_at_the_run_cap_and_on_provider_errors(setup, tmp_path):
    store, result, _ = setup
    out = run_review(store, result.documents, FakeReviewer(), ledger(tmp_path), PRICES, pricing_key="review-model", max_tokens=400, max_usd=0.009)
    assert out["reviewed"] == 1 and out["stopped"]["kind"] == "review_run_usd"
    first = review_queue(store, result.documents)[0][0].link_id
    out = run_review(store, result.documents, FakeReviewer(fail_on=first), ledger(tmp_path), PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert out["stopped"]["reason"] == "provider_failed" and out["reviewed"] == 0 and out["ledger"]["open_reserved_usd"] == 0
    assert store.link(first).status == "candidate"


def test_a_reply_citing_unknown_fields_is_not_recorded(setup, tmp_path):
    store, result, _ = setup
    first = review_queue(store, result.documents)[0][0].link_id
    reviewer = FakeReviewer({first: {"verdict": "agree", "rationale": "x", "cited_fields": ["subject.fields.invented"]}})
    out = run_review(store, result.documents, reviewer, ledger(tmp_path), PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert out["results"][0]["outcome"] == "invalid_citation" and out["stopped"]["reason"] == "invalid_citation"
    assert store.report()["reviews"] == 0


def test_a_record_that_was_not_supplied_is_not_reviewed(setup, tmp_path):
    store, result, _ = setup
    docs = {k: v for k, v in result.documents.items() if k != "ticket:1003"}
    reviewer = FakeReviewer()
    out = run_review(store, docs, reviewer, ledger(tmp_path), PRICES, pricing_key="review-model", max_tokens=400, max_usd=1.0)
    assert {u["missing"] for u in out["unavailable"]} == {"ticket:1003"}
    assert all(p["subject"]["id"] != "ticket:1003" for p in reviewer.calls)


def test_review_needs_a_price_and_a_strict_reply(setup, tmp_path):
    store, result, _ = setup
    with pytest.raises(ConfigError, match="price"):
        run_review(store, result.documents, FakeReviewer(), ledger(tmp_path), PRICES, pricing_key="unpriced", max_tokens=400, max_usd=1.0)
    with pytest.raises(ValueError):
        LinkReviewReply.model_validate({"verdict": "agree", "rationale": "x", "extra": 1})


# --- CLI ----------------------------------------------------------------------------------------------


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


def v3_config(tmp_path: Path, registry: Path, sha: str, *, links: bool = True) -> Path:
    data: dict[str, Any] = {
        "schema_version": 3,
        "dataset": "links-cli",
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state")}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "pipeline": {"arms": {"lexical": {"backend": "local"}}},
        "models": {"roles": {"reviewer": {"provider": "anthropic", "model": "review-model", "api_key_env": "SU_TEST_UNSET_KEY", "max_tokens": 400}}},
        "crosssource": {"identity": {"registry_path": str(registry), "sha256": sha}},
        "budgets": {"price_table": "prices.yaml"},
    }
    if links:
        data["crosssource"]["links"] = {"review": {"usd_ceiling": 1.0, "approval_ref": APPROVAL}}
    (tmp_path / "prices.yaml").write_text(yaml.safe_dump({"version": "t", "prices": {"review-model": {"input_per_million": 3.0, "output_per_million": 15.0}}}))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_cli_end_to_end(fx, tmp_path, monkeypatch, capsys):
    view = identity_view(fx, tmp_path)
    registry = tmp_path / "ids.sqlite"
    docs = tmp_path / "docs.jsonl"
    docs.write_text("".join(d.model_dump_json() + "\n" for d in pinned_documents(fx)))
    config = v3_config(tmp_path, registry, view.sha256)
    store = tmp_path / "private" / "links.sqlite"
    base = ["--config", str(config), "links"]
    code, out, err = cli(monkeypatch, capsys, *base, "build", "--store", str(store), "--documents", str(docs))
    assert code == 1 and "does not exist" in err
    code, out, err = cli(monkeypatch, capsys, *base, "build", "--create", "--store", str(store), "--documents", str(docs))
    assert code == 0, err
    assert out["links"] == 17 and out["added"] == 17 and out["report"]["identity"]["joins_enabled"] and out["sha256"] == sha256_file(store)
    calls: list[Any] = []
    monkeypatch.setattr("su_multi_arm.cli_links.provider_for", lambda role: calls.append(role) or FakeReviewer())
    code, plan, err = cli(monkeypatch, capsys, *base, "review", "--store", str(store), "--documents", str(docs), "--dry-run")
    assert code == 0, err
    assert plan["queue"] == 13 and plan["run_refused_because"] == [] and plan["ledger"] == str(tmp_path / "review_ledger.sqlite") and calls == []
    assert not (tmp_path / "review_ledger.sqlite").exists()
    code, _, err = cli(monkeypatch, capsys, *base, "review", "--store", str(store), "--documents", str(docs))
    assert code == 1 and "--max-usd" in err
    code, result, err = cli(monkeypatch, capsys, *base, "review", "--store", str(store), "--documents", str(docs), "--max-usd", "0.05", "--limit", "2")
    assert code == 0, err
    assert result["reviewed"] == 2 and result["ledger"]["calls"] == 2 and result["ledger"]["approval_ref"] == APPROVAL and len(calls) == 1
    link = result["results"][0]["link_id"]
    code, out, err = cli(monkeypatch, capsys, *base, "decide", "--store", str(store), "--link", link, "--action", "accept", "--rationale", "confirmed")
    assert code == 0, err
    assert out["status"] == "human_accepted"
    code, report, err = cli(monkeypatch, capsys, *base, "report", "--store", str(store))
    assert code == 0, err
    assert report["reviews"] == 2 and report["review_queue"] == 11 and report["by_status"]["human_accepted"] == 1
    code, shown, err = cli(monkeypatch, capsys, *base, "show", "--store", str(store), "--record", "ticket:1003")
    assert code == 0, err
    assert shown["enabled"] and shown["pinned"] == "current_file" and {e["other_id"] for e in shown["links"]} >= {"tracker:ENG-103"}
    code, shown, err = cli(monkeypatch, capsys, *base, "show", "--store", str(store), "--record", "ticket:1003", "--sha256", "0" * 64)
    assert shown["enabled"] is False and shown["reason"] == "digest_mismatch" and shown["links"] == []
    code, exported, err = cli(monkeypatch, capsys, *base, "export", "--store", str(store), "--out", str(tmp_path / "links.yaml"))
    assert code == 0, err
    assert exported["sha256"] == sha256_file(tmp_path / "links.yaml")


def test_cli_build_with_an_unpinned_registry_builds_no_customer_links(fx, tmp_path, monkeypatch, capsys):
    identity_view(fx, tmp_path)
    docs = tmp_path / "docs.jsonl"
    docs.write_text("".join(d.model_dump_json() + "\n" for d in pinned_documents(fx)))
    config = v3_config(tmp_path, tmp_path / "ids.sqlite", "0" * 64)
    code, out, err = cli(monkeypatch, capsys, "--config", str(config), "links", "build", "--create", "--store", str(tmp_path / "l.sqlite"), "--documents", str(docs))
    assert code == 0, err
    assert out["report"]["builders"]["customer_window_signature"] == {"status": "disabled", "reason": "identity_digest_mismatch"}
    assert "customer_window_signature" not in out["report"]["links"]["by_builder"]


def test_cli_review_is_refused_without_the_shared_ceiling(fx, tmp_path, monkeypatch, capsys):
    view = identity_view(fx, tmp_path)
    docs = tmp_path / "docs.jsonl"
    docs.write_text("".join(d.model_dump_json() + "\n" for d in pinned_documents(fx)))
    config = v3_config(tmp_path, tmp_path / "ids.sqlite", view.sha256, links=False)
    store = tmp_path / "l.sqlite"
    assert cli(monkeypatch, capsys, "--config", str(config), "links", "build", "--create", "--store", str(store), "--documents", str(docs))[0] == 0
    monkeypatch.setattr("su_multi_arm.cli_links.provider_for", lambda role: pytest.fail("no provider may be built"))
    code, plan, err = cli(monkeypatch, capsys, "--config", str(config), "links", "review", "--store", str(store), "--documents", str(docs), "--dry-run")
    assert code == 0 and "crosssource.links.review is not configured" in plan["run_refused_because"][0]
    code, _, err = cli(monkeypatch, capsys, "--config", str(config), "links", "review", "--store", str(store), "--documents", str(docs), "--max-usd", "1")
    assert code == 2 and "crosssource.links.review" in err


def test_fixture_root_is_synthetic():
    assert (F0 / "links" / "candidates.yaml").is_file()
