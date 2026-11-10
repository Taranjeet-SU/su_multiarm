"""F0 (unified merge plan rev 3): the synthetic multi-source fixtures are complete, consistent and
exercise the cases the partition core, live slices, identity registry and links must handle.

Synthetic only. The committed tree under ``tests/fixtures/unified`` must equal a fresh
``build_files()`` (regenerate with ``write_files``)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from su_multi_arm.core.config_logs import LINE_TEXT_FIELDS
from su_multi_arm.core.types import MetadataSchema
from su_multi_arm.ingestion.episode_archive import BOUNDED_KINDS
from su_multi_arm.testing.fixtures.unified import build_files, load
from su_multi_arm.testing.fixtures.unified.collection import (
    DATASET,
    EPOCHS,
    LATE_LINES,
    MERGE_GAP_SECONDS,
    RULES,
    P,
    incident_id,
)

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "unified"


@pytest.fixture(scope="module")
def fx():
    return load(ROOT)


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def all_holders(fx) -> dict[str, list[str]]:
    holders: dict[str, list[str]] = {}
    for name, docs in [*fx.members.items(), *fx.slices.items()]:
        for d in docs:
            holders.setdefault(d.id, []).append(name)
    return holders


# --- the committed tree ---------------------------------------------------------------------------


def test_committed_tree_equals_a_fresh_build():
    built = build_files()
    committed = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in ROOT.rglob("*") if p.is_file()}
    assert sorted(committed) == sorted(built), "regenerate with su_multi_arm.testing.fixtures.unified.write_files"
    stale = [name for name in built if committed[name] != built[name]]
    assert not stale, f"stale fixture files (regenerate): {stale}"


def test_fixture_is_small_and_synthetic():
    total = sum(p.stat().st_size for p in ROOT.rglob("*") if p.is_file())
    assert total < 512 * 1024
    text = "\n".join(p.read_text() for p in ROOT.rglob("*") if p.is_file())
    assert "/" + "home/" not in text and "\\Users\\" not in text
    hosts = {h.split("/")[0] for h in __import__("re").findall(r"https?://([^\s\"'<>]+)", text)}
    assert hosts and all(h.endswith((".example.org", ".example.com")) for h in hosts), hosts


# --- members and documents ------------------------------------------------------------------------


def test_every_source_and_member_tier_is_present(fx):
    sources = {d.source for d in fx.documents()}
    assert sources == {"ticket", "doc", "tracker", "logs", "analytics"}
    tiers = {(m["id"], m["tier"]) for m in fx.manifest["members"]}
    assert {("support", "adopted"), ("tracker", "snapshot"), ("logs", "archive"), ("analytics", "snapshot")} <= tiers
    kinds = {d.kind for d in fx.documents()}
    assert {"ticket", "doc", "tracker_issue", "incident", "signature_day", "episode_cell", "signature", "unit_day", "summary_coverage",
            "shared_error", "unclassified_example", "search_query_day", "search_click"} <= kinds
    for m in fx.manifest["members"]:
        docs = fx.members[f"{m['id']}/{m['key']}"]
        counts: dict[str, int] = {}
        for d in docs:
            counts[d.kind] = counts.get(d.kind, 0) + 1
        assert counts == m["counts"], m["key"]


def test_ids_are_unique_inside_every_unit_and_follow_their_namespaces(fx):
    for name, docs in [*fx.members.items(), *fx.slices.items()]:
        ids = [d.id for d in docs]
        assert len(ids) == len(set(ids)), name
    for m in fx.manifest["members"]:
        for d in fx.members[f"{m['id']}/{m['key']}"]:
            assert d.id.startswith(tuple(m["id_namespaces"])), (m["key"], d.id)
            assert d.dataset == DATASET and d.metadata.get("source", d.source) == d.source
    for docs in fx.slices.values():
        assert all(d.id.startswith("logs:") for d in docs)


def test_non_log_ids_are_unique_across_members_and_keep_legacy_formats(fx):
    holders = all_holders(fx)
    for doc_id, names in holders.items():
        if not doc_id.startswith("logs:"):
            assert len(names) == 1, (doc_id, names)
    support = fx.members[next(f"support/{m['key']}" for m in fx.manifest["members"] if m["id"] == "support")]
    assert {d.id for d in support if d.kind == "ticket"} == {f"ticket:{n}" for n in range(1001, 1013)}
    assert all(d.id == d.url for d in support if d.kind == "doc")


def test_metadata_matches_the_shared_schema(fx):
    schema = MetadataSchema.model_validate(fx.manifest["metadata_schema"])
    assert schema.unknown_fields == "reject" and schema.field("source").enum_values == ["ticket", "doc", "tracker", "logs", "analytics"]
    problems = [(d.id, v.field, v.message) for d in fx.documents() for v in schema.validate_document(d)]
    assert problems == []


# --- real log id behaviour and collisions ----------------------------------------------------------


def test_log_ids_carry_no_day_and_the_rule_catalog_repeats_every_day(fx):
    log_units = {n: docs for n, docs in [*fx.members.items(), *fx.slices.items()] if n.startswith(("logs/", "slice:"))}
    days = {"2026-10-14", "2026-10-30", *(f"2026-11-0{i}" for i in range(4, 10))}
    for docs in log_units.values():
        for d in docs:
            if d.kind in {"incident", "signature_day", "signature"}:
                assert not any(day in d.id or day.replace("-", ".") in d.id for day in days)
    signature_sets = {n: {d.id for d in docs if d.kind == "signature"} for n, docs in log_units.items()}
    with_catalog = {n: ids for n, ids in signature_sets.items() if ids}
    # every archive member and every day's base/day slice carries the catalog
    assert set(with_catalog) >= {n for n in log_units if n.startswith("logs/") or n.endswith((":base", ":day"))}
    shared = set.intersection(*with_catalog.values())
    assert len(shared) == 3  # index.write_failed, search.5xx and scheduler.job_halted are in both epochs
    collisions = fx.manifest["expected"]["collisions"]
    assert set(collisions["signature_catalog_every_day"]["ids"]) == set.union(*with_catalog.values())
    holders = all_holders(fx)
    for doc_id, names in collisions["signature_catalog_every_day"]["holders"].items():
        assert len(names) == len(holders[doc_id]) >= 2


def test_archive_and_live_collision_is_the_cross_midnight_episode(fx):
    holders = all_holders(fx)
    chain_b = incident_id("chainB_live")
    assert chain_b == incident_id("chainB_archive")  # same key and first failure -> same id
    assert fx.manifest["expected"]["collisions"]["archive_and_live"]["ids"] == sorted([chain_b, chain_b.replace("incident", "signature_day")])
    names = holders[chain_b]
    assert "logs/day:2026-11-06" in names and "slice:2026-11-07:base" in names and "slice:2026-11-07:day" in names
    archived = next(d for d in fx.members["logs/day:2026-11-06"] if d.id == chain_b)
    live = fx.live_view("2026-11-07")[chain_b]
    assert (archived.metadata["occurrences"], live.metadata["occurrences"]) == (6, 15)
    assert archived.metadata["date"] == live.metadata["date"] == "2026-11-06"  # the live version's date is the closed day


def test_chain_a_crosses_midnight_between_archive_days_as_two_ids(fx):
    a1, a2 = incident_id("chainA_1"), incident_id("chainA_2")
    assert a1 != a2
    assert a1 in {d.id for d in fx.members["logs/day:2026-11-05"]} and a2 in {d.id for d in fx.members["logs/day:2026-11-06"]}
    gap = (ts(P["chainA_2"].first) - ts(P["chainA_1"].last)).total_seconds()
    assert 0 < gap <= MERGE_GAP_SECONDS  # continuous across midnight, split by the day boundary


def test_ledger_supersede_latest_wins_within_and_across_days(fx):
    ledger = fx.ledger
    for slices in ledger["days"].values():
        for s in slices:
            assert hashlib.sha256((ROOT / s["file"]).read_bytes()).hexdigest() == s["sha256"]
            assert len(fx.slices[s["slice_id"]]) == s["documents"]
    c, d = incident_id("chainC_v2"), incident_id("D_v2")
    assert c == incident_id("chainC_v1") and d == incident_id("D_v1")
    assert fx.live_view("2026-11-08")[c].metadata["occurrences"] == 4
    assert fx.live_view("2026-11-09")[c].metadata["occurrences"] == 7  # superseded from the next day's base slice
    assert fx.live_view("2026-11-09")[c].metadata["date"] == "2026-11-08"
    assert fx.live_view("2026-11-09")[d].metadata["occurrences"] == 21
    m1025 = next(s for s in ledger["days"]["2026-11-09"] if s["slice_id"] == "slice:2026-11-09:m1025")
    assert {x["id"] for x in m1025["supersedes"]} >= {d}
    base09 = next(s for s in ledger["days"]["2026-11-09"] if s["level"] == "base")
    assert {"id": c, "previous": "slice:2026-11-08:h23"} in base09["supersedes"]
    assert sum(len(v) for v in ledger["current"].values()) <= 19 * 3


def test_gap_fill_merges_two_groups_and_retires_the_later_id(fx):
    merge = fx.manifest["expected"]["gap_merge"]
    e1, e2, merged = P["E1"], P["E2"], P["E_merged"]
    assert (ts(e2.first) - ts(e1.last)).total_seconds() > MERGE_GAP_SECONDS  # two groups before the late lines
    points = [e1.last, *LATE_LINES, e2.first]
    assert all((ts(b) - ts(a)).total_seconds() <= MERGE_GAP_SECONDS for a, b in zip(points, points[1:]))  # one group after
    assert merged.occurrences == e1.occurrences + e2.occurrences + len(LATE_LINES)
    before = {d.id for d in fx.slices["slice:2026-11-07:h13"] if d.kind == "incident"}
    after = {d.id for d in fx.slices["slice:2026-11-07:day"] if d.kind == "incident" and d.metadata["tenant"] == "alderbank-stg"}
    assert before == set(merge["before"]["incident_ids"]) == {incident_id("E1"), incident_id("E2")}
    assert after == set(merge["after"]["incident_ids"]) == {incident_id("E1")} == {merge["survivor"]}
    assert len(set(merge["before"]["episode_row_ids"])) == 2 and merge["after"]["episode_row_ids"] == merge["before"]["episode_row_ids"][:1]
    day = next(s for s in fx.ledger["days"]["2026-11-07"] if s["level"] == "day")
    assert day["retires"] == merge["tombstones"]
    assert day["replaces"] == ["slice:2026-11-07:base", "slice:2026-11-07:h13"] and fx.ledger["current"]["2026-11-07"] == [day["slice_id"]]
    # latest-wins over every registered slice would still hold E2: only the tombstone removes it
    union = {d.id for s in fx.ledger["days"]["2026-11-07"] for d in fx.slices[s["slice_id"]]}
    assert set(merge["tombstones"]) <= union and not set(merge["tombstones"]) & set(fx.live_view("2026-11-07"))


def test_every_day_is_in_exactly_one_tier_and_bounded_kinds_stay_live(fx):
    archive_days = {m["key"].split(":", 1)[1] for m in fx.manifest["members"] if m["id"] == "logs"}
    live_days = set(fx.ledger["days"])
    assert archive_days.isdisjoint(live_days)
    tiers = fx.manifest["expected"]["tiers"]
    assert {d for d, t in tiers.items() if t == "archive"} == archive_days and {d for d, t in tiers.items() if t == "provisional"} == live_days
    assert sorted(live_days) == fx.manifest["expected"]["live_window"] and len(live_days) == fx.manifest["question_clock"]["default_view_days"]
    for name, docs in fx.members.items():
        assert not any(d.kind in BOUNDED_KINDS for d in docs), name
    assert any(d.kind in BOUNDED_KINDS for docs in fx.slices.values() for d in docs)


def test_keyword_epochs(fx):
    by_epoch = {m["key"]: m["keyword_epoch"] for m in fx.manifest["members"] if m["id"] == "logs"}
    assert by_epoch == {"month:2026-10": "kw-e1", "day:2026-11-04": "kw-e1", "day:2026-11-05": "kw-e2", "day:2026-11-06": "kw-e2"}
    for key, epoch in by_epoch.items():
        catalog = {d.metadata["signature_id"] for d in fx.members[f"logs/{key}"] if d.kind == "signature"}
        assert catalog == set(EPOCHS[epoch]["rules"])
        used = {d.metadata["signature_id"] for d in fx.members[f"logs/{key}"] if d.kind == "incident"}
        assert used <= catalog
    assert set(EPOCHS["kw-e2"]["supersedes"]["crawler.timeout"]) <= set(EPOCHS["kw-e2"]["rules"]) and "crawler.timeout" not in EPOCHS["kw-e2"]["rules"]


def test_routing_expectations(fx):
    holders = all_holders(fx)
    for row in fx.manifest["expected"]["routing"]:
        if row["member"] is None:
            assert row["id"] not in holders
        else:
            names = holders[row["id"]]
            assert all(n.startswith((row["member"] + "/", "slice:")) for n in names)
            if "holders" in row:
                assert len(row["holders"]) == len(names)


# --- identity registry ----------------------------------------------------------------------------


def test_registry_has_every_status_and_a_consistent_report(fx):
    reg = fx.registry
    aliases = reg["aliases"]
    assert {a["status"] for a in aliases} == set(reg["statuses"]) == {"matched", "candidate", "contested", "ambiguous", "excluded", "unmatched", "missing"}
    assert {a["rule"] for a in aliases if a["status"] == "matched"} >= {"exact", "casefold", "curated"}
    customers = {c["customer_id"] for c in reg["customers"]}
    evidence = {e["id"] for e in reg["evidence"]}
    reviews = {r["id"]: r for r in reg["reviews"]}
    for a in aliases:
        assert set(a["evidence"]) <= evidence
        if a["status"] == "matched":
            assert a["customer_id"] in customers
            assert a["rule"] in reg["auto_match_rules"] or (a["authority"] == "coordinator" and a["evidence"] and reviews[a["review"]]["verdict"] == "agree")
        if a["status"] == "ambiguous":
            assert a["customer_id"] is None and len(set(a["candidates"]) & customers) >= 2
        if a["status"] == "contested":
            assert reviews[a["review"]]["verdict"] == "refutes"
        if a["status"] in {"unmatched", "missing", "excluded"}:
            assert a["customer_id"] is None
    report: dict[str, dict[str, int]] = {}
    for a in aliases:
        row = report.setdefault(a["namespace"], {"total": 0, **{s: 0 for s in reg["statuses"]}})
        row["total"] += 1
        row[a["status"]] += 1
    assert report == reg["expected"]["report"]
    assert all(row["total"] == sum(row[s] for s in reg["statuses"]) for row in report.values())
    assert {(j["namespace"], j["raw"]) for j in reg["expected"]["joinable"]} == {(a["namespace"], a["raw"]) for a in aliases if a["status"] == "matched"}


def test_registry_raw_values_occur_in_the_members(fx):
    field = {"support.account": ("ticket", "account"), "logs.tenant": (None, "tenant"), "analytics.company_id": ("search_query_day", "company_id"),
             "tracker.customer": ("tracker_issue", "customer")}
    for a in fx.registry["aliases"]:
        kind, name = field[a["namespace"]]
        docs = [d for d in fx.documents() if (kind is None and d.source == "logs") or d.kind == kind]
        if a["status"] == "missing":
            for rid in a["records"]:
                doc = next(d for d in docs if d.id == rid)
                assert name not in doc.metadata
        else:
            assert any(d.metadata.get(name) == a["raw"] for d in docs), a
    # trap: an environment-suffixed tenant is a candidate only, and casefold collisions are ambiguous
    by_raw = {(a["namespace"], a["raw"]): a for a in fx.registry["aliases"]}
    assert by_raw[("logs.tenant", "alderbank-prod")]["status"] == "candidate"
    assert by_raw[("logs.tenant", "eastvale")]["status"] == "ambiguous"


# --- links ----------------------------------------------------------------------------------------


def _resolvable(fx, ref: str) -> bool:
    if ref.startswith("family:"):
        return ref.split(":", 1)[1] in RULES
    if ref.startswith("q:"):
        return any(d.metadata.get("query_id") == ref for d in fx.documents())
    return ref in all_holders(fx)


def test_links_cover_every_tier_and_status_and_resolve(fx):
    links = fx.links
    cands = links["candidates"]
    assert {c["tier"] for c in cands} == {"A", "B", "C"}
    assert {c["status"] for c in cands} == set(links["statuses"])
    assert {c["pair"] for c in cands} == {"ticket-episode", "ticket-family", "ticket-doc", "family-doc", "query-doc", "query-ticket"}
    for c in cands:
        assert _resolvable(fx, c["subject_id"]) and _resolvable(fx, c["object_id"]), c["id"]
    display = links["display"]
    assert sorted(display["shown_labelled"] + display["shown"] + display["hidden"]) == sorted(links["statuses"])


def test_planted_tier_b_links_satisfy_identity_window_and_mention(fx):
    docs = {d.id: d for d in fx.documents()}
    aliases = {f"{a['namespace']}:{a['raw']}": a for a in fx.registry["aliases"]}
    window = fx.links["window"]
    for c in fx.links["candidates"]:
        if c["tier"] != "B" or c["pair"] != "ticket-episode":
            continue
        ticket, incident = docs[c["subject_id"]], docs[c["object_id"]]
        used = [aliases[a] for a in c["evidence"]["aliases"]]
        assert all(a["status"] == "matched" and a["customer_id"] == c["evidence"]["customer_id"] for a in used)
        assert ticket.metadata["account"] == used[0]["raw"] and incident.metadata["tenant"] == used[1]["raw"]
        lo = ts(incident.metadata["first_hour"]) - timedelta(days=window["before_days"])
        hi = ts(incident.metadata["last_hour"]) + timedelta(days=window["after_days"])
        assert lo <= ts(ticket.metadata["created_at"]) <= hi
        assert c["evidence"]["mention"] in " ".join(ticket.fields.values()) and incident.metadata["signature_id"] == c["evidence"]["mention"]
    for c in fx.links["candidates"]:
        if c["builder"] == "explicit_ref":  # tier A: the subject's text names the object id (no identity needed)
            assert c["tier"] == "A" and c["object_id"] in docs[c["subject_id"]].fields[c["evidence"]["field"]]
    tenant = docs[incident_id("chainB_live")].metadata["tenant"]
    assert next(a for a in fx.registry["aliases"] if a["raw"] == tenant)["status"] == "contested"


def test_negative_links_are_refusable_for_their_stated_reason(fx):
    docs = {d.id: d for d in fx.documents()}
    by_raw = {(a["namespace"], a["raw"]): a for a in fx.registry["aliases"]}
    planted = {(p["subject_id"], p["object_id"]) for p in fx.links["expected"]["planted"]}
    for n in fx.links["expected"]["negatives"]:
        assert (n["subject_id"], n["object_id"]) not in planted
        if n["reason"] == "dangling_object":
            assert n["object_id"] not in docs
            continue
        ticket, incident = docs[n["subject_id"]], docs[n["object_id"]]
        tenant = by_raw.get(("logs.tenant", incident.metadata["tenant"]))
        account = by_raw.get(("support.account", ticket.metadata.get("account")))
        if n["reason"] == "outside_window":
            assert ts(ticket.metadata["created_at"]) < ts(incident.metadata["first_hour"]) - timedelta(days=1)
            assert tenant["status"] == account["status"] == "matched" and tenant["customer_id"] == account["customer_id"]
        elif n["reason"] == "contested_alias":
            assert tenant["status"] == "contested"
        elif n["reason"] == "unreviewed_candidate_alias":
            assert tenant["status"] == "candidate"
        elif n["reason"] == "ambiguous_alias":
            assert account["status"] == "ambiguous"
        else:  # pragma: no cover
            pytest.fail(n["reason"])


# --- traps and onboarding -------------------------------------------------------------------------


def test_traps_describe_real_violations(fx):
    traps = {t["id"]: t for t in fx.traps["traps"]}
    from su_multi_arm.testing.fixtures.unified.collection import read_documents

    dup = read_documents(ROOT / traps["duplicate_id_member"]["file"])
    assert {d.id for d in dup} <= set(all_holders(fx))
    late = read_documents(ROOT / traps["archived_day_slice"]["file"])
    day = traps["archived_day_slice"]["slice"]["day"]
    assert fx.manifest["expected"]["tiers"][day] == "archive" and all(d.metadata["date"] == day for d in late)
    for kind, fields in traps["line_text_embedded"]["member"]["embed_fields"].items():
        assert set(fields) <= set(LINE_TEXT_FIELDS[kind])
    for m in fx.manifest["members"]:
        for kind, fields in m["embed_fields"].items():
            assert not set(fields) & set(LINE_TEXT_FIELDS.get(kind, ()))
    assert traps["foreign_embedding_space"]["member"]["recipe"]["embedding_space"] != fx.manifest["embedding_space"]["id"]
    from su_multi_arm.core.config import validate_config
    from su_multi_arm.core.errors import ConfigError

    data = {"backends": {"local": {"kind": "sqlite", "path": "state"}}, "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
            "pipeline": {"arms": {"lexical": {"backend": "local"}}}, **traps["v2_with_collection"]["config"]}
    with pytest.raises((ConfigError, ValueError)):
        validate_config(data)
    assert not set(traps["unroutable_ids"]["ids"]) & set(all_holders(fx))


def test_onboarding_example_has_every_step():
    data = yaml.safe_load((ROOT / "onboarding" / "analytics.yaml").read_text())
    steps = [k for k in data if k[:1] == "D" and k[1:].split("_")[0].isdigit()]
    assert [int(k[1:].split("_")[0]) for k in steps] == list(range(1, 12))


# --- one monolithic smoke ingest (the equivalence kit's baseline) ----------------------------------


def test_the_current_union_view_ingests_into_one_sqlite_engine(tmp_path, fx):
    from su_multi_arm import Engine, SearchRequest
    from su_multi_arm.core.config import validate_config

    view: dict[str, object] = {}
    for m in fx.manifest["members"]:
        for d in fx.members[f"{m['id']}/{m['key']}"]:
            view[d.id] = d
    for day in sorted(fx.ledger["current"]):
        view.update(fx.live_view(day))  # smoke only: not a ruling on archive vs live collisions
    config = validate_config({
        "schema_version": 2, "dataset": DATASET,
        "backends": {"local": {"kind": "sqlite", "path": str(tmp_path / "state"), "lexical_engine": "bm25"}},
        "storage": {k: "local" for k in ("catalog", "records", "ledger", "sessions", "cache", "communities")},
        "metadata_schema": fx.manifest["metadata_schema"],
        "models": {"roles": {"embedder": {"provider": "hash", "dimensions": 64}}},
        "pipeline": {"arms": {"lexical": {"backend": "local"}, "dense": {"backend": "local"}}},
    })
    engine = Engine(config)
    engine.ingest(list(view.values()))
    result = engine.search(SearchRequest(query="crawler fetch timeout"))
    sources = {view[h.document_id].source for h in result.hits}
    assert {"ticket", "logs"} <= sources


# --- live-shape config ----------------------------------------------------------------------------


def test_live_shape_config_materializes_and_validates(tmp_path):
    from su_multi_arm.backends.kuzu.attach import AttachMapping
    from su_multi_arm.core.config import load_config
    from su_multi_arm.pipeline.validate import config_diagnostics
    from su_multi_arm.runtime.budget import load_price_table
    from su_multi_arm.testing.fixtures.unified import materialize_live_shape

    path = materialize_live_shape(tmp_path / "live")
    raw = path.read_text()
    assert "@sha256:" not in raw and "@path:" not in raw
    assert "/" + "home/" not in (ROOT / "live_shape" / "config.template.yaml").read_text()
    with pytest.warns(UserWarning, match="prompt mode"):  # the planner schema falls back to prompt mode, as in the live shape
        config = load_config(path)
    errors = [d for d in config_diagnostics(config) if d.severity == "error"]
    assert errors == []
    arms = config.pipeline.arms
    assert config.backends["local"].lexical_engine == "bm25" and config.backends["local"].vector_engine == "faiss"
    assert config.backends["kz"].mode == "attach" and arms.graph.backend == "kz"
    assert arms.typed_query.mode == "sql_surface" and config.pipeline.decision.typed_query.routing == "router"
    assert arms.segment is not None and arms.community.claim_hits and config.communities.strategy == "imported"
    assert config.models.roles.embedder.precomputed is not None and config.projection.enabled
    assert config.request.evidence_chars_max == 120000 and config.all_pipelines()["agentic"].review.enabled
    assert config.budgets.max_usd == 0.25 and config.serving.answers.enabled
    AttachMapping.model_validate(yaml.safe_load(config.backends["kz"].attach.mapping.read_text()))
    assert load_price_table(config.budgets.price_table).prices
    for source in config.ingestion.attach:
        assert hashlib.sha256(source.path.read_bytes()).hexdigest() == source.sha256
    pre = config.models.roles.embedder.precomputed
    import numpy as np

    vectors = np.load(pre.vectors)
    assert vectors.shape == (len(pre.keys.read_text().splitlines()), config.models.roles.embedder.dimensions)
    assert hashlib.sha256(pre.vectors.read_bytes()).hexdigest() == pre.sha256
    texts = {d.id: " ".join([d.title, *d.fields.values()]) for d in load(ROOT).documents() if d.source == "ticket"}
    for line in config.communities.imported.path.read_text().splitlines():
        row = json.loads(line)
        for claim in row["claims"]:
            for ev in claim["evidence"]:
                assert ev["quote"] in texts[ev["document_id"]]
