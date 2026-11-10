"""LLM fallback extraction (addendum A2 rev 2 §4.2; slices C1–C4) with a scripted fake provider:
cluster labels and regexes through the gate, span-grounded values, the reply cache (0 calls on a
recurring template with new line ids), budget and capacity deferral with retry, provider routes
(local → local → paid, no paid fallback by default, provider total cap), per-line egress,
grounding of quotes, field-gap fill, validators, determinism and the accounting identity after the
overlay. Mocked providers only (``httpx.MockTransport``): no network, no key, no spend."""

from __future__ import annotations

from datetime import timedelta

import pytest
import yaml

from su_multi_arm.core.errors import ConfigError
from su_multi_arm.ingestion.overlay import OverlayStore, overlay_payload_digest
from su_multi_arm.learning.patterns.egress import EgressRefused
from tests.fallback_support import DAY, FakeProvider, bodies, cluster_reply, config, heads_of, load, seal_day, session, write_library
from tests.pattern_fixtures import engine_or_skip
from tests.logs_support import ENGINE

KEY = DAY.isoformat()


@pytest.fixture(autouse=True)
def _engine():
    engine_or_skip(ENGINE)


def setup(tmp_path, **kwargs):
    write_library(tmp_path / "patterns")
    cfg = config(tmp_path, **kwargs)
    return cfg, seal_day(cfg)


def run(cfg, store, fake=None, keys=(KEY,), **kwargs):
    fake = fake or FakeProvider()
    report = session(cfg, fake).run(store, list(keys), load(cfg), **kwargs)
    return report, fake


def by_status(heads):
    out = {}
    for head in heads.values():
        out.setdefault(head.final_status if head.final_status != "unresolved" else f"unresolved:{head.reason}", []).append(head.head_id)
    return out


# --- seal-time statuses ------------------------------------------------------------------------


def test_seal_marks_residue_pending_and_field_gaps(tmp_path):
    cfg, store = setup(tmp_path)
    coverage = store.get(KEY).accounting["coverage"]
    assert coverage["by_status"]["parsed"] == 2
    # format_only heads keep their stage reason; the 3 http lines without latency are field gaps
    assert coverage["unresolved_by_reason"] == {"fallback_pending": 13, "field_gap": 3}
    rows = {r["id"]: r for r in store.read(KEY, "matches.jsonl.gz")}
    gap = rows[f"{KEY}-h010"]
    assert gap["final_status"] == "unresolved" and gap["required_gaps"] == ["latency_ms"] and gap["account_owner"] == "user"


def test_seal_without_the_fallback_uses_not_attempted(tmp_path):
    write_library(tmp_path / "patterns")
    cfg = config(tmp_path, extraction={"enabled": False})
    store = seal_day(cfg, rows=[("ERROR", "something odd happened here")])
    assert store.get(KEY).accounting["coverage"]["unresolved_by_reason"] == {"format_only": 1}


# --- cluster tier ------------------------------------------------------------------------------


def test_cluster_labels_make_provisional_rows_whose_values_are_spans(tmp_path):
    cfg, store = setup(tmp_path)
    report, fake = run(cfg, store)
    heads = heads_of(store, report)
    groups = by_status(heads)
    assert len(groups["provisional"]) == 10 and len(groups["parsed_filled"]) == 3 and groups["model_line"] == [f"{KEY}-h015"]
    timeout = heads[f"{KEY}-h003"]
    line = store_line(store, timeout.head_id)
    assert {f.name: f.value for f in timeout.fields} == {"dependency": "postgres", "latency_ms": 121}
    for value in timeout.fields:
        assert line[value.start : value.end] == str(value.value) and value.source == "provisional_pattern" and value.reply_sha
    assert timeout.pattern_owner == "learned" and timeout.extraction_source == "model_assisted"
    proposals = yaml.safe_load((tmp_path / "patterns" / "proposed.yaml").read_text())
    labelled = next(p for p in proposals if p["id"].startswith("lbl."))
    assert labelled["provenance"]["labels_by"] == "model" and "(?P<dependency>" in labelled["regex"]  # vN groups renamed (N3)
    assert fake.tasks() == ["cluster", "cluster", "fix", "lines"]


def store_line(store, head_id):
    for unit in store.units(KEY):
        for record in unit.records:
            if record.id == head_id:
                return record.message.split("\n", 1)[0]
    raise KeyError(head_id)


def test_invalid_labels_are_rejected_whole(tmp_path):
    def script(payload):
        reply = cluster_reply(payload)
        if payload["task"] == "cluster" and payload["template"].startswith("upstream"):
            reply["labels"] = [{"group": "v9", "field": "dependency", "type": "string", "entity_type": None}, {"group": "v2", "field": "latency_ms", "type": "string", "entity_type": None}]
        return reply

    cfg, store = setup(tmp_path)
    report, _ = run(cfg, store, FakeProvider(cluster=script))
    heads = heads_of(store, report)
    assert {heads[f"{KEY}-h{i:03d}"].reason for i in range(6)} == {"gate_failed"}
    row = next(c for c in report["clusters"] if c.get("outcome") == "gate_failed")
    assert "unknown group 'v9'" in row["detail"] and "declared integer, labelled string" in row["detail"]


def test_a_model_regex_is_applied_only_after_the_full_gate(tmp_path):
    def script(payload):
        reply = cluster_reply(payload)
        if payload["task"] == "cluster" and payload["template"].startswith("cache warmup"):
            reply["regex"] = r"^cache (?P<x>[a-z ]{1,40}) (?P<shard>[0-9]{1,4})$"  # also matches other residue? no: but cites an unknown id
            reply["positive_line_ids"] = ["not-a-supplied-id", *reply["positive_line_ids"][:2]]
        return reply

    cfg, store = setup(tmp_path)
    report, _ = run(cfg, store, FakeProvider(cluster=script))
    heads = heads_of(store, report)
    assert {heads[f"{KEY}-h{i:03d}"].reason for i in range(6, 10)} == {"gate_failed"}
    row = next(c for c in report["clusters"] if c.get("entry_id", "").startswith("llm."))
    assert row["gate"]["checks"]["cited_examples"]["passed"] is False


def test_reply_cache_hit_reuses_labels_on_new_line_ids_with_no_call(tmp_path):
    cfg, store = setup(tmp_path, extraction={"template": {"write_proposals": False}})  # no pending proposals: the cache path itself
    run(cfg, store)
    day2 = DAY + timedelta(days=1)
    seal_day(cfg, day2, rows=bodies(day_offset=1))
    report, fake = run(cfg, store, keys=(day2.isoformat(),))
    heads = heads_of(store, report, day2.isoformat())
    assert [t for t in fake.tasks() if t in {"cluster", "fix"}] == []  # 0 cluster/fix calls (N4)
    assert report["calls"]["pattern_learner"]["cache_hits"] == 3
    assert len(by_status(heads)["provisional"]) == 10  # re-gated on today's members and ids
    assert heads[f"{day2.isoformat()}-h000"].fields[1].value == 101


def test_a_template_with_a_pending_proposal_gets_no_new_call(tmp_path):
    cfg, store = setup(tmp_path)
    run(cfg, store)  # writes proposals for the two templates (promotion: manual)
    day2 = DAY + timedelta(days=1)
    seal_day(cfg, day2, rows=bodies(day_offset=1))
    report, fake = run(cfg, store, keys=(day2.isoformat(),))
    assert [t for t in fake.tasks() if t == "cluster"] == []
    pending = [c for c in report["clusters"] if c.get("reply") == "pending_proposal"]
    assert len(pending) == 2 and all(c["outcome"] == "provisional" for c in pending)
    assert len(by_status(heads_of(store, report, day2.isoformat()))["provisional"]) == 10


def test_a_changed_template_is_not_skipped(tmp_path):
    cfg, store = setup(tmp_path)
    run(cfg, store)
    day2 = DAY + timedelta(days=1)
    rows = [(level, body.replace("upstream timeout calling", "upstream deadline reached calling")) for level, body in bodies(day_offset=1)]
    seal_day(cfg, day2, rows=rows)
    _, fake = run(cfg, store, keys=(day2.isoformat(),))
    assert fake.tasks().count("cluster") == 1  # the changed template is asked about; the warm-up one is pending


def test_overlay_rebuild_without_calls_leaves_misses_pending(tmp_path):
    cfg, store = setup(tmp_path)
    report, fake = run(cfg, store, allow_calls=False)
    assert fake.requests == []
    reasons = {h.reason for h in heads_of(store, report).values() if h.final_status == "unresolved"}
    assert reasons <= {"fallback_pending", "egress_refused"} and "fallback_pending" in reasons


# --- budget, capacity, routes ------------------------------------------------------------------


def test_budget_deferral_then_retry_picks_the_deferred_heads_up(tmp_path):
    cfg, store = setup(tmp_path, patterns={"max_calls_per_day": 1, "max_calls_per_run": 1}, extraction={"template": {"write_proposals": False}})
    report, fake = run(cfg, store)
    heads = heads_of(store, report)
    deferred = [h for h in heads.values() if h.reason == "budget_deferred"]
    assert fake.tasks().count("cluster") == 1 and deferred
    assert report["partitions"][KEY]["deferred_heads"] >= len(deferred) and report["backlog"]["deferred_heads"] >= len(deferred)
    state = session(cfg, FakeProvider()).status(store, KEY, load(cfg))
    assert state["status"] == "deferred"
    # next run (a new day for the ledger): the deferred clusters are retried; the paid-for one is cached
    cfg2 = config(tmp_path, extraction={"template": {"write_proposals": False}})
    retry, fake2 = run(cfg2, store)
    assert fake2.tasks().count("cluster") == 1 and retry["calls"]["pattern_learner"]["cache_hits"] == 1
    assert not [h for h in heads_of(store, retry).values() if h.reason == "budget_deferred"]


def local(name, host):
    return {"name": name, "role": {"provider": "openai_compatible", "model": f"model-{name}", "base_url": f"http://{host}:9/v1", "max_tokens": 512}}


def paid_setup(tmp_path, monkeypatch, *, allow, cap=5.0):
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    prices = tmp_path / "prices.yaml"
    prices.write_text(yaml.safe_dump({"version": "test-1", "prices": {"gpt-4o-mini": {"input_per_million": 100.0, "output_per_million": 100.0}}}))
    paid = {"name": "paid", "role": {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 256, "capabilities": {"structured_output": "native"}}}
    routes = {"pattern_learner": [local("local_a", "127.0.0.1"), local("local_b", "b.localhost"), paid], "log_extractor": [local("local_a", "127.0.0.1")], "allow_paid_fallback": allow}
    return setup(
        tmp_path,
        routes=routes,
        patterns={"egress": {"allow_hosted": True}, "max_usd_per_run": 1.0, "max_usd_per_day": 1.0},
        budgets={"price_table": str(prices), "providers": {"openai": {"max_usd_total": cap}}},
    )


def test_routes_are_tried_in_order_and_a_down_local_route_is_skipped(tmp_path):
    cfg, store = setup(tmp_path, routes={"pattern_learner": [local("local_a", "127.0.0.1"), local("local_b", "b.localhost")]})
    report, fake = run(cfg, store, FakeProvider(down={"127.0.0.1"}))
    cluster_hosts = {r["host"] for r in fake.requests if r["payload"]["task"] != "lines"}
    assert cluster_hosts == {"b.localhost"}
    routes = {r["name"]: r for r in report["calls"]["pattern_learner"]["routes"]}
    assert routes["local_a"]["status"].startswith("unavailable") and routes["local_b"]["status"] == "ready"
    # throughput per tier is measured around each call (mocked transport here: not a model benchmark)
    assert routes["local_b"]["tier"] == "local" and routes["local_b"]["tokens"] == 380 * routes["local_b"]["calls"]
    assert routes["local_b"]["calls_per_min"] > 0 and routes["local_b"]["tokens_per_s"] > 0 and routes["local_a"]["calls_per_min"] is None
    assert len(by_status(heads_of(store, report))["provisional"]) == 10


def test_no_route_available_is_capacity_deferred_and_retried_next_run(tmp_path):
    cfg, store = setup(tmp_path)
    report, fake = run(cfg, store, FakeProvider(down={"127.0.0.1"}))
    heads = heads_of(store, report)
    assert fake.requests == [] and {h.reason for h in heads.values() if h.final_status == "unresolved"} == {"capacity_deferred", "egress_refused"}
    assert session(cfg, FakeProvider()).status(store, KEY, load(cfg))["status"] == "deferred"
    retry, fake2 = run(cfg, store)
    assert fake2.requests and "capacity_deferred" not in {h.reason for h in heads_of(store, retry).values()}


def test_no_paid_fallback_by_default(tmp_path, monkeypatch):
    cfg, store = paid_setup(tmp_path, monkeypatch, allow=False)
    report, fake = run(cfg, store, FakeProvider(down={"127.0.0.1", "b.localhost"}))
    assert not [r for r in fake.requests if r["host"] == "api.openai.com"]
    routes = {r["name"]: r["status"] for r in report["calls"]["pattern_learner"]["routes"]}
    assert routes["paid"] == "paid_fallback_not_allowed"
    assert {heads_of(store, report)[f"{KEY}-h000"].reason} == {"capacity_deferred"}


def test_paid_fallback_when_allowed_stops_at_the_provider_total_cap(tmp_path, monkeypatch):
    cfg, store = paid_setup(tmp_path, monkeypatch, allow=True, cap=0.18)
    report, fake = run(cfg, store, FakeProvider(down={"127.0.0.1", "b.localhost"}))
    paid_calls = [r for r in fake.requests if r["host"] == "api.openai.com"]
    assert len(paid_calls) == 1  # ~0.169 reserved, 0.038 committed: the second reservation (0.038 + ~0.169) exceeds 0.18
    caller = report["calls"]["pattern_learner"]
    assert caller["budget_refusals"] == 1 and caller["stopped"].startswith("budget_exhausted:total_usd")
    reasons = {h.reason for h in heads_of(store, report).values() if h.final_status == "unresolved"}
    assert "budget_deferred" in reasons
    totals = caller["spend"][0]["totals"][0]
    assert totals["key"] == "provider:openai" and totals["used_usd"] <= 0.18
    # local routes record calls at USD 0: the log_extractor route was local and is unpriced
    line_spend = report["calls"]["log_extractor"]["spend"]
    assert all(s["usd"] is None for s in line_spend)


def test_a_paid_route_needs_a_provider_total_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    write_library(tmp_path / "patterns")
    paid = {"name": "paid", "role": {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 256}}
    with pytest.raises(ConfigError, match="budgets.providers.openai.max_usd_total"):
        config(tmp_path, routes={"pattern_learner": [paid]}, patterns={"egress": {"allow_hosted": True}})


def test_a_paid_route_is_refused_without_finite_daily_caps(tmp_path, monkeypatch):
    """R3-B2: a provider total alone is not enough; null daily USD caps refuse the config."""
    monkeypatch.setenv("SU_TEST_FAKE_KEY", "sk-offline-not-a-key")
    write_library(tmp_path / "patterns")
    prices = tmp_path / "prices.yaml"
    prices.write_text(yaml.safe_dump({"version": "test-1", "prices": {"gpt-4o-mini": {"input_per_million": 1.0, "output_per_million": 5.0}}}))
    paid = {"name": "paid", "role": {"provider": "openai", "model": "gpt-4o-mini", "api_key_env": "SU_TEST_FAKE_KEY", "max_tokens": 256}}
    budgets = {"price_table": str(prices), "providers": {"openai": {"max_usd_total": 2.0}}}
    with pytest.raises(ConfigError, match="paid pattern_learner route needs finite"):
        config(tmp_path, routes={"pattern_learner": [paid]}, patterns={"egress": {"allow_hosted": True}}, budgets=budgets)
    with pytest.raises(ConfigError, match="paid log_extractor route needs finite"):
        config(tmp_path, routes={"log_extractor": [paid]}, extraction={"egress": {"allow_hosted": True, "max_refused_share": 0.2}}, budgets=budgets)
    ok = config(tmp_path, routes={"pattern_learner": [paid]}, patterns={"egress": {"allow_hosted": True}, "max_usd_per_run": 0.5, "max_usd_per_day": 1.0}, budgets=budgets)
    assert ok.models.routes.pattern_learner[0].name == "paid"


# --- egress ------------------------------------------------------------------------------------


def test_egress_refuses_an_unredacted_line_on_its_own(tmp_path):
    cfg, store = setup(tmp_path)
    report, fake = run(cfg, store)
    assert heads_of(store, report)[f"{KEY}-h017"].reason == "egress_refused"
    assert "ops.person@example.org" not in fake.sent_text() and "REDACTED" not in fake.sent_text()
    assert "t001" not in fake.sent_text()  # tenant values are pseudonymised in prompts


def test_egress_refuses_the_run_above_the_refused_share(tmp_path):
    cfg, store = setup(tmp_path, extraction={"egress": {"max_refused_share": 0.01}})
    with pytest.raises(EgressRefused, match="above 0.01"):
        run(cfg, store)


# --- line tail and grounding -------------------------------------------------------------------


def test_grounding_rejects_hallucinated_and_pseudonymised_quotes(tmp_path):
    def lines(payload):
        out = []
        for line in payload["lines"]:
            if "quota" in line["text"]:
                out.append({"line_id": line["id"], "fields": [{"name": "percent", "quote": "97"}], "signature_hint": None})
            else:
                out.append({"line_id": line["id"], "fields": [{"name": "endpoint", "quote": "/never/seen"}], "signature_hint": None})
        out.append({"line_id": "invented-id", "fields": [{"name": "percent", "quote": "91"}]})
        return {"lines": out}

    cfg, store = setup(tmp_path)
    report, _ = run(cfg, store, FakeProvider(lines=lines))
    heads = heads_of(store, report)
    assert heads[f"{KEY}-h015"].final_status == "unresolved" and heads[f"{KEY}-h015"].reason == "grounding_failed"
    assert heads[f"{KEY}-h016"].reason == "grounding_failed"
    assert "invented-id" not in heads


def test_quote_grounding_levels_and_ambiguity():
    from su_multi_arm.ingestion.accounting import Validators
    from su_multi_arm.learning.extraction.common import ground_quote, quoted_value

    line = "disk quota of volume data reached 91 percent"
    assert ground_quote("91", line, ["exact"]).start == line.index("91")
    assert ground_quote("VOLUME   data", line, ["exact", "normalised"]).text == "volume data"
    assert ground_quote("VOLUME data", line, ["exact"]) is None  # normalised not allowed
    twice = "retry 3 of 3"
    assert ground_quote("3", twice, ["exact"]).occurrences == 2  # identical at every occurrence: accepted
    mixed = "Code A then code a"
    assert ground_quote("CODE A", mixed, ["exact", "normalised"]) is None  # differs between occurrences
    value, reason = quoted_value("percent", "integer", "data", line, levels=["exact"], validators=Validators({}), source="model_line", reply_sha="x", model="m")
    assert value is None and reason == "type_invalid"


def test_validators_reject_out_of_range_values(tmp_path):
    rows = [("WARN", "worker reported status 777 for job alpha"), ("WARN", "worker reported status 200 for job beta")]

    def lines(payload):
        return {"lines": [{"line_id": line["id"], "fields": [{"name": "status_code", "quote": line["text"].split()[-4]}]} for line in payload["lines"]]}

    write_library(tmp_path / "patterns")
    cfg = config(tmp_path)
    store = seal_day(cfg, rows=rows)
    report, _ = run(cfg, store, FakeProvider(lines=lines))
    heads = heads_of(store, report)
    assert heads[f"{KEY}-h000"].reason == "range_invalid"
    assert heads[f"{KEY}-h001"].final_status == "model_line" and heads[f"{KEY}-h001"].fields[0].value == 200


def test_line_tail_cap_defers_the_rest(tmp_path):
    cfg, store = setup(tmp_path, extraction={"lines": {"enabled": True, "max_per_day": 1}})
    report, _ = run(cfg, store)
    reasons = [h.reason for h in heads_of(store, report).values()]
    assert reasons.count("budget_deferred") == 1 and report["tail"]["over_cap"] == 1


# --- fill --------------------------------------------------------------------------------------


def test_systematic_gap_gets_a_gated_fix_and_never_overwrites_pattern_values(tmp_path):
    cfg, store = setup(tmp_path)
    report, _ = run(cfg, store)
    head = heads_of(store, report)[f"{KEY}-h011"]
    sources = {f.name: f.source for f in head.fields}
    assert head.final_status == "parsed_filled" and sources == {"status_code": "pattern", "endpoint": "pattern", "latency_ms": "provisional_pattern"}
    fix = report["fill"]["fix_calls"][0]
    assert fix["outcome"] == "filled" and fix["gate"]["checks"]["fix_target"]["passed"]
    proposals = yaml.safe_load((tmp_path / "patterns" / "proposed.yaml").read_text())
    assert any(p["provenance"].get("target") == "msg.http.request" for p in proposals)


def test_gap_line_tier_when_the_fix_is_declined(tmp_path):
    def script(payload):
        reply = cluster_reply(payload)
        return {**reply, "action": "none", "regex": None} if payload["task"] == "fix" else reply

    def lines(payload):
        return {"lines": [{"line_id": line["id"], "fields": [{"name": "latency_ms", "quote": line["text"].rsplit(" ", 1)[1].removesuffix("ms")}, {"name": "status_code", "quote": "999"}]} for line in payload["lines"]]}

    cfg, store = setup(tmp_path)
    report, fake = run(cfg, store, FakeProvider(cluster=script, lines=lines))
    head = heads_of(store, report)[f"{KEY}-h010"]
    assert head.final_status == "parsed_filled"
    by_name = {f.name: f for f in head.fields}
    assert by_name["latency_ms"].source == "model_fill" and by_name["latency_ms"].value == 30
    assert by_name["status_code"].source == "pattern" and by_name["status_code"].value == 500  # never overwritten
    gap_ids = {f"{KEY}-h{i:03d}" for i in (10, 11, 12)}
    asked = [r["payload"]["fields"] for r in fake.requests if r["payload"]["task"] == "lines" and {line["id"] for line in r["payload"]["lines"]} & gap_ids]
    assert asked == [{"latency_ms": "integer"}]  # only the gap is asked for


# --- accounting and determinism ----------------------------------------------------------------


def test_overlay_accounting_identity_holds_and_is_rechecked(tmp_path):
    cfg, store = setup(tmp_path)
    report, _ = run(cfg, store)
    part = report["partitions"][KEY]
    manifest = OverlayStore(store).read(KEY, part["version"], part["identity"])
    for account in manifest.accounting.values():
        assert account.balanced() and sum(account.by_status.values()) == account.heads == 18
    rows_manifest, rows = OverlayStore(store).rows(KEY, part["identity"])  # the snapshot/compose read API
    assert rows_manifest.identity == part["identity"]
    assert len(rows) == sum(part["by_status"].values()) and rows_manifest.accounting == manifest.accounting
    coverage = part["coverage"]
    assert coverage["regex_only"]["extracted_coverage"] < coverage["with_llm"]["extracted_coverage"]
    assert coverage["regex_only"]["by_status"]["provisional"] == 0  # the two numbers are never merged
    assert "model assistance" in coverage["basis"]


def test_determinism_with_a_fixed_mock(tmp_path):
    digests = []
    for name in ("a", "b"):
        root = tmp_path / name
        cfg, store = setup(root)
        report, _ = run(cfg, store)
        digests.append(overlay_payload_digest(list(heads_of(store, report).values())))
    assert digests[0] == digests[1]


def test_a_stale_overlay_is_rebuilt_from_the_cache(tmp_path):
    cfg, store = setup(tmp_path)
    first, _ = run(cfg, store)
    # a new proposal changes the proposed-set identity: yesterday's overlay is stale
    session_ = session(cfg, FakeProvider())
    assert session_.status(store, KEY, load(cfg))["status"] == "stale"
    rebuilt, fake = run(cfg, store, allow_calls=False)
    assert fake.requests == []
    assert by_status(heads_of(store, rebuilt)).keys() == by_status(heads_of(store, first)).keys()
    # both overlays carry the same created_at (fixed clock): the most recently written one wins
    latest, _ = OverlayStore(store).rows(KEY)
    assert latest.identity == rebuilt["partitions"][KEY]["identity"] != first["partitions"][KEY]["identity"]
